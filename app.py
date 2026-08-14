import os
import sys
import atexit
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
import re
import shutil
from urllib.parse import urlsplit
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
from token_helper import write_databricks_token_wrapper
from pat_rotator import PATRotator
from sp_token_broker import (
    BROKER_URL_ENV,
    broker_url,
    mint_sp_token,
    start_sp_token_broker,
    stop_sp_token_broker,
)
from telemetry import log_telemetry, set_product_info
from resource_capacity import CapacityDecision, controller_from_env, env_int

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
# Browser PTY sessions are deliberately capped independently of Omnigent host
# runners. The controller below adds a cgroup-v2 memory guard without using
# host-wide memory, which is not a truthful limit inside an Apps container.
MAX_CONCURRENT_SESSIONS = max(1, env_int("MAX_CONCURRENT_SESSIONS", 5))
_browser_capacity = controller_from_env()
#: Browser launches admitted but not yet inserted into ``sessions``. Guarded by
#: ``sessions_lock`` so admission counts active + in-flight atomically.
_browser_pending = 0

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
    logger.warning(
        "SIGTERM received after %.0fs uptime (%s active sessions) "
        "— platform is stopping this worker",
        time.time() - _start_time,
        _sess,
    )
    # Notify WS clients immediately (HTTP poll clients will see shutting_down on next poll)
    try:
        socketio.emit("shutting_down", {})
    except Exception:
        pass
    # Do blocking listener teardown outside the signal callback. The worker
    # fails closed as soon as the teardown thread invalidates the capability.
    threading.Thread(
        target=_shutdown_sp_token_broker,
        daemon=True,
        name="sp-token-broker-shutdown",
    ).start()


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
        {"id": "editors",    "label": "Detecting available editors",  "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "gh",         "label": "Installing GitHub CLI",        "status": "pending", "started_at": None, "completed_at": None, "error": None},
        {"id": "jq",         "label": "Installing jq",                "status": "pending", "started_at": None, "completed_at": None, "error": None},
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
        {"id": "projects",   "label": "Setting up bundled projects",   "status": "pending", "started_at": None, "completed_at": None, "error": None},
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
_sp_token_broker_shutdown_lock = threading.Lock()
_sp_token_broker_atexit_registered = False


def _shutdown_sp_token_broker():
    """Remove the capability coordinate and close the loopback listener."""
    global _sp_token_broker_server
    with _sp_token_broker_shutdown_lock:
        server = _sp_token_broker_server
        os.environ.pop(BROKER_URL_ENV, None)
        stop_sp_token_broker(server)
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
            # The profile-backed broker shim is also needed by direct terminal
            # `databricks` commands, not only Omnigent's SDK model-catalog path.
            # On a no-Omnigent deploy the host setup never calls its installer,
            # leaving databricks_cli_path pointing at a missing wrapper and every
            # workspace CLI call failing with `no cached credentials`. Install it
            # immediately after the real CLI is available.
            if step_id == "dbcli" and os.environ.get(BROKER_URL_ENV):
                _ensure_broker_cli_wrapper()
            _update_step(step_id, status="complete", completed_at=time.time())
        else:
            err = result.stderr.strip() or result.stdout.strip() or "Unknown error"
            _update_step(step_id, status="error", completed_at=time.time(), error=err[:500])
    except subprocess.TimeoutExpired:
        _update_step(step_id, status="error", completed_at=time.time(), error="Timed out after 300s")
    except Exception as e:
        _update_step(step_id, status="error", completed_at=time.time(), error=str(e))


def _ensure_broker_cli_wrapper() -> bool:
    """Install the broker-aware Databricks CLI shim for terminal commands.

    The SDK honours the absolute `databricks_cli_path` in the omnigents-host
    profile, but a user typing `databricks ...` resolves through PATH. Put the
    same shim first in the terminal PATH too. This is needed when
    ENABLE_SP_APIKEYHELPER is on but Omnigent resources are absent: the broker
    starts, the profile is written, but the Omnigent host setup (which used to
    install the shim) is intentionally skipped.
    """
    if not os.environ.get(BROKER_URL_ENV, "").strip():
        return False
    home = os.environ.get("HOME", "/app/python/source_code")
    if not home or home == "/":
        home = "/app/python/source_code"
    expected = os.path.join(home, ".local", "bin", "databricks")
    real_cli = expected if os.path.isfile(expected) else shutil.which("databricks")
    if not real_cli:
        logger.warning("SP broker is running but Databricks CLI is not installed yet; wrapper deferred")
        return False
    wrapper = write_databricks_token_wrapper(os.path.join(home, ".coda-broker-bin"), real_cli)
    logger.info("Installed broker-aware Databricks CLI wrapper at %s", wrapper)
    return True


_TERMINAL_ENV_ALLOWLIST = frozenset({
    # Shell/runtime basics.
    "HOME", "PATH", "USER", "LOGNAME", "SHELL", "TERM", "COLORTERM",
    "LANG", "LANGUAGE", "TZ", "TMPDIR", "EDITOR", "VISUAL", "PAGER",
    "LESS", "NO_COLOR", "FORCE_COLOR",
    # Enterprise network configuration. Credential-bearing proxy URLs are
    # rejected separately below; registry credentials live in private files.
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "no_proxy",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS", "ENTERPRISE_MODE", "UV_HTTP_TIMEOUT",
    "NPM_REGISTRY", "npm_config_registry", "GITHUB_API_BASE",
    "GITHUB_RELEASE_MIRROR", "CLAUDE_INSTALLER_URL", "HERMES_PIP_URL",
    "DEEPWIKI_MCP_URL", "EXA_MCP_URL",
    # Non-secret model and feature selection read by terminal-launched CLIs.
    "ANTHROPIC_MODEL", "PI_MODEL", "GEMINI_MODEL", "CODEX_MODEL",
    "HERMES_MODEL", "HERMES_FALLBACK_MODEL", "ENABLE_FABLE_MODELS",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY", "CLAUDE_CODE_OTEL_ENABLED",
    "MLFLOW_TRACING_ENABLED", "MLFLOW_OSS_TRACKING_ENABLED",
    "PROXY_TRACE_CONTENT", "CODA_OMNIGENT_MODE",
    # Broker/profile plumbing. The broker URL is an intentionally reviewed
    # loopback capability; it is not an ambient bearer or client secret.
    "CODA_VENV_PYTHON", "DATABRICKS_CONFIG_FILE", "DATABRICKS_CONFIG_PROFILE",
    BROKER_URL_ENV,
})
_TERMINAL_ENV_PREFIX_ALLOWLIST = ("LC_", "ENABLE_")
_TERMINAL_URL_VARS = frozenset({
    "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
    "NPM_REGISTRY", "npm_config_registry", "GITHUB_API_BASE",
    "GITHUB_RELEASE_MIRROR", "CLAUDE_INSTALLER_URL", "HERMES_PIP_URL",
    "DEEPWIKI_MCP_URL", "EXA_MCP_URL", BROKER_URL_ENV,
})
_CREDENTIAL_SHAPED_ENV_PATTERN = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSPHRASE|CREDENTIALS?|BEARER|AUTH|SESSION"
    r"|COOKIE|SIGNING|SALT|API[_-]?KEY|PRIVATE[_-]?KEY|CLIENT[_-]?ID"
    r"|(?:^|_)(?:KEY|PAT|PWD)(?:_|$)",
    re.IGNORECASE,
)
_TERMINAL_CREDENTIAL_EXCEPTIONS = frozenset({
    BROKER_URL_ENV,
    # Boolean feature flag; contains APIKEY but never credential material.
    "ENABLE_SP_APIKEYHELPER",
})


