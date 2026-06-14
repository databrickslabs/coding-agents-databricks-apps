# GOAL: Attach a CoDA instance to Omnigents as an agent host

> **Status:** MVP (Shape A, FR-1…FR-9) IMPLEMENTED + DEPLOYED LIVE on branch
> `feat/omnigents-host`. CoDA-side pipeline fully working & observable
> (`/api/omnigents-status` = all green). One residual blocker, isolated to
> Omnigents' host WS-auth mechanics (NOT a CoDA bug) — see §0.1.
> **Author:** David O'Keeffe · **Date:** 2026-06-14
> **Tracking:** FEIP-7646 (child of FEIP-2996).

---

## 0.1 LIVE deploy result (2026-06-14) — CoDA side complete; residual = Omnigents host auth

Redeployed the live `coda` app from this branch with the integration enabled.
Verified via `/api/omnigents-status`: **every CoDA-side stage succeeds** —
`sp_creds_captured:true, installed:true, host_launched:true, stage:"running"`.

The CoDA app process: captures the app-SP creds before the strip → downloads
the protocol-matched host wheel from a UC Volume (SDK auth with the captured
creds) → installs the CLI (click==8.1.8) → writes an `oauth-m2m` profile with
`https://` host → launches a supervised `omnigents host`.

**Bugs found & fixed this session (all CoDA-side, committed):**
1. Build had stripped `omnigents/resources/examples` → server crashed at
   import. Rebuilt with it.
2. `grant_sp_perms.py` (Lakebase public-schema grant) was needed for the
   server to boot.
3. `_materialize_spec` bare `WorkspaceClient()` couldn't auth after the cred
   strip → pass captured SP creds explicitly.
