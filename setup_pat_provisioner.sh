#!/bin/bash
# setup_pat_provisioner.sh — create (or reuse) a dedicated service principal
# that provision_coda_pats.sh uses to inject PATs into the CoDA fleet.
#
# WHY a dedicated SP: /api/inject-pat sits behind the Databricks Apps edge,
# which authenticates an OAuth bearer BEFORE the request reaches the Flask app
# (a PAT bearer 401s there). So the provisioner needs OAuth (M2M) creds plus:
#   - CAN_USE on each target app        (to pass the app edge)
#   - the `workspace-access` entitlement (to call `apps get` / management APIs)
#   - CAN_USE on tokens                  (to mint the per-app bootstrap PATs)
#
# This script sets all of that up idempotently and writes a `[coda-provisioner]`
# M2M profile to ~/.databrickscfg so you can immediately run:
#
#   ./provision_coda_pats.sh --profile coda-provisioner --app-prefix coda- \
#       --secret "$CODA_BOOTSTRAP_SECRET"
#
# Requires: you run this as a workspace admin (SCIM + app perms + token ACL).
#
# Idempotent: reuses an existing SP with the same --display-name, re-grants only
# what's missing, and skips the profile write if it's already present. It always
# MINTS A NEW OAuth secret (the API returns the value only once), so re-running
# rotates the client_secret in the profile.
#
# Examples:
#   ./setup_pat_provisioner.sh --profile admin --app-prefix coda-
#   ./setup_pat_provisioner.sh --profile admin --apps coda-04,coda-05
#   ./setup_pat_provisioner.sh --profile admin --app-prefix coda- --name my-provisioner

set -euo pipefail

PROFILE=""
NAME="coda-pat-provisioner"
PROFILE_OUT="coda-provisioner"
APPS=""
APP_PREFIX=""

usage() { sed -n '2,34p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)      PROFILE="$2"; shift 2 ;;
    --name)         NAME="$2"; shift 2 ;;
    --profile-out)  PROFILE_OUT="$2"; shift 2 ;;
    --apps)         APPS="$2"; shift 2 ;;
    --app-prefix)   APP_PREFIX="$2"; shift 2 ;;
    -h|--help)      usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
done

if [[ -z "$APPS" && -z "$APP_PREFIX" ]]; then
  echo "ERROR: provide --apps <a,b,c> or --app-prefix <prefix>." >&2
  usage 1
fi

DBX=(databricks)
[[ -n "$PROFILE" ]] && DBX+=(--profile "$PROFILE")
command -v databricks >/dev/null 2>&1 || { echo "ERROR: databricks CLI not found." >&2; exit 1; }
command -v python3   >/dev/null 2>&1 || { echo "ERROR: python3 not found." >&2; exit 1; }

HOST=$("${DBX[@]}" auth env 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('env',{}).get('DATABRICKS_HOST',''))" 2>/dev/null || true)
HOST="${HOST%/}"
[[ -z "$HOST" ]] && { echo "ERROR: could not resolve workspace host from profile '${PROFILE:-DEFAULT}'." >&2; exit 1; }
echo "==> Workspace host: $HOST"

