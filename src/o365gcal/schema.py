"""SharePoint list schema -- the single definition of the state store.

Flow 0 provisions these lists through `Send an HTTP request to SharePoint` (a
Standard action, unlike the generic premium HTTP action), so the same field
definitions drive provisioning, the docs, and the static tests.

Internal names deliberately avoid SharePoint's reserved names and its habit of
mangling display names containing spaces into `_x0020_` escapes.
"""

from __future__ import annotations

# SharePoint FieldTypeKind values used below.
TEXT, NOTE, DATETIME, NUMBER, BOOLEAN, URL, CHOICE = 2, 3, 4, 9, 8, 11, 6

SYNC_MAP = {
    "list": "O365GCalSyncMap",
    "description": "Correlates each mirrored Outlook occurrence to its Google event. "
                   "Source of truth for what this automation created, and therefore for "
                   "what it is allowed to delete.",
    "fields": [
        # Title holds the correlation key `{iCalUId}|{startUtc}`. Indexed because
        # every single-event lookup filters on it; without the index SharePoint
        # throws a threshold error once the list passes 5,000 rows.
        {"name": "OutlookEventId", "type": TEXT, "indexed": False,
         "note": "Current Graph event id. Changes when an invitation is accepted, which is exactly why it is not the correlation key."},
        {"name": "OutlookICalUId", "type": TEXT, "indexed": True,
         "note": "Stable across the id rewrite and shared by every occurrence of a series."},
        {"name": "OutlookSeriesMasterId", "type": TEXT, "indexed": False},
        {"name": "OccurrenceStartUtc", "type": DATETIME, "indexed": True,
         "note": "Drives the in-window filter so out-of-window rows are never mistaken for deletions."},
        {"name": "GoogleEventId", "type": TEXT, "indexed": False,
         "note": "Empty means nothing to delete. We only ever delete an id we recorded ourselves."},
        {"name": "GoogleHtmlLink", "type": URL, "indexed": False},
        {"name": "ContentFingerprint", "type": NOTE, "indexed": False,
         "note": "Change detection. A plain comparable string rather than a hash, because the Power Automate expression language has no hashing function."},
        {"name": "SyncState", "type": TEXT, "indexed": True,
         "note": "Active | Deleted | Error | Skipped."},
        {"name": "LastSyncedUtc", "type": DATETIME, "indexed": False},
        {"name": "LastOutlookModifiedUtc", "type": DATETIME, "indexed": False},
        {"name": "AttendeeSummary", "type": NOTE, "indexed": False},
        {"name": "MyResponse", "type": TEXT, "indexed": False},
        {"name": "ErrorCount", "type": NUMBER, "indexed": False},
        {"name": "LastError", "type": NOTE, "indexed": False},
        {"name": "OwnerUpn", "type": TEXT, "indexed": False},
    ],
}

LOG = {
    "list": "O365GCalLog",
    "description": "Append-only audit trail. Input to the daily digest and the first "
                   "place to look when something did not mirror.",
    "fields": [
        # Title holds the run id.
        {"name": "Timestamp", "type": DATETIME, "indexed": True},
        {"name": "FlowName", "type": TEXT, "indexed": False},
        {"name": "Level", "type": TEXT, "indexed": True, "note": "Info | Warn | Error."},
        {"name": "Operation", "type": TEXT, "indexed": False,
         "note": "Create | Update | Delete | NoOp | Skip."},
        {"name": "CorrelationKey", "type": TEXT, "indexed": True},
        {"name": "OutlookEventId", "type": TEXT, "indexed": False},
        {"name": "GoogleEventId", "type": TEXT, "indexed": False},
        {"name": "Message", "type": NOTE, "indexed": False},
        {"name": "DetailJson", "type": NOTE, "indexed": False},
    ],
}

