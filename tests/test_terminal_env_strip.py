"""Tests for _build_terminal_shell_env in app.py — F-01 security fix.

The deployer-level credentials set in app.yaml (NPM_TOKEN, UV_INDEX
passwords, derived npm auth tokens) MUST NOT be readable from a user's
terminal session. This module verifies the strip logic in isolation,
since the full create_session path is hard to unit-test (PTY + Popen).
"""

from __future__ import annotations

import pytest


def _build_terminal_shell_env():
    from app import _build_terminal_shell_env
    return _build_terminal_shell_env


class TestTerminalEnvStrip:
    """The user terminal must not inherit deployer-level credentials."""

    def test_strips_databricks_token(self):
        build = _build_terminal_shell_env()
        env = build({"DATABRICKS_TOKEN": "dapi-xxx", "HOME": "/app"})
        assert "DATABRICKS_TOKEN" not in env

    def test_strips_databricks_host(self):
        build = _build_terminal_shell_env()
        env = build({"DATABRICKS_HOST": "https://workspace", "HOME": "/app"})
        assert "DATABRICKS_HOST" not in env

    def test_strips_gemini_api_key(self):
        build = _build_terminal_shell_env()
        env = build({"GEMINI_API_KEY": "key-xxx", "HOME": "/app"})
        assert "GEMINI_API_KEY" not in env

    def test_strips_npm_token(self):
        """NPM_TOKEN is a deployer-level JFrog credential — must not leak."""
        build = _build_terminal_shell_env()
        env = build({"NPM_TOKEN": "tok-abc", "HOME": "/app"})
        assert "NPM_TOKEN" not in env

    def test_strips_derived_npm_auth_token(self):
        """The npm_config_//host/:_authToken key derived from NPM_TOKEN must not leak."""
        build = _build_terminal_shell_env()
        env = build({
            "npm_config_//jfrog.example.com/:_authToken": "tok-abc",
            "HOME": "/app",
        })
        assert "npm_config_//jfrog.example.com/:_authToken" not in env

    def test_strips_uv_index_password(self):
        """UV_INDEX_*_PASSWORD is a deployer-level credential — must not leak."""
        build = _build_terminal_shell_env()
        env = build({
            "UV_INDEX_INTERNAL_PASSWORD": "s3cr3t",
            "UV_INDEX_INTERNAL_USERNAME": "svc-coda",
            "HOME": "/app",
        })
        assert "UV_INDEX_INTERNAL_PASSWORD" not in env
        assert "UV_INDEX_INTERNAL_USERNAME" not in env

    def test_strips_uv_default_index(self):
        """The PyPI index URL is enterprise-config and shouldn't be in user env."""
        build = _build_terminal_shell_env()
        env = build({"UV_DEFAULT_INDEX": "https://internal/pypi/", "HOME": "/app"})
        assert "UV_DEFAULT_INDEX" not in env

    def test_strips_claude_code_state(self):
        """CLAUDECODE / CLAUDE_CODE_SESSION would make the terminal think it's nested."""
        build = _build_terminal_shell_env()
        env = build({
            "CLAUDECODE": "1",
            "CLAUDE_CODE_SESSION": "abc",
            "HOME": "/app",
        })
        assert "CLAUDECODE" not in env
        assert "CLAUDE_CODE_SESSION" not in env

    def test_sets_term(self):
        build = _build_terminal_shell_env()
        env = build({"HOME": "/app"})
        assert env["TERM"] == "xterm-256color"

    def test_preserves_unrelated_env(self):
        """Other env vars (PATH, USER, custom workspace vars) pass through."""
        build = _build_terminal_shell_env()
        env = build({
            "HOME": "/app",
            "PATH": "/usr/bin",
            "USER": "app",
            "MY_CUSTOM_VAR": "hello",
        })
        assert env["PATH"] == "/usr/bin"
        assert env["USER"] == "app"
        assert env["MY_CUSTOM_VAR"] == "hello"

    def test_does_not_mutate_input(self):
        """Caller's env dict (typically os.environ) must not be modified."""
        build = _build_terminal_shell_env()
        base = {"DATABRICKS_TOKEN": "dapi-xxx", "HOME": "/app"}
        build(base)
        assert "DATABRICKS_TOKEN" in base  # original unchanged

    @pytest.mark.parametrize("key", [
        "UV_INDEX_FOO_PASSWORD",
        "UV_INDEX_BAR_USERNAME",
        "UV_INDEX_LONG_NAME_WITH_UNDERSCORES_PASSWORD",
        "npm_config_//jfrog-x.example.com/:_authToken",
        "npm_config_//host:8080/:_authToken",
    ])
    def test_pattern_match_strips_all_credential_shapes(self, key):
        """Each operator-named credential variant matches the strip pattern."""
        build = _build_terminal_shell_env()
        env = build({key: "secret", "HOME": "/app"})
        assert key not in env
