# How synchronization actually behaves

The complete reference for what gets mirrored, when, and what happens at the limits.
Written to be checkable: every number here comes from a default in
`src/o365gcal/model.py` or a guard in the flows, and the tests named alongside each
claim are the ones that hold it true.

---

## 1. The shape of it

```
Outlook change trigger ──┐  (fast path, creates only)
                         ├──► 2 Apply Event ──► Google Calendar
Scheduled reconciler ────┘  (engine of record)         │
   every 15 minutes                                    ▼
                                          SyncMap · Log · Health
```

**The reconciler is the engine of record.** Every guarantee below is delivered by it
alone, working from a full calendar read. The Outlook change trigger is only a latency
optimisation, because Microsoft documents it as firing for whole series, emitting
spurious updates from internal Exchange churn, reporting `Deleted` for events merely
leaving its watch window, double-firing when an invitation is accepted, and lagging up
to an hour. A missed, duplicated or spurious trigger therefore costs speed and never
correctness.

That is why the trigger **only ever creates**. It never updates and never deletes.

---

## 2. Timing

| Path | Latency |
|---|---|
| Reconciler | Runs every 15 minutes, takes about a minute → **worst case ~16 minutes** |
| Change trigger | Usually a few minutes; Microsoft documents up to an hour |
| Deletions | Always the reconciler. Never faster than the next cycle. |
| Edits | Always the reconciler. |
| Google-side deletion detected | ~4 hours (see §6) |
| Change email | Immediately after the reconcile that made the change, if `notify` is on |
| Daily digest | 07:00 UTC |
| Invitation reminder | Every `rsvpdays` days at `rsvphour` UTC |

The reconciler's interval is a literal in the flow definition: Power Automate does not
allow a Recurrence interval to be driven by a setting, so changing it means editing
flow 3.

---

## 3. What each change does

| In Outlook | On Google |
|---|---|
| New meeting | Created |
| Subject, time, location, body, or show-as changed | Updated in place, same event id |
| Your RSVP changes | Updated — the description shows your response |
| Attendee added or removed | Updated |
| Meeting deleted | Removed |
| One occurrence of a series deleted | Only that occurrence removed |
| Cancellation that removes the item | Removed |
| Cancellation leaving a `Canceled:` item | Kept, title updated to match |
| Nothing changed | **Nothing happens, and no API call is made** |

That last row is what makes the whole thing affordable. Each occurrence is reduced to
a twelve-field fingerprint; if it matches the stored one, the reconciler moves on
without touching Google.

Deliberately excluded from the fingerprint: `lastModifiedDateTime` and the Graph event
id. Exchange rewrites both without the user changing anything, and including either
would rewrite the entire calendar on every run.

*Held by:* `tests/unit/test_normalize.py`, `tests/unit/test_diff.py`

---

## 4. Recurring meetings

Series are **expanded into individual Google events**, one per occurrence. The Google
Calendar connector has no recurrence parameter at all, so there is no way to create a
repeating event through it.

Consequences worth knowing:

- A weekly meeting appears as many separate entries on Google.
- Editing one occurrence in Outlook updates only that one.
- Cancelling one occurrence removes only that one.
- Each occurrence is identified by `{iCalUId}|{startUtc}` — all occurrences of a
  series share the `iCalUId`, so the start time separates them.

The correlation key uses `iCalUId` rather than the Graph event `id` on purpose: Outlook
rewrites the event id when a user accepts an invitation, which is the documented cause
of the trigger firing twice for one meeting. Keying on `iCalUId` collapses that to a
single update instead of mirroring the meeting twice.

*Held by:* `test_recurring_occurrences_are_independent`,
`test_accept_invitation_double_fire_collapses`

---

## 5. Deletion, in detail

Deletion is inferred from **absence**, not from a notification. Each reconcile:

1. Reads the full calendar view for the window
2. Records every occurrence key it saw
3. Finds sync-map rows it created that are *not* in that set
4. Removes those from Google and marks the row `Deleted`

**Never deleted:**

- Events with no recorded Google id — anything you created directly in Google is
  invisible to this path.
- Rows whose occurrence has aged out of the window. Leaving the window is not a
  cancellation; old mirrored events remain as history.

There is no cancellation *flag* to read: `isCancelled` is not on the connector's
calendar-view response. So behaviour depends on what Outlook does to the item rather
than on a property.

Deletes run **last**, after creates and updates, so a run cut short by the throttle cap
has completed its additive, reversible work first. A 404 from Google counts as success —
the event being already gone is the desired end state.

*Held by:* `test_event_absent_from_outlook_is_deleted`,
`test_row_outside_window_is_not_deleted`, `test_delete_of_already_absent_event_succeeds`

---

