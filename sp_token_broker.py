"""Loopback-only broker for short-lived CoDA app service-principal tokens."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen

BROKER_URL_ENV = "CODA_SP_TOKEN_BROKER_URL"


def mint_sp_token(creds: dict[str, str]) -> str:
    """Mint a fresh M2M bearer without persisting the client secret."""
    from databricks.sdk.core import Config

    headers = Config(
        host=creds["host"],
        client_id=creds["client_id"],
        client_secret=creds["client_secret"],
        auth_type="oauth-m2m",
    ).authenticate()
    auth = (headers or {}).get("Authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else auth.strip()
    if not token:
        raise RuntimeError("app service principal returned no bearer token")
    return token


def start_sp_token_broker(
    mint_token: Callable[[], str], *, port: int = 0
) -> ThreadingHTTPServer:
    """Start a daemon HTTP server bound only to IPv4 loopback."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
            if self.path != "/token":
                self.send_error(404)
                return
            try:
                body = mint_token().encode()
            except Exception:
                self.send_error(503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(
        target=server.serve_forever,
        daemon=True,
        name="sp-token-broker",
    ).start()
    return server


def broker_url(server: ThreadingHTTPServer) -> str:
    """Return the token URL for a running broker."""
    host, port = server.server_address[:2]
    return f"http://{host}:{port}/token"


def fetch_sp_token(url: str | None = None) -> str | None:
    """Fetch a fresh bearer from the configured loopback broker."""
    target = (url or os.environ.get(BROKER_URL_ENV, "")).strip()
    if not target:
        return None
    try:
        with urlopen(target, timeout=5) as response:
            return response.read().decode().strip() or None
    except Exception:
        return None
