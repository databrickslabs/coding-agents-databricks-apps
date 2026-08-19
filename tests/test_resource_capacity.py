"""Deterministic cgroup admission and browser-capacity tests.

Covers the fixed 10/11 browser boundary, concurrent POST races, the
pre-fork and post-fork rejection paths (including child/fd cleanup),
malformed configuration, every cgroup telemetry shape, the reserve and
hysteresis boundaries, and the response contract (legacy ``error`` string
plus the structured ``code``).
"""

import importlib
import os
import signal
import threading
from pathlib import Path
from unittest import mock

import pytest

import resource_capacity as rc

MiB = 1024 * 1024


def _cgroup(tmp_path: Path, current: str, maximum: str):
    (tmp_path / "memory.current").write_text(current)
    (tmp_path / "memory.max").write_text(maximum)
    return lambda: rc.read_cgroup_memory(tmp_path)


def _reload_app():
    """Reload ``app`` with boot-time initialization stubbed out."""
    with mock.patch("app.initialize_app"):
        import app

        return importlib.reload(app)


@pytest.fixture(autouse=True)
def _restore_app_module():
    """Undo this module's global mutations for every other test module.

    ``importlib.reload`` mutates the shared ``app`` module in place, so
    pinning ``MAX_CONCURRENT_SESSIONS`` / ``_browser_capacity`` here would
    otherwise leak into suites that assert the defaults.
    """
    yield
    module = _reload_app()
    with module.sessions_lock:
        module.sessions.clear()


def _fresh_app(limit: int = 2, controller: rc.BrowserCapacityController | None = None):
    """Reload ``app`` with initialization stubbed, then pin capacity wiring."""
    module = _reload_app()
    module.app.config["TESTING"] = True
    module.MAX_CONCURRENT_SESSIONS = limit
    module._browser_capacity = controller or rc.BrowserCapacityController(
        memory_reader=lambda: rc.CgroupMemory(None, None, False)
    )
    with module.sessions_lock:
        module.sessions.clear()
    return module


def test_invalid_or_non_positive_browser_limit_falls_back(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_SESSIONS", "not-a-number")
    module = _reload_app()
    assert module.MAX_CONCURRENT_SESSIONS == 5

    monkeypatch.setenv("MAX_CONCURRENT_SESSIONS", "0")
    module = _reload_app()
    assert module.MAX_CONCURRENT_SESSIONS == 1


# ── cgroup telemetry ─────────────────────────────────────────


def test_cgroup_finite_max_and_unavailable(tmp_path):
    finite = rc.read_cgroup_memory(tmp_path)
    assert finite.available is False
    reader = _cgroup(tmp_path, "1048576\n", "4194304\n")
    value = reader()
    assert value.available and value.used_bytes == 1048576
    assert value.limit_bytes == 4194304
    assert value.percent == 25.0

    _cgroup(tmp_path, "1048576", "max")
    unlimited = rc.read_cgroup_memory(tmp_path)
    assert unlimited.available is True
    assert unlimited.limit_bytes is None
    assert unlimited.percent is None

    (tmp_path / "memory.current").write_text("not-a-number")
    assert rc.read_cgroup_memory(tmp_path).available is False


def test_cgroup_rejects_zero_and_negative_limits(tmp_path):
    """A zero/negative limit cannot support percentages, so it reads unusable."""
    _cgroup(tmp_path, "100", "0")
    assert rc.read_cgroup_memory(tmp_path).available is False
    _cgroup(tmp_path, "-1", "1000")
    assert rc.read_cgroup_memory(tmp_path).available is False


def test_cgroup_root_env_is_honored(tmp_path, monkeypatch):
    """The cgroup root is configurable so tests and sidecars can redirect it."""
    _cgroup(tmp_path, "500", "1000")
    monkeypatch.setenv(rc.CGROUP_ROOT_ENV, str(tmp_path))
    reading = rc.read_cgroup_memory()
    assert reading.available and reading.percent == 50.0


def _cgroup_v1(base: Path, usage: str, limit: str) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / "memory.usage_in_bytes").write_text(usage)
    (base / "memory.limit_in_bytes").write_text(limit)


def test_cgroup_v1_is_read_when_v2_is_absent(tmp_path):
    """Databricks Apps containers expose cgroup v1, so v1 must work.

    Without it the memory gate is permanently inactive in production and
    only the fixed session cap protects the container.
    """
    _cgroup_v1(tmp_path / "memory", str(3 * MiB), str(8 * MiB))
    reading = rc.read_cgroup_memory(tmp_path)
    assert reading.available and reading.used_bytes == 3 * MiB
    assert reading.limit_bytes == 8 * MiB
    assert reading.percent == 37.5


