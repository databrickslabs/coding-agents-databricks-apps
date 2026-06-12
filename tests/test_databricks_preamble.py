"""Unit tests for coda_mcp.databricks_preamble."""
import re

from coda_mcp.databricks_preamble import (
    build_capabilities,
    build_workflow_protocol,
    get_databricks_skills,
)


def test_get_databricks_skills_returns_exactly_sixteen():
    skills = get_databricks_skills()
    assert isinstance(skills, tuple)
    assert len(skills) == 16, f"Expected 16 skills, got {len(skills)}: {skills}"


def test_skills_list_matches_claude_md():
    """The hardcoded skill tuple must match the Databricks Skills table in CLAUDE.md.

    Drift in either direction (added to tuple but not docs, or vice versa) fails
    this test. The test is the canary that forces both sources to stay in sync.
    """
    import os
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    claude_md = os.path.join(repo_root, "CLAUDE.md")
    with open(claude_md, "r") as f:
        text = f.read()
    # Find the Databricks Skills section. Names are comma-separated within table cells.
    section_match = re.search(
        r"###\s+Databricks Skills.*?(?=\n###|\n##|\Z)",
        text, re.DOTALL,
    )
    assert section_match, "Could not find 'Databricks Skills' section in CLAUDE.md"
    section = section_match.group(0)
    # Extract skill names: kebab-case tokens that follow a list pattern. Be loose —
    # accept anything that looks like a skill identifier inside table cells.
    skill_names_in_md = set(re.findall(r"\b([a-z][a-z0-9-]{2,}(?:-[a-z0-9]+)+)\b", section))
    skills_in_code = set(get_databricks_skills())
    # Every skill in code must appear in CLAUDE.md.
    missing_from_md = skills_in_code - skill_names_in_md
    assert not missing_from_md, (
        f"Skills in code but NOT in CLAUDE.md (update CLAUDE.md): {missing_from_md}"
    )
    # Every kebab-case identifier in CLAUDE.md's Databricks section must appear in code.
    # The regex deliberately matches lowercase-only, so category labels like
    # "AI & Agents" / "Data Engineering" cannot create false positives.
    missing_from_code = skill_names_in_md - skills_in_code
    assert not missing_from_code, (
        f"Skills in CLAUDE.md but NOT in code (update databricks_preamble.py): "
        f"{missing_from_code}"
    )


def test_capabilities_mentions_cli():
    text = build_capabilities()
    assert "Databricks CLI" in text
    assert "databricks current-user me" in text


def test_capabilities_lists_at_least_ten_skills():
    text = build_capabilities()
    skills = get_databricks_skills()
    hits = sum(1 for s in skills if s in text)
    assert hits >= 10, f"Expected at least 10 skills in CAPABILITIES, found {hits}"


def test_capabilities_mentions_all_three_mcp_servers():
    text = build_capabilities()
    assert "DeepWiki" in text
    assert "Exa" in text
    assert "CoDA" in text


def test_capabilities_under_token_budget():
    text = build_capabilities()
    # ~4 chars/token rough lower bound. 1600 chars ≈ 400 tokens budget.
    assert len(text) < 1600, (
        f"CAPABILITIES is {len(text)} chars (~{len(text)//4} tokens); budget is 1600."
    )


def test_workflow_protocol_lists_three_phases():
    text = build_workflow_protocol()
    assert "PHASE 1 — PLAN" in text
    assert "PHASE 2 — EXECUTE" in text
    assert "PHASE 3 — SYNTHESIZE" in text


def test_workflow_protocol_caps_iterations_at_two():
    text = build_workflow_protocol()
    # The string "Maximum 2" should appear once per phase = 3 times.
    count = text.count("Maximum 2")
    assert count == 3, f"Expected 'Maximum 2' to appear 3 times (once per phase); got {count}"


def test_workflow_protocol_describes_info_needed():
    text = build_workflow_protocol()
    assert "info_needed" in text
    assert "feedback" in text


def test_workflow_protocol_disambiguates_needs_approval():
    text = build_workflow_protocol()
    assert "needs_approval" in text
    assert "DISAMBIGUATION" in text


def test_workflow_protocol_under_token_budget():
    text = build_workflow_protocol()
    # ~4 chars/token. 3200 chars ≈ 800 tokens budget.
    assert len(text) < 3200, (
        f"WORKFLOW PROTOCOL is {len(text)} chars (~{len(text)//4} tokens); budget is 3200."
    )
