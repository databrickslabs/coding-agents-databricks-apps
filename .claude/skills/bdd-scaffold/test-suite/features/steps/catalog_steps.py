"""Step definitions for Unity Catalog operations.

Table names in Gherkin are short ("customers").
Schema resolution happens in _fqn() via context.test_schema.
"""
from __future__ import annotations

from behave import when, then
from behave.runner import Context


@when("I list tables in the test schema")
def step_list_tables(context: Context) -> None:
    catalog, schema = context.test_schema.split(".", 1)
    context.table_list = list(
        context.workspace.tables.list(catalog_name=catalog, schema_name=schema)
    )


@then("the table list should include {table_count:d} tables")
def step_table_count(context: Context, table_count: int) -> None:
    actual = len(context.table_list)
    assert actual == table_count, (
        f"Expected {table_count} tables, got {actual}: "
        f"{[t.name for t in context.table_list]}"
    )


@then('the table list should include "{table_name}"')
def step_table_in_list(context: Context, table_name: str) -> None:
    names = [t.name for t in context.table_list]
    assert table_name in names, f"Table '{table_name}' not in list: {names}"


@when('I grant {privilege} on managed table "{table_name}" to group "{group}"')
def step_grant_privilege(
    context: Context, privilege: str, table_name: str, group: str
) -> None:
    """Grant privilege using SQL — more stable across SDK versions than the grants API."""
    from databricks.sdk.service.sql import StatementState

    fqn = f"{context.test_schema}.{table_name}"
    result = context.workspace.statement_execution.execute_statement(
        warehouse_id=context.warehouse_id,
        statement=f"GRANT {privilege} ON TABLE {fqn} TO `{group}`",
        wait_timeout="30s",
    )
    assert result.status.state == StatementState.SUCCEEDED, (
        f"GRANT failed: {result.status.error}"
    )


@then('the group "{group}" should have {privilege} on managed table "{table_name}"')
def step_verify_permission(
    context: Context, group: str, privilege: str, table_name: str
) -> None:
    """Verify privilege using SHOW GRANTS — stable across SDK versions."""
    from databricks.sdk.service.sql import StatementState

    fqn = f"{context.test_schema}.{table_name}"
    result = context.workspace.statement_execution.execute_statement(
        warehouse_id=context.warehouse_id,
        statement=f"SHOW GRANTS ON TABLE {fqn}",
        wait_timeout="30s",
    )
    assert result.status.state == StatementState.SUCCEEDED, (
        f"SHOW GRANTS failed: {result.status.error}"
    )
    # Parse result: columns are Principal, ActionType, ObjectType, ObjectKey (PascalCase)
    rows = result.result.data_array or []
    columns = [c.name for c in result.manifest.schema.columns]
    # Handle case variation — normalize to lowercase for lookup
    col_lower = [c.lower() for c in columns]
    principal_idx = col_lower.index("principal")
    action_idx = col_lower.index("actiontype")

    found = any(
        row[principal_idx] == group and row[action_idx] == privilege
        for row in rows
    )
    assert found, (
        f"Expected {group} to have {privilege} on {fqn}. "
        f"Grants found: {[(r[principal_idx], r[action_idx]) for r in rows]}"
    )
