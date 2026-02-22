---
name: refresh-databricks-skills
description: Use when Databricks skills need updating, user asks to refresh or sync skills from upstream, or skills seem outdated compared to the ai-dev-kit repo
---

# Refresh Databricks Skills

## Overview

Pulls the latest Databricks skills from the upstream source repo and replaces all existing Databricks skills in the project while preserving non-Databricks skills (e.g., superpowers workflow skills).

**Source repo:** `https://github.com/databricks-solutions/ai-dev-kit` (path: `databricks-skills/`)

## When to Use

- User asks to update, refresh, or sync Databricks skills
- Skills seem outdated or missing newer Databricks features
- A new Databricks skill was added upstream that the project needs

## Process

1. **Clone the upstream repo** (shallow clone for speed):
   ```bash
   git clone --depth 1 https://github.com/databricks-solutions/ai-dev-kit.git $TMPDIR/ai-dev-kit
   ```

2. **Identify non-Databricks skills to preserve.** These are the superpowers workflow skills that live alongside Databricks skills. List them by checking which directories in `.claude/skills/` do NOT have a matching folder in the upstream `databricks-skills/` directory. Common superpowers skills include: `brainstorming`, `dispatching-parallel-agents`, `executing-plans`, `finishing-a-development-branch`, `receiving-code-review`, `requesting-code-review`, `subagent-driven-development`, `systematic-debugging`, `test-driven-development`, `using-git-worktrees`, `using-superpowers`, `verification-before-completion`, `writing-plans`, `writing-skills`. Also preserve any other project-specific skills (like this one: `refresh-databricks-skills`).

3. **Remove old Databricks skills** from `.claude/skills/`, keeping all non-Databricks skills identified above.

4. **Copy new Databricks skills** from the cloned repo. Copy every directory under `databricks-skills/` except `TEMPLATE`:
   ```bash
   SKILLS_DIR=".claude/skills"
   UPSTREAM="$TMPDIR/ai-dev-kit/databricks-skills"
   for dir in "$UPSTREAM"/databricks-* "$UPSTREAM"/spark-*; do
     [ -d "$dir" ] && cp -r "$dir" "$SKILLS_DIR/$(basename "$dir")"
   done
   ```

5. **Clean up** the cloned repo:
   ```bash
   rm -rf $TMPDIR/ai-dev-kit
   ```

6. **Report** the count of skills added, removed, and updated.

## After Refreshing

If the project is deployed as a Databricks App, remind the user to sync the updated skills to the workspace and redeploy:
```bash
databricks workspace import-dir <local-path> <workspace-path> --overwrite --profile <profile>
databricks apps deploy <app-name> --source-code-path <workspace-path> --profile <profile>
```

## Common Mistakes

- **Deleting non-Databricks skills:** Always identify and preserve superpowers and project-specific skills before removing anything.
- **Forgetting this skill itself:** `refresh-databricks-skills` must be preserved during the refresh.
- **Not using `--depth 1`:** Full clone is slow and unnecessary. Always shallow clone.
