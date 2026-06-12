import asyncio
import os
import pty
import fcntl
import struct
import termios
import select
import subprocess
import uuid
import threading
import signal
import time
import copy
import logging
from concurrent.futures import ThreadPoolExecutor, wait
from flask import Flask, send_from_directory, request, jsonify, session
from flask_socketio import SocketIO, emit, join_room, leave_room, disconnect
from werkzeug.utils import secure_filename
from collections import deque

import tomllib
import requests

import app_state
import enterprise_config
from utils import ensure_https, get_gateway_host
from pat_rotator import PATRotator
from telemetry import log_telemetry, set_product_info

# Sanitize DATABRICKS_TOKEN early — the platform sometimes injects trailing
# newlines / whitespace which causes auth failures.  Cleaning it here prevents
# the agent from "fixing" it in the terminal and leaking the raw token.
_raw_token = os.environ.get("DATABRICKS_TOKEN", "")
if _raw_token != _raw_token.strip():
    os.environ["DATABRICKS_TOKEN"] = _raw_token.strip()

# App version (single source of truth: pyproject.toml)
_pyproject_file = os.path.join(os.path.dirname(__file__), 'pyproject.toml')
try:
    with open(_pyproject_file, 'rb') as _f:
        APP_VERSION = tomllib.load(_f)['project']['version']
except Exception:
    APP_VERSION = '0.0.0'

# Session timeout configuration
SESSION_TIMEOUT_SECONDS = 86400      # No poll for 24 hours = dead session
CLEANUP_INTERVAL_SECONDS = 900       # Check for stale sessions every 15 min
GRACEFUL_SHUTDOWN_WAIT = 3          # Seconds to wait after SIGHUP before SIGKILL
MAX_CONCURRENT_SESSIONS = int(os.environ.get("MAX_CONCURRENT_SESSIONS", "5"))
TRANSCRIPT_CAP_BYTES = 10 * 1024 * 1024  # 10 MB soft cap per transcript

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# PAT auto-rotation — initialized after sessions dict is defined (see below)

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.secret_key = os.urandom(24)
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32 MB — aligned with Claude Code's 30 MB file limit

# WebSocket support via Flask-SocketIO (simple-websocket transport, threading mode)
# Used for local dev (python app.py). Under uvicorn/ASGI, the AsyncServer in
# mcp_asgi.py intercepts /socket.io/ before WSGIMiddleware, so these handlers
# are only active in WSGI mode.
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins=[], logger=False, engineio_logger=False)

# ── ASGI WebSocket support (python-socketio AsyncServer) ─────────────
# Set by mcp_asgi.py at startup. Background threads use _emit_from_thread()
# which routes to the async server (ASGI) or Flask-SocketIO (WSGI) automatically.
_async_sio = None
_event_loop = None


def set_async_sio(sio_instance, loop):
    """Called by mcp_asgi.py to wire up the ASGI Socket.IO server."""
    global _async_sio, _event_loop
    _async_sio = sio_instance
    _event_loop = loop


def _emit_from_thread(event, data, room=None):
    """Thread-safe emit for background threads (PTY reader, cleanup, SIGTERM).

    Routes to AsyncServer (ASGI mode) or Flask-SocketIO (WSGI mode) automatically.
    """
    if _async_sio and _event_loop and _event_loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(
                _async_sio.emit(event, data, room=room),
                _event_loop,
            )
        except Exception:
            pass
    else:
        # WSGI mode (local dev) — use Flask-SocketIO directly
        try:
            socketio.emit(event, data, room=room)
        except Exception:
            pass


# Store sessions: {session_id: {"master_fd": fd, "pid": pid, "output_buffer": deque, "lock": Lock, ...}}
# sessions_lock guards dict-level ops (add/remove/iterate); each session["lock"] guards per-session state
sessions = {}
sessions_lock = threading.Lock()

# PAT auto-rotation (short-lived tokens, background refresh)
# Only rotates while active sessions exist — stops when all sessions are reaped
pat_rotator = PATRotator(
    session_count_fn=lambda: len(sessions),
)

# SIGTERM graceful shutdown: notify clients before gunicorn stops the worker
shutting_down = False

_start_time = time.time()

def handle_sigterm(signum, frame):
    """Notify clients that app is shutting down, then let gunicorn handle the rest."""
    global shutting_down
    # Ignore SIGTERMs in the first 10s — likely stale signals from a prior process kill
    if time.time() - _start_time < 10:
        logger.info("SIGTERM received during startup — ignoring (likely stale signal)")
        return
    shutting_down = True
    logger.info("SIGTERM received — setting shutting_down flag for clients")
    # Notify WS clients immediately (HTTP poll clients will see shutting_down on next poll)
    _emit_from_thread('shutting_down', {})

# NOTE: Do not register SIGTERM handler at module level.
# It is installed in initialize_app() for gunicorn only.
# For local dev (__main__), we keep SIG_DFL so the process just exits.

# Setup state tracking
setup_lock = threading.Lock()
setup_state = {
    "status": "pending",
    "started_at": None,
    "completed_at": None,
    "error": None,
    "steps": [
        {"id": "git",        "label": "Configuring git identity",     "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "micro",      "label": "Installing micro editor",      "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "gh",         "label": "Installing GitHub CLI",        "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "dbcli",     "label": "Upgrading Databricks CLI",     "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "proxy",   "label": "Starting content-filter proxy", "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "claude",     "label": "Configuring Claude CLI",       "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "codex",      "label": "Configuring Codex CLI",        "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "opencode",   "label": "Configuring OpenCode CLI",     "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "gemini",     "label": "Configuring Gemini CLI",       "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "hermes",     "label": "Configuring Hermes Agent",     "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "databricks", "label": "Setting up Databricks CLI",    "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "mlflow",     "label": "Enabling MLflow tracing",       "status": "pending", "started_at": None, "completed_at": None, "error": None},
    ]
}


def _update_step(step_id, **kwargs):
    with setup_lock:
        for step in setup_state["steps"]:
            if step["id"] == step_id:
                step.update(kwargs)
                break


def _get_setup_state_snapshot():
    with setup_lock:
        return copy.deepcopy(setup_state)


# Single-user security: only the token owner can access the terminal
app_owner = None


def _run_step(step_id, command):
    _update_step(step_id, status="running", started_at=time.time())
    try:
        env = os.environ.copy()
        if not env.get("HOME") or env["HOME"] == "/":
            env["HOME"] = "/app/python/source_code"
        home = env.get("HOME", "/app/python/source_code")
        # Ensure uv and other tools in ~/.local/bin are on PATH
        local_bin = os.path.join(home, ".local", "bin")
        if local_bin not in env.get("PATH", ""):
            env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"
        env.pop("DATABRICKS_CLIENT_ID", None)
        env.pop("DATABRICKS_CLIENT_SECRET", None)

        # Ensure setup scripts can still import from repo root (e.g. `from utils import ...`)
        app_dir = os.path.dirname(os.path.abspath(__file__))
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{app_dir}:{existing_pp}" if existing_pp else app_dir

        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=300)
        if result.returncode == 0:
            _update_step(step_id, status="complete", completed_at=time.time())
        else:
            err = result.stderr.strip() or result.stdout.strip() or "Unknown error"
            _update_step(step_id, status="error", completed_at=time.time(), error=err[:500])
    except subprocess.TimeoutExpired:
        _update_step(step_id, status="error", completed_at=time.time(), error="Timed out after 300s")
    except Exception as e:
        _update_step(step_id, status="error", completed_at=time.time(), error=str(e))


