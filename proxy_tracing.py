"""Fire-and-forget MLflow span emission for the content-filter proxy (spec-D).

Traces OpenCode / Hermes / Pi — the coding agents that route model requests
through content_filter_proxy.py (127.0.0.1:4000) but have no first-party MLflow
hook. One span per model request, emitted on a background thread AFTER the
response is sent, so the model call's latency is never affected (D-N1).

Design constraints (spec-D):
  - D-N1: NEVER block or slow the request path. All MLflow work runs on a bounded
    background pool; if the queue is full or MLflow is down, the span is dropped.
  - D-N2: streaming-safe — the caller passes the already-assembled response.
  - D-N4: keep the proxy lightweight — mlflow import is isolated here and guarded;
    a missing mlflow degrades to no-tracing, never a proxy crash.
  - D-N3/D-C3: content vs metadata — full prompt/response bodies are captured ONLY
    when PROXY_TRACE_CONTENT=true (default: metadata only).

Enabled by MLFLOW_OSS_TRACKING_ENABLED=true + MLFLOW_OSS_URL (same flag as spec-B).
Auth to the OSS app uses the app-SP M2M token (MLFLOW_TRACKING_TOKEN), set by the
same mechanism as setup_mlflow.py.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("proxy-tracing")

_ENABLED = (
    os.environ.get("MLFLOW_OSS_TRACKING_ENABLED", "false").lower() == "true"
    and bool(os.environ.get("MLFLOW_OSS_URL", "").strip())
)
_CAPTURE_CONTENT = os.environ.get("PROXY_TRACE_CONTENT", "false").lower() == "true"


def _resolve_experiment() -> str:
    """The experiment each CoDA's traces land in. Prefer an explicit
    MLFLOW_EXPERIMENT_NAME; else DERIVE `/Users/{APP_OWNER}/{DATABRICKS_APP_NAME}`
    — the SAME name setup_mlflow.py creates — from env every CoDA app already has.

    FLEET-CRITICAL: without this the proxy leaves the experiment unset and every
    CoDA's spans pile into the Default experiment (id 0) on the shared OSS server,
    with no per-CoDA separation. Deriving per-app means coda-01, coda-02, … each
    route to their OWN experiment automatically, no per-CoDA config needed."""
    explicit = os.environ.get("MLFLOW_EXPERIMENT_NAME", "").strip()
    if explicit:
        return explicit
    owner = os.environ.get("APP_OWNER", "").strip()
    app = os.environ.get("DATABRICKS_APP_NAME", "").strip()
    if owner and app:
        return f"/Users/{owner}/{app}"
    return ""  # cannot derive → server default (Default); logged in _ensure_mlflow


_EXPERIMENT = _resolve_experiment()

# Small bounded pool: span emission is I/O to the OSS server. If it saturates,
# new spans are dropped (D-N1) rather than queued unboundedly.
_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()
_mlflow_ready = False
_init_lock = threading.Lock()

# App-SP M2M token for the OSS app. The proxy runs in a SEPARATE process from
# Claude Code, so it does NOT inherit MLFLOW_TRACKING_TOKEN from
# ~/.claude/settings.json (where setup_mlflow.py writes it). Databricks Apps
# reject PATs/user bearers (302 → OIDC) and accept only an app-SP M2M token, so
# this proxy must mint its OWN — same mechanism as setup_mlflow._mint_app_sp_token
# and the proxy's upstream _get_fresh_token pattern. Cached under the ~1h token
# life and refreshed in-process (no restart needed).
_token_cache: dict = {"token": None, "minted_at": 0.0}
_token_lock = threading.Lock()
_TOKEN_TTL = 45 * 60  # refresh well under the ~1h OAuth token life


def _mint_app_sp_token() -> str | None:
    """Mint an app-SP OAuth (client-credentials/M2M) token for the OSS app URL.

    Mirrors setup_mlflow._mint_app_sp_token: prefer the `omnigents-host` M2M
    profile (written when the host integration is on); else the injected app-SP
    client creds. Returns None if neither is available.
    """
    try:
        from databricks.sdk.core import Config
    except Exception:  # noqa: BLE001 — sdk missing → no token, no tracing
        return None
    try:
        headers = Config(profile="omnigents-host").authenticate()
        auth = (headers or {}).get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
    except Exception:  # noqa: BLE001 — try the injected creds next
        pass
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


def _refresh_oss_token() -> bool:
    """Ensure a fresh app-SP token is in os.environ['MLFLOW_TRACKING_TOKEN'].

    MLflow's HTTP client reads MLFLOW_TRACKING_TOKEN per request, so keeping this
    env var current (in this process) is enough to authenticate every span emit.
    Returns True if a token is available. Cached + time-refreshed under the token
    life so we mint at most once per _TOKEN_TTL.
    """
    now = time.time()
    tok = _token_cache["token"]
    if tok and (now - _token_cache["minted_at"]) < _TOKEN_TTL:
        os.environ["MLFLOW_TRACKING_TOKEN"] = tok
        return True
    with _token_lock:
        tok = _token_cache["token"]
        if tok and (now - _token_cache["minted_at"]) < _TOKEN_TTL:
            os.environ["MLFLOW_TRACKING_TOKEN"] = tok
            return True
        fresh = _mint_app_sp_token()
        if fresh:
            _token_cache["token"] = fresh
            _token_cache["minted_at"] = now
            os.environ["MLFLOW_TRACKING_TOKEN"] = fresh
            return True
        log.warning("could not mint app-SP token for OSS app; spans will 302 without it")
        return bool(_token_cache["token"])  # stale is better than nothing


def _get_pool() -> ThreadPoolExecutor | None:
    global _pool
    if not _ENABLED:
        return None
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="trace")
    return _pool


def _ensure_mlflow():
    """Lazily configure MLflow once, on a background thread. Returns the module or None."""
    global _mlflow_ready
    try:
        import mlflow
    except Exception:  # noqa: BLE001 — mlflow not installed → no tracing
        return None
    # Refresh the app-SP token on EVERY call (cheap when cache-hot), so a span
    # emitted after the ~1h token expiry re-mints instead of silently 302-ing.
    # Must run outside the _mlflow_ready gate — the URI is set once, the token
    # is not. Without this the OSS app (a Databricks App) rejects the write.
    _refresh_oss_token()
    if not _mlflow_ready:
        with _init_lock:
            if not _mlflow_ready:
                try:
                    mlflow.set_tracking_uri(os.environ["MLFLOW_OSS_URL"].rstrip("/"))
                    if _EXPERIMENT:
                        # set_experiment creates it if absent, so each fleet member
                        # auto-provisions its own experiment on first trace.
                        mlflow.set_experiment(_EXPERIMENT)
                        log.info("proxy tracing → experiment %s", _EXPERIMENT)
                    else:
                        log.warning("no experiment resolved (APP_OWNER/DATABRICKS_APP_NAME "
                                    "unset) — traces will land in the OSS Default experiment")
                    _mlflow_ready = True
                except Exception as e:  # noqa: BLE001
                    log.warning("MLflow tracing init failed (%s); disabling", e)
                    return None
    return mlflow


def _header(headers: dict, name: str) -> str:
    """Case-insensitive header lookup. HTTP header casing is unpredictable across
    clients (Pi/OpenCode/http.client all differ), so match on lowercased keys —
    a title-case `X-Coda-Session` must resolve the same as `x-coda-session`."""
    if not headers:
        return ""
    low = name.lower()
    for k in headers:
        if k.lower() == low:
            return headers[k] or ""
    return ""


def _agent_from_request(path: str, req: dict, headers: dict) -> str:
    """Best-effort agent attribution (D-R2). Priority:
      1. explicit `x-coda-agent` header (set by the CLI when it can);
      2. agent name in the User-Agent;
      3. the request PROTOCOL as a coarse label — better than a useless
         'proxy-agent'. Anthropic Messages (`/v1/messages`) vs OpenAI
         (`/chat/completions`). Doesn't uniquely identify the agent (Pi+Claude
         both speak Anthropic; OpenCode+Hermes both OpenAI), but tells you the
         dialect, which is more useful than 'proxy-agent' for filtering."""
    explicit = _header(headers, "x-coda-agent")
    if explicit:
        return explicit
    ua = _header(headers, "user-agent").lower()
    for name in ("opencode", "hermes", "pi"):
        if name in ua:
            return name
    if "/v1/messages" in path or "/v1/complete" in path:
        return "anthropic-api"
    if "/chat/completions" in path or "/completions" in path:
        return "openai-api"
    return "proxy-agent"


def _emit(path, req, resp, status, agent, session_id, user, project, t_start, t_end):
    """Runs on a background thread — build + log one MLflow span."""
    mlflow = _ensure_mlflow()
    if mlflow is None:
        return
    try:
        model = (req or {}).get("model", "unknown")
        usage = (resp or {}).get("usage", {}) if isinstance(resp, dict) else {}
        tags = {"agent": agent}
        if session_id:
            tags["session_id"] = session_id
        if user:
            tags["mlflow.user"] = user
        if project:
            tags["project"] = project

        with mlflow.start_span(name=f"{agent}_request") as span:
            # Set trace-level tags INSIDE the span so a trace is active (else they
            # go nowhere — verified 2026-07-12). These are what the copy job and
            # the Traces UI filter on (session grouping, agent attribution).
            if hasattr(mlflow, "update_current_trace"):
                mlflow.update_current_trace(tags=tags)
            span.set_attributes({
                "agent": agent,
                "model": model,
                "http.status": status,
                "path": path,
                "latency_ms": round((t_end - t_start) * 1000, 1),
                "tokens.input": usage.get("input_tokens") or usage.get("prompt_tokens"),
                "tokens.output": usage.get("output_tokens") or usage.get("completion_tokens"),
            })
            if _CAPTURE_CONTENT:
                span.set_inputs({"messages": (req or {}).get("messages")})
                span.set_outputs({"response": resp})
            else:
                # metadata only (D-N3): shapes, not content
                span.set_inputs({"n_messages": len((req or {}).get("messages", []))})
                span.set_outputs({"stop_reason": (resp or {}).get("stop_reason")})
        try:
            mlflow.flush_trace_async_logging(terminate=False)
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001 — a tracing failure must never surface
        log.debug("span emit failed (%s)", e)


def trace_request(*, path, request_body, response_body, status, headers,
                  t_start, t_end):
    """Fire-and-forget: schedule span emission. Returns IMMEDIATELY (D-N1).

    Called by content_filter_proxy AFTER the response has been written to the
    client, so nothing here is on the model call's critical path.
    """
    pool = _get_pool()
    if pool is None:
        return
    agent = _agent_from_request(path, request_body or {}, headers or {})
    # session/user/project threaded via headers the app/CLI sets (D-R4/D-O2).
    # Case-insensitive lookup — a client sending X-Coda-Session (title case) must
    # still resolve, else session grouping silently breaks (review #8).
    session_id = _header(headers or {}, "x-coda-session") or os.environ.get("CODA_SESSION_ID", "")
    user = _header(headers or {}, "x-forwarded-email") or os.environ.get("CODA_USER", "")
    project = _header(headers or {}, "x-coda-project") or os.environ.get("CODA_PROJECT", "")
    try:
        pool.submit(_emit, path, request_body, response_body, status, agent,
                    session_id, user, project, t_start, t_end)
    except Exception:  # noqa: BLE001 — pool rejected (saturated) → drop the span
        pass
