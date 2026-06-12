"""Tests for content_filter_proxy — request/response sanitization for OpenCode."""

import json
import time

import pytest
from unittest import mock


# ---------------------------------------------------------------------------
# strip_unsupported_schema_keys
# ---------------------------------------------------------------------------

class TestStripUnsupportedSchemaKeys:
    def test_strips_top_level_keys(self):
        from content_filter_proxy import strip_unsupported_schema_keys
        obj = {"type": "object", "$schema": "http://...", "additionalProperties": False, "title": "Foo"}
        result = strip_unsupported_schema_keys(obj)
        assert result == {"type": "object", "title": "Foo"}

    def test_strips_nested_keys(self):
        from content_filter_proxy import strip_unsupported_schema_keys
        obj = {
            "type": "object",
            "properties": {
                "name": {"type": "string", "$ref": "#/defs/Name", "$comment": "ignore"},
            },
        }
        result = strip_unsupported_schema_keys(obj)
        assert result == {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
            },
        }

    def test_strips_inside_lists(self):
        from content_filter_proxy import strip_unsupported_schema_keys
        obj = [{"$id": "x", "type": "string"}, {"type": "int"}]
        result = strip_unsupported_schema_keys(obj)
        assert result == [{"type": "string"}, {"type": "int"}]

    def test_passes_through_primitives(self):
        from content_filter_proxy import strip_unsupported_schema_keys
        assert strip_unsupported_schema_keys("hello") == "hello"
        assert strip_unsupported_schema_keys(42) == 42
        assert strip_unsupported_schema_keys(None) is None


# ---------------------------------------------------------------------------
# sanitize_tool_schemas
# ---------------------------------------------------------------------------

class TestSanitizeToolSchemas:
    def test_cleans_tool_parameters(self):
        from content_filter_proxy import sanitize_tool_schemas
        data = {
            "tools": [
                {"function": {"name": "foo", "parameters": {"$schema": "x", "type": "object"}}},
            ],
        }
        result = sanitize_tool_schemas(data)
        assert result["tools"][0]["function"]["parameters"] == {"type": "object"}

    def test_strips_top_level_request_keys(self):
        from content_filter_proxy import sanitize_tool_schemas
        data = {
            "tools": [{"function": {"name": "foo", "parameters": {"type": "object"}}}],
            "stream_options": {"include_usage": True},
            "$schema": "x",
        }
        result = sanitize_tool_schemas(data)
        assert "stream_options" not in result
        assert "$schema" not in result

    def test_no_tools_is_noop(self):
        from content_filter_proxy import sanitize_tool_schemas
        data = {"messages": [{"role": "user", "content": "hi"}]}
        result = sanitize_tool_schemas(data)
        assert result == data


# ---------------------------------------------------------------------------
# _extract_tool_ids_from_message
# ---------------------------------------------------------------------------

class TestExtractToolIds:
    def test_anthropic_format(self):
        from content_filter_proxy import _extract_tool_ids_from_message
        msg = {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_1", "name": "bash"},
                {"type": "text", "text": "running..."},
                {"type": "tool_use", "id": "tu_2", "name": "read"},
            ],
        }
        assert _extract_tool_ids_from_message(msg) == {"tu_1", "tu_2"}

    def test_openai_format(self):
        from content_filter_proxy import _extract_tool_ids_from_message
        msg = {
            "role": "assistant",
            "tool_calls": [
                {"id": "tc_1", "function": {"name": "bash"}},
                {"id": "tc_2", "function": {"name": "read"}},
            ],
        }
        assert _extract_tool_ids_from_message(msg) == {"tc_1", "tc_2"}

    def test_no_tools(self):
        from content_filter_proxy import _extract_tool_ids_from_message
        msg = {"role": "assistant", "content": "hello"}
        assert _extract_tool_ids_from_message(msg) == set()


# ---------------------------------------------------------------------------
# _extract_tool_refs_from_message
# ---------------------------------------------------------------------------

class TestExtractToolRefs:
    def test_anthropic_tool_result(self):
        from content_filter_proxy import _extract_tool_refs_from_message
        msg = {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"},
            ],
        }
        assert _extract_tool_refs_from_message(msg) == {"tu_1"}

    def test_openai_tool_message(self):
        from content_filter_proxy import _extract_tool_refs_from_message
        msg = {"role": "tool", "tool_call_id": "tc_1", "content": "result"}
        assert _extract_tool_refs_from_message(msg) == {"tc_1"}

    def test_no_refs(self):
        from content_filter_proxy import _extract_tool_refs_from_message
        msg = {"role": "user", "content": "hi"}
        assert _extract_tool_refs_from_message(msg) == set()


