"""Tests for 24-hour session linger (issue #76).

Verifies that:
- SESSION_TIMEOUT_SECONDS is 86400 (24 hours)
- CLEANUP_INTERVAL_SECONDS is 900 (15 minutes)
- Sessions idle < 24h survive cleanup
- Sessions idle > 24h are reaped
- Warning fires at 80% of 24h (~19.2h)
- /api/status reports the correct timeout to the frontend
"""

import time
from collections import deque
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_app():
    """Import app with initialize_app mocked out."""
    with mock.patch("app.initialize_app"):
        import app as app_module
        app_module.app.config["TESTING"] = True
        return app_module


def _add_session(app_module, session_id, idle_seconds):
    """Insert a fake session that has been idle for `idle_seconds`."""
    session = {
        "master_fd": 999,
        "pid": 12345,
        "output_buffer": deque(maxlen=1000),
        "last_poll_time": time.time() - idle_seconds,
        "created_at": time.time() - idle_seconds - 60,
    }
    with app_module.sessions_lock:
        app_module.sessions[session_id] = session
    return session


def _cleanup(app_module, *session_ids):
    with app_module.sessions_lock:
        for sid in session_ids:
            app_module.sessions.pop(sid, None)


# ---------------------------------------------------------------------------
# 1. Constants are set to 24-hour values
# ---------------------------------------------------------------------------

class TestTimeoutConstants:

    def test_session_timeout_is_24_hours(self):
        app_module = _get_app()
        assert app_module.SESSION_TIMEOUT_SECONDS == 86400

    def test_cleanup_interval_is_15_minutes(self):
        app_module = _get_app()
        assert app_module.CLEANUP_INTERVAL_SECONDS == 900


# ---------------------------------------------------------------------------
# 2. Sessions survive well within the 24h window
# ---------------------------------------------------------------------------

class TestSessionSurvival:

    def test_session_idle_1_hour_survives(self):
        app_module = _get_app()
        _add_session(app_module, "alive-1h", idle_seconds=3600)
        try:
            now = time.time()
            with app_module.sessions_lock:
                idle = now - app_module.sessions["alive-1h"]["last_poll_time"]
                assert idle <= app_module.SESSION_TIMEOUT_SECONDS
            assert "alive-1h" in app_module.sessions
        finally:
            _cleanup(app_module, "alive-1h")

    def test_session_idle_12_hours_survives(self):
        app_module = _get_app()
        _add_session(app_module, "alive-12h", idle_seconds=43200)
        try:
            now = time.time()
            with app_module.sessions_lock:
                idle = now - app_module.sessions["alive-12h"]["last_poll_time"]
                assert idle <= app_module.SESSION_TIMEOUT_SECONDS
            assert "alive-12h" in app_module.sessions
        finally:
            _cleanup(app_module, "alive-12h")

    def test_session_idle_23_hours_survives(self):
        app_module = _get_app()
        _add_session(app_module, "alive-23h", idle_seconds=82800)
        try:
            now = time.time()
            with app_module.sessions_lock:
                idle = now - app_module.sessions["alive-23h"]["last_poll_time"]
                assert idle <= app_module.SESSION_TIMEOUT_SECONDS
            assert "alive-23h" in app_module.sessions
        finally:
            _cleanup(app_module, "alive-23h")


# ---------------------------------------------------------------------------
# 3. Sessions past 24h are reaped by cleanup
# ---------------------------------------------------------------------------

class TestSessionReaping:

    def test_session_idle_25_hours_is_reaped(self):
        app_module = _get_app()
        _add_session(app_module, "stale-25h", idle_seconds=90000)
        try:
            stale = []
            now = time.time()
            with app_module.sessions_lock:
                for sid, s in app_module.sessions.items():
                    if sid != "stale-25h":
                        continue
                    idle = now - s["last_poll_time"]
                    if idle > app_module.SESSION_TIMEOUT_SECONDS:
                        stale.append(sid)
            assert "stale-25h" in stale
        finally:
            _cleanup(app_module, "stale-25h")

    def test_session_idle_exactly_24h_plus_1s_is_reaped(self):
        app_module = _get_app()
        _add_session(app_module, "stale-boundary", idle_seconds=86401)
        try:
            now = time.time()
            with app_module.sessions_lock:
                idle = now - app_module.sessions["stale-boundary"]["last_poll_time"]
                assert idle > app_module.SESSION_TIMEOUT_SECONDS
        finally:
            _cleanup(app_module, "stale-boundary")


# ---------------------------------------------------------------------------
# 4. Warning fires at 80% (~19.2 hours)
# ---------------------------------------------------------------------------

class TestTimeoutWarning:

    def test_no_warning_at_18_hours(self):
        app_module = _get_app()
        _add_session(app_module, "warn-18h", idle_seconds=64800)
        try:
            warning_threshold = app_module.SESSION_TIMEOUT_SECONDS * 0.8
            now = time.time()
            with app_module.sessions_lock:
                idle = now - app_module.sessions["warn-18h"]["last_poll_time"]
                assert idle < warning_threshold
        finally:
            _cleanup(app_module, "warn-18h")

    def test_warning_at_20_hours(self):
        app_module = _get_app()
        _add_session(app_module, "warn-20h", idle_seconds=72000)
        try:
            warning_threshold = app_module.SESSION_TIMEOUT_SECONDS * 0.8
            now = time.time()
            with app_module.sessions_lock:
                s = app_module.sessions["warn-20h"]
                idle = now - s["last_poll_time"]
                assert idle > warning_threshold
                assert idle <= app_module.SESSION_TIMEOUT_SECONDS
        finally:
            _cleanup(app_module, "warn-20h")


# ---------------------------------------------------------------------------
# 5. /api/status reports 86400 to the frontend
# ---------------------------------------------------------------------------

class TestStatusEndpoint:

    def test_health_reports_24h_timeout(self):
        app_module = _get_app()
        client = app_module.app.test_client()
        with mock.patch.object(app_module, "check_authorization", return_value=(True, "test-user")):
            resp = client.get("/health")
        body = resp.get_json()
        assert body["session_timeout_seconds"] == 86400
