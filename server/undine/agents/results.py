"""What a reader sees of a tool: its result fitted to a budget as valid JSON (agent_tools.call on the server, `undine
call` as it prints), and its argument schemas in one line (shape).

A result longer than its budget is shortened inside, never cut through. The budget is shared out from the top: the
parts of a value that fit their share stay whole, and the largest give way, each shortened to the same size by going
into it (a record's long fields, the few large results of a batch). A list, or an object whose values are all of one
kind (instances by name), lists its first parts instead where it would leave a part less than MIN_PART; a long string
keeps its start. Each cut says so in place:

    "instances": {"M1": {...}, "M2": {...}, "…": "114 more (116 in all)"}
    "rows": [[0, 0.1], [1e-9, 0.2], "… 510 more (512 in all)"]
    "value": ".control\\nrun\\n.meas tran tdel… (2210 more characters)"

Records (an object of different kinds of values, such as a result's own fields) keep every key. The result gets
`_truncated`: its full size, the budget, what was cut ("kept of total" by path) and how to get the rest (a tool's
filters or offset, or a larger budget). A part is measured only when it is looked at, so a result of megabytes is
fitted in a fraction of a second. Standard library only: the shell client uses it too.
"""
from __future__ import annotations

import json
import re
from itertools import islice

MARK = "…"
MIN_TEXT = 200          # a string is not shortened below this many characters
MIN_PART = 1000         # nor a part of a list below about this many: fewer parts are listed instead
CUTS_NAMED = 20         # the cuts _truncated names; the others are counted


def compact(v) -> str:
    return json.dumps(v, ensure_ascii=False, separators=(",", ":"))


def text(v, limit: int | None, how: str = "", dumps=compact) -> str:
    """The result as a reader gets it: a string as it is (cut at the end: it is not JSON), anything else as JSON of at
    most `limit` characters (fit). None or 0: the whole result."""
    if isinstance(v, str):
        if not limit or len(v) <= limit:
            return v
        return v[:limit] + f"\n{MARK} (truncated, {len(v) - limit} more characters{'; ' + how if how else ''})"
    return dumps(fit(v, limit, how, dumps) if limit else v)


