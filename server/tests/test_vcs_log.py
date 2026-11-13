"""Merge and diff of the operation log (vcs/logmerge.py, docs/design/merge.md): both branches' operations combined,
theirs renumbered after ours with their undo / redo references, a merge record that stops the undo / redo stacks, no
operation taken twice when a branch is merged back into the one it was updated from, and the unsaved layout edits (the
journal) carried over only onto the cell files they were recorded on, else a conflict, or a warning for those a merge
drops without one (a revert)."""
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import klayout.db as kdb  # noqa: E402

from undine.layouts.layout import LayoutDB  # noqa: E402
from undine.store.oplog import OpLog, net_records  # noqa: E402
from undine.store.project import Library  # noqa: E402
from undine.vcs import layoutmerge, logmerge  # noqa: E402

M1_W = {"k": "i", "id": "M1", "do": "mod", "id_after": "M1", "changes": {"props.W": ["1", "2"]}}
SAVE = {"op": "save", "cell": None, "view": "layout", "effects": None}


def rec(seq: int, op: str = "set_props", cell: str | None = "inv", view: str | None = "schematic",
        t: str = "2026-10-23T10:00:00+0900", actor: str = "user", reason: str | None = None, **kw) -> dict:
    r = {"v": 1, "seq": seq, "t": t, "op": op, "args": {}, "actor": actor, "reason": reason, "changeset": None,
         "cell": cell, "view": view, "sha": f"{seq:040x}", "effects": [M1_W]}
    r.update(kw)
    return r


def undo(seq: int, of: int, **kw) -> dict:
    return rec(seq, "undo", of=of, effects=None, **kw)


def jsonl(*recs: dict) -> bytes:
    return b"".join(json.dumps(r, separators=(",", ":")).encode() + b"\n" for r in recs)


def parse(data: bytes) -> list[dict]:
    return [json.loads(line) for line in data.decode().splitlines() if line.strip()]


def seqs(data: bytes) -> list[int]:
    """The seq numbers of a log's journal (logmerge.journal: what LayoutDB replays)."""
    return [r["seq"] for r in logmerge.journal(data)]


BASE = [rec(1, reason="size the inverter"), rec(2, cell="inv", view="layout", op="create", t="2026-10-23T10:05:00+0900")]
OURS = [*BASE, rec(3, reason="widen M1", t="2026-10-23T11:00:00+0900"),
        rec(4, cell="buf", t="2026-10-23T02:30:00+0000")]                     # = 11:30 in Tokyo: the latest
THEIRS = [*BASE, rec(3, cell="inv", view="layout", op="create", actor="agent:route", t="2026-10-23T10:30:00+0900"),
          undo(4, 3, view="layout", actor="agent:route", t="2026-10-23T10:31:00+0900"),
          undo(5, 1, actor="agent:route", t="2026-10-23T11:00:00+0900")]      # an operation of the common part


def test_both_sides_append_ours_then_theirs_renumbered_then_a_merge_record(tmp_path):
    ours, theirs = jsonl(*OURS), jsonl(*THEIRS)
    res = logmerge.merge(jsonl(*BASE), ours, theirs)
    assert res.kind == "log" and res.clean and res.conflicts == [] and res.warnings == []
    assert res.data.startswith(ours)                                          # ours' lines as they were
    out = parse(res.data)
    assert [r["seq"] for r in out] == list(range(1, 9))
    assert out[:4] == OURS
    assert [r.get("merged_seq") for r in out[4:7]] == [3, 4, 5]
    assert {k: v for k, v in out[4].items() if k not in ("seq", "merged_seq")} == {
        k: v for k, v in THEIRS[2].items() if k != "seq"}
    assert out[5]["op"] == "undo" and out[5]["of"] == 5                       # theirs' own operation: follows it
    assert out[6]["op"] == "undo" and out[6]["of"] == 1                       # an operation both had: unchanged
    # ours changed no layout, so the merged cell files are theirs': theirs' journal (the base's unsaved edit, then
    # theirs' edit and its undo) stays pending, listed by the merge record, not copied
    assert out[7] == {"v": 1, "seq": 8, "t": "2026-10-23T02:30:00+0000", "op": "merge", "actor": "git",
                      "cell": None, "view": None, "effects": [],
                      "reason": "merged 3 operations of the other branch", "replay": [2, 5, 6]}
    assert seqs(res.data) == [2, 5, 6]
    assert res.combined == ["3 operations of the other branch: inv (layout), inv (schematic), numbered from 5",
                            "layout: ours did not change it, so the merged cell files are theirs', and theirs' "
                            "3 unsaved edits stay pending: inv (layout)"]
    # what is in effect, read by the server: theirs' undo took back theirs' layout edit and the shared first edit
    path = tmp_path / "history" / "t.jsonl"
    path.parent.mkdir()
    path.write_bytes(res.data)
    assert [r["seq"] for r in net_records(OpLog(path))] == [2, 3, 4]
    assert OpLog(path).append({"op": "save", "cell": None, "view": "layout"})["seq"] == 9


