"""Tests for coda_interactive — terminal-side workspace pull (no server-side export)."""
import asyncio
import inspect
import json
import os

import pytest

from coda_mcp import mcp_server

ALLOWED_AGENTS = {"claude", "hermes", "codex", "gemini", "opencode"}


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Wire PTY hooks with recording mocks; HOME -> tmp so project_dir is sandboxed.

    ``_wait_for_pull`` is mocked to return ``state["pull_outcome"]`` (default
    "ok"); tests override it to exercise the failure / timeout paths.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    inputs: list[str] = []
    state = {"pty_id": "pty-abc123", "pull_outcome": "ok", "closed": []}

    def fake_create(label, replay_only=False, **kw):
        return state["pty_id"]

    def fake_send(pty_id, text):
        inputs.append(text)

    def fake_close(pty_id):
        state["closed"].append(pty_id)

    async def fake_wait_pull(pty_id, target_dir):
        return state["pull_outcome"]

    async def fake_agent_ready(*a, **kw):
        return None

    monkeypatch.setattr(mcp_server, "_app_create_session", fake_create)
    monkeypatch.setattr(mcp_server, "_app_send_input", fake_send)
    monkeypatch.setattr(mcp_server, "_app_close_session", fake_close)
    monkeypatch.setattr(mcp_server, "_wait_for_pull", fake_wait_pull)
    monkeypatch.setattr(mcp_server, "_wait_for_agent_ready", fake_agent_ready)
    monkeypatch.setattr(
        mcp_server.url_builder, "build_viewer_url", lambda pid: f"https://viewer/{pid}"
    )
    return inputs, state


# ── new contract: terminal-side pull ─────────────────────────────────


@pytest.mark.asyncio
async def test_pull_command_is_sent_first(wired):
    inputs, _ = wired
    await mcp_server.coda_interactive(
        prompt="analyze", workspace_path="/Workspace/Users/x@y.com/WAM", agent="claude"
    )
    first = inputs[0]
    assert "databricks workspace export-dir" in first
    assert "/Users/x@y.com/WAM" in first          # /Workspace prefix stripped
    assert "/Workspace/Users" not in first
    assert "./WAM" in first
    assert "&& cd " in first                        # cd into the pulled dir
    assert "echo " in first                         # completion-marker tail present


@pytest.mark.asyncio
async def test_pull_marker_not_literal_in_command(wired):
    """CRITICAL: the contiguous marker tokens must NOT appear in the typed command.

    The shell echoes the command line back into the PTY output buffer. If the
    contiguous token were present in the command, the wait would match it from
    the echo and declare success before export-dir ran. The command builds the
    tokens from split string literals, so only their split form is typed.
    """
    inputs, _ = wired
    await mcp_server.coda_interactive(
        prompt="x", workspace_path="/Users/x/WAM", agent="claude"
    )
    pull = inputs[0]
    assert mcp_server._PULL_OK not in pull, f"contiguous OK token leaked into command: {pull!r}"
    assert mcp_server._PULL_FAIL not in pull, f"contiguous FAIL token leaked into command: {pull!r}"


@pytest.mark.asyncio
async def test_claude_launches_auto_mode_with_embedded_prompt(wired):
    """claude launches in ONE command: --enable-auto-mode + the prompt as an arg.

    No separate bare `claude` line and no separately-typed prompt — that avoids
    the per-directory folder-trust dialog and the TUI cold-start timing.
    """
    inputs, _ = wired
    await mcp_server.coda_interactive(
        prompt="go now", workspace_path="/Users/x/WAM", agent="claude"
    )
    assert len(inputs) == 2, f"expected pull + atomic launch only; got {inputs!r}"
    launch = inputs[1]
    assert launch.startswith("claude --enable-auto-mode ")
    assert "go now" in launch
    assert "/Users/x/WAM" in launch                          # context prefix embedded
    assert not any(t.strip() == "claude" for t in inputs)    # no bare claude launch


def test_claude_in_auto_launch_map():
    assert mcp_server._AGENT_AUTO_LAUNCH.get("claude") == "claude --enable-auto-mode"


@pytest.mark.asyncio
async def test_prompt_seeded_with_context_line(wired):
    inputs, _ = wired
    await mcp_server.coda_interactive(
        prompt="DO THE THING", workspace_path="/Users/x/WAM", agent="claude"
    )
    seeded = inputs[-1]
    assert "/Users/x/WAM" in seeded
    assert "DO THE THING" in seeded
    assert "Workspace" in seeded                                     # precondition (clean fail, not ValueError)
    assert seeded.index("Workspace") < seeded.index("DO THE THING")  # context precedes prompt


