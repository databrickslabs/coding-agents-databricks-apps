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
import subprocess
from pathlib import Path

# Set HOME if not properly set
if not os.environ.get("HOME") or os.environ["HOME"] == "/":
    os.environ["HOME"] = "/app/python/source_code"

home = Path(os.environ["HOME"])

host = os.environ.get("DATABRICKS_HOST", "")
token = os.environ.get("DATABRICKS_TOKEN", "")
gemini_model = os.environ.get("GEMINI_MODEL", "databricks-gemini-2-5-flash")

if not host or not token:
    print("Warning: DATABRICKS_HOST or DATABRICKS_TOKEN not set, skipping Gemini CLI config")
    exit(0)

# Strip trailing slash from host
host = host.rstrip("/")

# Use DATABRICKS_GATEWAY_HOST if available (new AI Gateway), otherwise fall back to DATABRICKS_HOST
gateway_host = os.environ.get("DATABRICKS_GATEWAY_HOST", "").rstrip("/")
gateway_token = os.environ.get("DATABRICKS_GATEWAY_TOKEN", "") if gateway_host else ""
if gateway_host and not gateway_token:
    print("Warning: DATABRICKS_GATEWAY_HOST set but DATABRICKS_GATEWAY_TOKEN missing, falling back to DATABRICKS_HOST")
    gateway_host = ""

if gateway_host:
    gemini_base_url = f"{gateway_host}/gemini"
    auth_token = gateway_token
    print(f"Using Databricks AI Gateway: {gateway_host}")
else:
    gemini_base_url = f"{host}/serving-endpoints/google"
    auth_token = token
    print(f"Using Databricks Host: {host}")

# 1. Install Gemini CLI into ~/.local/bin (same approach as Claude Code)
local_bin = home / ".local" / "bin"
local_bin.mkdir(parents=True, exist_ok=True)
gemini_bin = local_bin / "gemini"

if not gemini_bin.exists():
    print("Installing Gemini CLI...")
    # Use --prefix ~/.local so npm installs directly into ~/.local/bin (avoids EACCES on /usr/local)
    npm_prefix = str(home / ".local")
    result = subprocess.run(
        ["npm", "install", "-g", f"--prefix={npm_prefix}", "@google/gemini-cli"],
        capture_output=True, text=True,
        env={**os.environ, "HOME": str(home)}
    )
    if result.returncode == 0:
        print(f"Gemini CLI installed to {gemini_bin}")
    else:
        print(f"Gemini CLI install warning: {result.stderr}")
else:
    print(f"Gemini CLI already installed at {gemini_bin}")

# 2. Create ~/.gemini directory and configure environment
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

# 3. Write settings.json with model preferences
settings = {
    "theme": "Default",
    "selectedAuthType": "api-key"
}

settings_path = gemini_dir / "settings.json"
settings_path.write_text(json.dumps(settings, indent=2))
print(f"Gemini CLI settings configured: {settings_path}")

print("\nGemini CLI ready! Usage:")
print("  gemini                                    # Start Gemini CLI")
print(f"  gemini -m gemini-2.5-flash                # Use Gemini 2.5 Flash")
print(f"  gemini -m gemini-2.5-pro                  # Use Gemini 2.5 Pro")
print(f"\nEndpoint: {gemini_base_url}")
print("Auth: Bearer token (Databricks PAT)")
