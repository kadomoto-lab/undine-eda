"""Schematic editing on the server.

Edits are applied to the Xschem file model and written through to the .sch
file (so Xschem can open the same directory). An in-memory snapshot stack per
file provides undo / redo.
"""
from __future__ import annotations

import math
import re
import threading
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from undine.schematics import netlist, schematic, schroute, symedit, xschem
from undine.store import oplog
from undine.store.oplog import OpLog
from undine.schematics.xschem import XComp, XFile, XText

# nets the agent connects with labels rather than wires: supplies and ground, and nets with many pins
POWER_NET = re.compile(r"^(v(dd|ss|cc|ee|pwr|gnd)\w*|a?gnd\w*|[ad]v(dd|ss)\w*|0)$", re.I)
MAX_WIRED_PINS = 8

# seconds (of the routing thread's CPU time: schroute.time_budget) a drag or wire preview may spend routing before it
# shows the position as blocked
PREVIEW_BUDGET = 0.25
# seconds a move or a routed wire may spend routing before it is refused; routes that exist are found in well under
# 0.1 s, so only searches that would fail after exhausting the router reach it
EDIT_BUDGET = 2.0


def _nearest(point: tuple[float, float], points: list[tuple[float, float]], wires: list[tuple]) -> tuple[float, float]:
    """The point of a net's tree (its pins and wires) closest to `point`, on the 10-unit grid along a wire."""
    best, dist = points[0], math.inf
    for p in points:
        d = abs(p[0] - point[0]) + abs(p[1] - point[1])
        if d < dist:
            best, dist = p, d
    for x1, y1, x2, y2 in wires:
        if y1 == y2:
            q = (min(max(round(point[0] / 10) * 10, min(x1, x2)), max(x1, x2)), y1)
        elif x1 == x2:
            q = (x1, min(max(round(point[1] / 10) * 10, min(y1, y2)), max(y1, y2)))
        else:
            continue
        d = abs(q[0] - point[0]) + abs(q[1] - point[1])
        if d < dist:
            best, dist = q, d
    return best


def _on_grid(p: tuple[float, float], w: tuple) -> tuple[float, float]:
    """A point along an orthogonal wire on the grid moved to the nearest grid point on it: a label carried along a
    moved wire stays on the grid (a wire off the grid keeps the exact point)."""
    if any(abs(v / schroute.GRID - round(v / schroute.GRID)) > 1e-9 for v in w[:4]):
        return p
    for k in (0, 1):
        if w[1 - k] == w[3 - k]:                          # runs along axis k
            lo, hi = sorted((w[k], w[k + 2]))
            v = round(p[k] / schroute.GRID) * schroute.GRID
            if lo <= v <= hi:
                return (v, p[1]) if k == 0 else (p[0], v)
    return p


def _merge_spans(spans: list[tuple]) -> list[tuple]:
    """Collinear pieces of one wire joined where they meet, so a wire cut in the middle stays as few wires as can be."""
    out: list[list] = []
    for a, b in sorted((min(s), max(s)) for s in spans):
        if out and out[-1][1] == a:
            out[-1][1] = b
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


class _Lines:
    """Wire indexes by the line each lies on, so the wires through a point are found without trying every wire."""

    def __init__(self, wires: list[tuple]):
        self.wires = wires
        h: dict[float, list[int]] = {}
        v: dict[float, list[int]] = {}
        self.other = []
        for i, w in enumerate(wires):
            if w[1] == w[3]:
                h.setdefault(w[1], []).append(i)
            if w[0] == w[2]:
                v.setdefault(w[0], []).append(i)
            if w[0] != w[2] and w[1] != w[3]:
                self.other.append(i)
        self.h, self.v = h, v
        self.hy, self.vx = sorted(h), sorted(v)

    def through(self, p) -> list[int]:
        found = [i for y in schroute._near(self.hy, self.hy, p[1]) for i in self.h[y]]
        found += [i for x in schroute._near(self.vx, self.vx, p[0]) for i in self.v[x]]
        return [i for i in dict.fromkeys(found + self.other) if schematic._on_segment(p[0], p[1], *self.wires[i][:4])]


ROUTE_TARGETS = 4        # points of a net a new pin tries to reach before it gets a label instead


def _targets(point: tuple[float, float], points: list[tuple[float, float]], wires: list[tuple],
             first: tuple[float, float]) -> list[tuple[float, float]]:
    """Where a pin may join its net, nearest first: `first` (_nearest), then the net's other pins and wire ends."""
    rest = sorted({p for p in points} | {p for w in wires for p in (tuple(w[:2]), tuple(w[2:4]))},
                  key=lambda p: (abs(p[0] - point[0]) + abs(p[1] - point[1]), p))
    return list(dict.fromkeys([first, *rest]))[:ROUTE_TARGETS]


