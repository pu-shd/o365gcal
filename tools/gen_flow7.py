"""Generate flow 7: Dedup and Repair.

Kept as a generator because the flow is mostly repetitive structure around a handful
of expressions that must match src/o365gcal/dedup.py. Regenerate with:

    .venv/bin/python tools/gen_flow7.py && ./scripts/build.sh
"""

import json
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "solution/src/Workflows/O365GCal-7-Dedup-9C1F4A20-3B77-4E51-9A3D-5E2B10C7D008.json"

LIMIT = 256
MARKER = "o365gcal-key:"
SP_API = "/providers/Microsoft.PowerApps/apis/shared_sharepointonline"
GC_API = "/providers/Microsoft.PowerApps/apis/shared_googlecalendar"
O3_API = "/providers/Microsoft.PowerApps/apis/shared_office365"


def d(text):
    return text if len(text) <= LIMIT else textwrap.shorten(text, width=LIMIT, placeholder="")


def E(name):
    return f"parameters('{name} (o3gc_{name})')"


ENV = [("GoogleCalendarId", "primary", "String"), ("StateSiteUrl", "", "String"),
       ("StateListName", "O365GCalSyncMap", "String"), ("LogListName", "O365GCalLog", "String"),
       ("AlertEmail", "", "String"), ("WindowPastDays", 7, "Int"),
       ("WindowFutureDays", 120, "Int"), ("MaxDeletePercent", 25, "Int"),
       ("MinDeletesBeforeBreaker", 5, "Int")]
PARAMS = {f"{n} (o3gc_{n})": {"defaultValue": v, "type": t, "metadata": {"schemaName": f"o3gc_{n}"}}
          for n, v, t in ENV}

LIST_URI = "@{concat('_api/web/lists/getbytitle(''', " + E("LogListName") + ", ''')/items')}"


def sp(method, uri, body=None, headers=None):
    h = {"Accept": "application/json;odata=nometadata"}
    if body is not None:
        h["Content-Type"] = "application/json;odata=nometadata"
    h.update(headers or {})
    params = {"dataset": "@" + E("StateSiteUrl"), "parameters/method": method,
              "parameters/uri": uri, "parameters/headers": h}
    if body is not None:
        params["parameters/body"] = body
    return {"type": "OpenApiConnection",
            "inputs": {"host": {"connectionName": "shared_sharepointonline",
                                "operationId": "HttpRequest", "apiId": SP_API},
                       "parameters": params}}


def log_row(level, op, message_expr, after):
    body = ("@setProperty(setProperty(setProperty(setProperty(setProperty(setProperty("
            "json('{}'), 'Title', variables('RunId')), 'Timestamp', utcNow()), "
            "'FlowName', '7 Dedup and Repair'), "
            f"'Level', '{level}'), 'Operation', '{op}'), 'Message', {message_expr})")
    action = sp("POST", LIST_URI, body)
    action["runAfter"] = after
    return action


actions = {}


def add(name, action, after=None, states=("Succeeded",)):
    action["runAfter"] = {} if after is None else {after: list(states)}
    actions[name] = action
    return name


prev = None
for name, typ, val in [("RunId", "string", "@{guid()}"), ("Pairs", "array", []),
                       ("Unreadable", "array", []), ("Deletes", "array", []),
                       ("Repairs", "array", []), ("Removed", "integer", 0)]:
    prev = add(f"Init_{name}",
               {"type": "InitializeVariable",
                "inputs": {"variables": [{"name": name, "type": typ, "value": val}]}}, prev)

prev = add("Get_Map_Rows", {**sp("GET",
    "@{concat('_api/web/lists/getbytitle(''', " + E("StateListName") + ", ''')/items?$select=Id,Title,GoogleEventId,SyncState&$top=5000')}"),
    "description": d("The map is consulted, not trusted. It may be empty - that is the "
                     "situation this flow repairs - but where it does name an event, that "
                     "copy is kept so a working install keeps working.")}, prev)

prev = add("Compose_WindowStart", {"type": "Compose",
    "inputs": "@startOfDay(addDays(utcNow(), mul(-1, int(" + E("WindowPastDays") + "))))"}, prev)

prev = add("Compose_SliceCount", {"type": "Compose",
    "description": d("One slice per day. ListEvents has no paging and returns arbitrary "
                     "order, so the only reliable enumeration is a range small enough that "
                     "one response is the whole answer."),
    "inputs": "@add(int(" + E("WindowPastDays") + "), int(" + E("WindowFutureDays") + "))"}, prev)

