"""Clipboard endpoints keep reads, proposal scope and atomic edits distinct."""
import shutil
import sys
from pathlib import Path

import pytest
import klayout.db as kdb
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine.server.api import create_app  # noqa: E402
import test_changeset as tc  # noqa: E402


@pytest.fixture
def design():
    root, srv, cs = tc.setup()
    try:
        with TestClient(create_app(srv, None)) as client:
            yield srv, cs, client
    finally:
        shutil.rmtree(root)


def sch(project="p", cell="top"):
    return f"/api/p/{project}/l/l/c/{cell}/schematic"


def body(snapshot, **extra):
    return {"source": {"project": "p", "lib": "l"}, "payload": snapshot["payload"], "dx": 100, "dy": 0, **extra}


def test_copy_does_not_write_and_paste_records_actor_and_one_undo(design):
    srv, _, client = design
    ed = srv.editor("p", "l", "top", "schematic")
    before = ed.path.read_bytes()
    response = client.post(sch() + "/copy", json={"items": [{"k": "i", "id": "R1"}]})
    assert response.status_code == 200, response.text
    snapshot = response.json()
    assert snapshot["preview"]["insts"][0]["id"] == "R1"
    assert ed.path.read_bytes() == before and srv.oplog("p", "l").read() == []
    response = client.post(sch() + "/paste", json=body(snapshot), headers={"X-Undine-Actor": "user:clipboard"})
    assert response.status_code == 200, response.text
    assert response.json()["result"] == ["R2"]
    history = srv.oplog("p", "l").read()
    assert len(history) == 1 and history[0]["op"] == "paste" and history[0]["actor"] == "user:clipboard"
    assert client.post(sch() + "/edit", json={"op": "undo"}).status_code == 200
    assert [c.props["name"] for c in ed._load().comps] == ["R1"]


def test_paste_into_proposal_preserves_real_design_and_tags_history(design):
    srv, cs, client = design
    snapshot = client.post(sch() + "/copy", json={"items": [{"k": "i", "id": "R1"}]}).json()
    original = srv.editor("p", "l", "top", "schematic").path.read_bytes()
    proposal = cs.create("p", "Clipboard test")
    response = client.post(sch(proposal["preview"]) + "/paste", json=body(snapshot))
    assert response.status_code == 200, response.text
    history = srv.oplog(proposal["preview"], "l").read()
    assert history[-1]["changeset"] == proposal["id"]
    assert srv.editor("p", "l", "top", "schematic").path.read_bytes() == original
    baseline = proposal["preview"] + "~base"
    assert client.post(sch(baseline) + "/paste", json=body(snapshot)).status_code == 403


def test_wrong_scope_or_invalid_payload_never_changes_schematic(design):
    srv, _, client = design
    ed = srv.editor("p", "l", "top", "schematic")
    snapshot = client.post(sch() + "/copy", json={"items": [{"k": "i", "id": "R1"}]}).json()
    before = ed.path.read_bytes()
    for extra in [{"source": {"project": "other", "lib": "l"}}, {"source": {"project": "p", "lib": "other"}},
                  {"payload": {"kind": "undine-layout", "version": 1}}, {"dx": "NaN"}]:
        response = client.post(sch() + "/paste", json=body(snapshot, **extra))
        assert response.status_code in (400, 422), response.text
    assert client.post(sch(cell="missing") + "/paste", json=body(snapshot)).status_code == 404
    assert ed.path.read_bytes() == before and srv.oplog("p", "l").read() == []


def test_layout_snapshot_and_paste_route_through_one_logged_edit(design):
    srv, cs, client = design
    db = srv.db("p", "l")
    layer = db.layout.layer(68, 20)
    item = db._commit("top", [("ins", layer, kdb.Box(0, 0, 100, 100))])[0]
    base = "/api/p/p/l/l/layout/top"
    history = srv.oplog("p", "l").read()
    version = db.version
    response = client.post(base + "/copy", json={"refs": [item["ref"]]})
    assert response.status_code == 200, response.text
    snapshot = response.json()
    assert snapshot["preview"][0]["kind"] == "shape"
    assert db.version == version and srv.oplog("p", "l").read() == history
    for extra in [{"source": {"project": "other", "lib": "l"}}, {"payload": {}}, {"dy": "Infinity"}]:
        assert client.post(base + "/paste", json=body(snapshot, **extra)).status_code in (400, 422)
    assert db.version == version and srv.oplog("p", "l").read() == history
    response = client.post(base + "/paste", json=body(snapshot))
    assert response.status_code == 200, response.text
    assert response.json()["version"] == version + 1
    assert len(response.json()["result"]) == 1
    assert srv.oplog("p", "l").read()[-1]["op"] == "paste"
    assert client.post("/api/p/p/l/l/undo", json={}).status_code == 200
    assert len(list(db.cell("top").shapes(layer).each())) == 1


def test_readonly_source_can_copy_but_cannot_paste(design, monkeypatch):
    srv, _, client = design
    library = srv.project("p").library("l")
    # Use the same resolved library for every request, with its read-only flag set.
    project = srv.project("p")
    monkeypatch.setattr(project, "library", lambda _: library)
    monkeypatch.setattr(srv, "project", lambda _: project)
    library.readonly = True
    response = client.post(sch() + "/copy", json={"items": [{"k": "i", "id": "R1"}]})
    assert response.status_code == 200, response.text
    assert client.post(sch() + "/paste", json=body(response.json())).status_code == 403
