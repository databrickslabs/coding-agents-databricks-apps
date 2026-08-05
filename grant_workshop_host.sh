#!/bin/bash
# Convenience wrapper for granting a workshop CoDA app the two IAM permissions
# required to register as an Omnigent host. All deployment-specific values must
# be supplied by the operator; none are committed to this public repository.
#
# Required environment variables: PROFILE, SERVER_APP, WHEEL_VOLUME
# Optional first argument: the CoDA app name (defaults to coding-agents-01).
set -euo pipefail
: "${PROFILE:?Set PROFILE to the deployment's Databricks CLI profile}"
: "${SERVER_APP:?Set SERVER_APP to the Omnigent server app name}"
: "${WHEEL_VOLUME:?Set WHEEL_VOLUME to the UC volume path}"
CODA_APP="${1:-coding-agents-01}"
exec "$(dirname "$0")/grant_omnigent_host.sh" \
  --profile "$PROFILE" \
  --coda-app "$CODA_APP" \
  --server-app "$SERVER_APP" \
  --wheel-volume "$WHEEL_VOLUME"
