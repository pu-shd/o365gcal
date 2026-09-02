#!/bin/zsh
# Find a SharePoint site you can use for O365GCal's bookkeeping lists.
#
# The automation needs somewhere to keep three small lists. Any SharePoint site you
# can create lists in will do.
set -euo pipefail

autoload -U colors && colors
ok()   { print -P "%F{green} ok%f $*" }
warn() { print -P "%F{yellow}  ! %f$*" }
info() { print -P "%F{cyan}==>%f $*" }
die()  { print -P "%F{red}error:%f $*" >&2; exit 1 }

command -v az >/dev/null 2>&1 || die "az CLI not found. Install with: brew install azure-cli"

RESOURCE="https://graph.microsoft.com"
TENANT="${O365GCAL_TENANT:-$(az account show --query tenantId -o tsv 2>/dev/null || true)}"

TOKEN="$(az account get-access-token --resource "$RESOURCE" --query accessToken -o tsv 2>/dev/null || true)"
if [[ -z "$TOKEN" ]]; then
  warn "Signing in for Microsoft Graph access."
  az login --tenant "$TENANT" --scope "${RESOURCE}/.default" >/dev/null || die "az login failed."
  TOKEN="$(az account get-access-token --resource "$RESOURCE" --query accessToken -o tsv 2>/dev/null || true)"
  [[ -n "$TOKEN" ]] || die "Could not obtain a Graph token."
fi
ok "Graph token acquired"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

graph() {  # graph <path> <outfile>
  curl -sS -H "Authorization: Bearer $TOKEN" "https://graph.microsoft.com/v1.0/$1" -o "$TMP/$2" || true
}

graph 'me/drive?$select=webUrl'                                   drive.json
graph 'me?$select=userPrincipalName,displayName'                  me.json
graph 'me/followedSites?$select=webUrl,displayName'               followed.json
graph 'sites?search=*&$select=webUrl,displayName&$top=50'         search.json
graph 'me/joinedTeams?$select=id,displayName'                     teams.json
graph 'sites/root?$select=webUrl,displayName'                     root.json

python3 - "$TMP" <<'PY'
import json, os, re, sys

tmp = sys.argv[1]


def load(name):
    path = os.path.join(tmp, name)
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {}


def err(doc):
    return (doc.get("error") or {}).get("message", "")


candidates = []

print()
print("== Your personal site (OneDrive-backed)")
drive = load("drive.json")
url = drive.get("webUrl", "")
if url:
    site = re.sub(r"/Documents/?$", "", url)
    print(f"   {site}")
    candidates.append(site)
else:
    # Do NOT read this as "no OneDrive". The Azure CLI is pre-consented for only a
    # narrow set of Graph permissions, and without Files.Read this call fails even
    # when the drive plainly exists. Derive the address instead.
    reason = err(drive) or "no response"
    print(f"   Graph could not read /me/drive ({reason}).")
    print("   That is usually a token-scope limitation of the az CLI, not a missing")
    print("   OneDrive. Deriving the conventional address instead:")

    upn = load("me.json").get("userPrincipalName", "")
    root_url = load("root.json").get("webUrl", "")
    host = ""
    if root_url:
        m = re.match(r"https://([^./]+)\.sharepoint\.com", root_url)
        if m:
            host = f"https://{m.group(1)}-my.sharepoint.com"
    if upn and host:
        guess = f"{host}/personal/" + re.sub(r"[.@]", "_", upn)
        print(f"      {guess}")
        candidates.append(guess)
        print()
        print("   Confirm it by opening it in a browser. If it loads your files, it is")
        print("   correct. The certain method is below.")
    else:
        print("      could not derive it - use the browser method below.")

print()
print("== Sites you follow")
items = load("followed.json").get("value") or []
if items:
    for s in items:
        print(f"   {s.get('displayName', '?')[:38]:38}  {s.get('webUrl', '')}")
        candidates.append(s.get("webUrl", ""))
else:
    print("   none")

print()
print("== Teams you belong to (each has a SharePoint site)")
teams = load("teams.json").get("value") or []
if teams:
    for t in teams[:25]:
        print(f"   {t.get('displayName', '?')}")
else:
    reason = err(load("teams.json"))
    print(f"   none{' - ' + reason[:80] if reason else ''}")

print()
print("== Sites returned by search")
doc = load("search.json")
items = doc.get("value") or []
if items:
    for s in items[:25]:
        print(f"   {s.get('displayName', '?')[:38]:38}  {s.get('webUrl', '')}")
        candidates.append(s.get("webUrl", ""))
else:
    reason = err(doc)
    print(f"   none{' - ' + reason[:100] if reason else ''}")

root = load("root.json")
if root.get("webUrl"):
    print()
    print("== Tenant root site")
    print(f"   {root['webUrl']}  ({root.get('displayName', '')})")

print()
print("=" * 66)
usable = [c for c in candidates if c]
if usable:
    print("Candidates found. Pick one you can create a list in:")
    for c in dict.fromkeys(usable):
        print(f"   {c}")
else:
    print("Nothing this token can see. That is a permissions limit, not a verdict")
    print("on what you have.")
print()
print("The certain method, if any of the above is in doubt:")
print()
print("   Open OneDrive in a browser (office.com > app launcher > OneDrive).")
print("   The address bar will read something like:")
print()
print("      https://CONTOSO-my.sharepoint.com/personal/you_contoso_edu/_layouts/...")
print()
print("   Everything before  /_layouts  is the site URL. That is what StateSiteUrl")
print("   wants. A Team's Files tab works the same way and is equally valid.")
print("=" * 66)
PY
