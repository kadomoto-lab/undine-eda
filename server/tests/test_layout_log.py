"""Layout operations are logged, undoable from the log, and survive a restart (journal)."""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import klayout.db as kdb  # noqa: E402
from undine.layouts import layout, layoutstore  # noqa: E402
from undine.layouts.layout import LayoutDB  # noqa: E402
from undine.store.oplog import OpLog  # noqa: E402
from undine.store.project import Library  # noqa: E402
from undine.vcs import logmerge  # noqa: E402


def boxes(db, cell="top"):
    c = db.cell(cell)
    out = []
    for li in db.layout.layer_indexes():
        for s in c.shapes(li).each():
            out.append((db._layer_key(li), str(s.box)))
    return sorted(out)


def fresh():
    d = Path(tempfile.mkdtemp())
    lib = Library("t", d)
    log = OpLog(d / "history" / "t.jsonl")
    db = LayoutDB(lib, None, log)
    db.new_cell("top")
    db.new_cell("sub")
    db.save()
    return d, lib, log, db


def test_effects_undo_redo_and_attribution():
    d, lib, log, db = fresh()
    li = db.layout.layer(66, 20)
    with db.recording("create", {"cell": "top"}, "agent:t", "gate poly", "cs-1"):
        db._commit("top", [("ins", li, kdb.Box(0, 0, 100, 200))])
    with db.recording("place", {"cell": "top"}):
        db._commit("top", [("iins", kdb.CellInstArray(db.cell("sub").cell_index(), kdb.Trans(1, False, 10, 20)),
                            db.layout.properties_id({"undine_inst": "M1"}))])
    rec = log.read(view="layout")[-2]
    assert rec["actor"] == "agent:t" and rec["reason"] == "gate poly" and rec["changeset"] == "cs-1"
    assert rec["effects"] == [{"k": "s", "do": "add", "cell": "top", "layer": "66/20", "t": "box", "s": "(0,0;100,200)"}]
    inst = log.read(view="layout")[-1]["effects"][0]
    assert inst["child"] == "sub" and inst["inst"] == "M1" and inst["trans"] == "r90 10,20"
    assert db.undo() == "top" and not list(db.cell("top").each_inst())
    assert db.undo() == "top" and boxes(db) == []
    assert db.redo() == "top" and len(boxes(db)) == 1
    assert db.redo() == "top" and db.named_instances("top")[0]["name"] == "M1"
    shutil.rmtree(d)



def test_undo_in_a_cell_never_takes_back_another_cells_edit():
    """demo, 10/24: an extra Ctrl+Z in a PCell cell took back a via placed in the Tiny Tapeout tile the day before,
    unseen, and the tile lost its VDD connection. An editor undoes the operations of its own cell only."""
    d, lib, log, db = fresh()
    li = db.layout.layer(66, 20)
    with db.recording("place", {"cell": "top"}):                     # yesterday, in the tile
        db._commit("top", [("iins", kdb.CellInstArray(db.cell("sub").cell_index(), kdb.Trans(0, 0)), 0)])
    with db.recording("create", {"cell": "sub"}):                    # today, in another cell
        db._commit("sub", [("ins", li, kdb.Box(0, 0, 10, 10))])
    assert db.can_undo_redo("sub") == (2, 0) and db.can_undo_redo("top") == (2, 0)   # each: its creation + one edit
    assert db.undo("sub") == "sub" and boxes(db, "sub") == []
    assert db.undo("sub") == "sub" and db.cell("sub") is not None       # its creation (a cell is never removed)
    assert db.undo("sub") is None                                     # nothing left in sub: the tile is untouched
    assert len(list(db.cell("top").each_inst())) == 1
    assert db.redo("sub") == "sub" and db.redo("sub") == "sub" and len(boxes(db, "sub")) == 1
    assert db.can_undo_redo("sub") == (2, 0)
    # the operations of each cell stay in order; the library-wide stack (no cell) still exists for scripts
    assert db.undo("top") == "top" and not list(db.cell("top").each_inst())
    assert db.undo() == "sub" and boxes(db, "sub") == []
    # undo records follow their cell, so a restart replays the same state
    db2 = LayoutDB(lib, None, log)
    assert boxes(db2, "sub") == [] and not list(db2.cell("top").each_inst())
    assert db2.redo("top") == "top" and len(list(db2.cell("top").each_inst())) == 1
    shutil.rmtree(d)


def test_unsaved_edits_and_undo_survive_restart():
    d, lib, log, db = fresh()
    li = db.layout.layer(66, 20)
    db._commit("top", [("ins", li, kdb.Box(0, 0, 100, 200))])
    db._commit("top", [("ins", li, kdb.Box(300, 0, 400, 200))])
    db.save()
    db._commit("top", [("ins", li, kdb.Box(600, 0, 700, 200))])      # unsaved
    db.undo()                                                        # unsaved undo of that one ...
    db.undo()                                                        # ... and of a saved one
    want = boxes(db)
    db2 = LayoutDB(lib, None, OpLog(d / "history" / "t.jsonl"))       # = server restart
    assert boxes(db2) == want == [("66/20", "(0,0;100,200)")] and db2.dirty
    assert db2.redo() == "top" and len(boxes(db2)) == 2               # undo history is intact
    shutil.rmtree(d)


