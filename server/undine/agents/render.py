"""Pictures for agents: a schematic or a waveform as a PNG, drawn by KLayout's LayoutView (the renderer the layout tiles
already use, so no extra imaging library). A drawing is turned into a throwaway layout: one layer per colour and kind,
lines and wires as thin paths, texts in KLayout's scalable stroke font. Layouts are drawn by LayoutDB.render.

An agent gets the picture with the numbers it shows (see agent_tools: render_schematic, render_layout, plot_waveform),
so it can look and measure: the picture is context, the numbers are the evidence.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import klayout.db as kdb
import klayout.lay as klay

BACKGROUND = "#071217"            # = --bg in web/src/styles.css
TEXT = 0xb6cbd1                   # --text-soft
AXIS = 0x2a4856                   # --line2
GRID = 0x1a3540                   # --line
SERIES = [0x72bfc6, 0x8fd6b5, 0xe9929a, 0xe6c88f, 0xb7a9ee, 0x9bdcf2, 0xf0b28a, 0x9fe3d6]   # theme.ts SERIES
STROKE_FONT = "2"                 # KLayout's scalable sans stroke font (0 is a fixed pixel font)
MAX_PX = 2048


@dataclass
class Picture:
    """What a tool returns to show something: the PNG and the facts about it (sent as text next to the image)."""
    png: bytes
    info: dict = field(default_factory=dict)


def _hex(c: str | int) -> int:
    return c if isinstance(c, int) else int(c.lstrip("#")[:6] or "cccccc", 16)


class _Canvas:
    """A throwaway layout in world units (y up), drawn with one layer per (colour, style)."""

    def __init__(self, dbu: float = 0.001):
        self.ly = kdb.Layout()
        self.ly.dbu = dbu
        self.top = self.ly.create_cell("top")
        self.styles: dict[tuple[int, str], int] = {}       # (rgb, "solid" | "frame" | "text") -> layer index

    def layer(self, rgb: int, style: str) -> int:
        key = (rgb, style)
        if key not in self.styles:
            self.styles[key] = self.ly.layer(len(self.styles) + 1, 0)
        return self.styles[key]

    def path(self, rgb: int, pts: list[tuple[float, float]], width: float) -> None:
        if len(pts) >= 2:
            self.top.shapes(self.layer(rgb, "solid")).insert(kdb.DPath([kdb.DPoint(x, y) for x, y in pts], width, width / 2, width / 2, True))

    def box(self, rgb: int, x1: float, y1: float, x2: float, y2: float, solid: bool) -> None:
        self.top.shapes(self.layer(rgb, "solid" if solid else "frame")).insert(kdb.DBox(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))

    def polygon(self, rgb: int, pts: list[tuple[float, float]]) -> None:
        if len(pts) >= 3:
            self.top.shapes(self.layer(rgb, "solid")).insert(kdb.DPolygon([kdb.DPoint(x, y) for x, y in pts]))

    def text(self, rgb: int, s: str, x: float, y: float, size: float, rot90: bool = False,
             halign=kdb.HAlign.HAlignLeft, valign=kdb.VAlign.VAlignTop) -> None:
        if not s.strip():
            return                                                # an empty text would still draw its origin mark
        t = kdb.DText(s, kdb.DTrans(kdb.DTrans.R90 if rot90 else kdb.DTrans.R0, kdb.DVector(x, y)))
        t.size, t.halign, t.valign = size, halign, valign
        self.top.shapes(self.layer(rgb, "text")).insert(t)

    def png(self, box: kdb.DBox, w: int, h: int) -> bytes:
        lv = klay.LayoutView(True)
        for k, v in {"background-color": BACKGROUND, "grid-visible": "false", "text-visible": "true", "text-font": STROKE_FONT,
                     "apply-text-trans": "true", "bitmap-oversampling": "2", "drop-small-cells": "false"}.items():
            lv.set_config(k, v)
        cv = lv.show_layout(self.ly, False)
        lv.max_hier()
        lv.clear_layers()
        for (rgb, style), li in self.styles.items():
            p = klay.LayerPropertiesNode()
            info = self.ly.get_info(li)
            p.source = f"{info.layer}/{info.datatype}@{cv + 1}"
            p.fill_color = p.frame_color = rgb
            p.dither_pattern = 1 if style == "frame" else 0         # 0: solid, 1: hollow
            p.width = 1
            lv.insert_layer(lv.end_layers(), p)
        return lv.get_pixels_with_options(w, h, 1, 1, 0, box).to_png_data()


def _fit(bw: float, bh: float, size: int) -> tuple[int, int]:
    size = max(64, min(int(size), MAX_PX))
    if bw >= bh:
        return size, max(32, round(size * bh / bw))
    return max(32, round(size * bw / bh)), size


# ---------------------------------------------------------------------------------------------------------- schematic

def _text_box(t: dict, w: float, h: float) -> tuple[float, float, float, float]:
    """schdraw.ts textBox: the text's box in schematic coordinates (y down), from its anchor, rotation, flip, centring."""
    x1, y1, r, f = t["x"], t["y"], t["rot"], t["flip"]
    if t["hc"]:
        d = w / 2
        if r == 0:
            x1 += d if f else -d
        elif r == 1:
            y1 += d if f else -d
        elif r == 2:
            x1 += -d if f else d
        else:
            y1 += -d if f else d
    if t["vc"]:
        d = h / 2
        x1, y1 = {0: (x1, y1 - d), 1: (x1 + d, y1), 2: (x1, y1 + d)}.get(r, (x1 - d, y1))
    px, py = x1 + w, y1 + h
    xx = 2 * t["x"] - px if f else px
    if r == 0:
        rx, ry = xx, py
    elif r == 1:
        rx, ry = t["x"] - py + t["y"], t["y"] + xx - t["x"]
    elif r == 2:
        rx, ry = 2 * t["x"] - xx, 2 * t["y"] - py
    else:
        rx, ry = t["x"] + py - t["y"], t["y"] - xx + t["x"]
    return min(x1, rx), min(y1, ry), max(x1, rx), max(y1, ry)


