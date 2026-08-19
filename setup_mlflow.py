"""Wire up Claude Code's Stop hook for MLflow tracing.

Gated on MLFLOW_TRACING_ENABLED. The SAME switch also enables Codex tracing in
setup_codex.py. (Gemini/Hermes/Pi/OpenCode have no first-party MLflow hook — see
docs/observability.md §1; do not claim they are traced.) Traces land in
/Users/{app_owner}/{app_name}.
"""

import os
import sys
import json
from pathlib import Path

from cli_auth import _atomic_write_text


def _mint_app_sp_token():
    """Mint an app-SP OAuth (client-credentials/M2M) token for the OSS app URL.

    Databricks Apps accept ONLY an SP M2M token on inbound HTTP (PATs/user bearers
    get 302 → OIDC login — verified 2026-07-11). Same mechanism as the Omnigent
    host tunnel and token_helper._sp_oauth_token: Config.authenticate() runs the
    client-credentials flow (the `databricks auth token` CLI verb is U2M-only and
    refuses M2M). Sources, in order:
      1. The `omnigents-host` M2M profile (written when the host integration is on).
      2. Injected DATABRICKS_CLIENT_ID/SECRET (before CoDA strips them).
    Returns None if neither is available; caller warns and skips the token.
    """
    try:
        from databricks.sdk.core import Config
    except ImportError:
        return None
    # 1. The SP OAuth profile (kept in sync with token_helper.SP_PROFILE).
    for kwargs in ({"profile": "omnigents-host"},):
        try:
            headers = Config(**kwargs).authenticate()
            auth = (headers or {}).get("Authorization", "")
            if auth.startswith("Bearer "):
                return auth[7:].strip()
        except Exception:  # noqa: BLE001 — try the next source
            pass
    # 2. Injected app-SP client creds (if still present in env at setup time).
    cid = os.environ.get("DATABRICKS_CLIENT_ID", "").strip()
    csec = os.environ.get("DATABRICKS_CLIENT_SECRET", "").strip()
    host = os.environ.get("DATABRICKS_HOST", "").strip()
    if cid and csec and host:
        try:
            headers = Config(
                host=host, client_id=cid, client_secret=csec, auth_type="oauth-m2m",
            ).authenticate()
            auth = (headers or {}).get("Authorization", "")
            if auth.startswith("Bearer "):
                return auth[7:].strip()
        except Exception:  # noqa: BLE001
            pass
    return None


# Set HOME if not properly set
if not os.environ.get("HOME") or os.environ["HOME"] == "/":
    os.environ["HOME"] = "/app/python/source_code"

home = Path(os.environ["HOME"])
settings_path = home / ".claude" / "settings.json"

# Read existing settings (written by setup_claude.py)
if settings_path.exists():
    settings = json.loads(settings_path.read_text())
else:
    settings = {}

app_owner = os.environ.get("APP_OWNER", "")
app_name = os.environ.get("DATABRICKS_APP_NAME", "coding-agents")

if not app_owner:
    print("MLflow tracing skipped: APP_OWNER not set")
    raise SystemExit(0)

experiment_name = f"/Users/{app_owner}/{app_name}"

# Single switch that controls tracing for Claude, Codex, and Gemini.
# Defaults to "false" so opt-in requires explicit configuration.
tracing_enabled = os.environ.get("MLFLOW_TRACING_ENABLED", "false").lower() == "true"

# ─── spec-B: restricted-network OSS redirect ────────────────────────────────
# When MLFLOW_OSS_TRACKING_ENABLED=true and MLFLOW_OSS_URL is set, point Claude
# Code's tracing at the self-hosted MLflow OSS app (spec-A) instead of
# `databricks`. Needed where the prod workspace blocks Zerobus/managed storage
# (see specs/mlflow-lakebase-tracing/GOAL.md §1). Off by default → the direct
# `databricks` path below is unchanged.
oss_enabled = os.environ.get("MLFLOW_OSS_TRACKING_ENABLED", "false").lower() == "true"
oss_url = os.environ.get("MLFLOW_OSS_URL", "").strip().rstrip("/")
tracking_uri = "databricks"
# Ensure the env dict exists BEFORE the OSS block writes into it (line-order bug
# fix: this setdefault used to be below, so writing MLFLOW_TRACKING_TOKEN here
# raised KeyError('env') and crashed setup_mlflow → no tracing wired at all).
settings.setdefault("env", {})
if oss_enabled and oss_url:
    tracking_uri = oss_url
    # Databricks Apps reject PATs (302 → OIDC login) and accept only an OAuth
    # token minted from the app SP's client creds (client-credentials / M2M).
    # Verified live 2026-07-11: a user bearer to a deployed app URL gets 302/hang;
    # the SP M2M token is the accepted path (same as the Omnigent host tunnel and
    # token_helper._sp_oauth_token). The CoDA app SP must have CAN_USE on the OSS
    # app — see grant_mlflow_host.sh. The MLflow HTTP client reads this token from
    # MLFLOW_TRACKING_TOKEN.
    oss_token = _mint_app_sp_token()  # defined below; None if creds absent
    if oss_token:
        settings["env"]["MLFLOW_TRACKING_TOKEN"] = oss_token
    else:
        print("WARNING: MLFLOW_OSS_URL set but could not mint app-SP token; "
              "OSS tracing will 302 without it. Check DATABRICKS_CLIENT_ID/SECRET.")

