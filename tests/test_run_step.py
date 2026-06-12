"""Tests for _run_step and _configure_all_cli_auth — env setup for subprocesses."""

import os
import subprocess
from unittest import mock

import pytest


# We need to test _run_step from app.py. It calls subprocess.run, so we mock that.
# The function also updates setup_state, so we mock that too.


@pytest.fixture
def patch_app_globals():
    """Patch app.py globals needed by _run_step."""
    with mock.patch("app._update_step"):
        yield


class TestRunStepEnvStripping:
    """Verify _run_step strips OAuth credentials from subprocess env."""

    def test_strips_databricks_client_id(self, patch_app_globals):
        from app import _run_step
        with mock.patch.dict(os.environ, {
            "DATABRICKS_CLIENT_ID": "sp-client-id",
            "DATABRICKS_CLIENT_SECRET": "sp-client-secret",
            "HOME": "/tmp/test-home",
        }), mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedResult = mock.MagicMock(
                returncode=0, stdout="ok", stderr=""
            )
            _run_step("test-step", "echo hello")

        call_env = mock_run.call_args.kwargs.get("env", {})
        assert "DATABRICKS_CLIENT_ID" not in call_env
        assert "DATABRICKS_CLIENT_SECRET" not in call_env

    def test_preserves_other_env_vars(self, patch_app_globals):
        from app import _run_step
        with mock.patch.dict(os.environ, {
            "HOME": "/tmp/test-home",
            "MY_CUSTOM_VAR": "keep-this",
            "DATABRICKS_CLIENT_ID": "remove-this",
        }), mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="ok", stderr="")
            _run_step("test-step", "echo hello")

        call_env = mock_run.call_args.kwargs.get("env", {})
        assert call_env.get("MY_CUSTOM_VAR") == "keep-this"


class TestRunStepPythonpath:
    """Verify _run_step injects PYTHONPATH for setup script imports."""

    def test_sets_pythonpath_to_app_dir(self, patch_app_globals):
        from app import _run_step
        with mock.patch.dict(os.environ, {"HOME": "/tmp/test-home"}), \
             mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="ok", stderr="")
            _run_step("test-step", "echo hello")

        call_env = mock_run.call_args.kwargs.get("env", {})
        # PYTHONPATH should contain the app directory (dirname of app.py)
        assert "PYTHONPATH" in call_env
        assert call_env["PYTHONPATH"]  # non-empty

    def test_prepends_to_existing_pythonpath(self, patch_app_globals):
        from app import _run_step
        with mock.patch.dict(os.environ, {
            "HOME": "/tmp/test-home",
            "PYTHONPATH": "/existing/path",
        }), mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="ok", stderr="")
            _run_step("test-step", "echo hello")

        call_env = mock_run.call_args.kwargs.get("env", {})
        assert "/existing/path" in call_env["PYTHONPATH"]


class TestRunStepPath:
    """Verify _run_step adds ~/.local/bin to PATH."""

    def test_adds_local_bin_to_path(self, patch_app_globals):
        from app import _run_step
        with mock.patch.dict(os.environ, {
            "HOME": "/tmp/test-home",
            "PATH": "/usr/bin",
        }), mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="ok", stderr="")
            _run_step("test-step", "echo hello")

        call_env = mock_run.call_args.kwargs.get("env", {})
        assert "/tmp/test-home/.local/bin" in call_env["PATH"]

    def test_skips_if_already_in_path(self, patch_app_globals):
        from app import _run_step
        with mock.patch.dict(os.environ, {
            "HOME": "/tmp/test-home",
            "PATH": "/tmp/test-home/.local/bin:/usr/bin",
        }), mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="ok", stderr="")
            _run_step("test-step", "echo hello")

        call_env = mock_run.call_args.kwargs.get("env", {})
        # Should not duplicate
        assert call_env["PATH"].count(".local/bin") == 1

    def test_defaults_home_when_empty(self, patch_app_globals):
        """When HOME is empty or '/', should default to /app/python/source_code."""
        from app import _run_step
        with mock.patch.dict(os.environ, {"HOME": ""}, clear=False), \
             mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="ok", stderr="")
            _run_step("test-step", "echo hello")

        call_env = mock_run.call_args.kwargs.get("env", {})
        assert "/app/python/source_code" in call_env.get("HOME", "")


# ---------------------------------------------------------------------------
# _configure_all_cli_auth — PAT reconfiguration path
# ---------------------------------------------------------------------------

class TestConfigureAllCliAuth:
    """Verify _configure_all_cli_auth injects PYTHONPATH for setup script imports.

    This is a separate code path from _run_step — it runs setup scripts via
    subprocess.run after PAT rotation. Without PYTHONPATH, the scripts can't
    `from utils import ...` since they live in setup/ subdirectory.
    """

    def _call_configure(self, mock_run, tmp_path, token="dapi_test"):
        """Helper to call _configure_all_cli_auth with all dependencies mocked."""
        from app import _configure_all_cli_auth
        # Create .claude dir so settings.json write succeeds
        (tmp_path / ".claude").mkdir(exist_ok=True)
        with mock.patch("utils.resolve_and_cache_gateway"), \
             mock.patch("app.get_gateway_host", return_value=None), \
             mock.patch("app.ensure_https", return_value="https://test.databricks.com"), \
             mock.patch("app.pat_rotator"), \
             mock.patch.dict(os.environ, {"HOME": str(tmp_path)}):
            _configure_all_cli_auth(token)

    def test_injects_pythonpath(self, tmp_path):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
            self._call_configure(mock_run, tmp_path)

        # Find a subprocess call that runs a setup script
        setup_calls = [c for c in mock_run.call_args_list
                       if any("setup/" in str(a) for a in c[0][0])]
        assert len(setup_calls) > 0, "Expected subprocess calls for setup scripts"

        for call in setup_calls:
            call_env = call.kwargs.get("env") or call[1].get("env", {})
            assert "PYTHONPATH" in call_env, f"PYTHONPATH missing from env for {call[0][0]}"
            assert call_env["PYTHONPATH"], "PYTHONPATH should not be empty"

    def test_passes_token_in_env(self, tmp_path):
        with mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.MagicMock(returncode=0, stdout="", stderr="")
            self._call_configure(mock_run, tmp_path, token="dapi_mytoken")

        setup_calls = [c for c in mock_run.call_args_list
                       if any("setup/" in str(a) for a in c[0][0])]
        for call in setup_calls:
            call_env = call.kwargs.get("env") or call[1].get("env", {})
            assert call_env.get("DATABRICKS_TOKEN") == "dapi_mytoken"
