import os
import json
import shutil
import subprocess
from pathlib import Path

from utils import ensure_https, get_gateway_host

# Set HOME if not properly set
if not os.environ.get("HOME") or os.environ["HOME"] == "/":
    os.environ["HOME"] = "/app/python/source_code"

home = Path(os.environ["HOME"])

# Create ~/.claude directory
claude_dir = home / ".claude"
claude_dir.mkdir(exist_ok=True)

# 1. Write settings.json for Databricks model serving (requires DATABRICKS_TOKEN)
token = os.environ.get("DATABRICKS_TOKEN", "").strip()
if token:
    gateway_host = get_gateway_host()
    databricks_host = ensure_https(os.environ.get("DATABRICKS_HOST", "").rstrip("/"))

    if gateway_host:
        anthropic_base_url = f"{gateway_host}/anthropic"
        print(f"Using Databricks AI Gateway: {gateway_host}")
    else:
        anthropic_base_url = f"{databricks_host}/serving-endpoints/anthropic"
        print(f"Using Databricks Host: {databricks_host}")

    settings = {
        "env": {
            "ANTHROPIC_MODEL": os.environ.get("ANTHROPIC_MODEL", "databricks-claude-opus-4-6"),
            "ANTHROPIC_BASE_URL": anthropic_base_url,
            "ANTHROPIC_AUTH_TOKEN": token,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-4-6",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "databricks-claude-sonnet-4-6",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "databricks-claude-haiku-4-5",
            "ANTHROPIC_CUSTOM_HEADERS": "x-databricks-use-coding-agent-mode: true",
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
        }
    }

    settings_path = claude_dir / "settings.json"
    settings_path.write_text(json.dumps(settings, indent=2))
    print(f"Claude configured: {settings_path}")
else:
    print("No DATABRICKS_TOKEN — skipping settings.json (will be configured after PAT setup)")

# 2. Write ~/.claude.json with onboarding skip AND MCP servers
mcp_servers = {
    "deepwiki": {
        "type": "http",
        "url": "https://mcp.deepwiki.com/mcp"
    },
    "exa": {
        "type": "http",
        "url": "https://mcp.exa.ai/mcp"
    }
}

# Auto-configure team-memory MCP if URL is provided
team_memory_url = os.environ.get("TEAM_MEMORY_MCP_URL", "").strip().rstrip("/")
if team_memory_url:
    mcp_servers["team-memory"] = {
        "type": "http",
        "url": f"{team_memory_url}/mcp"
    }
    print(f"Team memory MCP configured: {team_memory_url}/mcp")

claude_json = {
    "hasCompletedOnboarding": True,
    "mcpServers": mcp_servers
}

claude_json_path = home / ".claude.json"
claude_json_path.write_text(json.dumps(claude_json, indent=2))

print(f"Onboarding skipped + MCPs configured: {claude_json_path}")

# 3. Install Claude Code CLI if not present
local_bin = home / ".local" / "bin"
claude_bin = local_bin / "claude"

print("Installing/upgrading Claude Code CLI...")
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

# 4. Copy subagent definitions to ~/.claude/agents/
# These enable TDD workflow: prd-writer → test-generator → implementer → build-feature
agents_src = Path(__file__).parent / "agents"
agents_dst = claude_dir / "agents"
agents_dst.mkdir(exist_ok=True)

if agents_src.exists():
    copied = []
    for agent_file in agents_src.glob("*.md"):
        shutil.copy2(str(agent_file), str(agents_dst / agent_file.name))
        copied.append(agent_file.name)
    if copied:
        print(f"Subagents installed: {', '.join(copied)}")
else:
    print("No agents directory found, skipping subagent setup")

# 5. Create projects directory
projects_dir = home / "projects"
projects_dir.mkdir(exist_ok=True)
print(f"Projects directory: {projects_dir}")

# 5. Git identity and hooks are now configured by app.py's _setup_git_config()
# (runs directly in Python before setup_claude.py, writes ~/.gitconfig and ~/.githooks/)
print("Git identity and hooks: configured by app.py (skipping here)")
