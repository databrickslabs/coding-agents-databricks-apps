"""Tests for sync_to_workspace — path-escape guard and workspace sync."""

import subprocess
from pathlib import Path
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# _read_databrickscfg
# ---------------------------------------------------------------------------

class TestReadDatabrickscfg:
    def test_reads_host_and_token(self, tmp_path):
        cfg = tmp_path / ".databrickscfg"
        cfg.write_text("[DEFAULT]\nhost = https://test.cloud.databricks.com\ntoken = dapi_abc123\n")
        with mock.patch("sync_to_workspace.Path.home", return_value=tmp_path):
            from sync_to_workspace import _read_databrickscfg
            host, token = _read_databrickscfg()
        assert host == "https://test.cloud.databricks.com"
        assert token == "dapi_abc123"

    def test_returns_none_when_missing(self, tmp_path):
        with mock.patch("sync_to_workspace.Path.home", return_value=tmp_path):
            from sync_to_workspace import _read_databrickscfg
            host, token = _read_databrickscfg()
        assert host is None
        assert token is None

    def test_returns_none_for_missing_keys(self, tmp_path):
        cfg = tmp_path / ".databrickscfg"
        cfg.write_text("[DEFAULT]\n# empty section\n")
        with mock.patch("sync_to_workspace.Path.home", return_value=tmp_path):
            from sync_to_workspace import _read_databrickscfg
            host, token = _read_databrickscfg()
        assert host is None
        assert token is None


# ---------------------------------------------------------------------------
# get_user_email
# ---------------------------------------------------------------------------

class TestGetUserEmail:
    def test_raises_when_no_config(self, tmp_path):
        from sync_to_workspace import get_user_email
        with mock.patch("sync_to_workspace._read_databrickscfg", return_value=(None, None)):
            with pytest.raises(RuntimeError, match="missing host or token"):
                get_user_email()

    def test_raises_when_no_token(self):
        from sync_to_workspace import get_user_email
        with mock.patch("sync_to_workspace._read_databrickscfg", return_value=("https://host", None)):
            with pytest.raises(RuntimeError, match="missing host or token"):
                get_user_email()

    def test_returns_email(self):
        from sync_to_workspace import get_user_email
        mock_user = mock.MagicMock()
        mock_user.user_name = "test@example.com"
        mock_client = mock.MagicMock()
        mock_client.current_user.me.return_value = mock_user
        with mock.patch("sync_to_workspace._read_databrickscfg", return_value=("https://host", "tok")):
            with mock.patch("sync_to_workspace.WorkspaceClient", return_value=mock_client):
                email = get_user_email()
        assert email == "test@example.com"


# ---------------------------------------------------------------------------
# sync_project — path-escape guard
# ---------------------------------------------------------------------------

class TestSyncProject:
    def test_rejects_path_outside_projects_dir(self, tmp_path, capsys):
        from sync_to_workspace import sync_project
        # Create a path outside ~/projects/
        outside = tmp_path / "evil-repo"
        outside.mkdir()
        with mock.patch("sync_to_workspace.Path.home", return_value=tmp_path):
            sync_project(outside)
        captured = capsys.readouterr()
        assert "SKIP" in captured.err
        assert "outside" in captured.err

    def test_accepts_path_inside_projects_dir(self, tmp_path):
        from sync_to_workspace import sync_project
        projects = tmp_path / "projects"
        projects.mkdir()
        repo = projects / "my-repo"
        repo.mkdir()

        mock_user = mock.MagicMock()
        mock_user.user_name = "test@example.com"
        mock_client = mock.MagicMock()
        mock_client.current_user.me.return_value = mock_user

        with mock.patch("sync_to_workspace.Path.home", return_value=tmp_path), \
             mock.patch("sync_to_workspace._read_databrickscfg", return_value=("https://host", "tok")), \
             mock.patch("sync_to_workspace.WorkspaceClient", return_value=mock_client), \
             mock.patch("sync_to_workspace.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            sync_project(repo)

        mock_run.assert_called_once()
        args = mock_run.call_args
        assert "databricks" in args[0][0][0]
        assert "sync" in args[0][0][1]

    def test_strips_oauth_env_from_subprocess(self, tmp_path):
        """Verify OAuth credentials are stripped so CLI falls through to ~/.databrickscfg."""
        from sync_to_workspace import sync_project
        projects = tmp_path / "projects"
        projects.mkdir()
        repo = projects / "my-repo"
        repo.mkdir()

        mock_user = mock.MagicMock()
        mock_user.user_name = "test@example.com"
        mock_client = mock.MagicMock()
        mock_client.current_user.me.return_value = mock_user

        with mock.patch("sync_to_workspace.Path.home", return_value=tmp_path), \
             mock.patch("sync_to_workspace._read_databrickscfg", return_value=("https://host", "tok")), \
             mock.patch("sync_to_workspace.WorkspaceClient", return_value=mock_client), \
             mock.patch("sync_to_workspace.subprocess.run") as mock_run, \
             mock.patch.dict("os.environ", {
                 "DATABRICKS_CLIENT_ID": "sp-id",
                 "DATABRICKS_CLIENT_SECRET": "sp-secret",
                 "DATABRICKS_HOST": "https://host",
                 "DATABRICKS_TOKEN": "dapi_tok",
             }):
            mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            sync_project(repo)

        call_env = mock_run.call_args[1].get("env") or mock_run.call_args.kwargs.get("env", {})
        assert "DATABRICKS_CLIENT_ID" not in call_env
        assert "DATABRICKS_CLIENT_SECRET" not in call_env
        assert "DATABRICKS_HOST" not in call_env
        assert "DATABRICKS_TOKEN" not in call_env

    def test_logs_error_on_failure(self, tmp_path, capsys):
        from sync_to_workspace import sync_project
        projects = tmp_path / "projects"
        projects.mkdir()
        repo = projects / "my-repo"
        repo.mkdir()

        with mock.patch("sync_to_workspace.Path.home", return_value=tmp_path), \
             mock.patch("sync_to_workspace.get_user_email", side_effect=Exception("auth failed")):
            sync_project(repo)

        captured = capsys.readouterr()
        assert "Sync failed" in captured.err
        # Error should be logged to file
        error_log = tmp_path / ".sync-errors.log"
        assert error_log.exists()
        assert "auth failed" in error_log.read_text()

    def test_sync_failure_warns(self, tmp_path, capsys):
        """Non-zero return code from databricks sync should print warning."""
        from sync_to_workspace import sync_project
        projects = tmp_path / "projects"
        projects.mkdir()
        repo = projects / "my-repo"
        repo.mkdir()

        mock_user = mock.MagicMock()
        mock_user.user_name = "test@example.com"
        mock_client = mock.MagicMock()
        mock_client.current_user.me.return_value = mock_user

        with mock.patch("sync_to_workspace.Path.home", return_value=tmp_path), \
             mock.patch("sync_to_workspace._read_databrickscfg", return_value=("https://host", "tok")), \
             mock.patch("sync_to_workspace.WorkspaceClient", return_value=mock_client), \
             mock.patch("sync_to_workspace.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr="permission denied")
            sync_project(repo)

        captured = capsys.readouterr()
        assert "Sync warning" in captured.err
