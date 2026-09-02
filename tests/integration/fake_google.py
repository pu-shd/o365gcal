"""In-memory stand-in for the Google Calendar connector.

Reproduces the connector behaviours that actually bite: opaque server-assigned ids,
404 on a deleted event, 429 throttling, and the fact that UpdateEvent overwrites the
whole event rather than patching it.
"""

from __future__ import annotations

from o365gcal.apply import GoogleClient, GoogleNotFound, GoogleThrottled


class FakeGoogle(GoogleClient):
    def __init__(self, *, fail_update_with_404: set[str] | None = None,
                 throttle_after: int | None = None):
        self.events: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self._seq = 0
        self._fail_update_404 = fail_update_with_404 or set()
        self._throttle_after = throttle_after

    def _check_throttle(self) -> None:
        if self._throttle_after is not None and len(self.calls) >= self._throttle_after:
            raise GoogleThrottled("429 rateLimitExceeded")

    def create_event(self, payload: dict) -> dict:
        self._check_throttle()
        self._seq += 1
        eid = f"goog{self._seq:04d}"
        self.calls.append(("create", eid))
        self.events[eid] = dict(payload, id=eid, htmlLink=f"https://calendar.google.com/{eid}")
        return self.events[eid]

    def update_event(self, event_id: str, payload: dict) -> dict:
        self._check_throttle()
        self.calls.append(("update", event_id))
        if event_id in self._fail_update_404 or event_id not in self.events:
            raise GoogleNotFound(f"404 Not Found: {event_id}")
        # Full overwrite, matching the connector's documented reset-on-omit behaviour.
        self.events[event_id] = dict(
            payload, id=event_id, htmlLink=self.events[event_id]["htmlLink"]
        )
        return self.events[event_id]

    def delete_event(self, calendar_id: str, event_id: str) -> None:
        self._check_throttle()
        self.calls.append(("delete", event_id))
        if event_id not in self.events:
            raise GoogleNotFound(f"404 Not Found: {event_id}")
        del self.events[event_id]

    @property
    def mutation_count(self) -> int:
        return len(self.calls)
