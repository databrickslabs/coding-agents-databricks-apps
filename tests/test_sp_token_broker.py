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


# --- Deploy-from-Omnigent auth path (bundle deploy without a PAT) ------------
#
# These cover the failure that made Databricks Asset Bundle deploys look
# impossible from an Omnigent runner: the Go CLI honours neither
# `databricks_cli_path` (a Python-SDK Config field) nor an injected
# DATABRICKS_TOKEN once a named profile is selected, and Omnigent's native
# harness terminals unset DATABRICKS_CONFIG_PROFILE outright.


def _echo_cli(path):
    path.write_text(
        "#!/bin/sh\n"
        "printf 'TOKEN=%s HOST=%s PROFILE=%s ARGS=%s\\n' "
        "\"$DATABRICKS_TOKEN\" \"$DATABRICKS_HOST\" "
        "\"$DATABRICKS_CONFIG_PROFILE\" \"$*\"\n"
    )
    path.chmod(0o700)
    return path


def _host_only_cfg(tmp_path, extra=""):
    cfg = tmp_path / ".databrickscfg"
    cfg.write_text(
        "[omnigents-host]\n"
        "host = https://workspace.example\n"
        "auth_type = databricks-cli\n"
        "databricks_cli_path = /nonexistent/databricks\n" + extra
    )
    return cfg


def _wrapper_env(tmp_path, server, **overrides):
    env = dict(
        os.environ,
        HOME=str(tmp_path),
        CODA_SP_TOKEN_BROKER_URL=broker.broker_url(server),
    )
    for key in (
        "DATABRICKS_CONFIG_PROFILE",
        "DATABRICKS_TOKEN",
        "DATABRICKS_HOST",
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
        "DATABRICKS_CONFIG_FILE",
        "CODA_BROKER_ALLOW_AUTH_LOGIN",
    ):
        env.pop(key, None)
    env.update(overrides)
    return env


@pytest.mark.parametrize(
    "selector",
    [
        ["--profile", "omnigents-host"],
        ["--profile=omnigents-host"],
        ["-p", "omnigents-host"],
        ["-p=omnigents-host"],
    ],
)
def test_databricks_wrapper_strips_explicit_broker_profile_flag(tmp_path, selector):
    """`databricks bundle deploy --profile omnigents-host` must authenticate.

    Regression: the flag survived into the real CLI, which then read
    `auth_type = databricks-cli` from the profile, ignored the injected
    DATABRICKS_TOKEN, and failed with "cache: no cached credentials; run
    `databricks auth login` to sign in".
    """
    server = broker.start_sp_token_broker(lambda: "flag-token", port=0)
    try:
        real_cli = _echo_cli(tmp_path / "real-databricks")
        _host_only_cfg(tmp_path)
        wrapper = write_databricks_token_wrapper(tmp_path / "bin", str(real_cli))
        result = subprocess.run(
            [str(wrapper), "bundle", "deploy", "-t", "dev", *selector],
            env=_wrapper_env(tmp_path, server),
            capture_output=True,
            text=True,
            check=True,
        )
        assert "TOKEN=flag-token" in result.stdout
        assert "HOST=https://workspace.example" in result.stdout
        assert "ARGS=bundle deploy -t dev" in result.stdout
        assert "omnigents-host" not in result.stdout.split("ARGS=")[1]
    finally:
        server.shutdown()
        server.server_close()


def test_databricks_wrapper_brokers_when_no_profile_is_selected(tmp_path):
    """Omnigent runners unset DATABRICKS_CONFIG_PROFILE; deploys must still work."""
    server = broker.start_sp_token_broker(lambda: "implicit-token", port=0)
    try:
        real_cli = _echo_cli(tmp_path / "real-databricks")
        _host_only_cfg(tmp_path)
        wrapper = write_databricks_token_wrapper(tmp_path / "bin", str(real_cli))
        result = subprocess.run(
            [str(wrapper), "bundle", "validate", "--strict", "-t", "dev"],
            env=_wrapper_env(tmp_path, server),
            capture_output=True,
            text=True,
            check=True,
        )
        assert "TOKEN=implicit-token" in result.stdout
        assert "HOST=https://workspace.example" in result.stdout
        assert "ARGS=bundle validate --strict -t dev" in result.stdout
    finally:
        server.shutdown()
        server.server_close()


