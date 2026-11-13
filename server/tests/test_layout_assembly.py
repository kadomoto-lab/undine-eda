"""Layout assembly by agents: a library of its own, cells placed from the library, another library or the PDK's standard
cells (one array instance for a regular array), layouts imported from a generator or a LibreLane run, all in the
changeset and in its review; schematic-driven layout of standard-cell instances (the real PDK where it is installed)."""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import klayout.db as kdb  # noqa: E402
import pytest  # noqa: E402

from undine.agents import agent_tools as T  # noqa: E402
from undine.pdks import pdkprofile  # noqa: E402
from undine.pdks.pdk import default_pdk_roots, discover  # noqa: E402
from undine.store.changeset import Changesets  # noqa: E402
import test_changeset as tc  # noqa: E402


def _agent(srv):
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    assert not T.call(ctx, "open_changeset", {"title": "assemble", "reason": "r"})[1]
    return ctx


def _call(ctx, tool, **a):
    res, err = T.call(ctx, tool, {"reason": "x", **a})
    assert not err, res
    return json.loads(res)


def _layers(srv, ctx, lib):
    """Layer views for 68/20 (no PDK here: a library's views are the layers its layout has)."""
    db = srv.db(ctx.work, lib)
    db.layout.layer(68, 20)
    db.lv.add_missing_layers()
    return db


def _ops(cs, ctx):
    return [(lc["lib"], o["op"], o["cell"], [(e["k"], e["do"], e.get("child") or e.get("cell")) for e in o["effects"]])
            for lc in cs.get("p", ctx.changeset)["changes"] for o in lc["ops"]]


def _gds(path: Path, cells: dict[str, list]) -> Path:
    """A layout file: {cell: [box (dbu) on 68/20 | (child cell, x, y)]}."""
    ly = kdb.Layout()
    ly.dbu = 0.001
    li = ly.layer(68, 20)
    made = {n: ly.create_cell(n) for n in cells}
    for n, items in cells.items():
        for it in items:
            if isinstance(it[0], str):
                if it[0] not in made:                         # placed, not defined: a ghost cell in the file
                    made[it[0]] = ly.create_cell(it[0])
                    made[it[0]].ghost_cell = True
                made[n].insert(kdb.CellInstArray(made[it[0]].cell_index(), kdb.Trans(it[1], it[2])))
            else:
                made[n].shapes(li).insert(kdb.Box(*it))
    ly.write(str(path))
    return path


def test_standard_cells_are_named_by_the_profile():
    assert pdkprofile.SKY130.stdcell("sky130_fd_sc_hd__nand2_1") == ("sky130_fd_sc_hd", "libs.ref/sky130_fd_sc_hd/gds/sky130_fd_sc_hd.gds")
    assert pdkprofile.SKY130.stdcell("sky130_ef_sc_hd__decap_12")[0] == "sky130_fd_sc_hd"      # in the hd library's GDS
    assert pdkprofile.SKY130.stdcell("nfet_01v8") is None and pdkprofile.SKY130.stdcell("") is None
    assert pdkprofile.GF180.stdcell("gf180mcu_fd_sc_mcu9t5v0__inv_1")[0] == "gf180mcu_fd_sc_mcu9t5v0"
    assert pdkprofile.IHP.stdcell("sg13g2_inv_1")[0] == "sg13g2_stdcell" and pdkprofile.GENERIC.stdcell("x__y") is None


def test_create_library_is_recorded_in_the_changeset_and_applied():
    root, srv, cs = tc.setup()
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    res, err = T.call(ctx, "create_library", {"name": "cdac", "reason": "x"})
    assert err and "open_changeset" in res
    T.call(ctx, "open_changeset", {"title": "CDAC", "reason": "r"})
    for bad, msg in (({"name": "cdac"}, "reason"), ({"name": "bad name", "reason": "x"}, "invalid name"),
                     ({"name": "l", "reason": "x"}, "library l exists"), ({"name": "-x", "reason": "x"}, "invalid name")):
        res, err = T.call(ctx, "create_library", bad)
        assert err and msg in res, (bad, res)
    out = _call(ctx, "create_library", name="cdac", reason="the capacitor array is a block of its own")
    assert out["library"] == "cdac"
    assert "cdac" in srv.project(ctx.work).libraries() and "cdac" not in srv.project("p").libraries()   # the real project: not yet
    assert T.call(ctx, "create_library", {"name": "cdac", "reason": "x"})[1]                           # once
    _call(ctx, "create_cell", lib="cdac", cell="unit", views=["layout"])
    _layers(srv, ctx, "cdac")
    _call(ctx, "add_box", lib="cdac", cell="unit", layer="68/20", box=[0, 0, 1, 1])
    assert _ops(cs, ctx)[0] == ("cdac", "create_library", None, [("l", "add", None)])
    assert cs.get("p", ctx.changeset)["changes"][0]["ops"][0]["effects"] == [{"k": "l", "do": "add", "lib": "cdac"}]
    tl = cs.timeline("p", ctx.changeset)
    assert tl["ops"][0]["ops"] == ["create_library"] and tl["ops"][0]["in_effect"]
    assert tl["ops"][0]["effects"] == ["new library cdac"]
    assert any(s["op"] == "create_library" and "cdac" in s["cells"] and "its own" in s["reason"]   # the library's own
               for s in json.loads(T.call(ctx, "get_changeset", {})[0])["changes"])
    cs.apply("p", ctx.changeset, "user")
    real = srv.project("p").library("cdac")
    assert "unit" in real.layout_cells()[0]
    assert [r["op"] for r in srv.oplog("p", "cdac").read()][:2] == ["create_library", "create_cell"]
    shutil.rmtree(root)


