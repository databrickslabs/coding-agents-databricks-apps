"""Enterprise-mode configuration for restricted networks (proxy/registry mode).

CoDA's default behaviour assumes outbound internet to public registries
(npmjs.org, pypi.org), GitHub (release tarballs, api.github.com), and
claude.ai (Claude Code installer). In locked-down enterprise environments
these are firewalled — only internal mirrors (JFrog Artifactory, Nexus,
GitHub Enterprise) are reachable.

This module is the single source of truth for the env-var contract that
redirects every external reach. Setup scripts and install scripts consult
helpers here rather than reading env vars directly, so the contract has
exactly one place to test, one place to log, and one place to evolve.

**Default behaviour with no env vars set is unchanged**: every helper falls
back to the original public URL, so non-enterprise deployments see zero
behavioural difference.

Env-var contract:

  ENTERPRISE_MODE                Master switch. When truthy, log a banner
                                 at startup and warn on missing mirrors.
                                 Behavioural overrides are driven by the
                                 individual vars below, not by this flag.

  HTTPS_PROXY / HTTP_PROXY       Corporate egress proxy. Honoured natively by
  NO_PROXY                       curl, uv, npm, git, requests — we just pass
                                 them through to subprocesses.

  REQUESTS_CA_BUNDLE             Corporate root CA bundle (PEM). Honoured by
  NODE_EXTRA_CA_CERTS            requests, node, openssl respectively.
  SSL_CERT_FILE

  UV_DEFAULT_INDEX               Internal PyPI proxy (e.g. JFrog pypi-virtual).
  UV_HTTP_TIMEOUT                Larger timeout for slow proxies.

  NPM_REGISTRY                   Internal npm registry URL. Written to
                                 ~/.npmrc as `registry=...`.
  NPM_TOKEN                      Bearer token for NPM_REGISTRY. Written as
                                 `//host/:_authToken=...`.

  GITHUB_API_BASE                Replacement for https://api.github.com.
  GITHUB_RELEASE_MIRROR          Replacement for https://github.com (release
                                 download paths). Convention: mirror keeps the
                                 same `/{owner}/{repo}/releases/download/...`
                                 tail, so it works against a JFrog generic-repo
                                 proxy with no path rewriting.

  CLAUDE_INSTALLER_URL           Override https://claude.ai/install.sh.
  HERMES_PIP_URL                 Override the upstream Hermes git URL for
                                 `uv tool install`.

  DEEPWIKI_MCP_URL               Override or set empty to omit the DeepWiki
  EXA_MCP_URL                    and Exa MCP servers (public endpoints).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# --- Constants ---------------------------------------------------------------

DEFAULT_GITHUB_API = "https://api.github.com"
DEFAULT_GITHUB_HOST = "https://github.com"
DEFAULT_CLAUDE_INSTALLER = "https://claude.ai/install.sh"
DEFAULT_HERMES_PIP_URL = "git+https://github.com/NousResearch/hermes-agent.git"
DEFAULT_DEEPWIKI_MCP = "https://mcp.deepwiki.com/mcp"
DEFAULT_EXA_MCP = "https://mcp.exa.ai/mcp"

# Env vars that surface in the startup banner (masked if they look secret-y).
_BANNER_VARS = (
    "ENTERPRISE_MODE",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "UV_DEFAULT_INDEX",
    "UV_HTTP_TIMEOUT",
    "NPM_REGISTRY",
    "NPM_TOKEN",
    "GITHUB_API_BASE",
    "GITHUB_RELEASE_MIRROR",
    "CLAUDE_INSTALLER_URL",
    "HERMES_PIP_URL",
    "DEEPWIKI_MCP_URL",
    "EXA_MCP_URL",
)

# Vars treated as secrets in the banner (full value masked).
_SECRET_VARS = frozenset({"NPM_TOKEN"})


# --- Truthy parsing ----------------------------------------------------------


def _truthy(value: str | None) -> bool:
    """Treat the standard set of "yes" strings as true; everything else false.

    Mirrors what setup_hermes.py does for ENABLE_HERMES so operators don't have
    to remember a different convention here.
    """
    if value is None:
        return False
    return value.strip().lower() in ("true", "1", "yes", "on")


def is_enabled() -> bool:
    """Return True when ENTERPRISE_MODE is set to a truthy value."""
    return _truthy(os.environ.get("ENTERPRISE_MODE"))


# --- Env-var pass-through helpers --------------------------------------------


def _passthrough(*names: str) -> dict[str, str]:
    """Pluck the named env vars from os.environ, dropping unset/blank ones."""
    out: dict[str, str] = {}
    for name in names:
        value = os.environ.get(name, "")
        if value:
            out[name] = value
    return out


def proxy_env() -> dict[str, str]:
    """Return proxy and CA-bundle env vars to forward to subprocesses.

    Includes both upper- and lower-case spellings because some tools only
    consult one (curl looks at lowercase; many Python libs look at uppercase).
    """
    upper = _passthrough(
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "SSL_CERT_FILE",
    )
    # Mirror to lowercase for curl/wget which only honour lowercase.
    mirrored = dict(upper)
    for upper_name, lower_name in (
        ("HTTPS_PROXY", "https_proxy"),
        ("HTTP_PROXY", "http_proxy"),
        ("NO_PROXY", "no_proxy"),
    ):
        if upper_name in upper and lower_name not in os.environ:
            mirrored[lower_name] = upper[upper_name]
    return mirrored


def uv_env() -> dict[str, str]:
    """Return uv-specific env vars (PyPI index, timeout, auth)."""
    out: dict[str, str] = {}
    index = os.environ.get("UV_DEFAULT_INDEX", "").strip()
    if index:
        out["UV_DEFAULT_INDEX"] = index
    timeout = os.environ.get("UV_HTTP_TIMEOUT", "").strip()
    if timeout:
        out["UV_HTTP_TIMEOUT"] = timeout
    # Forward any UV_INDEX_<name>_USERNAME / UV_INDEX_<name>_PASSWORD pairs
    # the operator has set — uv reads these natively.
    for key, value in os.environ.items():
        if key.startswith("UV_INDEX_") and (
            key.endswith("_USERNAME") or key.endswith("_PASSWORD")
        ):
            if value:
                out[key] = value
    return out


def npm_env() -> dict[str, str]:
    """Return npm config env vars derived from NPM_REGISTRY / NPM_TOKEN.

    `npm` reads `npm_config_<key>=value` style env vars natively. Setting
    `npm_config_registry` is equivalent to writing `registry=` in `.npmrc`,
    but the env-var form survives subprocess boundaries even if `.npmrc`
    hasn't been written yet.
    """
    out: dict[str, str] = {}
    registry = os.environ.get("NPM_REGISTRY", "").strip()
    token = os.environ.get("NPM_TOKEN", "").strip()
    if registry:
        out["npm_config_registry"] = registry
        if token:
            host = urlparse(registry).hostname
            if host:
                # npm's auth-token key format: //host/:_authToken=...
                out[f"npm_config_//{host}/:_authToken"] = token
    return out


def subprocess_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Build an env dict combining base + proxy + uv + npm settings.

    `base` defaults to a copy of `os.environ` so callers can pass it straight
    to `subprocess.run(env=...)`. Caller can also pass an empty dict to get a
    "just the enterprise additions" view, useful for logging or diffing.
    """
    merged: dict[str, str] = dict(base if base is not None else os.environ)
    merged.update(proxy_env())
    merged.update(uv_env())
    merged.update(npm_env())
    return merged


