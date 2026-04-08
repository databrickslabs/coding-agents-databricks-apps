#!/usr/bin/env python
"""Configure Gemini CLI with Databricks Model Serving.

Gemini CLI uses the Google Generative Language API protocol, not OpenAI-compatible.
Databricks provides a Google-native endpoint at /serving-endpoints/google
(similar to /serving-endpoints/anthropic for Claude).

PR #11893 (by Databricks engineer AarushiShah) added auto-detection of *.databricks.com
URLs, switching to Bearer token auth automatically.

Auth: GEMINI_API_KEY_AUTH_MECHANISM=bearer sends Databricks PAT as Bearer token.
"""
import os
import json
import shutil
import subprocess
from pathlib import Path

from utils import adapt_instructions_file, ensure_https, get_gateway_host, get_npm_version

# Set HOME if not properly set
if not os.environ.get("HOME") or os.environ["HOME"] == "/":
    os.environ["HOME"] = "/app/python/source_code"

home = Path(os.environ["HOME"])

host = os.environ.get("DATABRICKS_HOST", "")
token = os.environ.get("DATABRICKS_TOKEN", "")
gemini_model = os.environ.get("GEMINI_MODEL", "databricks-gemini-3-1-pro")

# 1. Install Gemini CLI into ~/.local/bin (always, even without token)
local_bin = home / ".local" / "bin"
local_bin.mkdir(parents=True, exist_ok=True)
gemini_bin = local_bin / "gemini"

if not gemini_bin.exists():
    # Use --prefix ~/.local so npm installs directly into ~/.local/bin (avoids EACCES on /usr/local)
    npm_prefix = str(home / ".local")
    gemini_version = get_npm_version("@google/gemini-cli")
    gemini_pkg = f"@google/gemini-cli@{gemini_version}" if gemini_version else "@google/gemini-cli@latest"
    print(f"Installing {gemini_pkg}...")
    result = subprocess.run(
        ["npm", "install", "-g", f"--prefix={npm_prefix}", gemini_pkg],
        capture_output=True, text=True,
        env={**os.environ, "HOME": str(home)}
    )
    if result.returncode == 0:
        print(f"Gemini CLI installed to {gemini_bin}")
    else:
        print(f"Gemini CLI install warning: {result.stderr}")
else:
    print(f"Gemini CLI already installed at {gemini_bin}")

# 2. Skip auth config if no token (will be configured after PAT setup)
if not host or not token:
    print("Gemini CLI installed — config will be set after PAT setup")
    exit(0)

# Strip trailing slash and ensure https:// prefix
host = ensure_https(host.rstrip("/"))

gateway_host = get_gateway_host()
gateway_token = os.environ.get("DATABRICKS_TOKEN", "") if gateway_host else ""
if gateway_host and not gateway_token:
    print("Warning: AI Gateway resolved but DATABRICKS_TOKEN missing, falling back to DATABRICKS_HOST")
    gateway_host = ""

if gateway_host:
    gemini_base_url = f"{gateway_host}/gemini"
    auth_token = gateway_token
    print(f"Using Databricks AI Gateway: {gateway_host}")
else:
    gemini_base_url = f"{host}/serving-endpoints/google"
    auth_token = token
    print(f"Using Databricks Host: {host}")

# 3. Create ~/.gemini directory and configure environment
gemini_dir = home / ".gemini"
gemini_dir.mkdir(exist_ok=True)

# Write .env file with Databricks endpoint configuration
# Gemini CLI auto-loads env from ~/.gemini/.env
# The Google-native endpoint on Databricks mirrors /serving-endpoints/anthropic
env_content = f"""# Databricks Model Serving - Google Gemini native endpoint
GEMINI_MODEL={gemini_model}
GOOGLE_GEMINI_BASE_URL={gemini_base_url}
GEMINI_API_KEY_AUTH_MECHANISM="bearer"
GEMINI_API_KEY={auth_token}
"""

env_path = gemini_dir / ".env"
env_path.write_text(env_content)
env_path.chmod(0o600)
print(f"Gemini CLI env configured: {env_path}")

# 4. Write settings.json with model preferences and auth
settings = {
    "theme": "Default",
    "selectedAuthType": "gemini-api-key",
    "model": {
        "name": gemini_model
    }
}

settings_path = gemini_dir / "settings.json"
settings_path.write_text(json.dumps(settings, indent=2))
print(f"Gemini CLI settings configured: {settings_path}")

# 5. Copy Claude skills into .gemini/skills for shared reference
claude_skills_dir = home / ".claude" / "skills"
gemini_skills_dir = gemini_dir / "skills"
if claude_skills_dir.exists():
    if gemini_skills_dir.exists():
        shutil.rmtree(gemini_skills_dir)
    shutil.copytree(claude_skills_dir, gemini_skills_dir)
    print(f"Skills copied: {claude_skills_dir} -> {gemini_skills_dir}")
else:
    print(f"No Claude skills found at {claude_skills_dir}, skipping copy")

# 6. Adapt CLAUDE.md to GEMINI.md for Gemini CLI
# Look for CLAUDE.md in common locations
claude_md_locations = [
    Path(__file__).parent / "CLAUDE.md",  # Same directory as setup script
    home / ".claude" / "CLAUDE.md",        # User's Claude config
    Path("/app/python/source_code/CLAUDE.md"),  # Databricks App location
]

claude_md_path = None
for loc in claude_md_locations:
    if loc.exists():
        claude_md_path = loc
        break

gemini_md_path = gemini_dir / "GEMINI.md"
adapt_instructions_file(
    source_path=claude_md_path or claude_md_locations[0],
    target_path=gemini_md_path,
    new_header="# Gemini CLI on Databricks",
    cli_name="Gemini",
)

print("\nGemini CLI ready! Usage:")
print("  gemini                                    # Start Gemini CLI")
print(f"\nEndpoint: {gemini_base_url}")
print("Auth: Bearer token (Databricks PAT)")
