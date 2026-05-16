import os
import json
import shutil
import subprocess
from pathlib import Path

from utils import discover_serving_endpoints, ensure_https, get_gateway_host, pick_in_geo_model

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

    settings_path = claude_dir / "settings.json"

    # Read-merge-write to preserve env vars from other setup scripts (e.g. setup_mlflow.py)
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, OSError):
            settings = {}
    else:
        settings = {}

    # Discover models actually served at this workspace. The direct serving-
    # endpoints list reflects Databricks Geo Designated Services policy — a
    # workspace in AU only sees in-geo models, etc. Validating env-set defaults
    # against this list avoids configuring Claude Code with a model the gateway
    # claims to serve but the user's geo can't access.
    available = discover_serving_endpoints(databricks_host, token)
    if available:
        print(f"Discovered {len(available)} READY serving endpoints at workspace")

    requested_model = os.environ.get("ANTHROPIC_MODEL", "databricks-claude-opus-4-7")
    active_model = pick_in_geo_model(
        [requested_model, "databricks-claude-opus-4-6", "databricks-claude-sonnet-4-6"],
        available,
        fallback=requested_model,
    )
    opus_model = pick_in_geo_model(
        ["databricks-claude-opus-4-7", "databricks-claude-opus-4-6"],
        available,
        fallback="databricks-claude-opus-4-7",
    )
    sonnet_model = pick_in_geo_model(
        ["databricks-claude-sonnet-4-6", "databricks-claude-sonnet-4-5"],
        available,
        fallback="databricks-claude-sonnet-4-6",
    )
    haiku_model = pick_in_geo_model(
        ["databricks-claude-haiku-4-5"],
        available,
        fallback="databricks-claude-haiku-4-5",
    )
    if available and active_model != requested_model:
        print(f"ANTHROPIC_MODEL={requested_model} not served at this workspace, using {active_model}")

    settings.setdefault("env", {})
    settings["env"]["ANTHROPIC_MODEL"] = active_model
    settings["env"]["ANTHROPIC_BASE_URL"] = anthropic_base_url
    settings["env"]["ANTHROPIC_AUTH_TOKEN"] = token
    settings["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] = opus_model
    settings["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] = sonnet_model
    settings["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = haiku_model
    settings["env"]["ANTHROPIC_CUSTOM_HEADERS"] = "x-databricks-use-coding-agent-mode: true"
    settings["env"]["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"

    settings_path.write_text(json.dumps(settings, indent=2))
    print(f"Claude configured: {settings_path}")
else:
    print("No DATABRICKS_TOKEN — skipping settings.json (will be configured after PAT setup)")

# 2. Write ~/.claude.json with onboarding skip AND MCP servers
# Honour DEEPWIKI_MCP_URL / EXA_MCP_URL from enterprise_config — operators in
# locked-down envs can set these to empty string to omit the public MCP
# servers entirely. Default behaviour (no env vars) remains unchanged.
from enterprise_config import deepwiki_mcp_url, exa_mcp_url

mcp_servers = {}
if dw_url := deepwiki_mcp_url():
    mcp_servers["deepwiki"] = {"type": "http", "url": dw_url}
if exa_url := exa_mcp_url():
    mcp_servers["exa"] = {"type": "http", "url": exa_url}

# Auto-configure team-memory MCP if URL is provided
team_memory_url = os.environ.get("TEAM_MEMORY_MCP_URL", "").strip().rstrip("/")
if team_memory_url:
    mcp_servers["team-memory"] = {
        "type": "http",
        "url": f"{team_memory_url}/mcp"
    }
    print(f"Team memory MCP configured: {team_memory_url}/mcp")

# Read-merge-write rather than overwrite — preserves any keys the user (or
# claude itself) wrote into ~/.claude.json between setups (F-09).
claude_json_path = home / ".claude.json"
if claude_json_path.exists():
    try:
        existing = json.loads(claude_json_path.read_text())
    except (json.JSONDecodeError, OSError):
        existing = {}
else:
    existing = {}
existing["hasCompletedOnboarding"] = True
existing["mcpServers"] = mcp_servers  # ours wins — these are the agent CLIs we manage
claude_json_path.write_text(json.dumps(existing, indent=2))

print(f"Onboarding skipped + MCPs configured ({len(mcp_servers)} servers): {claude_json_path}")

# 3. Install Claude Code CLI if not present
local_bin = home / ".local" / "bin"
claude_bin = local_bin / "claude"

# Honour CLAUDE_INSTALLER_URL for enterprise environments where claude.ai is
# firewalled — defaults to the public installer when unset. The URL is
# validated by enterprise_config to reject shell metacharacters before it
# reaches subprocess. Additionally, we avoid embedding the URL in a shell
# string by piping curl's output into bash via positional args — even if a
# malicious URL somehow slipped through validation, it would land as a curl
# argument, not as shell.
from enterprise_config import claude_installer_url

installer_url = claude_installer_url()
print(f"Installing/upgrading Claude Code CLI from {installer_url}...")
curl_proc = subprocess.Popen(
    ["curl", "-fsSL", installer_url],
    stdout=subprocess.PIPE,
    env={**os.environ, "HOME": str(home)},
)
result = subprocess.run(
    ["bash"],
    stdin=curl_proc.stdout,
    env={**os.environ, "HOME": str(home)},
    capture_output=True,
    text=True,
)
curl_proc.stdout.close()
curl_proc.wait()
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