# ---------------------------------------------------------------------------
# sanitize_messages — the big one
# ---------------------------------------------------------------------------

class TestSanitizeMessages:
    def test_strips_empty_text_blocks(self):
        from content_filter_proxy import sanitize_messages
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "hello"},
                {"type": "text", "text": ""},
                {"type": "text", "text": "   "},
            ]},
        ]
        result = sanitize_messages(messages)
        assert len(result) == 1
        assert len(result[0]["content"]) == 1
        assert result[0]["content"][0]["text"] == "hello"

    def test_strips_orphaned_tool_result_anthropic(self):
        """tool_result referencing a tool_use ID that doesn't exist in prev assistant msg."""
        from content_filter_proxy import sanitize_messages
        messages = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tu_1", "name": "bash"},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"},
                {"type": "tool_result", "tool_use_id": "tu_ORPHAN", "content": "stale"},
            ]},
        ]
        result = sanitize_messages(messages)
        assert len(result) == 2
        # Only tu_1 should survive
        user_blocks = result[1]["content"]
        assert len(user_blocks) == 1
        assert user_blocks[0]["tool_use_id"] == "tu_1"

    def test_strips_orphaned_openai_tool_message(self):
        from content_filter_proxy import sanitize_messages
        messages = [
            {"role": "assistant", "tool_calls": [{"id": "tc_1", "function": {"name": "bash"}}]},
            {"role": "tool", "tool_call_id": "tc_1", "content": "ok"},
            {"role": "tool", "tool_call_id": "tc_ORPHAN", "content": "stale"},
        ]
        result = sanitize_messages(messages)
        assert len(result) == 2
        assert result[1]["role"] == "tool"
        assert result[1]["tool_call_id"] == "tc_1"

    def test_cascading_orphan_removal(self):
        """Dropping one message can make the next one orphaned too — multi-pass."""
        from content_filter_proxy import sanitize_messages
        messages = [
            # assistant with tool_use tu_A
            {"role": "assistant", "content": [{"type": "tool_use", "id": "tu_A", "name": "bash"}]},
            # user responds to tu_A
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_A", "content": "ok"}]},
            # assistant with tool_use tu_B (referencing something dropped)
            {"role": "assistant", "content": [{"type": "tool_use", "id": "tu_B", "name": "read"}]},
            # user responds to tu_B AND orphan tu_C (no matching tool_use)
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_B", "content": "ok"},
                {"type": "tool_result", "tool_use_id": "tu_C", "content": "orphan"},
            ]},
        ]
        result = sanitize_messages(messages)
        # tu_C should be stripped, tu_A and tu_B should survive
        assert len(result) == 4
        last_user_blocks = result[3]["content"]
        assert len(last_user_blocks) == 1
        assert last_user_blocks[0]["tool_use_id"] == "tu_B"

    def test_drops_empty_user_message_after_filter(self):
        """If all content blocks are stripped, the user message is dropped entirely."""
        from content_filter_proxy import sanitize_messages
        messages = [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "tu_1", "name": "bash"}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_ORPHAN", "content": "stale"},
            ]},
        ]
        result = sanitize_messages(messages)
        # The user message should be dropped (all blocks were orphaned)
        assert len(result) == 1
        assert result[0]["role"] == "assistant"

    def test_keeps_empty_assistant_message(self):
        """Empty assistant messages are kept (not dropped) to preserve alternation."""
        from content_filter_proxy import sanitize_messages
        messages = [
            {"role": "assistant", "content": [{"type": "text", "text": ""}]},
        ]
        result = sanitize_messages(messages)
        assert len(result) == 1
        assert result[0]["role"] == "assistant"

    def test_replaces_null_assistant_content(self):
        from content_filter_proxy import sanitize_messages
        messages = [
            {"role": "assistant", "content": None},
        ]
        result = sanitize_messages(messages)
        assert result[0]["content"] == "."

    def test_replaces_empty_string_assistant(self):
        from content_filter_proxy import sanitize_messages
        messages = [
            {"role": "assistant", "content": "   "},
        ]
        result = sanitize_messages(messages)
        assert result[0]["content"] == "."

    def test_strips_empty_string_user(self):
        from content_filter_proxy import sanitize_messages
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": ""},
        ]
        result = sanitize_messages(messages)
        assert len(result) == 2  # empty user dropped

    def test_passthrough_non_list(self):
        from content_filter_proxy import sanitize_messages
        assert sanitize_messages("not a list") == "not a list"
        assert sanitize_messages(None) is None

    def test_preserves_non_dict_blocks(self):
        """Non-dict items in content list are preserved as-is."""
        from content_filter_proxy import sanitize_messages
        messages = [
            {"role": "user", "content": ["plain string", {"type": "text", "text": "hi"}]},
        ]
        result = sanitize_messages(messages)
        assert len(result[0]["content"]) == 2

    def test_null_assistant_with_tool_calls_not_replaced(self):
        """Assistant msg with null content but tool_calls should NOT get placeholder."""
        from content_filter_proxy import sanitize_messages
        messages = [
            {"role": "assistant", "content": None, "tool_calls": [{"id": "tc_1"}]},
        ]
        result = sanitize_messages(messages)
        assert result[0]["content"] is None  # preserved because tool_calls exist


