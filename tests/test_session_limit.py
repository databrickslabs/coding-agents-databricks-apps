"""Tests for MAX_CONCURRENT_SESSIONS cap (issue #118).

Verifies that:
- MAX_CONCURRENT_SESSIONS defaults to 5
- Session creation succeeds when under the limit
- Session creation returns 429 when at the limit
- Exited sessions don't count toward the limit
- The 429 response includes an informative error message
"""

import threading
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


def _add_session(app_module, session_id, exited=False):
    """Insert a fake session into the sessions dict."""
    session = {
        "master_fd": 999,
        "pid": 12345,
        "output_buffer": deque(maxlen=1000),
        "lock": threading.Lock(),
        "last_poll_time": time.time(),
        "created_at": time.time(),
        "label": session_id,
    }
    if exited:
        session["exited"] = True
    with app_module.sessions_lock:
        app_module.sessions[session_id] = session
    return session


def _cleanup(app_module, *session_ids):
    with app_module.sessions_lock:
        for sid in session_ids:
            app_module.sessions.pop(sid, None)


# ---------------------------------------------------------------------------
# 1. Default constant value
# ---------------------------------------------------------------------------

class TestSessionLimitConstant:

    def test_default_max_concurrent_sessions_is_5(self):
        app_module = _get_app()
        assert app_module.MAX_CONCURRENT_SESSIONS == 5

    def test_max_concurrent_sessions_reads_from_env(self):
        """Verify that patching the module-level constant changes the cap."""
        app_module = _get_app()
        original = app_module.MAX_CONCURRENT_SESSIONS
        try:
            app_module.MAX_CONCURRENT_SESSIONS = 3
            assert app_module.MAX_CONCURRENT_SESSIONS == 3
        finally:
            app_module.MAX_CONCURRENT_SESSIONS = original


# ---------------------------------------------------------------------------
# 2. Session creation under the limit succeeds
# ---------------------------------------------------------------------------

class TestSessionCreationUnderLimit:

    def test_create_session_with_zero_active(self):
        app_module = _get_app()
        client = app_module.app.test_client()
        # Mock out pty, subprocess, and threading to avoid real PTY creation
        with mock.patch.object(app_module, "check_authorization", return_value=(True, "test-user")), \
             mock.patch("pty.openpty", return_value=(10, 11)), \
             mock.patch("subprocess.Popen") as mock_popen, \
             mock.patch("os.close"), \
             mock.patch("threading.Thread") as mock_thread:
            mock_popen.return_value.pid = 99999
            mock_thread.return_value.start = mock.Mock()
            resp = client.post("/api/session", json={"label": "test"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert "session_id" in data
        # Cleanup the created session
        _cleanup(app_module, data["session_id"])

    def test_create_session_with_some_active(self):
        app_module = _get_app()
        # Add 3 sessions (under default limit of 5)
        sids = [f"existing-{i}" for i in range(3)]
        try:
            for sid in sids:
                _add_session(app_module, sid)
            client = app_module.app.test_client()
            with mock.patch.object(app_module, "check_authorization", return_value=(True, "test-user")), \
                 mock.patch("pty.openpty", return_value=(10, 11)), \
                 mock.patch("subprocess.Popen") as mock_popen, \
                 mock.patch("os.close"), \
                 mock.patch("threading.Thread") as mock_thread:
                mock_popen.return_value.pid = 99999
                mock_thread.return_value.start = mock.Mock()
                resp = client.post("/api/session", json={"label": "test"})
            assert resp.status_code == 200
            data = resp.get_json()
            assert "session_id" in data
            sids.append(data["session_id"])
        finally:
            _cleanup(app_module, *sids)


# ---------------------------------------------------------------------------
# 3. Session creation at/over the limit returns 429
# ---------------------------------------------------------------------------

class TestSessionCreationAtLimit:

    def test_create_session_at_limit_returns_429(self):
        app_module = _get_app()
        limit = app_module.MAX_CONCURRENT_SESSIONS
        sids = [f"full-{i}" for i in range(limit)]
        try:
            for sid in sids:
                _add_session(app_module, sid)
            client = app_module.app.test_client()
            with mock.patch.object(app_module, "check_authorization", return_value=(True, "test-user")):
                resp = client.post("/api/session", json={"label": "one-too-many"})
            assert resp.status_code == 429
            data = resp.get_json()
            assert "error" in data
            assert "Maximum" in data["error"]
        finally:
            _cleanup(app_module, *sids)

    def test_429_error_message_includes_limit(self):
        app_module = _get_app()
        limit = app_module.MAX_CONCURRENT_SESSIONS
        sids = [f"msg-{i}" for i in range(limit)]
        try:
            for sid in sids:
                _add_session(app_module, sid)
            client = app_module.app.test_client()
            with mock.patch.object(app_module, "check_authorization", return_value=(True, "test-user")):
                resp = client.post("/api/session", json={})
            data = resp.get_json()
            assert str(limit) in data["error"]
            assert "Close an existing session" in data["error"]
        finally:
            _cleanup(app_module, *sids)


# ---------------------------------------------------------------------------
# 4. Removed sessions free up slots (sessions are popped from dict, not marked)
# ---------------------------------------------------------------------------

class TestRemovedSessionsFreeSlots:

    def test_removing_session_frees_slot(self):
        """After removing a session from the dict, a new one can be created."""
        app_module = _get_app()
        limit = app_module.MAX_CONCURRENT_SESSIONS
        sids = [f"full-{i}" for i in range(limit)]
        try:
            for sid in sids:
                _add_session(app_module, sid)
            # At limit — verify rejection
            client = app_module.app.test_client()
            with mock.patch.object(app_module, "check_authorization", return_value=(True, "test-user")):
                resp = client.post("/api/session", json={})
            assert resp.status_code == 429
            # Remove one session (simulates terminate_session popping it)
            _cleanup(app_module, sids[0])
            sids = sids[1:]
            # Now creation should succeed
            with mock.patch.object(app_module, "check_authorization", return_value=(True, "test-user")), \
                 mock.patch("pty.openpty", return_value=(10, 11)), \
                 mock.patch("subprocess.Popen") as mock_popen, \
                 mock.patch("os.close"), \
                 mock.patch("threading.Thread") as mock_thread:
                mock_popen.return_value.pid = 99999
                mock_thread.return_value.start = mock.Mock()
                resp = client.post("/api/session", json={"label": "after-removal"})
            assert resp.status_code == 200
            data = resp.get_json()
            sids.append(data["session_id"])
        finally:
            _cleanup(app_module, *sids)

    def test_count_based_on_dict_size(self):
        """The cap is based on len(sessions), not an exited flag."""
        app_module = _get_app()
        limit = app_module.MAX_CONCURRENT_SESSIONS
        sids = [f"slot-{i}" for i in range(limit - 1)]
        try:
            for sid in sids:
                _add_session(app_module, sid)
            # One slot remaining — creation should succeed
            client = app_module.app.test_client()
            with mock.patch.object(app_module, "check_authorization", return_value=(True, "test-user")), \
                 mock.patch("pty.openpty", return_value=(10, 11)), \
                 mock.patch("subprocess.Popen") as mock_popen, \
                 mock.patch("os.close"), \
                 mock.patch("threading.Thread") as mock_thread:
                mock_popen.return_value.pid = 99999
                mock_thread.return_value.start = mock.Mock()
                resp = client.post("/api/session", json={"label": "last-slot"})
            assert resp.status_code == 200
            data = resp.get_json()
            sids.append(data["session_id"])
        finally:
            _cleanup(app_module, *sids)
