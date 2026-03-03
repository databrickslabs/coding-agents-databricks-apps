# Issue #8: Frontend Keep-Alive, Reconnection & Web Worker Polling

## Context

The frontend polling is fragile. A single `setInterval` at 100ms calls `/api/output` — any non-200 response immediately kills the session with no retry. Browsers throttle background tab timers, so switching tabs easily causes polls to stall past the 300s timeout. The current workaround (bumping timeout from 60s to 300s) masks the problem but doesn't fix it.

**Branch:** Create `feat/frontend-keepalive` off `main`

## Architecture

```
Main Thread (index.html)          Web Worker (poll-worker.js)         Backend (app.py)
─────────────────────────         ──────────────────────────         ────────────────
- xterm.js / DOM                  - Output polling (100ms fg)         - /api/output (existing)
- visibilitychange handler  ←──→  - Heartbeat polling (30s bg)  ──→  - /api/heartbeat (NEW)
- pagehide sendBeacon             - Retry/backoff state               - /api/session/close
- Input/resize sending            - Per-pane state map
```

Web Workers are NOT throttled by browsers in background tabs — this is the key benefit.

## Changes

### 1. Backend: Add `/api/heartbeat` endpoint

**File:** `app.py` (insert after `/api/output` at line 530)

```python
@app.route("/api/heartbeat", methods=["POST"])
def heartbeat():
    """Lightweight keep-alive — resets timeout without draining output buffer."""
    data = request.json
    session_id = data.get("session_id")
    with sessions_lock:
        if session_id not in sessions:
            return jsonify({"error": "Session not found"}), 404
        session = sessions[session_id]
        session["last_poll_time"] = time.time()
        timeout_warning = session.pop("timeout_warning", False)
    return jsonify({"status": "ok", "timeout_warning": timeout_warning})
```

Critical: does NOT touch `output_buffer` — output is only drained by `/api/output`.

### 2. New file: `static/poll-worker.js`

Web Worker handling all HTTP polling and retry logic (~120 lines).

**Per-pane state:**
```javascript
const panes = new Map();
// Each: { sessionId, pollTimerId, heartbeatTimerId, retryCount, mode: 'foreground'|'background' }
```

**Message protocol (main → worker):**
- `{ type: 'start_poll', paneId, sessionId }` — begin polling for a pane
- `{ type: 'stop_poll', paneId }` — stop polling on close
- `{ type: 'visibility_change', hidden: bool }` — switch fg/bg mode

**Message protocol (worker → main):**
- `{ type: 'output', paneId, data }` — terminal output + flags
- `{ type: 'session_ended', paneId, reason }` — 'exited' | 'auth_expired' | 'shutting_down'
- `{ type: 'connection_status', paneId, status, attempt, maxAttempts }` — reconnecting/connected
- `{ type: 'session_dead', paneId }` — retries exhausted

**Retry strategy:** Capped exponential backoff with jitter
- Base: 500ms, multiplier: 2x, max delay: 10s, max attempts: 5
- Schedule: ~500ms → ~1s → ~2s → ~4s → ~8s (~15.5s total)
- 403 (auth) and `exited` flag: no retry (permanent)
- 404, 5xx, network error: full retry with backoff

**Visibility modes:**
- Foreground: output poll every 100ms, no heartbeat
- Background: no output poll, heartbeat every 30s

### 3. Modify `static/index.html`

**Remove:**
- `pollOutput(pane)` function (lines 704-738)
- `setInterval(() => pollOutput(pane), 100)` (line 809)

**Add:**
- Worker init: `const pollWorker = new Worker('/static/poll-worker.js');`
- `handleWorkerMessage(event)` — routes worker messages to xterm writes per pane
- `visibilitychange` listener → sends `visibility_change` to worker
- `pagehide` listener → `navigator.sendBeacon('/api/heartbeat', ...)` for all active panes

**Modify:**
- `createPane()`: replace `setInterval` with `pollWorker.postMessage({ type: 'start_poll', ... })`
- `cleanupPane(pane)`: replace `clearInterval` with `pollWorker.postMessage({ type: 'stop_poll', ... })`
- Remove `pollInterval` from pane object (no longer needed)

### 4. New test: `tests/test_heartbeat.py`

- Heartbeat with valid session returns 200, resets `last_poll_time`
- Heartbeat with unknown session returns 404
- Heartbeat does NOT drain output buffer (critical invariant)
- Heartbeat returns and clears `timeout_warning` flag

## Edge Cases Handled

| Scenario | Behavior |
|----------|----------|
| Background tab | Worker switches to 30s heartbeat; resumes 100ms polling on return |
| Laptop sleep (>5min) | Session expires server-side; on wake, retry exhaustion → "Connection lost" |
| Backend restart/deploy | `shutting_down` flag warns client; retries handle brief downtime |
| Auth expired (403) | No retry, immediate "refresh page" message |
| Network blip | Backoff retries recover transparently |
| Multiple panes | Independent per-pane state in Worker |
| `pagehide` (tab close) | sendBeacon fires heartbeat as safety net before Worker dies |

## Verification

1. `uv run --with pytest pytest tests/test_heartbeat.py -v` — heartbeat tests pass
2. `uv run --with pytest pytest tests/ -v` — all existing tests still pass
3. Manual: open terminal, verify output works at 100ms (Network tab)
4. Manual: background tab 30s → return → session alive, buffered output appears
5. Manual: background tab >5min → return → clean "session expired" message
6. Manual: check Network tab shows `/api/heartbeat` every ~30s when backgrounded
