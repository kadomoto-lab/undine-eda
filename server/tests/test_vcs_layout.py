"""Layout cell files (vcs/layoutmerge.py, docs/design/merge.md): three-way merge of shapes by value, instances by
undine_inst name, meta information by name, written like the layout store writes a cell file; and the semantic diff
(structural, and the XOR of each layer's own shapes)."""
import itertools
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import klayout.db as kdb  # noqa: E402

from undine.layouts import layoutstore  # noqa: E402
from undine.vcs import layoutmerge  # noqa: E402

MET1, LI1 = (68, 20), (67, 20)
PCELL = "undine.pdks.pcell"
L_SHAPE = [(0, 0), (0, 1000), (500, 1000), (500, 500), (1000, 500), (1000, 0)]
_n = itertools.count()


def amp() -> kdb.Layout:
    """The base: cell amp with metal and li shapes, transistors M1 / M2 (named), a via array (unnamed) and PCell-like
    parameters, and the cells it uses."""
    ly = kdb.Layout()
    ly.dbu = 0.001
    top, nfet, via = (ly.create_cell(n) for n in ("amp", "nfet$1", "via$1"))
    nfet.shapes(ly.layer(65, 20)).insert(kdb.Box(-100, -100, 600, 300))
    via.shapes(ly.layer(*MET1)).insert(kdb.Box(0, 0, 170, 170))
    m1, li = ly.layer(*MET1), ly.layer(*LI1)
    top.shapes(m1).insert(kdb.Box(0, 0, 500, 2000)).set_property("net", "out")
    top.shapes(m1).insert(kdb.Box(2000, 0, 2500, 2000))
    top.shapes(m1).insert(kdb.Text("out", kdb.Trans(250, 1000)))
    top.shapes(li).insert(kdb.Polygon([kdb.Point(x, y) for x, y in L_SHAPE]))
    for name, x in (("M1", 1000), ("M2", 3000)):
        i = top.insert(kdb.CellInstArray(nfet.cell_index(), kdb.Trans(x, 0)))
        i.set_property("undine_inst", name)
        i.set_property("role", "input")
    top.insert(kdb.CellInstArray(via.cell_index(), kdb.Trans(10, 10), kdb.Vector(400, 0), kdb.Vector(0, 400), 3, 2))
    top.add_meta_info(kdb.LayoutMetaInfo(PCELL, json.dumps({"pcell": "amp", "params": {"w": 1}}), None, True))
    return ly


def cell_file(tmp: Path, *edits, cell: str = "amp") -> bytes:
    """The cell file of amp() after `edits`, as the layout store writes it."""
    ly = amp()
    for e in edits:
        e(ly)
    d = tmp / f"lib{next(_n)}"
    layoutstore.write(d, ly, {})
    return (d / layoutstore.file_name(cell)).read_bytes()


def read(tmp: Path, data: bytes) -> kdb.Layout:
    p = tmp / f"read{next(_n)}.oas"
    p.write_bytes(data)
    ly = kdb.Layout()
    ly.read(str(p))
    return ly


def shapes(ly: kdb.Layout, layer: tuple[int, int], cell: str = "amp") -> list[str]:
    return sorted(s.to_s() for s in ly.cell(cell).shapes(ly.layer(*layer)).each())


def insts(ly: kdb.Layout, cell: str = "amp") -> dict:
    """Named instances: name -> (child, placement, other properties)."""
    out = {}
    for i in ly.cell(cell).each_inst():
        props = dict(ly.properties(i.prop_id)) if i.prop_id else {}
        if "undine_inst" in props:
            out[props.pop("undine_inst")] = (i.cell.name, str(i.cplx_trans), props)
    return out


def meta(ly: kdb.Layout, cell: str = "amp") -> dict:
    return {m.name: m.value for m in ly.cell(cell).each_meta_info()}


# edits

def add(layer, obj):
    return lambda ly: ly.cell("amp").shapes(ly.layer(*layer)).insert(obj)


def remove(layer, box):
    def edit(ly):
        sh = ly.cell("amp").shapes(ly.layer(*layer))
        sh.erase(next(s for s in sh.each() if s.is_box() and s.box == box))
    return edit


def named(ly, name):
    return next(i for i in ly.cell("amp").each_inst() if i.property("undine_inst") == name)


def move(name, x, y):
    def edit(ly):
        named(ly, name).trans = kdb.Trans(x, y)
    return edit


def set_prop(name, key, value):
    return lambda ly: named(ly, name).set_property(key, value)


def set_params(**params):
    def edit(ly):
        c = ly.cell("amp")
        c.add_meta_info(kdb.LayoutMetaInfo(PCELL, json.dumps({"pcell": "amp", "params": params}), None, True))
    return edit


# ---------------------------------------------------------------- merge


def test_shapes_added_in_different_places_combine(tmp_path):
    base = cell_file(tmp_path)
    ours = cell_file(tmp_path, add(MET1, kdb.Box(5000, 0, 5500, 500)))
    theirs = cell_file(tmp_path, add(MET1, kdb.Box(0, 5000, 500, 5500)), add(LI1, kdb.Box(9000, 0, 9100, 100)))
    r = layoutmerge.merge(base, ours, theirs)
    assert r.kind == "layout" and r.clean and r.conflicts == [] and r.warnings == []
    ly = read(tmp_path, r.data)
    assert shapes(ly, MET1) == sorted(shapes(read(tmp_path, base), MET1) + ["box (5000,0;5500,500)",
                                                                            "box (0,5000;500,5500)"])
    assert "box (0,0;500,2000) props={net=>out}" in shapes(ly, MET1)          # properties kept
    assert "box (9000,0;9100,100)" in shapes(ly, LI1)
    assert insts(ly) == insts(read(tmp_path, base)) and meta(ly) == meta(read(tmp_path, base))
    assert r.combined == ["cell amp layer 67/20: shapes from theirs, 1 added",
                          "cell amp layer 68/20: shapes from theirs, 1 added"]
    assert r.data not in (ours, theirs)


