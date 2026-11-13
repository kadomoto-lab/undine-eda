"""The netlist view (netlist.py): connectivity next to the drawing. Editors write both; consumers read the netlist."""
import re
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402

from undine.schematics import netlist, schematic, xschem  # noqa: E402
from undine import tools
from undine.server.api import Server, create_app  # noqa: E402
from undine.store.oplog import OpLog  # noqa: E402
from undine.schematics.schedit import SchEditor  # noqa: E402

V = "v {xschem version=3.4.5 file_version=1.2}\nG {}\nK {}\nV {}\nS {}\nE {}\n"
# a two-pin symbol of our own (pins P at the top, M at the bottom), so the tests need no PDK
R_SYM = ('v {xschem version=3.4.5 file_version=1.2}\nK {type=resistor\nformat="@name @pinlist @value"\ntemplate="name=R1 value=1k"}\n'
         'L 4 0 -30 0 30 {}\nB 5 -2.5 -32.5 2.5 -27.5 {name=P dir=inout}\nB 5 -2.5 27.5 2.5 32.5 {name=M dir=inout}\n')


def _devices():
    xl = tools.xschem_library()
    if xl is None:
        pytest.skip("Xschem's devices library is not installed")
    return [xl, xl / "devices"]


@pytest.fixture
def cell(tmp_path):
    lib = tmp_path / "l"
    lib.mkdir()
    (lib / "r.sym").write_text(R_SYM)
    # in -- R1 -- mid -- R2 -- (unlabeled) -- R3 -- out ; R1.M and R2.P are on one unlabeled wire
    (lib / "div.sch").write_text(V + "\n".join([
        "C {devices/ipin.sym} -100 -30 0 0 {name=p1 lab=in}",
        "C {devices/opin.sym} 300 -30 0 0 {name=p2 lab=out}",
        "C {r.sym} 0 0 0 0 {name=R1 value=1k}",
        "C {r.sym} 100 0 0 0 {name=R2 value=2k}",
        "C {r.sym} 200 0 0 0 {name=R3 value=3k}",
        "N -100 -30 0 -30 {lab=in}",
        "N 0 30 100 -30 {}",
        "N 100 30 200 -30 {}",
        "N 200 30 300 -30 {lab=out}",
    ]) + "\n")
    resolver = xschem.SymbolResolver([lib, *_devices()])
    ed = SchEditor(lib / "div.sch", resolver, OpLog(tmp_path / "history.jsonl"))
    return lib, resolver, ed


def test_a_file_is_read_into_every_key(tmp_path):
    """normalize is the one reading of a file (load, and the merge in vcs/yamlmerge.py)."""
    assert netlist.normalize({}) == netlist.empty()
    ports = [{"name": "A", "type": "ipin"}]
    assert netlist.normalize({"ports": ports, "code": None, "pending": []}) == {**netlist.empty(), "ports": ports}
    assert netlist.normalize({"pending": {"M3.G": 0}})["pending"] == {"M3.G": "0"}
    path = tmp_path / f"c{netlist.SUFFIX}"
    path.write_text("version: 1\nports: []\ninstances: {}\npending: []\n")
    assert netlist.load(path) == netlist.empty() and netlist.load(tmp_path / "none.netlist.yaml") is None


def test_the_drawing_is_read_into_a_deterministic_file(cell):
    lib, resolver, ed = cell
    m = netlist.sync(lib / "div.sch", resolver)
    assert m["ports"] == [{"name": "in", "type": "ipin"}, {"name": "out", "type": "opin"}]      # the schematic's order
    assert list(m["instances"]) == ["R1", "R2", "R3"]
    assert m["instances"]["R1"] == {"symbol": "r.sym", "props": {"value": "1k"}, "pins": {"P": "in", "M": "net1"}}
    assert m["instances"]["R2"]["pins"] == {"P": "net1", "M": "net2"}
    text = netlist.path_of(lib / "div.sch").read_text()
    assert netlist.dump(netlist.load(netlist.path_of(lib / "div.sch"))) == text                  # stable round trip
    # the netlisters read the netlist, and it is what the drawing says
    for mode in ("spice", "lvs"):
        assert schematic.spice(lib / "div.sch", resolver, mode=mode).text == \
            schematic.spice(lib / "div.sch", resolver, mode=mode, source="drawing").text


