"""Tests for omnigents_host — the Omnigents agent-host integration.

Covers the off-by-default contract (AC-6) and the credential guards: the
feature must be inert with no config, and must refuse to start the host
without the OAuth-capable SP creds (a PAT alone is rejected by the Apps proxy).
"""

from __future__ import annotations

import hashlib
import os
import shlex
import sys

import psutil
import pytest
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


def test_lease_is_user_scoped_and_same_owner_adopts_existing() -> None:
    oh.reset_for_tests()
    ok, first = oh.acquire_lease("alice@example.com", "lease-a")
    assert ok is True
    ok, adopted = oh.acquire_lease("alice@example.com", "lease-b")
    assert ok is True
    assert adopted["lease_id"] == first["lease_id"] == "lease-a"
    ok, _ = oh.acquire_lease("bob@example.com", "lease-c")
    assert ok is False
    assert oh.release_lease("stale") is False
    assert oh.release_lease("lease-a") is True


def test_allocate_workspace_is_fenced_and_distinct(monkeypatch, tmp_path) -> None:
    oh.reset_for_tests()
    monkeypatch.setenv("HOME", str(tmp_path))
    oh.acquire_lease("alice@example.com", "lease-a")
    one = oh.allocate_workspace("lease-a", "session_one")
    two = oh.allocate_workspace("lease-a", "session_two")
    assert one != two
    assert os.path.isdir(one)
    assert os.path.isdir(two)
    with pytest.raises(ValueError, match="stale lease"):
        oh.allocate_workspace("lease-old", "session_three")


def test_idle_lease_releases_after_no_runner_window(monkeypatch) -> None:
    oh.reset_for_tests()
    oh.acquire_lease("alice@example.com", "lease-a")
    monkeypatch.setattr(oh, "disconnect_host", lambda: {})
    assert oh.release_idle_lease(now=100.0, runner_count=0) is False
    assert oh.release_idle_lease(now=699.0, runner_count=0) is False
    assert oh.release_idle_lease(now=700.0, runner_count=0) is True
    assert oh.active_lease() is None


class _FakeRunnerProcess:
    def __init__(self, pid, cmdline, ppid, status=psutil.STATUS_RUNNING, running=True):
        self.pid = pid
        self._cmdline = cmdline
        self._ppid = ppid
        self._status = status
        self._running = running

    def status(self):
        return self._status

    def is_running(self):
        return self._running

    def cmdline(self):
        return self._cmdline

    def ppid(self):
        return self._ppid


def _patch_process_tree(monkeypatch, children):
    class _HostProcess:
        pid = 123

        def poll(self):
            return None

    class _Process:
        def __init__(self, pid):
            assert pid == 123

        def children(self, recursive):
            assert recursive is True
            return children

    monkeypatch.setattr(psutil, "Process", _Process)
    monkeypatch.setattr(oh, "_proc", _HostProcess())


def test_direct_runner_prevents_idle_lease_release(monkeypatch) -> None:
    oh.reset_for_tests()
    oh.acquire_lease("alice@example.com", "lease-a")
    _patch_process_tree(
        monkeypatch,
        [_FakeRunnerProcess(201, ["python", "-m", "omnigent.runner._entry"], 123)],
    )
    monkeypatch.setattr(oh, "disconnect_host", lambda: (_ for _ in ()).throw(AssertionError()))

    assert oh._live_runner_count() == 1
    assert oh.release_idle_lease(now=100.0) is False
    assert oh.release_idle_lease(now=700.0) is False
    assert oh.active_lease() is not None


def test_zygote_and_forked_runner_keep_lease_alive(monkeypatch) -> None:
    oh.reset_for_tests()
    oh.acquire_lease("alice@example.com", "lease-a")
    _patch_process_tree(
        monkeypatch,
        [
            _FakeRunnerProcess(200, ["python", "-m", "omnigent.runner._zygote"], 123),
            # A fork may inherit the zygote's exact command line; parentage
            # distinguishes the runner from the infrastructure process.
            _FakeRunnerProcess(201, ["python", "-m", "omnigent.runner._zygote"], 200),
        ],
    )
    monkeypatch.setattr(oh, "disconnect_host", lambda: (_ for _ in ()).throw(AssertionError()))

    assert oh._live_runner_count() == 1
    assert oh.release_idle_lease(now=100.0) is False
    assert oh.release_idle_lease(now=700.0) is False


