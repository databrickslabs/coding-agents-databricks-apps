#!/usr/bin/env python3
"""Stdio-to-HTTP MCP bridge with Databricks OAuth token injection.

Proxies MCP JSON-RPC (stdio) to a Databricks App (Streamable HTTP),
injecting fresh OAuth tokens via `databricks auth token`.

Config via environment variables (set in Claude Code settings.json):

    CODA_MCP_URL         — App MCP endpoint URL
    DATABRICKS_PROFILE   — Databricks CLI profile for auth
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

APP_URL = os.environ.get("CODA_MCP_URL", "")
PROFILE = os.environ.get("DATABRICKS_PROFILE", "DEFAULT")
TOKEN_TTL = 1800  # cache 30 min (tokens last 60)

_cache = {"token": None, "expires_at": 0.0}
_session_id = None


def _log(msg):
    print(f"[coda-bridge] {msg}", file=sys.stderr, flush=True)


def _get_token(force=False):
    now = time.time()
    if not force and _cache["token"] and now < _cache["expires_at"]:
        return _cache["token"]
    result = subprocess.run(
        ["databricks", "auth", "token", "-p", PROFILE],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"databricks auth token failed: {result.stderr.strip()}")
    data = json.loads(result.stdout)
    _cache["token"] = data["access_token"]
    _cache["expires_at"] = now + TOKEN_TTL
    _log("OAuth token refreshed")
    return _cache["token"]


def _forward(line):
    global _session_id
    token = _get_token()

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    }
    if _session_id:
        headers["Mcp-Session-Id"] = _session_id

    req = urllib.request.Request(APP_URL, data=line.encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                _session_id = sid
            body = resp.read().decode()
            if body.strip():
                sys.stdout.write(body.rstrip("\n") + "\n")
                sys.stdout.flush()
    except urllib.error.HTTPError as e:
        if e.code in (302, 401, 403):
            _log(f"Auth failed ({e.code}), forcing token refresh")
            token = _get_token(force=True)
            headers["Authorization"] = f"Bearer {token}"
            retry = urllib.request.Request(APP_URL, data=line.encode(), headers=headers, method="POST")
            with urllib.request.urlopen(retry, timeout=300) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    _session_id = sid
                body = resp.read().decode()
                if body.strip():
                    sys.stdout.write(body.rstrip("\n") + "\n")
                    sys.stdout.flush()
        else:
            raise


def main():
    if not APP_URL:
        _log("FATAL: CODA_MCP_URL not set")
        sys.exit(1)
    _log(f"Proxying to {APP_URL} (profile={PROFILE})")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            _forward(line)
        except Exception as e:
            _log(f"Error: {e}")
            try:
                msg_id = json.loads(line).get("id")
            except Exception:
                msg_id = None
            if msg_id is not None:
                err = json.dumps({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32000, "message": str(e)},
                })
                sys.stdout.write(err + "\n")
                sys.stdout.flush()


if __name__ == "__main__":
    main()