HEALTH = {
    "list": "O365GCalHealth",
    "description": "One row per flow. The watchdog reads this; a stale row is how an "
                   "expired connection or a switched-off flow gets noticed, neither of "
                   "which any in-flow error handler can catch.",
    "fields": [
        # Title holds the flow name.
        {"name": "LastSuccessUtc", "type": DATETIME, "indexed": False},
        {"name": "LastRunUtc", "type": DATETIME, "indexed": False},
        {"name": "LastRunStatus", "type": TEXT, "indexed": False},
        {"name": "ConsecutiveFailures", "type": NUMBER, "indexed": False},
        {"name": "ItemsProcessed", "type": NUMBER, "indexed": False},
        {"name": "LastDetail", "type": NOTE, "indexed": False},
        {"name": "StaleAfterMinutes", "type": NUMBER, "indexed": False,
         "note": "Per-flow staleness threshold. A single global value marks a daily "
                 "flow as broken for most of every day."},
        {"name": "AffectsSync", "type": TEXT, "indexed": False,
         "note": "yes only for the reconciler. Determines whether an alert may claim "
                 "the calendar is going stale."},
    ],
}

ALL_LISTS = [SYNC_MAP, LOG, HEALTH]

#: Flow names, used as Health list row titles and in log rows.
FLOWS = [
    "0 Setup and Provision",
    "1 Sync Outlook Trigger",
    "2 Apply Event",
    "3 Reconcile",
    "4 Digest",
    "5 Watchdog",
    "6 Backup State",
    "7 Dedup and Repair",
    "8 Invitation Reminder",
]

#: How long each flow may be silent before something is actually wrong, in minutes.
#:
#: One global threshold does not work here: the flows run on cadences from every 30
#: minutes to once a day. A 90-minute rule marks a daily digest as broken for 22 hours
#: out of every 24, and an alert that cries wolf daily is worse than no alert - people
#: stop reading it, including on the day it is right.
#:
#: The multiple deliberately shrinks as the cadence lengthens. A flow running every
#: 30 minutes can skip one run harmlessly, so it gets 3x headroom. A daily flow
#: missing a day is itself worth reporting, so it gets only enough grace for a late
#: run - not enough to hide a skipped one.
HEARTBEAT_EXPECTATIONS = {
    "3 Reconcile": 45,        # every 15 minutes
    "5 Watchdog": 180,        # hourly
    "4 Digest": 1800,         # daily; 30 hours allows a late run without alarm
    "6 Backup State": 1800,   # daily
    # Runs hourly but only sends on its own cadence; it stamps health on every
    # evaluation, so an hourly threshold with headroom is right.
    "8 Invitation Reminder": 180,
}

#: Flows deliberately without a heartbeat expectation, and why. Listed explicitly so
#: that adding a flow forces a decision rather than defaulting into false alarms.
NO_HEARTBEAT = {
    "0 Setup and Provision": "run once by hand at install; silence afterwards is correct",
    "1 Sync Outlook Trigger": (
        "event-driven. It runs only when Outlook reports a change, so on a quiet "
        "calendar - a weekend, a holiday - it is legitimately silent for days. It is "
        "also only a latency optimisation: flow 3 is the engine of record and IS "
        "monitored, so a broken trigger costs speed, never correctness."
    ),
    "2 Apply Event": "a child flow; it runs only when a parent calls it",
    "7 Dedup and Repair": "a manual repair tool; it should be silent",
}

#: Kept for the Health list: every flow that gets a row seeded.
HEARTBEAT_FLOWS = list(HEARTBEAT_EXPECTATIONS)


def create_list_body(spec: dict) -> dict:
    """POST body for `_api/web/lists` -- 100 is the generic list template."""
    return {
        "__metadata": {"type": "SP.List"},
        "BaseTemplate": 100,
        "Title": spec["list"],
        "Description": spec["description"],
        "ContentTypesEnabled": False,
    }


def add_field_body(field: dict) -> dict:
    """POST body for `_api/web/lists/getbytitle('X')/fields`."""
    body = {
        "__metadata": {"type": "SP.Field"},
        "Title": field["name"],
        "FieldTypeKind": field["type"],
        "Required": False,
        "EnforceUniqueValues": False,
    }
    if field["type"] == NOTE:
        body["__metadata"]["type"] = "SP.FieldMultiLineText"
        body["NumberOfLines"] = 6
        body["RichText"] = False
    return body