def _terminal_url_is_safe(key: str, value: str) -> bool:
    """Reject URL userinfo/malformed ports while accepting Hermes specs."""
    candidate = value
    if key == "HERMES_PIP_URL":
        if not enterprise_config._HERMES_SPEC_RE.match(value):
            return False
        direct_url = re.search(r"git\+(https?://\S+)", value)
        if direct_url is None:
            return True  # Internal-index package spec, e.g. hermes-agent==1.2.3
        candidate = direct_url.group(1)

    try:
        parsed = urlsplit(candidate)
        parsed.port  # Trigger urllib validation for non-numeric/out-of-range ports.
    except ValueError:
        return False
    return (
        parsed.scheme in ("http", "https")
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
    )


def _build_terminal_shell_env(base_env: dict) -> dict:
    """Build a deny-by-default environment for a browser terminal PTY.

    Only explicitly reviewed non-secret names and prefixes are copied from the
    Flask process. A final credential-shaped-name guard makes a future allowlist
    edit fail closed unless the name is also added to the narrowly reviewed
    exception set. Registry credentials remain available through private config
    files; app-SP credentials remain in the Flask process.
    """
    shell_env = {
        key: value
        for key, value in base_env.items()
        if key in _TERMINAL_ENV_ALLOWLIST
        or key.startswith(_TERMINAL_ENV_PREFIX_ALLOWLIST)
    }

    # Proxy variables are required in enterprise deployments, but URLs with
    # embedded userinfo are credentials rather than safe network configuration.
    url_keys = {
        key
        for key in shell_env
        if key in _TERMINAL_URL_VARS or key.upper().endswith(("_URL", "_URI"))
    }
    for key in url_keys:
        value = shell_env.get(key, "").strip()
        if not value:
            continue
        if not _terminal_url_is_safe(key, value):
            shell_env.pop(key, None)
            # Name only: URL values can contain the credential being excluded.
            logger.warning("Browser terminal dropped unsafe URL variable %s", key)
        else:
            shell_env[key] = value

    # Defence in depth: even a future explicit allowlist addition cannot expose
    # a credential-shaped variable without a separately reviewed exception.
    for key in tuple(shell_env):
        if (
            _CREDENTIAL_SHAPED_ENV_PATTERN.search(key)
            and key not in _TERMINAL_CREDENTIAL_EXCEPTIONS
        ):
            shell_env.pop(key, None)

    shell_env["TERM"] = "xterm-256color"
    lc_all = shell_env.get("LC_ALL")
    locale_value = lc_all if lc_all else shell_env.get("LANG", "")
    if not locale_value.replace("-", "").replace("_", "").lower().endswith("utf8"):
        shell_env["LANG"] = "C.UTF-8"
        shell_env["LC_ALL"] = "C.UTF-8"
    if shell_env.get("ENABLE_SP_APIKEYHELPER", "").strip().lower() in (
        "true",
        "1",
        "yes",
    ):
        shell_env["DATABRICKS_CONFIG_PROFILE"] = "omnigents-host"

    # Make the broker shim the direct terminal's first Databricks executable too.
    # The session creator also prepends this path defensively; doing it here
    # keeps every caller of the environment builder consistent.
    if shell_env.get(BROKER_URL_ENV, "").strip():
        home = shell_env.get("HOME", "/app/python/source_code")
        broker_bin = os.path.join(home, ".coda-broker-bin")
        if os.path.isdir(broker_bin):
            shell_env["PATH"] = f"{broker_bin}:{shell_env.get('PATH', '')}"

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

    # Configure gh as git's credential helper so `git push` works without the
    # user wiring credentials by hand. gh must be authenticated (GH_TOKEN, or
    # `gh auth login` in the terminal) for the helper to actually supply
    # anything — this only installs the plumbing.
    try:
        subprocess.run(["gh", "auth", "setup-git"], capture_output=True, timeout=10)
        logger.info("gh auth setup-git configured")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # gh is installed later in run_setup(); absent at first boot is normal.
        logger.debug("gh not available, skipping credential helper setup")

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

    # `wsync` — manual workspace sync for the current repo. The post-commit hook
    # above covers the normal path; this is the recovery path for when a commit's
    # sync failed (see ~/.sync.log) and you don't want an empty commit to retry.
    local_bin = os.path.join(home, ".local", "bin")
    os.makedirs(local_bin, exist_ok=True)
    wsync_path = os.path.join(local_bin, "wsync")
    with open(wsync_path, "w") as f:
        f.write(
            '#!/bin/bash\n'
            '# Manually sync the current git repo to the Databricks Workspace.\n'
            'set -euo pipefail\n'
            'REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"\n'
            'if [ -z "$REPO_ROOT" ]; then\n'
            '    echo "Error: not inside a git repo" >&2\n'
            '    exit 1\n'
            'fi\n'
            'APP_DIR="/app/python/source_code"\n'
            'SYNC_SCRIPT="$APP_DIR/sync_to_workspace.py"\n'
            'if [ ! -f "$SYNC_SCRIPT" ]; then\n'
            '    echo "Error: sync script not found at $SYNC_SCRIPT" >&2\n'
            '    exit 1\n'
            'fi\n'
            'echo "Syncing $REPO_ROOT to Databricks Workspace..."\n'
            'uv run --project "$APP_DIR" python "$SYNC_SCRIPT" "$REPO_ROOT"\n'
        )
    os.chmod(wsync_path, 0o755)
    logger.info(f"wsync command written to {wsync_path}")

    # Reinit app source git to remove template origin (Databricks Apps only)
    _reinit_app_git()