def test_place_cells_from_the_library_and_from_another_one():
    root, srv, cs = tc.setup()
    ctx = _agent(srv)
    db = _layers(srv, ctx, "l")
    _call(ctx, "create_cell", lib="l", cell="via", views=["layout"])
    _call(ctx, "add_box", lib="l", cell="via", layer="68/20", box=[0, 0, 0.2, 0.2])
    _call(ctx, "create_library", name="m")
    _layers(srv, ctx, "m")
    for cell in ("via", "cap", "unit"):
        _call(ctx, "create_cell", lib="m", cell=cell, views=["layout"])
    _call(ctx, "add_box", lib="m", cell="via", layer="68/20", box=[0, 0, 0.2, 0.2])          # as l's via
    _call(ctx, "add_box", lib="m", cell="cap", layer="68/20", box=[0, 0, 2, 2])
    _call(ctx, "place_cell", lib="m", cell="cap", child="via", x=0.5, y=0.5)
    _call(ctx, "add_box", lib="m", cell="unit", layer="68/20", box=[0, 0, 5, 5])
    _call(ctx, "create_cell", lib="l", cell="unit", views=["layout"])
    _call(ctx, "add_box", lib="l", cell="unit", layer="68/20", box=[0, 0, 1, 1])               # not m's unit

    # a cell of the library itself; then one of another library, copied in with the cell under it (l's via: the same)
    out = _call(ctx, "place_cell", lib="l", cell="top", child="unit", x=1, y=2)
    assert out["from"] == "l" and out["bbox_um"] == [1.0, 2.0, 2.0, 3.0] and "unbound" in out["note"]
    out = _call(ctx, "place_cell", lib="l", cell="top", child="cap", child_lib="m", x=10, y=0, rot=90)
    assert out["from"] == "m" and out["bbox_um"] == [8.0, 0.0, 10.0, 2.0]
    names = {c.name for c in db.layout.each_cell()}
    assert {"cap", "via"} <= names and not any("$" in n for n in names)                      # l's via is used, not via$1
    out = _call(ctx, "place_cell", lib="l", cell="top", child="cap", child_lib="m", x=20, y=0)  # a second one: no copy
    assert out["from"] == "l (as in m)"
    res, err = T.call(ctx, "place_cell", {"lib": "l", "cell": "top", "child": "unit", "child_lib": "m", "x": 0, "y": 0, "reason": "x"})
    assert err and "of its own" in res                                                        # never m's over l's silently
    for bad, msg in (({"child": "nope"}, "no layout cell nope in l"), ({"child": "cap", "child_lib": "zz"}, "no library zz"),
                     ({"child": "unit", "rot": 45}, "rot is 0, 90"), ({"child": "unit", "columns": 4}, "pitch_x"),
                     ({"child": "unit", "columns": 2.5, "pitch_x": 1}, "whole number"), ({"child": "top"}, "recursive"),
                     ({"child": "unit", "columns": 2048, "rows": 1024, "pitch_x": 1, "pitch_y": 1}, "at most 1048576 elements"),
                     ({"child": ""}, "child is required")):
        res, err = T.call(ctx, "place_cell", {"lib": "l", "cell": "top", "x": 0, "y": 0, "reason": "x", **bad})
        assert err and msg in res, (bad, res)
    ops = _ops(cs, ctx)
    assert ("l", "place_cell", "cap", [("c", "add", "cap")]) in ops                           # the copy, then the instance
    assert ("l", "place_cell", "top", [("i", "add", "cap")]) in ops
    real = srv.db("p", "l")
    assert real.layout.cell("cap") is None and not list(real.cell("top").each_inst())         # the real design is untouched
    shutil.rmtree(root)


