#!/usr/bin/env python
"""Lightweight HTTP proxy that sanitizes requests and responses between OpenCode and Databricks.

Request-side fixes:
  - Strips empty/whitespace-only text content blocks (OpenCode #5028)
  - Strips orphaned tool_result blocks with no matching tool_use
  - Removes empty messages after filtering

Response-side fixes:
  - Remaps 'databricks-tool-call' back to real tool names
  - Fixes finish_reason when tool calls are present

Runs on localhost (never exposed externally). Zero external dependencies
beyond stdlib + requests (already installed via databricks-sdk).

See: https://github.com/sst/opencode/issues/5028
     https://github.com/BerriAI/litellm/pull/20384
"""
import configparser
import json
import logging
import os
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlsplit

import requests
from gateway_models import normalize_workspace
from token_helper import resolve_databricks_token, resolve_sp_oauth_token

UPSTREAM_BASE = os.environ.get("PROXY_UPSTREAM_BASE", "")
LISTEN_HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("PROXY_PORT", "4000"))

_HEALTH_SERVICE = "coda-content-filter-proxy"
_HEALTH_CACHE_TTL = 3.0
_HEALTH_CACHE: dict = {"checked_at": 0.0, "status": 503, "payload": None}


def _trace_proxy_request(*, path, req, headers, resp_body, status, t_start):
    """spec-D: hand the request off to proxy_tracing for a fire-and-forget span.

    Fully guarded — if proxy_tracing / mlflow is unavailable or errors, the proxy
    is unaffected (D-N4). Never on the request's critical path (called post-send).
    """
    try:
        import time as _time

        import proxy_tracing

        resp_json = None
        if resp_body:
            try:
                resp_json = json.loads(resp_body)
            except Exception:  # noqa: BLE001
                resp_json = None
        proxy_tracing.trace_request(
            path=path, request_body=req, response_body=resp_json, status=status,
            headers={k: headers[k] for k in headers}, t_start=t_start,
            t_end=_time.monotonic(),
        )
    except Exception:  # noqa: BLE001 — tracing must never break the proxy
        pass

# ---------------------------------------------------------------------------
# Fresh token injection — survives PAT rotation
# ---------------------------------------------------------------------------
# The PAT rotator writes the latest token to ~/.databrickscfg every rotation.
# OpenCode (and this proxy) are separate processes with frozen env snapshots,
# so we read the file on-demand instead of trusting os.environ.

_TOKEN_CACHE: dict = {"token": None, "read_at": 0.0, "mtime": 0.0}
# Hard ceiling on cache age. With mtime invalidation below, the cache normally
# refreshes the instant the rotator rewrites the file, so this is just a
# defence against an mtime that stops advancing (e.g. clock skew, watched fs
# tools that touch the file without updating contents).
_TOKEN_CACHE_TTL = 30

_HOME = os.environ.get("HOME", "/app/python/source_code")
if not _HOME or _HOME == "/":
    _HOME = "/app/python/source_code"
_DATABRICKSCFG_PATH = os.path.join(_HOME, ".databrickscfg")


def _resolve_current_token() -> str | None:
    """Resolve a current credential without falling back to a stale cache."""
    token = resolve_sp_oauth_token()
    if token:
        return token
    try:
        config = configparser.ConfigParser()
        config.read(_DATABRICKSCFG_PATH)
        token = config.get("DEFAULT", "token", fallback=None)
        if token:
            return token
    except Exception as e:
        log.warning(f"Could not read fresh token from {_DATABRICKSCFG_PATH}: {e}")
    return resolve_databricks_token()


def _get_fresh_token() -> str | None:
    """Read current token, with a stale fallback only for in-flight requests."""
    now = time.time()
    try:
        mtime = os.stat(_DATABRICKSCFG_PATH).st_mtime
    except OSError:
        mtime = 0.0

    cache_hot = (
        _TOKEN_CACHE["token"]
        and mtime <= _TOKEN_CACHE["mtime"]
        and (now - _TOKEN_CACHE["read_at"]) < _TOKEN_CACHE_TTL
    )
    if cache_hot:
        return _TOKEN_CACHE["token"]

    token = _resolve_current_token()
    if token:
        _TOKEN_CACHE["token"] = token
        _TOKEN_CACHE["read_at"] = now
        _TOKEN_CACHE["mtime"] = mtime
        return token

    return _TOKEN_CACHE.get("token")  # stale is better than interrupting a request