4. OAuth profile host lacked `https://` scheme → token wouldn't mint.
5. Profile needed `auth_type = oauth-m2m` (CLI/SDK won't infer M2M otherwise).
6. Omnigents' token factory ignores `--profile`; set
   `DATABRICKS_CONFIG_PROFILE` + clear shadowing PAT env vars.
- Prereqs: CoDA SP granted `READ_VOLUME` on the wheels volume + `CAN_USE` on
  the `omnigents-daveok` app; wheels staged in `ot_demo.omnigents.artifacts`.

**Residual blocker (Omnigents-side, needs their input):** the live
`omnigents host` still 302-loops to OIDC. PROVEN it is NOT the credential:
the M2M token minted from the exact `omnigents-host` profile
(`aud=<workspace>, scope=all-apis`) **connects the tunnel** when sent as a raw
WS bearer (`websockets` + `Authorization: Bearer` → `M2M_CONNECTS_NOW`, twice).
So the token + proxy + SP grant all work. The 302 is in **how Omnigents' host
WS client attaches/uses the token** — likely its `_make_auth_token_factory` /
`_resolve_databricks_auth` returns/caches `None` (or the WS upgrade omits the
header), sending the tunnel upgrade unauthenticated. This is in
`agent-framework` `omnigents/runner/_entry.py` + `omnigents/host/connect.py`,
not CoDA. Next step: instrument the host's actual outgoing WS-upgrade
Authorization header, or raise with the Omnigents team — headless M2M host auth
may not be fully supported yet.

---

## 0. Phase-0 findings (2026-06-13) — empirical, supersedes the guesses below

We stood up a real Omnigents server and ran the host-attach de-risk live.

- **Build path WORKS.** Built + deployed `omnigents-daveok` (Omnigents server,
  API-only / `--skip-web-ui`) into the `daveok` workspace *by driving the
  existing `coda` app's shell over its REST API* — `coda` has fast public
  internet, so we patched the build to public PyPI (repo `uv.toml` +
  `deploy.py` index) and built there, sidestepping both the laptop's bad wifi
  and the unreachable internal Databricks pypi proxy. App compute ACTIVE,
  `bundle deploy` SUCCEEDED. (Note: public-PyPI drift pulled `click 8.4.1`
  which broke the CLI — `Context.protected_args` is read-only in click ≥8.2;
  pin **`click==8.1.8`** per Omnigents' lockfile.)
- **Host attach FAILED on auth — this is the gating result.** Installed the
  built `omnigents` wheel in `coda` and ran
  `omnigents host <omnigents-daveok-url> --profile DEFAULT`. The WSS tunnel
  (`wss://.../v1/hosts/<id>/tunnel`) is **redirected to interactive OIDC**
  (`/.auth/callback` → `/oidc/oauth2/v2.0/authorize`) by the Databricks Apps
  ingress proxy; the host logs *"the server redirected the host tunnel to a
  login page… credentials from profile 'DEFAULT' were not accepted"* and
  retries every 10s forever. **A headless CoDA host's PAT/SP token is NOT
  accepted by the stock-deployed Omnigents-app ingress.**
- **CORRECTED (the first read was wrong):** the initial "coda accepts a bearer
  token, omnigents-daveok doesn't" was a test artifact — I'd used an **OAuth**
  token for coda and a **PAT** for the host. Re-tested apples-to-apples:
  - **PAT** → both apps `302 → /oidc/oauth2/v2.0/authorize` (bounced *at the
    Apps ingress proxy*, never reaches the app).
  - **OAuth token** (`databricks auth token`) → `omnigents-daveok` returns
    `502` — i.e. it **passed the proxy** and reached the app (which then
    errored internally; see app-health note).
  - **Conclusion:** the auth gate is a **token-type** issue. The Databricks
    Apps ingress accepts an **OAuth / service-principal** token and rejects a
    **PAT**. `omnigents host` failed only because it presented CoDA's
    PAT-based `DEFAULT` profile.
- **THIS IS FIXABLE FROM CoDA'S SIDE — it's a code change, not a config hunt.**
  The fix is the two-credential design (FR-3/FR-4), now evidence-backed:
  - **Host tunnel:** CoDA's **app process** (which holds the injected app-SP
    `DATABRICKS_CLIENT_ID/SECRET` — note: these are NOT in the browser-terminal
    shell, CoDA strips them, so the fix must live in `app.py`, not a shell
    command) mints an **M2M OAuth token** and launches `omnigents host` with
    it → passes the proxy.
  - **Agent work:** the runner uses CoDA's PAT + `ANTHROPIC_*` gateway creds
    (forwarded via `HARNESS_CREDENTIAL_ENV_VARS`) for the actual coding.
- **Secondary blocker (separate from auth): app `/health` = 502.** The
  Omnigents app reached but errored — almost certainly the Lakebase public-
  schema grant the README's `grant_sp_perms.py` step does, which `deploy.py`
  does NOT run. Run `grant_sp_perms.py` (app SP Alembic privileges) before the
  app can serve. Needed before a host can do useful work, but independent of
  the host-attach auth path.

---

## 1. Purpose and scope

### Purpose

Let an Omnigents server run coding-agent **sessions on a CoDA instance** the
same way it already runs them on a **Lakebox** sandbox or a user's laptop —
so that a long-lived, customer-tenant CoDA App becomes a registered *host* in
the Omnigents host picker, drivable from the Omnigents Web UI and mobile app.

### Scope

In scope: making a deployed CoDA App register itself with an Omnigents server
as a host, accept `host.launch_runner` frames, and run coding-agent runners in
its own container.

Out of scope (this iteration): Omnigents *provisioning* CoDA apps on demand
(see §8, Phase 2), changes to the Omnigents server, and any change to how CoDA
runs its own native browser terminals (those keep working unchanged).

### Why this is worth doing (the one-paragraph rationale)

Omnigents already supports two host types — your **laptop** (transient,
sleeps, behind NAT) and a **Lakebox** sandbox (Databricks-provisioned,
internal-only today, one sandbox backs many sessions). CoDA is a **third,
distinct shape neither covers**: an *always-on application running inside the
customer's own Databricks tenant*, already holding workspace credentials, an
AI Gateway route, MLflow tracing, and the agent CLIs installed. Attaching CoDA
as a host gives Omnigents an in-tenant, always-available execution surface
without the customer standing up a Lakebox or keeping a laptop online.

---

## 2. Users and stakeholders

| Stakeholder | Interest |
|---|---|
| **CoDA app owner (SA / field eng)** | Wants `coda` apps already deployed in customer tenants to be usable as Omnigents hosts with minimal extra setup. |
| **End user (developer)** | Wants to start an Omnigents session from the Web UI / phone and have it execute on the in-tenant CoDA host, not their laptop. |
| **Omnigents team** | Owns the server + host contract; must not be regressed. This work consumes their contract, it does not change it. |
| **Customer security/platform** | Cares that the host runs under the app service principal's UC/AI-Gateway scope and dials *out* to the server (no inbound exposure). |

---

## 3. Background: what "a host" is in Omnigents (domain facts)

Grounded in `agent-framework` at `omnigents/host/connect.py`,
`omnigents/onboarding/sandboxes/base.py`, and the Databricks Apps deploy at
`deploy/databricks/`.

- **Architecture is four-tier:** `server → runner(host) → harness → clients`.
  The **server** holds storage + client API and zero execution state. A
  **host** runs `omnigents host`, connects to the server over an **outbound
  WebSocket**, and on each `host.launch_runner` frame spawns a **runner**
  subprocess that drives a harness (Claude SDK, Codex, …).
- **A host is not special infrastructure.** Anything that runs
  `omnigents host --server <url> [--profile <p>]` and can reach the server is a
  host. The laptop `--host` flag and the Lakebox both reduce to this.
- **Lakebox is one `SandboxLauncher` provider** (`onboarding/sandboxes/lakebox.py`,
  beside `modal.py`). A launcher's job is: `provision()` a VM → `put()` the
  Omnigents wheels in → `exec_foreground()` runs `omnigents host` inside it.
  The base class (`base.py`) distinguishes **CLI-bootstrap launchers** (do all
  of the above) from **managed-only launchers** (run from a pre-baked image;
  most primitives stay the raising default).
- **The server already has a `host_type` concept** (`host_type="managed"`,
  PR #2857) — so adding a `databricks-app` host type is consistent with the
  server's existing model, not a fork of it.
- **CoDA is already a Databricks App** deployed via DABs with workspace creds,
  AI Gateway routing, and the agent CLIs installed at boot (`setup_*.py`).

**Implication chosen for the MVP:** CoDA does not need to be *provisioned* by
Omnigents. It already exists and is always on. The cheapest correct design is
**CoDA self-registers as a host** — its container runs `omnigents host`
pointing at a configured server. No `SandboxLauncher` is required for the MVP.

### Two credentials, two boundaries (the load-bearing detail)

Grounded in `omnigents/host/connect.py` and `omnigents/server/auth.py`.

Two **separate** credentials are in play; conflating them is the main source of
confusion:

1. **Host-tunnel token** — authenticates `omnigents host` *to the Omnigents
   server* over the outbound WSS. Crosses two checkpoints:
   - **Boundary 1 — Databricks Apps ingress proxy** in front of the Omnigents
     server App. CoDA's app-SP / rotated-PAT profile token passes this (same
     identity mechanism CoDA already uses).
   - **Boundary 2 — the Omnigents server's own auth mode** (`auth.py:7-11`):
     - `header` (default): server trusts the `X-Forwarded-Email` the proxy
       injects from whatever identity passed Boundary 1. **A headless CoDA
       container works here** — no interactive login. Session is attributed to
       the SP's identity.
     - `accounts` / `oidc`: server demands its own signed `__Host-ap_session`
       cookie minted by an **interactive** OIDC/PKCE browser login. **A
       headless container cannot do this** → host gets `403` on WS upgrade.
   - **Net:** CoDA-as-host requires the target server to run **`header` auth
     mode**. The shared trust anchor is the Databricks Apps proxy, not any
     CoDA↔Omnigents code coupling.
2. **Harness LLM credential** — what the *runner* (the coding agent) uses to
   call the model through AI Gateway. The host forwards a curated set
   (`HARNESS_CREDENTIAL_ENV_VARS`: `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`,
   `OPENAI_*`, …) host→runner as the **deliberate exception** to the
   secrets-blocking allowlist. The `connect.py` comment names this exact case:
   *"on a server-managed sandbox: the deployment's injected provider secrets —
   forwarding them is the intent."* **CoDA is "the deployment": its
   already-injected AI-Gateway env forwards to runners for free.** This — not
   the host-tunnel token — is what "does the work."

The runner does **not** inherit CoDA's personal shell secrets (allowlist
blocks them), but it **does** inherit `DATABRICKS_CONFIG_PROFILE` /
`DATABRICKS_CONFIG_FILE` (both allowlisted), so it resolves the **same
Databricks profile** — hence the same Unity Catalog scope. Data-plane
governance is preserved via the profile file the rotated PAT writes, not via an
inherited live token.

