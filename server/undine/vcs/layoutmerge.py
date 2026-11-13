"""Three-way merge and semantic diff of layout cell files (`libs/<lib>/<cell>.oas`, kind "layout"):
docs/design/merge.md.

A cell file holds one cell (its shapes, instances and persisted meta information) and the cells it instantiates as
ghost cells, by name (layoutstore.py). Each side is read with KLayout and merged per cell name (Undine writes one cell
per file; a file with several is merged cell by cell, a cell deleted on one side and changed on the other is the
conflict "cell:<cell>"). What a cell file says is what layoutstore.cell_sha hashes:

    shapes       per layer ("68/20"), counted by the shape's value: kind,     "shape:amp:68/20:box (0,0;500,2000)"
                 geometry, sorted properties; merged by region (below)
    instances    with an `undine_inst` name, by that name: delete / modify      "inst:amp:M1"
                 the child cell one value                                      "inst:amp:M1:child"
                 the placement (transformation and array) one value            "inst:amp:M1:placement"
                 the other properties one value each                           "inst:amp:M1:prop:role"
                 without a name (or one not unique in the cell): counted by    "inst:amp:via$1 r0 *1 10,10"
                 the whole value, by region like shapes
    meta info    persisted entries by name, one value each (PCell parameters)  "meta:amp:undine.pdks.pcell"
    dbu          must agree: otherwise one side's file is kept whole           "dbu"

Shapes and unnamed instances have no identity but their value: a changed one is a removed and an added one. They
merge region by region (merge3.regions): changed objects that touch, and an added copy of a removed object with the
removed one (a move: the nearest copy that side removed), are one region; a region only one side changed is taken
from it, a region both sides changed differently is one conflict ("shape:<cell>:<layer>:<first removed shape>",
"inst:<cell>:<first removed instance>"), decided for the whole region. What touches:

    shapes       bounding boxes touching on the same layer (a moved copy: same kind, geometry and properties once
                 moved to the origin)
    instances    the same displacement (children are ghost cells: no geometry to go by; a PCell variant swapped for
                 another keeps the place); a moved copy is an instance of the same child cell

So one side's edit of a place is never combined with the other side's edit of it (no contact twice, no array twice,
no deletion lost to a move); shapes added on both sides that touch are a conflict too (two nets drawn into each other).
Instances of the same child cell only ours and only theirs added (or, named, moved) to the same displacement, a named
one among them, are a warning. A merge without conflicts is no checked layout either way.

The merged file is written the way the layout store writes a cell file (layoutstore.cell_layout), with the children of
the merged instances as its ghost cells. When the merged cells hash like ours' (layoutstore.snapshot), ours' bytes are
returned; like theirs', theirs'.

diff(old, new) reports changes with the same identities: per cell and layer the shapes added and removed (counts, a few
examples in um; id "shape:<cell>:<layer>"), instances added, removed or changed (field by field, placements in um), meta
information. Next to it a geometric diff: per changed layer the XOR area (um^2) of the cell's own shapes before and
after, flagged `geometry_unchanged` when the structural change draws the same area (a polygon replaced by two boxes
covering it) with the same labels (texts have no area: a moved label is a change all the same). Instantiated cells are not flattened into it: moving an instance is an instance change, not an XOR area.

Lifetimes: KLayout objects that refer into a Layout (Cell, Shapes, Shape, Instance) are used only while that Layout is a
local of the same function. Whatever outlives it is copied out first: Python data, geometry and layer infos (KLayout
hands them out as copies owning their data) and cell arrays rebuilt from their numbers (a copy of a layout's regular
array shares its array part with that layout). The box index owns a Shapes container of its own.
"""
from __future__ import annotations

import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import klayout.db as kdb

from undine.layouts import layoutstore
from undine.vcs.merge3 import MISSING, Merge, Result, describe, mapping, regions

KIND = "layout"
NAME = "undine_inst"          # the instance property that names a layout instance (layout.py)
EXAMPLES = 3                  # shapes shown per layer and direction in a diff
NOTE = "geometry: XOR of each cell's own shapes per layer; instantiated cells are not flattened into it"
_DONE = {"add": "added", "del": "removed", "mod": "changed"}


