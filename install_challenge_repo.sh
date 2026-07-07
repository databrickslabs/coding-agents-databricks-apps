#!/bin/bash
# Preload the workshop challenge repo into ~/projects/ (spec A-R7).
#
# Reads CHALLENGE_REPO_URL (public https clone URL) and CHALLENGE_REPO_REF
# (pinned commit sha or tag) from the environment — set in app.yaml.workshop.
# - No-op when CHALLENGE_REPO_URL is unset or a placeholder (non-workshop deploys).
# - Skips if the clone already exists, so attendee work survives app restarts.
# - Pins the default branch to CHALLENGE_REPO_REF so every instance starts
#   from an identical state (no detached HEAD — friendlier for attendees).

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

git clone "$CHALLENGE_REPO_URL" "$REPO_DIR"

if [ -n "${CHALLENGE_REPO_REF:-}" ]; then
  git -C "$REPO_DIR" reset --hard "$CHALLENGE_REPO_REF"
fi

echo "Challenge repo ready at $REPO_DIR @ ${CHALLENGE_REPO_REF:-default branch}"
