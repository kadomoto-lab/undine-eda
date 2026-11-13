"""Xschem .sch / .sym reader.

The server parses Xschem files and resolves symbols; the browser only gets
already-transformed drawing primitives (lines, boxes, polygons, arcs, wires,
texts) in schematic coordinates (y grows downwards, as in Xschem).
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from undine import _core

# Xschem default dark colour scheme (layer index -> colour).
DARK_COLORS = [
    "#000000", "#6fcfd9", "#4f6f78", "#cfe3e7", "#8fd6b5",
    "#e9929a", "#8ec9ee", "#f08a97", "#f2d58a", "#f5fbfc",
    "#d5a6e6", "#8fd6b5", "#6aa7ee", "#cdbb6e", "#b5d1c0",
    "#f0a7a0", "#c3e8a0", "#7fe0cf", "#c77bb5", "#d8d58a",
    "#ee8f86", "#f0c36a",
]
WIRE_LAYER = 1
TEXT_LAYER = 3
SYMBOL_LAYER = 4
PIN_LAYER = 5
FONT_SCALE = 52.0  # text_bbox(): size = xscale * 52


# --------------------------------------------------------------------------
# Tokenizer


class _Reader:
    def __init__(self, text: str):
        self.s = text
        self.i = 0
        self.n = len(text)

    def skip_ws(self) -> None:
        while self.i < self.n and self.s[self.i] in " \t\r\n":
            self.i += 1

    def eof(self) -> bool:
        self.skip_ws()
        return self.i >= self.n

    def peek(self) -> str:
        self.skip_ws()
        return self.s[self.i] if self.i < self.n else ""

    def char(self) -> str:
        self.skip_ws()
        c = self.s[self.i]
        self.i += 1
        return c

    def word(self) -> str:
        self.skip_ws()
        j = self.i
        while self.i < self.n and self.s[self.i] not in " \t\r\n{}[]":
            self.i += 1
        return self.s[j:self.i]

    def num(self) -> float:
        w = self.word()
        try:
            return float(w)
        except ValueError:
            return 0.0

    def brace(self) -> str:
        """Reads a {...} group; nested braces are kept, backslash escapes removed."""
        self.skip_ws()
        if self.i >= self.n or self.s[self.i] != "{":
            return self.word()
        self.i += 1
        depth = 1
        out = []
        while self.i < self.n:
            c = self.s[self.i]
            if c == "\\" and self.i + 1 < self.n:
                out.append(self.s[self.i + 1])
                self.i += 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    self.i += 1
                    break
            out.append(c)
            self.i += 1
        return "".join(out)

    def bracket_block(self) -> str:
        """Reads an embedded symbol block [ ... ] (after a C record)."""
        self.skip_ws()
        self.i += 1  # '['
        j = self.i
        depth = 0
        while self.i < self.n:
            c = self.s[self.i]
            if c == "\\":
                self.i += 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            elif c == "]" and depth == 0:
                block = self.s[j:self.i]
                self.i += 1
                return block
            self.i += 1
        return self.s[j:]


def _py_parse_props(s: str) -> dict[str, str]:
    """Reference implementation of parse_props (the Rust one in undine._core is used; tests compare the two)."""
    props: dict[str, str] = {}
    i, n = 0, len(s)
    while i < n:
        while i < n and s[i] in " \t\r\n":
            i += 1
        j = i
        while i < n and s[i] not in "= \t\r\n":
            i += 1
        key = s[j:i]
        if i >= n or s[i] != "=":
            if key:
                props.setdefault(key, "")
            continue
        i += 1
        if i < n and s[i] == '"':
            i += 1
            val = []
            while i < n and s[i] != '"':
                if s[i] == "\\" and i + 1 < n:
                    val.append(s[i + 1])
                    i += 2
                    continue
                val.append(s[i])
                i += 1
            i += 1
            props[key] = "".join(val)
        else:
            j = i
            while i < n and s[i] not in " \t\r\n":
                i += 1
            props[key] = s[j:i]
    return props


# --------------------------------------------------------------------------
# Data model


@dataclass
class XText:
    s: str
    x: float
    y: float
    rot: int
    flip: int
    xs: float
    ys: float
    props: dict


@dataclass
class XComp:
    sym: str
    x: float
    y: float
    rot: int
    flip: int
    props: dict
    embedded: "XFile | None" = None


@dataclass
class XFile:
    lines: list = field(default_factory=list)  # (layer, x1, y1, x2, y2, props)
    boxes: list = field(default_factory=list)  # (layer, x1, y1, x2, y2, props)
    polys: list = field(default_factory=list)  # (layer, [x, y, ...], props)
    arcs: list = field(default_factory=list)  # (layer, cx, cy, r, a, b, props)
    texts: list = field(default_factory=list)  # XText
    wires: list = field(default_factory=list)  # (x1, y1, x2, y2, props)
    comps: list = field(default_factory=list)  # XComp
    global_props: dict = field(default_factory=dict)  # K record (symbol attributes)
    g_text: str = ""                                  # G record (schematic-level text)
    code: dict = field(default_factory=dict)          # V / S / E / F raw blocks
    version: str = ""
    k_raw: str = ""                                   # K record as written


def parse_props(s: str) -> dict[str, str]:
    """Parses an Xschem attribute string: key=value key2="quoted value" ... (undine._core, Rust)."""
    return _core.parse_props(s)


def parse(text: str) -> XFile:
    """Parses a .sch / .sym file (undine._core, Rust; about 5x faster than _py_parse, which it must match)."""
    return _core.parse(text, XFile, XComp, XText)


def _py_parse(text: str) -> XFile:
    """Reference implementation of parse."""
    r = _Reader(text)
    f = XFile()
    while not r.eof():
        t = r.char()
        if t == "v":
            f.version = r.brace()
        elif t in "GK":
            raw = r.brace()
            p = _py_parse_props(raw)
            if t == "K":
                f.k_raw = raw
            else:
                f.g_text = raw
            if p:
                f.global_props.update(p)
        elif t in "VSEF":
            f.code[t] = r.brace()
        elif t in "LB":
            layer = int(r.num())
            x1, y1, x2, y2 = r.num(), r.num(), r.num(), r.num()
            props = _py_parse_props(r.brace())
            (f.lines if t == "L" else f.boxes).append((layer, x1, y1, x2, y2, props))
        elif t == "P":
            layer = int(r.num())
            npts = int(r.num())
            pts = [r.num() for _ in range(2 * npts)]
            f.polys.append((layer, pts, _py_parse_props(r.brace())))
        elif t == "A":
            layer = int(r.num())
            vals = [r.num() for _ in range(5)]
            f.arcs.append((layer, *vals, _py_parse_props(r.brace())))
        elif t == "T":
            s = r.brace()
            x, y, rot, flip, xs, ys = (r.num() for _ in range(6))
            f.texts.append(XText(s, x, y, int(rot) & 3, int(flip) & 1, xs, ys, _py_parse_props(r.brace())))
        elif t == "N":
            x1, y1, x2, y2 = r.num(), r.num(), r.num(), r.num()
            f.wires.append((x1, y1, x2, y2, _py_parse_props(r.brace())))
        elif t == "C":
            sym = r.brace()
            x, y, rot, flip = r.num(), r.num(), r.num(), r.num()
            comp = XComp(sym, x, y, int(rot) & 3, int(flip) & 1, _py_parse_props(r.brace()))
            if r.peek() == "[":
                comp.embedded = _py_parse(r.bracket_block())
            f.comps.append(comp)
        elif t in "#*":
            while r.i < r.n and r.s[r.i] != "\n":
                r.i += 1
        else:
            # unknown record: skip to the end of the line
            while r.i < r.n and r.s[r.i] != "\n":
                r.i += 1
    return f


# --------------------------------------------------------------------------
# Symbol resolution and flattening to primitives


def _rot(rot: int, flip: int, x: float, y: float) -> tuple[float, float]:
    """Xschem ROTATION() around the origin."""
    if flip:
        x = -x
    if rot == 0:
        return x, y
    if rot == 1:
        return -y, x
    if rot == 2:
        return -x, -y
    return y, -x


_TOKEN = re.compile(r"@(#[0-9A-Za-z_]+:[A-Za-z_]+|[A-Za-z_][A-Za-z0-9_]*)")


def _subst(s: str, inst: dict, template: dict, symname: str) -> str:
    s = re.sub(r"tcleval\((.*)\)", r"\1", s, flags=re.S)

    def rep(m: re.Match) -> str:
        k = m.group(1)
        if k.startswith("#"):  # pin attributes (e.g. net names): shown only on request in Xschem
            return ""
        if k == "symname":
            return symname
        if k in inst:
            return inst[k]
        if k in template:
            return template[k]
        return ""

    return _TOKEN.sub(rep, s)


class SymbolResolver:
    def __init__(self, search_paths: list[Path]):
        self.paths = [Path(p) for p in search_paths if p and Path(p).is_dir()]
        self.cache: dict[str, XFile | None] = {}

    def find(self, name: str, base: Path | None) -> Path | None:
        cands = []
        if os.path.isabs(name):
            cands.append(Path(name))
        if base is not None:
            cands.append(base / name)
        for p in self.paths:
            cands.append(p / name)
        if not name.endswith((".sym", ".sch")):
            cands += [c.with_name(c.name + ".sym") for c in list(cands)]
        for c in cands:
            if c.is_file():
                return c
        return None

    def load(self, name: str, base: Path | None) -> XFile | None:
        key = f"{base}|{name}"
        if key not in self.cache:
            p = self.find(name, base)
            self.cache[key] = parse(p.read_text(errors="replace")) if p else None
        return self.cache[key]


@dataclass
class Prims:
    lines: list = field(default_factory=list)   # [layer, x1, y1, x2, y2]
    boxes: list = field(default_factory=list)   # [layer, x1, y1, x2, y2, fill]
    polys: list = field(default_factory=list)   # [layer, fill, [pts]]
    arcs: list = field(default_factory=list)    # [layer, cx, cy, r, a, b, fill]
    texts: list = field(default_factory=list)   # {...}
    wires: list = field(default_factory=list)   # [x1, y1, x2, y2, bus, label]
    insts: list = field(default_factory=list)   # {name, symbol, bbox, props}
    missing: set = field(default_factory=set)


def _fill(props: dict) -> int:
    f = props.get("fill", "")
    if f == "full":
        return 2
    return 1 if f == "true" else 0


def _emit_text(out: Prims, t: XText, text: str, x: float, y: float, rot: int, flip: int,
               default_layer: int) -> None:
    if t.props.get("hide", "") == "true" or not text.strip():
        return
    out.texts.append({
        "s": text, "x": x, "y": y, "rot": rot, "flip": flip,
        "size": t.xs * FONT_SCALE,
        "layer": int(t.props.get("layer", default_layer) or default_layer),
        "hc": t.props.get("hcenter", "") == "true",
        "vc": t.props.get("vcenter", "") == "true",
        "bold": t.props.get("weight", "") == "bold",
        "italic": t.props.get("slant", "") in ("italic", "oblique"),
    })


def text_box(t: dict) -> list[float]:
    """Approximate extent of an emitted text, as web/src/editors/schdraw.ts textBox() draws it (Inter: ~0.55 em
    per character), so that fitting the view includes labels, not just their anchors."""
    lines = t["s"].split("\n")
    w = max(len(l) for l in lines) * t["size"] * 0.55
    h = len(lines) * t["size"] * 1.16
    x1, y1, r, f = t["x"], t["y"], t["rot"] & 3, t["flip"]
    if t["hc"]:
        d = w / 2
        if r == 0: x1 += d if f else -d
        elif r == 1: y1 += d if f else -d
        elif r == 2: x1 += -d if f else d
        else: y1 += -d if f else d
    if t["vc"]:
        d = h / 2
        if r == 0: y1 -= d
        elif r == 1: x1 += d
        elif r == 2: y1 += d
        else: x1 -= d
    px, py = x1 + w, y1 + h
    xx = 2 * t["x"] - px if f else px
    if r == 0: rx, ry = xx, py
    elif r == 1: rx, ry = t["x"] - py + t["y"], t["y"] + xx - t["x"]
    elif r == 2: rx, ry = 2 * t["x"] - xx, 2 * t["y"] - py
    else: rx, ry = t["x"] + py - t["y"], t["y"] - xx + t["x"]
    return [min(x1, rx), min(y1, ry), max(x1, rx), max(y1, ry)]


def _emit_symbol(out: Prims, sym: XFile, comp: XComp, symname: str) -> list[float]:
    rot, flip, ox, oy = comp.rot, comp.flip, comp.x, comp.y
    tmpl = parse_props(sym.global_props.get("template", ""))
    bb = [math.inf, math.inf, -math.inf, -math.inf]

    def tp(x: float, y: float) -> tuple[float, float]:
        rx, ry = _rot(rot, flip, x, y)
        rx, ry = rx + ox, ry + oy
        bb[0], bb[1] = min(bb[0], rx), min(bb[1], ry)
        bb[2], bb[3] = max(bb[2], rx), max(bb[3], ry)
        return rx, ry

    for layer, x1, y1, x2, y2, _ in sym.lines:
        out.lines.append([layer, *tp(x1, y1), *tp(x2, y2)])
    for layer, x1, y1, x2, y2, props in sym.boxes:
        if props.get("flags", "").startswith("graph"):
            continue
        a, b = tp(x1, y1), tp(x2, y2)
        out.boxes.append([layer, min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]),
                          _fill(props) if layer != PIN_LAYER else 1])
    for layer, pts, props in sym.polys:
        q = []
        for k in range(0, len(pts), 2):
            q.extend(tp(pts[k], pts[k + 1]))
        out.polys.append([layer, _fill(props), q])
    for layer, cx, cy, r, a, b, props in sym.arcs:
        c = tp(cx, cy)
        # angle transform: Xschem arcs are in degrees, y-down; rotate and mirror
        a2 = a
        if flip:
            a2 = 180 - a - b
        a2 = (a2 - 90 * rot) % 360
        for dx, dy in ((r, r), (-r, -r)):
            tp(cx + dx, cy + dy)
        out.arcs.append([layer, c[0], c[1], r, a2, b, _fill(props)])
    for t in sym.texts:
        text = _subst(t.s, comp.props, tmpl, symname)
        x, y = tp(t.x, t.y)
        trot = (rot + ((t.rot + 2) if (flip and (t.rot & 1)) else t.rot)) & 3
        tflip = flip ^ t.flip
        _emit_text(out, t, text, x, y, trot, tflip, TEXT_LAYER)
    if bb[0] == math.inf:
        bb = [ox - 10, oy - 10, ox + 10, oy + 10]
    return bb


def render(path: Path, resolver: SymbolResolver) -> dict:
    """Flattens one schematic or symbol view (one level) to drawing primitives (undine._core, Rust; symbol files
    are parsed once per process and re-read when they change)."""
    d = _core.render(str(path), [str(p) for p in resolver.paths])
    d["colors"] = DARK_COLORS
    return d


def _py_render(path: Path, resolver: SymbolResolver) -> dict:
    """Reference implementation of render (tests compare the two)."""
    return render_model(parse(path.read_text(errors="replace")), path, resolver)


def render_model(f: XFile, path: Path, resolver: SymbolResolver) -> dict:
    """Render an in-memory edit using the original cell's symbol resolution context.

    Previews must not temporarily overwrite a design file or add operation-log
    records. The same primitive renderer also backs the Rust parity reference.
    """
    out = Prims()
    base = path.parent
    is_symbol = path.suffix == ".sym"

    # The file's own drawing.
    ident = XComp("", 0, 0, 0, 0, {})
    if is_symbol:
        own_tmpl = parse_props(f.global_props.get("template", ""))
        ident.props = own_tmpl
    _emit_symbol(out, f, ident, path.stem)
    if is_symbol:
        # keep raw @tokens visible in the symbol editor view
        out.texts = [t for t in out.texts]

    for x1, y1, x2, y2, props in f.wires:
        out.wires.append([x1, y1, x2, y2, props.get("bus", "") == "true", props.get("lab", ""),
                          wire_id(x1, y1, x2, y2)])

    ids = comp_ids(f)
    for ci, comp in enumerate(f.comps):
        sym = comp.embedded or resolver.load(comp.sym, base)
        symname = Path(comp.sym).stem
        if sym is None:
            out.missing.add(comp.sym)
            bb = [comp.x - 20, comp.y - 20, comp.x + 20, comp.y + 20]
            out.boxes.append([7, *bb, 0])
            out.texts.append({"s": f"? {comp.sym}", "x": bb[0], "y": bb[3] + 2, "rot": 0, "flip": 0,
                              "size": 0.2 * FONT_SCALE, "layer": 7, "hc": False, "vc": False,
                              "bold": False, "italic": False})
        else:
            bb = _emit_symbol(out, sym, comp, symname)
        out.insts.append({
            "i": ci, "id": ids[ci], "x": comp.x, "y": comp.y, "rot": comp.rot, "flip": comp.flip,
            "name": comp.props.get("name", ""),
            "symbol": comp.sym,
            "type": (sym.global_props.get("type", "") if sym else ""),
            "bbox": bb,
            "props": {k: v for k, v in comp.props.items() if len(v) < 200},
        })

    xs, ys = [], []
    for l in out.lines:
        xs += [l[1], l[3]]; ys += [l[2], l[4]]
    for b in out.boxes:
        xs += [b[1], b[3]]; ys += [b[2], b[4]]
    for w in out.wires:
        xs += [w[0], w[2]]; ys += [w[1], w[3]]
    for p in out.polys:
        xs += p[2][0::2]; ys += p[2][1::2]
    for i in out.insts:
        xs += [i["bbox"][0], i["bbox"][2]]; ys += [i["bbox"][1], i["bbox"][3]]
    for t in out.texts:
        tb = text_box(t)
        xs += [tb[0], tb[2]]; ys += [tb[1], tb[3]]
    bbox = [min(xs), min(ys), max(xs), max(ys)] if xs else [-100, -100, 100, 100]

    return {
        "kind": "symbol" if is_symbol else "schematic",
        "bbox": bbox,
        "colors": DARK_COLORS,
        "lines": out.lines, "boxes": out.boxes, "polys": out.polys, "arcs": out.arcs,
        "texts": out.texts, "wires": out.wires, "insts": out.insts,
        "missing": sorted(out.missing),
        "props": {k: v for k, v in f.global_props.items() if k in ("type", "format", "template")},
    }


# --------------------------------------------------------------------------
# Stable identifiers
#
# Instances are identified by their `name` attribute (unique within a schematic; later duplicates of an
# imported file get `name#2` ...; instances without a name get `@<n>`). Wires have no name in the Xschem
# format, so a wire is identified by its end points. Both survive insertions and deletions, so a recorded
# edit ("set W of M1") stays meaningful and Xschem can still edit the same file.


SIDES = ("N", "S", "E", "W")
PIN_DIR = {"ipin": "in", "opin": "out", "iopin": "inout"}


def default_side(port_type: str) -> str:
    """Where make_symbol puts a port without a side of its own: inputs on the left, the rest on the right."""
    return "W" if port_type == "ipin" else "E"


def make_symbol(ports: list[tuple[str, str]], sides: dict[str, str] | None = None) -> XFile:
    """A subcircuit symbol for a schematic's ports (name, ipin | opin | iopin): a box with each port on its side
    (`sides`: N, S, E or W; by default inputs on the left, outputs and in/outs on the right), pins in the schematic's
    port order (which is the subcircuit's pin order)."""
    seen: set[str] = set()
    ports = [(n, t) for n, t in ports if n and not (n in seen or seen.add(n))]
    side_of = {n: (sides or {}).get(n) if (sides or {}).get(n) in SIDES else default_side(t) for n, t in ports}
    on = {s: [n for n, _ in ports if side_of[n] == s] for s in SIDES}
    rows = max(len(on["W"]), len(on["E"]), 1)
    tall = max((len(n) for n in on["N"]), default=0) + max((len(n) for n in on["S"]), default=0)
    hh = max(rows * 10 + 10, _grid(6 * tall / 2 + 15))                       # half height, on the 10 grid
    hw = max(40, -(-(20 + 5 * max((len(n) for n in on["W"] + on["E"]), default=0)) // 10) * 10,
             _grid(10 * (max(len(on["N"]), len(on["S"])) - 1) + 20))        # half width
    f = XFile(version="xschem version=3.4.5 file_version=1.2\n")
    f.global_props = {"type": "subcircuit", "format": "@name @pinlist @symname", "template": "name=x1"}
    f.k_raw = 'type=subcircuit\nformat="@name @pinlist @symname"\ntemplate="name=x1"\n'
    f.lines = [(4, -hw, -hh, hw, -hh, {}), (4, hw, -hh, hw, hh, {}), (4, hw, hh, -hw, hh, {}), (4, -hw, hh, -hw, -hh, {})]
    f.texts = [XText("@symname", -hw, -hh - (46 if on["N"] else 24), 0, 0, 0.3, 0.3, {}),
               XText("@name", -hw, hh + (26 if on["S"] else 4), 0, 0, 0.2, 0.2, {})]
    where: dict[str, tuple[float, float]] = {}
    for s, names in on.items():
        for i, n in enumerate(names):
            k = (i - (len(names) - 1) / 2) * 20
            where[n] = {"W": (-hw - 20, k), "E": (hw + 20, k), "N": (k, -hh - 20), "S": (k, hh + 20)}[s]
    for n, t in ports:                                                      # boxes in port order
        x, y = where[n]
        f.boxes.append((5, x - 2.5, y - 2.5, x + 2.5, y + 2.5, {"name": n, "dir": PIN_DIR.get(t, "inout")}))
        f.lines.append(pin_lead(side_of[n], x, y, hw, hh, n))
        f.texts.append(pin_text(side_of[n], x, y, n))
    return f


def _grid(v: float) -> float:
    return math.ceil(v / 10) * 10


def pin_lead(side: str, x: float, y: float, hw: float, hh: float, name: str) -> tuple:
    """The line from the body's edge (a box of half width hw, half height hh around the origin) out to a pin."""
    edge = {"W": (-hw, y), "E": (hw, y), "N": (x, -hh), "S": (x, hh)}[side]
    return (4, *edge, x, y, {"undine_pin": name})


def pin_text(side: str, x: float, y: float, name: str) -> XText:
    """A pin's name inside the body next to where its lead meets the edge (20 from the pin): along a left or right
    edge, written downwards from the top edge and upwards from the bottom one."""
    if side in ("W", "E"):
        return XText(name, x + 25 if side == "W" else x - 25, y - 6, 0, 1 if side == "E" else 0, 0.2, 0.2, {"undine_pin": name})
    return XText(name, x + 6, y + 25, 1, 0, 0.2, 0.2, {"undine_pin": name}) if side == "N" else \
        XText(name, x - 6, y - 25, 3, 0, 0.2, 0.2, {"undine_pin": name})


def canonical(f: "XFile") -> "XFile":
    """Puts the drawing in a deterministic order, so the same drawing is always written the same way and a diff shows
    only what changed: wires, lines, boxes, polygons, arcs and texts sorted by geometry. Instances keep their order:
    the order of pins (ipin / opin) is the subcircuit's pin order and code blocks are written in order."""
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    f.wires.sort(key=lambda w: (wire_id(*w[:4]), str(sorted(w[4].items()))))
    f.lines.sort(key=lambda e: (str(e[0]), num(e[1]), num(e[2]), num(e[3]), num(e[4]), str(sorted(e[5].items()))))
    f.boxes.sort(key=lambda e: (str(e[0]), num(e[1]), num(e[2]), num(e[3]), num(e[4]), str(sorted(e[5].items()))))
    f.polys.sort(key=lambda e: (str(e[0]), [num(v) for v in e[1]], str(sorted(e[2].items()))))
    f.arcs.sort(key=lambda e: (str(e[0]), *[num(v) for v in e[1:6]], str(sorted(e[6].items()))))
    f.texts.sort(key=lambda tx: (num(tx.y), num(tx.x), tx.s))
    return f


def comp_ids(f: "XFile") -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for i, c in enumerate(f.comps):
        base = c.props.get("name", "") or f"@{i}"
        seen[base] = seen.get(base, 0) + 1
        out.append(base if seen[base] == 1 else f"{base}#{seen[base]}")
    return out


def wire_id(x1: float, y1: float, x2: float, y2: float) -> str:
    """A wire's identity: its end points, sorted (undine._core, Rust)."""
    return _core.wire_id(x1, y1, x2, y2)


def _py_wire_id(x1: float, y1: float, x2: float, y2: float) -> str:
    a, b = sorted([(x1, y1), (x2, y2)])
    return f"{a[0]:g},{a[1]:g},{b[0]:g},{b[1]:g}"


def normalize_names(f: "XFile") -> None:
    """Gives every instance a unique name (called when a file is edited, never when it is only read)."""
    used = {c.props.get("name") for c in f.comps if c.props.get("name")}
    seen: set[str] = set()
    for i, c in enumerate(f.comps):
        nm = c.props.get("name", "")
        if nm and nm not in seen:
            seen.add(nm)
            continue
        stem = (nm.rstrip("0123456789") if nm else "x") or "x"
        n = 1
        while f"{stem}{n}" in used or f"{stem}{n}" in seen:
            n += 1
        c.props["name"] = f"{stem}{n}"
        used.add(c.props["name"])
        seen.add(c.props["name"])


# --------------------------------------------------------------------------
# Writer


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def dump_props(props: dict) -> str:
    out = []
    for k, v in props.items():
        v = str(v)
        if v == "" or any(c in v for c in ' \t\r\n"=\\'):
            out.append(f'{k}="' + v.replace("\\", "\\\\").replace('"', '\\"') + '"')
        else:
            out.append(f"{k}={v}")
    return "\n".join(out) if any("\n" in str(v) for v in props.values()) else " ".join(out)


def _n(v: float) -> str:
    return f"{v:.10g}"


def dump(f: XFile, embedded: bool = False) -> str:
    """Serializes to Xschem's text format (undine._core, Rust; _py_dump is the reference)."""
    return _core.dump(f, embedded)


def _py_dump(f: XFile, embedded: bool = False) -> str:
    o: list[str] = []
    if not embedded:
        o.append("v {" + _esc(f.version or "xschem version=3.4.5 file_version=1.2\n") + "}")
    o.append("G {" + _esc(f.g_text) + "}")
    if f.k_raw or (f.global_props and f.g_text == ""):
        o.append("K {" + _esc(f.k_raw or dump_props(f.global_props)) + "}")
    for k in "VSEF":
        if k in f.code:
            o.append(k + " {" + _esc(f.code[k]) + "}")
    for layer, x1, y1, x2, y2, pr in f.lines:
        o.append(f"L {layer} {_n(x1)} {_n(y1)} {_n(x2)} {_n(y2)} {{{_esc(dump_props(pr))}}}")
    for layer, x1, y1, x2, y2, pr in f.boxes:
        o.append(f"B {layer} {_n(x1)} {_n(y1)} {_n(x2)} {_n(y2)} {{{_esc(dump_props(pr))}}}")
    for layer, pts, pr in f.polys:
        o.append(f"P {layer} {len(pts) // 2} " + " ".join(_n(v) for v in pts) + f" {{{_esc(dump_props(pr))}}}")
    for layer, cx, cy, r, a, b, pr in f.arcs:
        o.append(f"A {layer} {_n(cx)} {_n(cy)} {_n(r)} {_n(a)} {_n(b)} {{{_esc(dump_props(pr))}}}")
    for t in f.texts:
        o.append(f"T {{{_esc(t.s)}}} {_n(t.x)} {_n(t.y)} {t.rot} {t.flip} {_n(t.xs)} {_n(t.ys)} {{{_esc(dump_props(t.props))}}}")
    for x1, y1, x2, y2, pr in f.wires:
        o.append(f"N {_n(x1)} {_n(y1)} {_n(x2)} {_n(y2)} {{{_esc(dump_props(pr))}}}")
    for c in f.comps:
        line = f"C {{{_esc(c.sym)}}} {_n(c.x)} {_n(c.y)} {c.rot} {c.flip} {{{_esc(dump_props(c.props))}}}"
        if c.embedded is not None:
            line += " [\n" + _py_dump(c.embedded, True) + "\n]"
        o.append(line)
    return "\n".join(o) + "\n"
