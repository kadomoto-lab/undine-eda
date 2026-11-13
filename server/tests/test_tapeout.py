"""Shuttle target checks (Tiny Tapeout SKY130 analog, wafer.space GF180MCU, ChipFoundry Caravel) and the submission bundle."""
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import klayout.db as kdb  # noqa: E402
from undine.checks import tapeout  # noqa: E402
from undine.pdks.pdk import Pdk  # noqa: E402

PDK = Pdk(name="sky130A", family="sky130", version="x", root=Path("/nonexistent/sky130A"))
M4, M4L, M4P, M5, PRB, VIA3 = (71, 20), (71, 5), (71, 16), (72, 20), (235, 4), (70, 44)
UM = 1000
P = {"top_module": "tt_um_demo", "tiles": "1x2", "analog_pins": "1", "ua": "analog input"}


def tile(extra=None, vgnd_w=2.0, height=225.76, ua0=True, name="mycell"):
    """A conforming 1x2 tile: prBoundary, every template pin on met4, two power stripes, ua[0] wired to a pad ring."""
    tpl = tapeout.tt_template("1x2")
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell(name)
    m4, lab, pin = ly.layer(*M4), ly.layer(*M4L), ly.layer(*M4P)
    top.shapes(ly.layer(*PRB)).insert(kdb.Box(0, 0, tpl["die"][0], tpl["die"][1]))
    for p in tpl["pins"].values():
        top.shapes(m4).insert(kdb.Box(*p["rect"]))
        top.shapes(pin).insert(kdb.Box(*p["rect"]))

    def stripe(label, x, w):
        top.shapes(m4).insert(kdb.Box(int(x * UM), 5 * UM, int((x + w) * UM), int((height - 5) * UM)))
        top.shapes(pin).insert(kdb.Box(int(x * UM), 5 * UM, int((x + w) * UM), int((height - 5) * UM)))
        top.shapes(lab).insert(kdb.Text(label, kdb.Trans(int((x + w / 2) * UM), 100 * UM)))
    stripe("VGND", 100, vgnd_w)
    stripe("VDPWR", 110, 2.0)
    if ua0:                                           # ua[0] pad is 151.81..152.71 x 0..1: a met4 wire 0.2 um beside it
        top.shapes(m4).insert(kdb.Box(151400, 0, 151600, 20 * UM))
    if extra:
        extra(ly, top)
    return ly


def status(checks):
    return {c["id"]: c["status"] for c in checks}


def run(ly, **kw):
    return status(tapeout.run_checks(ly, "mycell", PDK, "tt-sky130-analog", {**P, **kw}))


def test_tt_checks_pass_and_fail():
    s = run(tile())
    assert all(s[k] == "pass" for k in ("tt_top", "tt_no_met5", "tt_boundary", "tt_cellnames", "tt_pins", "tt_power", "tt_overlap",
                                         "tt_analog", "tt_urpm", "dbu")), s
    assert s["tt_verilog"] == "manual"                                           # what cannot be verified is never "pass"
    assert run(tile(), top_module="demo")["tt_top"] == "fail"
    assert run(tile(extra=lambda l, t: t.shapes(l.layer(*M5)).insert(kdb.Box(0, 0, 1000, 1000))))["tt_no_met5"] == "fail"
    assert run(tile(vgnd_w=1.0))["tt_power"] == "fail"
    assert run(tile(height=225.76), tiles="1x2")["tt_boundary"] == "pass"
    assert run(tile(extra=lambda l, t: t.shapes(l.layer(*M4)).insert(kdb.Box(0, 0, 10 * UM, 300 * UM))))["tt_boundary"] == "fail"
    nob = tile()
    nob.cell("mycell").shapes(nob.layer(*PRB)).clear()
    assert run(nob)["tt_boundary"] == "fail"                                      # prBoundary 235/4 is required
    assert run(tile(ua0=False))["tt_analog"] == "fail"                            # declared but not wired
    assert run(tile(), analog_pins="0", ua="")["tt_analog"] == "fail"             # wired but not declared
    assert run(tile(), ua="")["tt_analog"] == "fail"                              # no description
    first = next(iter(tapeout.tt_template("1x2")["pins"].values()))["rect"]
    nopin = tile(extra=lambda l, t: None)
    reg = kdb.Region(nopin.cell("mycell").begin_shapes_rec(nopin.layer(*M4))) - kdb.Region(kdb.Box(*first))
    nopin.cell("mycell").shapes(nopin.layer(*M4)).clear()
    nopin.cell("mycell").shapes(nopin.layer(*M4)).insert(reg)
    assert run(nopin)["tt_pins"] == "fail"
    nop = tile()
    nop.cell("mycell").shapes(nop.layer(*M4P)).clear()                           # drawn on met4 but not on the pin purpose
    r = run(nop)
    assert r["tt_pins"] == "fail" and r["tt_power"] == "fail"
    try:
        tapeout.run_checks(tile(), "mycell", Pdk("gf180mcuD", "gf180mcu", "", Path("/x")), "tt-sky130-analog", {})
        raise AssertionError("wrong PDK accepted")
    except ValueError:
        pass


