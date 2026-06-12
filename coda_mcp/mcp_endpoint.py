"""Flask Blueprint fallback for MCP JSON-RPC.

NOTE: This is NOT the production path. Production deployment uses
`coda_mcp.mcp_asgi:app` served by uvicorn, which mounts the native MCP
SDK Streamable HTTP transport at /mcp. This module is a Flask-native
JSON-RPC fallback used only under WSGI runtimes (gunicorn local dev,
tests that exercise the Flask test client without spinning up ASGI).

Both paths expose the same four tools (coda_run, coda_inbox,
coda_get_result, coda_interactive) and produce equivalent JSON-RPC responses,
so switching between them is transparent to MCP clients.
"""
import asyncio
import json
import logging
from flask import Blueprint, request, jsonify

logger = logging.getLogger(__name__)

mcp_bp = Blueprint("mcp", __name__)

# Import tool functions from mcp_server.py
from coda_mcp.mcp_server import (
    mcp as mcp_instance,
    coda_run,
    coda_inbox,
    coda_get_result,
    coda_interactive,
)

# Tool function dispatch
_TOOL_DISPATCH = {
    "coda_run": coda_run,
    "coda_inbox": coda_inbox,
    "coda_get_result": coda_get_result,
    "coda_interactive": coda_interactive,
}

SERVER_INFO = {
    "name": "coda",
    "version": "1.0.0",
}

CAPABILITIES = {
    "tools": {"listChanged": False},
}



def _cors_headers():
    """Build CORS response headers.

    Permissive CORS for /mcp — the Databricks Apps proxy handles auth.
    """
    headers = {}
    origin = request.headers.get("Origin", "")
    if origin:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        # Explicitly list all headers Genie Code might send
        # (wildcard * is incompatible with credentials=true per CORS spec)
        allowed_headers = ", ".join([
            "Content-Type", "Authorization", "Accept",
            "Mcp-Session-Id", "X-Request-Id", "X-Requested-With",
            "X-Forwarded-Email", "X-Forwarded-User", "X-Databricks-User-Email",
            "Cookie", "Origin", "Referer",
        ])
        headers["Access-Control-Allow-Headers"] = allowed_headers
        headers["Access-Control-Allow-Credentials"] = "true"
        headers["Access-Control-Max-Age"] = "86400"
    return headers


@mcp_bp.route("/mcp", methods=["POST", "OPTIONS", "GET"])
def mcp_handler():
    # Handle CORS preflight
    if request.method == "OPTIONS":
        resp = jsonify({})
        resp.status_code = 204
        for k, v in _cors_headers().items():
            resp.headers[k] = v
        return resp

    # Handle GET for SSE (not supported in stateless mode)
    if request.method == "GET":
        resp = jsonify({"error": "SSE not supported. Use POST."})
        resp.status_code = 405
        return resp

    # Origin validation skipped — Databricks Apps proxy handles auth.

    data = request.get_json(silent=True) or {}
    method = data.get("method", "")
    req_id = data.get("id")
    params = data.get("params", {})

    # Route by method
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion", "2025-03-26"),
            "capabilities": CAPABILITIES,
            "serverInfo": SERVER_INFO,
            "instructions": mcp_instance._instructions if hasattr(mcp_instance, '_instructions') else "",
        }
        resp = jsonify({"jsonrpc": "2.0", "id": req_id, "result": result})

    elif method == "notifications/initialized":
        # No-op acknowledgment — return empty OK
        resp = jsonify({})
        resp.status_code = 200

    elif method == "tools/list":
        tools = _build_tools_list()
        resp = jsonify({"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}})

    elif method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        tool_fn = _TOOL_DISPATCH.get(tool_name)
        if not tool_fn:
            resp = jsonify({
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}
            })
        else:
            try:
                # Tool functions are async — run them
                result_str = asyncio.run(tool_fn(**arguments))
                result_data = json.loads(result_str)
                resp = jsonify({
                    "jsonrpc": "2.0", "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": result_str}],
                        "isError": "error" in result_data,
                    }
                })
            except Exception as e:
                resp = jsonify({
                    "jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32603, "message": str(e)}
                })

    elif method == "ping":
        resp = jsonify({"jsonrpc": "2.0", "id": req_id, "result": {}})

    else:
        resp = jsonify({
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"}
        })

    # Add CORS headers
    for k, v in _cors_headers().items():
        resp.headers[k] = v

    return resp


def _build_tools_list():
    """Extract tool definitions from FastMCP registry."""
    tools = []
    # Access FastMCP's internal tool manager
    tool_manager = mcp_instance._tool_manager
    for name, tool in tool_manager._tools.items():
        tool_dict = {
            "name": tool.name,
            "description": tool.description or "",
            "inputSchema": tool.parameters if hasattr(tool, 'parameters') else {},
        }
        if hasattr(tool, 'annotations') and tool.annotations:
            tool_dict["annotations"] = {}
            if tool.annotations.readOnlyHint is not None:
                tool_dict["annotations"]["readOnlyHint"] = tool.annotations.readOnlyHint
            if tool.annotations.destructiveHint is not None:
                tool_dict["annotations"]["destructiveHint"] = tool.annotations.destructiveHint
            if tool.annotations.idempotentHint is not None:
                tool_dict["annotations"]["idempotentHint"] = tool.annotations.idempotentHint
        tools.append(tool_dict)
    return tools
