#!/usr/bin/env bash
# fix_opus48_gateway.sh — disable sensitive PAYLOAD logging on the opus-4-8
# serving endpoint while KEEPING usage/cost tracking on.
#
# WHY (Part 3 / policy):
#   The databricks-claude-opus-4-8 endpoint has AI Gateway enabled with BOTH:
#     - usage_tracking_config.enabled = true   (cost/token analytics, no payloads) → KEEP
#     - inference_table_config.enabled = true  (full request+response payloads)    → DISABLE
#   opus-4-8 backs Claude Code, Pi and Hermes, so the payload table
#   (edp_aisandbox_aisandbox_dev.ppcs.all_anthropic-opus-4-8_payload) is armed to
#   capture every attendee's prompts + responses — including the promo/customer/
#   pricing data the PPCS operating envelope forbids putting into telemetry.
#   (At audit time the table was still 0 rows — this disables it BEFORE it fills.)
#
# WHAT IT DOES:
#   PUTs an ai_gateway config that sets inference_table_config.enabled=false and
#   leaves usage_tracking_config.enabled=true. Nothing else changes.
#
# USAGE:
#   scripts/fix_opus48_gateway.sh            # DRY RUN — prints the PUT body, changes nothing
#   scripts/fix_opus48_gateway.sh --apply    # applies the change to the live endpoint
#
# SAFETY: this touches a SHARED live endpoint other attendees use. Dry-run first,
# get sign-off, then --apply. Re-runnable (idempotent).
set -euo pipefail

ENDPOINT="databricks-claude-opus-4-8"
APPLY="${1:-}"

# Desired ai_gateway: usage tracking ON, payload/inference-table logging OFF.
read -r -d '' BODY <<'JSON' || true
{
  "usage_tracking_config": { "enabled": true },
  "inference_table_config": { "enabled": false }
}
JSON

echo "Endpoint: ${ENDPOINT}"
echo "Target ai_gateway config:"
echo "${BODY}"
echo

echo "== Current ai_gateway state =="
databricks serving-endpoints get "${ENDPOINT}" --output json \
  | python3 -c 'import sys,json; print(json.dumps(json.load(sys.stdin).get("ai_gateway"), indent=2))'
echo

if [ "${APPLY}" != "--apply" ]; then
  echo "DRY RUN — no change applied. Re-run with --apply to disable payload logging."
  exit 0
fi

echo "== Applying (PUT ai-gateway) =="
# The dedicated ai-gateway PUT endpoint updates gateway config without touching
# served entities / traffic config.
databricks api put "/api/2.0/serving-endpoints/${ENDPOINT}/ai-gateway" --json "${BODY}"

echo
echo "== Verify: inference_table should now be enabled=false, usage_tracking enabled=true =="
databricks serving-endpoints get "${ENDPOINT}" --output json \
  | python3 -c 'import sys,json; g=json.load(sys.stdin).get("ai_gateway",{}); print(json.dumps(g, indent=2)); import sys as s; s.exit(0 if (g.get("inference_table_config",{}).get("enabled") is False and g.get("usage_tracking_config",{}).get("enabled") is True) else 1)'
echo "✓ Payload logging disabled, usage tracking retained."