collect_pair = {
    "runAfter": {}, "type": "AppendToArrayVariable",
    "inputs": {"name": "Pairs", "value": {
        "key": "@trim(last(split(coalesce(item()?['description'], ''), '" + MARKER + "')))",
        "id": "@item()?['id']",
        "summary": "@coalesce(item()?['summary'], '')",
        "htmlLink": "@coalesce(item()?['htmlLink'], '')"}}}

prev = add("For_Each_Slice", {"type": "Foreach",
    "foreach": "@range(0, outputs('Compose_SliceCount'))",
    "runtimeConfiguration": {"concurrency": {"repetitions": 1}},
    "actions": {
        "Google_List": {
            "runAfter": {}, "type": "OpenApiConnection",
            "description": d("q filters to events carrying our marker. Google's text index "
                             "is not read-your-writes consistent, so a very recent event may "
                             "not appear - which is why this is a repair tool, not part of "
                             "the sync path."),
            "inputs": {"host": {"connectionName": "shared_googlecalendar",
                                "operationId": "ListEvents", "apiId": GC_API},
                       "parameters": {
                           "calendarId": "@" + E("GoogleCalendarId"),
                           "timeMin": "@{formatDateTime(addDays(outputs('Compose_WindowStart'), item()), 'yyyy-MM-ddTHH:mm:ss.fffZ')}",
                           "timeMax": "@{formatDateTime(addDays(outputs('Compose_WindowStart'), add(item(), 1)), 'yyyy-MM-ddTHH:mm:ss.fffZ')}",
                           "q": MARKER}},
            "operationOptions": "DisableAsyncPattern"},
        "Compose_Returned": {
            "runAfter": {"Google_List": ["Succeeded"]}, "type": "Compose",
            "inputs": "@length(coalesce(body('Google_List')?['items'], json('[]')))"},
        "Check_Saturated": {
            "runAfter": {"Compose_Returned": ["Succeeded"]}, "type": "If",
            "description": d("A suspiciously full response may have been truncated and the "
                             "connector gives no way to tell. Treating it as complete would "
                             "delete events that merely fell off the end, so the slice is "
                             "marked unreadable instead."),
            "expression": {"greaterOrEquals": ["@outputs('Compose_Returned')", 250]},
            "actions": {"Mark_Unreadable": {
                "runAfter": {}, "type": "AppendToArrayVariable",
                "inputs": {"name": "Unreadable",
                           "value": "@{formatDateTime(addDays(outputs('Compose_WindowStart'), item()), 'yyyy-MM-dd')}"}}},
            "else": {"actions": {"For_Each_Event": {
                "runAfter": {}, "type": "Foreach",
                "foreach": "@coalesce(body('Google_List')?['items'], json('[]'))",
                "runtimeConfiguration": {"concurrency": {"repetitions": 1}},
                "actions": {"Check_Marked": {
                    "runAfter": {}, "type": "If",
                    "description": d("Without the marker the event is not ours - the user's "
                                     "own, or one whose description was edited beyond "
                                     "recognition. Never a deletion candidate."),
                    "expression": {"contains": ["@coalesce(item()?['description'], '')", MARKER]},
                    "actions": {"Collect_Pair": collect_pair},
                    "else": {"actions": {}}}}}}}}}}, prev)

prev = add("Select_Keys", {"type": "Select",
    "inputs": {"from": "@variables('Pairs')", "select": "@item()?['key']"}}, prev)

prev = add("Compose_UniqueKeys", {"type": "Compose",
    "description": d("union with itself is the only way to get distinct values here."),
    "inputs": "@union(body('Select_Keys'), body('Select_Keys'))"}, prev)

MAP_ROWS = "coalesce(body('Get_Map_Rows')?['value'], json('[]'))"
ROW_FOR_KEY = "first(body('Filter_Repair_Row'))"

