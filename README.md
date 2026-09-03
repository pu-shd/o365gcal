# O365GCal — one-way Outlook → Google Calendar mirror

A Power Platform solution that mirrors an Outlook calendar onto a Google calendar so
that **someone who never opens Outlook still sees every meeting** — with a daily
summary of changes, visibility of invitations they still owe a reply to, and an alert
if the automation itself breaks.

Built with **Standard connectors only**. No Power Automate Premium licence is required
by anyone who installs it.

## For someone who just wants to use it

```zsh
pac auth create        # sign in, opens a browser
./scripts/install.sh
```

That is the whole installation. It works out where to keep its bookkeeping, checks
your accounts are linked, installs, switches everything on, lets you pick the Google
calendar **from a numbered list**, then does a practice run that writes nothing so you
can see the plan before approving it. No need to open Power Automate or SharePoint, or
to find a site address or paste a calendar identifier.

Afterwards, settings are plain words rather than a portal:

```zsh
./scripts/configure.sh              # see everything and what it does
./scripts/configure.sh notify on    # email me on every change
./scripts/configure.sh dryrun on    # back to practice mode
./scripts/configure.sh calendar     # pick a different Google calendar
```

Full walkthrough in **[docs/INSTALL.md](docs/INSTALL.md)**.

| | |
|---|---|
| `./scripts/configure.sh` | See and change every setting, in plain language |
| `./scripts/status.sh` | What is installed, is it healthy, is it running |
| `./scripts/update.sh` | Graceful upgrade, preserving settings and bookkeeping |
| `./scripts/teardown.sh` | Staged removal, each destructive step confirmed separately |
| `./scripts/show-state.sh` | Whether the state lists exist and how many rows they hold |
| `./scripts/run-flow.sh 6` | Back up the state lists to OneDrive on demand |
| `./scripts/run-flow.sh 7` | Report duplicate mirrored events (dry run) |
| `./scripts/run-flow.sh --apply 7` | Delete duplicates and rebuild the sync map |
| `./scripts/backup.sh` | Snapshot configuration and flow states locally |
| `./scripts/restore.sh <dir>` | Rebuild an install from a backup |
| `./scripts/preflight.sh` | Check the DLP gate before you start |

### Nothing here deletes a calendar event

`update.sh`, `teardown.sh`, `backup.sh` and `restore.sh` never create, change or
remove an event on any calendar, and a test enforces it. An upgrade or a teardown
leaves everything already mirrored to Google exactly as it is — it simply stops being
kept up to date. Outlook is only ever read.

Teardown deliberately leaves the SharePoint lists in place. The sync map is the only
record of which Google events the automation created; without it a reinstall cannot
tell its own events from yours, mirrors the calendar a second time, and can never
clean up. If that has already happened, **flow 7 Dedup and Repair** recovers from it
using the `o365gcal-key` marker every mirrored event carries — no backup required.

> **Check this first.** Some organisations block Microsoft and Google connectors from
> being used in the same flow. `./scripts/preflight.sh` explains the five-minute test.
> If your tenant blocks it, nothing here can work and no workaround exists on this side.

## For someone maintaining it

```zsh
make test          # full offline suite locally
make test-docker   # the same suite in the container
make build         # pack managed + unmanaged zips into dist/
make swagger       # fetch connector swagger, enabling contract tests
make export        # pull maker-portal edits back into solution/src
```

`solution/src/` is the source of truth. Edits made in the maker portal are pulled back
with `make export`; never hand-edit the zips.

## Behaviour at a glance

| | |
|---|---|
| Sync direction | One-way, Outlook → Google |
| Latency | ~15 minutes worst case, usually less |
| Recurring meetings | Expanded to individual Google events |
| Deletions | Detected by absence on a full calendar read |
| Google-side deletions | Detected within ~4 hours by a rotating sweep |
| Unchanged events | Cost zero API calls |
| Worst-case API spend | 70 of the connector's 100 calls per 60 seconds |

Full detail, including every limit guard, in
**[docs/SYNCHRONIZATION.md](docs/SYNCHRONIZATION.md)**.

## How it works, briefly

Six flows. The **scheduled reconciler is the engine of record** — it diffs a full
Outlook calendar read against a SharePoint sync map and applies the difference. The
Outlook change trigger is only a latency optimisation, because Microsoft documents it
as dropping events, firing spuriously and lagging up to an hour. A missed trigger
therefore costs speed, never correctness.

Deletion is the only irreversible operation and is inferred from absence, so a
**circuit breaker** refuses any batch that is both a large share of the mirror and
large in absolute terms — a failed Outlook read looks identical to a mass cancellation.

Attendees are rendered as **text in the event description**, not attached as real
Google attendees, because real attendees make Google email an invitation to every
Outlook attendee from the user's own account.

Full reasoning, including the platform constraints that forced these choices, in
**[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## Repository layout

```
solution/src/     the solution (source of truth)
src/o365gcal/     reference sync engine - the executable specification
tests/            unit, expression-parity, mocked-integration and static validation
scripts/          zsh lifecycle scripts
docs/             INSTALL, ADMIN, ARCHITECTURE, TROUBLESHOOTING
```

## Documentation

- **[SYNCHRONIZATION.md](docs/SYNCHRONIZATION.md)** — what gets mirrored, when, and
  every guard against the API limits. The behavioural reference.
- **[INSTALL.md](docs/INSTALL.md)** — install and configure
- **[ADMIN.md](docs/ADMIN.md)** — DLP, rollout, licensing, second-layer alerting
- **[ARCHITECTURE.md](docs/ARCHITECTURE.md)** — why it is shaped this way
- **[TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** — when something looks wrong

## Known limits

Recurring meetings become individual Google events (the connector cannot create
repeating events at all). One-way only. Changes appear within about 15 minutes, and
usually sooner. Event bodies are shortened plus a link back to Outlook. Attachments,
room resources and private notes are not mirrored.
