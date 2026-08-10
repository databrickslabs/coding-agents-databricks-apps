import json
import runpy
import stat
from pathlib import Path
from unittest import mock

import pytest

import gateway_models as gm

WORKSPACE = "https://adb-7405618534560628.8.azuredatabricks.net"
CATALOG = {
    "anthropic": ["system.ai.claude-opus-5", "system.ai.claude-sonnet-4-6"],
    "openai": ["system.ai.gpt-5-5"],
    "gemini": ["system.ai.gemini-3-flash"],
    "oss": ["system.ai.kimi-k2-7-code"],
    "oss_specs": [
        {
            "id": "system.ai.kimi-k2-7-code",
            "reasoning": True,
            "context_window": 128_000,
            "max_tokens": 65_536,
        }
    ],
}


def _seed_binary(home: Path, name: str, version: str = "1.17.20") -> None:
    """Seed a fake CLI that answers ``--version``.

    setup_opencode.py compares the installed version against Omnigent's floor,
    so a stub that says nothing is treated as too old and the setup script would
    try a real ``npm install``.
    """
    path = home / ".local" / "bin" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\nif [ "$1" = "--version" ]; then echo "{version}"; fi\nexit 0\n')
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_ucode_workspace_gateway_urls_never_use_external_gateway_host():
    assert gm.opencode_base_urls(WORKSPACE) == {
        "anthropic": WORKSPACE + "/ai-gateway/anthropic/v1",
        "gemini": WORKSPACE + "/ai-gateway/gemini/v1beta",
        "openai": WORKSPACE + "/ai-gateway/codex/v1",
        "oss": WORKSPACE + "/ai-gateway/mlflow/v1",
    }
    assert gm.pi_base_urls(WORKSPACE)["claude"] == WORKSPACE + "/ai-gateway/anthropic"
    assert (
        gm.opencode_base_urls(WORKSPACE)["anthropic"] + "/messages"
        == WORKSPACE + "/ai-gateway/anthropic/v1/messages"
    )
    assert (
        gm.opencode_base_urls(WORKSPACE)["oss"] + "/chat/completions"
        == WORKSPACE + "/ai-gateway/mlflow/v1/chat/completions"
    )
    assert all(".ai-gateway." not in url for url in gm.opencode_base_urls(WORKSPACE).values())


@pytest.mark.parametrize(
    "value",
    [
        "http://workspace.example.com",
        "https://user@workspace.example.com",
        "https://workspace.example.com/path",
        "https://workspace.example.com?query=1",
        "https://7405618534560628.0.ai-gateway.azuredatabricks.net",
    ],
)
def test_workspace_origin_rejects_unsafe_values(value):
    with pytest.raises(ValueError):
        gm.normalize_workspace(value)


def test_model_services_pages_and_filters_to_system_ai(monkeypatch):
    pages = [
        {
            "model_services": [
                {"name": "model-services/system.ai.claude-opus-5"},
                {"name": "main.user.private-model"},
            ],
            "next_page_token": "next",
        },
        {
            "model_services": [
                {"name": "system.ai.gpt-5-5"},
                {"name": "model-services/system.ai.claude-opus-5"},
            ]
        },
    ]
    get = mock.Mock(side_effect=pages)
    monkeypatch.setattr(gm, "_get_json", get)
    assert gm.list_model_services(WORKSPACE, "token") == [
        "system.ai.claude-opus-5",
        "system.ai.gpt-5-5",
    ]
    assert "page_size=100" in get.call_args_list[0].args[0]
    assert "page_token=next" in get.call_args_list[1].args[0]


