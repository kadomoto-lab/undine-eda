"""Schematic and symbol editing for agents: the router draws readable wires, drawn nets can be re-wired (a pin moved
to another net, a long wire replaced by labels, a symbol moved with its wires), the port order and the symbol follow
the schematic, and standard-cell supply parameters count as connections. Everything is one undoable, reviewable step."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine.agents import agent_tools as T  # noqa: E402
from undine.schematics import netlist, schematic, schroute, symbolsync, symedit, xschem  # noqa: E402
from undine.schematics.xschem import SymbolResolver, XComp, XFile  # noqa: E402
from undine.server.api import Server  # noqa: E402
from undine.store import oplog  # noqa: E402
from undine.store.changeset import Changesets  # noqa: E402

PORT = 'v {{}}\nK {{type={t}\ntemplate="name=p1 lab=xxx"}}\nB 5 -2 -2 2 2 {{name=p dir={d}}}\n'
SYMBOLS = {
    # a resistor with a body, pins P (top) and M (bottom)
    "res.sym": 'v {}\nK {type=resistor\nformat="@name @pinlist @value"\ntemplate="name=R1 value=1k"}\n'
               'B 4 -10 -20 10 20 {}\nL 4 0 -30 0 -20 {}\nL 4 0 20 0 30 {}\n'
               'B 5 -2.5 -32.5 2.5 -27.5 {name=P}\nB 5 -2.5 27.5 2.5 32.5 {name=M}\n',
    # a standard cell as sky130_stdcells draws one: its supplies are parameters (extra), written as nodes of its format
    "sc_inv.sym": 'v {}\nK {type=primitive\nformat="@name @@A @VGND @VNB @VPB @VPWR @@Y @prefix\\\\\\\\inv_1"\n'
                  'template="name=x1 VGND=VGND VNB=VNB VPB=VPB VPWR=VPWR prefix=sky130_fd_sc_hd__ "\n'
                  'extra="VGND VNB VPB VPWR prefix"}\n'
                  'L 4 -20 -20 -20 20 {}\nL 4 -20 -20 20 0 {}\nL 4 -20 20 20 0 {}\nL 4 -40 0 -20 0 {}\nL 4 20 0 40 0 {}\n'
                  'B 5 -42.5 -2.5 -37.5 2.5 {name=A dir=in}\nB 5 37.5 -2.5 42.5 2.5 {name=Y dir=out}\n',
    "devices/lab_pin.sym": 'v {}\nK {type=label\ntemplate="name=l1 lab=xxx"}\nB 5 -2 -2 2 2 {name=p}\n',
    "devices/ipin.sym": PORT.format(t="ipin", d="in"),
    "devices/opin.sym": PORT.format(t="opin", d="out"),
    "devices/iopin.sym": PORT.format(t="iopin", d="inout"),
    "devices/code_shown.sym": 'v {}\nK {type=netlist_commands\ntemplate="name=s1 value=blabla"\nformat="@value"}\n',
}


@pytest.fixture
def proj(tmp_path, monkeypatch):
    monkeypatch.delenv("UNDINE_TOOLS", raising=False)            # the symbols here, not an installed Xschem library's
    syms = tmp_path / "syms"
    for name, text in SYMBOLS.items():
        (syms / name).parent.mkdir(parents=True, exist_ok=True)
        (syms / name).write_text(text)
    srv = Server(tmp_path / "srv", {}, [tmp_path], llm_policy="none")
    prj = srv.store.create("p")
    prj.meta["symbol_paths"] = [str(syms)]
    prj.save()
    lib = prj.create_library("l")
    (lib.path / "top.sch").write_text(xschem.dump(XFile(version="xschem version=3.4.5 file_version=1.2\n")))
    return SimpleNamespace(srv=srv, prj=prj, lib=lib, syms=syms, ed=lambda cell="top", project="p": srv.editor(project, "l", cell, "schematic"))


def wires_of(ed):
    return [w[:4] for w in ed._load().wires]


def nets(ed) -> dict[str, set[str]]:
    c = schematic.extract(ed.path, ed.resolver)
    return {name: {f"{d}:{p}" for d, p in pins} for name, pins in c.nets.items() if pins}


def partition(ed) -> list[list[str]]:
    return sorted(sorted(v) for v in nets(ed).values())


# ------------------------------------------------------------------ the router: on the grid, direct, nothing reads as joined

def gate(x, y, name):
    """An OR gate: its back is a shallow arc of a circle of radius 100 centred far to the left, drawn off the grid."""
    sym = XFile(global_props={"type": "primitive"})
    sym.lines = [(4, -60, -20, -30, -20, {}), (4, -60, 20, -30, 20, {}), (4, 30, 0, 60, 0, {}),
                 (4, -30, -30, 0, -30, {}), (4, -30, 30, 0, 30, {})]
    sym.arcs = [(4, -125, 0, 100, 342.54, 34.92, {}), (4, 0, 0, 30, 270, 180, {})]
    sym.polys = [(4, [36.31948, -3.17, 43.68, 0, 36.31948, 3.17], {})]
    sym.boxes = [(5, -62.5, -22.5, -57.5, -17.5, {"name": "A"}), (5, -62.5, 17.5, -57.5, 22.5, {"name": "B"}),
                 (5, 57.5, -2.5, 62.5, 2.5, {"name": "X"})]
    return XComp("or.sym", x, y, 0, 0, {"name": name}, sym)


def inv(x, y, name):
    sym = XFile(global_props={"type": "primitive"})
    sym.lines = [(4, -20, -20, -20, 20, {}), (4, -20, -20, 20, 0, {}), (4, -20, 20, 20, 0, {}),
                 (4, -40, 0, -20, 0, {}), (4, 20, 0, 40, 0, {})]
    sym.boxes = [(5, -42.5, -2.5, -37.5, 2.5, {"name": "A"}), (5, 37.5, -2.5, 42.5, 2.5, {"name": "Y"})]
    return XComp("inv.sym", x, y, 0, 0, {"name": name}, sym)


def route(f, a, b, **kw):
    return schroute.route(f, SymbolResolver([]), Path("."), a, b, **kw)


def test_a_drawing_off_the_grid_keeps_wires_on_it():
    f = XFile(comps=[gate(0, 0, "x1"), inv(-150, 100, "x2")])
    for ob in schroute.body_obstacles(f, SymbolResolver([]), Path(".")):
        assert all(v % 10 == 0 for v in (ob.xmin, ob.ymin, ob.xmax, ob.ymax))       # keepouts end on the grid
    path = route(f, (60, 0), (-190, 100))                                           # around both bodies
    assert all(v % 10 == 0 for p in path for v in p), path


def test_a_shallow_arc_is_no_keepout_beyond_the_gate():
    # the arc's whole circle made a keepout far left of the gate: the route went round it (540 long, off the grid)
    f = XFile(comps=[gate(0, 0, "x1"), inv(-150, 200, "x2")])
    assert route(f, (-110, 200), (-60, -20)) == [(-110, 200), (-100, 200), (-100, -20), (-60, -20)]
    ob = schroute.body_obstacles(f, SymbolResolver([]), Path("."))[0]
    assert (ob.xmin, ob.ymin, ob.xmax, ob.ymax) == (-70, -50, 70, 50)


def test_keep_clear_bends_away_from_other_pins_and_their_leads():
    label = XFile(global_props={"type": "label"}, boxes=[(5, -2, -2, 2, 2, {"name": "p"})])
    f = XFile(comps=[XComp("lab.sym", 100, -10, 0, 0, {"name": "l1"}, label)])
    assert route(f, (0, 0), (100, 100)) == [(0, 0), (100, 0), (100, 100)]        # a bend right under the other pin
    assert route(f, (0, 0), (100, 100), keep_clear=True) == [(0, 0), (0, 100), (100, 100)]
    # a capacitor's lower pin (300, 90), its lead pointing down: a wire passing just below it reads like a short
    cap = XFile(global_props={"type": "capacitor"}, lines=[(4, 0, 30, 0, 50, {}), (4, 0, 70, 0, 90, {}),
                                                            (4, -10, 50, 10, 50, {}), (4, -10, 70, 10, 70, {})],
                boxes=[(5, -2, 28, 2, 32, {"name": "p"}), (5, -2, 88, 2, 92, {"name": "m"})])
    f = XFile(comps=[XComp("c.sym", 300, 0, 0, 0, {"name": "Ccmp"}, cap)])
    assert route(f, (200, 110), (400, 110)) == [(200, 110), (400, 110)]
    clear = route(f, (200, 100), (400, 100), keep_clear=True)
    assert not any(schematic._on_segment(300, 100, *p, *q) for p, q in zip(clear, clear[1:])), clear
    with pytest.raises(ValueError, match="readable"):           # a junction there: the caller tries another point
        route(XFile(comps=f.comps, wires=[(200, 100, 400, 100, {})]), (300, 100), (300, 200), keep_clear=True)


def test_a_testbench_net_does_not_end_or_pass_next_to_another_pin(proj):
    # the cdac testbench: vref ran a grid step from Ccmp's pin, and read as joined to it (here vref went along y=20,
    # through the end of the lead of Ccmp:P at (300, 30))
    ed = proj.ed()
    for name, x, y in (("Rv", 150, 130), ("Ccmp", 300, 60), ("Rd", 450, 130)):
        ed.add_instance("res.sym", x, y, props={"name": name})
    ed.connect_nets([("gnd", ["Ccmp:M"]), ("vref", ["Rv:P", "Rd:P"])], "auto", "devices/lab_pin.sym")
    assert nets(ed)["vref"] == {"Rv:P", "Rd:P"} and not any("Ccmp:M" in v for k, v in nets(ed).items() if k != "gnd")
    for pin, lead_end in (((300, 30), (300, 20)), ((300, 90), (300, 100))):
        for w in wires_of(ed):
            assert not schematic._on_segment(*lead_end, *w), w                      # not across the end of its lead
            for end in (w[:2], w[2:]):
                assert abs(end[0] - pin[0]) + abs(end[1] - pin[1]) > 10, w          # no wire end a grid step from it


def test_the_routing_budget_is_cpu_time_so_a_busy_machine_draws_the_same():
    import time
    f = XFile(comps=[gate(0, 0, "x1")])
    with schroute.time_budget(0.05):
        time.sleep(0.1)                                 # the wall clock is past the budget; the routing thread did nothing
        assert len(route(f, (-200, 0), (200, 0))) == 4  # a detour search, not a timeout


def test_wire_and_body_indexes_agree_with_checking_every_one():
    import random
    rnd = random.Random(3)
    coords = [0, 10, 20, 30, 35.5, -10, 9.9999995]
    wires = [((rnd.choice(coords), rnd.choice(coords)), (rnd.choice(coords), rnd.choice(coords))) for _ in range(40)]
    wires = [(a, b) for a, b in wires if a[0] == b[0] or a[1] == b[1]]
    idx = schroute._WireIndex(wires)

    def meets(a, b, c, d):
        if (a[1] == b[1]) == (c[1] == d[1]) and (a[0] == b[0]) == (c[0] == d[0]):     # parallel: overlap on one line
            return any(schematic._on_segment(*p, *c, *d) for p in (a, b)) or any(schematic._on_segment(*p, *a, *b) for p in (c, d))
        h, v = ((a, b), (c, d)) if a[1] == b[1] else ((c, d), (a, b))
        x, y = v[0][0], h[0][1]
        return schematic._on_segment(x, y, *h[0], *h[1]) and schematic._on_segment(x, y, *v[0], *v[1])

    for x in coords:
        for y in coords:
            for a, b in (((x - 10, y), (x + 10, y)), ((x, y - 10), (x, y + 10))):
                assert idx.touches(a, b) == any(meets(a, b, c, d) for c, d in wires), (a, b)
    obs = [schroute.Obstacle(x, y, x + rnd.choice([10, 50, 400]), y + 30) for x in range(-500, 500, 70) for y in (-90, 0, 90)]
    bodies = schroute._Bodies(obs)
    for a, b in (((-600, 0), (600, 0)), ((5, -100), (5, 100)), ((300, 10), (300, 10)), ((-1e6, 5), (1e6, 5))):
        assert {id(o) for o in obs if o.hits(a, b) or o.contains(a)} <= {id(o) for o in bodies.near(a, b)}


# ------------------------------------------------------------------ re-wiring drawn nets

def chain(p, *instances):
    ed = p.ed()
    for name, sym, x, y in instances:
        ed.add_instance(sym, x, y, props={"name": name}, placed="auto")     # as a tool places it
    return ed


def test_port_order_is_one_undoable_reviewed_change(proj):
    ed = chain(proj, *[(f"p{i}", f"devices/{t}.sym", 0, 40 * i) for i, t in enumerate(("ipin", "opin", "iopin"))])
    for i, lab in enumerate(("in", "out", "vdd")):
        ed.set_props("i", f"p{i}", {"lab": lab})
    assert [p["name"] for p in netlist.current(ed.path, ed.resolver)["ports"]] == ["in", "out", "vdd"]
    assert ed.set_port_order(["vdd", "in", "out"]) == ["vdd", "in", "out"]
    assert [p["name"] for p in netlist.current(ed.path, ed.resolver)["ports"]] == ["vdd", "in", "out"]
    fx = ed.log.read()[-1]["effects"]
    assert fx == [{"k": "o", "id": "ports", "id_after": "ports", "do": "mod",
                   "changes": {"order": [["p0", "p1", "p2"], ["p2", "p0", "p1"]], "ports": [["in", "out", "vdd"], ["vdd", "in", "out"]]}}]
    f = ed._load()
    oplog.apply(f, oplog.invert(fx), False)                       # a changeset's restore takes it back like this
    assert [c.props["lab"] for c in f.comps] == ["in", "out", "vdd"]
    assert ed.undo() and [c.props["lab"] for c in ed._load().comps] == ["in", "out", "vdd"]
    assert ed.redo() and [c.props["lab"] for c in ed._load().comps] == ["vdd", "in", "out"]
    for bad in (["vdd", "in"], ["vdd", "in", "out", "x"], ["vdd", "vdd", "out"]):
        with pytest.raises(ValueError, match="every port once"):
            ed.set_port_order(bad)


def test_a_pin_moves_to_another_net_and_the_rest_of_its_net_stays(proj):
    # x1:Y drives x2:A and x3:A; take x2:A off and put a new stage (x4) between x1:Y and x2:A
    ed = chain(proj, ("x1", "sc_inv.sym", 0, 0), ("x2", "sc_inv.sym", 300, 0), ("x3", "sc_inv.sym", 300, 200),
               ("x4", "sc_inv.sym", 150, -200))
    ed.connect_nets([("tog", ["x1:Y", "x2:A", "x3:A"])], "wire")
    with pytest.raises(ValueError, match="already on net tog"):
        ed.connect_nets([("tog_e", ["x4:Y", "x2:A"])], "wire")
    r = ed.connect_nets([("tog", ["x1:Y", "x4:A"]), ("tog_e", ["x4:Y", "x2:A"])], "wire", move=True)
    assert r[1]["moved_from"] == {"x2:A": "tog"}
    n = nets(ed)
    assert n["tog"] == {"x1:Y", "x3:A", "x4:A"} and n["tog_e"] == {"x4:Y", "x2:A"}
    assert all(v % 10 == 0 for w in wires_of(ed) for v in w)
    assert ed.undo() and nets(ed)["tog"] == {"x1:Y", "x2:A", "x3:A"}           # one step


def test_disconnect_rejoins_what_it_cuts_apart_and_keeps_the_name(proj):
    ed = chain(proj, ("x1", "sc_inv.sym", 0, 0), ("x2", "sc_inv.sym", 300, 0), ("x3", "sc_inv.sym", 300, 200))
    ed.connect_nets([("a", ["x2:A", "x1:Y", "x3:A"])], "wire")
    # route x1:Y -> x2:A first (carrying the name), x3:A then joins it: taking x2:A off removes the named wire
    assert ed.disconnect(["x2:A"]) == {"x2:A": "a"}
    n = nets(ed)
    assert n["a"] == {"x1:Y", "x3:A"} and not any("x2:A" in v for k, v in n.items() if k != "a" and len(v) > 1)
    assert all(v % 10 == 0 for w in wires_of(ed) for v in w)
    with pytest.raises(KeyError, match="no pin nope:p"):
        ed.disconnect(["nope:p"])
    ed.add_instance("devices/ipin.sym", -300, 0, props={"name": "pin", "lab": "a"})
    with pytest.raises(ValueError, match="label or port"):
        ed.disconnect(["pin:p"])


def test_disconnect_removes_a_label_on_the_pin_and_refuses_a_port(proj):
    ed = chain(proj, ("x1", "sc_inv.sym", 0, 0), ("x2", "sc_inv.sym", 0, 200))
    ed.connect_nets([("vdd", ["x1:A", "x2:A"])], "label", "devices/lab_pin.sym")
    assert ed.disconnect(["x1:A"]) == {"x1:A": "vdd"}
    assert sum(c.sym.endswith("lab_pin.sym") for c in ed._load().comps) == 1 and nets(ed)["vdd"] == {"x2:A"}
    ed.add_instance("devices/opin.sym", 40, 200, props={"name": "po", "lab": "out"})       # on x2:Y
    with pytest.raises(ValueError, match="port po sits on it"):
        ed.disconnect(["x2:Y"])


def test_label_net_replaces_a_long_wire_and_leaves_the_circuit(proj):
    ed = chain(proj, ("x1", "sc_inv.sym", 0, 0), ("x2", "sc_inv.sym", 1000, 0), ("x3", "sc_inv.sym", 1000, 200))
    ed.connect_nets([("far", ["x1:Y", "x2:A", "x3:A"])], "wire")
    before = partition(ed)
    long_wires = [xschem.wire_id(*w) for w in wires_of(ed) if abs(w[0] - w[2]) > 500]
    with pytest.raises(KeyError, match="not wires of far"):
        ed.label_net("far", ["1,2,3,4"])
    r = ed.label_net("far", long_wires)
    assert set(long_wires) <= set(r["wires_removed"]) and r["labelled"]
    assert partition(ed) == before and nets(ed)["far"] == {"x1:Y", "x2:A", "x3:A"}
    assert not [w for w in wires_of(ed) if abs(w[0] - w[2]) > 500]
    ends = [p for w in wires_of(ed) for p in (w[:2], w[2:])]
    pins = {(x, y) for d in schematic.extract(ed.path, ed.resolver).devices for _, x, y in d.pins}
    assert all(p in pins or ends.count(p) > 1 or any(schematic._on_segment(*p, *w) for w in wires_of(ed) if w[:2] != p and w[2:] != p)
               for p in ends)                                      # no wire left dangling
    assert ed.undo() and partition(ed) == before


def test_move_carries_its_wires_and_is_the_movers_choice(proj):
    ed = chain(proj, ("x1", "sc_inv.sym", 0, 0), ("x2", "sc_inv.sym", 300, 0))
    ed.connect_nets([("a", ["x1:Y", "x2:A"])], "wire")
    assert ed._load().comps[1].props.get(netlist.PLACED) == "auto"
    ed.move([{"k": "i", "id": "x2"}], 0, 100, owned=True)
    assert nets(ed)["a"] == {"x1:Y", "x2:A"} and netlist.PLACED not in ed._load().comps[1].props


def test_drawing_lists_each_nets_wires_and_what_overlaps(proj):
    ed = chain(proj, ("x1", "sc_inv.sym", 0, 0), ("x2", "sc_inv.sym", 300, 0), ("R1", "res.sym", 150, -10))
    ed.set_props("i", "R1", {"value": "PWL(0 0 1u 1.8 2u 0 3u 1.8 4u 0 5u 1.8 6u 0 7u 1.8 8u 0)"})
    proj.syms.joinpath("res.sym").write_text(SYMBOLS["res.sym"] + "T {@value} 15 -5 0 0 0.2 0.2 {}\n")
    ed.resolver.cache.clear()
    ed.connect_nets([("a", ["x1:Y", "x2:A"])], "wire")
    d = ed.drawing()
    assert set(d["nets"]["a"]["pins"]) == {"x1:Y", "x2:A"} and d["nets"]["a"]["wires"] == [xschem.wire_id(*w) for w in wires_of(ed)]
    assert d["instances"]["R1"]["extent"][2] > 300                  # the long value is part of its extent
    assert ["R1", "x2"] in d["overlaps"]
    assert set(ed.drawing(net="a")["instances"]) == {"x1", "x2"}
    with pytest.raises(KeyError, match="no net zz"):
        ed.drawing(net="zz")


# ------------------------------------------------------------------ symbols from the schematic's ports

def test_make_symbol_puts_ports_on_their_sides():
    f = xschem.make_symbol([("in", "ipin"), ("out", "opin"), ("vdd", "iopin"), ("vss", "iopin")], {"vdd": "N", "vss": "S"})
    pins = {p.name: (p.x, p.y) for p in schematic.sym_pins(f)}
    assert [p.name for p in schematic.sym_pins(f)] == ["in", "out", "vdd", "vss"]
    assert symedit.pin_sides(f) == {"in": "W", "out": "E", "vdd": "N", "vss": "S"}
    assert pins["vdd"][0] == 0 and pins["vdd"][1] < 0 < pins["vss"][1] and all(v % 10 == 0 for p in pins.values() for v in p)
    sym_text = next(t for t in f.texts if t.s == "@symname")
    assert sym_text.y < pins["vdd"][1]                                # the cell name clears the top pin
    assert symedit.generated(f) and not symedit.generated(XFile(lines=f.lines, arcs=[(4, 0, 0, 5, 0, 360, {})]))


def hand_drawn() -> XFile:
    """An amplifier triangle with pins b, a, z (in that order) and plain texts, no undine leads."""
    f = XFile(version="xschem version=3.4.5 file_version=1.2\n", global_props={"type": "subcircuit"},
              k_raw="type=subcircuit\n")
    f.lines = [(4, -40, -40, -40, 40, {}), (4, -40, -40, 40, 0, {}), (4, -40, 40, 40, 0, {}),
               (4, -60, -20, -40, -20, {}), (4, -60, 20, -40, 20, {}), (4, 40, 0, 60, 0, {})]
    f.boxes = [(5, -62.5, 17.5, -57.5, 22.5, {"name": "b", "dir": "in"}), (5, -62.5, -22.5, -57.5, -17.5, {"name": "a", "dir": "in"}),
               (5, 57.5, -2.5, 62.5, 2.5, {"name": "z", "dir": "out"})]
    f.texts = [xschem.XText("b", -55, 10, 0, 0, 0.2, 0.2, {}), xschem.XText("a", -55, -30, 0, 0, 0.2, 0.2, {}),
               xschem.XText("@symname", -40, -60, 0, 0, 0.3, 0.3, {})]
    return f


def test_a_hand_drawn_symbol_keeps_its_drawing():
    f = hand_drawn()
    out, rep = symedit.from_ports(f, [("a", "ipin"), ("b", "ipin"), ("z", "opin")])
    assert [p.name for p in schematic.sym_pins(out)] == ["a", "b", "z"] and rep["order_changed"]
    assert out.lines == f.lines and out.texts == f.texts and not rep.get("regenerated")      # only the order changed
    assert {p.name: (p.x, p.y) for p in schematic.sym_pins(out)} == {p.name: (p.x, p.y) for p in schematic.sym_pins(f)}
    out, rep = symedit.from_ports(f, [("a", "ipin"), ("z", "opin"), ("en", "ipin"), ("vdd", "iopin")], {"vdd": "N"})
    assert rep["removed"] == ["b"] and rep["added"] == ["en", "vdd"]
    assert not any(t.s == "b" for t in out.texts) and (-60, 20, -40, 20, {}) not in [ln[1:] for ln in out.lines]
    pins = {p.name: (p.x, p.y) for p in schematic.sym_pins(out)}
    assert symedit.pin_sides(out) == {"a": "W", "z": "E", "en": "W", "vdd": "N"}
    assert pins["en"][0] == -60 and abs(pins["en"][1] - pins["a"][1]) >= 20 and pins["vdd"][1] == -60
    assert all(ln in out.lines for ln in f.lines if ln[1:5] != (-60, 20, -40, 20))       # the body is untouched


def test_a_box_symbol_is_redrawn_for_new_sides():
    f = xschem.make_symbol([("in", "ipin"), ("out", "opin"), ("vdd", "iopin")])
    out, rep = symedit.from_ports(f, [("vdd", "iopin"), ("in", "ipin"), ("out", "opin")], {"vdd": "N"})
    assert rep["regenerated"] and rep["moved"] == {"vdd": ["E", "N"]}
    assert symedit.pin_sides(out) == {"vdd": "N", "in": "W", "out": "E"} and [p.name for p in schematic.sym_pins(out)] == ["vdd", "in", "out"]


def write_amp(proj, ports):
    """Cell amp's schematic, as changed outside Undine: its ports as ipin / opin / iopin instances in this order."""
    f = XFile(version="xschem version=3.4.5 file_version=1.2\n")
    f.comps = [XComp(f"devices/{t}.sym", 0, 40 * i, 0, 0, {"name": f"p{i}", "lab": n}) for i, (n, t) in enumerate(ports)]
    (proj.lib.path / "amp.sch").write_text(xschem.dump(f))