def test_an_array_is_one_instance_bound_to_one_schematic_instance():
    root, srv, cs = tc.setup()
    ctx = _agent(srv)
    db = _layers(srv, ctx, "l")
    _call(ctx, "create_cell", lib="l", cell="unit", views=["layout"])
    _call(ctx, "add_box", lib="l", cell="unit", layer="68/20", box=[0, 0, 1.5, 2.5])
    out = _call(ctx, "place_cell", lib="l", cell="top", child="unit", x=0, y=0, columns=32, rows=64, pitch_x=2, pitch_y=3, name="R1")
    assert out["array"] == {"columns": 32, "rows": 64, "pitch_um": [2.0, 3.0]} and out["bbox_um"] == [0.0, 0.0, 63.5, 191.5]
    assert "warning" not in out                                                # R1 is the schematic's instance
    info = _call(ctx, "get_layout_info", lib="l", cell="top")
    assert info["instances"] == [{"name": "R1", "id": "unit@0,0", "cell": "unit", "bbox_um": [0.0, 0.0, 63.5, 191.5],
                                  "array": {"columns": 32, "rows": 64, "pitch_um": [2.0, 3.0]}}]
    assert info["unbound_instances"] == 0
    res, err = T.call(ctx, "place_cell", {"lib": "l", "cell": "top", "child": "unit", "x": 0, "y": 300, "name": "R1", "reason": "x"})
    assert err and "placed in top already" in res
    out = _call(ctx, "place_cell", lib="l", cell="top", child="unit", x=0, y=300, name="X9")
    assert "no instance X9" in out["warning"]
    _call(ctx, "place_cell", lib="l", cell="top", child="unit", x=100, y=0, columns=4, pitch_x=2)
    info = _call(ctx, "get_layout_info", lib="l", cell="top")
    assert info["unbound_instances"] == 1 and info["unbound_by_cell"] == {"unit": 1}
    effects = [e for lc in cs.get("p", ctx.changeset)["changes"] for o in lc["ops"] if o["op"] == "place_cell" for e in o["effects"]]
    assert effects[0]["array"] == {"na": 32, "nb": 64, "a": "2000,0", "b": "0,3000"} and effects[0]["inst"] == "R1"
    _call(ctx, "move_instance", lib="l", cell="top", instance="R1", dx=1, dy=0)                # the whole array moves
    assert _call(ctx, "get_layout_info", lib="l", cell="top")["instances"][0]["bbox_um"][0] == 1.0
    shutil.rmtree(root)


def test_import_layout_adds_cells_and_never_overwrites_one_silently():
    root, srv, cs = tc.setup()
    ctx = _agent(srv)
    gen = root / "generated"
    gen.mkdir()
    f = _gds(gen / "cdac.gds", {"cdac": [(0, 0, 100, 100), ("leaf", 0, 0), ("leaf", 1000, 0)], "leaf": [(0, 0, 500, 500)]})
    for bad, msg in (({}, "give path"), ({"path": str(f), "job": "j"}, "give path"), ({"path": "/etc/hosts"}, "outside"),
                     ({"path": str(gen / "x.txt")}, "import a .gds"), ({"path": str(gen / "none.gds")}, "no file"),
                     ({"path": str(f), "lib": "nolib"}, "no library nolib"), ({"job": "nojob"}, "no job nojob")):
        res, err = T.call(ctx, "import_layout", {"lib": "l", "reason": "x", **bad})
        assert err and msg in res, (bad, res)
    out = _call(ctx, "import_layout", lib="l", path=str(f), reason="the CDAC from its generator")
    assert out["top"] == ["cdac"] and out["added"] == ["cdac", "leaf"] and out["replaced"] == []
    db = srv.db(ctx.work, "l")
    assert db.cell("cdac").bbox() == kdb.Box(0, 0, 1500, 500)
    assert (srv.project(ctx.work).library("l").path / "cdac.oas").is_file()                  # saved right away
    assert ("l", "import_layout", "cdac", [("c", "add", "cdac"), ("c", "add", "leaf")]) in _ops(cs, ctx)
    assert srv.db("p", "l").layout.cell("cdac") is None                                       # the real design is untouched
    out = _call(ctx, "import_layout", lib="l", path=str(f))                                   # again: the same cells
    assert out["unchanged"] == ["cdac", "leaf"] and out["added"] == [] and "nothing changed" in out["note"]
    # the generator changed leaf: refused, saying which and how to go on; nothing changes
    _gds(f, {"cdac": [(0, 0, 100, 100), ("leaf", 0, 0), ("leaf", 1000, 0)], "leaf": [(0, 0, 600, 600)], "extra": [(0, 0, 5, 5)]})
    res, err = T.call(ctx, "import_layout", {"lib": "l", "path": str(f), "reason": "x"})
    assert err and "leaf" in res and "replace" in res and "create_library" in res
    assert db.layout.cell("extra") is None and db.cell("leaf").bbox() == kdb.Box(0, 0, 500, 500)
    out = _call(ctx, "import_layout", lib="l", path=str(f), replace=["leaf"], reason="the generator's new unit")
    assert out["added"] == ["extra"] and out["replaced"] == ["leaf"] and out["unchanged"] == ["cdac"]
    assert db.cell("leaf").bbox() == kdb.Box(0, 0, 600, 600)
    rec = [o for o in _ops(cs, ctx) if o[2] == "leaf"]
    assert rec == [("l", "import_layout", "leaf", [("s", "del", "leaf"), ("s", "add", "leaf")])]   # shape by shape
    db.undo("leaf")                                                                            # and so it can be undone
    assert db.cell("leaf").bbox() == kdb.Box(0, 0, 500, 500)
    # a file that places a cell it does not define (and that is no standard cell)
    _gds(gen / "orphan.gds", {"blk": [("missing_cell", 0, 0)]})
    res, err = T.call(ctx, "import_layout", {"lib": "l", "path": str(gen / "orphan.gds"), "reason": "x"})
    assert err and "missing_cell" in res and db.layout.cell("blk") is None
    shutil.rmtree(root)