def log_upstream_error(response, request_path: str) -> None:
    """Log an upstream failure as bounded metadata, never its body.

    A gateway error body can echo the prompt, tool arguments, or completion it
    rejected, and this log is tailed into the app logger, so only the status,
    the request route, and non-content headers may be recorded.
    """
    headers = getattr(response, "headers", None) or {}
    log.error(
        "Upstream returned %s for %s (%s bytes, request-id=%s)",
        getattr(response, "status_code", "unknown"),
        request_path,
        headers.get("content-length", "unknown"),
        headers.get("x-request-id", "none"),
    )


def _readiness_target(upstream: str) -> tuple[str, str, str, str, str] | None:
    """Return the authenticated zero-inference readiness target and semantics."""
    parsed = urlsplit(upstream)
    path = parsed.path.rstrip("/")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    if path == "/serving-endpoints":
        workspace = f"{parsed.scheme}://{parsed.netloc}"
        return (
            workspace + "/api/2.0/serving-endpoints",
            "workspace-serving-endpoints",
            "authenticated-workspace-serving-endpoints-listing",
            workspace,
            "upstream_status",
        )
    if path == "/ai-gateway/mlflow/v1":
        workspace = f"{parsed.scheme}://{parsed.netloc}"
        try:
            configured_workspace = normalize_workspace(os.environ.get("DATABRICKS_HOST", ""))
        except ValueError:
            return None
        if workspace != configured_workspace:
            return None
        return (
            workspace + "/api/2.0/serving-endpoints:foundation-models",
            "workspace-foundation-models",
            "authenticated-workspace-foundation-models-for-mlflow-route",
            workspace,
            "workspace_status",
        )
    return None


def _readiness_status() -> tuple[int, dict]:
    """Check proxy identity, current credentials, and upstream auth/liveness."""
    now = time.monotonic()
    cached = _HEALTH_CACHE.get("payload")
    if cached is not None and now - _HEALTH_CACHE["checked_at"] < _HEALTH_CACHE_TTL:
        return _HEALTH_CACHE["status"], dict(cached)

    payload = {
        "service": _HEALTH_SERVICE,
        "schema": 1,
        "status": "unready",
        "upstream": UPSTREAM_BASE,
        "upstream_ready": False,
        "upstream_status": None,
        "workspace": None,
        "workspace_status": None,
        "check": None,
        "readiness_semantics": None,
    }
    status = 503
    target = _readiness_target(UPSTREAM_BASE)
    if target is None:
        upstream_path = urlsplit(UPSTREAM_BASE).path.rstrip("/")
        payload["reason"] = (
            "workspace_config_invalid"
            if upstream_path == "/ai-gateway/mlflow/v1"
            else "upstream_config_invalid"
        )
    else:
        target_url, check, semantics, workspace, status_field = target
        payload.update(
            check=check,
            readiness_semantics=semantics,
            workspace=workspace,
        )
        token = _resolve_current_token()
        if not token:
            payload["reason"] = "token_unavailable"
        else:
            try:
                response = requests.get(
                    target_url,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=3,
                )
                payload[status_field] = response.status_code
                if response.status_code == 200:
                    payload["status"] = "ready"
                    payload["upstream_ready"] = True
                    status = 200
                else:
                    payload["reason"] = status_field
            except requests.exceptions.RequestException:
                payload["reason"] = (
                    "workspace_unreachable"
                    if status_field == "workspace_status"
                    else "upstream_unreachable"
                )

    _HEALTH_CACHE.update(checked_at=now, status=status, payload=dict(payload))
    return status, payload


# Diagnostic logging — writes to stderr which goes to ~/.content-filter-proxy.log
log = logging.getLogger("content-filter-proxy")
log.setLevel(logging.INFO)
if not log.handlers:
    _sh = logging.StreamHandler(sys.stderr)
    _sh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(_sh)

# JSON Schema keywords that Gemini doesn't support.
#
# Gemini validates tool parameter schemas against an OpenAPI-3.0 subset, not
# full JSON Schema (draft 2020-12). Keywords that exist only in JSON Schema —
# or that OpenAPI 3.0 models differently — are rejected outright with
# "Invalid JSON payload received. Unknown name \"<key>\" ... Cannot find field".
# The MCP tool schemas CoDA forwards routinely carry numeric-bound keywords
# (`exclusiveMinimum`/`exclusiveMaximum` are NUMBERS in draft 2020-12 but
# BOOLEAN modifiers in OpenAPI 3.0) and string/array validators Gemini's
# subset omits. Stripping them keeps the tool callable — they are advisory
# constraints, never required by any downstream API; Claude/GPT ignore them.
GEMINI_UNSUPPORTED_SCHEMA_KEYS = {
    "$schema", "$ref", "$defs", "$id", "$comment", "additionalProperties",
    # Validation keywords outside Gemini's function-declaration subset. Sending
    # any of them 400s the whole request, so the tool becomes unusable rather
    # than merely unvalidated.
    #
    # Numeric bounds: draft-2020 models exclusiveMinimum/Maximum as NUMBERS,
    # but Gemini's OpenAPI-3.0 parser has no such field → the reported 400.
    "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    # String validators.
    "minLength", "maxLength", "pattern",
    # Array validators.
    "minItems", "maxItems", "uniqueItems",
    # Object validators.
    "minProperties", "maxProperties", "patternProperties",
}

