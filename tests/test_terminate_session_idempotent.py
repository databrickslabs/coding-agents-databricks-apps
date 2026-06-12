"""Regression test: terminate_session must close master_fd exactly once.

Both the explicit close path (mcp_close_pty_session) and the read-thread exit
path (read_pty_output, which calls terminate_session when its loop ends) fire
for the same session. If terminate_session closes master_fd on BOTH calls, the
second os.close() can land on a since-reused fd — e.g. an asyncio event loop's
self-pipe allocated by a later test — corrupting unrelated I/O. That is the
source of the intermittent 'OSError: [Errno 9] Bad file descriptor' (EBADF)
flakiness seen when PTY tests and asyncio tests run together.

terminate_session must be idempotent: claim the session atomically and close
the fd exactly once.
"""

import threading


def test_terminate_session_closes_master_fd_exactly_once(monkeypatch):
    import app

    closed = []
    monkeypatch.setattr(app.os, "close", lambda fd: closed.append(fd))
    monkeypatch.setattr(app.os, "kill", lambda *a, **k: None)
    monkeypatch.setattr(app.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(app, "_emit_from_thread", lambda *a, **k: None)

    fake_fd = 999777
    sid = "sess-idempotent-test"
    with app.sessions_lock:
        app.sessions[sid] = {
            "lock": threading.Lock(),
            "pid": 2147480000,  # implausible; os.kill is mocked anyway
            "master_fd": fake_fd,
            "transcript_fh": None,
        }

    # Two callers, same session: explicit close, then read-thread auto-terminate.
    app.terminate_session(sid, 2147480000, fake_fd)
    app.terminate_session(sid, 2147480000, fake_fd)

    assert closed.count(fake_fd) == 1, (
        f"master_fd was closed {closed.count(fake_fd)}x — a double close can land "
        f"on a reused fd and corrupt unrelated I/O (EBADF)"
    )
    assert sid not in app.sessions


def test_terminate_session_missing_session_is_noop(monkeypatch):
    """Terminating an unknown/already-removed session must not close any fd."""
    import app

    closed = []
    monkeypatch.setattr(app.os, "close", lambda fd: closed.append(fd))
    monkeypatch.setattr(app.os, "kill", lambda *a, **k: None)
    monkeypatch.setattr(app.time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(app, "_emit_from_thread", lambda *a, **k: None)

    app.terminate_session("sess-does-not-exist", 2147480000, 999778)
    assert closed == []
