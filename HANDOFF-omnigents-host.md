# Handoff — CoDA ↔ Omnigent host integration

**Date:** 2026-06-16 · **Branch:** `feat/omnigents-host` · **Author of this handoff:** prior session
**Repo:** `~/Repos/coding-agents-databricks-apps` · **Workspace profile:** `daveok`
**Tracking:** FEIP-7646 (child of FEIP-2996)

> Read this with `memory/project_coda_omnigents_host.md` — it has the full hard-won detail.
> This file is the "where we are / what's next" summary.

---

## UPDATE 2026-06-17 — runner connects; native harness blocked on bwrap (full chain peeled)

The original `runner_failed_to_start` / OIDC-redirect error is **FIXED**. Peeling it exposed a
chain of independent issues; 3 are fixed (all CoDA-side), the last is the bwrap sandbox.

**FIXED (committed, deployed, verified live):**
1. **`b5b11a6`** — `pat_rotator._write_databrickscfg` rewrote `~/.databrickscfg` in `"w"` mode with
   only `[DEFAULT]` every 10 min, **clobbering the `[omnigents-host]` OAuth profile**. Host kept
   working (SDK cached the token in-process) but each fresh runner re-read the PAT-only file →
   profile missing → unauthenticated tunnel → 302 OIDC → `runner_failed_to_start`. Fix: preserve
   non-DEFAULT sections. Test `test_preserves_other_profiles`.
2. **`90e78b6`** — `_materialize_spec` built a `WorkspaceClient` from SP creds without pinning
   `auth_type` → SDK also saw the ambient PAT → "more than one authorization method configured".
   Fix: `auth_type="oauth-m2m"`. (Surfaces only on a fresh container.)
3. **`71f1860` + `71fe22a`** — installed static `tmux` (install_tmux.sh; mjakob-gh/build-static-tmux)
   in both `run_setup()` and the host-connect path (`_ensure_tmux`, before the readiness probe).
   This + the `claude` CLI (installed by `setup_claude.py` in `run_setup()`) clears the
   "Claude Code isn't configured on coda" banner. **Banner = `shutil.which("claude")`, NOT tmux**
   (harness_readiness.py → harness_cli_installed) — tmux is needed at *runtime*, not for the banner.

**Verified live:** Polly (claude-sdk) AND native Claude Code sessions both spawn a runner that
connects (host log: runner started, 0 tunnel failures). The error class moved past auth entirely.

**REMAINING BLOCKER — native terminal needs `bwrap` (sandbox):**
Native Claude Code now fails with `native_terminal_start_failed`. Runner log root cause:
```
OSError: linux_bwrap sandbox requires the 'bwrap' binary on PATH ...
  or set os_env.sandbox.type to 'none' to disable sandboxing.
(omnigent/inner/bwrap_sandbox.py:233, via _auto_create_claude_terminal → launch_terminal)
```
- The native terminal wraps the agent in a **bubblewrap sandbox** (default `linux_bwrap`). CoDA's
  container has no `bwrap` and no root (`apt`/`apt-get` exist at /usr/bin but can't install without
  root). **Confirmed `unshare --user --map-root-user echo` → NS_OK**, so unprivileged userns IS
  allowed → a vendored static `bwrap` WOULD run.
- A **session-level `enforce_sandbox: none` policy does NOT fix it**: tried live (pol_… added then
  deleted). The native terminal's `effective_os_env_spec` comes from the **agent spec's `os_env`**
  (default linux_bwrap), bypassing the start-policy verdict (which only rewrites the harness spec,
  not the auto-created claude terminal). The `claude-sdk`/Polly harness defaults to `sandbox=none`
  and needs NO bwrap (and no tmux) — its only blocker is the separate server-side spec-404.

**Three ways to land the native harness (pick later):**
1. **Vendor a static `bwrap`** (musl + static libcap, linux-amd64) into the repo, install to
   `~/.local/bin` like tmux. userns confirmed working. Most effort; upstream ships only source.
