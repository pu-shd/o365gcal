"""Growth, retention and the limits that fail quietly.

Three problems this module exists to solve, found by auditing what the flows actually
do rather than what they were meant to do.

**1. A silent correctness bug, not merely growth.** Reconcile reads the sync map with
`$top=5000`. SharePoint returns the first 5000 rows and says nothing about the rest.
Rows past the cap are invisible to the diff, so their occurrences look unmirrored and
reconcile *creates them again* - duplicating events while reporting success. A read
that might be short is therefore treated as a reason to abandon the run, not to
proceed on partial data.

**2. The sync map never shrank.** Nothing deleted a row. Every occurrence ever
mirrored kept its row forever, so a calendar with five meetings a day crosses 5000
rows in about three years and then hits problem 1.

**3. The log's pruning could not keep up.** The watchdog deletes rows older than
`LogRetentionDays`, but only 200 per run. Running hourly that is 4800 rows a day,
which is fine - but nothing checked whether it was keeping up, and nothing warned as
the list approached the threshold.

SharePoint's 5000-item limit is a view threshold, not a hard cap: filters on indexed
columns still work beyond it, which is why Title, SyncState, Timestamp and
OccurrenceStartUtc are indexed. What does not survive is an unbounded read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .model import Config, MapRow, SyncState

#: SharePoint's list view threshold. Not a storage limit - a query limit.
LIST_VIEW_THRESHOLD = 5000

#: The page size every list read asks for.
READ_TOP = 5000


def read_saturated(returned: int, requested: int = READ_TOP) -> bool:
    """Whether a list read may have been truncated.

    SharePoint gives no continuation token here, so a full page is indistinguishable
    from a truncated one. Assuming completeness is what turns a large calendar into
    duplicated events.
    """
    return returned >= requested


@dataclass
class ListHealth:
    name: str
    rows: int
    saturated: bool
    warn_at: int

    @property
    def state(self) -> str:
        if self.saturated:
            return "saturated"
        if self.rows >= self.warn_at:
            return "approaching"
        return "ok"

    @property
    def message(self) -> str:
        if self.saturated:
            return (
                f"{self.name} returned a full page ({self.rows}). The read may be "
                f"incomplete, so anything derived from it is unsafe."
            )
        if self.rows >= self.warn_at:
            pct = self.rows / LIST_VIEW_THRESHOLD * 100
            return (
                f"{self.name} holds {self.rows} rows, {pct:.0f}% of SharePoint's "
                f"{LIST_VIEW_THRESHOLD}-row view threshold."
            )
        return f"{self.name}: {self.rows} rows"


def assess_list(name: str, rows: int, config: Config) -> ListHealth:
    return ListHealth(name, rows, read_saturated(rows), config.list_size_warn_at)


@dataclass
class PrunePlan:
    """Which sync-map rows may be removed, and why."""

    stale_active: list[MapRow] = field(default_factory=list)
    settled_deleted: list[MapRow] = field(default_factory=list)
    kept: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def all_rows(self) -> list[MapRow]:
        return [*self.settled_deleted, *self.stale_active]

    def summary(self) -> dict:
        return {
            "staleActive": len(self.stale_active),
            "settledDeleted": len(self.settled_deleted),
            "kept": self.kept,
            "totalPruned": len(self.all_rows),
            "warnings": list(self.warnings),
        }


def build_prune_plan(
    rows: list[MapRow], config: Config, now: datetime | None = None
) -> PrunePlan:
    """Decide which map rows are safe to delete.

    Two categories, with different reasoning:

    * **Settled deletions.** The row records that we withdrew an event from Google.
      There is nothing left to point at, so once it is older than
      `deleted_row_retention_days` it carries no information.

    * **Stale active rows.** The occurrence sits far enough in the past that the sync
      window will never include it again, so reconcile will never consult the row.
      Removing it *orphans* the Google event - the automation forgets it created it.
      For a meeting that happened over a year ago that is harmless: it will never be
      updated again. It is not free, though: if someone later widens
      `WindowPastDays` far enough to reach back over an orphan, it would be mirrored
      a second time. Flow 7 finds such duplicates by their marker, and the retention
      default is deliberately far wider than the window to make the case unlikely.

    A row inside the window is never pruned, whatever its age.
    """
    now = now or datetime.now(timezone.utc)
    plan = PrunePlan()

    window_floor = now - timedelta(days=config.window_past_days)
    stale_floor = window_floor - timedelta(days=config.map_retention_days)
    deleted_floor = now - timedelta(days=config.deleted_row_retention_days)

    for row in rows:
        start = row.occurrence_start_utc
        if start is not None and start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)

        if row.sync_state == SyncState.DELETED:
            settled = row.last_synced_utc or start
            if settled is not None and settled.tzinfo is None:
                settled = settled.replace(tzinfo=timezone.utc)
            if settled is not None and settled < deleted_floor:
                plan.settled_deleted.append(row)
                continue
            plan.kept += 1
            continue

        if start is None:
            # No occurrence date means we cannot prove it is out of range. Keep it;
            # an unprunable row is a nuisance, a wrongly pruned one orphans an event.
            plan.kept += 1
            continue

        if start < stale_floor:
            plan.stale_active.append(row)
        else:
            plan.kept += 1

    return plan


def projected_rows_per_year(events_per_day: float, config: Config) -> int:
    """Rough sync-map growth, for sizing retention against the threshold."""
    return int(events_per_day * 365)


def retention_is_sane(config: Config) -> list[str]:
    """Configuration combinations that would defeat the pruning.

    Checked because these are easy to set to something that looks reasonable and
    quietly disables the protection.
    """
    problems: list[str] = []
    if config.map_retention_days < config.window_future_days:
        problems.append(
            f"MapRetentionDays ({config.map_retention_days}) is shorter than "
            f"WindowFutureDays ({config.window_future_days}). Rows could be pruned "
            f"while their occurrence is still inside the sync window, and reconcile "
            f"would mirror those events again."
        )
    if config.deleted_row_retention_days < 1:
        problems.append(
            "DeletedRowRetentionDays below 1 would delete withdrawal records in the "
            "same run that created them, losing the audit trail of what was removed."
        )
    if config.list_size_warn_at >= LIST_VIEW_THRESHOLD:
        problems.append(
            f"ListSizeWarnAt ({config.list_size_warn_at}) is at or above the "
            f"{LIST_VIEW_THRESHOLD}-row threshold, so the warning would arrive only "
            f"once reads were already truncating."
        )
    return problems