def test_a_git_merge_record_is_a_save_for_the_journal(capsys):
    """A git merge writes the merged cell files and continues the log with a merge record, a boundary like a save: a
    restart replays the records the merge record lists in "replay" (a branch's journal the merge kept pending on that
    branch's files, vcs/logmerge.py), then what follows it. The edits before it that it does not list are in the merged
    files already; replayed again, they were there twice."""
    d, lib, log, db = fresh()
    li = db.layout.layer(66, 20)
    db._commit("top", [("ins", li, kdb.Box(0, 0, 100, 200))])        # unsaved ...
    layoutstore.write(d, db.layout, db.saved)                         # ... and in the files the merge wrote
    db._commit("top", [("ins", li, kdb.Box(300, 0, 400, 200))])      # unsaved and not in the files: listed
    db._commit("top", [("ins", li, kdb.Box(600, 0, 700, 200))])
    assert db.undo("top") == "top"                                   # 600 taken back: listed with its undo
    listed = [r["seq"] for r in log.read(view="layout")[-3:]]
    log.append({"op": "merge", "actor": "git", "cell": None, "view": None, "effects": [],
                "replay": [*listed, 999]})                            # 999: no such record, reported and skipped
    db._commit("top", [("ins", li, kdb.Box(900, 0, 1000, 200))])     # after the merge: the journal
    want = [("66/20", "(0,0;100,200)"), ("66/20", "(300,0;400,200)"), ("66/20", "(900,0;1000,200)")]
    db2 = LayoutDB(lib, None, OpLog(d / "history" / "t.jsonl"))
    assert boxes(db2) == boxes(db) == want and db2.dirty
    assert "cannot replay layout operation 999" in capsys.readouterr().err
    # the records the git hook and the merge driver read as the journal are the ones replayed
    assert [r["seq"] for r in logmerge.journal(log.path.read_bytes())] == [*listed, log.read()[-1]["seq"]]
    # a save after the merge is the boundary again
    db2.save()
    db2._commit("top", [("ins", li, kdb.Box(1200, 0, 1300, 200))])
    assert len(boxes(LayoutDB(lib, None, OpLog(d / "history" / "t.jsonl")))) == 4
    # a merge record that lists nothing: the files have every edit before it
    log.append({"op": "merge", "actor": "git", "cell": None, "view": None, "effects": []})
    assert len(boxes(LayoutDB(lib, None, OpLog(d / "history" / "t.jsonl")))) == 3
    shutil.rmtree(d)


def test_versions_are_never_given_twice():
    """The browser caches tiles under the layout version for an hour: a library loaded again (a server restart, a
    changeset step put back) counted from 0 again, and the screen showed what the earlier load drew under that number."""
    d, lib, log, db = fresh()
    li = db.layout.layer(66, 20)
    seen = [db.version]
    for x in range(3):
        db._commit("top", [("ins", li, kdb.Box(x * 100, 0, x * 100 + 50, 50))])
        seen.append(db.version)
    assert seen == list(range(seen[0], seen[0] + 4))                  # one up per change
    db2 = LayoutDB(lib, None, OpLog(d / "history" / "t.jsonl"))       # loaded again, same content
    assert db2.version > max(seen)
    db2._commit("top", [("ins", li, kdb.Box(0, 100, 50, 150))])
    assert db2.version not in seen
    assert layout.VERSIONS.__class__().first() > db2.version         # another process starts above this one
    shutil.rmtree(d)


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)


def test_save_reports_the_cells_it_wrote():
    d, lib, log, db = fresh()
    li = db.layout.layer(66, 20)
    db._commit("top", [("ins", li, kdb.Box(0, 0, 100, 200))])
    assert db.save() == {"written": ["top"], "removed": []}           # the edited cell, not the whole library
    assert db.save() == {"written": [], "removed": []}
    shutil.rmtree(d)


def test_cells_generated_for_devices_are_flagged():
    d, lib, log, db = fresh()
    dev = "sky130_fd_pr__nfet_01v8_w2p0_l0p5_nf4_ed09c5"
    db.new_cell(dev)
    db.new_cell(dev + "_par")
    db.new_cell(dev.replace("ed09c5", "16f291"))                     # left over after a regeneration: unplaced
    db.place(dev + "_par", dev, 0, 0)
    db.place("top", dev + "_par", 0, 0)
    db.save()
    (d / "amp_abc123.sch").write_text("v {xschem version=3.4.5 file_version=1.2}\n")   # the pattern, but a schematic
    flags = {c["name"]: c["device"] for c in lib.cells()}
    assert flags[dev] and flags[dev + "_par"] and flags[dev.replace("ed09c5", "16f291")]
    assert not flags["top"] and not flags["sub"] and not flags["amp_abc123"]
    shutil.rmtree(d)