def test_zygote_alone_allows_idle_lease_release(monkeypatch) -> None:
    oh.reset_for_tests()
    oh.acquire_lease("alice@example.com", "lease-a")
    _patch_process_tree(
        monkeypatch,
        [_FakeRunnerProcess(200, ["python", "-m", "omnigent.runner._zygote"], 123)],
    )
    monkeypatch.setattr(oh, "disconnect_host", lambda: {})

    assert oh._live_runner_count() == 0
    assert oh.release_idle_lease(now=100.0) is False
    assert oh.release_idle_lease(now=700.0) is True
    assert oh.active_lease() is None


def test_zombie_and_dead_descendants_are_ignored(monkeypatch) -> None:
    oh.reset_for_tests()
    _patch_process_tree(
        monkeypatch,
        [
            _FakeRunnerProcess(
                201,
                ["python", "-m", "omnigent.runner"],
                123,
                status=psutil.STATUS_ZOMBIE,
            ),
            _FakeRunnerProcess(
                202,
                ["python", "-m", "omnigent.runner"],
                123,
                status=getattr(psutil, "STATUS_DEAD", "dead"),
                running=False,
            ),
        ],
    )

    assert oh._live_runner_count() == 0


def test_runner_inspection_failure_resets_armed_idle_timer(monkeypatch, caplog) -> None:
    oh.reset_for_tests()
    oh.acquire_lease("alice@example.com", "lease-a")

    # Arm the timer, then make inspection unknown after the threshold. The
    # unknown result must reset the timer rather than release the lease.
    assert oh.release_idle_lease(now=100.0, runner_count=0) is False

    class _DeniedRunner(_FakeRunnerProcess):
        def status(self):
            raise psutil.AccessDenied(self.pid)

    _patch_process_tree(
        monkeypatch,
        [_DeniedRunner(201, ["python", "-m", "omnigent.runner._entry"], 123)],
    )
    monkeypatch.setattr(oh, "disconnect_host", lambda: {})

    assert oh.release_idle_lease(now=700.0) is False
    assert oh.active_lease() is not None
    assert "preserving lease" in caplog.text

    # A later definitive zero starts a fresh idle window; it does not inherit
    # the pre-failure timer and release immediately.
    _patch_process_tree(monkeypatch, [])
    assert oh.release_idle_lease(now=800.0) is False
    assert oh.release_idle_lease(now=1399.0) is False
    assert oh.release_idle_lease(now=1400.0) is True
    assert oh.active_lease() is None


def test_root_disappearing_during_process_inspection_preserves_lease(monkeypatch, caplog) -> None:
    oh.reset_for_tests()
    oh.acquire_lease("alice@example.com", "lease-a")

    class _Process:
        def __init__(self, pid):
            assert pid == 123

        def children(self, recursive):
            assert recursive is True
            raise psutil.NoSuchProcess(123)

    class _HostProcess:
        pid = 123

        def poll(self):
            return None

    monkeypatch.setattr(psutil, "Process", _Process)
    monkeypatch.setattr(oh, "_proc", _HostProcess())
    monkeypatch.setattr(oh, "disconnect_host", lambda: (_ for _ in ()).throw(AssertionError()))

    assert oh._live_runner_count() is None
    assert oh.release_idle_lease(now=100.0) is False
    assert oh.release_idle_lease(now=700.0) is False
    assert oh.active_lease() is not None
    assert "preserving lease" in caplog.text


def test_managed_mode_skips_legacy_boot_registration(monkeypatch) -> None:
    oh.reset_for_tests()
    monkeypatch.setenv("CODA_OMNIGENT_MODE", "managed")
    monkeypatch.setenv("OMNIGENTS_SERVER_URL", "https://omnigent.example.com")
    monkeypatch.setattr(oh, "connect_host", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError))
    oh.start_host({"client_id": "id"})
    assert oh.get_status()["stage"] == "idle"


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
    oh._set(configured=True, running=True, server_url="https://srv.example.com", stage="running", pid=1234)

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


