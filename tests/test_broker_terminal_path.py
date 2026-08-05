"""Regression tests for direct Databricks CLI use with the SP broker."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock


def test_broker_wrapper_is_installed_when_cli_exists(tmp_path, monkeypatch):
    import app

    real_cli = tmp_path / ".local" / "bin" / "databricks"
    real_cli.parent.mkdir(parents=True)
    real_cli.write_text("#!/bin/sh\nexit 0\n")
    real_cli.chmod(0o700)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(app.BROKER_URL_ENV, "http://127.0.0.1:12345/token")

    with mock.patch.object(app, "write_databricks_token_wrapper") as write:
        write.side_effect = lambda directory, cli: Path(directory).mkdir(parents=True, exist_ok=True) or Path(directory) / "databricks"
        assert app._ensure_broker_cli_wrapper() is True

    write.assert_called_once_with(str(tmp_path / ".coda-broker-bin"), str(real_cli))


def test_terminal_env_puts_broker_before_real_cli(tmp_path, monkeypatch):
    import app

    broker = tmp_path / ".coda-broker-bin"
    broker.mkdir()
    (broker / "databricks").write_text("shim")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(app.BROKER_URL_ENV, "http://127.0.0.1:12345/token")
    monkeypatch.setenv("ENABLE_SP_APIKEYHELPER", "true")
    monkeypatch.setenv("PATH", "/system/bin")

    env = app._build_terminal_shell_env(dict(os.environ))
    # The endpoint's session wrapper adds ~/.local/bin after this helper; the
    # broker bin must already be first in the shell environment.
    assert env["PATH"].split(":")[0] == str(broker)
    assert "DATABRICKS_TOKEN" not in env
    assert "DATABRICKS_HOST" not in env
