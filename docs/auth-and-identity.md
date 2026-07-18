# Auth & identity on a CoDA box

Reference for who does what on a running CoDA host (e.g. `coda-01`..`coda-08`),
how each agent authenticates its model calls, and the known zero-PAT gap for
OpenCode / Hermes. Written 2026-07-11.

## TL;DR — two identities, don't confuse them

A CoDA container carries **two separate Databricks identities**:

| Identity | What it is | What it's used for |
|---|---|---|
| **The user** (e.g. `user@example.com`) via the PAT in `~/.databrickscfg [DEFAULT]` | A **user** personal access token (`dapi…`), kept fresh by the PAT rotator | **Everything the terminal / CLI does**: `databricks` commands, `git`, `gh`, workspace/UC ops, file writes. Also the token the content-filter proxy injects for OpenCode / Hermes / Codex model calls. |
| **The app service principal** (e.g. `app-4n8qml coda-02`, an OAuth client_id) | The Databricks App's own SP, no PAT | **Claude & Pi model inference** (via the `apiKeyHelper` / `!command` that mints an **SP-OAuth** token from the `omnigents-host` profile), plus the Omnigent host registration / tunnel. |

### So: when Claude runs a command on this box, who is it?

**You.** Any shell command Claude Code runs — `git commit`, `databricks …`, file
edits — authenticates with the **injected user PAT** in `~/.databrickscfg`, so it
acts as **your user identity**, not the app SP. That's why commits/pushes show as
you and CLI calls have your grants.

Only the **model inference** for Claude/Pi uses the app SP (zero-PAT, via the
token helper). The shell/tools do not.

> Consequence: on a **shared team CoDA**, everyone on that host shares the one
> injected PAT — so terminal actions all run as whoever injected it (the host
> owner), regardless of which teammate is driving. Segregation is at the Omnigent
> host-share layer, not per-user identity inside the box.

## How each agent authenticates its MODEL calls

| Agent | Mechanism | Zero-PAT (SP-OAuth) works? |
|---|---|---|
| **Claude** | `apiKeyHelper` in `~/.claude/settings.json` (shared `token_helper.py`) | yes |
| **Pi** | `!command` apiKey in `~/.pi/agent/models.json` (same `token_helper.py`) | yes |
| **OpenCode** | `baseURL` → local **content-filter proxy** (`127.0.0.1:4000`), which injects a fresh token per request | ⚠️ only after a PAT is injected once |
| **Hermes** | routes via the **content-filter proxy** too (same fresh-token injection) | ⚠️ only after a PAT is injected once |
| **Codex** | content-filter proxy | ⚠️ same as above |

### Why Claude/Pi are zero-PAT but OpenCode/Hermes aren't

- **Claude & Pi** resolve their bearer through `token_helper.py`, which mints an
  **SP-OAuth** token directly from the `omnigents-host` profile (falling back to
  a PAT only if that profile is absent). No user PAT required.
- **OpenCode / Hermes / Codex** route through `content_filter_proxy.py`, whose
  `_get_fresh_token()` reads the current token from **`~/.databrickscfg`**
  (`content_filter_proxy.py:54`, injected at `:569-573`). The PAT rotator keeps
  that file fresh — **but only after a PAT has been bootstrapped**. On the pure
  SP-OAuth host path there is no PAT, so `~/.databrickscfg` has no token to read
  until you inject one in the UI. That's the one-time PAT injection.
- Once injected, the proxy keeps OpenCode/Hermes fresh across PAT rotation with
  no further injection (that's the dynamic-refresh mechanism — it's not static).

## Known gap / future fix (not done — intentional)

To make **OpenCode / Hermes** zero-PAT like Claude/Pi, teach
`content_filter_proxy._get_fresh_token()` to **fall back to minting an SP-OAuth
token** from the `omnigents-host` profile (the same source `token_helper.py`
uses) when no PAT is present in `~/.databrickscfg`. Small, well-scoped change:

- `content_filter_proxy.py` — add an SP-OAuth mint (via `databricks.sdk` `Config(profile="omnigents-host").authenticate()`) as the fallback in `_get_fresh_token()`, cached with a short TTL like the current path.
- No change needed to `setup_opencode.py` / `setup_hermes.py` — they already
  route through the proxy; only the proxy's token source needs the fallback.

Decision (2026-07-11): **left as-is.** "Claude/Pi work with no PAT; Hermes/OpenCode
work after a one-time PAT injection" is acceptable for the workshop.

## Key files

- `token_helper.py` — shared SP-OAuth/PAT resolver for Claude (`apiKeyHelper`) and Pi (`!command`).
- `setup_claude.py` / `setup_pi.py` — wire the helper (default-on; opt out via `DISABLE_SP_APIKEYHELPER`).
- `content_filter_proxy.py` — local proxy for OpenCode/Hermes/Codex; `_get_fresh_token()` (`:54`) + header injection (`:569-573`).
- `setup_opencode.py` / `setup_hermes.py` — point the agent at the proxy (`127.0.0.1:4000`).
- `pat_rotator.py` — mints/rotates the user PAT, writes `~/.databrickscfg`, fans out via `cli_auth.py`.
- `cli_auth.py` — on rotation, refreshes static tokens in each agent's config (skips the `!command` / helper-owned ones).
- `omnigents_host.py` — host path: `_ensure_{claude,pi,opencode}_settings()` re-run setup with a minted SP bearer on host-connect.
