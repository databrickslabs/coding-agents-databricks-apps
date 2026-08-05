"""End-to-end security-fix verification against a live deployed CoDA app.

This is the codified version of the chrome-devtools MCP-driven session
that verified the F-01/F-04/F-05/F-06 fixes on daveok. It replaces the
manual "open browser, paste PAT, run commands" loop with a Playwright
test that runs autonomously.

Prerequisites (one-time setup):
  1. `make e2e-auth` — records your Databricks SSO session to auth.json.
  2. Databricks CLI authed for the target profile (default: daveok).

To run: `make e2e-test PROFILE=daveok` or
        `uv run pytest tests/e2e/test_live_security.py`

The test is SELF-CONTAINED: it base64-encodes the local verify.sh and
sends it through the PTY, so it doesn't depend on the deployed branch
having verify.sh on disk. This matters because the test infrastructure
was added AFTER the security-fix deploy on daveok — without inlining,
the test would require a re-deploy first.

Wall time per run: ~30s (no PAT setup; reuses existing bash session).
LLM tokens per run: zero.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

import pytest

# Skip the entire module cleanly if Playwright isn't installed.
playwright = pytest.importorskip("playwright.sync_api")

VERIFY_SH = Path(__file__).resolve().parent.parent / "integration" / "verify.sh"


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


def _read_output(page, sid: str) -> str:
    """Drain the per-session output buffer via /api/output.

    Each poll consumes the buffer — the app replaces it with a fresh deque
    on every read. Caller should accumulate across polls.
    """
    info = page.evaluate(
        """async ({sid}) => {
            const r = await fetch('/api/output', {
                method: 'POST',
                credentials: 'include',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({session_id: sid}),
            });
            return r.json();
        }""",
        {"sid": sid},
    )
    return info.get("output", "")


def _read_session_id(page) -> str:
    """Find a bash terminal session id via the app's own /api/sessions.

    /api/sessions returns a bare list of session dicts (not wrapped).
    We prefer a bash session over claude/codex/etc. so verify.sh runs
    in a plain shell. Falls back to the first session if none are bash.
    """
    sessions = page.evaluate(
        """async () => {
            const r = await fetch('/api/sessions', {credentials: 'include'});
            return r.json();
        }"""
    )
    if not sessions:
        raise RuntimeError("No sessions returned by /api/sessions")
    for s in sessions:
        if s.get("process") == "bash":
            return s["session_id"]
    return sessions[0]["session_id"]


def _verify_command() -> str:
    """Build a one-liner that decodes + runs verify.sh from the test repo.

    Base64-encodes the local verify.sh and decodes inside the container —
    sidesteps every shell-escape pitfall (quotes, newlines, $ vars) and
    means the test doesn't depend on verify.sh being deployed to the app.
    """
    if not VERIFY_SH.exists():
        raise RuntimeError(f"verify.sh missing at {VERIFY_SH}")
    b64 = base64.b64encode(VERIFY_SH.read_bytes()).decode()
    # Write to /tmp inside the container, run it, echo exit code marker
    return (
        f'echo {b64} | base64 -d > /tmp/coda_verify.sh && '
        f'bash /tmp/coda_verify.sh; '
        f'echo "VERIFY-EXIT-CODE=$?"'
    )


def test_live_app_security_fixes(page, app_url, fresh_pat):
    """Drive the live CoDA app and assert the security-fixes verify script passes.

    Architectural choice: this test sends commands and reads output via the
    HTTP API (/api/input + /api/output) directly, NOT by scraping the xterm
    DOM. Reasons:
      - The xterm DOM only shows the currently-attached session, but we
        want to drive a specific bash session regardless of UI state.
      - /api/output drains the per-session buffer, so polling captures
        everything the PTY emitted whether or not the UI renders it.
      - Works regardless of which startup state the page is in (PAT prompt,
        session selector, attached terminal).

    Handles three startup states the app can be in:
      (a) Fresh container — shows the "Token:" PAT prompt → fills PAT
      (b) PAT configured AND existing bash session — uses it directly
      (c) PAT configured but no bash session — picks "n" / new session
    """
    page.goto(app_url, timeout=30_000)

    # Wait for any of the known startup states to render. The page may take
    # 1-3s for the WebSocket to connect and for xterm to draw.
    page.wait_for_function(
        """() => {
            const t = document.body.innerText;
            return t.includes('Token:')
                || t.includes('active sessions')
                || t.includes('Ready')
                || t.includes('? for shortcuts')
                || /\\$\\s*$/m.test(t);
        }""",
        timeout=90_000,
    )

    body = page.evaluate("() => document.body.innerText")
    terminal_input = page.get_by_role("textbox", name="Terminal input")

    if "Token:" in body:
        # Fresh container — paste the PAT and wait for setup to complete.
        terminal_input.fill(fresh_pat + "\n")
        page.wait_for_function(
            """() => document.body.innerText.includes('Ready')""",
            timeout=180_000,
        )

    # Discover or create a bash session id. /api/sessions is authoritative
    # regardless of which terminal the UI is currently rendering.
    sessions = page.evaluate(
        """async () => (await (await fetch('/api/sessions')).json())"""
    )
    bash_sids = [s["session_id"] for s in sessions if s.get("process") == "bash"]
    if bash_sids:
        sid = bash_sids[0]
    elif "active sessions" in body:
        # Spawn a new (bash) session via the UI's session selector.
        terminal_input.fill("n\n")
        time.sleep(3)
        sessions = page.evaluate(
            """async () => (await (await fetch('/api/sessions')).json())"""
        )
        bash_sids = [s["session_id"] for s in sessions if s.get("process") == "bash"]
        assert bash_sids, "no bash session after picking 'new'"
        sid = bash_sids[0]
    else:
        # No bash sessions and not at the selector — call /api/session POST
        # to create one (the same endpoint the UI uses on "+ new tab").
        new_session = page.evaluate(
            """async () => {
                const r = await fetch('/api/session', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({label: 'e2e-test'}),
                });
                return r.json();
            }"""
        )
        sid = new_session["session_id"]
        # Brief wait for the PTY to spawn before we send input.
        time.sleep(2)

    # Drain whatever's in the buffer first so our verify output isn't mixed
    # with prior content.
    _read_output(page, sid)

    # Send verify.sh (inlined via base64 so we don't depend on the deployed
    # branch having tests/integration/verify.sh on disk).
    _send_input(page, sid, _verify_command())

    # Poll /api/output until the EXIT-CODE marker WITH A DIGIT appears.
    # The bare string "VERIFY-EXIT-CODE=" also appears in the *echo* of
    # the sent command (before the script runs), so checking for that
    # substring alone exits the loop too early. Wait for the actual
    # script-produced "VERIFY-EXIT-CODE=<digits>".
    import re
    exit_re = re.compile(r"VERIFY-EXIT-CODE=(\d+)")
    deadline = time.time() + 120  # verify.sh runtime ~30-45s; allow buffer race
    accumulated = ""
    exit_match = None
    while time.time() < deadline:
        chunk = _read_output(page, sid)
        if chunk:
            accumulated += chunk
        exit_match = exit_re.search(accumulated)
        if exit_match:
            break
        time.sleep(1)

    assert exit_match, (
        f"verify.sh never reported an exit code (with digit) in 120s. "
        f"Accumulated output:\n{accumulated[-3000:]}"
    )

    exit_code = int(exit_match.group(1))
    fail_lines = [line for line in accumulated.splitlines() if "[FAIL]" in line]
    pass_lines = [line for line in accumulated.splitlines() if "[PASS]" in line]

    print("\n=== verify.sh output (last 60 lines) ===")
    for line in accumulated.splitlines()[-60:]:
        print(line)
    print(f"\nVERIFY-EXIT-CODE = {exit_code}")
    print(f"PASS count: {len(pass_lines)}")
    print(f"FAIL count: {len(fail_lines)}")

    assert exit_code == 0, (
        f"verify.sh exited non-zero ({exit_code}). Failures:\n"
        + "\n".join(fail_lines)
    )
    assert not fail_lines, "verify.sh emitted [FAIL] lines:\n" + "\n".join(fail_lines)
    must_pass = ["F-01", "F-05", "F-06", "cooldown opencode", "cooldown codex", "cooldown gemini"]
    missing = [m for m in must_pass if not any(m in p for p in pass_lines)]
    assert not missing, f"Expected [PASS] markers missing: {missing}"
