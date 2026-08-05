"""Which identity signs agent model calls — and that every path agrees.

CoDA has always used the user's PAT for what agents *do* (git, databricks CLI,
file writes). Model inference, though, was signed with the app service
principal, so all AI Gateway usage, cost attribution and per-user governance
collapsed onto one SP identity.

`CODA_MODEL_AUTH` now selects the identity, defaulting to `pat` so agents act as
a real user end to end. The other identity remains the fallback, so a missing PAT
(or a missing broker) degrades instead of breaking.

The thing worth testing is not the switch itself but that **three independent
code paths agree on it**:

  1. `token_helper.resolve_databricks_token()` — the in-process resolver.
  2. The emitted helper script — runs as a standalone subprocess per request for
     Claude Code (`apiKeyHelper`) and pi (`!command`), so it cannot import the
     parent module and duplicates the check.
  3. `content_filter_proxy._get_fresh_token()` — signs OpenCode / Hermes / Codex.

If these disagreed, browser-terminal agents and proxied agents would run as
different identities — the exact drift that produced the OpenCode `auth.json`
bug, where a writer and a rotator quietly diverged on a field name.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest import mock

import pytest

import token_helper

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("CODA_MODEL_AUTH", raising=False)
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)


class TestPreferenceFlag:
    def test_defaults_to_pat(self):
        assert token_helper.prefer_pat_for_model_auth() is True

    @pytest.mark.parametrize("value", ["sp", "SP", " sp ", "Sp"])
    def test_sp_selects_service_principal(self, value, monkeypatch):
        monkeypatch.setenv("CODA_MODEL_AUTH", value)
        assert token_helper.prefer_pat_for_model_auth() is False

    @pytest.mark.parametrize("value", ["pat", "", "PAT", "anything-else"])
    def test_everything_else_means_pat(self, value, monkeypatch):
        """Fail towards the user identity: an unrecognised value must not
        silently hand model calls to the service principal."""
        monkeypatch.setenv("CODA_MODEL_AUTH", value)
        assert token_helper.prefer_pat_for_model_auth() is True


class TestInProcessResolver:
    def test_pat_preferred_by_default(self):
        with mock.patch.object(token_helper, "resolve_pat", return_value="pat-tok"), \
             mock.patch.object(token_helper, "resolve_sp_oauth_token", return_value="sp-tok"):
            assert token_helper.resolve_databricks_token() == "pat-tok"

    def test_sp_preferred_when_selected(self, monkeypatch):
        monkeypatch.setenv("CODA_MODEL_AUTH", "sp")
        with mock.patch.object(token_helper, "resolve_pat", return_value="pat-tok"), \
             mock.patch.object(token_helper, "resolve_sp_oauth_token", return_value="sp-tok"):
            assert token_helper.resolve_databricks_token() == "sp-tok"

    def test_falls_back_when_preferred_absent(self):
        with mock.patch.object(token_helper, "resolve_pat", return_value=None), \
             mock.patch.object(token_helper, "resolve_sp_oauth_token", return_value="sp-tok"):
            assert token_helper.resolve_databricks_token() == "sp-tok"

    def test_none_when_neither_available(self):
        with mock.patch.object(token_helper, "resolve_pat", return_value=None), \
             mock.patch.object(token_helper, "resolve_sp_oauth_token", return_value=None):
            assert token_helper.resolve_databricks_token() is None


class TestEmittedHelperScriptAgrees:
    """The helper script duplicates the check because it runs standalone. These
    tests are what stop the duplicate drifting from the original."""

    def test_script_reads_the_same_env_var(self):
        assert token_helper.MODEL_AUTH_ENV == "CODA_MODEL_AUTH"
        assert 'os.environ.get("CODA_MODEL_AUTH", "pat")' in token_helper._HELPER_SRC, (
            "the emitted helper must read CODA_MODEL_AUTH with the same 'pat' "
            "default as prefer_pat_for_model_auth()"
        )

    def test_script_compares_against_sp_the_same_way(self):
        """Both sides must treat only the literal 'sp' as selecting the SP."""
        assert '.strip().lower() != "sp"' in token_helper._HELPER_SRC

    def test_script_prefers_pat_first_in_main(self):
        """Order matters: the fallback chain must put the PAT first by default."""
        main_src = token_helper._HELPER_SRC.split("def main()", 1)[1]
        assert re.search(r"_pat_token\(\)\s*or\s*_sp_oauth_token\(\)", main_src), (
            "default branch should be `_pat_token() or _sp_oauth_token()`"
        )
        assert re.search(r"_sp_oauth_token\(\)\s*or\s*_pat_token\(\)", main_src), (
            "the CODA_MODEL_AUTH=sp branch should be the inverse"
        )


class TestProxyAgrees:
    """content_filter_proxy signs OpenCode / Hermes / Codex. If it disagreed with
    the helper, those agents would be attributed to a different identity than
    Claude and pi on the same box."""

    def test_proxy_uses_the_shared_preference_helper(self):
        src = (REPO_ROOT / "content_filter_proxy.py").read_text()
        assert "prefer_pat_for_model_auth" in src, (
            "the proxy must consult token_helper.prefer_pat_for_model_auth() "
            "rather than hardcoding an order"
        )

    def test_proxy_prefers_pat_by_default(self, tmp_path, monkeypatch):
        import content_filter_proxy as cfp

        cfg = tmp_path / ".databrickscfg"
        cfg.write_text("[DEFAULT]\nhost = https://x\ntoken = pat-from-cfg\n")
        monkeypatch.setattr(cfp, "_DATABRICKSCFG_PATH", str(cfg))
        monkeypatch.setattr(cfp, "_TOKEN_CACHE", {"token": None, "read_at": 0.0, "mtime": 0.0})
        monkeypatch.setattr(cfp, "resolve_sp_oauth_token", lambda: "sp-tok")
        monkeypatch.setattr(cfp, "resolve_databricks_token", lambda: None)

        assert cfp._get_fresh_token() == "pat-from-cfg"

    def test_proxy_prefers_sp_when_selected(self, tmp_path, monkeypatch):
        import content_filter_proxy as cfp

        monkeypatch.setenv("CODA_MODEL_AUTH", "sp")
        cfg = tmp_path / ".databrickscfg"
        cfg.write_text("[DEFAULT]\nhost = https://x\ntoken = pat-from-cfg\n")
        monkeypatch.setattr(cfp, "_DATABRICKSCFG_PATH", str(cfg))
        monkeypatch.setattr(cfp, "_TOKEN_CACHE", {"token": None, "read_at": 0.0, "mtime": 0.0})
        monkeypatch.setattr(cfp, "resolve_sp_oauth_token", lambda: "sp-tok")
        monkeypatch.setattr(cfp, "resolve_databricks_token", lambda: None)

        assert cfp._get_fresh_token() == "sp-tok"

    def test_proxy_falls_back_to_sp_when_no_pat(self, tmp_path, monkeypatch):
        import content_filter_proxy as cfp

        cfg = tmp_path / ".databrickscfg"
        cfg.write_text("[DEFAULT]\nhost = https://x\n")  # no token
        monkeypatch.setattr(cfp, "_DATABRICKSCFG_PATH", str(cfg))
        monkeypatch.setattr(cfp, "_TOKEN_CACHE", {"token": None, "read_at": 0.0, "mtime": 0.0})
        monkeypatch.setattr(cfp, "resolve_sp_oauth_token", lambda: "sp-tok")
        monkeypatch.setattr(cfp, "resolve_databricks_token", lambda: None)

        assert cfp._get_fresh_token() == "sp-tok"


class TestDeployedConfigMatchesIntent:
    def test_app_yaml_selects_pat_and_leaves_sp_helper_off(self):
        yaml = pytest.importorskip("yaml")
        env = {
            e["name"]: e.get("value")
            for e in yaml.safe_load((REPO_ROOT / "app.yaml").read_text())["env"]
        }
        assert env.get("CODA_MODEL_AUTH") == "pat"
        assert "ENABLE_SP_APIKEYHELPER" not in env, (
            "ENABLE_SP_APIKEYHELPER should stay commented out: it attributes model "
            "inference to the service principal instead of the user"
        )

    def test_app_yaml_is_single_user(self):
        yaml = pytest.importorskip("yaml")
        env = {
            e["name"]: e.get("value")
            for e in yaml.safe_load((REPO_ROOT / "app.yaml").read_text())["env"]
        }
        assert "CODA_DISABLE_OWNER_CHECK" not in env, (
            "shared-app mode must stay off: it lets any workspace user drive the "
            "terminal as the single injected identity, which defeats attributing "
            "agent actions to a real user"
        )
