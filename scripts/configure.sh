#!/bin/zsh
#
# Read and change settings in plain language, without opening any Microsoft portal.
#
# Settings live as Power Platform "environment variables". Changing one in the web UI
# means finding the solution, finding the variable, and knowing which magic string it
# wants. This does it by name, validates the value, and shows what everything is set
# to in words.
#
#   ./scripts/configure.sh                    show every setting and what it does
#   ./scripts/configure.sh notify on          turn change notifications on
#   ./scripts/configure.sh dryrun off         start writing to Google for real
#   ./scripts/configure.sh private on         hide details of private events
#   ./scripts/configure.sh calendar           choose the Google calendar from a list
#   ./scripts/configure.sh email you@x.com    where alerts go
#   ./scripts/configure.sh window 7 120       days back, days ahead
source "${0:A:h}/common.sh"
require_auth
require_az

# short name -> schema name, kind, and one line of plain English
typeset -A SCHEMA KIND BLURB
set_meta() { SCHEMA[$1]=$2; KIND[$1]=$3; BLURB[$1]=$4 }
set_meta dryrun   o3gc_DryRun                   bool "Practice mode: log what would change, write nothing to Google"
set_meta notify   o3gc_NotifyOnChange           bool "Email me whenever the mirror adds, changes or removes an event"
set_meta private  o3gc_HidePrivateEventDetails  bool "Show private events as just 'Busy', hiding subject and attendees"
set_meta attendees o3gc_CopyAttendeesAsGoogleAttendees bool "(unsupported) invite attendees on Google"
set_meta calendar o3gc_GoogleCalendarId         text "Which Google calendar receives the events"
set_meta source   o3gc_OutlookCalendarId        text "Which Outlook calendar is mirrored"
set_meta email    o3gc_AlertEmail               text "Where digests and warnings are sent"
set_meta site     o3gc_StateSiteUrl             text "Where the automation keeps its bookkeeping"
set_meta prefix   o3gc_TitlePrefix              text "Text put in front of mirrored titles ('none' for nothing)"
set_meta back     o3gc_WindowPastDays           num  "How many days of past meetings to mirror"
set_meta ahead    o3gc_WindowFutureDays         num  "How many days ahead to mirror"

ORDER=(dryrun notify private calendar source email back ahead prefix site)

ORG="$(org_url)"
[[ -n "$ORG" ]] || die "not connected to an environment"
TOKEN="$(token_for "$ORG")"
API="$ORG/api/data/v9.2"
H=(-H "Authorization: Bearer $TOKEN" -H "OData-MaxVersion: 4.0" -H "OData-Version: 4.0"
   -H "Accept: application/json" -H "Content-Type: application/json; charset=utf-8")

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

fetch_all() {
  curl -sS "${H[@]}" "$API/environmentvariabledefinitions?\$select=schemaname,defaultvalue,environmentvariabledefinitionid&\$filter=startswith(schemaname,%27o3gc_%27)" -o "$TMP/def.json"
  curl -sS "${H[@]}" "$API/environmentvariablevalues?\$select=value,_environmentvariabledefinitionid_value,environmentvariablevalueid" -o "$TMP/val.json"
}

show() {
  fetch_all
  # Each entry is a separate argument: the descriptions contain spaces, so joining
  # them into one string and splitting on whitespace mangles them.
  python3 - "$TMP/def.json" "$TMP/val.json" "$@" <<'PY'
import json, sys

defs = json.load(open(sys.argv[1])).get("value", [])
vals = json.load(open(sys.argv[2])).get("value", [])
rows = sys.argv[3:]

by_def = {v.get("_environmentvariabledefinitionid_value"): v.get("value") for v in vals}
effective = {}
for d in defs:
    v = by_def.get(d["environmentvariabledefinitionid"])
    effective[d["schemaname"]] = d.get("defaultvalue") if v is None else v

meta = {}
for chunk in rows:
    short, schema, kind, blurb = chunk.split("|", 3)
    meta[short] = (schema, kind, blurb)

print()
print("  Setting     Now        What it does")
print("  " + "-" * 86)
for short in meta:
    schema, kind, blurb = meta[short]
    if schema not in effective:
        shown = "NOT INSTALLED"
    elif (raw := effective.get(schema)) is None or not str(raw).strip():
        shown = "(unset)"
    elif kind == "bool":
        shown = "ON" if str(raw).lower() in ("yes", "true", "1") else "off"
    else:
        shown = str(raw)
        if len(shown) > 34:
            shown = shown[:31] + "..."
    print(f"  {short:<11} {shown:<10} {blurb}")
missing = [s for s, _, _ in meta.values() if s not in effective]
if missing:
    print("  Settings marked NOT INSTALLED are missing from this environment.")
    print("  Deploy the current version:  ./scripts/update.sh")
    print()
PY
}

set_value() {  # set_value <schema> <value>
  fetch_all
  local defid valid
  defid="$(python3 - "$TMP/def.json" "$1" <<'PY'
import json, sys
for d in json.load(open(sys.argv[1])).get("value", []):
    if d["schemaname"] == sys.argv[2]:
        print(d["environmentvariabledefinitionid"]); break
PY
)"
  [[ -n "$defid" ]] || die "no such setting in this environment: $1"
  valid="$(python3 - "$TMP/val.json" "$defid" <<'PY'