# Top-level request fields that Gemini doesn't support
GEMINI_UNSUPPORTED_REQUEST_KEYS = {
    "stream_options",
}

# Reasoning controls that only the GPT family accepts. Stripped for GPT-routed
# requests where the served endpoint rejects them; deliberately NOT stripped
# globally, because removing them from a model that *does* support reasoning
# silently downgrades its output quality.
GPT_REASONING_KEYS = {
    "reasoningSummary",
    "reasoning_effort",
}


# ---------------------------------------------------------------------------
# Gemini compatibility
# ---------------------------------------------------------------------------

def strip_unsupported_schema_keys(obj):
    """Recursively strip JSON Schema keywords that Gemini doesn't support."""
    if isinstance(obj, dict):
        cleaned = {
            k: strip_unsupported_schema_keys(v)
            for k, v in obj.items()
            if k not in GEMINI_UNSUPPORTED_SCHEMA_KEYS
        }
        # Gemini rejects range bounds on integer-typed properties specifically.
        if cleaned.get("type") == "integer":
            cleaned.pop("minimum", None)
            cleaned.pop("maximum", None)
        return cleaned
    elif isinstance(obj, list):
        return [strip_unsupported_schema_keys(item) for item in obj]
    return obj


def sanitize_tool_schemas(data):
    """Strip request fields and JSON Schema keywords that providers reject.

    Schema stripping is applied universally — $schema, additionalProperties etc.
    are never required by any downstream API. Claude/GPT ignore them, Gemini
    rejects them. Stripping for all models is safe and avoids model detection.

    Note there is deliberately NO early return for tool-less requests. There used
    to be one, which meant the top-level cleanup below (stream_options, $schema,
    and the GPT reasoning keys) silently never ran for any request without
    `tools` — i.e. every plain chat turn.
    """
    for tool in data.get("tools", []) or []:
        func = tool.get("function", {})
        if "parameters" in func:
            func["parameters"] = strip_unsupported_schema_keys(func["parameters"])

    # Strip unsupported top-level fields
    for key in GEMINI_UNSUPPORTED_REQUEST_KEYS:
        if key in data:
            log.info(f"  Stripped top-level field: {key}")
            del data[key]

    # GPT-only reasoning controls. Scoped by model id rather than applied
    # globally so reasoning-capable models keep their settings.
    model = (data.get("model") or "").lower()
    if "gpt" in model:
        for key in GPT_REASONING_KEYS:
            if key in data:
                log.info(f"  Stripped GPT reasoning field: {key} (model={model})")
                del data[key]

    # Strip $schema from top level if present
    data.pop("$schema", None)

    return data


# Served models that reject sampling params, keyed by a substring of the model id
# (tolerant of the gateway's `global.` / `databricks-` prefixes), mapped to the
# request fields to drop. claude-opus-4-8 returns 400 on `temperature` (and, by the
# same class, other sampling controls) — see strip_unsupported_sampling_params.
_MODEL_UNSUPPORTED_SAMPLING = {
    "claude-opus-4-8": ("temperature", "top_p", "top_k"),
}


def strip_unsupported_sampling_params(data):
    """Drop sampling params a specific served model rejects (400), by model id.

    Only strips for models known to reject the param — most models accept
    `temperature`, so this must NOT be unconditional. Matches on a substring of
    the request's `model` so `global.anthropic.claude-opus-4-8`,
    `databricks-claude-opus-4-8`, etc. all hit. No-op if `model` is absent.
    """
    model = (data.get("model") or "").lower()
    if not model:
        return data
    for needle, fields in _MODEL_UNSUPPORTED_SAMPLING.items():
        if needle in model:
            for f in fields:
                if f in data:
                    log.info(f"  Stripped unsupported sampling param '{f}' for {model}")
                    del data[f]
    return data


# ---------------------------------------------------------------------------
# Request-side sanitization
# ---------------------------------------------------------------------------

def _extract_tool_ids_from_message(msg):
    """Extract all tool_use/tool_call IDs from an assistant message."""
    ids = set()
    # Anthropic format: content blocks with type=tool_use
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tid = block.get("id")
                if tid:
                    ids.add(tid)
    # OpenAI format: tool_calls array
    for tc in msg.get("tool_calls") or []:
        tid = tc.get("id")
        if tid:
            ids.add(tid)
    return ids


