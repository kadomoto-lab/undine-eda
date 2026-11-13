"""DRC / LVS / ERC with the real tools on sky130A cells built from its PCells: KLayout's LVS compares a MIM capacitor by
its area and perimeter (the schematic's call becomes the deck's element), run_checks runs Magic's drc(full) next to the
KLayout deck and an ERC of body taps butted against another net's source / drain, each row with its job, when it
finished and whether it was reused, get_drc_report says where the violations are (inside a placed sub-cell too) and
render_layout draws them; Magic's DRC and the ERC on GF180MCU and IHP. Skipped without KLayout, Magic, the PDK or its
PCells (as test_parasitics.py does)."""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import klayout.db as kdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine import tools  # noqa: E402
from undine.agents import agent_tools as T  # noqa: E402
from undine.checks import verify  # noqa: E402
from undine.pdks import pcell  # noqa: E402
from undine.pdks.pdk import default_pdk_roots, discover  # noqa: E402
from undine.schematics import xschem  # noqa: E402
from undine.schematics.xschem import XComp, XFile  # noqa: E402
from undine.server.api import Server  # noqa: E402

PDKS = discover(default_pdk_roots())


def _need():
    pdk = next((p for p in PDKS.values() if p.name == "sky130A" and pcell.available(p)), None)
    if pdk is None or not verify.decks(pdk)["magic"] or not verify.decks(pdk)["lvs"] or not tools.find("klayout"):
        pytest.skip("sky130A with its PCells, KLayout or Magic is not installed")
    if tools.xschem_library() is None:
        pytest.skip("Xschem's devices library is not installed")
    return pdk


def _pcell(pdk, ly: kdb.Layout, kind: str, params: dict) -> kdb.Cell:
    path, name, _ = pcell.generate(pdk, kind, params)
    src = kdb.Layout()
    src.read(str(path))
    c = ly.cell(name) or ly.create_cell(name)
    if c.is_empty():
        c.copy_tree(src.cell(name))
    return c


def _wait(job, limit: float = 300):
    t0 = time.time()
    while job.status()["state"] in ("queued", "running"):
        assert time.time() - t0 < limit, job.log(20)
        time.sleep(0.3)
    return job.status()


def _server(pdk, files: dict[str, XFile]):
    root = Path(tempfile.mkdtemp())
    srv = Server(root / "projects", PDKS, [root])
    lib = srv.store.create("p", pdk.id).create_library("l")
    for name, f in files.items():
        (lib.path / name).write_text(xschem.dump(f))
    return root, srv


def C(sym, x, y, **p):
    return XComp(sym, x, y, 0, 0, p)


def _call(ctx, tool: str, args: dict):
    res, err = T.call(ctx, tool, args)
    assert not err, res
    return json.loads(res) if res.startswith("{") else res


# ------------------------------------------------------------------ MIM capacitors in LVS


def _mims(pdk, w: float, l: float) -> kdb.Layout:
    """Two MIM units of W x L um (the PCell: capm, the met3 bottom plate, met4 straps), stacked 6 um apart, the top
    plates joined in met4 (label c0) and the bottom plates in met3 (label c1)."""
    ly = kdb.Layout()
    ly.dbu = 0.001
    unit = _pcell(pdk, ly, "mim_cap", {"type": "sky130_fd_pr__model__cap_mim", "w": w, "l": l})
    top = ly.create_cell("mim2")
    for y in (0, 6):
        top.insert(kdb.DCellInstArray(unit.cell_index(), kdb.DTrans(kdb.DVector(0, y))))
    top.shapes(ly.layer(70, 20)).insert(kdb.DBox(-0.2, 3.0, 0.3, 6.0))             # the bottom plates
    top.shapes(ly.layer(71, 20)).insert(kdb.DBox(0.3, 2.5, 1.0, 6.5))              # the top plates' straps
    top.shapes(ly.layer(71, 5)).insert(kdb.DText("c0", 0.6, 4.5))
    top.shapes(ly.layer(70, 5)).insert(kdb.DText("c1", 0.0, 4.5))
    return ly


