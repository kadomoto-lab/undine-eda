"""GF180MCU and IHP SG13G2 with their real PDKs (skipped where they are not installed): the decks, Magic / Netgen and
the models run the way the profiles say, on the PDKs' own inverter cells."""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

import klayout.db as kdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine.agents import agent_tools as T  # noqa: E402
from undine.simulation import simresults
from undine import tools
from undine.server.api import Server  # noqa: E402
from undine.pdks.pdk import default_pdk_roots, discover  # noqa: E402
from test_simresults import _tb  # noqa: E402

PDKS = discover(default_pdk_roots())
CELLS = {"gf180mcuD": ("gf180mcu_fd_sc_mcu7t5v0", "gf180mcu_fd_sc_mcu7t5v0__inv_1"), "ihp-sg13g2": ("sg13g2_stdcell", "sg13g2_inv_1")}


def _setup(name):
    pdk = next((p for p in PDKS.values() if p.name == name), None)
    if pdk is None or not tools.find("klayout"):
        pytest.skip(f"{name} or KLayout is not installed")
    lib, cell = CELLS[name]
    root = Path(tempfile.mkdtemp())
    full = kdb.Layout()
    full.read(str(pdk.root / "libs.ref" / lib / "gds" / f"{lib}.gds"))
    one = kdb.Layout()
    one.dbu = full.dbu
    one.create_cell(cell).copy_tree(full.cell(cell))
    one.write(str(root / "inv.gds"))
    spice = (pdk.root / "libs.ref" / lib / "spice" / f"{lib}.spice").read_text()
    sub = spice[spice.lower().index(f".subckt {cell} "):]
    (root / "inv.spice").write_text(sub[:sub.lower().index(".ends") + 5] + "\n")
    srv = Server(root / "projects", PDKS, [root])
    srv.store.create("p", pdk.id).create_library("l")
    srv.db("p", "l").import_oas(root / "inv.gds")
    return root, srv, pdk, cell


def _wait(job):
    while job.status()["state"] in ("queued", "running"):
        time.sleep(0.2)
    st = job.status()
    assert st["state"] == "done", st
    return st["summary"]


@pytest.mark.parametrize("name", ["gf180mcuD", "ihp-sg13g2"])
def test_the_pdk_decks_check_and_compare_its_inverter(name):
    root, srv, pdk, cell = _setup(name)
    drc = _wait(srv.start_drc("p", "l", cell))
    # a lone standard cell has no well taps (they come from tap cells in a row); anything else is a profile mistake
    assert {c["name"] for c in drc["categories"]} <= {"DF.13_MV", "DF.14_MV"}, drc
    # the LVS readers take devices as M (what the symbols' lvs_format writes): IHP's CDL; for GF180 the cell's netlist with
    # the wells as the deck extracts them (substrate SUB, the n-well a net of its own: taps come from tap cells)
    if name == "ihp-sg13g2":
        lib = CELLS[name][0]
        cdl = (pdk.root / "libs.ref" / lib / "cdl" / f"{lib}.cdl").read_text()
        sub = cdl[cdl.index(f".SUBCKT {cell} "):]
        ref = sub[:sub.index(".ENDS") + 5] + "\n"
    else:
        ref = (f".SUBCKT {cell} I ZN VDD VSS SUB\nM0 ZN I VSS SUB nfet_05v0 W=8.2e-07 L=6e-07\n"
               "M1 ZN I VDD nw pfet_05v0 W=1.22e-06 L=5e-07\n.ENDS\n")
    (root / "ref.spice").write_text(ref)
    assert _wait(srv.start_lvs("p", "l", cell, schematic_file=str(root / "ref.spice")))["match"]
    if tools.find("magic") and tools.find("netgen"):
        assert _wait(srv.start_magic_drc("p", "l", cell))["total"] == 0
        assert _wait(srv.start_netgen_lvs("p", "l", cell, schematic_file=str(root / "inv.spice")))["match"]
    shutil.rmtree(root)


def test_gf180_testbench_gets_its_models_and_corners():
    if not tools.find("ngspice"):
        pytest.skip("ngspice not installed")
    root, srv, pdk, _ = _setup("gf180mcuD")
    ctx = _tb(srv, "tb", [("devices/vsource.sym", 0, {"name": "V1", "value": "3.3"}),
                          ("devices/res.sym", 100, {"name": "R1", "value": "10k"}),
                          ("symbols/nfet_03v3.sym", 200, {"name": "M1", "W": "1u", "L": "0.28u"})],
              {"vdd": ["V1:p", "R1:P", "M1:G"], "d": ["R1:M", "M1:D"], "0": ["V1:m", "M1:S", "M1:B"]})
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "tb", "analysis": {"type": "op"}, "signals": ["d"]})
    assert not err, res
    res = json.loads(res)
    assert "typical corner" in res["notes"][0] and "corner.sym" not in res["notes"][0]     # GF180 has no corner symbol
    vd = {"typical": res["node_voltages"]["d"]}
    for corner in ("ss", "ff"):
        job = srv.start_sim(ctx.work, "l", "tb", {"type": "op"}, corner=corner)
        while job.status()["state"] in ("queued", "running"):
            time.sleep(0.2)
        assert job.status()["state"] == "done", job.status()
        assert f"sm141064.ngspice {corner}" in (job.path / "netlist.cir").read_text()
        vd[corner] = simresults.summary(job, ["d"])["node_voltages"]["d"]
    assert vd["ff"] < vd["typical"] < vd["ss"], vd                    # a faster device pulls the drain lower
    shutil.rmtree(root)


