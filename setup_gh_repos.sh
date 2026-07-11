#!/bin/bash
# setup_gh_repos.sh — one-shot helper to get GitHub repos ready under ~/projects/.
#
# Run this manually inside a CoDA container to:
#   1. Ensure `gh` is authenticated (github.com)
#   2. Wire gh's credentials into git (`gh auth setup-git`) so plain
#      `git clone/pull/push` over HTTPS works without prompts
#   3. Clone a set of repos into ~/projects/ (idempotent — skips existing clones)
#
# By default it clones the repos in DEFAULT_REPOS below (the ones we keep pulled
# down in CoDA). Override the selection with positional args or CODA_CLONE_REPOS.
#
# NOT wired into app.py startup — invoke it yourself when you need the repos:
#     bash setup_gh_repos.sh                                    # default set
#     bash setup_gh_repos.sh dgokeeffe/some-other-repo         # explicit list
#     CODA_CLONE_REPOS="owner/a owner/b" bash setup_gh_repos.sh # via env var
#
# Auth precedence (first that applies wins):
#   - Already logged in       -> reused as-is (no re-login)
#   - GH_TOKEN / GITHUB_TOKEN -> `gh auth login --with-token`
#   - Otherwise               -> interactive `gh auth login` (web/device flow)
#
# Safe to re-run: auth is skipped when already logged in, and clones are
# skipped when the target dir already has a .git (so local work is preserved).

set -euo pipefail

# --- Config ---------------------------------------------------------------
PROJECTS_DIR="${HOME}/projects"

# Default repo list — the repos we currently keep pulled down in CoDA.
# Override by passing repos as args, or via CODA_CLONE_REPOS.
DEFAULT_REPOS=(
  "<private-repo>"
  "<private-mirror>"
  "<private-repo>"
  "<private-repo>"
  "<private-repo>"
)

# Positional args win; then CODA_CLONE_REPOS; then the built-in default.
if [ "$#" -gt 0 ]; then
  REPOS=("$@")
elif [ -n "${CODA_CLONE_REPOS:-}" ]; then
  # shellcheck disable=SC2206  # intentional word-split on the space-separated list
  REPOS=(${CODA_CLONE_REPOS})
else
  REPOS=("${DEFAULT_REPOS[@]}")
fi

# --- Preconditions --------------------------------------------------------
if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: gh (GitHub CLI) not found on PATH." >&2
  echo "       In CoDA it is installed by install_gh.sh at startup; run that first." >&2
  exit 1
fi

mkdir -p "$PROJECTS_DIR"

# --- 1. Authenticate gh ---------------------------------------------------
if gh auth status >/dev/null 2>&1; then
  echo "gh: already authenticated as $(gh api user --jq .login 2>/dev/null || echo '<unknown>') — reusing existing login"
elif [ -n "${GH_TOKEN:-}${GITHUB_TOKEN:-}" ]; then
  echo "gh: authenticating with token from GH_TOKEN/GITHUB_TOKEN"
  # gh reads the token from stdin; prefer GH_TOKEN, fall back to GITHUB_TOKEN.
  printf '%s' "${GH_TOKEN:-${GITHUB_TOKEN}}" \
    | gh auth login --hostname github.com --git-protocol https --with-token
  echo "gh: logged in as $(gh api user --jq .login 2>/dev/null || echo '<unknown>')"
else
  echo "gh: not authenticated and no GH_TOKEN/GITHUB_TOKEN set — starting interactive login"
  gh auth login --hostname github.com --git-protocol https
fi

# --- 2. Wire gh credentials into git -------------------------------------
# Makes `git` use gh as a credential helper for github.com over HTTPS, so
# bare git clone/pull/push work without prompting.
echo "gh: configuring git credential helper (gh auth setup-git)"
gh auth setup-git --hostname github.com

# --- 3. Clone the repos ---------------------------------------------------
had_error=0
for repo in "${REPOS[@]}"; do
  name="$(basename "$repo")"
  dest="$PROJECTS_DIR/$name"

  if [ -d "$dest/.git" ]; then
    echo "skip: $repo already present at $dest ($(git -C "$dest" rev-parse --short HEAD 2>/dev/null || echo '?')) — leaving as-is"
    continue
  fi

  echo "clone: $repo -> $dest"
  if gh repo clone "$repo" "$dest"; then
    echo "  ready at $dest @ $(git -C "$dest" rev-parse --short HEAD 2>/dev/null || echo '?')"
  else
    echo "  ERROR: failed to clone $repo" >&2
    had_error=1
  fi
done

echo
if [ "$had_error" -eq 0 ]; then
  echo "Done. Repos available under $PROJECTS_DIR/"
else
  echo "Done with errors — see messages above." >&2
  exit 1
fi
