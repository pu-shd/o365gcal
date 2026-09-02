"""Data model shared by the engine and the flows.

Field names mirror the Office 365 Outlook connector's `GetEventsCalendarViewV3`
output and the SharePoint sync-map columns exactly, so a fixture captured from a
real flow run can be loaded here without translation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class SyncState(str, Enum):
    ACTIVE = "Active"
    DELETED = "Deleted"
    ERROR = "Error"
    SKIPPED = "Skipped"


class Operation(str, Enum):
    CREATE = "Create"
    UPDATE = "Update"
    DELETE = "Delete"
    NOOP = "NoOp"
    SKIP = "Skip"


class Level(str, Enum):
    INFO = "Info"
    WARN = "Warn"
    ERROR = "Error"


class ResponseType(str, Enum):
    """Graph `responseType`. `NONE` is what the organiser sees on their own event."""

    NONE = "none"
    ORGANIZER = "organizer"
    TENTATIVE = "tentativelyAccepted"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    NOT_RESPONDED = "notResponded"


#: Response states that mean the user still owes someone an answer. Surfaced in the
#: daily digest -- a Google-only user cannot otherwise discover these.
AWAITING_RESPONSE = frozenset({ResponseType.NOT_RESPONDED, ResponseType.TENTATIVE})


class Sensitivity(str, Enum):
    NORMAL = "normal"
    PERSONAL = "personal"
    PRIVATE = "private"
    CONFIDENTIAL = "confidential"


@dataclass
class OutlookEvent:
    """One *occurrence*. Recurring series arrive here already expanded by
    `GetEventsCalendarViewV3`, because the Google Calendar connector has no
    recurrence support whatsoever."""

    ical_uid: str
    event_id: str
    subject: str
    #: Populated from the connector's `startWithTimeZone` / `endWithTimeZone`. The
    #: plain `start` / `end` fields are `date-no-tz` -- a local wall-clock string with
    #: no offset at all -- so using them would silently shift every event for anyone
    #: whose mailbox is not on UTC.
    start_utc: datetime
    end_utc: datetime
    is_all_day: bool = False
    location: str = ""
    #: Raw body exactly as the connector returns it: a string that is HTML whenever
    #: `is_html` is set. `GraphCalendarEventClientReceive` has no `bodyPreview` -- that
    #: field exists only on mail types and on the deprecated V2 calendar backend -- and
    #: the expression language has no regex, so a flow cannot strip the markup. The
    #: fingerprint therefore uses a bounded slice rather than normalised text.
    body_html: str = ""
    is_html: bool = True
    #: The connector returns the organiser as a bare email string, not an object.
    organizer: str = ""
    #: Semicolon-separated, per the connector. Not comma - Google wants commas, so the
    #: separator is converted at the point of use rather than here.
    required_attendees: str = ""
    optional_attendees: str = ""
    resource_attendees: str = ""
    show_as: str = "busy"
    sensitivity: Sensitivity = Sensitivity.NORMAL
    #: Not supplied by the calendar view at all. Cancellation is detected in production
    #: by the occurrence disappearing from the read, which the calendar view guarantees.
    #: Retained because the diff still reasons about it and the live smoke test sets it.
    is_cancelled: bool = False
    my_response: ResponseType = ResponseType.NONE
    series_master_id: str = ""
    web_link: str = ""
    last_modified_utc: datetime | None = None

    @property
    def is_recurring_instance(self) -> bool:
        return bool(self.series_master_id)


@dataclass
class MapRow:
    """A row of the SharePoint `O365GCalSyncMap` list."""

    correlation_key: str
    google_event_id: str = ""
    google_html_link: str = ""
    content_hash: str = ""
    sync_state: SyncState = SyncState.ACTIVE
    outlook_event_id: str = ""
    outlook_ical_uid: str = ""
    outlook_series_master_id: str = ""
    occurrence_start_utc: datetime | None = None
    last_synced_utc: datetime | None = None
    last_outlook_modified_utc: datetime | None = None
    attendee_summary: str = ""
    my_response: str = ""
    error_count: int = 0
    last_error: str = ""
    owner_upn: str = ""


@dataclass
class PlannedOp:
    """One intended mutation. The reconciler produces these; flow 2 applies them."""

    operation: Operation
    correlation_key: str
    event: OutlookEvent | None = None
    map_row: MapRow | None = None
    reason: str = ""
    content_hash: str = ""

    @property
    def is_mutation(self) -> bool:
        return self.operation in (Operation.CREATE, Operation.UPDATE, Operation.DELETE)


@dataclass
class SyncPlan:
    """The full outcome of one reconcile pass, including what was deliberately
    withheld. Everything suppressed is reported rather than silently dropped."""

    creates: list[PlannedOp] = field(default_factory=list)
    updates: list[PlannedOp] = field(default_factory=list)
    deletes: list[PlannedOp] = field(default_factory=list)
    noops: list[PlannedOp] = field(default_factory=list)
    deferred: list[PlannedOp] = field(default_factory=list)
    circuit_breaker_tripped: bool = False
    circuit_breaker_reason: str = ""
    truncated_by_cap: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def mutations(self) -> list[PlannedOp]:
        return [*self.creates, *self.updates, *self.deletes]

    @property
    def mutation_count(self) -> int:
        return len(self.creates) + len(self.updates) + len(self.deletes)

    def summary(self) -> dict[str, Any]:
        return {
            "creates": len(self.creates),
            "updates": len(self.updates),
            "deletes": len(self.deletes),
            "noops": len(self.noops),
            "deferred": len(self.deferred),
            "circuitBreakerTripped": self.circuit_breaker_tripped,
            "truncatedByCap": self.truncated_by_cap,
            "warnings": list(self.warnings),
        }


@dataclass
class Config:
    """Mirrors the solution's environment variables one-for-one."""

    outlook_calendar_id: str = "Calendar"
    google_calendar_id: str = "primary"
    window_past_days: int = 7
    window_future_days: int = 120
    alert_email: str = ""
    dry_run: bool = False
    title_prefix: str = ""
    privacy_mode: str = "full"  # full | busy-only
    max_mutations_per_run: int = 60
    max_delete_percent: int = 25
    #: Absolute floor below which the percentage rule is not applied. Without it, a
    #: user with a sparse calendar deleting one of three meetings would hit 33% and
    #: have a perfectly legitimate deletion refused on every run.
    min_deletes_before_breaker: int = 5
    heartbeat_stale_minutes: int = 90
    log_retention_days: int = 90
    #: How long a sync-map row outlives the sync window before being pruned. Wide on
    #: purpose: pruning a row orphans its Google event, and a generous margin keeps
    #: that far away from any plausible widening of WindowPastDays.
    map_retention_days: int = 400
    #: A row recording a withdrawal has no Google event left to point at, so it can go
    #: much sooner - it is kept only as a short audit trail.
    deleted_row_retention_days: int = 30
    #: Row count at which the watchdog starts warning, below SharePoint's 5000-row
    #: view threshold so the warning arrives before reads begin truncating.
    list_size_warn_at: int = 4000
