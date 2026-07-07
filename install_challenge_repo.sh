#!/bin/bash
# Preload the workshop challenge repo into ~/projects/ (spec A-R7).
#
# Runs at container startup (initialize_app). Reads from the environment
# (set in app.yaml.workshop):
#   CHALLENGE_REPO_URL        — https clone URL of the (private) challenge repo
#   CHALLENGE_REPO_READ_TOKEN — repo-scoped read token, injected via app.yaml
#                               `valueFrom` a Databricks secret; optional for
#                               public repos
# - No-op when CHALLENGE_REPO_URL is unset or a placeholder (non-workshop deploys).
# - Skips if the clone already exists, so attendee work survives app restarts.
# - Clones the default (main) branch — NOT pinned; instances track the repo.
# - The token is used only for the clone via a transient credential helper;
#   it never lands in the remote URL or .git/config, so attendee pushes go
#   through their own gh identity.

set -euo pipefail

if [ -z "${CHALLENGE_REPO_URL:-}" ] || [[ "${CHALLENGE_REPO_URL}" == *"FILL-IN"* ]]; then
  echo "CHALLENGE_REPO_URL not set — skipping challenge repo preload"
  exit 0
fi

PROJECTS_DIR="$HOME/projects"
mkdir -p "$PROJECTS_DIR"

REPO_NAME=$(basename "${CHALLENGE_REPO_URL%/}" .git)
REPO_DIR="$PROJECTS_DIR/$REPO_NAME"

if [ -d "$REPO_DIR/.git" ]; then
  echo "Challenge repo already present at $REPO_DIR — skipping (attendee work preserved)"
  exit 0
fi

if [ -n "${CHALLENGE_REPO_READ_TOKEN:-}" ]; then
  # First -c (empty) clears any system/global credential helpers so only ours
  # answers. The helper reads the token from the inherited environment at
  # fetch time — single quotes are deliberate so nothing is expanded into the
  # git config. GIT_TERMINAL_PROMPT=0 makes a bad token fail loudly instead
  # of wedging the setup step on a prompt.
  GIT_TERMINAL_PROMPT=0 git clone \
    -c credential.helper= \
    -c credential.helper='!f() { echo "username=x-access-token"; echo "password=${CHALLENGE_REPO_READ_TOKEN}"; }; f' \
    "$CHALLENGE_REPO_URL" "$REPO_DIR"
  # `git clone -c` PERSISTS the settings into the new repo's .git/config —
  # remove them so attendee pushes use their own (gh) credential helper.
  git -C "$REPO_DIR" config --remove-section credential
else
  GIT_TERMINAL_PROMPT=0 git clone "$CHALLENGE_REPO_URL" "$REPO_DIR"
fi

echo "Challenge repo ready at $REPO_DIR @ $(git -C "$REPO_DIR" rev-parse --short HEAD) (default branch)"
