"""Connections survive interactive schematic editing, including undo and failures."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine.schematics import schematic, schroute, xschem  # noqa: E402
from undine.store.oplog import OpLog  # noqa: E402
from undine.schematics.schedit import SchEditor  # noqa: E402


@pytest.fixture
def ed(tmp_path):
    (tmp_path / "r.sym").write_text(
        'v {}\nK {type=resistor\nformat="@name @pinlist @value"\ntemplate="name=R1 value=1k"}\n'
        'B 4 -10 -15 10 15 {}\n'
        'B 5 -2 -32 2 -28 {name=P}\nB 5 -2 28 2 32 {name=M}\n')
    (tmp_path / "port.sym").write_text(
        'v {}\nK {type=ipin\ntemplate="name=p1 lab=IN"}\nB 5 -2 -2 2 2 {name=P}\n')
    (tmp_path / "top.sch").write_text(xschem.dump(xschem.XFile()))
    return SchEditor(tmp_path / "top.sch", xschem.SymbolResolver([tmp_path]),
                     OpLog(tmp_path / "history.jsonl"))


def instance(ed, name, x, y):
    return ed.add_instance("r.sym", x, y, props={"name": name})


def mos(ed):
    # Drawn pin leads put terminals inside expanded symbol clearance, as in
    # SKY130 MOS symbols. Gate branches must share the outward corridor.
    (ed.path.parent / "mos.sym").write_text(
        'v {}\nK {type=nmos\nformat="@name @pinlist model"\ntemplate="name=M1"}\n'
        'B 4 -5 -10 20 10 {}\nL 4 -20 0 -5 0 {}\n'
        'L 4 20 -30 20 -10 {}\nL 4 20 10 20 30 {}\n'
        'B 5 -22 -2 -18 2 {name=G}\n'
        'B 5 18 -32 22 -28 {name=D}\nB 5 18 28 22 32 {name=S}\n')
    return ed.add_instance("mos.sym", 0, 0, props={"name": "M1"})


def move(ed, names, dx, dy, **kw):
    return ed.move([{"k": "i", "id": name} for name in names], dx, dy, **kw)


def nets(ed):
    """Port/net membership without depending on anonymous wire numbering."""
    c = schematic.extract(ed.path, ed.resolver)
    return {frozenset(pins) for pins in c.nets.values() if pins}, c.ports


def assert_orthogonal_clear(ed):
    f = ed._load()
    assert all(w[0] == w[2] or w[1] == w[3] for w in f.wires)
    assert all(not schroute.segment_blocked(f, ed.resolver, ed.path.parent, w[:2], w[2:4]) for w in f.wires)
    assert len(f.wires) == len({xschem.wire_id(*w[:4]) for w in f.wires})


def test_move_connected_pin_and_undo_redo_is_one_operation(ed):
    instance(ed, "R1", 0, 0)
    instance(ed, "R2", 200, 0)
    ed.add_wire(0, -30, 200, -30, "signal")
    before = xschem.dump(ed._load())
    membership = nets(ed)
    n = len(ed.log.read(limit=100))
    assert move(ed, ["R1"], 40, 80) == ["R1"]
    assert nets(ed) == membership
    assert all(w[4].get("lab") == "signal" for w in ed._load().wires)
    assert len(ed.log.read(limit=100)) == n + 1
    assert_orthogonal_clear(ed)
    after = xschem.dump(ed._load())
    assert ed.undo() and xschem.dump(ed._load()) == before
    assert ed.redo() and xschem.dump(ed._load()) == after


def test_move_pin_attached_in_middle_of_wire_keeps_both_branches(ed):
    instance(ed, "R1", 0, 0)
    instance(ed, "R2", -200, 0)
    instance(ed, "R3", 200, 0)
    ed.add_wire(-200, -30, 200, -30)
    before = nets(ed)
    move(ed, ["R1"], 30, 100)
    assert nets(ed) == before
    assert_orthogonal_clear(ed)


def test_moving_midspan_pin_relocates_junction_when_body_covers_old_point(ed):
    instance(ed, "R1", 0, 0)
    instance(ed, "R2", -200, 0)
    instance(ed, "R3", 200, 0)
    ed.add_wire(-200, -30, 200, -30)
    before = nets(ed)
    move(ed, ["R1"], 0, -10)
    assert nets(ed) == before
    assert_orthogonal_clear(ed)


def test_move_shared_pins_without_a_wire_adds_connecting_lead(ed):
    instance(ed, "R1", 0, 0)
    instance(ed, "R2", 0, -60)
    before = nets(ed)
    move(ed, ["R1"], 100, 0)
    assert nets(ed) == before
    assert ed._load().wires
    assert_orthogonal_clear(ed)


def test_port_terminal_is_exposed_and_moves_with_its_connection(ed):
    instance(ed, "R1", 100, 0)
    ed.add_instance("port.sym", 0, -30, props={"name": "input", "lab": "VIN"})
    ed.add_wire(0, -30, 100, -30)
    assert ["input", "P", 0.0, -30.0] in ed.view()["terminals"]
    before = nets(ed)
    move(ed, ["input"], -50, -50)
    assert nets(ed) == before
    assert schematic.extract(ed.path, ed.resolver).devices[0].nets["P"] == "VIN"
    assert_orthogonal_clear(ed)


def test_move_pin_shared_with_port_and_existing_wire(ed):
    instance(ed, "R1", 0, 0)
    instance(ed, "R2", 200, 0)
    ed.add_instance("port.sym", 0, -30, props={"name": "input", "lab": "VIN"})
    ed.add_wire(0, -30, 200, -30)
    before = nets(ed)
    move(ed, ["R1"], 80, 50)
    assert nets(ed) == before
    assert_orthogonal_clear(ed)


def test_bent_wire_and_branch_keep_connectivity(ed):
    instance(ed, "R1", 0, 0)
    instance(ed, "R2", 200, 0)
    instance(ed, "R3", 100, -150)
    ed.add_wire(0, -30, 0, -80)
    ed.add_wire(0, -80, 200, -80)
    ed.add_wire(200, -80, 200, -30)
    ed.add_wire(100, -120, 100, -80)
    before = nets(ed)
    move(ed, ["R1"], -50, 50)
    assert nets(ed) == before
    assert_orthogonal_clear(ed)


def test_moving_mos_gate_branch_reuses_only_its_own_outward_lead(ed):
    mos(ed)
    ed.add_wire(-100, 0, -20, 0, "gate")
    ed.add_wire(-20, 0, -20, -100)
    ed.add_wire(-60, -150, -60, 150, "other")
    before = nets(ed)
    move(ed, ["M1"], 50, 60)
    assert nets(ed) == before
    assert schematic.extract(ed.path, ed.resolver).devices[0].nets["G"] == "gate"
    assert_orthogonal_clear(ed)


def test_second_mos_branch_preview_reuses_lead_without_duplicate_wire(ed):
    mos(ed)
    ed.route_wire(-20, 0, -100, 0)
    original = ed._load().wires
    before = ed.path.read_text()
    preview = ed.wire_route(-20, 0, -20, -100)
    assert ed.path.read_text() == before
    added = ed.route_wire(-20, 0, -20, -100)
    assert added
    f = ed._load()
    assert f.wires[:len(original)] == original
    assert all(not ed._uncovered(a, b, f.wires) for a, b in zip(preview, preview[1:]))
    # Every newly persisted segment is absent from the preceding drawing.
    assert all(ed._uncovered(w[:2], w[2:4], original) == [(w[:2], w[2:4])]
               for w in f.wires[len(original):])
    assert_orthogonal_clear(ed)
    assert ed.undo() and ed._load().wires == original


def test_fractional_move_keeps_terminal_escape_and_netlist(ed):
    mos(ed)
    ed.add_wire(-100, 0, -20, 0, "gate")
    before = nets(ed)
    move(ed, ["M1"], 1 / 3, 60)
    assert nets(ed) == before
    assert schematic.extract(ed.path, ed.resolver).devices[0].nets["G"] == "gate"


def test_move_both_connected_devices_and_selected_wire_only_once(ed):
    instance(ed, "R1", 0, 0)
    instance(ed, "R2", 200, 0)
    wire = ed.add_wire(0, -30, 200, -30)
    before = nets(ed)
    result = ed.move([{"k": "i", "id": "R1"}, {"k": "i", "id": "R2"},
                      {"k": "w", "id": wire}, {"k": "i", "id": "R1"}], 40, 100)
    assert result == ["R1", "R2", "40,70,240,70"]
    assert ed._load().wires[0][:4] == (40, 70, 240, 70)
    assert nets(ed) == before
    assert_orthogonal_clear(ed)


def test_move_both_connected_devices_without_selecting_wire(ed):
    instance(ed, "R1", 0, 0)
    instance(ed, "R2", 200, 0)
    ed.add_wire(0, -30, 200, -30)
    before = nets(ed)
    move(ed, ["R1", "R2"], 30, 100)
    assert ed._load().wires[0][:4] == (30, 70, 230, 70)
    assert nets(ed) == before
    assert_orthogonal_clear(ed)


def test_moving_only_wire_keeps_fixed_terminal_attachments(ed):
    instance(ed, "R1", 0, 0)
    instance(ed, "R2", 200, 0)
    wire = ed.add_wire(0, -30, 200, -30)
    membership = nets(ed)
    result = ed.move([{"k": "w", "id": wire}], 0, -50)
    assert result
    assert "0,-80,200,-80" in result
    assert nets(ed) == membership
    assert_orthogonal_clear(ed)


@pytest.mark.parametrize("rotate,mirror", [(1, False), (2, False), (0, True), (1, True)])
def test_rotate_and_mirror_keep_pin_identity(ed, rotate, mirror):
    instance(ed, "R1", 0, 0)
    instance(ed, "R2", 200, 0)
    instance(ed, "R3", -200, 0)
    ed.add_wire(0, -30, 200, -30, "top")
    ed.add_wire(-200, 30, 0, 30, "bottom")
    before = nets(ed)
    ed.orient([{"k": "i", "id": "R1"}], rotate=rotate, mirror=mirror)
    assert nets(ed) == before
    c = schematic.extract(ed.path, ed.resolver)
    assert c.devices[0].nets == {"P": "top", "M": "bottom"}
    assert_orthogonal_clear(ed)


def test_moving_body_reroutes_unrelated_wire_without_joining_pins(ed):
    instance(ed, "R1", 100, 150)
    ed.add_wire(0, 0, 200, 0, "other")
    before = nets(ed)
    move(ed, ["R1"], 0, -150)
    assert nets(ed) == before
    assert len(ed._load().wires) > 1
    assert all(w[4].get("lab") == "other" for w in ed._load().wires)
    assert_orthogonal_clear(ed)


def test_moving_body_over_floating_loop_keeps_conductor(ed):
    instance(ed, "R1", 100, 150)
    ed.add_wire(0, 0, 200, 0, "floating")
    ed.add_wire(200, 0, 200, -100)
    ed.add_wire(200, -100, 0, -100)
    ed.add_wire(0, -100, 0, 0)
    membership = nets(ed)
    move(ed, ["R1"], 0, -150)
    assert ed._load().wires
    assert any(w[4].get("lab") == "floating" for w in ed._load().wires)
    assert nets(ed) == membership
    assert_orthogonal_clear(ed)


def test_rebuilt_connection_keeps_all_original_labels(ed):
    instance(ed, "R1", 0, 0)
    instance(ed, "R2", 200, 0)
    ed.add_wire(0, -30, 0, -80, "source")
    ed.add_wire(0, -80, 200, -80, "alias")
    ed.add_wire(200, -80, 200, -30, "dest")
    move(ed, ["R1"], 0, -80)
    assert {w[4].get("lab") for w in ed._load().wires} == {"source", "alias", "dest"}
    c = schematic.extract(ed.path, ed.resolver)
    assert c.devices[0].nets["P"] == c.devices[1].nets["P"]
    assert_orthogonal_clear(ed)


def test_move_keeps_crossing_nets_separate(ed):
    instance(ed, "R1", 0, 0)
    instance(ed, "R2", 200, 0)
    instance(ed, "R3", 100, -150)
    ed.add_wire(0, -30, 200, -30, "horizontal")
    ed.add_wire(100, -120, 100, 100, "vertical")
    before = nets(ed)
    move(ed, ["R1"], -50, 50)
    assert nets(ed) == before
    c = schematic.extract(ed.path, ed.resolver)
    assert c.devices[0].nets["P"] == c.devices[1].nets["P"] == "horizontal"
    assert c.devices[2].nets["M"] == "vertical"
    assert_orthogonal_clear(ed)


def test_copy_does_not_drag_original_wires(ed):
    instance(ed, "R1", 0, 0)
    instance(ed, "R2", 200, 0)
    ed.add_wire(0, -30, 200, -30)
    wires = ed._load().wires
    assert move(ed, ["R1"], 0, 150, copy=True) == ["R3"]
    assert ed._load().wires == wires
    c = schematic.extract(ed.path, ed.resolver)
    assert c.devices[0].nets["P"] == c.devices[1].nets["P"]
    assert c.devices[2].nets["P"] != c.devices[0].nets["P"]


def test_unconnected_pin_moves_wire_away_without_silently_shorting_it(ed):
    instance(ed, "R1", 100, 150)
    ed.add_wire(0, 0, 200, 0)
    membership = nets(ed)
    move(ed, ["R1"], 0, -120)
    assert nets(ed) == membership
    assert not any(schematic._on_segment(100, 0, *w[:4]) for w in ed._load().wires)
    assert_orthogonal_clear(ed)


def test_move_over_old_bend_rebuilds_whole_connection(ed):
    instance(ed, "R1", 0, 0)
    instance(ed, "R2", 200, 0)
    ed.add_wire(0, -30, 0, -100, "signal")
    ed.add_wire(0, -100, 200, -100)
    ed.add_wire(200, -100, 200, -30)
    membership = nets(ed)
    before = ed.path.read_text(), ed.log.path.read_text()
    preview = ed.preview_move([{"k": "i", "id": "R1"}], 0, -80)
    assert (ed.path.read_text(), ed.log.path.read_text()) == before
    assert preview["result"] == ["R1"]
    assert ["R1", "P", 0, -110] in preview["view"]["terminals"]
    assert move(ed, ["R1"], 0, -80) == preview["result"]
    view = xschem.render_model(ed._load(), ed.path, ed.resolver)
    assert all(preview["view"][k] == value for k, value in view.items())
    assert nets(ed) == membership
    assert_orthogonal_clear(ed)
    after = ed._load()
    assert ed.undo()
    original = xschem.parse(before[0])
    assert sorted(ed._load().wires, key=str) == sorted(original.wires, key=str)
    assert ed._load().comps == original.comps
    assert ed.redo()
    assert sorted(ed._load().wires, key=str) == sorted(after.wires, key=str)
    assert ed._load().comps == after.comps


def test_move_over_old_branch_relocates_junction(ed):
    instance(ed, "R1", 0, 0)
    instance(ed, "R2", 200, 0)
    instance(ed, "R3", 100, -150)
    ed.add_wire(0, -30, 0, -80)
    ed.add_wire(0, -80, 200, -80)
    ed.add_wire(200, -80, 200, -30)
    ed.add_wire(100, -120, 100, -80)
    membership = nets(ed)
    move(ed, ["R1"], 100, -60)
    assert nets(ed) == membership
    assert_orthogonal_clear(ed)


def test_group_move_rebuilds_multiply_connected_net(ed):
    instance(ed, "R1", 0, 0)
    instance(ed, "R2", 200, 0)
    instance(ed, "R3", 100, -150)
    ed.add_wire(0, -30, 0, -80)
    ed.add_wire(0, -80, 200, -80)
    ed.add_wire(200, -80, 200, -30)
    ed.add_wire(100, -120, 100, -80)
    membership = nets(ed)
    move(ed, ["R1", "R2"], 0, -60)
    assert nets(ed) == membership
    assert_orthogonal_clear(ed)


@pytest.mark.parametrize("delta", [(0, 10), (0, -10), (10, 0), (-10, 0), (80, -80)])
def test_device_attached_net_labels_follow_small_and_large_moves(ed, delta):
    mos(ed)
    (ed.path.parent / "label.sym").write_text(
        'v {}\nK {type=label\ntemplate="name=l1 lab=gate"}\nB 5 -2 -2 2 2 {name=P}\n')
    for name, x, y in [("gate", -20, 0), ("drain", 20, -30), ("source", 20, 30)]:
        ed.add_instance("label.sym", x, y, props={"name": name, "lab": name})
    ed.add_wire(-100, 0, -20, 0)
    membership = nets(ed)
    original = {c.props["name"]: (c.x, c.y) for c in ed._load().comps}
    move(ed, ["M1"], *delta)
    assert nets(ed) == membership
    for c in ed._load().comps:
        x, y = original[c.props["name"]]
        assert (c.x, c.y) == (x + delta[0], y + delta[1])
    assert_orthogonal_clear(ed)


def test_label_shared_with_fixed_device_stays_with_fixed_pin(ed):
    instance(ed, "R1", 0, 0)
    instance(ed, "R2", 0, -60)
    (ed.path.parent / "label.sym").write_text(
        'v {}\nK {type=label}\nB 5 -2 -2 2 2 {name=P}\n')
    ed.add_instance("label.sym", 0, -30, props={"name": "shared", "lab": "shared"})
    membership = nets(ed)
    move(ed, ["R1"], 100, 0)
    label = ed._load().comps[-1]
    assert (label.x, label.y) == (0, -30)
    assert nets(ed) == membership
    assert_orthogonal_clear(ed)


def test_label_on_wire_can_move_out_of_the_way_of_device(ed):
    instance(ed, "R1", 100, 150)
    (ed.path.parent / "label.sym").write_text(
        'v {}\nK {type=label}\nB 5 -2 -2 2 2 {name=P}\n')
    ed.add_instance("label.sym", 100, 0, props={"name": "wire_label", "lab": "signal"})
    ed.add_wire(0, 0, 200, 0)
    membership = nets(ed)
    move(ed, ["R1"], 0, -150)
    label = ed._load().comps[-1]
    assert (label.x, label.y) != (100, 0)
    assert nets(ed) == membership
    assert schematic.extract(ed.path, ed.resolver).wire_net.count("signal") == len(ed._load().wires)
    assert_orthogonal_clear(ed)


def wire_label(ed, name, x, y, *, lab="signal", rot=0, flip=0, offset=False):
    (ed.path.parent / "label.sym").write_text(
        'v {}\nK {type=label}\n' +
        ('B 5 8 -2 12 2 {name=P}\n' if offset else 'B 5 -2 -2 2 2 {name=P}\n'))
    return ed.add_instance("label.sym", x, y, rot, flip, props={"name": name, "lab": lab})


def label_point(ed, name):
    f = ed._load()
    ids = xschem.comp_ids(f)
    return next(point for (ci, _), _, point, _ in ed._pins(f) if ids[ci] == name)


@pytest.mark.parametrize("x", [0., 60., 200.])
@pytest.mark.parametrize("also_label", [False, True])
def test_selected_wire_carries_its_label_once_from_end_or_interior(ed, x, also_label):
    wire = ed.add_wire(0, -100, 200, -100)
    wire_label(ed, "label", x, -100)
    before = ed.path.read_text()
    history = ed.log.read()
    items = [{"k": "w", "id": wire}]
    if also_label:
        items += [{"k": "i", "id": "label"}, {"k": "i", "id": "label"}]
    preview = ed.preview_move(items, 40.5, 70.25)
    assert ed.path.read_text() == before and ed.log.read() == history
    ed.move(items, 40.5, 70.25)
    assert label_point(ed, "label") == (x + 40.5, -29.75)
    assert preview["view"]["wires"] == ed.view()["wires"]
    assert preview["view"]["insts"] == ed.view()["insts"]
    assert len(ed.log.read()) == len(history) + 1
    assert ed.undo() and ed.path.read_text() == before


def test_connected_devices_carry_wire_label_by_same_translation(ed):
    instance(ed, "R1", 0, 0)
    instance(ed, "R2", 200, 0)
    ed.add_wire(0, -30, 200, -30)
    wire_label(ed, "label", 60, -30, rot=1, flip=1)
    membership = nets(ed)
    move(ed, ["R1", "R2"], 40.5, 70.25)
    assert label_point(ed, "label") == (100.5, 40.25)
    assert (ed._load().comps[-1].rot, ed._load().comps[-1].flip) == (1, 1)
    assert ed._load().comps[-1].props == {"name": "label", "lab": "signal"}
    assert nets(ed) == membership
    assert_orthogonal_clear(ed)


def test_label_on_rerouted_wire_follows_new_conductor_instead_of_old_bend(ed):
    instance(ed, "R1", 0, 0)
    instance(ed, "R2", 200, 0)
    ed.add_wire(0, -30, 0, -100)
    ed.add_wire(0, -100, 200, -100)
    ed.add_wire(200, -100, 200, -30)
    wire_label(ed, "label", 80, -100)
    membership = nets(ed)
    move(ed, ["R1"], 0, -80)
    point = label_point(ed, "label")
    assert point != (80, -100)
    assert any(schematic._on_segment(*point, *w[:4]) for w in ed._load().wires)
    assert not any(schematic._on_segment(80, -100, *w[:4]) for w in ed._load().wires)
    assert nets(ed) == membership
    assert_orthogonal_clear(ed)


def test_same_named_separate_wire_label_stays_with_its_physical_conductor(ed):
    moving = ed.add_wire(0, 0, 100, 0)
    remote = ed.add_wire(0, 40, 100, 40)
    wire_label(ed, "moving", 60, 0)
    wire_label(ed, "remote", 60, 40)
    ed.move([{"k": "w", "id": moving}], 20, 30)
    assert label_point(ed, "moving") == (80, 30)
    assert label_point(ed, "remote") == (60, 40)
    f = ed._load()
    assert any(xschem.wire_id(*w[:4]) == remote for w in f.wires)
    assert not any(schematic._on_segment(60, 40, *w[:4]) for w in f.wires
                   if xschem.wire_id(*w[:4]) != remote)


def test_wire_motion_keeps_label_shared_with_fixed_port_at_port(ed):
    wire = ed.add_wire(0, 0, 200, 0)
    ed.add_instance("port.sym", 0, 0, props={"name": "input", "lab": "signal"})
    wire_label(ed, "label", 0, 0)
    membership = nets(ed)
    ed.move([{"k": "w", "id": wire}], 40, -50)
    assert label_point(ed, "label") == (0, 0)
    assert label_point(ed, "input") == (0, 0)
    assert nets(ed) == membership


@pytest.mark.parametrize("rotate,mirror", [(1, False), (0, True), (1, True), (2, False)])
def test_offset_label_pin_stays_attached_when_rotating_device_and_label(ed, rotate, mirror):
    instance(ed, "R1", 0, 0)
    wire_label(ed, "label", -10, -30, offset=True)
    membership = nets(ed)
    ed.orient([{"k": "i", "id": "R1"}, {"k": "i", "id": "label"}], rotate, mirror)
    pin = next(point for (ci, pi), _, point, _ in ed._pins(ed._load()) if ci == 0 and pi == 0)
    assert label_point(ed, "label") == pin
    assert nets(ed) == membership
    assert not ed._load().wires


def test_failed_move_preview_leaves_files_history_unchanged(ed):
    instance(ed, "R1", 0, 0)
    instance(ed, "R2", 200, 0)
    ed.add_wire(0, -30, 200, -30)
    before = ed.path.read_text(), ed.log.path.read_text()
    with pytest.raises(ValueError):
        ed.preview_move([{"k": "i", "id": "R1"}], 200, 30)
    assert (ed.path.read_text(), ed.log.path.read_text()) == before


def test_failed_route_never_commits_partial_move(ed, monkeypatch):
    instance(ed, "R1", 0, 0)
    instance(ed, "R2", 200, 0)
    ed.add_wire(0, -30, 200, -30)
    before = ed.path.read_text(), ed.log.path.read_text()

    def fail(*a, **kw):
        raise ValueError("No safe wire route")

    monkeypatch.setattr(schroute, "route", fail)
    with pytest.raises(ValueError, match="No safe wire route"):
        move(ed, ["R1"], 50, 70)
    assert (ed.path.read_text(), ed.log.path.read_text()) == before


def test_routed_connection_is_atomic_deduplicated_and_undoable(ed):
    instance(ed, "R1", 100, 0)
    before = ed.path.read_text()
    log_count = len(ed.log.read(limit=100))
    assert ed.wire_route(0, 0, 200, 0)
    assert ed.path.read_text() == before
    ids = ed.route_wire(0, 0, 200, 0, "N")
    assert len(ids) >= 3
    assert len(ed.log.read(limit=100)) == log_count + 1
    assert_orthogonal_clear(ed)
    assert ed.undo() and not ed._load().wires
    assert ed.redo() and [xschem.wire_id(*w[:4]) for w in ed._load().wires] == ids
    n = len(ed.log.read(limit=100))
    assert ed.route_wire(0, 0, 0, 0) == []
    assert len(ed.log.read(limit=100)) == n


def test_routing_an_existing_straight_segment_is_noop(ed):
    ed.add_wire(0, 0, 100, 0)
    before = ed.path.read_text(), ed.log.path.read_text()
    assert ed.wire_route(100, 0, 0, 0) == [(100, 0), (0, 0)]
    assert ed.route_wire(100, 0, 0, 0) == []
    assert (ed.path.read_text(), ed.log.path.read_text()) == before


def test_continuing_from_waypoint_keeps_it_as_a_bend_instead_of_a_stub(ed):
    instance(ed, "R1", 20, 0)
    ed.add_wire(20, -170, 20, -110)
    ed.add_wire(20, -110, 90, -110)
    before = ed.path.read_text(), ed.log.path.read_text()
    preview = ed.wire_route(90, -110, 20, -30)
    assert preview == [(90, -110), (90, -30), (20, -30)]
    assert (ed.path.read_text(), ed.log.path.read_text()) == before
    ed.route_wire(90, -110, 20, -30)
    f = ed._load()
    assert len(f.wires) == 4
    assert all(not ed._uncovered(a, b, f.wires) for a, b in zip(preview, preview[1:]))
    assert_orthogonal_clear(ed)
    assert ed.undo() and ed.path.read_text() == before[0]


def test_connection_cache_tracks_wire_labels_and_resolved_symbol_pins(ed):
    instance(ed, "R1", 0, 0)
    ed.add_wire(0, -30, 100, -30, "one")
    ed.add_wire(200, 100, 300, 100, "two")
    f = ed._load()
    initial = ed._connections(f)
    assert ed._connections(f) is initial
    f.wires[1][4]["lab"] = "one"
    renamed = ed._connections(f)
    assert renamed != initial
    f.comps[0].embedded = xschem.parse('B 5 -2 98 2 102 {name=P}\nB 5 -2 28 2 32 {name=M}\n')
    assert ed._connections(f) != renamed
