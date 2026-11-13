"""Schematic-driven layout on GF180MCU and IHP SG13G2: their symbols' devices map to the PDKs' own PCells (lengths as
SPICE values, the fingers and m as more fingers), multi-finger devices are strapped by the profile's rules into one
device, and each PDK's decks see one DRC-clean device with the schematic's total width."""
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest  # noqa: E402

from undine.agents import agent_tools as T  # noqa: E402
from undine.pdks import pcell  # noqa: E402
from undine.schematics import schematic
from undine.layouts import sdl
from undine import tools
from undine.server.api import Server  # noqa: E402
from undine.pdks.pdk import default_pdk_roots, discover  # noqa: E402
from undine.schematics.xschem import XComp  # noqa: E402

GF = {"nfet": {"params": [{"name": "volt", "choice_values": ["3.3V", "5V", "6V"]}, {"name": "w_gate"}]},
      "pfet": {"params": [{"name": "volt", "choice_values": ["3.3V", "5V", "6V"]}]}, "nfet_06v0_nvt": {"params": []}}
IHP = {"nmos": {"params": [{"name": "guardRingType", "choice_values": ["'none'", "'psub'"]}]}, "pmos": {"params": []}}


def _dev(sym, props):
    return schematic.Device(name="M1", comp=XComp(sym, 0, 0, 0, 0, props), index=0, sym=None, symname=sym, type="nmos")


def test_lengths_are_read_as_spice_values():
    assert sdl._um("0.28u") == 0.28 and sdl._um("280n") == 0.28 and sdl._um("2.8e-7") == 0.28
    assert sdl._um("2") == 2.0 and sdl._um("") is None and sdl._um("'1u'") == 1.0


@pytest.mark.parametrize("sym, props, want", [
    ("symbols/nfet_03v3.sym", {"model": "nfet_03v3", "W": "4u", "L": "0.5u", "nf": "2"},
     ("nfet", {"volt": "3.3V", "w_gate": 2.0, "l_gate": 0.5, "nf": 2})),                       # W is the total of nf fingers
    ("symbols/pfet_05v0.sym", {"model": "pfet_05v0", "W": "2u", "L": "0.6u", "m": "3"},
     ("pfet", {"volt": "5V", "w_gate": 2.0, "l_gate": 0.6, "nf": 3})),
    ("symbols/nfet_06v0_nvt.sym", {"model": "nfet_06v0_nvt", "W": "10u", "L": "1.8u"}, ("nfet_06v0_nvt", {"w_gate": 10.0, "l_gate": 1.8})),
])
def test_gf180_devices_map_to_its_pcells(sym, props, want):
    name, params = sdl.device_pcell(_dev(sym, props), props, GF, "gf180mcu")
    assert name == want[0] and {k: params[k] for k in want[1]} == want[1] and params["bulk"] == "Bulk Tie"


@pytest.mark.parametrize("props, want", [
    ({"model": "sg13_lv_nmos", "w": "4u", "l": "0.13u", "ng": "2"}, {"w": "4u", "l": "0.13u", "ng": "2"}),   # w is the total
    ({"model": "sg13_lv_nmos", "w": "2u", "l": "0.13u", "m": "2"}, {"w": "4u", "l": "0.13u", "ng": "2"}),
])
def test_ihp_devices_map_to_its_pcells(props, want):
    name, params = sdl.device_pcell(_dev("sg13g2_pr/sg13_lv_nmos.sym", props), props, IHP, "ihp-sg13g2")
    assert name == "nmos" and {k: params[k] for k in want} == want and params["guardRingType"] == "'psub'"
    assert sdl.device_pcell(_dev("x.sym", {"model": "rppd"}), {"model": "rppd"}, IHP, "ihp-sg13g2") is None     # not yet


CASES = {
    "gf180mcuD": [("symbols/nfet_03v3.sym", {"W": "4u", "L": "0.5u", "nf": "2"}, "nfet_03v3", 4.0),
                  ("symbols/pfet_03v3.sym", {"W": "4u", "L": "0.5u", "m": "2"}, "pfet_03v3", 8.0)],
    "ihp-sg13g2": [("sg13g2_pr/sg13_lv_nmos.sym", {"w": "1u", "l": "0.13u"}, "sg13_lv_nmos", 1.0),      # one gate: a contact all the same
                   ("sg13g2_pr/sg13_lv_nmos.sym", {"w": "4u", "l": "0.13u", "ng": "2"}, "sg13_lv_nmos", 4.0),
                   ("sg13g2_pr/sg13_lv_pmos.sym", {"w": "2u", "l": "0.13u", "m": "2"}, "sg13_lv_pmos", 4.0)],
}