def test_lvs_compares_a_sky130_mim_by_area_and_perimeter():
    """The symbol writes a call (XC1 c0 c1 sky130_fd_pr__cap_mim_m3_1 W=2 L=3 m=2) that the deck's reader leaves a
    call, while the deck extracts the plates as a capacitor compared by A and P: the reference has it as that
    capacitor, per unit, with the multiplier. Two 2 x 3 um units match; half the area, another perimeter (the same
    area) and other nets do not."""
    pdk = _need()
    sch = XFile(version="xschem version=3.4.5 file_version=1.2\n")
    sch.comps = [C("sky130_fd_pr/cap_mim_m3_1.sym", 0, 0, name="C1", model="cap_mim_m3_1", W="2", L="3", MF="2",
                   spiceprefix="X"),
                 C("devices/iopin.sym", 0, -30, name="p1", lab="c0"), C("devices/iopin.sym", 0, 30, name="p2", lab="c1")]
    root, srv = _server(pdk, {"mim2.sch": sch})
    ly = _mims(pdk, 2.0, 3.0)
    ly.write(str(root / "mim2.gds"))
    srv.db("p", "l").import_oas(root / "mim2.gds")
    st = _wait(srv.prepare_lvs("p", "l", "mim2").launch())
    ref = (srv.jobs.get(srv.project("p").path, st["id"]).path / "schematic.spice").read_text()
    assert "CC1 c0 c1 sky130_fd_pr__model__cap_mim A=6 P=10 m=2" in ref, ref
    assert st["summary"]["match"] is True, st
    for wrong in ("XC1 c0 c1 sky130_fd_pr__cap_mim_m3_1 W=2 L=3 m=1",       # half the area
                  "XC1 c0 c1 sky130_fd_pr__cap_mim_m3_1 W=2.4 L=2.5 m=2",   # A 6, P 9.8
                  "XC1 c0 vss sky130_fd_pr__cap_mim_m3_1 W=2 L=3 m=2"):     # other nets
        text = pdk.profile.lvs_netlist(f".subckt mim2 c0 c1 vss\n{wrong}\n.ends\n")
        job = verify.start_lvs(srv.jobs, srv.project("p").path, pdk, srv.db("p", "l").layout, "mim2", "l", text)
        assert _wait(job)["summary"]["match"] is False, wrong
    with pytest.raises(ValueError, match="XC1: LVS compares it as sky130_fd_pr__model__cap_mim"):
        pdk.profile.lvs_netlist("XC1 c0 c1 sky130_fd_pr__cap_mim_m3_1 W=wc L=3 m=2")
    shutil.rmtree(root)


