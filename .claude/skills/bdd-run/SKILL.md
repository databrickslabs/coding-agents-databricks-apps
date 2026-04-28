---
name: bdd-run
description: "This skill should be used when the user asks to \"run BDD tests\", \"execute Behave\", \"run Gherkin tests\", \"run my feature files\", \"behave test results\", \"run smoke tests\", \"BDD test report\", or needs to execute Behave test suites with specific options like tag filtering, parallel execution, or CI reporting."
user-invocable: true
---

# BDD Run — Execute and Report Behave Tests

Execute Behave test suites with tag filtering, parallel execution, output formatting, and CI integration. Diagnose failures and suggest fixes.

## When to use

- Running the full BDD test suite or a subset by tags
- Getting JUnit/JSON reports for CI pipelines
- Re-running only failed scenarios
- Running tests in parallel for speed
- Diagnosing and triaging test failures

## Process

### 1. Pre-flight checks

Before running tests, verify the environment:

```bash
# Verify Behave is installed
uv run behave --version

# Verify Databricks auth
uv run python -c "from databricks.sdk import WorkspaceClient; print(WorkspaceClient().current_user.me().user_name)"

# Dry run to check step coverage
uv run behave --dry-run
```

If any undefined steps are found, report them and suggest using the `bdd-steps` skill.

### 2. Execute tests

**Run by tag (most common):**

```bash
# Smoke tests only
uv run behave --tags="@smoke" --format=pretty

# All except slow and WIP
uv run behave --tags="not @slow and not @wip"

# Specific domain
uv run behave --tags="@catalog"
uv run behave --tags="@pipeline"

# Boolean combinations
uv run behave --tags="(@catalog or @pipeline) and @smoke"
```

**Run specific feature file or directory:**

```bash
uv run behave features/catalog/permissions.feature
uv run behave features/pipelines/
```

**Run by scenario name:**

```bash
uv run behave --name "Grant SELECT on a table"
```

**Pass runtime configuration:**

```bash
uv run behave -D warehouse_id=abc123 -D catalog=my_catalog -D environment=dev
```

### 3. Output and reporting

**For local development:**

```bash
uv run behave --format=pretty --show-timings
```

**For CI pipelines (JUnit XML):**

```bash
uv run behave --junit --junit-directory=reports/behave/ --format=progress
```

**JSON output for programmatic analysis:**

```bash
uv run behave --format=json --outfile=reports/results.json --format=progress
```

**Multiple formatters simultaneously:**

```bash
uv run behave --format=pretty --format=json --outfile=reports/results.json
```

### 4. Re-run failed tests

Configure rerun file output, then re-run only failures:

```bash
# First run captures failures
uv run behave --format=rerun --outfile=reports/rerun.txt --format=pretty

# Re-run only failed scenarios
uv run behave @reports/rerun.txt
```

### 5. Parallel execution

Behave has no built-in parallelism. Use `behavex` for parallel feature execution:

```bash
uv run behavex --parallel-processes 4 --parallel-scheme feature
```

Each parallel worker needs its own test schema to avoid cross-contamination. The `environment.py` template from `bdd-scaffold` handles this by using timestamped schema names with worker ID suffixes.

### 6. Failure diagnosis

When tests fail, read the output and categorize:

| Failure type | Symptom | Action |
|-------------|---------|--------|
| Undefined step | `NotImplementedError` or "undefined" in output | Generate step with `bdd-steps` |
| Auth failure | `PermissionDenied`, 401/403 | Check `databricks auth profiles` |
| Timeout | `TimeoutError` in polling steps | Increase timeout parameter or check resource state |
| Data mismatch | Assertion error with expected vs. actual | Check test data setup or query logic |
| Schema not found | `SCHEMA_NOT_FOUND` | Verify `before_all` created the ephemeral schema |
| Warehouse stopped | `WAREHOUSE_NOT_RUNNING` | Start warehouse or use `@fixture.sql_warehouse` tag hook |

### 7. Makefile integration

If a Makefile exists, prefer `make` targets:

```bash
make bdd           # Full suite
make bdd-smoke     # Smoke tests
make bdd-report    # JUnit for CI
```
