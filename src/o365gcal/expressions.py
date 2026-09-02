"""Canonical Power Automate expression strings.

The flows embed these verbatim and `tests/validate/test_expression_parity.py`
asserts both that the flow JSON contains them and that evaluating them produces the
same result as the Python engine. That is what backs the claim that the tested logic
and the shipped logic are the same rules rather than two similar implementations.

Notation used throughout:
  `decodeUriComponent('%1F')`  -- the US separator; no literal for it exists in WDL.
  `parameters('X (o3gc_X)')`   -- environment variable reference.
  `items('Apply_to_each')`     -- current Outlook occurrence inside the reconcile loop.
"""

from __future__ import annotations

#: Environment variable reference. Display name and schema name both appear.
def env(name: str) -> str:
    return f"parameters('{name} (o3gc_{name})')"


def env_or(name: str, fallback: str = "''") -> str:
    """Read an optional environment variable, tolerating an unset value.

    Variables with no default omit `<defaultvalue>` and therefore evaluate to null
    until someone sets them. Passing null into `concat` fails the action, so every
    optional read goes through here."""
    return f"coalesce({env(name)}, {fallback})"


US = "decodeUriComponent('%1F')"

#: Second-precision UTC. Sub-second digits vary between reads of an unchanged event,
#: so including them would make the fingerprint unstable and cause endless updates.
#: Always fed from `startWithTimeZone` / `endWithTimeZone`; the bare `start` / `end`
#: fields are `date-no-tz` and would shift every event for a non-UTC mailbox.
def iso_utc(expr: str) -> str:
    return f"formatDateTime({expr}, 'yyyy-MM-ddTHH:mm:ss\\Z')"


def correlation_key(uid_expr: str, start_expr: str) -> str:
    return f"concat({uid_expr}, '|', {iso_utc(start_expr)})"


#: Line-ending and tab flattening, expressed without regex (which WDL does not have).
#: Runs of spaces are deliberately left alone -- collapsing them needs a regex, so the
#: reference engine does not do it either.
def collapse_ws(expr: str) -> str:
    return (
        f"trim(replace(replace(replace({expr}, "
        f"decodeUriComponent('%0A'), ' '), decodeUriComponent('%0D'), ' '), "
        f"decodeUriComponent('%09'), ' '))"
    )


def body_fingerprint(body_expr: str) -> str:
    """`{length}:{first 256 chars}` over the line-flattened raw body.

    Raw, not stripped: `GraphCalendarEventClientReceive` has no `bodyPreview` and the
    expression language has no regex, so the markup cannot be removed.
    """
    flat = collapse_ws(body_expr)
    return (
        f"concat(string(length({flat})), ':', "
        f"substring({flat}, 0, min(256, length({flat}))))"
    )


def attendees(required_expr: str, optional_expr: str) -> str:
    """Lowercased concatenation of the connector's two semicolon-separated strings.

    No splitting, sorting or deduplication -- none of which a flow can do -- so the
    reference engine deliberately does no more than this either.
    """
    return f"toLower(concat(trim({required_expr}), ';', trim({optional_expr})))"


# No to_google_attendees: attendees are never sent to Google. CreateEvent rejects an
# empty attendees string and a null one alike, and a flow cannot omit a parameter
# conditionally, so the parameter is left out of the write entirely.


#: Field order here must match `o365gcal.normalize.fingerprint` exactly.
#:
#: No `isCancelled`: the calendar view does not return it, so a flow cannot compute it.
#: Cancellation is detected by the occurrence disappearing from the read.
FINGERPRINT_FIELDS = [
    "effectiveSubject",
    "startUtc",
    "endUtc",
    "isAllDay",
    "location",
    "bodyFingerprint",
    "showAs",
    "sensitivity",
    "attendees",
    "organizer",
    "myResponse",
    "privacyMode",
]


def fingerprint(parts: dict[str, str]) -> str:
    """Join the twelve normalised fields with US into one comparable string."""
    joined = f", {US}, ".join(parts[name] for name in FINGERPRINT_FIELDS)
    return f"concat({joined})"


#: Whether an occurrence is inside the sync window. Rows outside it must never be
#: read as deletions -- ageing out of the window is not a cancellation.
IN_WINDOW = (
    "and("
    f"greaterOrEquals(item()?['OccurrenceStartUtc'], addDays(utcNow(), mul(-1, int({env('WindowPastDays')}))))"
    ", "
    f"lessOrEquals(item()?['OccurrenceStartUtc'], addDays(utcNow(), int({env('WindowFutureDays')})))"
    ")"
)

#: Circuit breaker. Conjunctive on purpose: a batch must be both a large share of the
#: mirror and over an absolute floor, so a sparse calendar can still lose an event.
CIRCUIT_BREAKER_TRIPPED = (
    "and("
    f"greater(length(variables('Deletes')), int({env('MinDeletesBeforeBreaker')}))"
    ", "
    "greater("
    "  div(mul(length(variables('Deletes')), 100), max(length(variables('ActiveRows')), 1))"
    f", int({env('MaxDeletePercent')})"
    ")"
    ")"
)

#: Mutations are applied create, then update, then delete, so a run cut short by the
#: cap has done its additive, reversible work before touching anything destructive.
THROTTLE_BUDGET_REMAINING = (
    f"sub(int({env('MaxMutationsPerRun')}), length(variables('Applied')))"
)

