# /speckit.specify — (C) Omnigent Admin Host-Management Page

**Component:** Omnigent server + web UI — **upstream OSS** (`github.com/omnigent-ai/omnigent`) or a controlled fork. **Not CoDA.**
**Feature:** An **admin page** in the Omnigent web UI to (1) see all hosts connected across the server, (2) shut a host down, and (3) manage shares (grant/revoke `use`) on a host — the capability that lets SP-owned CoDA hosts be made usable by others.
**Status:** Draft. This is a *product feature* (justified on its own merits), distinct from the workshop (spec A), which needs none of this. Supersedes the retired spec B by reframing "host sharing" as one action inside a broader admin host-management surface.
**Grounding:** Verified against the OSS clone — the server already has an admin concept, a host registry, and a host-disconnect lifecycle; this feature mostly *exposes and admin-gates* existing primitives, plus adds the genuinely-missing permission-grant endpoint.

---

## 1. Problem

Omnigent hosts today are opaque and strictly single-owner:
- `GET /v1/hosts` is **owner-scoped** — no one, including admins, can see the full set of hosts connected to the server.
- Hosts are **single-owner** — `GET /v1/hosts/{id}` → `403 "not your host"`; there is **no** permission/share endpoint (`PUT /v1/hosts/{id}/permissions/{user}` → `405`, verified live 2026-07-07). CoDA's `share_and_launch` (`omnigents_host.py:648`) already *calls* this endpoint but it doesn't exist server-side, so the call is dead.
- There is **no operator surface** to shut down a wedged/abandoned host, audit what's connected, or delegate `use`.

For an operator running a multi-CoDA / multi-user Omnigent server, this is a real gap: you can't see your fleet, can't reclaim resources, and can't share a host you own with a colleague or cohort.

## 2. Users

| User | Need |
|------|------|
| **Server admin / operator** (the primary user) | See every connected host, its owner, status, harnesses, load; shut down hosts; grant/revoke `use` shares |
| **Host owner** (e.g. a CoDA app SP, or a person) | Grant a colleague `use` on their own host without an admin |
| **Grantee** | See and use a host that was shared with them (today: impossible) |

**Admin gate:** reuse the existing `admin_list.is_admin(email)` / `permission_store.is_admin(user_id)` mechanism (`server/admin_list.py`, `_require_admin`). Do **not** invent a new admin model.

## 3. Requirements