def test_catalog_buckets_provider_dialects_with_native_precedence(monkeypatch):
    ids = [
        "system.ai.claude-opus-5",
        "system.ai.claude-opus-4-8",
        "system.ai.gemini-3-flash",
        "system.ai.gpt-5-5",
        "system.ai.gpt-oss-120b",
        "system.ai.kimi-k2-7-code",
    ]
    monkeypatch.setattr(gm, "list_model_services", lambda *_: ids)
    monkeypatch.setattr(
        gm,
        "discover_oss_specs",
        lambda *_: [
            {"id": "databricks-gpt-oss-120b", "reasoning": True, "context_window": 128000, "max_tokens": 25000},
            {"id": "databricks-kimi-k2-7-code", "reasoning": True, "context_window": 128000, "max_tokens": 65536},
        ],
    )
    catalog = gm.discover_model_catalog(WORKSPACE, "token")
    assert catalog["anthropic"] == ["system.ai.claude-opus-5"]
    assert catalog["openai"] == ["system.ai.gpt-5-5"]
    assert catalog["gemini"] == ["system.ai.gemini-3-flash"]
    assert catalog["oss"] == [
        "system.ai.gpt-oss-120b",
        "system.ai.kimi-k2-7-code",
    ]


def test_setup_opencode_writes_ucode_provider_buckets(monkeypatch, tmp_path):
    _seed_binary(tmp_path, "opencode")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABRICKS_HOST", WORKSPACE)
    monkeypatch.setenv("DATABRICKS_TOKEN", "test-token")
    monkeypatch.setenv("ANTHROPIC_MODEL", "system.ai.claude-opus-5")  # explicit request wins
    monkeypatch.setattr(gm, "discover_model_catalog", lambda *_: CATALOG)
    runpy.run_path(str(Path(__file__).parents[1] / "setup_opencode.py"), run_name="__main__")

    config = json.loads((tmp_path / ".config/opencode/opencode.json").read_text())
    assert config["model"] == "databricks-anthropic/system.ai.claude-opus-5"
    assert config["provider"]["databricks-anthropic"]["options"]["baseURL"] == (
        WORKSPACE + "/ai-gateway/anthropic/v1"
    )
    assert config["provider"]["databricks-openai"]["options"]["baseURL"] == (
        WORKSPACE + "/ai-gateway/codex/v1"
    )
    assert config["provider"]["databricks-google"]["options"]["baseURL"] == (
        WORKSPACE + "/ai-gateway/gemini/v1beta"
    )
    assert config["provider"]["databricks-oss"]["options"]["baseURL"] == "http://127.0.0.1:4000"
    assert config["provider"]["databricks-oss"]["models"]["system.ai.kimi-k2-7-code"]["limit"] == {
        "context": 128_000,
        "output": 65_536,
    }
    serialized = json.dumps(config)
    assert ".ai-gateway." not in serialized
    assert "claude-native" not in serialized

    auth = json.loads((tmp_path / ".local/share/opencode/auth.json").read_text())
    assert set(auth) == {
        "databricks-anthropic",
        "databricks-openai",
        "databricks-google",
        "databricks-oss",
    }


def test_setup_opencode_upgrades_a_binary_below_the_omnigent_floor(monkeypatch, tmp_path):
    """A pre-existing old opencode must be upgraded, not accepted.

    Omnigent reports the host as `version-too-low` and refuses to launch an
    opencode-native session, which is invisible until a session fails.
    """
    _seed_binary(tmp_path, "opencode", version="1.18.11")  # newest stable, above the window
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABRICKS_HOST", WORKSPACE)
    monkeypatch.setenv("DATABRICKS_TOKEN", "test-token")
    monkeypatch.setattr(gm, "discover_model_catalog", lambda *_: CATALOG)

    import utils

    monkeypatch.setattr(utils, "get_npm_version", lambda *a, **kw: "0.0.0-beta-202605152242")
    installs: list[list[str]] = []

    def _fake_run(cmd, **_kwargs):
        installs.append(cmd)
        return mock.Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    runpy.run_path(str(Path(__file__).parents[1] / "setup_opencode.py"), run_name="__main__")

    opencode_installs = [c for c in installs if any("opencode-ai@" in str(p) for p in c)]
    assert opencode_installs, "an out-of-date opencode must trigger an install"
    spec = next(str(p) for p in opencode_installs[0] if "opencode-ai@" in str(p))
    # A resolved snapshot below the floor must not be requested verbatim.
    assert spec == "opencode-ai@~1.17.7", spec


def test_setup_proxy_source_pins_workspace_mlflow_route():
    source = (Path(__file__).parents[1] / "setup_proxy.py").read_text()
    assert 'upstream_base = f"{host}/ai-gateway/mlflow/v1"' in source
    assert "get_gateway_host" not in source
    assert "{gateway_host}/mlflow/v1" not in source