## 6. Changes made on the Google side

This is a **one-way mirror**. Anything you change on a mirrored Google event is
overwritten the next time that Outlook meeting changes.

Deleting a mirrored event in Google is different, because an unchanged Outlook meeting
decides "no change" before anything looks at Google. A rotating verification sweep
covers it: each reconcile checks one slice of the mirror for existence.

| Setting | Default | Effect |
|---|---|---|
| `VerifySlices` | 16 | Sweep length. 16 × 15 min = **full sweep every 4 hours** |
| `MaxVerifyPerRun` | 10 | Ceiling on existence checks per run |

A definite 404 clears the row's Google id **and its fingerprint**, which queues the
ordinary create path for the next run. Clearing only the id would leave the fingerprint
matching, so the diff would see no change and skip the recreation it just queued.

A throttled or failed read is **not** treated as a deletion. Acting on one would
recreate events that are perfectly fine whenever the connector is merely busy.

*Held by:* `tests/unit/test_verify.py`

---

## 7. API limits, and every guard against them

### The limit

| Connector | Limit |
|---|---|
| **Google Calendar** | **100 calls per 60 seconds, per connection** |
| Office 365 Outlook | 300 calls per 60 seconds |
| SharePoint | Effectively not binding here |

The Google figure is the binding constraint and **it is Microsoft's, not Google's**. It
is imposed by the Power Automate connector, per connection. Institutional Google
billing, a paid Google Cloud project, or a raised Google Calendar API quota make no
difference — the connector applies its own throttle in front of Google regardless. The
only way past it is to stop using the connector, which means a Logic App or a
containerised job against Google's REST API directly.

### Spend per reconcile

| Source | Calls |
|---|---|
| Mutations (`MaxMutationsPerRun`) | ≤ 60 |
| Verification sweep (`MaxVerifyPerRun`) | ≤ 10 |
| **Total, when nothing retries** | **≤ 70 of 100** |

Steady state is far below that: an unchanged event costs zero calls, so a calendar that
is not changing spends only the verification slice — about ten calls per run.

**The caveat that matters.** The cap counts *attempts*, not API calls. Every Google
action carries the connector's default retry policy — four retries with exponential
backoff — so one counted mutation can be up to five calls. Under sustained throttling,
60 mutations would be 300 calls, well past the limit, and the cap alone would not stop
it.

That is what guard 4 below exists for: the first rate-limit response ends the run's
mutation phase, so a throttled run spends its budget once rather than five times over.

### The guards, in the order they act

**1. Fingerprint comparison — the primary defence.**
An unchanged occurrence costs no API call at all. This is what keeps a large calendar
affordable; everything below is a backstop for when things genuinely change.
*→ `test_unchanged_event_is_a_noop_with_zero_api_calls`*

**2. Sequential loops.**
Every loop that touches Google runs with `concurrency: 1`. Concurrent branches would
race the counter and produce bursts that overrun the per-minute window even while the
total stayed legal.
*→ `test_reconcile_loops_are_sequential`*

**3. Mutation cap (`MaxMutationsPerRun`, 60).**
Once spent, remaining work is counted into `Deferred` and left for the next run.
Applied create → update → delete, so a truncated run has done its reversible work
first. The run **says so** in the log and the summary, because a truncated run that
looked complete would read as "everything is mirrored" when a third of the calendar is
missing.
*→ `test_throttle_cap_defers_overflow`, `test_truncated_run_is_never_silent`*

**4. Throttle detection — stop, do not retry into a closed window.**
When Google reports a 429 or a rate-limit error, the child flow reports it back and the
reconciler stops applying for the rest of the run. Remaining work counts as deferred
and the run summary records that it **stopped early**, so a throttled run is never
mistaken for a complete one. Without this, the retry policy would turn a 60-mutation
cap into up to 300 calls, and every subsequent event would retry into a window that is
already exhausted.
*→ `test_reconcile_stops_applying_when_throttled`, `test_child_flow_reports_throttling`*

**5. Backlog drains across runs.**
A large first sync converges over successive runs rather than failing mid-batch. A
1,000-event initial sync takes about 17 runs, roughly four hours.
*→ `test_backlog_drains_across_runs`, `test_backlog_converges_under_cap`*

**6. Verification ceiling (`MaxVerifyPerRun`, 10).**
Bounded independently of the mutation cap, so a large calendar cannot spend the whole
budget on checking.
*→ `test_ceiling_is_respected_however_large_the_calendar`*

**7. Automatic retry with exponential backoff.**
Every Google action uses the connector's default policy: four retries, backing off
exponentially. A 429 is retried rather than failing the event, and the backoff spreads
the retries out of the congested window.

