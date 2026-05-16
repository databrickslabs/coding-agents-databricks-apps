#!/usr/bin/env bash
# Run the CoDA setup pipeline inside the apps-like container, then verify.
#
# Invoked by `tests/integration/test_setup_pipeline.py` after the container
# is started and the repo is mounted at /work. We don't shell into the
# container interactively — we just run this script as the entry command.
#
# Mirrors the order app.py:run_setup() uses on the live app: install_*.sh
# sequentially, then setup_*.py in series (we skip parallelism here for
# log readability). Skips setup_proxy.py (the localhost content-filter
# proxy isn't relevant for the security-fix checks) and setup_mlflow.py
# (MLflow tracing isn't part of the security-fix scope).

set -eo pipefail

# The repo is mounted read-only at /repo. Copy to a writable /work for uv
# venv + setup-script side effects. Excludes .venv (huge) and .git.
echo "============================================================"
echo "CoDA apps-like integration test — pipeline + verify"
echo "============================================================"
echo "HOME=$HOME"
echo "USER=$(id -u -n)"
echo "PATH=$PATH"
echo

mkdir -p /work
cd /repo && cp -a --no-preserve=ownership \
    requirements.txt pyproject.toml app.py utils.py app_state.py \
    pat_rotator.py telemetry.py cli_auth.py enterprise_config.py \
    install_micro.sh install_gh.sh install_databricks_cli.sh \
    setup_proxy.py setup_claude.py setup_codex.py setup_gemini.py \
    setup_opencode.py setup_hermes.py setup_databricks.py setup_mlflow.py \
    content_filter_proxy.py CLAUDE.md \
    /work/ 2>/dev/null || true
# Also need the tests dir for verify.sh
mkdir -p /work/tests/integration
cp /repo/tests/integration/verify.sh /work/tests/integration/
# And the agents/skills directories that setup_claude.py references
[ -d /repo/agents ] && cp -a --no-preserve=ownership /repo/agents /work/ || true
[ -d /repo/.claude ] && cp -a --no-preserve=ownership /repo/.claude /work/ || true
cd /work

# Stage 1: install pinned deps (mirrors what `databricks apps deploy` does
# at the [BUILD] step — pip, not uv pip). Two adjustments vs the real Apps
# build:
#   1. Pre-install setuptools/wheel so pip's build-isolation subprocess
#      doesn't fail trying to reach pypi with a fresh trust store.
#   2. `--no-build-isolation` — pip's isolated build env doesn't inherit
#      our CA bundle env vars, which breaks corporate-proxied networks.
#      The real Apps build runs in an env that has setuptools available.
echo ">>> Stage 1: pip install -r requirements.txt"
# Use python3 -m venv (not `uv venv`) so pip is seeded into the venv.
# `uv venv` skips pip by default, which caused `pip` to resolve to the
# system Python's pip (user-install mode, no CA bundle env), breaking
# package resolution under corporate TLS interception.
python3 -m venv .venv
. .venv/bin/activate
pip install --no-cache-dir --upgrade pip setuptools wheel
pip install --no-cache-dir --no-build-isolation -r requirements.txt
echo

# Stage 2: setup-script credentials so subsequent stages have something
# to bind to (these are fake, used only for the install/config writes).
export DATABRICKS_HOST="https://fake.databricks.com"
export DATABRICKS_TOKEN="dapifake0000000000000000000000000000"
export ANTHROPIC_MODEL="databricks-claude-opus-4-7"
export GEMINI_MODEL="databricks-gemini-2-5-pro"
export CODEX_MODEL="databricks-gpt-5-5"
export HERMES_MODEL="databricks-claude-opus-4-6"
export HERMES_FALLBACK_MODEL="databricks-claude-opus-4-6"
export ENABLE_HERMES="true"

# Stage 3a (BEFORE the install scripts): enterprise_config.bootstrap() runs
# the same URL validation app.py does at startup — including refusing to
# proceed if GITHUB_API_BASE / GITHUB_RELEASE_MIRROR / CLAUDE_INSTALLER_URL
# / HERMES_PIP_URL contain shell metacharacters. Must run BEFORE install_*.sh
# because those scripts interpolate GITHUB_API_BASE / GITHUB_RELEASE_MIRROR
# into curl/eval contexts.
echo ">>> Stage 3a: enterprise_config.bootstrap() (validates env first)"
# Don't kill the pipeline if bootstrap raises (e.g. UnsafeUrlError under the
# malicious-mirror test) — we want the error message to surface in stdout
# for the test driver to assert on.
python3 -c "import enterprise_config; enterprise_config.bootstrap()" || \
    echo "(bootstrap raised — see above)"
echo

# Stage 3b: install_*.sh — three GitHub-release downloaders. All wrapped
# with || true so a single failure (e.g. invalid GITHUB_API_BASE) doesn't
# kill the rest of the pipeline; verify.sh checks the resulting state.
echo ">>> Stage 3b: install_micro.sh"
bash install_micro.sh && mv micro $HOME/.local/bin/ 2>/dev/null || echo "(install_micro.sh failed)"
echo ">>> Stage 3b: install_gh.sh"
bash install_gh.sh || echo "(install_gh.sh failed)"
echo ">>> Stage 3b: install_databricks_cli.sh"
bash install_databricks_cli.sh || echo "(install_databricks_cli.sh failed)"
echo

echo ">>> Stage 3b: setup_claude.py"
uv run python setup_claude.py || echo "(setup_claude.py exited non-zero — checking what landed anyway)"

echo ">>> Stage 3c: setup_codex.py"
uv run python setup_codex.py || echo "(setup_codex.py exited non-zero)"

echo ">>> Stage 3d: setup_gemini.py"
uv run python setup_gemini.py || echo "(setup_gemini.py exited non-zero)"

echo ">>> Stage 3e: setup_opencode.py"
uv run python setup_opencode.py || echo "(setup_opencode.py exited non-zero)"

echo ">>> Stage 3f: setup_hermes.py"
uv run python setup_hermes.py || echo "(setup_hermes.py exited non-zero)"

echo

# Stage 4: simulate the terminal-env strip that create_session() applies.
# We can't exec verify.sh under the real PTY env (no Flask app running),
# so we approximate by running verify.sh under the env that
# _build_terminal_shell_env() would produce. This exercises F-01 exactly
# the way a user terminal would experience it.
echo ">>> Stage 4: verify.sh (under simulated terminal env)"
cd /work
python3 - <<'PYEOF'
import os, subprocess, sys
sys.path.insert(0, '/work')
from app import _build_terminal_shell_env
env = _build_terminal_shell_env(os.environ)
sys.exit(subprocess.call(
    ["bash", "/work/tests/integration/verify.sh"],
    env=env,
))
PYEOF
