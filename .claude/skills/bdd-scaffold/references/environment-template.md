# environment.py Template — Databricks + Behave

Complete annotated template for `features/environment.py`. Copy and adapt to the target project.

## Full template

```python
"""Behave environment hooks — Databricks SDK integration.

Sets up workspace connection, ephemeral test schema, and per-scenario cleanup.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

from behave.model import Feature, Scenario, Step
from behave.runner import Context

logger = logging.getLogger("behave.databricks")


# ─── Session-level hooks ────────────────────────────────────────

def before_all(context: Context) -> None:
    """Initialize Databricks clients and create ephemeral test schema."""
    from databricks.sdk import WorkspaceClient

    context.workspace = WorkspaceClient()

    # Fix host URL — some profiles include ?o=<org_id> which breaks SDK API paths.
    # The CLI handles this transparently but the SDK does not.
    if context.workspace.config.host and "?" in context.workspace.config.host:
        clean_host = context.workspace.config.host.split("?")[0].rstrip("/")
        profile = os.environ.get("DATABRICKS_CONFIG_PROFILE")
        context.workspace = WorkspaceClient(profile=profile, host=clean_host)

    # Verify auth
    me = context.workspace.current_user.me()
    context.current_user = me.user_name
    logger.info("Authenticated as: %s", context.current_user)

    # Warehouse — from -D userdata, env var, or auto-discover
    userdata = context.config.userdata
    context.warehouse_id = (
        userdata.get("warehouse_id")
        or os.environ.get("DATABRICKS_WAREHOUSE_ID")
        or _discover_warehouse(context.workspace)
    )
    logger.info("Using warehouse: %s", context.warehouse_id)

    # Catalog — from -D userdata or env var
    context.test_catalog = userdata.get("catalog", os.environ.get("TEST_CATALOG", "main"))

    # Create ephemeral schema (timestamped for isolation)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    worker = os.environ.get("BEHAVE_WORKER_ID", "0")
    context.test_schema = f"{context.test_catalog}.behave_test_{ts}_w{worker}"

    _execute_sql(context, f"CREATE SCHEMA IF NOT EXISTS {context.test_schema}")
    logger.info("Created test schema: %s", context.test_schema)


def after_all(context: Context) -> None:
    """Drop ephemeral test schema."""
    if hasattr(context, "test_schema"):
        try:
            _execute_sql(context, f"DROP SCHEMA IF EXISTS {context.test_schema} CASCADE")
            logger.info("Dropped test schema: %s", context.test_schema)
        except Exception as e:
            logger.warning("Failed to drop test schema %s: %s", context.test_schema, e)


# ─── Feature-level hooks ────────────────────────────────────────

def before_feature(context: Context, feature: Feature) -> None:
    """Log feature start. Skip if tagged @skip."""
    logger.info("▶ Feature: %s", feature.name)
    if "skip" in feature.tags:
        feature.skip("Marked with @skip")


def after_feature(context: Context, feature: Feature) -> None:
    logger.info("◀ Feature: %s [%s]", feature.name, feature.status)


# ─── Scenario-level hooks ───────────────────────────────────────

def before_scenario(context: Context, scenario: Scenario) -> None:
    """Initialize per-scenario state. Skip @wip scenarios."""
    logger.info("  ▶ Scenario: %s", scenario.name)
    if "wip" in scenario.tags:
        scenario.skip("Work in progress")
        return
    # Track resources created during this scenario for cleanup
    context.scenario_cleanup_sql = []


def after_scenario(context: Context, scenario: Scenario) -> None:
    """Clean up scenario-specific resources."""
    for sql in getattr(context, "scenario_cleanup_sql", []):
        try:
            _execute_sql(context, sql)
        except Exception as e:
            logger.warning("Cleanup SQL failed: %s — %s", sql, e)
    if scenario.status == "failed":
        logger.error("  ✗ FAILED: %s", scenario.name)
    else:
        logger.info("  ◀ Scenario: %s [%s]", scenario.name, scenario.status)


# ─── Step-level hooks ───────────────────────────────────────────

def before_step(context: Context, step: Step) -> None:
    context._step_start = datetime.now()


def after_step(context: Context, step: Step) -> None:
    elapsed = (datetime.now() - context._step_start).total_seconds()
    if elapsed > 10:
        logger.warning("    Slow step (%.1fs): %s %s", elapsed, step.keyword, step.name)
    if step.status == "failed":
        logger.error("    ✗ %s %s\n      %s", step.keyword, step.name, step.error_message)


# ─── Tag-based hooks ────────────────────────────────────────────

def before_tag(context, tag: str) -> None:
    """Ensure resources for tagged scenarios."""
    if tag == "fixture.sql_warehouse":
        _ensure_warehouse_running(context)


# ─── Helpers ────────────────────────────────────────────────────

def _execute_sql(context: Context, sql: str) -> object:
    """Execute a SQL statement via the Statement Execution API."""
    return context.workspace.statement_execution.execute_statement(
        warehouse_id=context.warehouse_id,
        statement=sql,
        wait_timeout="30s",
    )


def _discover_warehouse(workspace) -> str:
    """Find the first available SQL warehouse."""
    from databricks.sdk.service.sql import State

    warehouses = list(workspace.warehouses.list())
    # Prefer running warehouses
    for wh in warehouses:
        if wh.state == State.RUNNING:
            return wh.id
    if warehouses:
        return warehouses[0].id
    raise RuntimeError(
        "No SQL warehouses found. Pass warehouse_id via -D warehouse_id=<id> "
        "or set DATABRICKS_WAREHOUSE_ID."
    )


def _ensure_warehouse_running(context: Context) -> None:
    """Start warehouse if stopped. Used by @fixture.sql_warehouse tag."""
    from databricks.sdk.service.sql import State

    wh = context.workspace.warehouses.get(context.warehouse_id)
    if wh.state != State.RUNNING:
        logger.info("Starting warehouse %s...", context.warehouse_id)
        context.workspace.warehouses.start(context.warehouse_id)
        context.workspace.warehouses.wait_get_warehouse_running(context.warehouse_id)
        logger.info("Warehouse %s is running.", context.warehouse_id)
```

## Context object layering

Behave's `context` has scoped layers. Data set at different levels has different lifetimes:

| Set in | Lifetime | Example |
|--------|----------|---------|
| `before_all` | Entire run | `context.workspace`, `context.test_schema` |
| `before_feature` | Current feature | `context.feature_data` |
| `before_scenario` / steps | Current scenario | `context.query_result`, `context.scenario_cleanup_sql` |

At the end of each scenario, the scenario layer is popped — anything set during steps is gone. Root-level data persists across everything.

## Parallel execution isolation

When using `behavex` for parallel execution, each worker needs its own schema. The template uses `BEHAVE_WORKER_ID` from the environment. Set it in the parallel runner config or wrapper script:

```bash
# Example wrapper for behavex
export BEHAVE_WORKER_ID=$WORKER_INDEX
behave "$@"
```
