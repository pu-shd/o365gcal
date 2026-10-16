# Troubleshooting

First look at the `O365GCalLog` list on your SharePoint site, newest first: it records
every decision. Then:

```zsh
./scripts/status.sh
./scripts/run-flow.sh --runs 3         # recent reconciles
./scripts/run-flow.sh --failed 2       # which step failed, and why
```

## Install or import failed

`install.sh` prints the import's last lines and stops. A data loss prevention error
means the connectors cannot be combined: see [ADMIN.md](ADMIN.md). Otherwise the
manual steps in [INSTALL.md](INSTALL.md) show which part failed.

## Nothing appears on Google

- **3 Reconcile** off, or `dryrun` on (`./scripts/configure.sh`).
- Log empty: the flow is not running; check `StateSiteUrl` and the connections.
- Log says `NoOp` but Google is empty: clear `O365GCalSyncMap`; the next run rebuilds.

## An Outlook change did not reach Google

Wait 15 minutes. A body edit that changes neither the length nor the first 256
characters of the body is not detected on its own.

## Duplicates on Google

**One meeting re-created every 15 minutes** means its sync-map row cannot be written:

```zsh
./scripts/run-flow.sh --outputs Write_Map_Row 2   # SharePoint's message
./scripts/run-flow.sh --sizes Write_Map_Row 2     # which field is too long
```

Before 1.3.1 this happened to invitations with very long ids. To clean up, run
`make dedup` to preview, then `make dedup-apply`, which opens flow 7's page: switch
**Apply deletions** to Yes there (a script cannot set it). If the cleanup is refused as
too large, `./scripts/configure.sh maxdelete 50` for that run, then back to 25.

Recurring meetings appearing as separate events are expected. Mirrored events can be
found by searching Google Calendar for `o365gcal-key`.

## "Mass deletion refused" email

A batch over 25% of the mirror and over 5 events was refused. If Outlook returned
unexpectedly few events, the read failed; the next run retries. If the cancellations
are real, `./scripts/configure.sh maxdelete 100` for one run, then back to 25.

## Only part of the calendar synced

Log rows saying `deferred to next run`: at most 60 writes per run. A large first sync
takes several runs.

## An event deleted on Google did not come back

Google-side deletions are found by a rotating check, within about four hours.

## An event deleted in Outlook is still on Google

Wait 15 minutes. Events outside the sync window (`window`) are kept on purpose.

## "Your Google calendar is going stale"

If flow 3 is off, switch it on. If its newest run has been **Running** for more than a
few minutes, Power Automate has stalled it:

```zsh
./scripts/unstick.sh            # list stuck runs
./scripts/unstick.sh --cancel   # clear them; the next run redoes the work
```

## Watchdog says a connection is failing

Reauthorise it under **make.powerautomate.com → Connections**. Google revokes tokens
after a password change or long inactivity; nothing is lost.

## No emails at all

Check **5 Watchdog** is on, `AlertEmail` is right, Junk, and Power Platform's own
failure emails ([ADMIN.md](ADMIN.md)).

## Reminder emails

`./scripts/configure.sh rsvpdays N` (0 stops them), `rsvphour H` (UTC).

## Start over

1. `./scripts/configure.sh dryrun on`; `./scripts/enable-flows.sh --off 1 3`.
2. Delete mirrored Google events (search `o365gcal-key`) and all `O365GCalSyncMap` rows.
3. `./scripts/configure.sh dryrun off`; `./scripts/enable-flows.sh 3 1`.
