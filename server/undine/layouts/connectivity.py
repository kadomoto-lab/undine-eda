"""What a layout connects, read with KLayout, in two ways.

The nets of a layout as the PDK's stack defines it (pdkprofile.Stack, class Nets), extracted in place: which shapes
are one net, which labels name it, and the terminals of a device cell (S / G / D / B of a MOSFET, the plates of a MIM
capacitor) with their shapes. The drawing tools (agent_tools: add_via, route, add_tap, guard_ring) check with it that
what they draw touches no other net, render_layout highlights nets with it, and get_device_geometry names the
terminals of a placed device. It is not LVS: devices are recognised only to find their terminals, and nothing is
compared with the schematic. A net is named by its labels and by the device terminals on it: the schematic net of each
terminal of a bound device (XMS:S on vin names the net vin), so an unlabelled net that only joins devices still has a
name.

A MOSFET's source and drain are interchangeable (LVS swaps them). The extraction names the two strips arbitrarily, so
they are named after the schematic: the strip already routed to the schematic's drain net (a label, another device's
terminal on it) is D. Unrouted, the strip butted against the PCell's body tap is the one on the bulk's net (an nMOS's S
on vss), so that tying it to the tap leaves no junction of another net against the tap; else the PCell's order holds.

The wiring check (check: get_layout_info's unrouted_nets and wiring) compares the drawn wiring with what the schematic
wants connected. The cell is extracted flat, so every element of a unit array is probed (a CDAC's 1024 units), with
its conductors joined the way the PDK's LVS deck joins them (sky130: diffusion cut by the gates, a resistor's body cut
by its marker, li1 / metal through the contacts and vias, a MIM's top plate through the via3 on its capm), and labels
name the nets. The substrate and the n-wells are no wires here: taps are joined only by what is drawn between them
(the deck joins every p-tap to its substrate net and every n-tap to its well, so LVS would not miss a vss rail, nor
the metal between two taps of one well): substrate_only / well_only name the nets only they would join, and two nets
whose taps share a well are a short. A body without a tap of its own (bulk none) is tied by a tap of its bulk net in
the same substrate region (or n-well) within the latch-up distance of its diffusion (TapRule.latchup: body_ties). Each placed device's
terminals are found in its cell (a MOSFET's sources / drains beside its gates, its gate, its tap; a MIM's plates; a
resistor's two ends; a cell's pin labels) and probed: per schematic net, the layout nets its device terminals (and a
port's label, where the net leaves the cell) are on. A net whose terminals are on more than one layout net is not
routed yet (substrate_only / well_only: the substrate or a well alone would join its pieces); a layout
net with the terminals of more than one schematic net is a short; a tap butted against a source / drain on another net
is a short in silicon that LVS does not see (the silicide over the junction). A source and drain (a resistor's two
ends) are interchangeable: each device takes the way round that agrees with the rest. It does not extract devices (a
device drawn by hand outside a bound instance is not seen), and only the profiles with a recipe here (sky130) have it;
Nets covers every PDK with a stack.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

import klayout.db as kdb

from undine.pdks.pdkprofile import Profile, Stack

MOS_PINS = ("S", "G", "D", "B")


def stack(profile: Profile | None) -> Stack | None:
    return profile.stack if profile is not None and profile.stack is not None else None


def inside(poly: kdb.Polygon) -> kdb.Point:
    """A point inside a polygon, away from its edges: the centre of its largest trapezoid."""
    parts = poly.decompose_trapezoids() if poly.num_points() > 4 else []
    return max(parts or [poly], key=lambda p: p.area()).bbox().center()


def boxes(region: kdb.Region) -> list[kdb.Box]:
    """A region as rectangles (exact for Manhattan shapes: each merged polygon split into its fewest stripes)."""
    out = []
    for p in region.merged().each():
        if p.is_box():
            out.append(p.bbox())
        else:
            out += [t.bbox() for t in p.decompose_trapezoids(kdb.Polygon.TD_htrapezoids)]
    return out


def _layer(layout: kdb.Layout, profile: Profile, name: str) -> int | None:
    ld = profile.layers.get(name)
    return layout.find_layer(*ld) if ld else None


# ------------------------------------------------------------------------------------------ the terminals of a device


def device_terminals(layout: kdb.Layout, cell: kdb.Cell, profile: Profile) -> dict:
    """The device a cell holds and its terminals, extracted flat (a PCell variant: a millisecond): {"kind": "mos" |
    "mim", "class": "nmos" | "pmos" | "mim", "params": {W, L ...} or {A, P}, "terminals": {name: {layer: Region}}} in
    the cell's coordinates. MOSFET terminals are S / G / D / B (fingers joined in parallel are one device), a MIM
    capacitor's its symbol's pins (sky130: c0 the top plate, c1 the bottom one). A multi-finger MOSFET PCell whose
    fingers are not joined yet (a variant placed as it is, nf > 1: its meta information says so) is one device too,
    "fingers": n: its sources are the strips on
    one side of each gate taken in turn (the outer ones of two fingers), its drains the others, each terminal the
    strips of all its fingers (not one net until routed), W the fingers' sum. {"kind": None, "devices": n} when the
    cell holds none or more than one (a standard cell, a block), or the PDK has no stack."""
    st = stack(profile)
    if st is None:
        return {"kind": None, "devices": 0}
    l2n = kdb.LayoutToNetlist(cell.name, layout.dbu)
    R: dict[str, kdb.Region] = {}
    names = {st.diff, st.poly, st.well, *([st.tap] if st.tap else []), *(c.name for c in st.conductors),
             *(c.name for c in st.cuts), *(p.name for p in st.plates), *(x for p in st.plates for x in (p.cut, *p.also))}
    for n in sorted(names):
        li = _layer(layout, profile, n)
        R[n] = kdb.Region(cell.begin_shapes_rec(li)) if li is not None else kdb.Region()
        l2n.register(R[n], n)
    sd = R[st.diff] - R[st.poly]
    gate = R[st.diff] & R[st.poly]
    ngate, pgate = gate - R[st.well], gate & R[st.well]
    sub = kdb.Region(cell.bbox().enlarged(1000, 1000)) - R[st.well]
    for r, n in ((sd, "$sd"), (ngate, "$ngate"), (pgate, "$pgate"), (sub, "$sub")):
        l2n.register(r, n)
    l2n.extract_devices(kdb.DeviceExtractorMOS4Transistor("nmos"), {"SD": sd, "G": ngate, "P": R[st.poly], "W": sub})
    l2n.extract_devices(kdb.DeviceExtractorMOS4Transistor("pmos"), {"SD": sd, "G": pgate, "P": R[st.poly], "W": R[st.well]})
    for p in st.plates:
        l2n.extract_devices(kdb.DeviceExtractorCapacitor(f"mim:{p.name}", 1e-15), {"P1": R[p.metal], "P2": R[p.name]})
    conductors = _connect(l2n, st, R, sd)
    if st.tap:
        l2n.connect(R[st.tap], R[st.well])
        l2n.connect(R[st.tap], sub)
    l2n.connect(R[st.well])
    l2n.connect(sub)
    l2n.extract_netlist()
    nl = l2n.netlist()
    nl.combine_devices()
    c = nl.circuit_by_name(cell.name)
    devs = list(c.each_device()) if c is not None else []
    if not devs:
        return {"kind": None, "devices": 0}
    d = devs[0]
    dc = d.device_class()
    plate = next((p for p in st.plates if dc.name == f"mim:{p.name}"), None) if len(devs) == 1 else None
    kind = "mim" if plate else "mos"
    extra: dict = {}
    if len(devs) == 1:
        rename = {"A": plate.bottom, "B": plate.top} if plate else {}         # P1 the metal under it, P2 the plate
        nets_of = {rename.get(t.name, t.name): [d.net_for_terminal(t.id())] for t in dc.terminal_definitions()}
        params = {p.name: round(d.parameter(p.id()), 6) for p in dc.parameter_definitions()
                  if p.name in (("W", "L") if kind == "mos" else ("A", "P"))}
    else:
        got = _fingers(devs, lambda net: l2n.shapes_of_net(net, sd, True)) if _pcell_fingers(layout, cell) == len(devs) else None
        if got is None:
            return {"kind": None, "devices": len(devs)}
        nets_of, params = got
        extra = {"fingers": len(devs)}
    shown = [(n, r) for n, r in conductors] + [(st.well, R[st.well])] + [(c.name, R[c.name]) for c in st.cuts]
    terms: dict[str, dict[str, kdb.Region]] = {}
    for t, nets in nets_of.items():
        layers: dict[str, kdb.Region] = {}
        for net in nets:
            if net is None:
                continue
            for n, r in shown:
                got = l2n.shapes_of_net(net, r, True)
                if not got.is_empty():
                    layers.setdefault(n, kdb.Region()).insert(got)
        if any(n is not None for n in nets):
            terms[t] = {n: r.merged() for n, r in layers.items()}
    return {"kind": kind, "class": "mim" if plate else dc.name, "params": params, "terminals": terms, **extra}


def _pcell_fingers(layout: kdb.Layout, cell: kdb.Cell) -> int | None:
    """The fingers (nf) of the PDK MOSFET PCell a cell is a variant of (its meta information, pcell_worker.py), or
    holds as its only generated cell (sync_layout's wrapper): None for anything else. Only then are several
    transistors in a chain one device in fingers; a stack of transistors in series looks the same."""
    import json

    cells = [cell] + [layout.cell(ci) for ci in cell.called_cells()]
    found = []
    for c in cells:
        for m in c.each_meta_info():
            if m.name == "undine.pdks.pcell":
                try:
                    found.append(json.loads(m.value))
                except (TypeError, ValueError):
                    return None
    if len(found) != 1:
        return None
    try:
        return int((found[0].get("params") or {}).get("nf") or 1)
    except (TypeError, ValueError):
        return None


def _fingers(devs: list, sd_of) -> tuple[dict[str, list], dict] | None:
    """The fingers of one MOSFET not joined yet (a PCell's nf > 1 before routing) as one device: {terminal: [nets]},
    {W: their sum, L}; None when they are not that (another class or L, a strip shared by no chain of fingers). The
    strips are coloured in turn along the chain of fingers; the sources are the colour of the leftmost strip (sd_of:
    a net's source / drain shapes)."""
    classes = {x.device_class().name for x in devs}
    if len(classes) != 1 or next(iter(classes)) not in ("nmos", "pmos"):
        return None
    dc = devs[0].device_class()
    pid = {p.name: p.id() for p in dc.parameter_definitions()}
    tid = {t.name: t.id() for t in dc.terminal_definitions()}
    if len({round(x.parameter(pid["L"]), 6) for x in devs}) != 1:
        return None
    nets: dict[int, object] = {}
    adj: dict[int, set[int]] = defaultdict(set)
    for x in devs:
        a, b = x.net_for_terminal(tid["S"]), x.net_for_terminal(tid["D"])
        if a is None or b is None or a.cluster_id == b.cluster_id:
            return None
        nets[a.cluster_id], nets[b.cluster_id] = a, b
        adj[a.cluster_id].add(b.cluster_id)
        adj[b.cluster_id].add(a.cluster_id)
    first = next(iter(adj))
    colour, todo = {first: 0}, [first]
    while todo:
        n = todo.pop()
        for m in adj[n]:
            if m not in colour:
                colour[m] = 1 - colour[n]
                todo.append(m)
            elif colour[m] == colour[n]:
                return None                           # strips that are a source of one finger and a drain of the next
    if len(colour) != len(adj):
        return None                                   # two devices side by side, not one in fingers
    left = {n: (sd_of(net).bbox().left if not sd_of(net).is_empty() else 0) for n, net in nets.items()}
    src = colour[min(left, key=lambda n: (left[n], n))]
    side = lambda c_: [nets[n] for n in sorted(colour, key=lambda n: left[n]) if colour[n] == c_]
    one = lambda t: list({x.net_for_terminal(tid[t]).cluster_id: x.net_for_terminal(tid[t]) for x in devs
                          if x.net_for_terminal(tid[t]) is not None}.values())
    return ({"S": side(src), "G": one("G"), "D": side(1 - src), "B": one("B")},
            {"W": round(sum(x.parameter(pid["W"]) for x in devs), 6), "L": round(devs[0].parameter(pid["L"]), 6)})


def _connect(l2n: kdb.LayoutToNetlist, st: Stack, R: dict, sd: kdb.Region) -> list[tuple[str, kdb.Region]]:
    """The stack's connections in an extractor (the diffusion as source / drain pieces, `sd`; a cut that lands on a
    capacitor plate joins the plate, not the metal under it, and the plate's own cuts join it to the metal above).
    Returns the conductors (name, layer) bottom to top."""
    layer = {st.diff: sd}
    plated = {p.cut: p for p in st.plates}
    for p in st.plates:
        free = R[p.metal] - R[p.name]                  # the metal where the cut is not on the plate
        l2n.register(free, f"${p.metal}-{p.name}")
        l2n.connect(free, R[p.metal])
        l2n.connect(R[p.cut], R[p.name])
        layer[(p.cut, p.metal)] = free
        above = next(c.above for c in st.cuts if c.name == p.cut)
        for x in p.also:
            l2n.connect(R[x])
            l2n.connect(R[x], R[p.name])
            l2n.connect(R[x], R[above])
    for c in st.cuts:
        l2n.connect(R[c.name])
        below = layer.get((c.name, c.below)) if c.name in plated and c.below == plated[c.name].metal else None
        l2n.connect(below if below is not None else layer.get(c.below, R[c.below]), R[c.name])
        l2n.connect(R[c.name], layer.get(c.above, R[c.above]))
    out = [(st.diff, sd)] + ([(st.tap, R[st.tap])] if st.tap else []) + [(c.name, R[c.name]) for c in st.conductors] + \
        [(p.name, R[p.name]) for p in st.plates]
    for _, r in out:
        l2n.connect(r)
    return out


# ------------------------------------------------------------------------------------------ the nets of a layout


Key = tuple                       # a net: (ids of the subcircuits down to it, its cluster id); () for the top cell's


class Nets:
    """The nets of a cell's layout, extracted hierarchically (an array of unit cells stays one array), named by their
    labels and by the terminals of the devices bound to schematic instances (`schematic`: {instance: {pin: net}}, the
    schematic's connectivity). A net is a Key; what it is called: names(key) (labels first), describe(key). An n-well
    joins its taps here, as in silicon and LVS (a drawing that touches a well touches its net); metal_at /
    metal_pieces give the nets of the wiring alone, for route.

    Built under the layout's lock by the caller; it holds its own copy of the shapes, but the windows it reads
    (touching, near) look at the layout again, so the layout must not change in between."""

    ARRAY_NAMED = 256             # the elements of a bound array whose terminals name nets (a CDAC's unit capacitors)

    def __init__(self, layout: kdb.Layout, cell: kdb.Cell, profile: Profile, schematic: dict[str, dict[str, str]] | None = None):
        st = stack(profile)
        if st is None:
            raise ValueError(f"{profile.label} has no layer stack in Undine: nets and terminals are not available")
        self.layout, self.top, self.profile, self.st = layout, cell, profile, st
        self.l2n = l2n = kdb.LayoutToNetlist(kdb.RecursiveShapeIterator(layout, cell, []))
        self.R: dict[str, kdb.Region] = {}
        self.li: dict[str, int] = {}
        names = {st.diff, st.poly, st.well, *([st.tap] if st.tap else []), *(c.name for c in st.conductors),
                 *(c.name for c in st.cuts), *(p.name for p in st.plates), *(x for p in st.plates for x in p.also)}
        for n in sorted(names):
            ld = profile.layers.get(n)
            if ld is None:
                raise ValueError(f"the stack's layer {n} is not in {profile.label}'s layer map")
            self.li[n] = li = layout.find_layer(*ld)
            # a layer the layout lacks: an empty one of the extractor's own, the layout left as it is
            self.R[n] = l2n.make_polygon_layer(li, n) if li is not None else l2n.make_layer(n)
        self.sd = self.R[st.diff] - self.R[st.poly]
        l2n.register(self.sd, "$sd")
        self.conductors = [n for n, _ in _connect(l2n, st, self.R, self.sd)]
        if st.tap:
            l2n.connect(self.R[st.tap], self.R[st.well])
        l2n.connect(self.R[st.well])
        for lab in profile.pin_label_layers:
            metal = lab.removesuffix(".label")
            if metal in self.R and (ld := profile.layers.get(lab)):
                li = layout.find_layer(*ld)
                if li is not None:
                    l2n.connect(self.R[metal], l2n.make_text_layer(li, lab))
        l2n.include_floating_subcircuits = True        # an unrouted device is still an instance of its own
        l2n.extract_netlist()
        self.circuit = l2n.netlist().circuit_by_name(cell.name)
        self._nets: dict[Key, tuple] = {}
        self._metal: tuple | None = None                      # the same without the wells (metal_pieces)
        self.terminal_names: dict[Key, list[str]] = {}         # "XMS:S"
        self.schematic_names: dict[Key, set[str]] = {}         # the schematic nets of the terminals on it
        self.devices: dict[str, dict] = {}
        self._bind(schematic or {})

    # ------------------------------------------------------------------ identity

    def _key(self, net, path: list) -> Key:
        key = (tuple(sc.id() for sc in path if sc is not None), net.cluster_id)
        self._nets.setdefault(key, (net, list(path)))
        return key

    def at(self, layer: str, point: kdb.Point) -> Key | None:
        """The net of the shape of `layer` (a conductor, a cut or a plate) at a point."""
        path: list = []
        net = self.l2n.probe_net(self.sd if layer == self.st.diff else self.R[layer], point, path)
        return None if net is None else self._key(net, path)

    def labels(self, key: Key) -> list[str]:
        net = self._nets[key][0]
        return [n for n in (net.name or "").split(",") if n] if not key[0] else []

    def names(self, key: Key) -> list[str]:
        """What the net is called: its labels, then its device terminals' schematic nets."""
        return list(dict.fromkeys([*self.labels(key), *sorted(self.schematic_names.get(key, ()))]))

    def describe(self, key: Key) -> str:
        names, terms = self.names(key), self.terminal_names.get(key, [])
        if not names:
            return "an unnamed net" + (f" ({', '.join(terms[:4])})" if terms else "")
        out = " / ".join(names) if len(names) > 1 else names[0]
        if len(names) > 1:
            out += " (shorted)"
        return out + (f" ({', '.join(terms[:4])}{' ...' if len(terms) > 4 else ''})" if terms else "")

    def find(self, name: str) -> list[Key]:
        """The nets called `name` (a label, or a schematic net a device terminal on it has), case as in SPICE."""
        low = name.lower()
        out = []
        for net in self.circuit.each_net() if self.circuit is not None else []:
            if low in {n.lower() for n in (net.name or "").split(",")}:
                out.append(self._key(net, []))
        out += [k for k, v in self.schematic_names.items() if low in {n.lower() for n in v} and k not in out]
        return out

    # ------------------------------------------------------------------ joined by the wiring alone

    def metal_at(self, layer: str, point: kdb.Point) -> Key | None:
        """The net of the shape of `layer` at a point with the n-wells joining nothing (a well is no wire: two taps in
        one well are two nets here, one in at()). Extracted the first time it is asked."""
        if self._metal is None:
            l2n = kdb.LayoutToNetlist(kdb.RecursiveShapeIterator(self.layout, self.top, []))
            R = {n: l2n.make_polygon_layer(li, n) if li is not None else l2n.make_layer(n) for n, li in self.li.items()}
            sd = R[self.st.diff] - R[self.st.poly]
            l2n.register(sd, "$sd")
            _connect(l2n, self.st, R, sd)
            l2n.include_floating_subcircuits = True
            l2n.extract_netlist()
            self._metal = (l2n, R, sd)
        l2n, R, sd = self._metal
        if layer != self.st.diff and layer not in R:
            return None
        path: list = []
        net = l2n.probe_net(sd if layer == self.st.diff else R[layer], point, path)
        return None if net is None else (tuple(sc.id() for sc in path if sc is not None), net.cluster_id)

    def metal_pieces(self, layers: dict[str, kdb.Region]) -> dict[Key, dict[str, kdb.Region]]:
        """Shapes (per conductor) grouped by the net each is on through the wiring alone (metal_at)."""
        out: dict[Key, dict[str, kdb.Region]] = {}
        for n, reg in layers.items():
            if n not in self.conductors:
                continue
            for p in reg.merged().each():
                key = self.metal_at(n, inside(p))
                if key is not None:
                    out.setdefault(key, {}).setdefault(n, kdb.Region()).insert(p)
        return out

    # ------------------------------------------------------------------ geometry

    def shapes(self, key: Key, layer: str) -> kdb.Region:
        """The net's shapes of `layer` in the cell's coordinates."""
        net, path = self._nets[key]
        tr = kdb.ICplxTrans()
        for sc in path:
            tr = tr * kdb.ICplxTrans(sc.trans, self.layout.dbu)
        return self.l2n.shapes_of_net(net, self.sd if layer == self.st.diff else self.R[layer], True, tr)

    def window(self, layer: str, box: kdb.Box) -> kdb.Region:
        """The layout's shapes of `layer` (merged, flat) that meet a box: a layer of the stack (the diffusion as source /
        drain pieces; "diff$raw" whole) or any other of the profile's (an implant)."""
        if layer == self.st.diff:
            return self.window(self.st.diff + "$raw", box) - self.window(self.st.poly, box)
        name = layer.removesuffix("$raw")
        if name in self.li:
            li = self.li[name]
        else:
            ld = self.profile.layers.get(name)
            li = self.layout.find_layer(*ld) if ld else None
        if li is None:
            return kdb.Region()
        return kdb.Region(kdb.RecursiveShapeIterator(self.layout, self.top, li, box, False)).merged()

    def touching(self, layer: str, region: kdb.Region) -> dict[Key, kdb.Region]:
        """The nets whose shapes of `layer` touch or overlap `region`, with those shapes."""
        out: dict[Key, kdb.Region] = {}
        if region.is_empty():
            return out
        for p in self.window(layer, region.bbox().enlarged(1, 1)).interacting(region).each():
            key = self.at(layer, inside(p))
            if key is not None:
                out.setdefault(key, kdb.Region()).insert(p)
        return out

    def near(self, layer: str, region: kdb.Region, space: int, wide: tuple[int, int] | None = None) -> list[kdb.Polygon]:
        """The shapes of `layer` closer than `space` (dbu) to `region` without touching it (a spacing violation). wide
        (width, spacing): where either side is wider than width, the larger spacing holds (the wide part as the decks
        find it: the shape sized in by half that width and out again)."""
        reach = max(space, wide[1] if wide else 0)
        win = self.window(layer, region.bbox().enlarged(reach, reach))
        others = (win - win.interacting(region)).merged()
        if wide is None:
            return list(others.interacting(region.sized(space - 1)).each())
        half = wide[0] // 2
        fat = region.sized(-half).sized(half) & region         # the new shapes' wide part
        out = []
        for p in others.each():
            r = kdb.Region(p)
            mine = r.sized(-half).sized(half) & r
            if not r.interacting(region.sized(space - 1)).is_empty() or \
                    (not fat.is_empty() and not r.interacting(fat.sized(wide[1] - 1)).is_empty()) or \
                    (not mine.is_empty() and not mine.interacting(region.sized(wide[1] - 1)).is_empty()):
                out.append(p)
        return out

    # ------------------------------------------------------------------ devices

    def _bind(self, schematic: dict[str, dict[str, str]]) -> None:
        """The terminals of each bound instance, where they are and on which net, and the nets they name. A device
        placed as several pieces (place_pcell, one per finger or per row) is each piece (pieces: by its id, child@x,y)
        and, by its name, all of them: a terminal is the shapes of every piece's, on the nets of every piece's."""
        from undine.layouts.layout import instance_ids

        self._cells: dict[int, dict] = {}
        self.pieces: dict[str, dict] = {}            # id -> the piece as devices has a device (bound or not: piece())
        self._ids = instance_ids(self.layout, self.top)
        pending = []
        by_name: dict[str, list[dict]] = {}
        for inst, iid in self._ids:
            name = inst.property("undine_inst")
            if not name:
                continue
            pins = {k.upper(): v for k, v in (schematic.get(name) or {}).items()}
            info, elements = self._piece(inst, iid, pins)
            self.pieces[iid] = info
            by_name.setdefault(name, []).append(info)
            pending += [(name, info, terms) for terms in elements]
        for name, info, terms in pending:               # plates, gates, bulks: their names hold either way
            for t, v in terms.items():
                if info["kind"] != "mos" or t not in ("S", "D"):
                    for key in v["keys"]:
                        self._name(key, f"{name}:{t}", info["pins"].get(t.upper()))
        for name, info, terms in pending:               # source and drain: after the schematic, as routed
            if info["kind"] == "mos" and "S" in terms and "D" in terms:
                s, d = info["pins"].get("S"), info["pins"].get("D")
                ks, kd = terms["S"]["keys"], terms["D"]["keys"]
                known = lambda keys: set().union(*(self._known(k) for k in keys)) if keys else set()
                routed = (d in known(ks) or s in known(kd), s in known(ks) or d in known(kd))
                if s and d and s != d and (routed[0] and not routed[1] or not any(routed) and self._tap_wants_swap(info, terms)):
                    terms["S"], terms["D"] = terms["D"], terms["S"]
                    info["swapped"] = True
                for t in ("S", "D"):
                    for key in terms[t]["keys"]:
                        self._name(key, f"{name}:{t}", info["pins"].get(t))
        for name, infos in by_name.items():
            self.devices[name] = infos[0] if len(infos) == 1 else _joined(infos)

    def _piece(self, inst: kdb.Instance, iid: str, pins: dict[str, str]) -> tuple[dict, list[dict]]:
        """A top-level instance as a device: its info (cell, kind, class, params, terminals of its first element) and
        the terminals of each element (up to ARRAY_NAMED), each {name: {"layers", "key", "keys"}}."""
        ci = inst.cell_index
        if ci not in self._cells:
            self._cells[ci] = device_terminals(self.layout, self.layout.cell(ci), self.profile)
        dev = self._cells[ci]
        arr = inst.cell_inst
        info = {"cell": self.layout.cell(ci).name, "kind": dev["kind"], "class": dev.get("class"),
                "params": dev.get("params") or {}, "terminals": {}, "pins": pins, "id": iid,
                **({"fingers": dev["fingers"]} if dev.get("fingers") else {})}
        if arr.is_regular_array():
            info["array"] = {"columns": arr.na, "rows": arr.nb}
        elements = []
        if dev["kind"]:
            for k, tr in enumerate(arr.each_cplx_trans()):
                if k >= self.ARRAY_NAMED:
                    break
                terms = {}
                for t, layers in dev["terminals"].items():
                    where = {n: r.transformed(tr) for n, r in layers.items()}
                    keys = self._probe_all(where) if dev.get("fingers") else [k_ for k_ in [self._probe(where)] if k_ is not None]
                    terms[t] = {"layers": where, "key": keys[0] if keys else None, "keys": keys}
                if k == 0:
                    info["terminals"] = terms
                elements.append(terms)
        return info, elements

    def piece(self, iid: str) -> dict | None:
        """A top-level instance by its id (child@x,y, as get_layout_info lists it), bound or not, as a device."""
        from undine.layouts.layout import ID_RE

        def norm(x: str):                       # the origin with any decimals: as the grid has it
            m = ID_RE.fullmatch(str(x).strip())
            try:
                return m and (m.group(1), round(float(m.group(2)) / self.layout.dbu),
                              round(float(m.group(3)) / self.layout.dbu), m.group(4) or "")
            except ValueError:
                return None
        want = norm(iid)
        if not want:
            return None
        hit = next(((i, x) for i, x in self._ids if norm(x) == want), None)
        if hit is None:
            return None
        inst, iid = hit
        if iid in self.pieces:
            return self.pieces[iid]
        info, _ = self._piece(inst, iid, {})
        self.pieces[iid] = info
        return info

    def _probe_all(self, layers: dict[str, kdb.Region]) -> list[Key]:
        """The nets of a terminal in pieces (a multi-finger MOSFET's strips before routing): each of its shapes on
        each conductor probed, the topmost first."""
        out: list[Key] = []
        for n in reversed(self.conductors):
            r = layers.get(n)
            for poly in (r.merged().each() if r is not None else []):
                key = self.at(n, inside(poly))
                if key is not None and key not in out:
                    out.append(key)
        return out

    def _tap_wants_swap(self, info: dict, terms: dict) -> bool:
        """Whether, nothing routed yet, the strips are better named the other way: the one butted against the body tap
        (a PCell's bulk tie) is to be the one on the bulk's net, so that no junction of another net sits against it."""
        tap = (terms.get("B") or {}).get("layers", {}).get(self.st.tap) if self.st.tap else None
        if tap is None or tap.is_empty():
            return False
        butts = {t: not terms[t]["layers"].get(self.st.diff, kdb.Region()).interacting(tap).is_empty() for t in ("S", "D")}
        bulk = info["pins"].get("B")
        return bool(bulk) and ((butts["D"] and not butts["S"] and info["pins"].get("S") == bulk) or
                               (butts["S"] and not butts["D"] and info["pins"].get("D") == bulk))

    def _known(self, key: Key | None) -> set[str]:
        return set(self.names(key)) if key is not None else set()

    def _name(self, key: Key | None, term: str, net: str | None) -> None:
        if key is None:
            return
        have = self.terminal_names.setdefault(key, [])
        if term not in have:                            # a device in pieces: its terminal once
            have.append(term)
        if net:
            self.schematic_names.setdefault(key, set()).add(net)

    def _probe(self, layers: dict[str, kdb.Region]) -> Key | None:
        """The net of a terminal: probed on its topmost conductor (or plate), inside one of its shapes."""
        for n in reversed(self.conductors):
            r = layers.get(n)
            if r is not None and not r.is_empty():
                return self.at(n, inside(next(r.each())))
        return None


def _joined(infos: list[dict]) -> dict:
    """A device placed as several pieces as one (Nets.devices): the first piece's cell and kind, every piece's
    terminals (their shapes together, the nets of all of them), the pieces' ids."""
    first = next((i for i in infos if i["kind"]), infos[0])
    terms: dict[str, dict] = {}
    for i in infos:
        for t, v in i["terminals"].items():
            e = terms.setdefault(t, {"layers": {}, "key": v["key"], "keys": []})
            for n, r in v["layers"].items():
                e["layers"].setdefault(n, kdb.Region()).insert(r)     # merged once below: a CDAC bit's 512 pieces
            e["keys"] += [k for k in v["keys"] if k not in e["keys"]]
            if e["key"] is None:
                e["key"] = v["key"]
    for e in terms.values():
        e["layers"] = {n: r.merged() for n, r in e["layers"].items()}
    params = dict(first["params"])
    if first["kind"] == "mos" and all(i["kind"] == "mos" and i["params"].get("L") == params.get("L") for i in infos):
        params["W"] = round(sum(i["params"].get("W", 0) for i in infos), 6)        # pieces in parallel
    return {**first, "params": params, "terminals": terms, "pieces": [i["id"] for i in infos],
            **({"swapped": True} if any(i.get("swapped") for i in infos) else {})}


# ------------------------------------------------------------------------------------------ the wiring check


SUBSTRATE = "substrate"   # the body of an nfet without a tap of its own: the substrate under it, no drawn conductor
LIST = 8          # pins named per net / short; the rest counted


def _sky130(l2n: kdb.LayoutToNetlist, layout: kdb.Layout, layers: dict) -> dict[str, kdb.Region]:
    """The sky130 conductors, connected as sky130.lvs connects them (its MIM handling included), but for its n-wells:
    a well is no wire, so it is a net of its own here, not joined to its taps (check() says which nets only a well
    joins)."""
    R = {}

    def layer(name):
        li = layout.find_layer(*layers[name]) if name in layers else None
        return l2n.make_layer(li if li is not None else layout.layer(*layers[name]), name)

    for n in ("diff", "tap", "poly", "licon1", "li1", "mcon", "met1", "via", "met2", "via2", "met3", "via3", "met4", "via4",
              "met5", "nwell", "capm", "cap2m", "poly.res", "diff.res"):
        R[n] = layer(n)
    derived = {"sd": R["diff"] - R["poly"] - R["diff.res"], "gate": R["poly"] - R["poly.res"],
               "ntap": R["tap"] & R["nwell"], "ptap": R["tap"] - R["nwell"],
               "met3n": R["met3"] - R["capm"], "met4n": R["met4"] - R["cap2m"]}
    for n, r in derived.items():
        l2n.register(r, n)
        R[n] = r
    for a, b in (("sd", "licon1"), ("ptap", "licon1"), ("ntap", "licon1"), ("gate", "licon1"), ("licon1", "li1"),
                 ("li1", "mcon"), ("mcon", "met1"), ("met1", "via"), ("via", "met2"), ("met2", "via2"), ("via2", "met3n"),
                 ("met3n", "via3"), ("capm", "via3"), ("via3", "met4n"), ("met4n", "via4"), ("cap2m", "via4"),
                 ("via4", "met5")):
        l2n.connect(R[a])
        l2n.connect(R[b])
        l2n.connect(R[a], R[b])
    l2n.connect(R["nwell"])       # an n-well is a net of its own: no wire, its taps are joined by check() apart
    for lab, cond in (("li1.label", "li1"), ("met1.label", "met1"), ("met2.label", "met2"), ("met3.label", "met3n"),
                      ("met4.label", "met4n"), ("met5.label", "met5"), ("met4.pin", "met4n")):
        if lab in layers:
            li = layout.find_layer(*layers[lab])
            if li is not None:
                t = l2n.make_text_layer(li, lab)
                l2n.connect(R[cond], t)
    return R


RECIPES = {"sky130": _sky130}


def _flat(layout: kdb.Layout, top: kdb.Cell) -> kdb.Layout:
    """`top` flattened into a layout of its own: one circuit, so every net is the top cell's. Labels only from `top`
    itself (as LVS names its pins): a cell's own pin labels do not name the nets they end up on."""
    out = kdb.Layout()
    out.dbu = layout.dbu
    cell = out.create_cell(top.name)
    for li in layout.layer_indexes():
        it = top.begin_shapes_rec(li)
        it.shape_flags = kdb.Shapes.SPolygons | kdb.Shapes.SBoxes | kdb.Shapes.SPaths
        target = cell.shapes(out.layer(layout.get_info(li)))
        target.insert(it)
        for s in top.shapes(li).each(kdb.Shapes.STexts):
            target.insert(s.text)
    return out


def _terminals(layout: kdb.Layout, cell: kdb.Cell, kind: str, layers: dict, cache: dict) -> list[dict]:
    """The terminals of a device cell, in its own frame: [{"pin": name, or "sd"/"end" with "group" 0 / 1 for an
    interchangeable pair, "on": the conductor to probe, "at": a point}]."""
    key = (cell.cell_index(), kind)
    if key in cache:
        return cache[key]

    def reg(name):
        li = layout.find_layer(*layers[name]) if name in layers else None
        return kdb.Region(cell.begin_shapes_rec(li)).merged() if li is not None else kdb.Region()

    out: list[dict] = []
    if kind in ("n", "p"):
        diff, poly, tap, nwell = reg("diff"), reg("poly"), reg("tap"), reg("nwell")
        sd = sorted((diff - poly).each(), key=lambda p: p.bbox().left)
        for i, p in enumerate(sd):
            t = {"pin": "sd", "group": i % 2, "on": "sd", "at": inside(p)}
            butted = tap.interacting(kdb.Region(p).sized(1))        # a tap touching it: the silicide joins the two
            if not butted.is_empty():
                t["butted"] = {"on": "ntap" if kind == "p" else "ptap", "at": inside(next(iter(butted.each())))}
            out.append(t)
        out += [{"pin": "G", "on": "gate", "at": inside(p)} for p in (diff & poly).each()]
        body = {"diff": diff.bbox()} if not diff.is_empty() else {}    # a body without a tap: where its diffusion is
        if kind == "n":
            taps = list((tap - nwell).each())
            out.append({"pin": "B", "on": "ptap", "at": inside(taps[0])} if taps else
                       {"pin": "B", "on": SUBSTRATE, **body})
        else:
            taps = list((tap & nwell).each())
            wells = list(nwell.each())
            out.append({"pin": "B", "on": "ntap", "at": inside(taps[0])} if taps else
                       {"pin": "B", "on": "nwell", "at": inside(wells[0]), **body} if wells else {"pin": "B", "on": None})
    elif kind == "mim":
        for cap, plate, top, bottom in (("capm", "met3", "c0", "c1"), ("cap2m", "met4", "c0", "c1")):
            caps = list(reg(cap).each())
            if caps:
                c = caps[0]
                rim = (reg(plate) & kdb.Region(c.bbox().enlarged(150, 150))) - kdb.Region(c)
                out.append({"pin": top, "on": cap, "at": inside(c)})
                if not rim.is_empty():
                    out.append({"pin": bottom, "on": plate + "n", "at": inside(next(iter(rim.each())))})
                break
    elif kind == "res":
        for body, mark in (("poly", "poly.res"), ("diff", "diff.res")):
            m = reg(mark)
            if m.is_empty():
                continue
            ends = sorted(((reg(body) - m).interacting(m)).each(), key=lambda p: (p.bbox().left, p.bbox().bottom))
            out += [{"pin": "end", "group": i, "on": "gate" if body == "poly" else "sd", "at": inside(p)}
                    for i, p in enumerate(ends[:2])]
            break
    elif kind == "cell":
        for lab, cond in (("li1.label", "li1"), ("met1.label", "met1"), ("met2.label", "met2"), ("met3.label", "met3n"),
                          ("met4.label", "met4n"), ("met5.label", "met5")):
            li = layout.find_layer(*layers[lab]) if lab in layers else None
            if li is None:
                continue
            for s in cell.shapes(li).each():
                if s.is_text():
                    out.append({"pin": s.text.string, "on": cond, "at": s.text.trans.disp.to_p()})
    cache[key] = out
    return out


def _kind(d: dict) -> str | None:
    """n / p (MOSFET), mim, res or cell: by its PCell, else by its symbol's name (no PCell environment here)."""
    if d.get("kind") == "cell":
        return "cell"
    name = d.get("pcell") or Path(str(d.get("symbol") or "")).stem
    for pre, kind in (("nfet", "n"), ("pfet", "p"), ("mim_cap", "mim"), ("cap_mim", "mim"), ("res_", "res")):
        if name.startswith(pre):
            return kind
    return None


def _elements(inst: dict) -> list[kdb.Trans]:
    tr = kdb.Trans.from_s(inst["trans"])
    a = inst.get("array")
    if not a:
        return [tr]
    return [kdb.Trans(kdb.Vector(i * a["a"][0] + j * a["b"][0], i * a["a"][1] + j * a["b"][1])) * tr
            for j in range(a["rows"]) for i in range(a["columns"])]


def check(pdk, layout: kdb.Layout, cell: str, conn, devices: list[dict], bound: dict[str, list[dict]]) -> dict | None:
    """Routed, unrouted and shorted nets of `cell` by its drawn conductors (module docstring); None when the PDK has
    no recipe. `conn`: the schematic's connectivity, `devices`: Sdl._devices' items, `bound`: Sdl._bound()."""
    family = pdk.profile.family if pdk else ""
    recipe = RECIPES.get(family)
    top = layout.cell(cell)
    if recipe is None or top is None:
        return None
    layers = pdk.profile.layers
    st = pdk.profile.stack
    latchup = st.taps.latchup if st is not None and st.taps is not None else 0.0
    flat = _flat(layout, top)
    l2n = kdb.LayoutToNetlist(kdb.RecursiveShapeIterator(flat, flat.top_cell(), []))
    R = recipe(l2n, flat, layers)
    l2n.extract_netlist()
    circuit = l2n.netlist().circuit_by_name(cell) or next(iter(l2n.netlist().each_circuit_top_down()), None)
    dbu = flat.dbu

    def probe(term: dict, tr: kdb.Trans):
        if term["on"] == SUBSTRATE:
            return SUBSTRATE
        if term["on"] not in R:
            return None
        return l2n.probe_net(R[term["on"]], tr.trans(term["at"]))

    def nid(net) -> str | None:
        return net if net is None or net is SUBSTRATE else str(net.cluster_id)

    # the substrate regions (a deep n-well's p-well is one of its own) and the taps: (net, where, its region or well)
    dli = flat.find_layer(*layers["dnwell"]) if "dnwell" in layers else None
    deep = list(kdb.Region(flat.top_cell().begin_shapes_rec(dli)).merged().each()) if dli is not None else []

    def region(p: kdb.Point) -> int:
        return next((i for i, d in enumerate(deep) if d.inside(p)), -1)
    ptaps, ntaps = [], []
    for p in R["ptap"].merged().each():
        if (n := l2n.probe_net(R["ptap"], inside(p))) is not None:
            ptaps.append((nid(n), boxes(kdb.Region(p)), region(inside(p))))
    for p in R["ntap"].merged().each():
        n, w = l2n.probe_net(R["ntap"], inside(p)), l2n.probe_net(R["nwell"], inside(p))
        if n is not None:
            ntaps.append((nid(n), boxes(kdb.Region(p)), nid(w)))
    # what the substrate and the wells would join: the substrate its p-taps, a well its n-taps (the deck joins them so)
    parent: dict = {}

    def find(n):
        while parent.get(n, n) != n:
            parent[n] = parent.get(parent[n], parent[n])
            n = parent[n]
        return n

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    for n, _, _ in ptaps:
        union(n, SUBSTRATE)
    for n, _, w in ntaps:
        if w is not None:
            union(n, w)
    # the n-wells each layout net is in (by its n-taps; a well's own net, a body without a tap, is in it), apart from
    # the union: a net with both kinds of tap joins its wells to the substrate there, and a well still joins its nets
    in_wells: dict = defaultdict(set)
    for n, _, w in ntaps:
        if w is not None:
            in_wells[n].add(w)
    for p in R["nwell"].merged().each():
        if (w := l2n.probe_net(R["nwell"], inside(p))) is not None:
            in_wells[nid(w)].add(nid(w))
    on_substrate = {SUBSTRATE} | {n for n, _, _ in ptaps}
    names: dict[str, str] = {}                  # layout net id -> its name (a label), when it has one
    cache: dict = {}
    pins_of: dict[tuple[str, str], list[str | None]] = defaultdict(list)   # (instance, pin) -> layout nets, per element
    pairs: list[tuple[dict, list[list[str | None]]]] = []                   # a device element's interchangeable pair
    unchecked: Counter = Counter()
    butted: list[tuple[str, str | None, str]] = []          # (device, the source / drain's net, its butted tap's net)
    bodies: list[tuple] = []        # a body without a tap: ((instance, pin), element, n / p, its diffusion, region / well)
    for d in devices:
        kind = _kind(d)
        pieces = bound.get(d["name"], [])
        if not pieces:
            continue
        if kind is None:
            unchecked[f"{d['name']} (no terminals known)"] += 1
            continue
        pins = {k.upper(): k for k in d["nets"]}
        for p in pieces:
            c = layout.cell(p["cell"])
            terms = _terminals(layout, c, kind, layers, cache)
            for tr in _elements(p):
                groups: list[list[str | None]] = [[], []]
                for t in terms:
                    net = probe(t, tr)
                    if net is not None and net is not SUBSTRATE and net.name:
                        names[nid(net)] = net.name
                    if t["pin"] in ("sd", "end"):
                        groups[t["group"]].append(nid(net))
                    if t.get("butted") and (tap := probe(t["butted"], tr)) is not None and nid(tap) != nid(net):
                        butted.append((d["name"], nid(net), nid(tap)))
                    elif t["pin"].upper() in pins:
                        key = (d["name"], pins[t["pin"].upper()])
                        if t.get("diff") is not None and net is not None:
                            box = t["diff"].transformed(tr)
                            bodies.append((key, len(pins_of[key]), kind, box,
                                           region(box.center()) if kind == "n" else nid(net)))
                        pins_of[key].append(nid(net))
                if any(groups):
                    pairs.append((d, groups))
    if circuit is not None:
        names.update({nid(n): n.name for n in circuit.each_net() if n.name})
    votes: dict[str, Counter] = defaultdict(Counter)       # schematic net -> layout nets known to be on it
    for (inst, pin), nets in pins_of.items():              # pins that are what they are (gates, plates, bulks)
        votes[_net_of(conn, inst, pin)].update(n for n in nets if n)
    for n, name in names.items():                          # and the labels
        votes[name][n] += 1
    todo = []
    for d, groups in pairs:
        two = [k for k in d["nets"] if k.upper() in ("S", "D")] if _kind(d) in ("n", "p") else \
            [k for k in d["nets"] if k.upper() not in ("B",)][:2]
        if len(two) == 2:
            todo.append((d, two, (_net_of(conn, d["name"], two[0]), _net_of(conn, d["name"], two[1])), groups))
    while todo:                                            # sources / drains: the way round that agrees more, surest first
        def margin(t):
            _d, _two, (a, b), (g0, g1) = t
            score = lambda x, y: sum(votes[a][n] for n in x if n) + sum(votes[b][n] for n in y if n)
            return score(g0, g1) - score(g1, g0)
        t = max(todo, key=lambda t: abs(margin(t)))
        todo.remove(t)
        d, two, (a, b), (g0, g1) = t
        if margin(t) < 0:
            g0, g1 = g1, g0
        pins_of[(d["name"], two[0])] += g0
        pins_of[(d["name"], two[1])] += g1
        votes[a].update(n for n in g0 if n)
        votes[b].update(n for n in g1 if n)
    ties, untied = _bodies(conn, pins_of, names, bodies, ptaps, ntaps, latchup, dbu)
    by_net: dict[str, dict[str | None, Counter]] = defaultdict(lambda: defaultdict(Counter))   # net -> layout net -> pins
    total: Counter = Counter()                              # (instance, pin) -> its elements (the units of an array)
    for (inst, pin), nets in pins_of.items():
        sn = _net_of(conn, inst, pin)
        if sn is None:
            continue
        total[f"{inst}:{pin}"] += len(nets)
        for n in nets:
            by_net[sn][n][f"{inst}:{pin}"] += 1
    ports = [p[0] if isinstance(p, (list, tuple)) else p for p in (conn.ports or [])]
    for n, name in names.items():              # a port's label is where the net leaves the cell: one of its pins too
        for p in set(name.split(",")) & set(ports):
            by_net[p][n][f"{p} (label)"] += 1
    unrouted, shorts, substrate_only, well_only = [], defaultdict(set), [], []
    wells: dict = defaultdict(set)              # what a well joins: the well -> the schematic nets on it
    for sn, parts in sorted(by_net.items()):
        for n in parts:
            if n is not None:
                shorts[n].add(sn)
                for w in in_wells.get(n, ()):
                    wells[w].add(sn)
        if len(parts) > 1 or None in parts:              # the pins off the piece most of them are on
            main = max((n for n in parts if n is not None), key=lambda n: sum(parts[n].values()), default=None)
            off = sum((ps for n, ps in parts.items() if n != main and n is not None), Counter())
            apart = [p + (f" ({k} of {total[p]})" if total[p] > 1 else "") for p, k in sorted(off.items())]
            apart += [p + f" ({k} of {total[p]}: on no conductor, e.g. no tap)" for p, k in sorted((parts.get(None) or {}).items())]
            unrouted.append({"net": sn, "pieces": len([n for n in parts if n is not None]) + (None in parts),
                             "apart": apart[:LIST], **({"more": len(apart) - LIST} if len(apart) > LIST else {})})
            if None not in parts and len({SUBSTRATE if n in on_substrate else n for n in parts}) == 1:
                substrate_only.append(sn)
            elif None not in parts and len({find(n) for n in parts}) == 1:
                well_only.append(sn)
    short = [{"nets": sorted(s), **({"label": names[n]} if n in names else {"on": SUBSTRATE} if n == SUBSTRATE else {})}
             for n, s in shorts.items() if len(s) > 1]
    seen = {tuple(x["nets"]) for x in short}
    for s in sorted(tuple(sorted(s)) for s in wells.values() if len(s) > 1):
        if s not in seen:
            seen.add(s)
            short.append({"nets": list(s), "on": "nwell", "why": "their taps (or bodies) are in one n-well, which joins them"})
    on = lambda n: "/".join(sorted(shorts.get(n) or [])) or names.get(n) or ("open" if n is None else "unnamed")
    taps = sorted({(dev, on(sd), on(tap)) for dev, sd, tap in butted})
    labelled = {v for v in names.values()}
    out = {"method": "the drawn conductors, extracted (KLayout, flat; the substrate and the n-wells join nothing), at "
                     "each placed device's terminals",
           "nets": len(by_net), "routed": len([n for n in by_net if not any(u["net"] == n for u in unrouted)]),
           "unrouted": unrouted, "shorts": short}
    if substrate_only:       # LVS joins every p-tap to its substrate net and so matches: the routing is still missing
        out["substrate_only"] = substrate_only
    if well_only:            # LVS joins an n-well's taps through it and so matches: the path is the resistive well
        out["well_only"] = well_only
    if ties:                 # a body without a tap of its own, tied by a tap of its net next to it
        out["body_ties"] = ties
    if untied:
        out["bodies_untied"] = untied
    if taps:                 # LVS and PEX see two nets; in silicon the silicide over the butted junction joins them
        out["butted_taps"] = [{"device": dev, "source_drain": a, "tap": b} for dev, a, b in taps]
    if (nolabel := [p for p in ports if p not in labelled]):
        out["unlabelled_ports"] = nolabel
    if unchecked:
        out["unchecked"] = sorted(unchecked)
    return out


def _bodies(conn, pins_of: dict, names: dict, bodies: list, ptaps: list, ntaps: list, latchup: float, dbu: float):
    """The bodies of devices without a tap of their own (an nMOS on the substrate, a pMOS on its n-well) tied by a tap
    of their bulk net: a p-tap in the same substrate region (not across a deep n-well), an n-tap in the same n-well,
    whose layout net carries another pin of the net or its label (a net with no pin but such bodies, and no label: a
    tap wired to no pin or label of another net), within `latchup` um of every point of the device's
    diffusion (the nearest such tap). A tied body's pin moves onto that tap's net in pins_of. Returns (ties, untied):
    per pin, the tap (the farthest of its elements' taps) and its distance, or why it is not tied."""
    if not bodies:
        return [], []
    body_keys = {b[0] for b in bodies}
    carried: dict[str, set] = defaultdict(set)              # schematic net -> layout nets with another of its pins
    for key, nets in pins_of.items():
        if key not in body_keys and (sn := _net_of(conn, *key)) is not None:
            carried[sn].update(n for n in nets if n is not None and n is not SUBSTRATE)
    for n, name in names.items():
        for x in name.split(","):
            carried[x].add(n)
    claimed = set().union(*carried.values())                # layout nets with a pin or label of some net

    def dist(p: kdb.Point, tap: list[kdb.Box]) -> float:
        return min(((max(b.left - p.x, 0, p.x - b.right) ** 2 + max(b.bottom - p.y, 0, p.y - b.top) ** 2) ** 0.5
                    for b in tap), default=float("inf")) * dbu

    def far(tap: list[kdb.Box], box: kdb.Box) -> float:
        """How far the corner of the diffusion `box` farthest from the tap is from it, um."""
        return max(dist(p, tap) for p in (box.p1, box.p2, kdb.Point(box.left, box.top), kdb.Point(box.right, box.bottom)))

    def nearest(tap: list[kdb.Box], p: kdb.Point) -> list[float]:
        """The point of the tap nearest p, um."""
        b = min(tap, key=lambda b: dist(p, [b]))
        return [round(min(max(p.x, b.left), b.right) * dbu, 3), round(min(max(p.y, b.bottom), b.top) * dbu, 3)]
    tied: dict[str, dict] = {}
    untied: dict[str, dict] = {}
    for key, k, kind, box, where in bodies:
        pin = f"{key[0]}:{key[1]}"
        sn = _net_of(conn, *key)
        mine = carried.get(sn or "", set())
        # a net whose pins are all such bodies (and no label of it): a tap wired to no other net's pin or label is its
        mine_ = (lambda n: n in mine) if sn in carried else (lambda n: n not in claimed)
        cands = [(far(b, box), n, b) for n, b, r in (ptaps if kind == "n" else ntaps) if r == where and mine_(n)]
        rule = "LU.2" if kind == "n" else "LU.3"
        what = "substrate" if kind == "n" else "n-well"
        if not cands:
            untied[pin] = {"pin": pin, "net": sn, "why": f"no tap in its {what} wired to another pin or the label of "
                                                         f"{sn}: draw one (add_tap / guard_ring) and route it to {sn}"
                                                         if sn in carried else f"no tap in its {what}: draw one (add_tap / "
                                                         "guard_ring)"}
            continue
        d, n, b = min(cands, key=lambda c: c[0])
        at = nearest(b, box.center())
        if latchup and d > latchup:
            untied[pin] = {"pin": pin, "net": sn, "why": f"the nearest tap of {sn} (at {at[0]:g}, {at[1]:g}) is "
                                                         f"{d:.3g} um from its diffusion, beyond the {latchup:g} um "
                                                         f"latch-up distance ({rule}): draw a tap nearer"}
            continue
        pins_of[key][k] = n
        if pin not in tied or d > tied[pin]["distance_um"]:
            tied[pin] = {"pin": pin, "net": sn, "tap": "ptap" if kind == "n" else "ntap", "tap_um": at,
                         "distance_um": round(d, 3)}
    return sorted(tied.values(), key=lambda t: t["pin"]), sorted(untied.values(), key=lambda t: t["pin"])


def _net_of(conn, inst: str, pin: str) -> str | None:
    d = next((d for d in conn.devices if d.name == inst), None)
    return d.nets.get(pin) if d is not None else None
