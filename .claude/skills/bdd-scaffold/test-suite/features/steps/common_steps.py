"""Shared step definitions for Databricks BDD tests.

Design principle: Gherkin uses short table names ("customers").
Step definitions prepend context.test_schema internally.
This avoids {curly_brace} conflicts with Behave's parse library.
"""
from __future__ import annotations

from behave import given, when, then, step
from behave.runner import Context
from databricks.sdk.service.sql import StatementState


# ─── Connection and setup ────────────────────────────────────────


@given("a Databricks workspace connection is established")
def step_workspace_connection(context: Context) -> None:
    assert hasattr(context, "workspace"), "No workspace client — check environment.py"
    assert hasattr(context, "warehouse_id"), "No warehouse_id — check environment.py"


@given("a test schema is provisioned")
def step_test_schema(context: Context) -> None:
    assert hasattr(context, "test_schema"), "No test_schema — check environment.py"


# ─── Table creation ──────────────────────────────────────────────


@given('a managed table "{table_name}" exists')
def step_ensure_table(context: Context, table_name: str) -> None:
    fqn = _fqn(context, table_name)
    _sql(context, f"CREATE TABLE IF NOT EXISTS {fqn} (id BIGINT)")
    context.scenario_cleanup_sql.append(f"DROP TABLE IF EXISTS {fqn}")


@given('a managed table "{table_name}" with data:')
def step_create_with_data(context: Context, table_name: str) -> None:
    fqn = _fqn(context, table_name)
    headers = context.table.headings
    rows = context.table.rows

    col_defs = ", ".join(f"`{h}` STRING" for h in headers)
    _sql(context, f"CREATE OR REPLACE TABLE {fqn} ({col_defs})")
    context.scenario_cleanup_sql.append(f"DROP TABLE IF EXISTS {fqn}")

    for row in rows:
        values = ", ".join(f"'{cell}'" for cell in row)
        _sql(context, f"INSERT INTO {fqn} VALUES ({values})")


# ─── SQL execution ───────────────────────────────────────────────


@when("I execute a query on the test schema:")
def step_execute_sql(context: Context) -> None:
    """Execute SQL from docstring. Use {schema} as placeholder for test schema."""
    sql = context.text.replace("{schema}", context.test_schema)
    context.query_result = _sql(context, sql)


# ─── Table assertions ────────────────────────────────────────────


@then('the managed table "{table_name}" should exist')
def step_table_exists(context: Context, table_name: str) -> None:
    fqn = _fqn(context, table_name)
    try:
        context.workspace.tables.get(fqn)
    except Exception as e:
        raise AssertionError(f"Table {fqn} does not exist: {e}")


@then('the managed table "{table_name}" should have {expected:d} rows')
def step_row_count(context: Context, table_name: str, expected: int) -> None:
    fqn = _fqn(context, table_name)
    result = _sql(context, f"SELECT COUNT(*) AS cnt FROM {fqn}")
    actual = int(result.result.data_array[0][0])
    assert actual == expected, f"Expected {expected} rows in {table_name}, got {actual}"


@then("the test schema should exist in Unity Catalog")
def step_schema_exists(context: Context) -> None:
    try:
        context.workspace.schemas.get(context.test_schema)
    except Exception as e:
        raise AssertionError(f"Schema {context.test_schema} does not exist: {e}")


# ─── Query result assertions ────────────────────────────────────


@then("the query result should have {expected:d} rows")
def step_result_row_count(context: Context, expected: int) -> None:
    rows = context.query_result.result.data_array or []
    actual = len(rows)
    assert actual == expected, f"Expected {expected} result rows, got {actual}"


@then('the first result column "{col}" should be "{value}"')
def step_first_result_value(context: Context, col: str, value: str) -> None:
    result = context.query_result
    columns = [c.name for c in result.manifest.schema.columns]
    assert col in columns, f"Column '{col}' not in result: {columns}"
    col_idx = columns.index(col)
    actual = result.result.data_array[0][col_idx]
    assert str(actual) == value, f"Expected {col}='{value}', got '{actual}'"


# ─── Helpers ─────────────────────────────────────────────────────


def _fqn(context: Context, table_name: str) -> str:
    """Build fully-qualified table name from short name."""
    return f"{context.test_schema}.{table_name}"


def _sql(context: Context, sql: str):
    """Execute SQL and assert success."""
    result = context.workspace.statement_execution.execute_statement(
        warehouse_id=context.warehouse_id,
        statement=sql,
        wait_timeout="30s",
    )
    assert result.status.state == StatementState.SUCCEEDED, (
        f"SQL failed ({result.status.state}): {result.status.error}\n"
        f"Statement: {sql[:200]}"
    )
    return result
