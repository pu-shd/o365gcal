"""Grouping outstanding invitations for the reminder email.

The first version of this listed one line per occurrence, which turned two recurring
series into seventeen near-identical lines - technically complete and practically
unreadable. Someone scanning that cannot tell whether they owe two replies or
seventeen.

The answer is to group by series and report the next occurrence plus a count, because
replying in Outlook answers the whole series at once. Sorting by that next occurrence
puts the ones that matter soonest at the top.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .model import AWAITING_RESPONSE, Config, OutlookEvent, ResponseType


@dataclass
class InvitationGroup:
    """One meeting or series awaiting a reply."""

    subject: str
    series_key: str
    next_start: datetime
    occurrences: int = 1
    response: ResponseType = ResponseType.NOT_RESPONDED
    organizer: str = ""
    web_link: str = ""

    @property
    def is_series(self) -> bool:
        return self.occurrences > 1

    @property
    def urgency_days(self) -> int | None:
        """Days until the next occurrence; None if it has no start."""
        return None


def days_until(start: datetime, now: datetime) -> int:
    start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
    return (start - now).days


def group_invitations(
    events: list[OutlookEvent], config: Config, now: datetime | None = None
) -> list[InvitationGroup]:
    """Collapse outstanding invitations into one entry per meeting or series.

    Grouped by `seriesMasterId` when present, else by `iCalUId`, so a single meeting
    and a whole series are handled by the same path. Only invitations inside the
    horizon are included: a meeting eight months out is not urgent and pushes the ones
    that are off the bottom of the email.
    """
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(days=config.rsvp_horizon_days)

    groups: dict[str, InvitationGroup] = {}
    for event in events:
        if event.my_response not in AWAITING_RESPONSE or event.is_cancelled:
            continue
        start = event.start_utc
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if start < now or start > horizon:
            continue

        key = event.series_master_id or event.ical_uid
        existing = groups.get(key)
        if existing is None:
            groups[key] = InvitationGroup(
                subject=event.subject or "(no subject)",
                series_key=key,
                next_start=start,
                response=event.my_response,
                organizer=event.organizer or "",
                web_link=event.web_link or "",
            )
            continue

        existing.occurrences += 1
        if start < existing.next_start:
            # Keep the soonest occurrence and its link: that is the one the reader
            # needs to act on.
            existing.next_start = start
            existing.web_link = event.web_link or existing.web_link

    # Soonest first: the reader's attention should land on what is most imminent.
    return sorted(groups.values(), key=lambda g: g.next_start)


def summarise(groups: list[InvitationGroup]) -> str:
    """One line of plain text for a subject line."""
    if not groups:
        return "no outstanding invitations"
    meetings = len(groups)
    soonest = groups[0]
    word = "invitation" if meetings == 1 else "invitations"
    return f"{meetings} {word} awaiting your reply, soonest {soonest.subject}"
