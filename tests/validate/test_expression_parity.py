"""Proves the flow expressions and the Python engine compute identical values.

Without this, "the tested logic is the shipped logic" would be an assertion rather
than a fact. It has already earned its keep: it caught the Python engine collapsing
whitespace runs with a regex, which the expression language cannot do.
"""

from datetime import datetime, timezone

import pytest
from o365gcal import expressions as x
from o365gcal.model import Config, ResponseType
from o365gcal.diff import build_plan
from o365gcal.normalize import body_fingerprint, content_hash, correlation_key, iso_utc
from conftest import make_event, make_row
from wdl import Evaluator, WdlError

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def ev(item=None, params=None, variables=None):
    return Evaluator(
        {
            "item": item or {},
            "parameters": params or {},
            "variables": variables or {},
            "utcNow": NOW,
        }
    )


def envmap(**overrides):
    """Environment variables keyed the way `parameters()` addresses them."""
    base = {
        "WindowPastDays": 7, "WindowFutureDays": 120, "MaxDeletePercent": 25,
        "MinDeletesBeforeBreaker": 5, "MaxMutationsPerRun": 60,
        "HeartbeatStaleMinutes": 90, "HidePrivateEventDetails": False,
    }
    base.update(overrides)
    return {f"{k} (o3gc_{k})": v for k, v in base.items()}


@pytest.mark.parametrize(
    "preview",
    ["", "Agenda: budget", "  padded  ", "line one\nline two", "tab\there",
     "\r\nleading crlf", "x" * 900],
    ids="empty simple padded newline tab crlf long".split(),
)
def test_body_fingerprint_matches_engine(preview):
    got = ev({"bodyPreview": preview}).eval(x.body_fingerprint("item()?['bodyPreview']"))
    assert got == body_fingerprint(preview)


@pytest.mark.parametrize(
    "uid,start",
    [
        ("abc", "2026-09-01T14:00:00Z"),
        ("040000008200E00074C5B7101A82E008", "2026-12-31T23:30:00Z"),
        ("uid-with-dashes", "2026-03-08T07:00:00Z"),
    ],
)
def test_correlation_key_matches_engine(uid, start):
    parsed = datetime.fromisoformat(start.replace("Z", "+00:00"))
    got = ev({"iCalUId": uid, "start": start}).eval(
        x.correlation_key("item()?['iCalUId']", "item()?['start']")
    )
    assert got == correlation_key(uid, parsed)


def test_iso_utc_matches_engine():
    stamp = "2026-09-01T14:05:09.482Z"
    got = ev({"s": stamp}).eval(x.iso_utc("item()?['s']"))
    assert got == iso_utc(datetime.fromisoformat(stamp.replace("Z", "+00:00")))
    assert got == "2026-09-01T14:05:09Z", "sub-second precision must be dropped"


@pytest.mark.parametrize(
    "deletes,active,expected",
    [
        (0, 40, False),    # nothing to delete
        (4, 40, False),    # 10%, under both limits
        (5, 40, False),    # exactly at the absolute floor
        (6, 40, False),    # 15%, over the floor but under the percentage
        (20, 40, True),    # 50%, over both
        (40, 40, True),    # the catastrophic empty-read case
        (2, 4, False),     # sparse calendar: 50% but only 2 deletes
        (6, 8, True),      # 75% and over the floor
    ],
)
def test_circuit_breaker_matches_engine(deletes, active, expected):
    """The flow's breaker must trip on exactly the same inputs as `diff.py`'s."""
    from conftest import make_event, make_row
    from o365gcal.diff import build_plan

    e = ev(
        params=envmap(),
        variables={"Deletes": list(range(deletes)), "ActiveRows": list(range(active))},
    )
    assert e.eval(x.CIRCUIT_BREAKER_TRIPPED) is expected

    cfg = Config()
    events = [make_event(f"E {i}", offset_days=i + 1) for i in range(active)]
    rows = [make_row(ee, cfg, f"g-{i}") for i, ee in enumerate(events)]
    plan = build_plan(events[: active - deletes], rows, cfg, NOW)
    assert plan.circuit_breaker_tripped is expected


@pytest.mark.parametrize(
    "offset_days,expected",
    [(-8, False), (-6, True), (0, True), (119, True), (121, False)],
)
def test_in_window_matches_engine(offset_days, expected):
    from datetime import timedelta

    from o365gcal.diff import in_window

    start = NOW + timedelta(days=offset_days)
    got = ev({"OccurrenceStartUtc": start.strftime("%Y-%m-%dT%H:%M:%SZ")},
             params=envmap()).eval(x.IN_WINDOW)
    assert got is expected
    assert in_window(start, NOW, Config()) is expected


