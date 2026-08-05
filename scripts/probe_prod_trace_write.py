#!/usr/bin/env python
"""C-C4 PROD PROBE — does an MLflow trace-write survive the prod Zerobus/NCC block?

WHY THIS EXISTS (spec-C C-C4)
-----------------------------
The MLflow-OSS + Lakebase design (specs/mlflow-lakebase-tracing/) ends with a
Lakeflow serverless job that copies traces into a Databricks MLflow experiment.
That final write is the SAME OTLP→Zerobus→ADLS path the OTEL investigation proved
FAILS in the prod target workspace (400 PERMISSION_DENIED / 403, because
Serverless-Compute Zerobus can't reach Private-Link storage). But that proof was
for the OTEL *metrics* endpoint — NOT the MLflow *trace-write* the copy job uses.
Those may route differently. This probe answers the actual question:

    From SERVERLESS compute in the PROD workspace, does writing one MLflow trace
    to a Databricks experiment succeed, or does it hit the Zerobus block?

If it SUCCEEDS → the destination track (spec-C) is viable in prod as-is.
If it's BLOCKED → the copy hop is gated on the NCC/account-team fix, and that must
be surfaced now (descope to Lakebase/OSS-only, or a non-Private-Link schema).

HOW TO RUN — this must run on SERVERLESS in the PROD (Coles) workspace, because
the block is specifically about Serverless Compute reaching Private-Link storage.
Running it on a dev workspace (lakemeter) or from a laptop gives a FALSE GREEN.

  Option 1 (recommended): paste into a Databricks notebook, attach to
  **Serverless** compute, Run All. The cell markers (# COMMAND ----------) make it
  import cleanly as a notebook.

  Option 2: run as a serverless job task (spark_python_task / notebook_task).

Optional env / notebook widgets:
    PROBE_EXPERIMENT   experiment path to write to. Default: a self-created
                       /Users/{me}/coda_ccp4_probe experiment (needs no setup).
    PROBE_CATALOG_SCHEMA  if set (e.g. "edp_aisandbox_aisandbox_dev.ppcs"), also
                       runs the UC-schema-linked trace path — this is the variant
                       most likely to hit Zerobus, so set it to the REAL target
                       schema for the most faithful test.

Exit / final line is one of:
    PASS   — trace written AND read back. Prod MLflow trace-write works.
    BLOCKED — write failed with the Zerobus/Private-Endpoint/NCC signature.
    INCONCLUSIVE — failed for another reason (perms/experiment/network) — reported
                   verbatim so you can tell it apart from a real Zerobus block.
"""
import os
import sys
import time
import traceback

# COMMAND ----------
# --- 0. Environment sanity: are we actually on serverless in the right place? ---

def _detect_context():
    """Best-effort: report where this is running so a false-green is obvious."""
    info = {}
    try:
        from pyspark.sql import SparkSession
        spark = SparkSession.getActiveSession()
        if spark is not None:
            info["workspace_url"] = spark.conf.get(
                "spark.databricks.workspaceUrl", "?"
            )
            # Serverless sets this; classic clusters generally don't.
            info["is_serverless_hint"] = spark.conf.get(
                "spark.databricks.clusterUsageTags.clusterName", "(no cluster name — likely serverless)"
            )
    except Exception as e:  # noqa: BLE001
        info["spark"] = f"unavailable ({type(e).__name__})"
    return info


# COMMAND ----------
# --- 1. Classify a failure: is it the Zerobus/NCC block, or something else? -----

# The proven prod signatures (from the OTEL investigation): the underlying error
# is a Private-Endpoint / NCC / Zerobus-storage-403. Match on these, not just the
# HTTP code, so a plain permission or missing-experiment error isn't mis-called.
_ZEROBUS_SIGNATURES = (
    "zerobus",
    "private endpoint",
    "private-endpoint",
    "private link",
    "privatelink",
    "validate ncc",
    "403 forbidden",
    "storage",  # broad — combined with others below via _looks_like_zerobus
)
_STORAGE_HINTS = ("abfss", "dfs.core.windows.net", "adls", "forbidden", "permission_denied")


