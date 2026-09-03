"""Grouping outstanding invitations.

Written against a real digest that listed seventeen lines for what were actually two
recurring series. Complete, and unreadable: a reader cannot tell whether they owe two
replies or seventeen.
"""

from datetime import datetime, timedelta, timezone

import pytest
from o365gcal.model import Config, OutlookEvent, ResponseType
from o365gcal.rsvp import group_invitations, summarise

NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


def invite(subject, days, series=None, uid=None, response=ResponseType.NOT_RESPONDED,
           cancelled=False, link=""):
    start = NOW + timedelta(days=days)
    return OutlookEvent(
        ical_uid=uid or f"uid-{subject}",
        event_id=f"e-{subject}-{days}",
        subject=subject,
        start_utc=start,
        end_utc=start + timedelta(hours=1),
        series_master_id=series or "",
        my_response=response,
        is_cancelled=cancelled,
        organizer="organiser@example.com",
        web_link=link,
    )


def series(subject, count, every, start_at, key):
    return [invite(subject, start_at + every * i, series=key) for i in range(count)]


# --- the reason this exists --------------------------------------------------

def test_a_recurring_series_is_one_line_not_seventeen(config):
    events = series("VRM Office Hour", 9, 14, 4, "vrm") + \
             series("IT Touchpoint", 8, 14, 7, "itsc")
    groups = group_invitations(events, config, NOW)
    assert len(groups) == 2, "two series must not read as seventeen separate asks"
    assert {g.subject for g in groups} == {"VRM Office Hour", "IT Touchpoint"}


def test_a_group_reports_how_many_occurrences_it_covers(config):
    groups = group_invitations(series("Standup", 4, 7, 1, "s"), config, NOW)
    assert groups[0].occurrences == 4
    assert groups[0].is_series


def test_a_single_meeting_is_not_reported_as_a_series(config):
    groups = group_invitations([invite("One-off", 2)], config, NOW)
    assert groups[0].occurrences == 1
    assert not groups[0].is_series


# --- ordering ----------------------------------------------------------------

def test_soonest_first(config):
    events = [invite("Later", 30), invite("Sooner", 2), invite("Middle", 10)]
    assert [g.subject for g in group_invitations(events, config, NOW)] == [
        "Sooner", "Middle", "Later"]


def test_a_group_reports_its_soonest_occurrence(config):
    """The reader needs the one they must act on, not whichever came first in the
    read - Graph does not guarantee ordering."""
    events = [invite("Weekly", 20, series="w"), invite("Weekly", 5, series="w"),
              invite("Weekly", 12, series="w")]
    group = group_invitations(events, config, NOW)[0]
    assert (group.next_start - NOW).days == 5


def test_the_link_follows_the_soonest_occurrence(config):
    events = [invite("Weekly", 20, series="w", link="late"),
              invite("Weekly", 3, series="w", link="early")]
    assert group_invitations(events, config, NOW)[0].web_link == "early"


# --- what is excluded --------------------------------------------------------

@pytest.mark.parametrize("response", [ResponseType.ACCEPTED, ResponseType.DECLINED,
                                      ResponseType.ORGANIZER])
def test_answered_invitations_are_excluded(config, response):
    assert group_invitations([invite("Done", 2, response=response)], config, NOW) == []


def test_tentative_still_counts_as_outstanding(config):
    """Tentative is not an answer the organiser can plan around."""
    groups = group_invitations(
        [invite("Maybe", 2, response=ResponseType.TENTATIVE)], config, NOW)
    assert len(groups) == 1


def test_cancelled_invitations_are_excluded(config):
    assert group_invitations([invite("Gone", 2, cancelled=True)], config, NOW) == []


def test_past_occurrences_are_excluded(config):
    """Nobody needs reminding to reply to yesterday's meeting."""
    assert group_invitations([invite("Yesterday", -1)], config, NOW) == []


def test_invitations_beyond_the_horizon_are_excluded(config):
    """A meeting eight months out is not urgent and pushes the urgent ones off the
    bottom of the email."""
    far = config.rsvp_horizon_days + 30
    assert group_invitations([invite("Distant", far)], config, NOW) == []


def test_a_series_is_counted_only_within_the_horizon(config):
    """A series running for years should report the occurrences that matter now."""
    events = series("Long runner", 40, 14, 1, "lr")
    group = group_invitations(events, config, NOW)[0]
    assert group.occurrences < 40
    assert group.occurrences == sum(
        1 for e in events if 0 <= (e.start_utc - NOW).days <= config.rsvp_horizon_days
    )


def test_grouping_falls_back_to_ical_uid(config):
    """A non-recurring invitation has no series id, so the key has to fall back or
    every occurrence becomes its own group."""
    events = [invite("Solo", 2, uid="shared"), invite("Solo", 9, uid="shared")]
    assert len(group_invitations(events, config, NOW)) == 1


# --- subject line ------------------------------------------------------------

def test_summary_names_the_most_urgent(config):
    events = [invite("Later", 20), invite("Urgent", 1)]
    assert "Urgent" in summarise(group_invitations(events, config, NOW))


def test_summary_is_singular_for_one(config):
    text = summarise(group_invitations([invite("Only", 2)], config, NOW))
    assert "1 invitation awaiting" in text


def test_summary_when_nothing_is_outstanding(config):
    assert summarise([]) == "no outstanding invitations"