def _build_terminal_shell_env(base_env: dict) -> dict:
    """Build the env dict for a user terminal PTY.

    Starts from ``base_env`` (typically ``os.environ``) and strips the
    credentials and CLI-state vars that should never reach a user shell:

    - ``CLAUDECODE`` / ``CLAUDE_CODE_SESSION`` — would mark the terminal as
      a nested-Claude session.
    - ``DATABRICKS_TOKEN`` / ``DATABRICKS_HOST`` — forces CLIs to read
      ``~/.databrickscfg`` per-request so they pick up rotated PATs without
      an env-snapshot rewrite.
    - ``GEMINI_API_KEY`` — same pattern, read from config file instead.
    - ``NPM_TOKEN`` / ``UV_DEFAULT_INDEX`` / ``UV_INDEX_*_PASSWORD`` /
      ``UV_INDEX_*_USERNAME`` / ``npm_config_//host/:_authToken`` —
      deployer-level credentials from app.yaml that must not be readable
      via ``env`` inside the user terminal. The user's npm/uv operations
      still work because ``~/.npmrc`` (written by
      ``enterprise_config.bootstrap``) holds the registry config — they
      just can't see the bearer token in plaintext. (F-01)
    """
    shell_env = base_env.copy()
    shell_env["TERM"] = "xterm-256color"

    # Always-strip fixed names
    for key in (
        "CLAUDECODE", "CLAUDE_CODE_SESSION",
        "DATABRICKS_TOKEN", "DATABRICKS_HOST",
        "GEMINI_API_KEY",
        "NPM_TOKEN", "UV_DEFAULT_INDEX",
    ):
        shell_env.pop(key, None)

    # Pattern-strip operator-named registry credentials
    for key in list(shell_env.keys()):
        if (
            key.startswith("npm_config_//")  # derived registry-auth tokens
            or (
                key.startswith("UV_INDEX_")
                and (key.endswith("_PASSWORD") or key.endswith("_USERNAME"))
            )
        ):
            shell_env.pop(key, None)

    return shell_env