2. **Server-side**: set `os_env.sandbox.type: none` (or `allow_sandbox_override: true`) on the
   `omnigents-daveok` built-in "Claude Code" agent spec, OR have a server admin add a server-wide
   `enforce_sandbox(sandbox_type="none")` default policy (`POST /v1/policies` is admin-gated; my
   user OAuth got `forbidden`; admin = file roster in the server's data dir per `admin_list.py`).
3. **Pivot to claude-sdk/Polly** (sandbox=none, no bwrap/tmux): fix the server-side agent-spec-404.

Note: the host connects via SP OAuth creds captured at startup, **independent of the terminal PAT
gate** — `POST /api/omnigent-host/connect` works before any PAT is pasted. But `run_setup()`
(installs claude+tmux) IS gated behind the PAT bootstrap.

## TL;DR (original, pre-fix)

CoDA-as-an-Omnigent-host **works end-to-end through runner-launch-in-container**: the CoDA
app connects to the Omnigent server as a host, authenticates (app-SP OAuth), is shareable to a
human user, appears in their Web-UI host picker, accepts a session, and **spawns a runner inside
the CoDA container**. What does NOT yet work is an agent session *completing* — blocked by two
independent issues (below). Neither is a flaw in CoDA's host transport.

The original "NOT SP-owned, user brings own identity" goal was proven **architecturally
infeasible** from a CoDA container (3 source paths; see memory). We pivoted to **SP-owned + shared
to the user**, which works.

---

## What's DONE (committed on `feat/omnigents-host`, all deployed to live `coda`)

| Commit | What |
|--------|------|
| `115d3a4` | UI note in Omnigent Host panel: host attaches as the app SP, only visible in your picker if shared |
| `50784c2` | `/api/omnigent-host/share` endpoint (owner-gated) — mints SP OAuth, calls `PUT /v1/hosts/{id}/permissions/{user}` (grant `use`) + optional `POST /runners`. Helper `share_and_launch` in `omnigents_host.py`. Tests in `tests/test_omnigents_host_api.py` |
| `4e8ab13` | `_run_setup_once()` in `omnigents_host.py` — runs `omnigent setup` (feeds `q`) after connect to adopt ambient creds. **NOTE: this does NOT fix the native harness — see blocker #1. Keep it (harmless) or reconsider.** |

Runtime user-pull flow (URL input → Connect → host attaches, no redeploy) was already built in
prior commits and is live. 26 tests pass: `uv run pytest tests/test_omnigents_host.py tests/test_omnigents_host_api.py -q`

Deploy method (NOT `make deploy` — that defaults APP_NAME wrong):
```bash
export DATABRICKS_CONFIG_PROFILE=daveok
WS="/Workspace/Users/david.okeeffe@databricks.com/apps/coda"
databricks sync . "$WS" --watch=false --profile daveok
databricks apps deploy coda --source-code-path "$WS" --profile daveok --no-wait
```
One clean deploy worked fine (~12s, no boot-wedge) — the churn-wedge risk had settled.

---

## TWO OPEN BLOCKERS (keep them separate — they are independent)

### Blocker #1 — native Claude/Codex harness needs `tmux` (CoDA-side fix)
The host's own setup output says it plainly:
> ⚠ tmux not found on PATH — `omnigent claude` and `omnigent codex` launch the agent through a
> local tmux terminal and **refuse to start without it**. The pure-Python openai-agents harness
> runs without these.

- `claude` CLI **is** installed (v2.1.178, `/app/python/source_code/.local/bin/claude`).
- `claude-native` reports "not configured" because the native harness needs **tmux**, which CoDA
  doesn't ship. (Confirmed in OSS `onboarding/harness_readiness.py`.)
- **`omnigent setup` does NOT fix this** — my `4e8ab13` setup-step was aimed here but it only
  adopts credentials; it doesn't install tmux.
- **FIX:** install `tmux` in the CoDA container, on PATH, before/at host connect. Databricks Apps
  containers have **no apt** — so likely vendor a static `tmux` binary or deliver it via a method
  CoDA already uses for system tooling. Then native Claude works (modulo blocker #2).

### Blocker #2 — agent-spec 404 → bogus harness fallback (server-side, DOMINANT)
From the in-container runner log (`~/.omnigent/logs/host-runner/runner-*.log`):
```
runner connected to wss://.../tunnel            ✓ (auth + transport fine)
GET /v1/sessions/{conv}/agent/contents → 404    ✗ missing agent spec
spec_resolver: 404 for missing agent
harness spawn failed: unknown harness 'runner-test-default';
  registered: [claude, claude-native, claude-sdk, codex, codex-native, databricks_supervisor, openai-agents, pi]
```
- The server falls back to a **test-only placeholder harness `runner-test-default`** (confirmed by
  comment in OSS `server/routes/sessions.py`) when the session's agent spec 404s.
- This kills **every** session regardless of harness — that's why Polly (SDK, needs no tmux) also
  showed `polly — Failed`.
- Likely cause: the **Polly/Debby example agent specs aren't deployed on the `omnigents-daveok`
  server build**. FIX is server-side (deploy the agent specs) OR start a session with an agent
  whose spec resolves to a registered harness (`databricks_supervisor` is registered).

**Verify-it-works sequence once both are fixed:** New Session in Omnigent Web UI → pick host
`coda` → pick a harness whose deps are met → ask agent to write a file → confirm it lands in
CoDA's container.

---

## Cleanup / hygiene owed
- **Live PAT to revoke:** `bea9756ceb8e1656e8a00e454985784e6d239e7cdffc93cac85f22cf6ea9e9f4`
  (comment `coda-auto-rotated`) — this is the CoDA-rotated token derived from the PAT pasted into
  the terminal for log-debugging. It's CoDA's *active* terminal credential; revoke when done with
  the terminal: `databricks tokens delete <id> --profile daveok`. (Earlier prior-session ~90-day
  PAT was already revoked.)
- **`omnigents-daveok` app:** currently **RUNNING** (restarted this session for testing). To halt
  metering: `databricks apps stop omnigents-daveok --profile daveok`. App + its Lakebase branch
  (`projects/omnigents-daveok/branches/production` on the SHARED `ot-demo-lakebase` CU_1) stay
  intact and restartable. **Do NOT delete** — it's the verified host server and the DB is shared.

---

## Key environment facts (so you don't re-derive them)
- coda app SP: `793257c7-63d3-464f-b6fb-3bc11880bf2d` (`app-2hnbfl coda`).
- Stable host_id (deterministic): `host_30023c7760d5a8726abf9820d912b4e0`
  = `_stable_host_identity()` = sha256(`coda-omnigents-host:<SP client_id>`).
- Omnigent server runs **header auth mode** (default; accepts SP via proxy `X-Forwarded-Email`).
- OSS source is the authoritative ref: `github.com/omnigent-ai/omnigent` (Databricks open-sourced
  it 2026-06-13; same codebase as internal agent-framework). Use `gh api repos/omnigent-ai/omnigent/contents/<path> --jq .content | base64 -d` to read files (account: `david-okeeffe_data`).
- Host visibility = the identity on the tunnel at `omnigent host` launch; owner-scoped in
  `server/host_registry.py` + `auth.py`.
- CoDA terminal driving (e2e): mint short-lived PAT → paste into terminal `Token:` gate (user
  pastes, to keep secret out of agent context) → drive via chrome-devtools. xterm canvas is NOT in
  the a11y tree — read output via `take_screenshot`, and it can wedge (reload the tab to recover).

## Suggested first move for the fresh session
Pick ONE blocker. Blocker #2 (agent-spec 404) is dominant — no session completes without it — and
is server-side. Blocker #1 (tmux) is a clean, self-contained CoDA-side fix the user explicitly
asked to automate in the connect path. If continuing the user's last ask: **install tmux in the
container at connect, then re-verify `claude-native` flips to configured.**
