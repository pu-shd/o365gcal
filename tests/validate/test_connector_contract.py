"""Validates every connector action in the flows against the live connector swagger.

Why this exists: a flow action keys its parameters by the connector's swagger
parameter names, and body properties appear flattened as `body/prop` or `item/prop`.
Those keys are not published in the Microsoft Learn connector reference. Getting one
wrong produces a solution that imports cleanly and then fails, or silently drops a
field, at runtime in someone else's tenant.

The swagger is not committed by default because it is large and tenant-fetchable.
Run `./scripts/fetch-connector-swagger.sh` and these tests light up; without it they
skip loudly rather than passing vacuously.
"""

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SWAGGER_DIR = ROOT / "connectors"
WORKFLOWS = sorted((ROOT / "solution" / "src" / "Workflows").glob("*.json"))

pytestmark = pytest.mark.skipif(
    not SWAGGER_DIR.exists() or not any(SWAGGER_DIR.glob("*.json")),
    reason="No connector swagger. Run ./scripts/fetch-connector-swagger.sh to enable "
           "connector contract checks (see docs/ADMIN.md).",
)


def load_swagger(api: str) -> dict:
    doc = json.loads((SWAGGER_DIR / f"{api}.json").read_text())
    # The API wraps the spec as properties.swagger; accept a bare spec too.
    return doc.get("properties", {}).get("swagger", doc)


def operations(api: str) -> dict[str, dict]:
    """Map operationId -> its swagger operation object."""
    out = {}
    for path, methods in load_swagger(api).get("paths", {}).items():
        for method, op in methods.items():
            if isinstance(op, dict) and "operationId" in op:
                out[op["operationId"]] = {"path": path, "method": method, **op}
    return out


def deref_param(param: dict, spec: dict) -> dict:
    """Connectors hoist shared parameters into a top-level `parameters` map and
    reference them by $ref, so a naive read sees a dict with no `name` at all."""
    if "$ref" in param:
        return spec.get("parameters", {}).get(param["$ref"].rsplit("/", 1)[-1], {})
    return param


def op_parameters(api: str, operation_id: str) -> list[dict]:
    spec = load_swagger(api)
    op = operations(api).get(operation_id)
    return [deref_param(p, spec) for p in (op or {}).get("parameters", [])]


def expand(prefix: str, schema: dict, spec: dict, acc: set) -> None:
    """Flatten a body schema into the dotted keys a flow action actually uses."""
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = spec.get("definitions", {}).get(name, {})
    for prop, sub in (schema.get("properties") or {}).items():
        key = f"{prefix}/{prop}"
        acc.add(key)
        if isinstance(sub, dict) and (sub.get("type") == "object" or "$ref" in sub):
            expand(key, sub, spec, acc)


def valid_keys(api: str, operation_id: str) -> set[str]:
    spec = load_swagger(api)
    op = operations(api).get(operation_id)
    if op is None:
        return set()
    keys: set[str] = set()
    for param in op_parameters(api, operation_id):
        if not param.get("name"):
            continue
        if param.get("in") == "body":
            keys.add(param["name"])
            expand(param["name"], param.get("schema", {}), spec, keys)
        else:
            keys.add(param["name"])
    return keys


def connector_actions():
    """Every OpenApiConnection action across all flows, flattened."""
    from test_solution_static import definition, host_of, walk_actions

    for path in WORKFLOWS:
        d = definition(path)
        for name, action in list(walk_actions(d["actions"])) + list(d["triggers"].items()):
            host = host_of(action)
            if host.get("operationId"):
                yield path.name, name, host["apiId"].rsplit("/", 1)[-1], host["operationId"], action


@pytest.mark.parametrize(
    "flow,action,api,op",
    [(f, a, api, op) for f, a, api, op, _ in connector_actions()],
    ids=lambda v: str(v)[:40],
)
def test_operation_exists(flow, action, api, op):
    available = operations(api)
    assert op in available, (
        f"{flow}:{action} calls {api}.{op}, which the connector does not expose. "
        f"Closest available: {sorted(k for k in available if op[:6].lower() in k.lower())[:5]}"
    )


@pytest.mark.parametrize(
    "flow,action,api,op,defn",
    list(connector_actions()),
    ids=lambda v: str(v)[:40],
)
def test_parameter_keys_are_real(flow, action, api, op, defn):
    """The check that guards against a runtime-only failure: a misspelled or
    wrongly-nested parameter key is accepted at import and dropped when it runs."""
    allowed = valid_keys(api, op)
    if not allowed:
        pytest.skip(f"{api}.{op} not present in swagger")
    used = set((defn.get("inputs") or {}).get("parameters", {}))
    unknown = used - allowed
    assert not unknown, (
        f"{flow}:{action} ({api}.{op}) uses parameter key(s) the connector does not "
        f"define: {sorted(unknown)}.\nValid keys: {sorted(allowed)}"
    )


@pytest.mark.parametrize(
    "flow,action,api,op,defn",
    list(connector_actions()),
    ids=lambda v: str(v)[:40],
)
def test_required_parameters_are_supplied(flow, action, api, op, defn):
    spec = load_swagger(api)
    swagger_op = operations(api).get(op)
    if swagger_op is None:
        pytest.skip("operation not in swagger")
    used = set((defn.get("inputs") or {}).get("parameters", {}))
    missing = []
    for param in op_parameters(api, op):
        if not param.get("name"):
            continue
        if param.get("in") == "body":
            schema = param.get("schema", {})
            if "$ref" in schema:
                schema = spec.get("definitions", {}).get(schema["$ref"].rsplit("/", 1)[-1], {})
            for req in schema.get("required", []):
                if f"{param['name']}/{req}" not in used:
                    missing.append(f"{param['name']}/{req}")
        elif param.get("required") and param["name"] not in used:
            # connectionId and friends are injected by the runtime, never authored.
            if param.get("x-ms-visibility") not in ("internal",) and param["name"] != "connectionId":
                missing.append(param["name"])
    assert not missing, f"{flow}:{action} ({api}.{op}) omits required parameter(s): {missing}"
