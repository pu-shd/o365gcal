"""Detecting mirrored events that vanished from Google.

The gap this closes: the per-event decision is "no Google id -> create, fingerprint
changed -> update, otherwise nothing". An unchanged Outlook meeting short-circuits to
no-op before anything checks whether its Google copy still exists, so an event deleted
directly in Google stays missing until that meeting happens to change.

Verifying every event on every run would cost one Google call per event, against a
connector budget of 100 calls per 60 seconds - unaffordable for a calendar of any
size. Instead each run verifies one rotating slice of the mirror, so the whole window
is covered over `verify_slices` runs at a cost of roughly
`active_rows / verify_slices` calls per run.

The rotation is derived from the clock rather than stored, which keeps it stateless:
no cursor to persist, and nothing to go stale or need repairing itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .model import Config, MapRow

#: Quarter-hour slots in a day. The reconciler runs every 15 minutes, so one slot per
#: run, and the slot number is what selects the slice.
SLOTS_PER_DAY = 96


def current_slot(now: datetime, slices: int) -> int:
    """Which slice this run should verify.

    Derived from the wall clock so that consecutive runs pick different slices and the
    cycle repeats predictably: with 16 slices and a 15-minute cadence, every row is
    checked once every four hours.
    """
    if slices < 1:
        return 0
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    quarter = now.hour * 4 + now.minute // 15
    return quarter % slices


def rows_to_verify(
    rows: list[MapRow], now: datetime, config: Config
) -> list[MapRow]:
    """The slice of mirrored rows this run checks.

    Selection is `row id mod slices == slot`, which spreads rows evenly and does not
    depend on their order. Rows carrying no Google event id are skipped: there is
    nothing to verify, and the ordinary diff already treats them as needing a create.
    """
    slices = max(1, config.verify_slices)
    slot = current_slot(now, slices)
    chosen = [
        row for row in rows
        if row.google_event_id and _row_number(row) % slices == slot
    ]
    return chosen[: max(0, config.max_verify_per_run)]


def _row_number(row: MapRow) -> int:
    """A stable integer per row. SharePoint's item id when present, else a hash of the
    correlation key so the selection still spreads evenly in tests and in the engine."""
    if row.list_item_id is not None:
        return int(row.list_item_id)
    return abs(hash(row.correlation_key))


@dataclass
class VerifyOutcome:
    """What a verification pass found."""

    checked: list[MapRow] = field(default_factory=list)
    missing: list[MapRow] = field(default_factory=list)
    slot: int = 0
    slices: int = 1

    @property
    def coverage_runs(self) -> int:
        """How many runs a full sweep takes."""
        return self.slices

    def summary(self) -> dict:
        return {
            "slot": self.slot,
            "slices": self.slices,
            "checked": len(self.checked),
            "missing": len(self.missing),
        }


def verify(
    rows: list[MapRow],
    exists: dict[str, bool],
    config: Config,
    now: datetime | None = None,
) -> VerifyOutcome:
    """Decide which mirrored events have gone from Google.

    `exists` maps Google event id to whether a read found it. An id absent from the
    mapping is treated as *not checked* rather than missing: a failed read is not
    evidence of deletion, and acting on one would recreate events that are perfectly
    fine while the connector is merely throttling.
    """
    now = now or datetime.now(timezone.utc)
    slices = max(1, config.verify_slices)
    outcome = VerifyOutcome(slot=current_slot(now, slices), slices=slices)

    for row in rows_to_verify(rows, now, config):
        if row.google_event_id not in exists:
            continue
        outcome.checked.append(row)
        if not exists[row.google_event_id]:
            outcome.missing.append(row)
    return outcome


def repair(row: MapRow) -> MapRow:
    """Mark a row whose Google event has gone, so the next reconcile recreates it.

    Clearing the id rather than recreating here on the spot is deliberate: the create
    path already exists, is idempotent, and builds its payload from a fresh Outlook
    read. Duplicating that logic in a repair branch would be a second writer to keep
    in step. The cost is one extra cycle of latency.
    """
    row.google_event_id = ""
    row.content_hash = ""
    row.last_error = "Google copy was missing; queued for recreation"
    return row