def test_a_port_the_schematic_does_not_use_inside_matches_by_its_label():
    """sar_cdac's vdd: an interface port connected to nothing inside the cell. The deck's simplification purges the
    labelled floating metal, so the layout has no VDD pin and KLayout's pin pair has no layout side; the label in the
    top cell satisfies it (unused_pins), and without the label it is still an error, saying what is missing. A used
    pin missing from the layout is the error it was."""
    pdk = _need()
    root, srv = _server(pdk, {})
    ly = _mims(pdk, 2.0, 3.0)
    top = ly.cell("mim2")
    top.shapes(ly.layer(68, 20)).insert(kdb.DBox(3.0, 0.0, 3.5, 0.5))              # a met1 stub for the port's label
    top.shapes(ly.layer(68, 5)).insert(kdb.DText("vdd", 3.25, 0.25))
    ly.write(str(root / "mim2.gds"))
    srv.db("p", "l").import_oas(root / "mim2.gds")
    db = srv.db("p", "l")

    def lvs(text: str) -> dict:
        job = verify.start_lvs(srv.jobs, srv.project("p").path, pdk, db.layout, "mim2", "l", pdk.profile.lvs_netlist(text))
        return _wait(job)["summary"]
    ref = ".subckt mim2 c0 c1 vdd\nXC1 c0 c1 sky130_fd_pr__cap_mim_m3_1 W=2 L=3 m=2\n.ends\n"
    s = lvs(ref)
    assert s["match"] is True and "pin_errors" not in s, s
    assert [p["pin"] for p in s["unused_pins"]] == ["VDD"], s

    def texts(old: str, *new: tuple) -> None:
        """Take the cell's `old` texts away and put `new` ones ((layer, datatype), text, x, y)."""
        with db.lock:
            c = db.layout.cell("mim2")
            for li in db.layout.layer_indexes():
                for sh in [x for x in c.shapes(li).each() if x.is_text() and x.text_string == old]:
                    c.shapes(li).erase(sh)
            for ld, t, x, y in new:
                c.shapes(db.layout.layer(*ld)).insert(kdb.DText(t, x, y))
    # the label on live metal: the bottom plates (an internal net of the schematic) labelled vdd, not c1. The
    # exemption would pass a VDD port that is the capacitor's plate; it is an error naming the net
    texts("c1", ((70, 5), "vdd", 0.0, 4.5))
    s = lvs(".subckt mim2 c0 vdd\nXC1 c0 c1 sky130_fd_pr__cap_mim_m3_1 W=2 L=3 m=2\n.ends\n")
    assert s["match"] is False and "unused_pins" not in s, s
    assert any(e.startswith("pin VDD: connected to nothing in the schematic but its label is on the layout net")
               for e in s["pin_errors"]), s
    # ... and a vdd label on the c1 port's net as well as c1's
    texts("vdd", ((70, 5), "c1", 0.0, 4.5), ((70, 5), "vdd", 0.0, 4.0), ((68, 5), "vdd", 3.25, 0.25))
    s = lvs(ref)
    assert s["match"] is False and "unused_pins" not in s, s
    assert any(e.startswith("pin VDD: connected to nothing in the schematic but its label is on the layout net")
               for e in s["pin_errors"]), s
    # a vdd text on a layer that names no net (235/4), or on met1.label off any met1, is no label of the port
    for ld, x in (((235, 4), 3.25), ((68, 5), 8.0)):
        texts("vdd", (ld, "vdd", x, 0.25))
        s = lvs(ref)
        assert s["match"] is False and "unused_pins" not in s, (ld, s)
        assert s["pin_errors"] == ["pin VDD: connected to nothing inside the schematic and no VDD label in the "
                                   "layout's top cell: label the port where it leaves the cell"], (ld, s)
    texts("vdd", ((68, 5), "vdd", 3.25, 0.25))
    assert lvs(ref)["match"] is True
    # without the label: the port is not on the layout at all
    texts("vdd")
    s = lvs(ref)
    assert s["match"] is False and "unused_pins" not in s, s
    assert s["pin_errors"] == ["pin VDD: connected to nothing inside the schematic and no VDD label in the layout's "
                               "top cell: label the port where it leaves the cell"], s
    # a used pin the layout lacks (c1 also on a second capacitor to a net vdd that is not drawn): an error as before
    s = lvs(".subckt mim2 c0 c1 vdd\nXC1 c0 c1 sky130_fd_pr__cap_mim_m3_1 W=2 L=3 m=2\n"
            "XC2 vdd c1 sky130_fd_pr__cap_mim_m3_1 W=2 L=3 m=1\n.ends\n")
    assert s["match"] is False, s
    shutil.rmtree(root)


# ------------------------------------------------------------------ run_checks: Magic DRC, ERC, jobs


def _switch(pdk, ly: kdb.Layout, name: str, butted: str) -> None:
    """An nfet PCell (W 1 / L 0.15, its tap butted against the right source / drain) as cell `name`, its tap tied to
    VSS in li1; the `butted` net (out or VSS) on the right source / drain, the other on the left."""
    fet = _pcell(pdk, ly, "nfet", {"type": "sky130_fd_pr__nfet_01v8", "w": 1.0, "l": 0.15, "nf": 1, "bulk": "bulk tie"})
    top = ly.create_cell(name)
    top.insert(kdb.DCellInstArray(fet.cell_index(), kdb.DTrans()))
    li, txt = ly.layer(67, 20), ly.layer(68, 5)
    if butted == "VSS":
        top.shapes(li).insert(kdb.DBox(0.6, 0.155, 0.95, 0.845))                  # right source / drain to the tap
    else:                                                                          # left one to the tap, below the gate
        for box in ((0.065, -0.45, 0.235, 0.2), (0.89, -0.45, 1.06, 0.2), (0.065, -0.45, 1.06, -0.25)):
            top.shapes(li).insert(kdb.DBox(*box))
    other = "out" if butted == "VSS" else "VSS"
    for t, x, y in ((butted, 0.6, 0.5), (other, 0.15, 0.5), ("g", 0.375, 1.36)):
        top.shapes(txt).insert(kdb.DText(t, x, y))


