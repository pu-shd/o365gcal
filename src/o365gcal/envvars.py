"""The environment variable catalogue -- one definition of every setting.

`tools/gen_envvars.py` writes the solution XML from this, and
`tests/validate/test_solution_static.py` checks the two stay in step. Hand-written
XML is what produced the first import failure: the definition file wants element text
and a deterministic id, not the attribute form it is tempting to guess at.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

#: environmentvariabledefinition `type` option set.
STRING, NUMBER, BOOLEAN, JSON, DATASOURCE, SECRET = (
    100000000, 100000001, 100000002, 100000003, 100000004, 100000005,
)

#: Stable namespace so regenerating never churns ids, which would make every
#: solution upgrade look like a delete-and-recreate of all settings.
_NS = uuid.uuid5(uuid.NAMESPACE_URL, "https://github.com/pu-orfe/o365gcal")


@dataclass(frozen=True)
class EnvVar:
    name: str
    type: int
    default: str
    description: str
    #: No sensible default exists and the solution cannot work until the installer
    #: supplies one. Marked required so the import wizard prompts for it, rather than
    #: importing cleanly and failing on first run.
    required_at_install: bool = False

    @property
    def optional(self) -> bool:
        """No default and not demanded at install: may legitimately be null forever,
        so every flow that reads it must coalesce."""
        return not self.default and not self.required_at_install

    @property
    def schema_name(self) -> str:
        return f"o3gc_{self.name}"

    @property
    def definition_id(self) -> str:
        return str(uuid.uuid5(_NS, f"environmentvariabledefinition/{self.schema_name}"))


CATALOGUE: list[EnvVar] = [
    EnvVar("OutlookCalendarId", STRING, "Calendar",
           "Source Outlook calendar. Use an ID from flow 0 Setup; 'Calendar' is the primary mailbox calendar."),
    EnvVar("GoogleCalendarId", STRING, "primary",
           "Target Google calendar ID. 'primary' is the signed-in Google account's main calendar; otherwise the long ...@group.calendar.google.com ID from flow 0 Setup."),
    EnvVar("StateSiteUrl", STRING, "",
           "SharePoint site URL holding the three state lists, e.g. https://contoso.sharepoint.com/sites/MyCalendarSync",
           required_at_install=True),
    EnvVar("StateListName", STRING, "O365GCalSyncMap",
           "Title of the sync-map list. Correlates Outlook occurrences to Google event IDs."),
    EnvVar("LogListName", STRING, "O365GCalLog",
           "Title of the append-only audit log list."),
    EnvVar("HealthListName", STRING, "O365GCalHealth",
           "Title of the heartbeat list the watchdog reads."),
    EnvVar("WindowPastDays", NUMBER, "7",
           "How many days into the past to mirror. Events older than this are left alone, never deleted."),
    EnvVar("WindowFutureDays", NUMBER, "120",
           "How many days ahead to mirror. Larger windows cost more Google API calls per reconcile."),
    EnvVar("AlertEmail", STRING, "",
           "Where digests and breakage alerts are sent. Usually your own address.",
           required_at_install=True),
    EnvVar("DryRun", BOOLEAN, "no",
           "When on, flows log every intended change but perform no Google writes. Use for the first run."),
    # The default is the literal word "none", not an empty string and not a space.
    # A variable with neither a value nor a default cannot be resolved at all - every
    # flow referencing it fails to activate with
    # XrmEnvironmentVariableAttributeNotFound. A single space looked like a neat way
    # round that and is not: whitespace-only XML text is normalised away on import, so
    # the default became empty again and a fresh install failed the same way. A
    # sentinel survives, and both the engine and the flow map it to no prefix.
    EnvVar("TitlePrefix", STRING, "none",
           "Prefix for mirrored event titles, e.g. '[Outlook] '. The literal word "
           "'none' means no prefix."),
    EnvVar("PrivacyMode", STRING, "full",
           "'full' mirrors all details. 'busy-only' mirrors private and confidential events as an opaque 'Busy' block with no subject, location or attendees."),
    EnvVar("MaxMutationsPerRun", NUMBER, "60",
           "Cap on Google writes per reconcile. The connector allows 100 calls per 60 seconds; staying under it lets a backlog drain across runs instead of failing mid-batch."),
    EnvVar("MaxDeletePercent", NUMBER, "25",
           "Circuit breaker. If one run would delete more than this share of mirrored events, it deletes nothing and alerts instead - a short Outlook read looks identical to a mass cancellation."),
    EnvVar("MinDeletesBeforeBreaker", NUMBER, "5",
           "Absolute floor below which the delete percentage rule is not applied, so a sparse calendar can still have events legitimately removed."),
    EnvVar("HeartbeatStaleMinutes", NUMBER, "90",
           "Watchdog threshold. Alerts if a flow has not recorded a successful run within this many minutes."),
    EnvVar("LogRetentionDays", NUMBER, "90",
           "Audit log rows older than this are pruned by the watchdog."),
    EnvVar("NotifyOnChange", BOOLEAN, "no",
           "Email a summary after any reconcile that changed something, naming each "
           "event added, updated or removed. Silent when a run changes nothing, so it "
           "does not become noise. Off by default; the daily digest covers the same "
           "ground once a day."),
    EnvVar("MapRetentionDays", NUMBER, "400",
           "How long a sync-map row is kept after its occurrence leaves the sync window. "
           "Pruning a row makes the automation forget it created that Google event, so this "
           "is deliberately much wider than the window."),
    EnvVar("DeletedRowRetentionDays", NUMBER, "30",
           "How long to keep a row recording that an event was withdrawn from Google. It "
           "points at nothing, so it is only a short audit trail."),
    EnvVar("ListSizeWarnAt", NUMBER, "4000",
           "Row count at which the watchdog warns. Below SharePoint's 5000-row view "
           "threshold so the warning arrives before list reads start truncating."),
    EnvVar("BackupFolderName", STRING, "O365GCal-Backups",
           "Folder in the state site's document library where flow 6 writes backups. "
           "On a OneDrive-backed site this appears in your own Files."),
    EnvVar("BackupRetentionCount", NUMBER, "8",
           "How many backup folders to keep. Older ones are removed by flow 6 after a "
           "successful backup, newest first."),
]

BY_SCHEMA = {v.schema_name: v for v in CATALOGUE}


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def definition_xml(var: EnvVar) -> str:
    """The `environmentvariabledefinition.xml` body.

    Two things here are load-bearing and neither is guessable:

    1. **No XML declaration.** The importer splices this file's root node straight
       into customizations.xml, and an `<?xml ?>` declaration cannot be a child node.
       Including one fails the entire solution import with nothing more specific than
       "the specified node cannot be inserted ... the specified node is the wrong
       type" -- no file name, no line number. This cost a long bisect to find.
    2. **Child elements carry text, not attributes.** `<displayname>X</displayname>`,
       never `<displayname default="X"/>`.
    3. **`<defaultvalue>` is always emitted, even when empty.** Omitting it looked
       tidier and broke flow activation outright: a variable with neither a value nor
       a default cannot be referenced at all, and every flow reading it fails with
       `XrmEnvironmentVariableAttributeNotFound: Attribute 'value' was not found`.
       An empty element supplies an empty-string default, which resolves cleanly.
       Flows still read optional variables through `expressions.env_or`.
    """
    default = f"  <defaultvalue>{_escape(var.default)}</defaultvalue>\n"
    return (
        f'<environmentvariabledefinition environmentvariabledefinitionid="{{{var.definition_id}}}" '
        f'schemaname="{var.schema_name}">\n'
        f"  <displayname>{_escape(var.name)}</displayname>\n"
        f"  <description>{_escape(var.description)}</description>\n"
        f"  <type>{var.type}</type>\n"
        f"  <isrequired>{1 if var.required_at_install else 0}</isrequired>\n"
        f"{default}"
        f"  <introducedversion>1.0</introducedversion>\n"
        f"  <iscustomizable>1</iscustomizable>\n"
        f"</environmentvariabledefinition>\n"
    )
