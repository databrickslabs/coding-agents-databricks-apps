# Reusable Step Definition Library

Complete library of Databricks step definitions for Behave. Organized by domain. Copy relevant sections into `features/steps/` files.

**Proven patterns used throughout:**

- Step patterns use **short names** (e.g., `"{table_name}"`), never `{test_schema}.table` in the pattern
- Step code builds FQN internally: `fqn = f"{context.test_schema}.{table_name}"`
- SQL in docstrings uses `{schema}` placeholder, replaced via `context.text.replace("{schema}", context.test_schema)`
- Steps with data tables have a **trailing colon** in the decorator: `@given('... with data:')`
- Grants use **SQL**, not the SDK grants API (which breaks on recent SDK versions)
- Integer parameters use Behave's built-in `{count:d}` format, not custom type parsers

---

## Common Steps (`common_steps.py`)

Always include these. They provide workspace connection, SQL execution, and basic assertions.

```python
"""Shared step definitions for Databricks BDD tests."""
from __future__ import annotations

import os
from datetime import datetime

from behave import given, then, step
from behave.runner import Context
from databricks.sdk.service.sql import StatementState


# ─── Connection and setup steps ─────────────────────────────────

@given("a Databricks workspace connection is established")
def step_workspace_connection(context: Context) -> None:
    """Initialize workspace client. Usually handled by environment.py."""
    if not hasattr(context, "workspace"):
        from databricks.sdk import WorkspaceClient
        context.workspace = WorkspaceClient()
        me = context.workspace.current_user.me()
        context.current_user = me.user_name


@given("a test schema is provisioned")
def step_test_schema(context: Context) -> None:
    """Verify test schema exists. Usually handled by environment.py."""
    assert hasattr(context, "test_schema"), (
        "No test_schema on context — check environment.py before_all"
    )


# ─── SQL execution steps ────────────────────────────────────────

@step("I execute the following SQL")
def step_execute_sql_docstring(context: Context) -> None:
    """Execute SQL from a docstring (triple-quoted text in feature file).

    In feature files, use {schema} as the placeholder:
        When I execute the following SQL
            \"\"\"
            SELECT * FROM {schema}.customers
            \"\"\"
    """
    sql = context.text.replace("{schema}", context.test_schema)
    context.query_result = _execute_sql(context, sql)


@step('I execute SQL "{sql}"')
def step_execute_sql_inline(context: Context, sql: str) -> None:
    """Execute inline SQL. The {schema} placeholder is replaced automatically."""
    sql = sql.replace("{schema}", context.test_schema)
    context.query_result = _execute_sql(context, sql)


# ─── Table existence and row count assertions ───────────────────

@then('the table "{table_name}" should exist')
def step_table_exists(context: Context, table_name: str) -> None:
    fqn = f"{context.test_schema}.{table_name}"
    try:
        context.workspace.tables.get(fqn)
    except Exception as e:
        raise AssertionError(f"Table {fqn} does not exist: {e}")


@then('the streaming table "{table_name}" should exist')
def step_streaming_table_exists(context: Context, table_name: str) -> None:
    fqn = f"{context.test_schema}.{table_name}"
    try:
        info = context.workspace.tables.get(fqn)
        assert info.table_type is not None, f"{fqn} exists but has no table_type"
    except Exception as e:
        raise AssertionError(f"Streaming table {fqn} does not exist: {e}")


@then('the materialized view "{table_name}" should exist')
def step_mv_exists(context: Context, table_name: str) -> None:
    fqn = f"{context.test_schema}.{table_name}"
    try:
        context.workspace.tables.get(fqn)
    except Exception as e:
        raise AssertionError(f"Materialized view {fqn} does not exist: {e}")


@then('the table "{table_name}" should have {expected:d} rows')
def step_exact_row_count(context: Context, table_name: str, expected: int) -> None:
    actual = _count_rows(context, table_name)
    assert actual == expected, f"Expected {expected} rows in {table_name}, got {actual}"


@then('the table "{table_name}" should have more than {expected:d} rows')
def step_min_row_count(context: Context, table_name: str, expected: int) -> None:
    actual = _count_rows(context, table_name)
    assert actual > expected, f"Expected more than {expected} rows in {table_name}, got {actual}"


@then('the table "{table_name}" should have 0 rows')
def step_empty_table(context: Context, table_name: str) -> None:
    actual = _count_rows(context, table_name)
    assert actual == 0, f"Expected 0 rows in {table_name}, got {actual}"


# ─── Query result assertions ────────────────────────────────────

@then("the result should have {expected:d} rows")
def step_result_row_count(context: Context, expected: int) -> None:
    rows = context.query_result.result.data_array or []
    actual = len(rows)
    assert actual == expected, f"Expected {expected} rows, got {actual}"


@then("the result should have more than {expected:d} rows")
def step_result_min_rows(context: Context, expected: int) -> None:
    rows = context.query_result.result.data_array or []
    actual = len(rows)
    assert actual > expected, f"Expected more than {expected} rows, got {actual}"


@then('the first row column "{col}" should be "{value}"')
def step_first_row_value(context: Context, col: str, value: str) -> None:
    result = context.query_result
    columns = [c.name for c in result.manifest.schema.columns]
    col_idx = columns.index(col)
    actual = result.result.data_array[0][col_idx]
    assert str(actual) == value, f"Expected {col}={value}, got {actual}"


# ─── Data setup steps ───────────────────────────────────────────

@given('the table "{table_name}" has been loaded')
def step_table_loaded(context: Context, table_name: str) -> None:
    """Assert table exists and is not empty."""
    fqn = f"{context.test_schema}.{table_name}"
    count = _count_rows(context, table_name)
    assert count > 0, f"Table {fqn} exists but is empty"


@given('a managed table "{table_name}" exists')
def step_ensure_table_exists(context: Context, table_name: str) -> None:
    fqn = f"{context.test_schema}.{table_name}"
    try:
        context.workspace.tables.get(fqn)
    except Exception:
        # Create a minimal table
        _execute_sql(context, f"CREATE TABLE IF NOT EXISTS {fqn} (id BIGINT)")
        context.scenario_cleanup_sql.append(f"DROP TABLE IF EXISTS {fqn}")


@given('a managed table "{table_name}" with data:')
def step_create_table_with_data(context: Context, table_name: str) -> None:
    """Create a table and populate from the Gherkin data table.

    The trailing colon in the decorator is required — Behave matches it
    as part of the step text when a data table follows.

    Example feature file usage:
        Given a managed table "customers" with data:
            | id | name    | region |
            | 1  | Acme    | APAC   |
            | 2  | Contoso | EMEA   |
    """
    fqn = f"{context.test_schema}.{table_name}"
    headers = context.table.headings
    rows = context.table.rows

    # Infer types (simple heuristic — all STRING)
    col_defs = ", ".join(f"{h} STRING" for h in headers)
    _execute_sql(context, f"CREATE OR REPLACE TABLE {fqn} ({col_defs})")
    context.scenario_cleanup_sql.append(f"DROP TABLE IF EXISTS {fqn}")

    # Insert rows
    for row in rows:
        values = ", ".join(f"'{cell}'" for cell in row)
        _execute_sql(context, f"INSERT INTO {fqn} VALUES ({values})")


# ─── Helpers ────────────────────────────────────────────────────

def _execute_sql(context: Context, sql: str):
    """Execute SQL and return result."""
    result = context.workspace.statement_execution.execute_statement(
        warehouse_id=context.warehouse_id,
        statement=sql,
        wait_timeout="30s",
    )
    assert result.status.state == StatementState.SUCCEEDED, (
        f"SQL failed: {result.status.error}\nStatement: {sql[:200]}"
    )
    return result


def _count_rows(context: Context, table_name: str) -> int:
    """Count rows in a table."""
    fqn = f"{context.test_schema}.{table_name}"
    result = _execute_sql(context, f"SELECT COUNT(*) AS cnt FROM {fqn}")
    return int(result.result.data_array[0][0])
```

