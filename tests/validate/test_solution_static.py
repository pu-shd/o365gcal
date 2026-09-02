"""Static validation of the packaged solution.

These checks stand in for the things that would otherwise only fail at import time
or, worse, silently at runtime in someone else's tenant.
"""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "solution" / "src"
WORKFLOWS = sorted((SRC / "Workflows").glob("*.json"))
EVD = SRC / "environmentvariabledefinitions"

#: The only connectors this solution is allowed to touch. Everything here is Standard;
#: adding anything else would quietly impose a Power Automate Premium licence on every
#: person the solution is handed to, which is the entire reason for the design.
ALLOWED_CONNECTORS = {"shared_office365", "shared_googlecalendar", "shared_sharepointonline"}

#: Connectors that are easy to reach for and would break the Standard-only promise.
PREMIUM_TRAPS = {
    "shared_commondataserviceforapps": "Dataverse",
    "shared_webcontents": "HTTP with Microsoft Entra ID",
    "shared_logicflows": "Logic Apps",
    "shared_sql": "SQL Server",
    "shared_azureblob": "Azure Blob Storage",
    "shared_outlook": "Outlook.com personal (wrong connector - use shared_office365)",
}


CUSTOMIZATIONS = ET.fromstring((SRC / "Other" / "Customizations.xml").read_text())


def workflow_meta(name_fragment: str):
    """Cloud-flow metadata lives in Customizations.xml under <Workflows>, not in
    separate .meta.xml files. Solution Packager silently omits every flow whose
    metadata is not declared there - it still logs the flow as 'processed' - so the
    whole solution packs cleanly with no flows in it. test_packed_zip_contains_flows
    is the backstop for that."""
    for w in CUSTOMIZATIONS.findall(".//Workflows/Workflow"):
        if name_fragment in (w.findtext("JsonFileName") or ""):
            return w
    raise AssertionError(f"no <Workflow> declared for {name_fragment}")


def load(path):
    return json.loads(path.read_text())


def definition(path):
    return load(path)["properties"]["definition"]


def host_of(action):
    """Connector host block, or {} for Compose and other actions whose `inputs` is a
    bare string rather than an object."""
    inputs = action.get("inputs")
    return inputs.get("host", {}) if isinstance(inputs, dict) else {}


def walk_actions(actions):
    """Yield every action at every nesting depth: scopes, conditions, loops, switches."""
    for name, action in (actions or {}).items():
        yield name, action
        for key in ("actions", "else"):
            block = action.get(key)
            if isinstance(block, dict):
                yield from walk_actions(block.get("actions", block))
        for case in (action.get("cases") or {}).values():
            yield from walk_actions(case.get("actions", {}))
        if isinstance(action.get("default"), dict):
            yield from walk_actions(action["default"].get("actions", {}))


#: 0 Setup, 1 Sync trigger, 2 Apply Event (child), 3 Reconcile, 4 Digest,
#: 5 Watchdog, 6 Backup State, 7 Dedup and Repair.
EXPECTED_FLOWS = 8


def test_all_flows_present():
    assert len(WORKFLOWS) == EXPECTED_FLOWS, [w.name for w in WORKFLOWS]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name.split("-")[1])
def test_valid_workflow_shape(path):
    d = definition(path)
    assert d["$schema"].endswith("workflowdefinition.json#")
    assert d["triggers"], "a flow with no trigger can never run"
    assert d["actions"], "a flow with no actions does nothing"
    assert d.get("description"), "every flow must say what it is for"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name.split("-")[1])
def test_no_premium_connectors(path):
    """The guard that keeps the Standard-only promise true as the solution evolves."""
    raw = path.read_text()
    for api, label in PREMIUM_TRAPS.items():
        assert f'"{api}"' not in raw, f"{path.name} references {api} ({label})"
    for ref in load(path)["properties"].get("connectionReferences", {}):
        assert ref in ALLOWED_CONNECTORS, f"{path.name} uses unapproved connector {ref}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name.split("-")[1])
def test_connection_references_are_declared(path):
    """An action naming a connection the flow never declared fails only at import."""
    doc = load(path)
    declared = set(doc["properties"].get("connectionReferences", {}))
    for name, action in walk_actions(definition(path)["actions"]):
        conn = host_of(action).get("connectionName")
        if conn:
            assert conn in declared, f"{path.name}:{name} uses undeclared connection {conn}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name.split("-")[1])
def test_runafter_targets_exist(path):
    """A runAfter pointing at a renamed action leaves that branch permanently skipped -
    which looks like a working flow that quietly does nothing."""
    def check(actions, scope):
        names = set(actions)
        for name, action in actions.items():
            for target in (action.get("runAfter") or {}):
                assert target in names, f"{path.name}: {scope}/{name} runAfter unknown '{target}'"
            for key in ("actions", "else"):
                block = action.get(key)
                if isinstance(block, dict):
                    check(block.get("actions", block), f"{scope}/{name}")
            for cname, case in (action.get("cases") or {}).items():
                check(case.get("actions", {}), f"{scope}/{name}/{cname}")
            if isinstance(action.get("default"), dict):
                check(action["default"].get("actions", {}), f"{scope}/{name}/default")

    check(definition(path)["actions"], path.stem)


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name.split("-")[1])
def test_environment_variables_are_declared_and_defined(path):
    """A typo'd env var reference evaluates to null at runtime and silently mirrors
    into the wrong calendar, so it has to be caught here."""
    d = definition(path)
    declared = set(d["parameters"])
    on_disk = {p.name for p in EVD.iterdir() if p.is_dir()}

    for ref in set(re.findall(r"parameters\('([^']+)'\)", path.read_text())):
        if ref in ("$connections", "$authentication"):
            continue
        assert ref in declared, f"{path.name} references undeclared parameter {ref!r}"
        schema = re.match(r".+ \((o3gc_\w+)\)$", ref)
        assert schema, f"{path.name}: {ref!r} is not in 'Name (o3gc_Name)' form"
        assert schema.group(1) in on_disk, f"{path.name}: no definition for {schema.group(1)}"


