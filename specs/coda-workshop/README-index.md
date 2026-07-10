# /speckit.specify — CoDA Workshop Delivery Platform (INDEX — SPLIT)

> **This combined spec has been split into two component specs** (per review, 2026-07-07):
> - **`spec-A-coda-workshop-layer.md`** — CoDA workshop-layer changes (our codebase). Needed on both branches.
> - **`spec-B-omnigent-host-sharing-DROPPED.md`** — Omnigent host-sharing (upstream OSS). **Conditional** — only if O3 = Polly, and likely avoidable even then.
>
> Correction captured in the split: "CoDA is sorted, no Omnigent changes" was an overclaim true only for the recommended CoDA-terminal branch. The CoDA *host integration* is shipped; the CoDA *workshop layer* is unbuilt (spec A). Omnigent changes are needed only on the Polly branch (spec B).
>
> The sections below are retained as the shared problem framing that both specs draw from.

**Feature:** Run CoDA (Coding Agents on Databricks Apps), fronted by Omnigent, for ~50 concurrent workshop attendees doing a live software-engineering workload.
**Status:** Superseded by A + B. Retained for shared context.
**Scope note:** The CoDA↔Omnigent *host integration* is already built and verified (commits `a636823`, `e63705d`, `3fe77a2`, `4117432`) and is treated here as an existing foundation, not part of this spec.

---

## 1. Problem

We want to run a hands-on workshop where ~50 attendees concurrently use always-on coding agents (Claude Code / Codex, via CoDA) to do real SWE work: **clone a Git repo, drive an agent live in a UI to make changes, and raise a PR.**

The naive approach — point everyone at one shared CoDA host through Omnigent — does not work. The research session established three platform facts that force a different architecture:

- **Omnigent hosts are single-owner with no share mechanism** (verified: `403 "not your host"` on every host route; `PUT /v1/hosts/{id}/permissions/{user}` → `405`; absent in the deployed build *and* latest upstream `main`). A host cannot be shared to a cohort, a group, or the public.
- **Omnigent does not load-balance across hosts** (verified: session creation takes a caller-supplied `host_id`; no pool, no round-robin). Distribution of attendees to compute must be arranged by us.
- **A single container cannot hold 50 live agents** (asserted, *not yet measured*). Each Omnigent runner is a real `claude`/`codex` subprocess; the app runs `compute_size: MEDIUM`, whose exact RAM was not confirmed. The real per-container ceiling is an open question (§7), not a known number.

The problem this spec addresses: **deliver an isolated, live, interactive coding-agent environment to each of ~50 attendees, with deliberate load distribution and delegated access control, using platform capabilities that actually exist.**

## 2. Users

| User | Role in the workshop | What they need |
|------|---------------------|----------------|
| **Attendee** (~50) | Does the SWE exercise | A live agent UI they can reach; an isolated workspace; ability to clone the repo and raise a PR without stepping on others |
| **Facilitator** (you / FE team) | Runs the workshop | Control over who lands on which environment (load spread); ability to provision and tear down N environments quickly; a way to hand each attendee their access |
| **Repo owner** | Owns the target Git repo | Attributable PRs (branch-per-attendee), a single well-scoped bot identity rather than 50 human credentials |

**Deliberate non-user:** per-attendee *Databricks/Omnigent human identity*. The workload is a shared-identity SWE task; attendees do **not** need individual SSO principals. Isolation is provided by separate containers, not separate identities.

## 3. Requirements

### Functional
- **R1 — Isolated environment per attendee.** Each attendee gets a filesystem and process space not shared with others (own clone, own branch namespace, no cross-visibility of files).
- **R2 — Live interactive UI.** An attendee can watch and steer a coding agent in real time (not a fire-and-forget batch job).
- **R3 — Shared git identity.** All agent-produced PRs are raised by one workshop bot (`gh` token), on per-attendee branches (`attendee-N/…`). No per-human SSO.
- **R4 — Delegated access control.** The facilitator can assign specific attendees (or groups) to specific environments and control that assignment — this is the load-distribution mechanism.
- **R5 — Repeatable provisioning.** N environments can be stood up from one templated definition and torn down cleanly after the workshop.
- **R6 — Bounded blast radius.** One attendee exhausting or crashing their environment must not take down others'.

### Non-functional
- **N1 — Capacity:** the platform must sustain ~50 concurrent live agents across the fleet with acceptable latency. The per-environment ceiling that sets fleet size *N* is unmeasured (§7, O1).
- **N2 — Cost/effort:** provisioning ~N apps and rostering ~50 people must be tractable for a small FE team in workshop-prep time.
- **N3 — Recoverability:** an environment that wedges (per the known apps-redeploy-churn boot issue) can be restarted without re-provisioning the fleet.

