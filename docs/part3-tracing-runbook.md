# Part 3 Runbook — prove MLflow traces & OTEL logs land in Databricks

This is the live-demo script for the workshop's Part 3 (observability / policy /
cost). It has two parts: a **standalone proof** (always works, no agent needed)
and a **live hook demo** (a real Claude Code session firing its Stop hook). Both
land in the same MLflow experiment.

Target experiment: `122546556359397`
  → `/Users/david.okeeffe1@coles.com.au/coding-agents-david-okeeffe1`
App venv python (has mlflow 3.14.0): `/app/python/source_code/.venv/bin/python`

---

## Part A — Standalone round-trip proof (30 seconds, no agent)

Use this to prove the tracking path works *before* relying on any agent hook.

```bash
/app/python/source_code/.venv/bin/python \
  projects/coding-agents-databricks-apps-private/scripts/prove_trace_lands.py
```

Expect `✓ PROVEN: trace persisted in Databricks` with a `tr-...` id, `state: OK`,
and your user. If you export `DATABRICKS_HOST` first, it also prints a clickable
MLflow UI URL. This confirms: MLflow → `databricks` tracking URI → experiment
`122546556359397` round-trips and is queryable. (Verified working 2026-07-10.)

---

## Part B — Live Claude Code Stop-hook demo (the "hook" moment)

This proves the *agent itself* produces a trace, via the Stop hook wired in
`setup_mlflow.py`. This is the part that was configured-but-never-exercised
(the experiment had zero agent traces before this audit).

### B0. Preconditions (verify once)

```bash
# 1. Tracing master switch is on (set in app.yaml):
grep MLFLOW_TRACING_ENABLED projects/coding-agents-databricks-apps-private/app.yaml
#   → MLFLOW_TRACING_ENABLED: "true"

# 2. Claude settings carry the MLflow env + Stop hook:
python3 - <<'PY'
import json, os
p = os.path.expanduser("~/.claude/settings.json")
s = json.load(open(p))
print("MLFLOW_CLAUDE_TRACING_ENABLED:", s.get("env", {}).get("MLFLOW_CLAUDE_TRACING_ENABLED"))
print("MLFLOW_EXPERIMENT_NAME:", s.get("env", {}).get("MLFLOW_EXPERIMENT_NAME"))
stop = s.get("hooks", {}).get("Stop", [])
print("Stop hook present:",
      any("stop_hook_handler" in h.get("hooks", [{}])[0].get("command", "") for h in stop))
PY
```

If `MLFLOW_CLAUDE_TRACING_ENABLED` is `false` or the hook is missing, the app
hasn't re-run setup since the flag flipped. Re-run setup (or redeploy) so
`setup_mlflow.py` writes the enabled env + hook, then continue.

> The hook is defined as:
> `<venv-python> -c "from mlflow.claude_code.hooks import stop_hook_handler; stop_hook_handler()"`
> and fires on **session end**. The handler reads Claude Code's transcript and
> posts it as an MLflow trace. It short-circuits harmlessly when tracing is off.

### B1. Run a tiny Claude Code session

```bash
claude -p "Read README.md and tell me in one sentence what this repo is."
```

Let it finish and exit (the Stop hook fires on session end, not mid-turn).

### B2. Confirm the agent trace landed

```bash
databricks api post /api/3.0/mlflow/traces/search --json \
  '{"locations":[{"mlflow_experiment":{"experiment_id":"122546556359397"}}],"max_results":5}' \
  | python3 -c 'import sys,json; [print(t.get("tags",{}).get("mlflow.traceName"), t.get("request_time")) for t in json.load(sys.stdin).get("traces",[])]'
```

You should now see a Claude-Code-generated trace (a session/transcript trace),
distinct from the `coda_part3_proof_turn` proof trace. Open it in
**Workspace ▸ Machine Learning ▸ Experiments ▸ coding-agents-david-okeeffe1 ▸
Traces**.

---

## Part C — OTEL logs (Claude Code metrics → Unity Catalog)

Separate channel from MLflow traces. Claude Code's native OpenTelemetry emitter
(`CLAUDE_CODE_OTEL_ENABLED=true`, `claude_otel.py`) writes metrics to UC:

```
edp_aisandbox_aisandbox_dev.ppcs.claude_otel_metrics
```

This table was **0 rows** at audit time (configured, never exercised). After the
Part B session, check whether OTEL metrics have started flowing:

```bash
# needs a running SQL warehouse (e.g. a7e45ceca5efa579)
databricks api post /api/2.0/sql/statements --json \
  '{"warehouse_id":"a7e45ceca5efa579","statement":"SELECT count(*) FROM edp_aisandbox_aisandbox_dev.ppcs.claude_otel_metrics","wait_timeout":"50s"}'
```

If it's still 0 after a real session, the OTEL export isn't reaching the UC sink
(vs. MLflow traces which are proven working) — that's a real finding to report,
not a demo failure. OTEL metrics can also lag; re-check after a few minutes.

Note: only **Claude Code** emits OTEL. No other agent has an OTLP path
(`setup_gemini/hermes/pi/opencode` have none) — see `observability.md` §1.

---

## What to say in the Part 3 readout

1. **Traces reach Databricks — proven** (Part A, hard round-trip with trace id).
2. **The agent hook works** (Part B, Claude Stop hook → real session trace).
3. **Honest coverage**: Claude Code (MLflow + OTEL) and Codex (MLflow) are
   traced; Gemini/Hermes/Pi/OpenCode/Omnigent are not — but AI Gateway usage
   tracking still gives endpoint-level cost for all of them.
4. **Governance catch**: the opus-4-8 gateway payload table was armed to capture
   sensitive prompts/responses — disabled per the envelope, usage tracking kept
   (see `observability.md` §3 and `scripts/fix_opus48_gateway.sh`).