def test_import_layout_takes_the_final_gds_of_a_librelane_run():
    root, srv, cs = tc.setup()
    ctx = _agent(srv)
    run = srv.project(ctx.work).path / "runs" / "ll-1"
    (run / "runs" / "undine" / "final" / "gds").mkdir(parents=True)
    _gds(run / "runs" / "undine" / "final" / "gds" / "sar.gds", {"sar": [(0, 0, 10, 10)]})
    (run / "status.json").write_text(json.dumps({"id": "ll-1", "kind": "librelane", "state": "running", "lib": "l", "cell": "sar"}))
    res, err = T.call(ctx, "import_layout", {"lib": "l", "job": "ll-1", "reason": "x"})
    assert err and "no final GDS" in res
    (run / "status.json").write_text(json.dumps({"id": "ll-1", "kind": "librelane", "state": "done", "lib": "l", "cell": "sar"}))
    out = _call(ctx, "import_layout", lib="l", job="ll-1", reason="the SAR logic from LibreLane")
    assert out["added"] == ["sar"]
    eff = [o for lc in cs.get("p", ctx.changeset)["changes"] for o in lc["ops"] if o["op"] == "import_layout"][0]["effects"][0]
    assert eff["src"] == {"job": "ll-1", "file": "runs/undine/final/gds/sar.gds"}
    shutil.rmtree(root)


def test_restore_takes_the_cells_brought_in_out_again():
    root, srv, cs = tc.setup()
    ctx = _agent(srv)
    _call(ctx, "create_library", name="m")
    _layers(srv, ctx, "m")
    for cell in ("via", "cap"):
        _call(ctx, "create_cell", lib="m", cell=cell, views=["layout"])
    _call(ctx, "add_box", lib="m", cell="via", layer="68/20", box=[0, 0, 0.2, 0.2])
    _call(ctx, "place_cell", lib="m", cell="cap", child="via", x=0, y=0)
    point = cs.timeline("p", ctx.changeset)["ops"][-1]["last"]
    _call(ctx, "place_cell", lib="l", cell="top", child="cap", child_lib="m", x=0, y=0)      # cap and via copied into l
    _call(ctx, "import_layout", lib="l", path=str(_gds(root / "blk.gds", {"blk": [(0, 0, 10, 10), ("leaf", 0, 0)],
                                                                            "leaf": [(0, 0, 5, 5)]})))
    db = srv.db(ctx.work, "l")
    assert all(db.layout.cell(n) is not None for n in ("cap", "via", "blk", "leaf"))
    cs.restore("p", ctx.changeset, point, "user")
    db = srv.db(ctx.work, "l")
    assert all(db.layout.cell(n) is None for n in ("cap", "via", "blk", "leaf")) and not list(db.cell("top").each_inst())
    # a cell changed after it came in, other than by the operations the restore takes back, stays: refused
    point = cs.timeline("p", ctx.changeset)["ops"][-1]["last"]
    _call(ctx, "place_cell", lib="l", cell="top", child="cap", child_lib="m", x=0, y=0)
    db = srv.db(ctx.work, "l")
    db.cell("via").shapes(db.layout.layer(68, 20)).insert(kdb.Box(1000, 1000, 2000, 2000))   # not recorded
    with pytest.raises(Exception, match="via was changed after it was brought in"):
        cs.restore("p", ctx.changeset, point, "user")
    assert srv.db(ctx.work, "l").layout.cell("cap") is not None
    shutil.rmtree(root)