def _extract_tool_refs_from_message(msg):
    """Extract all tool_use_id/tool_call_id references from a user/tool message."""
    refs = set()
    role = msg.get("role", "")
    content = msg.get("content")
    # Anthropic format: tool_result blocks
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                ref = block.get("tool_use_id")
                if ref:
                    refs.add(ref)
    # OpenAI format: tool messages
    if role == "tool":
        ref = msg.get("tool_call_id")
        if ref:
            refs.add(ref)
    return refs


def sanitize_messages(messages):
    """Strip empty text blocks and orphaned tool_result/tool messages.

    Runs multiple passes to handle cascading orphans (dropping one message
    can make the next one orphaned too).
    """
    if not isinstance(messages, list):
        return messages

    log.info(f"Sanitizing {len(messages)} messages")

    # Log message structure for debugging
    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        tool_ids = _extract_tool_ids_from_message(msg)
        tool_refs = _extract_tool_refs_from_message(msg)
        content = msg.get("content")
        content_desc = ""
        if isinstance(content, list):
            types = [b.get("type", "?") if isinstance(b, dict) else "str" for b in content]
            content_desc = f"[{', '.join(types)}]"
        elif isinstance(content, str):
            content_desc = f'str({len(content)} chars)'
        elif content is None:
            content_desc = "null"
        extras = ""
        if tool_ids:
            extras += f" tool_ids={tool_ids}"
        if tool_refs:
            extras += f" tool_refs={tool_refs}"
        if msg.get("tool_calls"):
            extras += f" tool_calls={len(msg['tool_calls'])}"
        log.info(f"  [{i}] {role}: {content_desc}{extras}")

    # Multi-pass sanitization (handles cascading orphans)
    prev_len = -1
    pass_num = 0
    result = list(messages)

    while len(result) != prev_len and pass_num < 5:
        prev_len = len(result)
        pass_num += 1
        result = _sanitize_single_pass(result, pass_num)

    stripped = len(messages) - len(result)
    if stripped > 0:
        log.info(f"Sanitization complete: stripped {stripped} messages/blocks in {pass_num} passes")

    return result


def _sanitize_single_pass(messages, pass_num):
    """One pass of message sanitization."""
    cleaned = []

    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        content = msg.get("content")

        # Build valid tool IDs from the most recent assistant message IN THE
        # CLEANED list (not the original), so cascading drops are handled.
        prev_tool_ids = set()
        for j in range(len(cleaned) - 1, -1, -1):
            if cleaned[j].get("role") == "assistant":
                prev_tool_ids = _extract_tool_ids_from_message(cleaned[j])
                break

        # --- Handle list content (Anthropic format) ---
        if isinstance(content, list):
            filtered = []
            for block in content:
                if not isinstance(block, dict):
                    filtered.append(block)
                    continue

                # Strip empty/whitespace-only text blocks
                if block.get("type") == "text" and block.get("text", "").strip() == "":
                    log.info(f"  pass {pass_num}: strip empty text block from msg[{i}] ({role})")
                    continue

                # Strip orphaned tool_result blocks
                if block.get("type") == "tool_result":
                    tool_use_id = block.get("tool_use_id")
                    if tool_use_id and tool_use_id not in prev_tool_ids:
                        log.info(f"  pass {pass_num}: strip orphaned tool_result {tool_use_id} from msg[{i}] (prev_ids={prev_tool_ids})")
                        continue

                filtered.append(block)

            if not filtered:
                if role == "assistant":
                    msg = {**msg, "content": filtered}
                else:
                    log.info(f"  pass {pass_num}: drop empty {role} msg[{i}]")
                    continue
            else:
                msg = {**msg, "content": filtered}

        # --- Handle OpenAI tool messages ---
        elif role == "tool":
            tool_call_id = msg.get("tool_call_id")
            if tool_call_id and tool_call_id not in prev_tool_ids:
                log.info(f"  pass {pass_num}: strip orphaned tool msg[{i}] {tool_call_id} (prev_ids={prev_tool_ids})")
                continue

        # --- Handle empty/null string content ---
        elif content is None and role == "assistant" and not msg.get("tool_calls"):
            # Assistant message with null content and no tool_calls — replace
            log.info(f"  pass {pass_num}: replace null assistant content msg[{i}] with placeholder")
            msg = {**msg, "content": "."}
        elif isinstance(content, str) and content.strip() == "":
            if role == "assistant":
                # Can't drop assistant messages (breaks alternation), replace with minimal content
                log.info(f"  pass {pass_num}: replace empty assistant string msg[{i}] with placeholder")
                msg = {**msg, "content": "."}
            else:
                log.info(f"  pass {pass_num}: strip empty string {role} msg[{i}]")
                continue

        cleaned.append(msg)

    return cleaned


