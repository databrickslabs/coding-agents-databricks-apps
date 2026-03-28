#!/usr/bin/env python
"""Configure OpenCode CLI with Databricks Model Serving (via content-filter proxy) local proxy.

Routes requests through a local content-filter proxy proxy (localhost:4000) which sanitizes empty
text content blocks before forwarding to Databricks AI Gateway. This fixes OpenCode
issue #5028 where empty content blocks cause "Bad Request" errors.
See docs/plans/2026-03-11-litellm-empty-content-blocks-design.md for details.
"""
import os
import json
import subprocess
from pathlib import Path

from utils import ensure_https, get_npm_version

# content-filter proxy local proxy — sanitizes empty content blocks before reaching Databricks
# (see https://github.com/sst/opencode/issues/5028)
CONTENT_FILTER_PROXY_URL = "http://127.0.0.1:4000"

# Set HOME if not properly set
if not os.environ.get("HOME") or os.environ["HOME"] == "/":
    os.environ["HOME"] = "/app/python/source_code"

home = Path(os.environ["HOME"])

host = os.environ.get("DATABRICKS_HOST", "")
token = os.environ.get("DATABRICKS_TOKEN", "")
anthropic_model = os.environ.get("ANTHROPIC_MODEL", "databricks-claude-sonnet-4-6")

# 1. Install OpenCode CLI into ~/.local/bin (always, even without token)
local_bin = home / ".local" / "bin"
local_bin.mkdir(parents=True, exist_ok=True)
opencode_bin = local_bin / "opencode"

if not opencode_bin.exists():
    # Use --prefix ~/.local so npm installs directly into ~/.local/bin (avoids EACCES on /usr/local)
    npm_prefix = str(home / ".local")

    # Resolve exact versions to avoid mutable @latest tags (supply chain hardening)
    oc_version = get_npm_version("opencode-ai")
    oc_pkg = f"opencode-ai@{oc_version}" if oc_version else "opencode-ai@latest"
    print(f"Installing {oc_pkg}...")
    result = subprocess.run(
        ["npm", "install", "-g", f"--prefix={npm_prefix}", oc_pkg],
        capture_output=True, text=True,
        env={**os.environ, "HOME": str(home)}
    )
    if result.returncode == 0:
        print(f"OpenCode CLI installed to {opencode_bin}")
    else:
        print(f"OpenCode install warning: {result.stderr}")

    # Install @ai-sdk/openai for GPT models (Responses API support)
    sdk_version = get_npm_version("@ai-sdk/openai")
    sdk_pkg = f"@ai-sdk/openai@{sdk_version}" if sdk_version else "@ai-sdk/openai"
    print(f"Installing {sdk_pkg}...")
    result = subprocess.run(
        ["npm", "install", "-g", f"--prefix={npm_prefix}", sdk_pkg],
        capture_output=True, text=True,
        env={**os.environ, "HOME": str(home)}
    )
    if result.returncode == 0:
        print(f"@ai-sdk/openai@{sdk_version or 'latest'} installed (Responses API support)")
    else:
        print(f"@ai-sdk/openai install warning: {result.stderr}")
else:
    print(f"OpenCode CLI already installed at {opencode_bin}")

# 2. Skip auth config if no token (will be configured after PAT setup)
if not host or not token:
    print("OpenCode CLI installed — config will be set after PAT setup")
    exit(0)

# Strip trailing slash and ensure https:// prefix
host = ensure_https(host.rstrip("/"))

# Use DATABRICKS_GATEWAY_HOST if available (new AI Gateway), otherwise fall back to current gateway (DATABRICKS_HOST)
gateway_host = ensure_https(os.environ.get("DATABRICKS_GATEWAY_HOST", "").rstrip("/"))
gateway_token = os.environ.get("DATABRICKS_TOKEN", "") if gateway_host else ""
if gateway_host and not gateway_token:
    print("Warning: DATABRICKS_GATEWAY_HOST set but DATABRICKS_TOKEN missing, falling back to DATABRICKS_HOST")
    gateway_host = ""

if gateway_host:
    print(f"Using Databricks AI Gateway: {gateway_host}")
else:
    print(f"Using Databricks Host: {host}")

