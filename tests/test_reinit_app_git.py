"""Tests for _reinit_app_git() — git reinit on Databricks Apps startup."""

import os
import subprocess
import textwrap
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_app():
    """Import app module (deferred so monkeypatching can happen first)."""
    import app as app_module
    return app_module


# ---------------------------------------------------------------------------
# 1. Environment detection — only runs on Databricks Apps
# ---------------------------------------------------------------------------

class TestEnvironmentDetection:
    """_reinit_app_git should only act when app_dir == /app/python/source_code."""

    def test_skips_on_local_dev(self, tmp_path):
        """On local dev (any path != /app/python/source_code), function is a no-op."""
        app_mod = _import_app()
        # Create a fake .git dir in tmp_path to prove it's NOT touched
        fake_git = tmp_path / ".git"
        fake_git.mkdir()

        with mock.patch("os.path.abspath", return_value=str(tmp_path / "app.py")):
            app_mod._reinit_app_git()

        assert fake_git.is_dir(), ".git should NOT be removed on local dev"

    def test_runs_on_databricks_apps(self, tmp_path):
        """When app_dir == /app/python/source_code, reinit should execute."""
        app_mod = _import_app()
        fake_git = tmp_path / ".git"
        fake_git.mkdir()

        with mock.patch("os.path.abspath", return_value="/app/python/source_code/app.py"), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch("shutil.rmtree") as mock_rm, \
             mock.patch("subprocess.run") as mock_run:
            app_mod._reinit_app_git()

        mock_rm.assert_called_once_with("/app/python/source_code/.git")
        assert mock_run.call_count == 3  # git init, git add, git commit


# ---------------------------------------------------------------------------
# 2. Idempotency — safe on restarts
# ---------------------------------------------------------------------------

class TestIdempotency:
    """Function should be safe to call multiple times."""

    def test_skips_when_git_dir_missing(self):
        """If .git already removed (e.g. restart), function is a no-op."""
        app_mod = _import_app()

        with mock.patch("os.path.abspath", return_value="/app/python/source_code/app.py"), \
             mock.patch("os.path.isdir", return_value=False), \
             mock.patch("shutil.rmtree") as mock_rm, \
             mock.patch("subprocess.run") as mock_run:
            app_mod._reinit_app_git()

        mock_rm.assert_not_called()
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Git operations — correct commands issued
# ---------------------------------------------------------------------------

class TestGitOperations:
    """Verify the exact git commands issued during reinit."""

    def test_removes_git_dir(self):
        """Should call shutil.rmtree on the .git directory."""
        app_mod = _import_app()

        with mock.patch("os.path.abspath", return_value="/app/python/source_code/app.py"), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch("shutil.rmtree") as mock_rm, \
             mock.patch("subprocess.run"):
            app_mod._reinit_app_git()

        mock_rm.assert_called_once_with("/app/python/source_code/.git")

    def test_runs_git_init(self):
        """Should run git init in the app directory."""
        app_mod = _import_app()

        with mock.patch("os.path.abspath", return_value="/app/python/source_code/app.py"), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch("shutil.rmtree"), \
             mock.patch("subprocess.run") as mock_run:
            app_mod._reinit_app_git()

        calls = mock_run.call_args_list
        assert calls[0] == mock.call(
            ["git", "init"], cwd="/app/python/source_code", capture_output=True
        )

    def test_runs_git_add_all(self):
        """Should run git add . in the app directory."""
        app_mod = _import_app()

        with mock.patch("os.path.abspath", return_value="/app/python/source_code/app.py"), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch("shutil.rmtree"), \
             mock.patch("subprocess.run") as mock_run:
            app_mod._reinit_app_git()

        calls = mock_run.call_args_list
        assert calls[1] == mock.call(
            ["git", "add", "."], cwd="/app/python/source_code", capture_output=True
        )

    def test_runs_git_commit_with_template_message(self):
        """Should run git commit with the template message."""
        app_mod = _import_app()

        with mock.patch("os.path.abspath", return_value="/app/python/source_code/app.py"), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch("shutil.rmtree"), \
             mock.patch("subprocess.run") as mock_run:
            app_mod._reinit_app_git()

        calls = mock_run.call_args_list
        assert calls[2] == mock.call(
            ["git", "commit", "-m", "Initial commit from coding-agents template"],
            cwd="/app/python/source_code", capture_output=True,
        )


