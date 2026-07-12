"""Tests for utils.workspace_sync_dest — the Workspace sync-back path.

The path is keyed on the CoDA instance name so that same-identity instances
(a shared-app fleet all resolving one PAT identity) don't overwrite each other's
sync-back. Both sync_to_workspace.py (write) and restore_from_workspace.py
(read) call this, so it is the single source of truth for the path.
See specs/workspace-sync-collision/GOAL.md.
"""

import pytest

from utils import workspace_sync_dest

_ENV_VARS = ("CODA_INSTANCE_NAME", "DATABRICKS_APP_NAME")


@pytest.fixture(autouse=True)
def _clear_instance_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_uses_coda_instance_name_first(monkeypatch):
    monkeypatch.setenv("CODA_INSTANCE_NAME", "coda-01")
    monkeypatch.setenv("DATABRICKS_APP_NAME", "ignored")
    assert workspace_sync_dest("myrepo") == "/Workspace/Shared/coda/coda-01/myrepo"


def test_falls_back_to_app_name(monkeypatch):
    monkeypatch.setenv("DATABRICKS_APP_NAME", "coda-02")
    assert workspace_sync_dest("myrepo") == "/Workspace/Shared/coda/coda-02/myrepo"


def test_local_fallback_when_unset():
    # Solo/local CoDA with no instance name → stable literal, path stays total.
    assert workspace_sync_dest("myrepo") == "/Workspace/Shared/coda/_local/myrepo"


def test_distinct_instances_never_collide_on_same_repo(monkeypatch):
    """The collision-prevention property: same repo name, different instance
    name → different path. This is the whole point of the fix."""
    monkeypatch.setenv("DATABRICKS_APP_NAME", "coda-01")
    a = workspace_sync_dest("challenge")
    monkeypatch.setenv("DATABRICKS_APP_NAME", "coda-02")
    b = workspace_sync_dest("challenge")
    assert a != b
