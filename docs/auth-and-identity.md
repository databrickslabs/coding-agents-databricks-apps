# Auth & identity on a CoDA box

Reference for who does what on a running CoDA host (e.g. `coda-01`..`coda-08`),
how each agent authenticates its model calls, and the known zero-PAT gap for
OpenCode / Hermes. Written 2026-07-11.

## TL;DR — two identities, don't confuse them

A CoDA container carries **two separate Databricks identities**:

| Identity | What it is | What it's used for |
|---|---|---|
| **The user** (e.g. `user@example.com`) via the PAT in `~/.databrickscfg [DEFAULT]` | A **user** personal access token (`dapi…`), kept fresh by the PAT rotator | **Everything the terminal / CLI does**: `databricks` commands, `git`, `gh`, workspace/UC ops, and file writes. |
| **The app service principal** (e.g. `app-4n8qml coda-02`, an OAuth client_id) | The Databricks App's own SP, no PAT | **Agent model inference**, Omnigent host registration, and spawned-runner callbacks via short-lived OAuth tokens from the loopback broker. |

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
| **OpenCode** | `baseURL` → local **content-filter proxy** (`127.0.0.1:4000`), which injects a fresh token per request | yes |
| **Hermes** | routes via the **content-filter proxy** too (same fresh-token injection) | yes |
| **Codex** | content-filter proxy | yes |

### Browser-terminal secret boundary

- The Flask process alone retains the app-SP client secret.
- A loopback-only broker mints short-lived OAuth tokens on demand.
- The `[omnigents-host]` profile stores only `host`; it has no client ID,
  client secret, or static token.
- Browser PTYs use a deny-by-default environment allowlist. Workspace
  credentials, client secrets, registry tokens, bootstrap secrets, and
  credential-bearing URL values are excluded before the shell starts.
- The loopback broker coordinate is an intentional, narrow capability needed by
  terminal-launched CLIs; it is not a bearer token.

This is an environment-inheritance control, not an OS process sandbox. A
deployment that requires confidentiality from terminal-executed code still
needs separate identities or container/UID isolation.

## Key files

- `sp_token_broker.py` — loopback-only app-SP token broker.
- `token_helper.py` — shared broker/SP-OAuth/PAT resolver for agent helpers.
- `content_filter_proxy.py` — local proxy for OpenCode/Hermes/Codex.
- `omnigents_host.py` — host supervision and spawned-runner refresh wiring.
- `pat_rotator.py` — optional user-PAT fallback and rotation.
