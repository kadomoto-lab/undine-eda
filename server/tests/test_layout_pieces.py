"""Drawing round devices placed in pieces and the cells generation leaves (agent_tools, connectivity, drawing, sdl,
layout): route's points and bends on the 5 nm grid, its via pads turned along a source / drain strip when that is
what fits, vias Magic refuses next to a placed cell's own (a MIM capacitor's contacts) refused, a device placed as
several place_pcell pieces enclosed and addressed as a whole or piece by piece, a multi-finger PCell's terminals,
unused generated cells taken out (recorded, brought back by a restore) and left out of the checks, and every restore
point of a changeset reachable. Checked against KLayout's mr deck and Magic's drc(full) where it matters."""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import klayout.db as kdb  # noqa: E402
import pytest  # noqa: E402

from test_layout_drawing import SKY, _call, _drc, _refused, _server, needs_drc, needs_pcells  # noqa: E402
from undine.agents import agent_tools as T  # noqa: E402
from undine.layouts import drawing, layout, sdl  # noqa: E402
from undine.pdks import pdkprofile  # noqa: E402
from undine.store import oplog  # noqa: E402

NFET = "sky130_fd_pr__nfet_01v8"


def _on_grid(srv, ctx, cell: str, lib: str = "l") -> list:
    """The cell's own shapes with a coordinate off the 5 nm grid."""
    db = srv.db(ctx.work, lib)
    c = db.layout.cell(cell)
    return [(db.layout.get_info(li), s.bbox()) for li in db.layout.layer_indexes() for s in c.shapes(li).each()
            if any(v % 5 for v in (s.bbox().left, s.bbox().bottom, s.bbox().right, s.bbox().top))]


# ------------------------------------------------------------------------------------------ route on the grid


def test_z_ways_and_detours_keep_their_bends_on_the_grid():
    r = drawing.Rules(pdkprofile.SKY130, 0.001)
    for way in drawing.manhattan((0, 0), (1010, 2030), grid=r.grid):
        assert all(x % 5 == 0 and y % 5 == 0 for x, y in way), way
    assert any(len(w) == 4 for w in drawing.manhattan((0, 0), (1010, 2030), grid=r.grid))
    for way in drawing.detours((0, 0), (5000, 0), kdb.Box(1003, -1001, 3998, 1002), 333, r.grid):
        assert all(x % 5 == 0 and y % 5 == 0 for x, y in way), way
        assert all(not (1003 - 333 < y < 1002 + 333) or x in (0, 5000) for x, y in way[1:-1])   # at least clear out


@needs_drc
def test_a_z_route_between_obstacles_is_drawn_on_the_grid_and_passes_the_offgrid_check():
    """Both L's blocked by another net, route takes a Z whose middle leg falls between grid points unsnapped
    (y 0.5075 um, drawn at 0.508 before): drawn on the grid, KLayout's x.1b (OFFGRID) finds nothing."""
    root, srv, ctx = _server()
    call = lambda tool, **a: _call(ctx, tool, lib="l", cell="z", **a)
    call("create_cell", views=["layout"])
    for b, net in (([0.9, -0.4, 1.4, 0.1], "x"), ([-0.4, 1.7, 0.4, 2.4], "x")):
        call("add_box", layer="met1", box=b)
        call("add_label", layer="met1.label", text=net, x=(b[0] + b[2]) / 2, y=(b[1] + b[3]) / 2)
    out = call("route", layer="met1", **{"from": [0, 0], "to": [1.01, 2.03]})
    assert len(out["points"]) == 4 and out["points"][1] == [0.0, 0.51], out      # y 0.5075 unsnapped
    assert all(round(v / 0.005, 6) % 1 == 0 for p in out["points"] for v in p), out["points"]
    assert not _on_grid(srv, ctx, "z")
    klayout, magic = _drc(srv, ctx.work, "z")
    assert klayout["total"] == 0 and magic["total"] == 0, (klayout, magic)
    shutil.rmtree(root)


# ------------------------------------------------------------------------------------------ via pads on a strip


