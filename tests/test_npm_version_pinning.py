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
            result = get_npm_version("opencode-ai")
            assert result == "1.2.24"

    def test_strips_whitespace(self):
        get_npm_version = _get_npm_version()
        with mock.patch("utils.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="  3.0.41\n  ")
            result = get_npm_version("@ai-sdk/openai")
            assert result == "3.0.41"

    def test_calls_npm_view_with_correct_args(self):
        get_npm_version = _get_npm_version()
        with mock.patch("utils.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="1.0.0\n")
            get_npm_version("@openai/codex")
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
            result = get_npm_version("nonexistent-package")
            assert result is None

    def test_returns_none_on_empty_stdout(self):
        get_npm_version = _get_npm_version()
        with mock.patch("utils.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="")
            result = get_npm_version("some-package")
            assert result is None

    def test_returns_none_on_whitespace_only_stdout(self):
        get_npm_version = _get_npm_version()
        with mock.patch("utils.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0, stdout="  \n  ")
            result = get_npm_version("some-package")
            assert result is None

    def test_returns_none_on_timeout(self):
        get_npm_version = _get_npm_version()
        import subprocess
        with mock.patch("utils.subprocess.run", side_effect=subprocess.TimeoutExpired("npm", 30)):
            result = get_npm_version("slow-package")
            assert result is None

    def test_returns_none_when_npm_not_found(self):
        get_npm_version = _get_npm_version()
        with mock.patch("utils.subprocess.run", side_effect=FileNotFoundError("npm not found")):
            result = get_npm_version("any-package")
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
            v = get_npm_version(package)
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
            v = get_npm_version(package)
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
# 4. Live integration (runs actual npm, skip if npm not available)
# ---------------------------------------------------------------------------

class TestNpmVersionLive:
    """Run against real npm registry to verify the function works end-to-end."""

    @pytest.mark.skipif(
        not __import__("shutil").which("npm"),
        reason="npm not installed"
    )
    def test_resolves_real_package(self):
        get_npm_version = _get_npm_version()
        version = get_npm_version("opencode-ai")
        assert version is not None
        # Version should look like a semver (X.Y.Z)
        parts = version.split(".")
        assert len(parts) >= 2, f"Expected semver, got: {version}"
        assert parts[0].isdigit(), f"Major version not a number: {version}"
