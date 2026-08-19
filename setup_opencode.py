#!/usr/bin/env python
"""Configure OpenCode for workspace AI Gateway v2 provider routes.

The provider URL and model bucket contract is ported from current ucode. The
OpenCode OSS/chat-completions provider stays behind the loopback content-filter
proxy; that proxy forwards only to ``DATABRICKS_HOST/ai-gateway/mlflow/v1``.
No route uses the legacy external ``*.ai-gateway.*`` hostname.
"""
import json
import os
import subprocess
import time
from pathlib import Path

from enterprise_config import deepwiki_mcp_url, exa_mcp_url, npm_env
from cli_auth import _atomic_write_text
from gateway_models import (
    claude_model_capabilities,
    discover_model_catalog,
    opencode_base_urls,
    preferred_model,
)
from token_helper import resolve_databricks_token
from utils import (
    get_npm_version,
    opencode_api_credential,
)

if os.environ.get("ENABLE_OPENCODE", "true").strip().lower() in ("false", "0", "no"):
    print("ENABLE_OPENCODE=false — skipping OpenCode CLI setup")
    raise SystemExit(0)

CONTENT_FILTER_PROXY_URL = "http://127.0.0.1:4000"
if not os.environ.get("HOME") or os.environ["HOME"] == "/":
    os.environ["HOME"] = "/app/python/source_code"
home = Path(os.environ["HOME"])
host = os.environ.get("DATABRICKS_HOST", "")
token = resolve_databricks_token() or ""
requested_model = os.environ.get("ANTHROPIC_MODEL", "system.ai.claude-sonnet-5")

# Install OpenCode and the OpenAI SDK used by Responses/MLflow providers.
local_bin = home / ".local" / "bin"
local_bin.mkdir(parents=True, exist_ok=True)
opencode_bin = local_bin / "opencode"
if not opencode_bin.exists():
    npm_prefix = str(home / ".local")
    version = get_npm_version("opencode-ai")
    package = f"opencode-ai@{version}" if version else "opencode-ai@latest"
    print(f"Installing {package}")
    for attempt in range(1, 4):
        result = subprocess.run(
            ["npm", "install", "-g", f"--prefix={npm_prefix}", package],
            capture_output=True,
            text=True,
            env={**os.environ, "HOME": str(home), **npm_env()},
        )
        if result.returncode == 0 and opencode_bin.exists():
            break
        print(f"OpenCode install failed (attempt {attempt}/3, rc={result.returncode})")
        if attempt < 3:
            time.sleep(5)
    sdk_version = get_npm_version("@ai-sdk/openai")
    sdk_package = f"@ai-sdk/openai@{sdk_version}" if sdk_version else "@ai-sdk/openai"
    subprocess.run(
        ["npm", "install", "-g", f"--prefix={npm_prefix}", sdk_package],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home), **npm_env()},
    )
else:
    print(f"OpenCode CLI already installed at {opencode_bin}")

if not host or not token:
    print("OpenCode CLI installed — config will be set after auth is available")
    raise SystemExit(0)

base_urls = opencode_base_urls(host)
catalog = discover_model_catalog(host, token)
anthropic_models = catalog["anthropic"] or [requested_model]
active_model = preferred_model(requested_model, anthropic_models)
print(
    "Using workspace AI Gateway model-services: "
    f"claude={len(catalog['anthropic'])}, openai={len(catalog['openai'])}, "
    f"gemini={len(catalog['gemini'])}, oss={len(catalog['oss'])}"
)

anthropic_specs = {spec["id"]: spec for spec in catalog.get("anthropic_specs") or []}

# Mirrors ucode's opencode overlay.
#
# Auth: `@ai-sdk/anthropic` sends the key as `x-api-key` (Anthropic native), and
# the workspace AI Gateway only accepts `Authorization: Bearer` — it answers an
# x-api-key request with 401 "Credential was not sent or was of an unsupported
# type for this API" (verified: Bearer -> 400 body validation, x-api-key -> 401).
# So the bearer is supplied explicitly. cli_auth rotates this alongside
# auth.json so the two cannot drift.
#
# User-Agent has to live on each model entry: OpenCode injects its own UA after
# the AI SDK merges provider headers, clobbering provider-level `headers`, while
# per-model headers are merged afterwards and win.
#
# `toolStreaming: False` is per-model too (opencode reads per-call
# providerOptions from `models.<m>.options`): @ai-sdk/anthropic sets
# `eager_input_streaming: true` on tool definitions and the gateway's strict
# validator rejects it. opencode's own auto-disable skips ids containing
# "claude", which these `system.ai.claude-*` ids do not match on its check.
auth_headers = {"Authorization": f"Bearer {token}"}
ua_header = {"User-Agent": "coda/opencode"}