@needs_pcells
def test_a_route_arriving_across_the_strips_turns_its_via_pads_along_the_drain_strip():
    """A drain strip of a multi-finger nfet between two source strips, reached straight from the side on met2: the
    met1 pad laid along the approach would come within 0.14 um of the source strip beside it; turned along the strip it
    fits, so the straight way is taken, DRC clean."""
    root, srv, ctx = _server()
    call = lambda tool, **a: _call(ctx, tool, lib="l", cell="t", **a)
    call("create_cell", views=["layout"])
    pid = call("place_pcell", pcell="nfet", x=0, y=0, params={"type": NFET, "w": 4, "l": 0.15, "nf": 3,
                                                                  "bulk": "bulk tie"})["id"]
    g = call("get_device_geometry", instance=pid, layers=["met1"])
    d = sorted(g["terminals"]["D"]["shapes"]["met1"])
    s = sorted(g["terminals"]["S"]["shapes"]["met1"])
    assert len(d) == 2 and len(s) == 2 and s[0][2] < d[0][0] < s[1][0]          # S D S D
    y = 2.0
    out = call("route", layer="met2", **{"from": [-1.5, y], "to": f"{pid}:D"})
    assert len(out["points"]) == 2 and out["points"][-1][1] == y, out             # straight in
    assert out["vias"][0].get("pads") == "turned along what it lands on", out
    klayout, magic = _drc(srv, ctx.work, "t")
    assert klayout["total"] == 0 and magic["total"] == 0, (klayout, magic)
    shutil.rmtree(root)


# ------------------------------------------------------------------------------------------ vias Magic refuses


