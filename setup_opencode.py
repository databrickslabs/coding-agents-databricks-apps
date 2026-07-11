#!/usr/bin/env python
"""Configure OpenCode CLI with Databricks Model Serving (via content-filter proxy) local proxy.

Routes requests through a local content-filter proxy proxy (localhost:4000) which sanitizes empty
text content blocks before forwarding to Databricks AI Gateway. This fixes OpenCode
issue #5028 where empty content blocks cause "Bad Request" errors.
See docs/plans/2026-03-11-litellm-empty-content-blocks-design.md for details.

Opt-out:
  Set ENABLE_OPENCODE=false in app.yaml to skip installation entirely.
"""
import os
import json
import subprocess
from pathlib import Path

from utils import (
    discover_serving_endpoints,
    ensure_https,
    get_gateway_host,
    get_npm_version,
    pick_in_geo_model,
)
from enterprise_config import npm_env

# Opt-out: allow operators to disable OpenCode bundling without removing the file.
if os.environ.get("ENABLE_OPENCODE", "true").strip().lower() in ("false", "0", "no"):
    print("ENABLE_OPENCODE=false — skipping OpenCode CLI setup")
    raise SystemExit(0)

# content-filter proxy local proxy — sanitizes empty content blocks before reaching Databricks
# (see https://github.com/sst/opencode/issues/5028)
CONTENT_FILTER_PROXY_URL = "http://127.0.0.1:4000"

# Set HOME if not properly set
if not os.environ.get("HOME") or os.environ["HOME"] == "/":
    os.environ["HOME"] = "/app/python/source_code"

home = Path(os.environ["HOME"])

host = os.environ.get("DATABRICKS_HOST", "")
token = os.environ.get("DATABRICKS_TOKEN", "")
anthropic_model = os.environ.get("ANTHROPIC_MODEL", "databricks-claude-opus-4-8")

# 1. Install OpenCode CLI into ~/.local/bin (always, even without token)
local_bin = home / ".local" / "bin"
local_bin.mkdir(parents=True, exist_ok=True)
opencode_bin = local_bin / "opencode"

MAX_RETRIES = 3
RETRY_DELAY = 5  # seconds

if not opencode_bin.exists():
    npm_prefix = str(home / ".local")

    # Resolve exact versions to avoid mutable @latest tags (supply chain hardening)
    oc_version = get_npm_version("opencode-ai")
    oc_pkg = f"opencode-ai@{oc_version}" if oc_version else "opencode-ai@latest"

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"Installing {oc_pkg} (attempt {attempt}/{MAX_RETRIES})...")
        result = subprocess.run(
            ["npm", "install", "-g", f"--prefix={npm_prefix}", oc_pkg],
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(home), **npm_env()}
        )
        if result.returncode == 0 and opencode_bin.exists():
            print(f"OpenCode CLI installed to {opencode_bin}")
            break
        else:
            stderr = result.stderr.strip()
            print(f"OpenCode install failed (attempt {attempt}/{MAX_RETRIES}, rc={result.returncode})")
            if stderr:
                print(f"  stderr: {stderr[:500]}")
            if result.stdout.strip():
                print(f"  stdout: {result.stdout.strip()[:500]}")
            if attempt < MAX_RETRIES:
                import time
                print(f"  Retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"ERROR: OpenCode installation failed after {MAX_RETRIES} attempts. "
                      f"Run manually: npm install -g --prefix=$HOME/.local opencode-ai")

    # Install @ai-sdk/openai for GPT models (Responses API support)
    sdk_version = get_npm_version("@ai-sdk/openai")
    sdk_pkg = f"@ai-sdk/openai@{sdk_version}" if sdk_version else "@ai-sdk/openai"
    print(f"Installing {sdk_pkg}...")
    result = subprocess.run(
        ["npm", "install", "-g", f"--prefix={npm_prefix}", sdk_pkg],
        capture_output=True, text=True,
        env={**os.environ, "HOME": str(home), **npm_env()}
    )
    if result.returncode == 0:
        print(f"@ai-sdk/openai@{sdk_version or 'latest'} installed (Responses API support)")
    else:
        print(f"@ai-sdk/openai install warning: {result.stderr[:500]}")
