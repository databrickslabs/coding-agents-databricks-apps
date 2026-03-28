"""Tests for MLflow tracing setup — setup_mlflow.py + app.py integration."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SETUP_MLFLOW = Path(__file__).parent.parent / "setup_mlflow.py"


def run_setup_mlflow(tmp_path, env_overrides=None):
    """Run setup_mlflow.py as a subprocess with controlled env, using tmp_path as HOME."""
    env = {
        "HOME": str(tmp_path),
        "DATABRICKS_HOST": "https://test.cloud.databricks.com",
        "DATABRICKS_TOKEN": "dapi_test_token",
        "PATH": os.environ.get("PATH", ""),
    }
    if env_overrides:
        env.update(env_overrides)

    # Ensure ~/.claude/ exists (setup_claude.py would have created it)
    (tmp_path / ".claude").mkdir(exist_ok=True)

    result = subprocess.run(
        [sys.executable, str(SETUP_MLFLOW)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result


def write_existing_settings(tmp_path, settings):
    """Write a pre-existing settings.json (simulating setup_claude.py output)."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(exist_ok=True)
    (claude_dir / "settings.json").write_text(json.dumps(settings, indent=2))


def read_settings(tmp_path):
    """Read the resulting settings.json."""
    return json.loads((tmp_path / ".claude" / "settings.json").read_text())


# ---------------------------------------------------------------------------
# 1. MLflow env vars written correctly
# ---------------------------------------------------------------------------

class TestMlflowEnvVars:
    """Verify MLflow environment variables are added to settings.json."""

    def test_tracing_enabled(self, tmp_path):
        write_existing_settings(tmp_path, {"env": {"ANTHROPIC_MODEL": "test"}})
        result = run_setup_mlflow(tmp_path, {"APP_OWNER": "jane@company.com"})
        assert result.returncode == 0
        settings = read_settings(tmp_path)
        assert settings["env"]["MLFLOW_CLAUDE_TRACING_ENABLED"] == "false"

    def test_tracking_uri(self, tmp_path):
        write_existing_settings(tmp_path, {"env": {}})
        result = run_setup_mlflow(tmp_path, {"APP_OWNER": "jane@company.com"})
        assert result.returncode == 0
        settings = read_settings(tmp_path)
        assert settings["env"]["MLFLOW_TRACKING_URI"] == "databricks"

    def test_experiment_name_format(self, tmp_path):
        write_existing_settings(tmp_path, {"env": {}})
        result = run_setup_mlflow(tmp_path, {
            "APP_OWNER": "jane@company.com",
            "DATABRICKS_APP_NAME": "my-cool-app",
        })
        assert result.returncode == 0
        settings = read_settings(tmp_path)
        assert settings["env"]["MLFLOW_EXPERIMENT_NAME"] == "/Users/jane@company.com/my-cool-app"

    def test_experiment_name_default_app_name(self, tmp_path):
        write_existing_settings(tmp_path, {"env": {}})
        result = run_setup_mlflow(tmp_path, {"APP_OWNER": "jane@company.com"})
        assert result.returncode == 0
        settings = read_settings(tmp_path)
        assert settings["env"]["MLFLOW_EXPERIMENT_NAME"] == "/Users/jane@company.com/coding-agents"


# ---------------------------------------------------------------------------
# 2. Stop hook configured correctly
# ---------------------------------------------------------------------------

class TestStopHook:
    """Verify the MLflow Stop hook is added to settings.json."""

    def test_stop_hook_present(self, tmp_path):
        write_existing_settings(tmp_path, {"env": {}})
        result = run_setup_mlflow(tmp_path, {"APP_OWNER": "jane@company.com"})
        assert result.returncode == 0
        settings = read_settings(tmp_path)
        assert "hooks" in settings
        assert "Stop" in settings["hooks"]
        assert len(settings["hooks"]["Stop"]) == 1

    def test_stop_hook_command(self, tmp_path):
        write_existing_settings(tmp_path, {"env": {}})
        result = run_setup_mlflow(tmp_path, {"APP_OWNER": "jane@company.com"})
        assert result.returncode == 0
        settings = read_settings(tmp_path)
        hook = settings["hooks"]["Stop"][0]["hooks"][0]
        assert hook["type"] == "command"
        assert "stop_hook_handler" in hook["command"]
        assert "mlflow.claude_code.hooks" in hook["command"]


# ---------------------------------------------------------------------------
# 3. Existing settings preserved
# ---------------------------------------------------------------------------

