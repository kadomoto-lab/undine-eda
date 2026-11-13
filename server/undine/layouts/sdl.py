"""Schematic-driven layout and cross-probing.

A layout instance is bound to a schematic instance through the instance
property `undine_inst` (the schematic instance name). From the schematic the
server can generate the missing devices (PDK PCells, or the layout of a
hierarchical child), report what is placed / missing / extra, produce a
ratsnest of unrouted connections and translate selections in either direction
(instance <-> instance, net <-> instances).
"""
from __future__ import annotations

import itertools
import math
import re
from pathlib import Path

import klayout.db as kdb

from undine.pdks import pcell
from undine.schematics import schematic, xschem
from undine.layouts import drawing, placement
from undine.layouts.layout import LayoutDB, stdcell_layout
from undine.pdks.pdk import Pdk

_PREFIX = (("nfet", "nfet"), ("pfet", "pfet"), ("res_", None), ("cap_mim", "mim_cap"), ("npn", "npn_bjt"), ("pnp", "pnp_bjt"))


def _num(v: str):
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


_SCALE = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3}


def _um(v) -> float | None:
    """A length from a schematic property in um: "0.28u" / "280n" / "2.8e-7" are SPICE values in metres, a plain number
    of 1e-3 or more is already um (sky130's symbols)."""
    t = str(v).strip().strip("'\"").lower()
    m = re.fullmatch(r"([-+]?[0-9.]+(?:e[-+]?\d+)?)([fpnum]?)", t)
    if not m:
        return None
    x = float(m.group(1))
    if m.group(2):
        return round(x * _SCALE[m.group(2)] * 1e6, 6)
    return round(x * 1e6, 6) if 0 < abs(x) < 1e-3 else x


def _count(props: dict, *keys: str) -> int:
    for k in keys:
        if props.get(k, "") != "":
            n = _num(props[k])
            return int(n) if isinstance(n, float) and n >= 1 else 1
    return 1


def device_pcell(dev: schematic.Device, props: dict, catalog: dict[str, dict], family: str = "sky130") -> tuple[str, dict] | None:
    """Maps a schematic primitive to (pcell name, params), or None, by the PDK family's naming."""
    if family == "gf180mcu":
        return _gf180_pcell(dev, props, catalog)
    if family.startswith("ihp"):
        return _ihp_pcell(dev, props, catalog)
    return _sky130_pcell(dev, props, catalog)


def _sky130_pcell(dev: schematic.Device, props: dict, catalog: dict[str, dict]) -> tuple[str, dict] | None:
    model = props.get("model", "") or dev.symname
    full = model if model.startswith("sky130_fd_pr__") else f"sky130_fd_pr__{model}"
    short = full.removeprefix("sky130_fd_pr__")
    name = None
    for pre, pc in _PREFIX:
        if short.startswith(pre):
            name = pc
            break
    if short.startswith("res_"):
        name = "res_poly" if "po" in short else "res_diff" if "iso" in short or "pw" in short or "nw" in short else "res_metal"
    if name is None or name not in catalog:
        return None
    params: dict = {}
    decl = {p["name"]: p for p in catalog[name]["params"]}
    choices = decl.get("type", {}).get("choices") or []
    if full in choices:
        params["type"] = full
    elif name == "mim_cap" and (alias := {"cap_mim_m3_1": "sky130_fd_pr__model__cap_mim",
                                          "cap_mim_m3_2": "sky130_fd_pr__model__cap_mim_m4"}.get(short)) in choices:
        params["type"] = alias                       # the PCell names the MIM caps by their model, the schematic by the device
    elif "type" in decl:
        return None
    if name in ("nfet", "pfet"):
        params["bulk"] = "bulk tie"  # tap + well/implant so that the device is extractable
        # gate contact on the outer side (nfet below, pfet above) keeps the space between a stacked pair free
        params["gate_con_pos"] = "top" if name == "pfet" else "bottom"
        # The schematic's W is the total width of the nf fingers (the sky130 symbols compute ad / as from W / nf), the
        # PCell's w is the width of one finger. mult parallel devices become more fingers of the same device, so the
        # layout has W * mult in nf * mult fingers, which is what LVS compares against W with m = mult.
        nf = int(_num(props.get("nf", "")) or 1) if props.get("nf", "") != "" else 1
        mult = props.get("mult", "") or props.get("m", "")
        mult = int(_num(mult) or 1) if mult != "" else 1
        if props.get("W", "") != "":
            params["w"] = round(_num(props["W"]) / nf, 4)
        if props.get("L", "") != "":
            params["l"] = _num(props["L"])
        if nf != 1 or mult != 1:
            params["nf"] = nf * mult
    elif name.startswith("res_"):
        # resistors: length from the schematic if given
        if props.get("W", "") != "":
            params["w"] = _num(props["W"])
        if props.get("L", "") != "":
            params["len"] = _num(props["L"])
    elif name == "mim_cap":
        if props.get("W", "") != "":
            params["w"] = _num(props["W"])
        if props.get("L", "") != "":
            params["l"] = _num(props["L"])
    return name, {k: v for k, v in params.items() if v != ""}


_GF_VOLT = {"03v3": "3.3V", "05v0": "5V", "06v0": "6V"}


# GF180MCU passives: schematic model -> (PCell, fixed parameters); the symbols give W / L in metres
_GF_RES = {"ppolyf_u": "ppolyf_u_resistor", "npolyf_u": "npolyf_u_resistor", "nplus_u": "nplus_u_resistor",
           "pplus_u": "pplus_u_resistor", "nwell": "nwell_resistor"}


def _gf180_passive(model: str, props: dict, catalog: dict[str, dict]) -> tuple[str, dict] | None:
    if _count(props, "m", "mult") != 1:
        return None                                    # parallel copies are not generated (yet)
    w, length = _um(props.get("W", "")), _um(props.get("L", ""))
    if model in _GF_RES:
        name, params = _GF_RES[model], {}
    elif re.fullmatch(r"rm[1-3]", model):
        name, params = "metal_resistor", {"res_type": model}
    elif model == "cap_mim_2f0ff":
        # gf180mcuD's MIM option B sits under the top metal (Metal5 in its 5-metal stack); its LVS deck extracts 2 fF/um2
        name, params = "cap_mim", {"mim_option": "MIM-B", "metal_level": "M5"}
        if w is not None:
            params["wc"] = w
        if length is not None:
            params["lc"] = length
        return (name, params) if name in catalog else None
    else:
        return None
    if name not in catalog:
        return None
    if w is not None:
        params["w_res"] = w
    if length is not None:
        params["l_res"] = length
    return name, params


def _gf180_pcell(dev: schematic.Device, props: dict, catalog: dict[str, dict]) -> tuple[str, dict] | None:
    """GF180MCU MOSFETs (nfet_03v3 / 05v0 / 06v0, pfet_..., nfet_06v0_nvt): the symbols' W is the total of the nf
    fingers (their ad / as use W / nf), the PCell's w_gate one finger's, as for sky130. Resistors and the MIM capacitor
    map to their PCells with W / L as they are."""
    model = (props.get("model", "") or Path(dev.symname).stem).lower()
    m = re.fullmatch(r"([np])fet_(03v3|05v0|06v0)(_nvt)?", model)
    if not m:
        return _gf180_passive(model, props, catalog)
    name = "nfet_06v0_nvt" if m.group(3) else f"{m.group(1)}fet"
    if name not in catalog:
        return None
    decl = {p["name"]: p for p in catalog[name]["params"]}
    params: dict = {}
    if "volt" in decl and _GF_VOLT[m.group(2)] in (decl["volt"].get("choice_values") or []):
        params["volt"] = _GF_VOLT[m.group(2)]
    params["bulk"] = "Bulk Tie"                       # tap + implant so that the device is extractable
    params["gate_con_pos"] = "top" if m.group(1) == "p" else "bottom"
    nf, mult = _count(props, "nf"), _count(props, "m", "mult")
    if (w := _um(props.get("W", ""))) is not None:
        params["w_gate"] = round(w / nf, 4)
    if (length := _um(props.get("L", ""))) is not None:
        params["l_gate"] = length
    if nf * mult != 1:
        params["nf"] = nf * mult
    return name, params


_IHP_MOS = {"sg13_lv_nmos": "nmos", "sg13_lv_pmos": "pmos", "sg13_hv_nmos": "nmosHV", "sg13_hv_pmos": "pmosHV"}