def _setup_bundled_projects():
    """Copy project templates bundled with the app source into ~/projects/.

    Anything under <app_source>/projects/<name>/ is copied to ~/projects/<name>/
    (skipped if already present) and git-init'd, so the post-commit hook picks it
    up and commits sync to the Workspace — which is the only durable backup in
    this ephemeral container.

    No-op when the app ships no `projects/` directory, which is the default.
    """
    import shutil

    app_dir = os.path.dirname(os.path.abspath(__file__))
    bundled_dir = os.path.join(app_dir, "projects")
    if not os.path.isdir(bundled_dir):
        return

    home = os.environ.get("HOME", "/app/python/source_code")
    if not home or home == "/":
        home = "/app/python/source_code"
    projects_dir = os.path.join(home, "projects")
    os.makedirs(projects_dir, exist_ok=True)

    for name in sorted(os.listdir(bundled_dir)):
        src = os.path.join(bundled_dir, name)
        if not os.path.isdir(src):
            continue
        dest = os.path.join(projects_dir, name)
        if os.path.exists(dest):
            logger.info(f"Bundled project already present, skipping: {dest}")
            continue

        shutil.copytree(src, dest)
        # git init so the post-commit workspace-sync hook applies here too.
        subprocess.run(["git", "init"], cwd=dest, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=dest, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit from bundled project"],
            cwd=dest, capture_output=True,
        )
        logger.info(f"Bundled project initialized: {dest}")


def _run_projects_step():
    """Run bundled-project setup as a tracked setup step."""
    _update_step("projects", status="running", started_at=time.time())
    try:
        _setup_bundled_projects()
        _update_step("projects", status="complete", completed_at=time.time())
    except Exception as e:
        logger.warning(f"Bundled project setup failed: {e}")
        _update_step("projects", status="error", completed_at=time.time(), error=str(e))


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

    from cli_auth import _atomic_write_text

    _atomic_write_text(settings_path, json.dumps(settings, indent=2))
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
                logger.warning(
                    "CLI config failed: %s (exit=%s)", script, result.returncode
                )
        except Exception as e:
            logger.warning("CLI config error: %s (%s)", script, type(e).__name__)


def _refresh_cli_auth_after_setup(token):
    """Reconcile setup/rotation races without exposing token-bearing errors."""
    try:
        from cli_auth import update_cli_tokens

        result = update_cli_tokens(token)
    except Exception as error:
        logger.warning("Post-setup token sync failed (%s)", type(error).__name__)
        return False
    if getattr(result, "ok", False) is not True:
        failed = tuple(getattr(result, "failed", ()))
        logger.warning(
            "Post-setup token sync incomplete: failed=%s",
            ",".join(failed) if failed else "unknown",
        )
        return False
    logger.info("Post-setup token sync: CLI configs hold the current token")
    return True


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

    # Probe which terminal editors actually exist in this container. The runtime
    # image's editor set is not guaranteed, and "which editor can I use?" is a
    # recurring first question — for humans and for agents driving the terminal.
    # Report lands at ~/.local/share/coda/editors.txt.
    # Note the `|| true` on the probe: `command -v` returns non-zero for a
    # missing editor, and without it the loop's exit status is the *last*
    # probe's. Since `mcedit` is absent on the standard image, the whole step
    # would exit 1 and be reported as an error on every single boot.
    _run_step("editors", ["bash", "-c",
        "mkdir -p ~/.local/share/coda && "
        "{ echo 'Available terminal editors (detected at app startup):'; "
        "  for ed in micro nano vim vi emacs ed pico joe mcedit; do "
        "    p=$(command -v \"$ed\" 2>/dev/null) && echo \"  $ed -> $p\" || true; "
        "  done; } > ~/.local/share/coda/editors.txt && "
        "cat ~/.local/share/coda/editors.txt"])

    _run_step("gh", ["bash", "install_gh.sh"])


    # tmux — required by Omnigent's native claude/codex harnesses (they launch
    # the agent through a local tmux terminal and refuse to start without it).
    _run_step("tmux", ["bash", "install_tmux.sh"])

    # jq — required by Omnigent's native harness Databricks auth command
    # (`... --output json | jq -r .access_token`). Without it pi/claude/codex
    # resolve an empty token → "Failed to resolve API key". No system jq in the
    # Apps image, so install a static binary (same pattern as tmux).
    _run_step("jq", ["bash", "install_jq.sh"])

    # beads (`bd`) — Gas City work-graph tracker (https://beads.gascity.com) used
    # by the bundled projects: projects/agentic-energy-on-databricks-public tracks
    # a .beads/ config and its bootstrap script hard-fails without `bd` on PATH.
    # Best-effort install (same pattern as jq) so a firewalled deploy without a
    # release mirror doesn't error the whole setup.
    _run_step("beads", ["bash", "install_beads.sh"])

    # --- Upgrade Databricks CLI (runtime image ships an older version) ---
    _run_step("dbcli", ["bash", "install_databricks_cli.sh"])

    # --- Content-filter proxy (must be running before OpenCode starts) ---
    # Sanitizes requests/responses between OpenCode and Databricks
    # (see OpenCode #5028, docs/plans/2026-03-11-litellm-empty-content-blocks-design.md)
    _py = _venv_python()
    _run_step("proxy", [_py, "setup_proxy.py"])

    # --- Parallel agent setup (all independent of each other) ---
    # Each setup script enforces its own ENABLE_* gate. Keeping the steps in
    # the status payload makes skipped agents observable without installing
    # them; disabled scripts exit successfully and are marked complete.
    parallel_steps = [
        ("claude",     [_py, "setup_claude.py"]),
        ("pi",         [_py, "setup_pi.py"]),
        ("codex",      [_py, "setup_codex.py"]),
        ("opencode",   [_py, "setup_opencode.py"]),
        ("gemini",     [_py, "setup_gemini.py"]),
        ("hermes",     [_py, "setup_hermes.py"]),
        ("databricks", [_py, "setup_databricks.py"]),
    ]

    with ThreadPoolExecutor(max_workers=len(parallel_steps) + 1) as executor:
        futures = [
            executor.submit(_run_step, step_id, command)
            for step_id, command in parallel_steps
        ]
        # Bundled projects (copy + git init) — no network, runs alongside the
        # agent installs rather than adding to the critical path.
        futures.append(executor.submit(_run_projects_step))
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
        _refresh_cli_auth_after_setup(current_token)

    with setup_lock:
        any_error = any(s["status"] == "error" for s in setup_state["steps"])
        setup_state["status"] = "error" if any_error else "complete"
        setup_state["completed_at"] = time.time()