# --- Persistent npm config ---------------------------------------------------


def write_npmrc(home: Path | str) -> Path | None:
    """Write `~/.npmrc` with NPM_REGISTRY + NPM_TOKEN if set.

    Idempotent: re-running with the same env produces an identical file.
    No-op (returns None) if NPM_REGISTRY is unset, so non-enterprise
    deployments keep using the public npmjs.org default.

    Some npm operations (notably `npm view` via `utils.get_npm_version`) prefer
    `.npmrc` over env vars in edge cases, and a developer who shells into the
    container should see the same registry as the install scripts.
    """
    registry = os.environ.get("NPM_REGISTRY", "").strip()
    token = os.environ.get("NPM_TOKEN", "").strip()
    if not registry:
        return None

    home_path = Path(home)
    npmrc = home_path / ".npmrc"
    home_path.mkdir(parents=True, exist_ok=True)

    lines = [f"registry={registry}"]
    host = urlparse(registry).hostname
    if token and host:
        lines.append(f"//{host}/:_authToken={token}")
    lines.append("always-auth=true")
    content = "\n".join(lines) + "\n"

    if npmrc.exists() and npmrc.read_text() == content:
        return npmrc
    npmrc.write_text(content)
    try:
        npmrc.chmod(0o600)
    except OSError:
        # Best effort — chmod can fail on some workspace filesystems.
        pass
    return npmrc


