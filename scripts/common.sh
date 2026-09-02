#!/bin/zsh
# Shared helpers. Sourced by the other scripts; not meant to be run directly.
set -euo pipefail

SOLUTION_NAME="O365GCal"
REPO_ROOT="${0:A:h:h}"
SRC_DIR="$REPO_ROOT/solution/src"
OUT_DIR="$REPO_ROOT/solution/out"
DIST_DIR="$REPO_ROOT/dist"

autoload -U colors && colors
info()  { print -P "%F{cyan}==>%f $*" }
ok()    { print -P "%F{green} ok%f $*" }
warn()  { print -P "%F{yellow}  ! %f$*" }
die()   { print -P "%F{red}error:%f $*" >&2; exit 1 }

require_pac() {
  command -v pac >/dev/null 2>&1 || die "pac CLI not found. Install: dotnet tool install --global Microsoft.PowerApps.CLI.Tool"
}

require_auth() {
  require_pac
  pac org who >/dev/null 2>&1 || die "Not connected to a Dataverse environment. Run: pac auth create --environment <url>"
}

current_env() {
  pac org who 2>/dev/null | awk -F': +' '/Friendly Name/{print $2}'
}

confirm() {
  local prompt="$1"
  print -n -P "%F{yellow}?%f $prompt [y/N] "
  read -r reply
  [[ "$reply" == [yY]* ]]
}

SETTINGS_FILE="${O365GCAL_SETTINGS:-$REPO_ROOT/o365gcal.settings.json}"

# --- state inspection -------------------------------------------------------

solution_installed() {
  pac solution list 2>/dev/null | grep -qE "^${SOLUTION_NAME}\s"
}

solution_version() {
  pac solution list 2>/dev/null | awk -v n="$SOLUTION_NAME" '$1==n {print $(NF-1)}'
}

solution_is_managed() {
  [[ "$(pac solution list 2>/dev/null | awk -v n="$SOLUTION_NAME" '$1==n {print $NF}')" == "True" ]]
}

# Healthy connection id for a connector, or empty. Users routinely have several
# connections per connector with stale ones among them, so pick a working one.
connection_id_for() {
  pac connection list 2>/dev/null \
    | grep -F "apis/$1" | grep -i "Connected" | head -1 | awk '{print $1}'
}

# --- prompting --------------------------------------------------------------

ask() {  # ask <var> <prompt> [default]
  local __var="$1" __prompt="$2" __default="${3:-}" __reply
  if [[ -n "$__default" ]]; then
    print -n -P "%F{cyan}?%f $__prompt %F{242}[$__default]%f: "
  else
    print -n -P "%F{cyan}?%f $__prompt: "
  fi
  read -r __reply
  [[ -z "$__reply" ]] && __reply="$__default"
  typeset -g "$__var"="$__reply"
}

ask_required() {  # keeps asking until non-empty
  local __var="$1"
  while true; do
    ask "$__var" "$2" "${3:-}"
    [[ -n "${(P)__var}" ]] && break
    warn "This one is required."
  done
}

# Two-stage confirmation for anything irreversible.
confirm_destructive() {
  local prompt="$1" word="$2" reply
  print -P "%F{red}!!%f $prompt"
  print -n -P "%F{red}?%f Type %B$word%b to confirm: "
  read -r reply
  [[ "$reply" == "$word" ]]
}

# --- tokens and API helpers -------------------------------------------------
#
# pac cannot read run history, activate a flow, or read list data, so several
# lifecycle operations go straight to the APIs. All of them need a token per
# resource, and az is the only interactive sign-in available here.

require_az() {
  command -v az >/dev/null 2>&1 || die "az CLI not found. Install with: brew install azure-cli"
}

tenant_id() {
  print -r -- "${O365GCAL_TENANT:-$(az account show --query tenantId -o tsv 2>/dev/null || true)}"
}

