"""Copy is a snapshot; paste is one atomic edit, independent of the source selection."""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import klayout.db as kdb  # noqa: E402
from undine.schematics import xschem  # noqa: E402
from undine.layouts.layout import LayoutDB  # noqa: E402
from undine.store.oplog import OpLog  # noqa: E402
from undine.store.project import Library  # noqa: E402
from undine.schematics.schedit import SchEditor  # noqa: E402


@pytest.fixture
def editors(tmp_path):
    symbol = xschem.parse('v {}\nK {type=resistor template="name=R1 value=1k"}\n'
                         'B 4 -10 -15 10 15 {}\nB 5 -2 -32 2 -28 {name=P}\n')
    (tmp_path / "r.sym").write_text(xschem.dump(symbol))
    f = xschem.XFile(
        comps=[xschem.XComp("r.sym", 40, 60, 1, 1, {"name": "R1", "value": "22k"}),
               xschem.XComp("inline", 100, 60, 2, 0, {"name": "R2", "value": "47k"}, symbol)],
        wires=[(40, 30, 100, 30, {"lab": "signal", "bus": "true"})],
        texts=[xschem.XText("日本語 note", 50, 100, 1, 1, 0.4, 0.3, {"weight": "bold"})])
    (tmp_path / "source.sch").write_text(xschem.dump(f))
    (tmp_path / "target.sch").write_text(xschem.dump(xschem.XFile()))
    log = OpLog(tmp_path / "history.jsonl")
    res = xschem.SymbolResolver([tmp_path])
    return SchEditor(tmp_path / "source.sch", res, log), SchEditor(tmp_path / "target.sch", res, log)


def sch_items(ed):
    f = ed._load()
    return ([{"k": "i", "id": n} for n in xschem.comp_ids(f)]
            + [{"k": "w", "id": xschem.wire_id(*w[:4])} for w in f.wires]
            + [{"k": "t", "id": f"{t.x:g},{t.y:g},{t.s}"} for t in f.texts])


def test_schematic_snapshot_survives_source_delete_and_pastes_as_one_edit(editors):
    source, target = editors
    before = source.path.read_bytes()
    snapshot = source.copy_items(sch_items(source))
    assert source.path.read_bytes() == before and source.log.read(limit=100) == []
    assert snapshot["bbox"] == snapshot["view"]["bbox"]
    payload = json.loads(json.dumps(snapshot["payload"]))
    source.delete(sch_items(source))
    count = len(source.log.read(limit=100))
    ids = target.paste_items(payload, 200, -30)
    f = target._load()
    assert ids == ["R1", "R2", "240,0,300,0", "250,70,日本語 note"]
    assert [(c.x, c.y, c.rot, c.flip, c.props["value"]) for c in f.comps] == [
        (240, 30, 1, 1, "22k"), (300, 30, 2, 0, "47k")]
    assert xschem.dump(f.comps[1].embedded) == (target.path.parent / "r.sym").read_text()
    assert f.wires == [(240, 0, 300, 0, {"lab": "signal", "bus": "true"})]
    assert f.texts[0].props == {"weight": "bold"}
    assert len(source.log.read(limit=100)) == count + 1
    after = target.path.read_bytes()
    assert target.undo() and not sch_items(target)
    assert not sch_items(source)
    assert target.redo() and target.path.read_bytes() == after
    target.paste_items(payload, 400, -30)
    assert [c.props["name"] for c in target._load().comps] == ["R1", "R2", "R3", "R4"]
    assert payload == snapshot["payload"]


def test_schematic_copy_rejects_partly_stale_selection(editors):
    source, _ = editors
    with pytest.raises(KeyError):
        source.copy_items(sch_items(source) + [{"k": "i", "id": "missing"}])
    assert source.log.read(limit=100) == []


@pytest.mark.parametrize("damage", ["wrong_kind", "truncated", "own_shapes", "nonfinite", "empty"])
def test_schematic_invalid_clipboard_does_not_modify_destination(editors, damage):
    source, target = editors
    p = source.copy_items(sch_items(source))["payload"]
    if damage == "wrong_kind":
        p["kind"] = "undine-layout"
    elif damage == "truncated":
        p["fragment"] += "C {broken"
    elif damage == "own_shapes":
        p["fragment"] += "B 4 0 0 100 100 {}\n"
    elif damage == "nonfinite":
        f = xschem.parse(p["fragment"])
        f.comps[0].x = float("nan")
        p["fragment"] = xschem.dump(f)
    else:
        p["fragment"] = xschem.dump(xschem.XFile())
    before = target.path.read_bytes()
    with pytest.raises(ValueError):
        target.paste_items(p, 100, 100)
    assert target.path.read_bytes() == before and target.log.read(limit=100) == []


