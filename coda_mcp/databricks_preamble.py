"""Builders for the CoDA prompt envelope's CAPABILITIES and WORKFLOW PROTOCOL sections.

These are injected into prompt.txt by ``task_manager.wrap_prompt`` when
``workflow_protocol=True``. Pure functions — no side effects, no I/O.
"""
from __future__ import annotations


_DATABRICKS_SKILLS: tuple[str, ...] = (
    "agent-bricks",
    "databricks-genie",
    "databricks-apps-python",
    "databricks-ai-functions",
    "databricks-jobs",
    "databricks-unity-catalog",
    "spark-declarative-pipelines",
    "aibi-dashboards",
    "model-serving",
    "mlflow-evaluation",
    "databricks-bundles",
    "databricks-python-sdk",
    "databricks-config",
    "databricks-docs",
    "synthetic-data-gen",
    "unstructured-pdf-generation",
)


def get_databricks_skills() -> tuple[str, ...]:
    """Return the canonical Databricks skill list. Tests pin this against CLAUDE.md."""
    return _DATABRICKS_SKILLS


def build_capabilities() -> str:
    """Orientation block: CLI, skills, MCP servers, when to prefer Databricks-native paths."""
    skills_lines = []
    # Pack 4 skills per line for readability in prompt.txt.
    for i in range(0, len(_DATABRICKS_SKILLS), 4):
        chunk = _DATABRICKS_SKILLS[i:i + 4]
        skills_lines.append("- " + ", ".join(chunk))
    skills_block = "\n".join(skills_lines)
    return (
        "You are running inside CoDA on a Databricks-authenticated host.\n"
        "\n"
        "Databricks CLI: pre-configured. `databricks current-user me` confirms auth.\n"
        "Use it for jobs, workspace, clusters, warehouses, Unity Catalog operations.\n"
        "\n"
        "Skills available at ~/.claude/skills/ — read each skill's SKILL.md before\n"
        "invoking. Relevant Databricks skills:\n"
        f"{skills_block}\n"
        "\n"
        "MCP servers wired:\n"
        "- DeepWiki — ask_question, read_wiki_contents for any GitHub repo\n"
        "- Exa — web_search_exa, web_fetch_exa for live web context\n"
        "- CoDA — chain follow-up tasks via previous_session_id\n"
        "\n"
        "When the task touches Databricks data, pipelines, jobs, dashboards, agents,\n"
        "or model serving, DEFAULT to the skill / CLI / SDK path above instead of\n"
        "generic Python or web search."
    )


def build_workflow_protocol() -> str:
    """3-phase workflow with critique at each phase + info_needed escape hatch."""
    return (
        "You MUST process this task in three phases. Emit status.jsonl events as\n"
        "you go (one JSON object per line, format below).\n"
        "\n"
        "PHASE 1 — PLAN\n"
        "- Write a step-by-step plan as a status.jsonl line with step=\"plan\" and\n"
        "  message containing the numbered steps.\n"
        "- Then critique your own plan as if you were a separate reviewer.\n"
        "  (Spawn a sub-agent for the critique if your agent supports it; otherwise\n"
        "  write the critique inline as a self-review.) Emit step=\"critique_plan\"\n"
        "  with the verdict (APPROVE / BLOCK / APPROVE-WITH-FIXES) and findings.\n"
        "- If the critique surfaces blockers, revise the plan once and re-emit\n"
        "  step=\"plan\". Maximum 2 plan iterations total.\n"
        "- If after 2 attempts you still cannot produce a viable plan, write\n"
        "  result.json with status=\"info_needed\" (see below) and stop.\n"
        "\n"
        "PHASE 2 — EXECUTE\n"
        "- Work the plan. Emit step=\"execute_<n>\" lines after completing each plan\n"
        "  step (n is 1-indexed, matches the plan's numbering).\n"
        "- After execution, emit step=\"critique_execute\" with a review of what got\n"
        "  built vs what the plan said. APPROVE / BLOCK / APPROVE-WITH-FIXES.\n"
        "- If the critique surfaces correctness or scope gaps, fix them and re-emit\n"
        "  step=\"critique_execute\". Maximum 2 execute iterations total.\n"
        "- If you hit a hard blocker (missing access, missing data, ambiguous\n"
        "  requirements that the plan revealed only mid-execution), write\n"
        "  result.json with status=\"info_needed\" and stop.\n"
        "\n"
        "PHASE 3 — SYNTHESIZE\n"
        "- Write result.json with status=\"completed\".\n"
        "- Emit step=\"critique_synthesize\" with a review of the result against the\n"
        "  original TASK.\n"
        "- If the critique surfaces gaps, revise result.json. Maximum 2 synthesis\n"
        "  iterations total.\n"
        "\n"
        "If at any phase you cannot proceed, use the INFO_NEEDED escape hatch:\n"
        "- Set status=\"info_needed\" in result.json.\n"
        "- Set \"feedback\" to a precise, actionable string naming exactly what is\n"
        "  missing (a table name, a decision, an access grant, a clarification).\n"
        "  The calling client will read this and resubmit with the missing context.\n"
        "- \"info_needed\" is NOT a failure — it is a structured request for\n"
        "  iteration. Use it whenever you would otherwise have to guess.\n"
        "\n"
        "If you encounter a hard, unrecoverable failure (a command crashed, an SDK\n"
        "returned 500, a file is corrupt), use status=\"failed\" with a description\n"
        "in \"errors\".\n"
        "\n"
        "DISAMBIGUATION — two soft statuses already exist and they mean different\n"
        "things; use the right one:\n"
        "- \"info_needed\" — the CALLER must add missing context (table name,\n"
        "  business decision, file contents, access grant) before the task can\n"
        "  proceed. Used when ambiguity or missing input blocks you.\n"
        "- \"needs_approval\" — you have a concrete plan to do something destructive\n"
        "  (drop a table, delete a job, modify permissions). You will execute it\n"
        "  if and only if the caller explicitly approves. Used at the SAFETY\n"
        "  boundary, never for ambiguity. See SAFETY section below.\n"
        "\n"
        "If both apply (e.g. \"I'd drop a table but I'm not sure which one\"), prefer\n"
        "\"info_needed\" — resolving the ambiguity first is cheaper than approving\n"
        "the wrong destructive action."
    )
