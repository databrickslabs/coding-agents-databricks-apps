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
        env = dict(os.environ, CODA_SP_TOKEN_BROKER_URL=broker.broker_url(server))
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
        assert json.loads(result.stdout) == {"access_token": "fresh-token"}

        delegated = subprocess.run([str(wrapper), "--version"], env=env, check=False)
        assert delegated.returncode == 1
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
