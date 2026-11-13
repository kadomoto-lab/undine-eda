"""Drawing on the PDK's stack by agents (drawing.py, connectivity.py, agent_tools): rule-correct via stacks, taps and
guard rings, L / Z routes that refuse to touch another net, a device's terminals with their nets, nets highlighted and
DRC markers in render_layout. Checked against the real tools where sky130A is installed: KLayout's mr deck and Magic's
drc(full) on what the tools drew, and an inverter routed only with route matching its schematic in KLayout LVS."""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import klayout.db as kdb  # noqa: E402
import pytest  # noqa: E402

from undine import tools  # noqa: E402
from undine.agents import agent_tools as T  # noqa: E402
from undine.layouts import connectivity, drawing  # noqa: E402
from undine.pdks import pcell, pdkprofile  # noqa: E402
from undine.pdks.pdk import default_pdk_roots, discover  # noqa: E402

SKY = next((p for p in discover(default_pdk_roots()).values() if p.name == "sky130A" and p.decks_dir), None)
needs_sky = pytest.mark.skipif(SKY is None, reason="sky130A is not installed")
needs_drc = pytest.mark.skipif(SKY is None or not tools.find("klayout") or not tools.find("magic"),
                               reason="sky130A, KLayout or Magic is not installed")
needs_pcells = pytest.mark.skipif(not (SKY is not None and pcell.available(SKY) and tools.find("klayout") and tools.find("magic")),
                                  reason="sky130A with its PCell environment, KLayout or Magic is not installed")


def _server(pdk=None):
    from undine.server.api import Server

    pdk = pdk or SKY
    root = Path(tempfile.mkdtemp())
    srv = Server(root, {pdk.id: pdk}, [root])
    srv.store.create("p", pdk.id).create_library("l")
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    assert not T.call(ctx, "open_changeset", {"title": "draw", "reason": "r"})[1]
    return root, srv, ctx


def _call(ctx, tool, **a):
    res, err = T.call(ctx, tool, {"reason": "x", **a})
    assert not err, res
    return json.loads(res)


def _refused(ctx, tool, **a) -> str:
    res, err = T.call(ctx, tool, {"reason": "x", **a})
    assert err, res
    return res


def _wait(job) -> dict:
    while job.status()["state"] in ("queued", "running"):
        time.sleep(0.2)
    st = job.status()
    assert st["state"] == "done", st
    return st["summary"]


def _drc(srv, project, cell) -> tuple[dict, dict]:
    """(KLayout mr deck, Magic drc(full)) summaries of a cell."""
    srv.db(project, "l").save()
    return _wait(srv.start_drc(project, "l", cell)), _wait(srv.start_magic_drc(project, "l", cell))


# ------------------------------------------------------------------------------------------ rules and geometry


def test_the_sky130_stack_names_its_layers_and_cuts():
    st = pdkprofile.SKY130.stack
    names = {st.diff, st.tap, st.poly, st.well, *(c.name for c in st.conductors), *(c.name for c in st.cuts),
             *(p.name for p in st.plates), *st.taps.implant.values(), *(e[0] for c in st.cuts for e in c.extra)}
    assert names <= set(pdkprofile.SKY130.layers), names - set(pdkprofile.SKY130.layers)
    for n in ("hvi", "capm", "npc", "hvntm", "areaid.sc"):       # what devices draw, named in get_device_geometry
        assert n in pdkprofile.SKY130.layers
    r = drawing.Rules(pdkprofile.SKY130, 0.001)
    assert [c.name for c in r.chain("li1", "met5")] == ["mcon", "via", "via2", "via3", "via4"]
    assert [c.name for c in r.chain("met3", "poly")] == ["licon1", "mcon", "via", "via2"]
    for frm, to, why in (("diff", "poly", "no cut joins"), ("met1", "met1", "both met1"), ("met1", "capm", "PCell's own cuts")):
        with pytest.raises(drawing.DrawError, match=why):
            r.chain(frm, to)
    with pytest.raises(drawing.DrawError, match="no layer stack"):
        drawing.Rules(pdkprofile.GENERIC, 0.001)


@pytest.mark.parametrize("frm,to,kw", [("li1", "met5", {}), ("poly", "met2", {}), ("li1", "met2", {"rows": 2, "cols": 3}),
                                       ("met1", "met4", {"size": (2000, 1000)}), ("met2", "met3", {"horizontal": False})])
def test_a_via_stack_meets_every_enclosure_width_and_area(frm, to, kw):
    """Each cut inside each pad by the rule on every side and the larger one on two opposite sides; each pad at least its
    conductor's width and area; cuts a spacing apart; everything on the 5 nm grid."""
    r = drawing.Rules(pdkprofile.SKY130, 0.001)
    res = drawing.via_stack(r, 1003, 2001, frm, to, **kw)
    pads = res["pads"]
    for lv, c in zip(res["levels"], r.chain(frm, to)):
        cuts = [b for n, b in res["shapes"] if n == c.name and lv["box"].contains(b.center())]
        assert len(cuts) == lv["rows"] * lv["cols"] and all(b.width() == b.height() == r.d(c.size) for b in cuts)
        gaps = [max(abs(a.center().x - b.center().x), abs(a.center().y - b.center().y)) - r.d(c.size)
                for a in cuts for b in cuts if a != b]
        assert not gaps or min(gaps) >= r.d(c.space)
        for layer, (lo, hi) in ((c.below, c.enc_below), (c.above, c.enc_above)):
            p, a = pads[layer], lv["box"]
            sides = (a.left - p.left, p.right - a.right, a.bottom - p.bottom, p.top - a.top)
            assert min(sides) >= r.d(lo) and (min(sides[:2]) >= r.d(hi) or min(sides[2:]) >= r.d(hi)), (layer, sides)
    for layer, p in pads.items():
        cond = r.st.conductor(layer)
        assert min(p.width(), p.height()) >= r.d(cond.width) and p.width() * p.height() * 1e-6 >= cond.area - 1e-9
    assert all(v % 5 == 0 for _, b in res["shapes"] for v in (b.left, b.bottom, b.right, b.top))
    if frm == "poly":                                              # a poly contact: its npc around it
        assert any(n == "npc" for n, _ in res["shapes"])


@needs_drc
def test_vias_taps_and_rings_the_tools_draw_are_drc_clean_in_klayout_and_magic():
    root, srv, ctx = _server()
    _call(ctx, "create_cell", lib="l", cell="t", views=["layout"])
    call = lambda tool, **a: _call(ctx, tool, lib="l", cell="t", **a)
    for i, (frm, to, kw) in enumerate((("li1", "met5", {}), ("li1", "met2", {"rows": 2, "cols": 3}),
                                       ("met1", "met4", {"size": [2.0, 1.0]}), ("poly", "met1", {}),
                                       ("met2", "met3", {"direction": "v"}), ("met3", "met4", {"rows": 3, "cols": 1}))):
        out = call("add_via", x=4 * i, y=0, from_layer=frm, to_layer=to, **kw)
        assert out["ok"] and out["levels"][0]["cut"] in ("mcon", "via", "licon1", "via2", "via3")
    assert call("add_tap", kind="ptap", box=[0, 5, 2, 5.25], net="vss")["contacts"] == 5
    call("add_tap", kind="ptap", x=4.2, y=5.1, metal="li1")
    call("add_tap", kind="ntap", box=[8, 5, 8.25, 7], net="vdd")
    call("guard_ring", kind="ptap", around=[12, 4, 15, 7], net="vss")
    call("guard_ring", kind="ntap", around=[18, 4, 21, 6], width=0.6, net="vdd")
    # strapped up to met2 / met3: the ring as wide as a row of each via needs, a one-contact tap's met1 grown for one
    assert call("guard_ring", kind="ptap", around=[24, 0, 27, 3], metal="met2", net="vss")["outer"] == [23.74, -0.26, 27.26, 3.26]
    assert call("guard_ring", kind="ptap", around=[30, 0, 33, 3], metal="met3", net="vss")["outer"] == [29.67, -0.33, 33.33, 3.33]
    assert "too narrow up to met2: it needs 0.26 um" in _refused(ctx, "guard_ring", lib="l", cell="t", kind="ptap",
                                                                 around=[36, 0, 39, 3], metal="met2", width=0.25)
    call("add_tap", kind="ptap", box=[0, 10, 2, 10.3], net="vss", metal="met2")
    call("add_tap", kind="ntap", box=[4, 10, 4.3, 12], net="vdd", metal="met3")
    # a contact on a diffusion needs that diffusion; drawn by hand under it, the contact goes on it (within 15 um of a
    # substrate tap: Magic's latch-up rule LU.2)
    assert "needs diff over" in _refused(ctx, "add_via", lib="l", cell="t", x=1, y=7.5, from_layer="diff", to_layer="met1")
    _call(ctx, "add_box", lib="l", cell="t", layer="diff", box=[0.5, 7, 1.5, 8])
    _call(ctx, "add_box", lib="l", cell="t", layer="nsdm", box=[0.375, 6.875, 1.625, 8.125])
    call("add_via", x=1, y=7.5, from_layer="diff", to_layer="met1")
    klayout, magic = _drc(srv, ctx.work, "t")
    assert klayout["total"] == 0 and magic["total"] == 0, (klayout, magic)
    # the decks see what is wrong: a via1 pad 5 nm short of its enclosure fails both
    _call(ctx, "create_cell", lib="l", cell="bad", views=["layout"])
    for layer, box in (("met1", [0, 0, 0.25, 0.32]), ("via", [0.05, 0.085, 0.2, 0.235]), ("met2", [0, 0, 0.26, 0.32])):
        _call(ctx, "add_box", lib="l", cell="bad", layer=layer, box=box)
    klayout, magic = _drc(srv, ctx.work, "bad")
    assert klayout["total"] > 0 and magic["total"] > 0, (klayout, magic)
    shutil.rmtree(root)