def _switch_schematic() -> XFile:
    sch = XFile(version="xschem version=3.4.5 file_version=1.2\n")
    sch.comps = [C("sky130_fd_pr/nfet_01v8.sym", 0, 0, name="M1", model="nfet_01v8", L="0.15", W="1", nf="1", mult="1",
                   spiceprefix="X"),
                 C("devices/iopin.sym", 20, -30, name="p1", lab="out"), C("devices/iopin.sym", -20, 0, name="p2", lab="g"),
                 C("devices/iopin.sym", 20, 30, name="p3", lab="VSS"), C("devices/lab_pin.sym", 20, 0, name="l1", lab="VSS")]
    return sch


def test_run_checks_runs_magic_drc_and_the_erc_and_names_their_jobs(monkeypatch):
    """Three new cells: sw (an nfet whose output is on the source / drain butted against the VSS tap), sw_ok (VSS on
    it) and hv (a 5 V nfet PCell, whose 0.45 um butted tap is clean in the KLayout deck and Magic's diff/tap.16).
    run_checks gives LVS, ERC and both DRCs with their jobs; again it reuses them, with fresh it runs them anew; without
    Magic its rows fail and say so."""
    pdk = _need()
    root, srv = _server(pdk, {"sw.sch": _switch_schematic(), "sw_ok.sch": _switch_schematic()})
    ly = kdb.Layout()
    ly.dbu = 0.001
    _switch(pdk, ly, "sw", "out")
    _switch(pdk, ly, "sw_ok", "VSS")
    hv = ly.create_cell("hv")
    hv.insert(kdb.DCellInstArray(_pcell(pdk, ly, "nfet", {"type": "sky130_fd_pr__nfet_g5v0d10v5", "w": 1.0, "l": 0.5,
                                                           "nf": 1, "bulk": "bulk tie"}).cell_index(), kdb.DTrans()))
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    _call(ctx, "open_changeset", {"title": "checks", "reason": "verify"})
    for cell in ("sw", "sw_ok", "hv"):                         # an import is recorded as its file's top cell
        opt = kdb.SaveLayoutOptions()
        opt.select_cell(ly.cell(cell).cell_index())
        ly.write(str(root / f"{cell}.gds"), opt)
        _call(ctx, "import_layout", {"lib": "l", "path": str(root / f"{cell}.gds"), "reason": "a cell to check"})
    rows = {(r["cell"], r["kind"], r.get("checker")): r for r in _call(ctx, "run_checks", {})["checks"]}
    assert set(rows) == {(c, k, ch) for c in ("sw", "sw_ok") for k, ch in (("lvs", "klayout"), ("erc", "klayout"),
                                                                          ("drc", "klayout"), ("drc", "magic"))} | \
        {("hv", "drc", "klayout"), ("hv", "drc", "magic")}
    assert all(r["state"] == "done" and r["job"] and r["finished"] and r["reused"] is False for r in rows.values()), rows
    assert rows["sw", "lvs", "klayout"]["ok"] and rows["sw_ok", "lvs", "klayout"]["ok"]
    assert rows["sw", "erc", "klayout"]["job"] == rows["sw", "lvs", "klayout"]["job"]       # from that LVS run
    erc = rows["sw", "erc", "klayout"]
    erc_status = srv.jobs.get(srv.project(ctx.work).path, erc["job"]).status()["erc"]
    assert erc["ok"] is False and len(erc["findings"]) == 1 and "abut a source / drain of another net" in erc["detail"]
    f = erc["findings"][0]
    assert (f["instance"], f["net"], f["tap_net"], f["edge_um"]) == ("M1", "out", "VSS", 1.0), f
    assert rows["sw_ok", "erc", "klayout"]["ok"] is True
    assert rows["hv", "drc", "klayout"]["ok"] is True                                   # the deck has no such rule
    assert rows["hv", "drc", "magic"]["ok"] is False and rows["hv", "drc", "magic"]["detail"] == "1 violations"
    # the DRC report: where, per checker and rule
    rep = _call(ctx, "get_drc_report", {"lib": "l", "cell": "hv"})
    by = {r["checker"]: r for r in rep["runs"]}
    assert by["klayout"]["total"] == 0 and by["klayout"]["rules"] == [] and by["klayout"]["outdated"] is False
    tap = by["magic"]["rules"]
    assert [(r["rule"], r["count"]) for r in tap] == [("diff/tap.16", 1)] and "MV" in tap[0]["description"]
    x1, y1, x2, y2 = tap[0]["markers"][0]["box"]          # beside the 0.45 um tap: where 0.7 um would reach
    tap_box = kdb.Region(ly.cell("hv").begin_shapes_rec(ly.layer(65, 44))).bbox().to_dtype(ly.dbu)
    assert x2 > x1 and y2 > y1 and tap_box.touches(kdb.DBox(x1, y1, x2, y2)), (tap_box, x1, y1, x2, y2)
    only = _call(ctx, "get_drc_report", {"lib": "l", "cell": "hv", "checker": "magic", "rule": "nosuch.1"})
    assert [r["checker"] for r in only["runs"]] == ["magic"] and only["not_found"] == ["nosuch.1"]
    pic = _call(ctx, "render_layout", {"lib": "l", "cell": "hv", "markers": True})
    assert pic["image_attached"] and pic["markers"]["diff/tap.16 (magic)"]["count"] == 1 and ctx.images
    none = _call(ctx, "render_layout", {"lib": "l", "cell": "hv", "markers": ["m1.1"]})   # the picture, and why no marker
    assert none["image_attached"] and none["markers"] == {} and "the latest runs found none" in none["markers_note"]
    checks = _call(ctx, "get_checks", {"lib": "l", "cell": "sw"})
    assert "DRC (Magic drc(full))" in checks and "ERC (from that LVS run): 1 warning" in checks and "M1" in checks
    # an ERC that could not be made says so, with none of its LVS run's summary (the review would read "match")
    lvs = srv.jobs.get(srv.project(ctx.work).path, erc["job"])
    lvs.set(erc={"error": "OSError: simulated"})
    row = next(r for r in ctx.cs.verify_status(ctx.project, ctx.cs._load(ctx.project, ctx.changeset))
               if (r["cell"], r["kind"], r["side"]) == ("sw", "erc", erc["side"]))
    assert row["summary"] == {} and row["ok"] is None and row["detail"] == "ERC failed: OSError: simulated", row
    lvs.set(erc=erc_status)
    # again: the same jobs, reused; fresh: new ones
    again = {(r["cell"], r["kind"], r.get("checker")): r for r in _call(ctx, "run_checks", {})["checks"]}
    assert all(again[k]["job"] == rows[k]["job"] and again[k]["reused"] is True for k in rows)
    fresh = {(r["cell"], r["kind"], r.get("checker")): r for r in _call(ctx, "run_checks", {"fresh": True})["checks"]}
    assert all(fresh[k]["job"] != rows[k]["job"] and fresh[k]["reused"] is False for k in rows)
    # without Magic, its DRC rows say why; the others still run
    find = tools.find
    monkeypatch.setattr(tools, "find", lambda name: None if name == "magic" else find(name))
    gone = {(r["cell"], r["kind"], r.get("checker")): r for r in _call(ctx, "run_checks", {"fresh": True})["checks"]}
    assert gone["hv", "drc", "magic"]["state"] == "failed" and "magic not found" in gone["hv", "drc", "magic"]["error"]
    assert gone["hv", "drc", "klayout"]["state"] == "done"
    shutil.rmtree(root)


