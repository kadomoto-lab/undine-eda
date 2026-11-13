"""Connecting pins by net name draws routed wires as a person would, with labels for supplies and unroutable pins,
and changes nothing unless every net ends up whole and no other nets are joined."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine.schematics import schedit, schematic, schroute, xschem  # noqa: E402
from undine.store.oplog import OpLog  # noqa: E402
from undine.schematics.schedit import SchEditor  # noqa: E402

LABEL = "lab.sym"


@pytest.fixture
def ed(tmp_path):
    (tmp_path / "r.sym").write_text(
        'v {}\nK {type=resistor\nformat="@name @pinlist @value"\ntemplate="name=R1 value=1k"}\n'
        'B 4 -10 -15 10 15 {}\n'
        'B 5 -2 -32 2 -28 {name=P}\nB 5 -2 28 2 32 {name=M}\n')
    (tmp_path / LABEL).write_text('v {}\nK {type=label\ntemplate="name=l1 lab=xxx"}\nB 5 -2 -2 2 2 {name=p}\n')
    (tmp_path / "top.sch").write_text(xschem.dump(xschem.XFile()))
    e = SchEditor(tmp_path / "top.sch", xschem.SymbolResolver([tmp_path]), OpLog(tmp_path / "history.jsonl"))
    for i, x in enumerate((0, 100, 200, 300)):
        e.add_instance("r.sym", x, 0, props={"name": f"R{i + 1}"})
    return e


def nets(ed) -> dict[str, set[str]]:
    c = schematic.extract(ed.path, ed.resolver)
    return {name: {f"{d}:{p}" for d, p in pins} for name, pins in c.nets.items() if pins}


def connect(ed, nets_, style="auto"):
    return ed.connect_nets(list(nets_.items()), style, LABEL)


def test_wires_join_the_pins_and_carry_the_net_name(ed):
    r = connect(ed, {"mid": ["R1:M", "R2:M", "R3:M"]})[0]
    assert r["wires_added"] > 0 and r["labelled"] == []
    f = ed._load()
    assert not [c for c in f.comps if c.sym == LABEL]                             # no labels: real wires
    assert any(w[4].get("lab") == "mid" for w in f.wires)
    assert nets(ed)["mid"] == {"R1:M", "R2:M", "R3:M"}
    assert all(w[0] == w[2] or w[1] == w[3] for w in f.wires)                    # orthogonal


def test_nets_stay_apart_and_one_operation_undoes_all(ed):
    connect(ed, {"a": ["R1:P", "R3:P"], "b": ["R2:P", "R4:P"]})
    n = nets(ed)
    assert n["a"] == {"R1:P", "R3:P"} and n["b"] == {"R2:P", "R4:P"}               # crossing nets, no short
    assert ed.undo() and not ed._load().wires                                      # one undo step for both nets


def test_supplies_and_style_label_use_labels(ed):
    r = connect(ed, {"vdd": ["R1:P", "R2:P"], "x": ["R3:M", "R4:M"]})
    assert r[0]["labelled"] == ["R1:P", "R2:P"] and r[0]["wires_added"] == 0
    assert r[1]["wires_added"] > 0
    r = connect(ed, {"y": ["R1:M", "R2:M"]}, style="label")[0]
    assert r["labelled"] == ["R1:M", "R2:M"]
    n = nets(ed)
    assert n["vdd"] == {"R1:P", "R2:P"} and n["y"] == {"R1:M", "R2:M"} and n["x"] == {"R3:M", "R4:M"}


def test_a_pin_without_a_route_in_time_gets_a_label_and_still_joins(ed, monkeypatch):
    monkeypatch.setattr(schedit, "EDIT_BUDGET", 0)          # any search gives up at once; straight routes still pass
    r = connect(ed, {"a": ["R1:P", "R2:M"]})[0]              # needs a detour around R2's body
    assert r["labelled"]                                      # fell back to a label (and the start pin got one too)
    assert nets(ed)["a"] == {"R1:P", "R2:M"}


def test_all_or_nothing_and_clear_errors(ed):
    text = ed.path.read_text()
    with pytest.raises(KeyError, match="no instance R9"):
        connect(ed, {"a": ["R1:P", "R2:P"], "b": ["R9:P"]})
    assert ed.path.read_text() == text                       # the first net was not written either
    with pytest.raises(KeyError, match="has no pin X"):
        connect(ed, {"a": ["R1:X"]})
    with pytest.raises(ValueError, match="instance:pin"):
        connect(ed, {"a": ["R1"]})
    connect(ed, {"a": ["R1:P", "R2:P"]})
    with pytest.raises(ValueError, match="already on net a"):
        connect(ed, {"b": ["R1:P", "R3:P"]})
    r = connect(ed, {"a": ["R1:P", "R3:P"]})[0]              # extend the net from where it is
    assert r["already_on_net"] == ["R1:P"] and nets(ed)["a"] == {"R1:P", "R2:P", "R3:P"}


def test_labels_point_away_from_their_device(ed):
    (ed.path.parent / "m.sym").write_text('v {}\nK {type=nmos\nformat="@name @pinlist"\ntemplate="name=M1"}\n'
                                          'B 4 -10 -10 10 10 {}\nB 5 -22 -2 -18 2 {name=G}\nB 5 18 -2 22 2 {name=D}\n')
    ed.add_instance("m.sym", 0, 200, props={"name": "M1"})
    connect(ed, {"g": ["M1:G"], "d": ["M1:D"]}, style="label")
    flips = {c.props["lab"]: c.flip for c in ed._load().comps if c.sym == LABEL}
    assert flips == {"g": 0, "d": 1}                         # text left of a left pin, right of a right pin
