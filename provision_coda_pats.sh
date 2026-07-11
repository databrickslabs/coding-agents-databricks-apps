#!/bin/bash
# provision_coda_pats.sh — mint a distinct PAT per CoDA app and inject it
# programmatically via each app's /api/inject-pat endpoint.
#
# Companion to the /api/inject-pat endpoint (app.py) and per-instance PAT
# rotation tagging (pat_rotator.py). Bootstraps many CoDAs in a workspace
# without pasting a PAT into each browser session:
#
#   1. Resolve each target CoDA app's URL via `databricks apps get`.
#   2. Mint a fresh short-lived PAT per app (via /api/2.0/token/create),
#      tagged so it's attributable to that CoDA.
#   3. POST it to that app's /api/inject-pat with the shared bootstrap secret.
#      The app then adopts it, mints its OWN controlled token, revokes this
#      bootstrap PAT, and starts auto-rotation tagged `coda-auto-rotated:<name>`.
#
# ── IMPORTANT: the Databricks Apps EDGE requires an OAuth bearer ───────────
# Every request to a Databricks App's HTTP surface is authenticated at the
# platform edge BEFORE it reaches the Flask app. A plain curl (or a PAT
# bearer) gets 401 there. So this script must present a *workspace OAuth
# token* for the app audience, from a principal that has CAN_USE (or
# CAN_MANAGE) on the target app. It sends that as `Authorization: Bearer`
# AND the shared secret as `X-Coda-Bootstrap-Secret`; the edge checks the
# former, the Flask app checks the latter.
#
# The OAuth bearer is obtained from the CLI profile, in this order:
#   - M2M profile (client_id/client_secret, auth_type=oauth-m2m):
#     client-credentials grant against {host}/oidc/v1/token  ← automation path
#   - U2M profile (`databricks auth login`): the cached token via
#     `databricks auth token`
# A PAT-only profile CANNOT mint an edge token — the script will say so and
# exit. Use an M2M service principal (with CAN_USE on the apps) for headless
# runs. Note the token identity is checked at the edge; the shared secret is
# what actually authorizes the injection inside the app.
#
# PRECONDITIONS on each target app (set in its app config, then redeploy):
#   - CODA_BOOTSTRAP_SECRET — the shared secret this script must present.
#                             Unset => /api/inject-pat is 404 (endpoint off).
#   - CODA_INSTANCE_NAME     — (recommended) names the app for rotation tags.
#                              Defaults to the app name/URL host if unset.
#   - the auth principal (below) needs CAN_USE on each target app's edge.
#
# NOTE: apps configured with ENABLE_SP_APIKEYHELPER=true auth PAT-free via
# their own SP OAuth and do NOT need an injected PAT — running this against
# them is optional (they still accept it, but SP OAuth already covers model
# auth). See docs/deployment.md.
#
# Idempotent-ish: if an app already has a live PAT it returns 409 (or the
# pre-check sees a valid PAT) and this script SKIPS it. Safe to re-run.
#
# This script MINTS PATs and SENDS them to app endpoints. Review args first.
#
# Examples:
#   # M2M service principal profile, all coda-* apps
#   ./provision_coda_pats.sh --profile sp-m2m --app-prefix coda- \
#       --secret "$SECRET"
#
#   # Explicit list, secret via env (kept out of ps/history)
#   CODA_BOOTSTRAP_SECRET="$SECRET" \
#     ./provision_coda_pats.sh --profile sp-m2m --apps coda-04,coda-05
#
#   # Longer-lived bootstrap PAT (default 900s / 15 min)
#   ./provision_coda_pats.sh --profile sp-m2m --apps coda-04 --lifetime 1800
#
#   # Dry run: resolve + auth-check, but mint/inject nothing
#   ./provision_coda_pats.sh --profile sp-m2m --app-prefix coda- --secret X --dry-run

set -euo pipefail

PROFILE=""
SECRET="${CODA_BOOTSTRAP_SECRET:-}"
APPS=""
APP_PREFIX=""
LIFETIME="900"
DRY_RUN=0