def test_databricks_wrapper_leaves_pat_identity_alone(tmp_path):
    """A [DEFAULT] PAT keeps winning: no silent switch to the app SP identity.

    The PAT bootstrap (`/api/configure-pat`, `/api/inject-pat`, `pat_rotator`)
    is the human-identity path. Bare `databricks` calls must keep resolving it,
    or an injected-PAT fleet would start deploying as the service principal.
    """
    server = broker.start_sp_token_broker(lambda: "must-not-be-used", port=0)
    try:
        real_cli = _echo_cli(tmp_path / "real-databricks")
        _host_only_cfg(tmp_path, extra="\n[DEFAULT]\nhost = https://workspace.example\ntoken = dapi-human-pat\n")
        wrapper = write_databricks_token_wrapper(tmp_path / "bin", str(real_cli))
        result = subprocess.run(
            [str(wrapper), "current-user", "me"],
            env=_wrapper_env(tmp_path, server),
            capture_output=True,
            text=True,
            check=True,
        )
        assert "TOKEN= " in result.stdout  # no injection; real CLI reads the file
        assert "must-not-be-used" not in result.stdout
    finally:
        server.shutdown()
        server.server_close()


def test_databricks_wrapper_leaves_ambient_env_credentials_alone(tmp_path):
    server = broker.start_sp_token_broker(lambda: "must-not-be-used", port=0)
    try:
        real_cli = _echo_cli(tmp_path / "real-databricks")
        _host_only_cfg(tmp_path)
        wrapper = write_databricks_token_wrapper(tmp_path / "bin", str(real_cli))
        result = subprocess.run(
            [str(wrapper), "current-user", "me"],
            env=_wrapper_env(tmp_path, server, DATABRICKS_TOKEN="ambient-pat"),
            capture_output=True,
            text=True,
            check=True,
        )
        assert "TOKEN=ambient-pat" in result.stdout
    finally:
        server.shutdown()
        server.server_close()


def test_databricks_auth_login_is_a_no_op_with_guidance(tmp_path):
    """`auth login` can never succeed here; it must not hang an agent."""
    server = broker.start_sp_token_broker(lambda: "login-probe-token", port=0)
    try:
        real_cli = tmp_path / "real-databricks"
        real_cli.write_text("#!/bin/sh\necho REAL_LOGIN_RAN\nexit 7\n")
        real_cli.chmod(0o700)
        _host_only_cfg(tmp_path)
        wrapper = write_databricks_token_wrapper(tmp_path / "bin", str(real_cli))
        result = subprocess.run(
            [str(wrapper), "auth", "login"],
            env=_wrapper_env(tmp_path, server),
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "REAL_LOGIN_RAN" not in result.stdout
        assert "already has" in result.stderr
        assert "bundle deploy" in result.stderr
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    "extra_args,env_overrides",
    [
        # Omnigent's _run_databricks_browser_login passes the ?o= org selector.
        (["--host", "https://workspace.example/?o=12345"], {}),
        # Another workspace entirely.
        (["--host", "https://other.workspace.example"], {}),
        # A different profile is an explicit request for the real flow.
        (["--profile", "some-other-profile"], {}),
        # Operator escape hatch.
        ([], {"CODA_BROKER_ALLOW_AUTH_LOGIN": "1"}),
    ],
)
def test_databricks_auth_login_passthrough_cases(tmp_path, extra_args, env_overrides):
    """The no-op is narrow: Omnigent's own login flow must reach the real CLI."""
    server = broker.start_sp_token_broker(lambda: "login-probe-token", port=0)
    try:
        real_cli = tmp_path / "real-databricks"
        real_cli.write_text("#!/bin/sh\necho REAL_LOGIN_RAN\nexit 7\n")
        real_cli.chmod(0o700)
        _host_only_cfg(tmp_path)
        wrapper = write_databricks_token_wrapper(tmp_path / "bin", str(real_cli))
        result = subprocess.run(
            [str(wrapper), "auth", "login", *extra_args],
            env=_wrapper_env(tmp_path, server, **env_overrides),
            capture_output=True,
            text=True,
        )
        assert "REAL_LOGIN_RAN" in result.stdout
        assert result.returncode == 7
    finally:
        server.shutdown()
        server.server_close()


def test_omnigent_sdk_token_contract_is_unchanged_by_profile_stripping(tmp_path):
    """`auth token --profile omnigents-host` still returns the SDK JSON shape.

    This is the contract omnigent's resolve_databricks_workspace / pi model
    catalog depends on; the profile-stripping fix must not touch it.
    """
    server = broker.start_sp_token_broker(lambda: "sdk-token", port=0)
    try:
        real_cli = tmp_path / "real-databricks"
        real_cli.write_text("#!/bin/sh\necho REAL_CLI_RAN\nexit 9\n")
        real_cli.chmod(0o700)
        _host_only_cfg(tmp_path)
        wrapper = write_databricks_token_wrapper(tmp_path / "bin", str(real_cli))
        result = subprocess.run(
            [str(wrapper), "auth", "token", "--profile", "omnigents-host"],
            env=_wrapper_env(tmp_path, server),
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(result.stdout)
        assert payload["access_token"] == "sdk-token"
        assert payload["token_type"] == "Bearer"
        datetime.datetime.strptime(payload["expiry"], "%Y-%m-%dT%H:%M:%SZ")
    finally:
        server.shutdown()
        server.server_close()
