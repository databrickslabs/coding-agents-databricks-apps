# End-to-end tests against a live CoDA deployment

These tests drive a real deployed CoDA app via Playwright. They exist
because the Docker integration tests can't replicate the Databricks Apps
SSO flow or PAT-rotator behaviour.

## What gets tested

- F-01 — terminal env credential strip (live PTY)
- F-04 — DEEPWIKI/EXA MCP wiring in `~/.claude.json` and `~/.hermes/config.yaml`
- F-05 — `~/.hermes/config.yaml` chmod 0o600
- F-06 — Hermes installed (SHA-pinned source resolved + `uv tool install` ran)
- Cooldown — npm CLIs are stable versions, not pre-releases

## One-time setup

### 1. Install Playwright + browser

```
uv sync --group dev
uv run playwright install chromium
```

### 2. Record your Databricks SSO session

```
make e2e-auth PROFILE=daveok
```

That launches a headed Chromium window pointed at the CoDA app URL.
Complete the Databricks Apps SSO login in the browser (Microsoft Entra
or whatever your workspace uses). Once you land on the CoDA terminal
page, close the window — Playwright will save the session cookies to
`tests/e2e/auth.json`.

The recorded `auth.json` contains workspace cookies. It's gitignored.
If you commit it by accident, revoke the cookies via your Databricks
account settings.

### 3. Make sure the Databricks CLI is authed

```
databricks current-user me --profile daveok
```

The fixtures use the CLI to mint a fresh PAT for each test run.

## Running the tests

```
make e2e-test PROFILE=daveok
# or directly:
uv run pytest tests/e2e/test_live_security.py -v
```

Wall time: ~2 min per test (PAT mint + container setup + verify).
LLM tokens per run: zero — Playwright drives the browser autonomously.

## Re-recording auth

Auth cookies expire (Databricks' default is hours, sometimes days). When
the e2e tests start failing with "could not find PAT prompt" or similar,
re-run `make e2e-auth` to refresh `auth.json`.

## What if Playwright isn't installed

The whole module skips cleanly via `pytest.importorskip("playwright.sync_api")`.
The unit + Docker integration tests don't depend on Playwright.

## Why not just run Selenium / Cypress / chrome-devtools MCP?

- **Selenium / Cypress** would work — Playwright was picked because it's
  the most reliable + fastest for SSO flows (built-in storage_state, no
  flaky driver setup) and has first-class Python bindings.
- **chrome-devtools MCP** is what we used during the security review
  itself — it's interactive and great for one-off exploration, but every
  step spends LLM tokens. Playwright is the codified version that runs
  without an LLM in the loop.
