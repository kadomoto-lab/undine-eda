"""Changeset life cycle on a throw-away project (no PDK needed): open, edit in the preview, review, apply, revert."""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import klayout.db as kdb  # noqa: E402
import pytest  # noqa: E402
from undine.layouts import layoutstore  # noqa: E402
from undine.schematics import xschem
from undine.server.api import Server  # noqa: E402
from undine.store.changeset import ChangesetError, Changesets, MergeConflict  # noqa: E402

R_SYM = 'v {}\nK {type=resistor\nformat="@name @pinlist @value"\ntemplate="name=R1 value=1k"}\n' \
        'B 5 -2.5 -32.5 2.5 -27.5 {name=P}\nB 5 -2.5 27.5 2.5 32.5 {name=M}\n'


def setup():
    root = Path(tempfile.mkdtemp())
    srv = Server(root, {}, [root])
    prj = srv.store.create("p")
    lib = prj.create_library("l")
    (lib.path / "r.sym").write_text(R_SYM)
    sch = xschem.XFile(version="xschem version=3.4.5 file_version=1.2\n")
    sch.comps = [xschem.XComp("r.sym", 0, 0, 0, 0, {"name": "R1", "value": "1k"})]
    (lib.path / "top.sch").write_text(xschem.dump(sch))
    ly = kdb.Layout()
    ly.create_cell("top")
    layoutstore.write(lib.path, ly, {})
    return root, srv, Changesets(srv)


def value(srv, project="p"):
    return srv.editor(project, "l", "top", "schematic").instance("R1")["props"]["value"]


def test_apply_and_revert():
    root, srv, cs = setup()
    meta = cs.create("p", "Raise R1", "less current", "agent:t")
    P = meta["preview"]
    ed = srv.editor(P, "l", "top", "schematic")
    with ed.recording("set_props", {"id": "R1"}, "agent:t", "less current", meta["id"]):
        ed.set_props("i", "R1", {"value": "10k"})
    db = srv.db(P, "l")
    li = db.layout.layer(68, 20)
    with db.recording("create", {}, "agent:t", "stub", meta["id"]):
        db._commit("top", [("ins", li, kdb.Box(0, 0, 100, 100))])
    assert value(srv) == "1k"                                      # the real design is untouched
    g = cs.get("p", meta["id"])
    assert [(o["view"], o["op"]) for o in g["changes"][0]["ops"]] == [("schematic", "set_props"), ("layout", "create")]
    cs.apply("p", meta["id"], "user")
    assert value(srv) == "10k"
    main = srv.db("p", "l")
    assert [str(s.box) for s in main.cell("top").shapes(main.layout.layer(68, 20)).each()] == ["(0,0;100,100)"]
    recs = srv.oplog("p", "l").read(limit=100)
    assert {r["changeset"] for r in recs if r["op"] in ("set_props", "create")} == {meta["id"]}
    assert cs.get("p", meta["id"])["status"] == "applied"
    cs.revert("p", meta["id"], "user")
    assert value(srv) == "1k"
    assert not list(main.cell("top").shapes(main.layout.layer(68, 20)).each())
    assert cs.get("p", meta["id"])["status"] == "reverted"
    shutil.rmtree(root)


def test_conflicts_and_discard():
    root, srv, cs = setup()
    a, b = cs.create("p", "A"), cs.create("p", "B")
    for m, v in ((a, "2k"), (b, "3k")):
        ed = srv.editor(m["preview"], "l", "top", "schematic")
        with ed.recording("set_props", {}, "agent:t", None, m["id"]):
            ed.set_props("i", "R1", {"value": v})
    cs.apply("p", a["id"])
    with pytest.raises(MergeConflict, match="conflict") as e:
        cs.apply("p", b["id"])                                      # B was made against R1=1k, which A changed
    # the three-way merge of the schematic conflicts on R1's value (apply is a merge: a conflict names its files)
    assert any(f["path"] == "libs/l/top.sch" and f["kind"] == "schematic" for f in e.value.files)
    assert value(srv) == "2k" and cs.get("p", b["id"])["status"] == "open"   # nothing half-applied
    cs.revert("p", a["id"])
    assert value(srv) == "1k"
    c = cs.create("p", "C")
    cs.discard("p", c["id"])
    assert not (root / ".changesets" / "p" / c["id"]).exists()
    try:
        cs.apply("p", c["id"])
        raise AssertionError("applied a discarded changeset")
    except ChangesetError:
        pass
    shutil.rmtree(root)