def test_every_environment_variable_is_used():
    """An unused variable is either dead config or a wiring mistake; both mislead."""
    used = set()
    for path in WORKFLOWS:
        used |= {m for m in re.findall(r"\(o3gc_(\w+)\)", path.read_text())}
    on_disk = {p.name[len("o3gc_"):] for p in EVD.iterdir() if p.is_dir()}
    assert on_disk - used == set(), f"declared but never used: {sorted(on_disk - used)}"


def test_child_flow_is_a_subprocess_and_activated():
    """'Run a Child Flow' only resolves a callee marked as a subprocess, and a child
    left in draft cannot be invoked at all."""
    w = workflow_meta("ApplyEvent")
    assert w.findtext("Subprocess") == "1"
    assert w.findtext("StateCode") == "1", "child flow must ship activated"


@pytest.mark.parametrize(
    "stem,subprocess",
    [("0-Setup", "0"), ("1-SyncTrigger", "0"), ("3-Reconcile", "0"),
     ("4-Digest", "0"), ("5-Watchdog", "0"), ("6-Backup", "0"), ("7-Dedup", "0")],
)
def test_parent_flows_are_not_subprocesses(stem, subprocess):
    assert workflow_meta(stem).findtext("Subprocess") == subprocess


@pytest.mark.parametrize("stem", ["1-SyncTrigger", "3-Reconcile", "4-Digest",
                                  "5-Watchdog", "6-Backup", "7-Dedup"])
def test_scheduled_flows_ship_switched_off(stem):
    """Nothing may write to a real calendar before the installer has set the
    environment variables and run Setup."""
    assert workflow_meta(stem).findtext("StateCode") == "0"


def all_workflow_ids():
    return {
        w.get("WorkflowId").strip("{}").lower()
        for w in CUSTOMIZATIONS.findall(".//Workflows/Workflow")
    }


def test_child_flow_ids_referenced_by_parents_exist():
    ids = all_workflow_ids()
    for path in WORKFLOWS:
        for name, action in walk_actions(definition(path)["actions"]):
            if action.get("type") == "Workflow":
                ref = action["inputs"]["host"]["workflowReferenceName"].strip("{}").lower()
                assert ref in ids, f"{path.name}:{name} calls unknown flow {ref}"


def test_root_components_cover_every_component():
    sol = ET.fromstring((SRC / "Other" / "Solution.xml").read_text())
    roots = sol.find(".//RootComponents")
    workflows = {r.get("id").strip("{}").lower() for r in roots if r.get("type") == "29"}
    envvars = {r.get("schemaName") for r in roots if r.get("type") == "380"}

    on_disk_flows = all_workflow_ids()
    on_disk_vars = {p.name for p in EVD.iterdir() if p.is_dir()}

    assert workflows == on_disk_flows, "a flow not in RootComponents is not exported"
    assert envvars == on_disk_vars, "an env var not in RootComponents is not exported"


def test_every_flow_json_is_declared_in_customizations():
    """A .json with no <Workflow> entry is dead weight the packager silently ignores."""
    declared = {
        (w.findtext("JsonFileName") or "").rsplit("/", 1)[-1]
        for w in CUSTOMIZATIONS.findall(".//Workflows/Workflow")
    }
    on_disk = {p.name for p in WORKFLOWS}
    assert declared == on_disk, f"declared={sorted(declared)} on_disk={sorted(on_disk)}"


def test_packed_zip_contains_every_flow():
    """The backstop for the failure mode that actually bit during development: the
    solution packed successfully, reported all six flows as processed, and produced a
    zip containing nothing but environment variables."""
    import zipfile

    zip_path = ROOT / "dist" / "O365GCal_managed.zip"
    if not zip_path.exists():
        pytest.skip("run ./scripts/build.sh first")
    names = zipfile.ZipFile(zip_path).namelist()
    packed = [n for n in names if n.startswith("Workflows/") and n.endswith(".json")]
    assert len(packed) == EXPECTED_FLOWS, f"only {len(packed)} flow(s) packed: {packed}"


def test_connection_references_declared_in_customizations():
    xml = (SRC / "Other" / "Customizations.xml").read_text()
    for logical, api in [
        ("o3gc_sharedoffice365", "shared_office365"),
        ("o3gc_sharedgooglecalendar", "shared_googlecalendar"),
        ("o3gc_sharedsharepointonline", "shared_sharepointonline"),
    ]:
        assert logical in xml and api in xml