class SchEditor:
    def __init__(self, path: Path, resolver: xschem.SymbolResolver, log: OpLog | None = None):
        self.path = path
        self.resolver = resolver
        self.log = log
        self.cell = path.stem
        self.view_kind = "symbol" if path.suffix == ".sym" else "schematic"
        self.lock = threading.RLock()
        self._meta: dict = {"op": "edit", "args": {}, "actor": "user"}
        self._net_cache: tuple[tuple, frozenset] | None = None

    @contextmanager
    def recording(self, op: str, args: dict, actor: str = "user", reason: str | None = None,
                  changeset: str | None = None):
        """Names the operation the following edit belongs to (written to the operation log)."""
        with self.lock:
            self._meta = {"op": op, "args": args, "actor": actor, "reason": reason, "changeset": changeset}
            try:
                yield
            finally:
                self._meta = {"op": "edit", "args": {}, "actor": "user"}

    # ------------------------------------------------------------------ io

    def _load(self) -> XFile:
        if self.path.is_file():
            return xschem.parse(self.path.read_text(errors="replace"))
        return XFile(version="xschem version=3.4.5 file_version=1.2\n")

    def _text(self) -> str:
        return self.path.read_text(errors="replace") if self.path.is_file() else ""

    def _commit(self, f: XFile, before: str, extra: list[dict] | None = None) -> None:
        if self.log is not None:
            self.log.guard()   # one writer per changeset (live.py)
        if self.path.suffix == ".sch":
            if self._meta.get("actor", "user") == "user":
                self._touched(before, f)
            xschem.canonical(f)
        text = xschem.dump(f)
        self.path.write_text(text)
        self._sync_netlist()
        if self.log is None:
            return
        # Record exactly the numeric precision and attributes written by Xschem's
        # serializer, so a later reload can safely compare geometry for Undo.
        effects = (extra or []) + oplog.diff(xschem.parse(before) if before else XFile(), xschem.parse(text))
        if effects:
            self.log.append({**self._meta, "cell": self.cell, "view": self.view_kind,
                             "sha": oplog.sha(text), "effects": effects})

    def _touched(self, before: str, f: XFile) -> None:
        """Instances a person moved, turned or mirrored are theirs: tools no longer place them (netlist.PLACED)."""
        if not before:
            return
        old = xschem.parse(before)
        was = {k: (c.x, c.y, c.rot, c.flip) for k, c in zip(xschem.comp_ids(old), old.comps)}
        for k, c in zip(xschem.comp_ids(f), f.comps):
            if netlist.PLACED in c.props and k in was and was[k] != (c.x, c.y, c.rot, c.flip):
                c.props.pop(netlist.PLACED)

    def _sync_netlist(self) -> None:
        """The drawing changed: read the netlist back from it (netlist.py). A schematic only; never fails an edit."""
        if self.path.suffix != ".sch":
            return
        try:
            if self.path.is_file():
                netlist.sync(self.path, self.resolver)
            else:
                netlist.path_of(self.path).unlink(missing_ok=True)
        except Exception as e:  # the drawing is saved; the netlist is read again the next time it is needed
            import sys
            print(f"undine: netlist of {self.path.name} not updated: {e}", file=sys.stderr)

    def _step(self, backwards: bool) -> bool:
        if self.log is None:
            return False
        self.log.guard()
        done, redo = self.log.stacks(self.cell, self.view_kind)
        rec = (done[-1] if backwards else redo[-1]) if (done if backwards else redo) else None
        if rec is None:
            return False
        if rec.get("symbol_group"):
            raise ValueError("This edit also changes other views; use the grouped Undo/Redo API")
        created = [e for e in rec["effects"] if e["k"] == "v" and e["do"] == "add"]
        removed = [e for e in rec["effects"] if e["k"] == "v" and e["do"] == "del"]
        if removed:                                    # delete_file: undo writes the file back, redo takes it again
            self._put(removed[0]["text"], present=not backwards)
            text = removed[0]["text"] if backwards else ""
        elif created:
            original = created[0]["text"]
            if backwards:
                if (not self.path.is_file() or
                        xschem.dump(self._load()) != xschem.dump(xschem.parse(original))):
                    raise ValueError("conflict: the created view changed outside this operation")
                self.path.unlink()
                text = ""
            else:
                if self.path.exists():
                    raise ValueError("conflict: this view was created in the meantime")
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_text(original)
                text = original
        else:
            f = self._load()
            oplog.apply(f, rec["effects"], backwards)
            text = xschem.dump(f)
            self.path.write_text(text)
        self._sync_netlist()
        self.log.append({"op": "undo" if backwards else "redo", "of": rec["seq"], "actor": self._meta.get("actor", "user"),
                         "cell": self.cell, "view": self.view_kind, "sha": oplog.sha(text), "effects": None})
        return True

    def replay(self, effects: list[dict], meta: dict) -> None:
        """Applies effects recorded elsewhere (a changeset, or the inverse of one) and logs them as `meta`."""
        with self.lock:
            if self.log is not None:
                self.log.guard()
            at = next((i for i, e in enumerate(effects) if e["k"] == "v" and (e["do"] == "del" or e.get("kept"))), None)
            if at is not None:                         # a deleted file back as it was, or out again (delete_file):
                if at:                                 # what comes before it, then it, then what follows
                    self.replay(effects[:at], meta)
                e = effects[at]
                self._put(e["text"], present=e["do"] == "del")
                if self.log is not None:
                    self.log.append({**meta, "cell": self.cell, "view": self.view_kind,
                                     "sha": oplog.sha("" if e["do"] == "del" else e["text"]), "effects": [dict(e)]})
                if effects[at + 1:]:
                    self.replay(effects[at + 1:], meta)
                return
            before = self._text()
            made = [e for e in effects if e["k"] == "v" and e["do"] == "add"]
            created = False
            f = self._load()
            if made:
                if self.path.is_file() and self.path.read_text(errors="replace") != made[0]["text"]:
                    raise ValueError(f"conflict: {self.path.name} was created in the meantime")
                if not self.path.is_file():
                    before = made[0]["text"]
                    f = xschem.parse(before)
                    created = True
            oplog.apply(f, effects, False)
            self._meta = meta
            try:
                if created:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                self._commit(f, before, made if created else None)
            finally:
                self._meta = {"op": "edit", "args": {}, "actor": "user"}

    def create_file(self, text: str) -> None:
        """Creates this view's file (a new schematic or symbol) and logs it, so a changeset can replay it."""
        with self.lock:
            if self.log is not None:
                self.log.guard()
            if self.path.exists():
                raise ValueError(f"{self.path.name} exists")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(text)
            self._sync_netlist()
            if self.log is not None:
                self.log.append({**self._meta, "cell": self.cell, "view": self.view_kind, "sha": oplog.sha(text),
                                 "effects": [{"k": "v", "do": "add", "text": text}]})

    def delete_file(self) -> None:
        """Takes this view's file (a schematic or a symbol) out and logs it with its text ({"k": "v", "do": "del",
        "kept": true}), so undo and a changeset's restore bring it back as it was (oplog.invert)."""
        with self.lock:
            if self.log is not None:
                self.log.guard()
            if not self.path.is_file():
                raise ValueError(f"{self.path.name} does not exist")
            text = self._text()
            self.path.unlink()
            self._net_cache = None
            self._sync_netlist()
            if self.log is not None:
                self.log.append({**self._meta, "cell": self.cell, "view": self.view_kind, "sha": oplog.sha(""),
                                 "effects": [{"k": "v", "do": "del", "kept": True, "text": text}]})

    def _put(self, text: str, present: bool) -> None:
        """A deleted view's file back (present: it is to be taken out, as it was deleted): refused when another file is
        there, or the one there is not the one recorded."""
        if present:
            if not self.path.is_file() or xschem.dump(self._load()) != xschem.dump(xschem.parse(text)):
                raise ValueError(f"conflict: {self.path.name} changed after it was brought back")
            self.path.unlink()
        else:
            if self.path.exists():
                raise ValueError(f"conflict: {self.path.name} was created again in the meantime")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(text)
        self._net_cache = None
        self._sync_netlist()

    def undo(self) -> bool:
        with self.lock:
            return self._step(True)

    def redo(self) -> bool:
        with self.lock:
            return self._step(False)

    # ----------------------------------------------------------------- view

    def view(self, kind: str = "schematic") -> dict:
        with self.lock:
            self.ensure_drawing()
            exists = self.path.is_file()
            d = (xschem.render(self.path, self.resolver) if exists
                 else xschem.render_model(self._load(), self.path, self.resolver))
            done, redo = self.log.stacks(self.cell, self.view_kind) if self.log else ([], [])
            d["undo"], d["redo"] = len(done), len(redo)
            if self.view_kind == "symbol":
                d["objects"] = symedit.objects(self._load())
                d["exists"] = exists
            if kind == "schematic" and self.path.suffix == ".sch":
                f = self._load()
                ids = xschem.comp_ids(f)
                d["terminals"] = [[ids[ci], name, *point]
                                  for (ci, _), name, point, _ in self._pins(f)]
                try:
                    # the drawing's connectivity, its nets under the netlist's names (netlist.py keeps them stable)
                    c, names = netlist.drawn_names(self.path, self.resolver)
                    d["wire_nets"] = [names.get(n, n) for n in c.wire_net]
                    d["ports"] = c.ports
                    d["pins"] = [[dev.name, pn, x, y, names.get(dev.nets.get(pn, ""), dev.nets.get(pn, ""))]
                                 for dev in c.devices for pn, x, y in dev.pins]
                    d["warnings"] = c.warnings
                    # connections the netlist has and the drawing does not show: drawn as a ratsnest
                    nl = netlist.current(self.path, self.resolver)
                    d["pending"] = netlist.pending_links(self.path, self.resolver, nl)
                    d["warnings"] = [*d["warnings"], *netlist.supply_warnings(nl, netlist.supplies(self.path, self.resolver, nl), self.path)]
                    d["placed_auto"] = [ids[ci] for ci, comp in enumerate(f.comps) if comp.props.get(netlist.PLACED) == "auto"]
                except Exception as e:  # keep the drawing usable even if connectivity fails
                    d["warnings"] = [f"connectivity: {e}"]
            return d

    def symbol_edit(self, op: str, values: dict):
        """One atomic symbol primitive edit, recorded with normal Undo and changeset effects."""
        if self.view_kind != "symbol":
            raise ValueError("symbol drawing operations require a symbol view")
        with self.lock:
            if not self.path.is_file():
                raise ValueError("create the symbol view before editing it")
            before = self._text()
            f = self._load()
            result = symedit.edit(f, op, values)
            self._commit(f, before)
            return result

    # ---------------------------------------------------------------- edits

    @staticmethod
    def _ids(f: XFile, kind: str, ref: str) -> list[int]:
        """Indexes of the objects a stable id refers to."""
        if kind == "i":
            idx = [i for i, x in enumerate(xschem.comp_ids(f)) if x == ref]
        elif kind == "w":
            idx = [i for i, w in enumerate(f.wires) if xschem.wire_id(*w[:4]) == ref]
        elif kind == "t":
            idx = [i for i, t in enumerate(f.texts) if f"{t.x:g},{t.y:g},{t.s}" == ref]
        else:
            raise ValueError(f"unknown object kind {kind}")
        if not idx:
            raise KeyError(f"no {({'i': 'instance', 'w': 'wire', 't': 'text'})[kind]} {ref}")
        return idx

    def _unique_name(self, f: XFile, base: str) -> str:
        m = re.match(r"^(.*?)(\d*)$", base)
        stem = (m.group(1) if m else base) or "x"
        used = {c.props.get("name", "") for c in f.comps}
        n = 1
        while f"{stem}{n}" in used:
            n += 1
        return f"{stem}{n}"

    def add_instance(self, sym: str, x: float | None, y: float | None, rot: int = 0, flip: int = 0,
                     props: dict | None = None, placed: str | None = None) -> str:
        """Places a symbol. Without x / y it goes to free space next to the drawing (free_spot) and nothing already
        drawn moves. placed "auto" marks it as placed by a tool (netlist.PLACED): tidy may move it until a person does."""
        with self.lock:
            before = self._text()
            f = self._load()
            s = self.resolver.load(sym, self.path.parent)
            if s is None:
                raise KeyError(f"symbol {sym} not found")
            p = dict(props or {})
            tmpl = schematic.sym_template(s)
            for k, v in tmpl.items():
                p.setdefault(k, v)
            if "name" in p and p["name"] in {c.props.get("name") for c in f.comps}:
                p["name"] = self._unique_name(f, p["name"])
            if x is None or y is None:
                x, y = self.free_spot(f, sym, rot & 3, flip & 1)
                placed = placed or "auto"
            if placed == "auto":
                p[netlist.PLACED] = "auto"
            f.comps.append(XComp(sym, x, y, rot & 3, flip & 1, p))
            xschem.normalize_names(f)
            self._commit(f, before)
            return xschem.comp_ids(f)[-1]

    # ----------------------------------------------------------------- placement by tools

    def _boxes(self, f: XFile, skip: set[int] | None = None) -> list[list[float]]:
        view = xschem.render_model(f, self.path, self.resolver)
        out = [i["bbox"] for n, i in enumerate(view["insts"]) if i.get("bbox") and not (skip and n in skip)]
        out += [[min(w[0], w[2]), min(w[1], w[3]), max(w[0], w[2]), max(w[1], w[3])] for w in f.wires]
        return out

    def free_spot(self, f: XFile, sym: str, rot: int = 0, flip: int = 0, taken: list[list[float]] | None = None,
                  skip: set[int] | None = None) -> tuple[float, float]:
        """Where a new symbol goes without touching anything drawn: in a column right of the drawing, the first
        place from the top where its outline (with a margin) overlaps no symbol and no wire."""
        path = self.resolver.find(sym, self.path.parent)
        if path is None:
            raise KeyError(f"symbol {sym} not found")
        sb = xschem.render(path, self.resolver).get("bbox") or [-20, -20, 20, 20]
        corners = [xschem._rot(rot, flip, x, y) for x in (sb[0], sb[2]) for y in (sb[1], sb[3])]
        sx1, sy1 = min(c[0] for c in corners), min(c[1] for c in corners)
        sx2, sy2 = max(c[0] for c in corners), max(c[1] for c in corners)
        boxes = self._boxes(f, skip) + list(taken or [])
        grid, gap = 10.0, 40.0
        snap = lambda v: math.ceil(v / grid) * grid                                    # noqa: E731
        if not boxes:
            return 0.0, 0.0
        right = max(b[2] for b in boxes)
        top = min(b[1] for b in boxes)
        x = snap(right + gap - sx1)
        y = snap(top - sy1)
        for _ in range(500):
            cand = [x + sx1 - gap / 2, y + sy1 - gap / 2, x + sx2 + gap / 2, y + sy2 + gap / 2]
            hit = [b for b in boxes if not (cand[2] <= b[0] or b[2] <= cand[0] or cand[3] <= b[1] or b[3] <= cand[1])]
            if not hit:
                return x, y
            y = snap(max(b[3] for b in hit) + gap / 2 - sy1)
        return x, y

    def tidy(self) -> list[str]:
        """Re-places the symbols tools placed (netlist.PLACED auto) that have nothing drawn to them, in a column right
        of everything else; a symbol a person moved, or one with wires, stays where it is. Returns the ids moved."""
        with self.lock:
            before = self._text()
            f = self._load()
            ids = xschem.comp_ids(f)
            drawn = set()
            for (ci, _), _, point, _ in self._pins(f):
                for w in f.wires:
                    if schematic._on_segment(point[0], point[1], *w[:4]):
                        drawn.add(ci)
            movable = {i for i, c in enumerate(f.comps) if c.props.get(netlist.PLACED) == "auto" and i not in drawn}
            if not movable:
                return []
            taken: list[list[float]] = []
            moved = []
            for i in sorted(movable, key=lambda i: ids[i]):
                c = f.comps[i]
                x, y = self.free_spot(f, c.sym, c.rot, c.flip, taken, skip=movable)
                if (x, y) != (c.x, c.y):
                    moved.append(ids[i])
                c.x, c.y = x, y
                view = xschem.render_model(f, self.path, self.resolver)
                b = view["insts"][i].get("bbox")
                if b:
                    taken.append(b)
            if moved:
                self._commit(f, before)
            return moved

    def ensure_drawing(self) -> bool:
        """A netlist without a schematic (written by a tool, or a synthesised one) gets a drawing, once: its ports as
        pins, its instances and code placed by free_spot, every connection pending (a ratsnest to draw). From then
        on it is an ordinary schematic. Returns whether one was made."""
        if self.path.suffix != ".sch" or self.path.exists():
            return False
        model = netlist.load(netlist.path_of(self.path))
        if model is None:
            return False
        with self.lock:
            f = XFile(version="xschem version=3.4.5 file_version=1.2\n")
            kinds = {"ipin": "devices/ipin.sym", "opin": "devices/opin.sym", "iopin": "devices/iopin.sym"}
            ins = [p for p in model["ports"] if p["type"] == "ipin"]
            outs = [p for p in model["ports"] if p["type"] != "ipin"]
            for col, ports, x in ((0, ins, -200.0), (1, outs, 200.0)):
                for k, port in enumerate(ports):
                    f.comps.append(XComp(kinds.get(port["type"], "devices/iopin.sym"), x, k * 40.0, 0, 0,
                                         {"name": f"p{len(f.comps) + 1}", "lab": port["name"], netlist.PLACED: "auto"}))
            for name, rec in model["instances"].items():
                x, y = self.free_spot(f, rec["symbol"]) if f.comps or f.wires else (0.0, 0.0)
                f.comps.append(XComp(rec["symbol"], x, y, 0, 0, {"name": name.split("#")[0], **rec.get("props", {}),
                                                                 netlist.PLACED: "auto"}))
            for c in model["code"]:
                x, y = self.free_spot(f, c["symbol"])
                f.comps.append(XComp(c["symbol"], x, y, 0, 0, {"name": c.get("name", ""), **c.get("props", {}),
                                                              netlist.PLACED: "auto"}))
            # every connection is pending until drawn
            model["pending"] = {f"{inst}.{pn}": net for inst, rec in model["instances"].items()
                                for pn, net in (rec.get("pins") or {}).items()}
            netlist._write(netlist.path_of(self.path), model)
            self.create_file(xschem.dump(xschem.canonical(f)))
            return True

    def add_wire(self, x1: float, y1: float, x2: float, y2: float, lab: str = "") -> str:
        with self.lock:
            before = self._text()
            f = self._load()
            f.wires.append((x1, y1, x2, y2, {"lab": lab} if lab else {}))
            self._commit(f, before)
            return xschem.wire_id(x1, y1, x2, y2)

    def wire_route(self, x1: float, y1: float, x2: float, y2: float) -> list[tuple[float, float]]:
        """Preview the same route that route_wire will commit, without writing or logging (within the preview budget)."""
        with self.lock, schroute.time_budget(PREVIEW_BUDGET):
            f = self._load()
            return self._routed_points(f, (x1, y1), (x2, y2))

    def route_wire(self, x1: float, y1: float, x2: float, y2: float, lab: str = "") -> list[str]:
        """Add a routed connection in one undoable operation."""
        with self.lock, schroute.time_budget(EDIT_BUDGET):
            before = self._text()
            f = self._load()
            points = self._routed_points(f, (x1, y1), (x2, y2))
            out = []
            for a, b in zip(points, points[1:]):
                for p, q in self._uncovered(a, b, f.wires):
                    f.wires.append((*p, *q, {"lab": lab} if lab else {}))
                    out.append(xschem.wire_id(*p, *q))
            # A label requested on an entirely reused connection still has to
            # be recorded; normal pointer wiring supplies no label.
            if lab and not out:
                for i, w in enumerate(f.wires):
                    if w[4].get("lab") != lab and any(
                            self._uncovered(a, b, [w]) != [(a, b)] and a != b
                            for a, b in zip(points, points[1:])):
                        f.wires[i] = (*w[:4], {**w[4], "lab": lab})
                        out.append(xschem.wire_id(*w[:4]))
                        break
            if out:
                self._commit(f, before)
            return out

    def _routed_points(self, f: XFile, start: tuple, end: tuple, keep_clear: bool = False) -> list[tuple[float, float]]:
        """Only the explicitly selected endpoint nets may supply reusable wire."""
        endpoint_tokens = set()
        for wi, w in enumerate(f.wires):
            if any(schematic._on_segment(*point, *w[:4]) for point in (start, end)):
                endpoint_tokens.add(('w', wi))
        for key, _, point, _ in self._pins(f):
            if point in (start, end):
                endpoint_tokens.add(('p', *key))
        allowed = {token[1] for net in self._connections(f) if net & endpoint_tokens
                   for token in net if token[0] == 'w'}
        return schroute.route(f, self.resolver, self.path.parent, start, end,
                              allowed_wire_indices=allowed, avoid_backtrack=True, keep_clear=keep_clear)

    def connect_nets(self, nets: list[tuple[str, list[str]]], style: str = "auto",
                     label_symbol: str = "devices/lab_pin.sym", move: bool = False) -> list[dict]:
        """Puts instance pins ("M1:D") on their nets in one undoable change, the way a person draws it: routed wires
        from each pin to the nearest pin or wire already on the net (the first wire carries the net's name), and a
        net label on a pin that has no safe route. style "label" labels every pin; "auto" labels power / ground
        nets and nets with many pins, and wires the rest. With `move`, a pin drawn to another net is taken off it
        first (_disconnect). Nothing is written unless every net's pins end up on one net and no other nets were
        joined."""
        if style not in ("auto", "wire", "label"):
            raise ValueError("style is auto, wire or label")
        with self.lock:
            before = self._text()
            f = self._load()
            results = []
            for net, pins in nets:
                with schroute.time_budget(EDIT_BUDGET):
                    away = self._elsewhere(f, net, pins) if move else []
                    moved = self._disconnect(f, away, label_symbol) if away else {}
                    results.append(self._connect_one(f, net, pins, style, label_symbol))
                    if moved:
                        results[-1]["moved_from"] = moved
            if xschem.dump(f) != before:
                self._commit(f, before)
            return results

    def _elsewhere(self, f: XFile, net: str, pins: list) -> list[str]:
        """Of `pins`, those drawn to a net other than `net` (connected to another terminal there)."""
        of, named = self._nets_of(f)
        members: dict[int, int] = {}
        for n in of.values():
            members[n] = members.get(n, 0) + 1
        out = []
        for ref in pins if isinstance(pins, list) else []:
            inst, _, pin = str(ref).partition(":")
            k = next((k for k in of if k[0] == inst and k[1].lower() == pin.lower()), None)
            if k is not None and members[of[k]] > 1 and named.get(of[k]) != net:
                out.append(str(ref))
        return out

    def _connect_one(self, f: XFile, net: str, pins: list[str], style: str, label_symbol: str) -> dict:
        net = (net or "").strip()
        if not net or not isinstance(pins, list) or not pins or not all(isinstance(p, str) and ":" in p for p in pins):
            raise ValueError('a net needs a name and pins, e.g. "out": ["M2:D", "M4:D"] (instance:pin)')
        terminals = self._pins(f)
        names = {ci: c.props.get("name", "") for ci, c in enumerate(f.comps)}
        wanted: list[tuple[tuple[int, int], tuple[float, float], str]] = []
        for ref in pins:
            inst, _, pin = str(ref).partition(":")
            found = [(key, point) for key, pname, point, _ in terminals
                     if names.get(key[0]) == inst and pname.lower() == pin.lower()]
            if not found:
                if inst not in names.values():
                    raise KeyError(f"no instance {inst} (there are: {', '.join(sorted(v for v in names.values() if v)) or 'none'})")
                have = [pname for key, pname, _, _ in terminals if names.get(key[0]) == inst]
                raise KeyError(f"{inst} has no pin {pin} (its pins: {', '.join(have)})")
            wanted.append((found[0][0], found[0][1], ref))
        before_groups = self._connections(f)
        wires_before = len(f.wires)

        def name_of(group) -> str:
            labels = {f.wires[t[1]][4].get("lab", "") for t in group if t[0] == "w" and t[1] < len(f.wires)}
            labels |= {label for key, _, _, label in terminals if ("p", *key) in group and label}
            labels.discard("")
            return sorted(labels)[0] if labels else ""

        group_of = {t: g for g in before_groups for t in g}
        todo, on_net = [], []
        for key, point, ref in wanted:
            now = name_of(group_of.get(("p", *key), frozenset()))
            if now and now != net:
                raise ValueError(f"{ref} is already on net {now}; delete that connection first")
            (on_net if now == net else todo).append((key, point, ref))
        wired = style == "wire" or (style == "auto" and not POWER_NET.match(net) and len(pins) <= MAX_WIRED_PINS)
        labelled: list[tuple[str, tuple[float, float], int]] = []      # (pin, where, instance it sits on)
        if not wired:
            labelled = [(ref, point, key[0]) for key, point, ref in todo]
        elif todo:
            # grow one tree, nearest pin first: from the pins already on the net, or else from the first pin
            start = None if on_net else todo.pop(0)
            tree_points = [point for _, point, _ in on_net] or [start[1]]
            tree_wires: list[tuple] = []
            named = bool(on_net)              # the tree carries the net's name (a labelled pin, or its first wire)
            while todo:
                targets = [_nearest(point, tree_points, tree_wires) for _, point, _ in todo]
                i = min(range(len(todo)), key=lambda i: abs(todo[i][1][0] - targets[i][0]) + abs(todo[i][1][1] - targets[i][1]))
                key, point, ref = todo.pop(i)
                route = None
                for target in _targets(point, tree_points, tree_wires, targets[i]):
                    try:
                        route = self._routed_points(f, point, target, keep_clear=True)
                        break
                    except schroute.RouteTimeout:
                        break
                    except ValueError:        # this point of the net cannot be reached readably: the next one
                        continue
                if route is None:             # no safe route (or none in time): this pin gets a label
                    labelled.append((ref, point, key[0]))
                    continue
                for p, q in ((p, q) for a, b in zip(route, route[1:]) for p, q in self._uncovered(a, b, f.wires)):
                    f.wires.append((*p, *q, {} if named else {"lab": net}))
                    tree_wires.append((*p, *q))
                    named = True
                tree_points.append(point)
            if not named:                     # nothing was routed: the starting pin needs the name as well
                labelled.append(("", start[1], start[0][0]))
        sym = self.resolver.load(label_symbol, self.path.parent) if labelled else None
        if labelled and sym is None:
            raise KeyError(f"the label symbol {label_symbol} is not installed")
        for _, (x, y), ci in labelled:
            props = {**schematic.sym_template(sym), "lab": net}
            props["name"] = self._unique_name(f, props.get("name") or "l1")
            # the label's text points away from the instance: mirrored (text to the right) for a pin right of its origin
            f.comps.append(XComp(label_symbol, x, y, 0, 1 if x > f.comps[ci].x else 0, props))
        xschem.normalize_names(f)
        self._check_joined(f, net, [key for key, _, _ in wanted], before_groups)
        return {"net": net, "wires_added": len(f.wires) - wires_before, "labelled": [ref for ref, _, _ in labelled if ref],
                "already_on_net": [ref for _, _, ref in on_net]}

    def _check_joined(self, f: XFile, net: str, keys: list[tuple[int, int]], before_groups) -> None:
        """The requested pins share one net now, and no two nets that were apart before were joined otherwise."""
        after = self._connections(f)
        group_of = {t: g for g in after for t in g}
        groups = {group_of.get(("p", *k)) for k in keys}
        if len(groups) != 1 or None in groups:
            raise ValueError(f"could not put all pins on net {net}")
        joined = next(iter(groups))
        for g in after:
            parts = [b for b in before_groups if b & g]
            if len(parts) < 2:
                continue
            if g is not joined or any(not (b & {("p", *k) for k in keys}) and not self._named(f, b, net) for b in parts):
                raise ValueError(f"connecting {net} would join other nets; nothing was changed")

    def _named(self, f: XFile, group, net: str) -> bool:
        labels = {f.wires[t[1]][4].get("lab", "") for t in group if t[0] == "w" and t[1] < len(f.wires)}
        labels |= {label for key, _, _, label in self._pins(f) if ("p", *key) in group}
        return net in labels

    # ---------------------------------------------------------------- re-wiring drawn nets

    def _types(self, f: XFile) -> list[str]:
        out = []
        for c in f.comps:
            sym = schematic.resolve_symbol(self.resolver, c, self.path.parent)
            out.append(sym.global_props.get("type", "") if sym is not None else "")
        return out

    def _nets_of(self, f: XFile) -> tuple[dict[tuple[str, str], int], dict[int, str]]:
        """Each terminal (instance name, pin) -> its drawn net (a number), and each net's name where the drawing
        gives one (a label, a port, a wire's lab)."""
        groups = sorted(self._connections(f), key=lambda g: sorted(g))
        pins = self._pins(f)
        names = xschem.comp_ids(f)
        where = {t: n for n, g in enumerate(groups) for t in g}
        out = {(names[key[0]], pname): where[("p", *key)] for key, pname, _, _ in pins}
        named: dict[int, str] = {}
        for n, g in enumerate(groups):
            labels = {f.wires[t[1]][4].get("lab", "") for t in g if t[0] == "w"}
            labels |= {label for key, _, _, label in pins if ("p", *key) in g and label}
            labels.discard("")
            if labels:
                named[n] = sorted(labels)[0]
        return out, named

    def _same_partition(self, before: dict, after: dict, cut: set) -> None:
        """Terminals that shared a net still do and no others now do, but those in `cut` (taken off their nets)."""
        keys = [k for k in before if k in after and k not in cut]
        old: dict[int, set] = {}
        new: dict[int, set] = {}
        for k in keys:
            old.setdefault(before[k], set()).add(k)
            new.setdefault(after[k], set()).add(k)
        if sorted(map(sorted, old.values())) != sorted(map(sorted, new.values())):
            raise ValueError("the change would join or split other nets; nothing was changed")
        for k in cut:
            if k in after and any(after[o] == after[k] for o in keys):
                raise ValueError(f"{k[0]}:{k[1]} is still connected to its old net; nothing was changed")

    def _segments(self, f: XFile) -> tuple[list[tuple], dict[tuple, list[int]]]:
        """The wires cut at every point that joins something (a wire end, a terminal): (a, b, wire index) pieces, and
        the pieces at each point."""
        points = {tuple(w[:2]) for w in f.wires} | {tuple(w[2:4]) for w in f.wires}
        points |= {point for _, _, point, _ in self._pins(f)}
        by_x: dict[float, list] = {}
        by_y: dict[float, list] = {}
        for p in points:
            by_x.setdefault(p[0], []).append(p)
            by_y.setdefault(p[1], []).append(p)
        pieces, at = [], {}
        for wi, w in enumerate(f.wires):
            line = by_y.get(w[1], []) if w[1] == w[3] else by_x.get(w[0], []) if w[0] == w[2] else points
            on = sorted({p for p in line if schematic._on_segment(*p, *w[:4])},
                        key=lambda p: (p[0] - w[0]) * (w[2] - w[0]) + (p[1] - w[1]) * (w[3] - w[1]))
            for a, b in zip(on, on[1:]):
                at.setdefault(a, []).append(len(pieces))
                at.setdefault(b, []).append(len(pieces))
                pieces.append((a, b, wi))
        return pieces, at

    def _disconnect(self, f: XFile, refs: list[str], label_symbol: str = "devices/lab_pin.sym") -> dict[str, str]:
        """Takes device pins ("M1:D") off the nets they are drawn to, in f: the wires from each pin up to the first
        junction or other pin go, as do net labels sitting on it; where that cuts the rest of its net apart, the
        pieces are joined again by routed wires. Returns {pin: the name of the net it was on, "" if unnamed}."""
        types = self._types(f)
        names = xschem.comp_ids(f)
        terms = self._pins(f)
        before, named = self._nets_of(f)
        targets = []
        for ref in dict.fromkeys(refs):
            inst, _, pin = str(ref).partition(":")
            hit = [(key, pname, point) for key, pname, point, _ in terms if names[key[0]] == inst and pname.lower() == pin.lower()]
            if not hit:
                raise KeyError(f"no pin {ref} (instance:pin)")
            if types[hit[0][0][0]] in schematic.NAMING_TYPES:
                raise ValueError(f"{ref} is a label or port: delete or move it instead")
            targets.append((ref, *hit[0]))
        drop: set[int] = set()
        for ref, key, _, point in targets:
            for other, pname, p, _ in terms:
                if p != point or other == key:
                    continue
                if types[other[0]] == "label":
                    drop.add(other[0])
                elif types[other[0]] in schematic.PORT_TYPES:
                    raise ValueError(f"{ref}: the port {names[other[0]]} sits on it; move or delete the port first")
                else:
                    raise ValueError(f"{ref} touches {names[other[0]]}:{pname} directly; move one of them first")
        pieces, at = self._segments(f)
        stops = {point for _, _, point, _ in terms}
        gone: set[int] = set()
        for _, _, _, point in targets:                 # from the pin along its wires to the first junction or terminal
            for piece in at.get(point, []):
                node = point
                while piece not in gone:
                    gone.add(piece)
                    a, b, _ = pieces[piece]
                    node = b if a == node else a
                    nxt = [i for i in at.get(node, []) if i not in gone]
                    if node in stops or len(nxt) != 1:
                        break
                    piece = nxt[0]
        cut_wires = {pieces[i][2] for i in gone}
        wires = []
        for wi, w in enumerate(f.wires):
            if wi not in cut_wires:
                wires.append(w)
                continue
            for a, b in _merge_spans([(p[0], p[1]) for i, p in enumerate(pieces) if p[2] == wi and i not in gone]):
                wires.append((*a, *b, dict(w[4])))
        f.wires = wires
        f.comps = [c for i, c in enumerate(f.comps) if i not in drop]
        cut = {(names[key[0]], pname) for _, key, pname, _ in targets}
        was = {ref: named.get(before[(names[key[0]], pname)], "") for ref, key, pname, _ in targets}
        after, _ = self._nets_of(f)
        for n in sorted({before[k] for k in cut}):     # the rest of each net in one piece again
            first: dict[int, tuple[str, str]] = {}
            for k, v in before.items():
                if v == n and k not in cut and k in after:
                    first.setdefault(after[k], k)
            if len(first) < 2:
                continue
            net = named.get(n) or f"undine_rejoin{n}"
            wires_before = len(f.wires)
            done = self._connect_one(f, net, [f"{i}:{p}" for i, p in first.values()], "wire", label_symbol)
            if n not in named:                        # an unnamed net stays unnamed
                if done["labelled"]:
                    raise ValueError("the rest of its net could not be joined again by wires; nothing was changed")
                f.wires = [w if i < wires_before else (*w[:4], {k: v for k, v in w[4].items() if (k, v) != ("lab", net)})
                           for i, w in enumerate(f.wires)]
        for n in sorted({before[k] for k in cut} & set(named)):
            self._keep_name(f, named[n], [k for k, v in before.items() if v == n and k not in cut], label_symbol)
        self._same_partition(before, self._nets_of(f)[0], cut)
        return was

    def _keep_name(self, f: XFile, net: str, members: list[tuple[str, str]], label_symbol: str) -> None:
        """The rest of a net still carries its name after wires of it went (the one with its lab among them): on one of
        its wires, or else on a label at one of its pins."""
        now, named = self._nets_of(f)
        group = next((now[k] for k in members if k in now), None)
        if group is None or named.get(group) == net:
            return
        groups = sorted(self._connections(f), key=lambda g: sorted(g))
        wires = sorted(t[1] for t in groups[group] if t[0] == "w")
        if wires:
            w = f.wires[wires[0]]
            f.wires[wires[0]] = (*w[:4], {**w[4], "lab": net})
            return
        sym = self.resolver.load(label_symbol, self.path.parent)
        if sym is None:
            raise KeyError(f"the label symbol {label_symbol} is not installed")
        names = xschem.comp_ids(f)
        key, point = next((key, point) for key, pname, point, _ in self._pins(f) if (names[key[0]], pname) in members)
        props = {**schematic.sym_template(sym), "lab": net}
        props["name"] = self._unique_name(f, props.get("name") or "l1")
        f.comps.append(XComp(label_symbol, *point, 0, 1 if point[0] > f.comps[key[0]].x else 0, props))
        xschem.normalize_names(f)

    def disconnect(self, refs: list[str], label_symbol: str = "devices/lab_pin.sym") -> dict[str, str]:
        """_disconnect as one undoable change."""
        with self.lock, schroute.time_budget(EDIT_BUDGET):
            before = self._text()
            f = self._load()
            out = self._disconnect(f, refs, label_symbol)
            if xschem.dump(f) != before:
                self._commit(f, before)
            return out

    def set_port_order(self, order: list[str]) -> list[str]:
        """Reorders the port instances (ipin / opin / iopin) so the ports follow `order`, which names each port (its
        lab) once: the subcircuit's pin order. Nothing else moves. Returns the new order."""
        with self.lock:
            before = self._text()
            f = self._load()
            types = self._types(f)
            slots = [i for i, t in enumerate(types) if t in schematic.PORT_TYPES]
            have = [schematic.inst_props(f.comps[i], schematic.resolve_symbol(self.resolver, f.comps[i], self.path.parent))
                    .get("lab", "") for i in slots]
            if len(set(have)) != len(have):
                raise ValueError(f"two port instances share a name ({', '.join(sorted({n for n in have if have.count(n) > 1}))})")
            if not isinstance(order, list) or sorted(map(str, order)) != sorted(have):
                raise ValueError(f"order must name every port once: {', '.join(have) or 'the schematic has no ports'}")
            by = dict(zip(have, (f.comps[i] for i in slots)))
            ids = xschem.comp_ids(f)
            for i, name in zip(slots, order):
                f.comps[i] = by[str(name)]
            if xschem.dump(f) != before:
                # oplog.diff knows a port by its symbol's name (devices/ipin.sym ...); one of another name is named here
                new_ids = xschem.comp_ids(f)
                own = {"k": "o", "id": "ports", "id_after": "ports", "do": "mod",
                       "changes": {"order": [[ids[i] for i in slots], [new_ids[i] for i in slots]], "ports": [have, [str(n) for n in order]]}}
                found = any(e["k"] == "o" for e in oplog.diff(xschem.parse(before), f))
                self._commit(f, before, None if found else [own])
            return [str(n) for n in order]

    def drawing(self, net: str | None = None, instances: list[str] | None = None,
                region: list[float] | None = None) -> dict:
        """How the schematic is drawn, for a tool that rearranges it: each instance's place and extent (its graphics
        and texts), each net's wires (ids), labels and pins under the netlist's names, and instances whose extents
        overlap (texts included: a long value over a neighbour). `net` / `instances` / `region` (x1, y1, x2, y2: what
        meets it) narrow it down."""
        with self.lock:
            f = self._load()
            ids = xschem.comp_ids(f)
            conn, rename = netlist.drawn_names(self.path, self.resolver)
            name = lambda n: rename.get(n, n)                       # noqa: E731
            types = self._types(f)
            nets: dict[str, dict] = {}
            spans = {xschem.wire_id(*w[:4]): (min(w[0], w[2]), min(w[1], w[3]), max(w[0], w[2]), max(w[1], w[3]))
                     for w in f.wires}
            for wi, w in enumerate(f.wires):
                if wi < len(conn.wire_net):
                    nets.setdefault(name(conn.wire_net[wi]), {"wires": [], "labels": [], "pins": []})["wires"].append(
                        xschem.wire_id(*w[:4]))
            for d in conn.devices:
                for pn, _, _ in d.pins:
                    nets.setdefault(name(d.nets.get(pn, "")), {"wires": [], "labels": [], "pins": []})["pins"].append(f"{d.name}:{pn}")
            pins_at = {}
            for d in conn.devices:
                for pn, x, y in d.pins:
                    pins_at[(x, y)] = name(d.nets.get(pn, ""))
            boxes, out = {}, {}
            for ci, c in enumerate(f.comps):
                sym = schematic.resolve_symbol(self.resolver, c, self.path.parent)
                prims = xschem.Prims()
                bb = xschem._emit_symbol(prims, sym, c, Path(c.sym).stem) if sym is not None else \
                    [c.x - 20, c.y - 20, c.x + 20, c.y + 20]
                for t in prims.texts:
                    tb = xschem.text_box(t)
                    bb = [min(bb[0], tb[0]), min(bb[1], tb[1]), max(bb[2], tb[2]), max(bb[3], tb[3])]
                boxes[ids[ci]] = bb
                rec = {"symbol": c.sym, "at": [c.x, c.y], "rot": c.rot, "flip": c.flip,
                       "extent": [round(v, 1) for v in bb]}
                if c.props.get(netlist.PLACED) == "auto":
                    rec["placed_by_tool"] = True
                if types[ci] in schematic.NAMING_TYPES:
                    lab = schematic.inst_props(c, sym).get("lab", "")
                    rec["label" if types[ci] == "label" else "port"] = lab
                    for key, _, point, _ in self._pins(XFile(comps=[c])):
                        n = pins_at.get(point) or lab
                        nets.setdefault(n, {"wires": [], "labels": [], "pins": []})["labels"].append(ids[ci])
                out[ids[ci]] = rec
            touching = {}
            for (ci, _), _, point, _ in self._pins(f):
                touching.setdefault(point, set()).add(ids[ci])
            joined = {frozenset((a, b)) for group in touching.values() for a in group for b in group if a != b}
            overlaps = []
            names = sorted(boxes)
            order = sorted(names, key=lambda n: boxes[n][0])
            for i, a in enumerate(order):                # sweep along x: only neighbours in x are compared
                for b in order[i + 1:]:
                    if boxes[b][0] >= boxes[a][2]:
                        break
                    ba, bb_ = boxes[a], boxes[b]
                    if ba[1] < bb_[3] and bb_[1] < ba[3] and frozenset((a, b)) not in joined:
                        overlaps.append(sorted((a, b)))
            if net is not None:
                if net not in nets:
                    raise KeyError(f"no net {net} in the drawing (there are: {', '.join(sorted(nets)[:40])})")
                keep = {p.split(":")[0] for p in nets[net]["pins"]} | set(nets[net]["labels"])
                nets = {net: nets[net]}
                out = {k: v for k, v in out.items() if k in keep}
            if instances:
                missing = [i for i in instances if i not in out and i not in ids]
                if missing:
                    raise KeyError(f"no instance {', '.join(missing)}")
                out = {k: v for k, v in out.items() if k in set(instances)}
                nets = {n: r for n, r in nets.items()          # the nets of their pins and labels
                        if set(r["labels"]) & set(out) or {p.rsplit(":", 1)[0] for p in r["pins"]} & set(out)}
            if region is not None:
                x1, x2 = sorted(region[0::2])
                y1, y2 = sorted(region[1::2])
                meets = lambda b: b[0] <= x2 and x1 <= b[2] and b[1] <= y2 and y1 <= b[3]      # noqa: E731
                out = {k: v for k, v in out.items() if meets(boxes[k])}
                nets = {n: {"wires": [w for w in r["wires"] if meets(spans[w])],
                            "labels": [i for i in r["labels"] if i in out],
                            "pins": [p for p in r["pins"] if p.rsplit(":", 1)[0] in out]} for n, r in nets.items()}
                nets = {n: r for n, r in nets.items() if r["wires"] or r["labels"] or r["pins"]}
            shown = set(out)
            return {"instances": out, "nets": nets,
                    "overlaps": [p for p in sorted(overlaps) if set(p) & shown]}

    def label_net(self, net: str, wire_ids: list[str] | None = None,
                  label_symbol: str = "devices/lab_pin.sym") -> dict:
        """Replaces wires of a net (`wire_ids`, or all of them) by net labels, in one undoable change: the wires go,
        with what is left dangling of the net's wiring, and every piece of the net that no longer carries its name
        gets a label on one of its pins. The circuit stays the same. `net` is the netlist's name of the net."""
        with self.lock:
            before = self._text()
            f = self._load()
            conn, rename = netlist.drawn_names(self.path, self.resolver)
            wire_net = [rename.get(n, n) for n in conn.wire_net]
            mine = [wi for wi, n in enumerate(wire_net) if n == net]
            pins = [(d.name, pn) for d in conn.devices for pn, _, _ in d.pins if rename.get(d.nets.get(pn, ""), d.nets.get(pn)) == net]
            if not mine and not pins:
                raise KeyError(f"no net {net} in the drawing")
            ids = [xschem.wire_id(*f.wires[wi][:4]) for wi in mine]
            if wire_ids:
                bad = [w for w in wire_ids if w not in ids]
                if bad:
                    raise KeyError(f"not wires of {net}: {', '.join(bad)} (its wires: {', '.join(ids) or 'none'})")
                drop = {wi for wi in mine if xschem.wire_id(*f.wires[wi][:4]) in set(wire_ids)}
            else:
                drop = set(mine)
            of, _ = self._nets_of(f)
            removed = [xschem.wire_id(*f.wires[wi][:4]) for wi in sorted(drop)]
            mine_left = {xschem.wire_id(*f.wires[wi][:4]) for wi in mine if wi not in drop}
            f.wires = [w for wi, w in enumerate(f.wires) if wi not in drop]
            stops = {point for _, _, point, _ in self._pins(f)}
            while True:                               # the net's wiring left dangling: an end on nothing
                ends = {}
                for wi, w in enumerate(f.wires):
                    for p in (tuple(w[:2]), tuple(w[2:4])):
                        ends[p] = ends.get(p, 0) + 1
                lines = _Lines(f.wires)
                loose = [wi for wi, w in enumerate(f.wires) if xschem.wire_id(*w[:4]) in mine_left and any(
                    p not in stops and ends[p] == 1 and lines.through(p) == [wi] for p in (tuple(w[:2]), tuple(w[2:4])))]
                if not loose:
                    break
                removed += [xschem.wire_id(*f.wires[wi][:4]) for wi in loose]
                f.wires = [w for wi, w in enumerate(f.wires) if wi not in set(loose)]
            after, named = self._nets_of(f)
            names = xschem.comp_ids(f)
            terms = {(names[key[0]], pname): (key, point) for key, pname, point, _ in self._pins(f)}
            pieces: dict[int, list[tuple[str, str]]] = {}
            for k in pins:
                if k in after:
                    pieces.setdefault(after[k], []).append(k)
            sym = self.resolver.load(label_symbol, self.path.parent)
            labelled = []
            for n, members in sorted(pieces.items()):
                if named.get(n) == net:
                    continue
                if sym is None:
                    raise KeyError(f"the label symbol {label_symbol} is not installed")
                key, (x, y) = terms[members[0]]
                props = {**schematic.sym_template(sym), "lab": net}
                props["name"] = self._unique_name(f, props.get("name") or "l1")
                f.comps.append(XComp(label_symbol, x, y, 0, 1 if x > f.comps[key[0]].x else 0, props))
                labelled.append(f"{members[0][0]}:{members[0][1]}")
            xschem.normalize_names(f)
            self._same_partition(of, self._nets_of(f)[0], set())
            if xschem.dump(f) != before:
                self._commit(f, before)
            return {"net": net, "wires_removed": removed, "labelled": labelled}

    @staticmethod
    def _uncovered(a: tuple, b: tuple, wires: list) -> list[tuple[tuple, tuple]]:
        """Subtract existing collinear spans so a reused pin lead is stored once."""
        if a == b:
            return []
        axis = 0 if a[1] == b[1] else 1
        other = 1 - axis
        low, high = sorted((a[axis], b[axis]))
        spans = sorted((max(low, min(w[axis], w[axis + 2])), min(high, max(w[axis], w[axis + 2])))
                       for w in wires if w[other] == w[other + 2] == a[other])
        intervals = []
        cursor = low
        for lo, hi in spans:
            if hi <= lo:
                continue
            if lo > cursor:
                intervals.append((cursor, lo))
            cursor = max(cursor, hi)
        if cursor < high:
            intervals.append((cursor, high))

        def point(value):
            return (value, a[1]) if axis == 0 else (a[0], value)

        if a[axis] > b[axis]:
            intervals = [(hi, lo) for lo, hi in reversed(intervals)]
        return [(point(lo), point(hi)) for lo, hi in intervals]

    def _pins(self, f: XFile, resolver: xschem.SymbolResolver | None = None
              ) -> list[tuple[tuple[int, int], str, tuple[float, float], str]]:
        """All electrical terminals, including naming symbols omitted from device netlists."""
        resolver = resolver or self.resolver
        out = []
        for ci, c in enumerate(f.comps):
            sym = schematic.resolve_symbol(resolver, c, self.path.parent)
            if sym is None:
                continue
            typ = sym.global_props.get("type", "")
            label = schematic.inst_props(c, sym).get("lab", "") if typ in schematic.NAMING_TYPES else ""
            for pi, p in enumerate(schematic.sym_pins(sym)):
                x, y = xschem._rot(c.rot, c.flip, p.x, p.y)
                out.append(((ci, pi), p.name, (x + c.x, y + c.y), label if pi == 0 else ""))
        return out

    def _connections(self, f: XFile, origins: list[set[int]] | None = None,
                     resolver: xschem.SymbolResolver | None = None) -> frozenset:
        """Compare logical nets without depending on auto-generated net names or wire order.

        Each original wire remains a token even when routing splits it. This also
        protects floating wires and connections with no device pins or net labels.
        """
        pins = self._pins(f, resolver)
        signature = None
        if origins is None:
            # Mouse previews share a design across many cursor positions. Cache
            # just its electrical graph, including resolved pin coordinates and
            # labels so external symbol edits cannot leave stale connectivity.
            signature = (tuple((*w[:4], w[4].get("lab", "")) for w in f.wires),
                         tuple((key, point, label) for key, _, point, label in pins))
            if self._net_cache is not None and self._net_cache[0] == signature:
                return self._net_cache[1]
        nw = len(f.wires)
        uf = schematic._UF(nw + len(pins))
        lines = _Lines(f.wires)
        for i, w in enumerate(f.wires):           # an end of one wire on another (either way round) joins them
            for p in (w[:2], w[2:4]):
                for j in lines.through(p):
                    uf.union(i, j)
        by_point = {}
        names = {}
        for i, w in enumerate(f.wires):
            label = w[4].get("lab", "")
            if label:
                if label in names:
                    uf.union(i, names[label])
                names[label] = i
        for i, (_, _, point, label) in enumerate(pins):
            node = nw + i
            for wi in lines.through(point):
                uf.union(node, wi)
            point_key = (round(point[0], 4), round(point[1], 4))
            if point_key in by_point:
                uf.union(node, by_point[point_key])
            by_point[point_key] = node
            if label:
                if label in names:
                    uf.union(node, names[label])
                names[label] = node
        groups: dict[int, set] = {}
        token_roots = {}
        for i in range(nw + len(pins)):
            root = uf.find(i)
            tokens = ([('w', wi) for wi in (origins[i] if origins is not None else {i})]
                      if i < nw else [('p', *pins[i - nw][0])])
            for token in tokens:
                if token in token_roots and token_roots[token] != root:
                    raise ValueError("Moving these objects would disconnect a wire; choose another position")
                token_roots[token] = root
                groups.setdefault(root, set()).add(token)
        result = frozenset(frozenset(g) for g in groups.values())
        if signature is not None:
            self._net_cache = signature, result
        return result

    def plan_symbol_change(self, before: XFile, old_resolver: xschem.SymbolResolver,
                           new_resolver: xschem.SymbolResolver) -> XFile:
        """Plan a consumer's wires against proposed symbols without writing any file.

        The caller supplies two resolver snapshots (the new one may overlay
        in-memory symbol models), then commits this result with the symbol in
        one transaction. Terminal identity/order and direction must be unchanged:
        this handles pin geometry, not an electrical interface migration.
        """
        if self.view_kind != "schematic":
            raise ValueError("symbol consumers must be schematic views")
        changed = False
        for comp in before.comps:
            old_sym = schematic.resolve_symbol(old_resolver, comp, self.path.parent)
            new_sym = schematic.resolve_symbol(new_resolver, comp, self.path.parent)
            name = comp.props.get("name", comp.sym)
            if old_sym is None and new_sym is None:
                continue
            if old_sym is None or new_sym is None:
                raise ValueError(f"Cannot preserve {name}: its symbol resolution changed")
            old_pins, new_pins = schematic.sym_pins(old_sym), schematic.sym_pins(new_sym)
            if ([(p.name, p.dir) for p in old_pins] != [(p.name, p.dir) for p in new_pins]
                    or old_sym.global_props.get("type", "") != new_sym.global_props.get("type", "")):
                raise ValueError(f"Cannot preserve {name}: symbol pin names, order, directions, and type must stay unchanged")
            changed |= any((a.x, a.y) != (b.x, b.y) for a, b in zip(old_pins, new_pins))
        result = deepcopy(before)
        if changed:
            # A private editor keeps the caller's resolver and connectivity cache
            # untouched, including on a failed routing attempt.
            planner = SchEditor(self.path, new_resolver)
            planner._follow_connections(before, result, set(), old_resolver=old_resolver)
        return result

    def _follow_connections(self, old: XFile, f: XFile, selected_wires: set[int],
                            dx: float = 0, dy: float = 0, *,
                            old_resolver: xschem.SymbolResolver | None = None) -> dict[int, list[str]]:
        """Rebuild affected conductors from their electrical attachments.

        A bend or a branch coordinate is drawing geometry, not an electrical
        anchor. Keep pins and free ends connected while letting that geometry
        move out of a device's way. All planning and net validation happen in
        memory before the caller's single file/log commit.
        """
        old_resolver = old_resolver or self.resolver
        old_pins = self._pins(old, old_resolver)
        new_pins = {key: point for key, _, point, _ in self._pins(f)}
        label_instances = set()
        for ci, comp in enumerate(old.comps):
            symbol = schematic.resolve_symbol(old_resolver, comp, self.path.parent)
            if symbol is not None and symbol.global_props.get("type") == "label":
                label_instances.add(ci)
        attached: dict[tuple, list[tuple]] = {}
        for key, _, point, _ in old_pins:
            if key[0] not in label_instances:
                attached.setdefault(point, []).append(key)
        # A net-name annotation attached directly to a device belongs with its
        # pin. External ports remain anchored, as do shared fixed device pins.
        for key, _, point, _ in old_pins:
            ci = key[0]
            if ci not in label_instances or not attached.get(point):
                continue
            destinations = {new_pins[other] for other in attached[point]}
            before, after = old.comps[ci], f.comps[ci]
            if len(destinations) == 1 and (after.x, after.y) == (before.x, before.y):
                destination = next(iter(destinations))
                after.x += destination[0] - new_pins[key][0]
                after.y += destination[1] - new_pins[key][1]
        old_connections = self._connections(old, resolver=old_resolver)
        net_of = {token: net for net, tokens in enumerate(old_connections) for token in tokens}
        new_pins = {key: point for key, _, point, _ in self._pins(f)}
        pins_at: dict[tuple, list[tuple]] = {}
        for key, _, point, _ in old_pins:
            pins_at.setdefault(point, []).append(key)
        nodes = set(pins_at)
        for w in old.wires:
            nodes.update((tuple(w[:2]), tuple(w[2:4])))
        ordered_nodes = sorted(nodes)
        node_index = {p: i for i, p in enumerate(ordered_nodes)}
        uf = schematic._UF(len(nodes))
        neighbors: dict[tuple, set[tuple]] = {}
        for w in old.wires:
            points = sorted((p for p in nodes if schematic._on_segment(*p, *w[:4])),
                            key=lambda p: (p[0] - w[0]) * (w[2] - w[0]) + (p[1] - w[1]) * (w[3] - w[1]))
            for a, b in zip(points, points[1:]):
                uf.union(node_index[a], node_index[b])
                neighbors.setdefault(a, set()).add(b)
                neighbors.setdefault(b, set()).add(a)
        # These are physical components, deliberately not joined by matching
        # labels: two remote pieces of the same named net need not be redrawn.
        groups: dict[int, dict] = {}
        for p in ordered_nodes:
            group = groups.setdefault(uf.find(node_index[p]), {"points": [], "wires": []})
            group["points"].append(p)
        for wi, w in enumerate(old.wires):
            groups[uf.find(node_index[tuple(w[:2])])]["wires"].append(wi)
        obstacles = schroute.body_obstacles(f, self.resolver, self.path.parent)
        def blocked_wire(w):
            return ((w[0] != w[2] and w[1] != w[3]) or
                    schroute.segment_blocked(f, self.resolver, self.path.parent,
                                            tuple(w[:2]), tuple(w[2:4]), obstacles=obstacles))

        def newly_touched(wi):
            return any(net_of[('p', *key)] != net_of[('w', wi)] and
                       schematic._on_segment(*p, *old.wires[wi][:4]) for key, p in new_pins.items())

        affected = []
        impacted = set()
        following_labels = set()
        for group in groups.values():
            group["pins"] = [key for p in group["points"] for key in pins_at.get(p, [])]
            conductor_moves = (
                any(new_pins[key] != p for p in group["points"] for key in pins_at.get(p, [])
                    if key[0] not in label_instances) or
                any(wi in selected_wires or blocked_wire(old.wires[wi]) or newly_touched(wi)
                    for wi in group["wires"]))
            group["labels"] = {key for p in group["points"] for key in pins_at.get(p, [])
                               if conductor_moves and group["wires"] and key[0] in label_instances
                               and not attached.get(p)}
            following_labels.update(key[0] for key in group["labels"])
            if (any(new_pins[key] != p for p in group["points"] for key in pins_at.get(p, [])) or
                    conductor_moves):
                affected.append(group)
                impacted.update(group["wires"])
        f.wires = [w for wi, w in enumerate(old.wires) if wi not in impacted]
        # Following annotations must not obstruct or dictate the new route.
        # Keep the editable model intact; the router only needs these two lists.
        routing = XFile(comps=[c for ci, c in enumerate(f.comps) if ci not in following_labels], wires=f.wires)
        origins = [{wi} for wi in range(len(old.wires)) if wi not in impacted]
        wire_nets = [net_of[('w', wi)] for wi in range(len(old.wires)) if wi not in impacted]
        ids: dict[int, list[str]] = {wi: [] for wi in impacted}
        label_attachments = []

        def append_route(a, b, source, net):
            if a == b:
                return
            allowed = {i for i, original in enumerate(origins) if original & source}
            points = schroute.route(routing, self.resolver, self.path.parent, a, b,
                                    allowed_wire_indices=allowed)
            for p, q in zip(points, points[1:]):
                reusable = [i for i, original in enumerate(origins) if original & source]
                for i in reusable:
                    if self._uncovered(p, q, [f.wires[i]]) != [(p, q)]:
                        origins[i].update(source)
                for u, v in self._uncovered(p, q, [f.wires[i] for i in reusable]):
                    f.wires.append((*u, *v, {}))
                    origins.append(set(source))
                    wire_nets.append(net)

        def free_end(point, net):
            """A free wire end may move to the nearest safe side of a new body."""
            other_pins = {p for key, p in new_pins.items()
                          if key[0] not in following_labels and net_of[('p', *key)] != net}
            if not any(ob.contains(point) for ob in obstacles) and point not in other_pins:
                return point
            candidates = {(ob.xmin, point[1]) for ob in obstacles if ob.contains(point)}
            candidates |= {(ob.xmax, point[1]) for ob in obstacles if ob.contains(point)}
            candidates |= {(point[0], ob.ymin) for ob in obstacles if ob.contains(point)}
            candidates |= {(point[0], ob.ymax) for ob in obstacles if ob.contains(point)}
            candidates |= {(point[0] + x, point[1] + y) for x, y in
                           ((-schroute.CLEARANCE, 0), (schroute.CLEARANCE, 0),
                            (0, -schroute.CLEARANCE), (0, schroute.CLEARANCE))}
            for p in sorted(candidates, key=lambda p: (abs(p[0] - point[0]) + abs(p[1] - point[1]), p)):
                if not any(ob.contains(p) for ob in obstacles) and p not in other_pins:
                    return p
            raise ValueError("No safe wire route: a wire end is blocked by a device")

        for group in affected:
            source = set(group["wires"])
            token = ('w', min(source)) if source else ('p', *group["pins"][0])
            net = net_of[token]
            fixed_pins = set(group["pins"]) - group["labels"]
            anchors = {new_pins[key] for key in fixed_pins}
            deltas = {(new_pins[key][0] - p[0], new_pins[key][1] - p[1])
                      for p in group["points"] for key in pins_at.get(p, []) if key in fixed_pins}
            uniform = next(iter(deltas)) if len(deltas) == 1 else (0, 0)
            for key, _, point, _ in old_pins:
                if key not in group["labels"]:
                    continue
                chosen = any(wi in selected_wires and schematic._on_segment(*point, *old.wires[wi][:4])
                             for wi in source)
                shift = (dx, dy) if chosen else uniform
                # An explicitly selected label has already moved in the model.
                # Apply a conductor's translation only when it has not moved.
                hint = new_pins[key] if new_pins[key] != point else (point[0] + shift[0], point[1] + shift[1])
                label_attachments.append((key, hint, source))
            for p in group["points"]:
                if len(neighbors.get(p, ())) > 1 or fixed_pins.intersection(pins_at.get(p, [])):
                    continue
                chosen = any(wi in selected_wires and p in (old.wires[wi][:2], old.wires[wi][2:4])
                             for wi in source)
                shift = (dx, dy) if chosen else uniform
                anchors.add(free_end((p[0] + shift[0], p[1] + shift[1]), net))
            # Explicitly moved wires retain their translated endpoints, while
            # device-only moves are free to choose entirely new bends/junctions.
            selected_routes = []
            for wi in sorted(source & selected_wires):
                w = old.wires[wi]
                a, b = (w[0] + dx, w[1] + dy), (w[2] + dx, w[3] + dy)
                anchors.update((a, b))
                selected_routes.append((a, b))
            if source and not anchors:
                # A floating closed conductor has neither pins nor free ends.
                # Retain its extent rather than dropping its wire identities.
                extremes = (min(group["points"]), max(group["points"]))
                anchors.update(free_end(p, net) for p in extremes)
            pending = set(anchors)
            connected = {min(pending)} if pending else set()
            pending -= connected
            while pending:
                candidates = []
                for target in pending:
                    attachment = set(connected)
                    for i, w in enumerate(f.wires):
                        if wire_nets[i] != net or not origins[i] & source:
                            continue
                        attachment.update((tuple(w[:2]), tuple(w[2:4])))
                        if w[1] == w[3]:
                            attachment.add((max(min(target[0], max(w[0], w[2])), min(w[0], w[2])), w[1]))
                        elif w[0] == w[2]:
                            attachment.add((w[0], max(min(target[1], max(w[1], w[3])), min(w[1], w[3]))))
                    candidates.extend((abs(target[0] - a[0]) + abs(target[1] - a[1]), target, a)
                                      for a in attachment)
                failure = None
                for _, target, attachment in sorted(candidates):
                    try:
                        append_route(attachment, target, source, net)
                        connected.add(target)
                        pending.remove(target)
                        break
                    except ValueError as exc:
                        failure = exc
                else:
                    raise failure or ValueError("No safe wire route found")
            for a, b in selected_routes:
                append_route(a, b, source, net)
            existing = [i for i, original in enumerate(origins) if original & source]
            if source and not existing:
                # A conductor collapsed to coincident pins still carries its
                # original identity (and any label) in the equivalence check.
                point = min(anchors)
                f.wires.append((*point, *point, {}))
                origins.append(set(source))
                wire_nets.append(net)
                existing = [len(f.wires) - 1]
            # Keep every distinct wire property set, especially net labels.
            # Split a new span if needed rather than duplicating conductors.
            properties = []
            for wi in sorted(source):
                props = old.wires[wi][4]
                if props and props not in properties:
                    properties.append(props)
            while len(existing) < len(properties):
                i = max(existing, key=lambda i: abs(f.wires[i][0] - f.wires[i][2]) + abs(f.wires[i][1] - f.wires[i][3]))
                w = f.wires[i]
                mid = ((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)
                if mid == w[:2]:
                    raise ValueError("Moving these objects would lose wire labels; choose another position")
                f.wires[i] = (*w[:2], *mid, w[4])
                f.wires.append((*mid, *w[2:4], {}))
                origins.append(set(origins[i]))
                wire_nets.append(net)
                existing.append(len(f.wires) - 1)
            for index, i in enumerate(existing):
                props = properties[min(index, len(properties) - 1)] if properties else {}
                f.wires[i] = (*f.wires[i][:4], dict(props))
        for key, hint, source in label_attachments:
            candidates = set()
            foreign_wires = [w for i, w in enumerate(f.wires) if not origins[i] & source]
            for i, w in enumerate(f.wires):
                if not origins[i] & source:
                    continue
                vx, vy = w[2] - w[0], w[3] - w[1]
                length2 = vx * vx + vy * vy
                along = max(0., min(1., ((hint[0] - w[0]) * vx + (hint[1] - w[1]) * vy) / length2)) if length2 else 0.
                # The projection preserves the old position on a translated
                # span. Endpoints and nearby positions avoid new crossings.
                step = schroute.CLEARANCE / (abs(vx) + abs(vy)) if length2 else 0.
                for t in (along, max(0., along - step), min(1., along + step), 0., .5, 1.):
                    candidates.add(_on_grid((w[0] + t * vx, w[1] + t * vy), w))
            foreign_pins = {p for other, p in new_pins.items()
                            if other[0] not in following_labels and net_of[('p', *other)] != net_of[('p', *key)]}
            ordered = sorted(candidates, key=lambda p: (abs(p[0] - hint[0]) + abs(p[1] - hint[1]), p))
            destination = next((p for p in ordered if p not in foreign_pins
                                and not any(schematic._on_segment(*p, *w[:4]) for w in foreign_wires)
                                and not any(ob.contains(p) and p not in ob.terminals for ob in obstacles)), None)
            if destination is None:
                raise ValueError("No safe location for the label on the moved wire")
            comp = f.comps[key[0]]
            comp.x += destination[0] - new_pins[key][0]
            comp.y += destination[1] - new_pins[key][1]
            new_pins[key] = destination
        if old_connections != self._connections(f, origins):
            raise ValueError("Moving these objects would change circuit connections; choose another position")
        for i, source in enumerate(origins):
            for wi in source & impacted:
                ids[wi].append(xschem.wire_id(*f.wires[i][:4]))
        return ids

    def add_text(self, s: str, x: float, y: float, size: float = 0.3) -> str:
        with self.lock:
            before = self._text()
            f = self._load()
            f.texts.append(XText(s, x, y, 0, 0, size, size, {}))
            self._commit(f, before)
            return f"{x:g},{y:g},{s}"

    def _move_plan(self, items: list[dict], dx: float, dy: float, copy: bool = False) -> tuple[XFile, list[str], str]:
        """The shared, side-effect-free model calculation for preview and commit."""
        before = self._text()
        f = self._load()
        old = xschem.parse(before)
        targets = list(dict.fromkeys((it["k"], i) for it in items
                                     for i in self._ids(f, it["k"], str(it["id"]))))
        add_w, add_t, out = [], [], []
        for k, i in targets:
            if k == "i":
                c = f.comps[i]
                if copy:
                    props = dict(c.props)
                    props["name"] = self._unique_name(f, props.get("name", "x1"))
                    f.comps.append(XComp(c.sym, c.x + dx, c.y + dy, c.rot, c.flip, props, c.embedded))
                    out.append(props["name"])
                else:
                    c.x += dx
                    c.y += dy
                    out.append(xschem.comp_ids(f)[i])
            elif k == "w":
                x1, y1, x2, y2, pr = f.wires[i]
                nw = (x1 + dx, y1 + dy, x2 + dx, y2 + dy, dict(pr))
                out.append(xschem.wire_id(*nw[:4]))
                if copy:
                    add_w.append(nw)
                else:
                    f.wires[i] = nw
            elif k == "t":
                t = f.texts[i]
                if copy:
                    add_t.append(XText(t.s, t.x + dx, t.y + dy, t.rot, t.flip, t.xs, t.ys, dict(t.props)))
                else:
                    t.x += dx
                    t.y += dy
        f.wires += add_w
        f.texts += add_t
        xschem.normalize_names(f)
        if not copy and (dx or dy) and self.path.suffix == ".sch" and any(k in ("i", "w") for k, _ in targets):
            selected_wires = {i for k, i in targets if k == "w"}
            routed = self._follow_connections(old, f, selected_wires, dx, dy)
            # Routed wires may contain several new segments, so return the
            # actual resulting ids rather than the discarded straight line.
            moved_ids = {xschem.wire_id(old.wires[i][0] + dx, old.wires[i][1] + dy,
                                       old.wires[i][2] + dx, old.wires[i][3] + dy)
                         for i in selected_wires}
            out = [ref for ref in out if ref not in moved_ids]
            out += list(dict.fromkeys(ref for i in selected_wires for ref in routed.get(i, [])))
        return f, out, before

    def preview_move(self, items: list[dict], dx: float, dy: float) -> dict:
        """Render exactly the proposed move without writing files or history. A position whose routes take longer
        than the preview budget is refused (the drag goes on to the next position; the move itself has EDIT_BUDGET)."""
        with self.lock, schroute.time_budget(PREVIEW_BUDGET):
            f, out, _ = self._move_plan(items, dx, dy)
            if self.path.suffix == ".sch":
                xschem.canonical(f)            # drawn exactly as the move will be written
            view = xschem.render_model(f, self.path, self.resolver)
            ids = xschem.comp_ids(f)
            view["terminals"] = [[ids[ci], name, *point]
                                 for (ci, _), name, point, _ in self._pins(f)]
            return {"result": out, "view": view}

    def move(self, items: list[dict], dx: float, dy: float, copy: bool = False, owned: bool = False) -> list[str]:
        """Moves (or duplicates) objects given by stable id; returns the ids of the result. `owned`: the instances moved
        are placed on purpose now, so tidy leaves them where they are (netlist.PLACED), as after a person's move."""
        with self.lock, schroute.time_budget(EDIT_BUDGET):
            f, out, before = self._move_plan(items, dx, dy, copy)
            if owned and not copy:
                chosen = {str(it["id"]) for it in items if it["k"] == "i"}
                for cid, c in zip(xschem.comp_ids(f), f.comps):
                    if cid in chosen:
                        c.props.pop(netlist.PLACED, None)
            self._commit(f, before)
            return out

    def copy_items(self, items: list[dict]) -> dict:
        """Take an independent selection snapshot without editing the source file."""
        with self.lock:
            f = self._load()
            targets = list(dict.fromkeys((it["k"], i) for it in items
                                         for i in self._ids(f, it["k"], str(it["id"]))))
            if not targets:
                raise ValueError("select objects to copy")
            fragment = XFile()
            for kind, i in targets:
                if kind == "i":
                    fragment.comps.append(deepcopy(f.comps[i]))
                elif kind == "w":
                    fragment.wires.append(deepcopy(f.wires[i]))
                else:
                    fragment.texts.append(deepcopy(f.texts[i]))
            view = xschem.render_model(fragment, self.path, self.resolver)
            return {"payload": {"kind": "undine-schematic", "version": 1,
                                "fragment": xschem.dump(fragment)},
                    "bbox": view["bbox"], "view": view}

    def paste_items(self, payload: dict, dx: float, dy: float) -> list[str]:
        """Paste a selection snapshot, with fresh instance names and one undo record."""
        with self.lock:
            if (not isinstance(payload, dict) or payload.get("kind") != "undine-schematic"
                    or payload.get("version") != 1 or not isinstance(payload.get("fragment"), str)):
                raise ValueError("invalid schematic clipboard")
            text = payload["fragment"]
            if len(text) > 2_000_000 or not math.isfinite(dx) or not math.isfinite(dy):
                raise ValueError("invalid schematic clipboard")
            fragment = xschem.parse(text)
            # The parser intentionally tolerates broken source files. Clipboard
            # payloads must instead be complete, canonical selection snapshots.
            if (xschem.dump(fragment) != text or fragment.lines or fragment.boxes or fragment.polys
                    or fragment.arcs or fragment.global_props or fragment.g_text or fragment.code
                    or not 0 < len(fragment.comps) + len(fragment.wires) + len(fragment.texts) <= 5000):
                raise ValueError("invalid schematic clipboard")

            def finite(value):
                if isinstance(value, float) and not math.isfinite(value):
                    raise ValueError("clipboard coordinates must be finite")
                if isinstance(value, dict):
                    for part in value.values():
                        finite(part)
                elif isinstance(value, (list, tuple)):
                    for part in value:
                        finite(part)

            finite(asdict(fragment))
            before = self._text()
            f = self._load()
            out = []
            for c in fragment.comps:
                c.x += dx
                c.y += dy
                name = c.props.get("name", "")
                if not name or name in {v.props.get("name") for v in f.comps}:
                    name = self._unique_name(f, name or "x1")
                c.props["name"] = name
                f.comps.append(c)
                out.append(name)
            for x1, y1, x2, y2, props in fragment.wires:
                wire = (x1 + dx, y1 + dy, x2 + dx, y2 + dy, props)
                f.wires.append(wire)
                out.append(xschem.wire_id(*wire[:4]))
            for t in fragment.texts:
                t.x += dx
                t.y += dy
                f.texts.append(t)
                out.append(f"{t.x:g},{t.y:g},{t.s}")
            finite(asdict(f))
            self._commit(f, before)
            return out

    def orient(self, items: list[dict], rotate: int = 0, mirror: bool = False) -> None:
        """Rotates by 90 degree steps / mirrors instances around their origin."""
        with self.lock:
            before = self._text()
            f = self._load()
            old = xschem.parse(before)
            targets = set()
            for it in items:
                if it["k"] != "i":
                    continue
                for i in self._ids(f, "i", str(it["id"])):
                    if i in targets:
                        continue
                    targets.add(i)
                    c = f.comps[i]
                    if mirror:
                        c.flip ^= 1
                    c.rot = (c.rot + rotate) & 3
            if targets and (rotate or mirror) and self.path.suffix == ".sch":
                self._follow_connections(old, f, set())
            self._commit(f, before)

    def delete(self, items: list[dict]) -> None:
        with self.lock:
            before = self._text()
            f = self._load()
            drop: dict[str, set[int]] = {"i": set(), "w": set(), "t": set()}
            for it in items:
                drop[it["k"]].update(self._ids(f, it["k"], str(it["id"])))
            f.comps = [c for n, c in enumerate(f.comps) if n not in drop["i"]]
            f.wires = [w for n, w in enumerate(f.wires) if n not in drop["w"]]
            f.texts = [t for n, t in enumerate(f.texts) if n not in drop["t"]]
            self._commit(f, before)

    def set_props(self, kind: str, ref: str, props: dict) -> str:
        """Sets attributes of an object; returns its (possibly new) id."""
        with self.lock:
            before = self._text()
            f = self._load()
            idx = self._ids(f, kind, ref)[0]
            new_id = ref
            if kind == "i":
                c = f.comps[idx]
                if props.get("name") and props["name"] != c.props.get("name") and \
                        props["name"] in {x.props.get("name") for x in f.comps}:
                    raise ValueError(f"an instance named {props['name']} already exists")
                for k, v in props.items():
                    if v is None:
                        c.props.pop(k, None)
                    else:
                        c.props[k] = str(v)
                xschem.normalize_names(f)
                new_id = xschem.comp_ids(f)[idx]
            elif kind == "w":
                x1, y1, x2, y2, pr = f.wires[idx]
                pr = dict(pr)
                for k, v in props.items():
                    if v in (None, ""):
                        pr.pop(k, None)
                    else:
                        pr[k] = str(v)
                f.wires[idx] = (x1, y1, x2, y2, pr)
            elif kind == "t":
                f.texts[idx].s = str(props.get("text", f.texts[idx].s))
                new_id = f"{f.texts[idx].x:g},{f.texts[idx].y:g},{f.texts[idx].s}"
            self._commit(f, before)
            return new_id

    def instance(self, ref: str) -> dict:
        with self.lock:
            f = self._load()
            c = f.comps[self._ids(f, "i", ref)[0]]
            s = self.resolver.load(c.sym, self.path.parent)
            return {"id": ref, "symbol": c.sym, "props": c.props, "template": schematic.sym_template(s),
                    "type": s.global_props.get("type", "") if s else ""}
