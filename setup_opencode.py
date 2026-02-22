#!/usr/bin/env python
"""Configure OpenCode CLI with Databricks Model Serving as an OpenAI-compatible provider."""
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
anthropic_model = os.environ.get("ANTHROPIC_MODEL", "databricks-claude-sonnet-4-6")

if not host or not token:
    print("Warning: DATABRICKS_HOST or DATABRICKS_TOKEN not set, skipping OpenCode config")
    exit(0)

# Strip trailing slash from host
host = host.rstrip("/")

# Use DATABRICKS_GATEWAY_HOST if available (new AI Gateway), otherwise fall back to current gateway (DATABRICKS_HOST)
gateway_host = os.environ.get("DATABRICKS_GATEWAY_HOST", "").rstrip("/")
gateway_token = os.environ.get("DATABRICKS_GATEWAY_TOKEN", "") if gateway_host else ""
if gateway_host and not gateway_token:
    print("Warning: DATABRICKS_GATEWAY_HOST set but DATABRICKS_GATEWAY_TOKEN missing, falling back to DATABRICKS_HOST")
    gateway_host = ""

if gateway_host:
    print(f"Using Databricks AI Gateway: {gateway_host}")
else:
    print(f"Using Databricks Host: {host}")

# 1. Install OpenCode CLI into ~/.local/bin (same approach as Claude Code)
local_bin = home / ".local" / "bin"
local_bin.mkdir(parents=True, exist_ok=True)
opencode_bin = local_bin / "opencode"

if not opencode_bin.exists():
    print("Installing OpenCode CLI...")
    # Use --prefix ~/.local so npm installs directly into ~/.local/bin (avoids EACCES on /usr/local)
    npm_prefix = str(home / ".local")
    result = subprocess.run(
        ["npm", "install", "-g", f"--prefix={npm_prefix}", "opencode-ai@latest"],
        capture_output=True, text=True,
        env={**os.environ, "HOME": str(home)}
    )
    if result.returncode == 0:
        print(f"OpenCode CLI installed to {opencode_bin}")
    else:
        print(f"OpenCode install warning: {result.stderr}")
else:
    print(f"OpenCode CLI already installed at {opencode_bin}")

# 2. Write global opencode.json config
# OpenCode looks for config at ~/.config/opencode/opencode.json (global)
# and ./opencode.json (project-level)
opencode_config_dir = home / ".config" / "opencode"
opencode_config_dir.mkdir(parents=True, exist_ok=True)

if gateway_host:
    # Gateway mode: separate providers for different API protocols
    # - Anthropic/Gemini models use MLflow endpoint: {gateway}/mlflow/v1/chat/completions
    # - OpenAI/GPT models use OpenAI endpoint: {gateway}/openai/v1/responses
    opencode_config = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "databricks": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Databricks AI Gateway (MLflow)",
                "options": {
                    "baseURL": f"{gateway_host}/mlflow/v1/chat/completions",
                    "apiKey": "{env:DATABRICKS_GATEWAY_TOKEN}"
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
                "npm": "@ai-sdk/openai-compatible",
                "name": "Databricks AI Gateway (OpenAI)",
                "options": {
                    "baseURL": f"{gateway_host}/openai/v1/responses",
                    "apiKey": "{env:DATABRICKS_GATEWAY_TOKEN}"
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
        "model": f"databricks/{anthropic_model}"
    }
else:
    # Fallback: current gateway using DATABRICKS_HOST /serving-endpoints (OpenAI-compatible)
    opencode_config = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "databricks": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Databricks Model Serving",
                "options": {
                    "baseURL": f"{host}/serving-endpoints",
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
        "model": f"databricks/{anthropic_model}"
    }

config_path = opencode_config_dir / "opencode.json"
config_path.write_text(json.dumps(opencode_config, indent=2))
print(f"OpenCode configured: {config_path}")

# 3. Also create auth credentials for the databricks provider(s)
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
