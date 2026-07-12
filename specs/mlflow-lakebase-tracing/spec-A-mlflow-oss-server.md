# /speckit.specify — (A) MLflow OSS tracking server on Lakebase

**Component:** New standalone Databricks App — the fleet's MLflow OSS tracking
server. Not part of the CoDA app; CoDA points at it (spec-B).
**Feature:** Run `mlflow server` as a Databricks App with a **Lakebase
(PostgreSQL) backend store** and a **local artifact root**, so agent traces
persist in Lakebase with **zero networking to zerobus or managed object
storage.**
**Status:** Draft. Core assumptions **verified** (see §Findings).
**Profile:** `<dev-profile>`.

## 0. Findings (verified 2026-07-11 — these de-risk the whole design)

Primary-source confirmations that the network-restricted path is viable:

- **Traces persist in the SQLAlchemy backend store, not the artifact store.**
  MLflow's tracking DB models (`mlflow/store/tracking/dbmodels/models.py`) define
  `SqlTraceInfo` (table `trace_info`) and `SqlSpan` (table `spans`; column
  `content` holds the full serialized span JSON, keyed by
  `(trace_id, span_id)`, FK `trace_id → trace_info.request_id`). So pointing
  `--backend-store-uri` at Lakebase Postgres **is** where the traces land.
- **Local artifact root is sufficient.** `mlflow server --backend-store-uri
  postgresql://... --default-artifact-root <local-path> --serve-artifacts`
  is a documented run mode. Because traces live in the backend DB, the artifact
  root can be a container-local path — **no S3/DBFS/blob required.** This is the
  fact that makes "no managed storage" workable.
- **The MLflow Claude plugin targets a self-hosted server.**
  `mlflow autolog claude -u http://<host>:<port> -n "<experiment>"` is
  documented — the `-u/--tracking-uri` flag accepts a plain HTTP(S) URL, so the
  CoDA client (spec-B) can point Claude Code at this app.

These collapse open questions Q1, Q2, Q3 from `GOAL.md` §7. What remains for
spec-A is the **deployment reality on Databricks Apps** (Lakebase wiring, the
app process, migrations, auth), not the MLflow-can-do-it question.

## 1. Problem

The fleet needs a trace sink that is reachable **inside** the restricted network.
Databricks-hosted MLflow is not (no zerobus, no managed storage). Lakebase *is*
reachable. MLflow OSS can use Lakebase as its backend store (§0). So we stand up
one MLflow OSS server, as a Databricks App, backed by Lakebase.

