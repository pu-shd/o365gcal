#!/bin/zsh
#
# Upgrade an installed O365GCal in place.
#
# What this never touches:
#   * any event on any Google calendar - already-mirrored events stay exactly as they are
#   * any event in Outlook - the source is only ever read
#   * the three state lists - the sync map survives, so nothing is re-mirrored
#
# Flows are switched off for the duration and put back afterwards. A reconcile that
# straddled two versions could compute one fingerprint under the old rules and the
# next under the new ones, rewriting events that had not changed.
#
#   ./scripts/update.sh              upgrade to the locally built version
#   ./scripts/update.sh --check      report versions only, change nothing
#   ./scripts/update.sh --no-backup  skip the safety backup (not advised)
source "${0:A:h}/common.sh"
require_auth

CHECK_ONLY=0
DO_BACKUP=1
while (( $# )); do
  case "$1" in
    --check) CHECK_ONLY=1; shift ;;
    --no-backup) DO_BACKUP=0; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

print -P "%B O365GCal update %b"
info "Environment: $(current_env)"
print ""

if ! solution_installed; then
  warn "O365GCal is not installed in this environment."
  print "  This would be a first install, not an upgrade:"
  print "     ./scripts/bootstrap.sh"
  exit 1
fi

INSTALLED="$(solution_version)"
LOCAL="$(grep -m1 '<Version>' "$SRC_DIR/Other/Solution.xml" | sed 's/.*<Version>\(.*\)<\/Version>.*/\1/')"
info "installed: $INSTALLED"
info "available: $LOCAL"

if (( CHECK_ONLY )); then
  [[ "$INSTALLED" == "$LOCAL" ]] && ok "up to date" || warn "an upgrade is available"
  exit 0
fi

if [[ "$INSTALLED" == "$LOCAL" ]]; then
  warn "Already at $LOCAL."
  confirm "Re-import the same version anyway?" || { info "Nothing changed."; exit 0 }
fi

print ""
print "  This upgrade will:"
print "    - switch the flows off, replace them, and switch them back on"
print "    - keep every environment variable value and connection binding"
print "    - leave the sync map, the log and the health list untouched"
print "    - leave every already-mirrored Google event exactly as it is"
print ""

# A backup first, because the sync map is the one artefact that cannot be reconstructed:
# without it a reinstall cannot tell its own Google events from the user's.
if (( DO_BACKUP )); then
  info "Taking a safety backup first"
  "$REPO_ROOT/scripts/backup.sh" >/dev/null 2>&1 \
    && ok "backup written to backups/" \
    || warn "backup failed; continuing (pass --no-backup to silence this)"
fi

# Capture the live configuration so the import cannot drop a value. An import with no
# settings file leaves connection references unbound and every flow unable to start.
info "Capturing current configuration"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

SETTINGS="$REPO_ROOT/o365gcal.settings.json"
if [[ ! -f "$SETTINGS" ]]; then
  warn "No o365gcal.settings.json found. Generating one from the live environment."
  pac solution create-settings --solution-zip "$DIST_DIR/${SOLUTION_NAME}_managed.zip" \
      --settings-file "$TMP/template.json" >/dev/null 2>&1 || die "create-settings failed"
  live_env_values "$TMP/live.json" 2>/dev/null || die "could not read live values"
  pac connection list 2>/dev/null > "$TMP/conns.txt" || true
  python3 - "$TMP/template.json" "$TMP/live.json" "$TMP/conns.txt" "$SETTINGS" <<'PY'
import json, re, sys
tmpl = json.load(open(sys.argv[1]))
live = json.load(open(sys.argv[2]))
conns = open(sys.argv[3]).read()

# An entry with an empty Value is rejected outright, so blanks are omitted and left
# to the definition default.
kept = []
for ev in tmpl["EnvironmentVariables"]:
    val = live.get(ev["SchemaName"]) or ev.get("DefaultValue") or ""
    if str(val).strip():
        ev["Value"] = val
        kept.append(ev)
tmpl["EnvironmentVariables"] = kept

for cr in tmpl.get("ConnectionReferences", []):
    api = cr["ConnectorId"].rsplit("/", 1)[-1]
    for line in conns.splitlines():
        if f"apis/{api}" in line and "Connected" in line:
            cr["ConnectionId"] = line.split()[0]
            break

json.dump(tmpl, open(sys.argv[4], "w"), indent=2)
print(f"  wrote {sys.argv[4]} ({len(kept)} variables)")
PY
fi

info "Reconciling settings against the new solution version"
RECONCILED="$TMP/settings.json"
reconcile_settings "$SETTINGS" "$DIST_DIR/${SOLUTION_NAME}_managed.zip" "$RECONCILED"
SETTINGS="$RECONCILED"
ok "settings reconciled"

print ""
confirm "Proceed with the upgrade?" || { info "Aborted. Nothing changed."; exit 0 }

# Record which flows were running so they can be put back exactly as they were.
info "Recording current flow states"
dataverse_get "workflows?\$select=name,statecode&\$filter=category%20eq%205%20and%20startswith(name,%27O365GCal%27)" \
  "$TMP/flows.json" 2>/dev/null || true
WERE_ON="$(python3 - "$TMP/flows.json" <<'PY'
import json, sys
try:
    rows = json.load(open(sys.argv[1])).get("value", [])
except Exception:
    rows = []
print(" ".join(sorted({r["name"].replace("O365GCal ", "")[:1]
                       for r in rows if r.get("statecode") == 1})))
PY
)"
[[ -n "$WERE_ON" ]] && ok "active: $WERE_ON" || warn "none currently active"

info "Switching flows off"
"$REPO_ROOT/scripts/enable-flows.sh" --off >/dev/null 2>&1 && ok "off" || warn "could not switch all off"

info "Importing $LOCAL"
pac solution import --path "$DIST_DIR/${SOLUTION_NAME}_managed.zip" \
  --settings-file "$SETTINGS" --activate-plugins --force-overwrite --publish-changes \
  >"$TMP/import.log" 2>&1 || { tail -5 "$TMP/import.log"; die "import failed - the previous version is still installed" }
ok "imported"

# An import replaces and deactivates every flow, so this is not optional cleanup.
info "Switching flows back on"
if [[ -n "$WERE_ON" ]]; then
  "$REPO_ROOT/scripts/enable-flows.sh" ${=WERE_ON} 2>&1 | grep -E "Activating|HTTP" || true
else
  "$REPO_ROOT/scripts/enable-flows.sh" 2>&1 | grep -E "Activating|HTTP" || true
fi

print ""
ok "Upgrade complete: $INSTALLED -> $LOCAL"
cat <<'NEXT'

  Verify, in this order:
    ./scripts/status.sh          every flow that was on is on again
    ./scripts/show-envvars.sh    no variable lost its value
    ./scripts/run-flow.sh 3      one reconcile, then read the log list

  A new version may add settings that take their defaults until you change them;
  show-envvars.sh is where you would notice.
NEXT