def test_app_yaml_enables_the_three_supported_harnesses_on_system_ai_defaults():
    """The installed set is Claude Code, Pi and OpenCode.

    Claude Code is Omnigent's default harness, so a participant who never opens
    the picker must still land on a working agent. The harnesses this workspace
    cannot serve (Hermes, Codex, Gemini) stay off, and every enabled harness
    defaults to the same `system.ai` sonnet model.
    """
    source = (Path(__file__).parents[1] / "app.yaml").read_text()
    assert source.count("value: system.ai.claude-sonnet-5") == 2
    assert '- name: ENABLE_CLAUDE\n    value: "true"' in source
    assert '- name: ENABLE_PI\n    value: "true"' in source
    assert '- name: ENABLE_OPENCODE\n    value: "true"' in source
    for disabled in ("ENABLE_HERMES", "ENABLE_CODEX", "ENABLE_GEMINI", "ENABLE_FABLE_MODELS"):
        assert f'- name: {disabled}\n    value: "false"' in source, disabled


def test_fable_is_withheld_from_pickers_by_default(monkeypatch):
    """`fable` is a preview family; a workshop picker must not offer it."""
    monkeypatch.delenv("ENABLE_FABLE_MODELS", raising=False)
    assert gm.offered_families() == ("sonnet", "opus", "haiku")

    ids = [
        "system.ai.claude-fable-2",
        "system.ai.claude-opus-5",
        "system.ai.claude-sonnet-5",
    ]
    monkeypatch.setattr(gm, "list_model_services", lambda *_a, **_kw: ids)
    monkeypatch.setattr(gm, "discover_oss_specs", lambda *_a, **_kw: [])

    catalog = gm.discover_model_catalog(WORKSPACE, "tok")

    assert catalog["anthropic"] == ["system.ai.claude-sonnet-5", "system.ai.claude-opus-5"]
    assert not any("fable" in model for model in catalog["anthropic"])


def test_fable_is_available_when_explicitly_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_FABLE_MODELS", "true")
    assert gm.offered_families() == ("sonnet", "opus", "haiku", "fable")


def test_default_model_is_sonnet_not_opus(monkeypatch):
    """An unavailable request must fall back to sonnet, not opus."""
    monkeypatch.delenv("ENABLE_FABLE_MODELS", raising=False)
    models = ["system.ai.claude-opus-5", "system.ai.claude-sonnet-5", "system.ai.claude-haiku-4-5"]
    assert gm.preferred_model("system.ai.claude-gone-9", models) == "system.ai.claude-sonnet-5"
    # An explicit, available request still wins.
    assert gm.preferred_model("system.ai.claude-opus-5", models) == "system.ai.claude-opus-5"


def test_app_yaml_defaults_the_pickers_to_sonnet(monkeypatch):
    source = (Path(__file__).parents[1] / "app.yaml").read_text()
    assert "value: system.ai.claude-sonnet-5" in source
    assert "value: system.ai.claude-opus-5" not in source
    assert 'name: ENABLE_FABLE_MODELS' in source


def _fm_payload(entries):
    """Build a foundation-models listing payload from (name, api_types) pairs."""
    return {
        "endpoints": [
            {
                "name": name,
                "config": {
                    "served_entities": [
                        {
                            "foundation_model": {
                                "ai_gateway_v2_supported": True,
                                "api_types": list(api_types),
                                "description": "context window of 200K tokens",
                            }
                        }
                    ]
                },
            }
            for name, api_types in entries
        ]
    }


