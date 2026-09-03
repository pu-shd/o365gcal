#!/bin/zsh
#
# Guided first-time install. Safe to re-run: it detects what is already in place and
# resumes rather than starting over.
#
#   ./scripts/bootstrap.sh                 interactive
#   ./scripts/bootstrap.sh --settings x.json   unattended, reusing saved answers
#   ./scripts/bootstrap.sh --unmanaged     install editable (for developing this)
#
source "${0:A:h}/common.sh"

KIND="managed"
NONINTERACTIVE=0
while (( $# )); do
  case "$1" in
    --settings) SETTINGS_FILE="$2"; NONINTERACTIVE=1; shift 2 ;;
    --unmanaged) KIND="unmanaged"; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

print -P "%B O365GCal - Outlook to Google calendar mirror %b"
print -P "%F{242} A guided install. Nothing is changed until you are asked to confirm.%f"
print ""
if (( ! NONINTERACTIVE )); then
  print -P "%F{242} If this is your first install, ./scripts/install.sh is friendlier:%f"
  print -P "%F{242} it picks the bookkeeping location and the Google calendar for you.%f"
  print ""
fi

# ---------------------------------------------------------------- 1. tooling
info "Step 1/6  Checking prerequisites"
if ! command -v pac >/dev/null 2>&1; then
  warn "The Power Platform CLI (pac) is not installed."
  print "  Install it with:"
  print "    dotnet tool install --global Microsoft.PowerApps.CLI.Tool"
  print "  (needs the .NET SDK: brew install --cask dotnet-sdk)"
  die "Install pac, then run this again."
fi
ok "pac $(pac --version 2>/dev/null | awk '/^Version/{print $2}')"

if ! pac org who >/dev/null 2>&1; then
  warn "Not signed in to a Power Platform environment."
  print "  Sign in with:  pac auth create --environment <your environment URL>"
  print "  List them with: pac env list"
  die "Sign in, then run this again."
fi
ok "signed in to: $(current_env)"

# --------------------------------------------------------- 2. already there?
print ""
info "Step 2/6  Checking for an existing install"
if solution_installed; then
  ok "O365GCal $(solution_version) is already installed."
  print ""
  print "  To change settings:  make an edit in make.powerautomate.com, or re-run"
  print "                       this script and it will re-import with new answers."
  print "  To upgrade:          ./scripts/update.sh"
  print "  To remove:           ./scripts/teardown.sh"
  print ""
  confirm "Re-import and overwrite the current install?" || { info "Nothing changed."; exit 0 }
else
  ok "not installed yet - proceeding with a fresh install"
fi

# ------------------------------------------------------------ 3. connections
print ""
info "Step 3/6  Checking your connections"
MISSING=()
for api in shared_office365 shared_googlecalendar shared_sharepointonline; do
  id="$(connection_id_for $api)"
  if [[ -n "$id" ]]; then
    ok "$api"
  else
    warn "$api - no healthy connection"
    MISSING+=("$api")
  fi
done

if (( ${#MISSING} )); then
  print ""
  warn "Create the missing connection(s) before continuing:"
  print "  https://make.powerautomate.com  >  Connections  >  + New connection"
  for m in "${MISSING[@]}"; do
    case "$m" in
      shared_office365)         print "    - Office 365 Outlook   (NOT 'Outlook.com')" ;;
      shared_googlecalendar)    print "    - Google Calendar      (sign in with the target Google account)" ;;
      shared_sharepointonline)  print "    - SharePoint" ;;
    esac
  done
  print ""
  confirm "I have created them - check again?" || { info "Run this script again when ready."; exit 0 }
  for api in "${MISSING[@]}"; do
    [[ -n "$(connection_id_for $api)" ]] || die "$api still has no healthy connection."
  done
  ok "all connections present"
fi

print ""
warn "Reminder: Outlook + Google in one flow can be blocked by your organisation's DLP"
warn "policy. If the import or the first run fails with a data-loss-prevention error,"
warn "see docs/ADMIN.md - it cannot be worked around from this side."

# --------------------------------------------------------------- 4. settings
print ""
info "Step 4/6  Configuration"

ZIP="$DIST_DIR/${SOLUTION_NAME}_${KIND}.zip"
[[ -f "$ZIP" ]] || { info "Building the solution first"; "$REPO_ROOT/scripts/build.sh" >/dev/null || die "build failed" }

if (( NONINTERACTIVE )); then
  [[ -f "$SETTINGS_FILE" ]] || die "settings file not found: $SETTINGS_FILE"
  info "Reconciling $SETTINGS_FILE against this solution's variables"
  RECONCILED="$(mktemp -t o365gcal-settings)"
  reconcile_settings "$SETTINGS_FILE" "$ZIP" "$RECONCILED"
  SETTINGS_FILE="$RECONCILED"
  ok "settings reconciled"
