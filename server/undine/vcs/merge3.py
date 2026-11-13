"""Three-way merge of values, mappings and multisets: the rules every view's merge is built from (docs/design/merge.md).

One rule for every value (an attribute, a placement, a shape count, a whole collection), with MISSING for an object a
side does not have:

    base  ours  theirs   result
    b     b     t        t
    b     o     b        o
    b     x     x        x          both made the same change
    b     o     t        conflict: ours is kept, theirs reported (or the side `prefer` names for that conflict id)

A view's merge walks its objects by identity and calls `Merge.value` for each value; what it combined, what conflicted
and what it warns about end up in the Merge, and the view returns a Result with the merged file.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


class _Missing:
    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False

    def __reduce__(self):            # deepcopy / pickle keep the one instance
        return "MISSING"


MISSING: Any = _Missing()


@dataclass
class Conflict:
    """A value both sides changed differently. `id` is stable for the same three inputs (resolve names it)."""
    id: str
    what: str                         # in words: "instance M1: placement"
    base: Any = None                  # JSON-able descriptions of the three values (None for missing)
    ours: Any = None
    theirs: Any = None
    taken: str = "ours"               # the side the merged file has
    resolved: bool = False            # `prefer` decided it (undine resolve)

    def to_json(self) -> dict:
        return {"id": self.id, "what": self.what, "base": self.base, "ours": self.ours, "theirs": self.theirs,
                "taken": self.taken, "resolved": self.resolved}


@dataclass
class Result:
    data: bytes                       # the merged file
    kind: str                         # schematic | symbol | layout | netlist | intent | log
    conflicts: list[Conflict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    combined: list[str] = field(default_factory=list)   # what came from theirs (or from both), in words

    @property
    def open_conflicts(self) -> list[Conflict]:
        """Conflicts no `prefer` entry decided (the merged file has ours for them)."""
        return [c for c in self.conflicts if not c.resolved]

    @property
    def clean(self) -> bool:
        return not self.open_conflicts

    def to_json(self) -> dict:
        return {"kind": self.kind, "clean": self.clean, "conflicts": [c.to_json() for c in self.conflicts],
                "warnings": self.warnings, "combined": self.combined}


def describe(v: Any) -> Any:
    """A value as the report shows it: MISSING as None, tuples as lists, everything else str() unless JSON-able."""
    if v is MISSING:
        return None
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple)):
        return [describe(x) for x in v]
    if isinstance(v, dict):
        return {str(k): describe(x) for k, x in v.items()}
    return str(v)


class Merge:
    """The state of one file's merge: decisions taken by `prefer` ({conflict id: "ours" | "theirs" | "base"}), and
    the report."""

    def __init__(self, prefer: dict[str, str] | None = None):
        self.prefer = dict(prefer or {})
        self.conflicts: list[Conflict] = []
        self.warnings: list[str] = []
        self.combined: list[str] = []

    def value(self, cid: str, what: str, b: Any, o: Any, t: Any, show: Callable[[Any], Any] = describe) -> Any:
        """The merged value of one thing (see the module docstring). Records a conflict when both sides changed it."""
        if o == t:
            return o
        if o == b:
            if t != b:
                self.combined.append(what)
            return t
        if t == b:
            return o
        side = self.conflict(cid, what, b, o, t, show, base=True)
        return b if side == "base" else t if side == "theirs" else o

    def conflict(self, cid: str, what: str, b: Any, o: Any, t: Any, show: Callable[[Any], Any] = describe,
                 base: bool = False) -> str:
        """Records a conflict found by a view's own rule; returns the side to take ("ours" unless prefer says otherwise).
        `prefer` may also say "base" (the merge of a criss-cross merge's bases, gitmerge._virtual_base): only a caller
        that can take the base's value passes `base`; for the others the conflict stays open, ours taken."""
        side = self.prefer.get(cid, "ours")
        if side == "base" and not base:
            side = "ours"
            self.conflicts.append(Conflict(cid, what, show(b), show(o), show(t), side, False))
            return side
        self.conflicts.append(Conflict(cid, what, show(b), show(o), show(t), side, cid in self.prefer))
        return side

    def warn(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)

    def result(self, data: bytes, kind: str) -> Result:
        return Result(data, kind, self.conflicts, self.warnings, self.combined)


# ---------------------------------------------------------------- collections


def order(base: Iterable, ours: Iterable, theirs: Iterable, keep: set) -> list:
    """The merged order of keys: ours' order, then each key only theirs has inserted after its nearest predecessor in
    theirs that is in the result (at the start when there is none). Keys not in `keep` (deleted) are left out."""
    out = [k for k in ours if k in keep]
    have = set(out)
    prev = None
    for k in theirs:
        if k in keep and k not in have:
            out.insert(0 if prev is None else out.index(prev) + 1, k)
            have.add(k)
        if k in have:
            prev = k
    for k in base:                       # kept keys neither side lists (cannot happen for well-formed input)
        if k in keep and k not in have:
            out.append(k)
            have.add(k)
    return out


def mapping(m: Merge, prefix: str, what: str, b: dict, o: dict, t: dict,
            merge_one: Callable[[str, Any, Any, Any], Any] | None = None) -> dict:
    """Merges dicts key by key (ordered like `order`). `merge_one(key, b, o, t)` merges a key all three sides have
    (or that both sides added); by default the key's value is one value. A key deleted on one side and changed on the
    other is a conflict (delete / modify). MISSING results are dropped."""
    keys = list(dict.fromkeys([*b, *o, *t]))
    res: dict = {}
    for k in keys:
        bv, ov, tv = b.get(k, MISSING), o.get(k, MISSING), t.get(k, MISSING)
        if merge_one is not None and ov is not MISSING and tv is not MISSING and (bv is not MISSING or ov != tv):
            v = merge_one(k, bv, ov, tv)
        else:
            v = m.value(f"{prefix}{k}", f"{what}{k}", bv, ov, tv)
        if v is not MISSING:
            res[k] = v
    return {k: res[k] for k in order(b, o, t, set(res))}


def multiset(m: Merge, prefix: str, what: Callable[[Any], str], b: Counter, o: Counter, t: Counter) -> Counter:
    """Merges counts per element (a shape that is its own identity): ours + theirs - base when one side kept the base
    count, the same count when both agree, else a conflict (both sides changed how many there are)."""
    out: Counter = Counter()
    for k in set(b) | set(o) | set(t):
        n = m.value(f"{prefix}{k}", what(k), b.get(k, 0), o.get(k, 0), t.get(k, 0))
        if n > 0:
            out[k] = n
    return out


def regions(m: Merge, prefix: str, where: str, noun: str, b: Counter, o: Counter, t: Counter,
            touching: Callable[[list], Iterable[tuple[Any, Any]]], move_key: Callable[[Any], Any],
            position: Callable[[Any], tuple[float, float]], show: Callable[[Any], Any] = describe) -> Counter:
    """Merges objects whose identity is their value (layout shapes, unnamed instances, schematic wires) region by region.

    An object cannot be modified, only replaced: an edit is a removed object and an added one, so per object counts
    (`multiset`) would keep both sides' edits of one object (two versions of a moved contact). Instead the changed objects
    (removed or added by either side) are grouped into regions: two changed objects are in one region when they touch
    (`touching(changed)` yields such pairs; the view decides what touching is), and an object a side added is in the
    region of the object it replaces when it is a copy of one that side removed (same `move_key`), the nearest such one
    (`position`; in a large group moved farther than it is spread, the one at its place in the group: `_match`), each
    removed object taken by one added copy at most: a move. Then per region:

        only one side changed it, or both changed it alike     that side's version
        both changed it differently                           a conflict, decided for the whole region (ours unless
                                                              `prefer` says theirs): that side's version of the region

    So one side's edit is never combined with the other side's edit of the same place, a deletion is never lost to the
    other side's move, and nothing is kept twice. The conflict id is `prefix` + the region's first removed object (its
    first object when it only has additions)."""
    changed = sorted((v for v in {*b, *o, *t} if o[v] != b[v] or t[v] != b[v]), key=str)
    out = Counter({v: n for v, n in o.items() if n > 0})
    if not changed:
        return out
    parent = {v: v for v in changed}

    def find(v):
        while parent[v] != v:
            parent[v] = parent[parent[v]]
            v = parent[v]
        return v

    def union(v, w):
        rv, rw = find(v), find(w)
        if rv != rw:
            parent[max(rv, rw, key=str)] = min(rv, rw, key=str)

    for v, w in touching(changed):
        union(v, w)
    for side in (o, t):
        for v, w in _moves([v for v in changed if side[v] < b[v]], [v for v in changed if side[v] > b[v]],
                           move_key, position):
            union(v, w)
    groups: dict = {}
    for v in changed:
        groups.setdefault(find(v), []).append(v)
    taken: list = []                                         # what theirs' regions bring, for one combined line
    for group in sorted(groups.values(), key=lambda g: str(g[0])):
        if all(t[v] == b[v] for v in group) or all(t[v] == o[v] for v in group):
            continue                                         # ours' version (theirs did nothing here, or the same)
        if all(o[v] == b[v] for v in group):
            side = "theirs"
            taken += group
        else:
            removed = [v for v in group if b[v] > 0]
            rep = (removed or group)[0]
            more = len(group) - 1

            def edit(x: Counter) -> dict:
                return {"removed": [show(v) for v in group if x[v] < b[v]], "added": [show(v) for v in group if x[v] > b[v]]}

            def did(x: Counter) -> str:
                rem, add = any(x[v] < b[v] for v in group), any(x[v] > b[v] for v in group)
                return "changed" if rem and add else "removed" if rem else "added"
            how = f"both sides {did(o)}" if did(o) == did(t) else f"ours {did(o)}, theirs {did(t)}"
            side = m.conflict(f"{prefix}{rep}", f"{where}: {how} {noun} {show(rep)}"
                              + (f" (and {more} more {noun}{'s' if more > 1 else ''} in its region)" if more else ""),
                              [show(v) for v in removed], edit(o), edit(t), base=True)
        src = b if side == "base" else t if side == "theirs" else o
        for v in group:
            if src[v] > 0:
                out[v] = src[v]
            else:
                out.pop(v, None)
    if taken:
        m.combined.append(f"{where}: {noun}s from theirs, {_edit(taken, b, t)}")
    return out


def _edit(group: list, b: Counter, x: Counter) -> str:
    added = sum(max(0, x[v] - b[v]) for v in group)
    removed = sum(max(0, b[v] - x[v]) for v in group)
    return ", ".join(p for p in (f"{added} added" if added else "", f"{removed} removed" if removed else "") if p)


def _moves(removed: list, added: list, key: Callable[[Any], Any], position: Callable[[Any], tuple[float, float]]
           ) -> list[tuple[Any, Any]]:
    """(removed, added) pairs of one side: each added object with the nearest removed copy of it (same key) that no
    nearer added copy took. Greedy by distance, ties by value: deterministic. Large groups (a shifted bus, a moved via
    field, a block moved far) are paired ring by ring instead of over all pairs (`_match`)."""
    by_key: dict = {}
    for v in removed:
        by_key.setdefault(key(v), ([], []))[0].append(v)
    for v in added:
        if key(v) in by_key:
            by_key[key(v)][1].append(v)
    pairs: list = []
    for rem, add in by_key.values():
        if add:
            _match(sorted(rem, key=str), sorted(add, key=str), {v: position(v) for v in (*rem, *add)}, pairs)
    return pairs


_ALL_PAIRS = 20000      # (removed, added) pairs one pairing may test, plus _PER_COPY per copy
_PER_COPY = 32
_SHIFTS = 4             # how often the added copies left are shifted onto the removed ones at most


def _most(rem: list, add: list) -> int:
    """How many candidate pairs one pairing (all pairs, or a ring round) may test."""
    return _ALL_PAIRS + _PER_COPY * (len(rem) + len(add))


def _match(rem: list, add: list, pos: dict, pairs: list) -> None:
    """Pairs the removed and added copies of one key (sorted by value), nearest first (`_pair`), into `pairs`.

    A group with few pairs (`_most`) is paired over all of them. A large one ring by ring on a grid: a round pairs each
    added copy with the removed ones within one cell of it (`_ring`), then the cell grows fourfold, starting from about
    the spacing of the removed copies. The cell stops growing once it is as wide as the spread of the removed copies
    left (a wider ring only reaches added copies farther away, each with every removed copy: all pairs), or when a
    round would test more pairs than it may. The copies left were then moved farther than they are spread (a block
    moved far, a field moved by more than its width): the added ones' positions are shifted, for pairing only, by the
    difference of the two groups' centroids, which puts each on the copy it was moved from, and the rings start again
    from the spacing. A shift is repeated (`_SHIFTS` times at most) only after one that paired something; what no
    shift brings together is not guessed at: those copies are made one region (any edit of the other side among them
    is then a conflict, never undone in silence).
    Deterministic: ties go by value."""
    pos = dict(pos)                                              # shifted below: positions for pairing only
    name = {v: str(v) for v in pos}
    cell, shifts, paired = _spacing(rem, add, pos), 0, True
    while add and rem and len(rem) * len(add) > _most(rem, add):
        wide = cell >= _spread([pos[r] for r in rem])            # a ring holds every removed copy left
        cands = _ring(rem, add, pos, name, cell)
        if cands is not None:
            left = len(add)
            rem, add = _pair(cands, rem, add, pairs)
            paired = paired or len(add) < left
            if not wide or len(rem) * len(add) <= _most(rem, add):
                cell *= 4
                continue
        if not paired or shifts == _SHIFTS:
            break
        (rx, ry), (ax, ay) = _centroid([pos[r] for r in rem]), _centroid([pos[a] for a in add])
        for a in add:
            pos[a] = (pos[a][0] + rx - ax, pos[a][1] + ry - ay)
        cell, shifts, paired = _spacing(rem, add, pos), shifts + 1, False
    if add and rem and len(rem) * len(add) > _most(rem, add):    # no pairing to trust: all of them one region
        first = min(rem, key=lambda v: (pos[v], name[v]))
        pairs += [(first, v) for v in (*rem, *add) if v != first]
    elif add and rem:
        _pair([(_dist(pos[a], pos[r]), name[a], name[r], a, r) for a in add for r in rem], rem, add, pairs)


def _ring(rem: list, add: list, pos: dict, name: dict, cell: float) -> list | None:
    """One round's candidate (distance, ..., added, removed) pairs: each added copy with the removed ones at most `cell`
    away in x and in y (found in the 3 x 3 grid cells around it). None when they would be more than a round may test."""
    grid: dict = {}
    for r in rem:
        grid.setdefault((pos[r][0] // cell, pos[r][1] // cell), []).append(r)
    most = _most(rem, add)
    cands: list = []
    for a in add:
        x, y = pos[a]
        gx, gy = x // cell, y // cell
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for r in grid.get((gx + dx, gy + dy), ()):
                    if max(abs(x - pos[r][0]), abs(y - pos[r][1])) <= cell:   # within this ring
                        cands.append((_dist(pos[a], pos[r]), name[a], name[r], a, r))
        if len(cands) > most:
            return None
    return cands


def _spread(ps: list[tuple[float, float]]) -> float:
    xs, ys = [p[0] for p in ps], [p[1] for p in ps]
    return max(max(xs) - min(xs), max(ys) - min(ys))


def _spacing(rem: list, add: list, pos: dict) -> float:
    """About the distance between neighbouring copies, from the removed copies' bounding box (an area, or a line) and
    the larger count; at least 1."""
    xs, ys = [pos[r][0] for r in rem], [pos[r][1] for r in rem]
    w, h, n = max(xs) - min(xs), max(ys) - min(ys), max(len(rem), len(add))
    return max((w * h / n) ** 0.5, max(w, h) / n, 1)


def _centroid(ps: list[tuple[float, float]]) -> tuple[float, float]:
    return sum(p[0] for p in ps) / len(ps), sum(p[1] for p in ps) / len(ps)


def _dist(p: tuple[float, float], q: tuple[float, float]) -> float:
    return (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2


def _pair(cands: list, rem: list, add: list, pairs: list) -> tuple[list, list]:
    """Takes candidate (distance, ..., added, removed) pairs nearest first, each object once; returns what is left."""
    cands.sort(key=lambda c: c[:3])
    used_r, used_a = set(), set()
    for _, _, _, a, r in cands:
        if a not in used_a and r not in used_r:
            used_a.add(a)
            used_r.add(r)
            pairs.append((r, a))
    return [r for r in rem if r not in used_r], [a for a in add if a not in used_a]
