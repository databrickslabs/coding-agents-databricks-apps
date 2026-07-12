# Coles deploy runbook — CoDA + MLflow OSS on workspace ...2455

> **Target:** Azure workspace `7405614666872455` (region shard `.15`),
> catalog `edp_aisandbox_aisandbox_dev`.
> **Deploy method:** git deployment (no local CLI profile for ...2455 needed).
> **Date:** 2026-07-12.

## Identity model (read first)

Two identities, two jobs — don't conflate them:

| Identity | Does what |
|----------|-----------|
| **Your personal PAT** (pasted in a CoDA terminal) | one-time privileged **setup**: deploy the OSS MLflow app, run the grant scripts |
| **The CoDA app SP** (`ENABLE_SP_APIKEYHELPER=true`) | steady-state **agent traffic**: model calls, Omnigent host, MLflow trace writes |

The app SP boots with **zero** privileges. Everything it needs is granted by your
PAT during setup. `app.yaml` cannot grant SP permissions — those are cross-object
ACLs (`CAN_USE` on another app, `READ_VOLUME` on a volume, `CAN_QUERY` on
serving), set out-of-band via the `grant_*.sh` scripts (or the Apps UI).

## What's already wired in `app.yaml`

Base `app.yaml` is pointed at Coles ...2455 (commit pending). Verify before deploy:

| Var | Value |
|-----|-------|
| `DATABRICKS_GATEWAY_HOST` | `https://adb-7405614666872455.15.azuredatabricks.net/ai-gateway` |
| `MLFLOW_OSS_URL` | `https://coda-mlflow-oss-7405614666872455.15.azure.databricksapps.com` |
| `MLFLOW_OSS_TRACKING_ENABLED` | **`false`** (staged — flip after step 4) |
| `OMNIGENTS_SERVER_URL` | `https://omnigent-7405614666872455.15.azure.databricksapps.com` |
| `OMNIGENTS_WHEEL_SPEC` | `/Volumes/edp_aisandbox_aisandbox_dev/ai/omnigents/wheels` |
| `CLAUDE_CODE_OTEL_CATALOG_SCHEMA` | `edp_aisandbox_aisandbox_dev.ppcs` |
| `ENABLE_SP_APIKEYHELPER` | `true` (agents auth as the app SP) |

## Deploy sequence (order matters — each step gates the next)

### 0. Prereqs on ...2455 (workspace admin, one-time)
- CoDA app created on ...2455 (`databricks apps create`; compute size is a
  create-time flag, e.g. `--compute-size LARGE` — NOT in `app.yaml`).
- The `edp_aisandbox_aisandbox_dev.ai.omnigents.wheels` volume exists, staged with
  the Omnigent host wheel.
- Your personal identity has `CAN_MANAGE` on the CoDA app (to run grants) and the
  usual FE grants on `edp_aisandbox_aisandbox_dev`.

### 1. Deploy CoDA (git deployment) — tracking OFF
Push the branch to the workspace-linked repo; the app rebuilds from `app.yaml`.
`MLFLOW_OSS_TRACKING_ENABLED=false` at this point is deliberate — the OSS app
doesn't exist yet, so leaving it on would fail every trace write.

**Verify (deploy ≠ done):** hit `/api/setup-status` + read boot logs
(`databricks apps logs` / `/api/logs`). `RUNNING` alone only means gunicorn bound
the port. Confirm agents can make a model call (SP has gateway access — see step 3).

### 2. Grant the app SP its steady-state permissions (your PAT, in a CoDA terminal)
Paste your personal PAT in the CoDA terminal, then:
```bash
# Serving / gateway: SP needs to make model calls through the AI Gateway on ...2455.
#   Grant the app SP CAN_QUERY on the serving endpoints it uses (UI or CLI).
# Omnigent wheel volume: SP needs READ_VOLUME to install the host CLI.
databricks grants update volume edp_aisandbox_aisandbox_dev.ai.omnigents \
  --principal <coda-app-sp> --add READ_VOLUME   # adjust to actual grant CLI/UI
```
> The exact serving grant depends on whether calls go via a named endpoint or the
> gateway path. The gateway is a workspace entitlement, not a single
> `serving-endpoint` resource — confirm the SP can reach it (a `400 Invalid Token`
> on model calls means the gateway/token is wrong for this workspace).

### 3. Deploy the MLflow OSS app on ...2455 (your PAT, in a CoDA terminal)
Deploy `coda-mlflow-oss` (Lakebase-backed) on ...2455. Confirm its URL matches
`MLFLOW_OSS_URL` above (deterministic mirror name).

### 4. Grant the CoDA SP CAN_USE on the OSS app (your PAT)
```bash
./grant_mlflow_host.sh --profile <coles-or-terminal-auth> \
  --coda-app <coda-app-name> --mlflow-app coda-mlflow-oss
```
Databricks Apps reject PATs/user bearers (302 → OIDC); only an SP M2M token from a
`CAN_USE`-granted caller reaches the OSS app. Idempotent; persists across redeploys.

### 5. Enable OSS tracing + redeploy
Flip `MLFLOW_OSS_TRACKING_ENABLED` → `true` in `app.yaml`, redeploy (git push).
Now Claude Code (spec-B) and the content-filter proxy for OpenCode/Hermes/Pi
(spec-D) send traces to `MLFLOW_OSS_URL` as the granted SP.

**Verify:** run an agent turn, confirm a trace lands in the OSS MLflow app. A
302/hang means the CAN_USE grant (step 4) didn't take.

## Gotchas (from prior CoDA deploys)

- **Region shard:** all Coles URLs are `.15`, not daveok's `.18`. A blind
  ID-only find-replace leaves a broken host.
- **App-resource limits:** `app.yaml` carries only `valueFrom:` secret refs. It
  cannot declare serving/volume/app grants — those are UI/DAB resource bindings or
  `grant_*.sh`. This repo has no DAB (`databricks.yml`), so use the scripts/UI.
- **Redeploy churn:** rapid `apps deploy` cycles can wedge boot
  ("did not start within 10 min"). If it hangs, stop → start, and isolate before
  blaming code.
- **`.databricksignore`:** confirm it excludes `vendor/`, `.venv/`, and any
  `auth.json`/storage-state files before the git-deploy sync uploads untracked files.
