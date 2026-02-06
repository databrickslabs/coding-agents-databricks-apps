# claude-code-cli-bricks
### What is it?

TL;DR: Claude Code on Databricks Apps for All Databricks Users 🚀

A browser-based terminal emulator built with Flask and xterm.js, designed for cloud development environments with Databricks workspace integration and Claude Code CLI support.

### Why now?
On Jan 26. 2026, Andrej Karpathy made [this viral tweet](https://x.com/karpathy/status/2015883857489522876?s=46&t=tEsLJXJnGFIkaWs-Bhs1yA). Boris Cherny, the creator of claude code responded and said the following.
![alt text](image.png)

This app template opens this up for all Databricks Users! ❤️

No more pesky IDE setups, no bespoke tweaks. 

Just use it all on Databricks, from the browser. Wired up to model serving endpoints on your workspace.

## Features

✅ **Browser-based Terminal** - Full PTY support with xterm.js frontend

✅ **Real-time I/O** - Responsive terminal with polling-based communication

✅ **Terminal Resizing** - Dynamic resize support for responsive layouts

✅ **Databricks Workspace Integration** - Auto-sync projects to Databricks Workspace on git commits

✅ **Claude Code CLI** - Pre-configured to use Databricks hosted models as the API endpoint

✅ **Configurable Model** - Switch between Claude models via `app.yaml` (default: `databricks-claude-sonnet-4-5`)

✅ **Micro Editor** - Ships with [micro](https://micro-editor.github.io/), a modern terminal-based text editor

✅ **Databricks CLI** - Pre-configured with your PAT for immediate use

✅ **Single-User Security** - Only the token owner can access the terminal

✅ **MCP Servers** - DeepWiki for GitHub docs, Exa for web search

### 30 Pre-installed Skills

✅ **Databricks Skills (16)** - Make building Databricks products simple. Create dashboards, jobs, pipelines, agents, and more with guided workflows that understand Databricks APIs and best practices.

✅ **Superpowers Skills (14)** - Provide the agentic framework for Claude Code. Test-driven development, systematic debugging, brainstorming, parallel agent workflows, and structured planning for complex tasks.

## Skill Details

### Databricks Skills

From [databricks-solutions/ai-dev-kit](https://github.com/databricks-solutions/ai-dev-kit):

| Category | Skills |
|----------|--------|
| AI & Agents | agent-bricks, databricks-genie, mlflow-evaluation, model-serving |
| Analytics | aibi-dashboards, databricks-unity-catalog |
| Data Engineering | spark-declarative-pipelines, databricks-jobs, synthetic-data-generation |
| Development | asset-bundles, databricks-app-apx, databricks-app-python, databricks-python-sdk, databricks-config |
| Reference | databricks-docs, unstructured-pdf-generation |

### Development Workflow Skills

From [obra/superpowers](https://github.com/obra/superpowers):

- brainstorming, test-driven-development, systematic-debugging, writing-plans
- verification-before-completion, executing-plans, dispatching-parallel-agents
- subagent-driven-development, using-git-worktrees, requesting-code-review
- receiving-code-review, finishing-a-development-branch, writing-skills, using-superpowers

## MCP Servers

Pre-configured MCP servers for enhanced capabilities:

| Server | Description |
|--------|-------------|
| **DeepWiki** | AI-powered documentation for any GitHub repository |
| **Exa** | Web search and code context retrieval |

### Updating Skills

Skills are bundled with the app. To update:

1. Pull latest from [ai-dev-kit](https://github.com/databricks-solutions/ai-dev-kit)
2. Copy `databricks-skills/*` to `.claude/skills/`
3. For superpowers, pull latest from [obra/superpowers](https://github.com/obra/superpowers) and copy `skills/*` to `.claude/skills/`
4. Redeploy the app

## Quick Start

### Prerequisites

- Python 3.11+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

## Deploying to Databricks

1. Clone this repo to your Databricks Workspace
2. Navigate to **Compute** → **Apps**
3. Click **Create App** and select **Custom App**
4. Point to the cloned repo and deploy

### Installation
```bash
# Clone the repository
git clone https://github.com/your-username/claude-code-cli-bricks.git
cd claude-code-cli-bricks

# Install dependencies
uv pip install -r requirements.txt
```

### Running Locally

```bash
uv run python app.py
```

Open http://localhost:8000 in your browser.


## Architecture

```
┌─────────────────────┐     HTTP      ┌─────────────────────┐
│   Browser Client    │◄────────────►│   Flask Backend     │
│   (xterm.js)        │   Polling     │   (PTY Manager)     │
└─────────────────────┘               └─────────────────────┘
                                              │
                                              ▼
                                      ┌─────────────────────┐
                                      │   Shell Process     │
                                      │   (/bin/bash)       │
                                      └─────────────────────┘
```

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Serves the terminal UI |
| `/health` | GET | Health check with session count |
| `/api/session` | POST | Create new terminal session |
| `/api/input` | POST | Send input to terminal |
| `/api/output` | POST | Poll for terminal output |
| `/api/resize` | POST | Resize terminal dimensions |
| `/api/session` | DELETE | Close terminal session |

## Project Structure

```
claude-code-cli-bricks/
├── .claude/
│   └── skills/            # 30 pre-installed skills
├── app.py                 # Flask backend with PTY management
├── app.yaml               # Databricks Apps deployment config
├── app.yaml.template      # Template for app.yaml configuration
├── CLAUDE.md              # Claude Code welcome message
├── requirements.txt       # Python dependencies
├── setup_claude.py        # Claude Code CLI + MCP configuration
├── setup_databricks.py    # Databricks CLI configuration
├── sync_to_workspace.py   # Git hook for Databricks sync
├── static/
│   ├── index.html         # Terminal UI
│   └── lib/               # xterm.js library files
└── docs/
    └── plans/             # Design documentation
```

## Configuration

### Setting up app.yaml

Copy the template and configure your Databricks workspace:

```bash
cp app.yaml.template app.yaml
```

Edit `app.yaml` and replace `<your-workspace>` with your Databricks workspace URL:

```yaml
env:
  - name: DATABRICKS_HOST
    value: https://<your-workspace>.cloud.databricks.com
```

The `DATABRICKS_HOST` is used by both:
- **Workspace sync** - To upload projects on git commits
- **Claude Code CLI** - As the Anthropic API endpoint (via Databricks serving endpoints)

## Databricks Deployment

This project is configured for deployment as a Databricks App.

### Environment Variables

| Variable | Description |
|----------|-------------|
| `DATABRICKS_HOST` | Databricks workspace URL |
| `DATABRICKS_TOKEN` | Your Personal Access Token (PAT) |
| `ANTHROPIC_MODEL` | Model name (default: `databricks-claude-sonnet-4-5`) |

### Security Model

This is a **single-user app**. Each user deploys their own instance with their own PAT:

1. The `DATABRICKS_TOKEN` in `app.yaml` identifies the owner
2. At startup, the app determines the token owner via Databricks API
3. Only requests from the token owner are allowed
4. Other users see a 403 Forbidden error

This ensures your terminal session is private and uses your Databricks permissions.

### Create App

First, create the app in your Databricks workspace:

```bash
databricks apps create xterm-terminal
```

### Deploy via CLI

Deploy the code using the Databricks CLI:

```bash
# 1. Import project files to workspace (wipe clean first for fresh deploy)
databricks workspace delete /Workspace/Users/<your-email>/xterm-experiment --recursive
databricks workspace import-dir . /Workspace/Users/<your-email>/xterm-experiment --overwrite

# 2. Deploy the app
databricks apps deploy xterm-terminal --source-code-path /Workspace/Users/<your-email>/xterm-experiment
```

Replace `<your-email>` with your Databricks username (e.g., `user@example.com`).

Once the app is deployed, create a secret with your PAT in your Databricks Workspace. In the [App Resources tab](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/resources), add the secret aliased as DATABRICKS_TOKEN.

### Automatic Git Configuration

When the app starts, it automatically configures git with your Databricks identity:
- **Email**: From your Databricks `userName`
- **Name**: From your Databricks `displayName` (or derived from email)

This means commits made within the app will be attributed to your Databricks account.

## Workspace Sync

When deployed, git commits automatically sync your projects to Databricks Workspace:

```
/Workspace/Users/{email}/projects/{project-name}/
```

This is enabled via a git post-commit hook configured by `setup_claude.py`.

## Technologies

- **Backend**: Flask, Python PTY/termios
- **Frontend**: xterm.js, FitAddon
- **Integration**: Databricks SDK, Claude Agent SDK

## License

MIT