def test_restore_takes_a_library_out_again():
    root, srv, cs = tc.setup()
    ctx = _agent(srv)
    _call(ctx, "create_cell", lib="l", cell="a", views=["layout"])
    point = cs.timeline("p", ctx.changeset)["ops"][-1]["last"]

    def build():
        _call(ctx, "create_library", name="gen")
        _layers(srv, ctx, "gen")
        _call(ctx, "create_cell", lib="gen", cell="u", views=["schematic", "layout"])
        _call(ctx, "add_box", lib="gen", cell="u", layer="68/20", box=[0, 0, 1, 1])
        _call(ctx, "import_layout", lib="gen", path=str(_gds(root / "blk.gds", {"blk": [(0, 0, 10, 10)]})))
        return cs.timeline("p", ctx.changeset)["ops"][-1]["last"]
    last = build()
    cs.restore("p", ctx.changeset, point, "user")
    prj = srv.project(ctx.work)
    assert list(prj.library_specs()) == ["l"] and not (prj.path / "libs" / "gen").exists()
    assert not (prj.path / "history" / "gen.jsonl").exists()                                # its log went with it
    assert [(lc["lib"], [o["op"] for o in lc["ops"]]) for lc in cs.get("p", ctx.changeset)["changes"]] == [("l", ["create_cell"])]
    # the name is free again; numbering goes on past the records that went
    assert build() > last
    # what the operations did not make stays, and the restore is refused: nothing changes
    (srv.project(ctx.work).library("gen").path / "notes.txt").write_text("mine")
    with pytest.raises(Exception, match="library gen holds notes.txt"):
        cs.restore("p", ctx.changeset, point, "user")
    assert "gen" in srv.project(ctx.work).library_specs() and srv.db(ctx.work, "gen").layout.cell("blk") is not None
    (srv.project(ctx.work).library("gen").path / "notes.txt").unlink()
    cs.restore("p", ctx.changeset, point, "user")
    cs.apply("p", ctx.changeset, "user")                                                       # the project gets no library
    real = srv.project("p")
    assert list(real.library_specs()) == ["l"] and not (real.path / "libs" / "gen").exists()
    assert not (real.path / "history" / "gen.jsonl").exists() and "a" in real.library("l").layout_cells()[0]
    shutil.rmtree(root)


def test_a_generators_arrays_are_the_same_read_back_from_the_cell_files():
    """A GDS's arrays (an AREF, a row, a column) and an instance placed twice come back from the library's cell files
    with their axes ordered and the duplicate gone: the same cells, so importing the file again (after a restart, the
    layout read from its files) changes nothing, and a restore knows the cells unchanged and takes them out."""
    from undine.layouts import layout

    root, srv, cs = tc.setup()
    ctx = _agent(srv)
    ly = kdb.Layout()
    ly.dbu = 0.001
    unit = ly.create_cell("unit")
    unit.shapes(ly.layer(68, 20)).insert(kdb.Box(0, 0, 500, 500))
    cdac = ly.create_cell("cdac")
    for a, b, na, nb, y in (((1000, 0), (0, 1000), 32, 64, 0), ((1000, 0), (0, 0), 8, 1, -2000), ((0, 1000), (0, 0), 4, 1, 70000)):
        cdac.insert(kdb.CellInstArray(unit.cell_index(), kdb.Trans(0, y), kdb.Vector(*a), kdb.Vector(*b), na, nb))
    for _ in range(2):
        cdac.insert(kdb.CellInstArray(unit.cell_index(), kdb.Trans(-3000, 0)))
    f = root / "cdac.gds"
    ly.write(str(f))
    assert _call(ctx, "import_layout", lib="l", path=str(f))["added"] == ["cdac", "unit"]
    srv.dbs.clear()                                                   # a restart: the layout read from its cell files
    db = srv.db(ctx.work, "l")
    gds = kdb.Layout()
    gds.read(str(f))
    assert sum(1 for _ in db.cell("cdac").each_inst()) == 4 and sum(1 for _ in gds.cell("cdac").each_inst()) == 5
    assert layout._content(gds, gds.cell("cdac")) == layout._content(db.layout, db.cell("cdac"))
    out = _call(ctx, "import_layout", lib="l", path=str(f))
    assert out["unchanged"] == ["cdac", "unit"] and out["added"] == out["replaced"] == []
    srv.dbs.clear()
    cs.restore("p", ctx.changeset, 0, "user")
    assert srv.db(ctx.work, "l").layout.cell("cdac") is None
    shutil.rmtree(root)


