# Deploy to Databricks Apps

## Prerequisites

- A Databricks workspace with Model Serving endpoints enabled

## Easy Start (Git Repo)

The simplest way — no CLI, no cloning, everything stays in the Databricks UI.

1. Go to **Databricks → Apps → Create App**
2. Choose **Custom App** and connect this Git repo:
   ```
   https://github.com/databrickslabs/coding-agents-databricks-apps.git
   ```
3. Click **Deploy**
4. Open the app — on first terminal session, paste a short-lived PAT when prompted

The app pulls the code directly from Git. To update later, just re-deploy — it picks up the latest from the repo.

> **Note:** On first startup, the app automatically removes the template's `.git` history and reinitializes a clean, remote-free git repo. This prevents accidental pushes back to the template repo from the in-browser terminal.

> **Optional (Highly Recommended):** If you use [Databricks AI Gateway](https://docs.databricks.com/aws/en/ai-gateway/), also add `DATABRICKS_GATEWAY_HOST` as a secret or environment variable. Otherwise the app falls back to direct model serving endpoints.

## Alternative: Deploy with CLI

If you prefer working from the terminal or need more control:

### 1. Clone the repo into your workspace

```bash
databricks repos create \
  --url https://github.com/datasciencemonkey/coding-agents-databricks-apps.git \
  --path /Workspace/Users/<your-email>/apps/coding-agents-databricks-apps
```

### 2. Configure `app.yaml`

In the cloned workspace folder, copy the template and edit it:

```bash
cp app.yaml.template app.yaml
```

Set your `DATABRICKS_GATEWAY_HOST`, or remove the gateway lines to fall back to direct model serving endpoints.

### 3. Create the app and deploy

```bash
databricks apps create <your-app-name>
```

No secrets or resources to configure. On first terminal session, paste a short-lived PAT when prompted — all CLIs are configured automatically.

### 4. Deploy

```bash
databricks apps deploy <your-app-name> \
  --source-code-path /Workspace/Users/<your-email>/apps/coding-agents-databricks-apps
```

> **Tip:** To update later, just `git pull` in the workspace repo and re-deploy.

## Deploy from a Git repository (native)

Databricks Apps can deploy **directly from a Git ref** — no sync-to-workspace
step. This is how the `coda-01..08` fleet runs. The `Makefile` wraps it:

```bash
# 1. Attach the repo to the app (idempotent; creates the app if needed)
make configure-git APP_NAME=coda-04 PROFILE=<profile>

# 2. Private repo? Add a Git credential to the app SP (needs CAN MANAGE on the SP).
#    Token is read from stdin so it never hits a command line / shell history.
gh auth token | make configure-git-credential APP_NAME=coda-04 PROFILE=<profile>

# 3. Deploy from a ref (branch | tag | commit)
make deploy-git   APP_NAME=coda-04 PROFILE=<profile> GIT_REF=main
make redeploy-git APP_NAME=coda-04 PROFILE=<profile> GIT_REF=main   # + (re)grant Omnigent IAM
```

Overridable vars: `GIT_URL`, `GIT_PROVIDER` (`gitHub`, `gitLab`, …), `GIT_REF`,
`GIT_REF_TYPE` (`branch` | `tag` | `commit`).

Raw CLI equivalents:

```bash
databricks apps create-update <app> --json '{"update_mask":"git_repository","git_repository":{"url":"<URL>","provider":"gitHub"}}'
databricks apps deploy        <app> --json '{"git_source":{"branch":"main"}}'   # or {"tag":...} / {"commit":...}
```

> **Note:** apps created before Git-deploy went GA may not grant the creator
> `CAN MANAGE` on the app SP. If adding a Git credential fails, ask a workspace
> admin to grant `CAN MANAGE` on the service principal first.

## Omnigent host permissions

When an app registers as an **Omnigent host** (`OMNIGENTS_SERVER_URL` set), its
service principal — which starts with **zero** privileges — needs a specific IAM
set, or the host silently never appears in the Omnigent picker.

`make grant-omnigent-host` (run automatically by `deploy` / `redeploy` /
`redeploy-git`) grants, via `grant_omnigent_host.sh`:

1. **`CAN_USE`** on the Omnigent server app — else the host tunnel's WebSocket
   upgrade is rejected at the Apps edge and the host never registers.
2. **The full Unity Catalog traversal chain** to the wheel volume
   (`OMNIGENTS_WHEEL_SPEC` = `/Volumes/<cat>/<schema>/<vol>`):
   - `USE_CATALOG` on the catalog
   - `USE_SCHEMA` on the schema
   - `READ_VOLUME` + `WRITE_VOLUME` on the volume

> **`READ_VOLUME` alone is a silent trap.** Without `USE_CATALOG`/`USE_SCHEMA`
> the SP cannot traverse to the volume; the wheel download fails with
> `User does not have USE CATALOG on Catalog '<cat>'`, the `omnigent` CLI never
> installs, and the host never registers — while every "grant" still reports
> green. This bit us in production (2026-07-11).

**Group option.** Instead of per-SP grants you can grant a **group** the chain
and add the app SPs to it. Gotcha: **account-federated** groups cannot have
members edited via the workspace SCIM `preview` endpoint
(`"can only be managed in account"`) — use the account SCIM proxy the UI uses,
`PATCH /api/2.0/account/scim/v2/Groups/{id}` (works with a workspace-admin PAT).
Membership uses the SP's SCIM `id` (== the app's `service_principal_id`), not
its `client_id`.

