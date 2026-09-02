#!/bin/zsh
# Trigger an O365GCal flow and report what happened, without the maker portal.
#
# The portal hides commands when an account lacks environment privileges, and pac
# cannot run a flow at all. The Flow API can do both, which also means bootstrap can
# run Setup unattended instead of asking the installer to click Run.
#
#   ./scripts/run-flow.sh 0            trigger flow 0 and wait for the result
#   ./scripts/run-flow.sh --runs 0     show recent runs of flow 0
#   ./scripts/run-flow.sh --runs 3 5   recent runs of flows 3 and 5
#   ./scripts/run-flow.sh --apply 7   flow 7 only: actually delete duplicates
#   ./scripts/run-flow.sh --detail 3  per-action detail of the NEWEST run
#   ./scripts/run-flow.sh --failed 3  per-action detail of the newest FAILED run
set -euo pipefail

autoload -U colors && colors
ok()   { print -P "%F{green} ok%f $*" }
warn() { print -P "%F{yellow}  ! %f$*" }
info() { print -P "%F{cyan}==>%f $*" }
die()  { print -P "%F{red}error:%f $*" >&2; exit 1 }

MODE="run"
APPLY=0
[[ "${1:-}" == "--runs" ]]   && { MODE="runs";   shift }
[[ "${1:-}" == "--detail" ]] && { MODE="detail"; shift }
# Investigate the last failure rather than the last run.
[[ "${1:-}" == "--failed" ]] && { MODE="detail"; WANT_FAILED=1; shift }
# Reads an action's actual outputs. Run status alone says a flow finished, not what it
# computed - and for a dry run, what it computed is the entire point.
[[ "${1:-}" == "--outputs" ]] && { MODE="outputs"; ACTION="$2"; shift 2 }
# Only flow 7 reads this. Without it that flow reports what it would do and changes
# nothing, which is the right default for something that deletes calendar events.
[[ "${1:-}" == "--apply" ]]  && { APPLY=1;       shift }
[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && { sed -n '2,12p' "$0"; exit 0 }
(( $# )) || die "which flow? e.g. ./scripts/run-flow.sh 0"

ORG="$(pac org who 2>/dev/null | awk -F': +' '/Org URL/{print $2}' | tr -d '[:space:]')"
ENV_ID="${O365GCAL_ENV_ID:-$(pac org who 2>/dev/null | awk -F': +' '/Environment ID/{print $2}' | tr -d '[:space:]')}"
[[ -n "$ORG" && -n "$ENV_ID" ]] || die "Could not read org/environment from pac."
ORG="${ORG%/}"
TENANT="${O365GCAL_TENANT:-$(az account show --query tenantId -o tsv 2>/dev/null || true)}"

FLOW_RES="https://service.flow.microsoft.com/"
get_flow_token() { az account get-access-token --resource "$FLOW_RES" --query accessToken -o tsv 2>/dev/null || true }
FTOKEN="$(get_flow_token)"
if [[ -z "$FTOKEN" ]]; then
  warn "Signing in for Power Automate access."
  az login --tenant "$TENANT" --scope "${FLOW_RES}.default" >/dev/null || die "az login failed."
  FTOKEN="$(get_flow_token)"
  [[ -n "$FTOKEN" ]] || die "Could not obtain a Power Automate token."
fi
DTOKEN="$(az account get-access-token --resource "$ORG" --query accessToken -o tsv 2>/dev/null || true)"
[[ -n "$DTOKEN" ]] || die "Could not obtain a Dataverse token."

REPO_SRC="${0:A:h:h}/solution/src"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
API="https://api.flow.microsoft.com/providers/Microsoft.ProcessSimple/environments/$ENV_ID"
FH=(-H "Authorization: Bearer $FTOKEN" -H "Accept: application/json" -H "Content-Type: application/json")

curl -sS -H "Authorization: Bearer $DTOKEN" -H "Accept: application/json" \
  "$ORG/api/data/v9.2/workflows?\$select=name,workflowid&\$filter=category%20eq%205%20and%20startswith(name,%27O365GCal%27)" \
  -o "$TMP/flows.json"

flow_id_for() {
  python3 - "$TMP/flows.json" "$1" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
want = sys.argv[2]
for r in doc.get("value", []):
    label = r["name"].replace("O365GCal ", "")
    if label.startswith(want):
        print(r["workflowid"]); break
PY
}

# Per-action detail for the most recent failed run. "An action failed. No dependent
# actions succeeded." is all the run summary ever says, which names nothing.
show_detail() {  # show_detail <flow-id> <label>
  curl -sS "${FH[@]}" "$API/flows/$1/runs?api-version=2016-11-01&\$top=10" -o "$TMP/runs.json" || true
  # The NEWEST run, not the newest failed one. Preferring failures meant a stale
  # failure from hours earlier was presented while investigating what just happened -
  # which is how a successful live run got read as a failure. Use --failed to hunt
  # deliberately for the last failure instead.
  RUN="$(python3 - "$TMP/runs.json" "${WANT_FAILED:-0}" <<'PY'
import json, sys
runs = json.load(open(sys.argv[1])).get("value", [])
if sys.argv[2] == "1":
    runs = [r for r in runs if r.get("properties", {}).get("status") == "Failed"] or runs
print(runs[0]["name"] if runs else "")
PY
)"
  if [[ -z "$RUN" ]]; then
    warn "$2 - no runs at all"
    return
  fi
  curl -sS "${FH[@]}" "$API/flows/$1/runs/$RUN/actions?api-version=2016-11-01" -o "$TMP/actions.json" || true
  python3 - "$TMP/actions.json" "$2" "$RUN" <<'PY'
import json, sys

import os

doc = json.load(open(sys.argv[1]))
label, run = sys.argv[2], sys.argv[3]
VERBOSE = os.environ.get("VERBOSE") == "1"
print(f"  {label}  (run {run})")
if "error" in doc:
    print(f"    action detail unavailable: {doc['error'].get('message','')[:200]}")
    raise SystemExit

acts = doc.get("value", [])
if not acts:
    print("    no action records returned")
fails = [a for a in acts if a.get("properties", {}).get("status") not in ("Succeeded", "Skipped")]
print(f"    {len(acts)} action record(s), {len(fails)} not succeeded")
for a in acts:
    p = a.get("properties", {})
    status = p.get("status", "?")
    if status == "Succeeded" and not VERBOSE:
        continue
    print(f"    {status:12} {a.get('name','?')}")
    err = p.get("error") or {}
    if err:
        print(f"        code    : {err.get('code','')}")
        print(f"        message : {str(err.get('message',''))[:700]}")
    code = p.get("code")
    if code and not err:
        print(f"        code    : {code}")
PY
}

show_outputs() {  # show_outputs <flow-id> <label> <action-name>
  curl -sS "${FH[@]}" "$API/flows/$1/runs?api-version=2016-11-01&\$top=1" -o "$TMP/runs.json" || true
  RUN="$(python3 - "$TMP/runs.json" <<'PY'
import json, sys
runs = json.load(open(sys.argv[1])).get("value", [])
print(runs[0]["name"] if runs else "")
PY
)"
  [[ -n "$RUN" ]] || { warn "$2 - no runs"; return }
  curl -sS "${FH[@]}" "$API/flows/$1/runs/$RUN/actions?api-version=2016-11-01" -o "$TMP/actions.json" || true
  LINK="$(python3 - "$TMP/actions.json" "$3" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
for a in doc.get("value", []):
    if a.get("name") == sys.argv[2]:
        print((a.get("properties", {}).get("outputsLink") or {}).get("uri", ""))
        break
PY
)"
  if [[ -z "$LINK" ]]; then
    warn "$2 - no outputs recorded for action '$3'"
    return
  fi
  info "$2 -> $3 (run $RUN)"
  curl -sS "$LINK" -o "$TMP/out.json" || true
  python3 - "$TMP/out.json" <<'PY'
import json, sys

try:
    doc = json.load(open(sys.argv[1]))
except Exception:
    print("  (outputs unreadable)")
    raise SystemExit

# A Compose action's output may be a scalar, not an object, so this cannot assume a
# body key or a dict.
body = doc.get("body", doc) if isinstance(doc, dict) else doc

# The size is reported before any truncation. Counting lines of a clipped dump gave a
# wrong answer once already - 6 rows for a 19-row list - and a measurement that is
# quietly capped is worse than no measurement.
if isinstance(body, list):
    print(f"  [array of {len(body)} item(s)]")
elif isinstance(body, dict) and isinstance(body.get("value"), list):
    print(f"  [object with value: array of {len(body['value'])} item(s)]")

text = body if isinstance(body, str) else json.dumps(body, indent=2, default=str)
lines = str(text).splitlines()
for line in lines[:40]:
    print("  " + line)
if len(lines) > 40:
    print(f"  ... {len(lines) - 40} more line(s) not shown (size reported above)")
PY
}

