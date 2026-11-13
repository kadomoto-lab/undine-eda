"""Cells and instances of a changeset as a layout designer clears up and re-arranges them (agent_tools): delete_cell
takes a cell's layout, schematic and symbol out (refused while placed; recorded with the content, so undo and a restore
bring it back; applying removes it from the project), delete_unused_cells takes only the unused generated cells this
changeset made unless asked for the baseline's too, get_changeset lists every cell the changeset made (the _tie / _par
cells round a device too), swap_instances exchanges two instances' places or bindings in one recorded step, and the
drawing tools extract a layout's nets once per state of it."""
import json
import shutil
import sys
from pathlib import Path

import klayout.db as kdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine.agents import agent_tools as T  # noqa: E402
from undine.layouts import connectivity, layout, layoutstore, sdl  # noqa: E402
from undine.schematics import xschem  # noqa: E402
import test_changeset as tc  # noqa: E402
import test_changeset_git as tg  # noqa: E402
from test_layout_drawing import SKY, needs_pcells, needs_sky  # noqa: E402

NFET = "sky130_fd_pr__nfet_01v8"
BLK_SYM = 'v {}\nK {type=subcircuit\nformat="@name @pinlist @symname"\ntemplate="name=X1"}\n' \
          'B 5 -2.5 -2.5 2.5 2.5 {name=a dir=inout}\n'


def _call(ctx, tool, **a):
    res, err = T.call(ctx, tool, {"reason": "x", **a})
    assert not err, res
    return json.loads(res)


def _refused(ctx, tool, **a) -> str:
    res, err = T.call(ctx, tool, {"reason": "x", **a})
    assert err, res
    return res


def _project():
    """tc.setup's project (l: r.sym placed in top.sch, layout cell top) with a pilot layout cell of one box and a
    block blk (schematic and symbol) that top.sch places, everything committed."""
    root, srv, cs = tc.setup()
    lib = srv.project("p").library("l")
    ly = kdb.Layout()
    ly.dbu = 0.001
    ly.create_cell("top")
    ly.create_cell("pilot").shapes(ly.layer(68, 20)).insert(kdb.Box(0, 0, 1000, 500))
    layoutstore.write(lib.path, ly, {})
    (lib.path / "blk.sch").write_text(xschem.dump(xschem.XFile(version="xschem version=3.4.5 file_version=1.2\n")))
    (lib.path / "blk.sym").write_text(BLK_SYM)
    top = xschem.parse((lib.path / "top.sch").read_text())
    top.comps.append(xschem.XComp("blk.sym", 100, 0, 0, 0, {"name": "X1"}))
    (lib.path / "top.sch").write_text(xschem.dump(top))
    p = root / "p"
    from undine.__main__ import git_setup
    git_setup(p, say=None)
    tg.git(p, "add", "-A")
    tg.git(p, "commit", "-qm", "the project")
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    _call(ctx, "open_changeset", title="tidy", reason="r")
    return root, srv, ctx, p


