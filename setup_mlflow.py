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

# Merge MLflow env vars (always written so flipping the flag at runtime works
# without rerunning setup — Claude reads MLFLOW_CLAUDE_TRACING_ENABLED on launch).
settings.setdefault("env", {})
settings["env"]["MLFLOW_CLAUDE_TRACING_ENABLED"] = "true" if tracing_enabled else "false"
settings["env"]["MLFLOW_TRACKING_URI"] = "databricks"
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
            "command": f"{python_cmd} -c \"from mlflow.claude_code.hooks import stop_hook_handler; stop_hook_handler()\""
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

settings_path.write_text(json.dumps(settings, indent=2))

# Install the MLflow Claude plugin (the real tracing path on mlflow 3.14+).
# Resolve the experiment id from the name so the plugin logs to the right place.
# Best-effort: never fail app startup if the plugin/CLI isn't available.
if tracing_enabled:
    import subprocess
    try:
        cmd = [
            python_cmd, "-m", "mlflow", "autolog", "claude",
            "-u", "databricks",
            "-n", experiment_name,
            "-d", os.getcwd(),
            "-y",
        ]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            print("MLflow Claude plugin installed (mlflow autolog claude)")
        else:
            print(f"MLflow Claude plugin setup returned {r.returncode}: "
                  f"{(r.stderr or r.stdout).strip()[:300]}")
    except Exception as e:  # noqa: BLE001 — never block startup on tracing setup
        print(f"MLflow Claude plugin setup skipped ({type(e).__name__}: {e}). "
              f"Run manually: {python_cmd} -m mlflow autolog claude -u databricks "
              f"-n '{experiment_name}' -y")
print(f"MLflow tracing {'ENABLED' if tracing_enabled else 'disabled'}: experiment={experiment_name}")
print(f"  Tracking URI: databricks")
print(f"  Settings updated: {settings_path}")
if not tracing_enabled:
    print("  Set MLFLOW_TRACING_ENABLED=true (in app.yaml) to enable Claude + Codex tracing.")
