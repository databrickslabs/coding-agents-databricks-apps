# /speckit.specify — (B) CoDA client redirection + app-to-app auth

**Component:** CoDA (Coding Agents on Databricks Apps).
**Feature:** When the restricted-network flag is on, point **Claude Code's**
MLflow tracing at a **self-hosted MLflow OSS app** (spec-A) instead of
`MLFLOW_TRACKING_URI=databricks`, and authorize the CoDA app SP to reach that app
via the same app-to-app SP-OAuth pattern the repo already ships for the Omnigent
host integration.
**Scope note:** This spec covers **Claude Code** — the one coding agent with a
native MLflow path. OpenCode/Hermes/Pi are traced via the proxy (spec-D), which
uses this spec's OSS destination + SP grant. **Codex is out of scope** (dropped
2026-07-11), so its `setup_codex.py` notify hook is untouched.
**Status:** Draft. Depends on spec-A (the OSS server must exist and expose a URL).
**Profile:** `<dev-profile>`.

## 1. Problem

`setup_mlflow.py:44` sets `MLFLOW_TRACKING_URI = "databricks"` and (on 3.14+)
runs `mlflow autolog claude -u databricks` — the Claude Code plugin path. It sends
spans **directly to Databricks-managed trace storage** over the network. In the
restricted environment that destination is unreachable (no zerobus, no managed
storage — see `GOAL.md` §1). We need Claude Code to send the same traces to the
MLflow OSS app's URL instead, and we need CoDA's app service principal to be
**allowed** to call that app (a Databricks App has zero ambient privileges
against another app — `grant_omnigent_host.sh:13`).

## 2. Users
- **Facilitator / maintainer:** flips one flag to move the whole fleet's tracing
  onto the OSS path in a restricted workspace; unaffected everywhere else.
- **CoDA app SP:** the identity that actually authenticates to the OSS app.
- **Attendee:** sees no difference — agents behave identically.

## 3. Requirements

### Functional

- **B-R1 — Restricted-network flag.** Add a single gate, e.g.
  `MLFLOW_OSS_TRACKING_ENABLED` (default `false`), read in `setup_mlflow.py`
  (and shared by spec-D's proxy tracing — one flag for the whole feature). When
  `false`, behaviour is **exactly** today's (direct `databricks` path) — this
  feature is purely additive and off by default.
  *(additive, mirrors the `ENABLE_*` gating pattern in `app.yaml.workshop`.)*
- **B-R2 — Redirect the tracking URI.** When the flag is on,
  `setup_mlflow.py` sets `MLFLOW_TRACKING_URI = <MLFLOW_OSS_URL>` (the OSS app's
  HTTPS URL, from env) instead of `"databricks"`, in
  `~/.claude/settings.json.env`.
  *(surgical — one value swap, gated.)*
- **B-R3 — Claude plugin against the OSS URL.** Resolve open question Q3
  (`GOAL.md` §7): the current path calls `mlflow autolog claude -u databricks`.
  Determine whether the plugin accepts `-u <http-url>`. If **yes**, pass the OSS
  URL. If **no**, fall back to generic MLflow client tracing
  (`MLFLOW_TRACKING_URI=<oss-url>` + `MLFLOW_EXPERIMENT_NAME`, no plugin) — the
  Claude transcript is still traced, just not via the plugin runtime. Whichever
  works, the acceptance test is G-1 (a live Claude session lands a trace in the
  OSS server), not which code path did it. *(spec-A/B seam — verify, don't
  assume.)*
- **B-R4 — App SP bearer on the MLflow client.** The MLflow client must present
  the CoDA app SP's OAuth bearer to the OSS app (Databricks Apps gate inbound
  requests on the caller identity). Set `MLFLOW_TRACKING_TOKEN` to a fresh SP
  OAuth token. **Reuse the `apiKeyHelper` / SP-OAuth token source from spec-C SP
  auth** — the same bearer that already authenticates Claude Code to the gateway
  authenticates it to the OSS app. If tokens rotate, the token must be refreshed
  in `settings.json.env` on the same cadence the PAT rotator already runs
  (`cli_auth.py`), OR minted per-call by a helper. *(reuses existing SP-OAuth
  plumbing — do NOT invent a second token source.)*
- **B-R5 — App-to-app grant script.** Add `grant_mlflow_host.sh` (mirroring
  `grant_omnigent_host.sh`) that resolves the CoDA app SP via
  `databricks apps get <coda-app>` and grants it **`CAN_USE` on the MLflow OSS
  app**. Idempotent (check-then-grant, like the Omnigent script). Wire it into
  the deploy flow as a Make target next to `grant-omnigent-host`. *(new — but a
  near-copy of a proven script.)*