# ------------------------------------------------------------------ where the markers are


def test_drc_markers_are_where_the_violation_is_placed():
    """A sub-cell with a met1 wire 0.1 um wide (m1.1 / met1.1) placed twice (at 10, 10 and turned by 90 degrees at
    20, 5) and one such wire in the top cell. KLayout's deck in deep mode reports the sub-cell's once, in its own coordinates
    with its placement as a reference: get_drc_report and the Verify panel's markers give it where it is placed, not
    at the origin. Magic finds the top cell's own wire too, at the cell's edge (its error area is outside the cell)."""
    pdk = _need()
    root, srv = _server(pdk, {})
    ly = kdb.Layout()
    ly.dbu = 0.001
    met1 = ly.layer(68, 20)
    sub = ly.create_cell("sub")
    sub.shapes(met1).insert(kdb.DBox(0, 0, 0.1, 2))
    top = ly.create_cell("t")
    top.insert(kdb.DCellInstArray(sub.cell_index(), kdb.DTrans(kdb.DVector(10, 10))))
    top.insert(kdb.DCellInstArray(sub.cell_index(), kdb.DTrans(kdb.DTrans.R90, kdb.DVector(20, 5))))
    top.shapes(met1).insert(kdb.DBox(30, 30, 30.1, 32))
    opt = kdb.SaveLayoutOptions()
    opt.select_cell(top.cell_index())
    ly.write(str(root / "t.gds"), opt)
    srv.db("p", "l").import_oas(root / "t.gds")
    st = _wait(srv.prepare_drc("p", "l", "t").launch())
    assert st["state"] == "done", st
    placed = {(10.0, 10.0, 10.1, 12.0), (18.0, 5.0, 20.0, 5.1)}               # the sub-cell's wire, per placement
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    rep = _call(ctx, "get_drc_report", {"lib": "l", "cell": "t", "checker": "klayout", "rule": "m1.1"})
    own, boxes = (30.0, 30.0, 30.1, 32.0), set()
    for m in rep["runs"][0]["rules"][0]["markers"]:
        xs, ys = [v for e in m["edges"] for v in e[0::2]], [v for e in m["edges"] for v in e[1::2]]
        boxes.add((min(xs), min(ys), max(xs), max(ys)))
    assert own in boxes and boxes - {own} and boxes <= placed | {own}, boxes
    items = verify.drc_items(srv.jobs.get(srv.project("p").path, st["id"]).path / "result.lyrdb", None)["items"]
    assert {tuple(round(v, 4) for v in it["bbox"]) for it in items if it["cat"] == "m1.1"} == boxes
    magic = _wait(srv.prepare_magic_drc("p", "l", "t").launch())
    rep = _call(ctx, "get_drc_report", {"lib": "l", "cell": "t", "checker": "magic"})
    got = {tuple(m["box"]) for m in rep["runs"][0]["rules"][0]["markers"]}
    assert magic["summary"]["total"] == 3 and got == {(30.1, 30.0, 30.14, 32.0), (10.1, 10.0, 10.14, 12.0),
                                                      (18.0, 5.1, 20.0, 5.14)}, got
    shutil.rmtree(root)