else:
    print(f"OpenCode CLI already installed at {opencode_bin}")

# 2. Skip auth config if no token (will be configured after PAT setup)
if not host or not token:
    print("OpenCode CLI installed — config will be set after PAT setup")
    exit(0)

# Strip trailing slash and ensure https:// prefix
host = ensure_https(host.rstrip("/"))

gateway_host = get_gateway_host()
gateway_token = os.environ.get("DATABRICKS_TOKEN", "") if gateway_host else ""
if gateway_host and not gateway_token:
    print("Warning: AI Gateway resolved but DATABRICKS_TOKEN missing, falling back to DATABRICKS_HOST")
    gateway_host = ""

if gateway_host:
    print(f"Using Databricks AI Gateway: {gateway_host}")
else:
    print(f"Using Databricks Host: {host}")

# 2b. Discover which endpoints the workspace actually serves, so the config
# never advertises a model that isn't deployed here (e.g. gemini endpoints in a
# claude-only workspace). A bad model id surfaces to opencode as
# ``AI_APICallError: Not Found`` (ENDPOINT_NOT_FOUND) and is the exact failure
# that broke opencode-native on the Omnigent host. Mirrors setup_pi.py.
# Empty set = discovery unavailable (network/auth) → keep the static defaults.
_served = discover_serving_endpoints(host, token)
if _served:
    print(f"Discovered {len(_served)} READY serving endpoints at workspace")

# Candidate chat models with opencode display metadata, in preference order.
# Only those actually served are written into the config.
_CLAUDE_CANDIDATES = [
    ("databricks-claude-opus-4-8", "Claude Opus 4.8 (Databricks)", 1000000, 16384),
    ("databricks-claude-opus-4-7", "Claude Opus 4.7 (Databricks)", 1000000, 16384),
    ("databricks-claude-opus-4-6", "Claude Opus 4.6 (Databricks)", 1000000, 16384),
    ("databricks-claude-sonnet-4-6", "Claude Sonnet 4.6 (Databricks)", 1000000, 8192),
    ("databricks-claude-sonnet-4-5", "Claude Sonnet 4.5 (Databricks)", 1000000, 8192),
    ("databricks-claude-haiku-4-5", "Claude Haiku 4.5 (Databricks)", 1000000, 8192),
    ("databricks-gemini-2-5-pro", "Gemini 2.5 Pro (Databricks)", 1000000, 8192),
    ("databricks-gemini-2-5-flash", "Gemini 2.5 Flash (Databricks)", 1000000, 8192),
]


def _build_models(candidates):
    """Return an opencode ``models`` dict for the served subset of *candidates*.

    When discovery is unavailable (empty ``_served``), fall back to the static
    candidate list unfiltered so a discovery blip doesn't wipe the config.
    """
    models = {}
    for name, label, ctx, out in candidates:
        if _served and name not in _served:
            continue
        models[name] = {"name": label, "limit": {"context": ctx, "output": out}}
    if not models:  # discovery filtered everything out — never ship an empty map
        for name, label, ctx, out in candidates:
            models[name] = {"name": label, "limit": {"context": ctx, "output": out}}
    return models


_databricks_models = _build_models(_CLAUDE_CANDIDATES)

# GPT / OpenAI-family endpoints for the (gateway-only) databricks-openai
# provider. Same served-subset filtering so it never advertises a
# non-deployed codex/gpt endpoint.
_GPT_CANDIDATES = [
    ("databricks-gpt-5-3-codex", "GPT 5.3 Codex (Databricks)", 200000, 16384),
    ("databricks-gpt-5-1-codex-max", "GPT 5.1 Codex Max (Databricks)", 200000, 16384),
    ("databricks-gpt-oss-120b", "GPT OSS 120B (Databricks)", 128000, 16384),
    ("databricks-gpt-oss-20b", "GPT OSS 20B (Databricks)", 128000, 16384),
]
_databricks_openai_models = _build_models(_GPT_CANDIDATES)