**8. One failed event never stalls the batch.**
The child flow wraps its work in a Try/Catch that records the failure and returns
successfully, so a poisoned event cannot block the rest of the calendar.
*→ `test_one_bad_event_does_not_stall_the_others`, `test_throttling_is_recorded_not_fatal`*

**9. Deletion circuit breaker.**
Refuses a batch that is **both** more than `MaxDeletePercent` (25%) of the mirror
**and** more than `MinDeletesBeforeBreaker` (5) events. A throttled Outlook read, an
expired token and a genuine mass cancellation are indistinguishable from the
reconciler's side, and deletion is the only irreversible operation here. It is
all-or-nothing: a partial delete batch is both destructive and inconsistent. Creates
and updates still proceed, being additive and safe.

The rule is conjunctive on purpose. A percentage test alone is useless on a sparse
calendar — cancelling one of three meetings is 33% and would be refused forever,
training the user to ignore the alert.
*→ `tests/unit/test_guards.py`*

**10. Truncated-read abort.**
A sync-map read that returns a full page (5,000 rows) may be short, and SharePoint
sends no continuation token. Continuing would treat unseen rows as never-mirrored and
create a second copy of each. The run logs an error, alerts, and **terminates as
Failed** so the watchdog escalates — a green run that skipped its work would be worse
than a red one.
*→ `test_reconcile_aborts_on_a_truncated_map_read`*

**11. List growth is bounded.**
The watchdog prunes settled deletions after `DeletedRowRetentionDays` (30) and rows
whose occurrence is older than the window plus `MapRetentionDays` (400), and warns at
`ListSizeWarnAt` (4,000) rows — before reads begin truncating.
*→ `tests/unit/test_limits.py`*

### If you do hit the limit anyway

The symptoms, in order of what you would notice:

1. A run summary saying **STOPPED EARLY: Google reported rate limiting**.
2. Log rows with `Level = Error` mentioning a 429 or throttling.
3. `Deferred` counts in the run summary that never reach zero.
4. Events appearing on Google slowly but steadily, over hours rather than minutes.

A single such run is not a problem — it stops, defers, and the next run picks up. It
matters if it repeats, which means the sustained rate of change exceeds what one
connection can carry.

What to do:

```zsh
./scripts/configure.sh                 # check current settings
# then, in the maker portal or by editing flow 3:
#   lower MaxMutationsPerRun   - smaller bites, more runs
#   raise VerifySlices         - verify less often
#   reduce ahead               - a smaller window means less to track
```

Reducing the window is usually the most effective: `ahead 60` instead of 120 roughly
halves the tracked event count and therefore the steady-state verification cost.

---

## 8. What is not mirrored

| | Why |
|---|---|
| Recurrence rules | The connector cannot create repeating events |
| Attachments | No connector support |
| Real Google attendees | Would email an invitation to every Outlook attendee from your own account, and the connector rejects an empty attendee list |
| Meeting-room resources | Not represented on the Google side |
| Full HTML body | The connector returns raw HTML with no plain-text alternative and a flow cannot strip markup; the description links back to Outlook instead |
| Teams join links beyond the body text | Same reason |
| Anything outside the window | `back` 7 days, `ahead` 120 days by default |
| Private event details, if `private` is on | Deliberate: mirrors as an opaque "Busy" block |

---

## 9. Failure behaviour

| Failure | What happens |
|---|---|
| One event fails to write | Logged, `SyncState = Error`, batch continues |
| Google returns 429 | Retried with backoff, four attempts |
| Google event missing on update | Recreated, map row repaired |
| Google event already gone on delete | Treated as success |
| Outlook read fails | Run fails; watchdog escalates after 3 consecutive |
| Sync-map read truncated | Run aborts deliberately rather than duplicate |
| Mass deletion detected | Refused entirely, alert sent |
| Google consent expires | Watchdog probes both connections hourly and alerts |
| A flow stops running | Heartbeat goes stale, watchdog alerts |
| The watchdog itself dies | Power Platform's own failure notifications — see `ADMIN.md` |

---

## 10. Verifying any of this yourself

```zsh
./scripts/status.sh                          # is everything running
./scripts/configure.sh                       # what is it set to
./scripts/run-flow.sh --runs 3               # recent reconciles
./scripts/run-flow.sh --detail 3             # per-action detail of the newest run
./scripts/run-flow.sh --outputs Guard_Outlook_Read 3   # events seen
./scripts/run-flow.sh --outputs Filter_Active_Rows 3   # events tracked
```

The `O365GCalLog` list on your state site records every decision, including the
no-ops — that is the fullest account of what the mirror has done and why.

To prove idempotency: note the newest `2 Apply Event` run time, force a reconcile, and
check it has not advanced. A count is not proof — a run listing capped at its own page
size will happily report equal numbers.