def test_write_oauth_profile_contains_host_but_no_long_lived_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    creds = {"client_id": "cid", "client_secret": "sec", "host": "https://h"}
    oh._write_oauth_profile(creds)
    oh._write_oauth_profile(creds)  # second call must not duplicate the block
    cfg = (tmp_path / ".databrickscfg").read_text()
    assert cfg.count(f"[{oh._HOST_PROFILE}]") == 1
    assert "host = https://h" in cfg
    # databricks-cli auth type + databricks_cli_path route
    # Config(profile=...).authenticate() through the broker CLI shim (by its
    # absolute path, since the SDK ignores $PATH), so omnigent's model-catalog
    # fetch can mint a token and pi shows the full workspace model list.
    assert "auth_type = databricks-cli" in cfg
    assert "databricks_cli_path = " in cfg
    assert ".coda-broker-bin/databricks" in cfg
    assert "client_id" not in cfg
    assert "client_secret" not in cfg
    assert "token" not in cfg


def test_write_oauth_profile_refreshes_rotated_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg_path = tmp_path / ".databrickscfg"
    cfg_path.write_text("[DEFAULT]\ntoken = dapi-user\n")

    oh._write_oauth_profile(
        {"client_id": "old-id", "client_secret": "old-secret", "host": "https://old"}
    )
    oh._write_oauth_profile(
        {"client_id": "new-id", "client_secret": "new-secret", "host": "https://new"}
    )

    cfg = cfg_path.read_text()
    assert "token = dapi-user" in cfg
    assert "host = https://new" in cfg
    assert "old-secret" not in cfg
    assert "new-secret" not in cfg
    assert cfg.count(f"[{oh._HOST_PROFILE}]") == 1