@dataclass
class _Cell:
    """What a cell file says about one cell, by identity."""
    shapes: dict[str, Counter] = field(default_factory=dict)     # layer -> shape value -> count
    insts: dict[str, dict] = field(default_factory=dict)         # name -> {"child", "placement", "props"}
    loose: Counter = field(default_factory=Counter)              # instances without a (unique) name, by value
    meta: dict[str, Any] = field(default_factory=dict)           # persisted meta information


@dataclass
class _File:
    """What a cell file says, and the objects behind the values (to write a merged file and to measure geometry):
    copies, none of them refers into the layout the file was read from."""
    dbu: float = 0.001
    cells: dict[str, _Cell] = field(default_factory=dict)
    layers: dict[str, kdb.LayerInfo] = field(default_factory=dict)
    shapes: dict[str, tuple[Any, dict]] = field(default_factory=dict)     # value -> (geometry, properties)
    places: dict[str, kdb.CellInstArray] = field(default_factory=dict)    # placement -> array (any cell index)
    loose: dict[str, tuple[str, str, dict]] = field(default_factory=dict)  # value -> (child, placement, properties)
    notes: dict[tuple[str, str], str] = field(default_factory=dict)       # (cell, meta name) -> description


# ---------------------------------------------------------------- reading


def _load(data: bytes, tmp: Path, name: str) -> kdb.Layout:
    path = tmp / f"{name}{layoutstore.SUFFIX}"          # KLayout reads from a path
    path.write_bytes(data)
    ly = kdb.Layout(True)             # editable: irregular repetitions read as single instances, arrays are regular
    ly.read(str(path))
    return ly


def _model(ly: kdb.Layout) -> _File:
    """The layout's values by identity: copies, nothing that refers into the layout (module docstring)."""
    f = _File(ly.dbu)
    layers = [(_layer_key(ly.get_info(li)), li) for li in ly.layer_indexes()]
    for c in ly.each_cell():
        if c.is_ghost_cell():
            continue
        cell = f.cells[c.name] = _Cell()
        for key, li in layers:
            n: Counter = Counter()
            for s in c.shapes(li).each():
                g = _geometry(s)
                if g is None:
                    continue
                props = _props(ly, s.prop_id)
                v = f"{g[0]} {g[1]}{_props_text(props)}"
                f.shapes.setdefault(v, (g[1], props))
                n[v] += 1
            if n:
                cell.shapes[key] = cell.shapes.get(key, Counter()) + n
                f.layers.setdefault(key, ly.get_info(li))
        insts = []
        for i in c.each_inst():
            place = _placement(i.cell_inst)
            f.places.setdefault(place, _array(i.cell_inst))
            insts.append((ly.cell(i.cell_index).name, place, _props(ly, i.prop_id)))
        names = Counter(p[NAME] for _, _, p in insts if p.get(NAME))
        for child, place, props in insts:
            if names[props.get(NAME)] == 1:
                cell.insts[props[NAME]] = {"child": child, "placement": place,
                                           "props": {k: v for k, v in props.items() if k != NAME}}
            else:                           # no name, or one that is not unique in the cell
                v = f"{child} {place}{_props_text(props)}"
                f.loose.setdefault(v, (child, place, props))
                cell.loose[v] += 1
        cell.insts = dict(sorted(cell.insts.items()))     # file order means nothing: reports go by name
        for m in sorted(c.each_meta_info(), key=lambda m: m.name):
            if m.is_persisted():
                cell.meta[m.name] = m.value
                f.notes[(c.name, m.name)] = m.description
    return f


def _pool(*sides: _File | None) -> _File:
    """The objects behind every side's values in one table, each from the first side that has it."""
    out = _File(sides[0].dbu)
    for f in reversed([f for f in sides if f is not None]):
        for table in ("layers", "shapes", "places", "loose", "notes"):
            getattr(out, table).update(getattr(f, table))
    return out


def _layer_key(info: kdb.LayerInfo) -> str:
    return info.name if info.is_named() else f"{info.layer}/{info.datatype}"