def test_one_shape_changed_differently_on_both_sides_is_a_conflict(tmp_path):
    old = kdb.Box(2000, 0, 2500, 2000)
    base = cell_file(tmp_path)
    ours = cell_file(tmp_path, remove(MET1, old), add(MET1, kdb.Box(2000, 0, 2600, 2000)),
                     add(MET1, kdb.Box(8000, 0, 8100, 100)))                    # and an unrelated shape far away
    theirs = cell_file(tmp_path, remove(MET1, old), add(MET1, kdb.Box(2000, 0, 2500, 2200)))
    r = layoutmerge.merge(base, ours, theirs)
    cid = "shape:amp:68/20:box (2000,0;2500,2000)"
    assert [c.id for c in r.open_conflicts] == [cid]
    c = r.conflicts[0]
    assert (c.base, c.ours, c.theirs, c.taken) == (
        ["box (2000,0;2500,2000)"], {"removed": ["box (2000,0;2500,2000)"], "added": ["box (2000,0;2600,2000)"]},
        {"removed": ["box (2000,0;2500,2000)"], "added": ["box (2000,0;2500,2200)"]}, "ours")
    assert "both sides changed" in c.what and "cell amp layer 68/20" in c.what
    got = shapes(read(tmp_path, r.data), MET1)
    assert "box (2000,0;2600,2000)" in got and "box (2000,0;2500,2200)" not in got   # not both replacements
    assert "box (2000,0;2500,2000)" not in got and "box (8000,0;8100,100)" in got

    r = layoutmerge.merge(base, ours, theirs, prefer={cid: "theirs"})
    assert r.clean and [(c.id, c.taken, c.resolved) for c in r.conflicts] == [(cid, "theirs", True)]
    got = shapes(read(tmp_path, r.data), MET1)
    assert "box (2000,0;2500,2200)" in got and "box (2000,0;2600,2000)" not in got
    assert "box (8000,0;8100,100)" in got                                      # ours' other edit stays

    same = layoutmerge.merge(base, theirs, theirs)                             # the same change on both sides
    assert same.clean and same.data == theirs


def test_a_decided_shape_conflict_takes_one_sides_whole_edit_of_the_place(tmp_path):
    # theirs changed shape A and its neighbour B (whose new version touches A too); ours changed only A. Taking ours
    # drops theirs' new B, so theirs' removal of the old B goes with it: B is not lost
    a, nb = kdb.Box(2000, 0, 2500, 2000), kdb.Box(2500, 0, 3000, 500)
    base = cell_file(tmp_path, add(MET1, nb))
    ours = cell_file(tmp_path, add(MET1, nb), remove(MET1, a), add(MET1, kdb.Box(2000, 0, 2600, 2000)))
    theirs = cell_file(tmp_path, add(MET1, nb), remove(MET1, a), add(MET1, kdb.Box(2000, 0, 2500, 2200)),
                       remove(MET1, nb), add(MET1, kdb.Box(2500, 0, 3100, 500)))
    r = layoutmerge.merge(base, ours, theirs)
    [c] = r.conflicts
    assert c.id == "shape:amp:68/20:box (2000,0;2500,2000)" and c.what.endswith("(and 4 more shapes in its region)")
    assert c.theirs == {"removed": ["box (2000,0;2500,2000)", "box (2500,0;3000,500)"],
                        "added": ["box (2000,0;2500,2200)", "box (2500,0;3100,500)"]}
    assert r.data == ours
    assert layoutmerge.merge(base, ours, theirs, prefer={c.id: "theirs"}).data == theirs


def test_kept_values_do_not_refer_into_the_layout_they_were_read_from(tmp_path):
    # a copy (dup too) of a layout's regular array shares its array part with the layout: kept past the layout, it
    # crashed the merge. In a subprocess, so that a crash fails this test and not the session
    (tmp_path / "amp.oas").write_bytes(cell_file(tmp_path))
    code = ("import sys, klayout.db as kdb\n"
            "from pathlib import Path\n"
            "from undine.vcs import layoutmerge as lm\n"
            "d = Path(sys.argv[1])\n"
            "f = lm._model(lm._load((d / 'amp.oas').read_bytes(), d, 'read'))   # the layout is gone after this\n"
            "junk = []\n"
            "for _ in range(300):\n"
            "    j = kdb.Layout(); c = j.create_cell('x' * 40); junk.append(j)\n"
            "    c.insert(kdb.CellInstArray(c.cell_index(), kdb.ICplxTrans(2.5, 45, True, 7, 7),\n"
            "                               kdb.Vector(9, 0), kdb.Vector(0, 9), 7, 7))\n"
            "ly = lm._build(f.cells, f)\n"
            "print(sorted(str(i.cell_inst) for i in ly.cell('amp').each_inst()))\n")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    p = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True, env=env)
    assert p.returncode == 0, p.stderr[-2000:]
    assert p.stdout.split("\n")[0] == "['#1 r0 1000,0', '#1 r0 3000,0', '#2 r0 10,10 [400,0*3;0,400*2]']"


