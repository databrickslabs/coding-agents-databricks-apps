# /speckit.specify — (C) Claude Code `apiKeyHelper` for SP-level auth

**Component:** CoDA (Coding Agents on Databricks Apps), branch `feat/workshop-fleet`.
**Feature:** Replace Claude Code's static `ANTHROPIC_AUTH_TOKEN` (rotated into
`~/.claude/settings.json` by the PAT rotator) with an `apiKeyHelper` command
that fetches a fresh Databricks token per-TTL. Target the app service
principal's OAuth as the token source so the per-user PAT can be dropped for
the workshop.
**Status:** Draft. Motivated by a live failure (2026-07-07): the PAT rotator
rewriting `~/.databrickscfg` clobbered the `[omnigents-host]` OAuth profile,
and more broadly the rotate-a-static-token model is the root cause of the
"token expired in settings.json" fragility.

## 1. Problem

Claude Code authenticates to the Databricks AI Gateway with a **static**
bearer token written to `~/.claude/settings.json` as `env.ANTHROPIC_AUTH_TOKEN`
(`setup_claude.py:80`). Claude Code treats that value as a fixed string — it has
no built-in token refresh. To keep the token alive, CoDA runs `pat_rotator.py`,
which every N minutes mints a fresh **user PAT** and rewrites it into every CLI
config, including `settings.json` (`cli_auth.py:_update_claude`) and
`~/.databrickscfg` (`pat_rotator._write_databrickscfg`).

Two costs fall out of this design:

- **Per-user PAT required.** Every attendee must paste a PAT into CoDA Setup
  before Claude works — friction the workshop wants to remove (spec A).
- **Config-rewrite fragility.** The rotator owns `~/.databrickscfg`; its
  DEFAULT-only rewrite has clobbered the co-owned `[omnigents-host]` OAuth
  profile (verified 2026-07-07 — see `project_coda_omnigents_host_runner_auth`),
  breaking SP-authed subprocesses.

Claude Code supports `apiKeyHelper`: a settings.json key holding a shell command
that Claude Code runs to obtain the bearer token, re-invoking it on the interval
set by `CLAUDE_CODE_API_KEY_HELPER_TTL_MS`. This inverts the model — Claude Code
**pulls** a live token per-TTL instead of CoDA **pushing** a static one. Omnigent's
own native-claude bridge already uses this exact mechanism against the same
gateway (`omnigent/claude_native.py`), so the pattern is proven, not speculative.

## 2. Users
- **Attendee:** should not have to paste a PAT for Claude Code to work.
- **Facilitator:** wants fewer moving parts per instance (no per-attendee PAT
  provisioning, no rotator/config-clobber failures).
- **CoDA maintainer:** wants the `~/.databrickscfg` clobber class of bug gone.

## 3. Requirements

### Functional
- **C-R1 — apiKeyHelper writes a helper script + settings key.** `setup_claude.py`
  writes an executable helper (e.g. `~/.claude/anthropic-token-helper.sh`) that
  prints a bearer token to stdout, and sets `apiKeyHelper` in `settings.json` to
  its path. The helper must emit **only** the token on stdout (Claude Code reads
  stdout verbatim). *(new)*
- **C-R2 — SP OAuth as token source (workshop path).** The helper mints the token
  via the app SP's OAuth (`databricks auth token -p <sp-profile>` → `.access_token`,
  the AI-Gateway-accepted bearer), so no user PAT is needed. Requires the SP OAuth
  profile to exist and survive (see C-R5). *(new — this is the "drop the PAT" win)*
- **C-R3 — Set the TTL.** Set `env.CLAUDE_CODE_API_KEY_HELPER_TTL_MS` in
  `settings.json` so Claude re-runs the helper before the token expires (SP OAuth
  tokens are short-lived; default ~1h). Pick a TTL comfortably under expiry
  (e.g. 15 min = `900000`) to match Omnigent's default. *(new)*
- **C-R4 — Rotator stops touching Claude's config.** Once the helper is the token
  source, `cli_auth._update_claude` no longer needs to rewrite
  `ANTHROPIC_AUTH_TOKEN`, and the rotator's reason to own it disappears. Remove
  the Claude arm from `update_cli_tokens` **only if** it no longer manages any
  other Claude setting the rotator must keep current (audit the OTEL token
  refresh in `_update_claude` — that may still need the rotator). *(surgical)*