# Boot-time owner resolution is bounded so it can't stall gunicorn binding the
# port (initialize_app runs before that). A transient failure is recovered by the
# background retry thread below rather than by a longer boot stall.
_OWNER_BOOT_ATTEMPTS = 3
_OWNER_BOOT_BASE_DELAY = 2.0
# The background retry keeps going for a while — the failure mode it exists for
# (app-SP credentials not yet usable for OAuth exchange) resolves in seconds to
# minutes, and until it does, check_authorization() fails closed and the app is
# unusable.
_OWNER_RETRY_ATTEMPTS = 8
_OWNER_RETRY_MAX_DELAY = 60


def _owner_from_apps_api():
    """Resolve the owner via the Apps API using the app's own SP credentials.

    Returns the lowercased email, or None if the call failed. Prefers the
    spawner's `owner:{email}` description over `app.creator`: when one identity
    creates apps on behalf of others, `creator` is the spawner, not the user who
    should own the box.
    """
    from databricks.sdk import WorkspaceClient

    app_name = os.environ.get("DATABRICKS_APP_NAME")
    if not app_name:
        return None

    w = WorkspaceClient()  # auto-detects SP credentials
    set_product_info(w)
    app_info = w.apps.get(name=app_name)

    description = getattr(app_info, "description", "") or ""
    if description.startswith("owner:"):
        owner = description.split(":", 1)[1].strip().lower()
        if owner:
            logger.info(f"Owner resolved from app description: {owner}")
            return owner

    owner = (app_info.creator or "").lower()
    if owner:
        logger.info(f"Owner resolved from app.creator: {owner}")
        return owner
    return None


def _retry_owner_resolution_in_background():
    """Keep trying to resolve the owner after a failed boot-time attempt.

    check_authorization() fails CLOSED when app_owner is unresolved, so a
    transient Apps-API failure at boot would otherwise brick the app until
    someone restarted it. Retrying in a daemon thread lets the app self-heal
    without delaying the port bind.
    """
    def _retry():
        global app_owner
        for attempt in range(_OWNER_RETRY_ATTEMPTS):
            delay = min(_OWNER_BOOT_BASE_DELAY * (2 ** attempt), _OWNER_RETRY_MAX_DELAY)
            time.sleep(delay)
            if app_owner:
                return  # resolved elsewhere (e.g. a PAT was configured)
            try:
                owner = _owner_from_apps_api()
            except Exception as e:
                logger.warning(
                    f"Background owner resolution attempt {attempt + 1}"
                    f"/{_OWNER_RETRY_ATTEMPTS} failed: {e}"
                )
                continue
            if owner:
                app_owner = owner
                os.environ["APP_OWNER"] = owner
                try:
                    app_state.set_app_owner(owner)
                except Exception as e:
                    logger.warning(f"Could not persist recovered owner: {e}")
                logger.info(f"App owner recovered in background: {owner}")
                return
        logger.error(
            "Owner resolution never succeeded — the app stays fail-closed. "
            "Set APP_OWNER_EMAIL in app.yaml to resolve the owner without an "
            "Apps API call."
        )

    threading.Thread(target=_retry, daemon=True, name="owner-resolve-retry").start()


def get_token_owner():
    """Get the owner email.

    Priority: APP_OWNER_EMAIL env var > Apps API (spawner description, then
    app.creator) > PAT (current_user.me).

    The Apps API path uses the auto-provisioned SP, so it needs no PAT. It is
    retried a few times because the SP's credentials are not always usable for
    OAuth token exchange the instant the container starts — but only briefly,
    since this runs before gunicorn binds the port. APP_OWNER_EMAIL skips the
    call entirely and is the deterministic escape hatch when the API is
    unreliable or the creator isn't the intended owner.
    """
    from databricks.sdk import WorkspaceClient

    # 0. Explicit owner from the deployer — no API call, cannot fail.
    explicit_owner = os.environ.get("APP_OWNER_EMAIL", "").strip().lower()
    if explicit_owner:
        logger.info(f"Owner resolved from APP_OWNER_EMAIL: {explicit_owner}")
        return explicit_owner

    # 1. Try Apps API via SP credentials (no PAT needed), bounded retry.
    if os.environ.get("DATABRICKS_APP_NAME"):
        for attempt in range(_OWNER_BOOT_ATTEMPTS):
            try:
                owner = _owner_from_apps_api()
                if owner:
                    return owner
                logger.warning("Apps API returned no owner for this app")
                break
            except Exception as e:
                last = attempt == _OWNER_BOOT_ATTEMPTS - 1
                if last:
                    logger.warning(
                        f"Could not resolve owner via Apps API after "
                        f"{_OWNER_BOOT_ATTEMPTS} attempts: {e}"
                    )
                else:
                    delay = _OWNER_BOOT_BASE_DELAY * (2 ** attempt)
                    logger.warning(
                        f"Apps API call failed (attempt {attempt + 1}"
                        f"/{_OWNER_BOOT_ATTEMPTS}): {e} — retrying in {delay:.0f}s"
                    )
                    time.sleep(delay)

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


def _capacity_decision() -> CapacityDecision:
    """Evaluate browser admission; count-only behavior is fail-safe."""
    with sessions_lock:
        count = len(sessions)
        pending = _browser_pending
    return _browser_capacity.evaluate(count, MAX_CONCURRENT_SESSIONS, pending)


