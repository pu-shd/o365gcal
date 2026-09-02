#!/bin/zsh
# Turn O365GCal flows on (or off) through the Dataverse API.
#
# The maker portal hides the on/off control when you lack certain environment
# privileges, and pac has no flow-activation command. Setting statecode on the
# workflow record does the same job and reports a precise error when it cannot.
#
# Order matters: the child flow must be active before any parent can call it.
#
#   ./scripts/enable-flows.sh              turn on every O365GCal flow
#   ./scripts/enable-flows.sh 0 2          turn on just those, by leading number
#   ./scripts/enable-flows.sh --off        turn them all off
#   ./scripts/enable-flows.sh --list       show current state, change nothing
set -euo pipefail

autoload -U colors && colors
ok()   { print -P "%F{green} ok%f $*" }
warn() { print -P "%F{yellow}  ! %f$*" }
info() { print -P "%F{cyan}==>%f $*" }
die()  { print -P "%F{red}error:%f $*" >&2; exit 1 }

MODE="on"
SELECT=()
while (( $# )); do
  case "$1" in
    --off)  MODE="off";  shift ;;
    --list) MODE="list"; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) SELECT+=("$1"); shift ;;
  esac
done

command -v az >/dev/null 2>&1 || die "az CLI not found. Install with: brew install azure-cli"

ORG="$(pac org who 2>/dev/null | awk -F': +' '/Org URL/{print $2}' | tr -d '[:space:]')"
[[ -n "$ORG" ]] || die "Could not determine the org URL. Run: pac auth create --environment <url>"
ORG="${ORG%/}"
TENANT="${O365GCAL_TENANT:-$(az account show --query tenantId -o tsv 2>/dev/null || true)}"

TOKEN="$(az account get-access-token --resource "$ORG" --query accessToken -o tsv 2>/dev/null || true)"
if [[ -z "$TOKEN" ]]; then
  warn "Signing in for Dataverse access."
  az login --tenant "$TENANT" --scope "${ORG}/.default" >/dev/null || die "az login failed."
  TOKEN="$(az account get-access-token --resource "$ORG" --query accessToken -o tsv 2>/dev/null || true)"
  [[ -n "$TOKEN" ]] || die "Could not obtain a Dataverse token."
fi

API="$ORG/api/data/v9.2"
HDR=(-H "Authorization: Bearer $TOKEN" -H "OData-MaxVersion: 4.0" -H "OData-Version: 4.0"
     -H "Accept: application/json" -H "Content-Type: application/json; charset=utf-8")

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

info "Reading O365GCal flows from $ORG"
curl -sS "${HDR[@]}" \
  "$API/workflows?\$select=name,statecode,statuscode,workflowid,category&\$filter=category%20eq%205%20and%20startswith(name,%27O365GCal%27)&\$orderby=name" \
  -o "$TMP/flows.json" || die "query failed"

python3 - "$TMP/flows.json" "$MODE" "${SELECT[@]}" <<'PY' > "$TMP/plan.txt"
import json, sys

doc = json.load(open(sys.argv[1]))
mode = sys.argv[2]
select = sys.argv[3:]

if "error" in doc:
    print("ERROR " + doc["error"].get("message", "")[:200])
    raise SystemExit

rows = doc.get("value", [])
if not rows:
    print("ERROR no O365GCal flows found in this environment")
    raise SystemExit

# The child flow has to be running before anything calls it, and off last for the
# same reason in reverse.
def rank(r):
    n = r["name"]
    return (0 if " 2 " in n else 1, n)

rows.sort(key=rank, reverse=(mode == "off"))

for r in rows:
    num = r["name"].replace("O365GCal ", "")[:1]
    if select and num not in select:
        continue
    state = "Activated" if r.get("statecode") == 1 else "Draft"
    print(f"{r['workflowid']}\t{r['name']}\t{state}")
PY

if grep -q '^ERROR' "$TMP/plan.txt"; then
  die "$(sed 's/^ERROR //' "$TMP/plan.txt")"
fi

if [[ "$MODE" == "list" ]]; then
  while IFS=$'\t' read -r id name state; do
    [[ "$state" == "Activated" ]] && ok "$name" || warn "$name - $state"
  done < "$TMP/plan.txt"
  exit 0
fi

if [[ "$MODE" == "on" ]]; then
  STATE=1; STATUS=2; VERB="Activating"
else
  STATE=0; STATUS=1; VERB="Deactivating"
fi

FAILED=0
while IFS=$'\t' read -r id name state; do
  print -n "  $VERB $name ... "
  code="$(curl -sS -o "$TMP/resp.json" -w '%{http_code}' -X PATCH "${HDR[@]}" \
    -d "{\"statecode\": $STATE, \"statuscode\": $STATUS}" \
    "$API/workflows($id)" || true)"
  if [[ "$code" == "204" ]]; then
    print -P "%F{green}done%f"
  else
    print -P "%F{red}HTTP $code%f"
    python3 - "$TMP/resp.json" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    msg = d.get("error", {}).get("message", "")
    print("      " + msg[:400])
except Exception:
    pass
PY
    FAILED=1
  fi
done < "$TMP/plan.txt"

print ""
if (( FAILED )); then
  warn "One or more flows could not be changed."
  warn "A privilege error here means your account lacks Write on the Process table in"
  warn "this environment - the same restriction the maker portal reports as"
  warn "'commands unavailable due to your current privileges'. Ask a Power Platform"
  warn "admin to grant it, or to run this in an environment you own."
  exit 1
fi
ok "Done. Check with: ./scripts/status.sh"