def _wait(job):
    while job.status()["state"] in ("queued", "running"):
        time.sleep(0.3)
    return job.status()


@pytest.mark.parametrize("name", list(CASES))
def test_strapped_devices_are_one_drc_clean_device(name):
    pdks = discover(default_pdk_roots())
    pdk = next((p for p in pdks.values() if p.name == name and pcell.available(p)), None)
    if pdk is None or not tools.find("klayout"):
        pytest.skip(f"{name}, its PCell environment or KLayout is not installed")
    root = Path(tempfile.mkdtemp())
    srv = Server(root, pdks, [root])
    srv.store.create("t", pdk.id).create_library("l")
    ctx = T.ToolCtx(srv, "t", actor="agent:t")
    T.call(ctx, "open_changeset", {"title": "sdl", "reason": "r"})
    for i, (sym, props, model, width) in enumerate(CASES[name]):
        call = lambda n, a: T.call(ctx, n, {"lib": "l", "cell": f"d{i}", "reason": "x", **a})
        assert not call("create_cell", {"views": ["schematic", "layout"]})[1]
        assert not call("add_instance", {"symbol": sym, "x": 0, "y": 0, "params": {"name": "M1", **props}})[1]
        assert not call("connect_nets", {"nets": {"nd": ["M1:D"], "ng": ["M1:G"], "ns": ["M1:S"], "nb": ["M1:B"]}})[1]
        assert json.loads(call("sync_layout", {})[0])["added"] == ["M1"]
        cell = json.loads(call("get_layout_info", {})[0])["instances"][0]["cell"]
        assert cell.endswith(sdl.STRAP)
        if pdk.profile.strap.cont:                    # IHP: each gate contacted under a metal1 pad, so it routes in metal1
            db = srv.db(ctx.work, "l")
            cont = db.layout.find_layer(*pdk.profile.layers["contact"])
            fingers = int(re.search(r"_ng(\d+)_", cell).group(1)) if "_ng" in cell else 1
            assert db.cell(cell).shapes(cont).size() == fingers
        drc = _wait(srv.prepare_drc(ctx.work, "l", f"d{i}").launch())["summary"]
        assert drc["total"] == 0, drc
        job = srv.prepare_lvs(ctx.work, "l", f"d{i}").launch()
        _wait(job)
        devs = [l for l in (job.path / "extracted.cir").read_text().replace("\n+", " ").splitlines() if l.startswith("M")]
        nets = [tuple(d.split()[1:5]) for d in devs]
        assert {model} == {d.split()[5] for d in devs}
        assert len({n[1] for n in nets}) == 1 and len({frozenset((n[0], n[2])) for n in nets}) == 1   # one gate, one S / D pair
        assert sum(float(re.search(r"W=([0-9.]+)", d, re.I).group(1)) for d in devs) == pytest.approx(width)
    shutil.rmtree(root)


def test_netlist_formats_escape_and_evaluate_like_xschem():
    class Dev:
        nets, pins, symname = {"p": "out"}, [], "x"
    fmt = lambda f, props: schematic._fmt(f, props, Dev(), {})
    assert fmt(r"@name @prefix\and4_1", {"name": "x1", "prefix": "sky130_fd_sc_hd__"}) == "x1 sky130_fd_sc_hd__and4_1"
    assert fmt(r"$[@model\] .save v(@@p\)", {"model": "rppd"}) == "$[rppd] .save v(out)"
    v = schematic._value("expr_eng( ( 9.0e-6 / @w + 7.0 * ( @l ) / ( @w + 1.0e-8 ) ) / @m )", {"w": "0.5e-6", "l": "2e-6", "m": "1"}, {})
    assert float(v) == pytest.approx(45.4509803922)                                 # IHP's rsil value


def test_model_calls_stay_devices_in_lvs_references():
    text = ".subckt top a b\nXR1 a b sub ppolyf_u r_width=1e-6 r_length=4e-6 m=1\nXC1 a b cap_mim_2f0fF c_width=5e-6 c_length=5e-6\n.ends\n"
    gf = __import__("undine.pdks.pdkprofile", fromlist=["GF180"]).GF180
    out = gf.lvs_netlist(text)
    assert "RR1 a b sub ppolyf_u W=1e-6 L=4e-6 m=1" in out and "CC1 a b cap_mim_2f0fF W=5e-6 L=5e-6" in out
    assert "XR1 a b sub ppolyf_u r_width=1e-6" in schematic.flatten_spice(text, "top")      # not a subcircuit: kept as is