def test_the_merged_log_undoes_nothing_from_before_the_merge(tmp_path):
    base = [rec(1)]
    ours = [*base, rec(2, reason="widen M1"), undo(3, 2)]                     # ours could redo 2
    theirs = [*base, rec(2, cell="buf")]
    path = tmp_path / "t.jsonl"
    path.write_bytes(logmerge.merge(jsonl(*base), jsonl(*ours), jsonl(*theirs)).data)
    log = OpLog(path)
    assert log.stacks("inv", "schematic") == ([], [])                        # was ([1], [2]) on ours
    assert log.stacks("buf", "schematic") == ([], [])
    assert log.stacks(None, "schematic") == ([], [])
    # an edit after the merge is undone and redone as usual
    e = log.append({"op": "set_props", "actor": "user", "cell": "inv", "view": "schematic", "effects": [M1_W]})
    assert e["seq"] == 6 and log.stacks("inv", "schematic") == ([e], [])
    log.append({"op": "undo", "of": e["seq"], "actor": "user", "cell": "inv", "view": "schematic", "effects": None})
    assert log.stacks("inv", "schematic") == ([], [e])
    log.append({"op": "redo", "of": e["seq"], "actor": "user", "cell": "inv", "view": "schematic", "effects": None})
    assert log.stacks("inv", "schematic") == ([e], [])


def test_the_journal_rules():
    """Which journal the merged log carries: the side whose cell files the merged ones are, when the other side changed
    no layout; nothing when both changed it and saved; else the conflict "layout-journal", decided either way to the
    same log, which carries no journal."""
    base = [rec(1, **SAVE)]
    edit_o = rec(2, view="layout", op="create")
    edit_t = rec(2, view="layout", op="create", actor="agent:x")
    sch_t = rec(2, cell="buf")
    # theirs changed the schematic only: the merged files are ours', ours' journal stays pending
    res = logmerge.merge(jsonl(*base), jsonl(*base, edit_o), jsonl(*base, sch_t))
    assert res.clean and parse(res.data)[-1]["replay"] == [2] and seqs(res.data) == [2]
    assert len(res.combined) == 1                                              # ours' journal is nothing of theirs
    # ours did not change the layout: theirs' journal, as the merged log numbers it
    res = logmerge.merge(jsonl(*base), jsonl(*base, rec(2, cell="buf")), jsonl(*base, edit_t))
    assert res.clean and parse(res.data)[-1]["replay"] == [3] and seqs(res.data) == [3]
    # both changed it and saved: nothing pending (and no "replay")
    saved_o, saved_t = jsonl(*base, edit_o, rec(3, **SAVE)), jsonl(*base, edit_t, rec(3, **SAVE))
    res = logmerge.merge(jsonl(*base), saved_o, saved_t)
    assert res.clean and res.conflicts == [] and "replay" not in parse(res.data)[-1] and seqs(res.data) == []
    # both changed it, a journal is not empty: a conflict, whichever side has it, which says what the merged log does
    for ours, theirs, sides in ((jsonl(*base, edit_o), saved_t, "ours"), (saved_o, jsonl(*base, edit_t), "theirs"),
                                (jsonl(*base, edit_o), jsonl(*base, edit_t), "both sides")):
        res = logmerge.merge(jsonl(*base), ours, theirs)
        assert not res.clean and [c.id for c in res.conflicts] == ["layout-journal"]
        c = res.conflicts[0]
        assert c.what == (f"unsaved layout edits of {sides} are dropped by this merge (the merged layout is the merge "
                          "of the saved files): git merge --abort, save the layouts on each branch, commit, and merge "
                          "again; or resolve it to accept dropping them")
        assert c.base is None and c.taken == "ours"
        assert (c.ours is not None, c.theirs is not None) == (sides != "theirs", sides != "ours")
        assert "replay" not in parse(res.data)[-1] and seqs(res.data) == []    # no journal carried over
        assert res.warnings == []                                              # the conflict reports them
        for side in ("ours", "theirs"):
            decided = logmerge.merge(jsonl(*base), ours, theirs, prefer={"layout-journal": side})
            assert decided.clean and decided.data == res.data and decided.conflicts[0].taken == side
            assert decided.warnings == []
    c = res.conflicts[0]
    assert c.ours == ["seq:2 create inv (layout) by user"] and c.theirs == ["seq:2 create inv (layout) by agent:x"]


def _boxes(db: LayoutDB, cell: str = "top") -> list[tuple[str, str]]:
    c = db.cell(cell)
    return sorted((db._layer_key(li), str(s.box)) for li in db.layout.layer_indexes() for s in c.shapes(li).each())


def _db(d: Path) -> LayoutDB:
    return LayoutDB(Library("t", d), None, OpLog(d / "history" / "t.jsonl"))


def _lib(d: Path) -> Path:
    d.mkdir(parents=True)
    db = _db(d)
    db.new_cell("top")
    db.save()
    return d


def _draw(db: LayoutDB, *xs: int) -> None:
    li = db.layout.layer(68, 20)
    for x in xs:
        db._commit("top", [("ins", li, kdb.Box(x, 0, x + 100, 100))])


def _sch(d: Path, cell: str = "top") -> None:
    """A schematic edit: its record is all the log merge sees of it."""
    OpLog(d / "history" / "t.jsonl").append({"op": "set_props", "actor": "user", "cell": cell, "view": "schematic",
                                             "effects": [M1_W]})


def _at(*xs: int) -> list[tuple[str, str]]:
    return sorted(("68/20", f"({x},0;{x + 100},100)") for x in xs)


def _log(d: Path) -> bytes:
    return (d / "history" / "t.jsonl").read_bytes()


