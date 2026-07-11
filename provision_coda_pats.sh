#!/bin/bash
# provision_coda_pats.sh — mint a distinct PAT per CoDA app and inject it
# programmatically via each app's /api/inject-pat endpoint.
#
# Companion to the /api/inject-pat endpoint (app.py) and per-instance PAT
# rotation tagging (pat_rotator.py). Use it to bootstrap many CoDAs in a
# workspace without pasting a PAT into each browser session:
#
#   1. Resolve each target CoDA app's URL via `databricks apps get`.
#   2. Mint a fresh short-lived PAT per app (via /api/2.0/token/create),
#      tagged so it's attributable to that CoDA.
#   3. POST it to that app's /api/inject-pat with the shared bootstrap secret.
#      The app then adopts it, mints its OWN controlled token, revokes this
#      bootstrap PAT, and starts auto-rotation tagged `coda-auto-rotated:<name>`.
#
# PRECONDITIONS on each target app (set in its app config, then redeploy):
#   - CODA_BOOTSTRAP_SECRET  — the shared secret this script must present.
#                              Unset => /api/inject-pat is 404 (endpoint disabled).
#   - CODA_INSTANCE_NAME      — (recommended) names the app for rotation tags.
#                               Defaults to the app name/URL host if unset.
#
# The minted PATs belong to whoever runs this script (the profile identity).
# For clean isolation prefer running under a dedicated identity per app, or
# accept that all CoDAs share this identity (rotation is still per-instance
# tagged, and bootstrap cleanup no longer cross-revokes — see pat_rotator.py).
#
# Idempotent-ish: if an app already has a live PAT it returns 409 and this
# script SKIPS it (does not mint a wasted token). Safe to re-run.
#
# This script MINTS PATs and SENDS them to app endpoints. Review args first.
#
# Examples:
#   # Explicit app list
#   ./provision_coda_pats.sh --profile fe-vm --secret "$SECRET" \
#       --apps coda-1,coda-2,coda-3
#
#   # All apps whose name matches a prefix (e.g. every "coda-*")
#   ./provision_coda_pats.sh --profile fe-vm --secret "$SECRET" \
#       --app-prefix coda-
#
#   # Longer-lived bootstrap PAT (default 900s / 15 min)
#   ./provision_coda_pats.sh --profile fe-vm --secret "$SECRET" \
#       --apps coda-1 --lifetime 1800
#
# The secret may also be passed via the CODA_BOOTSTRAP_SECRET env var instead
# of --secret, to keep it out of shell history / the process table:
#   CODA_BOOTSTRAP_SECRET="$SECRET" ./provision_coda_pats.sh --profile fe-vm --app-prefix coda-

set -euo pipefail

PROFILE=""
SECRET="${CODA_BOOTSTRAP_SECRET:-}"
APPS=""
APP_PREFIX=""
LIFETIME="900"

usage() {
  sed -n '2,44p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)     PROFILE="$2"; shift 2 ;;
    --secret)      SECRET="$2"; shift 2 ;;  # overrides CODA_BOOTSTRAP_SECRET env
    --apps)        APPS="$2"; shift 2 ;;
    --app-prefix)  APP_PREFIX="$2"; shift 2 ;;
    --lifetime)    LIFETIME="$2"; shift 2 ;;
    -h|--help)     usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
done

if [[ -z "$SECRET" ]]; then
  echo "ERROR: bootstrap secret required — pass --secret or set CODA_BOOTSTRAP_SECRET." >&2
  usage 1
fi
if [[ -z "$APPS" && -z "$APP_PREFIX" ]]; then
  echo "ERROR: provide --apps <a,b,c> or --app-prefix <prefix>." >&2
  usage 1
fi

DBX=(databricks)
if [[ -n "$PROFILE" ]]; then
  DBX+=(--profile "$PROFILE")
fi

command -v databricks >/dev/null 2>&1 || { echo "ERROR: databricks CLI not found." >&2; exit 1; }
command -v python3   >/dev/null 2>&1 || { echo "ERROR: python3 not found." >&2; exit 1; }
command -v curl      >/dev/null 2>&1 || { echo "ERROR: curl not found." >&2; exit 1; }

# --- Resolve the workspace host (for token/create) ------------------------
HOST=$("${DBX[@]}" auth env 2>/dev/null \
  | python3 -c "import sys,json; print(json.load(sys.stdin).get('env',{}).get('DATABRICKS_HOST',''))" 2>/dev/null || true)
if [[ -z "$HOST" ]]; then
  # Fallback: some CLI versions expose host differently; try `current-user me` host header round-trip.
  HOST=$("${DBX[@]}" api get /api/2.0/preview/scim/v2/Me >/dev/null 2>&1 && \
         "${DBX[@]}" auth describe --output json 2>/dev/null \
         | python3 -c "import sys,json; print(json.load(sys.stdin).get('details',{}).get('host',''))" 2>/dev/null || true)
