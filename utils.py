"""Shared utilities for Databricks App setup scripts."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
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

    # Tier 1: explicit override (trusted, no probe)
    explicit = os.environ.get("DATABRICKS_GATEWAY_HOST", "").strip().rstrip("/")
    if explicit:
        return ensure_https(explicit)

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
