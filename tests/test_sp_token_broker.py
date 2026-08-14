import datetime
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen

import pytest

import sp_token_broker as broker
from token_helper import write_databricks_token_wrapper
import http.client
import socket
import threading
from urllib.error import HTTPError
from urllib.parse import urlsplit


class _RedirectHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", self.server.redirect_target)
        self.end_headers()

    def log_message(self, *_args):
        pass


class _RedirectTokenHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"redirected-token"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def redirecting_broker_url():
    target = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectTokenHandler)
    redirect = ThreadingHTTPServer(("127.0.0.1", 0), _RedirectHandler)
    target_thread = threading.Thread(target=target.serve_forever, daemon=True)
    redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
    target_thread.start()
    redirect_thread.start()
    redirect.redirect_target = f"http://127.0.0.1:{target.server_port}/not-a-broker-path"
    try:
        yield f"http://127.0.0.1:{redirect.server_port}/token/" + "a" * 43
    finally:
        redirect.shutdown()
        target.shutdown()
        redirect.server_close()
        target.server_close()
        redirect_thread.join(timeout=2)
        target_thread.join(timeout=2)


def test_fetch_sp_token_rejects_redirects(redirecting_broker_url):
    assert broker.fetch_sp_token(redirecting_broker_url) is None


def test_broker_binds_loopback_and_mints_per_request():
    """Initial and continuation resolutions receive fresh broker tokens."""
    tokens = iter(("first-token", "second-token"))
    server = broker.start_sp_token_broker(lambda: next(tokens), port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
        url = broker.broker_url(server)
        assert urlsplit(url).path.startswith("/token/")
        assert len(urlsplit(url).path.rsplit("/", 1)[1]) >= 32
        with urlopen(url, timeout=2) as response:
            initial = response.read().decode()
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Pragma"] == "no-cache"
            assert response.headers.get_content_type() == "text/plain"
        continuation = urlopen(url, timeout=2).read().decode()
        assert initial == "first-token"
        assert continuation == "second-token"
        assert initial != continuation
    finally:
        server.shutdown()
        server.server_close()



@pytest.mark.parametrize("body", [b"", b"  \n", b"\xff"])
def test_fetch_sp_token_rejects_empty_or_malformed_response(monkeypatch, body):
    monkeypatch.setenv(
        "CODA_SP_TOKEN_BROKER_URL", "http://127.0.0.1:9/token/" + "a" * 43
    )
    monkeypatch.setattr(broker, "urlopen", lambda *_args, **_kwargs: _Response(body))

    assert broker.fetch_sp_token() is None



def test_fetch_sp_token_uses_configured_broker(monkeypatch):
    monkeypatch.setenv(
        "CODA_SP_TOKEN_BROKER_URL", "http://127.0.0.1:9/token/" + "a" * 43
    )
    monkeypatch.setattr(
        broker,
        "urlopen",
        lambda *_args, **_kwargs: _Response(b"broker-token"),
    )

    assert broker.fetch_sp_token() == "broker-token"



def test_databricks_wrapper_rejects_redirected_token(tmp_path, redirecting_broker_url):
    real_cli = tmp_path / "real-databricks"
    real_cli.write_text("#!/bin/sh\necho delegated\nexit 7\n")
    real_cli.chmod(0o700)
    wrapper = write_databricks_token_wrapper(tmp_path / "bin", str(real_cli))
    home = tmp_path / "home"
    home.mkdir()
    result = subprocess.run(
        [str(wrapper), "auth", "token", "--profile", "omnigents-host"],
        env={**os.environ, "HOME": str(home), "CODA_SP_TOKEN_BROKER_URL": redirecting_broker_url},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 7
    assert "redirected-token" not in result.stdout
    assert "delegated" in result.stdout


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


def test_databricks_wrapper_honors_absolute_config_file_and_cleans_shadowing(
    tmp_path, caplog
):
    server = broker.start_sp_token_broker(lambda: "custom-config-token", port=0)
    try:
        real_cli = tmp_path / "real-databricks"
        real_cli.write_text(
            "#!/bin/sh\n"
            "printf 'TOKEN=%s HOST=%s PROFILE=%s SECRET=%s CONFIG=%s ARGS=%s\\n' "
            '"$DATABRICKS_TOKEN" "$DATABRICKS_HOST" '
            '"$DATABRICKS_CONFIG_PROFILE" "$DATABRICKS_CLIENT_SECRET" '
            '"$DATABRICKS_CONFIG_FILE" "$*"\n'
        )
        real_cli.chmod(0o700)
        custom_cfg = tmp_path / "custom" / "databricks.cfg"
        custom_cfg.parent.mkdir()
        custom_cfg.write_text(
            "[omnigents-host]\n"
            "host = https://custom.workspace.example\n"
            "auth_type = databricks-cli\n"
        )
        wrapper = write_databricks_token_wrapper(tmp_path / "bin", str(real_cli))
        env = dict(
            os.environ,
            HOME=str(tmp_path / "different-home"),
            CODA_SP_TOKEN_BROKER_URL=broker.broker_url(server),
            DATABRICKS_CONFIG_FILE=str(custom_cfg),
            DATABRICKS_CONFIG_PROFILE="omnigents-host",
            DATABRICKS_TOKEN="AMBIENT_BEARER_MARKER",
            DATABRICKS_HOST="ambient.workspace.example",
            DATABRICKS_CLIENT_ID="AMBIENT_CLIENT_ID_MARKER",
            DATABRICKS_CLIENT_SECRET="AMBIENT_SECRET_MARKER",
        )
        result = subprocess.run(
            [str(wrapper), "current-user", "me"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "TOKEN=custom-config-token" in result.stdout
        assert "HOST=https://custom.workspace.example" in result.stdout
        assert "PROFILE= SECRET=" in result.stdout
        assert "CONFIG=" + str(custom_cfg) in result.stdout
        assert "ARGS=current-user me" in result.stdout
        observable = result.stdout + result.stderr + caplog.text
        assert "AMBIENT_BEARER_MARKER" not in observable
        assert "AMBIENT_CLIENT_ID_MARKER" not in observable
        assert "AMBIENT_SECRET_MARKER" not in observable
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
    def __init__(self, body, content_type="text/plain; charset=utf-8"):
        self.body = body
        self.headers = {"Content-Type": content_type}


    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, *_args):
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


def test_databricks_wrapper_leaves_u2m_profile_identity_alone(tmp_path):
    """A configured U2M auth cache must not be replaced by the app SP."""
    server = broker.start_sp_token_broker(lambda: "must-not-be-used", port=0)
    try:
        real_cli = _echo_cli(tmp_path / "real-databricks")
        _host_only_cfg(
            tmp_path,
            extra=(
                "\n[DEFAULT]\nhost = https://workspace.example\n"
                "auth_type = databricks-cli\n"
            ),
        )
        wrapper = write_databricks_token_wrapper(tmp_path / "bin", str(real_cli))
        result = subprocess.run(
            [str(wrapper), "current-user", "me"],
            env=_wrapper_env(tmp_path, server),
            capture_output=True,
            text=True,
            check=True,
        )
        assert "TOKEN= " in result.stdout
        assert "must-not-be-used" not in result.stdout
    finally:
        broker.stop_sp_token_broker(server)


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


def _request(server, path, *, method="GET", host=None, body=None, headers=()):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
    connection.putrequest(method, path, skip_host=True)
    connection.putheader("Host", host or f"127.0.0.1:{server.server_port}")
    if body is not None:
        connection.putheader("Content-Length", str(len(body)))
    for name, value in headers:
        connection.putheader(name, value)
    connection.endheaders(body)
    response = connection.getresponse()
    result = (response.status, dict(response.headers), response.read())
    connection.close()
    return result


@pytest.mark.parametrize(
    "method,path_kind,host_kind,body",
    [
        ("GET", "bare", "valid", None),
        ("GET", "wrong", "valid", None),
        ("GET", "malformed", "valid", None),
        ("GET", "query", "valid", None),
        ("GET", "valid", "localhost", None),
        ("GET", "valid", "missing_port", None),
        ("GET", "valid", "userinfo", None),
        ("POST", "valid", "valid", b""),
        ("GET", "valid", "valid", b"x"),
    ],
)
def test_broker_rejects_unauthorized_request_shapes_without_minting(
    method, path_kind, host_kind, body
):
    mint_calls = []
    server = broker.start_sp_token_broker(lambda: mint_calls.append(True) or "token")
    try:
        valid_path = urlsplit(broker.broker_url(server)).path
        paths = {
            "valid": valid_path,
            "bare": "/token",
            "wrong": "/token/" + "x" * 43,
            "malformed": "/token/%capability",
            "query": valid_path + "?copy=1",
        }
        hosts = {
            "valid": f"127.0.0.1:{server.server_port}",
            "localhost": f"localhost:{server.server_port}",
            "missing_port": "127.0.0.1",
            "userinfo": f"user@127.0.0.1:{server.server_port}",
        }
        host = hosts[host_kind]
        status, headers, response_body = _request(
            server, paths[path_kind], method=method, host=host, body=body
        )
        assert status in (404, 405)
        assert response_body == b""
        assert headers["Cache-Control"] == "no-store"
        assert mint_calls == []
    finally:
        server.shutdown()
        server.server_close()


def test_capability_is_per_process_and_cannot_be_replayed_after_restart():
    old = broker.start_sp_token_broker(lambda: "old-token")
    old_path = urlsplit(broker.broker_url(old)).path
    old.shutdown()
    old.server_close()

    mint_calls = []
    new = broker.start_sp_token_broker(lambda: mint_calls.append(True) or "new-token")
    try:
        assert urlsplit(broker.broker_url(new)).path != old_path
        status, _, body = _request(new, old_path)
        assert status == 404
        assert body == b""
        assert mint_calls == []
    finally:
        new.shutdown()
        new.server_close()


def test_duplicate_host_and_transfer_encoding_are_rejected_without_minting():
    mint_calls = []
    server = broker.start_sp_token_broker(lambda: mint_calls.append(True) or "token")
    try:
        path = urlsplit(broker.broker_url(server)).path
        status, _, _ = _request(
            server,
            path,
            headers=(("Host", f"127.0.0.1:{server.server_port}"),),
        )
        assert status == 404
        status, _, _ = _request(
            server,
            path,
            headers=(("Transfer-Encoding", "chunked"),),
        )
        assert status == 404
        assert mint_calls == []
    finally:
        server.shutdown()
        server.server_close()


def test_mint_failure_is_sanitized_and_never_returns_token_fragment():
    def fail():
        raise RuntimeError("mint failed for secret-fragment")

    server = broker.start_sp_token_broker(fail)
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(broker.broker_url(server), timeout=2)
        assert error.value.code == 503
        assert error.value.read() == b""
        assert "secret-fragment" not in str(error.value)
    finally:
        server.shutdown()
        server.server_close()


def test_mint_concurrency_is_bounded_without_spawning_unbounded_work():
    entered = threading.Event()
    release = threading.Event()
    mint_calls = []

    def mint():
        mint_calls.append(True)
        entered.set()
        assert release.wait(2)
        return "bounded-token"

    server = broker.start_sp_token_broker(mint, max_concurrent_mints=1)
    first_result = []
    try:
        first = threading.Thread(
            target=lambda: first_result.append(
                urlopen(broker.broker_url(server), timeout=3).read()
            )
        )
        first.start()
        assert entered.wait(1)
        with pytest.raises(HTTPError) as error:
            urlopen(broker.broker_url(server), timeout=2)
        assert error.value.code == 503
        assert mint_calls == [True]
        release.set()
        first.join(2)
        assert not first.is_alive()
        assert first_result == [b"bounded-token"]
    finally:
        release.set()
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"port": -1}, "port"),
        ({"max_concurrent_mints": 0}, "max_concurrent_mints"),
        ({"max_concurrent_handlers": 0}, "max_concurrent_handlers"),
        ({"mint_timeout_seconds": 0}, "mint_timeout_seconds"),
        ({"request_timeout_seconds": 0}, "request_timeout_seconds"),
    ],
)
def test_broker_rejects_unbounded_or_invalid_configuration(kwargs, match):
    with pytest.raises(ValueError, match=match):
        broker.start_sp_token_broker(lambda: "token", **kwargs)


