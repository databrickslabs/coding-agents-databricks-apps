# Pending work (scratch — wipe after done)

For each todo, the loop is:
1. Brainstorm shape → **critique gate**
2. Plan → **critique gate**
3. Implement → **critique gate**

A critique pass is mandatory at every gate (use `oh-my-claudecode:critic` or `oh-my-claudecode:architect` subagents, depending on whether the review is about quality/quality or design/architecture).

---

## Todo 1 — `coda_run` returns replay-only URL (no live attach)

**Intent.** Split the two use cases by tool, not by URL behavior. `coda_run` is fire-and-forget batch — its returned `viewer_url` should be **read-only static replay** of what the agent did. Live interaction is the exclusive surface area of Todo 2.

**Why.** Today `coda_run`'s `viewer_url` does double duty: live PTY attach during a 5-minute grace window, then static replay forever after. With `coda_interactive` arriving in Todo 2 as the dedicated live-attach tool, the dual-mode on `coda_run` is no longer useful — it just confuses the contract.

**Scope hint** (to refine in brainstorming):
- Server: `coda_run`'s `viewer_url` should resolve to the static-replay endpoint, not the live-PTY join path
- Static replay reads the on-disk transcript that's already being written (no changes to the tee mechanism)
- The 5-minute PTY grace period for live attach is no longer reachable from `coda_run`'s URL (still applies to `coda_interactive`)
- Update test expectations in `test_mcp_integration.py`, `test_mcp_server.py`, `test_replay_attach.py`

---

## Todo 2 — New MCP tool `coda_interactive`

**Intent.** MCP caller hands off to a human. Task is "running" until the agent process exits (human types `exit` / `/quit` / Ctrl-D).

**Default agent.** `claude`. Pluggable via `agent` parameter: `claude` (default), `hermes`, `codex`, `gemini`, `opencode`.

**Surface** (to refine in brainstorming):
```python
coda_interactive(
    prompt: str,
    agent: str = "claude",
    email: str = "",
    context: str = "",
    previous_session_id: str = "",
    timeout_s: int = 1800,  # 30 min — human-driven, generous
)
```

**Returns:** `{task_id, session_id, viewer_url, agent, status: "awaiting_human", instructions}`

**Flow** (to refine in brainstorming):
1. Reuse `coda_run`'s task setup (task_dir, prompt.txt, meta.json, PTY with transcript_path)
2. Send agent launch command per agent matrix
3. Wait briefly for agent to initialize
4. Paste prompt as first user message
5. Watcher polls for PTY child exit (master_fd EOF) — not `result.json`
6. On exit, write `result.json` = `{status: "completed", agent, transcript_path, exit_reason}`

**Agent launch matrix** (verify in brainstorming):
| Agent | Launch command |
|-------|----------------|
| `claude` | `claude` |
| `hermes` | `hermes chat` |
| `codex` | `codex` |
| `gemini` | `gemini` (or `gemini chat`?) |
| `opencode` | `opencode` |

---

## Workflow rules

- One todo at a time. Finish Todo 1 fully (brainstorm → critique → plan → critique → implement → critique) before starting Todo 2.
- Every critique gate uses a fresh subagent. No skipping.
- Both todos share the same branch (`coda-mcp`).
- Both eventually go into the same PR (or a new PR that subsumes #66 — decide later).