def _layer_order(key: str) -> tuple:
    l, _, d = key.partition("/")
    return (0, int(l), int(d), "") if l.isdigit() and d.isdigit() else (1, 0, 0, key)


def _geometry(s: kdb.Shape) -> tuple[str, Any] | None:
    if s.is_box():
        return "box", s.box
    if s.is_simple_polygon():         # what the OASIS reader makes of every polygon: same value as a polygon
        return "polygon", s.simple_polygon
    if s.is_polygon():
        return "polygon", s.polygon
    if s.is_path():
        return "path", s.path
    if s.is_text():
        return "text", s.text
    if s.is_edge():
        return "edge", s.edge
    if s.is_edge_pair():
        return "edge_pair", s.edge_pair
    if s.is_point():
        return "point", s.point
    return None


def _props(ly: kdb.Layout, prop_id: int) -> dict:
    return dict(ly.properties(prop_id)) if prop_id else {}


def _props_text(props: dict) -> str:
    if not props:
        return ""
    items = sorted(props.items(), key=lambda kv: (type(kv[0]).__name__, str(kv[0])))
    return " props={" + ", ".join(f"{k!r}: {v!r}" for k, v in items) + "}"


def _placement(arr: kdb.CellInstArray) -> str:
    a = f" array {arr.na}x{arr.nb} a={arr.a} b={arr.b}" if arr.is_regular_array() else ""
    return f"{arr.cplx_trans}{a}"


def _array(arr: kdb.CellInstArray) -> kdb.CellInstArray:
    """A copy of an instance's array that owns its data. A plain copy (dup too) of a layout's regular array shares
    its array part with the layout's array repository and breaks when the layout is gone."""
    if arr.is_regular_array():
        return kdb.CellInstArray(arr.cell_index, arr.cplx_trans, arr.a, arr.b, arr.na, arr.nb)
    return kdb.CellInstArray(arr.cell_index, arr.cplx_trans)


def _bbox(obj: Any) -> kdb.Box:
    return kdb.Box(obj, obj) if isinstance(obj, kdb.Point) else obj.bbox()


def _at_origin(v: str, geo: dict[str, tuple[Any, dict]]) -> str:
    """A shape's value moved so that its bounding box starts at the origin: the same for all its translated copies."""
    obj, props = geo[v]
    return f"{v.split(' ', 1)[0]} {kdb.Trans(-kdb.Vector(_bbox(obj).p1)) * obj}{_props_text(props)}"


class _BoxIndex:
    """Values by bounding box, looked up in KLayout's box tree (a regenerated cell changes thousands of shapes). The
    tree is a Shapes container of its own, in no Layout; boxes go in and come out as coordinates."""

    def __init__(self, boxes: dict[str, kdb.Box]):
        self._values: dict[tuple, list[str]] = {}
        for v, box in boxes.items():
            if not box.empty():
                self._values.setdefault(_corners(box), []).append(v)
        self._tree = kdb.Shapes()
        for k in self._values:
            self._tree.insert(kdb.Box(*k))

    def touching(self, box: kdb.Box) -> set[str]:
        return {v for s in self._tree.each_touching(box) for v in self._values[_corners(s.box)]}


def _corners(box: kdb.Box) -> tuple[int, int, int, int]:
    return box.left, box.bottom, box.right, box.top


def _by(values: Iterable[str], key: Callable[[str], Any]) -> dict[Any, set[str]]:
    out: dict[Any, set[str]] = {}
    for v in values:
        out.setdefault(key(v), set()).add(v)
    return out


_AREA = (kdb.Box, kdb.Polygon, kdb.SimplePolygon, kdb.Path)    # texts, edges: no area


def _region(values, geo: dict[str, tuple[Any, dict]]) -> kdb.Region:
    r = kdb.Region()
    for v in values:
        obj = geo[v][0]
        if isinstance(obj, _AREA):
            r.insert(obj)
    return r


def _no_area(shapes: Counter, geo: dict[str, tuple[Any, dict]]) -> Counter:
    """A layer's shapes that have no area (texts, edges): what the XOR does not see."""
    return Counter({v: n for v, n in shapes.items() if not isinstance(geo[v][0], _AREA)})


# ---------------------------------------------------------------- merge