#: True when the user still owes an RSVP. Drives the digest's invitations section.
#: No isCancelled term: the calendar view omits cancelled occurrences entirely, so
#: anything present in the read is by definition not cancelled.
AWAITS_RESPONSE = (
    "contains(createArray('notResponded','tentativelyAccepted'), "
    "coalesce(item()?['responseType'], 'none'))"
)

#: Heartbeat staleness. The watchdog's whole purpose: an expired Google OAuth consent
#: or a switched-off flow produces no failure event for any in-flow handler to catch.
HEARTBEAT_STALE = (
    "less(item()?['LastSuccessUtc'], "
    f"addMinutes(utcNow(), mul(-1, int({env('HeartbeatStaleMinutes')}))))"
)

#: Private events under busy-only render as an opaque block.
def is_hidden(sensitivity_expr: str) -> str:
    return (
        f"and(equals({env('PrivacyMode')}, 'busy-only'), "
        f"contains(createArray('private','confidential'), {sensitivity_expr}))"
    )


# ---------------------------------------------------------------------------
# The flow-side fingerprint, field by field.
#
# This is the single canonical definition: `tools/patch_flows.py` writes it into the
# reconcile flow and `tests/validate/test_expression_parity.py` evaluates it against
# `o365gcal.normalize.fingerprint`. Three copies of these rules would drift; one
# generated from here cannot.
#
# Field names are the connector's own, taken from the live swagger:
#   * `startWithTimeZone` / `endWithTimeZone`, never `start` / `end` -- the latter are
#     `date-no-tz`, a bare wall-clock string that would shift every event for anyone
#     whose mailbox is not on UTC.
#   * `body`, because `GraphCalendarEventClientReceive` has no `bodyPreview`.
#   * `requiredAttendees` / `optionalAttendees`, semicolon-separated.
#   * no `isCancelled`: the calendar view does not return it.
# ---------------------------------------------------------------------------

HIDDEN = "outputs('Compose_Hidden')"


def _hide(expr: str) -> str:
    return f"if({HIDDEN}, '', {expr})"


FLOW_FINGERPRINT_PARTS: dict[str, str] = {
    "effectiveSubject": (
        f"if({HIDDEN}, 'Busy', trim(concat({env_or('TitlePrefix')}, coalesce(item()?['subject'], ''))))"
    ),
    "startUtc": iso_utc("item()?['startWithTimeZone']"),
    "endUtc": iso_utc("item()?['endWithTimeZone']"),
    "isAllDay": "if(coalesce(item()?['isAllDay'], false), '1', '0')",
    "location": _hide("trim(coalesce(item()?['location'], ''))"),
    "bodyFingerprint": _hide(body_fingerprint("coalesce(item()?['body'], '')")),
    "showAs": "toLower(trim(coalesce(item()?['showAs'], 'busy')))",
    "sensitivity": "coalesce(item()?['sensitivity'], 'normal')",
    "attendees": _hide(
        attendees("coalesce(item()?['requiredAttendees'], '')",
                  "coalesce(item()?['optionalAttendees'], '')")
    ),
    "organizer": _hide("toLower(trim(coalesce(item()?['organizer'], '')))"),
    "myResponse": "coalesce(item()?['responseType'], 'none')",
    "privacyMode": env("PrivacyMode"),
}


def flow_fingerprint() -> str:
    """The complete fingerprint expression as it appears in the reconcile flow."""
    return fingerprint(FLOW_FINGERPRINT_PARTS)


NL = "decodeUriComponent('%0A')"


def flow_description(key_expr: str = "outputs('Compose_Key')") -> str:
    """The Google event description.

    HTML bodies are linked rather than pasted: the connector returns a full Outlook
    HTML document and nothing in a flow can strip markup, so inlining it would produce
    an unreadable Google event. Attendees are rendered as text -- attaching them as
    real Google attendees would email an invitation to every one of them from the
    user's own Google account.
    """
    rule = "'──────────────────────────────────'"
    body = (
        "if(coalesce(item()?['isHtml'], true), "
        "if(empty(coalesce(item()?['body'], '')), '', "
        "'(HTML body — open the Outlook link below to read it)'), "
        "substring(coalesce(item()?['body'], ''), 0, min(1000, length(coalesce(item()?['body'], '')))))"
    )
    hidden_block = (
        f"concat({rule}, {NL}, 'Mirrored from Outlook · edits here will be overwritten', {NL}, "
        f"'Details hidden (private event, busy-only mode)', {NL}, "
        f"'o365gcal-key: ', {key_expr})"
    )
    visible_block = (
        f"concat({body}, {NL}, {NL}, {rule}, {NL}, "
        f"'Mirrored from Outlook · edits here will be overwritten', {NL}, "
        f"'Organizer:     ', coalesce(item()?['organizer'], ''), {NL}, "
        "'Your response: ', "
        "if(equals(coalesce(item()?['responseType'], 'none'), 'notResponded'), '⚠ Not responded', "
        "if(equals(coalesce(item()?['responseType'], 'none'), 'tentativelyAccepted'), "
        "'⚠ Tentative — not confirmed', coalesce(item()?['responseType'], 'none'))), "
        f"{NL}, 'Required:      ', replace(coalesce(item()?['requiredAttendees'], ''), ';', ','), "
        f"{NL}, 'Optional:      ', replace(coalesce(item()?['optionalAttendees'], ''), ';', ','), "
        f"{NL}, 'Respond in Outlook: ', coalesce(item()?['webLink'], ''), "
        f"{NL}, 'o365gcal-key: ', {key_expr})"
    )
    return f"if({HIDDEN}, {hidden_block}, {visible_block})"