def _capacity_payload(
    decision: CapacityDecision,
    *,
    session_count: int,
    pending: int = 0,
) -> dict:
    """Secret-free operational projection shared by status and 429 responses.

    ``telemetry_available`` false means the memory gate is inactive and the
    fixed browser cap is the only limit — not that memory is fine.
    """
    memory = decision.memory
    return {
        "current": memory.used_bytes,
        "limit": memory.limit_bytes,
        "percent": memory.percent,
        "telemetry_available": memory.available and memory.limit_bytes is not None,
        "state": decision.state,
        "observed_at": time.time(),
        "high_watermark_percent": _browser_capacity.high_watermark_percent,
        "resume_threshold_percent": _browser_capacity.resume_threshold_percent,
        "reserve_mb": round(_browser_capacity.reserve_bytes / (1024 * 1024)),
        "browser_sessions": {
            "current": session_count,
            "pending": pending,
            "limit": MAX_CONCURRENT_SESSIONS,
            "accepting": decision.allowed,
        },
    }


def _capacity_rejection(decision: CapacityDecision, *, session_count: int, pending: int = 0):
    """Return a stable structured 429 while retaining the legacy error string."""
    if decision.state == "pressured":
        code = "BROWSER_MEMORY_PRESSURE"
        message = (
            "Browser session launch paused because shared container memory is "
            "above the safe high-watermark. Retry after memory falls below the "
            "resume threshold or close an existing session."
        )
        retry_guidance = "Retry after a running browser or Omnigent session exits and pressure clears."
    else:
        code = "BROWSER_SESSION_LIMIT"
        message = (
            f"Maximum {MAX_CONCURRENT_SESSIONS} concurrent browser sessions reached. "
            "Close an existing session before retrying."
        )
        retry_guidance = "Close an existing browser session, then retry."
    capacity = _capacity_payload(decision, session_count=session_count, pending=pending)
    return jsonify({
        "error": message,
        "code": code,
        "message": message,
        "current": capacity["current"],
        "limit": capacity["limit"],
        "retry_guidance": retry_guidance,
        "capacity": capacity,
    }), 429


#: Seconds to keep retrying the non-blocking reap of a killed speculative
#: child. SIGKILL is prompt, but `waitpid(WNOHANG)` returns (0, 0) while the
#: child is still finishing, and giving up there leaves a zombie.
_SPECULATIVE_REAP_TIMEOUT_S = 2.0


def _kill_speculative_session(pid: int, master_fd: int | None) -> None:
    """Close PTY resources and reap a child rejected after fork.

    Never raises: this runs on teardown paths that already have an error to
    report, and it must not turn a 429 into a 500.
    """
    if master_fd is not None:
        try:
            os.close(master_fd)
        except OSError:
            pass
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    # Monotonic, so a wall-clock adjustment cannot stop the deadline advancing.
    deadline = time.monotonic() + _SPECULATIVE_REAP_TIMEOUT_S
    while True:
        try:
            reaped, _status = os.waitpid(pid, os.WNOHANG)
        except (OSError, ChildProcessError):
            return  # already reaped, or not our child
        if reaped:
            return
        if time.monotonic() >= deadline:
            logger.warning("speculative browser child %s not reaped within %.0fs", pid,
                           _SPECULATIVE_REAP_TIMEOUT_S)
            return
        time.sleep(0.05)


def _omnigent_runner_hard_cap() -> int | None:
    """Configured Omnigent runner ceiling; ``None`` means unlimited/unset."""
    try:
        value = int(os.environ.get("OMNIGENT_HOST_MAX_RUNNERS", "0") or 0)
    except ValueError:
        return None
    return value if value > 0 else None


def _resource_capacity_snapshot() -> dict:
    """Authenticated, secret-free capacity projection for operators.

    Reports the two limits this container owns as distinct numbers, and
    states explicitly that the Omnigent managed-lease durable-session cap is
    NOT tracked here, so no reader can mistake one for another.
    """
    with sessions_lock:
        session_count = len(sessions)
        pending = _browser_pending
    decision = _browser_capacity.evaluate(session_count, MAX_CONCURRENT_SESSIONS, pending)
    capacity = _capacity_payload(decision, session_count=session_count, pending=pending)
    capacity["process_tree_rss_mb"] = _process_tree_rss_mb()
    capacity["omnigent"] = {
        # CoDA sets this env var for the host subprocess, so it can report the
        # configured ceiling — but the live active/pending counts belong to the
        # Omnigent host daemon and are published over its own tunnel.
        "active_runner_hard_cap": _omnigent_runner_hard_cap(),
        "active_runner_hard_cap_env": "OMNIGENT_HOST_MAX_RUNNERS",
        "managed_lease_durable_sessions": {
            "tracked_here": False,
            "owner": "omnigent server sandbox config (max_sessions_per_lease)",
        },
    }
    return capacity


def _log_resource_snapshot() -> None:
    """Log one resource sample; separate for deterministic unit tests."""
    with sessions_lock:
        n_sessions = len(sessions)
        pending = _browser_pending
    n_threads = threading.active_count()
    tree_rss = _process_tree_rss_mb()
    decision = _browser_capacity.evaluate(n_sessions, MAX_CONCURRENT_SESSIONS, pending)
    memory = decision.memory
    try:
        import resource as _res
        peak_self_mb = round(_res.getrusage(_res.RUSAGE_SELF).ru_maxrss / 1024)
    except Exception:
        peak_self_mb = None
    try:
        open_fds = len(os.listdir(f"/proc/{os.getpid()}/fd"))
    except Exception:
        open_fds = None
    per_session = round(tree_rss / n_sessions) if tree_rss and n_sessions else None
    logger.info(
        "RESOURCE: browser_sessions=%s/%s cgroup_used=%sB cgroup_limit=%sB "
        "cgroup_percent=%s pressure=%s threads=%s tree_rss=%sMB "
        "per_session=%sMB peak_self=%sMB open_fds=%s",
        n_sessions, MAX_CONCURRENT_SESSIONS, memory.used_bytes, memory.limit_bytes,
        memory.percent, decision.state, n_threads, tree_rss, per_session,
        peak_self_mb, open_fds,
    )


def resource_pressure_monitor():
    """Periodically log cgroup, process-tree, and browser-session pressure."""
    while True:
        time.sleep(RESOURCE_MONITOR_INTERVAL_SECONDS)
        try:
            _log_resource_snapshot()
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
    ) or request.path.startswith(("/socket.io", "/api/omnigent-host/")):
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


