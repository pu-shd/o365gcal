#!/bin/zsh
# Show which account each connection actually authenticated as.
#
# `pac connection list` prints the Power Platform user who owns the connection, which
# for a Google Calendar connection tells you nothing about which Google account it
# signed into. This asks the API for the real identity.
set -euo pipefail

autoload -U colors && colors
ok()   { print -P "%F{green} ok%f $*" }
warn() { print -P "%F{yellow}  ! %f$*" }
info() { print -P "%F{cyan}==>%f $*" }
die()  { print -P "%F{red}error:%f $*" >&2; exit 1 }

command -v az >/dev/null 2>&1 || die "az CLI not found. Install with: brew install azure-cli"

RESOURCE="https://service.powerapps.com/"
ENV_ID="${O365GCAL_ENV_ID:-$(pac org who 2>/dev/null | awk -F': +' '/Environment ID/{print $2}' | tr -d '[:space:]')}"
[[ -n "$ENV_ID" ]] || die "Could not determine the environment ID."
TENANT="${O365GCAL_TENANT:-$(az account show --query tenantId -o tsv 2>/dev/null || true)}"

TOKEN="$(az account get-access-token --resource "$RESOURCE" --query accessToken -o tsv 2>/dev/null || true)"
if [[ -z "$TOKEN" ]]; then
  warn "Signing in for Power Apps access."
  az login --tenant "$TENANT" --scope "${RESOURCE}/.default" >/dev/null || die "az login failed."
  TOKEN="$(az account get-access-token --resource "$RESOURCE" --query accessToken -o tsv 2>/dev/null || true)"
  [[ -n "$TOKEN" ]] || die "Could not obtain a token."
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

info "Connections in $ENV_ID"
curl -sS -G \
  -H "Authorization: Bearer $TOKEN" \
  --data-urlencode "api-version=2016-11-01" \
  --data-urlencode "\$filter=environment eq '${ENV_ID}'" \
  "https://api.powerapps.com/providers/Microsoft.PowerApps/connections" \
-o "$TMP/connections.json" || true

python3 - "$TMP/connections.json" <<'PY'
import json, sys

with open(sys.argv[1]) as fh:
    doc = json.load(fh)

want = ("shared_office365", "shared_googlecalendar", "shared_sharepointonline")
seen = False
for c in doc.get("value", []):
    p = c.get("properties", {})
    api = (p.get("apiId") or "").rsplit("/", 1)[-1]
    if api not in want:
        continue
    seen = True
    status = (p.get("statuses") or [{}])[0].get("status", "?")
    disp = p.get("displayName") or "(no display name)"
    token = (p.get("connectionParameters") or {}).get("token") or {}
    acct = token.get("Username") or ""
    creator = (p.get("createdBy") or {}).get("userPrincipalName", "")
    mark = "ok" if status == "Connected" else " !"
    print(f"  [{mark}] {api:26} {status}")
    print(f"        signed in as : {acct or disp}")
    print(f"        created by   : {creator}")
    print(f"        id           : {c.get('name', '')}")
    print()

if not seen:
    print("  none of the three connectors have connections in this environment")
PY

cat <<'NOTE'
  The "signed in as" line for Google Calendar is the Google account that will be
  written to. If it is not the account owning your target calendar, create a new
  Google Calendar connection at make.powerautomate.com > Connections.
NOTE
