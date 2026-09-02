"""The reconcile decision: given Outlook truth and our sync map, what should change?

This module is the safety-critical part of the system. Deletion is the only
irreversible operation, and the reconciler infers deletions from *absence* -- an
event missing from the Outlook read is assumed cancelled. But a throttled response,
an expired token, a lost mailbox permission and a genuine mass cancellation all look
identical from here: they all present as "Outlook returned fewer events than we have
rows for". Hence the circuit breaker below.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .model import (
    Config,
    MapRow,
    Operation,
    OutlookEvent,
    PlannedOp,
    SyncPlan,
    SyncState,
)
from .normalize import content_hash, correlation_key

#: Order mutations are applied in. Deletes run last so that a run truncated by the
#: throttle cap has done its additive, reversible work before touching anything
#: destructive.
_MUTATION_ORDER = (Operation.CREATE, Operation.UPDATE, Operation.DELETE)


def in_window(event_start: datetime, now: datetime, config: Config) -> bool:
    """Is this occurrence inside the configured sync window?"""
    start = event_start if event_start.tzinfo else event_start.replace(tzinfo=timezone.utc)
    lower = now - timedelta(days=config.window_past_days)
    upper = now + timedelta(days=config.window_future_days)
    return lower <= start <= upper


def build_plan(
    outlook_events: list[OutlookEvent],
    map_rows: list[MapRow],
    config: Config,
    now: datetime | None = None,
) -> SyncPlan:
    """Diff Outlook against the sync map and return an ordered, guarded plan.

    Nothing here performs I/O; the caller (flow 3) applies the plan through flow 2.
    """
    now = now or datetime.now(timezone.utc)
    plan = SyncPlan()

    by_key: dict[str, MapRow] = {r.correlation_key: r for r in map_rows}
    seen: set[str] = set()

    for event in outlook_events:
        key = correlation_key(event.ical_uid, event.start_utc)
        seen.add(key)
        row = by_key.get(key)
        digest = content_hash(event, config)

        if event.is_cancelled:
            # A cancelled occurrence that we mirrored must be withdrawn from Google.
            if row and row.google_event_id and row.sync_state == SyncState.ACTIVE:
                plan.deletes.append(
                    PlannedOp(Operation.DELETE, key, event, row, "cancelled in Outlook", digest)
                )
            else:
                plan.noops.append(
                    PlannedOp(Operation.NOOP, key, event, row, "cancelled and not mirrored", digest)
                )
            continue

        if row is None or row.sync_state == SyncState.DELETED or not row.google_event_id:
            # Either never mirrored, or previously withdrawn and now resurrected
            # (Outlook lets a user un-cancel by re-creating the same occurrence).
            reason = "not present on Google" if row is None else "map row inactive; re-creating"
            plan.creates.append(PlannedOp(Operation.CREATE, key, event, row, reason, digest))
        elif row.content_hash != digest:
            plan.updates.append(
                PlannedOp(Operation.UPDATE, key, event, row, "content hash changed", digest)
            )
        else:
            # The common case by a wide margin, and the reason this stays inside the
            # Google connector's 100-calls/60s budget: no API call at all.
            plan.noops.append(PlannedOp(Operation.NOOP, key, event, row, "unchanged", digest))

    # Rows we mirrored that Outlook no longer reports -> the event was deleted there.
    for row in map_rows:
        if row.correlation_key in seen or row.sync_state != SyncState.ACTIVE:
            continue
        if not row.google_event_id:
            continue
        if row.occurrence_start_utc and not in_window(row.occurrence_start_utc, now, config):
            # Merely aged out of the window. Not a deletion -- leave it alone.
            plan.noops.append(
                PlannedOp(Operation.NOOP, row.correlation_key, None, row, "outside sync window")
            )
            continue
        plan.deletes.append(
            PlannedOp(Operation.DELETE, row.correlation_key, None, row, "absent from Outlook")
        )

    _apply_delete_circuit_breaker(plan, map_rows, config)
    _apply_throttle_cap(plan, config)
    return plan


def _apply_delete_circuit_breaker(plan: SyncPlan, map_rows: list[MapRow], config: Config) -> None:
    """Refuse a suspiciously large batch of deletions.

    An empty or short Outlook read is far more likely to be a transient API failure
    than a real mass cancellation, and acting on it would wipe the user's mirrored
    calendar irrecoverably. Above the threshold we perform *no* deletes at all --
    not a partial batch -- and surface it as an error for the watchdog to alert on.
    Creates and updates still proceed: they are additive and safe.

    The rule is deliberately conjunctive: a deletion batch must be both a large
    *share* of the mirror and larger than an absolute floor. On a sparse calendar a
    percentage test alone is useless -- cancelling one of three meetings is 33% and
    would be refused forever, training the user to ignore the alert.
    """
    active = [r for r in map_rows if r.sync_state == SyncState.ACTIVE and r.google_event_id]
    if not plan.deletes or not active:
        return

    if len(plan.deletes) <= config.min_deletes_before_breaker:
        return

    share = len(plan.deletes) / len(active) * 100
    if share <= config.max_delete_percent:
        return

    withheld = plan.deletes
    plan.deletes = []
    plan.circuit_breaker_tripped = True
    plan.circuit_breaker_reason = (
        f"Refused {len(withheld)} deletion(s): {share:.0f}% of {len(active)} active mirrored "
        f"events, over the {config.max_delete_percent}% safety threshold. This usually means "
        f"the Outlook read failed or returned partial data rather than a genuine mass "
        f"cancellation. No events were removed from Google. Investigate, then either fix the "
        f"source problem or raise MaxDeletePercent for one run."
    )
    plan.warnings.append(plan.circuit_breaker_reason)
    for op in withheld:
        op.reason = f"withheld by circuit breaker: {op.reason}"
        plan.deferred.append(op)


def _apply_throttle_cap(plan: SyncPlan, config: Config) -> None:
    """Hold mutations to `MaxMutationsPerRun`, and say so when we do.

    The Google connector allows 100 calls per 60 seconds per connection. Exceeding it
    fails the run mid-batch and leaves the mirror half-written. Capping instead lets a
    large backlog drain predictably across successive runs. The overflow is recorded
    in `deferred` and flagged in `truncated_by_cap` so a truncated run can never be
    mistaken for a complete one.
    """
    cap = max(0, config.max_mutations_per_run)
    total = plan.mutation_count
    if total <= cap:
        return

    buckets = {
        Operation.CREATE: plan.creates,
        Operation.UPDATE: plan.updates,
        Operation.DELETE: plan.deletes,
    }
    kept: dict[Operation, list[PlannedOp]] = {op: [] for op in _MUTATION_ORDER}
    budget = cap
    for op in _MUTATION_ORDER:
        take = min(budget, len(buckets[op]))
        kept[op] = buckets[op][:take]
        for overflow in buckets[op][take:]:
            overflow.reason = f"deferred by throttle cap: {overflow.reason}"
            plan.deferred.append(overflow)
        budget -= take

    plan.creates, plan.updates, plan.deletes = (
        kept[Operation.CREATE],
        kept[Operation.UPDATE],
        kept[Operation.DELETE],
    )
    plan.truncated_by_cap = True
    plan.warnings.append(
        f"Throttle cap reached: applied {cap} of {total} pending changes this run; "
        f"{total - cap} deferred to the next run."
    )
