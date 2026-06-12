"""Tests for /api/session/attach replay fallback."""
import json
import os
from pathlib import Path

import pytest

from coda_mcp import task_manager

try:
    import pty as _pty
    _master, _slave = _pty.openpty()
    os.close(_master)
    os.close(_slave)
    _PTY_AVAILABLE = True
except Exception:
    _PTY_AVAILABLE = False

_pty_skip = pytest.mark.skipif(
    not _PTY_AVAILABLE,
    reason="PTY not allocatable in this environment",
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path))
    monkeypatch.setenv("MAX_CONCURRENT_SESSIONS", "5")
    import app as app_module
    # Set app_owner so check_authorization returns (True, None) for requests
    # with no user header (same pattern used by test_session_detach.py)
    app_module.app_owner = "test@example.com"
    with app_module.app.test_client() as c:
        yield c, tmp_path


def _seed_transcript(sessions_root: Path, pty_id: str, content: bytes) -> None:
    sess_id = "sess-test"
    task_id = "task-test"
    sdir = sessions_root / sess_id
    tdir = sdir / "tasks" / task_id
    tdir.mkdir(parents=True)
    (sdir / "session.json").write_text(json.dumps({
        "session_id": sess_id,
        "pty_session_id": pty_id,
        "current_task": None,
        "completed_tasks": [task_id],
        "status": "closed",
    }))
    (tdir / "transcript.log").write_bytes(content)


def test_attach_returns_replay_when_pty_gone_and_transcript_exists(client):
    c, root = client
    _seed_transcript(root, "pty-gone", b"hello\r\nworld\r\n")
    resp = c.post("/api/session/attach", json={"session_id": "pty-gone"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["replay"] is True
    assert data["output"] == ["hello\r\nworld\r\n"]
    assert data["label"] == "hermes-mcp (replay)"


def test_attach_404_when_pty_gone_and_no_transcript(client):
    c, root = client
    resp = c.post("/api/session/attach", json={"session_id": "pty-nope"})
    assert resp.status_code == 404


@_pty_skip
def test_attach_session_returns_replay_for_alive_replay_only_pty(tmp_path, monkeypatch):
    """A PTY created with `replay_only=True` (the flag introduced by coda_run's contract) that is still alive serves the transcript-from-disk, not the live output_buffer.

    This is the new contract introduced by the replay-only flag — historically
    a live PTY would serve its output_buffer.
    """
    from app import app as flask_app, mcp_create_pty_session, mcp_close_pty_session
    from coda_mcp import task_manager

    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path))

    sid = None
    try:
        sid = mcp_create_pty_session(label="replay-alive", replay_only=True)
        sess_id = "sess-x"
        task_id = "task-x"
        sdir = tmp_path / sess_id
        tdir = sdir / "tasks" / task_id
        tdir.mkdir(parents=True)
        (sdir / "session.json").write_text(
            '{"session_id": "%s", "pty_session_id": "%s", "current_task": "%s"}' % (sess_id, sid, task_id)
        )
        (tdir / "transcript.log").write_bytes(b"FROM DISK")
        # Cache may have stale entries from earlier tests — clear before the lookup.
        task_manager._pty_lookup_cache.clear()

        client = flask_app.test_client()
        resp = client.post("/api/session/attach", json={"session_id": sid})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["replay"] is True
        assert body["output"] == ["FROM DISK"]
    finally:
        if sid is not None:
            mcp_close_pty_session(sid)