class TestSettingsMerge:
    """Verify existing settings are not clobbered."""

    def test_preserves_existing_env_vars(self, tmp_path):
        write_existing_settings(tmp_path, {
            "env": {
                "ANTHROPIC_MODEL": "databricks-claude-opus-4-6",
                "ANTHROPIC_BASE_URL": "https://test.com/anthropic",
                "ANTHROPIC_AUTH_TOKEN": "secret",
            }
        })
        result = run_setup_mlflow(tmp_path, {"APP_OWNER": "jane@company.com"})
        assert result.returncode == 0
        settings = read_settings(tmp_path)
        assert settings["env"]["ANTHROPIC_MODEL"] == "databricks-claude-opus-4-6"
        assert settings["env"]["ANTHROPIC_BASE_URL"] == "https://test.com/anthropic"
        assert settings["env"]["ANTHROPIC_AUTH_TOKEN"] == "secret"
        assert settings["env"]["MLFLOW_CLAUDE_TRACING_ENABLED"] == "false"

    def test_preserves_existing_hooks(self, tmp_path):
        write_existing_settings(tmp_path, {
            "env": {},
            "hooks": {
                "PreToolUse": [{"hooks": [{"type": "command", "command": "echo pre"}]}]
            }
        })
        result = run_setup_mlflow(tmp_path, {"APP_OWNER": "jane@company.com"})
        assert result.returncode == 0
        settings = read_settings(tmp_path)
        assert "PreToolUse" in settings["hooks"]
        assert "Stop" in settings["hooks"]


# ---------------------------------------------------------------------------
# 4. Skips gracefully when APP_OWNER not set
# ---------------------------------------------------------------------------

class TestSkipWithoutOwner:
    """When APP_OWNER is not set, MLflow tracing should be skipped."""

    def test_exits_cleanly_without_app_owner(self, tmp_path):
        write_existing_settings(tmp_path, {"env": {"ANTHROPIC_MODEL": "test"}})
        result = run_setup_mlflow(tmp_path, {})  # No APP_OWNER
        assert result.returncode == 0
        assert "skipped" in result.stdout.lower()

    def test_settings_unchanged_without_app_owner(self, tmp_path):
        original = {"env": {"ANTHROPIC_MODEL": "test"}}
        write_existing_settings(tmp_path, original)
        run_setup_mlflow(tmp_path, {})
        settings = read_settings(tmp_path)
        assert "MLFLOW_CLAUDE_TRACING_ENABLED" not in settings["env"]
        assert "hooks" not in settings


# ---------------------------------------------------------------------------
# 5. APP_OWNER exported by initialize_app
# ---------------------------------------------------------------------------

class TestAppOwnerExport:
    """Verify app.py exports APP_OWNER to env for setup subprocesses."""

    def test_app_owner_set_in_env(self):
        import app as app_module
        with mock.patch.object(app_module, "get_token_owner", return_value="owner@test.com"), \
             mock.patch.object(app_module, "cleanup_stale_sessions"), \
             mock.patch.object(app_module, "run_setup"), \
             mock.patch("threading.Thread") as mock_thread:
            mock_thread.return_value.start = mock.MagicMock()
            app_module.initialize_app()
        assert os.environ.get("APP_OWNER") == "owner@test.com"

    def test_app_owner_not_set_when_unknown(self):
        import app as app_module
        os.environ.pop("APP_OWNER", None)
        with mock.patch.object(app_module, "get_token_owner", return_value=None), \
             mock.patch.object(app_module, "cleanup_stale_sessions"), \
             mock.patch.object(app_module, "run_setup"), \
             mock.patch("threading.Thread") as mock_thread:
            mock_thread.return_value.start = mock.MagicMock()
            app_module.initialize_app()
        assert os.environ.get("APP_OWNER") is None


# ---------------------------------------------------------------------------
# 6. Setup step registered in app.py
# ---------------------------------------------------------------------------

class TestSetupStepRegistered:
    """Verify the MLflow step appears in setup_state and run_setup."""

    def test_mlflow_step_in_setup_state(self):
        import app as app_module
        step_ids = [s["id"] for s in app_module.setup_state["steps"]]
        assert "mlflow" in step_ids

    def test_mlflow_step_label(self):
        import app as app_module
        mlflow_step = next(s for s in app_module.setup_state["steps"] if s["id"] == "mlflow")
        assert "MLflow" in mlflow_step["label"] or "mlflow" in mlflow_step["label"].lower()

    def test_run_setup_calls_mlflow_step(self):
        """Verify run_setup includes the mlflow step."""
        import inspect
        import app as app_module
        source = inspect.getsource(app_module.run_setup)
        assert '"mlflow"' in source
        assert "setup_mlflow.py" in source


# ---------------------------------------------------------------------------
# 7. Experiment path is absolute workspace path
# ---------------------------------------------------------------------------

class TestExperimentPath:
    """MLflow experiment names must be absolute Databricks workspace paths."""

    def test_starts_with_users(self, tmp_path):
        write_existing_settings(tmp_path, {"env": {}})
        result = run_setup_mlflow(tmp_path, {"APP_OWNER": "jane@company.com"})
        assert result.returncode == 0
        settings = read_settings(tmp_path)
        assert settings["env"]["MLFLOW_EXPERIMENT_NAME"].startswith("/Users/")

    def test_contains_owner_email(self, tmp_path):
        write_existing_settings(tmp_path, {"env": {}})
        result = run_setup_mlflow(tmp_path, {"APP_OWNER": "jane@company.com"})
        assert result.returncode == 0
        settings = read_settings(tmp_path)
        assert "jane@company.com" in settings["env"]["MLFLOW_EXPERIMENT_NAME"]