def test_discard():
    root, srv, cs = setup()
    c = cs.create("p", "C")
    ed = srv.editor(c["preview"], "l", "top", "schematic")
    with ed.recording("set_props", {}, "agent:t", None, c["id"]):
        ed.set_props("i", "R1", {"value": "4k"})
    cs.discard("p", c["id"])
    assert not (root / ".changesets" / "p" / c["id"]).exists() and value(srv) == "1k"
    with pytest.raises(ChangesetError):
        cs.apply("p", c["id"])
    shutil.rmtree(root)


def test_layout_sync_is_tied_to_the_schematic_edits_that_caused_it():
    def op(seq, view, name, effects, cell="top"):
        return {"seq": seq, "op": name, "cell": cell, "view": view, "effects": effects}
    sch = lambda seq, i, **kw: op(seq, "schematic", "set_props", [{"k": "i", "id": i, "do": "mod", **kw}])
    sync = lambda seq, *names: op(seq, "layout", "sdl_sync", [{"k": "i", "do": d, "child": "c", "inst": n} for n in names for d in ("del", "add")])
    ops = Changesets.link_causes([
        sch(1, "M2", changes={"props.W": ["1", "2"]}), sch(2, "M2", changes={"props.L": ["1", "2"]}), sch(3, "M3", changes={"props.W": ["1", "2"]}),
        sync(4, "M2"),                                   # follows M2 only: both of its edits, not M3's
        sch(5, "M2", changes={"props.W": ["2", "3"]}),
        sync(6, "M2", "M3"),                             # M2: only the edit after the last sync; M3: its pending edit
        sync(7, "M2"),                                   # nothing new: no cause
        op(8, "layout", "create", [{"k": "i", "do": "add", "child": "c", "inst": "M2"}]),   # not a sync
    ])
    by = {o["seq"]: o.get("causes") for o in ops}
    assert by[4] == {"M2": [1, 2]} and by[6] == {"M2": [5], "M3": [3]} and by[7] is None and by[8] is None
    assert by[1] is None
    # a renamed device is followed under its new name
    ren = Changesets.link_causes([sch(1, "M2", id_after="M9", changes={"x": [0, 1]}), sync(2, "M9")])
    assert ren[1]["causes"] == {"M9": [1]}


def test_layout_diff_between_a_preview_and_the_real_design():
    from fastapi.testclient import TestClient
    from undine.server.api import create_app
    root, srv, cs = setup()
    c = TestClient(create_app(srv, None))
    meta = cs.create("p", "Draw", "r", "agent:t")
    P = meta["preview"]
    db = srv.db(P, "l")
    li = db.layout.layer(68, 20)
    with db.recording("create", {}, "agent:t", "stub", meta["id"]):
        db._commit("top", [("ins", li, kdb.Box(0, 0, 100, 100))])
    r = c.get(f"/api/p/{P}/l/l/layout/top/diff/p").json()
    assert r["dbu"] == db.layout.dbu and r["removed"] == [] and not r["truncated"]
    assert r["added"] == [{"layer": "68/20", "polys": [[[0, 0, 0, 100, 100, 100, 100, 0]]]}]
    back = c.get(f"/api/p/p/l/l/layout/top/diff/{P}").json()          # reversed: the same geometry is "removed"
    assert back["added"] == [] and back["removed"][0]["layer"] == "68/20"
    assert c.get(f"/api/p/p/l/l/layout/top/diff/p").json()["added"] == []
    shutil.rmtree(root)


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
