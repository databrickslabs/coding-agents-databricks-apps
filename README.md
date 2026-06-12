# Coding Agents on Databricks Apps


[![Use this template](https://img.shields.io/badge/Use%20this%20template-2ea44f?logo=github)](https://github.com/datasciencemonkey/coding-agents-databricks-apps/generate)
[![Deploy to Databricks](https://img.shields.io/badge/Deploy-Databricks%20Apps-FF3621?logo=databricks&logoColor=white)](docs/deployment.md)
[![Agents](https://img.shields.io/badge/Agents-5%20included-green)](#whats-inside)
[![Skills](https://img.shields.io/badge/Skills-43%20built--in-blue)](#-all-43-skills)

> Run Claude Code, Codex, Gemini CLI, Hermes Agent, and OpenCode in your browser — zero setup, wired to your Databricks workspace.

---

<div align="center">
  <video src="https://github.com/user-attachments/assets/40405b46-532a-4f14-82e3-414cb3744684" controls width="900"></video>
</div>

## Screenshots

<div align="center">
  <img src="docs/screenshots/demo.gif" width="900" alt="CODA demo — splash screen, multi-tab terminals, keyboard shortcuts"/>
</div>

---

## Architecture

<div align="center">
  <img src="docs/screenshots/coda-architecture.png" width="900" alt="CoDA architecture — always-on coding agents inside the customer's Databricks tenancy, governed by Unity Catalog and audited by MLflow"/>
</div>

CoDA runs as a hosted Databricks App inside your tenancy, alongside **Genie Code** — Databricks' in-product AI coding agent that lives in notebooks, the SQL editor, and dashboards. Genie Code is the interactive in-product surface; CoDA is the always-on hosted-app surface where Developers brief the agents through the browser and Claude Code, Codex, Gemini CLI, and OpenCode execute alongside the Hermes orchestrator. Both surfaces share the same access plane: every model call routes through Foundation Model APIs (no third-party egress) and every tool call routes through Governed MCP Servers (Unity Catalog ACLs + MLflow trace + named human identity). The result: agentic coding for legacy migration, application development, multi-repo refactor, production monitoring, code modernisation, and CI/CD deployments — all governed like any other workload.

---

## What's Inside

🟠 **Claude Code** — Anthropic's coding agent with 39 Databricks skills + 2 MCP servers

🟣 **Codex** — OpenAI's coding agent, pre-configured for Databricks

🔵 **Gemini CLI** — Google's coding agent with shared skills

🟡 **Hermes Agent** — NousResearch's multi-provider AI CLI with tool-calling and skills

🟢 **OpenCode** — Open-source agent with multi-provider support

Every agent installs at boot and connects to your **Databricks AI Gateway** — on first terminal session, paste a short-lived PAT and all CLIs are configured automatically. Token auto-rotates every 10 minutes.

### 📺 Setup walkthrough (6 min)

Want to see CoDA installed and running end-to-end? Click the thumbnail to watch the full walkthrough on YouTube.

<div align="center">
  <a href="https://youtu.be/ofqBQ26_e9o">
    <img src="docs/screenshots/setup-walkthrough-poster.jpg" width="900" alt="Getting Started with CoDA — 6-minute setup walkthrough (click to watch on YouTube)"/>
  </a>
</div>

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
| 🌐 **WebSocket I/O** | Real-time terminal output over WebSocket — zero-latency, eliminates polling delay |
| 🔁 **HTTP Polling Fallback** | Automatic fallback via Web Worker when WebSocket is unavailable |
| 🚀 **Parallel Setup** | 6 agent setups run in parallel (~5x faster startup) |
| 🔍 **Search** | Find anything in your terminal history (Ctrl+Shift+F) |
| 🎤 **Voice Input** | Dictate commands with your mic (Option+V) |
| 📋 **Image Paste** | Paste or drag-and-drop images into the terminal — saved to `~/uploads/`, path inserted automatically |
| ⌨️ **Customizable** | Fonts, font sizes, themes — all persisted across sessions |
| 🔄 **Workspace Sync** | Every `git commit` auto-syncs to `/Workspace/Users/{you}/projects/` |
| ✏️ **Micro Editor** | Modern terminal editor, pre-installed |
| ⚙️ **Databricks CLI** | Installed at boot, configured interactively on first session |
| 📊 **MLflow Tracing** | Every Claude Code session is automatically traced to your Databricks MLflow experiment |

---

## MLflow Tracing

Claude Code and Codex sessions can both be **automatically traced** to a single Databricks MLflow experiment — flip one switch to turn them on.

### Turning it on

Set **`MLFLOW_TRACING_ENABLED=true`** in `app.yaml` (or your shell for local dev). That single variable enables tracing for both CLIs. Tracing is **off by default** to keep deploys lightweight — opt in when you want it.

```yaml
# app.yaml
env:
  - name: MLFLOW_TRACING_ENABLED
    value: "true"
```

### How it works

```
MLFLOW_TRACING_ENABLED=true
        │
        ├──► Claude Code: Stop hook fires on session end →
        │     mlflow.claude_code.hooks.stop_hook_handler() logs the transcript
        │
        └──► Codex: @mlflow/codex notify hook fires after each turn →
              trace appended to the experiment
```

Both land in the same MLflow experiment, so you can compare runs across agents side by side.

### Where traces live

```
/Users/{your-email}/{app-name}
```

For example, if you're `jane@company.com` and your app is named `coding-agents`:

```
/Users/jane@company.com/coding-agents
```

View them in the Databricks UI: **Workspace > Machine Learning > Experiments**.

### Configuration

Tracing is wired up during app startup:

| Setting | Value | Purpose |
|---------|-------|---------|
| `MLFLOW_TRACING_ENABLED` | `true`/`false` (default `false`) | Master switch for Claude + Codex |
| `MLFLOW_CLAUDE_TRACING_ENABLED` | mirrors `MLFLOW_TRACING_ENABLED` | Gates Claude's Stop hook at runtime |
| `MLFLOW_TRACKING_URI` | `databricks` | Routes traces to the Databricks backend |
| `MLFLOW_EXPERIMENT_NAME` | `/Users/{owner}/{app}` | Target experiment path |
| `MLFLOW_EXPERIMENT_ID` | resolved from name | Set in `~/.codex/.env` (Codex needs an ID) |

Tracing setup is skipped gracefully when `APP_OWNER` is not set (e.g., local dev without Databricks) or when `MLFLOW_TRACING_ENABLED` is left at its default `false`.

---

## Quick Start

### Deploy to Databricks Apps

1. Click [**Use this template**](https://github.com/datasciencemonkey/coding-agents-databricks-apps/generate) to create your own repo
2. Go to **Databricks → Apps → Create App**
3. Choose **Custom App** and connect your new repo
4. Deploy
5. Open the app — paste a short-lived PAT when prompted on first terminal session

That's it. No secrets to configure, no pre-deployment setup.

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

<details>
<summary><strong>🧠 All 43 Skills</strong></summary>

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

### BDD Skills (4)

| Category | Skills |
|----------|--------|
| Testing | bdd-features, bdd-run, bdd-scaffold, bdd-steps |

</details>

<details>
<summary><strong>🔌 MCP Servers</strong></summary>

### Built-in MCP Clients

| Server | What it does |
|--------|-------------|
| **DeepWiki** | Ask questions about any GitHub repo — gets AI-powered answers from the codebase |
| **Exa** | Web search and code context retrieval for up-to-date information |

### CoDA MCP Server (exposed at `/mcp`)

CoDA itself exposes an **MCP server** that any MCP-compatible client can connect to — delegate coding tasks to AI agents running on Databricks, without needing the terminal UI.

| Tool | Purpose |
|------|---------|
| `coda_run` | Fire-and-forget: submit a coding task, get back immediately |
| `coda_inbox` | Dashboard: see all running/completed/failed tasks at a glance |
| `coda_get_result` | Pull the full structured result of a completed task |

**Why this matters:** Any tool that speaks MCP can use your Databricks-hosted coding agents — no custom integration needed.

#### Example: Databricks Genie Code

Genie Code connects to CoDA's MCP endpoint and delegates coding work to agents running in the background:

```
User → Genie Code: "Build me a sales pipeline using the transactions table"

Genie Code calls coda_run(prompt="Build a sales pipeline...", email="user@company.com",
                          context='{"tables": ["sales.transactions"]}')

→ Returns immediately: {task_id: "task-abc", status: "running"}
→ User keeps chatting with Genie Code while the agent works

User → Genie Code: "How's my pipeline coming?"

Genie Code calls coda_inbox()
→ {tasks: [{task_id: "task-abc", status: "completed", summary: "Built pipeline.py..."}]}

Genie Code calls coda_get_result(task_id="task-abc", session_id="sess-123")
→ {summary: "Created pipeline.py with 3 stages", files_changed: ["pipeline.py"], ...}
```

#### Connecting MCP Clients (Claude Code, Claude Desktop, Cursor, etc.)

Databricks Apps use OAuth — not PATs — for authentication. A static `Authorization: Bearer <PAT>` header will get a `302` redirect to the OAuth login page. To connect any MCP client, use the **stdio bridge** (`tools/coda-bridge.py`) which injects fresh OAuth tokens automatically via `databricks auth token`.

**1. Copy the bridge script:**

```bash
mkdir -p ~/.claude/mcp-bridges
cp tools/coda-bridge.py ~/.claude/mcp-bridges/
```

**2. Add to your MCP client settings** (e.g. `~/.claude/settings.json`):

```json
"coda-mcp": {
    "type": "stdio",
    "command": "python3",
    "args": ["/path/to/.claude/mcp-bridges/coda-bridge.py"],
    "env": {
        "CODA_MCP_URL": "https://your-app.databricksapps.com/mcp",
        "DATABRICKS_PROFILE": "your-profile"
    }
}
```

**3. Restart your MCP client.**

The bridge reads `CODA_MCP_URL` and `DATABRICKS_PROFILE` from environment — no hardcoded values. If you redeploy the app or switch workspaces, just update the `env` block.

**Prerequisites:** `databricks` CLI installed and authenticated (`databricks auth login -p <profile>`), Python 3.8+, no pip dependencies.

**Troubleshooting:** Bridge logs go to stderr. If you see `Auth failed (302)`, refresh your CLI session with `databricks auth login -p <profile>`. See [full setup guide](docs/mcp-client-setup.md) for details.

#### Task Chaining

Chain tasks by passing `previous_session_id` — the new agent reads the prior task's results for context:

```
coda_run(prompt="Add monitoring to the pipeline", previous_session_id="sess-123")
```

See [MCP v2 Design Doc](docs/mcp-v2-background-execution.md) for the full protocol reference.

</details>

<details>
<summary><strong>🏗️ Architecture</strong></summary>

```
┌─────────────────────┐  WebSocket    ┌──────────────────────────────────┐
│   Browser Client    │◄═══════════►│   uvicorn (ASGI)                  │
│   (xterm.js)        │  (fallback)   │   ├─ python-socketio (Socket.IO) │
│                     │───────────►│   ├─ FastMCP /mcp                │
│                     │  HTTP Poll    │   └─ WSGIMiddleware(Flask + PTY) │
│                     │  (primary     │                                  │
│                     │   under uvicorn)                                │
└─────────────────────┘               └──────────────────────────────────┘
         │                                     │
         │ on first load                       │ on startup
         ▼                                     ▼
┌─────────────────────┐               ┌─────────────────────┐
│   Setup Progress    │               │   Background Setup  │
│   (inline UI)       │               │   (11 steps, 5→6 ║) │
└─────────────────────┘               └─────────────────────┘
                                               │
                                               ▼
                                      ┌─────────────────────┐
                                      │   Shell Process     │
                                      │   (/bin/bash)       │
                                      └─────────────────────┘
```

### Startup Flow

1. uvicorn starts `coda_mcp.mcp_asgi:app`, which calls `initialize_app()` during ASGI lifespan startup (Flask mounted via `WSGIMiddleware`; MCP mounted at `/mcp` via native ASGI; Socket.IO wraps both)
2. App serves the terminal UI with inline setup progress
3. Background thread runs setup: 5 sequential steps (git config, micro editor, GitHub CLI, Databricks CLI upgrade, content-filter proxy), then 6 agent setups (`setup/setup_claude.py`, `setup/setup_codex.py`, etc.) run in parallel via `ThreadPoolExecutor`
4. `/api/setup-status` endpoint reports progress to the UI
5. Once complete, the terminal becomes interactive

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Terminal UI with inline setup progress |
| `/health` | GET | Health check with session count and setup status |
| `/api/setup-status` | GET | Setup progress for the UI |
| `/api/app-state` | GET | Persisted app state (owner, last rotation) |
| `/api/version` | GET | App version |
| `/api/sessions` | GET | List active (non-exited) sessions with metadata |
| `/api/pat-status` | GET | Whether a valid, usable PAT is currently configured |
| `/api/configure-pat` | POST | Interactive first-session PAT setup |
| `/api/session` | POST | Create new terminal session |
| `/api/session/attach` | POST | Reattach to an existing session (replays buffered output) |
| `/api/input` | POST | Send input to terminal |
| `/api/output` | POST | Poll for terminal output (single session) |
| `/api/output-batch` | POST | Batch poll output for multiple sessions |
| `/api/heartbeat` | POST | Lightweight keepalive (no buffer drain) |
| `/api/resize` | POST | Resize terminal dimensions |
| `/api/upload` | POST | Upload file (clipboard image paste) |
| `/api/session/close` | POST | Close terminal session |
| `/mcp` | POST | MCP JSON-RPC endpoint (CoDA tools) |

### WebSocket Events (Socket.IO)

| Event | Direction | Description |
|-------|-----------|-------------|
| `join_session` | Client → Server | Join session room for output delivery |
| `leave_session` | Client → Server | Leave session room |
| `terminal_input` | Client → Server | Send keystrokes to PTY |
| `terminal_resize` | Client → Server | Resize terminal |
| `heartbeat` | Client → Server | Keepalive for idle sessions |
| `terminal_output` | Server → Client | Push PTY output in real time |
| `session_exited` | Server → Client | Shell process exited |
| `session_closed` | Server → Client | Session terminated by server |
| `shutting_down` | Server → Client | Server restarting (SIGTERM) |

</details>

<details>
<summary><strong>⚙️ Configuration</strong></summary>

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `HOME` | Yes | Set to `/app/python/source_code` in app.yaml |
| `DATABRICKS_TOKEN` | No | Optional. If not set, the app prompts for a token on first session. Auto-rotated every 10 minutes |
| `DATABRICKS_GATEWAY_HOST` | No | AI Gateway URL override. Auto-discovered from `DATABRICKS_WORKSPACE_ID` if unset |
| `ANTHROPIC_MODEL` | No | Claude model name (default: `databricks-claude-opus-4-7`) |
| `CODEX_MODEL` | No | Codex model name (default: `databricks-gpt-5-5`) |
| `GEMINI_MODEL` | No | Gemini model name (default: `databricks-gemini-2-5-pro`) |
| `HERMES_MODEL` | No | Hermes model name (default: `databricks-claude-opus-4-6`) |
| `HERMES_FALLBACK_MODEL` | No | Fallback model if `HERMES_MODEL` is unavailable in this workspace's geo |
| `ENABLE_HERMES` | No | Set to `"false"` to skip Hermes Agent install. Other CLIs are unaffected. Default `"true"` |
| `MAX_CONCURRENT_SESSIONS` | No | Cap on simultaneous PTY sessions per worker (default `5`) |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY` | No | Pass-through to Claude Code's auto-memory feature (default `0`) |
| `MLFLOW_TRACING_ENABLED` | No | Set to `"true"` to enable MLflow tracing for Claude, Codex, and Gemini in one switch (default `"false"`) |
| `DEEPWIKI_MCP_URL` | No | Override or disable the DeepWiki MCP server (set to `""` to remove) |
| `EXA_MCP_URL` | No | Override or disable the Exa MCP server (set to `""` to remove) |
| `TEAM_MEMORY_MCP_URL` | No | Optional shared-org-memory MCP server URL |
| `ENTERPRISE_MODE` | No | When `"true"`, logs a banner and warns on missing recommended mirrors. See [enterprise docs](docs/enterprise.md) for the full enterprise contract (JFrog mirrors, custom CA bundle, corporate proxy, etc.) |

### Security Model

Single-user app — the owner is resolved via the app's service principal and Apps API (`app.creator`), with no PAT required at deploy time. Authorization checks `X-Forwarded-Email` against `app.creator`. On first terminal session, the user pastes a short-lived PAT interactively. Tokens auto-rotate every 10 minutes (15-minute lifetime), with old tokens proactively revoked. On restart, the user re-pastes (no persistence by design).

### Server

Production uses `uvicorn` (single worker — PTY state is process-local) serving `coda_mcp.mcp_asgi:app`. The ASGI stack composes `python-socketio.ASGIApp` → MCP Streamable HTTP at `/mcp` → `WSGIMiddleware(Flask)` for the terminal UI. WebSocket transport falls back to HTTP polling under uvicorn — the `static/poll-worker.js` Web Worker already handles this transparently. `gunicorn.conf.py` is retained for reference and local WSGI-only dev; it is **not** used in production.

</details>

<details>
<summary><strong>📁 Project Structure</strong></summary>

```
coding-agents-databricks-apps/
├── app.py                       # Flask backend + PTY management + setup orchestration
├── app_state.py                 # Shared app state (setup progress, session registry)
├── app.yaml                     # Databricks Apps deployment config (uvicorn entrypoint)
├── cli_auth.py                  # Interactive PAT setup + CLI credential writer
├── content_filter_proxy.py      # Proxy: sanitises OpenCode/Gemini traffic, transparently relays Codex, injects rotated PATs
├── gunicorn.conf.py             # Legacy WSGI-only config (unused in production; uvicorn is the entrypoint)
├── pat_rotator.py               # Background PAT auto-rotation (10-min cycle)
├── pyproject.toml               # Package metadata + uv config (supply-chain guardrails)
├── requirements.txt             # Compiled from pyproject.toml (Dependabot compatibility)
├── requirements.lock            # Hash-pinned lockfile (auto-regenerated by CI)
├── Makefile                     # Deploy, redeploy, status, and cleanup targets
├── sync_to_workspace.py         # Post-commit hook: sync to Workspace
├── utils.py                     # Utility functions (ensure_https, gateway discovery)
├── coda_mcp/                    # MCP server package (CoDA — Coding Agents)
│   ├── __init__.py
│   ├── mcp_server.py            # FastMCP tool definitions (coda_run, coda_inbox, coda_get_result)
│   ├── mcp_endpoint.py          # Flask Blueprint: JSON-RPC /mcp endpoint
│   ├── mcp_asgi.py              # ASGI bridge (optional, for native MCP SDK transport)
│   └── task_manager.py          # Disk-based session/task state manager
├── setup/                       # Agent setup scripts (run at boot)
│   ├── setup_claude.py          # Claude Code CLI + MCP configuration
│   ├── setup_codex.py           # Codex CLI configuration
│   ├── setup_gemini.py          # Gemini CLI configuration
│   ├── setup_opencode.py        # OpenCode configuration
│   ├── setup_hermes.py          # Hermes Agent configuration
│   ├── setup_databricks.py      # Databricks CLI configuration
│   ├── setup_mlflow.py          # MLflow tracing auto-configuration
│   └── setup_proxy.py           # Content-filter proxy startup
├── scripts/                     # Shell scripts
│   ├── install_micro.sh         # Micro editor installer
│   ├── install_gh.sh            # GitHub CLI installer (OS/arch-aware)
│   └── install_databricks_cli.sh # Databricks CLI upgrade script
├── static/
│   ├── index.html               # Terminal UI (xterm.js + split panes + WebSocket)
│   ├── favicon.svg              # App favicon
│   ├── poll-worker.js           # Web Worker for HTTP polling fallback
│   └── lib/
│       ├── xterm.js             # xterm.js terminal emulator
│       └── socket.io.min.js     # Vendored Socket.IO client
├── .claude/
│   └── skills/                  # 39 pre-installed skills
├── .github/
│   └── workflows/
│       ├── dependency-audit.yml # Weekly CVE audit + lockfile drift check
│       └── update-lockfile.yml  # Auto-regenerate requirements.lock on push
├── tools/
│   └── coda-bridge.py           # Stdio-to-HTTP MCP bridge (OAuth token injection)
└── docs/
    ├── deployment.md            # Full Databricks Apps deployment guide
    ├── mcp-client-setup.md      # MCP client setup guide (bridge config)
    ├── mcp-v2-background-execution.md  # MCP server design doc
    ├── prd/                     # Product requirement documents
    └── plans/                   # Design documentation
```

</details>

---

## Technologies

Flask · Flask-SocketIO · Socket.IO · uvicorn · MCP (Streamable HTTP) · xterm.js · Python PTY · uv · Databricks SDK · Databricks AI Gateway · MLflow
