# /speckit.specify — (C) Lakeflow serverless copy job (Lakebase → Databricks experiments)

**Component:** New Lakeflow serverless job. Bridges the network-restricted world
(Lakebase, spec-A) to the destination the field team looks at
(Databricks-hosted MLflow experiments).
**Feature:** Incrementally read new agent traces from the MLflow OSS server /
Lakebase and **reconstruct** them into per-instance Databricks MLflow experiments,
idempotently, with session tags.
**Status:** Draft. **Highest-risk spec in the set — read §0.**
**Profile:** `lakemeter`.

## 0. The load-bearing risk — there is NO supported trace-copy tool

Verified 2026-07-11 against MLflow docs and **internal Databricks field
engineering guidance** (Confluence "MLflow OSS vs Managed", A. Gurary,
updated 2026-06-30, via Glean). Do not skip this:

- **`mlflow.log_trace` is deprecated (MLflow 3.6+) and only produces a
  single-root-span trace** from a `(request, response)` pair. It **cannot**
  faithfully copy a multi-span agent trace. **The copy job must NOT be built on
  `log_trace`.**
- **Databricks' own internal position: "No off-the-shelf tooling exists" for
  migrating OSS-MLflow traces onto managed MLflow.** It "has happened before, but
  always with heavy field involvement, and typically ended in a hybrid stack."
  The official talk-track marks *"you can migrate your existing traces"* as a
  **do-not-say**. **We are building exactly the thing Databricks says has no
  supported tooling — so we own it end to end, and we state its limits honestly**
  (the repo's "configured ≠ flowing / no overclaim" discipline applies double
  here).
- **The supported-but-DIY path** is span-by-span reconstruction (§3, C-R3): read
  the full `Trace` (TraceInfo + spans) from the source, then rebuild it in the
  destination via the client span APIs (`start_trace`/`start_span`/`end_span`/
  `end_trace`) or the `StartTraceV3` REST POST with the `trace_location` rewritten
  to the target experiment — preserving `start_time_ns`/`end_time_ns` and
  re-mapping parent span IDs. **New trace IDs are minted on write; some
  server-managed `trace_metadata` will not round-trip.** Accept and document this.

If §0 makes the copy look too fragile, the fallback is **not** to fabricate
fidelity — it's to reduce scope (e.g. copy a flattened session summary trace, or
keep traces in the OSS UI and only export summaries). That's a scoping decision,
not an implementation detail — see C-O1.

## 1. Problem

Traces persist in Lakebase (spec-A), reachable only inside the restricted
network. The people who consume traces work in **Databricks MLflow experiments**.
Networking *is* allowed from a Lakeflow **serverless job** context (given). So a
job reads traces from Lakebase / the OSS server and writes them into the Databricks
experiments, closing the loop the agents can't close directly.

## 2. Users
- **Trace consumer (field team / facilitator):** opens the Databricks experiment
  and sees agent sessions, organized and filterable.
- **Maintainer:** owns the job, its schedule, and its watermark.

## 3. Requirements

### Functional

- **C-R1 — Incremental read with a watermark.** Each run reads only traces newer
  than the last successful run. Source of truth for "new": `trace_info.timestamp_ms`
  (indexed with `experiment_id`, per spec-A §0). Persist the watermark (last
  copied `timestamp_ms` per source experiment) durably — Lakebase itself is a fine
  watermark store. *(the "incremental batch" decision, `GOAL.md` §4.)*
- **C-R2 — Read full traces (spans included).** Read via
  `MlflowClient(tracking_uri=<oss-url>).search_traces(locations=[exp_id],
  include_spans=True, filter_string="timestamp_ms > <watermark>")`, OR by direct
  Lakebase SQL on `trace_info` + `spans` (JSON `content`). Prefer the MLflow client
  read (schema-version-safe) unless it's too slow, then fall back to SQL.
  *(read side is fully supported.)*
