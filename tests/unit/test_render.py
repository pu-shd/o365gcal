"""Rendering, with particular attention to not emailing anyone by accident."""
from conftest import make_event
from o365gcal.model import ResponseType, Sensitivity
from o365gcal.normalize import MARKER_PREFIX
from o365gcal.render import awaits_response, render_description, render_google_event


def _invited(**kw):
    return make_event(
        "Faculty Meeting",
        location="Friend 006",
        body_html=kw.pop("body_html", "<p>Agenda: budget</p>"),
        is_html=kw.pop("is_html", True),
        organizer="jane@example.com",
        required_attendees="a@example.com;b@example.com",
        optional_attendees="c@example.com",
        my_response=ResponseType.NOT_RESPONDED,
        web_link="https://outlook.office.com/calendar/item/1",
        **kw,
    )


def test_attendees_are_never_sent_to_google(config):
    """Two reasons, and either would be sufficient.

    A non-empty attendees list makes Google email an invitation to every Outlook
    attendee from the user's own account - duplicate invites to real colleagues for
    every mirrored meeting. And the connector will not accept "no attendees" anyway:
    CreateEvent rejects an empty string and a null alike with "Invalid attendee
    email", and a flow cannot omit a parameter conditionally. So the parameter is not
    sent at all."""
    assert "attendees" not in render_google_event(_invited(), config)


def test_description_exposes_invitation_details(config):
    out = render_description(_invited(), config)
    assert "Organizer:     jane@example.com" in out
    assert "⚠ Not responded" in out
    assert "Required:      a@example.com,b@example.com" in out
    assert "Optional:      c@example.com" in out
    assert "https://outlook.office.com/calendar/item/1" in out


def test_html_body_is_linked_not_dumped(config):
    """The connector gives raw HTML with no plain-text alternative and nothing here
    can strip markup, so an Outlook HTML document must not be pasted into Google."""
    out = render_description(_invited(is_html=True, body_html="<html><head><style>x</style>"), config)
    assert "<style>" not in out and "<html>" not in out
    assert "open the Outlook link below" in out


def test_plain_text_body_is_carried_across(config):
    out = render_description(_invited(is_html=False, body_html="Agenda: budget"), config)
    assert "Agenda: budget" in out


def test_body_excerpt_is_bounded(config):
    """Asserts on the excerpt itself, not on a character count of the whole
    description - counting a letter that also occurs in the surrounding boilerplate
    makes the test fail for reasons unrelated to the bound."""
    from o365gcal.render import BODY_EXCERPT_CHARS, body_excerpt

    event = _invited(is_html=False, body_html="x" * 50_000)
    assert len(body_excerpt(event)) == BODY_EXCERPT_CHARS
    assert body_excerpt(event) in render_description(event, config)


def test_description_carries_repair_marker(config):
    e = _invited()
    out = render_description(e, config)
    assert f"{MARKER_PREFIX} {e.ical_uid}|" in out


def test_busy_only_leaks_nothing(config):
    config.hide_private_event_details = True
    e = _invited()
    e.sensitivity = Sensitivity.PRIVATE
    out = render_description(e, config)
    payload = render_google_event(e, config)
    assert payload["summary"] == "Busy"
    assert payload["location"] == ""
    for leak in ("Faculty Meeting", "budget", "Friend 006", "a@example.com"):
        assert leak not in out


def test_busy_only_sends_no_attendee_data_at_all(config):
    config.hide_private_event_details = True
    e = _invited()
    e.sensitivity = Sensitivity.PRIVATE
    payload = render_google_event(e, config)
    assert "attendees" not in payload
    assert "princeton.edu" not in payload["description"]


def test_title_prefix_applied(config):
    config.title_prefix = "[Outlook] "
    assert render_google_event(_invited(), config)["summary"] == "[Outlook] Faculty Meeting"


def test_update_payload_is_always_complete(config):
    """The connector's UpdateEvent resets omitted fields, so a partial payload
    silently wipes location/description on every edit."""
    payload = render_google_event(_invited(), config)
    assert set(payload) == {
        "calendarId", "summary", "start", "end",
        "description", "location", "status", "isAllDay",
    }


def test_cancelled_is_flagged_in_description(config):
    e = _invited()
    e.is_cancelled = True
    assert "CANCELLED in Outlook" in render_description(e, config)


def test_awaits_response_drives_digest_section(config):
    for state, expected in [
        (ResponseType.NOT_RESPONDED, True),
        (ResponseType.TENTATIVE, True),
        (ResponseType.ACCEPTED, False),
        (ResponseType.DECLINED, False),
        (ResponseType.ORGANIZER, False),
    ]:
        e = _invited()
        e.my_response = state
        assert awaits_response(e) is expected


def test_cancelled_event_no_longer_awaits_response(config):
    e = _invited()
    e.is_cancelled = True
    assert awaits_response(e) is False