# 3. Write global opencode.json config
# OpenCode looks for config at ~/.config/opencode/opencode.json (global)
# and ./opencode.json (project-level)
opencode_config_dir = home / ".config" / "opencode"
opencode_config_dir.mkdir(parents=True, exist_ok=True)

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
                "models": {
                    "databricks-claude-opus-4-6": {
                        "name": "Claude Opus 4.6 (Databricks)",
                        "limit": {
                            "context": 200000,
                            "output": 16384
                        }
                    },
                    "databricks-claude-sonnet-4-6": {
                        "name": "Claude Sonnet 4.6 (Databricks)",
                        "limit": {
                            "context": 200000,
                            "output": 8192
                        }
                    },
                    "databricks-gemini-2-5-flash": {
                        "name": "Gemini 2.5 Flash (Databricks)",
                        "limit": {
                            "context": 1000000,
                            "output": 8192
                        }
                    },
                    "databricks-gemini-2-5-pro": {
                        "name": "Gemini 2.5 Pro (Databricks)",
                        "limit": {
                            "context": 1000000,
                            "output": 8192
                        }
                    },
                    "databricks-gemini-3-1-pro": {
                        "name": "Gemini 3.1 Pro (Databricks)",
                        "limit": {
                            "context": 1000000,
                            "output": 8192
                        }
                    },
                }
            },
            "databricks-openai": {
                "npm": "@ai-sdk/openai",
                "name": "Databricks AI Gateway (OpenAI)",
                "options": {
                    "baseURL": f"{gateway_host}/openai/v1",
                    "apiKey": "{env:DATABRICKS_TOKEN}",
                    "compatibility": "compatible"
                },
                "models": {
                    "databricks-gpt-5-2-codex": {
                        "name": "GPT 5.2 Codex (Databricks)",
                        "limit": {
                            "context": 200000,
                            "output": 16384
                        }
                    },
                    "databricks-gpt-5-1-codex-max": {
                        "name": "GPT 5.1 Codex Max (Databricks)",
                        "limit": {
                            "context": 200000,
                            "output": 16384
                        }
                    }
                }
            }
        },
        "mcp": {
            "deepwiki": {
                "type": "remote",
                "url": "https://mcp.deepwiki.com/mcp",
                "enabled": True,
                "oauth": False
            },
            "exa": {
                "type": "remote",
                "url": "https://mcp.exa.ai/mcp",
                "enabled": True
            }
        },
        "model": f"databricks/{anthropic_model}"
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
                "models": {
                    "databricks-claude-opus-4-6": {
                        "name": "Claude Opus 4.6 (Databricks)",
                        "limit": {
                            "context": 200000,
                            "output": 16384
                        }
                    },
                    "databricks-claude-sonnet-4-6": {
                        "name": "Claude Sonnet 4.6 (Databricks)",
                        "limit": {
                            "context": 200000,
                            "output": 8192
                        }
                    },
                    "databricks-gemini-2-5-flash": {
                        "name": "Gemini 2.5 Flash (Databricks)",
                        "limit": {
                            "context": 1000000,
                            "output": 8192
                        }
                    },
                    "databricks-gemini-2-5-pro": {
                        "name": "Gemini 2.5 Pro (Databricks)",
                        "limit": {
                            "context": 1000000,
                            "output": 8192
                        }
                    },
                    "databricks-gemini-3-1-pro": {
                        "name": "Gemini 3.1 Pro (Databricks)",
                        "limit": {
                            "context": 1000000,
                            "output": 8192
                        }
                    },
                }
            }
        },
        "mcp": {
            "deepwiki": {
                "type": "remote",
                "url": "https://mcp.deepwiki.com/mcp",
                "enabled": True,
                "oauth": False
            },
            "exa": {
                "type": "remote",
                "url": "https://mcp.exa.ai/mcp",
                "enabled": True
            }
        },
        "model": f"databricks/{anthropic_model}"
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

print(f"\nOpenCode ready! Default model: {anthropic_model}")
print("  opencode                          # Start OpenCode TUI")
if gateway_host:
    print("  opencode -m databricks-openai/databricks-gpt-5-2-codex  # Use GPT 5.2 Codex")
print("  opencode -m databricks/databricks-gemini-2-5-flash  # Use Gemini")
print(f"  opencode -m databricks/{anthropic_model} # Use Claude (default)")
