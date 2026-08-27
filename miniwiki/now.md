# Current handoff

## 2026-08-19 — startup audit

- Working repository: `coding-agents-databricks-apps`.
- Local `dev` tracks `private/dev`; the refreshed measurement is `2 0`, so two
  local continuity commits are not yet present on the private mirror.
- `origin/main` was fetched and confirmed an ancestor of `dev`.
- The complete engineering record remains intact on private `dev`;
  it is not a public publication artifact.
- Pre-existing untracked `.isaac/` is preserved.

## 2026-08-19 — selective branch and validation

- `publish/recommended-subset` was reconstructed from refreshed `origin/main`; it
  does not merge `dev` wholesale and has no held-back path in its diff.
- Focused auth, gateway, capacity, terminal-boundary, and overlay tests pass.
- The non-Docker suite passes (`813 passed, 3 skipped`) on the final
  implementation. The full `uv run pytest tests -q` equivalent completed
  `814 passed, 3 skipped, 2 failed`; both failures were Docker setup-pipeline
  timeouts while external installers ran. CI deliberately excludes that
  network-dependent integration directory and its assertions remain enabled.
- Added Python 3.10 compatibility and staged all runtime modules required by
  the apps-like pipeline; commits `6652744`, `0d1e615`, `df7d528`, and
  `e884c4c` carry these fixes.
- Added-line tenant/secret scan and held-back-path intersection are empty;
  the Python 3.12 `requirements.lock` comparison is empty.

## 2026-08-19 — final pre-push validation

- Security fix `2623e7b` binds proxy readiness to the configured workspace host;
  broker-only Hermes setup is covered by focused gateway tests.
- Focused proxy/gateway tests after the fix: `89 passed`.
- The literal `pytest tests -q` command is unavailable in this shell; the `uv
  run` equivalent was used. Requirements dry-run, compileall, diff check, scrub
  scan, and held-back-path intersection pass on the current branch.
- The focused security re-review completed: the workspace-host binding passed;
  the requested Opus reviewer is unavailable in this environment. Generic
  miniwiki scope metadata was adjudicated as required non-sensitive decision
  evidence.
- Public PR: https://github.com/databrickslabs/coding-agents-databricks-apps/pull/137
- Tests workflow: https://github.com/databrickslabs/coding-agents-databricks-apps/actions/runs/32223937599
- Dependency Audit workflow: https://github.com/databrickslabs/coding-agents-databricks-apps/actions/runs/32223937335
  Both runs are queued with zero jobs because of the repository's external
  runner scheduling issue; no CI result or merge SHA exists yet.
- The topic branch is pushed directly to public `origin`; no private remote,
  private `main`, or Omnigent repository was modified.

Next: obtain green required CI, merge PR #137, fetch `origin`, prove the merge
SHA on `origin/main`, and append that evidence to this page and the decision
record.

## 2026-08-19 — publication outcome

- PR #137 merged into public `main` at
  `55137d5194699c2931812583fd1ec12dfdef680f`; a fresh fetch confirmed the
  merge commit on `origin/main`.
- GitHub Actions remained unscheduled (queued with zero jobs), so the merge was
  performed by explicit maintainer administrator authorization after local
  validation; no CI result was fabricated or bypassed as a test result.
- The public decision record now documents the merge evidence, validation, and
  the runner-scheduling limitation. `.isaac/` remains preserved and no private
  or Omnigent repository was modified.

## 2026-08-24 — aws-fevm deployment

- Confirmed the `aws-fevm` profile is valid for
  `https://fe-sandbox-classic-sandbox-ceeij9.cloud.databricks.com` and had no
  existing Apps before deployment.
- Created the fresh `coding-agents` CoDA app at MEDIUM compute; app ID
  `4de231de-9e3a-45d2-a1c3-5c4c3b9c3e21`.
