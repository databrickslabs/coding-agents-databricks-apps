# Observability, Policy & Cost (Workshop Part 3)

This doc is the source of truth for **what is actually traced/logged** in CoDA,
how to prove it works, and one governance finding that needs a decision.

It backs Round 3 of the PPCS challenge ("Scale, Sustain, Measure"), where every
scored submission needs an "MLflow/OTel trace link or trace id" and cost/usage
evidence.

---

## 1. MLflow tracing coverage — the honest matrix

`MLFLOW_TRACING_ENABLED=true` in `app.yaml` is the master switch. It routes
traces to the experiment `/Users/{APP_OWNER}/{DATABRICKS_APP_NAME}`.

**Verified against the setup scripts, not the marketing copy:**

| Agent          | MLflow trace? | Mechanism                                        |
|----------------|:-------------:|--------------------------------------------------|
| Claude Code    | ✅ yes        | Stop hook — `mlflow.claude_code.hooks` (`setup_mlflow.py`) |
| Claude Code    | ✅ (also)     | Native OTEL → UC tables (`CLAUDE_CODE_OTEL_ENABLED=true`) |
| Codex          | ✅ yes        | `@mlflow/codex` notify hook (`setup_codex.py`)   |
| Gemini         | ❌ no         | No first-party MLflow hook in `setup_gemini.py`  |
| Hermes         | ❌ no         | No MLflow wiring in `setup_hermes.py`            |
| Pi             | ❌ no         | No MLflow wiring in `setup_pi.py`                |
| OpenCode       | ❌ no         | No MLflow wiring in `setup_opencode.py`          |
| Omnigent host  | ❌ no         | No MLflow wiring in `omnigents_host.py`          |

**Why the gap is acceptable for now:** the two agents used as governed
dispatchers in the challenge (Claude Code, Codex) are the traced ones. For the
others, endpoint-level usage/cost is still captured by **AI Gateway usage
tracking** on the model serving endpoints (see §3), so no agent is completely
invisible on cost — only on per-turn trajectory traces.

**If you need full per-agent traces for Gemini/Hermes/Pi/OpenCode:** there is no
drop-in hook. It requires either (a) an OTLP exporter each CLI can be pointed at
that forwards to MLflow, or (b) a wrapper that starts an `mlflow` span around the
CLI invocation. Note: OpenCode already routes every request through a local
content-filter proxy (`localhost:4000`), which is the one clean interception
point among the four — instrument the proxy and OpenCode calls become traceable
with no per-CLI hook. Pi/Hermes/Gemini go straight to the gateway with no such
choke point. Tracked as a follow-up — do not claim these are traced until the
round-trip in §2 passes for that agent.

### ⚠️ Audit finding: configured ≠ flowing (both channels were EMPTY)

At the time of this audit, the tracing plumbing was wired but **no telemetry had
actually landed** — verified by querying Databricks directly:

| Sink | Configured? | Rows / traces found |
|------|:-----------:|---------------------|
| `edp_aisandbox_aisandbox_dev.ppcs.claude_otel_metrics` (Claude OTEL) | ✅ real OTEL schema | **0 rows** |
| MLflow experiment `122546556359397` (agent traces) | ✅ flag on | **no agent traces** (only a stray `RandomForestRegressor` ML run) |
| `edp_aisandbox_aisandbox_dev.ppcs.all_anthropic-opus-4-8_payload` (gateway payload) | ✅ enabled | **0 rows** (armed, not yet capturing) |

Takeaway: flipping `MLFLOW_TRACING_ENABLED=true` and enabling OTEL is necessary
but **not sufficient** — until an agent session actually runs through the
deployed app, the sinks stay empty. The §2 round-trip below is what turns
"configured" into "proven."

---

## 2. Prove tracing works — two levels

### 2a. Standalone round-trip (no live agent needed) — PROVEN ✅

`scripts/prove_trace_lands.py` emits one `@mlflow.trace` span shaped like an
agent turn, then **reads it back from Databricks** (REST v3) and prints the trace
id + UI URL. This proves persistence, not just a local write.

```bash
/app/python/source_code/.venv/bin/python scripts/prove_trace_lands.py
```

Verified output (run 2026-07-10):

