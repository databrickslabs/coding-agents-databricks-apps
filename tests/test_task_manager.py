"""Tests for task_manager — disk-based MCP session/task state."""

import json
import os
import time
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def isolated_sessions(tmp_path):
    """Point task_manager.SESSIONS_DIR at a temp dir."""
    sessions_dir = str(tmp_path / ".coda" / "sessions")
    with mock.patch("coda_mcp.task_manager.SESSIONS_DIR", sessions_dir):
        yield sessions_dir


# ── helpers ──────────────────────────────────────────────────────────


def _read_json(path):
    with open(path) as f:
        return json.load(f)


def _read_text(path):
    with open(path) as f:
        return f.read()


def _read_jsonl(path):
    lines = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                lines.append(json.loads(line))
    return lines


# ── Session lifecycle ────────────────────────────────────────────────


class TestCreateSession:
    def test_returns_session_id_and_status(self):
        from coda_mcp import task_manager

        result = task_manager.create_session("a@b.com", "u1", "my-label")
        assert result["status"] == "ready"
        assert result["session_id"].startswith("sess-")
        assert len(result["session_id"]) == 5 + 12  # "sess-" + 12 hex

    def test_creates_session_json_on_disk(self, isolated_sessions):
        from coda_mcp import task_manager

        result = task_manager.create_session("a@b.com", "u1", "my-label")
        sid = result["session_id"]
        path = os.path.join(isolated_sessions, sid, "session.json")
        assert os.path.isfile(path)
        data = _read_json(path)
        assert data["email"] == "a@b.com"
        assert data["user_id"] == "u1"
        assert data["label"] == "my-label"
        assert data["status"] == "ready"
        assert data["current_task"] is None
        assert data["completed_tasks"] == []
        assert "created_at" in data

    def test_unique_ids(self):
        from coda_mcp import task_manager

        ids = {task_manager.create_session("a@b.com", "u1")["session_id"] for _ in range(20)}
        assert len(ids) == 20


class TestCloseSession:
    def test_marks_session_closed(self, isolated_sessions):
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1")["session_id"]
        task_manager.close_session(sid)
        data = _read_json(os.path.join(isolated_sessions, sid, "session.json"))
        assert data["status"] == "closed"

    def test_close_nonexistent_raises(self):
        from coda_mcp import task_manager

        with pytest.raises(task_manager.SessionNotFoundError):
            task_manager.close_session("sess-doesnotexist")


class TestReadSession:
    def test_read_existing(self, isolated_sessions):
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1", "lbl")["session_id"]
        data = task_manager._read_session(sid)
        assert data["email"] == "a@b.com"

    def test_read_nonexistent_raises(self):
        from coda_mcp import task_manager

        with pytest.raises(task_manager.SessionNotFoundError):
            task_manager._read_session("sess-000000000000")


class TestUpdateSessionField:
    def test_updates_single_field(self, isolated_sessions):
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1")["session_id"]
        task_manager._update_session_field(sid, "status", "busy")
        data = task_manager._read_session(sid)
        assert data["status"] == "busy"

    def test_preserves_other_fields(self, isolated_sessions):
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1", "lbl")["session_id"]
        task_manager._update_session_field(sid, "status", "busy")
        data = task_manager._read_session(sid)
        assert data["email"] == "a@b.com"
        assert data["label"] == "lbl"


# ── Task lifecycle ───────────────────────────────────────────────────