import json, sys
for v in json.load(open(sys.argv[1])).get("value", []):
    if v.get("_environmentvariabledefinitionid_value") == sys.argv[2]:
        print(v["environmentvariablevalueid"]); break
PY
)"
  local body code
  body="$(python3 - "$2" <<'PY'
import json, sys
print(json.dumps({"value": sys.argv[1]}))
PY
)"
  if [[ -n "$valid" ]]; then
    code="$(curl -sS -o "$TMP/r.json" -w '%{http_code}' -X PATCH "${H[@]}" -d "$body" \
      "$API/environmentvariablevalues($valid)" || true)"
  else
    body="$(python3 - "$2" "$defid" <<'PY'
import json, sys
print(json.dumps({
    "value": sys.argv[1],
    "EnvironmentVariableDefinitionId@odata.bind":
        f"/environmentvariabledefinitions({sys.argv[2]})",
}))
PY
)"
    code="$(curl -sS -o "$TMP/r.json" -w '%{http_code}' -X POST "${H[@]}" -d "$body" \
      "$API/environmentvariablevalues" || true)"
  fi
  if [[ "$code" == 20* ]]; then
    return 0
  fi
  warn "HTTP $code"
  head -c 400 "$TMP/r.json"; print ""
  return 1
}

pick_calendar() {
  info "Reading your Google calendars (via the Setup flow)"
  "$REPO_ROOT/scripts/run-flow.sh" 0 >/dev/null 2>&1
  "$REPO_ROOT/scripts/run-flow.sh" --outputs List_Google_Calendars 0 > "$TMP/cal.txt" 2>&1
  python3 - "$TMP/cal.txt" <<'PY' > "$TMP/menu.txt"
import json, re, sys
raw = open(sys.argv[1]).read()
m = re.search(r"\{.*", raw, re.S)
items = []
if m:
    try:
        doc = json.loads(m.group(0))
        items = doc.get("items") or doc.get("body", {}).get("items") or []
    except Exception:
        items = []
for c in items:
    if c.get("id") and c.get("accessRole") in ("owner", "writer"):
        print(f"{c.get('summary','(no name)')}\t{c['id']}")
PY
  if [[ ! -s "$TMP/menu.txt" ]]; then
    die "Could not read your Google calendars. Is flow 0 activated? ./scripts/enable-flows.sh 0"
  fi
  print ""
  print "  Calendars you can write to:"
  print ""
  local -a names ids
  local i=0
  while IFS=$'\t' read -r name id; do
    i=$((i+1)); names[$i]="$name"; ids[$i]="$id"
    printf "   %2d) %s\n" "$i" "$name"
  done < "$TMP/menu.txt"
  print ""
  print -n -P "%F{cyan}?%f Which one should receive your Outlook events? [1-$i] "
  read -r choice
  [[ "$choice" == <-> ]] && (( choice >= 1 && choice <= i )) || die "not a valid choice"
  info "Setting target to: ${names[$choice]}"
  set_value o3gc_GoogleCalendarId "${ids[$choice]}" && ok "done"
}

to_bool() {
  case "${1:l}" in
    on|yes|true|1|enable|enabled)   print "yes" ;;
    off|no|false|0|disable|disabled) print "no" ;;
    *) die "say on or off, not '$1'" ;;
  esac
}

if (( $# == 0 )); then
  print -P "%B O365GCal settings %b"
  print -P "%F{242} Environment: $(current_env)%f"
  META=()
  for k in $ORDER; do META+=("$k|${SCHEMA[$k]}|${KIND[$k]}|${BLURB[$k]}") done
  show "${META[@]}"
  cat <<'HELP'
  Change one with:   ./scripts/configure.sh <setting> <value>
    ./scripts/configure.sh dryrun off        start mirroring for real
    ./scripts/configure.sh notify on         email me on every change
    ./scripts/configure.sh calendar          pick a Google calendar from a list
    ./scripts/configure.sh window 7 120      days back, days ahead

  Changes take effect on the next run; no reinstall needed.
HELP
  exit 0
fi

KEY="$1"; shift
case "$KEY" in
  calendar)
    (( $# )) && { set_value o3gc_GoogleCalendarId "$1" && ok "target calendar set"; } || pick_calendar
    ;;
  window)
    (( $# == 2 )) || die "usage: configure.sh window <days-back> <days-ahead>"
    set_value o3gc_WindowPastDays "$1" && set_value o3gc_WindowFutureDays "$2" \
      && ok "window set to $1 days back, $2 days ahead"
    ;;
  *)
    [[ -n "${SCHEMA[$KEY]}" ]] || die "unknown setting '$KEY'. Run with no arguments to list them."
    (( $# )) || die "give a value, e.g. ./scripts/configure.sh $KEY on"
    if [[ "${KIND[$KEY]}" == "bool" ]]; then
      VALUE="$(to_bool "$1")"
    else
      VALUE="$1"
    fi
    set_value "${SCHEMA[$KEY]}" "$VALUE" && ok "$KEY is now $VALUE"
    ;;
esac

print ""
print -P "%F{242}  Verify with: ./scripts/configure.sh%f"