def _merge_lib(base: Path, ours: Path, theirs: Path, out: Path, prefer: dict | None = None):
    """What git and the merge driver make of a library: a cell file both sides changed merged by layoutmerge, else the
    changed side's; the log by logmerge."""
    shutil.copytree(ours, out)
    for name in sorted({p.name for d in (base, ours, theirs) for p in d.glob("*.oas")}):
        b, o, t = ((d / name).read_bytes() if (d / name).is_file() else None for d in (base, ours, theirs))
        if o == t or t == b:
            continue
        if o == b:
            data = t
        else:
            res = layoutmerge.merge(b, o, t)
            assert res.clean, res.conflicts
            data = res.data
        (out / name).write_bytes(data)
    res = logmerge.merge(_log(base), _log(ours), _log(theirs), prefer)
    (out / "history" / "t.jsonl").write_bytes(res.data)
    return res


def _base_with_a_journal(d: Path) -> Path:
    """A library with a saved box (-400) and an unsaved one (-200), copied to ours and theirs."""
    base = _lib(d / "base")
    db = _db(base)
    _draw(db, -400)
    db.save()
    _draw(db, -200)
    shutil.copytree(base, d / "ours")
    shutil.copytree(base, d / "theirs")
    return base


def test_ours_unsaved_layout_edits_stay_pending_when_theirs_changed_the_schematic_only(tmp_path):
    """Theirs changed no layout: the merged cell files are ours', and ours' journal replayed on them gives ours'
    layout, whatever it holds (here a standalone redo onto files that lack the edit, after a save that dropped the base's
    unsaved box). The merge copies no record: it lists them."""
    base = _base_with_a_journal(tmp_path)
    o = _db(tmp_path / "ours")
    assert o.undo("top") == "top" and o.undo("top") == "top"                # -200 (unsaved), -400 (saved) taken back
    o.save()
    assert o.redo("top") == "top"                                             # -400 again: the files lack it
    _draw(o, 0)
    assert _boxes(o) == _at(-400, 0)
    _sch(tmp_path / "theirs")
    res = _merge_lib(base, tmp_path / "ours", tmp_path / "theirs", tmp_path / "merged")
    assert res.clean and res.combined == ["1 operation of the other branch: top (schematic), numbered from 11"]
    assert res.warnings == []                         # theirs' journal (the base's -200): ours took it back and saved
    out = parse(res.data)
    assert len(out) == len(parse(_log(tmp_path / "ours"))) + 2                # theirs' edit and the merge record
    assert out[-1]["op"] == "merge" and out[-1]["replay"] == [9, 10] == seqs(_log(tmp_path / "ours"))
    assert [(out[s - 1]["op"], out[s - 1].get("of")) for s in out[-1]["replay"]] == [("redo", 3), ("edit", None)]
    m = _db(tmp_path / "merged")
    assert _boxes(m) == _at(-400, 0) and m.dirty
    assert _boxes(_db(tmp_path / "merged")) == _at(-400, 0)                   # opened again: the same, once
    m.save()
    assert _boxes(_db(tmp_path / "merged")) == _at(-400, 0) and not _db(tmp_path / "merged").dirty


def test_theirs_unsaved_layout_edits_stay_pending_when_ours_changed_the_schematic_only(tmp_path):
    """The mirror: the merged cell files are theirs', and theirs' journal (the base's unsaved box, which theirs took
    back, an undo of a saved box, two boxes and an undo) is listed as the merged log numbers it. The base's box theirs
    removed does not come back; ours' journal (the base's unsaved box) is not replayed."""
    base = _base_with_a_journal(tmp_path)
    th = _db(tmp_path / "theirs")
    assert th.undo("top") == "top" and th.undo("top") == "top"              # -200 (unsaved), -400 (saved) taken back
    _draw(th, 200, 400)
    assert th.undo("top") == "top"                                            # 400 again
    assert _boxes(th) == _at(200)
    _sch(tmp_path / "ours")
    res = _merge_lib(base, tmp_path / "ours", tmp_path / "theirs", tmp_path / "merged")
    assert res.clean
    assert res.combined[-1] == ("layout: ours did not change it, so the merged cell files are theirs', and theirs' "
                                "6 unsaved edits stay pending: top (layout)")
    out = parse(res.data)
    assert out[-1]["replay"] == [5, 7, 8, 9, 10, 11] == seqs(res.data)         # 5: the base's, in the common part
    assert [r.get("merged_seq") for r in out[6:11]] == [6, 7, 8, 9, 10]       # theirs' own, renumbered
    assert out[out[-1]["replay"][2] - 1]["of"] == 3                           # theirs' undo of the saved -400
    m = _db(tmp_path / "merged")
    assert _boxes(m) == _at(200) and m.dirty
    assert _boxes(_db(tmp_path / "merged")) == _at(200)


def test_both_sides_changed_the_layout_with_unsaved_edits_is_a_conflict(tmp_path):
    """The merged cell files are then a merge of both sides' saved files, which no journal was recorded on: the log
    is a conflict, the merged log carries no journal (the unsaved edits are dropped), and deciding it either way gives
    that same log."""
    base = _lib(tmp_path / "base")
    shutil.copytree(base, tmp_path / "ours")
    shutil.copytree(base, tmp_path / "theirs")
    o = _db(tmp_path / "ours")
    _draw(o, 0)
    o.save()
    th = _db(tmp_path / "theirs")
    _draw(th, 200)
    th.save()
    _draw(th, 400)                                                            # unsaved
    res = _merge_lib(base, tmp_path / "ours", tmp_path / "theirs", tmp_path / "merged")
    assert not res.clean and [(c.id, c.taken, c.resolved) for c in res.conflicts] == [("layout-journal", "ours", False)]
    assert res.conflicts[0].ours is None and res.conflicts[0].theirs == ["seq:5 edit top (layout) by user"]
    assert "replay" not in parse(res.data)[-1] and seqs(res.data) == []
    m = _db(tmp_path / "merged")
    assert _boxes(m) == _at(0, 200) and not m.dirty                           # the saved files, nothing replayed
    decided = logmerge.merge(_log(base), _log(tmp_path / "ours"), _log(tmp_path / "theirs"),
                             prefer={"layout-journal": "theirs"})
    assert decided.clean and decided.data == res.data and decided.conflicts[0].resolved


