"""Behave environment hooks — Databricks SDK integration.

Tested against azure-east workspace.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

from behave.model import Feature, Scenario, Step
from behave.runner import Context

logger = logging.getLogger("behave.databricks")


def before_all(context: Context) -> None:
    """Initialize Databricks clients and create ephemeral test schema."""
    from databricks.sdk import WorkspaceClient

    # Use profile from env or default
    profile = os.environ.get("DATABRICKS_CONFIG_PROFILE", "azure-east")
    context.workspace = WorkspaceClient(profile=profile)

    # Fix host URL — some profiles include ?o=<org_id> which breaks SDK API paths
    if context.workspace.config.host and "?" in context.workspace.config.host:
        clean_host = context.workspace.config.host.split("?")[0].rstrip("/")
        context.workspace = WorkspaceClient(profile=profile, host=clean_host)

    me = context.workspace.current_user.me()
    context.current_user = me.user_name
    logger.info("Authenticated as: %s", context.current_user)

    # Warehouse — from -D userdata, env var, or auto-discover
    userdata = context.config.userdata
    wh_id = userdata.get("warehouse_id", "auto")
    if wh_id == "auto":
        wh_id = os.environ.get("DATABRICKS_WAREHOUSE_ID") or _discover_warehouse(
            context.workspace
        )
    context.warehouse_id = wh_id
    logger.info("Using warehouse: %s", context.warehouse_id)

    # Catalog
    context.test_catalog = userdata.get("catalog", "main")

    # Create ephemeral schema
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    context.test_schema = f"{context.test_catalog}.behave_test_{ts}"

    _execute_sql(context, f"CREATE SCHEMA IF NOT EXISTS {context.test_schema}")
    logger.info("Created test schema: %s", context.test_schema)


def after_all(context: Context) -> None:
    """Drop ephemeral test schema."""
    if hasattr(context, "test_schema"):
        try:
            _execute_sql(
                context, f"DROP SCHEMA IF EXISTS {context.test_schema} CASCADE"
            )
            logger.info("Dropped test schema: %s", context.test_schema)
        except Exception as e:
            logger.warning("Failed to drop test schema %s: %s", context.test_schema, e)


def before_feature(context: Context, feature: Feature) -> None:
    logger.info("▶ Feature: %s", feature.name)
    if "skip" in feature.tags:
        feature.skip("Marked with @skip")


def after_feature(context: Context, feature: Feature) -> None:
    logger.info("◀ Feature: %s [%s]", feature.name, feature.status)


def before_scenario(context: Context, scenario: Scenario) -> None:
    logger.info("  ▶ Scenario: %s", scenario.name)
    if "wip" in scenario.tags:
        scenario.skip("Work in progress")
        return
    context.scenario_cleanup_sql = []


def after_scenario(context: Context, scenario: Scenario) -> None:
    for sql in getattr(context, "scenario_cleanup_sql", []):
        try:
            _execute_sql(context, sql)
        except Exception as e:
            logger.warning("Cleanup SQL failed: %s — %s", sql, e)
    if scenario.status == "failed":
        logger.error("  ✗ FAILED: %s", scenario.name)


def before_step(context: Context, step: Step) -> None:
    context._step_start = datetime.now()


def after_step(context: Context, step: Step) -> None:
    elapsed = (datetime.now() - context._step_start).total_seconds()
    if elapsed > 10:
        logger.warning("    Slow step (%.1fs): %s %s", elapsed, step.keyword, step.name)
    if step.status == "failed":
        logger.error(
            "    ✗ %s %s\n      %s", step.keyword, step.name, step.error_message
        )


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
    for wh in warehouses:
        if wh.state == State.RUNNING:
            return wh.id
    if warehouses:
        return warehouses[0].id
    raise RuntimeError("No SQL warehouses found")