prev = add("For_Each_Key", {"type": "Foreach",
    "foreach": "@outputs('Compose_UniqueKeys')",
    "runtimeConfiguration": {"concurrency": {"repetitions": 1}},
    "actions": {
        "Filter_Group": {"runAfter": {}, "type": "Query",
            "inputs": {"from": "@variables('Pairs')",
                       "where": "@equals(item()?['key'], items('For_Each_Key'))"}},
        "Compose_Group": {"runAfter": {"Filter_Group": ["Succeeded"]}, "type": "Compose",
            "inputs": "@body('Filter_Group')"},
        "Filter_Map_Row": {"runAfter": {"Compose_Group": ["Succeeded"]}, "type": "Query",
            "inputs": {"from": "@" + MAP_ROWS,
                       "where": "@equals(item()?['Title'], items('For_Each_Key'))"}},
        "Compose_Mapped": {"runAfter": {"Filter_Map_Row": ["Succeeded"]}, "type": "Compose",
            "inputs": "@coalesce(first(body('Filter_Map_Row'))?['GoogleEventId'], '')"},
        "Select_Group_Ids": {"runAfter": {"Compose_Mapped": ["Succeeded"]}, "type": "Select",
            "inputs": {"from": "@outputs('Compose_Group')", "select": "@item()?['id']"}},
        "Compose_Survivor": {"runAfter": {"Select_Group_Ids": ["Succeeded"]}, "type": "Compose",
            "description": d("The mapped copy if it still exists, else the lowest id. "
                             "Arbitrary but stable: an unstable choice would delete a "
                             "different copy every run until none were left."),
            "inputs": ("@if(and(not(empty(outputs('Compose_Mapped'))), "
                       "contains(body('Select_Group_Ids'), outputs('Compose_Mapped'))), "
                       "outputs('Compose_Mapped'), "
                       "first(sort(body('Select_Group_Ids'))))")},
        "Check_Duplicate": {"runAfter": {"Compose_Survivor": ["Succeeded"]}, "type": "If",
            "description": d("Deletion requires a proven duplicate. A key with a single "
                             "event is left alone even if the map disagrees about it."),
            "expression": {"greater": ["@length(outputs('Compose_Group'))", 1]},
            "actions": {
                "Filter_Extras": {"runAfter": {}, "type": "Query",
                    "description": d("The survivor is excluded here, by construction rather "
                                     "than by a later check that could drift."),
                    "inputs": {"from": "@outputs('Compose_Group')",
                               "where": "@not(equals(item()?['id'], outputs('Compose_Survivor')))"}},
                "For_Each_Extra": {"runAfter": {"Filter_Extras": ["Succeeded"]}, "type": "Foreach",
                    "foreach": "@body('Filter_Extras')",
                    "runtimeConfiguration": {"concurrency": {"repetitions": 1}},
                    "actions": {"Collect_Delete": {
                        "runAfter": {}, "type": "AppendToArrayVariable",
                        "inputs": {"name": "Deletes", "value": {
                            "key": "@items('For_Each_Key')",
                            "id": "@item()?['id']",
                            "summary": "@coalesce(item()?['summary'], '')"}}}}},
                "Filter_Survivor": {"runAfter": {"For_Each_Extra": ["Succeeded"]},
                    "type": "Query",
                    "inputs": {"from": "@outputs('Compose_Group')",
                               "where": "@equals(item()?['id'], outputs('Compose_Survivor'))"}},
                "Collect_Repair": {"runAfter": {"Filter_Survivor": ["Succeeded"]},
                    "type": "AppendToArrayVariable",
                    "inputs": {"name": "Repairs", "value": {
                        "key": "@items('For_Each_Key')",
                        "id": "@outputs('Compose_Survivor')",
                        "htmlLink": "@coalesce(first(body('Filter_Survivor'))?['htmlLink'], '')"}}}},
            "else": {"actions": {"Check_Needs_Row": {
                "runAfter": {}, "type": "If",
                "description": d("A single marked event whose map row is missing or points "
                                 "elsewhere is exactly what a rebuild needs."),
                "expression": {"not": {"equals": ["@outputs('Compose_Mapped')",
                                                  "@outputs('Compose_Survivor')"]}},
                "actions": {"Collect_Rebuild": {
                    "runAfter": {}, "type": "AppendToArrayVariable",
                    "inputs": {"name": "Repairs", "value": {
                        "key": "@items('For_Each_Key')",
                        "id": "@outputs('Compose_Survivor')",
                        "htmlLink": "@coalesce(first(outputs('Compose_Group'))?['htmlLink'], '')"}}}},
                "else": {"actions": {}}}}}}}}, prev)

