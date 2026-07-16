"""Shared writer for the Databricks bearer-token helper script.

Both Claude Code (via ``apiKeyHelper`` in settings.json) and Pi (via a
``!command`` apiKey in models.json) resolve their model-auth token by running
this helper *per request / per TTL*. Centralising the writer keeps a single
source of truth for the token-resolution logic and its interpreter wiring, so
the two agents can never drift apart.

The helper prints exactly one line to stdout: the bearer token. Resolution
order (see the emitted script's docstring for the full rationale):
  1. SP OAuth token minted from the ``omnigents-host`` M2M profile (host path).
  2. The PAT from ``$DATABRICKS_TOKEN`` or ``~/.databrickscfg`` [DEFAULT].
Both are resolved fresh on each invocation, so a long-running agent survives
PAT rotation / SP-OAuth expiry without a restart.
"""

import configparser
import os
import sys
from pathlib import Path

# The SP OAuth profile the Omnigent host writes (auth_type=oauth-m2m). Kept in
# sync with omnigents_host._HOST_PROFILE and setup_claude._SP_PROFILE.
SP_PROFILE = "omnigents-host"


def resolve_databricks_token() -> str | None:
    """Resolve a fresh SP OAuth token, falling back to the current PAT."""
    try:
        from databricks.sdk.core import Config
        headers = Config(profile=SP_PROFILE).authenticate()
        auth = (headers or {}).get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else auth.strip()
        if token:
            return token
    except Exception:
        pass

    token = os.environ.get("DATABRICKS_TOKEN", "").strip()
    if token:
        return token
    try:
        config = configparser.ConfigParser()
        config.read(os.path.expanduser("~/.databrickscfg"))
        return config.get("DEFAULT", "token", fallback="").strip() or None
    except Exception:
        return None

_HELPER_SRC = '''#!/usr/bin/env python3
"""Print a Databricks bearer token for Claude Code / Pi token resolution.

stdout MUST be the token only -- both callers use it verbatim.
"""
import configparser
import os
import shutil
import subprocess
import sys

SP_PROFILE = "omnigents-host"
# Set on the uv re-run so the child (which has the SDK) doesn't recurse.
_REEXEC_GUARD = "OMNIGENTS_APIKEY_HELPER_REEXEC"


def _sp_oauth_token():
    # Mint the SP token via the SDK. The M2M (client_id/secret) profile the
    # Omnigent host writes is NOT usable via `databricks auth token` (that CLI
    # verb is U2M-only and refuses M2M); Config.authenticate() does the
    # client-credentials flow. Verified accepted by /anthropic (C-O1, HTTP 200).
    # Absent profile / non-host instance -> returns None, caller falls back.
    try:
        from databricks.sdk.core import Config
    except ImportError:
        # The helper is normally registered to run under the app's venv
        # interpreter (which has databricks-sdk), so this branch shouldn't
        # trigger. If it is ever invoked with a bare python3 that lacks the
        # SDK, re-exec once under an interpreter that has it: prefer the
        # recorded venv python (CODA_VENV_PYTHON), else fall back to
        # `uv run --with databricks-sdk python`.
        if os.environ.get(_REEXEC_GUARD) == "1":
            return None
        env = dict(os.environ, **{_REEXEC_GUARD: "1"})
        try:
            venv_python = os.environ.get("CODA_VENV_PYTHON")
            if venv_python and os.path.exists(venv_python):
                cmd = [venv_python, os.path.abspath(__file__)]
            else:
                uv = shutil.which("uv")
                if not uv:
                    return None
                cmd = [uv, "run", "--with", "databricks-sdk", "python",
                       os.path.abspath(__file__)]
            out = subprocess.run(
                cmd, env=env, capture_output=True, text=True, check=False,
            )
        except Exception:
            return None
        return (out.stdout or "").strip() or None
    except Exception:
        return None
    try:
        headers = Config(profile=SP_PROFILE).authenticate()
    except Exception:
        return None
    auth = (headers or {}).get("Authorization", "")
    # Strip the "Bearer " prefix so stdout carries the raw token only.
    return auth[7:].strip() if auth.startswith("Bearer ") else (auth.strip() or None)


def _pat_token():
    tok = os.environ.get("DATABRICKS_TOKEN", "").strip()
    if tok:
        return tok
    cfg_path = os.path.expanduser("~/.databrickscfg")
    try:
        cp = configparser.ConfigParser()
        cp.read(cfg_path)
        return (cp["DEFAULT"].get("token") or "").strip() or None
    except Exception:
        return None


def main():
    token = _sp_oauth_token() or _pat_token()
    if not token:
        print("no token source (no SP OAuth profile, no PAT)", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(token)


if __name__ == "__main__":
    main()
'''


def write_token_helper(target_dir) -> Path:
    """Write the executable token helper into ``target_dir``; return its path.

    Idempotent: overwrites any prior copy so a version bump propagates. The
    helper emits only the token on stdout (all diagnostics go to stderr).
    """
    target_dir = Path(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    helper_path = target_dir / "anthropic-token-helper.py"
    helper_path.write_text(_HELPER_SRC)
    helper_path.chmod(0o700)
    return helper_path


def helper_command(helper_path) -> str:
    """The `!command` string Pi uses to resolve its apiKey via the helper.

    Runs the helper under the app's venv interpreter (has databricks-sdk) so it
    never has to re-exec; falls back to the current interpreter. Pi resolves the
    `!`-prefixed value as a shell command fresh per request.
    """
    py = os.environ.get("CODA_VENV_PYTHON") or sys.executable or "python3"
    return f"!{py} {helper_path}"
