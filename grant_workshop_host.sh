#!/bin/bash
# Convenience wrapper: grant a workshop CoDA app the two IAM permissions it
# needs to register as an Omnigent host on omnigent-<profile> (<dev-profile>).
#
#   ./grant_workshop_host.sh                 # defaults to coding-agents-01
#   ./grant_workshop_host.sh coding-agents-02
#   ./grant_workshop_host.sh coding-agents-0N
#
# Fixed for the <dev-profile> workshop fleet:
#   profile      = <dev-profile>
#   server app   = omnigent-<profile>
#   wheel volume = <dev-profile>_catalog.<profile>_omnigent.artifacts  (from app.yaml.workshop)
# The CoDA app's SP is derived from the app name by the underlying script, so
# only the app name varies across the fleet.
#
# Delegates to grant_omnigent_host.sh (idempotent — safe to re-run).
CODA_APP="${1:-coding-agents-01}"
exec "$(dirname "$0")/grant_omnigent_host.sh" \
  --profile <dev-profile> \
  --coda-app "$CODA_APP" \
  --server-app omnigent-<profile> \
  --wheel-volume <dev-profile>_catalog.<profile>_omnigent.artifacts