@pytest.fixture
def layout_db(tmp_path):
    db = LayoutDB(Library("test", tmp_path), None, OpLog(tmp_path / "history" / "layout.jsonl"))
    for name in ("source", "target", "child"):
        db.new_cell(name)
    li = db.layout.layer(66, 20)
    db._commit("child", [("ins", li, kdb.Box(-10, -20, 10, 20))])
    props = db.layout.properties_id({17: "numeric key", "net": "signal"})
    instance_props = db.layout.properties_id({"undine_inst": "M1", "device": "nmos", 18: 2.5})
    array = kdb.CellInstArray(db.cell("child").cell_index(), kdb.ICplxTrans(2, 45, True, 300, 400),
                             kdb.Vector(100, 0), kdb.Vector(0, 100), 2, 3)
    selected = db._commit("source", [
        ("ins", li, kdb.Box(0, 0, 100, 200), props),
        ("ins", li, kdb.Path([kdb.Point(0, 300), kdb.Point(200, 300)], 20), 0),
        ("ins", li, kdb.Text("VDD", kdb.Trans(1, True, 10, 40)), 0),
        ("ins", li, kdb.Polygon([kdb.Point(0, 500), kdb.Point(100, 500), kdb.Point(50, 600)]), 0),
        ("iins", array, instance_props)])
    db.save()
    return db, selected


def test_layout_snapshot_survives_source_delete_and_preserves_properties_arrays(layout_db):
    db, selected = layout_db
    count, version = len(db.log.read(limit=100)), db.version
    snapshot = db.copy_items("source", [s["ref"] for s in selected])
    assert len(db.log.read(limit=100)) == count and db.version == version and not db.dirty
    assert snapshot["selection"] == selected
    payload = json.loads(json.dumps(snapshot["payload"]))
    db.delete("source", [s["ref"] for s in selected])
    count = len(db.log.read(limit=100))
    pasted = db.paste_items("target", payload, 1000, -200)
    assert len(pasted) == 5 and len(db.log.read(limit=100)) == count + 1
    box = next(s for s in pasted if s["type"] == "box")
    assert box["bbox"] == [1000, -200, 1100, 0] and box["layer"] == "66/20"
    shape = db._find(db.cell("target"), box["ref"])
    assert shape.property(17) == "numeric key" and shape.property("net") == "signal"
    inst = next(db.cell("target").each_inst())
    assert inst.property("undine_inst") == "M1" and inst.property("device") == "nmos"
    assert inst.property(18) == 2.5
    assert str(inst.cell_inst.cplx_trans) == "m22.5 *2 1300,200"
    assert inst.cell_inst.na == 2 and inst.cell_inst.nb == 3
    assert db.undo() == "target" and db.cell("target").is_empty()
    assert db.cell("source").is_empty()
    assert db.redo() == "target"
    assert db._find(db.cell("target"), box["ref"]).property("net") == "signal"
    assert next(db.cell("target").each_inst()).property("device") == "nmos"
    db.paste_items("target", payload, 2000, -200)
    assert {i.property("undine_inst") for i in db.cell("target").each_inst()} == {"M1", "M2"}
    assert payload == snapshot["payload"]
    # The journal must recreate all copied attributes on a later server load.
    again = LayoutDB(db.lib, None, db.log)
    assert {i.property("device") for i in again.cell("target").each_inst()} == {"nmos"}
    assert again._find(again.cell("target"), box["ref"]).property(17) == "numeric key"


def test_layout_copy_rejects_any_stale_ref_without_editing(layout_db):
    db, selected = layout_db
    refs = [s["ref"] for s in selected]
    stale = {**refs[0], "g": "(999,999;1000,1000)"}
    count, version = len(db.log.read(limit=100)), db.version
    with pytest.raises(KeyError):
        db.copy_items("source", refs + [stale])
    assert len(db.log.read(limit=100)) == count and db.version == version


@pytest.mark.parametrize("damage", ["dbu", "missing_child", "recursive", "invalid_shape", "array", "props"])
def test_layout_invalid_later_object_never_partly_pastes(layout_db, damage):
    db, selected = layout_db
    p = copy.deepcopy(db.copy_items("source", [s["ref"] for s in selected])["payload"])
    if damage == "dbu":
        p["dbu"] *= 10
    elif damage == "missing_child":
        p["effects"][-1]["child"] = "missing"
    elif damage == "recursive":
        p["effects"][-1]["child"] = "target"
    elif damage == "invalid_shape":
        p["effects"][-1] = {**p["effects"][0], "t": "unsupported"}
    elif damage == "array":
        p["effects"][-1]["array"]["na"] = -1
    else:
        p["effects"][-1]["props"] = [["bad", float("nan")]]
    count, version = len(db.log.read(limit=100)), db.version
    layers = list(db.layout.layer_indexes())
    with pytest.raises((ValueError, KeyError)):
        db.paste_items("target", p, 1000, 0)
    assert db.cell("target").is_empty()
    assert len(db.log.read(limit=100)) == count and db.version == version
    assert list(db.layout.layer_indexes()) == layers and not db.dirty


def test_layout_paste_rejects_indirect_cycle(layout_db):
    db, selected = layout_db
    db.place("child", "target", 0, 0)
    snapshot = db.copy_items("source", [selected[-1]["ref"]])
    with pytest.raises(ValueError, match="recursive"):
        db.paste_items("target", snapshot["payload"], 0, 0)
    assert db.cell("target").is_empty()
