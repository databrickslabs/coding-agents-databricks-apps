#!/usr/bin/env python
"""Configure the Pi coding agent (@earendil-works/pi-coding-agent) for Databricks.

Pi is Anthropic-wire-compatible: it authenticates to the same Databricks AI
Gateway `/anthropic` route CoDA already uses for Claude Code, with the same
service-principal token. So this is not a new gateway integration — it writes a
Pi-shaped config over the existing, proven auth path.

Config file: ~/.pi/agent/models.json (JSON). We configure ONLY the
`databricks-claude` provider (Pi's optional openai/gemini providers are left
out). Token freshness: `apiKey` is written as a `!command` that reads the
current token from ~/.databrickscfg. Pi resolves `!`-prefixed values as shell
commands *fresh per request* (docs/models.md: "shell commands are resolved at
request time"), so a long-running pi always sends a live token and survives PAT
rotation without a restart -- the rotator keeps ~/.databrickscfg current
(writing the new PAT before revoking the old one). cli_auth._update_pi()
therefore leaves this command in place (it only rewrites a static literal),
mirroring how Claude's apiKeyHelper owns its own auth.

Opt-out:
  Set ENABLE_PI=false in app.yaml to skip installation entirely.
"""
import os
import json
import subprocess
from pathlib import Path

from utils import (
    adapt_instructions_file,
    discover_serving_endpoints,
    ensure_https,
    get_gateway_host,
    get_npm_version,
    pick_in_geo_model,
)

# Opt-out: allow operators to disable Pi bundling without removing the file.
if os.environ.get("ENABLE_PI", "true").strip().lower() in ("false", "0", "no"):
    print("ENABLE_PI=false — skipping Pi CLI setup")
    raise SystemExit(0)

# Set HOME if not properly set
if not os.environ.get("HOME") or os.environ["HOME"] == "/":
    os.environ["HOME"] = "/app/python/source_code"

home = Path(os.environ["HOME"])

host = os.environ.get("DATABRICKS_HOST", "")
token = os.environ.get("DATABRICKS_TOKEN", "").strip()
pi_model = os.environ.get("PI_MODEL", "databricks-claude-opus-4-8")

PI_PACKAGE = "@earendil-works/pi-coding-agent"

# 1. Install Pi CLI into ~/.local/bin (always, even without token)
local_bin = home / ".local" / "bin"
local_bin.mkdir(parents=True, exist_ok=True)
pi_bin = local_bin / "pi"

MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds

if not pi_bin.exists():
    npm_prefix = str(home / ".local")
    pi_version = get_npm_version(PI_PACKAGE)
    pi_pkg = f"{PI_PACKAGE}@{pi_version}" if pi_version else f"{PI_PACKAGE}@latest"

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"Installing {pi_pkg} (attempt {attempt}/{MAX_RETRIES})...")
        # --ignore-scripts disables dependency lifecycle scripts; Pi does not
        # require install scripts for a normal npm install.
        result = subprocess.run(
            ["npm", "install", "-g", "--ignore-scripts", f"--prefix={npm_prefix}", pi_pkg],
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(home)}
        )
        if result.returncode == 0 and pi_bin.exists():
            print(f"Pi CLI installed to {pi_bin}")
            break
        else:
            stderr = result.stderr.strip()
            print(f"Pi CLI install failed (attempt {attempt}/{MAX_RETRIES}, rc={result.returncode})")
            if stderr:
                print(f"  stderr: {stderr[:500]}")
            if result.stdout.strip():
                print(f"  stdout: {result.stdout.strip()[:500]}")
            if attempt < MAX_RETRIES:
                import time
                print(f"  Retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"ERROR: Pi CLI installation failed after {MAX_RETRIES} attempts. "
                      f"Run manually: npm install -g --ignore-scripts --prefix=$HOME/.local {PI_PACKAGE}")
else:
    print(f"Pi CLI already installed at {pi_bin}")

# 2. Skip auth config if no token (will be configured after PAT setup)
if not host or not token:
    print("Pi CLI installed — config will be set after PAT setup")
    exit(0)

# Strip trailing slash and ensure https:// prefix
host = ensure_https(host.rstrip("/"))

