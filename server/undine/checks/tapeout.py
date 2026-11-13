"""Shuttle targets: checks that mirror what a shuttle asks for, and a submission bundle.

A target says which process it needs and what the shuttle expects of the layout. The local checks here are fast
Python checks on the layout; the heavy sign-off checks (KLayout decks, Magic DRC, Magic + Netgen LVS) are the jobs of
the Verify panel and are started together by `suite`. Where a requirement cannot be verified from public information
(pin positions defined by a template, current shuttle rules) the check says `manual`, never `pass`.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import klayout.db as kdb

from undine import tools
from undine.pdks.pdk import Pdk


@dataclass
class Check:
    id: str
    title: str
    status: str          # pass | fail | warn | manual
    detail: str = ""

    def public(self) -> dict:
        return {"id": self.id, "title": self.title, "status": self.status, "detail": self.detail}


TARGETS = {
    "tt-sky130-analog": {
        "label": "Tiny Tapeout - SKY130 analog tile", "family": "sky130", "max_metal": "met4",
        "note": "Mirrors the layout checks of Tiny Tapeout's precheck (tt-support-tools, Apache-2.0) with the official DEF pin table: "
                "project area and prBoundary, template pins, power stripes, ua[] pins vs info.yaml, no Metal 5, nwell/urpm. "
                "DRC and LVS come from the sign-off suite. Verilog syntax (Yosys) is left to the shuttle's own check.",
        "params": {"top_module": "tt_um_", "tiles": "1x2", "analog_pins": "", "ua": "", "title": "", "author": "", "description": ""},
        "lvs_options": {"lvs_sub": "VGND", "run_mode": "flat"},     # the tile's substrate is VGND; ports are matched flat
    },
    "wafer-space-gf180": {
        "label": "wafer.space - GF180MCU die", "family": "gf180mcu", "max_metal": None,
        "note": "Mirrors the layout checks of wafer.space's gf180mcu-precheck (Apache-2.0): one top cell at the origin, dbu, "
                "the 5-metal stack, the seal ring, the slot size, global density with the dummy fill, and for Chip-on-Board "
                "the ID cells. The main KLayout DRC, density, antenna and Magic DRC come from the sign-off suite. The CoB pad "
                "openings are compared with the golden mask by the precheck on the platform.",
        "params": {"slot": "1x1", "cob": "no"},
        # the precheck runs the PDK deck three times: the main checks (the profile default), density and antenna
        "drc_runs": {"density": {"decks": "density"}, "antenna": {"decks": "antenna"}},
    },
    "chipfoundry-caravel": {
        "label": "ChipFoundry - Caravel user project (SKY130)", "family": "sky130", "max_metal": "met5",
        "note": "Mirrors the layout checks of ChipFoundry's cf-precheck (Apache-2.0): the wrapper cell name, no '#' or '/' in "
                "cell names, the prBoundary the clear-area density deck reads, and the XOR of the wrapper's edge (pins and "
                "power rings outside the 2920 x 3520 um user area) against Caravel's empty wrapper GDS, when its path on "
                "the server is given (golden_gds; github.com/chipfoundry/caravel gds/). KLayout FEOL / BEOL / offgrid, "
                "density, pin-label and zero-area decks and Magic DRC / LVS come from the sign-off suite; GPIO defines and "
                "the CVC checks stay with cf-precheck.",
        "params": {"wrapper": "digital", "golden_gds": ""},
    },
    "generic-layout": {
        "label": "Layout hygiene only (any shuttle)", "family": None, "max_metal": None,
        "note": "Top cell, database unit, zero-area shapes, origin, highest metal. A shuttle's own precheck is still the judge.",
        "params": {"max_metal": ""},
    },
}


def targets() -> list[dict]:
    return [{"id": k, **{x: v[x] for x in ("label", "family", "note", "params")}} for k, v in TARGETS.items()]


# ----------------------------------------------------------------------------------- helpers


def _layer(layout: kdb.Layout, key: tuple[int, int]):
    return layout.find_layer(*key)


def _region(layout: kdb.Layout, cell: kdb.Cell, key: tuple[int, int]) -> kdb.Region:
    li = _layer(layout, key)
    return kdb.Region(cell.begin_shapes_rec(li)) if li is not None else kdb.Region()


def _texts(layout: kdb.Layout, cell: kdb.Cell, keys: list[tuple[int, int]]) -> list[tuple[str, kdb.Point]]:
    out = []
    for key in keys:
        li = _layer(layout, key)
        if li is None:
            continue
        it = cell.begin_shapes_rec(li)
        while not it.at_end():
            s = it.shape()
            if s.is_text():
                t = s.text.transformed(it.trans())
                out.append((t.string, kdb.Point(t.x, t.y)))
            it.next()
    return out


def _um(v: float, dbu: float) -> float:
    return round(v * dbu, 3)


# ----------------------------------------------------------------------------------- generic checks


def generic_checks(layout: kdb.Layout, cell: kdb.Cell, pdk: Pdk | None, max_metal: str | None) -> list[Check]:
    dbu = layout.dbu
    out: list[Check] = []
    prof = pdk.profile if pdk else None
    out.append(Check("dbu", "Database unit is 0.001 um", "pass" if abs(dbu - 0.001) < 1e-9 else "fail", f"dbu = {dbu} um"))
    bad = [c.name for c in layout.each_cell() if not re.fullmatch(r"[A-Za-z0-9_$.+-]+", c.name)]
    long = [c.name for c in layout.each_cell() if len(c.name) > 32]
    out.append(Check("cellnames", "Cell names use plain characters", "fail" if bad else ("warn" if long else "pass"),
                     f"illegal: {', '.join(bad[:5])}" if bad else (f"{len(long)} names longer than 32 characters (fine for GDSII-2, old tools truncate)" if long else "")))
    bb = cell.bbox()
    out.append(Check("origin", "Layout starts at the origin (lower-left >= 0,0)", "pass" if not bb.empty() and bb.left >= 0 and bb.bottom >= 0 else "warn",
                     f"bbox {_um(bb.left, dbu)},{_um(bb.bottom, dbu)} to {_um(bb.right, dbu)},{_um(bb.top, dbu)} um (wafer.space wants origin 0,0)"))
    zero = 0
    for c in layout.each_cell():
        for li in layout.layer_indexes():
            for s in c.shapes(li).each():
                if (s.is_box() or s.is_polygon() or s.is_path()) and s.area() == 0 and not s.is_text():
                    zero += 1
    out.append(Check("zero_area", "No zero-area shapes", "fail" if zero else "pass", f"{zero} found" if zero else ""))
    if prof and prof.layers and max_metal:
        order = list(prof.metals)
        if max_metal in order:
            above = order[order.index(max_metal) + 1:]
            found = []
            for m in above:
                for key in (prof.layers.get(m),):
                    li = _layer(layout, key) if key else None
                    if li is not None and not cell.bbox_per_layer(li).empty():
                        found.append(m)
            out.append(Check("max_metal", f"No metal above {max_metal}", "fail" if found else "pass",
                             f"shapes on {', '.join(found)}" if found else ""))
    return out


# ----------------------------------------------------------------------------------- Tiny Tapeout (sky130 analog)
# Mirrors TinyTapeout/tt-support-tools `precheck/precheck.py` (Apache-2.0): the checks that need only the layout, the
# template DEF pin table (data/tt_sky130A_analog.json, derived from the DEF files) and info.yaml values.

_TT_DATA: dict | None = None
MET4, MET4_LABEL, MET4_PIN, VIA3, NWELL, URPM, PRB = (71, 20), (71, 5), (71, 16), (70, 44), (64, 20), (79, 20), (235, 4)
MET5_LAYERS = [(72, 20), (72, 16), (72, 5)]


def tt_template(tiles: str) -> dict:
    global _TT_DATA
    if _TT_DATA is None:
        _TT_DATA = json.loads((Path(__file__).resolve().parents[1] / "data" / "tt_sky130A_analog.json").read_text())
    if tiles not in _TT_DATA["tiles"]:
        raise ValueError(f"no template for tiles={tiles} (have {', '.join(_TT_DATA['tiles'])})")
    return _TT_DATA["tiles"][tiles]


def tt_analog_pin_rects(uses_vapwr: bool = False) -> list[tuple[float, float, float, float]]:
    """Rectangles (um) next to which ua[0..7] must connect (tech_data.analog_pin_rects, sky130A)."""
    out = []
    for n in range(8):
        x1 = (136.17 if uses_vapwr else 151.81) - 19.32 * n
        out.append((x1, 0.0, x1 + 0.9, 1.0))
    return out


def tt_pins(layout: kdb.Layout, cell: kdb.Cell, prof) -> dict[str, list[list[int]]]:
    """Label name -> boxes (dbu) of the drawn met4 shapes under the label (a stripe, not the wires that join it)."""
    texts = _texts(layout, cell, [MET4_LABEL, (71, 16), MET4])
    li = _layer(layout, MET4)
    shapes = []
    if li is not None:
        it = cell.begin_shapes_rec(li)
        while not it.at_end():
            s = it.shape()
            if s.is_box() or s.is_polygon() or s.is_path():
                shapes.append(s.polygon.transformed(it.trans()))
            it.next()
    pins: dict[str, list[list[int]]] = {}
    for name, p in texts:
        for poly in shapes:
            if poly.inside(p):
                b = poly.bbox()
                box = [b.left, b.bottom, b.right, b.top]
                if box not in pins.setdefault(name, []):
                    pins[name].append(box)
    return pins


def _rect_region(r) -> kdb.Region:
    return kdb.Region(kdb.Box(*[int(round(v)) for v in r]))


def tt_detect_analog(layout: kdb.Layout, cell: kdb.Cell) -> int:
    """Highest ua index wired to metal (ring around the pad on met4, or via3 on it) + 1."""
    dbu = layout.dbu
    met4, via3 = _region(layout, cell, MET4), _region(layout, cell, VIA3)
    n = 0
    for i, r in enumerate(tt_analog_pin_rects()):
        rect = [v / dbu for v in r]
        ring = _rect_region([rect[0] - 500, rect[1] - 500, rect[2] + 500, rect[3] + 500]) - _rect_region([rect[0] - 100, rect[1] - 100, rect[2] + 100, rect[3] + 100])
        if not (met4 & ring).is_empty() or not (via3 & _rect_region(rect)).is_empty():
            n = i + 1
    return n


def _analog_pins(layout: kdb.Layout, cell: kdb.Cell, params: dict) -> int:
    v = str(params.get("analog_pins", "")).strip()
    if not v:
        return tt_detect_analog(layout, cell)
    if not v.isdigit() or not 0 <= int(v) <= 6:
        raise ValueError(f"'Analog pins used' must be a number from 0 to 6 (or blank), got {v!r}. "
                         "Put the per-pin descriptions in the 'ua[] descriptions' field instead.")
    return int(v)


def tt_checks(layout: kdb.Layout, cell: kdb.Cell, pdk: Pdk, params: dict) -> list[Check]:
    dbu = layout.dbu
    out: list[Check] = []
    top = (params.get("top_module") or "").strip()
    tiles = params.get("tiles", "1x2")
    tpl = tt_template(tiles)
    dw, dh = tpl["die"]                                   # nm
    analog_pins = _analog_pins(layout, cell, params)           # blank = as many as are wired
    ua_desc = [d.strip() for d in str(params.get("ua", "")).split(",")] if params.get("ua") else []

    out.append(Check("tt_top", "Top module is named tt_um_<name> and is the one top cell",
                     "pass" if re.fullmatch(r"tt_um_[A-Za-z0-9_]+", top) and len(list(layout.top_cells())) == 1 else "fail",
                     f"top_module = {top!r}; the exported top cell, GDS, LEF and info.yaml use this name"))
    # --- forbidden layers (met5)
    f5 = [f"{l}/{d}" for l, d in MET5_LAYERS if (li := _layer(layout, (l, d))) is not None and not cell.bbox_per_layer(li).empty()]
    out.append(Check("tt_no_met5", "No Metal 5 (the power grid uses it)", "fail" if f5 else "pass", f"shapes on {', '.join(f5)}" if f5 else ""))
    # --- prBoundary and project area
    bb = cell.bbox()
    li = _layer(layout, PRB)
    boxes = []
    if li is not None:
        boxes = [p.bbox() for p in kdb.Region(cell.begin_shapes_rec(li)).each()]
    die_box = kdb.Box(0, 0, int(round(dw / 1000 / dbu)), int(round(dh / 1000 / dbu)))
    if bb.empty():
        st, det = "fail", "the cell is empty"
    elif bb.left < 0 or bb.bottom < 0 or bb.right > die_box.right or bb.top > die_box.top:
        st, det = "fail", f"shapes outside the project area: bbox {_um(bb.left, dbu)},{_um(bb.bottom, dbu)} to {_um(bb.right, dbu)},{_um(bb.top, dbu)} um, area is 0,0 to {dw / 1000:g},{dh / 1000:g} um"
    elif bb != die_box:
        st, det = "fail", f"boundary layer does not cover the project area (bbox is {_um(bb.width(), dbu)} x {_um(bb.height(), dbu)} um, expected {dw / 1000:g} x {dh / 1000:g})"
    elif die_box not in boxes:
        st, det = "fail", "missing top-level prBoundary rectangle (layer 235/4) with exactly the project size"
    else:
        st, det = "pass", f"{dw / 1000:g} x {dh / 1000:g} um, prBoundary 235/4 present"
    out.append(Check("tt_boundary", f"Layout is exactly the {tiles} project area with a prBoundary", st, det))
    # --- cell names
    badn = [c.name for c in layout.each_cell() if "#" in c.name or "/" in c.name]
    out.append(Check("tt_cellnames", "No '#' or '/' in cell names", "fail" if badn else "pass", ", ".join(badn[:4])))
    # --- layers: every used layer must exist in the PDK's layer list
    unknown = _unknown_layers(layout, pdk)
    out.append(Check("tt_layers", "Only PDK layers are used", "warn" if unknown else "pass",
                     (f"not in the PDK layer list: {', '.join(unknown[:6])} (the shuttle's own list may differ)" if unknown else "")))
    # --- template pins exist on met4 exactly, and on the pin purpose (71/16) the precheck reads them from
    met4 = _region(layout, cell, MET4)
    met4.merge()
    pinreg = _region(layout, cell, MET4_PIN)
    pinreg.merge()
    inner = lambda r: _rect_region([r[0] + 1, r[1] + 1, r[2] - 1, r[3] - 1])        # the precheck tests the corners 1 nm inside
    missing = [n for n, p in tpl["pins"].items() if (_rect_region([v / 1000 / dbu for v in p["rect"]]) - met4).count() > 0]
    nopurpose = [n for n, p in tpl["pins"].items() if (inner([v / 1000 / dbu for v in p["rect"]]) - pinreg).count() > 0]
    out.append(Check("tt_pins", f"All {len(tpl['pins'])} template pins are drawn on met4 (71/20) and met4.pin (71/16) at their fixed positions",
                     "fail" if missing or nopurpose else "pass",
                     "; ".join(x for x in (f"missing or incomplete on met4: {', '.join(missing[:6])}" if missing else "",
                                           f"not on met4.pin (71/16): {', '.join(nopurpose[:6])}" if nopurpose else "") if x)))
    # --- power stripes
    pins = tt_pins(layout, cell, pdk.profile)
    probs = []
    for n in ("VGND", "VDPWR"):
        if n not in pins:
            probs.append(f"{n} is not labelled on a met4 shape")
            continue
        for b in pins[n]:
            if (inner(b) - pinreg).count() > 0:
                probs.append(f"{n} stripe is not on met4.pin (71/16)")
            w = _um(b[2] - b[0], dbu)
            if w < 1.2:
                probs.append(f"{n} stripe is {w} um wide (>= 1.2 um)")
            if b[1] * dbu > 10:
                probs.append(f"{n} is {_um(b[1], dbu)} um from the bottom edge (> 10 um)")
            if dh / 1000 - b[3] * dbu > 10:
                probs.append(f"{n} is {round(dh / 1000 - b[3] * dbu, 3)} um from the top edge (> 10 um)")
    out.append(Check("tt_power", "VGND and VDPWR: met4, >= 1.2 um wide, within 10 um of the bottom and top edge", "fail" if probs else "pass", "; ".join(probs[:4])))
    # --- pins must not overlap each other
    allp: dict[str, kdb.Region] = {n: _rect_region([v / 1000 / dbu for v in p["rect"]]) for n, p in tpl["pins"].items()}
    for n in ("VGND", "VDPWR"):
        if n in pins:
            allp[n] = kdb.Region([kdb.Box(*b) for b in pins[n]])
    names = sorted(allp)
    clash = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if not (allp[a].sized(1) & allp[b].sized(1)).is_empty():
                clash.append(f"{a}/{b}")
    out.append(Check("tt_overlap", "Pins do not overlap or abut", "fail" if clash else "pass", ", ".join(clash[:4])))
    # --- analog pins: connected to metal if and only if used (and described in info.yaml)
    via3 = _region(layout, cell, VIA3)
    bad = []
    for i, r in enumerate(tt_analog_pin_rects()):
        rect = [v / dbu for v in r]
        pad = _rect_region(rect)
        ring = _rect_region([rect[0] - 500, rect[1] - 500, rect[2] + 500, rect[3] + 500]) - _rect_region([rect[0] - 100, rect[1] - 100, rect[2] + 100, rect[3] + 100])
        connected = not (met4 & ring).is_empty() or not (via3 & pad).is_empty()
        expected = i < analog_pins
        desc = ua_desc[i] if i < len(ua_desc) else ""
        if connected and not expected:
            bad.append(f"ua[{i}] is connected to metal but analog_pins is {analog_pins}")
        elif not connected and expected:
            bad.append(f"ua[{i}] is not connected to any adjacent metal but analog_pins is {analog_pins}")
        elif expected and not desc:
            bad.append(f"ua[{i}] has no description (pinout in info.yaml)")
    out.append(Check("tt_analog", "ua[n] connect to metal exactly when used (analog_pins) and are described", "fail" if bad else "pass", "; ".join(bad[:4])))
    # --- nwell / urpm spacing
    nw, ur = _region(layout, cell, NWELL), _region(layout, cell, URPM)
    viol = nw.separation_check(ur, int(round(0.7 / dbu)), True).count() if not ur.is_empty() else 0
    out.append(Check("tt_urpm", "nwell to urpm spacing >= 0.7 um", "fail" if viol else "pass", f"{viol} violations" if viol else ""))
    out.append(Check("tt_verilog", "Verilog syntax (src/project.v) is checked with Yosys by the shuttle", "manual",
                     "yosys is not part of Undine's tools; the bundle contains a stub that only declares the ports"))
    return out


def _unknown_layers(layout: kdb.Layout, pdk: Pdk) -> list[str]:
    try:
        import klayout.lay as klay

        lp = klay.LayerProperties()
        lp.load(str(pdk.lyp))
        known = {(n.source_layer, n.source_datatype) for n in lp.each_layer() if n.source_layer >= 0}
    except Exception:
        return []
    used = {(layout.get_info(li).layer, layout.get_info(li).datatype) for li in layout.layer_indexes()
            if any(not c.bbox_per_layer(li).empty() for c in layout.each_cell())}
    return [f"{l}/{d}" for l, d in sorted(used - known)]


# ----------------------------------------------------------------------------------- wafer.space (GF180MCU)
# Mirrors wafer-space/gf180mcu-precheck (Apache-2.0): scripts/klayout/check_top.py, check_size.py, read_layout.py,
# generate_id.py and the PDK's density deck (rule_decks/density.rb).

WS_PDK = ("gf180mcuD", "792ef67037d74c8c51811eab3ce23cf793491296")    # the PDK and open_pdks commit the precheck pins
WS_DIE = (3932.0, 5122.0)                     # 3880 x 5070 um user area plus a 26 um seal ring on each side
WS_SAW = 60.0                                 # saw street between the parts of a split slot
WS_SLOTS = {"1x1": (1, 1), "0p5x1": (2, 1), "1x0p5": (1, 2), "0p5x0p5": (2, 2)}
WS_FILL = 4                                   # dummy fill datatype, counted as drawing (read_layout.py)
# global density over the die (layer, min %, max %)
WS_DENSITY = [("COMP", 22, 25, 70), ("Poly2", 30, 14, None), ("Metal1", 34, 30, None), ("Metal2", 36, 30, None),
              ("Metal3", 42, 30, None), ("Metal4", 46, 30, None), ("Metal5", 81, 30, None)]


def ws_slot_size(slot: str) -> tuple[float, float]:
    dx, dy = WS_SLOTS[slot]
    return (WS_DIE[0] - (dx - 1) * WS_SAW) / dx, (WS_DIE[1] - (dy - 1) * WS_SAW) / dy


def ws_id_boxes(w: float, h: float) -> dict[str, tuple[float, float, float, float]]:
    """Where the Chip-on-Board ID cells must sit (generate_id.py), in um."""
    return {"gf180mcu_ws_ip__qrcode_id": (26, 26, 168.8, 168.8), "gf180mcu_ws_ip__shuttle_id": (26, 175.6, 85.5, 371.1),
            "gf180mcu_ws_ip__project_id": (175.6, 26, 371.1, 85.5),
            "gf180mcu_ws_ip__marker": (w - 36 - 245, h - 36 - 245, w - 36, h - 36)}


def ws_checks(layout: kdb.Layout, cell: kdb.Cell, pdk: Pdk, params: dict) -> list[Check]:
    out: list[Check] = []
    dbu = layout.dbu
    name, commit = WS_PDK
    if pdk.name != name:
        out.append(Check("ws_pdk", f"PDK is {name}", "fail", f"this project uses {pdk.name}; wafer.space runs the precheck on {name} only"))
    else:
        out.append(Check("ws_pdk", f"PDK is {name}", "pass" if pdk.version.startswith(commit[:12]) else "warn",
                         "" if pdk.version.startswith(commit[:12]) else
                         f"open_pdks {pdk.version[:12] or 'unknown'}; the precheck pins {commit[:12]} (run `set-pdk` to move)"))
    bb = cell.dbbox()
    out.append(Check("ws_origin", "Die starts at the origin (0,0)", "pass" if not bb.empty() and bb.left == 0 and bb.bottom == 0 else "fail",
                     f"lower-left is {bb.left:g},{bb.bottom:g} um" if not bb.empty() else "the cell is empty"))
    high = [n for n, key in (("Via5", (82, 0)), ("MetalTop", (53, 0))) if not _region(layout, cell, key).is_empty()]
    out.append(Check("ws_5lm", "5-metal stack: nothing on Via5 or MetalTop", "fail" if high else "pass",
                     f"shapes on {', '.join(high)}" if high else ""))
    ring = not _region(layout, cell, (167, 5)).is_empty()
    out.append(Check("ws_seal_ring", "Seal ring (GUARD_RING_MK 167/5) around the die", "pass" if ring else "fail",
                     "" if ring else "add the seal ring: the project template's flow (KLayout.SealRing) or `make librelane-padring` makes it"))
    slot = str(params.get("slot") or "1x1")
    if slot not in WS_SLOTS:
        out.append(Check("ws_slot", "Die size matches the slot", "fail", f"unknown slot {slot}; one of {', '.join(WS_SLOTS)}"))
    else:
        w, h = ws_slot_size(slot)
        ok = not bb.empty() and abs(bb.width() - w) < 1e-6 and abs(bb.height() - h) < 1e-6
        out.append(Check("ws_slot", f"Die is exactly {w:g} x {h:g} um (slot {slot})", "pass" if ok else "fail",
                         "" if ok else f"the die is {bb.width():g} x {bb.height():g} um"))
    area = bb.width() * bb.height() / dbu / dbu if not bb.empty() else 0
    low = []
    for label, layer, lo, hi in WS_DENSITY:
        r = (_region(layout, cell, (layer, 0)) + _region(layout, cell, (layer, WS_FILL))).merged()
        pct = 100 * r.area() / area if area else 0
        if pct < lo or (hi is not None and pct > hi):
            low.append(f"{label} {pct:.1f}% (needs {'>= ' + str(lo) if hi is None else f'{lo}-{hi}'}%)")
    out.append(Check("ws_density", "Global density with the dummy fill (COMP 25-70%, Poly2 >= 14%, Metal1-5 >= 30%)",
                     "fail" if low else "pass", "; ".join(low) + ("; add dummy fill (datatype 4)" if low else "")))
    cob = str(params.get("cob", "no")).lower() in ("yes", "true", "1", "on")
    if cob:
        bad = []
        for cname, box in ws_id_boxes(bb.width(), bb.height()).items():
            c = layout.cell(cname)
            insts = [i.child_inst() for i in c.each_parent_inst()] if c else []
            if len(insts) != 1:
                bad.append(f"{cname}: {len(insts)} instances" if c else f"{cname} missing")
            elif insts[0].dbbox() != kdb.DBox(*box):
                b = insts[0].dbbox()
                bad.append(f"{cname} at {b.left:g},{b.bottom:g};{b.right:g},{b.top:g} (expected {','.join(f'{v:g}' for v in box)})")
        out.append(Check("ws_id_cells", "Chip-on-Board ID cells placed once where the template has them", "fail" if bad else "pass",
                         "; ".join(bad) + ("; take them from the project template (chip_top.sv: do not remove)" if bad else "")))
        out.append(Check("ws_pad_mask", "Pad openings cover the CoB pad mask", "manual",
                         "the platform's precheck compares Pad (37/0) with the golden mask of the slot; keep the template's pad ring"))
    return out


# ----------------------------------------------------------------------------------- ChipFoundry (Caravel, SKY130)
# Mirrors chipfoundry/cf-precheck (Apache-2.0): checks/topcell.py, illegal_cellname.py, xor.py with
# drc_scripts/erase_box.tcl + xor.rb.drc, and met_min_ca_density.lydrc (which measures inside the prBoundary).

CF_WRAPPERS = {"digital": "user_project_wrapper", "analog": "user_analog_project_wrapper"}
CF_AREA = (0.0, 0.0, 2920.0, 3520.0)                         # the user area (um)
CF_EDGE = (-43.63, -38.27, 2963.25, 3557.95)                 # the outer edge of the power rings: what the XOR compares
# erase_box.tcl takes out met5 on the left / right and met4 at the top / bottom: the user's straps may run into the rings
CF_ERASE = [((-43.63, 0, 0, 3520), (72, 20)), ((2920, 0, 2963.25, 3520), (72, 20)),
            ((0, 3520, 2920, 3557.95), (71, 20)), ((0, -38.27, 2920, 0), (71, 20))]
PIN_DT, LABEL_DT = 16, 5                                     # sky130 pin and label purposes


def cf_top(params: dict) -> str:
    return CF_WRAPPERS.get(str(params.get("wrapper") or "digital"), CF_WRAPPERS["digital"])


def cf_edge(layout: kdb.Layout, cell: kdb.Cell) -> dict[tuple[int, int], kdb.Region]:
    """The wrapper's edge as the XOR sees it, per layer: shapes outside the user area up to the rings' outer edge,
    flattened, without labels; pin shapes count as the drawn layer (Magic reads them as the same material)."""
    um = lambda b: kdb.Box(*(round(v / layout.dbu) for v in b))
    ring = kdb.Region(um(CF_EDGE)) - kdb.Region(um(CF_AREA))
    out: dict[tuple[int, int], kdb.Region] = {}
    for li in layout.layer_indexes():
        info = layout.get_info(li)
        if info.datatype == LABEL_DT:
            continue
        key = (info.layer, 20 if info.datatype == PIN_DT else info.datatype)
        r = kdb.Region(cell.begin_shapes_rec(li)) & ring
        for box, layer in CF_ERASE:
            if key == layer:
                r -= kdb.Region(um(box))
        if not r.is_empty():
            out[key] = (out[key] + r).merged() if key in out else r.merged()
    return out


def cf_xor(layout: kdb.Layout, cell: kdb.Cell, golden: Path, top: str) -> dict[str, int]:
    """Differences per layer between the edge of `cell` and that of the empty wrapper in `golden` (in um, the units of
    both: the golden file may use another dbu)."""
    g = kdb.Layout()
    g.read(str(golden))
    gc = g.cell(top) or g.top_cell()
    a, b = cf_edge(layout, cell), cf_edge(g, gc)
    out = {}
    for key in sorted(set(a) | set(b)):
        ra = (a.get(key) or kdb.Region()).transformed(kdb.ICplxTrans(layout.dbu / 0.001))
        rb = (b.get(key) or kdb.Region()).transformed(kdb.ICplxTrans(g.dbu / 0.001))
        n = (ra ^ rb).count()
        if n:
            out[f"{key[0]}/{key[1]}"] = n
    return out


def cf_checks(layout: kdb.Layout, cell: kdb.Cell, pdk: Pdk, params: dict, golden: Path | None) -> list[Check]:
    out: list[Check] = []
    top = cf_top(params)
    if pdk.name not in ("sky130A", "sky130B"):
        out.append(Check("cf_pdk", "PDK is sky130A or sky130B", "fail", f"this project uses {pdk.name}"))
    badn = [c.name for c in layout.each_cell() if "#" in c.name or "/" in c.name]
    out.append(Check("cf_cellnames", "No '#' or '/' in cell names", "fail" if badn else "pass", ", ".join(badn[:4])))
    prb = _region(layout, cell, (235, 4))
    area = kdb.DBox(*CF_AREA)
    ok = not prb.is_empty() and prb.bbox().to_dtype(layout.dbu) == area
    out.append(Check("cf_prboundary", "prBoundary (235/4) covers the user area 0,0 to 2920,3520 um", "pass" if ok else "fail",
                     "" if ok else ("missing: the clear-area density deck measures inside it" if prb.is_empty() else
                                    f"it spans {prb.bbox().to_dtype(layout.dbu)}")))
    edge = kdb.DBox(*CF_EDGE)
    bb = cell.dbbox()
    outside = not bb.empty() and (bb + edge) != edge
    out.append(Check("cf_extent", "Nothing beyond the power rings' outer edge", "fail" if outside else "pass",
                     f"the layout spans {bb.left:g},{bb.bottom:g} to {bb.right:g},{bb.top:g} um" if outside else ""))
    title = f"Edge of the wrapper matches Caravel's empty {top} (XOR)"
    if golden is None:
        out.append(Check("cf_xor", title, "manual",
                         f"give the path of {top}_empty.gds (github.com/chipfoundry/caravel, gds/) as golden_gds to compare here; "
                         "cf-precheck compares the pins and power rings outside the user area"))
    else:
        diff = cf_xor(layout, cell, golden, top)
        out.append(Check("cf_xor", title, "fail" if diff else "pass",
                         ", ".join(f"{k}: {n}" for k, n in list(diff.items())[:6]) +
                         (" differences; start from the empty wrapper and keep its edge as it is" if diff else "")))
    out.append(Check("cf_gpio", "GPIO defaults in verilog/rtl/user_defines.v", "manual",
                     "cf-precheck reads USER_CONFIG_GPIO_5..37_INIT from the repository (analog: 14-24 are skipped)"))
    out.append(Check("cf_oeb", "io_oeb drive and power (CVC)", "manual", "cf-precheck runs CVC on the extracted netlist"))
    return out


# ----------------------------------------------------------------------------------- entry points


def renamed_copy(layout: kdb.Layout, cell_name: str, top: str) -> tuple[kdb.Layout, kdb.Cell]:
    """What is submitted: a layout whose only top cell is `top` (a copy of `cell_name` and everything below it)."""
    src = layout.cell(cell_name)
    if src is None:
        raise KeyError(f"no layout cell {cell_name}")
    new = kdb.Layout()
    new.dbu = layout.dbu
    nc = new.create_cell(top)
    nc.copy_tree(src)
    return new, nc


def run_checks(layout: kdb.Layout, cell_name: str, pdk: Pdk | None, target: str, params: dict,
               golden: Path | None = None) -> list[dict]:
    """The target's layout checks. `golden`: Caravel's empty wrapper for the ChipFoundry XOR (a path the server
    allows; the caller checks it)."""
    if target not in TARGETS:
        raise KeyError(f"unknown target {target}")
    t = TARGETS[target]
    if layout.cell(cell_name) is None:
        raise KeyError(f"no layout cell {cell_name}")
    if t["family"] and (not pdk or pdk.profile.family != t["family"]):
        raise ValueError(f"{t['label']} needs a {t['family']} PDK (this project uses {pdk.profile.family if pdk else 'none'})")
    max_metal = t["max_metal"] or params.get("max_metal") or None
    if target == "tt-sky130-analog":
        top = (params.get("top_module") or "").strip() or cell_name
        sub, cell = renamed_copy(layout, cell_name, top)        # check what would be submitted
        return [c.public() for c in generic_checks(sub, cell, pdk, max_metal) + tt_checks(sub, cell, pdk, {**params, "top_module": top})]
    if target == "chipfoundry-caravel":
        sub, cell = renamed_copy(layout, cell_name, cf_top(params))
        # the wrapper's power rings lie below and left of the origin by design: cf_extent checks the extent instead
        return [c.public() for c in generic_checks(sub, cell, pdk, max_metal) + cf_checks(sub, cell, pdk, params, golden)
                if c.id != "origin"]
    if target == "wafer-space-gf180":
        sub, cell = renamed_copy(layout, cell_name, cell_name)
        own = ws_checks(sub, cell, pdk, params)
        return [c.public() for c in generic_checks(sub, cell, pdk, None) if c.id != "origin"] + [c.public() for c in own]
    return [c.public() for c in generic_checks(layout, layout.cell(cell_name), pdk, max_metal)]


def _tool_versions() -> dict:
    return {n: v for n in ("klayout", "magic", "netgen", "ngspice") if (v := tools.version(n))}


def _lef(name: str, layout: kdb.Layout, cell: kdb.Cell, pdk: Pdk, tiles: str) -> str:
    """Pin-only abstract: the template pins at their fixed positions plus the power stripes of the layout."""
    dbu = layout.dbu
    tpl = tt_template(tiles)
    pins = tt_pins(layout, cell, pdk.profile)
    um = lambda nm: f"{nm / 1000:.3f}".rstrip("0").rstrip(".") if nm % 10 else f"{nm / 1000:.3f}"
    L = ["VERSION 5.7 ;", 'BUSBITCHARS "[]" ;', 'DIVIDERCHAR "/" ;', "", f"MACRO {name}", "  CLASS BLOCK ;", f"  FOREIGN {name} ;",
         "  ORIGIN 0 0 ;", f"  SIZE {tpl['die'][0] / 1000:.3f} BY {tpl['die'][1] / 1000:.3f} ;", "  SYMMETRY X Y R90 ;"]
    for n in ("VDPWR", "VGND"):
        L += [f"  PIN {n}", "    DIRECTION INOUT ;", f"    USE {'GROUND' if n == 'VGND' else 'POWER'} ;", "    PORT", "      LAYER met4 ;"]
        L += [f"        RECT {b[0] * dbu:.3f} {b[1] * dbu:.3f} {b[2] * dbu:.3f} {b[3] * dbu:.3f} ;" for b in pins.get(n, [])]
        L += ["    END", f"  END {n}"]
    for n, p in tpl["pins"].items():
        r = p["rect"]
        L += [f"  PIN {n}", f"    DIRECTION {p['direction']} ;", f"    USE {p['use']} ;", "    PORT", f"      LAYER {p['layer']} ;",
              f"        RECT {r[0] / 1000:.3f} {r[1] / 1000:.3f} {r[2] / 1000:.3f} {r[3] / 1000:.3f} ;", "    END", f"  END {n}"]
    L += [f"END {name}", "", "END LIBRARY", ""]
    return "\n".join(L)


def _verilog(top: str) -> str:
    return f"""/*
 * Port stub for {top} (generated by Undine). The analog design itself is the layout; this file only declares the pins.
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module {top} (
    input  wire       VGND,
    input  wire       VDPWR,    // 1.8v power supply
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    inout  wire [7:0] ua,       // Analog pins, only ua[5:0] can be used
    input  wire       ena,      // always 1 when the design is powered, so you can ignore it
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);

endmodule
"""


def _info_yaml(top: str, tiles: str, analog_pins: int, ua: list[str], title: str, author: str, description: str) -> str:
    q = lambda s: json.dumps(s, ensure_ascii=False)
    pin = lambda k, i: f"  {k}[{i}]: {q(ua[i]) if k == 'ua' and i < len(ua) and i < analog_pins else chr(34) * 2}"
    out = ["# Tiny Tapeout project information (generated by Undine; check it against the current template)", "project:",
           f"  title:        {q(title)}", f"  author:       {q(author)}", '  discord:      ""', f"  description:  {q(description)}",
           '  language:     "Analog"', "  clock_hz:     0", f'  tiles: "{tiles}"', f"  analog_pins: {analog_pins}", "  uses_vapwr: false",
           f'  top_module:  "{top}"', "  source_files:", '    - "project.v"', "", "pinout:"]
    out += [pin("ui", i) for i in range(8)] + [pin("uo", i) for i in range(8)] + [pin("uio", i) for i in range(8)] + [pin("ua", i) for i in range(6)]
    out += ["", "yaml_version: 6", ""]
    return "\n".join(out)


def _ws_submit(top: str, params: dict) -> str:
    slot = params.get("slot") or "1x1"
    cob = str(params.get("cob", "no")).lower() in ("yes", "true", "1", "on")
    flags = (f" --slot {slot}" if slot != "1x1" else "") + (" --cob" if cob else "")
    return f"""# Submitting {top} to wafer.space

Generated by Undine. The local checks in report.json mirror the layout checks of the precheck; the precheck itself is the
judge.

1. Run the precheck (https://github.com/wafer-space/gf180mcu-precheck) on the GDS, in its nix shell with the PDK it
   pins (gf180mcuD, open_pdks {WS_PDK[1][:12]}):

       export PDK_ROOT=gf180mcu PDK=gf180mcuD
       python3 precheck.py --input gds/{top}.gds --top {top}{flags}

2. On https://platform.wafer.space create the project with slot size {slot}{' and Chip-on-Board' if cob else ''},
   then give it a link to the GDS (optionally with its SHA256 from report.json). The platform runs the same precheck;
   when it passes, choose *Submit for Manufacturing*.
"""


def _cf_submit(top: str, params: dict) -> str:
    analog = top == CF_WRAPPERS["analog"]
    return f"""# Submitting {top} to ChipFoundry

Generated by Undine. The local checks in report.json mirror the layout checks of cf-precheck; cf-precheck itself is the
judge, and it also reads files Undine does not make.

1. Put the GDS into your project repository (from chipfoundry/caravel_user_project{'_analog' if analog else ''}) as
   `gds/{top}.gds`, next to:
   - `verilog/rtl/user_defines.v` with the GPIO defaults (USER_CONFIG_GPIO_5..37_INIT{', 14-24 not checked for analog' if analog else ''});
   - `lvs/{top}/lvs_config.json` and the netlists LVS compares with ({'the SPICE netlist of the wrapper' if analog else 'verilog/gl/' + top + '.v and the macros'});
   - `.cf/project.json` (`cf init` makes it).
2. Run the precheck with the ChipFoundry CLI (`pip install chipfoundry-cli`): `cf precheck`.
3. Upload with `cf push`: it sends the GDS, user_defines.v and .cf/project.json.
"""


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def export_bundle(project_path: Path, layout: kdb.Layout, lib: str, cell_name: str, pdk: Pdk, target: str, params: dict,
                  revision: str | None = None, provenance: dict | None = None, golden: Path | None = None) -> dict:
    """Writes <project>/tapeout/<target>/<top>-<time>/: GDS, LEF, Verilog stub, info.yaml, docs and report.json (+ zip).
    `provenance` goes into report.json: {"layout": content hash of the exported cell, "signoff": Server.signoff()}."""
    checks = run_checks(layout, cell_name, pdk, target, params, golden)
    top = ((params.get("top_module") or cell_name).strip() if target == "tt-sky130-analog" else
           cf_top(params) if target == "chipfoundry-caravel" else cell_name)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = project_path / "tapeout" / target / f"{top}-{stamp}"
    (d / "gds").mkdir(parents=True)
    new, nc = renamed_copy(layout, cell_name, top)
    opt = kdb.SaveLayoutOptions()
    opt.format = "GDS2"
    new.write(str(d / "gds" / f"{top}.gds"), opt)
    files = [d / "gds" / f"{top}.gds"]
    if target == "tt-sky130-analog":
        tiles = params.get("tiles", "1x2")
        for sub in ("lef", "src", "docs"):
            (d / sub).mkdir()
        (d / "lef" / f"{top}.lef").write_text(_lef(top, new, nc, pdk, tiles))
        (d / "src" / "project.v").write_text(_verilog(top))
        (d / "gds" / f"{top}.v").write_text(_verilog(top))              # the precheck reads <gds stem>.v
        ua = [x.strip() for x in str(params.get("ua", "")).split(",")] if params.get("ua") else []
        (d / "info.yaml").write_text(_info_yaml(top, tiles, _analog_pins(new, nc, params), ua,
                                                params.get("title") or top, params.get("author", ""), params.get("description", "")))
        (d / "docs" / "info.md").write_text(
            f"## How it works\n\n{params.get('description') or 'Describe the circuit.'}\n\n## How to test\n\nExplain how to use the analog pins "
            f"({', '.join(f'ua[{i}]: {u}' for i, u in enumerate(ua))}).\n\n## External hardware\n\nNone.\n")
        files += [d / "lef" / f"{top}.lef", d / "src" / "project.v", d / "gds" / f"{top}.v", d / "info.yaml", d / "docs" / "info.md"]
    if target == "wafer-space-gf180":
        (d / "SUBMIT.md").write_text(_ws_submit(top, params))
        files.append(d / "SUBMIT.md")
    if target == "chipfoundry-caravel":
        (d / "SUBMIT.md").write_text(_cf_submit(top, params))
        files.append(d / "SUBMIT.md")
    report = {"target": target, "label": TARGETS[target]["label"], "params": params, "lib": lib, "cell": cell_name, "top": top,
              "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "pdk": pdk.id, "tools": _tool_versions(), "design_revision": revision,
              "checks": checks, "files": {str(f.relative_to(d)): _sha(f) for f in files}, **(provenance or {}),
              "note": "Local layout checks only. Run the sign-off suite (KLayout, Magic DRC, Magic+Netgen LVS) and the shuttle's own precheck before submitting."}
    (d / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    z = shutil.make_archive(str(d), "zip", d.parent, d.name)
    return {"dir": str(d.relative_to(project_path)), "zip": Path(z).name, "files": sorted(str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()),
            "checks": checks, "report": report}