def merge(base: bytes | None, ours: bytes, theirs: bytes, prefer: dict[str, str] | None = None, **kw) -> Result:
    m = Merge(prefer)
    with tempfile.TemporaryDirectory(prefix="undine-merge-") as tmp:
        d = Path(tmp)
        lo, lt = _load(ours, d, "ours"), _load(theirs, d, "theirs")
        lb = _load(base, d, "base") if base is not None else None
        kept_o, kept_t = layoutstore.snapshot(lo), layoutstore.snapshot(lt)
        o, t = _model(lo), _model(lt)
        b = _model(lb) if lb is not None else None
        del lo, lt, lb                    # from here on only values copied out of them
        if len({f"{f.dbu:.12g}" for f in (o, t, b) if f is not None}) > 1:
            return m.result(_whole(m, b, o, t, ours, theirs), KIND)
        pool = _pool(o, t, b)
        cells = _merge_cells(m, b or _File(o.dbu), o, t, pool)
        ly = _build(cells, pool)
        now = layoutstore.snapshot(ly)
        data = ours if now == kept_o else theirs if now == kept_t else _write(ly, list(cells), d)
    return m.result(data, KIND)


def _whole(m: Merge, b: _File | None, o: _File, t: _File, ours: bytes, theirs: bytes) -> bytes:
    """Database units differ: shapes cannot be compared, so the file is one value (one side's bytes)."""
    def key(f: _File | None):
        return f and (f"{f.dbu:.12g}", f.cells)
    if key(o) == key(t) or key(t) == key(b):
        return ours
    if key(o) == key(b):
        m.combined.append(f"the whole file (theirs changed the database unit to {t.dbu:g} um)")
        return theirs
    side = m.conflict("dbu", f"database unit (ours {o.dbu:g} um, theirs {t.dbu:g} um): the file is kept whole "
                             f"from one side", b.dbu if b is not None else MISSING, o.dbu, t.dbu)
    return theirs if side == "theirs" else ours


def _merge_cells(m: Merge, b: _File, o: _File, t: _File, pool: _File) -> dict[str, _Cell]:
    out: dict[str, _Cell] = {}
    for name in dict.fromkeys([*o.cells, *t.cells, *b.cells]):
        bc, oc, tc = (f.cells.get(name, MISSING) for f in (b, o, t))
        if oc is MISSING or tc is MISSING:
            c = m.value(f"cell:{name}", f"cell {name}", bc, oc, tc, show=_cell_json)
        else:
            c = _merge_cell(m, name, _Cell() if bc is MISSING else bc, oc, tc, pool)
        if c is not MISSING:
            out[name] = c
    return out


def _merge_cell(m: Merge, cell: str, b: _Cell, o: _Cell, t: _Cell, pool: _File) -> _Cell:
    out = _Cell()
    out.meta = mapping(m, f"meta:{cell}:", f"cell {cell}: meta ", b.meta, o.meta, t.meta)
    out.insts = mapping(m, f"inst:{cell}:", f"cell {cell}: instance ", b.insts, o.insts, t.insts,
                        lambda name, bv, ov, tv: _merge_inst(m, cell, name, bv, ov, tv))
    out.loose = regions(m, f"inst:{cell}:", f"cell {cell}", "instance", b.loose, o.loose, t.loose,
                        _same_place(pool), lambda v: pool.loose[v][0], lambda v: _displacement(pool.places[pool.loose[v][1]]))
    _warn_same_place(m, cell, b, o, t, out, pool)
    for layer in sorted({*b.shapes, *o.shapes, *t.shapes}, key=_layer_order):
        n = regions(m, f"shape:{cell}:{layer}:", f"cell {cell} layer {layer}", "shape",
                    *(x.shapes.get(layer, Counter()) for x in (b, o, t)), _touching(pool.shapes),
                    lambda v: _at_origin(v, pool.shapes), lambda v: _center(pool.shapes[v][0]))
        if n:
            out.shapes[layer] = n
    return out


