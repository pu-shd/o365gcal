"""Generate flow 0: Setup and Provision.

A real generator rather than an ad-hoc script, because the previous one was run once
and thrown away. Two columns were later added to `o365gcal.schema` and nothing
regenerated the provisioning actions, so the columns were never created and every
health-row insert failed - discovered only from a flow run, hours later.

Everything here derives from `o365gcal.schema` and `o365gcal.envvars`, so the lists
the flows read and the lists setup creates cannot drift apart.

    .venv/bin/python tools/gen_flow0.py && ./scripts/build.sh
"""

import json
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from o365gcal.schema import ALL_LISTS, HEARTBEAT_EXPECTATIONS, NOTE  # noqa: E402

OUT = ROOT / "solution/src/Workflows/O365GCal-0-Setup-9C1F4A20-3B77-4E51-9A3D-5E2B10C7D004.json"
SP_API = "/providers/Microsoft.PowerApps/apis/shared_sharepointonline"
O3_API = "/providers/Microsoft.PowerApps/apis/shared_office365"
GC_API = "/providers/Microsoft.PowerApps/apis/shared_googlecalendar"
LIMIT = 256


def d(t):
    return t if len(t) <= LIMIT else textwrap.shorten(t, width=LIMIT, placeholder="")


def E(n):
    return f"parameters('{n} (o3gc_{n})')"


def sp(method, uri, body=None, headers=None, no_retry=False):
    h = {"Accept": "application/json;odata=nometadata"}
    if body is not None:
        h["Content-Type"] = "application/json;odata=nometadata"
    h.update(headers or {})
    inputs = {
        "host": {"connectionName": "shared_sharepointonline", "operationId": "HttpRequest",
                 "apiId": SP_API},
        "parameters": {"dataset": "@" + E("StateSiteUrl"), "parameters/method": method,
                       "parameters/uri": uri, "parameters/headers": h},
    }
    if body is not None:
        inputs["parameters"]["parameters/body"] = body
    if no_retry:
        # Provisioning is idempotent by tolerating failure, so on every run after the
        # first these fail with "already exists". Retrying an expected failure - four
        # attempts with backoff, fifty times over - turned a re-run into ten minutes.
        inputs["retryPolicy"] = {"type": "none"}
    return {"type": "OpenApiConnection", "inputs": inputs}


actions: dict = {}
prev = None


def add(name, action, after=None, states=("Succeeded",)):
    global prev
    action["runAfter"] = {} if after is None else {after: list(states)}
    actions[name] = action
    prev = name
    return name


add("List_Outlook_Calendars", {
    "type": "OpenApiConnection",
    "description": d("Shows which calendar IDs are valid for OutlookCalendarId."),
    "inputs": {"host": {"connectionName": "shared_office365",
                        "operationId": "CalendarGetTables_V2", "apiId": O3_API},
               "parameters": {}}})

add("List_Google_Calendars", {
    "type": "OpenApiConnection",
    "description": d("Shows which calendar IDs are valid for GoogleCalendarId."),
    "inputs": {"host": {"connectionName": "shared_googlecalendar",
                        "operationId": "ListCalendars", "apiId": GC_API},
               "parameters": {"minAccessRole": "writer"}}, }, prev)

for spec in ALL_LISTS:
    key = spec["list"].replace("O365GCal", "")
    create = add(f"Create_List_{key}", {
        **sp("POST", "_api/web/lists",
             "@setProperty(setProperty(setProperty(setProperty(json('{}'), "
             f"'BaseTemplate', 100), 'Title', '{spec['list']}'), "
             f"'Description', '{spec['description'][:180]}'), 'ContentTypesEnabled', false)",
             no_retry=True),
        "description": d(f"Creates {spec['list']}, tolerating 'already exists' - the "
                         f"normal case from the second run onward. Retries are off "
                         f"because that failure is expected."),
    }, prev, ("Succeeded", "Failed"))

    last = create
    for field in spec["fields"]:
        body = ("@setProperty(setProperty(setProperty(setProperty(json('{}'), "
                f"'FieldTypeKind', {field['type']}), 'Title', '{field['name']}'), "
                "'Required', false), 'EnforceUniqueValues', false)")
        # No NumberOfLines/RichText: with odata=nometadata SharePoint cannot infer
        # the field subtype, so those are invalid on the base SP.Field and the create
        # fails with BadRequest. The defaults suit us.
        a = {**sp("POST", f"_api/web/lists/getbytitle('{spec['list']}')/fields",
                  body, no_retry=True)}
        if field.get("note"):
            a["description"] = d(field["note"])
        last = add(f"Add_{key}_{field['name']}", a, last, ("Succeeded", "Failed"))

    for field in [f for f in spec["fields"] if f.get("indexed")] :
        last = add(f"Index_{key}_{field['name']}", {
            **sp("POST",
                 f"_api/web/lists/getbytitle('{spec['list']}')/fields/getbytitle('{field['name']}')",
                 "@setProperty(json('{}'), 'Indexed', true)",
                 {"X-HTTP-Method": "MERGE", "IF-MATCH": "*"}, no_retry=True),
            "description": d("Indexed: filtered on every run, and SharePoint refuses "
                             "unindexed filters past 5000 items."),
        }, last, ("Succeeded", "Failed"))

    last = add(f"Index_{key}_Title", {
        **sp("POST",
             f"_api/web/lists/getbytitle('{spec['list']}')/fields/getbytitle('Title')",
             "@setProperty(json('{}'), 'Indexed', true)",
             {"X-HTTP-Method": "MERGE", "IF-MATCH": "*"}, no_retry=True),
        "description": d("Title carries the correlation key, the run id or the flow "
                         "name; every lookup filters on it."),
    }, last, ("Succeeded", "Failed"))
    prev = last