def test_google_writes_only_happen_in_the_child_flow():
    """Centralising CRUD is what makes the trigger path and the reconcile path safe to
    run concurrently. A second writer would reintroduce duplicate events.

    Flow 7 is the one deliberate exception. It is a repair tool that operates on Google
    state directly - it exists precisely because the sync map is untrustworthy, so it
    cannot route through the child flow, which reads that map to decide what to do. It
    only ever deletes, never creates, and only inside its Apply branch."""
    writers = {"CreateEvent", "UpdateEvent", "DeleteEvent"}
    for path in WORKFLOWS:
        for name, action in walk_actions(definition(path)["actions"]):
            op = host_of(action).get("operationId")
            if op not in writers:
                continue
            if "7-Dedup" in path.name:
                assert op == "DeleteEvent", (
                    f"{path.name}:{name} may only delete; creating would defeat the child "
                    f"flow's single-writer guarantee"
                )
                continue
            assert "ApplyEvent" in path.name, (
                f"{path.name}:{name} writes to Google directly; all CRUD belongs in flow 2"
            )


def test_reconcile_loops_are_sequential():
    """Concurrent iterations would race the Applied counter and overrun the Google
    throttle cap the counter exists to enforce."""
    path = next(p for p in WORKFLOWS if "Reconcile" in p.name)
    loops = [(n, a) for n, a in walk_actions(definition(path)["actions"]) if a.get("type") == "Foreach"]
    assert loops
    for name, action in loops:
        rep = action.get("runtimeConfiguration", {}).get("concurrency", {}).get("repetitions")
        assert rep == 1, f"{name} must be sequential, got concurrency {rep}"


# ---------------------------------------------------------------------------
# Response-field validation.
#
# The connector contract tests check the parameters a flow *sends*. Nothing checked
# the fields it *reads back*, and three of them did not exist: `bodyPreview` and
# `isCancelled` are not on GraphCalendarEventClientReceive at all (they belong to mail
# types and the deprecated V2 calendar backend), and `start`/`end` are `date-no-tz`
# wall-clock strings that would shift every event for a non-UTC mailbox.
# A missing field reads as null and fails silently, which is the worst kind.
# ---------------------------------------------------------------------------

CALENDAR_EVENT_FIELDS = None
SWAGGER = ROOT / "connectors" / "shared_office365.json"


def _calendar_event_fields():
    global CALENDAR_EVENT_FIELDS
    if CALENDAR_EVENT_FIELDS is None:
        spec = json.loads(SWAGGER.read_text())
        spec = spec.get("properties", {}).get("swagger", spec)
        CALENDAR_EVENT_FIELDS = set(
            spec["definitions"]["GraphCalendarEventClientReceive"]["properties"]
        )
    return CALENDAR_EVENT_FIELDS


#: Google's ResponseEvent, iterated by flow 7. A different API with different names,
#: so it cannot be checked against the Outlook definition.
GOOGLE_EVENT_FIELDS = {
    "attendees", "created", "creator", "description", "end", "endTimeUnspecified",
    "htmlLink", "id", "location", "organizer", "start", "status", "summary", "updated",
}

#: Fields that come from somewhere other than a calendar-view item: the trigger's own
#: payload, or SharePoint rows read in the same loops.
NON_CALENDAR_FIELDS = {
    "actionType", "value",
    "Id", "Title", "GoogleEventId", "ContentFingerprint", "SyncState",
    "OccurrenceStartUtc", "OutlookEventId", "LastSuccessUtc", "LastRunStatus",
    "ConsecutiveFailures", "Timestamp", "FlowName", "Level", "Operation",
    "CorrelationKey", "Message", "DetailJson",
    "key", "googleEventId", "mapItemId", "reason",
    "id", "htmlLink", "items", "summary",
    # Health-row fields, and the seed literal flow 0 iterates.
    "StaleAfterMinutes", "AffectsSync", "name", "stale", "affects",
}

#: Banned outright, with the reason, so the failure explains itself.
FORBIDDEN_FIELDS = {
    "bodyPreview": "not on GraphCalendarEventClientReceive - use 'body' with 'isHtml'",
    "isCancelled": "not on GraphCalendarEventClientReceive - cancellation is detected "
                   "by absence from the calendar view",
    "start": "date-no-tz wall-clock string - use 'startWithTimeZone'",
    "end": "date-no-tz wall-clock string - use 'endWithTimeZone'",
}


@pytest.mark.skipif(not SWAGGER.exists(), reason="run ./scripts/fetch-connector-swagger.sh")
@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name.split("-")[1])
def test_no_forbidden_event_fields(path):
    text = path.read_text()
    for field, why in FORBIDDEN_FIELDS.items():
        assert f"item()?['{field}']" not in text, f"{path.name} reads item()?['{field}']: {why}"


@pytest.mark.skipif(not SWAGGER.exists(), reason="run ./scripts/fetch-connector-swagger.sh")
@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name.split("-")[1])
def test_event_fields_exist_on_the_connector(path):
    known = _calendar_event_fields() | NON_CALENDAR_FIELDS | GOOGLE_EVENT_FIELDS
    used = set(re.findall(r"item\(\)\?\['([A-Za-z_][A-Za-z0-9_]*)'\]", path.read_text()))
    unknown = used - known
    assert not unknown, (
        f"{path.name} reads field(s) the connector does not return: {sorted(unknown)}. "
        f"A missing field evaluates to null and fails silently."
    )


