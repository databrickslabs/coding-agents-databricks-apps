"""Tests for mcp_server — v2 background execution + inbox API."""

import asyncio
import json
import os
from unittest import mock

import pytest
from coda_mcp import mcp_server, task_manager, url_builder


# ── helpers ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_hooks():
    """Clear app hooks before/after each test."""
    from coda_mcp import mcp_server

    mcp_server._app_create_session = None
    mcp_server._app_send_input = None
    mcp_server._app_close_session = None
    yield
    mcp_server._app_create_session = None
    mcp_server._app_send_input = None
    mcp_server._app_close_session = None


@pytest.fixture(autouse=True)
def _isolated_sessions(tmp_path):
    """Point task_manager.SESSIONS_DIR at a temp dir."""
    sessions_dir = str(tmp_path / ".coda" / "sessions")
    with mock.patch("coda_mcp.task_manager.SESSIONS_DIR", sessions_dir):
        yield sessions_dir


def _parse(result: str) -> dict:
    """Parse JSON string returned by MCP tools."""
    return json.loads(result)


# ── Tool registration ────────────────────────────────────────────────


class TestToolRegistration:
    def test_tools_registered(self):
        from coda_mcp import mcp_server

        tool_mgr = mcp_server.mcp._tool_manager
        tool_names = set(tool_mgr._tools.keys())
        expected = {"coda_run", "coda_inbox", "coda_get_result", "coda_interactive"}
        assert expected == tool_names, f"Expected {expected}, got {tool_names}"

    def test_tool_count(self):
        from coda_mcp import mcp_server

        tool_mgr = mcp_server.mcp._tool_manager
        assert len(tool_mgr._tools) == 4


# ── coda_run ─────────────────────────────────────────────────────────


class TestCodaRun:
    @pytest.mark.asyncio
    async def test_creates_task_disk_only(self):
        """Without app hooks, creates session+task on disk, returns immediately."""
        from coda_mcp import mcp_server

        result = await mcp_server.coda_run(
            prompt="fix the bug",
            email="a@b.com",
        )
        data = _parse(result)
        assert data["status"] == "running"
        assert data["task_id"].startswith("task-")
        assert data["session_id"].startswith("sess-")

    @pytest.mark.asyncio
    async def test_auto_creates_session(self):
        """coda_run auto-creates a session — no separate create_session needed."""
        from coda_mcp import mcp_server
        from coda_mcp import task_manager

        result = await mcp_server.coda_run(
            prompt="build pipeline",
            email="a@b.com",
        )
        data = _parse(result)
        session = task_manager._read_session(data["session_id"])
        assert session["email"] == "a@b.com"
        assert session["status"] == "busy"  # task is running

    @pytest.mark.asyncio
    async def test_sends_to_pty_when_hooks_set(self):
        """With hooks, creates PTY and sends hermes command."""
        from coda_mcp import mcp_server

        mock_create = mock.Mock(return_value="pty-xyz")
        mock_send = mock.Mock()
        mcp_server.set_app_hooks(
            create_session_fn=mock_create,
            send_input_fn=mock_send,
            close_session_fn=mock.Mock(),
        )

        with mock.patch("coda_mcp.mcp_server.threading"):
            result = await mcp_server.coda_run(
                prompt="fix the bug",
                email="a@b.com",
            )

        data = _parse(result)
        assert data["status"] == "running"
        mock_create.assert_called_once()
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["label"] == "hermes-mcp"
        assert "transcript_path" in call_kwargs
        mock_send.assert_called_once()
        assert "hermes" in mock_send.call_args[0][1]

    @pytest.mark.asyncio
    async def test_yolo_permission(self):
        """permissions='yolo' produces --yolo flag in PTY command."""
        from coda_mcp import mcp_server

        mock_send = mock.Mock()
        mcp_server.set_app_hooks(
            create_session_fn=mock.Mock(return_value="pty-1"),
            send_input_fn=mock_send,
            close_session_fn=mock.Mock(),
        )

        with mock.patch("coda_mcp.mcp_server.threading"):
            await mcp_server.coda_run(
                prompt="go fast",
                email="a@b.com",
                permissions="yolo",
            )

        cmd = mock_send.call_args[0][1]
        assert "--yolo" in cmd

    @pytest.mark.asyncio
    async def test_previous_session_id_in_prompt(self):
        """previous_session_id appears in the wrapped prompt."""
        from coda_mcp import mcp_server
        from coda_mcp import task_manager

        # Create a "prior" session with a completed task
        prior = task_manager.create_session("a@b.com", "u1")
        prior_sid = prior["session_id"]

        result = await mcp_server.coda_run(
            prompt="add tests",
            email="a@b.com",
            previous_session_id=prior_sid,
        )
        data = _parse(result)

        # Read the prompt.txt and verify prior session reference
        tdir = task_manager._task_dir(data["session_id"], data["task_id"])
        with open(os.path.join(tdir, "prompt.txt")) as f:
            prompt_text = f.read()

        assert f"PRIOR SESSION: {prior_sid}" in prompt_text

    @pytest.mark.asyncio
    async def test_meta_json_written(self):
        """coda_run writes meta.json with task metadata."""
        from coda_mcp import mcp_server
        from coda_mcp import task_manager

        result = await mcp_server.coda_run(
            prompt="build a dashboard for sales",
            email="alice@test.com",
            previous_session_id="sess-old",
        )
        data = _parse(result)

        meta_path = os.path.join(
            task_manager._task_dir(data["session_id"], data["task_id"]),
            "meta.json",
        )
        with open(meta_path) as f:
            meta = json.load(f)

        assert meta["email"] == "alice@test.com"
        assert meta["previous_session_id"] == "sess-old"
        assert meta["prompt_summary"] == "build a dashboard for sales"
        assert "created_at" in meta

    @pytest.mark.asyncio
    async def test_concurrency_limit(self):
        """Exceeding MAX_CONCURRENT_TASKS returns an error."""
        from coda_mcp import mcp_server

        with mock.patch("coda_mcp.task_manager.MAX_CONCURRENT_TASKS", 1):
            # First task succeeds
            r1 = await mcp_server.coda_run(prompt="task1", email="a@b.com")
            assert _parse(r1)["status"] == "running"

            # Second task should fail (1 already running)
            r2 = await mcp_server.coda_run(prompt="task2", email="a@b.com")
            d2 = _parse(r2)
            assert d2["status"] == "error"
            assert "concurrency" in d2["error"].lower()


