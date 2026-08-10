"""Tests for per-agent installation gates."""

from __future__ import annotations

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
