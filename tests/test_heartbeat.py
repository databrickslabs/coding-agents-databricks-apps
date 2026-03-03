"""Tests for /api/heartbeat endpoint — lightweight keep-alive."""

import time
from collections import deque
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_test_client():
    """Create a Flask test client for the app."""
    with mock.patch("app.initialize_app"):
        import app as app_module
        app_module.app.config["TESTING"] = True
        return app_module.app.test_client(), app_module


def _create_fake_session(app_module, session_id="test-session-123", **overrides):
    """Insert a fake session into the sessions dict."""
    session = {
        "master_fd": 999,
        "pid": 12345,
        "output_buffer": deque(maxlen=1000),
        "last_poll_time": time.time() - 60,  # 60s ago
        "created_at": time.time(),
    }
    session.update(overrides)
    with app_module.sessions_lock:
        app_module.sessions[session_id] = session
    return session


def _cleanup_session(app_module, session_id="test-session-123"):
    """Remove a fake session after test."""
    with app_module.sessions_lock:
        app_module.sessions.pop(session_id, None)


# ---------------------------------------------------------------------------
# 1. Valid session heartbeat
# ---------------------------------------------------------------------------

class TestHeartbeatValid:
    """Heartbeat with a valid session should return 200 and reset timeout."""

    def test_heartbeat_returns_200(self):
        client, app_module = _get_test_client()
        _create_fake_session(app_module)
        try:
            resp = client.post("/api/heartbeat",
                               json={"session_id": "test-session-123"})
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["status"] == "ok"
        finally:
            _cleanup_session(app_module)

    def test_heartbeat_resets_last_poll_time(self):
        client, app_module = _get_test_client()
        old_time = time.time() - 120
        _create_fake_session(app_module, last_poll_time=old_time)
        try:
            before = time.time()
            client.post("/api/heartbeat",
                        json={"session_id": "test-session-123"})
            after = time.time()

            with app_module.sessions_lock:
                new_poll_time = app_module.sessions["test-session-123"]["last_poll_time"]

            assert new_poll_time >= before
            assert new_poll_time <= after
        finally:
            _cleanup_session(app_module)


# ---------------------------------------------------------------------------
# 2. Unknown session
# ---------------------------------------------------------------------------

class TestHeartbeatUnknownSession:
    """Heartbeat with an unknown session should return 404."""

    def test_unknown_session_returns_404(self):
        client, app_module = _get_test_client()
        resp = client.post("/api/heartbeat",
                           json={"session_id": "nonexistent-session"})
        assert resp.status_code == 404
        assert "Session not found" in resp.get_json()["error"]


# ---------------------------------------------------------------------------
# 3. Does NOT drain output buffer (critical invariant)
# ---------------------------------------------------------------------------

class TestHeartbeatPreservesBuffer:
    """Heartbeat must NOT touch the output buffer."""

    def test_heartbeat_does_not_drain_output_buffer(self):
        client, app_module = _get_test_client()
        _create_fake_session(app_module)
        try:
            # Add some output to the buffer
            with app_module.sessions_lock:
                buf = app_module.sessions["test-session-123"]["output_buffer"]
                buf.append("line 1\r\n")
                buf.append("line 2\r\n")
                buf_len_before = len(buf)

            # Send heartbeat
            resp = client.post("/api/heartbeat",
                               json={"session_id": "test-session-123"})
            assert resp.status_code == 200

            # Buffer should be untouched
            with app_module.sessions_lock:
                buf = app_module.sessions["test-session-123"]["output_buffer"]
                assert len(buf) == buf_len_before
                assert list(buf) == ["line 1\r\n", "line 2\r\n"]
        finally:
            _cleanup_session(app_module)


# ---------------------------------------------------------------------------
# 4. Timeout warning flag
# ---------------------------------------------------------------------------

class TestHeartbeatTimeoutWarning:
    """Heartbeat should return and clear the timeout_warning flag."""

    def test_returns_timeout_warning_when_set(self):
        client, app_module = _get_test_client()
        _create_fake_session(app_module, timeout_warning=True)
        try:
            resp = client.post("/api/heartbeat",
                               json={"session_id": "test-session-123"})
            body = resp.get_json()
            assert body["timeout_warning"] is True
        finally:
            _cleanup_session(app_module)

    def test_clears_timeout_warning_after_returning(self):
        client, app_module = _get_test_client()
        _create_fake_session(app_module, timeout_warning=True)
        try:
            # First call: should return True and clear it
            client.post("/api/heartbeat",
                        json={"session_id": "test-session-123"})

            # Second call: should return False
            resp = client.post("/api/heartbeat",
                               json={"session_id": "test-session-123"})
            body = resp.get_json()
            assert body["timeout_warning"] is False
        finally:
            _cleanup_session(app_module)

    def test_no_timeout_warning_by_default(self):
        client, app_module = _get_test_client()
        _create_fake_session(app_module)
        try:
            resp = client.post("/api/heartbeat",
                               json={"session_id": "test-session-123"})
            body = resp.get_json()
            assert body["timeout_warning"] is False
        finally:
            _cleanup_session(app_module)