gateway_host = get_gateway_host()
gateway_token = os.environ.get("DATABRICKS_TOKEN", "") if gateway_host else ""
if gateway_host and not gateway_token:
    print("Warning: AI Gateway resolved but DATABRICKS_TOKEN missing, falling back to DATABRICKS_HOST")
    gateway_host = ""

if gateway_host:
    base_url = f"{gateway_host}/anthropic"
    auth_token = gateway_token
    print(f"Using Databricks AI Gateway: {gateway_host}")
else:
    base_url = f"{host}/serving-endpoints/anthropic"
    auth_token = token
    print(f"Using Databricks Host: {host}")

# Validate the requested model against what's actually served in this geo, the
# same way setup_claude.py does — Pi hits the identical /anthropic route, so the
# same model chain applies. Avoids writing a model the workspace's Geo
# Designated Services policy doesn't serve.
available = discover_serving_endpoints(host, token)
if available:
    print(f"Discovered {len(available)} READY serving endpoints at workspace")
active_model = pick_in_geo_model(
    [pi_model, "databricks-claude-opus-4-7", "databricks-claude-opus-4-6", "databricks-claude-sonnet-4-6"],
    available,
    fallback=pi_model,
)
if available and active_model != pi_model:
    print(f"PI_MODEL={pi_model} not served at this workspace, using {active_model}")

# 3. Write ~/.pi/agent/models.json (the databricks-claude provider only).
# Read-merge-write so a re-run preserves keys we don't own.
pi_config_dir = home / ".pi" / "agent"
pi_config_dir.mkdir(parents=True, exist_ok=True)
models_path = pi_config_dir / "models.json"

if models_path.exists():
    try:
        config = json.loads(models_path.read_text())
    except (json.JSONDecodeError, OSError):
        config = {}
else:
    config = {}

# apiKey as a per-request shell command, NOT a static literal: pi resolves a
# `!`-prefixed value fresh on every request. Point it at the SAME token helper
# Claude Code uses (SP OAuth from the omnigents-host profile on the host path,
# else the PAT from $DATABRICKS_TOKEN / ~/.databrickscfg on the interactive
# path). Because the helper is authoritative and resolved per request, a long-
# running pi survives PAT rotation and SP-OAuth expiry without a restart -- and
# there is no static token in models.json for the rotator to keep fresh. Write
# the helper here too (idempotent) so pi does not depend on setup_claude.py
# having run first.
from token_helper import write_token_helper, helper_command
helper_path = write_token_helper(home / ".claude")
api_key_command = helper_command(helper_path)

config["model"] = f"databricks-claude/{active_model}"
config.setdefault("providers", {})
config["providers"]["databricks-claude"] = {
    "baseUrl": base_url,
    "api": "anthropic-messages",
    "apiKey": api_key_command,
    "authHeader": True,
    "compat": {"supportsEagerToolInputStreaming": False},
    # contextWindow is explicit: Pi defaults a custom provider's model to 131072
    # (128K) when absent. The Databricks FMAPI-served Claude model has a ~1.05M
    # total-token context window (per Databricks Foundation Model APIs supported-
    # models docs), so the 128K default badly under-uses it. Set the real window.
    # NB: the FMAPI *rate* limits are separate (e.g. ~200k input tokens/minute on
    # the default tier) — that's throughput, not context, and is not fixed here.
    "models": [{"id": active_model, "contextWindow": 1000000}],
}

models_path.write_text(json.dumps(config, indent=2))
models_path.chmod(0o600)
print(f"Pi configured: {models_path}")

# 4. Adapt CLAUDE.md to PI.md for Pi. Look for CLAUDE.md in common locations.
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

pi_md_path = home / ".pi" / "PI.md"
adapt_instructions_file(
    source_path=claude_md_path or claude_md_locations[0],
    target_path=pi_md_path,
    new_header="# Pi on Databricks",
    cli_name="Pi",
)

print("\nPi CLI ready! Usage:")
print("  pi                                        # Start Pi")
print(f"\nEndpoint: {base_url}")
print(f"Model: databricks-claude/{active_model}")
print("Auth: Bearer token (Databricks)")
