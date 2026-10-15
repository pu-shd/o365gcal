"""The hash is what keeps the reconciler inside Google's 100-calls/60s budget, so
its stability under irrelevant change is the single most important property here."""
from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_event
from o365gcal.model import ResponseType, Sensitivity
from o365gcal.normalize import (
    KEY_MAX_CHARS,
    bound_key,
    content_hash,
    correlation_key,
    iso_utc,
    normalize_attendees,
    strip_html,
)


def test_hash_is_stable_across_identical_reads(config, event):
    assert content_hash(event, config) == content_hash(event, config)


def test_hash_ignores_last_modified_churn(config, event):
    """Exchange rewrites lastModifiedDateTime for internal reasons. If that moved the
    hash, every reconcile would rewrite every event and blow the throttle budget."""
    before = content_hash(event, config)
    event.last_modified_utc = event.last_modified_utc + timedelta(hours=3)
    assert content_hash(event, config) == before


def test_hash_ignores_event_id_rewrite(config, event):
    """Outlook rewrites the event id when an invitation is accepted."""
    before = content_hash(event, config)
    event.event_id = "AAMk-completely-different"
    assert content_hash(event, config) == before


def test_hash_ignores_subsecond_precision(config, event):
    before = content_hash(event, config)
    event.start_utc = event.start_utc.replace(microsecond=456789)
    assert content_hash(event, config) == before


def test_hash_ignores_attendee_letter_case(config, event):
    """Exchange varies casing between reads. Ordering, by contrast, is deliberately
    *not* absorbed - normalising it needs a sort, which a flow cannot do, and parity
    with the shipped flow matters more than suppressing a rare redundant write."""
    event.required_attendees = "A@example.com;B@example.com"
    first = content_hash(event, config)
    event.required_attendees = "a@example.com;b@example.com"
    assert content_hash(event, config) == first


def test_hash_ignores_line_break_churn(config, event):
    """Graph varies line endings between reads of an unchanged body."""
    event.body_html = "Agenda: budget"
    before = content_hash(event, config)
    event.body_html = "\r\nAgenda: budget\t"
    assert content_hash(event, config) == before


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: setattr(e, "subject", "Renamed"),
        lambda e: setattr(e, "location", "Friend 006"),
        lambda e: setattr(e, "start_utc", e.start_utc + timedelta(hours=1)),
        lambda e: setattr(e, "end_utc", e.end_utc + timedelta(hours=1)),
        lambda e: setattr(e, "is_all_day", True),
        lambda e: setattr(e, "show_as", "free"),
        lambda e: setattr(e, "body_html", "Totally new agenda"),
        lambda e: setattr(e, "my_response", ResponseType.ACCEPTED),
        lambda e: setattr(e, "required_attendees", "new@example.com"),
    ],
    ids="subject location start end allday showas body rsvp attendees".split(),
)
def test_hash_changes_on_meaningful_edit(config, event, mutate):
    before = content_hash(event, config)
    mutate(event)
    assert content_hash(event, config) != before


def test_hash_changes_when_rsvp_changes(config, event):
    """An RSVP change is user-visible in the rendered description, so it must
    propagate to Google rather than being suppressed as a no-op."""
    event.my_response = ResponseType.NOT_RESPONDED
    before = content_hash(event, config)
    event.my_response = ResponseType.ACCEPTED
    assert content_hash(event, config) != before


def test_flipping_the_privacy_toggle_rewrites_everything(config, event):
    event.sensitivity = Sensitivity.PRIVATE
    before = content_hash(event, config)
    config.hide_private_event_details = True
    assert content_hash(event, config) != before


def test_busy_only_hides_details_from_hash(config):
    """Under busy-only, two private events differing only in subject/body must be
    indistinguishable -- otherwise the detail leaks via update churn."""
    config.hide_private_event_details = True
    a = make_event("Secret One", body_html="confidential", location="Room A")
    b = make_event("Secret Two", body_html="also secret", location="Room B")
    b.ical_uid, b.start_utc, b.end_utc = a.ical_uid, a.start_utc, a.end_utc
    for e in (a, b):
        e.sensitivity = Sensitivity.PRIVATE
    assert content_hash(a, config) == content_hash(b, config)


def test_correlation_key_shared_uid_distinct_starts():
    """All occurrences of a series share an iCalUId; start time separates them."""
    a, b = make_event(offset_days=1), make_event(offset_days=8)
    assert a.ical_uid == b.ical_uid
    assert correlation_key(a.ical_uid, a.start_utc) != correlation_key(b.ical_uid, b.start_utc)


def test_strip_html_block_boundaries_become_newlines():
    assert strip_html("<p>One</p><p>Two</p>") == "One\nTwo"
    assert strip_html("A<br>B") == "A\nB"


def test_normalize_attendees_lowercases_the_connector_strings():
    assert normalize_attendees("B@example.com;a@example.com", "C@example.com") == "b@example.com;a@example.com;c@example.com"


def test_iso_utc_assumes_naive_is_utc():
    from datetime import datetime

    assert iso_utc(datetime(2026, 9, 1, 14, 0)) == "2026-09-01T14:00:00Z"


