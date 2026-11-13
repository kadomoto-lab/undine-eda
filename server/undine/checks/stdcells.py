"""The PDK's standard cells in KLayout LVS: a layout built from them (a LibreLane macro imported, cells placed by
hand) checked against a schematic of them (Profile.stdcell_lvs; docs/guide/verification.md, "Standard cells").

The reference: the schematic's netlist calls the cells (`xg1 a b vss vss vdd vdd y sky130_fd_sc_hd__nand2_1`) without
defining them. Each cell it calls is added from the library's SPICE file, its transistors as M elements the deck
compares with the layout's (the model renamed and W / L scaled as the profile says). A resistor of a model the deck
does not extract (sky130's res_generic_po in conb_1) is a short in the layout: its two nets are one in the reference
too. A cell made of such resistors alone is a tie cell (conb_1: HI joined to VPWR, LO to VGND): it leaves the
reference, and in the circuit that places it the nets on the joined pins become one, named after the one with the
most connections (the supply): the schematic's hi9 is vdd in the reference, as it is in the layout. A tie cell that
joins two ports (HI on an output port y, VPWR on vdd) is refused: the layout has one net there, LVS two pins. The cells
a place-and-route run adds for the layout alone (fill, tap, decap, antenna diode: `physical`) leave the reference too,
those the schematic draws as well: the layout's lose their devices (below), and a run adds more of the same cells. A
cell the PDK has no SPICE of, or one with a device the deck does not compare (a diode in a logic cell), is refused
with its name, never left as a call of an undefined subcircuit.

The layout: a copy of it, in which the labels the profile names (`drop_labels`, sky130's pwell VNB) are taken out of
the standard cells, and the cells a place-and-route run adds for the layout alone (fill, tap, decap, antenna diode:
`physical`) lose their devices (`strip` layers: diffusion and poly) and keep their wells, taps and rails, so the
n-wells of a row still reach their taps and nothing the schematic does not have is compared.
"""
from __future__ import annotations

import re
from pathlib import Path

import klayout.db as kdb

_DEFS: dict[tuple[str, float], dict[str, tuple[list[str], list[str]]]] = {}


def _lines(text: str) -> list[str]:
    """SPICE lines with their continuations (+) joined."""
    out: list[str] = []
    for raw in text.splitlines():
        if raw.startswith("+") and out:
            out[-1] += " " + raw[1:].strip()
        else:
            out.append(raw)
    return out


def definitions(path: Path) -> dict[str, tuple[list[str], list[str]]]:
    """{cell (lower case): (pins, element lines)} of a SPICE library file, read once per version of the file."""
    key = (str(path), path.stat().st_mtime)
    if key not in _DEFS:
        out: dict[str, tuple[list[str], list[str]]] = {}
        cur = None
        for ln in _lines(path.read_text(errors="replace")):
            t = ln.split()
            if not t or t[0].startswith("*"):
                continue
            if t[0].lower() == ".subckt" and len(t) > 1:
                cur = (t[1], t[2:], [])
            elif t[0].lower() == ".ends" and cur is not None:
                out.setdefault(cur[0].lower(), (cur[1], cur[2]))
                cur = None
            elif cur is not None:
                cur[2].append(ln.strip())
        if len(_DEFS) > 8:
            _DEFS.clear()
        _DEFS[key] = out
    return _DEFS[key]


def _split(line: str) -> tuple[str, list[str], str, dict[str, str]]:
    """(name, nodes, model, {param: value}) of an element call `Xn a b c d model k=v ...`."""
    t = line.split()
    i = next((k for k, x in enumerate(t) if "=" in x), len(t))
    params = dict(x.split("=", 1) for x in t[i:])
    return t[0], t[1:i - 1], t[i - 1], {k.lower(): v for k, v in params.items()}


