"""Live changesets, PR 2: the timeline and restoring a changeset's working copy to a checkpoint."""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import klayout.db as kdb  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from undine.agents import agent  # noqa: E402
from undine.layouts import layoutstore
from undine.server import live
from undine.agents import agent_tools as T  # noqa: E402
from undine.server.api import create_app  # noqa: E402
from undine.store.changeset import ChangesetError  # noqa: E402
import test_changeset as tc  # noqa: E402


def setup():
    root, srv, cs = tc.setup()
    meta = cs.create("p", "Try things", "r", "user")
    return root, srv, cs, meta["id"], meta["preview"]


def sch(srv, P, cell="top"):
    return srv.editor(P, "l", cell, "schematic")


def val(srv, P, inst="R1"):
    return sch(srv, P).instance(inst)["props"]["value"]


def last(srv, P):
    return max(r.get("gseq") or 0 for lib in ("l",) for r in srv.oplog(P, lib)._read_all())


def test_restore_rebuilds_the_state_at_a_checkpoint_and_apply_follows_it():
    root, srv, cs, cid, P = setup()
    sch(srv, P).set_props("i", "R1", {"value": "2k"})                     # gseq 1: the checkpoint
    g = last(srv, P)
    sch(srv, P).set_props("i", "R1", {"value": "3k"})
    sch(srv, P).add_instance("r.sym", 100, 0, 0, 0, {"name": "R9", "value": "9k"})
    out = cs.restore("p", cid, g, "user")
    assert val(srv, P) == "2k" and "R9" not in [c.props.get("name") for c in sch(srv, P)._load().comps]
    ops = [o["op"] for o in out["changes"][0]["ops"]]
    assert ops == ["edit"]                                             # only what is in effect at the checkpoint
    # undo after a restore takes back the checkpoint's own operation, not the restored-away ones
    assert sch(srv, P).undo() and val(srv, P) == "1k"
    assert sch(srv, P).redo() and val(srv, P) == "2k"
    cs.apply("p", cid, "user")
    assert val(srv, "p") == "2k" and "R9" not in [c.props.get("name") for c in sch(srv, "p")._load().comps]
    shutil.rmtree(root)


def test_restore_drops_a_cell_brought_in_from_a_file():
    """A PCell variant comes in with its content (LayoutDB.import_oas, as sync_layout and place_pcell bring one):
    restoring to before it drops the cell, which is as it came. It was refused as "not empty" before."""
    root, srv, cs, cid, P = setup()
    sch(srv, P).set_props("i", "R1", {"value": "2k"})
    g = last(srv, P)
    src = kdb.Layout()
    src.create_cell("unit_abc").shapes(src.layer(68, 20)).insert(kdb.Box(0, 0, 1000, 1000))
    src.write(str(root / "unit.oas"))
    assert srv.db(P, "l").import_oas(root / "unit.oas", {"pcell": "unit", "params": {}}) == ["unit_abc"]
    cs.restore("p", cid, g, "user")
    assert srv.db(P, "l").layout.cell("unit_abc") is None and val(srv, P) == "2k"
    shutil.rmtree(root)


def test_an_addition_undone_after_the_checkpoint_comes_back():
    root, srv, cs, cid, P = setup()
    sch(srv, P).add_instance("r.sym", 100, 0, 0, 0, {"name": "R9", "value": "9k"})
    g = last(srv, P)                                                        # checkpoint: R9 is there
    sch(srv, P).undo()                                                      # ... then taken back
    sch(srv, P).set_props("i", "R1", {"value": "5k"})                      # a new edit drops the redo stack
    assert not sch(srv, P).redo()
    cs.restore("p", cid, g, "user")
    assert sch(srv, P).instance("R9")["props"]["value"] == "9k" and val(srv, P) == "1k"
    assert [o["op"] for o in cs.get("p", cid)["changes"][0]["ops"]] == ["edit"]
    shutil.rmtree(root)


