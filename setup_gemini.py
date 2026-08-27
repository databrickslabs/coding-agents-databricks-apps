#!/usr/bin/env python
"""Configure Gemini CLI with Databricks Model Serving.

Gemini CLI uses the Google Generative Language API protocol, not OpenAI-compatible.
Databricks provides a Google-native route through the workspace AI Gateway.

PR #11893 (by Databricks engineer AarushiShah) added auto-detection of *.databricks.com
URLs, switching to Bearer token auth automatically.

Auth: GEMINI_API_KEY_AUTH_MECHANISM=bearer sends a fresh helper-resolved token.

Opt-out:
  Set ENABLE_GEMINI=false in app.yaml to skip installation entirely.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from cli_auth import _atomic_write_text
from gateway_models import discover_model_catalog, gemini_base_url
from token_helper import resolve_databricks_token, write_token_helper
from utils import adapt_instructions_file, ensure_https, get_npm_version

# Opt-out: allow operators to disable Gemini bundling without removing the file.
if os.environ.get("ENABLE_GEMINI", "true").strip().lower() in ("false", "0", "no"):
    print("ENABLE_GEMINI=false — skipping Gemini CLI setup")
    raise SystemExit(0)

# Set HOME if not properly set
if not os.environ.get("HOME") or os.environ["HOME"] == "/":
    os.environ["HOME"] = "/app/python/source_code"

home = Path(os.environ["HOME"])

host = os.environ.get("DATABRICKS_HOST", "")
# Use the same broker-aware credential resolution as Claude, Pi, and OpenCode
# so SP-authenticated boot works without requiring a pasted PAT.
token = resolve_databricks_token() or ""
requested_model = os.environ.get("GEMINI_MODEL", "system.ai.gemini-3-flash")

# 1. Install Gemini CLI into ~/.local/bin (always, even without token)
local_bin = home / ".local" / "bin"
local_bin.mkdir(parents=True, exist_ok=True)
gemini_bin = local_bin / "gemini"
gemini_real_bin = local_bin / "gemini.coda-real"

MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds

if not gemini_bin.exists():
    npm_prefix = str(home / ".local")
    gemini_version = get_npm_version("@google/gemini-cli")
    gemini_pkg = f"@google/gemini-cli@{gemini_version}" if gemini_version else "@google/gemini-cli@latest"

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"Installing {gemini_pkg} (attempt {attempt}/{MAX_RETRIES})...")
        result = subprocess.run(
            ["npm", "install", "-g", f"--prefix={npm_prefix}", gemini_pkg],
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(home)}
        )
        if result.returncode == 0 and gemini_bin.exists():
            print(f"Gemini CLI installed to {gemini_bin}")
            break
        else:
            stderr = result.stderr.strip()
            print(f"Gemini CLI install failed (attempt {attempt}/{MAX_RETRIES}, rc={result.returncode})")
            if stderr:
                print(f"  stderr: {stderr[:500]}")
            if result.stdout.strip():
                print(f"  stdout: {result.stdout.strip()[:500]}")
            if attempt < MAX_RETRIES:
                import time
                print(f"  Retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"ERROR: Gemini CLI installation failed after {MAX_RETRIES} attempts. "
                      f"Run manually: npm install -g --prefix=$HOME/.local @google/gemini-cli")
else:
    print(f"Gemini CLI already installed at {gemini_bin}")

# 2. Skip auth config if no token (will be configured after PAT setup)
if not host or not token:
    print("Gemini CLI installed — config will be set after PAT setup")
    exit(0)

# Strip trailing slash and ensure https:// prefix
host = ensure_https(host.rstrip("/"))

# Discover system.ai model services that advertise the native Gemini dialect.
# Prefer the configured request when it is served, otherwise select the newest
# compatible model returned by the gateway catalog.
catalog = discover_model_catalog(host, token)
gemini_models = catalog["gemini"]
gemini_model = requested_model
if gemini_models:
    gemini_model = requested_model if requested_model in gemini_models else gemini_models[0]
    if gemini_model != requested_model:
        print(f"GEMINI_MODEL={requested_model} not served here, using {gemini_model}")

gemini_url = gemini_base_url(host)
print(f"Using workspace AI Gateway Gemini API: {gemini_url}")

# 3. Create ~/.gemini directory and configure environment
gemini_dir = home / ".gemini"
gemini_dir.mkdir(exist_ok=True)

# Gemini CLI has no ucode-style auth-command provider field. Keep the same
# broker boundary by placing a tiny wrapper at the public `gemini` path; it
# resolves a fresh token through the shared helper before every CLI process.
helper_path = write_token_helper(gemini_dir)
if not gemini_real_bin.exists() and gemini_bin.exists():
    gemini_bin.rename(gemini_real_bin)
wrapper_content = f'''#!{sys.executable}
import os
import subprocess
import sys

helper = {str(helper_path)!r}
real_gemini = {str(gemini_real_bin)!r}
try:
    token = subprocess.check_output(
        [sys.executable, helper], text=True, stderr=subprocess.PIPE
    ).strip()
except (OSError, subprocess.CalledProcessError) as exc:
    print(f"Gemini token helper failed: {{exc}}", file=sys.stderr)
    raise SystemExit(1)
if not token:
    print("Gemini token helper returned no token", file=sys.stderr)
    raise SystemExit(1)
env = os.environ.copy()
env["GEMINI_API_KEY"] = token
os.execvpe(real_gemini, [real_gemini, *sys.argv[1:]], env)
'''
if gemini_real_bin.exists():
    gemini_bin.write_text(wrapper_content)
    gemini_bin.chmod(0o700)

# Pre-trust ~/projects/ so Gemini CLI loads .env and project settings.
# Without this, Gemini's security engine silently skips .env loading in
# untrusted workspaces, causing auth failures (see gemini-cli#20005).
projects_dir = str(home / "projects")
trusted_folders_path = gemini_dir / "trustedFolders.json"
try:
    if trusted_folders_path.exists():
        trusted = json.loads(trusted_folders_path.read_text())
    else:
        trusted = {}
    if trusted.get(projects_dir) != "TRUST_FOLDER":
        trusted[projects_dir] = "TRUST_FOLDER"
    # Also trust home dir so ~/.gemini/.env is always loadable
    home_str = str(home)
    if trusted.get(home_str) != "TRUST_FOLDER":
        trusted[home_str] = "TRUST_FOLDER"
    trusted_folders_path.write_text(json.dumps(trusted, indent=2))
    print(f"Gemini trusted folders configured: {trusted_folders_path}")
except Exception as e:
    print(f"Warning: could not write trustedFolders.json: {e}")

# Write .env file with Databricks endpoint configuration
# Gemini CLI auto-loads env from ~/.gemini/.env
# The Google-native route is the workspace AI Gateway Gemini v1beta path.
env_content = f"""# Databricks Model Serving - Google Gemini native endpoint
GEMINI_MODEL={gemini_model}
GOOGLE_GEMINI_BASE_URL={gemini_url}
GEMINI_API_KEY_AUTH_MECHANISM=bearer
"""

env_path = gemini_dir / ".env"
_atomic_write_text(str(env_path), env_content)
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

# 5. Skills live in ~/.agents/skills/ (shared across all CLIs, copied by setup_codex.py).
#    Do NOT copy into ~/.gemini/skills/ — Gemini discovers both paths and logs
#    "Skill conflict detected" warnings for every duplicate.

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
print(f"\nEndpoint: {gemini_url}")
print("Auth: Bearer token (Databricks PAT)")
