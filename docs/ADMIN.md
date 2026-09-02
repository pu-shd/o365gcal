# Administrator notes

For whoever runs Power Platform, and for whoever is rolling this out to a group.

## The one thing that can block this entirely

**DLP policy.** Data loss prevention sorts connectors into Business / Non-Business /
Blocked groups, and a single flow may not span groups. This solution needs three
connectors in one flow:

| Connector | API name | Class |
|---|---|---|
| Office 365 Outlook | `shared_office365` | Standard |
| Google Calendar | `shared_googlecalendar` | Standard |
| SharePoint | `shared_sharepointonline` | Standard |

Universities and enterprises very commonly place Google connectors in **Non-Business**
and Microsoft connectors in **Business**, which blocks this design at flow save time.
There is no solution-side workaround — it is a platform-level policy decision.

**Note that healthy connections do not prove the pairing is allowed.** A user can hold
a working Google Calendar connection and still be unable to combine it with Outlook.

### Settling it in five minutes

Have any prospective user do this in the target environment:

1. **+ Create → Instant cloud flow → Manually trigger a flow**
2. Add **Office 365 Outlook → Get calendars (V2)**
3. Add **Google Calendar → List calendars**
4. Add **SharePoint → Send an HTTP request to SharePoint**
5. **Save**

A clean save means the design is viable. An error naming a data loss prevention policy
means it is not. Delete the test flow afterwards.

`./scripts/preflight.sh` prints these steps and checks connection health first.

### If it is blocked

Options, roughly in order of effort:

1. **Move Google Calendar into the same data group as Office 365** for a scoped
   environment. Note this permits *any* maker in that environment to move data between
   Microsoft and Google, which is precisely what the policy exists to prevent — it is a
   real decision, not a formality.
2. **Create a dedicated environment** with its own DLP policy, and grant access only to
   the people who need the mirror. This is the usual compromise: the exception is
   contained rather than tenant-wide.
3. **Do not use Power Platform.** Run the equivalent as a Logic App or a small
   containerised job against Microsoft Graph and the Google Calendar REST API. That
   moves it outside Power Platform governance, which may be the actual objection.

## Licensing

Nothing here requires Power Automate Premium. All three connectors are Standard, and
the design deliberately avoids the premium traps:

- **Dataverse** (premium) → SharePoint lists instead.
- **The generic HTTP action** (premium) → `Send an HTTP request to SharePoint`, which is
  Standard, for all SharePoint REST work.
- **Power Automate Management** (premium) → a heartbeat list plus a watchdog flow.

`tests/validate/test_solution_static.py::test_no_premium_connectors` fails the build if
a premium connector is ever introduced, so the Standard-only property is mechanically
enforced rather than merely intended.

Solution-aware flows need a Dataverse database in the environment. Every modern
environment has one; there is no additional licence implication for using child flows.

## Rolling it out to a group

Each person installs their own copy and connects their own accounts. There are no
shared secrets and no service accounts.

Hand out three things:

1. `dist/O365GCal_managed.zip`
2. `docs/INSTALL.md`
3. The `scripts/` directory (for `bootstrap.sh`)

For a larger rollout, do one install yourself, keep the generated
`o365gcal.settings.json`, blank the personal values, and distribute it as a template.
Users then run `./scripts/bootstrap.sh --settings <file>` for an install with your
defaults already applied.

Each user needs a SharePoint site they can create lists in. A shared site works — the
lists are per-user by convention, so give each person their own site or their own list
names via the `StateListName` / `LogListName` / `HealthListName` settings.

## Second-layer alerting (do this)

The solution watches itself: each flow records a heartbeat and flow 5 alerts when one
goes stale or when a connection stops working. That covers the failures an in-flow error
handler cannot see — a switched-off flow, a suspended flow, an expired Google consent.

**It cannot cover its own death.** If flow 5 is switched off or fails, nothing reports
it. Enable Power Platform's built-in flow failure notifications as an independent
layer:

- Users receive Microsoft's automated "your flow is failing" email by default. Confirm
  it is not being filtered.
- Power Platform **turns off flows that fail continuously for 90 days**, and emails
  before doing so. That email is the last warning before a silent stop.

For a managed rollout, also review the Power Automate analytics in the Power Platform
admin centre for flows in the O365GCal solution.

## Throttling and capacity

The Google Calendar connector allows **100 calls per 60 seconds per connection**. Each
user has their own connection, so this is a per-user budget and does not aggregate
across a rollout.

`MaxMutationsPerRun` (default 60) holds each reconcile under that ceiling. Unchanged
events cost zero calls, so steady-state traffic is near zero regardless of calendar
size; only the initial sync and days with many changes approach the cap, and those
drain across successive runs.

The Office 365 Outlook connector allows 300 calls per 60 seconds — never the binding
constraint here.

## What the solution stores, and where

Three SharePoint lists on a site the user nominates:

| List | Contents |
|---|---|
| `O365GCalSyncMap` | Correlation between Outlook occurrences and Google event IDs, plus a change fingerprint |
| `O365GCalLog` | Audit trail; pruned after `LogRetentionDays` (default 90) |
| `O365GCalHealth` | Per-flow heartbeat |

The sync map holds meeting subjects only indirectly — the fingerprint contains subject
and attendee text. Treat the site as holding calendar metadata and set its permissions
accordingly. Under `PrivacyMode=busy-only`, private events contribute no subject,
location or attendee data to any of it.

## Verifying an install

```zsh
./scripts/status.sh
```

Reports the installed version, connection health and the flows present. For deeper
checks, the `O365GCalLog` list records every decision with a timestamp.
