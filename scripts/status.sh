#!/bin/zsh
# What is installed, is it healthy, and is it actually running?
source "${0:A:h}/common.sh"
require_auth

print -P "%B O365GCal status %b"
print -P "%F{242} Environment: $(current_env)%f"
print ""

if solution_installed; then
  ok "installed - version $(solution_version)$(solution_is_managed && print -n ' (managed)' || print -n ' (unmanaged)')"
else
  warn "not installed in this environment"
  print "  Install with: ./scripts/bootstrap.sh"
  exit 0
fi

print ""
info "Connections"
for api in shared_office365 shared_googlecalendar shared_sharepointonline; do
  id="$(connection_id_for $api)"
  [[ -n "$id" ]] && ok "$api" || warn "$api - NO healthy connection (the mirror is stalled)"
done

print ""
info "Flows"
# Passed as a file: an inline --xml argument is mangled by shell quoting and pac
# terminates with a bare XmlException rather than a usable message.
FETCH="$(mktemp -t o365gcal-fetch)"
cat > "$FETCH" <<'XML'
<fetch>
  <entity name="workflow">
    <attribute name="name" />
    <attribute name="statecode" />
    <filter>
      <condition attribute="category" operator="eq" value="5" />
      <condition attribute="name" operator="like" value="O365GCal%" />
    </filter>
    <order attribute="name" />
  </entity>
</fetch>
XML
FLOWS="$(pac env fetch --xmlFile "$FETCH" 2>/dev/null | grep '^O365GCal' || true)"
rm -f "$FETCH"

if [[ -z "$FLOWS" ]]; then
  warn "no O365GCal flows found in this environment"
else
  print -r -- "$FLOWS" | while read -r line; do
    state="$(print -r -- "$line" | awk '{print $(NF-1)}')"
    name="$(print -r -- "$line" | sed -E 's/[[:space:]]+(Draft|Activated)[[:space:]]+.*$//')"
    if [[ "$state" == "Activated" ]]; then
      ok "$name"
    else
      warn "$name - $state (off)"
    fi
  done
  if ! print -r -- "$FLOWS" | grep -q "Activated"; then
    print ""
    print -P "%F{242}  All flows are off. This solution ships them off deliberately, so that"
    print -P "  nothing touches a real calendar before Setup has run. Binding the"
    print -P "  connections does not switch them on; turn them on in"
    print -P "  make.powerautomate.com > Solutions > O365GCal.%f"
  fi
fi

print ""
print "  Flow state and run history:  https://make.powerautomate.com > Solutions > O365GCal"
print "  What it has been doing:      the O365GCalLog list on your SharePoint site"
print ""
print "  Upgrade:  ./scripts/update.sh"
print "  Remove:   ./scripts/teardown.sh"
