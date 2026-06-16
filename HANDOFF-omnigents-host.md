# Handoff — CoDA ↔ Omnigent host integration

**Date:** 2026-06-16 · **Branch:** `feat/omnigents-host` · **Author of this handoff:** prior session
**Repo:** `~/Repos/coding-agents-databricks-apps` · **Workspace profile:** `daveok`
**Tracking:** FEIP-7646 (child of FEIP-2996)

> Read this with `memory/project_coda_omnigents_host.md` — it has the full hard-won detail.
> This file is the "where we are / what's next" summary.

---

## TL;DR

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