@needs_drc
def test_taps_rings_and_routes_keep_the_front_end_rules_and_their_nets():
    """What the decks check beyond the drawn shapes themselves: an implant apart from another of its kind, an n-well
    from another n-well and from n+ diffusion, one net's n-well never merged with another's, no poly over a diffusion,
    a contact kept from the gate, the larger spacing next to a wide metal on either side. Refused where it would break
    one, and what is drawn next to them DRC clean."""
    root, srv, ctx = _server()
    _call(ctx, "create_cell", lib="l", cell="f", views=["layout"])
    call = lambda tool, **a: _call(ctx, tool, lib="l", cell="f", **a)
    refused = lambda tool, **a: _refused(ctx, tool, lib="l", cell="f", **a)
    # an n+ diffusion with a contact at each end, labelled x, and a substrate tap below it (Magic's latch-up rule)
    call("add_box", layer="diff", box=[0, 0, 4, 0.5])
    call("add_box", layer="nsdm", box=[-0.125, -0.125, 4.125, 0.625])
    for x in (0.3, 3.7):
        call("add_via", x=x, y=0.25, from_layer="diff", to_layer="li1")
    call("add_label", layer="li1.label", text="x", x=0.3, y=0.25)
    call("add_tap", kind="ptap", box=[-3, 0, -2.7, 2], net="vss")
    # poly across it would be a new transistor: refused when led over it, taken round it otherwise
    why = refused("route", layer="poly", **{"from": [2, -1.5], "to": [2, 2]}, through=[[2, 0]])
    assert "its poly would cross diff at [0.0, 0.0, 4.0, 0.5]" in why
    assert "closer than 0.075 um to diff" in refused("route", layer="poly", **{"from": [-1, 0.6], "to": [3, 0.6]},
                                                     through=[[1, 0.6]])
    way = call("route", layer="poly", **{"from": [2, -1.5], "to": [2, 2]}, dry_run=True)
    assert all(not (0 - 0.15 < x < 4 + 0.15) for x, _ in way["points"][1:-1])
    # an n-well tap 0.3 um above: its nsdm against the diffusion's, its n-well against the n+ diffusion
    why = refused("add_tap", kind="ntap", box=[1, 0.8, 3, 1.1], net="vdd")
    assert "nsdm comes closer than 0.38 um to nsdm" in why and "0.34 um to an n+ diffusion" in why
    # two n-well taps: another net's n-well is neither merged nor closer than 1.27 um; one of the same net is merged
    call("add_tap", kind="ntap", box=[0, 3, 2, 3.3], net="vdd")
    call("add_label", layer="met1.label", text="vdd", x=1, y=3.15)
    assert "touches vdd on nwell" in refused("add_tap", kind="ntap", box=[0, 3.6, 2, 3.9], net="bt")
    assert "n-well comes closer than 1.27 um" in refused("add_tap", kind="ntap", box=[0, 4.2, 2, 4.5], net="bt")
    call("add_tap", kind="ntap", box=[0, 5.8, 2, 6.1], net="bt")
    call("add_label", layer="met1.label", text="bt", x=1, y=5.95)
    # an ntap ring of vdd around bt's n-well would short them through the n-well
    assert "touches bt on nwell" in refused("guard_ring", kind="ntap", around=[-1, 5.5, 3, 7], net="vdd")
    # contacts keep from the gate: a poly contact on the gate over diffusion, a diffusion contact against the gate
    _hand_nfet(srv.db(ctx.work, "l"), "g")
    assert "licon.14" in _refused(ctx, "add_via", lib="l", cell="g", x=0.375, y=0.5, from_layer="poly", to_layer="li1")
    assert "licon.11" in _refused(ctx, "add_via", lib="l", cell="g", x=0.2, y=0.8, from_layer="diff", to_layer="li1")
    # a wide met3 route keeps 0.4 um from narrow met3 of another net, though that one is narrow
    call("add_box", layer="met3", box=[6, 2.1, 16, 2.4])
    call("add_label", layer="met3.label", text="a", x=11, y=2.25)
    assert "0.4 um where one is wider than 3 um" in refused("route", layer="met3", width=3.5, **{"from": [6, 0], "to": [16, 0]},
                                                           through=[[11, 0]])
    call("route", layer="met3", width=3.5, **{"from": [6, -0.1], "to": [16, -0.1]})
    klayout, magic = _drc(srv, ctx.work, "f")
    assert klayout["total"] == 0 and magic["total"] == 0, (klayout, magic)
    shutil.rmtree(root)