# ── coda_inbox ───────────────────────────────────────────────────────


class TestCodaInbox:
    @pytest.mark.asyncio
    async def test_empty_inbox(self):
        """No tasks → empty inbox."""
        from coda_mcp import mcp_server

        result = await mcp_server.coda_inbox()
        data = _parse(result)
        assert data["tasks"] == []
        assert data["counts"] == {"running": 0, "completed": 0, "failed": 0, "info_needed": 0, "needs_approval": 0}

    @pytest.mark.asyncio
    async def test_running_task_in_inbox(self):
        """A running task shows up in the inbox."""
        from coda_mcp import mcp_server

        await mcp_server.coda_run(prompt="build pipeline", email="a@b.com")

        result = await mcp_server.coda_inbox()
        data = _parse(result)
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["status"] == "running"
        assert data["tasks"][0]["prompt_summary"] == "build pipeline"
        assert data["counts"]["running"] == 1

    @pytest.mark.asyncio
    async def test_completed_task_in_inbox(self):
        """A completed task shows summary in inbox."""
        from coda_mcp import mcp_server
        from coda_mcp import task_manager

        r = await mcp_server.coda_run(prompt="fix bug", email="a@b.com")
        d = _parse(r)

        # Simulate agent writing result.json
        tdir = task_manager._task_dir(d["session_id"], d["task_id"])
        result_path = os.path.join(tdir, "result.json")
        with open(result_path, "w") as f:
            json.dump({
                "status": "completed",
                "summary": "Fixed the login bug",
                "files_changed": ["auth.py"],
                "artifacts": [],
                "errors": [],
            }, f)

        result = await mcp_server.coda_inbox()
        data = _parse(result)
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["status"] == "completed"
        assert data["tasks"][0]["summary"] == "Fixed the login bug"

    @pytest.mark.asyncio
    async def test_status_filter(self):
        """Filtering inbox by status works."""
        from coda_mcp import mcp_server
        from coda_mcp import task_manager

        # Create two tasks — one running, one completed
        r1 = await mcp_server.coda_run(prompt="task1", email="a@b.com")
        d1 = _parse(r1)

        r2 = await mcp_server.coda_run(prompt="task2", email="a@b.com")
        d2 = _parse(r2)

        # Complete task2
        tdir = task_manager._task_dir(d2["session_id"], d2["task_id"])
        with open(os.path.join(tdir, "result.json"), "w") as f:
            json.dump({"status": "completed", "summary": "done"}, f)

        # Filter running only
        result = await mcp_server.coda_inbox(status="running")
        data = _parse(result)
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["task_id"] == d1["task_id"]

    @pytest.mark.asyncio
    async def test_multiple_tasks_sorted_recent_first(self):
        """Inbox returns tasks sorted most recent first."""
        from coda_mcp import mcp_server

        await mcp_server.coda_run(prompt="first", email="a@b.com")
        await mcp_server.coda_run(prompt="second", email="a@b.com")

        result = await mcp_server.coda_inbox()
        data = _parse(result)
        assert len(data["tasks"]) == 2
        # Most recent first
        assert data["tasks"][0]["prompt_summary"] == "second"
        assert data["tasks"][1]["prompt_summary"] == "first"


