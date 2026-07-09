import os
import json
import shutil
import subprocess
from pathlib import Path

from claude_otel import apply_claude_otel_env
from utils import discover_serving_endpoints, ensure_https, get_gateway_host, pick_in_geo_model

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
    """Write the executable token helper Claude Code calls per-TTL (spec C).

    Claude Code runs this and reads *stdout verbatim* as the bearer token, so
    it must print the token and nothing else. Order of preference:
      1. SP OAuth token minted via the Databricks SDK from the omnigents-host
         M2M profile — the workshop/host path (no user PAT). Verified accepted
         by the /anthropic gateway (C-O1, HTTP 200). Uses the SDK's
         Config.authenticate(), NOT `databricks auth token`: the CLI verb is
         U2M-only and hard-refuses M2M (client_id/secret) profiles. Only works
         where the omnigents-host profile exists (host-connected instances).
      2. The PAT from $DATABRICKS_TOKEN, else the `token =` line of the
         [DEFAULT] profile — the standard per-user path.
    All diagnostics go to stderr; stdout carries only the token.
    """
    helper_path = claude_dir / "anthropic-token-helper.py"
    helper_src = '''#!/usr/bin/env python3
"""Print a Databricks bearer token for Claude Code's apiKeyHelper (spec C).

stdout MUST be the token only — Claude Code uses it verbatim.
"""
import configparser
import os
import shutil
import subprocess
import sys

SP_PROFILE = "omnigents-host"
# Set on the uv re-run so the child (which has the SDK) doesn't recurse.
_REEXEC_GUARD = "OMNIGENTS_APIKEY_HELPER_REEXEC"


def _sp_oauth_token():
    # Mint the SP token via the SDK. The M2M (client_id/secret) profile the
    # Omnigent host writes is NOT usable via `databricks auth token` (that CLI
    # verb is U2M-only and refuses M2M); Config.authenticate() does the
    # client-credentials flow. Verified accepted by /anthropic (C-O1, HTTP 200).
    # Absent profile / non-host instance -> returns None, caller falls back.
    try:
        from databricks.sdk.core import Config
    except ImportError:
        # Claude Code invokes this via a bare python3 (e.g. /usr/bin/python3)
        # that lacks databricks-sdk, so the import fails and the SP mint would
        # silently fall back to a PAT (absent in the host/SP context) -> empty
        # token -> gateway 401. Re-run this same file once under uv, which
        # provisions the SDK. Must be `uv run ... python <file>` (uv's OWN
        # managed interpreter) — `uv run --with X <external-python>` runs that
        # external python, which still lacks the SDK.
        if os.environ.get(_REEXEC_GUARD) == "1":
            return None
        uv = shutil.which("uv")
        if not uv:
            return None
        env = dict(os.environ, **{_REEXEC_GUARD: "1"})
        try:
            out = subprocess.run(
                [uv, "run", "--with", "databricks-sdk", "python",
                 os.path.abspath(__file__)],
                env=env, capture_output=True, text=True, check=False,
            )
        except Exception:
            return None
        return (out.stdout or "").strip() or None
    except Exception:
        return None
    try:
        headers = Config(profile=SP_PROFILE).authenticate()
    except Exception:
        return None
    auth = (headers or {}).get("Authorization", "")
    # Strip the "Bearer " prefix so stdout carries the raw token only.
    return auth[7:].strip() if auth.startswith("Bearer ") else (auth.strip() or None)


def _pat_token():
    tok = os.environ.get("DATABRICKS_TOKEN", "").strip()
    if tok:
        return tok
    cfg_path = os.path.expanduser("~/.databrickscfg")
    try:
        cp = configparser.ConfigParser()
        cp.read(cfg_path)
        return (cp["DEFAULT"].get("token") or "").strip() or None
    except Exception:
        return None


def main():
    token = _sp_oauth_token() or _pat_token()
    if not token:
        print("no token source (no SP OAuth profile, no PAT)", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(token)


if __name__ == "__main__":
    main()
'''
    helper_path.write_text(helper_src)
    helper_path.chmod(0o700)
    return helper_path

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

    # Token source (spec C): by default write the static PAT. When
    # ENABLE_SP_APIKEYHELPER is set, install an apiKeyHelper that fetches a
    # fresh token per-TTL instead — Claude Code re-runs it on the interval
    # below, so nothing has to rotate a static token into this file. The
    # helper falls back to the PAT when no SP OAuth profile is present, so the
    # standard per-user deploy is unaffected even with the flag on.
    if os.environ.get("ENABLE_SP_APIKEYHELPER", "").strip().lower() in ("true", "1", "yes"):
        helper_path = _write_apikey_helper(claude_dir)
        # apiKeyHelper is a shell command; invoke via python3 explicitly so it
        # doesn't depend on shebang resolution or the file's PATH.
        settings["apiKeyHelper"] = f"python3 {helper_path}"
        # SP OAuth tokens are short-lived (~1h); re-run the helper well under
        # that. Matches Omnigent's native-claude default.
        settings["env"]["CLAUDE_CODE_API_KEY_HELPER_TTL_MS"] = "900000"
        # Do not pin a static token — the helper is authoritative.
        settings["env"].pop("ANTHROPIC_AUTH_TOKEN", None)
        print(f"Claude apiKeyHelper installed: {helper_path}")
    else:
        settings["env"]["ANTHROPIC_AUTH_TOKEN"] = token
    settings["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] = opus_model
    settings["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] = sonnet_model
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

if os.environ.get("CODA_SKIP_CLAUDE_INSTALL", "").lower() == "true":
    print("Claude Code CLI install skipped")
else:
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