def test_delete_cell_refuses_what_is_placed_and_takes_the_rest_out_recorded():
    root, srv, ctx, p = _project()
    work, lib = ctx.work, srv.project(ctx.work).library("l")
    # placed: the symbol in top.sch (r, blk), the schematic while its placed symbol stays, a layout cell placed in top
    assert "its symbol is placed in l/top" in _refused(ctx, "delete_cell", lib="l", cell="r")
    assert "its symbol stays and is placed in l/top" in _refused(ctx, "delete_cell", lib="l", cell="blk", views=["schematic"])
    _call(ctx, "create_cell", lib="l", cell="unit", views=["layout"])
    _call(ctx, "place_cell", lib="l", cell="top", child="unit", x=0, y=0)
    assert "its layout is placed in top" in _refused(ctx, "delete_cell", lib="l", cell="unit")
    assert "has no schematic view" in _refused(ctx, "delete_cell", lib="l", cell="pilot", views=["schematic"])
    assert "read-only" not in _refused(ctx, "delete_cell", lib="l", cell="nothing")
    # a cell this changeset made, every view of it, and a baseline layout cell
    _call(ctx, "create_cell", lib="l", cell="tmp", views=["schematic", "layout"])
    _call(ctx, "create_cell", lib="l", cell="tmp", views=["symbol"])
    texts = {v: (lib.path / f"tmp.{v}").read_text() for v in ("sch", "sym")}
    made = _call(ctx, "get_changeset")["new_cells"]
    assert made["l/tmp"] == ["layout", "schematic", "symbol"] and made["l/unit"] == ["layout"] and "l/pilot" not in made
    mark = _call(ctx, "get_changeset")["restore_point_now"]
    dry = _call(ctx, "delete_cell", lib="l", cell="tmp", dry_run=True)
    assert dry["dry_run"] and dry["views"] == ["layout", "schematic", "symbol"] and (lib.path / "tmp.sch").is_file()
    out = _call(ctx, "delete_cell", lib="l", cell="tmp")
    assert out["views"] == ["layout", "schematic", "symbol"] and out["made_by_this_changeset"] == out["views"]
    assert not (lib.path / "tmp.sch").exists() and not (lib.path / "tmp.sym").exists()
    assert not (lib.path / "tmp.netlist.yaml").exists()
    assert srv.db(work, "l").layout.cell("tmp") is None
    out = _call(ctx, "delete_cell", lib="l", cell="pilot")
    assert out["views"] == ["layout"] and out["made_by_this_changeset"] == []
    g = _call(ctx, "get_changeset")
    assert "l/tmp" not in g["new_cells"]
    said = [x for st in g["changes"] if st["op"] == "delete_cell" for v in st.get("cells", {}).values() for x in v]
    assert any(x.startswith("removed layout cell pilot (its 1 shape") for x in said), said
    assert any(x == "removed file (schematic / symbol)" for x in said), said
    # back with a restore, exactly as they were; forward again, gone again
    done = g["restore_point_now"]
    _call(ctx, "restore_changeset", to=mark)
    assert {v: (lib.path / f"tmp.{v}").read_text() for v in ("sch", "sym")} == texts
    db = srv.db(work, "l")
    assert db.layout.cell("tmp") is not None and db.layout.cell("pilot").bbox() == kdb.Box(0, 0, 1000, 500)
    _call(ctx, "restore_changeset", to=done)
    db = srv.db(work, "l")
    assert db.layout.cell("pilot") is None and db.layout.cell("tmp") is None and not (lib.path / "tmp.sch").exists()
    # undo / redo of a deleted file in its editor
    _call(ctx, "delete_instances", lib="l", cell="top", child="unit")
    _call(ctx, "delete_cell", lib="l", cell="unit")
    # applied: the files are gone from the project, the rest is as it was
    srv.db(work, "l").save()
    assert ctx.cs.apply("p", ctx.changeset, "user")["status"] == "applied"
    plib = srv.project("p").library("l").path
    assert not (plib / "pilot.oas").exists() and not (plib / "tmp.sch").exists() and not (plib / "unit.oas").exists()
    assert (plib / "blk.sym").is_file() and (plib / "top.sch").is_file() and (plib / "r.sym").is_file()
    shutil.rmtree(root)


def test_a_deleted_schematic_comes_back_with_undo_and_goes_again_with_redo():
    root, srv, ctx, p = _project()
    lib = srv.project(ctx.work).library("l")
    _call(ctx, "create_cell", lib="l", cell="note", views=["schematic"])
    text = (lib.path / "note.sch").read_text()
    _call(ctx, "delete_cell", lib="l", cell="note")
    ed = srv.editor(ctx.work, "l", "note", "schematic")
    assert ed.undo() and (lib.path / "note.sch").read_text() == text
    assert ed.redo() and not (lib.path / "note.sch").exists()
    shutil.rmtree(root)


