"""Object editing for Xschem symbols, shared by the UI and operation log.

Pins are layer-5 boxes, whose file order is the subcircuit terminal order.
Object handles include their content so an old browser selection cannot silently
edit a different primitive after another writer changes the file.
"""
from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from dataclasses import asdict

from undine.schematics import xschem
from undine.schematics.xschem import XFile, XText

PIN_LABEL = "undine_pin"


def _number(values: dict, key: str, default: float | None = None) -> float:
    value = values.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{key} must be a finite number")
    if abs(value) > 1e9:
        raise ValueError(f"{key} is outside the supported coordinate range")
    return value


def _integer(values: dict, key: str, default: int) -> int:
    value = _number(values, key, default)
    if value != int(value):
        raise ValueError(f"{key} must be an integer")
    return int(value)


def _text(values: dict, key: str, default: str = "") -> str:
    value = values.get(key, default)
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError(f"{key} must be nonempty text of at most 4096 characters")
    return value


def _layer(values: dict, default: int) -> int:
    layer = _integer(values, "layer", default)
    if not 0 <= layer < len(xschem.DARK_COLORS):
        raise ValueError("invalid drawing layer")
    return layer


def _pin_name(values: dict, f: XFile, old: str | None = None) -> str:
    name = _text(values, "name", old or "").strip()
    if any(c.isspace() for c in name):
        raise ValueError("pin names cannot contain whitespace")
    if name != old and any(b[0] == xschem.PIN_LAYER and b[5].get("name") == name for b in f.boxes):
        raise ValueError(f"a pin named {name} already exists")
    return name


def _direction(values: dict, default: str = "inout") -> str:
    value = values.get("direction", default)
    if value not in ("in", "out", "inout"):
        raise ValueError("pin direction must be in, out, or inout")
    return value


def _records(f: XFile) -> list[tuple[str, int, dict]]:
    result = []
    pin_order = 0
    pin_names = {b[5].get("name", "") for b in f.boxes if b[0] == xschem.PIN_LAYER}
    for collection in ("lines", "boxes", "texts", "polys", "arcs"):
        for i, item in enumerate(getattr(f, collection)):
            raw = asdict(item) if collection == "texts" else item
            def canonical(value):
                if isinstance(value, (int, float)):
                    return format(value, ".10g")
                if isinstance(value, dict):
                    return {k: canonical(v) for k, v in value.items()}
                if isinstance(value, (list, tuple)):
                    return [canonical(v) for v in value]
                return value
            digest = hashlib.sha256(json.dumps(canonical(raw), sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:20]
            obj: dict = {"id": f"{collection}:{i}:{digest}"}
            if collection in ("lines", "boxes"):
                layer, x1, y1, x2, y2, props = item
                obj.update(kind="line" if collection == "lines" else "rectangle", layer=layer,
                           x=x1, y=y1, x2=x2, y2=y2,
                           bbox=[min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)])
                if collection == "boxes" and layer == xschem.PIN_LAYER:
                    pin_order += 1
                    obj.update(kind="pin", x=(x1 + x2) / 2, y=(y1 + y2) / 2,
                               name=props.get("name", ""), direction=props.get("dir", "inout"), order=pin_order)
                    obj.pop("x2"); obj.pop("y2")
            elif collection == "texts":
                # An explicitly associated label is part of the pin, not a second selectable object.
                if item.props.get(PIN_LABEL) in pin_names:
                    continue
                prims = xschem.Prims()
                xschem._emit_text(prims, item, item.s, item.x, item.y, item.rot, item.flip, xschem.TEXT_LAYER)
                emitted = prims.texts
                bbox = xschem.text_box(emitted[0]) if emitted else [item.x, item.y, item.x, item.y]
                obj.update(kind="text", x=item.x, y=item.y, text=item.s, size=item.xs,
                           layer=int(item.props.get("layer", xschem.TEXT_LAYER)), bbox=bbox)
            elif collection == "polys":
                layer, pts, _ = item
                if len(pts) < 2:
                    continue
                obj.update(kind="polygon", layer=layer, points=list(pts), x=min(pts[::2]), y=min(pts[1::2]),
                           bbox=[min(pts[::2]), min(pts[1::2]), max(pts[::2]), max(pts[1::2])])
            else:
                layer, x, y, r, start, sweep, _ = item
                obj.update(kind="arc", layer=layer, x=x, y=y, radius=r, start=start, sweep=sweep,
                           bbox=[x-r, y-r, x+r, y+r])
            result.append((collection, i, obj))
    return result


