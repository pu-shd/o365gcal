"""A small evaluator for the subset of the Workflow Definition Language the flows use.

This exists so `test_expression_parity.py` can prove that the expressions shipped in
the flow JSON compute the same values as the Python engine, instead of merely
asserting that some expected substring appears somewhere in the file. String matching
would pass happily while the two implementations diverged.

Only the functions the flows actually use are implemented; anything else raises, so
an unnoticed new function in a flow fails the test rather than silently evaluating.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<str>'(?:[^']|'')*')
      | (?P<num>-?\d+(?:\.\d+)?)
      | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
      | (?P<punc>\(|\)|,|\?|\[|\]|@)
    )""",
    re.VERBOSE,
)


class WdlError(Exception):
    pass


def _tokenize(src: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m:
            if src[pos].isspace():
                pos += 1
                continue
            raise WdlError(f"cannot tokenize at {src[pos:pos+30]!r}")
        pos = m.end()
        for kind in ("str", "num", "name", "punc"):
            if m.group(kind) is not None:
                out.append((kind, m.group(kind)))
                break
    return out


class Parser:
    def __init__(self, tokens):
        self.t, self.i = tokens, 0

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self, expect=None):
        kind, val = self.peek()
        if expect and val != expect:
            raise WdlError(f"expected {expect!r}, got {val!r}")
        self.i += 1
        return kind, val

    def parse(self):
        node = self.expr()
        if self.i != len(self.t):
            raise WdlError(f"trailing tokens at {self.t[self.i:]}")
        return node

    def expr(self):
        kind, val = self.peek()
        if val == "@":
            self.take()
            return self.expr()
        if kind == "str":
            self.take()
            return ("lit", val[1:-1].replace("''", "'"))
        if kind == "num":
            self.take()
            return ("lit", float(val) if "." in val else int(val))
        if kind == "name":
            self.take()
            args = []
            if self.peek()[1] == "(":
                self.take("(")
                while self.peek()[1] != ")":
                    args.append(self.expr())
                    if self.peek()[1] == ",":
                        self.take(",")
                self.take(")")
            node = ("call", val, args)
            return self.accessors(node)
        raise WdlError(f"unexpected token {val!r}")

    def accessors(self, node):
        while True:
            kind, val = self.peek()
            if val == "?":
                self.take("?")
                continue
            if val == "[":
                self.take("[")
                key = self.expr()
                self.take("]")
                node = ("index", node, key)
                continue
            return node


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_FORMATS = {"yyyy-MM-ddTHH:mm:ss\\Z": "%Y-%m-%dT%H:%M:%SZ", "yyyy-MM-ddTHH:mm:ssZ": "%Y-%m-%dT%H:%M:%SZ"}


class Evaluator:
    """`context` supplies item(), parameters(), variables() and utcNow()."""

    def __init__(self, context: dict):
        self.ctx = context

    def eval(self, src: str):
        return self._node(Parser(_tokenize(src)).parse())

    def _node(self, node):
        tag = node[0]
        if tag == "lit":
            return node[1]
        if tag == "index":
            base = self._node(node[1])
            key = self._node(node[2])
            return None if base is None else base.get(key)
        _, name, raw = node
        args = [self._node(a) for a in raw]
        return self._call(name, args)

    def _call(self, name, a):  # noqa: C901 - a flat dispatch table is clearest here
        if name == "item":
            return self.ctx["item"]
        if name == "parameters":
            if a[0] not in self.ctx["parameters"]:
                raise WdlError(f"undeclared environment variable: {a[0]}")
            return self.ctx["parameters"][a[0]]
        if name == "variables":
            return self.ctx["variables"][a[0]]
        if name == "outputs":
            return self.ctx.get("outputs", {})[a[0]]
        if name == "utcNow":
            return self.ctx["utcNow"].strftime("%Y-%m-%dT%H:%M:%SZ")
        if name == "concat":
            return "".join("" if x is None else str(x) for x in a)
        if name == "string":
            return "" if a[0] is None else str(a[0])
        if name == "length":
            return len(a[0] or ())
        if name == "substring":
            return a[0][int(a[1]) : int(a[1]) + int(a[2])]
        if name == "trim":
            return (a[0] or "").strip()
        if name == "replace":
            return (a[0] or "").replace(a[1], a[2])
        if name == "decodeUriComponent":
            from urllib.parse import unquote

            return unquote(a[0])
        if name == "toLower":
            return (a[0] or "").lower()
        if name == "coalesce":
            return next((x for x in a if x is not None), None)
        if name == "createArray":
            return list(a)
        if name == "contains":
            return a[1] in a[0]
        if name == "formatDateTime":
            fmt = _FORMATS.get(a[1]) if len(a) > 1 else "%Y-%m-%dT%H:%M:%SZ"
            if fmt is None:
                raise WdlError(f"unsupported date format {a[1]!r}")
            return _dt(a[0]).astimezone(timezone.utc).strftime(fmt)
        if name == "addDays":
            return (_dt(a[0]) + timedelta(days=int(a[1]))).strftime("%Y-%m-%dT%H:%M:%SZ")
        if name == "addMinutes":
            return (_dt(a[0]) + timedelta(minutes=int(a[1]))).strftime("%Y-%m-%dT%H:%M:%SZ")
        if name == "int":
            return int(a[0])
        if name == "min":
            return min(a)
        if name == "max":
            return max(a)
        if name == "mul":
            return a[0] * a[1]
        if name == "div":
            return a[0] // a[1] if all(isinstance(x, int) for x in a) else a[0] / a[1]
        if name == "sub":
            return a[0] - a[1]
        if name == "add":
            return a[0] + a[1]
        if name == "equals":
            return a[0] == a[1]
        if name == "greater":
            return a[0] > a[1]
        if name == "less":
            return a[0] < a[1]
        if name == "greaterOrEquals":
            return a[0] >= a[1]
        if name == "lessOrEquals":
            return a[0] <= a[1]
        if name == "and":
            return all(a)
        if name == "or":
            return any(a)
        if name == "not":
            return not a[0]
        if name == "if":
            return a[1] if a[0] else a[2]
        if name == "empty":
            return not a[0]
        if name in ("true", "false"):
            return name == "true"
        if name == "null":
            return None
        raise WdlError(f"unimplemented WDL function: {name}()")
