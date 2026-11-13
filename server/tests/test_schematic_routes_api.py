"""The browser route preview, edit history and changeset adoption share one routing engine."""
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine.schematics import schematic, xschem  # noqa: E402
from undine.server.api import create_app  # noqa: E402
import test_changeset as tc  # noqa: E402


@pytest.fixture
def design():
    root, srv, cs = tc.setup()
    lib = srv.project("p").library("l").path
    (lib / "r.sym").write_text(tc.R_SYM + 'B 4 -10 -15 10 15 {}\nL 4 0 -30 0 -15 {}\nL 4 0 15 0 30 {}\n')
    f = xschem.parse((lib / "top.sch").read_text())
    f.comps += [xschem.XComp("r.sym", 160, 0, 0, 0, {"name": "R2", "value": "2k"}),
                xschem.XComp("r.sym", 80, -30, 0, 0, {"name": "R3", "value": "3k"})]
    (lib / "top.sch").write_text(xschem.dump(f))
    try:
        with TestClient(create_app(srv, None)) as client:
            yield srv, cs, client
    finally:
        shutil.rmtree(root)


def endpoint(project="p", action="edit"):
    return f"/api/p/{project}/l/l/c/top/schematic/{action}"


ENDS = {"x": 0, "y": -30, "x2": 160, "y2": -30}


def pin_nets(srv, project="p"):
    ed = srv.editor(project, "l", "top", "schematic")
    return {f"{d.name}:{pin}": net for d in schematic.extract(ed.path, ed.resolver).devices for pin, net in d.nets.items()}


def test_preview_is_read_only_and_commit_matches_it_in_one_undo_step(design):
    srv, _, client = design
    ed = srv.editor("p", "l", "top", "schematic")
    before = ed.path.read_bytes()
    history = srv.oplog("p", "l").read()
    preview = client.post(endpoint(action="route"), json=ENDS)
    assert preview.status_code == 200, preview.text
    pts = preview.json()["points"]
    assert pts[0] == [0, -30] and pts[-1] == [160, -30]
    assert len(pts) > 2  # R3 blocks the direct route
    assert all(a[0] == b[0] or a[1] == b[1] for a, b in zip(pts, pts[1:]))
    assert ed.path.read_bytes() == before and srv.oplog("p", "l").read() == history

    commit = client.post(endpoint(), json={"op": "route_wire", **ENDS})
    assert commit.status_code == 200, commit.text
    wires = commit.json()["view"]["wires"]
    assert {w[6] for w in wires} == {xschem.wire_id(*a, *b) for a, b in zip(pts, pts[1:])}
    assert len(srv.oplog("p", "l").read()) == len(history) + 1
    pins = pin_nets(srv)
    assert pins["R1:P"] == pins["R2:P"]
    assert pins["R1:P"] not in (pins["R1:M"], pins["R2:M"], pins["R3:P"], pins["R3:M"])
    after = ed.path.read_bytes()
    assert client.post(endpoint(), json={"op": "undo"}).status_code == 200
    assert not ed._load().wires
    assert client.post(endpoint(), json={"op": "redo"}).status_code == 200
    assert ed.path.read_bytes() == after


def test_preview_validates_coordinates_and_missing_cells_without_edits(design):
    srv, _, client = design
    before = srv.editor("p", "l", "top", "schematic").path.read_bytes()
    assert client.post(endpoint(action="route"), json={"x": 0}).status_code == 422
    assert client.post(endpoint(action="route"), json={**ENDS, "x": "NaN"}).status_code == 422
    assert client.post(endpoint(action="route").replace("/top/", "/missing/"), json=ENDS).status_code == 404
    # A point inside a body is not a legal endpoint, and failed edits are atomic.
    response = client.post(endpoint(), json={"op": "route_wire", **ENDS, "x2": 80, "y2": -30})
    assert response.status_code == 400, response.text
    assert srv.editor("p", "l", "top", "schematic").path.read_bytes() == before
    assert srv.oplog("p", "l").read() == []


def test_routed_move_stays_in_proposal_until_adopted_and_can_be_reverted(design):
    srv, cs, client = design
    base = srv.editor("p", "l", "top", "schematic").path.read_bytes()
    proposal = cs.create("p", "Route then move", "Keep the same circuit")
    pv = proposal["preview"]
    route = client.post(endpoint(pv), json={"op": "route_wire", **ENDS})
    assert route.status_code == 200, route.text
    move = client.post(endpoint(pv), json={"op": "move", "items": [{"k": "i", "id": "R2"}], "dx": 40, "dy": 70})
    assert move.status_code == 200, move.text
    pins = pin_nets(srv, pv)
    assert pins["R1:P"] == pins["R2:P"] and pins["R1:P"] != pins["R3:P"]
    assert srv.editor("p", "l", "top", "schematic").path.read_bytes() == base
    cs.apply("p", proposal["id"], "user")
    assert pin_nets(srv)["R1:P"] == pin_nets(srv)["R2:P"]
    assert srv.editor("p", "l", "top", "schematic")._load().comps[1].x == 200
    cs.revert("p", proposal["id"], "user")
    restored = srv.editor("p", "l", "top", "schematic")._load()
    assert not restored.wires and restored.comps[1].x == 160 and restored.comps[1].y == 0


