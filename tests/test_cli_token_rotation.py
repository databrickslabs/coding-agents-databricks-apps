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

    def test_preserves_command_apikey(self, isolated_home):
        """A `!command` apiKey must NOT be clobbered to a static literal.

        When pi is configured to resolve its token via a shell command per
        request (survives rotation), the rotator must leave it alone; otherwise
        the next rotation reverts pi to the fragile cache-at-launch behavior.
        """
        from cli_auth import update_cli_tokens
        pi_dir = isolated_home / ".pi" / "agent"
        pi_dir.mkdir(parents=True)
        cmd = "!awk -F'= ' '/^token /{print $2; exit}' \"$HOME/.databrickscfg\""
        config = {
            "providers": {"databricks-claude": {"apiKey": cmd, "authHeader": True}}
        }
        (pi_dir / "models.json").write_text(json.dumps(config))

        update_cli_tokens("new-token")

        result = json.loads((pi_dir / "models.json").read_text())
        assert result["providers"]["databricks-claude"]["apiKey"] == cmd

    def test_non_string_api_key_fails_closed(self, isolated_home):
        from cli_auth import update_cli_tokens

        pi_dir = isolated_home / ".pi" / "agent"
        pi_dir.mkdir(parents=True)
        path = pi_dir / "models.json"
        path.write_text(json.dumps({
            "providers": {"databricks-claude": {"apiKey": {"command": "/helper"}}}
        }))

        result = update_cli_tokens("new-token")

        assert result.failed == ("pi",)

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
    """opencode's auth.json is a map of provider-id -> credential, where the
    credential is a discriminated union on `type`. The API-key variant keeps the
    secret in `key`:

        export class Api extends Schema.Class<Api>("ApiAuth")({
            type: Schema.Literal("api"),
            key: Schema.String,
            ...

    (packages/opencode/src/auth/index.ts). `api_key` is not a field opencode
    recognises — these tests previously asserted that shape, which is how the
    mismatch survived: setup_opencode.py wrote `api_key` and the rotator
    faithfully rotated a field opencode never reads.
    """

    def test_rotates_key_for_api_credentials(self, isolated_home):
        from cli_auth import update_cli_tokens
        auth_dir = isolated_home / ".local" / "share" / "opencode"
        auth_dir.mkdir(parents=True)
        auth = {
            "databricks": {"type": "api", "key": "old"},
            "databricks-openai": {"type": "api", "key": "old"},
        }
        (auth_dir / "auth.json").write_text(json.dumps(auth))

        update_cli_tokens("new-token")

        result = json.loads((auth_dir / "auth.json").read_text())
        assert result["databricks"]["key"] == "new-token"
        assert result["databricks-openai"]["key"] == "new-token"
        # `type` is the union discriminant — rotation must preserve it.
        assert result["databricks"]["type"] == "api"

    def test_leaves_non_api_credentials_alone(self, isolated_home):
        """oauth / wellknown credentials have different fields. Writing a PAT
        into them would corrupt a credential opencode still needs."""
        from cli_auth import update_cli_tokens
        auth_dir = isolated_home / ".local" / "share" / "opencode"
        auth_dir.mkdir(parents=True)
        auth = {
            "databricks": {"type": "api", "key": "old"},
            "anthropic": {
                "type": "oauth",
                "refresh": "r-token",
                "access": "a-token",
                "expires": 123,
            },
            "corp": {"type": "wellknown", "key": "wk-key", "token": "wk-token"},
        }
        (auth_dir / "auth.json").write_text(json.dumps(auth))

        update_cli_tokens("new-token")

        result = json.loads((auth_dir / "auth.json").read_text())
        assert result["databricks"]["key"] == "new-token"
        assert result["anthropic"] == auth["anthropic"], "oauth credential mutated"
        assert result["corp"]["token"] == "wk-token"
        assert result["corp"]["key"] == "wk-key", "wellknown key mutated"

    def test_ignores_legacy_api_key_shape(self, isolated_home):
        """A file left over from the old (invalid) writer has no `type`, so it
        isn't a credential opencode can load. Don't pretend rotating it works."""
        from cli_auth import update_cli_tokens
        auth_dir = isolated_home / ".local" / "share" / "opencode"
        auth_dir.mkdir(parents=True)
        (auth_dir / "auth.json").write_text(json.dumps({"databricks": {"api_key": "old"}}))

        refresh = update_cli_tokens("new-token")

        result = json.loads((auth_dir / "auth.json").read_text())
        assert result["databricks"] == {"api_key": "old"}
        assert refresh.failed == ("opencode",)

    def test_preserves_external_provider_credentials(self, isolated_home):
        from cli_auth import update_cli_tokens

        auth_dir = isolated_home / ".local" / "share" / "opencode"
        auth_dir.mkdir(parents=True)
        (auth_dir / "auth.json").write_text(json.dumps({
            "databricks-anthropic": {"type": "api", "key": "old-db"},
            "external-openai": {"type": "api", "key": "external-secret"},
        }))
        config_dir = isolated_home / ".config" / "opencode"
        config_dir.mkdir(parents=True)
        external = {
            "options": {
                "baseURL": "https://external.example/v1",
                "apiKey": "external-api-key",
                "headers": {"Authorization": "Bearer external-auth"},
            }
        }
        (config_dir / "opencode.json").write_text(json.dumps({
            "provider": {
                "databricks-anthropic": {
                    "options": {
                        "baseURL": "https://workspace/anthropic",
                        "apiKey": "old-db",
                        "headers": {"Authorization": "Bearer old-db"},
                    }
                },
                "external-openai": external,
            }
        }))

        result = update_cli_tokens("new-db-token")

        auth = json.loads((auth_dir / "auth.json").read_text())
        assert auth["databricks-anthropic"]["key"] == "new-db-token"
        assert auth["external-openai"]["key"] == "external-secret"
        config = json.loads((config_dir / "opencode.json").read_text())
        assert config["provider"]["external-openai"] == external
        assert "external-openai" not in result.failed

    def test_anthropic_provider_requires_authorization_header(self, isolated_home):
        from cli_auth import update_cli_tokens

        config_dir = isolated_home / ".config" / "opencode"
        config_dir.mkdir(parents=True)
        path = config_dir / "opencode.json"
        path.write_text(json.dumps({
            "provider": {
                "databricks-anthropic": {
                    "options": {"apiKey": "old", "headers": {}}
                }
            }
        }))

        result = update_cli_tokens("new-token")

        assert result.failed == ("opencode_provider",)

    def test_malformed_managed_provider_options_fail_closed(self, isolated_home):
        from cli_auth import update_cli_tokens

        config_dir = isolated_home / ".config" / "opencode"
        config_dir.mkdir(parents=True)
        malformed = {
            "provider": {
                "databricks": {
                    "options": {"apiKey": 42, "headers": []}
                }
            }
        }
        path = config_dir / "opencode.json"
        path.write_text(json.dumps(malformed))

        result = update_cli_tokens("new-token")

        assert result.failed == ("opencode_provider",)
        assert json.loads(path.read_text()) == malformed

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
            "  base_url: http://127.0.0.1:4000\n"
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

    @pytest.mark.parametrize("proxy_url", [
        "http://localhost:4000",
        "http://127.0.0.1:4000  # local proxy",
    ])
    def test_recognizes_safe_loopback_proxy_variants(
        self, isolated_home, proxy_url
    ):
        from cli_auth import update_cli_tokens

        hermes_dir = isolated_home / ".hermes"
        hermes_dir.mkdir()
        path = hermes_dir / "config.yaml"
        path.write_text(
            f"model:\n  base_url: {proxy_url}\n  api_key: old-token\n"
        )

        result = update_cli_tokens("new-token")

        assert result.ok is True
        assert "api_key: new-token" in path.read_text()

    def test_preserves_external_provider_key(self, isolated_home):
        from cli_auth import update_cli_tokens

        hermes_dir = isolated_home / ".hermes"
        hermes_dir.mkdir()
        path = hermes_dir / "config.yaml"
        path.write_text(
            "model:\n"
            "  provider: custom\n"
            "  base_url: http://127.0.0.1:4000\n"
            "  api_key: old-databricks\n"
            "fallback_providers:\n"
            "- provider: openai\n"
            "  base_url: https://api.openai.com/v1\n"
            "  api_key: EXTERNAL-KEY-SENTINEL\n"
        )

        result = update_cli_tokens("new-databricks")

        content = path.read_text()
        assert result.ok is True
        assert "api_key: new-databricks" in content
        assert "api_key: EXTERNAL-KEY-SENTINEL" in content

    def test_skips_missing_file(self, isolated_home):
        from cli_auth import update_cli_tokens
        update_cli_tokens("new-token")  # must not raise


