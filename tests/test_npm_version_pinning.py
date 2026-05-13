"""Tests for get_npm_version() — dynamic npm version resolution for supply chain hardening."""

from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_npm_version():
    """Import the function under test."""
    from utils import get_npm_version
    return get_npm_version


# ---------------------------------------------------------------------------
# 1. Successful version resolution
# ---------------------------------------------------------------------------

class TestNpmVersionSuccess:
    """get_npm_version should return the version string on success."""

    def test_returns_version_string(self):
        get_npm_version = _get_npm_version()
        with mock.patch("utils.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="1.2.24\n")
            result = get_npm_version("opencode-ai", min_age_days=0)
            assert result == "1.2.24"

    def test_strips_whitespace(self):
        get_npm_version = _get_npm_version()
        with mock.patch("utils.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="  3.0.41\n  ")
            result = get_npm_version("@ai-sdk/openai", min_age_days=0)
            assert result == "3.0.41"

    def test_calls_npm_view_with_correct_args(self):
        get_npm_version = _get_npm_version()
        with mock.patch("utils.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="1.0.0\n")
            get_npm_version("@openai/codex", min_age_days=0)
            mock_run.assert_called_once_with(
                ["npm", "view", "@openai/codex", "version"],
                capture_output=True, text=True, timeout=30
            )


# ---------------------------------------------------------------------------
# 2. Failure modes → return None (graceful fallback)
# ---------------------------------------------------------------------------

class TestNpmVersionFailure:
    """get_npm_version should return None on any failure, not crash."""

    def test_returns_none_on_nonzero_exit(self):
        get_npm_version = _get_npm_version()
        with mock.patch("utils.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=1, stdout="")
            result = get_npm_version("nonexistent-package", min_age_days=0)
            assert result is None

    def test_returns_none_on_empty_stdout(self):
        get_npm_version = _get_npm_version()
        with mock.patch("utils.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="")
            result = get_npm_version("some-package", min_age_days=0)
            assert result is None

    def test_returns_none_on_whitespace_only_stdout(self):
        get_npm_version = _get_npm_version()
        with mock.patch("utils.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="  \n  ")
            result = get_npm_version("some-package", min_age_days=0)
            assert result is None

    def test_returns_none_on_timeout(self):
        get_npm_version = _get_npm_version()
        import subprocess
        with mock.patch("utils.subprocess.run", side_effect=subprocess.TimeoutExpired("npm", 30)):
            result = get_npm_version("slow-package", min_age_days=0)
            assert result is None

    def test_returns_none_when_npm_not_found(self):
        get_npm_version = _get_npm_version()
        with mock.patch("utils.subprocess.run", side_effect=FileNotFoundError("npm not found")):
            result = get_npm_version("any-package", min_age_days=0)
            assert result is None


# ---------------------------------------------------------------------------
# 3. Integration: version resolution used in install commands
# ---------------------------------------------------------------------------

class TestNpmVersionIntegration:
    """Verify that resolved versions produce correct package specifiers."""

    @pytest.mark.parametrize("package,version,expected_spec", [
        ("opencode-ai", "1.2.24", "opencode-ai@1.2.24"),
        ("@ai-sdk/openai", "3.0.41", "@ai-sdk/openai@3.0.41"),
        ("@openai/codex", "0.114.0", "@openai/codex@0.114.0"),
        ("@google/gemini-cli", "0.33.0", "@google/gemini-cli@0.33.0"),
    ])
    def test_version_produces_pinned_spec(self, package, version, expected_spec):
        get_npm_version = _get_npm_version()
        with mock.patch("utils.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout=f"{version}\n")
            v = get_npm_version(package, min_age_days=0)
            spec = f"{package}@{v}" if v else f"{package}@latest"
            assert spec == expected_spec

    @pytest.mark.parametrize("package,fallback", [
        ("opencode-ai", "opencode-ai@latest"),
        ("@ai-sdk/openai", "@ai-sdk/openai"),
        ("@google/gemini-cli", "@google/gemini-cli@nightly"),
    ])
    def test_fallback_when_resolution_fails(self, package, fallback):
        get_npm_version = _get_npm_version()
        with mock.patch("utils.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=1, stdout="")
            v = get_npm_version(package, min_age_days=0)
            assert v is None
            # Simulate the fallback logic used in setup scripts
            if package == "opencode-ai":
                spec = f"{package}@{v}" if v else f"{package}@latest"
            elif package == "@google/gemini-cli":
                spec = f"{package}@{v}" if v else f"{package}@nightly"
            else:
                spec = f"{package}@{v}" if v else package
            assert spec == fallback


# ---------------------------------------------------------------------------
# 4. Cooldown filter — supply-chain hardening (min_age_days)
# ---------------------------------------------------------------------------

# A fixed "now" used by the cooldown tests so date arithmetic is deterministic.
# Pick a date well after npm started (2010) and treat it as "today" in tests.
FROZEN_NOW = "2026-05-13T12:00:00.000Z"


def _iso_days_ago(days):
    """ISO8601 timestamp `days` days before FROZEN_NOW."""
    from datetime import datetime, timedelta
    now = datetime.fromisoformat(FROZEN_NOW.replace("Z", "+00:00"))
    return (now - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _npm_view_versions_time_mock(versions, time_map):
    """Build a mock that returns the npm view <pkg> versions time --json shape."""
    import json
    return mock.Mock(returncode=0, stdout=json.dumps({"versions": versions, "time": time_map}))


class TestNpmVersionCooldown:
    """get_npm_version with min_age_days > 0 should skip too-recent releases."""

    def _patch_now(self, monkeypatch):
        """Freeze datetime.now(tz) inside utils to FROZEN_NOW."""
        import utils
        from datetime import datetime as real_datetime

        frozen = real_datetime.fromisoformat(FROZEN_NOW.replace("Z", "+00:00"))

        class _FrozenDatetime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen if tz is None else frozen.astimezone(tz)

        monkeypatch.setattr(utils, "datetime", _FrozenDatetime)

    def test_picks_latest_version_older_than_cooldown(self, monkeypatch):
        """The newest version is too fresh; cooldown picks the prior stable release."""
        self._patch_now(monkeypatch)
        get_npm_version = _get_npm_version()
        versions = ["1.2.0", "1.2.1", "1.2.2"]
        time_map = {
            "1.2.0": _iso_days_ago(60),
            "1.2.1": _iso_days_ago(30),
            "1.2.2": _iso_days_ago(2),   # too fresh
        }
        with mock.patch(
            "utils.subprocess.run",
            return_value=_npm_view_versions_time_mock(versions, time_map),
        ):
            result = get_npm_version("opencode-ai", min_age_days=7)
        assert result == "1.2.1"

    def test_returns_newest_when_all_old_enough(self, monkeypatch):
        """If every version satisfies the cooldown, return the highest one."""
        self._patch_now(monkeypatch)
        get_npm_version = _get_npm_version()
        versions = ["1.0.0", "1.0.1", "1.0.2"]
        time_map = {v: _iso_days_ago(60) for v in versions}
        with mock.patch(
            "utils.subprocess.run",
            return_value=_npm_view_versions_time_mock(versions, time_map),
        ):
            result = get_npm_version("@openai/codex", min_age_days=7)
        assert result == "1.0.2"

    def test_returns_none_when_no_version_old_enough(self, monkeypatch):
        """Every published version is within the cooldown window — refuse to install."""
        self._patch_now(monkeypatch)
        get_npm_version = _get_npm_version()
        versions = ["0.1.0", "0.2.0"]
        time_map = {
            "0.1.0": _iso_days_ago(3),
            "0.2.0": _iso_days_ago(1),
        }
        with mock.patch(
            "utils.subprocess.run",
            return_value=_npm_view_versions_time_mock(versions, time_map),
        ):
            result = get_npm_version("brand-new-package", min_age_days=7)
        assert result is None

    def test_skips_prerelease_versions(self, monkeypatch):
        """A pre-release like 2.0.0-rc.1 is never selected, even if old enough."""
        self._patch_now(monkeypatch)
        get_npm_version = _get_npm_version()
        versions = ["1.0.0", "2.0.0-rc.1", "2.0.0-rc.2"]
        time_map = {v: _iso_days_ago(60) for v in versions}
        with mock.patch(
            "utils.subprocess.run",
            return_value=_npm_view_versions_time_mock(versions, time_map),
        ):
            result = get_npm_version("some-pkg", min_age_days=7)
        assert result == "1.0.0"

    def test_calls_npm_view_with_json_args(self, monkeypatch):
        """Cooldown path queries both `versions` and `time` in one --json call."""
        self._patch_now(monkeypatch)
        get_npm_version = _get_npm_version()
        with mock.patch(
            "utils.subprocess.run",
            return_value=_npm_view_versions_time_mock(
                ["1.0.0"], {"1.0.0": _iso_days_ago(30)}
            ),
        ) as mock_run:
            get_npm_version("@google/gemini-cli", min_age_days=7)
            mock_run.assert_called_once_with(
                ["npm", "view", "@google/gemini-cli", "versions", "time", "--json"],
                capture_output=True, text=True, timeout=30,
            )

    def test_returns_none_on_malformed_json(self, monkeypatch):
        self._patch_now(monkeypatch)
        get_npm_version = _get_npm_version()
        with mock.patch(
            "utils.subprocess.run",
            return_value=mock.Mock(returncode=0, stdout="not-json-at-all"),
        ):
            result = get_npm_version("some-pkg", min_age_days=7)
        assert result is None

    def test_returns_none_on_nonzero_exit(self, monkeypatch):
        self._patch_now(monkeypatch)
        get_npm_version = _get_npm_version()
        with mock.patch(
            "utils.subprocess.run",
            return_value=mock.Mock(returncode=1, stdout=""),
        ):
            result = get_npm_version("some-pkg", min_age_days=7)
        assert result is None

    def test_env_var_overrides_default(self, monkeypatch):
        """NPM_MIN_RELEASE_AGE_DAYS env var sets the default when caller doesn't pass min_age_days."""
        self._patch_now(monkeypatch)
        monkeypatch.setenv("NPM_MIN_RELEASE_AGE_DAYS", "30")
        get_npm_version = _get_npm_version()
        # 1.0.1 is 10 days old — within the default 7-day cutoff but not the 30-day one.
        versions = ["1.0.0", "1.0.1"]
        time_map = {
            "1.0.0": _iso_days_ago(60),
            "1.0.1": _iso_days_ago(10),
        }
        with mock.patch(
            "utils.subprocess.run",
            return_value=_npm_view_versions_time_mock(versions, time_map),
        ):
            result = get_npm_version("some-pkg")  # no explicit min_age_days
        assert result == "1.0.0"

    def test_env_var_invalid_falls_back_to_seven(self, monkeypatch):
        """Garbage in NPM_MIN_RELEASE_AGE_DAYS doesn't crash — falls back to 7."""
        self._patch_now(monkeypatch)
        monkeypatch.setenv("NPM_MIN_RELEASE_AGE_DAYS", "not-an-int")
        get_npm_version = _get_npm_version()
        versions = ["1.0.0", "1.0.1"]
        time_map = {
            "1.0.0": _iso_days_ago(60),
            "1.0.1": _iso_days_ago(2),  # within default 7-day cooldown
        }
        with mock.patch(
            "utils.subprocess.run",
            return_value=_npm_view_versions_time_mock(versions, time_map),
        ):
            result = get_npm_version("some-pkg")
        assert result == "1.0.0"

    def test_zero_disables_cooldown_via_env(self, monkeypatch):
        """NPM_MIN_RELEASE_AGE_DAYS=0 routes to the fast path."""
        monkeypatch.setenv("NPM_MIN_RELEASE_AGE_DAYS", "0")
        get_npm_version = _get_npm_version()
        with mock.patch("utils.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="9.9.9\n")
            result = get_npm_version("some-pkg")  # no explicit min_age_days
            mock_run.assert_called_once_with(
                ["npm", "view", "some-pkg", "version"],
                capture_output=True, text=True, timeout=30,
            )
        assert result == "9.9.9"


# ---------------------------------------------------------------------------
# 5. Live integration (runs actual npm, skip if npm not available)
# ---------------------------------------------------------------------------

class TestNpmVersionLive:
    """Run against real npm registry to verify the function works end-to-end."""

    @pytest.mark.skipif(
        not __import__("shutil").which("npm"),
        reason="npm not installed"
    )
    def test_resolves_real_package(self):
        get_npm_version = _get_npm_version()
        # Use fast path (no cooldown) so this test isn't sensitive to recent
        # publishes — it's a sanity check that npm + the network work.
        version = get_npm_version("opencode-ai", min_age_days=0)
        assert version is not None
        # Version should look like a semver (X.Y.Z)
        parts = version.split(".")
        assert len(parts) >= 2, f"Expected semver, got: {version}"
        assert parts[0].isdigit(), f"Major version not a number: {version}"