apply_actions = {
    "For_Each_Delete": {"runAfter": {}, "type": "Foreach",
        "foreach": "@variables('Deletes')",
        "runtimeConfiguration": {"concurrency": {"repetitions": 1}},
        "actions": {
            "Google_Delete": {"runAfter": {}, "type": "OpenApiConnection",
                "inputs": {"host": {"connectionName": "shared_googlecalendar",
                                    "operationId": "DeleteEvent", "apiId": GC_API},
                           "parameters": {"calendarId": "@" + E("GoogleCalendarId"),
                                          "eventId": "@items('For_Each_Delete')?['id']"}},
                "operationOptions": "DisableAsyncPattern"},
            "Count_Removed": {"runAfter": {"Google_Delete": ["Succeeded", "Failed"]},
                "type": "IncrementVariable", "inputs": {"name": "Removed", "value": 1},
                "description": d("Counts a failed delete too: a 404 means the event is "
                                 "already gone, which is the desired end state.")},
            "Log_Removed": log_row("Info", "Delete",
                ("concat('Removed duplicate ', items('For_Each_Delete')?['id'], ' of ', "
                 "items('For_Each_Delete')?['key'], ' (', "
                 "coalesce(items('For_Each_Delete')?['summary'], ''), ')')"),
                {"Count_Removed": ["Succeeded"]})}},
    "For_Each_Repair": {"runAfter": {"For_Each_Delete": ["Succeeded"]}, "type": "Foreach",
        "description": d("Rebuilds map rows for survivors. ContentFingerprint is left empty "
                         "on purpose: the true value comes from Outlook, and an invented one "
                         "would mark a stale event as up to date."),
        "foreach": "@variables('Repairs')",
        "runtimeConfiguration": {"concurrency": {"repetitions": 1}},
        "actions": {
            "Filter_Repair_Row": {"runAfter": {}, "type": "Query",
                "description": d("Resolves the existing row id, so the upsert can MERGE "
                                 "when a row exists and POST when it does not."),
                "inputs": {"from": "@" + MAP_ROWS,
                           "where": "@equals(item()?['Title'], items('For_Each_Repair')?['key'])"}},
            "Upsert_Row": {"runAfter": {"Filter_Repair_Row": ["Succeeded"]}, **sp("POST",
            "@{if(empty(coalesce(" + ROW_FOR_KEY + "?['Id'], '')), "
            "concat('_api/web/lists/getbytitle(''', " + E("StateListName") + ", ''')/items'), "
            "concat('_api/web/lists/getbytitle(''', " + E("StateListName") + ", ''')/items(', "
            "string(" + ROW_FOR_KEY + "?['Id']), ')'))}",
            ("@setProperty(setProperty(setProperty(setProperty(setProperty(json('{}'), "
             "'Title', items('For_Each_Repair')?['key']), "
             "'GoogleEventId', items('For_Each_Repair')?['id']), "
             "'GoogleHtmlLink', items('For_Each_Repair')?['htmlLink']), "
             "'SyncState', 'Active'), "
             "'LastError', 'rebuilt by dedup; fingerprint pending next reconcile')")),
            "description": d("MERGE when a row exists, POST when it does not - the same "
                             "upsert the child flow uses.")}}},
    "Log_Applied": log_row("Info", "NoOp",
        ("concat('Dedup applied: removed ', string(variables('Removed')), "
         "' duplicate(s), repaired ', string(length(variables('Repairs'))), ' map row(s).')"),
        {"For_Each_Repair": ["Succeeded"]})}

refused_message = ("concat('Dedup refused to delete. ', string(length(variables('Deletes'))), "
                   "' duplicate(s) found across ', string(length(variables('Pairs'))), "
                   "' marked event(s); ', string(length(variables('Unreadable'))), "
                   "' slice(s) unreadable. Nothing was removed.')")

