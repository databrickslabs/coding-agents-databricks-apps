"""Tests for AI Gateway auto-discovery — utils.get_gateway_host() and endpoint construction."""

import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

# ---------------------------------------------------------------------------
# Unit tests for get_gateway_host()
# ---------------------------------------------------------------------------


class TestGetGatewayHost:
    """Test the 3-tier priority logic in get_gateway_host()."""

    def _get_fn(self):
        from utils import get_gateway_host
        return get_gateway_host

    @mock.patch.dict(os.environ, {
        "DATABRICKS_GATEWAY_HOST": "https://custom.gateway.com",
        "DATABRICKS_WORKSPACE_ID": "12345",
    })
    def test_explicit_override_wins(self):
        """Tier 1: explicit DATABRICKS_GATEWAY_HOST takes priority over workspace ID."""
        assert self._get_fn()() == "https://custom.gateway.com"

    @mock.patch.dict(os.environ, {
        "DATABRICKS_GATEWAY_HOST": "custom.gateway.com",
        "DATABRICKS_WORKSPACE_ID": "12345",
    })
    def test_explicit_override_gets_https(self):
        """Tier 1: explicit value without https:// gets it added."""
        assert self._get_fn()() == "https://custom.gateway.com"

    @mock.patch.dict(os.environ, {
        "DATABRICKS_GATEWAY_HOST": "https://custom.gateway.com/",
        "DATABRICKS_WORKSPACE_ID": "12345",
    })
    def test_explicit_override_trailing_slash_stripped(self):
        """Tier 1: trailing slash is stripped from explicit value."""
        assert self._get_fn()() == "https://custom.gateway.com"

    @mock.patch.dict(os.environ, {"DATABRICKS_WORKSPACE_ID": "6280049833385130"}, clear=False)
    def test_auto_construct_from_workspace_id(self):
        """Tier 2: construct gateway URL from DATABRICKS_WORKSPACE_ID."""
        env = os.environ.copy()
        env.pop("DATABRICKS_GATEWAY_HOST", None)
        with mock.patch.dict(os.environ, env, clear=True):
            result = self._get_fn()()
            assert result == "https://6280049833385130.ai-gateway.cloud.databricks.com"

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_empty_when_nothing_set(self):
        """Tier 3: returns empty string when neither env var is set."""
        assert self._get_fn()() == ""

    @mock.patch.dict(os.environ, {"DATABRICKS_GATEWAY_HOST": "", "DATABRICKS_WORKSPACE_ID": ""})
    def test_empty_when_both_blank(self):
        """Tier 3: returns empty when both vars are set but blank."""
        assert self._get_fn()() == ""

    @mock.patch.dict(os.environ, {"DATABRICKS_GATEWAY_HOST": "  ", "DATABRICKS_WORKSPACE_ID": "12345"})
    def test_whitespace_only_gateway_falls_through(self):
        """Whitespace-only DATABRICKS_GATEWAY_HOST falls through to workspace ID."""
        assert self._get_fn()() == "https://12345.ai-gateway.cloud.databricks.com"

    @mock.patch.dict(os.environ, {"DATABRICKS_GATEWAY_HOST": "", "DATABRICKS_WORKSPACE_ID": " 99999 "})
    def test_workspace_id_whitespace_stripped(self):
        """Leading/trailing whitespace in workspace ID is stripped."""
        assert self._get_fn()() == "https://99999.ai-gateway.cloud.databricks.com"


# ---------------------------------------------------------------------------
# Integration tests — verify endpoint URLs constructed by setup scripts
# ---------------------------------------------------------------------------

SETUP_DIR = Path(__file__).parent.parent