- **C-R5 — Preserve the SP OAuth profile.** The helper depends on the SP OAuth
  profile in `~/.databrickscfg`. The rotator's `_write_databrickscfg` must not
  clobber it. The b5b11a6 preserve-non-DEFAULT-sections fix is meant to cover
  this — **verify it actually holds** (the 2026-07-07 clobber suggests either a
  regression or a container-restart path that never re-wrote the profile), and
  fix if not. *(root-cause fix for the clobber bug)*

### Non-functional
- **C-N1 — Fallback preserved.** Non-workshop CoDA (per-user PAT model) must keep
  working. If the SP OAuth profile is absent, the helper should fall back to the
  existing PAT source, or setup should keep the static-token path — gate the
  apiKeyHelper path on an env flag or on SP-profile presence so the standard app
  is unaffected. *(additive, like the ENABLE_* pattern in spec A)*
- **C-N2 — No token on disk longer than needed.** The helper mints on demand; it
  must not cache the token to a world-readable file. stdout only.

## 4. Constraints
- **C-C1** Claude Code reads `apiKeyHelper` stdout as the literal token — any
  stray log line, prompt, or trailing junk corrupts auth. The helper must be
  silent except for the token (redirect helper stderr, `2>/dev/null` on the
  token command, no interactive prompt — `databricks auth token` can drop into a
  "Databricks host:" prompt if the profile is missing; guard against that).
- **C-C2** `apiKeyHelper` + `CLAUDE_CODE_API_KEY_HELPER_TTL_MS` live in
  `~/.claude/settings.json` (helper is a top-level key; TTL is under `env`).
  Preserve the existing read-merge-write in `setup_claude.py` so MLflow/OTEL
  env additions aren't clobbered.
- **C-C3** SP OAuth token is the AI-Gateway bearer only. It does **not** replace
  the user PAT for workspace writes / git / CLI — those keep the user's identity.
  This changes model-call identity to the SP; confirm that's acceptable for the
  workshop's audit posture (single-user governed app — likely fine, but a
  deliberate call).

## 5. In scope
The helper script + settings wiring in `setup_claude.py`; the TTL; making the
rotator stop clobbering Claude's token (C-R4) and the SP profile (C-R5); an
env/flag gate so the standard app keeps the PAT path.

## 6. Out of scope
- Dropping the user PAT for non-model actions (git, workspace writes) — those
  still need the user's identity.
- The broader Omnigent host integration (spec/other memories).
- Rewriting the rotator's PAT-minting for the standard app.

## 7. Open questions
- **C-O1 — SP OAuth token audience.** Confirm `databricks auth token -p <sp>`
  returns a token the `/anthropic` gateway accepts (the host tunnel already uses
  SP OAuth successfully, so likely yes — verify against the gateway, not just the
  token endpoint).
- **C-O2 — Which SP profile.** Reuse the `[omnigents-host]` OAuth profile the host
  writes, or write a dedicated Claude helper profile? Reusing couples this to the
  host feature; a dedicated profile is cleaner but duplicates the OAuth-profile
  write. Lean: a small shared `_write_oauth_profile` used by both.
- **C-O3 — TTL vs expiry.** Confirm SP OAuth token lifetime and set the TTL under
  it with margin. If the CLI caches/refreshes the OAuth token in-process, the
  helper cost is just a cache read — cheap enough to run per-turn.
- **C-O4 — b5b11a6 status (C-R5).** Determine whether the preserve-sections fix
  regressed or whether the restart path skips the profile write, then fix the
  actual cause.

## 8. Success criteria
- **C-S1** On a workshop instance with **no PAT configured**, Claude Code makes a
  successful model call — the token comes from the SP-OAuth apiKeyHelper.
- **C-S2** The `[omnigents-host]` OAuth profile survives a full PAT-rotation cycle
  (and a container restart) — no clobber.
- **C-S3** The standard (non-workshop) app is unaffected: per-user PAT model still
  works, gated off the apiKeyHelper path.
- **C-S4** Verified on a deployed instance by driving Claude Code, not by config
  inspection alone.