### Functional — server
- **C-R1 — Admin host listing.** An admin-scoped read that returns **all** connected hosts (not owner-filtered), each with: `host_id`, `name`, `owner`, `status` (online/offline), `configured_harnesses`, connect time / last-seen, and current runner/session count. *(Reads the existing host registry / `HostStore` — the data is already tracked; today's `GET /v1/hosts` just filters it. Add an admin path or an `?all=true` gated by `is_admin`.)*
- **C-R2 — Host shutdown.** An admin (and the host owner) can shut down a host: trigger the existing disconnect/deregister path (`host_tunnel.py` `on_host_disconnect`, "deregister, set offline in DB") over the tunnel, terminating its runners cleanly. New endpoint, existing lifecycle.
- **C-R3 — Host permission grants (the genuinely-new part).** Implement `PUT / GET / DELETE /v1/hosts/{host_id}/permissions/{principal}` accepting a level (`use`), for a user (and optionally a group / public sentinel — see C-O2). This is the endpoint CoDA already calls and the server currently lacks (returns 405).
- **C-R4 — Non-owner visibility.** `GET /v1/hosts` returns hosts the caller *owns OR has been granted* on — not only owned. (Extends the conversation-level public/grant model that already exists in `server/permissions.py` to hosts.)
- **C-R5 — Non-owner launch authorization.** `POST /v1/hosts/{id}/runners` (and session binding) succeeds for a principal granted `use`, not only the owner (today: hard `403`).

### Functional — web UI
- **C-R6 — Admin host page.** A new page (admin-gated) listing all hosts as a table/cards: owner, status, harnesses, load; scannable at a glance (state encoded as color/pill, not just text). Per-host actions: **Shut down**, **Manage shares**.
- **C-R7 — Share management UI.** On a host, view current grants and add/revoke a principal's `use`. Owner sees it for their hosts; admin for any.

### Non-functional
- **C-N1 — Isolation on shared hosts (the hard problem).** If multiple principals share one host, their sessions/filesystems must be isolated. **If this can't be guaranteed, `use`-sharing should be limited/documented** — an unshared-FS host shared to N people reintroduces the collision problems. This is the riskiest requirement (see C-O3).
- **C-N2 — Auth safety.** Every new endpoint gated: admin actions require `is_admin`; owner actions require ownership; grant actions require owner-or-admin. Fail closed.
- **C-N3 — Audit.** Shutdowns and grant/revoke changes are logged with actor + target.

## 4. Constraints
- **C-C1** Third-party OSS — delivery is via **upstream PR** or a **controlled fork you deploy** (like the air-gap OpenCode fork pattern). Timeline/acceptance not fully under our control. **Prerequisite: which of these, and do we have deploy access to the server?** (C-O1)
- **C-C2** Single-owner appears to be a **deliberate** design stance (consistent `403` across every host route) — upstream may push back; scope the change to be additive and opt-in.
- **C-C3** Sharing a host does **not** raise its capacity (same RAM ceiling). This feature is about *access & operability*, not scale.
- **C-C4** Auth mode matters: the deployed server is header-mode (`accounts_enabled:false`); the grant model must work there, not only in accounts mode. Verify how principals are identified in header mode.

## 5. In scope
Admin host-listing endpoint + page; host shutdown (endpoint + UI); host permission grant/revoke API; non-owner visibility & launch; share-management UI; admin gating on all of it; audit logging.

## 6. Out of scope
- CoDA-side changes — CoDA's `share_and_launch` *already* calls the grant endpoint; once C-R3 ships it lights up, possibly with a small shape tweak. Track as a follow-up, not part of C.
- The workshop (spec A) — it uses none of this.
- Raising host capacity (C-C3).

## 7. Open questions
- **C-O1 — Fork vs. upstream + deploy access (BLOCKING).** Where does the code change live, and can we deploy the modified server as our own Omnigent app? Without a deployable target, this is unbuildable. *(No omnigent repo is checked out locally yet.)*
- **C-O2 — Grant granularity.** User-only, or also groups / a `__public__`-style all-users grant (reusing the session-level public-grant machinery in `server/permissions.py:82`)? Groups/public make cohort scenarios one call instead of N.
- **C-O3 — Tenant isolation on a shared host (RISK).** Can two principals safely share one host's FS/sessions? If not, `use` may need to mean "can launch an isolated runner" rather than "shares the workspace." Determines whether C-N1 is achievable.
- **C-O4 — Header-mode principal identity.** In `accounts_enabled:false` mode, what identity string is a grant keyed on (email? SP id?), and does the SSO proxy carry it consistently (ref CoDA's `X-Forwarded-Email` authority)?
- **C-O5 — Upstream appetite (C-C2).** Would maintainers accept multi-owner hosts, or is a fork the realistic path (with its maintenance burden)?

## 8. Success criteria
- **C-S1** An admin opens the host page and sees **every** connected host across the server (owner, status, harnesses, load), not just their own.
- **C-S2** An admin shuts down a chosen host from the UI; its runners terminate and it goes offline.
- **C-S3** A host owner grants a colleague `use`; the colleague now sees the host in their `GET /v1/hosts` and can launch a session on it — end-to-end, replacing today's `403`/`405`.
- **C-S4** CoDA's existing `share_and_launch` call succeeds (no longer `405`) against the modified server.
- **C-S5** All actions admin/owner-gated and audit-logged; verified a non-admin/non-owner is refused.