---

## 4. Functional requirements

IDs are referenced by acceptance criteria in §7.

### MVP — CoDA self-registers as a host (Shape A)

- **FR-1 — Install the Omnigents host runtime in CoDA.** The CoDA image/boot
  installs the `omnigents` wheel(s) such that `omnigents host` is on `PATH`
  inside the running app container. Reuses CoDA's existing uv-based install
  path; respects the Databricks PyPI proxy on locked-down networks.
- **FR-2 — Configure the server target.** CoDA reads the Omnigents server URL
  (and optional auth profile) from app config (`app.yaml` env var, e.g.
  `OMNIGENTS_SERVER_URL`). Absent/empty → the host feature is **off** and CoDA
  behaves exactly as today (no regression).
- **FR-3 — Register as a host at boot.** When `OMNIGENTS_SERVER_URL` is set,
  CoDA launches `omnigents host --server <url>` as a supervised background
  process during startup, alongside the existing agent-setup work.
- **FR-4 — Two-credential wiring (host tunnel vs. harness LLM).**
  - **Host tunnel:** the host process authenticates to the server with CoDA's
    existing Databricks identity (rotated-PAT profile / app SP). The **target
    server MUST run `header` auth mode** (A-3); accounts/oidc servers reject a
    headless host.
  - **Harness LLM:** the runner authenticates to AI Gateway via CoDA's
    already-injected `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL` (+ any
    `OPENAI_*`), which the host forwards through `HARNESS_CREDENTIAL_ENV_VARS`.
    No new credential is minted; CoDA's existing gateway env does the work.
  - **Data plane:** the runner inherits `DATABRICKS_CONFIG_PROFILE` /
    `DATABRICKS_CONFIG_FILE`, resolving the same profile (same UC scope) the
    rotated PAT writes — so no live token is injected, governance is preserved.
  - If the defaults miss a custom gateway var, set
    `OMNIGENTS_RUNNER_ENV_PASSTHROUGH` on the host with the extra names.
