"""SPDX licence expressions, checked by syntax only (docs/design/packages.md, Licences; SPDX 2.3, Annex D).

    MIT    Apache-2.0    GPL-2.0+    GPL-2.0-only WITH Classpath-exception-2.0    LicenseRef-acme-1
    (MIT OR Apache-2.0) AND CC-BY-4.0    DocumentRef-spdx-tool-1.2:LicenseRef-MIT-Style-2

No licence list is shipped: an identifier is any idstring (letters, digits, - and .), so a new SPDX identifier needs
no Undine release, and a misspelt one is the reviewer's to see. WITH binds tighter than AND, AND tighter than OR;
the operators are upper case. parse() is used by `pkg init` and `pkg check` (a licence is required to publish), not
by the manifest parser, so a manifest that parsed before keeps parsing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_ID = r"[A-Za-z0-9.\-]+"
_TOKEN = re.compile(rf"\s*(\(|\)|\+|(?:DocumentRef-{_ID}:)?LicenseRef-{_ID}|{_ID}|\S)")
_REF = re.compile(rf"^(?:DocumentRef-{_ID}:)?LicenseRef-{_ID}$")
_IDSTRING = re.compile(rf"^{_ID}$")
OPERATORS = ("AND", "OR", "WITH")
MAX_LEN = 1024
MAX_DEPTH = 32                            # nested parentheses: far above a real expression, below Python's recursion


class SpdxError(ValueError):
    pass


@dataclass(frozen=True)
class License:
    """A simple expression: an identifier (with + for "or later") or a LicenseRef, and an exception (WITH)."""
    id: str
    plus: bool = False
    exception: str | None = None

    def __str__(self) -> str:
        return self.id + ("+" if self.plus else "") + (f" WITH {self.exception}" if self.exception else "")


@dataclass(frozen=True)
class Compound:
    op: str                                   # AND | OR
    left: License | Compound
    right: License | Compound

    def __str__(self) -> str:
        def side(x) -> str:
            return f"({x})" if isinstance(x, Compound) and x.op != self.op else str(x)
        return f"{side(self.left)} {self.op} {side(self.right)}"


Expr = License | Compound


def ids(e: Expr) -> list[str]:
    """The licence identifiers and LicenseRefs an expression names, in order, each once."""
    if isinstance(e, License):
        return [e.id]
    return list(dict.fromkeys(ids(e.left) + ids(e.right)))


def _tokens(text: str) -> list[str]:
    out, pos = [], 0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        m = _TOKEN.match(text, pos)
        out.append(m.group(1))
        pos = m.end()
    return out


def parse(text) -> Expr:
    """The expression in `text`, or SpdxError saying where its syntax fails."""
    if not isinstance(text, str) or not text.strip():
        raise SpdxError("no licence: write an SPDX expression, e.g. Apache-2.0 or MIT OR Apache-2.0")
    if len(text) > MAX_LEN:
        raise SpdxError(f"a licence expression of {len(text)} characters, more than {MAX_LEN}")
    toks = _tokens(text)
    pos = [0]

    def peek() -> str | None:
        return toks[pos[0]] if pos[0] < len(toks) else None

    def take() -> str:
        t = peek()
        pos[0] += 1
        return t

    def fail(what: str) -> SpdxError:
        at = peek()
        where = f" at {at!r}" if at is not None else " at the end"
        hint = ""
        if at is not None and at.upper() in OPERATORS and at not in OPERATORS:
            hint = f": write the operator in upper case, {at.upper()}"
        return SpdxError(f"{text!r} is not an SPDX expression: {what}{where}{hint}")

    def simple() -> License:
        t = peek()
        if t is None or t in OPERATORS or t in ("(", ")", "+") or not (_REF.match(t) or _IDSTRING.match(t)):
            raise fail("a licence identifier expected")
        take()
        plus = False
        if peek() == "+":
            if _REF.match(t):
                raise fail("+ follows a licence identifier, not a LicenseRef")
            take()
            plus = True
        exc = None
        if peek() == "WITH":
            take()
            e = peek()
            if e is None or e in OPERATORS or not _IDSTRING.match(e):
                raise fail("an exception identifier expected after WITH")
            exc = take()
        return License(t, plus, exc)

    depth = [0]

    def atom() -> Expr:
        if peek() == "(":
            depth[0] += 1
            if depth[0] > MAX_DEPTH:
                raise fail(f"parentheses nested deeper than {MAX_DEPTH}")
            take()
            e = disjunction()
            if peek() != ")":
                raise fail("a closing ) expected")
            take()
            depth[0] -= 1
            return e
        return simple()

    def conjunction() -> Expr:
        e = atom()
        while peek() == "AND":
            take()
            e = Compound("AND", e, atom())
        return e

    def disjunction() -> Expr:
        e = conjunction()
        while peek() == "OR":
            take()
            e = Compound("OR", e, conjunction())
        return e

    e = disjunction()
    if peek() is not None:
        raise fail("AND, OR or the end expected")
    return e


def problem(text) -> str | None:
    """Why `text` is not an SPDX expression, or None when it is one."""
    try:
        parse(text)
    except SpdxError as e:
        return str(e)
    return None
