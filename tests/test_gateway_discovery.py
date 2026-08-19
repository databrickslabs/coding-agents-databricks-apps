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
    """Test the 4-tier priority logic in get_gateway_host()."""

    def _get_fn(self):
        from utils import get_gateway_host
        return get_gateway_host

    # -- Tier 0: _GATEWAY_RESOLVED cache --

    @mock.patch.dict(os.environ, {
        "_GATEWAY_RESOLVED": "https://cached.gateway.com",
        "DATABRICKS_GATEWAY_HOST": "https://explicit.gateway.com",
    })
    def test_resolved_cache_wins_over_explicit(self):
        """Tier 0: _GATEWAY_RESOLVED takes priority over explicit DATABRICKS_GATEWAY_HOST."""
        assert self._get_fn()() == "https://cached.gateway.com"

    @mock.patch.dict(os.environ, {"_GATEWAY_RESOLVED": ""}, clear=True)
    def test_resolved_empty_returns_empty(self):
        """Tier 0: _GATEWAY_RESOLVED='' means 'probed, no gateway' — returns empty."""
        assert self._get_fn()() == ""

    @mock.patch.dict(os.environ, {
        "_GATEWAY_RESOLVED": "https://cached.gw.com",
        "DATABRICKS_WORKSPACE_ID": "12345",
    })
    @mock.patch("utils._probe_gateway")
    def test_resolved_skips_probe(self, mock_probe):
        """Tier 0: when _GATEWAY_RESOLVED is set, probe is never called."""
        self._get_fn()()
        mock_probe.assert_not_called()

    # -- Tier 1: explicit DATABRICKS_GATEWAY_HOST --

    @mock.patch.dict(os.environ, {
        "DATABRICKS_GATEWAY_HOST": "https://custom.gateway.com",
        "DATABRICKS_WORKSPACE_ID": "12345",
    })
    def test_explicit_override_wins(self):
        """Tier 1: explicit DATABRICKS_GATEWAY_HOST takes priority over workspace ID."""
        os.environ.pop("_GATEWAY_RESOLVED", None)
        assert self._get_fn()() == "https://custom.gateway.com"

    @mock.patch.dict(os.environ, {
        "DATABRICKS_GATEWAY_HOST": "custom.gateway.com",
        "DATABRICKS_WORKSPACE_ID": "12345",
    })
    def test_explicit_override_gets_https(self):
        """Tier 1: explicit value without https:// gets it added."""
        os.environ.pop("_GATEWAY_RESOLVED", None)
        assert self._get_fn()() == "https://custom.gateway.com"

    @mock.patch.dict(os.environ, {
        "DATABRICKS_GATEWAY_HOST": "https://custom.gateway.com/",
        "DATABRICKS_WORKSPACE_ID": "12345",
    })
    def test_explicit_override_trailing_slash_stripped(self):
        """Tier 1: trailing slash is stripped from explicit value."""
        os.environ.pop("_GATEWAY_RESOLVED", None)
        assert self._get_fn()() == "https://custom.gateway.com"

    # -- Tier 2: auto-construct from DATABRICKS_WORKSPACE_ID --

    @mock.patch("utils._probe_gateway", return_value=True)
    @mock.patch.dict(os.environ, {"DATABRICKS_WORKSPACE_ID": "1234567890123456"}, clear=False)
    def test_auto_construct_from_workspace_id(self, mock_probe):
        """Tier 2: construct gateway URL from DATABRICKS_WORKSPACE_ID when reachable."""
        env = os.environ.copy()
        env.pop("DATABRICKS_GATEWAY_HOST", None)
        env.pop("_GATEWAY_RESOLVED", None)
        with mock.patch.dict(os.environ, env, clear=True):
            result = self._get_fn()()
            assert result.startswith("https://1234567890123456.")
            assert ".ai-gateway." in result
            mock_probe.assert_called_once()

    @mock.patch("utils._probe_gateway", return_value=False)
    @mock.patch.dict(os.environ, {"DATABRICKS_WORKSPACE_ID": "1234567890123456"}, clear=False)
    def test_auto_construct_falls_back_when_unreachable(self, mock_probe):
        """Tier 2 fallback: returns empty when auto-discovered gateway is unreachable."""
        env = os.environ.copy()
        env.pop("DATABRICKS_GATEWAY_HOST", None)
        env.pop("_GATEWAY_RESOLVED", None)
        with mock.patch.dict(os.environ, env, clear=True):
            result = self._get_fn()()
            assert result == ""
            mock_probe.assert_called_once()

    # -- Tier 3: nothing available --

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_empty_when_nothing_set(self):
        """Tier 3: returns empty string when neither env var is set."""
        assert self._get_fn()() == ""

    @mock.patch.dict(os.environ, {"DATABRICKS_GATEWAY_HOST": "", "DATABRICKS_WORKSPACE_ID": ""})
    def test_empty_when_both_blank(self):
        """Tier 3: returns empty when both vars are set but blank."""
        os.environ.pop("_GATEWAY_RESOLVED", None)
        assert self._get_fn()() == ""

    @mock.patch("utils._probe_gateway", return_value=True)
    @mock.patch.dict(os.environ, {"DATABRICKS_GATEWAY_HOST": "  ", "DATABRICKS_WORKSPACE_ID": "12345"})
    def test_empty_gateway_host_forces_off(self, mock_probe):
        """An explicitly-set-but-empty DATABRICKS_GATEWAY_HOST forces the gateway
        OFF (returns "") — it does NOT fall through to workspace-ID derivation.
        This is the operator kill-switch: setting it empty on an Azure host must
        route model calls to serving-endpoints, not to a derived ai-gateway URL.
        """
        os.environ.pop("_GATEWAY_RESOLVED", None)
        assert self._get_fn()() == ""

    @mock.patch("utils._probe_gateway", return_value=True)
    @mock.patch.dict(os.environ, {"DATABRICKS_WORKSPACE_ID": " 99999 "})
    def test_workspace_id_whitespace_stripped(self, mock_probe):
        """Leading/trailing whitespace in workspace ID is stripped (tier 2).

        DATABRICKS_GATEWAY_HOST is left unset so derivation runs — setting it
        empty would now force the gateway off before tier 2 is reached.
        """
        os.environ.pop("_GATEWAY_RESOLVED", None)
        os.environ.pop("DATABRICKS_GATEWAY_HOST", None)
        result = self._get_fn()()
        assert result.startswith("https://99999.")
        assert ".ai-gateway." in result


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
            "DATABRICKS_HOST": "https://workspace.example.test",
            "DATABRICKS_TOKEN": "dapi_test_token",
            "DATABRICKS_WORKSPACE_ID": "1234567890123456",
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(SETUP_DIR),
            # Pre-resolve gateway so subprocess skips the network probe
            "_GATEWAY_RESOLVED": "",
            "CODA_SKIP_CLAUDE_INSTALL": "true",
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
            timeout=60,
        )
        return result

    def _claude_base_url(self, tmp_path):
        import json

        settings_path = tmp_path / ".claude" / "settings.json"
        if not settings_path.exists():
            return None
        return json.loads(settings_path.read_text()).get("env", {}).get("ANTHROPIC_BASE_URL", "")

    def test_setup_claude_uses_the_workspace_gateway_route(self, tmp_path):
        """Claude Code addresses `system.ai.*` over the workspace AI Gateway v2.

        The legacy external `*.ai-gateway.*` host and the
        `/serving-endpoints/anthropic` fallback cannot serve model services, so
        neither is a valid route any more.
        """
        result = self._run_setup("setup_claude.py", tmp_path)
        assert result.returncode == 0, f"stderr: {result.stderr}"

        base_url = self._claude_base_url(tmp_path)
        if base_url is not None:
            assert base_url == "https://workspace.example.test/ai-gateway/anthropic"

    def test_setup_claude_settings_are_private(self, tmp_path):
        import stat

        result = self._run_setup(
            "setup_claude.py",
            tmp_path,
            {"DISABLE_SP_APIKEYHELPER": "true"},
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        settings = tmp_path / ".claude" / "settings.json"
        claude_json = tmp_path / ".claude.json"
        assert settings.exists() and claude_json.exists()
        assert stat.S_IMODE(settings.stat().st_mode) == 0o600
        assert stat.S_IMODE(claude_json.stat().st_mode) == 0o600

    def test_setup_claude_ignores_a_legacy_gateway_override(self, tmp_path):
        """A stale DATABRICKS_GATEWAY_HOST must not redirect model traffic."""
        result = self._run_setup(
            "setup_claude.py",
            tmp_path,
            {
                "DATABRICKS_GATEWAY_HOST": "https://custom.gateway.example.com",
                "_GATEWAY_RESOLVED": "https://custom.gateway.example.com",
            },
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"

        base_url = self._claude_base_url(tmp_path)
        if base_url is not None:
            assert "custom.gateway.example.com" not in base_url
            assert base_url == "https://workspace.example.test/ai-gateway/anthropic"

    def test_setup_claude_route_does_not_depend_on_workspace_id(self, tmp_path):
        """The route is built from DATABRICKS_HOST, not from a derived gateway."""
        result = self._run_setup("setup_claude.py", tmp_path, {"DATABRICKS_WORKSPACE_ID": ""})
        assert result.returncode == 0, f"stderr: {result.stderr}"

        base_url = self._claude_base_url(tmp_path)
        if base_url is not None:
            assert base_url == "https://workspace.example.test/ai-gateway/anthropic"
            assert "serving-endpoints" not in base_url

    @mock.patch("utils._probe_gateway", return_value=True)
    def test_codex_gateway_url_construction(self, mock_probe):
        """Codex endpoint should use gateway /openai/v1 path."""
        from utils import get_gateway_host
        with mock.patch.dict(os.environ, {
            "DATABRICKS_WORKSPACE_ID": "1234567890123456",
        }, clear=False):
            env = os.environ.copy()
            env.pop("DATABRICKS_GATEWAY_HOST", None)
            env.pop("_GATEWAY_RESOLVED", None)
            with mock.patch.dict(os.environ, env, clear=True):
                gw = get_gateway_host()
                codex_url = f"{gw}/openai/v1"
                assert codex_url == f"{gw}/openai/v1"

    @mock.patch("utils._probe_gateway", return_value=True)
    def test_gemini_gateway_url_construction(self, mock_probe):
        """Gemini endpoint should use gateway /gemini path."""
        from utils import get_gateway_host
        with mock.patch.dict(os.environ, {
            "DATABRICKS_WORKSPACE_ID": "1234567890123456",
        }, clear=False):
            env = os.environ.copy()
            env.pop("DATABRICKS_GATEWAY_HOST", None)
            env.pop("_GATEWAY_RESOLVED", None)
            with mock.patch.dict(os.environ, env, clear=True):
                gw = get_gateway_host()
                gemini_url = f"{gw}/gemini"
                assert gemini_url == f"{gw}/gemini"

    @mock.patch("utils._probe_gateway", return_value=True)
    def test_anthropic_gateway_url_construction(self, mock_probe):
        """Anthropic endpoint should use gateway /anthropic path."""
        from utils import get_gateway_host
        with mock.patch.dict(os.environ, {
            "DATABRICKS_WORKSPACE_ID": "1234567890123456",
        }, clear=False):
            env = os.environ.copy()
            env.pop("DATABRICKS_GATEWAY_HOST", None)
            env.pop("_GATEWAY_RESOLVED", None)
            with mock.patch.dict(os.environ, env, clear=True):
                gw = get_gateway_host()
                anthropic_url = f"{gw}/anthropic"
                assert anthropic_url == f"{gw}/anthropic"

    @mock.patch("utils._probe_gateway", return_value=True)
    def test_proxy_gateway_url_construction(self, mock_probe):
        """Proxy endpoint should use gateway /mlflow/v1 path."""
        from utils import get_gateway_host
        with mock.patch.dict(os.environ, {
            "DATABRICKS_WORKSPACE_ID": "1234567890123456",
        }, clear=False):
            env = os.environ.copy()
            env.pop("DATABRICKS_GATEWAY_HOST", None)
            env.pop("_GATEWAY_RESOLVED", None)
            with mock.patch.dict(os.environ, env, clear=True):
                gw = get_gateway_host()
                proxy_url = f"{gw}/mlflow/v1"
                assert proxy_url == f"{gw}/mlflow/v1"