@pytest.mark.asyncio
async def test_pull_failure_returns_error_and_no_launch(wired):
    inputs, state = wired
    state["pull_outcome"] = "fail"
    out = json.loads(await mcp_server.coda_interactive(
        prompt="go", workspace_path="/Users/x/WAM", agent="claude"
    ))
    assert out["status"] == "error"
    assert "Failed to pull" in out["error"]
    assert state["closed"] == [state["pty_id"]]              # PTY closed
    assert not any(t.strip() == "claude" for t in inputs)    # agent NOT launched


@pytest.mark.asyncio
async def test_pull_timeout_returns_error(wired):
    inputs, state = wired
    state["pull_outcome"] = "timeout"
    out = json.loads(await mcp_server.coda_interactive(
        prompt="go", workspace_path="/Users/x/WAM", agent="claude"
    ))
    assert out["status"] == "error"
    assert "Timed out" in out["error"]
    assert state["closed"] == [state["pty_id"]]
    assert not any(t.strip() == "claude" for t in inputs)


@pytest.mark.asyncio
async def test_happy_path_returns_launched(wired):
    out = json.loads(await mcp_server.coda_interactive(
        prompt="go", workspace_path="/Users/x/WAM", agent="claude"
    ))
    assert out["status"] == "launched"
    assert out["viewer_url"] == "https://viewer/pty-abc123"
    assert out["project_dir"].endswith(os.path.join("pty-abc123", "WAM"))


@pytest.mark.asyncio
async def test_unknown_agent_rejected(wired):
    out = json.loads(await mcp_server.coda_interactive(
        prompt="x", workspace_path="/Users/x/WAM", agent="bogus"
    ))
    assert out["status"] == "error" and "Unknown agent" in out["error"]
    for allowed in ALLOWED_AGENTS:
        assert allowed in out["error"]


@pytest.mark.asyncio
async def test_pty_hook_not_wired(monkeypatch):
    monkeypatch.setattr(mcp_server, "_app_create_session", None)
    monkeypatch.setattr(mcp_server, "_app_send_input", None)
    out = json.loads(await mcp_server.coda_interactive(
        prompt="x", workspace_path="/Users/x/WAM", agent="claude"
    ))
    assert out["status"] == "error" and "PTY hook" in out["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("agent,cmd", [
    ("hermes", "hermes chat"), ("codex", "codex"),
    ("gemini", "gemini"), ("opencode", "opencode"),
])
async def test_fallback_agents_launch_then_type_prompt(wired, agent, cmd):
    """Agents without an auto-launch entry launch bare, then the prompt is typed."""
    inputs, _ = wired
    await mcp_server.coda_interactive(
        prompt="go", workspace_path="/Users/x/WAM", agent=agent
    )
    assert any(t.strip() == cmd for t in inputs)             # bare launch present
    assert inputs[-1].strip().endswith("go")                 # prompt typed last
    assert "--enable-auto-mode" not in " ".join(inputs)      # not the claude path


def test_no_blocking_sleep_in_source():
    src = inspect.getsource(mcp_server.coda_interactive)
    assert "time.sleep(" not in src


def test_no_workspaceclient_in_module():
    """The export-era WorkspaceClient import/use is gone from the module."""
    src = inspect.getsource(mcp_server)
    assert "export_workspace_tree" not in src
    assert "workspace.get_status(" not in src


# ── _wait_for_pull behavior (real helper, fake sessions buffer) ───────


@pytest.mark.asyncio
async def test_wait_for_pull_ok_with_files(monkeypatch, tmp_path):
    from app import sessions
    sid = "pty-pull-ok"
    target = tmp_path / "WAM"
    target.mkdir()
    (target / "README.md").write_text("# hi")
    sessions[sid] = {"output_buffer": [b"Exporting...\n", (mcp_server._PULL_OK + "\n").encode()]}
    try:
        out = await mcp_server._wait_for_pull(sid, str(target))
        assert out == "ok"
    finally:
        sessions.pop(sid, None)


@pytest.mark.asyncio
async def test_wait_for_pull_ok_marker_but_no_files_is_fail(monkeypatch, tmp_path):
    from app import sessions
    sid = "pty-pull-okempty"
    target = tmp_path / "WAM"  # never created
    sessions[sid] = {"output_buffer": [(mcp_server._PULL_OK + "\n").encode()]}
    try:
        assert await mcp_server._wait_for_pull(sid, str(target)) == "fail"
    finally:
        sessions.pop(sid, None)


@pytest.mark.asyncio
async def test_wait_for_pull_fail_marker(monkeypatch, tmp_path):
    from app import sessions
    sid = "pty-pull-fail"
    sessions[sid] = {"output_buffer": [b"ERROR: nope\n", (mcp_server._PULL_FAIL + "\n").encode()]}
    try:
        assert await mcp_server._wait_for_pull(sid, str(tmp_path / "WAM")) == "fail"
    finally:
        sessions.pop(sid, None)


