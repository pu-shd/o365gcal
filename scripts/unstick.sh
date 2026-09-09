#!/bin/zsh
#
# Clear a reconcile run that has wedged.
#
# Flow 3 occasionally stops mid-loop: the platform stops scheduling the next action,
# with no failure, no error and no entry in the run history beyond "Running". On
# 2026-09-08 one sat between two adjacent in-memory actions for 84 minutes.
#
# Since 1.2.0.0 the trigger permits one run at a time, which stops a stalled sweep
# being joined by the next fifteen. The cost of that trade is this script: a wedged
# run now blocks every later sweep, so mirroring stops until someone clears it.
#
# What this never touches: any event on any calendar, in Outlook or Google, and none
# of the three state lists. It cancels flow runs and nothing else. A reconcile is a
# full idempotent sweep, so an abandoned run loses no work - the next scheduled run
# redoes all of it. Already-mirrored events remain exactly as they are.
#
#   ./scripts/unstick.sh                   list wedged runs, change nothing
#   ./scripts/unstick.sh --cancel          cancel them
#   ./scripts/unstick.sh --older-than 90   treat 90 minutes as wedged (default 45)
#   ./scripts/unstick.sh --flow 3          which flow to inspect (default 3)
source "${0:A:h}/common.sh"
require_auth
require_az

DO_CANCEL=0
# 45 minutes is flow 3's own staleness threshold: past it the watchdog already calls
# the calendar stale, so a run still going is late by the system's own definition.
OLDER_THAN=45
WANT_FLOW=3
while (( $# )); do
  case "$1" in
    --cancel) DO_CANCEL=1; shift ;;
    --older-than) OLDER_THAN="$2"; shift 2 ;;
    --flow) WANT_FLOW="$2"; shift 2 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

print -P "%B O365GCal unstick %b"
info "Environment: $(current_env)"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
API="https://api.flow.microsoft.com/providers/Microsoft.ProcessSimple/environments/$(env_id)"
FTOKEN="$(token_for 'https://service.flow.microsoft.com/')"

dataverse_get "workflows?\$select=name,workflowid&\$filter=category%20eq%205%20and%20startswith(name,%27O365GCal%27)" \
  "$TMP/flows.json"

FID="$(python3 - "$TMP/flows.json" "$WANT_FLOW" <<'PY'
import json, sys
try:
    rows = json.load(open(sys.argv[1])).get("value", [])
except Exception:
    rows = []
for r in rows:
    if r["name"].replace("O365GCal ", "").startswith(sys.argv[2]):
        print(r["workflowid"])
        break
PY
)"
[[ -n "$FID" ]] || die "could not read the flow list, so cannot tell which runs exist. Check ./scripts/status.sh first."

curl -sS -H "Authorization: Bearer $FTOKEN" -H "Accept: application/json" \
  "$API/flows/$FID/runs?api-version=2016-11-01&\$top=50" -o "$TMP/runs.json" \
  || die "could not read the run history for flow $WANT_FLOW"

# An unreadable response must never be reported as "nothing is wedged": that is the
# reassuring answer, and it would be a guess. The parser says INCONCLUSIVE instead.
python3 - "$TMP/runs.json" "$OLDER_THAN" "$TMP/wedged.txt" <<'PY'
import json, re, sys
from datetime import datetime, timezone

def parse(text):
    text = text.replace("Z", "").replace("+00:00", "")
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?", text)
    frac = (m.group(2) or "0")[:6].ljust(6, "0")
    return datetime.fromisoformat(f"{m.group(1)}.{frac}").replace(tzinfo=timezone.utc)

try:
    runs = json.load(open(sys.argv[1]))["value"]
except Exception as exc:
    print(f"INCONCLUSIVE: could not read the run history ({exc})")
    raise SystemExit(3)

threshold = int(sys.argv[2])
now = datetime.now(timezone.utc)
wedged, running = [], 0
for r in runs:
    p = r["properties"]
    if p.get("status") != "Running":
        continue
    running += 1
    age = (now - parse(p["startTime"])).total_seconds() / 60
    if age >= threshold:
        wedged.append((r["name"], p["startTime"][:19], age))

with open(sys.argv[3], "w") as fh:
    for name, started, age in wedged:
        fh.write(f"{name} {started} {age:.0f}\n")

print(f"in flight: {running}; wedged beyond {threshold} min: {len(wedged)}")
for _, started, age in wedged:
    print(f"  started {started}Z, running for {age:.0f} min")
PY

[[ -s "$TMP/wedged.txt" ]] || { ok "nothing wedged - no run has outlived $OLDER_THAN minutes"; exit 0 }

if (( ! DO_CANCEL )); then
  print ""
  info "Nothing was changed. Re-run with --cancel to clear these."
  exit 0
fi

print ""
info "Cancelling. A reconcile is idempotent, so the next scheduled run redoes the work."
while read -r RUN STARTED AGE; do
  CODE="$(curl -sS -o /dev/null -w '%{http_code}' -X POST \
    -H "Authorization: Bearer $FTOKEN" -H "Content-Length: 0" \
    "$API/flows/$FID/runs/$RUN/cancel?api-version=2016-11-01")"
  if [[ "$CODE" == "200" ]]; then
    ok "cancelled the run started $STARTED""Z (was $AGE min in)"
  else
    warn "run started $STARTED""Z returned HTTP $CODE - it may already have finished"
  fi
done < "$TMP/wedged.txt"

print ""
info "Confirm the next run finishes normally:  ./scripts/run-flow.sh --runs $WANT_FLOW"
