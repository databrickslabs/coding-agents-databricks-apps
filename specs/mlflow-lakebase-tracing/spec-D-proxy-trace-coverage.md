# /speckit.specify — (D) Proxy-based trace coverage for OpenCode, Hermes, Pi

**Component:** CoDA — the content-filter proxy (`content_filter_proxy.py`).
**Feature:** Emit an MLflow trace **span per model request** from the local
content-filter proxy, so the three currently-un-traced coding agents
(**OpenCode, Hermes, Pi**) produce traces — without a per-CLI hook. Route Pi
through the proxy so all three are covered by one instrumentation point.
**Status:** Draft. **This is the coverage half of the feature** — specs A/B/C
change *where* traces go; spec-D creates traces that don't exist today.
**Profile:** `<dev-profile>`.

## 0. Why this spec exists (the coverage gap)

The destination specs (A/B/C) assume traces exist to redirect. For three of the
four target agents, **they don't**:

| Agent | MLflow trace today? | Routes through proxy `127.0.0.1:4000`? |
|-------|:-------------------:|:--------------------------------------:|
| Claude Code | ✅ (plugin) | no (has its own MLflow path) |
| **OpenCode** | ❌ none | ✅ yes (`setup_opencode.py:33,221,250`) |
| **Hermes** | ❌ none | ✅ yes (`setup_hermes.py:135`) |
| **Pi** | ❌ none | ❌ **no** — straight to gateway (`setup_pi.py:111`) |

`docs/observability.md` §1 already diagnosed this: "there is no drop-in hook…
OpenCode already routes every request through a local content-filter proxy…
**the one clean interception point** among the four — instrument the proxy and
OpenCode calls become traceable with no per-CLI hook. Pi/Hermes/Gemini go
straight to the gateway with no such choke point." Hermes has *since* been moved
onto the proxy too (`setup_hermes.py:116-135`), so **only Pi is left outside** —
and re-pointing Pi at the proxy closes the set.

So: **instrument the proxy once → OpenCode + Hermes traced. Route Pi through the
proxy → Pi traced too.** One mechanism, three agents.

## 1. Problem

OpenCode, Hermes, and Pi make model calls but emit no MLflow traces. We want
per-session traces for them in the same Databricks experiments as Claude Code
(via the OSS→Lakebase→copy path). The proxy is the only place that sees every
request for OpenCode/Hermes without touching each CLI; Pi bypasses it today.

## 2. Users
- **Trace consumer:** wants OpenCode/Hermes/Pi sessions visible alongside Claude
  Code, not a blank for 3 of 4 agents.
- **CoDA maintainer:** wants one instrumentation point, not three CLI wrappers.

## 3. Requirements

### Functional

- **D-R1 — Span per model request in the proxy.** In
  `content_filter_proxy.py`'s `ProxyHandler.do_POST` (line 537 — the single method
  every model request flows through), wrap the upstream call in an MLflow span.
  Capture: model/endpoint (from `self.path` / request body), the request messages,
  the response (or a size/preview of it), token usage if present, latency, and
  status. *(the core — one place, all proxied agents.)*
- **D-R2 — Attribute the span to the agent.** The span must record which agent
  made the call (`opencode` / `hermes` / `pi`). The proxy serves all three on the
  same port, so agent identity must be derived — from a header the CLI sets, the
  request shape, or a per-agent proxy port/path. Decide the discriminator
  (D-O1). *(without this, all three collapse into one undifferentiated stream.)*
- **D-R3 — Route Pi through the proxy.** Change `setup_pi.py` so Pi's `base_url`
  points at `http://127.0.0.1:4000` (like OpenCode/Hermes) instead of
  `{gateway_host}/anthropic` (`setup_pi.py:111`), with the proxy's
  `PROXY_UPSTREAM_BASE` set to the gateway `/anthropic` route. Verify Pi still
  works through the proxy (the proxy already sanitizes + refreshes tokens).
  *(the gap-closer — makes coverage uniform.)*
- **D-R4 — Session grouping by tag.** Per the chosen grain (per-request spans),
  a "session" is a set of spans sharing a `session_id` tag. The proxy must stamp
  each span with a `session_id` (and `user`, `project` where available) so the
  copy job (spec-C C-R6) and the Traces UI can group them. The proxy needs the
  session id threaded to it — via a request header the app/CLI sets, or an env
  var per agent process. Determine the propagation path (D-O2). *(this is what
  turns loose spans into legible sessions — the "organized nicely" ask.)*