def _merge_inst(m: Merge, cell: str, name: str, b: Any, o: dict, t: dict) -> dict:
    b = {} if b is MISSING else b       # both sides added it: every field is missing -> value
    cid, what = f"inst:{cell}:{name}", f"cell {cell}: instance {name}"
    return {"child": m.value(f"{cid}:child", f"{what} child cell", b.get("child", MISSING), o["child"], t["child"]),
            "placement": m.value(f"{cid}:placement", f"{what} placement", b.get("placement", MISSING),
                                 o["placement"], t["placement"]),
            "props": mapping(m, f"{cid}:prop:", f"{what} property ", b.get("props", {}), o["props"], t["props"])}


def _touching(geo: dict[str, tuple[Any, dict]]) -> Callable[[list], list]:
    """Changed shapes of one layer whose bounding boxes touch (a text or a point touches what it lies on)."""
    def pairs(changed: list) -> list:
        index = _BoxIndex({v: _bbox(geo[v][0]) for v in changed})
        return [(v, w) for v in changed for w in index.touching(_bbox(geo[v][0])) if w != v]
    return pairs


def _same_place(pool: _File) -> Callable[[list], list]:
    """Changed unnamed instances at the same displacement (children are ghost cells: no geometry to go by; a PCell
    variant swapped for another keeps the place)."""
    def pairs(changed: list) -> list:
        at = _by(changed, lambda v: _displacement(pool.places[pool.loose[v][1]]))
        return [(v, w) for group in at.values() for v in group for w in group if v < w]
    return pairs


def _center(obj: Any) -> tuple[float, float]:
    c = _bbox(obj).center()
    return c.x, c.y


def _displacement(arr: kdb.CellInstArray) -> tuple[int, int]:
    d = arr.cplx_trans.disp
    return d.x, d.y


def _warn_same_place(m: Merge, cell: str, b: _Cell, o: _Cell, t: _Cell, out: _Cell, pool: _File) -> None:
    """Instances of one child cell that only ours and only theirs put at the same displacement, a named one among them:
    a warning (unnamed ones alone at one place are one region: a conflict)."""
    ours, theirs = _placed_alone(b, o, t, out, pool), _placed_alone(b, t, o, out, pool)
    for key in sorted(ours.keys() & theirs.keys()):
        if {*ours[key], *theirs[key]} == {"unnamed"}:
            continue                    # unnamed ones at one place are one region: a conflict, not a warning
        child, x, y = key
        m.warn(f"cell {cell}: instances of {child} added on both sides at the same place "
               f"({x * pool.dbu:g},{y * pool.dbu:g} um): ours {', '.join(ours[key])}, theirs {', '.join(theirs[key])}")


def _placed_alone(b: _Cell, x: _Cell, y: _Cell, out: _Cell, pool: _File) -> dict[tuple, list[str]]:
    """The instances in the merged cell that side x added (or, named, moved or gave another child) and side y left
    alone: {(child, dx, dy): their names, "unnamed" for an instance without one}."""
    def at(child: str, place: str) -> tuple:
        return child, *_displacement(pool.places[place])

    def put(rec: dict | None) -> tuple | None:
        return (rec["child"], rec["placement"]) if rec else None
    got: dict[tuple, list[str]] = {}
    for nm, rec in out.insts.items():
        was, mine = put(b.insts.get(nm)), put(x.insts.get(nm))
        if mine not in (None, was) and put(y.insts.get(nm)) == was and put(rec) == mine:
            got.setdefault(at(*mine), []).append(nm)
    for v, k in out.loose.items():
        if x.loose[v] > b.loose[v] == y.loose[v] and k > b.loose[v]:
            got.setdefault(at(*pool.loose[v][:2]), []).append("unnamed")
    return got


def _cell_json(c: Any) -> Any:
    if c is MISSING:
        return None
    return {"shapes": sum(sum(n.values()) for n in c.shapes.values()),
            "instances": len(c.insts) + sum(c.loose.values()), "meta": sorted(c.meta)}


# ---------------------------------------------------------------- writing


