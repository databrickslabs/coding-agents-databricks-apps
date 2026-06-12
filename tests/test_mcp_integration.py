"""End-to-end MCP integration tests — v2 background execution + inbox API.

Exercises the full flow: coda_run -> coda_inbox -> coda_get_result.
No real PTY — app hooks are mocked.
"""

import json
import os
import time
from unittest.mock import MagicMock

import pytest


# ── helpers ──────────────────────────────────────────────────────────


def _parse(result: str) -> dict:
    """Parse JSON string returned by MCP tools."""
    return json.loads(result)


# ── fixture ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_env(tmp_path):
    """Redirect state to tmp and mock PTY hooks."""
    from coda_mcp import task_manager as tm
    from coda_mcp import mcp_server as ms

    original_dir = tm.SESSIONS_DIR
    tm.SESSIONS_DIR = str(tmp_path / "sessions")

    mock_send = MagicMock()
    mock_close = MagicMock()
    ms.set_app_hooks(
        create_session_fn=lambda label, **kwargs: f"pty-mock-{label}",
        send_input_fn=mock_send,
        close_session_fn=mock_close,
    )

    yield {"tmp": tmp_path, "mock_send": mock_send, "mock_close": mock_close}

    tm.SESSIONS_DIR = original_dir
    ms.set_app_hooks(None, None, None)


# ── 1. Happy-path: fire-and-forget → inbox → result ─────────────────


class TestFullMcpFlow:
    @pytest.mark.asyncio
    async def test_full_background_flow(self, isolated_env):
        """Happy path: run (fire-and-forget) → inbox → result."""
        from coda_mcp import mcp_server as ms
        from coda_mcp import task_manager as tm

        # Step 1: submit task (returns immediately)
        with MagicMock() as mock_thread:
            from coda_mcp import mcp_server
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr("coda_mcp.mcp_server.threading", mock_thread)
                raw = await ms.coda_run(
                    prompt="create a sales pipeline",
                    email="alice@test.com",
                    context='{"tables": ["sales.transactions"]}',
                )

        task = _parse(raw)
        assert task["status"] == "running"
        task_id = task["task_id"]
        session_id = task["session_id"]
        assert task_id.startswith("task-")
        assert session_id.startswith("sess-")

        # Step 2: inbox shows running task
        raw = await ms.coda_inbox()
        inbox = _parse(raw)
        assert len(inbox["tasks"]) == 1
        assert inbox["tasks"][0]["task_id"] == task_id
        assert inbox["tasks"][0]["status"] == "running"
        assert inbox["counts"]["running"] == 1

        # Step 3: simulate agent writing result.json
        tdir = tm._task_dir(session_id, task_id)
        result_path = os.path.join(tdir, "result.json")
        with open(result_path, "w") as f:
            json.dump({
                "status": "completed",
                "summary": "Created sales pipeline with 3 stages",
                "files_changed": ["pipeline.py", "config.yaml"],
                "artifacts": ["/workspace/pipeline.py"],
                "errors": [],
            }, f)

        # Step 4: complete_task (simulating what _watch_task does)
        tm.complete_task(session_id, task_id)

        # Step 5: inbox shows completed
        raw = await ms.coda_inbox()
        inbox = _parse(raw)
        assert len(inbox["tasks"]) == 1
        assert inbox["tasks"][0]["status"] == "completed"
        assert inbox["tasks"][0]["summary"] == "Created sales pipeline with 3 stages"
        assert inbox["counts"]["completed"] == 1

        # Step 6: get full result
        raw = await ms.coda_get_result(task_id=task_id, session_id=session_id)
        result = _parse(raw)
        assert result["task_id"] == task_id
        assert result["summary"] == "Created sales pipeline with 3 stages"
        assert result["files_changed"] == ["pipeline.py", "config.yaml"]

        # Step 7: session was auto-closed
        session = tm._read_session(session_id)
        assert session["status"] == "closed"


# ── 2. Task chaining with previous_session_id ───────────────────────


