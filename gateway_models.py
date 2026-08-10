"""Workspace AI Gateway model discovery and provider URL contracts.

This is a small runtime port of ucode's model-services bucketing and AI Gateway
URL builders. Harnesses use the workspace origin exclusively; the legacy
``*.ai-gateway.*`` host is not an agent API surface.
"""
from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlencode, urlsplit

import requests

ANTHROPIC_FAMILIES = ("fable", "opus", "sonnet", "haiku")
# Families offered to harness pickers, in preference order. Sonnet leads: it is
# the workshop default. `fable` is discovered but withheld unless explicitly
# enabled — it is a preview family, and offering it in a picker invites
# participants to select a model the workshop has not budgeted or validated.
DEFAULT_FAMILY_ORDER = ("sonnet", "opus", "haiku")
OPT_IN_FAMILIES = ("fable",)
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
_VERSION_TAIL_RE = re.compile(r"(\d+(?:-\d+)*)$")


def version_key(model: str) -> tuple[int, ...]:
    """Return a comparable version tuple from a model id's trailing digits.

    ``claude-opus-5`` -> ``(5,)`` and ``claude-opus-4-8`` -> ``(4, 8)``, so a
    plain string sort cannot put ``4-8`` above ``4-10``. Unversioned ids sort
    lowest.
    """
    match = _VERSION_TAIL_RE.search(_canonical(model))
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("-"))


def _newest_first(models: list[str]) -> list[str]:
    """Sort model ids newest version first, ties broken by id for determinism."""
    return sorted(models, key=lambda model: (version_key(model), model), reverse=True)


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


def fetch_foundation_models(workspace: str, token: str) -> dict[str, dict[str, Any]]:
    """Return live per-endpoint gateway metadata, keyed by canonical model name.

    One authenticated, zero-inference read of the workspace's foundation-model
    listing. Each entry carries the ``api_types`` the AI Gateway will actually
    accept for that model, which is what lets a picker list only the models a
    given provider dialect can address.
    """
    workspace = normalize_workspace(workspace)
    payload = _get_json(workspace + "/api/2.0/serving-endpoints:foundation-models", token)
    if not isinstance(payload, dict) or not isinstance(payload.get("endpoints"), list):
        return {}
    metadata: dict[str, dict[str, Any]] = {}
    for endpoint in payload["endpoints"]:
        if not isinstance(endpoint, dict) or not isinstance(endpoint.get("name"), str):
            continue
        name = endpoint["name"].strip()
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
        capabilities = endpoint.get("capabilities")
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        metadata.setdefault(
            _canonical(name),
            {
                "id": name,
                "api_types": api_types,
                "gateway_v2": v2,
                "description": description,
                "capabilities": capabilities,
                "reasoning": capabilities.get("openai_reasoning") is True,
            },
        )
    return metadata


# Shared Claude capability policy, ported from ucode's
# `databricks.claude_model_capabilities` so every harness agrees. This is a
# version policy, not a read of the gateway's `capabilities` flags: the gateway
# reports `long_context: false` for models that do serve the opt-in 1M tier (and
# `anthropic_reasoning: false` for models that do stream thinking), so trusting
# those flags mis-configures the pickers.
#
# Opus gained the opt-in 1M window in 4.6; Sonnet in 4.5. Sonnet's 1M tiers keep
# a conservative 64k output cap. Fable 5 is 1M by default, so it needs no `[1m]`
# suffix. Opus 4.5, Haiku and unrecognised ids use the conservative fallback.
_CLAUDE_MODEL_RE = re.compile(r"^claude-(fable|opus|sonnet|haiku)-(\d+)(?:-(\d+))?")
_CLAUDE_FALLBACK = {
    "context_window": 200_000,
    "max_tokens": 64_000,
    "supports_1m": False,
    "force_adaptive_thinking": False,
}


def claude_model_capabilities(model_id: str) -> dict[str, Any]:
    """Return the shared Claude capability policy for one model id."""
    match = _CLAUDE_MODEL_RE.match(_canonical(model_id))
    if not match:
        return dict(_CLAUDE_FALLBACK)
    family, major, minor = match.groups()
    version = (int(major), int(minor or 0))
    if family == "opus" and version >= (4, 6):
        return {
            "context_window": 1_000_000,
            "max_tokens": 128_000,
            "supports_1m": True,
            "force_adaptive_thinking": True,
        }
    if family == "sonnet" and version >= (4, 6):
        return {
            "context_window": 1_000_000,
            "max_tokens": 64_000,
            "supports_1m": True,
            "force_adaptive_thinking": True,
        }
    if family == "sonnet" and version >= (4, 5):
        return {
            "context_window": 1_000_000,
            "max_tokens": 64_000,
            "supports_1m": True,
            "force_adaptive_thinking": False,
        }
    if family == "fable" and version >= (5, 0):
        return {
            "context_window": 1_000_000,
            "max_tokens": 128_000,
            "supports_1m": False,
            "force_adaptive_thinking": True,
        }
    return dict(_CLAUDE_FALLBACK)