def test_sync_symbol_follows_the_ports_and_the_testbench_keeps_its_connections(proj):
    write_amp(proj, [("in", "ipin"), ("out", "opin"), ("vdd", "iopin")])
    srv = proj.srv
    assert symbolsync.from_schematic(srv, "p", "l", "amp")["created"]
    tb = proj.ed("tb")
    tb.create_file(xschem.dump(XFile(version="xschem version=3.4.5 file_version=1.2\n")))
    tb.add_instance("amp.sym", 0, 0, props={"name": "x1"})
    pins = {(d.name, pn): (x, y) for d in schematic.extract(tb.path, tb.resolver).devices for pn, x, y in d.pins}
    for (inst, pn), (x, y) in pins.items():
        tb.add_wire(x, y, x + (-60 if x < 0 else 60), y, f"n_{pn}")
    connected = nets(tb)
    text = tb.path.read_text()
    write_amp(proj, [("vdd", "iopin"), ("in", "ipin"), ("out", "opin")])           # the port order changed
    rep = symbolsync.from_schematic(srv, "p", "l", "amp", actor="agent:t", reason="order")
    assert rep["order"] == ["vdd", "in", "out"] and rep["consumers"] == ["l/tb"] and not rep.get("affected")
    assert tb.path.read_text() == text and nets(tb) == connected                     # nothing moved there
    rep = symbolsync.from_schematic(srv, "p", "l", "amp", {"vdd": "N"}, actor="agent:t", reason="supply on top")
    assert rep["affected"] == ["l/tb"] and nets(proj.ed("tb")) == connected        # its wiring followed the pin
    moved = proj.ed("tb").path.read_text()
    out = symbolsync.edit(srv, "p", "l", "amp", "symbol", "undo", {})             # symbol and wiring undo together
    assert out["result"] and symedit.pin_sides(xschem.parse((proj.lib.path / "amp.sym").read_text()))["vdd"] == "E"
    assert proj.ed("tb").path.read_text() != moved and nets(proj.ed("tb")) == connected
    write_amp(proj, [("vdd", "iopin"), ("in", "ipin")])                              # out is wired in tb: refused
    with pytest.raises(ValueError, match="pin out of x1 is connected"):
        symbolsync.from_schematic(srv, "p", "l", "amp")