class TestAllCLIsUpdated:
    def test_all_configured_clis_updated_in_one_call(self, isolated_home):
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
        (oc_dir / "auth.json").write_text(
            json.dumps({"databricks": {"type": "api", "key": "old"}})
        )
        oc_config_dir = isolated_home / ".config" / "opencode"
        oc_config_dir.mkdir(parents=True)
        (oc_config_dir / "opencode.json").write_text(json.dumps({
            "provider": {
                "databricks": {
                    "options": {
                        "apiKey": "old",
                        "headers": {"Authorization": "Bearer old"},
                    }
                }
            }
        }))

        gemini_dir = isolated_home / ".gemini"
        gemini_dir.mkdir()
        (gemini_dir / ".env").write_text("GEMINI_API_KEY=old\n")

        hermes_dir = isolated_home / ".hermes"
        hermes_dir.mkdir()
        (hermes_dir / "config.yaml").write_text(
            "model:\n"
            "  base_url: http://127.0.0.1:4000\n"
            "  api_key: old\n"
            "fallback_providers:\n"
            "- provider: custom\n"
            "  base_url: http://127.0.0.1:4000\n"
            "  api_key: old\n"
        )

        # One call updates all
        update_cli_tokens("rotated-token")

        assert json.loads((claude_dir / "settings.json").read_text())["env"]["ANTHROPIC_AUTH_TOKEN"] == "rotated-token"
        assert json.loads((pi_dir / "models.json").read_text())["providers"]["databricks-claude"]["apiKey"] == "rotated-token"
        assert "OPENAI_API_KEY=rotated-token" in (codex_dir / ".env").read_text()
        assert json.loads((oc_dir / "auth.json").read_text())["databricks"]["key"] == "rotated-token"
        opencode_provider = json.loads(
            (oc_config_dir / "opencode.json").read_text()
        )["provider"]["databricks"]["options"]
        assert opencode_provider["apiKey"] == "rotated-token"
        assert opencode_provider["headers"]["Authorization"] == "Bearer rotated-token"
        assert "GEMINI_API_KEY=rotated-token" in (gemini_dir / ".env").read_text()
        hermes_content = (hermes_dir / "config.yaml").read_text()
        assert hermes_content.count("api_key: rotated-token") == 2


