"""End-to-end security-fix verification against a live deployed CoDA app.

This is the codified version of the chrome-devtools MCP-driven session
that verified the F-01/F-04/F-05/F-06 fixes on <profile>. It replaces the
manual "open browser, paste PAT, run commands" loop with a Playwright
test that runs autonomously.

Prerequisites (one-time setup):
  1. `make e2e-auth` — records your Databricks SSO session to auth.json.
  2. Databricks CLI authed for the target profile (default: <profile>).

To run: `make e2e-test PROFILE=<profile>` or
        `uv run pytest tests/e2e/test_live_security.py`

Wall time per run: ~2 min (PAT + setup + verify).
LLM tokens per run: zero.
"""

from __future__ import annotations

import time

import pytest

# Skip the entire module cleanly if Playwright isn't installed.
playwright = pytest.importorskip("playwright.sync_api")


def _send_input(page, sid: str, cmd: str) -> None:
    """POST a command + trailing newline to the app's /api/input endpoint."""
    page.evaluate(
        """async ({sid, cmd}) => {
            await fetch('/api/input', {
                method: 'POST',
                credentials: 'include',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({session_id: sid, input: cmd + '\\n'}),
            });
        }""",
        {"sid": sid, "cmd": cmd},
    )


def _xterm_text(page) -> str:
    """Concatenate the currently-rendered xterm rows into one string."""
    return page.evaluate(
        """() => {
            const rows = document.querySelectorAll('.xterm-rows > div');
            return Array.from(rows).map(r => r.textContent || '').join('\\n');
        }"""
    )


def _read_session_id(page) -> str:
    """Find the active terminal session id via the app's own /api/sessions."""
    info = page.evaluate(
        """async () => {
            const r = await fetch('/api/sessions', {credentials: 'include'});
            return r.json();
        }"""
    )
    return info["sessions"][0]["session_id"]


def test_live_app_security_fixes(page, app_url, fresh_pat):
    """Drive the live CoDA app and assert the security-fixes verify script passes.

    Steps:
      1. Load the app (SSO state already in the browser context via conftest).
      2. Wait for the PAT prompt.
      3. Fill the PAT into the xterm input.
      4. Wait for "Ready" (setup pipeline complete).
      5. Run the verify.sh assertions via /api/input.
      6. Scrape the xterm DOM and assert no [FAIL] markers.
    """
    page.goto(app_url, timeout=30_000)
    # The PAT prompt arrives via the terminal output, not as a normal DOM
    # element. Wait for the trailing "Token:" prompt to appear in xterm.
    page.wait_for_function(
        """() => document.body.innerText.includes('Token:')""",
        timeout=60_000,
    )

    # Paste the fresh PAT into the xterm input. xterm.js routes keyboard
    # events through a hidden textarea labelled "Terminal input".
    terminal_input = page.get_by_role("textbox", name="Terminal input")
    terminal_input.fill(fresh_pat + "\n")

    # The "Ready" banner appears once setup completes (~60-90s on a fresh
    # container). Time out generously.
    page.wait_for_function(
        """() => document.body.innerText.includes('Ready')""",
        timeout=180_000,
    )

    # Get the active session id, then send the verify.sh assertions.
    sid = _read_session_id(page)

    # We run verify.sh from the repo path that's synced to the workspace.
    # The Databricks Apps env mounts the source at /app/python/source_code.
    verify_path = "/app/python/source_code/tests/integration/verify.sh"
    _send_input(page, sid, f'bash {verify_path}; echo "VERIFY-EXIT-CODE=$?"')

    # Poll xterm for the EXIT-CODE marker (verify.sh runs in ~5s).
    deadline = time.time() + 60
    output = ""
    while time.time() < deadline:
        output = _xterm_text(page)
        if "VERIFY-EXIT-CODE=" in output:
            break
        time.sleep(1)
    assert "VERIFY-EXIT-CODE=" in output, (
        f"verify.sh never reported an exit code. xterm contents:\n{output[-3000:]}"
    )

    # Extract the exit code line and the [FAIL] markers
    import re
    exit_match = re.search(r"VERIFY-EXIT-CODE=(\d+)", output)
    exit_code = int(exit_match.group(1)) if exit_match else -1

    fail_lines = [line for line in output.splitlines() if "[FAIL]" in line]
    pass_lines = [line for line in output.splitlines() if "[PASS]" in line]

    # Print full output for CI logs
    print("\n=== verify.sh output (last 60 lines) ===")
    for line in output.splitlines()[-60:]:
        print(line)
    print(f"\nVERIFY-EXIT-CODE = {exit_code}")
    print(f"PASS count: {len(pass_lines)}")
    print(f"FAIL count: {len(fail_lines)}")

    assert exit_code == 0, (
        f"verify.sh exited non-zero ({exit_code}). Failures:\n"
        + "\n".join(fail_lines)
    )
    assert not fail_lines, f"verify.sh emitted [FAIL] lines:\n" + "\n".join(fail_lines)
    # And explicitly check the critical fixes are observable
    must_pass = ["F-01", "F-05", "F-06", "cooldown opencode", "cooldown codex", "cooldown gemini"]
    missing = [m for m in must_pass if not any(m in p for p in pass_lines)]
    assert not missing, f"Expected [PASS] markers missing: {missing}"
