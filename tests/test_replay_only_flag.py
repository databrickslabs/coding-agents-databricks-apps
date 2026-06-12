"""Tests for the replay_only flag on PTY sessions."""
import inspect
import pytest

# Reuse the PTY-availability guard pattern from the suite.
import os
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


@_pty_skip
def test_mcp_create_pty_session_stores_replay_only_flag():
    """Creating a PTY with replay_only=True stores the flag in the session dict."""
    from app import mcp_create_pty_session, mcp_close_pty_session, sessions

    sid = mcp_create_pty_session(label="t1", replay_only=True)
    try:
        assert sessions[sid].get("replay_only") is True
    finally:
        mcp_close_pty_session(sid)


@_pty_skip
def test_mcp_create_pty_session_defaults_replay_only_false():
    """Default for replay_only is False (backward compat)."""
    from app import mcp_create_pty_session, mcp_close_pty_session, sessions

    sid = mcp_create_pty_session(label="t2")
    try:
        assert sessions[sid].get("replay_only") is False
    finally:
        mcp_close_pty_session(sid)


@_pty_skip
def test_attach_session_replay_only_alive_pty_returns_replay(tmp_path, monkeypatch):
    """A replay_only=True PTY that is still alive serves the transcript, not the live buffer."""
    from app import app as flask_app, mcp_create_pty_session, mcp_close_pty_session
    from coda_mcp import task_manager

    # Point task_manager at a tmp sessions root so find_task_dir_by_pty_session resolves.
    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path))

    # Create a fake task dir keyed by the PTY id we'll mint shortly.
    sid = mcp_create_pty_session(label="t-replay-alive", replay_only=True)
    try:
        # Plant a session.json that links task → this pty_session_id, plus a transcript.
        sess_id = "sess-fake"
        task_id = "task-fake"
        sdir = tmp_path / sess_id
        tdir = sdir / "tasks" / task_id
        tdir.mkdir(parents=True)
        (sdir / "session.json").write_text(
            '{"session_id": "%s", "pty_session_id": "%s", "current_task": "%s"}'
            % (sess_id, sid, task_id)
        )
        (tdir / "transcript.log").write_bytes(b"HELLO TRANSCRIPT")

        # Bust the lookup cache so find_task_dir_by_pty_session sees the new files.
        task_manager._pty_lookup_cache.clear()

        client = flask_app.test_client()
        resp = client.post("/api/session/attach", json={"session_id": sid})

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["replay"] is True
        assert body["output"] == ["HELLO TRANSCRIPT"]
    finally:
        mcp_close_pty_session(sid)


@_pty_skip
def test_attach_session_replay_only_false_alive_pty_returns_live_buffer():
    """A replay_only=False PTY that is still alive returns the live output_buffer (unchanged behavior)."""
    from app import app as flask_app, mcp_create_pty_session, mcp_close_pty_session

    sid = mcp_create_pty_session(label="t-live", replay_only=False)
    try:
        client = flask_app.test_client()
        resp = client.post("/api/session/attach", json={"session_id": sid})

        assert resp.status_code == 200
        body = resp.get_json()
        assert body.get("replay") in (False, None)  # live path doesn't set replay key
        assert "output" in body
    finally:
        mcp_close_pty_session(sid)


@_pty_skip
def test_coda_run_creates_pty_with_replay_only_true(tmp_path, monkeypatch):
    """coda_run must create its PTY with replay_only=True."""
    import asyncio
    import json
    from app import sessions, mcp_close_pty_session
    from coda_mcp import mcp_server, task_manager

    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path))
    # Stop the watcher from racing the test — we only care about creation here.
    monkeypatch.setattr(mcp_server, "_watch_task", lambda *a, **kw: None)

    result_str = asyncio.run(mcp_server.coda_run(prompt="ignored", email="t@example.com"))
    result = json.loads(result_str)
    session = task_manager._read_session(result["session_id"])
    pty_id = session.get("pty_session_id")
    try:
        assert pty_id is not None
        assert sessions[pty_id].get("replay_only") is True
    finally:
        if pty_id is not None:
            mcp_close_pty_session(pty_id)


def test_mcp_create_pty_session_signature_has_no_grace_param():
    """Regression guard: mcp_create_pty_session must not accept a 'grace' kwarg.

    Pure signature introspection — no PTY needed, runs unconditionally so
    that no-PTY environments (CI, sandboxed runners) still catch regressions.
    """
    from app import mcp_create_pty_session

    sig = inspect.signature(mcp_create_pty_session)
    assert "grace" not in sig.parameters, (
        f"mcp_create_pty_session should not accept a 'grace' parameter "
        f"(found in signature: {list(sig.parameters)})"
    )


@_pty_skip
def test_no_grace_key_in_session_dict():
    """Regression guard: session dicts from mcp_create_pty_session must not
    contain a 'grace' key.

    Protects against accidental re-introduction of grace-period machinery
    in future changes. PTY-gated because it actually allocates one.
    """
    from app import mcp_create_pty_session, mcp_close_pty_session, sessions

    sid = mcp_create_pty_session(label="t-no-grace", replay_only=True)
    try:
        assert "grace" not in sessions[sid], (
            f"session dict should not contain a 'grace' key "
            f"(found: {list(sessions[sid].keys())})"
        )
    finally:
        mcp_close_pty_session(sid)


@_pty_skip
def test_coda_run_does_not_create_project_dir(tmp_path, monkeypatch):
    """Regression guard: coda_run is Mode 3 (replay-only, no project dir).
    Only coda_interactive (Mode 2) creates dirs under ~/.coda/projects/.

    If a future change makes coda_run pull workspace files or otherwise
    creates a per-session project dir under ~/.coda/projects/, this test fires.
    """
    import asyncio
    import json
    import os
    from app import mcp_close_pty_session
    from coda_mcp import mcp_server, task_manager

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path / "sessions"))
    # Stop the watcher from racing the test.
    monkeypatch.setattr(mcp_server, "_watch_task", lambda *a, **kw: None)

    result_str = asyncio.run(mcp_server.coda_run(
        prompt="ignored", email="t@example.com",
    ))
    result = json.loads(result_str)
    pty_id = None
    try:
        sess = task_manager._read_session(result["session_id"])
        pty_id = sess.get("pty_session_id")

        # Project dir must NOT exist for coda_run.
        projects_root = os.path.join(str(tmp_path), ".coda", "projects")
        assert not os.path.isdir(projects_root) or not os.listdir(projects_root), (
            f"coda_run unexpectedly created project dirs under {projects_root}: "
            f"{os.listdir(projects_root) if os.path.isdir(projects_root) else 'n/a'}"
        )
    finally:
        if pty_id is not None:
            mcp_close_pty_session(pty_id)
