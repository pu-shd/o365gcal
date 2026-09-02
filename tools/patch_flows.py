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

    loop["Compose_Key"]["inputs"] = x.correlation_key(
        "item()?['iCalUId']", "item()?['startWithTimeZone']"
    )
    loop["Compose_Hidden"]["inputs"] = x.is_hidden("coalesce(item()?['sensitivity'], 'normal')")
    loop["Compose_BodyFingerprint"]["inputs"] = x.body_fingerprint("coalesce(item()?['body'], '')")
    loop["Compose_Subject"]["inputs"] = x.FLOW_FINGERPRINT_PARTS["effectiveSubject"]
    loop["Compose_Fingerprint"]["inputs"] = x.flow_fingerprint()
    loop["Compose_Description"]["inputs"] = x.flow_description()


    loop["Compose_Payload"] = {
        "runAfter": {"Decide": ["Succeeded"]},
        "type": "Compose",
        "description": "The complete occurrence handed to child flow 2, which is the only writer.",
        "inputs": payload_expression(),
    }
    loop["Apply_If_Needed"]["runAfter"] = {"Compose_Payload": ["Succeeded"]}

    # The decision no longer consults isCancelled: the calendar view omits cancelled
    # occurrences entirely, so absence from the read is the only cancellation signal.
    loop["Decide"]["inputs"] = (
        "@if(equals(coalesce(outputs('Find_Map_Row')?['GoogleEventId'], ''), ''), 'Create', "
        "if(equals(coalesce(outputs('Find_Map_Row')?['ContentFingerprint'], ''), "
        "outputs('Compose_Fingerprint')), 'NoOp', 'Update'))"
    )
    loop["Decide"]["description"] = describe(
        "No row or no Google id -> Create. Fingerprint moved -> Update. Otherwise NoOp, "
        "the common case, costing zero Google calls. Deletion is decided after the "
        "loop from rows absent in this read: the calendar view has no cancellation flag."
    )

    # Decide yields only Create, Update or NoOp; deletions come from rows absent in
    # the read, handled after the loop. There is no delete branch here.
    loop["Apply_If_Needed"]["actions"]["Run_Apply_Event"]["inputs"]["body"] = {
        "text": "@outputs('Compose_Key')",
        "text_1": "@outputs('Decide')",
        "text_2": "@{string(outputs('Compose_Payload'))}",
    }

    # Prefix bare expressions with @ where Logic Apps requires it.
    for name in ("Compose_Key", "Compose_Hidden", "Compose_BodyFingerprint",
                 "Compose_Subject", "Compose_Fingerprint", "Compose_Description",
                 "Compose_Payload"):
        value = loop[name]["inputs"]
        if isinstance(value, str) and not value.startswith("@"):
            loop[name]["inputs"] = "@" + value

    RECONCILE.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print(f"patched {RECONCILE.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