class TestCreateTask:
    def test_returns_task_id_and_running(self):
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1")["session_id"]
        result = task_manager.create_task(sid, "do something", "a@b.com")
        assert result["status"] == "running"
        assert result["task_id"].startswith("task-")
        assert len(result["task_id"]) == 5 + 8  # "task-" + 8 hex

    def test_creates_task_directory_with_files(self, isolated_sessions):
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1")["session_id"]
        tid = task_manager.create_task(sid, "do something", "a@b.com")["task_id"]
        task_dir = task_manager._task_dir(sid, tid)
        assert os.path.isdir(task_dir)
        assert os.path.isfile(os.path.join(task_dir, "prompt.txt"))
        assert os.path.isfile(os.path.join(task_dir, "status.jsonl"))

    def test_prompt_txt_contains_wrapped_prompt(self, isolated_sessions):
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1")["session_id"]
        tid = task_manager.create_task(sid, "fix the bug", "a@b.com")["task_id"]
        prompt = _read_text(os.path.join(task_manager._task_dir(sid, tid), "prompt.txt"))
        assert "---CODA-TASK---" in prompt
        assert "fix the bug" in prompt

    def test_session_marked_busy(self, isolated_sessions):
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1")["session_id"]
        task_manager.create_task(sid, "do it", "a@b.com")
        data = task_manager._read_session(sid)
        assert data["status"] == "busy"

    def test_session_current_task_set(self, isolated_sessions):
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1")["session_id"]
        tid = task_manager.create_task(sid, "do it", "a@b.com")["task_id"]
        data = task_manager._read_session(sid)
        assert data["current_task"] == tid

    def test_busy_session_raises(self):
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1")["session_id"]
        task_manager.create_task(sid, "first", "a@b.com")
        with pytest.raises(task_manager.SessionBusyError):
            task_manager.create_task(sid, "second", "a@b.com")

    def test_nonexistent_session_raises(self):
        from coda_mcp import task_manager

        with pytest.raises(task_manager.SessionNotFoundError):
            task_manager.create_task("sess-doesnotexist", "p", "e@x.com")

    def test_status_jsonl_has_initial_entry(self, isolated_sessions):
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1")["session_id"]
        tid = task_manager.create_task(sid, "go", "a@b.com")["task_id"]
        entries = _read_jsonl(
            os.path.join(task_manager._task_dir(sid, tid), "status.jsonl")
        )
        assert len(entries) == 1
        assert entries[0]["status"] == "running"

    def test_optional_params_stored(self, isolated_sessions):
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1")["session_id"]
        tid = task_manager.create_task(
            sid, "go", "a@b.com",
            context={"repo": "myrepo"},
            context_hint="look at utils.py",
            timeout_s=120,
            permissions=["read", "write"],
        )["task_id"]
        prompt = _read_text(os.path.join(task_manager._task_dir(sid, tid), "prompt.txt"))
        assert "myrepo" in prompt
        assert "utils.py" in prompt


class TestTaskDir:
    def test_returns_correct_path(self, isolated_sessions):
        from coda_mcp import task_manager

        path = task_manager._task_dir("sess-aabbccddee01", "task-11223344")
        expected = os.path.join(
            isolated_sessions, "sess-aabbccddee01", "tasks", "task-11223344"
        )
        assert path == expected


# ── Task status / result ─────────────────────────────────────────────


class TestGetTaskStatus:
    def test_returns_latest_status(self, isolated_sessions):
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1")["session_id"]
        tid = task_manager.create_task(sid, "go", "a@b.com")["task_id"]
        status = task_manager.get_task_status(tid, sid)
        assert status["status"] == "running"

    def test_reads_appended_lines(self, isolated_sessions):
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1")["session_id"]
        tid = task_manager.create_task(sid, "go", "a@b.com")["task_id"]
        # simulate agent appending progress
        status_path = os.path.join(task_manager._task_dir(sid, tid), "status.jsonl")
        with open(status_path, "a") as f:
            f.write(json.dumps({"status": "progress", "pct": 50, "ts": time.time()}) + "\n")
        status = task_manager.get_task_status(tid, sid)
        assert status["status"] == "progress"
        assert status["pct"] == 50

    def test_missing_task_returns_not_found(self, isolated_sessions):
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1")["session_id"]
        status = task_manager.get_task_status("task-nonexist", sid)
        assert status["status"] == "not_found"


class TestGetTaskResult:
    def test_returns_result_when_present(self, isolated_sessions):
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1")["session_id"]
        tid = task_manager.create_task(sid, "go", "a@b.com")["task_id"]
        # simulate agent writing result
        result_path = os.path.join(task_manager._task_dir(sid, tid), "result.json")
        with open(result_path, "w") as f:
            json.dump({"answer": 42}, f)
        result = task_manager.get_task_result(tid, sid)
        assert result["answer"] == 42

    def test_returns_none_when_absent(self, isolated_sessions):
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1")["session_id"]
        tid = task_manager.create_task(sid, "go", "a@b.com")["task_id"]
        result = task_manager.get_task_result(tid, sid)
        assert result is None

    def test_missing_task_returns_none(self, isolated_sessions):
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1")["session_id"]
        result = task_manager.get_task_result("task-nonexist", sid)
        assert result is None


# ── Complete task ─────────────────────────────────────────────────────


