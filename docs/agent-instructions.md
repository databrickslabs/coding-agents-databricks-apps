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
`/Workspace/Shared/coda/{app-name}/{repo}/` (see `sync_to_workspace.py` /
`utils.workspace_sync_dest`). The path is keyed on the instance name so
same-identity instances (shared-app fleets) don't clobber each other's sync-back.
Nothing that isn't committed survives a recycle.

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

CoDA is a **Flask + Flask-SocketIO web app** that runs coding agents in a
browser terminal, wired to a Databricks workspace via the AI Gateway. It deploys
as a Databricks App. Full architecture, endpoints, env vars, and skills catalog
live in `README.md` — read it for feature/onboarding detail; this file is
operating rules only.

**Active agents** (as configured in `app.yaml`; gateway/workspace dependent):
Claude Code, Hermes Agent, OpenCode. Codex and Gemini are disabled when the
workspace serves no compatible gateway endpoints (see comments in `app.yaml`).

**Skills catalog** under `.claude/skills/` tracks the [ai-dev-kit](https://github.com/databricks-solutions/ai-dev-kit)
`databricks-skills/` upstream — refresh with the `refresh-databricks-skills`
skill (shallow-clones upstream, replaces the `databricks-*`/`spark-*` dirs,
preserves everything else). `databricks-app-apx` is **CoDA-specific** (the APX
FastAPI+React framework) with no upstream counterpart — it must survive a
refresh; the Superpowers workflow skills and `refresh-databricks-skills` itself
are likewise preserved.

Key runtime facts an agent should know:

- **Single gunicorn worker** — thread count is in `gunicorn.conf.py` (not
  hardcoded here). PTY state is process-local.
- Setup runs at boot in `app.py` (`initialize_app` / `_setup_git_config` /
  `setup_*.py`); agent CLIs install in parallel. `RUNNING` app status only
  means gunicorn bound the port — check `/api/setup-status` or boot logs before
  claiming setup-dependent features work.
- **Auth (layered):** with `ENABLE_SP_APIKEYHELPER=true`, the app keeps its SP
  client secret in-process and brokers short-lived OAuth tokens over loopback;
  the on-disk `omnigents-host` profile contains only the workspace host. Agents
  install without a paste. A short-lived
  PAT pasted on first terminal session is the **fallback** and **auto-rotates
  every 10 min** (`pat_rotator.py`). The rotator rewrites `~/.databrickscfg` on
  every rotation — a known failure mode was clobbering co-owned profiles (e.g.
  `omnigents-host`); fixed in `b5b11a6`, but any CLI call that must use
  the file profile should go through `databrickscfg_only_env()` (see `utils.py`).
- Databricks CLI: in the **container**, test with `databricks current-user me`.
  On a **local Mac**, use `databricks auth describe --profile <profile>`.

---

## 2. Working conventions

- **Keep changes focused.** One logical change per commit/PR. Don't let scope
  sprawl into unrelated refactors (see `CONTRIBUTING.md`).
- **Understand every line you submit.** Code review is the bottleneck — small,
  reviewable diffs merge faster.
- **Branch names** are descriptive: `feat/…`, `fix/…`.
- **Check which branch you're on** before concluding a flag or call-site is
  absent. A grep miss or reading a feature branch that excludes certain commits
  produces false "dead config" reports — read the source on the relevant branch.
- **Tests / verification:** if a change can't be unit-tested, include a manual
  test plan in the PR. Deploy and verify on a workspace before opening a PR —
  *unless* the change is static (HTML/CSS/comment) and the live app risks a
  boot-wedge (see §4); in that case defer to the next natural deploy or use
  `stop` → `start`. Typical verification: deploy → `databricks apps logs` (boot
  markers roll off the ~200-line tail quickly) → `tail ~/.sync.log`.
- Prefer the repo's existing tools: `uv` for Python, the `Makefile` targets for
  deploy/redeploy/status/cleanup. **Read a Makefile target's recipe before
  running it** — e.g. `redeploy-git` chains `grant-omnigent-host` before
  `deploy-git`.

### Deploy modes (no `databricks.yml` in this repo)

| Mode | Makefile targets | What happens |
|------|------------------|--------------|
| **Workspace sync** | `deploy`, `redeploy`, `deploy-workshop`, … | Local files sync to Workspace path, then `apps deploy` |
| **Git-linked** | `deploy-git`, `redeploy-git` | Workspace pulls directly from GitHub at deploy time |

Detect Git-linked mode in the app UI: `repo-git-form-group=link` in the overview
URL. Before any deploy: `databricks apps list --profile <profile>` — the
Makefile default `APP_NAME=coding-agents` may not match the live instance
(e.g. `coding-agents-2`, `coda`). Pass `APP_NAME=` explicitly.

**Apps overlays replace, they don't merge.** `make deploy-workshop` swaps
`app.yaml.workshop` in wholesale at deploy time — any env var in the base
`app.yaml` but absent from the overlay silently disappears from the deployed
container. Security-relevant settings the workshop box needs must live in the
overlay itself (e.g. `CODA_DISABLE_OWNER_CHECK`).

**Redeploy vs cold boot:** `apps deploy` restarts the process on the *same*
container — cached binaries and old files survive. `apps stop` + `apps start`
allocates a fresh host. Cold boot is what fleet instances experience; use it to
prove reproducible installs.

**Deploy sequencing:** `apps deploy` against a STOPPED app fails. Sequence:
`start` → wait for ACTIVE compute → `deploy`. For live-app code updates on a
running instance, prefer `stop` → `start` over rapid redeploys — rapid redeploys
of the live `coda` app can wedge platform boot.

### Git remotes (foot-guns)

- `origin` → public `databrickslabs/coding-agents-databricks-apps` (PRs target
  here). Push topic branches directly to `databrickslabs` for upstream PRs — fork
  PRs don't work from the private mirror.
- `private` → your approved private mirror (where the app may deploy from). A
  reflexive `git push origin HEAD` from a
  `private`-tracking branch publishes sensitive work to the public labs repo.
- Confirm tracking with `git rev-parse --abbrev-ref --symbolic-full-name @{u}`,
  not `remote.origin.url`.

---

## 3. Databricks auth notes (gotchas)

- **Layered auth in-container:** brokered SP OAuth at boot (`ENABLE_SP_APIKEYHELPER`) is
  tried first; pasted PAT is the fallback. These coexist by design — not
  "PAT or CLIENT_ID/SECRET, pick one."
- Ambient app-SP env vars can shadow a `~/.databrickscfg` profile. The sync uses
  `databrickscfg_only_env()` (see `utils.py`) to strip them — reuse that helper
  for any CLI call that must resolve to the file profile. If *any* CLI call
  misbehaves in this repo (not just login), check for stale SP env vars first.
- **`DEFAULT` profile PAT goes stale.** Named profiles (`<profile>`, `<dev-profile>`,
  etc.) often still work. Use `PROFILE=<name>` on Makefile targets, or
  `export DATABRICKS_CONFIG_PROFILE=<name>` for ad-hoc CLI sessions.
- **(Claude Code only)** **Unity AI Gateway ≠ first-party Anthropic API** for the
  same Claude Code env vars. Verify against `databricks/ucode` before applying
  Claude Code docs.
  Example: `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` strips the `context-1m`
  beta header on the raw API, but the gateway parses `[1m]` server-side off the
  model-id string — the two coexist. `[1m]` applies to opus/sonnet ≥4.6 only,
  never Haiku.
- **Empty env vars are not always "disabled."** `DATABRICKS_GATEWAY_HOST=""`
  was a no-op pre-fix because `if explicit:` treats `""` as falsy. When setting
  an env var to turn off a feature, trace the consuming code's truthiness check
  (`if value:` vs `if key in os.environ`) before committing.
- **SP creds are stripped from env at boot** after `initialize_app()` captures
  them for the Omnigent tunnel. From any fresh shell inside the container, those
  env vars are absent — only the running host process holds them in memory.

---

## 4. Recovery cheat-sheet

```bash
# See whether recent commits actually synced to Workspace
tail -n 20 ~/.sync.log

# Rehydrate a project from Workspace after a container recycle
# (container-internal path only — not usable from a local Mac shell)
python /app/python/source_code/restore_from_workspace.py <repo-name>
#   e.g. restore_from_workspace.py <private-mirror>

# Manually trigger a sync for the current repo (if a commit's sync failed)
python /app/python/source_code/sync_to_workspace.py "$(git rev-parse --show-toplevel)"

# Confirm app name before deploying
databricks apps list --profile <profile>

# Start stopped compute before deploy
databricks apps start <app-name> --profile <profile>
# wait for ACTIVE, then deploy

# Boot logs (check immediately — tail is ~200 lines)
databricks apps logs <app-name> --profile <profile>
```

**Known noisy boot warning (safe to ignore until wired):**
`error resolving resource challenge-repo-token ... not found` — the secret key is
`challenge-repo-read-token`.

**`grant-omnigent-host` silently fails** when no Omnigent server app exists on
the workspace (`databricks apps get-permissions omnigent` → "App does not
exist"; stderr swallowed). On workspaces without an Omnigent server, use
`make deploy-git` directly instead of `make redeploy-git`.

---

## 5. Omnigent host integration

`OMNIGENTS_SERVER_URL` is the on/off switch — empty/absent means host attach is
off. Before PRing branch code to `main`, ensure `OMNIGENTS_SERVER_URL`,
`OMNIGENTS_WHEEL_SPEC`, `OMNIGENTS_FORCE_REINSTALL`, and personal-workspace
values like `CLAUDE_CODE_OTEL_CATALOG_SCHEMA` are commented out or defaulted off.

**Liveness check:** use `GET /api/omnigents-status` on the CoDA app itself
(`stage=running` + `host_launched=True`). Do **not** use `/v1/hosts` as a health
check — it returns only hosts the *calling identity* owns. App-side `✓
Connected` is the correct client signal; absence from the host list is expected,
not a failure.

**Apps proxy identity:** the proxy injects `X-Forwarded-Email` for authenticated
browser sessions. Raw `curl` with a workspace PAT gets `302 → OIDC` — it cannot
verify container-internal state or exercise endpoints that read
`get_request_user()`. Use an authenticated browser session for those.

**Host-specific gotchas:**
- `omnigent host` has no `--profile` flag; use `DATABRICKS_CONFIG_PROFILE`.
- An SP-owned host is invisible in a human's personal picker unless shared via
  `PUT /v1/hosts/{id}/permissions/{user_id}` (owner-only call from the SP).
- Runner subprocess stdout goes to `~/.omnigent/logs/host-runner/`, not CoDA's
  app log.
- CoDA's host has SDK agents (Polly, Debby) configured; the native CLI harnesses
  (Claude Code, Codex, OpenCode, pi) all need `omnigent setup` to have run inside
  the container — it auto-adopts CoDA's ambient AI-Gateway creds into
  `~/.omnigent/config.yaml` for whatever harness the runner uses. CoDA runs it via
  `_run_setup_once()` in `omnigents_host.py`, decoupled from the interactive PAT
  bootstrap that gates `run_setup()`.
- The deployment-specific Omnigent server app may be STOPPED (reversible with
  `databricks apps start <server-app> --profile <profile>`).

**E2E terminal output:** xterm.js renders to `<canvas>` — browser accessibility
snapshots show garbage. Read CoDA app logs (`/api/logs`, `databricks apps logs`)
or redirect terminal output to a file instead.

---

*Maintainers: keep this file current. If you discover a new pitfall in this
ephemeral environment, add it to §0 or the relevant section above.*
