"""Duplicate detection and sync-map rebuild, using only what is on Google.

The scenario this exists for: the solution is removed, the sync map goes with it, and
someone reinstalls. The new install has no record of what it created, so it mirrors
the whole calendar again. Now every meeting appears twice and nothing knows which
copy is which.

Recovery is possible because every mirrored event carries a marker in its
description:

    o365gcal-key: {iCalUId}|{occurrenceStartUtc}

That is the same correlation key the sync map uses, so Google itself holds enough
information to group duplicates, pick a survivor, and rebuild the map. A backup makes
this faster; it is not required.

Safety invariants, each covered by a test in tests/unit/test_dedup.py:

1. An event without the marker is never touched. Those are the user's own events.
2. A key with only one event is never touched. Deletion requires a proven duplicate.
3. Exactly one event survives every group - never zero.
4. The survivor is chosen deterministically, so repeated runs agree.
5. A run that cannot enumerate a slice completely refuses to delete from it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .model import Config, MapRow, SyncState
from .normalize import MARKER_PREFIX

#: The marker as it appears in a rendered description.
_MARKER = re.compile(
    re.escape(MARKER_PREFIX) + r"\s*([^\s|]+\|[0-9T:\-]+Z)"
)


@dataclass(frozen=True)
class GoogleEvent:
    """One event as the Google connector returns it."""

    id: str
    summary: str = ""
    description: str = ""
    start: datetime | None = None
    html_link: str = ""

    @property
    def marker_key(self) -> str | None:
        """The correlation key this event was created for, or None if unmarked."""
        m = _MARKER.search(self.description or "")
        return m.group(1) if m else None


@dataclass
class DedupAction:
    key: str
    keep: str
    delete: list[str]
    reason: str = ""


@dataclass
class DedupPlan:
    duplicates: list[DedupAction] = field(default_factory=list)
    #: Survivors with no sync-map row: the map can be rebuilt from these.
    map_repairs: list[MapRow] = field(default_factory=list)
    #: Marked events already correctly represented by a single map row.
    healthy: int = 0
    #: Events with no marker. Never candidates for anything.
    unmarked: int = 0
    #: Slices that could not be read completely; nothing in them is deleted.
    unreadable_slices: list[str] = field(default_factory=list)
    circuit_breaker_tripped: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def delete_ids(self) -> list[str]:
        return [i for a in self.duplicates for i in a.delete]

    def summary(self) -> dict:
        return {
            "duplicateGroups": len(self.duplicates),
            "eventsToDelete": len(self.delete_ids),
            "mapRowsToRebuild": len(self.map_repairs),
            "healthy": self.healthy,
            "unmarked": self.unmarked,
            "unreadableSlices": len(self.unreadable_slices),
            "circuitBreakerTripped": self.circuit_breaker_tripped,
            "warnings": list(self.warnings),
        }


def time_slices(start: datetime, end: datetime, days: int = 1) -> list[tuple[datetime, datetime]]:
    """Split a window into slices small enough to fit one unpaged response.

    `ListEvents` has no paging and returns results in arbitrary order, so the only way
    to enumerate a calendar reliably is to ask for ranges small enough that a single
    response is the whole answer. One day is the default because a calendar with more
    than a page of events in a single day is rare, and `saturated()` catches it when
    it happens rather than silently losing events.
    """
    out: list[tuple[datetime, datetime]] = []
    cursor = start
    step = timedelta(days=max(1, days))
    while cursor < end:
        nxt = min(cursor + step, end)
        out.append((cursor, nxt))
        cursor = nxt
    return out


def saturated(returned: int, page_hint: int = 250) -> bool:
    """Whether a slice's response may have been truncated.

    The connector documents no page size and gives no continuation token, so a full
    response is indistinguishable from a truncated one. Treating a suspiciously large
    response as complete would delete events that merely fell off the end, so it is
    treated as unreadable instead.
    """
    return returned >= page_hint


def choose_survivor(ids: list[str], mapped_id: str | None) -> str:
    """Pick which copy lives.

    Prefers the id the sync map already points at, so an existing install keeps
    working against the same event. Otherwise the lexicographically smallest id, which
    is arbitrary but *stable*: two runs over the same data must agree, or a repeated
    dedup would delete a different copy each time and eventually delete them all.
    """
    if mapped_id and mapped_id in ids:
        return mapped_id
    return sorted(ids)[0]


def build_dedup_plan(
    google_events: list[GoogleEvent],
    map_rows: list[MapRow],
    config: Config,
    unreadable_slices: list[str] | None = None,
) -> DedupPlan:
    """Group marked Google events by correlation key and decide what to remove."""
    plan = DedupPlan(unreadable_slices=list(unreadable_slices or []))
    by_key: dict[str, list[GoogleEvent]] = {}

    for event in google_events:
        key = event.marker_key
        if key is None:
            # Not ours. The user created it, or a mirrored event's description was
            # edited beyond recognition. Either way it is not a candidate.
            plan.unmarked += 1
            continue
        by_key.setdefault(key, []).append(event)

    rows_by_key = {r.correlation_key: r for r in map_rows}

    for key, events in sorted(by_key.items()):
        mapped = rows_by_key.get(key)
        mapped_id = mapped.google_event_id if mapped else None

        if len(events) == 1:
            plan.healthy += 1
            only = events[0]
            if mapped is None or mapped.google_event_id != only.id:
                plan.map_repairs.append(_row_for(key, only, config))
            continue

        ids = [e.id for e in events]
        keep = choose_survivor(ids, mapped_id)
        drop = [i for i in ids if i != keep]
        plan.duplicates.append(
            DedupAction(
                key=key,
                keep=keep,
                delete=drop,
                reason=(
                    f"{len(events)} copies of the same occurrence; keeping "
                    + ("the one the sync map references" if keep == mapped_id
                       else "the lowest event id for a stable, repeatable choice")
                ),
            )
        )
        if mapped is None or mapped.google_event_id != keep:
            survivor = next(e for e in events if e.id == keep)
            plan.map_repairs.append(_row_for(key, survivor, config))

    _apply_breaker(plan, google_events, config)
    return plan


def _row_for(key: str, event: GoogleEvent, config: Config) -> MapRow:
    """A rebuilt map row. `content_fingerprint` is deliberately left empty: the true
    value comes from Outlook, and the next reconcile will compute and store it. An
    invented one would mark a stale event as up to date."""
    ical_uid, _, start = key.partition("|")
    return MapRow(
        correlation_key=key,
        google_event_id=event.id,
        google_html_link=event.html_link,
        content_hash="",
        sync_state=SyncState.ACTIVE,
        outlook_ical_uid=ical_uid,
        occurrence_start_utc=event.start,
        last_synced_utc=None,
        last_error="rebuilt by dedup; fingerprint pending next reconcile",
    )


def _apply_breaker(plan: DedupPlan, events: list[GoogleEvent], config: Config) -> None:
    """The same all-or-nothing guard the reconciler uses.

    Deleting is irreversible and this routine runs precisely when things are already
    confused, so a large batch is refused outright rather than applied in part.
    """
    if plan.unreadable_slices:
        plan.circuit_breaker_tripped = True
        plan.circuit_breaker_reason = (
            f"{len(plan.unreadable_slices)} time slice(s) could not be read completely, "
            f"so duplicates in them cannot be distinguished from events that merely "
            f"fell off an unpaged response. No events were deleted. Narrow the slice "
            f"size and run again."
        )
        plan.warnings.append(plan.circuit_breaker_reason)
        plan.duplicates = []
        return

    total = len([e for e in events if e.marker_key])
    to_delete = len(plan.delete_ids)
    if not to_delete or not total:
        return
    if to_delete <= config.min_deletes_before_breaker:
        return
    share = to_delete / total * 100
    if share <= config.max_delete_percent:
        return

    plan.circuit_breaker_tripped = True
    plan.circuit_breaker_reason = (
        f"Refused to delete {to_delete} of {total} mirrored events ({share:.0f}%), over "
        f"the {config.max_delete_percent}% threshold. On a calendar this confused, a "
        f"partial delete is worse than none. Inspect the plan, then raise "
        f"MaxDeletePercent for one run if it is genuinely correct."
    )
    plan.warnings.append(plan.circuit_breaker_reason)
    plan.duplicates = []
