---
name: bdd-steps
description: "This skill should be used when the user asks to \"write step definitions\", \"implement BDD steps\", \"generate step code\", \"create Behave steps\", \"implement Given When Then\", \"write Python steps for Gherkin\", \"step definitions for Databricks\", or needs to create Python step implementations for existing Gherkin feature files."
user-invocable: true
---

# BDD Steps — Python Step Definition Generation

Generate Python step definitions for Behave that implement Gherkin steps using the Databricks SDK. Read existing `.feature` files, identify undefined steps, and produce well-typed implementations.

## When to use

- Implementing step definitions for new or existing feature files
- Adding Databricks SDK calls to step implementations
- Refactoring step definitions for reusability across features

## Process

### 1. Identify undefined steps

Read the target feature files, then run a dry-run to find undefined steps:

```bash
uv run behave --dry-run features/<target>.feature 2>&1
```

Behave prints suggested snippets for each undefined step. Use these as the starting point.

### 2. Write step definitions

Place step files in `features/steps/` organized by domain:

| File | Domain | Key SDK imports |
|------|--------|----------------|
| `common_steps.py` | Shared utilities | `WorkspaceClient`, `StatementState` |
| `catalog_steps.py` | Unity Catalog | `catalog.PermissionsChange`, `catalog.Privilege`, `catalog.SecurableType` |
| `pipeline_steps.py` | Lakeflow SDP | `pipelines.PipelineStateInfo` |
| `job_steps.py` | Jobs/Notebooks | `jobs.SubmitTask`, `jobs.NotebookTask`, `jobs.RunLifeCycleState` |
| `app_steps.py` | Databricks Apps | `httpx.Client` for HTTP assertions |
| `sql_steps.py` | SQL/Data quality | `sql.StatementState`, `sql.Disposition` |

**Step definition structure:**

```python
from __future__ import annotations

from behave import given, when, then
from behave.runner import Context


@given('a descriptive step pattern with "{parameter}"')
def step_impl(context: Context, parameter: str) -> None:
    """Docstring explaining what this step does."""
    # Implementation using context.workspace (set in environment.py)
    ...
```

Refer to `references/step-library.md` for a comprehensive library of reusable Databricks step definitions covering:
- Workspace connection and SQL execution
- Table/schema existence and row count assertions
- Grant and permission verification
- Pipeline triggering and status polling
- Job submission and completion waiting
- HTTP endpoint testing with SSO header simulation

### 3. Step writing principles

**Use `context` for state passing.** Store results in `context` attributes so downstream `Then` steps can assert on them:

```python
@when('I execute a query on "{table_name}"')
def step_execute(context: Context, table_name: str) -> None:
    context.query_result = context.workspace.statement_execution.execute_statement(...)

@then('the result should have {count:d} rows')
def step_check_rows(context: Context, count: int) -> None:
    actual = len(context.query_result.result.data_array or [])
    assert actual == count, f"Expected {count}, got {actual}"
```

**Type all parameters.** Use Behave's parse types (`{name:d}` for int, `{name:f}` for float) or register custom types.

**Assertion messages must be diagnostic.** Always include expected vs. actual values:

```python
assert actual == expected, f"Expected {expected}, got {actual}"
```

**Substitute `{test_schema}` references.** Feature files may use `{test_schema}` as a placeholder. Step definitions should resolve it from `context.test_schema`:

```python
table_fqn = table_name.replace("{test_schema}", context.test_schema)
```

**Poll with timeout for async operations.** Jobs, pipelines, and app deployments need polling loops with configurable timeouts.

### 4. Validate steps compile

After writing, verify all steps resolve:

```bash
uv run behave --dry-run
```

Zero undefined steps = ready to run.

## Additional resources

- **`references/step-library.md`** — Complete reusable step definition library for all Databricks domains