class TestAtomicWrites:
    """The rotator rewrites live agent configs every 10 minutes while agents
    may be reading them, so every write goes through `_atomic_write_text`."""

    def test_no_partial_file_and_no_tmp_left_behind(self, isolated_home):
        from cli_auth import _atomic_write_text

        path = isolated_home / "config.yaml"
        path.write_text("api_key: old\n")

        _atomic_write_text(str(path), "api_key: new\n")

        assert path.read_text() == "api_key: new\n"
        assert not (isolated_home / "config.yaml.tmp").exists()

    def test_preserves_restrictive_mode(self, isolated_home):
        """os.replace() installs the tmp file's inode — and therefore the tmp
        file's permissions. Without an explicit chmod, rotating the Hermes
        token would widen ~/.hermes/config.yaml from 0600 back to the umask
        default, silently undoing setup_hermes.py's hardening."""
        import stat

        path = isolated_home / "config.yaml"
        path.write_text("api_key: old\n")
        path.chmod(0o600)

        from cli_auth import _atomic_write_text

        _atomic_write_text(str(path), "api_key: new\n")

        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_hermes_rotation_keeps_config_private(self, isolated_home):
        """End-to-end via the public entry point: a 0600 Hermes config stays
        0600 across a token rotation."""
        import stat
        from cli_auth import update_cli_tokens

        hermes_dir = isolated_home / ".hermes"
        hermes_dir.mkdir()
        cfg = hermes_dir / "config.yaml"
        cfg.write_text(
            "model:\n"
            "  base_url: http://127.0.0.1:4000\n"
            "  api_key: old\n"
        )
        cfg.chmod(0o600)

        update_cli_tokens("rotated-token")

        assert "api_key: rotated-token" in cfg.read_text()
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


class TestMissingConfigsAreQuiet:
    def test_no_warnings_when_nothing_is_installed(self, isolated_home, caplog):
        """A rotation on a box where an agent never ran must not log warnings —
        the existence guards return early instead of raising OSError."""
        import logging

        from cli_auth import update_cli_tokens

        with caplog.at_level(logging.WARNING, logger="cli_auth"):
            update_cli_tokens("some-token")

        assert [r.message for r in caplog.records if r.levelno >= logging.WARNING] == []


