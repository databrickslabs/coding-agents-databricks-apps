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
import logging
from flask import Flask, send_from_directory, request, jsonify, session
from collections import deque

# Session timeout configuration
SESSION_TIMEOUT_SECONDS = 60        # No poll for 60s = dead session
CLEANUP_INTERVAL_SECONDS = 30       # How often to check for stale sessions
GRACEFUL_SHUTDOWN_WAIT = 3          # Seconds to wait after SIGHUP before SIGKILL

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='static', static_url_path='/static')
app.secret_key = os.urandom(24)

# Store sessions: {session_id: {"master_fd": fd, "pid": pid, "output_buffer": deque}}
sessions = {}
sessions_lock = threading.Lock()

# Single-user security: only the token owner can access the terminal
app_owner = None


def get_token_owner():
    """Get the owner email from DATABRICKS_TOKEN at startup."""
    try:
        from databricks.sdk import WorkspaceClient
        host = os.environ.get("DATABRICKS_HOST")
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


def check_authorization():
    """Check if the current user is authorized to access the app."""
    # If owner not set (local dev or SDK unavailable), allow access
    if not app_owner:
        return True, None

    current_user = get_request_user()

    # If no user identity in request (local dev), allow access
    if not current_user:
        return True, None

    # Check if current user is the owner
    if current_user != app_owner:
        logger.warning(f"Unauthorized access attempt by {current_user} (owner: {app_owner})")
        return False, current_user

    return True, None


def read_pty_output(session_id, fd):
    """Background thread to read PTY output into buffer."""
    while True:
        with sessions_lock:
            if session_id not in sessions:
                break
        try:
            if select.select([fd], [], [], 0.1)[0]:
                output = os.read(fd, 4096).decode(errors="replace")
                with sessions_lock:
                    if session_id in sessions:
                        sessions[session_id]["output_buffer"].append(output)
        except OSError:
            break


def terminate_session(session_id, pid, master_fd):
    """Gracefully terminate a session: SIGHUP -> wait -> SIGKILL -> cleanup."""
    logger.info(f"Terminating stale session {session_id} (pid={pid})")
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


def cleanup_stale_sessions():
    """Background thread that removes sessions with no recent polling."""
    while True:
        time.sleep(CLEANUP_INTERVAL_SECONDS)

        now = time.time()
        stale_sessions = []

        # Find stale sessions
        with sessions_lock:
            for session_id, session in sessions.items():
                if now - session["last_poll_time"] > SESSION_TIMEOUT_SECONDS:
                    stale_sessions.append((session_id, session["pid"], session["master_fd"]))

        if stale_sessions:
            logger.info(f"Found {len(stale_sessions)} stale session(s) to clean up")

        # Terminate each stale session (outside the lock)
        for session_id, pid, master_fd in stale_sessions:
            terminate_session(session_id, pid, master_fd)


@app.before_request
def authorize_request():
    """Check authorization before processing any request."""
    # Skip auth for health check
    if request.path == "/health":
        return None

    authorized, user = check_authorization()
    if not authorized:
        return jsonify({
            "error": "Unauthorized",
            "message": f"This app belongs to {app_owner}. You are logged in as {user}."
        }), 403

    return None


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/health")
def health():
    with sessions_lock:
        return jsonify({
            "status": "healthy",
            "active_sessions": len(sessions),
            "session_timeout_seconds": SESSION_TIMEOUT_SECONDS
        })


@app.route("/api/session", methods=["POST"])
def create_session():
    """Create a new terminal session."""
    try:
        master_fd, slave_fd = pty.openpty()
        # Set up environment for the shell
        shell_env = os.environ.copy()
        shell_env["TERM"] = "xterm-256color"
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

        session_id = str(uuid.uuid4())

        with sessions_lock:
            sessions[session_id] = {
                "master_fd": master_fd,
                "pid": pid,
                "output_buffer": deque(maxlen=1000),
                "last_poll_time": time.time(),
                "created_at": time.time()
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

    with sessions_lock:
        if session_id not in sessions:
            return jsonify({"error": "Session not found"}), 404

        fd = sessions[session_id]["master_fd"]

    try:
        os.write(fd, input_data.encode())
        return jsonify({"status": "ok"})
    except OSError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/output", methods=["POST"])
def get_output():
    """Get output from the terminal."""
    data = request.json
    session_id = data.get("session_id")

    with sessions_lock:
        if session_id not in sessions:
            return jsonify({"error": "Session not found"}), 404

        sessions[session_id]["last_poll_time"] = time.time()
        buffer = sessions[session_id]["output_buffer"]
        output = "".join(buffer)
        buffer.clear()

    return jsonify({"output": output})


@app.route("/api/resize", methods=["POST"])
def resize_terminal():
    """Resize the terminal."""
    data = request.json
    session_id = data.get("session_id")
    cols = data.get("cols", 80)
    rows = data.get("rows", 24)

    with sessions_lock:
        if session_id not in sessions:
            return jsonify({"error": "Session not found"}), 404
        fd = sessions[session_id]["master_fd"]

    try:
        # Set terminal size using TIOCSWINSZ
        winsize = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        return jsonify({"status": "ok"})
    except OSError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/session", methods=["DELETE"])
def delete_session():
    """Close a terminal session."""
    data = request.json
    session_id = data.get("session_id")

    with sessions_lock:
        if session_id in sessions:
            try:
                os.close(sessions[session_id]["master_fd"])
            except:
                pass
            del sessions[session_id]

    return jsonify({"status": "ok"})


if __name__ == "__main__":
    
    # Remove OAuth credentials - force PAT auth only
    os.environ.pop("DATABRICKS_CLIENT_ID", None)
    os.environ.pop("DATABRICKS_CLIENT_SECRET", None)

    # Determine app owner from DATABRICKS_TOKEN
    app_owner = get_token_owner()
    if app_owner:
        logger.info(f"App owner (from token): {app_owner}")
    else:
        logger.warning("Could not determine app owner - authorization disabled")

    # Start background cleanup thread
    cleanup_thread = threading.Thread(target=cleanup_stale_sessions, daemon=True)
    cleanup_thread.start()
    logger.info(f"Started session cleanup thread (timeout={SESSION_TIMEOUT_SECONDS}s, interval={CLEANUP_INTERVAL_SECONDS}s)")

    app.run(host="0.0.0.0", port=8000, threaded=True)