# ---------------------------------------------------------------------------
# Environment variable definitions.
#
# These were originally hand-written with `<displayname default="..."/>` attributes.
# The importer rejected the whole solution with nothing more useful than "the
# specified node is the wrong type", and because the failure happens while parsing
# customizations.xml it names no file. The definitions are now generated from
# o365gcal.envvars; these checks keep the generated output and the catalogue in step.
# ---------------------------------------------------------------------------

def _envvar_catalogue():
    from o365gcal.envvars import CATALOGUE

    return CATALOGUE


def test_definitions_match_the_catalogue():
    """Fails if someone edits the XML by hand instead of the catalogue."""
    from o365gcal.envvars import definition_xml

    for var in _envvar_catalogue():
        path = EVD / var.schema_name / "environmentvariabledefinition.xml"
        assert path.exists(), f"missing definition for {var.schema_name}"
        assert path.read_text() == definition_xml(var), (
            f"{var.schema_name} is out of step with the catalogue; "
            f"run: python tools/gen_envvars.py"
        )


def test_no_orphan_definitions_on_disk():
    known = {v.schema_name for v in _envvar_catalogue()}
    on_disk = {p.name for p in EVD.iterdir() if p.is_dir()}
    assert on_disk == known, f"orphaned: {sorted(on_disk - known)}"


@pytest.mark.parametrize("var", _envvar_catalogue(), ids=lambda v: v.name)
def test_definition_uses_element_text_not_attributes(var):
    """The exact shape that broke the first import."""
    root = ET.fromstring((EVD / var.schema_name / "environmentvariabledefinition.xml").read_text())
    assert root.tag == "environmentvariabledefinition"
    assert root.get("schemaname") == var.schema_name
    assert root.get("environmentvariabledefinitionid"), "importer needs a stable id"
    for child in ("displayname", "type", "introducedversion"):
        node = root.find(child)
        assert node is not None, f"{var.schema_name} missing <{child}>"
        assert not node.attrib, f"<{child}> must carry text, not a 'default' attribute"
    assert root.findtext("displayname") == var.name


@pytest.mark.parametrize("var", _envvar_catalogue(), ids=lambda v: v.name)
def test_definition_ids_are_deterministic(var):
    """Regenerating must not churn ids, or every upgrade reads as a delete-and-recreate
    of all settings and silently discards the values people configured."""
    from o365gcal.envvars import EnvVar

    twin = EnvVar(var.name, var.type, var.default, var.description)
    assert twin.definition_id == var.definition_id


@pytest.mark.parametrize("var", _envvar_catalogue(), ids=lambda v: v.name)
def test_boolean_defaults_are_yes_or_no(var):
    from o365gcal.envvars import BOOLEAN

    if var.type == BOOLEAN:
        assert var.default in ("yes", "no"), (
            f"{var.name}: boolean definitions take yes/no, not {var.default!r}"
        )


@pytest.mark.parametrize("path", sorted(EVD.glob("*/environmentvariabledefinition.xml")),
                         ids=lambda p: p.parent.name)
def test_definition_has_no_xml_declaration(path):
    """The single most expensive bug in this project to date.

    The importer splices each definition file's root node directly into
    customizations.xml. An `<?xml ?>` declaration cannot be a child node, so including
    one fails the *entire* solution import with only "the specified node cannot be
    inserted as the valid child of this node, because the specified node is the wrong
    type" -- no file name, no line number, no component name. Every other component
    imports fine, which makes it look like a workflow problem.
    """
    assert not path.read_text().lstrip().startswith("<?xml"), (
        "environmentvariabledefinition.xml must not begin with an XML declaration"
    )


@pytest.mark.parametrize("var", _envvar_catalogue(), ids=lambda v: v.name)
def test_every_definition_declares_a_default(var):
    """Every variable needs a <defaultvalue>, empty or not.

    Omitting it for empty defaults breaks activation: a variable with neither a value
    nor a default cannot be referenced, and every flow reading it fails with
    XrmEnvironmentVariableAttributeNotFound. The failure shows up only when a flow is
    switched on, long after a clean import."""
    xml = (EVD / var.schema_name / "environmentvariabledefinition.xml").read_text()
    assert "<defaultvalue>" in xml, f"{var.name} must declare <defaultvalue>"
    assert f"<defaultvalue>{var.default}</defaultvalue>" in xml
    assert var.default != "" or var.required_at_install, (
        f"{var.name}: a variable with an empty default and no required flag cannot be "
        f"resolved at all - flows referencing it fail to activate"
    )


def test_optional_variables_are_read_through_coalesce():
    """A genuinely optional variable evaluates to null until someone sets it, and
    passing null into concat fails the action at runtime.

    Variables marked required_at_install are excluded: they are read as whole
    parameter values (a site URL, an email recipient), where a null must fail loudly
    rather than be coalesced into an empty string that produces a baffling error
    further downstream."""
    from o365gcal.envvars import CATALOGUE

    optional = {v.name for v in CATALOGUE if v.optional}
    for path in WORKFLOWS:
        text = path.read_text()
        for name in optional:
            ref = f"parameters('{name} (o3gc_{name})')"
            for idx in (i for i in range(len(text)) if text.startswith(ref, i)):
                window = text[max(0, idx - 40):idx]
                assert "coalesce(" in window, (
                    f"{path.name} reads optional variable {name} without coalesce; "
                    f"it is null until someone sets it"
                )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name.split("-")[1])