def test_export_bundle_renames_top_and_writes_template_files():
    root = Path(tempfile.mkdtemp())
    ly = tile()
    r = tapeout.export_bundle(root, ly, "lib", "mycell", PDK, "tt-sky130-analog", P, "rev1")
    d = root / r["dir"]
    g = kdb.Layout()
    g.read(str(d / "gds" / "tt_um_demo.gds"))
    assert [c.name for c in g.top_cells()] == ["tt_um_demo"] and abs(g.dbu - 0.001) < 1e-12
    lef = (d / "lef" / "tt_um_demo.lef").read_text()
    assert "MACRO tt_um_demo" in lef and "SIZE 161.000 BY 225.760" in lef
    for pin in ("VGND", "VDPWR", "ua[0]", "ua[7]", "clk", "uio_oe[7]"):
        assert f"PIN {pin}\n" in lef, pin
    assert "USE GROUND" in lef and "USE POWER" in lef
    v = (d / "gds" / "tt_um_demo.v").read_text()
    assert "module tt_um_demo" in v and "VGND" in v and "VDPWR" in v and "ua" in v
    assert (d / "src" / "project.v").is_file() and (d / "docs" / "info.md").is_file()
    y = (d / "info.yaml").read_text()
    assert 'top_module:  "tt_um_demo"' in y and "analog_pins: 1" in y and 'ua[0]: "analog input"' in y and "yaml_version: 6" in y
    rep = json.loads((d / "report.json").read_text())
    assert rep["design_revision"] == "rev1" and rep["files"]["gds/tt_um_demo.gds"] and rep["checks"]
    assert "tt_um_demo.gds" in " ".join(zipfile.ZipFile(root / "tapeout" / "tt-sky130-analog" / r["zip"]).namelist())
    shutil.rmtree(root)


def test_analog_pins_must_be_a_number_and_blank_means_wired():
    assert run(tile(), analog_pins="")["tt_analog"] == "pass"                      # one ua pin is wired
    try:
        run(tile(), analog_pins="test input, tete output")
        raise AssertionError("text accepted as a pin count")
    except ValueError as e:
        assert "number" in str(e)


def test_flatten_spice_inlines_subcircuits_and_keeps_element_letters():
    from undine.schematics import schematic
    txt = (".subckt top a y vdd gnd\nx1 a y vdd gnd inv\n.ends\n"
           ".subckt inv A Y VDD VSS\nM1 Y A VDD VDD pfet L=0.15 W=2\nM2 Y A VSS VSS nfet L=0.15 W=1\nR1 Y n1 1k\n.ends\n.end\n")
    out = schematic.flatten_spice(txt, "top").splitlines()
    assert out[0] == ".subckt top a y vdd gnd" and "M1.x1 y a vdd vdd pfet L=0.15 W=2" in out and "M2.x1 y a gnd gnd nfet L=0.15 W=1" in out
    assert "R1.x1 y x1.n1 1k" in out                                              # internal nets are made unique per instance