@needs_pcells
def test_routes_and_guard_rings_round_pcells_are_drc_clean_and_keep_their_nets():
    """The PDK's own devices: a route between two bulk ties (the PCell's butted tie as place_pcell places it: sync_layout
    straps a source on the bulk net to it) leaves each from its narrow tap strip (as wide as a contact) by the strip's
    centre line, so its contact keeps clear of the source beside it, with or without through points; a ring round
    them keeps its implant 0.38 um from theirs, which reach their extent, by default; an n-well ring of another net
    than the pMOS's bulk is refused."""
    root, srv, ctx = _server()
    call = lambda tool, **a: _call(ctx, tool, lib="l", cell="rg", **a)
    refused = lambda tool, **a: _refused(ctx, tool, lib="l", cell="rg", **a)
    call("create_cell", views=["schematic", "layout"])
    for name, sym, x, y in (("XA", "nfet", 0, 200), ("XB", "nfet", 200, 200), ("XP", "pfet", 0, 0)):
        call("add_instance", symbol=f"sky130_fd_pr/{sym}_01v8.sym", x=x, y=y, params={"name": name, "W": "2", "L": "0.15"})
    call("connect_nets", nets={"in": ["XA:G", "XB:G", "XP:G"], "out": ["XA:D", "XB:D", "XP:D"], "vdd": ["XP:S", "XP:B"],
                               "vss": ["XA:S", "XA:B", "XB:S", "XB:B"]}, style="label")
    call("sync_layout")
    call("move_instance", instance="XP", dx=0, dy=7)
    at = {i["name"]: i["id"].rsplit("@", 1)[1].split(",") for i in call("get_layout_info")["instances"]
          if i.get("name") in ("XA", "XB")}
    call("delete_instances", instances=["XA", "XB"])
    for i, (x, y) in at.items():
        call("place_pcell", pcell="nfet", name=i, x=float(x), y=float(y),
             params={"type": "sky130_fd_pr__nfet_01v8", "w": 2, "l": 0.15, "bulk": "bulk tie"})
    g = {i: call("get_device_geometry", instance=i) for i in ("XA", "XB")}
    strip = {i: g[i]["terminals"]["B"]["shapes"]["li1"][0] for i in g}
    assert all(round(b[2] - b[0], 3) == 0.17 for b in strip.values())          # as wide as a contact
    mid = {i: round((b[0] + b[2]) / 2, 3) for i, b in strip.items()}
    way = call("route", layer="met1", **{"from": "XB:B", "to": "XA:B"}, dry_run=True)
    assert [way["points"][0][0], way["points"][-1][0]] == [mid["XB"], mid["XA"]]
    below = round(min(g["XA"]["bbox"][1], g["XB"]["bbox"][1]) - 0.21, 3)
    out = call("route", layer="met1", **{"from": "XB:B", "to": "XA:B"}, through=[[mid["XB"], below], [mid["XA"], below]])
    assert out["net"] == "vss" and out["points"][1:3] == [[mid["XB"], below], [mid["XA"], below]]
    assert out["points"][0][1] < 1 and out["points"][-1][1] < 1                  # each end nearest its through point
    assert "psdm.1" in refused("guard_ring", kind="ptap", around=["XA", "XB"], net="vss", spacing=0.27)
    call("guard_ring", kind="ptap", around=["XA", "XB"], net="vss")
    assert "touches vdd (XP:B, XP:S) on nwell" in refused("guard_ring", kind="ntap", around=["XP"], net="bt")   # tied
    assert "XA is an nMOS" in refused("guard_ring", kind="ntap", around=["XA", "XP"], net="vdd")
    call("guard_ring", kind="ntap", around=["XP"], net="vdd")
    klayout, magic = _drc(srv, ctx.work, "rg")
    assert klayout["total"] == 0 and magic["total"] == 0, (klayout, magic)
    shutil.rmtree(root)


@needs_pcells
def test_a_well_joins_no_wires_and_a_body_without_a_tap_is_tied_by_one_nearby():
    """Two pMOS on bt, each with its own tie, in one n-well drawn over both: bt is joined only through the well (LVS
    joins it there), so the wiring check lists it in well_only and route draws the metal between their sources (it is
    one net then); a third pMOS on vdd in that well shorts bt and vdd through it. An nMOS without a tap (bulk none)
    beside another one's vss tie is tied by that tap; moved 20 um away, it is beyond the latch-up distance (LU.2), and
    a vss guard ring round it ties it again."""
    root, srv, ctx = _server()
    call = lambda tool, **a: _call(ctx, tool, lib="l", cell="wo", **a)
    call("create_cell", views=["schematic", "layout"])
    for name, sym, x, y in (("P1", "pfet", 0, 0), ("P2", "pfet", 200, 0), ("P3", "pfet", 400, 0), ("N1", "nfet", 0, 300),
                            ("N2", "nfet", 200, 300)):
        call("add_instance", symbol=f"sky130_fd_pr/{sym}_01v8.sym", x=x, y=y, params={"name": name, "W": "2", "L": "0.15"})
    call("connect_nets", nets={"g": ["P1:G", "P2:G", "P3:G", "N1:G", "N2:G"], "a": ["P1:D"], "b": ["P2:D"], "c": ["N1:D"],
                               "d": ["N2:D"], "e": ["P3:D"], "bt": ["P1:S", "P1:B", "P2:S", "P2:B"], "vdd": ["P3:S", "P3:B"],
                               "vss": ["N1:S", "N1:B", "N2:S", "N2:B"]}, style="label")
    call("sync_layout")
    call("sync_layout", instances=["N1"], layout={"N1": {"bulk": "none"}})
    box = {i["name"]: i["bbox_um"] for i in call("get_layout_info")["instances"]}
    wiring = lambda: call("get_layout_info")["schematic_sync"]["wiring"]
    w = wiring()
    assert "well_only" not in w and next(u for u in w["unrouted"] if u["net"] == "bt")["apart"] in (["P1:B", "P1:S"],
                                                                                                     ["P2:B", "P2:S"])
    # N1's body: N2's tie (its source strapped to it: on vss) is the nearest tap of vss
    tie = w["body_ties"]
    assert [(t["pin"], t["net"], t["tap"]) for t in tie] == [("N1:B", "vss", "ptap")] and tie[0]["distance_um"] < 15
    assert "N1:B" not in str(w["unrouted"]) and "substrate_only" not in w and "bodies_untied" not in w
    p1, p2 = box["P1"], box["P2"]
    call("add_box", layer="nwell", box=[p1[0], max(p1[1], p2[1]), p2[2], min(p1[3], p2[3])])     # one well for both
    w = wiring()
    assert w["well_only"] == ["bt"] and w["shorts"] == []                     # joined, but only through the well
    r = call("route", layer="met1", **{"from": "P1:S", "to": "P2:S"})
    assert r["net"] == "bt" and "joined only through an n-well" in r["note"]
    assert "one net already" in _refused(ctx, "route", lib="l", cell="wo", layer="met1", **{"from": "P1:S", "to": "P2:S"})
    w = wiring()
    assert "well_only" not in w and "bt" not in [u["net"] for u in w["unrouted"]]
    p3 = box["P3"]
    call("add_box", layer="nwell", box=[p2[0], max(p2[1], p3[1]), p3[2], min(p2[3], p3[3])])     # P3's well joins it
    assert {"nets": ["bt", "vdd"], "on": "nwell"}.items() <= next(s for s in wiring()["shorts"] if s.get("on") == "nwell").items()
    call("move_instance", instance="N1", dx=-20, dy=0)
    w = wiring()
    assert "body_ties" not in w and [u["pin"] for u in w["bodies_untied"]] == ["N1:B"]
    assert "15 um latch-up distance (LU.2)" in w["bodies_untied"][0]["why"]
    assert "N1:B" in next(u for u in w["unrouted"] if u["net"] == "vss")["apart"]
    # a vss guard ring drawn round it (labelled vss: a tap of its net) ties it again
    ring = call("guard_ring", kind="ptap", around=["N1"], net="vss")
    (i0, i1, i2, i3), (o0, o1, o2, o3) = ring["inner"], ring["outer"]
    call("add_label", layer="met1.label", text="vss", x=round((i0 + o0) / 2, 3), y=round((i1 + i3) / 2, 3))
    w = wiring()
    tie = next(t for t in w["body_ties"] if t["pin"] == "N1:B")
    x, y = tie["tap_um"]
    assert tie["distance_um"] < 5 and o0 <= x <= o2 and o1 <= y <= o3 and not (i0 < x < i2 and i1 < y < i3), (tie, ring)
    assert "bodies_untied" not in w
    shutil.rmtree(root)


@needs_pcells
def test_a_well_short_is_found_when_a_net_in_the_well_has_a_ptap_too():
    """bt's and vdd's ties in one n-well are a short through it, and still are once bt also has a p-tap (its layout net
    then reaches the substrate, as a vss rail with p-taps and an n-tap in a shield well does)."""
    root, srv, ctx = _server()
    call = lambda tool, **a: _call(ctx, tool, lib="l", cell="ws", **a)
    call("create_cell", views=["schematic", "layout"])
    for name, x in (("P1", 0), ("P2", 200), ("P3", 400)):
        call("add_instance", symbol="sky130_fd_pr/pfet_01v8.sym", x=x, y=0, params={"name": name, "W": "2", "L": "0.15"})
    call("connect_nets", nets={"g": ["P1:G", "P2:G", "P3:G"], "a": ["P1:D"], "b": ["P2:D"], "e": ["P3:D"],
                               "bt": ["P1:S", "P1:B", "P2:S", "P2:B"], "vdd": ["P3:S", "P3:B"]}, style="label")
    call("sync_layout")
    box = {i["name"]: i["bbox_um"] for i in call("get_layout_info")["instances"]}
    wiring = lambda: call("get_layout_info")["schematic_sync"]["wiring"]
    p1, p2, p3 = box["P1"], box["P2"], box["P3"]
    call("add_box", layer="nwell", box=[p1[0], max(p1[1], p2[1], p3[1]), p3[2], min(p1[3], p2[3], p3[3])])
    well = [s for s in wiring()["shorts"] if s.get("on") == "nwell"]
    assert [s["nets"] for s in well] == [["bt", "vdd"]], well
    t = call("add_tap", kind="ptap", net="bt", x=p1[0] - 2.5, y=(p1[1] + p1[3]) / 2)["tap"]
    call("route", layer="met1", **{"from": [round((t[0] + t[2]) / 2, 3), round((t[1] + t[3]) / 2, 3)], "to": "P1:S"})
    well = [s for s in wiring()["shorts"] if s.get("on") == "nwell"]
    assert [s["nets"] for s in well] == [["bt", "vdd"]], well
    shutil.rmtree(root)