# ── coda_get_result ──────────────────────────────────────────────────


class TestCodaGetResult:
    @pytest.mark.asyncio
    async def test_returns_result(self):
        from coda_mcp import mcp_server
        from coda_mcp import task_manager

        r = await mcp_server.coda_run(prompt="go", email="a@b.com")
        d = _parse(r)

        # Simulate agent writing result.json
        tdir = task_manager._task_dir(d["session_id"], d["task_id"])
        with open(os.path.join(tdir, "result.json"), "w") as f:
            json.dump({
                "summary": "Fixed the bug",
                "files_changed": ["app.py"],
                "artifacts": [],
                "errors": [],
            }, f)

        result = await mcp_server.coda_get_result(
            task_id=d["task_id"], session_id=d["session_id"]
        )
        data = _parse(result)
        assert data["task_id"] == d["task_id"]
        assert data["session_id"] == d["session_id"]
        assert data["summary"] == "Fixed the bug"

    @pytest.mark.asyncio
    async def test_no_result_yet(self):
        from coda_mcp import mcp_server

        r = await mcp_server.coda_run(prompt="go", email="a@b.com")
        d = _parse(r)

        result = await mcp_server.coda_get_result(
            task_id=d["task_id"], session_id=d["session_id"]
        )
        data = _parse(result)
        assert data["status"] == "running"
        assert "not yet available" in data["message"]


# ── viewer_url + transcript_path wiring ─────────────────────────────


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if not asyncio.iscoroutine(coro) else asyncio.run(coro)


def test_coda_run_includes_viewer_url_when_builder_returns_one(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path))
    monkeypatch.setattr(url_builder, "_app_url_cache", "app.example.com")

    create = mock.MagicMock(return_value="pty-abc")
    send = mock.MagicMock()
    closer = mock.MagicMock()
    mcp_server.set_app_hooks(create, send, closer)

    result_json = asyncio.run(mcp_server.coda_run(prompt="do it", email="u@x"))
    result = json.loads(result_json)
    assert result["status"] == "running"
    assert "?session=pty-abc" in result["viewer_url"]
    assert result["viewer_url"].startswith("https://app.example.com")


def test_coda_run_omits_viewer_url_when_builder_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path))
    monkeypatch.setattr(url_builder, "_app_url_cache", None)
    monkeypatch.delenv("CODA_APP_URL", raising=False)

    create = mock.MagicMock(return_value="pty-abc")
    mcp_server.set_app_hooks(create, mock.MagicMock(), mock.MagicMock())

    result_json = asyncio.run(mcp_server.coda_run(prompt="do it", email="u@x"))
    result = json.loads(result_json)
    # viewer_url present but None when builder returns None
    assert result.get("viewer_url") is None


