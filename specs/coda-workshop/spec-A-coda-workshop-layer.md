# /speckit.specify — (A) CoDA Workshop Layer

**Component:** CoDA (Coding Agents on Databricks Apps) — *our* codebase (`coding-agents`, branch `feat/omnigents-host`).
**Feature:** The changes CoDA needs to deliver an isolated, live coding-agent environment to each of ~50 workshop attendees.
**Companion spec:** (B) Omnigent Host-Sharing — **DROPPED.** O3 resolved to CoDA-terminal (Claude Code, not Polly), so Omnigent is out of the workshop path and no Omnigent changes are needed. Spec B is retired; see its header.
**Status:** Draft, decisions resolved. This is now the **sole** spec for the workshop. Corrects the earlier overclaim that "CoDA is sorted" — the *host integration* is shipped & verified; the *workshop delivery layer* below is unbuilt and is the whole remaining job.

## Resolved decisions (2026-07-07)
- **O3 (experience fork) → CoDA-terminal.** Claude Code driven in CoDA's own web terminal is enough; Polly/orchestration is not required. → No Omnigent changes; spec B dropped.
- **Git identity → attendees use their own GitHub identity** against a **public** challenge repo (no internal-access blocker). Per-author PRs.
- **Challenge repo → preloaded** into every instance (baked at build, pinned commit), so attendees start with it already present.
- **Fleet sizing → over-provision, don't measure-to-the-edge (deliberate).**
  **6 apps × `compute_size: LARGE` × `MAX_CONCURRENT_SESSIONS=10`.** Rationale: 50 ÷ 10 = 5 apps + 1 cushion (absorbs a wedged app or an optimistic 10-assumption → ~8–9 real load/app). **⚠️ The "10 sessions/LARGE" figure is an ASSUMPTION with a safety margin, NOT measured** — chosen to avoid a blocking load test before a one-day event, accepting some wasted headroom. The `MAX_CONCURRENT_SESSIONS` cap is a plain env var (`app.py:48`, default 5) → raising it to 10 is a one-line `app.yaml` change, no code change (confirmed).
- **Primary bar → the CoDA sessions must work** for ~50 concurrent attendees; identity plumbing is secondary.

---

## 1. Problem

CoDA today is a single-tenant app: one deployment, per-user PAT/SSO auth, and (as of `3fe77a2`) a boot-time Omnigent host auto-register. To run a ~50-attendee SWE workshop (clone repo → drive agent live → raise PR), CoDA needs to become **templatable into N isolated instances that share one git identity and gate access per attendee-group** — none of which exists yet.

**What is already sorted (verified, not in scope):** the CoDA↔Omnigent host integration — boot auto-register (`3fe77a2`), the 3-wheel install fix, the Codex Responses-API fix (`e63705d`), and `grant_omnigent_host.sh` (`4117432`). This spec builds on that; it does not re-open it.

## 2. Users
- **Attendee (~50):** opens their assigned CoDA app's own web terminal, drives an agent, raises a PR. No individual Databricks/Omnigent identity (deliberate non-user).
- **Facilitator:** provisions N instances, assigns groups to instances, tears down.
- **Repo owner:** wants attributable, branch-per-attendee PRs from one bot.

## 3. Requirements (CoDA-side)

### Functional
- **A-R1 — Templated deploy.** One parameterized CoDA definition deployable as `coding-agents-01..0N` with per-instance config (index, group, size). *(unbuilt)*
- **A-R2 — Attendee-owned git identity.** Attendees authenticate their **own** GitHub in their CoDA terminal (`gh auth login`, device-flow) against a **public** challenge repo, and raise per-author PRs. CoDA must make this auth path smooth (pre-installed `gh`, clear prompt). Distinct from CoDA's existing Databricks per-user PAT/SSO path. *(new — attendee-facing gh auth flow)*
- **A-R3 — Omnigent host-register OFF.** The boot-time host auto-register added in `3fe77a2` (`OMNIGENTS_SERVER_URL`) must be **disabled** for workshop instances — attendees use CoDA's own terminal, not the Omnigent host tunnel, so the tunnel is pure overhead/failure-surface. Gate it by env (omit `OMNIGENTS_SERVER_URL` in the workshop `app.yaml`, which already no-ops `start_host`). *(the `3fe77a2` change is counterproductive here; workshop template must ship without the server URL)*
- **A-R4 — Isolation per instance.** Each app = own container = own filesystem/process space (inherent to separate apps; verify no shared state leaks via workspace paths or secret scope).
- **A-R5 — Access delegation hook.** CoDA must work correctly when access is gated by Databricks App `CAN_USE` (per-group), relying on the SSO proxy (C7). No CoDA code change expected here, but must be verified under group-scoped access.
- **A-R6 — Clean teardown.** Instances removable without residual workspace/secret cruft.
- **A-R7 — Preloaded challenge repo.** Each instance ships with the public challenge repo **already present** (baked at build, pinned to a specific commit/tag so all ~50 attendees start from an identical state). Attendees do not clone cold at workshop-start. *(new — deterministic, instant start; no per-instance network dependency at boot)*