def test_hash_and_fingerprint_agree(config, event):
    """The flows compare fingerprints; the engine compares hashes. If these ever
    disagreed, the tested logic would not be the shipped logic."""
    from o365gcal.normalize import fingerprint

    other = make_event("Different")
    assert (fingerprint(event, config) == fingerprint(other, config)) == (
        content_hash(event, config) == content_hash(other, config)
    )
    same = make_event()
    assert fingerprint(event, config) == fingerprint(same, config)
    assert content_hash(event, config) == content_hash(same, config)


def test_fingerprint_bounds_body_growth(config, event):
    """The fingerprint has to fit a SharePoint text column no matter the body size."""
    from o365gcal.normalize import BODY_FINGERPRINT_CHARS, fingerprint

    event.body_html = "x" * 500_000
    assert len(fingerprint(event, config)) < BODY_FINGERPRINT_CHARS + 1024


def test_fingerprint_detects_body_edits(config, event):
    from o365gcal.normalize import fingerprint

    event.body_html = "Agenda: budget review"
    before = fingerprint(event, config)
    event.body_html = "Agenda: budget review and hiring"
    assert fingerprint(event, config) != before


def test_fingerprint_uses_unit_separator(config, event):
    """A field boundary Outlook content cannot forge."""
    from o365gcal.normalize import fingerprint

    assert "\x1f" in fingerprint(event, config)


def test_title_prefix_sentinel_means_no_prefix(config, event):
    """An empty default cannot be resolved by the runtime, and a whitespace-only one is
    normalised away on import - a fresh install failed twice on exactly that. So the
    absence of a prefix is spelled out, and both the engine and the flow map the
    sentinel to nothing."""
    from o365gcal.normalize import NO_PREFIX, effective_subject

    config.title_prefix = NO_PREFIX
    assert effective_subject(event, config) == event.subject
    config.title_prefix = "NONE"
    assert effective_subject(event, config) == event.subject, "case-insensitive"
    config.title_prefix = "[Outlook] "
    assert effective_subject(event, config) == f"[Outlook] {event.subject}"


def test_default_config_applies_no_prefix(event):
    from o365gcal.model import Config
    from o365gcal.normalize import effective_subject

    assert effective_subject(event, Config()) == event.subject


def _wrapped_uid(external_uid: str) -> str:
    """An iCalUId the way Exchange stores an invitation sent by an outside system:
    a fixed header, then `vCal-Uid`, then the sender's own UID, all hex-encoded. The
    distinguishing part is at the end, and the length grows with the sender's UID."""
    data = b"vCal-Uid\x01\x00\x00\x00" + external_uid.encode() + b"\x00"
    return ("040000008200E00074C5B7101A82E008" + "0" * 40 + f"{len(data):08X}"
            + data.hex().upper())


#: Measured 2026-10-15: an IT@Princeton training-approval invitation whose iCalUId is
#: 236 characters, making a 257-character key that SharePoint refused to store.
LONG_UID = _wrapped_uid("approval-" + "a1b2c3d4" * 3 + "a1b2c3" + "@princeton.service-now.com")


def test_long_uid_fixture_matches_the_one_seen_in_production():
    assert len(LONG_UID) == 236


def test_correlation_key_fits_a_sharepoint_text_column():
    """Title is single-line text: 255 characters, and SharePoint rejects anything
    longer with 'Invalid text value'. Google already holds the event by then, so the
    next run creates it again - a new copy every 15 minutes."""
    start = datetime(2026, 10, 21, 13, 30, tzinfo=timezone.utc)
    key = correlation_key(LONG_UID, start)
    assert len(key) <= KEY_MAX_CHARS == 255
    assert key.endswith("|2026-10-21T13:30:00Z"), "the start time must survive intact"


def test_correlation_key_is_unchanged_when_it_already_fits():
    """Every row written before the limit existed must still be found, or the whole
    calendar would be re-created."""
    start = datetime(2026, 10, 21, 13, 30, tzinfo=timezone.utc)
    uid = "U" * (KEY_MAX_CHARS - len("|2026-10-21T13:30:00Z"))
    assert correlation_key(uid, start) == f"{uid}|2026-10-21T13:30:00Z"


def test_shortened_keys_still_tell_meetings_apart():
    """The header is dropped, not the sender's UID: two invitations from the same
    system differ only near the end."""
    start = datetime(2026, 10, 21, 13, 30, tzinfo=timezone.utc)
    a = LONG_UID
    b = _wrapped_uid("approval-" + "a1b2c3d4" * 3 + "ffffff" + "@princeton.service-now.com")
    assert correlation_key(a, start) != correlation_key(b, start)
    later = datetime(2026, 10, 28, 13, 30, tzinfo=timezone.utc)
    assert correlation_key(a, start) != correlation_key(a, later)


def test_bounding_an_already_bounded_key_changes_nothing():
    """Dedup reads keys back out of descriptions, some written before the limit."""
    start = datetime(2026, 10, 21, 13, 30, tzinfo=timezone.utc)
    full = f"{LONG_UID}|2026-10-21T13:30:00Z"
    assert bound_key(full) == correlation_key(LONG_UID, start)
    assert bound_key(bound_key(full)) == bound_key(full)
