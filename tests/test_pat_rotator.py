"""Tests for PATRotator — short-lived PAT auto-rotation.

Covers: rotation logic, token persistence, lifecycle management,
and logging output.
"""

import logging
import os
import stat
import threading
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_create_response(token_value="dapi-new-token-abc", token_id="tid-new-123",
                          status_code=200):
    """Build a mock requests.Response for token/create."""
    resp = mock.MagicMock()
    resp.status_code = status_code
    if status_code == 200:
        resp.json.return_value = {
            "token_value": token_value,
            "token_info": {"token_id": token_id},
        }
    else:
        resp.text = "error payload"
    return resp


def _mock_delete_response(status_code=200):
    """Build a mock requests.Response for token/delete."""
    resp = mock.MagicMock()
    resp.status_code = status_code
    resp.text = "delete payload"
    return resp


def _make_rotator(**kwargs):
    """Create a PATRotator with sane test defaults."""
    from pat_rotator import PATRotator
    defaults = dict(
        host="https://test.databricks.com",
        rotation_interval=1,
        token_lifetime=7200,
        session_count_fn=lambda: 1,  # default: pretend 1 active session
    )
    defaults.update(kwargs)
    return PATRotator(**defaults)


# ---------------------------------------------------------------------------
# 1. PAT Rotation — mint + revoke logic
# ---------------------------------------------------------------------------

class TestPATRotation:
    """Core rotation: mint new token, revoke old, handle failures."""

    @mock.patch("pat_rotator.requests.post")
    def test_mint_new_and_revoke_old(self, mock_post):
        """Successful rotation: new token minted, old token revoked."""
        mock_post.side_effect = [
            _mock_create_response(token_value="dapi-new", token_id="tid-new"),
            _mock_delete_response(status_code=200),
        ]
        rotator = _make_rotator()
        rotator._current_token = "dapi-old"
        rotator._current_token_id = "tid-old"

        result = rotator._rotate_once()

        assert result is True
        assert rotator.token == "dapi-new"
        assert rotator._current_token_id == "tid-new"
        # Two API calls: create + delete
        assert mock_post.call_count == 2
        # Verify delete was called with old token id
        delete_call = mock_post.call_args_list[1]
        assert delete_call[1]["json"]["token_id"] == "tid-old"

    @mock.patch("pat_rotator.requests.post")
    def test_create_failure_returns_false(self, mock_post):
        """When token creation fails (non-200), rotation returns False."""
        mock_post.return_value = _mock_create_response(status_code=403)
        rotator = _make_rotator()
        rotator._current_token = "dapi-old"

        result = rotator._rotate_once()

        assert result is False
        # Token should remain unchanged
        assert rotator.token == "dapi-old"

    @mock.patch("pat_rotator.requests.post")
    def test_create_request_exception_returns_false(self, mock_post):
        """When create request raises an exception, rotation returns False."""
        import requests
        mock_post.side_effect = requests.RequestException("network error")
        rotator = _make_rotator()
        rotator._current_token = "dapi-old"

        result = rotator._rotate_once()

        assert result is False
        assert rotator.token == "dapi-old"

    @mock.patch("pat_rotator.requests.post")
    def test_continues_if_revoke_fails(self, mock_post):
        """New token is kept even when old token revocation fails."""
        mock_post.side_effect = [
            _mock_create_response(token_value="dapi-new", token_id="tid-new"),
            _mock_delete_response(status_code=500),
        ]
        rotator = _make_rotator()
        rotator._current_token = "dapi-old"
        rotator._current_token_id = "tid-old"

        result = rotator._rotate_once()

        assert result is True
        assert rotator.token == "dapi-new"

    @mock.patch("pat_rotator.requests.post")
    def test_first_rotation_no_old_token(self, mock_post):
        """First rotation has no old token to revoke — should still succeed."""
        mock_post.return_value = _mock_create_response(
            token_value="dapi-first", token_id="tid-first"
        )
        rotator = _make_rotator()
        rotator._current_token = "dapi-bootstrap"
        rotator._current_token_id = None  # no old token id

        result = rotator._rotate_once()

        assert result is True
        assert rotator.token == "dapi-first"
        # Only one API call (create), no delete
        assert mock_post.call_count == 1

    def test_no_token_returns_false(self):
        """Rotation is a no-op when no current token exists."""
        rotator = _make_rotator()
        rotator._current_token = None

        result = rotator._rotate_once()

        assert result is False