def test_generic_zero_area_and_dbu():
    ly = kdb.Layout()
    ly.dbu = 0.005
    c = ly.create_cell("t")
    c.shapes(ly.layer(68, 20)).insert(kdb.Box(0, 0, 100, 100))
    c.shapes(ly.layer(68, 20)).insert(kdb.Box(0, 0, 0, 50))
    s = status(tapeout.run_checks(ly, "t", PDK, "generic-layout", {}))
    assert s["dbu"] == "fail" and s["zero_area"] == "fail" and s["origin"] == "pass"



# ----------------------------------------------------------------------------------- wafer.space

GF = Pdk(name="gf180mcuD", family="gf180mcu", version="792ef67037d74c8c51811eab3ce23cf793491296", root=Path("/nonexistent/gf180mcuD"))


def die(slot="0p5x0p5", size=None, ring=True, fill=0.5, extra=None):
    """A die of the slot's size at the origin: seal ring marker, COMP / Poly2 / Metal1-5 covering `fill` of it (half
    of the metal as dummy fill, datatype 4)."""
    w, h = size or tapeout.ws_slot_size(slot)
    ly = kdb.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("chip_top")
    W, H = int(w * UM), int(h * UM)
    if ring:
        top.shapes(ly.layer(167, 5)).insert(kdb.Box(0, 0, W, H))
    band = int(H * fill)
    for layer in (22, 30, 34, 36, 42, 46, 81):
        top.shapes(ly.layer(layer, 0)).insert(kdb.Box(0, 0, W // 2, band))
        top.shapes(ly.layer(layer, 4 if layer != 22 else 0)).insert(kdb.Box(W // 2, 0, W, band))
    if extra:
        extra(ly, top)
    return ly


def ws(ly, pdk=GF, **params):
    return status(tapeout.run_checks(ly, "chip_top", pdk, "wafer-space-gf180", {"slot": "0p5x0p5", "cob": "no", **params}))


def test_wafer_space_die_checks():
    assert tapeout.ws_slot_size("1x1") == (3932, 5122) and tapeout.ws_slot_size("0p5x1") == (1936, 5122)
    assert tapeout.ws_slot_size("1x0p5") == (3932, 2531)                         # the saw street between the halves
    s = ws(die())
    assert all(s[k] == "pass" for k in ("ws_pdk", "ws_origin", "ws_5lm", "ws_seal_ring", "ws_slot", "ws_density", "dbu")), s
    assert "origin" not in s and "ws_id_cells" not in s                         # the stricter origin; no CoB, no ID cells
    assert ws(die(), slot="1x1")["ws_slot"] == "fail"
    assert ws(die(size=(1936, 2530)))["ws_slot"] == "fail"
    assert ws(die(ring=False))["ws_seal_ring"] == "fail"
    assert ws(die(fill=0.2))["ws_density"] == "fail"                            # 20% metal
    assert ws(die(fill=0.8))["ws_density"] == "fail"                            # 80% COMP is too much
    assert ws(die(extra=lambda l, t: t.shapes(l.layer(53, 0)).insert(kdb.Box(0, 0, 1000, 1000))))["ws_5lm"] == "fail"
    assert ws(die(extra=lambda l, t: t.shapes(l.layer(22, 0)).insert(kdb.Box(-1000, 0, 0, 1000))))["ws_origin"] == "fail"
    old = Pdk(name="gf180mcuD", family="gf180mcu", version="78b7bc32", root=GF.root)
    assert ws(die(), pdk=old)["ws_pdk"] == "warn"
    assert ws(die(), pdk=Pdk(name="gf180mcuC", family="gf180mcu", version=GF.version, root=GF.root))["ws_pdk"] == "fail"


def test_wafer_space_chip_on_board_needs_the_id_cells_in_place():
    w, h = tapeout.ws_slot_size("0p5x0p5")
    boxes = tapeout.ws_id_boxes(w, h)

    def place(ly, top, skip=None, shift=0):
        for name, (x1, y1, x2, y2) in boxes.items():
            if name == skip:
                continue
            c = ly.create_cell(name)
            c.shapes(ly.layer(34, 0)).insert(kdb.Box(0, 0, int((x2 - x1) * UM), int((y2 - y1) * UM)))
            top.insert(kdb.CellInstArray(c.cell_index(), kdb.Trans(int(x1 * UM) + shift, int(y1 * UM))))
    s = ws(die(extra=place), cob="yes")
    assert s["ws_id_cells"] == "pass" and s["ws_pad_mask"] == "manual"
    assert ws(die(extra=lambda l, t: place(l, t, skip="gf180mcu_ws_ip__marker")), cob="yes")["ws_id_cells"] == "fail"
    assert ws(die(extra=lambda l, t: place(l, t, shift=1000)), cob="yes")["ws_id_cells"] == "fail"


def test_wafer_space_bundle_says_how_to_submit():
    root = Path(tempfile.mkdtemp())
    r = tapeout.export_bundle(root, die(), "lib", "chip_top", GF, "wafer-space-gf180", {"slot": "0p5x0p5", "cob": "no"})
    d = root / r["dir"]
    sub = (d / "SUBMIT.md").read_text()
    assert "precheck.py --input gds/chip_top.gds --top chip_top --slot 0p5x0p5" in sub and "--cob" not in sub
    assert sorted(r["files"]) == ["SUBMIT.md", "gds/chip_top.gds", "report.json"]
    assert json.loads((d / "report.json").read_text())["files"]["SUBMIT.md"]
    shutil.rmtree(root)


# ----------------------------------------------------------------------------------- ChipFoundry

def wrapper(content=True, edge=None, name="user_project_wrapper", dbu=0.001):
    """A stand-in for Caravel's empty wrapper: a met4 / met5 ring outside the user area, a met2 pin stub crossing the
    bottom edge and a met3 one on the left, the prBoundary; with `content` a met1 block inside."""
    ly = kdb.Layout()
    ly.dbu = dbu
    top = ly.create_cell(name)
    u = lambda *v: kdb.DBox(*v)
    top.shapes(ly.layer(71, 20)).insert(u(-10.03, -4.67, -6.93, 3524.67))                 # vertical ring sides on met4
    top.shapes(ly.layer(71, 20)).insert(u(2926.93, -4.67, 2930.03, 3524.67))
    top.shapes(ly.layer(72, 20)).insert(u(-10.03, -4.67, 2930.03, -1.57))                  # horizontal ones on met5
    top.shapes(ly.layer(72, 20)).insert(u(-10.03, 3521.57, 2930.03, 3524.67))
    top.shapes(ly.layer(69, 20)).insert(u(100, -4.8, 100.56, 2.4))                         # io pin stubs
    top.shapes(ly.layer(69, 16)).insert(u(100, -4.8, 100.56, 2.4))
    top.shapes(ly.layer(69, 5)).insert(kdb.DText("io_in[0]", 100.28, -1))
    top.shapes(ly.layer(70, 20)).insert(u(-4.8, 1000, 2.4, 1001.2))
    top.shapes(ly.layer(235, 4)).insert(u(0, 0, 2920, 3520))
    if content:
        top.shapes(ly.layer(68, 20)).insert(u(500, 500, 1500, 1500))
    if edge:
        edge(ly, top)
    return ly


def cf(ly, cell="user_project_wrapper", golden=None, **params):
    return status(tapeout.run_checks(ly, cell, PDK, "chipfoundry-caravel", {"wrapper": "digital", **params}, golden))


def test_chipfoundry_checks_and_the_edge_xor():
    root = Path(tempfile.mkdtemp())
    golden = root / "user_project_wrapper_empty.gds.gz"
    wrapper(content=False, dbu=0.0005).write(str(golden))                                  # another dbu, compressed
    s = cf(wrapper(name="mydesign"), "mydesign", golden)                                  # renamed to the wrapper name
    assert all(s[k] == "pass" for k in ("cf_cellnames", "cf_prboundary", "cf_extent", "cf_xor", "dbu")), s
    assert s["cf_gpio"] == s["cf_oeb"] == "manual" and "cf_pdk" not in s
    assert "origin" not in s                                                              # the rings sit below 0,0
    assert cf(wrapper())["cf_xor"] == "manual"                                            # without the golden file
    strap = lambda l, t: t.shapes(l.layer(72, 20)).insert(kdb.DBox(-30, 200, 2950, 203.1))  # met5 strap into the side rings
    assert cf(wrapper(edge=strap), golden=golden)["cf_xor"] == "pass"
    moved = lambda l, t: t.shapes(l.layer(69, 20)).insert(kdb.DBox(200, -4.8, 200.56, 2.4))
    r = tapeout.run_checks(wrapper(edge=moved), "user_project_wrapper", PDK, "chipfoundry-caravel", {"wrapper": "digital"}, golden)
    x = next(c for c in r if c["id"] == "cf_xor")
    assert x["status"] == "fail" and "69/20: 1" in x["detail"]
    far = lambda l, t: t.shapes(l.layer(68, 20)).insert(kdb.DBox(-100, 0, -50, 10))
    assert cf(wrapper(edge=far))["cf_extent"] == "fail"
    assert cf(wrapper(edge=lambda l, t: l.clear_layer(l.find_layer(235, 4))))["cf_prboundary"] == "fail"
    child = lambda l, t: t.insert(kdb.CellInstArray(l.create_cell("a#b").cell_index(), kdb.Trans()))
    assert cf(wrapper(edge=child))["cf_cellnames"] == "fail"
    other = Pdk(name="sky130C", family="sky130", version="", root=PDK.root)                 # a sky130 cf-precheck does not take
    assert status(tapeout.run_checks(wrapper(), "user_project_wrapper", other, "chipfoundry-caravel", {}))["cf_pdk"] == "fail"
    b = tapeout.export_bundle(root, wrapper(name="mydesign"), "lib", "mydesign", PDK, "chipfoundry-caravel",
                              {"wrapper": "analog"}, golden=None)
    assert "gds/user_analog_project_wrapper.gds" in b["files"]
    assert "cf push" in (root / b["dir"] / "SUBMIT.md").read_text()
    shutil.rmtree(root)


def test_the_golden_wrapper_is_read_only_from_allowed_directories():
    from fastapi.testclient import TestClient
    from undine.server.api import Server, create_app
    root = Path(tempfile.mkdtemp())
    srv = Server(root / "projects", {}, [root / "refs"])
    srv.store.create("p").create_library("l")
    (root / "refs").mkdir()
    wrapper(content=False).write(str(root / "refs" / "empty.gds"))
    wrapper().write(str(root / "w.gds"))
    srv.db("p", "l").import_oas(root / "w.gds")
    c = TestClient(create_app(srv, None))
    url = "/api/p/p/l/l/tapeout/user_project_wrapper/check"
    body = lambda g: {"target": "generic-layout", "params": {"golden_gds": g}}
    assert c.post(url, json=body(str(root / "w.gds"))).status_code == 403              # outside --import-root
    assert c.post(url, json=body(str(root / "refs" / "none.gds"))).status_code == 404
    assert c.post(url, json=body(str(root / "refs" / "empty.gds"))).status_code == 200
    shutil.rmtree(root)

if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