def placed_amp(proj, ports):
    """amp (schematic with `ports`, the hand-drawn symbol) placed as x1 in tb, pins a and z wired."""
    write_amp(proj, ports)
    (proj.lib.path / "amp.sym").write_text(xschem.dump(hand_drawn()))
    tb = proj.ed("tb")
    tb.create_file(xschem.dump(XFile(version="xschem version=3.4.5 file_version=1.2\n")))
    tb.add_instance("amp.sym", 0, 0, props={"name": "x1"})
    tb.add_wire(-60, -20, -120, -20, "n_a")
    tb.add_wire(60, 0, 120, 0, "n_z")
    return tb


def x1_nets(proj) -> dict[str, str]:
    """What tb's netlist connects x1's pins to, as the netlisters read it (a pin it lacks would be node 0)."""
    tb = proj.ed("tb")
    d = next(d for d in netlist.connectivity(tb.path, tb.resolver, "tb").devices if d.name == "x1")
    return {pn: d.nets.get(pn, "0") for pn, _, _ in d.pins}


def test_a_pin_the_symbol_gains_again_is_on_a_net_of_its_own_in_every_user(proj):
    # tb's drawing does not change when b goes and comes back, so only reading its netlist again keeps b off node 0
    placed_amp(proj, [("a", "ipin"), ("b", "ipin"), ("z", "opin")])
    write_amp(proj, [("a", "ipin"), ("z", "opin")])                     # b, unconnected in tb, is no port any more
    rep = symbolsync.from_schematic(proj.srv, "p", "l", "amp", actor="agent:t", reason="drop b")
    assert rep["removed"] == ["b"] and not rep.get("affected") and x1_nets(proj) == {"a": "n_a", "z": "n_z"}
    symbolsync.edit(proj.srv, "p", "l", "amp", "symbol", "undo", {})                 # a person undoes it in the symbol editor
    got = x1_nets(proj)
    assert list(got) == ["b", "a", "z"] and got["b"] not in ("0", "n_a", "n_z")
    symbolsync.edit(proj.srv, "p", "l", "amp", "symbol", "symbol_add", {"kind": "pin", "x": 0, "y": 60, "name": "en", "dir": "in"})
    got = x1_nets(proj)
    assert got["en"] not in ("0", got["b"], "n_a", "n_z")
    symbolsync.edit(proj.srv, "p", "l", "amp", "symbol", "undo", {})
    assert "en" not in netlist.current(proj.ed("tb").path, proj.ed("tb").resolver)["instances"]["x1"]["pins"]


