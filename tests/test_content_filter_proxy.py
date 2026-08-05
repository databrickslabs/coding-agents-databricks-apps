"""Tests for content_filter_proxy._get_fresh_token cache invalidation.

The proxy reads ~/.databrickscfg on every forwarded request, with a cache to
avoid filesystem hits in tight request bursts. The cache must invalidate the
moment the rotator rewrites the file, otherwise the proxy serves revoked
tokens to upstream for up to TTL seconds after each rotation.
"""

import time
from unittest import mock

import pytest


@pytest.fixture
def tmp_cfg(tmp_path, monkeypatch):
    """Point the proxy at a temp .databrickscfg, with a clean cache."""
    cfg = tmp_path / ".databrickscfg"
    import content_filter_proxy as cfp
    monkeypatch.setattr(cfp, "_DATABRICKSCFG_PATH", str(cfg))
    monkeypatch.setattr(cfp, "_TOKEN_CACHE", {"token": None, "read_at": 0.0, "mtime": 0.0})
    monkeypatch.setattr(cfp, "resolve_sp_oauth_token", lambda: None)
    monkeypatch.setattr(cfp, "resolve_databricks_token", lambda: None)
    return cfg


def _write_cfg(path, token):
    path.write_text(f"[DEFAULT]\nhost = https://example.databricks.com\ntoken = {token}\n")


class TestFreshTokenCacheInvalidation:
    def test_rotated_pat_beats_stale_proxy_env_token(self, tmp_cfg, monkeypatch):
        from content_filter_proxy import _get_fresh_token
        _write_cfg(tmp_cfg, "dapi-rotated")
        monkeypatch.setattr(
            "content_filter_proxy.resolve_databricks_token",
            lambda: "dapi-startup",
        )

        assert _get_fresh_token() == "dapi-rotated"

    def test_cache_invalidates_on_mtime_change(self, tmp_cfg):
        from content_filter_proxy import _get_fresh_token
        _write_cfg(tmp_cfg, "dapi-old")
        assert _get_fresh_token() == "dapi-old"

        # Simulate rotator rewriting the file. utime to a guaranteed-newer mtime
        # so the test isn't sensitive to filesystem mtime granularity.
        _write_cfg(tmp_cfg, "dapi-new")
        import os
        st = os.stat(tmp_cfg)
        os.utime(tmp_cfg, (st.st_atime, st.st_mtime + 10))

        assert _get_fresh_token() == "dapi-new", "must re-read after mtime change"

    def test_cache_hits_when_mtime_unchanged(self, tmp_cfg):
        from content_filter_proxy import _get_fresh_token
        _write_cfg(tmp_cfg, "dapi-stable")
        assert _get_fresh_token() == "dapi-stable"

        # Mutate the file contents WITHOUT advancing mtime (force mtime backwards).
        # If the cache ignored mtime, it'd happily keep serving "dapi-stable";
        # if it consulted mtime, it'd still serve "dapi-stable" because mtime
        # didn't advance. Either way we expect the cached value back, which
        # asserts the cache is doing its de-dup job within the TTL.
        import os
        st = os.stat(tmp_cfg)
        _write_cfg(tmp_cfg, "dapi-tampered")
        os.utime(tmp_cfg, (st.st_atime, st.st_mtime))  # restore old mtime

        assert _get_fresh_token() == "dapi-stable"

    def test_falls_back_to_cache_on_stat_error(self, tmp_cfg, monkeypatch):
        from content_filter_proxy import _get_fresh_token
        _write_cfg(tmp_cfg, "dapi-cached")
        assert _get_fresh_token() == "dapi-cached"

        # Now make os.stat fail. The cache should still return the last known token.
        def boom(_):
            raise OSError("stat broken")
        monkeypatch.setattr("content_filter_proxy.os.stat", boom)
        assert _get_fresh_token() == "dapi-cached"

    def test_returns_none_when_file_missing_and_cache_empty(self, tmp_path, monkeypatch):
        import content_filter_proxy as cfp
        missing = tmp_path / "does-not-exist"
        monkeypatch.setattr(cfp, "_DATABRICKSCFG_PATH", str(missing))
        monkeypatch.setattr(cfp, "_TOKEN_CACHE", {"token": None, "read_at": 0.0, "mtime": 0.0})
        monkeypatch.setattr(cfp, "resolve_databricks_token", lambda: None)
        assert cfp._get_fresh_token() is None

    def test_uses_sp_oauth_when_default_pat_is_missing(self, tmp_path, monkeypatch):
        import content_filter_proxy as cfp
        missing = tmp_path / "does-not-exist"
        monkeypatch.setattr(cfp, "_DATABRICKSCFG_PATH", str(missing))
        monkeypatch.setattr(cfp, "_TOKEN_CACHE", {"token": None, "read_at": 0.0, "mtime": 0.0})
        monkeypatch.setattr(cfp, "resolve_databricks_token", lambda: "sp-oauth-token")

        assert cfp._get_fresh_token() == "sp-oauth-token"


def test_sse_line_decodes_literal_utf8_without_latin1_mojibake():
    from content_filter_proxy import _decode_sse_line

    raw = 'data: {"text":"✓ café → │ ─ 😀"}'.encode("utf-8")
    assert _decode_sse_line(raw) == 'data: {"text":"✓ café → │ ─ 😀"}'


# ---------------------------------------------------------------------------
# Request sanitisation for providers that reject parts of the OpenAI shape
# ---------------------------------------------------------------------------

