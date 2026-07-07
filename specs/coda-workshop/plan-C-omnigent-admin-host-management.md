# /speckit.plan — (C) Omnigent Admin Host-Management Page

**Plans:** `spec-C-omnigent-admin-host-management.md`.
**Status:** Draft. **One hard prerequisite gates everything: a deployable Omnigent server we can modify** (fork or upstream + deploy access — C-O1). No omnigent repo is checked out locally yet.
**Codebase:** Omnigent (`github.com/omnigent-ai/omnigent`), server (`omnigent/server/…`) + web UI (`web/`). **Not CoDA.**
**Key leverage:** ~⅔ of this is exposing/admin-gating primitives that already exist (host registry, admin check, disconnect lifecycle). Only the permission-grant path is genuinely new.

---

## 1. Architecture

```
Admin (is_admin)  →  Admin Host page (web UI)  →  admin-scoped server API  →  Host Registry / HostStore (exists)
                          │                                                         │
                          ├─ Shut down  ─────────────→  host-tunnel disconnect path (exists: on_host_disconnect)
                          └─ Manage shares ─────────→  NEW host-permissions API  →  permission store (extend host scope)
                                                                                         │
Grantee  →  GET /v1/hosts (now returns granted hosts)  →  POST /hosts/{id}/runners (now allows non-owner) 
```

Three layers, mostly-existing → mostly-new:
- **Read (mostly exists):** host registry already tracks every connected host; today's `GET /v1/hosts` filters to owner. Add an admin-scoped listing (unfiltered) — small.
- **Shutdown (exists):** reuse `host_tunnel.py`'s disconnect/deregister path; add an admin/owner-triggered endpoint that invokes it — small–medium.
- **Share (new):** the missing `PUT/GET/DELETE /v1/hosts/{id}/permissions/{principal}`, plus threading a host-permission check through `GET /v1/hosts` (visibility) and `POST /hosts/{id}/runners` (launch authz) so a granted non-owner isn't `403`'d — the real work.

## 2. Data model
| Entity | Fields | Where |
|--------|--------|-------|
| **Host** (exists) | `host_id`, `name`, `owner`, `status`, `configured_harnesses`, connect/last-seen, runner count | Host registry / `HostStore` (extend read, not create) |
| **Host permission grant** (NEW) | `host_id`, `principal` (user / group / `__public__`), `level` (`use`), `granted_by`, `granted_at` | Permission store — extend the existing (session-scoped) grant model to host scope |
| **Audit entry** (NEW) | actor, action (shutdown / grant / revoke), target host, principal, ts | Server logs / audit table |

## 3. Interfaces (server)
| Endpoint | Status | Auth |
|----------|--------|------|
| `GET /v1/hosts?all=true` (or `/v1/admin/hosts`) | NEW (reads existing registry) | admin only |
| `POST /v1/hosts/{id}/shutdown` (or `DELETE`) | NEW (reuses disconnect path) | owner or admin |
| `PUT /v1/hosts/{id}/permissions/{principal}` | NEW (the 405 today) | owner or admin |
| `GET /v1/hosts/{id}/permissions` | NEW | owner or admin |
| `DELETE /v1/hosts/{id}/permissions/{principal}` | NEW | owner or admin |
| `GET /v1/hosts` | MODIFY — return owned **+ granted** | any authed |
| `POST /v1/hosts/{id}/runners` | MODIFY — allow granted non-owner | owner or granted |

Web UI: a new admin-gated **Hosts** page + a per-host **share management** panel.

## 4. Dependencies
- **A modifiable, deployable Omnigent server** (C-O1) — the blocking prerequisite.
- Existing: admin_list / permission_store / host registry / host-tunnel disconnect — build on these.
- The web UI toolchain (`web/`, the SPA that serves `/hosts`, `/settings`).
- CoDA is a *downstream consumer* (its `share_and_launch` starts working once C-R3 ships) — not a dependency, a beneficiary.

## 5. Risks
| # | Risk | Impact | Mitigation |
|---|------|--------|-----------|
| **RK-C1** | No deployable target (C-O1 unresolved) | Unbuildable | Resolve fork-vs-upstream + deploy access **first** — M0 gate |
| **RK-C2** | Tenant isolation on a shared host unsolved (C-O3) | Sharing unsafe / limited | Define `use` as "launch an *isolated* runner," not "share the workspace"; if FS can't isolate, scope shares to launch-only |
| **RK-C3** | Upstream rejects multi-owner hosts (C-C2/C-O5) | Fork maintenance burden | Keep the change additive/opt-in; design for upstream-ability; accept fork if needed |
| **RK-C4** | Header-mode principal identity mismatch (C-O4) | Grants key on wrong identity | Nail the identity string in header mode before building the grant store |
| **RK-C5** | Shutdown races an active session | Data loss / abrupt kill | Graceful drain + confirm dialog; audit; mirror runner graceful-timeout |
| **RK-C6** | Admin sees hosts but actions bypass owner intent | Trust/security | Owner-or-admin gating + audit on every mutating action (C-N2/C-N3) |

## 6. Milestones
- **M0 — Secure a deployable Omnigent (BLOCKING GATE, C-O1).** Decide fork vs. upstream; get a modifiable server you can deploy as (or alongside) `omnigent-daveok`. Clone it locally. **Nothing else starts until this exists.**
- **M1 — Admin host listing (read-only, lowest risk).** Admin-scoped unfiltered host list endpoint + the new admin Hosts page rendering the existing registry data. Ships value immediately (visibility) with no mutation risk.
- **M2 — Host shutdown.** Endpoint reusing the disconnect path + UI action with confirm + audit. Owner-or-admin gated.
- **M3 — Host permission model + grant API (the new core).** Extend the permission store to host scope; implement `PUT/GET/DELETE …/permissions/…`; resolve header-mode identity (C-O4) and isolation stance (C-O3) here.
- **M4 — Non-owner visibility + launch.** Modify `GET /v1/hosts` (owned+granted) and `POST …/runners` (allow granted). This is where a grantee first actually *uses* a shared host end-to-end (C-S3).
- **M5 — Share-management UI + CoDA loop.** The per-host share panel; verify CoDA's existing `share_and_launch` now succeeds (no 405, C-S4).
- **M6 — Audit + hardening.** Audit logging, fail-closed auth tests (non-admin/non-owner refused, C-S5), shutdown-race handling.

### Sequencing
```
M0 (deployable server) ──→ M1 (list) → M2 (shutdown) → M3 (grant API) → M4 (visibility+launch) → M5 (UI + CoDA loop) → M6 (audit)
```
M0 is an absolute gate — it's a **resourcing/access** question, not code. M1–M2 are low-risk existing-primitive exposure; M3–M4 are the genuinely-new authorization work; M5–M6 close the loop and harden.

## 7. Relationship to the workshop (spec A)
**None required.** The workshop uses per-attendee CoDA apps (spec A) and needs zero host-sharing. Feature C is a standalone product capability; if it ships, it *could* later simplify multi-user CoDA scenarios, but it is **not** on the workshop's critical path and must not block it.
