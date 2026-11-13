"""Versions and version requirements of packages: strict SemVer 2.0.0 and the requirement forms phase 2 needs.

A git requirement has exactly one candidate (the tree at its rev), so there is nothing for a range to choose among:
a requirement only checks the label a manifest's [package] version gives. Accepted forms, cargo's meanings:

    1.2.3, 1.2     caret (bare)        ^1.2.3   >=1.2.3, <2.0.0      ^0.3   >=0.3.0, <0.4.0
    ~1.2           >=1.2.0, <1.3.0     =1.2.3   exactly 1.2.3        *      any release

Comparators (>=, <) and comma lists come with the package index (phase 3), where there are versions to choose from.
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
_RANGE_HINT = "version ranges come with the package index (phase 3): use ^, ~ or ="


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
    """A version requirement: versions v with lo <= v < hi (hi None: no upper bound; `any`: every release).
    `text` is the requirement as written."""
    text: str
    lo: Version | None = None
    hi: Version | None = None

    @classmethod
    def parse(cls, s) -> Req:
        if not isinstance(s, str):
            raise SemverError(f"{s!r} is not a version requirement: a string such as \"^1.2\"")
        t = s.strip()
        if "," in t or t[:1] in (">", "<", "!"):
            raise SemverError(f"{s!r}: {_RANGE_HINT}")
        if t == "*":
            return cls(t)
        op, rest = (t[0], t[1:].strip()) if t[:1] in ("^", "~", "=") else ("", t)
        if op == "=":
            v = Version.parse(rest)
            return cls(t, v, v)
        lo, given = _lower(rest, s)
        if op == "~" and given < 2:
            raise SemverError(f"{s!r}: ~ needs MAJOR.MINOR, e.g. ~1.2 (for any 1.x write ^1)")
        if op == "" and given < 2:
            raise SemverError(f"{s!r}: a bare version needs MAJOR.MINOR, e.g. 1.2 (for any {rest}.x write ^{rest})")
        return cls(t, lo, _tilde_hi(lo, given) if op == "~" else _caret_hi(lo, given))

    def matches(self, v: Version) -> bool:
        if self.lo is None:                       # '*': any release
            return not v.pre
        if v.pre and not (self.lo.pre and self.lo.release == v.release):
            return False                          # a pre-release only for a requirement that names one of its release
        if self.lo == self.hi:                    # '='
            return v == self.lo
        return self.lo <= v and (self.hi is None or v < self.hi)

    def __str__(self) -> str:
        return self.text


def _lower(rest: str, s: str) -> tuple[Version, int]:
    """The lower bound of a caret or tilde requirement, and how many of MAJOR.MINOR.PATCH it gives."""
    m = _PARTIAL.match(rest)
    if m:
        given = sum(1 for g in m.groups() if g is not None)
        return Version(int(m[1]), int(m[2] or 0), int(m[3] or 0)), given
    if _VERSION.match(rest):
        return Version.parse(rest), 3
    raise SemverError(f"{s!r} is not a version requirement: ^, ~ or = and a version, e.g. ^1.2 or =1.2.0")


def _caret_hi(lo: Version, given: int) -> Version:
    # ^ keeps the leftmost non-zero part of what is given: ^1.2 <2.0.0, ^0.3 <0.4.0, ^0.0.3 <0.0.4, ^0.0 <0.1.0
    if lo.major > 0 or given == 1:
        return Version(lo.major + 1, 0, 0)
    if lo.minor > 0 or given == 2:
        return Version(0, lo.minor + 1, 0)
    return Version(0, 0, lo.patch + 1)


def _tilde_hi(lo: Version, given: int) -> Version:
    return Version(lo.major, lo.minor + 1, 0)