# --- URL mirrors -------------------------------------------------------------


def _github_release_mirror() -> str:
    """Return the configured release mirror with trailing slash stripped."""
    return os.environ.get("GITHUB_RELEASE_MIRROR", "").strip().rstrip("/")


def mirror_github_release(url: str) -> str:
    """Rewrite a github.com release URL to the configured mirror.

    Mirror convention: same path tail as github.com. So
    `https://github.com/cli/cli/releases/download/v2.50.0/gh.tar.gz`
    becomes
    `{GITHUB_RELEASE_MIRROR}/cli/cli/releases/download/v2.50.0/gh.tar.gz`.

    Non-github URLs and unset mirror pass through unchanged.
    """
    if not url:
        return url
    mirror = _github_release_mirror()
    if not mirror:
        return url
    parsed = urlparse(url)
    if parsed.hostname not in ("github.com", "www.github.com"):
        return url
    path = parsed.path or "/"
    return f"{mirror}{path}"


def mirror_github_api(url: str) -> str:
    """Rewrite an api.github.com URL to GITHUB_API_BASE if set."""
    if not url:
        return url
    base = os.environ.get("GITHUB_API_BASE", "").strip().rstrip("/")
    if not base:
        return url
    parsed = urlparse(url)
    if parsed.hostname not in ("api.github.com",):
        return url
    return f"{base}{parsed.path}"


def claude_installer_url() -> str:
    """Return the URL for the Claude Code installer (overridable)."""
    return (
        os.environ.get("CLAUDE_INSTALLER_URL", "").strip() or DEFAULT_CLAUDE_INSTALLER
    )


def hermes_pip_url() -> str:
    """Return the package spec for `uv tool install hermes-agent` (overridable).

    Default is the upstream git URL. Enterprise deployments typically set this
    to a mirrored git URL or an internal-index package spec like
    `hermes-agent==1.2.3` once the package is mirrored in their PyPI proxy.
    """
    return os.environ.get("HERMES_PIP_URL", "").strip() or DEFAULT_HERMES_PIP_URL


def deepwiki_mcp_url() -> str | None:
    """Return the DeepWiki MCP URL, or None if explicitly disabled.

    Operators can set DEEPWIKI_MCP_URL to an empty string in app.yaml to
    drop DeepWiki from the configs entirely (its public endpoint is
    typically blocked in locked-down envs).
    """
    raw = os.environ.get("DEEPWIKI_MCP_URL")
    if raw is None:
        return DEFAULT_DEEPWIKI_MCP
    stripped = raw.strip()
    return stripped or None


def exa_mcp_url() -> str | None:
    """Return the Exa MCP URL, or None if explicitly disabled."""
    raw = os.environ.get("EXA_MCP_URL")
    if raw is None:
        return DEFAULT_EXA_MCP
    stripped = raw.strip()
    return stripped or None


# --- Banner / diagnostics ----------------------------------------------------


def _mask(name: str, value: str) -> str:
    """Mask secret-shaped values for logging.

    Tokens (NPM_TOKEN) are always fully masked. URLs that contain a userinfo
    component (https://user:pass@host) get the password redacted.
    """
    if name in _SECRET_VARS:
        return "***"
    # Redact URL passwords (https://user:pass@host -> https://user:***@host)
    return re.sub(r"(://[^:/@\s]+:)[^@\s]+(@)", r"\1***\2", value)


def startup_banner() -> str:
    """Return a multi-line, log-friendly summary of the active enterprise config.

    Secrets are masked. Lines are sorted for deterministic output (helpful in
    tests and diffs).
    """
    lines = ["enterprise_config: effective settings"]
    for name in _BANNER_VARS:
        value = os.environ.get(name, "")
        if value:
            lines.append(f"  {name}={_mask(name, value)}")
        else:
            lines.append(f"  {name}=<unset>")
    return "\n".join(lines)


def missing_when_enabled() -> list[str]:
    """Return the list of recommended env vars that are unset when enterprise mode is on.

    Used by `bootstrap()` to log warnings (not errors — the operator may
    intentionally leave some vars unset, e.g. if their internal mirror handles
    npm but not PyPI yet).
    """
    if not is_enabled():
        return []
    recommended = (
        "UV_DEFAULT_INDEX",
        "NPM_REGISTRY",
        "GITHUB_RELEASE_MIRROR",
    )
    return [name for name in recommended if not os.environ.get(name, "").strip()]


