"""Tests for _build_terminal_shell_env's credential-stripping behavior.

Replaces the inline 5-key strip that mcp_create_pty_session used to do.
Both create_session (HTTP path) and mcp_create_pty_session (MCP path)
now call this helper, so it must strip both the original 5 keys and
the registry-credential patterns the HTTP path was already covering.
"""
import os
import pytest

from app import _build_terminal_shell_env


# Keys that must be absent from the child shell's env after the strip.
STRIPPED_KEYS = [
    "CLAUDECODE",
    "CLAUDE_CODE_SESSION",
    "DATABRICKS_TOKEN",
    "DATABRICKS_HOST",
    "GEMINI_API_KEY",
    "NPM_TOKEN",
    "UV_DEFAULT_INDEX",
    "UV_INDEX_MYREG_PASSWORD",
    "UV_INDEX_MYREG_USERNAME",
    "npm_config_//registry.example/:_authToken",
]


@pytest.mark.parametrize("key", STRIPPED_KEYS)
def test_build_terminal_shell_env_strips_credential_key(key):
    """Each known credential / registry-auth key is stripped from the child env."""
    fake_env = {
        "PATH": "/usr/bin:/usr/local/bin",  # positive control — must survive
        "HOME": "/home/test",
        key: "leak-me-test-value",
    }
    result = _build_terminal_shell_env(fake_env)
    assert key not in result, (
        f"{key} survived the strip — registry/auth credential leaked into "
        f"the child shell's env. Result keys: {sorted(result)}"
    )


def test_build_terminal_shell_env_preserves_benign_keys():
    """Positive control: non-credential keys survive the strip.

    Guards against a future regression where the strip becomes too aggressive
    and wipes the env entirely. If THIS test fails, the negative assertions
    above would silently pass for the wrong reason.
    """
    fake_env = {
        "PATH": "/usr/bin:/usr/local/bin",
        "HOME": "/home/test",
        "LANG": "en_US.UTF-8",
    }
    result = _build_terminal_shell_env(fake_env)
    assert result.get("PATH") and "/usr/bin" in result["PATH"]
    assert result.get("HOME") == "/home/test"
    assert result.get("LANG") == "en_US.UTF-8"


try:
    import pty as _pty
    _master, _slave = _pty.openpty()
    os.close(_master)
    os.close(_slave)
    _PTY_AVAILABLE = True
except Exception:
    _PTY_AVAILABLE = False

_pty_skip = pytest.mark.skipif(
    not _PTY_AVAILABLE,
    reason="PTY not allocatable in this environment",
)


@_pty_skip
def test_mcp_create_pty_session_respects_cwd_kwarg(tmp_path):
    """When cwd is passed, sessions[sid]['cwd'] records it."""
    from app import mcp_create_pty_session, mcp_close_pty_session, sessions

    sid = None
    try:
        sid = mcp_create_pty_session(label="t-cwd", cwd=str(tmp_path))
        assert sessions[sid].get("cwd") == str(tmp_path)
    finally:
        if sid is not None:
            mcp_close_pty_session(sid)


@_pty_skip
def test_mcp_create_pty_session_cwd_defaults_to_none():
    """When cwd is not passed, sessions[sid]['cwd'] is None (preserves current behavior)."""
    from app import mcp_create_pty_session, mcp_close_pty_session, sessions

    sid = None
    try:
        sid = mcp_create_pty_session(label="t-no-cwd")
        assert sessions[sid].get("cwd") is None
    finally:
        if sid is not None:
            mcp_close_pty_session(sid)


@_pty_skip
def test_mcp_close_pty_session_removes_project_dir(tmp_path, monkeypatch):
    """When the PTY is closed, any project dir at ~/.coda/projects/<pty_id>/ is removed."""
    import os
    from app import mcp_create_pty_session, mcp_close_pty_session

    # Point HOME at tmp_path so ~/.coda lives in a controllable place.
    monkeypatch.setenv("HOME", str(tmp_path))

    sid = None
    try:
        sid = mcp_create_pty_session(label="t-cleanup")

        project_dir = os.path.join(str(tmp_path), ".coda", "projects", sid)
        os.makedirs(project_dir, exist_ok=True)
        sentinel = os.path.join(project_dir, "SENTINEL")
        with open(sentinel, "w") as f:
            f.write("present-before-close")
        assert os.path.exists(sentinel)

        mcp_close_pty_session(sid)
        sid = None  # session closed; don't double-close in finally

        assert not os.path.exists(project_dir), \
            f"Expected project dir to be removed after PTY close: {project_dir} still exists"
    finally:
        if sid is not None:
            try:
                mcp_close_pty_session(sid)
            except Exception:
                pass


@_pty_skip
def test_mcp_close_pty_session_handles_missing_project_dir(tmp_path, monkeypatch):
    """No project dir present → close still succeeds (no exception)."""
    from app import mcp_create_pty_session, mcp_close_pty_session

    monkeypatch.setenv("HOME", str(tmp_path))

    sid = None
    try:
        sid = mcp_create_pty_session(label="t-no-projdir")
        # Do NOT create the project dir — verify close still works.
        mcp_close_pty_session(sid)  # must not raise
        sid = None
    finally:
        if sid is not None:
            try:
                mcp_close_pty_session(sid)
            except Exception:
                pass


@_pty_skip
def test_terminate_session_removes_project_dir(tmp_path, monkeypatch):
    """The idle reaper calls terminate_session directly. Project dir must still be cleaned."""
    import os
    from app import mcp_create_pty_session, sessions, terminate_session, mcp_close_pty_session

    monkeypatch.setenv("HOME", str(tmp_path))

    sid = None
    try:
        sid = mcp_create_pty_session(label="t-reaper-cleanup")

        # Plant a project dir like coda_interactive would have done.
        project_dir = os.path.join(str(tmp_path), ".coda", "projects", sid)
        os.makedirs(project_dir, exist_ok=True)
        with open(os.path.join(project_dir, "SENTINEL"), "w") as f:
            f.write("present")
        assert os.path.exists(project_dir)

        # Simulate the reaper's code path: call terminate_session directly.
        sess = sessions[sid]
        terminate_session(sid, sess["pid"], sess["master_fd"])
        sid = None  # session terminated; finally is a no-op

        # Project dir must be removed even though we bypassed mcp_close_pty_session.
        assert not os.path.exists(project_dir), \
            "Reaper path must also clean up the project dir — fix terminate_session not mcp_close_pty_session"
    finally:
        if sid is not None:
            try:
                mcp_close_pty_session(sid)
            except Exception:
                pass
