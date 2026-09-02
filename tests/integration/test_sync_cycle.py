"""End-to-end cycles: Outlook truth in, Google state out, driven through the same
plan/apply path the flows use."""
from datetime import timedelta

import pytest
from conftest import make_event, make_row
from fake_google import FakeGoogle
from o365gcal.apply import apply_plan
from o365gcal.diff import build_plan
from o365gcal.model import Level, Operation, ResponseType, SyncState


def cycle(events, rows, config, google, now):
    plan = build_plan(events, rows, config, now)
    result = apply_plan(plan, google, config, rows, now)
    return plan, result, list(result.rows.values())


def test_create_update_delete_lifecycle(config, now):
    g = FakeGoogle()
    e = make_event("Thesis Defense", location="Friend 006")

    _, res, rows = cycle([e], [], config, g, now)
    assert res.created == 1 and len(g.events) == 1
    gid = rows[0].google_event_id
    assert g.events[gid]["summary"] == "Thesis Defense"

    e.location = "McCosh 50"
    _, res, rows = cycle([e], rows, config, g, now)
    assert res.updated == 1
    assert rows[0].google_event_id == gid, "update must not orphan and re-create"
    assert g.events[gid]["location"] == "McCosh 50"

    _, res, rows = cycle([], rows, config, g, now)
    assert res.deleted == 1
    assert g.events == {}
    assert rows[0].sync_state == SyncState.DELETED


def test_repeated_runs_make_no_further_calls(config, now):
    """Idempotency: the reconciler runs every 30 minutes forever."""
    g = FakeGoogle()
    events = [make_event(f"E {i}", offset_days=i + 1) for i in range(5)]
    _, _, rows = cycle(events, [], config, g, now)
    baseline = g.mutation_count
    for _ in range(5):
        _, res, rows = cycle(events, rows, config, g, now)
        assert res.created == res.updated == res.deleted == 0
    assert g.mutation_count == baseline


def test_update_on_missing_google_event_repairs(config, now):
    """A user deleted the mirrored copy directly in Google. Re-create and re-point
    the map row instead of retrying a dead id every 30 minutes forever."""
    g = FakeGoogle()
    e = make_event()
    _, _, rows = cycle([e], [], config, g, now)
    gid = rows[0].google_event_id
    del g.events[gid]

    e.subject = "Renamed"
    _, res, rows = cycle([e], rows, config, g, now)
    assert res.repaired == 1
    assert rows[0].google_event_id != gid
    assert rows[0].sync_state == SyncState.ACTIVE
    assert g.events[rows[0].google_event_id]["summary"] == "Renamed"


def test_delete_of_already_absent_event_succeeds(config, now):
    g = FakeGoogle()
    e = make_event()
    _, _, rows = cycle([e], [], config, g, now)
    g.events.clear()
    _, res, rows = cycle([], rows, config, g, now)
    assert res.deleted == 1 and res.failed == 0
    assert rows[0].sync_state == SyncState.DELETED


def test_dry_run_mutates_nothing(config, now):
    config.dry_run = True
    g = FakeGoogle()
    events = [make_event(f"E {i}", offset_days=i + 1) for i in range(3)]
    plan, res, _ = cycle(events, [], config, g, now)
    assert plan.mutation_count == 3
    assert g.mutation_count == 0 and res.created == 0
    assert all("DRY RUN" in entry.message for entry in res.by_operation(Operation.SKIP))


def test_throttling_is_recorded_not_fatal(config, now):
    """One 429 must not abort the batch and strand the rest of the calendar."""
    g = FakeGoogle(throttle_after=3)
    events = [make_event(f"E {i}", offset_days=i + 1) for i in range(6)]
    _, res, _ = cycle(events, [], config, g, now)
    assert res.created == 3
    assert res.failed == 3
    assert any(e.level == Level.ERROR for e in res.logs)


