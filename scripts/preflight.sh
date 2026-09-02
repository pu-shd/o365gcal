#!/bin/zsh
# Step 0 gate: can this environment actually run an Outlook + Google + SharePoint flow?
#
# Existing healthy connections do NOT prove the combination is allowed. DLP policy
# sorts connectors into Business / Non-Business / Blocked groups and a single flow may
# not span groups. Universities commonly place Google connectors in Non-Business and
# Office 365 in Business, which blocks this design at save time.
source "${0:A:h}/common.sh"
require_auth

info "Environment: $(current_env)"
print ""
info "Checking connections for the three required Standard connectors"

CONNS="$(pac connection list 2>/dev/null)"
for api in shared_office365 shared_googlecalendar shared_sharepointonline; do
  # A user often has several connections per connector, some stale. Only the
  # existence of at least one healthy connection matters here.
  matches="$(print -r -- "$CONNS" | grep -F "apis/$api" || true)"
  total=$(print -r -- "$matches" | grep -c . || true)
  healthy=$(print -r -- "$matches" | grep -ci "Connected" || true)
  if (( total == 0 )); then
    warn "$api - no connection found (you will be prompted to create one at import)"
  elif (( healthy > 0 )); then
    if (( healthy < total )); then
      ok "$api - Connected ($healthy of $total healthy; pick a working one at import)"
    else
      ok "$api - Connected"
    fi
  else
    warn "$api - $total connection(s) present but NONE healthy. Re-authorise it in"
    warn "    make.powerautomate.com > Connections before installing."
  fi
done

if print -r -- "$CONNS" | grep -q "apis/shared_outlook\b"; then
  print ""
  warn "You also have a 'shared_outlook' connection. That is the Outlook.com PERSONAL"
  warn "connector, a different API. This solution binds to shared_office365 - do not"
  warn "pick the personal one when setting connection references."
fi

cat <<'GATE'

────────────────────────────────────────────────────────────────────────
MANUAL STEP - the DLP same-group check (about 10 minutes, do this once)

Connections being healthy does not mean they may be combined. To settle it:

  1. make.powerautomate.com > + Create > Instant cloud flow > Manually trigger
  2. Add an action: Office 365 Outlook > "Get calendars (V2)"
  3. Add an action: Google Calendar > "List calendars"
  4. Add an action: SharePoint > "Send an HTTP request to SharePoint"
  5. Save.

  Saves cleanly            -> this design is viable. Delete the test flow.
  Error naming a DLP policy-> STOP. Outlook and Google are in different data
                              groups in your tenant. No solution-side change can
                              work around it; see docs/ADMIN.md for the fallback.
────────────────────────────────────────────────────────────────────────
GATE