def test_no_unnamed_static_results(path):
    """A `staticResult` block must reference a named definition under the workflow's
    `staticResults`. An unnamed one is accepted at import and then fails activation
    with InvalidStaticResultName, so the solution installs and the flows will not
    start - which reads as a permissions problem rather than a malformed definition."""
    declared = set((definition(path).get("staticResults") or {}))
    for name, action in walk_actions(definition(path)["actions"]):
        block = (action.get("runtimeConfiguration") or {}).get("staticResult")
        if block is None:
            continue
        ref = block.get("name")
        assert ref, f"{path.name}:{name} has a staticResult with no name"
        assert ref in declared, f"{path.name}:{name} references undeclared staticResult {ref}"


def test_child_flow_has_a_response_action():
    """Power Automate refuses to activate any parent that calls a child flow lacking a
    response action (ChildFlowMissingResponseOperation). The solution imports fine and
    then no parent will start, which reads as a permissions problem."""
    path = next(p for p in WORKFLOWS if "ApplyEvent" in p.name)
    responses = [
        (n, a) for n, a in walk_actions(definition(path)["actions"])
        if a.get("type") == "Response"
    ]
    assert responses, "the child flow must contain a Response action"
    name, action = responses[0]
    after = action.get("runAfter", {})
    assert after, f"{name} must run after something"
    for target, states in after.items():
        assert "Failed" in states, (
            f"{name} must also respond when {target} fails, or a single bad event "
            f"faults the caller and stalls the batch"
        )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name.split("-")[1])
def test_result_only_references_scopes(path):
    """`result()` accepts only scope-type actions. Pointing it at a connector action
    passes import and fails template validation at activation."""
    import re

    d = definition(path)
    scopes = {n for n, a in walk_actions(d["actions"]) if a.get("type") == "Scope"}
    for ref in set(re.findall(r"result\('([^']+)'\)", path.read_text())):
        assert ref in scopes, (
            f"{path.name}: result('{ref}') targets a non-scope action; wrap it in a Scope"
        )


ACTION_DESCRIPTION_LIMIT = 256


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name.split("-")[1])
def test_action_descriptions_within_limit(path):
    """Power Automate caps action descriptions at 256 characters and enforces it only
    at activation, so an over-long comment imports fine and then blocks the flow from
    ever starting. The workflow-level description has no such limit, which is where
    the longer reasoning belongs."""
    d = definition(path)
    items = list(walk_actions(d["actions"])) + list(d["triggers"].items())
    for name, action in items:
        desc = action.get("description")
        if isinstance(desc, str):
            assert len(desc) <= ACTION_DESCRIPTION_LIMIT, (
                f"{path.name}:{name} description is {len(desc)} chars "
                f"(limit {ACTION_DESCRIPTION_LIMIT})"
            )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name.split("-")[1])
def test_no_self_referencing_set_variable(path):
    """`SetVariable` may not read the variable it assigns.

    The natural accumulator idiom - `x = union(x, item)` - is rejected with
    "Self reference is not supported", but only when the flow is activated, so it
    imports cleanly and then refuses to start. Use AppendToArrayVariable for arrays
    and IncrementVariable for counters instead."""
    for name, action in walk_actions(definition(path)["actions"]):
        if action.get("type") != "SetVariable":
            continue
        var = action["inputs"]["name"]
        value = json.dumps(action["inputs"].get("value"))
        assert f"variables('{var}')" not in value, (
            f"{path.name}:{name} assigns {var} from itself; "
            f"use AppendToArrayVariable or IncrementVariable"
        )


def test_backup_flow_uses_only_sharepoint():
    """The whole point of doing the backup in a flow is that the SharePoint connection
    already works. Reaching for OneDrive for Business would mean a second connector, a
    second consent, and another thing for a DLP policy to reconsider."""
    path = next(p for p in WORKFLOWS if "6-Backup" in p.name)
    refs = set(load(path)["properties"].get("connectionReferences", {}))
    assert refs == {"shared_sharepointonline"}, refs


def test_backup_flow_prunes_only_after_a_successful_write():
    """A failed backup must never delete an older good one."""
    path = next(p for p in WORKFLOWS if "6-Backup" in p.name)
    acts = definition(path)["actions"]
    assert "Write_Manifest" in acts["List_Backups"]["runAfter"]
    assert "List_Backups" in acts["Prune_Old_Backups"]["runAfter"]


def test_backup_flow_records_row_counts():
    """A backup that cannot prove what it captured cannot be distinguished from a
    backup of an empty install - which is how a failed export passes for a good one."""
    path = next(p for p in WORKFLOWS if "6-Backup" in p.name)
    manifest = definition(path)["actions"]["Compose_Manifest"]["inputs"]
    for field in ("syncMapRows", "logRows", "healthRows"):
        assert field in manifest


# ---------------------------------------------------------------------------
# Flow 7 safety. This is the only flow that deletes calendar events outside the
# normal sync path, and it runs precisely when state is already unreliable, so its
# guards are checked structurally rather than trusted to review.
# ---------------------------------------------------------------------------

def _dedup():
    return next(p for p in WORKFLOWS if "7-Dedup" in p.name)


