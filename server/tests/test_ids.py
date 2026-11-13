"""Stable ids of schematic objects (run: python -m pytest server/tests or python server/tests/test_ids.py)."""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine.schematics import xschem  # noqa: E402
from undine.store.oplog import OpLog  # noqa: E402
from undine.schematics.schedit import SchEditor  # noqa: E402

SYM = Path(__file__).resolve().parents[2] / "examples"


def make() -> tuple[SchEditor, Path]:
    d = Path(tempfile.mkdtemp())
    (d / "r.sym").write_text('v {}\nK {type=resistor\nformat="@name @pinlist @value"\ntemplate="name=R1 value=1k"}\n'
                              'B 5 -2.5 -32.5 2.5 -27.5 {name=P}\nB 5 -2.5 27.5 2.5 32.5 {name=M}\n')
    (d / "t.sch").write_text(xschem.dump(xschem.XFile(version="xschem version=3.4.5 file_version=1.2\n")))
    return SchEditor(d / "t.sch", xschem.SymbolResolver([d]), OpLog(d / "history" / "lib.jsonl")), d


def test_ids_survive_edits():
    ed, d = make()
    a = ed.add_instance("r.sym", 0, 0)
    b = ed.add_instance("r.sym", 100, 0)
    assert (a, b) == ("R1", "R2")
    ed.delete([{"k": "i", "id": "R1"}])          # indexes shift, ids must not
    assert ed.view()["insts"][0]["id"] == "R2"
    ed.set_props("i", "R2", {"value": "2k"})
    assert ed.instance("R2")["props"]["value"] == "2k"
    shutil.rmtree(d)


def test_rename_and_collision():
    ed, d = make()
    ed.add_instance("r.sym", 0, 0)
    ed.add_instance("r.sym", 100, 0)
    assert ed.set_props("i", "R1", {"name": "RL"}) == "RL"
    try:
        ed.set_props("i", "R2", {"name": "RL"})
        raise AssertionError("duplicate name accepted")
    except ValueError:
        pass
    shutil.rmtree(d)


def test_wire_ids_follow_geometry_and_undo():
    ed, d = make()
    w = ed.add_wire(10, 0, 0, 0)
    assert w == "0,0,10,0"                       # end points are ordered
    new = ed.move([{"k": "w", "id": w}], 20, 0)
    assert new == ["20,0,30,0"]
    assert ed.undo() and ed.view()["wires"][0][6] == w
    try:
        ed.delete([{"k": "w", "id": "9,9,9,9"}])
        raise AssertionError("unknown id accepted")
    except KeyError:
        pass
    shutil.rmtree(d)


def sem(ed):
    """Semantic content of the schematic (order independent)."""
    f = ed._load()
    return (sorted((c.props.get("name"), c.sym, c.x, c.y, c.rot, c.flip, sorted(c.props.items())) for c in f.comps),
            sorted((w[:4], sorted(w[4].items())) for w in f.wires))


def test_log_records_effects_and_undo_replays_after_restart():
    import json
    ed, d = make()
    ed.add_instance("r.sym", 0, 0)
    s0 = sem(ed)
    with ed.recording("set_props", {"id": "R1", "props": {"value": "2k"}}, "agent:test", "halve the current", "cs-1"):
        ed.set_props("i", "R1", {"value": "2k"})
    with ed.recording("set_props", {"id": "R1", "props": {"name": "RL"}}):
        ed.set_props("i", "R1", {"name": "RL"})
    ed.add_wire(0, 0, 10, 0)
    ed.delete([{"k": "w", "id": "0,0,10,0"}])
    recs = [json.loads(x) for x in (d / "history" / "lib.jsonl").read_text().splitlines()]
    assert [r["op"] for r in recs] == ["edit", "set_props", "set_props", "edit", "edit"]
    assert recs[1]["actor"] == "agent:test" and recs[1]["reason"] == "halve the current" and recs[1]["changeset"] == "cs-1"
    assert recs[1]["effects"] == [{"k": "i", "id": "R1", "do": "mod", "id_after": "R1", "changes": {"props.value": ["1k", "2k"]}}]
    assert recs[2]["effects"][0]["id_after"] == "RL"          # a rename is one change, not remove + add
    s_end = sem(ed)
    # a new editor on the same file and log (= server restart): undo everything, then redo everything
    ed2 = SchEditor(d / "t.sch", xschem.SymbolResolver([d]), OpLog(d / "history" / "lib.jsonl"))
    for _ in range(4):
        assert ed2.undo()
    assert sem(ed2) == s0
    assert ed2.undo() and sem(ed2)[0] == []                   # back to the empty schematic
    assert not ed2.undo()
    for _ in range(5):
        assert ed2.redo()
    assert sem(ed2) == s_end and not ed2.redo()
    shutil.rmtree(d)


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
