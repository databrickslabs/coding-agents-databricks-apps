#!/usr/bin/env python
"""Configure the Pi coding agent (@earendil-works/pi-coding-agent) for Databricks.

Pi uses the same workspace AI Gateway provider dialects and model-services
catalog as ucode: Anthropic, Responses, Gemini, and MLflow chat completions.
No harness route uses the legacy external ``*.ai-gateway.*`` hostname.

Config file: ~/.pi/agent/models.json (JSON). Token freshness: every provider's
`apiKey` is a `!command` that reads the
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

from cli_auth import _atomic_write_text
from utils import adapt_instructions_file, get_npm_version
from gateway_models import (
    claude_model_capabilities,
    discover_model_catalog,
    pi_base_urls,
    preferred_model,
)
from token_helper import resolve_databricks_token

# Opt-out: allow operators to disable Pi bundling without removing the file.
if os.environ.get("ENABLE_PI", "true").strip().lower() in ("false", "0", "no"):
    print("ENABLE_PI=false — skipping Pi CLI setup")
    raise SystemExit(0)

# Set HOME if not properly set
if not os.environ.get("HOME") or os.environ["HOME"] == "/":
    os.environ["HOME"] = "/app/python/source_code"

home = Path(os.environ["HOME"])

host = os.environ.get("DATABRICKS_HOST", "")
# The SP broker is the primary auth source on the no-PAT baseline. Checking only
# DATABRICKS_TOKEN made setup exit 0 before writing models.json even though the
# broker was healthy — setup-status said `pi: complete`, but Pi was unusable.
# Resolve through the same layered source as OpenCode/Claude: SP broker/profile,
# then user PAT. This token is only used during setup; Pi writes a helper command
# for per-request freshness below.
token = resolve_databricks_token() or ""
pi_model = os.environ.get("PI_MODEL", "system.ai.claude-sonnet-5")

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

base_urls = pi_base_urls(host)
catalog = discover_model_catalog(host, token)
claude_models = catalog["anthropic"] or [pi_model]
active_model = preferred_model(pi_model, claude_models)
print(f"Using workspace AI Gateway: {base_urls['claude']}")
print(
    "Discovered model-services: "
    f"claude={len(catalog['anthropic'])}, openai={len(catalog['openai'])}, "
    f"gemini={len(catalog['gemini'])}, oss={len(catalog['oss'])}"
)

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
for owned_provider in (
    "databricks-claude",
    "databricks-openai",
    "databricks-gemini",
    "databricks-mlflow",
):
    config["providers"].pop(owned_provider, None)
# Mirrors ucode's `_pi_claude_model_entry`. Databricks model ids don't match
# Pi's built-in Anthropic ids, so a bare entry silently inherits Pi's 128k/4k
# defaults — the limits have to be explicit. Newer Claude tiers need
# `forceAdaptiveThinking`: without it Pi sends `thinking: {type: "enabled"}` and
# the endpoint answers 400 "thinking.type.enabled is not supported for this
# model" (seen live on opus-5).
_claude_specs = {spec["id"]: spec for spec in (catalog.get("anthropic_specs") or [])}


def _pi_model_entry(model: str) -> dict:
    # The version policy is local, so a discovery outage must not silently
    # change limits or drop adaptive thinking.
    spec = _claude_specs.get(model) or {**claude_model_capabilities(model), "image_input": True}
    entry = {
        "id": model,
        "reasoning": True,
        "input": ["text", "image"] if spec.get("image_input", True) else ["text"],
        "contextWindow": spec.get("context_window") or 200_000,
        "maxTokens": spec.get("max_tokens") or 64_000,
    }
    if spec.get("force_adaptive_thinking"):
        entry["compat"] = {"forceAdaptiveThinking": True}
    return entry


config["providers"]["databricks-claude"] = {
    "baseUrl": base_urls["claude"],
    "api": "anthropic-messages",
    "apiKey": api_key_command,
    "authHeader": True,
    "compat": {"supportsEagerToolInputStreaming": False},
    "models": [_pi_model_entry(model) for model in claude_models],
}
if catalog["openai"]:
    config["providers"]["databricks-openai"] = {
        "baseUrl": base_urls["openai"],
        "api": "openai-responses",
        "apiKey": api_key_command,
        "authHeader": True,
        "models": [
            {
                "id": model,
                "contextWindow": 272_000,
                "maxTokens": 128_000,
                "reasoning": True,
                "input": ["text", "image"],
                "thinkingLevelMap": {"off": None},
            }
            for model in catalog["openai"]
        ],
    }
if catalog["gemini"]:
    config["providers"]["databricks-gemini"] = {
        "baseUrl": base_urls["gemini"],
        "api": "google-generative-ai",
        "apiKey": api_key_command,
        "authHeader": True,
        "models": [{"id": model} for model in catalog["gemini"]],
    }
if catalog["oss"]:
    specs = {spec["id"]: spec for spec in catalog["oss_specs"]}
    config["providers"]["databricks-mlflow"] = {
        "baseUrl": base_urls["oss"],
        "api": "openai-completions",
        "apiKey": api_key_command,
        "authHeader": True,
        "compat": {"supportsStore": False, "supportsStrictMode": False},
        "models": [
            {
                "id": model,
                **(
                    {"reasoning": True}
                    if specs.get(model, {}).get("reasoning") is True
                    else {}
                ),
                "contextWindow": specs.get(model, {}).get("context_window") or 128_000,
                "maxTokens": specs.get(model, {}).get("max_tokens") or 8_192,
            }
            for model in catalog["oss"]
        ],
    }

_atomic_write_text(str(models_path), json.dumps(config, indent=2))
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
print(f"\nEndpoint: {base_urls['claude']}")
print(f"Model: databricks-claude/{active_model}")
print("Auth: Bearer token (Databricks)")