def test_run_host_once_prepends_local_bin_to_path(monkeypatch, tmp_path):
    oh.reset_for_tests()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("DATABRICKS_APP_NAME", "coda")
    monkeypatch.setenv("DATABRICKS_HOST", "https://ambient.example.com")
    monkeypatch.setattr(oh, "_omnigents_bin", lambda: "/bin/omnigent")
    monkeypatch.setattr(oh, "fetch_sp_token", lambda: "short-lived-token")
    monkeypatch.setattr(
        oh,
        "_sp_creds",
        {
            "client_id": "793257c7-63d3-464f-b6fb-3bc11880bf2d",
            "host": "https://ambient.example.com",
        },
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
    expected_host_id = "host_" + hashlib.sha256(
        b"coda-omnigents-host:793257c7-63d3-464f-b6fb-3bc11880bf2d"
    ).hexdigest()[:32]
    assert captured["cwd"] == str(tmp_path)
    assert env["PATH"].split(":")[:2] == [
        str(tmp_path / ".coda-broker-bin"),
        str(tmp_path / ".local" / "bin"),
    ]
    assert env["OMNIGENT_HOST_ID"] == expected_host_id
    assert env["OMNIGENT_HOST_NAME"] == "coda"
    assert env["DATABRICKS_TOKEN"] == "short-lived-token"
    assert env["DATABRICKS_HOST"] == "https://ambient.example.com"
    assert env["OMNIGENT_DATABRICKS_TOKEN_COMMAND"] == (
        shlex.join([sys.executable, str(tmp_path / ".claude" / "anthropic-token-helper.py")])
    )
    assert "DATABRICKS_CONFIG_PROFILE" not in env
    assert "DATABRICKS_CLIENT_SECRET" not in env


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


# ── _configure_omnigent_databricks_auth (runner native-Pi/Claude/Codex fix) ──

def test_configure_omnigent_auth_writes_databricks_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    oh._configure_omnigent_databricks_auth()
    cfg = yaml.safe_load((tmp_path / ".omnigent" / "config.yaml").read_text())
    assert cfg["auth"] == {"type": "databricks", "profile": oh._HOST_PROFILE}
    assert cfg["providers"]["coda-databricks"] == {
        "kind": "databricks",
        "default": True,
        "profile": oh._HOST_PROFILE,
    }


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


# ── _materialize_spec: flat + nested (versioned) UC Volume wheel layouts ──────

class _FakeEntry:
    def __init__(self, path, is_directory=False, last_modified=0):
        self.path = path
        self.name = path.rstrip("/").split("/")[-1]
        self.is_directory = is_directory
        self.last_modified = last_modified


class _FakeFiles:
    """Minimal stand-in for w.files with a canned directory tree."""

    def __init__(self, tree, blobs):
        self._tree = tree      # dir path -> list[_FakeEntry]
        self._blobs = blobs    # file path -> bytes

    def list_directory_contents(self, path):
        return list(self._tree.get(path.rstrip("/"), []))

    def download(self, path):
        import io
        class _Resp:
            def __init__(self, data):
                self.contents = io.BytesIO(data)
        return _Resp(self._blobs[path])


class _FakeWC:
    def __init__(self, files):
        self.files = files


def _install_fake_wc(monkeypatch, files):
    import databricks.sdk as sdk
    monkeypatch.setattr(sdk, "WorkspaceClient", lambda *a, **k: _FakeWC(files))


def test_materialize_spec_flat_top_level(monkeypatch, tmp_path):
    """Wheels sitting flat at the volume root still resolve (back-compat)."""
    vol = "/Volumes/<catalog>/<schema>/<volume>"
    tree = {vol: [
        _FakeEntry(f"{vol}/omnigent-0.6.0-py3-none-any.whl", last_modified=100),
        _FakeEntry(f"{vol}/omnigent_client-0.6.0-py3-none-any.whl", last_modified=100),
    ]}
    blobs = {e.path: b"whl" for e in tree[vol]}
    _install_fake_wc(monkeypatch, _FakeFiles(tree, blobs))
    out = oh._materialize_spec(vol, sp_creds=None)
    got = sorted(__import__("os").listdir(out))
    assert got == ["omnigent-0.6.0-py3-none-any.whl", "omnigent_client-0.6.0-py3-none-any.whl"]


def test_materialize_spec_nested_versioned_picks_newest(monkeypatch, tmp_path):
    """No wheels at root -> recurse; pick the newest-uploaded version dir."""
    vol = "/Volumes/<catalog>/<schema>/<volume>"
    old = f"{vol}/wheels/0.6.0.post1"
    new = f"{vol}/wheels/0.6.0.post2"
    tree = {
        vol: [_FakeEntry(f"{vol}/wheels", is_directory=True)],
        f"{vol}/wheels": [
            _FakeEntry(old, is_directory=True),
            _FakeEntry(new, is_directory=True),
        ],
        old: [_FakeEntry(f"{old}/omnigent-0.6.0.post1-py3-none-any.whl", last_modified=100)],
        new: [_FakeEntry(f"{new}/omnigent-0.6.0.post2-py3-none-any.whl", last_modified=200)],
    }
    blobs = {
        f"{old}/omnigent-0.6.0.post1-py3-none-any.whl": b"old",
        f"{new}/omnigent-0.6.0.post2-py3-none-any.whl": b"new",
    }
    _install_fake_wc(monkeypatch, _FakeFiles(tree, blobs))
    out = oh._materialize_spec(vol, sp_creds=None)
    got = __import__("os").listdir(out)
    # Only the newest version's wheel is materialized.
    assert got == ["omnigent-0.6.0.post2-py3-none-any.whl"]


def test_materialize_spec_no_wheels_raises(monkeypatch):
    vol = "/Volumes/<catalog>/<schema>/<volume>"
    tree = {vol: [_FakeEntry(f"{vol}/readme.txt")]}
    _install_fake_wc(monkeypatch, _FakeFiles(tree, {}))
    import pytest
    with pytest.raises(FileNotFoundError):
        oh._materialize_spec(vol, sp_creds=None)