seed_rows = [{"name": f, "stale": m, "affects": "yes" if f == "3 Reconcile" else "no"}
             for f, m in HEARTBEAT_EXPECTATIONS.items()]

monitored = "createArray(" + ", ".join(f"'{f}'" for f in HEARTBEAT_EXPECTATIONS) + ")"
HEALTH = E("HealthListName")


def health_uri(suffix=""):
    """A SharePoint items URI for the health list.

    Built with concat() throughout: the expression language has no operators at
    all, so a '+' between strings is not concatenation - it is a syntax error the
    runtime only reports when the action runs.
    """
    return ("concat('_api/web/lists/getbytitle(''', " + HEALTH
            + ", ''')/items" + suffix + "')")


monitored = "createArray(" + ", ".join(f"'{f}'" for f in HEARTBEAT_EXPECTATIONS) + ")"
EXISTING = "coalesce(body('Get_Existing_Health')?['value'], json('[]'))"
ROW_ID = "string(first(body('Filter_Existing_Row'))?['Id'])"

add("Get_Existing_Health", {
    **sp("GET", "@{" + health_uri("?$select=Id,Title&$top=200") + "}"),
    "description": d("Health rows are replaced wholesale rather than updated in "
                     "place. An upsert keyed on Title cannot remove the duplicates "
                     "an earlier insert-only version left behind, and a duplicate "
                     "row carrying no threshold is exactly what raised false "
                     "alarms."),
}, prev, ("Succeeded", "Failed"))

add("Remove_All_Health_Rows", {
    "type": "Foreach",
    "description": d("Clears the list so seeding cannot duplicate. Heartbeat "
                     "history is reset, which is acceptable: setup is an explicit "
                     "act, and every monitored flow rewrites its row within one "
                     "cycle."),
    "foreach": "@coalesce(body('Get_Existing_Health')?['value'], json('[]'))",
    "runtimeConfiguration": {"concurrency": {"repetitions": 1}},
    "actions": {"Delete_Health_Row": {"runAfter": {}, **sp("POST",
        "@{" + health_uri("(', string(item()?['Id']), ')") + "}",
        None, {"X-HTTP-Method": "DELETE", "IF-MATCH": "*"}, no_retry=True)}},
}, prev)

add("Seed_Health_Rows", {
    "type": "Foreach",
    "description": d("One row per monitored flow, each carrying its own staleness "
                     "threshold. A single global threshold marked the daily digest "
                     "as broken for 22 hours out of every 24."),
    "foreach": "@json('" + json.dumps(seed_rows).replace("'", "''") + "')",
    "runtimeConfiguration": {"concurrency": {"repetitions": 1}},
    "actions": {"Create_Health_Row": {"runAfter": {}, **sp("POST",
        "@{" + health_uri() + "}",
        "@setProperty(setProperty(setProperty(setProperty(setProperty(setProperty("
        "json('{}'), 'Title', items('Seed_Health_Rows')?['name']), "
        "'StaleAfterMinutes', items('Seed_Health_Rows')?['stale']), "
        "'AffectsSync', items('Seed_Health_Rows')?['affects']), "
        "'LastSuccessUtc', utcNow()), "
        "'LastRunStatus', 'Provisioned'), 'ConsecutiveFailures', 0)"),
        "description": d("LastSuccessUtc is stamped now so a freshly seeded row is "
                         "not instantly stale - otherwise setup itself would "
                         "trigger the alert it exists to make trustworthy."),
    }},
}, prev, ("Succeeded", "Failed"))

