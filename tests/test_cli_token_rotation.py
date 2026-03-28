"""Tests for CLI token rotation — verify all config files get updated."""

import json
import os

import pytest
from unittest import mock


@pytest.fixture(autouse=True)
def isolated_home(tmp_path):
    """Point cli_auth._HOME at a temp dir."""
    with mock.patch("cli_auth._HOME", str(tmp_path)):
        yield tmp_path


class TestUpdateClaude:
    def test_updates_anthropic_auth_token(self, isolated_home):
        from cli_auth import update_cli_tokens
        claude_dir = isolated_home / ".claude"
        claude_dir.mkdir()
        settings = {"env": {"ANTHROPIC_AUTH_TOKEN": "old-token", "OTHER": "keep"}}
        (claude_dir / "settings.json").write_text(json.dumps(settings))

        update_cli_tokens("new-token")

        result = json.loads((claude_dir / "settings.json").read_text())
        assert result["env"]["ANTHROPIC_AUTH_TOKEN"] == "new-token"
        assert result["env"]["OTHER"] == "keep"

    def test_skips_missing_file(self, isolated_home):
        from cli_auth import update_cli_tokens
        update_cli_tokens("new-token")  # should not raise


class TestUpdateCodex:
    def test_updates_openai_api_key(self, isolated_home):
        from cli_auth import update_cli_tokens
        codex_dir = isolated_home / ".codex"
        codex_dir.mkdir()
        (codex_dir / ".env").write_text("# comment\nOPENAI_API_KEY=old-token\nOTHER=keep\n")

        update_cli_tokens("new-token")

        content = (codex_dir / ".env").read_text()
        assert "OPENAI_API_KEY=new-token" in content
        assert "OTHER=keep" in content

    def test_skips_missing_file(self, isolated_home):
        from cli_auth import update_cli_tokens
        update_cli_tokens("new-token")


class TestUpdateOpenCode:
    def test_updates_api_key_in_auth_json(self, isolated_home):
        from cli_auth import update_cli_tokens
        auth_dir = isolated_home / ".local" / "share" / "opencode"
        auth_dir.mkdir(parents=True)
        auth = {"databricks": {"api_key": "old"}, "databricks-openai": {"api_key": "old"}}
        (auth_dir / "auth.json").write_text(json.dumps(auth))

        update_cli_tokens("new-token")

        result = json.loads((auth_dir / "auth.json").read_text())
        assert result["databricks"]["api_key"] == "new-token"
        assert result["databricks-openai"]["api_key"] == "new-token"

    def test_skips_missing_file(self, isolated_home):
        from cli_auth import update_cli_tokens
        update_cli_tokens("new-token")


class TestUpdateGemini:
    def test_updates_gemini_api_key(self, isolated_home):
        from cli_auth import update_cli_tokens
        gemini_dir = isolated_home / ".gemini"
        gemini_dir.mkdir()
        (gemini_dir / ".env").write_text('GEMINI_MODEL=test\nGEMINI_API_KEY=old-token\n')

        update_cli_tokens("new-token")

        content = (gemini_dir / ".env").read_text()
        assert "GEMINI_API_KEY=new-token" in content
        assert "GEMINI_MODEL=test" in content

    def test_skips_missing_file(self, isolated_home):
        from cli_auth import update_cli_tokens
        update_cli_tokens("new-token")


class TestAllCLIsUpdated:
    def test_all_four_updated_in_one_call(self, isolated_home):
        from cli_auth import update_cli_tokens

        # Set up all config files
        claude_dir = isolated_home / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(
            json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "old"}})
        )

        codex_dir = isolated_home / ".codex"
        codex_dir.mkdir()
        (codex_dir / ".env").write_text("OPENAI_API_KEY=old\n")

        oc_dir = isolated_home / ".local" / "share" / "opencode"
        oc_dir.mkdir(parents=True)
        (oc_dir / "auth.json").write_text(json.dumps({"databricks": {"api_key": "old"}}))

        gemini_dir = isolated_home / ".gemini"
        gemini_dir.mkdir()
        (gemini_dir / ".env").write_text("GEMINI_API_KEY=old\n")

        # One call updates all
        update_cli_tokens("rotated-token")

        assert json.loads((claude_dir / "settings.json").read_text())["env"]["ANTHROPIC_AUTH_TOKEN"] == "rotated-token"
        assert "OPENAI_API_KEY=rotated-token" in (codex_dir / ".env").read_text()
        assert json.loads((oc_dir / "auth.json").read_text())["databricks"]["api_key"] == "rotated-token"
        assert "GEMINI_API_KEY=rotated-token" in (gemini_dir / ".env").read_text()
