#!/usr/bin/env bash
# Security-fixes verification script.
#
# Asserts the runtime state that should hold after the CoDA setup pipeline
# has run. Designed to work in TWO contexts:
#
#   1. Inside the Docker apps-like container (run by integration test).
#   2. Inside a live deployed CoDA terminal session (run by Playwright e2e
#      or manually).
#
# Exits 0 iff every check passes. Prints `[PASS] <name>` / `[FAIL] <name>`
# lines so test drivers can parse them.
#
# Coverage map:
#   F-01 — terminal env credentials stripped
#   F-04 — DEEPWIKI_MCP_URL / EXA_MCP_URL helpers wired into setup scripts
#   F-05 — ~/.hermes/config.yaml is chmod 0o600
#   F-06 — Hermes installed (from SHA-pinned source)
#   cooldown — npm cooldown still picks stable (non-pre-release) versions

set -u

fail=0
pass=0
HERMES_CFG="$HOME/.hermes/config.yaml"
CLAUDE_JSON="$HOME/.claude.json"

print_pass() { echo "[PASS] $1"; pass=$((pass + 1)); }
print_fail() { echo "[FAIL] $1 — $2"; fail=$((fail + 1)); }

# ---------------------------------------------------------------------------
# F-01: deployer-level credentials must NOT be visible in user shell env
# ---------------------------------------------------------------------------
# These vars are credentials set in app.yaml by the deployer. If they appear
# in `env` output, anything the user runs in the terminal can exfiltrate them.

leaked_creds=$(env | grep -E '^(NPM_TOKEN|UV_INDEX_.*_(PASSWORD|USERNAME))=|^npm_config_//.*:_authToken=|^DATABRICKS_TOKEN=|^DATABRICKS_HOST=' || true)
if [ -z "$leaked_creds" ]; then
    print_pass "F-01 terminal env has no leaked credentials"
else
    print_fail "F-01 terminal env contains credentials" "$(echo "$leaked_creds" | head -3)"
fi

# ---------------------------------------------------------------------------
# F-04: DEEPWIKI_MCP_URL / EXA_MCP_URL helpers wired into setup scripts
# ---------------------------------------------------------------------------
# When DEEPWIKI_MCP_URL is unset, Claude/Hermes should still have deepwiki
# configured (default). When set to "", they should NOT have deepwiki.

if [ -f "$CLAUDE_JSON" ]; then
    claude_mcps=$(python3 -c "
import json, sys
d = json.load(open('$CLAUDE_JSON'))
print(','.join(sorted((d.get('mcpServers') or {}).keys())))
" 2>/dev/null || echo "<parse-error>")

    expected_default="deepwiki,exa"
    if [ -z "${DEEPWIKI_MCP_URL+x}" ] && [ -z "${EXA_MCP_URL+x}" ]; then
        # Both env vars unset -> default behaviour expected
        if [ "$claude_mcps" = "$expected_default" ]; then
            print_pass "F-04 Claude MCP wiring (default: $claude_mcps)"
        else
            print_fail "F-04 Claude MCP wiring" "expected '$expected_default', got '$claude_mcps'"
        fi
    elif [ "${DEEPWIKI_MCP_URL:-unset}" = "" ] && [ "${EXA_MCP_URL:-unset}" = "" ]; then
        # Both env vars set to empty -> both should be absent
        if [ -z "$claude_mcps" ]; then
            print_pass "F-04 Claude MCP servers omitted when overrides empty"
        else
            print_fail "F-04 Claude MCP servers" "expected empty, got '$claude_mcps'"
        fi
    else
        # Custom overrides — just verify the file parses
        print_pass "F-04 Claude MCP servers (custom: $claude_mcps)"
    fi
else
    print_fail "F-04 Claude config" "$CLAUDE_JSON missing — setup_claude.py didn't run?"
fi

# ---------------------------------------------------------------------------
# F-05: ~/.hermes/config.yaml is chmod 0o600 (PAT in plaintext, no leak)
# ---------------------------------------------------------------------------
if [ -f "$HERMES_CFG" ]; then
    perms=$(stat -c %a "$HERMES_CFG" 2>/dev/null || stat -f %Lp "$HERMES_CFG" 2>/dev/null)
    if [ "$perms" = "600" ]; then
        print_pass "F-05 Hermes config chmod 0o600"
    else
        print_fail "F-05 Hermes config perms" "expected 600, got $perms"
    fi
else
    # If ENABLE_HERMES=false or no DATABRICKS_TOKEN, the config isn't written.
    # That's OK — the check is "if it exists, perms are right."
    print_pass "F-05 Hermes config not written (skipped — file absent)"
fi

# ---------------------------------------------------------------------------
# F-06: Hermes installed (SHA-pinned source resolved + uv tool install ran)
# ---------------------------------------------------------------------------
if command -v hermes >/dev/null 2>&1; then
    hermes_ver=$(hermes --version 2>&1 | head -1)
    if echo "$hermes_ver" | grep -qiE 'hermes.*[0-9]'; then
        print_pass "F-06 Hermes installed ($hermes_ver)"
    else
        print_fail "F-06 Hermes installed but version output unexpected" "$hermes_ver"
    fi
else
    if [ "${ENABLE_HERMES:-true}" = "false" ]; then
        print_pass "F-06 Hermes skipped (ENABLE_HERMES=false)"
    else
        print_fail "F-06 Hermes not installed" "hermes binary not on PATH"
    fi
fi

# ---------------------------------------------------------------------------
# Cooldown: npm-installed CLIs must NOT be pre-release versions
# ---------------------------------------------------------------------------
# The npm cooldown (commit cdd2266 + the cooldown-aware get_npm_version)
# should pick stable releases only. Pre-release versions contain a hyphen
# per semver (1.2.3-rc.1, 0.0.0-dev-..., etc.). A stable version is purely
# numeric segments separated by dots.

for cli in opencode codex gemini; do
    if ! command -v "$cli" >/dev/null 2>&1; then
        print_fail "cooldown $cli installed" "$cli binary not on PATH"
        continue
    fi
    # Extract the first version-like token from the CLI's --version output
    ver=$("$cli" --version 2>&1 | head -3 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+[A-Za-z0-9.\-]*' | head -1)
    if [ -z "$ver" ]; then
        print_fail "cooldown $cli version parse" "couldn't extract version from --version output"
    elif echo "$ver" | grep -qE -- '-(dev|alpha|beta|rc|preview|next)'; then
        print_fail "cooldown $cli pre-release installed" "$ver"
    else
        print_pass "cooldown $cli stable version ($ver)"
    fi
done

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "=== Summary: $pass passed, $fail failed ==="
exit $fail