# Merge MLflow env vars (always written so flipping the flag at runtime works
# without rerunning setup — Claude reads MLFLOW_CLAUDE_TRACING_ENABLED on launch).
settings.setdefault("env", {})
settings["env"]["MLFLOW_CLAUDE_TRACING_ENABLED"] = "true" if tracing_enabled else "false"
settings["env"]["MLFLOW_TRACKING_URI"] = tracking_uri
settings["env"]["MLFLOW_EXPERIMENT_NAME"] = experiment_name
# Override container-level OTEL endpoint so MLflow uses its native MlflowV3SpanExporter
# instead of sending traces to a non-existent localhost:4314 OTLP collector
settings["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] = ""

# Add Stop hook (processes full transcript at session end).
#
# ⚠️ MLflow 3.14 migration: the Python `stop_hook_handler()` was DEPRECATED to a
# no-op — it prints "MLflow Claude tracing has moved to the marketplace plugin
# runtime" and writes NO trace. Tracing is now driven by the MLflow Claude
# *plugin*, installed via `mlflow autolog claude`. We therefore (a) keep writing
# the legacy Stop hook for older mlflow builds (harmless no-op on 3.14+), AND
# (b) when tracing is enabled, install the plugin so 3.14+ actually traces.
# Verified 2026-07-10: after `mlflow autolog claude`, a live `claude` session's
# trace lands in the experiment; the bare Stop hook alone does not.
python_cmd = os.environ.get("CODA_VENV_PYTHON") or sys.executable or "python3"
mlflow_hook = {
    "hooks": [
        {
            "type": "command",
            "command": f"{python_cmd} -c \"from mlflow.claude_code.hooks import stop_hook_handler; stop_hook_handler()\"",
            # Bound the hook. On the pinned mlflow (3.14) the handler is a
            # no-op that returns immediately, but this entry is deliberately
            # kept for older builds where it processes the entire transcript
            # synchronously and blocks everything else on the Stop chain.
            # One dropped trace beats a wedged session.
            "timeout": 10,
        }
    ]
}

existing_hooks = settings.get("hooks", {})
stop_hooks = existing_hooks.get("Stop", [])
# Avoid duplicating the hook if setup runs multiple times
already_present = any(
    "stop_hook_handler" in h.get("hooks", [{}])[0].get("command", "")
    for h in stop_hooks if isinstance(h, dict)
)
if not already_present:
    stop_hooks.append(mlflow_hook)
existing_hooks["Stop"] = stop_hooks
settings["hooks"] = existing_hooks

settings_path.parent.mkdir(parents=True, exist_ok=True)
_atomic_write_text(str(settings_path), json.dumps(settings, indent=2))

# Install the MLflow Claude plugin (the real tracing path on mlflow 3.14+).
# Resolve the experiment id from the name so the plugin logs to the right place.
# Best-effort: never fail app startup if the plugin/CLI isn't available.
if tracing_enabled:
    import subprocess
    try:
        # Point the plugin at the OSS URL when redirecting (spec-B), else databricks.
        # The plugin accepts a non-`databricks` -u (verified: file://, sqlite://,
        # http(s):// follow the same pattern — spec-A §0).
        cmd = [
            python_cmd, "-m", "mlflow", "autolog", "claude",
            "-u", tracking_uri,
            "-n", experiment_name,
            "-d", os.getcwd(),
            "-y",
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            print(f"MLflow Claude plugin installed (mlflow autolog claude -u {tracking_uri})")
        else:
            print(f"MLflow Claude plugin setup returned {r.returncode}: "
                  f"{(r.stderr or r.stdout).strip()[:300]}")
    except Exception as e:  # noqa: BLE001 — never block startup on tracing setup
        print(f"MLflow Claude plugin setup skipped ({type(e).__name__}: {e}). "
              f"Run manually: {python_cmd} -m mlflow autolog claude -u {tracking_uri} "
              f"-n '{experiment_name}' -y")
print(f"MLflow tracing {'ENABLED' if tracing_enabled else 'disabled'}: experiment={experiment_name}")
print(f"  Tracking URI: {tracking_uri}")
print(f"  Settings updated: {settings_path}")
if not tracing_enabled:
    print("  Set MLFLOW_TRACING_ENABLED=true (in app.yaml) to enable Claude + Codex tracing.")
