"""Tests for omnigents_host — the Omnigents agent-host integration.

Covers the off-by-default contract (AC-6) and the credential guards: the
feature must be inert with no config, and must refuse to start the host
without the OAuth-capable SP creds (a PAT alone is rejected by the Apps proxy).
"""

from __future__ import annotations

import omnigents_host as oh


def test_disabled_by_default(monkeypatch):
    """No OMNIGENTS_SERVER_URL -> feature reports disabled (AC-6)."""
    monkeypatch.delenv("OMNIGENTS_SERVER_URL", raising=False)
    assert oh.omnigents_host_enabled() is False


def test_enabled_when_server_url_set(monkeypatch):
    monkeypatch.setenv("OMNIGENTS_SERVER_URL", "https://srv.example.com")
    assert oh.omnigents_host_enabled() is True


def test_blank_server_url_is_disabled(monkeypatch):
    monkeypatch.setenv("OMNIGENTS_SERVER_URL", "   ")
    assert oh.omnigents_host_enabled() is False


def test_start_host_noop_when_disabled(monkeypatch):
    """Disabled -> start_host never installs or spawns a thread (AC-6)."""
    monkeypatch.delenv("OMNIGENTS_SERVER_URL", raising=False)
    monkeypatch.setattr(oh, "ensure_installed", _fail("ensure_installed"))
    monkeypatch.setattr(oh.threading, "Thread", _fail("Thread"))
    # Must return cleanly without touching install or threads.
    oh.start_host(sp_creds={"client_id": "x", "client_secret": "y", "host": "h"})


def test_start_host_refuses_without_sp_creds(monkeypatch):
    """Enabled but PAT-only (no SP creds) -> host NOT started (FR-4 guard)."""
    monkeypatch.setenv("OMNIGENTS_SERVER_URL", "https://srv.example.com")
    monkeypatch.setattr(oh, "ensure_installed", _fail("ensure_installed"))
    monkeypatch.setattr(oh.threading, "Thread", _fail("Thread"))
    oh.start_host(sp_creds=None)  # must return without installing or spawning


def test_capture_sp_credentials_returns_none_when_absent(monkeypatch):
    for v in ("DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET", "DATABRICKS_HOST"):
        monkeypatch.delenv(v, raising=False)
    assert oh.capture_sp_credentials() is None


def test_capture_sp_credentials_returns_creds_when_present(monkeypatch):
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "cid")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "secret")
    monkeypatch.setenv("DATABRICKS_HOST", "https://host.example.com")
    creds = oh.capture_sp_credentials()
    assert creds == {
        "client_id": "cid",
        "client_secret": "secret",
        "host": "https://host.example.com",
    }


def test_write_oauth_profile_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    creds = {"client_id": "cid", "client_secret": "sec", "host": "https://h"}
    oh._write_oauth_profile(creds)
    oh._write_oauth_profile(creds)  # second call must not duplicate the block
    cfg = (tmp_path / ".databrickscfg").read_text()
    assert cfg.count(f"[{oh._HOST_PROFILE}]") == 1
    assert "client_id = cid" in cfg


def _fail(name):
    def _raise(*a, **k):
        raise AssertionError(f"{name} should not be called")

    return _raise
