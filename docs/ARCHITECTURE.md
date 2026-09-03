# Architecture

## The problem this shape solves

The obvious design — trigger on an Outlook calendar change, write to Google — does not
work reliably, because Microsoft documents the Outlook change trigger as:

- firing for **every event in a series** when the series master changes,
- emitting **spurious updates** from Exchange's own internal processes,
- reporting **`Deleted`** for events that merely fell outside the trigger's window,
- **firing twice** when a user accepts an invitation, because Outlook rewrites the
  event id and created date, and
- lagging **up to an hour**.

So the trigger cannot be the mechanism that guarantees anything. It is used only to
make the common case fast.

**The scheduled reconciler is the engine of record.** Every requirement is satisfied by
flow 3 alone, from a full calendar read. A dropped, duplicated or spurious trigger
therefore costs latency and never correctness.

```
Outlook change trigger ──┐
   (fast path, hints)    ├──► 2 Apply Event (child) ──► Google Calendar CRUD
Scheduled reconciler ────┘              │
   (engine of record)                   └──► SyncMap + Log (SharePoint)
                                                  │
                        4 Digest  ◄───────────────┤
                        5 Watchdog◄───────────────┘
```

## Constraints that shaped the design

The Google Calendar connector is Standard — no premium licence — but it is weak:

| Constraint | Consequence |
|---|---|
| No recurrence parameter at all | Series are expanded into individual occurrences via Outlook's calendar view |
| No extended/private properties | Correlation cannot be stored on the Google event; we keep our own map |
| `ListEvents` returns "the first page of arbitrarily ordered events" — no paging, no ordering, no `updatedMin`, no `showDeleted` | Google can never be reliably enumerated. The SharePoint map is the only record of what we created, and therefore of what we may delete |
| 100 calls / 60 seconds per connection | Mutations are capped per run and backlogs drain across runs |
| `UpdateEvent` is a PATCH (`updatedEvent/*`), and its body parameter is *not* the `item` convention other connectors use | Every update sends the complete field set, so a field cleared in Outlook is cleared on Google rather than left stale |

And two from Power Automate itself:

| Constraint | Consequence |
|---|---|
| **No hash function** in the expression language | Change detection stores a comparable *fingerprint string*, not a digest |
| **No regex** in the expression language | Body normalisation uses Graph's already-plain `bodyPreview`, and flattens only line breaks and tabs — because that is all a flow can do, and the reference engine must do exactly the same |

That second pair is why `src/o365gcal/` exists and why `tests/validate/wdl.py`
evaluates the shipped expressions: the temptation is to write elegant Python and
approximately-equivalent flow logic, and then the tests verify something the product
does not do.

## Correctness core

Each occurrence is reduced to fourteen normalised fields joined by `0x1f`:

```
subject │ startUtc │ endUtc │ isAllDay │ location │ bodyFingerprint │ showAs
        │ isCancelled │ sensitivity │ attendees │ organizer │ myResponse
        │ privacyMode │ copyAttendees
```

Stored on the map row as `ContentFingerprint`. Comparing it answers "did this change?"
in one operation and costs zero API calls, which is what keeps a 15-minute reconcile
of a large calendar inside Google's rate limit.

Deliberately **excluded**: `lastModifiedDateTime` and the Graph event `id`. Exchange
rewrites both without the user changing anything; including either would rewrite the
entire calendar on every run.

Deliberately **included**: the config fields that alter rendered output. Flipping
`HidePrivateEventDetails` *should* rewrite every event.

## Correlation key

`{iCalUId}|{occurrenceStartUtc}` — not the Graph event `id`, which Outlook rewrites
when an invitation is accepted. That rewrite is the documented cause of the trigger
double-fire; keying on `iCalUId` collapses it to a single update instead of mirroring
the meeting twice.

## Safety mechanisms

Deletion is the only irreversible operation, and it is inferred from *absence* in the
Outlook read. A throttled response, an expired token, a lost mailbox permission and a
genuine mass cancellation are indistinguishable from here.

