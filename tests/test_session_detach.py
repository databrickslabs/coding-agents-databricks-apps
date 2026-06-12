"""Tests for session detach & reconnect helpers.

Covers:
- _get_session_process() — foreground child detection
- GET /api/sessions — list active sessions with metadata
"""

import os
import subprocess
import threading
import time
from collections import deque
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers — import app with initialize_app mocked out
# ---------------------------------------------------------------------------

def _get_app():
    """Import app with initialize_app mocked out."""
    with mock.patch("app.initialize_app"):
        import app as app_module
        app_module.app.config["TESTING"] = True
        return app_module


# ---------------------------------------------------------------------------
# Tests for _get_session_process
# ---------------------------------------------------------------------------


class TestGetSessionProcess:
    """Tests for _get_session_process() helper."""

    def test_detects_child_process_name(self):
        """When a shell has a child process, return the child's name."""
        app_mod = _get_app()

        # Mock pgrep returning a child PID, then ps resolving it to "sleep"
        pgrep_result = mock.Mock(returncode=0, stdout="12345\n")
        ps_result = mock.Mock(returncode=0, stdout="sleep\n")
        with mock.patch("subprocess.run", side_effect=[pgrep_result, ps_result]):
            result = app_mod._get_session_process(100)
        assert result == "sleep", f"Expected 'sleep', got '{result}'"

    def test_returns_parent_process_name_when_no_children(self):
        """When a shell has no foreground children, return the shell name."""
        app_mod = _get_app()

        # Mock pgrep finding no children (exit 1), then ps resolving the process itself
        pgrep_result = mock.Mock(returncode=1, stdout="")
        ps_result = mock.Mock(returncode=0, stdout="cat\n")
        with mock.patch("subprocess.run", side_effect=[pgrep_result, ps_result]):
            result = app_mod._get_session_process(100)
        assert result == "cat", f"Expected 'cat', got '{result}'"

    def test_returns_unknown_for_dead_pid(self):
        """Return 'unknown' when the PID does not exist."""
        app_mod = _get_app()

        # Use a PID that almost certainly doesn't exist
        result = app_mod._get_session_process(999999999)
        assert result == "unknown"

    def test_returns_unknown_for_invalid_pid(self):
        """Return 'unknown' for negative or zero PIDs."""
        app_mod = _get_app()

        assert app_mod._get_session_process(-1) == "unknown"
        assert app_mod._get_session_process(0) == "unknown"


# ---------------------------------------------------------------------------
# Tests for GET /api/sessions
# ---------------------------------------------------------------------------


class TestListSessions:
    """Tests for the GET /api/sessions endpoint."""

    @pytest.fixture(autouse=True)
    def setup_app(self):
        app_module = _get_app()
        app_module.app_owner = "test@example.com"
        # Clear leftover sessions from prior test classes
        with app_module.sessions_lock:
            app_module.sessions.clear()
        self.client = app_module.app.test_client()
        self.app_module = app_module
        yield
        with app_module.sessions_lock:
            app_module.sessions.clear()

    def test_returns_empty_list(self):
        resp = self.client.get("/api/sessions")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_returns_session_with_metadata(self):
        # Add a session with our own PID (so ps works)
        now = time.time()
        with self.app_module.sessions_lock:
            self.app_module.sessions["sess-1"] = {
                "pid": os.getpid(),
                "master_fd": 0,
                "output_buffer": deque(maxlen=1000),
                "lock": threading.Lock(),
                "last_poll_time": now - 120,
                "created_at": now - 3600,
            }
        resp = self.client.get("/api/sessions")
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["session_id"] == "sess-1"
        assert "process" in data[0]
        assert "idle_seconds" in data[0]

    def test_excludes_exited_sessions(self):
        with self.app_module.sessions_lock:
            self.app_module.sessions["dead"] = {
                "pid": 1, "master_fd": 0,
                "output_buffer": deque(maxlen=1000),
                "lock": threading.Lock(),
                "last_poll_time": time.time(),
                "created_at": time.time(),
                "exited": True,
            }
        resp = self.client.get("/api/sessions")
        assert resp.get_json() == []


# ---------------------------------------------------------------------------
# Tests for POST /api/session/attach
# ---------------------------------------------------------------------------


class TestAttachSession:
    @pytest.fixture(autouse=True)
    def setup_app(self):
        import app as app_module
        app_module.app_owner = "test@example.com"
        self.client = app_module.app.test_client()
        self.app_module = app_module
        yield
        with app_module.sessions_lock:
            app_module.sessions.clear()

    def test_returns_buffer_and_metadata(self):
        now = time.time()
        with self.app_module.sessions_lock:
            self.app_module.sessions["sess-a"] = {
                "pid": os.getpid(), "master_fd": 0,
                "output_buffer": deque(["line1\r\n", "line2\r\n"], maxlen=1000),
                "lock": threading.Lock(),
                "last_poll_time": now - 300,
                "created_at": now - 7200,
            }
        resp = self.client.post("/api/session/attach", json={"session_id": "sess-a"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["session_id"] == "sess-a"
        assert data["output"] == ["line1\r\n", "line2\r\n"]
        assert "process" in data

    def test_resets_last_poll_time(self):
        old = time.time() - 600
        with self.app_module.sessions_lock:
            self.app_module.sessions["sess-b"] = {
                "pid": os.getpid(), "master_fd": 0,
                "output_buffer": deque(maxlen=1000),
                "lock": threading.Lock(),
                "last_poll_time": old, "created_at": old,
            }
        self.client.post("/api/session/attach", json={"session_id": "sess-b"})
        sess = self.app_module.sessions["sess-b"]
        assert sess["last_poll_time"] > old

    def test_404_missing(self):
        resp = self.client.post("/api/session/attach", json={"session_id": "nope"})
        assert resp.status_code == 404

    def test_404_exited(self):
        with self.app_module.sessions_lock:
            self.app_module.sessions["sess-x"] = {
                "pid": 1, "master_fd": 0,
                "output_buffer": deque(maxlen=1000),
                "lock": threading.Lock(),
                "last_poll_time": time.time(), "created_at": time.time(),
                "exited": True,
            }
        resp = self.client.post("/api/session/attach", json={"session_id": "sess-x"})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Tests for EOF cleanup in read_pty_output
# ---------------------------------------------------------------------------


class TestEOFCleanup:
    @pytest.fixture(autouse=True)
    def setup_app(self):
        import app as app_module
        self.app_module = app_module
        yield
        with app_module.sessions_lock:
            app_module.sessions.clear()

    def test_exited_session_removed_from_dict(self):
        fake_master = 50
        # Use a completed process so waitpid works
        proc = subprocess.Popen(
            ["bash", "-c", "exit 0"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        proc.wait()

        session_id = "sess-eof-test"
        with self.app_module.sessions_lock:
            self.app_module.sessions[session_id] = {
                "pid": proc.pid,
                "master_fd": fake_master,
                "output_buffer": deque(maxlen=1000),
                "lock": threading.Lock(),
                "last_poll_time": time.time(),
                "created_at": time.time(),
            }

        # Simulate EOF: select says readable, os.read returns empty bytes
        with mock.patch("select.select", return_value=([fake_master], [], [])), \
             mock.patch("os.read", return_value=b""), \
             mock.patch("os.close"), \
             mock.patch("os.kill"):
            self.app_module.read_pty_output(session_id, fake_master)

        with self.app_module.sessions_lock:
            assert session_id not in self.app_module.sessions
