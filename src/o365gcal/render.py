"""Renders the Google-side event payload.

Attendees are rendered as *text* into the description, and never attached as real
Google attendees. That was the design choice from the outset - a non-empty Google
attendee list makes Google email a calendar invitation to every Outlook attendee from
the user's own account, which for a one-way mirror means duplicate invites to real
colleagues for every meeting.

It is now also the only option the connector supports. `CreateEvent` rejects both an
empty `attendees` string and a null one with "Invalid attendee email", and a flow
cannot conditionally omit a parameter, so offering both modes would mean duplicating
every write path behind a condition. The parameter is therefore omitted entirely.
"""

from __future__ import annotations

from .model import AWAITING_RESPONSE, Config, OutlookEvent, ResponseType
from .normalize import MARKER_PREFIX, _is_hidden, correlation_key, effective_subject

RULE = "─" * 34

#: How much raw body to carry across when it is plain text. Bodies are unbounded and
#: the Google description should stay readable.
BODY_EXCERPT_CHARS = 1000

_RESPONSE_LABEL = {
    ResponseType.NONE: "—",
    ResponseType.ORGANIZER: "You are the organizer",
    ResponseType.ACCEPTED: "Accepted",
    ResponseType.DECLINED: "Declined",
    ResponseType.TENTATIVE: "⚠ Tentative — not confirmed",
    ResponseType.NOT_RESPONDED: "⚠ Not responded",
}


def response_label(response: ResponseType) -> str:
    return _RESPONSE_LABEL.get(response, str(response))


def awaits_response(event: OutlookEvent) -> bool:
    """True when the user still owes an RSVP. Drives the digest's invitations section."""
    return event.my_response in AWAITING_RESPONSE and not event.is_cancelled


def semicolons_to_commas(value: str) -> str:
    """The connector separates attendees with semicolons; Google's `attendees`
    parameter wants commas. Converting is a one-character substitution, which is
    exactly what a flow can do."""
    return (value or "").replace(";", ",").strip(", ")


def body_excerpt(event: OutlookEvent) -> str:
    """What of the Outlook body is worth putting on the Google event.

    The connector returns the body as raw HTML with no plain-text alternative, and
    neither a flow nor this module can strip markup without a regex. Dumping an
    Outlook HTML document into a Google description produces an unreadable mess, so
    HTML bodies are represented by a pointer to the original instead. Plain-text
    bodies are carried across, bounded.
    """
    if event.is_html:
        return "(HTML body — open the Outlook link below to read it)" if event.body_html else ""
    return (event.body_html or "").strip()[:BODY_EXCERPT_CHARS]


def render_description(event: OutlookEvent, config: Config) -> str:
    """Body excerpt + provenance/RSVP footer + the correlation marker."""
    hidden = _is_hidden(event, config)
    lines: list[str] = []

    if not hidden:
        excerpt = body_excerpt(event)
        if excerpt:
            lines.extend([excerpt, ""])

    lines.append(RULE)
    lines.append("Mirrored from Outlook · edits here will be overwritten")

    if hidden:
        lines.append("Details hidden (private event, busy-only mode)")
    else:
        if event.organizer:
            lines.append(f"Organizer:     {event.organizer}")
        lines.append(f"Your response: {response_label(event.my_response)}")
        for value, label in (
            (event.required_attendees, "Required:"),
            (event.optional_attendees, "Optional:"),
            (event.resource_attendees, "Resources:"),
        ):
            if value and value.strip():
                lines.append(f"{label:<14} {semicolons_to_commas(value)}")
        if event.is_cancelled:
            lines.append("Status:        CANCELLED in Outlook")

    if event.web_link:
        lines.append(f"Respond in Outlook: {event.web_link}")

    lines.append(f"{MARKER_PREFIX} {correlation_key(event.ical_uid, event.start_utc)}")
    return "\n".join(lines)


def render_google_event(event: OutlookEvent, config: Config) -> dict:
    """The exact parameter set handed to the Google connector's Create/Update.

    Keys are the connector's own: the body parameter is `newEvent` on create and
    `updatedEvent` on update -- not the `item` convention most Microsoft connectors
    use. Every field is always sent: update is a PATCH, so omitting a field would
    leave a stale value where Outlook has cleared one.

    `attendees` is absent by design. See the module docstring: the connector rejects
    both an empty string and a null, so the only way to mean "no attendees" is not to
    send the parameter.
    """
    return {
        "calendarId": config.google_calendar_id,
        "summary": effective_subject(event, config),
        "start": event.start_utc.isoformat(),
        "end": event.end_utc.isoformat(),
        "description": render_description(event, config),
        "location": "" if _is_hidden(event, config) else (event.location or ""),
        "status": "confirmed",
        "isAllDay": event.is_all_day,
    }


def render_attendee_summary(event: OutlookEvent) -> str:
    """Compact attendee digest stored on the sync-map row for reporting."""
    def count(value: str) -> int:
        return len([p for p in (value or "").split(";") if p.strip()])

    return (
        f"organizer={event.organizer}; required={count(event.required_attendees)}; "
        f"optional={count(event.optional_attendees)}; myResponse={event.my_response.value}"
    )
