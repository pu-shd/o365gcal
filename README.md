# O365GCal

One-way mirror of an Outlook calendar onto a Google calendar, built as a Power Platform
solution using Standard connectors only (no Premium licence).

> **First:** some tenants block Microsoft and Google connectors in the same flow. Run
> `./scripts/preflight.sh`; if it fails, nothing here can work.

## Install

```zsh
brew install --cask dotnet-sdk && brew install azure-cli
dotnet tool install --global Microsoft.PowerApps.CLI.Tool
pac auth create
./scripts/install.sh
```

Walkthrough: [docs/INSTALL.md](docs/INSTALL.md).

## Use

| Command | Does |
|---|---|
| `./scripts/configure.sh` | Show or change settings (`notify on`, `dryrun on`, `calendar`, …) |
| `./scripts/status.sh` | Health of the install |
| `./scripts/update.sh` | Upgrade, keeping settings and state |
| `./scripts/teardown.sh` | Remove, confirming each step |
| `./scripts/run-flow.sh 7` | Preview duplicate cleanup |
| `./scripts/run-flow.sh --apply 7` | Open the page that runs it for real |

Lifecycle scripts never touch calendar events. Teardown keeps the SharePoint lists:
they are the only record of which Google events the mirror created.

## Behaviour

Changes appear within ~15 minutes. Recurring meetings become individual events.
Attendees are listed in the description, not invited. Attachments and rooms are not
mirrored. Details: [docs/SYNCHRONIZATION.md](docs/SYNCHRONIZATION.md).

## Develop

```zsh
make test-docker   # full offline suite, as CI runs it
make build         # pack solution zips into dist/
```

`solution/src/` is the source of truth; `src/o365gcal/` is the tested reference engine.
See [ARCHITECTURE](docs/ARCHITECTURE.md), [ADMIN](docs/ADMIN.md) and
[TROUBLESHOOTING](docs/TROUBLESHOOTING.md).