def anthropic_specs(models: list[str], metadata: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Return per-model specs for the anthropic dialect.

    Token limits and thinking behaviour come from the shared version policy;
    only ``image_input`` is read from the gateway, defaulting to true because
    every current Claude service accepts images.
    """
    specs: list[dict[str, Any]] = []
    for model in models:
        entry = metadata.get(_canonical(model)) or {}
        capabilities = entry.get("capabilities") or {}
        policy = claude_model_capabilities(model)
        specs.append(
            {
                "id": model,
                "image_input": capabilities.get("image_input", True) is True,
                **policy,
            }
        )
    return specs


def serves_api_type(metadata: dict[str, dict[str, Any]], model: str, api_type: str) -> bool:
    """Return True when the gateway advertises ``api_type`` for ``model``.

    Unknown models are kept (True): the listing does not always enumerate every
    model service, and silently collapsing a picker to nothing is worse than
    offering a model the route may reject.
    """
    entry = metadata.get(_canonical(model))
    if entry is None:
        return True
    if not entry["gateway_v2"]:
        return False
    return not entry["api_types"] or api_type in entry["api_types"]


def discover_oss_specs(workspace: str, token: str) -> list[dict[str, Any]]:
    """Return chat-completions-only model capabilities from live metadata."""
    metadata = fetch_foundation_models(workspace, token)
    specs: dict[str, dict[str, Any]] = {}
    for canonical, entry in metadata.items():
        if (
            canonical.startswith(("claude-", "gemini-"))
            or re.match(r"^gpt-\d(?:-|$)", canonical)
            or any(marker in canonical for marker in NON_CHAT_MARKERS)
        ):
            continue
        api_types = entry["api_types"]
        if (
            not entry["gateway_v2"]
            or "mlflow/v1/chat/completions" not in api_types
            or api_types & NATIVE_API_TYPES
        ):
            continue
        specs.setdefault(
            canonical,
            {
                "id": entry["id"],
                "reasoning": entry["reasoning"],
                "context_window": _context_window(entry["description"]),
                "max_tokens": OSS_OUTPUT_LIMITS.get(canonical),
            },
        )
    return [specs[key] for key in sorted(specs)]


def discover_model_catalog(workspace: str, token: str) -> dict[str, Any]:
    """Bucket current model services using ucode's provider precedence.

    Each bucket is then filtered against the gateway's own advertised
    ``api_types``, so a picker only offers models the provider dialect it is
    configured with can actually address.
    """
    ids = list_model_services(workspace, token)
    metadata = fetch_foundation_models(workspace, token)
    # Every served version of each offered family, newest first — not just the
    # newest per family. A picker that lists one model per family cannot switch
    # to an older opus the workspace still serves, which is the whole point of
    # having a picker.
    servable_claude = [
        model for model in ids if serves_api_type(metadata, model, "anthropic/v1/messages")
    ]
    claude: list[str] = []
    for family in offered_families():
        claude.extend(family_models(family, servable_claude))
    openai = _newest_first(
        [
            model
            for model in ids
            if "gpt-" in model
            and "gpt-oss" not in model
            and serves_api_type(metadata, model, "openai/v1/responses")
        ]
    )
    gemini = _newest_first(
        [
            model
            for model in ids
            if "gemini-" in model
            and serves_api_type(metadata, model, "gemini/v1/generateContent")
        ]
    )
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
        "anthropic_specs": anthropic_specs(claude, metadata),
        "openai": openai,
        "gemini": gemini,
        "oss": oss,
        "oss_specs": normalized_specs,
    }


def offered_families() -> tuple[str, ...]:
    """Return the Anthropic families a picker may show, preference-ordered.

    ``ENABLE_FABLE_MODELS=true`` appends the opt-in preview families.
    """
    if os.environ.get("ENABLE_FABLE_MODELS", "false").strip().lower() in ("true", "1", "yes"):
        return DEFAULT_FAMILY_ORDER + OPT_IN_FAMILIES
    return DEFAULT_FAMILY_ORDER


def family_models(family: str, models: list[str]) -> list[str]:
    """Return every model of ``family`` in ``models``, newest first."""
    return _newest_first([model for model in models if f"claude-{family}-" in model])


def family_model(family: str, models: list[str], *, fallback: str) -> str:
    """Return the newest discovered model of ``family``, else ``fallback``.

    Used for the harness's per-tier defaults (Claude Code's opus/sonnet/haiku
    slots): a tier with nothing served must fall back to a model the gateway
    does accept rather than to a name that 404s.
    """
    matches = family_models(family, models)
    return matches[0] if matches else fallback


def preferred_model(requested: str, models: list[str]) -> str:
    """Return the model to default to, preferring sonnet over opus."""
    if requested in models:
        return requested
    for family in (f"claude-{name}-" for name in offered_families()):
        match = next((model for model in models if family in model), None)
        if match:
            return match
    return models[0] if models else requested
