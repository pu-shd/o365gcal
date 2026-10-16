# Architecture

## Shape

```
Outlook change trigger (1) ──┐ creates only
                             ├──► 2 Apply Event ──► Google Calendar
Scheduled reconciler (3) ────┘ engine of record         │
                                                        ▼
                                          SharePoint: SyncMap · Log · Health
4 Digest · 5 Watchdog · 6 Backup · 7 Dedup · 8 Invitation reminder · 0 Setup
```

Microsoft documents the Outlook change trigger as firing for whole series, emitting
spurious updates, reporting `Deleted` for events leaving its window, firing twice on
an accepted invitation, and lagging up to an hour. So the reconciler alone, working
from a full calendar read every 15 minutes, guarantees correctness; the trigger only
speeds up creates. Flow 2 is the single writer to Google, so the two callers cannot
produce duplicates.

## Constraints that forced it

| Constraint | Consequence |
|---|---|
| Google connector has no recurrence | Series are expanded into single events |
| No private properties on Google events | Correlation lives in our SharePoint map |
| `ListEvents`: one unordered page, no paging | Google cannot be enumerated; the map is the only record of what we may delete |
| 100 calls per 60 s per connection | Mutations capped per run; backlogs drain across runs |
| `UpdateEvent` uses `updatedEvent/*`, create uses `newEvent/*` | Every update sends all fields |
| No hash and no regex in flow expressions | Change detection compares a plain fingerprint string; body handling is limited to what a flow can compute |

`src/o365gcal/` is the reference engine, and `tests/validate/wdl.py` evaluates the
shipped expressions so the tests check what the flows actually do.

## Correctness

- **Fingerprint:** twelve fields joined by `0x1f` (subject, start, end, all-day,
  location, a 256-character body prefix with its length, show-as, sensitivity,
  attendees, organizer, your response, privacy setting). Unchanged → no API call.
  `lastModifiedDateTime` and the Graph id are excluded because Exchange rewrites them.
- **Key:** `{iCalUId}|{startUtc}`, capped to its last 255 characters (SharePoint's text
  limit). The Graph id is not used: it changes when an invitation is accepted.
- **Deletion** is inferred from absence in a full read, so a **circuit breaker** refuses
  any batch over 25% of the mirror *and* over 5 events: a failed read looks like a
  mass cancellation. Only events whose id we recorded are ever deleted.
- **Throttle cap:** 60 writes per run, creates first, deletes last.
- **Attendees** go in the description as text; real Google attendees would make Google
  invite every Outlook attendee from the user's account.

## Alerting

Try/Catch in each flow, a heartbeat watchdog for flows that stop running or lose
consent, and Power Platform's own failure emails for the watchdog itself (see
[ADMIN.md](ADMIN.md)).

## Backup and duplicates

Flow 6 writes the three lists as JSON to the state site's library, using only the
SharePoint connection. Every mirrored event's description carries
`o365gcal-key: {key}`, so flow 7 can group duplicates and rebuild the map from Google
alone. Its rules (`src/o365gcal/dedup.py`): unmarked events and single events are never
touched, exactly one survivor per group, the survivor is chosen deterministically, and
any day that cannot be read completely blocks all deletion.

## Testing

| Layer | Proves |
|---|---|
| Static | Flows parse, references resolve, Standard connectors only, the packed zip contains every flow |
| Unit | Fingerprint, diff, circuit breaker, cap, rendering, dedup |
| Parity | Shipped expressions, evaluated, equal the Python engine |
| Mocked | Full cycles against fake Graph and Google, including 429s and 404 repair |
| Contract | Parameter names against live connector definitions (opt-in) |

The live Power Automate runtime is not exercised.
