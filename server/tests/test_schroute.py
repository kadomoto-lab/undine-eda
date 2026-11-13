"""Routing safety is checked against the actual schematic connectivity rules."""
from pathlib import Path
import math

import pytest

from undine.schematics import schematic, schroute, xschem
from undine.schematics.xschem import XComp, XFile, XText, SymbolResolver


def device(x=0, y=0, *, rot=0, flip=0, label=False):
    sym = XFile(global_props={"type": "label" if label else "resistor"})
    sym.boxes = [(4, -20, -10, 20, 10, {}),
                 (5, -32, -2, -28, 2, {"name": "P"}),
                 (5, 28, -2, 32, 2, {"name": "N"})]
    sym.lines = [(4, -30, 0, -20, 0, {}), (4, 20, 0, 30, 0, {})]
    sym.texts = [XText("a very long property far away", -500, -500, 0, 0, .3, .3, {})]
    return XComp("r.sym", x, y, rot, flip, {"name": "R1"}, sym)


def routed(f, a, b, **kwargs):
    before = xschem.dump(f)
    path = schroute.route(f, SymbolResolver([]), Path("."), a, b, **kwargs)
    assert xschem.dump(f) == before
    assert path[0] == a and path[-1] == b
    assert all(p != q and (p[0] == q[0] or p[1] == q[1]) for p, q in zip(path, path[1:]))
    assert path == schroute.route(f, SymbolResolver([]), Path("."), a, b, **kwargs)
    return path


def no_short(path, wires, terminals=()):
    for p, q in zip(path, path[1:]):
        for point in terminals:
            assert point in (path[0], path[-1]) or not schematic._on_segment(*point, *p, *q)
        for wire in wires:
            for point in (wire[:2], wire[2:4]):
                assert point in (path[0], path[-1]) or not schematic._on_segment(*point, *p, *q)
    for point in path[1:-1]:
        assert not any(schematic._on_segment(*point, *wire[:4]) for wire in wires)


def test_empty_preserves_off_grid_endpoints():
    assert routed(XFile(), (.125, 3.75), (95.5, 41.25)) == [(.125, 3.75), (95.5, 3.75), (95.5, 41.25)]
    assert routed(XFile(), (.125, 3.75), (95.5, 3.75)) == [(.125, 3.75), (95.5, 3.75)]


def test_routes_around_body_and_ignores_annotation_bounds():
    f = XFile(comps=[device()])
    path = routed(f, (-100, 0), (100, 0))
    assert len(path) == 4
    assert abs(path[1][1]) == 20
    assert not any(ob.hits(p, q) for ob in schroute.body_obstacles(f, SymbolResolver([]), Path("."))
                   for p, q in zip(path, path[1:]))


@pytest.mark.parametrize("rot", range(4))
@pytest.mark.parametrize("flip", range(2))
def test_rotated_and_mirrored_pin_escapes_outward(rot, flip):
    f = XFile(comps=[device(17.25, 4.5, rot=rot, flip=flip)])
    rx, ry = xschem._rot(rot, flip, -30, 0)
    start = (17.25 + rx, 4.5 + ry)
    # Request a target on the opposite side: the first segment must escape
    # outward and the remaining path must go around the body.
    end = (17.25 - rx * 4, 4.5 - ry * 4)
    path = routed(f, start, end)
    assert (path[1][0] - start[0]) * rx + (path[1][1] - start[1]) * ry > 0
    assert all(not schroute.segment_blocked(f, SymbolResolver([]), Path("."), p, q)
               for p, q in zip(path, path[1:]))


def test_label_graphics_do_not_block_but_their_terminal_does():
    f = XFile(comps=[device(label=True)])
    assert schroute.body_obstacles(f, SymbolResolver([]), Path(".")) == []
    assert routed(f, (0, -100), (0, 100)) == [(0, -100), (0, 100)]
    path = routed(f, (-100, 0), (100, 0))
    no_short(path, [], schroute.terminal_points(f, SymbolResolver([]), Path(".")))


def test_corner_pin_follows_lead_orientation_instead_of_nearest_side_tie():
    sym = XFile(lines=[(4, -20, 0, 0, 0, {}), (4, 20, -30, 20, -10, {}),
                       (4, 20, 10, 20, 30, {})],
                boxes=[(5, 19, -31, 21, -29, {"name": "D"})])
    f = XFile(comps=[XComp("mos.sym", 0, 0, 0, 0, {}, sym)])
    path = routed(f, (20, -30), (100, -30))
    assert path[1][0] == 20 and path[1][1] < -30


@pytest.mark.parametrize("pin_y", [-30, -25])
def test_pin_outside_or_on_clearance_does_not_excuse_crossing_whole_body(pin_y):
    sym = XFile(boxes=[(4, -10, -15, 10, 15, {}),
                       (5, -1, pin_y - 1, 1, pin_y + 1, {"name": "P"})])
    f = XFile(comps=[XComp("no-lead.sym", 0, 0, 0, 0, {}, sym)])
    assert schroute.segment_blocked(f, SymbolResolver([]), Path("."), (0, pin_y), (0, 100))
    path = routed(f, (0, pin_y), (0, 100))
    assert len(path) >= 4
    assert all(not ob.hits(a, b) for ob in schroute.body_obstacles(f, SymbolResolver([]), Path("."))
               for a, b in zip(path, path[1:]))