@app.route("/api/capacity")
@app.route("/api/resource-status")
def capacity_status():
    """Authenticated, secret-free capacity projection for operations.

    Also drives the browser UI's ``N/limit`` session badge, so it stays
    cheap: no subprocess calls beyond the existing process-tree sample.
    """
    return jsonify(_resource_capacity_snapshot())


@app.route("/api/omnigents-status")
def omnigents_status():
    """Report browser-safe host state without runner or host log content."""
    from omnigents_host import get_status

    status = get_status()
    browser_fields = (
        "configured",
        "running",
        "installed",
        "host_launched",
        "server_url",
        "stage",
    )
    return jsonify({key: status.get(key) for key in browser_fields})


@app.route("/api/omnigent-host/status")
def omnigent_host_status():
    """Report runtime Omnigent host state to the configured server SP."""
    if not _omnigent_server_request_authorized():
        return jsonify({"error": "Forbidden"}), 403
    from omnigents_host import get_status
    return jsonify(get_status())


def _omnigent_server_request_authorized() -> bool:
    """Authorize the configured Omnigent server service principal.

    Databricks Apps validates the forwarded bearer before it reaches Flask;
    this check narrows the M2M endpoint to the configured server SP.
    """
    expected = os.environ.get("OMNIGENT_SERVER_SP_CLIENT_ID", "").strip()
    if not expected:
        return False
    # Only trust the Apps-proxy-injected token. Accepting a caller-supplied
    # Authorization header here would make unverified JWT payload decoding an
    # authorization bypass if the Flask port were ever exposed directly.
    token = request.headers.get("X-Forwarded-Access-Token", "").strip()
    try:
        import base64
        import json

        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        return False
    principals = {
        str(claims.get(key, "")).strip()
        for key in ("sub", "client_id", "azp", "appid")
    }
    return any(hmac.compare_digest(principal, expected) for principal in principals)


@app.route("/api/omnigent-host/lease", methods=["POST"])
def omnigent_host_lease():
    """Acquire or adopt the single user-scoped managed lease."""
    if not _omnigent_server_request_authorized():
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json(silent=True) or {}
    owner = str(data.get("owner") or "").strip()
    lease_id = str(data.get("lease_id") or "").strip()
    requested_app = str(data.get("app_name") or "").strip()
    app_name = os.environ.get("DATABRICKS_APP_NAME", "").strip()
    if not owner or not lease_id:
        return jsonify({"error": "owner and lease_id required"}), 400
    if not app_name or requested_app != app_name:
        return jsonify({"error": "app_name does not match this CoDA instance"}), 409
    from omnigents_host import acquire_lease

    ok, lease = acquire_lease(owner, lease_id)
    # The caller needs only the generation fence. Owner identity and lease
    # timestamps remain process-internal and never cross the control API.
    return jsonify({"lease_id": lease.get("lease_id"), "acquired": ok}), (200 if ok else 409)


def _repository_workspace_args(data):
    """Return optional protocol-v2 repository metadata from a control request."""
    fields = ("repo_url", "repo_branch", "repo_name")
    requested = any(data.get(field) is not None for field in fields)
    if requested and data.get("workspace_protocol_version") != 2:
        raise ValueError("repository workspace protocol version 2 is required")
    return requested, {field: data.get(field) for field in fields}


@app.route("/api/omnigent-host/workspaces", methods=["POST"])
def omnigent_host_workspace():
    """Allocate, materialize, or release a distinct session workspace."""
    if not _omnigent_server_request_authorized():
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json(silent=True) or {}
    from omnigents_host import (
        WorkspaceAllocationError,
        allocate_workspace,
        release_workspace,
    )

    lease_id = str(data.get("lease_id") or "")
    session_id = str(data.get("session_id") or "")
    try:
        if data.get("action") == "release":
            released = release_workspace(lease_id, session_id)
            return jsonify(
                {
                    "released": released,
                    "workspace_protocol_version": 2,
                }
            )
        repository_requested, repository = _repository_workspace_args(data)
        workspace = allocate_workspace(
            lease_id,
            session_id,
            **repository,
        )
    except WorkspaceAllocationError as exc:
        return jsonify({"error": str(exc)}), exc.status_code
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(
        {
            "workspace": workspace,
            "workspace_protocol_version": 2,
            "repository_materialized": repository_requested,
        }
    )


@app.route("/api/omnigent-host/runner-log/<session_id>")
def omnigent_host_runner_log(session_id):
    """Return a bounded runner log tail to the configured server SP."""
    if not _omnigent_server_request_authorized():
        return jsonify({"error": "Forbidden"}), 403
    from omnigents_host import runner_log_tail

    try:
        return jsonify({"lines": runner_log_tail(session_id)})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/omnigent-host/connect", methods=["POST"])