def test_request_processing_has_a_socket_deadline():
    server = broker.start_sp_token_broker(lambda: "token", request_timeout_seconds=0.25)
    try:
        assert server.request_timeout_seconds == 0.25
    finally:
        server.shutdown()
        server.server_close()


def test_shutdown_closes_listener_and_invalidates_capability():
    server = broker.start_sp_token_broker(lambda: "token")
    port = server.server_port
    broker.stop_sp_token_broker(server)

    with pytest.raises(RuntimeError, match="closed"):
        broker.broker_url(server)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=0.2)


def test_empty_capability_fails_closed_before_listener_close():
    mint_calls = []
    server = broker.start_sp_token_broker(lambda: mint_calls.append(True) or "token")
    old_path = urlsplit(broker.broker_url(server)).path
    try:
        server.begin_shutdown()
        status, _, body = _request(server, old_path)
        assert status == 404
        assert body == b""
        assert mint_calls == []
    finally:
        broker.stop_sp_token_broker(server)


def test_non_ascii_host_is_sanitized_without_mint_or_traceback():
    mint_calls = []
    server = broker.start_sp_token_broker(lambda: mint_calls.append(True) or "token")
    try:
        path = urlsplit(broker.broker_url(server)).path.encode("ascii")
        raw = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
        raw.sendall(b"GET " + path + b" HTTP/1.1\r\nHost: \xff\r\n\r\n")
        response = raw.recv(4096)
        raw.close()
        assert b" 404 " in response
        assert b"token" not in response
        assert mint_calls == []
    finally:
        broker.stop_sp_token_broker(server)