class TestGeminiSchemaStripping:
    """Gemini's function-declaration schema rejects several JSON Schema
    keywords. It 400s the whole request rather than ignoring them, so an
    unstripped key makes the tool unusable, not merely unvalidated."""

    def test_strips_numeric_and_array_validation_keywords(self):
        import content_filter_proxy as cfp

        schema = {
            "type": "object",
            "properties": {
                "n": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "exclusiveMaximum": 10,
                    "multipleOf": 2,
                },
                "items": {"type": "array", "uniqueItems": True},
            },
        }

        cleaned = cfp.strip_unsupported_schema_keys(schema)

        props = cleaned["properties"]
        for key in ("exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
            assert key not in props["n"], f"{key} should have been stripped"
        assert "uniqueItems" not in props["items"]
        # Non-offending keys survive.
        assert props["n"]["type"] == "number"

    def test_drops_range_bounds_on_integer_properties(self):
        """Gemini rejects minimum/maximum specifically on integer types."""
        import content_filter_proxy as cfp

        cleaned = cfp.strip_unsupported_schema_keys(
            {"type": "integer", "minimum": 1, "maximum": 5}
        )

        assert cleaned == {"type": "integer"}

    def test_keeps_range_bounds_on_number_properties(self):
        """...but not on numbers — over-stripping loses real constraints."""
        import content_filter_proxy as cfp

        cleaned = cfp.strip_unsupported_schema_keys(
            {"type": "number", "minimum": 1, "maximum": 5}
        )

        assert cleaned["minimum"] == 1
        assert cleaned["maximum"] == 5

    def test_strips_recursively_through_nested_schemas(self):
        import content_filter_proxy as cfp

        cleaned = cfp.strip_unsupported_schema_keys(
            {"properties": {"a": {"items": [{"type": "integer", "minimum": 0}]}}}
        )

        assert cleaned["properties"]["a"]["items"][0] == {"type": "integer"}


class TestGptReasoningKeyStripping:
    """reasoning_effort / reasoningSummary are GPT-family fields. Stripping is
    scoped by model id: removing them from a model that supports reasoning
    silently downgrades output quality."""

    def test_strips_for_gpt_models(self):
        import content_filter_proxy as cfp

        data = cfp.sanitize_tool_schemas({
            "model": "databricks-gpt-5-5",
            "reasoning_effort": "high",
            "reasoningSummary": "auto",
        })

        assert "reasoning_effort" not in data
        assert "reasoningSummary" not in data

    def test_preserves_for_non_gpt_models(self):
        import content_filter_proxy as cfp

        data = cfp.sanitize_tool_schemas({
            "model": "databricks-claude-opus-4-8",
            "reasoning_effort": "high",
        })

        assert data["reasoning_effort"] == "high"

    def test_model_match_is_case_insensitive(self):
        import content_filter_proxy as cfp

        data = cfp.sanitize_tool_schemas({
            "model": "Databricks-GPT-5-5", "reasoning_effort": "low",
        })

        assert "reasoning_effort" not in data

    def test_missing_model_field_does_not_raise(self):
        import content_filter_proxy as cfp

        data = cfp.sanitize_tool_schemas({"reasoning_effort": "high"})

        assert data["reasoning_effort"] == "high"


class TestContentBlockFlattening:
    """Some served models answer chat-completions with `content` as an array of
    typed blocks. OpenAI-shaped clients like OpenCode expect a string and render
    the raw array (or drop the message) otherwise."""

    def test_flattens_text_blocks_to_a_string(self):
        import content_filter_proxy as cfp

        out = cfp._flatten_content_blocks(
            [{"type": "text", "text": "Hello "}, {"type": "text", "text": "world"}]
        )

        assert out == "Hello world"

    def test_ignores_non_text_blocks(self):
        import content_filter_proxy as cfp

        out = cfp._flatten_content_blocks(
            [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "answer"}]
        )

        assert out == "answer"

    def test_passes_through_plain_strings(self):
        import content_filter_proxy as cfp

        assert cfp._flatten_content_blocks("already a string") == "already a string"

    def test_passes_through_none(self):
        import content_filter_proxy as cfp

        assert cfp._flatten_content_blocks(None) is None

    def test_applied_to_non_streaming_message(self):
        import content_filter_proxy as cfp

        fixed = cfp.fix_response_data({
            "choices": [{"message": {"content": [{"type": "text", "text": "hi"}]}}]
        })

        assert fixed["choices"][0]["message"]["content"] == "hi"

    def test_applied_to_streaming_delta(self):
        import content_filter_proxy as cfp

        fixed = cfp.fix_response_data({
            "choices": [{"delta": {"content": [{"type": "text", "text": "chunk"}]}}]
        })

        assert fixed["choices"][0]["delta"]["content"] == "chunk"

    def test_does_not_invent_a_content_field(self):
        """A tool-call-only message has no `content`; adding an empty string
        could be read as an empty assistant turn."""
        import content_filter_proxy as cfp

        fixed = cfp.fix_response_data({"choices": [{"message": {"tool_calls": []}}]})

        assert "content" not in fixed["choices"][0]["message"]


class TestToollessRequestsAreStillSanitised:
    """There used to be an early return for requests without `tools`, so the
    top-level cleanup never ran on plain chat turns."""

    def test_strips_stream_options_without_tools(self):
        import content_filter_proxy as cfp

        data = cfp.sanitize_tool_schemas({"model": "m", "stream_options": {"x": 1}})

        assert "stream_options" not in data

    def test_strips_top_level_schema_without_tools(self):
        import content_filter_proxy as cfp

        data = cfp.sanitize_tool_schemas({"model": "m", "$schema": "http://x"})

        assert "$schema" not in data

    def test_tool_schemas_still_sanitised_when_present(self):
        import content_filter_proxy as cfp

        data = cfp.sanitize_tool_schemas({
            "model": "m",
            "tools": [{"function": {"parameters": {"type": "object",
                                                   "additionalProperties": False}}}],
        })

        assert "additionalProperties" not in data["tools"][0]["function"]["parameters"]