class TestRefreshOrchestration:
    def test_returns_per_cli_success_report(self, monkeypatch):
        import cli_auth

        for name in (
            "_update_claude", "_update_pi", "_update_codex", "_update_opencode",
            "_update_opencode_provider_headers", "_update_gemini", "_update_hermes",
        ):
            monkeypatch.setattr(cli_auth, name, lambda _token: True)

        result = cli_auth.update_cli_tokens("rotated-token")

        assert result.ok is True
        assert result.failed == ()
        assert "rotated-token" not in repr(result)
        assert result.updated == (
            "claude", "pi", "codex", "opencode", "opencode_provider",
            "gemini", "hermes",
        )

    def test_partial_failure_is_bounded_observable_and_continues(
        self, monkeypatch, caplog
    ):
        import logging
        import cli_auth

        calls = []

        def succeed(name):
            return lambda _token: calls.append(name) or True

        for name, function_name in (
            ("claude", "_update_claude"),
            ("pi", "_update_pi"),
            ("codex", "_update_codex"),
            ("opencode_provider", "_update_opencode_provider_headers"),
            ("gemini", "_update_gemini"),
            ("hermes", "_update_hermes"),
        ):
            monkeypatch.setattr(cli_auth, function_name, succeed(name))

        token = "dapi-DO-NOT-LOG"
        monkeypatch.setattr(
            cli_auth,
            "_update_opencode",
            lambda _token: (_ for _ in ()).throw(RuntimeError(f"bad {token}")),
        )

        with caplog.at_level(logging.WARNING, logger="cli_auth"):
            result = cli_auth.update_cli_tokens(token)

        assert result.ok is False
        assert result.failed == ("opencode",)
        assert "opencode_provider" in calls
        assert "hermes" in calls
        assert "opencode" in " ".join(caplog.messages)
        assert token not in " ".join(caplog.messages)

    def test_concurrent_refreshes_are_serialized(self, monkeypatch):
        import threading
        import time
        import cli_auth

        entered = threading.Event()
        release = threading.Event()
        state_lock = threading.Lock()
        active = 0
        max_active = 0

        def blocking(_token):
            nonlocal active, max_active
            with state_lock:
                active += 1
                max_active = max(max_active, active)
            entered.set()
            assert release.wait(2)
            with state_lock:
                active -= 1
            return False

        monkeypatch.setattr(cli_auth, "_update_claude", blocking)
        for name in (
            "_update_pi", "_update_codex", "_update_opencode",
            "_update_opencode_provider_headers", "_update_gemini", "_update_hermes",
        ):
            monkeypatch.setattr(cli_auth, name, lambda _token: False)

        results = []
        first = threading.Thread(
            target=lambda: results.append(cli_auth.update_cli_tokens("same-token"))
        )
        second = threading.Thread(
            target=lambda: results.append(cli_auth.update_cli_tokens("same-token"))
        )
        first.start()
        assert entered.wait(1)
        second.start()
        time.sleep(0.05)
        release.set()
        first.join(2)
        second.join(2)

        assert not first.is_alive() and not second.is_alive()
        assert max_active == 1
        assert len(results) == 2
        assert all(result.ok for result in results)

    def test_refresh_lock_timeout_is_reported(self):
        import cli_auth

        assert cli_auth._CLI_REFRESH_LOCK.acquire(timeout=1)
        try:
            result = cli_auth.update_cli_tokens("token", lock_timeout=0)
        finally:
            cli_auth._CLI_REFRESH_LOCK.release()

        assert result.ok is False
        assert result.failed == ("refresh_lock",)

    def test_idempotent_refresh_does_not_rewrite_unchanged_file(
        self, isolated_home
    ):
        import cli_auth

        claude_dir = isolated_home / ".claude"
        claude_dir.mkdir()
        path = claude_dir / "settings.json"
        path.write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "same-token"}}))
        cli_auth.update_cli_tokens("same-token")

        with mock.patch.object(
            cli_auth, "_atomic_write_text", wraps=cli_auth._atomic_write_text
        ) as write:
            result = cli_auth.update_cli_tokens("same-token")

        assert result.ok is True
        write.assert_not_called()

    def test_atomic_failure_preserves_valid_file_and_cleans_temp(
        self, isolated_home, monkeypatch
    ):
        import cli_auth

        claude_dir = isolated_home / ".claude"
        claude_dir.mkdir()
        path = claude_dir / "settings.json"
        original = json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "old-token"}})
        path.write_text(original)
        monkeypatch.setattr(
            cli_auth.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace failed"))
        )

        result = cli_auth.update_cli_tokens("new-token")

        assert result.ok is False
        assert result.failed == ("claude",)
        assert path.read_text() == original
        assert list(claude_dir.glob(".settings.json.*")) == []

    @pytest.mark.parametrize("target", ["hermes", "codex", "gemini"])
    def test_present_but_unrecognized_config_is_failure(
        self, isolated_home, target
    ):
        import cli_auth

        if target == "hermes":
            path = isolated_home / ".hermes" / "config.yaml"
            path.parent.mkdir()
            path.write_text(
                "model:\n"
                "  base_url: http://127.0.0.1:4000\n"
                "    api_key: old-token\n"
            )
        else:
            directory = ".codex" if target == "codex" else ".gemini"
            path = isolated_home / directory / ".env"
            path.parent.mkdir()
            path.write_text("OTHER=value\n")

        result = cli_auth.update_cli_tokens("new-token")

        assert target in result.failed
        assert result.ok is False

    def test_refresh_tightens_loose_file_mode(self, isolated_home):
        import stat
        import cli_auth

        path = isolated_home / ".claude" / "settings.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "old"}}))
        path.chmod(0o644)

        result = cli_auth.update_cli_tokens("new")

        assert result.ok is True
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_backslashes_are_literal_in_dotenv_and_yaml_tokens(self, isolated_home):
        import cli_auth

        token = r"dapi-a\1b\g<1>"
        codex = isolated_home / ".codex" / ".env"
        codex.parent.mkdir()
        codex.write_text("OPENAI_API_KEY=old\n")
        hermes = isolated_home / ".hermes" / "config.yaml"
        hermes.parent.mkdir()
        hermes.write_text(
            "model:\n"
            "  base_url: http://127.0.0.1:4000\n"
            "  api_key: old\n"
        )

        result = cli_auth.update_cli_tokens(token)

        assert result.ok is True
        assert f"OPENAI_API_KEY={token}" in codex.read_text()
        assert f"api_key: {token}" in hermes.read_text()

    def test_atomic_write_fsyncs_file_and_parent_directory(self, isolated_home):
        import cli_auth

        path = isolated_home / "config.json"
        path.write_text("old")
        with mock.patch.object(cli_auth.os, "fsync") as fsync:
            cli_auth._atomic_write_text(str(path), "new")

        assert fsync.call_count == 2
        assert path.read_text() == "new"

    def test_no_change_refresh_still_tightens_mode(self, isolated_home):
        import stat
        import cli_auth

        path = isolated_home / ".claude" / "settings.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"apiKeyHelper": "/helper", "env": {}}))
        path.chmod(0o644)

        result = cli_auth.update_cli_tokens("unused-token")

        assert result.ok is True
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