def _num(v: str) -> float | None:
    m = re.fullmatch(r"([-+0-9.eE]+)([a-zA-Z]*)", v.strip())
    if not m:
        return None
    scale = {"": 1, "f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "meg": 1e6}.get(m.group(2).lower())
    try:
        return float(m.group(1)) * scale if scale is not None else None
    except ValueError:
        return None


class _Union:
    def __init__(self):
        self.up: dict[str, str] = {}

    def find(self, x: str) -> str:
        while self.up.get(x, x) != x:
            x = self.up[x]
        return x

    def join(self, a: str, b: str, keep) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            win, lose = (ra, rb) if keep(ra, rb) else (rb, ra)
            self.up[lose] = win


def _renamed(line: str, u: _Union) -> str:
    """An element line with its nodes renamed to their joined nets (parameters and the model are no net)."""
    t = line.split()
    return " ".join([t[0]] + [u.find(n) if "=" not in n and n in u.up else n for n in t[1:]])


def _cell(name: str, pins: list[str], body: list[str], rules) -> tuple[list[str] | None, list[tuple[str, str]]]:
    """A cell's definition for the reference (None: a tie cell) and the pairs of pins its shorts join. ValueError for
    a call the deck cannot compare (a subcircuit that is no transistor and no short: a diode, a cell inside a cell)."""
    u = _Union()
    elements, pairs = [], []
    for ln in body:
        try:
            el, nodes, model, params = _split(ln)
        except ValueError:
            elements.append(ln)
            continue
        if re.fullmatch(rules.short, model, re.I) and len(nodes) == 2:
            pairs.append((nodes[0], nodes[1]))
            continue
        if el[:1] in "xX" and re.fullmatch(rules.mos, model, re.I) and len(nodes) == 4:
            for pat, rep in rules.rename:
                model = re.sub(pat, rep, model)
            w, l = _num(params.get("w", "")), _num(params.get("l", ""))
            if w is None or l is None:
                elements.append(ln)
                continue
            m = params.get("m") or params.get("mult")
            elements.append(f"M{el[1:]} {' '.join(nodes)} {model} W={w * rules.scale * 1e6:.6g} L={l * rules.scale * 1e6:.6g}"
                            + (f" m={m}" if m else ""))
        elif el[:1] in "xX":
            raise ValueError(f"{name}: LVS cannot read its {el} ({model}), which is no transistor; a schematic with "
                             f"{name} is not compared with KLayout")
        else:
            elements.append(ln)
    if not elements:
        return None, pairs
    for a, b in pairs:                                  # inside a cell: an inner net takes the pin's name
        u.join(a, b, lambda x, y: x in pins or y not in pins)
    if u.up:
        elements = [_renamed(e, u) for e in elements]
    return [f".subckt {name} {' '.join(pins)}", *elements, ".ends"], [p for p in pairs if p[0] in pins and p[1] in pins]


def _definition(pdk, name: str, lib: str) -> tuple[tuple[list[str], list[str]] | None, list[Path]]:
    """(pins, element lines) of standard cell `name` of library `lib` from the PDK's SPICE (Profile.stdcell_spice:
    the library's file, else the cell's own), and the files looked in."""
    looked = []
    for rel in pdk.profile.stdcell_spice:
        f = pdk.root / rel.format(lib=lib, cell=name)
        if f in looked:
            continue
        looked.append(f)
        if f.is_file() and (d := definitions(f).get(name.lower())) is not None:
            return d, [f]
    return None, looked


def reference(text: str, pdk) -> tuple[str, dict]:
    """The reference netlist `text` with the standard cells it calls and does not define (module docstring), and what
    was done: {"cells": [cell], "ties": [instance], "joined": {net: the net it joined}, "physical": [instance],
    "libraries": [file]}. Unchanged (and {}) without Profile.stdcell_lvs or without a call of a standard cell.
    ValueError when a cell it calls cannot be read: no SPICE of it in the PDK, a device the deck does not compare, a
    tie cell between two ports (module docstring)."""
    rules = pdk.profile.stdcell_lvs if pdk is not None else None
    if rules is None:
        return text, {}
    lines = _lines(text)
    defined = {ln.split()[1].lower() for ln in lines if ln.strip().lower().startswith(".subckt") and len(ln.split()) > 1}
    calls: dict[str, tuple[str, str]] = {}              # lower name -> (name, its library)
    for ln in lines:
        t = ln.split()
        if not t or t[0][:1] not in "xX":
            continue
        args = [x for x in t[1:] if "=" not in x]
        name = args[-1] if args else ""
        if name.lower() in defined or name.lower() in calls or not (hit := pdk.profile.stdcell(name)):
            continue
        calls[name.lower()] = (name, hit[0])
    if not calls:
        return text, {}
    phys = re.compile(rules.physical, re.I)
    physical = {low for low, (name, _) in calls.items() if phys.search(name)}   # left out, as their devices are
    cells, ties, files, missing = [], {}, [], []
    for low, (name, lib) in sorted(calls.items()):
        if low in physical:
            continue
        d, where = _definition(pdk, name, lib)
        if d is None:
            missing.append(f"{name} (looked in {', '.join(f.name for f in where)})")
            continue
        body, pairs = _cell(name, d[0], d[1], rules)
        if body is None:
            ties[low] = (d[0], pairs)
        else:
            cells.append(body)
        if str(where[0]) not in files:
            files.append(str(where[0]))
    if missing:
        raise ValueError("the PDK has no SPICE of the standard cells " + "; ".join(missing))
    # tie cells: out of the reference, their joined nets one net in the circuit that places them; physical cells out
    out: list[str] = []
    joined: dict[str, str] = {}
    tie_insts: list[str] = []
    phys_insts: list[str] = []
    block: list[str] = []

    def flush(block: list[str]) -> None:
        if not block:
            return
        head = block[0].split() if block[0].strip().lower().startswith(".subckt") else []
        ports = set(head[2:]) if head else set()
        uses: dict[str, int] = {}
        for ln in block:
            t = ln.split()
            if t and t[0][:1].isalpha() and not t[0].startswith("."):
                for n in t[1:]:
                    if "=" not in n:
                        uses[n] = uses.get(n, 0) + 1
        u = _Union()
        keep, tied = [], []
        for ln in block:
            t = ln.split()
            args = [x for x in t[1:] if "=" not in x] if t and t[0][:1] in "xX" else []
            if args and args[-1].lower() in physical:
                phys_insts.append(t[0])
                continue
            if args and args[-1].lower() in ties:
                pins, pairs = ties[args[-1].lower()]
                nets = dict(zip(pins, args[:-1]))
                for a, b in pairs:
                    if a in nets and b in nets:
                        u.join(nets[a], nets[b], lambda x, y: (x in ports, uses.get(x, 0)) >= (y in ports, uses.get(y, 0)))
                tied.append(t[0])
                continue
            keep.append(ln)
        classes: dict[str, list[str]] = {}
        for n in ports:
            classes.setdefault(u.find(n), []).append(n)
        both = [sorted(c) for c in classes.values() if len(c) > 1]
        if both:
            # the layout's poly joins them into one net, which LVS cannot compare with two pins of the schematic
            raise ValueError(f"tie cell {', '.join(tied)} of {head[1] if head else 'the netlist'} joins the ports "
                             + "; ".join(" and ".join(c) for c in both) + ": in the layout they are one net, which LVS "
                             "cannot compare with two ports; give the constant port its own driver (a buffer from the "
                             "tie cell's net) or take the port out")
        tie_insts.extend(tied)
        for n in list(u.up):
            if u.find(n) != n:
                joined[n] = u.find(n)
        for ln in keep:
            t = ln.split()
            out.append(_renamed(ln, u) if u.up and t and t[0][:1].isalpha() else ln)

    for ln in lines:
        s = ln.strip().lower()
        if s.startswith(".subckt"):
            flush(block)
            block = [ln]
        elif s.startswith(".ends"):
            block.append(ln)
            flush(block)
            block = []
        else:
            block.append(ln)
    flush(block)
    text = "\n".join(out).rstrip("\n") + "\n" + "".join("\n".join(c) + "\n" for c in cells)
    return text, {"cells": sorted(calls[low][0] for low in calls if low not in ties and low not in physical),
                  "ties": tie_insts, "joined": joined, "physical": phys_insts, "libraries": files,
                  "simplify": rules.simplify}


def has_stdcells(layout: kdb.Layout, cell: str, pdk) -> bool:
    c = layout.cell(cell)
    if c is None or pdk is None or not pdk.profile.stdcells:
        return False
    return any(pdk.profile.stdcell(layout.cell(i).name) for i in c.each_child_cell()) or \
        any(pdk.profile.stdcell(layout.cell(i).name) for i in c.called_cells())


def layout_for_lvs(layout: kdb.Layout, cell: str, pdk) -> kdb.Layout | None:
    """A copy of the layout for LVS with the standard cells read as the module docstring says; None when there is
    nothing to change (no Profile.stdcell_lvs, no standard cell under `cell`)."""
    rules = pdk.profile.stdcell_lvs if pdk is not None else None
    if rules is None or not has_stdcells(layout, cell, pdk):
        return None
    ly = layout.dup()
    layers = pdk.profile.layers
    find = lambda name: ly.find_layer(*layers[name]) if name in layers else None      # noqa: E731
    drop = [li for li in (find(n) for n in rules.drop_labels) if li is not None]
    strip = [li for li in (find(n) for n in rules.strip) if li is not None]
    phys = re.compile(rules.physical, re.I)
    for c in ly.each_cell():
        if not pdk.profile.stdcell(c.name):
            continue
        for li in drop:
            for sh in [x for x in c.shapes(li).each() if x.is_text()]:
                c.shapes(li).erase(sh)
        if phys.search(c.name):
            for li in strip:
                c.shapes(li).clear()
    return ly
