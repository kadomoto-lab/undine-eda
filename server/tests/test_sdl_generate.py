"""Schematic-driven layout a designer steers: sync_layout makes a cell's layout view, places every unit of a MIM's MF at
the PDK's pitch, draws a MOSFET in the fingers its layout parameters ask for (kept with the layout), ties a tap only
to a source / drain on the bulk net; place_pcell, place_array (common centroid, interdigitated), and the instance
tools (delete, move, rotate, restore). Checked against the real KLayout and Magic decks and KLayout LVS on sky130A where
the PDK and its PCell environment are installed; the arrangements and parameter checks without them."""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import klayout.db as kdb  # noqa: E402
import pytest  # noqa: E402

from undine.agents import agent_tools as T  # noqa: E402
from undine.layouts import placement, sdl  # noqa: E402
from undine.pdks import pcell, pdkprofile  # noqa: E402
from undine.pdks.pdk import default_pdk_roots, discover  # noqa: E402
from undine.server.api import Server  # noqa: E402

PDKS = discover(default_pdk_roots())
SKY = next((p for k, p in PDKS.items() if k.startswith("sky130A") and pcell.available(p)), None)
real = pytest.mark.skipif(SKY is None, reason="sky130A with its PCell environment is not installed")


# ---------------------------------------------------------------------------------- without tools


def test_common_centroid_puts_every_even_device_on_the_centre():
    counts = {**{f"C{i}": 2 ** i for i in range(10)}, "CDUM": 1}                   # a binary CDAC: 1024 units
    cols, rows = placement.grid_for(sum(counts.values()))
    assert (cols, rows) == (32, 32)
    grid = placement.common_centroid(counts, cols, rows)
    assert {k: sum(r.count(k) for r in grid) for k in counts} == counts
    off = placement.centroid_error(grid, (4.17, 2.71))
    assert all(off[f"C{i}"] == [0.0, 0.0] for i in range(1, 10))                  # the odd ones share the middle pair
    assert off["C0"] == [-x for x in off["CDUM"]] and abs(off["C0"][0]) < 4.17
    pair = placement.common_centroid({"M1": 4, "M2": 4}, 4, 2)
    assert placement.centroid_error(pair, (1, 1)) == {"M1": [0.0, 0.0], "M2": [0.0, 0.0]}
    assert placement.interdigitated({"M1": 4, "M2": 4}, 4, 2) == [["M1", "M2", "M1", "M2"], ["M2", "M1", "M2", "M1"]]
    assert placement.interdigitated({"A": 2, "B": 4}, 6, 1) == [["B", "A", "B", "B", "A", "B"]]
    with pytest.raises(ValueError, match="do not fit"):
        placement.common_centroid({"A": 5, "B": 5}, 3, 3)
    assert placement.factor(512) == (32, 16) and placement.factor(7) == (7, 1) and placement.factor(2) == (2, 1)


def test_rule_gap_between_units():
    """sky130 MIM units 1.34 um from a capm to the next unit's metal3 (Magic capm.11); wells of one net may merge."""
    r = pdkprofile.SKY130.space
    unit = (kdb.Box(-200, -200, 2830, 1370), {"capm": kdb.Box(0, 0, 1170, 1170), "met3": kdb.Box(-200, -200, 2830, 1370),
                                              "met4": kdb.Box(195, -140, 2810, 1310)})
    assert unit[0].width() + placement.gap(unit, unit, r, 0, 0.001) == 4170
    assert unit[0].height() + placement.gap(unit, unit, r, 1, 0.001) == 2710
    well = (kdb.Box(0, 0, 1000, 1000), {"nwell": kdb.Box(0, 0, 1000, 1000), "diff": kdb.Box(300, 300, 700, 700)})
    assert placement.gap(well, well, r, 0, 0.001) == 1270                       # two wells: 1.27 apart
    assert placement.gap(well, well, r, 0, 0.001, merge=True) == -300           # one bulk: overlapping, diff 0.3 apart
    assert placement.gap(well, well, r, 0, 0.001, skip=("nwell",)) == -300      # filled over both


def test_layout_parameters_are_checked():
    assert sdl.layout_params("XMS", "mos", {"nf": 4, "bulk": "Ring", "tap_side": None}) == \
        {"nf": 4, "bulk": "ring", "tap_side": None}
    for bad, msg in (({"fingers": 2}, "takes no layout parameter fingers: it takes nf"), ({"nf": 0}, "whole number"),
                     ({"bulk": "tied"}, "bulk is auto, tie, ring, none"), ({"nf": True}, "whole number")):
        with pytest.raises(ValueError, match=msg):
            sdl.layout_params("XMS", "mos", bad)
    with pytest.raises(ValueError, match="it takes columns"):
        sdl.layout_params("C9", "units", {"nf": 2})
    with pytest.raises(ValueError, match="no PCell device"):
        sdl.layout_params("X1", None, {"nf": 2})


CAT = {"nfet": {"params": [{"name": "type", "choices": ["sky130_fd_pr__nfet_01v8", "sky130_fd_pr__nfet_g5v0d10v5"]},
                           {"name": "w"}, {"name": "l"}, {"name": "nf"}, {"name": "sd_con_col"},
                           {"name": "bulk", "choice_values": ["None", "bulk tie", "guard ring"]},
                           {"name": "gate_con_pos", "choice_values": ["top", "bottom", "alternating"]}]}}