def omnigent_host_connect():
    """Start a runtime Omnigent host tunnel for a supplied server URL."""
    if not _omnigent_server_request_authorized():
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json(silent=True) or {}
    server_url = (data.get("server_url") or "").strip()
    if not server_url:
        return jsonify({"error": "server_url required"}), 400
    configured_server_url = os.environ.get("OMNIGENTS_SERVER_URL", "").strip()
    if configured_server_url and server_url.rstrip("/") != configured_server_url.rstrip("/"):
        return jsonify({"error": "server_url does not match configured Omnigent server"}), 409

    from omnigents_host import (
        WorkspaceAllocationError,
        active_lease,
        allocate_workspace,
        connect_host,
        release_workspace,
    )

    lease_id = str(data.get("lease_id") or "")
    lease = active_lease()
    if lease is None or lease.get("lease_id") != lease_id:
        return jsonify({"error": "stale or missing lease"}), 409
    host_config = data.get("host_config")
    if host_config is not None and not isinstance(host_config, dict):
        return jsonify({"error": "host_config must be an object"}), 400
    allocated_session_id = None
    try:
        repository_requested, repository = _repository_workspace_args(data)
        session_id = str(data.get("session_id") or "")
        if repository_requested and not session_id:
            return jsonify({"error": "repository workspace protocol upgrade required"}), 426
        if session_id:
            # Isolate the lease-OPENING session too, not just the ones that
            # adopt the lease later. Returning $HOME here started the first
            # session of every claim in this app's own home directory: all
            # first-sessions shared one tree, and an agent's writes landed
            # beside app.py. Sessions 2..N already get ~/coda-sessions/<id>
            # from /api/omnigent-host/workspaces, so this only makes the
            # opener consistent with them.
            workspace = allocate_workspace(
                lease_id,
                session_id,
                **repository,
            )
            allocated_session_id = session_id
        else:
            # A server too old to send session_id has nothing to isolate on,
            # so it keeps the legacy whole-home workspace.
            workspace = os.environ.get("HOME", "/app/python/source_code")
        ok, status = connect_host(
            server_url,
            _omnigent_sp_creds,
            host_token=(data.get("host_token") or None),
            host_id=(data.get("host_id") or None),
            host_name=(data.get("host_name") or None),
            host_config=host_config,
            lease_id=(data.get("lease_id") or None),
        )
    except WorkspaceAllocationError as exc:
        return jsonify({"error": str(exc)}), exc.status_code
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        if allocated_session_id is not None:
            try:
                release_workspace(lease_id, allocated_session_id)
            except WorkspaceAllocationError:
                pass
        raise
    if not ok:
        if allocated_session_id is not None:
            try:
                release_workspace(lease_id, allocated_session_id)
            except WorkspaceAllocationError:
                pass
        code = 409 if status.get("last_error") == "host already running" else 400
        return jsonify({"error": "host connection failed"}), code
    status["workspace"] = workspace
    status["workspace_protocol_version"] = 2
    status["repository_materialized"] = repository_requested
    return jsonify(status), 202


@app.route("/api/omnigent-host/disconnect", methods=["POST"])
def omnigent_host_disconnect():
    """Release and scrub only the matching managed lease generation."""
    if not _omnigent_server_request_authorized():
        return jsonify({"error": "Forbidden"}), 403
    data = request.get_json(silent=True) or {}
    lease_id = str(data.get("lease_id") or "")
    from omnigents_host import release_managed_lease

    released, status = release_managed_lease(lease_id)
    if status.get("stale"):
        return jsonify(status)
    if not released:
        return jsonify(status), 503
    return jsonify(status)


