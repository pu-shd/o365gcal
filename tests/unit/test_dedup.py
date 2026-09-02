"""The dedup routine deletes calendar events, so its invariants matter more than its
features. Each test below corresponds to one of the guarantees in dedup.py's docstring."""

from datetime import datetime, timedelta, timezone

import pytest
from o365gcal.dedup import (
    DedupPlan,
    GoogleEvent,
    build_dedup_plan,
    choose_survivor,
    saturated,
    time_slices,
)
from o365gcal.model import Config, MapRow, SyncState
from o365gcal.normalize import MARKER_PREFIX

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def mirrored(event_id, key, summary="Standup"):
    return GoogleEvent(
        id=event_id,
        summary=summary,
        description=f"Agenda\n\nMirrored from Outlook\n{MARKER_PREFIX} {key}",
        start=NOW,
        html_link=f"https://calendar.google.com/{event_id}",
    )


def own(event_id, summary="My own event"):
    return GoogleEvent(id=event_id, summary=summary, description="something I wrote")


KEY = "uid-standup|2026-09-01T14:00:00Z"


# --- invariant 1: never touch what we did not create ------------------------

def test_unmarked_events_are_never_deleted(config):
    plan = build_dedup_plan([own("a"), own("b"), own("c")], [], config)
    assert plan.delete_ids == []
    assert plan.unmarked == 3


def test_unmarked_duplicates_are_left_alone(config):
    """Two of the user's own identical events are their business, not ours."""
    plan = build_dedup_plan([own("a", "Lunch"), own("b", "Lunch")], [], config)
    assert plan.delete_ids == []


def test_a_mirrored_event_whose_marker_was_edited_away_is_not_ours(config):
    stripped = GoogleEvent(id="g1", description="Agenda\n\nMirrored from Outlook")
    plan = build_dedup_plan([stripped], [], config)
    assert plan.unmarked == 1
    assert plan.delete_ids == []


# --- invariant 2: deletion requires a proven duplicate ----------------------

def test_single_marked_event_is_never_deleted(config):
    plan = build_dedup_plan([mirrored("g1", KEY)], [], config)
    assert plan.delete_ids == []
    assert plan.healthy == 1


def test_different_keys_are_not_duplicates(config):
    events = [mirrored(f"g{i}", f"uid-{i}|2026-09-0{i+1}T14:00:00Z") for i in range(5)]
    plan = build_dedup_plan(events, [], config)
    assert plan.delete_ids == []


def test_recurring_occurrences_are_not_duplicates(config):
    """Same series, different start times: distinct keys, all legitimate."""
    events = [mirrored(f"g{i}", f"uid-weekly|2026-09-0{i+1}T14:00:00Z") for i in range(4)]
    plan = build_dedup_plan(events, [], config)
    assert plan.delete_ids == []
    assert plan.healthy == 4


# --- invariant 3: exactly one survivor ---------------------------------------

@pytest.mark.parametrize("copies", [2, 3, 5, 12])
def test_exactly_one_survivor_per_group(config, copies):
    config.min_deletes_before_breaker = 100  # isolate this invariant from the breaker
    events = [mirrored(f"g{i}", KEY) for i in range(copies)]
    plan = build_dedup_plan(events, [], config)
    assert len(plan.duplicates) == 1
    action = plan.duplicates[0]
    assert len(action.delete) == copies - 1
    assert action.keep not in action.delete
    assert len({action.keep, *action.delete}) == copies, "no id may appear twice"


def test_survivor_is_never_deleted_across_many_groups(config):
    config.min_deletes_before_breaker = 100
    events = []
    for k in range(6):
        key = f"uid-{k}|2026-09-01T14:00:00Z"
        events += [mirrored(f"g{k}a", key), mirrored(f"g{k}b", key)]
    plan = build_dedup_plan(events, [], config)
    keeps = {a.keep for a in plan.duplicates}
    assert keeps.isdisjoint(set(plan.delete_ids))
    assert len(plan.duplicates) == 6


# --- invariant 4: deterministic and repeatable -------------------------------

def test_survivor_choice_is_stable_under_reordering(config):
    config.min_deletes_before_breaker = 100
    a, b, c = mirrored("gc", KEY), mirrored("ga", KEY), mirrored("gb", KEY)
    first = build_dedup_plan([a, b, c], [], config).duplicates[0].keep
    second = build_dedup_plan([c, a, b], [], config).duplicates[0].keep
    assert first == second == "ga"


def test_rerunning_after_a_dedup_finds_nothing(config):
    """A second pass must be a no-op. If the survivor choice were unstable, repeated
    runs would delete a different copy each time until none were left."""
    config.min_deletes_before_breaker = 100
    events = [mirrored("g1", KEY), mirrored("g2", KEY), mirrored("g3", KEY)]
    plan = build_dedup_plan(events, [], config)
    survivors = [e for e in events if e.id not in plan.delete_ids]
    again = build_dedup_plan(survivors, [], config)
    assert again.delete_ids == []


