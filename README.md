# Coding Agents on Databricks Apps

[![Use this template](https://img.shields.io/badge/Use%20this%20template-2ea44f?logo=github)](https://github.com/datasciencemonkey/coding-agents-databricks-apps/generate)
[![Deploy to Databricks](https://img.shields.io/badge/Deploy-Databricks%20Apps-FF3621?logo=databricks&logoColor=white)](docs/deployment.md)
[![Agents](https://img.shields.io/badge/Agents-4%20included-green)](#whats-inside)
[![Skills](https://img.shields.io/badge/Skills-39%20built--in-blue)](#-all-39-skills)

> Run Claude Code, Codex, Gemini CLI, and OpenCode in your browser — zero setup, wired to your Databricks workspace.

<!-- TODO: Add demo GIF here — screen recording of terminal in action -->

---

## What's Inside

🟠 **Claude Code** — Anthropic's coding agent with 39 Databricks skills + 2 MCP servers

🟣 **Codex** — OpenAI's coding agent, pre-configured for Databricks

🔵 **Gemini CLI** — Google's coding agent with shared skills

🟢 **OpenCode** — Open-source agent with multi-provider support

Every agent starts **pre-wired to your Databricks AI Gateway** — models, auth tokens, and base URLs are all configured at boot. No API keys to manage.

---

## Why Databricks

This isn't just a terminal in the cloud. Running coding agents on Databricks gives you enterprise-grade infrastructure out of the box:

| | Benefit | What you get |
|---|---|---|
| 🔐 | **Unity Catalog Integration** | All data access governed by UC permissions — agents can only touch what your identity allows |
| 🤖 | **AI Gateway** | Route all LLM calls through a single control plane — swap models, set rate limits, and manage API keys centrally |
| 🔀 | **Multi-AI & Multi-Agent** | Switch between Claude, GPT, Gemini, and open-source models on the fly — change the model or agent without redeploying |
| 📊 | **Consumption Monitoring** | Track token usage, cost, and latency per user and per model via the AI Gateway control center dashboard |
| 🔍 | **MLflow Tracing** | Every Claude Code session is automatically traced — review prompts, tool calls, and outputs in your MLflow experiment |
| 🧬 | **Assess Traces with Genie** | Point Genie at your MLflow traces to ask natural-language questions about agent behavior, cost patterns, and session quality |
| 📝 | **App Logs to Delta** | Optionally route application logs to Delta tables for long-term retention, querying, and dashboarding |

---

## Terminal Features

| | |
|---|---|
| 🎨 **8 Themes** | Dracula, Nord, Solarized, Monokai, GitHub Dark, and more |
| ✂️ **Split Panes** | Run two sessions side by side with a draggable divider |
| 🔍 **Search** | Find anything in your terminal history (Ctrl+Shift+F) |
| 🎤 **Voice Input** | Dictate commands with your mic (Option+V) |
| 📋 **Image Paste** | Paste or drag-and-drop images into the terminal — saved to `~/uploads/`, path inserted automatically |
| ⌨️ **Customizable** | Fonts, font sizes, themes — all persisted across sessions |
| 🐍 **Loading Screen** | Play snake while setup steps run in parallel |
| 🔄 **Workspace Sync** | Every `git commit` auto-syncs to `/Workspace/Users/{you}/projects/` |
| ✏️ **Micro Editor** | Modern terminal editor, pre-installed |
| ⚙️ **Databricks CLI** | Pre-configured with your PAT, ready to go |
| 📊 **MLflow Tracing** | Every Claude Code session is automatically traced to your Databricks MLflow experiment |

---

## MLflow Tracing

Every Claude Code session is **automatically traced** to a Databricks MLflow experiment — zero configuration required.

### How it works

```
Claude Code session starts
        │
        ▼
   Environment vars set automatically:
   MLFLOW_TRACKING_URI=databricks
   MLFLOW_EXPERIMENT_NAME=/Users/{you}/{app-name}
        │
        ▼
   You work normally — code, debug, deploy
        │
        ▼
   Session ends → Stop hook fires
        │
        ▼
   Full session transcript logged as an MLflow trace
   at /Users/{you}/{app-name} in your workspace
```

### What gets traced

When a Claude Code session ends, the **Stop hook** automatically calls `mlflow.claude_code.hooks.stop_hook_handler()`, which captures the full session transcript — your prompts, agent actions, tool calls, and outputs — and logs it as an MLflow trace.

### Where traces live

Traces are stored in a Databricks MLflow experiment at:

```
/Users/{your-email}/{app-name}
```

For example, if you're `jane@company.com` and your app is named `coding-agents`:

```
/Users/jane@company.com/coding-agents
```

View them in the Databricks UI: **Workspace > Machine Learning > Experiments**.

### Configuration

Tracing is configured during app startup by `setup_mlflow.py`, which merges the following into `~/.claude/settings.json`:

| Setting | Value | Purpose |
|---------|-------|---------|
| `MLFLOW_CLAUDE_TRACING_ENABLED` | `true` | Enables Claude Code tracing |
| `MLFLOW_TRACKING_URI` | `databricks` | Routes traces to Databricks backend |
| `MLFLOW_EXPERIMENT_NAME` | `/Users/{owner}/{app}` | Target experiment path |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `""` | Overrides container OTEL to prevent trace loss |
| Stop hook | `uv run python -c "from mlflow.claude_code.hooks import stop_hook_handler; stop_hook_handler()"` | Fires on session end |

Tracing is skipped gracefully if `APP_OWNER` is not set (e.g., local dev without Databricks).

---

## Quick Start

### Deploy to Databricks Apps

1. Click [**Use this template**](https://github.com/datasciencemonkey/coding-agents-databricks-apps/generate) to create your own repo
2. Go to **Databricks → Apps → Create App**
3. Choose **Custom App** and connect your new repo
4. Add your PAT as the `DATABRICKS_TOKEN` secret in **App Resources**
5. Deploy

That's it. Open the app URL and start coding.

[→ Full deployment guide](docs/deployment.md) — environment variables, gateway config, and advanced options.

### Run locally

1. Click [**Use this template**](https://github.com/datasciencemonkey/coding-agents-databricks-apps/generate) to create your own repo
2. Clone your new repo and run:

```bash
git clone https://github.com/<you>/<your-repo>.git
cd <your-repo>
uv run python app.py
```

Open [http://localhost:8000](http://localhost:8000) — type `claude`, `codex`, `gemini`, or `opencode` to start coding.

---

## Why This Exists

On Jan 26, 2026, Andrej Karpathy made [this viral tweet](https://x.com/karpathy/status/2015883857489522876?s=46&t=tEsLJXJnGFIkaWs-Bhs1yA) about the future of coding. Boris Cherny, the creator of Claude Code, responded:

![Boris Cherny's response](image.png)

This template repo opens that vision up for every Databricks user — no IDE setup, no local installs. Click "Use this template", deploy to Databricks Apps, and start coding with AI in your browser.

---

<details>
<summary><strong>🧠 All 39 Skills</strong></summary>

### Databricks Skills (25) — [ai-dev-kit](https://github.com/databricks-solutions/ai-dev-kit)

| Category | Skills |
|----------|--------|
| AI & Agents | agent-bricks, genie, mlflow-eval, model-serving |
| Analytics | aibi-dashboards, unity-catalog, metric-views |
| Data Engineering | declarative-pipelines, jobs, structured-streaming, synthetic-data, zerobus-ingest |
| Development | asset-bundles, app-apx, app-python, python-sdk, config, spark-python-data-source |
| Storage | lakebase-autoscale, lakebase-provisioned, vector-search |
| Reference | docs, dbsql, pdf-generation |
| Meta | refresh-databricks-skills |

### Superpowers Skills (14) — [obra/superpowers](https://github.com/obra/superpowers)

| Category | Skills |
|----------|--------|
| Build | brainstorming, writing-plans, executing-plans |
| Code | test-driven-dev, subagent-driven-dev |
| Debug | systematic-debugging, verification |
| Review | requesting-review, receiving-review |
| Ship | finishing-branch, git-worktrees |
| Meta | dispatching-agents, writing-skills, using-superpowers |

</details>

<details>
<summary><strong>🔌 2 MCP Servers</strong></summary>

| Server | What it does |
|--------|-------------|
| **DeepWiki** | Ask questions about any GitHub repo — gets AI-powered answers from the codebase |
| **Exa** | Web search and code context retrieval for up-to-date information |

</details>

<details>
<summary><strong>🏗️ Architecture</strong></summary>

```
┌─────────────────────┐     HTTP      ┌─────────────────────┐
│   Browser Client    │◄────────────►│   Gunicorn + Flask   │
│   (xterm.js)        │   Polling     │   (PTY Manager)     │
└─────────────────────┘               └─────────────────────┘
         │                                     │
         │ on first load                       │ on startup
         ▼                                     ▼
┌─────────────────────┐               ┌─────────────────────┐
│   Loading Screen    │               │   Background Setup  │
│   (snake game)      │               │   (8 setup steps)   │
└─────────────────────┘               └─────────────────────┘
                                               │
                                               ▼
                                      ┌─────────────────────┐
                                      │   Shell Process     │
                                      │   (/bin/bash)       │
                                      └─────────────────────┘
```

### Startup Flow

1. Gunicorn starts, calls `initialize_app()` via `post_worker_init` hook
2. App immediately serves the loading screen (snake game)
3. Background thread runs setup: git config, micro editor, Claude CLI, Codex CLI, OpenCode, Gemini CLI, Databricks CLI, MLflow tracing
4. `/api/setup-status` endpoint reports progress to the loading screen
5. Once complete, the loading screen transitions to the terminal UI

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Loading screen (during setup) or terminal UI |
| `/health` | GET | Health check with session count and setup status |
| `/api/setup-status` | GET | Setup progress for loading screen |
| `/api/session` | POST | Create new terminal session |
| `/api/input` | POST | Send input to terminal |
| `/api/output` | POST | Poll for terminal output |
| `/api/resize` | POST | Resize terminal dimensions |
| `/api/upload` | POST | Upload file (clipboard image paste) |
| `/api/session/close` | POST | Close terminal session |

</details>

<details>
<summary><strong>⚙️ Configuration</strong></summary>

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABRICKS_TOKEN` | Yes | Your Personal Access Token (secret) |
| `HOME` | Yes | Set to `/app/python/source_code` in app.yaml |
| `ANTHROPIC_MODEL` | No | Claude model name (default: `databricks-claude-opus-4-6`) |
| `CODEX_MODEL` | No | Codex model name (default: `databricks-gpt-5-2`) |
| `GEMINI_MODEL` | No | Gemini model name (default: `databricks-gemini-3-1-pro`) |
| `DATABRICKS_GATEWAY_HOST` | No | AI Gateway URL (recommended) |

### Security Model

Single-user app — each user deploys their own instance with their own PAT. Only the token owner can access the terminal. Everyone else sees 403.

### Gunicorn

Production uses `workers=1` (PTY state is process-local), `threads=8` (concurrent polling), `gthread` worker class.

</details>

<details>
<summary><strong>📁 Project Structure</strong></summary>

```
coding-agents-in-databricks/
├── app.py                   # Flask backend + PTY management + setup orchestration
├── app.yaml.template        # Databricks Apps deployment config template
├── gunicorn.conf.py         # Gunicorn production server config
├── requirements.txt         # Python dependencies
├── setup_claude.py          # Claude Code CLI + MCP configuration
├── setup_codex.py           # Codex CLI configuration
├── setup_gemini.py          # Gemini CLI configuration
├── setup_opencode.py        # OpenCode configuration
├── setup_databricks.py      # Databricks CLI configuration
├── setup_mlflow.py          # MLflow tracing auto-configuration
├── sync_to_workspace.py     # Post-commit hook: sync to Workspace
├── install_micro.sh         # Micro editor installer
├── static/
│   ├── index.html           # Terminal UI (xterm.js + split panes)
│   ├── loading.html         # Loading screen with snake game
│   └── lib/                 # xterm.js library files
├── .claude/
│   └── skills/              # 39 pre-installed skills
└── docs/
    ├── deployment.md        # Full Databricks Apps deployment guide
    └── plans/               # Design documentation
```

</details>

---

## Technologies

Flask · Gunicorn · xterm.js · Python PTY · Databricks SDK · Databricks AI Gateway · MLflow