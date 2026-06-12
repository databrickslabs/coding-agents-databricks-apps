"""MCP server exposing CoDA session/task tools via FastMCP.

v2: Background execution + inbox pattern.
- ``coda_run`` — fire-and-forget task submission (auto-creates ephemeral session)
- ``coda_inbox`` — dashboard of all background tasks
- ``coda_get_result`` — pull full structured result for a completed task

Delegates all disk state to ``task_manager.py``.  PTY operations are
handled through app hooks (create/send/close) set via ``set_app_hooks()``.

Run standalone for testing::

    python mcp_server.py          # stdio transport
"""

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import threading
import time

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import TransportSecuritySettings
from mcp.types import ToolAnnotations

from coda_mcp import task_manager
from coda_mcp import url_builder

logger = logging.getLogger(__name__)

# ── FastMCP instance ────────────────────────────────────────────────

# Build allowed origins from DATABRICKS_HOST for Genie Code requests
_databricks_host = os.environ.get("DATABRICKS_HOST", "")
_allowed_origins = []
if _databricks_host:
    # Ensure https:// prefix, strip trailing slash
    origin = _databricks_host if _databricks_host.startswith("https://") else f"https://{_databricks_host}"
    _allowed_origins.append(origin.rstrip("/"))

mcp = FastMCP(
    "coda",
    instructions=(
        "CoDA MCP server — delegate coding tasks to AI agents on Databricks.\n\n"
        "CRITICAL — FIRE AND FORGET:\n"
        "coda_run submits work and returns IMMEDIATELY. The task runs autonomously "
        "in the background. After calling coda_run, DO NOT call coda_inbox or "
        "coda_get_result to check on it. Do NOT loop, poll, or wait. Simply tell "
        "the user the task was submitted and MOVE ON to their next request.\n\n"
        "WHEN TO CHECK INBOX:\n"
        "Call coda_inbox ONLY when the user explicitly asks about background tasks "
        "(e.g. 'how's my task going?', 'check on that', 'what's in my inbox'). "
        "Never call it proactively, automatically, or in a loop.\n\n"
        "WORKFLOW:\n"
        "1) coda_run — submit work, get back task_id. Tell user it's running. Stop.\n"
        "2) Continue chatting about other topics — the task runs independently.\n"
        "3) coda_inbox — ONLY when user asks. Shows all tasks from last 24h.\n"
        "4) coda_get_result — for completed tasks, get full structured output.\n\n"
        "CHAINING: pass previous_session_id from a completed task's session_id "
        "to give the new task context of what was done before.\n\n"
        "INFO_NEEDED HANDOFF: When coda_inbox shows a task with status='info_needed', "
        "the agent could not proceed because of missing context. Call coda_get_result "
        "to read the 'feedback' field — it tells you exactly what the agent needs (a "
        "table name, a decision, a clarification). Add that context to the prompt and "
        "resubmit via coda_run with previous_session_id set to the original task's "
        "session_id so the agent has the prior attempt's context. 'needs_approval' is "
        "similar but means the agent has a destructive plan and is waiting for the "
        "caller's explicit go/no-go.\n\n"
        "SHARE THE REPLAY URL: When coda_run returns a viewer_url field (non-null), "
        "mention it to the user in plain text (e.g. \"you can view the session replay "
        "at <url>\"). The URL is a read-only static replay showing the prompt, the "
        "agent's work, and the final output. It reflects the task's progress while "
        "running, then the full transcript once it completes — and remains valid "
        "indefinitely after that. It is safe to share: it points to the same "
        "Databricks App the user is already authenticated against. Do this on the "
        "first mention of the task and any time the user asks where the task is or "
        "how to see what it did.\n\n"
        "INTERACTIVE HANDOFF (coda_interactive): When the user wants a human to "
        "drive a coding agent in CoDA — not autonomous execution — call "
        "coda_interactive instead of coda_run. The tool reads files from a "
        "directory that already exists in the Databricks Workspace (a Git "
        "Folder or a plain Workspace folder — either works). IMPORTANT: this "
        "tool runs inside CoDA on Databricks and reads ONLY from the Databricks "
        "Workspace — it CANNOT see your local filesystem. If you are a LOCAL "
        "agent (e.g. Claude Code or Codex on the user's machine) and the project "
        "files for this task live locally, you MUST first copy them into the "
        "Workspace, THEN pass that Workspace path. Easiest: run `databricks "
        "workspace import-dir <local-project-dir> "
        "/Workspace/Users/<user-email>/<project-name>` (Databricks CLI; the SDK "
        "or REST work too), then call coda_interactive with workspace_path set to "
        "that /Workspace/Users/... path. The tool does NOT accept inline file "
        "payloads. If the directory is a Git "
        "Folder, ensure the desired branch is checked out first — "
        "the pull is a point-in-time snapshot. The tool copies the directory "
        "into a Coda-local working directory using your credentials (via "
        "`databricks workspace export-dir`), launches the chosen agent "
        "(claude default; also hermes, codex, gemini, opencode), and types "
        "the prompt as the first user input. The return shape includes a "
        "viewer_url the user opens to attach — share it immediately in plain "
        "text; it is the only handle to the session, and the user drives it "
        "until they exit. Interactive sessions do NOT appear in coda_inbox, "
        "and coda_get_result returns nothing for them — do not try to poll "
        "or fetch results. Note that git history is NOT available inside the "
        "session (files-only export); if the user needs history context, "
        "include a git log summary in the prompt string."
    ),
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)