add("Send_Setup_Summary", {
    "type": "OpenApiConnection",
    "inputs": {"host": {"connectionName": "shared_office365",
                        "operationId": "SendEmailV2", "apiId": O3_API},
               "parameters": {
                   "emailMessage/To": "@" + E("AlertEmail"),
                   "emailMessage/Subject": "O365GCal: setup complete - confirm your calendar IDs",
                   "emailMessage/Body": (
                       "<h3>O365GCal is provisioned</h3>"
                       "<p>State lists created at <a href=\"@{" + E("StateSiteUrl") + "}\">"
                       "@{" + E("StateSiteUrl") + "}</a>.</p>"
                       "<p><b>Current configuration</b><br>"
                       "Outlook calendar: <code>@{" + E("OutlookCalendarId") + "}</code><br>"
                       "Google calendar: <code>@{" + E("GoogleCalendarId") + "}</code><br>"
                       "Sync window: @{" + E("WindowPastDays") + "} days back to "
                       "@{" + E("WindowFutureDays") + "} days ahead<br>"
                       "Dry run: <b>@{" + E("DryRun") + "}</b></p>"
                       "<p><b>Your Outlook calendars</b><br>"
                       "<code>@{string(body('List_Outlook_Calendars')?['value'])}</code></p>"
                       "<p><b>Your Google calendars</b><br>"
                       "<code>@{string(body('List_Google_Calendars')?['items'])}</code></p>"
                       "<p>If the IDs above are not the ones you want, update the "
                       "environment variables before turning on flows 3, 4, 5 and 6. "
                       "Leave DryRun on for one reconcile and read the log list first.</p>"),
                   "emailMessage/Importance": "Normal"}},
}, prev)

params = {f"{n} (o3gc_{n})": {"defaultValue": v, "type": t,
                              "metadata": {"schemaName": f"o3gc_{n}"}}
          for n, v, t in [("OutlookCalendarId", "Calendar", "String"),
                          ("GoogleCalendarId", "primary", "String"),
                          ("StateSiteUrl", "", "String"),
                          ("StateListName", "O365GCalSyncMap", "String"),
                          ("LogListName", "O365GCalLog", "String"),
                          ("HealthListName", "O365GCalHealth", "String"),
                          ("AlertEmail", "", "String"),
                          ("WindowPastDays", 7, "Int"),
                          ("WindowFutureDays", 120, "Int"),
                          ("DryRun", "no", "String")]}

flow = {"properties": {
    "connectionReferences": {
        "shared_office365": {"runtimeSource": "embedded",
            "connection": {"connectionReferenceLogicalName": "o3gc_sharedoffice365"},
            "api": {"name": "shared_office365"}},
        "shared_googlecalendar": {"runtimeSource": "embedded",
            "connection": {"connectionReferenceLogicalName": "o3gc_sharedgooglecalendar"},
            "api": {"name": "shared_googlecalendar"}},
        "shared_sharepointonline": {"runtimeSource": "embedded",
            "connection": {"connectionReferenceLogicalName": "o3gc_sharedsharepointonline"},
            "api": {"name": "shared_sharepointonline"}}},
    "definition": {
        "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
        "contentVersion": "1.0.0.0",
        "description": (
            "Run once at install, and again after any upgrade that adds a column. Lists "
            "the available Outlook and Google calendars, provisions the three state "
            "lists with their indexed columns, seeds one health row per monitored flow "
            "with that flow's own staleness threshold, and emails a configuration "
            "summary. Idempotent: every create tolerates 'already exists', and retries "
            "are disabled on those so a re-run takes seconds rather than minutes."),
        "parameters": {"$connections": {"defaultValue": {}, "type": "Object"},
                       "$authentication": {"defaultValue": {}, "type": "SecureObject"},
                       **params},
        "triggers": {"manual": {"type": "Request", "kind": "Button",
            "metadata": {"operationMetadataId": "9c1f4a20-3b77-4e51-9a3d-000000000004"},
            "inputs": {"schema": {"type": "object", "properties": {}}}}},
        "actions": actions, "outputs": {}}},
    "schemaVersion": "1.0.0.0"}

OUT.write_text(json.dumps(flow, indent=2, ensure_ascii=False) + "\n")
print(f"flow 0 regenerated: {len(actions)} actions, "
      f"{sum(len(s['fields']) for s in ALL_LISTS)} columns provisioned")
