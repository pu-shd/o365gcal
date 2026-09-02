"""Applies a SyncPlan -- the executable specification of flow 2 "Apply Event".

Flow 2 is the only place Google CRUD happens, in both the trigger path and the
reconcile path. Centralising it is what makes the two callers safe to run
concurrently and repeatedly: every operation here is idempotent.

`GoogleClient` is the seam the mocked integration tests plug into. In production the
same steps are performed by the Google Calendar connector actions CreateEvent /
UpdateEvent / DeleteEvent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from .model import (
    Config,
    Level,
    MapRow,
    Operation,
    PlannedOp,
    SyncPlan,
    SyncState,
)
from .render import render_attendee_summary, render_google_event


class GoogleNotFound(Exception):
    """Google returned 404/410 -- the event is already gone from their side."""


class GoogleThrottled(Exception):
    """Google returned 429. Retried with backoff by the connector's retry policy."""


class GoogleClient(Protocol):
    def create_event(self, payload: dict) -> dict: ...
    def update_event(self, event_id: str, payload: dict) -> dict: ...
    def delete_event(self, calendar_id: str, event_id: str) -> None: ...


@dataclass
class LogEntry:
    timestamp: datetime
    flow: str
    level: Level
    operation: Operation
    correlation_key: str
    message: str
    google_event_id: str = ""
    outlook_event_id: str = ""


@dataclass
class ApplyResult:
    rows: dict[str, MapRow] = field(default_factory=dict)
    logs: list[LogEntry] = field(default_factory=list)
    created: int = 0
    updated: int = 0
    deleted: int = 0
    noop: int = 0
    failed: int = 0
    repaired: int = 0

    def by_operation(self, operation: Operation) -> list[LogEntry]:
        return [entry for entry in self.logs if entry.operation == operation]


def apply_plan(
    plan: SyncPlan,
    client: GoogleClient,
    config: Config,
    existing_rows: list[MapRow] | None = None,
    now: datetime | None = None,
    flow: str = "3 Reconcile",
) -> ApplyResult:
    """Execute every mutation in `plan`, returning updated map rows and log entries.

    A failure on one event is recorded and skipped; it never aborts the batch, so one
    poisoned event cannot stall the whole mirror.
    """
    now = now or datetime.now(timezone.utc)
    result = ApplyResult(rows={r.correlation_key: r for r in (existing_rows or [])})

    def log(level: Level, op: Operation, key: str, message: str, gid: str = "", oid: str = "") -> None:
        result.logs.append(LogEntry(now, flow, level, op, key, message, gid, oid))

    if plan.circuit_breaker_tripped:
        log(Level.ERROR, Operation.SKIP, "", plan.circuit_breaker_reason)
    if plan.truncated_by_cap:
        log(Level.WARN, Operation.SKIP, "", plan.warnings[-1])

    for op in plan.noops:
        result.noop += 1

    for op in plan.mutations:
        if config.dry_run:
            log(Level.INFO, Operation.SKIP, op.correlation_key,
                f"DRY RUN: would {op.operation.value} ({op.reason})")
            continue
        try:
            _apply_one(op, client, config, result, now, log)
        except Exception as exc:  # noqa: BLE001 - mirrors the flow's Try/Catch scope
            result.failed += 1
            row = result.rows.get(op.correlation_key) or op.map_row
            if row:
                row.sync_state = SyncState.ERROR
                row.error_count += 1
                row.last_error = f"{type(exc).__name__}: {exc}"
                result.rows[op.correlation_key] = row
            log(Level.ERROR, op.operation, op.correlation_key,
                f"{op.operation.value} failed: {type(exc).__name__}: {exc}")

    return result


def _apply_one(op: PlannedOp, client: GoogleClient, config: Config,
               result: ApplyResult, now: datetime, log) -> None:
    if op.operation == Operation.CREATE:
        _do_create(op, client, config, result, now, log)
    elif op.operation == Operation.UPDATE:
        _do_update(op, client, config, result, now, log)
    elif op.operation == Operation.DELETE:
        _do_delete(op, client, config, result, now, log)


def _do_create(op, client, config, result, now, log) -> None:
    payload = render_google_event(op.event, config)
    created = client.create_event(payload)
    result.rows[op.correlation_key] = _row_from(op, created, now, config)
    result.created += 1
    log(Level.INFO, Operation.CREATE, op.correlation_key,
        f"Created on Google: {op.event.subject} ({op.reason})",
        created.get("id", ""), op.event.event_id)


def _do_update(op, client, config, result, now, log) -> None:
    payload = render_google_event(op.event, config)
    gid = op.map_row.google_event_id
    try:
        updated = client.update_event(gid, payload)
    except GoogleNotFound:
        # Someone deleted the mirrored event directly in Google. Re-create it and
        # repair the map, rather than leaving the row pointing at a dead id forever.
        updated = client.create_event(payload)
        result.repaired += 1
        log(Level.WARN, Operation.CREATE, op.correlation_key,
            f"Google event {gid} was missing; re-created and repaired map row",
            updated.get("id", ""), op.event.event_id)
        result.rows[op.correlation_key] = _row_from(op, updated, now, config)
        result.created += 1
        return
    result.rows[op.correlation_key] = _row_from(op, updated, now, config, fallback_id=gid)
    result.updated += 1
    log(Level.INFO, Operation.UPDATE, op.correlation_key,
        f"Updated on Google: {op.event.subject} ({op.reason})",
        updated.get("id", gid), op.event.event_id)


def _do_delete(op, client, config, result, now, log) -> None:
    row = op.map_row
    # We only ever delete an event whose id we recorded ourselves, so events the user
    # created natively in Google are never at risk.
    if not row or not row.google_event_id:
        log(Level.WARN, Operation.SKIP, op.correlation_key,
            "Delete skipped: no Google event id recorded")
        return
    try:
        client.delete_event(config.google_calendar_id, row.google_event_id)
    except GoogleNotFound:
        # Already gone. The desired end state, so treat as success.
        log(Level.INFO, Operation.DELETE, op.correlation_key,
            f"Google event {row.google_event_id} already absent; treated as deleted",
            row.google_event_id)
    row.sync_state = SyncState.DELETED
    row.last_synced_utc = now
    row.google_event_id = ""
    result.rows[op.correlation_key] = row
    result.deleted += 1
    log(Level.INFO, Operation.DELETE, op.correlation_key,
        f"Removed from Google ({op.reason})")


def _row_from(op: PlannedOp, google: dict, now: datetime, config: Config,
              fallback_id: str = "") -> MapRow:
    event = op.event
    row = op.map_row or MapRow(correlation_key=op.correlation_key)
    row.google_event_id = google.get("id", fallback_id)
    row.google_html_link = google.get("htmlLink", row.google_html_link)
    row.content_hash = op.content_hash
    row.sync_state = SyncState.ACTIVE
    row.outlook_event_id = event.event_id
    row.outlook_ical_uid = event.ical_uid
    row.outlook_series_master_id = event.series_master_id
    row.occurrence_start_utc = event.start_utc
    row.last_synced_utc = now
    row.last_outlook_modified_utc = event.last_modified_utc
    row.attendee_summary = render_attendee_summary(event)
    row.my_response = event.my_response.value
    row.error_count = 0
    row.last_error = ""
    return row
