"""Existence verification.

Closes a real hole: an event deleted directly in Google was never noticed, because an
unchanged Outlook meeting short-circuits to no-op before anything checks the Google
copy still exists. Checking every event every run is unaffordable against 100 Google
calls per 60 seconds, so each run checks a rotating slice.
"""

from datetime import datetime, timedelta, timezone

import pytest
from o365gcal.model import Config, MapRow
from o365gcal.verify import current_slot, repair, rows_to_verify, verify

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def rows(n, with_id=True):
    return [
        MapRow(f"key-{i}", google_event_id=f"g{i}" if with_id else "", list_item_id=i)
        for i in range(1, n + 1)
    ]


def all_present(rs):
    return {r.google_event_id: True for r in rs}


# --- rotation ---------------------------------------------------------------

def test_consecutive_runs_check_different_slices(config):
    slots = [current_slot(NOW + timedelta(minutes=15 * i), config.verify_slices)
             for i in range(4)]
    assert len(set(slots)) == 4, "a run must not re-check what the last one did"


def test_a_full_sweep_covers_every_row(config):
    """The guarantee: nothing is verified often, but everything is verified."""
    rs = rows(64)
    seen = set()
    for i in range(config.verify_slices):
        outcome = verify(rs, all_present(rs), config, NOW + timedelta(minutes=15 * i))
        seen |= {r.correlation_key for r in outcome.checked}
    assert seen == {r.correlation_key for r in rs}


def test_rotation_is_stateless(config):
    """Derived from the clock, so there is no cursor to persist, go stale, or need
    repairing itself."""
    a = current_slot(NOW, config.verify_slices)
    b = current_slot(NOW, config.verify_slices)
    assert a == b


def test_slot_wraps_within_the_slice_count(config):
    for i in range(200):
        slot = current_slot(NOW + timedelta(minutes=15 * i), config.verify_slices)
        assert 0 <= slot < config.verify_slices


# --- cost -------------------------------------------------------------------

def test_cost_per_run_is_a_fraction_of_the_mirror(config):
    """The whole reason for slicing: 500 events must not mean 500 calls."""
    rs = rows(500)
    checked = rows_to_verify(rs, NOW, config)
    assert len(checked) <= config.max_verify_per_run


def test_ceiling_is_respected_however_large_the_calendar(config):
    config.verify_slices = 1  # everything falls in one slice
    assert len(rows_to_verify(rows(1000), NOW, config)) == config.max_verify_per_run


def test_rows_without_a_google_id_are_not_checked(config):
    """Nothing to verify, and the ordinary diff already treats them as needing a
    create."""
    assert rows_to_verify(rows(20, with_id=False), NOW, config) == []


# --- detection --------------------------------------------------------------

def test_a_missing_event_is_reported(config):
    rs = rows(16)
    exists = all_present(rs)
    target = rows_to_verify(rs, NOW, config)[0]
    exists[target.google_event_id] = False
    outcome = verify(rs, exists, config, NOW)
    assert [r.correlation_key for r in outcome.missing] == [target.correlation_key]


def test_present_events_are_not_reported(config):
    rs = rows(16)
    assert verify(rs, all_present(rs), config, NOW).missing == []


def test_an_unread_event_is_not_treated_as_missing(config):
    """A failed read is not evidence of deletion. Treating it as one would recreate
    events that are perfectly fine whenever the connector throttles - the same unsound
    inference as reporting an error response as zero rows."""
    rs = rows(16)
    outcome = verify(rs, {}, config, NOW)  # nothing was successfully read
    assert outcome.missing == []
    assert outcome.checked == []


def test_only_the_selected_slice_is_judged(config):
    """An event outside this run's slice must not be reported even if the caller
    happened to supply information about it."""
    rs = rows(32)
    selected = {r.correlation_key for r in rows_to_verify(rs, NOW, config)}
    exists = {r.google_event_id: False for r in rs}
    outcome = verify(rs, exists, config, NOW)
    assert {r.correlation_key for r in outcome.missing} == selected


# --- repair -----------------------------------------------------------------

def test_repair_queues_a_recreation_rather_than_creating(config):
    """Clearing the id lets the existing, idempotent create path rebuild the event from
    a fresh Outlook read. A repair branch that created the event itself would be a
    second writer to keep in step with the first."""
    row = MapRow("k", google_event_id="gone", content_hash="abc", list_item_id=1)
    repaired = repair(row)
    assert repaired.google_event_id == ""
    assert repaired.content_hash == "", (
        "the fingerprint must go too, or the diff sees no change and skips the create"
    )
    assert "missing" in repaired.last_error


def test_a_repaired_row_is_then_seen_as_needing_a_create(config):
    """End to end with the real diff, so the two halves cannot drift apart."""
    from conftest import make_event, make_row
    from o365gcal.diff import build_plan
    from o365gcal.model import Operation

    event = make_event()
    row = make_row(event, config)
    plan = build_plan([event], [row], config, NOW)
    assert plan.mutation_count == 0, "baseline: nothing to do"

    build_plan([event], [repair(row)], config, NOW)
    plan = build_plan([event], [row], config, NOW)
    assert [op.operation for op in plan.mutations] == [Operation.CREATE]


def test_summary_reports_sweep_length(config):
    outcome = verify(rows(16), all_present(rows(16)), config, NOW)
    assert outcome.summary()["slices"] == config.verify_slices
    assert outcome.coverage_runs == config.verify_slices


def test_defaults_keep_verification_affordable():
    """16 slices at a 15-minute cadence is a four-hour sweep; the ceiling keeps the
    worst run to ten extra Google calls against a budget of a hundred a minute."""
    c = Config()
    assert c.verify_slices * 15 == 240
    assert c.max_verify_per_run <= 10