def test_a_point_before_a_deletion_is_reached_after_the_name_is_made_again():
    """Deleting a pilot and starting it again under its name: a restore to before the deletion takes the new one out
    (its creation inverted, after its edits) and brings the deleted one back as it was; restoring to after the remake
    gives the new one again. A schematic and a layout cell (with a layout save between deletion and remake)."""
    root, srv, ctx, p = _project()
    work, lib = ctx.work, srv.project(ctx.work).library("l")
    _call(ctx, "create_cell", lib="l", cell="tmp", views=["schematic"])
    _call(ctx, "add_wire", lib="l", cell="tmp", x1=0, y1=0, x2=100, y2=0, label="old")
    old = (lib.path / "tmp.sch").read_text()
    mark = _call(ctx, "get_changeset")["restore_point_now"]
    _call(ctx, "delete_cell", lib="l", cell="tmp")
    _call(ctx, "create_cell", lib="l", cell="tmp", views=["schematic"])
    _call(ctx, "add_wire", lib="l", cell="tmp", x1=0, y1=50, x2=100, y2=50, label="new")
    new = (lib.path / "tmp.sch").read_text()
    assert new != old
    after = _call(ctx, "get_changeset")["restore_point_now"]
    _call(ctx, "restore_changeset", to=mark)
    assert (lib.path / "tmp.sch").read_text() == old
    _call(ctx, "restore_changeset", to=after)
    assert (lib.path / "tmp.sch").read_text() == new
    _call(ctx, "restore_changeset", to=mark)
    assert (lib.path / "tmp.sch").read_text() == old
    # the layout: pilot (the baseline's, one box) deleted, the layout saved, pilot made again with another box
    mark = _call(ctx, "get_changeset")["restore_point_now"]
    _call(ctx, "delete_cell", lib="l", cell="pilot")
    srv.db(work, "l").save()
    _call(ctx, "create_cell", lib="l", cell="pilot", views=["layout"])
    _call(ctx, "add_box", lib="l", cell="pilot", layer="68/20", box=[0, 0, 2, 2])
    after = _call(ctx, "get_changeset")["restore_point_now"]
    _call(ctx, "restore_changeset", to=mark)
    db = srv.db(work, "l")
    c = db.layout.cell("pilot")
    assert c is not None and c.bbox() == kdb.Box(0, 0, 1000, 500)
    assert [s.box for s in c.shapes(db.layout.layer(68, 20)).each()] == [kdb.Box(0, 0, 1000, 500)]
    _call(ctx, "restore_changeset", to=after)
    db = srv.db(work, "l")
    assert db.layout.cell("pilot").bbox() == kdb.Box(0, 0, 2000, 2000)
    assert len(list(db.layout.cell("pilot").shapes(db.layout.layer(68, 20)).each())) == 1
    _call(ctx, "restore_changeset", to=mark)
    assert srv.db(work, "l").layout.cell("pilot").bbox() == kdb.Box(0, 0, 1000, 500)
    shutil.rmtree(root)


def test_a_deleted_layout_cell_is_gone_from_list_cells_and_its_name_free_before_a_save():
    """list_cells and create_cell read the layout the server holds, not the cell files the next save writes."""
    root, srv, ctx, p = _project()
    views = lambda: {x["cell"]: x["views"] for x in _call(ctx, "list_cells", lib="l")["l"]}
    assert views()["pilot"] == ["layout"]
    _call(ctx, "delete_cell", lib="l", cell="pilot")
    assert "pilot" not in views()
    _call(ctx, "create_cell", lib="l", cell="pilot", views=["layout"])
    _call(ctx, "create_cell", lib="l", cell="fresh", views=["layout"])
    assert views()["pilot"] == ["layout"] and views()["fresh"] == ["layout"]
    assert "already has a layout" in _refused(ctx, "create_cell", lib="l", cell="fresh", views=["layout"])
    shutil.rmtree(root)


def test_a_delete_cell_that_fails_part_way_keeps_every_view(monkeypatch):
    """The layout and the symbol go before the schematic: when the schematic's deletion fails, they come back, and
    the cell keeps all three views."""
    from undine.schematics.schedit import SchEditor
    root, srv, ctx, p = _project()
    work, lib = ctx.work, srv.project(ctx.work).library("l")
    _call(ctx, "create_cell", lib="l", cell="tmp", views=["schematic", "layout"])
    _call(ctx, "add_box", lib="l", cell="tmp", layer="68/20", box=[0, 0, 1, 1])
    _call(ctx, "create_cell", lib="l", cell="tmp", views=["symbol"])
    texts = {v: (lib.path / f"tmp.{v}").read_text() for v in ("sch", "sym")}
    real = SchEditor.delete_file

    def failing(self):
        if self.view_kind == "schematic":
            raise ValueError("conflict: tmp.sch is busy")
        return real(self)
    monkeypatch.setattr(SchEditor, "delete_file", failing)
    msg = _refused(ctx, "delete_cell", lib="l", cell="tmp")
    assert "tmp.sch is busy" in msg and "nothing was deleted" in msg, msg
    assert {v: (lib.path / f"tmp.{v}").read_text() for v in ("sch", "sym")} == texts
    db = srv.db(work, "l")
    assert db.layout.cell("tmp") is not None and db.layout.cell("tmp").bbox() == kdb.Box(0, 0, 1000, 1000)
    shutil.rmtree(root)