add("Circuit_Breaker", {"type": "If",
    "description": d("Refuses to act when the read was incomplete, or when the batch is both "
                     "large in absolute terms and a large share of the mirror. This runs when "
                     "things are already confused, so a partial delete is worse than none."),
    "expression": {"or": [
        {"greater": ["@length(variables('Unreadable'))", 0]},
        {"and": [
            {"greater": ["@length(variables('Deletes'))", "@int(" + E("MinDeletesBeforeBreaker") + ")"]},
            {"greater": ["@div(mul(length(variables('Deletes')), 100), max(length(variables('Pairs')), 1))",
                         "@int(" + E("MaxDeletePercent") + ")"]}]}]},
    "actions": {
        "Log_Refused": log_row("Error", "Skip", refused_message, {}),
        "Alert_Refused": {"runAfter": {"Log_Refused": ["Succeeded"]},
            "type": "OpenApiConnection",
            "inputs": {"host": {"connectionName": "shared_office365",
                                "operationId": "SendEmailV2", "apiId": O3_API},
                       "parameters": {
                           "emailMessage/To": "@" + E("AlertEmail"),
                           "emailMessage/Subject": "O365GCal: duplicate cleanup refused - review needed",
                           "emailMessage/Body": (
                               "<p>The duplicate cleanup found <b>@{length(variables('Deletes'))}</b> "
                               "duplicate event(s) among <b>@{length(variables('Pairs'))}</b> mirrored "
                               "events, and <b>@{length(variables('Unreadable'))}</b> day(s) it could "
                               "not read completely.</p><p><b>Nothing was deleted.</b></p>"
                               "<p>An unreadable day means one response may have been truncated, so a "
                               "missing event cannot be told apart from a deleted one. Days affected: "
                               "@{join(variables('Unreadable'), ', ')}</p>"
                               "<p>If the plan looks right, raise MaxDeletePercent for one run. If days "
                               "were unreadable, narrow the sync window and run again.</p>"),
                           "emailMessage/Importance": "High"}}}},
    "else": {"actions": {"Check_Apply": {
        "runAfter": {}, "type": "If",
        "description": d("Defaults to a dry run. The Apply toggle is absent when the flow is "
                         "invoked without a body, so an accidental run reports and changes "
                         "nothing."),
        "expression": {"equals": ["@coalesce(triggerBody()?['boolean'], false)", True]},
        "actions": apply_actions,
        "else": {"actions": {"Log_DryRun": log_row("Warn", "Skip",
            ("concat('DRY RUN: would remove ', string(length(variables('Deletes'))), "
             "' duplicate(s) and repair ', string(length(variables('Repairs'))), "
             "' map row(s) across ', string(length(variables('Pairs'))), "
             "' marked event(s). Re-run with Apply set to yes.')"), {})}}}}}}, prev)

flow = {"properties": {
    "connectionReferences": {
        "shared_googlecalendar": {"runtimeSource": "embedded",
            "connection": {"connectionReferenceLogicalName": "o3gc_sharedgooglecalendar"},
            "api": {"name": "shared_googlecalendar"}},
        "shared_office365": {"runtimeSource": "embedded",
            "connection": {"connectionReferenceLogicalName": "o3gc_sharedoffice365"},
            "api": {"name": "shared_office365"}},
        "shared_sharepointonline": {"runtimeSource": "embedded",
            "connection": {"connectionReferenceLogicalName": "o3gc_sharedsharepointonline"},
            "api": {"name": "shared_sharepointonline"}}},
    "definition": {
        "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
        "contentVersion": "1.0.0.0",
        "description": (
            "Finds and removes duplicate mirrored events and rebuilds the sync map from "
            "Google alone. For the case where the solution was removed, the sync map went "
            "with it, and a reinstall mirrored the whole calendar a second time. Every "
            "mirrored event carries o365gcal-key in its description - the same correlation "
            "key the map uses - so no backup is required. Defaults to a dry run; set the "
            "Apply toggle to delete for real. Mirrors src/o365gcal/dedup.py, whose five "
            "safety invariants are covered by tests."),
        "parameters": {"$connections": {"defaultValue": {}, "type": "Object"},
                       "$authentication": {"defaultValue": {}, "type": "SecureObject"},
                       **PARAMS},
        "triggers": {"manual": {"type": "Request", "kind": "Button",
            "metadata": {"operationMetadataId": "9c1f4a20-3b77-4e51-9a3d-000000000008"},
            "inputs": {"schema": {"type": "object", "properties": {
                "boolean": {"title": "Apply deletions", "type": "boolean",
                            "description": "Leave off for a dry run that only reports.",
                            "x-ms-dynamically-added": True}}}}}},
        "actions": actions, "outputs": {}}},
    "schemaVersion": "1.0.0.0"}

OUT.write_text(json.dumps(flow, indent=2, ensure_ascii=False) + "\n")
print(f"flow 7 written: {len(actions)} top-level actions")