def test_wafer_space_suite_runs_density_and_antenna_with_the_main_deck():
    root, srv, pdk, cell = _setup("gf180mcuD")
    runs = dict(srv.suite("p", "l", cell, "wafer-space-gf180"))
    assert {"KLayout gf180mcu.drc", "KLayout density", "KLayout antenna", "Magic DRC"} <= set(runs) or not tools.find("magic")
    assert runs["KLayout density"].inputs["options"] == {"decks": "density"}
    density = _wait(runs["KLayout density"].launch())
    assert {"M1.4", "PL.8"} <= {c["name"] for c in density["categories"]}       # one inverter does not fill a die
    names = [c["name"] for c in srv.signoff("p", "l", cell, "wafer-space-gf180")]
    sign = {c["name"]: c for c in srv.signoff("p", "l", cell, "wafer-space-gf180")}
    assert sign["KLayout density"].get("fresh") and not sign["KLayout gf180mcu.drc"].get("job")   # the main run is another check
    assert len(names) == len(set(names))
    shutil.rmtree(root)


def test_ihp_testbench_runs_with_its_osdi_models_at_each_corner():
    if not tools.find("ngspice") or not tools.ngspice_osdi(tools.find("ngspice")):
        pytest.skip("ngspice with OSDI is not installed (pixi run ngspice)")
    root, srv, pdk, _ = _setup("ihp-sg13g2")
    if not all(f.is_file() for f in pdk.osdi_files()):
        shutil.rmtree(root)
        pytest.skip("IHP's Verilog-A models are not compiled (pixi run osdi)")
    ctx = _tb(srv, "tb", [("devices/vsource.sym", 0, {"name": "V1", "value": "1.2"}),
                          ("devices/res.sym", 100, {"name": "R1", "value": "10k"}),
                          ("sg13g2_pr/sg13_lv_nmos.sym", 200, {"name": "M1", "w": "1u", "l": "0.13u"})],
              {"vdd": ["V1:p", "R1:P", "M1:G"], "d": ["R1:M", "M1:D"], "0": ["V1:m", "M1:S", "M1:B"]})
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "tb", "analysis": {"type": "op"}, "signals": ["d"]})
    assert not err, res
    res = json.loads(res)
    assert "mos_tt corner" in res["notes"][0]
    vd = {"mos_tt": res["node_voltages"]["d"]}
    for corner in ("mos_ss", "mos_ff"):
        job = srv.start_sim(ctx.work, "l", "tb", {"type": "op"}, corner=corner)
        while job.status()["state"] in ("queued", "running"):
            time.sleep(0.2)
        assert job.status()["state"] == "done", job.status()
        init = (job.path / ".spiceinit").read_text()
        assert all(f"osdi '{f}'" in init for f in pdk.osdi_files()), init
        vd[corner] = simresults.summary(job, ["d"])["node_voltages"]["d"]
    assert vd["mos_ff"] < vd["mos_tt"] < vd["mos_ss"] < 1.2, vd
    shutil.rmtree(root)


@pytest.mark.parametrize("name,pcell_name,params,model,w", [
    ("gf180mcuD", "nfet", {"w_gate": 2.0, "l_gate": 0.5, "nf": 2, "bulk": "Bulk Tie"}, "nfet_03v3", "2U"),   # W per finger
    ("ihp-sg13g2", "nmos", {"w": "2u", "l": "0.13u", "ng": "2"}, "sg13_lv_nmos", "1u"),                      # W in total
])
def test_the_pdk_pcells_generate_devices_its_lvs_extracts(name, pcell_name, params, model, w):
    from undine.pdks import pcell
    pdk = next((p for p in PDKS.values() if p.name == name), None)
    if pdk is None or not pcell.available(pdk) or not tools.find("klayout"):
        pytest.skip(f"{name}, its PCell environment or KLayout is not installed")
    assert pcell_name in {c["name"] for c in pcell.catalog(pdk)}
    root = Path(tempfile.mkdtemp())
    srv = Server(root / "projects", PDKS, [root])
    srv.store.create("p", pdk.id).create_library("l")
    path, cell, _ = pcell.generate(pdk, pcell_name, params)
    srv.db("p", "l").import_oas(path)
    (root / "ref.spice").write_text(f".SUBCKT {cell}\n.ENDS\n")         # an empty reference: only the extraction counts
    job = srv.start_lvs("p", "l", cell, schematic_file=str(root / "ref.spice"))
    assert _wait(job)["match"] is False
    devs = [x for x in (job.path / "extracted.cir").read_text().splitlines() if x.startswith("M")]
    assert len(devs) == 2 and all(model in d and f"W={w}" in d for d in devs), devs
    shutil.rmtree(root)
