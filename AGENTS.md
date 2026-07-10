# AGENTS.md

Agent guidance for this repo lives in one canonical file:

**→ [`docs/agent-instructions.md`](docs/agent-instructions.md)**

Read it before doing anything. It covers the **ephemeral-environment rules
(commit small + often, verify sync, restore from Workspace, never move `.git`)**,
what this repo is, working conventions, Databricks auth gotchas, and a recovery
cheat-sheet.

Human/onboarding detail (feature catalog, skills, endpoints, env vars) is in
[`README.md`](README.md).

Edit guidance in `.agents/instructions.md`, not here — this stub only points to
it so `CLAUDE.md`, `GEMINI.md`, and `AGENTS.md` never drift.