# ------------------------------------------------------------------------------------------ swap_instances


def _units(ctx):
    _call(ctx, "create_cell", lib="l", cell="unit", views=["layout"])
    _call(ctx, "add_box", lib="l", cell="unit", layer="68/20", box=[0, 0, 1, 0.5])
    _call(ctx, "create_cell", lib="l", cell="wide", views=["layout"])
    _call(ctx, "add_box", lib="l", cell="wide", layer="68/20", box=[0, 0, 2, 0.5])
    _call(ctx, "place_cell", lib="l", cell="top", child="unit", x=0, y=0, name="C1")
    _call(ctx, "place_cell", lib="l", cell="top", child="unit", x=10, y=0, rot=90, mirror=True, name="C2")
    _call(ctx, "place_cell", lib="l", cell="top", child="wide", x=20, y=0)


def test_two_bound_units_swap_places_in_one_step_and_bindings_without_moving():
    root, srv, ctx, p = _project()
    _units(ctx)
    db = srv.db(ctx.work, "l")

    def where():
        return {i.get("name") or i["cell"]: i["trans"] for i in db.instances("top")}
    before = where()
    pts = _call(ctx, "get_changeset")["restore_point_now"]
    out = _call(ctx, "swap_instances", lib="l", cell="top", instances=["C1", "C2"])
    now = where()
    assert now["C1"] == before["C2"] and now["C2"] == before["C1"] and now["wide"] == before["wide"], (before, now)
    g = _call(ctx, "get_changeset")
    last = g["changes"][-1]
    assert last["op"] == "swap_instances" and g["restore_points"][1]["after"] == pts, g["restore_points"][:2]
    said = next(iter(last["cells"].values()))
    assert sorted(x.split(" from ")[0] for x in said) == ["move instance C1 (unit)", "move instance C2 (unit)"], said
    assert {i["name"] for i in out["instances"]} == {"C1", "C2"}
    # by ids too, and back: as before
    ids = {i["name"]: i["id"] for i in db.instances("top") if i.get("name")}
    _call(ctx, "swap_instances", lib="l", cell="top", instances=[ids["C1"], ids["C2"]])
    assert where() == before
    # bindings: each stays, the names change places
    _call(ctx, "swap_instances", lib="l", cell="top", instances=["C1", "C2"], what="binding")
    now = where()
    assert now["C1"] == before["C2"] and now["C2"] == before["C1"]
    # refused: one instance twice, a binding between two cells, neither bound
    cur = {i["name"]: i["id"] for i in db.instances("top") if i.get("name")}
    assert "are one" in _refused(ctx, "swap_instances", lib="l", cell="top", instances=["C1", cur["C1"]])
    wide = next(i["id"] for i in db.instances("top") if i["cell"] == "wide")
    assert "only instances of one cell" in _refused(ctx, "swap_instances", lib="l", cell="top", instances=["C1", wide],
                                                     what="binding")
    assert "instances is two" in _refused(ctx, "swap_instances", lib="l", cell="top", instances=["C1"])
    # a place swap between two cells: each takes the other's origin and orientation
    c2 = kdb.Trans.from_s(where()["C2"])
    _call(ctx, "swap_instances", lib="l", cell="top", instances=["C2", wide])
    assert kdb.Trans.from_s(where()["wide"]) == c2
    # undone as one
    db.undo()
    assert kdb.Trans.from_s(where()["C2"]) == c2
    shutil.rmtree(root)


# ------------------------------------------------------------------------------------------ generated cells