def test_removing_a_shape_the_other_side_changed_is_a_conflict(tmp_path):
    old = kdb.Box(2000, 0, 2500, 2000)
    base = cell_file(tmp_path)
    ours = cell_file(tmp_path, remove(MET1, old))
    theirs = cell_file(tmp_path, remove(MET1, old), add(MET1, kdb.Box(2000, 0, 2500, 2500)))
    r = layoutmerge.merge(base, ours, theirs)
    assert [(c.id, c.ours["added"], c.theirs["added"]) for c in r.conflicts] == [
        ("shape:amp:68/20:box (2000,0;2500,2000)", [], ["box (2000,0;2500,2500)"])]
    assert "ours removed, theirs changed" in r.conflicts[0].what and r.data == ours
    r = layoutmerge.merge(base, ours, theirs, prefer={r.conflicts[0].id: "theirs"})
    assert r.clean and r.data == theirs


def test_both_deleting_the_same_shape_is_clean(tmp_path):
    gone = kdb.Box(0, 0, 500, 2000)
    base = cell_file(tmp_path)
    ours = cell_file(tmp_path, remove(MET1, gone))
    theirs = cell_file(tmp_path, remove(MET1, gone), add(LI1, kdb.Box(9000, 0, 9100, 100)))
    r = layoutmerge.merge(base, ours, theirs)
    assert r.clean and r.conflicts == [] and r.data == theirs               # theirs did all ours did, and more
    ly = read(tmp_path, r.data)
    assert not any(s.startswith("box (0,0;500,2000)") for s in shapes(ly, MET1))
    assert "box (9000,0;9100,100)" in shapes(ly, LI1)


def test_named_instance_moved_on_ours_and_property_changed_on_theirs_combine(tmp_path):
    base = cell_file(tmp_path)
    ours = cell_file(tmp_path, move("M1", 1200, 0))
    theirs = cell_file(tmp_path, set_prop("M1", "role", "bias"))
    r = layoutmerge.merge(base, ours, theirs)
    assert r.clean and r.combined == ["cell amp: instance M1 property role"]
    got = insts(read(tmp_path, r.data))
    assert got["M1"] == ("nfet$1", "r0 *1 1200,0", {"role": "bias"})
    assert got["M2"] == ("nfet$1", "r0 *1 3000,0", {"role": "input"})


def test_named_instance_moved_differently_on_both_sides_is_a_conflict(tmp_path):
    base = cell_file(tmp_path)
    ours = cell_file(tmp_path, move("M1", 1200, 0), add(MET1, kdb.Box(8000, 0, 8100, 100)))
    theirs = cell_file(tmp_path, move("M1", 1000, 300))
    r = layoutmerge.merge(base, ours, theirs)
    cid = "inst:amp:M1:placement"
    assert [(c.id, c.base, c.ours, c.theirs) for c in r.conflicts] == [
        (cid, "r0 *1 1000,0", "r0 *1 1200,0", "r0 *1 1000,300")]
    assert insts(read(tmp_path, r.data))["M1"][1] == "r0 *1 1200,0"           # ours kept: not a diagonal move
    r = layoutmerge.merge(base, ours, theirs, prefer={cid: "theirs"})
    assert r.clean
    ly = read(tmp_path, r.data)
    assert insts(ly)["M1"][1] == "r0 *1 1000,300" and "box (8000,0;8100,100)" in shapes(ly, MET1)


def test_delete_modify_of_a_named_instance_is_a_conflict(tmp_path):
    base = cell_file(tmp_path)
    ours = cell_file(tmp_path, lambda ly: named(ly, "M2").delete())
    theirs = cell_file(tmp_path, set_prop("M2", "role", "load"), move("M1", 1000, 500))
    r = layoutmerge.merge(base, ours, theirs)
    assert [c.id for c in r.conflicts] == ["inst:amp:M2"]
    c = r.conflicts[0]
    assert c.ours is None and c.base == {"child": "nfet$1", "placement": "r0 *1 3000,0", "props": {"role": "input"}}
    assert c.theirs["props"] == {"role": "load"}
    got = insts(read(tmp_path, r.data))
    assert "M2" not in got and got["M1"][1] == "r0 *1 1000,500"               # theirs' other change combined
    got = insts(read(tmp_path, layoutmerge.merge(base, ours, theirs, prefer={"inst:amp:M2": "theirs"}).data))
    assert got["M2"] == ("nfet$1", "r0 *1 3000,0", {"role": "load"})


def test_pcell_parameters_changed_on_one_side(tmp_path):
    base = cell_file(tmp_path)
    ours = cell_file(tmp_path, set_params(w=2))
    theirs = cell_file(tmp_path, add(MET1, kdb.Box(6000, 0, 6500, 500)))
    r = layoutmerge.merge(base, ours, theirs)
    assert r.clean
    ly = read(tmp_path, r.data)
    assert json.loads(meta(ly)[PCELL])["params"] == {"w": 2} and "box (6000,0;6500,500)" in shapes(ly, MET1)
    r = layoutmerge.merge(base, theirs, ours)                                   # the other way round
    assert r.clean and r.combined == [f"cell amp: meta {PCELL}"]
    assert json.loads(meta(read(tmp_path, r.data))[PCELL])["params"] == {"w": 2}

    r = layoutmerge.merge(base, ours, cell_file(tmp_path, set_params(w=3)))
    assert [c.id for c in r.conflicts] == [f"meta:amp:{PCELL}"]
    assert r.data == ours