- **FR-5 — Run runners in-container.** On a `host.launch_runner` frame, the
  host spawns a runner that executes the coding agent inside the CoDA
  container's filesystem (the same workspace the browser terminals use).
- **FR-6 — Appear in the host picker.** Once registered, the CoDA instance is
  selectable as a host when starting a new chat from the Omnigents Web UI /
  mobile app.
- **FR-7 — Survive PAT/token rotation.** The host process must keep working
  across CoDA's 10-minute token rotation (`pat_rotator.py`) — token refresh
  must reach the host's auth, or the host must re-auth on its own cadence.
- **FR-8 — Supervised lifecycle.** If the host process dies, CoDA restarts it
  (bounded retry/backoff) and surfaces status; if the server is unreachable,
  CoDA retries with backoff rather than crashing the app.
- **FR-9 — Observable.** Host process start/stop/restart and registration
  success/failure are logged through CoDA's existing logging (and, where
  enabled, MLflow/OTel) so an operator can tell whether the host is live.

### Stretch — Omnigents provisions CoDA hosts (Shape B, see §8)

- **FR-10 (Phase 2)** — A `CodaLauncher(SandboxLauncher)` lets the Omnigents
  server *provision* a CoDA app on demand (`provision()` deploys a CoDA app
  via DABs; `terminate()` deletes it), making `databricks-app` a managed host
  type. Explicitly deferred.

---

## 5. Non-functional requirements

