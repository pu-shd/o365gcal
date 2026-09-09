"""Write the canonical expressions from o365gcal.expressions into the flow JSON.

Run after changing any rule in `expressions.py`:

    .venv/bin/python tools/patch_flows.py && ./scripts/build.sh && make test

The flow JSON stays the committed source of truth and stays round-trippable through
the maker portal; this only keeps the handful of derived expressions in step with the
module the parity tests check.
"""

import json
import sys
import textwrap
from pathlib import Path

#: Power Automate rejects any action description over this length, and only at
#: activation time - so an over-long comment imports cleanly and then blocks the flow
#: from ever starting.
DESCRIPTION_LIMIT = 256


def describe(text: str) -> str:
    """Shorten to the activation limit without cutting mid-word."""
    if len(text) <= DESCRIPTION_LIMIT:
        return text
    return textwrap.shorten(text, width=DESCRIPTION_LIMIT, placeholder="")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from o365gcal import expressions as x  # noqa: E402

W = Path(__file__).resolve().parents[1] / "solution" / "src" / "Workflows"
RECONCILE = next(W.glob("*3-Reconcile*.json"))
CHILD_ID = "9C1F4A20-3B77-4E51-9A3D-5E2B10C7D001"


def payload_expression() -> str:
    """Everything flow 2 needs to apply one occurrence, as a single JSON string.

    Previously only googleEventId was passed, so the child flow had nothing to write:
    every create would have produced an empty Google event.
    """
    fields = [
        ("googleEventId", "coalesce(outputs('Find_Map_Row')?['GoogleEventId'], '')"),
        ("mapItemId", "string(coalesce(outputs('Find_Map_Row')?['Id'], ''))"),
        ("fingerprint", "outputs('Compose_Fingerprint')"),
        ("summary", "outputs('Compose_Subject')"),
        ("start", "item()?['startWithTimeZone']"),
        ("end", "item()?['endWithTimeZone']"),
        ("description", "outputs('Compose_Description')"),
        ("location", f"if({x.HIDDEN}, '', coalesce(item()?['location'], ''))"),
        ("isAllDay", "coalesce(item()?['isAllDay'], false)"),
        ("outlookEventId", "coalesce(item()?['id'], '')"),
        ("outlookICalUId", "coalesce(item()?['iCalUId'], '')"),
        ("seriesMasterId", "coalesce(item()?['seriesMasterId'], '')"),
        ("myResponse", "coalesce(item()?['responseType'], 'none')"),
    ]
    expr = "json('{}')"
    for name, value in fields:
        expr = f"setProperty({expr}, '{name}', {value})"
    return expr


def main() -> int:
    doc = json.loads(RECONCILE.read_text())
    loop = (
        doc["properties"]["definition"]["actions"]["Try_Reconcile"]["actions"]
        ["For_Each_Outlook_Event"]["actions"]
    )

    scope = doc["properties"]["definition"]["actions"]["Try_Reconcile"]["actions"]

    # Six Composes per event became one Select over the whole array. The expressions
    # are the same ones, rewritten by standalone() to stop referencing each other.
    scope["Select_Derived"]["inputs"]["select"] = {
        name: "@" + expr for name, expr in x.derived_event().items()
    }
    scope["Select_Row_Pairs"]["inputs"]["select"] = "@" + x.ROW_PAIR
    scope["Filter_Needs_Work"]["inputs"]["where"] = "@" + x.NEEDS_WORK

    # The payload is built once per event in the Select; the loop adds only the two
    # fields that come from the sync-map row.
    loop["Compose_Payload"] = {
        "runAfter": {"Decide": ["Succeeded"]},
        "type": "Compose",
        "description": "The occurrence handed to child flow 2, which is the only writer, "
                       "plus the two fields that come from the sync-map row.",
        "inputs": ("@setProperty(setProperty(items('For_Each_Outlook_Event')?['payload'], "
                   "'googleEventId', coalesce(outputs('Find_Map_Row')?['GoogleEventId'], '')), "
                   "'mapItemId', string(coalesce(outputs('Find_Map_Row')?['Id'], '')))"),
    }
    loop["Apply_If_Needed"]["runAfter"] = {"Compose_Payload": ["Succeeded"]}

    # The decision no longer consults isCancelled: the calendar view omits cancelled
    # occurrences entirely, so absence from the read is the only cancellation signal.
    loop["Decide"]["inputs"] = (
        "@if(equals(coalesce(outputs('Find_Map_Row')?['GoogleEventId'], ''), ''), 'Create', "
        "if(equals(coalesce(outputs('Find_Map_Row')?['ContentFingerprint'], ''), "
        "items('For_Each_Outlook_Event')?['fingerprint']), 'NoOp', 'Update'))"
    )
    loop["Decide"]["description"] = describe(
        "No row or no Google id -> Create. Fingerprint moved -> Update. Otherwise NoOp, "
        "the common case, costing zero Google calls. Deletion is decided after the "
        "loop from rows absent in this read: the calendar view has no cancellation flag."
    )

    # Decide yields only Create, Update or NoOp; deletions come from rows absent in
    # the read, handled after the loop. There is no delete branch here.
    loop["Apply_If_Needed"]["actions"]["Run_Apply_Event"]["inputs"]["body"] = {
        "text": "@items('For_Each_Outlook_Event')?['key']",
        "text_1": "@outputs('Decide')",
        "text_2": "@{string(outputs('Compose_Payload'))}",
    }

    # Prefix bare expressions with @ where Logic Apps requires it.
    for name in ("Compose_Payload",):
        value = loop[name]["inputs"]
        if isinstance(value, str) and not value.startswith("@"):
            loop[name]["inputs"] = "@" + value

    # The run summary reports elapsed time and raises its own level when the run
    # outlived its cadence, so a degraded reconcile is visible in the log list rather
    # than only inferable from the portal.
    scope = doc["properties"]["definition"]["actions"]["Try_Reconcile"]["actions"]
    scope["Log_Run_Summary"]["inputs"]["parameters"]["parameters/body"] = (
        x.reconcile_summary_body()
    )

    RECONCILE.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print(f"patched {RECONCILE.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