def test_independent_additions_that_touch_are_a_conflict(tmp_path):
    # shapes both sides drew into each other on one layer connect what neither side connected: one region, a conflict
    base = cell_file(tmp_path)
    ours = cell_file(tmp_path, add(MET1, kdb.Box(4000, 0, 4600, 600)))
    for other in (kdb.Box(4500, 500, 5000, 1000), kdb.Box(4600, 0, 5000, 600)):          # overlapping, abutting
        theirs = cell_file(tmp_path, add(MET1, other))
        r = layoutmerge.merge(base, ours, theirs)
        [c] = r.open_conflicts
        assert c.id == "shape:amp:68/20:box (4000,0;4600,600)" and "both sides added shape" in c.what
        assert r.data == ours
        assert layoutmerge.merge(base, ours, theirs, prefer={c.id: "theirs"}).data == theirs
    # on another layer, apart, or the same shape on both sides: no conflict
    for other in (add(LI1, kdb.Box(4500, 500, 5000, 1000)), add(MET1, kdb.Box(4700, 0, 5000, 600)),
                  add(MET1, kdb.Box(4000, 0, 4600, 600))):
        r = layoutmerge.merge(base, ours, cell_file(tmp_path, other))
        assert r.clean and r.warnings == []


def test_a_result_equal_to_one_side_keeps_its_bytes(tmp_path):
    def file(*edits, compress=True):
        ly = amp()
        for e in edits:
            e(ly)
        opt = kdb.SaveLayoutOptions()
        opt.format = "OASIS"
        opt.oasis_compression_level = 2 if compress else 0
        p = tmp_path / f"f{next(_n)}.oas"
        layoutstore.cell_layout(ly, ly.cell("amp")).write(str(p), opt)
        return p.read_bytes()
    base = file()
    reformatted = file(compress=False)                       # the same cell, other bytes (no repetitions)
    assert reformatted != base and layoutmerge.diff(base, reformatted)["changes"] == []
    ours = file(add(MET1, kdb.Box(5000, 0, 5500, 500)))
    r = layoutmerge.merge(base, ours, reformatted)
    assert r.clean and r.data == ours and r.combined == []
    theirs = file(move("M1", 0, 3000))
    r = layoutmerge.merge(base, reformatted, theirs)
    assert r.clean and r.data == theirs
    both = file(add(MET1, kdb.Box(5000, 0, 5500, 500)), move("M1", 0, 3000), compress=False)
    assert layoutmerge.merge(base, both, theirs).data == both                  # ours did what theirs did, and more


def via(x, y, rot=kdb.Trans.R0, cell="via$1"):
    """An unnamed instance of `cell` at x, y."""
    return lambda ly: ly.cell("amp").insert(kdb.CellInstArray(ly.cell(cell).cell_index(), kdb.Trans(rot, x, y)))


def the_array(ly):
    return next(i for i in ly.cell("amp").each_inst() if i.is_regular_array())


def drop_array(ly):
    the_array(ly).delete()


def reshape_array(na=3, nb=2, x=10, y=10):
    def edit(ly):
        a = the_array(ly).cell_inst
        the_array(ly).cell_inst = kdb.CellInstArray(a.cell_index, kdb.Trans(x, y), a.a, a.b, na, nb)
    return edit


def unnamed(ly, cell="amp") -> list[str]:
    return sorted(f"{i.cell.name} {i.cplx_trans}" + (f" {i.na}x{i.nb}" if i.is_regular_array() else "")
                  for i in ly.cell(cell).each_inst() if not i.property("undine_inst"))


ARRAY = "via$1 r0 *1 10,10 array 3x2 a=400,0 b=0,400"          # the fixture's unnamed via array, by value


def test_unnamed_instances_merge_as_a_multiset(tmp_path):
    def copy_array(ly):
        ly.cell("amp").insert(the_array(ly).cell_inst)
    base = cell_file(tmp_path)
    r = layoutmerge.merge(base, cell_file(tmp_path, via(5000, 5000)), cell_file(tmp_path, via(6000, 6000)))
    assert r.clean and r.warnings == []
    assert unnamed(read(tmp_path, r.data)) == ["via$1 r0 *1 10,10 3x2", "via$1 r0 *1 5000,5000",
                                               "via$1 r0 *1 6000,6000"]
    r = layoutmerge.merge(base, cell_file(tmp_path, drop_array), cell_file(tmp_path, drop_array, via(6000, 6000)))
    assert [c.id for c in r.conflicts] == [f"inst:amp:{ARRAY}"]   # theirs may have moved it: see the next test
    r = layoutmerge.merge(base, cell_file(tmp_path, drop_array), cell_file(tmp_path, drop_array))
    assert r.clean and unnamed(read(tmp_path, r.data)) == []
    # one side drops the array, the other adds a second copy: how many there are is a conflict
    r = layoutmerge.merge(base, cell_file(tmp_path, drop_array), cell_file(tmp_path, copy_array))
    assert [(c.id, c.base, c.ours, c.theirs) for c in r.conflicts] == [
        (f"inst:amp:{ARRAY}", [ARRAY], {"removed": [ARRAY], "added": []}, {"removed": [], "added": [ARRAY]})]