# ------------------------------------------------------------------ GF180MCU and IHP


def _other(name: str):
    pdk = next((p for p in PDKS.values() if p.name == name), None)
    if pdk is None or not tools.find("magic") or not verify.decks(pdk)["magic"]:
        pytest.skip(f"{name} or Magic is not installed")
    return pdk


@pytest.mark.parametrize("name, metal1, rule, inv", [
    ("gf180mcuD", (34, 0), "M1.1", "gf180mcu_fd_sc_mcu7t5v0__inv_1"),
    ("ihp-sg13g2", (8, 0), "M1.a", "sg13g2_inv_1"),
    ("ihp-sg13cmos5l", (8, 0), "M1.a", None)])                    # no standard cells in that PDK
def test_magic_drc_of_gf180_and_ihp(name, metal1, rule, inv):
    """Magic's drc(full) runs where the profile has a Magic tech (magic_drc): metal1 0.1 um wide in a sub-cell placed at
    10, 10 and in the top cell is found twice, each where it is, beside the PDK's inverter, which is clean."""
    pdk = _other(name)
    assert pdk.profile.magic_drc
    root, srv = _server(pdk, {})
    ly = kdb.Layout()
    ic = None
    if inv:
        _, gds = pdk.profile.stdcell(inv)
        src = kdb.Layout()
        src.read(str(pdk.root / gds))
        ly.dbu = src.dbu
        ic = ly.create_cell(inv)
        ic.copy_tree(src.cell(inv))
    sub = ly.create_cell("sub")
    sub.shapes(ly.layer(*metal1)).insert(kdb.DBox(0, 0, 0.1, 2))
    for name_, wires in (("t", True), ("clean", False)) if ic else (("t", True),):
        top = ly.create_cell(name_)
        if ic:
            top.insert(kdb.DCellInstArray(ic.cell_index(), kdb.DTrans(kdb.DVector(40 if wires else 0, 0))))
        if wires:
            top.insert(kdb.DCellInstArray(sub.cell_index(), kdb.DTrans(kdb.DVector(10, 10))))
            top.shapes(ly.layer(*metal1)).insert(kdb.DBox(30, 30, 30.1, 32))
        opt = kdb.SaveLayoutOptions()
        opt.select_cell(top.cell_index())
        ly.write(str(root / f"{name_}.gds"), opt)
        srv.db("p", "l").import_oas(root / f"{name_}.gds")
    if ic:
        assert _wait(srv.prepare_magic_drc("p", "l", "clean").launch())["summary"]["total"] == 0
    st = _wait(srv.prepare_magic_drc("p", "l", "t").launch())
    assert st["summary"]["total"] == 2 and [c["name"] for c in st["summary"]["categories"]] == [rule], st
    rep = _call(T.ToolCtx(srv, "p", actor="agent:t"), "get_drc_report", {"lib": "l", "cell": "t", "checker": "magic"})
    boxes = sorted(tuple(m["box"][:2]) + (m["box"][3],) for m in rep["runs"][0]["rules"][0]["markers"])
    assert boxes == [(10.1, 10.0, 12.0), (30.1, 30.0, 32.0)], rep                 # from the wire's edge to the minimum
    shutil.rmtree(root)