# Pick a valid default model that IS served (degradation chain), so opencode's
# TUI + first turn launch on a real endpoint instead of an unknown id.
active_model = pick_in_geo_model(
    [anthropic_model, "databricks-claude-opus-4-8", "databricks-claude-opus-4-7",
     "databricks-claude-opus-4-6", "databricks-claude-sonnet-4-6"],
    _served,
    fallback=anthropic_model,
)
if _served and active_model != anthropic_model:
    print(f"ANTHROPIC_MODEL={anthropic_model} not served here, using {active_model}")

# 3. Write global opencode.json config
# OpenCode looks for config at ~/.config/opencode/opencode.json (global)
# and ./opencode.json (project-level)
opencode_config_dir = home / ".config" / "opencode"
opencode_config_dir.mkdir(parents=True, exist_ok=True)

# Build the MCP server dict once, honouring enterprise overrides — empty
# DEEPWIKI_MCP_URL / EXA_MCP_URL drops the corresponding server (F-04).
from enterprise_config import deepwiki_mcp_url, exa_mcp_url

_mcp_servers = {}
if dw_url := deepwiki_mcp_url():
    _mcp_servers["deepwiki"] = {
        "type": "remote",
        "url": dw_url,
        "enabled": True,
        "oauth": False,
    }
if exa_url := exa_mcp_url():
    _mcp_servers["exa"] = {
        "type": "remote",
        "url": exa_url,
        "enabled": True,
    }

if gateway_host:
    # Gateway mode: route through content-filter proxy proxy for content block sanitization
    # content-filter proxy forwards clean requests to Databricks AI Gateway
    # OpenAI/GPT models go direct (not affected by the empty content block bug)
    opencode_config = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "databricks": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Databricks AI Gateway (via content-filter proxy)",
                "options": {
                    "baseURL": CONTENT_FILTER_PROXY_URL,
                    "apiKey": "{env:DATABRICKS_TOKEN}"
                },
                "models": _databricks_models
            },
            "databricks-openai": {
                "npm": "@ai-sdk/openai",
                "name": "Databricks AI Gateway (OpenAI)",
                "options": {
                    "baseURL": f"{gateway_host}/openai/v1",
                    "apiKey": "{env:DATABRICKS_TOKEN}",
                    "compatibility": "compatible"
                },
                "models": _databricks_openai_models
            }
        },
        "mcp": _mcp_servers,
        "model": f"databricks/{active_model}"
    }
else:
    # Fallback: route through content-filter proxy proxy for content block sanitization
    # content-filter proxy forwards clean requests to Databricks serving endpoints
    opencode_config = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "databricks": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Databricks Model Serving (via content-filter proxy)",
                "options": {
                    "baseURL": CONTENT_FILTER_PROXY_URL,
                    "apiKey": "{env:DATABRICKS_TOKEN}"
                },
                "models": _databricks_models
            }
        },
        "mcp": _mcp_servers,
        "model": f"databricks/{active_model}"
    }

config_path = opencode_config_dir / "opencode.json"
config_path.write_text(json.dumps(opencode_config, indent=2))
print(f"OpenCode configured: {config_path}")

# 4. Also create auth credentials for the databricks provider(s)
# OpenCode stores credentials at ~/.local/share/opencode/auth.json
opencode_data_dir = home / ".local" / "share" / "opencode"
opencode_data_dir.mkdir(parents=True, exist_ok=True)

if gateway_host:
    auth_data = {
        "databricks": {
            "api_key": gateway_token
        },
        "databricks-openai": {
            "api_key": gateway_token
        }
    }
else:
    auth_data = {
        "databricks": {
            "api_key": token
        }
    }

auth_path = opencode_data_dir / "auth.json"
auth_path.write_text(json.dumps(auth_data, indent=2))
auth_path.chmod(0o600)
print(f"OpenCode auth configured: {auth_path}")

print(f"\nOpenCode ready! Default model: {active_model}")
print("  opencode                          # Start OpenCode TUI")
if gateway_host and _databricks_openai_models:
    _gpt = sorted(_databricks_openai_models.keys())[0]
    print(f"  opencode -m databricks-openai/{_gpt}  # Use a GPT model")
_served_names = sorted(_databricks_models.keys())
if _served_names:
    print(f"  opencode -m databricks/{_served_names[0]}  # first served model")
print(f"  opencode -m databricks/{active_model} # default")