def test_cgroup_v1_files_may_sit_at_the_root(tmp_path):
    _cgroup_v1(tmp_path, str(1 * MiB), str(4 * MiB))
    reading = rc.read_cgroup_memory(tmp_path)
    assert reading.available and reading.percent == 25.0


def test_cgroup_v2_wins_over_v1(tmp_path):
    _cgroup(tmp_path, str(10 * MiB), str(100 * MiB))
    _cgroup_v1(tmp_path / "memory", str(90 * MiB), str(200 * MiB))
    reading = rc.read_cgroup_memory(tmp_path)
    assert reading.used_bytes == 10 * MiB and reading.limit_bytes == 100 * MiB


def test_cgroup_v1_unlimited_sentinel_has_no_percentage(tmp_path):
    """A 2^63-ish limit is the kernel's "unlimited", not a real budget.

    Reporting it as a budget would show a container as ~0% used and
    silently disable the memory gate.
    """
    _cgroup_v1(tmp_path / "memory", str(3 * MiB), "9223372036854771712")
    reading = rc.read_cgroup_memory(tmp_path)
    assert reading.available is True
    assert reading.limit_bytes is None
    assert reading.percent is None
    controller = rc.BrowserCapacityController(
        memory_reader=lambda: rc.read_cgroup_memory(tmp_path)
    )
    rejected = controller.evaluate(10, 10)
    assert not rejected.allowed and rejected.state == "unavailable"


def test_cgroup_v1_malformed_reads_as_unavailable(tmp_path):
    _cgroup_v1(tmp_path / "memory", "not-a-number", str(8 * MiB))
    assert rc.read_cgroup_memory(tmp_path).available is False
    _cgroup_v1(tmp_path / "memory", str(3 * MiB), "0")
    assert rc.read_cgroup_memory(tmp_path).available is False


def test_working_set_excludes_reclaimable_cache(tmp_path):
    """Usage is the working set, not usage-including-page-cache."""
    _cgroup(tmp_path, str(80 * MiB), str(100 * MiB))
    (tmp_path / "memory.stat").write_text(f"anon 1\ninactive_file {30 * MiB}\n")
    assert rc.read_cgroup_memory(tmp_path).used_bytes == 50 * MiB
    (tmp_path / "memory.stat").write_text("inactive_file bogus\n")
    assert rc.read_cgroup_memory(tmp_path).used_bytes == 80 * MiB
    (tmp_path / "memory.stat").unlink()
    assert rc.read_cgroup_memory(tmp_path).used_bytes == 80 * MiB

    base = tmp_path / "v1" / "memory"
    _cgroup_v1(base, str(80 * MiB), str(100 * MiB))
    (base / "memory.stat").write_text(f"total_inactive_file {25 * MiB}\n")
    assert rc.read_cgroup_memory(tmp_path / "v1").used_bytes == 55 * MiB


def test_working_set_excludes_active_file_cache(tmp_path):
    """Active file cache is reclaimable too, so it is not pressure.

    A warm container parks GBs in ``active_file`` (cloned repos, agent CLI
    installs, caches).  Counting it refused every new browser session on a
    container whose anonymous memory was a small fraction of the budget.
    """
    _cgroup(tmp_path, str(90 * MiB), str(100 * MiB))
    (tmp_path / "memory.stat").write_text(
        f"anon {20 * MiB}\ninactive_file {10 * MiB}\nactive_file {60 * MiB}\n"
    )
    assert rc.read_cgroup_memory(tmp_path).used_bytes == 20 * MiB

    # v1 subtree-inclusive counters win over the local ones.
    base = tmp_path / "v1" / "memory"
    _cgroup_v1(base, str(90 * MiB), str(100 * MiB))
    (base / "memory.stat").write_text(
        f"inactive_file {1 * MiB}\nactive_file {1 * MiB}\n"
        f"total_inactive_file {10 * MiB}\ntotal_active_file {60 * MiB}\n"
    )
    assert rc.read_cgroup_memory(tmp_path / "v1").used_bytes == 20 * MiB


def test_shmem_still_counts_as_pressure(tmp_path):
    """tmpfs pages are swap-backed, not reclaimable file cache."""
    _cgroup(tmp_path, str(90 * MiB), str(100 * MiB))
    (tmp_path / "memory.stat").write_text(
        f"anon {20 * MiB}\nshmem {60 * MiB}\ninactive_file {10 * MiB}\n"
    )
    assert rc.read_cgroup_memory(tmp_path).used_bytes == 80 * MiB


