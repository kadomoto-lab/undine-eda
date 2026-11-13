"""Operation log: what changed, by whom, why, and how to take it back.

One append-only JSON-lines file per library: <project>/history/<lib>.jsonl (committed with the design).
Each line is one *operation* (a user action or an agent step). Format, version 1:

    {"v": 1, "seq": 12, "t": "2026-10-23T01:40:12+0900",
     "actor": "user" | "agent:<name>",            # who made the change
     "cell": "inv", "view": "schematic",
     "op": "set_props",                            # the API operation (add_instance, move, delete, undo ...)
     "args": {...},                                # its arguments as sent (ids, not indexes)
     "reason": "free text" | null,                 # the intent, recorded by whoever made the change
     "changeset": "cs-..." | null,                 # groups the operations of one agent proposal
     "sha": "<sha1 of the cell file after the operation>",
     "effects": [ ... ]}                           # structural effects, below; null when not derivable

Effects are derived by comparing the file before and after the operation (so they are right for any
operation, including future ones) and are complete enough to apply them backwards or forwards:

    {"k": "i", "id": "M1", "do": "add", "index": 5, "obj": {sym, x, y, rot, flip, props}}
    {"k": "i", "id": "M1", "do": "del", "index": 5, "obj": {...}}
    {"k": "i", "id": "M1", "do": "mod", "id_after": "M1",
     "changes": {"props.W": ["2", "3"], "x": [0, 10]}}          # field: [before, after]
    {"k": "w", "id": "0,0,10,0", "do": "add" | "del", "index": 3, "obj": {x1, y1, x2, y2, props}}
    {"k": "t", "id": "x,y,text", "do": "add" | "del", "index": 0, "obj": {...}}
    {"k": "o", "id": "ports", "do": "mod", "id_after": "ports",   # the port instances reordered (the subcircuit's pin
     "changes": {"order": [["p1", "p2"], ["p2", "p1"]],          # order): their ids before / after, and their lab
                 "ports": [["in", "out"], ["out", "in"]]}}       # for a reader
    {"k": "v", "do": "add", "text": "<file text>"}               # the view's file itself was created (schematic / symbol);
                                                                 # the operation's cell / view say which. Applying it creates
                                                                 # the file, refusing if another one is there; reverting keeps it
    {"k": "v", "do": "del", "kept": true, "text": "<file text>"} # the view's file was deleted (delete_cell); reverting writes
                                                                 # it back ({"do": "add", "kept": true}: inverting that deletes
                                                                 # it again)
    {"k": "l", "do": "add", "lib": "cdac"}                       # the library itself was created (project.json, libs/<lib>;
                                                                 # its own log's first record, cell / view null); reverting
                                                                 # keeps it, a changeset's restore takes it out (changeset.py)

Undo / redo are operations too ({"op": "undo", "of": <seq>}), so the log stays append-only and the undo
stack survives a restart: it is replayed from the log.

A git merge of two branches' logs (undine.vcs.logmerge) appends the other branch's records, renumbered, and then
{"op": "merge", "actor": "git", "cell": null, "view": null, "effects": []}: the undo / redo stacks start again after it.

A changeset's working copy is a git worktree whose log starts as the project's was when it was opened: the records up
to its `floor` (the seq of the last of them) are the project's history, not the changeset's. They are not read (read),
not undone or redone (stacks), not reviewed (net_records), and a layout's journal among them is not replayed (the
working copy starts from the files). A `restore` (live.py) takes back its own changeset's records after its checkpoint
`to` (a gseq), and only those: a log can hold records of changesets applied before, with numbers of their own.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from copy import deepcopy
from pathlib import Path, PurePosixPath

from undine.schematics import xschem
from undine.schematics.xschem import XComp, XFile, XText

VERSION = 1
_COMP_FIELDS = ("sym", "x", "y", "rot", "flip", "embedded")


def sha(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


# ------------------------------------------------------------------ structural diff


def _comp_obj(c: XComp) -> dict:
    obj = {"sym": c.sym, "x": c.x, "y": c.y, "rot": c.rot, "flip": c.flip, "props": dict(c.props)}
    if c.embedded is not None:
        obj["embedded"] = xschem.dump(c.embedded, True)
    return obj


def _wire_obj(w: tuple) -> dict:
    return {"x1": w[0], "y1": w[1], "x2": w[2], "y2": w[3], "props": dict(w[4])}


def _text_obj(t: XText) -> dict:
    return {"s": t.s, "x": t.x, "y": t.y, "rot": t.rot, "flip": t.flip, "xs": t.xs, "ys": t.ys, "props": dict(t.props)}


def _text_id(t: XText) -> str:
    return f"{t.x:g},{t.y:g},{t.s}"


def _changes(a: dict, b: dict) -> dict:
    out = {}
    for k in _COMP_FIELDS:
        if a.get(k) != b.get(k):
            out[k] = [a.get(k), b.get(k)]
    pa, pb = a["props"], b["props"]
    for k in sorted(set(pa) | set(pb)):
        if pa.get(k) != pb.get(k):
            out[f"props.{k}"] = [pa.get(k), pb.get(k)]
    return out


def _is_port(c: XComp) -> bool:
    """A port instance (ipin / opin / iopin) as far as the file tells: an embedded symbol's type, else the symbol's
    file name (as vcs/schmerge.py, which has no symbol libraries either)."""
    typ = c.embedded.global_props.get("type", "") if c.embedded is not None else PurePosixPath(c.sym).stem
    return typ in ("ipin", "opin", "iopin")


def _same_comp(ca: XComp, cb: XComp) -> bool:
    return ((ca.sym, ca.x, ca.y, ca.rot, ca.flip) == (cb.sym, cb.x, cb.y, cb.rot, cb.flip)
            and ca.props == cb.props and ca.embedded == cb.embedded)


def _match_in_order(xa: list, xb: list) -> tuple[list, list]:
    """Pairs entries with equal ids, each a-entry with the first unused b-entry of its id (in b's order).
    Returns ([(a, b) ...], unmatched b entries in b's order)."""
    queues: dict[str, list[int]] = {}
    for j, e in enumerate(xb):
        queues.setdefault(e[0], []).append(j)
    pos: dict[str, int] = {}
    used = [False] * len(xb)
    pairs = []
    for e in xa:
        q, k = queues.get(e[0]), pos.get(e[0], 0)
        if q is None or k >= len(q):
            pairs.append((e, None))
            continue
        pos[e[0]] = k + 1
        used[q[k]] = True
        pairs.append((e, xb[q[k]]))
    return pairs, [e for j, e in enumerate(xb) if not used[j]]


def diff(a: XFile, b: XFile) -> list[dict]:
    """Effects that turn file `a` into file `b`."""
    fx: list[dict] = []
    # instances
    ia = dict(zip(xschem.comp_ids(a), enumerate(a.comps)))
    ib = dict(zip(xschem.comp_ids(b), enumerate(b.comps)))
    gone = {k: v for k, v in ia.items() if k not in ib}
    new = {k: v for k, v in ib.items() if k not in ia}
    for k in ia.keys() & ib.keys():
        ca, cb = ia[k][1], ib[k][1]
        if _same_comp(ca, cb):  # most instances: skip building the dicts
            continue
        ch = _changes(_comp_obj(ca), _comp_obj(cb))
        if ch:
            fx.append({"k": "i", "id": k, "do": "mod", "id_after": k, "changes": ch})
    # a rename shows up as one removed + one added instance at the same place: pair them
    for ka, (xa, ca) in list(gone.items()):
        for kb, (xb, cb) in list(new.items()):
            if (ca.sym, ca.x, ca.y, ca.rot, ca.flip) == (cb.sym, cb.x, cb.y, cb.rot, cb.flip):
                fx.append({"k": "i", "id": ka, "do": "mod", "id_after": kb,
                           "changes": _changes(_comp_obj(ca), _comp_obj(cb))})
                del gone[ka], new[kb]
                break
    for k, (i, c) in gone.items():
        fx.append({"k": "i", "id": k, "do": "del", "index": i, "obj": _comp_obj(c)})
    for k, (i, c) in new.items():
        fx.append({"k": "i", "id": k, "do": "add", "index": i, "obj": _comp_obj(c)})
    # the order of the ports is the subcircuit's pin order: reordering them is a change (other instances' order is not)
    pa = [k for k, c in zip(ia, a.comps) if k in ib and _is_port(c) and _is_port(ib[k][1])]
    pb = [k for k, c in zip(ib, b.comps) if k in ia and _is_port(c) and _is_port(ia[k][1])]
    if pa != pb:
        fx.append({"k": "o", "id": "ports", "id_after": "ports", "do": "mod",
                   "changes": {"order": [pa, pb], "ports": [[ia[k][1].props.get("lab", "") for k in pa],
                                                            [ib[k][1].props.get("lab", "") for k in pb]]}})
    # wires (identity = end points); a label change keeps the identity
    wa = [(xschem.wire_id(*w[:4]), i, w) for i, w in enumerate(a.wires)]
    wb = [(xschem.wire_id(*w[:4]), i, w) for i, w in enumerate(b.wires)]
    pairs, rest_b = _match_in_order(wa, wb)
    # Remove from the end so Undo restores the original indexes in ascending
    # order, including when a reroute replaces several consecutive segments.
    for (wid, i, w), m in reversed(pairs):
        if m is None:
            fx.append({"k": "w", "id": wid, "do": "del", "index": i, "obj": _wire_obj(w)})
        elif w[4] != m[2][4]:
            fx.append({"k": "w", "id": wid, "do": "mod", "id_after": wid,
                       "changes": {f"props.{k}": [w[4].get(k), m[2][4].get(k)]
                                   for k in sorted(set(w[4]) | set(m[2][4])) if w[4].get(k) != m[2][4].get(k)}})
    for wid, i, w in rest_b:
        fx.append({"k": "w", "id": wid, "do": "add", "index": i, "obj": _wire_obj(w)})
    # texts
    ta, tb = [(_text_id(t), i, t) for i, t in enumerate(a.texts)], [(_text_id(t), i, t) for i, t in enumerate(b.texts)]
    pairs, rest = _match_in_order(ta, tb)
    # Delete from the end, so Undo reinserts multiple moved labels in their
    # original order rather than clamping the later indexes into a shorter list.
    for (tid, i, t), m in reversed(pairs):
        if m is None:
            fx.append({"k": "t", "id": tid, "do": "del", "index": i, "obj": _text_obj(t)})
        elif t != m[2]:
            old, new = _text_obj(t), _text_obj(m[2])
            changes = {k: [old[k], new[k]] for k in old if k != "props" and old[k] != new[k]}
            changes.update({f"props.{k}": [old["props"].get(k), new["props"].get(k)]
                            for k in old["props"].keys() | new["props"].keys()
                            if old["props"].get(k) != new["props"].get(k)})
            if changes:
                fx.append({"k": "t", "id": tid, "id_after": tid, "do": "mod", "changes": changes})
    for tid, i, t in rest:
        fx.append({"k": "t", "id": tid, "do": "add", "index": i, "obj": _text_obj(t)})
    # Drawing order matters for symbol pins: layer-5 boxes define @pinlist.
    # Keep each primitive collection as an ordered, conflict-checked change.
    for field in ("lines", "boxes", "polys", "arcs"):
        old, new = _drawing_json(getattr(a, field)), _drawing_json(getattr(b, field))
        if old != new:
            fx.append({"k": "g", "id": field, "id_after": field, "do": "mod",
                       "changes": {"value": [old, new]}})
    return fx


def _drawing_json(value):
    """Normalize tuple/list differences from JSON log reloads without losing pin order."""
    return json.loads(json.dumps(value))


# ------------------------------------------------------------------ applying effects


def _mk_comp(o: dict) -> XComp:
    return XComp(o["sym"], o["x"], o["y"], o["rot"], o["flip"], dict(o["props"]),
                 xschem.parse(o["embedded"]) if "embedded" in o else None)


def _mk_wire(o: dict) -> tuple:
    return (o["x1"], o["y1"], o["x2"], o["y2"], dict(o["props"]))


def _mk_text(o: dict) -> XText:
    return XText(o["s"], o["x"], o["y"], o["rot"], o["flip"], o["xs"], o["ys"], dict(o["props"]))


def _cur(c: XComp, k: str):
    if k == "embedded":
        return xschem.dump(c.embedded, True) if c.embedded is not None else None
    return c.props.get(k[6:]) if k.startswith("props.") else getattr(c, k)


def _set_comp(c: XComp, ch: dict, side: int) -> None:
    for k, v in ch.items():
        if _cur(c, k) != v[1 - side]:  # the value must still be what the operation saw
            raise ValueError(f"conflict: {k} is {_cur(c, k)!r}, expected {v[1 - side]!r}")
        val = v[side]
        if k.startswith("props."):
            if val is None:
                c.props.pop(k[6:], None)
            else:
                c.props[k[6:]] = val
        elif k == "embedded":
            c.embedded = xschem.parse(val) if val is not None else None
        else:
            setattr(c, k, val)


def _find(lst_ids: list[str], ident: str) -> int:
    if ident not in lst_ids:
        raise KeyError(f"cannot apply: {ident} not found (the file changed outside of Undine?)")
    return lst_ids.index(ident)


def apply(f: XFile, effects: list[dict], backwards: bool) -> None:
    """Applies effects forwards (redo) or backwards (undo) to a parsed file, in place."""
    seq = list(reversed(effects)) if backwards else list(effects)
    for e in seq:
        do = e["do"]
        if backwards:
            do = {"add": "del", "del": "add", "mod": "mod"}[do]
        side = 0 if backwards else 1
        k = e["k"]
        if k == "v":
            continue  # the file itself: created by SchEditor.replay before the rest is applied
        if k == "i":
            ids = xschem.comp_ids(f)
            if do == "mod":
                # forwards the object still has its old id, backwards its new one
                cur = e["id"] if not backwards else e["id_after"]
                _set_comp(f.comps[_find(ids, cur)], e["changes"], side)
            elif do == "del":
                del f.comps[_find(ids, e["id"])]
            else:
                f.comps.insert(min(e["index"], len(f.comps)), _mk_comp(e["obj"]))
        elif k == "w":
            ids = [xschem.wire_id(*w[:4]) for w in f.wires]
            if do == "mod":
                i = _find(ids, e["id"])
                x1, y1, x2, y2, pr = f.wires[i]
                pr = dict(pr)
                for key, v in e["changes"].items():
                    if pr.get(key[6:]) != v[1 - side]:
                        raise ValueError(f"conflict: wire {key[6:]} is {pr.get(key[6:])!r}, expected {v[1 - side]!r}")
                    if v[side] is None:
                        pr.pop(key[6:], None)
                    else:
                        pr[key[6:]] = v[side]
                f.wires[i] = (x1, y1, x2, y2, pr)
            elif do == "del":
                del f.wires[_find(ids, e["id"])]
            else:
                f.wires.insert(min(e["index"], len(f.wires)), _mk_wire(e["obj"]))
        elif k == "t":
            ids = [_text_id(t) for t in f.texts]
            if do == "del":
                del f.texts[_find(ids, e["id"])]
            elif do == "add":
                f.texts.insert(min(e["index"], len(f.texts)), _mk_text(e["obj"]))
            elif do == "mod":
                _set_comp(f.texts[_find(ids, e["id"] if not backwards else e["id_after"])], e["changes"], side)
        elif k == "o":
            ids = xschem.comp_ids(f)
            was, now = e["changes"]["order"][1 - side], e["changes"]["order"][side]
            if [i for i in ids if i in set(was)] != list(was) or sorted(was) != sorted(now):
                raise ValueError("conflict: the ports were added, removed or reordered outside this operation")
            slots = [n for n, i in enumerate(ids) if i in set(was)]
            comps = {i: f.comps[n] for n, i in zip(slots, was)}
            for n, i in zip(slots, now):
                f.comps[n] = comps[i]
        elif k == "g":
            field = e["id"]
            if do != "mod" or field not in ("lines", "boxes", "polys", "arcs"):
                raise ValueError("invalid symbol drawing effect")
            value = e["changes"]["value"]
            if _drawing_json(getattr(f, field)) != value[1-side]:
                raise ValueError(f"conflict: symbol {field} changed outside this operation")
            setattr(f, field, [tuple(part) for part in deepcopy(value[side])])


def invert(effects: list[dict]) -> list[dict]:
    """Effects that undo `effects` when applied forwards (used to revert a recorded operation)."""
    out = []
    for e in reversed(effects):
        if e["k"] == "c" and e["do"] == "del" and e.get("kept"):
            out.append({**e, "do": "add", "drop": False})   # a cell dropped with its content recorded comes back
            continue
        if e["k"] == "c" and e["do"] == "add" and e.get("kept"):
            out.append({**e, "do": "del", "drop": True})    # ... and goes again (a restore that brought it back, inverted)
            continue
        if e["k"] == "v" and e.get("kept"):          # a view deleted with its text (delete_cell) comes back, and a
            out.append({**e, "do": "add" if e["do"] == "del" else "del"})    # restore that brought it back goes again
            continue
        if e["k"] in ("c", "v", "l"):
            continue  # a cell, view or library that was added stays (it may be referenced elsewhere)
        e = dict(e)
        if e["do"] == "add":
            e["do"] = "del"
        elif e["do"] == "del":
            e["do"] = "add"
        else:
            e["changes"] = {k: [v[1], v[0]] for k, v in e["changes"].items()}
            if "id_after" in e:
                e["id"], e["id_after"] = e["id_after"], e["id"]
        out.append(e)
    return out


def _before(recs: list[dict], i: int) -> list[dict]:
    """What the restore record recs[i] goes back to: the records before it but its changeset's after its checkpoint
    (a record with no changeset is a working copy's own: an edit made without one named)."""
    to, cs = recs[i].get("to") or 0, recs[i].get("changeset")
    return [r for r in recs[:i] if not ((r.get("gseq") or 0) > to and r.get("changeset") in (None, cs))]


def _net(recs: list[dict]) -> list[dict]:
    net: list[dict] = []
    byseq = {r["seq"]: r for r in recs}
    for i, r in enumerate(recs):
        if r["op"] == "restore":
            net = _net(_before(recs, i))             # back to what was in effect at the checkpoint
        elif r["op"] == "undo":
            net = [x for x in net if x["seq"] != r["of"]]
        elif r["op"] == "redo":
            if (x := byseq.get(r["of"])):
                net.append(x)
        elif r.get("effects"):
            net.append(r)
    return net


def net_records(log: "OpLog") -> list[dict]:
    """Operations that are still in effect (undone and restored-away ones removed), oldest first; saves and notes
    skipped. A `restore` record (changeset working copies, see live.py) returns to what was in effect at `to` (a gseq).
    The records up to the log's floor are not counted (module docstring)."""
    return _net(log.own())


# ------------------------------------------------------------------ the log file


def tail_lines(path: Path, chunk: int = 1 << 16):
    """The non-empty lines of a file from its end, read a chunk at a time (only as far back as the caller goes)."""
    try:
        fh = path.open("rb")
    except OSError:
        return
    with fh:
        fh.seek(0, 2)
        pos, rest = fh.tell(), b""
        while pos > 0:
            step = min(chunk, pos)
            pos -= step
            fh.seek(pos)
            block = fh.read(step) + rest
            lines = block.split(b"\n")
            rest = lines.pop(0)                      # may continue in the chunk before
            for line in reversed(lines):
                if line.strip():
                    yield line.decode("utf-8", "replace")
        if rest.strip():
            yield rest.decode("utf-8", "replace")


class OpLog:
    def __init__(self, path: Path, hooks=None, floor: int = 0):
        self.path = path
        self.lock = threading.RLock()
        self.hooks = hooks            # live.LogHooks in a changeset's working copy: write check, gseq / opid / turn
        self.floor = floor            # a changeset's working copy: the seq of the project's last record (module docstring)

    def guard(self) -> None:
        """Called by the editors before they change the design (refuses while someone else holds the write lease)."""
        if self.hooks is not None:
            self.hooks.guard()

    def _read_all(self) -> list[dict]:
        if not self.path.is_file():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # a torn last line must not make the history unreadable
        return out

    def own(self) -> list[dict]:
        """The records after the floor (all of them but in a changeset's working copy)."""
        recs = self._read_all()
        return [r for r in recs if r["seq"] > self.floor] if self.floor else recs

    def _last_seq(self) -> int:
        """The seq of the log's last record: read from its end (a log grows for the life of the project)."""
        for line in tail_lines(self.path):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue                             # a torn last line, as _read_all skips it
            if isinstance(r, dict) and isinstance(r.get("seq"), int):
                return r["seq"]
        return 0

    def append(self, rec: dict) -> dict:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            rec = {"v": VERSION, "seq": self._last_seq() + 1,
                   "t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **rec}
            if self.hooks is None:
                with self.path.open("a") as fh:
                    fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
                return rec
            with self.hooks.lock:                    # one order across the changeset's libraries
                rec.update(self.hooks.stamp())
                with self.path.open("a") as fh:
                    fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
            return rec

    def read(self, cell: str | None = None, view: str | None = None, limit: int | None = 200,
             after: int = 0) -> list[dict]:
        """The records after `after` (and the floor), of a cell / view, the newest `limit` (None: all), oldest first."""
        recs = [r for r in self._read_all() if r["seq"] > max(after, self.floor)
                and (cell is None or r.get("cell") == cell) and (view is None or r.get("view") == view)]
        return recs if limit is None else recs[-limit:]

    def stacks(self, cell: str, view: str) -> tuple[list[dict], list[dict]]:
        """(undoable, redoable) operations of a cell view, replayed from the log (newest last); never one before the
        floor."""
        return self._stacks(self.own(), cell, view)

    def _stacks(self, recs: list[dict], cell: str, view: str) -> tuple[list[dict], list[dict]]:
        done: list[dict] = []
        redo: list[dict] = []
        for i, r in enumerate(recs):
            if r["op"] == "restore" and (r.get("view") is None or r.get("view") == view):
                done, redo = self._stacks(_before(recs, i), cell, view)   # as it was at the checkpoint; no redo past it
                redo = []
                continue
            if r["op"] == "merge":     # a git merge: nothing before it is undone on top of the other branch's edits
                done, redo = [], []
                continue
            if (cell is not None and r.get("cell") != cell) or r.get("view") != view:
                continue
            if r["op"] == "undo":
                x = next((d for d in done if d["seq"] == r["of"]), None)
                if x:
                    done.remove(x)
                    redo.append(x)
            elif r["op"] == "redo":
                x = next((d for d in redo if d["seq"] == r["of"]), None)
                if x:
                    redo.remove(x)
                    done.append(x)
            elif r.get("effects"):
                done.append(r)
                redo.clear()
        return done, redo