---

## Catalog Steps (`catalog_steps.py`)

Uses SQL for grants instead of the SDK grants API. The SDK's `grants.update(securable_type=SecurableType.TABLE, ...)` fails with `SECURABLETYPE.TABLE is not a valid securable type` on recent SDK versions.

```python
"""Step definitions for Unity Catalog permissions and security.

Uses SQL for all grant operations. The SDK grants API is unreliable —
SecurableType.TABLE fails on recent databricks-sdk versions.
"""
from __future__ import annotations

from behave import when, then
from behave.runner import Context
from databricks.sdk.service.sql import StatementState


@when('I grant {privilege} on table "{table_name}" to group "{group}"')
def step_grant(context: Context, privilege: str, table_name: str, group: str) -> None:
    """Grant a privilege on a table using SQL.

    Example feature file usage:
        When I grant SELECT on table "customers" to group "analysts"
    """
    fqn = f"{context.test_schema}.{table_name}"
    _execute_sql(context, f"GRANT {privilege} ON TABLE {fqn} TO `{group}`")


@when('I revoke {privilege} on table "{table_name}" from group "{group}"')
def step_revoke(context: Context, privilege: str, table_name: str, group: str) -> None:
    """Revoke a privilege on a table using SQL."""
    fqn = f"{context.test_schema}.{table_name}"
    _execute_sql(context, f"REVOKE {privilege} ON TABLE {fqn} FROM `{group}`")


@then('the group "{group}" should have {privilege} permission on "{table_name}"')
def step_verify_grant(
    context: Context, group: str, privilege: str, table_name: str
) -> None:
    """Verify a grant exists using SHOW GRANTS.

    SHOW GRANTS returns PascalCase columns: Principal, ActionType, ObjectType, ObjectKey.
    """
    fqn = f"{context.test_schema}.{table_name}"
    result = _execute_sql(context, f"SHOW GRANTS ON TABLE {fqn}")
    columns = [c.name for c in result.manifest.schema.columns]
    principal_idx = columns.index("Principal")
    action_idx = columns.index("ActionType")

    found_privs = []
    for row in result.result.data_array or []:
        if row[principal_idx] == group:
            found_privs.append(row[action_idx])

    assert privilege in found_privs, (
        f"Expected {group} to have {privilege} on {fqn}, "
        f"found: {found_privs}"
    )


@then('the group "{group}" should not have {privilege} permission on "{table_name}"')
def step_verify_no_grant(
    context: Context, group: str, privilege: str, table_name: str
) -> None:
    """Verify a grant does NOT exist using SHOW GRANTS."""
    fqn = f"{context.test_schema}.{table_name}"
    result = _execute_sql(context, f"SHOW GRANTS ON TABLE {fqn}")
    columns = [c.name for c in result.manifest.schema.columns]
    principal_idx = columns.index("Principal")
    action_idx = columns.index("ActionType")

    found_privs = []
    for row in result.result.data_array or []:
        if row[principal_idx] == group:
            found_privs.append(row[action_idx])

    assert privilege not in found_privs, (
        f"Expected {group} NOT to have {privilege} on {fqn}, "
        f"but found: {found_privs}"
    )


def _execute_sql(context: Context, sql: str):
    """Execute SQL and return result."""
    result = context.workspace.statement_execution.execute_statement(
        warehouse_id=context.warehouse_id,
        statement=sql,
        wait_timeout="30s",
    )
    assert result.status.state == StatementState.SUCCEEDED, (
        f"SQL failed: {result.status.error}\nStatement: {sql[:200]}"
    )
    return result
```