def test_both_sides_saved_their_layouts_merges_clean_with_nothing_pending(tmp_path):
    base = _lib(tmp_path / "base")
    shutil.copytree(base, tmp_path / "ours")
    shutil.copytree(base, tmp_path / "theirs")
    for side, x in (("ours", 0), ("theirs", 200)):
        db = _db(tmp_path / side)
        _draw(db, x)
        db.save()
    res = _merge_lib(base, tmp_path / "ours", tmp_path / "theirs", tmp_path / "merged")
    assert res.clean and res.conflicts == [] and res.warnings == []
    assert "replay" not in parse(res.data)[-1] and seqs(res.data) == []
    m = _db(tmp_path / "merged")
    assert _boxes(m) == _at(0, 200) and not m.dirty


def test_a_branch_merged_back_carries_nothing_twice(tmp_path):
    """main draws an unsaved box while feature changes a schematic; feature is updated from main (main's journal is
    pending on feature, listed by the update's merge record), draws, and is merged back into main, which changed a
    schematic meanwhile: main's box is the same operation as feature's copy of it, so the merged library has it once,
    and once more is not an operation of feature's own."""
    for feature_saves in (False, True):
        d = tmp_path / f"{feature_saves:d}"
        c = _lib(d / "c")
        shutil.copytree(c, d / "main")
        shutil.copytree(c, d / "feature")
        _draw(_db(d / "main"), 0)
        _sch(d / "feature")
        shutil.copytree(d / "main", d / "m0")                                 # main when feature is updated from it
        up = _merge_lib(c, d / "feature", d / "main", d / "feature2")
        assert up.clean and parse(up.data)[-1]["replay"] == [4]               # main's box, copied as theirs' record
        f = _db(d / "feature2")
        assert _boxes(f) == _at(0)
        _draw(f, 400)
        if feature_saves:
            f.save()
        _sch(d / "main")
        res = _merge_lib(d / "m0", d / "main", d / "feature2", d / "merged")
        assert res.clean and res.conflicts == []
        assert res.warnings == []                     # main's box: pending on feature, or saved there
        n = 4 if feature_saves else 3                                         # schematic, merge record, box (, save)
        assert res.combined[0].startswith(f"{n} operations of the other branch")
        out = parse(res.data)
        assert len(out) == len(parse(_log(d / "main"))) + n + 1
        assert out[-1].get("replay", []) == ([] if feature_saves else [3, 7])  # 3: main's own record of its box
        assert _boxes(_db(d / "merged")) == _at(0, 400), feature_saves
        assert _boxes(_db(d / "merged")) == _at(0, 400), feature_saves


def test_a_merge_record_copied_by_an_update_is_not_taken_again():
    """A merge record's "replay" lists seq numbers, which a copy of it has renumbered: it is compared as the records
    it lists, so the copy an update made of main's merge record is main's merge record."""
    common = [rec(1)]
    feature0 = [*common, rec(2, cell="f1", view="layout", op="create")]      # unsaved
    main = parse(logmerge.merge(jsonl(*common), jsonl(*common, rec(2, cell="m1")), jsonl(*feature0)).data)
    assert [(r["seq"], r["cell"], r.get("replay")) for r in main] == [(1, "inv", None), (2, "m1", None),
                                                                        (3, "f1", None), (4, None, [3])]
    feature = parse(logmerge.merge(jsonl(*feature0), jsonl(*feature0, rec(3, cell="f2")), jsonl(*main)).data)
    assert [(r["seq"], r["cell"], r.get("merged_seq"), r.get("replay")) for r in feature] == [
        (1, "inv", None, None), (2, "f1", None, None), (3, "f2", None, None), (4, "m1", 2, None),
        (5, None, 4, [2]),                                                    # main's merge record, renumbered
        (6, None, None, [2])]                                                 # theirs (main) changed no layout
    res = logmerge.merge(jsonl(*main), jsonl(*main, rec(5, cell="m2")), jsonl(*feature))
    out = parse(res.data)
    assert [(r["seq"], r["cell"], r.get("merged_seq"), r.get("replay")) for r in out] == [
        (1, "inv", None, None), (2, "m1", None, None), (3, "f1", 2, None), (4, None, None, [3]),
        (5, "m2", None, None),
        (6, "f2", 3, None), (7, None, 6, [3]),                                # feature's own: f2 and its merge
        (8, None, None, [3])]                                                 # main's journal: f1, once
    assert res.combined == ["2 operations of the other branch: f2 (schematic), numbered from 6"]
    assert seqs(res.data) == [3]
    # the diff pairs them the same way: what feature has that main lacks is f2 and the update's merge record
    assert [c["id"] for c in logmerge.diff(jsonl(*main), jsonl(*feature))["changes"] if c["do"] == "add"] == [
        "seq:3", "seq:6"]