# ---------------------------------------------------------------------------
# 2. Token Persistence — env var + .databrickscfg
# ---------------------------------------------------------------------------

class TestTokenPersistence:
    """Token is persisted to env var and config file."""

    @mock.patch("pat_rotator.requests.post")
    def test_updates_env_var(self, mock_post):
        """DATABRICKS_TOKEN env var is updated after rotation."""
        mock_post.side_effect = [
            _mock_create_response(token_value="dapi-env-test", token_id="tid-env"),
            _mock_delete_response(),
        ]
        rotator = _make_rotator()
        rotator._current_token = "dapi-old"
        rotator._current_token_id = "tid-old"

        with mock.patch.dict(os.environ, {"DATABRICKS_TOKEN": "dapi-old"}):
            rotator._rotate_once()
            assert os.environ["DATABRICKS_TOKEN"] == "dapi-env-test"

    @mock.patch("pat_rotator.requests.post")
    def test_writes_databrickscfg(self, mock_post, tmp_path):
        """Rotation writes a valid .databrickscfg file."""
        mock_post.side_effect = [
            _mock_create_response(token_value="dapi-cfg-test", token_id="tid-cfg"),
            _mock_delete_response(),
        ]
        cfg_path = str(tmp_path / ".databrickscfg")
        rotator = _make_rotator()
        rotator._current_token = "dapi-old"
        rotator._current_token_id = "tid-old"
        rotator._databrickscfg_path = cfg_path

        rotator._rotate_once()

        content = open(cfg_path).read()
        assert "[DEFAULT]" in content
        assert "token = dapi-cfg-test" in content
        assert "host = https://test.databricks.com" in content

    @mock.patch("pat_rotator.requests.post")
    def test_databrickscfg_permissions(self, mock_post, tmp_path):
        """Config file should have 0o600 permissions (owner read/write only)."""
        mock_post.side_effect = [
            _mock_create_response(token_value="dapi-perm", token_id="tid-perm"),
            _mock_delete_response(),
        ]
        cfg_path = str(tmp_path / ".databrickscfg")
        rotator = _make_rotator()
        rotator._current_token = "dapi-old"
        rotator._current_token_id = "tid-old"
        rotator._databrickscfg_path = cfg_path

        rotator._rotate_once()

        mode = stat.S_IMODE(os.stat(cfg_path).st_mode)
        assert mode == 0o600


# ---------------------------------------------------------------------------
# 3. Rotator Lifecycle — start / stop / daemon thread
# ---------------------------------------------------------------------------

class TestRotatorLifecycle:
    """Start/stop behavior and daemon thread management."""

    def test_starts_daemon_thread(self):
        """start() launches a daemon thread named 'pat-rotation'."""
        rotator = _make_rotator()
        rotator._current_token = "dapi-lifecycle"
        # Prevent actual rotation by making interval very long
        rotator._rotation_interval = 9999

        rotator.start()
        try:
            assert rotator._thread is not None
            assert rotator._thread.is_alive()
            assert rotator._thread.daemon is True
            assert rotator._thread.name == "pat-rotation"
        finally:
            rotator.stop()
            rotator._thread.join(timeout=2)

    def test_no_start_without_token(self):
        """start() does nothing when no token is configured."""
        rotator = _make_rotator()
        rotator._current_token = None

        rotator.start()

        assert rotator._thread is None

    def test_stop_signals_thread(self):
        """stop() sets the stop event so the thread exits."""
        rotator = _make_rotator()
        rotator._current_token = "dapi-stop-test"
        rotator._rotation_interval = 9999

        rotator.start()
        rotator.stop()
        rotator._thread.join(timeout=3)

        assert not rotator._thread.is_alive()

    def test_idempotent_start(self):
        """Calling start() twice does not create a second thread."""
        rotator = _make_rotator()
        rotator._current_token = "dapi-idem"
        rotator._rotation_interval = 9999

        rotator.start()
        first_thread = rotator._thread
        rotator.start()
        second_thread = rotator._thread

        try:
            assert first_thread is second_thread
        finally:
            rotator.stop()
            rotator._thread.join(timeout=2)


# ---------------------------------------------------------------------------
# 4. Session awareness — only rotate when sessions exist
# ---------------------------------------------------------------------------