def test_safe_crossing_is_straight_with_no_junction():
    f = XFile(wires=[(0, -100, 0, 100, {})])
    assert routed(f, (-50, 0), (50, 0)) == [(-50, 0), (50, 0)]


def test_never_bends_on_existing_wire():
    f = XFile(wires=[(20, -20, 20, 20, {}), (-20, 20, 20, 20, {})])
    path = routed(f, (0, 0), (40, 40))
    no_short(path, f.wires)


def test_avoids_unrelated_wire_endpoints_and_collinear_overlap():
    f = XFile(wires=[(0, 0, 100, 0, {})])
    path = routed(f, (-50, 0), (150, 0))
    assert len(path) == 4
    assert path[1][1] != 0
    no_short(path, f.wires)


def test_endpoint_may_join_existing_wire_interior():
    f = XFile(wires=[(0, -100, 0, 100, {})])
    assert routed(f, (-50, 0), (0, 0)) == [(-50, 0), (0, 0)]


def test_same_net_wire_exemption_is_explicit():
    f = XFile(wires=[(0, 0, 100, 0, {})])
    assert routed(f, (-50, 0), (150, 0), allowed_wire_indices={0}) == [(-50, 0), (150, 0)]


@pytest.mark.parametrize("rot", range(4))
@pytest.mark.parametrize("reverse_wire", [False, True])
def test_extension_turns_at_dangling_endpoint_instead_of_retracing(rot, reverse_wire):
    rotate = lambda p: xschem._rot(rot, 0, *p)
    a, start, end = map(rotate, [(0, 0), (100, 0), (0, 100)])
    wire = (*start, *a, {}) if reverse_wire else (*a, *start, {})
    f = XFile(wires=[wire])
    path = routed(f, start, end, allowed_wire_indices={0}, avoid_backtrack=True)
    assert path == [start, rotate((100, 100)), end]
    forward = rotate((200, 0))
    assert routed(f, start, forward, allowed_wire_indices={0}, avoid_backtrack=True) == [start, forward]


def test_multiple_bodies_need_multiple_bends():
    f = XFile(comps=[device(-40, -10, rot=1), device(40, 10, rot=1)])
    path = routed(f, (-120, 0), (120, 0))
    assert len(path) >= 4
    assert all(not schroute.segment_blocked(f, SymbolResolver([]), Path("."), p, q)
               for p, q in zip(path, path[1:]))


def test_arc_polygon_and_zero_width_line_bodies():
    sym = XFile(arcs=[(4, 0, 0, 20, 0, 360, {})],
                polys=[(4, [0, -20, 30, 0, 0, 20], {})],
                lines=[(4, 35, -20, 35, 20, {})])
    f = XFile(comps=[XComp("shape.sym", 0, 0, 1, 1, {}, sym)])
    path = routed(f, (-100, 0), (100, 0))
    assert len(path) == 4
    assert abs(path[1][1]) >= 30


def test_blocked_endpoint_fails_without_mutation():
    f = XFile(comps=[device()])
    before = xschem.dump(f)
    with pytest.raises(ValueError, match="blocked"):
        schroute.route(f, SymbolResolver([]), Path("."), (0, 0), (100, 0))
    assert xschem.dump(f) == before


def test_overlapping_device_blocks_pin_escape():
    f = XFile(comps=[device(), device(-60, 0)])
    with pytest.raises(ValueError, match="blocked"):
        schroute.route(f, SymbolResolver([]), Path("."), (-30, 0), (-120, 0))


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_nonfinite_endpoint_rejected(bad):
    with pytest.raises(ValueError, match="finite"):
        schroute.route(XFile(), SymbolResolver([]), Path("."), (bad, 0), (10, 0))


def test_work_limit_fails_cleanly(monkeypatch):
    monkeypatch.setattr(schroute, "_MAX_STATES", 0)
    with pytest.raises(ValueError, match="No safe wire route"):
        schroute.route(XFile(comps=[device()]), SymbolResolver([]), Path("."), (-30, 0), (100, 0))


def test_indexes_agree_with_checking_every_point_and_wire():
    import random
    rnd = random.Random(7)
    coords = [0, 10, 10.000001, 9.9999995, 20, 35.5, -10]
    points = [(rnd.choice(coords), rnd.choice(coords)) for _ in range(60)]
    wires = [((rnd.choice(coords), rnd.choice(coords)), (rnd.choice(coords), rnd.choice(coords))) for _ in range(40)]
    pi, wi = schroute._PointIndex(points), schroute._WireIndex(wires)
    for a in points:
        for b in [(a[0], 40), (40, a[1]), (a[0] + 5, a[1] + 5)]:
            assert pi.any_on(a, b) == any(schroute._on(p, a, b) for p in points)
        assert wi.through(a) == any(schroute._on(a, c, d) for c, d in wires)


def test_a_preview_budget_stops_the_search_but_not_a_plain_route():
    f = XFile(comps=[device()])
    with schroute.time_budget(0):
        assert routed(XFile(), (0, 0), (50, 20)) == [(0, 0), (50, 0), (50, 20)]   # no search needed
        with pytest.raises(schroute.RouteTimeout):
            schroute.route(f, SymbolResolver([]), Path("."), (-100, 0), (100, 0))  # a detour around the body
    assert len(routed(f, (-100, 0), (100, 0))) == 4                                 # outside the budget, as before
