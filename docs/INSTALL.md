# Installing the Outlook → Google calendar mirror

This puts your Outlook meetings onto a Google calendar automatically, so you can live
in Google Calendar and still see everything that lands in Outlook. It also emails you
a daily summary of what changed, tells you which invitations you still owe a reply to,
and warns you if the automation itself stops working.

It is **one-way**. Outlook is the source of truth; anything you change on the Google
copy is overwritten on the next sync.

Budget about 20 minutes.

---

## Before you start

You need:

- A Microsoft 365 account with Power Automate (the seeded licence in M365 is enough —
  no premium licence is required).
- A Google account you can sign into.
- A SharePoint site you can create lists in. A team site works; so does a personal
  site. The automation keeps its bookkeeping there.

**One thing to check first.** Some organisations block Microsoft and Google connectors
from being used in the same flow. If yours does, nothing here will work and no
workaround exists on this side. Test it in five minutes:

1. Go to [make.powerautomate.com](https://make.powerautomate.com) → **+ Create** →
   **Instant cloud flow** → **Manually trigger a flow**.
2. Add an action: **Office 365 Outlook → Get calendars (V2)**.
3. Add an action: **Google Calendar → List calendars**.
4. Add an action: **SharePoint → Send an HTTP request to SharePoint**.
5. Press **Save**.

If it saves, you are fine — delete the test flow and carry on. If you get an error
mentioning a **data loss prevention policy**, stop here and send `docs/ADMIN.md` to
whoever administers Power Platform for your organisation.

---

## The short version

```zsh
pac auth create          # sign in, opens a browser
./scripts/install.sh
```

`install.sh` does everything else: it works out where to keep its bookkeeping, checks
your accounts are linked, installs, switches everything on, lets you pick the Google
calendar **from a numbered list**, and then does a practice run that writes nothing so
you can see the plan before approving it.

You never need to open Power Automate or SharePoint, find a site address, or paste a
calendar identifier.

If a tool is missing it tells you the one command to install it and stops. If your
accounts are not linked yet it gives you the single page to visit — that part has to
happen in a browser once, because it involves signing in to Google.

## Changing settings afterwards

```zsh
./scripts/configure.sh                 # see everything, in plain words
./scripts/configure.sh notify on       # email me on every change
./scripts/configure.sh dryrun on       # back to practice mode
./scripts/configure.sh private on      # hide details of private events
./scripts/configure.sh calendar        # pick a different Google calendar
./scripts/configure.sh window 7 120    # days back, days ahead
```

Changes take effect on the next run. No reinstall, no portal.

| Setting | Default | What it does |
|---|---|---|
| `dryrun` | off | Practice mode: logs what it would do, writes nothing |
| `notify` | off | Email whenever an event is added, changed or removed |
| `private` | off | Private events appear as just "Busy" |
| `calendar` | — | Which Google calendar receives the events |
| `source` | `Calendar` | Which Outlook calendar is mirrored |
| `email` | — | Where digests and warnings go |
| `back` / `ahead` | 7 / 120 | How far back and forward to mirror |
| `prefix` | `none` | Text in front of mirrored titles |

Everything else has a sensible default and is listed by `./scripts/configure.sh`.

## Doing it by hand instead

If you would rather not use the installer, or it fails partway:

1. `./scripts/build.sh` — produces `dist/O365GCal_managed.zip`
2. Import that zip at [make.powerautomate.com](https://make.powerautomate.com) →
   **Solutions** → **Import solution**
3. Supply the three connections when asked. Pick **Office 365 Outlook**, not
   **Outlook.com** — they look alike in the picker and are different services.
4. Set at least `StateSiteUrl` and `AlertEmail`
5. Turn the flows on: `./scripts/enable-flows.sh`
6. Run Setup once: `./scripts/run-flow.sh 0`

## Living with it

| Want to | Run |
|---|---|
| See whether it is installed and healthy | `./scripts/status.sh` |
| Upgrade to a newer version | `./scripts/update.sh` |
| Check for an upgrade without installing it | `./scripts/update.sh --check` |
| Remove it | `./scripts/teardown.sh` |

`update.sh` keeps your settings, your connections and all bookkeeping — only the flows
are replaced. It refuses to run if the solution is not installed and points you at
`bootstrap.sh` instead.

`teardown.sh` removes things in three separate stages, each confirmed on its own, and
the default at every prompt is to keep things. It deliberately leaves the SharePoint
lists alone: the sync map is what stops a future reinstall from creating a second copy
of every event.

> **If you want the mirrored Google events gone too, do that before removing the
> solution.** Once it is uninstalled, the record of which Google events belong to it is
> gone. `teardown.sh` explains both ways at its first prompt.

## If something looks wrong

Start with `docs/TROUBLESHOOTING.md`. The `O365GCalLog` list on your SharePoint site
records every decision the automation made and is usually enough to explain anything.
