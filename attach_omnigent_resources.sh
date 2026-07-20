#!/bin/bash
# Attach the per-app RESOURCES the generic app.yaml resolves at runtime via
# valueFrom, so workspace-specific values (the Omnigent server URL, the wheel
# volume) never have to be committed in app.yaml.
#
# The generic app.yaml references two resource keys:
#   - name: OMNIGENTS_SERVER_URL    valueFrom: omnigent-server-url
#   - name: OMNIGENTS_WHEEL_SPEC    valueFrom: omnigent-wheels
#
# This script attaches those two resources to the app:
#   1. omnigent-wheels  — a UC Volume resource pointing at the wheel volume
#      (the same <catalog>.<schema>.<volume> grant_omnigent_host.sh grants
#      READ_VOLUME on). Resolves at runtime to /Volumes/<c>/<s>/<v>.
#   2. omnigent-server-url — a Secret resource holding the Omnigent server app
#      URL for this workspace. Stored in a Databricks secret scope/key (created
#      if missing) because app.yaml's valueFrom can only reference secrets, not
#      arbitrary strings.
#
# Uses `apps create-update <app> resources` (the targeted field-mask patch) so
# ONLY the resources field is touched — `apps update --json` is a full-body
# write that clears unset fields (notably git_repository on git-linked apps).
# Merges the two resources with the app's existing ones (read → merge → write)
# to avoid clobbering unrelated resources (e.g. workshop challenge-repo-token).
#
# Run AFTER grant_omnigent_host.sh (which grants the SP the UC traversal it
# needs to read the wheel volume). Idempotent: re-running is safe — it updates
# in place.
#
# This script ISSUES IAM/RESOURCE CHANGES. Review the args before running.
#
# Example (aws-daveok):
#   ./attach_omnigent_resources.sh \
#       --profile aws-daveok \
#       --coda-app coda \
#       --server-url https://omnigent-7474660536734442.aws.databricksapps.com \
#       --wheel-volume dok_aws_sandbox_catalog.omnigent.artifacts \
#       --secret-scope coda-omnigent \
#       --secret-key omnigent-server-url

set -euo pipefail

PROFILE=""
CODA_APP=""
SERVER_URL=""
WHEEL_VOLUME=""
SECRET_SCOPE="coda-omnigent"
SECRET_KEY="omnigent-server-url"

usage() {
  sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)        PROFILE="$2"; shift 2 ;;
    --coda-app)       CODA_APP="$2"; shift 2 ;;
    --server-url)     SERVER_URL="$2"; shift 2 ;;
    --wheel-volume)   WHEEL_VOLUME="$2"; shift 2 ;;
    --secret-scope)   SECRET_SCOPE="$2"; shift 2 ;;
    --secret-key)     SECRET_KEY="$2"; shift 2 ;;
    -h|--help)        usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
done

for req in PROFILE CODA_APP SERVER_URL WHEEL_VOLUME; do
  if [[ -z "${!req}" ]]; then
    echo "ERROR: --$(echo "$req" | tr 'A-Z_' 'a-z-') is required" >&2
    usage 1
  fi
done

DBX=(databricks --profile "$PROFILE")

echo "==> Attaching Omnigent resources to '$CODA_APP' on profile '$PROFILE'..."
echo "    server URL:   $SERVER_URL"
echo "    wheel volume: $WHEEL_VOLUME"
echo "    secret:       $SECRET_SCOPE/$SECRET_KEY"

# ---- 1. Store the server URL in a Databricks secret -------------------------
echo "==> Storing server URL in secret $SECRET_SCOPE/$SECRET_KEY..."
# Create the scope idempotently (ignore error if it exists). Scope is
# workspace-local; no initial_principal needed — the app SP reads via the
# resource attachment, not the scope ACL.
"${DBX[@]}" secrets create-scope "$SECRET_SCOPE" 2>/dev/null \
  && echo "    created scope '$SECRET_SCOPE'" \
  || echo "    scope '$SECRET_SCOPE' already exists — reusing"
# Put the secret value via stdin so it never lands on argv or in shell history.
printf '%s' "$SERVER_URL" | "${DBX[@]}" secrets put-secret "$SECRET_SCOPE" "$SECRET_KEY"
echo "    secret stored"