# ── App hooks (PTY integration) ─────────────────────────────────────

_app_create_session = None
_app_send_input = None
_app_close_session = None


def set_app_hooks(
    create_session_fn,
    send_input_fn,
    close_session_fn,
):
    """Wire up Flask app callbacks for PTY operations.

    Registers the create/send/close hooks that ``coda_run`` and ``_watch_task``
    use to drive the underlying PTY session.
    """
    global _app_create_session, _app_send_input, _app_close_session
    _app_create_session = create_session_fn
    _app_send_input = send_input_fn
    _app_close_session = close_session_fn


# ── Background watcher ──────────────────────────────────────────────


def _watch_task(session_id: str, task_id: str, timeout_s: int) -> None:
    """Poll for result.json in a daemon thread.

    - Checks every 5 seconds for ``result.json`` in the task directory.
    - If found, calls ``task_manager.complete_task()`` (which auto-closes session).
    - Tracks last activity from ``status.jsonl`` mtime.
    - Timeout: if wall clock exceeds *timeout_s* AND no status update
      in the last 5 minutes, writes a timeout result and completes.
    - On completion, closes the PTY if hooks are wired.
    """
    tdir = task_manager._task_dir(session_id, task_id)
    status_path = os.path.join(tdir, "status.jsonl")
    start = time.time()
    stale_threshold = 300  # 5 minutes

    while True:
        time.sleep(5)

        # Check for result.json (may be at root or in results/ subdir)
        result_path = task_manager._find_result_json(tdir)
        if result_path:
            try:
                task_manager.complete_task(session_id, task_id)
                _close_pty_immediately(session_id)
                logger.info("Watcher: task %s completed (result found)", task_id)
            except Exception:
                logger.exception("Watcher: error completing task %s", task_id)
            return

        # Check timeout
        elapsed = time.time() - start
        if elapsed > timeout_s:
            # Check last activity
            try:
                last_activity = os.path.getmtime(status_path)
            except OSError:
                last_activity = start

            if (time.time() - last_activity) > stale_threshold:
                # Write timeout result and complete
                try:
                    timeout_result_path = os.path.join(tdir, "result.json")
                    task_manager._write_json(timeout_result_path, {
                        "status": "timeout",
                        "summary": "Task timed out",
                        "files_changed": [],
                        "artifacts": [],
                        "errors": [f"Timeout after {timeout_s}s with no activity for 5 min"],
                    })
                    task_manager.complete_task(session_id, task_id)
                    _close_pty_immediately(session_id)
                    logger.warning("Watcher: task %s timed out", task_id)
                except Exception:
                    logger.exception("Watcher: error timing out task %s", task_id)
                return