def test_moving_things_changes_the_drawing_not_the_netlist(cell):
    lib, resolver, ed = cell
    netlist.sync(lib / "div.sch", resolver)
    before = netlist.path_of(lib / "div.sch").read_text()
    sch_before = (lib / "div.sch").read_text()
    ed.move([{"k": "i", "id": "R3"}, {"k": "i", "id": "p2"}], 0, 200)          # wires follow; the circuit is the same
    assert (lib / "div.sch").read_text() != sch_before
    assert netlist.path_of(lib / "div.sch").read_text() == before


def test_unlabeled_nets_keep_their_names(cell):
    lib, resolver, ed = cell
    netlist.sync(lib / "div.sch", resolver)
    # redraw the first unlabeled net: delete its wire, draw it again (the drawing now numbers its nets differently)
    w = xschem.wire_id(0, 30, 100, -30)
    ed.delete([{"k": "w", "id": w}])
    m = netlist.load(netlist.path_of(lib / "div.sch"))
    assert m["instances"]["R3"]["pins"]["P"] == "net2"                          # the other net did not move to net1
    ed.add_wire(0, 30, 100, -30)
    m = netlist.load(netlist.path_of(lib / "div.sch"))
    assert m["instances"]["R1"]["pins"]["M"] == m["instances"]["R2"]["pins"]["P"]
    assert m["instances"]["R2"]["pins"]["M"] == "net2"


def test_pending_connections_until_drawn(cell, tmp_path):
    lib, resolver, ed = cell
    ed.add_instance("r.sym", None, None, props={"name": "R9", "value": "9k"}, placed="auto")
    nl = netlist.NetlistEditor(lib / "div.sch", resolver, ed.log)
    assert nl.connect("R9", "P", "in") == "pending"
    assert nl.connect("R9", "M", "out") == "pending"
    m = nl.model()
    assert m["pending"] == {"R9.M": "out", "R9.P": "in"}
    assert m["instances"]["R9"]["pins"] == {"P": "in", "M": "out"}
    spice = schematic.spice(lib / "div.sch", resolver).text
    assert "R9 in out 9k" in spice                                               # the netlisters use it
    assert "R9 in out" not in schematic.spice(lib / "div.sch", resolver, source="drawing").text   # not drawn
    links = netlist.pending_links(lib / "div.sch", resolver, m)
    assert {(l[0], l[1], l[2]) for l in links} == {("R9", "P", "in"), ("R9", "M", "out")}       # the ratsnest
    with pytest.raises(ValueError, match="drawn to"):
        nl.connect("R1", "P", "out")                                             # drawn: change the drawing
    # drawing it takes the place of the pending connection
    r9 = next(c for c in xschem.parse((lib / "div.sch").read_text()).comps if c.props.get("name") == "R9")
    ed.add_wire(r9.x, r9.y - 30, -100, -30)
    m = nl.model()
    assert "R9.P" not in m["pending"] and m["instances"]["R9"]["pins"]["P"] == "in" and m["pending"] == {"R9.M": "out"}
    # undo of a pending connection
    assert nl.undo() and "R9.M" not in nl.model()["pending"]
    assert nl.redo() and nl.model()["pending"] == {"R9.M": "out"}


def test_a_drawing_edited_outside_is_read_back(cell):
    lib, resolver, ed = cell
    netlist.sync(lib / "div.sch", resolver)
    text = (lib / "div.sch").read_text().replace("C {r.sym} 200 0 0 0 {name=R3 value=3k}", "C {r.sym} 200 0 0 0 {name=R3 value=4k}")
    (lib / "div.sch").write_text(text)                                           # as Xschem would
    assert netlist.current(lib / "div.sch", resolver)["instances"]["R3"]["props"]["value"] == "4k"
    assert "value: 4k" in netlist.path_of(lib / "div.sch").read_text()


