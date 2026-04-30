"""Tests for Databricks Labs telemetry — telemetry.py module."""

import threading
import time
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wait_for_telemetry_threads(timeout=2.0):
    """Wait for any background telemetry threads to complete."""
    deadline = time.monotonic() + timeout
    for t in threading.enumerate():
        if t.name.startswith("telemetry-"):
            remaining = deadline - time.monotonic()
            if remaining > 0:
                t.join(timeout=remaining)


# ---------------------------------------------------------------------------
# _get_version
# ---------------------------------------------------------------------------


class TestGetVersion:
    def test_reads_from_pyproject(self):
        import telemetry

        # Reset cache so it re-reads
        telemetry._version_cache = None
        version = telemetry._get_version()
        assert version != "0.0.0"
        assert "." in version  # semver-ish

    def test_caches_result(self):
        import telemetry

        telemetry._version_cache = None
        v1 = telemetry._get_version()
        v2 = telemetry._get_version()
        assert v1 == v2
        assert telemetry._version_cache == v1

    def test_falls_back_on_missing_file(self, tmp_path):
        import telemetry

        telemetry._version_cache = None
        with mock.patch("telemetry.os.path.dirname", return_value=str(tmp_path)):
            version = telemetry._get_version()
        assert version == "0.0.0"
        # Reset for other tests
        telemetry._version_cache = None


# ---------------------------------------------------------------------------
# set_product_info
# ---------------------------------------------------------------------------


class TestSetProductInfo:
    def test_sets_product_info_on_ws(self):
        from telemetry import set_product_info

        ws = mock.MagicMock()
        ws.config._product_info = None

        set_product_info(ws)

        assert ws.config._product_info == ("coda", mock.ANY)
        assert ws.config._product_info[0] == "coda"

    def test_idempotent_when_already_set(self):
        from telemetry import set_product_info

        ws = mock.MagicMock()
        ws.config._product_info = ("coda", "0.17.2")

        set_product_info(ws)

        # Should not overwrite
        assert ws.config._product_info == ("coda", "0.17.2")

    def test_overwrites_different_product(self):
        from telemetry import set_product_info

        ws = mock.MagicMock()
        ws.config._product_info = ("other-project", "1.0.0")

        set_product_info(ws)

        assert ws.config._product_info[0] == "coda"


# ---------------------------------------------------------------------------
# log_telemetry
# ---------------------------------------------------------------------------


class TestLogTelemetry:
    @mock.patch("databricks.sdk.WorkspaceClient")
    def test_fires_in_background_thread(self, mock_ws_cls):
        from telemetry import log_telemetry

        mock_ws = mock.MagicMock()
        mock_ws.config._product_info = None
        mock_ws.config.copy.return_value.with_user_agent_extra.return_value = (
            mock.MagicMock()
        )
        mock_ws_cls.return_value = mock_ws

        log_telemetry("event", "test_event")
        _wait_for_telemetry_threads()

        # WorkspaceClient() called twice: once for initial ws, once for temp_ws with config
        assert mock_ws_cls.call_count == 2

    @mock.patch("databricks.sdk.WorkspaceClient")
    def test_calls_select_spark_version(self, mock_ws_cls):
        from telemetry import log_telemetry

        mock_ws = mock.MagicMock()
        mock_ws.config._product_info = None

        mock_config_copy = mock.MagicMock()
        mock_ws.config.copy.return_value.with_user_agent_extra.return_value = (
            mock_config_copy
        )

        mock_temp_ws = mock.MagicMock()
        # First call: WorkspaceClient() -> mock_ws, Second call: WorkspaceClient(config=...) -> mock_temp_ws
        mock_ws_cls.side_effect = [mock_ws, mock_temp_ws]

        log_telemetry("agent", "claude")
        _wait_for_telemetry_threads()

        # The temp WS should have clusters.select_spark_version() called
        assert mock_ws_cls.call_count == 2
        mock_temp_ws.clusters.select_spark_version.assert_called_once()

    @mock.patch("databricks.sdk.WorkspaceClient")
    def test_adds_user_agent_extra(self, mock_ws_cls):
        from telemetry import log_telemetry

        mock_ws = mock.MagicMock()
        mock_ws.config._product_info = None
        mock_ws_cls.return_value = mock_ws

        log_telemetry("event", "file_upload")
        _wait_for_telemetry_threads()

        mock_ws.config.copy.return_value.with_user_agent_extra.assert_called_once_with(
            "event", "file_upload"
        )

    @mock.patch("databricks.sdk.WorkspaceClient")
    def test_fire_and_forget_on_ws_error(self, mock_ws_cls):
        """Telemetry errors must never propagate to caller."""
        from telemetry import log_telemetry

        mock_ws_cls.side_effect = Exception("No databrickscfg")

        # Should not raise
        log_telemetry("event", "startup")
        _wait_for_telemetry_threads()

    @mock.patch("databricks.sdk.WorkspaceClient")
    def test_fire_and_forget_on_api_error(self, mock_ws_cls):
        """DatabricksError during transmit must be swallowed."""
        from databricks.sdk.errors import DatabricksError
        from telemetry import log_telemetry

        mock_ws = mock.MagicMock()
        mock_ws.config._product_info = None
        mock_config_copy = mock.MagicMock()
        mock_ws.config.copy.return_value.with_user_agent_extra.return_value = (
            mock_config_copy
        )
        mock_temp_ws = mock.MagicMock()
        mock_temp_ws.clusters.select_spark_version.side_effect = DatabricksError(
            "Forbidden"
        )
        mock_ws_cls.side_effect = [mock_ws, mock_temp_ws]

        # Should not raise
        log_telemetry("event", "test")
        _wait_for_telemetry_threads()

    def test_runs_in_daemon_thread(self):
        """Telemetry threads must be daemons so they don't block shutdown."""
        from telemetry import log_telemetry

        with mock.patch("databricks.sdk.WorkspaceClient") as mock_ws_cls:
            # Make the WS constructor block so we can inspect the thread
            barrier = threading.Event()

            def slow_init(*args, **kwargs):
                barrier.wait(timeout=5)
                return mock.MagicMock()

            mock_ws_cls.side_effect = slow_init

            log_telemetry("event", "test")

            # Find the telemetry thread
            telemetry_threads = [
                t for t in threading.enumerate() if t.name.startswith("telemetry-")
            ]
            assert len(telemetry_threads) >= 1
            assert telemetry_threads[0].daemon is True

            barrier.set()  # unblock
            _wait_for_telemetry_threads()