def _junctions(wires: list) -> list[tuple[float, float]]:
    """Points where three or more wire rays meet (schConnectivity.ts): the dots of a schematic."""
    ends: dict[tuple[float, float], int] = {}
    for x1, y1, x2, y2, *_ in wires:
        for p in ((x1, y1), (x2, y2)):
            ends[p] = ends.get(p, 0) + 1
    out = []
    for (x, y), n in ends.items():
        rays = n
        for x1, y1, x2, y2, *_ in wires:
            if (x, y) in ((x1, y1), (x2, y2)):
                continue
            if min(x1, x2) <= x <= max(x1, x2) and min(y1, y2) <= y <= max(y1, y2) and (x2 - x1) * (y - y1) == (y2 - y1) * (x - x1):
                rays += 2
        if rays >= 3:
            out.append((x, y))
    return out


def schematic_extent(d: dict) -> list[float]:
    """What schematic_png draws of a whole schematic: its symbols, wires and texts with a margin, [x1, y1, x2, y2] in
    schematic units (y down)."""
    x1, y1, x2, y2 = d["bbox"]
    for t in d.get("texts", []):                                # long texts (annotations) reach past the symbols
        lines = t["s"].split("\n")
        tb = _text_box(t, max(len(s) for s in lines) * t["size"] * 0.6, len(lines) * t["size"] * 1.16)
        x1, y1, x2, y2 = min(x1, tb[0]), min(y1, tb[1]), max(x2, tb[2]), max(y2, tb[3])
    pad = max(x2 - x1, y2 - y1) * 0.04 + 10
    return [x1 - pad, y1 - pad, x2 + pad, y2 + pad]