def test_dedup_defaults_to_a_dry_run():
    """Invoked with no body - which is how the CLI and a stray click both invoke it -
    the Apply toggle is absent and must coalesce to false."""
    acts = definition(_dedup())["actions"]
    check = acts["Circuit_Breaker"]["else"]["actions"]["Check_Apply"]
    left = check["expression"]["equals"][0]
    assert "coalesce(triggerBody()?['boolean'], false)" in left


def test_dedup_deletes_only_inside_the_apply_branch():
    """A delete reachable outside the Apply branch would make a dry run destructive."""
    acts = definition(_dedup())["actions"]
    apply_branch = acts["Circuit_Breaker"]["else"]["actions"]["Check_Apply"]["actions"]
    inside = {n for n, _ in walk_actions(apply_branch)}
    for name, action in walk_actions(definition(_dedup())["actions"]):
        if host_of(action).get("operationId") == "DeleteEvent":
            assert name in inside, f"{name} can delete outside the Apply branch"


def test_dedup_only_considers_marked_events():
    """The invariant that protects the user's own events."""
    text = _dedup().read_text()
    assert "o365gcal-key:" in text
    acts = definition(_dedup())["actions"]
    slice_acts = acts["For_Each_Slice"]["actions"]
    marked = slice_acts["Check_Saturated"]["else"]["actions"]["For_Each_Event"]["actions"]["Check_Marked"]
    assert marked["expression"]["contains"][1] == "o365gcal-key:"


def test_dedup_requires_more_than_one_copy():
    acts = definition(_dedup())["actions"]
    check = acts["For_Each_Key"]["actions"]["Check_Duplicate"]
    assert check["expression"] == {"greater": ["@length(outputs('Compose_Group'))", 1]}


def test_dedup_survivor_is_excluded_from_deletion():
    """Excluded by construction in the Query that builds the delete set, rather than by
    a later check that could drift out of step with it."""
    acts = definition(_dedup())["actions"]
    dup = acts["For_Each_Key"]["actions"]["Check_Duplicate"]["actions"]
    where = dup["Filter_Extras"]["inputs"]["where"]
    assert where == "@not(equals(item()?['id'], outputs('Compose_Survivor')))"
    assert dup["For_Each_Extra"]["foreach"] == "@body('Filter_Extras')"


def test_dedup_survivor_choice_is_deterministic():
    """sort() rather than first-seen order: Google returns events in arbitrary order,
    so an order-dependent choice would delete a different copy on every run."""
    acts = definition(_dedup())["actions"]["For_Each_Key"]["actions"]
    survivor = acts["Compose_Survivor"]["inputs"]
    assert "sort(body('Select_Group_Ids'))" in survivor
    assert "Compose_Mapped" in survivor, "must prefer the id the sync map already names"
    assert acts["Select_Group_Ids"]["inputs"]["select"] == "@item()?['id']"


def test_dedup_blocks_deletion_when_a_slice_was_unreadable():
    acts = definition(_dedup())["actions"]
    expr = acts["Circuit_Breaker"]["expression"]["or"][0]
    assert expr == {"greater": ["@length(variables('Unreadable'))", 0]}


def test_dedup_detects_a_saturated_response():
    acts = definition(_dedup())["actions"]
    check = acts["For_Each_Slice"]["actions"]["Check_Saturated"]
    assert check["expression"] == {"greaterOrEquals": ["@outputs('Compose_Returned')", 250]}


def test_dedup_reads_items_not_value():
    """CalendarEventList returns `items`; reading `value` would silently see nothing
    and conclude the calendar is empty."""
    text = _dedup().read_text()
    assert "body('Google_List')?['items']" in text
    assert "body('Google_List')?['value']" not in text


def test_dedup_does_not_invent_a_fingerprint():
    """A fabricated fingerprint would mark a stale event as up to date, so the next
    reconcile would skip repairing it and the duplicate cleanup would leave wrong data
    behind. Checks the payload actually written, not the file text - the explanatory
    comment legitimately names the field."""
    acts = definition(_dedup())["actions"]
    upsert = (acts["Circuit_Breaker"]["else"]["actions"]["Check_Apply"]["actions"]
              ["For_Each_Repair"]["actions"]["Upsert_Row"])
    body = upsert["inputs"]["parameters"]["parameters/body"]
    assert "ContentFingerprint" not in body, "the repair must not write a fingerprint"
    assert "fingerprint pending next reconcile" in body


# ---------------------------------------------------------------------------
# Growth and truncation guards. Every read here is capped and SharePoint sends no
# continuation token, so a full page is indistinguishable from a complete one.
# ---------------------------------------------------------------------------

def test_reconcile_aborts_on_a_truncated_map_read():
    """The silent bug this prevents: reconcile reads 5000 of 6000 map rows, sees the
    missing occurrences as unmirrored, and creates a second copy of each while
    reporting success."""
    path = next(p for p in WORKFLOWS if "3-Reconcile" in p.name)
    acts = definition(path)["actions"]["Try_Reconcile"]["actions"]
    check = acts["Check_Map_Read_Complete"]
    assert check["expression"] == {"greaterOrEquals": ["@length(variables('MapRows'))", 5000]}
    terminate = check["actions"]["Stop_Run"]
    assert terminate["type"] == "Terminate"
    assert terminate["inputs"]["runStatus"] == "Failed", (
        "must fail the run, not succeed having done nothing - a green run that skipped "
        "the work is indistinguishable from a healthy one"
    )
    assert "Check_Map_Read_Complete" in acts["For_Each_Outlook_Event"]["runAfter"], (
        "the guard must gate the loop, not run beside it"
    )


