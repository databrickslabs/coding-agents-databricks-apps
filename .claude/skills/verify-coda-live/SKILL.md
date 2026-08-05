---
name: verify-coda-live
description: Use after merging CoDA PRs, before a release, or when asked whether coda-main still works. Drives coda-main through an authenticated Chrome session and CoDA's JSON terminal API, runs the structured scripts/verify_coda_live.py smoke test, and returns evidence for SP-authenticated Pi/OpenCode inference, live workspace model-catalog parity, GitHub CLI access, and Databricks CLI workspace read/write access. Can switch coda-main's git-linked deployment branch and always restores it.
---

# Verify CoDA live on `coda-main`

This is the release smoke test for CoDA's base contract after a batch of merges.
It is deliberately executable by a cheaper agent: the decisions and shell logic
live in `scripts/verify_coda_live.py`; the agent only deploys, opens the page,
starts one terminal session through JSON APIs, and reports the resulting JSON.

## Required contract

A run passes only when all three hold:

1. **Agent inference / model discovery**
   - Pi and OpenCode both make a real model call through Unity AI Gateway.
   - The token used by Pi's helper identifies as the **app service principal**
     when `--expect-model-auth sp` is selected.
   - No user PAT is present for the SP-only baseline.
   - Each agent's configured model catalog exactly matches the compatible READY
     serving endpoints in the active workspace:
       - Pi: READY `databricks-claude-*` endpoints.
       - OpenCode: READY `databricks-claude-*`, `databricks-gemini-*`, and
         `databricks-gpt-*` endpoints.
     Extra stale models and missing active models both fail the run.
2. **GitHub**
   - `gh auth status` succeeds.
   - `gh api user` resolves a login.
   - `gh repo view databrickslabs/coding-agents-databricks-apps` succeeds and
     reports viewer permission.
   - Plain git can `ls-remote` main over HTTPS (proves gh/git credential wiring,
     not only the gh API).
3. **Databricks workspace**
   - `databricks current-user me` succeeds and reports the effective identity.
   - `databricks serving-endpoints list` succeeds.
   - A unique file can be created under `/Shared/coda-live-smoke-*`, exported
     byte-for-byte, and deleted. The verifier cleans it in `finally` even when
     an intermediate step fails.

The two inference prompts request one fixed marker each (`CODA_PI_OK`,
`CODA_OPENCODE_OK`) and enable no tools, so token/cost usage is minimal.

---

## Non-negotiable safety rules

- Target **`coda-main` only**. Never deploy, stop, start, or restart `coda`.
- Never request, paste, echo, print, or retrieve a PAT/client secret. The verifier
  reports only booleans, profile names, and `/Me` identity fields.
- Do not scrape xterm's canvas. Terminal output must come from `/api/output`.
- Do not use raw curl against the app URL. The Apps proxy returns `302 → OIDC`
  before the request reaches CoDA. Use `fetch()` from the authenticated page.
- If the page shows login, stop and ask the operator to authenticate.
- If changing the deployed branch, record the starting branch/commit and restore
  it at the end even after a failed test.
- Do not claim PASS from local tests or config inspection. Only the live JSON
  report counts.

---

## Phase 0 — identify and, if needed, deploy the target

Read the current deployment:

```bash
databricks apps get coda-main --profile <profile> --output json
```

Record:

- `active_deployment.git_source.branch`
- `active_deployment.git_source.resolved_commit`
- `active_deployment.status.state`

Choose the branch under test:

- After this skill/script is merged, normally use `main`.
- Before merge, use the PR branch containing
  `scripts/verify_coda_live.py` (currently `docs/verify-live-skill`).
- To validate another branch, it must include the verifier script or be rebased
  onto a commit that does.

`coda-main` is git-linked. Change only the git reference; repository URL/provider
are configured at app level and the API rejects them in the deploy request:

```bash
cat >/tmp/coda-verify-deploy.json <<'JSON'
{
  "mode": "SNAPSHOT",
  "git_source": {"branch": "BRANCH_UNDER_TEST"}
}
JSON

databricks apps deploy coda-main \
  --profile <profile> \
  --json @/tmp/coda-verify-deploy.json \
  --output json
```

Require `status.state == "SUCCEEDED"`. Record the resolved commit. If the deploy
fails, report the build message and stop; still restore at the end.

Immediately inspect boot logs before markers roll off the short tail:

```bash
databricks apps logs coda-main --profile <profile>
```

For the expected SP baseline, require:

- `Owner resolved from app.creator: ...`
- `SP token broker listening on loopback`
- `SP apikeyhelper: setup triggered at boot (no PAT paste needed)`

These two build errors are expected when optional Omnigent resources are not
attached and do **not** fail deployment:

- `error resolving resource omnigent-server-url ... not found`
- `error resolving resource omnigent-wheels ... not found`