def test_tools_place_in_free_space_and_tidy_moves_only_their_own(cell):
    lib, resolver, ed = cell
    boxes = ed._boxes(ed._load())
    a = ed.add_instance("r.sym", None, None, props={"name": "RA"}, placed="auto")
    b = ed.add_instance("r.sym", None, None, props={"name": "RB"}, placed="auto")
    f = ed._load()
    view = xschem.render_model(f, ed.path, resolver)
    placed = {i["id"]: i["bbox"] for i in view["insts"] if i["id"] in (a, b)}
    overlap = lambda p, q: not (p[2] <= q[0] or q[2] <= p[0] or p[3] <= q[1] or q[3] <= p[1])   # noqa: E731
    assert not any(overlap(placed[a], q) for q in boxes) and not overlap(placed[a], placed[b])
    assert all(c.props.get(netlist.PLACED) == "auto" for c in f.comps if c.props.get("name") in (a, b))
    assert netlist.PLACED not in netlist.load(netlist.path_of(ed.path))["instances"][a]["props"]   # drawing only
    # a person moves RA: it is theirs now; tidy only moves RB
    with ed.recording("move", {}, "user"):
        ed.move([{"k": "i", "id": a}], 0, 400)
    ra = next(c for c in ed._load().comps if c.props.get("name") == a)
    assert netlist.PLACED not in ra.props
    rb_before = next(c for c in ed._load().comps if c.props.get("name") == b)
    rb_before.x += 500
    with ed.recording("move", {}, "agent:t"):
        ed.move([{"k": "i", "id": b}], 500, 0)
    assert ed.tidy() == [b]
    after = {c.props.get("name"): (c.x, c.y) for c in ed._load().comps}
    assert after[a] == (ra.x, ra.y)


def test_a_netlist_without_a_drawing_is_drawn_once(cell):
    lib, resolver, ed = cell
    netlist.sync(lib / "div.sch", resolver)
    model = netlist.load(netlist.path_of(lib / "div.sch"))
    (lib / "copy.netlist.yaml").write_text(netlist.dump(model))
    ed2 = SchEditor(lib / "copy.sch", resolver, ed.log)
    view = ed2.view()
    assert (lib / "copy.sch").is_file()
    m = netlist.load(lib / "copy.netlist.yaml")
    assert m["instances"]["R2"]["pins"] == model["instances"]["R2"]["pins"]                   # same circuit
    assert set(m["pending"]) == {f"{i}.{p}" for i, r in model["instances"].items() for p in r["pins"]}   # nothing drawn
    assert len(view["pending"]) == len(m["pending"])
    assert all(c.props.get(netlist.PLACED) == "auto" for c in xschem.parse((lib / "copy.sch").read_text()).comps)
    assert schematic.spice(lib / "copy.sch", resolver).text.replace("copy", "div") == \
        schematic.spice(lib / "div.sch", resolver).text                         # the same circuit, under its own name
    assert not ed2.ensure_drawing()                                              # once


def test_drawing_is_written_in_a_canonical_order(cell):
    lib, resolver, ed = cell
    f = xschem.parse((lib / "div.sch").read_text())
    shuffled = xschem.parse((lib / "div.sch").read_text())
    shuffled.wires.reverse()
    assert xschem.dump(xschem.canonical(f)) == xschem.dump(xschem.canonical(shuffled))
    assert [c.props["name"] for c in xschem.canonical(f).comps] == ["p1", "p2", "R1", "R2", "R3"]   # pins keep order


def test_netlist_api_and_agent_tool(tmp_path):
    _devices()
    root = tmp_path / "projects"
    srv = Server(root, {}, [root])
    c = TestClient(create_app(srv, None))
    assert c.post("/api/projects", json={"name": "p"}).status_code == 200
    assert c.post("/api/p/p/libs", json={"name": "l"}).status_code == 200
    lib = root / "p/libs/l"
    (lib / "r.sym").write_text(R_SYM)
    assert c.post("/api/p/p/l/l/cells", json={"name": "top", "views": ["schematic"]}).status_code == 200
    assert (lib / "top.netlist.yaml").is_file()                                  # a new schematic has its netlist
    r = c.post("/api/p/p/l/l/c/top/netlist/edit", json={"op": "add_instance", "symbol": "r.sym", "props": {"name": "R1", "value": "5k"},
                                                        "pins": {"P": "a", "M": "b"}, "reason": "a tool adds a resistor"})
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["result"] == "R1" and v["netlist"]["instances"]["R1"]["pins"] == {"P": "a", "M": "b"}
    assert {p["pin"] for p in v["pending"]} == {"R1.P", "R1.M"}
    sch = c.get("/api/p/p/l/l/c/top/schematic").json()
    assert sch["pending"] == []                          # nothing else is on nets a / b: no line to draw yet
    assert "R1" in sch["placed_auto"]
    from undine.agents import agent_tools as T
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    out = T.call(ctx, "get_netlist", {"lib": "l", "cell": "top"})[0]
    assert '"R1"' in out and '"pending"' in out
    r = c.post("/api/p/p/l/l/c/top/netlist/edit", json={"op": "undo"})
    assert r.status_code == 200 and r.json()["pending"] == []


