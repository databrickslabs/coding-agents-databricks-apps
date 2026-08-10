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
from gateway_models import discover_model_catalog, opencode_base_urls, preferred_model
from token_helper import resolve_databricks_token
from utils import (
    get_npm_version,
    installed_cli_version,
    opencode_api_credential,
    version_at_least,
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
requested_model = os.environ.get("ANTHROPIC_MODEL", "system.ai.claude-opus-5")

# Install OpenCode and the OpenAI SDK used by Responses/MLflow providers.
local_bin = home / ".local" / "bin"
local_bin.mkdir(parents=True, exist_ok=True)
opencode_bin = local_bin / "opencode"
# Omnigent's opencode-native harness refuses anything below its own floor and
# reports the host as `version-too-low`, which fails session launch. npm's
# `latest` tag for opencode-ai currently points at a `0.0.0-<snapshot>` build,
# which is lower than every real release, and the image may already ship an old
# binary — so check the installed version against the floor instead of only
# checking that the file exists.
OPENCODE_MIN_VERSION = os.environ.get("OPENCODE_MIN_VERSION", "1.17.7").strip() or "1.17.7"
current_version = installed_cli_version(opencode_bin) if opencode_bin.exists() else None
if current_version and version_at_least(current_version, OPENCODE_MIN_VERSION):
    print(f"OpenCode {current_version} already satisfies >= {OPENCODE_MIN_VERSION}")
else:
    if current_version:
        print(
            f"OpenCode {current_version} is below the required "
            f"{OPENCODE_MIN_VERSION}; upgrading"
        )
    npm_prefix = str(home / ".local")
    version = get_npm_version("opencode-ai")
    if version and not version_at_least(version, OPENCODE_MIN_VERSION):
        # A resolved version below the floor is worse than useless: it would
        # reinstall the same unusable harness on every deploy.
        print(
            f"Warning: npm resolved opencode-ai@{version}, below the required "
            f"{OPENCODE_MIN_VERSION}; requesting ^{OPENCODE_MIN_VERSION} instead"
        )
        version = None
    package = f"opencode-ai@{version}" if version else f"opencode-ai@^{OPENCODE_MIN_VERSION}"
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
    installed = installed_cli_version(opencode_bin)
    if not version_at_least(installed, OPENCODE_MIN_VERSION):
        print(
            f"Warning: OpenCode reports {installed!r} after install, still below "
            f"{OPENCODE_MIN_VERSION} — opencode-native sessions will not launch"
        )
    else:
        print(f"OpenCode {installed} installed")
    sdk_version = get_npm_version("@ai-sdk/openai")
    sdk_package = f"@ai-sdk/openai@{sdk_version}" if sdk_version else "@ai-sdk/openai"
    subprocess.run(
        ["npm", "install", "-g", f"--prefix={npm_prefix}", sdk_package],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home), **npm_env()},
    )

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

providers = {
    "databricks-anthropic": {
        "npm": "@ai-sdk/anthropic",
        "name": "Databricks Anthropic Gateway",
        "options": {
            "baseURL": base_urls["anthropic"],
            "apiKey": "{env:DATABRICKS_TOKEN}",
        },
        "models": {
            model: {"options": {"toolStreaming": False}}
            for model in anthropic_models
        },
    }
}
if catalog["openai"]:
    providers["databricks-openai"] = {
        "npm": "@ai-sdk/openai",
        "name": "Databricks Responses Gateway",
        "options": {
            "baseURL": base_urls["openai"],
            "apiKey": "{env:DATABRICKS_TOKEN}",
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
            "apiKey": "{env:DATABRICKS_TOKEN}",
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
            "apiKey": "{env:DATABRICKS_TOKEN}",
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
config_path.write_text(json.dumps(config, indent=2))
config_path.chmod(0o600)

# Auth keys match provider names; cli_auth rotates every API credential entry.
auth_dir = home / ".local" / "share" / "opencode"
auth_dir.mkdir(parents=True, exist_ok=True)
auth_path = auth_dir / "auth.json"
auth_path.write_text(
    json.dumps(
        {name: opencode_api_credential(token) for name in providers},
        indent=2,
    )
)
auth_path.chmod(0o600)
print(f"OpenCode configured: {config_path}")
print(f"OpenCode default: databricks-anthropic/{active_model}")