@pytest.mark.parametrize(
    "response,expected",
    [
        ("notResponded", True),
        ("tentativelyAccepted", True),
        ("accepted", False),
        ("declined", False),
        ("organizer", False),
    ],
)
def test_awaits_response_matches_engine(response, expected):
    from conftest import make_event
    from o365gcal.render import awaits_response

    assert ev({"responseType": response}).eval(x.AWAITS_RESPONSE) is expected
    assert awaits_response(make_event(my_response=ResponseType(response))) is expected


def test_awaits_response_ignores_cancellation_on_the_flow_side():
    """The engine still suppresses cancelled events, but the flow has no isCancelled
    field to test - the calendar view simply omits cancelled occurrences, so anything
    the flow sees is by definition live."""
    from conftest import make_event
    from o365gcal.render import awaits_response

    e = make_event(my_response=ResponseType.NOT_RESPONDED, is_cancelled=True)
    assert awaits_response(e) is False


def test_heartbeat_stale_threshold():
    from datetime import timedelta

    fresh = (NOW - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = (NOW - timedelta(minutes=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert ev({"LastSuccessUtc": fresh}, params=envmap()).eval(x.HEARTBEAT_STALE) is False
    assert ev({"LastSuccessUtc": stale}, params=envmap()).eval(x.HEARTBEAT_STALE) is True


def test_unknown_environment_variable_is_an_error():
    """A typo'd env var reference must fail the build, not evaluate to null at runtime."""
    with pytest.raises(WdlError, match="undeclared environment variable"):
        ev(params={}).eval(x.env("NoSuchVariable"))


def test_unimplemented_function_is_an_error():
    """New WDL functions must be added to the evaluator, not silently unverified."""
    with pytest.raises(WdlError, match="unimplemented"):
        ev().eval("rand(1, 5)")


# ---------------------------------------------------------------------------
# Full-fingerprint parity.
#
# Previously only individual sub-expressions were compared, which let a real
# divergence through: the engine sorted attendees into `required:a@x:none;...` while
# the flow could only lowercase-concatenate two semicolon-separated strings. The
# pieces each looked fine; the whole was never checked. This compares the complete
# thirteen-field value the flow actually stores.
# ---------------------------------------------------------------------------

# Imported, not restated: the flow, this test and the engine must all read from one
# definition or the drift this test exists to catch can hide in a fourth copy.
FLOW_FINGERPRINT_PARTS = x.FLOW_FINGERPRINT_PARTS


def _flow_item(event):
    """The connector's shape for one occurrence, from the real swagger field names."""
    return {
        "subject": event.subject,
        "startWithTimeZone": event.start_utc.isoformat(),
        "endWithTimeZone": event.end_utc.isoformat(),
        "isAllDay": event.is_all_day,
        "location": event.location,
        "body": event.body_html,
        "showAs": event.show_as,
        "sensitivity": event.sensitivity.value,
        "requiredAttendees": event.required_attendees,
        "optionalAttendees": event.optional_attendees,
        "organizer": event.organizer,
        "responseType": event.my_response.value,
    }


def _eval_flow_fingerprint(event, cfg):
    from o365gcal.normalize import _is_hidden

    params = {
        f"{k} (o3gc_{k})": v
        for k, v in {
            "TitlePrefix": cfg.title_prefix,
            "HidePrivateEventDetails": cfg.hide_private_event_details,
        }.items()
    }
    ev = Evaluator({
        "item": _flow_item(event),
        "parameters": params,
        "variables": {},
        "utcNow": NOW,
        # Compose_Hidden is a separate action in the flow; supply its value the same
        # way the flow computes it.
        "outputs": {"Compose_Hidden": _is_hidden(event, cfg)},
    })
    return "\x1f".join(
        ev.eval(FLOW_FINGERPRINT_PARTS[name]) for name in x.FINGERPRINT_FIELDS
    )


@pytest.mark.parametrize(
    "mutate,label",
    [
        (lambda e: None, "baseline"),
        (lambda e: setattr(e, "subject", "Renamed meeting"), "subject"),
        (lambda e: setattr(e, "location", "Friend 006"), "location"),
        (lambda e: setattr(e, "is_all_day", True), "allday"),
        (lambda e: setattr(e, "body_html", "<p>Agenda: budget</p>"), "body"),
        (lambda e: setattr(e, "body_html", ""), "empty body"),
        (lambda e: setattr(e, "show_as", "Free"), "showas casing"),
        (lambda e: setattr(e, "required_attendees", "A@example.com;b@example.com"), "attendee casing"),
        (lambda e: setattr(e, "optional_attendees", "c@example.com"), "optional attendees"),
        (lambda e: setattr(e, "organizer", "Jane@example.com"), "organizer casing"),
        (lambda e: setattr(e, "my_response", ResponseType.ACCEPTED), "rsvp"),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_full_fingerprint_matches_engine(mutate, label, config):
    from conftest import make_event
    from o365gcal.normalize import fingerprint

    e = make_event(
        organizer="jane@example.com",
        required_attendees="a@example.com;b@example.com",
        body_html="<p>hello</p>",
    )
    mutate(e)
    assert _eval_flow_fingerprint(e, config) == fingerprint(e, config), label


@pytest.mark.parametrize("hide", [False, True])
def test_full_fingerprint_parity_when_hiding_private_events(hide, config):
    from conftest import make_event
    from o365gcal.model import Sensitivity
    from o365gcal.normalize import fingerprint

    config.hide_private_event_details = hide
    e = make_event(
        subject="Confidential review",
        location="Nassau Hall",
        body_html="<p>sensitive</p>",
        organizer="jane@example.com",
        required_attendees="a@example.com",
    )
    e.sensitivity = Sensitivity.PRIVATE
    assert _eval_flow_fingerprint(e, config) == fingerprint(e, config)




# --- the hoisted per-event pipeline ----------------------------------------
#
# The six Composes per event became one Select over the whole array, and the loop now
# runs only for events that need a decision. Both changes are only safe if the
# standalone expressions compute what the Compose chain computed, and if the filter
# selects exactly the events the engine would have acted on.


def _standalone_eval(expr, event, cfg, rows=None):
    """Evaluate an expression with no Compose chain behind it - only the event."""
    params = {
        f"{k} (o3gc_{k})": v
        for k, v in {
            "TitlePrefix": cfg.title_prefix,
            "HidePrivateEventDetails": cfg.hide_private_event_details,
        }.items()
    }
    # _flow_item predates the key being computed from the event itself, so the
    # identity fields the standalone expressions read are added here.
    item = dict(_flow_item(event), **{
        "iCalUId": event.ical_uid,
        "id": event.event_id,
        "seriesMasterId": "",
    })
    return Evaluator({
        "item": item,
        "parameters": params,
        "variables": {},
        "utcNow": NOW,
        "outputs": {},
        "body": {"Select_Row_Pairs": rows or []},
    }).eval(expr)


@pytest.mark.parametrize("hide", [False, True], ids=["visible", "busy-only"])
def test_standalone_fingerprint_matches_the_compose_chain(hide, config):
    """Same value, one action instead of six - or the hoist silently rewrites every
    event on the next run."""
    config.hide_private_event_details = hide
    for subject in ("Standup", "Faculty Meeting"):
        event = make_event(subject, body_html="<p>Agenda: budget</p>", location="Room B")
        chained = _eval_flow_fingerprint(event, config)
        hoisted = _standalone_eval(x.standalone(x.flow_fingerprint()), event, config)
        assert hoisted == chained, f"{subject} under hide={hide}"


def test_standalone_key_matches_the_engine(config):
    event = make_event("Standup")
    got = _standalone_eval(x.KEY_INLINE, event, config)
    assert got == correlation_key(event.ical_uid, event.start_utc)


def _pairs_for(rows):
    """The Select_Row_Pairs result, evaluated rather than assumed."""
    out = []
    for row in rows:
        item = {"Title": row.correlation_key, "ContentFingerprint": row.content_hash}
        out.append(Evaluator({"item": item, "utcNow": NOW}).eval(x.ROW_PAIR))
    return out


@pytest.mark.parametrize(
    "label,stale,drop_row",
    [("unchanged", False, False),
     ("fingerprint moved", True, False),
     ("never mirrored", False, True)],
)
def test_prefilter_selects_exactly_what_the_engine_would_act_on(label, stale, drop_row, config):
    """The loop used to spend twelve actions per event concluding 'nothing to do'.
    Skipping that is only correct if the filter agrees with the engine about which
    events need a decision."""
    event = make_event("Standup")
    rows = [] if drop_row else [make_row(event, config, stale=stale)]

    needs_work = _standalone_eval(
        x.NEEDS_WORK.replace("item()?['key']", f"'{correlation_key(event.ical_uid, event.start_utc)}'")
                    .replace("item()?['fingerprint']", f"'{content_hash(event, config)}'"),
        event, config, rows=_pairs_for(rows),
    )

    plan = build_plan([event], rows, config, NOW)
    engine_acts = bool(plan.creates or plan.updates)
    assert needs_work == engine_acts, (
        f"{label}: filter says needs_work={needs_work}, engine says {engine_acts}"
    )


def test_the_verification_sweep_still_forces_a_rebuild(config):
    """Verification clears a row's fingerprint so the event is rebuilt. That relies on
    the cleared value failing to match, which is now the filter's job."""
    event = make_event("Standup")
    row = make_row(event, config)
    row.content_hash = ""
    needs_work = _standalone_eval(
        x.NEEDS_WORK.replace("item()?['key']", f"'{correlation_key(event.ical_uid, event.start_utc)}'")
                    .replace("item()?['fingerprint']", f"'{content_hash(event, config)}'"),
        event, config, rows=_pairs_for([row]),
    )
    assert needs_work, "a cleared fingerprint must not read as unchanged"
