#!/usr/bin/env python
"""Start the content-filter proxy between local coding agents and Databricks.

OpenCode (OpenAI-compatible traffic) and Gemini CLI (Gemini-native traffic
under /gemini/*) both route through it. Fixes known agent/endpoint
incompatibilities by sanitizing requests and responses:
  - Empty text content blocks (OpenCode #5028)
  - Orphaned tool_result blocks with no matching tool_use
  - Databricks 'databricks-tool-call' name mangling
  - Incorrect finish_reason on tool call responses
  - OpenAI reasoning-era params Databricks rejects (reasoning_effort et al.)
  - Gemini functionCall/functionResponse `id` fields the /gemini route rejects

See docs/plans/2026-03-11-litellm-empty-content-blocks-design.md
"""
import os
import signal
import sys
import time
import subprocess
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

from utils import ensure_https, get_gateway_host

PROXY_PORT = 4000
PROXY_HOST = "127.0.0.1"
HEALTH_TIMEOUT = 15
HEALTH_POLL_INTERVAL = 0.5


def resolve_upstream_bases(gateway_host, host):
    """Upstream base pair (openai_compatible, gemini_native) for the proxy.

    Mirrors the direct base URLs the agent setups pointed at before routing
    through the proxy: setup_opencode used mlflow/v1 | serving-endpoints and
    setup_gemini used gemini | serving-endpoints/google.
    """
    if gateway_host:
        return f"{gateway_host}/mlflow/v1", f"{gateway_host}/gemini"
    return f"{host}/serving-endpoints", f"{host}/serving-endpoints/google"


def resolve_proxy_script_path():
    """Absolute path to the content_filter_proxy.py server this launcher runs.

    content_filter_proxy.py lives at the REPO ROOT, not in this setup/ directory.
    This file (setup_proxy.py) was moved into setup/ in git fec2152 without
    updating the lookup; resolving from setup/ pointed Popen at a nonexistent
    file, so the proxy never started and OpenCode (then the only agent routing
    through 127.0.0.1:4000) failed with "Cannot connect to API". Resolve from
    the parent of setup/ so the path tracks the proxy's real location.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, "content_filter_proxy.py")


def main():
    # Set HOME if not properly set
    if not os.environ.get("HOME") or os.environ["HOME"] == "/":
        os.environ["HOME"] = "/app/python/source_code"

    home = Path(os.environ["HOME"])

    # Kill any existing proxy on our port (more reliable than PID file)
    try:
        result = subprocess.run(
            ["fuser", "-k", f"{PROXY_PORT}/tcp"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            print(f"Killed previous process on port {PROXY_PORT}")
            time.sleep(1)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        # fuser not available, try lsof
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{PROXY_PORT}"],
                capture_output=True, text=True, timeout=5
            )
            for pid in result.stdout.strip().split():
                try:
                    os.kill(int(pid), signal.SIGKILL)
                    print(f"Killed previous proxy (PID: {pid})")
                except (ValueError, ProcessLookupError):
                    pass
            if result.stdout.strip():
                time.sleep(1)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    # Clean up stale PID file
    pid_path = home / ".content-filter-proxy.pid"
    pid_path.unlink(missing_ok=True)

    # Databricks configuration
    gateway_host = get_gateway_host()
    host = ensure_https(os.environ.get("DATABRICKS_HOST", "").rstrip("/"))
    token = os.environ.get("DATABRICKS_TOKEN", "")

    if not token:
        print("Warning: DATABRICKS_TOKEN not set, skipping proxy setup")
        sys.exit(0)

    # Determine the upstream base URLs (OpenAI-compatible + Gemini-native)
    upstream_base, gemini_upstream_base = resolve_upstream_bases(gateway_host, host)
    if gateway_host:
        print(f"Content-filter proxy will forward to AI Gateway: {gateway_host}")
    else:
        print(f"Content-filter proxy will forward to: {host}/serving-endpoints")
    print(f"Gemini-native requests (/gemini/*) will forward to: {gemini_upstream_base}")

    # Start proxy as a background process
    proxy_script = resolve_proxy_script_path()
    log_path = home / ".content-filter-proxy.log"
    print(f"Starting content-filter proxy on {PROXY_HOST}:{PROXY_PORT}...")

    env = os.environ.copy()
    env["PROXY_UPSTREAM_BASE"] = upstream_base
    env["PROXY_GEMINI_UPSTREAM_BASE"] = gemini_upstream_base
    env["PROXY_HOST"] = PROXY_HOST
    env["PROXY_PORT"] = str(PROXY_PORT)

    proc = subprocess.Popen(
        [sys.executable, proxy_script],
        stdout=open(log_path, "w"),
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )

    # Write PID file for cleanup
    pid_path = home / ".content-filter-proxy.pid"
    pid_path.write_text(str(proc.pid))
    print(f"Proxy started (PID: {proc.pid})")

    # Wait for health check
    health_url = f"http://{PROXY_HOST}:{PROXY_PORT}/health"
    start = time.time()
    ready = False

    while time.time() - start < HEALTH_TIMEOUT:
        try:
            resp = urlopen(Request(health_url), timeout=2)
            if resp.status == 200:
                ready = True
                break
        except (URLError, OSError):
            pass

        if proc.poll() is not None:
            print(f"Error: Proxy exited with code {proc.returncode}")
            try:
                print(f"Logs: {log_path.read_text()[:1000]}")
            except Exception:
                pass
            sys.exit(1)

        time.sleep(HEALTH_POLL_INTERVAL)

    if ready:
        elapsed = time.time() - start
        print(f"Content-filter proxy ready on {PROXY_HOST}:{PROXY_PORT} ({elapsed:.1f}s)")
    else:
        print(f"Warning: Proxy health check timed out after {HEALTH_TIMEOUT}s")
        try:
            print(f"Logs: {log_path.read_text()[:1000]}")
        except Exception:
            pass


if __name__ == "__main__":
    main()
