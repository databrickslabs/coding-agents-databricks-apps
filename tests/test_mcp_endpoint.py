"""Unit tests for the Flask Blueprint fallback at coda_mcp.mcp_endpoint.

Production traffic flows through coda_mcp.mcp_asgi (uvicorn + native MCP SDK).
This blueprint is the WSGI-only fallback. These tests pin the JSON-RPC contract
so the two paths stay in lockstep.
"""
import json

import pytest


@pytest.fixture
def client():
    from app import app as flask_app

    return flask_app.test_client()


def _rpc(method, params=None, rpc_id=1):
    return {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}}


def test_initialize_returns_server_info(client):
    r = client.post("/mcp", json=_rpc("initialize", {"protocolVersion": "2025-03-26"}))
    assert r.status_code == 200
    body = r.get_json()
    assert body["jsonrpc"] == "2.0"
    assert body["result"]["serverInfo"]["name"] == "coda"
    assert "capabilities" in body["result"]


def test_tools_list_returns_v2_tools(client):
    r = client.post("/mcp", json=_rpc("tools/list", {}, rpc_id=2))
    assert r.status_code == 200
    tools = r.get_json()["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"coda_run", "coda_inbox", "coda_get_result", "coda_interactive"}, (
        f"Tool surface drifted from the v2 contract (docs/mcp-v2-background-execution.md). Got: {names}"
    )


def test_tools_list_each_tool_has_description_and_schema(client):
    r = client.post("/mcp", json=_rpc("tools/list", {}, rpc_id=3))
    for t in r.get_json()["result"]["tools"]:
        assert t.get("description"), f"tool {t['name']} missing description (MCP requires it)"
        assert isinstance(t.get("inputSchema"), dict), f"tool {t['name']} missing inputSchema"


def test_cors_preflight_returns_204(client):
    r = client.options(
        "/mcp",
        headers={
            "Origin": "https://test.cloud.databricks.com",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.status_code == 204
    assert "Access-Control-Allow-Origin" in r.headers


def test_ping_returns_empty_result(client):
    r = client.post("/mcp", json=_rpc("ping", {}, rpc_id=4))
    assert r.status_code == 200
    body = r.get_json()
    assert body["result"] == {}
    assert "error" not in body


def test_unknown_method_returns_method_not_found(client):
    r = client.post("/mcp", json=_rpc("does/not/exist", {}, rpc_id=5))
    body = r.get_json()
    assert body.get("error", {}).get("code") == -32601, (
        f"Expected JSON-RPC method-not-found (-32601); got {body}"
    )


def test_unknown_tool_returns_jsonrpc_error(client):
    r = client.post(
        "/mcp",
        json=_rpc("tools/call", {"name": "not_a_real_tool", "arguments": {}}, rpc_id=6),
    )
    body = r.get_json()
    assert "error" in body or (
        "result" in body and body["result"].get("isError") is True
    ), f"Calling an unknown tool should error; got {body}"


def test_jsonrpc_id_is_echoed(client):
    for rpc_id in (7, "string-id", 0):
        r = client.post("/mcp", json=_rpc("ping", {}, rpc_id=rpc_id))
        assert r.get_json()["id"] == rpc_id


def test_post_with_non_json_body_does_not_crash(client):
    r = client.post(
        "/mcp",
        data="not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code in (200, 400)
    if r.status_code == 200:
        assert "error" in r.get_json()