def test_a_pin_with_a_pending_connection_is_not_taken_out(proj):
    write_amp(proj, [("in", "ipin"), ("out", "opin")])
    symbolsync.from_schematic(proj.srv, "p", "l", "amp")
    tb = proj.ed("tb")
    tb.create_file(xschem.dump(XFile(version="xschem version=3.4.5 file_version=1.2\n")))
    tb.add_instance("amp.sym", 0, 0, props={"name": "x1"})
    tb.add_instance("res.sym", 300, 0, props={"name": "R1"})
    nl = proj.srv.netlist_editor("p", "l", "tb")
    nl.connect("x1", "out", "o")                                        # in the netlist, not drawn
    nl.connect("R1", "P", "o")
    write_amp(proj, [("in", "ipin"), ("vout", "opin")])                  # out renamed vout
    with pytest.raises(ValueError, match="pin out of x1 is connected to o .a pending connection"):
        symbolsync.from_schematic(proj.srv, "p", "l", "amp")
    assert netlist.current(tb.path, tb.resolver)["pending"] == {"R1.P": "o", "x1.out": "o"}
    assert [p.name for p in schematic.sym_pins(xschem.parse((proj.lib.path / "amp.sym").read_text()))] == ["in", "out"]


# ------------------------------------------------------------------ standard-cell supplies

