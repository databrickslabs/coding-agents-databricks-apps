"""Native MCP ASGI app with WebSocket support for terminal I/O.

Architecture (all on one port, one uvicorn process):

    socketio.ASGIApp          ← /socket.io/  → native ASGI WebSocket (terminal)
        └── mcp_starlette     ← /mcp         → FastMCP Streamable HTTP (Genie Code)
                └── WSGI(Flask) ← /*          → REST API, static files (HTTP only)

Usage in app.yaml::

    command: ["uvicorn", "coda_mcp.mcp_asgi:app", "--host", "0.0.0.0", "--port", "8000"]
"""

import os
import logging
import warnings

import socketio as socketio_lib
from starlette.middleware.cors import CORSMiddleware

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    from starlette.middleware.wsgi import WSGIMiddleware

from coda_mcp.mcp_server import mcp as mcp_instance, set_app_hooks
from coda_mcp import url_builder
from utils import ensure_https

logger = logging.getLogger(__name__)


class AppUrlCaptureMiddleware:
    """Capture X-Forwarded-Host (or Host) from every inbound HTTP request and
    populate url_builder._app_url_cache. Used so MCP tools can return a
    working viewer_url without manual configuration.

    Caveat: /socket.io/ traffic is intercepted by socketio.ASGIApp *before*
    reaching mcp_starlette, so WebSocket connect requests never hit this
    middleware. This is fine in practice — every HTTP request to /mcp and to
    Flask routes does hit it, which is enough to keep the cache hot.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            headers = dict(scope.get("headers") or [])
            host_bytes = headers.get(b"x-forwarded-host") or headers.get(b"host")
            if host_bytes:
                try:
                    url_builder.capture_from_headers(host_bytes.decode("latin-1"))
                except Exception:
                    pass
        await self.app(scope, receive, send)

# ── Build allowed origins ─────────────────────────────────────────
# The browser connects from the app's own URL (e.g. mcp-test-coda-*.databricksapps.com)
# which differs from DATABRICKS_HOST (workspace URL). Databricks proxy handles auth,
# so Socket.IO CORS can safely allow all origins. Starlette CORSMiddleware below
# uses the same list for MCP/Flask routes.
_databricks_host = os.environ.get("DATABRICKS_HOST", "")
ALLOWED_ORIGINS = []
if _databricks_host:
    ALLOWED_ORIGINS.append(ensure_https(_databricks_host).rstrip("/"))

# ── Import and initialize Flask app ────────────────────────────────
from app import (
    app as flask_app,
    initialize_app,
    mcp_create_pty_session,
    mcp_send_input,
    mcp_close_pty_session,
    register_sio_handlers,
)

initialize_app()

# Wire MCP tools to PTY infrastructure
set_app_hooks(
    create_session_fn=mcp_create_pty_session,
    send_input_fn=mcp_send_input,
    close_session_fn=mcp_close_pty_session,
)

# ── Async Socket.IO server (native ASGI WebSocket) ───────────────
# python-socketio AsyncServer handles /socket.io/ with real WebSocket,
# eliminating the WSGIMiddleware limitation that forced HTTP polling fallback.
sio = socketio_lib.AsyncServer(
    async_mode='asgi',
    cors_allowed_origins='*',  # App URL differs from DATABRICKS_HOST; proxy handles auth
    logger=False,
    engineio_logger=False,
)

# Register terminal I/O event handlers (connect, join_session, terminal_input, etc.)
register_sio_handlers(sio)

# ── Build the ASGI app per Genie Code docs ─────────────────────────
mcp_starlette = mcp_instance.streamable_http_app()

# Mount Flask as catch-all via WSGI adapter (HTTP routes only)
flask_asgi = WSGIMiddleware(flask_app.wsgi_app)
mcp_starlette.mount("/", app=flask_asgi)

# CORS for MCP and Flask routes
mcp_starlette.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Capture X-Forwarded-Host into url_builder cache (for MCP viewer_url).
# Added AFTER CORS so it wraps the CORS-handled request.
mcp_starlette.add_middleware(AppUrlCaptureMiddleware)

# ── Top-level ASGI app ────────────────────────────────────────────
# socketio.ASGIApp intercepts /socket.io/ for WebSocket + polling,
# passes everything else to mcp_starlette (MCP at /mcp, Flask at /)
app = socketio_lib.ASGIApp(sio, other_asgi_app=mcp_starlette)