usage() {
  sed -n '2,66p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)     PROFILE="$2"; shift 2 ;;
    --secret)      SECRET="$2"; shift 2 ;;  # overrides CODA_BOOTSTRAP_SECRET env
    --apps)        APPS="$2"; shift 2 ;;
    --app-prefix)  APP_PREFIX="$2"; shift 2 ;;
    --lifetime)    LIFETIME="$2"; shift 2 ;;
    --dry-run)     DRY_RUN=1; shift ;;
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
  HOST=$("${DBX[@]}" auth describe --output json 2>/dev/null \
         | python3 -c "import sys,json; print(json.load(sys.stdin).get('details',{}).get('host',''))" 2>/dev/null || true)
fi
HOST="${HOST%/}"
if [[ -z "$HOST" ]]; then
  echo "ERROR: could not resolve workspace host from the CLI profile." >&2
  echo "       Check: databricks ${PROFILE:+--profile $PROFILE }current-user me" >&2
  exit 1
fi
echo "==> Workspace host: $HOST"

# --- Mint the OAuth edge bearer (M2M client-credentials, else U2M cache) ---
# The Databricks Apps edge authenticates this bearer; a PAT will NOT work.
echo "==> Acquiring OAuth edge bearer from profile '${PROFILE:-DEFAULT}'..."
EDGE_TOKEN=""

# 1. Try M2M: read client_id/client_secret from the profile and do the
#    client-credentials grant directly (the CLI's `auth token` refuses M2M).
EDGE_TOKEN=$(HOST="$HOST" PROFILE="${PROFILE:-DEFAULT}" python3 - <<'PY' 2>/dev/null || true
import configparser, os, sys, urllib.request, urllib.parse, base64, json
home = os.path.expanduser("~")
cfg = configparser.ConfigParser()
cfg.read(os.path.join(home, ".databrickscfg"))
prof = os.environ["PROFILE"]
if prof not in cfg:
    sys.exit(0)
sec = cfg[prof]
cid = sec.get("client_id", "").strip()
csec = sec.get("client_secret", "").strip()
host = (sec.get("host", "") or os.environ["HOST"]).strip().rstrip("/")
if not (cid and csec and host):
    sys.exit(0)  # not an M2M profile — fall through to U2M
data = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "all-apis"}).encode()
req = urllib.request.Request(f"{host}/oidc/v1/token", data=data, method="POST")
req.add_header("Content-Type", "application/x-www-form-urlencoded")
req.add_header("Authorization", "Basic " + base64.b64encode(f"{cid}:{csec}".encode()).decode())
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        print(json.load(r).get("access_token", ""))
except Exception:
    sys.exit(0)
PY
)

# 2. Fall back to U2M cached token (works after `databricks auth login`).
if [[ -z "$EDGE_TOKEN" ]]; then
  EDGE_TOKEN=$("${DBX[@]}" auth token --output json 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null || true)
fi

if [[ -z "$EDGE_TOKEN" ]]; then
  echo "ERROR: could not obtain an OAuth edge bearer from profile '${PROFILE:-DEFAULT}'." >&2
  echo "       The Databricks Apps edge needs OAuth — a PAT-only profile can't do this." >&2
  echo "       Use an M2M service-principal profile (client_id/client_secret," >&2
  echo "       auth_type=oauth-m2m) with CAN_USE on the target apps, or run" >&2
  echo "       'databricks auth login --profile ${PROFILE:-<name>}' for a U2M token." >&2
  exit 1
fi
echo "    edge bearer acquired."

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
[[ "$DRY_RUN" -eq 1 ]] && echo "==> DRY RUN — will resolve + pre-check only, mint/inject nothing."

