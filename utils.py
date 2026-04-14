"""Shared utilities for Databricks App setup scripts."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


def get_npm_version(package_name):
    """Resolve the latest stable version of an npm package.

    Uses ``npm view`` to query the registry, returning an exact version string
    (e.g. "1.2.24") that can be appended to the package spec for pinned installs.
    Returns None if the lookup fails (network issue, package not found).
    """
    try:
        result = subprocess.run(
            ["npm", "view", package_name, "version"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
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


def get_gateway_host() -> str:
    """Resolve the AI Gateway host URL.

    Priority:
      0. _GATEWAY_RESOLVED env var (set by parent process after probing — avoids
         re-probing in subprocesses). None = never probed, "" = probed, no gateway.
      1. Explicit DATABRICKS_GATEWAY_HOST env var (trusted — no probe)
      2. Auto-constructed from DATABRICKS_WORKSPACE_ID (probed for reachability)
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
    workspace_id = os.environ.get("DATABRICKS_WORKSPACE_ID", "").strip()
    if workspace_id:
        candidate = f"https://{workspace_id}.ai-gateway.cloud.databricks.com"
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
