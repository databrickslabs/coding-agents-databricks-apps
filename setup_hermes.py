#!/usr/bin/env python
"""Configure Hermes Agent with Databricks Model Serving.

Hermes Agent (github.com/NousResearch/hermes-agent) is a multi-provider AI CLI
with tool-calling, persistent memory, slash commands, and a rich skill system.

Unlike the other CLIs in CoDA, Hermes is a Python application (not npm).
This script installs from PyPI with minimal deps (core covers chat +
Databricks model serving). The upstream `.[all]` extras pull ~500 MB /
90+ packages — users can add specific extras later if needed:

    uv pip install "hermes-agent[mcp,messaging,...]"

Config: ~/.hermes/config.yaml with custom provider for Databricks.
Auth:   Bearer token via Databricks PAT.

Config precedence (matches Claude/Codex/Gemini/OpenCode setup):
  1. If DATABRICKS_GATEWAY_HOST or DATABRICKS_WORKSPACE_ID -> AI Gateway
  2. Otherwise -> DATABRICKS_HOST/serving-endpoints

Opt-out:
  Set ENABLE_HERMES=false in app.yaml to skip installation entirely.
"""
import os
import subprocess
from pathlib import Path

from utils import adapt_instructions_file, ensure_https, get_gateway_host

# Opt-out: allow operators to disable Hermes bundling without removing the file.
if os.environ.get("ENABLE_HERMES", "true").strip().lower() in ("false", "0", "no"):
    print("ENABLE_HERMES=false — skipping Hermes Agent setup")
    raise SystemExit(0)

# Set HOME if not properly set
if not os.environ.get("HOME") or os.environ["HOME"] == "/":
    os.environ["HOME"] = "/app/python/source_code"

home = Path(os.environ["HOME"])

host = os.environ.get("DATABRICKS_HOST", "")
token = os.environ.get("DATABRICKS_TOKEN", "")
hermes_model = os.environ.get("HERMES_MODEL", "databricks-claude-opus-4-7")
hermes_fallback_model = os.environ.get("HERMES_FALLBACK_MODEL", "databricks-claude-opus-4-6")

hermes_home = home / ".hermes"
hermes_bin = home / ".local" / "bin" / "hermes"

# Minimal install from git — core deps (openai, anthropic, prompt_toolkit, rich,
# httpx, pyyaml, pydantic) cover chat + Databricks model serving. Not on PyPI,
# so we install directly from GitHub. uv tool install handles venv + binary.
# The mcp package is needed for HTTP transport (DeepWiki, Exa MCP servers).
HERMES_PKG = "hermes-agent @ git+https://github.com/NousResearch/hermes-agent.git"
HERMES_EXTRA_DEPS = ["mcp>=1.2.0"]

# 1. Install Hermes Agent (always, even without token).
local_bin = home / ".local" / "bin"
local_bin.mkdir(parents=True, exist_ok=True)
hermes_home.mkdir(parents=True, exist_ok=True)


def _run(cmd, **kwargs):
    """Run a subprocess command and return (rc, stdout, stderr)."""
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    return result.returncode, result.stdout, result.stderr


if not hermes_bin.exists():
    print("Installing Hermes Agent from PyPI (minimal)...")

    install_cmd = ["uv", "tool", "install"]
    for dep in HERMES_EXTRA_DEPS:
        install_cmd.extend(["--with", dep])
    install_cmd.append(HERMES_PKG)

    rc, _, err = _run(
        install_cmd,
        timeout=600,
    )
    if rc != 0:
        print(f"Hermes install failed (rc={rc}): {err[-600:]}")
        raise SystemExit(0)

    if hermes_bin.exists():
        print(f"Hermes Agent installed at {hermes_bin}")
    else:
        print(f"Warning: uv tool install succeeded but {hermes_bin} not found")
else:
    print(f"Hermes Agent already installed at {hermes_bin}")

# 2. Pre-create standard Hermes runtime dirs so first run doesn't race on mkdir.
for sub in ("sessions", "logs", "memories", "skills", "cron", "pairing",
            "hooks", "image_cache", "audio_cache"):
    (hermes_home / sub).mkdir(parents=True, exist_ok=True)

# 3. Skip auth config if no token (will be configured after PAT setup)
if not host or not token:
    print("Hermes Agent installed — config will be set after PAT setup")
    raise SystemExit(0)

# Strip trailing slash and ensure https:// prefix
host = ensure_https(host.rstrip("/"))

gateway_host = get_gateway_host()
gateway_token = os.environ.get("DATABRICKS_TOKEN", "") if gateway_host else ""
if gateway_host and not gateway_token:
    print("Warning: AI Gateway resolved but DATABRICKS_TOKEN missing, falling back to DATABRICKS_HOST")
    gateway_host = ""

