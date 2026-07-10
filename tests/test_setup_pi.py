"""Tests for setup_pi.py — verify the Pi models.json config is written correctly.

Runs the real setup_pi.py as a subprocess against a fake HOME. A fake `pi`
binary is pre-seeded so the npm install is skipped (setup_pi.py guards it behind
`if not pi_bin.exists()`), and no gateway host is configured so discovery fails
closed to an empty set — pick_in_geo_model then returns PI_MODEL unchanged, so
the write path is deterministic without network access.
"""

import json
import os
import stat
import subprocess
import sys
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
        # No DATABRICKS_GATEWAY_HOST / workspace id -> get_gateway_host() returns
        # "" -> base_url falls to /serving-endpoints/anthropic (deterministic).
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
        result = run_setup_pi(tmp_path, {"PI_MODEL": "databricks-claude-opus-4-8"})
        assert result.returncode == 0, result.stderr

        config = read_models(tmp_path)
        assert config["model"] == "databricks-claude/databricks-claude-opus-4-8"
        provider = config["providers"]["databricks-claude"]
        assert provider["api"] == "anthropic-messages"
        assert provider["authHeader"] is True
        assert provider["apiKey"] == "dapi_test_token"
        assert provider["baseUrl"].endswith("/serving-endpoints/anthropic")
        assert provider["compat"] == {"supportsEagerToolInputStreaming": False}
        assert provider["models"] == [{"id": "databricks-claude-opus-4-8"}]

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

    def test_no_token_installs_but_skips_config(self, tmp_path):
        _seed_fake_pi_binary(tmp_path)
        result = run_setup_pi(tmp_path, {"DATABRICKS_TOKEN": ""})
        assert result.returncode == 0
        # Config write is gated on a token; without one, no models.json.
        assert not (tmp_path / ".pi" / "agent" / "models.json").exists()