def _close_pty_immediately(session_id: str) -> None:
    """Close the PTY session associated with this task session immediately.

    Called by ``_watch_task`` as soon as the task transitions to completed
    or failed. Reads ``pty_session_id`` from the task-manager's session.json
    and calls the ``_app_close_session`` hook (i.e. ``mcp_close_pty_session``
    in production).
    """
    if _app_close_session is None:
        return
    try:
        session = task_manager._read_session(session_id)
        pty_session_id = session.get("pty_session_id")
        if pty_session_id:
            _app_close_session(pty_session_id)
    except Exception:
        logger.debug("Could not close PTY for session %s", session_id, exc_info=True)


# ── Tool definitions ────────────────────────────────────────────────


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
    ),
)
async def coda_run(
    prompt: str,
    email: str,
    context: str = "{}",
    previous_session_id: str = "",
    permissions: str = "smart",
    timeout_s: int = 3600,
    workflow_protocol: bool = True,
) -> str:
    """Submit a coding task — FIRE AND FORGET.

    Returns IMMEDIATELY with a task_id. The task runs autonomously in the
    background. After receiving the response, tell the user the task was
    submitted and move on. Do NOT follow up with coda_inbox or coda_get_result
    unless the user explicitly asks to check status later.

    ``context`` is a JSON string with Unity Catalog metadata (tables, schemas).
    ``previous_session_id`` chains to a prior task's session for context continuity.
    ``permissions`` can be ``"smart"`` (default, safe) or ``"yolo"`` (auto-approve all).

    ``workflow_protocol`` defaults to True, which injects a Databricks
    orientation block and a 3-phase workflow protocol (PLAN/EXECUTE/SYNTHESIZE
    with critique at each phase) into the agent's prompt. The protocol also
    defines the ``info_needed`` terminal status for clean handoff when the
    agent is blocked. Set False to skip — useful for non-Databricks tasks.

    Returns JSON with ``task_id``, ``session_id``, and ``status: "running"``.
    """
    try:
        # Check concurrency limit
        running = task_manager.count_running_tasks()
        if running >= task_manager.MAX_CONCURRENT_TASKS:
            return json.dumps({
                "status": "error",
                "error": f"Concurrency limit reached ({task_manager.MAX_CONCURRENT_TASKS} "
                         f"tasks running). Try again when a task completes.",
            })

        # Parse context JSON
        try:
            ctx = json.loads(context) if context else None
        except json.JSONDecodeError:
            return json.dumps({
                "status": "error",
                "error": f"Invalid JSON in context parameter: {context!r}",
            })

        # Auto-create ephemeral session
        session_result = task_manager.create_session(email, "", label="hermes-mcp")
        session_id = session_result["session_id"]

        # Create task first (we need task_id to compute transcript_path).
        result = task_manager.create_task(
            session_id=session_id,
            prompt=prompt,
            email=email,
            context=ctx,
            timeout_s=timeout_s,
            permissions=permissions,
            previous_session_id=previous_session_id or None,
            workflow_protocol=workflow_protocol,
        )
        task_id = result["task_id"]

        pty_session_id = None
        if _app_create_session is not None:
            transcript_path = os.path.join(
                task_manager._task_dir(session_id, task_id),
                "transcript.log",
            )
            pty_session_id = _app_create_session(
                label="hermes-mcp",
                transcript_path=transcript_path,
                replay_only=True,   # coda_run URLs are post-hoc review only
            )
            task_manager._update_session_field(
                session_id, "pty_session_id", pty_session_id
            )

        # Send to PTY if hooks are wired
        if _app_send_input is not None and pty_session_id is not None:
            tdir = task_manager._task_dir(session_id, task_id)
            prompt_path = os.path.join(tdir, "prompt.txt")
            cmd = f'hermes -z "{prompt_path}"'
            if permissions == "yolo":
                cmd += " --yolo"
            cmd += "\n"
            _app_send_input(pty_session_id, cmd)

            # Start background watcher
            t = threading.Thread(
                target=_watch_task,
                args=(session_id, task_id, timeout_s),
                daemon=True,
            )
            t.start()

        return json.dumps({
            "task_id": task_id,
            "session_id": session_id,
            "status": "running",
            "viewer_url": url_builder.build_viewer_url(pty_session_id) if pty_session_id else None,
        })

    except Exception as exc:
        return json.dumps({"status": "error", "error": str(exc)})


