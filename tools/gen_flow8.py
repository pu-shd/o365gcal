"""Generate flow 8: Invitation Reminder.

A dedicated reminder for outstanding RSVPs, separate from the daily digest so it can
run on its own cadence and be read on its own. Grouped by series: the digest that
prompted this listed seventeen lines for two recurring meetings.

    .venv/bin/python tools/gen_flow8.py && ./scripts/build.sh
"""

import json
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "solution/src/Workflows/O365GCal-8-Invitations-9C1F4A20-3B77-4E51-9A3D-5E2B10C7D009.json"
O3_API = "/providers/Microsoft.PowerApps/apis/shared_office365"
SP_API = "/providers/Microsoft.PowerApps/apis/shared_sharepointonline"


def d(t, n=256):
    return t if len(t) <= n else textwrap.shorten(t, width=n, placeholder="")


def E(n):
    return f"parameters('{n} (o3gc_{n})')"


ENV = [("OutlookCalendarId", "Calendar", "String"), ("AlertEmail", "", "String"),
       ("StateSiteUrl", "", "String"), ("HealthListName", "O365GCalHealth", "String"),
       ("RsvpReminderDays", 3, "Int"), ("RsvpReminderHour", 8, "Int"),
       ("RsvpHorizonDays", 60, "Int")]
PARAMS = {f"{n} (o3gc_{n})": {"defaultValue": v, "type": t,
                              "metadata": {"schemaName": f"o3gc_{n}"}}
          for n, v, t in ENV}

A = {}
prev = None


def add(name, action, after=None, states=("Succeeded",)):
    global prev
    action["runAfter"] = {} if after is None else {after: list(states)}
    A[name] = action
    prev = name
    return name


# InitializeVariable has to sit at the top level: nesting one inside a condition or a
# scope is rejected at activation with InvalidVariableInitialization. So the variable
# is declared here and only assigned inside the send branch.
add("Init_Groups", {
    "type": "InitializeVariable",
    "description": d("Declared at the top level because InitializeVariable cannot be "
                     "nested inside a condition."),
    "inputs": {"variables": [{"name": "Groups", "type": "array", "value": []}]}})

# Runs hourly and decides for itself whether today is a send day. A Recurrence
# interval cannot be driven by an environment variable, so the cadence has to be a
# setting the flow reads rather than a schedule it is given.
add("Check_Send_Window", {
    "type": "If",
    "description": d("Hourly trigger, but sends only at the configured hour and only "
                     "every RsvpReminderDays. Recurrence intervals cannot be driven by "
                     "a setting, so the cadence is decided here instead."),
    "expression": {"and": [
        {"greater": [f"@int({E('RsvpReminderDays')})", 0]},
        {"equals": ["@int(formatDateTime(utcNow(), 'HH'))", f"@int({E('RsvpReminderHour')})"]},
        {"equals": [
            "@mod(int(formatDateTime(utcNow(), 'dd')), int(" + E("RsvpReminderDays") + "))",
            0]},
    ]},
    "actions": {},
    "else": {"actions": {}},
}, prev)

INNER = A["Check_Send_Window"]["actions"]

INNER["Get_Upcoming"] = {
    "runAfter": {}, "type": "OpenApiConnection",
    "description": d("Read live from Outlook. An RSVP still owed is a current fact "
                     "about the calendar, not something to infer from the log."),
    "inputs": {"host": {"connectionName": "shared_office365",
                        "operationId": "GetEventsCalendarViewV3", "apiId": O3_API},
               "parameters": {
                   "calendarId": "@" + E("OutlookCalendarId"),
                   "startDateTimeUtc": "@{formatDateTime(utcNow(), 'yyyy-MM-ddTHH:mm:ss')}",
                   "endDateTimeUtc": "@{formatDateTime(addDays(utcNow(), int(" + E("RsvpHorizonDays") + ")), 'yyyy-MM-ddTHH:mm:ss')}",
                   "$orderby": "start/dateTime", "$top": 250}},
    "operationOptions": "DisableAsyncPattern"}

INNER["Filter_Outstanding"] = {
    "runAfter": {"Get_Upcoming": ["Succeeded"]}, "type": "Query",
    "description": d("Tentative counts as outstanding: it is not an answer the "
                     "organiser can plan around."),
    "inputs": {"from": "@coalesce(body('Get_Upcoming')?['value'], json('[]'))",
               "where": "@contains(createArray('notResponded','tentativelyAccepted'), coalesce(item()?['responseType'], 'none'))"}}

# Grouping key per occurrence: the series master if there is one, else the iCalUId.
INNER["Select_Keys"] = {
    "runAfter": {"Filter_Outstanding": ["Succeeded"]}, "type": "Select",
    "inputs": {"from": "@body('Filter_Outstanding')",
               "select": "@coalesce(item()?['seriesMasterId'], item()?['iCalUId'])"}}

