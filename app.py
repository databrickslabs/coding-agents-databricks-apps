import os
import sys
import hmac
import codecs
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
from claude_otel import apply_claude_otel_env
from utils import add_1m_context_suffix, ensure_https, get_gateway_host
from pat_rotator import PATRotator
from sp_token_broker import BROKER_URL_ENV, broker_url, mint_sp_token, start_sp_token_broker
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

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ── Crash breadcrumbs ─────────────────────────────────────────────────
# The app runs a single gunicorn worker, so an unhandled exception in ANY
# background thread (PTY readers, cleanup, setup pool, telemetry) that isn't
# already wrapped can silently take down the process with no traceback. These
# hooks guarantee a full traceback lands in the log first. Registered at import
# so they cover threads spawned before initialize_app().
def _log_uncaught_main(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    logger.critical("UNCAUGHT EXCEPTION in main thread — process may exit",
                    exc_info=(exc_type, exc_value, exc_tb))


def _log_uncaught_thread(args):
    if issubclass(args.exc_type, SystemExit):
        return
    logger.critical("UNCAUGHT EXCEPTION in thread %r — that thread has died",
                    getattr(args.thread, "name", "?"),
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


sys.excepthook = _log_uncaught_main
threading.excepthook = _log_uncaught_thread

# PAT auto-rotation — initialized after sessions dict is defined (see below)

app = Flask(__name__, static_folder='static', static_url_path='/static')


def _resolve_secret_key():
    """Return the Flask secret_key, which signs session cookies.

    Prefers FLASK_SECRET_KEY (typically wired to a Databricks secret in
    app.yaml) so cookies survive worker restarts and stay valid across workers
    if we ever scale beyond one. Falls back to a fresh random key, which is fine
    for local dev — sessions there are short-lived and single-process — but logs
    a warning because in production it silently invalidates every session on
    each restart.
    """
    configured = os.environ.get("FLASK_SECRET_KEY", "").strip()
    if configured:
        return configured.encode()
    logger.warning(
        "FLASK_SECRET_KEY not set — generated an ephemeral key. "
        "Existing sessions will be invalidated on every worker restart. "
        "For production, wire FLASK_SECRET_KEY to a Databricks secret in app.yaml."
    )
    return os.urandom(24)


app.secret_key = _resolve_secret_key()
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32 MB — aligned with Claude Code's 30 MB file limit

# WebSocket support via Flask-SocketIO (simple-websocket transport, threading mode)
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins=[], logger=False, engineio_logger=False)

# Store sessions: {session_id: {"master_fd": fd, "pid": pid, "output_buffer": deque, "lock": Lock, ...}}
# sessions_lock guards dict-level ops (add/remove/iterate); each session["lock"] guards per-session state
sessions = {}
sessions_lock = threading.Lock()

# PAT auto-rotation (short-lived tokens, background refresh)
# Only rotates while active sessions exist — stops when all sessions are reaped.
# Interval/lifetime are env-overridable (PAT_ROTATION_INTERVAL / PAT_TOKEN_LIFETIME)
# via the module-level defaults in pat_rotator.py; the workshop overlay sets them
# for the shared box, while secure boxes keep 10-minute rotation / 15-minute lifetime.
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
    # Record uptime + active session count with the signal so the log shows
    # whether the platform reaped us mid-load vs. a clean redeploy.
    try:
        _sess = len(sessions)
    except Exception:
        _sess = "?"
    logger.warning("SIGTERM received after %.0fs uptime (%s active sessions) "
                   "— platform is stopping this worker",
                   time.time() - _start_time, _sess)
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
        {"id": "pi",         "label": "Configuring Pi CLI",           "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "codex",      "label": "Configuring Codex CLI",        "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "opencode",   "label": "Configuring OpenCode CLI",     "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "gemini",     "label": "Configuring Gemini CLI",       "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "hermes",     "label": "Configuring Hermes Agent",     "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "databricks", "label": "Setting up Databricks CLI",    "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "mlflow",     "label": "Enabling MLflow tracing",       "status": "pending", "started_at": None, "completed_at": None, "error": None},
    ]
}

# Workshop deploys (app.yaml.workshop) preload the private challenge repo at
# container startup (spec A-R7). Register the step in the setup UI only when
# configured so normal deploys are unaffected.
if os.environ.get("CHALLENGE_REPO_URL"):
    setup_state["steps"].append(
        {"id": "challenge", "label": "Preloading challenge repo", "status": "pending", "started_at": None, "completed_at": None, "error": None}
    )


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
_omnigent_sp_creds = None
_sp_token_broker_server = None