def test_watchdog_prunes_the_sync_map():
    """Nothing deleted a map row before this: every occurrence ever mirrored kept its
    row forever, so a busy calendar crossed the read limit in about three years."""
    path = next(p for p in WORKFLOWS if "5-Watchdog" in p.name)
    acts = definition(path)["actions"]
    assert "Find_Settled_Deletions" in acts and "Delete_Settled_Deletions" in acts
    assert "Find_Stale_Rows" in acts and "Delete_Stale_Rows" in acts


def test_watchdog_prunes_withdrawals_before_stale_rows():
    """Withdrawal rows point at nothing, so they are unambiguously safe to remove;
    stale rows orphan a Google event. Do the safe work first."""
    path = next(p for p in WORKFLOWS if "5-Watchdog" in p.name)
    acts = definition(path)["actions"]
    assert "Delete_Settled_Deletions" in acts["Find_Stale_Rows"]["runAfter"]


def test_watchdog_warns_before_the_read_limit():
    path = next(p for p in WORKFLOWS if "5-Watchdog" in p.name)
    acts = definition(path)["actions"]
    assert "Get_Map_Size" in acts
    assert "ItemCount" in acts["Get_Map_Size"]["inputs"]["parameters"]["parameters/uri"], (
        "use ItemCount: a capped page read cannot report a count above its own cap"
    )
    assert "ListSizeWarnAt" in json.dumps(acts["Check_List_Size"]["expression"])


def test_stale_row_filter_respects_the_sync_window():
    """Pruning a row whose occurrence is still in the window would make reconcile
    mirror that event again."""
    path = next(p for p in WORKFLOWS if "5-Watchdog" in p.name)
    uri = definition(path)["actions"]["Find_Stale_Rows"]["inputs"]["parameters"]["parameters/uri"]
    assert "WindowPastDays" in uri and "MapRetentionDays" in uri, (
        "the cutoff must combine the window with retention, not use retention alone"
    )


def test_retention_defaults_are_internally_consistent():
    from o365gcal.limits import retention_is_sane
    from o365gcal.model import Config

    assert retention_is_sane(Config()) == []


def test_google_writes_never_send_an_attendees_parameter():
    """The connector rejects both an empty attendees string and a null one with
    "Invalid attendee email", and a flow cannot omit a parameter conditionally. Sending
    it at all therefore fails every create for any event with no attendees - which is
    most of them. Attendee information is carried in the description instead."""
    for path in WORKFLOWS:
        for name, action in walk_actions(definition(path)["actions"]):
            host = host_of(action)
            if host.get("operationId") not in ("CreateEvent", "UpdateEvent"):
                continue
            params = action["inputs"]["parameters"]
            offenders = [k for k in params if k.endswith("/attendees")]
            assert not offenders, (
                f"{path.name}:{name} sends {offenders}; the connector cannot express "
                f"'no attendees'"
            )


def test_attendee_information_still_reaches_the_description():
    """Removing the parameter must not lose the information. The organiser, both
    attendee lists and the RSVP state are what 'visibility of event invitations'
    means, and they belong in the description."""
    path = next(p for p in WORKFLOWS if "3-Reconcile" in p.name)
    loop = (definition(path)["actions"]["Try_Reconcile"]["actions"]
            ["For_Each_Outlook_Event"]["actions"])
    desc = loop["Compose_Description"]["inputs"]
    for field in ("organizer", "requiredAttendees", "optionalAttendees", "responseType"):
        assert f"item()?['{field}']" in desc, f"description must include {field}"
    assert "Respond in Outlook" in desc


def test_child_flow_persists_the_map_row_after_a_successful_write():
    """The map write must depend on the Google write succeeding, and nothing else may
    run in between: a failed create with a written row would make reconcile believe an
    event exists that does not."""
    path = next(p for p in WORKFLOWS if "ApplyEvent" in p.name)
    acts = dict(walk_actions(definition(path)["actions"]))
    assert "Switch_Operation" in acts["Write_Map_Row"]["runAfter"]
    assert acts["Write_Map_Row"]["runAfter"]["Switch_Operation"] == ["Succeeded"]


# ---------------------------------------------------------------------------
# Watchdog accuracy. It sent two alerts claiming the calendar was going stale while
# the reconciler was healthy and syncing. An alert that is wrong is worse than none:
# people stop reading it, including on the day it is right.
# ---------------------------------------------------------------------------

def _watchdog():
    return next(p for p in WORKFLOWS if "5-Watchdog" in p.name)


def test_staleness_uses_the_per_flow_threshold():
    """A single global threshold marked a daily digest as broken for 22 hours a day."""
    where = definition(_watchdog())["actions"]["Filter_Stale"]["inputs"]["where"]
    assert "item()?['StaleAfterMinutes']" in where, "must read the row's own threshold"
    assert "HeartbeatStaleMinutes" in where, "keep a fallback for rows predating it"