@pytest.mark.parametrize("family, sym, props, want", [
    ("gf180mcu", "symbols/ppolyf_u.sym", {"model": "ppolyf_u", "W": "1e-6", "L": "4e-6"}, ("ppolyf_u_resistor", {"w_res": 1.0, "l_res": 4.0})),
    ("gf180mcu", "symbols/rm2.sym", {"model": "rm2", "W": "0.5e-6", "L": "10e-6"}, ("metal_resistor", {"res_type": "rm2", "w_res": 0.5})),
    ("gf180mcu", "symbols/cap_mim_2f0fF.sym", {"model": "cap_mim_2f0fF", "W": "5e-6", "L": "5e-6"}, ("cap_mim", {"mim_option": "MIM-B", "wc": 5.0})),
    ("ihp-sg13g2", "sg13g2_pr/rppd.sym", {"model": "rppd", "w": "0.5e-6", "l": "2e-6"}, ("rppd", {"w": "0.5u", "l": "2u", "Calculate": "R"})),
    ("ihp-sg13g2", "sg13g2_pr/cap_cmim.sym", {"model": "cap_cmim", "w": "5e-6", "l": "5e-6"}, ("cmim", {"w": "5u", "Calculate": "C"})),
])
def test_passives_map_to_the_pdk_pcells(family, sym, props, want):
    cat = {"ppolyf_u_resistor": {"params": []}, "metal_resistor": {"params": []}, "cap_mim": {"params": []},
           "rppd": {"params": [{"name": "Calculate", "choice_values": ["R", "w", "l"]}]},
           "cmim": {"params": [{"name": "Calculate", "choice_values": ["C", "w", "l", "w&l"]}]}}
    name, params = sdl.device_pcell(_dev(sym, props), props, cat, family)
    assert name == want[0] and {k: params[k] for k in want[1]} == want[1]
    assert sdl.device_pcell(_dev(sym, {**props, "m": "2"}), {**props, "m": "2"}, cat, family) is None   # parallel copies: not yet


PASSIVES = {
    "gf180mcuD": [("symbols/ppolyf_u.sym", {"W": "1e-6", "L": "4e-6"}, ["M", "P", "B"]), ("symbols/rm1.sym", {"W": "0.5e-6", "L": "10e-6"}, ["M", "P"]),
                  ("symbols/cap_mim_2f0fF.sym", {"W": "5e-6", "L": "5e-6"}, ["G", "B"])],
    "ihp-sg13g2": [("sg13g2_pr/rppd.sym", {"w": "0.5e-6", "l": "2e-6"}, ["P", "M"]), ("sg13g2_pr/cap_cmim.sym", {"w": "5e-6", "l": "5e-6"}, ["c0", "c1"])],
}


@pytest.mark.parametrize("name", list(PASSIVES))
def test_passives_are_drc_clean_and_compare_with_the_schematic(name):
    pdks = discover(default_pdk_roots())
    pdk = next((p for p in pdks.values() if p.name == name and pcell.available(p)), None)
    if pdk is None or not tools.find("klayout"):
        pytest.skip(f"{name}, its PCell environment or KLayout is not installed")
    root = Path(tempfile.mkdtemp())
    srv = Server(root, pdks, [root])
    srv.store.create("t", pdk.id).create_library("l")
    ctx = T.ToolCtx(srv, "t", actor="agent:t")
    T.call(ctx, "open_changeset", {"title": "sdl", "reason": "r"})
    for i, (sym, props, pins) in enumerate(PASSIVES[name]):
        call = lambda n, a: T.call(ctx, n, {"lib": "l", "cell": f"p{i}", "reason": "x", **a})
        assert not call("create_cell", {"views": ["schematic", "layout"]})[1]
        assert not call("add_instance", {"symbol": sym, "x": 0, "y": 0, "params": {"name": "R1", **props}})[1]
        assert not call("connect_nets", {"nets": {f"n{p.lower()}": [f"R1:{p}"] for p in pins}})[1]
        assert json.loads(call("sync_layout", {})[0])["added"] == ["R1"]
        assert _wait(srv.prepare_drc(ctx.work, "l", f"p{i}").launch())["summary"]["total"] == 0
        lvs = _wait(srv.prepare_lvs(ctx.work, "l", f"p{i}").launch())
        assert lvs["summary"]["circuits"][0]["status"] == "Match", lvs   # the pins are unlabelled: the device and its size match
    shutil.rmtree(root)
