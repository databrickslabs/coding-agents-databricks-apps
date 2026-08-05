"""Unit tests for scripts/verify_coda_live.py's decision logic.

The live commands themselves only count when run inside coda-main. These tests
hold the parser/comparison/reporting logic so a verifier bug cannot turn a real
regression into a green report.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "verify_coda_live.py"


def _load():
    spec = importlib.util.spec_from_file_location("verify_coda_live", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def verifier():
    return _load()


class TestReadyEndpointParsing:
    def test_parses_cli_dict_shape_and_only_ready(self, verifier):
        raw = {
            "endpoints": [
                {"name": "databricks-claude-opus-4-8", "state": {"ready": "READY"}},
                {"name": "databricks-gpt-5", "state": {"ready": "NOT_READY"}},
                {"name": "custom", "state": {"ready": "READY"}},
            ]
        }
        assert verifier.ready_endpoints(raw) == ["custom", "databricks-claude-opus-4-8"]

    def test_accepts_list_shape(self, verifier):
        raw = [{"name": "b", "state": {"ready": "READY"}}, {"name": "a", "state": {"ready": "READY"}}]
        assert verifier.ready_endpoints(raw) == ["a", "b"]

    @pytest.mark.parametrize("raw", [None, {}, [], "garbage"])
    def test_malformed_or_empty_is_empty(self, verifier, raw):
        assert verifier.ready_endpoints(raw) == []


class TestCatalogExtraction:
    def test_pi_models(self, verifier):
        cfg = {
            "providers": {
                "databricks-claude": {
                    "models": [{"id": "b"}, {"id": "a"}, {"other": "ignored"}]
                }
            }
        }
        assert verifier.pi_models(cfg) == ["a", "b"]

    def test_opencode_combines_databricks_providers(self, verifier):
        cfg = {
            "provider": {
                "databricks": {"models": {"claude": {}}},
                "databricks-openai": {"models": {"gpt": {}}},
                "unrelated": {"models": {"must-not-appear": {}}},
            }
        }
        assert verifier.opencode_models(cfg) == ["claude", "gpt"]

    def test_claude_models_strips_1m_suffix(self, verifier, tmp_path):
        cfg = {"env": {
            "ANTHROPIC_MODEL": "databricks-claude-opus-4-8",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-4-8[1m]",
        }}
        assert verifier.claude_active_models(cfg) == ["databricks-claude-opus-4-8"]


class TestCatalogParity:
    def _write_configs(self, verifier, tmp_path, pi, oc):
        pi_path = tmp_path / "pi.json"
        oc_path = tmp_path / "oc.json"
        pi_path.write_text(json.dumps({
            "providers": {
                "databricks-claude": {
                    "baseUrl": "https://x.ai-gateway.test/anthropic",
                    "apiKey": "!python helper.py",
                    "models": [{"id": x} for x in pi],
                }
            }
        }))
        oc_path.write_text(json.dumps({
            "provider": {
                "databricks": {
                    "options": {"baseURL": "http://127.0.0.1:4000"},
                    "models": {x: {} for x in oc if not x.startswith("databricks-gpt-")},
                },
                "databricks-openai": {
                    "options": {"baseURL": "https://x.ai-gateway.test/openai/v1"},
                    "models": {x: {} for x in oc if x.startswith("databricks-gpt-")},
                },
            }
        }))
        verifier.PI_CONFIG = pi_path
        verifier.OPENCODE_CONFIG = oc_path
        verifier.opencode_displayed_models = lambda: {
            "command_ok": True,
            "models": sorted(oc),
            "errors": [],
        }

    def test_exact_match_passes(self, verifier, tmp_path):
        ready = ["databricks-claude-a", "databricks-gpt-b", "custom-endpoint"]
        self._write_configs(verifier, tmp_path, ["databricks-claude-a"], ["databricks-claude-a", "databricks-gpt-b"])
        result = verifier.catalog_comparison(ready)
        assert result["pi"]["exact_match"] is True
        assert result["opencode"]["exact_match"] is True

    def test_extra_stale_model_fails(self, verifier, tmp_path):
        self._write_configs(verifier, tmp_path, ["databricks-claude-old"], [])
        result = verifier.catalog_comparison(["databricks-claude-live"])
        assert result["pi"]["extra_not_ready"] == ["databricks-claude-old"]
        assert result["pi"]["missing_ready"] == ["databricks-claude-live"]
        assert result["pi"]["exact_match"] is False

    def test_missing_active_model_fails(self, verifier, tmp_path):
        self._write_configs(verifier, tmp_path, ["databricks-claude-a"], [])
        result = verifier.catalog_comparison(["databricks-claude-a", "databricks-claude-b"])
        assert result["pi"]["missing_ready"] == ["databricks-claude-b"]

    def test_custom_endpoint_is_not_expected(self, verifier, tmp_path):
        self._write_configs(verifier, tmp_path, [], [])
        result = verifier.catalog_comparison(["my-rag-endpoint", "custom-llama"])
        assert result["pi"]["expected_ready_compatible"] == []
        assert result["opencode"]["expected_ready_compatible"] == []


class TestFailureAggregation:
    def _green_report(self):
        return {
            "auth_material": {"broker_url_present": True, "default_pat_present": False},
            "model_token_identity": {"ok": True, "classified_as": "service_principal"},
            "databricks_cli": {"command_ok": True},
            "model_catalogs": {
                "claude": {"exact_match": True, "config_exists": True},
                "pi": {"exact_match": True, "config_exists": True},
                "opencode": {
                    "exact_match": True,
                    "config_exists": True,
                    "cli_display_exact_match": True,
                },
            },
            "inference": {"claude": {"ok": True}, "pi": {"ok": True}, "opencode": {"ok": True}},
            "github": {"ok": True},
            "workspace_round_trip": {"ok": True},
        }

    def test_green_report_has_no_failures(self, verifier):
        assert verifier.required_failures(self._green_report(), "sp") == []

    @pytest.mark.parametrize(
        "mutate,expected",
        [
            (lambda r: r["auth_material"].update(broker_url_present=False), "SP broker URL absent"),
            (lambda r: r["auth_material"].update(default_pat_present=True), "PAT present while testing SP-only baseline"),
            (lambda r: r["model_token_identity"].update(classified_as="user"), "Pi helper token did not identify as a service principal"),
            (lambda r: r["model_catalogs"]["claude"].update(exact_match=False), "claude model catalog does not exactly match READY compatible endpoints"),
            (lambda r: r["model_catalogs"]["pi"].update(exact_match=False), "pi model catalog does not exactly match READY compatible endpoints"),
            (lambda r: r["model_catalogs"]["opencode"].update(cli_display_exact_match=False), "OpenCode displayed model list does not exactly match READY compatible endpoints"),
            (lambda r: r["inference"]["opencode"].update(ok=False), "opencode inference smoke failed"),
            (lambda r: r["github"].update(ok=False), "GitHub CLI/repository read smoke failed"),
            (lambda r: r["workspace_round_trip"].update(ok=False), "Databricks workspace write/read/delete round-trip failed"),
        ],
    )
    def test_each_required_lane_can_fail_the_run(self, verifier, mutate, expected):
        report = self._green_report()
        mutate(report)
        assert expected in verifier.required_failures(report, "sp")


class TestSecretSafety:
    def test_source_never_prints_token_value(self):
        source = SCRIPT.read_text()
        # A token is held transiently for /Me, but must never enter the report.
        assert '"token": token' not in source
        assert "print(token)" not in source
        # The verifier may say that it does not print auth.json, but it must not
        # open/read that file (only the non-secret opencode config).
        assert 'open("auth.json")' not in source
        assert 'OPENCODE_AUTH' not in source

    def test_reported_auth_material_is_boolean_only(self, verifier, tmp_path):
        cfg = tmp_path / ".databrickscfg"
        cfg.write_text("[DEFAULT]\nhost = https://x\ntoken = dapi-super-secret\n")
        verifier.DATABRICKS_CFG = cfg
        summary = verifier.profile_summary()
        assert summary["default_pat_present"] is True
        assert "dapi-super-secret" not in json.dumps(summary)