def _owner_check_disabled() -> bool:
    """True when the operator has opted OUT of the single-user owner binding.

    Set CODA_DISABLE_OWNER_CHECK=true for a shared, trusted, time-boxed
    deployment (e.g. a workshop) where every attendee drives the terminal as
    the single injected PAT identity. This ONLY opens the terminal + WebSocket
    auth — the owner-gated write endpoints (configure-pat, omnigent-host/share)
    stay owner-only so an attendee can't overwrite the shared PAT or mis-grant
    the Omnigent host. app_owner is still resolved normally, so the Omnigent
    integration is unaffected. Off by default; fail-closed remains the norm.
    """
    return os.environ.get("CODA_DISABLE_OWNER_CHECK", "").strip().lower() in (
        "true", "1", "yes"
    )


def _venv_python():
    """Return the interpreter that runs the app.

    On the Databricks Apps runtime gunicorn runs inside the uv-managed venv,
    so ``sys.executable`` already has every declared dependency (including
    databricks-sdk) importable. Invoking setup scripts with this interpreter
    directly removes the need for ``uv run python`` (which re-resolves the
    environment on every call and depends on ``uv`` being on PATH).
    """
    return sys.executable


def _run_step(step_id, command):
    _update_step(step_id, status="running", started_at=time.time())
    try:
        env = os.environ.copy()
        if not env.get("HOME") or env["HOME"] == "/":
            env["HOME"] = "/app/python/source_code"
        home = env.get("HOME", "/app/python/source_code")
        # Ensure uv and other tools in ~/.local/bin are on PATH. Still needed
        # for `uv tool install` (Hermes) and any script that shells out to uv.
        local_bin = os.path.join(home, ".local", "bin")
        if local_bin not in env.get("PATH", ""):
            env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"
        # Expose the venv interpreter so child scripts that need to re-exec a
        # dependency-complete Python (e.g. Claude's apiKeyHelper) can use it
        # instead of shelling out to `uv run`.
        env.setdefault("CODA_VENV_PYTHON", _venv_python())
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
    - ``CHALLENGE_REPO_READ_TOKEN`` — workshop-only read token for the
      startup challenge-repo clone; must never be exposed in attendee
      terminals.
    """
    shell_env = base_env.copy()
    shell_env["TERM"] = "xterm-256color"
    lc_all = shell_env.get("LC_ALL")
    locale_value = lc_all if lc_all else shell_env.get("LANG", "")
    if not locale_value.replace("-", "").replace("_", "").lower().endswith("utf8"):
        shell_env["LANG"] = "C.UTF-8"
        shell_env["LC_ALL"] = "C.UTF-8"
    if shell_env.get("ENABLE_SP_APIKEYHELPER", "").strip().lower() in ("true", "1", "yes"):
        shell_env["DATABRICKS_CONFIG_PROFILE"] = "omnigents-host"

    # Always-strip fixed names
    for key in (
        "CLAUDECODE", "CLAUDE_CODE_SESSION",
        "DATABRICKS_TOKEN", "DATABRICKS_HOST",
        "GEMINI_API_KEY",
        "NPM_TOKEN", "UV_DEFAULT_INDEX",
        "CHALLENGE_REPO_READ_TOKEN",
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


# Home-level agent context, fanned out to GEMINI.md / PI.md by the setup_*.py
# scripts (they read this exact path). Regenerated at every boot so the
# ephemeral-container operating rules survive a disk recycle — a hand-edited
# ~/CLAUDE.md would not (home is not a git repo, so it is never synced).
_HOME_CLAUDE_MD = '''# Coding Agents on Databricks (CoDA)

Global operating context for every AI coding agent in this environment — Claude
Code, Codex, Gemini CLI, Hermes Agent, OpenCode. (This file is fanned out to
`GEMINI.md` / `PI.md` at boot, so all agents inherit what's here.)

---

## \u26a0\ufe0f 0. This is an EPHEMERAL container — a git commit is your only backup

CoDA runs inside a Databricks App container whose disk can be recycled at any
time (redeploy, restart, timeout, platform recycle). Local disk is scratch.

A `post-commit` hook auto-syncs every repo under `~/projects/` to Databricks
Workspace at `/Workspace/Shared/coda/{app-name}/{repo}/`. **Nothing that isn't
committed survives a recycle.** So:

> ⚠ **Shared CoDA:** this container's filesystem (`~/projects/`, git config, and
> the Workspace sync-back) is **shared with everyone else on this app**. Two
> people committing the same repo trample each other. Work in your **own git
> worktree or branch** (`git worktree add ../<you>-<branch> -b <branch>`) — don't
> commit on top of someone else's tree.

1. **Commit small and commit often.** After every self-contained change — a
   working function, a passing test, a fixed bug — commit. Never batch a whole
   session into one commit; a recycle mid-session loses all of it.
2. **A commit == a backup.** The commit triggers the sync. "I'll commit at the
   end" is how work gets lost here.
3. **Verify the sync happened.** After committing, check `tail ~/.sync.log` for
   a `\u2713 Synced to /Workspace/...` line. If you see `\u26a0 Sync failed`, fix it
   before continuing — an unsynced commit is not a backup.
4. **After a recycle, restore before new work.** If a project dir is missing or
   stale, rehydrate it from Workspace with
   `python /app/python/source_code/restore_from_workspace.py <repo-name>`
   *before* rebuilding from memory.
5. **NEVER move or import `.git` into the Workspace.** If you run
   `databricks workspace import`, exclude `.git` — moving it corrupts the repo
   and breaks the sync/restore round-trip. This rule has bitten people
   repeatedly.

Recovery cheat-sheet:
```bash
tail -n 20 ~/.sync.log                                   # did my commits sync?
python /app/python/source_code/restore_from_workspace.py <repo-name>   # rehydrate
python /app/python/source_code/sync_to_workspace.py "$(git rev-parse --show-toplevel)"  # manual re-sync
```

---

## 1. Start every project in git

Before creating any new project or docs, initialize git first — that's what
makes the workspace backup work:
```bash
mkdir ~/projects/my-project && cd ~/projects/my-project && git init
# or: git clone https://github.com/user/repo.git   (into ~/projects/)
```
Only repos inside `~/projects/` are synced.

---

## 2. Working conventions (shared)

- One logical change per commit; imperative commit messages; work on a branch.
- Make the smallest change that satisfies the task — no unrequested refactors.
- Understand every line you submit; code review is the bottleneck.
- Never commit secrets, `.env` files, or credentials.
- A repo may have its own `AGENTS.md` / `CLAUDE.md` — those take precedence for
  project-specific setup, conventions, and gotchas.

---

## 3. Databricks CLI

Pre-configured with your credentials. Test: `databricks current-user me`.
Authenticate with a PAT **or** a `CLIENT_ID`/`CLIENT_SECRET` pair — not both. If
login misbehaves, unset `DATABRICKS_CLIENT_ID` + `DATABRICKS_CLIENT_SECRET` and
retry so access is based only on the owner's credentials.
```bash
databricks workspace list /Workspace/Users/
databricks jobs list
databricks clusters list
```

---

## 4. What's installed

- **5 agents**: Claude Code, Codex, Gemini CLI, Hermes Agent (`hermes chat`),
  OpenCode.
- **Skills**: Databricks skills (ai-dev-kit) + Superpowers dev-workflow skills.
- **MCP servers**: DeepWiki (ask any GitHub repo), Exa (web search).
- **Micro editor**, GitHub CLI, tmux.

---

## 5. Architecture (one-liner)

Real-time terminal I/O over WebSocket (Flask-SocketIO) with HTTP-polling
fallback. Single gunicorn worker (PTY fds are process-local), 16 gthread
threads. Parallel agent setup at startup via ThreadPoolExecutor.

---

## Credits
- Databricks skills: [databricks-solutions/ai-dev-kit](https://github.com/databricks-solutions/ai-dev-kit)
- Dev-workflow skills: [obra/superpowers](https://github.com/obra/superpowers)
'''


def _write_home_claude_md():
    """Write the home-level CLAUDE.md that all agents inherit.

    Regenerated at boot because home is not a git repo (never synced), so a
    hand-edited copy would evaporate on the next container recycle. The
    setup_*.py fan-out reads this exact path to derive GEMINI.md / PI.md.
    """
    target = "/app/python/source_code/CLAUDE.md"
    try:
        with open(target, "w") as f:
            f.write(_HOME_CLAUDE_MD)
        logger.info(f"Home-level agent context written to {target}")
    except Exception as e:
        logger.warning(f"Could not write home-level CLAUDE.md: {e}")


def _setup_git_config():
    """Configure git identity and hooks by writing files directly (no subprocess)."""
    home = os.environ.get("HOME", "/app/python/source_code")
    if not home or home == "/":
        home = "/app/python/source_code"

    # Regenerate the home-level agent context (all agents inherit it; fanned out
    # to GEMINI.md / PI.md). Done here so it's refreshed on every boot/recycle.
    _write_home_claude_md()

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
        # Use the app's own venv interpreter (dependency-complete) so the sync
        # script runs with the right Python + deps without shelling out to uv.
        f.write('APP_DIR="/app/python/source_code"\n')
        f.write('SYNC_SCRIPT="$APP_DIR/sync_to_workspace.py"\n')
        f.write(f'APP_PYTHON="{_venv_python()}"\n')
        f.write('# Fall back to uv if the recorded interpreter is missing.\n')
        f.write('if [ ! -x "$APP_PYTHON" ]; then\n')
        f.write('    APP_PYTHON="uv run --project $APP_DIR python"\n')
        f.write('fi\n')
        f.write('\n')
        f.write('if [ -f "$SYNC_SCRIPT" ]; then\n')
        f.write('    nohup $APP_PYTHON "$SYNC_SCRIPT" "$REPO_ROOT" >> "$SYNC_LOG" 2>&1 & disown\n')
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
    settings["env"]["ANTHROPIC_MODEL"] = os.environ.get("ANTHROPIC_MODEL", "databricks-claude-opus-4-8")
    settings["env"]["ANTHROPIC_BASE_URL"] = anthropic_base_url
    # Respect the spec-C apiKeyHelper: when it owns model auth (setup_claude.py
    # installed the "apiKeyHelper" key), don't re-pin a static token here — the
    # helper fetches its own per-TTL. Otherwise write the PAT as before.
    if settings.get("apiKeyHelper"):
        settings["env"].pop("ANTHROPIC_AUTH_TOKEN", None)
    else:
        settings["env"]["ANTHROPIC_AUTH_TOKEN"] = token
    # [1m] suffix requests the 1M context window via the gateway (opus/sonnet
    # only; see utils.add_1m_context_suffix). Keep in sync with setup_claude.py.
    settings["env"]["ANTHROPIC_DEFAULT_OPUS_MODEL"] = add_1m_context_suffix("databricks-claude-opus-4-8")
    settings["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] = add_1m_context_suffix("databricks-claude-sonnet-4-6")
    settings["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = "databricks-claude-haiku-4-5"
    settings["env"]["ANTHROPIC_CUSTOM_HEADERS"] = "x-databricks-use-coding-agent-mode: true"
    settings["env"]["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"
    if apply_claude_otel_env(settings, token, databricks_host):
        logger.info("Claude Code OTEL export configured")

    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)

    logger.info(f"Claude CLI auth configured: {settings_path}")

    # 2. Configure Databricks CLI (~/.databrickscfg) — already called by
    #    configure_pat() via pat_rotator, but explicit for clarity
    pat_rotator._write_databrickscfg(token)
    logger.info("Databricks CLI auth configured: ~/.databrickscfg")

    # 3. Re-run Codex, OpenCode, Gemini setup scripts with token in env
    #    They are idempotent: detect CLI already installed, just write config files
    env = {**os.environ, "DATABRICKS_TOKEN": token,
           "CODA_VENV_PYTHON": _venv_python()}
    for script in ["setup_pi.py", "setup_codex.py", "setup_opencode.py", "setup_gemini.py", "setup_hermes.py"]:
        try:
            result = subprocess.run(
                [_venv_python(), script],
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
        "mkdir -p ~/.local/bin && bash install_micro.sh && mv micro ~/.local/bin/ 2>/dev/null || true"])

    _run_step("gh", ["bash", "install_gh.sh"])


    # tmux — required by Omnigent's native claude/codex harnesses (they launch
    # the agent through a local tmux terminal and refuse to start without it).
    _run_step("tmux", ["bash", "install_tmux.sh"])

    # --- Upgrade Databricks CLI (runtime image ships an older version) ---
    _run_step("dbcli", ["bash", "install_databricks_cli.sh"])

    # --- Content-filter proxy (must be running before OpenCode starts) ---
    # Sanitizes requests/responses between OpenCode and Databricks
    # (see OpenCode #5028, docs/plans/2026-03-11-litellm-empty-content-blocks-design.md)
    _py = _venv_python()
    _run_step("proxy", [_py, "setup_proxy.py"])

    # --- Parallel agent setup (all independent of each other) ---
    parallel_steps = [
        ("claude",     [_py, "setup_claude.py"]),
        ("pi",         [_py, "setup_pi.py"]),
        ("codex",      [_py, "setup_codex.py"]),
        ("opencode",   [_py, "setup_opencode.py"]),
        ("gemini",     [_py, "setup_gemini.py"]),
        ("hermes",     [_py, "setup_hermes.py"]),
        ("databricks", [_py, "setup_databricks.py"]),
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
    _run_step("mlflow", [_py, "setup_mlflow.py"])

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
    # Shared-app opt-out (workshops): allow any authenticated proxy user.
    if _owner_check_disabled():
        return True, None

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
    # Shared-app opt-out (workshops): mirror check_authorization().
    if _owner_check_disabled():
        return True

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
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def emit_output(decoded):
        if not decoded:
            return
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
                    emit_output(decoder.decode(b"", final=True))
                    break
                emit_output(decoder.decode(output))
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


RESOURCE_MONITOR_INTERVAL_SECONDS = int(
    os.environ.get("RESOURCE_MONITOR_INTERVAL", "60")
)


def _process_tree_rss_mb():
    """Total RSS (MB) of this process + its descendants (the agent children).

    Best-effort via /proc; returns None if unavailable (non-Linux/local dev).
    Walks the process tree from our own PID so it captures the memory the
    spawned agents (Claude/Pi/OpenCode) actually consume, not just the worker.
    """
    try:
        import glob
        # Build pid -> (ppid, rss_pages) from /proc/*/stat
        pgsize = os.sysconf("SC_PAGE_SIZE")
        procs = {}
        for stat_path in glob.glob("/proc/[0-9]*/stat"):
            try:
                with open(stat_path) as f:
                    fields = f.read().split()
                # Fields after comm can shift if comm has spaces; use rsplit on ')'.
                pid = int(fields[0])
                after = stat_path  # unused
                rest = fields
                ppid = int(rest[3])
                rss_pages = int(rest[23])
                procs[pid] = (ppid, rss_pages)
            except (OSError, ValueError, IndexError):
                continue
        me = os.getpid()
        # BFS over descendants
        total_pages = procs.get(me, (0, 0))[1]
        frontier = {me}
        seen = {me}
        children_by_ppid = {}
        for pid, (ppid, _) in procs.items():
            children_by_ppid.setdefault(ppid, []).append(pid)
        while frontier:
            nxt = set()
            for p in frontier:
                for c in children_by_ppid.get(p, []):
                    if c not in seen:
                        seen.add(c)
                        nxt.add(c)
                        total_pages += procs.get(c, (0, 0))[1]
            frontier = nxt
        return round(total_pages * pgsize / (1024 * 1024))
    except Exception:
        return None


def resource_pressure_monitor():
    """Periodically log memory / thread / session pressure.

    Read-only observability: gives a crash a runway of telemetry instead of a
    single silent moment, and surfaces per-session memory so the operator can
    tune MAX_CONCURRENT_SESSIONS from data. Never raises — monitoring must not
    be able to take down the process it's watching.
    """
    while True:
        time.sleep(RESOURCE_MONITOR_INTERVAL_SECONDS)
        try:
            with sessions_lock:
                n_sessions = len(sessions)
            n_threads = threading.active_count()
            tree_rss = _process_tree_rss_mb()
            try:
                import resource as _res
                # ru_maxrss is KB on Linux — peak RSS of THIS process only.
                peak_self_mb = round(
                    _res.getrusage(_res.RUSAGE_SELF).ru_maxrss / 1024
                )
            except Exception:
                peak_self_mb = None
            try:
                open_fds = len(os.listdir(f"/proc/{os.getpid()}/fd"))
            except Exception:
                open_fds = None
            per_session = (
                round(tree_rss / n_sessions) if tree_rss and n_sessions else None
            )
            logger.info(
                "RESOURCE: sessions=%s/%s threads=%s tree_rss=%sMB "
                "per_session=%sMB peak_self=%sMB open_fds=%s",
                n_sessions, MAX_CONCURRENT_SESSIONS, n_threads,
                tree_rss, per_session, peak_self_mb, open_fds,
            )
        except Exception as e:
            logger.warning("RESOURCE monitor iteration failed: %s", e)


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
    # Auth-exempt:
    #   /health            — liveness probe. Stays reachable for the platform,
    #                        but trims its body for unauthenticated callers;
    #                        see health() for what each audience sees.
    #   /api/configure-pat — owner-gates itself in-handler (cannot use the
    #                        before_request gate; needed during bootstrap before
    #                        app_owner is resolved). See configure_pat() guard.
    #   /api/inject-pat    — gated on the CODA_BOOTSTRAP_SECRET shared secret,
    #                        and 404s when that env var is unset. Provisioning
    #                        scripts have no SSO session, so it can't use the
    #                        SSO gate. See inject_pat().
    #   /socket.io/*       — has own auth gate via the 'connect' WS event
    #
    # Previously exempt but now owner-gated (closed unauth info-disclosure
    # surface): /api/setup-status, /api/pat-status, /api/app-state. All three
    # are only polled by the frontend, which loads from "/" (auth'd) so already
    # has SSO cookies — no functional regression.
    if request.path in (
        "/health", "/api/configure-pat", "/api/inject-pat",
    ) or request.path.startswith("/socket.io"):
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
    # Two audiences, two response shapes:
    #
    #   unauthenticated — {"status": "healthy"|"degraded"} and nothing else.
    #     Version, session counts, setup state and rotator internals all enable
    #     version-targeted exploit selection or leak the app's auth posture to
    #     anyone who can reach the URL.
    #   the owner — the full diagnostic payload below.
    #
    # `status` itself stays visible to everyone: a liveness probe that can't
    # report unhealthiness is useless, and "degraded" is the signal that makes
    # a zombie app (worker answering, PAT rotation dead) observable at all.
    with sessions_lock:
        session_count = len(sessions)
    with setup_lock:
        current_setup_status = setup_state["status"]

    # Meaningful liveness signal. Previously this returned "healthy"
    # unconditionally as long as the worker could answer — so an app whose PAT
    # rotation had silently died (auth expiring, the suspected coda-02 window)
    # still reported healthy until every call started 401-ing. Report the auth
    # sub-state so a zombie is observable. Best-effort: never let the health
    # endpoint itself raise.
    auth = {}
    try:
        # Only meaningful once a token has been configured (post PAT paste /
        # SP-auth boot). Before that, setup_status carries the real state.
        if pat_rotator.token:
            auth = {
                "rotator_alive": pat_rotator.is_alive,
                "token_expired": pat_rotator.is_token_expired,
                "seconds_since_rotation": (
                    round(pat_rotator.seconds_since_rotation)
                    if pat_rotator.seconds_since_rotation is not None else None
                ),
            }
    except Exception:
        auth = {"error": "unavailable"}

    # "degraded" when auth machinery has failed while the app claims to be up:
    # rotator thread died, or the token has aged past its lifetime.
    degraded = bool(auth.get("token_expired")) or (
        pat_rotator.token is not None and auth.get("rotator_alive") is False
    )

    status = "degraded" if degraded else "healthy"

    authorized, _ = check_authorization()
    if not authorized:
        return jsonify({"status": status})

    return jsonify({
        "status": status,
        "version": APP_VERSION,
        "setup_status": current_setup_status,
        "active_sessions": session_count,
        "session_timeout_seconds": SESSION_TIMEOUT_SECONDS,
        "auth": auth,
    })


@app.route("/api/version")
def get_version():
    return jsonify({"version": APP_VERSION})


@app.route("/api/omnigents-status")
def omnigents_status():
    """Report Omnigents host-integration state (FR-9 observability)."""
    from omnigents_host import get_status
    return jsonify(get_status())


@app.route("/api/omnigent-host/status")
def omnigent_host_status():
    """Report runtime Omnigent host state."""
    from omnigents_host import get_status
    return jsonify(get_status())


@app.route("/api/omnigent-host/connect", methods=["POST"])
def omnigent_host_connect():
    """Start a runtime Omnigent host tunnel for a supplied server URL."""
    data = request.get_json(silent=True) or {}
    server_url = (data.get("server_url") or "").strip()
    if not server_url:
        return jsonify({"error": "server_url required"}), 400

    from omnigents_host import connect_host
    ok, status = connect_host(server_url, _omnigent_sp_creds)
    if not ok:
        code = 409 if status.get("last_error") == "host already running" else 400
        return jsonify(status), code
    return jsonify(status)


@app.route("/api/omnigent-host/disconnect", methods=["POST"])
def omnigent_host_disconnect():
    """Stop the active runtime Omnigent host tunnel, if any."""
    from omnigents_host import disconnect_host
    return jsonify(disconnect_host())


@app.route("/api/omnigent-host/share", methods=["POST"])
def omnigent_host_share():
    """Share this SP-owned host with the app owner so it shows in their picker.

    The host is owned by the app SP, so the operator's personal Omnigent UI
    can't see it until the owner (SP) grants them ``use``. This issues that
    grant — and optionally launches a runner — using the captured SP creds.
    Owner-gated identically to configure-pat: only the resolved app owner may
    invoke it, since it acts with the SP's authority.
    """
    if _is_databricks_apps() and app_owner:
        if get_request_user() != app_owner:
            return jsonify({"error": "Forbidden"}), 403

    from omnigents_host import get_status
    server_url = os.environ.get("OMNIGENTS_SERVER_URL", "").strip() or str(
        get_status().get("server_url") or ""
    ).strip()
    if not server_url:
        return jsonify({"error": "no server_url; connect the host first"}), 400

    grant_user = get_request_user() or app_owner
    if not grant_user:
        return jsonify({"error": "could not resolve a user to grant"}), 400

    data = request.get_json(silent=True) or {}
    launch = bool(data.get("launch", True))

    from omnigents_host import share_and_launch
    result = share_and_launch(server_url, _omnigent_sp_creds, grant_user, launch=launch)
    return jsonify(result), (200 if result.get("ok") else 502)


@app.route("/api/pat-status")
def pat_status():
    """Check if a valid, usable PAT is configured."""
    host = ensure_https(os.environ.get("DATABRICKS_HOST", ""))
    token = os.environ.get("DATABRICKS_TOKEN", "").strip()

    if (
        _omnigent_sp_creds
        and os.environ.get("ENABLE_SP_APIKEYHELPER", "").strip().lower()
        in ("true", "1", "yes")
    ):
        return jsonify({
            "auth_mode": "sp_oauth",
            "configured": True,
            "valid": True,
            "user": app_owner or "app-service-principal",
        })

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


def _bootstrap_pat(token):
    """Validate a PAT, adopt it, mint a controlled short-lived token, start
    rotation, configure all CLIs, and trigger setup.

    Shared by the interactive owner endpoint (/api/configure-pat) and the
    programmatic endpoint (/api/inject-pat). Returns (ok, payload, status_code)
    where payload is a JSON-serializable dict. Does NOT enforce authorization —
    callers own that (SSO owner-gate vs. shared-secret gate).
    """
    token = (token or "").strip()
    if not token:
        return False, {"error": "Token required"}, 400

    # Validate the token — direct HTTP, no SDK fallback
    host = ensure_https(os.environ.get("DATABRICKS_HOST", ""))
    try:
        resp = requests.get(f"{host}/api/2.0/preview/scim/v2/Me",
                            headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if resp.status_code != 200:
            return False, {"error": "Invalid token"}, 400
        user = resp.json().get("userName", "unknown")
    except Exception as e:
        return False, {"error": f"Token validation failed: {e}"}, 400

    # Immediately mint a controlled short-lived token from the supplied PAT.
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
        # Rotation failed — fall back to supplied token (still valid)
        pat_rotator._write_databrickscfg(token)
    pat_rotator.start()

    # Configure all CLI tools (Claude, Codex, OpenCode, Gemini, Databricks)
    _configure_all_cli_auth(pat_rotator.token or token)

    # Run setup now that we have a valid token (installs CLIs, configures agents)
    with setup_lock:
        if setup_state["status"] != "complete":
            threading.Thread(target=run_setup, daemon=True, name="setup-thread").start()
            logger.info("Setup triggered after PAT configuration")

    return True, {
        "status": "ok",
        "user": user,
        "instance": pat_rotator.instance_name,
        "message": "Token configured. Auto-rotation started.",
    }, 200


@app.route("/api/inject-pat", methods=["POST"])
def inject_pat():
    """Programmatically inject a PAT for THIS CoDA (no SSO required).

    Designed for provisioning many CoDAs in a workspace from a script: each
    CoDA gets its own distinct PAT, and auto-rotated tokens are tagged with
    this instance's name (see pat_rotator.rotation_comment).

    Auth: requires a shared bootstrap secret. Set CODA_BOOTSTRAP_SECRET in the
    app config, then send it as the ``X-Coda-Bootstrap-Secret`` header (or
    ``Authorization: Bearer <secret>``). If the env var is unset, this endpoint
    is DISABLED (returns 404) so it can't be abused on boxes that don't opt in.

    Body: {"token": "dapiXXXX"}
    """
    expected = os.environ.get("CODA_BOOTSTRAP_SECRET", "").strip()
    if not expected:
        # Not opted in — behave as if the route doesn't exist.
        return jsonify({"error": "Not found"}), 404

    provided = (
        request.headers.get("X-Coda-Bootstrap-Secret", "")
        or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    )
    # Constant-time compare to avoid leaking the secret via timing.
    if not provided or not hmac.compare_digest(provided, expected):
        logger.warning(f"Rejected inject-pat: bad/absent bootstrap secret (source={request.remote_addr})")
        return jsonify({"error": "Forbidden"}), 403

    # Single-shot, same as configure-pat: refuse if a live PAT is already set,
    # unless it has expired (idle-timeout re-bootstrap path).
    if pat_rotator.token and not pat_rotator.is_token_expired:
        return jsonify({
            "error": "PAT already configured. Restart the app to reconfigure."
        }), 409

    data = request.get_json(silent=True) or {}
    ok, payload, status = _bootstrap_pat(data.get("token", ""))
    if ok:
        logger.info(
            f"PAT injected programmatically for instance "
            f"'{pat_rotator.instance_name or '<unnamed>'}' "
            f"(user={payload.get('user')}) — rotation started"
        )
    return jsonify(payload), status


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

    data = request.json or {}
    ok, payload, status = _bootstrap_pat(data.get("token", ""))
    if ok:
        logger.info(f"PAT configured interactively by {payload.get('user')} — rotation started")
    return jsonify(payload), status


@app.route("/api/session", methods=["POST"])
def create_session():
    """Create a new terminal session."""
    # Quick reject before forking a PTY (approximate — authoritative check below)
    with sessions_lock:
        if len(sessions) >= MAX_CONCURRENT_SESSIONS:
            return jsonify({"error": f"Maximum {MAX_CONCURRENT_SESSIONS} concurrent sessions reached. Close an existing session first."}), 429

    data = request.get_json(silent=True) or {}
    label = data.get("label", "")
    try:
        master_fd, slave_fd = pty.openpty()
        # Set up environment for the shell — strips PAT, SP creds, registry
        # tokens, the workshop challenge-repo token, and other secrets that
        # must not be readable from the user's terminal. See
        # _build_terminal_shell_env docstring for the full list.
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

        # Workshop: open the terminal directly inside the preloaded challenge
        # repo when it exists (A-R7 — attendees start in the repo, no cd/clone).
        challenge_url = os.environ.get("CHALLENGE_REPO_URL", "")
        if challenge_url:
            repo_name = os.path.basename(challenge_url.rstrip("/")).removesuffix(".git")
            challenge_dir = os.path.join(projects_dir, repo_name)
            if os.path.isdir(challenge_dir):
                projects_dir = challenge_dir

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
            if len(sessions) >= MAX_CONCURRENT_SESSIONS:
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
    global app_owner, _omnigent_sp_creds, _sp_token_broker_server

    # Install SIGTERM handler only for gunicorn (production).
    # For local dev, SIG_DFL is fine — the process just exits cleanly.
    if not local_dev:
        signal.signal(signal.SIGTERM, handle_sigterm)

    # SP credentials preserved — needed for Apps API (owner resolution) and secret persistence

    # Capture the app SP's M2M OAuth creds BEFORE the strip below — the
    # Omnigents host tunnel needs an OAuth token (the Apps proxy rejects PATs).
    # No-op / returns None when disabled or creds absent. See omnigents_host.py.
    from omnigents_host import capture_sp_credentials, start_host
    _omnigent_sp_creds = capture_sp_credentials()

    # Resolve owner: Apps API (app.creator via SP) > PAT (current_user.me)
    app_owner = get_token_owner()
    if app_owner:
        logger.info(f"App owner: {app_owner}")
        os.environ["APP_OWNER"] = app_owner
        app_state.set_app_owner(app_owner)
    else:
        logger.warning("Could not determine app owner - authorization disabled")

    sp_helper_enabled = os.environ.get("ENABLE_SP_APIKEYHELPER", "").strip().lower() in (
        "true", "1", "yes",
    )
    host_enabled = bool(os.environ.get("OMNIGENTS_SERVER_URL", "").strip())
    if _omnigent_sp_creds and (sp_helper_enabled or host_enabled):
        _sp_token_broker_server = start_sp_token_broker(
            lambda: mint_sp_token(_omnigent_sp_creds)
        )
        os.environ[BROKER_URL_ENV] = broker_url(_sp_token_broker_server)
        logger.info("SP token broker listening on loopback")

    # The profile retains only the workspace host for Omnigent routing. The
    # long-lived client secret stays in this process; helpers mint via broker.
    if _omnigent_sp_creds and sp_helper_enabled:
        from omnigents_host import _write_oauth_profile
        _write_oauth_profile(_omnigent_sp_creds)
        logger.info("SP apikeyhelper: wrote secret-free host profile at boot")

    # Strip SP credentials — only needed for owner resolution above.
    # Keeping them causes SDK to silently fall back to SP auth when PAT is dead.
    os.environ.pop("DATABRICKS_CLIENT_ID", None)
    os.environ.pop("DATABRICKS_CLIENT_SECRET", None)
    logger.info("SP credentials stripped — PAT-only auth from this point")

    # Register as an Omnigents host (no-op unless OMNIGENTS_SERVER_URL is set).
    # Uses the SP creds captured above to mint OAuth for the host tunnel; the
    # spawned runner uses CoDA's PAT + AI-Gateway creds for the actual coding.
    start_host(_omnigent_sp_creds)

    # Workshop: preload the private challenge repo at container startup (A-R7).
    # The read token comes from app.yaml env (secret valueFrom), so this does
    # not wait for PAT setup. Background thread — never blocks app boot.
    if os.environ.get("CHALLENGE_REPO_URL"):
        threading.Thread(
            target=_run_step, args=("challenge", ["bash", "install_challenge_repo.sh"]),
            daemon=True, name="challenge-preload",
        ).start()

    # SP-auth workshop path: when the app self-auths as its own SP (profile
    # written above), no PAT paste will ever come — so trigger setup at boot
    # instead of waiting for /api/configure-pat. Installs the agent CLIs and
    # configures them against the SP OAuth token via the apiKeyHelper. Guarded
    # on the same flag + captured creds; background thread, never blocks boot.
    if _omnigent_sp_creds and os.environ.get("ENABLE_SP_APIKEYHELPER", "").strip().lower() in ("true", "1", "yes"):
        with setup_lock:
            already = setup_state["status"] in ("running", "complete")
        if not already:
            threading.Thread(target=run_setup, daemon=True, name="setup-thread-sp").start()
            logger.info("SP apikeyhelper: setup triggered at boot (no PAT paste needed)")

    # Telemetry: app startup ping (fire-and-forget in background thread)
    log_telemetry("event", "app_startup")

    # Start background cleanup thread
    cleanup_thread = threading.Thread(target=cleanup_stale_sessions, daemon=True)
    cleanup_thread.start()
    logger.info(f"Started session cleanup thread (timeout={SESSION_TIMEOUT_SECONDS}s, interval={CLEANUP_INTERVAL_SECONDS}s)")

    # Start resource-pressure monitor. On a 4 vCPU / 12 GB box a couple of heavy
    # agent sessions can approach the memory cliff; this logs a runway of
    # telemetry so a crash isn't a single silent moment, and lets us measure
    # real per-session RSS to tune MAX_CONCURRENT_SESSIONS with data.
    monitor_thread = threading.Thread(target=resource_pressure_monitor, daemon=True,
                                      name="resource-monitor")
    monitor_thread.start()


if __name__ == "__main__":
    # Local dev — no SIGTERM handler (SIG_DFL), no shutting_down flag
    initialize_app(local_dev=True)
    shutting_down = False  # safety net: ensure clean state before serving
    port = int(os.environ.get("DATABRICKS_APP_PORT", 8000))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)
