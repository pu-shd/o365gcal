#!/bin/zsh
# Pull portal edits back into solution/src so the repo stays the source of truth.
#
# Power Automate rewrites flow JSON when a flow is edited in the maker portal. Without
# a round-trip the repo silently drifts from what is actually deployed.
source "${0:A:h}/common.sh"
require_auth

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

info "Exporting $SOLUTION_NAME from $(current_env)"
pac solution export --name "$SOLUTION_NAME" --path "$TMP" --managed false --overwrite

info "Unpacking over solution/src"
pac solution unpack \
  --zipfile "$TMP/${SOLUTION_NAME}.zip" \
  --folder "$SRC_DIR" \
  --packagetype Unmanaged \
  --allowDelete \
  --errorlevel Info

ok "solution/src updated."
print ""
print "Review the diff before committing - exports also churn version numbers and"
print "connection reference ids:"
print "  git diff --stat solution/src"