show_runs() {  # show_runs <flow-id> <label>
  curl -sS "${FH[@]}" "$API/flows/$1/runs?api-version=2016-11-01&\$top=${RUNS_TOP:-5}" -o "$TMP/runs.json" || true
  python3 - "$TMP/runs.json" "$2" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
label = sys.argv[2]
if "error" in doc:
    print(f"  {label}: {doc['error'].get('message','')[:200]}")
    raise SystemExit
runs = doc.get("value", [])
if not runs:
    print(f"  {label}: no runs yet")

import os
cap = int(os.environ.get("RUNS_TOP", "5"))
if len(runs) >= cap:
    print(f"  [showing {len(runs)}, the requested maximum - the list is CAPPED, so do "
          f"not count these as a total]")

for r in runs:
    p = r.get("properties", {})
    status = p.get("status", "?")
    start = (p.get("startTime") or "")[:19]
    err = (p.get("error") or {})
    line = f"  {label:34} {status:12} {start}"
    print(line)
    if err:
        print(f"      code    : {err.get('code','')}")
        print(f"      message : {str(err.get('message',''))[:600]}")
PY
}

for n in "$@"; do
  ID="$(flow_id_for "$n")"
  [[ -n "$ID" ]] || { warn "no flow matching '$n'"; continue }
  LABEL="$(python3 - "$TMP/flows.json" "$ID" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1]))
