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
- the complete private findings record and all unrepaired findings;
- any file or Make target that would expose or depend on those private layers.

These holds reopen only after the relevant implementation is remediated,
tenant-specific evidence is removed, an independent security/data review is
complete, and a new publication authorization names the resulting scope.

## Private source record

The complete six-finding record remains on `dev` at
`docs/plans/2026-08-13-session-admission-and-secret-boundary-findings.md`.
It is intentionally not copied into the public publication.
