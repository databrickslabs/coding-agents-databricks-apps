"""Builds the viewer_url returned by CoDA MCP tools.

Resolution order:
1. ``CODA_APP_URL`` env var (explicit override for local dev / power users).
2. Module-level cache populated by ``AppUrlCaptureMiddleware`` from the
   ``X-Forwarded-Host`` header (officially provided by Databricks Apps).
3. ``None`` — caller omits the field entirely.

The cache is process-global (single uvicorn worker per app) and refreshed
on every inbound HTTP request.
"""
from __future__ import annotations

import os
from typing import Optional

_app_url_cache: Optional[str] = None


def capture_from_headers(host: Optional[str]) -> None:
    """Called by the ASGI middleware on every inbound HTTP request.

    No-op when ``host`` is falsy (None or empty) to avoid wiping a good
    cache value with a missing header on a probe/CORS preflight.

    Strips any accidental ``https://`` / ``http://`` prefix on the way in
    so build_viewer_url's unconditional ``https://`` prepend can't produce
    a double-scheme URL.
    """
    global _app_url_cache
    if host:
        host = host.removeprefix("https://").removeprefix("http://").strip("/")
        if host:
            _app_url_cache = host


def build_viewer_url(pty_session_id: str) -> Optional[str]:
    """Return the full viewer URL for a PTY session, or None if no base is known."""
    override = os.environ.get("CODA_APP_URL", "").strip()
    if override:
        base = override.rstrip("/")
    elif _app_url_cache:
        base = f"https://{_app_url_cache}"
    else:
        return None
    return f"{base}/?session={pty_session_id}"