# Helper: curl the app edge with both the OAuth bearer and the shared secret.
# Usage: app_curl <method> <url> [json-body]  -> prints "<http_code>\n<body>"
app_curl() {
  local method="$1" url="$2" body="${3:-}"
  local tmp; tmp="$(mktemp)"
  local code
  if [[ -n "$body" ]]; then
    code=$(curl -sS --max-time 60 -o "$tmp" -w '%{http_code}' -X "$method" "$url" \
      -H "Authorization: Bearer $EDGE_TOKEN" \
      -H "X-Coda-Bootstrap-Secret: $SECRET" \
      -H "Content-Type: application/json" \
      --data "$body" 2>/dev/null || echo "000")
  else
    code=$(curl -sS --max-time 30 -o "$tmp" -w '%{http_code}' -X "$method" "$url" \
      -H "Authorization: Bearer $EDGE_TOKEN" \
      -H "X-Coda-Bootstrap-Secret: $SECRET" 2>/dev/null || echo "000")
  fi
  printf '%s\n' "$code"
  cat "$tmp"; rm -f "$tmp"
}

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
  ps_out=$(app_curl GET "$APP_URL/api/pat-status")
  ps_code=$(printf '%s' "$ps_out" | head -n1)
  ps_body=$(printf '%s' "$ps_out" | tail -n +2)
  if [[ "$ps_code" == "401" ]]; then
    echo "    ERROR: 401 at the edge for '$app' — the auth principal lacks CAN_USE" >&2
    echo "           on this app (or the bearer is invalid). Grant CAN_USE and retry." >&2
    had_error=1
    continue
  fi
  ALREADY=$(printf '%s' "$ps_body" | python3 -c "import sys,json
try: d=json.loads(sys.stdin.read() or '{}')
except Exception: d={}
print('yes' if d.get('configured') and d.get('valid') else 'no')" 2>/dev/null || echo "no")
  if [[ "$ALREADY" == "yes" ]]; then
    echo "    skip: app already has a valid PAT — leaving as-is"
    skipped=$((skipped+1)); continue
  fi

  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "    dry-run: edge reachable (pat-status HTTP $ps_code) — would mint + inject"
    continue
  fi

  # Mint a fresh bootstrap PAT tagged for this app.
  echo "    minting bootstrap PAT (lifetime=${LIFETIME}s)..."
  MINT=$("${DBX[@]}" api post /api/2.0/token/create \
    --json "{\"lifetime_seconds\": $LIFETIME, \"comment\": \"coda-bootstrap:$app\"}" 2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('token_value',''),d.get('token_info',{}).get('token_id',''))" 2>/dev/null || true)
  TOKEN="${MINT%% *}"
  TOKEN_ID="${MINT##* }"
  if [[ -z "$TOKEN" ]]; then
    echo "    ERROR: token/create failed for '$app'." >&2
    had_error=1; continue
  fi

  # Inject it. On 409 (already configured) treat as a skip, not an error.
  echo "    injecting via /api/inject-pat..."
  inj_out=$(app_curl POST "$APP_URL/api/inject-pat" "{\"token\": \"$TOKEN\"}")
  RESP=$(printf '%s' "$inj_out" | head -n1)
  BODY=$(printf '%s' "$inj_out" | tail -n +2)

  # On any non-success, revoke the bootstrap PAT we just minted so a failed
  # inject never leaves an orphan token behind. On 200 the app has already
  # adopted+revoked it (it mints its own controlled token), so leave it be.
  revoke_orphan() {
    [[ -z "$TOKEN_ID" ]] && return
    "${DBX[@]}" api post /api/2.0/token/delete --json "{\"token_id\": \"$TOKEN_ID\"}" >/dev/null 2>&1 \
      && echo "    (revoked unused bootstrap PAT $TOKEN_ID)"
  }

  case "$RESP" in
    200)
      inst=$(printf '%s' "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('instance','') or '')" 2>/dev/null || true)
      echo "    OK: PAT injected${inst:+ (instance: $inst)} — rotation started"
      provisioned=$((provisioned+1)) ;;
    409)
      echo "    skip: app reports a PAT already configured (409)"
      revoke_orphan
      skipped=$((skipped+1)) ;;
    401)
      echo "    ERROR: 401 at the edge — auth principal lacks CAN_USE on '$app'." >&2
      revoke_orphan; had_error=1 ;;
    403)
      echo "    ERROR: 403 — bad/absent bootstrap secret for '$app'." >&2
      revoke_orphan; had_error=1 ;;
    404)
      echo "    ERROR: 404 — /api/inject-pat disabled ('$app' missing CODA_BOOTSTRAP_SECRET?)." >&2
      revoke_orphan; had_error=1 ;;
    *)
      echo "    ERROR: unexpected response $RESP from '$app': $BODY" >&2
      revoke_orphan; had_error=1 ;;
  esac
done

echo
echo "Done. provisioned=$provisioned skipped=$skipped errors=$([[ $had_error -eq 0 ]] && echo 0 || echo yes)"
[[ "$had_error" -eq 0 ]] || exit 1
