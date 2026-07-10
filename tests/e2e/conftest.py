"""Playwright e2e fixtures for the live CoDA app.

Tests in this directory drive a real deployed CoDA instance — they exist
because Docker can't replicate the Databricks Apps SSO + PAT-rotator
flow. Each test:

1. Loads pre-recorded SSO auth state (`auth.json`) so the browser starts
   already logged in to Databricks. See README for how to record it.
2. Mints a fresh PAT via the Databricks CLI.
3. Drives the app via Playwright + the same /api/input + DOM-scrape
   pattern the chrome-devtools MCP session used.

Token cost per run: zero LLM tokens. Wall time: ~1-2 min per test.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AUTH_STATE = Path(__file__).parent / "auth.json"


def _databricks_profile() -> str:
    return os.environ.get("DATABRICKS_PROFILE", "daveok")


def _app_url() -> str:
    """Resolve the CoDA app URL for the configured profile.

    Reads `databricks apps get coding-agents --profile <profile>` so the
    test doesn't have to hardcode workspace URLs.
    """
    override = os.environ.get("CODA_APP_URL", "").strip()
    if override:
        return override
    result = subprocess.run(
        [
            "databricks", "apps", "get", "coding-agents",
            "--profile", _databricks_profile(), "--output", "json",
        ],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        pytest.skip(
            f"Cannot resolve app URL (databricks apps get failed): "
            f"{result.stderr.strip()}"
        )
    import json
    return json.loads(result.stdout)["url"]


def _mint_pat() -> str:
    """Mint a short-lived PAT via the Databricks CLI for the test session."""
    result = subprocess.run(
        [
            "databricks", "tokens", "create",
            "--lifetime-seconds", "3600",   # 1h — comfortably covers the test
            "--comment", "coda-e2e-test",
            "--profile", _databricks_profile(),
            "--output", "json",
        ],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        pytest.skip(
            f"Cannot mint PAT (databricks tokens create failed): "
            f"{result.stderr.strip()}"
        )
    import json
    return json.loads(result.stdout)["token_value"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(config, items):
    """Skip the whole e2e suite if prerequisites aren't met."""
    skips = []
    if not AUTH_STATE.exists():
        skips.append(
            f"missing {AUTH_STATE.relative_to(REPO_ROOT)} — "
            f"run `make e2e-auth` first to record SSO session"
        )
    if subprocess.run(
        ["databricks", "current-user", "me", "--profile", _databricks_profile()],
        capture_output=True, timeout=10,
    ).returncode != 0:
        skips.append(
            f"databricks CLI not authed for profile {_databricks_profile()!r} — "
            f"run `databricks auth login --profile {_databricks_profile()}`"
        )
    if skips:
        skip_marker = pytest.mark.skip(reason=" | ".join(skips))
        for item in items:
            item.add_marker(skip_marker)


@pytest.fixture(scope="module")
def app_url() -> str:
    return _app_url()


@pytest.fixture(scope="module")
def auth_state_path() -> str:
    return str(AUTH_STATE)


@pytest.fixture
def fresh_pat() -> str:
    """A freshly-minted 1h PAT. New token per test to avoid cross-test bleed."""
    return _mint_pat()


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    """Inject the recorded SSO storage state into every Playwright context."""
    return {**browser_context_args, "storage_state": str(AUTH_STATE)}
