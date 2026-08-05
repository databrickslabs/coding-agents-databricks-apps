# GOAL: MLflow tracing for CoDA sessions via MLflow OSS + Lakebase, mirrored into Databricks experiments

> **Status:** DRAFT (design). Not yet built. Supersedes the *direct-to-Databricks*
> tracing path (`setup_mlflow.py` → `MLFLOW_TRACKING_URI=databricks`) **for
> network-restricted deployments only**. The direct path stays the default where
> the network allows it.
> **Author:** David O'Keeffe · **Date:** 2026-07-11
> **Profile for development:** `lakemeter`
> **Repo the feature lands in:** `coding-agents-databricks-apps` (private mirror
> `coding-agents-databricks-apps-private`). This repo
> (`databricks-mlflow-lakebase-tracing`) holds the spec + the standalone
> tracing-server / copy-job artifacts.

---

## 1. Problem — the network envelope breaks direct tracing

**The target agents are the coding agents: Claude Code, OpenCode, Pi, Hermes.**
Two problems compound:

1. **Only Claude Code is traced at all today.** CoDA traces Claude Code into a
   **Databricks-hosted MLflow experiment** via the MLflow Claude plugin
   (`mlflow autolog claude -u databricks`, `setup_mlflow.py`). **OpenCode, Pi,
   and Hermes have no MLflow hook** (`docs/observability.md` §1) — they produce
   zero traces to organize. (Codex has a hook but is **out of scope** for this
   feature — 2026-07-11 decision.)
2. **Even Claude Code's path fails in the restricted environment.** It sets
   `MLFLOW_TRACKING_URI=databricks`, so every span travels **directly over the
   network** to Databricks-managed trace storage.

So this feature has **two tracks**: **(coverage)** create traces for the
un-hooked agents (OpenCode/Pi/Hermes) — spec-D; and **(destination)** send all
coding-agent traces to a sink reachable in the restricted network — specs A/B/C.

**The constraint is the PRODUCTION target workspace, not the dev workspace.**
Zerobus writes **work fine in the `lakemeter` dev workspace** (so the whole
pipeline can be built and proven there), but **do NOT work in the target prod
workspace** — which is where the feature must ultimately run. So the direct
`MLFLOW_TRACKING_URI=databricks` path **cannot work in prod**, and this was
**proven, not assumed** (OTEL/Zerobus investigation, HTTP-level debug + a
hand-built protobuf probe against the prod-restricted environment):

- **In prod, the OTLP/OTEL ingest path is blocked server-side at Zerobus.** The
  client emits fine; the break is Databricks' OTLP→Zerobus ingest on **Serverless
  Compute**, which cannot reach the **Private-Link / NCC-protected ADLS storage**
  backing the UC catalog. The synthetic probe reproduced the exact failure:
  metrics → HTTP **400 `PERMISSION_DENIED`**, logs/spans → HTTP **404
  `TABLE_DOES_NOT_EXIST`**, underlying error *"Zerobus doesn't support storage
  behind a Private Endpoint yet … 403 Forbidden."* The block was **workspace-wide**
  in that environment — a second storage account also 403s — so **no client-side
  change makes rows land in prod.**
- **In prod, managed trace storage (DBFS/ADLS) is unreachable for the same
  reason** — behind the Private Endpoint that Serverless-Compute Zerobus can't
  traverse.
- **What IS reachable in prod:** PyPI / npm (package installs work), and
  **Lakebase** (managed PostgreSQL) — the app can open a Postgres connection.

> **Dev vs prod — how to build this.** Because Zerobus **works on `lakemeter`**,
> the full A→B→C→D pipeline (agents → OSS → Lakebase → copy job → experiment) can
> be developed and proven end-to-end on dev, *including* the copy job's write to a
> Databricks experiment. The **one thing dev cannot prove** is that the copy job's
> Databricks-experiment write survives **prod's** Zerobus/NCC block — that must be
> validated against prod specifically (spec-C C-C4). Do not mistake a green run on
> lakemeter for a green run in prod on the write path.
>
> **Why not just fix the prod network?** The remediation is either (a) route the
> destination to a schema whose storage is *not* Private-Link protected, or (b)
> have the Databricks **account team add NCC / private-endpoint access** — **both
> depend on an infra/account-team change on someone else's timeline.** MLflow OSS +
> Lakebase is the **client-owned path**: the agents' traces land in Lakebase in
> prod regardless. The only prod-blocked hop is the *final* copy into the managed
> experiment (C-C4) — and that hop runs from a serverless job whose networking is
> the thing to confirm, not the agents' hot path.