def test_unnamed_instance_changed_differently_on_both_sides_is_a_conflict(tmp_path):
    base = cell_file(tmp_path)
    for ours, theirs, o, t in (
            (reshape_array(4, 2), reshape_array(3, 3), "via$1 r0 *1 10,10 4x2", "via$1 r0 *1 10,10 3x3"),
            (reshape_array(x=100), reshape_array(y=100), "via$1 r0 *1 100,10 3x2", "via$1 r0 *1 10,100 3x2")):
        ours, theirs = cell_file(tmp_path, ours, via(9000, 0, cell="nfet$1")), cell_file(tmp_path, theirs)
        r = layoutmerge.merge(base, ours, theirs)
        assert [c.id for c in r.open_conflicts] == [f"inst:amp:{ARRAY}"]
        c = r.conflicts[0]
        assert "cell amp: both sides changed instance" in c.what and c.base == [ARRAY]
        assert len(c.ours["added"]) == len(c.theirs["added"]) == 1 and c.ours != c.theirs
        assert unnamed(read(tmp_path, r.data)) == ["nfet$1 r0 *1 9000,0", o]     # not both arrays
        r = layoutmerge.merge(base, ours, theirs, prefer={c.id: "theirs"})
        assert r.clean and unnamed(read(tmp_path, r.data)) == ["nfet$1 r0 *1 9000,0", t]
    # deleted on one side, changed on the other
    ours, theirs = cell_file(tmp_path, drop_array), cell_file(tmp_path, reshape_array(4, 2))
    r = layoutmerge.merge(base, ours, theirs)
    assert [(c.id, c.ours["added"], c.theirs["added"]) for c in r.conflicts] == [
        (f"inst:amp:{ARRAY}", [], ["via$1 r0 *1 10,10 array 4x2 a=400,0 b=0,400"])]
    assert "ours removed, theirs changed" in r.conflicts[0].what and r.data == ours
    assert layoutmerge.merge(base, ours, theirs, prefer={f"inst:amp:{ARRAY}": "theirs"}).data == theirs
    r = layoutmerge.merge(base, theirs, ours)
    assert "ours changed, theirs removed" in r.conflicts[0].what and r.data == theirs
    # the same change on both sides, or a change on one side only: no conflict
    assert layoutmerge.merge(base, theirs, theirs).clean
    r = layoutmerge.merge(base, theirs, cell_file(tmp_path, via(9000, 0, cell="nfet$1")))
    assert r.clean and unnamed(read(tmp_path, r.data)) == ["nfet$1 r0 *1 9000,0", "via$1 r0 *1 10,10 4x2"]


def test_a_move_elsewhere_is_its_own_region(tmp_path):
    # theirs changed the array and moved a second via; ours changed only the array. Each new via goes with the nearest
    # one theirs removed (a move), so the array is one region (a conflict) and the moved via another (theirs')
    base = cell_file(tmp_path, via(5000, 5000))
    ours = cell_file(tmp_path, via(5000, 5000), reshape_array(4, 2))
    theirs = cell_file(tmp_path, reshape_array(3, 3), via(6000, 6000))          # the via at 5000,5000 moved
    r = layoutmerge.merge(base, ours, theirs)
    [c] = r.conflicts
    assert c.id == f"inst:amp:{ARRAY}" and c.theirs["added"] == ["via$1 r0 *1 10,10 array 3x3 a=400,0 b=0,400"]
    assert unnamed(read(tmp_path, r.data)) == ["via$1 r0 *1 10,10 4x2", "via$1 r0 *1 6000,6000"]
    assert layoutmerge.merge(base, ours, theirs, prefer={c.id: "theirs"}).data == theirs


def swap_array(cell):
    """The via array replaced by one of `cell` (a new cell) at the same place: a PCell variant swapped for another."""
    def edit(ly):
        new = ly.create_cell(cell)
        new.shapes(ly.layer(*MET1)).insert(kdb.Box(0, 0, 200, 200))
        a = the_array(ly).cell_inst
        the_array(ly).cell_inst = kdb.CellInstArray(new.cell_index(), a.cplx_trans, a.a, a.b, a.na, a.nb)
    return edit


def test_an_instance_given_another_child_cell_is_replaced_at_its_place(tmp_path):
    # the instances that may replace an unnamed one are those of its child cell and those at its place: two variant
    # swaps of one array are not two arrays, and a swap does not undo the other side's deletion
    base = cell_file(tmp_path)
    ours, theirs = cell_file(tmp_path, swap_array("via$2")), cell_file(tmp_path, swap_array("via$3"))
    r = layoutmerge.merge(base, ours, theirs)
    assert [(c.id, c.ours["added"], c.theirs["added"]) for c in r.conflicts] == [
        (f"inst:amp:{ARRAY}", ["via$2 r0 *1 10,10 array 3x2 a=400,0 b=0,400"],
         ["via$3 r0 *1 10,10 array 3x2 a=400,0 b=0,400"])]
    assert "both sides changed" in r.conflicts[0].what
    assert unnamed(read(tmp_path, r.data)) == ["via$2 r0 *1 10,10 3x2"] and r.data == ours
    assert layoutmerge.merge(base, ours, theirs, prefer={f"inst:amp:{ARRAY}": "theirs"}).data == theirs
    gone = cell_file(tmp_path, drop_array)
    r = layoutmerge.merge(base, gone, theirs)
    assert [c.id for c in r.conflicts] == [f"inst:amp:{ARRAY}"]
    assert "ours removed, theirs changed" in r.conflicts[0].what and r.data == gone          # the deletion is not lost
    # the same swap on both sides, or a swap on one side only: no conflict
    assert layoutmerge.merge(base, ours, ours).clean
    r = layoutmerge.merge(base, ours, cell_file(tmp_path, via(9000, 0, cell="nfet$1")))
    assert r.clean and unnamed(read(tmp_path, r.data)) == ["nfet$1 r0 *1 9000,0", "via$2 r0 *1 10,10 3x2"]