def _ihp_pcell(dev: schematic.Device, props: dict, catalog: dict[str, dict]) -> tuple[str, dict] | None:
    """IHP SG13G2 MOSFETs: the symbols' and the PCell's w are both the total over ng gates; m parallel devices become
    more gates of the same device."""
    model = (props.get("model", "") or Path(dev.symname).stem).lower()
    name = _IHP_MOS.get(model)
    if name is None:
        return _ihp_passive(model, props, catalog)
    if name not in catalog:
        return None
    decl = {p["name"]: p for p in catalog[name]["params"]}
    params: dict = {}
    ng, mult = _count(props, "ng", "nf"), _count(props, "m", "mult")
    if (w := _um(props.get("w", props.get("W", "")))) is not None:
        params["w"] = f"{round(w * mult, 4):g}u"
    if (length := _um(props.get("l", props.get("L", "")))) is not None:
        params["l"] = f"{length:g}u"
    if ng * mult != 1:
        params["ng"] = str(ng * mult)
    ring = [c for c in (decl.get("guardRingType", {}).get("choice_values") or []) if c.strip("'") != "none"]
    if ring:
        params["guardRingType"] = ring[0]              # the substrate / well tap: extractable, and no latch-up finding
    return name, params


_IHP_PASSIVE = {"rppd": "rppd", "rsil": "rsil", "rhigh": "rhigh", "cap_cmim": "cmim"}


def _ihp_passive(model: str, props: dict, catalog: dict[str, dict]) -> tuple[str, dict] | None:
    """SG13G2 resistors (rppd, rsil, rhigh: w, l and bends b) and the MIM capacitor (cap_cmim -> cmim: w, l)."""
    name = _IHP_PASSIVE.get(model)
    if name is None or name not in catalog or _count(props, "m", "mult") != 1:
        return None
    params: dict = {}
    for k in ("w", "l"):
        if (v := _um(props.get(k, props.get(k.upper(), "")))) is not None:
            params[k] = f"{v:g}u"
    if name != "cmim" and props.get("b", "") not in ("", "0"):
        params["b"] = str(int(_num(props["b"])))
    # the PCells derive one size from the value unless told to compute the value from the sizes
    want = "C" if name == "cmim" else "R"
    calc = next((c for c in (({p["name"]: p for p in catalog[name]["params"]}.get("Calculate") or {}).get("choice_values") or [])
                 if c.strip("'") == want), None)
    if calc:
        params["Calculate"] = calc
    return name, params


def _stdcell_name(dev: schematic.Device, props: dict) -> str | None:
    """The cell a symbol netlists as, if it may be a standard cell: a subcircuit symbol's own name, or prefix + name for
    the PDK's own standard-cell symbols (sky130_stdcells/inv_1.sym: type primitive, format "... @prefix\\\\inv_1")."""
    if dev.type == "subcircuit":
        return dev.symname
    fmt = dev.sym.global_props.get("format", "") if dev.sym is not None else ""
    m = re.search(r"@prefix\\*(\w+)\s*$", fmt.strip())
    return props.get("prefix", "") + m.group(1) if m and props.get("prefix") else None


def _fingers(d: dict) -> int:
    p = d.get("params") or {}
    return int(_num(p.get("nf", p.get("ng", 1))) or 1)


def _mos_kind(pcell_name: str | None) -> str | None:
    """n / p for the MOSFET PCells of the PDKs, else None."""
    if pcell_name in ("nfet", "nfet_06v0_nvt", "nmos", "nmosHV"):
        return "n"
    if pcell_name in ("pfet", "pmos", "pmosHV"):
        return "p"
    return None


STRAP = "_par"   # suffix of the cell that holds a multi-finger PCell and the straps that make it one device
TIE = "_tie"     # ... and the strap that joins the source / drain beside a butted tap to the tap (it is on the bulk net)
LEFT = "_left"   # ... with the PCell mirrored, its tap on the left
DEVICE_CELL = re.compile(r".+_[0-9a-f]{6}(" + re.escape(STRAP) + ")?(" + re.escape(TIE) + ")?(" + re.escape(LEFT) + ")?")
# a cell sync places for a PCell: pcell.cell_name's hash, then the wrapper's suffixes
PCELL_META = "undine.pdks.pcell"   # the meta information a generated PCell variant carries (pcell_worker.py)


def generated_cells(layout: kdb.Layout) -> set[str]:
    """The cells Undine generated for the PDK's PCells: the variants (place_pcell's, sync_layout's: their meta
    information says which PCell with which parameters), and the cells sync_layout puts one in with its straps / tie
    (DEVICE_CELL with a suffix, holding generated cells only, named after one)."""
    out = {c.name for c in layout.each_cell() if not c.is_ghost_cell()
           and any(m.name == PCELL_META for m in c.each_meta_info())}
    for c in layout.each_cell():
        m = DEVICE_CELL.fullmatch(c.name)
        if c.name in out or c.is_ghost_cell() or not m or not any(m.groups()):
            continue
        kids = {layout.cell(i).name for i in c.each_child_cell()}
        if kids and kids <= out and any(c.name.startswith(k) for k in kids):
            out.add(c.name)
    return out


def unused_generated(layout: kdb.Layout) -> list[str]:
    """The generated cells (generated_cells) nothing in the layout places, and those only they place, parents first:
    what delete_unused_cells takes out."""
    gen = generated_cells(layout)
    out: list[str] = []
    while True:
        gone = set(out)
        more = sorted(n for n in gen - gone if not any(layout.cell(p).name not in gone
                                                        for p in layout.cell(n).each_parent_cell()))
        if not more:
            return out
        out += more

# the layout parameters a device keeps with its layout instance (LAYOUT_PROP), by what it is: sync_layout(layout=...)
MOS_LAYOUT = {"nf": "fingers drawn, an integer: each is the schematic's W x mult / nf wide (the schematic keeps its nf)",
              "bulk": "auto (default: a butted tie where a source / drain is on the bulk net, else a guard ring), tie, "
                      "ring or none (no tap: draw one)",
              "tap_side": "right (default) or left: the side of a butted tie",
              "gate": "bottom, top or alternating: where the gate contacts are",
              "sd_contacts": "contact columns per source / drain, an integer"}
UNIT_LAYOUT = {"columns": "columns of the unit array (default: as square as the unit count allows)"}
BULK = {"tie": "bulk tie", "ring": "guard ring", "none": "none"}
# thick-oxide devices: Magic's diff/tap.16 wants a butted tap 0.7 um long; the PCell's is 1.5 (nfet) / 1.3 (pfet) x the
# source / drain, 0.45 um with one contact column, 0.99 / 0.86 um with two
HV_MODEL = re.compile(r"g5v0d10v5|05v0|g5v0d16v0|10v5|20v0")


def _choice(decl: dict, key: str, want: str) -> str | None:
    """The PCell's own spelling of choice `want` of parameter `key` (sky130 "bulk tie", GF180 "Bulk Tie")."""
    return next((c for c in (decl.get(key, {}).get("choice_values") or decl.get(key, {}).get("choices") or [])
                 if c.lower() == want.lower()), None)


def layout_params(name: str, kind: str | None, given: dict) -> dict:
    """`given` (a device's layout parameters) checked against what a device of this kind takes ("mos", "units" or
    None): ValueError naming the ones it takes. None values are kept (they take a stored value back)."""
    keys = MOS_LAYOUT if kind == "mos" else UNIT_LAYOUT if kind == "units" else {}
    if not isinstance(given, dict):
        raise ValueError(f"layout.{name} is an object of layout parameters, e.g. {{\"nf\": 4}}")
    bad = [k for k in given if k not in keys]
    if bad:
        raise ValueError(f"{name} takes no layout parameter {', '.join(bad)}" +
                         (f": it takes {'; '.join(f'{k} ({v})' for k, v in keys.items())}" if keys else
                          ": it is no PCell device sync_layout generates"))
    out = {}
    for k, v in given.items():
        if v is None:
            out[k] = None
        elif k in ("nf", "sd_contacts", "columns"):
            if isinstance(v, bool) or not isinstance(v, (int, float, str)) or not str(v).isdigit() or int(v) < 1:
                raise ValueError(f"{name}: {k} is a whole number from 1, not {v!r}")
            out[k] = int(v)
        else:
            choices = {"bulk": ("auto", *BULK), "tap_side": ("right", "left"), "gate": ("bottom", "top", "alternating")}[k]
            if str(v).lower() not in choices:
                raise ValueError(f"{name}: {k} is {', '.join(choices)}, not {v!r}")
            out[k] = str(v).lower()
    return out