def test_warm_container_with_low_anon_admits_sessions(tmp_path):
    """Regression: 12 GiB container, 7.5 GiB page cache, 1.9 GiB anon.

    The reported cgroup usage was 85% of the limit, so a single-session
    container reported "memory full" and refused every new browser session.
    """
    limit = 12 * 1024 * MiB
    active_file, inactive_file, anon = 6240 * MiB, 1250 * MiB, 1950 * MiB
    _cgroup_v1(tmp_path / "memory", str(active_file + inactive_file + anon), str(limit))
    (tmp_path / "memory" / "memory.stat").write_text(
        f"total_inactive_file {inactive_file}\ntotal_active_file {active_file}\n"
        f"total_rss {anon}\n"
    )
    controller = rc.BrowserCapacityController(
        memory_reader=lambda: rc.read_cgroup_memory(tmp_path)
    )
    decision = controller.evaluate(1, 10)
    assert decision.allowed and decision.state == "normal"
    assert decision.memory.percent < 20


def test_never_falls_back_to_host_wide_memory(monkeypatch):
    """With no cgroup files the controller must express NO memory opinion.

    Host-wide memory is not this container's budget, so substituting it
    would either block a healthy app or claim headroom it does not have.
    """
    monkeypatch.setenv(rc.CGROUP_ROOT_ENV, "/definitely/not/a/cgroup")
    controller = rc.controller_from_env()
    decision = controller.evaluate(0, 10)
    assert decision.state == "unavailable"
    assert decision.memory.used_bytes is None
    assert decision.memory.limit_bytes is None
    assert decision.allowed is True


# ── hysteresis and reserve ───────────────────────────────────


def test_reserve_threshold_and_hysteresis(tmp_path):
    reader = _cgroup(tmp_path, "300", "1000")
    controller = rc.BrowserCapacityController(
        high_watermark_percent=80,
        resume_threshold_percent=70,
        reserve_bytes=100,
        memory_reader=reader,
    )
    assert controller.evaluate(0, 2).allowed
    (tmp_path / "memory.current").write_text("750")
    # 750 + 100 would cross 80%, so reject and latch pressure.
    assert controller.evaluate(0, 2).state == "pressured"
    (tmp_path / "memory.current").write_text("710")
    assert controller.evaluate(0, 2).state == "pressured"
    (tmp_path / "memory.current").write_text("700")
    assert controller.evaluate(0, 2).allowed
    assert controller.pressure_state == "normal"


def test_pressure_latch_never_flaps(tmp_path):
    """Set and clear conditions are exact complements, so one evaluation
    cannot clear the latch and immediately re-arm it.

    A flapping latch would admit a session at a usage level the reserve
    check just rejected.
    """
    reader = _cgroup(tmp_path, "850", "1000")
    controller = rc.BrowserCapacityController(
        high_watermark_percent=80,
        resume_threshold_percent=70,
        # A 200-byte reserve needs usage <= 600 to fit under 80% of 1000.
        reserve_bytes=200,
        memory_reader=reader,
    )
    assert controller.evaluate(0, 10).state == "pressured"
    # Below the 70% resume threshold, but the reserve still would not fit.
    (tmp_path / "memory.current").write_text("650")
    assert controller.evaluate(0, 10).state == "pressured"
    assert controller.evaluate(0, 10).state == "pressured"
    # Exactly on the reserve boundary → clears, and stays clear.
    (tmp_path / "memory.current").write_text("600")
    assert controller.evaluate(0, 10).allowed
    assert controller.evaluate(0, 10).allowed


def test_high_watermark_alone_latches_with_zero_reserve(tmp_path):
    reader = _cgroup(tmp_path, "800", "1000")
    controller = rc.BrowserCapacityController(
        high_watermark_percent=80,
        resume_threshold_percent=70,
        reserve_bytes=0,
        memory_reader=reader,
    )
    assert controller.evaluate(0, 10).state == "pressured"
    (tmp_path / "memory.current").write_text("700")
    assert controller.evaluate(0, 10).allowed


def test_unlimited_max_falls_back_to_fixed_count(tmp_path):
    """``memory.max == max`` is readable but has no percentage."""
    reader = _cgroup(tmp_path, "1048576", "max")
    controller = rc.BrowserCapacityController(memory_reader=reader)
    assert controller.evaluate(9, 10).allowed
    rejected = controller.evaluate(10, 10)
    assert not rejected.allowed and rejected.state == "unavailable"