@needs_pcells
def test_a_bias_net_of_bodies_only_is_tied_by_the_taps_drawn_in_its_wells():
    """nb is only the bodies of two pMOS without taps (bulk none), with no label: an n-tap drawn in each one's well,
    wired to nothing else, ties it; the two taps joined by metal route nb."""
    root, srv, ctx = _server()
    call = lambda tool, **a: _call(ctx, tool, lib="l", cell="nb", **a)
    call("create_cell", views=["schematic", "layout"])
    for name, x in (("P4", 0), ("P5", 200)):
        call("add_instance", symbol="sky130_fd_pr/pfet_01v8.sym", x=x, y=0, params={"name": name, "W": "2", "L": "0.15"})
    call("connect_nets", nets={"g": ["P4:G", "P5:G"], "a": ["P4:D"], "b": ["P5:D"], "s": ["P4:S", "P5:S"],
                               "nb": ["P4:B", "P5:B"]}, style="label")
    call("sync_layout", layout={"P4": {"bulk": "none"}, "P5": {"bulk": "none"}})
    box = {i["name"]: i["bbox_um"] for i in call("get_layout_info")["instances"]}
    wiring = lambda: call("get_layout_info")["schematic_sync"]["wiring"]
    w = wiring()
    assert {u["pin"]: u["why"] for u in w["bodies_untied"]} == {
        "P4:B": "no tap in its n-well: draw one (add_tap / guard_ring)",
        "P5:B": "no tap in its n-well: draw one (add_tap / guard_ring)"}, w
    p4, p5 = box["P4"], box["P5"]
    t = call("add_tap", kind="ntap", net="nb", box=[p4[0] - 0.42, 1.2, p4[0] - 0.17, 1.9])["tap"]
    u = call("add_tap", kind="ntap", net="nb", box=[p5[2] + 0.17, 1.2, p5[2] + 0.42, 1.9])["tap"]
    w = wiring()
    assert [(b["pin"], b["net"]) for b in w["body_ties"]] == [("P4:B", "nb"), ("P5:B", "nb")] and "bodies_untied" not in w
    assert next(x for x in w["unrouted"] if x["net"] == "nb")["pieces"] == 2         # the two taps are not joined yet
    c = lambda b: [round((b[0] + b[2]) / 2, 3), round((b[1] + b[3]) / 2, 3)]
    call("route", layer="met2", **{"from": c(t), "to": c(u)})
    w = wiring()
    assert "nb" not in [x["net"] for x in w["unrouted"]] and w["shorts"] == [] and "bodies_untied" not in w
    shutil.rmtree(root)


@needs_pcells
def test_pcell_terminals_carry_their_nets_now_and_in_the_latest_lvs():
    """A MIM capacitor's plates (c0: capm with the met4 over it, c1: met3) and a MOSFET's terminals, from the PDK's
    PCells, with the schematic net each is on and, after a real KLayout LVS, the layout and schematic net it had
    there."""
    root, srv, ctx = _server()
    call = lambda tool, **a: _call(ctx, tool, lib="l", cell="m", **a)
    call("create_cell", views=["schematic", "layout"])
    call("add_instance", symbol="sky130_fd_pr/cap_mim_m3_1.sym", x=0, y=0, params={"name": "XC", "W": "2", "L": "2", "MF": "1"})
    call("add_instance", symbol="sky130_fd_pr/nfet_01v8.sym", x=300, y=0, params={"name": "XN", "W": "1", "L": "0.15"})
    call("connect_nets", nets={"top": ["XC:c0", "XN:D"], "bot": ["XC:c1", "XN:G"], "vss": ["XN:S", "XN:B"]}, style="label")
    call("sync_layout")
    call("move_instance", instance="XN", dx=-6, dy=0)
    c = call("get_device_geometry", instance="XC")
    assert c["device"]["kind"] == "mim" and sorted(c["terminals"]) == ["c0", "c1"] and "lvs" not in c
    assert set(c["terminals"]["c0"]["shapes"]) == {"capm", "met4"} and "met3" in c["terminals"]["c1"]["shapes"]
    assert [c["terminals"][t]["schematic_net"] for t in ("c0", "c1")] == ["top", "bot"]
    assert call("route", layer="met4", **{"from": "XC:c0", "to": "XN:D"})["net"] == "top"
    assert call("route", layer="met3", **{"from": "XC:c1", "to": "XN:G"})["net"] == "bot"
    # sync_layout strapped the source (on vss, the bulk) to the butted tap (the _tie cell): one net already
    assert "one net already" in _refused(ctx, "route", lib="l", cell="m", layer="li1", **{"from": "XN:S", "to": "XN:B"})
    srv.db(ctx.work, "l").save()
    job = srv.start_lvs(ctx.work, "l", "m")
    _wait(job)
    c, n = call("get_device_geometry", instance="XC"), call("get_device_geometry", instance="XN")
    assert c["lvs"]["job"] == n["lvs"]["job"] == job.id and c["lvs"]["outdated"] is False
    assert {t: e["lvs"]["schematic_net"] for t, e in c["terminals"].items()} == {"c0": "TOP", "c1": "BOT"}
    assert {t: e["lvs"]["schematic_net"] for t, e in n["terminals"].items()} == {"S": "VSS", "G": "BOT", "D": "TOP", "B": "VSS"}
    assert c["terminals"]["c0"]["lvs"]["layout_net"] == n["terminals"]["D"]["lvs"]["layout_net"]
    shutil.rmtree(root)


@needs_drc
def test_poly_contacts_bridge_their_npc_wide_metal_is_its_wide_part_and_detours_never_double_back():
    """npc round neighbouring poly contacts closer than 0.27 um is bridged into one (npc.2, as Magic does when it
    writes GDS), a corner-to-corner gap no fill closes is refused; a comb of narrow bars is not a wide shape, so a
    wire between its fingers keeps only the plain spacing; a way round an obstacle never runs past its end and back;
    reading a layout's nets adds no layer to it. What is drawn is DRC clean."""
    root, srv, ctx = _server()
    _call(ctx, "create_cell", lib="l", cell="e", views=["layout"])
    db = srv.db(ctx.work, "l")
    before = sorted(str(db.layout.get_info(li)) for li in db.layout.layer_indexes())
    _call(ctx, "route", lib="l", cell="e", layer="met1", **{"from": [0, 0], "to": [1, 0]}, dry_run=True)
    assert sorted(str(db.layout.get_info(li)) for li in db.layout.layer_indexes()) == before
    _call(ctx, "create_cell", lib="l", cell="n", views=["layout"])
    call = lambda tool, **a: _call(ctx, tool, lib="l", cell="n", **a)
    refused = lambda tool, **a: _refused(ctx, tool, lib="l", cell="n", **a)
    for b in ([0, -1, 0.27, 2.2], [0.6, -1, 0.87, 2.2], [1.6, -1, 1.87, 2.2], [2.2, -1, 2.47, 2.6]):
        call("add_box", layer="poly", box=b)
    assert "bridged" not in call("add_via", x=0.135, y=1.5, from_layer="poly", to_layer="li1", direction="v")
    out = call("add_via", x=0.735, y=1.5, from_layer="poly", to_layer="li1", direction="v")       # 0.23 um apart
    assert out["bridged"] == {"npc": [[0.32, 1.315, 0.55, 1.685]]} and out["drawn"]["npc"] == 2
    assert "bridged" not in call("add_via", x=1.735, y=1.5, from_layer="poly", to_layer="li1", direction="v")
    assert "no fill bridges them" in refused("add_via", x=2.335, y=1.95, from_layer="poly", to_layer="li1", direction="v")
    # a met1 comb of 0.3 um bars, net a: wider than 3 um only as a whole
    for b in ([5, 0, 10, 0.3], [6, 0, 6.3, 5], [9, 0, 9.3, 5], [7.5, 2, 7.8, 4]):
        call("add_box", layer="met1", box=b)
    call("add_label", layer="met1.label", text="a", x=5.1, y=0.15)
    call("add_label", layer="met1.label", text="b", x=7.65, y=3)
    assert call("route", layer="met1", **{"from": [6.6, 0.57], "to": [8.6, 0.57]})["points"] == [[6.6, 0.57], [8.6, 0.57]]
    # b's bar between two level ends: over or under it, never past an end and back
    way = call("route", layer="met1", **{"from": [6.65, 3], "to": [8.65, 3]}, dry_run=True)
    assert len(way["points"]) == 4 and all(6.65 <= x <= 8.65 for x, _ in way["points"]) and way["length"] < 2 + 2 * 1.5
    call("route", layer="met1", **{"from": [6.65, 3], "to": [8.65, 3]})
    klayout, magic = _drc(srv, ctx.work, "n")
    assert klayout["total"] == 0 and magic["total"] == 0, (klayout, magic)
    shutil.rmtree(root)