def _safe_dirname(workspace_path: str) -> str:
    """Local directory name for the pulled folder = sanitized basename."""
    base = os.path.basename(workspace_path.rstrip("/"))
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    # Reject empty and the traversal names "." / ".." — `.` and `-` are allowed
    # by the regex, so a basename of ".." would otherwise make ./<name> escape
    # or alias the project dir.
    if safe in ("", ".", ".."):
        return "workspace"
    return safe


def _normalize_workspace_path(workspace_path: str) -> str:
    """Canonical Workspace API path: drop the /Workspace FUSE prefix if present.

    The deployed terminal's CLI uses the unprefixed form (/Users/...); REST
    accepts both, but normalizing matches what the CLI expects and is harmless.
    """
    p = workspace_path.rstrip("/")
    if p.startswith("/Workspace/"):
        p = p[len("/Workspace"):]
    return p


_ALLOWED_AGENTS = {"claude", "hermes", "codex", "gemini", "opencode"}

# Wait for the agent's TUI to settle by polling the PTY output buffer. Returns
# as soon as the buffer length stays constant for _PROMPT_SEED_STABILITY_S, or
# _PROMPT_SEED_MAX_WAIT_S elapses (whichever first). Replaces a brittle
# hardcoded sleep that didn't adapt to slow agent cold-starts.
_PROMPT_SEED_MAX_WAIT_S = 5.0
_PROMPT_SEED_STABILITY_S = 1.0
# Terminal-side `databricks workspace export-dir` pull (coda_interactive). We wait
# for an explicit shell completion marker, NOT for output to go quiet: the
# databricks CLI cold-starts SILENTLY for ~2s before writing any files, so an
# output-quiet heuristic declares "done" too early and the disk check finds
# nothing. The pull command's tail echoes one of these tokens; they are built
# from split string literals in the command (echo "CODA""_PULL_""OK") so the
# contiguous form here appears ONLY when the echo executes — never in the
# shell's echo of the typed command line.
_PULL_MAX_WAIT_S = 60.0
_PULL_OK = "CODA_PULL_OK"
_PULL_FAIL = "CODA_PULL_FAIL"


async def _wait_for_output_stable(
    pty_session_id: str, max_wait: float, stability: float
) -> None:
    """Poll the PTY output buffer; return when it stabilizes or ``max_wait`` elapses.

    Stability = buffer length unchanged for ``stability`` seconds, after at least
    one byte has appeared. If the session disappears mid-wait (PTY died), return.
    """
    from app import sessions
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait
    last_len = -1
    stable_since: float | None = None
    poll_interval = 0.1

    while loop.time() < deadline:
        await asyncio.sleep(poll_interval)
        sess = sessions.get(pty_session_id)
        if sess is None:
            return
        current_len = sum(len(chunk) for chunk in sess.get("output_buffer", []))
        if current_len > 0 and current_len == last_len:
            if stable_since is None:
                stable_since = loop.time()
            elif (loop.time() - stable_since) >= stability:
                return
        else:
            stable_since = None
            last_len = current_len


async def _wait_for_agent_ready(pty_session_id: str) -> None:
    """Wait for an agent TUI to settle (prompt-seed budget). Wrapper for back-compat."""
    await _wait_for_output_stable(
        pty_session_id, _PROMPT_SEED_MAX_WAIT_S, _PROMPT_SEED_STABILITY_S
    )


