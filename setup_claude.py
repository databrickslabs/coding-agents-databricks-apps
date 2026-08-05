import os
import sys
import json
import shutil
import subprocess
from pathlib import Path

from claude_otel import apply_claude_otel_env
from token_helper import resolve_databricks_token
from utils import (
    add_1m_context_suffix,
    discover_serving_endpoints,
    ensure_https,
    get_gateway_host,
    pick_in_geo_model,
)

# Set HOME if not properly set
if not os.environ.get("HOME") or os.environ["HOME"] == "/":
    os.environ["HOME"] = "/app/python/source_code"

home = Path(os.environ["HOME"])

# The SP OAuth profile the Omnigent host writes (auth_type=oauth-m2m). The
# apiKeyHelper prefers it so model calls can use the app service principal and
# the workshop needs no per-attendee PAT. Kept in sync with
# omnigents_host._HOST_PROFILE.
_SP_PROFILE = "omnigents-host"


def _write_apikey_helper(claude_dir: Path) -> Path:
    """Write the token helper Claude Code calls per-TTL (spec C).

    Thin wrapper over the shared ``token_helper.write_token_helper`` so Claude
    and Pi resolve model auth through the exact same script (SP OAuth from the
    omnigents-host profile, else the PAT). Claude Code reads the helper's stdout
    verbatim as the bearer token.
    """
    from token_helper import write_token_helper
    return write_token_helper(claude_dir)

# Create ~/.claude directory
claude_dir = home / ".claude"
claude_dir.mkdir(exist_ok=True)

# 1. Write settings.json for Databricks model serving. The SP broker is the
# primary auth source on the no-PAT baseline; checking only the raw
# DATABRICKS_TOKEN env var made Claude setup silently skip its config even while
# brokered SP auth was healthy. Resolve through the same layered source as Pi and
# OpenCode: SP broker/profile, then user PAT.
token = resolve_databricks_token() or ""
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

    requested_model = os.environ.get("ANTHROPIC_MODEL", "databricks-claude-opus-4-8")
    active_model = pick_in_geo_model(
        [requested_model, "databricks-claude-opus-4-7", "databricks-claude-opus-4-6", "databricks-claude-sonnet-4-6"],
        available,
        fallback=requested_model,
    )
    opus_model = pick_in_geo_model(
        ["databricks-claude-opus-4-8", "databricks-claude-opus-4-7", "databricks-claude-opus-4-6"],
        available,
        fallback="databricks-claude-opus-4-8",
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

    # Token source (spec C): by default install an apiKeyHelper that fetches a
    # fresh token per-TTL -- Claude Code re-runs it on the interval below, so
    # nothing has to rotate a static token into this file. This is the path
    # that survives PAT rotation: a static ANTHROPIC_AUTH_TOKEN is cached by
    # Claude Code at launch and dies when the rotator revokes the old PAT,
    # whereas the helper pulls a live token each TTL. The helper falls back to
    # the PAT (from $DATABRICKS_TOKEN, else ~/.databrickscfg [DEFAULT]) when no
    # SP OAuth profile is present, so the standard per-user deploy is
    # unaffected. Set DISABLE_SP_APIKEYHELPER=true to force the legacy
    # static-token path (fragile across rotation -- escape hatch only).
    _disable_helper = os.environ.get("DISABLE_SP_APIKEYHELPER", "").strip().lower() in ("true", "1", "yes")
    if not _disable_helper:
        helper_path = _write_apikey_helper(claude_dir)
        # apiKeyHelper is a shell command; invoke it with the app's own venv
        # interpreter (dependency-complete, has databricks-sdk) so the helper
        # never has to re-exec under `uv run` to import the SDK. Fall back to a
        # bare python3 only if the venv interpreter is unknown.
        helper_python = os.environ.get("CODA_VENV_PYTHON") or sys.executable or "python3"
        settings["apiKeyHelper"] = f"{helper_python} {helper_path}"
        # SP OAuth tokens are short-lived (~1h); re-run the helper well under
        # that. Matches Omnigent's native-claude default.
        settings["env"]["CLAUDE_CODE_API_KEY_HELPER_TTL_MS"] = "900000"
        # Do not pin a static token — the helper is authoritative.
        settings["env"].pop("ANTHROPIC_AUTH_TOKEN", None)
        print(f"Claude apiKeyHelper installed: {helper_path}")
    else:
        settings["env"]["ANTHROPIC_AUTH_TOKEN"] = token
    # Suffix opus/sonnet with [1m] so Claude Code requests the 1M context window
    # via the gateway (see utils.add_1m_context_suffix). Haiku stays 200K-native.
    settings["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] = add_1m_context_suffix(opus_model)
    settings["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] = add_1m_context_suffix(sonnet_model)
    settings["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = haiku_model
    settings["env"]["ANTHROPIC_CUSTOM_HEADERS"] = "x-databricks-use-coding-agent-mode: true"
    settings["env"]["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"
    if apply_claude_otel_env(settings, token, databricks_host):
        print("Claude Code OTEL export enabled")

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

if os.environ.get("CODA_SKIP_CLAUDE_INSTALL", "").lower() == "true":
    print("Claude Code CLI install skipped")
elif os.environ.get("CLAUDE_INSTALL_METHOD", "").strip().lower() == "npm":
    # npm install path for firewalled networks where the claude.ai installer
    # host (or the CDN its install.sh pulls from) is blocked but the npm
    # registry is reachable. @anthropic-ai/claude-code is the same CLI as the
    # curl installer produces. Mirrors setup_pi.py's hardened pattern:
    # version cooldown (get_npm_version), NPM_REGISTRY override (npm_env),
    # retries, and loud stderr. Unlike setup_pi.py we do NOT pass
    # --ignore-scripts: Claude Code's postinstall (node install.cjs) is what
    # places the native binary, so skipping scripts yields no working `claude`.
    from utils import get_npm_version
    from enterprise_config import npm_env

    CLAUDE_PACKAGE = "@anthropic-ai/claude-code"
    npm_prefix = str(home / ".local")
    claude_version = get_npm_version(CLAUDE_PACKAGE)
    claude_pkg = (
        f"{CLAUDE_PACKAGE}@{claude_version}" if claude_version
        else f"{CLAUDE_PACKAGE}@latest"
    )

    MAX_RETRIES = 3
    RETRY_DELAY = 5  # seconds
    for attempt in range(1, MAX_RETRIES + 1):
        print(f"Installing {claude_pkg} via npm (attempt {attempt}/{MAX_RETRIES})...")
        result = subprocess.run(
            ["npm", "install", "-g", f"--prefix={npm_prefix}", claude_pkg],
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(home), **npm_env()},
        )
        if result.returncode == 0 and claude_bin.exists():
            print(f"Claude Code CLI installed to {claude_bin}")
            break
        else:
            stderr = result.stderr.strip()
            print(f"Claude Code npm install failed (attempt {attempt}/{MAX_RETRIES}, rc={result.returncode})")
            if stderr:
                print(f"  stderr: {stderr[:500]}")
            if result.stdout.strip():
                print(f"  stdout: {result.stdout.strip()[:500]}")
            if attempt < MAX_RETRIES:
                import time
                print(f"  Retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"ERROR: Claude Code npm install failed after {MAX_RETRIES} attempts. "
                      f"Run manually: npm install -g --prefix=$HOME/.local {CLAUDE_PACKAGE}")
else:
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