def test_gf180_erc_finds_a_tap_butted_against_another_nets_drain():
    """GF180MCU draws no tap layer: its taps are COMP of the well's own implant (erc_tap). The nfet PCell's bulk tie is
    butted against its drain; with the drain on out and the tap on VSS, LVS matches (its deck does not join a silicided
    junction, a short in the silicon), and the LVS run's ERC finds the drain against the VSS tap; with the drain
    strapped to the tap, nothing."""
    pdk = _other("gf180mcuD")
    if not pcell.available(pdk) or not verify.decks(pdk)["lvs"] or not tools.find("klayout"):
        pytest.skip("gf180mcuD's PCells or its LVS deck is not installed")
    root, srv = _server(pdk, {})
    path, name, _ = pcell.generate(pdk, "nfet", {"w_gate": 1.0, "l_gate": 0.28, "bulk": "Bulk Tie", "nf": 1})
    src = kdb.Layout()
    src.read(str(path))
    ly = kdb.Layout()
    ly.dbu = src.dbu
    dev = ly.create_cell(name)
    dev.copy_tree(src.cell(name))
    tap = kdb.Region(dev.begin_shapes_rec(ly.layer(31, 0))) & kdb.Region(dev.begin_shapes_rec(ly.layer(22, 0)))
    m1 = sorted(kdb.Region(dev.begin_shapes_rec(ly.layer(34, 0))).merged().each(), key=lambda p: p.bbox().left)
    s, d, t = [p.bbox().center().to_dtype(ly.dbu) for p in m1 if p.bbox().center().y < tap.bbox().top][:3]
    g = next(p for p in m1 if p.bbox().center().y >= tap.bbox().top).bbox().center().to_dtype(ly.dbu)
    found = {}
    for cell, drain in (("sw", "out"), ("strapped", "VSS")):
        top = ly.create_cell(cell)
        top.insert(kdb.DCellInstArray(dev.cell_index(), kdb.DTrans()))
        labels = [("in", s), ("VSS", t), ("g", g)] + ([(drain, d)] if drain != "VSS" else [])
        if drain == "VSS":
            top.shapes(ly.layer(34, 0)).insert(kdb.DBox(d.x, d.y - 0.1, t.x, t.y + 0.1))
        for text, at in labels:
            top.shapes(ly.layer(34, 10)).insert(kdb.DText(text, at.x, at.y))
        opt = kdb.SaveLayoutOptions()
        opt.select_cell(top.cell_index())
        ly.write(str(root / f"{cell}.gds"), opt)
        srv.db("p", "l").import_oas(root / f"{cell}.gds")
        ports = " ".join(dict.fromkeys(["in", drain, "g", "VSS"]))
        (root / f"{cell}.spice").write_text(f".SUBCKT {cell} {ports}\nM1 {drain} g in VSS nfet_03v3 W=1e-06 L=2.8e-07\n.ENDS\n")
        st = _wait(srv.start_lvs("p", "l", cell, schematic_file=str(root / f"{cell}.spice")))
        assert st["summary"]["match"], st
        found[cell] = st["erc"]["findings"]
    assert [(f["model"], f["terminal"], f["net"], f["tap_net"], f["edge_um"]) for f in found["sw"]] == \
        [("nfet_03v3", "D", "out", "VSS", 1.0)], found
    assert found["strapped"] == []
    shutil.rmtree(root)


