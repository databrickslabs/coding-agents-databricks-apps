# Omnigent Host Deployment: UC Traversal Grants + Git-Based Deploy

> **For the implementer:** This is a build-later spec. It captures a hard-won
> production diagnosis (why a CoDA instance did not appear in the Omnigent UI)
> and the deploy-automation work needed so it never recurs. Implement
> task-by-task; each task is independently verifiable.

**Goal:** Make a freshly deployed CoDA app reliably register as an Omnigent host
with a single, repeatable, Git-based deploy — including the *complete* Unity
Catalog permission chain its service principal needs, which the current tooling
under-grants.

**Status:** Not started. Diagnosis complete and verified live (2026-07-11).

---

## Background: the live incident (why this spec exists)

A deployed CoDA instance (`coda-04`) was missing from the Omnigent UI host
picker. Peeling it apart, in order:

1. **App-level `CAN_USE` on the Omnigent app** — already granted. Not the cause.
   The SP authenticated to the server fine (`GET /v1/hosts` → `200`).
2. **Host registration** — the server returned `{"hosts":[]}` and the instance's
   deterministic `host_id` 404'd. So the host had *never registered*.
3. **Root cause** — the `omnigent` CLI was never installed in the container
   (`which omnigent` → not found, no `~/.omnigent/logs/`, no host process). The
   boot-time install downloads the wheel from `OMNIGENTS_WHEEL_SPEC`
   (a UC Volume: `/Volumes/<cat>/<schema>/<vol>`), and that download failed with:

   ```
   User does not have USE CATALOG on Catalog '<catalog>'.
   ```

4. **Why** — the app SP had `READ_VOLUME` + `WRITE_VOLUME` on the *volume*, but
   **not** the Unity Catalog *traversal* privileges required to reach it:
   `USE_CATALOG` on the catalog and `USE_SCHEMA` on the schema. In UC you need
   the full chain (`USE_CATALOG` → `USE_SCHEMA` → `READ_VOLUME`) or you cannot
   read the volume even with `READ_VOLUME`.

**Fix applied live (manually):** granted `USE_CATALOG` + `USE_SCHEMA` to the SP
(via a group), after which `_materialize_spec(...)` successfully downloaded the
omnigent wheels using the SP creds — proving the chain. The instance still
needs a **restart** to re-run boot install/launch, because it booted before the
grant and the install does not retry in-process.

### The tooling gap this spec closes

`grant_omnigent_host.sh` (and therefore `make grant-omnigent-host`, wired into
`deploy`/`redeploy`) grants only:

- `CAN_USE` on the server app, and
- `READ_VOLUME` + `WRITE_VOLUME` on the wheel volume.

It **never grants `USE_CATALOG` or `USE_SCHEMA`**, so any SP that isn't already
carrying those (directly or via a group) silently fails to install the CLI and
never registers as a host. Every documented "grant" verifies green while the
host stays invisible — a confusing failure mode.

---

## Non-Goals

- Do not change Omnigent server semantics or the `omnigents_host.py` supervisor.
- Do not auto-restart apps as part of the grant (grants persist; restart is a
  separate, explicit step). But DO document that a restart is required when an
  app booted before its grants landed.
- Do not hardcode catalog/schema/volume/server names — derive them from the
  deployed `app.yaml` (as the Makefile already does for `WHEEL_VOLUME`).

---

## Task 1 — Fix `grant_omnigent_host.sh`: grant the full UC traversal chain

Add, between the existing `CAN_USE` grant and the volume grant, two idempotent
grants derived by splitting `WHEEL_VOLUME` (`<catalog>.<schema>.<volume>`):

- **2a. `USE_CATALOG`** on `${WHEEL_VOLUME%%.*}` (the catalog)
- **2b. `USE_SCHEMA`** on `${WHEEL_VOLUME%.*}` (the `<catalog>.<schema>`)
- **2c.** existing `READ_VOLUME` + `WRITE_VOLUME` on the volume

Requirements:

- Idempotent: check current grants first; treat `ALL_PRIVILEGES` as satisfying
  the `USE_*` requirement (some catalogs grant `ALL_PRIVILEGES` to groups).
- Extend the final verification block to also assert `USE_CATALOG` is present;
  fail the script (non-zero) if any link in the chain is missing.
- Update the header comment to document the full chain and *why* `READ_VOLUME`
  alone is insufficient (cite the `User does not have USE CATALOG` error).

Use `databricks grants get|update catalog|schema <fq-name>` with the same
`{"changes":[{"principal": <sp>, "add":[<priv>]}]}` shape already used for the
volume grant.

**Verify:** on a workspace where the SP lacks catalog/schema `USE`, run the
script and confirm the SP can then list the wheel volume:

```bash
databricks fs ls "dbfs:/Volumes/<cat>/<schema>/<vol>" --profile <sp-profile>
```

## Task 2 — Optional group-based grant path (nice-to-have)

Support granting a **group** instead of each SP directly, so N CoDA instances
share one set of grants:

- Add `--grant-group <name>` (mutually exclusive with per-SP): grant the UC
  chain + volume perms to the group, and ensure the app SP is a member.