def test_cherry_pick_takes_the_picked_commits_operations_only(tmp_path):
    """git cherry-pick B2 runs the driver with base = B1 (B2's parent), ours = main, theirs = B2: B1's operations are
    not picked, neither as records nor as unsaved layout edits."""
    common = [rec(1)]
    b1 = [*common, rec(2, cell="b1")]
    b2 = [*b1, rec(3, cell="b2"), undo(4, 2, cell="b1")]                     # B2 also undid an operation of B1
    main = [*common, rec(2, cell="m")]
    out = parse(logmerge.merge(jsonl(*b1), jsonl(*main), jsonl(*b2)).data)
    assert [(r["seq"], r["op"], r["cell"], r.get("merged_seq"), r.get("of")) for r in out] == [
        (1, "set_props", "inv", None, None), (2, "set_props", "m", None, None),
        (3, "set_props", "b2", 3, None), (4, "undo", "b1", 4, None),         # of an operation main does not have
        (5, "merge", None, None, None)]
    # a revert of B2 (base = B2, theirs = B1) adds no operation: the log keeps them
    assert logmerge.merge(jsonl(*b2), jsonl(*b2, rec(5, cell="m")), jsonl(*b1)).data == jsonl(*b2, rec(5, cell="m"))
    # the layout: the common commit has an unsaved box (-200), B1 and B2 each draw one, main changes a schematic:
    # main's cell files are the base's, so B2's journal stays pending, less B1's box
    c = _lib(tmp_path / "c")
    _draw(_db(c), -200)
    shutil.copytree(c, tmp_path / "main")
    _sch(tmp_path / "main")
    shutil.copytree(c, tmp_path / "b1")
    _draw(_db(tmp_path / "b1"), 200)
    shutil.copytree(tmp_path / "b1", tmp_path / "b2")
    _draw(_db(tmp_path / "b2"), 400)
    th = _db(tmp_path / "b2")
    assert th.undo("top") == "top" and th.undo("top") == "top"              # 400, then B1's 200: not picked either
    assert th.redo("top") == "top" and th.redo("top") == "top"
    res = _merge_lib(tmp_path / "b1", tmp_path / "main", tmp_path / "b2", tmp_path / "picked")
    assert res.clean
    assert res.combined[0] == "5 operations of the other branch: top (layout), numbered from 5"
    out = parse(res.data)
    assert out[-1]["replay"] == [3, 5, 6, 9]                                  # -200, 400, its undo, its redo
    assert _boxes(_db(tmp_path / "picked")) == _at(-200, 400)


def test_a_cherry_pick_whose_parent_saved_the_layout_is_a_conflict(tmp_path):
    """B1 saved the common commit's unsaved box (-200) with its own; main's journal has that box. Picking B2 merges the
    cell files against B1's, which main's journal was not recorded on: main's box would be lost, so it is a conflict,
    though main changed no layout itself."""
    c = _lib(tmp_path / "c")
    _draw(_db(c), -200)
    shutil.copytree(c, tmp_path / "main")
    _sch(tmp_path / "main")
    shutil.copytree(c, tmp_path / "b1")
    b1 = _db(tmp_path / "b1")
    _draw(b1, 200)
    b1.save()
    shutil.copytree(tmp_path / "b1", tmp_path / "b2")
    _draw(_db(tmp_path / "b2"), 400)
    res = _merge_lib(tmp_path / "b1", tmp_path / "main", tmp_path / "b2", tmp_path / "picked")
    assert [c.id for c in res.open_conflicts] == ["layout-journal"]
    assert res.conflicts[0].what.startswith("unsaved layout edits of both sides are dropped by this merge")
    assert seqs(res.data) == []
    # B2 changed no layout: main's files are kept, and main's journal with them
    shutil.copytree(tmp_path / "b1", tmp_path / "b2s")
    _sch(tmp_path / "b2s")
    res = _merge_lib(tmp_path / "b1", tmp_path / "main", tmp_path / "b2s", tmp_path / "picked2")
    assert res.clean and _boxes(_db(tmp_path / "picked2")) == _at(-200)


def _move(db: LayoutDB, x: int, nx: int) -> None:
    li = db.layout.layer(68, 20)
    db._commit("top", [("del", li, kdb.Box(x, 0, x + 100, 100), 0), ("ins", li, kdb.Box(nx, 0, nx + 100, 100))])


def test_a_revert_of_a_saved_layout_edited_since_is_a_conflict(tmp_path):
    """git revert C runs the driver with base = C, ours = HEAD, theirs = C's parent: theirs adds no operation, but it
    lacks C's layout records, so it changed the layout (git takes the parent's cell files). C saved a box that ours then
    moved without saving: replaying the move on files without the box would bring it back (at 500), so ours' journal
    is a conflict, dropped by the merged log, which ends it with a merge record."""
    parent = _lib(tmp_path / "parent")
    c = tmp_path / "c"
    shutil.copytree(parent, c)
    db = _db(c)
    _draw(db, 0)
    db.save()
    shutil.copytree(c, tmp_path / "ours")
    _move(_db(tmp_path / "ours"), 0, 500)                                     # unsaved
    res = _merge_lib(c, tmp_path / "ours", parent, tmp_path / "reverted")
    assert [x.id for x in res.open_conflicts] == ["layout-journal"]
    x = res.conflicts[0]
    assert x.what.startswith("unsaved layout edits of ours are dropped by this merge")
    assert x.ours == ["seq:5 edit top (layout) by user"] and x.theirs is None and res.warnings == []
    out = parse(res.data)
    assert out[:-1] == parse(_log(tmp_path / "ours")) and res.combined == []
    assert out[-1]["op"] == "merge" and out[-1]["reason"] == "merged 0 operations of the other branch"
    assert "replay" not in out[-1] and seqs(res.data) == []
    m = _db(tmp_path / "reverted")
    assert _boxes(m) == [] and not m.dirty                                    # the reverted box stays out