def test_detours_are_simple_ways():
    assert drawing.simplify([(0, 0), (5, 0), (5, 0), (9, 0)]) == [(0, 0), (9, 0)]
    assert drawing.simplify([(0, 0), (9, 0), (4, 0)]) is None
    ways = drawing.detours((0, 0), (10, 0), kdb.Box(4, -1, 6, 1), 2)
    assert ways == [[(0, 0), (0, -3), (10, -3), (10, 0)], [(0, 0), (0, 3), (10, 3), (10, 0)]]


OTHERS = {p.name: p for p in discover(default_pdk_roots()).values() if p.name in ("gf180mcuD", "ihp-sg13g2") and p.decks_dir}
# what a lone cell breaks in their decks whatever is drawn: no substrate or well tap near its diffusion (rows of
# standard cells get them from tap cells; these tools draw no taps on these PDKs)
TAP_REACH = {"gf180mcuD": {"DF.13_LV", "DF.14_LV", "DF.13_MV", "DF.14_MV"}, "ihp-sg13g2": {"LU.a", "LU.b"}}


@pytest.mark.parametrize("name", ["gf180mcuD", "ihp-sg13g2"])
def test_the_gf180_and_ihp_stacks_draw_what_their_decks_pass(name):
    """Via stacks at every level (one cut and arrays, the whole stack, a poly and a diffusion contact) and routes on
    every metal, drawn on GF180MCU's and IHP SG13G2's stacks, pass the PDK's own KLayout deck; their nets refuse a
    short as on sky130; taps are refused, saying why."""
    pdk = OTHERS.get(name)
    if pdk is None or not tools.find("klayout"):
        pytest.skip(f"{name} or KLayout is not installed")
    root, srv, ctx = _server(pdk)
    call = lambda tool, **a: _call(ctx, tool, lib="l", cell="d", **a)
    call("create_cell", views=["layout"])
    st = pdk.profile.stack
    metals = [c.name for c in st.conductors if c.name != st.poly]
    x = 0
    for lo, hi in zip(metals, metals[1:]):
        for kw in ({}, {"rows": 2, "cols": 3}):
            assert call("add_via", x=x, y=0, from_layer=lo, to_layer=hi, **kw)["ok"]
            x += 8
    assert [lv["cut"] for lv in call("add_via", x=x, y=0, from_layer=metals[0], to_layer=metals[-1])["levels"]] == \
        [c.name for c in st.cuts if c.below != st.diff and c.below != st.poly]
    call("add_via", x=x + 10, y=0, from_layer=st.poly, to_layer=metals[1])
    call("add_box", layer=st.diff, box=[x + 16, 0, x + 18, 1])
    if name.startswith("gf180"):                                   # COMP needs its implant; IHP's Activ is n+ without pSD
        call("add_box", layer="nplus", box=[x + 15.8, -0.2, x + 18.2, 1.2])
    call("add_via", x=x + 17, y=0.5, from_layer=st.diff, to_layer=metals[2])
    for i, m in enumerate(metals):
        out = call("route", layer=m, **{"from": [0, 10 + 4 * i], "to": [12, 13 + 4 * i]})
        assert out["width"] == pdk.profile.stack.conductor(m).width and len(out["points"]) == 3
    assert call("route", layer=metals[2], **{"from": [20, 10], "to": [25, 14]})["vias"] == []
    # two labelled nets on metal1: a route between them is refused
    for text, box in (("a", [30, 10, 31, 11]), ("b", [36, 10, 37, 11])):
        call("add_box", layer=metals[0], box=box)
        call("add_label", layer=f"{metals[0]}.label", text=text, x=box[0] + 0.5, y=box[1] + 0.5)
    assert "would short two nets" in _refused(ctx, "route", lib="l", cell="d", layer=metals[1], **{"from": "a", "to": "b"})
    assert "no tap rules" in _refused(ctx, "add_tap", lib="l", cell="d", kind="ptap", x=40, y=0)
    srv.db(ctx.work, "l").save()
    drc = _wait(srv.start_drc(ctx.work, "l", "d"))
    assert {c["name"] for c in drc["categories"]} <= TAP_REACH[name], drc
    shutil.rmtree(root)


# ------------------------------------------------------------------------------------------ nets, routes, refusals


def _pads(ctx, cell="r"):
    """Two met1 pads labelled a and a met1 bar labelled b between them, a met2 bar labelled c above the second pad."""
    _call(ctx, "create_cell", lib="l", cell=cell, views=["layout"])
    for layer, box in (("met1", [0, 0, 1, 1]), ("met1", [6, 0, 7, 1]), ("met1", [3, -2, 3.5, 3]), ("met2", [8, -1, 8.5, 2])):
        _call(ctx, "add_box", lib="l", cell=cell, layer=layer, box=box)
    for text, layer, x, y in (("a", "met1.label", 0.5, 0.5), ("a", "met1.label", 6.5, 0.5), ("b", "met1.label", 3.2, 0),
                              ("c", "met2.label", 8.2, 0)):
        _call(ctx, "add_label", lib="l", cell=cell, layer=layer, text=text, x=x, y=y)


@needs_sky
def test_a_via_pad_that_would_leave_a_notch_in_its_own_net_is_refused():
    """sar_bootsw's vin, as an e2e run drew it: a route's via pad and a strap on met2, and a second via whose pad
    sticks out of the strap 0.075 um beside the first pad. KLayout's m2.2 and Magic's met2.2 flag the notch between
    the two pads; add_via says so instead of drawing it, and draws it 0.15 um further on."""
    root, srv, ctx = _server()
    _call(ctx, "create_cell", lib="l", cell="n", views=["layout"])
    for box in ([0.215, 0.085, 2.335, 0.345], [0.21, 0.0, 0.49, 0.37]):          # the strap, the first via's pad
        _call(ctx, "add_box", lib="l", cell="n", layer="met2", box=box)
    _call(ctx, "add_label", lib="l", cell="n", layer="met2.label", text="vin", x=1.5, y=0.2)
    msg = _refused(ctx, "add_via", lib="l", cell="n", x=0.95, y=0.215, from_layer="met2", to_layer="met3", cols=2, net="vin")
    assert "notch narrower than 0.14 um in its own net's met2" in msg, msg
    _call(ctx, "add_via", lib="l", cell="n", x=1.1, y=0.215, from_layer="met2", to_layer="met3", cols=2, net="vin")
    shutil.rmtree(root)


