#!/usr/bin/env python
"""Sync a project directory to Databricks Workspace."""
import os
import sys
import subprocess
from pathlib import Path

try:
    from databricks.sdk import WorkspaceClient
except ImportError:
    # Log and exit gracefully - databricks-sdk should be pre-installed
    error_log = Path.home() / ".sync-errors.log"
    with open(error_log, "a") as f:
        f.write(f"databricks-sdk not installed for {sys.executable}\n")
    print(f"⚠ databricks-sdk not available", file=sys.stderr)
    sys.exit(0)


def get_user_email():
    """Get current user's email from Databricks token."""
    # Force PAT auth, ignore OAuth credentials
    w = WorkspaceClient(
        host=os.environ.get("DATABRICKS_HOST"),
        token=os.environ.get("DATABRICKS_TOKEN"),
        auth_type="pat"
    )
    return w.current_user.me().user_name


def sync_project(project_path: Path):
    """Sync project to user's Workspace."""
    # Only sync projects inside ~/projects/
    project_path = project_path.resolve()
    projects_dir = Path.home() / "projects"
    try:
        project_path.relative_to(projects_dir)
    except ValueError:
        print(f"⚠ SKIP: {project_path} is outside {projects_dir}", file=sys.stderr)
        return

    try:
        user_email = get_user_email()
        workspace_dest = f"/Workspace/Users/{user_email}/projects/{project_path.name}"

        # Create env with only PAT auth (remove OAuth vars)
        sync_env = os.environ.copy()
        sync_env.pop("DATABRICKS_CLIENT_ID", None)
        sync_env.pop("DATABRICKS_CLIENT_SECRET", None)

        result = subprocess.run(
            ["databricks", "sync", str(project_path), workspace_dest, "--watch=false"],
            capture_output=True,
            text=True,
            env=sync_env
        )

        if result.returncode == 0:
            print(f"✓ Synced to {workspace_dest}")
        else:
            print(f"⚠ Sync warning: {result.stderr}", file=sys.stderr)

    except Exception as e:
        # Log error but don't block the commit
        error_log = Path.home() / ".sync-errors.log"
        with open(error_log, "a") as f:
            f.write(f"{project_path}: {e}\n")
        print(f"⚠ Sync failed (logged to ~/.sync-errors.log)", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sync_project(Path(sys.argv[1]))
    else:
        sync_project(Path.cwd())