def test_the_same_change_on_both_sides_and_more_on_one(tmp_path):
    # the same edit on both sides and an addition elsewhere on one: two regions, no conflict
    base = cell_file(tmp_path)
    small, large = cell_file(tmp_path, reshape_array(4, 2)), cell_file(tmp_path, reshape_array(4, 2), via(9000, 9000))
    for ours, theirs in ((small, large), (large, small)):
        r = layoutmerge.merge(base, ours, theirs)
        assert r.clean and r.conflicts == [] and r.data == large
        assert unnamed(read(tmp_path, r.data)) == ["via$1 r0 *1 10,10 4x2", "via$1 r0 *1 9000,9000"]
    # ... and an addition touching the edit on one: one region both sides changed differently, a conflict (git
    # treats the same edit of a line plus an added line next to it on one side alike)
    a, wide, top = kdb.Box(2000, 0, 2500, 2000), kdb.Box(2000, 0, 2600, 2000), kdb.Box(2000, 2000, 2500, 2500)
    small = cell_file(tmp_path, remove(MET1, a), add(MET1, wide))
    large = cell_file(tmp_path, remove(MET1, a), add(MET1, wide), add(MET1, top))       # touches the old box too
    for ours, theirs in ((small, large), (large, small)):
        r = layoutmerge.merge(base, ours, theirs)
        assert [c.id for c in r.conflicts] == ["shape:amp:68/20:box (2000,0;2500,2000)"] and r.data == ours
    # a side that only removed the object made no part of the other side's change: still delete / modify
    r = layoutmerge.merge(base, cell_file(tmp_path, remove(MET1, a)), large)
    assert [c.id for c in r.conflicts] == ["shape:amp:68/20:box (2000,0;2500,2000)"]
    assert "ours removed, theirs changed" in r.conflicts[0].what
    r = layoutmerge.merge(base, cell_file(tmp_path, drop_array), cell_file(tmp_path, reshape_array(4, 2),
                                                                            via(9000, 9000)))
    assert [c.id for c in r.conflicts] == [f"inst:amp:{ARRAY}"]


def test_a_copy_moves_the_nearest_shape_it_copies(tmp_path):
    # a translated copy a side added replaces the nearest copy that side removed: both sides deleting contact c1 while
    # ours nudges another contact 17 um away are independent edits, not a conflict over c1
    CONT = (66, 44)
    c1, c2 = kdb.Box(3000, 3000, 3170, 3170), kdb.Box(20000, 3000, 20170, 3170)
    base = cell_file(tmp_path, add(CONT, c1), add(CONT, c2))
    ours, theirs = cell_file(tmp_path, add(CONT, c2.moved(400, 0))), cell_file(tmp_path, add(CONT, c2))
    r = layoutmerge.merge(base, ours, theirs)
    assert r.clean and r.conflicts == [] and r.data == ours
    assert shapes(read(tmp_path, r.data), CONT) == ["box (20400,3000;20570,3170)"]
    # a move however far is still that contact: two moves of it conflict, a move against a deletion too
    for dx in (400, 50000):
        right = cell_file(tmp_path, add(CONT, c1.moved(dx, 0)), add(CONT, c2))
        r = layoutmerge.merge(base, right, cell_file(tmp_path, add(CONT, c1.moved(0, 400)), add(CONT, c2)))
        assert [(c.id, c.ours["added"]) for c in r.conflicts] == [
            ("shape:amp:66/44:box (3000,3000;3170,3170)", [f"box ({3000 + dx},3000;{3170 + dx},3170)"])]
        r = layoutmerge.merge(base, right, cell_file(tmp_path, add(CONT, c2)))
        assert "ours changed, theirs removed" in r.conflicts[0].what

    def label_at(x, y):
        def edit(ly):
            sh = ly.cell("amp").shapes(ly.layer(*MET1))
            sh.erase(next(s for s in sh.each() if s.is_text()))
            sh.insert(kdb.Text("out", kdb.Trans(x, y)))
        return edit
    # two moves of one label are not two labels
    r = layoutmerge.merge(cell_file(tmp_path), cell_file(tmp_path, label_at(250, 1500)),
                          cell_file(tmp_path, label_at(9000, 0)))
    assert [(c.id, c.ours["added"], c.theirs["added"]) for c in r.conflicts] == [
        ("shape:amp:68/20:text ('out',r0 250,1000)", ["text ('out',r0 250,1500)"], ["text ('out',r0 9000,0)"])]


def test_a_shape_moved_further_than_its_size_is_still_the_same_shape(tmp_path):
    CONT = (66, 44)
    old = kdb.Box(3000, 3000, 3170, 3170)

    def moved(dx, dy):
        return remove(CONT, old), add(CONT, old.moved(dx, dy))
    base = cell_file(tmp_path, add(CONT, old))
    right, up = cell_file(tmp_path, add(CONT, old), *moved(400, 0)), cell_file(tmp_path, add(CONT, old), *moved(0, 400))
    r = layoutmerge.merge(base, right, up)
    cid = "shape:amp:66/44:box (3000,3000;3170,3170)"
    assert [(c.id, c.ours["added"], c.theirs["added"]) for c in r.conflicts] == [
        (cid, ["box (3400,3000;3570,3170)"], ["box (3000,3400;3170,3570)"])]
    assert "both sides changed" in r.conflicts[0].what and r.data == right            # one contact, not two
    assert layoutmerge.merge(base, right, up, prefer={cid: "theirs"}).data == up
    gone = cell_file(tmp_path, add(CONT, old), remove(CONT, old))
    r = layoutmerge.merge(base, gone, up)
    assert [c.id for c in r.conflicts] == [cid] and "ours removed, theirs changed" in r.conflicts[0].what
    assert r.data == gone                                                             # the deletion is not lost
    # a copy with other properties is another shape; a move on one side only, or the same on both, combines
    other = cell_file(tmp_path, add(CONT, old), remove(CONT, old),
                      lambda ly: ly.cell("amp").shapes(ly.layer(*CONT)).insert(old.moved(400, 0)).set_property("n", 1))
    assert layoutmerge.merge(base, other, up).conflicts[0].ours["added"] == []
    assert layoutmerge.merge(base, right, right).clean
    r = layoutmerge.merge(base, right, cell_file(tmp_path, add(CONT, old), add(MET1, kdb.Box(9000, 0, 9100, 100))))
    assert r.clean and "box (3400,3000;3570,3170)" in shapes(read(tmp_path, r.data), CONT)