def objects(f: XFile) -> list[dict]:
    return [obj for _, _, obj in _records(f)]


def _find(f: XFile, ident: str) -> tuple[str, int, dict]:
    found = next((r for r in _records(f) if r[2]["id"] == ident), None)
    if found is None:
        raise ValueError("the selected symbol object changed; refresh and select it again")
    return found


def _pin_label(f: XFile, name: str, *, dx: float = 0, dy: float = 0, rename: str | None = None,
               lead_x: float | None = None, lead_y: float | None = None,
               skip_lines: set[int] | None = None) -> None:
    for t in f.texts:
        if t.props.get(PIN_LABEL) == name:
            t.x += dx
            t.y += dy
            if rename is not None:
                t.s = rename
                t.props[PIN_LABEL] = rename
    for i, (layer, x1, y1, x2, y2, props) in enumerate(f.lines):
        if props.get(PIN_LABEL) != name:
            continue
        # Generated leads run from the body to the pin. Keep their body-side
        # endpoint fixed unless the whole line is explicitly selected to move.
        if lead_x is not None and i not in (skip_lines or set()):
            x2, y2 = lead_x, lead_y
        if rename is not None:
            props = {**props, PIN_LABEL: rename}
        f.lines[i] = (layer, x1, y1, x2, y2, props)


def _reorder_pin(f: XFile, index: int, order: int) -> int:
    slots = [i for i, b in enumerate(f.boxes) if b[0] == xschem.PIN_LAYER]
    if not 1 <= order <= len(slots):
        raise ValueError(f"pin order must be between 1 and {len(slots)}")
    pins = [f.boxes[i] for i in slots]
    pins.insert(order - 1, pins.pop(slots.index(index)))
    for i, pin in zip(slots, pins):
        f.boxes[i] = pin
    return slots[order - 1]


def _id_at(f: XFile, collection: str, index: int) -> str:
    return next(obj["id"] for col, i, obj in _records(f) if col == collection and i == index)


def _add(f: XFile, values: dict) -> str:
    kind = values.get("kind")
    x, y = _number(values, "x"), _number(values, "y")
    if kind == "pin":
        name, direction = _pin_name(values, f), _direction(values)
        f.boxes.append((xschem.PIN_LAYER, x-2.5, y-2.5, x+2.5, y+2.5, {"name": name, "dir": direction}))
        count = sum(b[0] == xschem.PIN_LAYER for b in f.boxes)
        i = _reorder_pin(f, len(f.boxes)-1, _integer(values, "order", count))
        f.texts.append(XText(name, x+8, y-6, 0, 0, 0.2, 0.2, {PIN_LABEL: name}))
        return _id_at(f, "boxes", i)
    if kind in ("line", "rectangle"):
        x2, y2 = _number(values, "x2"), _number(values, "y2")
        layer = _layer(values, xschem.SYMBOL_LAYER)
        if kind == "rectangle" and layer == xschem.PIN_LAYER:
            raise ValueError("use a pin to create a layer-5 box")
        if (x, y) == (x2, y2) or (kind == "rectangle" and (x == x2 or y == y2)):
            raise ValueError("drawing geometry must have a nonzero size")
        if kind == "rectangle":
            x, y, x2, y2 = min(x, x2), min(y, y2), max(x, x2), max(y, y2)
        collection = "lines" if kind == "line" else "boxes"
        items = getattr(f, collection)
        items.append((layer, x, y, x2, y2, {}))
        return _id_at(f, collection, len(items)-1)
    if kind == "text":
        size = _number(values, "size", 0.3)
        if not 0 < size <= 100:
            raise ValueError("text size must be between 0 and 100")
        layer = _layer(values, xschem.TEXT_LAYER)
        f.texts.append(XText(_text(values, "text"), x, y, 0, 0, size, size, {"layer": str(layer)}))
        return _id_at(f, "texts", len(f.texts)-1)
    raise ValueError(f"cannot add symbol object kind {kind}")