INNER["Compose_Unique"] = {
    "runAfter": {"Select_Keys": ["Succeeded"]}, "type": "Compose",
    "description": d("union with itself is the only way to get distinct values in this "
                     "expression language."),
    "inputs": "@union(body('Select_Keys'), body('Select_Keys'))"}

INNER["For_Each_Group"] = {
    "runAfter": {"Compose_Unique": ["Succeeded"]}, "type": "Foreach",
    "description": d("One entry per meeting or series, reporting the soonest occurrence "
                     "and how many are outstanding. Replying in Outlook answers the "
                     "whole series at once."),
    "foreach": "@outputs('Compose_Unique')",
    "runtimeConfiguration": {"concurrency": {"repetitions": 1}},
    "actions": {
        "Filter_Members": {"runAfter": {}, "type": "Query",
            "inputs": {"from": "@body('Filter_Outstanding')",
                       "where": "@equals(coalesce(item()?['seriesMasterId'], item()?['iCalUId']), items('For_Each_Group'))"}},
        "Compose_Soonest": {"runAfter": {"Filter_Members": ["Succeeded"]}, "type": "Compose",
            "description": d("Graph does not guarantee ordering, so the soonest is "
                             "chosen explicitly rather than taken as first."),
            "inputs": "@first(sort(body('Filter_Members'), 'startWithTimeZone'))"},
        "Append_Group": {"runAfter": {"Compose_Soonest": ["Succeeded"]},
            "type": "AppendToArrayVariable",
            "inputs": {"name": "Groups", "value": {
                "subject": "@coalesce(outputs('Compose_Soonest')?['subject'], '(no subject)')",
                "nextStart": "@outputs('Compose_Soonest')?['startWithTimeZone']",
                "count": "@length(body('Filter_Members'))",
                "response": "@coalesce(outputs('Compose_Soonest')?['responseType'], '')",
                "organizer": "@coalesce(outputs('Compose_Soonest')?['organizer'], '')",
                "webLink": "@coalesce(outputs('Compose_Soonest')?['webLink'], '')"}}}}}

INNER["Sort_Groups"] = {
    "runAfter": {"For_Each_Group": ["Succeeded"]}, "type": "Compose",
    "description": d("Soonest first: attention should land on what is most imminent."),
    "inputs": "@sort(variables('Groups'), 'nextStart')"}

# One card per meeting. Inline styles because email clients discard <style> blocks.
CARD = (
    "@concat("
    "'<tr><td style=\"padding:14px 16px;border-bottom:1px solid #e8e8e8\">',"
    "'<div style=\"font:600 15px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;color:#1a1a1a\">',"
    "coalesce(item()?['subject'], '(no subject)'),"
    "'</div>',"
    "'<div style=\"font:13px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;color:#666;margin-top:3px\">',"
    "'Next: ', formatDateTime(item()?['nextStart'], 'ddd d MMM, HH:mm'), ' UTC',"
    "if(greater(item()?['count'], 1), concat(' &middot; ', string(item()?['count']), ' occurrences awaiting reply'), ''),"
    "if(equals(item()?['response'], 'tentativelyAccepted'), ' &middot; currently tentative', ''),"
    "'</div>',"
    "if(empty(coalesce(item()?['organizer'], '')), '',"
    "  concat('<div style=\"font:13px/1.6 -apple-system,Segoe UI,Roboto,sans-serif;color:#888\">from ', item()?['organizer'], '</div>')),"
    # One literal, deliberately. Splitting it across adjacent Python strings put a
    # '' at the join, which WDL reads as an escaped single quote - the result was a
    # stray apostrophe inside the <a> tag. The malformed attribute made the mail
    # client's sanitiser discard the rest of the table, so a two-card email rendered
    # as one.
    "'<div style=\"margin-top:8px\"><a href=\"', coalesce(item()?['webLink'], ''), "
    "'\" style=\"display:inline-block;padding:7px 14px;background:#0b5cab;color:#fff;text-decoration:none;border-radius:4px;font:600 13px -apple-system,Segoe UI,Roboto,sans-serif\">Reply in Outlook</a></div>',"
    "'</td></tr>')"
)

INNER["Select_Cards"] = {
    "runAfter": {"Sort_Groups": ["Succeeded"]}, "type": "Select",
    "inputs": {"from": "@outputs('Sort_Groups')", "select": CARD}}

