# Claude Code on Databricks

Welcome! This environment comes pre-configured with 5 AI coding agents, 50 skills, and 3 MCP servers. Hermes Agent is available alongside Claude Code, Codex, Gemini CLI, and OpenCode — launch it with `hermes chat`.

## Skills (50 total)

### Databricks Skills (16)

| Category | Skills |
|----------|--------|
| AI & Agents | agent-bricks, databricks-genie, databricks-ai-functions, mlflow-evaluation, model-serving |
| Analytics | aibi-dashboards, databricks-unity-catalog |
| Data Engineering | spark-declarative-pipelines, databricks-jobs, synthetic-data-gen |
| Development | databricks-bundles, databricks-apps-python, databricks-python-sdk, databricks-config |
| Reference | databricks-docs, unstructured-pdf-generation |

### Development Workflow Skills (14)

From [obra/superpowers](https://github.com/obra/superpowers):

| Skill | Purpose |
|-------|---------|
| brainstorming | Design features through collaborative dialogue |
| test-driven-development | RED-GREEN-REFACTOR cycle |
| systematic-debugging | 4-phase root cause analysis |
| writing-plans | Create detailed implementation plans |
| verification-before-completion | Verify before claiming done |
| executing-plans | Batch execution with checkpoints |
| dispatching-parallel-agents | Concurrent subagent workflows |
| subagent-driven-development | Fast iteration with two-stage review |
| using-git-worktrees | Parallel development branches |
| requesting-code-review | Pre-review checklist |
| receiving-code-review | Responding to feedback |
| finishing-a-development-branch | Merge/PR decision workflow |
| writing-skills | Create new skills |
| using-superpowers | Introduction to available skills |

### Flutter Skills (10)

From [flutter/skills](https://github.com/flutter/skills):

| Skill | Purpose |
|-------|---------|
| flutter-add-integration-test | Add integration tests |
| flutter-add-widget-preview | Add widget previews |
| flutter-add-widget-test | Add widget tests |
| flutter-apply-architecture-best-practices | Apply Flutter architecture best practices |
| flutter-build-responsive-layout | Build adaptive/responsive layouts |
| flutter-fix-layout-issues | Diagnose and fix layout issues |
| flutter-implement-json-serialization | Implement JSON serialization |
| flutter-setup-declarative-routing | Set up declarative routing (go_router) |
| flutter-setup-localization | Set up localization (i18n) |
| flutter-use-http-package | Networking with the http package |

### Dart Skills (9)

From [dart-lang/skills](https://github.com/dart-lang/skills):

| Skill | Purpose |
|-------|---------|
| dart-add-unit-test | Add unit tests |
| dart-build-cli-app | Build a Dart CLI app |
| dart-collect-coverage | Collect test coverage |
| dart-fix-runtime-errors | Diagnose and fix runtime errors |
| dart-generate-test-mocks | Generate test mocks (mockito/mocktail) |
| dart-migrate-to-checks-package | Migrate assertions to the checks package |
| dart-resolve-package-conflicts | Resolve package version conflicts |
| dart-run-static-analysis | Run static analysis |
| dart-use-pattern-matching | Use Dart pattern matching |

### Flutter UI Skills (1)

From [nank1ro/flutter-shadcn-ui](https://github.com/nank1ro/flutter-shadcn-ui):

| Skill | Purpose |
|-------|---------|
| shadcn-ui-flutter | Build Flutter UIs with shadcn_ui components |

## MCP Servers

- **DeepWiki** - AI-powered documentation for any GitHub repository
- **Exa** - Web search and code context retrieval
- **CoDA** (exposed at `/mcp`) - Delegate coding tasks to AI agents via MCP. Any MCP client (Genie Code, Claude Desktop, Cursor) can call `coda_run`, `coda_inbox`, and `coda_get_result` to submit background tasks, check status, and retrieve results. See `docs/mcp-v2-background-execution.md`.

## Databricks CLI

The Databricks CLI is pre-configured with your credentials. Test it:
```bash
databricks current-user me
```

Databricks can only authenticate with a PAT or CLIENT_ID and CLIENT_SECRET pair. If you have trouble logging in, remove the CLIENT_SECRET and CLIENT_ID from your environment, then try again. We want access to only be based on the app owner's credentials.

Common commands:
```bash
databricks workspace list /Workspace/Users/
databricks jobs list
databricks clusters list
```

## Project Setup

Before starting any new project or documentation:

1. **Always initialize a git repo first:**
   ```bash
   mkdir my-project && cd my-project
   git init
   ```
   Or clone an existing repo:
   ```bash
   git clone https://github.com/user/repo.git
   cd repo
   ```

2. **Why?** Git commits automatically sync your work to Databricks Workspace at `/Workspace/Users/{your-email}/projects/{project-name}/`

3. **Then start working** - your commits will be backed up to Workspace

## Architecture

Real-time terminal I/O over **WebSocket** (Flask-SocketIO) with automatic **HTTP polling fallback** via a Web Worker. Single gunicorn worker (PTY fds are process-local), 16 gthread threads. Per-session locks for WebSocket handlers; parallel agent setup at startup via ThreadPoolExecutor.

## Quick Start

- Projects sync to Databricks Workspace on git commit
- Use `/commit` for guided commits
- Ask "help me create a dashboard" to see skills in action
- Ask about any GitHub repo with DeepWiki MCP

## Credits

- Databricks skills from [databricks-solutions/ai-dev-kit](https://github.com/databricks-solutions/ai-dev-kit)
- Development workflow skills from [obra/superpowers](https://github.com/obra/superpowers)

# things to remember
Remember to never move .git folder to the workspace if you're running workspace import.