class TestCompleteTask:
    def test_marks_session_closed(self, isolated_sessions):
        """v2: sessions are ephemeral — complete_task auto-closes the session."""
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1")["session_id"]
        tid = task_manager.create_task(sid, "go", "a@b.com")["task_id"]
        task_manager.complete_task(sid, tid)
        data = task_manager._read_session(sid)
        assert data["status"] == "closed"
        assert "closed_at" in data

    def test_appends_to_completed_tasks(self, isolated_sessions):
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1")["session_id"]
        tid = task_manager.create_task(sid, "go", "a@b.com")["task_id"]
        task_manager.complete_task(sid, tid)
        data = task_manager._read_session(sid)
        assert tid in data["completed_tasks"]

    def test_closed_session_rejects_new_task(self, isolated_sessions):
        """v2: ephemeral sessions — new tasks need new sessions."""
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1")["session_id"]
        tid1 = task_manager.create_task(sid, "first", "a@b.com")["task_id"]
        task_manager.complete_task(sid, tid1)
        with pytest.raises(task_manager.SessionNotFoundError):
            task_manager.create_task(sid, "second", "a@b.com")

    def test_appends_done_to_status_jsonl(self, isolated_sessions):
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1")["session_id"]
        tid = task_manager.create_task(sid, "go", "a@b.com")["task_id"]
        task_manager.complete_task(sid, tid)
        entries = _read_jsonl(
            os.path.join(task_manager._task_dir(sid, tid), "status.jsonl")
        )
        assert entries[-1]["status"] == "done"

    def test_nonexistent_session_raises(self):
        from coda_mcp import task_manager

        with pytest.raises(task_manager.SessionNotFoundError):
            task_manager.complete_task("sess-doesnotexist", "task-00000000")


# ── Prompt wrapping ──────────────────────────────────────────────────


class TestWrapPrompt:
    def test_contains_marker(self):
        from coda_mcp import task_manager

        wrapped = task_manager.wrap_prompt(
            task_id="task-aabbccdd",
            session_id="sess-112233445566",
            email="a@b.com",
            prompt="fix the bug",
            context=None,
            results_dir="/tmp/r",
            context_hint=None,
        )
        assert "---CODA-TASK---" in wrapped
        assert "fix the bug" in wrapped
        assert "task-aabbccdd" in wrapped
        assert "sess-112233445566" in wrapped
        assert "a@b.com" in wrapped
        assert "/tmp/r" in wrapped

    def test_includes_context_when_provided(self):
        from coda_mcp import task_manager

        wrapped = task_manager.wrap_prompt(
            task_id="task-aabbccdd",
            session_id="sess-112233445566",
            email="a@b.com",
            prompt="go",
            context={"repo": "myrepo", "branch": "main"},
            results_dir="/tmp/r",
            context_hint=None,
        )
        assert "myrepo" in wrapped
        assert "main" in wrapped

    def test_includes_context_hint(self):
        from coda_mcp import task_manager

        wrapped = task_manager.wrap_prompt(
            task_id="task-aabbccdd",
            session_id="sess-112233445566",
            email="a@b.com",
            prompt="go",
            context=None,
            results_dir="/tmp/r",
            context_hint="look at utils.py first",
        )
        assert "look at utils.py first" in wrapped

    def test_no_context_still_valid(self):
        from coda_mcp import task_manager

        wrapped = task_manager.wrap_prompt(
            task_id="task-aabbccdd",
            session_id="sess-112233445566",
            email="a@b.com",
            prompt="hello",
            context=None,
            results_dir="/tmp/r",
            context_hint=None,
        )
        assert "---CODA-TASK---" in wrapped
        assert "hello" in wrapped


# ── Edge cases ────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_closed_session_rejects_task(self, isolated_sessions):
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1")["session_id"]
        task_manager.close_session(sid)
        with pytest.raises(task_manager.SessionNotFoundError):
            task_manager.create_task(sid, "go", "a@b.com")

    def test_multiple_tasks_across_sessions(self, isolated_sessions):
        """v2: each task gets its own ephemeral session; all appear in list_all_tasks."""
        from coda_mcp import task_manager

        tids = []
        for i in range(3):
            sid = task_manager.create_session("a@b.com", "u1")["session_id"]
            tid = task_manager.create_task(sid, f"task {i}", "a@b.com")["task_id"]
            task_manager.complete_task(sid, tid)
            tids.append(tid)
            # Each session auto-closes
            data = task_manager._read_session(sid)
            assert data["status"] == "closed"

        all_tasks = task_manager.list_all_tasks()
        all_tids = [t["task_id"] for t in all_tasks]
        for tid in tids:
            assert tid in all_tids

    def test_corrupt_session_json_raises(self, isolated_sessions):
        from coda_mcp import task_manager

        sid = task_manager.create_session("a@b.com", "u1")["session_id"]
        path = os.path.join(isolated_sessions, sid, "session.json")
        with open(path, "w") as f:
            f.write("{bad json")
        with pytest.raises(task_manager.SessionNotFoundError):
            task_manager._read_session(sid)


