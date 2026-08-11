import datetime
import json
import os
import subprocess
from urllib.request import urlopen

import sp_token_broker as broker
from token_helper import write_databricks_token_wrapper


def test_broker_binds_loopback_and_mints_per_request():
    tokens = iter(("first-token", "second-token"))
    server = broker.start_sp_token_broker(lambda: next(tokens), port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
        url = broker.broker_url(server)
        assert urlopen(url, timeout=2).read().decode() == "first-token"
        assert urlopen(url, timeout=2).read().decode() == "second-token"
    finally:
        server.shutdown()
        server.server_close()


def test_fetch_sp_token_uses_configured_broker(monkeypatch):
    monkeypatch.setenv("CODA_SP_TOKEN_BROKER_URL", "http://127.0.0.1:9/token")
    monkeypatch.setattr(
        broker,
        "urlopen",
        lambda *_args, **_kwargs: _Response(b"broker-token"),
    )

    assert broker.fetch_sp_token() == "broker-token"


def test_databricks_wrapper_intercepts_only_broker_profile(tmp_path):
    server = broker.start_sp_token_broker(lambda: "fresh-token", port=0)
    try:
        wrapper = write_databricks_token_wrapper(tmp_path, "/usr/bin/false")
        # HOME without a ~/.local/bin/databricks: the shim prefers an installed
        # CLI at call time (see _real_cli), and this case asserts delegation to
        # the baked path, so the real container CLI must not be picked up here.
        empty_home = tmp_path / "no-installed-cli"
        empty_home.mkdir()
        env = dict(
            os.environ,
            HOME=str(empty_home),
            CODA_SP_TOKEN_BROKER_URL=broker.broker_url(server),
        )
        result = subprocess.run(
            [
                str(wrapper),
                "auth",
                "token",
                "--profile",
                "omnigents-host",
                "--output",
                "json",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        # The shim must emit the FULL OAuth shape the databricks-sdk CLI token
        # source (DatabricksCliTokenSource) requires: access_token + token_type
        # + expiry. A bare {access_token} raises "cannot unmarshal CLI result",
        # which breaks Config(profile=...).authenticate() and collapses pi's
        # model picker to a single default.
        payload = json.loads(result.stdout)
        assert payload["access_token"] == "fresh-token"
        assert payload["token_type"] == "Bearer"
        # expiry parses in the SDK's format ("%Y-%m-%dT%H:%M:%S", trailing Z ok)
        # and is in the near future (shim sets now + 5 min).
        parsed = datetime.datetime.strptime(
            payload["expiry"].rstrip("Z").split(".")[0], "%Y-%m-%dT%H:%M:%S"
        )
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        assert now < parsed <= now + datetime.timedelta(minutes=6)

        # The SDK's DatabricksCliTokenSource builds `auth token --profile <p>`
        # WITHOUT `--output json` yet still json.loads()s stdout. So the shim
        # must emit JSON on the no-flag path too — this is the exact regression
        # that collapsed pi's model picker (SDK: "cannot unmarshal CLI result").
        no_flag = subprocess.run(
            [str(wrapper), "auth", "token", "--profile", "omnigents-host"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        no_flag_payload = json.loads(no_flag.stdout)
        assert no_flag_payload["access_token"] == "fresh-token"
        assert no_flag_payload["token_type"] == "Bearer"
        assert "expiry" in no_flag_payload

        delegated = subprocess.run([str(wrapper), "--version"], env=env, check=False)
        assert delegated.returncode == 1
    finally:
        server.shutdown()
        server.server_close()


def test_databricks_wrapper_routes_direct_profile_commands_through_broker(tmp_path):
    """A terminal user types `databricks current-user me`, not `auth token`.
    The shim must inject the broker token into the delegated real CLI for the
    secret-free omnigents-host profile, otherwise the real CLI searches for a
    nonexistent OAuth cache."""
    server = broker.start_sp_token_broker(lambda: "direct-command-token", port=0)
    try:
        real_cli = tmp_path / "real-databricks"
        real_cli.write_text(
            "#!/bin/sh\n"
            "printf 'TOKEN=%s HOST=%s ARGS=%s\\n' \"$DATABRICKS_TOKEN\" \"$DATABRICKS_HOST\" \"$*\"\n"
        )
        real_cli.chmod(0o700)
        cfg = tmp_path / ".databrickscfg"
        cfg.write_text(
            "[omnigents-host]\n"
            "host = https://workspace.example\n"
            "auth_type = databricks-cli\n"
        )
        wrapper = write_databricks_token_wrapper(tmp_path / "bin", str(real_cli))
        env = dict(
            os.environ,
            HOME=str(tmp_path),
            CODA_SP_TOKEN_BROKER_URL=broker.broker_url(server),
            DATABRICKS_CONFIG_PROFILE="omnigents-host",
        )
        result = subprocess.run(
            [str(wrapper), "current-user", "me"],
            env=env, capture_output=True, text=True, check=True,
        )
        assert "TOKEN=direct-command-token" in result.stdout
        assert "HOST=https://workspace.example" in result.stdout
        assert "ARGS=current-user me" in result.stdout
    finally:
        server.shutdown()
        server.server_close()


class _Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.body


def test_databricks_wrapper_prefers_installed_cli_over_baked_path(tmp_path):
    """A CLI installed after the shim was written must still win.

    The wrapper is generated as soon as the SP broker is up, which can be before
    install_databricks_cli.sh has put the current CLI in ~/.local/bin. Baking
    the older image CLI for the container's lifetime silently downgraded every
    call — including Databricks Asset Bundle deploys, where the old CLI ignores
    `bundle.engine: direct` and falls back to Terraform.
    """
    home = tmp_path / "home"
    stale_cli = tmp_path / "stale-databricks"
    stale_cli.write_text("#!/bin/sh\nprintf 'STALE ARGS=%s\\n' \"$*\"\n")
    stale_cli.chmod(0o700)
    installed_cli = home / ".local" / "bin" / "databricks"
    installed_cli.parent.mkdir(parents=True)
    installed_cli.write_text("#!/bin/sh\nprintf 'INSTALLED ARGS=%s\\n' \"$*\"\n")
    installed_cli.chmod(0o700)

    wrapper = write_databricks_token_wrapper(tmp_path / "bin", str(stale_cli))
    env = dict(os.environ, HOME=str(home))
    env.pop("DATABRICKS_CONFIG_PROFILE", None)
    env.pop("CODA_SP_TOKEN_BROKER_URL", None)

    result = subprocess.run(
        [str(wrapper), "version"], env=env, capture_output=True, text=True, check=True
    )

    assert "INSTALLED ARGS=version" in result.stdout
    assert "STALE" not in result.stdout


def test_databricks_wrapper_falls_back_when_no_installed_cli(tmp_path):
    """With no ~/.local/bin CLI the baked path is still used, not an error."""
    home = tmp_path / "home"
    home.mkdir()
    baked_cli = tmp_path / "baked-databricks"
    baked_cli.write_text("#!/bin/sh\nprintf 'BAKED ARGS=%s\\n' \"$*\"\n")
    baked_cli.chmod(0o700)

    wrapper = write_databricks_token_wrapper(tmp_path / "bin", str(baked_cli))
    env = dict(os.environ, HOME=str(home))
    env.pop("DATABRICKS_CONFIG_PROFILE", None)
    env.pop("CODA_SP_TOKEN_BROKER_URL", None)

    result = subprocess.run(
        [str(wrapper), "version"], env=env, capture_output=True, text=True, check=True
    )

    assert "BAKED ARGS=version" in result.stdout