def test_every_pdk_schematic_netlists_the_same_from_its_netlist_view():
    """The netlisters moved from the drawing to the netlist view: same SPICE for every Xschem schematic of the PDK,
    and nothing written into the PDK."""
    from undine.pdks.pdk import default_pdk_roots, discover
    pdk = next((p for p in discover(default_pdk_roots()).values() if p.xschem_dir and p.family == "sky130"), None)
    if pdk is None:
        pytest.skip("no PDK with Xschem libraries installed")
    devices = _devices()
    files = sorted(Path(pdk.xschem_dir).rglob("*.sch"))
    refused = set()
    for f in files:
        res = xschem.SymbolResolver([f.parent, f.parent.parent, pdk.xschem_dir, *devices])
        for mode in ("spice", "lvs"):
            try:
                text = schematic.spice(f, res, mode=mode).text
            except ValueError as e:
                # a launcher page placing two cells of one name (sky130_tests/top: tb places mips_cpu/top) is refused
                # from either source alike
                assert "two cells named" in str(e), (f, mode, e)
                with pytest.raises(ValueError, match=re.escape(str(e))):
                    schematic.spice(f, res, mode=mode, source="drawing")
                refused.add((f.relative_to(pdk.xschem_dir).as_posix(), mode))
                continue
            assert text == schematic.spice(f, res, mode=mode, source="drawing").text, (f, mode)
    if (Path(pdk.xschem_dir) / "sky130_tests" / "top.sch").is_file():      # its subcell top is another library's
        assert {("sky130_tests/top.sch", "spice"), ("sky130_tests/top.sch", "lvs")} <= refused, refused
    assert not list(Path(pdk.xschem_dir).rglob("*" + netlist.SUFFIX))


def test_the_watch_reads_outside_edits_right_away_and_logs_them(tmp_path):
    _devices()
    root = tmp_path / "projects"
    srv = Server(root, {}, [root])
    c = TestClient(create_app(srv, None))
    assert c.post("/api/projects", json={"name": "p"}).status_code == 200
    assert c.post("/api/p/p/libs", json={"name": "l"}).status_code == 200
    lib = root / "p/libs/l"
    (lib / "r.sym").write_text(R_SYM)
    assert c.post("/api/p/p/l/l/cells", json={"name": "top", "views": ["schematic"]}).status_code == 200
    watch = netlist.Watcher(srv)
    watch.scan()                                                                  # starts from what is there
    assert watch.scan() == []                                                     # nothing changed: nothing read
    # Undine's own edit: synced by the editor, not imported again
    ed = srv.editor("p", "l", "top", "schematic")
    ed.add_instance("r.sym", 0, 0, props={"name": "R1", "value": "1k"})
    watch.scan()
    assert not [r for r in srv.oplog("p", "l").read(limit=100) if r["op"] == "import_drawing"]
    # an edit outside Undine (as Xschem would save it): in the netlist on the next scan, without anyone reading it
    sch = lib / "top.sch"
    sch.write_text(sch.read_text().replace("value=1k", "value=7k") + "C {r.sym} 200 0 0 0 {name=R2 value=2k}\n")
    assert watch.scan() == [str(sch)]
    m = netlist.load(netlist.path_of(sch))
    assert m["instances"]["R1"]["props"]["value"] == "7k" and "R2" in m["instances"]
    rec = [r for r in srv.oplog("p", "l").read(limit=100) if r["op"] == "import_drawing"][-1]
    assert rec["actor"] == "external" and rec["cell"] == "top" and rec["effects"] is None
    assert "added R2" in rec["reason"] and "R1 parameters" in rec["reason"]
    assert srv.netlist_editor("p", "l", "top").can_undo_redo() == {"undo": False, "redo": False}   # an import is not undone