def test_slow_connections_are_bounded_before_handler_thread_creation():
    mint_calls = []
    server = broker.start_sp_token_broker(
        lambda: mint_calls.append(True) or "token",
        max_concurrent_handlers=1,
        request_timeout_seconds=0.5,
    )
    slow = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
    try:
        slow.sendall(b"GET /token/")
        deadline = datetime.datetime.now().timestamp() + 1
        while getattr(server.handler_slots, "_value", 1) != 0:
            if datetime.datetime.now().timestamp() >= deadline:
                pytest.fail("slow connection never occupied the bounded handler slot")
            threading.Event().wait(0.01)
        with pytest.raises(
            (ConnectionResetError, http.client.RemoteDisconnected, OSError)
        ):
            _request(server, urlsplit(broker.broker_url(server)).path)
        assert mint_calls == []
    finally:
        slow.close()
        broker.stop_sp_token_broker(server)


def test_drip_fed_request_cannot_hold_handler_slot_past_total_deadline():
    server = broker.start_sp_token_broker(
        lambda: "token",
        max_concurrent_handlers=1,
        request_timeout_seconds=0.2,
    )
    slow = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
    stop = threading.Event()

    def drip() -> None:
        while not stop.wait(0.05):
            try:
                slow.sendall(b"x")
            except OSError:
                return

    thread = threading.Thread(target=drip)
    try:
        slow.sendall(b"GET /token/")
        thread.start()
        assert thread.is_alive()
        thread.join(1)
        assert not thread.is_alive()
        deadline = datetime.datetime.now().timestamp() + 1
        while getattr(server.handler_slots, "_value", 0) != 1:
            if datetime.datetime.now().timestamp() >= deadline:
                pytest.fail("total request deadline did not reclaim handler slot")
            stop.wait(0.01)
    finally:
        stop.set()
        slow.close()
        thread.join(1)
        broker.stop_sp_token_broker(server)


