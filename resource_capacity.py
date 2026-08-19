"""Container memory capacity and browser-session admission control.

Only cgroup memory accounting is used: v2 (``memory.current`` /
``memory.max``) when present, otherwise v1 (``memory.usage_in_bytes`` /
``memory.limit_in_bytes``) — Databricks Apps containers still expose v1.  In
an Apps container, host memory (for example ``/proc/meminfo`` or
psutil.virtual_memory()) is not the app's limit and must never be used to
claim capacity.

Usage is the *working set*: reported usage minus the reclaimable file cache
(``inactive_file`` *and* ``active_file``) from ``memory.stat``.  Counting
reclaimable page cache as pressure would refuse sessions on a container that
is merely warm — a CoDA container that has cloned repos, installed agent CLIs
and written caches easily parks several GB in *active* file cache, which the
kernel evicts on demand long before it OOM-kills anything.  Only anonymous
and kernel memory (which cannot be reclaimed without swap or an OOM kill)
counts as pressure.

This guard is deliberately independent of any external runner or durable
session cap. Multiple controls can consume the same container memory budget,
so the memory gate can reduce currently available browser capacity below
``MAX_CONCURRENT_SESSIONS``.

Pressure uses a latch whose set and clear conditions are exact complements,
so one evaluation can never clear and immediately re-arm it:

* set when usage is at/above the high watermark, or when one more session's
  reserve would not fit under that watermark;
* clear only when usage is at/below the resume threshold *and* the reserve
  fits under the high watermark.

A consequence worth configuring for: a reserve of ``R`` percent makes the
effective admission ceiling ``high - R``, so a resume threshold above that
is subsumed by the reserve rather than ignored.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Callable

#: Canonical deployment env names for the browser memory guard.
HIGH_WATERMARK_ENV = "CODA_MEMORY_HIGH_WATERMARK_PERCENT"
RESUME_THRESHOLD_ENV = "CODA_MEMORY_RESUME_THRESHOLD_PERCENT"
BROWSER_RESERVE_MB_ENV = "CODA_BROWSER_SESSION_RESERVE_MB"
CGROUP_ROOT_ENV = "CODA_CGROUP_V2_ROOT"

DEFAULT_HIGH_WATERMARK_PERCENT = 80.0
DEFAULT_RESUME_THRESHOLD_PERCENT = 70.0
DEFAULT_BROWSER_RESERVE_MB = 768


#: A cgroup v1 limit at/above this is the kernel's "unlimited" sentinel,
#: not a real budget.
V1_UNLIMITED_FLOOR = 1 << 60


@dataclass(frozen=True)
class CgroupMemory:
    """A cgroup memory reading (v2 or v1).

    ``limit_bytes`` is ``None`` when memory.max contains ``max``.  That is a
    valid cgroup reading but cannot support percentage-based admission control.
    ``available`` is false when the cgroup files could not be read or parsed.
    """

    used_bytes: int | None
    limit_bytes: int | None
    available: bool

    @property
    def percent(self) -> float | None:
        if not self.available or self.used_bytes is None or not self.limit_bytes:
            return None
        return round(self.used_bytes * 100 / self.limit_bytes, 2)


def parse_cgroup_memory_value(value: str, *, allow_max: bool = False) -> int | None:
    """Parse one cgroup-v2 byte value, returning ``None`` for ``max``/invalid."""
    value = value.strip()
    if allow_max and value == "max":
        return None
    try:
        parsed = int(value, 10)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


#: File-LRU counters that the kernel can reclaim under pressure.  Shmem/tmpfs
#: is deliberately absent: it is swap-backed, sits on the anon LRUs, and must
#: keep counting as pressure.
_RECLAIMABLE_FILE_KEYS = ("inactive_file", "active_file")


def _reclaimable_file_bytes(stat_path: Path) -> int:
    """Reclaimable file cache from ``memory.stat``; 0 when unavailable.

    v2 spells the counters ``inactive_file``/``active_file``; v1 exposes both
    those and subtree-inclusive ``total_`` variants, which win when present.
    An unparseable counter contributes 0 so a malformed file can only ever
    make the guard more conservative, never less.
    """
    try:
        text = stat_path.read_text()
    except OSError:
        return 0
    stats: dict[str, str] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 2:
            stats.setdefault(fields[0], fields[1])
    total = 0
    for key in _RECLAIMABLE_FILE_KEYS:
        raw = stats.get(f"total_{key}", stats.get(key))
        if raw is None:
            continue
        try:
            total += max(0, int(raw))
        except ValueError:
            continue
    return total


def _read_cgroup_v2(root_path: Path) -> CgroupMemory | None:
    """Read cgroup-v2 ``memory.current`` / ``memory.max``."""
    try:
        used_raw = (root_path / "memory.current").read_text().strip()
        limit_raw = (root_path / "memory.max").read_text().strip()
    except (OSError, ValueError):
        return None
    used = parse_cgroup_memory_value(used_raw)
    limit = parse_cgroup_memory_value(limit_raw, allow_max=True)
    if used is None or (limit is not None and limit <= 0):
        return CgroupMemory(None, None, False)
    working_set = max(0, used - _reclaimable_file_bytes(root_path / "memory.stat"))
    return CgroupMemory(working_set, limit, True)


def _read_cgroup_v1(root_path: Path) -> CgroupMemory | None:
    """Read cgroup-v1 memory accounting from ``<root>/memory`` or ``<root>``.

    A malformed pair in one location must not mask a valid pair in the other,
    so parsing failures keep searching and only the exhausted search reports
    "unavailable".
    """
    seen_malformed = False
    for base in (root_path / "memory", root_path):
        try:
            used_raw = (base / "memory.usage_in_bytes").read_text().strip()
            limit_raw = (base / "memory.limit_in_bytes").read_text().strip()
        except (OSError, ValueError):
            continue
        used = parse_cgroup_memory_value(used_raw)
        limit = parse_cgroup_memory_value(limit_raw)
        if used is None or limit is None or limit <= 0:
            seen_malformed = True
            continue
        if limit >= V1_UNLIMITED_FLOOR:
            # The kernel's unlimited sentinel; a percentage of it is
            # meaningless, so report "readable but no usable limit".
            return CgroupMemory(used, None, True)
        working_set = max(0, used - _reclaimable_file_bytes(base / "memory.stat"))
        return CgroupMemory(working_set, limit, True)
    return CgroupMemory(None, None, False) if seen_malformed else None


def read_cgroup_memory(root: str | os.PathLike[str] | None = None) -> CgroupMemory:
    """Read container memory without any host-wide fallback.

    Tries cgroup v2 first, then v1.  An unreadable/unparseable pair reports
    ``available=False`` so admission falls back to the fixed session cap.
    """
    root_path = Path(root or os.environ.get(CGROUP_ROOT_ENV, "/sys/fs/cgroup"))
    v2 = _read_cgroup_v2(root_path)
    if v2 is not None and v2.available:
        return v2
    v1 = _read_cgroup_v1(root_path)
    if v1 is not None:
        return v1
    return v2 if v2 is not None else CgroupMemory(None, None, False)


@dataclass(frozen=True)
class CapacityDecision:
    allowed: bool
    state: str
    memory: CgroupMemory
    reason: str | None = None


class BrowserCapacityController:
    """Admission controller with high-watermark/resume hysteresis."""

    def __init__(
        self,
        *,
        high_watermark_percent: float = DEFAULT_HIGH_WATERMARK_PERCENT,
        resume_threshold_percent: float = DEFAULT_RESUME_THRESHOLD_PERCENT,
        reserve_bytes: int = DEFAULT_BROWSER_RESERVE_MB * 1024 * 1024,
        memory_reader: Callable[[], CgroupMemory] = read_cgroup_memory,
    ) -> None:
        if not 0 < resume_threshold_percent <= high_watermark_percent <= 100:
            raise ValueError("resume threshold must be <= high watermark and both <= 100")
        if reserve_bytes < 0:
            raise ValueError("reserve_bytes must be non-negative")
        self.high_watermark_percent = float(high_watermark_percent)
        self.resume_threshold_percent = float(resume_threshold_percent)
        self.reserve_bytes = int(reserve_bytes)
        self.memory_reader = memory_reader
        self._pressured = False

    @property
    def pressure_state(self) -> str:
        return "pressured" if self._pressured else "normal"

    def evaluate(
        self,
        current_sessions: int,
        session_limit: int,
        pending_sessions: int = 0,
    ) -> CapacityDecision:
        """Decide whether one more browser session may be admitted.

        ``pending_sessions`` are launches already admitted whose PTY/child has
        not been inserted yet. They consume both a count slot and a memory
        reserve; ignoring them lets a burst of concurrent requests all pass the
        same reading and fork past the ceiling before the post-fork check.
        """
        used_slots = current_sessions + pending_sessions
        memory = self.memory_reader()
        # Count admission is the fail-safe whenever cgroup telemetry is absent,
        # malformed, or has an unlimited max with no usable percentage.
        if not memory.available or memory.limit_bytes is None or memory.percent is None:
            self._pressured = False
            return CapacityDecision(
                used_slots < session_limit,
                "unavailable",
                memory,
                "cgroup memory telemetry unavailable; using fixed browser-session cap"
                if used_slots >= session_limit else None,
            )

        percent = memory.percent
        # Each new session's reserve must fit under the high watermark, and
        # already-pending launches have not grown into their memory yet.  The
        # set/clear conditions below are exact complements of each other, so
        # the latch cannot flap within a single evaluation.
        needed = (pending_sessions + 1) * self.reserve_bytes
        reserve_fits = memory.used_bytes + needed <= (
            memory.limit_bytes * self.high_watermark_percent / 100
        )
        if self._pressured:
            if percent <= self.resume_threshold_percent and reserve_fits:
                self._pressured = False
        elif percent >= self.high_watermark_percent or not reserve_fits:
            self._pressured = True

        if self._pressured:
            return CapacityDecision(
                False,
                "pressured",
                memory,
                "shared cgroup memory is above the safe browser-session launch budget",
            )
        return CapacityDecision(used_slots < session_limit, "normal", memory)


def env_float(name: str, default: float) -> float:
    """Read a float from the environment, falling back on a bad value."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    """Read an int from the environment, falling back on a bad value."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def controller_from_env(
    memory_reader: Callable[[], CgroupMemory] = read_cgroup_memory,
) -> BrowserCapacityController:
    """Build the CoDA controller from deployment configuration.

    A malformed or self-inconsistent configuration falls back to the
    documented defaults rather than disabling the guard.
    """
    high = env_float(HIGH_WATERMARK_ENV, DEFAULT_HIGH_WATERMARK_PERCENT)
    resume = env_float(RESUME_THRESHOLD_ENV, DEFAULT_RESUME_THRESHOLD_PERCENT)
    reserve_mb = env_int(BROWSER_RESERVE_MB_ENV, DEFAULT_BROWSER_RESERVE_MB)
    try:
        return BrowserCapacityController(
            high_watermark_percent=high,
            resume_threshold_percent=resume,
            reserve_bytes=max(0, reserve_mb) * 1024 * 1024,
            memory_reader=memory_reader,
        )
    except ValueError:
        return BrowserCapacityController(memory_reader=memory_reader)
