"""Tests covering counts dict, coda_get_result docstring, and MCP instructions
all reflect the new info_needed / needs_approval terminal statuses."""
import asyncio
import json


def test_mcp_instructions_mention_info_needed():
    """Server-level MCP instructions teach calling LLMs about info_needed."""
    from coda_mcp import mcp_server

    txt = mcp_server.mcp.instructions
    assert "info_needed" in txt
    assert "needs_approval" in txt
    assert "feedback" in txt


def test_coda_get_result_docstring_mentions_info_needed():
    """coda_get_result docstring lists info_needed / needs_approval alongside completed/failed."""
    from coda_mcp import mcp_server

    doc = (mcp_server.coda_get_result.__doc__ or "").lower()
    assert "info_needed" in doc
    assert "needs_approval" in doc


def test_inbox_counts_dict_includes_new_statuses(monkeypatch):
    """coda_inbox counts dict has info_needed and needs_approval keys."""
    from coda_mcp import mcp_server

    fake_tasks = [
        {"task_id": "t1", "session_id": "s1", "status": "running"},
        {"task_id": "t2", "session_id": "s2", "status": "completed"},
        {"task_id": "t3", "session_id": "s3", "status": "failed"},
        {"task_id": "t4", "session_id": "s4", "status": "info_needed"},
        {"task_id": "t5", "session_id": "s5", "status": "needs_approval"},
        {"task_id": "t6", "session_id": "s6", "status": "info_needed"},
    ]

    monkeypatch.setattr(
        mcp_server.task_manager, "list_all_tasks",
        lambda email, status_filter=None: list(fake_tasks),
    )
    # _read_session_safe is called inside the loop; return None so no viewer_url is added.
    monkeypatch.setattr(
        mcp_server.task_manager, "_read_session_safe", lambda sid: None,
    )

    result_str = asyncio.run(mcp_server.coda_inbox(email="u@e"))
    result = json.loads(result_str)
    counts = result["counts"]

    assert counts["running"] == 1
    assert counts["completed"] == 1
    assert counts["failed"] == 1
    assert counts["info_needed"] == 2
    assert counts["needs_approval"] == 1