def _build(cells: dict[str, _Cell], pool: _File) -> kdb.Layout:
    """The merged cells in a layout of their own; the cells they instantiate and do not define are ghost cells."""
    ly = kdb.Layout()
    ly.dbu = pool.dbu
    for name in cells:
        ly.create_cell(name)

    def insert(c: kdb.Cell, child: str, place: str, props: dict) -> None:
        ch = ly.cell(child)
        if ch is None:
            ch = ly.create_cell(child)
            ch.ghost_cell = True
        arr = pool.places[place].dup()
        arr.cell_index = ch.cell_index()
        i = c.insert(arr)
        if props:
            i.prop_id = ly.properties_id(props)

    for name, mc in cells.items():
        c = ly.cell(name)
        for layer, n in mc.shapes.items():
            sh = c.shapes(ly.layer(pool.layers[layer]))
            for v in sorted(n):
                obj, props = pool.shapes[v]
                for _ in range(n[v]):
                    s = sh.insert(obj)
                    if props:
                        s.prop_id = ly.properties_id(props)
        for nm, rec in mc.insts.items():
            insert(c, rec["child"], rec["placement"], {**rec["props"], NAME: nm})
        for v in sorted(mc.loose):
            for _ in range(mc.loose[v]):
                insert(c, *pool.loose[v])
        for k, val in mc.meta.items():
            c.add_meta_info(kdb.LayoutMetaInfo(k, val, pool.notes.get((name, k)), True))
    return ly


def _write(ly: kdb.Layout, cells: list[str], tmp: Path) -> bytes:
    out = layoutstore.cell_layout(ly, ly.cell(cells[0])) if len(cells) == 1 else ly
    opt = kdb.SaveLayoutOptions()
    opt.format = "OASIS"
    path = tmp / f"merged{layoutstore.SUFFIX}"
    out.write(str(path), opt)
    return path.read_bytes()


# ---------------------------------------------------------------- diff


def diff(old: bytes | None, new: bytes | None, **kw) -> dict:
    changes: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="undine-diff-") as tmp:
        d = Path(tmp)
        la = _load(old, d, "old") if old is not None else None
        lb = _load(new, d, "new") if new is not None else None
    if la is not None and lb is not None and f"{la.dbu:.12g}" != f"{lb.dbu:.12g}":
        changes.append({"do": "mod", "id": "dbu", "what": f"database unit {la.dbu:g} -> {lb.dbu:g} um",
                        "before": la.dbu, "after": lb.dbu})
        la.transform(kdb.ICplxTrans(la.dbu / lb.dbu))      # compare in the new unit
        la.dbu = lb.dbu
    dbu = lb.dbu if lb is not None else la.dbu if la is not None else 0.001
    a, b = (_model(ly) if ly is not None else _File(dbu) for ly in (la, lb))
    del la, lb                                              # from here on only values copied out of them
    pool = _pool(b, a)

    def um(place: str) -> str:
        return _placement_um(pool.places[place], dbu)

    geometry: list[dict] = []
    for name in sorted({*a.cells, *b.cells}):
        ca, cb = a.cells.get(name), b.cells.get(name)
        if ca is None or cb is None:
            do = "add" if ca is None else "del"
            changes.append({"do": do, "id": f"cell:{name}", "what": f"cell {name} {_DONE[do]}",
                            "before": _cell_json(ca or MISSING), "after": _cell_json(cb or MISSING)})
        ca, cb = ca or _Cell(), cb or _Cell()
        changes += _diff_meta(name, ca.meta, cb.meta)
        changes += _diff_insts(name, ca, cb, pool.loose, um)
        for layer in sorted({*ca.shapes, *cb.shapes}, key=_layer_order):
            sa, sb = ca.shapes.get(layer, Counter()), cb.shapes.get(layer, Counter())
            if sa == sb:
                continue
            changes.append(_diff_layer(name, layer, sa, sb, pool.shapes, dbu))
            xor = (_region(sa, pool.shapes) ^ _region(sb, pool.shapes)).area() * dbu * dbu
            # unchanged: the same area drawn otherwise, and the same labels (a moved label has no XOR either)
            same = xor == 0 and _no_area(sa, pool.shapes) == _no_area(sb, pool.shapes)
            geometry.append({"cell": name, "layer": layer, "xor_um2": round(xor, 6), "geometry_unchanged": same})
    return {"kind": KIND, "changes": changes, "geometry": geometry, "note": NOTE}