- **Gotcha to document:** account-federated groups CANNOT have members edited
  via the workspace SCIM `preview` endpoint (`"can only be managed in account"`).
  Use the **account SCIM proxy** the UI uses:
  `PATCH /api/2.0/account/scim/v2/Groups/{id}` (works with a workspace-admin PAT).
  Workspace-local groups accept `/api/2.0/preview/scim/v2/Groups/{id}`.
- Membership uses the SP's SCIM `id` (== the app's `service_principal_id`), not
  its `client_id`.

## Task 3 — Git-based deploy in the Makefile

The current `sync` + `apps deploy --source-code-path` flow deploys from a
workspace copy. Add a **Git-repository** deploy path so CoDA instances deploy
directly from `main` (matches how coda-01..08 already run).

Add targets (keep existing sync-based ones for backward compat):

```make
GIT_URL      ?= https://github.com/databrickslabs/coding-agents-databricks-apps
GIT_PROVIDER ?= gitHub
GIT_REF      ?= main
GIT_REF_TYPE ?= branch   # branch | tag | commit

configure-git:   ## Attach the Git repo to the app (create or create-update)
configure-git-credential: ## Add a Git credential to the app SP (private repos; reads token from stdin)
deploy-git:      ## Deploy the app from the configured Git ref
redeploy-git: grant-omnigent-host deploy-git
```

Implementation notes (from Databricks Apps Git-deploy docs):

- Create with repo:
  `databricks apps create <app> --json '{"git_repository":{"url":"<URL>","provider":"gitHub"}}'`
- Attach to existing:
  `databricks apps create-update <app> --json '{"update_mask":"git_repository","git_repository":{...}}'`
- Private repos need a Git credential on the **app SP** (`CAN MANAGE` on the SP
  required to add it):
  `databricks git-credentials create --json '{"git_provider":"gitHub","git_email":"<e>","personal_access_token":"<t>","principal_id":<SP_ID>,"name":"..."}'`
  — read the token from stdin, never on the command line.
- Deploy from a ref (branch/tag/commit are mutually exclusive):
  `databricks apps deploy <app> --json '{"git_source":{"branch":"main"}}'`
  (or `{"tag":...}` / `{"commit":...}`; optional `"source_code_path"` for a
  subdirectory).
- Note the docs caveat: apps created before Git-deploy GA may not grant the
  creator `CAN MANAGE` on the SP — a workspace admin may need to grant it before
  a Git credential can be added.

Keep `deploy-git` composable with `grant-omnigent-host` exactly like the
existing `deploy`/`redeploy` targets, so grants run before/with each deploy.

## Task 4 — Document in `docs/deployment.md`

Add a **"Deploy from a Git repository"** section (UI + CLI, per Databricks docs)
and an **"Omnigent host permissions"** subsection that spells out:

- the required IAM: `CAN_USE` on the server app + the full UC chain
  (`USE_CATALOG` → `USE_SCHEMA` → `READ_VOLUME`/`WRITE_VOLUME`);
- that `READ_VOLUME` alone is a silent trap (cite the exact error);
- the group option and the account-SCIM-proxy gotcha;
- that an app which booted **before** its grants must be **restarted** to
  re-run the CLI install/host launch (grants don't retroactively install);
- how to verify: SP can `fs ls` the wheel volume; `GET /v1/hosts` lists the
  deterministic `host_id`; the instance appears in the Omnigent picker (share
  to the user if it's SP-owned).

Cross-link from the Makefile `grant-omnigent-host` comment.

---

## Acceptance criteria

- [ ] `grant_omnigent_host.sh` grants and verifies the full UC chain; a fresh SP
      with zero UC privileges can install the omnigent wheel after one run.
- [ ] `make deploy-git` / `make redeploy-git` deploy a CoDA app from a Git ref,
      including private-repo Git-credential setup, with grants applied.
- [ ] `docs/deployment.md` documents Git deploy + the full Omnigent host grant
      chain + the "restart after late grant" and account-SCIM-proxy gotchas.
- [ ] A brand-new instance, deployed via the Git target on a workspace where the
      SP starts with no UC access, registers as a host and shows in the picker
      with no manual grant surgery.

## Test / verification commands

```bash
# UC chain reachable as the SP
databricks fs ls "dbfs:/Volumes/<cat>/<schema>/<vol>" --profile <sp>

# Host registered on the server (as the SP; host_id is deterministic per SP)
#   host_id = "host_" + sha256("coda-omnigents-host:<sp_client_id>")[:32]
GET /v1/hosts        # should list the host_id

# In-container signals after a good boot
which omnigent
ls ~/.omnigent/logs/host-runner/
```

## References

- `grant_omnigent_host.sh`, `Makefile` (`grant-omnigent-host`, `deploy`/`redeploy`)
- `omnigents_host.py` (`ensure_installed`, `_materialize_spec`, `_install_command`,
  `_stable_host_identity`, `connect_host`)
- `HANDOFF-omnigents-host.md`, `docs/plans/2026-06-14-omnigent-host-runtime-control.md`
- Databricks Apps: "Deploy from a Git repository"