def _update(f: XFile, values: dict) -> str:
    collection, i, obj = _find(f, values.get("id", ""))
    kind = obj["kind"]
    x, y = _number(values, "x", obj["x"]), _number(values, "y", obj["y"])
    if kind == "pin":
        name, direction = _pin_name(values, f, obj["name"]), _direction(values, obj["direction"])
        dx, dy = x-obj["x"], y-obj["y"]
        layer, x1, y1, x2, y2, props = f.boxes[i]
        f.boxes[i] = (layer, x1+dx, y1+dy, x2+dx, y2+dy, {**props, "name": name, "dir": direction})
        _pin_label(f, obj["name"], dx=dx, dy=dy, rename=name, lead_x=x, lead_y=y)
        i = _reorder_pin(f, i, _integer(values, "order", obj["order"]))
    elif kind in ("line", "rectangle"):
        layer = _layer(values, obj["layer"])
        if kind == "rectangle" and layer == xschem.PIN_LAYER:
            raise ValueError("use a pin to create a layer-5 box")
        x2, y2 = _number(values, "x2", obj["x2"]), _number(values, "y2", obj["y2"])
        if (x, y) == (x2, y2) or (kind == "rectangle" and (x == x2 or y == y2)):
            raise ValueError("drawing geometry must have a nonzero size")
        if kind == "rectangle":
            x, y, x2, y2 = min(x, x2), min(y, y2), max(x, x2), max(y, y2)
        items = getattr(f, collection)
        items[i] = (layer, x, y, x2, y2, dict(items[i][-1]))
    elif kind == "text":
        t = f.texts[i]
        size = _number(values, "size", t.xs)
        if not 0 < size <= 100:
            raise ValueError("text size must be between 0 and 100")
        t.s, t.x, t.y, t.xs, t.ys = _text(values, "text", t.s), x, y, size, size
        t.props["layer"] = str(_layer(values, obj["layer"]))
    else:
        _translate(f, collection, i, obj, x-obj["x"], y-obj["y"])
    return _id_at(f, collection, i)


def _translate(f: XFile, collection: str, i: int, obj: dict, dx: float, dy: float,
               skip_lines: set[int] | None = None) -> None:
    items = getattr(f, collection)
    item = items[i]
    if collection in ("lines", "boxes"):
        layer, x1, y1, x2, y2, props = item
        items[i] = (layer, x1+dx, y1+dy, x2+dx, y2+dy, props)
        if obj["kind"] == "pin":
            _pin_label(f, obj["name"], dx=dx, dy=dy, lead_x=obj["x"]+dx, lead_y=obj["y"]+dy,
                       skip_lines=skip_lines)
    elif collection == "texts":
        item.x += dx
        item.y += dy
    elif collection == "polys":
        layer, pts, props = item
        items[i] = (layer, [v+(dx if n % 2 == 0 else dy) for n, v in enumerate(pts)], props)
    elif collection == "arcs":
        layer, x, y, r, a, b, props = item
        items[i] = (layer, x+dx, y+dy, r, a, b, props)


# ------------------------------------------------------------------ the pins from a schematic's ports


def _pin_boxes(f: XFile) -> list[int]:
    return [i for i, b in enumerate(f.boxes) if b[0] == xschem.PIN_LAYER]


def _center(box: tuple) -> tuple[float, float]:
    return (box[1] + box[3]) / 2, (box[2] + box[4]) / 2