# ── find_task_dir_by_pty_session ─────────────────────────────────────


@pytest.fixture
def sessions_root(tmp_path, monkeypatch):
    from coda_mcp import task_manager
    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path))
    # Reset the lookup cache between tests
    task_manager._pty_lookup_cache.clear()
    return tmp_path


def _make_session_dir(root, sess_id, pty_id, current_task=None, completed=None):
    sdir = root / sess_id
    (sdir / "tasks").mkdir(parents=True)
    data = {
        "session_id": sess_id,
        "pty_session_id": pty_id,
        "current_task": current_task,
        "completed_tasks": completed or [],
        "status": "ready",
    }
    (sdir / "session.json").write_text(json.dumps(data))
    return sdir


def test_find_task_dir_hits_current_task(sessions_root):
    from coda_mcp import task_manager

    _make_session_dir(sessions_root, "sess-A", "pty-1", current_task="task-X")
    result = task_manager.find_task_dir_by_pty_session("pty-1")
    assert result == str(sessions_root / "sess-A" / "tasks" / "task-X")


def test_find_task_dir_falls_back_to_last_completed(sessions_root):
    from coda_mcp import task_manager

    _make_session_dir(
        sessions_root, "sess-A", "pty-1",
        current_task=None,
        completed=["task-old", "task-recent"],
    )
    result = task_manager.find_task_dir_by_pty_session("pty-1")
    assert result == str(sessions_root / "sess-A" / "tasks" / "task-recent")


def test_find_task_dir_returns_none_when_no_match(sessions_root):
    from coda_mcp import task_manager

    _make_session_dir(sessions_root, "sess-A", "pty-1", current_task="task-X")
    assert task_manager.find_task_dir_by_pty_session("pty-NONEXIST") is None


def test_find_task_dir_ignores_corrupt_session_json(sessions_root):
    from coda_mcp import task_manager

    sdir = sessions_root / "sess-bad"
    sdir.mkdir()
    (sdir / "session.json").write_text("not json {{{")
    _make_session_dir(sessions_root, "sess-good", "pty-1", current_task="task-X")
    assert task_manager.find_task_dir_by_pty_session("pty-1") == \
        str(sessions_root / "sess-good" / "tasks" / "task-X")


def test_find_task_dir_cache_hits_within_ttl(sessions_root):
    from coda_mcp import task_manager

    _make_session_dir(sessions_root, "sess-A", "pty-1", current_task="task-X")
    task_manager.find_task_dir_by_pty_session("pty-1")
    # Remove session.json — cache should still return the hit
    (sessions_root / "sess-A" / "session.json").unlink()
    assert task_manager.find_task_dir_by_pty_session("pty-1") == \
        str(sessions_root / "sess-A" / "tasks" / "task-X")


def test_find_task_dir_cache_expires(sessions_root, monkeypatch):
    from coda_mcp import task_manager

    monkeypatch.setattr(task_manager, "_PTY_LOOKUP_TTL", 0.01)
    _make_session_dir(sessions_root, "sess-A", "pty-1", current_task="task-X")
    task_manager.find_task_dir_by_pty_session("pty-1")
    (sessions_root / "sess-A" / "session.json").unlink()
    time.sleep(0.02)
    assert task_manager.find_task_dir_by_pty_session("pty-1") is None


def test_find_task_dir_no_sessions_dir(sessions_root, monkeypatch):
    from coda_mcp import task_manager

    monkeypatch.setattr(task_manager, "SESSIONS_DIR", "/nonexistent/path/that/does/not/exist")
    assert task_manager.find_task_dir_by_pty_session("pty-1") is None


# ── workflow_protocol flag wiring ────────────────────────────────────


def test_wrap_prompt_default_includes_capabilities_and_workflow():
    """Default workflow_protocol=True; rendered prompt contains both new sections."""
    from coda_mcp.task_manager import wrap_prompt

    out = wrap_prompt(
        task_id="t-1",
        session_id="s-1",
        email="user@example.com",
        prompt="do the thing",
        context=None,
        results_dir="/tmp/results",
    )
    assert "CAPABILITIES:" in out
    assert "WORKFLOW PROTOCOL:" in out
    # Sanity: still has the existing structure.
    assert "TASK:" in out
    assert "INSTRUCTIONS:" in out
    assert "SAFETY:" in out