- **NFR-1 — No inbound exposure.** The host connects *outbound* to the server
  (WebSocket dial-out). CoDA must not need to open any inbound port for this.
- **NFR-2 — Governance preserved.** Runners execute under the same Databricks
  profile (hence UC scope) CoDA already resolves, inherited via
  `DATABRICKS_CONFIG_PROFILE`/`_FILE`; the host introduces no path that escapes
  it. The runner env is an allowlist — CoDA's personal/host secrets are NOT
  forwarded, only the curated harness LLM creds. Identity at the server is
  asserted by the Databricks Apps proxy (`header` mode), consistent with CoDA's
  proxy-authoritative model.
- **NFR-3 — Off by default.** With no server configured, CoDA's behaviour,
  startup time, and resource use are unchanged. The feature is purely additive.
- **NFR-4 — Startup budget.** Registering the host must not block CoDA's
  existing parallel agent setup; it runs concurrently and the app serves the
  terminal UI on the same timeline as today.
- **NFR-5 — Supply-chain hygiene.** The `omnigents` wheel is installed from a
  pinned, known source (matching CoDA's existing pinning posture); no switch to
  a moving fork. Air-gapped/vendored installs remain possible.
- **NFR-6 — Resilience.** Server outage, auth expiry, or host crash degrade to
  "host offline," never to "CoDA app down."

---

## 6. Assumptions and constraints

- **A-1** The CoDA container can reach the Omnigents server URL over HTTPS/WSS
  (in-tenant App egress to the server's Apps URL). *Verification gate before
  build — this is the single biggest unknown.*
- **A-2** The installed `omnigents` build exposes `omnigents host` with a
  `--server` (and `--profile`) interface compatible with the target server
  version. (Confirmed present in the build on this machine, 0.1.0.)
- **A-3 (RESOLVED → now a constraint, C-3)** The Omnigents server accepts a
  headless self-registering host **iff it runs `header` auth mode** (trusts the
  Apps-proxy-injected `X-Forwarded-Email`). `accounts`/`oidc` mode requires an
  interactive login a container cannot perform → blocked without an Omnigents
  server-side headless-SP login path (out of MVP scope). **Target a header-mode
  server.** Session ownership = the SP identity the proxy forwards.
- **A-4** CoDA's filesystem is an acceptable execution environment for runners
  (it already hosts the agent CLIs and a workspace).
- **C-1** No changes to the Omnigents server in the MVP. If A-3 requires a
  server change, that becomes a dependency on the Omnigents team, not part of
  this iteration.
- **C-2** CoDA is single-user per instance (its auth model); a CoDA host backs
  that one user's sessions, not multi-tenant fan-out.
- **C-3** The target Omnigents server runs **`header` auth mode** (see A-3). The
  `omnigents-daveok` server we deploy defaults to this; the shared internal
  server may run accounts/oidc and is therefore likely NOT a valid MVP target.

---

## 7. Acceptance criteria

Each maps to functional requirements (traceability in §9). Phrased as
observable pass/fail checks.

- **AC-1 (FR-1, FR-2, FR-3)** — Deploy a CoDA app with `OMNIGENTS_SERVER_URL`
  set to a reachable Omnigents server. After boot, `omnigents host` is running
  in the container **and** the server's host registry lists this CoDA instance.
- **AC-2 (FR-6)** — In the Omnigents Web UI **New Chat → host picker**, the
  CoDA instance appears as a selectable host.
- **AC-3 (FR-5)** — Starting a session on the CoDA host and asking the agent to
  create a file results in that file existing **in the CoDA container's
  workspace** (verified via CoDA's terminal/file view), proving execution
  happened in-container.
- **AC-4 (FR-4, NFR-2)** — The session's workspace/data access is scoped to the
  app service principal's UC permissions (a resource the SP cannot see is not
  accessible from the agent session).
- **AC-5 (FR-7)** — A session started before a token rotation is still
  controllable from the Web UI ≥ 11 minutes later (past one rotation), with no
  manual re-auth.
- **AC-6 (NFR-3)** — Deploy the same CoDA build with `OMNIGENTS_SERVER_URL`
  unset: no `omnigents host` process runs, no registration is attempted, and
  the existing CoDA terminal UX is unchanged.
- **AC-7 (FR-8, NFR-6)** — Kill the host process in a running CoDA container;
  within the retry budget it restarts and re-appears in the host registry, and
  the CoDA app never returns non-200 on `/health` during the outage.
- **AC-8 (FR-8)** — Point CoDA at an unreachable server URL: CoDA boots
  normally, serves the terminal UI, logs repeated connect failures with
  backoff, and does not crash.

---

## 8. Out of scope (this iteration)

- **Shape B / FR-10** — Omnigents *provisioning* CoDA apps via a
  `CodaLauncher(SandboxLauncher)` (managed `databricks-app` host type). Larger
  effort; revisit once Shape A proves the execution model and A-3 is resolved.
- Any modification to the Omnigents server, its frames, or its UI.
- Multi-user fan-out from a single CoDA host (precluded by C-2).
- Replacing CoDA's native browser terminals with the Omnigents Web UI.
- Persisting Omnigents session state into CoDA's Lakebase.

---

## 9. Traceability

| Requirement | Verified by | Notes |
|---|---|---|
| FR-1 install host runtime | AC-1 | reuse CoDA uv install |
| FR-2 server config | AC-1, AC-6 | `OMNIGENTS_SERVER_URL` env |
| FR-3 register at boot | AC-1 | supervised bg process |
| FR-4 SP auth + AI Gateway | AC-4 | no user PAT |
| FR-5 in-container runner | AC-3 | file lands in CoDA workspace |
| FR-6 host picker | AC-2 | Web UI |
| FR-7 token rotation | AC-5 | survives `pat_rotator` cycle |
| FR-8 supervised lifecycle | AC-7, AC-8 | restart + backoff |
| FR-9 observable | (manual log review) | logs / OTel |
| NFR-1 no inbound | AC-2 (dial-out) | WSS outbound |
| NFR-2 governance | AC-4 | UC SP scope |
| NFR-3 off by default | AC-6 | additive |
| NFR-6 resilience | AC-7, AC-8 | degrade to "host offline" |
| FR-10 (Phase 2) | — | deferred |

---

## 10. Open questions (resolve before implementation)

1. **A-1 — Can a CoDA App container actually reach the Omnigents server URL?**
   App-to-App egress within a tenant / to the shared server. Verify with a
   single `curl`/WS probe from inside a CoDA container before any build.
   *(Now the #1 gate, since A-3 is resolved.)*
2. **Confirm the target server's auth mode is `header`** (C-3). For
   `omnigents-daveok` this is the default; verify no `OMNIGENTS_AUTH_PROVIDER`
   override was set. If targeting any other server, confirm before building.
3. **Session-ownership acceptability** — in header mode the session is owned by
   the app SP's forwarded email, not the human operator. Confirm that's
   acceptable for the audit/attribution story (it is for single-user CoDA, but
   state it).
4. **Wheel size / install** — `omnigents` pulls `claude-agent-sdk` (~60 MB);
   confirm it installs cleanly in the CoDA image within the startup budget and
   respects the air-gap/vendoring posture (NFR-5).
5. **Version compatibility** — which Omnigents server version is the target,
   and does the host wheel's `frame_protocol_version` match it?
```

> **First experiment to de-risk (do this before writing FR code):** in a
> running CoDA container, install the `omnigents` wheel and run
> `omnigents host --server <header-mode-server> --profile <p>` by hand.
> Decision tree:
> - **`HostHelloFrame` accepted → host shows in the Web-UI picker** → Shape A
>   validated; the rest is packaging + supervision (the prompt below).
> - **`403` on WS upgrade** → server is NOT header mode (or rejects the SP).
>   Stop: either point at a header-mode server, or this needs an Omnigents
>   server-side change (out of scope).
> - **Connection refused / timeout** → A-1 egress problem. Resolve reachability
>   before anything else.
>
> Then start a session on the CoDA host and have the agent write a file —
> confirm it lands in the CoDA workspace (AC-3) and used CoDA's AI-Gateway
> creds (no separate key configured).
