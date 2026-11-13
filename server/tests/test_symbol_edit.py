"""Symbol geometry/pins are editable, undoable, and replayable without losing terminal order."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine.store import oplog  # noqa: E402
from undine.schematics import symedit, xschem
from undine.schematics.schedit import SchEditor  # noqa: E402


@pytest.fixture
def ed(tmp_path):
    path = tmp_path / "amp.sym"
    path.write_text(xschem.dump(xschem.make_symbol([("A", "ipin"), ("Y", "opin"), ("VDD", "iopin")])))
    return SchEditor(path, xschem.SymbolResolver([tmp_path]), oplog.OpLog(tmp_path / "history.jsonl"))


def objects(ed, kind=None):
    return [o for o in ed.view("symbol")["objects"] if kind is None or o["kind"] == kind]


def edit(ed, op, **values):
    with ed.recording(op, values, "user"):
        return ed.symbol_edit(op, values)


def contents(ed):
    return xschem.dump(ed._load())


def test_generated_default_pin_names_directions_order_and_labels(ed):
    pins = objects(ed, "pin")
    assert [(p["name"], p["direction"], p["order"]) for p in pins] == [
        ("A", "in", 1), ("Y", "out", 2), ("VDD", "inout", 3)]
    assert all(p["x"] < 0 if p["direction"] == "in" else p["x"] > 0 for p in pins)
    assert [t["text"] for t in objects(ed, "text")] == ["@symname", "@name"]
    assert {t.props[symedit.PIN_LABEL] for t in ed._load().texts if symedit.PIN_LABEL in t.props} == {"A", "Y", "VDD"}


@pytest.mark.parametrize("kind,params", [
    ("pin", {"name": "VSS", "direction": "inout", "order": 2}),
    ("line", {"x2": 20, "y2": 10}),
    ("rectangle", {"x2": 30, "y2": 25}),
    ("text", {"text": "Gain", "size": 0.25}),
])
def test_add_move_delete_round_trip_and_persistent_undo(ed, kind, params):
    before = contents(ed)
    ident = edit(ed, "symbol_add", kind=kind, x=0, y=0, **params)
    assert ident in {o["id"] for o in objects(ed)}
    added = contents(ed)
    moved = edit(ed, "symbol_move", ids=[ident], dx=10, dy=-20)
    assert len(moved) == 1 and moved[0] in {o["id"] for o in objects(ed)}
    moved_content = contents(ed)
    edit(ed, "symbol_delete", ids=moved)
    assert contents(ed) == before
    restarted = SchEditor(ed.path, ed.resolver, oplog.OpLog(ed.log.path))
    assert restarted.undo() and contents(ed) == moved_content
    assert restarted.undo() and contents(ed) == added
    assert restarted.undo() and contents(ed) == before
    assert restarted.redo() and contents(ed) == added
    assert restarted.redo() and contents(ed) == moved_content


def test_pin_rename_move_and_reorder_preserve_metadata_and_associated_label(ed):
    old = contents(ed)
    pin = objects(ed, "pin")[0]
    ident = edit(ed, "symbol_update", id=pin["id"], name="IN", direction="inout", order=3,
                 x=pin["x"]+10, y=pin["y"]+20)
    pins = objects(ed, "pin")
    assert [p["name"] for p in pins] == ["Y", "VDD", "IN"]
    assert pins[-1]["id"] == ident and pins[-1]["direction"] == "inout"
    labels = [t for t in ed._load().texts if t.props.get(symedit.PIN_LABEL) == "IN"]
    prior = next(t for t in xschem.parse(old).texts if t.props.get(symedit.PIN_LABEL) == "A")
    assert len(labels) == 1 and labels[0].s == "IN"
    assert (labels[0].x, labels[0].y) == (prior.x+10, prior.y+20)
    modified = contents(ed)
    assert ed.undo() and contents(ed) == old
    assert ed.redo() and contents(ed) == modified


def test_generated_pin_lead_follows_move_rename_delete_and_undo(ed):
    before = contents(ed)
    pin = objects(ed, "pin")[0]
    lead = next(line for line in ed._load().lines if line[-1].get(symedit.PIN_LABEL) == pin["name"])
    ident = edit(ed, "symbol_move", ids=[pin["id"]], dx=-20, dy=30)[0]
    moved = next(line for line in ed._load().lines if line[-1].get(symedit.PIN_LABEL) == pin["name"])
    assert moved[1:3] == lead[1:3]  # body-side endpoint stays on the body
    assert moved[3:5] == (pin["x"]-20, pin["y"]+30)
    ident = edit(ed, "symbol_update", id=ident, name="INPUT", x=pin["x"]-30, y=pin["y"]+40)
    renamed = next(line for line in ed._load().lines if line[-1].get(symedit.PIN_LABEL) == "INPUT")
    assert renamed[1:3] == lead[1:3] and renamed[3:5] == (pin["x"]-30, pin["y"]+40)
    edit(ed, "symbol_delete", ids=[ident])
    assert not any(line[-1].get(symedit.PIN_LABEL) == "INPUT" for line in ed._load().lines)
    assert ed.undo() and ed.undo() and ed.undo() and contents(ed) == before


@pytest.mark.parametrize("pin_first", [False, True])
def test_moving_generated_pin_and_its_lead_together_does_not_double_move(ed, pin_first):
    pin = objects(ed, "pin")[0]
    f = ed._load()
    i, lead = next((i, line) for i, line in enumerate(f.lines) if line[-1].get(symedit.PIN_LABEL) == pin["name"])
    lead_id = symedit._id_at(f, "lines", i)
    ids = [pin["id"], lead_id] if pin_first else [lead_id, pin["id"]]
    before = contents(ed)
    edit(ed, "symbol_move", ids=ids, dx=20, dy=30)
    moved = next(line for line in ed._load().lines if line[-1].get(symedit.PIN_LABEL) == pin["name"])
    assert moved[1:5] == (lead[1]+20, lead[2]+30, lead[3]+20, lead[4]+30)
    assert ed.undo() and contents(ed) == before


def test_imported_independent_lead_is_not_modified_with_pin(ed):
    f = ed._load()
    for line in f.lines:
        line[-1].pop(symedit.PIN_LABEL, None)
    ed.path.write_text(xschem.dump(f))
    pin = objects(ed, "pin")[0]
    original_lines = ed._load().lines
    ident = edit(ed, "symbol_move", ids=[pin["id"]], dx=20, dy=30)[0]
    assert ed._load().lines == original_lines
    edit(ed, "symbol_delete", ids=[ident])
    assert ed._load().lines == original_lines


def test_update_text_size_is_logged_even_when_text_identity_does_not_change(ed):
    text = objects(ed, "text")[0]
    before = contents(ed)
    edit(ed, "symbol_update", id=text["id"], size=0.8)
    assert objects(ed, "text")[0]["size"] == 0.8
    assert ed.undo() and contents(ed) == before
    assert ed.redo() and objects(ed, "text")[0]["size"] == 0.8


def test_group_move_is_one_atomic_edit_and_keeps_managed_labels(ed):
    before = contents(ed)
    selected = [p["id"] for p in objects(ed, "pin")] + [objects(ed, "line")[0]["id"]]
    result = edit(ed, "symbol_move", ids=selected, dx=30, dy=-10)
    assert len(result) == len(selected) and len(ed.log.read()) == 1
    assert ed.undo() and contents(ed) == before


def test_imported_polygon_arc_and_unrelated_attributes_survive(ed):
    f = ed._load()
    f.polys = [(4, [0, 0, 10, 0, 10, 20], {"fill": "true"})]
    f.arcs = [(4, 0, 0, 30, 10, 120, {"dash": "2"})]
    f.boxes[-1][-1]["pinnumber"] = "99"
    f.global_props["custom"] = "unchanged"
    f.k_raw = xschem.dump_props(f.global_props)
    ed.path.write_text(xschem.dump(f))
    before = contents(ed)
    targets = [o["id"] for o in objects(ed) if o["kind"] in ("polygon", "arc")]
    moved = edit(ed, "symbol_move", ids=targets, dx=10, dy=20)
    assert ed._load().polys[0][1] == [10, 20, 20, 20, 20, 40]
    assert ed._load().arcs[0][1:3] == (10, 20)
    assert ed._load().global_props["custom"] == "unchanged"
    assert ed._load().boxes[-1][-1]["pinnumber"] == "99"
    edit(ed, "symbol_delete", ids=moved)
    assert not ed._load().polys and not ed._load().arcs
    assert ed.undo() and ed.undo() and contents(ed) == before


def test_invalid_pin_edits_and_stale_handles_are_atomic(ed):
    pin = objects(ed, "pin")[0]
    before = contents(ed)
    for values in ({"name": "Y"}, {"name": ""}, {"name": "two names"}, {"direction": "bad"},
                   {"order": 0}, {"order": 9}, {"x": float("nan")}):
        with pytest.raises(ValueError):
            edit(ed, "symbol_update", id=pin["id"], **values)
        assert contents(ed) == before and not ed.log.read()
    edit(ed, "symbol_move", ids=[pin["id"]], dx=20, dy=0)
    moved = contents(ed)
    with pytest.raises(ValueError, match="changed"):
        edit(ed, "symbol_delete", ids=[pin["id"]])
    assert contents(ed) == moved and len(ed.log.read()) == 1


def test_symbol_edit_rejects_other_view_and_missing_symbol(ed):
    sch = SchEditor(ed.path.with_suffix(".sch"), ed.resolver, ed.log)
    with pytest.raises(ValueError, match="symbol view"):
        edit(sch, "symbol_add", kind="pin", name="A", x=0, y=0)
    ed.path.unlink()
    with pytest.raises(ValueError, match="create the symbol"):
        edit(ed, "symbol_add", kind="pin", name="A", x=0, y=0)


def test_live_write_guard_blocks_symbol_edits_before_persistence(ed):
    class Locked:
        def guard(self):
            raise PermissionError("another writer")
    ed.log.hooks = Locked()
    before = contents(ed)
    with pytest.raises(PermissionError):
        edit(ed, "symbol_add", kind="pin", name="VSS", x=0, y=0)
    assert contents(ed) == before and not ed.log.read()


def test_geometry_replay_revert_and_conflict_after_restart(ed, tmp_path):
    initial = contents(ed)
    target_path = tmp_path / "target.sym"
    target_path.write_text(initial)
    target = SchEditor(target_path, ed.resolver, oplog.OpLog(tmp_path / "target-history.jsonl"))
    pin = objects(ed, "pin")[0]
    edit(ed, "symbol_update", id=pin["id"], order=3, name="INPUT")
    record = json.loads(ed.log.path.read_text().splitlines()[-1])
    target.replay(record["effects"], {"op": "apply", "actor": "user"})
    assert contents(target) == contents(ed)
    target.replay(oplog.invert(record["effects"]), {"op": "revert", "actor": "user"})
    assert contents(target) == initial
    target.symbol_edit("symbol_add", {"kind": "pin", "name": "EXTRA", "x": 0, "y": 0})
    changed = contents(target)
    with pytest.raises(ValueError, match="conflict"):
        target.replay(record["effects"], {"op": "apply", "actor": "user"})
    assert contents(target) == changed


def test_decimal_geometry_undo_matches_the_written_precision(ed):
    ident = edit(ed, "symbol_add", kind="line", x=1/3, y=2/3, x2=11/3, y2=22/3)
    assert ident in {o["id"] for o in objects(ed)}
    assert ed.undo() and ed.redo()


def test_create_undo_redo_and_safe_refusal_if_file_changed(tmp_path):
    path = tmp_path / "new.sym"
    ed = SchEditor(path, xschem.SymbolResolver([tmp_path]), oplog.OpLog(tmp_path / "history.jsonl"))
    original = xschem.dump(xschem.make_symbol([("IN", "ipin"), ("OUT", "opin")]))
    with ed.recording("create_cell", {}, "user"):
        ed.create_file(original)
    assert ed.undo() and not path.exists()
    assert ed.view("symbol")["exists"] is False and ed.view("symbol")["redo"] == 1
    assert ed.redo() and contents(ed) == original
    path.write_text(original + "L 4 0 0 20 20 {}\n")
    with pytest.raises(ValueError, match="conflict"):
        ed.undo()
    assert path.exists()
    path.write_text(original)
    assert ed.undo()
    path.write_text(original)
    with pytest.raises(ValueError, match="conflict"):
        ed.redo()


def test_creation_replay_logs_one_creation_effect_without_double_drawing(tmp_path):
    text = xschem.dump(xschem.make_symbol([("IN", "ipin")]))
    ed = SchEditor(tmp_path / "new.sym", xschem.SymbolResolver([tmp_path]), oplog.OpLog(tmp_path / "history.jsonl"))
    ed.replay([{"k": "v", "do": "add", "text": text}], {"op": "apply", "actor": "user"})
    assert [e["k"] for e in ed.log.read()[-1]["effects"]] == ["v"]
    assert ed.undo() and not ed.path.exists()
    assert ed.redo() and contents(ed) == text


def test_symbol_changeset_apply_revert_and_checkpoint_preserve_real_design(tmp_path):
    from undine.server.api import Server
    from undine.store.changeset import Changesets

    srv = Server(tmp_path, {}, [tmp_path])
    prj = srv.store.create("p")
    lib = prj.create_library("l")
    real_path = lib.path / "amp.sym"
    initial = xschem.dump(xschem.make_symbol([("IN", "ipin"), ("OUT", "opin")]))
    real_path.write_text(initial)
    cs = Changesets(srv)
    meta = cs.create("p", "Edit symbol")
    ed = srv.editor(meta["preview"], "l", "amp", "symbol")
    pin = objects(ed, "pin")[0]
    ident = edit(ed, "symbol_update", id=pin["id"], name="INPUT", order=2)
    checkpoint = ed.log.read()[-1]["gseq"]
    kept = contents(ed)
    edit(ed, "symbol_move", ids=[ident], dx=20, dy=0)
    assert real_path.read_text() == initial
    cs.restore("p", meta["id"], checkpoint, "user")
    assert contents(ed) == kept
    cs.apply("p", meta["id"], "user")
    assert real_path.read_text() == kept
    cs.revert("p", meta["id"], "user")
    assert real_path.read_text() == initial


def test_created_symbol_changeset_apply_and_restore_after_undo(tmp_path):
    from undine.server.api import Server
    from undine.store.changeset import Changesets

    srv = Server(tmp_path, {}, [tmp_path])
    srv.store.create("p").create_library("l")
    cs = Changesets(srv)
    meta = cs.create("p", "Create symbol")
    ed = srv.editor(meta["preview"], "l", "amp", "symbol")
    initial = xschem.dump(xschem.make_symbol([("IN", "ipin")]))
    with ed.recording("create_cell", {}, "user"):
        ed.create_file(initial)
    checkpoint = ed.log.read()[-1]["gseq"]
    assert ed.undo() and not ed.path.exists()
    cs.restore("p", meta["id"], checkpoint, "user")
    assert ed.path.read_text() == initial
    pin = objects(ed, "pin")[0]
    edit(ed, "symbol_update", id=pin["id"], name="INPUT")
    final = contents(ed)
    cs.apply("p", meta["id"], "user")
    assert srv.editor("p", "l", "amp", "symbol").path.read_text() == final