Three facts we have already established in this environment (treat as given, not
re-litigated):

1. **MLflow OSS runs fine as a Databricks App.** A self-hosted `mlflow server`
   inside an app boots and serves.
2. **MLflow OSS can log to Lakebase.** Its SQLAlchemy backend store points at the
   Lakebase Postgres endpoint.
3. **Traces can be copied from Lakebase into a Databricks MLflow experiment via a
   Lakeflow serverless job** — networking *is* allowed from the serverless job
   context, so the job bridges the two worlds.

So the architecture is forced by the envelope: **agents → MLflow OSS (in-network)
→ Lakebase (in-network) → Lakeflow serverless copy job (has network) → Databricks
experiment (the destination the field team actually looks at).**

## 2. What "done" looks like

A CoDA fleet instance, deployed with tracing enabled, produces agent-session
traces that a user can open in a **Databricks MLflow experiment** — organized so
each session is legible (agent, user, project, session id) — **without any agent
traffic ever touching zerobus or managed storage.**

Concretely, the end state is:

- **All four coding agents write traces to a self-hosted MLflow OSS server**, not
  to `databricks`. Claude Code points its MLflow tracking URI at the OSS server
  (spec-B); OpenCode, Hermes, and Pi are traced by instrumenting the shared
  content-filter proxy they route through (spec-D), which writes to the same OSS
  server.
- **The OSS server persists traces to a shared Lakebase database.** One MLflow
  OSS app serves the whole CoDA fleet; every instance's traces land in one shared
  Lakebase, keyed by instance.