---

## Pipeline Steps (`pipeline_steps.py`)

```python
"""Step definitions for Lakeflow Spark Declarative Pipelines."""
from __future__ import annotations

import time

from behave import given, when, then
from behave.runner import Context


@given('a pipeline "{name}" exists targeting "{schema}"')
def step_pipeline_exists(context: Context, name: str, schema: str) -> None:
    pipelines = list(
        context.workspace.pipelines.list_pipelines(filter=f'name LIKE "{name}"')
    )
    if pipelines:
        context.pipeline_id = pipelines[0].pipeline_id
    else:
        result = context.workspace.pipelines.create(
            name=name,
            target=schema,
            catalog=context.test_catalog,
            channel="CURRENT",
        )
        context.pipeline_id = result.pipeline_id
        context.scenario_cleanup_sql.append(None)  # Mark for pipeline cleanup


@given('the pipeline "{name}" has completed a full refresh')
def step_pipeline_refreshed(context: Context, name: str) -> None:
    """Ensure pipeline exists and has been refreshed at least once."""
    pipelines = list(
        context.workspace.pipelines.list_pipelines(filter=f'name LIKE "{name}"')
    )
    assert pipelines, f"Pipeline '{name}' not found"
    context.pipeline_id = pipelines[0].pipeline_id
    # Check latest update status
    detail = context.workspace.pipelines.get(context.pipeline_id)
    assert detail.latest_updates, f"Pipeline '{name}' has never been run"


@when("I trigger a full refresh of the pipeline")
def step_full_refresh(context: Context) -> None:
    response = context.workspace.pipelines.start_update(
        pipeline_id=context.pipeline_id,
        full_refresh=True,
    )
    context.update_id = response.update_id


@when("I trigger an incremental refresh of the pipeline")
def step_incremental_refresh(context: Context) -> None:
    response = context.workspace.pipelines.start_update(
        pipeline_id=context.pipeline_id,
        full_refresh=False,
    )
    context.update_id = response.update_id


@then("the pipeline update should succeed within {timeout:d} seconds")
def step_pipeline_success(context: Context, timeout: int) -> None:
    _wait_for_pipeline(context, timeout, expect_success=True)


@then("the pipeline update should fail")
def step_pipeline_fail(context: Context) -> None:
    _wait_for_pipeline(context, timeout=300, expect_success=False)


@then('the pipeline error should mention {keyword}')
def step_pipeline_error_contains(context: Context, keyword: str) -> None:
    events = list(context.workspace.pipelines.list_pipeline_events(
        pipeline_id=context.pipeline_id,
        max_results=10,
    ))
    error_messages = " ".join(
        str(e.message) for e in events if e.level == "ERROR"
    )
    assert keyword.lower() in error_messages.lower(), (
        f"Expected pipeline error to mention '{keyword}', "
        f"but errors were: {error_messages[:500]}"
    )


def _wait_for_pipeline(
    context: Context, timeout: int, expect_success: bool
) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        update = context.workspace.pipelines.get_update(
            pipeline_id=context.pipeline_id,
            update_id=context.update_id,
        )
        state = update.update.state
        if state in ("COMPLETED",):
            if expect_success:
                return
            raise AssertionError("Expected pipeline to fail, but it succeeded")
        if state in ("FAILED", "CANCELED"):
            if not expect_success:
                return
            raise AssertionError(
                f"Pipeline update {state}. Check update {context.update_id}"
            )
        time.sleep(15)
    raise TimeoutError(f"Pipeline did not complete within {timeout}s")
```