def body_box(f: XFile) -> list[float] | None:
    """The bounds of a symbol's drawing without its pins and their leads (lines tagged as a pin's, or ending at a pin)."""
    from undine.schematics.schroute import _arc_extent
    pins = {_center(f.boxes[i]) for i in _pin_boxes(f)}
    pts: list[tuple[float, float]] = []
    for layer, x1, y1, x2, y2, props in f.lines:
        if layer not in (xschem.PIN_LAYER, xschem.TEXT_LAYER) and not props.get(PIN_LABEL) \
                and (x1, y1) not in pins and (x2, y2) not in pins:
            pts += [(x1, y1), (x2, y2)]
    for layer, x1, y1, x2, y2, props in f.boxes:
        if layer not in (xschem.PIN_LAYER, xschem.TEXT_LAYER) and not props.get("flags", "").startswith("graph"):
            pts += [(x1, y1), (x2, y2)]
    for layer, p, _ in f.polys:
        if layer not in (xschem.PIN_LAYER, xschem.TEXT_LAYER):
            pts += [(p[i], p[i + 1]) for i in range(0, len(p) - 1, 2)]
    for layer, cx, cy, r, a, b, _ in f.arcs:
        if layer not in (xschem.PIN_LAYER, xschem.TEXT_LAYER):
            pts += _arc_extent(cx, cy, r, a, b)
    if not pts:
        return None
    xs, ys = zip(*pts)
    return [min(xs), min(ys), max(xs), max(ys)]


def side_of(p: tuple[float, float], box: list[float]) -> str:
    """The side of the body a pin is on: the one it lies furthest beyond, or the nearest edge for a pin inside."""
    out = {"W": box[0] - p[0], "E": p[0] - box[2], "N": box[1] - p[1], "S": p[1] - box[3]}
    side = max(out, key=lambda s: out[s])
    return side if out[side] > 0 else min(out, key=lambda s: -out[s])


def pin_sides(f: XFile) -> dict[str, str]:
    box = body_box(f)
    if box is None:
        return {}
    return {f.boxes[i][5].get("name", ""): side_of(_center(f.boxes[i]), box) for i in _pin_boxes(f)}


def _drop_pin(f: XFile, i: int) -> list[str]:
    """Takes pin box i out with what belongs to it: lines and texts tagged with its name, an untagged lead ending at it,
    and an untagged text of exactly its name next to it. Returns what was taken besides the box (for the report)."""
    name, at = f.boxes[i][5].get("name", ""), _center(f.boxes[i])
    near = lambda x, y: abs(x - at[0]) <= 40 and abs(y - at[1]) <= 40          # noqa: E731
    del f.boxes[i]
    lines = [ln for ln in f.lines if ln[5].get(PIN_LABEL) == name or (not ln[5].get(PIN_LABEL) and at in ((ln[1], ln[2]), (ln[3], ln[4])))]
    texts = [t for t in f.texts if t.props.get(PIN_LABEL) == name or (not t.props.get(PIN_LABEL) and t.s == name and near(t.x, t.y))]
    f.lines = [ln for ln in f.lines if ln not in lines]
    f.texts = [t for t in f.texts if t not in texts]
    return [f"lead of {name}"] * bool(lines) + [f"label of {name}"] * bool(texts)


