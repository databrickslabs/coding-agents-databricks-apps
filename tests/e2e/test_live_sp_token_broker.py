"""Drive a deployed CoDA PTY and verify the brokered SP credential boundary."""

from __future__ import annotations

import base64
import json
import re
import time

import pytest

pytest.importorskip("playwright.sync_api")

from tests.e2e.test_live_security import _read_output, _send_input


def _probe_command() -> str:
    script = r'''
import configparser
import json
import os
import pathlib
import subprocess
import sys

cfg = configparser.ConfigParser()
cfg.read(pathlib.Path.home() / ".databrickscfg")
profile = dict(cfg["omnigents-host"])
helper = pathlib.Path.home() / ".claude" / "anthropic-token-helper.py"
helper_run = subprocess.run([sys.executable, str(helper)], capture_output=True, text=True)
wrapper_run = subprocess.run(
    [str(pathlib.Path.home() / ".coda-broker-bin" / "databricks"),
     "auth", "token", "--profile", "omnigents-host", "--output", "json"],
    capture_output=True,
    text=True,
)
try:
    wrapper_token = json.loads(wrapper_run.stdout).get("access_token", "")
except Exception:
    wrapper_token = ""

result = {
    "profile_keys": sorted(profile),
    "secret_free_profile": not ({"client_id", "client_secret", "token"} & set(profile)),
    "secret_free_env": "DATABRICKS_CLIENT_SECRET" not in os.environ,
    "helper_ok": helper_run.returncode == 0 and len(helper_run.stdout.strip()) > 20,
    "wrapper_ok": wrapper_run.returncode == 0 and len(wrapper_token) > 20,
    "unicode": "café Ελληνικά 日本語 😀",
}
print("BROKER-E2E=" + json.dumps(result, ensure_ascii=False))
'''
    encoded = base64.b64encode(script.encode()).decode()
    return f"echo {encoded} | base64 -d | python3; echo BROKER-E2E-EXIT=$?"


def test_live_sp_token_broker_and_unicode(page, app_url):
    page.goto(app_url, timeout=30_000)
    page.wait_for_function(
        """async () => {
            try {
                const r = await fetch('/api/sessions', {credentials: 'include'});
                return r.ok;
            } catch (_) {
                return false;
            }
        }""",
        timeout=180_000,
    )

    new_session = page.evaluate(
        """async () => {
            const r = await fetch('/api/session', {
                method: 'POST',
                credentials: 'include',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({label: 'broker-e2e'}),
            });
            return r.json();
        }"""
    )
    sid = new_session["session_id"]
    time.sleep(2)
    _read_output(page, sid)
    _send_input(page, sid, _probe_command())

    deadline = time.time() + 60
    output = ""
    exit_re = re.compile(r"BROKER-E2E-EXIT=(\d+)")
    while time.time() < deadline and not exit_re.search(output):
        output += _read_output(page, sid)
        time.sleep(0.5)

    match = re.search(r"BROKER-E2E=(\{.*\})", output)
    assert match, output[-3000:]
    result = json.loads(match.group(1))
    assert result["profile_keys"] == ["host"]
    assert result["secret_free_profile"] is True
    assert result["secret_free_env"] is True
    assert result["helper_ok"] is True
    assert result["wrapper_ok"] is True
    assert result["unicode"] == "café Ελληνικά 日本語 😀"
    assert not re.search(r"Ã|Â|â|ðŸ|�", output)
    assert exit_re.search(output).group(1) == "0"