def test_instances_added_at_the_same_place_on_both_sides_are_a_warning(tmp_path):
    def nfet(name, x, y):
        def edit(ly):
            i = ly.cell("amp").insert(kdb.CellInstArray(ly.cell("nfet$1").cell_index(), kdb.Trans(x, y)))
            i.set_property("undine_inst", name)
        return edit
    base = cell_file(tmp_path)
    r = layoutmerge.merge(base, cell_file(tmp_path, nfet("M3", 5000, 0)), cell_file(tmp_path, nfet("M4", 5000, 0)))
    assert r.clean and r.warnings == ["cell amp: instances of nfet$1 added on both sides at the same place (5,0 um): "
                                      "ours M3, theirs M4"]
    assert {"M3", "M4"} <= set(insts(read(tmp_path, r.data)))
    # unnamed ones at one place are one region: a conflict
    r = layoutmerge.merge(base, cell_file(tmp_path, via(5000, 0)), cell_file(tmp_path, via(5000, 0, kdb.Trans.R90)))
    assert [c.id for c in r.conflicts] == ["inst:amp:via$1 r0 *1 5000,0"] and r.warnings == []
    r = layoutmerge.merge(base, cell_file(tmp_path, move("M1", 5000, 0)),
                          cell_file(tmp_path, via(5000, 0, cell="nfet$1")))
    assert r.warnings == ["cell amp: instances of nfet$1 added on both sides at the same place (5,0 um): "
                          "ours M1, theirs unnamed"]                                  # moved there counts too
    # the same instance added on both sides (merged into one), other places, other cells: nothing to look at
    for o, t in ((via(5000, 0), via(5000, 0)), (via(5000, 0), via(5000, 10)), (via(5000, 0), nfet("M4", 5000, 0)),
                 (nfet("M3", 5000, 0), move("M2", 5000, 10))):
        r = layoutmerge.merge(base, cell_file(tmp_path, o), cell_file(tmp_path, t))
        assert r.clean and r.warnings == []


def test_merged_file_reads_back_with_the_right_ghost_cells(tmp_path):
    def add_cap(ly):
        cap = ly.create_cell("cap$1")
        cap.shapes(ly.layer(*MET1)).insert(kdb.Box(0, 0, 1000, 1000))
        ly.cell("amp").insert(kdb.CellInstArray(cap.cell_index(), kdb.Trans(kdb.Trans.R90, 7000, 0))) \
            .set_property("undine_inst", "C1")

    def drop_vias(ly):
        next(i for i in ly.cell("amp").each_inst() if i.cell.name == "via$1").delete()
    base = cell_file(tmp_path)
    r = layoutmerge.merge(base, cell_file(tmp_path, add_cap), cell_file(tmp_path, drop_vias))
    assert r.clean
    ly = read(tmp_path, r.data)
    assert sorted((c.name, c.is_ghost_cell()) for c in ly.each_cell()) == [
        ("amp", False), ("cap$1", True), ("nfet$1", True)]                     # via$1 is no longer used
    assert insts(ly)["C1"] == ("cap$1", "r90 *1 7000,0", {})
    # in the library, next to the files of the cells it uses, the ghost cells resolve
    lib = tmp_path / "lib"
    full = amp()
    add_cap(full)
    layoutstore.write(lib, full, {})
    (lib / "amp.oas").write_bytes(r.data)
    back = kdb.Layout()
    layoutstore.read(lib, back)
    assert not any(c.is_ghost_cell() for c in back.each_cell())
    assert sorted(back.cell(i).name for i in back.cell("amp").each_child_cell()) == ["cap$1", "nfet$1"]


def test_database_units_that_differ_keep_one_whole_file(tmp_path):
    def scaled(ly):
        ly.transform(kdb.ICplxTrans(2.0))
        ly.dbu = 0.0005
    base = cell_file(tmp_path)
    ours = cell_file(tmp_path, add(MET1, kdb.Box(5000, 0, 5500, 500)))
    theirs = cell_file(tmp_path, scaled)
    r = layoutmerge.merge(base, ours, theirs)
    assert [(c.id, c.base, c.ours, c.theirs) for c in r.conflicts] == [("dbu", 0.001, 0.001, 0.0005)]
    assert r.data == ours
    assert layoutmerge.merge(base, ours, theirs, prefer={"dbu": "theirs"}).data == theirs
    r = layoutmerge.merge(base, base, theirs)                                   # only theirs changed anything
    assert r.clean and r.data == theirs
    d = layoutmerge.diff(base, theirs)                                         # compared in the new unit
    assert d["changes"] == [{"do": "mod", "id": "dbu", "what": "database unit 0.001 -> 0.0005 um",
                             "before": 0.001, "after": 0.0005}]


def test_both_sides_added_the_file(tmp_path):
    ours = cell_file(tmp_path, add(MET1, kdb.Box(5000, 0, 5500, 500)))
    theirs = cell_file(tmp_path, add(MET1, kdb.Box(6000, 0, 6500, 500)), move("M1", 1000, 300))
    r = layoutmerge.merge(None, ours, theirs)
    assert [c.id for c in r.conflicts] == ["inst:amp:M1:placement"]           # added twice, differently
    got = shapes(read(tmp_path, r.data), MET1)
    assert {"box (5000,0;5500,500)", "box (6000,0;6500,500)"} <= set(got)
    assert got.count("box (2000,0;2500,2000)") == 1                            # the same shape added once
    assert layoutmerge.merge(None, ours, ours).data == ours