@needs_pcells
def test_vias_on_a_mim_capacitors_own_contacts_or_half_on_a_cells_via_are_refused_as_magic_refuses_them():
    """Magic reads a via as a contact (the cut grown by its enclosure) and refuses one on a MIM's own via3 array (a
    via4 on its top plate: 'Can't overlap those layers') or abutting / half on a via of its kind in a subcell. Those
    are refused here; a via4 on the bottom plate's strip (an ordinary via3 under it: they stack), one on the top plate
    led out past the capm, and one right on a subcell's via pass Magic as they pass KLayout."""
    root, srv, ctx = _server()
    call = lambda tool, **a: _call(ctx, tool, lib="l", cell="m", **a)
    refused = lambda tool, **a: _refused(ctx, tool, lib="l", cell="m", **a)
    call("create_cell", views=["layout"])
    call("place_pcell", pcell="mim_cap", x=0, y=0, params={"type": "sky130_fd_pr__model__cap_mim", "w": 4, "l": 4})
    why = refused("add_via", x=2.0, y=2.0, from_layer="met4", to_layer="met5")
    assert "capacitor's capm" in why and "Can't overlap those layers" in why, why
    assert "capacitor" in refused("route", layer="met5", **{"from": [2.0, 2.0], "to": [9, 2.0]})
    call("add_via", x=5.4, y=2.0, from_layer="met4", to_layer="met5")             # on the bottom plate's strip
    call("add_box", layer="met4", box=[1.5, 3.6, 2.5, 6.4])                        # the top plate led out north
    call("add_via", x=2.0, y=5.6, from_layer="met4", to_layer="met5")
    _call(ctx, "create_cell", lib="l", cell="v", views=["layout"])
    _call(ctx, "add_via", lib="l", cell="v", x=0, y=0, from_layer="met3", to_layer="met4")
    _call(ctx, "create_cell", lib="l", cell="v4", views=["layout"])                # a 2x2 via3 array: one contact
    _call(ctx, "add_via", lib="l", cell="v4", x=0, y=0, from_layer="met3", to_layer="met4", cols=2, rows=2)
    call("place_cell", child="v", x=10, y=0)
    call("place_cell", child="v4", x=14, y=0)
    why = refused("add_via", x=10.1, y=0, from_layer="met3", to_layer="met4")
    assert "abuts or partly overlaps a via3 of a placed cell" in why, why
    why = refused("add_via", x=13.8, y=-0.2, from_layer="met3", to_layer="met4")   # right on one cut of the array
    assert "abuts or partly overlaps a via3 of a placed cell" in why and "the same array" in why, why
    assert "partly overlaps" in refused("add_via", x=10.2, y=0.2, from_layer="met3", to_layer="met4", cols=2, rows=2)
    call("add_via", x=10, y=0, from_layer="met3", to_layer="met5")                 # right on it, and a via4 over it
    call("add_via", x=14, y=0, from_layer="met3", to_layer="met4", cols=2, rows=2) # the same array exactly
    klayout, magic = _drc(srv, ctx.work, "m")
    assert klayout["total"] == 0 and magic["total"] == 0, (klayout, magic)
    # what was refused, drawn box by box past the checks: Magic refuses it as the refusals say, KLayout passes it
    for bad, child, boxes in (("bad_mim", None, [("via4", [1.6, 1.6, 2.4, 2.4]), ("met5", [1.0, 1.0, 3.0, 3.0])]),
                              ("bad_cut", "v4", [("via3", [13.7, -0.3, 13.9, -0.1]), ("met3", [13.6, -0.4, 14.0, 0.0]),
                                                 ("met4", [13.6, -0.4, 14.0, 0.0])])):
        _call(ctx, "create_cell", lib="l", cell=bad, views=["layout"])
        if child:
            _call(ctx, "place_cell", lib="l", cell=bad, child=child, x=14, y=0)
        else:
            _call(ctx, "place_pcell", lib="l", cell=bad, pcell="mim_cap", x=0, y=0,
                  params={"type": "sky130_fd_pr__model__cap_mim", "w": 4, "l": 4})
            x = boxes[0][1]
            assert "Can't overlap those layers" in _refused(ctx, "add_via", lib="l", cell=bad, x=(x[0] + x[2]) / 2,
                                                            y=(x[1] + x[3]) / 2, from_layer="met4", to_layer="met5")
        for layer, b in boxes:
            _call(ctx, "add_box", lib="l", cell=bad, layer=layer, box=b)
        klayout, magic = _drc(srv, ctx.work, bad)
        said = " ".join(c_["description"] for c_ in magic["categories"])
        assert klayout["total"] == 0 and magic["total"] > 0, (bad, klayout, magic)
        assert ("Can't overlap those layers" if bad == "bad_mim" else "can't abut or partially overlap") in said, (bad, magic)
    shutil.rmtree(root)


# ------------------------------------------------------------------------------------------ devices in pieces


def _pieces(ctx, at=((4, 0), (6, 0))):
    """XMS: an nfet in two fingers placed as one PCell (nf 2), XMD: one placed as two place_pcell pieces (at)."""
    call = lambda tool, **a: _call(ctx, tool, lib="l", cell="p", **a)
    call("create_cell", views=["schematic", "layout"])
    call("add_instance", symbol="sky130_fd_pr/nfet_01v8.sym", x=0, y=0, params={"name": "XMS", "W": "8", "L": "0.15", "nf": "2"})
    call("add_instance", symbol="sky130_fd_pr/nfet_01v8.sym", x=300, y=0, params={"name": "XMD", "W": "4", "L": "0.15"})
    call("connect_nets", nets={"a": ["XMS:D", "XMD:D"], "g": ["XMS:G", "XMD:G"],
                               "vss": ["XMS:S", "XMS:B", "XMD:S", "XMD:B"]}, style="label")
    call("place_pcell", pcell="nfet", name="XMS", x=0, y=0, params={"type": NFET, "w": 4, "l": 0.15, "nf": 2})
    for x, y in at:
        call("place_pcell", pcell="nfet", name="XMD", x=x, y=y, params={"type": NFET, "w": 2, "l": 0.15})
    return call


