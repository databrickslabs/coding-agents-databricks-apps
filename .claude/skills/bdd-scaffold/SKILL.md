---
name: bdd-scaffold
description: "This skill should be used when the user asks to \"set up BDD\", \"create a Behave project\", \"scaffold BDD tests\", \"initialize Behave\", \"add BDD to my project\", \"set up Gherkin testing\", \"create test structure for Behave\", or mentions setting up behavior-driven development testing. Generates a complete Behave project structure wired to Databricks SDK."
user-invocable: true
---

# BDD Scaffold — Behave + Databricks Project Setup

Generate a complete Python Behave project structure pre-wired with Databricks SDK integration, including `environment.py` hooks, test isolation via ephemeral schemas, and `behave.ini` configuration.

## When to use

- Starting a new BDD test suite for a Databricks project
- Adding Behave-based acceptance tests to an existing repo
- Setting up integration testing against Unity Catalog, pipelines, jobs, or Apps

## Process

### 1. Detect project context

Identify the project root and existing tooling:

```bash
git rev-parse --show-toplevel
```

Check for existing test infrastructure: `pyproject.toml`, `Makefile`, `behave.ini`, `features/` directory. If a `features/` directory already exists, confirm before overwriting.

### 2. Determine test domains

Ask (or infer from the codebase) which Databricks domains to scaffold step files for:

| Domain | Step file | When |
|--------|-----------|------|
| Unity Catalog | `catalog_steps.py` | Tables, schemas, grants, row filters, column masks |
| Pipelines | `pipeline_steps.py` | Lakeflow SDP, streaming tables, materialized views |
| Jobs | `job_steps.py` | Notebook runs, workflow tasks, job clusters |
| Apps | `app_steps.py` | FastAPI endpoints, SSO headers, deployment |
| SQL | `sql_steps.py` | Statement execution, warehouse queries, data validation |

Always generate `common_steps.py` (shared workspace connection, row counting, table existence checks).

### 3. Generate the directory structure

```
features/
├── environment.py           # Databricks SDK setup, ephemeral schema lifecycle
├── steps/
│   ├── common_steps.py      # Shared steps (always generated)
│   └── <domain>_steps.py    # Per-domain (based on step 2)
├── catalog/                 # Feature file directories (one per domain)
├── pipelines/
├── jobs/
├── apps/
└── sql/
behave.ini
Makefile                     # (append BDD targets if Makefile exists)
```

Refer to `references/environment-template.md` for the full `environment.py` template with:
- `before_all`: WorkspaceClient init, warehouse auto-discovery, ephemeral schema creation
- `after_all`: Schema cascade drop
- `before_scenario` / `after_scenario`: Per-scenario resource tracking and cleanup
- Tag-based hooks for `@wip`, `@skip`, `@slow`

Refer to `references/behave-config.md` for `behave.ini` and `pyproject.toml` configuration.

### 4. Add dependencies

If `pyproject.toml` exists and uses `uv`:

```bash
uv add --group test behave databricks-sdk httpx
```

If no `pyproject.toml`, create a minimal one with test dependencies.

### 5. Add Makefile targets

Append these targets (or create a Makefile if none exists):

```makefile
.PHONY: bdd bdd-smoke bdd-report

bdd:
	uv run behave --format=pretty

bdd-smoke:
	uv run behave --tags="@smoke" --format=pretty

bdd-report:
	uv run behave --junit --junit-directory=reports/ --format=progress
```

### 6. Verify scaffold

Run `behave --dry-run` to confirm step discovery works and there are no import errors:

```bash
uv run behave --dry-run
```

Report the generated structure and next steps to the user.

## Key design decisions

- **Ephemeral schemas** — each test run creates a timestamped schema (`behave_test_YYYYMMDD_HHMMSS`) and drops it in `after_all`. Prevents cross-run contamination.
- **`-D` userdata** for parameterization — warehouse IDs, catalog names, and targets are passed via CLI args, never hardcoded.
- **Step files are globally scoped** in Behave — all files in `steps/` are imported regardless of which feature runs. Name step patterns carefully to avoid collisions.

## Additional resources

- **`references/environment-template.md`** — Full annotated environment.py template
- **`references/behave-config.md`** — behave.ini and pyproject.toml configuration reference
