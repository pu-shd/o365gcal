#!/bin/zsh
# Print the identifiers M365 actually uses for you, and every plausible personal-site
# address derived from them.
#
# A OneDrive personal site path comes from the user principal name, which is not
# always the address you type to sign in - aliases, mail attributes and UPNs can all
# differ. Guessing the path lands you on a redirect to the bare host.
set -euo pipefail

autoload -U colors && colors
ok()   { print -P "%F{green} ok%f $*" }
warn() { print -P "%F{yellow}  ! %f$*" }
info() { print -P "%F{cyan}==>%f $*" }
die()  { print -P "%F{red}error:%f $*" >&2; exit 1 }

RESOURCE="https://graph.microsoft.com"
TENANT="${O365GCAL_TENANT:-$(az account show --query tenantId -o tsv 2>/dev/null || true)}"
TOKEN="$(az account get-access-token --resource "$RESOURCE" --query accessToken -o tsv 2>/dev/null || true)"
if [[ -z "$TOKEN" ]]; then
  az login --tenant "$TENANT" --scope "${RESOURCE}/.default" >/dev/null || die "az login failed."
  TOKEN="$(az account get-access-token --resource "$RESOURCE" --query accessToken -o tsv 2>/dev/null || true)"
fi
[[ -n "$TOKEN" ]] || die "Could not obtain a Graph token."

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

curl -sS -H "Authorization: Bearer $TOKEN" \
  'https://graph.microsoft.com/v1.0/me?$select=userPrincipalName,mail,displayName,id,onPremisesSamAccountName' \
  -o "$TMP/me.json" || true
curl -sS -H "Authorization: Bearer $TOKEN" \
  'https://graph.microsoft.com/v1.0/sites/root?$select=webUrl' \
  -o "$TMP/root.json" || true

python3 - "$TMP" <<'PY'
import json, os, re, sys

tmp = sys.argv[1]


def load(n):
    p = os.path.join(tmp, n)
    try:
        with open(p) as fh:
            return json.load(fh)
    except Exception:
        return {}


me = load("me.json")
if me.get("error"):
    print("  Graph /me failed:", me["error"].get("message", "")[:120])

print()
print("== Who M365 thinks you are")
for label, key in (("userPrincipalName", "userPrincipalName"),
                   ("mail", "mail"),
                   ("displayName", "displayName"),
                   ("samAccountName", "onPremisesSamAccountName")):
    val = me.get(key)
    if val:
        print(f"   {label:20} {val}")

root = load("root.json").get("webUrl", "")
host = ""
m = re.match(r"https://([^./]+)\.sharepoint\.com", root)
if m:
    host = f"https://{m.group(1)}-my.sharepoint.com"

print()
print("== Personal-site addresses to try, most likely first")
seen = []
for key in ("userPrincipalName", "mail", "onPremisesSamAccountName"):
    v = me.get(key)
    if not v:
        continue
    seg = re.sub(r"[.@]", "_", v)
    if seg in seen:
        continue
    seen.append(seg)
    print(f"   {host}/personal/{seg}")

if not seen:
    print("   Graph told us nothing usable - use the browser method below.")

print()
print("=" * 68)
print("If none of those load, get it directly:")
print()
print("   1. Go to https://www.office.com and sign in")
print("   2. App launcher (grid, top left) > OneDrive")
print("   3. Wait for your files to appear, THEN copy the address bar")
print()
print("   The first load often redirects through the bare host before landing")
print("   on the real path, so copy it only once the file list is showing.")
print()
print("A Team's SharePoint site works just as well: in Teams, open the team,")
print("Files tab > Open in SharePoint, and copy that address instead.")
print("=" * 68)
PY
