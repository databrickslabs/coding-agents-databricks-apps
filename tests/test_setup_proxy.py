"""Regression tests for setup/setup_proxy.py — the content-filter proxy launcher.

The launcher spawns ``content_filter_proxy.py`` as a subprocess. That file lives
at the REPO ROOT, not in setup/. A 2026 refactor moved setup_proxy.py into
setup/ (git fec2152, R100 rename) without updating its relative path lookup, so
the launcher pointed at a nonexistent ``setup/content_filter_proxy.py`` and the
proxy never started — silently breaking OpenCode (the only agent that routes
through the proxy at 127.0.0.1:4000). These tests pin the resolved path to an
existing file so a future move can't regress it again.
"""

import importlib.util
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETUP_PROXY_PATH = os.path.join(REPO_ROOT, "setup", "setup_proxy.py")


def _load_setup_proxy():
    """Import setup_proxy.py by path WITHOUT running its main() side effects."""
    spec = importlib.util.spec_from_file_location("setup_proxy_under_test", SETUP_PROXY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_resolved_proxy_script_exists():
    """The script the launcher hands to Popen must actually exist on disk."""
    mod = _load_setup_proxy()
    path = mod.resolve_proxy_script_path()
    assert os.path.isfile(path), (
        f"setup_proxy.py resolves the proxy server to a non-existent path: {path}. "
        f"content_filter_proxy.py lives at the repo root, not in setup/."
    )


def test_resolved_proxy_script_is_repo_root_content_filter_proxy():
    """It must be the repo-root content_filter_proxy.py, not a setup/-relative path."""
    mod = _load_setup_proxy()
    path = mod.resolve_proxy_script_path()
    assert os.path.basename(path) == "content_filter_proxy.py"
    assert os.path.dirname(os.path.abspath(path)) == REPO_ROOT, (
        f"expected the repo-root copy ({REPO_ROOT}), got dir "
        f"{os.path.dirname(os.path.abspath(path))}"
    )