def test_read_only_libraries_get_no_netlist_files(tmp_path):
    """A PDK (or a pinned dependency) is often writable on disk, e.g. installed in the home directory: opening, reading,
    netlisting or watching its schematics must still never write a netlist file there."""
    from undine.pdks.pdk import Pdk

    pdk_root = tmp_path / "pdk" / "fakeA"
    plib = pdk_root / "libs.tech" / "xschem" / "plib"
    plib.mkdir(parents=True)
    (plib / "r.sym").write_text(R_SYM)
    sub = plib / "sub.sch"
    sub.write_text(V + "C {r.sym} 0 0 0 0 {name=R1 value=1k}\nC {r.sym} 200 0 0 0 {name=R2 value=2k}\n")
    pdk = Pdk("fakeA", "sky130", "", pdk_root, xschem_dir=pdk_root / "libs.tech" / "xschem")
    root = tmp_path / "projects"
    srv = Server(root, {pdk.id: pdk}, [root])
    srv.store.create("p", pdk.id)
    c = TestClient(create_app(srv, None))
    lib = srv.project("p").library("plib")
    assert lib.readonly and lib.origin == "pdk"

    view = srv.editor("p", "plib", "sub", "schematic").view()                   # opening it in the editor
    assert {"R1", "R2"} <= {p[0] for p in view["pins"]}
    res = xschem.SymbolResolver(srv.project("p").symbol_paths())
    assert {d.name for d in netlist.connectivity(sub, res).devices} == {"R1", "R2"}   # a consumer, write left at its default
    assert set(netlist.current(sub, res)["instances"]) == {"R1", "R2"}          # write=True asked, not allowed
    assert set(netlist.sync(sub, res)["instances"]) == {"R1", "R2"}
    r = c.get("/api/p/p/l/plib/c/sub/netlist/model")                             # the netlist view over the API
    assert r.status_code == 200, r.text
    netlist.Watcher(srv).scan()
    sub.write_text(sub.read_text().replace("value=1k", "value=5k"))            # changed on disk: read again, still not written
    assert netlist.current(sub, res)["instances"]["R1"]["props"]["value"] == "5k"
    netlist.Watcher(srv).scan()
    with pytest.raises(PermissionError):                                        # the one sink refuses outright
        netlist._write(netlist.path_of(sub), {"instances": {}})
    assert not list(pdk_root.rglob("*" + netlist.SUFFIX))


def test_symbols_that_do_not_resolve_are_warned_of_in_the_netlist(tmp_path):
    """An inverter drawn with sky130 devices in a project without that PDK: the devices cannot be read into the
    netlist, and the reference netlist says so instead of coming back empty without a word."""
    _devices()
    root = tmp_path / "projects"
    srv = Server(root, {}, [root])
    c = TestClient(create_app(srv, None))
    assert c.post("/api/projects", json={"name": "p"}).status_code == 200
    assert c.post("/api/p/p/libs", json={"name": "l"}).status_code == 200
    sch = root / "p/libs/l/inv.sch"
    sch.write_text(V + "\n".join([
        "C {devices/ipin.sym} -100 0 0 0 {name=p1 lab=A}",
        "C {devices/opin.sym} 100 0 0 0 {name=p2 lab=Y}",
        "C {sky130_fd_pr/pfet_01v8.sym} 0 -60 0 0 {name=M1 L=0.15 W=2}",
        "C {sky130_fd_pr/nfet_01v8.sym} 0 60 0 0 {name=M2 L=0.15 W=1}",
    ]) + "\n")
    prj = srv.project("p")
    for mode in ("lvs", "sim"):
        for _ in range(2):                              # the second time the netlist is already read (and written)
            text, warnings = srv.reference_netlist(prj, "l", "inv", "inv", mode)
            assert "M1" not in text and "M2" not in text
            assert "inv: missing symbol sky130_fd_pr/pfet_01v8.sym (instance M1)" in warnings, warnings
            assert "inv: missing symbol sky130_fd_pr/nfet_01v8.sym (instance M2)" in warnings, warnings
            assert len(warnings) == len(set(warnings))
    res = xschem.SymbolResolver(prj.symbol_paths())
    assert netlist.current(sch, res, write=False)["instances"] == {}      # the netlist view itself is as before


