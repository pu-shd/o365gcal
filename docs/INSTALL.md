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

## Step 1 — Import the solution

### The easy way

```zsh
pac auth create --environment <your environment URL>
./scripts/bootstrap.sh
```

`bootstrap.sh` walks the whole thing: it checks your tooling, confirms your three
connections are healthy, asks the three questions from step 3, installs with those
answers already applied, and tells you what to do next. It changes nothing until you
confirm, and it is safe to re-run — if something is already in place it says so and
resumes rather than starting over.

It also saves your answers to `o365gcal.settings.json`, so a reinstall later is just:

```zsh
./scripts/bootstrap.sh --settings o365gcal.settings.json
```

**If bootstrap did the work for you, skip to step 4.**

### Through the browser instead

1. [make.powerautomate.com](https://make.powerautomate.com) → **Solutions** →
   **Import solution**.
2. Choose `dist/O365GCal_managed.zip` → **Next**.

## Step 2 — Connect your accounts

The importer asks for three connections. Create or pick one for each:

| Connection | Sign in as |
|---|---|
| Office 365 Outlook | your work account |
| Google Calendar | the Google account that owns the target calendar |
| SharePoint | your work account |

> **Careful:** pick **Office 365 Outlook**, not **Outlook.com**. They look similar in
> the picker and are different services. Outlook.com is the personal one and will not
> work here.

## Step 3 — Fill in the settings

Open the solution → **Environment variables**. You must set three:

| Setting | What to put |
|---|---|
| `StateSiteUrl` | The SharePoint site for bookkeeping, e.g. `https://contoso.sharepoint.com/sites/MyCalendarSync` |
| `AlertEmail` | Where digests and warnings go. Usually your own address. |
| `GoogleCalendarId` | `primary` for your main Google calendar, or a specific ID (step 4 shows you the options) |

Leave everything else alone for now. In particular leave **`DryRun` switched on** — it
lets the first run show you what it *would* do without touching anything.

The rest, for later:

| Setting | Default | What it does |
|---|---|---|
| `OutlookCalendarId` | `Calendar` | Which Outlook calendar to mirror |
| `WindowPastDays` / `WindowFutureDays` | 7 / 120 | How far back and ahead to mirror |
| `TitlePrefix` | *(empty)* | Prefix on mirrored titles, e.g. `[Outlook] ` |
| `PrivacyMode` | `full` | `busy-only` hides subject, location and attendees for private events |
| `CopyAttendeesAsGoogleAttendees` | off | **Leave off.** See the warning below. |
| `MaxMutationsPerRun` | 60 | Google write cap per run |
| `MaxDeletePercent` | 25 | Refuses suspiciously large deletion batches |
| `HeartbeatStaleMinutes` | 90 | How long silence lasts before you get warned |

> ### Why `CopyAttendeesAsGoogleAttendees` is off
> If you turn it on, Google emails a calendar invitation **from your Google account to
> every attendee of every mirrored meeting** — your colleagues get a duplicate invite
> for each one. Instead, attendees and your RSVP status are written into the Google
> event's description, so you can see exactly who is invited without anyone being
> emailed.

## Step 4 — Run setup once

In the solution, open **O365GCal 0 Setup and Provision** and press **Run**.

It creates three lists on your SharePoint site and emails you a summary that includes
**the list of your Outlook and Google calendar IDs**. Check the IDs in that email
against what you entered in step 3 and correct them if needed.

## Step 5 — Dry run

Turn on **O365GCal 3 Reconcile** and let it run once (or press **Run** yourself).

With `DryRun` still on, nothing is written to Google. Open the `O365GCalLog` list on
your SharePoint site and read the rows: each says what *would* have happened. If the
event count looks about right, you are ready.

## Step 6 — Go live

1. Set `DryRun` to **off**.
2. Turn on the remaining flows:
   - **1 Sync Outlook Trigger** — picks up changes quickly
   - **3 Reconcile** — the one that guarantees correctness (every 30 min)
   - **4 Digest and Change Alerts** — daily email
   - **5 Watchdog** — hourly health check

Leave **2 Apply Event** on; the others call it.

The first reconcile fills in your calendar. If you have a lot of meetings it spreads
the work over several runs to stay inside Google's rate limit — the log tells you when
that happens.

---

## What you should see

- Outlook meetings appear on Google within about 30 minutes.
- Each mirrored event's description shows the organiser, the attendees, whether you
  have replied, and a link back to the Outlook event.
- A daily email listing what was added, changed and removed, plus **invitations you
  still owe a reply to** — the thing Google genuinely cannot tell you.
- An email if the automation breaks.

## Known limits

- **Recurring meetings become individual events on Google.** The Google Calendar
  connector cannot create repeating events at all, so a weekly meeting appears as many
  separate entries. They stay correct; they just do not group.
- **One-way only.** Edits made on Google are overwritten.
- **Up to 30 minutes behind** in the worst case.
- **Meetings outside the window** (default 7 days back, 120 ahead) appear as they come
  into range.
- **Bodies are shortened.** The description shows Outlook's preview text plus a link;
  attachments, embedded images and long bodies are not copied.
- **Attachments, room resources and private notes are not mirrored.**

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
