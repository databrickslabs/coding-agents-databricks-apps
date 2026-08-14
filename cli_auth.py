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
import stat
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
        # os.replace() installs the *tmp* file's inode, so it also installs the
        # tmp file's permissions. Preserve an existing restrictive mode; a new
        # mkstemp file is already 0600.
        try:
            os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
        except OSError:
            pass  # target missing/unreadable — callers guard on existence
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
    with open(path) as f:
        settings = json.load(f)
    original = copy.deepcopy(settings)
    env = settings.get("env")
    # apiKeyHelper mode has no static ANTHROPIC_AUTH_TOKEN. OTEL headers are a
    # separate credential surface and still refresh when present.
    if isinstance(env, dict) and "ANTHROPIC_AUTH_TOKEN" in env:
        env["ANTHROPIC_AUTH_TOKEN"] = token
    refresh_claude_otel_token(settings, token)
    if settings == original:
        return False
    _atomic_write_text(path, json.dumps(settings, indent=2))
    return True


def _update_pi(token):
    """Refresh a legacy literal Pi key; preserve per-request helper commands."""
    path = os.path.join(_HOME, ".pi", "agent", "models.json")
    if not os.path.exists(path):
        return False
    with open(path) as f:
        config = json.load(f)
    provider = config.get("providers", {}).get("databricks-claude")
    if not (
        isinstance(provider, dict)
        and "apiKey" in provider
        and not str(provider["apiKey"]).startswith("!")
        and provider["apiKey"] != token
    ):
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
    with open(path) as f:
        auth = json.load(f)
    changed = False
    for provider in auth.values():
        if (
            is_opencode_api_credential(provider)
            and provider.get(OPENCODE_AUTH_KEY_FIELD) != token
        ):
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
    with open(path) as f:
        config = json.load(f)
    providers = config.get("provider")
    if not isinstance(providers, dict):
        return False
    changed = False
    for provider in providers.values():
        options = provider.get("options") if isinstance(provider, dict) else None
        if not isinstance(options, dict):
            continue
        api_key = options.get("apiKey")
        if (
            isinstance(api_key, str)
            and not api_key.startswith("{")
            and api_key != token
        ):
            options["apiKey"] = token
            changed = True
        headers = options.get("headers")
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
    with open(path) as f:
        content = f.read()
    new_content = re.sub(
        r"^(  api_key: ).*$", rf"\g<1>{token}", content, flags=re.MULTILINE
    )
    if new_content == content:
        return False
    _atomic_write_text(path, new_content)
    return True


def _replace_dotenv_key(path, key, value):
    """Replace a KEY=value line in a dotenv file."""
    if not os.path.exists(path):
        return False
    with open(path) as f:
        content = f.read()
    new_content = re.sub(
        rf"^{re.escape(key)}=.*$", f"{key}={value}", content, flags=re.MULTILINE
    )
    if new_content == content:
        return False
    _atomic_write_text(path, new_content)
    return True
