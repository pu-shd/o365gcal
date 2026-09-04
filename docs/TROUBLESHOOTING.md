# Troubleshooting

**Start here:** the `O365GCalLog` list on your SharePoint site records every decision
the automation made, with a timestamp, the flow name and a message. Sort by
`Timestamp` descending. Most questions are answered in one look.

---

## Nothing is appearing on Google

**Is the flow on?** Solution → check **3 Reconcile** is switched on. Flows ship off on
purpose so nothing writes to a real calendar before you have configured it.

**Is `DryRun` still on?** It logs `DRY RUN: would Create ...` and writes nothing. Set
it to off.

**Is the log empty too?** Then the flow is not running at all. Open its run history for
an error. The usual causes are a `StateSiteUrl` that is wrong or that you cannot write
to, or a connection needing reauthorisation.

**Log says `NoOp` for everything?** The fingerprints match, so the automation believes
Google is already up to date. If Google is in fact empty, the map is stale — someone
deleted the Google events directly. Fix: clear the `O365GCalSyncMap` list and let the
next reconcile rebuild it.

## An event changed in Outlook but not on Google

Wait one reconcile cycle (15 minutes by default). If it still has not moved, find the
event's row in `O365GCalSyncMap` and compare `ContentFingerprint` against what you
expect. If it is unchanged, the edit was in a field the fingerprint does not track.

**Known blind spot:** a body edit that changes neither the total length nor the first
512 characters of Outlook's preview text will not be detected on its own. Change
anything else about the event and it syncs.

## "Mass deletion refused" email

Working as designed. The reconciler wanted to delete more than 25% of your mirrored
events at once and refused, because a failed Outlook read looks exactly like a mass
cancellation and deleting is irreversible.

The email says how many events Outlook returned. **If that number is unexpectedly
low**, the read failed — check the Office 365 Outlook connection and let the next run
handle it. Nothing was removed from Google.

**If the cancellations are genuine** (you really did clear your calendar), raise
`MaxDeletePercent` to `100`, let one reconcile run, then set it back to `25`.

## Duplicate events on Google

Should not happen; all writes go through one child flow keyed on `iCalUId`. If it does:

1. Check whether the duplicates are actually a **recurring meeting**. Those are
   expected to appear as separate events — the Google connector cannot create
   repeating events.
2. Otherwise the map lost track. Delete the duplicates in Google, delete their rows
   from `O365GCalSyncMap`, and let the next reconcile rebuild.

To find events the automation created, search Google Calendar for `o365gcal-key` —
every mirrored event carries that marker in its description.

## Only part of my calendar synced

Look for a log row saying `deferred to next run`. The throttle cap holds writes to 60
per run to stay inside Google's limit of 100 calls per 60 seconds. A large first sync
drains over several runs; it is not stuck. Raise `MaxMutationsPerRun` cautiously if you
want it faster — going over the limit fails the run mid-batch.

## I deleted an event in Google and it did not come back

It will, but not immediately. Each reconcile verifies a rotating slice of the mirror
rather than every event, so a manual deletion on the Google side is noticed within
about four hours by default and recreated on the run after that. Lower `VerifySlices`
to check more often at the cost of more API calls.

## An event I deleted in Outlook is still on Google

Give it one reconcile cycle (15 minutes). If it persists, check whether its start date
is outside the sync window - `back`/`ahead` in `configure.sh`. Events that have aged
out of the window are deliberately left alone, because leaving the window is not a
cancellation.

If a whole batch failed to disappear, look in the log list for a circuit-breaker
entry: a deletion batch that is both a large share of the mirror and more than five
events is refused outright, because a partial Outlook read looks identical to a mass
cancellation.

## Too many, too few, or badly formatted reminder emails

```zsh
./scripts/configure.sh rsvpdays 1     # remind me daily
./scripts/configure.sh rsvpdays 7     # weekly
./scripts/configure.sh rsvpdays 0     # stop them
./scripts/configure.sh rsvphour 8     # hour of day, UTC
```

Reminders group by meeting, not by occurrence, so a recurring series is one entry with
a count. Only invitations starting within `RsvpHorizonDays` (60 by default) are
listed.

## Watchdog says a connection is failing

Go to **make.powerautomate.com → Connections** and reauthorise the one named.

Google connections break most often: Google revokes refresh tokens on password change
and after long inactivity. Nothing is lost — the next reconcile catches up.

## An hourly health report about a supporting flow

The report says the reconciler is healthy and lists one supporting flow as past its
threshold, and it arrives again every hour. Before version 1.1 this was usually the
watchdog being right about the wrong thing: the flow was running fine, but was not
writing its heartbeat on every run.

* **8 Invitation Reminder** stamped its heartbeat inside the send-window condition, so
  the row was written for one hour every `RsvpReminderDays` and was stale for the rest.
* **6 Backup State** never stamped a heartbeat at all. Its row is seeded at install, so
  it looked healthy for one day and then reported stale forever.

Both are fixed by upgrading: `make build && ./scripts/update.sh`. The alerts stop after
the next run of each flow — within the hour for flow 8, at 02:00 UTC for flow 6.

If you are already on 1.1 or later, the flow really is failing: open its run history in
**make.powerautomate.com → Solutions → O365GCal**.

## I stopped getting any emails at all

That is the case the watchdog cannot cover, because it may itself be off. Check:

1. **5 Watchdog** is switched on.
2. `AlertEmail` is set correctly.
3. The emails are not in Junk. Add yourself to safe senders.
4. Power Platform's own flow-failure notifications are enabled (see `ADMIN.md`).

## Private events are showing details I wanted hidden

Turn on `HidePrivateEventDetails`. Events marked Private or Confidential in Outlook then
appear on Google as `Busy` with no subject, location, body or attendees.

Changing this rewrites every mirrored event on the next reconcile, by design.

## Colleagues got Google invitations from me

`CopyAttendeesAsGoogleAttendees` is on. Turn it off. With it on, Google emails an
invitation from your Google account to every attendee of every mirrored meeting.

Turning it off stops further invitations. Ones already sent cannot be recalled; the
mirrored events are updated to drop the attendee list on the next reconcile.

## Starting over

1. Set `DryRun` on and turn off flows 1, 3, 4, 5.
2. Delete the mirrored events in Google (search `o365gcal-key` to find them).
3. Delete all rows from `O365GCalSyncMap`. Leave `O365GCalLog` for history.
4. Turn `DryRun` off and turn the flows back on.

The next reconcile rebuilds from scratch.
