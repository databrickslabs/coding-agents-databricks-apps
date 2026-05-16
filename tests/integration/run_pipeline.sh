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

# Stage 1: synced uv venv (mirrors what `databricks apps deploy` does at build)
echo ">>> Stage 1: uv sync (pyproject.toml)"
uv venv .venv
uv pip install -r requirements.txt
. .venv/bin/activate
echo

# Stage 2: install_*.sh — three GitHub-release downloaders
echo ">>> Stage 2: install_micro.sh"
bash install_micro.sh && mv micro $HOME/.local/bin/ 2>/dev/null || true
echo ">>> Stage 2: install_gh.sh"
bash install_gh.sh
echo ">>> Stage 2: install_databricks_cli.sh"
bash install_databricks_cli.sh
echo

# Stage 3: setup_*.py — agent CLI installs.
# Use fake creds — Codex/Gemini/OpenCode/Hermes install regardless of
# token (it's only the config-write step that needs auth).
export DATABRICKS_HOST="https://fake.databricks.com"
export DATABRICKS_TOKEN="dapifake0000000000000000000000000000"
export ANTHROPIC_MODEL="databricks-claude-opus-4-7"
export GEMINI_MODEL="databricks-gemini-2-5-pro"
export CODEX_MODEL="databricks-gpt-5-5"
export HERMES_MODEL="databricks-claude-opus-4-6"
export HERMES_FALLBACK_MODEL="databricks-claude-opus-4-6"
export ENABLE_HERMES="true"

# enterprise_config.bootstrap() runs from app.py at startup; in tests we
# invoke it directly so its side effects (npmrc write, env var push,
# URL validation) happen before any setup script tries to install.
echo ">>> Stage 3a: enterprise_config.bootstrap()"
# Don't kill the pipeline if bootstrap raises (e.g. UnsafeUrlError under the
# malicious-mirror test) — we want the error message to surface in stdout
# for the test driver to assert on.
python3 -c "import enterprise_config; enterprise_config.bootstrap()" || \
    echo "(bootstrap raised — see above)"

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