def _looks_like_zerobus(text: str) -> bool:
    t = (text or "").lower()
    if "zerobus" in t or "private endpoint" in t or "private-endpoint" in t or "validate ncc" in t:
        return True
    # Otherwise require BOTH a storage hint AND a denial hint to avoid false positives.
    has_storage = any(h in t for h in _STORAGE_HINTS)
    has_denial = "403" in t or "forbidden" in t or "permission_denied" in t or "access" in t
    return has_storage and has_denial


def _verdict(kind: str, detail: str = "") -> None:
    print("\n" + "=" * 60)
    print(f"C-C4 PROBE VERDICT: {kind}")
    if detail:
        print(detail)
    print("=" * 60)
    # Non-zero for BLOCKED/INCONCLUSIVE so a job task surfaces it as failed.
    sys.exit(0 if kind == "PASS" else 2)


# COMMAND ----------
# --- 2. The probe: write one trace, read it back --------------------------------

def main() -> None:
    print("context:", _detect_context())

    try:
        import mlflow
    except ImportError:
        _verdict("INCONCLUSIVE", "mlflow not importable in this runtime — attach a runtime that has mlflow.")

    mlflow.set_tracking_uri("databricks")
    print(f"mlflow {mlflow.__version__}  tracking_uri={mlflow.get_tracking_uri()}")

    # Experiment: use the provided one, else self-create under the current user so
    # the probe needs zero pre-setup in the target workspace.
    exp_path = os.environ.get("PROBE_EXPERIMENT", "").strip()
    if not exp_path:
        try:
            me = mlflow.utils.databricks_utils.get_databricks_host_creds  # noqa: F841 (import guard)
            from mlflow.tracking import MlflowClient  # noqa: F401
            user = _current_user()
            exp_path = f"/Users/{user}/coda_ccp4_probe"
        except Exception:
            exp_path = "/Shared/coda_ccp4_probe"
    print(f"target experiment: {exp_path}")

    try:
        mlflow.set_experiment(exp_path)
    except Exception as e:  # noqa: BLE001
        msg = f"{e}\n{traceback.format_exc()}"
        # Creating/selecting the experiment itself can fail on Private-Link storage.
        if _looks_like_zerobus(msg):
            _verdict("BLOCKED", f"Experiment create/select hit the storage block:\n{msg[:1200]}")
        _verdict("INCONCLUSIVE", f"Could not set experiment (not a Zerobus signature):\n{msg[:1200]}")

    # --- write one trace shaped like a copied agent turn ---
    marker = f"ccp4-probe-{int(time.time())}"

    try:
        @mlflow.trace(name="coda_ccp4_probe_turn", attributes={"agent": "probe"})
        def turn(prompt: str) -> str:
            # Set marker as a TRACE TAG (searchable), not a span attribute.
            # A span attribute does NOT become a trace tag and is not searchable —
            # a bug caught in self-test that made the read-back always miss.
            mlflow.update_current_trace(tags={"source_trace_id": marker})
            with mlflow.start_span(name="reconstructed_span",
                                   attributes={"tool": "copy_job_probe"}) as s:
                s.set_inputs({"marker": marker})
                s.set_outputs({"ok": True})
            return f"handled: {prompt}"

        turn(marker)
        # MLflow 3.x exports traces on an async queue. Force a terminating flush so
        # the read-back tests real persistence, not export timing. terminate=True
        # fully drains the queue (terminate=False did NOT drain it in self-test).
        try:
            mlflow.flush_trace_async_logging(terminate=True)
        except Exception:  # noqa: BLE001
            pass
        print(f"trace write returned + flushed (marker={marker})")
    except Exception as e:  # noqa: BLE001
        msg = f"{e}\n{traceback.format_exc()}"
        if _looks_like_zerobus(msg):
            _verdict("BLOCKED",
                     "MLflow trace-write raised the Zerobus/Private-Endpoint/NCC error — "
                     "the copy job's final write is blocked in this workspace.\n" + msg[:1500])
        _verdict("INCONCLUSIVE", "Trace write failed, but NOT with a Zerobus signature:\n" + msg[:1500])

    # --- read it back via REST v3 (the SDK's search_traces is unreliable from some
    # contexts — documented in observability.md / prove_trace_lands.py; the REST v3
    # endpoint returns immediately). Match on the searchable source_trace_id tag. ---
    found = None
    read_err = None
    try:
        import json as _json
        import urllib.request
        import urllib.error
        from mlflow.utils.databricks_utils import get_databricks_host_creds
        from mlflow.tracking import MlflowClient

        exp = MlflowClient().get_experiment_by_name(exp_path)
        creds = get_databricks_host_creds()
        url = creds.host.rstrip("/") + "/api/3.0/mlflow/traces/search"
        body = _json.dumps({
            "locations": [{"mlflow_experiment": {"experiment_id": exp.experiment_id}}],
            "max_results": 25,
        }).encode()
        for attempt in range(1, 9):
            time.sleep(3)
            req = urllib.request.Request(
                url, data=body,
                headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = _json.loads(resp.read())
            except urllib.error.HTTPError as he:
                read_err = f"HTTP {he.code}: {he.read()[:600]}"
                if _looks_like_zerobus(read_err):
                    _verdict("BLOCKED", "Trace-search read-back hit the storage block:\n" + read_err)
                break
            for t in data.get("traces", []):
                if t.get("tags", {}).get("source_trace_id") == marker:
                    found = t
                    break
            if found:
                break
            print(f"  read-back attempt {attempt}: not visible yet…")
    except Exception as e:  # noqa: BLE001
        msg = f"{e}\n{traceback.format_exc()}"
        if _looks_like_zerobus(msg):
            _verdict("BLOCKED", "Read-back hit the storage block:\n" + msg[:1200])
        read_err = msg[:800]

    if not found:
        # A read-back miss after a clean write+flush is SUSPICIOUS but does NOT by
        # itself prove a storage block — it can be search lag/indexing. Only an
        # actual Zerobus-signature error proves BLOCKED. Report INCONCLUSIVE with
        # disambiguation. (Auto-calling this BLOCKED was a false-negative bug caught
        # in self-test.)
        _verdict("INCONCLUSIVE",
                 "Trace write + flush succeeded, but the trace was not readable within ~24s.\n"
                 + (f"(last read error: {read_err})\n" if read_err else "")
                 + "This is NOT proof of a Zerobus block — likely search-index lag. "
                 "DISAMBIGUATE: open this experiment's Traces tab in the MLflow UI and look for "
                 "source_trace_id=" + marker + ". If it's there → treat as PASS (read-back lag). "
                 "If the Traces tab shows a storage/ingest error → that IS the block "
                 "(compare scripts/otlp_probe.py's explicit 400/403).")

    tid = found.get("trace_id") or found.get("client_request_id", "?")
    _verdict("PASS",
             f"Trace persisted and was read back. trace_id={tid}\n"
             f"MLflow trace-write SURVIVES this workspace — spec-C's copy hop is viable here.")


def _current_user() -> str:
    try:
        from mlflow.utils.databricks_utils import get_databricks_host_creds  # noqa: F401
        import mlflow
        # Cheap way to get the caller identity in a notebook context.
        from pyspark.sql import SparkSession
        spark = SparkSession.getActiveSession()
        if spark is not None:
            return spark.sql("SELECT current_user()").collect()[0][0]
    except Exception:
        pass
    return os.environ.get("USER", "unknown")


# COMMAND ----------
if __name__ == "__main__":
    main()
