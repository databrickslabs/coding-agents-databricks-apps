#!/bin/bash
# Grant a CoDA app's service principal CAN_USE on the MLflow OSS tracking app
# (spec-B B-R5). Databricks Apps reject PATs/user bearers (302 -> OIDC login) and
# accept only an SP OAuth (M2M) token from a CAN_USE-granted caller — so without
# this grant, the CoDA agents' MLflow client cannot reach the OSS app URL.
# Verified live 2026-07-11: user bearer -> 302/hang; CAN_USE + SP M2M token is the
# accepted path (same mechanism as grant_omnigent_host.sh).
#
# A Databricks App runs as its own SP with ZERO ambient privileges, so this grant
# is explicit and one-time (persists across redeploys). Idempotent — re-running
# checks current state and grants only if missing.
#
# The CoDA app's SP is DERIVED from --coda-app via `databricks apps get`, so this
# is portable across workspaces and never hardcodes an SP id.
#
# Usage:
#   ./grant_mlflow_host.sh --profile daveok --coda-app coda --mlflow-app coda-mlflow-oss
set -euo pipefail

PROFILE="" CODA_APP="" MLFLOW_APP=""
usage() { echo "usage: $0 --profile P --coda-app CODA --mlflow-app MLFLOW" >&2; exit "${1:-1}"; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)     PROFILE="$2"; shift 2 ;;
    --coda-app)    CODA_APP="$2"; shift 2 ;;
    --mlflow-app)  MLFLOW_APP="$2"; shift 2 ;;
    -h|--help)     usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
done
for req in PROFILE CODA_APP MLFLOW_APP; do
  if [[ -z "${!req}" ]]; then
    echo "ERROR: --$(echo "$req" | tr 'A-Z_' 'a-z-') is required" >&2; usage 1
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

echo "==> Grant: CAN_USE on MLflow OSS app '$MLFLOW_APP'"
HAS_USE=$("${DBX[@]}" apps get-permissions "$MLFLOW_APP" --output json 2>/dev/null \
  | CODA_SP="$CODA_SP" python3 -c "
import sys, json, os
d = json.load(sys.stdin); sp = os.environ['CODA_SP']
print('yes' if any(
    sp in str(a) and any(p.get('permission_level')=='CAN_USE' for p in a.get('all_permissions',[]))
    for a in d.get('access_control_list',[])
) else 'no')")
if [[ "$HAS_USE" == "yes" ]]; then
  echo "    already granted — skipping"
else
  "${DBX[@]}" apps update-permissions "$MLFLOW_APP" \
    --json "{\"access_control_list\":[{\"service_principal_name\":\"$CODA_SP\",\"permission_level\":\"CAN_USE\"}]}" >/dev/null
  echo "    granted CAN_USE"
fi

echo "==> Done. '$CODA_APP' SP can now present an M2M OAuth token to '$MLFLOW_APP'."
echo "    Set on the CoDA app: MLFLOW_OSS_TRACKING_ENABLED=true and"
echo "    MLFLOW_OSS_URL=<the $MLFLOW_APP app URL>, then redeploy."
