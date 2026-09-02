#!/bin/zsh
# Show what actually exists on the state site: which lists are there, and how many
# rows each holds.
#
# Worth having separately from status.sh: a flow reporting success proves only that
# its actions returned 2xx, not that the objects they were supposed to create are
# present and reachable under the names everything else expects.
source "${0:A:h}/common.sh"
require_auth
require_az

SITE="${1:-$(state_site_url)}"
[[ -n "$SITE" ]] || die "No state site. Pass one, or set StateSiteUrl and run flow 0."
SITE="${SITE%/}"

print -P "%B O365GCal state %b"
info "Site: $SITE"
print ""

HOST="$(print -r -- "$SITE" | sed -E 's|https://([^/]+).*|\1|')"
SITE_PATH="$(print -r -- "$SITE" | sed -E 's|https://[^/]+||')"
TOK="$(token_for 'https://graph.microsoft.com')"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

curl -sS -H "Authorization: Bearer $TOK" -H "Accept: application/json" \
  "https://graph.microsoft.com/v1.0/sites/${HOST}:${SITE_PATH}:/lists?\$select=id,name,displayName" \
  -o "$TMP/lists.json" || true

python3 - "$TMP/lists.json" <<'PY' > "$TMP/matched.txt"
import json, sys

WANT = ("O365GCalSyncMap", "O365GCalLog", "O365GCalHealth")
doc = json.load(open(sys.argv[1]))

if "error" in doc:
    print("ERROR\t" + doc["error"].get("message", "")[:200])
    raise SystemExit

lists = doc.get("value", [])
by_title = {l.get("displayName"): l for l in lists}
by_name = {l.get("name"): l for l in lists}

# An empty collection is not evidence of absence. Every personal site has at least a
# Documents library, so zero lists means this token cannot see them - the Azure CLI is
# pre-consented for only a narrow slice of Graph. Saying "NOT PRESENT" here would be
# the same unsound inference as reading a failed /me/drive read as "you have no
# OneDrive": a failed read tells you the read failed, nothing more.
if not lists:
    print("INCONCLUSIVE\tGraph returned no lists at all, not even Documents - this "
          "token cannot enumerate them")
    raise SystemExit

print(f"INFO\t{len(lists)} list(s) visible on this site")
for want in WANT:
    hit = by_title.get(want) or by_name.get(want)
    if hit:
        print(f"FOUND\t{want}\t{hit['id']}\t{hit.get('name','')}")
    else:
        print(f"MISSING\t{want}")

others = [l.get("displayName") for l in lists
          if l.get("displayName") not in WANT and l.get("name") not in WANT]
if others:
    print("OTHER\t" + ", ".join(sorted(x for x in others if x))[:300])
PY

if grep -q '^ERROR' "$TMP/matched.txt"; then
  warn "Could not enumerate lists: $(sed 's/^ERROR\t//' "$TMP/matched.txt")"
  print ""
  print "  Check it in a browser instead:"
  print "     ${SITE}/_layouts/15/viewlsts.aspx"
  exit 1
fi

MISSING=0
INCONCLUSIVE=0
while IFS=$'\t' read -r kind a b c; do
  case "$kind" in
    INFO)    info "$a" ;;
    FOUND)
      curl -sS -H "Authorization: Bearer $TOK" -H "Accept: application/json" \
        "https://graph.microsoft.com/v1.0/sites/${HOST}:${SITE_PATH}:/lists/${b}/items?\$select=id&\$top=999" \
        -o "$TMP/items.json" 2>/dev/null || true
      COUNT="$(python3 - "$TMP/items.json" <<'PY'
import json, sys
try:
    doc = json.load(open(sys.argv[1]))
except Exception:
    print("?"); raise SystemExit
print(len(doc["value"]) if "value" in doc else "?")
PY
)"
      ok "$a - $COUNT row(s)"
      ;;
    MISSING) warn "$a - not found among the visible lists"; MISSING=1 ;;
    OTHER)   print -P "%F{242}  also on this site: $a%f" ;;
    INCONCLUSIVE)
      warn "$a"
      INCONCLUSIVE=1
      ;;
  esac
done < "$TMP/matched.txt"

print ""
if (( INCONCLUSIVE )); then
  print "  This says nothing about whether the lists exist. Two better sources:"
  print ""
  print "    1. The browser, which is authoritative:"
  print "         ${SITE}/_layouts/15/viewlsts.aspx"
  print ""
  print "    2. Whether flow 3 succeeds. It reads the health list on every run, so a"
  print "       successful reconcile proves the lists exist and are reachable:"
  print "         ./scripts/run-flow.sh --runs 3"
  exit 0
fi

if (( MISSING )); then
  warn "One or more state lists were not among the visible lists."
  warn "Confirm in the browser before concluding they are absent:"
  print "     ${SITE}/_layouts/15/viewlsts.aspx"
  print ""
  warn "If they really are missing, run flow 0 and read its per-action records -"
  warn "the flow reports success even when provisioning fails, by design, so the"
  warn "action detail is the only place a failure shows:"
  print "     VERBOSE=1 ./scripts/run-flow.sh --detail 0"
  exit 1
fi
ok "All three state lists are present."
