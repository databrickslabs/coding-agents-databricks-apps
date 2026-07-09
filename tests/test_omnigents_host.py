"""Tests for omnigents_host — the Omnigents agent-host integration.

Covers the off-by-default contract (AC-6) and the credential guards: the
feature must be inert with no config, and must refuse to start the host
without the OAuth-capable SP creds (a PAT alone is rejected by the Apps proxy).
"""

from __future__ import annotations

import hashlib

import yaml

import omnigents_host as oh


def test_disabled_by_default(monkeypatch):
    """No OMNIGENTS_SERVER_URL -> feature reports disabled (AC-6)."""
    monkeypatch.delenv("OMNIGENTS_SERVER_URL", raising=False)
    assert oh.omnigents_host_enabled() is False


def test_enabled_when_server_url_set(monkeypatch):
    monkeypatch.setenv("OMNIGENTS_SERVER_URL", "https://srv.example.com")
    assert oh.omnigents_host_enabled() is True


def test_blank_server_url_is_disabled(monkeypatch):
    monkeypatch.setenv("OMNIGENTS_SERVER_URL", "   ")
    assert oh.omnigents_host_enabled() is False


def test_start_host_noop_when_disabled(monkeypatch):
    """Disabled -> start_host never installs or spawns a thread (AC-6)."""
    oh.reset_for_tests()
    monkeypatch.delenv("OMNIGENTS_SERVER_URL", raising=False)
    monkeypatch.setattr(oh, "ensure_installed", _fail("ensure_installed"))
    monkeypatch.setattr(oh.threading, "Thread", _fail("Thread"))
    # Must return cleanly without touching install or threads.
    oh.start_host(sp_creds={"client_id": "x", "client_secret": "y", "host": "h"})
    assert oh.get_status()["stage"] == "idle"


def test_start_host_legacy_noop_without_env(monkeypatch):
    oh.reset_for_tests()
    monkeypatch.delenv("OMNIGENTS_SERVER_URL", raising=False)
    monkeypatch.setattr(oh, "connect_host", _fail("connect_host"))
    oh.start_host({"client_id": "c", "client_secret": "s", "host": "https://h"})
    assert oh.get_status()["stage"] == "idle"


def test_start_host_refuses_without_sp_creds(monkeypatch):
    """Enabled but PAT-only (no SP creds) -> host NOT started (FR-4 guard)."""
    oh.reset_for_tests()
    monkeypatch.setenv("OMNIGENTS_SERVER_URL", "https://srv.example.com")
    monkeypatch.setattr(oh, "ensure_installed", _fail("ensure_installed"))
    monkeypatch.setattr(oh.threading, "Thread", _fail("Thread"))
    oh.start_host(sp_creds=None)  # must return without installing or spawning


def test_status_initially_idle(monkeypatch):
    monkeypatch.delenv("OMNIGENTS_SERVER_URL", raising=False)
    oh.reset_for_tests()
    status = oh.get_status()
    assert status["configured"] is False
    assert status["running"] is False
    assert status["server_url"] is None
    assert status["stage"] == "idle"


def test_connect_requires_server_url():
    oh.reset_for_tests()
    ok, status = oh.connect_host(
        " ",
        sp_creds={"client_id": "c", "client_secret": "s", "host": "https://h"},
    )
    assert ok is False
    assert status["stage"] == "invalid_server_url"


def test_connect_requires_sp_creds():
    oh.reset_for_tests()
    ok, status = oh.connect_host("https://omnigent.example.com", sp_creds=None)
    assert ok is False
    assert status["stage"] == "no_sp_creds"


def test_connect_starts_supervisor_thread(monkeypatch):
    oh.reset_for_tests()
    monkeypatch.setattr(oh, "ensure_installed", lambda sp_creds=None: True)
    monkeypatch.setattr(oh, "_write_oauth_profile", lambda creds: None)
    monkeypatch.setattr(oh, "_run_host_once", lambda server_url, stop_event=None: 0)

    started = []

    class FakeThread:
        def __init__(self, target, args, daemon, name):
            self.target = target
            self.args = args
            self.daemon = daemon
            self.name = name

        def start(self):
            started.append(self.name)

    monkeypatch.setattr(oh.threading, "Thread", FakeThread)
    ok, status = oh.connect_host(
        "https://omnigent.example.com",
        sp_creds={"client_id": "c", "client_secret": "s", "host": "https://h"},
    )
    assert ok is True
    assert started == ["omnigent-host"]
    assert status["server_url"] == "https://omnigent.example.com"
    assert status["stage"] == "starting"