@pytest.mark.asyncio
async def test_wait_for_pull_split_echo_does_not_false_trigger(monkeypatch, tmp_path):
    """The split-literal command echo must NOT be read as the success marker."""
    from app import sessions
    sid = "pty-pull-splitecho"
    # This is what the shell echoes when the command line is typed — the SPLIT form.
    echoed_command = 'cd /x && databricks workspace export-dir /Users/x/WAM ./WAM && cd WAM && echo "CODA""_PULL_""OK" || echo "CODA""_PULL_""FAIL"\n'
    sessions[sid] = {"output_buffer": [echoed_command.encode()]}
    monkeypatch.setattr(mcp_server, "_PULL_MAX_WAIT_S", 0.5)  # keep the test fast
    try:
        # Only the split echo is present (no executed contiguous token) -> timeout.
        assert await mcp_server._wait_for_pull(sid, str(tmp_path)) == "timeout"
    finally:
        sessions.pop(sid, None)


# ── preserved signature / contract guards ────────────────────────────


def test_default_agent_is_claude():
    sig = inspect.signature(mcp_server.coda_interactive)
    assert sig.parameters["agent"].default == "claude"


def test_no_branch_parameter():
    sig = inspect.signature(mcp_server.coda_interactive)
    assert "branch" not in sig.parameters


def test_instructions_drop_stale_export_wording_and_keep_contract():
    """Server-level MCP instructions: no stale server-side export claim; contract intact."""
    txt = mcp_server.mcp.instructions
    lowered = txt.lower()
    assert "server-side snapshot" not in txt
    assert "export-dir" in txt
    assert "coda_interactive" in txt
    assert (
        "git folder or" in lowered
        or "plain workspace folder" in lowered
        or "plain folder" in lowered
    )
    # Local-agent contract: must tell a local caller to copy local files INTO the
    # Workspace first, with the concrete command, since the tool can't see local disk.
    assert "import-dir" in lowered, "instructions must give the `workspace import-dir` command"
    assert "local" in lowered, "instructions must address the local-agent case"


def test_docstring_tells_local_callers_to_import_dir():
    """coda_interactive's own docstring carries the local-upload guidance too."""
    doc = (mcp_server.coda_interactive.__doc__ or "").lower()
    assert "import-dir" in doc
    assert "cannot read your local filesystem" in doc


# ── preserved wait-helper behavior tests (now via the wrapper) ────────


def test_wait_for_agent_ready_returns_when_buffer_stabilizes(monkeypatch):
    """Wrapper returns once the output buffer has been stable for the window."""
    from app import sessions

    sid = "pty-stabilize-test"
    sessions[sid] = {"output_buffer": [b"banner line\n", b"prompt> "]}
    monkeypatch.setattr(mcp_server, "_PROMPT_SEED_STABILITY_S", 0.05)
    monkeypatch.setattr(mcp_server, "_PROMPT_SEED_MAX_WAIT_S", 2.0)
    try:
        async def _run():
            import time
            t0 = time.time()
            await mcp_server._wait_for_agent_ready(sid)
            return time.time() - t0
        elapsed = asyncio.run(_run())
        assert elapsed < 1.0, f"Helper took {elapsed:.2f}s — should return quickly when stable"
    finally:
        sessions.pop(sid, None)


def test_wait_for_agent_ready_times_out_when_buffer_empty(monkeypatch):
    """Wrapper returns at max-wait if the buffer never gets content."""
    from app import sessions

    sid = "pty-empty-test"
    sessions[sid] = {"output_buffer": []}
    monkeypatch.setattr(mcp_server, "_PROMPT_SEED_STABILITY_S", 0.05)
    monkeypatch.setattr(mcp_server, "_PROMPT_SEED_MAX_WAIT_S", 0.3)
    try:
        async def _run():
            import time
            t0 = time.time()
            await mcp_server._wait_for_agent_ready(sid)
            return time.time() - t0
        elapsed = asyncio.run(_run())
        assert 0.2 <= elapsed <= 0.8, f"Expected ~0.3s max-wait; got {elapsed:.2f}s"
    finally:
        sessions.pop(sid, None)


def test_wait_for_agent_ready_returns_when_session_gone(monkeypatch):
    """Wrapper returns immediately if the session is no longer present."""
    monkeypatch.setattr(mcp_server, "_PROMPT_SEED_STABILITY_S", 0.05)
    monkeypatch.setattr(mcp_server, "_PROMPT_SEED_MAX_WAIT_S", 5.0)

    async def _run():
        import time
        t0 = time.time()
        await mcp_server._wait_for_agent_ready("nonexistent-pty-id")
        return time.time() - t0
    elapsed = asyncio.run(_run())
    assert elapsed < 0.5, f"Helper took {elapsed:.2f}s — should return when session gone"
