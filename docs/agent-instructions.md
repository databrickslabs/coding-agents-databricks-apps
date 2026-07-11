# Agent instructions — Coding Agents on Databricks Apps (CoDA)

This is the **single source of truth** for every AI coding agent working in this
repo (Claude Code, Codex, Gemini CLI, Hermes Agent, OpenCode). `CLAUDE.md`,
`GEMINI.md`, and `AGENTS.md` (at the repo root) are thin pointers to this file —
edit guidance here, never in the stubs, so they can never drift.

> Note: this doc lives at `docs/agent-instructions.md` (tracked + synced), **not**
> `.agents/`, because `.agents/` is gitignored — it's a runtime-generated Codex
> skills dir written by `setup_codex.py`. Anything under it would not be
> committed, and in this ephemeral env uncommitted == lost.

---

## 0. This environment is ephemeral — READ THIS FIRST

CoDA runs inside a **Databricks App container whose disk can vanish at any
time** (redeploy, restart, platform recycle, session timeout). Treat local disk
as scratch space, not storage.

**The only durable backup is a git commit.** A `post-commit` hook auto-syncs
every repo under `~/projects/` to Databricks Workspace at
`/Workspace/Users/{you}/projects/{repo}/` (see `sync_to_workspace.py`). Nothing
that isn't committed survives a recycle.

Therefore, non-negotiable operating rules:

1. **Commit small and commit often.** After every self-contained change — a
   working function, a passing test, a fixed bug — commit. Do not batch a
   session's worth of work into one commit; a recycle mid-session loses all of
   it. Aim for commits you'd be comfortable losing *nothing* before.
2. **A commit == a backup.** The commit is what triggers the workspace sync. If
   you haven't committed, your work is not backed up. "I'll commit at the end"
   is how work gets lost here.
3. **Verify the sync actually happened.** After committing, the hook logs to
   `~/.sync.log`. A healthy line looks like `✓ Synced to /Workspace/...`. If you
   see `⚠ Sync failed`, fix it before continuing — an unsynced commit is not a
   backup. Tail it with `tail ~/.sync.log`.
4. **After a recycle, restore before you start.** If the container was recycled
   and your project directory is missing or stale, rehydrate it from Workspace
   with `restore_from_workspace.py` (the inverse of the sync) *before* doing new
   work — don't rebuild from memory.
5. **NEVER move or import the `.git` folder into the Workspace.** If you run
   `databricks workspace import`, exclude `.git`. Moving it corrupts the repo and
   breaks the sync/restore round-trip. This is the one rule that has bitten
   people repeatedly.

---

## 1. What this repo is

CoDA is a **Flask + Flask-SocketIO web app** that runs five coding agents
(Claude Code, Codex, Gemini CLI, Hermes Agent, OpenCode) in a browser terminal,
wired to a Databricks workspace via the AI Gateway. It deploys as a Databricks
App. Full architecture, endpoints, env vars, and skills catalog live in
`README.md` — read it for feature/onboarding detail; this file is operating
rules only.

Key runtime facts an agent should know:

- **Single gunicorn worker**, 16 gthread threads — PTY state is process-local.
- Setup runs at boot in `app.py` (`initialize_app` / `_setup_git_config` /
  `setup_*.py`); agent CLIs install in parallel.
- Auth: single-user app owned by the app service principal. A short-lived PAT is
  pasted on first terminal session and **auto-rotates every 10 min**
  (`pat_rotator.py`). Nothing is persisted across restarts by design.
- Databricks CLI is pre-configured; test with `databricks current-user me`.

---

## 2. Working conventions

- **Keep changes focused.** One logical change per commit/PR. Don't let scope
  sprawl into unrelated refactors (see `CONTRIBUTING.md`).
- **Understand every line you submit.** Code review is the bottleneck — small,
  reviewable diffs merge faster.
- **Branch names** are descriptive: `feat/…`, `fix/…`.
- **Tests / verification:** if a change can't be unit-tested, include a manual
  test plan (screenshots/video) in the PR. Deploy and verify on a workspace
  before opening a PR.
- Prefer the repo's existing tools: `uv` for Python, the `Makefile` targets for
  deploy/redeploy/status/cleanup.

---

## 3. Databricks auth notes (gotchas)

- Authenticate with a **PAT** *or* a `CLIENT_ID`/`CLIENT_SECRET` pair — not both.
  If login misbehaves, unset `DATABRICKS_CLIENT_ID` and
  `DATABRICKS_CLIENT_SECRET` and retry so access is based purely on the owner's
  credentials.
- Ambient app-SP env vars can shadow a `~/.databrickscfg` profile. The sync uses
  `databrickscfg_only_env()` (see `utils.py`) to strip them — reuse that helper
  for any CLI call that must resolve to the file profile.

---

## 4. Recovery cheat-sheet

```bash
# See whether recent commits actually synced to Workspace
tail -n 20 ~/.sync.log

# Rehydrate a project from Workspace after a container recycle
python /app/python/source_code/restore_from_workspace.py <repo-name>
#   e.g. restore_from_workspace.py coding-agents-databricks-apps-private

# Manually trigger a sync for the current repo (if a commit's sync failed)
python /app/python/source_code/sync_to_workspace.py "$(git rev-parse --show-toplevel)"
```

---

*Maintainers: keep this file current. If you discover a new pitfall in this
ephemeral environment, add it to §0.*