# ---------------------------------------------------------------------------
# remap_tool_call
# ---------------------------------------------------------------------------

class TestRemapToolCall:
    def test_remaps_databricks_tool_call(self):
        from content_filter_proxy import remap_tool_call
        tc = {
            "id": "tc_1",
            "function": {
                "name": "databricks-tool-call",
                "arguments": json.dumps({"name": "execute_sql", "query": "SELECT 1"}),
            },
        }
        result = remap_tool_call(tc)
        assert result["function"]["name"] == "execute_sql"
        args = json.loads(result["function"]["arguments"])
        assert "name" not in args
        assert args["query"] == "SELECT 1"

    def test_passthrough_normal_tool(self):
        from content_filter_proxy import remap_tool_call
        tc = {"id": "tc_1", "function": {"name": "bash", "arguments": '{"cmd": "ls"}'}}
        result = remap_tool_call(tc)
        assert result["function"]["name"] == "bash"

    def test_handles_invalid_json_args(self):
        from content_filter_proxy import remap_tool_call
        tc = {"id": "tc_1", "function": {"name": "databricks-tool-call", "arguments": "not json"}}
        result = remap_tool_call(tc)
        assert result["function"]["name"] == "databricks-tool-call"  # unchanged


# ---------------------------------------------------------------------------
# fix_response_data
# ---------------------------------------------------------------------------

