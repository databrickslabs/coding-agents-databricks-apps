from configure_gateway_resources import (
    discover_gateway_endpoint_names,
    endpoint_model_id,
    gateway_resource_name,
    merge_resources,
)


def _endpoint(name, *, task="llm/v1/chat", ready="READY", entity=None):
    return {
        "name": name,
        "task": task,
        "state": {"ready": ready},
        "config": {
            "served_entities": [
                {"entity_name": entity or f"system.ai.{name}"}
            ]
        },
    }


def test_discovers_only_ready_foundation_model_chat_endpoints():
    endpoints = [
        _endpoint("databricks-claude-sonnet-5"),
        _endpoint("databricks-gpt-oss-120b"),
        _endpoint("databricks-qwen3-embedding", task="llm/v1/embeddings"),
        _endpoint("databricks-not-ready", ready="NOT_READY"),
        _endpoint("custom-chat", entity="catalog.schema.model"),
    ]

    assert discover_gateway_endpoint_names(endpoints) == [
        "databricks-claude-sonnet-5",
        "databricks-gpt-oss-120b",
    ]


def test_endpoint_names_map_to_routable_model_ids():
    assert endpoint_model_id("databricks-claude-sonnet-5") == "system.ai.claude-sonnet-5"


def test_gateway_resource_names_are_stable_and_fit_apps_limit():
    name = gateway_resource_name("databricks-claude-sonnet-5")
    assert name == gateway_resource_name("databricks-claude-sonnet-5")
    assert len(name) <= 30
    assert name != gateway_resource_name("databricks-claude-sonnet-4-5")


def test_merge_preserves_unrelated_resources_and_replaces_managed_set():
    current = [
        {"name": "challenge", "secret": {"scope": "s", "key": "k", "permission": "READ"}},
        {
            "name": "coda-gw-stale",
            "serving_endpoint": {"name": "databricks-stale", "permission": "CAN_QUERY"},
        },
    ]

    merged = [resource.as_dict() for resource in merge_resources(
        current,
        ["databricks-claude-sonnet-5"],
        catalog_secret_key="coda-model-catalog",
    )]
    by_name = {resource["name"]: resource for resource in merged}

    assert "challenge" in by_name
    assert "coda-gw-stale" not in by_name
    resource_name = gateway_resource_name("databricks-claude-sonnet-5")
    assert by_name[resource_name]["serving_endpoint"] == {
        "name": "databricks-claude-sonnet-5",
        "permission": "CAN_QUERY",
    }
    assert by_name["gateway-model-catalog"]["secret"] == {
        "scope": "coda-gateway",
        "key": "coda-model-catalog",
        "permission": "READ",
    }