- **C-R3 — Reconstruct into the destination (NOT `log_trace`).** For each source
  trace, create a matching trace in the target Databricks experiment by replaying
  spans in dependency order (`start_span`/`end_span` with original timings and
  re-mapped parent ids), then finalizing. Preserve span name, type, status,
  inputs/outputs (`content`), and timings. Set `MLFLOW_TRACKING_URI=databricks`
  for the write side. *(the DIY core — see §0.)*
- **C-R4 — Idempotent via trace-id dedup.** Because the destination mints a **new**
  trace id (§0), dedup on the **source** trace id: write it as a destination tag
  (e.g. `source_trace_id`) and, before reconstructing, skip any source trace whose
  id already appears in the destination. Re-running the job must not double-write.
  *(the "idempotent via trace-id dedup" decision.)*
- **C-R5 — Experiment mapping.** Source experiment `/Users/{owner}/{app_name}`
  (in the OSS server) → Databricks experiment of the **same name**
  (spec-B B-R6). Create the destination experiment if absent. One source
  experiment → one destination experiment. *(keeps the destination identical to
  today's direct path.)*
- **C-R6 — Session tags (G-4).** On each reconstructed trace set tags:
  `agent` (claude/opencode/hermes/pi), `user`, `session_id`, `project`, and
  `source_trace_id` (for dedup). Derive these from the source trace's own
  tags/metadata where the agent already sets them; where it doesn't, derive what's
  available (at minimum agent + source id). *(the "organized nicely" ask.)*
- **C-R7 — Scheduled serverless.** Run as a serverless Lakeflow job on a short
  interval (e.g. every 5–15 min). The job needs: network egress to the Databricks
  MLflow API (allowed here) and connectivity to Lakebase / the OSS app. *(the
  cadence decision.)*

### Non-functional

- **C-N1 — Fidelity honesty.** The job MUST record, in its own output/docs, what
  does and does not round-trip (new ids, dropped server metadata, any span types
  that don't reconstruct). No claim that the Databricks trace is byte-identical to
  the OSS trace. *(no-overclaim, doubled per §0.)*
- **C-N2 — Failure isolation.** A trace that fails to reconstruct must not abort
  the run — log it, skip it, advance past it (or dead-letter it), and keep the
  watermark honest (don't advance past a trace you failed to write, or you lose
  it silently). *(robustness.)*
- **C-N3 — Best-effort end-to-end (G-6 alignment).** The job is downstream of the
  agents; if it lags or fails, agents and OSS-server tracing are unaffected.
  Traces accumulate in Lakebase until the next successful run. *(decoupling.)*
- **C-N4 — Cost.** Serverless + short interval + incremental = small. Don't
  re-scan the whole history each run (that's what the watermark prevents). *(cost
  discipline.)*

## 4. Constraints

- **C-C1 — New ids on write (§0).** Dedup cannot use the destination id; it must
  use `source_trace_id` (C-R4). Any design that assumes stable ids across the copy
  is wrong.
- **C-C2 — PG 12+ / schema parity.** The `spans` table uses a generated
  `duration_ns` column (PG 12+); Lakebase satisfies this (spec-A §0). If reading
  via SQL, the job's schema assumptions must match the server's pinned MLflow
  version — prefer the client read (C-R2) to avoid coupling to DDL.
- **C-C3 — Auth, two identities.** Read side authenticates to the OSS app
  (app `CAN_USE` or direct Lakebase creds); write side authenticates to Databricks
  MLflow as the **job's** identity, which must have write access to the target
  experiments. Two distinct auth contexts in one job. *(don't conflate them.)*
- **C-C4 — The destination write is the SAME path Zerobus blocks IN PROD —
  it will pass on dev and can still fail in prod.** Writing an MLflow trace into a
  Databricks experiment is the OTLP→Zerobus→ADLS write that **works on `lakemeter`
  but was proven to fail in the prod target** (400 `PERMISSION_DENIED` / 403,
  Serverless-Compute Zerobus can't reach Private-Link storage — `GOAL.md` §1).
  **Consequence for testing:** a green copy-job run on lakemeter proves the
  *logic* but NOT that prod's write survives — the dev workspace has working
  Zerobus, so it can't reproduce the prod block. The premise "the serverless job
  has networking" must be validated against **trace-write in the prod workspace
  specifically**, not general egress and not dev: either the job's serverless
  compute is NCC-allow-listed to the experiment's backing storage, OR the
  destination experiment lives in a schema whose storage is **not** Private-Link
  protected. **Prove one real trace write from a serverless job in the PROD
  workspace before declaring the destination track done** — if it 400s like the
  OTEL path did, the final copy hop is blocked on the same NCC/account-team fix,
  and that must be surfaced as a known prod dependency, not discovered at
  cutover. *(make-or-break external dependency; dev cannot de-risk it.)*

## 5. In scope
The serverless job: incremental read, span-reconstruction write, dedup, experiment
mapping, session tags, schedule, watermark, and the fidelity-limits documentation.

## 6. Out of scope
- Producing the traces (spec-A/B).
- Real-time / triggered copy (rejected in `GOAL.md` §4 — scheduled batch chosen).
- Backfilling pre-feature history.
- A general OSS→managed MLflow migration tool (we build the **narrow** CoDA-trace
  case, not the general one Databricks says is unsupported).

## 7. Open questions
- **C-O1 — Reconstruction fidelity vs scope (the big one).** Does span-by-span
  reconstruction faithfully reproduce a real agent trace (a Claude Code plugin
  trace, or a proxy-emitted OpenCode/Hermes/Pi trace), or do we
  accept a **flattened per-session summary trace** in the destination (cheaper,
  robust, less faithful)? Prototype the reconstruction on ONE real trace before
  committing the full job. If fidelity is poor, descope to a summary trace and say
  so. *(prototype-first; do not build the whole job on an unproven copy.)*
- **C-O2 — Read path: MLflow client vs direct SQL.** The client read is
  schema-safe but was **slow from the CoDA container**
  (`observability.md`: `search_traces` hung, killed at 45s — though that was
  against `databricks`, not an OSS server). Benchmark the client read against the
  OSS server; fall back to Lakebase SQL if it's too slow.
- **C-O3 — What tags do the source traces already carry?** Claude Code plugin
  traces vs spec-D proxy-emitted traces may tag differently — the proxy can set
  `agent`/`session_id` deliberately (spec-D D-R4), Claude Code's plugin sets
  whatever it sets. Determines how much of C-R6 is "read an existing tag" vs
  "derive/inject." Inspect a real trace of each kind in the OSS server.
- **C-O4 — Watermark granularity.** Per-source-experiment watermark vs global.
  Per-experiment is safer for a fleet (one lagging instance doesn't stall others).
- **C-O5 — `StartTraceV3` REST vs client span replay.** Which reconstruction
  mechanism is less lossy and simpler? Decide during the C-O1 prototype.

## 8. Success criteria
- **C-S1 (=G-3).** After the job runs, a trace written by a real agent session
  (spec-B) appears in the Databricks experiment `/Users/{owner}/{app_name}`,
  matched to its source by the `source_trace_id` tag, verified via
  `/api/3.0/mlflow/traces/search`.
- **C-S2 (=C-R4).** Running the job twice does not duplicate traces
  (dedup holds).
- **C-S3 (=G-4).** The destination trace carries `agent`, `user`, `session_id`,
  `project` tags and is filterable in the Traces UI.
- **C-S4 (=C-N1).** The job's docs state the reconstruction's fidelity limits
  (new ids, any dropped metadata) — verified by comparing one source/destination
  trace pair, not asserted.
- **C-S5 (=C-N2).** A deliberately malformed source trace is skipped/dead-lettered
  without aborting the run or corrupting the watermark.