def test_supply_parameters_are_connections_and_a_name_nothing_is_on_warns(proj):
    ed = chain(proj, ("x1", "sc_inv.sym", 0, 0), ("x2", "sc_inv.sym", 200, 0), ("pv", "devices/iopin.sym", -200, -100),
               ("pg", "devices/iopin.sym", -200, -60))
    ed.set_props("i", "pv", {"lab": "vdd"})
    ed.set_props("i", "pg", {"lab": "vss"})
    ed.set_props("i", "x1", {"VPWR": "vdd", "VPB": "vdd", "VGND": "vss", "VNB": "vss"})
    ed.set_props("i", "x2", {"VPWR": "vdd", "VPB": "vdd", "VGND": "vss"})          # VNB left at its default
    model = netlist.current(ed.path, ed.resolver)
    sup = netlist.supplies(ed.path, ed.resolver, model)
    assert sup["x1"] == {"VGND": "vss", "VNB": "vss", "VPB": "vdd", "VPWR": "vdd"} and sup["x2"]["VNB"] == "VNB"
    warn = netlist.supply_warnings(model, sup)
    assert len(warn) == 1 and warn[0].startswith("VNB=VNB on x2 names no net")
    conn = schematic.connectivity(ed.path, ed.resolver, "top")
    assert warn[0] in conn.warnings                                 # the netlisters warn too
    assert warn[0] in ed.view()["warnings"]                          # and the schematic editor
    v = proj.srv.netlist_view("p", "l", "top")
    assert v["supplies"]["x1"]["VPWR"] == "vdd" and v["warnings"] == warn
    from undine.agents import llm_context
    text, _ = llm_context.build(proj.srv, "p", {"lib": "l", "cell": "top", "selection": [{"k": "n", "id": "vss"}]})
    assert "- net vss: pins none; supply parameters x1.VGND, x1.VNB, x2.VGND; port vss (iopin)" in text


def test_a_supply_a_code_block_drives_or_a_label_names_is_a_net(proj):
    # a testbench puts the cells straight on supplies its code block defines: the netlist connects them, no warning
    ed = chain(proj, ("x1", "sc_inv.sym", 0, 0), ("x2", "sc_inv.sym", 200, 0), ("s1", "devices/code_shown.sym", 0, -300),
               ("l1", "devices/lab_pin.sym", 400, -200))
    ed.set_props("i", "s1", {"value": "Vdd VDD 0 1.8\nVss vss 0\n+ 0\n.global vg"})
    ed.set_props("i", "l1", {"lab": "vlab"})
    ed.set_props("i", "x1", {"VPWR": "vdd", "VPB": "vg", "VGND": "vss", "VNB": "vss"})      # vdd: VDD to SPICE
    ed.set_props("i", "x2", {"VPWR": "vlab", "VPB": "vlab", "VGND": "0", "VNB": "vsss"})    # vsss: a typo
    model = netlist.current(ed.path, ed.resolver)
    assert {"vdd", "vss", "vg", "0"} <= netlist.code_nodes(model)
    warn = netlist.supply_warnings(model, netlist.supplies(ed.path, ed.resolver, model), ed.path)
    assert len(warn) == 1 and warn[0].startswith("VNB=vsss on x2 names no net")
    assert schematic.connectivity(ed.path, ed.resolver, "top").warnings == warn
    assert proj.srv.netlist_view("p", "l", "top")["warnings"] == warn


