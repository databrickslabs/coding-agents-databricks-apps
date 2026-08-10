"""Tests for per-agent installation gates."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import omnigents_host as oh


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_claude_setup_is_skipped_when_disabled(tmp_path):
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "ENABLE_CLAUDE": "false",
    }
    result = subprocess.run(
        [sys.executable, "setup_claude.py"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "ENABLE_CLAUDE=false" in result.stdout
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_omnigent_host_skips_opencode_proxy_when_disabled(monkeypatch):
    monkeypatch.setenv("ENABLE_OPENCODE", "false")
    ready = mock.Mock()
    run = mock.Mock()
    monkeypatch.setattr(oh, "_proxy_ready", ready)
    monkeypatch.setattr(oh.subprocess, "run", run)

    oh._ensure_content_filter_proxy()

    ready.assert_not_called()
    run.assert_not_called()


def test_omnigent_host_keeps_healthy_opencode_proxy(monkeypatch):
    monkeypatch.setenv("ENABLE_OPENCODE", "true")
    monkeypatch.setattr(oh, "_proxy_ready", mock.Mock(return_value=True))
    run = mock.Mock()
    monkeypatch.setattr(oh.subprocess, "run", run)

    oh._ensure_content_filter_proxy()

    run.assert_not_called()


def test_omnigent_host_starts_missing_opencode_proxy(monkeypatch):
    monkeypatch.setenv("ENABLE_OPENCODE", "true")
    monkeypatch.setattr(oh, "_proxy_ready", mock.Mock(side_effect=[False, True]))
    run = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(oh.subprocess, "run", run)

    oh._ensure_content_filter_proxy()

    run.assert_called_once()
    assert run.call_args.args[0][-1] == "setup_proxy.py"


@pytest.mark.parametrize(
    ("result", "ready", "message"),
    [
        (SimpleNamespace(returncode=7, stdout="", stderr="failed"), True, "status 7"),
        (SimpleNamespace(returncode=0, stdout="", stderr=""), False, "unhealthy after setup"),
    ],
)
def test_omnigent_host_rejects_failed_or_unhealthy_proxy_setup(
    monkeypatch, result, ready, message
):
    monkeypatch.setenv("ENABLE_OPENCODE", "true")
    monkeypatch.setattr(oh, "_proxy_ready", mock.Mock(side_effect=[False, ready]))
    monkeypatch.setattr(oh.subprocess, "run", mock.Mock(return_value=result))

    with pytest.raises(RuntimeError, match=message):
        oh._ensure_content_filter_proxy()


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (subprocess.TimeoutExpired("setup_proxy.py", 180), "timed out"),
        (OSError("cannot spawn"), "setup failed"),
    ],
)
def test_omnigent_host_rejects_proxy_setup_exceptions(monkeypatch, failure, message):
    monkeypatch.setenv("ENABLE_OPENCODE", "true")
    monkeypatch.setattr(oh, "_proxy_ready", mock.Mock(return_value=False))
    monkeypatch.setattr(oh.subprocess, "run", mock.Mock(side_effect=failure))

    with pytest.raises(RuntimeError, match=message):
        oh._ensure_content_filter_proxy()


def test_supervisor_does_not_register_host_when_proxy_is_unhealthy(monkeypatch):
    oh.reset_for_tests()
    monkeypatch.setattr(oh, "ensure_installed", lambda sp_creds=None: True)
    monkeypatch.setattr(oh, "_omnigent_broker_capability", lambda: (True, "compatible"))
    monkeypatch.setattr(oh, "_write_oauth_profile", lambda creds: None)
    monkeypatch.setattr(oh, "_install_broker_cli_wrapper", lambda: None)
    monkeypatch.setattr(oh, "_claude_enabled", lambda: False)
    monkeypatch.setattr(oh, "_pi_enabled", lambda: False)
    monkeypatch.setattr(oh, "_opencode_enabled", lambda: True)
    monkeypatch.setattr(
        oh,
        "_ensure_content_filter_proxy",
        mock.Mock(side_effect=RuntimeError("OpenCode content-filter proxy is unhealthy")),
    )
    run_host = mock.Mock()
    monkeypatch.setattr(oh, "_run_host_once", run_host)

    oh._supervise("https://omnigent.example.com", {}, threading.Event())

    run_host.assert_not_called()
    status = oh.get_status()
    assert status["stage"] == "proxy_failed"
    assert status["host_launched"] is False
    assert status["running"] is False
    assert "proxy is unhealthy" in status["last_error"]


class _HealthResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self._body = (
            payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        )

    def read(self, _limit):
        return self._body


def _healthy_payload():
    return {
        "service": "coda-content-filter-proxy",
        "schema": 1,
        "status": "ready",
        "upstream": "https://workspace.example.com/serving-endpoints",
        "upstream_ready": True,
        "upstream_status": 200,
        "workspace": "https://workspace.example.com",
        "workspace_status": None,
        "check": "workspace-serving-endpoints",
        "readiness_semantics": (
            "authenticated-workspace-serving-endpoints-listing"
        ),
    }


def test_proxy_ready_accepts_exact_authenticated_readiness(monkeypatch):
    urlopen = mock.Mock(return_value=_HealthResponse(_healthy_payload()))
    monkeypatch.setattr(oh, "urlopen", urlopen)

    assert oh._proxy_ready() is True
    assert urlopen.call_args.kwargs["timeout"] == 5


def test_proxy_ready_accepts_ucode_mlflow_foundation_readiness(monkeypatch):
    payload = {
        **_healthy_payload(),
        "upstream": "https://workspace.example.com/ai-gateway/mlflow/v1",
        "upstream_status": None,
        "workspace": "https://workspace.example.com",
        "workspace_status": 200,
        "check": "workspace-foundation-models",
        "readiness_semantics": (
            "authenticated-workspace-foundation-models-for-mlflow-route"
        ),
    }
    monkeypatch.setattr(oh, "urlopen", lambda *_args, **_kwargs: _HealthResponse(payload))

    assert oh._proxy_ready() is True


def test_proxy_ready_rejects_standalone_mlflow_400(monkeypatch):
    payload = {
        **_healthy_payload(),
        "upstream": "https://gateway.example.com/mlflow/v1",
        "upstream_status": 400,
        "workspace_status": None,
        "check": "mlflow-models",
        "readiness_semantics": "authenticated-route-listing-unsupported",
    }
    monkeypatch.setattr(oh, "urlopen", lambda *_args, **_kwargs: _HealthResponse(payload))

    assert oh._proxy_ready() is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("service", "forged-listener"),
        ("schema", 2),
        ("status", "ok"),
        ("upstream_ready", False),
        ("upstream_status", 503),
        ("workspace_status", 200),
        ("check", "workspace-foundation-models"),
        ("readiness_semantics", "forged-semantics"),
        ("workspace", "http://workspace.example.com"),
        ("workspace", "https://user@workspace.example.com"),
        ("workspace", "https://workspace.example.com/path"),
        ("upstream", "http://workspace.example.com/serving-endpoints"),
        ("upstream", "https://workspace.example.com/unrelated"),
        ("upstream", "https://workspace.example.com/serving-endpoints?forged=1"),
    ],
)
def test_proxy_ready_rejects_forged_or_unready_listener(
    monkeypatch, field, value
):
    payload = _healthy_payload()
    payload[field] = value
    monkeypatch.setattr(oh, "urlopen", lambda *_args, **_kwargs: _HealthResponse(payload))

    assert oh._proxy_ready() is False


@pytest.mark.parametrize(
    "response",
    [
        _HealthResponse(b"not-json"),
        _HealthResponse(b"x" * 4097),
        _HealthResponse(_healthy_payload(), status=503),
    ],
)
def test_proxy_ready_rejects_malformed_oversized_or_non_200(monkeypatch, response):
    monkeypatch.setattr(oh, "urlopen", lambda *_args, **_kwargs: response)

    assert oh._proxy_ready() is False