else
  TEMPLATE="$(mktemp -t o365gcal-settings)"
  pac solution create-settings --solution-zip "$ZIP" --settings-file "$TEMPLATE" >/dev/null

  print ""
  print "  Three answers are needed. Everything else has a sensible default you can"
  print "  change later in make.powerautomate.com."
  print ""
  ask_required SITE_URL   "SharePoint site for bookkeeping (e.g. https://contoso.sharepoint.com/sites/cal)"
  ask_required ALERT_MAIL "Email address for digests and alerts" "$(pac org who 2>/dev/null | awk -F': +' '/User Email/{print $2}')"
  print ""
  print -P "%F{242}  'primary' means the main calendar of the Google account you connected.%f"
  print -P "%F{242}  Setup will email you the full list of IDs to choose from afterwards.%f"
  ask GCAL_ID "Target Google calendar ID" "primary"
  print ""
  ask OCAL_ID "Source Outlook calendar ID" "Calendar"

  PY_BIN="python3"
  [[ -x "$REPO_ROOT/.venv/bin/python" ]] && PY_BIN="$REPO_ROOT/.venv/bin/python"

  "$PY_BIN" - "$TEMPLATE" "$SETTINGS_FILE" \
      "$SITE_URL" "$ALERT_MAIL" "$GCAL_ID" "$OCAL_ID" \
      "$(connection_id_for shared_office365)" \
      "$(connection_id_for shared_googlecalendar)" \
      "$(connection_id_for shared_sharepointonline)" <<'PY'
import json, sys
tmpl, out, site, mail, gcal, ocal, c365, cgoog, csp = sys.argv[1:10]
d = json.load(open(tmpl))

# DryRun ships ON: the first reconcile should show what it would do, not do it.
values = {
    "o3gc_StateSiteUrl": site,
    "o3gc_AlertEmail": mail,
    "o3gc_GoogleCalendarId": gcal,
    "o3gc_OutlookCalendarId": ocal,
    "o3gc_DryRun": "1",
}
for ev in d["EnvironmentVariables"]:
    ev["Value"] = values.get(ev["SchemaName"], ev.get("DefaultValue", "") or "")

conns = {
    "o3gc_sharedoffice365": c365,
    "o3gc_sharedgooglecalendar": cgoog,
    "o3gc_sharedsharepointonline": csp,
}
for cr in d.get("ConnectionReferences", []):
    cr["ConnectionId"] = conns.get(cr["LogicalName"], "")

json.dump(d, open(out, "w"), indent=2)
PY
  [[ -f "$SETTINGS_FILE" ]] || die "could not write $SETTINGS_FILE"
  ok "saved your answers to $SETTINGS_FILE"
  print -P "%F{242}  Keep this file to reinstall later with: ./scripts/bootstrap.sh --settings $SETTINGS_FILE%f"
fi

# ----------------------------------------------------------------- 5. import
print ""
info "Step 5/6  Installing"
print -P "%F{242}  This creates six flows. They are switched OFF and DryRun is ON, so%f"
print -P "%F{242}  nothing touches your real calendars yet.%f"
print ""
confirm "Import into '$(current_env)'?" || { info "Aborted. Nothing was changed."; exit 0 }

pac solution import \
  --path "$ZIP" \
  --settings-file "$SETTINGS_FILE" \
  --activate-plugins \
  --force-overwrite \
  --publish-changes \
  --async \
  --max-async-wait-time 30 || die "Import failed. See docs/TROUBLESHOOTING.md."

ok "imported"

# ------------------------------------------------------------- 6. next steps
print ""
info "Step 6/6  Finishing up"
cat <<'NEXT'

  Two things left, both in https://make.powerautomate.com > Solutions > O365GCal:

  1. Run "O365GCal 0 Setup and Provision" once.
     It creates the bookkeeping lists and emails you a summary listing every
     Outlook and Google calendar ID you could use. Check the IDs it reports
     against what you entered.

  2. Turn on "O365GCal 3 Reconcile" and let it run once.
     DryRun is still on, so it writes nothing. Open the O365GCalLog list on your
     SharePoint site and read what it *would* have done.

  Happy with that? Then go live:
     - set the DryRun environment variable to off
     - turn on flows 1, 3, 4 and 5   (leave 2 on; the others call it)

  Check state any time with:  ./scripts/status.sh

NEXT
ok "Bootstrap complete."