# ---------------------------------------------------------------------------
# Response-side fixes
# ---------------------------------------------------------------------------

def remap_tool_call(tool_call):
    """If tool name is 'databricks-tool-call', extract real name from arguments."""
    func = tool_call.get("function", {})
    if func.get("name") != "databricks-tool-call":
        return tool_call

    args_str = func.get("arguments", "")
    try:
        args = json.loads(args_str)
        if isinstance(args, dict) and "name" in args:
            real_name = args.pop("name")
            tool_call = {**tool_call, "function": {
                **func,
                "name": real_name,
                "arguments": json.dumps(args),
            }}
    except (json.JSONDecodeError, TypeError):
        pass  # Can't parse — leave as-is

    return tool_call


def _flatten_content_blocks(content):
    """Collapse an Anthropic-style content array into a plain string.

    Some served models answer chat-completions requests with `content` as a list
    of typed blocks (`[{"type": "text", "text": "..."}]`) instead of a string.
    OpenAI-shaped clients like OpenCode expect a string and render the raw list
    (or drop the message) when handed an array. Non-list input passes through, so
    this is a no-op for well-behaved responses.
    """
    if not isinstance(content, list):
        return content
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def fix_response_data(data):
    """Fix tool names, finish_reason, and content-block arrays in a response."""
    if not isinstance(data, dict):
        return data

    for choice in data.get("choices", []):
        # Non-streaming: choice.message
        message = choice.get("message", {})
        if "content" in message:
            message["content"] = _flatten_content_blocks(message["content"])
        tool_calls = message.get("tool_calls", [])
        if tool_calls:
            message["tool_calls"] = [remap_tool_call(tc) for tc in tool_calls]
            # Fix finish_reason: should be "tool_calls" if tools are invoked
            if choice.get("finish_reason") == "stop" and tool_calls:
                choice["finish_reason"] = "tool_calls"

        # Streaming: choice.delta
        delta = choice.get("delta", {})
        if "content" in delta:
            delta["content"] = _flatten_content_blocks(delta["content"])
        delta_tool_calls = delta.get("tool_calls", [])
        if delta_tool_calls:
            delta["tool_calls"] = [remap_tool_call(tc) for tc in delta_tool_calls]

        # Fix finish_reason for streaming chunks
        if choice.get("finish_reason") == "stop" and delta_tool_calls:
            choice["finish_reason"] = "tool_calls"

    return data


# ---------------------------------------------------------------------------
# SSE stream processing
# ---------------------------------------------------------------------------

