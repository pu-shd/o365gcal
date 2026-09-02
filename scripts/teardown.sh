#!/bin/zsh
#
# Remove O365GCal, in stages, confirming each one separately.
#
# The guarantee, stated plainly: nothing here deletes a single event from any
# calendar. Everything already mirrored to Google stays exactly as it is - it simply
# stops being updated. Outlook is only ever read, so it cannot be affected at all.
#
# Stages, each optional and each confirmed on its own:
#   1. stop the mirror        - flows off. Instant, fully reversible.
#   2. back up                - the sync map, so a reinstall does not duplicate events.
#   3. remove the solution    - deletes the six flows and the settings.
#   4. remove the state lists - off by default. See the warning at that stage.
#
#   ./scripts/teardown.sh            interactive
#   ./scripts/teardown.sh --stop     only stage 1: pause the mirror and stop
source "${0:A:h}/common.sh"
require_auth

STOP_ONLY=0
[[ "${1:-}" == "--stop" ]] && STOP_ONLY=1
[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && { sed -n '2,18p' "$0"; exit 0 }

print -P "%B O365GCal teardown %b"
info "Environment: $(current_env)"
print ""

if ! solution_installed; then
  warn "O365GCal is not installed here - nothing to remove."
  print ""
  print "  If mirrored events are still on your Google calendar, they are orphans now."
  print "  Find them by searching Google Calendar for:  o365gcal-key"
  exit 0
fi

ok "found O365GCal $(solution_version)"
print ""
print -P "%F{green}  Your calendars are safe.%f Nothing in this script deletes a calendar event."
print "  Mirrored Google events remain; they just stop being kept up to date."
print ""

# ---------------------------------------------------------------- stage 1
info "Stage 1/4  Stop the mirror"
print "  Switches the six flows off. Instant, and reversible with:"
print "     ./scripts/enable-flows.sh"
print ""
if confirm "Switch the flows off now?"; then
  "$REPO_ROOT/scripts/enable-flows.sh" --off 2>&1 | grep -E "Deactivating|HTTP" || true
  ok "the mirror is paused"
else
  info "left the flows running"
fi

if (( STOP_ONLY )); then
  print ""
  ok "Stopped at stage 1 as requested. The solution is still installed."
  exit 0
fi

# ---------------------------------------------------------------- stage 2
print ""
info "Stage 2/4  Back up before removing anything"
print "  The sync map is the only record of which Google events this automation"
print "  created. Without it, a future reinstall cannot tell its own events from"
print "  yours: it mirrors the calendar a second time and can never clean up the"
print "  first copy. Ten seconds now saves that."
print ""
if confirm "Take a backup?"; then
  "$REPO_ROOT/scripts/backup.sh" 2>&1 | tail -6
else
  warn "no backup taken"
fi

# ---------------------------------------------------------------- stage 3
print ""
info "Stage 3/4  Remove the solution"
print "  Removes: the six flows, the environment variables, the connection references."
print "  Keeps:   your connections, the three state lists, every Google event."
print ""
if confirm_destructive "This deletes the O365GCal solution from $(current_env)." "delete"; then
  pac solution delete --solution-name "$SOLUTION_NAME" >/dev/null 2>&1 \
    && ok "solution removed" \
    || die "deletion failed - the solution is still installed"
else
  print ""
  info "Kept the solution. Stopping here."
  print "  The flows may be switched off; turn them back on with:"
  print "     ./scripts/enable-flows.sh"
  exit 0
fi

# ---------------------------------------------------------------- stage 4
print ""
info "Stage 4/4  Remove the state lists"
SITE="${O365GCAL_STATE_SITE:-}"
print "  O365GCalSyncMap  which Google event corresponds to which Outlook meeting"
print "  O365GCalLog      the audit trail of everything the mirror did"
print "  O365GCalHealth   heartbeat records"
print ""
warn "Keep these if there is any chance you will reinstall."
print ""
print "  They are not deleted here, deliberately - the solution is already gone, so"
print "  this script can no longer read where they live. Remove them by hand from"
print "  your SharePoint or OneDrive site: Site contents, then delete the three lists."
print ""

ok "Teardown complete."
cat <<'NEXT'

  State now:
    - the automation is gone; nothing will change your Google calendar again
    - every event it already mirrored is still on Google, frozen as it was
    - your Outlook calendar was never modified

  To also remove the mirrored Google events, search Google Calendar for
  o365gcal-key and delete the matches - every event this tool created carries
  that marker in its description.

  To reinstall:  ./scripts/bootstrap.sh
  With history:  ./scripts/restore.sh backups/<timestamp>
NEXT
