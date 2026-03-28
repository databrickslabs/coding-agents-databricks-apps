# Session Detach & Reconnect

**Date:** 2026-03-28
**Context:** Coding agent sessions (claude, opencode, gemini) should survive tab closure. Only `exit` in the shell kills a session.

---

## Problem

Closing a browser tab kills the PTY process immediately via `sendBeacon('/api/session/close')`. For a coding agent mid-task, this destroys work in progress. The user didn't intend to kill the session — they just closed a tab.

## Design

### Principle: Detach, Don't Kill

- **Tab/pane close = detach.** Frontend disconnects, PTY keeps running.
- **`exit` in shell = the only kill.** PTY EOF detection triggers cleanup.
- **24-hour reaper = safety net.** Orphaned sessions die after 24h with no heartbeat.

### Changes

#### 1. Frontend — `cleanupPane()` stops killing

Remove `sendBeacon('/api/session/close')` from `cleanupPane()`. Keep poll stop, WS room leave, and xterm disposal. The `beforeunload` handler still calls `cleanupAllPanes()` but it no longer kills anything. `pagehide` already just sends a heartbeat.

#### 2. Backend — `GET /api/sessions`

Returns active sessions with process detection:

```json
[
  {
    "session_id": "abc-123",
    "created_at": 1743120382.5,
    "last_poll_time": 1743120982.5,
    "exited": false,
    "process": "claude",
    "idle_seconds": 342
  }
]
```

Process detection: `ps --ppid {pid} -o comm=` to find the child process of the shell. Falls back to "bash" if no child.

Added to auth skip list alongside `/api/pat-status`.

#### 3. Backend — `POST /api/session/attach`

Reattach to an existing session:

- Input: `{ session_id }`
- Validates session exists and not exited
- Resets `last_poll_time` (restarts 24h idle clock)
- Returns output buffer (last ~1000 lines) for replay
- Returns metadata (process name, created_at)

```json
{
  "session_id": "abc-123",
  "output": ["line1\r\n", "line2\r\n"],
  "process": "claude",
  "created_at": 1743120382.5
}
```

#### 4. Frontend — Session picker on return visit

The picker only appears when PAT is already valid (return visit). First-time PAT flow always creates a new session.

```
createPane()
  → /api/pat-status
  → invalid → PAT prompt → setup → create new session
  → valid   → GET /api/sessions
              → 0 sessions → create new
              → 1 session  → auto-reattach (replay buffer)
              → N sessions → show picker
```

**Picker UI** (rendered in xterm with mouse support):

```
  Existing sessions:

  claude   (running, 2h ago)    [Attach]  [✕]
  opencode (running, 45m ago)   [Attach]  [✕]
  bash     (idle, 3h ago)       [Attach]  [✕]

  [+ New session]
```

- Click **Attach** or session row → `POST /api/session/attach`, replay buffer, join WS room, start polling
- Click **✕** → `POST /api/session/close` for that session, re-render picker
- Click **+ New session** → `POST /api/session` as today
- One session → skip picker, auto-reattach

#### 5. Exited session cleanup

When `read_pty_output()` detects EOF (user typed `exit`), call `terminate_session()` immediately to remove from dict. No zombie sessions in the picker.

Session picker also filters out `exited: true` (defensive, race condition guard).

---

## Files to Modify

| File | Change |
|------|--------|
| `app.py` | Add `GET /api/sessions`, `POST /api/session/attach`. Update auth skip list. Update `read_pty_output()` to call `terminate_session()` on EOF. Add `_get_session_process(pid)` helper. |
| `static/index.html` | Remove `sendBeacon('/api/session/close')` from `cleanupPane()`. Add session picker flow in `createPane()`. Add mouse click handling for picker UI. |

## What Doesn't Change

- `POST /api/session/close` endpoint stays — used by EOF cleanup path
- `terminate_session()` stays — core kill logic unchanged
- 24-hour timeout stays — safety net for orphans
- `pagehide` heartbeat stays — already correct
- WebSocket disconnect behavior stays — already doesn't kill PTY
- PAT rotation, session awareness — unchanged (sessions still count)