# ------------------------------------------------------------------ the agent tools

@pytest.fixture
def agent(proj):
    ctx = T.ToolCtx(proj.srv, "p", actor="agent:t")
    T.call(ctx, "open_changeset", {"title": "edit", "reason": "r"})

    def call(name, **a):
        res, err = T.call(ctx, name, {"lib": "l", **a})
        assert not err, res
        return json.loads(res) if res.startswith("{") else res

    def fails(name, **a):
        res, err = T.call(ctx, name, {"lib": "l", **a})
        assert err, res
        return res
    return SimpleNamespace(ctx=ctx, call=call, fails=fails, cs=Changesets(proj.srv), proj=proj)


def test_agent_finds_and_fixes_a_symbol_out_of_step(agent):
    call = agent.call
    call("create_cell", cell="amp", views=["schematic"], reason="new")
    for i, (lab, t) in enumerate((("in", "ipin"), ("out", "opin"), ("vdd", "iopin"), ("vss", "iopin"))):
        call("add_instance", cell="amp", symbol=f"devices/{t}.sym", x=200, y=40 * i, params={"lab": lab}, reason="port")
    call("create_cell", cell="amp", views=["symbol"], reason="symbol")
    assert "sync_symbol" in agent.fails("create_cell", cell="amp", views=["symbol"], reason="again")
    call("set_port_order", cell="amp", order=["vdd", "vss", "in", "out"], symbol=False, reason="supplies first")
    chk = call("check_symbol", cell="amp")
    assert not chk["match"] and chk["issues"][0]["code"] == "pin_order_mismatch"
    assert chk["fix"]["sync_symbol would"]["order"] == ["vdd", "vss", "in", "out"]
    ctxt = call("get_design_context", cell="amp")
    assert "## Schematic checks" in ctxt and ctxt.index("## Schematic checks") < ctxt.index("## Schematic of amp")
    checks = call("run_checks")["checks"]
    assert any(r["kind"] == "schematic" and r["cell"] == "amp" and not r["ok"] and "pin order" in r["detail"] for r in checks)
    call("set_port_intent", cell="amp", port="vdd", side="N", reason="supply on top")
    call("set_port_intent", cell="amp", port="vss", side="S", reason="ground at the bottom")
    assert call("check_symbol", cell="amp")["sides_differ_from_intent"] == {"vdd": {"symbol": "E", "intent": "N"},
                                                                          "vss": {"symbol": "E", "intent": "S"}}
    res = call("sync_symbol", cell="amp", reason="follow the schematic")
    assert res["order"] == ["vdd", "vss", "in", "out"] and res["sides"] == {"vdd": "N", "vss": "S", "in": "W", "out": "E"}
    assert call("check_symbol", cell="amp")["match"]
    call("set_port_order", cell="amp", order=["in", "out", "vdd", "vss"], reason="back")     # the symbol follows by default
    assert call("check_symbol", cell="amp")["match"]
    ops = [(o["view"], o["op"]) for lc in agent.cs.get("p", agent.ctx.changeset)["changes"] for o in lc["ops"]]
    assert ("symbol", "sync_symbol") in ops and ("symbol", "set_port_order") in ops and ("schematic", "set_port_order") in ops


def test_a_symbol_out_of_step_before_the_changeset_comes_with_the_baselines_row(agent):
    """run_checks on a changeset that edits a schematic whose symbol was already out of step before it (the pin order):
    the failing work row comes with the baseline's row and says unchanged_from_baseline; a finding the changeset adds
    is new_in_work."""
    call = agent.call
    call("create_cell", cell="amp", views=["schematic"], reason="new")
    names = [call("add_instance", cell="amp", symbol=f"devices/{t}.sym", x=200, y=40 * i, params={"lab": lab},
                  reason="port")["instance"]
             for i, (lab, t) in enumerate((("in", "ipin"), ("out", "opin"), ("vdd", "iopin"), ("vss", "iopin")))]
    call("create_cell", cell="amp", views=["symbol"], reason="symbol")
    call("set_port_order", cell="amp", order=["vdd", "vss", "in", "out"], symbol=False, reason="supplies first")
    agent.cs.apply("p", agent.ctx.changeset)
    call("open_changeset", title="tidy", reason="r")
    call("move_instance", cell="amp", view="schematic", instance=names[0], dx=0, dy=-20, reason="room")
    rows = [r for r in call("run_checks")["checks"] if r["kind"] == "schematic" and r["cell"] == "amp"]
    assert [r["side"] for r in rows] == ["base", "work"]
    base, work = rows
    assert not base["ok"] and "pin order" in base["detail"] and not work["ok"]
    assert work["unchanged_from_baseline"] is True and "new_in_work" not in work
    call("set_instance_params", cell="amp", instance=names[1], params={"lab": "vdd"}, reason="a short")
    work = next(r for r in call("run_checks")["checks"] if r["kind"] == "schematic" and r["side"] == "work")
    assert "unchanged_from_baseline" not in work and work.get("new_in_work"), work


def test_agent_rewires_reads_wires_and_moves_symbols(agent):
    call = agent.call
    call("create_cell", cell="logic", views=["schematic"], reason="new")
    call("add_instances", cell="logic", reason="cells", instances=[
        {"symbol": "sc_inv.sym", "x": 0, "y": 0, "params": {"name": "xdl5"}},
        {"symbol": "sc_inv.sym", "x": 600, "y": 0, "params": {"name": "xrdy"}},
        {"symbol": "sc_inv.sym", "x": 300, "y": 200, "params": {"name": "xd6"}}])
    call("connect_nets", cell="logic", nets={"tog_d": ["xdl5:Y", "xrdy:A"]}, reason="c")
    d = call("get_drawing", cell="logic", net="tog_d")
    assert d["nets"]["tog_d"]["pins"] == ["xdl5:Y", "xrdy:A"] and d["nets"]["tog_d"]["wires"]
    err = agent.fails("connect_nets", cell="logic", nets={"tog_e": ["xd6:Y", "xrdy:A"]}, reason="x")
    assert "move: true" in err
    res = call("connect_nets", cell="logic", nets={"tog_d": ["xdl5:Y", "xd6:A"], "tog_e": ["xd6:Y", "xrdy:A"]}, move=True,
               reason="insert a delay stage")
    assert res["moved_from"] == {"xrdy:A": "tog_d"}
    nl = call("get_netlist", cell="logic")
    assert nl["instances"]["xd6"]["pins"] == {"A": "tog_d", "Y": "tog_e"} and nl["instances"]["xrdy"]["pins"]["A"] == "tog_e"
    assert nl["instances"]["xd6"]["supply_pins"]["VPWR"] == "VPWR" and any("VPWR=VPWR" in w for w in nl["warnings"])
    assert nl["instances"]["xd6"]["params"] == {"prefix": "sky130_fd_sc_hd__"}       # a supply once, in supply_pins
    res = call("label_net", cell="logic", net="tog_e", reason="a label reads better here")
    assert res["wires_removed"] and len(res["labelled"]) == 2
    assert call("get_netlist", cell="logic")["instances"]["xrdy"]["pins"]["A"] == "tog_e"
    res = call("move_instance", cell="logic", view="schematic", instance="xd6", dx=4, dy=96, reason="room")
    assert res["by"] == [0, 100]
    assert call("get_drawing", cell="logic", instances=["xd6"])["instances"]["xd6"]["at"] == [300, 300]
    res = call("disconnect_pins", cell="logic", pins=["xd6:A"], reason="free it")
    assert res["disconnected"] == {"xd6:A": "tog_d"}
    assert call("get_netlist", cell="logic")["instances"]["xd6"]["pins"]["A"] != "tog_d"
    row = next(r for r in call("run_checks")["checks"] if r["kind"] == "schematic")
    assert not row["ok"] and "VPWR=VPWR" in row["detail"]
    items = [(o["op"], [e["k"] for e in o["effects"]]) for lc in agent.cs.get("p", agent.ctx.changeset)["changes"] for o in lc["ops"]]
    assert [op for op, _ in items].count("move") == 1 and ("label_net" in [op for op, _ in items])


