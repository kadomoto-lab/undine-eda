"""Three-way merge of project.json and undine.lock (docs/design/packages.md, Changesets and evidence; merge.md).

Merged as lines, two changesets that each add a dependency touch neighbouring lines of both files: a line conflict,
or a lock that git merged cleanly into something that matches neither side's installed trees. Here each file is
merged as JSON, entry by entry, with merge3's rule for each value:

    project.json   `libraries` and `indexes` entry by entry (each entry one value); `trusted` as a set (a key either
                   side added is kept, one either side removed is gone); every other key one value (`pdk`, `name`)
    undine.lock    `libraries` and `packages` entry by entry, `tools` tool by tool; every other key one value
                   (`format`, `pdk`, `profile`)

Two sides that changed one entry differently conflict on that entry (ours is kept), so two changesets adding different
packages merge cleanly in either order, and two that move one package to different versions conflict there. Whether
the merged lock still pins what the merged project.json asks is not this file's question: the adopt checks that
(changeset.py, _dependency_conflicts) and refuses it with the reason.

The merged files are written as Undine writes them: project.json as Project.save (indent 2, keys in order), the lock
as deps.write_lock (indent 2, sorted keys).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from undine.vcs.merge3 import MISSING, Merge, Result, describe, mapping, order

KINDS = {"project.json": "project", "undine.lock": "lock"}
_TABLES = {"project": {"libraries": ("library:", "library "), "indexes": ("index:", "index ")},
           "lock": {"libraries": ("lock:library:", "locked library "), "packages": ("lock:package:", "locked package "),
                    "tools": ("lock:tool:", "tool ")}}


def kind_of(path: str | Path) -> str | None:
    """"project" for project.json, "lock" for undine.lock, else None."""
    return KINDS.get(Path(str(path)).name)


def _read(data: bytes | None, kind: str) -> dict:
    if data is None:
        return {}
    try:
        v = json.loads(data)
    except ValueError as e:
        raise ValueError(f"not JSON: {e}") from None
    if not isinstance(v, dict):
        raise ValueError("not a JSON object")
    for key in _TABLES[kind]:
        if key in v and not isinstance(v[key], dict):
            raise ValueError(f"its {key} is not an object")
    return v


def _dump(v: dict, kind: str) -> bytes:
    return (json.dumps(v, indent=2, sort_keys=kind == "lock") + "\n").encode()


def _set(m: Merge, cid: str, what: str, b: Any, o: Any, t: Any) -> Any:
    """A list of keys merged as a set: what either side added, less what either side removed, in merge3.order."""
    if not all(isinstance(x, list) for x in (o, t)) or not isinstance(b if b is not MISSING else [], list):
        return m.value(cid, what, b, o, t)
    base = [] if b is MISSING else b
    gone = (set(base) - set(o)) | (set(base) - set(t))
    keep = (set(o) | set(t)) - gone
    out = order(base, o, t, keep)
    if [x for x in out if x not in o]:
        m.combined.append(what)
    return out


def merge(base: bytes | None, ours: bytes, theirs: bytes, prefer: dict[str, str] | None = None,
          kind: str | None = None, path: str | Path | None = None, **kw) -> Result:
    """Merges project.json or undine.lock; `kind` names which, or `path` does by its name."""
    k = kind or (kind_of(path) if path is not None else None)
    if k not in _TABLES:
        raise ValueError(f"not project.json or undine.lock: {kind or path}")
    m = Merge(prefer)
    if ours == theirs:
        return m.result(ours, k)
    try:
        b, o, t = _read(base, k), _read(ours, k), _read(theirs, k)
    except ValueError as e:
        side = m.conflict("file", f"{'project.json' if k == 'project' else 'undine.lock'} cannot be read ({e})",
                          None, None, None)
        return m.result(theirs if side == "theirs" else ours, k)
    tables = _TABLES[k]

    def one(key: str, bv: Any, ov: Any, tv: Any) -> Any:
        if key in tables:
            prefix, words = tables[key]
            return mapping(m, prefix, words, bv if isinstance(bv, dict) else {}, ov, tv)
        if k == "project" and key == "trusted":
            return _set(m, "trusted", "trusted", bv, ov, tv)
        return m.value(key, key, bv, ov, tv)
    out = mapping(m, "", "", b, o, t, one)
    return m.result(_dump(out, k), k)


def diff(old: bytes | None, new: bytes | None, kind: str | None = None, path: str | Path | None = None,
         **kw) -> dict:
    """The changes from `old` to `new`, entry by entry: [{do, id, what, before, after}]."""
    k = kind or (kind_of(path) if path is not None else None)
    if k not in _TABLES:
        raise ValueError(f"not project.json or undine.lock: {kind or path}")
    a, b = _read(old, k), _read(new, k)
    out: list[dict] = []
    tables = _TABLES[k]

    def change(cid: str, what: str, x: Any, y: Any) -> None:
        do = "add" if x is MISSING else "del" if y is MISSING else "mod"
        out.append({"do": do, "id": cid, "what": what, "before": describe(x), "after": describe(y)})
    for key in order(a, b, a, set(a) | set(b)):
        x, y = a.get(key, MISSING), b.get(key, MISSING)
        if x == y:
            continue
        xa, yb = (x if x is not MISSING else {}), (y if y is not MISSING else {})
        if key in tables and isinstance(xa, dict) and isinstance(yb, dict):
            prefix, words = tables[key]
            for e in order(xa, yb, xa, set(xa) | set(yb)):
                if xa.get(e, MISSING) != yb.get(e, MISSING):
                    change(f"{prefix}{e}", f"{words}{e}", xa.get(e, MISSING), yb.get(e, MISSING))
        else:
            change(key, key, x, y)
    return {"kind": k, "changes": out}
