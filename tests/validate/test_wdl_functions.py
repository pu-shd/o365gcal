"""Rejects flow expressions that call functions the runtime does not define.

The guard that was missing. `filter(...)` shipped in four flows: it imported cleanly,
activated cleanly, and failed at runtime with "The template function 'filter' is not
defined or not valid." Activation does not validate function names, so nothing before
this test could have caught it.
"""

import json
import re
from pathlib import Path

import pytest
from o365gcal.wdl_functions import NOT_FUNCTIONS, VALID, invalid_functions

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((ROOT / "solution" / "src" / "Workflows").glob("*.json"))

#: A whole-string expression: "@concat(...)".
_WHOLE = re.compile(r"^@[a-zA-Z_]")
#: Interpolations embedded in a larger string: "<p>@{join(...)}</p>". Missing these
#: is how six select() calls hid inside HTML email bodies - the string starts with
#: markup, so a prefix-only check never looked at it.
_INTERPOLATED = re.compile(r"@\{(.*?)\}", re.S)


def expressions_in(node, path="") -> list[tuple[str, str]]:
    """Every expression in a flow definition, with a rough location.

    Covers both whole-string expressions and interpolations inside longer text.
    """
    found: list[tuple[str, str]] = []
    if isinstance(node, str):
        if _WHOLE.match(node):
            found.append((path, node))
        for i, inner in enumerate(_INTERPOLATED.findall(node)):
            found.append((f"{path}#{i}", inner))
    elif isinstance(node, dict):
        for k, v in node.items():
            # Descriptions are prose and may legitimately mention a function name.
            if k == "description":
                continue
            found += expressions_in(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found += expressions_in(v, f"{path}[{i}]")
    return found


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name.split("-")[1])
def test_every_function_exists(path):
    doc = json.loads(path.read_text())
    failures = []
    for where, expr in expressions_in(doc["properties"]["definition"]):
        for name, hint in invalid_functions(expr).items():
            failures.append(f"  {where}: {name}() - {hint}")
    assert not failures, f"{path.name} calls undefined function(s):\n" + "\n".join(
        sorted(set(failures))
    )


@pytest.mark.parametrize("name", sorted(NOT_FUNCTIONS))
def test_known_non_functions_are_not_in_the_valid_set(name):
    """Guards the guard: if one of these were ever added to VALID by mistake, the
    check above would silently stop protecting anything."""
    assert name not in VALID


def test_the_evaluator_never_implements_a_missing_function():
    """Keeps the parity harness honest.

    The evaluator does not currently implement any of these, and must not start: a
    harness more capable than the runtime it models does not verify expressions, it
    flatters them."""
    evaluator = (ROOT / "tests" / "validate" / "wdl.py").read_text()
    for name in NOT_FUNCTIONS:
        assert f'name == "{name}"' not in evaluator, (
            f"the evaluator must not implement {name}(); the runtime has no such function"
        )


#: The expression language has no operators at all - no +, -, ==, &&. Everything is a
#: function call: concat, add, equals, and. A '+' between two strings is not
#: concatenation; it is a syntax error the runtime reports only when the action runs.
_OPERATOR = re.compile(r"'\s*\+\s*|\s\+\s'|\|\||&&|(?<![<>!=])==(?!=)")


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name.split("-")[1])
def test_no_infix_operators_in_expressions(path):
    """Caught a generator emitting `concat(...) + '(' + string(id) + ')'`, which
    produced a URI that looked plausible and could never evaluate."""
    doc = json.loads(path.read_text())
    offenders = []
    for where, expr in expressions_in(doc["properties"]["definition"]):
        if _OPERATOR.search(expr):
            offenders.append(f"  {where}: {expr[:120]}")
    assert not offenders, (
        f"{path.name} uses an infix operator; use concat/add/equals/and instead:\n"
        + "\n".join(offenders)
    )