def test_one_bad_event_does_not_stall_the_others(config, now):
    g = FakeGoogle(throttle_after=None)
    good = [make_event(f"Good {i}", offset_days=i + 1) for i in range(3)]

    class Flaky(FakeGoogle):
        def create_event(self, payload):
            if payload["summary"] == "Good 1":
                raise RuntimeError("connector blew up")
            return super().create_event(payload)

    g = Flaky()
    _, res, _ = cycle(good, [], config, g, now)
    assert res.created == 2 and res.failed == 1


def test_recurring_series_edit_touches_only_changed_occurrence(config, now):
    g = FakeGoogle()
    occs = [make_event("Weekly", offset_days=d, series_master_id="m1") for d in (1, 8, 15, 22)]
    _, _, rows = cycle(occs, [], config, g, now)
    assert len(g.events) == 4
    before = g.mutation_count

    occs[2].location = "Moved room"
    _, res, rows = cycle(occs, rows, config, g, now)
    assert res.updated == 1 and res.created == 0 and res.deleted == 0
    assert g.mutation_count == before + 1


def test_cancelling_one_occurrence_leaves_the_series(config, now):
    g = FakeGoogle()
    occs = [make_event("Weekly", offset_days=d, series_master_id="m1") for d in (1, 8, 15, 22)]
    _, _, rows = cycle(occs, [], config, g, now)
    occs[1].is_cancelled = True
    _, res, rows = cycle(occs, rows, config, g, now)
    assert res.deleted == 1
    assert len(g.events) == 3


def test_circuit_breaker_preserves_google_state(config, now):
    """The whole point: a bad Outlook read must leave Google untouched."""
    g = FakeGoogle()
    events = [make_event(f"E {i}", offset_days=i + 1) for i in range(30)]
    _, _, rows = cycle(events, [], config, g, now)
    assert len(g.events) == 30

    plan, res, _ = cycle([], rows, config, g, now)  # Outlook returns nothing
    assert plan.circuit_breaker_tripped
    assert len(g.events) == 30, "no event may be removed when the breaker trips"
    assert res.deleted == 0
    assert any(e.level == Level.ERROR for e in res.logs)


def test_all_day_and_timezone_fields_round_trip(config, now):
    g = FakeGoogle()
    e = make_event("Conference", is_all_day=True)
    _, _, rows = cycle([e], [], config, g, now)
    payload = g.events[rows[0].google_event_id]
    assert payload["isAllDay"] is True
    assert payload["start"].endswith("+00:00")


def test_rsvp_change_propagates_to_google(config, now):
    """A Google-only user must be able to see they still owe an answer."""
    g = FakeGoogle()
    e = make_event("Committee", my_response=ResponseType.NOT_RESPONDED,
                   required_attendees="a@example.com")
    _, _, rows = cycle([e], [], config, g, now)
    assert "⚠ Not responded" in g.events[rows[0].google_event_id]["description"]

    e.my_response = ResponseType.ACCEPTED
    _, res, rows = cycle([e], rows, config, g, now)
    assert res.updated == 1
    assert "Accepted" in g.events[rows[0].google_event_id]["description"]


def test_accept_invitation_does_not_duplicate(config, now):
    """The documented double-fire: id rewritten on acceptance. Must stay one event."""
    g = FakeGoogle()
    e = make_event("Invited Meeting", my_response=ResponseType.NOT_RESPONDED)
    _, _, rows = cycle([e], [], config, g, now)
    gid = rows[0].google_event_id

    e.event_id = "AAMk-rewritten"
    e.my_response = ResponseType.ACCEPTED
    _, res, rows = cycle([e], rows, config, g, now)
    assert len(g.events) == 1, "acceptance must not create a second Google event"
    assert rows[0].google_event_id == gid
    assert res.created == 0 and res.updated == 1


def test_backlog_converges_under_cap(config, now):
    config.max_mutations_per_run = 10
    g = FakeGoogle()
    events = [make_event(f"E {i}", offset_days=i + 1) for i in range(34)]
    rows, runs = [], 0
    while runs < 12:
        plan, res, rows = cycle(events, rows, config, g, now)
        if plan.mutation_count == 0:
            break
        runs += 1
    assert runs == 4
    assert len(g.events) == 34