def test_a_copied_cell_is_compared_with_the_cells_under_it():
    root, srv, cs = tc.setup()
    ctx = _agent(srv)
    _layers(srv, ctx, "l")
    _call(ctx, "create_library", name="m")
    _layers(srv, ctx, "m")
    for lib, side in (("l", 0.2), ("m", 0.6)):                         # the same cap, over another via
        for cell in ("via", "cap"):
            _call(ctx, "create_cell", lib=lib, cell=cell, views=["layout"])
        _call(ctx, "add_box", lib=lib, cell="via", layer="68/20", box=[0, 0, side, side])
        _call(ctx, "add_box", lib=lib, cell="cap", layer="68/20", box=[0, 0, 2, 2])
        _call(ctx, "place_cell", lib=lib, cell="cap", child="via", x=0.5, y=0.5)
    res, err = T.call(ctx, "place_cell", {"lib": "l", "cell": "top", "child": "cap", "child_lib": "m", "x": 0, "y": 0, "reason": "x"})
    assert err and "a cell under it" in res                            # never l's cap placed as if it were m's
    # a cell l lacks, over m's via: copied, its via as via$1 (l's via is another), and the answer says so
    _call(ctx, "create_cell", lib="m", cell="cap2", views=["layout"])
    _call(ctx, "place_cell", lib="m", cell="cap2", child="via", x=0, y=0)
    out = _call(ctx, "place_cell", lib="l", cell="top", child="cap2", child_lib="m", x=0, y=0)
    assert out["from"] == "m" and out["renamed"] == {"via": "via$1"} and "via$1 (not via: l has a via with other content)" in out["warning"]
    db = srv.db(ctx.work, "l")
    assert [db.layout.cell(i.cell_index).name for i in db.cell("cap2").each_inst()] == ["via$1"]
    shutil.rmtree(root)


def test_a_long_batch_keeps_every_warning():
    root, srv, cs = tc.setup()
    ctx = _agent(srv)
    _layers(srv, ctx, "l")
    _call(ctx, "create_cell", lib="l", cell="unit", views=["layout"])
    _call(ctx, "add_box", lib="l", cell="unit", layer="68/20", box=[0, 0, 1, 1])
    # 50 answers that differ (each unit's box): the first 20 listed, the warning of a later one kept
    ops = [{"tool": "place_cell", "args": {"lib": "l", "cell": "top", "child": "unit", "x": 2 * i, "y": 0}} for i in range(50)]
    ops[30] = {"tool": "place_cell", "args": {"lib": "l", "cell": "top", "child": "unit", "x": 0, "y": 5, "name": "X30"}}
    out = json.loads(T.call(ctx, "batch", {"ops": ops, "reason": "units"})[0])
    assert len(out["results"]) == 20 and out["results_left_out"] == 30
    assert [(w["op"], w["tool"]) for w in out["warnings"]] == [(30, "place_cell")] and "no instance X30" in out["warnings"][0]["warning"]
    # what it changed: one entry per reason, its operations counted, the instances by name and place per cell
    assert out["changes"] == [{"op": "place_cell", "operations": 50, "view": "layout", "reason": "units", "cells": {"l/top": [
        "add instance (unit) at 0,0", "add instance (unit) at 2,0", "add instance (unit) at 4,0", "add instance (unit) at 6,0",
        "add instance (unit) at 8,0", "add instance (unit) at 10,0", "… 44 more"]}}]
    shutil.rmtree(root)


def test_a_long_batch_answers_with_counts():
    root, srv, cs = tc.setup()
    ctx = _agent(srv)
    _layers(srv, ctx, "l")
    ops = [{"tool": "add_box", "args": {"lib": "l", "cell": "top", "layer": "68/20", "box": [i, 0, i + 0.5, 0.5]}} for i in range(50)]
    res, err = T.call(ctx, "batch", {"ops": ops, "reason": "unit plates"})
    out = json.loads(res)                     # the same answer 50 times is one, and the boxes are counted per layer
    assert not err and out["applied"] == 50 and out["results"] == [{"op": "0-49", "tool": "add_box", "result": {"ok": True}}]
    assert out["changes"] == [{"op": "add_box", "operations": 50, "view": "layout", "reason": "unit plates",
                               "cells": {"l/top": ["add box 68/20 ×50"]}}]
    assert len(res) < 400
    assert T.call(ctx, "batch", {"ops": ops * 41, "reason": "x"})[0].startswith(f"at most {T.BATCH_MAX} ops")
    shutil.rmtree(root)


def test_a_failed_batch_takes_a_library_and_its_imports_back():
    root, srv, cs = tc.setup()
    ctx = _agent(srv)
    f = _gds(root / "blk.gds", {"blk": [(0, 0, 10, 10)]})
    res, err = T.call(ctx, "batch", {"reason": "x", "ops": [
        {"tool": "create_library", "args": {"name": "gen"}},
        {"tool": "import_layout", "args": {"lib": "gen", "path": str(f)}},
        {"tool": "place_cell", "args": {"lib": "gen", "cell": "blk", "child": "nope", "x": 0, "y": 0}}]})
    assert err and "ops[2] place_cell" in res
    assert "gen" not in srv.project(ctx.work).libraries()
    out = json.loads(T.call(ctx, "batch", {"reason": "x", "ops": [
        {"tool": "create_library", "args": {"name": "gen"}},
        {"tool": "import_layout", "args": {"lib": "gen", "path": str(f)}},
        {"tool": "place_cell", "args": {"lib": "l", "cell": "top", "child": "blk", "child_lib": "gen", "x": 0, "y": 0}}]})[0])
    assert out["applied"] == 3 and srv.db(ctx.work, "l").layout.cell("blk") is not None
    shutil.rmtree(root)