@needs_pcells
def test_a_multi_finger_pcell_has_terminals_and_a_device_in_pieces_is_one_and_each_piece():
    root, srv, ctx = _server()
    call = _pieces(ctx)
    s = call("get_device_geometry", instance="XMS", layers=["li1"])
    assert s["device"] == {"kind": "mos", "class": "nmos", "W": 8.0, "L": 0.15, "fingers": 2}, s
    assert s["terminals"]["S"]["schematic_net"] == "vss" and len(s["terminals"]["S"]["shapes"]["li1"]) == 2   # the outer strips
    assert len(s["terminals"]["D"]["shapes"]["li1"]) == 1 and len(s["terminals"]["S"]["nets"]) == 2
    assert "not joined yet" in s["terminals_split"]
    d = call("get_device_geometry", instance="XMD", layers=["li1"])
    assert [p["bbox"][0] for p in d["pieces"]] == [3.875, 5.875] and d["bbox"] == [3.875, -0.2, 6.875, 2.54]
    assert d["device"]["W"] == 4.0 and "2 pieces" in d["note"]
    assert len(d["terminals"]["D"]["shapes"]["li1"]) == 2                       # of both pieces
    one = call("get_device_geometry", instance=d["pieces"][1]["id"].replace("@6,0", "@6.000,0"), layers=["li1"])
    assert one["bound_to"] == "XMD" and one["bbox"] == d["pieces"][1]["bbox"] and one["device"]["W"] == 2.0
    assert len(one["terminals"]["D"]["shapes"]["li1"]) == 1
    assert "no layout instance bound to XMX" in _refused(ctx, "get_device_geometry", lib="l", cell="p", instance="XMX")
    # routes to the fingers' terminals and to one piece's
    out = call("route", layer="met2", **{"from": "XMS:D", "to": f"{d['pieces'][0]['id']}:D"})
    assert out["net"] == "a"
    assert call("route", layer="met2", **{"from": "XMD:S", "to": "XMS:S"}, dry_run=True)["net"] == "vss"
    # the pieces' gates joined: one net, XMD:G named on it once (not once per piece)
    call("route", layer="met3", **{"from": f"{d['pieces'][0]['id']}:G", "to": f"{d['pieces'][1]['id']}:G"})
    g = call("get_device_geometry", instance="XMD", layers=["li1"])["terminals"]["G"]
    assert "nets" not in g and g["net"].count("XMD:G") == 1, g
    # a ring round both: the extent of all their pieces
    ring = call("guard_ring", kind="ptap", around=["XMS", "XMD"], net="vss")
    inner = ring["inner"]
    assert inner[0] < -0.125 and inner[2] > 6.875 and ring["encloses"] == ["XMS", "XMD"], ring
    klayout, magic = _drc(srv, ctx.work, "p")
    assert klayout["total"] == 0, klayout
    shutil.rmtree(root)


@needs_pcells
def test_a_ring_round_a_device_in_pieces_encloses_each_and_a_terminal_in_strips_is_joined_strip_by_strip():
    """XMD's pieces one above the other, apart from XMS: a ring round XMD alone encloses both (not the last piece
    placed). Routing XMS:S to XMD:S again goes from the strips the first route did not join to those it did not, till
    all are one net, then is refused."""
    root, srv, ctx = _server()
    call = _pieces(ctx, at=((9, 0), (9, 5)))
    d = call("get_device_geometry", instance="XMD", layers=["li1"])
    boxes = [p["bbox"] for p in d["pieces"]]
    assert len(boxes) == 2 and boxes[0][3] < boxes[1][1], boxes
    ring = call("guard_ring", kind="ptap", around=["XMD"], net="vss", dry_run=True)
    inner = ring["inner"]
    assert all(inner[0] < b[0] and inner[1] < b[1] and inner[2] > b[2] and inner[3] > b[3] for b in boxes), (ring, boxes)
    s = call("get_device_geometry", instance="XMS", layers=["li1"])["terminals"]["S"]
    assert len(s["nets"]) == 2
    first = call("route", layer="met2", **{"from": "XMS:S", "to": "XMD:S"})
    second = call("route", layer="met2", **{"from": "XMS:S", "to": "XMD:S"})
    assert second["points"] != first["points"], (first, second)
    assert second["points"][0][0] != first["points"][0][0], (first, second)            # the other S strip of XMS
    assert abs(second["points"][-1][1] - first["points"][-1][1]) > 2, (first, second)  # the other piece of XMD
    s = call("get_device_geometry", instance="XMS", layers=["li1"])["terminals"]["S"]
    assert len(s["nets"]) == 2, s                      # each S strip joined to a piece of XMD: still two nets
    third = call("route", layer="met2", **{"from": "XMS:S", "to": "XMD:S"})      # from one of them to the other
    assert third["points"] not in (first["points"], second["points"]), third
    s = call("get_device_geometry", instance="XMS", layers=["li1"])
    assert "nets" not in s["terminals"]["S"] and "vss" in s["terminals"]["S"]["net"], s
    assert "one net already" in _refused(ctx, "route", lib="l", cell="p", layer="met2", **{"from": "XMS:S", "to": "XMD:S"})
    klayout, magic = _drc(srv, ctx.work, "p")
    assert klayout["total"] == 0, klayout
    shutil.rmtree(root)