def test_set_port_order_reorders_the_symbol_only_and_says_what_else_differs(agent, monkeypatch):
    call = agent.call
    call("create_cell", cell="amp", views=["schematic"], reason="new")
    for i, (lab, t) in enumerate((("in", "ipin"), ("out", "opin"), ("vdd", "iopin"))):
        call("add_instance", cell="amp", symbol=f"devices/{t}.sym", x=200, y=40 * i, params={"lab": lab}, reason="port")
    call("create_cell", cell="amp", views=["symbol"], reason="symbol")
    call("create_cell", cell="tb", views=["schematic"], reason="tb")
    call("add_instances", cell="tb", reason="r", instances=[{"symbol": "amp.sym", "x": 0, "y": 0, "params": {"name": "x1"}},
                                                             {"symbol": "res.sym", "x": 300, "y": 0, "params": {"name": "R1"}}])
    call("connect_nets", cell="tb", nets={"o": ["x1:out", "R1:P"]}, style="wire", reason="load")
    work = agent.proj.srv.project(agent.ctx.work).library("l").path
    sym, tb = (work / "amp.sym").read_text(), (work / "tb.sch").read_text()
    port = next(k for k, v in call("get_drawing", cell="amp")["instances"].items() if v.get("port") == "out")
    call("delete_objects", cell="amp", objects=[{"kind": "instance", "id": port}], reason="rename out")
    call("add_instance", cell="amp", symbol="devices/opin.sym", x=200, y=200, params={"lab": "vout"}, reason="as vout")
    # out is wired in tb, so a full sync is refused; the order alone is not, and nothing is added, removed or moved
    res = call("set_port_order", cell="amp", order=["vout", "in", "vdd"], reason="output first")
    assert res["order"] == ["vout", "in", "vdd"] and res["symbol"]["order"] == ["in", "vdd", "out"]
    assert res["symbol"]["not_in_symbol"] == ["vout"] and "sync_symbol" in res["symbol"]["note"]
    after = xschem.parse((work / "amp.sym").read_text())
    assert {p.name: (p.x, p.y) for p in schematic.sym_pins(after)} == {p.name: (p.x, p.y) for p in schematic.sym_pins(xschem.parse(sym))}
    assert (work / "tb.sch").read_text() == tb
    # the symbol step failing leaves the schematic's order set, and says so
    monkeypatch.setattr(symbolsync, "from_schematic", lambda *a, **k: (_ for _ in ()).throw(ValueError("refused")))
    res = call("set_port_order", cell="amp", order=["in", "vout", "vdd"], reason="back")
    assert res["ok"] and res["symbol"]["error"] == "refused" and "order is set" in res["symbol"]["note"]
    assert [p["name"] for p in call("get_netlist", cell="amp")["ports"]] == ["in", "vout", "vdd"]