@needs_drc
def test_a_cut_on_or_against_a_cut_already_there_is_refused_unless_it_is_the_same_cut():
    """sar_bootsw as an e2e run drew it: a route's end via landing 0.11 um off a via already on that net, and a guard
    ring's mcons 0.19 um along the mcons of the ring already there, merge with them into shapes no cut is (KLayout's
    via.1a and ct.1_b, Magic's via1 width and mcon.2). add_via and route say so instead; a via exactly on the one
    there, or clear of it, is drawn, and both decks pass what was drawn."""
    root, srv, ctx = _server()
    _call(ctx, "create_cell", lib="l", cell="v", views=["layout"])
    _call(ctx, "add_box", lib="l", cell="v", layer="met1", box=[0, 0, 3, 0.5])
    _call(ctx, "add_label", lib="l", cell="v", layer="met1.label", text="a", x=2.5, y=0.25)
    first = _call(ctx, "add_via", lib="l", cell="v", x=0.5, y=0.25, from_layer="met1", to_layer="met2", net="a")
    msg = _refused(ctx, "add_via", lib="l", cell="v", x=0.61, y=0.25, from_layer="met1", to_layer="met2", net="a")
    assert "overlaps or abuts a via already there" in msg, msg
    msg = _refused(ctx, "add_via", lib="l", cell="v", x=0.65, y=0.25, from_layer="met1", to_layer="met2", net="a")
    assert "overlaps or abuts a via already there" in msg, msg          # edge to edge: one shape too
    _call(ctx, "add_via", lib="l", cell="v", x=0.5, y=0.25, from_layer="met1", to_layer="met2", net="a")   # the same cut
    _call(ctx, "add_via", lib="l", cell="v", x=1.5, y=0.25, from_layer="met1", to_layer="met2", net="a")   # clear of it
    # a route whose end via would land on the via there off by a fraction: led through a point it is refused there
    msg = _refused(ctx, "route", lib="l", cell="v", **{"from": [0.61, 0.385], "to": [2.5, 0.25]}, layer="met2",
                   through=[[0.61, 1.5], [2.5, 1.5]])          # on met1 just past the met2 pad there
    assert "overlaps or abuts a via already there" in msg, msg
    assert first["ok"]
    k, m = _drc(srv, ctx.work, "v")
    assert k["total"] == 0 and m["total"] == 0, (k, m)
    shutil.rmtree(root)


@needs_sky
def test_a_route_goes_round_another_net_on_a_layer_above_and_never_shorts():
    root, srv, ctx = _server()
    _pads(ctx)
    call = lambda **a: _call(ctx, "route", lib="l", cell="r", **a)
    refused = lambda **a: _refused(ctx, "route", lib="l", cell="r", **a)
    # on met1 the straight way crosses b: the route goes round b's bar instead, a spacing (a wide one too) clear
    way = call(**{"from": [1, 0.5], "to": [6, 0.5]}, layer="met1", dry_run=True)
    assert way["tried"] == 2 and way["points"] == [[1.0, 0.5], [1.0, -2.49], [6.0, -2.49], [6.0, 0.5]]
    # led straight through it, refused, saying where
    why = refused(**{"from": [1, 0.5], "to": [6, 0.5]}, layer="met1", through=[[3.25, 0.5]])
    assert "touches b on met1 at [3.0, -2.0, 3.5, 3.0]" in why and "through" in why
    # two nets of different names are not joined, a name the ends are not on is refused
    assert "a route between them would short two nets" in refused(**{"from": "a", "to": "b"}, layer="met1")
    assert "not on vdd" in refused(**{"from": [0.5, 0.5], "to": [6.5, 0.5]}, layer="met2", net="vdd")
    # on met2 it passes over b, with a rule-correct stack down to each pad; a dry run draws nothing
    before = srv.db(ctx.work, "l").cell("r").shapes(srv.db(ctx.work, "l").layout.layer(68, 44)).size()
    dry = call(**{"from": "a", "to": [6.5, 0.5]}, layer="met2", dry_run=True)
    assert dry["dry_run"] and srv.db(ctx.work, "l").cell("r").shapes(srv.db(ctx.work, "l").layout.layer(68, 44)).size() == before
    assert dry["points"] == [[0.925, 0.5], [6.5, 0.5]]          # a is in two pieces: from the one 6.5,0.5 is not on
    out = call(**{"from": [0.5, 0.5], "to": [6.5, 0.5]}, layer="met2")
    assert "one net already (a): nothing to route" in refused(**{"from": "a", "to": [6.5, 0.5]}, layer="met2")
    assert out["net"] == "a" and [v["from"] for v in out["vias"]] == ["met1", "met1"] and out["drawn"]["via"] == 2
    assert out["points"] == [[0.5, 0.5], [6.5, 0.5]] and out["width"] == 0.14
    nets = connectivity.Nets(srv.db(ctx.work, "l").layout, srv.db(ctx.work, "l").cell("r"), SKY.profile)
    assert len(nets.find("a")) == 1 and nets.describe(nets.find("a")[0]) == "a"
    # from the net a (its met2 now) round c's bar: the first L crosses c, the other one is taken; through leads it
    out = call(**{"from": "a", "to": [9.5, 3.5]}, layer="met2", width=0.2)
    assert out["tried"] == 2 and out["vias"] == [] and out["points"][1] == [out["points"][0][0], 3.5]
    out = call(**{"from": [12, 0], "to": [14, -2.5]}, layer="met2", through=[[12.5, -2]])
    assert out["points"] == [[12.0, 0.0], [12.5, 0.0], [12.5, -2.0], [14.0, -2.0], [14.0, -2.5]] and out["net"] is None
    # to a net by name: its nearest shape, a minimum-width wire too (by its centre line), not only a wider pad of it
    for layer, box in (("met2", [10, 5, 20, 5.14]), ("met2", [30, 0, 31, 1])):
        _call(ctx, "add_box", lib="l", cell="r", layer=layer, box=box)
    for x, y in ((15, 5.07), (30.5, 0.5)):
        _call(ctx, "add_label", lib="l", cell="r", layer="met2.label", text="w", x=x, y=y)
    assert call(**{"from": [15, 8], "to": "w"}, layer="met2", dry_run=True)["points"] == [[15.0, 8.0], [15.0, 5.07]]
    # a via that would join a and b is refused, one on b is drawn along b's bar; on b, but said to be a, refused
    assert "it would join a and b" in _refused(ctx, "add_via", lib="l", cell="r", x=3.25, y=0.5, from_layer="met1",
                                              to_layer="met2", size=[1.0, 0.5])
    assert "touches b" in _refused(ctx, "add_via", lib="l", cell="r", x=3.25, y=-1.5, from_layer="met1", to_layer="met3", net="a")
    out = _call(ctx, "add_via", lib="l", cell="r", x=3.25, y=-1.5, from_layer="met1", to_layer="met3", net="b")
    assert out["direction"] == "v" and out["pads"]["met3"][3] - out["pads"]["met3"][1] > 0.7
    shutil.rmtree(root)


def _hand_nfet(db, name="nmos1"):
    """A transistor drawn by hand (no PCell needed): diffusion crossed by a poly gate with a contact head above, a
    contact and li1 on each side."""
    db.new_cell(name)
    shapes = {(65, 20): [(0, 0, 750, 1000)], (66, 20): [(300, -200, 450, 1500), (210, 1250, 540, 1500)],
              (93, 44): [(-125, -125, 875, 1125)], (66, 44): [(65, 415, 235, 585), (515, 415, 685, 585), (290, 1290, 460, 1460)],
              (67, 20): [(65, 335, 235, 665), (515, 335, 685, 665), (210, 1290, 540, 1460)]}
    db.draw(name, [(ld, kdb.Box(*b)) for ld, bs in shapes.items() for b in bs])


