"""Databricks Labs telemetry for CoDA.

Follows the DQX pattern: piggybacks telemetry on the Databricks SDK's
User-Agent header. Each log_telemetry() call creates a throwaway
WorkspaceClient, augments the User-Agent with key-value data, and fires
clusters.select_spark_version() to transmit the header to Databricks
servers where it's recorded.

All telemetry runs in background daemon threads -- never blocks the
Flask request path or terminal I/O.

Reference: https://github.com/databrickslabs/dqx/blob/main/src/databricks/labs/dqx/telemetry.py
"""

import functools
import logging
import os
import threading

import tomllib

logger = logging.getLogger(__name__)

_version_cache = None


def _get_version():
    """Get CoDA version from pyproject.toml (cached after first call)."""
    global _version_cache
    if _version_cache is not None:
        return _version_cache
    try:
        pyproject = os.path.join(os.path.dirname(__file__), "pyproject.toml")
        with open(pyproject, "rb") as f:
            _version_cache = tomllib.load(f)["project"]["version"]
    except Exception:
        _version_cache = "0.0.0"
    return _version_cache


def set_product_info(ws):
    """Set CoDA product info on a WorkspaceClient for telemetry attribution.

    Call this on any WorkspaceClient so all SDK API calls carry the 'coda'
    product identifier in the User-Agent header.
    """
    product_info = getattr(ws.config, "_product_info", None)
    if product_info is None or product_info[0] != "coda":
        setattr(ws.config, "_product_info", ("coda", _get_version()))


def log_telemetry(key, value):
    """Send a telemetry key-value pair via the Databricks SDK User-Agent header.

    Creates a throwaway WorkspaceClient from ~/.databrickscfg, adds the
    key-value to the User-Agent, and fires clusters.select_spark_version()
    to transmit. Runs in a background daemon thread. Errors are caught and
    logged, never raised.
    """

    def _send():
        try:
            from databricks.sdk import WorkspaceClient
            from databricks.sdk.errors import DatabricksError

            ws = WorkspaceClient()
            set_product_info(ws)
            new_config = ws.config.copy().with_user_agent_extra(key, value)
            temp_ws = WorkspaceClient(config=new_config)
            try:
                temp_ws.clusters.select_spark_version()
            except DatabricksError as e:
                logger.debug(f"Telemetry transmit failed: {e}")
        except Exception as e:
            logger.debug(f"Telemetry error ({key}={value}): {e}")

    threading.Thread(target=_send, daemon=True, name=f"telemetry-{key}").start()


def telemetry_logger(key, value):
    """Decorator that fires telemetry before executing the wrapped function.

    Works on standalone functions and class methods alike. Creates its own
    WorkspaceClient from ~/.databrickscfg -- no self.ws required.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                log_telemetry(key, value)
            except Exception:
                pass  # Telemetry must never break the wrapped function
            return func(*args, **kwargs)

        return wrapper

    return decorator
