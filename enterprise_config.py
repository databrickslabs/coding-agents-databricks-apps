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
# Pinned commit SHA for the default Hermes install — chosen to be at least
# 7 days old at the time of selection, matching the cooldown semantics applied
# to npm packages. This blocks the "force-push to default branch poisons every
# CoDA container" attack: even if NousResearch's gh account is compromised, an
# attacker would have to wait through the cooldown window before code lands
# in CoDA. Bump this SHA deliberately during CoDA releases; do not auto-update.
DEFAULT_HERMES_PIN_SHA = "8e4f3ba4da5337e1ad674a876ac4fb8490f0b79c"  # 2026-05-08
DEFAULT_HERMES_PIP_URL = (
    "hermes-agent @ git+https://github.com/NousResearch/hermes-agent.git"
    f"@{DEFAULT_HERMES_PIN_SHA}"
)
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

# Regex for env-var NAMES whose values should be fully masked in any log
# output. Used in addition to _SECRET_VARS — covers operator-named indexes
# like UV_INDEX_INTERNAL_PASSWORD without enumerating each one.
_SECRET_VAR_PATTERN = re.compile(
    r"^UV_INDEX_.+_(PASSWORD|USERNAME)$"
    r"|^npm_config_//.+/:_authToken$"
)


def _is_secret_var(name: str) -> bool:
    """True if a var name should have its value masked in banner output."""
    return name in _SECRET_VARS or bool(_SECRET_VAR_PATTERN.match(name))


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
    Also mirrors REQUESTS_CA_BUNDLE to CURL_CA_BUNDLE and SSL_CERT_FILE so
    the install_*.sh scripts pick up the corporate root CA without needing
    explicit --cacert flags.
    """
    upper = _passthrough(
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "NO_PROXY",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "SSL_CERT_FILE",
        "CURL_CA_BUNDLE",
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
    # Mirror REQUESTS_CA_BUNDLE to curl/openssl flavours so shell scripts
    # don't need explicit --cacert handling.
    if ca := upper.get("REQUESTS_CA_BUNDLE"):
        mirrored.setdefault("CURL_CA_BUNDLE", ca)
        mirrored.setdefault("SSL_CERT_FILE", ca)
    # Auto-include the Databricks workspace host in NO_PROXY when HTTPS_PROXY
    # is set, so PAT-bearing API calls (token rotation, sync, jobs) don't
    # route through the corporate proxy where a network operator can MITM
    # them (F-07). If the operator has already added the host to NO_PROXY,
    # this is a no-op.
    if "HTTPS_PROXY" in upper:
        databricks_host = _databricks_host_for_no_proxy()
        if databricks_host:
            existing_no_proxy = mirrored.get("NO_PROXY", "")
            if not _host_in_no_proxy(databricks_host, existing_no_proxy):
                new_no_proxy = (
                    f"{existing_no_proxy},{databricks_host}"
                    if existing_no_proxy
                    else databricks_host
                )
                mirrored["NO_PROXY"] = new_no_proxy
                mirrored["no_proxy"] = new_no_proxy
    return mirrored


def _databricks_host_for_no_proxy() -> str:
    """Extract the workspace hostname from DATABRICKS_HOST.

    Returns "" if DATABRICKS_HOST is unset. NO_PROXY expects bare hostnames
    (no scheme, no path), so strip those.
    """
    host = os.environ.get("DATABRICKS_HOST", "").strip()
    if not host:
        return ""
    parsed = urlparse(host if "://" in host else f"https://{host}")
    return parsed.hostname or ""


def _host_in_no_proxy(host: str, no_proxy: str) -> bool:
    """Check whether `host` is already covered by `no_proxy`.

    Conservative: matches exact hostname or wildcard `.suffix` entries.
    Doesn't try to handle every edge case curl/python differ on — false
    negatives just mean we add the host (a duplicate is harmless).
    """
    if not no_proxy:
        return False
    for entry in no_proxy.split(","):
        entry = entry.strip().lstrip(".")
        if not entry:
            continue
        if host == entry or host.endswith(f".{entry}"):
            return True
    return False


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


def _effective_npm_registry() -> str:
    """Resolve the single effective npm registry from operator-set env vars.

    If the operator sets both `NPM_REGISTRY` (our contract) AND
    `npm_config_registry` (npm's native env var), they could point at
    different URLs — `~/.npmrc` would be written from one while subprocesses
    read the other. We pick the explicit `npm_config_registry` if set (it's
    npm-native and lower-level), otherwise fall through to `NPM_REGISTRY`.
    Both `write_npmrc()` and `npm_env()` read from this resolver so they
    always agree (F-10).
    """
    return (
        os.environ.get("npm_config_registry", "").strip()
        or os.environ.get("NPM_REGISTRY", "").strip()
    )


def npm_env() -> dict[str, str]:
    """Return npm config env vars derived from NPM_REGISTRY / NPM_TOKEN.

    `npm` reads `npm_config_<key>=value` style env vars natively. Setting
    `npm_config_registry` is equivalent to writing `registry=` in `.npmrc`,
    but the env-var form survives subprocess boundaries even if `.npmrc`
    hasn't been written yet.

    Honours `_effective_npm_registry()` so a direct `npm_config_registry`
    env var the operator may have set wins consistently — same value flows
    into `~/.npmrc` and into the env npm reads.
    """
    out: dict[str, str] = {}
    registry = _effective_npm_registry()
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
    # Resolve via the same path npm_env() uses so .npmrc and the env never
    # diverge if the operator set both NPM_REGISTRY and npm_config_registry.
    registry = _effective_npm_registry()
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


class UnsafeUrlError(ValueError):
    """Raised when an operator-supplied URL contains characters that would
    enable shell injection if interpolated into a shell-quoted string.

    The enterprise URLs (CLAUDE_INSTALLER_URL, GITHUB_API_BASE,
    GITHUB_RELEASE_MIRROR, HERMES_PIP_URL) flow through curl invocations and
    one `eval` site in install_micro.sh. Even with positional-arg subprocess
    forms, defense-in-depth says these values should never contain control
    characters or quote characters that could compromise downstream shell
    handlers. This validator is the choke point.
    """


# Conservative URL allow-list: alphanumerics + dot, hyphen, underscore,
# slash, tilde, percent (pct-encoding), question mark, colon (port), `#`,
# brackets (IPv6 literal), `@` (userinfo), `=` (query), `&` is intentionally
# EXCLUDED even though it's a valid URL char — it's a shell command
# separator in unquoted contexts. Same for `$`, `(`, `)`, `;`, single quote,
# backtick, whitespace, and `*`. Real package mirror URLs don't need any of
# these characters; if an operator's URL does require them, they should
# percent-encode (e.g. ``%3B`` for `;`).
_SAFE_URL_RE = re.compile(r"^https?://[A-Za-z0-9._\-/~:?#\[\]@=%]+$")


def _validate_url(name: str, url: str) -> str:
    """Ensure an operator-supplied URL is safe for shell interpolation.

    Returns the URL unchanged on success. Raises UnsafeUrlError on rejection.
    Defense-in-depth: setup scripts also use positional-arg subprocess forms.
    """
    if not _SAFE_URL_RE.match(url):
        raise UnsafeUrlError(
            f"{name} contains characters that aren't safe for shell "
            f"interpolation. URLs must match {_SAFE_URL_RE.pattern!r}. "
            f"Got: {url!r}"
        )
    return url


def _github_release_mirror() -> str:
    """Return the configured release mirror with trailing slash stripped."""
    return os.environ.get("GITHUB_RELEASE_MIRROR", "").strip().rstrip("/")


def _github_api_base() -> str:
    """Return the configured GitHub API base with trailing slash stripped."""
    return os.environ.get("GITHUB_API_BASE", "").strip().rstrip("/")


def validate_mirror_env() -> None:
    """Validate operator-supplied URL env vars before they reach subprocesses.

    Called from bootstrap() — raises UnsafeUrlError if any of the URL env vars
    contain shell metacharacters. Without this, a single quote in
    GITHUB_API_BASE would be interpolated into install_micro.sh's `eval` and
    execute attacker-supplied shell. Defense-in-depth alongside the install
    scripts' own usage.

    Only validates values that are actually set — defaults (unset) are
    inherently safe.
    """
    if mirror := _github_release_mirror():
        _validate_url("GITHUB_RELEASE_MIRROR", mirror)
    if base := _github_api_base():
        _validate_url("GITHUB_API_BASE", base)
    if installer := os.environ.get("CLAUDE_INSTALLER_URL", "").strip():
        _validate_url("CLAUDE_INSTALLER_URL", installer)
    # HERMES_PIP_URL has its own dedicated validator inside hermes_pip_url();
    # we trigger it here so misconfig surfaces at bootstrap rather than at
    # install time. Errors propagate.
    hermes_pip_url()


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
    """Return the URL for the Claude Code installer (overridable).

    Validated with `_validate_url` because the value is passed to curl in a
    subprocess invocation — any shell metacharacter could enable injection
    if a future caller embeds it in a shell string. We reject unsafe values
    rather than try to escape them.
    """
    url = os.environ.get("CLAUDE_INSTALLER_URL", "").strip() or DEFAULT_CLAUDE_INSTALLER
    return _validate_url("CLAUDE_INSTALLER_URL", url)


# Package spec for `uv tool install hermes-agent` must accept three forms:
#   1. PyPI-style spec:        hermes-agent==1.2.3
#   2. Direct URL spec:        hermes-agent @ git+https://host/repo.git@<sha>
#   3. Bare URL spec:          git+https://host/repo.git@<sha>
# All three are parsed by uv. We validate the structure conservatively: no
# shell metacharacters, no shell-special whitespace. The URL portion uses
# the same restrictive char set as _SAFE_URL_RE.
_HERMES_SPEC_RE = re.compile(
    r"^[A-Za-z0-9._\-=<>!~]+(?:\s*@\s*git\+https?://[A-Za-z0-9._\-/~:?#\[\]@=%]+)?$"
    r"|^git\+https?://[A-Za-z0-9._\-/~:?#\[\]@=%]+$"
)


def hermes_pip_url() -> str:
    """Return the package spec for `uv tool install hermes-agent` (overridable).

    Default is the upstream git URL pinned to a specific commit SHA to mitigate
    the "force-push to default branch poisons every CoDA container" attack.
    Enterprise deployments typically override this to a mirrored git URL or
    an internal-index package spec like `hermes-agent==1.2.3` once the package
    is mirrored in their PyPI proxy.

    The returned spec is validated as a uv-installable form with no shell
    metacharacters (defense in depth — the value flows through
    `uv tool install` which is positional-arg-safe, but we reject ambiguity).
    """
    spec = os.environ.get("HERMES_PIP_URL", "").strip() or DEFAULT_HERMES_PIP_URL
    if not _HERMES_SPEC_RE.match(spec):
        raise UnsafeUrlError(
            f"HERMES_PIP_URL must be a uv-installable spec (e.g. "
            f"'hermes-agent==1.2.3' or 'hermes-agent @ git+https://host/repo.git@<sha>'). "
            f"Got: {spec!r}"
        )
    return spec


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

    NPM_TOKEN and any UV_INDEX_*_PASSWORD / UV_INDEX_*_USERNAME /
    npm_config_//host/:_authToken are always fully masked. URLs that contain
    a userinfo component (https://user:pass@host) get the password redacted.
    """
    if _is_secret_var(name):
        return "***"
    # Redact URL passwords (https://user:pass@host -> https://user:***@host)
    return re.sub(r"(://[^:/@\s]+:)[^@\s]+(@)", r"\1***\2", value)


def startup_banner() -> str:
    """Return a multi-line, log-friendly summary of the active enterprise config.

    Secrets are masked. Lines are sorted for deterministic output (helpful in
    tests and diffs).

    Includes the documented banner vars (_BANNER_VARS) plus any operator-set
    secret-shaped vars (UV_INDEX_*_PASSWORD/USERNAME, npm_config auth tokens)
    so misconfigured credential variables are at least visible — with their
    values masked.
    """
    lines = ["enterprise_config: effective settings"]
    for name in _BANNER_VARS:
        value = os.environ.get(name, "")
        if value:
            lines.append(f"  {name}={_mask(name, value)}")
        else:
            lines.append(f"  {name}=<unset>")
    # Surface any UV_INDEX_*_PASSWORD/USERNAME and npm_config auth tokens
    # the operator has set — these aren't in _BANNER_VARS because the names
    # are operator-defined, but they're secret-shaped and worth surfacing.
    secret_extras = sorted(
        k for k in os.environ
        if _SECRET_VAR_PATTERN.match(k)
    )
    for name in secret_extras:
        lines.append(f"  {name}={_mask(name, os.environ[name])}")
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

    # 0. Validate operator-supplied URL env vars before they reach any
    #    subprocess. install_micro.sh uses `eval` on these values, so a
    #    shell metacharacter would be exploitable. We fail loud rather than
    #    try to sanitise — operators should provide clean URLs.
    validate_mirror_env()

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
    actually configured AND have an http(s) scheme. The scheme allow-list
    prevents the doctor from being used as an SSRF probe (file://, gopher://,
    cloud metadata via http://169.254.169.254 are explicitly *allowed*
    through http because that's a valid mirror configuration, but the
    allow-list excludes file:// and other unusual schemes that an attacker
    might use to escalate a misconfigured doctor run).
    """
    candidates: list[tuple[str, str]] = []
    if index := os.environ.get("UV_DEFAULT_INDEX", "").strip():
        candidates.append(("UV_DEFAULT_INDEX", index))
    if registry := os.environ.get("NPM_REGISTRY", "").strip():
        candidates.append(("NPM_REGISTRY", registry))
    if mirror := _github_release_mirror():
        candidates.append(("GITHUB_RELEASE_MIRROR", mirror))
    if api := _github_api_base():
        candidates.append(("GITHUB_API_BASE", api))
    if installer := os.environ.get("CLAUDE_INSTALLER_URL", "").strip():
        candidates.append(("CLAUDE_INSTALLER_URL", installer))
    # Filter to http(s) only — other schemes (file://, ftp://, etc.) would
    # turn the doctor into an unintended probing tool.
    return [
        (name, url) for name, url in candidates
        if urlparse(url).scheme in ("http", "https")
    ]


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