def test_connect_rejects_duplicate_running_host(monkeypatch):
    oh.reset_for_tests()

    class FakeThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

    monkeypatch.setattr(oh.threading, "Thread", FakeThread)
    creds = {"client_id": "c", "client_secret": "s", "host": "https://h"}
    ok, _status = oh.connect_host("https://one.example.com", creds)
    assert ok is True

    ok, status = oh.connect_host("https://two.example.com", creds)
    assert ok is False
    assert status["last_error"] == "host already running"


def test_disconnect_stops_running_process(monkeypatch):
    oh.reset_for_tests()

    class FakeProc:
        pid = 1234
        returncode = None
        terminated = False

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

    proc = FakeProc()
    monkeypatch.setattr(oh, "_proc", proc)
    oh._set(
        configured=True,
        running=True,
        server_url="https://srv.example.com",
        stage="running",
        pid=1234,
    )

    status = oh.disconnect_host()
    assert proc.terminated is True
    assert status["running"] is False
    assert status["pid"] is None
    assert status["stage"] == "stopped"


def test_capture_sp_credentials_returns_none_when_absent(monkeypatch):
    for v in ("DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET", "DATABRICKS_HOST"):
        monkeypatch.delenv(v, raising=False)
    assert oh.capture_sp_credentials() is None


def test_capture_sp_credentials_returns_creds_when_present(monkeypatch):
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "cid")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "secret")
    monkeypatch.setenv("DATABRICKS_HOST", "https://host.example.com")
    creds = oh.capture_sp_credentials()
    assert creds == {
        "client_id": "cid",
        "client_secret": "secret",
        "host": "https://host.example.com",
    }


def test_capture_sp_credentials_adds_https_scheme(monkeypatch):
    """Bare host (as Databricks Apps injects it) gets https:// — config requires it."""
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "cid")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "secret")
    monkeypatch.setenv("DATABRICKS_HOST", "adb-123.azuredatabricks.net")
    creds = oh.capture_sp_credentials()
    assert creds is not None
    assert creds["host"] == "https://adb-123.azuredatabricks.net"


def test_write_oauth_profile_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    creds = {"client_id": "cid", "client_secret": "sec", "host": "https://h"}
    oh._write_oauth_profile(creds)
    oh._write_oauth_profile(creds)  # second call must not duplicate the block
    cfg = (tmp_path / ".databrickscfg").read_text()
    assert cfg.count(f"[{oh._HOST_PROFILE}]") == 1
    assert "client_id = cid" in cfg


def test_run_host_once_prepends_local_bin_to_path(monkeypatch, tmp_path):
    oh.reset_for_tests()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("DATABRICKS_APP_NAME", "coda")
    monkeypatch.setenv("DATABRICKS_HOST", "https://ambient.example.com")
    monkeypatch.setattr(oh, "_omnigents_bin", lambda: "/bin/omnigent")
    monkeypatch.setattr(
        oh,
        "_sp_creds",
        {"client_id": "793257c7-63d3-464f-b6fb-3bc11880bf2d"},
    )

    captured = {}

    class FakeProc:
        pid = 1234
        stdout = None

        def poll(self):
            return 0

        def wait(self):
            return 0

    def fake_popen(cmd, stdout, stderr, text, cwd, env):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["env"] = env
        return FakeProc()

    monkeypatch.setattr(oh.subprocess, "Popen", fake_popen)

    assert oh._run_host_once("https://omnigent.example.com") == 0

    env = captured["env"]
    expected_host_id = (
        "host_"
        + hashlib.sha256(
            b"coda-omnigents-host:793257c7-63d3-464f-b6fb-3bc11880bf2d"
        ).hexdigest()[:32]
    )
    assert captured["cwd"] == str(tmp_path)
    assert env["PATH"].split(":")[0] == str(tmp_path / ".local" / "bin")
    assert env["OMNIGENT_HOST_ID"] == expected_host_id
    assert env["OMNIGENT_HOST_NAME"] == "coda"
    assert env["DATABRICKS_CONFIG_PROFILE"] == oh._HOST_PROFILE
    assert "DATABRICKS_HOST" not in env


def _fail(name):
    def _raise(*a, **k):
        raise AssertionError(f"{name} should not be called")

    return _raise


def test_run_setup_once_feeds_quit_input(monkeypatch):
    """setup must run non-interactively: feed 'q' so it adopts ambient creds
    and exits cleanly instead of blocking on the harness menu."""
    calls = {}

    class FakeResult:
        returncode = 0
        stdout = "Found existing credentials ... auto-configured for omnigent: Claude"
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        calls["input"] = kwargs.get("input")
        calls["timeout"] = kwargs.get("timeout")
        return FakeResult()

    monkeypatch.setattr(oh.subprocess, "run", fake_run)
    oh._run_setup_once()

    assert calls["cmd"][-1] == "setup"
    assert calls["input"] == "q\n"
    assert calls["timeout"] and calls["timeout"] > 0