def _anthropic_model_overlay(model: str) -> dict:
    spec = anthropic_specs.get(model) or claude_model_capabilities(model)
    return {
        "headers": ua_header,
        "options": {"toolStreaming": False},
        "limit": {
            "context": spec.get("context_window", 200_000),
            "output": spec.get("max_tokens", 64_000),
        },
    }


providers = {
    "databricks-anthropic": {
        "npm": "@ai-sdk/anthropic",
        "name": "Databricks Anthropic Gateway",
        "options": {
            # Route Anthropic messages through the local proxy so it resolves
            # a fresh broker/PAT credential per request. The proxy maps
            # /v1/messages back to the workspace Anthropic gateway and strips
            # the SDK's x-api-key header in favour of Authorization.
            "baseURL": f"{CONTENT_FILTER_PROXY_URL}/v1",
            "apiKey": token,
            "headers": auth_headers,
        },
        "models": {model: _anthropic_model_overlay(model) for model in anthropic_models},
    }
}
if catalog["openai"]:
    providers["databricks-openai"] = {
        "npm": "@ai-sdk/openai",
        "name": "Databricks Responses Gateway",
        "options": {
            "baseURL": base_urls["openai"],
            "apiKey": token,
        },
        "models": {
            model: {
                "limit": {"context": 272_000, "output": 128_000},
                "options": {"useResponsesApi": True},
            }
            for model in catalog["openai"]
        },
    }
if catalog["gemini"]:
    providers["databricks-google"] = {
        "npm": "@ai-sdk/google",
        "name": "Databricks Gemini Gateway",
        "options": {
            "baseURL": base_urls["gemini"],
            "apiKey": token,
        },
        "models": {model: {} for model in catalog["gemini"]},
    }
if catalog["oss"]:
    specs = {spec["id"]: spec for spec in catalog["oss_specs"]}
    providers["databricks-oss"] = {
        "npm": "@ai-sdk/openai",
        "name": "Databricks MLflow Gateway (filtered)",
        "options": {
            "baseURL": CONTENT_FILTER_PROXY_URL,
            "apiKey": token,
        },
        "models": {
            model: {
                "limit": {
                    "context": specs.get(model, {}).get("context_window") or 128_000,
                    "output": specs.get(model, {}).get("max_tokens") or 8_192,
                },
                **(
                    {"reasoning": True}
                    if specs.get(model, {}).get("reasoning") is True
                    else {}
                ),
            }
            for model in catalog["oss"]
        },
    }

mcp = {}
if url := deepwiki_mcp_url():
    mcp["deepwiki"] = {"type": "remote", "url": url, "enabled": True, "oauth": False}
if url := exa_mcp_url():
    mcp["exa"] = {"type": "remote", "url": url, "enabled": True}

config = {
    "$schema": "https://opencode.ai/config.json",
    "provider": providers,
    "mcp": mcp,
    "model": f"databricks-anthropic/{active_model}",
}
config_dir = home / ".config" / "opencode"
config_dir.mkdir(parents=True, exist_ok=True)
config_path = config_dir / "opencode.json"
_atomic_write_text(str(config_path), json.dumps(config, indent=2))

# Auth keys match provider names; cli_auth rotates every API credential entry.
auth_dir = home / ".local" / "share" / "opencode"
auth_dir.mkdir(parents=True, exist_ok=True)
auth_path = auth_dir / "auth.json"
_atomic_write_text(
    str(auth_path),
    json.dumps(
        {name: opencode_api_credential(token) for name in providers},
        indent=2,
    )
)
print(f"OpenCode configured: {config_path}")
print(f"OpenCode default: databricks-anthropic/{active_model}")