def test_unavailable_telemetry_falls_back_to_fixed_count():
    controller = rc.BrowserCapacityController(
        memory_reader=lambda: rc.CgroupMemory(None, None, False)
    )
    assert controller.evaluate(1, 2).allowed
    rejected = controller.evaluate(2, 2)
    assert not rejected.allowed and rejected.state == "unavailable"


def test_controller_rejects_inconsistent_construction():
    with pytest.raises(ValueError):
        rc.BrowserCapacityController(high_watermark_percent=60, resume_threshold_percent=70)
    with pytest.raises(ValueError):
        rc.BrowserCapacityController(high_watermark_percent=101)
    with pytest.raises(ValueError):
        rc.BrowserCapacityController(reserve_bytes=-1)


def test_controller_from_env_reads_canonical_names(monkeypatch):
    monkeypatch.setenv(rc.HIGH_WATERMARK_ENV, "85")
    monkeypatch.setenv(rc.RESUME_THRESHOLD_ENV, "60")
    monkeypatch.setenv(rc.BROWSER_RESERVE_MB_ENV, "256")
    controller = rc.controller_from_env(lambda: rc.CgroupMemory(None, None, False))
    assert controller.high_watermark_percent == 85.0
    assert controller.resume_threshold_percent == 60.0
    assert controller.reserve_bytes == 256 * MiB


@pytest.mark.parametrize(
    "high,resume,reserve",
    [
        ("abc", "70", "768"),  # unparseable watermark
        ("80", "xyz", "768"),  # unparseable resume
        ("80", "70", "nope"),  # unparseable reserve
        ("60", "90", "768"),  # resume above watermark (invalid combination)
        ("80", "70", "-5"),  # negative reserve
    ],
)
def test_controller_from_env_falls_back_on_malformed_config(
    monkeypatch, high, resume, reserve
):
    """A malformed deployment value must never DISABLE the guard.

    Silently constructing an unguarded controller is how a config typo
    turns into an OOM.
    """
    monkeypatch.setenv(rc.HIGH_WATERMARK_ENV, high)
    monkeypatch.setenv(rc.RESUME_THRESHOLD_ENV, resume)
    monkeypatch.setenv(rc.BROWSER_RESERVE_MB_ENV, reserve)
    controller = rc.controller_from_env(lambda: rc.CgroupMemory(None, None, False))
    assert 0 < controller.resume_threshold_percent <= controller.high_watermark_percent <= 100
    assert controller.reserve_bytes >= 0


# ── status projection ────────────────────────────────────────


def test_status_projection_redacts_host_process_details(monkeypatch):
    module = _fresh_app(
        limit=10,
        controller=rc.BrowserCapacityController(
            memory_reader=lambda: rc.CgroupMemory(100, 1000, True),
            reserve_bytes=0,
        ),
    )
    monkeypatch.setattr(module, "_process_tree_rss_mb", lambda: 123)
    with mock.patch.object(module, "check_authorization", return_value=(True, "operator")):
        response = module.app.test_client().get("/api/capacity")
    body = response.get_json()
    assert response.status_code == 200
    assert body["process_tree_rss_mb"] == 123
    assert body["browser_sessions"] == {
        "current": 0,
        "pending": 0,
        "limit": 10,
        "accepting": True,
    }
    assert "pid" not in body and "token" not in str(body).lower()


def test_status_projection_does_not_include_host_details(monkeypatch):
    module = _fresh_app(limit=10)
    with mock.patch.object(module, "check_authorization", return_value=(True, "operator")):
        body = module.app.test_client().get("/api/capacity").get_json()
    assert "runner" not in body


def test_status_projection_marks_telemetry_unavailable():
    """A UI must be able to tell "no memory data" from "memory is fine"."""
    module = _fresh_app(limit=10)
    with mock.patch.object(module, "check_authorization", return_value=(True, "operator")):
        body = module.app.test_client().get("/api/capacity").get_json()
    assert body["telemetry_available"] is False
    assert body["percent"] is None
    assert body["state"] == "unavailable"
    assert body["browser_sessions"]["limit"] == 10


def test_resource_status_alias_matches_capacity():
    module = _fresh_app(limit=10)
    with mock.patch.object(module, "check_authorization", return_value=(True, "operator")):
        client = module.app.test_client()
        first = client.get("/api/capacity").get_json()
        second = client.get("/api/resource-status").get_json()
    assert first["browser_sessions"] == second["browser_sessions"]