def _setup_git_config():
    """Configure git identity and hooks by writing files directly (no subprocess)."""
    home = os.environ.get("HOME", "/app/python/source_code")
    if not home or home == "/":
        home = "/app/python/source_code"

    # Get user identity from Databricks token
    user_email = None
    display_name = None
    try:
        from databricks.sdk import WorkspaceClient
        db_host = ensure_https(os.environ.get("DATABRICKS_HOST", ""))
        db_token = os.environ.get("DATABRICKS_TOKEN")
        if db_host and db_token:
            w = WorkspaceClient(host=db_host, token=db_token, auth_type="pat")
            set_product_info(w)
            me = w.current_user.me()
            user_email = me.user_name
            display_name = me.display_name or user_email.split("@")[0]
    except Exception as e:
        logger.warning(f"Could not get user identity from token: {e}")

    # Write ~/.gitconfig directly (more reliable than subprocess git config)
    gitconfig_path = os.path.join(home, ".gitconfig")
    hooks_dir = os.path.join(home, ".githooks")
    os.makedirs(hooks_dir, exist_ok=True)

    lines = []
    if user_email and display_name:
        lines.append("[user]")
        lines.append(f"\temail = {user_email}")
        lines.append(f"\tname = {display_name}")
    lines.append("[core]")
    lines.append(f"\thooksPath = {hooks_dir}")

    with open(gitconfig_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    logger.info(f"Git config written to {gitconfig_path}")

    # Write post-commit hook for workspace sync (works from any CLI: Claude, Gemini, OpenCode, etc.)
    # Only syncs repos inside ~/projects/ — skips the app source and any other repos
    post_commit = os.path.join(hooks_dir, "post-commit")
    with open(post_commit, "w") as f:
        f.write('#!/bin/bash\n')
        f.write('# Auto-sync to Databricks Workspace on commit (works from any CLI)\n')
        f.write('SYNC_LOG="$HOME/.sync.log"\n')
        f.write('\n')
        f.write('# Resolve git repo root (handles commits from subdirectories)\n')
        f.write('REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"\n')
        f.write('if [ -z "$REPO_ROOT" ]; then\n')
        f.write('    echo "[post-commit] $(date +%H:%M:%S) SKIP: not inside a git repo" >> "$SYNC_LOG"\n')
        f.write('    exit 0\n')
        f.write('fi\n')
        f.write('\n')
        f.write('# Only sync repos inside ~/projects/\n')
        f.write('PROJECTS_DIR="$HOME/projects"\n')
        f.write('case "$REPO_ROOT" in\n')
        f.write('    "$PROJECTS_DIR"/*)\n')
        f.write('        ;; # allowed - continue\n')
        f.write('    *)\n')
        f.write('        echo "[post-commit] $(date +%H:%M:%S) SKIP: $REPO_ROOT is outside $PROJECTS_DIR" >> "$SYNC_LOG"\n')
        f.write('        exit 0\n')
        f.write('        ;;\n')
        f.write('esac\n')
        f.write('\n')
        f.write('echo "[post-commit] $(date +%H:%M:%S) syncing $REPO_ROOT" >> "$SYNC_LOG"\n')
        f.write('\n')
        f.write('# Use uv run so sync script gets the correct Python + deps\n')
        f.write('APP_DIR="/app/python/source_code"\n')
        f.write('SYNC_SCRIPT="$APP_DIR/sync_to_workspace.py"\n')
        f.write('\n')
        f.write('if [ -f "$SYNC_SCRIPT" ]; then\n')
        f.write('    nohup uv run --project "$APP_DIR" python "$SYNC_SCRIPT" "$REPO_ROOT" >> "$SYNC_LOG" 2>&1 & disown\n')
        f.write('else\n')
        f.write('    echo "[post-commit] $(date +%H:%M:%S) SKIP: sync script not found" >> "$SYNC_LOG"\n')
        f.write('fi\n')
    os.chmod(post_commit, 0o755)
    logger.info(f"Post-commit hook written to {post_commit}")

    # Reinit app source git to remove template origin (Databricks Apps only)
    _reinit_app_git()


def _reinit_app_git():
    """On Databricks Apps, reinit git to remove template origin remote."""
    app_dir = os.path.dirname(os.path.abspath(__file__))
    if app_dir != "/app/python/source_code":
        return  # Local dev — leave git intact

    git_dir = os.path.join(app_dir, ".git")
    if not os.path.isdir(git_dir):
        return  # Already clean

    import shutil
    shutil.rmtree(git_dir)
    subprocess.run(["git", "init"], cwd=app_dir, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=app_dir, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit from coding-agents template"],
        cwd=app_dir, capture_output=True,
    )
    logger.info("Reinitialized app source git (template origin removed)")


def _configure_all_cli_auth(token):
    """Configure auth for ALL coding-agent CLIs after a PAT is provided.

    Called from /api/configure-pat when a user supplies a PAT interactively.
    Handles: Claude CLI (inline), Databricks CLI (via pat_rotator), and
    Codex/OpenCode/Gemini CLIs (by re-running their setup scripts with token in env).
    """
    import json

    from utils import resolve_and_cache_gateway
    resolve_and_cache_gateway()

    home = os.environ.get("HOME", "/app/python/source_code")
    if not home or home == "/":
        home = "/app/python/source_code"

    # 1. Configure Claude CLI (~/.claude/settings.json)
    claude_dir = os.path.join(home, ".claude")
    os.makedirs(claude_dir, exist_ok=True)

    gateway_host = get_gateway_host()
    databricks_host = ensure_https(os.environ.get("DATABRICKS_HOST", "").rstrip("/"))

    if gateway_host:
        anthropic_base_url = f"{gateway_host}/anthropic"
    else:
        anthropic_base_url = f"{databricks_host}/serving-endpoints/anthropic"

    # Read-merge-write to preserve env vars from other setup scripts (e.g. setup_mlflow.py)
    settings_path = os.path.join(claude_dir, "settings.json")
    try:
        with open(settings_path) as f:
            settings = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        settings = {}

    settings.setdefault("env", {})
    settings["env"]["ANTHROPIC_MODEL"] = os.environ.get("ANTHROPIC_MODEL", "databricks-claude-opus-4-7")
    settings["env"]["ANTHROPIC_BASE_URL"] = anthropic_base_url
    settings["env"]["ANTHROPIC_AUTH_TOKEN"] = token
    settings["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] = "databricks-claude-opus-4-7"
    settings["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] = "databricks-claude-sonnet-4-6"
    settings["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = "databricks-claude-haiku-4-5"
    settings["env"]["ANTHROPIC_CUSTOM_HEADERS"] = "x-databricks-use-coding-agent-mode: true"
    settings["env"]["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"

    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)

    logger.info(f"Claude CLI auth configured: {settings_path}")

    # 2. Configure Databricks CLI (~/.databrickscfg) — already called by
    #    configure_pat() via pat_rotator, but explicit for clarity
    pat_rotator._write_databrickscfg(token)
    logger.info("Databricks CLI auth configured: ~/.databrickscfg")

    # 3. Re-run Codex, OpenCode, Gemini setup scripts with token in env
    #    They are idempotent: detect CLI already installed, just write config files
    app_dir = os.path.dirname(os.path.abspath(__file__))
    existing_pp = os.environ.get("PYTHONPATH", "")
    env = {
        **os.environ,
        "DATABRICKS_TOKEN": token,
        "PYTHONPATH": f"{app_dir}:{existing_pp}" if existing_pp else app_dir,
    }
    for script in ["setup/setup_codex.py", "setup/setup_opencode.py", "setup/setup_gemini.py", "setup/setup_hermes.py"]:
        try:
            result = subprocess.run(
                ["uv", "run", "python", script],
                env=env, capture_output=True, text=True, timeout=60
            )
            if result.returncode == 0:
                logger.info(f"CLI config updated: {script}")
            else:
                logger.warning(f"CLI config failed: {script}: {result.stderr[:200]}")
        except Exception as e:
            logger.warning(f"CLI config error: {script}: {e}")


def run_setup():
    with setup_lock:
        setup_state["status"] = "running"
        setup_state["started_at"] = time.time()

    # Apply enterprise (proxy/registry) config before any subprocess runs:
    # writes ~/.npmrc, pushes derived env vars (npm_config_registry, CURL_CA_BUNDLE,
    # etc.) into os.environ so every child process inherits them, and logs a
    # banner of the effective config. No-op when no enterprise env vars are set.
    enterprise_config.bootstrap()

    # Probe AI Gateway once; result is cached in _GATEWAY_RESOLVED for subprocesses
    from utils import resolve_and_cache_gateway
    resolve_and_cache_gateway()

    # --- Sequential prerequisites (git identity + editor) ---
    # Git config — done directly in Python, not as a subprocess
    _update_step("git", status="running", started_at=time.time())
    try:
        _setup_git_config()
        _update_step("git", status="complete", completed_at=time.time())
    except Exception as e:
        _update_step("git", status="error", completed_at=time.time(), error=str(e))

    _run_step("micro", ["bash", "-c",
        "mkdir -p ~/.local/bin && bash scripts/install_micro.sh && mv micro ~/.local/bin/ 2>/dev/null || true"])

    _run_step("gh", ["bash", "scripts/install_gh.sh"])

    # --- Upgrade Databricks CLI (runtime image ships an older version) ---
    _run_step("dbcli", ["bash", "scripts/install_databricks_cli.sh"])

    # --- Content-filter proxy (must be running before OpenCode starts) ---
    # Sanitizes requests/responses between OpenCode and Databricks
    # (see OpenCode #5028, docs/plans/2026-03-11-litellm-empty-content-blocks-design.md)
    _run_step("proxy", ["uv", "run", "python", "setup/setup_proxy.py"])

    # --- Parallel agent setup (all independent of each other) ---
    parallel_steps = [
        ("claude",     ["uv", "run", "python", "setup/setup_claude.py"]),
        ("codex",      ["uv", "run", "python", "setup/setup_codex.py"]),
        ("opencode",   ["uv", "run", "python", "setup/setup_opencode.py"]),
        ("gemini",     ["uv", "run", "python", "setup/setup_gemini.py"]),
        ("hermes",     ["uv", "run", "python", "setup/setup_hermes.py"]),
        ("databricks", ["uv", "run", "python", "setup/setup_databricks.py"]),
    ]

    with ThreadPoolExecutor(max_workers=len(parallel_steps)) as executor:
        futures = [
            executor.submit(_run_step, step_id, command)
            for step_id, command in parallel_steps
        ]
        wait(futures)

    # --- MLflow setup runs AFTER claude setup to avoid settings.json race ---
    # setup_mlflow.py merges env vars into ~/.claude/settings.json which
    # setup_claude.py also writes; running sequentially prevents clobbering.
    _run_step("mlflow", ["uv", "run", "python", "setup/setup_mlflow.py"])

    # Sync latest token into all CLI configs — covers the race where PAT
    # rotation happened while a setup script was still installing (the
    # rotation's update_cli_tokens() call silently skips missing config files).
    current_token = os.environ.get("DATABRICKS_TOKEN", "")
    if current_token:
        try:
            from cli_auth import update_cli_tokens
            update_cli_tokens(current_token)
            logger.info("Post-setup token sync: all CLI configs updated with current token")
        except Exception as e:
            logger.warning(f"Post-setup token sync failed: {e}")

    with setup_lock:
        any_error = any(s["status"] == "error" for s in setup_state["steps"])
        setup_state["status"] = "error" if any_error else "complete"
        setup_state["completed_at"] = time.time()


def get_token_owner():
    """Get the owner email. Priority: Apps API (app.creator) > PAT (current_user.me).

    Uses the auto-provisioned SP to call the Apps API — no PAT needed for
    owner resolution. Falls back to PAT-based lookup for backward compat.
    """
    from databricks.sdk import WorkspaceClient

    # 1. Try Apps API via SP credentials (no PAT needed)
    app_name = os.environ.get("DATABRICKS_APP_NAME")
    if app_name:
        try:
            w = WorkspaceClient()  # auto-detects SP credentials
            set_product_info(w)
            app = w.apps.get(name=app_name)
            owner = (app.creator or "").lower()
            logger.info(f"Owner resolved from app.creator: {owner}")
            return owner
        except Exception as e:
            logger.warning(f"Could not resolve owner via Apps API: {e}")

    # 2. Fallback: PAT-based resolution
    try:
        host = ensure_https(os.environ.get("DATABRICKS_HOST", ""))
        token = os.environ.get("DATABRICKS_TOKEN")
        if not host or not token:
            return None
        w = WorkspaceClient(host=host, token=token, auth_type="pat")
        set_product_info(w)
        username = w.current_user.me().user_name
        return username.lower() if username else username
    except Exception as e:
        logger.warning(f"Could not determine token owner: {e}")
        return None


def get_request_user():
    """Extract user email from Databricks Apps request headers.

    Returns lowercase email to ensure case-insensitive matching against app_owner.
    """
    email = (
        request.headers.get("X-Forwarded-Email")
        or request.headers.get("X-Forwarded-User")
        or request.headers.get("X-Databricks-User-Email")
    )
    return email.lower() if email else email


def _is_databricks_apps():
    """Detect if we're running on Databricks Apps (not local dev)."""
    return os.environ.get("DATABRICKS_APP_PORT") or os.path.isdir("/app/python/source_code")


def check_authorization():
    """Check if the current user is authorized to access the app.

    Fails CLOSED on Databricks Apps: if we can't determine the owner,
    deny all access rather than allowing unauthenticated terminal access.
    Fails open only for local development.
    Fixes: https://github.com/datasciencemonkey/coding-agents-databricks-apps/issues/57
    """
    # Fail closed on Databricks Apps if owner couldn't be resolved
    if not app_owner:
        if _is_databricks_apps():
            logger.error("SECURITY: app_owner not resolved — denying all access (fail-closed)")
            return False, "unknown"
        return True, None  # Local dev only

    current_user = get_request_user()

    # If no user identity in request (local dev), allow access
    if not current_user:
        if _is_databricks_apps():
            logger.warning("No user identity in request on Databricks Apps — denying access")
            return False, "unknown"
        return True, None

    # Check if current user is the owner
    if current_user != app_owner:
        logger.warning(f"Unauthorized access attempt by {current_user} (owner: {app_owner})")
        return False, current_user

    return True, None


def _check_ws_authorization():
    """Check authorization for WebSocket connections — mirrors HTTP check_authorization().

    Fails CLOSED on Databricks Apps: if app_owner is unresolved or no user identity
    in headers, deny WebSocket access. Matches the HTTP handler's behavior exactly.
    """
    if not app_owner:
        if _is_databricks_apps():
            logger.error("SECURITY: app_owner not resolved — denying WebSocket (fail-closed)")
            return False
        return True  # Local dev only

    # Socket.IO passes HTTP headers from the initial handshake via request context
    raw_user = (
        request.headers.get("X-Forwarded-Email")
        or request.headers.get("X-Forwarded-User")
        or request.headers.get("X-Databricks-User-Email")
    )
    current_user = raw_user.lower() if raw_user else raw_user

    if not current_user:
        if _is_databricks_apps():
            logger.warning("No user identity in WebSocket request on Databricks Apps — denying")
            return False
        return True  # Local dev only

    if current_user != app_owner:
        logger.warning(f"WebSocket unauthorized: {current_user} (owner: {app_owner})")
        return False
    return True


def _check_ws_authorization_from_environ(environ):
    """Check authorization from WSGI environ dict (for ASGI WebSocket via python-socketio).

    Same logic as _check_ws_authorization() but reads headers from the environ
    dict instead of Flask's request context. WSGI environ stores HTTP headers as
    HTTP_X_FORWARDED_EMAIL (uppercase, underscores, HTTP_ prefix).
    """
    if not app_owner:
        if _is_databricks_apps():
            logger.error("SECURITY: app_owner not resolved — denying WebSocket (fail-closed)")
            return False
        return True  # Local dev only

    raw_user = (
        environ.get("HTTP_X_FORWARDED_EMAIL")
        or environ.get("HTTP_X_FORWARDED_USER")
        or environ.get("HTTP_X_DATABRICKS_USER_EMAIL")
    )
    current_user = raw_user.lower() if raw_user else raw_user

    if not current_user:
        if _is_databricks_apps():
            logger.warning("No user identity in WebSocket request on Databricks Apps — denying")
            return False
        return True  # Local dev only

    if current_user != app_owner:
        logger.warning(f"WebSocket unauthorized: {current_user} (owner: {app_owner})")
        return False
    return True


def register_sio_handlers(sio):
    """Register Socket.IO event handlers on an AsyncServer for ASGI mode.

    Called by mcp_asgi.py. The handlers mirror the Flask-SocketIO handlers below
    but use python-socketio's async API (explicit sid, enter_room/leave_room,
    async def, ConnectionRefusedError for auth denial).
    """

    @sio.on('connect')
    async def handle_connect(sid, environ, auth):
        # Capture event loop on first connection for _emit_from_thread()
        set_async_sio(sio, asyncio.get_running_loop())

        # Diagnostic: log transport and header presence for debugging proxy behavior
        transport = environ.get('QUERY_STRING', '')
        has_email = bool(environ.get('HTTP_X_FORWARDED_EMAIL'))
        has_user = bool(environ.get('HTTP_X_FORWARDED_USER'))
        logger.info(f"WS connect: sid={sid}, qs={transport}, "
                     f"has_email={has_email}, has_user={has_user}")

        if not _check_ws_authorization_from_environ(environ):
            raise ConnectionRefusedError('unauthorized')
        logger.info("WebSocket client connected (ASGI)")

    @sio.on('join_session')
    async def handle_join_session(sid, data):
        session_id = data.get('session_id')
        if not session_id:
            return {'status': 'error', 'message': 'session_id required'}
        sess = _get_session(session_id)
        if not sess:
            return {'status': 'error', 'message': 'Session not found'}
        with sess["lock"]:
            sess["last_poll_time"] = time.time()
            sess["output_buffer"].clear()
        await sio.enter_room(sid, session_id)
        logger.info(f"WebSocket client joined session room {session_id}")
        return {'status': 'ok'}

    @sio.on('leave_session')
    async def handle_leave_session(sid, data):
        session_id = data.get('session_id')
        if session_id:
            await sio.leave_room(sid, session_id)
            logger.info(f"WebSocket client left session room {session_id}")

    @sio.on('terminal_input')
    async def handle_terminal_input(sid, data):
        session_id = data.get('session_id')
        input_data = data.get('input', '')
        sess = _get_session(session_id)
        if not sess:
            return
        with sess["lock"]:
            sess["last_poll_time"] = time.time()
        fd = sess["master_fd"]
        try:
            os.write(fd, input_data.encode())
        except OSError as e:
            logger.warning(f"WebSocket input write error for {session_id}: {e}")

    @sio.on('terminal_resize')
    async def handle_terminal_resize(sid, data):
        session_id = data.get('session_id')
        cols = data.get('cols', 80)
        rows = data.get('rows', 24)
        sess = _get_session(session_id)
        if not sess:
            return
        with sess["lock"]:
            sess["last_poll_time"] = time.time()
        fd = sess["master_fd"]
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        except OSError as e:
            logger.warning(f"WebSocket resize error for {session_id}: {e}")

    @sio.on('heartbeat')
    async def handle_heartbeat(sid, data):
        session_ids = data.get('session_ids', [])
        now = time.time()
        for s_id in session_ids:
            sess = _get_session(s_id)
            if sess:
                with sess["lock"]:
                    sess["last_poll_time"] = now

    @sio.on('disconnect')
    async def handle_disconnect(sid):
        logger.info("WebSocket client disconnected (ASGI)")


# ── WebSocket Event Handlers (Flask-SocketIO — WSGI/local dev only) ──────

@socketio.on('connect')
def handle_ws_connect():
    """Authenticate WebSocket connections (AC-3)."""
    if not _check_ws_authorization():
        disconnect()
        return False
    logger.info("WebSocket client connected")


@socketio.on('join_session')
def handle_join_session(data):
    """Client joins a session room to receive output (AC-4)."""
    session_id = data.get('session_id')
    if not session_id:
        return {'status': 'error', 'message': 'session_id required'}

    session = _get_session(session_id)
    if not session:
        return {'status': 'error', 'message': 'Session not found'}

    with session["lock"]:
        session["last_poll_time"] = time.time()
        session["output_buffer"].clear()  # Prevent duplicate output on WS↔HTTP switch

    join_room(session_id)
    logger.info(f"WebSocket client joined session room {session_id}")
    return {'status': 'ok'}


@socketio.on('leave_session')
def handle_leave_session(data):
    """Client leaves a session room (AC-5)."""
    session_id = data.get('session_id')
    if session_id:
        leave_room(session_id)
        logger.info(f"WebSocket client left session room {session_id}")


@socketio.on('terminal_input')
def handle_terminal_input(data):
    """Receive keystrokes from client, write to PTY (AC-6)."""
    session_id = data.get('session_id')
    input_data = data.get('input', '')

    session = _get_session(session_id)
    if not session:
        return

    with session["lock"]:
        session["last_poll_time"] = time.time()
    fd = session["master_fd"]

    try:
        os.write(fd, input_data.encode())
    except OSError as e:
        logger.warning(f"WebSocket input write error for {session_id}: {e}")


@socketio.on('terminal_resize')
def handle_terminal_resize(data):
    """Receive resize events from client (AC-7)."""
    session_id = data.get('session_id')
    cols = data.get('cols', 80)
    rows = data.get('rows', 24)

    session = _get_session(session_id)
    if not session:
        return

    with session["lock"]:
        session["last_poll_time"] = time.time()
    fd = session["master_fd"]

    try:
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError as e:
        logger.warning(f"WebSocket resize error for {session_id}: {e}")


@socketio.on('heartbeat')
def handle_ws_heartbeat(data):
    """Periodic keepalive from WS client — prevents idle session reaping (AC-17)."""
    session_ids = data.get('session_ids', [])
    now = time.time()
    for sid in session_ids:
        session = _get_session(sid)
        if session:
            with session["lock"]:
                session["last_poll_time"] = now


@socketio.on('disconnect')
def handle_ws_disconnect():
    """Log WebSocket disconnections. Do NOT auto-close PTY — client may reconnect."""
    logger.info("WebSocket client disconnected")


def _get_session(session_id):
    """Get a session dict reference under the global lock. Returns None if not found."""
    with sessions_lock:
        return sessions.get(session_id)


def _tee_transcript_chunk(session, output: bytes, cap: int = TRANSCRIPT_CAP_BYTES) -> None:
    """Append PTY output to the transcript file. Single-writer (read_pty_output).

    All file-handle access is under ``session["lock"]`` so we never race the
    Timer-driven close path in ``terminate_session``. The ``ValueError`` catch
    is belt-and-suspenders for the tiny window where the handle is closed
    between the ``is not None`` check and the actual ``write`` call (the lock
    prevents this, but be defensive).
    """
    with session["lock"]:
        fh = session.get("transcript_fh")
        written = session.get("transcript_bytes", 0)
        if fh is None:
            return
        remaining = cap - written
        if remaining <= 0:
            return
        chunk = output[:remaining]
        try:
            fh.write(chunk)
            fh.flush()
            session["transcript_bytes"] = written + len(chunk)
            if len(chunk) < len(output):
                fh.write(b"\n[transcript truncated at %d bytes]\n" % cap)
                fh.flush()
                fh.close()
                session["transcript_fh"] = None
        except (OSError, ValueError) as exc:
            logger.warning("transcript write failed: %s", exc)
            try:
                fh.close()
            except Exception:
                pass
            session["transcript_fh"] = None


def read_pty_output(session_id, fd):
    """Background thread to read PTY output into buffer and push via WebSocket."""
    session = _get_session(session_id)
    if not session:
        return
    pid = session["pid"]
    session_lock = session["lock"]

    while True:
        with sessions_lock:
            if session_id not in sessions:
                break
        try:
            readable, _, errors = select.select([fd], [], [fd], 0.05)
            if readable or errors:
                output = os.read(fd, 65536)
                if not output:
                    # EOF — process exited
                    break
                decoded = output.decode(errors="replace")
                with session_lock:
                    # Buffer for HTTP polling fallback (AC-15)
                    session["output_buffer"].append(decoded)
                    session["last_poll_time"] = time.time()  # Keep session alive during WS output
                # Push via WebSocket to the session room (AC-8)
                _emit_from_thread('terminal_output',
                                  {'session_id': session_id, 'output': decoded},
                                  room=session_id)
                # Tee to transcript file if enabled for this session
                _tee_transcript_chunk(session, output)
            else:
                # select timed out — check if process is still alive
                try:
                    pid_result, _ = os.waitpid(pid, os.WNOHANG)
                    if pid_result != 0:
                        # Process exited
                        break
                except ChildProcessError:
                    # Process already reaped
                    break
        except OSError:
            break

    # Process exited or fd closed — notify WebSocket clients (AC-9)
    _emit_from_thread('session_exited', {'session_id': session_id}, room=session_id)

    logger.info(f"Session {session_id} process exited")

    # Clean up immediately — no zombie sessions in the picker
    if session:
        terminate_session(session_id, session["pid"], session["master_fd"])


def terminate_session(session_id, pid, master_fd):
    """Gracefully terminate a session: SIGHUP -> wait -> SIGKILL -> cleanup.

    Idempotent. Both the explicit close path (``mcp_close_pty_session``) and the
    read-thread exit path (``read_pty_output``) call this for the same session.
    We atomically *claim* the session by popping it from ``sessions`` — only the
    caller that wins the pop kills the process and closes ``master_fd``. This
    guarantees ``os.close()`` runs exactly once: a second close could land on a
    since-reused fd (e.g. an asyncio event loop's self-pipe allocated by a later
    test) and corrupt unrelated I/O, surfacing as intermittent EBADF.
    """
    # Atomically claim the session. If it's already gone, the other teardown
    # path handled it — bail out WITHOUT touching the (possibly reused) fd.
    with sessions_lock:
        sess = sessions.pop(session_id, None)
    if sess is None:
        return

    logger.info(f"Terminating stale session {session_id} (pid={pid})")

    # Notify WebSocket clients that the session is closed
    _emit_from_thread('session_closed', {'session_id': session_id}, room=session_id)

    # Close transcript handle (if any) under per-session lock; swap-then-close
    # outside the lock to avoid blocking on slow filesystems.
    with sess["lock"]:
        transcript_fh = sess.get("transcript_fh")
        sess["transcript_fh"] = None
    if transcript_fh is not None:
        try:
            transcript_fh.close()
        except Exception:
            pass

    try:
        os.kill(pid, signal.SIGHUP)
        time.sleep(GRACEFUL_SHUTDOWN_WAIT)

        # Check if still alive, force kill if needed
        try:
            os.kill(pid, 0)  # Check if process exists
            os.kill(pid, signal.SIGKILL)
            logger.info(f"Force killed session {session_id} (pid={pid})")
        except OSError:
            pass  # Already dead

        os.close(master_fd)
    except OSError:
        pass  # Process or fd already gone

    # Clean up the project dir if coda_interactive created one.
    # Done here (not in mcp_close_pty_session) so BOTH the graceful close
    # path AND the idle reaper (which calls terminate_session directly) hit
    # this cleanup. Safe for HTTP-created sessions too — they never planted
    # a dir at this path, so os.path.isdir short-circuits.
    import shutil
    project_dir = os.path.join(
        os.path.expanduser("~/.coda/projects"),
        session_id,
    )
    if os.path.isdir(project_dir):
        try:
            shutil.rmtree(project_dir)
        except OSError as e:
            logger.warning("Failed to clean up project dir %s: %s", project_dir, e)


def _get_session_process(pid):
    """Return the name of the foreground child process for *pid*.

    Uses ``pgrep -P`` to find children (works on both macOS and Linux),
    then ``ps -o comm=`` to resolve the process name.

    Returns:
        str: process name, or ``"unknown"`` on any error / dead PID.
    """
    if not isinstance(pid, int) or pid <= 0:
        return "unknown"

    try:
        # Step 1 — find child PIDs via pgrep (cross-platform)
        child_result = subprocess.run(
            ["pgrep", "-P", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if child_result.returncode == 0 and child_result.stdout.strip():
            child_pids = child_result.stdout.strip().splitlines()
            last_child_pid = child_pids[-1].strip()

            # Step 2 — resolve child name
            name_result = subprocess.run(
                ["ps", "-o", "comm=", "-p", last_child_pid],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if name_result.returncode == 0 and name_result.stdout.strip():
                name = name_result.stdout.strip().splitlines()[0].strip()
                # ps may return the full path; take basename
                return os.path.basename(name)

        # Step 3 — no children: fall back to the process itself
        self_result = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if self_result.returncode == 0 and self_result.stdout.strip():
            name = self_result.stdout.strip().splitlines()[0].strip()
            return os.path.basename(name)

        return "unknown"
    except Exception:
        return "unknown"


def cleanup_stale_sessions():
    """Background thread that removes sessions with no recent polling."""
    while True:
        time.sleep(CLEANUP_INTERVAL_SECONDS)

        now = time.time()
        stale_sessions = []
        warning_threshold = SESSION_TIMEOUT_SECONDS * 0.8

        with sessions_lock:
            session_snapshot = list(sessions.items())

        for session_id, session in session_snapshot:
            with session["lock"]:
                idle = now - session["last_poll_time"]
                if idle > SESSION_TIMEOUT_SECONDS:
                    stale_sessions.append((session_id, session["pid"], session["master_fd"]))
                elif idle > warning_threshold:
                    session["timeout_warning"] = True

        if stale_sessions:
            logger.info(f"Found {len(stale_sessions)} stale session(s) to clean up")

        # Terminate each stale session (outside the lock)
        for session_id, pid, master_fd in stale_sessions:
            terminate_session(session_id, pid, master_fd)


@app.before_request
def authorize_request():
    """Check authorization before processing any request."""
    # Skip auth for health check, setup status, and Socket.IO (has own auth via connect event)
    if request.path in ("/health", "/api/setup-status", "/api/pat-status", "/api/configure-pat", "/api/app-state") or request.path.startswith("/socket.io") or request.path.startswith("/mcp"):
        return None

    authorized, user = check_authorization()
    if not authorized:
        return jsonify({
            "error": "Unauthorized",
            "message": f"This app belongs to {app_owner}. You are logged in as {user}."
        }), 403

    return None


@app.after_request
def set_security_headers(response):
    # MCP endpoint handles its own CORS/headers — skip security headers
    # that might interfere (CSP connect-src, X-Frame-Options, etc.)
    if request.path.startswith("/mcp"):
        return response
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # CSP: restrict scripts to self + inline (needed for embedded <script> block),
    # styles to self + inline, block all other sources. Prevents external script injection.
    # connect-src allows WebSocket + API calls to self.
    # Fixes: https://github.com/datasciencemonkey/coding-agents-databricks-apps/issues/58
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; "
        "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "img-src 'self' data:; "
        "font-src 'self' https://fonts.gstatic.com; "
        "connect-src 'self' ws: wss:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    return response


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/setup-status")
def get_setup_status():
    return jsonify(_get_setup_state_snapshot())


@app.route("/api/app-state")
def get_app_state():
    """Admin endpoint: persisted app state (owner, last rotation)."""
    return jsonify(app_state.get_state())


@app.route("/api/sessions")
def list_sessions():
    """Return a JSON array of active (non-exited) sessions with metadata."""
    now = time.time()
    with sessions_lock:
        snapshot = list(sessions.items())

    result = []
    for session_id, sess in snapshot:
        if sess.get("exited"):
            continue
        result.append({
            "session_id": session_id,
            "label": sess.get("label", ""),
            "created_at": sess.get("created_at"),
            "last_poll_time": sess.get("last_poll_time"),
            "exited": False,
            "process": _get_session_process(sess["pid"]),
            "idle_seconds": round(now - sess.get("last_poll_time", now), 1),
        })
    return jsonify(result)


def _serve_transcript_replay(session_id: str):
    """Serve the on-disk transcript for a PTY session as a replay response.

    Used by attach_session() in two cases:
      1. The PTY is gone (transcript-fallback path).
      2. The PTY exists but is replay_only=True (no live attach allowed).

    Returns either a Flask JSON response with replay=True, or a 404 if no
    transcript exists for this pty_session_id.
    """
    from coda_mcp import task_manager as _tm
    tdir = _tm.find_task_dir_by_pty_session(session_id)
    if tdir:
        transcript = os.path.join(tdir, "transcript.log")
        if os.path.isfile(transcript):
            try:
                with open(transcript, "rb") as f:
                    content = f.read()
                return jsonify({
                    "session_id": session_id,
                    "label": "hermes-mcp (replay)",
                    "output": [content.decode("utf-8", errors="replace")],
                    "replay": True,
                    "process": None,
                    "created_at": None,
                })
            except OSError:
                pass
    return jsonify({"error": "Session not found or exited"}), 404


@app.route("/api/session/attach", methods=["POST"])
def attach_session():
    """Reattach to an existing session — returns buffered output for replay.

    If the live PTY is gone but an on-disk transcript exists for this
    pty_session_id, return the transcript as ``output`` with ``replay: True``.
    """
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")

    sess = _get_session(session_id)

    # Replay-only sessions (e.g. those created by coda_run) always serve the
    # transcript-from-disk, even when the PTY is still alive.
    if sess and sess.get("replay_only"):
        return _serve_transcript_replay(session_id)

    if not sess or sess.get("exited"):
        return _serve_transcript_replay(session_id)

    # Existing live-attach path
    sess["last_poll_time"] = time.time()

    return jsonify({
        "session_id": session_id,
        "label": sess.get("label", ""),
        "output": list(sess["output_buffer"]),
        "process": _get_session_process(sess["pid"]),
        "created_at": sess.get("created_at"),
    })


@app.route("/health")
def health():
    with sessions_lock:
        session_count = len(sessions)
    with setup_lock:
        current_setup_status = setup_state["status"]
    return jsonify({
        "status": "healthy",
        "version": APP_VERSION,
        "setup_status": current_setup_status,
        "active_sessions": session_count,
        "session_timeout_seconds": SESSION_TIMEOUT_SECONDS
    })


@app.route("/api/version")
def get_version():
    return jsonify({"version": APP_VERSION})


@app.route("/api/pat-status")
def pat_status():
    """Check if a valid, usable PAT is configured."""
    host = ensure_https(os.environ.get("DATABRICKS_HOST", ""))
    token = os.environ.get("DATABRICKS_TOKEN", "").strip()

    if not token or pat_rotator.is_token_expired:
        # No token, or token lifetime exceeded (rotation stopped while no sessions)
        return jsonify({"configured": False, "valid": False,
                       "workspace_host": host})

    # Validate with direct HTTP — avoids SDK auth fallback to SP
    try:
        resp = requests.get(f"{host}/api/2.0/preview/scim/v2/Me",
                           headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if resp.status_code == 200:
            user = resp.json().get("userName", "unknown")
            return jsonify({"configured": True, "valid": True, "user": user})
        return jsonify({"configured": True, "valid": False,
                       "workspace_host": host})
    except Exception:
        return jsonify({"configured": True, "valid": False,
                       "workspace_host": host})


@app.route("/api/configure-pat", methods=["POST"])
def configure_pat():
    """Accept a user-provided PAT, validate it, and start rotation."""
    # Hotfix: only the resolved owner may (re-)configure the PAT. Without this,
    # any workspace-SSO'd user who reaches the app can submit their own valid
    # PAT and persistently impersonate the owner — every CLI call would then
    # run under the submitter's identity. app_owner is set in initialize_app()
    # before gunicorn binds, so it's reliably populated by request time on
    # Databricks Apps; this guard short-circuits to "allow" only when owner
    # resolution failed (matches the rest of the auth surface's behaviour).
    if _is_databricks_apps() and app_owner:
        if get_request_user() != app_owner:
            logger.warning(f"Rejected configure-pat from non-owner {get_request_user()} (owner: {app_owner})")
            return jsonify({"error": "Forbidden"}), 403

    # Idempotency / defence-in-depth: bootstrap is single-shot. Once a PAT
    # is configured and the rotator is alive, refuse re-submission. Without
    # this, an XSS or session-hijack vector inside the owner's browser could
    # drive a swap to an attacker-controlled PAT — the owner-gate above
    # would let it through because the request truly does come from the
    # owner's session. The expired-token escape hatch preserves the legitimate
    # re-bootstrap path (rotator timed out while idle, owner needs to refresh).
    if pat_rotator.token and not pat_rotator.is_token_expired:
        logger.warning(
            f"Rejected configure-pat: PAT already active "
            f"(user={get_request_user()}, source={request.remote_addr})"
        )
        return jsonify({
            "error": "PAT already configured. Restart the app to reconfigure."
        }), 409

    data = request.json
    token = data.get("token", "").strip()
    if not token:
        return jsonify({"error": "Token required"}), 400

    # Validate the token — direct HTTP, no SDK fallback
    host = ensure_https(os.environ.get("DATABRICKS_HOST", ""))
    try:
        resp = requests.get(f"{host}/api/2.0/preview/scim/v2/Me",
                           headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if resp.status_code != 200:
            return jsonify({"error": "Invalid token"}), 400
        user = resp.json().get("userName", "unknown")
    except Exception as e:
        return jsonify({"error": f"Token validation failed: {e}"}), 400

    # Immediately mint a controlled short-lived token from the user-pasted PAT.
    # This gives us a token ID we own — all future rotations can revoke the old one.
    os.environ["DATABRICKS_TOKEN"] = token
    pat_rotator._current_token = token
    pat_rotator._current_token_id = None
    rotated = pat_rotator._rotate_once()
    if rotated:
        token = pat_rotator.token  # use the newly minted token from here on
        # Revoke only the bootstrap PAT — leave other user PATs intact (#98)
        pat_rotator.revoke_bootstrap_token()
    else:
        # Rotation failed — fall back to user-pasted token (still valid)
        pat_rotator._write_databrickscfg(token)
    pat_rotator.start()

    # Configure all CLI tools (Claude, Codex, OpenCode, Gemini, Databricks)
    _configure_all_cli_auth(pat_rotator.token or token)

    # Run setup now that we have a valid token (installs CLIs, configures agents)
    # Only run if setup hasn't completed yet
    with setup_lock:
        if setup_state["status"] != "complete":
            setup_thread = threading.Thread(target=run_setup, daemon=True, name="setup-thread")
            setup_thread.start()
            logger.info("Setup triggered after PAT configuration")

    logger.info(f"PAT configured interactively by {user} — rotation started")
    return jsonify({"status": "ok", "user": user, "message": "Token configured. Auto-rotation started."})


@app.route("/api/session", methods=["POST"])
def create_session():
    """Create a new terminal session."""
    # Quick reject before forking a PTY (approximate — authoritative check below)
    with sessions_lock:
        active = len(sessions)
        if active >= MAX_CONCURRENT_SESSIONS:
            return jsonify({"error": f"Maximum {MAX_CONCURRENT_SESSIONS} concurrent sessions reached. Close an existing session first."}), 429

    data = request.get_json(silent=True) or {}
    label = data.get("label", "")
    try:
        master_fd, slave_fd = pty.openpty()
        # Set up environment for the shell — strips PAT, SP creds, registry
        # tokens, and other secrets that must not be readable from the
        # user's terminal. See _build_terminal_shell_env docstring for the
        # full list.
        shell_env = _build_terminal_shell_env(os.environ)
        # Ensure HOME is set correctly
        if not shell_env.get("HOME") or shell_env["HOME"] == "/":
            shell_env["HOME"] = "/app/python/source_code"
        # Add ~/.local/bin to PATH for claude command
        local_bin = f"{shell_env['HOME']}/.local/bin"
        shell_env["PATH"] = f"{local_bin}:{shell_env.get('PATH', '')}"

        # Start shell in ~/projects/ directory
        projects_dir = os.path.join(shell_env["HOME"], "projects")
        os.makedirs(projects_dir, exist_ok=True)

        pid = subprocess.Popen(
            ["/bin/bash"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            preexec_fn=os.setsid,
            env=shell_env,
            cwd=projects_dir
        ).pid
        os.close(slave_fd)  # Parent doesn't need the slave side; child inherited it

        session_id = str(uuid.uuid4())

        with sessions_lock:
            # Authoritative check under the same lock as insertion — prevents
            # TOCTOU race where two concurrent requests both pass the early check.
            active = len(sessions)
            if active >= MAX_CONCURRENT_SESSIONS:
                os.close(master_fd)
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
                return jsonify({"error": f"Maximum {MAX_CONCURRENT_SESSIONS} concurrent sessions reached. Close an existing session first."}), 429
            sessions[session_id] = {
                "master_fd": master_fd,
                "pid": pid,
                "output_buffer": deque(maxlen=1000),
                "lock": threading.Lock(),
                "last_poll_time": time.time(),
                "created_at": time.time(),
                "label": label,
            }

        # Start background reader thread
        thread = threading.Thread(target=read_pty_output, args=(session_id, master_fd), daemon=True)
        thread.start()

        # Telemetry: track session creation with agent type
        log_telemetry("agent", label or "shell")

        return jsonify({"session_id": session_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── MCP Integration Helpers ──────────────────────────────────────────


def mcp_create_pty_session(
    label: str = "hermes-mcp",
    transcript_path: str | None = None,
    replay_only: bool = False,
    cwd: str | None = None,
) -> str:
    """Create a PTY session for MCP use. Returns the PTY session_id."""
    with sessions_lock:
        active = len(sessions)
        if active >= MAX_CONCURRENT_SESSIONS:
            raise RuntimeError(
                f"Maximum {MAX_CONCURRENT_SESSIONS} concurrent sessions reached."
            )

    master_fd, slave_fd = pty.openpty()

    # Strip PAT, SP creds, registry tokens, and other secrets that must not be
    # readable from the agent's PTY. See _build_terminal_shell_env docstring
    # for the full list. (F-01)
    shell_env = _build_terminal_shell_env(os.environ)
    if not shell_env.get("HOME") or shell_env["HOME"] == "/":
        shell_env["HOME"] = "/app/python/source_code"
    local_bin = f"{shell_env['HOME']}/.local/bin"
    shell_env["PATH"] = f"{local_bin}:{shell_env.get('PATH', '')}"

    projects_dir = os.path.join(shell_env["HOME"], "projects")
    os.makedirs(projects_dir, exist_ok=True)

    # When caller passes cwd, use it; otherwise fall back to projects_dir
    # (preserves current behavior for existing callers that don't pass cwd).
    spawn_cwd = cwd if cwd is not None else projects_dir

    pid = subprocess.Popen(
        ["/bin/bash"],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        preexec_fn=os.setsid,
        env=shell_env,
        cwd=spawn_cwd,
    ).pid
    os.close(slave_fd)

    # Open transcript file (if requested) before locking the session dict.
    transcript_fh = None
    if transcript_path:
        try:
            parent_dir = os.path.dirname(transcript_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            transcript_fh = open(transcript_path, "ab", buffering=0)
            os.fchmod(transcript_fh.fileno(), 0o600)
        except OSError as exc:
            logger.warning("Could not open transcript at %s: %s", transcript_path, exc)
            transcript_fh = None

    session_id = str(uuid.uuid4())

    try:
        with sessions_lock:
            active = len(sessions)
            if active >= MAX_CONCURRENT_SESSIONS:
                os.close(master_fd)
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
                raise RuntimeError(
                    f"Maximum {MAX_CONCURRENT_SESSIONS} concurrent sessions reached."
                )
            sessions[session_id] = {
                "master_fd": master_fd,
                "pid": pid,
                "output_buffer": deque(maxlen=1000),
                "lock": threading.Lock(),
                "last_poll_time": time.time(),
                "created_at": time.time(),
                "label": label,
                "transcript_path": transcript_path if transcript_fh else None,
                "transcript_fh": transcript_fh,
                "transcript_bytes": 0,
                "replay_only": replay_only,
                "cwd": cwd,
            }

        thread = threading.Thread(
            target=read_pty_output, args=(session_id, master_fd), daemon=True
        )
        thread.start()
    except BaseException:
        # Roll back transcript open if anything below it raises before the
        # session is fully wired. The PTY itself is cleaned up by existing
        # error paths; this is just the transcript handle.
        if transcript_fh is not None:
            try:
                transcript_fh.close()
            except Exception:
                pass
        raise

    return session_id


def mcp_send_input(session_id: str, data: str):
    """Send input to a PTY session."""
    session = _get_session(session_id)
    if not session:
        raise RuntimeError(f"Session {session_id} not found")
    with session["lock"]:
        os.write(session["master_fd"], data.encode())


def mcp_close_pty_session(session_id: str):
    """Close a PTY session.

    Project-dir cleanup (for coda_interactive sessions) lives inside
    terminate_session so the idle reaper hits it too.
    """
    session = _get_session(session_id)
    if not session:
        return
    terminate_session(session_id, session["pid"], session["master_fd"])


@app.route("/api/input", methods=["POST"])
def send_input():
    """Send input to the terminal."""
    data = request.json
    session_id = data.get("session_id")
    input_data = data.get("input", "")

    session = _get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    fd = session["master_fd"]

    try:
        os.write(fd, input_data.encode())
        return jsonify({"status": "ok"})
    except OSError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/upload", methods=["POST"])
def upload_file():
    """Save an uploaded file (e.g. clipboard image) and return its path."""
    logger.info(f"Upload request: content_type={request.content_type}, content_length={request.content_length}")

    if "file" not in request.files:
        logger.warning(f"Upload missing 'file' key. Keys: {list(request.files.keys())}")
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    logger.info(f"Upload file: name={f.filename}, content_type={f.content_type}")

    home = os.environ.get("HOME", "/app/python/source_code")
    if not home or home == "/":
        home = "/app/python/source_code"
    upload_dir = os.path.join(home, "uploads")
    os.makedirs(upload_dir, exist_ok=True)

    safe_name = f"{uuid.uuid4().hex[:8]}_{secure_filename(f.filename)}"
    file_path = os.path.join(upload_dir, safe_name)
    f.save(file_path)

    file_size = os.path.getsize(file_path) if os.path.exists(file_path) else 0
    logger.info(f"Upload saved: {file_path} ({file_size} bytes)")

    # Telemetry: track file uploads
    log_telemetry("event", "file_upload")

    return jsonify({"path": file_path})


@app.route("/api/output", methods=["POST"])
def get_output():
    """Get output from the terminal."""
    data = request.json
    session_id = data.get("session_id")

    session = _get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    with session["lock"]:
        session["last_poll_time"] = time.time()
        # Atomic buffer swap: replace buffer, then join outside the lock
        old_buffer = session["output_buffer"]
        session["output_buffer"] = deque(maxlen=1000)
        exited = session.get("exited", False)
        timeout_warning = session.pop("timeout_warning", False)

    output = "".join(old_buffer)

    return jsonify({"output": output, "exited": exited, "shutting_down": shutting_down, "timeout_warning": timeout_warning})


@app.route("/api/output-batch", methods=["POST"])
def get_output_batch():
    """Get output from multiple terminal sessions in one request.

    Accepts: {"session_ids": ["id1", "id2", ...]}
    Returns: {"outputs": {"id1": {"output": "...", "exited": false}, ...}}
    """
    data = request.json or {}
    session_ids = data.get("session_ids")

    if session_ids is None:
        return jsonify({"error": "session_ids required"}), 400

    outputs = {}
    now = time.time()

    # Step 1: Resolve session refs under global lock (fast dict lookups only)
    resolved = {}
    with sessions_lock:
        for sid in session_ids:
            if sid in sessions:
                resolved[sid] = sessions[sid]

    # Step 2: Swap buffers under per-session locks (same pattern as get_output)
    swapped = {}
    for sid, session in resolved.items():
        with session["lock"]:
            session["last_poll_time"] = now
            old_buffer = session["output_buffer"]
            session["output_buffer"] = deque(maxlen=1000)
            exited = session.get("exited", False)
            timeout_warning = session.pop("timeout_warning", False)
        swapped[sid] = (old_buffer, exited, timeout_warning)

    # Step 3: Join strings outside all locks
    for sid, (old_buffer, exited, timeout_warning) in swapped.items():
        outputs[sid] = {
            "output": "".join(old_buffer),
            "exited": exited,
            "timeout_warning": timeout_warning,
        }

    return jsonify({"outputs": outputs, "shutting_down": shutting_down})


@app.route("/api/heartbeat", methods=["POST"])
def heartbeat():
    """Lightweight keep-alive — resets timeout without draining output buffer."""
    data = request.json
    session_id = data.get("session_id")

    session = _get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    with session["lock"]:
        session["last_poll_time"] = time.time()
        timeout_warning = session.pop("timeout_warning", False)
    return jsonify({"status": "ok", "timeout_warning": timeout_warning})


@app.route("/api/resize", methods=["POST"])
def resize_terminal():
    """Resize the terminal."""
    data = request.json
    session_id = data.get("session_id")
    cols = data.get("cols", 80)
    rows = data.get("rows", 24)

    session = _get_session(session_id)
    if not session:
        return jsonify({"error": "Session not found"}), 404

    fd = session["master_fd"]

    try:
        # Set terminal size using TIOCSWINSZ
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        return jsonify({"status": "ok"})
    except OSError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/session/close", methods=["POST"])
def close_session():
    """Gracefully close a terminal session, killing the process."""
    data = request.json
    session_id = data.get("session_id")

    if not session_id:
        return jsonify({"error": "session_id required"}), 400

    session = _get_session(session_id)
    if not session:
        return jsonify({"status": "ok", "detail": "session not found"})

    pid = session["pid"]
    master_fd = session["master_fd"]

    terminate_session(session_id, pid, master_fd)
    logger.info(f"Session {session_id} closed by client")
    return jsonify({"status": "ok"})


def initialize_app(local_dev=False):
    """One-time init: detect owner, start cleanup thread."""
    global app_owner

    # Install SIGTERM handler only for gunicorn (production).
    # For local dev, SIG_DFL is fine — the process just exits cleanly.
    if not local_dev:
        signal.signal(signal.SIGTERM, handle_sigterm)

    # SP credentials preserved — needed for Apps API (owner resolution) and secret persistence

    # Resolve owner: Apps API (app.creator via SP) > PAT (current_user.me)
    app_owner = get_token_owner()
    if app_owner:
        logger.info(f"App owner: {app_owner}")
        os.environ["APP_OWNER"] = app_owner
        app_state.set_app_owner(app_owner)
    else:
        logger.warning("Could not determine app owner - authorization disabled")

    # Strip SP credentials — only needed for owner resolution above.
    # Keeping them causes SDK to silently fall back to SP auth when PAT is dead.
    os.environ.pop("DATABRICKS_CLIENT_ID", None)
    os.environ.pop("DATABRICKS_CLIENT_SECRET", None)
    logger.info("SP credentials stripped — PAT-only auth from this point")

    # Telemetry: app startup ping (fire-and-forget in background thread)
    log_telemetry("event", "app_startup")

    # Start background cleanup thread
    cleanup_thread = threading.Thread(target=cleanup_stale_sessions, daemon=True)
    cleanup_thread.start()
    logger.info(f"Started session cleanup thread (timeout={SESSION_TIMEOUT_SECONDS}s, interval={CLEANUP_INTERVAL_SECONDS}s)")


# ── MCP Endpoint ─────────────────────────────────────────────────────
from coda_mcp.mcp_endpoint import mcp_bp
from coda_mcp.mcp_server import set_app_hooks

app.register_blueprint(mcp_bp)

# Wire MCP tools to PTY infrastructure
set_app_hooks(
    create_session_fn=mcp_create_pty_session,
    send_input_fn=mcp_send_input,
    close_session_fn=mcp_close_pty_session,
)


if __name__ == "__main__":
    # Local dev — no SIGTERM handler (SIG_DFL), no shutting_down flag
    initialize_app(local_dev=True)
    shutting_down = False  # safety net: ensure clean state before serving
    port = int(os.environ.get("DATABRICKS_APP_PORT", 8000))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)
