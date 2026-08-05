#!/usr/bin/env python
"""Part 3 proof: emit one MLflow trace and read it back from Databricks.

WHY THIS EXISTS
---------------
CoDA's MLflow-tracing plumbing (Claude Code Stop hook, Codex notify hook) and
the Claude Code OTEL export are *configured* in app.yaml, but as of the Part 3
observability audit the target sinks were EMPTY — no agent session had actually
exercised them, so nothing proved traces reach Databricks. This script closes
that gap with a hard, self-contained round-trip that does NOT need a live agent:

  1. Point MLflow at Databricks + the CoDA experiment.
  2. Emit ONE real trace (@mlflow.trace) with a nested tool-call span, shaped
     like an agent turn (model + tool attributes).
  3. Read it back from the tracking server (REST v3) and print the trace id,
     name, state, and the MLflow UI URL — proving PERSISTENCE, not a local write.

This is the "prove the traces at least end up in Databricks" deliverable for the
workshop's Part 3 (observability / policy / cost).

RUN IT (must use the app venv python that has mlflow):
    /app/python/source_code/.venv/bin/python scripts/prove_trace_lands.py

Optional env overrides:
    MLFLOW_EXPERIMENT_ID   (required; use a deployment-specific experiment ID)
    DATABRICKS_HOST        (used only to build the clickable UI URL)

Exit code 0 = trace emitted AND read back. Non-zero = something didn't land.
"""
import json
import os
import subprocess
import sys
import time

EXP_ID = os.environ.get("MLFLOW_EXPERIMENT_ID", "")


def _fail(msg: str) -> "NoReturn":  # noqa: F821
    print(f"\n✗ FAIL: {msg}")
    sys.exit(1)


def main() -> None:
    try:
        import mlflow
    except ImportError:
        _fail(
            "mlflow not importable. Run with the app venv python:\n"
            "    /app/python/source_code/.venv/bin/python scripts/prove_trace_lands.py"
        )

    mlflow.set_tracking_uri("databricks")
    mlflow.set_experiment(experiment_id=EXP_ID)
    print(f"mlflow {mlflow.__version__}  tracking_uri={mlflow.get_tracking_uri()}  experiment_id={EXP_ID}")

    # --- 1. Emit one trace shaped like an agent turn -------------------------
    @mlflow.trace(
        name="coda_part3_proof_turn",
        attributes={"agent": "proof", "model": "databricks-claude-opus-4-8"},
    )
    def agent_turn(prompt: str) -> str:
        with mlflow.start_span(name="tool_call", attributes={"tool": "read_file"}) as s:
            s.set_inputs({"path": "README.md"})
            s.set_outputs({"lines": 42})
        return f"handled: {prompt}"

    marker = f"part3-proof-{int(time.time())}"
    result = agent_turn(marker)
    print(f"emitted trace, handler returned: {result!r}")

    # --- 2. Read it back from Databricks (REST v3; the SDK search is slow) ----
    # Bounded retry: the trace search endpoint can lag a few seconds behind write.
    body = json.dumps(
        {
            "locations": [{"mlflow_experiment": {"experiment_id": EXP_ID}}],
            "max_results": 5,
        }
    )
    found = None
    for attempt in range(1, 6):
        time.sleep(2)
        proc = subprocess.run(
            ["databricks", "api", "post", "/api/3.0/mlflow/traces/search", "--json", body],
            capture_output=True,
            text=True,
            timeout=45,
        )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            continue
        for t in data.get("traces", []):
            if t.get("tags", {}).get("mlflow.traceName") == "coda_part3_proof_turn" and marker in (
                t.get("request", "") + t.get("response", "")
            ):
                found = t
                break
        if found:
            break
        print(f"  read-back attempt {attempt}: not visible yet, retrying…")

    if not found:
        _fail(
            "trace was emitted but did NOT appear in Databricks within ~12s. "
            "The write path connected (no exception), so check experiment perms / "
            "tracking-server lag, then re-run."
        )

    # --- 3. Report ------------------------------------------------------------
    tid = found.get("client_request_id") or found.get("trace_id", "?")
    host = os.environ.get("DATABRICKS_HOST", "").rstrip("/")
    ui = f"{host}/ml/experiments/{EXP_ID}/traces?selectedTraceId={tid}" if host else "(set DATABRICKS_HOST for a clickable URL)"
    print("\n✓ PROVEN: trace persisted in Databricks")
    print(f"  trace_id:  {tid}")
    print(f"  name:      {found.get('tags', {}).get('mlflow.traceName')}")
    print(f"  state:     {found.get('state')}")
    print(f"  duration:  {found.get('execution_duration')}")
    print(f"  user:      {found.get('tags', {}).get('mlflow.user', '?')}")
    print(f"  UI:        {ui}")
    sys.exit(0)


if __name__ == "__main__":
    main()