# ---------------------------------------------------------------------------
# telemetry_logger decorator
# ---------------------------------------------------------------------------


class TestTelemetryLogger:
    @mock.patch("telemetry.log_telemetry")
    def test_decorator_fires_telemetry(self, mock_log):
        from telemetry import telemetry_logger

        @telemetry_logger("event", "decorated_fn")
        def my_function(x, y):
            return x + y

        result = my_function(1, 2)

        assert result == 3
        mock_log.assert_called_once_with("event", "decorated_fn")

    @mock.patch("telemetry.log_telemetry")
    def test_preserves_function_metadata(self, mock_log):
        from telemetry import telemetry_logger

        @telemetry_logger("event", "test")
        def documented_function():
            """This is the docstring."""
            pass

        assert documented_function.__name__ == "documented_function"
        assert documented_function.__doc__ == "This is the docstring."

    @mock.patch("telemetry.log_telemetry")
    def test_passes_args_and_kwargs(self, mock_log):
        from telemetry import telemetry_logger

        @telemetry_logger("event", "test")
        def func_with_args(a, b, c=None):
            return (a, b, c)

        result = func_with_args("x", "y", c="z")
        assert result == ("x", "y", "z")

    @mock.patch("telemetry.log_telemetry", side_effect=Exception("boom"))
    def test_telemetry_failure_doesnt_break_function(self, mock_log):
        """If telemetry itself fails, the wrapped function must still execute."""
        from telemetry import telemetry_logger

        @telemetry_logger("event", "test")
        def important_function():
            return "success"

        # The decorator catches the exception from log_telemetry
        # but log_telemetry itself is fire-and-forget, so this tests
        # that the function still returns correctly
        result = important_function()
        assert result == "success"


# ---------------------------------------------------------------------------
# Integration: product_info + telemetry together
# ---------------------------------------------------------------------------


class TestProductInfoIntegration:
    @mock.patch("databricks.sdk.WorkspaceClient")
    def test_product_info_set_during_telemetry(self, mock_ws_cls):
        """log_telemetry should set product_info before transmitting."""
        from telemetry import log_telemetry

        mock_ws = mock.MagicMock()
        mock_ws.config._product_info = None
        mock_ws_cls.return_value = mock_ws

        log_telemetry("event", "startup")
        _wait_for_telemetry_threads()

        # product_info should have been set to ('coda', version)
        assert mock_ws.config._product_info[0] == "coda"