def test_restore_covers_every_library_and_can_go_back_past_an_earlier_restore():
    root, srv, cs, cid, P = setup()
    lib2 = srv.project("p").create_library("m")
    ly = kdb.Layout()
    ly.create_cell("blk")
    layoutstore.write(lib2.path, ly, {})
    cs.discard("p", cid)
    cid = cs.create("p", "Two libraries", "r", "user")["id"]
    P = f"p~{cid}"
    db = srv.db(P, "m")
    li = db.layout.layer(68, 20)
    sch(srv, P).set_props("i", "R1", {"value": "2k"})                       # 1
    with db.recording("create", {}, "user", None, cid):
        db._commit("blk", [("ins", li, kdb.Box(0, 0, 10, 10))])             # 2
    with db.recording("create", {}, "user", None, cid):
        db._commit("blk", [("ins", li, kdb.Box(20, 0, 30, 10))])            # 3
    sch(srv, P).set_props("i", "R1", {"value": "3k"})                       # 4
    shapes = lambda: sorted(str(s.box) for s in srv.db(P, "m").cell("blk").shapes(srv.db(P, "m").layout.layer(68, 20)).each())
    cs.restore("p", cid, 1, "user")                                         # back to: R1 2k, no shapes
    assert val(srv, P) == "2k" and shapes() == []
    sch(srv, P).set_props("i", "R1", {"value": "7k"})
    tl = cs.timeline("p", cid)
    assert [o["kind"] for o in tl["ops"]] == ["edit", "edit", "edit", "edit", "restore", "edit"]
    assert [o["in_effect"] for o in tl["ops"] if o["kind"] == "edit"] == [True, False, False, False, True]
    cs.restore("p", cid, 3, "user")                                         # past the earlier restore: both shapes, R1 2k
    assert val(srv, P) == "2k" and shapes() == ["(0,0;10,10)", "(20,0;30,10)"]
    # the layout survives a reload from disk + journal (as after a server restart)
    cs._forget("p", cid)
    assert shapes() == ["(0,0;10,10)", "(20,0;30,10)"]
    changed = {(lc["lib"], o["op"]) for lc in cs.get("p", cid)["changes"] for o in lc["ops"]}
    assert changed == {("l", "edit"), ("m", "create")}
    shutil.rmtree(root)


def test_cells_created_after_the_checkpoint_are_removed():
    root, srv, cs, cid, P = setup()
    ctx = T.ToolCtx(srv, "p", actor="agent:t", changeset=cid)
    sch(srv, P).set_props("i", "R1", {"value": "2k"})
    g = last(srv, P)
    assert not T.call(ctx, "create_cell", {"lib": "l", "cell": "ota", "views": ["schematic", "layout"], "reason": "new"})[1]
    assert not T.call(ctx, "add_instance", {"lib": "l", "cell": "ota", "symbol": "r.sym", "params": {"name": "RL"}, "reason": "load"})[1]
    work = srv.project(P).library("l")
    assert (work.path / "ota.sch").is_file() and srv.db(P, "l").layout.cell("ota") is not None
    cs.restore("p", cid, g, "user")
    assert not (work.path / "ota.sch").exists() and srv.db(P, "l").layout.cell("ota") is None
    assert not T.call(ctx, "create_cell", {"lib": "l", "cell": "ota", "views": ["schematic"], "reason": "again"})[1]   # the name is free
    shutil.rmtree(root)


def test_a_failed_restore_changes_nothing():
    root, srv, cs, cid, P = setup()
    ctx = T.ToolCtx(srv, "p", actor="agent:t", changeset=cid)
    sch(srv, P).set_props("i", "R1", {"value": "2k"})
    g = last(srv, P)
    sch(srv, P).set_props("i", "R1", {"value": "3k"})
    T.call(ctx, "create_cell", {"lib": "l", "cell": "ota", "reason": "new"})
    sota = srv.project(P).library("l").path / "ota.sch"
    sota.write_text(sota.read_text() + "T {edited outside} 0 0 0 0 0.2 0.2 {}\n")   # not in the log
    before = {r["seq"] for r in srv.oplog(P, "l")._read_all()}
    with pytest.raises(ChangesetError, match="nothing was changed"):
        cs.restore("p", cid, g, "user")
    assert val(srv, P) == "3k" and sota.is_file()                           # the edit before the failing step is undone too
    assert {r["seq"] for r in srv.oplog(P, "l")._read_all()} == before      # and nothing was recorded
    with pytest.raises(ChangesetError, match="not a point"):
        cs.restore("p", cid, 999, "user")
    shutil.rmtree(root)


def test_restore_is_refused_while_someone_else_writes_and_the_assistant_is_told():
    root, srv, cs, cid, P = setup()
    c = TestClient(create_app(srv, None))
    with live.operation(turn="t-1"), live.acting("turn:t-1"):
        sch(srv, P).set_props("i", "R1", {"value": "2k"})
    g = last(srv, P)
    sch(srv, P).set_props("i", "R1", {"value": "3k"})
    srv.leases.acquire(cid, "turn:t-9", "turn")
    assert c.post(f"/api/p/p/changesets/{cid}/restore", json={"to": g}).status_code == 400
    srv.leases.release(cid, "turn:t-9")
    ctx = T.ToolCtx(srv, "p", actor="agent:assistant", changeset=cid)
    _, seen = agent._since(ctx, {})                                         # the assistant has seen everything so far
    r = c.post(f"/api/p/p/changesets/{cid}/restore", json={"to": g})
    assert r.status_code == 200 and val(srv, P) == "2k"
    text, _ = agent._since(ctx, seen)
    assert "restored the changeset to checkpoint" in text and "get_changeset" in text
    tl = c.get(f"/api/p/p/changesets/{cid}/timeline").json()
    assert tl["ops"][0]["turn"] == "t-1" and tl["ops"][-1]["kind"] == "restore" and tl["checkpoints"][0] == 0
    shutil.rmtree(root)