class TestPostSetupRefreshLogging:
    def test_bootstrap_writes_claude_settings_mode_600(
        self, tmp_path, monkeypatch
    ):
        import stat
        import app
        import utils

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("DATABRICKS_HOST", "https://workspace.example")
        monkeypatch.setattr(utils, "resolve_and_cache_gateway", lambda: None)
        monkeypatch.setattr(app, "get_gateway_host", lambda: "https://gateway.example")
        monkeypatch.setattr(app, "apply_claude_otel_env", lambda *_args: False)
        monkeypatch.setattr(app.pat_rotator, "_write_databrickscfg", lambda _token: True)
        monkeypatch.setattr(app, "_venv_python", lambda: "/usr/bin/python3")
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(app.subprocess, "run", return_value=completed):
            app._configure_all_cli_auth("dapi-bootstrap-test")

        settings = tmp_path / ".claude" / "settings.json"
        assert stat.S_IMODE(settings.stat().st_mode) == 0o600

    def test_exception_message_cannot_leak_token(self, monkeypatch, caplog):
        import logging
        import app
        import cli_auth

        token = "dapi-POST-SETUP-DO-NOT-LOG"
        monkeypatch.setattr(
            cli_auth,
            "update_cli_tokens",
            lambda _token: (_ for _ in ()).throw(RuntimeError(token)),
        )

        with caplog.at_level(logging.WARNING, logger="app"):
            assert app._refresh_cli_auth_after_setup(token) is False

        assert token not in " ".join(caplog.messages)
        assert "RuntimeError" in " ".join(caplog.messages)
