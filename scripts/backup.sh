#!/bin/zsh
#
# Snapshot everything needed to rebuild this install: the solution, its configuration,
# and - most importantly - the sync map.
#
# The sync map is the only record of which Google events this automation created. Lose
# it and a reinstall cannot tell an event it made from one you made yourself, so it
# mirrors everything a second time and can never clean up the first copy. That is the
# failure this script exists to prevent.
#
# Reads only. Nothing on any calendar is touched.
#
#   ./scripts/backup.sh                 -> backups/<timestamp>/
#   ./scripts/backup.sh --to /some/dir
source "${0:A:h}/common.sh"
require_auth
require_az

DEST=""
while (( $# )); do
  case "$1" in
    --to) DEST="$2"; shift 2 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="${DEST:-$REPO_ROOT/backups/$STAMP}"
mkdir -p "$DEST"

print -P "%B O365GCal backup %b"
info "Environment: $(current_env)"
info "Destination: $DEST"
print ""

if ! solution_installed; then
  warn "O365GCal is not installed here. Backing up configuration only."
fi

# --- 1. the solution itself
info "1/4  Exporting the solution"
if solution_installed; then
  pac solution export --name "$SOLUTION_NAME" --path "$DEST" --managed false --overwrite >/dev/null 2>&1 \
    && ok "solution.zip" \
    || warn "export failed (you may lack read access); dist/ zips remain your fallback"
  [[ -f "$DEST/${SOLUTION_NAME}.zip" ]] && mv "$DEST/${SOLUTION_NAME}.zip" "$DEST/solution.zip"
else
  warn "skipped - not installed"
fi

# --- 2. configuration: environment variable values and connection bindings
info "2/4  Capturing configuration"
if live_env_values "$DEST/environment-variables.json" 2>/dev/null; then
  COUNT="$(python3 - "$DEST/environment-variables.json" <<'PY'
import json, sys
print(len(json.load(open(sys.argv[1]))))
PY
)"
  ok "environment-variables.json ($COUNT variables)"
else
  warn "could not read environment variables"
fi

# A record of which connections were bound, so a restore can rebind the same ones.
# This only reads pac's listing; no connector is ever invoked from this script.
pac connection list 2>/dev/null | grep -iE "o365|googlecal|sharepointonline" \
  > "$DEST/connections.txt" || true
ok "connections.txt"

[[ -f "$REPO_ROOT/o365gcal.settings.json" ]] && cp "$REPO_ROOT/o365gcal.settings.json" "$DEST/settings.json" && ok "settings.json"

# --- 3. flow states, so a restore can put them back as they were
info "3/4  Recording flow states"
dataverse_get "workflows?\$select=name,statecode,workflowid&\$filter=category%20eq%205%20and%20startswith(name,%27O365GCal%27)" \
  "$DEST/flows.json" 2>/dev/null && ok "flows.json" || warn "could not read flow states"

# --- 4. the state lists
info "4/4  Exporting the state lists"
# An error body is not an empty list. Counting rows without checking for one is how
# a failed read gets reported as "0 rows, ok" - which reads as a successful backup of
# an empty install and is far worse than a loud failure.
row_count() {
  python3 - "$1" <<'PY'
import json, sys
try:
    doc = json.load(open(sys.argv[1]))
except Exception:
    print(-1); raise SystemExit
if not isinstance(doc, dict) or "value" not in doc:
    print(-1)
else:
    print(len(doc["value"]))
PY
}

# Two transports, because neither is guaranteed. SharePoint REST needs a token whose
# audience is the site host, which the az CLI cannot always issue; Graph needs
# Sites.Read, which some tenants withhold.
export_list() {  # export_list <site> <list> <outfile>
  local site="${1%/}" list="$2" out="$3"
  if sharepoint_get "$site" "_api/web/lists/getbytitle('$list')/items?\$top=5000" "$out" 2>/dev/null \
     && [[ "$(row_count "$out")" != "-1" ]]; then
    return 0
  fi
  # NOT `local path`: in zsh `path` is tied to PATH, so assigning a string to it
  # empties the command search path for the rest of the function and every external
  # command silently becomes "command not found".
  local host site_path tok
  host="$(print -r -- "$site" | sed -E 's|https://([^/]+).*|\1|')"
  site_path="$(print -r -- "$site" | sed -E 's|https://[^/]+||')"
  tok="$(token_for "https://graph.microsoft.com" 2>/dev/null || true)"
  if [[ -n "$tok" ]]; then
    curl -sS -H "Authorization: Bearer $tok" -H "Accept: application/json" \
      "https://graph.microsoft.com/v1.0/sites/${host}:${site_path}:/lists/${list}/items?\$expand=fields&\$top=5000" \
      -o "$out" 2>/dev/null || true
    if [[ "$(row_count "$out")" != "-1" ]]; then
      return 0
    fi
  fi
  local snippet
  snippet="$(head -c 120 "$out" 2>/dev/null | tr -d '\012' || true)"
  warn "$list - could not read: ${snippet:-no response}"
  rm -f "$out"
  return 1
}

SITE="$(state_site_url)"
if [[ -z "$SITE" ]]; then
  warn "StateSiteUrl is not set; skipping list export"
else
  print -P "%F{242}     site: $SITE%f"
  TOTAL=0
  FAILED_LISTS=0
  for list in O365GCalSyncMap O365GCalLog O365GCalHealth; do
    if ! export_list "$SITE" "$list" "$DEST/$list.json"; then
      FAILED_LISTS=1
      continue
    fi
    COUNT="$(row_count "$DEST/$list.json")"
    ok "$list.json ($COUNT rows)"
    TOTAL=$((TOTAL + COUNT))
    (( COUNT >= 5000 )) && warn "$list hit the 5000-row cap; this export is INCOMPLETE"
  done

  if (( FAILED_LISTS )); then
    print ""
    warn "State list export FAILED. The configuration backup above is still valid,"
    warn "but the sync map is NOT in this backup."
    print ""
    print "  Export the lists by hand instead - open each list in the browser and use"
    print "  the List > Export > Export to CSV command:"
    print "     ${SITE}/_layouts/15/viewlsts.aspx"
    print ""
    print "  Keep the CSVs alongside this backup directory. They are what stops a"
    print "  reinstall from mirroring every event a second time."
  fi
fi

# --- manifest
python3 - "$DEST" "$STAMP" "$(current_env)" "$(env_id)" "$(solution_version)" "$SITE" <<'PY'
import json, os, sys
dest, stamp, envname, envid, version, site = sys.argv[1:7]
files = sorted(f for f in os.listdir(dest) if f != "manifest.json")
json.dump({
    "takenUtc": stamp,
    "environment": envname,
    "environmentId": envid,
    "solutionVersion": version,
    "stateSiteUrl": site,
    "files": files,
    "note": (
        "Restore with scripts/restore.sh. The sync map is the critical file: it maps "
        "Outlook occurrences to the Google events this automation created, and is what "
        "stops a reinstall from mirroring everything a second time."
    ),
}, open(os.path.join(dest, "manifest.json"), "w"), indent=2)
PY

print ""
ok "Backup complete: $DEST"
print -P "%F{242}  Nothing on any calendar was touched. Restore with:"
print -P "     ./scripts/restore.sh $DEST%f"