@needs_sky
def test_device_geometry_gives_terminals_with_their_nets_and_any_layer():
    root, srv, ctx = _server()
    call = lambda tool, **a: _call(ctx, tool, lib="l", cell="top", **a)
    call("create_cell", views=["schematic", "layout"])
    call("add_instance", symbol="sky130_fd_pr/nfet_01v8.sym", x=0, y=0, params={"name": "M1", "W": "1", "L": "0.15"})
    call("connect_nets", nets={"out": ["M1:D"], "in": ["M1:G"], "vss": ["M1:S", "M1:B"]}, style="label")
    db = srv.db(ctx.work, "l")
    _hand_nfet(db)
    call("place_cell", child="nmos1", x=10, y=10, name="M1")
    call("add_label", layer="li1.label", text="vss", x=10.6, y=10.5)      # on the right strip: vss is routed there
    g = call("get_device_geometry", instance="M1")
    assert {k: v for k, v in g["device"].items() if k != "note"} == {"kind": "mos", "class": "nmos", "L": 0.15, "W": 1.0}
    assert sorted(g["terminals"]) == ["B", "D", "G", "S"] and g["terminals"]["G"]["schematic_net"] == "in"
    # S and D follow the schematic: the strip labelled vss is S, though the extraction calls it D
    assert g["terminals"]["S"]["shapes"]["li1"] == [[10.515, 10.335, 10.685, 10.665]] and g["terminals"]["S"]["net"].startswith("vss")
    assert g["terminals"]["D"]["net"] == "out (M1:D)" and "note" in g["device"]
    # exact rectangles: the gate with its contact head reads as both, not as its bounding box
    assert g["layers"]["poly (66/20)"] == [[10.21, 11.25, 10.54, 11.5], [10.3, 9.8, 10.45, 11.25]]
    assert g["layers"]["licon1 (66/44)"]["cuts"] == 3 and g["layers"]["licon1 (66/44)"]["size"] == [0.17, 0.17]
    # any layer by name, profile name or number; an unknown one is refused, not dropped
    g = call("get_device_geometry", instance="M1", layers=["93/44", "nsdm", "areaid.sc"], terminals=False)
    assert list(g["layers"]) == ["nsdm (93/44)", "areaid.sc (81/4)"] and "terminals" not in g
    assert "unknown layer 'nothing'" in _refused(ctx, "get_device_geometry", lib="l", cell="top", instance="M1", layers=["nothing"])
    g = call("get_device_geometry", instance="M1", layers=["poly"], polygons=True, terminals=False)
    assert len(g["layers"]["poly (66/20)"]) == 1 and len(g["layers"]["poly (66/20)"][0]) == 8          # one outline, 8 corners
    assert "no layout instance bound to M2" in _refused(ctx, "get_device_geometry", lib="l", cell="top", instance="M2")
    # route takes the terminals by name, and refuses to join two schematic nets
    assert "M1:D is on out and M1:S on vss" in _refused(ctx, "route", lib="l", cell="top", layer="met1",
                                                         **{"from": "M1:D", "to": "M1:S"})
    out = call("route", layer="met1", **{"from": "M1:G", "to": [10.375, 14]})
    assert out["net"] == "in" and out["vias"][0]["from"] == "li1" and out["vias"][0]["cuts"] == "mcon 1x1"
    shutil.rmtree(root)


def test_contacts_are_summarised_as_arrays_and_uneven_columns_said_so():
    """A source's, a drain's and a tap's contact columns side by side (XMS of the pilot: 0.45 and 0.375 um apart) are
    one cluster: irregular, with where its columns are."""
    cols = kdb.Region([kdb.Box(x, y, x + 170, y + 170) for x in (0, 450, 825) for y in range(0, 3600, 360)])
    a = T._cut_arrays(cols, 0.001)
    assert a["cuts"] == 30 and a["size"] == [0.17, 0.17]
    assert a["arrays"] == [{"count": 30, "columns": 3, "rows": 10, "bbox": [0.0, 0.0, 0.995, 3.41], "pitch": [0.375, 0.36],
                            "irregular": True, "x": [0.0, 0.45, 0.825]}]
    even = T._cut_arrays(kdb.Region([kdb.Box(x, 0, x + 170, 170) for x in (0, 340, 680)]), 0.001)["arrays"]
    assert even == [{"count": 3, "columns": 3, "rows": 1, "bbox": [0.0, 0.0, 0.85, 0.17], "pitch": [0.34, 0]}]


@needs_sky
def test_render_layout_highlights_nets_and_draws_the_drc_markers():
    root, srv, ctx = _server()
    _pads(ctx)
    pic = _call(ctx, "render_layout", lib="l", cell="r", highlight_nets=["a", "b", "nothing"], solid=True)
    assert [(h["net"], h["layers"], h["pieces"]) for h in pic["highlighted"]] == [("a", ["met1"], 2), ("b", ["met1"], 1)]
    assert pic["not_found"] == ["nothing"] and ctx.images and ctx.images[0][:4] == b"\x89PNG"
    lit = ctx.images[0]
    plain = _call(ctx, "render_layout", lib="l", cell="r", solid=True)
    assert "highlighted" not in plain and ctx.images[0] != lit
    none = _call(ctx, "render_layout", lib="l", cell="r", markers=True)
    assert none["markers"] == {} and "no finished DRC run yet" in none["markers_note"]
    if tools.find("klayout"):
        _call(ctx, "add_box", lib="l", cell="r", layer="met1", box=[1.05, 0, 1.1, 1])          # 0.05 wide, 0.05 from the pad
        job = srv.start_drc(ctx.work, "l", "r")
        _wait(job)
        m = _call(ctx, "render_layout", lib="l", cell="r", markers=True)["markers"]     # rule (checker) -> legend
        assert {"m1.1 (klayout)", "m1.2 (klayout)"} <= set(m) and all(r["drawn"] >= 1 and "outdated" not in r for r in m.values())
        # still found behind more than a hundred newer runs (a sweep's): not only among the newest 100
        runs = srv.project(ctx.work).path / "runs"
        for i in range(101):
            (runs / f"29991231-000000-{i:04d}").mkdir()
            (runs / f"29991231-000000-{i:04d}" / "status.json").write_text(json.dumps({"kind": "sim", "state": "done"}))
        assert _call(ctx, "render_layout", lib="l", cell="r", markers=True)["markers"]["m1.1 (klayout)"]["job"] == job.id
        assert "DRC (" in T.call(ctx, "get_checks", {"lib": "l", "cell": "r"})[0]
    shutil.rmtree(root)


# ------------------------------------------------------------------------------------------ an inverter, end to end


@needs_pcells
def test_an_inverter_routed_only_with_route_is_drc_clean_and_matches_lvs():
    root, srv, ctx = _server()
    call = lambda tool, **a: _call(ctx, tool, lib="l", cell="inv", **a)
    call("create_cell", views=["schematic", "layout"])
    call("add_instance", symbol="sky130_fd_pr/nfet_01v8.sym", x=0, y=200, params={"name": "XN", "W": "1", "L": "0.15"})
    call("add_instance", symbol="sky130_fd_pr/pfet_01v8.sym", x=0, y=0, params={"name": "XP", "W": "2", "L": "0.15"})
    for i, (pin, kind) in enumerate((("in", "ipin"), ("out", "opin"), ("vdd", "iopin"), ("vss", "iopin"))):
        call("add_instance", symbol=f"devices/{kind}.sym", x=-200, y=100 * i, params={"lab": pin})
    call("connect_nets", nets={"in": ["XN:G", "XP:G"], "out": ["XN:D", "XP:D"], "vdd": ["XP:S", "XP:B"],
                               "vss": ["XN:S", "XN:B"]}, style="label")
    assert sorted(call("sync_layout")["added"]) == ["XN", "XP"]
    call("move_instance", instance="XP", dx=0, dy=4)
    route = lambda a, b, layer, **kw: call("route", layer=layer, **{"from": a, "to": b}, **kw)
    # unrouted, S is the strip against the PCell's body tap (both on the bulk's net): tied to it in li1, no junction
    # of another net butts the tap
    assert all(call("get_device_geometry", instance=i)["terminals"]["S"].get("abuts_tap") for i in ("XN", "XP"))
    gate = route("XN:G", "XP:G", "met1")                    # straight and the L's cross the strips: round them
    assert gate["net"] == "in" and gate["tried"] > 1
    out = route("XN:D", "XP:D", "met2")
    assert out["net"] == "out" and len(out["vias"]) == 2
    for i in ("XN", "XP"):              # sync_layout strapped each source to its butted tap (the _tie cell) already
        assert "one net already" in _refused(ctx, "route", lib="l", cell="inv", layer="li1", **{"from": f"{i}:B", "to": f"{i}:S"})
    vss = route("XN:S", [-2, -1], "met2", width=0.3)
    vdd = route("XP:S", [-2, 11], "met2", width=0.3)
    assert (vss["net"], vdd["net"]) == ("vss", "vdd")
    assert "would short two nets" in _refused(ctx, "route", lib="l", cell="inv", layer="met2", **{"from": "XN:S", "to": "XP:S"})
    mid = lambda r, i: [(r["points"][i][0] + r["points"][i + 1][0]) / 2, (r["points"][i][1] + r["points"][i + 1][1]) / 2]
    for text, layer, (x, y) in (("in", "met1.label", mid(gate, 1)), ("out", "met2.label", mid(out, 0)),
                                ("vss", "met2.label", vss["points"][-1]), ("vdd", "met2.label", vdd["points"][-1])):
        call("add_label", layer=layer, text=text, x=x, y=y)
    klayout, magic = _drc(srv, ctx.work, "inv")
    assert klayout["total"] == 0 and magic["total"] == 0, (klayout, magic)
    lvs = _wait(srv.start_lvs(ctx.work, "l", "inv"))
    assert lvs["match"], lvs
    shutil.rmtree(root)