## 2. Users
- **CoDA agents (via CoDA app SP):** write traces to this server (spec-B).
- **Copy job (spec-C):** reads traces out of Lakebase (or via this server's API).
- **Maintainer:** operates one app + one Lakebase for the whole fleet.

## 3. Requirements

### Functional

- **A-R1 — Run `mlflow server` as a Databricks App.** An app whose command is
  `mlflow server --host 0.0.0.0 --port <app-port> --backend-store-uri <lakebase>
  --default-artifact-root <local> --serve-artifacts`. The port must be the one
  Databricks Apps expects the process to bind (the app framework's assigned
  port). *(new app.)*
- **A-R2 — Lakebase backend store.** `--backend-store-uri` is a
  `postgresql://...` URL built from the Lakebase connection env
  (`PGHOST/PGDATABASE/PGUSER/PGPASSWORD/PGPORT` — injected when Lakebase is added
  as an app resource, per `.claude/skills/databricks-apps-python/5-lakebase.md`).
  Requires `psycopg2-binary` (or `psycopg`) in `requirements.txt` — **the #1
  cause of Lakebase app crashes if omitted** (per the same skill). *(new.)*
- **A-R3 — Shared, instance-keyed.** One Lakebase database serves the whole
  fleet. Per-instance isolation is by **MLflow experiment** — each CoDA instance
  logs to its own experiment name (`/Users/{owner}/{app_name}`, spec-B B-R6), and
  MLflow's schema already keys `trace_info`/`spans` by `experiment_id`. No
  per-instance DB. *(design decision from `GOAL.md` §4.)*
- **A-R4 — Schema init / migrations.** On first boot the server must create the
  MLflow schema in Lakebase. MLflow runs Alembic migrations against the backend
  store; ensure the Lakebase role has DDL rights (Lakebase grants the app role
  `Can connect and create`, per the skill — sufficient). Booting against an empty
  DB must self-provision. *(operational.)*
- **A-R5 — Read-back API for verification.** The server must expose MLflow's REST
  trace-search so G-1/the copy job can read traces back
  (`/api/3.0/mlflow/traces/search` or the OSS equivalent). This is inherent to
  `mlflow server`; the requirement is to **verify it answers** on the deployed
  app URL. *(verification hook.)*
- **A-R6 — Inbound auth = Databricks Apps SP gating.** The app is protected by
  Databricks Apps' native auth; only identities with `CAN_USE` reach it
  (spec-B grants CoDA's SP that). MLflow OSS itself has no auth here — the app
  boundary is the auth boundary. Document that the MLflow server is **only** as
  private as the app's `CAN_USE` ACL. *(security posture — state it explicitly.)*

### Non-functional

- **A-N1 — Local artifact root is ephemeral; traces must not depend on it.**
  Since traces persist in Lakebase (§0), a container restart that wipes the local
  artifact root loses only run artifacts (which we don't produce), **not** traces.
  Confirm no trace data is written to the artifact root. *(durability.)*
- **A-N2 — Survives redeploy/restart.** Because state lives in Lakebase, the app
  is stateless and can restart freely without losing traces. *(matches Lakebase's
  point.)*
- **A-N3 — One app for the fleet — capacity.** A single MLflow server takes all
  fleet trace writes. For a ~50-attendee workshop this is low volume (session-end
  writes, not per-token), but the per-write load is **asserted, not measured** —
  flag as a capacity open question, mirroring the honesty in the workshop spec's
  §7. *(sizing.)*

## 4. Constraints

- **A-C1 — No object storage, no zerobus.** The whole reason this app exists.
  `--default-artifact-root` MUST be a local path (or a
  Lakebase/`serve-artifacts` local mode), never `dbfs:` or `s3:`. Any config that
  reaches for managed storage breaks the envelope.
- **A-C2 — Lakebase is Public Preview** (per the skill) — accept preview-tier
  caveats (autoscale/branch behaviour, connection limits). The shared-DB design
  concentrates connections on one instance; watch the Postgres connection ceiling
  under fleet load.
- **A-C3 — MLflow version parity.** The server's MLflow version must be
  compatible with the clients' (CoDA app venv is **mlflow 3.14.0**, per
  `docs/part3-tracing-runbook.md`). Trace schema compat across 3.x is the risk;
  pin the server to a 3.14-compatible line.
- **A-C4 — App process model.** `mlflow server` is the app's main process (not
  gunicorn+FastAPI like CoDA). The `app.yaml` `command:` runs `mlflow server`
  directly. Confirm Databricks Apps is happy running a non-FastAPI long-lived
  process on its assigned port.

## 5. In scope
The new app (`app.yaml`, `requirements.txt`, the `mlflow server` command); the
Lakebase resource wiring; schema init; verifying the REST read-back; documenting
the `CAN_USE`-is-the-auth-boundary posture.

## 6. Out of scope
- Pointing CoDA at it (spec-B).
- The copy job (spec-C).
- Multi-region / HA MLflow. One app, one Lakebase, one region (<dev-profile>).
- Authn *inside* MLflow (basic-auth users) — the app ACL is the boundary (A-R6).

## 7. Open questions
- **A-O1 — Lakebase connection ceiling under fleet write load** (A-N3/A-C2).
  Measure before claiming it scales to 50 attendees.
- **A-O2 — `serve-artifacts` local mode vs pure `--default-artifact-root`.**
  Confirm the minimal flag set that ingests traces with **no** external artifact
  store and no errors on the trace path.
- **A-O3 — Does Databricks Apps' assigned port + health check tolerate the
  `mlflow server` process** (A-C4), or does it need a thin wrapper/`gunicorn`
  front? Verify on deploy.
- **A-O4 — Lakebase DDL rights for Alembic** (A-R4). Confirm the auto-granted app
  role can run MLflow's migrations on first boot.

## 8. Success criteria
- **A-S1 (=G-2).** After a client writes a trace (spec-B), rows appear in the
  Lakebase `trace_info` and `spans` tables — verified by a direct Postgres query.
- **A-S2.** The deployed app answers a trace-search REST call on its app URL with
  the written trace.
- **A-S3 (=A-N2).** Restarting the app loses no traces (they're in Lakebase).
- **A-S4 (=A-C1).** Config review confirms no `dbfs:`/`s3:`/zerobus reference on
  any trace path — the envelope holds.