print(next(r["name"] for r in doc["value"] if r["workflowid"] == sys.argv[2]))
PY
)"

  if [[ "$MODE" == "runs" ]]; then
    info "Recent runs - $LABEL"
    show_runs "$ID" "$LABEL"
    continue
  fi

  if [[ "$MODE" == "outputs" ]]; then
    show_outputs "$ID" "$LABEL" "$ACTION"
    continue
  fi

  if [[ "$MODE" == "detail" ]]; then
    info "Action detail - $LABEL"
    show_detail "$ID" "$LABEL"
    continue
  fi

  # Scheduled flows have no "manual" trigger; the endpoint needs the trigger's real
  # name, which is read from the solution source rather than guessed.
  TRIGGER="$(python3 - "$REPO_SRC" "$LABEL" <<'PY'
import glob, json, sys
src, label = sys.argv[1], sys.argv[2]
want = label.replace("O365GCal ", "").split(" ")[0]
for path in sorted(glob.glob(f"{src}/Workflows/*.json")):
    doc = json.load(open(path))
    for wf in [doc]:
        triggers = wf["properties"]["definition"]["triggers"]
        desc = wf["properties"]["definition"].get("description", "")
        if f"-{want}-" in path or f"-{want}" in path.split("/")[-1].split("-")[1:2]:
            print(next(iter(triggers))); raise SystemExit
# fall back: match by leading number in the file name
for path in sorted(glob.glob(f"{src}/Workflows/*.json")):
    parts = path.split("/")[-1].split("-")
    if len(parts) > 1 and parts[1] == want:
        doc = json.load(open(path))
        print(next(iter(doc["properties"]["definition"]["triggers"]))); raise SystemExit
print("manual")
PY
)"
  [[ -n "$TRIGGER" ]] || TRIGGER="manual"
  info "Triggering $LABEL (trigger: $TRIGGER)"
  BODY='{}'
  if (( APPLY )); then
    BODY='{"boolean": true}'
    warn "--apply given: this run will DELETE duplicate Google events."
    print -n -P "%F{red}?%f Type %Bapply%b to confirm: "
    read -r reply
    [[ "$reply" == "apply" ]] || die "not confirmed; nothing was run"
  fi
  CODE="$(curl -sS -o "$TMP/trig.json" -w '%{http_code}' -X POST "${FH[@]}" -d "$BODY" \
    "$API/flows/$ID/triggers/$TRIGGER/run?api-version=2016-11-01" || true)"
  if [[ "$CODE" != "200" && "$CODE" != "202" && "$CODE" != "201" ]]; then
    warn "trigger returned HTTP $CODE"
    head -c 700 "$TMP/trig.json"; print ""
    continue
  fi
  ok "triggered; waiting for it to finish"

  for i in {1..40}; do
    sleep 6
    curl -sS "${FH[@]}" "$API/flows/$ID/runs?api-version=2016-11-01&\$top=1" -o "$TMP/one.json" || true
    STATE="$(python3 - "$TMP/one.json" <<'PY'
import json, sys
try:
    doc = json.load(open(sys.argv[1]))
    runs = doc.get("value") or []
    print(runs[0]["properties"].get("status", "") if runs else "")
except Exception:
    print("")
PY
)"
    [[ "$STATE" == "Running" || -z "$STATE" ]] || break
    print -n "."
  done
  print ""
  show_runs "$ID" "$LABEL"
done