# ---------------------------------------------------------------------------
# 4. Call ordering — reinit happens after identity is written
# ---------------------------------------------------------------------------

class TestCallOrdering:
    """_reinit_app_git must be called AFTER git identity is configured."""

    def test_reinit_called_at_end_of_setup_git_config(self):
        """Verify _reinit_app_git() is the last thing _setup_git_config() does."""
        import inspect
        app_mod = _import_app()
        source = inspect.getsource(app_mod._setup_git_config)
        lines = source.strip().split("\n")

        # Find the _reinit_app_git() call
        reinit_lines = [i for i, l in enumerate(lines) if "_reinit_app_git()" in l and "def " not in l]
        assert reinit_lines, "_reinit_app_git() call not found in _setup_git_config"

        # It should be near the end (last few lines, allowing for comments/whitespace)
        last_reinit = reinit_lines[-1]
        remaining = [l.strip() for l in lines[last_reinit + 1:] if l.strip() and not l.strip().startswith("#")]
        assert remaining == [], f"Code after _reinit_app_git(): {remaining}"

    def test_gitconfig_written_before_reinit(self):
        """Verify .gitconfig is written before _reinit_app_git is called."""
        import inspect
        app_mod = _import_app()
        source = inspect.getsource(app_mod._setup_git_config)

        gitconfig_pos = source.find("Git config written to")
        reinit_pos = source.find("_reinit_app_git()")
        assert gitconfig_pos < reinit_pos, "gitconfig should be written before reinit is called"


# ---------------------------------------------------------------------------
# 5. Post-commit hook safety — hook skips app source
# ---------------------------------------------------------------------------

class TestPostCommitHookSafety:
    """The post-commit hook must not sync app source to workspace."""

    def test_hook_skips_non_project_repos(self):
        """The case statement in the hook should skip repos outside ~/projects/."""
        import inspect
        app_mod = _import_app()
        source = inspect.getsource(app_mod._setup_git_config)

        # Verify the hook has the PROJECTS_DIR guard
        assert 'PROJECTS_DIR="$HOME/projects"' in source
        assert 'case "$REPO_ROOT" in' in source
        assert '"$PROJECTS_DIR"/*)' in source
        assert "exit 0" in source

    def test_app_source_is_outside_projects_dir(self):
        """App source (/app/python/source_code) is not inside ~/projects/."""
        app_source = "/app/python/source_code"
        projects_dir = "/app/python/source_code/projects"
        assert not app_source.startswith(projects_dir + "/"), \
            "App source should NOT be inside projects dir"


# ---------------------------------------------------------------------------
# 6. .gitignore coverage
# ---------------------------------------------------------------------------

class TestGitignore:
    """Ensure .gitignore excludes sensitive/generated files from git add ."""

    @pytest.fixture
    def gitignore_content(self):
        gitignore_path = os.path.join(os.path.dirname(__file__), "..", ".gitignore")
        with open(gitignore_path) as f:
            return f.read()

    @pytest.mark.parametrize("pattern", [
        "__pycache__/",
        "*.pyc",
        ".env",
        ".venv/",
        "venv/",
    ])
    def test_gitignore_excludes(self, gitignore_content, pattern):
        assert pattern in gitignore_content, f"{pattern} missing from .gitignore"


# ---------------------------------------------------------------------------
# 7. Documentation consistency
# ---------------------------------------------------------------------------

class TestDocumentation:
    """Verify docs mention the git reinit behavior."""

    def test_deployment_docs_mention_reinit(self):
        docs_path = os.path.join(os.path.dirname(__file__), "..", "docs", "deployment.md")
        with open(docs_path) as f:
            content = f.read()
        assert "reinitializes" in content or "reinit" in content, \
            "deployment.md should mention git reinit"

    def test_readme_recommends_template(self):
        readme_path = os.path.join(os.path.dirname(__file__), "..", "README.md")
        with open(readme_path) as f:
            content = f.read()
        assert "Use this template" in content, \
            "README should recommend 'Use this template' workflow"
