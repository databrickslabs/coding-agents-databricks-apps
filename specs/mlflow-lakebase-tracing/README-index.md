# MLflow tracing for CoDA via MLflow OSS + Lakebase — SPEC INDEX

**Goal:** organize MLflow traces for **every CoDA session** into deployed
Databricks MLflow experiments, in an environment where **no networking to
zerobus or managed storage** is allowed — only PyPI/npm and **Lakebase** are
reachable. The forced architecture:

Target agents: **Claude Code, OpenCode, Pi, Hermes** (Codex out of scope).

```
Claude Code                    OpenCode · Hermes · Pi
  │ MLflow plugin (spec-B)        │ every request → content-filter proxy
  │ -u <oss-url>                  │ 127.0.0.1:4000 → span per request (spec-D)
  │                               │ (Pi re-routed through the proxy, D-R3)
  └───────────────┬───────────────┘
                  ▼
MLflow OSS server  (Databricks App)                     (spec-A)
        │  --backend-store-uri postgresql://…lakebase…
        ▼
Lakebase (Postgres)  — trace_info + spans tables         (spec-A)
        │  read (networking allowed in serverless)
        ▼
Lakeflow serverless copy job  — span reconstruction      (spec-C)
        │  MLFLOW_TRACKING_URI = databricks
        ▼
Databricks MLflow experiments  /Users/{owner}/{app_name}
   organized by session tags: agent · user · session_id · project
```

**Two tracks:** *coverage* (spec-D — create traces for the 3 un-hooked agents)
and *destination* (specs A/B/C — route all traces to a network-reachable sink).

**Author:** David O'Keeffe · **Date:** 2026-07-11 · **Dev profile:** `<dev-profile>`
**Lands in:** `coding-agents-databricks-apps` (private:
`<private-mirror>`).

---

## Documents

| Doc | Track | Scope | Status |
|-----|-------|-------|--------|
| **`GOAL.md`** | — | North star: the network envelope, the end state, acceptance bar (G-1..G-7), the load-bearing design decisions. | Draft |
| **`spec-A-mlflow-oss-server.md`** | destination | Standalone MLflow OSS app on Lakebase (local artifact root, no object storage). Core assumptions **verified**. | Draft |
| **`spec-B-coda-client-redirection.md`** | destination | Point **Claude Code** at the OSS app; app-to-app SP-OAuth grant (`CAN_USE`), reusing the Omnigent-host pattern; gated behind a flag. | Draft |
| **`spec-C-lakeflow-copy-job.md`** | destination | Serverless Lakebase→Databricks copy. **Highest-risk** — no supported OSS→managed trace-migration tool exists; span-reconstruction is DIY. | Draft |
| **`spec-D-proxy-trace-coverage.md`** | **coverage** | Instrument the content-filter proxy to emit a span per request → traces **OpenCode + Hermes**; route **Pi** through the proxy too. **Creates the traces that don't exist today.** | Draft |

---

## What's verified vs open (be honest — repo discipline)

**Verified 2026-07-11 (primary sources + internal Glean):**
- MLflow OSS persists **traces/spans in the Postgres backend store**
  (`trace_info`, `spans` tables; span JSON in `spans.content`) — Lakebase works
  as the trace store. *(spec-A §0)*
- Trace ingestion **does not need object storage** — a **local** artifact root
  suffices (traces live in the DB). Unlocks the no-managed-storage envelope.
  *(spec-A §0; inferred + must be validated empirically)*
- The **Claude plugin accepts a non-`databricks` tracking URI**
  (`mlflow autolog claude -u <url>`) — the CoDA client can be redirected.
  *(spec-B B-R3, resolved)*
- **App-to-app auth is a solved pattern here** — `CAN_USE` on the target app,
  granted to the caller app's SP (`grant_omnigent_host.sh`). spec-B reuses it.
- **The content-filter proxy is the universal choke point for the un-hooked
  agents.** OpenCode and Hermes already route every model request through
  `127.0.0.1:4000` (`setup_opencode.py`, `setup_hermes.py:135`); only Pi bypasses
  it (`setup_pi.py:111`). Instrument the proxy once → OpenCode + Hermes traced;
  re-route Pi → all three. This is spec-D, and it's the coverage half of the
  feature. *(confirmed against the setup scripts, not assumed)*

**The one big risk (spec-C §0):**
- **No off-the-shelf OSS→managed MLflow trace-migration tooling exists**
  (Databricks internal position, Confluence 2026-06-30). `mlflow.log_trace` is
  deprecated and single-span-only. The copy job must **reconstruct spans**
  itself, mint new destination ids, and **honestly document fidelity limits**.
  **Prototype the reconstruction on one real trace (C-O1) before building the
  full job.**

---

## Recommended build order

1. **spec-A** — stand up the MLflow OSS app on Lakebase; prove a trace lands in
   `trace_info`/`spans` (A-S1). Nothing else works until this does.
2. **spec-B** — redirect **Claude Code** + the SP grant; prove a live session
   lands in the OSS server (G-1) and that the grant is load-bearing (B-S2).
   Best-effort/non-blocking checked here (G-6). This proves the destination path
   end-to-end with the one already-traced agent.
3. **spec-D** — the coverage lift. Instrument the proxy → prove **OpenCode**
   traces (D-S1), then **Hermes** (D-S2), then re-route **Pi** through the proxy
   (D-R3) and prove it (D-S2). **Verify the per-request path adds no latency with
   the sink down (D-S3)** — this is the NFR that can sink the design if wrong.
4. **spec-C** — **prototype the span reconstruction on one trace first (C-O1).**
   Only if fidelity is acceptable, build the incremental job; else descope to a
   session-summary trace and record that.

> Order note: spec-D depends only on spec-A (a sink to write to), not on spec-B —
> steps 2 and 3 can proceed in parallel once spec-A is up. spec-C is last because
> it has nothing to copy until traces exist in Lakebase.

Each step is a **round-trip read-back**, not a config check (matches
`docs/observability.md`'s configured-≠-flowing discipline and
`prove_trace_lands.py`).

---

## Relationship to existing tracing

This is the **restricted-network fallback**, not a replacement. The direct
`MLFLOW_TRACKING_URI=databricks` path (`setup_mlflow.py`, `docs/observability.md`)
stays the **default** wherever the network allows it. Everything here is gated
behind `MLFLOW_OSS_TRACKING_ENABLED` (spec-B B-R1) and off by default.