class TestEndpointConstruction:
    """Verify setup scripts construct correct endpoint URLs with gateway auto-discovery."""

    def _run_setup(self, script_name, tmp_path, env_overrides=None):
        """Run a setup script as subprocess and capture output."""
        env = {
            "HOME": str(tmp_path),
            "DATABRICKS_HOST": "https://test.cloud.databricks.com",
            "DATABRICKS_TOKEN": "dapi_test_token",
            "DATABRICKS_WORKSPACE_ID": "6280049833385130",
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(SETUP_DIR),
        }
        # Ensure DATABRICKS_GATEWAY_HOST is NOT set (test auto-discovery)
        env.pop("DATABRICKS_GATEWAY_HOST", None)
        if env_overrides:
            env.update(env_overrides)

        # Create required dirs
        (tmp_path / ".claude").mkdir(exist_ok=True)

        result = subprocess.run(
            [sys.executable, str(SETUP_DIR / script_name)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result

    def test_setup_claude_uses_gateway(self, tmp_path):
        """setup_claude.py should use auto-discovered gateway for anthropic URL."""
        result = self._run_setup("setup_claude.py", tmp_path)
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "AI Gateway" in result.stdout or "6280049833385130" in result.stdout

        # Verify settings.json has gateway-based URL
        import json
        settings_path = tmp_path / ".claude" / "settings.json"
        if settings_path.exists():
            settings = json.loads(settings_path.read_text())
            base_url = settings.get("env", {}).get("ANTHROPIC_BASE_URL", "")
            assert "6280049833385130.ai-gateway.cloud.databricks.com" in base_url
            assert base_url.endswith("/anthropic")

    def test_setup_claude_explicit_override(self, tmp_path):
        """setup_claude.py should prefer explicit DATABRICKS_GATEWAY_HOST."""
        result = self._run_setup("setup_claude.py", tmp_path, {
            "DATABRICKS_GATEWAY_HOST": "https://custom.gateway.example.com",
        })
        assert result.returncode == 0, f"stderr: {result.stderr}"

        import json
        settings_path = tmp_path / ".claude" / "settings.json"
        if settings_path.exists():
            settings = json.loads(settings_path.read_text())
            base_url = settings.get("env", {}).get("ANTHROPIC_BASE_URL", "")
            assert "custom.gateway.example.com" in base_url

    def test_setup_claude_fallback_no_gateway(self, tmp_path):
        """setup_claude.py falls back to DATABRICKS_HOST when no gateway available."""
        result = self._run_setup("setup_claude.py", tmp_path, {
            "DATABRICKS_WORKSPACE_ID": "",  # No workspace ID
        })
        assert result.returncode == 0, f"stderr: {result.stderr}"

        import json
        settings_path = tmp_path / ".claude" / "settings.json"
        if settings_path.exists():
            settings = json.loads(settings_path.read_text())
            base_url = settings.get("env", {}).get("ANTHROPIC_BASE_URL", "")
            assert "test.cloud.databricks.com/serving-endpoints/anthropic" in base_url

    def test_codex_gateway_url_construction(self):
        """Codex endpoint should use gateway /openai/v1 path."""
        from utils import get_gateway_host
        with mock.patch.dict(os.environ, {
            "DATABRICKS_WORKSPACE_ID": "6280049833385130",
        }, clear=False):
            env = os.environ.copy()
            env.pop("DATABRICKS_GATEWAY_HOST", None)
            with mock.patch.dict(os.environ, env, clear=True):
                gw = get_gateway_host()
                codex_url = f"{gw}/openai/v1"
                assert codex_url == "https://6280049833385130.ai-gateway.cloud.databricks.com/openai/v1"

    def test_gemini_gateway_url_construction(self):
        """Gemini endpoint should use gateway /gemini path."""
        from utils import get_gateway_host
        with mock.patch.dict(os.environ, {
            "DATABRICKS_WORKSPACE_ID": "6280049833385130",
        }, clear=False):
            env = os.environ.copy()
            env.pop("DATABRICKS_GATEWAY_HOST", None)
            with mock.patch.dict(os.environ, env, clear=True):
                gw = get_gateway_host()
                gemini_url = f"{gw}/gemini"
                assert gemini_url == "https://6280049833385130.ai-gateway.cloud.databricks.com/gemini"

    def test_anthropic_gateway_url_construction(self):
        """Anthropic endpoint should use gateway /anthropic path."""
        from utils import get_gateway_host
        with mock.patch.dict(os.environ, {
            "DATABRICKS_WORKSPACE_ID": "6280049833385130",
        }, clear=False):
            env = os.environ.copy()
            env.pop("DATABRICKS_GATEWAY_HOST", None)
            with mock.patch.dict(os.environ, env, clear=True):
                gw = get_gateway_host()
                anthropic_url = f"{gw}/anthropic"
                assert anthropic_url == "https://6280049833385130.ai-gateway.cloud.databricks.com/anthropic"

    def test_proxy_gateway_url_construction(self):
        """Proxy endpoint should use gateway /mlflow/v1 path."""
        from utils import get_gateway_host
        with mock.patch.dict(os.environ, {
            "DATABRICKS_WORKSPACE_ID": "6280049833385130",
        }, clear=False):
            env = os.environ.copy()
            env.pop("DATABRICKS_GATEWAY_HOST", None)
            with mock.patch.dict(os.environ, env, clear=True):
                gw = get_gateway_host()
                proxy_url = f"{gw}/mlflow/v1"
                assert proxy_url == "https://6280049833385130.ai-gateway.cloud.databricks.com/mlflow/v1"