def test_a_revert_of_unsaved_layout_edits_drops_them_with_a_warning(tmp_path):
    """C drew a box without saving (committed around the hook), a later commit changed a schematic: reverting C changes
    no cell file, but the box is in ours' journal. Theirs (C's parent) lacks it, so theirs changed the layout and ours
    did not: the merged layout is theirs', ours' journal is not carried, and a warning says so."""
    parent = _lib(tmp_path / "parent")
    c = tmp_path / "c"
    shutil.copytree(parent, c)
    _draw(_db(c), 0)
    shutil.copytree(c, tmp_path / "ours")
    _sch(tmp_path / "ours")
    res = _merge_lib(c, tmp_path / "ours", parent, tmp_path / "reverted")
    assert res.clean and res.combined == []
    assert res.warnings == ["1 unsaved layout edit of ours is dropped: the merged layout follows theirs' saved files"]
    out = parse(res.data)
    assert out[:-1] == parse(_log(tmp_path / "ours")) and out[-1]["op"] == "merge" and "replay" not in out[-1]
    assert seqs(_log(tmp_path / "ours")) == [3] and seqs(res.data) == []
    m = _db(tmp_path / "reverted")
    assert _boxes(m) == [] and not m.dirty
    # the parent's own unsaved edit (2) stays pending, C's (3) does not; a revert of a commit that changed no layout
    # keeps ours' bytes
    e, f = rec(2, view="layout", op="create"), rec(3, cell="buf", view="layout", op="create")
    parent_log, c_log = [rec(1, **SAVE), e], [rec(1, **SAVE), e, f]
    res = logmerge.merge(jsonl(*c_log), jsonl(*c_log, rec(4)), jsonl(*parent_log))
    assert res.clean and parse(res.data)[-1]["replay"] == [2] and seqs(res.data) == [2]
    assert res.warnings == ["1 unsaved layout edit of ours is dropped: the merged layout follows theirs' saved files"]
    res = logmerge.merge(jsonl(*c_log, rec(4)), jsonl(*c_log, rec(4), rec(5, cell="m")), jsonl(*c_log))
    assert res.data == jsonl(*c_log, rec(4), rec(5, cell="m")) and res.warnings == []


def test_unsaved_edits_a_merge_drops_without_a_conflict_are_a_warning(tmp_path):
    """main draws a box without saving; feature saves one and is updated from main: a layout-journal conflict, resolved
    (main's box is dropped on feature). Merging feature back into main, which changed a schematic meanwhile, takes
    feature's cell files without main's box: a warning says so, and merging main into feature again too. An edit the
    other side has pending, or saved, is in the merged layout: no warning (test_a_branch_merged_back_...)."""
    c = _lib(tmp_path / "c")
    shutil.copytree(c, tmp_path / "main")
    shutil.copytree(c, tmp_path / "feature")
    _draw(_db(tmp_path / "main"), 0)
    f = _db(tmp_path / "feature")
    _draw(f, 400)
    f.save()
    up = _merge_lib(c, tmp_path / "feature", tmp_path / "main", tmp_path / "feature2",
                    prefer={"layout-journal": "theirs"})
    assert up.clean and up.conflicts[0].resolved and up.warnings == []
    shutil.copytree(tmp_path / "main", tmp_path / "m0")
    _sch(tmp_path / "main")
    res = _merge_lib(tmp_path / "m0", tmp_path / "main", tmp_path / "feature2", tmp_path / "merged")
    assert res.clean and res.conflicts == []
    assert res.warnings == ["1 unsaved layout edit of ours is dropped: the merged layout follows theirs' saved files"]
    m = _db(tmp_path / "merged")
    assert _boxes(m) == _at(400) and not m.dirty
    res = _merge_lib(tmp_path / "m0", tmp_path / "feature2", tmp_path / "main", tmp_path / "merged2")
    assert res.clean and res.conflicts == []
    assert res.warnings == ["1 unsaved layout edit of theirs is dropped: the merged layout follows ours' saved files"]
    assert _boxes(_db(tmp_path / "merged2")) == _at(400)
    # theirs has every operation ours has, and dropped ours' unsaved box: theirs' bytes, with the warning
    base, box = [rec(1, **SAVE)], rec(2, view="layout", op="create")
    merged = {"v": 1, "seq": 5, "t": "2026-10-23T10:00:00+0900", "op": "merge", "actor": "git", "cell": None,
              "view": None, "effects": [], "reason": "merged 1 operation of the other branch"}
    theirs = [*base, rec(2, view="layout", op="create", actor="agent:x"), rec(3, **SAVE),
              {**box, "seq": 4, "merged_seq": 2}, merged]
    res = logmerge.merge(jsonl(*base), jsonl(*base, box), jsonl(*theirs))
    assert res.data == jsonl(*theirs) and res.clean
    assert res.warnings == ["1 unsaved layout edit of ours is dropped: the merged layout follows theirs' saved files"]
    theirs[-1] = {**merged, "replay": [4]}                                    # carried there: pending, no warning
    res = logmerge.merge(jsonl(*base), jsonl(*base, box), jsonl(*theirs))
    assert res.data == jsonl(*theirs) and res.warnings == []


