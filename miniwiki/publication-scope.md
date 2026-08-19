# Publication scope and acceptance contract

## Decision

Publish the reviewed auth/credential, proxy/gateway/MLflow, capacity fix, and
supporting documentation/test changes to public `main`. Publish a generalized
write-up of only the fixed active-page-cache accounting issue and the fixed
browser-terminal credential boundary. Keep the complete private engineering
record on `dev`.

## Acceptance contract

The final public merge must satisfy all of the following:

- the PR is merged into `databrickslabs/coding-agents-databricks-apps:main`;
- the refreshed `origin/main` contains the merge commit;
- the published diff contains the authorized implementation and decision pages,
  no held-back path, and no tenant-specific host, workspace, gateway, app,
  identity, email, token, or secret values;
- focused tests and `pytest tests -q` pass;
- `uv pip install --dry-run -r requirements.txt`, a fresh lock compile,
  `git diff --check`, and the explicit scrub/intersection gates pass;
- review findings are reproduced and resolved or explicitly adjudicated;
- local `dev` remains tracked by `private/dev`, and neither `private/main` nor
  any Omnigent repository is modified.

## Public implementation set

The selected code covers CLI authentication and credential refresh, the
loopback service-principal token broker, credential-writer hardening, the
AI-gateway proxy/model catalog and MLflow setup, the active-file-cache capacity
fix, their tests, and the dependency/CI/docs changes required by those
behaviours. The Beads boot/install path is retired from the public branch.

## Held back

- workshop fleet provisioning and challenge-repository automation;
- managed Omnigent host integration and its evidence pages;
- unreleased private engineering items and their supporting evidence;
- the complete private findings record and any other unrepaired work;
- any file or Make target that would expose or depend on those private layers.

These holds reopen individually only after the corresponding implementation is
remediated, tenant-specific evidence is removed, an independent security/data
review is complete, and a new publication authorization names the item and the
resulting scope. Credential-boundary work additionally requires proof that all
bootstrap and rotation paths use the controlled exchange and revoke bootstrap
credentials. Any reliability or integration work requires new deterministic
regression coverage and review evidence before reconsideration.

## Private source record

The complete private source record remains on the private development line.
It is intentionally not copied into the public publication, and the public
record does not link to a private-only path.

## Publication outcome

- PR #137 was merged into public `main` on 2026-08-19 with merge commit
  `55137d5194699c2931812583fd1ec12dfdef680f`.
- `origin/main` was fetched after the merge and points to that merge commit.
- Required GitHub Actions checks could not be scheduled: both workflows remained
  queued with zero jobs, cancellation returned HTTP 500, and the Actions
  permissions check returned HTTP 403. The merge used explicit maintainer
  administrator authorization rather than bypassing the recorded local
  validation.
- Local evidence before merge: focused `406 passed, 1 skipped`; CI-equivalent
  suite `813 passed, 3 skipped`; full suite `814 passed, 3 skipped` with two
  Docker integration timeouts; dependency dry-run, fresh lock comparison,
  `git diff --check`, scrub scan, and held-back-path intersection passed.
- The published diff contained no held-back paths and the added-line tenant/secret
  scrub was empty. The complete private record and held-back layers remain
  private.
