"""Changing symbol pin geometry preserves consumer connectivity before any file is written."""
import sys
import json
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine.schematics import schematic, schroute, symedit, xschem  # noqa: E402
from undine.store.oplog import OpLog  # noqa: E402
from undine.schematics.schedit import SchEditor  # noqa: E402


class OverlayResolver(xschem.SymbolResolver):
    def __init__(self, paths, overlays=None):
        super().__init__(paths)
        self.overlays = {p.resolve(): f for p, f in (overlays or {}).items()}

    def load(self, name, base):
        path = self.find(name, base)
        return self.overlays[path.resolve()] if path and path.resolve() in self.overlays else super().load(name, base)


@pytest.fixture
def consumer(tmp_path):
    symbol = xschem.make_symbol([("A", "ipin"), ("Y", "opin")])
    (tmp_path / "amp.sym").write_text(xschem.dump(symbol))
    (tmp_path / "port.sym").write_text('v {}\nK {type=ipin}\nB 5 -2 -2 2 2 {name=P dir=in}\n')
    (tmp_path / "label.sym").write_text('v {}\nK {type=label}\nB 5 -2 -2 2 2 {name=P}\n')
    path = tmp_path / "top.sch"
    f = xschem.XFile()
    f.comps = [xschem.XComp("amp.sym", 0, 0, 0, 0, {"name": "X1"}),
               xschem.XComp("port.sym", -200, 0, 0, 0, {"name": "P1", "lab": "INPUT"})]
    f.wires = [(-200, 0, -60, 0, {"lab": "input", "bus": "true"}), (60, 0, 200, 0, {"lab": "output"})]
    path.write_text(xschem.dump(f))
    ed = SchEditor(path, OverlayResolver([tmp_path]), OpLog(tmp_path / "history.jsonl"))
    return ed, symbol


def moved_symbol(symbol, x=-90, y=0):
    proposed = deepcopy(symbol)
    pin = next(o for o in symedit.objects(proposed) if o["kind"] == "pin" and o["name"] == "A")
    symedit.edit(proposed, "symbol_update", {"id": pin["id"], "x": x, "y": y})
    return proposed


def proposed_resolver(ed, symbol):
    return OverlayResolver(ed.resolver.paths, {ed.path.parent / "amp.sym": symbol})


def pin_nets(ed, f, resolver):
    """Ignore remeshed wire IDs while comparing every terminal's connected partners."""
    return frozenset(frozenset(t for t in net if t[0] == "p")
                     for net in ed._connections(f, resolver=resolver) if any(t[0] == "p" for t in net))


def assert_clear(ed, f, resolver):
    assert all(w[0] == w[2] or w[1] == w[3] for w in f.wires)
    assert all(not schroute.segment_blocked(f, resolver, ed.path.parent, w[:2], w[2:4]) for w in f.wires)


def test_plan_preserves_fixed_port_wires_and_properties_without_writing(consumer):
    ed, symbol = consumer
    before = ed._load()
    old_text = {p: p.read_bytes() for p in ed.path.parent.iterdir() if p.is_file()}
    changed_symbol = moved_symbol(symbol, -90, -20)
    resolver = proposed_resolver(ed, changed_symbol)
    prior_resolver, prior_cache = ed.resolver, ed._net_cache
    result = ed.plan_symbol_change(before, ed.resolver, resolver)
    assert result is not before and before == xschem.parse(ed.path.read_text())
    assert ed.resolver is prior_resolver and ed._net_cache is prior_cache
    assert {p: p.read_bytes() for p in old_text} == old_text and not ed.log.path.exists()
    assert result.comps == before.comps
    assert pin_nets(ed, result, resolver) == pin_nets(ed, before, ed.resolver)
    assert any(schematic._on_segment(-90, -20, *w[:4]) for w in result.wires)
    assert any(schematic._on_segment(-200, 0, *w[:4]) for w in result.wires)
    assert {tuple(sorted(w[4].items())) for w in result.wires} == {tuple(sorted(w[4].items())) for w in before.wires}
    assert_clear(ed, result, resolver)


def test_attached_label_follows_pin_and_wire_label_stays_on_its_net(consumer):
    ed, symbol = consumer
    before = ed._load()
    before.comps += [xschem.XComp("label.sym", -60, 0, 0, 0, {"name": "direct", "lab": "input"}),
                     xschem.XComp("label.sym", -140, 0, 0, 0, {"name": "onwire", "lab": "input"})]
    resolver = proposed_resolver(ed, moved_symbol(symbol, -80, -40))
    result = ed.plan_symbol_change(before, ed.resolver, resolver)
    direct = next(c for c in result.comps if c.props["name"] == "direct")
    onwire = next(c for c in result.comps if c.props["name"] == "onwire")
    assert (direct.x, direct.y) == (-80, -40)
    assert any(schematic._on_segment(onwire.x, onwire.y, *w[:4]) for w in result.wires)
    assert (result.comps[1].x, result.comps[1].y) == (-200, 0)
    assert pin_nets(ed, result, resolver) == pin_nets(ed, before, ed.resolver)
    assert_clear(ed, result, resolver)


