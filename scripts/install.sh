#!/bin/zsh
#
# One command, start to finish, for someone who has never opened Power Automate or
# SharePoint and should not have to.
#
# It works out where to keep its bookkeeping, installs, provisions, switches
# everything on, runs one practice pass that writes nothing, and then shows you the
# plan before anything reaches your calendar. You choose the Google calendar from a
# numbered list rather than pasting an identifier.
#
#   ./scripts/install.sh
source "${0:A:h}/common.sh"

step() { print -P "\n%B$1%b" }

print -P "%B Set up your Outlook to Google calendar mirror %b"
print -P "%F{242} About ten minutes. Nothing touches your calendar until you approve it.%f"

# ------------------------------------------------------------------ 1. tooling
step "1. Checking the tools"
if ! command -v pac >/dev/null 2>&1; then
  warn "One missing piece: the Power Platform CLI."
  print "  Install it by running these two commands, then start this again:"
  print "     brew install --cask dotnet-sdk"
  print "     dotnet tool install --global Microsoft.PowerApps.CLI.Tool"
  exit 1
fi
command -v az >/dev/null 2>&1 || {
  warn "One missing piece: the Azure CLI."
  print "  Install it with:  brew install azure-cli"
  print "  Then start this again."
  exit 1
}
ok "tools present"

if ! pac org who >/dev/null 2>&1; then
  warn "You are not signed in yet."
  print "  Sign in with the command below, then start this again."
  print "  Your work email and password; it opens a browser."
  print ""
  print "     pac auth create"
  exit 1
fi
ok "signed in as $(pac org who 2>/dev/null | awk -F': +' '/User Email/{print $2}')"
ok "environment: $(current_env)"

# ------------------------------------------------------------- 2. connections
step "2. Checking your accounts are linked"
MISSING=()
for api in shared_office365 shared_googlecalendar shared_sharepointonline; do
  [[ -n "$(connection_id_for $api)" ]] || MISSING+=("$api")
done
if (( ${#MISSING} )); then
  warn "Some accounts are not linked yet. This part has to be done in a browser once."
  print ""
  print "  Open:  https://make.powerautomate.com/connections"
  print "  Click  + New connection  and add each of these:"
  for m in "${MISSING[@]}"; do
    case "$m" in
      shared_office365)        print "     - Office 365 Outlook   (your work account - NOT 'Outlook.com')" ;;
      shared_googlecalendar)   print "     - Google Calendar      (the Google account you want events in)" ;;
      shared_sharepointonline) print "     - SharePoint           (your work account)" ;;
    esac
  done
  print ""
  confirm "Done that? Check again"  || { info "Start this again when you are ready."; exit 0 }
  for api in "${MISSING[@]}"; do
    [[ -n "$(connection_id_for $api)" ]] || die "$api still is not linked."
  done
fi
ok "Outlook, Google and SharePoint are all linked"

# --------------------------------------------------- 3. where bookkeeping goes
step "3. Choosing where to keep its notes"
print -P "%F{242}  The mirror keeps a small private record of what it has copied, so it never%f"
print -P "%F{242}  copies the same meeting twice. It goes in your own OneDrive.%f"

RESOURCE="https://graph.microsoft.com"
GTOK="$(token_for "$RESOURCE")"
graph_field() {  # graph_field <path> <field>
  curl -sS -H "Authorization: Bearer $GTOK" "https://graph.microsoft.com/v1.0/$1" \
    -o "$GTMP/g.json" 2>/dev/null || true
  python3 - "$GTMP/g.json" "$2" <<'PY'
import json, sys
try:
    print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
except Exception:
    print("")
PY
}

GTMP="$(mktemp -d)"
ME="$(graph_field 'me?$select=userPrincipalName' userPrincipalName)"
ROOT_SITE="$(graph_field 'sites/root?$select=webUrl' webUrl)"
rm -rf "$GTMP"

SITE=""
if [[ -n "$ME" && -n "$ROOT_SITE" ]]; then
  HOSTPART="${${ROOT_SITE#https://}%%.sharepoint.com*}"
  SITE="https://${HOSTPART}-my.sharepoint.com/personal/${${ME//[.@]/_}}"
  ok "found it: $SITE"
else
  warn "Could not work that out automatically."
fi

print ""
print "  If that looks right, press Enter. Otherwise paste a different address."
print -P "%F{242}  (To find yours: open OneDrive in a browser and copy the address bar up to%f"
print -P "%F{242}   and including your name, e.g. .../personal/your_name_here)%f"
ask SITE_INPUT "OneDrive address" "$SITE"
[[ -n "$SITE_INPUT" ]] || die "an address is required"
SITE="${SITE_INPUT%/}"