# a two-pin subcircuit symbol: a at the left, y at the right
INV_SYM = ('v {xschem version=3.4.5 file_version=1.2}\nK {type=subcircuit\nformat="@name @pinlist @symname"\n'
           'template="name=x1"}\nB 5 -22.5 -2.5 -17.5 2.5 {name=a dir=in}\nB 5 17.5 -2.5 22.5 2.5 {name=y dir=out}\n')


def _inv_lib(d: Path, r: str, places: str = "r.sym", missing: bool = False) -> None:
    """A library `inv` of its own: one resistor of value `r` (or one instance of `places`) from a to y; `missing`: and
    an instance M9 of a symbol that resolves nowhere (a warning)."""
    d.mkdir(exist_ok=True)
    (d / "r.sym").write_text(R_SYM)
    (d / "inv.sym").write_text(INV_SYM)
    pins = {"r.sym": ((0, -30), (0, 30))}.get(places, ((-20, 0), (20, 0)))
    (d / "inv.sch").write_text(V + "\n".join([
        "C {devices/iopin.sym} -200 0 0 0 {name=p1 lab=a}", "C {devices/iopin.sym} -200 40 0 0 {name=p2 lab=y}",
        f"C {{{places}}} 0 0 0 0 {{name={'R1' if places == 'r.sym' else 'x1'} value={r}}}",
        f"C {{devices/lab_pin.sym}} {pins[0][0]} {pins[0][1]} 0 0 {{name=l1 lab=a}}",
        f"C {{devices/lab_pin.sym}} {pins[1][0]} {pins[1][1]} 0 0 {{name=l2 lab=y}}",
        *(["C {nolib/missing.sym} 100 100 0 0 {name=M9}"] if missing else [])]) + "\n")


def _places(d: Path, cell: str, syms: list[str], ports: bool = False) -> Path:
    """Schematic `cell` in `d` placing each symbol (an inv's pins) from in to o<k>."""
    lines = ["C {devices/iopin.sym} -200 0 0 0 {name=p1 lab=in}", "C {devices/iopin.sym} -200 40 0 0 {name=p2 lab=o0}"] \
        if ports else []
    for k, sym in enumerate(syms):
        x = 200 * k
        lines += [f"C {{{sym}}} {x} 0 0 0 {{name=x{k}}}", f"C {{devices/lab_pin.sym}} {x - 20} 0 0 0 {{name=la{k} lab=in}}",
                  f"C {{devices/lab_pin.sym}} {x + 20} 0 0 0 {{name=ly{k} lab=o{k}}}"]
    (d / f"{cell}.sch").write_text(V + "\n".join(lines) + "\n")
    (d / f"{cell}.sym").write_text(INV_SYM.replace("{name=a ", "{name=in ").replace("{name=y ", "{name=o0 "))
    return d / f"{cell}.sch"


