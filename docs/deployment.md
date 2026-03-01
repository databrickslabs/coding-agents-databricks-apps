# Deploy to Databricks Apps

## Prerequisites

- A Databricks workspace with Model Serving endpoints enabled
- A Personal Access Token (PAT)

## Easy Start (Git Repo)

The simplest way — no CLI, no cloning, everything stays in the Databricks UI.

1. Go to **Databricks → Apps → Create App**
2. Choose **Custom App** and connect this Git repo:
   ```
   https://github.com/datasciencemonkey/coding-agents-databricks-apps.git
   ```
3. In the **App Resources** tab, add your PAT as the `DATABRICKS_TOKEN` secret
4. Click **Deploy**

The app pulls the code directly from Git. To update later, just re-deploy — it picks up the latest from the repo.

> **Optional:** If you use [Databricks AI Gateway](https://docs.databricks.com/aws/en/ai-gateway/), also add `DATABRICKS_GATEWAY_HOST` as a secret or environment variable. Otherwise the app falls back to direct model serving endpoints.

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

### 3. Create the app and add your token

```bash
databricks apps create <your-app-name>
```

In the [App Resources tab](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/resources), add your PAT as the `DATABRICKS_TOKEN` secret.

### 4. Deploy

```bash
databricks apps deploy <your-app-name> \
  --source-code-path /Workspace/Users/<your-email>/apps/coding-agents-databricks-apps
```

> **Tip:** To update later, just `git pull` in the workspace repo and re-deploy.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABRICKS_TOKEN` | Yes | Your Personal Access Token (secret) |
| `HOME` | Yes | Set to `/app/python/source_code` in app.yaml |
| `ANTHROPIC_MODEL` | No | Claude model name (default: `databricks-claude-opus-4-6`) |
| `CODEX_MODEL` | No | Codex model name (default: `databricks-gpt-5-2`) |
| `GEMINI_MODEL` | No | Gemini model name (default: `databricks-gemini-3-1-pro`) |
| `DATABRICKS_GATEWAY_HOST` | No | AI Gateway URL (recommended). Falls back to direct model serving if unset |

## Security Model

This is a **single-user app**. Each user deploys their own instance with their own PAT:

1. The `DATABRICKS_TOKEN` in `app.yaml` identifies the owner
2. At startup, the app determines the token owner via Databricks API
3. Only requests from the token owner are allowed
4. Other users see a 403 Forbidden error

## Gunicorn Configuration

Production uses Gunicorn (`gunicorn.conf.py`) with:
- `workers=1` — PTY file descriptors and in-memory session state can't survive forking
- `threads=8` — Handles concurrent polling from the terminal client
- `worker_class=gthread` — Single process + thread pool
- `post_worker_init` hook calls `initialize_app()` to start setup

## Workspace Sync

Git commits automatically sync projects to Databricks Workspace:

```
/Workspace/Users/{email}/projects/{project-name}/
```

The post-commit hook uses `nohup ... & disown` to ensure the sync process survives across all coding agents, since some agents kill the entire process group when a shell command finishes.
