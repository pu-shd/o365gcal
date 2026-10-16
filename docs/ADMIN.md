# Administration

## DLP can block it

One flow needs three Standard connectors together: Office 365 Outlook
(`shared_office365`), Google Calendar (`shared_googlecalendar`) and SharePoint
(`shared_sharepointonline`). Policies that put Google in Non-Business and Microsoft in
Business block this at save time. Healthy connections do not prove the pairing is
allowed; the save test in [INSTALL.md](INSTALL.md) (also printed by
`./scripts/preflight.sh`) does.

If blocked, in increasing effort:

1. Put Google Calendar in the same data group as Office 365. This lets any maker in
   that environment move data between the two.
2. Use a dedicated environment with its own policy, limited to the people who need it.
3. Run the equivalent outside Power Platform (Logic App or a container against Graph
   and the Google Calendar API).

## Licensing

No Premium. SharePoint lists replace Dataverse, *Send an HTTP request to SharePoint*
replaces the HTTP action, and a heartbeat list plus watchdog replaces Power Automate
Management. `test_no_premium_connectors` fails the build if a Premium connector appears.

## Rollout

Each person installs their own copy with their own connections; nothing is shared. Hand
out `dist/O365GCal_managed.zip`, `docs/INSTALL.md` and `scripts/`. For a template, keep
an install's `o365gcal.settings.json`, blank the personal values, and have users run
`./scripts/bootstrap.sh --settings <file>`. A shared site works if each person uses
their own list names (`StateListName`, `LogListName`, `HealthListName`).

## Second-layer alerting

Flow 5 alerts on stale heartbeats and broken connections, but cannot report its own
failure. Make sure Power Platform's own "your flow is failing" emails are not filtered;
the platform also emails before switching off a flow that has failed for 90 days.

## Capacity

The Google connector allows 100 calls per 60 seconds per connection, so per user.
`MaxMutationsPerRun` (60) keeps each reconcile under it, and unchanged events cost
nothing. Outlook's 300 per 60 seconds is never the limit.

## Stored data

| List | Holds |
|---|---|
| `O365GCalSyncMap` | Outlook occurrence ↔ Google event id, plus a fingerprint containing subject and attendee text |
| `O365GCalLog` | Every decision; pruned after `LogRetentionDays` (90) |
| `O365GCalHealth` | Per-flow heartbeat |

Treat the site as holding calendar metadata. With `HidePrivateEventDetails` on, private
events contribute no subject, location or attendees.

## Connector contract checks

`./scripts/fetch-connector-swagger.sh` downloads the live connector definitions;
`tests/validate/test_connector_contract.py` then checks every flow's parameter names
against them. Without the download those tests skip.