- **A Lakeflow serverless job incrementally copies new traces from Lakebase into
  per-instance Databricks MLflow experiments** (`/Users/{app_owner}/{app_name}`,
  matching today's naming), tagged so a session is filterable by agent, user,
  session, and project.
- **The whole tracing path is best-effort:** if the MLflow OSS server or Lakebase
  is unreachable, the agent session runs normally and the trace is dropped — the
  agent is **never** blocked. This matches the repo's existing rule ("never block
  startup on tracing setup", `setup_mlflow.py:103`).
- **App-to-app permissions are correct:** the CoDA app's service principal is
  authorized to reach the MLflow OSS app, reusing the app-to-app SP-OAuth pattern
  the repo already ships for the Omnigent host integration
  (`grant_omnigent_host.sh`, `grant_workshop_host.sh`, spec-C SP auth).

## 3. Non-goals

- **Replacing the direct-to-Databricks path everywhere.** Where the network
  allows `MLFLOW_TRACKING_URI=databricks`, that remains the default — it is
  simpler and has no copy-job lag. This feature is the **restricted-network
  fallback**, gated behind a flag.
- **Tracing agents outside the coding-agent set.** Gemini and the Omnigent host
  are **not** in scope — only the four coding agents (Claude Code, OpenCode, Pi,
  Hermes). Codex is dropped too (2026-07-11). *(Note: coverage for OpenCode/Pi/
  Hermes IS in scope — that's spec-D — this feature does both coverage and
  destination for the coding agents.)*
- **OTEL metrics → UC tables.** That channel hits the same prod Zerobus/NCC block
  described in §1 (and is a known Claude Code 2.1.200 headless gap besides). It's
  orthogonal to MLflow traces. Out of scope.
- **Replaying/backfilling historical traces** that predate the feature.

## 4. Why this shape (the load-bearing decisions)

Recorded so a future maintainer doesn't re-open settled questions. These were
chosen deliberately (2026-07-11):

- **One MLflow OSS app for the fleet, shared Lakebase, keyed by instance.**
  Rejected: MLflow-embedded-in-each-CoDA-container (couples MLflow lifecycle to
  the agent app, no fleet sharing, N Lakebases to manage). One server keeps the
  operational surface small; MLflow's experiment abstraction gives per-instance
  isolation *within* one server.
- **Experiment-per-instance in the destination, with session tags.** Matches the
  existing `/Users/{app_owner}/{app_name}` convention (`setup_mlflow.py:34`), so
  the destination looks exactly like today's direct-path experiments — the only
  difference is *how the traces got there*. Session-level tags (agent, user,
  session id, project) make each trace filterable in the Traces UI.
- **Scheduled incremental batch copy.** A serverless Lakeflow job on a short
  interval, reading new traces since a watermark, idempotent via trace-id dedup.
  Rejected continuous/triggered (more moving parts, latency isn't critical for an
  observability readout) and on-demand-only (traces invisible until someone runs
  it).
- **Best-effort, never block the agent.** A dead sink must not cost a session.
  Rejected local-spool-and-backfill for v1 (more code, stronger delivery
  guarantee than the use case needs) — but see the open question on whether the
  OSS server's own availability already gives us enough buffering.

## 5. Acceptance criteria (the bar for "proven", not "configured")

Following the repo's discipline that *configured ≠ flowing*
(`observability.md` §1 audit finding), the feature is done when, on a **deployed
lakemeter instance with the restricted-network flag on**:

- **G-1 — Agent → OSS.** A real `claude -p "..."` session produces a trace that
  lands in the **MLflow OSS server**, verified by reading it back from the OSS
  server's own API (not just a local write). And a real **OpenCode / Hermes / Pi**
  session produces a trace via the instrumented proxy (spec-D) — all four coding
  agents land traces in the OSS server.
- **G-2 — OSS → Lakebase.** That trace is present as rows in the **Lakebase**
  trace tables, verified by a direct Postgres query.
- **G-3 — Lakebase → Databricks experiment.** After the copy job runs, the same
  trace (matched by trace id) is present in the **Databricks experiment**
  `/Users/{app_owner}/{app_name}`, verified via the REST v3 traces search
  (`/api/3.0/mlflow/traces/search`) — the same read-back the existing runbook
  uses (`docs/part3-tracing-runbook.md`).
- **G-4 — Organized.** In the Databricks experiment, the trace carries tags that
  identify agent, user, session id, and project, and is visible/filterable in the
  Traces UI.
- **G-5 — No forbidden traffic.** No trace path touches zerobus or managed
  storage. (Negative check: the direct `MLFLOW_TRACKING_URI=databricks` plugin is
  NOT active when the flag is on.)
- **G-6 — Non-blocking.** With the MLflow OSS app **stopped**, a Claude Code
  session still completes normally (trace dropped, no agent error).
- **G-7 — App-to-app auth.** The copy from agent → OSS is authorized by the CoDA
  app SP against the MLflow OSS app; a fresh instance grant is idempotent and
  survives a redeploy (mirrors the Omnigent host grant's success criteria).

Each of G-1..G-3 is a **round-trip read-back**, not a config inspection — the
same standard `prove_trace_lands.py` set for the direct path.

## 6. Component specs

This goal is delivered by the specs in this folder:

- **spec-A — MLflow OSS tracking server on Lakebase** (the standalone app: server,
  Lakebase backend, health/read APIs). *(destination track)*
- **spec-B — CoDA client redirection + app-to-app auth** (point **Claude Code**'s
  tracking URI at the OSS app; wire the SP grant next to `grant-omnigent-host`;
  gate behind the restricted-network flag). *(destination track)*
- **spec-C — Lakeflow serverless copy job** (Lakebase → Databricks experiments,
  incremental, idempotent, session tags). *(destination track)*
- **spec-D — Proxy-based trace coverage for OpenCode, Hermes, Pi** (instrument the
  content-filter proxy to emit a span per model request; route Pi through the
  proxy). *(coverage track — the half that creates traces that don't exist today)*

See `README-index.md` for the current status of each.

## 7. Open questions (carried into the specs)

- **Q1 — Does MLflow OSS actually persist *traces* (spans) in the SQLAlchemy
  Postgres backend, or only runs/experiments?** This is the single load-bearing
  assumption. If traces need a separate store, spec-A changes. *(Under research;
  resolved answer folded into spec-A.)*
- **Q2 — Artifact store with no object storage.** MLflow wants an artifact root.
  With no DBFS/blob, can the OSS server use a **local** artifact root and still
  ingest traces (traces living in the DB, not the artifact store)? *(spec-A.)*
- **Q3 — Does the MLflow Claude *plugin* accept a non-`databricks` tracking URI?**
  The plugin path (`mlflow autolog claude -u databricks`) is how Claude Code
  traces today. If it only speaks `databricks`, spec-B needs a different client
  wiring (e.g. `MLFLOW_TRACKING_URI=http://<oss-app>` + generic autolog).
  *(spec-B.)*
- **Q4 — Re-logging a trace into another store.** What is the supported API to
  read a trace out of the OSS store and write it into a Databricks experiment
  with its spans/tags intact? *(spec-C.)*
- **Q5 — App-to-app reachability for a *server* app.** The Omnigent pattern grants
  CoDA's SP access to another app. Confirm the same grant lets CoDA's agents make
  ordinary authenticated HTTP calls to the MLflow OSS app's URL (the MLflow client
  is a plain HTTP client, not the Databricks SDK). *(spec-B.)*