BODY = (
    "<div style=\"background:#f4f5f7;padding:24px 12px;font-family:-apple-system,Segoe UI,Roboto,sans-serif\">"
    "<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" "
    "style=\"max-width:560px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;"
    "box-shadow:0 1px 3px rgba(0,0,0,.08)\">"
    "<tr><td style=\"padding:20px 16px 14px\">"
    "<div style=\"font:600 17px/1.3 -apple-system,Segoe UI,Roboto,sans-serif;color:#1a1a1a\">"
    "@{length(outputs('Sort_Groups'))} @{if(equals(length(outputs('Sort_Groups')), 1), 'invitation', 'invitations')} awaiting your reply</div>"
    "<div style=\"font:13px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#666;margin-top:5px\">"
    "These are on your Google calendar, but the organiser has not had an answer. "
    "Google cannot show this &mdash; only Outlook knows.</div>"
    "</td></tr>"
    "@{join(body('Select_Cards'), '')}"
    "<tr><td style=\"padding:12px 16px;background:#fafafa\">"
    "<div style=\"font:11px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#999\">"
    "Recurring meetings are shown once; replying answers the whole series. "
    "Only invitations in the next @{parameters('RsvpHorizonDays (o3gc_RsvpHorizonDays)')} days are listed. "
    "Change the frequency with: configure.sh rsvpdays &lt;n&gt;, or 0 to stop these.</div>"
    "</td></tr></table></div>"
)

INNER["Check_Any_Outstanding"] = {
    "runAfter": {"Select_Cards": ["Succeeded"]}, "type": "If",
    "description": d("Nothing outstanding means no email. A reminder that arrives to "
                     "say there is nothing to do trains people to ignore it."),
    "expression": {"greater": ["@length(outputs('Sort_Groups'))", 0]},
    "actions": {"Send_Reminder": {
        "runAfter": {}, "type": "OpenApiConnection",
        "inputs": {"host": {"connectionName": "shared_office365",
                            "operationId": "SendEmailV2", "apiId": O3_API},
                   "parameters": {
                       "emailMessage/To": "@" + E("AlertEmail"),
                       "emailMessage/Subject": (
                           "@{concat(string(length(outputs('Sort_Groups'))), "
                           "if(equals(length(outputs('Sort_Groups')), 1), "
                           "' invitation needs your reply', ' invitations need your reply'), "
                           "' - soonest ', first(outputs('Sort_Groups'))?['subject'])}"),
                       "emailMessage/Body": BODY,
                       "emailMessage/Importance": "Normal"}}}},
    "else": {"actions": {}}}

INNER["Get_Health_Row"] = {
    "runAfter": {"Check_Any_Outstanding": ["Succeeded"]}, "type": "OpenApiConnection",
    "inputs": {"host": {"connectionName": "shared_sharepointonline",
                        "operationId": "HttpRequest", "apiId": SP_API},
               "parameters": {"dataset": "@" + E("StateSiteUrl"),
                              "parameters/method": "GET",
                              "parameters/uri": "@{concat('_api/web/lists/getbytitle(''', " + E("HealthListName") + ", ''')/items?$select=Id&$filter=Title eq ''8 Invitation Reminder''&$top=1')}",
                              "parameters/headers": {"Accept": "application/json;odata=nometadata"}}}}

INNER["Stamp_Health"] = {
    "runAfter": {"Get_Health_Row": ["Succeeded"]}, "type": "OpenApiConnection",
    "inputs": {"host": {"connectionName": "shared_sharepointonline",
                        "operationId": "HttpRequest", "apiId": SP_API},
               "parameters": {"dataset": "@" + E("StateSiteUrl"),
                              "parameters/method": "POST",
                              "parameters/uri": "@{concat('_api/web/lists/getbytitle(''', " + E("HealthListName") + ", ''')/items(', string(first(body('Get_Health_Row')?['value'])?['Id']), ')')}",
                              "parameters/headers": {"Accept": "application/json;odata=nometadata",
                                                     "Content-Type": "application/json;odata=nometadata",
                                                     "X-HTTP-Method": "MERGE", "IF-MATCH": "*"},
                              "parameters/body": "@setProperty(setProperty(setProperty(json('{}'), 'LastSuccessUtc', utcNow()), 'LastRunUtc', utcNow()), 'LastRunStatus', 'Succeeded')"}}}

flow = {"properties": {
    "connectionReferences": {
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
            "Reminds you which meeting invitations still need a reply, on its own "
            "schedule and grouped by meeting rather than by occurrence. This is the one "
            "thing a Google-only user cannot discover for themselves: Google shows the "
            "meeting but never that the organiser is still waiting. Triggered hourly "
            "and decides internally whether to send, because a Recurrence interval "
            "cannot be driven by a setting."),
        "parameters": {"$connections": {"defaultValue": {}, "type": "Object"},
                       "$authentication": {"defaultValue": {}, "type": "SecureObject"},
                       **PARAMS},
        "triggers": {"Every_hour": {"type": "Recurrence",
            "recurrence": {"frequency": "Hour", "interval": 1},
            "metadata": {"operationMetadataId": "9c1f4a20-3b77-4e51-9a3d-000000000009"}}},
        "actions": A, "outputs": {}}},
    "schemaVersion": "1.0.0.0"}

OUT.write_text(json.dumps(flow, indent=2, ensure_ascii=False) + "\n")
print(f"flow 8 written: {len(A)} top-level, {len(INNER)} inner actions")