def test_a_file_with_several_cells_merges_cell_by_cell(tmp_path):
    def file(*edits):
        ly = amp()
        for e in edits:
            e(ly)
        p = tmp_path / f"all{next(_n)}.oas"
        ly.write(str(p))                                       # a whole library in one file (format 1)
        return p.read_bytes()

    def via_box(ly):
        ly.cell("via$1").shapes(ly.layer(*MET1)).insert(kdb.Box(0, 0, 200, 200))
    base = file()
    r = layoutmerge.merge(base, file(add(MET1, kdb.Box(5000, 0, 5500, 500))), file(via_box))
    assert r.clean
    ly = read(tmp_path, r.data)
    assert sorted((c.name, c.is_ghost_cell()) for c in ly.each_cell()) == [
        ("amp", False), ("nfet$1", False), ("via$1", False)]
    assert "box (5000,0;5500,500)" in shapes(ly, MET1) and "box (0,0;200,200)" in shapes(ly, MET1, "via$1")
    r = layoutmerge.merge(base, file(lambda ly: ly.delete_cell(ly.cell("via$1").cell_index())), file(via_box))
    assert [c.id for c in r.conflicts] == ["cell:via$1"]                       # deleted / modified


def test_conflict_ids_are_stable(tmp_path):
    old = kdb.Box(2000, 0, 2500, 2000)
    base = cell_file(tmp_path)
    ours = cell_file(tmp_path, move("M1", 1200, 0), move("M2", 0, 0), remove(MET1, old),
                     add(MET1, kdb.Box(2000, 0, 2600, 2000)), set_params(w=2))
    theirs = cell_file(tmp_path, move("M1", 0, 1200), move("M2", 9, 9), remove(MET1, old),
                       add(MET1, kdb.Box(2000, 0, 2500, 2600)), set_params(w=3))
    ids = [c.id for c in layoutmerge.merge(base, ours, theirs).conflicts]
    assert ids == [f"meta:amp:{PCELL}", "inst:amp:M1:placement", "inst:amp:M2:placement",
                   "shape:amp:68/20:box (2000,0;2500,2000)"]
    assert [c.id for c in layoutmerge.merge(base, ours, theirs).conflicts] == ids
    r = layoutmerge.merge(base, ours, theirs, prefer={i: "theirs" for i in ids})
    assert r.clean and r.data == theirs


# ---------------------------------------------------------------- diff


def test_diff_of_a_polygon_replaced_by_boxes_covering_it_has_no_xor(tmp_path):
    def split(ly):
        sh = ly.cell("amp").shapes(ly.layer(*LI1))
        sh.clear()
        sh.insert(kdb.Box(0, 0, 500, 1000))
        sh.insert(kdb.Box(500, 0, 1000, 500))
    d = layoutmerge.diff(cell_file(tmp_path), cell_file(tmp_path, split))
    assert d["kind"] == "layout" and "not flattened" in d["note"]
    assert d["changes"] == [{
        "do": "mod", "id": "shape:amp:67/20", "what": "cell amp layer 67/20: shapes 2 added, 1 removed",
        "before": {"shapes": 1, "removed": 1, "examples": ["polygon (0,0;0,1;0.5,1;0.5,0.5;1,0.5;1,0)"]},
        "after": {"shapes": 2, "added": 2, "examples": ["box (0,0;0.5,1)", "box (0.5,0;1,0.5)"]}}]
    assert d["geometry"] == [{"cell": "amp", "layer": "67/20", "xor_um2": 0.0, "geometry_unchanged": True}]


def test_diff_of_a_moved_label_has_no_xor_and_is_a_change(tmp_path):
    def relabel(ly):
        sh = ly.cell("amp").shapes(ly.layer(*MET1))
        sh.erase(next(s for s in sh.each() if s.is_text()))
        sh.insert(kdb.Text("out", kdb.Trans(250, 1500)))
    d = layoutmerge.diff(cell_file(tmp_path), cell_file(tmp_path, relabel))
    assert d["geometry"] == [{"cell": "amp", "layer": "68/20", "xor_um2": 0.0, "geometry_unchanged": False}]


def test_diff_reports_shapes_instances_and_meta(tmp_path):
    base = cell_file(tmp_path)
    new = cell_file(tmp_path, remove(MET1, kdb.Box(2000, 0, 2500, 2000)), add(MET1, kdb.Box(2000, 0, 2500, 3000)),
                    move("M1", 1500, 0), set_prop("M1", "role", "bias"), lambda ly: named(ly, "M2").delete(),
                    set_params(w=2))
    d = layoutmerge.diff(base, new)
    assert [(c["do"], c["id"]) for c in d["changes"]] == [
        ("mod", f"meta:amp:{PCELL}"), ("mod", "inst:amp:M1"), ("del", "inst:amp:M2"), ("mod", "shape:amp:68/20")]
    m1 = d["changes"][1]
    assert m1["what"] == "cell amp: instance M1: placement r0 *1 1,0 -> r0 *1 1.5,0; property role input -> bias"
    assert m1["before"] == {"child": "nfet$1", "placement": "r0 *1 1,0", "props": {"role": "input"}}
    assert d["changes"][2]["after"] is None and d["changes"][2]["what"].endswith("removed")
    assert json.loads(d["changes"][0]["after"])["params"] == {"w": 2}
    assert d["geometry"] == [{"cell": "amp", "layer": "68/20", "xor_um2": 0.5, "geometry_unchanged": False}]
    assert layoutmerge.diff(base, base) == {"kind": "layout", "changes": [], "geometry": [], "note": d["note"]}
    assert layoutmerge.diff(base, new) == d                                    # deterministic
    added = layoutmerge.diff(None, base)
    assert added["changes"][0] == {"do": "add", "id": "cell:amp", "what": "cell amp added", "before": None,
                                   "after": {"shapes": 4, "instances": 3, "meta": [PCELL]}}
    assert {c["do"] for c in added["changes"]} == {"add"}
    assert {c["do"] for c in layoutmerge.diff(base, None)["changes"]} == {"del"}