**Circuit breaker.** If one run would delete more than `MaxDeletePercent` (25%) of
active mirrored events **and** more than `MinDeletesBeforeBreaker` (5) in absolute
terms, it deletes *nothing*, logs an error and emails an alert. All-or-nothing is
deliberate: a partial delete batch is both destructive and inconsistent.

The rule is conjunctive because a percentage test alone is useless on a sparse
calendar — cancelling one of three meetings is 33% and would be refused forever,
training the user to ignore the alert.

**Throttle cap.** At most `MaxMutationsPerRun` (60) Google writes per run, applied
create → update → delete so a truncated run has done its additive, reversible work
first. Overflow is counted and reported: a truncated run must never read as complete.

**Ownership.** We only ever delete a Google event whose id we recorded ourselves.
Events created natively in Google are never touched.

## Why CRUD is centralised in flow 2

Both the trigger path and the reconciler mutate Google. Two writers means duplicate
events whenever they overlap. Flow 2 is the single writer and every operation is
idempotent — it reads the map row first and no-ops when the fingerprint matches — so
the two callers can run concurrently and repeatedly without harm.

Flow 1 goes further and only ever issues **Create**. Updates and deletions need a
fingerprint computed from a consistent full read, which only flow 3 has.

## Attendees

Attendees are rendered as **text in the Google event description**, not attached as
real Google attendees.

A non-empty Google attendee list makes Google send a calendar invitation from the
user's own Google account to every attendee — a duplicate invite to real colleagues for
every mirrored meeting. The description gives full visibility of who is invited and
whether an RSVP is outstanding, and emails nobody.

`CopyAttendeesAsGoogleAttendees` exists for anyone who genuinely wants the other
behaviour, and defaults off.

## Alerting

Three independent layers, because they fail differently:

1. **In-flow Try/Catch** — catches errors during a run. Blind to a run that never happens.
2. **Heartbeat + watchdog** — catches silence: a flow switched off, suspended by the
   platform, or whose Google OAuth consent expired. The watchdog also probes both
   connections live, because expired consent is the most common real breakage and
   produces no failed run at all.
3. **Power Platform's built-in flow failure notifications** — catches the watchdog
   itself dying. A watchdog cannot report its own death. See `ADMIN.md`.

## Backup, and recovering from duplicates

Backup runs **inside a flow** (flow 6), not from the CLI, for a practical reason: the
Power Platform connection already has working access to the state site, whereas an
Azure CLI token often cannot get a usable SharePoint audience. Flow 6 writes the three
lists as JSON into the state site's document library — on a OneDrive-backed site, that
is the user's own Files, visible and downloadable with no tooling. It uses only the
SharePoint connector, so no second connector, consent, or DLP consideration is
introduced. Pruning happens only after a successful write, so a failed backup never
removes a good one, and the manifest records row counts so a failed export cannot pass
for a backup of an empty install.

`scripts/backup.sh` remains for configuration, flow states and settings, which the CLI
*can* read. The two are complementary, and the script says plainly when it cannot
reach the lists rather than reporting zero rows.

**Duplicates** are the failure this all guards against: remove the solution, lose the
sync map, reinstall, and the new install mirrors everything again with no way to
identify the first copy. Recovery does not depend on having a backup, because every
mirrored event carries `o365gcal-key: {iCalUId}|{startUtc}` in its description. That is
the same correlation key the sync map uses, so Google itself holds enough to group
duplicates, choose a survivor and rebuild the map.

`src/o365gcal/dedup.py` is the decision logic, with five invariants each covered by
tests:

1. An event without the marker is never touched — those are the user's own.
2. A key with a single event is never touched; deletion requires a proven duplicate.
3. Exactly one event survives every group, never zero.
4. The survivor is chosen deterministically (the mapped id if present, else the lowest
   id), so repeated runs agree. An unstable choice would delete a different copy each
   run until none were left.
5. A time slice that could not be read completely blocks all deletion.