# token_for <resource> -- prints a bearer token, signing in if the cached one is
# stale. `|| true` on the inner call is load-bearing: under `set -e` a failed command
# substitution aborts the script before the retry can run.
token_for() {
  local resource="$1" tok
  tok="$(az account get-access-token --resource "$resource" --query accessToken -o tsv 2>/dev/null || true)"
  if [[ -z "$tok" ]]; then
    warn "Signing in for $resource" >&2
    az login --tenant "$(tenant_id)" --scope "${resource%/}/.default" >/dev/null 2>&1 || true
    tok="$(az account get-access-token --resource "$resource" --query accessToken -o tsv 2>/dev/null || true)"
  fi
  [[ -n "$tok" ]] || die "Could not obtain a token for $resource"
  print -r -- "$tok"
}

org_url()  { pac org who 2>/dev/null | awk -F': +' '/Org URL/{print $2}' | tr -d '[:space:]' | sed 's|/$||' }
env_id()   { print -r -- "${O365GCAL_ENV_ID:-$(pac org who 2>/dev/null | awk -F': +' '/Environment ID/{print $2}' | tr -d '[:space:]')}" }

# dataverse_get <relative-path> <outfile>
dataverse_get() {
  local org tok
  org="$(org_url)"; tok="$(token_for "$org")"
  curl -sS -H "Authorization: Bearer $tok" -H "OData-MaxVersion: 4.0" \
       -H "OData-Version: 4.0" -H "Accept: application/json" \
       "$org/api/data/v9.2/$1" -o "$2"
}

# The site host is the token audience for SharePoint REST; the site path is not.
sp_host_of() { print -r -- "$1" | sed -E 's|(https://[^/]+).*|\1|' }

# sharepoint_get <site-url> <relative-api-path> <outfile>
sharepoint_get() {
  local site="${1%/}" tok
  tok="$(token_for "$(sp_host_of "$site")")"
  curl -sS -H "Authorization: Bearer $tok" \
       -H "Accept: application/json;odata=nometadata" \
       "$site/$2" -o "$3"
}

# sharepoint_post <site-url> <relative-api-path> <json-body> <outfile> [extra-header ...]
sharepoint_post() {
  local site="${1%/}" path="$2" body="$3" out="$4" tok
  shift 4
  tok="$(token_for "$(sp_host_of "$site")")"
  curl -sS -X POST -H "Authorization: Bearer $tok" \
       -H "Accept: application/json;odata=nometadata" \
       -H "Content-Type: application/json;odata=nometadata" \
       "$@" --data "$body" "$site/$path" -o "$out" -w '%{http_code}'
}

# Reads the live environment-variable values. The solution file and the environment
# can disagree: a managed upgrade will not overwrite a definition that already
# exists, so a corrected default never reaches an environment that has the old one.
live_env_values() {  # live_env_values <outfile>
  local tmp; tmp="$(mktemp -d)"
  dataverse_get "environmentvariabledefinitions?\$select=schemaname,defaultvalue&\$filter=startswith(schemaname,%27o3gc_%27)" "$tmp/def.json"
  dataverse_get "environmentvariablevalues?\$select=value,_environmentvariabledefinitionid_value" "$tmp/val.json"
  python3 - "$tmp/def.json" "$tmp/val.json" "$1" <<'PY'
import json, sys
defs = json.load(open(sys.argv[1])).get("value", [])
vals = json.load(open(sys.argv[2])).get("value", [])
by_def = {v.get("_environmentvariabledefinitionid_value"): v.get("value") for v in vals}
out = {}
for d in defs:
    effective = by_def.get(d.get("environmentvariabledefinitionid"))
    if effective is None:
        effective = d.get("defaultvalue")
    out[d["schemaname"]] = effective
json.dump(out, open(sys.argv[3], "w"), indent=2, sort_keys=True)
PY
  rm -rf "$tmp"
}

state_site_url() {  # from the live environment, not from a local settings file
  local tmp; tmp="$(mktemp -d)"
  live_env_values "$tmp/env.json" >/dev/null 2>&1 || true
  python3 - "$tmp/env.json" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1])).get("o3gc_StateSiteUrl") or "")
except Exception:
    print("")
PY
  rm -rf "$tmp"
}