def _diff_meta(cell: str, a: dict, b: dict) -> list[dict]:
    out = []
    for k in sorted({*a, *b}):
        x, y = a.get(k, MISSING), b.get(k, MISSING)
        if x != y:
            do = "add" if x is MISSING else "del" if y is MISSING else "mod"
            out.append({"do": do, "id": f"meta:{cell}:{k}", "what": f"cell {cell}: meta {k} {_DONE[do]}",
                        "before": describe(x), "after": describe(y)})
    return out


def _diff_insts(cell: str, a: _Cell, b: _Cell, loose: dict, um: Callable[[str], str]) -> list[dict]:
    def show(rec: dict) -> dict:
        return {"child": rec["child"], "placement": um(rec["placement"]), "props": describe(rec["props"])}
    out = []
    for nm in sorted({*a.insts, *b.insts}):
        x, y = a.insts.get(nm), b.insts.get(nm)
        cid = f"inst:{cell}:{nm}"
        if x == y:
            continue
        if x is None or y is None:
            rec, do = (y, "add") if x is None else (x, "del")
            out.append({"do": do, "id": cid,
                        "what": f"cell {cell}: instance {nm} of {rec['child']} at {um(rec['placement'])} {_DONE[do]}",
                        "before": x and show(x), "after": y and show(y)})
            continue
        fields = []
        if x["child"] != y["child"]:
            fields.append(f"child cell {x['child']} -> {y['child']}")
        if x["placement"] != y["placement"]:
            fields.append(f"placement {um(x['placement'])} -> {um(y['placement'])}")
        for k in sorted({*x["props"], *y["props"]}, key=str):
            p, q = x["props"].get(k, MISSING), y["props"].get(k, MISSING)
            if p != q:
                fields.append(f"property {k} {describe(p)} -> {describe(q)}")
        out.append({"do": "mod", "id": cid, "what": f"cell {cell}: instance {nm}: " + "; ".join(fields),
                    "before": show(x), "after": show(y)})
    for v in sorted({*a.loose, *b.loose}):
        na, nb = a.loose[v], b.loose[v]
        if na == nb:
            continue
        child, place, props = loose[v]
        rec, do = {"child": child, "placement": um(place), "props": describe(props)}, "add" if nb > na else "del"
        out.append({"do": do, "id": f"inst:{cell}:{v}",
                    "what": f"cell {cell}: {abs(nb - na)} instance(s) of {child} at {um(place)} {_DONE[do]}",
                    "before": {**rec, "count": na} if na else None, "after": {**rec, "count": nb} if nb else None})
    return out


def _diff_layer(cell: str, layer: str, a: Counter, b: Counter, geo: dict, dbu: float) -> dict:
    added, removed = b - a, a - b
    do = "add" if not a else "del" if not b else "mod"

    def side(n: Counter, which: str, moved: Counter) -> dict:
        return {"shapes": sum(n.values()), which: sum(moved.values()),
                "examples": [_shape_um(v, geo, dbu) for v in sorted(moved)[:EXAMPLES]]}
    return {"do": do, "id": f"shape:{cell}:{layer}",
            "what": f"cell {cell} layer {layer}: shapes {_counts(added, removed)}",
            "before": None if do == "add" else side(a, "removed", removed),
            "after": None if do == "del" else side(b, "added", added)}


def _counts(added: Counter, removed: Counter) -> str:
    plus, minus = sum(added.values()), sum(removed.values())
    return ", ".join(w for w in (plus and f"{plus} added", minus and f"{minus} removed") if w)


def _shape_um(v: str, geo: dict, dbu: float) -> str:
    obj, props = geo[v]
    return f"{v.split(' ', 1)[0]} {obj.to_dtype(dbu)}{_props_text(props)}"


def _placement_um(arr: kdb.CellInstArray, dbu: float) -> str:
    t = arr.cplx_trans
    s = str(kdb.DCplxTrans(t.mag, t.angle, t.is_mirror(), t.disp.to_dtype(dbu)))
    if arr.is_regular_array():
        s += f" array {arr.na}x{arr.nb} a={arr.a.to_dtype(dbu)} b={arr.b.to_dtype(dbu)}"
    return s