def schematic_png(d: dict, size: int = 1024, box: list[float] | None = None) -> tuple[bytes, int, int]:
    """A schematic (SchEditor.view(): flattened symbol graphics, wires, texts) as drawn in the editor: the whole of it,
    or `box` ([x1, y1, x2, y2] in schematic units, y down) at the same size, to look closer."""
    colors = [_hex(c) for c in d.get("colors") or []]
    col = lambda i: colors[i] if 0 <= i < len(colors) else 0xcccccc   # noqa: E731
    x1, y1, x2, y2 = box or schematic_extent(d)
    bw, bh = x2 - x1, y2 - y1
    w, h = _fit(bw, bh, size)
    px = max(bw / w, bh / h)                                    # one pixel, in schematic units
    c = _Canvas()
    Y = lambda y: -y                                             # noqa: E731  schematic y is down, the layout's is up
    for l, ax, ay, bx, by in d.get("lines", []):
        c.path(col(l), [(ax, Y(ay)), (bx, Y(by))], 1.25 * px)
    for l, ax, ay, bx, by, fill in d.get("boxes", []):
        if fill:
            c.box(col(l), ax, Y(ay), bx, Y(by), True)
        else:
            c.path(col(l), [(ax, Y(ay)), (bx, Y(ay)), (bx, Y(by)), (ax, Y(by)), (ax, Y(ay))], 1.25 * px)
    for l, fill, pts in d.get("polys", []):
        p = [(pts[i], Y(pts[i + 1])) for i in range(0, len(pts) - 1, 2)]
        if fill:
            c.polygon(col(l), p)
        else:
            c.path(col(l), p, 1.25 * px)
    for l, cx, cy, r, a, b, fill in d.get("arcs", []):
        n = max(8, int(abs(b) / 10))
        p = [(cx + r * math.cos(math.radians(a + b * i / n)), Y(cy) + r * math.sin(math.radians(a + b * i / n))) for i in range(n + 1)]
        if fill and b >= 360:
            c.polygon(col(l), p)
        else:
            c.path(col(l), p, 1.25 * px)
    for x1_, y1_, x2_, y2_, bus, *_ in d.get("wires", []):
        c.path(col(1), [(x1_, Y(y1_)), (x2_, Y(y2_))], (4 if bus else 1.6) * px)
    for x, y in _junctions(d.get("wires", [])):
        r = max(3, 3 * px)
        c.polygon(col(1), [(x + r * math.cos(k * math.pi / 8), Y(y) + r * math.sin(k * math.pi / 8)) for k in range(16)])
    for t in d.get("texts", []):
        fs = t["size"]
        if fs / px < 3.5:                                         # too small to read at this size, as in the editor
            continue
        lines = t["s"].split("\n")
        tw, th = max(len(s) for s in lines) * fs * 0.6, len(lines) * fs * 1.16
        bx1, by1, _, by2 = _text_box(t, tw, th)
        for i, s in enumerate(lines):
            if t["rot"] & 1:
                c.text(col(t["layer"]), s, bx1 + i * fs * 1.16, Y(by2), fs * 0.82, rot90=True)
            else:
                c.text(col(t["layer"]), s, bx1, Y(by1 + i * fs * 1.16), fs * 0.82)
    return c.png(kdb.DBox(x1, -y2, x2, -y1), w, h), w, h


# ---------------------------------------------------------------------------------------------------------- waveforms

def _ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    if hi <= lo:
        return [lo]
    step = 10 ** math.floor(math.log10((hi - lo) / n))
    for m in (1, 2, 5, 10):
        if (hi - lo) / (step * m) <= n:
            step *= m
            break
    first = math.ceil(lo / step) * step
    return [first + i * step for i in range(int((hi - first) / step + 1e-9) + 1)]


def _si(v: float) -> str:
    if v == 0:
        return "0"
    for exp, p in ((12, "T"), (9, "G"), (6, "M"), (3, "k"), (0, ""), (-3, "m"), (-6, "u"), (-9, "n"), (-12, "p"), (-15, "f")):
        if abs(v) >= 10 ** exp * 0.9999:
            return f"{v / 10 ** exp:.3g}{p}"
    return f"{v:.3g}"


UNITS = {"time": "s", "frequency": "Hz", "voltage": "V", "current": "A"}   # ngspice vector types