SKY = next((p for p in discover(default_pdk_roots()).values() if p.name == "sky130A" and
            (p.root / "libs.ref/sky130_fd_sc_hd/gds/sky130_fd_sc_hd.gds").is_file()), None)


@pytest.mark.skipif(SKY is None, reason="sky130A with its standard cells is not installed")
def test_standard_cells_come_from_the_pdk():
    import tempfile
    from undine.server.api import Server

    root = Path(tempfile.mkdtemp())
    srv = Server(root, {SKY.id: SKY}, [root])
    srv.store.create("p", SKY.id).create_library("l")
    ctx = _agent(srv)
    call = lambda n, **a: _call(ctx, n, lib="l", cell="logic", **a)
    call("create_cell", views=["schematic", "layout"])
    call("add_instance", symbol="sky130_stdcells/inv_1.sym", x=0, y=0, params={"name": "x1"})
    call("add_instance", symbol="sky130_stdcells/nand2_1.sym", x=200, y=0, params={"name": "x2"})
    before = _call(ctx, "get_layout_info", lib="l", cell="logic")["schematic_sync"]
    assert sorted(before["missing"]) == ["x1", "x2"] and before["unsupported"] == []
    out = call("sync_layout")
    assert sorted(out["added"]) == ["x1", "x2"] and out["errors"] == []
    info = _call(ctx, "get_layout_info", lib="l", cell="logic")
    assert {i["name"]: i["cell"] for i in info["instances"]} == {"x1": "sky130_fd_sc_hd__inv_1", "x2": "sky130_fd_sc_hd__nand2_1"}
    assert info["schematic_sync"]["missing"] == [] and info["schematic_sync"]["out_of_date"] == []
    db = srv.db(ctx.work, "l")
    gds = kdb.Layout()
    gds.read(str(SKY.root / "libs.ref/sky130_fd_sc_hd/gds/sky130_fd_sc_hd.gds"))
    assert db.cell("sky130_fd_sc_hd__inv_1").bbox() == gds.cell("sky130_fd_sc_hd__inv_1").bbox()
    eff = [e for lc in Changesets(srv).get("p", ctx.changeset)["changes"] for o in lc["ops"] for e in o["effects"] if e["k"] == "c"]
    assert {e["cell"]: e["src"] for e in eff}["sky130_fd_sc_hd__inv_1"] == {"stdcell": "sky130_fd_sc_hd"}
    # placed by name, with no schematic behind it; the standard cell already in the library is used again
    _call(ctx, "create_cell", lib="l", cell="row", views=["layout"])
    out = _call(ctx, "place_cell", lib="l", cell="row", child="sky130_fd_sc_hd__nand2_1", x=0, y=0, columns=8, pitch_x=1.38)
    assert out["from"] == "l" and out["bbox_um"][2] == pytest.approx(1.38 * 7 + 1.57, abs=0.01)
    out = _call(ctx, "place_cell", lib="l", cell="row", child="sky130_fd_sc_hd__dfxtp_1", x=0, y=5)
    assert out["from"] == "the PDK's sky130_fd_sc_hd"
    res, err = T.call(ctx, "place_cell", {"lib": "l", "cell": "row", "child": "sky130_fd_sc_hd__nothing_1", "x": 0, "y": 0, "reason": "x"})
    assert err and "no layout cell" in res
    # a LibreLane / generator GDS holds the standard cells it uses: those the library has (as read back from its cell
    # files, which keep no text size) are the same, those it only names come from the PDK
    ly = kdb.Layout()
    blk = ly.create_cell("blk")
    inv, or2 = ly.create_cell("sky130_fd_sc_hd__inv_1"), ly.create_cell("sky130_fd_sc_hd__or2_1")
    inv.copy_tree(gds.cell("sky130_fd_sc_hd__inv_1"))
    or2.ghost_cell = True
    for i, ch in enumerate((inv, or2)):
        blk.insert(kdb.CellInstArray(ch.cell_index(), kdb.Trans(i * 2000, 0)))
    ly.write(str(root / "blk.gds"))
    db.save()
    db.reload()
    out = _call(ctx, "import_layout", lib="l", path=str(root / "blk.gds"))
    assert out["added"] == ["blk"] and out["unchanged"] == ["sky130_fd_sc_hd__inv_1"] and out["stdcells"] == ["sky130_fd_sc_hd__or2_1"]
    # the PDK's cell asked for by its library: l's copy is used when it is the same, refused when l changed it
    std = {"lib": "l", "cell": "row", "child": "sky130_fd_sc_hd__inv_1", "child_lib": "sky130_fd_sc_hd", "x": 0, "y": 10, "reason": "x"}
    assert _call(ctx, "place_cell", **std)["from"] == "l (as in the PDK's sky130_fd_sc_hd)"
    _call(ctx, "add_box", lib="l", cell="sky130_fd_sc_hd__inv_1", layer="met1", box=[0, 0, 0.5, 0.5])
    res, err = T.call(ctx, "place_cell", std)
    assert err and "other than the PDK's sky130_fd_sc_hd's" in res
    shutil.rmtree(root)