class SSEProcessor:
    """Buffers and fixes SSE events, handling tool name remapping across chunks."""

    def __init__(self):
        # Per tool-call-index state for streaming name resolution
        # {index: {"args_buffer": str, "resolved_name": str|None, "buffered_lines": []}}
        self._tool_state = {}
        self._pending_flush = []

    def process_line(self, line):
        """Process one SSE line. Returns list of lines to send (may be empty if buffering)."""
        # Non-data lines pass through immediately
        if not line.startswith("data: "):
            return [line]

        payload = line[6:]  # Strip "data: " prefix

        # [DONE] signal passes through
        if payload.strip() == "[DONE]":
            # Flush any remaining buffered events
            result = list(self._pending_flush)
            self._pending_flush.clear()
            result.append(line)
            return result

        # Parse event JSON
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return [line]  # Can't parse — pass through

        # Check for tool calls that need remapping
        needs_buffering = False
        for choice in data.get("choices", []):
            delta = choice.get("delta", {})
            for tc in delta.get("tool_calls", []):
                idx = tc.get("index", 0)
                func = tc.get("function", {})

                # First chunk with tool name
                if "name" in func:
                    if func["name"] == "databricks-tool-call":
                        self._tool_state[idx] = {
                            "args_buffer": func.get("arguments", ""),
                            "resolved_name": None,
                            "buffered_lines": [],
                        }
                        needs_buffering = True
                    else:
                        # Normal tool name — no remapping needed
                        self._tool_state.pop(idx, None)

                # Argument chunks for a pending tool call
                elif idx in self._tool_state and self._tool_state[idx]["resolved_name"] is None:
                    state = self._tool_state[idx]
                    state["args_buffer"] += func.get("arguments", "")
                    needs_buffering = True

                    # Try to extract the real name from accumulated arguments
                    try:
                        args = json.loads(state["args_buffer"])
                        if isinstance(args, dict) and "name" in args:
                            state["resolved_name"] = args.pop("name")
                            # Rewrite all buffered events with the real name
                            flushed = self._flush_tool_buffer(idx, state["resolved_name"], args)
                            return flushed + [self._rewrite_event_line(line, data)]
                    except json.JSONDecodeError:
                        pass  # Arguments still incomplete — keep buffering

                # Subsequent chunks after name is resolved
                elif idx in self._tool_state and self._tool_state[idx]["resolved_name"]:
                    # Name already resolved — strip "name" from args if present
                    pass  # Just pass through, name was fixed in first event

            # Fix finish_reason
            if choice.get("finish_reason") == "stop":
                # Check if any tool calls were made in this response
                if self._tool_state:
                    choice["finish_reason"] = "tool_calls"

        if needs_buffering:
            # Buffer this event until we can resolve the tool name
            for idx, state in self._tool_state.items():
                if state["resolved_name"] is None:
                    state["buffered_lines"].append(line)
                    return []  # Don't send yet

        # No buffering needed — fix and forward
        fixed = fix_response_data(data)
        return [f"data: {json.dumps(fixed)}"]

    def _flush_tool_buffer(self, idx, real_name, cleaned_args):
        """Rewrite buffered events with the resolved tool name."""
        state = self._tool_state[idx]
        result = []
        for buffered_line in state["buffered_lines"]:
            payload = buffered_line[6:]  # Strip "data: "
            try:
                bdata = json.loads(payload)
                for choice in bdata.get("choices", []):
                    delta = choice.get("delta", {})
                    for tc in delta.get("tool_calls", []):
                        if tc.get("index", 0) == idx:
                            func = tc.get("function", {})
                            if "name" in func and func["name"] == "databricks-tool-call":
                                func["name"] = real_name
                            if "arguments" in func:
                                # Clear arguments in buffered events (we'll send clean args)
                                func["arguments"] = ""
                result.append(f"data: {json.dumps(bdata)}")
            except json.JSONDecodeError:
                result.append(buffered_line)

        state["buffered_lines"].clear()

        # Send the cleaned arguments as a separate event
        args_event = {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": idx,
                        "function": {"arguments": json.dumps(cleaned_args)}
                    }]
                },
                "finish_reason": None
            }]
        }
        result.append(f"data: {json.dumps(args_event)}")
        return result

    def _rewrite_event_line(self, line, data):
        """Rewrite an event line with fixed data."""
        fixed = fix_response_data(data)
        return f"data: {json.dumps(fixed)}"

    def flush_remaining(self):
        """Flush any remaining buffered events (graceful fallback)."""
        result = []
        for idx, state in self._tool_state.items():
            for buffered_line in state["buffered_lines"]:
                result.append(buffered_line)
            state["buffered_lines"].clear()
        result.extend(self._pending_flush)
        self._pending_flush.clear()
        return result