# ── browser admission ────────────────────────────────────────


def test_tenth_browser_session_allowed_eleventh_rejected():
    """The configured ceiling of 10 admits exactly ten browser PTYs."""
    module = _fresh_app(limit=10)
    with module.sessions_lock:
        module.sessions.clear()
    # Nine live sessions still leave room for the tenth.
    for i in range(9):
        with module.sessions_lock:
            module.sessions[f"s{i}"] = {"pid": i, "master_fd": i}
    assert module._capacity_decision().allowed is True
    with module.sessions_lock:
        module.sessions["s9"] = {"pid": 9, "master_fd": 9}
    assert module._capacity_decision().allowed is False

    with mock.patch.object(module, "check_authorization", return_value=(True, "operator")), \
         mock.patch("pty.openpty") as openpty:
        # Ten live sessions → the eleventh request is refused pre-fork.
        response = module.app.test_client().post("/api/session", json={})
    assert response.status_code == 429
    body = response.get_json()
    assert body["code"] == "BROWSER_SESSION_LIMIT"
    assert "Maximum 10 concurrent browser sessions" in body["error"]
    assert body["capacity"]["browser_sessions"] == {
        "current": 10,
        "pending": 0,
        "limit": 10,
        "accepting": False,
    }
    openpty.assert_not_called()
    with module.sessions_lock:
        module.sessions.clear()


def test_browser_limit_response_keeps_legacy_error_string():
    """The old browser UI matches on ``error`` containing "Maximum"."""
    module = _fresh_app(limit=1)
    with module.sessions_lock:
        module.sessions["s0"] = {"pid": 1, "master_fd": 3}
    with mock.patch.object(module, "check_authorization", return_value=(True, "operator")):
        body = module.app.test_client().post("/api/session", json={}).get_json()
    assert "Maximum" in body["error"]
    assert body["code"] == "BROWSER_SESSION_LIMIT"
    assert body["message"] == body["error"]
    assert body["retry_guidance"]
    with module.sessions_lock:
        module.sessions.clear()


def test_browser_429_before_fork_on_memory_pressure():
    module = _fresh_app(
        limit=2,
        controller=rc.BrowserCapacityController(
            memory_reader=lambda: rc.CgroupMemory(900, 1000, True),
            high_watermark_percent=80,
            resume_threshold_percent=70,
            reserve_bytes=1,
        ),
    )
    with mock.patch.object(module, "check_authorization", return_value=(True, "operator")), \
         mock.patch("pty.openpty") as openpty:
        response = module.app.test_client().post("/api/session", json={})
    assert response.status_code == 429
    body = response.get_json()
    assert body["code"] == "BROWSER_MEMORY_PRESSURE"
    assert "memory" in body["error"].lower()
    assert body["retry_guidance"]
    openpty.assert_not_called()


def test_browser_429_after_fork_kills_speculative_child():
    module = _fresh_app(limit=2)
    readings = iter((rc.CgroupMemory(100, 1000, True), rc.CgroupMemory(900, 1000, True)))
    module._browser_capacity = rc.BrowserCapacityController(
        memory_reader=lambda: next(readings),
        high_watermark_percent=80,
        resume_threshold_percent=70,
        reserve_bytes=1,
    )
    proc = mock.Mock(pid=4321)
    proc.poll.return_value = 0
    killed = []
    closed = []
    reaped = []
    with mock.patch.object(module, "check_authorization", return_value=(True, "operator")), \
         mock.patch("pty.openpty", return_value=(10, 11)), \
         mock.patch("subprocess.Popen", return_value=proc), \
         mock.patch("os.close", side_effect=lambda fd: closed.append(fd)), \
         mock.patch("os.kill", side_effect=lambda pid, sig: killed.append((pid, sig))), \
         mock.patch("os.waitpid", side_effect=lambda pid, flags: (reaped.append((pid, flags)), (pid, 0))[1]):
        response = module.app.test_client().post("/api/session", json={})
    assert response.status_code == 429
    assert response.get_json()["code"] == "BROWSER_MEMORY_PRESSURE"
    assert killed == [(4321, module.signal.SIGKILL)]
    # The PTY master fd is closed and the child reaped — no fd or zombie leak.
    assert 10 in closed
    assert reaped == [(4321, os.WNOHANG)]
    with module.sessions_lock:
        assert not module.sessions


