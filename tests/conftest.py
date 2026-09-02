import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from o365gcal.model import Config, MapRow, OutlookEvent, ResponseType, SyncState  # noqa: E402
from o365gcal.normalize import content_hash, correlation_key  # noqa: E402

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def config():
    return Config(google_calendar_id="test@group.calendar.google.com", alert_email="u@example.com")


@pytest.fixture
def now():
    return NOW


def make_event(subject="Standup", offset_days=1, duration_min=30, **kwargs):
    start = kwargs.pop("start_utc", NOW + timedelta(days=offset_days))
    return OutlookEvent(
        ical_uid=kwargs.pop("ical_uid", f"uid-{subject.lower().replace(' ', '-')}"),
        event_id=kwargs.pop("event_id", f"AAMk-{subject}"),
        subject=subject,
        start_utc=start,
        end_utc=kwargs.pop("end_utc", start + timedelta(minutes=duration_min)),
        organizer=kwargs.pop("organizer", "jane@example.com"),
        last_modified_utc=kwargs.pop("last_modified_utc", NOW),
        **kwargs,
    )


def make_row(event, config, google_id="g-1", state=SyncState.ACTIVE, stale=False):
    return MapRow(
        correlation_key=correlation_key(event.ical_uid, event.start_utc),
        google_event_id=google_id,
        content_hash="stale-hash" if stale else content_hash(event, config),
        sync_state=state,
        outlook_event_id=event.event_id,
        outlook_ical_uid=event.ical_uid,
        occurrence_start_utc=event.start_utc,
    )


@pytest.fixture
def event():
    return make_event()
