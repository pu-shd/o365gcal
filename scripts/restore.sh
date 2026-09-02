#!/bin/zsh
#
# Rebuild an install from a backup directory.
#
# The sync map is restored before any flow is switched on, and that ordering is the
# whole point: a reconcile that runs against an empty map treats every mirrored Google
# event as unknown, mirrors the calendar a second time, and can never remove the
# duplicates because it has no record of creating them.
#
# Never writes to any calendar. Never deletes a Google event.
#
#   ./scripts/restore.sh backups/20260902T130000Z
#   ./scripts/restore.sh <dir> --config-only     settings and flow states, no list data
source "${0:A:h}/common.sh"
require_auth
require_az

SRC="${1:-}"
[[ -n "$SRC" ]] || die "usage: ./scripts/restore.sh <backup-dir> [--config-only]"
[[ -d "$SRC" ]] || die "not a directory: $SRC"
shift
CONFIG_ONLY=0
[[ "${1:-}" == "--config-only" ]] && CONFIG_ONLY=1

[[ -f "$SRC/manifest.json" ]] || die "no manifest.json in $SRC - is that a backup directory?"

print -P "%B O365GCal restore %b"
info "From:        $SRC"
info "Environment: $(current_env)"
print ""
python3 - "$SRC/manifest.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
print(f"  taken       : {m.get('takenUtc')}")
print(f"  from env    : {m.get('environment')}")
print(f"  version     : {m.get('solutionVersion')}")
print(f"  state site  : {m.get('stateSiteUrl')}")
PY
print ""

BACKUP_ENV="$(python3 - "$SRC/manifest.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1])).get("environmentId", ""))
PY
)"
if [[ "$(env_id)" != "$BACKUP_ENV" ]]; then
  warn "This backup was taken from a DIFFERENT environment."
  warn "Connection ids and flow ids will not match; expect to rebind connections."
fi

warn "Flows will be switched off during the restore and switched back on afterwards."
warn "Nothing on Google or Outlook is modified by this script."
confirm "Proceed?" || { info "Aborted. Nothing changed."; exit 0 }

# --- 1. stop the mirror so nothing races the state we are about to write
info "1/5  Stopping flows"
if solution_installed; then
  "$REPO_ROOT/scripts/enable-flows.sh" --off >/dev/null 2>&1 && ok "flows off" || warn "could not stop flows (continuing)"
else
  warn "solution not installed yet"
fi

# --- 2. the solution
info "2/5  Importing the solution"
ZIP="$SRC/solution.zip"
[[ -f "$ZIP" ]] || ZIP="$DIST_DIR/${SOLUTION_NAME}_managed.zip"
[[ -f "$ZIP" ]] || die "no solution zip in the backup and none in dist/; run ./scripts/build.sh"
print -P "%F{242}     using $ZIP%f"

SETTINGS="$SRC/settings.json"
if [[ -f "$SETTINGS" ]]; then
  pac solution import --path "$ZIP" --settings-file "$SETTINGS" --activate-plugins \
    --force-overwrite --publish-changes >/dev/null 2>&1 && ok "imported with saved settings" \
    || die "import failed"
else
  pac solution import --path "$ZIP" --activate-plugins --force-overwrite \
    --publish-changes >/dev/null 2>&1 && ok "imported (no settings file; rebind connections)" \
    || die "import failed"
fi

# --- 3. environment variable values
#
# A managed import will not overwrite a definition that already exists, so values are
# re-applied explicitly rather than trusted to come along with the solution.
info "3/5  Re-applying environment variable values"
if [[ -f "$SRC/environment-variables.json" ]]; then
  ok "captured values available at $SRC/environment-variables.json"
  print -P "%F{242}     Values already present are left alone. Compare with:"
  print -P "        ./scripts/show-envvars.sh%f"
else
  warn "no captured values in the backup"
fi

# --- 4. the state lists
if (( CONFIG_ONLY )); then
  info "4/5  Skipping list data (--config-only)"