- **D-R5 — Write to the same MLflow destination.** The proxy's spans go to the
  MLflow OSS server (spec-A) via `MLFLOW_TRACKING_URI=<oss-url>` +
  `MLFLOW_EXPERIMENT_NAME=/Users/{owner}/{app_name}` (spec-B naming), authed with
  the SP OAuth bearer the proxy already has access to (`_get_fresh_token`,
  `content_filter_proxy.py:54`). Same experiment as Claude Code → one unified
  view. *(reuses spec-A/B destination + the proxy's existing token.)*

### Non-functional

- **D-N1 — Never add latency or failure to a model call (hard rule).** The proxy
  is on the **critical request path** for OpenCode/Hermes/Pi. MLflow tracing must
  be **fire-and-forget / async / best-effort** — a slow or dead MLflow server must
  **never** delay or fail a model request. If span emission would block, drop the
  span. This is stricter than the setup-time best-effort rule (B-N1) because it's
  per-request, not per-boot. *(the make-or-break NFR — get this wrong and you've
  slowed every agent turn.)*
- **D-N2 — Streaming responses.** The proxy handles SSE streaming
  (`SSEProcessor`, line 378). The span must close correctly for streamed
  responses (end the span when the stream flushes, `flush_remaining` line 513),
  capturing the assembled response, not mid-stream. *(don't leave spans open or
  capture partial output.)*
- **D-N3 — No sensitive-payload hoarding beyond policy.** The proxy sees full
  prompts/responses (incl. customer/pricing data in the workshop). Match the
  gateway policy (`observability.md` §3): capture usage/metadata freely; gate full
  request/response **content** behind a flag (default off, or previewed/truncated)
  so tracing doesn't become the payload-hoarding the envelope forbids. *(governance
  parity — the same catch as the gateway inference tables.)*
- **D-N4 — Proxy stays lightweight.** The proxy deliberately uses "stdlib +
  requests" only (`content_filter_proxy.py:14`). Adding mlflow imports there is a
  real dependency bump — confirm mlflow is available in the proxy's runtime, and
  keep the tracing code isolated so a missing mlflow degrades to no-tracing, not a
  proxy crash. *(don't break the proxy's minimalism / startup.)*

## 4. Constraints

- **D-C1 — One port, three agents (D-R2).** The discriminator for agent identity
  must be reliable. A per-agent port (4000/4001/4002) is the most robust but means
  three listeners; a header is lighter but needs each CLI to set it. Decide
  deliberately.
- **D-C2 — Pi re-routing must not regress Pi (D-R3).** Pi works today going
  direct; moving it behind the proxy adds the proxy's sanitization to Pi's path.
  Verify Pi's request shape survives the proxy's OpenCode-oriented sanitizers
  (`sanitize_messages`, etc.) — they may assume a shape Pi doesn't share. *(the
  proxy was built for OpenCode; Pi is a new client of it.)*
- **D-C3 — Content vs metadata (D-N3).** Same two-switch discipline as the gateway:
  usage/latency/model always; full content only behind an explicit flag.
- **D-C4 — Session-id propagation (D-R4).** The proxy is a separate process from
  the agent; it only knows what's in the request or its env. Threading a stable
  session id to it is the crux of making sessions legible — no id, no grouping.

## 5. In scope
Span emission in `do_POST`; agent attribution; routing Pi through the proxy;
session/user/project tagging; async/best-effort write to the OSS destination;
streaming-safe span close; the content-vs-metadata gate.

## 6. Out of scope
- Claude Code tracing (it has its own path — spec-B).
- Codex (dropped from scope per 2026-07-11 decision).
- Gemini (not in the target set).
- The OSS server (spec-A) and copy job (spec-C) — spec-D is a *producer* into the
  same destination.
- Per-session (single-trace) grain — chosen grain is **per-request spans grouped
  by session tag**; nesting into one session trace is a possible later refinement,
  not this spec.

## 7. Open questions
- **D-O1 — Agent discriminator (D-R2/D-C1).** Per-agent port, request header, or
  inferred? Lean: a header each `setup_*` writes into the CLI's request config, if
  the CLIs allow custom headers; else per-agent ports.
- **D-O2 — Session-id propagation (D-R4/D-C4).** How does a stable session id
  reach the proxy? Options: the app sets an env var per agent subprocess it spawns
  (the app *does* spawn these — it knows the session), or a header. This likely
  touches `app.py`'s subprocess launch, not just the proxy.
- **D-O3 — Is mlflow importable in the proxy runtime (D-N4)?** The proxy runs as
  its own process; confirm the app venv (mlflow 3.14) is what runs it, or add a
  guarded import.
- **D-O4 — Streaming span timing (D-N2).** Does wrapping the whole `do_POST`
  cleanly capture the SSE-assembled response, or does the span need to live inside
  `SSEProcessor.flush_remaining`? Prototype on one streamed OpenCode call.
- **D-O5 — Async write mechanism (D-N1).** Thread pool, background queue, or
  `mlflow` async logging? Whatever guarantees the request path never blocks on the
  MLflow write.

## 8. Success criteria
- **D-S1.** A live **OpenCode** session produces a trace in the MLflow OSS server
  (then, via spec-C, in the Databricks experiment), tagged `agent=opencode` +
  `session_id`.
- **D-S2.** Same for **Hermes** (`agent=hermes`) and **Pi** (`agent=pi`) — Pi via
  its new proxy route (D-R3).
- **D-S3 (=D-N1).** With the MLflow OSS server **stopped**, OpenCode/Hermes/Pi
  model calls have **no added latency and no failures** — spans silently dropped.
  Measured, not assumed (compare turn latency proxy-with-tracing vs
  tracing-disabled).
- **D-S4 (=D-N2).** A **streamed** response yields one well-formed span with the
  full assembled response, not a partial or orphaned span.
- **D-S5.** In the destination experiment, the three agents are
  **distinguishable** (agent tag) and their requests **group into sessions**
  (session_id tag) — verified in the Traces UI.