def test_coda_run_passes_transcript_path_to_create_session(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path))
    create = mock.MagicMock(return_value="pty-abc")
    mcp_server.set_app_hooks(create, mock.MagicMock(), mock.MagicMock())

    asyncio.run(mcp_server.coda_run(prompt="do it", email="u@x"))
    # create_session was called with transcript_path=... pointing into ~/.coda/sessions/<sess>/tasks/<task>/transcript.log
    kwargs = create.call_args.kwargs
    assert "transcript_path" in kwargs
    assert kwargs["transcript_path"].endswith("transcript.log")
    assert "tasks" in kwargs["transcript_path"]


def test_coda_inbox_decorates_each_task_with_viewer_url(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path))
    monkeypatch.setattr(url_builder, "_app_url_cache", "app.example.com")

    # Seed one session with one task and a pty_session_id
    s = task_manager.create_session("u@x", "uid", label="t")
    sid = s["session_id"]
    task_manager._update_session_field(sid, "pty_session_id", "pty-xyz")
    task_manager.create_task(sid, "prompt", "u@x")

    result_json = asyncio.run(mcp_server.coda_inbox())
    result = json.loads(result_json)
    assert len(result["tasks"]) == 1
    assert "viewer_url" in result["tasks"][0]
    assert "?session=pty-xyz" in result["tasks"][0]["viewer_url"]


def test_coda_get_result_includes_viewer_url(tmp_path, monkeypatch):
    monkeypatch.setattr(task_manager, "SESSIONS_DIR", str(tmp_path))
    monkeypatch.setattr(url_builder, "_app_url_cache", "app.example.com")

    s = task_manager.create_session("u@x", "uid", label="t")
    sid = s["session_id"]
    task_manager._update_session_field(sid, "pty_session_id", "pty-xyz")
    t = task_manager.create_task(sid, "prompt", "u@x")
    tid = t["task_id"]
    tdir = task_manager._task_dir(sid, tid)
    task_manager._write_json(tdir + "/result.json", {
        "status": "completed", "summary": "ok",
    })

    result_json = asyncio.run(mcp_server.coda_get_result(tid, sid))
    result = json.loads(result_json)
    assert "viewer_url" in result
    assert "?session=pty-xyz" in result["viewer_url"]


class TestInteractiveHelpers:
    def test_safe_dirname_basename(self):
        from coda_mcp.mcp_server import _safe_dirname
        assert _safe_dirname("/Users/x@y.com/WAM") == "WAM"
        assert _safe_dirname("/Users/x@y.com/WAM/") == "WAM"

    def test_safe_dirname_sanitizes(self):
        from coda_mcp.mcp_server import _safe_dirname
        assert _safe_dirname("/Users/x/My Project!") == "My_Project_"

    def test_safe_dirname_empty_fallback(self):
        from coda_mcp.mcp_server import _safe_dirname
        assert _safe_dirname("/") == "workspace"
        assert _safe_dirname("") == "workspace"

    def test_safe_dirname_rejects_traversal(self):
        from coda_mcp.mcp_server import _safe_dirname
        assert _safe_dirname("/foo/..") == "workspace"
        assert _safe_dirname("/foo/.") == "workspace"

    def test_normalize_strips_workspace_prefix(self):
        from coda_mcp.mcp_server import _normalize_workspace_path
        assert _normalize_workspace_path("/Workspace/Users/x/WAM") == "/Users/x/WAM"

    def test_normalize_leaves_plain_path(self):
        from coda_mcp.mcp_server import _normalize_workspace_path
        assert _normalize_workspace_path("/Users/x/WAM") == "/Users/x/WAM"
        assert _normalize_workspace_path("/Users/x/WAM/") == "/Users/x/WAM"

    @pytest.mark.asyncio
    async def test_wait_for_agent_ready_delegates(self, monkeypatch):
        """_wait_for_agent_ready calls _wait_for_output_stable with prompt-seed constants."""
        from coda_mcp import mcp_server
        seen = {}

        async def fake_stable(pty, max_wait, stability):
            seen["args"] = (pty, max_wait, stability)

        monkeypatch.setattr(mcp_server, "_wait_for_output_stable", fake_stable)
        await mcp_server._wait_for_agent_ready("pty-1")
        assert seen["args"] == (
            "pty-1",
            mcp_server._PROMPT_SEED_MAX_WAIT_S,
            mcp_server._PROMPT_SEED_STABILITY_S,
        )
