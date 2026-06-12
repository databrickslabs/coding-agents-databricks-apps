#!/usr/bin/env python
"""Start localhost proxies that survive PAT rotation.

Two instances run side by side:

  - port 4000: OpenCode-facing proxy. Sanitizes chat-completions requests
    (empty text blocks, orphaned tool_results, tool name mangling, finish_reason).
  - port 4001: Codex-facing proxy in passthrough mode. Only re-injects the
    fresh PAT from ~/.databrickscfg so long-lived Codex sessions don't go
    stale when the rotator rolls the token.

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

OPENCODE_PROXY_PORT = 4000
CODEX_PROXY_PORT = 4001
PROXY_HOST = "127.0.0.1"
HEALTH_TIMEOUT = 15
HEALTH_POLL_INTERVAL = 0.5

# Set HOME if not properly set
if not os.environ.get("HOME") or os.environ["HOME"] == "/":
    os.environ["HOME"] = "/app/python/source_code"

home = Path(os.environ["HOME"])
proxy_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "content_filter_proxy.py")


def _kill_port(port: int) -> None:
    """Kill any process holding `port` so we can rebind."""
    try:
        result = subprocess.run(
            ["fuser", "-k", f"{port}/tcp"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            print(f"Killed previous process on port {port}")
            time.sleep(1)
            return
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=5
        )
        for pid in result.stdout.strip().split():
            try:
                os.kill(int(pid), signal.SIGKILL)
                print(f"Killed previous proxy on port {port} (PID: {pid})")
            except (ValueError, ProcessLookupError):
                pass
        if result.stdout.strip():
            time.sleep(1)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def _start_proxy(port: int, upstream_base: str, passthrough: bool, label: str) -> bool:
    """Spawn a content_filter_proxy instance and wait for it to come up."""
    _kill_port(port)

    log_path = home / f".content-filter-proxy-{label}.log"
    pid_path = home / f".content-filter-proxy-{label}.pid"
    pid_path.unlink(missing_ok=True)

    print(f"Starting {label} proxy on {PROXY_HOST}:{port} -> {upstream_base}"
          f"{' [passthrough]' if passthrough else ''}")

    env = os.environ.copy()
    env["PROXY_UPSTREAM_BASE"] = upstream_base
    env["PROXY_HOST"] = PROXY_HOST
    env["PROXY_PORT"] = str(port)
    env["PROXY_PASSTHROUGH"] = "true" if passthrough else "false"

    proc = subprocess.Popen(
        [sys.executable, proxy_script],
        stdout=open(log_path, "w"),
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    pid_path.write_text(str(proc.pid))
    print(f"  {label} proxy started (PID: {proc.pid})")

    health_url = f"http://{PROXY_HOST}:{port}/health"
    start = time.time()
    while time.time() - start < HEALTH_TIMEOUT:
        try:
            resp = urlopen(Request(health_url), timeout=2)
            if resp.status == 200:
                elapsed = time.time() - start
                print(f"  {label} proxy ready ({elapsed:.1f}s)")
                return True
        except (URLError, OSError):
            pass

        if proc.poll() is not None:
            print(f"  ERROR: {label} proxy exited with code {proc.returncode}")
            try:
                print(f"  Logs: {log_path.read_text()[:1000]}")
            except Exception:
                pass
            return False

        time.sleep(HEALTH_POLL_INTERVAL)

    print(f"  WARN: {label} proxy health check timed out after {HEALTH_TIMEOUT}s")
    try:
        print(f"  Logs: {log_path.read_text()[:1000]}")
    except Exception:
        pass
    return False


# Legacy PID file from the single-proxy era — remove so it doesn't confuse
# anyone looking for the OpenCode proxy.
(home / ".content-filter-proxy.pid").unlink(missing_ok=True)

# Databricks configuration
gateway_host = get_gateway_host()
host = ensure_https(os.environ.get("DATABRICKS_HOST", "").rstrip("/"))
token = os.environ.get("DATABRICKS_TOKEN", "")

if not token:
    print("Warning: DATABRICKS_TOKEN not set, skipping proxy setup")
    sys.exit(0)

# OpenCode proxy: sanitizes chat-completions traffic
if gateway_host:
    opencode_upstream = f"{gateway_host}/mlflow/v1"
else:
    opencode_upstream = f"{host}/serving-endpoints"
_start_proxy(OPENCODE_PROXY_PORT, opencode_upstream, passthrough=False, label="opencode")

# Codex proxy: token-injection only. Codex caches OPENAI_API_KEY at startup
# and never re-reads ~/.codex/.env, so a long-lived TUI outlives the 15-min
# rotated PAT. Routing through this proxy lets every request grab the fresh
# token from ~/.databrickscfg via _get_fresh_token().
# Path layout: Codex sends to /v1/responses against base_url=http://127.0.0.1:4001/v1,
# proxy appends path to upstream → {gateway}/openai/v1/responses.
if gateway_host:
    _start_proxy(CODEX_PROXY_PORT, f"{gateway_host}/openai", passthrough=True, label="codex")
else:
    print("No AI Gateway configured — skipping Codex proxy (direct serving-endpoints "
          "has no /responses route)")