def _decode_sse_line(raw_line: bytes | str) -> str:
    """Decode SSE bytes as UTF-8 instead of requests' Latin-1 HTTP default."""
    if isinstance(raw_line, bytes):
        return raw_line.decode("utf-8")
    return raw_line


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle concurrent requests (e.g., health checks during streaming)."""
    daemon_threads = True


class ProxyHandler(BaseHTTPRequestHandler):
    """Proxy that sanitizes requests and fixes responses."""

    def do_POST(self):
        import time as _time
        _t_start = _time.monotonic()
        _req_data = None  # parsed request, reused for tracing (spec-D)
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        log.info(f"POST {self.path} ({content_length} bytes)")

        # --- Sanitize request ---
        try:
            data = json.loads(body)
            _req_data = data  # keep for spec-D tracing
            if "messages" in data:
                before = len(data["messages"])
                data["messages"] = sanitize_messages(data["messages"])
                after = len(data["messages"])
                if before != after:
                    log.info(f"Messages: {before} -> {after}")
            # Strip unsupported schema keys from tool definitions (all models)
            data = sanitize_tool_schemas(data)
            # Strip sampling params that specific served models reject. Some callers
            # (e.g. an agent's title-generation sub-call) send `temperature`, but the
            # gateway-served `(global.)anthropic.claude-opus-4-8` returns 400
            # "does not support the temperature parameter" — surfaced via tracing
            # 2026-07-12. Model-targeted (like the Gemini key stripping), tolerant of
            # the gateway's `global.`/`databricks-` name prefixes.
            data = strip_unsupported_sampling_params(data)
            body = json.dumps(data).encode()
        except (json.JSONDecodeError, KeyError) as e:
            log.warning(f"Could not parse request body: {e}")
            pass  # Forward as-is if not valid JSON

        # Build upstream URL, routing by request PROTOCOL.
        #
        # The proxy serves multiple agents that speak DIFFERENT model-API dialects
        # over one port, and the AI Gateway exposes a different route per dialect:
        #   - OpenCode (@ai-sdk/openai-compatible) + Hermes → OpenAI-style paths
        #     (`/chat/completions`), which the configured UPSTREAM_BASE (`.../mlflow/v1`)
        #     accepts. Verified working (traces land).
        #   - Pi (anthropic-messages) → `/v1/messages`, which the gateway serves ONLY
        #     at `.../anthropic/v1` (verified live: `.../anthropic/v1/messages` → 200,
        #     `.../mlflow/v1/messages` → 400 "doesn't match any known API type").
        # A single UPSTREAM_BASE cannot serve both, so detect the Anthropic Messages
        # protocol by its path and swap to the `/anthropic` gateway base for it. Derive
        # that base from UPSTREAM_BASE by replacing the trailing `/mlflow/v1` service
        # segment, so it tracks whatever gateway host is configured.
        if self.path.startswith("/v1/messages") or self.path.startswith("/v1/complete"):
            gw_root = UPSTREAM_BASE.rstrip("/")
            for suffix in ("/mlflow/v1", "/mlflow", "/serving-endpoints"):
                if gw_root.endswith(suffix):
                    gw_root = gw_root[: -len(suffix)]
                    break
            upstream_url = gw_root + "/anthropic" + self.path  # → /anthropic/v1/messages
        else:
            upstream_url = UPSTREAM_BASE + self.path

        # Forward headers (inject fresh token to survive PAT rotation)
        headers = {}
        for key in self.headers:
            if key.lower() not in ("host", "content-length", "transfer-encoding"):
                headers[key] = self.headers[key]
        headers["Content-Length"] = str(len(body))

        # Override auth with fresh token from disk — OpenCode's cached token
        # goes stale after PAT rotation since it's a long-lived TUI process
        fresh_token = _get_fresh_token()
        if fresh_token:
            headers["Authorization"] = f"Bearer {fresh_token}"

        # Detect streaming
        is_stream = False
        try:
            is_stream = json.loads(body).get("stream", False)
        except Exception:
            pass

        try:
            resp = requests.post(
                upstream_url,
                data=body,
                headers=headers,
                stream=is_stream,
                timeout=300,
            )

            if resp.status_code >= 400:
                log_upstream_error(resp, self.path)

            # --- Non-streaming response ---
            if not is_stream:
                # Fix response
                try:
                    resp_data = resp.json()
                    resp_data = fix_response_data(resp_data)
                    resp_body = json.dumps(resp_data).encode()
                except (json.JSONDecodeError, ValueError):
                    resp_body = resp.content

                self.send_response(resp.status_code)
                for key, value in resp.headers.items():
                    if key.lower() not in ("transfer-encoding", "content-encoding", "content-length"):
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
                # spec-D: fire-and-forget span AFTER the response is sent (D-N1).
                _trace_proxy_request(
                    path=self.path, req=_req_data, headers=self.headers,
                    resp_body=resp_body, status=resp.status_code, t_start=_t_start,
                )
                return

            # --- Streaming response ---
            self.send_response(resp.status_code)
            for key, value in resp.headers.items():
                if key.lower() not in ("transfer-encoding", "content-encoding", "content-length"):
                    self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            processor = SSEProcessor()

            # Accumulate the assembled response as SSE chunks stream past, so the
            # trace can record the model's reply (not just null). We already iterate
            # every line here; sniffing the text/stop_reason deltas is cheap and stays
            # OFF the client's critical path (we only build strings). Fully guarded —
            # any reconstruction error must never break the client stream (D-N4).
            # Per-content-block text accumulators (keyed by block index) so multiple
            # Anthropic text blocks stay separated instead of running together, plus
            # tool_use capture (input_json_delta) so tool-only turns aren't blank.
            _blocks: dict[int, dict] = {}      # index -> {"type","text"|"name"+"input"}
            _oai_parts: list[str] = []         # OpenAI content deltas (single stream)
            _stop_reason = None
            _usage: dict = {}

            def _sniff_stream_event(line_str):  # noqa: ANN001
                nonlocal _stop_reason  # must precede any use of the name
                if not line_str.startswith("data:"):
                    return
                payload = line_str[5:].strip()
                if not payload or payload == "[DONE]":
                    return
                try:
                    evt = json.loads(payload)
                except Exception:  # noqa: BLE001
                    return

                # --- Anthropic Messages streaming ---
                idx = evt.get("index")
                etype = evt.get("type")
                if etype == "content_block_start" and idx is not None:
                    cb = evt.get("content_block") or {}
                    if cb.get("type") == "tool_use":
                        _blocks[idx] = {"type": "tool_use", "name": cb.get("name"),
                                        "input": ""}
                    else:
                        _blocks[idx] = {"type": "text", "text": ""}
                delta = evt.get("delta") or {}
                if isinstance(delta, dict):
                    # text_delta (assistant text) / input_json_delta (tool args)
                    if delta.get("text") and idx is not None:
                        _blocks.setdefault(idx, {"type": "text", "text": ""})
                        _blocks[idx]["text"] = _blocks[idx].get("text", "") + delta["text"]
                    if delta.get("partial_json") is not None and idx is not None:
                        _blocks.setdefault(idx, {"type": "tool_use", "name": None, "input": ""})
                        _blocks[idx]["input"] = _blocks[idx].get("input", "") + delta["partial_json"]
                    # stop_reason arrives on the top-level message_delta.delta
                    if delta.get("stop_reason"):
                        _stop_reason = delta["stop_reason"]
                # usage is SPLIT across message_start (input_tokens) and
                # message_delta (output_tokens) — MERGE, don't overwrite (review #1).
                if evt.get("usage"):
                    _usage.update(evt["usage"])
                msg = evt.get("message") or {}
                if isinstance(msg, dict) and msg.get("usage"):
                    _usage.update(msg["usage"])

                # --- OpenAI Chat Completions streaming ---
                for ch in evt.get("choices", []) or []:
                    cd = (ch or {}).get("delta") or {}
                    if cd.get("content"):
                        _oai_parts.append(cd["content"])
                    if ch.get("finish_reason"):
                        _stop_reason = ch["finish_reason"]

            for raw_line in resp.iter_lines(decode_unicode=False):
                if raw_line is None:
                    continue

                line = _decode_sse_line(raw_line).strip()

                if not line:
                    # Blank line = event boundary, send it
                    self._send_chunk(b"\r\n")
                    continue

                try:
                    _sniff_stream_event(line)
                except Exception:  # noqa: BLE001 — tracing sniff must never break the stream
                    pass

                # Process through SSE fixer
                output_lines = processor.process_line(line)
                for out_line in output_lines:
                    self._send_chunk((out_line + "\r\n").encode())

            # Flush any remaining buffered events
            for remaining in processor.flush_remaining():
                self._send_chunk((remaining + "\r\n").encode())

            # Send final zero-length chunk to end chunked transfer
            self._send_chunk(b"")

            # spec-D: fire-and-forget span after the stream completes (D-N2).
            # Reconstruct the streamed response so the trace carries the model's reply.
            # Build a content[] that preserves block structure + tool_use (review #3):
            # each Anthropic text block stays a separate {type:text} entry, and tool
            # calls surface as {type:tool_use} with the accumulated JSON args — so a
            # tool-only turn is no longer a blank trace. OpenAI deltas collapse to one
            # text block (that dialect has no block indices).
            _content = []
            for _i in sorted(_blocks):
                b = _blocks[_i]
                if b.get("type") == "tool_use":
                    _content.append({"type": "tool_use", "name": b.get("name"),
                                     "input": b.get("input", "")})
                elif b.get("text"):
                    _content.append({"type": "text", "text": b["text"]})
            if _oai_parts:
                _content.append({"type": "text", "text": "".join(_oai_parts)})
            _stream_resp = {"content": _content, "stop_reason": _stop_reason}
            if _usage:
                _stream_resp["usage"] = _usage
            _trace_proxy_request(
                path=self.path, req=_req_data, headers=self.headers,
                resp_body=json.dumps(_stream_resp).encode(), status=resp.status_code,
                t_start=_t_start,
            )

        except requests.exceptions.ConnectionError as e:
            self.send_error(502, f"Upstream connection failed: {e}")
        except requests.exceptions.Timeout:
            self.send_error(504, "Upstream timeout")

    def _send_chunk(self, data):
        """Send a chunk in HTTP chunked transfer encoding."""
        if data:
            chunk = f"{len(data):x}\r\n".encode() + data + b"\r\n"
        else:
            chunk = b"0\r\n\r\n"  # Final chunk
        try:
            self.wfile.write(chunk)
            self.wfile.flush()
        except BrokenPipeError:
            pass

    def do_GET(self):
        """Bounded zero-inference readiness endpoint."""
        if self.path == "/health":
            status, payload = _readiness_status()
            body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        """Suppress per-request logging to keep container logs clean."""
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not UPSTREAM_BASE:
        print("Error: PROXY_UPSTREAM_BASE environment variable is required", file=sys.stderr)
        sys.exit(1)

    server = ThreadedHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    print(f"Content-filter proxy listening on {LISTEN_HOST}:{LISTEN_PORT}")
    print(f"Forwarding to: {UPSTREAM_BASE}")
    print("Fixes: empty text blocks, orphaned tool_results, tool name remapping, finish_reason")
    sys.stdout.flush()
    server.serve_forever()
