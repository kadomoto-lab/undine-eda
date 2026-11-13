"""Schematic-driven layout of parallel devices: the schematic's W (total of nf fingers) and mult become one PCell with
W / nf per finger and nf * mult fingers, strapped into one device; LVS matches and the straps are DRC clean."""
import json
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
from undine.server.api import Server  # noqa: E402
from undine.pdks.pdk import default_pdk_roots, discover  # noqa: E402
from undine.schematics.xschem import XComp  # noqa: E402

CATALOG = {"nfet": {"params": [{"name": "type", "choices": ["sky130_fd_pr__nfet_01v8"]}, {"name": "w"}, {"name": "l"}, {"name": "nf"}]},
           "mim_cap": {"params": [{"name": "type", "choices": ["sky130_fd_pr__model__cap_mim", "sky130_fd_pr__model__cap_mim_m4"]},
                                  {"name": "w"}, {"name": "l"}]}}


def _dev(sym, props):
    return schematic.Device(name="M1", comp=XComp(sym, 0, 0, 0, 0, props), index=0, sym=None, symname=sym, type="nmos")


@pytest.mark.parametrize("props, want", [
    ({"model": "nfet_01v8", "W": "2", "L": "1"}, {"w": 2.0, "l": 1.0}),
    ({"model": "nfet_01v8", "W": "4", "L": "0.5", "nf": "2"}, {"w": 2.0, "l": 0.5, "nf": 2}),          # W is the total
    ({"model": "nfet_01v8", "W": "4", "L": "1", "nf": "1", "mult": "4"}, {"w": 4.0, "l": 1.0, "nf": 4}),
    ({"model": "nfet_01v8", "W": "4", "L": "0.5", "nf": "2", "mult": "3"}, {"w": 2.0, "l": 0.5, "nf": 6}),
])
def test_width_fingers_and_mult(props, want):
    name, params = sdl.device_pcell(_dev("sky130_fd_pr/nfet_01v8.sym", props), props, CATALOG)
    assert name == "nfet" and {k: params[k] for k in want} == want and ("nf" in params) == ("nf" in want)


def test_mim_cap_maps_to_the_pcells_model_name():
    props = {"model": "cap_mim_m3_1", "W": "10", "L": "10"}
    name, params = sdl.device_pcell(_dev("sky130_fd_pr/cap_mim_m3_1.sym", props), props, CATALOG)
    assert name == "mim_cap" and params == {"type": "sky130_fd_pr__model__cap_mim", "w": 10.0, "l": 10.0}


def _wait(job, limit=600):
    t0 = time.time()
    while job.status()["state"] in ("queued", "running") and time.time() - t0 < limit:
        time.sleep(1)
    return job.status()


def test_parallel_nfet_is_one_strapped_device_that_matches_lvs_and_is_drc_clean():
    pdks = discover(default_pdk_roots())
    pdk = next((p for p in pdks if p.startswith("sky130A") and pcell.available(pdks[p])), None)   # a version Undine's PCells run
    if pdk is None:
        pytest.skip("sky130A with its PCell environment is not installed")
    root = Path(tempfile.mkdtemp())
    srv = Server(root, pdks, [root])
    srv.store.create("t", pdk).create_library("l")
    ctx = T.ToolCtx(srv, "t", actor="agent:t")
    T.call(ctx, "open_changeset", {"title": "mult", "reason": "r"})
    call = lambda n, a: T.call(ctx, n, {"lib": "l", "cell": "dev", "reason": "x", **a})
    assert not call("create_cell", {"views": ["schematic", "layout"]})[1]
    assert not call("add_instance", {"symbol": "sky130_fd_pr/nfet_01v8.sym", "x": 0, "y": 0,
                                     "params": {"name": "M1", "W": "4", "L": "1", "mult": "4"}})[1]
    assert not call("connect_nets", {"nets": {"nd": ["M1:D"], "ng": ["M1:G"], "ns": ["M1:S"], "nb": ["M1:B"]}})[1]
    assert json.loads(call("sync_layout", {})[0])["added"] == ["M1"]
    info = json.loads(call("get_layout_info", {})[0])
    m1 = info["instances"][0]
    assert "_w4p0_l1p0_nf4_" in m1["cell"] and m1["cell"].endswith(sdl.STRAP)                 # 4 fingers of 4 um
    assert info["schematic_sync"]["out_of_date"] == []
    st = _wait(srv.prepare_lvs(ctx.work, "l", "dev").launch())
    assert st["state"] == "done" and st["summary"]["match"] is True, st
    st = _wait(srv.prepare_drc(ctx.work, "l", "dev").launch())
    assert st["state"] == "done" and st["summary"]["total"] == 0, st["summary"]
    # a new mult is a new device: sync replaces it in place
    assert not call("set_instance_params", {"instance": "M1", "params": {"mult": "2"}})[1]
    upd = json.loads(call("sync_layout", {})[0])["updated"][0]
    assert "_w4p0_l1p0_nf2_" in upd["to"] and upd["to"].endswith(sdl.STRAP)
    shutil.rmtree(root)
