"""The Workflow Definition Language function names that actually exist.

This list exists because three expressions shipped that could not run. `filter`,
`select` and `map` read naturally and are what any functional programmer reaches for,
but the expression language has none of them - filtering and projecting are *actions*
(`Query` and `Select`), not functions.

What made it hard to catch: **activation does not validate function names.** A flow
using `filter(...)` imports and activates without complaint, then fails at runtime with
"The template function 'filter' is not defined or not valid." Every earlier inference
of the form "it activated, so the expression must be valid" was therefore unsound -
including the conclusion that `sort()` was safe because a flow using it activated.
`sort()` does happen to exist, but activation was never evidence of that.

Nothing else covered the gap either. The parity tests evaluate expressions against a
small interpreter, but only the handful of expressions they were written for, and the
offending calls were not among them. Hence this list, checked against every expression
in every flow.

Source: the Logic Apps workflow definition language function reference.
"""

from __future__ import annotations

import re

STRING = {
    "concat", "contains", "endsWith", "formatNumber", "guid", "indexOf", "isFloat",
    "isInt", "lastIndexOf", "length", "nthIndexOf", "replace", "slice", "split",
    "startsWith", "substring", "toLower", "toUpper", "trim",
}

COLLECTION = {
    "chunk", "contains", "empty", "first", "intersection", "item", "items", "join",
    "last", "length", "reverse", "skip", "sort", "take", "union",
}

LOGICAL = {
    "and", "equals", "greater", "greaterOrEquals", "if", "less", "lessOrEquals",
    "not", "or", "xor",
}

CONVERSION = {
    "array", "base64", "base64ToBinary", "base64ToString", "binary", "bool",
    "createArray", "dataUri", "dataUriToBinary", "dataUriToString", "decimal",
    "decodeBase64", "decodeDataUri", "decodeUriComponent", "encodeUriComponent",
    "float", "int", "json", "string", "uriComponent", "uriComponentToBinary",
    "uriComponentToString", "xml",
}

MATH = {"add", "div", "max", "min", "mod", "mul", "rand", "range", "sub"}

DATE = {
    "addDays", "addHours", "addMinutes", "addSeconds", "addToTime", "convertFromUtc",
    "convertTimeZone", "convertToUtc", "dateDifference", "dayOfMonth", "dayOfWeek",
    "dayOfYear", "endOfDay", "endOfHour", "endOfMonth", "formatDateTime",
    "getFutureTime", "getPastTime", "parseDateTime", "startOfDay", "startOfHour",
    "startOfMonth", "subtractFromTime", "ticks", "utcNow",
}

REFERENCING = {
    "action", "actionBody", "actionOutputs", "actions", "body", "formDataMultiValues",
    "formDataValue", "item", "items", "iterationIndexes", "multipartBody",
    "outputs", "parameters", "result", "trigger", "triggerBody", "triggerFormDataValue",
    "triggerMultipartBody", "triggerOutputs", "variables", "workflow",
}

MANIPULATION = {"addProperty", "coalesce", "removeProperty", "setProperty"}

URI = {"uriHost", "uriPath", "uriPathAndQuery", "uriPort", "uriQuery", "uriScheme"}

VALID = (STRING | COLLECTION | LOGICAL | CONVERSION | MATH | DATE | REFERENCING
         | MANIPULATION | URI)

#: Names that look like functions but are not, with what to use instead. Kept
#: explicit so the failure message teaches rather than merely rejects.
NOT_FUNCTIONS = {
    "filter": "use a Query action (Filter array); there is no filter() expression",
    "select": "use a Select action; there is no select() expression",
    "map": "use a Select action; there is no map() expression",
    "where": "use a Query action (Filter array)",
    "groupBy": "no equivalent exists; collect into an array and iterate unique keys",
    "sortBy": "sort(collection, 'property')",
    "count": "length(collection)",
    "sum": "no equivalent exists; accumulate with IncrementVariable in a loop",
    "distinct": "union(collection, collection)",
    "flatten": "no equivalent exists; append per item in a nested loop",
}

_CALL = re.compile(r"(?<![A-Za-z0-9_'])([A-Za-z_][A-Za-z0-9_]*)\s*\(")

#: Single-quoted string literals, with '' as the escaped quote. Stripped before
#: scanning: a SharePoint REST path such as getbytitle('X') sits inside a literal and
#: is a URL, not a function call.
_LITERAL = re.compile(r"'(?:[^']|'')*'")


def functions_used(expression: str) -> set[str]:
    """Every function name invoked in an expression string.

    String literals are removed first, so text that merely looks like a call - an
    OData path, a URL fragment - is not mistaken for one.
    """
    return set(_CALL.findall(_LITERAL.sub("''", expression or "")))


def invalid_functions(expression: str) -> dict[str, str]:
    """Names used that the runtime does not define, mapped to guidance."""
    problems: dict[str, str] = {}
    for name in functions_used(expression):
        if name in VALID:
            continue
        problems[name] = NOT_FUNCTIONS.get(
            name, "not a Workflow Definition Language function"
        )
    return problems