@needs_pcells
def test_unused_generated_cells_of_this_changeset_go_by_default_and_the_baselines_on_request():
    from test_layout_drawing import _server

    root, srv, ctx = _server()
    db = srv.db("p", "l")                                    # the project itself, before the changeset: the baseline
    base = sdl.pcell_variant(SKY, db, "nfet", {"type": NFET, "w": 0.5, "l": 0.15})
    db.save()
    ctx = T.ToolCtx(srv, "p", actor="agent:u")
    _call(ctx, "open_changeset", title="cells", reason="r")
    call = lambda tool, **a: _call(ctx, tool, lib="l", cell="u", **a)
    call("create_cell", views=["schematic", "layout"])
    call("add_instance", symbol="sky130_fd_pr/nfet_01v8.sym", x=0, y=0, params={"name": "XM", "W": "4", "L": "0.15"})
    call("connect_nets", nets={"d": ["XM:D"], "g": ["XM:G"], "vss": ["XM:S", "XM:B"]}, style="label")
    call("sync_layout")
    call("sync_layout", layout={"XM": {"nf": 2}})              # the first variant and the cells round it: unused
    wdb = srv.db(ctx.work, "l")
    unused = sdl.unused_generated(wdb.layout)
    assert base in unused and len(unused) >= 2, unused
    mine = [n for n in unused if n != base]
    made = _call(ctx, "get_changeset")["new_cells"]
    gen = {c.split("/", 1)[1] for c in made} - {"u"}
    wrappers = [n for n in gen if n.endswith(sdl.TIE) or n.endswith(sdl.STRAP)]
    assert wrappers and set(mine) <= gen and base not in gen, (made, unused)
    assert all(made[f"l/{n}"] == ["layout"] for n in gen) and made["l/u"] == ["layout", "schematic"]
    dry = _call(ctx, "delete_unused_cells", lib="l", dry_run=True)
    assert dry["would_delete"] == mine and dry["baseline_unused"] == 1 and dry["baseline_cells"] == [base], dry
    assert "include_baseline" in dry["baseline_note"]
    every = _call(ctx, "delete_unused_cells", lib="l", dry_run=True, include_baseline=True)
    assert sorted(every["would_delete"]) == sorted(unused) and "baseline_unused" not in every
    mark = _call(ctx, "get_changeset")["restore_point_now"]
    was = {n: layout._content(wdb.layout, wdb.layout.cell(n)) for n in mine}
    out = _call(ctx, "delete_unused_cells", lib="l")
    assert out["deleted"] == mine and wdb.layout.cell(base) is not None
    assert all(wdb.layout.cell(n) is None for n in mine)
    out = _call(ctx, "delete_unused_cells", lib="l", cells=[base])            # named: the baseline's goes too
    assert out["deleted"] == [base]
    # the first variant generated again (its cells under the names deleted): a restore to before the deletion takes
    # the new ones out as they were generated and brings the deleted ones back
    call("sync_layout", layout={"XM": {"nf": 1}})
    wdb = srv.db(ctx.work, "l")
    again = [n for n in mine if wdb.layout.cell(n) is not None]
    assert again, mine
    after = _call(ctx, "get_changeset")["restore_point_now"]
    _call(ctx, "restore_changeset", to=mark)
    wdb = srv.db(ctx.work, "l")
    assert {n: layout._content(wdb.layout, wdb.layout.cell(n)) for n in mine} == was
    _call(ctx, "restore_changeset", to=after)
    wdb = srv.db(ctx.work, "l")
    assert [n for n in mine if wdb.layout.cell(n) is not None] == again and wdb.layout.cell(base) is None
    shutil.rmtree(root)


# ------------------------------------------------------------------------------------------ nets once per state


@needs_sky
def test_the_nets_are_extracted_once_per_state_of_the_layout(monkeypatch):
    from test_layout_drawing import _server

    root, srv, ctx = _server()
    call = lambda tool, **a: _call(ctx, tool, lib="l", cell="n", **a)
    call("create_cell", views=["layout"])
    call("add_box", layer="met1", box=[0, 0, 2, 0.5])
    call("add_label", layer="met1.label", text="a", x=1, y=0.25)
    made = []
    real = connectivity.Nets.__init__

    def counting(self, *a, **k):
        made.append(1)
        real(self, *a, **k)
    monkeypatch.setattr(connectivity.Nets, "__init__", counting)
    call("add_via", x=1, y=0.25, from_layer="met1", to_layer="met2", dry_run=True)
    call("add_via", x=1, y=0.25, from_layer="met1", to_layer="met2", dry_run=True)
    assert len(made) == 1                                          # nothing changed in between
    call("add_via", x=1, y=0.25, from_layer="met1", to_layer="met2")
    assert len(made) == 1                                          # the draw after the dry run: the same state
    out = call("add_via", x=1.6, y=0.25, from_layer="met1", to_layer="met2", dry_run=True)
    assert len(made) == 2 and out["dry_run"]                       # the via drawn: a new state, extracted again
    srv.db(ctx.work, "l").undo()
    call("add_via", x=1.6, y=0.25, from_layer="met1", to_layer="met2", dry_run=True)
    assert len(made) == 3                                          # undo: another state again
    shutil.rmtree(root)