def test_shutdown_linearizes_before_token_response_write():
    server = broker.start_sp_token_broker(lambda: "must-not-be-returned")
    entered = threading.Event()
    release = threading.Event()

    class BarrierLock:
        def __init__(self):
            self._lock = threading.Lock()

        def __enter__(self):
            if threading.current_thread() is not threading.main_thread():
                entered.set()
                assert release.wait(2)
            self._lock.acquire()
            return self

        def __exit__(self, *_args):
            self._lock.release()
            return None

    server.response_lock = BarrierLock()
    outcome = []

    def request_token() -> None:
        try:
            outcome.append(urlopen(broker.broker_url(server), timeout=3).read())
        except HTTPError as exc:
            outcome.append((exc.code, exc.read()))

    request_thread = threading.Thread(target=request_token)
    request_thread.start()
    try:
        assert entered.wait(1)
        server.begin_shutdown()
        release.set()
        request_thread.join(2)
        assert outcome == [(503, b"")]
    finally:
        release.set()
        broker.stop_sp_token_broker(server)


def test_shutdown_does_not_return_token_from_authorized_in_flight_mint():
    entered = threading.Event()
    release = threading.Event()

    def mint():
        entered.set()
        assert release.wait(2)
        return "must-not-be-returned"

    server = broker.start_sp_token_broker(mint)
    outcome = []

    def request_token():
        try:
            outcome.append(urlopen(broker.broker_url(server), timeout=3).read())
        except HTTPError as exc:
            outcome.append((exc.code, exc.read()))

    request_thread = threading.Thread(target=request_token)
    request_thread.start()
    try:
        assert entered.wait(1)
        server.begin_shutdown()
        release.set()
        request_thread.join(2)
        assert not request_thread.is_alive()
        assert outcome == [(503, b"")]
    finally:
        release.set()
        broker.stop_sp_token_broker(server)


@pytest.mark.parametrize(
    "target",
    [
        "https://127.0.0.1:1234/token/" + "a" * 43,
        "http://localhost:1234/token/" + "a" * 43,
        "http://127.0.0.2:1234/token/" + "a" * 43,
        "http://user@127.0.0.1:1234/token/" + "a" * 43,
        "http://127.0.0.1:1234/token",
        "http://127.0.0.1:1234/token/short",
        "http://127.0.0.1:1234/token/" + "a" * 43 + "?query=1",
    ],
)
def test_fetch_sp_token_rejects_non_capability_or_non_loopback_target(
    monkeypatch, target
):
    called = []
    monkeypatch.setattr(broker, "urlopen", lambda *_a, **_k: called.append(True))

    assert broker.fetch_sp_token(target) is None
    assert called == []


@pytest.mark.parametrize("content_type", [None, "", "application/json", "text/html"])
def test_fetch_sp_token_rejects_wrong_or_missing_content_type(
    monkeypatch, content_type
):
    monkeypatch.setenv(
        "CODA_SP_TOKEN_BROKER_URL", "http://127.0.0.1:9/token/" + "a" * 43
    )
    monkeypatch.setattr(
        broker,
        "urlopen",
        lambda *_args, **_kwargs: _Response(b"broker-token", content_type),
    )

    assert broker.fetch_sp_token() is None
