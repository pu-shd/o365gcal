# Synchronization

What is mirrored, when, and the limits. Defaults come from `src/o365gcal/model.py`;
the tests named after each claim hold it true.

## Timing

| | |
|---|---|
| Reconcile (engine of record) | Every 15 minutes; worst case ~16 minutes |
| Outlook change trigger | Creates only, usually within minutes |
| Edits and deletions | Next reconcile |
| Event deleted on Google | Found by a rotating check, every 4 hours (`VerifySlices` 16 × 15 min) |
| Daily digest | 07:00 UTC |

## What each Outlook change does

| Outlook | Google |
|---|---|
| New meeting | Created |
| Subject, time, location, body, show-as, attendees or your RSVP changed | Updated in place |
| Deleted, or cancelled so the item disappears | Removed |
| Cancelled, leaving a `Canceled:` item | Kept, title updated |
| One occurrence of a series changed or deleted | Only that occurrence |
| Moved outside the window (7 days back, 120 ahead) | Kept |
| Nothing | Nothing, and no API call |

Each occurrence is reduced to a fingerprint; if it matches the stored one, Google is
not called (`test_unchanged_event_is_a_noop_with_zero_api_calls`). Series are expanded
into one Google event per occurrence, keyed `{iCalUId}|{startUtc}`
(`test_recurring_occurrences_are_independent`). Keys over 255 characters keep their
last 255 (`test_correlation_key_fits_a_sharepoint_text_column`); shorter keys are
unchanged (`test_correlation_key_is_unchanged_when_it_already_fits`). Keying on
`iCalUId` rather than the Graph id means accepting an invitation updates instead of
duplicating (`test_accept_invitation_double_fire_collapses`).

Deletion is inferred from absence in a full read
(`test_event_absent_from_outlook_is_deleted`); rows outside the window are left alone
(`test_row_outside_window_is_not_deleted`); a 404 on delete counts as done
(`test_delete_of_already_absent_event_succeeds`). Only events whose Google id was
recorded are ever deleted.

This is a one-way mirror: edits made on Google are overwritten when the Outlook
meeting next changes.

## API limits

The Google Calendar connector allows **100 calls per 60 seconds**, per connection.
That limit is Microsoft's, not Google's: a paid Google project or a raised Google quota
does not change it.

| Per reconcile | Calls |
|---|---|
| Writes (`MaxMutationsPerRun`) | ≤ 60 |
| Existence checks (`MaxVerifyPerRun`) | ≤ 10 |
| Total, with no retries | ≤ 70 of 100 |

The cap counts *attempts*, not API calls. Each Google action retries up to four
times, so 60 capped writes could become 300 calls under throttling. That is why the
first rate-limit response ends the run's writes: the summary records that it stopped
early and the rest is deferred
(`test_reconcile_stops_applying_when_throttled`, `test_child_flow_reports_throttling`).

Other guards:

- Google loops run one at a time (`test_reconcile_loops_are_sequential`).
- Overflow past the cap is deferred and reported
  (`test_throttle_cap_defers_overflow`, `test_truncated_run_is_never_silent`); a
  1,000-event first sync drains in about 17 runs
  (`test_backlog_drains_across_runs`, `test_backlog_converges_under_cap`).
- The existence check is bounded however large the calendar
  (`test_ceiling_is_respected_however_large_the_calendar`).
- One failing event never stops the rest
  (`test_one_bad_event_does_not_stall_the_others`, `test_throttling_is_recorded_not_fatal`).
- Deletion circuit breaker: a batch over `MaxDeletePercent` (25%) of the mirror **and**
  over `MinDeletesBeforeBreaker` (5) events is refused entirely
  (`tests/unit/test_guards.py`).
- A sync-map read of a full 5,000-row page aborts the run rather than risk duplicates
  (`test_reconcile_aborts_on_a_truncated_map_read`).
- The watchdog prunes deleted rows after 30 days and rows older than the window plus
  400 days, and warns at 4,000 rows (`tests/unit/test_limits.py`).

If runs keep saying **STOPPED EARLY** or deferring, shrink the window
(`./scripts/configure.sh window 7 60`) or lower `MaxMutationsPerRun`.

## Not mirrored

Recurrence rules (the connector cannot create them), attachments, room resources, real
Google attendees (they would be emailed invitations; attendees are listed in the
description instead), the full HTML body (the description links back to Outlook), and
anything outside the window. With `private` on, private events appear as "Busy".

## Checking it yourself

```zsh
./scripts/run-flow.sh --runs 3                          # recent reconciles
./scripts/run-flow.sh --outputs Guard_Outlook_Read 3    # events read
```

The `O365GCalLog` list records every decision, including no-ops.
