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

    def test_updates_claude_otel_header_tokens(self, isolated_home):
        from cli_auth import update_cli_tokens
        claude_dir = isolated_home / ".claude"
        claude_dir.mkdir()
        settings = {
            "env": {
                "ANTHROPIC_AUTH_TOKEN": "old-token",
                "OTEL_EXPORTER_OTLP_TRACES_HEADERS": (
                    "content-type=application/x-protobuf,"
                    "Authorization=Bearer old-token,"
                    "X-Databricks-UC-Table-Name=cat.sch.claude_otel_spans"
                ),
                "OTEL_EXPORTER_OTLP_LOGS_HEADERS": (
                    "content-type=application/x-protobuf,"
                    "Authorization=Bearer old-token,"
                    "X-Databricks-UC-Table-Name=cat.sch.claude_otel_logs"
                ),
                "OTEL_EXPORTER_OTLP_METRICS_HEADERS": (
                    "content-type=application/x-protobuf,"
                    "Authorization=Bearer old-token,"
                    "X-Databricks-UC-Table-Name=cat.sch.claude_otel_metrics"
                ),
            }
        }
        (claude_dir / "settings.json").write_text(json.dumps(settings))

        update_cli_tokens("new-token")

        result = json.loads((claude_dir / "settings.json").read_text())
        assert result["env"]["ANTHROPIC_AUTH_TOKEN"] == "new-token"
        for key in [
            "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
            "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
            "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
        ]:
            assert "Authorization=Bearer new-token" in result["env"][key]
            assert "old-token" not in result["env"][key]
        assert "cat.sch.claude_otel_spans" in result["env"]["OTEL_EXPORTER_OTLP_TRACES_HEADERS"]
        assert "cat.sch.claude_otel_logs" in result["env"]["OTEL_EXPORTER_OTLP_LOGS_HEADERS"]
        assert "cat.sch.claude_otel_metrics" in result["env"]["OTEL_EXPORTER_OTLP_METRICS_HEADERS"]

    def test_skips_missing_file(self, isolated_home):
        from cli_auth import update_cli_tokens
        update_cli_tokens("new-token")  # should not raise


class TestUpdatePi:
    def test_updates_apikey_in_models_json(self, isolated_home):
        from cli_auth import update_cli_tokens
        pi_dir = isolated_home / ".pi" / "agent"
        pi_dir.mkdir(parents=True)
        config = {
            "model": "databricks-claude/databricks-claude-opus-4-8",
            "providers": {
                "databricks-claude": {
                    "baseUrl": "https://gw/anthropic",
                    "api": "anthropic-messages",
                    "apiKey": "old-token",
                    "authHeader": True,
                    "models": [{"id": "databricks-claude-opus-4-8"}],
                }
            },
        }
        (pi_dir / "models.json").write_text(json.dumps(config))

        update_cli_tokens("new-token")

        result = json.loads((pi_dir / "models.json").read_text())
        # Only the token changed; the rest of the config is intact.
        assert result["providers"]["databricks-claude"]["apiKey"] == "new-token"
        assert result["providers"]["databricks-claude"]["baseUrl"] == "https://gw/anthropic"
        assert result["providers"]["databricks-claude"]["api"] == "anthropic-messages"
        assert result["model"] == "databricks-claude/databricks-claude-opus-4-8"

    def test_skips_missing_file(self, isolated_home):
        from cli_auth import update_cli_tokens
        update_cli_tokens("new-token")


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


class TestUpdateHermes:
    def test_updates_both_api_key_lines(self, isolated_home):
        """Hermes config has two api_key lines (primary + fallback). Both must rotate."""
        from cli_auth import update_cli_tokens
        hermes_dir = isolated_home / ".hermes"
        hermes_dir.mkdir()
        config = (
            "model:\n"
            "  default: databricks-claude-opus-4-7\n"
            "  provider: custom\n"
            "  base_url: http://127.0.0.1:4000\n"
            "  api_key: old-token\n"
            "\n"
            "fallback_providers:\n"
            "- provider: custom\n"
            "  model: databricks-claude-opus-4-6\n"
            "  base_url: http://127.0.0.1:4000\n"
            "  api_key: old-token\n"
        )
        (hermes_dir / "config.yaml").write_text(config)

        update_cli_tokens("new-token")

        content = (hermes_dir / "config.yaml").read_text()
        assert content.count("api_key: new-token") == 2, (
            "Both primary and fallback api_key lines must be rotated. "
            f"Content was:\n{content}"
        )
        assert "old-token" not in content
        # Unrelated lines preserved
        assert "default: databricks-claude-opus-4-7" in content
        assert "model: databricks-claude-opus-4-6" in content

    def test_preserves_other_indentation(self, isolated_home):
        """Regex must match only `  api_key:` with two-space indent, not arbitrary text."""
        from cli_auth import update_cli_tokens
        hermes_dir = isolated_home / ".hermes"
        hermes_dir.mkdir()
        # Decoy: a comment that mentions api_key, plus a 4-space-indented api_key
        # that should NOT be touched.
        config = (
            "# api_key: this-is-a-comment-not-a-value\n"
            "model:\n"
            "  api_key: old-token\n"
            "deep:\n"
            "    api_key: should-not-change\n"
        )
        (hermes_dir / "config.yaml").write_text(config)

        update_cli_tokens("new-token")

        content = (hermes_dir / "config.yaml").read_text()
        assert "  api_key: new-token" in content
        assert "# api_key: this-is-a-comment-not-a-value" in content
        assert "    api_key: should-not-change" in content

    def test_skips_missing_file(self, isolated_home):
        from cli_auth import update_cli_tokens
        update_cli_tokens("new-token")  # must not raise


class TestAllCLIsUpdated:
    def test_all_five_updated_in_one_call(self, isolated_home):
        from cli_auth import update_cli_tokens

        # Set up all config files
        claude_dir = isolated_home / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(
            json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "old"}})
        )

        pi_dir = isolated_home / ".pi" / "agent"
        pi_dir.mkdir(parents=True)
        (pi_dir / "models.json").write_text(
            json.dumps({"providers": {"databricks-claude": {"apiKey": "old"}}})
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

        hermes_dir = isolated_home / ".hermes"
        hermes_dir.mkdir()
        (hermes_dir / "config.yaml").write_text(
            "model:\n  api_key: old\nfallback_providers:\n- provider: custom\n  api_key: old\n"
        )

        # One call updates all
        update_cli_tokens("rotated-token")

        assert json.loads((claude_dir / "settings.json").read_text())["env"]["ANTHROPIC_AUTH_TOKEN"] == "rotated-token"
        assert json.loads((pi_dir / "models.json").read_text())["providers"]["databricks-claude"]["apiKey"] == "rotated-token"
        assert "OPENAI_API_KEY=rotated-token" in (codex_dir / ".env").read_text()
        assert json.loads((oc_dir / "auth.json").read_text())["databricks"]["api_key"] == "rotated-token"
        assert "GEMINI_API_KEY=rotated-token" in (gemini_dir / ".env").read_text()
        hermes_content = (hermes_dir / "config.yaml").read_text()
        assert hermes_content.count("api_key: rotated-token") == 2