def test_existing_map_row_wins_the_survivor_choice(config):
    """Keeping the mapped copy means an install that is still working keeps working."""
    config.min_deletes_before_breaker = 100
    events = [mirrored("gaaa", KEY), mirrored("gzzz", KEY)]
    row = MapRow(correlation_key=KEY, google_event_id="gzzz")
    plan = build_dedup_plan(events, [row], config)
    assert plan.duplicates[0].keep == "gzzz"
    assert plan.duplicates[0].delete == ["gaaa"]


def test_choose_survivor_ignores_a_mapped_id_that_is_absent(config):
    assert choose_survivor(["gb", "ga"], "gone") == "ga"


# --- invariant 5: incomplete reads never delete ------------------------------

def test_unreadable_slice_blocks_all_deletion(config):
    config.min_deletes_before_breaker = 100
    events = [mirrored("g1", KEY), mirrored("g2", KEY)]
    plan = build_dedup_plan(events, [], config, unreadable_slices=["2026-09-01"])
    assert plan.delete_ids == []
    assert plan.circuit_breaker_tripped
    assert "could not be read completely" in plan.warnings[0]


def test_saturation_detection():
    assert saturated(250) is True
    assert saturated(999) is True
    assert saturated(12) is False


def test_time_slices_cover_the_window_without_gaps():
    start, end = NOW, NOW + timedelta(days=10)
    slices = time_slices(start, end, days=1)
    assert slices[0][0] == start
    assert slices[-1][1] == end
    for (_, a), (b, _) in zip(slices, slices[1:]):
        assert a == b, "slices must be contiguous or events fall between them"
    assert len(slices) == 10


# --- the breaker -------------------------------------------------------------

def test_breaker_refuses_a_large_batch(config):
    events = []
    for k in range(20):
        key = f"uid-{k}|2026-09-01T14:00:00Z"
        events += [mirrored(f"g{k}a", key), mirrored(f"g{k}b", key)]
    plan = build_dedup_plan(events, [], config)
    assert plan.circuit_breaker_tripped
    assert plan.delete_ids == []


def test_breaker_floor_allows_a_small_cleanup(config):
    """A handful of duplicates is the ordinary case this routine is for."""
    events = []
    for k in range(3):
        key = f"uid-{k}|2026-09-01T14:00:00Z"
        events += [mirrored(f"g{k}a", key), mirrored(f"g{k}b", key)]
    plan = build_dedup_plan(events, [], config)
    assert not plan.circuit_breaker_tripped
    assert len(plan.delete_ids) == 3


# --- map rebuild -------------------------------------------------------------

def test_map_is_rebuilt_for_survivors_without_rows(config):
    """The point of the whole exercise: recovering the map from Google alone."""
    config.min_deletes_before_breaker = 100
    events = [mirrored("g1", KEY), mirrored("g2", KEY)]
    plan = build_dedup_plan(events, [], config)
    assert len(plan.map_repairs) == 1
    row = plan.map_repairs[0]
    assert row.correlation_key == KEY
    assert row.google_event_id == plan.duplicates[0].keep
    assert row.sync_state == SyncState.ACTIVE
    assert row.outlook_ical_uid == "uid-standup"


def test_rebuilt_row_has_no_fingerprint(config):
    """An invented fingerprint would mark a stale event as up to date and the next
    reconcile would skip fixing it."""
    plan = build_dedup_plan([mirrored("g1", KEY)], [], config)
    assert plan.map_repairs[0].content_hash == ""


def test_healthy_mapped_event_needs_no_repair(config):
    row = MapRow(correlation_key=KEY, google_event_id="g1")
    plan = build_dedup_plan([mirrored("g1", KEY)], [row], config)
    assert plan.map_repairs == []
    assert plan.healthy == 1


def test_map_row_pointing_at_a_deleted_copy_is_repaired(config):
    """After dedup the map must point at the survivor, not the copy just removed."""
    config.min_deletes_before_breaker = 100
    events = [mirrored("ga", KEY), mirrored("gb", KEY)]
    row = MapRow(correlation_key=KEY, google_event_id="gb")
    plan = build_dedup_plan(events, [], config)  # no rows -> keeps 'ga'
    assert plan.duplicates[0].keep == "ga"
    assert plan.map_repairs[0].google_event_id == "ga"


def test_marker_parsing_tolerates_surrounding_text(config):
    ev = GoogleEvent(
        id="g1",
        description=f"line one\nline two\n\n{MARKER_PREFIX}   {KEY}   \ntrailing",
    )
    assert ev.marker_key == KEY
