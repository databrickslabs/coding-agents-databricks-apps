"""Shared utilities for Databricks App setup scripts."""

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


def get_gateway_host() -> str:
    """Resolve the AI Gateway host URL.

    Priority:
      1. Explicit DATABRICKS_GATEWAY_HOST env var (override)
      2. Auto-constructed from DATABRICKS_WORKSPACE_ID
      3. Empty string (caller falls back to DATABRICKS_HOST/serving-endpoints)
    """
    explicit = os.environ.get("DATABRICKS_GATEWAY_HOST", "").strip().rstrip("/")
    if explicit:
        return ensure_https(explicit)

    workspace_id = os.environ.get("DATABRICKS_WORKSPACE_ID", "").strip()
    if workspace_id:
        return f"https://{workspace_id}.ai-gateway.cloud.databricks.com"

    return ""


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
