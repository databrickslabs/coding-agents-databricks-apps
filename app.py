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
from utils import ensure_https
from pat_rotator import PATRotator

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

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# PAT auto-rotation — initialized after sessions dict is defined (see below)

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.secret_key = os.urandom(24)
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32 MB — aligned with Claude Code's 30 MB file limit

# WebSocket support via Flask-SocketIO (simple-websocket transport, threading mode)
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins=[], logger=False, engineio_logger=False)

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
    try:
        socketio.emit('shutting_down', {})
    except Exception:
        pass

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

    home = os.environ.get("HOME", "/app/python/source_code")
    if not home or home == "/":
        home = "/app/python/source_code"

    # 1. Configure Claude CLI (~/.claude/settings.json)
    claude_dir = os.path.join(home, ".claude")
    os.makedirs(claude_dir, exist_ok=True)

    gateway_host = ensure_https(os.environ.get("DATABRICKS_GATEWAY_HOST", "").rstrip("/"))
    databricks_host = ensure_https(os.environ.get("DATABRICKS_HOST", "").rstrip("/"))

    if gateway_host:
        anthropic_base_url = f"{gateway_host}/anthropic"
    else:
        anthropic_base_url = f"{databricks_host}/serving-endpoints/anthropic"

    settings = {
        "env": {
            "ANTHROPIC_MODEL": os.environ.get("ANTHROPIC_MODEL", "databricks-claude-sonnet-4-6"),
            "ANTHROPIC_BASE_URL": anthropic_base_url,
            "ANTHROPIC_AUTH_TOKEN": token,
            "ANTHROPIC_CUSTOM_HEADERS": "x-databricks-use-coding-agent-mode: true",
        }
    }

    settings_path = os.path.join(claude_dir, "settings.json")
    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)

    logger.info(f"Claude CLI auth configured: {settings_path}")

    # 2. Configure Databricks CLI (~/.databrickscfg) — already called by
    #    configure_pat() via pat_rotator, but explicit for clarity
    pat_rotator._write_databrickscfg(token)
    logger.info("Databricks CLI auth configured: ~/.databrickscfg")

    # 3. Re-run Codex, OpenCode, Gemini setup scripts with token in env
    #    They are idempotent: detect CLI already installed, just write config files
    env = {**os.environ, "DATABRICKS_TOKEN": token}
    for script in ["setup_codex.py", "setup_opencode.py", "setup_gemini.py"]:
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

    # --- Sequential prerequisites (git identity + editor) ---
    # Git config — done directly in Python, not as a subprocess
    _update_step("git", status="running", started_at=time.time())
    try:
        _setup_git_config()
        _update_step("git", status="complete", completed_at=time.time())
    except Exception as e:
        _update_step("git", status="error", completed_at=time.time(), error=str(e))

    _run_step("micro", ["bash", "-c",
        "mkdir -p ~/.local/bin && bash install_micro.sh && mv micro ~/.local/bin/ 2>/dev/null || true"])

    _run_step("gh", ["bash", "install_gh.sh"])

    # --- Upgrade Databricks CLI (runtime image ships an older version) ---
    _run_step("dbcli", ["bash", "install_databricks_cli.sh"])

    # --- Content-filter proxy (must be running before OpenCode starts) ---
    # Sanitizes requests/responses between OpenCode and Databricks
    # (see OpenCode #5028, docs/plans/2026-03-11-litellm-empty-content-blocks-design.md)
    _run_step("proxy", ["uv", "run", "python", "setup_proxy.py"])

    # --- Parallel agent setup (all independent of each other) ---
    parallel_steps = [
        ("claude",     ["uv", "run", "python", "setup_claude.py"]),
        ("codex",      ["uv", "run", "python", "setup_codex.py"]),
        ("opencode",   ["uv", "run", "python", "setup_opencode.py"]),
        ("gemini",     ["uv", "run", "python", "setup_gemini.py"]),
        ("databricks", ["uv", "run", "python", "setup_databricks.py"]),
        ("mlflow",     ["uv", "run", "python", "setup_mlflow.py"]),
    ]

    with ThreadPoolExecutor(max_workers=len(parallel_steps)) as executor:
        futures = [
            executor.submit(_run_step, step_id, command)
            for step_id, command in parallel_steps
        ]
        wait(futures)

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
            app = w.apps.get(name=app_name)
            owner = app.creator
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
        return w.current_user.me().user_name
    except Exception as e:
        logger.warning(f"Could not determine token owner: {e}")
        return None


