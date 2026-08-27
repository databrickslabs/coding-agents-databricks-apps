import json
import runpy
import stat
from pathlib import Path
from unittest import mock

import pytest

import gateway_models as gm

WORKSPACE = "https://workspace.example.test"
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

    setup_opencode.py compares the installed version against the supported floor,
    so a stub that says nothing is treated as too old and the setup script would
    try a real ``npm install``.
    """
    path = home / ".local" / "bin" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\nif [ "$1" = "--version" ]; then echo "{version}"; fi\nexit 0\n')
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


CATALOG_WITH_SPECS = {
    **CATALOG,
    "anthropic_specs": [
        {
            "id": model,
            "reasoning": False,
            "image_input": True,
            "long_context": False,
            "context_window": 200_000,
            "max_tokens": 64_000,
        }
        for model in CATALOG["anthropic"]
    ],
}


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
        "https://synthetic.ai-gateway.example.test",
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
    # Both served opus versions reach the picker, newest first.
    assert catalog["anthropic"] == [
        "system.ai.claude-opus-5",
        "system.ai.claude-opus-4-8",
    ]
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
        "http://127.0.0.1:4000/v1"
    )
    assert config["provider"]["databricks-openai"]["options"]["baseURL"] == (
        WORKSPACE + "/ai-gateway/codex/v1"
    )
    assert config["provider"]["databricks-google"]["options"]["baseURL"] == (
        WORKSPACE + "/ai-gateway/gemini/v1beta"
    )
    for provider_name in ("databricks-openai", "databricks-google", "databricks-oss"):
        assert config["provider"][provider_name]["options"]["apiKey"] == "test-token"
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


def test_setup_hermes_uses_broker_auth_without_pat(monkeypatch, tmp_path):
    hermes_bin = tmp_path / ".local" / "bin" / "hermes"
    hermes_bin.parent.mkdir(parents=True)
    hermes_bin.write_text("#!/bin/sh\nexit 0\n")
    hermes_bin.chmod(hermes_bin.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABRICKS_HOST", WORKSPACE)
    monkeypatch.setenv("DATABRICKS_GATEWAY_HOST", "")
    monkeypatch.setenv("ENABLE_HERMES", "true")
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)

    import token_helper

    monkeypatch.setattr(token_helper, "resolve_databricks_token", lambda: "broker-token")
    runpy.run_path(str(Path(__file__).parents[1] / "setup_hermes.py"), run_name="__main__")

    config = (tmp_path / ".hermes/config.yaml").read_text()
    assert "api_key: broker-token" in config


def test_setup_proxy_source_pins_workspace_mlflow_route():
    source = (Path(__file__).parents[1] / "setup_proxy.py").read_text()
    assert 'upstream_base = f"{host}/ai-gateway/mlflow/v1"' in source
    assert "get_gateway_host" not in source
    assert "{gateway_host}/mlflow/v1" not in source


def test_app_yaml_enables_the_default_harnesses():
    """The base CoDA config installs all default harnesses except Hermes.

    Claude Code is the default harness, so a participant who never opens the
    picker must still land on a working agent. Codex and Gemini are enabled by
    default now that compatible endpoints are available; Hermes remains opt-in.
    """
    source = (Path(__file__).parents[1] / "app.yaml").read_text()
    assert source.count("value: system.ai.claude-sonnet-5") == 2
    assert '- name: ENABLE_CLAUDE\n    value: "true"' in source
    assert '- name: ENABLE_PI\n    value: "true"' in source
    assert '- name: ENABLE_OPENCODE\n    value: "true"' in source
    assert '- name: ENABLE_CODEX\n    value: "true"' in source
    assert '- name: ENABLE_GEMINI\n    value: "true"' in source
    for disabled in ("ENABLE_HERMES", "ENABLE_FABLE_MODELS"):
        assert f'- name: {disabled}\n    value: "false"' in source, disabled


def test_fable_is_withheld_from_pickers_by_default(monkeypatch):
    """`fable` is a preview family; the picker must not offer it by default."""
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
    """Claude Code is the default harness, so it must be installed."""
    source = (Path(__file__).parents[1] / "app.yaml").read_text()
    for toggle in ("ENABLE_CLAUDE", "ENABLE_PI", "ENABLE_OPENCODE"):
        block = source.split(f"name: {toggle}", 1)[1].split("value:", 1)[1]
        assert block.strip().startswith('"true"'), toggle


def test_every_served_version_reaches_the_picker(monkeypatch):
    """A picker listing one model per family cannot switch to an older opus.

    The workspace serves opus-4-8/4-7/4-6 alongside opus-5; collapsing to the
    newest per family silently removed them.
    """
    monkeypatch.delenv("ENABLE_FABLE_MODELS", raising=False)
    ids = [
        "system.ai.claude-opus-4-6",
        "system.ai.claude-opus-4-8",
        "system.ai.claude-opus-5",
        "system.ai.claude-opus-4-7",
        "system.ai.claude-sonnet-4-6",
        "system.ai.claude-sonnet-5",
        "system.ai.claude-haiku-4-5",
        "system.ai.claude-fable-5",
    ]
    monkeypatch.setattr(gm, "list_model_services", lambda *_a, **_kw: ids)
    monkeypatch.setattr(
        gm,
        "_get_json",
        lambda *_a, **_kw: _fm_payload([(i, ["anthropic/v1/messages"]) for i in ids]),
    )

    picker = gm.discover_model_catalog(WORKSPACE, "tok")["anthropic"]

    # Sonnet family first (the default tier), each family newest -> oldest.
    assert picker == [
        "system.ai.claude-sonnet-5",
        "system.ai.claude-sonnet-4-6",
        "system.ai.claude-opus-5",
        "system.ai.claude-opus-4-8",
        "system.ai.claude-opus-4-7",
        "system.ai.claude-opus-4-6",
        "system.ai.claude-haiku-4-5",
    ]
    assert gm.preferred_model("system.ai.claude-sonnet-5", picker) == "system.ai.claude-sonnet-5"


def test_version_ordering_is_numeric_not_lexicographic():
    """`4-10` is newer than `4-8`; a string sort gets that backwards."""
    assert gm.version_key("system.ai.claude-opus-4-10") > gm.version_key(
        "system.ai.claude-opus-4-8"
    )
    assert gm.version_key("system.ai.claude-opus-5") > gm.version_key(
        "system.ai.claude-opus-4-8"
    )
    assert gm.version_key("system.ai.claude-opus-unversioned") == ()

    ordered = gm.family_models(
        "opus",
        [
            "system.ai.claude-opus-4-8",
            "system.ai.claude-opus-4-10",
            "system.ai.claude-opus-5",
        ],
    )
    assert ordered == [
        "system.ai.claude-opus-5",
        "system.ai.claude-opus-4-10",
        "system.ai.claude-opus-4-8",
    ]


def test_family_model_still_returns_the_newest_for_each_tier():
    served = [
        "system.ai.claude-opus-4-8",
        "system.ai.claude-opus-5",
        "system.ai.claude-sonnet-4-6",
        "system.ai.claude-sonnet-5",
    ]
    assert gm.family_model("opus", served, fallback="x") == "system.ai.claude-opus-5"
    assert gm.family_model("sonnet", served, fallback="x") == "system.ai.claude-sonnet-5"


def test_claude_capabilities_follow_the_shared_version_policy():
    """Ported from ucode: opus gained 1M at 4.6, sonnet at 4.5.

    The gateway's own `long_context` / `anthropic_reasoning` flags disagree with
    reality (it reports false for models that do serve these tiers), so the
    version policy is authoritative and shared by every harness.
    """
    opus5 = gm.claude_model_capabilities("system.ai.claude-opus-5")
    assert opus5["context_window"] == 1_000_000
    assert opus5["max_tokens"] == 128_000
    assert opus5["supports_1m"] is True
    assert opus5["force_adaptive_thinking"] is True

    sonnet46 = gm.claude_model_capabilities("system.ai.claude-sonnet-4-6")
    assert sonnet46["context_window"] == 1_000_000
    assert sonnet46["max_tokens"] == 64_000
    assert sonnet46["force_adaptive_thinking"] is True

    # Sonnet 4-5 has the 1M tier but not adaptive thinking.
    sonnet45 = gm.claude_model_capabilities("system.ai.claude-sonnet-4-5")
    assert sonnet45["supports_1m"] is True
    assert sonnet45["force_adaptive_thinking"] is False

    # Fable 5 is 1M by default, so it must not be given the [1m] suffix.
    fable = gm.claude_model_capabilities("system.ai.claude-fable-5")
    assert fable["context_window"] == 1_000_000
    assert fable["supports_1m"] is False
    assert fable["force_adaptive_thinking"] is True

    for conservative in (
        "system.ai.claude-opus-4-5",
        "system.ai.claude-haiku-4-5",
        "system.ai.claude-unknown-9",
    ):
        caps = gm.claude_model_capabilities(conservative)
        assert caps == {
            "context_window": 200_000,
            "max_tokens": 64_000,
            "supports_1m": False,
            "force_adaptive_thinking": False,
        }, conservative


def test_pi_declares_adaptive_thinking_for_the_newer_tiers(monkeypatch, tmp_path):
    """Without forceAdaptiveThinking Pi sends thinking.type=enabled.

    The endpoint answers 400 "thinking.type.enabled is not supported for this
    model" — hit live on opus-5.
    """
    _seed_binary(tmp_path, "pi")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABRICKS_HOST", WORKSPACE)
    monkeypatch.setenv("DATABRICKS_TOKEN", "test-token")
    monkeypatch.setenv("PI_MODEL", "system.ai.claude-opus-5")
    models = ["system.ai.claude-opus-5", "system.ai.claude-haiku-4-5"]
    monkeypatch.setattr(
        gm,
        "discover_model_catalog",
        lambda *_a, **_kw: {
            "anthropic": models,
            "anthropic_specs": gm.anthropic_specs(models, {}),
            "openai": [],
            "gemini": [],
            "oss": [],
            "oss_specs": [],
        },
    )
    runpy.run_path(str(Path(__file__).parents[1] / "setup_pi.py"), run_name="__main__")

    entries = {
        m["id"]: m
        for m in json.loads((tmp_path / ".pi/agent/models.json").read_text())["providers"][
            "databricks-claude"
        ]["models"]
    }
    opus = entries["system.ai.claude-opus-5"]
    assert opus["reasoning"] is True
    assert opus["compat"] == {"forceAdaptiveThinking": True}
    assert opus["contextWindow"] == 1_000_000
    assert opus["maxTokens"] == 128_000

    # Haiku is conservative and must not claim adaptive thinking.
    haiku = entries["system.ai.claude-haiku-4-5"]
    assert "compat" not in haiku
    assert haiku["contextWindow"] == 200_000


def test_opencode_anthropic_provider_sends_an_explicit_bearer(monkeypatch, tmp_path):
    """@ai-sdk/anthropic would send x-api-key, which the gateway 401s.

    Verified against the live gateway: Bearer -> 400 (body validation only),
    x-api-key -> 401 "Credential was not sent or was of an unsupported type for
    this API" — the error seen in OpenCode.
    """
    _seed_binary(tmp_path, "opencode")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DATABRICKS_HOST", WORKSPACE)
    monkeypatch.setenv("DATABRICKS_TOKEN", "test-token")
    monkeypatch.setattr(gm, "discover_model_catalog", lambda *_a, **_kw: CATALOG_WITH_SPECS)
    runpy.run_path(str(Path(__file__).parents[1] / "setup_opencode.py"), run_name="__main__")

    config = json.loads((tmp_path / ".config/opencode/opencode.json").read_text())
    provider = config["provider"]["databricks-anthropic"]
    assert provider["options"]["baseURL"] == "http://127.0.0.1:4000/v1"
    assert provider["options"]["headers"]["Authorization"].startswith("Bearer ")
    # UA must be per-model: opencode clobbers provider-level headers.
    for overlay in provider["models"].values():
        assert "User-Agent" in overlay["headers"]
        assert overlay["options"] == {"toolStreaming": False}
    assert ".ai-gateway." not in json.dumps(config)


def test_cli_auth_rotates_the_opencode_provider_bearer(tmp_path, monkeypatch):
    """Rotating only auth.json would strand the provider bearer."""
    import cli_auth

    config_dir = tmp_path / ".config" / "opencode"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "opencode.json"
    config_path.write_text(
        json.dumps(
            {
                "provider": {
                    "databricks-anthropic": {
                        "options": {
                            "apiKey": "old-token",
                            "headers": {"Authorization": "Bearer old-token"},
                        }
                    },
                    "templated": {"options": {"apiKey": "{env:DATABRICKS_TOKEN}"}},
                }
            }
        )
    )
    monkeypatch.setattr(cli_auth, "_HOME", str(tmp_path))

    cli_auth._update_opencode_provider_headers("new-token")

    updated = json.loads(config_path.read_text())
    options = updated["provider"]["databricks-anthropic"]["options"]
    assert options["apiKey"] == "new-token"
    assert options["headers"]["Authorization"] == "Bearer new-token"
    # A `{env:...}` template resolves at launch and must be left alone.
    assert updated["provider"]["templated"]["options"]["apiKey"] == "{env:DATABRICKS_TOKEN}"