## 4. Constraints (verified platform facts)

These are not design choices; they are established limits the design must obey.

- **C1** Omnigent hosts are **single-owner; no share/permissions API** at any version. *(verified: live OpenAPI + source on deployed `0.5.0.dev0` and upstream `main`)*
- **C2** Omnigent performs **no load balancing**; the caller supplies `host_id`. *(verified: schema + routes)*
- **C3** CoDA's `MAX_CONCURRENT_SESSIONS` (default 5) governs CoDA's **own browser-terminal PTYs**, not Omnigent runners; Omnigent runners have no CoDA-side cap. *(verified: `app.py`)*
- **C4** Databricks App permissions **do** support per-user/group `CAN_USE` (`PUT /api/2.0/permissions/apps/{app}`), enforced by the SSO proxy. This is the only real "delegate access" lever. *(verified: docs + API)*
- **C5** CoDA's `share_and_launch` / auto-share feature (`a636823`) targets a **non-existent** server endpoint and must not be relied on. *(verified: would 405)*
- **C6** The app runs on a **fixed named compute tier** (`MEDIUM`); scaling up depends on a larger tier existing (unconfirmed, §7 O2).
- **C7** CoDA's single-user auth model relies on the Databricks Apps SSO proxy being authoritative on `X-Forwarded-Email`. *(verified in prior work, Azure; AWS assumed-equivalent.)*

## 5. In scope

- A templated, parameterized CoDA app definition deployable N× on the lakemeter workspace.
- A shared workshop `gh`/git bot credential baked into every environment.
- A roster + `CAN_USE` grant mechanism mapping attendee groups → apps (load distribution).
- The attendee-facing access path (which URL each attendee opens).
- A tear-down path.

## 6. Out of scope

- Building host-sharing into Omnigent (doesn't exist; not our code to change here).
- Per-attendee human identities / SSO provisioning.
- The Omnigent multi-agent orchestration UI (Polly, session picker) — **conditionally out** (see O3): included only if the "watch Polly orchestrate" experience is chosen, which changes the architecture.
- Changes to the already-shipped CoDA↔Omnigent host integration.
- The workshop *content* (the repo, the exercise itself).

## 7. Open questions (must resolve before committing the design)

- **O1 — Per-environment capacity ceiling (blocking sizing).** How many concurrent live agents does one `MEDIUM` CoDA app actually sustain before latency degrades or it OOMs? **Never measured.** Sets *N* (≈ 50 ÷ ceiling). Resolve by load test against the live `coding-agents` host.
- **O2 — Compute tier headroom.** What RAM/CPU does `MEDIUM` provide, and does a larger tier exist (to trade *N* down for bigger apps)? Not confirmed in docs checked.
- **O3 — Experience fork (blocking architecture).** Is the workshop's value *"drive a coding agent live → PR"* (→ use each CoDA app's **own web terminal**, drop the Omnigent host layer, and C1's wall disappears) **or** *"watch Polly orchestrate a team of agents"* (→ Omnigent is required, and each attendee needs a distinct Omnigent identity owning their own host, which is far heavier)? This single answer determines the entire architecture.
- **O4 — Access hand-off.** How does attendee-N learn/reach their assigned environment URL — pre-shared roster, a landing page, a redirector?
- **O5 — Provisioning channel.** Templated Databricks Asset Bundle vs. FEVM addon vs. scripted `apps create` loop — which is fastest/most reliable for ~N deploys and teardown?
- **O6 — Git bot scope.** Single bot token for all 50, or a small pool? Rate limits / PR-spam considerations on the target repo.
- **O7 — Boot-churn risk.** N near-simultaneous app deploys may hit the known "did not start within 10 min" churn wedge; does provisioning need to be staggered?

## 8. Success criteria

- **S1** ~50 attendees each reach an isolated, live coding-agent UI concurrently, within workshop tolerances for latency.
- **S2** Each attendee can clone the repo, drive an agent, and produce a PR on their own branch, attributed to the workshop bot.
- **S3** No attendee's activity degrades another's environment (R6/N1 verified under realistic load).
- **S4** The facilitator provisioned the fleet and rostered attendees within prep-time budget, and can tear it down afterward.
- **S5** *N* was chosen from a **measured** per-environment ceiling (O1 resolved), not an estimate.
