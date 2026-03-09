# PRD: WebSocket for Terminal I/O

**Status:** COMPLETE
**Author:** Tech Lead
**Date:** 2026-03-08
**Slug:** websocket-terminal-io

## Problem Statement

The terminal app currently uses HTTP polling at 100ms intervals (10 requests/sec per focused terminal) via a Web Worker (`poll-worker.js`). This approach:

1. Creates continuous network chatter (10 HTTP round-trips per second per pane)
2. Adds latency -- output only appears at the next poll interval, not instantly
3. Wastes battery and bandwidth on mobile/laptop
4. Does not scale well with multiple panes (20 req/sec with split panes)

## Proposed Solution

Replace HTTP polling with WebSocket (Socket.IO) for real-time, bidirectional terminal I/O. The server pushes output to clients as soon as it is read from the PTY, eliminating the polling delay. HTTP endpoints remain as a fallback.

## Technical Approach

- **Server**: Add `flask-socketio` with `simple-websocket` transport (threading mode, no eventlet/gevent). Wrap the Flask app with `SocketIO`. Add WebSocket event handlers for terminal input, resize, session join/leave. Modify `read_pty_output()` to emit output via SocketIO rooms.
- **Client**: Load Socket.IO client from CDN. On pane creation, connect via WebSocket and join a session room. Send input/resize via `socket.emit()`. Receive output via `socket.on('terminal_output')`. Fall back to HTTP polling if WebSocket fails.
- **Gunicorn**: Keep `workers=1` and `gthread` worker class. `simple-websocket` works with threading -- no worker class change needed.

## Acceptance Criteria

### AC-1: Server dependencies added
`flask-socketio` and `simple-websocket` are added to `requirements.txt`. They install without errors.

### AC-2: SocketIO instance created and wraps Flask app
A `SocketIO` instance is created in `app.py`, wrapping the Flask app with `async_mode='threading'`. The `socketio` object is available at module level.

### AC-3: WebSocket connect event authenticates clients
The `connect` event handler checks authorization using the same logic as `authorize_request()` (checking `X-Forwarded-Email` against `app_owner`). Unauthenticated connections are rejected.

### AC-4: `join_session` event adds client to a SocketIO room
When a client emits `join_session` with `{session_id}`, the server validates the session exists, then adds the client to a room named by the session_id. The server acknowledges with `{status: 'ok'}`.

### AC-5: `leave_session` event removes client from a SocketIO room
When a client emits `leave_session` with `{session_id}`, the server removes the client from the session room.

### AC-6: `terminal_input` event writes to PTY
When a client emits `terminal_input` with `{session_id, input}`, the server writes the input to the PTY's master_fd -- same behavior as `POST /api/input`.

### AC-7: `terminal_resize` event resizes PTY
When a client emits `terminal_resize` with `{session_id, cols, rows}`, the server performs the ioctl resize -- same behavior as `POST /api/resize`.

### AC-8: Server pushes output via SocketIO rooms
`read_pty_output()` emits `terminal_output` events with `{session_id, output}` to the session's room whenever PTY output is read. Output is still buffered in the deque for HTTP fallback.

### AC-9: Server emits `session_exited` when process exits
When a PTY process exits, the server emits `session_exited` with `{session_id}` to the session room before marking the session as exited.

### AC-10: Client loads Socket.IO from CDN
`static/index.html` includes `<script src="https://cdn.socket.io/4.8.1/socket.io.min.js">` before the main script block.

### AC-11: Client connects via WebSocket on pane creation
When `createPane()` runs, the client creates a Socket.IO connection (if not already connected), emits `join_session` with the session_id, and registers listeners for `terminal_output` and `session_exited`.

### AC-12: Client sends input via WebSocket when connected
`sendInput()` uses `socket.emit('terminal_input', ...)` when a WebSocket connection is active, falling back to `fetch('/api/input', ...)` otherwise.

### AC-13: Client sends resize via WebSocket when connected
`sendResize()` uses `socket.emit('terminal_resize', ...)` when a WebSocket connection is active, falling back to `fetch('/api/resize', ...)` otherwise.

### AC-14: Client falls back to HTTP polling when WebSocket fails
If the WebSocket connection fails or disconnects, the client automatically falls back to the existing `poll-worker.js` HTTP polling mechanism. When WebSocket reconnects, polling stops again.

### AC-15: HTTP endpoints preserved
All existing HTTP endpoints (`/api/input`, `/api/output`, `/api/resize`, `/api/session`, `/api/session/close`, `/api/heartbeat`, `/api/upload`) continue to work unchanged.

### AC-16: Polling disabled when WebSocket is active
The `poll-worker.js` polling interval is stopped (or not started) for a pane when its WebSocket connection is active. Heartbeats are also unnecessary over WebSocket since the persistent connection implicitly keeps the session alive.

### AC-17: Session timeout updated for WebSocket connections
The `last_poll_time` for a session is updated on WebSocket activity (input, join, heartbeat) so sessions using WebSocket do not get cleaned up by the stale session reaper.

### AC-18: Gunicorn configuration supports WebSocket
`gunicorn.conf.py` works with Flask-SocketIO using `simple-websocket`. The app starts and accepts WebSocket upgrade requests. `workers=1` is preserved.

### AC-19: App imports without errors
`uv run python -c "from app import app, socketio"` succeeds without import errors.

## Out of Scope

- Changing setup scripts or state sync code
- Adding eventlet or gevent
- Removing HTTP endpoints
- Adding authentication mechanisms beyond what exists
- Load testing or horizontal scaling

## Key Files to Modify

| File | Changes |
|------|---------|
| `requirements.txt` | Add `flask-socketio` and `simple-websocket` |
| `app.py` | Add SocketIO instance, event handlers, modify `read_pty_output()` |
| `static/index.html` | Add Socket.IO client, WebSocket logic, fallback behavior |
| `gunicorn.conf.py` | Potentially update for SocketIO compatibility |

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| WebSocket blocked by corporate proxy | HTTP long-polling fallback built into Socket.IO |
| Thread safety with SocketIO emit from PTY reader | Flask-SocketIO's `emit()` is thread-safe when using `socketio.emit()` (not `flask_socketio.emit()`) |
| Session cleanup race with WebSocket disconnect | Handle `disconnect` event to clean up room membership; do not auto-close PTY on disconnect (client may reconnect) |
