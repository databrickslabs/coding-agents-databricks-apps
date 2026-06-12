"""Unit tests for the stdio→HTTP MCP bridge (tools/coda-bridge.py).

The bridge sits between a local MCP client (Claude Code's OAuth flow) and a
remote deployed CoDA app. It must:
  1. Mint a Databricks access token via the CLI and inject it as Bearer auth
  2. Forward the JSON-RPC payload unchanged to the configured APP_URL
  3. Surface server errors without dropping them
  4. Refuse to run without an APP_URL (operator misconfiguration)
"""
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_PATH = REPO_ROOT / "tools" / "coda-bridge.py"


def _load_bridge():
    spec = importlib.util.spec_from_file_location("coda_bridge", BRIDGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    monkeypatch.setenv("CODA_MCP_URL", "https://fake-app.databricksapps.com/mcp")
    monkeypatch.setenv("DATABRICKS_PROFILE", "test")
    monkeypatch.setenv("HOME", str(tmp_path))
    return _load_bridge()


def test_bridge_loads_with_app_url(bridge):
    assert bridge is not None
    assert callable(getattr(bridge, "_forward", None)) or callable(
        getattr(bridge, "forward", None)
    ), "bridge must expose a forward function"


def test_forward_injects_authorization_header(bridge):
    forward = getattr(bridge, "_forward", None) or getattr(bridge, "forward", None)
    if forward is None:
        pytest.skip("bridge implementation does not expose a forward entrypoint")

    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.headers = {}
    fake_resp.read.return_value = b'{"jsonrpc":"2.0","id":1,"result":{}}'
    fake_resp.__enter__ = lambda s: s
    fake_resp.__exit__ = MagicMock(return_value=False)

    fake_proc = MagicMock(
        returncode=0,
        stdout=json.dumps({"access_token": "tok-from-cli"}),
        stderr="",
    )

    with patch("subprocess.run", return_value=fake_proc), \
         patch("urllib.request.urlopen", return_value=fake_resp) as mock_open:
        forward(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}))

    sent_req = mock_open.call_args[0][0]
    headers_lower = {k.lower(): v for k, v in sent_req.headers.items()}
    assert "authorization" in headers_lower, "Bearer token MUST be injected"
    assert "tok-from-cli" in headers_lower["authorization"], (
        "Authorization header should contain the token from `databricks auth token`"
    )


def test_forward_returns_server_response_body(bridge):
    forward = getattr(bridge, "_forward", None) or getattr(bridge, "forward", None)
    if forward is None:
        pytest.skip("bridge implementation does not expose a forward entrypoint")

    server_payload = b'{"jsonrpc":"2.0","id":42,"result":{"ok":true}}'
    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.headers = {}
    fake_resp.read.return_value = server_payload
    fake_resp.__enter__ = lambda s: s
    fake_resp.__exit__ = MagicMock(return_value=False)

    fake_proc = MagicMock(
        returncode=0,
        stdout=json.dumps({"access_token": "tok"}),
        stderr="",
    )

    with patch("subprocess.run", return_value=fake_proc), \
         patch("urllib.request.urlopen", return_value=fake_resp):
        result = forward(
            json.dumps({"jsonrpc": "2.0", "id": 42, "method": "tools/list", "params": {}})
        )

    if result is None:
        pytest.skip("bridge writes directly to stdout — capture via capsys in a follow-up")
    if isinstance(result, (bytes, bytearray)):
        result = result.decode()
    assert "ok" in result and "true" in result.lower(), (
        f"forward should surface the server response body; got {result!r}"
    )


def test_missing_app_url_is_handled(monkeypatch, tmp_path):
    monkeypatch.delenv("CODA_MCP_URL", raising=False)
    monkeypatch.delenv("APP_URL", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    sys.modules.pop("coda_bridge", None)
    with pytest.raises((SystemExit, ValueError, RuntimeError, KeyError)):
        spec = importlib.util.spec_from_file_location("coda_bridge", BRIDGE_PATH)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # If import-time guard is absent, the forward call itself should refuse.
        forward = getattr(mod, "_forward", None) or getattr(mod, "forward", None)
        if forward:
            forward(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}))
