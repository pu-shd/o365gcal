"""Heartbeat expectations. These exist because the watchdog cried wolf: a single
90-minute threshold flagged a daily digest and an event-driven trigger as broken while
the actual sync engine was healthy."""

import pytest
from o365gcal.schema import (
    FLOWS,
    HEARTBEAT_EXPECTATIONS,
    HEARTBEAT_FLOWS,
    NO_HEARTBEAT,
)

#: Each flow's actual schedule, in minutes. None means event-driven or on demand.
CADENCE = {
    "0 Setup and Provision": None,
    "1 Sync Outlook Trigger": None,
    "2 Apply Event": None,
    "3 Reconcile": 15,
    "4 Digest": 1440,
    "5 Watchdog": 60,
    "6 Backup State": 1440,
    "7 Dedup and Repair": None,
    "8 Invitation Reminder": 60,
}


def test_every_flow_has_a_deliberate_decision():
    """Adding a flow must force a choice, not default into a false alarm."""
    decided = set(HEARTBEAT_EXPECTATIONS) | set(NO_HEARTBEAT)
    assert set(FLOWS) == decided, f"undecided: {set(FLOWS) ^ decided}"


def test_no_flow_is_both_monitored_and_exempt():
    assert not (set(HEARTBEAT_EXPECTATIONS) & set(NO_HEARTBEAT))


@pytest.mark.parametrize("flow", sorted(HEARTBEAT_EXPECTATIONS))
def test_threshold_gives_grace_without_hiding_an_outage(flow):
    """Every threshold must clear the cadence with room for a late run, and stay
    within 3x so a real outage is reported promptly.

    The headroom shrinks as the cadence lengthens, on purpose. A 30-minute flow can
    skip a run harmlessly. A daily flow skipping a day is itself the news, so it gets
    grace for lateness only."""
    cadence = CADENCE[flow]
    assert cadence is not None, f"{flow} has no schedule but is monitored"
    threshold = HEARTBEAT_EXPECTATIONS[flow]
    assert threshold >= cadence * 1.2, (
        f"{flow}: {threshold}min threshold on a {cadence}min cadence leaves no grace "
        f"for a merely late run, so normal operation would alert"
    )
    assert threshold <= cadence * 3, (
        f"{flow}: {threshold}min threshold on a {cadence}min cadence is loose enough "
        f"to hide a real outage"
    )


@pytest.mark.parametrize("flow", sorted(NO_HEARTBEAT))
def test_exempt_flows_have_no_schedule(flow):
    """Anything on a schedule should be monitored; only event-driven, child and manual
    flows are legitimately silent."""
    assert CADENCE[flow] is None, f"{flow} runs on a schedule and should be monitored"


def test_exemptions_are_explained():
    for flow, reason in NO_HEARTBEAT.items():
        assert len(reason) > 30, f"{flow}: give the actual reason, not a label"


def test_the_daily_digest_would_have_been_a_false_alarm():
    """The specific bug: 90 minutes against a daily flow."""
    assert HEARTBEAT_EXPECTATIONS["4 Digest"] > 90 * 10


def test_the_event_driven_trigger_is_not_monitored():
    """It is silent by design on a quiet calendar, and it is only a latency
    optimisation - flow 3 is the engine of record and is monitored."""
    assert "1 Sync Outlook Trigger" not in HEARTBEAT_EXPECTATIONS
    assert "1 Sync Outlook Trigger" in NO_HEARTBEAT


def test_the_reconciler_is_monitored_most_tightly():
    """It is the only flow whose silence actually means the calendar goes stale."""
    assert HEARTBEAT_EXPECTATIONS["3 Reconcile"] == min(HEARTBEAT_EXPECTATIONS.values())


def test_health_rows_are_seeded_for_exactly_the_monitored_flows():
    assert set(HEARTBEAT_FLOWS) == set(HEARTBEAT_EXPECTATIONS)