def test_unchanged_sides_keep_their_bytes():
    # written by hand (spaces, another key order): a merge that rewrote it would change its bytes
    base = b'{"seq": 1, "v": 1, "op": "set_props", "cell": "inv", "view": "schematic", "effects": []}\n'
    ours = base + b'{"seq": 2, "v": 1, "op": "move", "cell": "inv", "view": "schematic", "effects": []}\n'
    theirs = base + b'{"seq": 2, "v": 1, "op": "rotate", "cell": "inv", "view": "schematic", "effects": []}\n'
    res = logmerge.merge(base, ours, base)
    assert res.data == ours and res.combined == [] and res.clean
    res = logmerge.merge(base, base, theirs)
    assert res.data == theirs and res.combined == ["1 operation of the other branch: inv (schematic)"]
    assert logmerge.merge(base, ours, ours).data == ours                      # both recorded the same
    assert logmerge.merge(None, ours, base).data == ours                      # theirs has nothing ours lacks


def test_torn_and_invalid_lines_are_skipped():
    base = jsonl(rec(1))
    ours = base + jsonl(rec(2, reason="ours")) + b'{"v":1,"seq":3,"t":"2026-10-'               # a torn last line
    theirs = base + b"\n[1, 2]\nnot json\n" + jsonl(rec(2, cell="buf")) + b'{"op":"no seq"}\n'
    assert [r["seq"] for _, r in logmerge.records(ours)] == [1, 2]
    res = logmerge.merge(base, ours, theirs)
    assert res.clean
    out = parse(res.data)                                                     # every line is a record again
    assert [(r["seq"], r["cell"], r.get("merged_seq")) for r in out[:3]] == [(1, "inv", None), (2, "inv", None),
                                                                               (3, "buf", 2)]
    assert out[3]["op"] == "merge" and out[3]["seq"] == 4 and out[3]["reason"] == "merged 1 operation of the other branch"
    assert logmerge.merge(base, ours, base).data == ours                      # unchanged: kept as it was, torn line too


def test_both_sides_created_the_log():
    ours, theirs = jsonl(rec(1, cell="a")), jsonl(rec(1, cell="b", t="2026-10-24T09:00:00+0900"))
    out = parse(logmerge.merge(None, ours, theirs).data)
    assert [(r["seq"], r["cell"], r.get("merged_seq")) for r in out] == [(1, "a", None), (2, "b", 1), (3, None, None)]
    assert out[2]["op"] == "merge" and out[2]["t"] == "2026-10-24T09:00:00+0900"


def test_a_branch_merged_back_takes_no_operation_twice(tmp_path):
    """feature is updated from main (main's operations copied, renumbered), undoes one of them, and is merged into main
    after main moved on: main gets feature's own operations, once, and the undo points to main's own record."""
    common = [rec(1)]
    main = [*common, rec(2, cell="m1"), rec(3, cell="m2")]
    feature = parse(logmerge.merge(jsonl(*common), jsonl(*common, rec(2, cell="f1")), jsonl(*main)).data)
    assert [(r["seq"], r["cell"]) for r in feature] == [(1, "inv"), (2, "f1"), (3, "m1"), (4, "m2"), (5, None)]
    feature.append(undo(6, 4, cell="m2", reason="not on this branch"))       # m2 is seq 3 on main
    main.append(rec(4, cell="m3"))
    # git's merge base is the main commit feature was updated from
    res = logmerge.merge(jsonl(*main[:3]), jsonl(*main), jsonl(*feature))
    out = parse(res.data)
    assert [(r["seq"], r["op"], r["cell"], r.get("merged_seq")) for r in out] == [
        (1, "set_props", "inv", None), (2, "set_props", "m1", None), (3, "set_props", "m2", None),
        (4, "set_props", "m3", None),
        (5, "set_props", "f1", 2), (6, "merge", None, 5), (7, "undo", "m2", 6),  # feature's own records
        (8, "merge", None, None)]
    assert out[6]["of"] == 3                                                  # main's m2, not a copy of it
    assert out[7]["reason"] == "merged 3 operations of the other branch"
    path = tmp_path / "t.jsonl"
    path.write_bytes(res.data)
    assert [r["cell"] for r in net_records(OpLog(path))] == ["inv", "m1", "m3", "f1"]
    # main has nothing feature lacks: feature's bytes
    assert logmerge.merge(jsonl(*common), jsonl(*main[:3]), jsonl(*feature)).data == jsonl(*feature)
    # and feature updated again from main gets m3 only
    again = parse(logmerge.merge(jsonl(*main[:3]), jsonl(*feature), jsonl(*main)).data)
    assert [(r["cell"], r.get("merged_seq")) for r in again[6:]] == [("m3", 4), (None, None)]


def test_the_same_edit_made_on_both_branches_is_two_operations():
    """Equal records that no merge copied (a script run on both branches within one second) are both kept: the log
    keeps both records as history (the layout keeps the shape once, test_the_same_shape_on_both_branches_is_one_shape).
    Both are unsaved layout edits on files both sides changed: the journal is a conflict."""
    box = rec(2, view="layout", op="create", effects=[{"k": "s", "do": "add", "cell": "inv", "layer": "68/20",
                                                       "t": "box", "s": "(0,0;100,100)"}])
    ours = [rec(1), rec(2, cell="buf"), {**box, "seq": 3}]
    res = logmerge.merge(jsonl(rec(1)), jsonl(*ours), jsonl(rec(1), box))
    out = parse(res.data)
    assert [(r["seq"], r["op"], r.get("merged_seq")) for r in out] == [
        (1, "set_props", None), (2, "set_props", None), (3, "create", None), (4, "create", 2), (5, "merge", None)]
    assert [c.id for c in res.conflicts] == ["layout-journal"]


