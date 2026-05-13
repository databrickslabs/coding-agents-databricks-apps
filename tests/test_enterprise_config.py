"""Tests for enterprise_config.py — env-var contract for restricted networks."""

from __future__ import annotations

import os
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helper: scrub the env so each test starts from a known baseline.
# Any new var we touch must be added here.
# ---------------------------------------------------------------------------

ENTERPRISE_VARS = (
    "ENTERPRISE_MODE",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "CURL_CA_BUNDLE",
    "UV_DEFAULT_INDEX",
    "UV_HTTP_TIMEOUT",
    "UV_INDEX_INTERNAL_USERNAME",
    "UV_INDEX_INTERNAL_PASSWORD",
    "NPM_REGISTRY",
    "NPM_TOKEN",
    "GITHUB_API_BASE",
    "GITHUB_RELEASE_MIRROR",
    "CLAUDE_INSTALLER_URL",
    "HERMES_PIP_URL",
    "DEEPWIKI_MCP_URL",
    "EXA_MCP_URL",
    "npm_config_registry",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip every enterprise env var before each test."""
    for name in ENTERPRISE_VARS:
        monkeypatch.delenv(name, raising=False)
    # Also remove any computed npm_config_// keys lingering from prior tests
    for key in list(os.environ.keys()):
        if key.startswith("npm_config_//"):
            monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# 1. is_enabled / _truthy parsing
# ---------------------------------------------------------------------------


class TestIsEnabled:
    """ENTERPRISE_MODE is parsed leniently to match ENABLE_HERMES conventions."""

    def test_unset_returns_false(self):
        from enterprise_config import is_enabled

        assert is_enabled() is False

    @pytest.mark.parametrize(
        "value", ["true", "TRUE", "True", "1", "yes", "on", " true "]
    )
    def test_truthy_values(self, value, monkeypatch):
        monkeypatch.setenv("ENTERPRISE_MODE", value)
        from enterprise_config import is_enabled

        assert is_enabled() is True

    @pytest.mark.parametrize("value", ["false", "0", "no", "off", "", "  ", "maybe"])
    def test_falsy_values(self, value, monkeypatch):
        monkeypatch.setenv("ENTERPRISE_MODE", value)
        from enterprise_config import is_enabled

        assert is_enabled() is False


# ---------------------------------------------------------------------------
# 2. proxy_env — pass-through + lowercase mirroring
# ---------------------------------------------------------------------------


class TestProxyEnv:
    def test_empty_when_unset(self):
        from enterprise_config import proxy_env

        assert proxy_env() == {}

    def test_passes_through_set_vars(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
        monkeypatch.setenv("NO_PROXY", "localhost,.internal")
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/etc/ssl/corp.pem")
        from enterprise_config import proxy_env

        result = proxy_env()
        assert result["HTTPS_PROXY"] == "http://proxy:3128"
        assert result["NO_PROXY"] == "localhost,.internal"
        assert result["REQUESTS_CA_BUNDLE"] == "/etc/ssl/corp.pem"

    def test_mirrors_to_lowercase_for_curl(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
        monkeypatch.setenv("HTTP_PROXY", "http://proxy:3128")
        from enterprise_config import proxy_env

        result = proxy_env()
        assert result["https_proxy"] == "http://proxy:3128"
        assert result["http_proxy"] == "http://proxy:3128"

    def test_explicit_lowercase_not_overwritten(self, monkeypatch):
        """If operator already set lowercase var explicitly, leave it alone."""
        monkeypatch.setenv("HTTPS_PROXY", "http://upper:3128")
        monkeypatch.setenv("https_proxy", "http://lower:3128")
        from enterprise_config import proxy_env

        result = proxy_env()
        # Lowercase already in environment — proxy_env shouldn't re-mirror over it
        assert (
            "https_proxy" not in result
            or result.get("https_proxy") == "http://upper:3128"
        )

    def test_skips_blank_values(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "")
        from enterprise_config import proxy_env

        assert "HTTPS_PROXY" not in proxy_env()

    def test_ca_bundle_mirrored_to_curl_and_openssl(self, monkeypatch):
        """REQUESTS_CA_BUNDLE → CURL_CA_BUNDLE + SSL_CERT_FILE so shell scripts pick it up."""
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/etc/ssl/corp.pem")
        from enterprise_config import proxy_env

        result = proxy_env()
        assert result["CURL_CA_BUNDLE"] == "/etc/ssl/corp.pem"
        assert result["SSL_CERT_FILE"] == "/etc/ssl/corp.pem"

    def test_ca_bundle_does_not_overwrite_explicit_curl_var(self, monkeypatch):
        """Operator-set CURL_CA_BUNDLE wins over derivation from REQUESTS_CA_BUNDLE."""
        monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/etc/ssl/corp.pem")
        monkeypatch.setenv("CURL_CA_BUNDLE", "/etc/ssl/explicit.pem")
        from enterprise_config import proxy_env

        result = proxy_env()
        assert result["CURL_CA_BUNDLE"] == "/etc/ssl/explicit.pem"


# ---------------------------------------------------------------------------
# 3. uv_env — index URL + auth + scoped indexes
# ---------------------------------------------------------------------------


class TestUvEnv:
    def test_empty_when_unset(self):
        from enterprise_config import uv_env

        assert uv_env() == {}

    def test_index_url(self, monkeypatch):
        monkeypatch.setenv(
            "UV_DEFAULT_INDEX", "https://jfrog/api/pypi/pypi-virtual/simple/"
        )
        from enterprise_config import uv_env

        assert (
            uv_env()["UV_DEFAULT_INDEX"]
            == "https://jfrog/api/pypi/pypi-virtual/simple/"
        )

    def test_timeout(self, monkeypatch):
        monkeypatch.setenv("UV_HTTP_TIMEOUT", "120")
        from enterprise_config import uv_env

        assert uv_env()["UV_HTTP_TIMEOUT"] == "120"

    def test_index_auth_pairs_forwarded(self, monkeypatch):
        monkeypatch.setenv("UV_INDEX_INTERNAL_USERNAME", "svc-bot")
        monkeypatch.setenv("UV_INDEX_INTERNAL_PASSWORD", "secret")
        from enterprise_config import uv_env

        result = uv_env()
        assert result["UV_INDEX_INTERNAL_USERNAME"] == "svc-bot"
        assert result["UV_INDEX_INTERNAL_PASSWORD"] == "secret"


# ---------------------------------------------------------------------------
# 4. npm_env — npm_config_registry + auth-token key
# ---------------------------------------------------------------------------


class TestNpmEnv:
    def test_empty_when_unset(self):
        from enterprise_config import npm_env

        assert npm_env() == {}

    def test_registry_only(self, monkeypatch):
        monkeypatch.setenv(
            "NPM_REGISTRY", "https://jfrog.example.com/api/npm/npm-virtual/"
        )
        from enterprise_config import npm_env

        result = npm_env()
        assert (
            result["npm_config_registry"]
            == "https://jfrog.example.com/api/npm/npm-virtual/"
        )
        # No token configured -> no auth key
        assert not any(k.startswith("npm_config_//") for k in result)

    def test_registry_with_token(self, monkeypatch):
        monkeypatch.setenv(
            "NPM_REGISTRY", "https://jfrog.example.com/api/npm/npm-virtual/"
        )
        monkeypatch.setenv("NPM_TOKEN", "tok-abc")
        from enterprise_config import npm_env

        result = npm_env()
        assert result["npm_config_//jfrog.example.com/:_authToken"] == "tok-abc"

    def test_token_without_registry_ignored(self, monkeypatch):
        """Token alone is meaningless — there's no host to attach it to."""
        monkeypatch.setenv("NPM_TOKEN", "tok-abc")
        from enterprise_config import npm_env

        assert npm_env() == {}


# ---------------------------------------------------------------------------
# 5. subprocess_env — merge semantics
# ---------------------------------------------------------------------------


class TestSubprocessEnv:
    def test_includes_base_env(self, monkeypatch):
        monkeypatch.setenv("FOO", "bar")
        from enterprise_config import subprocess_env

        env = subprocess_env()
        assert env["FOO"] == "bar"

    def test_overlays_enterprise_vars(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
        monkeypatch.setenv("UV_DEFAULT_INDEX", "https://internal/")
        monkeypatch.setenv("NPM_REGISTRY", "https://internal-npm/")
        from enterprise_config import subprocess_env

        env = subprocess_env()
        assert env["HTTPS_PROXY"] == "http://proxy:3128"
        assert env["UV_DEFAULT_INDEX"] == "https://internal/"
        assert env["npm_config_registry"] == "https://internal-npm/"

    def test_empty_base_isolates_additions(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
        from enterprise_config import subprocess_env

        env = subprocess_env(base={})
        # No PATH, no HOME — just the enterprise contribution
        assert env == {
            "HTTPS_PROXY": "http://proxy:3128",
            "https_proxy": "http://proxy:3128",
        }


# ---------------------------------------------------------------------------
# 6. write_npmrc — file contents + idempotency
# ---------------------------------------------------------------------------


class TestWriteNpmrc:
    def test_noop_when_registry_unset(self, tmp_path):
        from enterprise_config import write_npmrc

        assert write_npmrc(tmp_path) is None
        assert not (tmp_path / ".npmrc").exists()

    def test_writes_registry_line(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "NPM_REGISTRY", "https://jfrog.example.com/api/npm/npm-virtual/"
        )
        from enterprise_config import write_npmrc

        path = write_npmrc(tmp_path)
        assert path == tmp_path / ".npmrc"
        text = path.read_text()
        assert "registry=https://jfrog.example.com/api/npm/npm-virtual/" in text

    def test_writes_auth_token_line(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "NPM_REGISTRY", "https://jfrog.example.com/api/npm/npm-virtual/"
        )
        monkeypatch.setenv("NPM_TOKEN", "tok-abc")
        from enterprise_config import write_npmrc

        path = write_npmrc(tmp_path)
        text = path.read_text()
        assert "//jfrog.example.com/:_authToken=tok-abc" in text

    def test_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NPM_REGISTRY", "https://example.com/npm/")
        from enterprise_config import write_npmrc

        path = write_npmrc(tmp_path)
        first_mtime = path.stat().st_mtime_ns
        # Re-run — should not rewrite (same content)
        write_npmrc(tmp_path)
        assert path.stat().st_mtime_ns == first_mtime

    def test_overwrites_on_content_change(self, tmp_path, monkeypatch):
        from enterprise_config import write_npmrc

        monkeypatch.setenv("NPM_REGISTRY", "https://old.example.com/npm/")
        path = write_npmrc(tmp_path)
        assert "old.example.com" in path.read_text()
        monkeypatch.setenv("NPM_REGISTRY", "https://new.example.com/npm/")
        write_npmrc(tmp_path)
        assert "new.example.com" in path.read_text()


# ---------------------------------------------------------------------------
# 7. mirror_github_release / mirror_github_api
# ---------------------------------------------------------------------------


class TestMirrorGithubRelease:
    def test_passthrough_when_mirror_unset(self):
        from enterprise_config import mirror_github_release

        url = "https://github.com/cli/cli/releases/download/v2.50.0/gh.tar.gz"
        assert mirror_github_release(url) == url

    def test_rewrites_when_mirror_set(self, monkeypatch):
        monkeypatch.setenv(
            "GITHUB_RELEASE_MIRROR",
            "https://jfrog.example.com/artifactory/github-mirror",
        )
        from enterprise_config import mirror_github_release

        result = mirror_github_release(
            "https://github.com/cli/cli/releases/download/v2.50.0/gh.tar.gz"
        )
        assert result == (
            "https://jfrog.example.com/artifactory/github-mirror"
            "/cli/cli/releases/download/v2.50.0/gh.tar.gz"
        )

    def test_strips_trailing_slash_from_mirror(self, monkeypatch):
        monkeypatch.setenv("GITHUB_RELEASE_MIRROR", "https://mirror.example.com/")
        from enterprise_config import mirror_github_release

        result = mirror_github_release(
            "https://github.com/foo/bar/releases/download/v1/a.zip"
        )
        assert result == "https://mirror.example.com/foo/bar/releases/download/v1/a.zip"

    def test_non_github_url_unchanged(self, monkeypatch):
        monkeypatch.setenv("GITHUB_RELEASE_MIRROR", "https://mirror.example.com")
        from enterprise_config import mirror_github_release

        result = mirror_github_release("https://example.com/foo")
        assert result == "https://example.com/foo"

    def test_empty_url_unchanged(self):
        from enterprise_config import mirror_github_release

        assert mirror_github_release("") == ""


class TestMirrorGithubApi:
    def test_passthrough_when_base_unset(self):
        from enterprise_config import mirror_github_api

        url = "https://api.github.com/repos/cli/cli/releases/latest"
        assert mirror_github_api(url) == url

    def test_rewrites_when_base_set(self, monkeypatch):
        monkeypatch.setenv("GITHUB_API_BASE", "https://ghe.example.com/api/v3")
        from enterprise_config import mirror_github_api

        result = mirror_github_api(
            "https://api.github.com/repos/cli/cli/releases/latest"
        )
        assert result == "https://ghe.example.com/api/v3/repos/cli/cli/releases/latest"

    def test_non_github_api_unchanged(self, monkeypatch):
        monkeypatch.setenv("GITHUB_API_BASE", "https://ghe.example.com/api/v3")
        from enterprise_config import mirror_github_api

        result = mirror_github_api("https://example.com/foo")
        assert result == "https://example.com/foo"


# ---------------------------------------------------------------------------
# 8. URL overrides (Claude installer, Hermes pip, MCP URLs)
# ---------------------------------------------------------------------------


class TestUrlOverrides:
    def test_claude_installer_default(self):
        from enterprise_config import claude_installer_url

        assert claude_installer_url() == "https://claude.ai/install.sh"

    def test_claude_installer_override(self, monkeypatch):
        monkeypatch.setenv(
            "CLAUDE_INSTALLER_URL", "https://mirror.example.com/claude-install.sh"
        )
        from enterprise_config import claude_installer_url

        assert claude_installer_url() == "https://mirror.example.com/claude-install.sh"

    def test_hermes_pip_default(self):
        from enterprise_config import hermes_pip_url

        assert (
            hermes_pip_url() == "git+https://github.com/NousResearch/hermes-agent.git"
        )

    def test_hermes_pip_override(self, monkeypatch):
        monkeypatch.setenv("HERMES_PIP_URL", "hermes-agent==1.2.3")
        from enterprise_config import hermes_pip_url

        assert hermes_pip_url() == "hermes-agent==1.2.3"

    def test_deepwiki_default(self):
        from enterprise_config import deepwiki_mcp_url

        assert deepwiki_mcp_url() == "https://mcp.deepwiki.com/mcp"

    def test_deepwiki_explicitly_disabled(self, monkeypatch):
        """Empty string means "drop this MCP server entirely"."""
        monkeypatch.setenv("DEEPWIKI_MCP_URL", "")
        from enterprise_config import deepwiki_mcp_url

        assert deepwiki_mcp_url() is None

    def test_deepwiki_override(self, monkeypatch):
        monkeypatch.setenv(
            "DEEPWIKI_MCP_URL", "https://internal-mcp.example.com/deepwiki"
        )
        from enterprise_config import deepwiki_mcp_url

        assert deepwiki_mcp_url() == "https://internal-mcp.example.com/deepwiki"

    def test_exa_explicitly_disabled(self, monkeypatch):
        monkeypatch.setenv("EXA_MCP_URL", "")
        from enterprise_config import exa_mcp_url

        assert exa_mcp_url() is None


# ---------------------------------------------------------------------------
# 9. startup_banner — secret masking and content
# ---------------------------------------------------------------------------


class TestStartupBanner:
    def test_unset_vars_marked(self):
        from enterprise_config import startup_banner

        out = startup_banner()
        assert "ENTERPRISE_MODE=<unset>" in out
        assert "NPM_REGISTRY=<unset>" in out

    def test_set_vars_shown(self, monkeypatch):
        monkeypatch.setenv("NPM_REGISTRY", "https://jfrog.example.com/api/npm/")
        from enterprise_config import startup_banner

        out = startup_banner()
        assert "NPM_REGISTRY=https://jfrog.example.com/api/npm/" in out

    def test_npm_token_masked(self, monkeypatch):
        monkeypatch.setenv("NPM_TOKEN", "tok-supersecret")
        from enterprise_config import startup_banner

        out = startup_banner()
        assert "tok-supersecret" not in out
        assert "NPM_TOKEN=***" in out

    def test_url_userinfo_password_masked(self, monkeypatch):
        """URLs of the form https://user:pass@host should have the password redacted."""
        monkeypatch.setenv(
            "UV_DEFAULT_INDEX",
            "https://svc-bot:topsecret@jfrog.example.com/api/pypi/simple/",
        )
        from enterprise_config import startup_banner

        out = startup_banner()
        assert "topsecret" not in out
        assert "svc-bot:***@jfrog.example.com" in out


# ---------------------------------------------------------------------------
# 10. missing_when_enabled — warning surface
# ---------------------------------------------------------------------------


class TestMissingWhenEnabled:
    def test_empty_when_not_enabled(self):
        from enterprise_config import missing_when_enabled

        assert missing_when_enabled() == []

    def test_lists_recommended_when_enabled(self, monkeypatch):
        monkeypatch.setenv("ENTERPRISE_MODE", "true")
        from enterprise_config import missing_when_enabled

        result = missing_when_enabled()
        assert set(result) == {
            "UV_DEFAULT_INDEX",
            "NPM_REGISTRY",
            "GITHUB_RELEASE_MIRROR",
        }

    def test_partial_config(self, monkeypatch):
        monkeypatch.setenv("ENTERPRISE_MODE", "true")
        monkeypatch.setenv("NPM_REGISTRY", "https://internal/")
        from enterprise_config import missing_when_enabled

        result = missing_when_enabled()
        assert "NPM_REGISTRY" not in result
        assert "UV_DEFAULT_INDEX" in result


# ---------------------------------------------------------------------------
# 11. bootstrap — side effects and idempotency
# ---------------------------------------------------------------------------


class TestBootstrap:
    def test_writes_npmrc_when_registry_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NPM_REGISTRY", "https://jfrog.example.com/api/npm/")
        from enterprise_config import bootstrap

        bootstrap(home=tmp_path)
        assert (tmp_path / ".npmrc").exists()

    def test_pushes_npm_config_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NPM_REGISTRY", "https://internal/")
        from enterprise_config import bootstrap

        bootstrap(home=tmp_path)
        assert os.environ.get("npm_config_registry") == "https://internal/"

    def test_setdefault_preserves_operator_value(self, tmp_path, monkeypatch):
        """An operator who manually set npm_config_registry wins over our derived value."""
        monkeypatch.setenv("NPM_REGISTRY", "https://internal/")
        monkeypatch.setenv("npm_config_registry", "https://manual-override/")
        from enterprise_config import bootstrap

        bootstrap(home=tmp_path)
        assert os.environ["npm_config_registry"] == "https://manual-override/"

    def test_logs_banner(self, tmp_path, caplog):
        from enterprise_config import bootstrap

        with caplog.at_level("INFO"):
            bootstrap(home=tmp_path)
        assert any(
            "enterprise_config: effective settings" in r.message for r in caplog.records
        )

    def test_warns_on_missing_recommended_when_enabled(
        self, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.setenv("ENTERPRISE_MODE", "true")
        from enterprise_config import bootstrap

        with caplog.at_level("WARNING"):
            bootstrap(home=tmp_path)
        assert any(
            "recommended mirrors are unset" in r.message and "NPM_REGISTRY" in r.message
            for r in caplog.records
        )

    def test_no_warning_when_disabled(self, tmp_path, monkeypatch, caplog):
        from enterprise_config import bootstrap

        with caplog.at_level("WARNING"):
            bootstrap(home=tmp_path)
        assert not any(
            "recommended mirrors are unset" in r.message for r in caplog.records
        )


# ---------------------------------------------------------------------------
# 12. doctor — reachability with injected http_get
# ---------------------------------------------------------------------------


class TestDoctor:
    def test_empty_targets_when_nothing_configured(self):
        from enterprise_config import doctor_targets

        assert doctor_targets() == []

    def test_targets_only_include_configured_vars(self, monkeypatch):
        monkeypatch.setenv("NPM_REGISTRY", "https://internal-npm/")
        monkeypatch.setenv("UV_DEFAULT_INDEX", "https://internal-pypi/")
        from enterprise_config import doctor_targets

        targets = dict(doctor_targets())
        assert targets == {
            "NPM_REGISTRY": "https://internal-npm/",
            "UV_DEFAULT_INDEX": "https://internal-pypi/",
        }

    def test_doctor_reports_reachable(self, monkeypatch):
        monkeypatch.setenv("NPM_REGISTRY", "https://internal-npm/")
        from enterprise_config import doctor

        def fake_get(url):
            return mock.Mock(status_code=200)

        results = doctor(http_get=fake_get)
        assert results == [("NPM_REGISTRY", "https://internal-npm/", True, "HTTP 200")]

    def test_doctor_reports_unreachable(self, monkeypatch):
        monkeypatch.setenv("NPM_REGISTRY", "https://unreachable/")
        from enterprise_config import doctor

        def fake_get(url):
            raise ConnectionError("name resolution failed")

        results = doctor(http_get=fake_get)
        assert results[0][:3] == ("NPM_REGISTRY", "https://unreachable/", False)
        assert "name resolution failed" in results[0][3]


# ---------------------------------------------------------------------------
# 13. shell_export_lines — debug shell replay
# ---------------------------------------------------------------------------


class TestShellExport:
    def test_empty_when_nothing_set(self):
        from enterprise_config import shell_export_lines

        assert shell_export_lines() == []

    def test_renders_export_lines(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
        from enterprise_config import shell_export_lines

        lines = shell_export_lines()
        assert "export HTTPS_PROXY='http://proxy:3128'" in lines

    def test_escapes_single_quotes(self, monkeypatch):
        monkeypatch.setenv("HTTPS_PROXY", "http://o'malley:3128")
        from enterprise_config import shell_export_lines

        lines = shell_export_lines()
        # Standard shell-escape pattern: close, escape, reopen
        assert any("o'\\''malley" in line for line in lines)