@pytest.mark.parametrize("model, nets, layout, want", [
    ("nfet_01v8", {"D": "out", "S": "vss", "B": "vss"}, {}, ({"bulk": "bulk tie"}, {"kind": "tie", "terminal": "S"})),
    ("nfet_01v8", {"D": "vss", "S": "x", "B": "vss"}, {}, ({"bulk": "bulk tie"}, {"kind": "tie", "terminal": "D"})),
    ("nfet_01v8", {"D": "vout", "S": "vin", "B": "vss"}, {}, ({"bulk": "guard ring"}, {"kind": "ring"})),
    ("nfet_g5v0d10v5", {"D": "g", "S": "vss", "B": "vss"}, {}, ({"bulk": "bulk tie", "sd_con_col": 2}, {"kind": "tie"})),
    ("nfet_01v8", {"D": "out", "S": "vss", "B": "vss"}, {"bulk": "ring"}, ({"bulk": "guard ring"}, {"kind": "ring"})),
    ("nfet_01v8", {"D": "out", "S": "vss", "B": "vss"}, {"bulk": "none"}, ({"bulk": "None"}, {"kind": "none"})),
    ("nfet_01v8", {"D": "out", "S": "vss", "B": "vss"}, {"tap_side": "left", "nf": 4, "gate": "top"},
     ({"bulk": "bulk tie", "w": 1.0, "nf": 4, "gate_con_pos": "top"}, {"kind": "tie", "side": "left"})),
])
def test_a_tie_only_where_a_source_or_drain_is_on_the_bulk_net(model, nets, layout, want):
    item = {"name": "M1", "pcell": "nfet", "nets": {**nets, "G": "g"}, "layout": layout,
            "params": {"type": f"sky130_fd_pr__{model}", "w": 4.0, "l": 0.15, "bulk": "bulk tie"}}
    sdl.Sdl._mos_layout(None, item, {}, CAT)
    assert {k: item["params"].get(k) for k in want[0]} == want[0]
    assert {k: item["tap"].get(k) for k in want[1]} == want[1]


def test_a_tie_on_a_signal_net_and_off_grid_fingers_are_refused():
    item = {"name": "XMS", "pcell": "nfet", "nets": {"D": "vout", "S": "vin", "G": "g", "B": "vss"}, "layout": {"bulk": "tie"},
            "params": {"type": "sky130_fd_pr__nfet_01v8", "w": 20.0, "l": 0.15}}
    with pytest.raises(ValueError, match=r"neither S \(vin\) nor D \(vout\) is on the bulk net vss"):
        sdl.Sdl._mos_layout(None, {**item, "params": dict(item["params"], bulk="bulk tie")}, {}, CAT)
    with pytest.raises(ValueError, match="off the 5 nm grid"):
        sdl.Sdl._mos_layout(None, {**item, "layout": {"nf": 3}, "params": dict(item["params"])}, {}, CAT)