def _buffer_text(chunks) -> str:
    """Decode a PTY output_buffer (list of bytes/str chunks) into one string."""
    parts = []
    for c in chunks:
        parts.append(c.decode("utf-8", "replace") if isinstance(c, (bytes, bytearray)) else str(c))
    return "".join(parts)


async def _wait_for_pull(pty_session_id: str, target_dir: str) -> str:
    """Wait for the terminal-side export-dir pull to finish. Returns 'ok'/'fail'/'timeout'.

    Watches the PTY output for the explicit completion marker echoed by the pull
    command's ``&& echo OK || echo FAIL`` tail — robust against the databricks
    CLI's silent cold-start (a "wait for output to go quiet" heuristic fires
    during that silence, before any files exist). On the OK marker we also
    confirm the files actually landed on disk.
    """
    from app import sessions
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _PULL_MAX_WAIT_S
    poll_interval = 0.2

    while loop.time() < deadline:
        await asyncio.sleep(poll_interval)
        sess = sessions.get(pty_session_id)
        if sess is None:
            return "fail"
        text = _buffer_text(sess.get("output_buffer", []))
        if _PULL_OK in text:
            if os.path.isdir(target_dir) and os.listdir(target_dir):
                return "ok"
            # Marker present but no files — treat as failure (shouldn't happen).
            return "fail"
        if _PULL_FAIL in text:
            return "fail"
    return "timeout"


_AGENT_LAUNCH_CMDS = {
    "claude": "claude",
    "hermes": "hermes chat",
    "codex": "codex",
    "gemini": "gemini",
    "opencode": "opencode",
}

