# Tmux Evaluation for Session Persistence

**Date:** 2026-03-08
**Branch:** feat/multi-tab-terminals
**Verdict:** Remove tmux. Use localStorage-based session recovery instead.

## What We Tried

Wrapped PTY sessions in `tmux new-session -A -s pane-{id}` so terminals survive Flask worker restarts and browser tab closes. Added `/api/tmux-sessions` endpoint, tmux AppImage install step, and frontend reattach logic.

## Problems Observed

1. **Green status bar** — tmux renders its own status line (`[pane-0] 0:zsh*`), stealing screen space and looking foreign inside xterm.js
2. **Dot fill pattern** — tmux fills unused area with dots when its window size doesn't match the attached client
3. **Resize escape codes leaking** — `^[[8;51;148t` sequences visible in terminal output
4. **Splash screen broken** — tmux reattach suppressed the welcome/coda screen
5. **Double resize management** — xterm.js resizes the PTY, but tmux has its own window size logic, causing conflicts
6. **Keybinding conflicts** — tmux's prefix key (Ctrl-B) can interfere with CLI tools like Claude Code

## Why Tmux Doesn't Solve the Real Problem

The main persistence need is surviving **Databricks Apps container restarts**. On container restart:
- All processes die, including the tmux server
- The filesystem is recreated from the deployment artifact
- Only `/Workspace/` files survive

Tmux helps in two scenarios where the **container stays alive but sessions disconnect**:

1. **Browser tab close/refresh** — user accidentally closes tab while Claude Code is mid-task
2. **Gunicorn worker restart** — `timeout = 30` in gunicorn.conf.py means any request >30s causes gunicorn to SIGKILL the worker and spawn a new one. This is NOT rare during heavy setup or long-running requests.

**Risk of removing tmux:** In scenario 1, without tmux (or an equivalent), a running Claude Code session is orphaned and killed after the session timeout. This is a real user pain point — losing a 10-minute coding task because of an accidental tab close.

**Mitigation:** localStorage-based session recovery (see below) addresses scenario 1 without tmux's visual baggage. Scenario 2 remains a gap — if gunicorn kills the worker, PTY FDs are gone and no application-level trick can recover them. Tmux genuinely solves this; our approach does not.

**Accepted risk:** We accept the gunicorn worker restart gap because (a) it requires a request to exceed 30s which is uncommon during normal terminal use, and (b) the visual/UX cost of tmux outweighs the protection it provides for this edge case.

## What About David's state_sync.py?

`state_sync.py` persists two things to `/Workspace/Users/{email}/.state/` every 5 min:
1. `~/.claude/projects/*/memory/` — Claude Code auto-memory
2. `~/.bash_history` — shell history

### Honest assessment

**Claude auto-memory IS valuable.** CLAUDE.md covers project-level instructions, but auto-memory accumulates session-specific learnings: "tried approach X, failed because Y", user preferences discovered during conversation, debugging insights. These can't be replicated by CLAUDE.md alone and are lost on every container restart without state_sync.

**Shell history has low value** in this context — users mostly interact via AI agents, not manual shell commands.

**Verdict on state_sync:** Worth adopting in a future PR for the auto-memory persistence alone. Not blocking for the current multi-tab work, but genuinely useful. We were too dismissive initially.

## What Already Works (and Gaps)

- **Post-commit hook** (`sync_to_workspace.py`): syncs `~/projects/*` repos to Workspace on every git commit. **Gap:** only committed code survives — uncommitted WIP is lost.
- **`GIT_REPOS` env var**: auto-clones repos on startup, so code is restored
- **Web Worker polling**: handles browser background/foreground transitions without session loss
- **5-minute session timeout**: keeps orphaned PTY sessions alive long enough for tab-refresh reconnection. **Gap:** if user steps away longer than 5 min, session is killed.

## Recommended Approach: localStorage Session Recovery

Instead of tmux, store session IDs in `localStorage`. On page load:

```
1. Check localStorage for previous session_id
2. POST /api/output with old session_id
3. If responds → reattach xterm.js to existing PTY
4. If 404 → create new session
```

Benefits:
- Running processes survive tab close/refresh (same as tmux)
- No visual artifacts (no status bar, no dot fill, no resize conflicts)
- Splash screen works normally on new sessions
- Zero extra dependencies

Trade-offs vs tmux:
- **No scrollback replay** — xterm.js buffer is lost on refresh, user sees blank terminal attached to a running process. Tmux replays the visible screen (~24-50 rows), which is meaningfully better.
- **No gunicorn worker crash recovery** — if gunicorn kills the worker, PTY FDs are gone. Tmux survives this; localStorage recovery does not.
- **Simpler, lighter, no visual bugs** — the trade-off we're choosing to make.

## What to Keep from feat/multi-tab-terminals

- `/api/output-batch` endpoint — single request for N panes instead of N requests
- Web Worker batch polling rewrite — background-throttle-immune
- Security response headers (X-Content-Type-Options, X-Frame-Options, etc.)
- SIGTERM handler fix (don't register at module level, only in gunicorn)

## What to Strip

- tmux session wrapping in `create_session()`
- `/api/tmux-sessions` endpoint
- tmux install step in `run_setup()`
- `checkTmuxSessions()` in frontend
- Reattach/splash suppression logic
- `pane_id` parameter (only needed for tmux session naming)
