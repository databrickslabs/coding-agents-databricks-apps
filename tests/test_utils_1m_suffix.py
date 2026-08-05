"""Tests for utils.add_1m_context_suffix — the [1m] gateway 1M-context suffix.

The suffix is how Claude Code requests the 1M context window through the
Databricks AI Gateway (parsed server-side off the model-id string). Only
opus/sonnet >= 4.6 get it; Haiku 4.5 is 200K-native. Mirrors Databricks'
own `ucode` wrapper (`_maybe_add_1m_suffix`).
"""

import pytest

from utils import add_1m_context_suffix


@pytest.mark.parametrize(
    "model, expected",
    [
        # opus/sonnet >= 4.6 → suffixed
        ("databricks-claude-opus-4-8", "databricks-claude-opus-4-8[1m]"),
        ("databricks-claude-opus-4-7", "databricks-claude-opus-4-7[1m]"),
        ("databricks-claude-opus-4-6", "databricks-claude-opus-4-6[1m]"),
        ("databricks-claude-sonnet-4-6", "databricks-claude-sonnet-4-6[1m]"),
        # UC model-services (system.ai.) form also suffixed
        ("system.ai.claude-opus-4-8", "system.ai.claude-opus-4-8[1m]"),
        ("system.ai.claude-sonnet-4-6", "system.ai.claude-sonnet-4-6[1m]"),
        # haiku is never suffixed (200K-native)
        ("databricks-claude-haiku-4-5", "databricks-claude-haiku-4-5"),
        ("system.ai.claude-haiku-4-5", "system.ai.claude-haiku-4-5"),
        # opus/sonnet < 4.6 do not get 1M
        ("databricks-claude-sonnet-4-5", "databricks-claude-sonnet-4-5"),
        ("databricks-claude-opus-4-5", "databricks-claude-opus-4-5"),
        # idempotent — already suffixed
        ("databricks-claude-opus-4-8[1m]", "databricks-claude-opus-4-8[1m]"),
        # non-Claude ids pass through untouched
        ("databricks-gpt-5-3-codex", "databricks-gpt-5-3-codex"),
        ("databricks-gemini-2-5-pro", "databricks-gemini-2-5-pro"),
    ],
)
def test_add_1m_context_suffix(model, expected):
    assert add_1m_context_suffix(model) == expected
