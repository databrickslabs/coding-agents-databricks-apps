"""Update literal tokens in CLI config files on PAT rotation.

Called by pat_rotator._persist_token() every 10 minutes. Lightweight —
just swaps token values in existing files, no installs or script runs.

All writes are atomic (write to `.tmp`, then `os.replace`) so a Hermes / OpenCode
/ Codex invocation that reads the file mid-update sees the old token whole or
the new token whole — never a half-written file. Errors other than "file does
not exist" surface as warnings rather than being silently swallowed.
"""

import copy
import json
import os
import re
import tempfile
import threading
import logging
from dataclasses import dataclass

from claude_otel import refresh_claude_otel_token
from utils import OPENCODE_AUTH_KEY_FIELD, is_opencode_api_credential

logger = logging.getLogger(__name__)

_HOME = os.environ.get("HOME", "/app/python/source_code")
if not _HOME or _HOME == "/":
    _HOME = "/app/python/source_code"

_CLI_REFRESH_LOCK = threading.Lock()
_CODA_OPENCODE_PROVIDER_IDS = frozenset({
    "databricks",  # legacy pre-gateway provider id
    "databricks-anthropic",
    "databricks-openai",
    "databricks-google",
    "databricks-oss",
})


@dataclass(frozen=True)
class CLIAuthRefreshResult:
    """Bounded, non-secret outcome of one all-CLI credential refresh."""

    updated: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed


def _atomic_write_text(path, content):
    """Write `content` to `path` atomically via tmp file + rename.

    Prevents the read-while-rewriting race that bit Hermes specifically:
    Hermes reads `~/.hermes/config.yaml` on every invocation, so a bare
    open(path, 'w') by the rotator could leave the file in a partial state
    visible to a concurrent Hermes call → 403 Invalid access token.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.", dir=directory, text=True
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        # Every target stores a live credential. Never inherit a pre-existing
        # loose mode; mkstemp starts at 0600 and we pin it explicitly.
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        directory_fd = os.open(
            directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _ensure_private(path):
    """Every existing file touched by the refresh path carries credentials."""
    os.chmod(path, 0o600)


def update_cli_tokens(token, *, lock_timeout=5.0):
    """Refresh every configured CLI under one bounded in-process lock.

    The report contains CLI names only. Exception text and token values never
    enter logs, and one target failure does not prevent later targets from
    receiving the current token.
    """
    if not _CLI_REFRESH_LOCK.acquire(timeout=max(0.0, float(lock_timeout))):
        logger.warning("CLI token refresh skipped: refresh lock timed out")
        return CLIAuthRefreshResult(failed=("refresh_lock",))

    updated = []
    skipped = []
    failed = []
    updaters = (
        ("claude", _update_claude),
        ("pi", _update_pi),
        ("codex", _update_codex),
        ("opencode", _update_opencode),
        ("opencode_provider", _update_opencode_provider_headers),
        ("gemini", _update_gemini),
        ("hermes", _update_hermes),
    )
    try:
        for name, updater in updaters:
            try:
                changed = updater(token)
            except Exception as error:
                failed.append(name)
                # The exception can contain file contents or the token. Log only
                # the target and exception class, never its message.
                logger.warning(
                    "CLI token refresh failed for %s (%s)",
                    name,
                    type(error).__name__,
                )
                continue
            (updated if changed else skipped).append(name)
    finally:
        _CLI_REFRESH_LOCK.release()

    if failed:
        logger.warning("CLI token refresh incomplete: failed=%s", ",".join(failed))
    else:
        logger.info("CLI token refresh complete")
    return CLIAuthRefreshResult(
        updated=tuple(updated), skipped=tuple(skipped), failed=tuple(failed)
    )


def _update_claude(token):
    """Update Claude tokens in ~/.claude/settings.json."""
    path = os.path.join(_HOME, ".claude", "settings.json")
    if not os.path.exists(path):
        return False
    _ensure_private(path)
    with open(path) as f:
        settings = json.load(f)
    original = copy.deepcopy(settings)
    env = settings.get("env")
    # apiKeyHelper mode has no static ANTHROPIC_AUTH_TOKEN. OTEL headers are a
    # separate credential surface and still refresh when present.
    has_static = isinstance(env, dict) and "ANTHROPIC_AUTH_TOKEN" in env
    has_otel = isinstance(env, dict) and any(
        key.startswith("OTEL_EXPORTER_OTLP_") and key.endswith("_HEADERS")
        for key in env
    )
    if has_static:
        env["ANTHROPIC_AUTH_TOKEN"] = token
    refresh_claude_otel_token(settings, token)
    if not settings.get("apiKeyHelper") and not has_static and not has_otel:
        raise ValueError("Claude credential field is missing")
    if settings == original:
        return False
    _atomic_write_text(path, json.dumps(settings, indent=2))
    return True


def _update_pi(token):
    """Refresh a legacy literal Pi key; preserve per-request helper commands."""
    path = os.path.join(_HOME, ".pi", "agent", "models.json")
    if not os.path.exists(path):
        return False
    _ensure_private(path)
    with open(path) as f:
        config = json.load(f)
    provider = config.get("providers", {}).get("databricks-claude")
    if provider is None:
        return False
    if not isinstance(provider, dict) or "apiKey" not in provider:
        raise ValueError("Pi credential field is missing")
    if str(provider["apiKey"]).startswith("!") or provider["apiKey"] == token:
        return False
    provider["apiKey"] = token
    _atomic_write_text(path, json.dumps(config, indent=2))
    return True


def _update_codex(token):
    """Update OPENAI_API_KEY in ~/.codex/.env."""
    return _replace_dotenv_key(
        os.path.join(_HOME, ".codex", ".env"), "OPENAI_API_KEY", token
    )


def _update_opencode(token):
    """Rotate only OpenCode's API credential union variants."""
    path = os.path.join(_HOME, ".local", "share", "opencode", "auth.json")
    if not os.path.exists(path):
        return False
    _ensure_private(path)
    with open(path) as f:
        auth = json.load(f)
    changed = False
    managed_ids = _CODA_OPENCODE_PROVIDER_IDS.intersection(auth)
    if not managed_ids:
        return False
    for provider_id in managed_ids:
        provider = auth[provider_id]
        if not is_opencode_api_credential(provider):
            raise ValueError(f"OpenCode credential shape is invalid for {provider_id}")
        if provider.get(OPENCODE_AUTH_KEY_FIELD) != token:
            provider[OPENCODE_AUTH_KEY_FIELD] = token
            changed = True
    if not changed:
        return False
    _atomic_write_text(path, json.dumps(auth, indent=2))
    return True