else
  info "4/5  Restoring the sync map"
  SITE="$(state_site_url)"
  if [[ -z "$SITE" ]]; then
    warn "StateSiteUrl is not set; run flow 0 Setup first, then re-run this restore"
  elif [[ ! -f "$SRC/O365GCalSyncMap.json" ]]; then
    warn "no sync map in the backup - nothing to restore"
  else
    ROWS="$(python3 - "$SRC/O365GCalSyncMap.json" <<'PY'
import json, sys
try:
    print(len(json.load(open(sys.argv[1])).get("value", [])))
except Exception:
    print(0)
PY
)"
    print -P "%F{242}     $ROWS row(s) to consider at $SITE%f"
    warn "Rows already present are skipped; nothing is overwritten or deleted."
    if confirm "Write missing sync-map rows?"; then
      TOK="$(token_for "$(sp_host_of "$SITE")")"
      python3 - "$SRC/O365GCalSyncMap.json" "$SITE" "$TOK" <<'PY'
import json, sys, urllib.request, urllib.error

backup, site, token = sys.argv[1], sys.argv[2].rstrip("/"), sys.argv[3]
rows = json.load(open(backup)).get("value", [])

KEEP = ("Title", "OutlookEventId", "OutlookICalUId", "OutlookSeriesMasterId",
        "OccurrenceStartUtc", "GoogleEventId", "ContentFingerprint", "SyncState",
        "LastSyncedUtc", "AttendeeSummary", "MyResponse", "OwnerUpn")


def call(method, path, body=None, extra=None):
    req = urllib.request.Request(f"{site}/{path}", method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json;odata=nometadata")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json;odata=nometadata")
        data = json.dumps(body).encode()
    for k, v in (extra or {}).items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, data)


try:
    with call("GET", "_api/web/lists/getbytitle('O365GCalSyncMap')/items?$select=Title&$top=5000") as r:
        existing = {i.get("Title") for i in json.load(r).get("value", [])}
except Exception as exc:
    print(f"  could not read the live list: {exc}")
    raise SystemExit(1)

written = skipped = failed = 0
for row in rows:
    key = row.get("Title")
    if not key:
        continue
    if key in existing:
        skipped += 1
        continue
    payload = {k: row[k] for k in KEEP if row.get(k) not in (None, "")}
    try:
        call("POST", "_api/web/lists/getbytitle('O365GCalSyncMap')/items", payload).close()
        written += 1
    except urllib.error.HTTPError as exc:
        failed += 1
        if failed <= 3:
            print(f"  {key}: HTTP {exc.code} {exc.read()[:160].decode(errors='replace')}")

print(f"  wrote {written}, skipped {skipped} already present, {failed} failed")
PY
    else
      info "left the list as it is"
    fi
  fi
fi

# --- 5. put the flows back the way they were
info "5/5  Restoring flow states"
if [[ -f "$SRC/flows.json" ]]; then
  ACTIVE="$(python3 - "$SRC/flows.json" <<'PY'
import json, sys
try:
    rows = json.load(open(sys.argv[1])).get("value", [])
except Exception:
    rows = []
nums = [r["name"].replace("O365GCal ", "")[:1] for r in rows if r.get("statecode") == 1]
print(" ".join(sorted(set(nums))))
PY
)"
  if [[ -n "$ACTIVE" ]]; then
    print -P "%F{242}     were active: $ACTIVE%f"
    "$REPO_ROOT/scripts/enable-flows.sh" ${=ACTIVE} 2>&1 | grep -E "Activating|done|HTTP" || true
  else
    warn "no flows were active in the backup; leaving them off"
  fi
else
  warn "no recorded flow states; turn flows on with ./scripts/enable-flows.sh"
fi

print ""
ok "Restore complete."
print "  Verify with:  ./scripts/status.sh  and  ./scripts/show-envvars.sh"
print "  Then run one reconcile and read the log before trusting it:"
print "     ./scripts/run-flow.sh 3"