def bootstrap(home: Path | str | None = None) -> None:
    """Apply enterprise config side-effects: ~/.npmrc, banner, warnings.

    Call once at app startup, before any setup subprocess runs. Idempotent.

    Pushes derived env vars (npm_config_registry, etc.) into os.environ so
    every subsequent subprocess inherits them via the parent's environment —
    no need to thread an `env=` arg through every `subprocess.run` call.
    """
    home_path = (
        Path(home)
        if home is not None
        else Path(os.environ.get("HOME", str(Path.home())))
    )

    # 1. Persist npm registry config to disk.
    try:
        write_npmrc(home_path)
    except Exception as e:  # noqa: BLE001 — diagnostic logging only
        logger.warning("enterprise_config: write_npmrc failed: %s", e)

    # 2. Push derived env into os.environ so subprocesses inherit it.
    for key, value in npm_env().items():
        os.environ.setdefault(key, value)
    for key, value in uv_env().items():
        os.environ.setdefault(key, value)
    for key, value in proxy_env().items():
        # Lowercase pass-through vars may collide with case-sensitive shells;
        # setdefault keeps any explicit operator value intact.
        os.environ.setdefault(key, value)

    # 3. Log effective config (always — useful for debugging non-enterprise
    #    deployments too, since the banner shows everything is `<unset>`).
    for line in startup_banner().splitlines():
        logger.info(line)

    # 4. Warn about recommended-but-unset mirrors only when ENTERPRISE_MODE is on.
    missing = missing_when_enabled()
    if missing:
        logger.warning(
            "enterprise_config: ENTERPRISE_MODE=true but recommended mirrors "
            "are unset: %s",
            ", ".join(missing),
        )


# --- Reachability ------------------------------------------------------------


def doctor_targets() -> list[tuple[str, str]]:
    """Return (name, url) pairs that should be reachable in the current config.

    Used by scripts/enterprise_doctor.py. Returns only the targets that are
    actually configured — there's no point checking the GitHub release mirror
    if the operator hasn't set GITHUB_RELEASE_MIRROR.
    """
    targets: list[tuple[str, str]] = []
    if index := os.environ.get("UV_DEFAULT_INDEX", "").strip():
        targets.append(("UV_DEFAULT_INDEX", index))
    if registry := os.environ.get("NPM_REGISTRY", "").strip():
        targets.append(("NPM_REGISTRY", registry))
    if mirror := _github_release_mirror():
        targets.append(("GITHUB_RELEASE_MIRROR", mirror))
    if api := os.environ.get("GITHUB_API_BASE", "").strip().rstrip("/"):
        targets.append(("GITHUB_API_BASE", api))
    if installer := os.environ.get("CLAUDE_INSTALLER_URL", "").strip():
        targets.append(("CLAUDE_INSTALLER_URL", installer))
    return targets


def doctor(http_get: object | None = None) -> list[tuple[str, str, bool, str]]:
    """Probe each configured target. Returns (name, url, ok, detail) tuples.

    `http_get` is injected for testing — defaults to `requests.get`. Any
    response (even 401/404) counts as reachable; only connection errors and
    timeouts mean unreachable.
    """
    if http_get is None:
        import requests

        def http_get(url: str):  # type: ignore[no-redef]
            return requests.get(url, timeout=5, allow_redirects=False)

    results: list[tuple[str, str, bool, str]] = []
    for name, url in doctor_targets():
        try:
            resp = http_get(url)
            results.append((name, url, True, f"HTTP {resp.status_code}"))
        except Exception as e:  # noqa: BLE001 — surface every error verbatim
            results.append((name, url, False, str(e)[:200]))
    return results


# --- Convenience for shell scripts -------------------------------------------


def shell_export_lines(names: Iterable[str] | None = None) -> list[str]:
    """Render env-var values as `export FOO=bar` lines for shell scripts.

    Used by `scripts/enterprise_doctor.py` or by debug shells that want to
    replay the effective config without re-running bootstrap().
    """
    if names is None:
        names = _BANNER_VARS
    lines: list[str] = []
    for name in names:
        value = os.environ.get(name, "")
        if not value:
            continue
        # Single-quote the value, escaping any embedded single quotes.
        escaped = value.replace("'", "'\\''")
        lines.append(f"export {name}='{escaped}'")
    return lines