def test_picker_lists_only_models_the_gateway_serves_for_that_dialect(monkeypatch):
    """Detection: ask the gateway what each model accepts, then populate the list.

    A Claude model that the workspace does not serve over
    `anthropic/v1/messages` must not reach the OpenCode/Pi picker — addressing
    it surfaces as AI_APICallError / ENDPOINT_NOT_FOUND at first use.
    """
    monkeypatch.delenv("ENABLE_FABLE_MODELS", raising=False)
    ids = [
        "system.ai.claude-sonnet-5",
        "system.ai.claude-opus-5",
        "system.ai.gpt-5-3",
        "system.ai.gemini-3-pro",
    ]
    monkeypatch.setattr(gm, "list_model_services", lambda *_a, **_kw: ids)
    monkeypatch.setattr(
        gm,
        "_get_json",
        lambda *_a, **_kw: _fm_payload(
            [
                ("system.ai.claude-sonnet-5", ["anthropic/v1/messages"]),
                # Served, but only as chat-completions — not addressable by the
                # anthropic-messages provider the picker is configured with.
                ("system.ai.claude-opus-5", ["mlflow/v1/chat/completions"]),
                ("system.ai.gpt-5-3", ["openai/v1/responses"]),
                ("system.ai.gemini-3-pro", ["gemini/v1/generateContent"]),
            ]
        ),
    )

    catalog = gm.discover_model_catalog(WORKSPACE, "tok")

    assert catalog["anthropic"] == ["system.ai.claude-sonnet-5"]
    assert catalog["openai"] == ["system.ai.gpt-5-3"]
    assert catalog["gemini"] == ["system.ai.gemini-3-pro"]


def test_unknown_models_are_kept_so_a_picker_never_collapses(monkeypatch):
    """Discovery failure must not silently reduce the picker to nothing."""
    ids = ["system.ai.claude-sonnet-5", "system.ai.claude-opus-5"]
    monkeypatch.setattr(gm, "list_model_services", lambda *_a, **_kw: ids)
    monkeypatch.setattr(gm, "_get_json", lambda *_a, **_kw: None)

    catalog = gm.discover_model_catalog(WORKSPACE, "tok")

    assert catalog["anthropic"] == ["system.ai.claude-sonnet-5", "system.ai.claude-opus-5"]


def test_a_model_without_gateway_v2_is_dropped(monkeypatch):
    ids = ["system.ai.claude-sonnet-5"]
    monkeypatch.setattr(gm, "list_model_services", lambda *_a, **_kw: ids)
    payload = _fm_payload([("system.ai.claude-sonnet-5", ["anthropic/v1/messages"])])
    entity = payload["endpoints"][0]["config"]["served_entities"][0]
    entity["foundation_model"]["ai_gateway_v2_supported"] = False
    monkeypatch.setattr(gm, "_get_json", lambda *_a, **_kw: payload)

    assert gm.discover_model_catalog(WORKSPACE, "tok")["anthropic"] == []


def test_setup_claude_uses_the_workspace_gateway_and_discovered_models():
    """Claude Code must not keep the legacy gateway / serving-endpoints route.

    The `*.ai-gateway.*` host and `/serving-endpoints/anthropic` cannot serve
    `system.ai.*` model services, so enabling Claude on those routes 404s on the
    first message.
    """
    source = (Path(__file__).parents[1] / "setup_claude.py").read_text()
    assert "get_gateway_host" not in source
    assert "discover_serving_endpoints" not in source
    assert "pick_in_geo_model" not in source
    assert 'pi_base_urls(databricks_host)["claude"]' in source
    assert "discover_model_catalog" in source
    assert '"ANTHROPIC_MODEL", "system.ai.claude-sonnet-5"' in source


def test_family_model_picks_newest_and_falls_back():
    served = [
        "system.ai.claude-sonnet-5",
        "system.ai.claude-sonnet-4-6",
        "system.ai.claude-opus-5",
    ]
    assert gm.family_model("sonnet", served, fallback="x") == "system.ai.claude-sonnet-5"
    assert gm.family_model("opus", served, fallback="x") == "system.ai.claude-opus-5"
    # Nothing served for the tier -> the caller's usable fallback, not a 404 name.
    assert gm.family_model("haiku", served, fallback="system.ai.claude-sonnet-5") == (
        "system.ai.claude-sonnet-5"
    )


def test_app_yaml_enables_claude_pi_and_opencode():
    """Claude Code is Omnigent's default harness, so it must be installed."""
    source = (Path(__file__).parents[1] / "app.yaml").read_text()
    for toggle in ("ENABLE_CLAUDE", "ENABLE_PI", "ENABLE_OPENCODE"):
        block = source.split(f"name: {toggle}", 1)[1].split("value:", 1)[1]
        assert block.strip().startswith('"true"'), toggle