# ------------------------------------------------------------------------------------------ unused generated cells


@needs_pcells
def test_unused_generated_cells_are_left_out_of_the_checks_deleted_on_request_and_come_back_with_a_restore():
    root, srv, ctx = _server()
    call = lambda tool, **a: _call(ctx, tool, lib="l", cell="u", **a)
    call("create_cell", views=["schematic", "layout"])
    call("add_instance", symbol="sky130_fd_pr/nfet_01v8.sym", x=0, y=0, params={"name": "XM", "W": "4", "L": "0.15"})
    call("connect_nets", nets={"d": ["XM:D"], "g": ["XM:G"], "vss": ["XM:S", "XM:B"]}, style="label")
    call("sync_layout")
    db = srv.db(ctx.work, "l")
    first = {i["cell"] for i in db.instances("u")}
    call("sync_layout", layout={"XM": {"nf": 2}})                       # a new variant; the old one is left over
    probe = call("place_pcell", pcell="nfet", x=20, y=0, params={"type": NFET, "w": 1, "l": 0.15})
    call("delete_instances", instances=[probe["id"]])
    gen = sdl.generated_cells(db.layout)
    unused = sdl.unused_generated(db.layout)
    used = {i["cell"] for i in db.instances("u")}
    assert first <= set(unused) and probe["cell"] in unused and not used & set(unused), (unused, used)
    assert all(n in gen for n in unused) and "u" not in gen
    assert unused.index(next(iter(first))) < max(unused.index(n) for n in unused)   # the wrapper before what it holds
    # the checks leave them out, and run_checks says how many
    meta = ctx.cs._load(ctx.project, ctx.changeset)
    rows = {(r["lib"], r["cell"]) for r in ctx.cs._checks(ctx.project, meta)}
    assert ("l", "u") in rows and not rows & {("l", n) for n in unused}, rows
    assert sorted(c for _, c in ctx.cs.unused_generated(ctx.project, meta)) == sorted(unused)
    # refused: a cell still placed, a cell of the designer's
    assert "placed in u" in _refused(ctx, "delete_unused_cells", lib="l", cells=sorted(used)[:1])
    assert "not a cell Undine generated" in _refused(ctx, "delete_unused_cells", lib="l", cells=["u"])
    dry = _call(ctx, "delete_unused_cells", lib="l", dry_run=True)
    assert dry["would_delete"] == unused and all(db.layout.cell(n) is not None for n in unused)
    mark = _call(ctx, "get_changeset")["restore_points"][0]["after"]
    content = {n: layout._content(db.layout, db.cell(n)) for n in unused}
    meta_of = {n: sorted((m.name, m.value) for m in db.cell(n).each_meta_info()) for n in unused}
    out = _call(ctx, "delete_unused_cells", lib="l")
    assert out["deleted"] == unused and all(db.layout.cell(n) is None for n in unused)
    assert not sdl.unused_generated(db.layout) and {i["cell"] for i in db.instances("u")} == used
    db.save()
    assert not any((db.lib.path / f"{n}.oas").exists() or any(db.lib.path.glob(f"{n}.*")) for n in unused)
    steps = _call(ctx, "get_changeset")["changes"]
    assert steps[-1]["op"] == "delete_unused_cells"
    said = [x for v in steps[-1]["cells"].values() for x in v]           # each cell one line, its content counted
    assert all(x.startswith("removed layout cell ") and " (its " in x for x in said), said
    assert sorted(x.split()[3] for x in said) == sorted(unused), said
    # one operation: one undo brings every cell back (a wrapper with the variant it holds), one redo takes them out
    assert len([n for n in unused if n.endswith(sdl.TIE) or n.endswith(sdl.STRAP)]) >= 1, unused
    db.undo()
    assert {n: layout._content(db.layout, db.cell(n)) for n in unused} == content
    db.redo()
    assert all(db.layout.cell(n) is None for n in unused)
    done = _call(ctx, "get_changeset")["restore_points"][0]["after"]
    # recorded with their content: a restore brings each back as it was (its meta too)
    _call(ctx, "restore_changeset", to=mark)
    db = srv.db(ctx.work, "l")
    assert all(db.layout.cell(n) is not None for n in unused)
    assert {n: layout._content(db.layout, db.cell(n)) for n in unused} == content
    assert {n: sorted((m.name, m.value) for m in db.cell(n).each_meta_info()) for n in unused} == meta_of
    assert sdl.unused_generated(db.layout) == unused
    # and forward again, to right after delete_unused_cells: they go again (not left empty)
    _call(ctx, "restore_changeset", to=done)
    db = srv.db(ctx.work, "l")
    assert all(db.layout.cell(n) is None for n in unused) and not sdl.unused_generated(db.layout)
    assert {i["cell"] for i in db.instances("u")} == used
    shutil.rmtree(root)