That last one exists because `ListEvents` has no paging and returns events in
arbitrary order, so the window is walked in small slices and a suspiciously full
response is treated as unreadable rather than complete.

## How deletions and cancellations reach Google

Deletion is inferred from **absence**, not from a notification. Each reconcile reads
the whole calendar for the window, records every occurrence it saw, then finds
sync-map rows it created that Outlook no longer reports. Those are removed from Google
and the row is marked `Deleted`.

| In Outlook | On Google |
|---|---|
| Meeting deleted | Removed within ~15 minutes |
| One occurrence of a series deleted | Only that occurrence removed |
| Cancellation that removes the item | Removed, same as a deletion |
| Cancellation that leaves a `Canceled:` item | Kept, title updated to match |
| Event ages out of the sync window | **Kept** - leaving the window is not a cancellation |
| Event you created directly in Google | **Never touched** - no recorded id |

The trigger flow never deletes anything: it makes no Google calls at all. That trigger
reports `Deleted` both for genuine cancellations *and* for events merely leaving its
watch window, with no way to distinguish them, so acting on it would remove live
meetings. Every deletion therefore waits for a reconcile.

There is no cancellation *flag* to read - `isCancelled` is not on the connector's
calendar-view response - which is why the behaviour depends on what Outlook does to
the item rather than on a property.

Deletes are applied last, after creates and updates, so a run truncated by the
throttle cap has done its reversible work first. A 404 from Google counts as success:
the event being already gone is the desired end state.

### Events deleted directly in Google

An unchanged Outlook meeting decides `NoOp` before anything checks whether its Google
copy still exists, so a manual deletion on the Google side would otherwise go
unnoticed until that meeting happened to change. Verifying every event every run would
cost one Google call each against a budget of 100 per 60 seconds, so each reconcile
verifies **one rotating slice**: with `VerifySlices` at 16 and a 15-minute cadence,
every mirrored event is checked about every four hours for roughly one extra call per
run, capped by `MaxVerifyPerRun`.

A definite 404 clears the row's Google id *and its fingerprint*, which queues the
ordinary create path for the next run. Clearing only the id would leave the
fingerprint matching, so the diff would see no change and skip the recreation it just
queued. A throttled or failed read is not treated as a deletion.

## Testing

Flows cannot execute outside Power Automate, so the suite verifies the two things that
can be: the shipped artefact and the logic it encodes.

| Layer | What it proves |
|---|---|
| L1 static | Every flow parses; `runAfter` targets exist; env vars declared *and* used; no premium connector; child flow is an activated subprocess; **the packed zip actually contains all six flows** |
| L2 unit | Fingerprint stability, diff decisions, circuit breaker, throttle cap, rendering |
| Parity | The shipped WDL expressions, evaluated, equal the Python engine |
| L3 mocked | Full cycles against fake Graph and Google APIs, including 429s, 404 repair, and backlog convergence |
| L4 live | Opt-in, real calendars, never in CI |

It does **not** exercise the live Power Automate runtime. Connector parameter keys are
checked against the real connector swagger by `test_connector_contract.py`, which runs
only once `./scripts/fetch-connector-swagger.sh` has been run.

Two real defects were caught by this harness during development and are worth
recording, because both would have shipped silently:

- The Python engine collapsed whitespace runs with a regex; the flow cannot, so the
  fingerprints diverged. Caught by the parity evaluator.
- Solution Packager logged all six flows as "processed" and produced a zip containing
  none of them, because cloud-flow metadata must be declared in `Customizations.xml`
  rather than in separate `.meta.xml` files. Caught by `test_packed_zip_contains_every_flow`.
- Every Google Calendar action was authored with `item/...` parameter keys, the
  convention most Microsoft connectors follow. This connector uses `newEvent/...` on
  create and `updatedEvent/...` on update. The solution would have imported cleanly and
  failed on the first write. Caught by `test_connector_contract.py` against the live
  swagger — which is why that file is worth the trouble of fetching.
