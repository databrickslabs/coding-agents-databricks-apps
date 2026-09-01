#!/usr/bin/env python3
"""Attach available Foundation Model endpoints to a CoDA Databricks App.

This is the deployment-time half of CoDA's ucode-compatible Gateway setup.
The deploying identity discovers READY chat endpoints; the Apps resource API
then grants the app service principal only CAN_QUERY. Existing resources are
preserved, embeddings are excluded, and repeated runs are idempotent.
"""
from __future__ import annotations

import argparse
import hashlib
from collections.abc import Iterable

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.apps import App, AppResource

_RESOURCE_PREFIX = "coda-gw-"


def gateway_resource_name(endpoint_name: str) -> str:
    """Return a deterministic Apps resource name within the 30-char limit."""
    slug = endpoint_name.removeprefix("databricks-").replace("_", "-")
    digest = hashlib.sha256(endpoint_name.encode()).hexdigest()[:8]
    return f"{_RESOURCE_PREFIX}{slug[:13]}-{digest}"


def discover_gateway_endpoint_names(endpoints: Iterable[object]) -> list[str]:
    """Return READY Foundation Model chat endpoint names."""
    names: set[str] = set()
    for endpoint in endpoints:
        data = endpoint.as_dict() if hasattr(endpoint, "as_dict") else endpoint
        if not isinstance(data, dict) or data.get("task") != "llm/v1/chat":
            continue
        state = data.get("state") or {}
        if state.get("ready") != "READY":
            continue
        entities = ((data.get("config") or {}).get("served_entities") or [])
        if not any(
            isinstance(entity, dict)
            and str(entity.get("entity_name") or "").startswith("system.ai.")
            for entity in entities
        ):
            continue
        name = data.get("name")
        if isinstance(name, str) and name.startswith("databricks-"):
            names.add(name)
    return sorted(names)


def merge_resources(current: list[dict], endpoint_names: list[str]) -> list[AppResource]:
    """Preserve non-Gateway resources and replace our managed endpoint set."""
    by_name = {
        resource["name"]: resource
        for resource in current
        if isinstance(resource, dict)
        and isinstance(resource.get("name"), str)
        and not resource["name"].startswith(_RESOURCE_PREFIX)
    }
    for endpoint_name in endpoint_names:
        resource_name = gateway_resource_name(endpoint_name)
        by_name[resource_name] = {
            "name": resource_name,
            "description": "ucode-compatible AI Gateway model access",
            "serving_endpoint": {
                "name": endpoint_name,
                "permission": "CAN_QUERY",
            },
        }
    return [AppResource.from_dict(resource) for resource in by_name.values()]


def configure(profile: str, app_name: str) -> list[str]:
    w = WorkspaceClient(profile=profile)
    app = w.apps.get(app_name)
    endpoint_names = discover_gateway_endpoint_names(w.serving_endpoints.list())
    if not endpoint_names:
        raise RuntimeError("no READY Foundation Model chat endpoints were discovered")
    current = [resource.as_dict() for resource in (app.resources or [])]
    resources = merge_resources(current, endpoint_names)
    w.apps.create_update(app_name, "resources", app=App(name=app_name, resources=resources))
    return endpoint_names


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", required=True)
    parser.add_argument("--app", required=True)
    args = parser.parse_args()
    endpoints = configure(args.profile, args.app)
    print(f"Attached {len(endpoints)} READY chat endpoints to {args.app} with CAN_QUERY:")
    for endpoint in endpoints:
        print(f"  {endpoint}")


if __name__ == "__main__":
    main()
