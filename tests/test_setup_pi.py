"""Tests for setup_pi.py — verify the Pi models.json config is written correctly.

Runs the real setup_pi.py as a subprocess against a fake HOME. A fake `pi`
binary is pre-seeded so the npm install is skipped. Model-services discovery
fails closed against the fake workspace, so only the requested system.ai model
is configured and the write path remains deterministic without inference.
"""

import json
import os
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SETUP_PI = Path(__file__).parent.parent / "setup_pi.py"


def _seed_fake_pi_binary(home: Path):
    """Create a fake ~/.local/bin/pi so setup_pi.py skips the npm install."""
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)
    pi_bin = local_bin / "pi"
    pi_bin.write_text("#!/bin/sh\nexit 0\n")
    pi_bin.chmod(pi_bin.stat().st_mode | stat.S_IEXEC)


def run_setup_pi(tmp_path, env_overrides=None):
    env = {
        "HOME": str(tmp_path),
        "DATABRICKS_HOST": "https://test.cloud.databricks.com",
        "DATABRICKS_TOKEN": "dapi_test_token",
        "PATH": os.environ.get("PATH", ""),
        "_GATEWAY_RESOLVED": "",
    }
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, str(SETUP_PI)],
        env=env, capture_output=True, text=True, timeout=30,
        cwd=str(SETUP_PI.parent),
    )


def read_models(tmp_path):
    return json.loads((tmp_path / ".pi" / "agent" / "models.json").read_text())


class TestSetupPiConfig:
    def test_writes_databricks_claude_provider_schema(self, tmp_path):
        _seed_fake_pi_binary(tmp_path)
        result = run_setup_pi(tmp_path, {"PI_MODEL": "system.ai.claude-opus-5"})
        assert result.returncode == 0, result.stderr

        config = read_models(tmp_path)
        assert config["model"] == "databricks-claude/system.ai.claude-opus-5"
        provider = config["providers"]["databricks-claude"]
        assert provider["api"] == "anthropic-messages"
        assert provider["authHeader"] is True
        # apiKey is a per-request `!command` (the shared token helper), NOT a
        # static literal -- that's what lets a long-running pi survive PAT
        # rotation / SP-OAuth expiry without a restart.
        assert provider["apiKey"].startswith("!")
        assert provider["apiKey"].endswith("anthropic-token-helper.py")
        assert provider["baseUrl"] == "https://test.cloud.databricks.com/ai-gateway/anthropic"
        assert ".ai-gateway." not in provider["baseUrl"]
        assert provider["compat"] == {"supportsEagerToolInputStreaming": False}
        assert [m["id"] for m in provider["models"]] == ["system.ai.claude-opus-5"]
        # Limits and thinking come from the shared Claude version policy: opus 5
        # is a >= 4.6 tier, so 1M/128k with adaptive thinking. Without
        # forceAdaptiveThinking Pi sends `thinking: {type: "enabled"}` and the
        # endpoint answers 400 "thinking.type.enabled is not supported".
        assert provider["models"][0]["reasoning"] is True
        assert provider["models"][0]["compat"] == {"forceAdaptiveThinking": True}
        assert provider["models"][0]["contextWindow"] == 1_000_000
        assert provider["models"][0]["maxTokens"] == 128_000

    def test_models_json_is_chmod_600(self, tmp_path):
        _seed_fake_pi_binary(tmp_path)
        result = run_setup_pi(tmp_path)
        assert result.returncode == 0, result.stderr
        models_path = tmp_path / ".pi" / "agent" / "models.json"
        mode = stat.S_IMODE(models_path.stat().st_mode)
        assert mode == 0o600

    def test_read_merge_write_preserves_foreign_keys(self, tmp_path):
        _seed_fake_pi_binary(tmp_path)
        pi_dir = tmp_path / ".pi" / "agent"
        pi_dir.mkdir(parents=True)
        (pi_dir / "models.json").write_text(json.dumps({
            "someUserSetting": "keep-me",
            "providers": {"custom-provider": {"apiKey": "user-owned"}},
        }))

        result = run_setup_pi(tmp_path)
        assert result.returncode == 0, result.stderr

        config = read_models(tmp_path)
        # Our provider is written...
        assert "databricks-claude" in config["providers"]
        # ...without clobbering keys we don't own.
        assert config["someUserSetting"] == "keep-me"
        assert config["providers"]["custom-provider"]["apiKey"] == "user-owned"

    def test_enable_pi_false_skips_setup(self, tmp_path):
        _seed_fake_pi_binary(tmp_path)
        result = run_setup_pi(tmp_path, {"ENABLE_PI": "false"})
        assert result.returncode == 0
        assert not (tmp_path / ".pi" / "agent" / "models.json").exists()

    def test_no_token_and_no_broker_installs_but_skips_config(self, tmp_path):
        _seed_fake_pi_binary(tmp_path)
        result = run_setup_pi(tmp_path, {"DATABRICKS_TOKEN": "", "CODA_SP_TOKEN_BROKER_URL": ""})
        assert result.returncode == 0
        # No auth source at all still skips config; this is distinct from the
        # SP-broker case below.
        assert not (tmp_path / ".pi" / "agent" / "models.json").exists()

    def test_sp_broker_token_allows_config_without_pat(self, tmp_path):
        """SP baseline: Pi setup must not gate models.json on raw
        DATABRICKS_TOKEN. The broker is the auth source when no PAT exists."""
        _seed_fake_pi_binary(tmp_path)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = b"sp-token-for-setup"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = run_setup_pi(tmp_path, {
                "DATABRICKS_TOKEN": "",
                "CODA_SP_TOKEN_BROKER_URL": f"http://127.0.0.1:{server.server_port}/token",
            })
        finally:
            server.shutdown()
            thread.join(timeout=2)

        assert result.returncode == 0, result.stderr
        config = read_models(tmp_path)
        assert config["providers"]["databricks-claude"]["apiKey"].startswith("!")
