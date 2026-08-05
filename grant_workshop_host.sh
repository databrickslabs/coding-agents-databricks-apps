#!/bin/bash
# Convenience wrapper: grant a workshop CoDA app the two IAM permissions it
# needs to register as an Omnigent host on omnigent-daveok (lakemeter).
#
#   ./grant_workshop_host.sh                 # defaults to coding-agents-01
#   ./grant_workshop_host.sh coding-agents-02
#   ./grant_workshop_host.sh coding-agents-0N
#
# Fixed for the lakemeter workshop fleet:
#   profile      = lakemeter
#   server app   = omnigent-daveok
#   wheel volume = lakemeter_catalog.daveok_omnigent.artifacts  (from app.yaml.workshop)
# The CoDA app's SP is derived from the app name by the underlying script, so
# only the app name varies across the fleet.
#
# Delegates to grant_omnigent_host.sh (idempotent — safe to re-run).
CODA_APP="${1:-coding-agents-01}"
exec "$(dirname "$0")/grant_omnigent_host.sh" \
  --profile lakemeter \
  --coda-app "$CODA_APP" \
  --server-app omnigent-daveok \
  --wheel-volume lakemeter_catalog.daveok_omnigent.artifacts