def test_a_dropped_cell_inverts_to_its_return_and_other_cell_effects_stay_as_they_were():
    kept = {"k": "c", "do": "del", "cell": "a", "drop": True, "kept": True, "src": {}, "meta": [["m", "v"]]}
    shape = {"k": "s", "do": "del", "cell": "a", "layer": "68/20", "t": "box", "s": "(0,0;1,1)"}
    inv = oplog.invert([shape, kept])
    assert inv[0]["k"] == "c" and inv[0]["do"] == "add" and inv[0]["meta"] == [["m", "v"]] and inv[1]["do"] == "add"
    assert oplog.invert([{"k": "c", "do": "del", "cell": "a", "drop": True}]) == []      # a restore's drop: as before
    assert oplog.invert([{"k": "c", "do": "add", "cell": "a"}]) == []
    back = oplog.invert(inv)                                                     # a restore that brought it back
    assert back[-1]["k"] == "c" and back[-1]["do"] == "del" and back[-1]["drop"] and back[0]["do"] == "del", back


@needs_pcells
def test_a_dropped_cell_comes_back_with_undo_and_goes_again_with_redo():
    root, srv, ctx = _server()
    _call(ctx, "create_cell", lib="l", cell="w", views=["layout"])
    probe = _call(ctx, "place_pcell", lib="l", cell="w", pcell="nfet", x=0, y=0, params={"type": NFET, "w": 1, "l": 0.15})
    _call(ctx, "delete_instances", lib="l", cell="w", instances=[probe["id"]])
    db = srv.db(ctx.work, "l")
    before = layout._content(db.layout, db.cell(probe["cell"]))
    _call(ctx, "delete_unused_cells", lib="l", cells=[probe["cell"]])
    assert db.layout.cell(probe["cell"]) is None
    db.undo()
    assert layout._content(db.layout, db.cell(probe["cell"])) == before
    db.redo()
    assert db.layout.cell(probe["cell"]) is None
    shutil.rmtree(root)


# ------------------------------------------------------------------------------------------ restore points