def test_multiple_rotated_and_mirrored_instances_use_new_transformed_pin_positions(consumer):
    ed, symbol = consumer
    before = xschem.XFile()
    ports = []
    for rot in range(4):
        for flip in range(2):
            n = 2*rot+flip
            ox, oy = n*500, 0
            px, py = xschem._rot(rot, flip, -60, 0)
            tx, ty = xschem._rot(rot, flip, -200, 0)
            before.comps += [xschem.XComp("amp.sym", ox, oy, rot, flip, {"name": f"X{n}"}),
                             xschem.XComp("port.sym", ox+tx, oy+ty, 0, 0, {"name": f"P{n}", "lab": f"N{n}"})]
            before.wires.append((ox+tx, oy+ty, ox+px, oy+py, {"lab": f"N{n}"}))
            ports.append((ox+tx, oy+ty))
    resolver = proposed_resolver(ed, moved_symbol(symbol, -90, -20))
    result = ed.plan_symbol_change(before, ed.resolver, resolver)
    assert [(c.x, c.y) for c in result.comps[1::2]] == ports
    assert pin_nets(ed, result, resolver) == pin_nets(ed, before, ed.resolver)
    new_pins = {key: p for key, _, p, _ in ed._pins(result, resolver)}
    for n in range(8):
        assert any(schematic._on_segment(*new_pins[(2*n, 0)], *w[:4]) for w in result.wires)
    assert_clear(ed, result, resolver)


def test_branch_and_crossing_net_are_preserved(consumer):
    ed, symbol = consumer
    before = ed._load()
    before.comps += [xschem.XComp("port.sym", -160, -100, 0, 0, {"name": "branch", "lab": "input"}),
                     xschem.XComp("port.sym", -120, -100, 0, 0, {"name": "cross", "lab": "separate"})]
    before.wires += [(-160, -100, -160, 0, {}), (-120, -100, -120, 100, {"lab": "separate"})]
    resolver = proposed_resolver(ed, moved_symbol(symbol, -80, -30))
    result = ed.plan_symbol_change(before, ed.resolver, resolver)
    assert pin_nets(ed, result, resolver) == pin_nets(ed, before, ed.resolver)
    assert_clear(ed, result, resolver)


def test_embedded_symbol_is_not_affected_by_external_file_change(consumer):
    ed, symbol = consumer
    before = ed._load()
    before.comps[0].embedded = deepcopy(symbol)
    result = ed.plan_symbol_change(before, ed.resolver, proposed_resolver(ed, moved_symbol(symbol)))
    assert result == before and result is not before


def test_coincident_fixed_port_gets_a_connection_when_pin_moves(consumer):
    ed, symbol = consumer
    before = ed._load()
    before.wires = []
    before.comps[1].x, before.comps[1].y = -60, 0
    resolver = proposed_resolver(ed, moved_symbol(symbol, -40, 0))
    result = ed.plan_symbol_change(before, ed.resolver, resolver)
    assert (result.comps[1].x, result.comps[1].y) == (-60, 0)
    assert result.wires
    assert pin_nets(ed, result, resolver) == pin_nets(ed, before, ed.resolver)
    assert_clear(ed, result, resolver)


@pytest.mark.parametrize("change", ["add", "remove", "rename", "order", "direction", "type"])
def test_interface_changes_are_rejected_before_mutating_consumer(consumer, change):
    ed, symbol = consumer
    before = ed._load()
    untouched = deepcopy(before)
    proposed = moved_symbol(symbol)
    if change == "add":
        proposed.boxes.append((5, 0, 38, 4, 42, {"name": "VSS", "dir": "inout"}))
    elif change == "remove":
        proposed.boxes.pop()
    elif change == "rename":
        proposed.boxes[0][-1]["name"] = "INPUT"
    elif change == "order":
        proposed.boxes.reverse()
    elif change == "direction":
        proposed.boxes[0][-1]["dir"] = "inout"
    else:
        proposed.global_props["type"] = "label"
    with pytest.raises(ValueError, match="must stay unchanged"):
        ed.plan_symbol_change(before, ed.resolver, proposed_resolver(ed, proposed))
    assert before == untouched and not ed.log.path.exists()


def test_colliding_different_net_pins_fail_without_changing_design(consumer):
    ed, symbol = consumer
    before = ed._load()
    prior = deepcopy(before)
    resolver = proposed_resolver(ed, moved_symbol(symbol, 60, 0))
    with pytest.raises(ValueError):
        ed.plan_symbol_change(before, ed.resolver, resolver)
    assert before == prior and ed._load() == prior and not ed.log.path.exists()


def test_direct_undo_redo_cannot_partially_apply_a_grouped_symbol_edit(consumer):
    ed, _ = consumer
    initial = ed.path.read_text()
    ed.add_text("annotation", 0, 100)
    record = ed.log.read()[-1]
    record["symbol_group"] = {"id": "group-1", "members": [["l", "amp", "symbol"], ["l", "top", "schematic"]]}
    ed.log.path.write_text(json.dumps(record)+"\n")
    modified = ed.path.read_text()
    with pytest.raises(ValueError, match="grouped Undo/Redo API"):
        ed.undo()
    assert ed.path.read_text() == modified and len(ed.log.read()) == 1
    # The group transaction owns this state transition and logs it itself.
    ed.path.write_text(initial)
    ed.log.append({"op": "undo", "of": record["seq"], "cell": ed.cell, "view": ed.view_kind,
                   "actor": "user", "effects": None})
    with pytest.raises(ValueError, match="grouped Undo/Redo API"):
        ed.redo()
    assert ed.path.read_text() == initial and len(ed.log.read()) == 2
