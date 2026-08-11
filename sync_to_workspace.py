#!/usr/bin/env python
"""Sync a project directory to Databricks Workspace."""
import sys
import subprocess
from pathlib import Path

try:
    from databricks.sdk import WorkspaceClient
except ImportError:
    error_log = Path.home() / ".sync-errors.log"
    with open(error_log, "a") as f:
        f.write(f"databricks-sdk not installed for {sys.executable}\n")
    print("⚠ databricks-sdk not available", file=sys.stderr)
    sys.exit(0)


def sync_project(project_path: Path):
    """Sync project to user's Workspace."""
    project_path = project_path.resolve()
    projects_dir = Path.home() / "projects"
    try:
        project_path.relative_to(projects_dir)
    except ValueError:
        print(f"⚠ SKIP: {project_path} is outside {projects_dir}", file=sys.stderr)
        return

    try:
        from utils import workspace_sync_auth, workspace_sync_dest

        workspace_dest = workspace_sync_dest(project_path.name)

        # Validates auth (PAT profile, else the SP-broker profile) + inits
        # telemetry, and returns the env the CLI must run with.
        sync_env, _ = workspace_sync_auth()

        result = subprocess.run(
            ["databricks", "sync", str(project_path), workspace_dest, "--watch=false"],
            capture_output=True,
            text=True,
            env=sync_env,
        )

        if result.returncode == 0:
            print(f"✓ Synced to {workspace_dest}")
            # Telemetry: track workspace sync events
            try:
                from telemetry import log_telemetry
                log_telemetry("event", "workspace_sync")
            except Exception:
                pass  # Telemetry must never break sync
        else:
            print(f"⚠ Sync warning: {result.stderr}", file=sys.stderr)

    except Exception as e:
        error_log = Path.home() / ".sync-errors.log"
        with open(error_log, "a") as f:
            f.write(f"{project_path}: {e}\n")
        print(f"⚠ Sync failed (logged to ~/.sync-errors.log)", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sync_project(Path(sys.argv[1]))
    else:
        sync_project(Path.cwd())