**Restart after a late grant.** Grants persist, but an app that **booted before**
its grants landed won't retroactively install the CLI — the boot-time install
does not retry in-process. **Restart the app** to re-run install/launch:

```bash
databricks apps stop  <app> --profile <p> && databricks apps start <app> --profile <p>
```

**Verify** the host is live:

```bash
# SP can now traverse to the wheel volume
databricks fs ls "dbfs:/Volumes/<cat>/<schema>/<vol>" --profile <sp-profile>

# In-container after a good boot
which omnigent && ls ~/.omnigent/logs/host-runner/

# On the server (as the SP): GET /v1/hosts lists the deterministic host_id
#   host_id = "host_" + sha256("coda-omnigents-host:<sp_client_id>")[:32]
```

If the host is registered but not in *your* picker, it's SP-owned — share it to
your user (owner-gated `POST /api/omnigent-host/share`).

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABRICKS_TOKEN` | No | Optional. If not set, the app prompts for a token on first session. Auto-rotated every 10 minutes |
| `HOME` | Yes | Set to `/app/python/source_code` in app.yaml |
| `ANTHROPIC_MODEL` | No | Claude model name (default: `databricks-claude-opus-4-8`) |
| `PI_MODEL` | No | Pi model name — same `/anthropic` gateway route as Claude (default: `databricks-claude-opus-4-8`) |
| `ENABLE_PI` | No | Set `false` to skip installing the Pi coding agent (default: `true`) |
| `ENABLE_CODEX` | No | Set `false` to skip installing Codex (default: `true`) |
| `CODEX_MODEL` | No | Codex Responses-API model name (default: `databricks-gpt-5-3-codex`) |
| `ENABLE_GEMINI` | No | Set `false` to skip installing Gemini CLI (default: `true`) |
| `GEMINI_MODEL` | No | Gemini model name (default: `databricks-gemini-2-5-pro`) |
| `HERMES_MODEL` | No | Hermes model name (default: `databricks-claude-opus-4-8`) |
| `DATABRICKS_GATEWAY_HOST` | No | AI Gateway URL override. Auto-discovered from `DATABRICKS_WORKSPACE_ID` if unset. Falls back to direct model serving if neither is available |
| `MAX_CONCURRENT_SESSIONS` | No | Browser PTY hard cap per worker (default `5`) |
| `CODA_MEMORY_HIGH_WATERMARK_PERCENT` / `CODA_MEMORY_RESUME_THRESHOLD_PERCENT` | No | cgroup admission hysteresis (defaults `80` / `70`) |
| `CODA_BROWSER_SESSION_RESERVE_MB` | No | Reserve required for each new browser PTY (default `768` MB) |
| `CODA_CGROUP_V2_ROOT` | No | Optional cgroup root for deterministic tests or non-default layouts |

The memory gate uses the container's own cgroup accounting. It subtracts both
active and inactive reclaimable file cache, keeps swap-backed shared memory in
the working set, and falls back to the fixed session cap when accounting is
unavailable. Status responses distinguish unavailable telemetry from healthy
low usage.

## Security Model

This is a **single-user, zero-config auth** app. No secrets or tokens are required at deploy time.

1. **Owner resolution**: The app owner is determined from `app.creator` via the service principal + Apps API — no PAT needed
2. **Authorization**: Each request's `X-Forwarded-Email` header is compared against `app.creator`. Non-matching users see 403
3. **Interactive PAT setup**: On first terminal session, the user pastes a short-lived PAT interactively. All CLIs (Claude, Codex, OpenCode, Gemini, Hermes, Databricks) are configured automatically
4. **Auto-rotation**: PAT rotates every 10 minutes with a 15-minute lifetime. Old tokens are proactively revoked. Maximum leaked-token exposure: 15 minutes
5. **Session-aware**: Rotation is skipped when no active terminal sessions exist
6. **On restart**: The user re-pastes a token (no persistence by design)

## Gunicorn Configuration

Production uses Gunicorn (`gunicorn.conf.py`) with:
- `workers=1` — PTY file descriptors and in-memory session state can't survive forking
- `threads=8` — Handles concurrent polling from the terminal client
- `worker_class=gthread` — Single process + thread pool
- `post_worker_init` hook calls `initialize_app()` to start setup

## Workspace Sync

Git commits automatically sync projects to Databricks Workspace:

```
/Workspace/Shared/coda/{app-name}/{project-name}/
```

The post-commit hook uses `nohup ... & disown` to ensure the sync process survives across all coding agents, since some agents kill the entire process group when a shell command finishes.