def test_two_cells_of_one_name_with_other_content_are_refused_naming_both(tmp_path):
    """Subcircuits are named by cell name alone, so a project library's inv and a dependency's inv (or two
    dependencies') in one netlist would be one subcircuit, the first silently standing for the other: refused, at any
    depth and with every backend, naming both. Two of the same content are one subcircuit, written once."""
    paths = _devices()
    _inv_lib(tmp_path / "a", "1k")
    _inv_lib(tmp_path / "b", "2k")
    res = xschem.SymbolResolver([tmp_path, *paths])
    tb = _places(tmp_path / "a", "tb", ["a/inv.sym", "b/inv.sym"])
    both = r"two cells named inv are netlisted, a/inv and b/inv \(instance x1 in tb\), and they differ: a netlist has " \
           r"one subcircuit per name, so one would stand for the other. Rename one of them, or place the same cell"
    with pytest.raises(ValueError, match="tb: " + both):
        schematic.spice(tb, res, top_subckt=False)
    for mode in ("lvs", "netgen"):
        with pytest.raises(ValueError, match=both):
            schematic.spice(tb, res, mode=mode)
    with pytest.raises(ValueError, match=both):                 # before Xschem runs (or is looked for)
        schematic.xschem_netlist(tb, [tmp_path, *paths], {})
    try:
        import vlsirtools  # noqa: F401
    except ImportError:
        pass
    else:
        with pytest.raises(ValueError, match=both):
            schematic.vlsirtools_netlist(tb, res)
    # at depth 2: b's buf places b's inv, the testbench a's inv and b's buf
    _places(tmp_path / "b", "buf", ["b/inv.sym"], ports=True)
    with pytest.raises(ValueError, match=r"a/inv and b/inv \(instance x0 in buf\)"):
        schematic.spice(_places(tmp_path / "a", "tb2", ["a/inv.sym", "b/buf.sym"]), res, top_subckt=False)
    # inside: a cell inv that wraps another library's inv
    _inv_lib(tmp_path / "c", "", places="b/inv.sym")
    with pytest.raises(ValueError, match=r"c/inv and b/inv \(instance x1 in inv\)"):
        schematic.spice(_places(tmp_path / "a", "tb3", ["c/inv.sym"]), res, top_subckt=False)
    # the top cell itself: a/top places b/top, another cell of its name
    (tmp_path / "b" / "top.sch").write_text((tmp_path / "b" / "inv.sch").read_text())
    (tmp_path / "b" / "top.sym").write_text(INV_SYM)
    with pytest.raises(ValueError, match=r"top: two cells named top are netlisted, a/top and b/top "
                                         r"\(instance x0 in top\), and they differ"):
        schematic.spice(_places(tmp_path / "a", "top", ["b/top.sym"]), res, top_subckt=False)
    # the same content in both libraries: one subcircuit, written once, with nothing said twice (each inv warns of
    # its missing symbol M9, the second's netlist compared and dropped)
    _inv_lib(tmp_path / "a", "1k", missing=True)
    _inv_lib(tmp_path / "b", "1k", missing=True)
    r = schematic.spice(tb, res, top_subckt=False)
    assert r.text.count(".subckt inv a y\n") == 1 and "\nR1 a y 1k\n" in r.text and r.subckts == ["inv"]
    assert "\nx0 in o0 inv\n" in r.text and "\nx1 in o1 inv\n" in r.text and len(r.warnings) == len(set(r.warnings))
    assert r.warnings.count("inv: missing symbol nolib/missing.sym (instance M9)") == 1, r.warnings
    r = schematic.spice(_places(tmp_path / "a", "tb2", ["a/inv.sym", "b/buf.sym", "b/inv.sym"]), res, top_subckt=False)
    assert r.text.count(".subckt inv a y\n") == 1 and r.subckts == ["inv", "buf"]


def test_the_xschem_backend_refuses_only_what_xschem_itself_would_netlist_as_one_cell(tmp_path):
    """Undine looks for a bare `inv.sym` in the placing schematic's directory first, Xschem on its library path first:
    with libraries a and b both on the path, a/tb and b/buf each placing `inv.sym` are two cells for Undine (refused)
    and one for Xschem (a/inv, first hit), so the Xschem backend netlists it."""
    paths = _devices()
    a, b = tmp_path / "a", tmp_path / "b"
    _inv_lib(a, "1k")
    _inv_lib(b, "2k")
    _places(b, "buf", ["inv.sym"], ports=True)
    tb = _places(a, "tb", ["inv.sym", "buf.sym"])
    with pytest.raises(ValueError, match=r"a/inv and b/inv \(instance x0 in buf\)"):
        schematic.spice(tb, xschem.SymbolResolver([a, b, *paths]), top_subckt=False)
    order = schematic.XschemOrder([a, b, *paths])
    assert order.find("inv.sym", b) == a / "inv.sym" and order.find("./inv.sym", b) == b / "inv.sym"
    r = schematic.spice(tb, order, top_subckt=False)
    assert r.text.count(".subckt inv ") == 1 and "R1 a y 1k" in r.text and "2k" not in r.text
    if tools.find("xschem") is None:
        with pytest.raises(RuntimeError, match="xschem not found"):     # past the check, to the missing executable
            schematic.xschem_netlist(tb, [a, b, *paths], {})
        return
    text = schematic.xschem_netlist(tb, [a, b, *paths], {})
    assert text.count(".subckt inv ") == 1 and "R1 a y 1k" in text and "2k" not in text, text     # a/inv, as above