def _strap_shapes(layout: kdb.Layout, cell: kdb.Cell, L: dict[str, int], rules=None) -> list[tuple[int, kdb.Box]] | None:
    """Straps that join the fingers of a multi-finger MOSFET PCell into one device. The source / drain columns (metal1
    stripes beside each gate) are joined alternately by a metal2 bar along the top and along the bottom of the diffusion,
    through via1 on metal1 pads; the gates by a metal1 bar over the PCell's gate pads (sky130, GF180MCU: staggered
    outside the diffusion) or, where the PCell draws no gate contacts (IHP), by a poly bar below the diffusion. `rules`
    is the profile's Strap (sky130's numbers by default). None if the fingers are too short for two metal2 bars (then
    the device is left unstrapped)."""
    from undine.pdks.pdkprofile import Strap
    r = rules or Strap(via=0.15, enc1=(0.085, 0.055), enc2=(0.085, 0.055), m2_space=0.14)
    um = lambda v: int(round(v / layout.dbu))
    diff_r = kdb.Region(cell.begin_shapes_rec(L["diff"])).merged()
    gates = sorted((g.bbox() for g in (kdb.Region(cell.begin_shapes_rec(L["poly"])) & diff_r).merged().each()),
                   key=lambda b: b.left) if "poly" in L else []
    m1 = [b.bbox() for b in kdb.Region(cell.begin_shapes_rec(L["met1"])).merged().each()]
    diff = (kdb.Region([kdb.Box(g.left, g.bottom, g.right, g.top) for g in gates]).bbox() if gates else diff_r.bbox())
    gate_shapes = _gate_contacts(layout, cell, L, r, diff) if r.gate == "poly" and r.cont else None
    inside = [b for b in m1 if b.bottom >= diff.bottom and b.top <= diff.top]
    if gates:
        # the stripe nearest to each side of each gate: a tap butted to the device (GF180's bulk tie) is not a column
        cols = sorted({min((b for b in inside if b.right <= g.left), key=lambda b: g.left - b.right, default=None) for g in gates} |
                      {min((b for b in inside if b.left >= g.right), key=lambda b: b.left - g.right, default=None) for g in gates}
                      - {None}, key=lambda b: b.left)
    else:
        cols = sorted(inside, key=lambda b: b.left)
    pads = [b for b in m1 if b.top <= diff.bottom or b.bottom >= diff.top]
    if len(cols) < 3:
        return gate_shapes or []                       # one finger: only the gate contact (if the PCell has none)
    out: list[tuple[int, kdb.Box]] = list(gate_shapes or [])
    if gate_shapes is None and r.gate == "poly":
        polys = [b.bbox() for b in kdb.Region(cell.begin_shapes_rec(L["poly"])).merged().each()
                 if b.bbox().overlaps(diff)]
        if len(polys) < 2:
            return []
        y1 = min(b.bottom for b in polys)
        out.append((L["poly"], kdb.Box(min(b.left for b in polys), y1 - um(r.poly_w), max(b.right for b in polys), y1)))
    elif gate_shapes is None:
        if len(pads) < 2:
            return []
        below = all(p.top <= diff.bottom for p in pads)
        outer = min(pads, key=lambda p: p.bottom) if below else max(pads, key=lambda p: p.top)
        bar = kdb.Box(min(p.left for p in pads), outer.bottom, max(p.right for p in pads), outer.top)
        out.append((L["met1"], bar))
        for p in pads:
            if not p.overlaps(bar):
                out.append((L["met1"], kdb.Box(p.left, bar.bottom, p.right, p.top) if below else kdb.Box(p.left, p.bottom, p.right, bar.top)))
    via = um(r.via)
    pad_w, pad_h = via + 2 * um(r.enc1[0]), via + 2 * um(r.enc1[1])
    bar_h = via + 2 * um(r.enc2[1])
    top, bottom = min(c.top for c in cols), max(c.bottom for c in cols)
    if top - bottom < 2 * max(pad_h, bar_h) + um(r.m2_space):
        return None
    for group, (y0, y1) in ((cols[0::2], (top - max(pad_h, bar_h), top)), (cols[1::2], (bottom, bottom + max(pad_h, bar_h)))):
        if len(group) < 2:
            continue
        xs = []
        for c in group:
            cx = (c.left + c.right) // 2
            vy = (y0 + y1) // 2 - via // 2
            out.append((L["met1"], kdb.Box(cx - pad_w // 2, vy - um(r.enc1[1]), cx + pad_w // 2, vy + via + um(r.enc1[1]))))
            out.append((L["via"], kdb.Box(cx - via // 2, vy, cx - via // 2 + via, vy + via)))
            xs.append(cx)
        out.append((L["met2"], kdb.Box(xs[0] - via // 2 - um(r.enc2[0]), (y0 + y1) // 2 - bar_h // 2,
                                       xs[-1] - via // 2 + via + um(r.enc2[0]), (y0 + y1) // 2 - bar_h // 2 + bar_h)))
    return out


def _gate_contacts(layout: kdb.Layout, cell: kdb.Cell, L: dict[str, int], r, diff: kdb.Box) -> list[tuple[int, kdb.Box]]:
    """A poly bar below the diffusion that joins the gate fingers, a contact on each and a metal1 pad over them, for a
    PCell that draws no gate contacts (IHP): the gate then routes, and takes a label, in metal1. The bar hangs from the
    fingers' ends, which the PDK keeps far enough from the diffusion (gate extension >= contact-to-diffusion space)."""
    um = lambda v: int(round(v / layout.dbu))
    polys = sorted((b.bbox() for b in kdb.Region(cell.begin_shapes_rec(L["poly"])).merged().each() if b.bbox().overlaps(diff)),
                   key=lambda b: b.left)
    if not polys:
        return []
    top = min(b.bottom for b in polys)                 # where the fingers end below the diffusion
    c, ep, (m1l, m1s) = um(r.cont), um(r.cont_poly), (um(r.cont_m1[0]), um(r.cont_m1[1]))
    cy0, cy1 = top - ep - c, top - ep
    xs = [(b.left + b.right) // 2 for b in polys]
    out = [(L["poly"], kdb.Box(min(min(b.left for b in polys), xs[0] - c // 2 - ep), cy0 - ep,
                               max(max(b.right for b in polys), xs[-1] + c // 2 + ep), top))]
    out += [(L["cont"], kdb.Box(x - c // 2, cy0, x - c // 2 + c, cy1)) for x in xs]
    half = max(c // 2 + m1l, um(r.m1_min) // 2)
    pad_h = max(c + 2 * m1s, um(r.m1_min))
    out.append((L["met1"], kdb.Box(xs[0] - half, top - pad_h, xs[-1] + half, top)))
    return out


def _union(boxes: list[list[int]]) -> list[int]:
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _tap_text(tap: dict) -> str:
    """How a generated MOSFET's tap is to be routed, in a line (the same for every device it applies to)."""
    if tap["kind"] == "tie":
        return (f"bulk tie on the {tap['side']}: the {tap['side']}-hand source / drain is joined to the tap, route "
                f"{tap['terminal']} there")
    if tap["kind"] == "ring":
        return "guard ring: route it (li1) to the bulk net"
    return "no tap: draw one on the bulk net within the latch-up distance (LU.2)"


def taps(devs: list[dict]) -> dict[str, list[str]]:
    """The devices by how their taps are to be routed (_tap_text)."""
    out: dict[str, list[str]] = {}
    for d in devs:
        if d.get("tap"):
            out.setdefault(_tap_text(d["tap"]), []).append(d["name"])
    return out


def _tie_shapes(layout: kdb.Layout, cell: kdb.Cell, L: dict[str, int]) -> list[tuple[int, kdb.Box]]:
    """A bar on the first conductor (sky130's li1) that joins the source / drain beside a butted tap (the PCell's bulk
    tie, on the right of the diffusion) to the tap's own: that source / drain is the device's bulk net then, as the
    silicide over the butted junction makes it anyway, so routing another net to it is an LVS error, not a short in
    silicon. [] when the PCell has no butted tap."""
    diff = kdb.Region(cell.begin_shapes_rec(L["diff"])).merged()
    taps = [p.bbox() for p in kdb.Region(cell.begin_shapes_rec(L["tap"])).merged().each()]
    cond = [p.bbox() for p in kdb.Region(cell.begin_shapes_rec(L["li1"])).merged().each()]
    if diff.is_empty() or not taps:
        return []
    right = diff.bbox().right
    tap = next((t for t in taps if t.left == right), None)       # butted against the diffusion
    if tap is None:
        return []
    on_tap = [c for c in cond if c.overlaps(tap)]
    beside = [c for c in cond if c.right <= tap.left and c.top > tap.bottom and c.bottom < tap.top]
    if not on_tap or not beside:
        return []
    a, b = max(beside, key=lambda c: c.right), on_tap[0]
    return [(L["li1"], kdb.Box(a.left, max(a.bottom, b.bottom), b.right, min(a.top, b.top)))]


def _ring(prof, inner: kdb.Box, mos: str, dbu: float) -> list[tuple[tuple[int, int], object]]:
    """A guard ring around `inner` as guard_ring draws one (drawing.guard_ring: the tap ring, its contacts on one
    lattice per side, its implant on both sides, the conductor ring over it), pdkprofile.Ring's width and conductor;
    an n-tap ring's well is the caller's (it encloses the units too)."""
    r = drawing.Rules(prof, dbu)
    shapes = drawing.guard_ring(r, inner, "ptap" if mos == "n" else "ntap", width=r.d(prof.ring.width),
                                metal=prof.ring.conductor, fill_well=False)
    return [(prof.layers[layer], b) for layer, b in shapes]


def pcell_variant(pdk: Pdk, db: LayoutDB, name: str, params: dict, check: bool = True) -> str:
    """The cell of PDK PCell `name` with `params` in the library (generated and brought in once, LayoutDB.import_oas
    recording where it comes from; a variant the library has is not generated again). `check` (parameters someone
    chose): both against the PDK's catalog first, ValueError naming the PCells, the parameters it takes (with their
    defaults and choices) or the choices of a parameter. A MIM capacitor the generator would draw at another size than
    asked is refused."""
    cat = {p["name"]: p for p in pcell.catalog(pdk)} if check else {}
    if check and name not in cat:
        raise ValueError(f"no PCell {name!r} in {pdk.id if pdk else 'the PDK'}: it has {', '.join(sorted(cat)) or 'none'}")
    decl = {p["name"]: p for p in cat[name]["params"] if not p.get("readonly")} if check else {}
    bad = [k for k in params if k not in decl] if check else []
    if bad:
        raise ValueError(f"PCell {name} takes no {', '.join(bad)}: it takes " + "; ".join(
            f"{k} (default {p.get('default')!r}" + (f", one of {', '.join(map(str, p['choice_values']))}" if p.get("choice_values") else "")
            + ")" for k, p in decl.items()))
    for k, v in params.items() if check else ():
        ch = decl[k].get("choice_values")
        if ch and str(v) not in ch:
            raise ValueError(f"PCell {name}: {k} is one of {', '.join(map(str, ch))}, not {v!r}")
    cname = pcell.cell_name(name, params)
    have = db.layout.cell(cname)
    if have is None or have.is_ghost_cell():              # else generated before (its name holds the parameters' hash)
        path, cname, _ = pcell.generate(pdk, name, params)
        db.import_oas(path, {"pcell": name, "params": params})
    if name == "mim_cap" and "capm" in (pdk.profile.layers or {}):          # the generators clamp a side they find small
        types = {"sky130_fd_pr__model__cap_mim_m4": "cap2m"}
        li = db.layout.find_layer(*pdk.profile.layers[types.get(params.get("type"), "capm")])
        b = db.cell(cname).bbox_per_layer(li) if li is not None else kdb.Box()
        got = (round(b.width() * db.layout.dbu, 4), round(b.height() * db.layout.dbu, 4))
        want = (float(params.get("w", got[0])), float(params.get("l", got[1])))
        if not b.empty() and any(abs(g - w) > 1e-6 for g, w in zip(got, want)):
            raise ValueError(f"the PDK's {name} draws {got[0]:g} x {got[1]:g} um for {want[0]:g} x {want[1]:g} um "
                             "(its smallest side): give a larger W / L")
    return cname


class Sdl:
    def __init__(self, pdk: Pdk, db: LayoutDB, sch_path: Path, resolver: xschem.SymbolResolver):
        self.pdk, self.db, self.sch_path, self.resolver = pdk, db, sch_path, resolver
        self.cell = sch_path.stem

    def _catalog(self) -> dict[str, dict]:
        try:
            return {p["name"]: p for p in pcell.catalog(self.pdk)}
        except Exception:
            return {}

    def _devices(self, stored: dict[str, dict] | None = None, strict: set[str] = frozenset()) -> tuple[schematic.Connectivity, list[dict]]:
        """The schematic's devices and what the layout makes of each: kind (pcell | cell | None: unsupported, with
        why), the PCell and its parameters, units (MIMs / resistors with MF / m: that many copies of one unit), the
        tap (MOSFETs) and the device's layout parameters (`stored`: {name: parameters}, LAYOUT_PROP) applied. Layout
        parameters that no longer fit (a tie whose source left the bulk net) are a note, the device as without them;
        for the devices in `strict` (asked for now) a ValueError."""
        conn = schematic.connectivity(self.sch_path, self.resolver, self.cell)       # the netlist view (pending included)
        cat = self._catalog()
        family = self.pdk.profile.family if self.pdk else ""
        out = []
        for d in conn.devices:
            props = schematic.inst_props(d.comp, d.sym)
            item = {"name": d.name, "symbol": d.symname, "index": d.index, "type": d.type, "kind": None,
                    "nets": dict(d.nets), "units": 1, "layout": dict((stored or {}).get(d.name) or {})}
            std = _stdcell_name(d, props)
            std_lib = stdcell_layout(self.pdk, std) if std else None
            if d.type == "subcircuit" or std_lib:
                name = std if std_lib else d.symname
                item["kind"] = "cell"
                item["layout_cell_expected"] = name
                have = self.db.layout.cell(name)
                if have is not None and not have.is_ghost_cell():
                    item["layout_cell"] = name
                elif std_lib:                          # the PDK's standard cell: copied in when it is placed
                    item["layout_cell"], item["stdcell"] = name, std_lib[0]
                else:
                    item["layout_cell"] = None
                if _count(props, "m", "mult") > 1:
                    item["note"] = f"m = {_count(props, 'm', 'mult')}: one {name} is placed; place the others yourself"
            else:
                m = device_pcell(d, props, cat, family)
                if m:
                    item["kind"] = "pcell"
                    item["pcell"], item["params"] = m
                    if _mos_kind(item["pcell"]):
                        plain = dict(item["params"])
                        try:
                            self._mos_layout(item, props, cat)
                        except ValueError as e:
                            if d.name in strict:
                                raise
                            item["note"] = f"{e}; its layout parameters {item['layout']} are not applied"
                            item["params"], item["layout"] = plain, {}
                            self._mos_layout(item, props, cat)
                    else:
                        item["units"] = _count(props, "MF") * _count(props, "m", "mult")
                elif (n := _count(props, "MF") * _count(props, "m", "mult")) > 1:
                    item["note"] = f"MF / m = {n}: parallel copies of this device are not generated for {family or 'this PDK'}"
                else:
                    item["note"] = f"{props.get('model') or d.symname}: no PCell of the PDK is this device"
            out.append(item)
        return conn, out

    def _mos_layout(self, item: dict, props: dict, cat: dict) -> None:
        """A MOSFET's layout parameters (item["layout"]) on its PCell parameters, and its tap: a butted tie only where
        a source / drain is on the bulk net (the silicide over the butted junction joins them), else a guard ring
        (layout bulk overrides; a tie the nets forbid is refused); a thick-oxide tie two contact columns wide."""
        lp, params = item["layout"], item["params"]
        decl = {p["name"]: p for p in cat.get(item["pcell"], {}).get("params", [])}
        per = "w" if "w" in params and "nf" in decl else "w_gate" if "w_gate" in params else None
        if lp.get("nf") and per:
            total = round(params[per] * _fingers(item), 6)
            w = total / lp["nf"]
            if abs(w * 200 - round(w * 200)) > 1e-6:              # 5 nm grid
                raise ValueError(f"{item['name']}: W {total:g} um in {lp['nf']} fingers is {w:.4g} um a finger, off the 5 nm "
                                 "grid: choose nf so that W / nf is a multiple of 0.005")
            params[per] = round(w, 4)
            params.pop("nf", None)
            if lp["nf"] > 1:
                params["nf"] = lp["nf"]
        elif lp.get("nf"):
            raise ValueError(f"{item['name']}: its PCell ({item['pcell']}) takes no finger count Undine can set")
        if lp.get("gate") and (c := _choice(decl, "gate_con_pos", lp["gate"])):
            params["gate_con_pos"] = c
        if lp.get("sd_contacts") and "sd_con_col" in decl:
            params["sd_con_col"] = lp["sd_contacts"]
        if "bulk" not in params:                                   # IHP: its guard ring, no choice here
            return
        pins = {k.upper(): v for k, v in item["nets"].items()}
        b = pins.get("B")
        on_bulk = [t for t in ("S", "D") if pins.get(t) is not None and pins.get(t) == b]
        want = lp.get("bulk") or "auto"
        if want == "auto":
            want = "tie" if on_bulk else "ring"
        if want == "tie" and not on_bulk:
            raise ValueError(f"{item['name']}: a bulk tie butts its tap against a source / drain, and neither S ({pins.get('S')}) "
                             f"nor D ({pins.get('D')}) is on the bulk net {b}: the silicide would join it to {b}. Use bulk "
                             "ring (a guard ring) or none (no tap: draw one apart)")
        choice = _choice(decl, "bulk", BULK[want]) or ("None" if want == "none" else None)
        if choice is None:
            raise ValueError(f"{item['name']}: its PCell ({item['pcell']}) has no {BULK[want]}")
        params["bulk"] = choice
        hv = bool(HV_MODEL.search(str(params.get("type", ""))))
        if want == "tie" and hv and "sd_con_col" in decl:
            params["sd_con_col"] = max(2, int(params.get("sd_con_col") or 1))
        side = lp.get("tap_side") or "right"
        item["tap"] = ({"kind": "tie", "side": side, "terminal": on_bulk[0], "net": b} if want == "tie" else
                       {"kind": want, "net": b})

    # ------------------------------------------------------------- status

    def _bound(self) -> dict[str, list[dict]]:
        """The layout instances bound to each schematic instance (one, or the pieces of a device split up)."""
        out: dict[str, list[dict]] = {}
        if self.db.layout.cell(self.cell) is not None:
            for i in self.db.named_instances(self.cell):
                out.setdefault(i["name"], []).append(i)
        return out

    @staticmethod
    def _units_of(pieces: list[dict]) -> int:
        return sum(p["array"]["columns"] * p["array"]["rows"] if "array" in p else 1 for p in pieces)

    def _state(self, d: dict, pieces: list[dict]) -> tuple[str, str | None]:
        """("ok" | "stale" | "own", why) of a device's bound layout: what the schematic gives (its cell, its count of
        units), a cell or count sync puts right (one instance, or one array, of a PCell sync places), or the
        designer's own layout (_own_layout), which sync leaves as it is."""
        exp, units = self._expected(d)
        have = self._units_of(pieces)
        if exp is None or ({p["cell"] for p in pieces} == {exp} and have == units):
            return "ok", None
        if (why := self._own_layout(d, pieces)):
            return "own", why
        return "stale", None

    def status(self) -> dict:
        conn, devs = self._devices(self._stored())
        has_cell = self.db.layout.cell(self.cell) is not None
        bound = self._bound()
        names = {d["name"] for d in devs}
        for d in devs:
            pieces = bound.get(d["name"], [])
            d["placed"] = bool(pieces)
            if pieces:
                d["bbox"] = _union([p["bbox"] for p in pieces])
                d["layout_cell"] = pieces[0]["cell"]
                exp, units = self._expected(d)
                state, why = self._state(d, pieces)
                if why:
                    d["own_layout"] = why
                d["stale"] = state == "stale"
                d["expected_cell"] = exp
                d["units_placed"] = self._units_of(pieces)
                if len(pieces) > 1:
                    d["pieces"] = len(pieces)
        placed = {n: {"name": n, "bbox": _union([p["bbox"] for p in ps]), "cell": ps[0]["cell"]} for n, ps in bound.items()}
        return {
            "cell": self.cell, "layout_exists": has_cell,
            "devices": devs,
            "missing": [d["name"] for d in devs if d["kind"] and not d["placed"]],
            "stale": [d["name"] for d in devs if d.get("stale")],
            "own_layout": [d["name"] for d in devs if d.get("own_layout")],
            "unsupported": [d["name"] for d in devs if not d["kind"]],
            "notes": {d["name"]: d["note"] for d in devs if d.get("note")},
            "extra": [n for n in bound if n not in names],
            "pcells": pcell.available(self.pdk),
            "pcells_reason": pcell.unavailable_reason(self.pdk),
            "ratsnest": self.ratsnest(conn, placed) if placed else [],
            "warnings": conn.warnings,
        }

    def _stored(self) -> dict[str, dict]:
        """The layout parameters each bound device keeps (LAYOUT_PROP of its instance; one of a device's pieces)."""
        return {n: ps[0]["layout"] for n, ps in self._bound().items() if ps[0].get("layout")}

    def binding(self, name: str) -> dict | None:
        """What the layout instance bound to schematic instance `name` is to sync (None: the schematic has no `name`):
        {"expected": the cell the schematic gives (None: it cannot tell), "units": [placed, the schematic's],
        "out_of_date": sync puts that one in its place, "own_layout": why sync leaves it as it is, or None}."""
        d = next((d for d in self._devices(self._stored())[1] if d["name"] == name), None)
        if d is None:
            return None
        exp, units = self._expected(d) if d["kind"] else (None, 1)
        pieces = self._bound().get(name, [])
        state, why = self._state(d, pieces) if pieces and exp else ("ok", None)
        return {"expected": exp, "units": [self._units_of(pieces), units], "out_of_date": state == "stale", "own_layout": why}

    def _own_layout(self, d: dict, pieces: list[dict]) -> str | None:
        """Why sync leaves the bound layout of device `d` as it is, not what the schematic gives (None: sync puts that
        in its place): pieces a designer placed (place_pcell, place_array: OWN_PROP, one of them is enough), several
        pieces (a device split by hand), or a cell that is not a PCell sync places (a unit cell, a device drawn by
        hand; a subcircuit's other cell), alone or as an array. Such a layout is the designer's; LVS still compares
        the devices it holds."""
        exp, units = self._expected(d)
        have = self._units_of(pieces)
        by = sorted({p["own"] for p in pieces if p.get("own")})
        if len(pieces) > 1 or by:
            cells = sorted({p["cell"] for p in pieces})
            return (f"{len(pieces)} piece{'s' * (len(pieces) > 1)}, {have} unit{'s' * (have > 1)} of {', '.join(cells)}" +
                    (f", placed with {' and '.join(by)}" if by else "") +
                    (f"; the schematic has {units}" if have != units and not _mos_kind(d.get("pcell")) else ""))
        p = pieces[0]
        if d["kind"] != "pcell" or not DEVICE_CELL.fullmatch(p["cell"]):
            a = p.get("array")
            return f"an array of {p['cell']} ({a['columns']} x {a['rows']})" if a else f"{p['cell']}, a cell of your own"
        return None

    def _expected(self, d: dict) -> tuple[str | None, int]:
        """(the cell the schematic gives, how many of it: its units)."""
        return self._expected_cell(d), d.get("units", 1)

    def _expected_cell(self, d: dict) -> str | None:
        if d["kind"] == "pcell":
            return pcell.cell_name(d["pcell"], d["params"]) + self._suffix(d)
        if d["kind"] == "cell":
            return d.get("layout_cell_expected") or d["symbol"]
        return None

    def _suffix(self, d: dict) -> str:
        """The wrapper a device's PCell goes into: straps joining its fingers, a strap joining a butted tie to its
        source / drain (profiles with li1 and tap layers), the PCell mirrored (its tie on the left)."""
        tap = d.get("tap") or {}
        tie = tap.get("kind") == "tie"
        return ((STRAP if self._strapped(d) else "") + (TIE if tie and self._tie_layers() else "") +
                (LEFT if tie and tap.get("side") == "left" else ""))

    def _tie_layers(self) -> dict[str, int] | None:
        prof = self.pdk.profile if self.pdk else None
        if not prof or not all(k in (prof.layers or {}) for k in ("diff", "tap", "li1")):
            return None
        return {k: self.db.layout.layer(*prof.layers[k]) for k in ("diff", "tap", "li1")}

    def _strapped(self, d: dict) -> bool:
        """Whether the device goes into a cell with straps: several fingers, or a PCell without gate contacts (IHP)."""
        r = self.pdk.profile.strap
        return bool(_mos_kind(d.get("pcell")) and r and (_fingers(d) > 1 or (r.gate == "poly" and r.cont)))

    def _device_cell(self, d: dict) -> tuple[str, str | None]:
        """Generates the device's PCell into the library and, when it needs one (_suffix), the cell around it with
        Undine's straps; returns the cell to place and a warning."""
        db = self.db
        cname = pcell_variant(self.pdk, db, d["pcell"], d["params"], check=False)
        suffix = self._suffix(d)
        if not suffix:
            return cname, None
        wrapper = cname + suffix
        if db.layout.cell(wrapper) is not None:
            return wrapper, None
        warn, shapes = None, []
        if STRAP in suffix:
            rules = self.pdk.profile.strap
            L = {k: db.layout.layer(*self.pdk.profile.layers[name]) for k, name in zip(("diff", "poly", "met1", "via", "met2"), rules.layers)}
            if rules.cont:
                L["cont"] = db.layout.layer(*self.pdk.profile.layers[rules.contact_layer])
            got = _strap_shapes(db.layout, db.cell(cname), L, rules)
            if got is None:
                warn = (f"{d['name']}: its fingers are too short to strap automatically; connect their gates, sources "
                        "and drains when routing")
            shapes += got or []
        if TIE in suffix:
            got = _tie_shapes(db.layout, db.cell(cname), self._tie_layers())
            if not got:
                warn = f"{d['name']}: its PCell has no butted tap to join to the source / drain; route that yourself"
            shapes += got
        tr = kdb.Trans(2, True, 0, 0) if LEFT in suffix else kdb.Trans()      # mirrored about the y axis
        db.new_cell(wrapper)
        db._commit(wrapper, [("iins", kdb.CellInstArray(db.cell(cname).cell_index(), tr), 0)] +
                   [("ins", li, b.transformed(tr)) for li, b in shapes])
        return wrapper, warn

    # ------------------------------------------------------------ placing

    def _unit_pitch(self, cname: str) -> tuple[int, int]:
        """The step (dbu) between units of one device in an array: their cell's size plus the space the rules want
        (placement.gap; units of one device may share a well)."""
        c = self.db.cell(cname)
        f = (c.bbox(), placement.extents(self.db.layout, c, self.pdk.profile.layers or {}))
        rules, dbu = self.pdk.profile.space, self.db.layout.dbu
        return (f[0].width() + placement.gap(f, f, rules, 0, dbu, merge=True),
                f[0].height() + placement.gap(f, f, rules, 1, dbu, merge=True))

    def _footprint(self, d: dict, cname: str) -> tuple[kdb.Box, dict, tuple[int, int], tuple[int, int]]:
        """What placing device d as `cname` covers, in the cell's frame: (bbox, extents per layer, (columns, rows),
        pitch), its units as one array (columns from its layout parameters, else as square as their count allows)."""
        c = self.db.cell(cname)
        box, ext = c.bbox(), placement.extents(self.db.layout, c, self.pdk.profile.layers or {})
        n = d.get("units", 1)
        if n <= 1:
            return box, ext, (1, 1), (0, 0)
        cols = d["layout"].get("columns") or placement.factor(n)[0]
        if n % cols:
            raise ValueError(f"{d['name']}: {n} units do not fill {cols} columns; give columns that divide {n}")
        grid, pitch = (cols, n // cols), self._unit_pitch(cname)
        grow = lambda b: kdb.Box(b.left, b.bottom, b.right + (grid[0] - 1) * pitch[0], b.top + (grid[1] - 1) * pitch[1])
        return grow(box), {k: grow(v) for k, v in ext.items()}, grid, pitch

    def _place(self, d: dict, cname: str, tr: kdb.Trans) -> None:
        """Places device d as `cname` (its units as one array, its steps turned with it) at `tr` (its orientation
        too: Trans.angle is in 90 degree steps), bound to it with its layout parameters."""
        _box, _ext, grid, pitch = self._footprint(d, cname)
        self.db.place(self.cell, cname, tr.disp.x, tr.disp.y, tr.angle, tr.is_mirror(), d["name"],
                      grid[0], grid[1], pitch, layout=d["layout"] or None, turned=True)

    # ---------------------------------------------------------------- sync

    def sync(self, names: list[str] | None = None, layout: dict[str, dict] | None = None) -> dict:
        """Brings the layout devices in line with the schematic: devices whose parameters changed get the matching
        PCell in place of the old one (same position and orientation, so surrounding routing stays where it was), a
        unit count that changed a new array at the old one's origin; devices that are not placed yet are placed. A
        bound array of another cell, a cell of the designer's own or a device in several pieces is left as it is
        ("own_layout", _own_layout). `layout`: {device: layout parameters} kept with the layout instance (MOS_LAYOUT,
        UNIT_LAYOUT; a null value takes a stored one back), so the next sync keeps them. Routing is NOT redone:
        re-run DRC / LVS afterwards."""
        db = self.db
        if db.lib.readonly:
            raise PermissionError("library is read-only")
        stored = self._stored()
        conn, devs = self._devices(stored)
        known = {d["name"]: d for d in devs}
        for n, given in (layout or {}).items():
            if n not in known:
                raise ValueError(f"layout: the schematic has no instance {n}")
            d = known[n]
            kind = "mos" if _mos_kind(d.get("pcell")) else "units" if d["kind"] == "pcell" else None
            merged = {**(stored.get(n) or {}), **layout_params(n, kind, given)}
            stored[n] = {k: v for k, v in merged.items() if v is not None and not (k == "bulk" and v == "auto")}
        if layout:
            conn, devs = self._devices(stored, strict=set(layout))
        if db.layout.cell(self.cell) is None:
            db.new_cell(self.cell)
        bound = self._bound()
        out = {"updated": [], "unchanged": [], "added": [], "unsupported": [], "errors": []}
        missing = []
        for d in devs:
            if names and d["name"] not in names and d["name"] not in (layout or {}):
                continue
            if not d["kind"]:
                out["unsupported"].append(d["name"])
                continue
            pieces = bound.get(d["name"])
            if not pieces:
                missing.append(d["name"])
                continue
            state, why = self._state(d, pieces)
            asked = d["name"] in (layout or {})
            relaid = asked and (pieces[0].get("layout") or {}) != (d["layout"] or {})
            if state == "own" or (asked and self._own_layout(d, pieces)):     # in step or not, the designer's
                if state == "own":
                    out.setdefault("own_layout", []).append({"name": d["name"], "layout": why, "schematic": self._expected_cell(d)})
                else:
                    out["unchanged"].append(d["name"])
                if asked:
                    out["errors"].append({"name": d["name"], "error": "its layout is your own (own_layout): the layout "
                                          "parameters apply to a device sync places; delete it (delete_instances) first"})
                continue
            if state == "ok" and not relaid:
                out["unchanged"].append(d["name"])
                continue
            try:
                old = pieces[0]
                cname, warn = self._device_cell(d)
                if warn:
                    out.setdefault("warnings", []).append(warn)
                tr = kdb.Trans.from_s(old["trans"])
                db.delete(self.cell, [old["ref"]])
                self._place(d, cname, tr)
                nb = _union([i["bbox"] for i in db.named_instances(self.cell) if i["name"] == d["name"]])
                out["updated"].append({"name": d["name"], "from": old["cell"], "to": cname, "bbox_before": old["bbox"],
                                       "bbox_after": nb, **({"units": d["units"]} if d["units"] > 1 else {})})
            except Exception as e:
                out["errors"].append({"name": d["name"], "error": str(e)})
        if missing:
            r = self.generate(only_missing=True, names=missing, stored=stored)
            out["added"] = [c["name"] for c in r["created"] if c["name"] in missing]
            out["errors"] += r["errors"]
        if out["updated"]:                         # a device that grew in place may now sit on its neighbours
            insts = db.instances(self.cell)
            for u in out["updated"]:
                b = kdb.Box(*u["bbox_after"])
                hit = sorted({i.get("name") or i["id"] for i in insts if i.get("name") != u["name"] and b.overlaps(kdb.Box(*i["bbox"]))})
                if hit:
                    out.setdefault("warnings", []).append(f"{u['name']} now overlaps {', '.join(hit[:6])}: move one apart "
                                                          "(move_instance)")
        done = set(out["added"]) | {u["name"] for u in out["updated"]}
        if (how := taps([d for d in devs if d["name"] in done])):
            out["taps"] = how
        notes = {d["name"]: d["note"] for d in devs if d.get("note") and (not names or d["name"] in names)}
        if notes:
            out["notes"] = notes
        return out

    # ----------------------------------------------------------- generate

    def generate(self, only_missing: bool = True, names: list[str] | None = None, stored: dict | None = None) -> dict:
        """Places the devices not placed yet (all of them with only_missing False: anew), right of what the cell has,
        in rows bottom-up: nfets, pfets, the others; each at the space the PDK's rules want from its neighbours
        (placement.gap, at least placement.GAP_UM), units of a device as one array."""
        db = self.db
        if db.lib.readonly:
            raise PermissionError("library is read-only")
        conn, devs = self._devices(self._stored() if stored is None else stored)
        if db.layout.cell(self.cell) is None:
            db.new_cell(self.cell)
        bound = self._bound()
        todo = [d for d in devs if d["kind"] and (d["name"] not in bound or not only_missing) and (not names or d["name"] in names)]
        created, errors = [], []
        bb = db.cell(self.cell).bbox()
        dbu = db.layout.dbu
        base_x = bb.right + int(round(placement.GAP_UM / dbu)) if not bb.empty() else 0
        base_y = bb.bottom if not bb.empty() else 0
        gen: list[tuple[int, dict, str, tuple]] = []
        for d in todo:
            try:
                if d["kind"] == "pcell":
                    cname, warn = self._device_cell(d)
                    if warn:
                        errors.append({"name": d["name"], "error": warn})
                elif d.get("stdcell"):                  # the PDK's standard cell, its layout from the PDK's GDS
                    db.add_stdcell(d["layout_cell"])
                    cname = d["layout_cell"]
                elif not d["layout_cell"]:
                    raise KeyError(f"{d['symbol']} has no layout in {db.lib.name}: draw it (a layout cell named "
                                   f"{d['layout_cell_expected']}) or place one, then sync again")
                else:
                    cname = d["layout_cell"]
                kind = _mos_kind(d.get("pcell"))
                row = 1 if kind == "p" else 0 if kind == "n" else 2
                gen.append((row, d, cname, self._footprint(d, cname)[:2]))
            except Exception as e:
                errors.append({"name": d["name"], "error": str(e)})
        rules, floor = (self.pdk.profile.space if self.pdk else ()), placement.GAP_UM
        y, below = base_y, []
        for row in sorted({g[0] for g in gen}):
            members = [g for g in gen if g[0] == row]
            if below:                                  # clear of every device of the rows below, by the rules
                y = max(y, max(b[0].top + placement.gap(b, m[3], rules, 1, dbu, floor_um=floor) - m[3][0].bottom
                               for b in below for m in members))
            x, prev, placed_row = base_x, None, []
            for _, d, cname, fp in members:
                if prev is not None:
                    x += placement.gap(prev, fp, rules, 0, dbu, floor_um=floor)
                try:
                    if d["name"] in bound:
                        db.delete(self.cell, [p["ref"] for p in bound[d["name"]]])
                    self._place(d, cname, kdb.Trans(x - fp[0].left, y - fp[0].bottom))
                    created.append({"name": d["name"], "cell": cname})
                except Exception as e:
                    errors.append({"name": d["name"], "error": str(e)})
                dx, dy = x - fp[0].left, y - fp[0].bottom
                moved = (fp[0].moved(dx, dy), {k: v.moved(dx, dy) for k, v in fp[1].items()})
                placed_row.append(moved)
                x, prev = moved[0].right, moved
            below += placed_row
        return {"created": created, "errors": errors, "status": self.status()}

    # ---------------------------------------------------------- unit arrays

    def place_array(self, devices: list[str] | dict[str, int], pattern: str = "common_centroid",
                    rows: list | None = None, columns: int | None = None, pitch: tuple[float, float] | None = None,
                    origin: tuple[float, float] = (0.0, 0.0), dummies: int = 0, dummy: str | None = None,
                    replace: bool = False) -> dict:
        """The units of several devices in one grid, each unit an instance bound to its device: MIM capacitors and
        resistors by their MF x m (a count of their own would change the device), MOSFETs as one-finger devices of
        W x mult / units each (units: their layout nf, else nf x mult, or the count given), without a tap of their own.
        pattern common_centroid (placement.common_centroid), interdigitated, or rows (`rows`, top row first, each a
        list of device names, "dummy" or "" for a free cell). `dummies` rings of dummy units around it (unbound, or
        bound to schematic instance `dummy`, a device of their own with exactly that many units). pitch (um) by
        default from the PDK's rules (placement.gap), `origin` the lower-left corner of the array (um). A device
        already placed is refused unless `replace`: then its instances go first. Nothing is generated before every
        check passed. The units carry OWN_PROP: sync leaves them as they are. Returns the unit map and what it means
        for each device."""
        db, dbu = self.db, self.db.layout.dbu
        if db.lib.readonly:
            raise PermissionError("library is read-only")
        _conn, devs = self._devices(self._stored())
        known = {d["name"]: d for d in devs}
        want = dict(devices) if isinstance(devices, dict) else {n: None for n in devices}
        if pattern == "rows":
            for row in rows or []:
                for t in (row if isinstance(row, list) else list(row)):
                    if t and t != "dummy" and t not in want:
                        want[t] = None
        if not want:
            raise ValueError("devices is empty: name the schematic instances whose units go into the array")
        counts, units, bulk, kinds = {}, {}, set(), set()
        for n, given in want.items():
            d = known.get(n)
            if d is None:
                raise ValueError(f"the schematic {self.cell} has no instance {n}")
            if d["kind"] != "pcell":
                raise ValueError(f"{n} is no PCell device ({d.get('note') or d['symbol']}): it has no units")
            counts[n], units[n] = self._unit(d, given)
            kinds.add(_mos_kind(d["pcell"]))
            if _mos_kind(d["pcell"]):
                bulk.add({k.upper(): v for k, v in d["nets"].items()}.get("B"))
        if len(kinds) > 1:
            raise ValueError("an array holds one kind of device: nfets, pfets or passives (their wells and taps differ)")
        mos = kinds.pop()
        total = sum(counts.values())
        if pattern == "rows":
            grid = [[(t or None) for t in (row if isinstance(row, list) else list(row))] for row in reversed(rows or [])]
            width = max((len(r) for r in grid), default=0)
            grid = [r + [None] * (width - len(r)) for r in grid]
            got = {n: sum(r.count(n) for r in grid) for n in counts}
            if got != counts:
                raise ValueError("rows hold " + ", ".join(f"{n} x {got[n]}" for n in counts) + "; the devices have " +
                                 ", ".join(f"{n} x {counts[n]}" for n in counts))
        else:
            if columns:
                cols = int(columns)
                nrows = -(-total // cols)
                if pattern == "common_centroid" and (cols * nrows - total) % 2:
                    nrows += 1
            elif pattern == "interdigitated":                    # one row: ABAB ...
                cols, nrows = total, 1
            else:
                cols, nrows = placement.grid_for(total)
            if pattern == "common_centroid":
                grid = placement.common_centroid(counts, cols, nrows)
            elif pattern == "interdigitated":
                grid = placement.interdigitated(counts, cols, nrows)
            else:
                raise ValueError(f"pattern is common_centroid, interdigitated or rows, not {pattern!r}")
        if dummies:
            w = len(grid[0]) + 2 * dummies
            grid = ([["dummy"] * w for _ in range(dummies)] + [["dummy"] * dummies + r + ["dummy"] * dummies for r in grid] +
                    [["dummy"] * w for _ in range(dummies)])
        if dummy is not None and dummy not in known:
            raise ValueError(f"dummy: the schematic {self.cell} has no instance {dummy}")
        ndum = sum(r.count("dummy") for r in grid)
        dunit = next(iter(units.values()))
        if dummy and ndum:                     # bound to a device: as many units as it has, or LVS counts them wrong
            dd = known[dummy]
            if dummy in counts:
                raise ValueError(f"{dummy} is in the array already, all {counts[dummy]} of its units: the {ndum} dummy "
                                 "units would be more of it. Bind them to a device of their own (dummy) or leave them unbound")
            if dd["kind"] != "pcell" or _mos_kind(dd["pcell"]) != mos:
                raise ValueError(f"dummy {dummy} is no {'MOSFET' if mos else 'passive'} PCell device like the array's units")
            nd, dunit = self._unit(dd, None)
            if nd != ndum:
                raise ValueError(f"dummy {dummy} has {nd} unit{'s' * (nd > 1)} ({'its fingers' if mos else 'MF x m'}) and "
                                 f"the array {ndum} dummy units: LVS would compare {ndum} devices with {nd}. Set its "
                                 f"{'nf' if mos else 'MF'} to {ndum} in the schematic, or leave dummy out (unbound dummies, "
                                 "their terminals tied together)")
        bound = self._bound()
        placed = sorted(n for n in set(counts) | ({dummy} if dummy else set()) if n in bound)
        if placed and not replace:
            raise ValueError(f"{', '.join(placed)} {'is' if len(placed) == 1 else 'are'} placed already: delete "
                             f"{'it' if len(placed) == 1 else 'them'} (delete_instances) or pass replace: true")
        # the unit cells once nothing refuses the array (a variant the library has is not generated again)
        cells = {n: pcell_variant(self.pdk, db, *u, check=False) for n, u in units.items()}
        dcell = pcell_variant(self.pdk, db, *dunit, check=False)
        used = set(cells.values()) | ({dcell} if ndum else set())
        prof = self.pdk.profile
        frames = {c: (db.cell(c).bbox(), placement.extents(db.layout, db.cell(c), prof.layers or {})) for c in used}
        fill = tuple(prof.fill) if mos and len(bulk) <= 1 else ()          # one well / implant over units of one bulk net
        rules, merge = prof.space, len(bulk) <= 1
        rule_pitch = (max(frames[a][0].width() + placement.gap(frames[a], frames[b], rules, 0, dbu, merge, skip=fill)
                          for a in used for b in used),
                      max(frames[a][0].height() + placement.gap(frames[a], frames[b], rules, 1, dbu, merge, skip=fill)
                          for a in used for b in used))
        step = tuple(int(round(v / dbu)) for v in pitch) if pitch else rule_pitch
        warnings = []
        if step[0] < rule_pitch[0] or step[1] < rule_pitch[1]:
            warnings.append(f"pitch {[round(v * dbu, 3) for v in step]} um is below the rules' "
                            f"{[round(v * dbu, 3) for v in rule_pitch]} um: DRC will find units too close")
        if db.layout.cell(self.cell) is None:
            db.new_cell(self.cell)
        if placed:
            db.delete(self.cell, [p["ref"] for n in placed for p in bound[n]])
        ox, oy = (int(round(v / dbu)) for v in origin)
        items = []
        for r, row in enumerate(grid):
            for c, t in enumerate(row):
                if t is None:
                    continue
                cell = dcell if t == "dummy" else cells[t]
                b = frames[cell][0]
                items.append((cell, kdb.Trans(ox + c * step[0] - b.left, oy + r * step[1] - b.bottom),
                              (dummy if t == "dummy" else t)))
        boxes, ring = self._fills(items, frames, fill, mos)
        db.place_many(self.cell, items, boxes, own="place_array")
        legend = {n: chr(ord("A") + i) if i < 26 else chr(ord("a") + i - 26) for i, n in enumerate(counts)}
        sym = {**legend, "dummy": ".", None: "-"}
        k = lambda v: round(v * dbu, 3)
        out = {"columns": len(grid[0]), "rows": len(grid), "pitch_um": [k(step[0]), k(step[1])],
               "rule_pitch_um": [k(rule_pitch[0]), k(rule_pitch[1])], "origin_um": [k(ox), k(oy)],
               "bbox_um": [k(ox), k(oy), k(ox + (len(grid[0]) - 1) * step[0] + max(frames[c][0].width() for c in used)),
                           k(oy + (len(grid) - 1) * step[1] + max(frames[c][0].height() for c in used))],
               "legend": {v: n for n, v in legend.items()} | ({".": f"dummy ({dummy})" if dummy else "dummy (unbound)"} if ndum else {})
               | ({"-": "free"} if any(None in r for r in grid) else {}),
               "map": ["".join(sym[t] for t in row) for row in reversed(grid)],
               "map_note": "top row first; the unit in column c, row r (from the bottom left, 0-based) has its cell's "
                           "lower-left corner at origin + [c, r] x pitch, and its id is cell@x,y with x, y that plus "
                           "unit_origin_um[cell] (where the cell's origin is from its lower-left corner)",
               "unit_origin_um": {c: [k(-frames[c][0].left), k(-frames[c][0].bottom)] for c in sorted(used)},
               "devices": {n: {"units": counts[n], "cell": cells[n], "centroid_offset_um": off}
                           for n, off in placement.centroid_error([[t if t != "dummy" else None for t in r] for r in grid],
                                                                  (k(step[0]), k(step[1]))).items()}}
        if ndum:
            out["dummies"] = ndum
            if not dummy:
                warnings.append(f"LVS sees the {ndum} dummy units as devices: give the schematic a device for them (as "
                                "many units, e.g. MF) and pass dummy: its name, or tie each dummy's terminals together")
        if ring:
            out["guard_ring"] = {"bbox_um": [k(ring.left), k(ring.bottom), k(ring.right), k(ring.top)],
                                 "note": f"the units' tap, drawn in the cell: route its {self.pdk.profile.ring.conductor} "
                                         f"to {', '.join(sorted(map(str, bulk)))}"}
        elif mos:
            warnings.append(f"the MOSFET units have no tap of their own: put taps or a guard ring around the array, on "
                            f"{', '.join(sorted(map(str, bulk)))}")
        if warnings:
            out["warnings"] = warnings
        return out

    def _fills(self, items: list, frames: dict, fill: tuple[str, ...], mos: str | None) -> tuple[list, kdb.Box | None]:
        """What an array of MOSFET units shares, as boxes ((layer, datatype), box) of the cell's own: one box per
        fill layer over all the units' own (their wells, implants, marks: the gaps between those would be too
        narrow), and the profile's guard ring around them (_ring), in the well for pfets, under the thick-oxide mark
        for thick-oxide units; with the ring's outline. Nothing for passives."""
        prof, dbu = self.pdk.profile, self.db.layout.dbu
        if not mos:
            return [], None
        boxes: dict[str, kdb.Box] = {}
        whole = kdb.Box()
        for cell, tr, _ in items:
            whole += frames[cell][0].transformed(tr)
            for layer, b in frames[cell][1].items():
                if layer in fill:
                    boxes[layer] = boxes.get(layer, kdb.Box()) + b.transformed(tr)
        out, outline = [], None
        if (r := prof.ring) is not None and prof.stack is not None and prof.stack.taps is not None:
            um = lambda v: int(round(v / dbu))
            inner = whole.enlarged(um(r.space), um(r.space))
            outline = inner.enlarged(um(r.width), um(r.width))
            out = _ring(prof, inner, mos, dbu)
            if mos == "p":                              # the n-tap ring in the well too
                w = um(r.well[1 if "hvi" in boxes else 0])
                boxes["nwell"] = boxes.get("nwell", kdb.Box()) + outline.enlarged(w, w)
            if "hvi" in boxes:                          # thick oxide: the ring (and its well) inside the mark too, an
                boxes["hvi"] += boxes["nwell"] if mos == "p" else outline       # HV n-well wholly (nwell.9, diff/tap.21)
        return [(prof.layers[k], b) for k, b in boxes.items()] + out, outline

    def _unit(self, d: dict, given: int | None) -> tuple[int, tuple[str, dict]]:
        """(units, the unit's PCell and parameters) of device d in an array: a MIM / resistor's own unit and count
        (MF x m), a MOSFET's fingers (its layout nf, else nf x mult; or `given`) as one-finger PCells of W x mult /
        units without a tap."""
        if not _mos_kind(d["pcell"]):
            if given is not None and int(given) != d["units"]:
                raise ValueError(f"{d['name']} has {d['units']} units (MF x m): its units are devices, so another count "
                                 "would change it; set MF / m in the schematic")
            return d["units"], (d["pcell"], d["params"])
        params = dict(d["params"])
        per = "w" if "w" in params else "w_gate" if "w_gate" in params else None
        if per is None:
            raise ValueError(f"{d['name']}: its PCell ({d['pcell']}) cannot be split into units here")
        total = round(params[per] * _fingers(d), 6)
        n = int(given or _fingers(d))
        w = total / n
        if n < 1 or abs(w * 200 - round(w * 200)) > 1e-6:
            raise ValueError(f"{d['name']}: W {total:g} um in {n} units is {w:.4g} um a unit, off the 5 nm grid: "
                             "choose a count that divides W into multiples of 0.005")
        params[per] = round(w, 4)
        params.pop("nf", None)
        if "bulk" in params:
            cat = {p["name"]: p for p in self._catalog().get(d["pcell"], {}).get("params", [])}
            params["bulk"] = _choice(cat, "bulk", "none") or "None"
        return n, (d["pcell"], params)

    # ------------------------------------------------------------ ratsnest

    def ratsnest(self, conn: schematic.Connectivity, placed: dict[str, dict]) -> list[dict]:
        """Minimum spanning tree per net between the placed instances' centres (dbu)."""
        out = []
        for net, pins in conn.nets.items():
            insts = sorted({i for i, _ in pins if i in placed})
            if len(insts) < 2:
                continue
            ctr = {}
            for i in insts:
                b = placed[i]["bbox"]
                ctr[i] = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
            # Prim
            inside = {insts[0]}
            while len(inside) < len(insts):
                best = None
                for a, b in itertools.product(inside, [x for x in insts if x not in inside]):
                    dd = math.dist(ctr[a], ctr[b])
                    if best is None or dd < best[0]:
                        best = (dd, a, b)
                _, a, b = best
                out.append({"net": net, "a": a, "b": b, "p": [*ctr[a], *ctr[b]]})
                inside.add(b)
        return out

    # ----------------------------------------------------------- crossprobe

    def probe(self, inst: str | None = None, net: str | None = None) -> dict:
        """Translates a selection (schematic instance or net) into both views."""
        conn = schematic.extract(self.sch_path, self.resolver, self.cell)
        placed = {n: ps[0] for n, ps in self._bound().items()}
        sch_insts, nets = [], []
        if inst:
            d = next((d for d in conn.devices if d.name == inst), None)
            if d:
                sch_insts.append(d.name)
                nets = sorted(set(d.nets.values()))
            names = [inst]
        else:
            nets = [net] if net else []
            names = sorted({i for n in nets for i, _ in conn.nets.get(n, [])})
            sch_insts = [d.name for d in conn.devices if d.name in names]
        return {
            "insts": names, "sch_insts": sch_insts, "nets": nets,
            "wires": [w for w, n in zip(conn.wire_ids, conn.wire_net) if n in nets],
            "layout": [placed[n] for n in names if n in placed],
        }
