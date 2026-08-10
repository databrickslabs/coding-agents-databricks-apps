"""Workspace AI Gateway model discovery and provider URL contracts.

This is a small runtime port of ucode's model-services bucketing and AI Gateway
URL builders. Harnesses use the workspace origin exclusively; the legacy
``*.ai-gateway.*`` host is not an agent API surface.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode, urlsplit

import requests

ANTHROPIC_FAMILIES = ("fable", "opus", "sonnet", "haiku")
OSS_STATIC_FAMILIES = ("kimi-", "glm-")
NATIVE_API_TYPES = {
    "anthropic/v1/messages",
    "openai/v1/responses",
    "gemini/v1/generateContent",
}
NON_CHAT_MARKERS = ("embedding", "embed", "rerank")
OSS_OUTPUT_LIMITS = {
    "glm-5-2": 65_536,
    "inkling": 65_536,
    "kimi-k2-7-code": 65_536,
    "gpt-oss-120b": 25_000,
    "gpt-oss-20b": 25_000,
    "qwen35-122b-a10b": 25_000,
    "qwen3-next-80b-a3b-instruct": 10_000,
    "llama-4-maverick": 8_192,
    "meta-llama-3-1-8b-instruct": 8_192,
    "meta-llama-3-3-70b-instruct": 8_192,
    "gemma-3-12b": 8_192,
}
_CONTEXT_RE = re.compile(r"context (?:length|window) of ([\d.,]+)\s*([MK])", re.I)


def normalize_workspace(workspace: str) -> str:
    """Return an HTTPS, path-free workspace origin or raise ValueError."""
    value = workspace.strip().rstrip("/")
    if "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or ".ai-gateway." in parsed.hostname.lower()
        or parsed.username
        or parsed.password
        or parsed.path.rstrip("/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("DATABRICKS_HOST must be a path-free HTTPS workspace origin")
    return f"https://{parsed.netloc}"


def opencode_base_urls(workspace: str) -> dict[str, str]:
    workspace = normalize_workspace(workspace)
    return {
        "anthropic": workspace + "/ai-gateway/anthropic/v1",
        "gemini": workspace + "/ai-gateway/gemini/v1beta",
        "openai": workspace + "/ai-gateway/codex/v1",
        "oss": workspace + "/ai-gateway/mlflow/v1",
    }


def pi_base_urls(workspace: str) -> dict[str, str]:
    workspace = normalize_workspace(workspace)
    return {
        "claude": workspace + "/ai-gateway/anthropic",
        "gemini": workspace + "/ai-gateway/gemini/v1beta",
        "openai": workspace + "/ai-gateway/codex/v1",
        "oss": workspace + "/ai-gateway/mlflow/v1",
    }


def _get_json(url: str, token: str, *, timeout: int = 15) -> Any | None:
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
        )
        if response.status_code != 200:
            return None
        return response.json()
    except (requests.RequestException, ValueError):
        return None


def list_model_services(workspace: str, token: str, *, max_pages: int = 20) -> list[str]:
    """Return bounded, de-duplicated ``system.ai.*`` model-service ids."""
    workspace = normalize_workspace(workspace)
    ids: list[str] = []
    page_token: str | None = None
    seen_tokens: set[str] = set()
    for _ in range(max_pages):
        params = {"page_size": "100"}
        if page_token:
            params["page_token"] = page_token
        payload = _get_json(
            workspace + "/api/2.1/unity-catalog/model-services?" + urlencode(params),
            token,
        )
        if not isinstance(payload, dict):
            break
        for service in payload.get("model_services", []):
            if not isinstance(service, dict):
                continue
            name = service.get("name")
            if not isinstance(name, str):
                continue
            name = name.strip()
            if name.startswith("model-services/"):
                name = name[len("model-services/") :]
            if name.startswith("system.ai."):
                ids.append(name)
        page_token = payload.get("next_page_token") or None
        if not page_token or page_token in seen_tokens:
            break
        seen_tokens.add(page_token)
    return sorted(set(ids))


def _canonical(model_id: str) -> str:
    tail = model_id.rsplit("/", 1)[-1].strip().lower()
    for prefix in ("system.ai.", "databricks-"):
        if tail.startswith(prefix):
            tail = tail[len(prefix) :]
    return tail


def _context_window(description: str) -> int | None:
    match = _CONTEXT_RE.search(description or "")
    if not match:
        return None
    try:
        multiplier = 1_000_000 if match.group(2).upper() == "M" else 1_000
        value = int(float(match.group(1).replace(",", "")) * multiplier)
    except (ValueError, OverflowError):
        return None
    return value if value > 0 else None


def discover_oss_specs(workspace: str, token: str) -> list[dict[str, Any]]:
    """Return chat-completions-only model capabilities from live metadata."""
    workspace = normalize_workspace(workspace)
    payload = _get_json(
        workspace + "/api/2.0/serving-endpoints:foundation-models", token
    )
    if not isinstance(payload, dict) or not isinstance(payload.get("endpoints"), list):
        return []
    specs: dict[str, dict[str, Any]] = {}
    for endpoint in payload["endpoints"]:
        if not isinstance(endpoint, dict) or not isinstance(endpoint.get("name"), str):
            continue
        name = endpoint["name"].strip()
        canonical = _canonical(name)
        if (
            canonical.startswith(("claude-", "gemini-"))
            or re.match(r"^gpt-\d(?:-|$)", canonical)
            or any(marker in canonical for marker in NON_CHAT_MARKERS)
        ):
            continue
        config = endpoint.get("config")
        entities = config.get("served_entities") if isinstance(config, dict) else None
        if not isinstance(entities, list):
            continue
        api_types: set[str] = set()
        description = ""
        v2 = False
        for entity in entities:
            fm = entity.get("foundation_model") if isinstance(entity, dict) else None
            if not isinstance(fm, dict) or fm.get("ai_gateway_v2_supported") is not True:
                continue
            v2 = True
            raw_types = fm.get("api_types")
            if isinstance(raw_types, list):
                api_types.update(value for value in raw_types if isinstance(value, str))
            if not description and isinstance(fm.get("description"), str):
                description = fm["description"]
        if (
            not v2
            or "mlflow/v1/chat/completions" not in api_types
            or api_types & NATIVE_API_TYPES
        ):
            continue
        capabilities = endpoint.get("capabilities")
        specs.setdefault(
            canonical,
            {
                "id": name,
                "reasoning": isinstance(capabilities, dict)
                and capabilities.get("openai_reasoning") is True,
                "context_window": _context_window(description),
                "max_tokens": OSS_OUTPUT_LIMITS.get(canonical),
            },
        )
    return [specs[key] for key in sorted(specs)]


def discover_model_catalog(workspace: str, token: str) -> dict[str, Any]:
    """Bucket current model services using ucode's provider precedence."""
    ids = list_model_services(workspace, token)
    claude: list[str] = []
    for family in ANTHROPIC_FAMILIES:
        matches = sorted((model for model in ids if f"claude-{family}-" in model), reverse=True)
        if matches:
            claude.append(matches[0])
    openai = sorted(
        (model for model in ids if "gpt-" in model and "gpt-oss" not in model),
        reverse=True,
    )
    gemini = sorted((model for model in ids if "gemini-" in model), reverse=True)
    specs = discover_oss_specs(workspace, token)
    specs_by_id = {_canonical(spec["id"]): spec for spec in specs}
    oss: list[str] = []
    normalized_specs: list[dict[str, Any]] = []
    for model in ids:
        canonical = _canonical(model)
        spec = specs_by_id.get(canonical)
        if spec is None and any(family in canonical for family in OSS_STATIC_FAMILIES):
            spec = {
                "id": model,
                "reasoning": True,
                "context_window": 1_000_000 if "glm-5-2" in canonical else 128_000,
                "max_tokens": 65_536 if "kimi" in canonical or "glm-5-2" in canonical else 8_192,
            }
        if spec is not None:
            oss.append(model)
            normalized_specs.append({**spec, "id": model})
    return {
        "anthropic": claude,
        "openai": openai,
        "gemini": gemini,
        "oss": oss,
        "oss_specs": normalized_specs,
    }


def preferred_model(requested: str, models: list[str]) -> str:
    if requested in models:
        return requested
    for family in ("claude-opus-", "claude-sonnet-", "claude-haiku-"):
        match = next((model for model in models if family in model), None)
        if match:
            return match
    return models[0] if models else requested
