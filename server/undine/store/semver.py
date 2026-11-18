"""Versions and version requirements of packages: strict SemVer 2.0.0 and cargo's requirement forms.

A git requirement has exactly one candidate (the tree at its rev): there a requirement only checks the label a
manifest's [package] version gives. An index requirement chooses among the versions an index lists (store/index.py).
Accepted forms, cargo's meanings:

    1.2.3, 1.2     caret (bare)        ^1.2.3   >=1.2.3, <2.0.0      ^0.3   >=0.3.0, <0.4.0
    ~1.2           >=1.2.0, <1.3.0     =1.2.3   exactly 1.2.3        *      any release
    >=1.2, <1.5    comparators (>=, >, <, <=), a comma list meaning all of them
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_NUM = r"(?:0|[1-9]\d*)"
_PRE_ID = r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
_VERSION = re.compile(rf"^({_NUM})\.({_NUM})\.({_NUM})"
                      rf"(?:-({_PRE_ID}(?:\.{_PRE_ID})*))?"
                      r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$")
_PARTIAL = re.compile(rf"^({_NUM})(?:\.({_NUM}))?(?:\.({_NUM}))?$")


class SemverError(ValueError):
    pass


@dataclass(frozen=True, eq=False)
class Version:
    """A SemVer 2.0.0 version. Compared by precedence: build metadata is ignored, so 1.0.0+a == 1.0.0+b."""
    major: int
    minor: int
    patch: int
    pre: tuple[str | int, ...] = ()
    build: str = ""

    @classmethod
    def parse(cls, s) -> Version:
        m = _VERSION.match(s) if isinstance(s, str) else None
        if not m:
            raise SemverError(f"{s!r} is not semver: MAJOR.MINOR.PATCH, e.g. 1.2.0")
        pre = tuple(int(x) if x.isdigit() else x for x in m[4].split(".")) if m[4] else ()
        return cls(int(m[1]), int(m[2]), int(m[3]), pre, m[5] or "")

    def _key(self) -> tuple:
        # a release sorts above its pre-releases; numeric identifiers sort below alphanumeric ones (section 11)
        pre = tuple((0, x, "") if isinstance(x, int) else (1, 0, x) for x in self.pre)
        return (self.major, self.minor, self.patch, 0 if self.pre else 1, pre)

    @property
    def release(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __eq__(self, other):
        return self._key() == other._key() if isinstance(other, Version) else NotImplemented

    def __hash__(self):
        return hash(self._key())

    def __lt__(self, other):
        return self._key() < other._key() if isinstance(other, Version) else NotImplemented

    def __le__(self, other):
        return self._key() <= other._key() if isinstance(other, Version) else NotImplemented

    def __gt__(self, other):
        return self._key() > other._key() if isinstance(other, Version) else NotImplemented

    def __ge__(self, other):
        return self._key() >= other._key() if isinstance(other, Version) else NotImplemented

    def __str__(self) -> str:
        s = f"{self.major}.{self.minor}.{self.patch}"
        if self.pre:
            s += "-" + ".".join(str(x) for x in self.pre)
        return s + (f"+{self.build}" if self.build else "")


@dataclass(frozen=True)
class Req:
    """A version requirement: the versions every clause accepts. A clause is (op, version), op one of >=, >, <, <=
    and =; ^ and ~ give two (>= and <); '*' gives none (every release). `text` is the requirement as written."""
    text: str
    clauses: tuple[tuple[str, Version], ...] = ()

    @classmethod
    def parse(cls, s) -> Req:
        if not isinstance(s, str):
            raise SemverError(f"{s!r} is not a version requirement: a string such as \"^1.2\"")
        t = s.strip()
        if t == "*":
            return cls(t)
        parts = [p.strip() for p in t.split(",")]
        if any(not p for p in parts):
            raise SemverError(f"{s!r}: an empty part in a comma list (write e.g. \">=1.2, <1.5\")")
        if any(p == "*" for p in parts):
            raise SemverError(f"{s!r}: * stands alone (it means any release)")
        return cls(t, tuple(c for p in parts for c in _clauses(p, s)))

    @property
    def lo(self) -> Version | None:
        """The lowest version a >= or = clause names (None: no lower bound)."""
        vs = [v for op, v in self.clauses if op in (">=", "=")]
        return max(vs) if vs else None

    def matches(self, v: Version) -> bool:
        if v.pre and not any(c.pre and c.release == v.release for _, c in self.clauses):
            return False                          # a pre-release only for a requirement that names one of its release
        return all(_OPS[op](v, c) for op, c in self.clauses)

    def __str__(self) -> str:
        return self.text


_OPS = {">=": lambda v, c: v >= c, ">": lambda v, c: v > c, "<": lambda v, c: v < c, "<=": lambda v, c: v <= c,
        "=": lambda v, c: v == c}


def _clauses(t: str, s: str) -> list[tuple[str, Version]]:
    """The clauses of one part of a requirement (between commas)."""
    op = next((o for o in (">=", "<=", ">", "<", "=", "^", "~") if t.startswith(o)), "")
    rest = t[len(op):].strip()
    if op == "=":
        return [("=", Version.parse(rest))]
    lo, given = _lower(rest, s)
    if op in (">=", ">", "<", "<="):
        if op == ">" and given < 3:                 # >1.2 means above every 1.2.x
            return [(">=", _tilde_hi(lo, given) if given == 2 else Version(lo.major + 1, 0, 0))]
        if op == "<=" and given < 3:                # <=1.2 means up to every 1.2.x
            return [("<", _tilde_hi(lo, given) if given == 2 else Version(lo.major + 1, 0, 0))]
        return [(op, lo)]
    if op == "~" and given < 2:
        raise SemverError(f"{s!r}: ~ needs MAJOR.MINOR, e.g. ~1.2 (for any 1.x write ^1)")
    if op == "" and given < 2:
        raise SemverError(f"{s!r}: a bare version needs MAJOR.MINOR, e.g. 1.2 (for any {rest}.x write ^{rest})")
    return [(">=", lo), ("<", _tilde_hi(lo, given) if op == "~" else _caret_hi(lo, given))]


def _lower(rest: str, s: str) -> tuple[Version, int]:
    """The lower bound of a caret or tilde requirement, and how many of MAJOR.MINOR.PATCH it gives."""
    m = _PARTIAL.match(rest)
    if m:
        given = sum(1 for g in m.groups() if g is not None)
        return Version(int(m[1]), int(m[2] or 0), int(m[3] or 0)), given
    if _VERSION.match(rest):
        return Version.parse(rest), 3
    raise SemverError(f"{s!r} is not a version requirement: ^, ~, =, >=, >, < or <= and a version, e.g. ^1.2, =1.2.0 "
                      "or >=1.2, <1.5")


def _caret_hi(lo: Version, given: int) -> Version:
    # ^ keeps the leftmost non-zero part of what is given: ^1.2 <2.0.0, ^0.3 <0.4.0, ^0.0.3 <0.0.4, ^0.0 <0.1.0
    if lo.major > 0 or given == 1:
        return Version(lo.major + 1, 0, 0)
    if lo.minor > 0 or given == 2:
        return Version(0, lo.minor + 1, 0)
    return Version(0, 0, lo.patch + 1)


def _tilde_hi(lo: Version, given: int) -> Version:
    return Version(lo.major, lo.minor + 1, 0)