# --- 1. Create or reuse the SP --------------------------------------------
echo "==> Ensuring service principal '$NAME' exists..."
SP_JSON=$("${DBX[@]}" service-principals list --output json 2>/dev/null \
  | NAME="$NAME" python3 -c "
import os,sys,json
d=json.load(sys.stdin); nm=os.environ['NAME']
sps=d if isinstance(d,list) else d.get('Resources',d.get('resources',[]))
for s in sps:
    if s.get('displayName')==nm:
        print(json.dumps({'id':s.get('id'),'app':s.get('applicationId')})); break")
if [[ -n "$SP_JSON" ]]; then
  SP_ID=$(printf '%s' "$SP_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
  SP_APP=$(printf '%s' "$SP_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['app'])")
  echo "    reusing existing SP (id=$SP_ID app=$SP_APP)"
else
  CREATE=$("${DBX[@]}" service-principals create --display-name "$NAME" --active --output json 2>&1)
  SP_ID=$(printf '%s' "$CREATE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))" 2>/dev/null || true)
  SP_APP=$(printf '%s' "$CREATE" | python3 -c "import sys,json; print(json.load(sys.stdin).get('applicationId',''))" 2>/dev/null || true)
  [[ -z "$SP_ID" || -z "$SP_APP" ]] && { echo "ERROR: SP create failed: $CREATE" >&2; exit 1; }
  echo "    created SP (id=$SP_ID app=$SP_APP)"
fi

# --- 2. Entitlement: workspace-access (for management APIs like `apps get`) -
echo "==> Ensuring 'workspace-access' entitlement..."
"${DBX[@]}" service-principals patch "$SP_ID" \
  --json '{"Operations":[{"op":"add","path":"entitlements","value":[{"value":"workspace-access"}]}],"schemas":["urn:ietf:params:scim:api:messages:2.0:PatchOp"]}' \
  >/dev/null 2>&1 || true
HAS=$("${DBX[@]}" service-principals get "$SP_ID" --output json 2>/dev/null \
  | python3 -c "import sys,json; print('yes' if any(e.get('value')=='workspace-access' for e in json.load(sys.stdin).get('entitlements',[])) else 'no')")
echo "    workspace-access: $HAS"

# --- 3. Token ACL: CAN_USE on tokens (to mint per-app bootstrap PATs) -------
echo "==> Granting CAN_USE on tokens..."
"${DBX[@]}" api patch /api/2.0/permissions/authorization/tokens \
  --json "{\"access_control_list\":[{\"service_principal_name\":\"$SP_APP\",\"permission_level\":\"CAN_USE\"}]}" >/dev/null 2>&1 \
  && echo "    token CAN_USE granted" || echo "    token grant may have failed (check admin rights)"

# --- 4. Build the target app list and grant CAN_USE on each -----------------
declare -a APP_NAMES=()
if [[ -n "$APPS" ]]; then
  IFS=',' read -r -a APP_NAMES <<< "$APPS"
else
  mapfile -t APP_NAMES < <("${DBX[@]}" apps list --output json \
    | APP_PREFIX="$APP_PREFIX" python3 -c "
import os,sys,json
pfx=os.environ['APP_PREFIX']; data=json.load(sys.stdin)
apps=data if isinstance(data,list) else data.get('apps',[])
[print(a.get('name','')) for a in apps if a.get('name','').startswith(pfx)]")
fi
[[ "${#APP_NAMES[@]}" -eq 0 ]] && { echo "ERROR: no target apps resolved." >&2; exit 1; }

echo "==> Granting CAN_USE on ${#APP_NAMES[@]} app(s): ${APP_NAMES[*]}"
for app in "${APP_NAMES[@]}"; do
  ok=$("${DBX[@]}" apps update-permissions "$app" \
    --json "{\"access_control_list\":[{\"service_principal_name\":\"$SP_APP\",\"permission_level\":\"CAN_USE\"}]}" \
    --output json 2>/dev/null \
    | SP="$SP_APP" python3 -c "import os,sys,json; d=json.load(sys.stdin); print('OK' if any(os.environ['SP'] in str(a) for a in d.get('access_control_list',[])) else 'NOACL')" 2>/dev/null || echo "ERR")
  echo "    $app -> $ok"
done

# --- 5. Mint an OAuth secret and write the M2M profile ----------------------
# Skip minting when the profile already exists — the create API always returns
# a NEW secret (value shown once), so re-minting would orphan an OAuth secret
# on the SP without updating the profile. Only mint when we'll actually write.
CFG="${HOME}/.databrickscfg"
if grep -q "^\[$PROFILE_OUT\]" "$CFG" 2>/dev/null; then
  echo "==> Profile '[$PROFILE_OUT]' already exists in $CFG — NOT overwriting."
  echo "    Skipping secret mint (delete the profile block + re-run to rotate)."
else
  echo "==> Minting OAuth secret for the provisioner SP..."
  SECRET=$("${DBX[@]}" service-principal-secrets-proxy create "$SP_ID" --output json 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('secret',''))" 2>/dev/null || true)
  [[ -z "$SECRET" ]] && { echo "ERROR: could not mint SP OAuth secret." >&2; exit 1; }
  {
    printf '\n[%s]\n' "$PROFILE_OUT"
    printf 'host = %s\n' "$HOST"
    printf 'client_id = %s\n' "$SP_APP"
    printf 'client_secret = %s\n' "$SECRET"
    printf 'auth_type = oauth-m2m\n'
  } >> "$CFG"
  chmod 600 "$CFG"
  echo "==> Wrote M2M profile '[$PROFILE_OUT]' to $CFG"
fi

echo
echo "Done. Provisioner SP ready: app_id=$SP_APP"
echo "Next:"
echo "  ./provision_coda_pats.sh --profile $PROFILE_OUT --app-prefix ${APP_PREFIX:-<prefix>} --secret \"\$CODA_BOOTSTRAP_SECRET\""
