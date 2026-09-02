"""The two mechanisms that stand between a transient API failure and a wiped calendar."""
from conftest import make_event, make_row
from o365gcal.diff import build_plan
from o365gcal.model import Operation


def _mirror(config, count):
    events = [make_event(f"Meeting {i}", offset_days=i + 1) for i in range(count)]
    rows = [make_row(e, config, f"g-{i}") for i, e in enumerate(events)]
    return events, rows


def test_empty_outlook_read_deletes_nothing(config, now):
    """The catastrophic case: Outlook returns nothing because of a throttle, an
    expired token or a lost permission. Acting on it would erase the whole mirror."""
    _, rows = _mirror(config, 40)
    plan = build_plan([], rows, config, now)
    assert plan.deletes == []
    assert plan.circuit_breaker_tripped
    assert len(plan.deferred) == 40
    assert "40 deletion(s)" in plan.circuit_breaker_reason


def test_breaker_withholds_all_or_nothing(config, now):
    """A partial delete batch is the worst outcome: destructive *and* inconsistent."""
    events, rows = _mirror(config, 40)
    plan = build_plan(events[:10], rows, config, now)
    assert plan.deletes == []
    assert plan.circuit_breaker_tripped


def test_breaker_allows_normal_deletion_volume(config, now):
    events, rows = _mirror(config, 40)
    plan = build_plan(events[:36], rows, config, now)  # 4 deletes = 10%
    assert len(plan.deletes) == 4
    assert not plan.circuit_breaker_tripped


def test_breaker_floor_permits_small_absolute_batches(config, now):
    """Sparse calendar: 2 of 4 is 50%, but two deletions are not a catastrophe."""
    events, rows = _mirror(config, 4)
    plan = build_plan(events[:2], rows, config, now)
    assert len(plan.deletes) == 2
    assert not plan.circuit_breaker_tripped


def test_breaker_does_not_block_creates_and_updates(config, now):
    """Additive work is safe and must continue even while deletes are withheld."""
    events, rows = _mirror(config, 40)
    events[0].subject = "Changed"
    fresh = make_event("Brand New", offset_days=99)
    plan = build_plan([events[0], fresh], rows, config, now)
    assert plan.circuit_breaker_tripped
    assert len(plan.creates) == 1 and len(plan.updates) == 1


def test_throttle_cap_defers_overflow(config, now):
    config.max_mutations_per_run = 10
    events = [make_event(f"New {i}", offset_days=i + 1) for i in range(25)]
    plan = build_plan(events, [], config, now)
    assert plan.mutation_count == 10
    assert len(plan.deferred) == 15
    assert plan.truncated_by_cap


def test_truncated_run_is_never_silent(config, now):
    """A truncated run that looked complete would read as 'everything is mirrored'
    when a third of the calendar is missing."""
    config.max_mutations_per_run = 5
    plan = build_plan([make_event(f"N {i}", offset_days=i + 1) for i in range(12)], [], config, now)
    assert plan.truncated_by_cap
    assert any("deferred to the next run" in w for w in plan.warnings)
    assert plan.summary()["truncatedByCap"] is True


def test_cap_prioritises_additive_work_over_deletes(config, now):
    """Under pressure, get events onto the calendar first; deletion can wait a run."""
    config.max_mutations_per_run = 3
    events, rows = _mirror(config, 20)
    new = [make_event(f"New {i}", offset_days=i + 60) for i in range(3)]
    plan = build_plan(new + events[:16], rows, config, now)
    assert len(plan.creates) == 3
    assert plan.deletes == []


def test_backlog_drains_across_runs(config, now):
    """A large first sync must converge over successive runs, not stall."""
    config.max_mutations_per_run = 10
    events = [make_event(f"E {i}", offset_days=i + 1) for i in range(35)]
    rows, runs = [], 0
    while runs < 10:
        plan = build_plan(events, rows, config, now)
        if plan.mutation_count == 0:
            break
        for op in plan.creates:
            rows.append(make_row(op.event, config, f"g-{op.correlation_key}"))
        runs += 1
    assert runs == 4  # ceil(35/10)
    assert len(rows) == 35
    assert build_plan(events, rows, config, now).mutation_count == 0
