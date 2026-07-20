# Design: adopt `ucode` for the interactive-terminal agent config path

**Status:** proposed (follow-up to the jq + Gemini-schema hotfixes shipped
2026-07-20)
**Owner:** TBD
**Scope:** the *interactive terminal* launch path only. Explicitly NOT the
Omnigent runner native-harness path (see §4).

---

## 1. Problem

CoDA hand-maintains per-agent config writers — `setup_pi.py`,
`setup_opencode.py`, `setup_codex.py`, `setup_gemini.py`, plus their
token-rotation glue in `cli_auth.py` — that **reimplement, less completely,
what [`ucode`](https://github.com/databricks/ucode) already does**
(`~/Repos/ucode`, `src/ucode/agents/{pi,opencode,codex,gemini,claude}.py`).

This duplication is not cosmetic — it is the direct source of two production
bugs found on 2026-07-20:

1. **`exclusiveMinimum` 400 (opencode).** CoDA routes opencode's Claude models
   over the OpenAI-compatible `/chat/completions` dialect to the mlflow gateway
   surface (via the content-filter proxy), whose OpenAPI-3.0-subset validator
   rejects JSON-Schema-only keywords. ucode routes Claude to
   `@ai-sdk/anthropic` → `/ai-gateway/anthropic/v1` (Anthropic Messages), which
   never hits that validator. ucode also carries the per-model `compat` flags
   (`toolStreaming:false`, `supportsEagerToolInputStreaming:false`, per-model
   UA headers) that work around the gateway's strict validators — CoDA does not,
   so CoDA rediscovers each gateway quirk one incident at a time.

2. **Empty-token `jq` failure.** Not a CoDA config bug (it's omnigent's — see
   §4), but note ucode's own token resolver (`get_databricks_token`) uses
   `json.loads`, never `jq`, so a ucode-driven interactive session would not
   have this failure mode either.

ucode is actively maintained upstream (dialect fixes, model-catalog discovery,
compat flags, new model families land there first). Every fix CoDA lands in
`setup_*.py` is a fix ucode already shipped.

## 2. Why not "just replace everything with ucode"

Because CoDA has **three** launch layers with different owners, and ucode only
covers one of them:

| Layer | Config writer | ucode applies? |
|---|---|---|
| Interactive terminal (`pi`/`opencode` typed in the CoDA shell) | CoDA `setup_*.py` | **Yes — this proposal** |
| Omnigent runner native harness (Web-UI dispatched sessions) | omnigent `pi_native_credentials.py` (reads `~/.omnigent/config.yaml`) | **No — bypasses ucode entirely** |
| Auth (SP broker, PAT rotation, `omnigents-host` M2M profile) | CoDA `token_helper.py` / `pat_rotator.py` / `sp_token_broker.py` | **No equivalent — but ucode has a clean seam (§3)** |

## 3. Proposed change (interactive path)

Replace CoDA's per-agent config writers with a ucode install + launch, using
ucode's built-in `DATABRICKS_BEARER` short-circuit as the auth seam.

- **Auth seam.** ucode's `get_databricks_token` (src/ucode/databricks.py:791)
  returns `$DATABRICKS_BEARER` verbatim when set, skipping the
  `databricks auth token` subprocess entirely. CoDA already mints a fresh SP
  bearer via the loopback broker; export it as `DATABRICKS_BEARER` into the
  interactive shell env (refreshed on the same cadence as the existing PAT
  rotator). No CLI, no jq, no profile juggling for the model-auth path.
- **Install.** `uv tool install git+https://github.com/databricks/ucode`
  (pin a ref; supply-chain-harden like the existing npm pins). Add an
  `install_ucode.sh` mirroring `install_tmux.sh`/`install_jq.sh`.
- **Launch.** Users run `ucode pi`, `ucode opencode`, etc. Alias or wrap the
  bare `pi`/`opencode` names so muscle memory still works.
- **Delete.** `setup_pi.py`, `setup_opencode.py`, and the model/config bodies
  of `setup_codex.py`/`setup_gemini.py`; the `_update_pi`/`_update_opencode`/
  `_update_gemini` rotators in `cli_auth.py` (ucode owns config now). Keep
  `setup_claude.py` only if the Claude-Code apiKeyHelper path (spec C) is still
  required — evaluate separately, ucode also configures claude.
- **Net effect.** ~500 LOC deleted from CoDA; dialect + compat + model-catalog
  behaviour inherited from ucode and kept current by upstream.

### Risks / open questions
- ucode assumes an interactive U2M user; the `DATABRICKS_BEARER` seam is the
  only supported non-interactive entry. Verify it covers *every* ucode code path
  the launched agents hit (token refresh mid-session, `configure`, `status`).
- ucode relocates each tool's config dir (`PI_UCODE_HOME`, `OPENCODE_XDG_CONFIG_HOME`).
  Confirm this coexists with CoDA's existing `~/.pi`, `~/.config/opencode`
  without confusing users who inspect those paths.
- The content-filter proxy exists partly to sanitize empty content blocks
  (opencode #5028) and to inject a fresh rotating token. If ucode talks to the
  gateway directly (correct dialect, `DATABRICKS_BEARER` auth), decide whether
  the proxy is still needed on the interactive path or only for tracing.
- MCP server registration (deepwiki/exa, enterprise overrides) currently written
  by `setup_opencode.py` — ucode has `configure mcp`; map CoDA's enterprise
  MCP config onto it.

## 4. Explicitly out of scope: the Omnigent runner path

The errors originally reported came from the **Omnigent runner native harness**,
which does NOT use ucode or CoDA's `setup_*.py`. omnigent's
`pi_native_credentials.py` writes a managed `models.json` from
`~/.omnigent/config.yaml`. Fixes for that path live in **omnigent**
(`~/Repos/omnigent`) or in CoDA's host wiring (`omnigents_host.py`,
`~/.omnigent/config.yaml` provider + the jq install already shipped). Adopting
ucode does not touch it. If the runner path needs the same dialect correctness
ucode has, that is a separate omnigent change (e.g. omnigent's
`_unsupported_in_pi` / provider-dialect selection).

## 5. Recommendation

Pursue §3 as a standalone PR with its own deploy + cold-boot verification
(`ucode pi` authenticates via `DATABRICKS_BEARER`; `ucode opencode` tool call
succeeds with no `exclusiveMinimum` 400). Keep the 2026-07-20 hotfixes in place
regardless — they unblock both paths today and the jq fix is required by the
runner path independent of this migration.