def test_a_large_drawing_and_netlist_put_overlaps_and_warnings_first(agent):
    # sar_logic's size: 120 cells with their supplies at the template's default, a code block over some of them
    work = agent.proj.srv.project(agent.ctx.work).library("l").path
    f = XFile(version="xschem version=3.4.5 file_version=1.2\n")
    f.comps = [XComp("sc_inv.sym", 200 * (i % 12), 160 * (i // 12), 0, 0, {"name": f"x{i}"}) for i in range(120)]
    f.comps.append(XComp("res.sym", 100, 0, 0, 0, {"name": "R1", "value": "PWL(" + " 1u 1.8" * 40 + ")"}))
    (work / "logic.sch").write_text(xschem.dump(f))
    agent.proj.syms.joinpath("res.sym").write_text(SYMBOLS["res.sym"] + "T {@value} 15 -5 0 0 0.2 0.2 {}\n")
    res, err = T.call(agent.ctx, "get_drawing", {"lib": "l", "cell": "logic"})
    assert not err and len(res) <= T.RESULT_CHARS
    d = json.loads(res)
    assert list(d)[:2] == ["overlaps", "note"] and ["R1", "x1"] in d["overlaps"] and "region" in d["note"]
    part = agent.call("get_drawing", cell="logic", region=[-100, -100, 250, 100])
    assert set(part["instances"]) == {"x0", "x1", "R1"} and part["instances"]["x1"]["at"] == [200, 0]
    assert all(p.split(":")[0] in part["instances"] for n in part["nets"].values() for p in n["pins"])
    one = agent.call("get_drawing", cell="logic", instances=["x5"])               # the nets of its pins only
    assert set(one["instances"]) == {"x5"} and sorted(p for n in one["nets"].values() for p in n["pins"]) == ["x5:A", "x5:Y"]
    res, err = T.call(agent.ctx, "get_netlist", {"lib": "l", "cell": "logic"})
    assert not err and res.index('"warnings"') < res.index('"instances"') < T.RESULT_CHARS
    assert "VPWR=VPWR on x0, x1" in res


def test_yes_no_arguments_sent_as_text_are_not_all_true(agent):
    # `undine call move=false` sends the text 'false', which bool() reads as true: each tool gets a boolean
    call, work = agent.call, agent.proj.srv.project(agent.ctx.work).library("l").path
    call("create_cell", cell="logic", views=["schematic"], reason="new")
    call("add_instances", cell="logic", reason="cells", instances=[
        {"symbol": "sc_inv.sym", "x": 0, "y": 0, "params": {"name": "xa"}},
        {"symbol": "sc_inv.sym", "x": 600, "y": 0, "params": {"name": "xb"}},
        {"symbol": "sc_inv.sym", "x": 300, "y": 200, "params": {"name": "xc"}}])
    call("connect_nets", cell="logic", nets={"n1": ["xa:Y", "xb:A"]}, reason="c")
    err = agent.fails("connect_nets", cell="logic", nets={"n2": ["xc:Y", "xb:A"]}, move="false", reason="no move")
    assert "move: true" in err
    assert call("get_netlist", cell="logic", instances=["xb"])["instances"]["xb"]["pins"]["A"] == "n1"
    assert "move is a string ('no'), not true or false" in agent.fails("connect_nets", cell="logic", nets={"n2": ["xc:Y"]},
                                                                      move="no", reason="x")
    assert call("connect_nets", cell="logic", nets={"n2": ["xc:Y", "xb:A"]}, move="True", reason="move")["moved_from"] == {"xb:A": "n1"}
    # set_port_order with symbol=false leaves the symbol's pins in their order
    call("create_cell", cell="amp", views=["schematic"], reason="new")
    for i, (lab, t) in enumerate((("in", "ipin"), ("out", "opin"), ("vdd", "iopin"))):
        call("add_instance", cell="amp", symbol=f"devices/{t}.sym", x=200, y=40 * i, params={"lab": lab}, reason="port")
    call("create_cell", cell="amp", views=["symbol"], reason="symbol")
    res = call("set_port_order", cell="amp", order=["vdd", "in", "out"], symbol="false", reason="supply first")
    assert res["order"] == ["vdd", "in", "out"] and "symbol" not in res
    assert [p.name for p in schematic.sym_pins(xschem.parse((work / "amp.sym").read_text()))] == ["in", "out", "vdd"]
    # sync_symbol with regenerate=false keeps a hand-drawn symbol's drawing (only the order changes)
    f = XFile(version="xschem version=3.4.5 file_version=1.2\n")
    f.comps = [XComp(f"devices/{t}.sym", 0, 40 * i, 0, 0, {"name": f"p{i}", "lab": n})
               for i, (n, t) in enumerate((("a", "ipin"), ("b", "ipin"), ("z", "opin")))]
    (work / "tri.sch").write_text(xschem.dump(f))
    (work / "tri.sym").write_text(xschem.dump(hand_drawn()))
    res = call("sync_symbol", cell="tri", regenerate="false", reason="follow the order")
    assert res["order"] == ["a", "b", "z"] and not res.get("regenerated")
    assert xschem.parse((work / "tri.sym").read_text()).lines == hand_drawn().lines            # the triangle stays


def test_supplies_given_as_parameters_are_on_their_nets_for_the_readers(agent):
    # a testbench puts standard cells on vdd by their supply parameters only, and a code block drives vdd
    call = agent.call
    call("create_cell", cell="tb", views=["schematic"], reason="new")
    sup = {"VPWR": "vdd", "VPB": "vdd", "VGND": "0", "VNB": "0"}
    call("add_instances", cell="tb", reason="cells", instances=[
        {"symbol": "sc_inv.sym", "x": 0, "y": 0, "params": {"name": "xa", **sup}},
        {"symbol": "sc_inv.sym", "x": 300, "y": 0, "params": {"name": "xb", **sup}},
        {"symbol": "res.sym", "x": 600, "y": 0, "params": {"name": "R1"}},
        {"symbol": "devices/code_shown.sym", "x": 0, "y": -300, "params": {"name": "s1", "value": "Vdd VDD 0 1.8"}}])
    call("connect_nets", cell="tb", nets={"a": ["xa:Y", "xb:A"]}, reason="chain")
    on = call("get_netlist", cell="tb", nets=["vdd"])
    assert list(on["instances"]) == ["xa", "xb"] and on["shown"]["instances"] == 2
    assert on["nets"] == {"vdd": ["xa:VPB", "xa:VPWR", "xb:VPB", "xb:VPWR"]}
    assert [x["name"] for x in on["code"]] == ["s1"]                                  # what drives it
    a = call("get_netlist", cell="tb", nets=["a"])
    assert list(a["instances"]) == ["xa", "xb"] and a["nets"] == {"a": ["xa:Y", "xb:A"]} and "code" not in a
    text = call("get_design_context", cell="tb", objects=["vdd", "zzz"])
    assert "- net vdd: pins none; supply parameters xa.VPB, xa.VPWR, xb.VPB, xb.VPWR; named in code block s1" in text
    assert "- zzz: no instance, code block or net of this name in tb" in text


def test_a_restore_reads_the_users_of_a_symbol_again(agent):
    call = agent.call
    call("create_cell", cell="amp", views=["schematic"], reason="new")
    for i, (lab, t) in enumerate((("a", "ipin"), ("b", "ipin"), ("z", "opin"))):
        call("add_instance", cell="amp", symbol=f"devices/{t}.sym", x=200, y=40 * i, params={"lab": lab}, reason="port")
    call("create_cell", cell="amp", views=["symbol"], reason="symbol")
    call("create_cell", cell="tb", views=["schematic"], reason="tb")
    call("add_instance", cell="tb", symbol="amp.sym", x=0, y=0, params={"name": "x1"}, reason="dut")
    point = agent.cs.timeline("p", agent.ctx.changeset)["ops"][-1]["last"]
    port = next(k for k, v in call("get_drawing", cell="amp")["instances"].items() if v.get("port") == "b")
    call("delete_objects", cell="amp", objects=[{"kind": "instance", "id": port}], reason="no b")
    assert call("sync_symbol", cell="amp", reason="follow")["removed"] == ["b"]
    assert "b" not in call("get_netlist", cell="tb")["instances"]["x1"]["pins"]
    agent.cs.restore("p", agent.ctx.changeset, point, "user")              # amp.sym has b again; tb's drawing is as it was
    assert set(call("get_netlist", cell="tb")["instances"]["x1"]["pins"]) == {"a", "b", "z"}


def test_an_apply_reads_the_projects_users_of_a_symbol_again(proj):
    # a schematic of the project placing the symbol, made after the changeset was opened, is no part of it
    write_amp(proj, [("a", "ipin"), ("z", "opin")])
    symbolsync.from_schematic(proj.srv, "p", "l", "amp")
    cs = Changesets(proj.srv)
    meta = cs.create("p", "enable")
    work = proj.srv.project(meta["preview"]).library("l").path
    f = xschem.parse((work / "amp.sch").read_text())
    f.comps.append(XComp("devices/ipin.sym", 0, 200, 0, 0, {"name": "p9", "lab": "en"}))
    (work / "amp.sch").write_text(xschem.dump(f))
    assert symbolsync.from_schematic(proj.srv, meta["preview"], "l", "amp", actor="agent:t", reason="en")["added"] == ["en"]
    tb = proj.ed("tb")
    tb.create_file(xschem.dump(XFile(version="xschem version=3.4.5 file_version=1.2\n")))
    tb.add_instance("amp.sym", 0, 0, props={"name": "x1"})
    cs.apply("p", meta["id"], "user")
    got = netlist.load(netlist.path_of(tb.path))["instances"]["x1"]["pins"]
    assert set(got) == {"a", "z", "en"} and got["en"] not in ("0", got["a"], got["z"])
