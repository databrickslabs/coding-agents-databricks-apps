#!/usr/bin/env bash
# gateway_logging.sh — audit + enforce the CoDA gateway-logging policy across all
# chat serving endpoints the coding agents can hit.
#
# POLICY (Part 3 / observability + envelope):
#   - usage_tracking_config.enabled = true   on EVERY endpoint  → you see every
#     request (tokens / cost / latency / model / caller). No payloads. KEEP/ENSURE.
#   - inference_table_config.enabled = false on every endpoint  → do NOT hoard
#     full prompt+response bodies (promo/customer/pricing data) in telemetry.
#
# So: "trace all requests so you know" == usage tracking on everywhere; payloads
# off everywhere. This script makes both true.
#
# USAGE:
#   scripts/gateway_logging.sh                 # AUDIT only — prints the matrix, changes nothing
#   scripts/gateway_logging.sh --apply         # ensure usage-tracking ON + payload logging OFF on all
#
# SAFETY: touches SHARED live endpoints. Audit first, get sign-off, then --apply.
# Idempotent and re-runnable.
set -euo pipefail

# Every llm/v1/chat endpoint an agent could route to in this workspace, plus the
# external claude-opus-4-7. (Codex/Gemini app.yaml names are not served here and
# get remapped to a served Claude endpoint by in-geo discovery, so covering the
# served chat endpoints covers all agent traffic.)
ENDPOINTS=(
  databricks-claude-opus-4-8
  databricks-claude-opus-4-7
  databricks-claude-opus-4-6
  databricks-claude-sonnet-4-6
  databricks-claude-sonnet-4-5
  databricks-claude-haiku-4-5
  databricks-gpt-oss-120b
  databricks-gpt-oss-20b
  databricks-gemma-3-12b
  claude-opus-4-7
)

APPLY="${1:-}"

# Desired gateway config: usage tracking ON, payload/inference-table logging OFF.
read -r -d '' BODY <<'JSON' || true
{
  "usage_tracking_config": { "enabled": true },
  "inference_table_config": { "enabled": false }
}
JSON

state() {  # $1=endpoint → prints "<usage> <payload>"
  databricks serving-endpoints get "$1" --output json 2>/dev/null \
    | python3 -c 'import sys,json; g=(json.load(sys.stdin).get("ai_gateway") or {}); print(g.get("usage_tracking_config",{}).get("enabled"), g.get("inference_table_config",{}).get("enabled"))' \
    2>/dev/null || echo "ERR ERR"
}

printf '%-32s %-8s %-9s\n' "endpoint" "usage" "payload"
printf -- '-%.0s' {1..52}; echo
NEED_FIX=()
for ep in "${ENDPOINTS[@]}"; do
  read -r usage payload <<<"$(state "$ep")"
  printf '%-32s %-8s %-9s\n' "$ep" "$usage" "$payload"
  # needs a fix if usage isn't True, or payload logging is on (True)
  if [ "$usage" != "True" ] || [ "$payload" = "True" ]; then
    NEED_FIX+=("$ep")
  fi
done
echo
if [ ${#NEED_FIX[@]} -eq 0 ]; then
  echo "✓ Policy already satisfied on all endpoints (usage ON, payload OFF). Nothing to do."
  exit 0
fi
echo "Endpoints needing a change (enable usage / disable payload): ${NEED_FIX[*]}"

if [ "${APPLY}" != "--apply" ]; then
  echo
  echo "DRY RUN — no change applied. Re-run with --apply to enforce the policy."
  echo "Would PUT to each of the above:"
  echo "${BODY}"
  exit 0
fi

echo
for ep in "${NEED_FIX[@]}"; do
  echo "== Applying to ${ep} =="
  databricks api put "/api/2.0/serving-endpoints/${ep}/ai-gateway" --json "${BODY}" >/dev/null
  read -r usage payload <<<"$(state "$ep")"
  printf '  now: usage=%s payload=%s\n' "$usage" "$payload"
  if [ "$usage" != "True" ] || [ "$payload" = "True" ]; then
    echo "  ⚠ ${ep} did not reach desired state — check manually."
  fi
done
echo "✓ Done. Usage tracking on, payload logging off across all agent endpoints."
