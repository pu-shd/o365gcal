#!/bin/zsh
# Fetch the live swagger for the three Standard connectors this solution binds to.
#
# Why this exists: a cloud flow's action JSON keys its parameters by the connector's
# swagger parameter names, and body properties appear flattened (e.g. "item/summary").
# Those keys are not published in the Microsoft Learn connector reference, so they
# have to come from the live API. Run this once; the output is what
# tests/validate/test_connector_contract.py checks the flow JSON against.
#
# Usage:  ./scripts/fetch-connector-swagger.sh [output-dir]
set -euo pipefail

autoload -U colors && colors
ok()   { print -P "%F{green} ok%f $*" }
warn() { print -P "%F{yellow}  ! %f$*" }
die()  { print -P "%F{red}error:%f $*" >&2; exit 1 }

OUT="${1:-connectors}"
API_VERSION="2016-11-01"
CONNECTORS=(shared_googlecalendar shared_office365 shared_sharepointonline)

mkdir -p "$OUT"

if ! command -v az >/dev/null 2>&1; then
  die "az CLI not found. Install with: brew install azure-cli"
fi

RESOURCE="https://service.powerapps.com/"

# The apis endpoint refuses to answer without an environment filter. Discover it from
# the active pac session; override with O365GCAL_ENV_ID if you need a different one.
ENV_ID="${O365GCAL_ENV_ID:-$(pac org who 2>/dev/null | awk -F': +' '/Environment ID/{print $2}' | tr -d '[:space:]')}"
[[ -n "$ENV_ID" ]] || die "Could not determine the environment ID. Run 'pac auth create --environment <url>' first, or set O365GCAL_ENV_ID."

# Tenant for the interactive fallback. Override with O365GCAL_TENANT if az is signed
# in to a different directory than the Power Platform environment.
TENANT="${O365GCAL_TENANT:-$(az account show --query tenantId -o tsv 2>/dev/null || true)}"

az_login() {
  print "Signing in to Azure (a browser window will open)..."
  if [[ -n "$TENANT" ]]; then
    az login --tenant "$TENANT" --scope "${RESOURCE}/.default" >/dev/null \
      || die "az login failed."
  else
    az login --scope "${RESOURCE}/.default" >/dev/null || die "az login failed."
  fi
}

get_token() {
  # `|| true` is load-bearing: under `set -e` an assignment whose command
  # substitution exits non-zero kills the script, so without it a stale token
  # aborts here silently and the interactive retry below never runs.
  az account get-access-token --resource "$RESOURCE" --query accessToken -o tsv 2>/dev/null || true
}

# `az account show` succeeding proves only that a profile is cached, not that its
# refresh token is still valid - Entra expires them after 90 days of inactivity. So
# the real test is asking for the token and retrying interactively when that fails.
az account show >/dev/null 2>&1 || az_login

print "Requesting Power Apps token..."
TOKEN="$(get_token)"

if [[ -z "$TOKEN" ]]; then
  warn "Cached credentials are stale or lack the Power Apps scope. Re-authenticating."
  az_login
  TOKEN="$(get_token)"
  [[ -n "$TOKEN" ]] || die "Could not obtain a Power Apps token even after signing in."
fi
ok "token acquired"

valid_json() {
  python3 - "$1" <<'PY' 2>/dev/null
import json, sys
json.load(open(sys.argv[1]))
PY
}

FAILED=0
for c in "${CONNECTORS[@]}"; do
  print "Fetching $c ..."
  # -G with --data-urlencode so the OData $filter is encoded correctly; the single
  # quotes keep the shell from expanding $expand and $filter.
  http_code="$(curl -sS -G -w '%{http_code}' \
    -H "Authorization: Bearer $TOKEN" \
    --data-urlencode "api-version=${API_VERSION}" \
    --data-urlencode '$expand=swagger' \
    --data-urlencode "\$filter=environment eq '${ENV_ID}'" \
    "https://api.powerapps.com/providers/Microsoft.PowerApps/apis/${c}" \
    -o "$OUT/${c}.json" || true)"

  if [[ "$http_code" != "200" ]]; then
    warn "$c - HTTP $http_code"
    head -c 300 "$OUT/${c}.json" 2>/dev/null
    print ""
    rm -f "$OUT/${c}.json"
    FAILED=1
  elif ! valid_json "$OUT/${c}.json"; then
    warn "$c - response was not valid JSON"
    rm -f "$OUT/${c}.json"
    FAILED=1
  else
    ok "$OUT/${c}.json ($(wc -c < "$OUT/${c}.json" | tr -d ' ') bytes)"
  fi
done

print ""
if (( FAILED )); then
  die "One or more connectors could not be fetched. See the messages above."
fi
print "Done. Now run the connector contract tests:"
print "    make test"
print ""
print "These files are public connector metadata and contain no secrets, but they are"
print "gitignored because they are large and regenerable."
