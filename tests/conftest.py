"""Shared pytest fixtures.

The most important one is `_isolate_home` — it forces tests that touch
filesystem state under `$HOME` to see a throwaway directory. Without it,
pat_rotator's `_write_databrickscfg` (which opens `$HOME/.databrickscfg`
in `"w"` mode) can clobber a developer's real `~/.databrickscfg` during
a routine `pytest` run.

Scope is limited to the test files known to construct PatRotator instances
or invoke `app.initialize_app` — tests that need a real HOME (e.g. the live
npm integration test, which relies on the user's npm cache) keep it.
"""

import pytest


_HOME_WRITERS = frozenset({
    "tests.test_pat_rotator",
    "tests.test_mlflow_tracing",
    "tests.test_app",
})


@pytest.fixture(autouse=True)
def _isolate_home(request, tmp_path, monkeypatch):
    """Point HOME at a tmp dir for tests that may write under it.

    Opt-in by module name. If a future test starts writing to HOME, add
    its module to `_HOME_WRITERS` rather than letting it leak to the
    real filesystem.
    """
    if request.module.__name__ in _HOME_WRITERS:
        monkeypatch.setenv("HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def _restore_real_app_hooks():
    """Keep mcp_server's PTY hooks pointed at app's real implementations.

    set_app_hooks() mutates process-wide module globals in coda_mcp.mcp_server.
    Several test files clear or mock those hooks for their own cases — e.g.
    test_mcp_server._reset_hooks sets them to None in teardown, and
    test_mcp_integration.isolated_env does set_app_hooks(None, None, None). That
    cleared state LEAKED into later files: test_replay_only_flag's coda_run then
    saw _app_create_session is None and created no PTY, so it failed only in
    full-suite runs (never in isolation, where app's import re-wired the hooks).

    Re-establishing app's real hooks AFTER every test makes hook state
    independent of file order. Tests that need mocks/None still set them in
    their own setup — this only governs the post-test baseline. No-op until
    `app` has been imported (and for the few tests that run before that)."""
    yield
    import sys
    app_mod = sys.modules.get("app")
    ms = sys.modules.get("coda_mcp.mcp_server")
    if app_mod is not None and ms is not None:
        ms.set_app_hooks(
            app_mod.mcp_create_pty_session,
            app_mod.mcp_send_input,
            app_mod.mcp_close_pty_session,
        )