@pytest.mark.skipif(SKY is None, reason="sky130A is not installed")
def test_every_restore_point_is_reachable_in_pages_or_all_at_once():
    root, srv, ctx = _server()
    _call(ctx, "create_cell", lib="l", cell="r", views=["layout"])
    for i in range(16):
        _call(ctx, "add_box", lib="l", cell="r", layer="met1", box=[i, 0, i + 0.5, 0.5])
    g = _call(ctx, "get_changeset")
    pts = g["restore_points"]
    assert len(pts) == 12 and g["restore_points_more"]["older"] == 5         # 17 operations: create_cell and 16 boxes
    older = _call(ctx, "get_changeset", restore_before=pts[-1]["after"])
    assert len(older["restore_points"]) == 5 and "restore_points_more" not in older
    assert all(p["after"] < pts[-1]["after"] for p in older["restore_points"])
    every = _call(ctx, "get_changeset", restore_points="all")["restore_points"]
    assert [p["after"] for p in every] == [p["after"] for p in pts + older["restore_points"]]
    assert len(_call(ctx, "get_changeset", restore_points=3)["restore_points"]) == 3
    assert "restore_points is how many" in _refused(ctx, "get_changeset", restore_points=0)
    first = every[-1]["after"]                                                 # right after create_cell
    _call(ctx, "restore_changeset", to=first)
    assert srv.db(ctx.work, "l").cell("r").shapes(srv.db(ctx.work, "l").layout.find_layer(68, 20)).is_empty()
    shutil.rmtree(root)


@pytest.mark.skipif(SKY is None, reason="sky130A is not installed")
def test_restore_points_and_operations_agree_and_a_running_batch_is_not_listed_yet():
    """The newest restore point is where the changeset is now (restore_point_now), whatever the numbering skipped (an
    undone operation, a batch taken back); while a batch is still being applied (its client may be gone), get_changeset
    says how far, and neither counts nor lists its operations nor gives it a restore point until it ends."""
    root, srv, ctx = _server()
    _call(ctx, "create_cell", lib="l", cell="r", views=["layout"])
    _call(ctx, "add_box", lib="l", cell="r", layer="met1", box=[0, 0, 0.5, 0.5])
    _refused(ctx, "batch", ops=[{"tool": "add_box", "args": {"lib": "l", "cell": "r", "layer": "met1", "box": [2, 0, 2.5, 0.5]}},
                                {"tool": "add_box", "args": {"lib": "l", "cell": "nope", "layer": "met1", "box": [0, 0, 1, 1]}}],
             reason="taken back")
    g = _call(ctx, "get_changeset")
    assert g["restore_point_now"] == g["restore_points"][0]["after"] and g["operations"] == 2
    assert "operations counts those in effect" in g["numbering"] and "in_progress" not in g
    before = g["restore_point_now"]
    seen = {}

    def peek(c, a):                                    # another agent reads the changeset while the batch runs
        seen.update(_call(T.ToolCtx(srv, "p", actor="agent:other"), "get_changeset", id=ctx.changeset))
        return {"ok": True}
    T.BY_NAME["peek"] = {"name": "peek", "write": False, "fn": peek, "input_schema": {"type": "object"}}
    try:
        _call(ctx, "batch", ops=[{"tool": "add_box", "args": {"lib": "l", "cell": "r", "layer": "met1", "box": [i, 2, i + 0.5, 2.5]}}
                                 for i in range(3)] + [{"tool": "peek", "args": {}}], reason="three boxes")
    finally:
        del T.BY_NAME["peek"]
    assert seen["in_progress"]["what"] == "batch" and seen["in_progress"]["done"] == 3 and seen["in_progress"]["of"] == 4
    assert seen["operations"] == 2 and seen["restore_point_now"] == before
    assert [p["after"] for p in seen["restore_points"]][0] == before
    after = _call(ctx, "get_changeset")
    assert "in_progress" not in after and after["operations"] == 5 and after["restore_point_now"] > before
    assert after["restore_points"][0]["what"] == "add_box" and after["restore_points"][0]["after"] == after["restore_point_now"]
    assert not srv.batch_progress and not srv.batching
    shutil.rmtree(root)
