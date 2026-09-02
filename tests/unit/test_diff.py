"""Diff correctness: the five behaviours the user asked for, stated as tests."""
from datetime import timedelta

from conftest import make_event, make_row
from o365gcal.diff import build_plan, in_window
from o365gcal.model import Operation, SyncState
from o365gcal.normalize import content_hash, correlation_key


def test_new_outlook_event_is_created(config, now):
    e = make_event()
    plan = build_plan([e], [], config, now)
    assert [op.operation for op in plan.mutations] == [Operation.CREATE]
    assert plan.creates[0].correlation_key == correlation_key(e.ical_uid, e.start_utc)


def test_unchanged_event_is_a_noop_with_zero_api_calls(config, now):
    """The common case. Any regression here silently multiplies Google API traffic."""
    e = make_event()
    plan = build_plan([e], [make_row(e, config)], config, now)
    assert plan.mutation_count == 0
    assert len(plan.noops) == 1


def test_changed_event_is_updated_not_recreated(config, now):
    e = make_event()
    row = make_row(e, config)
    e.subject = "Standup (moved)"
    plan = build_plan([e], [row], config, now)
    assert [op.operation for op in plan.mutations] == [Operation.UPDATE]
    assert plan.updates[0].map_row.google_event_id == "g-1"


def test_event_absent_from_outlook_is_deleted(config, now):
    gone = make_event()
    keep = [make_event(f"Keep {i}", offset_days=i + 2) for i in range(8)]
    rows = [make_row(gone, config)] + [make_row(k, config, f"g-{i}") for i, k in enumerate(keep)]
    plan = build_plan(keep, rows, config, now)
    assert [op.operation for op in plan.mutations] == [Operation.DELETE]
    assert plan.deletes[0].map_row.google_event_id == "g-1"


def test_cancelled_occurrence_is_deleted(config, now):
    e = make_event()
    row = make_row(e, config)
    e.is_cancelled = True
    plan = build_plan([e], [row], config, now)
    assert [op.operation for op in plan.mutations] == [Operation.DELETE]
    assert "cancelled" in plan.deletes[0].reason


def test_cancelled_event_never_mirrored_is_noop(config, now):
    e = make_event(is_cancelled=True)
    plan = build_plan([e], [], config, now)
    assert plan.mutation_count == 0


def test_row_outside_window_is_not_deleted(config, now):
    """Ageing out of the sync window is not a cancellation. Deleting here would
    silently erase history every time the window slid forward."""
    old = make_event(offset_days=-400)
    plan = build_plan([], [make_row(old, config)], config, now)
    assert plan.mutation_count == 0
    assert "outside sync window" in plan.noops[0].reason


def test_deleted_row_resurrects_as_create(config, now):
    e = make_event()
    row = make_row(e, config, state=SyncState.DELETED)
    plan = build_plan([e], [row], config, now)
    assert [op.operation for op in plan.mutations] == [Operation.CREATE]


def test_row_without_google_id_is_recreated(config, now):
    e = make_event()
    row = make_row(e, config, google_id="")
    plan = build_plan([e], [row], config, now)
    assert [op.operation for op in plan.mutations] == [Operation.CREATE]


def test_recurring_occurrences_are_independent(config, now):
    """One series, three occurrences: edit one, cancel one, leave one. Only the two
    touched occurrences may generate work."""
    occs = [make_event("Weekly", offset_days=d, series_master_id="master-1") for d in (1, 8, 15)]
    rows = [make_row(o, config, f"g-{i}") for i, o in enumerate(occs)]
    occs[0].subject = "Weekly (agenda added)"
    occs[1].is_cancelled = True
    plan = build_plan(occs, rows, config, now)
    assert len(plan.updates) == 1 and len(plan.deletes) == 1 and len(plan.noops) == 1
    assert plan.updates[0].map_row.google_event_id == "g-0"
    assert plan.deletes[0].map_row.google_event_id == "g-1"


def test_accept_invitation_double_fire_collapses(config, now):
    """Outlook rewrites the event id on acceptance, producing what looks like a
    second event. Keying on iCalUId must collapse it to one update, not a duplicate."""
    e = make_event()
    row = make_row(e, config)
    from o365gcal.model import ResponseType
    e.event_id, e.my_response = "AAMk-rewritten-after-accept", ResponseType.ACCEPTED
    plan = build_plan([e], [row], config, now)
    assert len(plan.creates) == 0
    assert [op.operation for op in plan.mutations] == [Operation.UPDATE]


def test_in_window_boundaries(config, now):
    assert in_window(now + timedelta(days=119), now, config)
    assert not in_window(now + timedelta(days=121), now, config)
    assert in_window(now - timedelta(days=6), now, config)
    assert not in_window(now - timedelta(days=8), now, config)
