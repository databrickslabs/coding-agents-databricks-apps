"""Regression tests for direct Databricks CLI use with the SP broker."""

from __future__ import annotations

import os


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