def fit(v, limit: int, how: str = "", dumps=compact):
    """v shortened until dumps(v) has at most `limit` characters (see the module docstring); v itself if it fits. When
    nothing is left to shorten (a record of many small fields) the result is valid and over the budget."""
    s = dumps(v)
    if len(s) <= limit:
        return v
    w, m = json.loads(s), {}                         # a copy in JSON's own types
    if dumps is compact and isinstance(w, (dict, list)):
        m[id(w)] = len(s)                            # measured already
    if not isinstance(w, dict):
        w = {"result": w}
    budget = limit * _size(w, m) / len(s)            # in characters of compact JSON
    fits = over = lo = hi = None
    for _ in range(10):         # the budget corrected for _truncated and for what dumps (indented) makes of it
        cuts: dict[tuple, tuple] = {}
        out = {**_shrink(w, int(budget), (), cuts, m, True), "_truncated": {
            "chars": len(s), "max_chars": limit, "cut": _named(cuts), **({"how": how} if how else {})}}
        n = len(dumps(out))
        if n <= limit:
            fits = (n, out) if fits is None or n > fits[0] else fits
            lo = budget if lo is None else max(lo, budget)
            if n >= limit - max(limit // 50, 20):
                break
        else:
            over = (n, out) if over is None or n < over[0] else over
            hi = budget if hi is None else min(hi, budget)
        if lo is not None and hi is not None:          # between a budget that fits and one that does not
            if hi - lo < 2:
                break
            budget = (lo + hi) / 2
        else:
            r = len(compact(out)) / n
            budget += (limit - n) * r * 0.9 if n < limit else (limit - n - limit / 100) * r
            if budget < 0:                           # nothing more gives way
                break
    return (fits or over)[1]


def _size(x, m: dict) -> int:
    """len(compact(x)); a container's kept in m by id."""
    if isinstance(x, str):
        return _len(x)
    if isinstance(x, (dict, list)):
        if (n := m.get(id(x))) is None:
            n = m[id(x)] = len(compact(x))
        return n
    return len(compact(x)) if isinstance(x, bool) or x is None else len(repr(x))


def _least(x, m: dict) -> int:
    """The shortest _shrink makes x: a string MIN_TEXT long, a collection its first part, a record its parts at their
    least (kept in m)."""
    if isinstance(x, str):
        return min(_len(x), _len(x[:MIN_TEXT]) + len(f"{MARK} ({len(x)} more characters)"))
    if not isinstance(x, (dict, list)):
        return _size(x, m)
    if (n := m.get((id(x),))) is None:
        if len(x) > 1 and _collection(x, False):
            k, first = next(_items(x))
            n = min(_size(x, m), 2 + _key(x, k) + _least(first, m) + _marked(x, len(x) - 1, len(x)))
        else:
            n = _size(x, m) - sum(_size(p, m) - _least(p, m) for _, p in _items(x))
        m[(id(x),)] = n
    return n


def _items(v):
    """A container's parts, (key or index, value), as they are needed."""
    return iter(v.items()) if isinstance(v, dict) else enumerate(v)


def _key(v, k) -> int:
    """The length of a part's key in compact JSON ("M1":), 0 in a list."""
    return _len(k) + 1 if isinstance(v, dict) else 0


def _len(s: str) -> int:
    """len(compact(s)), without encoding a string that needs no escapes."""
    return len(s) + 2 if s.isprintable() and '"' not in s and "\\" not in s else len(compact(s))


def _collection(v, root: bool) -> bool:
    """Shortened by listing its first parts: a list, or an object whose values are all of one JSON kind (instances by
    name ...)."""
    if isinstance(v, list):
        return True
    if root or not isinstance(v, dict):
        return False
    return len({type(x) if isinstance(x, (dict, list, str)) else float for x in v.values()}) == 1


def _marked(v, more: int, n: int) -> int:
    """The length of a collection's marker, with its comma."""
    return 1 + (len(MARK) + 3 + _len(f"{more} more ({n} in all)") if isinstance(v, dict) else
                _len(f"{MARK} {more} more ({n} in all)"))


def _shrink(v, b: int, path: tuple, cuts: dict, m: dict, root: bool = False):
    """v made at most about b characters of compact JSON: its parts share b, the largest shortened to one size
    (_level); a collection keeps its first parts (_count). cuts gets each cut by path: (total, kept, unit)."""
    if isinstance(v, str):
        n, size = len(v), _len(v)
        if size <= b:
            return v
        keep = max(MIN_TEXT, int((b - 2 - len(f"{MARK} ({n} more characters)")) * n / size))   # escapes count twice
        if keep >= n:
            return v
        cuts[path] = (n, keep, " characters")
        return v[:keep] + f"{MARK} ({n - keep} more characters)"
    if not isinstance(v, (dict, list)) or _size(v, m) <= b:
        return v
    n = len(v)
    k = _count(v, b, m) if n > 1 and _collection(v, root) else n
    if k < n:
        cuts[path] = (n, k, "")
    parts = [(key, x, _size(x, m) + kn, _least(x, m) + kn, kn)
             for key, x in islice(_items(v), k) for kn in [_key(v, key)]]
    level = _level(parts, b - 1 - k - (_marked(v, n - k, n) if k < n else 0))
    kept = [(key, x if s <= level else _shrink(x, max(level, least) - kn, (*path, key), cuts, m))
            for key, x, s, least, kn in parts]
    if isinstance(v, dict):
        return {**dict(kept), **({MARK: f"{n - k} more ({n} in all)"} if k < n else {})}
    return [x for _, x in kept] + ([f"{MARK} {n - k} more ({n} in all)"] if k < n else [])


def _count(v, b: int, m: dict) -> int:
    """How many of a collection's first parts fit in b: whole, or the large ones shortened to MIN_PART (at least one;
    with fewer parts, each gets more). Measures only the parts it looks at: a long list is not measured to its end."""
    n, total, fit = len(v), 0, 1
    for k, (key, x) in enumerate(_items(v), 1):
        kn, s = _key(v, key), _size(x, m)
        total += kn + (s if kn + s <= MIN_PART else max(_least(x, m), MIN_PART - kn))
        if total + 1 + k + (_marked(v, n - k, n) if k < n else 0) <= b:
            fit = k
        elif total > b:
            break
    return fit


def _level(parts: list, room: int) -> float:
    """The size the largest parts are shortened to, so that all of them take at most room (a part never below its
    least); inf when they fit whole."""
    if sum(p[2] for p in parts) <= room:
        return float("inf")
    lo, hi = 0, max(p[2] for p in parts)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if sum(max(p[3], min(p[2], mid)) for p in parts) <= room:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _named(cuts: dict) -> dict:
    named = {_name(p): f"{k} of {n}{u}" for p, (n, k, u) in list(cuts.items())[:CUTS_NAMED]}
    return {**named, MARK: f"{len(cuts) - CUTS_NAMED} more cuts"} if len(cuts) > CUTS_NAMED else named


def shape(s: dict) -> str:
    """A JSON schema as a reader writes it: string, instance|wire, [string, ...], {kind: instance|wire, id: string}
    (? marks what is optional)."""
    if "enum" in s:
        return "|".join(map(str, s["enum"]))
    if "anyOf" in s:
        return " or ".join(shape(x) for x in s["anyOf"])
    t = s.get("type")
    if t == "array":
        return f"[{shape(s['items'])}, ...]" if s.get("items") else "[...]"
    if t == "object" and s.get("properties"):
        req = set(s.get("required") or [])
        return "{" + ", ".join(f"{k}{'' if k in req else '?'}: {shape(x)}" for k, x in s["properties"].items()) + \
            (", ..." if s.get("additionalProperties") is not False else "") + "}"
    if t == "object" and isinstance(s.get("additionalProperties"), dict):
        return "{name: " + shape(s["additionalProperties"]) + "}"
    return t or "any"


def _name(path: tuple) -> str:
    """A path as a reader writes it: instances.M1, results[3].result, signals["v(x1.b9)"]."""
    out = ""
    for k in path:
        if isinstance(k, int):
            out += f"[{k}]"
        elif re.fullmatch(r"[A-Za-z_][\w-]*", k):
            out += f".{k}" if out else k
        else:
            out += f"[{json.dumps(k, ensure_ascii=False)}]"
    return out