# Agents that launch INTERACTIVELY with an auto-accept flag (no trust/permission
# dialog) and the kickoff prompt as a positional arg. For these, coda_interactive
# launches in one atomic command — no separate prompt-seeding, no TUI-ready wait.
# claude launches in a fresh per-session dir each time, which would otherwise trip
# its per-directory folder-trust dialog and swallow the prompt. Agents not listed
# fall back to launch -> wait-for-ready -> type the prompt.
_AGENT_AUTO_LAUNCH = {
    "claude": "claude --enable-auto-mode",
}


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
    ),
)
async def coda_interactive(
    prompt: str,
    workspace_path: str,
    agent: str = "claude",
    email: str = "",
) -> str:
    """Launch an interactive agent session in CoDA, handed off via a viewer URL.

    The MCP caller passes a Databricks Workspace directory path. This path must
    already exist in the Databricks Workspace — the tool runs inside CoDA and
    CANNOT read your local filesystem. If you are a local agent and the project
    files live locally, FIRST upload them, e.g.
    ``databricks workspace import-dir <local-dir> /Workspace/Users/<you>/<proj>``,
    then pass that ``/Workspace/Users/...`` path. CoDA pulls that folder onto the
    session's disk IN THE TERMINAL (authenticated as you) via
    ``databricks workspace export-dir``, launches the chosen agent (claude
    default) in the pulled directory, seeds ``prompt`` as the first user input,
    and returns a ``viewer_url`` the calling user opens to drive it.

    If the pull produces no files (bad path or no read access) the tool returns
    a ``status=error`` and does not launch the agent.

    Interactive sessions do NOT appear in ``coda_inbox`` and ``coda_get_result``
    will not return anything for them. The viewer URL is the only handle.

    ``email`` is accepted for forward-compatibility and is currently unused.

    Allowed agents: claude (default), hermes, codex, gemini, opencode.
    """
    if agent not in _ALLOWED_AGENTS:
        return json.dumps({
            "status": "error",
            "error": f"Unknown agent: {agent!r}. Allowed: {sorted(_ALLOWED_AGENTS)}",
        })

    if _app_create_session is None or _app_send_input is None:
        return json.dumps({
            "status": "error",
            "error": "PTY hook not wired",
        })

    pty_session_id = None
    project_dir = None
    try:
        # Create PTY FIRST so we have its session_id for the project_dir name.
        pty_session_id = _app_create_session(
            label=f"{agent}-interactive",
            replay_only=False,
        )
        project_dir = os.path.join(
            os.path.expanduser("~/.coda/projects"),
            pty_session_id,
        )
        os.makedirs(project_dir, exist_ok=True)

        name = _safe_dirname(workspace_path)
        source_path = _normalize_workspace_path(workspace_path)

        target_dir = os.path.join(project_dir, name)

        # Pull the Workspace folder into ./<name> AS THE USER (terminal creds).
        # The tail echoes a completion marker so we detect success/failure WITHOUT
        # relying on output timing — the databricks CLI cold-starts silently for
        # ~2s before writing files, so a "wait for output to go quiet" heuristic
        # races it and checks the disk too early. The marker tokens are split
        # across string literals (echo "CODA""_PULL_""OK") so their contiguous
        # form appears in the PTY output ONLY when the echo runs, never in the
        # shell's echo of the typed command line. A failed export-dir
        # short-circuits the && chain, so OK never prints and || echoes FAIL.
        pull_cmd = (
            f"cd {shlex.quote(project_dir)} && "
            f"databricks workspace export-dir {shlex.quote(source_path)} "
            f"{shlex.quote('./' + name)} && "
            f"cd {shlex.quote(name)} "
            f'&& echo "CODA""_PULL_""OK" || echo "CODA""_PULL_""FAIL"'
        )
        _app_send_input(pty_session_id, pull_cmd + "\n")

        outcome = await _wait_for_pull(pty_session_id, target_dir)
        if outcome != "ok":
            if _app_close_session is not None:
                try:
                    _app_close_session(pty_session_id)
                except Exception:
                    pass
            if os.path.isdir(project_dir):
                shutil.rmtree(project_dir, ignore_errors=True)
            if outcome == "timeout":
                msg = (
                    f"Timed out pulling files from {workspace_path} after "
                    f"{int(_PULL_MAX_WAIT_S)}s — the export may be very large or "
                    f"`databricks workspace export-dir` is hung."
                )
            else:
                msg = (
                    f"Failed to pull files from {workspace_path}. Check the path "
                    f"exists in the Workspace and that you have read access "
                    f"(ran `databricks workspace export-dir`)."
                )
            return json.dumps({"status": "error", "error": msg})

        # Kickoff prompt with a one-line context prefix naming the source. Kept
        # to ONE line so it is safe both as a quoted CLI arg and as typed input
        # (an embedded newline inside a quote would trigger shell line-continuation).
        seeded_prompt = (
            f"Your working directory holds files exported from the Databricks "
            f"Workspace path {workspace_path}. {prompt}"
        )

        # Launch the agent. Agents in _AGENT_AUTO_LAUNCH accept an auto-accept
        # flag + the prompt as a positional arg, so we launch in ONE atomic
        # command: no trust/permission dialog blocks the handoff, and the prompt
        # isn't subject to TUI cold-start timing. Other agents fall back to
        # launch -> wait-for-ready -> type the prompt.
        auto_launch = _AGENT_AUTO_LAUNCH.get(agent)
        if auto_launch is not None:
            _app_send_input(
                pty_session_id, f"{auto_launch} {shlex.quote(seeded_prompt)}\n"
            )
        else:
            _app_send_input(pty_session_id, _AGENT_LAUNCH_CMDS[agent] + "\n")
            await _wait_for_agent_ready(pty_session_id)
            _app_send_input(pty_session_id, seeded_prompt + "\n")

        viewer_url = url_builder.build_viewer_url(pty_session_id)

        return json.dumps({
            "status": "launched",
            "viewer_url": viewer_url,
            "agent": agent,
            "project_dir": target_dir,
            "workspace_path": workspace_path,
            "instructions": (
                "Open viewer_url to attach. The agent is running in a directory "
                "holding the files pulled from your Workspace folder, with your "
                "kickoff prompt typed. Type the agent's quit command (e.g. /quit) "
                "then `exit` to end the session. Note: files are a snapshot pulled "
                "via 'databricks workspace export-dir' — git history is not included."
            ),
        })
    except Exception as e:
        # Catch-all: ensure no resource leak.
        if pty_session_id and _app_close_session is not None:
            try:
                _app_close_session(pty_session_id)
            except Exception:
                pass
        if project_dir and os.path.isdir(project_dir):
            shutil.rmtree(project_dir, ignore_errors=True)
        return json.dumps({
            "status": "error",
            "error": f"coda_interactive failed: {e}",
        })


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
    ),
)
async def coda_inbox(
    email: str = "",
    status: str = "",
) -> str:
    """Check status of all background tasks — your inbox.

    Call this instead of polling — it returns ALL tasks at once.
    No need to track individual task_ids; the inbox shows everything
    from the last 24 hours: running, completed, and failed tasks.

    By default returns all tasks. Filter by ``status`` to narrow:
    ``"running"`` for in-progress only, ``"completed"`` for finished,
    ``"failed"`` for errors, or ``""`` (default) for everything.

    Each task includes: ``task_id``, ``session_id``, ``status``,
    ``elapsed_s``, ``prompt_summary`` (first 100 chars of what was asked),
    ``previous_session_id`` (if chained from prior work).
    Completed tasks also include ``summary`` (what was done).
    Running tasks also include ``progress`` (latest agent step).

    Returns JSON with ``tasks`` (list sorted most recent first)
    and ``counts`` (e.g. ``{"running": 1, "completed": 2, "failed": 0}``).
    """
    try:
        tasks = task_manager.list_all_tasks(email=email, status_filter=status)
        # Decorate each task with its viewer URL (if available).
        for t in tasks:
            sess = task_manager._read_session_safe(t["session_id"])
            pty = sess.get("pty_session_id") if sess else None
            if pty:
                vu = url_builder.build_viewer_url(pty)
                if vu:
                    t["viewer_url"] = vu

        counts = {
            "running": 0,
            "completed": 0,
            "failed": 0,
            "info_needed": 0,
            "needs_approval": 0,
        }
        for t in tasks:
            s = t.get("status", "")
            if s in counts:
                counts[s] += 1
            elif s == "done":
                counts["completed"] += 1
            elif s == "timeout":
                counts["failed"] += 1

        return json.dumps({"tasks": tasks, "counts": counts})
    except Exception as exc:
        return json.dumps({"status": "error", "error": str(exc)})


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
    ),
)
async def coda_get_result(
    task_id: str,
    session_id: str,
) -> str:
    """Retrieve the structured result of a completed task.

    Call this AFTER coda_inbox shows a task as "completed", "failed",
    "info_needed", or "needs_approval".

    Returns JSON with ``task_id``, ``session_id``, ``status``, ``summary``
    (what was done or why the agent stopped), ``files_changed`` (list of
    modified files), ``artifacts`` (job IDs, commit hashes, etc.),
    ``errors`` (if any), and — when status is "info_needed" — ``feedback``
    (a precise description of what context the caller must add before
    resubmitting).
    """
    try:
        result = task_manager.get_task_result(task_id, session_id)
        if result is None:
            # No result yet — return current status
            status = task_manager.get_task_status(task_id, session_id)
            return json.dumps({
                "task_id": task_id,
                "session_id": session_id,
                "status": status.get("status", "unknown"),
                "message": "Result not yet available — task is still in progress.",
            })

        result["task_id"] = task_id
        result["session_id"] = session_id
        # Ensure standard fields exist
        result.setdefault("status", "done")
        result.setdefault("summary", "")
        result.setdefault("files_changed", [])
        result.setdefault("artifacts", [])
        result.setdefault("errors", [])
        # Decorate with viewer_url if known
        sess = task_manager._read_session_safe(session_id)
        pty = sess.get("pty_session_id") if sess else None
        if pty:
            vu = url_builder.build_viewer_url(pty)
            if vu:
                result["viewer_url"] = vu
        return json.dumps(result)
    except Exception as exc:
        return json.dumps({"status": "error", "task_id": task_id, "error": str(exc)})


# ── Standalone entry point ──────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