- **B-R6 — Experiment name unchanged.** Keep
  `experiment_name = f"/Users/{app_owner}/{app_name}"` (`setup_mlflow.py:34`) as
  the logical experiment name written into the OSS server. The copy job (spec-C)
  maps it to the same-named Databricks experiment, so the destination looks
  identical to the direct path. *(no change — reuse the existing name.)*

### Non-functional

- **B-N1 — Best-effort, never block (G-6).** If the OSS URL is unset,
  unreachable, or returns an error, agent startup and every agent session must
  proceed normally, trace dropped. Preserve the existing
  "never block startup on tracing" guard (`setup_mlflow.py:103`). No new failure
  can brick an agent.
- **B-N2 — Default path untouched.** With the flag off, `git diff` of runtime
  behaviour is zero — the `databricks` path and its tests behave exactly as
  before. *(regression guard.)*
- **B-N3 — No secrets on disk beyond today's posture.** The SP token written to
  `settings.json.env` is the same class of secret already there
  (`ANTHROPIC_AUTH_TOKEN` / OTEL bearer). No new secret-at-rest surface.

## 4. Constraints

- **B-C1 — App SP has zero ambient privilege.** CoDA's SP cannot reach the OSS
  app until `CAN_USE` is granted (B-R5). This is the entire reason the grant
  script exists; verified true for the Omnigent case
  (`grant_omnigent_host.sh:13`).
- **B-C2 — The MLflow client is a plain HTTP client**, not the Databricks SDK.
  Its only auth channel to a self-hosted server is `MLFLOW_TRACKING_TOKEN`
  (bearer) / `MLFLOW_TRACKING_*` basic auth. Confirm the OSS app accepts the app
  SP OAuth bearer on inbound (open question Q5, `GOAL.md` §7) — Databricks Apps'
  inbound auth accepts SP OAuth tokens for `CAN_USE`-granted callers, but verify
  against the deployed OSS app, not by assumption.
- **B-C3 — `settings.json` is read-merge-written** by several setup scripts
  (`setup_claude.py`, `setup_mlflow.py`, `claude_otel.py`). The new env keys must
  be *merged*, not clobbering MLflow/OTEL/apiKeyHelper keys already there
  (same hazard called out in spec-C SP auth C-C2).
- **B-C4 — Token TTL vs session length.** SP OAuth tokens are short-lived (~1h).
  A long agent session must not die mid-trace on an expired
  `MLFLOW_TRACKING_TOKEN`. Prefer a per-mint helper or align the refresh with the
  rotator cadence (B-R4). A dropped trace on expiry is acceptable (B-N1); a
  crashed agent is not.

## 5. In scope
The flag; the tracking-URI + token redirect in `setup_mlflow.py` (Claude Code);
`grant_mlflow_host.sh` + its Make wiring; the plugin-vs-generic decision (B-R3);
merging env keys safely.

## 6. Out of scope
- The OSS server itself (spec-A) and the copy job (spec-C).
- Extending tracing to the 5 currently-untraced agents (`GOAL.md` §3).
- Replacing the PAT/SP-OAuth token *source* — B-R4 reuses spec-C's, it does not
  redesign it.

## 7. Open questions
- **B-O1 (=Q3) — LIKELY RESOLVED.** `mlflow autolog claude -u <url>` accepts a
  non-`databricks` tracking URI (documented for `file://` and `sqlite://`; `http(s)://`
  follows the same pattern — verified 2026-07-11, spec-A §0). Still confirm the
  `http(s)://` case specifically on the app venv python (mlflow 3.14) before
  relying on it — that's the one variant no doc example covers explicitly.
- **B-O2 (=Q5).** Does a `CAN_USE`-granted app SP OAuth bearer authenticate an
  ordinary inbound HTTP call to the OSS app (as opposed to only SDK calls)? This
  is the make-or-break for app-to-app. Probe the deployed OSS app directly.
- **B-O3.** One shared SP profile for both the Omnigent host and MLflow host
  grants, or separate? Lean shared (fewer OAuth profiles), matching spec-C
  SP-auth C-O2.

## 8. Success criteria
- **B-S1 (=G-1).** On a deployed <dev-profile> instance with the flag on, a live
  `claude -p "..."` session lands a trace in the **MLflow OSS server**, read back
  from the OSS API.
- **B-S2 (=G-7).** `grant_mlflow_host.sh` is idempotent and the grant survives a
  redeploy; with the grant absent, the OSS call is rejected (proving the grant is
  load-bearing, not incidental).
- **B-S3 (=G-6).** With the OSS app stopped, a Claude session still completes;
  no agent error, trace dropped.
- **B-S4 (=B-N2).** With the flag off, the direct `databricks` path is byte-for-
  byte unchanged (existing `tests/test_mlflow_tracing.py` still passes).
- **B-S5.** Verified by driving the agent on a deployed instance, not by config
  inspection alone (repo discipline: configured ≠ flowing).
