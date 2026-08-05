# /speckit.plan — CoDA Workshop Delivery Platform

**Plans:** `spec-A-coda-workshop-layer.md` (spec B dropped).
**Status:** Draft, decisions resolved, **no blocking gates.** O3 = CoDA-terminal / Claude Code (Omnigent out, single codebase). Sizing decided by over-provisioning: **6 apps × LARGE × `MAX_CONCURRENT_SESSIONS=10`** (assumed, unmeasured, +1-app cushion) — capacity validated by a day-of smoke test, not a pre-build gate. Git identity = attendees' own GitHub against a public repo; challenge repo preloaded (baked, pinned).
**Foundation (already built & verified, not re-planned here):** 3-wheel install fix, Codex Responses-API fix (`e63705d`), `grant_omnigent_host.sh` (`4117432`). Note: the boot-time host auto-register (`3fe77a2`) is **deliberately NOT used** by workshop instances (spec A-R3).

---

## 1. Architecture

### Recommended branch — "CoDA-terminal" (assumes O3 = drive-agent-live→PR)

Drop the Omnigent *host* indirection. Each attendee uses a CoDA app's **own web terminal** — already a live interactive coding-agent UI. This makes constraint C1 (no host-sharing) irrelevant, because we never share an Omnigent host; access is gated at the Databricks App layer (C4).

```
~50 attendees, 6 groups   →   App CAN_USE grants (per group)   →   6 CoDA apps · LARGE · cap=10
  ~8–9 per group                the load-distribution + access lever      coding-agents-01..06
                                                                          each: own FS/container
                                                                          preloaded challenge repo (pinned)
                                                                                    ↓
                                                              attendee auths OWN GitHub → PR on own branch
                                                                          (public repo)
```

- **Isolation (R1, R6):** one container per app → separate filesystem, process space, memory budget. Blast radius = one group (~8–9 people).
- **Access + load control (R4):** `PUT /api/2.0/permissions/apps/{app}` grants group-A `CAN_USE` on `coding-agents-01`, etc. The SSO proxy enforces per-user (C4, C7). *We* choose the group→app mapping — that IS the load balancer Omnigent doesn't provide (C2).
- **Identity (R3):** **no shared secret** — each attendee runs `gh auth login` in-terminal (their own GitHub), raises per-author PRs against the public repo.
- **Preload (R7):** the pinned challenge repo is baked into each image; the terminal opens into it.
- **Fleet size:** **6 apps** = `ceil(50/10)=5` + 1 cushion. The `10` is an over-provision *assumption* (LARGE headroom), validated day-of by smoke test — **not** measured pre-build.

### (Alternative Omnigent-orchestration branch — RETIRED)
O3 resolved to CoDA-terminal, so the Polly branch and its Omnigent dependency (spec B) are dropped. Recorded in `spec-B-omnigent-host-sharing-DROPPED.md` for posterity. Omnigent does not appear in the workshop data path.

## 2. Data model

Minimal — this is provisioning state, not an application datastore.

| Entity | Fields | Where it lives |
|--------|--------|----------------|
| **Roster entry** | `attendee_id`, `attendee_email/handle`, `assigned_app`, `branch_prefix` | Facilitator-held (sheet / CSV / config) |
| **App instance** | `app_name` (`coding-agents-NN`), `compute_size`, `url`, `sp_client_id`, `status` | Databricks Apps API (source of truth) |
| **Group → app map** | `group_id` → `app_name`, `member_emails[]` | Roster config; realized as `CAN_USE` grants |
| **Challenge repo (preloaded)** | public URL, **pinned commit/tag**, in-container path | Baked into each instance at build (A-R7); source of truth is the public repo |
| **Attendee git identity** | attendee's own GitHub account | Authenticated live in-container via `gh auth login` (A-R2); not provisioned by us |
| **Per-attendee PR** | branch + PR under attendee's own account | The public repo (emergent output) |

No shared database, and **no shared secret** (attendees bring their own GitHub identity). State is: the roster (ours), the Apps API (fleet), the preloaded pinned repo (baked), and the public Git repo (output).

## 3. Interfaces

| Interface | API / mechanism | Used for |
|-----------|-----------------|----------|
| **Provision app** | `POST /api/2.0/apps` (or DAB / FEVM) | Stand up `coding-agents-NN` |
| **Deploy source** | `apps deploy` + workspace sync | Push CoDA + `app.yaml` (per-index) |
| **Grant access** | `PUT /api/2.0/permissions/apps/{app}` `CAN_USE` | Attendee-group → app (C4) |
| **Inject secret** | `app.yaml` `env valueFrom` → secret scope | Shared `gh` bot token (R3) |
| **Attendee entry** | App URL `https://coding-agents-NN-<id>.aws.databricksapps.com` | Live terminal (R2) |
| **Sizing knob** | `compute_size` at app-create | Trade N ↔ per-app capacity (O2) |
| **Teardown** | `DELETE /api/2.0/apps/{app}` | Post-workshop cleanup (R5) |
| **Git/PR** | `gh` CLI in-container (bot token) | Clone + PR (R3) |

