# Claude Code on Databricks

Welcome! This environment comes pre-configured with 30 skills and 2 MCP servers.

## Skills (30 total)

### Databricks Skills (16)

| Category | Skills |
|----------|--------|
| AI & Agents | agent-bricks, databricks-genie, mlflow-evaluation, model-serving |
| Analytics | aibi-dashboards, databricks-unity-catalog |
| Data Engineering | spark-declarative-pipelines, databricks-jobs, synthetic-data-generation |
| Development | asset-bundles, databricks-app-apx, databricks-app-python, databricks-python-sdk, databricks-config |
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

## MCP Servers

- **DeepWiki** - AI-powered documentation for any GitHub repository
- **Exa** - Web search and code context retrieval

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

## Quick Start

- Projects sync to Databricks Workspace on git commit
- Use `/commit` for guided commits
- Ask "help me create a dashboard" to see skills in action
- Ask about any GitHub repo with DeepWiki MCP

## Credits

- Databricks skills from [databricks-solutions/ai-dev-kit](https://github.com/databricks-solutions/ai-dev-kit)
- Development workflow skills from [obra/superpowers](https://github.com/obra/superpowers)