@app.route("/api/omnigent-host/share", methods=["POST"])
def omnigent_host_share():
    """Share this SP-owned host with a user so it shows in their picker.

    The host is owned by the app SP, so a user's personal Omnigent UI can't
    see it until the owner (SP) grants them ``use``. This issues that grant —
    and optionally launches a runner — using the captured SP creds.
    Owner-gated identically to configure-pat: only the resolved app owner may
    invoke it, since it acts with the SP's authority.

    Body (all optional):
      grant_user: email to share to. Defaults to the calling user (the owner),
                  so a bare POST self-shares (the browser auto-share path).
                  Pass an explicit email to share to a different user, e.g.
                  a teammate who needs to run sessions on this host.
      launch:     also launch a runner after granting (default true).
    """
    if _is_databricks_apps() and (
        not app_owner or get_request_user() != app_owner
    ):
        return jsonify({"error": "Forbidden"}), 403

    from omnigents_host import get_status
    server_url = os.environ.get("OMNIGENTS_SERVER_URL", "").strip() or str(
        get_status().get("server_url") or ""
    ).strip()
    if not server_url:
        return jsonify({"error": "no server_url; connect the host first"}), 400

    data = request.get_json(silent=True) or {}
    launch = bool(data.get("launch", True))
    # Default: share to the calling user (owner). An explicit grant_user lets the
    # owner share to a teammate — the SP owns the host, so only the owner (via
    # this endpoint) can issue that grant.
    grant_user = (data.get("grant_user") or get_request_user() or app_owner or "").strip()
    if not grant_user:
        return jsonify({"error": "could not resolve a user to grant"}), 400

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
    global _browser_pending
    # Reserve a slot before forking a PTY. Counting in-flight launches here is
    # what stops a burst of concurrent requests from all passing the same
    # reading and forking past the ceiling; the authoritative re-check under
    # the insertion lock below still closes the residual race.
    with sessions_lock:
        session_count = len(sessions)
        decision = _browser_capacity.evaluate(
            session_count, MAX_CONCURRENT_SESSIONS, _browser_pending
        )
        if not decision.allowed:
            return _capacity_rejection(
                decision, session_count=session_count, pending=_browser_pending
            )
        _browser_pending += 1

    master_fd = slave_fd = None
    pid = None
    inserted = False
    reserved = True
    try:
        # Inside the try so a malformed body (e.g. a JSON array) cannot strand
        # the reservation taken above.
        data = request.get_json(silent=True)
        label = data.get("label", "") if isinstance(data, dict) else ""
        master_fd, slave_fd = pty.openpty()
        # Build the browser PTY's deny-by-default inherited environment. The
        # explicit allowlist retains required shell/broker/feature plumbing;
        # ambient credentials and unknown variables never enter Popen(env=...).
        shell_env = _build_terminal_shell_env(os.environ)
        # Ensure HOME is set correctly
        if not shell_env.get("HOME") or shell_env["HOME"] == "/":
            shell_env["HOME"] = "/app/python/source_code"
        # Add the broker shim before ~/.local/bin when SP helper auth is active,
        # then the normal CLI/tools. Without this, direct `databricks ...` calls
        # use the real binary, which has no OAuth cache in the container.
        local_bin = f"{shell_env['HOME']}/.local/bin"
        broker_bin = f"{shell_env['HOME']}/.coda-broker-bin"
        path_parts = []
        if shell_env.get(BROKER_URL_ENV, "").strip() and os.path.isdir(broker_bin):
            path_parts.append(broker_bin)
        path_parts.extend([local_bin, shell_env.get("PATH", "")])
        shell_env["PATH"] = ":".join(part for part in path_parts if part)

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
        slave_fd = None

        session_id = str(uuid.uuid4())

        with sessions_lock:
            # Authoritative count + memory check under the insertion lock
            # prevents TOCTOU races. A speculative child is always killed and
            # its PTY closed when this check rejects it. This request's own
            # reservation is excluded from `pending` so it does not refuse
            # itself.
            session_count = len(sessions)
            decision = _browser_capacity.evaluate(
                session_count, MAX_CONCURRENT_SESSIONS, _browser_pending - 1
            )
            # The reservation is retired here, inside the insertion lock, so a
            # session is never counted twice (once as `pending`, once in
            # `sessions`) by a concurrent request's authoritative check.
            _browser_pending -= 1
            reserved = False
            if not decision.allowed:
                return _capacity_rejection(
                    decision, session_count=session_count, pending=_browser_pending
                )
            sessions[session_id] = {
                "master_fd": master_fd,
                "pid": pid,
                "output_buffer": deque(maxlen=1000),
                "lock": threading.Lock(),
                "last_poll_time": time.time(),
                "created_at": time.time(),
                "label": label,
            }

        # Start the reader before transferring cleanup ownership to the session.
        # If thread startup fails, remove the invisible session and let the
        # finally block close the PTY and kill/reap the child.
        thread = threading.Thread(target=read_pty_output, args=(session_id, master_fd), daemon=True)
        try:
            thread.start()
        except Exception:
            with sessions_lock:
                sessions.pop(session_id, None)
            raise
        inserted = True

        # Telemetry must never turn a usable session into an unreturned leak.
        try:
            log_telemetry("agent", label or "shell")
        except Exception as exc:
            logger.warning("session creation telemetry failed: %s", exc)

        return jsonify({"session_id": session_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        # Any path that did not insert the session owns the teardown: close
        # both PTY descriptors and kill/reap the speculative child. Without
        # this, a capacity rejection or an exception after openpty()/Popen()
        # leaks an fd and an orphan shell for the life of the worker.
        if not inserted:
            if slave_fd is not None:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass
            if pid is not None:
                _kill_speculative_session(pid, master_fd)
            elif master_fd is not None:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
        if reserved:
            with sessions_lock:
                _browser_pending -= 1


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
    global _sp_token_broker_atexit_registered

    if not _sp_token_broker_atexit_registered:
        atexit.register(_shutdown_sp_token_broker)
        _sp_token_broker_atexit_registered = True

    # Install SIGTERM handler only for gunicorn (production).
    # For local dev, SIG_DFL is fine — the process just exits cleanly.
    if not local_dev:
        signal.signal(signal.SIGTERM, handle_sigterm)

    # SP credentials preserved — needed for Apps API (owner resolution) and secret persistence

    # Capture the app SP's M2M OAuth creds BEFORE the strip below — the
    # Omnigents host tunnel needs an OAuth token (the Apps proxy rejects PATs).
    # No-op / returns None when disabled or creds absent. See omnigents_host.py.
    from omnigents_host import capture_sp_credentials, start_host, start_lease_reaper

    _omnigent_sp_creds = capture_sp_credentials()
    start_lease_reaper()

    # Resolve owner: Apps API (app.creator via SP) > PAT (current_user.me)
    app_owner = get_token_owner()
    if app_owner:
        logger.info(f"App owner: {app_owner}")
        os.environ["APP_OWNER"] = app_owner
        app_state.set_app_owner(app_owner)
    else:
        # check_authorization() fails CLOSED on Databricks Apps when app_owner is
        # unresolved, so this is not "authorization disabled" — it's "nobody can
        # use the app". Keep trying in the background so a transient Apps-API
        # failure at boot doesn't require a manual restart.
        logger.error(
            "Could not determine app owner — access stays denied (fail-closed) "
            "until resolution succeeds"
        )
        _retry_owner_resolution_in_background()

    sp_helper_enabled = os.environ.get(
        "ENABLE_SP_APIKEYHELPER", ""
    ).strip().lower() in (
        "true",
        "1",
        "yes",
    )
    host_enabled = bool(os.environ.get("OMNIGENTS_SERVER_URL", "").strip())
    if _omnigent_sp_creds and (sp_helper_enabled or host_enabled):
        _sp_token_broker_server = start_sp_token_broker(
            lambda: mint_sp_token(_omnigent_sp_creds)
        )
        os.environ[BROKER_URL_ENV] = broker_url(_sp_token_broker_server)
        # Install immediately when the CLI is already cached; _run_step("dbcli")
        # retries this after a cold-boot install completes.
        _ensure_broker_cli_wrapper()
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
            target=_run_step,
            args=("challenge", ["bash", "install_challenge_repo.sh"]),
            daemon=True,
            name="challenge-preload",
        ).start()

    # SP-auth workshop path: when the app self-auths as its own SP (profile
    # written above), no PAT paste will ever come — so trigger setup at boot
    # instead of waiting for /api/configure-pat. Installs the agent CLIs and
    # configures them against the SP OAuth token via the apiKeyHelper. Guarded
    # on the same flag + captured creds; background thread, never blocks boot.
    if _omnigent_sp_creds and os.environ.get(
        "ENABLE_SP_APIKEYHELPER", ""
    ).strip().lower() in ("true", "1", "yes"):
        with setup_lock:
            already = setup_state["status"] in ("running", "complete")
        if not already:
            threading.Thread(
                target=run_setup, daemon=True, name="setup-thread-sp"
            ).start()
            logger.info(
                "SP apikeyhelper: setup triggered at boot (no PAT paste needed)"
            )

    # Telemetry: app startup ping (fire-and-forget in background thread)
    log_telemetry("event", "app_startup")

    # Start background cleanup thread
    cleanup_thread = threading.Thread(target=cleanup_stale_sessions, daemon=True)
    cleanup_thread.start()
    logger.info(
        f"Started session cleanup thread (timeout={SESSION_TIMEOUT_SECONDS}s, interval={CLEANUP_INTERVAL_SECONDS}s)"
    )

    # Start resource-pressure monitor. On a 4 vCPU / 12 GB box a couple of heavy
    # agent sessions can approach the memory cliff; this logs a runway of
    # telemetry so a crash isn't a single silent moment, and lets us measure
    # real per-session RSS to tune MAX_CONCURRENT_SESSIONS with data.
    monitor_thread = threading.Thread(
        target=resource_pressure_monitor, daemon=True, name="resource-monitor"
    )
    monitor_thread.start()



if __name__ == "__main__":
    # Local dev — no SIGTERM handler (SIG_DFL), no shutting_down flag
    initialize_app(local_dev=True)
    shutting_down = False  # safety net: ensure clean state before serving
    port = int(os.environ.get("DATABRICKS_APP_PORT", 8000))
    socketio.run(app, host="0.0.0.0", port=port, allow_unsafe_werkzeug=True)