@pytest.mark.skipif(SKY is None, reason="sky130A is not installed")
def test_sync_leaves_a_bound_array_and_a_cell_of_your_own_as_they_are(monkeypatch):
    """Unit capacitors bound as an array to the schematic's MIM capacitor, and a unit cell bound to another, are the
    layout's own: sync_layout never puts one PCell in their place, and get_layout_info lists them apart from
    out_of_date. The PCell generator is stood in for (no PCell environment needed): a cell of the PCell's name."""
    import tempfile
    from undine.layouts import sdl
    from undine.pdks import pcell
    from undine.server.api import Server

    monkeypatch.setattr(sdl.Sdl, "_catalog", lambda self: {"mim_cap": {"params": [
        {"name": "type", "choices": ["sky130_fd_pr__model__cap_mim"]}, {"name": "w"}, {"name": "l"}]}})

    def generated(self, d):
        name = pcell.cell_name(d["pcell"], d["params"])
        if self.db.layout.cell(name) is None:
            self.db.new_cell(name)
            self.db._commit(name, [("ins", self.db.layout.layer(70, 20), kdb.Box(0, 0, 2000, 2000))])
        return name, None
    monkeypatch.setattr(sdl.Sdl, "_device_cell", generated)
    root = Path(tempfile.mkdtemp())
    srv = Server(root, {SKY.id: SKY}, [root])
    srv.store.create("p", SKY.id).create_library("l")
    ctx = _agent(srv)
    call = lambda n, **a: _call(ctx, n, lib="l", cell="caps", **a)
    info = lambda: _call(ctx, "get_layout_info", lib="l", cell="caps")
    call("create_cell", views=["schematic", "layout"])
    for i, (n, side) in enumerate((("C1", "2"), ("C2", "2"), ("C3", "3"))):
        call("add_instance", symbol="sky130_fd_pr/cap_mim_m3_1.sym", x=i * 200, y=0,
             params={"name": n, "W": side, "L": side, **({"MF": "64"} if n == "C1" else {})})
    _call(ctx, "create_cell", lib="l", cell="unit", views=["layout"])
    _call(ctx, "add_box", lib="l", cell="unit", layer="met3", box=[0, 0, 3, 3])
    out = call("place_cell", child="unit", x=0, y=0, columns=8, rows=8, pitch_x=4, pitch_y=4, name="C1")
    assert "an array of unit (8 x 8): sync_layout leaves it as it is" in out["warning"]
    assert "unit, a cell of your own" in call("place_cell", child="unit", x=-10, y=0, name="C2")["warning"]
    st = info()["schematic_sync"]
    assert st["out_of_date"] == [] and st["missing"] == ["C3"]
    assert [(o["name"], o["layout"]) for o in st["own_layout"]] == [("C1", "an array of unit (8 x 8)"), ("C2", "unit, a cell of your own")]
    assert st["own_layout"][0]["schematic"].startswith("sky130_fd_pr__model__cap_mim_w2p0_l2p0_")
    out = call("sync_layout")
    assert out["updated"] == [] and out["added"] == ["C3"] and [o["name"] for o in out["own_layout"]] == ["C1", "C2"]
    placed = {i["name"]: i for i in info()["instances"]}
    assert placed["C1"]["cell"] == placed["C2"]["cell"] == "unit" and placed["C1"]["array"]["columns"] == 8
    # a device sync placed follows the schematic as before
    call("set_instance_params", instance="C3", params={"W": "4"})
    assert info()["schematic_sync"]["out_of_date"] == ["C3"]
    out = call("sync_layout")
    assert [u["name"] for u in out["updated"]] == ["C3"] and "_w4p0_" in out["updated"][0]["to"]
    # an array of the device's own PCell: its units count, against the schematic's MF (one here)
    call("add_instance", symbol="sky130_fd_pr/cap_mim_m3_1.sym", x=600, y=0, params={"name": "C4", "W": "4", "L": "3"})
    out = call("place_cell", child=out["updated"][0]["to"], x=0, y=50, columns=2, rows=2, pitch_x=5, pitch_y=5, name="C4")
    assert out["warning"].startswith("4 units placed, and the schematic's C4 has 1 (MF x m)")
    st = info()["schematic_sync"]
    assert st["out_of_date"] == ["C4"] and [o["name"] for o in st["own_layout"]] == ["C1", "C2"]
    call("set_instance_params", instance="C4", params={"MF": "4"})
    st = info()["schematic_sync"]
    assert st["out_of_date"] == [] and [o["name"] for o in st["own_layout"]] == ["C1", "C2"]
    shutil.rmtree(root)
