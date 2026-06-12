"""Regression tests for PR #67 review findings on agent setup configs.

Three live defects reported by review on the deployed app:
  1. Gemini CLI 400s — newer gemini-cli attaches `id` to functionCall /
     functionResponse parts, which the Databricks /gemini route rejects
     ('Unknown name "id" ... Cannot find field'). Fix: route Gemini CLI
     through the content-filter proxy, which strips the field. These tests
     pin the proxy upstream wiring and the setup_gemini base URL.
  2. OpenCode 400s after a model switch — covered in
     tests/test_content_filter_proxy.py (strip_unsupported_openai_params).
  3. Hermes `known_models` catalog listed databricks-gemini-2-5-pro twice
     and omitted the default model databricks-claude-opus-4-7 entirely.

setup_gemini.py and setup_hermes.py execute at import (module-level
scripts), so they are pinned via source/AST inspection instead of import.
"""

import ast
import importlib.util
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_setup_proxy():
    """Import setup/setup_proxy.py by path WITHOUT running main()."""
    path = os.path.join(REPO_ROOT, "setup", "setup_proxy.py")
    spec = importlib.util.spec_from_file_location("setup_proxy_config_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read_setup_source(name):
    with open(os.path.join(REPO_ROOT, "setup", name)) as f:
        return f.read()


# ---------------------------------------------------------------------------
# setup_proxy — upstream base pair (OpenAI-compatible + Gemini-native)
# ---------------------------------------------------------------------------

class TestResolveUpstreamBases:
    def test_gateway_mode(self):
        mod = _load_setup_proxy()
        upstream, gemini, openai = mod.resolve_upstream_bases(
            "https://123.ai-gateway.cloud.databricks.com", "https://ws.cloud.databricks.com"
        )
        assert upstream == "https://123.ai-gateway.cloud.databricks.com/mlflow/v1"
        assert gemini == "https://123.ai-gateway.cloud.databricks.com/gemini"
        assert openai == "https://123.ai-gateway.cloud.databricks.com/openai/v1"

    def test_host_fallback_mode(self):
        mod = _load_setup_proxy()
        upstream, gemini, openai = mod.resolve_upstream_bases("", "https://ws.cloud.databricks.com")
        assert upstream == "https://ws.cloud.databricks.com/serving-endpoints"
        assert gemini == "https://ws.cloud.databricks.com/serving-endpoints/google"
        assert openai == "https://ws.cloud.databricks.com/serving-endpoints"


# ---------------------------------------------------------------------------
# setup_gemini — must point Gemini CLI at the content-filter proxy, matching
# the /gemini prefix the proxy routes on.
# ---------------------------------------------------------------------------

class TestGeminiRoutesThroughProxy:
    def test_base_url_is_proxy_gemini_prefix(self):
        src = _read_setup_source("setup_gemini.py")
        why = (
            "setup_gemini.py must point GOOGLE_GEMINI_BASE_URL at the "
            "content-filter proxy (http://127.0.0.1:4000 + /gemini prefix) so "
            "the proxy can strip functionCall/functionResponse `id` fields "
            "that the Databricks /gemini route rejects with 400."
        )
        assert 'CONTENT_FILTER_PROXY_URL = "http://127.0.0.1:4000"' in src, why
        assert '{CONTENT_FILTER_PROXY_URL}/gemini' in src, why


# ---------------------------------------------------------------------------
# setup_codex — must point Codex at the proxy's transparent /openai prefix so
# long-running Codex sessions survive PAT rotation (fresh token per request)
# without losing any Responses-API capability (the proxy does not munge
# /openai traffic).
# ---------------------------------------------------------------------------

class TestCodexRoutesThroughProxy:
    def test_base_url_is_proxy_openai_prefix(self):
        src = _read_setup_source("setup_codex.py")
        why = (
            "setup_codex.py must point base_url at the content-filter proxy "
            "(http://127.0.0.1:4000 + /openai prefix). The proxy relays "
            "/openai/* verbatim with a fresh rotated PAT injected per request."
        )
        assert 'CONTENT_FILTER_PROXY_URL = "http://127.0.0.1:4000"' in src, why
        assert '{CONTENT_FILTER_PROXY_URL}/openai' in src, why


# ---------------------------------------------------------------------------
# setup_hermes — known_models catalog hygiene
# ---------------------------------------------------------------------------

def _hermes_model_catalog():
    src = _read_setup_source("setup_hermes.py")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if getattr(target, "id", None) == "model_catalog":
                    return ast.literal_eval(node.value)
    raise AssertionError("model_catalog assignment not found in setup_hermes.py")


class TestHermesModelCatalog:
    def test_no_duplicate_entries(self):
        catalog = _hermes_model_catalog()
        dupes = {m for m in catalog if catalog.count(m) > 1}
        assert not dupes, f"Duplicate entries in Hermes model catalog: {dupes}"

    def test_includes_default_and_fallback_models(self):
        catalog = _hermes_model_catalog()
        assert "databricks-claude-opus-4-7" in catalog, (
            "Hermes default model (HERMES_MODEL fallback) missing from known_models"
        )
        assert "databricks-claude-opus-4-6" in catalog, (
            "Hermes fallback model missing from known_models"
        )


# ---------------------------------------------------------------------------
# Syntax gate — the source-pin tests above read setup scripts as TEXT and can
# pass on syntactically broken files (observed live: a constant injected
# inside a parenthesized import left setup_codex.py unimportable while every
# substring assertion still passed). The setup scripts execute at import, so
# they can't be imported here — but they must at least parse.
# ---------------------------------------------------------------------------

class TestSetupScriptsParse:
    def test_all_setup_scripts_are_valid_python(self):
        setup_dir = os.path.join(REPO_ROOT, "setup")
        scripts = sorted(f for f in os.listdir(setup_dir) if f.endswith(".py"))
        assert scripts, "no setup scripts found under setup/"
        for name in scripts:
            try:
                ast.parse(_read_setup_source(name), filename=name)
            except SyntaxError as e:
                raise AssertionError(f"setup/{name} does not parse: {e}") from e