def test_speculative_cleanup_survives_unreapable_child():
    """Cleanup must not raise when the child cannot be reaped yet.

    ``waitpid(WNOHANG)`` legitimately raises/returns nothing for a child
    that has not finished dying; a raise here would 500 the request and
    leave the PTY fd open.
    """
    module = _fresh_app(limit=2)
    with mock.patch("os.close", side_effect=OSError("already closed")), \
         mock.patch("os.kill", side_effect=OSError("no such process")), \
         mock.patch("os.waitpid", side_effect=ChildProcessError("not reapable")):
        module._kill_speculative_session(1234, 7)  # must not raise


def test_concurrent_browser_creates_never_exceed_the_limit():
    """Ten threads racing a limit of 3 must insert exactly three sessions.

    The authoritative check runs under the same lock as the insertion, so
    the post-fork losers are killed rather than admitted.
    """
    module = _fresh_app(limit=3)
    module._browser_capacity = rc.BrowserCapacityController(
        memory_reader=lambda: rc.CgroupMemory(None, None, False)
    )
    with module.sessions_lock:
        module.sessions.clear()

    attempts = 10
    barrier = threading.Barrier(attempts)
    statuses: list[int] = []
    statuses_lock = threading.Lock()
    fd_counter = iter(range(100, 100 + attempts * 2))
    pid_counter = iter(range(9000, 9000 + attempts))
    killed: list[int] = []

    client = module.app.test_client()

    def _openpty():
        return (next(fd_counter), next(fd_counter))

    def _popen(*_args, **_kwargs):
        proc = mock.Mock(pid=next(pid_counter))
        proc.poll.return_value = None
        return proc

    def attempt():
        barrier.wait()
        response = client.post("/api/session", json={})
        with statuses_lock:
            statuses.append(response.status_code)

    with mock.patch.object(module, "check_authorization", return_value=(True, "operator")), \
         mock.patch("pty.openpty", side_effect=_openpty), \
         mock.patch("subprocess.Popen", side_effect=_popen), \
         mock.patch("os.close"), \
         mock.patch("os.makedirs"), \
         mock.patch("os.kill", side_effect=lambda pid, sig: killed.append(pid)), \
         mock.patch("os.waitpid", side_effect=lambda pid, flags: (pid, 0)), \
         mock.patch.object(module, "read_pty_output", lambda *a, **k: None), \
         mock.patch.object(module, "log_telemetry", lambda *a, **k: None):
        threads = [threading.Thread(target=attempt) for _ in range(attempts)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    with module.sessions_lock:
        created = len(module.sessions)
        module.sessions.clear()
    assert created == 3, f"expected exactly 3 sessions, got {created}"
    assert statuses.count(429) == attempts - 3
    assert statuses.count(200) == 3


# ── monitoring ───────────────────────────────────────────────


def test_monitor_logs_capacity_fields(monkeypatch, caplog):
    module = _fresh_app(
        limit=10,
        controller=rc.BrowserCapacityController(
            memory_reader=lambda: rc.CgroupMemory(800, 1000, True)
        ),
    )
    monkeypatch.setattr(module, "_process_tree_rss_mb", lambda: 42)
    with caplog.at_level("INFO"):
        module._log_resource_snapshot()
    assert "browser_sessions=" in caplog.text
    assert "cgroup_used=800B" in caplog.text
    assert "cgroup_limit=1000B" in caplog.text
    assert "cgroup_percent=80.0" in caplog.text
    assert "pressure=pressured" in caplog.text


def test_monitor_never_raises_on_bad_telemetry(caplog):
    """Monitoring must not be able to kill the process it watches."""
    module = _fresh_app(limit=10)

    def _explode():
        raise RuntimeError("cgroup read blew up")

    module._browser_capacity = rc.BrowserCapacityController(memory_reader=_explode)
    with caplog.at_level("INFO"), pytest.raises(RuntimeError):
        module._log_resource_snapshot()
    # The supervising loop swallows it (see resource_pressure_monitor), which
    # is what keeps a telemetry bug from taking down the worker.


def test_signal_module_available_for_cleanup():
    """``_kill_speculative_session`` relies on the module-level signal import."""
    module = _fresh_app(limit=1)
    assert module.signal is signal


# ── review-driven regression coverage ────────────────────────


def test_pending_reservations_block_a_concurrent_burst():
    """Admitted-but-not-inserted launches consume both a slot and a reserve.

    Without pending accounting, ten simultaneous requests all read the same
    session count and fork past the ceiling before the post-fork check.
    """
    controller = rc.BrowserCapacityController(
        memory_reader=lambda: rc.CgroupMemory(None, None, False)
    )
    assert controller.evaluate(0, 3, 0).allowed
    assert controller.evaluate(0, 3, 2).allowed
    assert controller.evaluate(0, 3, 3).allowed is False
    assert controller.evaluate(2, 3, 1).allowed is False


def test_pending_reservations_are_memory_reserved(tmp_path):
    """Each pending launch reserves its own memory, not one shared reserve."""
    reader = _cgroup(tmp_path, "500", "1000")
    controller = rc.BrowserCapacityController(
        high_watermark_percent=80,
        resume_threshold_percent=70,
        reserve_bytes=100,
        memory_reader=reader,
    )
    # 500 + 1*100 <= 800 → fits.
    assert controller.evaluate(0, 10, 0).allowed
    # 500 + 4*100 > 800 → the burst would cross the watermark together.
    fresh = rc.BrowserCapacityController(
        high_watermark_percent=80,
        resume_threshold_percent=70,
        reserve_bytes=100,
        memory_reader=reader,
    )
    rejected = fresh.evaluate(0, 10, 3)
    assert not rejected.allowed and rejected.state == "pressured"


def test_speculative_reap_retries_until_the_child_is_gone():
    """A child still dying must be reaped, not abandoned as a zombie."""
    module = _fresh_app(limit=1)
    calls = []

    def _waitpid(pid, flags):
        calls.append((pid, flags))
        return (pid, 0) if len(calls) >= 3 else (0, 0)

    with mock.patch("os.close"), mock.patch("os.kill"), \
         mock.patch("os.waitpid", side_effect=_waitpid), \
         mock.patch("time.sleep"):
        module._kill_speculative_session(4321, 9)
    assert len(calls) == 3, f"expected retries until reaped, got {calls}"


def test_speculative_reap_gives_up_without_raising(monkeypatch):
    """A never-reapable child logs and returns rather than hanging or raising."""
    module = _fresh_app(limit=1)
    monkeypatch.setattr(module, "_SPECULATIVE_REAP_TIMEOUT_S", 0.0)
    with mock.patch("os.close"), mock.patch("os.kill"), \
         mock.patch("os.waitpid", return_value=(0, 0)), \
         mock.patch("time.sleep"):
        module._kill_speculative_session(4321, 9)  # must not raise


def test_spawn_failure_closes_both_pty_descriptors():
    """An exception after openpty must not leak either descriptor.

    Previously the broad handler returned 500 with the master (and sometimes
    the slave) fd still open, leaking one per failed request.
    """
    module = _fresh_app(limit=10)
    closed = []
    with mock.patch.object(module, "check_authorization", return_value=(True, "operator")), \
         mock.patch("pty.openpty", return_value=(31, 32)), \
         mock.patch("os.makedirs"), \
         mock.patch("os.close", side_effect=lambda fd: closed.append(fd)), \
         mock.patch("subprocess.Popen", side_effect=OSError("no fds")):
        response = module.app.test_client().post("/api/session", json={})
    assert response.status_code == 500
    assert set(closed) == {31, 32}, f"both descriptors must close, got {closed}"
    assert module._browser_pending == 0, "the reservation must be released"
    with module.sessions_lock:
        assert not module.sessions


def test_post_fork_rejection_releases_the_reservation():
    """A rejected speculative launch must not strand its pending slot."""
    module = _fresh_app(limit=2)
    readings = iter((rc.CgroupMemory(100, 1000, True), rc.CgroupMemory(900, 1000, True)))
    module._browser_capacity = rc.BrowserCapacityController(
        memory_reader=lambda: next(readings),
        high_watermark_percent=80,
        resume_threshold_percent=70,
        reserve_bytes=1,
    )
    proc = mock.Mock(pid=555)
    proc.poll.return_value = 0
    with mock.patch.object(module, "check_authorization", return_value=(True, "operator")), \
         mock.patch("pty.openpty", return_value=(21, 22)), \
         mock.patch("os.makedirs"), \
         mock.patch("subprocess.Popen", return_value=proc), \
         mock.patch("os.close"), mock.patch("os.kill"), \
         mock.patch("os.waitpid", side_effect=lambda pid, flags: (pid, 0)):
        response = module.app.test_client().post("/api/session", json={})
    assert response.status_code == 429
    assert module._browser_pending == 0


def test_cgroup_v1_malformed_nested_falls_back_to_root(tmp_path):
    """A malformed nested v1 pair must not mask a valid root-level pair.

    Otherwise one unreadable file silently disables the whole memory gate.
    """
    _cgroup_v1(tmp_path / "memory", "not-a-number", "0")
    _cgroup_v1(tmp_path, str(2 * MiB), str(8 * MiB))
    reading = rc.read_cgroup_memory(tmp_path)
    assert reading.available and reading.limit_bytes == 8 * MiB
    assert reading.used_bytes == 2 * MiB


def test_cgroup_v1_all_locations_malformed_reads_unavailable(tmp_path):
    _cgroup_v1(tmp_path / "memory", "bogus", "bogus")
    assert rc.read_cgroup_memory(tmp_path).available is False


def test_malformed_json_body_does_not_strand_the_reservation():
    """A non-object JSON body must not leak the pending slot.

    Reading the body outside the try/finally stranded `_browser_pending`
    forever, permanently shrinking the effective browser ceiling.
    """
    module = _fresh_app(limit=2)
    with mock.patch.object(module, "check_authorization", return_value=(True, "operator")), \
         mock.patch("pty.openpty") as _openpty:
        response = module.app.test_client().post(
            "/api/session", data="[1]", content_type="application/json"
        )
    assert response.status_code in (200, 400, 500)
    assert module._browser_pending == 0, "the reservation must be released"
    with module.sessions_lock:
        assert not module.sessions


def test_capacity_rejection_reports_the_real_pending_count():
    """The 429 payload must show pending launches, not a hard-coded 0.

    Otherwise an operator reading `0/1` cannot tell why the request was
    refused.
    """
    module = _fresh_app(limit=1)
    with module.sessions_lock:
        module._browser_pending = 1
    try:
        with mock.patch.object(module, "check_authorization", return_value=(True, "operator")):
            body = module.app.test_client().post("/api/session", json={}).get_json()
    finally:
        with module.sessions_lock:
            module._browser_pending = 0
    assert body["code"] == "BROWSER_SESSION_LIMIT"
    assert body["capacity"]["browser_sessions"]["pending"] == 1


def test_reader_thread_start_failure_removes_session_and_reaps_child():
    """A failed reader startup must not leave an unreachable inserted session."""
    module = _fresh_app(limit=2)
    proc = mock.Mock(pid=4321)
    proc.poll.return_value = None
    with mock.patch.object(module, "check_authorization", return_value=(True, "operator")), \
         mock.patch("pty.openpty", return_value=(31, 32)), \
         mock.patch("os.makedirs"), \
         mock.patch("subprocess.Popen", return_value=proc), \
         mock.patch("os.close"), mock.patch("os.kill"), \
         mock.patch("os.waitpid", return_value=(4321, 0)), \
         mock.patch("threading.Thread.start", side_effect=RuntimeError("thread limit")):
        response = module.app.test_client().post("/api/session", json={})
    assert response.status_code == 500
    assert module._browser_pending == 0
    with module.sessions_lock:
        assert not module.sessions


def test_session_survives_telemetry_failure():
    """Best-effort telemetry cannot hide a successfully started session."""
    module = _fresh_app(limit=2)
    proc = mock.Mock(pid=4322)
    proc.poll.return_value = None
    with mock.patch.object(module, "check_authorization", return_value=(True, "operator")), \
         mock.patch("pty.openpty", return_value=(41, 42)), \
         mock.patch("os.makedirs"), \
         mock.patch("subprocess.Popen", return_value=proc), \
         mock.patch("os.close"), \
         mock.patch("threading.Thread.start"), \
         mock.patch.object(module, "log_telemetry", side_effect=RuntimeError("offline")):
        response = module.app.test_client().post("/api/session", json={})
    assert response.status_code == 200
    session_id = response.get_json()["session_id"]
    with module.sessions_lock:
        assert session_id in module.sessions
    module.sessions.clear()


def test_speculative_reap_deadline_is_monotonic(monkeypatch):
    """A frozen wall clock must not make the bounded reap unbounded."""
    module = _fresh_app(limit=1)
    monkeypatch.setattr(module, "_SPECULATIVE_REAP_TIMEOUT_S", 1.0)
    # Wall clock frozen; monotonic advances. The loop must use monotonic.
    clock = {"t": 100.0}
    sleeps = []

    def _sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    with mock.patch("os.close"), mock.patch("os.kill"), \
         mock.patch("os.waitpid", return_value=(0, 0)), \
         mock.patch("time.time", return_value=1_000.0), \
         mock.patch("time.monotonic", side_effect=lambda: clock["t"]), \
         mock.patch("time.sleep", side_effect=_sleep):
        module._kill_speculative_session(4321, 9)
    assert 0 < len(sleeps) <= 25, f"bounded by the monotonic deadline, got {len(sleeps)}"