---

## Job Steps (`job_steps.py`)

```python
"""Step definitions for Databricks Jobs and notebook runs."""
from __future__ import annotations

import time

from behave import when, then
from behave.runner import Context
from databricks.sdk.service.jobs import (
    NotebookTask,
    RunLifeCycleState,
    SubmitTask,
)


@when('I run the notebook "{path}" with parameters:')
def step_run_notebook(context: Context, path: str) -> None:
    """Run a notebook with parameters from a Gherkin data table.

    The trailing colon is required when a data table follows.

    Example feature file usage:
        When I run the notebook "/Workspace/tests/etl" with parameters:
            | key    | value       |
            | schema | my_schema   |
            | mode   | full        |
    """
    params = {}
    for row in context.table:
        value = row["value"].replace("{schema}", context.test_schema)
        params[row["key"]] = value

    run = context.workspace.jobs.submit(
        run_name=f"behave-{context.scenario.name[:50]}",
        tasks=[
            SubmitTask(
                task_key="main",
                notebook_task=NotebookTask(
                    notebook_path=path,
                    base_parameters=params,
                ),
            )
        ],
    )
    context.run_id = run.response.run_id


@then('the job should complete with status "{expected}" within {timeout:d} seconds')
def step_job_status(context: Context, expected: str, timeout: int) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = context.workspace.jobs.get_run(context.run_id)
        state = run.state
        if state.life_cycle_state in (
            RunLifeCycleState.TERMINATED,
            RunLifeCycleState.INTERNAL_ERROR,
            RunLifeCycleState.SKIPPED,
        ):
            break
        time.sleep(10)
    else:
        raise TimeoutError(f"Run {context.run_id} did not complete within {timeout}s")

    actual = state.result_state.value if state.result_state else "UNKNOWN"
    assert actual == expected, (
        f"Expected {expected}, got {actual}. Message: {state.state_message}"
    )
```

---

## App Steps (`app_steps.py`)