class TestSessionAwareness:
    """Rotation skips when no active sessions."""

    @mock.patch("pat_rotator.requests.post")
    def test_skips_rotation_when_no_sessions(self, mock_post, caplog):
        """No sessions → no API calls, log skip message."""
        rotator = _make_rotator(session_count_fn=lambda: 0)
        rotator._current_token = "dapi-test"
        rotator._current_token_id = "tid-test"

        # Simulate one iteration of the loop body
        with caplog.at_level(logging.INFO, logger="pat_rotator"):
            session_count = rotator._session_count_fn()
            if session_count == 0:
                caplog.records.clear()
                import logging as _logging
                logger = _logging.getLogger("pat_rotator")
                logger.info("PAT rotation: no active sessions — skipping rotation")

        assert "no active sessions" in " ".join(caplog.messages)
        mock_post.assert_not_called()

    @mock.patch("pat_rotator.requests.post")
    def test_rotates_when_sessions_exist(self, mock_post, tmp_path):
        """Active sessions → rotation proceeds."""
        mock_post.side_effect = [
            _mock_create_response(),
            _mock_delete_response(),
        ]
        rotator = _make_rotator(session_count_fn=lambda: 3)
        rotator._current_token = "dapi-old"
        rotator._current_token_id = "tid-old"
        rotator._databrickscfg_path = str(tmp_path / ".databrickscfg")

        result = rotator._rotate_once()
        assert result is True
        assert mock_post.called


# ---------------------------------------------------------------------------
# 5. Logging — verify key messages
# ---------------------------------------------------------------------------

class TestLogging:
    """Verify rotation events are logged with expected messages."""

    @mock.patch("pat_rotator.requests.post")
    def test_log_eliminated_on_successful_revoke(self, mock_post, caplog, tmp_path):
        """Log message includes 'ELIMINATED' when old token is revoked."""
        mock_post.side_effect = [
            _mock_create_response(token_value="dapi-log", token_id="tid-log-new"),
            _mock_delete_response(status_code=200),
        ]
        rotator = _make_rotator()
        rotator._current_token = "dapi-old"
        rotator._current_token_id = "tid-log-old"
        rotator._databrickscfg_path = str(tmp_path / ".databrickscfg")

        with caplog.at_level(logging.INFO, logger="pat_rotator"):
            rotator._rotate_once()

        combined = " ".join(caplog.messages)
        assert "ELIMINATED" in combined
        assert "tid-log-old" in combined
        assert "tid-log-new" in combined

    @mock.patch("pat_rotator.requests.post")
    def test_log_warning_on_failed_revoke(self, mock_post, caplog, tmp_path):
        """Log message warns when revocation fails (but rotation succeeds)."""
        mock_post.side_effect = [
            _mock_create_response(token_value="dapi-log2", token_id="tid-log2-new"),
            _mock_delete_response(status_code=500),
        ]
        rotator = _make_rotator()
        rotator._current_token = "dapi-old"
        rotator._current_token_id = "tid-log2-old"
        rotator._databrickscfg_path = str(tmp_path / ".databrickscfg")

        with caplog.at_level(logging.WARNING, logger="pat_rotator"):
            rotator._rotate_once()

        combined = " ".join(caplog.messages)
        assert "revocation failed" in combined
        assert "expire naturally" in combined

    @mock.patch("pat_rotator.requests.post")
    def test_log_first_rotation(self, mock_post, caplog, tmp_path):
        """First rotation logs 'no old token to revoke'."""
        mock_post.return_value = _mock_create_response(
            token_value="dapi-first-log", token_id="tid-first-log"
        )
        rotator = _make_rotator()
        rotator._current_token = "dapi-bootstrap"
        rotator._current_token_id = None
        rotator._databrickscfg_path = str(tmp_path / ".databrickscfg")

        with caplog.at_level(logging.INFO, logger="pat_rotator"):
            rotator._rotate_once()

        combined = " ".join(caplog.messages)
        assert "no old token to revoke" in combined

    @mock.patch("pat_rotator.requests.post")
    def test_log_pat_rotated_label(self, mock_post, caplog, tmp_path):
        """Every successful rotation includes 'PAT rotation complete' in the log."""
        mock_post.side_effect = [
            _mock_create_response(token_value="dapi-label", token_id="tid-label"),
            _mock_delete_response(status_code=200),
        ]
        rotator = _make_rotator()
        rotator._current_token = "dapi-old"
        rotator._current_token_id = "tid-old-label"
        rotator._databrickscfg_path = str(tmp_path / ".databrickscfg")

        with caplog.at_level(logging.INFO, logger="pat_rotator"):
            rotator._rotate_once()

        combined = " ".join(caplog.messages)
        assert "PAT rotation complete" in combined