# ---- 2. Read the app's current resources (merge, don't replace) ------------
echo "==> Reading current resources on '$CODA_APP'..."
CURRENT=$("${DBX[@]}" apps get "$CODA_APP" --output json \
  | python3 -c "
import sys,json
d=json.load(sys.stdin)
# Emit the resources as a JSON array of resource objects, or [] if none.
print(json.dumps(d.get('resources') or []))
")
echo "    existing resources: $(printf '%s' "$CURRENT" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")"

# ---- 3. Merge the two omnigent resources and write -------------------------
# Use the Apps SDK's create_update(app, update_mask='resources', app=App(...))
# — the targeted field-mask patch — so ONLY the resources field is touched.
# The `apps update --json` CLI path is a full-body write that CLEARS unset
# fields (notably git_repository on git-linked apps — learned live). The CLI's
# `create-update --json` body shape is finicky (field-name mismatches), so do
# this step in Python with the SDK where the App/AppResource schema is explicit.
# Merge with existing resources (indexed by name) so we don't clobber unrelated
# ones (e.g. workshop challenge-repo-token).
echo "==> Merging + attaching resources..."
DATABRICKS_CONFIG_PROFILE="$PROFILE" python3 - "$CODA_APP" "$WHEEL_VOLUME" "$SECRET_SCOPE" "$SECRET_KEY" "$CURRENT" <<'PY'
import json, os, sys
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.apps import App, AppResource, AppResourceUcSecurable, AppResourceUcSecurableUcSecurableType, AppResourceUcSecurableUcSecurablePermission, AppResourceSecret, AppResourceSecretSecretPermission

coda_app = sys.argv[1]
wheel_volume = sys.argv[2]
scope, key = sys.argv[3], sys.argv[4]
current = json.loads(sys.argv[5])

w = WorkspaceClient(profile=os.environ['DATABRICKS_CONFIG_PROFILE'])
# Index existing resources by name so we update in place, not duplicate.
by_name = {r.get('name'): r for r in current if isinstance(r, dict) and r.get('name')}
by_name['omnigent-wheels'] = {
    'name': 'omnigent-wheels',
    'uc_securable': {
        'securable_full_name': wheel_volume,
        'securable_type': 'VOLUME',
        'permission': 'READ_VOLUME',
    },
}
by_name['omnigent-server-url'] = {
    'name': 'omnigent-server-url',
    'secret': {'scope': scope, 'key': key, 'permission': 'READ'},
}

def to_resource(d):
    name = d['name']
    if 'uc_securable' in d:
        uc = d['uc_securable']
        return AppResource(name=name, uc_securable=AppResourceUcSecurable(
            securable_full_name=uc['securable_full_name'],
            securable_type=AppResourceUcSecurableUcSecurableType[uc['securable_type']],
            permission=AppResourceUcSecurableUcSecurablePermission[uc['permission']],
        ))
    if 'secret' in d:
        s = d['secret']
        return AppResource(name=name, secret=AppResourceSecret(
            scope=s['scope'], key=s['key'],
            permission=AppResourceSecretSecretPermission[s['permission']],
        ))
    raise ValueError(f"unknown resource shape for {name}")

resources = [to_resource(r) for r in by_name.values()]
w.apps.create_update(coda_app, 'resources', app=App(name=coda_app, resources=resources))
PY
echo "    resources attached"

# ---- 4. Verify -------------------------------------------------------------
echo "==> Verifying..."
FINAL=$("${DBX[@]}" apps get "$CODA_APP" --output json \
  | python3 -c "
import sys,json
d=json.load(sys.stdin)
res={r.get('name'): r for r in (d.get('resources') or []) if isinstance(r,dict)}
out=[]
if 'omnigent-wheels' in res:
    uc=res['omnigent-wheels'].get('uc_securable',{})
    out.append('omnigent-wheels=%s perm=%s' % (uc.get('securable_full_name'), uc.get('permission')))
else:
    out.append('omnigent-wheels=MISSING')
if 'omnigent-server-url' in res:
    s=res['omnigent-server-url'].get('secret',{})
    out.append('omnigent-server-url=%s/%s perm=%s' % (s.get('scope'), s.get('key'), s.get('permission')))
else:
    out.append('omnigent-server-url=MISSING')
print('  ' + '  '.join(out))
")
echo "$FINAL"

if echo "$FINAL" | grep -q MISSING; then
  echo "ERROR: one or both resources did not attach — see above." >&2
  exit 1
fi
echo "==> Done. Redeploy '$CODA_APP' for the valueFrom refs to resolve."