# ------------------------------------------------------------------------------------------ answers an agent reads


def test_changeset_history_and_batch_answers_are_grouped_and_rounded():
    import test_changeset as tc

    root, srv, cs = tc.setup()
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    _call(ctx, "open_changeset", title="t")
    db = srv.db(ctx.work, "l")
    db.layout.layer(68, 20)
    db.layout.layer(68, 5)
    db.lv.add_missing_layers()
    why = "the routing of the bootstrap core, which is long to say twice"
    ops = [{"tool": "add_box", "args": {"lib": "l", "cell": "top", "layer": "68/20", "box": [i, 0, i + 0.5, 0.5]}} for i in range(36)]
    ops.append({"tool": "add_box", "args": {"lib": "l", "cell": "top", "layer": "68/20", "box": [17.8651, 0, 18, 1]}})
    ops.append({"tool": "add_label", "args": {"lib": "l", "cell": "top", "layer": "68/5", "text": "vin", "x": 0.2, "y": 0.2}})
    out = _call(ctx, "batch", ops=ops, reason=why)
    assert out["results"] == [{"op": "0-35", "tool": "add_box", "result": {"ok": True}},
                              {"op": 36, "tool": "add_box", "result": {"ok": True, "snapped_to": [17.865, 0.0, 18.0, 1.0]}},
                              {"op": 37, "tool": "add_label", "result": {"ok": True}}]
    # one entry for the reason, said once; its operations counted, its effects per cell
    assert out["changes"] == [{"op": "batch", "operations": 38, "ops": {"add_box": 37, "add_label": 1}, "view": "layout",
                               "cells": {"l/top": ["add box 68/20 ×37", "add label 'vin' 68/5"]}, "reason": why}]
    _call(ctx, "create_cell", lib="l", cell="unit", views=["layout"])
    _call(ctx, "add_box", lib="l", cell="unit", layer="68/20", box=[0, 0, 1, 1])
    for x in (0.6, 2.6, 4.6):                                     # alike steps: one entry
        _call(ctx, "place_cell", lib="l", cell="top", child="unit", x=x, y=14.2)
    _call(ctx, "place_cell", lib="l", cell="top", child="unit", x=0.6, y=0.7, name="XCB", rot=90, reason="the cap")
    g = _call(ctx, "get_changeset")
    assert g["operations"] == 44 and [(o["op"], o.get("operations"), o.get("steps"), o.get("cells"), o.get("new_cells"))
                                      for o in g["changes"]] == [
        ("batch", 38, None, {"l/top": ["add box 68/20 ×37", "add label 'vin' 68/5"]}, None),
        ("create_cell", None, None, None, ["l/unit"]),
        ("add_box", None, None, {"l/unit": ["add box 68/20"]}, None),
        ("place_cell", 3, 3, {"l/top": ["add instance (unit) at 0.6,14.2", "add instance (unit) at 2.6,14.2",
                                        "add instance (unit) at 4.6,14.2"]}, None),
        ("place_cell", None, None, {"l/top": ["add instance XCB (unit) at 0.6,0.7 r90"]}, None)]
    assert [o["reason"] for o in g["changes"]].count(why) == 1
    one = _call(ctx, "get_changeset", detail=True, cell="l/top")       # each operation, only that cell's, counted so
    assert one["operations"] == len(one["changes"]) == 42
    assert _call(ctx, "get_changeset", cell="l/unit")["operations"] == 2
    assert "changed nothing of l/nothing" in _refused(ctx, "get_changeset", cell="l/nothing")
    hist = T.call(ctx, "get_history", {"lib": "l", "cell": "top"})[0]
    assert "add_box ×37, add_label add box 68/20 ×37" in hist and hist.count("bootstrap core") == 1
    # a step larger than the history's window is counted whole, at any limit
    _call(ctx, "batch", ops=[{"tool": "add_box", "args": {"lib": "l", "cell": "top", "layer": "68/20", "box": [i, 2, i + 0.5, 2.5]}}
                             for i in range(160)], reason="plates")
    for limit in (1, 3, 15):
        hist = T.call(ctx, "get_history", {"lib": "l", "cell": "top", "limit": limit})[0].splitlines()
        assert len(hist) == min(limit, 6) and "add_box ×160 add box 68/20 ×160" in hist[-1]
    shutil.rmtree(root)


def test_drawing_tools_refuse_a_pdk_without_a_stack():
    import test_changeset as tc

    root, srv, cs = tc.setup()                              # no PDK: no stack, no rules
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    _call(ctx, "open_changeset", title="t")
    for tool, a in (("route", {"from": [0, 0], "to": [1, 0], "layer": "met1"}),
                    ("add_via", {"x": 0, "y": 0, "from_layer": "met1", "to_layer": "met2"}),
                    ("add_tap", {"kind": "ptap", "x": 0, "y": 0}), ("guard_ring", {"kind": "ptap", "around": [0, 0, 1, 1]})):
        assert "needs the PDK's layer stack" in _refused(ctx, tool, lib="l", cell="top", **a)
    # a device's layers still, its terminals said to need the stack
    _call(ctx, "create_cell", lib="l", cell="dev", views=["layout"])
    db = srv.db(ctx.work, "l")
    db.layout.layer(66, 20)
    db.lv.add_missing_layers()
    _call(ctx, "add_box", lib="l", cell="dev", layer="66/20", box=[0, 0, 0.15, 1])
    _call(ctx, "place_cell", lib="l", cell="top", child="dev", x=1, y=1, name="M1")
    g = _call(ctx, "get_device_geometry", lib="l", cell="top", instance="M1")
    assert g["layers"] == {"66/20": [[1.0, 1.0, 1.15, 2.0]]} and g["terminals"] is None
    assert "layer stack" in g["terminals_note"] and "INST:TERM" not in g["note"]
    shutil.rmtree(root)


def test_get_netlist_leaves_a_long_code_block_out_on_request():
    import test_changeset as tc
    from undine.schematics import xschem

    root, srv, cs = tc.setup()
    lib = srv.project("p").library("l")
    sch = xschem.parse((lib.path / "top.sch").read_text())
    sch.comps.append(xschem.XComp("devices/code_shown.sym", 0, 200, 0, 0, {"name": "s1", "only_toplevel": "false",
                                                                           "value": ".control\n" + "* x\n" * 5000 + ".endc"}))
    (lib.path / "top.sch").write_text(xschem.dump(sch))
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    full = _call(ctx, "get_netlist", lib="l", cell="top", limit=1)            # the code fills the answer: shortened
    assert "_truncated" in full
    cut = _call(ctx, "get_netlist", lib="l", cell="top", code_limit=100)
    assert "_truncated" not in cut and len(json.dumps(cut)) < 2000
    assert cut["code"][0]["value"].startswith(".control") and "19914 more characters; all of it: instances ['s1']" in cut["code"][0]["value"]
    none = _call(ctx, "get_netlist", lib="l", cell="top", code=False)
    assert "code" not in none and none["code_blocks"] == ["s1"] and "R1" in none["instances"]
    shutil.rmtree(root)
