"""Tests for AppUrlCaptureMiddleware — populates url_builder._app_url_cache."""
import asyncio
import importlib

import pytest

from coda_mcp import url_builder


@pytest.fixture(autouse=True)
def _reset_cache():
    importlib.reload(url_builder)
    yield


async def _fake_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"", "more_body": False})


def _make_scope(headers: list[tuple[bytes, bytes]]):
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": "POST",
        "path": "/mcp",
        "headers": headers,
    }


async def _drive(middleware, scope):
    sent = []
    async def send(msg): sent.append(msg)
    async def receive(): return {"type": "http.request", "body": b"", "more_body": False}
    await middleware(scope, receive, send)


def test_middleware_captures_x_forwarded_host():
    from coda_mcp.mcp_asgi import AppUrlCaptureMiddleware
    mw = AppUrlCaptureMiddleware(_fake_app)
    scope = _make_scope([(b"x-forwarded-host", b"app.databricksapps.com")])
    asyncio.run(_drive(mw, scope))
    assert url_builder._app_url_cache == "app.databricksapps.com"


def test_middleware_falls_back_to_host_when_no_xforwarded():
    from coda_mcp.mcp_asgi import AppUrlCaptureMiddleware
    mw = AppUrlCaptureMiddleware(_fake_app)
    scope = _make_scope([(b"host", b"localhost:8000")])
    asyncio.run(_drive(mw, scope))
    assert url_builder._app_url_cache == "localhost:8000"


def test_middleware_skips_non_http_scope():
    from coda_mcp.mcp_asgi import AppUrlCaptureMiddleware
    mw = AppUrlCaptureMiddleware(_fake_app)
    scope = {"type": "lifespan"}
    async def receive(): return {"type": "lifespan.startup"}
    sent = []
    async def send(msg): sent.append(msg)
    # Must not crash. Cache stays None.
    asyncio.run(mw(scope, receive, send))
    assert url_builder._app_url_cache is None


def test_middleware_no_op_when_no_host_header():
    from coda_mcp.mcp_asgi import AppUrlCaptureMiddleware
    mw = AppUrlCaptureMiddleware(_fake_app)
    scope = _make_scope([])
    asyncio.run(_drive(mw, scope))
    assert url_builder._app_url_cache is None