def get_request_user():
    """Extract user email from Databricks Apps request headers."""
    return request.headers.get("X-Forwarded-Email") or \
           request.headers.get("X-Forwarded-User") or \
           request.headers.get("X-Databricks-User-Email")


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
    current_user = request.headers.get("X-Forwarded-Email") or \
                   request.headers.get("X-Forwarded-User") or \
                   request.headers.get("X-Databricks-User-Email")

    if not current_user:
        if _is_databricks_apps():
            logger.warning("No user identity in WebSocket request on Databricks Apps — denying")
            return False
        return True  # Local dev only

    if current_user != app_owner:
        logger.warning(f"WebSocket unauthorized: {current_user} (owner: {app_owner})")
        return False
    return True


# ── WebSocket Event Handlers ──────────────────────────────────────────────

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
                output = os.read(fd, 4096)
                if not output:
                    # EOF — process exited
                    break
                decoded = output.decode(errors="replace")
                with session_lock:
                    # Buffer for HTTP polling fallback (AC-15)
                    session["output_buffer"].append(decoded)
                    session["last_poll_time"] = time.time()  # Keep session alive during WS output
                # Push via WebSocket to the session room (AC-8)
                try:
                    socketio.emit('terminal_output',
                                  {'session_id': session_id, 'output': decoded},
                                  room=session_id)
                except Exception:
                    pass  # No WebSocket clients — HTTP polling handles it
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
    try:
        socketio.emit('session_exited', {'session_id': session_id}, room=session_id)
    except Exception:
        pass

    logger.info(f"Session {session_id} process exited")

    # Clean up immediately — no zombie sessions in the picker
    if session:
        terminate_session(session_id, session["pid"], session["master_fd"])


def terminate_session(session_id, pid, master_fd):
    """Gracefully terminate a session: SIGHUP -> wait -> SIGKILL -> cleanup."""
    logger.info(f"Terminating stale session {session_id} (pid={pid})")

    # Notify WebSocket clients that the session is closed
    try:
        socketio.emit('session_closed', {'session_id': session_id}, room=session_id)
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

    with sessions_lock:
        sessions.pop(session_id, None)


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
    if request.path in ("/health", "/api/setup-status", "/api/pat-status", "/api/configure-pat", "/api/app-state", "/api/sessions", "/api/session/attach") or request.path.startswith("/socket.io"):
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


@app.route("/api/session/attach", methods=["POST"])
def attach_session():
    """Reattach to an existing session — returns buffered output for replay."""
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")

    sess = _get_session(session_id)
    if not sess or sess.get("exited"):
        return jsonify({"error": "Session not found or exited"}), 404

    # Reset idle clock so the 24h reaper starts fresh
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
    # The user-pasted PAT becomes unused after this (expires per its own lifetime).
    os.environ["DATABRICKS_TOKEN"] = token
    pat_rotator._current_token = token
    pat_rotator._current_token_id = None
    rotated = pat_rotator._rotate_once()
    if rotated:
        token = pat_rotator.token  # use the newly minted token from here on
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
    data = request.get_json(silent=True) or {}
    label = data.get("label", "")
    try:
        master_fd, slave_fd = pty.openpty()
        # Set up environment for the shell
        shell_env = os.environ.copy()
        shell_env["TERM"] = "xterm-256color"
        # Remove Claude Code env vars so the browser terminal isn't seen as nested
        shell_env.pop("CLAUDECODE", None)
        shell_env.pop("CLAUDE_CODE_SESSION", None)
        # Remove DATABRICKS_TOKEN so CLI/SDK reads from ~/.databrickscfg (always
        # current after rotation) instead of inheriting a stale env var snapshot
        shell_env.pop("DATABRICKS_TOKEN", None)
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

        return jsonify({"session_id": session_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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

    # Start background cleanup thread
    cleanup_thread = threading.Thread(target=cleanup_stale_sessions, daemon=True)
    cleanup_thread.start()
    logger.info(f"Started session cleanup thread (timeout={SESSION_TIMEOUT_SECONDS}s, interval={CLEANUP_INTERVAL_SECONDS}s)")


if __name__ == "__main__":
    # Local dev — no SIGTERM handler (SIG_DFL), no shutting_down flag
    initialize_app(local_dev=True)
    shutting_down = False  # safety net: ensure clean state before serving
    port = int(os.environ.get("DATABRICKS_APP_PORT", 8000))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)
