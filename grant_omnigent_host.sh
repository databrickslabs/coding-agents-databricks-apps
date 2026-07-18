#!/bin/bash
# Grant a CoDA (coding-agents) app the two IAM permissions it needs to register
# as an Omnigent HOST on a managed Omnigent server.
#
# For a deployed CoDA app to appear as a selectable host in the Omnigent picker,
# its container must successfully run `omnigent host <server>`. That requires the
# app's service principal to have BOTH:
#   1. CAN_USE on the Omnigent server app — or the host tunnel (WebSocket
#      upgrade) is rejected and the host never registers.
#   2. UC access to the wheel volume = the FULL traversal chain:
#        a. USE_CATALOG on the catalog
#        b. USE_SCHEMA  on the schema
#        c. READ_VOLUME + WRITE_VOLUME on the volume
#      READ_VOLUME alone is NOT enough: without USE_CATALOG/USE_SCHEMA the SP
#      cannot traverse to the volume and the wheel download fails with
#      "User does not have USE CATALOG on Catalog '<cat>'", so the omnigent
#      CLI never installs and the host never registers. (Learned the hard way.)
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
# Example (lakemeter / AWS FE-VM):
#   ./grant_omnigent_host.sh \
#       --profile lakemeter \
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

# ---- Grant 2a/2b: UC traversal (USE_CATALOG on catalog, USE_SCHEMA on schema)
# READ_VOLUME alone is NOT enough: Unity Catalog requires the full traversal
# chain to reach a volume. WHEEL_VOLUME is <catalog>.<schema>.<volume>; split it
# to target the parents.
CATALOG="${WHEEL_VOLUME%%.*}"
SCHEMA_FQ="${WHEEL_VOLUME%.*}"   # <catalog>.<schema>

grant_use() {  # $1=securable-type (catalog|schema)  $2=fq-name  $3=privilege
  local kind="$1" name="$2" priv="$3" have
  have=$("${DBX[@]}" grants get "$kind" "$name" --output json 2>/dev/null \
    | CODA_SP="$CODA_SP" PRIV="$priv" python3 -c "
import os,sys,json
d=json.load(sys.stdin); sp=os.environ['CODA_SP']; priv=os.environ['PRIV']
have=set()
for a in d.get('privilege_assignments',[]):
    if sp in a.get('principal',''):
        have.update(a.get('privileges',[]))
print('yes' if (priv in have or 'ALL_PRIVILEGES' in have) else 'no')")
  if [[ "$have" == "yes" ]]; then
    echo "    $kind '$name': $priv already granted — skipping"
  else
    "${DBX[@]}" grants update "$kind" "$name" \
      --json "{\"changes\":[{\"principal\":\"$CODA_SP\",\"add\":[\"$priv\"]}]}" >/dev/null
    echo "    $kind '$name': granted $priv"
  fi
}

echo "==> Grant 2a: USE_CATALOG on catalog '$CATALOG'"
grant_use catalog "$CATALOG" USE_CATALOG
echo "==> Grant 2b: USE_SCHEMA on schema '$SCHEMA_FQ'"
grant_use schema "$SCHEMA_FQ" USE_SCHEMA

# ---- Grant 2c: READ_VOLUME + WRITE_VOLUME on the wheel volume --------------
echo "==> Grant 2c: READ_VOLUME + WRITE_VOLUME on volume '$WHEEL_VOLUME'"
MISSING=$("${DBX[@]}" grants get volume "$WHEEL_VOLUME" --output json 2>/dev/null \
  | CODA_SP="$CODA_SP" python3 -c "
import os,sys,json
d=json.load(sys.stdin); sp=os.environ['CODA_SP']
have=set()
for a in d.get('privilege_assignments',[]):
    if sp in a.get('principal',''):
        have.update(a.get('privileges',[]))
missing=[p for p in ('READ_VOLUME','WRITE_VOLUME') if p not in have]
print(','.join(missing))")
if [[ -z "$MISSING" ]]; then
  echo "    already granted — skipping"
else
  ADD_JSON=$(CODA_SP="$CODA_SP" MISSING="$MISSING" python3 -c "
import os,json
print(json.dumps({'changes':[{'principal':os.environ['CODA_SP'],'add':os.environ['MISSING'].split(',')}]}))")
  "${DBX[@]}" grants update volume "$WHEEL_VOLUME" --json "$ADD_JSON" >/dev/null
  echo "    granted $MISSING"
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
have=set()
for a in d.get('privilege_assignments',[]):
    if sp in a.get('principal',''):
        have.update(a.get('privileges',[]))
print('READ_VOLUME=%s WRITE_VOLUME=%s' % (
    'yes' if 'READ_VOLUME' in have else 'NO',
    'yes' if 'WRITE_VOLUME' in have else 'NO'))")
FINAL_TRAVERSE=$("${DBX[@]}" grants get catalog "$CATALOG" --output json 2>/dev/null \
  | CODA_SP="$CODA_SP" python3 -c "
import os,sys,json
d=json.load(sys.stdin); sp=os.environ['CODA_SP']
have=set()
for a in d.get('privilege_assignments',[]):
    if sp in a.get('principal',''):
        have.update(a.get('privileges',[]))
print('USE_CATALOG=yes' if ('USE_CATALOG' in have or 'ALL_PRIVILEGES' in have) else 'USE_CATALOG=NO')")
echo "    $FINAL_USE  $FINAL_TRAVERSE  $FINAL_READ"

if [[ "$FINAL_USE" == *=yes && "$FINAL_TRAVERSE" == *"USE_CATALOG=yes"* \
      && "$FINAL_READ" == *"READ_VOLUME=yes"* && "$FINAL_READ" == *"WRITE_VOLUME=yes"* ]]; then
  echo "==> Done. '$CODA_APP' can now register as a host on '$SERVER_APP'."
  echo "    Trigger the connect from the CoDA app UI/API, then it should appear in the Omnigent picker."
else
  echo "ERROR: one or both grants did not verify — see above." >&2
  exit 1
fi