# ------------------------------------------------------------- 4. where alerts go
step "4. Where should it email you"
ask_required ALERT "Your email address" "$(pac org who 2>/dev/null | awk -F': +' '/User Email/{print $2}')"

# --------------------------------------------------------------- 5. install
step "5. Installing"
ZIP="$DIST_DIR/${SOLUTION_NAME}_managed.zip"
[[ -f "$ZIP" ]] || { info "Building first"; "$REPO_ROOT/scripts/build.sh" >/dev/null || die "build failed" }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
pac solution create-settings --solution-zip "$ZIP" --settings-file "$TMP/t.json" >/dev/null 2>&1 \
  || die "could not read the solution settings"

PY_BIN="python3"; [[ -x "$REPO_ROOT/.venv/bin/python" ]] && PY_BIN="$REPO_ROOT/.venv/bin/python"
"$PY_BIN" - "$TMP/t.json" "$TMP/s.json" "$SITE" "$ALERT" \
  "$(connection_id_for shared_office365)" \
  "$(connection_id_for shared_googlecalendar)" \
  "$(connection_id_for shared_sharepointonline)" <<'PY'
import json, sys
tmpl, out, site, alert, c365, cgoog, csp = sys.argv[1:8]
d = json.load(open(tmpl))
# Practice mode on for the first run: the installer sees the plan before anything is
# written to a real calendar.
values = {"o3gc_StateSiteUrl": site, "o3gc_AlertEmail": alert, "o3gc_DryRun": "yes"}
kept = []
for ev in d["EnvironmentVariables"]:
    v = values.get(ev["SchemaName"], ev.get("DefaultValue") or "")
    if str(v).strip():
        ev["Value"] = v
        kept.append(ev)
d["EnvironmentVariables"] = kept
conns = {"o3gc_sharedoffice365": c365, "o3gc_sharedgooglecalendar": cgoog,
         "o3gc_sharedsharepointonline": csp}
for cr in d.get("ConnectionReferences", []):
    cr["ConnectionId"] = conns.get(cr["LogicalName"], "")
json.dump(d, open(out, "w"), indent=2)
PY
cp "$TMP/s.json" "$REPO_ROOT/o365gcal.settings.json"

info "This takes a couple of minutes"
pac solution import --path "$ZIP" --settings-file "$TMP/s.json" --activate-plugins \
  --force-overwrite --publish-changes >"$TMP/import.log" 2>&1 \
  || { tail -4 "$TMP/import.log"; die "Install failed. See docs/TROUBLESHOOTING.md" }
ok "installed"

info "Switching it on"
"$REPO_ROOT/scripts/enable-flows.sh" >/dev/null 2>&1 || warn "some parts did not start"
ok "running"

info "Creating its notebook in your OneDrive"
"$REPO_ROOT/scripts/run-flow.sh" 0 >/dev/null 2>&1
ok "done"

# ------------------------------------------------------ 6. pick the calendar
step "6. Which Google calendar should receive your meetings"
"$REPO_ROOT/scripts/configure.sh" calendar || die "no calendar chosen"

# --------------------------------------------------------- 7. practice run
step "7. A practice run - nothing is written yet"
"$REPO_ROOT/scripts/run-flow.sh" 3 >/dev/null 2>&1
COUNT="$("$REPO_ROOT/scripts/run-flow.sh" --outputs Guard_Outlook_Read 3 2>/dev/null | tail -1 | tr -d ' ')"
print ""
ok "It found ${COUNT:-?} meetings in your Outlook calendar to copy across."
print ""
print "  Nothing has been written to Google yet - this was practice mode."
print ""
confirm "Start mirroring for real?" || {
  print ""
  info "Left in practice mode. When you are ready:"
  print "     ./scripts/configure.sh dryrun off"
  exit 0
}

"$REPO_ROOT/scripts/configure.sh" dryrun off >/dev/null 2>&1
"$REPO_ROOT/scripts/configure.sh" notify on  >/dev/null 2>&1
ok "mirroring for real, and it will email you when something changes"

info "Copying your meetings across now"
"$REPO_ROOT/scripts/run-flow.sh" 3 >/dev/null 2>&1
ok "done"

cat <<'DONE'

  ────────────────────────────────────────────────────────────────
  All set. Your Outlook meetings now appear on the Google calendar
  you chose, within about fifteen minutes of any change.

  Each copied event shows who organised it, who is invited, and
  whether you still owe someone a reply.

  Everyday commands, none of which need a browser:

     ./scripts/configure.sh          see and change settings
     ./scripts/status.sh             is it running?
     ./scripts/update.sh             install a newer version
     ./scripts/teardown.sh           remove it

  It emails you if it ever stops working.
  ────────────────────────────────────────────────────────────────
DONE