Wait until the app is ACTIVE:

```bash
databricks apps get coda-main --profile <profile> --output json
```

---

## Phase 1 — attach to the authenticated Chrome page

Use Chrome DevTools tools, not curl.

1. `chrome_devtools_list_pages`
2. Select the page whose URL begins:
   `https://coda-main-<workspace-id>.8.azure.databricksapps.com`
3. If absent, `chrome_devtools_new_page` with that URL.
4. If a login/consent page appears, stop and ask the operator to finish login.

Confirm authenticated access from inside the page:

```js
async () => {
  const r = await fetch('/api/setup-status');
  return {status: r.status, body: await r.text()};
}
```

Require HTTP 200. Parse the body and report every setup step whose status is not
`complete`. A still-running step can be polled for up to 10 minutes. Any `error`
step is a failed smoke test.

Also record:

```js
async () => (await fetch('/api/pat-status')).json()
```

For `--expect-model-auth sp`, require no configured user PAT. This establishes
that successful inference cannot be accidentally attributed to a pasted PAT.

---

## Phase 2 — create one terminal session through the JSON API

Create the session:

```js
async () => {
  const r = await fetch('/api/session', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({label: 'coda-live-verifier'})
  });
  const body = await r.json();
  window.__codaVerify = {
    sessionId: body.session_id,
    output: '',
    begin: '__CODA_VERIFY_BEGIN__',
    end: '__CODA_VERIFY_END__'
  };
  return {status: r.status, body};
}
```

Require a `session_id`. Wait 2 seconds for the shell, then send the verifier.
The command never emits a token. It writes JSON to a temp file, prints it between
markers, and prints the exit code:

```js
async () => {
  const s = window.__codaVerify;
  const command = [
    "uv run --project /app/python/source_code python /app/python/source_code/scripts/verify_coda_live.py",
    "  --expect-model-auth sp",
    "  >/tmp/coda-live-report.json 2>/tmp/coda-live-error.txt; rc=$?;",
    "printf '\\n__CODA_VERIFY_BEGIN__\\n';",
    "if [ -s /tmp/coda-live-report.json ]; then cat /tmp/coda-live-report.json;",
    "else printf '{\"ok\":false,\"failures\":[\"verifier crashed\"],\"stderr\":';",
    "python3 -c 'import json;print(json.dumps(open(\"/tmp/coda-live-error.txt\").read()))';",
    "printf '}'; fi;",
    "printf '\\n__CODA_VERIFY_END__\\n__CODA_VERIFY_RC__:%s\\n' \"$rc\""
  ].join(' ');
  const r = await fetch('/api/input', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id: s.sessionId, input: command + '\n'})
  });
  return {status: r.status, body: await r.text()};
}
```

### Poll output without canvas scraping

`/api/output` **drains** the session buffer. Accumulate every chunk in a page
global. Use this 15-second poll repeatedly until `done` is true; model calls can
take a few minutes:

```js
async () => {
  const s = window.__codaVerify;
  for (let i = 0; i < 15; i++) {
    const r = await fetch('/api/output', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: s.sessionId})
    });
    const body = await r.json();
    s.output += body.output || '';
    if (s.output.includes(s.end)) break;
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  // PTY echoes the command itself, which contains the marker text. Use the
  // LAST begin marker, not the first echoed one, or reportText starts halfway
  // through the command instead of at the JSON report.
  const begin = s.output.lastIndexOf(s.begin);
  const end = s.output.lastIndexOf(s.end);
  const reportText = begin >= 0 && end > begin
    ? s.output.slice(begin + s.begin.length, end).trim()
    : null;
  const rcMatch = s.output.match(/__CODA_VERIFY_RC__:(\d+)/);
  return {
    done: end >= 0,
    rc: rcMatch ? Number(rcMatch[1]) : null,
    reportText,
    tail: s.output.slice(-2000)
  };
}
```

When `done` is true, parse `reportText` as JSON. If parsing fails, return the raw
text and tail; do not invent a result.

---

## Phase 3 — interpret the structured report

The verifier exits 0 only when every required lane passes. Still inspect the
fields; the failures are more valuable than the summary boolean.

### A. SP auth and model inference

Require:

```text
auth_material.default_pat_present == false
auth_material.broker_url_present == true
model_token_identity.classified_as == "service_principal"
model_token_identity.ok == true
inference.pi.ok == true
inference.pi.marker_seen == true
inference.opencode.ok == true
inference.opencode.marker_seen == true
```

`model_token_identity` executes Pi's configured helper command, keeps the bearer
only in memory, calls workspace SCIM `/Me`, and returns identity fields — never
the token. This is direct evidence that Pi's model token is the SP, not inference
from config flags.

For OpenCode, the content-filter proxy obtains the same brokered SP source. With
no PAT present, a successful OpenCode marker is evidence that the SP path works.

