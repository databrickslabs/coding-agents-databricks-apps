"""Shared utilities for Databricks App setup scripts."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import subprocess
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


def discover_serving_endpoints(host: str, token: str, timeout: float = 5.0) -> set[str]:
    """Return the set of READY serving-endpoint names at the workspace.

    The workspace's direct serving-endpoints list naturally reflects in-geo
    model availability — Databricks Geo Designated Services restricts which
    models are deployed to each region. Validating an env-set model against
    this list is therefore equivalent to "is this model in the workspace's
    geo / data-residency policy", without parsing GDS rules ourselves.

    Returns an empty set on any failure (auth error, network blip, JSON parse,
    etc.) — caller should treat empty as "discovery unavailable, keep defaults".
    """
    if not host or not token:
        return set()
    try:
        resp = requests.get(
            f"{host}/api/2.0/serving-endpoints",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        endpoints = resp.json().get("endpoints", [])
        return {
            ep["name"]
            for ep in endpoints
            if ep.get("name") and ep.get("state", {}).get("ready") == "READY"
        }
    except Exception as e:
        logger.warning("Could not discover serving endpoints at %s: %s", host, e)
        return set()


def pick_in_geo_model(preferred: list[str], available: set[str], fallback: str) -> str:
    """Pick the highest-priority preferred model that's actually served here.

    `preferred` is the caller's degradation chain (e.g. opus-4-7 → opus-4-6).
    Returns the first entry that's in `available`. If none match (or `available`
    is empty because discovery failed), returns `fallback` — typically the
    original env-set default. The user will see a clean ENDPOINT_NOT_FOUND
    later if they actually try to use a missing model, rather than getting
    silently downgraded to a different model tier.
    """
    for m in preferred:
        if m in available:
            return m
    return fallback


# Matches both the AI Gateway form (`databricks-claude-opus-4-8`) and the UC
# model-services form (`system.ai.claude-opus-4-8`), capturing family + version.
_CLAUDE_MODEL_RE = re.compile(
    r"^(?:system\.ai\.)?(?:databricks-)?claude-(opus|sonnet|haiku)-(\d+)-(\d+)(.*)$"
)


def add_1m_context_suffix(model: str) -> str:
    """Append Claude Code's ``[1m]`` suffix for gateway 1M-context routing.

    Claude Code reads ``ANTHROPIC_DEFAULT_OPUS_MODEL`` / ``_SONNET_MODEL`` and,
    when the id ends in ``[1m]``, requests the 1M context window. On the raw
    Anthropic API that becomes the ``context-1m-2025-08-07`` beta header — which
    ``CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`` (set alongside this) would strip.
    But we route through the Databricks AI Gateway, which parses ``[1m]`` off the
    model-id string server-side, so the suffix and the disable-betas flag coexist
    (the flag is required because the gateway 400s on unknown ``anthropic-beta``
    headers). This mirrors Databricks' own ``ucode`` wrapper.

    Only opus/sonnet >= 4.6 get the suffix — Haiku 4.5 is 200K-native, and
    suffixing it would produce an unroutable endpoint id. Idempotent: an id that
    already ends in ``[1m]`` (or that doesn't parse as a Claude model) is
    returned unchanged.
    """
    if model.endswith("[1m]"):
        return model
    match = _CLAUDE_MODEL_RE.match(model)
    if not match:
        return model
    family, major_raw, minor_raw, _ = match.groups()
    version = (int(major_raw), int(minor_raw))
    should_suffix = family in ("opus", "sonnet") and version >= (4, 6)
    return f"{model}[1m]" if should_suffix else model


def _default_npm_min_age_days() -> int:
    """Read NPM_MIN_RELEASE_AGE_DAYS env var, default 7. Falls back to 7 on parse error."""
    try:
        return int(os.environ.get("NPM_MIN_RELEASE_AGE_DAYS", "7"))
    except ValueError:
        return 7


def get_npm_version(package_name, min_age_days=None):
    """Resolve the latest stable npm version that satisfies a release-age cooldown.

    Returns the highest stable (non-pre-release) version of ``package_name``
    that was published at least ``min_age_days`` days ago. This is a
    supply-chain hardening measure: malicious npm packages are typically
    detected and yanked within hours-to-days of publishing (see Shai-Hulud,
    Nx, event-stream incidents), so an N-day cooldown gives the community
    time to flag bad versions before we install them.

    Mirrors the role of ``[tool.uv] exclude-newer = "7 days"`` in
    ``pyproject.toml``. See https://github.com/lirantal/npm-security-best-practices
    section 3 for background.

    Args:
        package_name: npm package name (e.g. "opencode-ai" or "@openai/codex").
        min_age_days: Minimum publish age in days. Defaults to
            ``NPM_MIN_RELEASE_AGE_DAYS`` env var or 7 days. Pass 0 to disable
            the cooldown (single-query fast path, original behaviour).

    Returns:
        Exact version string (e.g. "1.2.24") suitable for pinning via
        ``npm install -g <pkg>@<version>``. Returns None on lookup failure
        (network, package not found, no version old enough) — callers
        already fall back to "@latest" in that case.
    """
    if min_age_days is None:
        min_age_days = _default_npm_min_age_days()
    if min_age_days <= 0:
        return _npm_view_latest(package_name)
    return _npm_view_with_cooldown(package_name, min_age_days)


def _npm_view_latest(package_name):
    """Single-query fast path: return whatever version 'latest' points at."""
    try:
        result = subprocess.run(
            ["npm", "view", package_name, "version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _npm_view_with_cooldown(package_name, min_age_days):
    """Pick the highest stable version published >= min_age_days ago.

    Walks ``npm view <pkg> versions time --json`` from newest to oldest,
    skipping pre-release tags and any version whose publish time is too
    recent. Returns the first match (which is the highest stable version
    satisfying the cooldown). Returns None if no version qualifies or any
    step fails — caller falls back to ``@latest``.
    """
    try:
        result = subprocess.run(
            ["npm", "view", package_name, "versions", "time", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        data = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return None

    versions = data.get("versions") or []
    times = data.get("time") or {}
    if not versions or not times:
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(days=min_age_days)
    # `versions` is in publish order (oldest -> newest); iterate newest first
    # so we return the highest-numbered version that satisfies the cooldown.
    for ver in reversed(versions):
        # Skip pre-releases (alpha/beta/rc/next) — `1.2.3-rc.1` always
        # contains a hyphen per semver. Matches the "latest stable" intent.
        if "-" in ver:
            continue
        ts = times.get(ver)
        if not ts:
            continue
        try:
            pub = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if pub <= cutoff:
            return ver
    return None


def adapt_instructions_file(
    source_path: Path,
    target_path: Path,
    new_header: str,
    cli_name: str,
) -> bool:
    """Read a CLAUDE.md file and adapt it for another CLI's instructions format.
    
    Reads the source instructions file (typically CLAUDE.md), replaces the first
    header line with a CLI-specific header, and writes to the target location.
    
    Args:
        source_path: Path to the source instructions file (e.g., CLAUDE.md)
        target_path: Path to write the adapted instructions file
        new_header: The new header line (e.g., "# Codex Agent Instructions")
        cli_name: Name of the CLI for logging (e.g., "Codex", "Gemini")
        
    Returns:
        True if successful, False if source file not found
    """
    if not source_path.exists():
        print(f"Warning: {source_path} not found, skipping {cli_name} instructions")
        return False
    
    content = source_path.read_text()
    
    # Replace the first markdown header (# ...) with the new header
    # This handles "# Claude Code on Databricks" -> "# Codex Agent Instructions"
    adapted_content = re.sub(r"^#\s+.*$", new_header, content, count=1, flags=re.MULTILINE)
    
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(adapted_content)
    print(f"{cli_name} instructions configured: {target_path}")
    return True


def _probe_gateway(url: str, timeout: float = 2.0) -> bool:
    """Quick connectivity check against an AI Gateway host.

    Sends a lightweight GET to the root. Any HTTP response (even 401/404)
    means the host exists. Only a connection failure means it doesn't.
    Timeout is 2s — the gateway is same-region, so it responds fast if it exists.
    """
    import requests

    try:
        requests.get(url, timeout=timeout, allow_redirects=False)
        return True
    except (requests.ConnectionError, requests.Timeout):
        return False
    except Exception:
        return False


def _derive_workspace_id_from_host(host: str) -> str:
    """Extract the workspace ID from a Databricks host URL.

    Azure host pattern is `adb-{workspace_id}.{region}.azuredatabricks.net`,
    so the digits between `adb-` and the first dot are the workspace ID. AWS
    hosts don't carry the workspace ID in the URL, so this returns "" there.
    """
    m = re.match(r"(?:https?://)?adb-(\d+)\.", host or "")
    return m.group(1) if m else ""


def _build_gateway_candidate(workspace_id: str, host: str) -> str:
    """Build the AI Gateway URL for a workspace, picking the right cloud pattern.

    Azure: `https://{ws}.0.ai-gateway.azuredatabricks.net`
    AWS:   `https://{ws}.ai-gateway.cloud.databricks.com`
    """
    if "azuredatabricks.net" in (host or "").lower():
        return f"https://{workspace_id}.0.ai-gateway.azuredatabricks.net"
    return f"https://{workspace_id}.ai-gateway.cloud.databricks.com"


def get_gateway_host() -> str:
    """Resolve the AI Gateway host URL.

    Priority:
      0. _GATEWAY_RESOLVED env var (set by parent process after probing — avoids
         re-probing in subprocesses). None = never probed, "" = probed, no gateway.
      1. Explicit DATABRICKS_GATEWAY_HOST env var (trusted — no probe)
      2. Auto-constructed from workspace ID. Workspace ID is read from
         DATABRICKS_WORKSPACE_ID, or derived from DATABRICKS_HOST on Azure
         (host pattern `adb-{ws}.{region}.azuredatabricks.net`). Cloud-specific
         URL pattern is picked based on whether the host is Azure or AWS.
         Result is probed for reachability before returning.
      3. Empty string (caller falls back to DATABRICKS_HOST/serving-endpoints)
    """
    # Tier 0: already resolved by a parent process
    resolved = os.environ.get("_GATEWAY_RESOLVED")
    if resolved is not None:
        return resolved

    # Tier 1: explicit override. Presence of the env var is authoritative:
    #   - set to a URL  -> trust it, no probe.
    #   - set but empty  -> operator is explicitly disabling the gateway; return
    #     "" so callers use serving-endpoints. Do NOT fall through to tier 2,
    #     which would otherwise DERIVE a gateway URL from an Azure host
    #     (adb-{id}.…azuredatabricks.net) and probe it — reintroducing a gateway
    #     the operator meant to turn off.
    #   - unset entirely -> fall through to tier 2 auto-derivation.
    if "DATABRICKS_GATEWAY_HOST" in os.environ:
        explicit = os.environ["DATABRICKS_GATEWAY_HOST"].strip().rstrip("/")
        return ensure_https(explicit) if explicit else ""

    # Tier 2: auto-construct from workspace ID and probe for reachability
    host = os.environ.get("DATABRICKS_HOST", "")
    workspace_id = (
        os.environ.get("DATABRICKS_WORKSPACE_ID", "").strip()
        or _derive_workspace_id_from_host(host)
    )
    if workspace_id:
        candidate = _build_gateway_candidate(workspace_id, host)
        if _probe_gateway(candidate):
            return candidate
        print(
            f"AI Gateway not reachable at {candidate}, "
            "falling back to serving-endpoints"
        )

    return ""


def resolve_and_cache_gateway() -> str:
    """Probe the gateway once and cache the result in the environment.

    Subsequent calls to get_gateway_host() — including those in child
    processes — will see _GATEWAY_RESOLVED and skip the probe.
    """
    result = get_gateway_host()
    os.environ["_GATEWAY_RESOLVED"] = result
    return result


def ensure_https(url: str) -> str:
    """Ensure a URL has the https:// prefix.
    
    Databricks Apps may inject DATABRICKS_HOST without the protocol prefix,
    which causes URL parsing errors downstream.
    
    Args:
        url: A URL that may or may not have a protocol prefix
        
    Returns:
        The URL with https:// prefix (or unchanged if already has http(s)://)
    """
    if not url:
        return url
    if not url.startswith(("http://", "https://")):
        return f"https://{url}"
    return url


# --- Databricks unified-auth env shaping -------------------------------------
#
# The Databricks SDK/CLI "unified auth" resolver errors ("more than one
# authorization method configured") when it sees more than one credential
# source at once, and it silently prefers ambient env vars over
# ~/.databrickscfg. Several CoDA subprocess call sites therefore have to shape
# the child's environment to expose exactly ONE credential source. These
# helpers centralize that shaping so the (previously duplicated, slightly
# divergent) var lists stay in sync.

# App/M2M OAuth credentials injected by the Databricks Apps runtime.
_OAUTH_ENV_VARS = ("DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET")
# Everything that steers unified-auth away from an explicit config profile.
# Includes the DATABRICKS_APP_* / WORKSPACE_ID vars Apps injects, which drive
# auth to the app's ambient identity and cause a 302 -> OIDC loop.
_PROFILE_SHADOWING_ENV_VARS = (
    "DATABRICKS_TOKEN",
    "DATABRICKS_HOST",
    "DATABRICKS_WORKSPACE_ID",
    "DATABRICKS_APP_NAME",
    "DATABRICKS_APP_URL",
    "DATABRICKS_CLIENT_ID",
    "DATABRICKS_CLIENT_SECRET",
)


def pat_only_env(base_env: dict | None = None) -> dict:
    """Return an env that forces PAT auth by neutralizing OAuth creds.

    Blanks ``DATABRICKS_CLIENT_ID`` / ``DATABRICKS_CLIENT_SECRET`` (set to
    ``""`` rather than popped, so the unified-auth resolver treats them as
    explicitly absent) while leaving ``DATABRICKS_HOST`` / ``DATABRICKS_TOKEN``
    in place. Use when a PAT is present in the environment and OAuth vars would
    otherwise collide with it.
    """
    env = dict(base_env if base_env is not None else os.environ)
    for key in _OAUTH_ENV_VARS:
        env[key] = ""
    return env


def databrickscfg_only_env(base_env: dict | None = None) -> dict:
    """Return an env with all ambient Databricks creds stripped.

    Pops ``DATABRICKS_CLIENT_ID`` / ``DATABRICKS_CLIENT_SECRET`` /
    ``DATABRICKS_HOST`` / ``DATABRICKS_TOKEN`` so the CLI/SDK falls through to
    the ``[DEFAULT]`` profile in ``~/.databrickscfg``. Use for tools (e.g.
    ``databricks sync``) that should authenticate from the config file.
    """
    env = dict(base_env if base_env is not None else os.environ)
    for key in (*_OAUTH_ENV_VARS, "DATABRICKS_HOST", "DATABRICKS_TOKEN"):
        env.pop(key, None)
    return env


def config_profile_env(profile: str, base_env: dict | None = None) -> dict:
    """Return an env that pins unified-auth to a named ``~/.databrickscfg`` profile.

    Sets ``DATABRICKS_CONFIG_PROFILE`` and strips every ambient var that would
    shadow the profile in the SDK's resolution (see
    ``_PROFILE_SHADOWING_ENV_VARS``). Use when a tool must authenticate as a
    specific profile (e.g. the Omnigent host's ``omnigents-host`` M2M profile).
    """
    env = dict(base_env if base_env is not None else os.environ)
    env["DATABRICKS_CONFIG_PROFILE"] = profile
    for key in _PROFILE_SHADOWING_ENV_VARS:
        env.pop(key, None)
    return env


@contextmanager
def databrickscfg_update_lock(path: str | Path):
    """Serialize read-modify-write ownership of co-managed profile sections."""
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(f"{config_path}.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read_non_default_databrickscfg_sections(path: str | Path) -> str:
    """Return every ``~/.databrickscfg`` section except ``[DEFAULT]``.

    The PAT rotator owns ``[DEFAULT]`` and rewrites it on every rotation; the
    Omnigent host appends an ``[omnigents-host]`` OAuth (M2M) profile that its
    runners re-read from this file. Any DEFAULT-only rewrite must preserve those
    co-owned sections, or a fresh runner (or CLI call) after the rewrite can't
    authenticate. Both the rotator (pat_rotator.py) and the boot-time writer
    (setup_databricks.py) call this so they honor the same contract.

    Returns ``""`` when the file is absent or has no non-DEFAULT sections;
    otherwise the preserved text wrapped in leading/trailing newlines so it can
    be concatenated directly after a freshly built ``[DEFAULT]`` block.
    """
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return ""
    out: list[str] = []
    in_default = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_default = stripped == "[DEFAULT]"
        if not in_default:
            out.append(line)
    text = "".join(out).strip()
    return f"\n{text}\n" if text else ""


def resolve_mlflow_experiment_id(host: str, token: str, experiment_name: str) -> str | None:
    """Look up (or create) a Databricks MLflow experiment by name and return its ID.

    Used by Codex and Gemini CLI tracing setup — both need an experiment *ID*,
    not name, in their config files / OTLP headers.

    Returns None on any failure so callers can degrade gracefully.
    """
    if not host or not token or not experiment_name:
        return None
    try:
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.errors import ResourceDoesNotExist

        w = WorkspaceClient(host=ensure_https(host.rstrip("/")), token=token)
        try:
            exp = w.experiments.get_by_name(experiment_name=experiment_name)
            if exp and exp.experiment:
                return exp.experiment.experiment_id
        except ResourceDoesNotExist:
            pass  # fall through to create
        return w.experiments.create_experiment(name=experiment_name).experiment_id
    except Exception as exc:
        logger.warning(f"Could not resolve MLflow experiment '{experiment_name}': {exc}")
        return None


SYNC_FALLBACK_PROFILE = "omnigents-host"


def workspace_sync_auth():
    """Resolve auth for the workspace sync/restore round-trip.

    Returns ``(env, client)``: the environment the ``databricks`` CLI should run
    with, and an authenticated ``WorkspaceClient`` (used to validate the creds
    and init telemetry before a long CLI call).

    Two layers, matching the app's own auth layering:

    1. ``[DEFAULT]`` PAT in ``~/.databrickscfg`` — the pasted/rotated PAT path.
       Pinned explicitly to ``DEFAULT``: an ambient
       ``DATABRICKS_CONFIG_PROFILE`` (Apps sets it to the SP profile) would
       otherwise silently steer the CLI at the wrong identity.
    2. Otherwise a named profile (``DATABRICKS_CONFIG_PROFILE``, default
       ``omnigents-host``) — the SP-broker path, where the on-disk profile holds
       only a host + ``auth_type = databricks-cli``. With
       ``ENABLE_SP_APIKEYHELPER=true`` no PAT is ever written, so this is the
       normal case; requiring a PAT here silently disabled every backup.

    Raises if neither layer can authenticate — callers must treat that as "this
    commit is NOT backed up".
    """
    import configparser

    from databricks.sdk import WorkspaceClient

    def _init_telemetry(client):
        try:
            from telemetry import set_product_info

            set_product_info(client)
        except Exception:
            pass  # Telemetry must never break sync/restore
        return client

    cfg_path = Path.home() / ".databrickscfg"
    host = token = None
    if cfg_path.exists():
        parser = configparser.ConfigParser()
        parser.read(cfg_path)
        host = parser.get("DEFAULT", "host", fallback=None)
        token = parser.get("DEFAULT", "token", fallback=None)

    if host and token:
        client = WorkspaceClient(host=host, token=token, auth_type="pat")
        client.current_user.me()  # fail fast on an expired PAT
        return config_profile_env("DEFAULT"), _init_telemetry(client)

    profile = os.environ.get("DATABRICKS_CONFIG_PROFILE") or SYNC_FALLBACK_PROFILE
    env = config_profile_env(profile)
    client = WorkspaceClient(profile=profile)
    client.current_user.me()
    return env, _init_telemetry(client)


def workspace_sync_dest(repo_name: str) -> str:
    """Databricks Workspace path a repo syncs to / restores from.

    Single source of truth shared by sync_to_workspace.py (write) and
    restore_from_workspace.py (read) so the two can never drift.

    Disambiguated by the CoDA instance name: on a shared app every session
    resolves the SAME identity from the shared ~/.databrickscfg, so a
    per-identity path collided whenever two instances (or two writers) synced a
    same-named repo. Keying on the instance name keeps each CoDA's sync-back
    isolated. Base is /Workspace/Shared — its default ACL grants the `users`
    group CAN_MANAGE, so the app SP can write it with no per-workspace grant
    (unlike /Users/{x}, writable only by x).
    """
    app_name = (
        os.environ.get("CODA_INSTANCE_NAME")
        or os.environ.get("DATABRICKS_APP_NAME")
        or "_local"
    )
    return f"/Workspace/Shared/coda/{app_name}/{repo_name}"


# ---------------------------------------------------------------------------
# OpenCode credential schema
# ---------------------------------------------------------------------------
#
# opencode stores credentials at ~/.local/share/opencode/auth.json as a map of
# provider-id -> credential, where the credential is a discriminated union on
# `type`. The API-key variant keeps the secret in `key`:
#
#     export class Api extends Schema.Class<Api>("ApiAuth")({
#         type: Schema.Literal("api"),
#         key: Schema.String,
#         metadata: Schema.optional(Schema.Record(Schema.String, Schema.String)),
#     }) {}
#
#     const _Info = Schema.Union([Oauth, Api, WellKnown])
#         .annotate({ discriminator: "type", identifier: "Auth" })
#
# (opencode, packages/opencode/src/auth/index.ts)
#
# Two places touch this file and MUST agree, or token rotation silently stops
# working: setup_opencode.py writes it, and cli_auth._update_opencode() rewrites
# the secret on every PAT rotation. They previously both used `api_key` — not a
# field opencode recognises — so the credential was unloadable and each rotation
# faithfully updated a key nothing read. Defining the shape once here is what
# stops that drifting again.

OPENCODE_AUTH_TYPE_API = "api"
OPENCODE_AUTH_KEY_FIELD = "key"


def opencode_api_credential(key: str) -> dict:
    """Build an opencode API-key credential in its tagged-union shape."""
    return {"type": OPENCODE_AUTH_TYPE_API, OPENCODE_AUTH_KEY_FIELD: key}


def is_opencode_api_credential(cred) -> bool:
    """True if `cred` is an opencode API-key credential this code may rotate.

    Deliberately narrow: `oauth` and `wellknown` credentials carry different
    fields, and writing a PAT into one would corrupt a credential opencode
    still needs.
    """
    return (
        isinstance(cred, dict)
        and cred.get("type") == OPENCODE_AUTH_TYPE_API
        and OPENCODE_AUTH_KEY_FIELD in cred
    )