def test_run_setup_once_swallows_failure(monkeypatch):
    """A setup failure must not propagate — the host must still launch."""

    def boom(*a, **k):
        raise RuntimeError("setup blew up")

    monkeypatch.setattr(oh.subprocess, "run", boom)
    oh._run_setup_once()  # must not raise


# ── Liveness watchdog ────────────────────────────────
# A hung tunnel (half-open WebSocket after a token rollover) leaves the host
# process alive but silent, so the supervisor's restart loop never fires. The
# watchdog terminates a process that has produced no output past the timeout;
# that termination is what lets the supervisor restart it with fresh auth.


def test_is_stalled_true_past_timeout():
    """No output for longer than the timeout -> stalled."""
    now = 1000.0
    last = now - oh._WATCHDOG_TIMEOUT_SECONDS - 1
    assert oh._is_stalled(last, now) is True


def test_is_stalled_false_within_timeout():
    """Recent output -> not stalled."""
    now = 1000.0
    last = now - 1  # one second ago
    assert oh._is_stalled(last, now) is False


def test_is_stalled_exactly_at_timeout_is_not_stalled():
    """Boundary: exactly at the timeout is still alive (strict >)."""
    now = 1000.0
    last = now - oh._WATCHDOG_TIMEOUT_SECONDS
    assert oh._is_stalled(last, now) is False


def test_watchdog_terminates_stalled_process(monkeypatch):
    """A live-but-silent process is terminated so the supervisor can restart it."""
    terminated = {"called": False}

    class FakeProc:
        def poll(self):
            return None  # still running

        def terminate(self):
            terminated["called"] = True

    # Pin the timeout tiny and the clock past it so the check fires immediately.
    monkeypatch.setattr(oh, "_WATCHDOG_TIMEOUT_SECONDS", 0.01)
    last_output = {"at": oh.time.monotonic() - 1.0}
    stop = oh.threading.Event()

    oh._watchdog(FakeProc(), last_output, stop)
    assert terminated["called"] is True


def test_watchdog_leaves_healthy_process_alone(monkeypatch):
    """A process producing recent output is never terminated."""

    class FakeProc:
        def __init__(self):
            self._polls = 0

        def poll(self):
            # Exit after a couple of ticks so the watchdog loop ends.
            self._polls += 1
            return None if self._polls < 3 else 0

        def terminate(self):
            raise AssertionError("healthy process must not be terminated")

    monkeypatch.setattr(oh, "_WATCHDOG_TIMEOUT_SECONDS", 100.0)
    monkeypatch.setattr(oh, "_WATCHDOG_POLL_SECONDS", 0.001)
    last_output = {"at": oh.time.monotonic()}  # just now
    stop = oh.threading.Event()

    oh._watchdog(FakeProc(), last_output, stop)  # must not raise


# ── _configure_omnigent_databricks_auth (runner native-Pi/Claude/Codex fix) ──

def test_configure_omnigent_auth_writes_databricks_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    oh._configure_omnigent_databricks_auth()
    cfg = yaml.safe_load((tmp_path / ".omnigent" / "config.yaml").read_text())
    assert cfg["auth"] == {"type": "databricks", "profile": oh._HOST_PROFILE}


def test_configure_omnigent_auth_preserves_other_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_dir = tmp_path / ".omnigent"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text(yaml.safe_dump({
        "default_agent": "examples/hello.yaml",
        "server": "https://srv.example.com",
        "auth": {"type": "api_key", "api_key": "env-adopted"},  # what setup wrote
    }))
    oh._configure_omnigent_databricks_auth()
    cfg = yaml.safe_load((cfg_dir / "config.yaml").read_text())
    # auth is upgraded to the databricks profile...
    assert cfg["auth"] == {"type": "databricks", "profile": oh._HOST_PROFILE}
    # ...without clobbering setup's other keys.
    assert cfg["default_agent"] == "examples/hello.yaml"
    assert cfg["server"] == "https://srv.example.com"


def test_configure_omnigent_auth_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    oh._configure_omnigent_databricks_auth()
    first = (tmp_path / ".omnigent" / "config.yaml").read_text()
    oh._configure_omnigent_databricks_auth()  # second run must not error/churn
    second = (tmp_path / ".omnigent" / "config.yaml").read_text()
    assert first == second