### Non-functional
- **A-N1 — Per-instance capacity:** the number of concurrent live agents one instance sustains is **unmeasured** (O1). CoDA imposes no runner cap; limit is container RAM.
- **A-N2 — Boot reliability at fleet scale:** N near-simultaneous deploys risk the known boot-churn wedge; instances must recover via stop→start without re-provisioning.

## 4. Constraints
- **A-C1** `MAX_CONCURRENT_SESSIONS` (`app.py:48`, env var, default 5) governs CoDA's *own* browser terminals — which on this branch **IS** the attendee UI. Set to `10` for the workshop. Raising it is config-only. *(verified)*
- **A-C2** CoDA's existing auth is per-user Databricks PAT/SSO; the attendee's own `gh` auth is an additive, different path (GitHub, not Databricks) — must not break the existing one. *(design constraint)*
- **A-C3** Workshop apps run `compute_size: LARGE` (up from the current MEDIUM). Exact LARGE RAM unconfirmed; the 10-sessions/LARGE figure is an over-provision **assumption**, not measured (A-O1). *(assumption + margin)*
- **A-C4** SSO proxy authoritative on `X-Forwarded-Email` (verified Azure; AWS assumed).

## 5. In scope
Templating/parameterization; shared git-bot auth path; boot-behavior toggle; per-instance isolation verification; teardown; verifying correct behavior under App `CAN_USE` gating; possibly raising `MAX_CONCURRENT_SESSIONS` for the terminal-as-UI branch.

## 6. Out of scope
- The already-shipped host integration.
- Any Omnigent server change (that's spec B, and only on the Polly branch).
- Per-attendee human identity.
- Workshop content.

## 7. Open questions (all non-blocking after the over-provision decision)
- **A-O1 — capacity assumption, now validated not gated.** Sizing is *assumed* (10/LARGE) with a 6th-app cushion rather than measured. Downgraded from a blocking gate to a **day-of smoke test**: bring up app-01, run a handful of concurrent Claude Code sessions, sanity-check RAM/latency before standing up the rest. If it's clearly tight, raise app count or lower the cap. *(deliberate: over-provision beats measure-to-the-edge for a one-day event)*
- **A-O2 — `MAX_CONCURRENT_SESSIONS` value.** Set to `10` per the sizing decision (config-only). The smoke test (A-O1) is what would tell us to dial it down.
- **A-O3 — preload mechanism detail.** Baked-at-build confirmed; open: exact in-container path the terminal opens into (so the repo is *right there*), and how the pinned commit is refreshed if the challenge changes pre-workshop (rebuild the fleet).
- **A-O4 — `gh` device-flow at scale.** ~50 attendees auth'ing GitHub in the first minutes = a friction/support spike. Not a blocker (public repo, they have accounts) but needs a smooth prompt + a facilitator fallback. Pre-workshop check: confirm attendees have GitHub accounts.
- **A-O5 — does `LARGE` exist / what RAM?** Confirm the tier name and headroom when provisioning (cheap — the `apps create` call either accepts `LARGE` or it doesn't).

*(Resolved & removed: the experience-fork question — O3 = CoDA-terminal. Omnigent is out; spec B dropped.)*

## 8. Success criteria
- **A-S1** N isolated CoDA instances provisioned from one template, each opening into the **preloaded** challenge repo, each able to drive Claude Code and raise a PR under the attendee's own GitHub identity.
- **A-S2** Attendee-group `CAN_USE` gating verified: group-A reaches app-01 and not app-02.
- **A-S3** Workshop instance runs cleanly with Omnigent host-register OFF (no `OMNIGENTS_SERVER_URL`).
- **A-S4** N derived from a **measured** ceiling (A-O1), not an estimate.
- **A-S5** A cold-start attendee reaches a working Claude Code session in the preloaded repo within workshop-start tolerance (no manual clone, minimal gh-auth friction).
