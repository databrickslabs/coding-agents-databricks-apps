#!/usr/bin/env python
"""Sync a project directory to Databricks Workspace."""
import configparser
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


def _read_databrickscfg():
    """Read host and token from ~/.databrickscfg [DEFAULT] profile."""
    cfg_path = Path.home() / ".databrickscfg"
    if not cfg_path.exists():
        return None, None
    parser = configparser.ConfigParser()
    parser.read(cfg_path)
    return (
        parser.get("DEFAULT", "host", fallback=None),
        parser.get("DEFAULT", "token", fallback=None),
    )


def get_user_email():
    """Get current user's email from Databricks token."""
    host, token = _read_databrickscfg()
    if not host or not token:
        raise RuntimeError("~/.databrickscfg missing host or token")
    w = WorkspaceClient(host=host, token=token, auth_type="pat")
    try:
        from telemetry import set_product_info
        set_product_info(w)
    except Exception:
        pass
    return w.current_user.me().user_name


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
        get_user_email()  # validates ~/.databrickscfg auth + inits telemetry
        from utils import databrickscfg_only_env, workspace_sync_dest

        workspace_dest = workspace_sync_dest(project_path.name)

        # Strip ambient creds so the CLI falls through to ~/.databrickscfg
        sync_env = databrickscfg_only_env()

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
