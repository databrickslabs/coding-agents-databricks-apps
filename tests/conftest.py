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