- Synced the tracked contents of commit `737a0f4` to
  `/Workspace/Users/david.okeeffe@databricks.com/apps/coding-agents` using a
  temporary `git archive` staging directory, preserving unrelated local
  untracked files from deployment.
- Deployment `01f19f57b36511f68bcbda3d32d2b506` completed with `SUCCEEDED`; the
  app is `RUNNING` and compute is `ACTIVE`.
- Boot logs verified gunicorn startup, owner resolution, loopback SP token
  broker startup, secret-free Omnigent profile creation, and normal resource
  monitoring. The deployed app URL is
  `https://coding-agents-7474656066902749.aws.databricksapps.com`.

## 2026-08-27 — Codex/Gemini default hotfix

- On local `main`, commit `8f7d18f` re-enables Codex and Gemini by default in
  `app.yaml`, `app.yaml.template`, and `app.yaml.workshop`.
- Codex defaults are aligned to `databricks-gpt-5-3-codex`, and the setup
  fallback plus README/deployment documentation now describe the Responses-API
  requirement. Overlay tests cover all three replacement manifests and assert
  both enabled flags.
- Deterministic evidence: focused configuration/model tests pass (`47 passed`);
  the non-Docker suite passes (`809 passed, 1 skipped`) when excluding the
  unrelated broken `tests/test_apikey_helper.py`. The literal full non-Docker
  suite remains `810 passed, 1 skipped, 11 failed` because that test file's
  extractor cannot find the current `token_helper.py` source literal.
- Independent requirements, edge-case, security triage, and regression reviews
  completed. Hotfix-relevant findings were fixed; workspace endpoint
  availability and pre-existing installer/model-override risks remain recorded
  in `verification/hotfix-codex-gemini-defaults-72832c5.yaml`.
- No Databricks deployment or public push was performed. The post-commit sync
  hook recorded `SKIP` because this checkout is outside `/Users/david.okeeffe/projects`.
  Pre-existing untracked `.beads.retired-2026-08-19/` and
  `docs/omnigent-sandbox-architecture-gtm.md` remain untouched.

## 2026-08-27 — system.ai discovery follow-up

- Commit `8cac5b6` changes Codex and Gemini (plus Claude/Pi manifest seeds) to
  `system.ai` model-service defaults, discovers live provider-compatible models
  through `gateway_models.py`, and routes Codex/Gemini through the workspace AI
  Gateway paths used by ucode.
- Commit `57c2849` makes broker-only setup refresh-safe: Codex uses a per-request
  auth command and Gemini uses a launcher wrapper that resolves the shared token
  helper before each process. Static broker bearers are not persisted.
- Additional hardening handles malformed model-service responses and reuses one
  foundation-model metadata snapshot per catalog build. Auth documentation and
  focused runtime tests were updated.
- Verification: `73 passed` focused tests; `815 passed, 1 skipped` in the
  non-Docker suite excluding the unrelated `tests/test_apikey_helper.py`; ruff,
  compileall, YAML assertions, and diff checks pass. Final independent reviewer
  reruns were unavailable because all three provider calls returned OpenAI API
  `401 Invalid Token`; this is recorded as unavailable, not approval.
- The user explicitly authorized publication to `databrickslabs` remote. No
  remote push has yet been performed in this checkpoint; next action is push the
  reviewed commits and verify `origin/main`.

## 2026-08-27 — publication blocked by GitHub SSO

- Attempted `git push origin main` after the discovery/auth fixes. GitHub
  rejected the push with HTTP 403 because `databrickslabs` requires SAML SSO
  authorization for the active GitHub CLI OAuth application.
- `gh auth refresh --hostname github.com --scopes repo` issued a one-time device
  authorization at `https://github.com/login/device` and waited for browser
  authorization. The one-time code is intentionally not recorded. User action
  is required before retrying the push.
- Local `main` retains the verified commits through `5867aa2`; `origin/main`
  remains at `737a0f4`. No remote state was changed.
