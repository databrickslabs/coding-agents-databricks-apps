# CoDA MCP v2 — Background Execution + Inbox Pattern

## Overview

CoDA exposes 3 MCP tools so Databricks GenieCode (or any MCP client) can delegate
coding tasks to AI agents running in the background. GenieCode's chat context stays
free while tasks execute — no polling required.

## Tools

| Tool | Purpose |
|------|---------|
| `coda_run` | Fire-and-forget task submission |
| `coda_inbox` | Dashboard of all background tasks |
| `coda_get_result` | Pull full structured result |

## Flow Diagram

```
┌─────────────┐         ┌──────────────┐         ┌─────────────┐
│  GenieCode  │         │   CoDA MCP   │         │   Hermes    │
│  (caller)   │         │   (3 tools)  │         │  (executor) │
└──────┬──────┘         └──────┬───────┘         └──────┬──────┘
       │                       │                        │
       │  1. coda_run(prompt)  │                        │
       │──────────────────────>│                        │
       │                       │  auto-create session   │
       │                       │  + PTY + task dir      │
       │                       │  write prompt.txt      │
       │                       │  write meta.json       │
       │                       │                        │
       │  {task_id, sess_id,   │  hermes -z prompt.txt  │
       │   status: "running"}  │───────────────────────>│
       │<──────────────────────│                        │
       │                       │   _watch_task thread   │
       │  ✓ context is FREE    │   monitors result.json │
       │  user keeps chatting  │                        │
       │                       │                        │  works...
       │         ...           │                        │  delegates
       │                       │                        │  to claude/
       │                       │                        │  codex/gemini
       │                       │                        │
       │  2. coda_inbox()      │                        │  writes
       │──────────────────────>│                        │  status.jsonl
       │                       │  scan all sessions     │
       │  {tasks: [...],       │  read meta + status    │
       │   counts: {run:1}}    │                        │
       │<──────────────────────│                        │
       │                       │                        │
       │         ...           │                        │  writes
       │                       │                        │  result.json
       │                       │                        │
       │                       │  _watch_task detects   │
       │                       │  result.json exists    │
       │                       │  → complete_task()     │
       │                       │  → auto-close session  │
       │                       │  → free PTY            │
       │                       │                        │
       │  3. coda_inbox()      │                        │
       │──────────────────────>│                        │
       │  {tasks: [{status:    │                        │
       │   "completed",        │                        │
       │   summary: "..."}]}   │                        │
       │<──────────────────────│                        │
       │                       │                        │
       │  4. coda_get_result() │                        │
       │──────────────────────>│                        │
       │  {summary, files,     │  read result.json      │
       │   artifacts, errors}  │                        │
       │<──────────────────────│                        │
       │                       │                        │
       ├── CHAINING ───────────┤                        │
       │                       │                        │
       │  5. coda_run(prompt,  │                        │
       │  previous_session_id) │  new session + PTY     │
       │──────────────────────>│  inject PRIOR SESSION  │
       │                       │  block in prompt       │
       │  {new task_id,        │───────────────────────>│
       │   new sess_id}        │                        │  reads prior
       │<──────────────────────│                        │  result.json
       │                       │                        │  for context
```

## Key Design Decisions

### Sessions are ephemeral, tasks are persistent
- Session = PTY + Hermes instance. Auto-closes when task completes.
- Task state (prompt, status, result) persists on disk for 24 hours.
- Continuity via `previous_session_id`, not long-lived sessions.

### No polling from GenieCode
- `coda_inbox` replaces `coda_get_status` — shows ALL tasks at once.
- GenieCode checks when the user asks, not on a timer.
- CoDA's internal `_watch_task` thread polls the filesystem (invisible to caller).

### Task chaining
- `previous_session_id` points to a prior session's disk state.
- Hermes reads `~/.coda/sessions/{prev_id}/tasks/*/result.json` for context.
- Chain depth: one level. Hermes can walk deeper if needed.

### Concurrency
- `CODA_MAX_CONCURRENT` env var (default: 5).
- Each task gets its own session — no "session busy" errors.
- Exceeding the limit returns a clear error.

## Data Model

```
~/.coda/sessions/{session-id}/
    session.json          # metadata + auto-close timestamp
    tasks/{task-id}/
        prompt.txt        # wrapped prompt sent to Hermes
        meta.json         # {email, created_at, previous_session_id, permissions}
        status.jsonl      # append-only progress log
        result.json       # final structured output
```

## Tool Reference

### `coda_run`

```python
coda_run(
    prompt: str,                       # what to do
    email: str,                        # who's asking
    context: str = "{}",               # UC metadata (tables, schemas)
    previous_session_id: str = "",     # chain from prior work
    permissions: str = "smart",        # "smart" or "yolo"
    timeout_s: int = 3600,             # max 1 hour default
)
# Returns: {"task_id", "session_id", "status": "running"}
```

### `coda_inbox`

```python
coda_inbox(
    email: str = "",      # filter by user
    status: str = "",     # "running", "completed", "failed", or "" for all
)
# Returns: {"tasks": [...], "counts": {"running": N, "completed": N, "failed": N}}
```

Each task entry: `task_id`, `session_id`, `status`, `elapsed_s`, `prompt_summary`,
`summary` (completed), `progress` (running), `previous_session_id`, `created_at`.

### `coda_get_result`

```python
coda_get_result(task_id: str, session_id: str)
# Returns: {"task_id", "session_id", "status", "summary",
#           "files_changed", "artifacts", "errors"}
```

## Migration from v1

| v1 Tool | v2 Equivalent |
|---------|--------------|
| `coda_create_session` | Removed — auto-created by `coda_run` |
| `coda_run_task` | `coda_run` (simplified, auto-session) |
| `coda_get_status` | `coda_inbox` (all tasks at once) |
| `coda_get_result` | `coda_get_result` (unchanged) |
| `coda_close_session` | Removed — auto-closed on completion |

## Limitations

- **Ephemeral filesystem**: On Databricks Apps, `~/.coda/` is local disk. App
  redeployment wipes task state. Real artifacts (git commits, jobs, workspace files)
  are unaffected.
- **No push notifications**: GenieCode must call `coda_inbox` to discover completions.
  SSE/streaming is a future consideration if polling proves insufficient.
