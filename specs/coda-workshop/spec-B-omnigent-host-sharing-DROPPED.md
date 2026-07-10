# /speckit.specify — (B) Omnigent Host Sharing / Multi-Tenant Hosts  — ❌ DROPPED

> **RETIRED 2026-07-07.** O3 resolved to **CoDA-terminal / Claude Code** (Polly not required).
> Omnigent is out of the workshop path entirely, so **no Omnigent changes are needed** and
> this spec is not being pursued. Kept as a decision record: it documents *why* host-sharing
> was considered and why it's unnecessary. If a future need for the Polly/orchestration UI at
> cohort scale arises, re-read §9 first — even then, per-attendee hosts likely avoid this work.

**Component:** Omnigent server — **upstream OSS** (`github.com/omnigent-ai/omnigent`), **not our codebase.**
**Feature:** Make an Omnigent host usable by principals other than its single owner.
**Status:** ❌ **DROPPED** — O3 = CoDA-terminal. Not in play. Retained for the record only.
**Ownership caveat:** This is a change to a third-party OSS project we do not control. "Spec" here means *the feature Omnigent would need to add (or that we'd contribute upstream)*; it is a dependency, not a task we can unilaterally schedule.

---

## 1. Problem

An Omnigent host is **strictly single-owner with no sharing mechanism** — verified against the deployed server AND latest upstream `main`:
- Every host route enforces `if host.owner != user_id → 403 "not your host"`.
- `GET /v1/hosts` is identity-scoped (owner sees only their own hosts).
- `PUT /v1/hosts/{id}/permissions/{user}` → `405` (route does not exist).
- No `__public__`/group/team grant for hosts (the public-grant machinery exists only for *conversations*, not hosts).

Consequently, a pool of shared CoDA hosts cannot be offered to ~50 attendees through Omnigent. To make the Polly-orchestration workshop experience viable at cohort scale, **Omnigent must gain a host-sharing / multi-tenant-host capability that does not exist at any version.**

*(Note: CoDA's own `share_and_launch` (`omnigents_host.py:648`) already calls the non-existent `PUT /v1/hosts/{id}/permissions/{user}` — it is dead code against real Omnigent. If B is built, that CoDA code becomes live; if B is not built, that CoDA code should be removed. See spec A.)*

## 2. Users
- **Facilitator:** wants to grant a cohort `use` on a small pool of shared CoDA hosts.
- **Attendee:** wants to see and select a shared host they don't own in the Omnigent UI.
- **Host owner (the CoDA app SP):** wants to delegate `use` without transferring ownership.

## 3. Requirements (Omnigent-side, aspirational upstream)
- **B-R1 — Host permission grant API.** A real `PUT /v1/hosts/{id}/permissions/{principal}` (+ GET/DELETE) accepting a level (`use`) for a user, group, or public sentinel. *(does not exist)*
- **B-R2 — Non-owner visibility.** `GET /v1/hosts` returns hosts the caller has been *granted* on, not only those they own. *(does not exist — currently owner-scoped)*
- **B-R3 — Non-owner session launch.** `POST /v1/hosts/{id}/runners` and session creation succeed for a granted non-owner (currently `403`). *(does not exist)*
- **B-R4 — Group/public grants.** Extend the conversation-level `__public__`/public-grant model to hosts, so a cohort can be granted in one call rather than 50. *(does not exist for hosts)*
- **B-R5 — Isolation between tenants on a shared host.** If multiple principals share one host, their sessions/filesystems must be isolated — otherwise sharing reintroduces the git-collision + cross-visibility problems that per-container isolation solved. **This is the hard part and may argue against shared hosts entirely.**

## 4. Constraints / reality
- **B-C1** This is third-party OSS; delivery is via upstream contribution or a maintained fork — timeline not under our control.
- **B-C2** Single-owner appears to be a **deliberate design stance** in Omnigent (consistent across every host route), not an oversight — so upstream may reject multi-tenant hosts on purpose.
- **B-C3** Even if B-R1–R4 land, **B-R5 (tenant isolation on a shared container) collides with the same RAM ceiling and FS-collision problems** that made the shared-single-host model fail in the first place. Sharing a host does not raise its capacity.

## 5. In scope (if pursued)
Host permission model + API; visibility change; non-owner launch authorization; group/public host grants; per-tenant isolation on a shared host.

## 6. Out of scope
- Anything on the CoDA-terminal branch (this spec doesn't apply).
- CoDA-side changes (spec A).

## 7. Open questions
- **B-O1 — Is this even the right solution?** Given B-C3, "shared hosts" may be strictly worse than "per-attendee hosts" (which need no Omnigent change). The per-attendee model — each attendee owns their own host via their own identity — sidesteps B entirely. **Strongly consider before committing to B.**
- **B-O2 — Upstream appetite.** Would Omnigent accept host-sharing, or is single-owner intentional (B-C2)? A fork is a maintenance burden (see prior air-gap fork experience).
- **B-O3 — Does the Polly experience actually require *shared* hosts?** Polly orchestration can run on a *per-attendee* host too. If so, B is unnecessary even on the Polly branch — you just need per-attendee Omnigent identities + hosts, not host-sharing.

## 8. Success criteria (if pursued)
- **B-S1** A cohort granted `use` on a shared CoDA host can see and launch sessions on it via the Omnigent UI.
- **B-S2** Tenant isolation verified (no cross-attendee FS/session visibility).
- **B-S3** Capacity still respected (B does not exceed the measured per-host ceiling).

## 9. Strong recommendation
**Do not pursue spec B unless (a) O3 = Polly AND (b) B-O3 confirms Polly genuinely needs *shared* (not per-attendee) hosts.** The per-attendee-identity model delivers the Polly experience with **zero Omnigent changes** — it only costs identity provisioning. B is a large, uncertain, third-party change that may be rejected upstream and that doesn't even solve the capacity problem (B-C3). This spec exists mainly to make that cost **visible** at the O3 decision point.