@pytest.mark.parametrize("salblock", [False, True])
def test_ihp_erc_finds_a_butted_tap_under_salblock(salblock):
    """IHP's LVS deck joins a salicided n+ drain butted to a p+ tap to the tap (nsd_ptap_abutt): an nfet's drain on out
    against a VSS tap is a short out / VSS that LVS reports, and the ERC, on one net, finds nothing. Under SalBlock the
    junction is a diode: LVS matches, and the ERC finds the drain against the VSS tap (erc_tap: Activ of pSD outside
    the n-well)."""
    pdk = _other("ihp-sg13g2")
    if not verify.decks(pdk)["lvs"] or not tools.find("klayout"):
        pytest.skip("ihp-sg13g2's LVS deck or KLayout is not installed")
    root, srv = _server(pdk, {})
    L = pdk.profile.layers
    ly = kdb.Layout()
    ly.dbu = 0.001
    t = ly.create_cell("sw")

    def box(ld, *b):
        t.shapes(ly.layer(*ld)).insert(kdb.DBox(*b))
    box(L["diff"], 0, 0, 2.0, 1.0)                                    # source, gate at 0.8, drain, tap from 1.6
    box(L["poly"], 0.8, -0.2, 0.93, 1.6)
    box(L["psd"], 1.6, -0.2, 2.2, 1.2)
    if salblock:
        box((28, 0), 1.4, -0.3, 1.8, 1.3)                              # SalBlock over the junction
    for x in (0.2, 1.1, 1.75):                                         # contacts on the source, the drain, the tap
        box(L["contact"], x, 0.42, x + 0.16, 0.58)
        box(L["metal1"], x - 0.05, 0.3, x + 0.21, 0.7)
    box(L["contact"], 0.785, 1.3, 0.945, 1.46)
    box(L["metal1"], 0.7, 1.25, 1.0, 1.55)
    for text, x, y in (("in", 0.28, 0.5), ("out", 1.18, 0.5), ("VSS", 1.83, 0.5), ("g", 0.86, 1.4)):
        t.shapes(ly.layer(*L["metal1.label"])).insert(kdb.DText(text, x, y))
    ly.write(str(root / "sw.gds"))
    srv.db("p", "l").import_oas(root / "sw.gds")
    (root / "ref.spice").write_text(".SUBCKT sw in out g VSS\nM1 out g in VSS sg13_lv_nmos w=1u l=130n ng=1\n.ENDS\n")
    st = _wait(srv.start_lvs("p", "l", "sw", schematic_file=str(root / "ref.spice")))
    assert st["summary"]["match"] is salblock, st
    got = [(f["model"], f["terminal"], f["net"], f["tap_net"], f["edge_um"]) for f in st["erc"]["findings"]]
    assert got == ([("sg13_lv_nmos", "D", "out", "VSS", 1.0)] if salblock else []), st["erc"]
    shutil.rmtree(root)