def test_only_the_reconciler_may_claim_the_calendar_is_stale():
    acts = definition(_watchdog())["actions"]
    where = acts["Filter_Sync_Affecting"]["inputs"]["where"]
    assert "AffectsSync" in where
    body = acts["Alert_If_Broken"]["actions"]["Send_Breakage_Alert"]["inputs"]["parameters"]["emailMessage/Body"]
    assert "Filter_Sync_Affecting" in body, (
        "the 'going stale' wording must be conditional on a sync-affecting flow"
    )
    assert "still syncing normally" in body, (
        "when only a supporting flow is late, say so plainly"
    )


def test_setup_seeds_a_threshold_for_every_monitored_flow():
    from o365gcal.schema import HEARTBEAT_EXPECTATIONS

    path = next(p for p in WORKFLOWS if "0-Setup" in p.name)
    seed = definition(path)["actions"]["Seed_Health_Rows"]
    for flow, minutes in HEARTBEAT_EXPECTATIONS.items():
        assert flow in seed["foreach"], f"{flow} gets no health row"
        assert str(minutes) in seed["foreach"], f"{flow}'s threshold is not seeded"
    body = seed["actions"]["Create_Health_Row"]["inputs"]["parameters"]["parameters/body"]
    assert "StaleAfterMinutes" in body and "AffectsSync" in body
    assert "LastSuccessUtc" in body, (
        "a freshly seeded row must be stamped, or setup itself trips the staleness alert"
    )


def test_the_event_driven_trigger_gets_no_health_row():
    """It is silent by design on a quiet calendar. Monitoring it produced a false
    alarm within hours of going live."""
    path = next(p for p in WORKFLOWS if "0-Setup" in p.name)
    seed = definition(path)["actions"]["Seed_Health_Rows"]
    assert "1 Sync Outlook Trigger" not in seed["foreach"]


def test_setup_does_not_retry_expected_failures():
    """Provisioning is idempotent by tolerating failure, so on every run after the
    first each create fails with "already exists". Retrying that - four attempts with
    exponential backoff, around fifty times in sequence - turned a re-run from seconds
    into more than ten minutes, which makes "safe to re-run" true in principle and
    useless in practice."""
    path = next(p for p in WORKFLOWS if "0-Setup" in p.name)
    acts = definition(path)["actions"]
    provisioning = [n for n in acts if n.startswith(("Create_List_", "Add_", "Index_"))]
    assert provisioning, "expected provisioning actions"
    for name in provisioning:
        policy = (acts[name].get("inputs") or {}).get("retryPolicy")
        assert policy == {"type": "none"}, (
            f"{name} retries a failure that is expected on every re-run"
        )


def test_setup_provisions_every_column_the_schema_declares():
    """The failure this catches: two columns were added to o365gcal.schema and nothing
    regenerated flow 0, so they were never created and every health-row insert failed.
    Found from a flow run hours later rather than from the build."""
    from o365gcal.schema import ALL_LISTS

    path = next(p for p in WORKFLOWS if "0-Setup" in p.name)
    names = {n for n, _ in walk_actions(definition(path)["actions"])}
    missing = []
    for spec in ALL_LISTS:
        key = spec["list"].replace("O365GCal", "")
        for field in spec["fields"]:
            if f"Add_{key}_{field['name']}" not in names:
                missing.append(f"{spec['list']}.{field['name']}")
    assert not missing, (
        f"setup does not create: {missing}. Run: python tools/gen_flow0.py"
    )


def test_setup_indexes_every_column_the_schema_marks_indexed():
    from o365gcal.schema import ALL_LISTS

    path = next(p for p in WORKFLOWS if "0-Setup" in p.name)
    names = {n for n, _ in walk_actions(definition(path)["actions"])}
    for spec in ALL_LISTS:
        key = spec["list"].replace("O365GCal", "")
        for field in [f for f in spec["fields"] if f.get("indexed")]:
            assert f"Index_{key}_{field['name']}" in names, (
                f"{spec['list']}.{field['name']} is marked indexed but never indexed"
            )


def test_setup_clears_health_rows_before_seeding():
    """Seeding used a plain insert, so every re-run added a second row per flow. Those
    duplicates carried no threshold, fell back to the global default, and were exactly
    what the watchdog reported as broken flows. An upsert keyed on Title cannot remove
    duplicates that already exist; replacing the set can."""
    path = next(p for p in WORKFLOWS if "0-Setup" in p.name)
    acts = definition(path)["actions"]
    assert "Remove_All_Health_Rows" in acts
    assert "Remove_All_Health_Rows" in acts["Seed_Health_Rows"]["runAfter"], (
        "the clear must precede the seed"
    )
    delete = acts["Remove_All_Health_Rows"]["actions"]["Delete_Health_Row"]
    assert delete["inputs"]["parameters"]["parameters/headers"]["X-HTTP-Method"] == "DELETE"


def test_watchdog_advice_matches_what_is_actually_broken():
    """Telling someone to reauthorise a connection that just probed OK is how an alert
    teaches people to distrust it."""
    path = next(p for p in WORKFLOWS if "5-Watchdog" in p.name)
    body = (definition(path)["actions"]["Alert_If_Broken"]["actions"]
            ["Send_Breakage_Alert"]["inputs"]["parameters"]["emailMessage/Body"])
    assert "both connections are healthy" in body
    assert body.count("Connection_Broken") >= 2, (
        "impact and advice must both be conditional on the probe result"
    )