class TestFixResponseData:
    def test_remaps_tool_calls_in_message(self):
        from content_filter_proxy import fix_response_data
        data = {
            "choices": [{
                "message": {
                    "tool_calls": [{
                        "id": "tc_1",
                        "function": {
                            "name": "databricks-tool-call",
                            "arguments": json.dumps({"name": "run_sql", "q": "SELECT 1"}),
                        },
                    }],
                },
                "finish_reason": "stop",
            }],
        }
        result = fix_response_data(data)
        assert result["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "run_sql"
        assert result["choices"][0]["finish_reason"] == "tool_calls"

    def test_fixes_streaming_delta(self):
        from content_filter_proxy import fix_response_data
        data = {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "id": "tc_1",
                        "function": {
                            "name": "databricks-tool-call",
                            "arguments": json.dumps({"name": "run_sql"}),
                        },
                    }],
                },
                "finish_reason": "stop",
            }],
        }
        result = fix_response_data(data)
        assert result["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "run_sql"
        assert result["choices"][0]["finish_reason"] == "tool_calls"

    def test_noop_on_non_dict(self):
        from content_filter_proxy import fix_response_data
        assert fix_response_data("string") == "string"
        assert fix_response_data(None) is None

    def test_no_choices_is_noop(self):
        from content_filter_proxy import fix_response_data
        data = {"id": "resp_1"}
        assert fix_response_data(data) == data


# ---------------------------------------------------------------------------
# SSEProcessor
# ---------------------------------------------------------------------------

class TestSSEProcessor:
    def test_passthrough_non_data_lines(self):
        from content_filter_proxy import SSEProcessor
        proc = SSEProcessor()
        assert proc.process_line("event: message") == ["event: message"]
        assert proc.process_line(": comment") == [": comment"]

    def test_passthrough_done_signal(self):
        from content_filter_proxy import SSEProcessor
        proc = SSEProcessor()
        result = proc.process_line("data: [DONE]")
        assert "data: [DONE]" in result

    def test_passthrough_normal_tool(self):
        from content_filter_proxy import SSEProcessor
        proc = SSEProcessor()
        event = {
            "choices": [{
                "delta": {"tool_calls": [{"index": 0, "function": {"name": "bash"}}]},
                "finish_reason": None,
            }],
        }
        result = proc.process_line(f"data: {json.dumps(event)}")
        assert len(result) == 1
        assert "bash" in result[0]

    def test_buffers_databricks_tool_call(self):
        """First chunk with databricks-tool-call name should be buffered."""
        from content_filter_proxy import SSEProcessor
        proc = SSEProcessor()
        event = {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {"name": "databricks-tool-call", "arguments": ""},
                    }],
                },
                "finish_reason": None,
            }],
        }
        result = proc.process_line(f"data: {json.dumps(event)}")
        assert result == []  # buffered, not sent

    def test_resolves_name_from_args(self):
        """Once args JSON is complete, name is resolved and buffered events flushed."""
        from content_filter_proxy import SSEProcessor
        proc = SSEProcessor()
        # First chunk — name is databricks-tool-call
        event1 = {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {"name": "databricks-tool-call", "arguments": ""},
                    }],
                },
                "finish_reason": None,
            }],
        }
        proc.process_line(f"data: {json.dumps(event1)}")

        # Second chunk — args with real name
        event2 = {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {"arguments": json.dumps({"name": "execute_sql", "query": "SELECT 1"})},
                    }],
                },
                "finish_reason": None,
            }],
        }
        result = proc.process_line(f"data: {json.dumps(event2)}")
        # Should flush buffered events + current event
        assert len(result) >= 1
        # The resolved name should appear in flushed output
        combined = " ".join(result)
        assert "execute_sql" in combined

    def test_flush_remaining(self):
        from content_filter_proxy import SSEProcessor
        proc = SSEProcessor()
        # Buffer a databricks-tool-call but never resolve it
        event = {
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "function": {"name": "databricks-tool-call", "arguments": '{"partial'},
                    }],
                },
                "finish_reason": None,
            }],
        }
        proc.process_line(f"data: {json.dumps(event)}")
        remaining = proc.flush_remaining()
        assert len(remaining) >= 1  # buffered lines flushed as-is

    def test_fixes_finish_reason_on_stop(self):
        """finish_reason 'stop' with active tool state should become 'tool_calls'."""
        from content_filter_proxy import SSEProcessor
        proc = SSEProcessor()
        # Seed tool state
        proc._tool_state[0] = {"args_buffer": "", "resolved_name": "bash", "buffered_lines": []}
        event = {
            "choices": [{"delta": {}, "finish_reason": "stop"}],
        }
        result = proc.process_line(f"data: {json.dumps(event)}")
        parsed = json.loads(result[0][6:])  # strip "data: "
        assert parsed["choices"][0]["finish_reason"] == "tool_calls"

    def test_invalid_json_passthrough(self):
        from content_filter_proxy import SSEProcessor
        proc = SSEProcessor()
        result = proc.process_line("data: {invalid json}")
        assert result == ["data: {invalid json}"]


# ---------------------------------------------------------------------------
# _get_fresh_token
# ---------------------------------------------------------------------------