def _update_opencode_provider_headers(token):
    """Rotate literal OpenCode provider keys and Authorization headers."""
    path = os.path.join(_HOME, ".config", "opencode", "opencode.json")
    if not os.path.exists(path):
        return False
    _ensure_private(path)
    with open(path) as f:
        config = json.load(f)
    providers = config.get("provider")
    if not isinstance(providers, dict):
        return False
    changed = False
    managed_ids = _CODA_OPENCODE_PROVIDER_IDS.intersection(providers)
    if not managed_ids:
        return False
    for provider_id in managed_ids:
        provider = providers[provider_id]
        options = provider.get("options") if isinstance(provider, dict) else None
        if not isinstance(options, dict):
            raise ValueError(f"OpenCode provider options missing for {provider_id}")
        if "apiKey" not in options or not isinstance(options["apiKey"], str):
            raise ValueError(f"OpenCode provider apiKey invalid for {provider_id}")
        headers = options.get("headers")
        if headers is not None and not isinstance(headers, dict):
            raise ValueError(f"OpenCode provider headers invalid for {provider_id}")
        if (
            isinstance(headers, dict)
            and "Authorization" in headers
            and not isinstance(headers["Authorization"], str)
        ):
            raise ValueError(
                f"OpenCode Authorization header invalid for {provider_id}"
            )

        api_key = options["apiKey"]
        if (
            not api_key.startswith("{")
            and api_key != token
        ):
            options["apiKey"] = token
            changed = True
        expected = f"Bearer {token}"
        if (
            isinstance(headers, dict)
            and isinstance(headers.get("Authorization"), str)
            and headers["Authorization"] != expected
        ):
            headers["Authorization"] = expected
            changed = True
    if not changed:
        return False
    _atomic_write_text(path, json.dumps(config, indent=2))
    os.chmod(path, 0o600)
    return True


def _update_gemini(token):
    """Update GEMINI_API_KEY in ~/.gemini/.env."""
    return _replace_dotenv_key(
        os.path.join(_HOME, ".gemini", ".env"), "GEMINI_API_KEY", token
    )


def _update_hermes(token):
    """Update api_key lines in ~/.hermes/config.yaml."""
    path = os.path.join(_HOME, ".hermes", "config.yaml")
    if not os.path.exists(path):
        return False
    _ensure_private(path)
    with open(path) as f:
        content = f.read()
    pattern = re.compile(r"^(  api_key: ).*$", flags=re.MULTILINE)
    if not pattern.search(content):
        raise ValueError("Hermes credential field is missing")
    new_content = pattern.sub(lambda match: match.group(1) + token, content)
    if new_content == content:
        return False
    _atomic_write_text(path, new_content)
    return True


def _replace_dotenv_key(path, key, value):
    """Replace a KEY=value line in a dotenv file."""
    if not os.path.exists(path):
        return False
    _ensure_private(path)
    with open(path) as f:
        content = f.read()
    pattern = re.compile(rf"^{re.escape(key)}=.*$", flags=re.MULTILINE)
    if not pattern.search(content):
        raise ValueError(f"{key} credential field is missing")
    new_content = pattern.sub(lambda _match: f"{key}={value}", content)
    if new_content == content:
        return False
    _atomic_write_text(path, new_content)
    return True
