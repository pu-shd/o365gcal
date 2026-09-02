"""Growth and truncation. The saturation case is the important one: it is a silent
correctness bug, not a capacity concern."""

from datetime import datetime, timedelta, timezone

import pytest
from o365gcal.limits import (
    LIST_VIEW_THRESHOLD,
    READ_TOP,
    assess_list,
    build_prune_plan,
    read_saturated,
    retention_is_sane,
)
from o365gcal.model import Config, MapRow, SyncState

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


def row(key, days_ago, state=SyncState.ACTIVE, settled_days_ago=None):
    return MapRow(
        correlation_key=key,
        google_event_id=f"g-{key}",
        sync_state=state,
        occurrence_start_utc=NOW - timedelta(days=days_ago),
        last_synced_utc=None if settled_days_ago is None else NOW - timedelta(days=settled_days_ago),
    )


# --- truncation ---------------------------------------------------------------

def test_a_full_page_is_treated_as_possibly_truncated():
    """The bug this prevents: reconcile reads 5000 of 6000 map rows, sees 1000
    occurrences with no row, and mirrors them a second time while reporting success."""
    assert read_saturated(READ_TOP) is True
    assert read_saturated(READ_TOP + 1) is True
    assert read_saturated(READ_TOP - 1) is False


def test_saturated_list_reports_the_read_as_unsafe(config):
    health = assess_list("O365GCalSyncMap", READ_TOP, config)
    assert health.state == "saturated"
    assert "may be incomplete" in health.message


def test_warning_arrives_before_truncation(config):
    """A warning at or above the threshold would arrive only once reads were already
    losing rows, which is too late to be useful."""
    assert config.list_size_warn_at < LIST_VIEW_THRESHOLD
    assert assess_list("L", config.list_size_warn_at, config).state == "approaching"
    assert assess_list("L", config.list_size_warn_at - 1, config).state == "ok"


def test_small_list_is_quiet(config):
    assert assess_list("O365GCalLog", 12, config).state == "ok"


# --- pruning ------------------------------------------------------------------

def test_rows_inside_the_window_are_never_pruned(config):
    rows = [row("a", 0), row("b", 3), row("c", -60)]  # today, recent past, future
    plan = build_prune_plan(rows, config, NOW)
    assert plan.all_rows == []
    assert plan.kept == 3


def test_rows_just_outside_the_window_are_still_kept(config):
    """Leaving the window is not a reason to forget an event: retention is what
    decides, and it is deliberately much wider than the window."""
    plan = build_prune_plan([row("a", config.window_past_days + 5)], config, NOW)
    assert plan.all_rows == []


def test_rows_beyond_retention_are_pruned(config):
    old = config.window_past_days + config.map_retention_days + 10
    plan = build_prune_plan([row("a", old)], config, NOW)
    assert len(plan.stale_active) == 1
    assert plan.kept == 0


def test_settled_deletions_are_pruned_sooner(config):
    """A withdrawal record points at nothing, so it need not be kept for a year."""
    plan = build_prune_plan(
        [row("a", 40, SyncState.DELETED, settled_days_ago=config.deleted_row_retention_days + 5)],
        config, NOW,
    )
    assert len(plan.settled_deleted) == 1


def test_recent_deletion_records_are_kept(config):
    plan = build_prune_plan([row("a", 5, SyncState.DELETED, settled_days_ago=1)], config, NOW)
    assert plan.all_rows == []
    assert plan.kept == 1


def test_a_row_without_a_date_is_kept(config):
    """An unprunable row is a nuisance; a wrongly pruned one orphans a Google event."""
    plan = build_prune_plan([MapRow("mystery", google_event_id="g1")], config, NOW)
    assert plan.all_rows == []
    assert plan.kept == 1


def test_naive_datetimes_do_not_break_comparison(config):
    """SharePoint returns timestamps without an offset."""
    r = MapRow("a", google_event_id="g", occurrence_start_utc=datetime(2020, 1, 1))
    plan = build_prune_plan([r], config, NOW)
    assert len(plan.stale_active) == 1


def test_pruning_keeps_the_map_under_the_threshold(config):
    """The point of retention: five meetings a day must not cross 5000 rows."""
    per_day = 5
    span = config.window_past_days + config.map_retention_days
    rows = [row(f"k{i}", d) for d in range(span + 400) for i in range(per_day)][:12000]
    plan = build_prune_plan(rows, config, NOW)
    assert plan.kept < LIST_VIEW_THRESHOLD, (
        f"{plan.kept} rows would survive pruning, over the view threshold"
    )


# --- configuration sanity -----------------------------------------------------

def test_default_configuration_is_sane():
    assert retention_is_sane(Config()) == []


def test_retention_shorter_than_the_window_is_rejected():
    problems = retention_is_sane(Config(map_retention_days=30, window_future_days=120))
    assert problems and "still inside the sync window" in problems[0]


def test_warn_threshold_at_the_limit_is_rejected():
    problems = retention_is_sane(Config(list_size_warn_at=LIST_VIEW_THRESHOLD))
    assert problems and "already truncating" in problems[0]


@pytest.mark.parametrize("days", [0, -1])
def test_zero_deleted_retention_is_rejected(days):
    assert retention_is_sane(Config(deleted_row_retention_days=days))