class TestGetFreshToken:
    def setup_method(self):
        """Reset token cache before each test."""
        from content_filter_proxy import _TOKEN_CACHE
        _TOKEN_CACHE["token"] = None
        _TOKEN_CACHE["read_at"] = 0.0

    def test_reads_from_databrickscfg(self, tmp_path):
        from content_filter_proxy import _get_fresh_token, _TOKEN_CACHE
        cfg = tmp_path / ".databrickscfg"
        cfg.write_text("[DEFAULT]\nhost = https://test.cloud.databricks.com\ntoken = dapi_test123\n")
        with mock.patch("content_filter_proxy._DATABRICKSCFG_PATH", str(cfg)):
            token = _get_fresh_token()
        assert token == "dapi_test123"
        assert _TOKEN_CACHE["token"] == "dapi_test123"

    def test_returns_cached_within_ttl(self, tmp_path):
        from content_filter_proxy import _get_fresh_token, _TOKEN_CACHE
        _TOKEN_CACHE["token"] = "cached_token"
        _TOKEN_CACHE["read_at"] = time.time()  # just now
        # Even with a bad path, should return cached
        with mock.patch("content_filter_proxy._DATABRICKSCFG_PATH", "/nonexistent"):
            token = _get_fresh_token()
        assert token == "cached_token"

    def test_refreshes_after_ttl(self, tmp_path):
        from content_filter_proxy import _get_fresh_token, _TOKEN_CACHE
        _TOKEN_CACHE["token"] = "old_token"
        _TOKEN_CACHE["read_at"] = time.time() - 60  # expired
        cfg = tmp_path / ".databrickscfg"
        cfg.write_text("[DEFAULT]\nhost = https://test.cloud.databricks.com\ntoken = new_token\n")
        with mock.patch("content_filter_proxy._DATABRICKSCFG_PATH", str(cfg)):
            token = _get_fresh_token()
        assert token == "new_token"

    def test_returns_stale_on_read_error(self, tmp_path):
        from content_filter_proxy import _get_fresh_token, _TOKEN_CACHE
        _TOKEN_CACHE["token"] = "stale_token"
        _TOKEN_CACHE["read_at"] = 0.0  # force re-read
        with mock.patch("content_filter_proxy._DATABRICKSCFG_PATH", "/nonexistent"):
            token = _get_fresh_token()
        assert token == "stale_token"

    def test_returns_none_when_no_cache_and_no_file(self):
        from content_filter_proxy import _get_fresh_token, _TOKEN_CACHE
        _TOKEN_CACHE["token"] = None
        _TOKEN_CACHE["read_at"] = 0.0
        with mock.patch("content_filter_proxy._DATABRICKSCFG_PATH", "/nonexistent"):
            token = _get_fresh_token()
        assert token is None


# ---------------------------------------------------------------------------
# strip_unsupported_openai_params — PR #67 review: OpenCode sends
# reasoning_effort after a model switch; Databricks chat-completions rejects
# unknown params with 400 "Extra inputs are not permitted".
# ---------------------------------------------------------------------------

class TestStripUnsupportedOpenAIParams:
    def test_strips_reasoning_effort(self):
        from content_filter_proxy import strip_unsupported_openai_params
        data = {
            "model": "databricks-claude-opus-4-7",
            "messages": [{"role": "user", "content": "hi"}],
            "reasoning_effort": "medium",
        }
        result = strip_unsupported_openai_params(data)
        assert "reasoning_effort" not in result

    def test_strips_reasoning_and_verbosity(self):
        from content_filter_proxy import strip_unsupported_openai_params
        data = {"model": "m", "messages": [], "reasoning": {"effort": "high"}, "verbosity": "low"}
        result = strip_unsupported_openai_params(data)
        assert "reasoning" not in result
        assert "verbosity" not in result

    def test_preserves_supported_params(self):
        from content_filter_proxy import strip_unsupported_openai_params
        data = {
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.5,
            "max_tokens": 100,
            "stream": True,
            "tools": [],
            "tool_choice": "auto",
        }
        result = strip_unsupported_openai_params(dict(data))
        assert result == data


# ---------------------------------------------------------------------------
# sanitize_gemini_contents — PR #67 review: gemini-cli >= 0.4x attaches `id`
# to functionCall/functionResponse parts; the Databricks /gemini route rejects
# them with 400 'Unknown name "id" at contents[].parts[].function_call'.
# ---------------------------------------------------------------------------