def test_the_same_shape_on_both_branches_is_one_shape(tmp_path):
    """Both branches drew the same box and saved (ours after a schematic edit): the layout merge keeps the shape once
    (both made the same change), while the log keeps both sides' records as history."""
    base = _lib(tmp_path / "base")
    for side in ("ours", "theirs"):
        shutil.copytree(base, tmp_path / side)
        if side == "ours":
            _sch(tmp_path / side)
        db = _db(tmp_path / side)
        _draw(db, 0)
        db.save()
    res = _merge_lib(base, tmp_path / "ours", tmp_path / "theirs", tmp_path / "merged")
    assert res.clean and _boxes(_db(tmp_path / "merged")) == _at(0)
    out = parse(res.data)
    assert [(r["op"], r.get("merged_seq")) for r in out[2:]] == [("set_props", None), ("edit", None), ("save", None),
                                                                  ("edit", 3), ("save", 4), ("merge", None)]


def test_dates_are_read_as_oplog_writes_them_on_python_3_10(monkeypatch):
    """OpLog.append writes the offset without a colon ("+0900"), which datetime.fromisoformat reads only from Python
    3.11: the merge record is dated like the latest record on 3.10 too (its fromisoformat stood in for here)."""
    class Py310(datetime):
        @classmethod
        def fromisoformat(cls, s):
            if re.search(r"(Z|[+-]\d{4})$", s):
                raise ValueError(f"Invalid isoformat string: {s!r}")
            return super().fromisoformat(s)

    monkeypatch.setattr(logmerge, "datetime", Py310)
    res = logmerge.merge(jsonl(*BASE), jsonl(*OURS), jsonl(*THEIRS))
    assert parse(res.data)[-1]["t"] == "2026-10-23T02:30:00+0000"            # 11:30 in Tokyo: the latest
    assert logmerge._latest([{"t": "2026-10-23T09:00:00+09:00"}, {"t": "2026-10-23T00:30:00.500000+00:00"},
                             {"t": "2026-10-23T00:20:00Z"}, {"t": "now"}, {"t": None}]) == (
        "2026-10-23T00:30:00.500000+00:00")                                  # fromisoformat: fractions of a second


def test_merge_is_deterministic():
    args = (jsonl(*BASE), jsonl(*OURS), jsonl(*THEIRS))
    a = logmerge.merge(*args)
    assert logmerge.merge(*args).data == a.data
    b = logmerge.merge(*args, prefer={"log:1": "theirs"})
    assert b.data == a.data and b.conflicts == []


def test_diff_lists_the_operations_after_the_common_part():
    ours, theirs = jsonl(*OURS), jsonl(*THEIRS)
    merged = logmerge.merge(jsonl(*BASE), ours, theirs).data
    d = logmerge.diff(ours, merged)
    assert d["kind"] == "log" and d["common"] == 4
    assert [(c["do"], c["id"], c["what"]) for c in d["changes"]] == [
        ("add", "seq:5", "create inv (layout) by agent:route"),
        ("add", "seq:6", "undo of 5 inv (layout) by agent:route"),
        ("add", "seq:7", "undo of 1 inv (schematic) by agent:route"),
        ("add", "seq:8", 'merge by git: "merged 3 operations of the other branch"')]
    assert d["changes"][0]["before"] is None and d["changes"][0]["after"] == {
        "seq": 5, "t": "2026-10-23T10:30:00+0900", "op": "create", "cell": "inv", "view": "layout",
        "actor": "agent:route", "reason": None, "changeset": None, "merged_seq": 3}
    assert d["changes"][3]["after"]["replay"] == [2, 5, 6]
    # theirs' operations were only renumbered: what the merge added to theirs is ours' and the merge record
    assert [(c["do"], c["id"]) for c in logmerge.diff(theirs, merged)["changes"]] == [
        ("add", "seq:3"), ("add", "seq:4"), ("add", "seq:8")]
    # two branches: what only the old one has is removed
    assert [(c["do"], c["id"], c["what"]) for c in logmerge.diff(ours, theirs)["changes"]] == [
        ("del", "seq:3", 'set_props inv (schematic) by user: "widen M1"'), ("del", "seq:4", "set_props buf (schematic) by user"),
        ("add", "seq:3", "create inv (layout) by agent:route"), ("add", "seq:4", "undo of 3 inv (layout) by agent:route"),
        ("add", "seq:5", "undo of 1 inv (schematic) by agent:route")]
    assert [c["do"] for c in logmerge.diff(None, ours)["changes"]] == ["add"] * 4
    assert [c["do"] for c in logmerge.diff(ours, None)["changes"]] == ["del"] * 4
    # formatting is no change
    spaced = b"".join(json.dumps(r).encode() + b"\n" for r in OURS)
    assert logmerge.diff(ours, spaced)["changes"] == [] and logmerge.diff(ours, ours + b"\n")["changes"] == []


def test_journal_reads_what_the_layout_replays():
    """journal(): the layout records after the last layout save or merge record, those the merge record lists first
    (records it lists that are not layout records before it are no journal)."""
    assert logmerge.journal(b"") == []
    log = [rec(1, view="layout"), rec(2, **SAVE), rec(3, view="layout"), undo(4, 3, view="layout"), rec(5)]
    assert seqs(jsonl(*log)) == [3, 4]
    merge = {"v": 1, "seq": 6, "op": "merge", "cell": None, "view": None, "effects": [], "replay": [3, 5, 1, 99, "x"]}
    assert seqs(jsonl(*log, merge, rec(7, view="layout"))) == [3, 1, 7]
    assert seqs(jsonl(*log, {**merge, "replay": None})) == []
    assert seqs(jsonl(*log, merge, rec(7, **SAVE))) == []
