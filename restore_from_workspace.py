#!/usr/bin/env python
"""Restore a project directory FROM Databricks Workspace back to local disk.

Inverse of ``sync_to_workspace.py``. This environment is ephemeral — the
container's disk can be recycled at any time, and the only durable copy of a
project is the one the post-commit hook synced to
``/Workspace/Shared/coda/{app-name}/{name}``. Use this to rehydrate a project
after a recycle, before starting new work.

Usage:
    restore_from_workspace.py                # restore the cwd's project
    restore_from_workspace.py <name>         # restore ~/projects/<name>
    restore_from_workspace.py <name> --force # overwrite existing local files

Safety:
- Only ever writes under ``~/projects/`` (mirrors the sync's allowlist).
- Refuses to overwrite an existing non-empty target unless ``--force`` is given,
  so it can't silently clobber uncommitted local work.
- NEVER touches ``.git`` on the workspace side (the workspace copy is a plain
  file mirror, not a repo) — after restoring, re-run ``git init`` / re-clone if
  you need history. Do not import ``.git`` into the workspace.
"""
import subprocess
import sys
from pathlib import Path

try:
    from databricks.sdk import WorkspaceClient
except ImportError:
    error_log = Path.home() / ".sync-errors.log"
    with open(error_log, "a") as f:
        f.write(f"databricks-sdk not installed for {sys.executable}\n")
    print("⚠ databricks-sdk not available", file=sys.stderr)
    sys.exit(1)


def _target_is_nonempty(path: Path) -> bool:
    return path.exists() and any(path.iterdir())


def restore_project(project_name: str, force: bool = False):
    """Export a project from the user's Workspace to ~/projects/<name>."""
    projects_dir = Path.home() / "projects"
    local_dest = (projects_dir / project_name).resolve()

    # Guard: only ever write inside ~/projects/ (mirrors sync's allowlist).
    try:
        local_dest.relative_to(projects_dir)
    except ValueError:
        print(f"⚠ REFUSE: {local_dest} is outside {projects_dir}", file=sys.stderr)
        return 2

    if _target_is_nonempty(local_dest) and not force:
        print(
            f"⚠ REFUSE: {local_dest} already exists and is non-empty.\n"
            f"  Pass --force to overwrite (this can clobber uncommitted local work).",
            file=sys.stderr,
        )
        return 3

    try:
        from utils import workspace_sync_auth, workspace_sync_dest

        workspace_src = workspace_sync_dest(project_name)

        # Validates auth (PAT profile, else the SP-broker profile) + inits
        # telemetry, and returns the env the CLI must run with.
        restore_env, _ = workspace_sync_auth()

        local_dest.mkdir(parents=True, exist_ok=True)

        cmd = ["databricks", "workspace", "export-dir", workspace_src, str(local_dest)]
        if force:
            cmd.append("--overwrite")

        result = subprocess.run(cmd, capture_output=True, text=True, env=restore_env)

        if result.returncode == 0:
            print(f"✓ Restored {workspace_src} -> {local_dest}")
            print(
                "  Note: this is a file mirror, not a git repo. If you need history,\n"
                "  run 'git init' here or re-clone the remote. Never import .git into\n"
                "  the Workspace."
            )
            try:
                from telemetry import log_telemetry
                log_telemetry("event", "workspace_restore")
            except Exception:
                pass  # Telemetry must never break restore
            return 0
        else:
            print(f"⚠ Restore failed: {result.stderr}", file=sys.stderr)
            return 1

    except Exception as e:
        error_log = Path.home() / ".sync-errors.log"
        with open(error_log, "a") as f:
            f.write(f"restore {project_name}: {e}\n")
        print(f"⚠ Restore failed (logged to ~/.sync-errors.log): {e}", file=sys.stderr)
        return 1


def main(argv):
    args = [a for a in argv if a != "--force"]
    force = "--force" in argv

    if args:
        project_name = args[0]
    else:
        # No name given: infer from the current working directory.
        cwd = Path.cwd().resolve()
        projects_dir = (Path.home() / "projects").resolve()
        try:
            rel = cwd.relative_to(projects_dir)
            project_name = rel.parts[0]
        except (ValueError, IndexError):
            print(
                "Usage: restore_from_workspace.py <project-name> [--force]\n"
                "  (or run from inside a ~/projects/<name> directory)",
                file=sys.stderr,
            )
            return 2

    return restore_project(project_name, force=force)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