fi
HOST="${HOST%/}"
if [[ -z "$HOST" ]]; then
  echo "ERROR: could not resolve workspace host from the CLI profile." >&2
  echo "       Check: databricks ${PROFILE:+--profile $PROFILE }current-user me" >&2
  exit 1
fi
echo "==> Workspace host: $HOST"

# --- Build the target app list --------------------------------------------
declare -a APP_NAMES=()
if [[ -n "$APPS" ]]; then
  IFS=',' read -r -a APP_NAMES <<< "$APPS"
else
  echo "==> Listing apps with prefix '$APP_PREFIX'..."
  mapfile -t APP_NAMES < <(
    "${DBX[@]}" apps list --output json \
      | APP_PREFIX="$APP_PREFIX" python3 -c "
import os,sys,json
pfx=os.environ['APP_PREFIX']
data=json.load(sys.stdin)
apps=data if isinstance(data,list) else data.get('apps',[])
for a in apps:
    n=a.get('name','')
    if n.startswith(pfx):
        print(n)"
  )
fi

if [[ "${#APP_NAMES[@]}" -eq 0 ]]; then
  echo "ERROR: no target apps resolved." >&2
  exit 1
fi
echo "==> Target apps (${#APP_NAMES[@]}): ${APP_NAMES[*]}"

# --- Provision each app ----------------------------------------------------
had_error=0
provisioned=0
skipped=0

for app in "${APP_NAMES[@]}"; do
  echo
  echo "==> [$app] resolving URL..."
  APP_URL=$("${DBX[@]}" apps get "$app" --output json 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('url','') or '')" 2>/dev/null || true)
  APP_URL="${APP_URL%/}"
  if [[ -z "$APP_URL" ]]; then
    echo "    ERROR: could not resolve URL for app '$app' (deployed on this profile?)." >&2
    had_error=1
    continue
  fi
  echo "    URL: $APP_URL"

  # Pre-check: skip if the app already has a live PAT (avoids minting a waste).
  STATUS_JSON=$(curl -fsS --max-time 15 "$APP_URL/api/pat-status" 2>/dev/null || true)
  ALREADY=$(printf '%s' "$STATUS_JSON" \
    | python3 -c "import sys,json
try:
    d=json.loads(sys.stdin.read() or '{}')
except Exception:
    d={}
print('yes' if d.get('configured') and d.get('valid') else 'no')" 2>/dev/null || echo "no")
  if [[ "$ALREADY" == "yes" ]]; then
    echo "    skip: app already has a valid PAT — leaving as-is"
    skipped=$((skipped+1))
    continue
  fi

  # Mint a fresh bootstrap PAT tagged for this app.
  echo "    minting bootstrap PAT (lifetime=${LIFETIME}s)..."
  TOKEN=$("${DBX[@]}" api post /api/2.0/token/create \
    --json "{\"lifetime_seconds\": $LIFETIME, \"comment\": \"coda-bootstrap:$app\"}" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('token_value',''))" 2>/dev/null || true)
  if [[ -z "$TOKEN" ]]; then
    echo "    ERROR: token/create failed for '$app'." >&2
    had_error=1
    continue
  fi

  # Inject it. On 409 (already configured) treat as a skip, not an error.
  echo "    injecting via /api/inject-pat..."
  RESP=$(curl -sS --max-time 60 -o /tmp/coda_inject_body -w '%{http_code}' \
    -X POST "$APP_URL/api/inject-pat" \
    -H "Content-Type: application/json" \
    -H "X-Coda-Bootstrap-Secret: $SECRET" \
    --data "{\"token\": \"$TOKEN\"}" 2>/dev/null || echo "000")
  BODY=$(cat /tmp/coda_inject_body 2>/dev/null || true); rm -f /tmp/coda_inject_body

  case "$RESP" in
    200)
      inst=$(printf '%s' "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('instance','') or '')" 2>/dev/null || true)
      echo "    OK: PAT injected${inst:+ (instance: $inst)} — rotation started"
      provisioned=$((provisioned+1))
      ;;
    409)
      echo "    skip: app reports a PAT already configured (409)"
      skipped=$((skipped+1))
      ;;
    403)
      echo "    ERROR: 403 — bad/absent bootstrap secret for '$app'." >&2
      had_error=1
      ;;
    404)
      echo "    ERROR: 404 — /api/inject-pat disabled ('$app' missing CODA_BOOTSTRAP_SECRET?)." >&2
      had_error=1
      ;;
    *)
      echo "    ERROR: unexpected response $RESP from '$app': $BODY" >&2
      had_error=1
      ;;
  esac
done

echo
echo "Done. provisioned=$provisioned skipped=$skipped errors=$([[ $had_error -eq 0 ]] && echo 0 || echo yes)"
[[ "$had_error" -eq 0 ]] || exit 1