### B. Workspace model catalog parity

Require all three:

```text
model_catalogs.pi.exact_match == true
model_catalogs.opencode.exact_match == true
model_catalogs.opencode.cli_display_exact_match == true
```

The third assertion runs `opencode models databricks` and
`opencode models databricks-openai` and checks what the CLI **actually displays**,
not just what `opencode.json` contains.

Inspect all four sets:

- `configured`
- `expected_ready_compatible`
- `extra_not_ready`
- `missing_ready`
- for OpenCode: `cli_displayed`, `cli_display_extra`, `cli_display_missing`

Any model in `extra_not_ready` means the picker advertises something the active
workspace does not serve. Any model in `missing_ready` means an active compatible
model is absent from the picker. Do not waive one direction.

This check may expose a real issue: current Pi setup historically wrote only one
active model into `models.json`. If the workspace serves several Claude models,
Pi will fail exact parity. Report that as a product bug; do not weaken the test.

Also require:

```text
model_catalogs.pi.uses_gateway_route == true
model_catalogs.pi.api_key_is_helper_command == true
model_catalogs.opencode.uses_proxy_or_gateway == true
```

### C. GitHub

Require:

```text
github.ok == true
github.login is non-empty
github.repo.nameWithOwner == "databrickslabs/coding-agents-databricks-apps"
github.git_ls_remote_main == true
```

This is read-only. It proves gh API auth plus plain git credential-helper wiring.
Do not push, create issues, merge PRs, or mutate repository state.

### D. Databricks workspace

Require:

```text
databricks_cli.command_ok == true
workspace_models.command_ok == true
workspace_round_trip.ok == true
workspace_round_trip.round_trip_equal == true
all workspace_round_trip.steps.*.ok == true
```

The unique `/Shared/coda-live-smoke-*` path must be deleted. If cleanup failed,
report the exact path prominently so an operator can remove it.

---

## Phase 4 — cleanup and restore

Close the terminal session:

```js
async () => {
  const s = window.__codaVerify;
  if (!s || !s.sessionId) return {skipped: true};
  const r = await fetch('/api/session/close', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({session_id: s.sessionId})
  });
  return {status: r.status, body: await r.text()};
}
```

If the deployed branch was changed, always restore the branch recorded in Phase
0. Usually that is `main`:

```bash
cat >/tmp/coda-verify-restore.json <<'JSON'
{
  "mode": "SNAPSHOT",
  "git_source": {"branch": "main"}
}
JSON

databricks apps deploy coda-main \
  --profile <profile> \
  --json @/tmp/coda-verify-restore.json \
  --output json
```

Confirm restore status SUCCEEDED and `coda-main` ACTIVE.

---

## Fast diagnostic modes

Useful while iterating on the verifier, but **not sufficient for release PASS**:

- `--skip-inference`: no model calls; validates auth material, catalogs, gh and
  Databricks workspace operations.
- `--skip-workspace-write`: avoids the `/Shared` round-trip.
- `--expect-model-auth pat`: verifies a PAT-based branch instead of SP baseline.

For a real smoke test, use no skip flags.

---

## Suggested extended checks (not release blockers yet)

Run these after the base contract is stable:

1. **Session lifecycle:** detach/reattach and confirm buffered output replay.
2. **PAT fallback:** with an operator-provided short-lived PAT, rerun using
   `--expect-model-auth pat`; never have the agent handle the PAT.
3. **Cold boot:** stop/start `coda-main` and repeat, proving installs don't rely
   on cached binaries from an earlier deployment.
4. **Gateway attribution:** inspect AI Gateway usage for the two marker calls and
   confirm the principal is the expected app SP.
5. **Git write in a disposable repo:** create a temporary branch/commit/push only
   when explicitly authorized; read-only gh/git is the default smoke test.
6. **Unity Catalog:** create/read/drop a uniquely named table or volume under a
   dedicated smoke-test schema, if the app SP is supposed to have those grants.
7. **Owner security:** confirm a non-owner cannot create a terminal session or
   invoke owner-only endpoints in single-user mode.

---

## Required report format

Return one concise report with:

1. App, deployed branch, resolved commit, deployment status.
2. Setup steps not complete.
3. SP-auth evidence (no PAT, broker present, `/Me` classified SP).
4. Pi inference result + selected model.
5. OpenCode inference result + selected model.
6. Pi and OpenCode catalog parity — list extras and missing models explicitly.
7. GitHub login, viewer permission, and git ls-remote result.
8. Databricks CLI identity and workspace round-trip result.
9. Cleanup and branch-restore result.
10. Overall PASS/FAIL, copying the verifier's `failures` array verbatim.

Never collapse a failed subcheck into "mostly works." The purpose is to find the
regression surface created by the merge batch.
