# GOAL: Stop shared-CoDA workspace sync-backs from clobbering each other

> **Status:** DRAFT (design). Not yet built.
> **Author:** David O'Keeffe · **Date:** 2026-07-12
> **Repo the fix lands in:** `coding-agents-databricks-apps` (private mirror
> `<private-mirror>`).
> **Files touched:** `sync_to_workspace.py`, `restore_from_workspace.py`
> (path derivation — must stay symmetric), `app.py` (post-commit hook +
> shared-storage banner). No new infra.

---

## 1. Problem — one path, many writers

Every repo under `~/projects/` is synced back to the Databricks Workspace by a
`post-commit` git hook (`app.py:465`), which runs `sync_to_workspace.py` in a
**detached `nohup` subprocess** (`app.py:504`). The sync target is:

```python
# sync_to_workspace.py:57   (unchanged since bbd898b, 2026-02-03)
workspace_dest = f"/Workspace/Users/{user_email}/projects/{project_path.name}"
```

`restore_from_workspace.py:91` reads back from the **identical** formula. The two
files are coupled by an implicit shared convention, not a shared function — any
path change must land in both, atomically, or restore silently reads the old
location.

`user_email` comes from `current_user.me()` (`sync_to_workspace.py:42`) — i.e.
whatever identity the **shared `~/.databrickscfg` PAT** resolves to. On a shared
CoDA every session sees the same file, so `user_email` is **identical across all
writers**. The only thing keeping paths distinct is `{project_path.name}` — the
repo basename.

**Collision truth table** (N writers, same PAT identity):

| Scenario | `user_email` | `project_path.name` | Collide? |
|----------|-------------|---------------------|----------|
| Same identity, **same repo name** | identical | identical | **YES — overwrite** |
| Same identity, different repo names | identical | differs | No |
| Distinct per-instance identities | differs | any | No |

This bites the **shared-app fleet** (`project_workshop_shared_app`: one PAT for
all attendees) and any multi-instance deployment where two CoDAs sync a
same-named repo (very likely — they all carry
`<private-mirror>` and the challenge repo). Last writer
wins; the others' Workspace copy silently disappears. Because the Workspace copy
**is** the ephemeral-recovery path (`docs/agent-instructions.md` §0/§4), a
collision means "restore" rehydrates the wrong writer's tree.

### 1a. Three layers of shared state — but only one is a code bug

A shared CoDA shares **one filesystem in one container** (`omnigents_host.py:5`,
`:682`). Multiple developers therefore collide at three layers:

| Layer | Shared resource | Collision | Fix |
|-------|-----------------|-----------|-----|
| 1. Working tree | one `~/projects/{repo}` on disk | devs edit/commit the *same files* | **guidance:** use git worktrees/branches |
| 2. Git identity | one `~/.databrickscfg` → one `me()` | commits attributed to the PAT identity | **guidance** (see non-goals) |
| 3. Sync target | `workspace_dest` identical | sync-backs overwrite in Workspace | **this spec's code fix** |

Layers 1–2 are **not** solved with per-user isolation machinery — a git worktree
already gives each developer an isolated working dir + branch on the shared
filesystem with zero new infra. The right intervention is to **make the
shared-storage reality explicit** (banner + `~/projects/README`) and steer
developers to worktrees. Only layer 3 needs code.

## 2. What "done" looks like

1. Two writers on the same/different CoDA committing a same-named repo produce
   **distinct** `workspace_dest` paths — neither overwrites the other.
2. `restore_from_workspace.py` reads from the **same** derived path (round-trip
   proven, not assumed).
3. A solo CoDA's behaviour is **backward-compatible** enough to still recover
   prior syncs, OR a documented one-time migration is provided.
4. Developers on a shared CoDA are **told** they share storage and should use
   worktrees — before they collide, not after.

## 3. Non-goals

- **Per-developer `~/projects/{user}/` roots** — rejected; worktrees cover it.
- **Per-session git author identity** — a shared box commits as the shared
  identity; correcting authorship needs per-session `GIT_AUTHOR_*` plumbing into
  a detached hook and is out of scope. Documented as a known limitation.
- **Per-end-user (SSO email) sync isolation** — the `X-Forwarded-Email` signal
  exists only in the *web request*; the detached sync subprocess and the
  Omnigent-runner path have **no request context** (`omnigents_host.py:682`), so
  this signal is unreachable at sync time without new plumbing. Explicitly out.

## 4. Why this shape (load-bearing decisions)

- **Base path `/Workspace/Shared/coda/…`** (decided 2026-07-12). Identity-neutral;
  honest for a shared app whose PAT identity is a service account, not a person.
  **Cost:** requires the sync identity to have **write ACL on `/Workspace/Shared`**
  — verify per target workspace (open question Q1).
- **Disambiguator = signal present in BOTH execution contexts.** The sync fires
  from the browser terminal *and* from Omnigent runners; only env vars survive
  into the detached subprocess. `DATABRICKS_APP_NAME` (a.k.a.
  `CODA_INSTANCE_NAME`, already read in `pat_rotator.py:35`) is present in both →
  it is the disambiguator. SSO email is not (see non-goals).
- **Repo/worktree tail stays `{project_path.name}`.** Worktrees checked out under
  distinct dir names in `~/projects/` naturally get distinct tails — so the
  worktree guidance and the path fix reinforce each other.

Proposed path:
```
/Workspace/Shared/coda/{app_name}/{project_path.name}
```
Solo/unnamed instances (no `DATABRICKS_APP_NAME`) fall back to a stable literal
(e.g. `coda/_local/{repo}`) so the formula is total.

## 5. Acceptance criteria (the bar for "proven")

- **Round-trip:** commit in repo `X` on `app_name=coda-01`; confirm the file
  lands at `/Workspace/Shared/coda/coda-01/X`; run `restore_from_workspace.py X`
  in a fresh container with `app_name=coda-01` and get the file back.
- **No cross-instance clobber:** `coda-01` and `coda-02` both sync repo `X`;
  both Workspace copies coexist.
- **ACL:** the sync identity can write `/Workspace/Shared/coda/…` in the target
  prod workspace (not just dev) — proven with a real sync, exit 0, file present.
- **Banner:** a fresh shared-CoDA terminal shows the shared-storage + worktree
  notice.

## 6. Component specs

- `spec-1-sync-path.md` — the `workspace_dest` / restore-path change + the
  shared-storage banner. (This is the whole code change.)

## 7. Open questions (carried into the spec)

- **Q1 — `/Workspace/Shared` write ACL.** Does the shared-app PAT identity (a
  low-priv SP per `project_workshop_shared_app`) have write on `/Workspace/Shared`
  in the target workspaces? If not: grant it, or fall back to
  `/Workspace/Users/{pat_identity}/coda/{app_name}/{repo}` (no ACL change, less
  honest naming). **Blocks acceptance criterion 3.**
- **Q2 — migration of existing syncs.** Prior syncs live at the old
  `/Workspace/Users/{email}/projects/{repo}` path. Do we migrate, or just let the
  old copies age out (ephemeral anyway)? Leaning: no migration, document the
  cutover.
- **Q3 — does the Omnigent runner even commit? — RESOLVED (2026-07-12).** Yes.
  The post-commit hook is written into the global `hooks_dir` at boot
  (`app.py:465`, *"works from any CLI"*), and all contexts share one filesystem +
  git config (`omnigents_host.py:5`). Any commit from any context — browser, SDK
  agent, Omnigent runner — fires the sync in the same request-less detached
  subprocess. **Confirms the disambiguator must be env-derived
  (`DATABRICKS_APP_NAME`); SSO email is unreachable there.** No longer open.
