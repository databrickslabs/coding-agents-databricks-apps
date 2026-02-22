import os
import json
import subprocess
from pathlib import Path

# Set HOME if not properly set
if not os.environ.get("HOME") or os.environ["HOME"] == "/":
    os.environ["HOME"] = "/app/python/source_code"

home = Path(os.environ["HOME"])

# Create ~/.claude directory
claude_dir = home / ".claude"
claude_dir.mkdir(exist_ok=True)

# 1. Write settings.json for Databricks model serving
# Use DATABRICKS_GATEWAY_HOST if available (new AI Gateway), otherwise fall back to DATABRICKS_HOST
gateway_host = os.environ.get("DATABRICKS_GATEWAY_HOST", "").rstrip("/")
databricks_host = os.environ.get("DATABRICKS_HOST", "").rstrip("/")

if gateway_host:
    gateway_token = os.environ.get("DATABRICKS_GATEWAY_TOKEN", "")
    if not gateway_token:
        print("Warning: DATABRICKS_GATEWAY_HOST set but DATABRICKS_GATEWAY_TOKEN missing, falling back to DATABRICKS_HOST")
        gateway_host = ""

if gateway_host:
    anthropic_base_url = f"{gateway_host}/anthropic"
    auth_token = gateway_token
    print(f"Using Databricks AI Gateway: {gateway_host}")
else:
    anthropic_base_url = f"{databricks_host}/serving-endpoints/anthropic"
    auth_token = os.environ["DATABRICKS_TOKEN"]
    print(f"Using Databricks Host: {databricks_host}")

settings = {
    "env": {
        "ANTHROPIC_MODEL": os.environ.get("ANTHROPIC_MODEL", "databricks-claude-sonnet-4-6"),
        "ANTHROPIC_BASE_URL": anthropic_base_url,
        "ANTHROPIC_AUTH_TOKEN": auth_token,
        "ANTHROPIC_CUSTOM_HEADERS": "x-databricks-use-coding-agent-mode: true"
    }
}

settings_path = claude_dir / "settings.json"
settings_path.write_text(json.dumps(settings, indent=2))

# 2. Write ~/.claude.json with onboarding skip AND MCP servers
claude_json = {
    "hasCompletedOnboarding": True,
    "mcpServers": {
        "deepwiki": {
            "type": "http",
            "url": "https://mcp.deepwiki.com/mcp"
        },
        "exa": {
            "type": "http",
            "url": "https://mcp.exa.ai/mcp"
        }
    }
}

claude_json_path = home / ".claude.json"
claude_json_path.write_text(json.dumps(claude_json, indent=2))

print(f"Claude configured: {settings_path}")
print(f"Onboarding skipped + MCPs configured: {claude_json_path}")

# 3. Install Claude Code CLI if not present
local_bin = home / ".local" / "bin"
claude_bin = local_bin / "claude"

if not claude_bin.exists():
    print("Installing Claude Code CLI...")
    result = subprocess.run(
        ["bash", "-c", "curl -fsSL https://claude.ai/install.sh | bash"],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True
    )
    if result.returncode == 0:
        print("Claude Code CLI installed successfully")
    else:
        print(f"CLI install warning: {result.stderr}")
else:
    print(f"Claude Code CLI already installed at {claude_bin}")

# 4. Create projects directory
projects_dir = home / "projects"
projects_dir.mkdir(exist_ok=True)
print(f"Projects directory: {projects_dir}")

# 5. Git identity and hooks are now configured by app.py's _setup_git_config()
# (runs directly in Python before setup_claude.py, writes ~/.gitconfig and ~/.githooks/)
print("Git identity and hooks: configured by app.py (skipping here)")
