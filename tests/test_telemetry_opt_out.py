"""Tests for the telemetry opt-out path (CODA_TELEMETRY_DISABLED).

Enterprise procurement teams require an inventory of every outbound data
flow. The opt-out lets operators ship CoDA with no disclosed telemetry,
which is often the only way to pass third-party-risk review for regulated
workspaces.
"""

from __future__ import annotations

from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("CODA_TELEMETRY_DISABLED", raising=False)


def test_telemetry_disabled_default_false():
    from telemetry import _telemetry_disabled

    assert _telemetry_disabled() is False


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", " true "])
def test_telemetry_disabled_truthy_values(value, monkeypatch):
    monkeypatch.setenv("CODA_TELEMETRY_DISABLED", value)
    from telemetry import _telemetry_disabled

    assert _telemetry_disabled() is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "", "maybe"])
def test_telemetry_disabled_falsy_values(value, monkeypatch):
    monkeypatch.setenv("CODA_TELEMETRY_DISABLED", value)
    from telemetry import _telemetry_disabled

    assert _telemetry_disabled() is False


def test_log_telemetry_noop_when_disabled(monkeypatch):
    """When opt-out is set, log_telemetry must not spawn the background thread."""
    monkeypatch.setenv("CODA_TELEMETRY_DISABLED", "true")
    from telemetry import log_telemetry

    with mock.patch("telemetry.threading.Thread") as mock_thread:
        log_telemetry("test_event", "1")
        mock_thread.assert_not_called()


def test_log_telemetry_fires_when_enabled(monkeypatch):
    """Default (opt-out unset) must still spawn the telemetry thread."""
    from telemetry import log_telemetry

    with mock.patch("telemetry.threading.Thread") as mock_thread:
        mock_thread.return_value.start = mock.Mock()
        log_telemetry("test_event", "1")
        mock_thread.assert_called_once()
        mock_thread.return_value.start.assert_called_once()


# ---------------------------------------------------------------------------
# Stable Flask session key (FLASK_SECRET_KEY)
# ---------------------------------------------------------------------------


class TestFlaskSecretKey:
    """`app.secret_key` signs session cookies. Regenerating it per worker start
    silently invalidates every live session, so operators can pin it."""

    def _resolver(self):
        import app as app_module

        return app_module._resolve_secret_key

    def test_uses_configured_key(self, monkeypatch):
        monkeypatch.setenv("FLASK_SECRET_KEY", "s3cret-from-databricks")

        assert self._resolver()() == b"s3cret-from-databricks"

    def test_configured_key_is_stable_across_calls(self, monkeypatch):
        """The whole point: two workers reading the same env var agree."""
        monkeypatch.setenv("FLASK_SECRET_KEY", "s3cret-from-databricks")
        resolve = self._resolver()

        assert resolve() == resolve()

    def test_whitespace_only_key_is_treated_as_unset(self, monkeypatch):
        """An env var wired to an empty secret must not become the signing key."""
        monkeypatch.setenv("FLASK_SECRET_KEY", "   ")
        resolve = self._resolver()

        key = resolve()
        assert key != b"   "
        assert len(key) == 24  # the os.urandom fallback

    def test_falls_back_to_random_and_warns(self, monkeypatch, caplog):
        import logging

        monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
        resolve = self._resolver()

        with caplog.at_level(logging.WARNING, logger="app"):
            first, second = resolve(), resolve()

        assert first != second, "fallback must be random per call"
        assert len(first) == 24
        assert any("FLASK_SECRET_KEY not set" in r.message for r in caplog.records)