```python
"""Step definitions for Databricks Apps (FastAPI) testing."""
from __future__ import annotations

import subprocess
import os

import httpx
from behave import given, when, then
from behave.runner import Context


@given('the app is running at "{base_url}"')
def step_app_running(context: Context, base_url: str) -> None:
    context.app_client = httpx.Client(base_url=base_url, timeout=10)


@given('the test user is "{email}"')
def step_test_user(context: Context, email: str) -> None:
    context.auth_headers = {
        "X-Forwarded-Email": email,
        "X-Forwarded-User": email.split("@")[0],
    }


@when('I GET "{path}"')
def step_get(context: Context, path: str) -> None:
    context.response = context.app_client.get(path)


@when('I GET "{path}" with auth headers')
def step_get_auth(context: Context, path: str) -> None:
    context.response = context.app_client.get(path, headers=context.auth_headers)


@when('I GET "{path}" without auth headers')
def step_get_no_auth(context: Context, path: str) -> None:
    context.response = context.app_client.get(path)


@when('I POST "{path}" with auth headers and body')
def step_post_auth(context: Context, path: str) -> None:
    """POST with JSON body from a docstring.

    Example feature file usage:
        When I POST "/api/items" with auth headers and body
            \"\"\"
            {"name": "test-item", "value": 42}
            \"\"\"
    """
    import json
    body = json.loads(context.text)
    context.response = context.app_client.post(
        path, json=body, headers=context.auth_headers,
    )


@then("the response status should be {code:d}")
def step_status_code(context: Context, code: int) -> None:
    assert context.response.status_code == code, (
        f"Expected {code}, got {context.response.status_code}: "
        f"{context.response.text[:200]}"
    )


@then('the response JSON should contain "{key}" with value "{value}"')
def step_json_value(context: Context, key: str, value: str) -> None:
    data = context.response.json()
    assert key in data, f"Key '{key}' not in response: {list(data.keys())}"
    assert str(data[key]) == value, f"Expected {key}='{value}', got '{data[key]}'"


@then("the response should be a JSON list")
def step_json_list(context: Context) -> None:
    data = context.response.json()
    assert isinstance(data, list), f"Expected list, got {type(data).__name__}"


# ─── Deployment steps ────────────────────────────────────────────

@when('I deploy using Asset Bundles with target "{target}"')
def step_deploy_bundle(context: Context, target: str) -> None:
    result = subprocess.run(
        ["databricks", "bundle", "deploy", "--target", target],
        capture_output=True,
        text=True,
        env={**dict(os.environ), "DATABRICKS_BUNDLE_ENGINE": "direct"},
        timeout=300,
    )
    context.deploy_result = result


@then("the deployment should succeed")
def step_deploy_success(context: Context) -> None:
    r = context.deploy_result
    assert r.returncode == 0, (
        f"Deploy failed (rc={r.returncode}):\n{r.stderr[:500]}"
    )
```

---

## Shell Command Steps (reusable)

```python
"""Step definitions for running CLI commands (DABs, databricks CLI)."""
from __future__ import annotations

import os
import subprocess

from behave import when, then
from behave.runner import Context


@when('I run "{command}" with target "{target}"')
def step_run_command(context: Context, command: str, target: str) -> None:
    full_cmd = f"{command} --target {target}"
    context.cmd_result = subprocess.run(
        full_cmd.split(),
        capture_output=True,
        text=True,
        env={**dict(os.environ), "DATABRICKS_BUNDLE_ENGINE": "direct"},
        timeout=300,
    )


@when('I run "{command}" with target "{target}" and auto-approve')
def step_run_command_approve(context: Context, command: str, target: str) -> None:
    full_cmd = f"{command} --target {target} --auto-approve"
    context.cmd_result = subprocess.run(
        full_cmd.split(),
        capture_output=True,
        text=True,
        env={**dict(os.environ), "DATABRICKS_BUNDLE_ENGINE": "direct"},
        timeout=300,
    )


@then("the command should exit with code {code:d}")
def step_exit_code(context: Context, code: int) -> None:
    actual = context.cmd_result.returncode
    assert actual == code, (
        f"Expected exit code {code}, got {actual}.\n"
        f"stdout: {context.cmd_result.stdout[:300]}\n"
        f"stderr: {context.cmd_result.stderr[:300]}"
    )


@then("the command should succeed")
def step_command_success(context: Context) -> None:
    assert context.cmd_result.returncode == 0, (
        f"Command failed (rc={context.cmd_result.returncode}):\n"
        f"{context.cmd_result.stderr[:500]}"
    )
```