Explicitly **not** used: any Omnigent `/v1/hosts/.../permissions` endpoint (C5 — doesn't exist).

## 4. Dependencies

- **Databricks Apps** on lakemeter (AWS) — provisioning, permissions, SSO proxy (C7).
- **A larger compute tier** *if* O2 says N should be reduced by scaling up (unconfirmed it exists).
- **Databricks secret scope** for the `gh` bot token.
- **A `gh` bot account** with PR rights on the target repo (O6).
- **Model serving endpoints** — Claude / Codex via the app SP (already wired; Codex fix `e63705d` must be in the deployed image).
- **CoDA image** — the current `feat/omnigents-host` tree; the workshop template forks/parameterizes it.
- **The target Git repo + the exercise content** — workshop-owned, external to this build.

## 5. Risks

| # | Risk | Likelihood | Impact | Mitigation |
|---|------|-----------|--------|------------|
| **RK1** | *N is wrong because per-app ceiling was guessed* (O1 unmeasured) | High if unresolved | Under-provision → attendees blocked mid-workshop | **Load-test milestone M1 is a hard gate.** Do not size the fleet from an estimate (S5). |
| **RK2** | No larger tier exists (O2) → can't "scale up," must scale *out* | Medium | More apps to manage | Fall back to more `MEDIUM` apps; budget provisioning time accordingly |
| **RK3** | Simultaneous N deploys hit boot-churn wedge ("did not start within 10 min") | Medium | Some apps dead at workshop start | Stagger deploys; pre-warm the fleet the day before; scripted stop→start recovery |
| **RK4** | ~50 attendees `gh auth login` in the first minutes → friction/support spike | Medium | Slow/uneven workshop start | Pre-installed `gh`; a clear one-line prompt in the terminal; facilitator fallback; pre-workshop check that attendees have GitHub accounts (A-O4) |
| **RK5** | Attendee reaches the wrong app (roster/URL hand-off) | Medium | Confusion, uneven load | Pre-assigned roster + a landing/redirect page (O4) |
| **RK6** | Preloaded repo goes **stale** — challenge changes after images are baked | Medium | Attendees start from wrong state | Pin a commit/tag; rebuild the fleet if the challenge changes; freeze the challenge before bake (A-O3) |
| **RK7** | Public repo + 50 real forks/PRs → noise, or attendees lack GitHub accounts | Low | Some can't PR | Public repo removes access blocker; confirm accounts pre-workshop; fork-based PRs keep the base repo clean |
| **RK8** | Cost of N medium apps for the workshop window | Known | Budget | Ephemeral: provision day-of, tear down after (R5); estimate from N once measured |

## 6. Implementation milestones

- **M1 — Template one CoDA workshop instance (start here — no gate).**
  Parameterize the CoDA tree by index; ship the workshop `app.yaml`: `compute_size: LARGE`, `MAX_CONCURRENT_SESSIONS: "10"`, and **omit** `OMNIGENTS_SERVER_URL` (A-R3, host-register OFF). **Bake the pinned challenge repo** into the image at the terminal's default cwd (A-R7); ensure `gh` is pre-installed. Confirm one instance end-to-end: open terminal → preloaded repo present → `gh auth login` as a test user → drive Claude Code → raise a PR. Also confirms `LARGE` is a valid tier (A-O5).

- **M2 — Provisioning + roster automation.**
  Script stand-up of the 6 apps (channel per O5), the 6 group→app `CAN_USE` grants (extend the pattern proven in `grant_omnigent_host.sh`), and teardown. Include staggering for RK3.

- **M3 — Smoke test the assumption (replaces the old measure-gate; day-of or dry-run).**
  On the first provisioned LARGE app, run ~8–10 concurrent Claude Code sessions and sanity-check RAM/latency. **This validates the over-provision assumption rather than gating the build.** If clearly tight → add apps or lower the cap before the workshop; if comfortable → proceed. (A-O1/A-O2.)

- **M4 — Fleet dry-run at scale.**
  Provision all 6, roster a test cohort, drive synthetic concurrent load matching ~50 attendees (including the `gh`-auth spike, RK4). Verify S1/S3/S5; exercise the recovery path (RK3).

- **M5 — Attendee hand-off + runbook.**
  Access path (O4), a one-page attendee guide (two steps: open your app URL → `gh auth login`), and a facilitator runbook (provision day-of, monitor, recover, tear down). Confirm attendees have GitHub accounts pre-workshop (RK7).

### Sequencing
```
M1 (template) → M2 (provision 6) → M3 (smoke-test assumption) → M4 (dry-run) → M5 (handoff)
```
**No blocking pre-build gate** — the capacity question is handled by over-provisioning (6×LARGE×10) and validated at M3, not measured before M1. The experience fork is resolved (CoDA-terminal).

## 7. What this plan deliberately does not do
- Does not extend Omnigent (host-sharing / load-balancing) — out of scope, and not our code (C1/C2/C5).
- Does not build per-attendee identity infrastructure (deliberate non-user).
- Does not re-plan the shipped CoDA↔Omnigent host integration — it's the verified foundation.
