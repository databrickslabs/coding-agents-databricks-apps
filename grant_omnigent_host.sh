#!/bin/bash
# Grant a CoDA (coding-agents) app the two IAM permissions it needs to register
# as an Omnigent HOST on a managed Omnigent server.
#
# For a deployed CoDA app to appear as a selectable host in the Omnigent picker,
# its container must successfully run `omnigent host <server>`. That requires the
# app's service principal to have BOTH:
#   1. CAN_USE on the Omnigent server app  — or the host tunnel (WebSocket
#      upgrade) is rejected and the host never registers.
#   2. READ_VOLUME on the wheel volume      — or the container can't download the
#      `omnigent` CLI wheel (OMNIGENTS_WHEEL_SPEC) and can't even dial.
#
# A Databricks App runs as its own service principal with ZERO ambient UC/app
# privileges, so both grants are explicit and one-time (persist across redeploys).
#
# The CoDA app's SP is DERIVED from --coda-app via `databricks apps get`, so this
# script is portable across workspaces and never hardcodes an SP id.
#
# Idempotent: re-running is safe — it checks current state, grants only what's
# missing, then re-reads to verify.
#
# This script ISSUES IAM GRANTS. Review the args before running.
#
# Example (<dev-profile> / AWS FE-VM):
#   ./grant_omnigent_host.sh \
#       --profile <dev-profile> \
#       --coda-app coding-agents \
#       --server-app <your-omnigent-app> \
#       --wheel-volume <catalog>.<schema>.artifacts

set -euo pipefail

PROFILE=""
CODA_APP=""
SERVER_APP=""
WHEEL_VOLUME=""

usage() {
  sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)      PROFILE="$2"; shift 2 ;;
    --coda-app)     CODA_APP="$2"; shift 2 ;;
    --server-app)   SERVER_APP="$2"; shift 2 ;;
    --wheel-volume) WHEEL_VOLUME="$2"; shift 2 ;;
    -h|--help)      usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
done

for req in PROFILE CODA_APP SERVER_APP WHEEL_VOLUME; do
  if [[ -z "${!req}" ]]; then
    echo "ERROR: --$(echo "$req" | tr 'A-Z_' 'a-z-') is required" >&2
    usage 1
  fi
done

DBX=(databricks --profile "$PROFILE")

echo "==> Resolving CoDA app service principal for '$CODA_APP'..."
CODA_SP=$("${DBX[@]}" apps get "$CODA_APP" --output json \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('service_principal_client_id',''))")
if [[ -z "$CODA_SP" ]]; then
  echo "ERROR: could not resolve service_principal_client_id for app '$CODA_APP'." >&2
  echo "       Is the app deployed on profile '$PROFILE'?" >&2
  exit 1
fi
echo "    CoDA SP: $CODA_SP"

# ---- Grant 1: CAN_USE on the Omnigent server app --------------------------
echo "==> Grant 1: CAN_USE on server app '$SERVER_APP'"
HAS_USE=$("${DBX[@]}" apps get-permissions "$SERVER_APP" --output json 2>/dev/null \
  | CODA_SP="$CODA_SP" python3 -c "
import os,sys,json
d=json.load(sys.stdin); sp=os.environ['CODA_SP']
print('yes' if any(sp in str(a) and any(p.get('permission_level')=='CAN_USE' for p in a.get('all_permissions',[])) for a in d.get('access_control_list',[])) else 'no')")
if [[ "$HAS_USE" == "yes" ]]; then
  echo "    already granted — skipping"
else
  "${DBX[@]}" apps update-permissions "$SERVER_APP" \
    --json "{\"access_control_list\":[{\"service_principal_name\":\"$CODA_SP\",\"permission_level\":\"CAN_USE\"}]}" >/dev/null
  echo "    granted CAN_USE"
fi

# ---- Grant 2: READ_VOLUME on the wheel volume -----------------------------
echo "==> Grant 2: READ_VOLUME on volume '$WHEEL_VOLUME'"
HAS_READ=$("${DBX[@]}" grants get volume "$WHEEL_VOLUME" --output json 2>/dev/null \
  | CODA_SP="$CODA_SP" python3 -c "
import os,sys,json
d=json.load(sys.stdin); sp=os.environ['CODA_SP']
print('yes' if any(sp in a.get('principal','') and 'READ_VOLUME' in a.get('privileges',[]) for a in d.get('privilege_assignments',[])) else 'no')")
if [[ "$HAS_READ" == "yes" ]]; then
  echo "    already granted — skipping"
else
  "${DBX[@]}" grants update volume "$WHEEL_VOLUME" \
    --json "{\"changes\":[{\"principal\":\"$CODA_SP\",\"add\":[\"READ_VOLUME\"]}]}" >/dev/null
  echo "    granted READ_VOLUME"
fi

# ---- Verify ---------------------------------------------------------------
echo "==> Verifying final state..."
FINAL_USE=$("${DBX[@]}" apps get-permissions "$SERVER_APP" --output json 2>/dev/null \
  | CODA_SP="$CODA_SP" python3 -c "
import os,sys,json
d=json.load(sys.stdin); sp=os.environ['CODA_SP']
print('CAN_USE=yes' if any(sp in str(a) and any(p.get('permission_level')=='CAN_USE' for p in a.get('all_permissions',[])) for a in d.get('access_control_list',[])) else 'CAN_USE=NO')")
FINAL_READ=$("${DBX[@]}" grants get volume "$WHEEL_VOLUME" --output json 2>/dev/null \
  | CODA_SP="$CODA_SP" python3 -c "
import os,sys,json
d=json.load(sys.stdin); sp=os.environ['CODA_SP']
print('READ_VOLUME=yes' if any(sp in a.get('principal','') and 'READ_VOLUME' in a.get('privileges',[]) for a in d.get('privilege_assignments',[])) else 'READ_VOLUME=NO')")
echo "    $FINAL_USE  $FINAL_READ"

if [[ "$FINAL_USE" == *=yes && "$FINAL_READ" == *=yes ]]; then
  echo "==> Done. '$CODA_APP' can now register as a host on '$SERVER_APP'."
  echo "    Trigger the connect from the CoDA app UI/API, then it should appear in the Omnigent picker."
else
  echo "ERROR: one or both grants did not verify — see above." >&2
  exit 1
fi