def waveform_png(wf: dict, size: tuple[int, int] = (1024, 600)) -> tuple[bytes, list[dict]]:
    """Traces of a simulation (sim.waveform_json) as a plot: one panel, or magnitude (dB) and phase panels for an AC
    sweep with a log frequency axis. Returns the PNG and, per trace, what the plot shows (colour and range)."""
    w, h = max(320, min(size[0], MAX_PX)), max(200, min(size[1], MAX_PX))
    xs = [float(v) for v in wf["x"]["values"]]
    ac = bool(wf.get("complex"))
    logx = ac and all(v > 0 for v in xs)
    X = (lambda v: math.log10(v)) if logx else (lambda v: v)    # noqa: E731
    traces = [(n, t) for n, t in wf["traces"].items()]
    types = {v["name"]: v.get("type") for v in wf.get("vectors", [])}
    yunits = {UNITS.get(types.get(n) or "", "") for n, _ in traces}
    panels = [("mag_db", "dB"), ("phase_deg", "deg")] if ac else [("y", yunits.pop() if len(yunits) == 1 else "")]
    W, H = 1000.0, 1000.0 * h / w                                # world units: the whole picture
    left, right, top, bottom, gap = 110.0, 30.0, 52.0, 70.0, 40.0
    ph = (H - top - bottom - gap * (len(panels) - 1)) / len(panels)
    c = _Canvas(dbu=0.001)
    fs = 15.0
    xlo, xhi = X(min(xs)), X(max(xs))
    if xhi <= xlo:
        xhi = xlo + 1
    legend = []
    for pi, (key, unit) in enumerate(panels):
        y0 = H - top - (pi + 1) * ph - pi * gap
        ys = [v for _, t in traces for v in (t.get(key) or []) if v is not None and math.isfinite(v)]
        lo, hi = (min(ys), max(ys)) if ys else (0.0, 1.0)
        if hi - lo < 1e-15:
            lo, hi = lo - 1, hi + 1
        lo, hi = lo - (hi - lo) * 0.05, hi + (hi - lo) * 0.05
        px_ = lambda v: left + (X(v) - xlo) / (xhi - xlo) * (W - left - right)   # noqa: E731
        py_ = lambda v: y0 + (v - lo) / (hi - lo) * ph                           # noqa: E731
        for v in _ticks(lo, hi):
            c.path(GRID, [(left, py_(v)), (W - right, py_(v))], 1.0)
            c.text(TEXT, _si(v), left - 8, py_(v), fs, halign=kdb.HAlign.HAlignRight, valign=kdb.VAlign.VAlignCenter)
        xt = [10 ** e for e in range(math.ceil(xlo), math.floor(xhi) + 1)] if logx else _ticks(min(xs), max(xs))
        for v in xt:
            c.path(GRID, [(px_(v), y0), (px_(v), y0 + ph)], 1.0)
            if pi == len(panels) - 1:
                c.text(TEXT, _si(v), px_(v), y0 - 8, fs, halign=kdb.HAlign.HAlignCenter)
        c.path(AXIS, [(left, y0), (W - right, y0), (W - right, y0 + ph), (left, y0 + ph), (left, y0)], 1.6)
        c.text(TEXT, unit, 12, y0 + ph, fs, valign=kdb.VAlign.VAlignTop)
        for i, (name, t) in enumerate(traces):
            vals = t.get(key) or []
            pts = [(px_(x), py_(v)) for x, v in zip(xs, vals) if v is not None and math.isfinite(v)]
            col = SERIES[i % len(SERIES)]
            c.path(col, pts, 2.2)
            if pi == 0:
                fin = [v for v in vals if v is not None and math.isfinite(v)]
                legend.append({"signal": name, "color": f"#{col:06x}", **({"min": min(fin), "max": max(fin)} if fin else {})})
    xunit = UNITS.get(wf["x"].get("unit") or "", wf["x"].get("unit") or "")
    xname = wf["x"].get("name", "x") + (f" ({xunit})" if xunit and xunit != wf["x"].get("name") else "")
    c.text(TEXT, xname + ("  (log)" if logx else ""), W - right, 8, fs, halign=kdb.HAlign.HAlignRight, valign=kdb.VAlign.VAlignBottom)
    x = left                                                      # the legend, in a row above the plot
    for lg in legend:
        yy = H - 14
        c.path(_hex(lg["color"]), [(x, yy - fs / 2), (x + 26, yy - fs / 2)], 3.0)
        c.text(TEXT, lg["signal"], x + 32, yy, fs)
        x += 32 + len(lg["signal"]) * fs * 0.62 + 28
    return c.png(kdb.DBox(0, 0, W, H), w, h), legend