def _free_slot(side: str, box: list[float], taken: list[tuple[float, float]]) -> tuple[float, float]:
    """A grid position for a new pin on a side of the body: 20 out from its edge, at least 20 from the pins there,
    as near the middle of the side as there is room (beyond the side's ends when it is full)."""
    g = 10.0
    if side in ("W", "E"):
        fixed = math.floor((box[0] - 20) / g) * g if side == "W" else math.ceil((box[2] + 20) / g) * g
        lo, hi = box[1], box[3]
        others = [p[1] for p in taken if abs(p[0] - fixed) < 1e-6]
    else:
        fixed = math.floor((box[1] - 20) / g) * g if side == "N" else math.ceil((box[3] + 20) / g) * g
        lo, hi = box[0], box[2]
        others = [p[0] for p in taken if abs(p[1] - fixed) < 1e-6]
    mid = round((lo + hi) / 2 / g) * g
    span = max(hi - lo, 0) / 2 + 20 * (len(others) + 2)
    for k in sorted(range(-int(span // g), int(span // g) + 1), key=lambda k: (abs(k), k)):
        v = mid + k * g
        if all(abs(v - o) >= 20 - 1e-6 for o in others) and (lo + 10 <= v <= hi - 10 or abs(k) * g > (hi - lo) / 2):
            return (fixed, v) if side in ("W", "E") else (v, fixed)
    return (fixed, mid + span) if side in ("W", "E") else (mid + span, fixed)


def _add_pin(f: XFile, box: list[float], side: str, at: tuple[float, float], name: str, direction: str) -> None:
    x, y = at
    f.boxes.append((xschem.PIN_LAYER, x - 2.5, y - 2.5, x + 2.5, y + 2.5, {"name": name, "dir": direction}))
    edge = {"W": (box[0], y), "E": (box[2], y), "N": (x, box[1]), "S": (x, box[3])}[side]
    f.lines.append((xschem.SYMBOL_LAYER, *edge, x, y, {PIN_LABEL: name}))
    t = {"W": XText(name, box[0] + 5, y - 6, 0, 0, 0.2, 0.2, {}), "E": XText(name, box[2] - 5, y - 6, 0, 1, 0.2, 0.2, {}),
         "N": XText(name, x + 6, box[1] + 5, 1, 0, 0.2, 0.2, {}), "S": XText(name, x - 6, box[3] - 5, 3, 0, 0.2, 0.2, {})}[side]
    t.props[PIN_LABEL] = name
    f.texts.append(t)


def generated(f: XFile) -> bool:
    """Whether a symbol is a plain box with pins as make_symbol draws it (nothing drawn by hand to keep)."""
    body = [ln for ln in f.lines if not ln[5].get(PIN_LABEL)]
    corners = {p for ln in body for p in ((ln[1], ln[2]), (ln[3], ln[4]))}
    xs, ys = {p[0] for p in corners}, {p[1] for p in corners}
    return (len(body) == 4 and len(xs) == len(ys) == 2 and len(corners) == 4
            and all(ln[1] == ln[3] or ln[2] == ln[4] for ln in body)
            and all(b[0] == xschem.PIN_LAYER for b in f.boxes) and not f.polys and not f.arcs
            and all(t.props.get(PIN_LABEL) or t.s in ("@symname", "@name") for t in f.texts))


def from_ports(f: XFile, ports: list[tuple[str, str]], sides: dict[str, str] | None = None,
               regenerate: bool = False) -> tuple[XFile, dict]:
    """The symbol `f` made to match a schematic's ports (name, ipin | opin | iopin, in the subcircuit's order): pins in
    that order with its directions, pins without a port taken out, a pin for each new port, and each pin on the side
    `sides` names (N, S, E, W). The drawing is kept: a new or moved pin goes to a free place on its side of the body,
    with a lead and its name. With `regenerate` (or a symbol with no body to keep) it is drawn anew as a box
    (xschem.make_symbol), keeping its attributes (K record). Returns (symbol, what changed)."""
    seen: set[str] = set()
    ports = [(n, t) for n, t in ports if n and not (n in seen or seen.add(n))]
    port_type = dict(ports)
    sides = {n: s for n, s in (sides or {}).items() if s in xschem.SIDES}
    out = deepcopy(f)
    before = [(b[5].get("name", ""), b[5].get("dir", "inout")) for b in (f.boxes[i] for i in _pin_boxes(f))]
    was_side = pin_sides(f)
    report: dict = {"added": [], "removed": [], "moved": {}, "directions": {}, "order_changed": False, "notes": []}
    want = {n: sides.get(n) or was_side.get(n) or xschem.default_side(t) for n, t in ports}
    box = body_box(out)
    names = [n for n, _ in before]
    first = {n: k for k, n in reversed(list(enumerate(names)))}
    regrown = generated(f) and (set(names) != set(port_type) or len(names) != len(set(names))
                                or any(want[n] != was_side.get(n) for n in port_type if n in was_side))
    if regenerate or box is None or regrown:      # a box symbol of Undine's is drawn anew, at the size its pins need
        new = xschem.make_symbol(ports, want)
        if f.k_raw or f.global_props:
            new.global_props, new.k_raw = dict(f.global_props), f.k_raw
        new.version = f.version or new.version
        out = new
        report["added"] = [n for n, _ in ports if n not in first]
        report["removed"] = [n for n in dict.fromkeys(names) if n not in port_type]
        report["moved"] = {n: [was_side[n], want[n]] for n in port_type if n in was_side and want[n] != was_side[n]}
        report["regenerated"] = True
    else:
        kept: set[str] = set()
        for k, i in reversed(list(enumerate(_pin_boxes(out)))):   # pins without a port, duplicates, and those to move
            name = names[k]
            if name not in port_type or first[name] != k:
                report["notes"] += _drop_pin(out, i)
                report["removed"].append(name)
            elif name in sides and was_side.get(name) and sides[name] != was_side[name]:
                report["notes"] += _drop_pin(out, i)
                report["moved"][name] = [was_side[name], sides[name]]
            else:
                kept.add(name)
        taken = [_center(out.boxes[i]) for i in _pin_boxes(out)]
        for n, t in ports:
            if n in kept:
                continue
            at = _free_slot(want[n], box, taken)
            taken.append(at)
            _add_pin(out, box, want[n], at, n, xschem.PIN_DIR.get(t, "inout"))
            if n not in report["moved"]:
                report["added"].append(n)
        slots = _pin_boxes(out)
        by_name = {out.boxes[i][5].get("name", ""): out.boxes[i] for i in slots}
        for i, (n, t) in zip(slots, ports):                    # the pins in port order, each with its port's direction
            b = by_name[n]
            out.boxes[i] = (*b[:5], {**b[5], "dir": xschem.PIN_DIR.get(t, "inout")})
        report["removed"].reverse()
    after = [(b[5].get("name", ""), b[5].get("dir", "inout")) for b in (out.boxes[i] for i in _pin_boxes(out))]
    old_dir = dict(before)
    report["directions"] = {n: [old_dir[n], d] for n, d in after if n in old_dir and old_dir[n] != d}
    common = [n for n, _ in after if n in old_dir]
    report["order_changed"] = common != [n for n, _ in before if n in dict(after)]
    report["order"] = [n for n, _ in after]
    report["sides"] = pin_sides(out)
    return out, report


def reorder_pins(f: XFile, order: list[str]) -> tuple[XFile, dict]:
    """The symbol `f` with its pins in `order` (those it has; its other pins after them, as they were): the
    subcircuit's pin order only, nothing added, taken out, turned or moved. Returns (symbol, what changed)."""
    out = deepcopy(f)
    slots = _pin_boxes(out)
    rank = {n: k for k, n in reversed(list(enumerate(order)))}
    boxes = sorted((out.boxes[i] for i in slots), key=lambda b: rank.get(b[5].get("name", ""), len(rank)))   # stable
    for i, b in zip(slots, boxes):
        out.boxes[i] = b
    before = [f.boxes[i][5].get("name", "") for i in slots]
    after = [b[5].get("name", "") for b in boxes]
    return out, {"order": after, "order_changed": after != before,
                 "not_in_symbol": [n for n in order if n not in set(before)], "not_ports": [n for n in before if n not in rank]}


def edit(f: XFile, op: str, values: dict):
    """Mutate an in-memory model only; the caller commits the entire operation atomically."""
    if op == "symbol_add":
        return _add(f, values)
    if op == "symbol_update":
        return _update(f, values)
    if op not in ("symbol_move", "symbol_delete"):
        raise ValueError(f"unknown symbol operation {op}")
    ids = values.get("ids")
    if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
        raise ValueError("select symbol objects first")
    records = [_find(f, ident) for ident in dict.fromkeys(ids)]
    if op == "symbol_move":
        dx, dy = _number(values, "dx"), _number(values, "dy")
        selected_lines = {i for collection, i, _ in records if collection == "lines"}
        for collection, i, obj in records:
            _translate(f, collection, i, obj, dx, dy, selected_lines)
        return [_id_at(f, collection, i) for collection, i, _ in records]
    labels = {obj["name"] for _, _, obj in records if obj["kind"] == "pin"}
    for collection in ("lines", "boxes", "texts", "polys", "arcs"):
        drop = {i for col, i, _ in records if col == collection}
        items = [item for i, item in enumerate(getattr(f, collection)) if i not in drop]
        if collection == "texts":
            items = [t for t in items if t.props.get(PIN_LABEL) not in labels]
        elif collection == "lines":
            items = [line for line in items if line[-1].get(PIN_LABEL) not in labels]
        setattr(f, collection, items)
    return []