class TestTaskChaining:
    @pytest.mark.asyncio
    async def test_chained_task_references_prior_session(self, isolated_env):
        """A chained task includes prior session context in prompt."""
        from coda_mcp import mcp_server as ms
        from coda_mcp import task_manager as tm

        # First task
        raw = await ms.coda_run(
            prompt="build pipeline",
            email="bob@test.com",
        )
        first = _parse(raw)
        first_sid = first["session_id"]
        first_tid = first["task_id"]

        # Complete first task
        tdir = tm._task_dir(first_sid, first_tid)
        with open(os.path.join(tdir, "result.json"), "w") as f:
            json.dump({
                "status": "completed",
                "summary": "Built pipeline.py",
                "files_changed": ["pipeline.py"],
            }, f)
        tm.complete_task(first_sid, first_tid)

        # Second task chained to first
        raw = await ms.coda_run(
            prompt="add tests for the pipeline",
            email="bob@test.com",
            previous_session_id=first_sid,
        )
        second = _parse(raw)
        second_sid = second["session_id"]
        second_tid = second["task_id"]

        # Verify prompt references prior session
        prompt_path = os.path.join(
            tm._task_dir(second_sid, second_tid), "prompt.txt"
        )
        with open(prompt_path) as f:
            prompt_text = f.read()
        assert f"PRIOR SESSION: {first_sid}" in prompt_text

        # Verify meta.json has previous_session_id
        meta_path = os.path.join(
            tm._task_dir(second_sid, second_tid), "meta.json"
        )
        with open(meta_path) as f:
            meta = json.load(f)
        assert meta["previous_session_id"] == first_sid

        # Verify inbox shows chaining
        raw = await ms.coda_inbox()
        inbox = _parse(raw)
        running_tasks = [t for t in inbox["tasks"] if t["status"] == "running"]
        assert len(running_tasks) == 1
        assert running_tasks[0]["previous_session_id"] == first_sid


# ── 3. Concurrency limit ────────────────────────────────────────────


class TestConcurrencyLimit:
    @pytest.mark.asyncio
    async def test_exceeding_limit_returns_error(self, isolated_env):
        """Exceeding MAX_CONCURRENT_TASKS returns a clear error."""
        from coda_mcp import mcp_server as ms
        from unittest.mock import patch

        with patch("coda_mcp.task_manager.MAX_CONCURRENT_TASKS", 1):
            r1 = await ms.coda_run(prompt="task1", email="a@b.com")
            assert _parse(r1)["status"] == "running"

            r2 = await ms.coda_run(prompt="task2", email="a@b.com")
            d2 = _parse(r2)
            assert d2["status"] == "error"
            assert "concurrency" in d2["error"].lower()


# ── 4. Yolo permissions → --yolo flag ───────────────────────────────


class TestYoloPermissions:
    @pytest.mark.asyncio
    async def test_yolo_permissions(self, isolated_env):
        """permissions='yolo' causes the PTY command to include --yolo."""
        from coda_mcp import mcp_server as ms

        mock_send = isolated_env["mock_send"]

        with MagicMock() as mock_thread:
            from coda_mcp import mcp_server
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr("coda_mcp.mcp_server.threading", mock_thread)
                await ms.coda_run(
                    prompt="deploy everything",
                    email="dave@test.com",
                    permissions="yolo",
                )

        mock_send.assert_called_once()
        cmd = mock_send.call_args[0][1]
        assert "--yolo" in cmd


# ── 5. Session auto-close on completion ──────────────────────────────


class TestAutoClose:
    @pytest.mark.asyncio
    async def test_session_auto_closes(self, isolated_env):
        """Session is auto-closed when task completes."""
        from coda_mcp import mcp_server as ms
        from coda_mcp import task_manager as tm

        raw = await ms.coda_run(prompt="quick job", email="a@b.com")
        d = _parse(raw)

        # Session should be busy
        session = tm._read_session(d["session_id"])
        assert session["status"] == "busy"

        # Complete the task
        tdir = tm._task_dir(d["session_id"], d["task_id"])
        with open(os.path.join(tdir, "result.json"), "w") as f:
            json.dump({"status": "completed", "summary": "done"}, f)
        tm.complete_task(d["session_id"], d["task_id"])

        # Session should now be closed
        session = tm._read_session(d["session_id"])
        assert session["status"] == "closed"
        assert "closed_at" in session


# ── 6. Cleanup expired tasks ────────────────────────────────────────


class TestCleanup:
    @pytest.mark.asyncio
    async def test_cleanup_removes_expired(self, isolated_env):
        """cleanup_expired_tasks removes old closed sessions."""
        from coda_mcp import mcp_server as ms
        from coda_mcp import task_manager as tm
        from unittest.mock import patch

        raw = await ms.coda_run(prompt="old task", email="a@b.com")
        d = _parse(raw)

        # Complete and close
        tdir = tm._task_dir(d["session_id"], d["task_id"])
        with open(os.path.join(tdir, "result.json"), "w") as f:
            json.dump({"status": "completed", "summary": "done"}, f)
        tm.complete_task(d["session_id"], d["task_id"])

        # Backdate closed_at to expire it
        session = tm._read_session(d["session_id"])
        session["closed_at"] = time.time() - 90000  # 25 hours ago
        tm._write_json(tm._session_file(d["session_id"]), session)

        # Cleanup should remove it
        removed = tm.cleanup_expired_tasks()
        assert removed == 1

        # Inbox should be empty now
        raw = await ms.coda_inbox()
        inbox = _parse(raw)
        assert len(inbox["tasks"]) == 0


