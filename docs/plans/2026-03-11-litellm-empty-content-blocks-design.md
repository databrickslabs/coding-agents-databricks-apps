# Design: LiteLLM Local Proxy for Empty Content Block Sanitization

**Date:** 2026-03-11
**Branch:** `fix/litellm-empty-content-blocks`
**Related:** OpenCode [#5028](https://github.com/sst/opencode/issues/5028), LiteLLM [PR #20384](https://github.com/BerriAI/litellm/pull/20384)

## Problem

OpenCode intermittently sends malformed messages containing empty text content blocks
(`{"type": "text", "text": ""}`) to the Databricks Foundation Model API. This occurs during:

1. **Streaming** — empty text blocks appear between thinking blocks in conversation history
2. **Compaction** — `/compact` command produces empty or whitespace-only blocks
3. **Model switching** — switching between models (e.g., Gemini to Claude) generates whitespace-only chunks

The Databricks Foundation Model API strictly rejects these with:
```
Bad Request: {"message":"messages: text content blocks must be non-empty"}
```

Once a corrupted message enters the conversation history, **every subsequent request fails** —
the session is permanently bricked. This is OpenCode issue
[#5028](https://github.com/sst/opencode/issues/5028), still open as of March 2026.

## Why Not PR #52's Approach

[PR #52](https://github.com/datasciencemonkey/coding-agents-databricks-apps/pull/52) proposes
forking OpenCode (`dgokeeffe/opencode`) to add a native Databricks provider. After analysis:

1. **Does not fix the root cause** — The fork's `feat/databricks-ai-sdk-provider` branch
   has no commits that sanitize empty content blocks. The bug originates in OpenCode's core
   agent loop (conversation history management), not the provider layer. A native provider
   sends whatever the core gives it.

2. **Fork maintenance burden** — Must track upstream OpenCode releases indefinitely.
   When upstream fixes #5028, the fork may conflict.

3. **Scope creep** — PR #52 bundles the fork with a spawner app, GitHub CLI setup,
   and performance fixes. These are independent concerns that should be separate PRs.

4. **Fragile coupling** — Tightly couples our project to a fork that may diverge from
   upstream, creating long-term maintenance risk for a demo/tool project.

### What to cherry-pick from PR #52 (separately)

PR #52 contains valuable changes that are **independent of the fork** and should be
extracted into their own PRs:

- **Performance fixes** — `select()` timeout reduction (500ms → 50ms), lock contention
  fixes in `get_output_batch()` and `cleanup_stale_sessions()`, poll-worker interval
  reduction (100ms → 50ms). These are changes to `app.py` and `static/poll-worker.js`.

- **WebSocket detection fix** — Correct Socket.IO transport detection that checks
  `socket.io.engine.transport.name` instead of trusting `connected=true`. This is a
  change to `static/index.html`.

- **GitHub CLI setup** — Automated `gh` install with xterm.js-safe auth wrapper.
  Standalone setup script.

These should be reviewed and merged independently — they don't require the OpenCode fork.

## Our Approach: LiteLLM Local Proxy

Run a lightweight LiteLLM instance **inside the same container** on an internal port.
It intercepts requests from OpenCode, strips empty content blocks via the sanitization
logic added in [LiteLLM PR #20384](https://github.com/BerriAI/litellm/pull/20384),
and forwards clean messages to Databricks AI Gateway.

### Architecture

In the current setup, **OpenCode** talks directly to the **Databricks AI Gateway**.
Because OpenCode sends malformed "empty text blocks," the Gateway rejects them
immediately with a 400 error.

By introducing **LiteLLM**, we change the traffic flow inside the container:

```
Users → port 8000 (Flask/xterm.js UI)
              ↓ spawns PTY
       OpenCode → localhost:4000 (LiteLLM) → Databricks AI Gateway → Claude/Gemini
```

1. **OpenCode** (the agent) sends the request to `http://localhost:4000` (the **LiteLLM Proxy**).
2. **LiteLLM** intercepts the request *before* it leaves the container.
3. **LiteLLM** applies the sanitization logic (stripping the `{"type": "text", "text": ""}` blocks).
4. **LiteLLM** then forwards the "cleaned" request to the **Databricks AI Gateway**.
5. **Databricks** receives a perfectly valid request and processes it.

So, while the traffic eventually reaches Databricks, it is "washed" by LiteLLM locally
first. This ensures that the Databricks Gateway never sees the malformed data that causes
it to throw an error.

- **Port 8000** — Flask/Gunicorn (exposed to users via Databricks Apps)
- **Port 4000** — LiteLLM proxy (internal only, never exposed externally)
- Databricks Apps only routes external traffic to port 8000

When upstream OpenCode eventually fixes #5028, LiteLLM becomes a no-op (nothing to
strip) — it degrades gracefully. At that point, remove `setup_litellm.py`, revert the
baseURL in `setup_opencode.py`, and drop the dependency.

### Implementation Plan

#### 1. Add `litellm` to `requirements.txt`

```
litellm>=1.60
```

#### 2. Create `setup_litellm.py`

New setup script that:
- Writes a LiteLLM config YAML pointing to Databricks AI Gateway
- Starts LiteLLM as a background process on `localhost:4000`
- Waits for the health endpoint to confirm it's ready
- Maps each Databricks model to the `databricks/` prefix so the sanitization path activates

#### 3. Update `setup_opencode.py`

Change OpenCode's `baseURL` from the Databricks Gateway URL to `http://localhost:4000`
so all requests route through LiteLLM first. The model names and auth stay the same.

#### 4. Add `litellm` setup step to `app.py`

Add a new step in `run_setup()` that runs **before** the parallel agent setup
(LiteLLM must be running before OpenCode starts using it):

```python
# Sequential: LiteLLM proxy must be running before agents that use it
_run_step("litellm", ["python", "setup_litellm.py"])

# Then parallel agent setup...
```

#### 5. Health check

`setup_litellm.py` should poll `http://localhost:4000/health` before returning success,
ensuring the proxy is ready before OpenCode sends its first request.

### Trade-offs

| Aspect | Impact |
|--------|--------|
| Added dependency | `litellm` package (~small footprint as proxy) |
| Added latency | Negligible — localhost hop, no network |
| Startup time | ~2-3s for LiteLLM to start (sequential, before agents) |
| Maintenance | Zero — LiteLLM is a well-maintained OSS project |
| Graceful degradation | When #5028 is fixed upstream, proxy strips nothing |
| Governance preserved | AI Gateway, MLflow tracing, Unity Catalog all intact |

### Testing

1. Deploy to Databricks Apps
2. Launch OpenCode with `databricks-claude-opus-4-6`
3. Run 10+ iterations including `/compact` — verify no 400 errors
4. Check MLflow traces — confirm requests still flow through AI Gateway
5. Verify LiteLLM is NOT accessible from outside the container (port 4000 not exposed)