def test_wrap_prompt_workflow_protocol_false_omits_sections():
    """With workflow_protocol=False, both new sections are absent."""
    from coda_mcp.task_manager import wrap_prompt

    out = wrap_prompt(
        task_id="t-1",
        session_id="s-1",
        email="user@example.com",
        prompt="do the thing",
        context=None,
        results_dir="/tmp/results",
        workflow_protocol=False,
    )
    assert "CAPABILITIES:" not in out
    assert "WORKFLOW PROTOCOL:" not in out
    # Existing sections are still present.
    assert "TASK:" in out
    assert "INSTRUCTIONS:" in out


def test_wrap_prompt_workflow_protocol_default_is_true():
    """Signature inspection: default value of workflow_protocol is True."""
    import inspect
    from coda_mcp.task_manager import wrap_prompt

    sig = inspect.signature(wrap_prompt)
    assert "workflow_protocol" in sig.parameters
    assert sig.parameters["workflow_protocol"].default is True


def test_create_task_signature_has_workflow_protocol_param():
    """create_task accepts workflow_protocol kwarg with default True."""
    import inspect
    from coda_mcp.task_manager import create_task

    sig = inspect.signature(create_task)
    assert "workflow_protocol" in sig.parameters
    assert sig.parameters["workflow_protocol"].default is True


def test_create_task_forwards_workflow_protocol_to_wrap_prompt(monkeypatch, tmp_path):
    """create_task must pass workflow_protocol through to wrap_prompt."""
    from coda_mcp import task_manager

    captured: dict = {}

    def fake_wrap_prompt(**kwargs):
        captured.update(kwargs)
        return "DUMMY PROMPT"

    monkeypatch.setattr(task_manager, "wrap_prompt", fake_wrap_prompt)
    monkeypatch.setattr(task_manager, "_session_dir", lambda sid: str(tmp_path))
    monkeypatch.setattr(task_manager, "_task_dir", lambda sid, tid: str(tmp_path))
    # Stub _read_session so create_task sees a valid ready session without disk I/O.
    monkeypatch.setattr(
        task_manager,
        "_read_session",
        lambda sid: {"session_id": sid, "status": "ready", "current_task": None, "completed_tasks": []},
    )
    # _write_json is the real helper used inside create_task (writes meta.json + session file).
    # Stub it out — we're testing flag pass-through, not filesystem behavior.
    monkeypatch.setattr(task_manager, "_write_json", lambda *a, **kw: None)
    monkeypatch.setattr(task_manager.os, "makedirs", lambda *a, **kw: None)
    # Stub the file-open for prompt.txt and status.jsonl writes.
    real_open = open
    def fake_open(path, mode="r", *args, **kwargs):
        if ("prompt.txt" in str(path) or "status.jsonl" in str(path)) and "w" in mode:
            import io
            return io.StringIO()
        return real_open(path, mode, *args, **kwargs)
    monkeypatch.setattr("builtins.open", fake_open)

    task_manager.create_task(
        session_id="s-1",
        prompt="x",
        email="u@example.com",
        workflow_protocol=False,
    )
    assert captured.get("workflow_protocol") is False


def test_coda_run_signature_has_workflow_protocol_param():
    """coda_run accepts workflow_protocol kwarg with default True."""
    import inspect
    from coda_mcp import mcp_server

    sig = inspect.signature(mcp_server.coda_run)
    assert "workflow_protocol" in sig.parameters
    assert sig.parameters["workflow_protocol"].default is True


def test_wrap_prompt_instructions_documents_info_needed():
    """INSTRUCTIONS section must mention the info_needed status and feedback field."""
    from coda_mcp.task_manager import wrap_prompt

    out = wrap_prompt(
        task_id="t-1",
        session_id="s-1",
        email="user@example.com",
        prompt="do the thing",
        context=None,
        results_dir="/tmp/r",
    )
    # Pull the INSTRUCTIONS section out for focused assertions.
    assert "info_needed" in out
    assert "feedback" in out


def test_wrap_prompt_instructions_lists_new_step_labels():
    """INSTRUCTIONS section enumerates the canonical step labels emitted by the agent."""
    from coda_mcp.task_manager import wrap_prompt

    out = wrap_prompt(
        task_id="t-1",
        session_id="s-1",
        email="user@example.com",
        prompt="do the thing",
        context=None,
        results_dir="/tmp/r",
    )
    for label in ("plan", "critique_plan", "execute", "critique_execute", "synthesize", "critique_synthesize"):
        assert label in out, f"Missing step label {label!r} from prompt text"