class TestSanitizeGeminiContents:
    def test_strips_id_from_function_call_camel(self):
        from content_filter_proxy import sanitize_gemini_contents
        data = {
            "contents": [
                {"role": "model", "parts": [
                    {"functionCall": {"id": "call_1", "name": "bash", "args": {"cmd": "ls"}}},
                ]},
            ],
        }
        result = sanitize_gemini_contents(data)
        fc = result["contents"][0]["parts"][0]["functionCall"]
        assert "id" not in fc
        assert fc["name"] == "bash"
        assert fc["args"] == {"cmd": "ls"}

    def test_strips_id_from_function_response_camel(self):
        from content_filter_proxy import sanitize_gemini_contents
        data = {
            "contents": [
                {"role": "user", "parts": [
                    {"functionResponse": {"id": "call_1", "name": "bash", "response": {"output": "ok"}}},
                ]},
            ],
        }
        result = sanitize_gemini_contents(data)
        fr = result["contents"][0]["parts"][0]["functionResponse"]
        assert "id" not in fr
        assert fr["response"] == {"output": "ok"}

    def test_strips_id_from_snake_case_keys(self):
        from content_filter_proxy import sanitize_gemini_contents
        data = {
            "contents": [
                {"role": "model", "parts": [{"function_call": {"id": "c1", "name": "f"}}]},
                {"role": "user", "parts": [{"function_response": {"id": "c1", "name": "f", "response": {}}}]},
            ],
        }
        result = sanitize_gemini_contents(data)
        assert "id" not in result["contents"][0]["parts"][0]["function_call"]
        assert "id" not in result["contents"][1]["parts"][0]["function_response"]

    def test_tolerates_non_dict_contents_and_parts(self):
        from content_filter_proxy import sanitize_gemini_contents
        data = {"contents": ["plain", {"role": "user", "parts": ["text", {"text": "hi"}]}, {"role": "x"}]}
        result = sanitize_gemini_contents(data)
        assert result == data

    def test_noop_without_contents(self):
        from content_filter_proxy import sanitize_gemini_contents
        data = {"messages": [{"role": "user", "content": "hi"}]}
        assert sanitize_gemini_contents(data) == data


# ---------------------------------------------------------------------------
# resolve_upstream_url — /gemini/* requests forward to the gateway's Gemini
# route; everything else keeps the original OpenAI-compatible upstream.
# ---------------------------------------------------------------------------

class TestResolveUpstreamUrl:
    def test_gemini_path_routes_to_gemini_upstream(self):
        import content_filter_proxy as cfp
        with mock.patch.object(cfp, "UPSTREAM_BASE", "https://gw/mlflow/v1"), \
             mock.patch.object(cfp, "GEMINI_UPSTREAM_BASE", "https://gw/gemini"):
            url = cfp.resolve_upstream_url(
                "/gemini/v1beta/models/databricks-gemini-2-5-pro:streamGenerateContent?alt=sse"
            )
        assert url == "https://gw/gemini/v1beta/models/databricks-gemini-2-5-pro:streamGenerateContent?alt=sse"

    def test_non_gemini_path_routes_to_default_upstream(self):
        import content_filter_proxy as cfp
        with mock.patch.object(cfp, "UPSTREAM_BASE", "https://gw/mlflow/v1"), \
             mock.patch.object(cfp, "GEMINI_UPSTREAM_BASE", "https://gw/gemini"):
            url = cfp.resolve_upstream_url("/chat/completions")
        assert url == "https://gw/mlflow/v1/chat/completions"

    def test_gemini_path_without_gemini_base_falls_back(self):
        import content_filter_proxy as cfp
        with mock.patch.object(cfp, "UPSTREAM_BASE", "https://gw/mlflow/v1"), \
             mock.patch.object(cfp, "GEMINI_UPSTREAM_BASE", ""):
            url = cfp.resolve_upstream_url("/gemini/v1beta/models/m:generateContent")
        assert url == "https://gw/mlflow/v1/gemini/v1beta/models/m:generateContent"


# ---------------------------------------------------------------------------
# _is_streaming_request — Gemini signals streaming in the URL
# (:streamGenerateContent?alt=sse), not via a "stream" body field.
# ---------------------------------------------------------------------------

class TestIsStreamingRequest:
    def test_openai_stream_flag(self):
        from content_filter_proxy import _is_streaming_request
        assert _is_streaming_request("/chat/completions", {"stream": True}) is True

    def test_openai_non_streaming(self):
        from content_filter_proxy import _is_streaming_request
        assert _is_streaming_request("/chat/completions", {"stream": False}) is False
        assert _is_streaming_request("/chat/completions", {}) is False

    def test_gemini_stream_generate_content_path(self):
        from content_filter_proxy import _is_streaming_request
        assert _is_streaming_request(
            "/gemini/v1beta/models/m:streamGenerateContent?alt=sse", {}
        ) is True

    def test_gemini_unary_generate_content_path(self):
        from content_filter_proxy import _is_streaming_request
        assert _is_streaming_request("/gemini/v1beta/models/m:generateContent", {}) is False

    def test_tolerates_unparsed_body(self):
        from content_filter_proxy import _is_streaming_request
        assert _is_streaming_request("/chat/completions", None) is False
