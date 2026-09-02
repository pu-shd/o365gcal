#!/bin/zsh
# Show each O365GCal environment variable's definition default and current value as
# they exist in Dataverse - which is not always what the solution file says, because
# an upgrade will not overwrite a definition that is already present.
set -euo pipefail
autoload -U colors && colors
ok()   { print -P "%F{green} ok%f $*" }
warn() { print -P "%F{yellow}  ! %f$*" }
info() { print -P "%F{cyan}==>%f $*" }
die()  { print -P "%F{red}error:%f $*" >&2; exit 1 }

ORG="$(pac org who 2>/dev/null | awk -F': +' '/Org URL/{print $2}' | tr -d '[:space:]')"
[[ -n "$ORG" ]] || die "no org URL"
ORG="${ORG%/}"
TOKEN="$(az account get-access-token --resource "$ORG" --query accessToken -o tsv 2>/dev/null || true)"
[[ -n "$TOKEN" ]] || die "no Dataverse token; run: az login --scope \"$ORG/.default\""

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
H=(-H "Authorization: Bearer $TOKEN" -H "OData-MaxVersion: 4.0" -H "OData-Version: 4.0" -H "Accept: application/json")

curl -sS "${H[@]}" "$ORG/api/data/v9.2/environmentvariabledefinitions?\$select=schemaname,defaultvalue,type,isrequired&\$filter=startswith(schemaname,%27o3gc_%27)&\$orderby=schemaname" -o "$TMP/def.json"
curl -sS "${H[@]}" "$ORG/api/data/v9.2/environmentvariablevalues?\$select=value,_environmentvariabledefinitionid_value" -o "$TMP/val.json"

python3 - "$TMP/def.json" "$TMP/val.json" <<'PY'
import json, sys
defs = json.load(open(sys.argv[1])).get("value", [])
vals = json.load(open(sys.argv[2])).get("value", [])
by_def = {v.get("_environmentvariabledefinitionid_value"): v.get("value") for v in vals}
if not defs:
    print("  no o3gc_ definitions found")
print(f"  {'schema':38} {'default':22} {'value':22} req")
print("  " + "-" * 88)
for d in defs:
    dv = d.get("defaultvalue")
    val = by_def.get(d.get("environmentvariabledefinitionid"))
    flag = "yes" if d.get("isrequired") else ""
    dshow = "(null)" if dv is None else (f"'{dv}'" if dv.strip() == "" else dv)
    vshow = "(none)" if val is None else (f"'{val}'" if val.strip() == "" else val)
    mark = "  " if (dv is not None or val is not None) else "!!"
    print(f"{mark}{d['schemaname']:38} {dshow[:22]:22} {vshow[:22]:22} {flag}")
print()
print("  '!!' marks a variable with neither a default nor a value. Any flow that")
print("  references one fails to activate with XrmEnvironmentVariableAttributeNotFound.")
PY