def test_an_agent_restores_its_own_changeset_while_it_holds_it():
    """restore_changeset over MCP: the agent writing the changeset holds its lease, and that is no reason to refuse;
    another session's restore is refused while the lease is held."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_changeset as tc

    root, srv, _cs = tc.setup()
    ctx = T.ToolCtx(srv, "p", actor="agent:t", owner="mcp:s1", owner_kind="mcp")
    assert not T.call(ctx, "open_changeset", {"title": "t", "reason": "r"})[1]
    db = srv.db(ctx.work, "l")
    db.layout.layer(68, 20)
    db.lv.add_missing_layers()
    _call(ctx, "add_box", lib="l", cell="top", layer="68/20", box=[0, 0, 1, 1])
    mark = _call(ctx, "get_changeset")["restore_points"][0]["after"]
    _call(ctx, "add_box", lib="l", cell="top", layer="68/20", box=[2, 0, 3, 1])
    other = T.ToolCtx(srv, "p", actor="agent:u", owner="mcp:s2", owner_kind="mcp", changeset=ctx.changeset)
    res, err = T.call(other, "restore_changeset", {"to": mark})
    assert err and "is editing this changeset" in res
    out = _call(ctx, "restore_changeset", to=mark)
    assert out["restored_to"] == mark and out["restore_points"][0]["what"] == "restore"
    assert srv.db(ctx.work, "l").cell("top").shapes(srv.db(ctx.work, "l").layout.layer(68, 20)).size() == 1
    shutil.rmtree(root)


def _plain_changeset():
    """A project without a PDK (test_changeset's), a changeset open in it, a layout cell u (a 2 x 1 um box) to place."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_changeset as tc

    root, srv, _cs = tc.setup()
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    assert not T.call(ctx, "open_changeset", {"title": "t", "reason": "r"})[1]
    _call(ctx, "create_cell", lib="l", cell="u", views=["layout"])
    db = srv.db(ctx.work, "l")
    db.layout.layer(68, 20)
    db.lv.add_missing_layers()
    _call(ctx, "add_box", lib="l", cell="u", layer="68/20", box=[0, 0, 2, 1])
    return root, srv, ctx


def _trans(srv, ctx, cell="top"):
    return [(i["id"], i["trans"], i.get("array")) for i in srv.db(ctx.work, "l").instances(cell)]


def test_move_instance_keeps_the_orientation_it_is_not_told_and_moves_after_turning():
    """rot alone keeps the mirror, mirror alone the rotation (Trans.angle is in 90 degree steps), x / y keep both; rot
    with dx turns in place and then moves; an array's steps turn with it."""
    root, srv, ctx = _plain_changeset()
    _call(ctx, "place_cell", lib="l", cell="top", child="u", x=10, y=10)
    i = lambda: _trans(srv, ctx)[0]
    _call(ctx, "move_instance", lib="l", cell="top", instance="u@10,10", rot=90)
    assert i()[1].startswith("r90 ")
    _call(ctx, "move_instance", lib="l", cell="top", instance=i()[0], mirror=True)
    assert i()[1].startswith("m45 ")                                      # mirrored, still turned by 90
    _call(ctx, "move_instance", lib="l", cell="top", instance=i()[0], x=5, y=5)
    assert i()[1] == "m45 5000,5000"
    before = _call(ctx, "get_layout_info", lib="l", cell="top")["unbound"][0]["bbox_um"]
    after = _call(ctx, "move_instance", lib="l", cell="top", instance=i()[0], rot=180, dx=3, dy=-1)["instances"][0]["bbox_um"]
    assert i()[1].startswith("m90 ")
    centre = lambda b: (round((b[0] + b[2]) * 500), round((b[1] + b[3]) * 500))
    assert before == [5, 5, 6, 7] and after[2] - after[0] == 2                          # 1 x 2 turned to 2 x 1
    assert (centre(after)[0] - centre(before)[0], centre(after)[1] - centre(before)[1]) == (3000, -1000)
    _call(ctx, "place_cell", lib="l", cell="top", child="u", x=0, y=20, columns=2, rows=1, pitch_x=3, pitch_y=2)
    arr = next(x for x in _trans(srv, ctx) if x[2])
    _call(ctx, "move_instance", lib="l", cell="top", instance=arr[0], rot=90)
    assert next(x for x in _trans(srv, ctx) if x[2])[2]["a"] == [0, 3000]      # the row turned into a column
    shutil.rmtree(root)


def test_instance_ids_are_exact_and_found_in_any_decimals():
    root, srv, ctx = _plain_changeset()
    for x, y in ((104.45, 67.95), (1234.565, 0), (-0.2, 3)):
        _call(ctx, "place_cell", lib="l", cell="top", child="u", x=x, y=y)
    ids = [t[0] for t in _trans(srv, ctx)]
    assert sorted(ids) == ["u@-0.2,3", "u@104.45,67.95", "u@1234.565,0"]
    out = _call(ctx, "delete_instances", lib="l", cell="top", instances=["u@104.450,67.9500", "u@1234.565,0.0"])
    assert out["deleted"] == 2 and [t[0] for t in _trans(srv, ctx)] == ["u@-0.2,3"]
    shutil.rmtree(root)


def test_a_one_row_array_moved_after_a_save_is_one_array_after_a_restart():
    """The cell file holds a one-row array as its reader makes it (the unused step zeroed, the two maybe swapped): the
    journal's move of the array as it was placed still finds it, so a restart does not bring back a second array."""
    root, srv, ctx = _plain_changeset()
    _call(ctx, "place_cell", lib="l", cell="top", child="u", x=0, y=0, columns=2, rows=1, pitch_x=3, pitch_y=2)
    _call(ctx, "place_cell", lib="l", cell="top", child="u", x=0, y=10, columns=1, rows=3, pitch_x=3, pitch_y=2)
    srv.db(ctx.work, "l").save()
    for iid in [t[0] for t in _trans(srv, ctx)]:
        _call(ctx, "move_instance", lib="l", cell="top", instance=iid, rot=90)
    _call(ctx, "move_instance", lib="l", cell="top", instance=_trans(srv, ctx)[0][0], dx=1)
    want = sorted(_trans(srv, ctx))
    again = Server(root, {}, [root])                                     # a restart: the files, then the journal
    got = sorted((i["id"], i["trans"]) for i in again.db(ctx.work, "l").instances("top"))
    assert got == [(w[0], w[1]) for w in want] and len(got) == 2
    shutil.rmtree(root)


def test_mim_variants_above_the_old_clamp_keep_their_names():
    """Only a MIM side below the generator's old 2 um clamp is drawn differently since the clamp was lowered: those
    variants are of a new generation, the others keep the names layouts already place (the pilot's 12 x 12 um)."""
    big = {"type": "sky130_fd_pr__model__cap_mim", "w": 12.0, "l": 12.0}
    assert pcell.cell_name("mim_cap", big) == "sky130_fd_pr__model__cap_mim_w12p0_l12p0_6577f9"
    assert pcell.gen_version("mim_cap", {**big, "w": 1.17}) == 4 and pcell.gen_version("mim_cap", big) == pcell.GEN_VERSION
    assert pcell.gen_version("nfet", {"w": 1.0}) == pcell.GEN_VERSION


# ---------------------------------------------------------------------------------- with sky130A and its tools


def _wait(job, limit=900):
    t0 = time.time()
    while job.status()["state"] in ("queued", "running") and time.time() - t0 < limit:
        time.sleep(0.5)
    return job.status()


@pytest.fixture(scope="module")
def env():
    root = Path(tempfile.mkdtemp())
    srv = Server(root, PDKS, [root])
    srv.store.create("p", SKY.id).create_library("l")
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    assert not T.call(ctx, "open_changeset", {"title": "sdl", "reason": "r"})[1]
    yield srv, ctx
    shutil.rmtree(root, ignore_errors=True)


def _call(ctx, tool, **a):
    res, err = T.call(ctx, tool, {"reason": "x", **a}, limit=None)
    assert not err, res
    return json.loads(res)


def _drc(srv, ctx, cell, lib="l"):
    """(KLayout mr deck, Magic drc(full)) rule counts of a cell."""
    out = []
    for run in (srv.prepare_drc(ctx.work, lib, cell), srv.prepare_magic_drc(ctx.work, lib, cell)):
        st = _wait(run.launch())
        assert st["state"] == "done", st
        out.append({c["name"]: c["count"] for c in st["summary"]["categories"]})
    return out


def _schematic(ctx, cell, devices, nets):
    _call(ctx, "create_cell", lib="l", cell=cell, views=["schematic"])
    _call(ctx, "add_instances", lib="l", cell=cell, instances=[
        {"symbol": f"sky130_fd_pr/{sym}.sym", "x": 200 * i, "y": 0, "params": {"name": n, **p}}
        for i, (n, sym, p) in enumerate(devices)])
    _call(ctx, "connect_nets", lib="l", cell=cell, nets=nets)


@real
def test_sync_makes_the_layout_view_units_and_taps_drc_clean(env):
    srv, ctx = env
    _schematic(ctx, "bsw", [("XMS", "nfet_01v8", {"W": "20", "L": "0.15"}),
                            ("XM5", "nfet_g5v0d10v5", {"W": "24", "L": "0.5"}),
                            ("XMH", "nfet_g5v0d10v5", {"W": "4", "L": "0.5"}),
                            ("XMIN", "nfet_01v8", {"W": "1", "L": "0.15"}),
                            ("XMIP", "pfet_01v8", {"W": "2", "L": "0.15"}),
                            ("XCB", "cap_mim_m3_1", {"W": "12", "L": "12", "MF": "2"})],
               {"vin": ["XMS:S"], "vout": ["XMS:D"], "g": ["XMS:G", "XM5:D"], "dis": ["XM5:S"], "h": ["XMH:D"],
                "vdd": ["XM5:G", "XMH:G", "XMIP:S", "XMIP:B"], "vss": ["XMS:B", "XM5:B", "XMH:S", "XMH:B", "XMIN:S", "XMIN:B"],
                "clk": ["XMIN:G", "XMIP:G"], "clkb": ["XMIN:D", "XMIP:D"], "bt": ["XCB:c0"], "bb": ["XCB:c1"]})
    res, err = T.call(ctx, "get_layout_info", {"lib": "l", "cell": "bsw"})
    assert err and "bsw has no layout view yet: sync_layout creates one" in res
    out = _call(ctx, "sync_layout", lib="l", cell="bsw")
    assert out["created"] == "the layout view of l/bsw" and out["errors"] == [] and len(out["added"]) == 6
    tie = "bulk tie on the right: the right-hand source / drain is joined to the tap, route S there"
    assert sorted(out["taps"][tie]) == ["XMH", "XMIN", "XMIP"]
    assert sorted(out["taps"]["guard ring: route it (li1) to the bulk net"]) == ["XM5", "XMS"]
    info = _call(ctx, "get_layout_info", lib="l", cell="bsw")
    inst = {i["name"]: i for i in info["instances"]}
    assert inst["XCB"]["array"] == {"columns": 2, "rows": 1, "pitch_um": [15.0, 13.54]}   # 12 x 12 units, capm.11 apart
    assert inst["XMIN"]["cell"].endswith(sdl.TIE) and "_par" not in inst["XMIN"]["cell"]
    st = info["schematic_sync"]
    assert st["out_of_date"] == [] and st["missing"] == []
    wiring = st["wiring"]
    assert "butted_taps" not in wiring and wiring["shorts"] == []
    assert st["unrouted_nets"] == ["bb", "bt", "clk", "clkb", "g", "vdd", "vss"]    # bt / bb: the two units
    assert wiring["substrate_only"] == ["vss"]                  # the taps of four devices: nothing drawn joins them
    klayout, magic = _drc(srv, ctx, "bsw")
    assert klayout == {} and magic == {}, (klayout, magic)          # diff/tap.16: XMH's MV tie is two contacts wide
    hv = srv.db(ctx.work, "l").cell(inst["XMH"]["cell"])
    tap = kdb.Region(hv.begin_shapes_rec(srv.db(ctx.work, "l").layout.layer(65, 44))).bbox()
    assert tap.width() >= 700


@real
def test_layout_fingers_are_kept_and_lvs_matches_the_schematic_width(env):
    """The pilot's XMS: W 20 / L 0.15, one finger in the schematic, drawn as 4 fingers of 5 um: KLayout combines them
    into the schematic's W 20; the layout parameter outlasts a later sync and a schematic change."""
    srv, ctx = env
    _schematic(ctx, "xms", [("XMS", "nfet_01v8", {"W": "20", "L": "0.15"})],
               {"vin": ["XMS:S"], "vout": ["XMS:D"], "g": ["XMS:G"], "vss": ["XMS:B"]})
    out = _call(ctx, "sync_layout", lib="l", cell="xms", layout={"XMS": {"nf": 4}})
    assert out["added"] == ["XMS"]
    info = _call(ctx, "get_layout_info", lib="l", cell="xms")
    xms = info["instances"][0]
    assert "_w5p0_l0p15_nf4_" in xms["cell"] and xms["cell"].endswith(sdl.STRAP) and xms["layout"] == {"nf": 4}
    assert _call(ctx, "sync_layout", lib="l", cell="xms")["unchanged"] == ["XMS"]          # kept with the layout
    st = _wait(srv.prepare_lvs(ctx.work, "l", "xms").launch())
    assert st["state"] == "done" and st["summary"]["match"] is True, st
    nl = next(Path(srv.project(ctx.work).path, "runs", st["id"]).glob("extracted.cir")).read_text()
    assert "W=20000000" in nl and nl.count("sky130_fd_pr__nfet_01v8") == 1                 # one device of the total width
    assert _drc(srv, ctx, "xms") == [{}, {}]
    _call(ctx, "set_instance_params", lib="l", cell="xms", instance="XMS", params={"W": "16"})
    upd = _call(ctx, "sync_layout", lib="l", cell="xms")["updated"][0]
    assert "_w4p0_l0p15_nf4_" in upd["to"]                                                  # still 4 fingers, of 4 um
    out = _call(ctx, "sync_layout", lib="l", cell="xms", layout={"XMS": {"nf": None, "bulk": "none"}})
    assert "_w16p0_" in out["updated"][0]["to"] and out["taps"] == {
        "no tap: draw one on the bulk net within the latch-up distance (LU.2)": ["XMS"]}
    res, err = T.call(ctx, "sync_layout", {"lib": "l", "cell": "xms", "reason": "x", "layout": {"XMS": {"bulk": "tie"}}})
    assert err and "neither S (vin) nor D (vout) is on the bulk net vss" in res


@real
def test_place_pcell_move_rotate_delete_and_restore(env):
    srv, ctx = env
    _schematic(ctx, "ed", [("M1", "nfet_01v8", {"W": "2", "L": "0.15"})], {"a": ["M1:D"], "b": ["M1:S"], "g": ["M1:G"], "vss": ["M1:B"]})
    _call(ctx, "sync_layout", lib="l", cell="ed")
    res, err = T.call(ctx, "place_pcell", {"lib": "l", "cell": "ed", "pcell": "nfett", "params": {}, "x": 0, "y": 0, "reason": "x"})
    assert err and "no PCell 'nfett'" in res and "nfet, " in res
    res, err = T.call(ctx, "place_pcell", {"lib": "l", "cell": "ed", "pcell": "nfet", "params": {"width": 1}, "x": 0, "y": 0, "reason": "x"})
    assert err and "takes no width: it takes" in res and "bulk (default 'None', one of None, bulk tie, guard ring)" in res
    dummy = _call(ctx, "place_pcell", lib="l", cell="ed", pcell="nfet", x=20, y=0,
                  params={"type": "sky130_fd_pr__nfet_01v8", "w": 2, "l": 0.15, "bulk": "guard ring"})
    assert dummy["id"].endswith("@20,0") and "not bound" in dummy["note"]
    piece = _call(ctx, "place_pcell", lib="l", cell="ed", pcell="nfet", x=40, y=0, name="M1",
                  params={"type": "sky130_fd_pr__nfet_01v8", "w": 1, "l": 0.15})
    assert piece["name"] == "M1"
    info = _call(ctx, "get_layout_info", lib="l", cell="ed")
    assert [u["id"] for u in info["unbound"]] == [dummy["id"]]
    assert [o["name"] for o in info["schematic_sync"]["own_layout"]] == ["M1"]                # in two pieces: yours
    moved = _call(ctx, "move_instance", lib="l", cell="ed", instance=dummy["id"], x=30, y=5)["instances"][0]
    assert moved["id"].endswith("@30,5")
    b = moved["bbox_um"]
    turned = _call(ctx, "move_instance", lib="l", cell="ed", instance=moved["id"], rot=180, mirror=True)["instances"][0]
    tb = turned["bbox_um"]
    assert abs((tb[0] + tb[2]) - (b[0] + b[2])) < 0.02 and abs((tb[1] + tb[3]) - (b[1] + b[3])) < 0.02  # in place
    db = srv.db(ctx.work, "l")
    assert next(i for i in db.instances("ed") if i["id"] == turned["id"])["trans"].startswith("m90")
    mark = _call(ctx, "get_changeset")["restore_points"][0]["after"]
    gone = _call(ctx, "delete_instances", lib="l", cell="ed", instances=["M1"])
    assert gone["deleted"] == 2
    assert _call(ctx, "get_layout_info", lib="l", cell="ed")["schematic_sync"]["missing"] == ["M1"]
    assert _call(ctx, "delete_instances", lib="l", cell="ed", child="nope")["deleted"] == 0
    _call(ctx, "restore_changeset", to=mark)
    assert sorted(i.get("name", "") for i in srv.db(ctx.work, "l").instances("ed")) == ["", "M1", "M1"]
    res, err = T.call(ctx, "move_instance", {"lib": "l", "cell": "ed", "instance": "M1", "x": 0, "y": 0, "reason": "x"})
    assert err and "is 2 instances: move one at a time by its id" in res
    assert _call(ctx, "delete_instances", lib="l", cell="ed", box=[25, -10, 50, 20])["deleted"] == 2


def _route_mim_array(db, cell, out, devices):
    """Joins a place_array of 2 x 2 um MIM units: every top plate to `top` on met5 (via4 on the top plate, a bar per row
    and one down the left), each device's bottom plates to its own met2 bus below the array (via2 on the plate's
    metal3 right of the capm, a met1 drop on a track of its own, via1 at both ends), labelled."""
    L = lambda l, d: db.layout.layer(l, d)
    m1, v1, m2, v2, v4, m5 = L(68, 20), L(68, 44), L(69, 20), L(69, 44), L(71, 44), L(72, 20)
    um = lambda v: int(round(v * 1000))
    ox, oy = out["origin_um"]
    px, py = out["pitch_um"]
    rows = out["map"][::-1]                                       # bottom row first
    legend = out["legend"]
    ops, buses = [], {n: oy - 2.0 - 1.2 * i for i, n in enumerate(devices)}
    for r, row in enumerate(rows):
        y0 = oy + r * py + 0.2                                    # the capm's lower-left (the cell's bbox is 0.2 out)
        ops.append((m5, kdb.Box(um(ox - 3), um(y0 + 0.2), um(ox + len(row) * px), um(y0 + 1.8))))
        for c, ch in enumerate(row):
            x0 = ox + c * px + 0.2
            ops.append((v4, kdb.Box(um(x0 + 0.6), um(y0 + 0.6), um(x0 + 1.4), um(y0 + 1.4))))
            vx, vy, by = x0 + 2.3 + 0.6 * r, y0 + 1.0, buses[legend[ch]]
            for lay, h in ((v2, 0.1), (v1, 0.075), (m2, 0.2), (m1, 0.2)):
                ops.append((lay, kdb.Box(um(vx - h), um(vy - h), um(vx + h), um(vy + h))))
            ops += [(m1, kdb.Box(um(vx - 0.2), um(by - 0.2), um(vx + 0.2), um(vy))), (v1, kdb.Box(um(vx - 0.075), um(by - 0.075), um(vx + 0.075), um(by + 0.075)))]
    ops.append((m5, kdb.Box(um(ox - 3), um(oy + 0.2), um(ox - 1.4), um(oy + (len(rows) - 1) * py + 2.2))))
    right = ox + len(rows[0]) * px
    ops += [(m2, kdb.Box(um(ox - 1), um(y - 0.2), um(right), um(y + 0.2))) for y in buses.values()]
    ops += [(L(72, 5), kdb.Text("top", kdb.Trans(um(ox - 2.2), um(oy + 1))))]
    ops += [(L(69, 5), kdb.Text(n.lower().replace("c", "b"), kdb.Trans(um(ox - 0.5), um(y)))) for n, y in buses.items()]
    db._commit(cell, [("ins", li, o) for li, o in ops])


@real
def test_a_binary_mim_array_in_common_centroid_is_drc_clean_and_matches_lvs(env):
    """C0 .. C3 with 1, 1, 2, 4 units of a 2 x 2 um MIM in one 4 x 2 grid, common centroid: both decks clean at the rule
    pitch; routed, KLayout LVS combines each device's units into its capacitance (a reference with the MIM written as the
    deck's element, the sky130 profile's own reference not being one yet), and a unit too few is a mismatch."""
    srv, ctx = env
    devs = [(f"C{i}", "cap_mim_m3_1", {"W": "2", "L": "2", "MF": str(n)}) for i, n in enumerate((1, 1, 2, 4))]
    _schematic(ctx, "cdac", devs, {"top": [f"C{i}:c0" for i in range(4)], **{f"b{i}": [f"C{i}:c1"] for i in range(4)}})
    out = _call(ctx, "place_array", lib="l", cell="cdac", devices=["C0", "C1", "C2", "C3"], pattern="common_centroid", origin=[0, 0])
    assert (out["columns"], out["rows"]) == (4, 2) and out["pitch_um"] == out["rule_pitch_um"] == [5.0, 3.54]
    assert sorted("".join(out["map"])) == sorted("ABCCDDDD")
    assert out["devices"]["C3"]["centroid_offset_um"] == out["devices"]["C2"]["centroid_offset_um"] == [0.0, 0.0]
    res, err = T.call(ctx, "place_array", {"lib": "l", "cell": "cdac", "devices": ["C3"], "origin": [0, 20], "reason": "x"})
    assert err and "C3 is placed already" in res
    st = _call(ctx, "get_layout_info", lib="l", cell="cdac")["schematic_sync"]
    assert st["out_of_date"] == [] and "own_layout" not in st                    # every device has its units: in sync
    assert _drc(srv, ctx, "cdac") == [{}, {}]
    db = srv.db(ctx.work, "l")
    _route_mim_array(db, "cdac", out, ["C0", "C1", "C2", "C3"])
    wiring = _call(ctx, "get_layout_info", lib="l", cell="cdac")["schematic_sync"]["wiring"]
    assert wiring["unrouted"] == [] and wiring["shorts"] == [] and wiring["routed"] == 5
    ref = Path(srv.project(ctx.work).path) / "cdac.spice"
    for m, match in (((1, 1, 2, 4), True), ((1, 1, 2, 3), False)):
        ref.write_text(".subckt cdac top b0 b1 b2 b3\n" + "".join(
            f"CC{i} top b{i} sky130_fd_pr__model__cap_mim A=4 P=8 m={n}\n" for i, n in enumerate(m)) + ".ends\n")
        st = _wait(srv.prepare_lvs(ctx.work, "l", "cdac", schematic_file=str(ref)).launch())
        assert st["state"] == "done" and st["summary"]["match"] is match, st


@real
def test_mosfet_units_in_arrays_get_one_well_and_a_guard_ring(env):
    srv, ctx = env
    _schematic(ctx, "pair", [("M1", "nfet_01v8_lvt", {"W": "10", "L": "0.2"}), ("M2", "nfet_01v8_lvt", {"W": "10", "L": "0.2"}),
                             ("M8", "pfet_01v8", {"W": "2", "L": "0.3"}), ("M9", "pfet_01v8", {"W": "2", "L": "0.3"})],
               {"fp": ["M1:D"], "fn": ["M2:D"], "tail": ["M1:S", "M2:S"], "inp": ["M1:G"], "inn": ["M2:G"], "vss": ["M1:B", "M2:B"],
                "sp": ["M8:D", "M9:G"], "sn": ["M9:D", "M8:G"], "vdd": ["M8:S", "M9:S", "M8:B", "M9:B"]})
    out = _call(ctx, "place_array", lib="l", cell="pair", devices={"M1": 4, "M2": 4}, pattern="common_centroid",
                origin=[0, 0], dummies=1)
    assert out["map"][1:3] == [".BBAA.", ".AABB."] and out["dummies"] == 16 and out["guard_ring"]
    assert all(d["cell"].startswith("sky130_fd_pr__nfet_01v8_lvt_w2p5_l0p2_") for d in out["devices"].values())
    out = _call(ctx, "place_array", lib="l", cell="pair", devices={"M8": 2, "M9": 2}, pattern="interdigitated", origin=[0, 20])
    assert out["map"] == ["ABAB"]
    res, err = T.call(ctx, "place_array", {"lib": "l", "cell": "pair", "devices": {"M1": 3}, "origin": [0, 40], "reason": "x"})
    assert err and "off the 5 nm grid" in res
    assert _drc(srv, ctx, "pair") == [{}, {}]
    st = _call(ctx, "get_layout_info", lib="l", cell="pair")["schematic_sync"]
    assert {o["name"] for o in st["own_layout"]} == {"M1", "M2", "M8", "M9"}              # pieces: yours to keep


@real
def test_a_device_sync_puts_in_place_keeps_its_orientation_and_an_array_its_turned_steps(env):
    """XMS mirrored left to right and the MIM pair turned upright by hand; the schematic changes W and MF: sync puts
    the new cells in with the same orientation, the array's steps still turned."""
    srv, ctx = env
    _schematic(ctx, "ori", [("XMS", "nfet_01v8", {"W": "20", "L": "0.15"}),
                            ("XCB", "cap_mim_m3_1", {"W": "12", "L": "12", "MF": "2"})],
               {"vin": ["XMS:S"], "vout": ["XMS:D"], "g": ["XMS:G"], "vss": ["XMS:B"], "bt": ["XCB:c0"], "bb": ["XCB:c1"]})
    _call(ctx, "sync_layout", lib="l", cell="ori", layout={"XMS": {"nf": 4}})
    _call(ctx, "move_instance", lib="l", cell="ori", instance="XMS", rot=180, mirror=True)
    _call(ctx, "move_instance", lib="l", cell="ori", instance="XCB", rot=90)
    db = srv.db(ctx.work, "l")
    was = {i["name"]: i for i in db.instances("ori")}
    assert was["XMS"]["trans"].startswith("m90 ") and was["XCB"]["array"]["a"] == [0, 15000]
    _call(ctx, "set_instance_params", lib="l", cell="ori", instance="XMS", params={"W": "16"})
    _call(ctx, "set_instance_params", lib="l", cell="ori", instance="XCB", params={"MF": "4"})
    out = _call(ctx, "sync_layout", lib="l", cell="ori")
    assert sorted(u["name"] for u in out["updated"]) == ["XCB", "XMS"]
    now = {i["name"]: i for i in db.instances("ori")}
    assert now["XMS"]["trans"] == was["XMS"]["trans"] and "_w4p0_l0p15_nf4_" in now["XMS"]["cell"]
    assert now["XCB"]["trans"] == was["XCB"]["trans"]
    assert now["XCB"]["array"] == {"columns": 2, "rows": 2, "a": [0, 15000], "b": [-13540, 0]}     # a column, turned


@real
def test_small_mim_units_are_drawn_as_asked_with_their_dummies_and_ids(env, monkeypatch):
    """A CDAC's 1.17 um unit (the generator's own clamp would draw 2 um; below capm.1's 1 um it is refused), the dummy
    ring bound to a device of its own with as many units (MF 1 refused, CDUM-like units of the array refused), a
    unit's id from the map, the unit generated once: both decks clean."""
    srv, ctx = env
    devs = [(f"C{i}", "cap_mim_m3_1", {"W": "1.17", "L": "1.17", "MF": str(n)}) for i, n in enumerate((1, 1, 2, 4))]
    devs.append(("CD", "cap_mim_m3_1", {"W": "1.17", "L": "1.17", "MF": "1"}))
    _schematic(ctx, "cdac4", devs, {"top": [f"C{i}:c0" for i in range(4)] + ["CD:c0"], "bd": ["CD:c1"],
                                    **{f"b{i}": [f"C{i}:c1"] for i in range(4)}})
    args = {"lib": "l", "cell": "cdac4", "devices": ["C0", "C1", "C2", "C3"], "origin": [0, 0], "dummies": 1, "dummy": "CD"}
    res, err = T.call(ctx, "place_array", {**args, "reason": "x"})
    assert err and "dummy CD has 1 unit (MF x m) and the array 16 dummy units" in res and "Set its MF to 16" in res
    res, err = T.call(ctx, "place_array", {**args, "devices": ["C0", "C1", "C2", "C3", "CD"], "reason": "x"})
    assert err and "CD is in the array already" in res
    db = srv.db(ctx.work, "l")
    assert not [c.name for c in db.layout.each_cell() if "_w1p17_" in c.name]          # refused before generating
    _call(ctx, "set_instance_params", lib="l", cell="cdac4", instance="CD", params={"MF": "16"})
    out = _call(ctx, "place_array", **args)
    unit = out["devices"]["C3"]["cell"]
    capm = db.cell(unit).bbox_per_layer(db.layout.layer(89, 44))
    assert (capm.width(), capm.height()) == (1170, 1170) and out["pitch_um"] == [4.17, 2.71] and out["dummies"] == 16
    ch = next(k for k, v in out["legend"].items() if v == "C3")
    rows = out["map"][::-1]
    r, c = next((r, row.index(ch)) for r, row in enumerate(rows) if ch in row)
    (ox, oy), (px, py), (ux, uy) = out["origin_um"], out["pitch_um"], out["unit_origin_um"][unit]
    iid = f"{unit}@{ox + c * px + ux:.4f},{oy + r * py + uy:.4f}"
    assert _call(ctx, "move_instance", lib="l", cell="cdac4", instance=iid, dx=0, dy=0)["instances"][0]["name"] == "C3"
    st = _call(ctx, "get_layout_info", lib="l", cell="cdac4")["schematic_sync"]
    assert st["out_of_date"] == [] and "own_layout" not in st
    assert _call(ctx, "sync_layout", lib="l", cell="cdac4")["updated"] == []
    assert _drc(srv, ctx, "cdac4") == [{}, {}]
    monkeypatch.setattr(pcell, "generate", lambda *a: pytest.fail("the unit is in the library already"))
    assert _call(ctx, "place_array", **args, replace=True)["map"] == out["map"]
    monkeypatch.undo()
    res, err = T.call(ctx, "place_pcell", {"lib": "l", "cell": "cdac4", "pcell": "mim_cap", "x": 0, "y": -10, "reason": "x",
                                           "params": {"type": "sky130_fd_pr__model__cap_mim", "w": 0.8, "l": 0.8}})
    assert err and "draws 1 x 1 um for 0.8 x 0.8 um" in res


@real
def test_thick_oxide_units_have_their_ring_and_well_inside_the_mark(env):
    """nfet / pfet g5v0d10v5 units in arrays: the guard ring (and a pfet ring's n-well) inside hvi, an HV n-well wholly
    (KLayout nwell.9, Magic diff/tap.21)."""
    srv, ctx = env
    _schematic(ctx, "hv", [(n, f"{k}fet_g5v0d10v5", {"W": "4", "L": "0.5"})
                           for n, k in (("MA", "n"), ("MB", "n"), ("PA", "p"), ("PB", "p"))],
               {"a": ["MA:D"], "b": ["MB:D"], "t": ["MA:S", "MB:S"], "ga": ["MA:G"], "gb": ["MB:G"], "vss": ["MA:B", "MB:B"],
                "pa": ["PA:D"], "pb": ["PB:D"], "vdd": ["PA:S", "PB:S", "PA:B", "PB:B"], "gp": ["PA:G", "PB:G"]})
    assert _call(ctx, "place_array", lib="l", cell="hv", devices={"MA": 2, "MB": 2}, origin=[0, 0])["guard_ring"]
    assert _call(ctx, "place_array", lib="l", cell="hv", devices={"PA": 2, "PB": 2}, pattern="interdigitated",
                 origin=[0, 20])["map"] == ["ABAB"]
    assert _drc(srv, ctx, "hv") == [{}, {}]


@real
def test_wiring_finds_butted_taps_shorts_and_nets_only_the_substrate_joins(env):
    """M2 / M3 have their source on vss (ties), M1 neither (a guard ring): vss is joined only through the substrate.
    M1 drawn by hand as one tied piece: its tap butts against a signal net, and sync leaves the piece alone. An li1 bar
    across M3 shorts its drain to vss."""
    srv, ctx = env
    nfet = {"W": "1", "L": "0.15"}
    _schematic(ctx, "wr", [("M1", "nfet_01v8", nfet), ("M2", "nfet_01v8", nfet), ("M3", "nfet_01v8", nfet)],
               {"a": ["M1:D"], "b": ["M1:S"], "g": ["M1:G", "M2:G", "M3:G"], "c": ["M2:D"], "x": ["M3:D"],
                "vss": ["M1:B", "M2:S", "M2:B", "M3:S", "M3:B"]})
    _call(ctx, "sync_layout", lib="l", cell="wr")
    w = _call(ctx, "get_layout_info", lib="l", cell="wr")["schematic_sync"]["wiring"]
    assert w["substrate_only"] == ["vss"] and "butted_taps" not in w and w["shorts"] == []
    _call(ctx, "delete_instances", lib="l", cell="wr", instances=["M1"])
    _call(ctx, "place_pcell", lib="l", cell="wr", pcell="nfet", x=-20, y=0, name="M1",
          params={"type": "sky130_fd_pr__nfet_01v8", "w": 1, "l": 0.15, "bulk": "bulk tie"})
    out = _call(ctx, "sync_layout", lib="l", cell="wr", instances=["M1"])
    assert out["updated"] == [] and [o["name"] for o in out["own_layout"]] == ["M1"]    # one piece, placed by hand
    info = _call(ctx, "get_layout_info", lib="l", cell="wr")
    taps = info["schematic_sync"]["wiring"]["butted_taps"]
    assert len(taps) == 1 and taps[0]["device"] == "M1" and taps[0]["tap"] == "vss"
    assert taps[0]["source_drain"] in ("a", "b")                          # whichever side the PCell butts it to
    m3 = next(i for i in info["instances"] if i["name"] == "M3")["bbox_um"]
    mid = round((m3[1] + m3[3]) / 2, 2)
    _call(ctx, "add_box", lib="l", cell="wr", layer="li1", box=[m3[0], mid - 0.1, m3[2], mid + 0.1])
    w = _call(ctx, "get_layout_info", lib="l", cell="wr")["schematic_sync"]["wiring"]
    assert [s["nets"] for s in w["shorts"]] == [["vss", "x"]]
    # a port's label is a pin of its net: on a piece of its own (a pin left behind when the device moved), c is open
    _call(ctx, "add_instance", lib="l", cell="wr", symbol="devices/opin.sym", x=-200, y=0, params={"lab": "c"})
    _call(ctx, "connect_pins", lib="l", cell="wr", net="c", pins=["M2:D"])
    _call(ctx, "add_box", lib="l", cell="wr", layer="met1", box=[-40, 0, -39, 1])
    _call(ctx, "add_label", lib="l", cell="wr", layer="met1.label", text="c", x=-39.5, y=0.5)
    sync = _call(ctx, "get_layout_info", lib="l", cell="wr")["schematic_sync"]
    c = next(u for u in sync["wiring"]["unrouted"] if u["net"] == "c")
    assert "c" in sync["unrouted_nets"] and c["pieces"] == 2 and set(c["apart"]) <= {"c (label)", "M2:D"}
