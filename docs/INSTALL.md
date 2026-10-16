# Install

One-way: Outlook is the source; edits made on the Google copy are overwritten. About
20 minutes.

## Needs

- Microsoft 365 with Power Automate (the seeded licence is enough).
- A Google account.
- A SharePoint site you can create lists in (a personal site works).
- Two CLIs: `pac` (installs the solution) and `az` (switches flows on).

```zsh
brew install --cask dotnet-sdk
dotnet tool install --global Microsoft.PowerApps.CLI.Tool
brew install azure-cli
```

Linux: install the .NET SDK and Azure CLI from Microsoft's packages, then the same
`dotnet tool install`. If `pac` is then not found, add `$HOME/.dotnet/tools` to `PATH`.

**Check the connectors can be combined.** In
[make.powerautomate.com](https://make.powerautomate.com), create an instant flow with
Office 365 Outlook → *Get calendars (V2)*, Google Calendar → *List calendars* and
SharePoint → *Send an HTTP request to SharePoint*, and save it. A data loss prevention
error means it cannot work: send [ADMIN.md](ADMIN.md) to your Power Platform admin.

## Install

```zsh
pac auth create
./scripts/install.sh
```

It finds a site for its lists, links your accounts (one browser sign-in to Google),
installs, lets you pick the Google calendar from a list, and does a practice run that
writes nothing before you approve.

## Settings

```zsh
./scripts/configure.sh               # list all, with defaults
./scripts/configure.sh notify on     # email on every change
./scripts/configure.sh dryrun on     # practice mode
./scripts/configure.sh calendar      # pick another Google calendar
./scripts/configure.sh window 7 120  # days back, days ahead
```

Flows that are on are restarted so they pick up the change.

## By hand

1. `./scripts/build.sh`, then import `dist/O365GCal_managed.zip` under **Solutions**.
2. Pick **Office 365 Outlook**, not Outlook.com, for the connection.
3. Set `StateSiteUrl` and `AlertEmail`.
4. `./scripts/enable-flows.sh`, then `./scripts/run-flow.sh 0`.

## Upgrade and remove

`./scripts/update.sh` replaces the flows and keeps settings, connections and lists.
`./scripts/teardown.sh` removes in confirmed stages and keeps the SharePoint lists,
which are the only record of which Google events are mirrored. Delete those Google
events *before* uninstalling if you want them gone.

Problems: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