```
mlflow 3.14.0  tracking_uri=databricks  experiment_id=122546556359397
emitted trace, handler returned: 'handled: part3-proof-...'
✓ PROVEN: trace persisted in Databricks
  trace_id:  tr-5e6de694a2c0e54d1dd88c32aad3c347
  name:      coda_part3_proof_turn
  state:     OK
  user:      david.okeeffe1@coles.com.au
```

> NB: use the app venv python (`/app/python/source_code/.venv/bin/python`, mlflow
> 3.14.0) — it's the same interpreter the Claude Stop hook uses. The system
> `python3` has no mlflow. The SDK's `search_traces` is slow from the container
> (tens of seconds); the script therefore reads back via the REST v3 endpoint,
> which returns instantly.

### 2b. Live agent hook demo (Part 3 live moment) — see the runbook

`docs/part3-tracing-runbook.md` walks through running a real Claude Code session
so its **Stop hook** fires and a genuine agent trace appears in the experiment —
the "hook demonstration" for the Part 3 session. Same experiment, but now
populated by the agent itself rather than a proof script.

---

## 3. AI Gateway logging — trace ALL requests, but not the payloads

Goal (per team guidance): **usage tracking on for every endpoint the agents can
hit, so no request is invisible** — while NOT hoarding sensitive payloads.

Gateway logging has two independent switches, and they matter very differently:

- `usage_tracking_config` → tokens, cost, latency, model, caller. **No content.**
  This is the "trace all requests so you know" switch. **ON everywhere.**
- `inference_table_config` → full request+response **payloads** (the actual
  prompts/responses, incl. promo/customer/pricing data). Envelope-sensitive.
  **OFF everywhere.**

### Full audit (all chat endpoints agents can route to, 2026-07-10)

| Endpoint | usage tracking | payload logging |
|----------|:--------------:|:---------------:|
| databricks-claude-opus-4-8 | ✅ on | ⚠️ **ON → disable** |
| databricks-claude-opus-4-7 | ✅ on | off |
| databricks-claude-opus-4-6 | ✅ on | ⚠️ **ON → disable** |
| databricks-claude-sonnet-4-6 | ✅ on | off |
| databricks-claude-sonnet-4-5 | ✅ on | off |
| databricks-claude-haiku-4-5 | ✅ on | off |
| databricks-gpt-oss-120b | ✅ on | off |
| databricks-gpt-oss-20b | ✅ on | off |
| databricks-gemma-3-12b | ✅ on | off |
| claude-opus-4-7 (external) | ✅ on | ⚠️ **ON → disable** |

**Good news:** usage tracking is **already on across all 10 endpoints** — so
every agent request (Claude Code, Codex, Gemini, Hermes, Pi, OpenCode, Omnigent)
is already logged at the gateway for cost/usage, regardless of whether that agent
has a per-turn MLflow/OTEL hook. This is the universal safety net that covers the
5 un-hooked agents from §1.

> Note: `databricks-gpt-5-3-codex` and `databricks-gemini-2-5-pro` (the Codex /
> Gemini model names in app.yaml) are **not served in this workspace**. In-geo
> discovery remaps those agents to a served Claude endpoint, so their traffic is
> already covered by the endpoints above.

**Envelope catch:** 3 endpoints (opus-4-8, opus-4-6, external claude-opus-4-7)
also have payload logging on — writing full prompts/responses to inference
tables (e.g. `edp_aisandbox_aisandbox_dev.ppcs.all_anthropic-opus-4-8_payload`).
The `00-participant-guide.md` envelope forbids sensitive payloads in telemetry.
At audit time these payload tables were still **0 rows** (armed, not yet
capturing) — so this is a fix-before-it-fills, not a cleanup.

### Enforce the policy

```bash
scripts/gateway_logging.sh            # AUDIT — prints the matrix above, changes nothing
scripts/gateway_logging.sh --apply    # ensure usage-tracking ON + payload logging OFF on all
```

The script is idempotent: it only PUTs to endpoints that aren't already
compliant, and re-verifies each after applying.

If the team decides payloads MUST be captured for eval, the compliant path is:
redact before logging (OTEL collector redaction — see PPCS-027), or point the
inference table at a restricted schema only the reviewer identity can read, and
document the approval in the evidence pack.
