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

import requests

UPSTREAM_BASE = os.environ.get("PROXY_UPSTREAM_BASE", "")
LISTEN_HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("PROXY_PORT", "4000"))

# ---------------------------------------------------------------------------
# Fresh token injection — survives PAT rotation
# ---------------------------------------------------------------------------
# The PAT rotator writes the latest token to ~/.databrickscfg every rotation.
# OpenCode (and this proxy) are separate processes with frozen env snapshots,
# so we read the file on-demand instead of trusting os.environ.

_TOKEN_CACHE: dict = {"token": None, "read_at": 0.0}
_TOKEN_CACHE_TTL = 30  # seconds — short enough to pick up rotations quickly

_HOME = os.environ.get("HOME", "/app/python/source_code")
if not _HOME or _HOME == "/":
    _HOME = "/app/python/source_code"
_DATABRICKSCFG_PATH = os.path.join(_HOME, ".databrickscfg")


def _get_fresh_token() -> str | None:
    """Read current token from ~/.databrickscfg (updated by PAT rotator).

    Returns cached value if read within the last _TOKEN_CACHE_TTL seconds.
    """
    now = time.time()
    if _TOKEN_CACHE["token"] and (now - _TOKEN_CACHE["read_at"]) < _TOKEN_CACHE_TTL:
        return _TOKEN_CACHE["token"]

    try:
        config = configparser.ConfigParser()
        config.read(_DATABRICKSCFG_PATH)
        token = config.get("DEFAULT", "token", fallback=None)
        if token:
            _TOKEN_CACHE["token"] = token
            _TOKEN_CACHE["read_at"] = now
            return token
    except Exception as e:
        log.warning(f"Could not read fresh token from {_DATABRICKSCFG_PATH}: {e}")

    return _TOKEN_CACHE.get("token")  # stale is better than nothing


# Diagnostic logging — writes to stderr which goes to ~/.content-filter-proxy.log
log = logging.getLogger("content-filter-proxy")
log.setLevel(logging.INFO)
if not log.handlers:
    _sh = logging.StreamHandler(sys.stderr)
    _sh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(_sh)

# JSON Schema keywords that Gemini doesn't support
GEMINI_UNSUPPORTED_SCHEMA_KEYS = {
    "$schema", "$ref", "$defs", "$id", "$comment", "additionalProperties",
}

# Top-level request fields that Gemini doesn't support
GEMINI_UNSUPPORTED_REQUEST_KEYS = {
    "stream_options",
}


# ---------------------------------------------------------------------------
# Gemini compatibility
# ---------------------------------------------------------------------------

def strip_unsupported_schema_keys(obj):
    """Recursively strip JSON Schema keywords that Gemini doesn't support."""
    if isinstance(obj, dict):
        return {
            k: strip_unsupported_schema_keys(v)
            for k, v in obj.items()
            if k not in GEMINI_UNSUPPORTED_SCHEMA_KEYS
        }
    elif isinstance(obj, list):
        return [strip_unsupported_schema_keys(item) for item in obj]
    return obj


def sanitize_tool_schemas(data):
    """Strip JSON Schema keywords that some providers reject.

    Applied universally — $schema, additionalProperties etc. are never
    required by any downstream API. Claude/GPT ignore them, Gemini rejects them.
    Stripping for all models is safe and avoids model detection issues.
    """
    tools = data.get("tools", [])
    if not tools:
        return data

    for tool in tools:
        func = tool.get("function", {})
        if "parameters" in func:
            func["parameters"] = strip_unsupported_schema_keys(func["parameters"])

    # Strip unsupported top-level fields
    for key in GEMINI_UNSUPPORTED_REQUEST_KEYS:
        if key in data:
            log.info(f"  Stripped top-level field: {key}")
            del data[key]

    # Strip $schema from top level if present
    data.pop("$schema", None)

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


def fix_response_data(data):
    """Fix tool names and finish_reason in a parsed response object."""
    if not isinstance(data, dict):
        return data

    for choice in data.get("choices", []):
        # Non-streaming: choice.message
        message = choice.get("message", {})
        tool_calls = message.get("tool_calls", [])
        if tool_calls:
            message["tool_calls"] = [remap_tool_call(tc) for tc in tool_calls]
            # Fix finish_reason: should be "tool_calls" if tools are invoked
            if choice.get("finish_reason") == "stop" and tool_calls:
                choice["finish_reason"] = "tool_calls"

        # Streaming: choice.delta
        delta = choice.get("delta", {})
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


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle concurrent requests (e.g., health checks during streaming)."""
    daemon_threads = True


class ProxyHandler(BaseHTTPRequestHandler):
    """Proxy that sanitizes requests and fixes responses."""

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        log.info(f"POST {self.path} ({content_length} bytes)")

        # --- Sanitize request ---
        try:
            data = json.loads(body)
            if "messages" in data:
                before = len(data["messages"])
                data["messages"] = sanitize_messages(data["messages"])
                after = len(data["messages"])
                if before != after:
                    log.info(f"Messages: {before} -> {after}")
            # Strip unsupported schema keys from tool definitions (all models)
            data = sanitize_tool_schemas(data)
            body = json.dumps(data).encode()
        except (json.JSONDecodeError, KeyError) as e:
            log.warning(f"Could not parse request body: {e}")
            pass  # Forward as-is if not valid JSON

        # Build upstream URL
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

            # Log upstream errors
            if resp.status_code >= 400:
                log.error(f"Upstream returned {resp.status_code}: {resp.text[:500]}")

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
                return

            # --- Streaming response ---
            self.send_response(resp.status_code)
            for key, value in resp.headers.items():
                if key.lower() not in ("transfer-encoding", "content-encoding", "content-length"):
                    self.send_header(key, value)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            processor = SSEProcessor()

            for raw_line in resp.iter_lines(decode_unicode=True):
                if raw_line is None:
                    continue

                line = raw_line.strip() if isinstance(raw_line, str) else raw_line.decode().strip()

                if not line:
                    # Blank line = event boundary, send it
                    self._send_chunk(b"\r\n")
                    continue

                # Process through SSE fixer
                output_lines = processor.process_line(line)
                for out_line in output_lines:
                    self._send_chunk((out_line + "\r\n").encode())

            # Flush any remaining buffered events
            for remaining in processor.flush_remaining():
                self._send_chunk((remaining + "\r\n").encode())

            # Send final zero-length chunk to end chunked transfer
            self._send_chunk(b"")

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
        """Health check endpoint."""
        if self.path == "/health":
            body = json.dumps({"status": "ok", "upstream": UPSTREAM_BASE}).encode()
            self.send_response(200)
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
    print(f"Fixes: empty text blocks, orphaned tool_results, tool name remapping, finish_reason")
    sys.stdout.flush()
    server.serve_forever()