if gateway_host:
    base_url = f"{gateway_host}/mlflow/v1"
    auth_token = gateway_token
    print(f"Using Databricks AI Gateway: {gateway_host}")
else:
    base_url = f"{host}/serving-endpoints"
    auth_token = token
    print(f"Using Databricks Host: {host}")

# 4. Write ~/.hermes/config.yaml
config_path = hermes_home / "config.yaml"

claude_skills_dir = Path("/app/python/source_code/.claude/skills")
external_skills = [str(claude_skills_dir)] if claude_skills_dir.exists() else []

model_catalog = [
    "databricks-claude-opus-4-6",
    "databricks-claude-sonnet-4-6",
    "databricks-claude-haiku-4-5",
    "databricks-gpt-5-3-codex",
    "databricks-gpt-5-1-codex-max",
    "databricks-gemini-2-5-flash",
    "databricks-gemini-2-5-pro",
    "databricks-gemini-2-5-pro",
]

lines = []
lines.append("# Hermes Agent config — generated by setup_hermes.py")
lines.append("# Regenerate by re-running: uv run python setup_hermes.py")
lines.append("")
lines.append("model:")
lines.append(f"  default: {hermes_model}")
lines.append("  provider: custom")
lines.append(f"  base_url: {base_url}")
lines.append(f"  api_key: {auth_token}")
lines.append("")
lines.append("# Fallback chain — triggers on 429 (rate limit), 529 (overload),")
lines.append("# 503 (service errors), or connection failures.")
lines.append("fallback_providers:")
lines.append("- provider: custom")
lines.append(f"  model: {hermes_fallback_model}")
lines.append(f"  base_url: {base_url}")
lines.append(f"  api_key: {auth_token}")
lines.append("")
lines.append("# External skills — Claude Code skill directory (shared with other agents)")
lines.append("skills:")
if external_skills:
    lines.append("  external_dirs:")
    for d in external_skills:
        lines.append(f"    - {d}")
else:
    lines.append("  external_dirs: []")
lines.append("")
lines.append("# Native MCP servers — DeepWiki (GitHub wiki lookup) + Exa (web search)")
lines.append("mcp_servers:")
lines.append("  deepwiki:")
lines.append("    url: https://mcp.deepwiki.com/mcp")
lines.append("    timeout: 60")
lines.append("  exa:")
lines.append("    url: https://mcp.exa.ai/mcp")
lines.append("    timeout: 60")

team_memory_url = os.environ.get("TEAM_MEMORY_MCP_URL", "").strip().rstrip("/")
if team_memory_url:
    lines.append("  team-memory:")
    lines.append(f"    url: {team_memory_url}/mcp")
    lines.append("    timeout: 60")
    print(f"Team memory MCP configured: {team_memory_url}/mcp")

lines.append("")
lines.append("# Model catalog hint — users can `/model` switch inside chat")
lines.append("display:")
lines.append("  known_models:")
for m in model_catalog:
    lines.append(f"    - {m}")
lines.append("")

should_write = True
if config_path.exists():
    existing = config_path.read_text()
    if "generated by setup_hermes.py" not in existing and "provider: custom" in existing:
        print(f"Existing {config_path} looks hand-edited — preserving it (skipping rewrite)")
        should_write = False

if should_write:
    config_path.write_text("\n".join(lines))
    print(f"Hermes config written: {config_path}")

# 5. Adapt CLAUDE.md -> ~/.hermes/HERMES.md for first-run context
claude_md_locations = [
    Path(__file__).parent / "CLAUDE.md",
    home / ".claude" / "CLAUDE.md",
    Path("/app/python/source_code/CLAUDE.md"),
]

claude_md_path = None
for loc in claude_md_locations:
    if loc.exists():
        claude_md_path = loc
        break

hermes_md = hermes_home / "HERMES.md"
adapt_instructions_file(
    source_path=claude_md_path or claude_md_locations[0],
    target_path=hermes_md,
    new_header="# Hermes Agent on Databricks",
    cli_name="Hermes",
)

# 6. Create projects directory (parity with other agents)
projects_dir = home / "projects"
projects_dir.mkdir(exist_ok=True)

print("\nHermes Agent ready! Usage:")
print("  hermes chat                    # Interactive chat")
print("  hermes --tui chat              # Rich Ink TUI")
print("  hermes model                   # Select default model")
print("  hermes setup                   # Reconfigure wizard")
print("  hermes mcp add <name> <url>    # Add MCP server")
print(f"\nEndpoint:       {base_url}")
print(f"Primary model:  {hermes_model}")
print(f"Fallback model: {hermes_fallback_model} (auto-activates on 429/529/503)")
print(f"Install:        minimal  (add extras: uv pip install \"hermes-agent[mcp,messaging,...]\")")
print("Auth:           Bearer token (Databricks PAT)")