def test_move_preview_is_read_only_and_draws_the_exact_committed_route(design):
    srv, _, client = design
    assert client.post(endpoint(), json={"op": "route_wire", **ENDS}).status_code == 200
    ed = srv.editor("p", "l", "top", "schematic")
    before = ed.path.read_bytes()
    history = srv.oplog("p", "l").read()
    original_nets = pin_nets(srv)
    move = {"items": [{"k": "i", "id": "R2"}], "dx": 40, "dy": 70}

    # Multiple mouse positions must leave the saved drawing and Undo stack alone.
    for dy in (20, 40, 70):
        response = client.post(endpoint(action="move-preview"), json={**move, "dy": dy})
        assert response.status_code == 200, response.text
        preview = response.json()
        assert preview["result"] == ["R2"]
        inst = next(i for i in preview["view"]["insts"] if i["id"] == "R2")
        assert (inst["x"], inst["y"]) == (200, dy)
        assert preview["view"]["wires"]
        assert ed.path.read_bytes() == before
        assert srv.oplog("p", "l").read() == history
    assert pin_nets(srv) == original_nets

    commit = client.post(endpoint(), json={"op": "move", **move})
    assert commit.status_code == 200, commit.text
    committed = commit.json()["view"]
    for key in ("lines", "boxes", "polys", "arcs", "texts", "wires", "insts", "terminals"):
        assert preview["view"][key] == committed[key], key
    assert len(srv.oplog("p", "l").read()) == len(history) + 1
    assert client.post(endpoint(), json={"op": "undo"}).status_code == 200
    assert ed.path.read_bytes() == before


def test_move_preview_rejects_invalid_requests_without_changing_the_design(design):
    srv, _, client = design
    ed = srv.editor("p", "l", "top", "schematic")
    before = ed.path.read_bytes()
    body = {"items": [{"k": "i", "id": "R1"}], "dx": 10, "dy": 20}
    for bad in ({"items": []}, {**body, "dx": "NaN"}, {**body, "dy": "Infinity"},
                {**body, "items": [{"k": "unknown", "id": "R1"}]}):
        assert client.post(endpoint(action="move-preview"), json=bad).status_code == 422
    assert client.post(endpoint(action="move-preview").replace("/top/", "/missing/"), json=body).status_code == 404
    assert ed.path.read_bytes() == before
    assert srv.oplog("p", "l").read() == []


def test_move_preview_in_proposal_does_not_modify_either_version(design):
    srv, cs, client = design
    proposal = cs.create("p", "Preview a drag")
    pv = proposal["preview"]
    assert client.post(endpoint(pv), json={"op": "route_wire", **ENDS}).status_code == 200
    real = srv.editor("p", "l", "top", "schematic").path
    work = srv.editor(pv, "l", "top", "schematic").path
    before = real.read_bytes(), work.read_bytes(), srv.oplog(pv, "l").read()
    response = client.post(endpoint(pv, action="move-preview"),
                           json={"items": [{"k": "i", "id": "R2"}], "dx": 40, "dy": 70})
    assert response.status_code == 200, response.text
    assert (real.read_bytes(), work.read_bytes(), srv.oplog(pv, "l").read()) == before


@pytest.mark.parametrize("selection", ["wire", "devices"])
def test_wire_label_follows_in_preview_commit_and_undo_inside_proposal(design, selection):
    srv, cs, client = design
    original = srv.editor("p", "l", "top", "schematic")
    (original.path.parent / "label.sym").write_text(
        'v {}\nK {type=label\ntemplate="name=p1 lab=signal"}\n'
        'B 5 -2 -2 2 2 {name=P}\nT {@lab} 0 -10 0 0 0.3 0.3 {}\n')
    original.add_wire(0, 30, 160, 30)
    original.add_instance("label.sym", 60, 30, props={"name": "marker", "lab": "signal"})
    real_before = original.path.read_bytes()
    proposal = cs.create("p", "Move a labelled connection")
    pv = proposal["preview"]
    work = srv.editor(pv, "l", "top", "schematic")
    before = work.path.read_bytes()
    history = srv.oplog(pv, "l").read()
    initial_nets = pin_nets(srv, pv)
    items = ([{"k": "w", "id": xschem.wire_id(0, 30, 160, 30)}] if selection == "wire"
             else [{"k": "i", "id": "R1"}, {"k": "i", "id": "R2"}])
    move = {"items": items, "dx": 20, "dy": 70}

    response = client.post(endpoint(pv, "move-preview"), json=move)
    assert response.status_code == 200, response.text
    preview = response.json()["view"]
    label = next(i for i in preview["insts"] if i["id"] == "marker")
    point = label["x"], label["y"]
    if selection == "wire":
        assert point == (80, 100)
    else:
        # Device clearance may reroute the conductor beyond the translation.
        assert point != (60, 30)
    assert any(schematic._on_segment(*point, *wire[:4]) for wire in preview["wires"])
    assert label["props"]["lab"] == "signal"
    assert any(t["s"] == "signal" for t in preview["texts"])
    assert work.path.read_bytes() == before and original.path.read_bytes() == real_before
    assert srv.oplog(pv, "l").read() == history

    response = client.post(endpoint(pv), json={"op": "move", **move})
    assert response.status_code == 200, response.text
    committed = response.json()["view"]
    for key in ("texts", "wires", "insts", "terminals"):
        assert preview[key] == committed[key], key
    assert pin_nets(srv, pv) == initial_nets
    assert len(srv.oplog(pv, "l").read()) == len(history) + 1
    assert client.post(endpoint(pv), json={"op": "undo"}).status_code == 200
    assert work.path.read_bytes() == before and original.path.read_bytes() == real_before
