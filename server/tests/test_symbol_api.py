"""Symbol creation/editing and interface verification through the public HTTP API."""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undine.schematics import schematic, xschem  # noqa: E402
from undine.server.api import Server, create_app  # noqa: E402
from undine.store.changeset import Changesets  # noqa: E402


@pytest.fixture
def api(tmp_path):
    srv = Server(tmp_path, {}, [tmp_path], llm_policy="none")
    prj = srv.store.create("p")
    lib = prj.create_library("l")
    ports = [("OUT", "opin"), ("IN", "ipin"), ("VSS", "iopin")]
    model = xschem.XFile(comps=[
        xschem.XComp(f"{kind}.sym", i * 40, 0, 0, 0, {"name": f"P{i + 1}", "lab": name},
                    xschem.XFile(global_props={"type": kind},
                                 boxes=[(5, -2.5, -2.5, 2.5, 2.5, {"name": "p"})]))
        for i, (name, kind) in enumerate(ports)])
    (lib.path / "amp.sch").write_text(xschem.dump(model))
    client = TestClient(create_app(srv, None))
    return SimpleNamespace(srv=srv, client=client, prj=prj, lib=lib, ports=ports, root=tmp_path)


def _post(client, url, body):
    response = client.post(url, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _create(api, cell="amp", project="p"):
    return _post(api.client, f"/api/p/{project}/l/l/cells", {"name": cell, "views": ["symbol"]})


def _pins(api, project="p", cell="amp"):
    response = api.client.get(f"/api/p/{project}/l/l/c/{cell}/symbol")
    assert response.status_code == 200, response.text
    return [p for p in response.json()["objects"] if p["kind"] == "pin"]


def _check(api, project="p", cell="amp"):
    job = _post(api.client, f"/api/p/{project}/symbol-check", {"lib": "l", "cell": cell})
    deadline = time.monotonic() + 5
    while job["state"] in ("queued", "running") and time.monotonic() < deadline:
        time.sleep(0.01)
        job = api.client.get(f"/api/p/{project}/jobs/{job['id']}").json()
    assert job["state"] == "done", job
    return job


def test_new_symbol_inherits_schematic_interface_in_netlist_order(api):
    assert _create(api) == {"name": "amp", "created": ["symbol"]}
    pins = _pins(api)
    assert [(p["name"], p["direction"], p["order"]) for p in pins] == [
        ("OUT", "out", 1), ("IN", "in", 2), ("VSS", "inout", 3)]
    assert pins[1]["x"] < 0 and pins[0]["x"] > 0
    resolver = xschem.SymbolResolver(api.prj.symbol_paths())
    assert schematic.spice(api.lib.path / "amp.sch", resolver).ports == [p["name"] for p in pins]
    before = (api.lib.path / "amp.sym").read_text()
    duplicate = api.client.post("/api/p/p/l/l/cells", json={"name": "amp", "views": ["symbol", "layout"]})
    assert duplicate.status_code == 400 and (api.lib.path / "amp.sym").read_text() == before
    assert not (api.lib.path / "amp.oas").exists()


def test_standalone_symbol_creation_and_undo_redo(api):
    _create(api, "standalone")
    assert _pins(api, cell="standalone") == []
    path = api.lib.path / "standalone.sym"
    initial = path.read_text()
    model = xschem.parse(initial)
    assert model.global_props["type"] == "subcircuit"
    assert model.global_props["format"] == "@name @pinlist @symname"
    edit = "/api/p/p/l/l/c/standalone/symbol/edit"
    created = _post(api.client, edit, {"op": "symbol_add", "kind": "pin", "name": "A", "direction": "in", "x": -60, "y": 0})
    assert created["view"]["undo"] == 2
    _post(api.client, edit, {"op": "undo"})
    assert path.read_text() == initial
    removed = _post(api.client, edit, {"op": "undo"})
    assert not path.exists() and removed["view"]["exists"] is False
    _post(api.client, edit, {"op": "redo"})
    assert path.read_text() == initial
    _post(api.client, edit, {"op": "redo"})
    assert _pins(api, cell="standalone")[0]["name"] == "A"


def test_readonly_and_baseline_symbol_writes_are_rejected(api):
    _create(api)
    meta = Changesets(api.srv).create("p", "Readonly baseline")
    baseline = meta["baseline"]
    assert api.client.post(f"/api/p/{baseline}/l/l/cells", json={"name": "new", "views": ["symbol"]}).status_code == 403
    assert api.client.post(f"/api/p/{baseline}/l/l/c/amp/symbol/edit", json={"op": "undo"}).status_code == 403
    # Dependency libraries use the same readonly contract without requiring network access.
    api.prj.meta["libraries"]["locked"] = {"git": "https://example.invalid/library.git", "rev": "main"}
    api.prj.save()
    locked = api.prj.library("locked")
    locked.path.mkdir(parents=True)
    (locked.path / "amp.sym").write_text((api.lib.path / "amp.sym").read_text())
    assert api.client.post("/api/p/p/l/locked/cells", json={"name": "new", "views": ["symbol"]}).status_code == 403
    assert api.client.post("/api/p/p/l/locked/c/amp/symbol/edit", json={"op": "undo"}).status_code == 403
    assert not (locked.path / "new.sym").exists()


def test_proposal_symbol_creation_and_edits_are_isolated_apply_and_revert(api):
    _create(api)
    original = (api.lib.path / "amp.sym").read_text()
    changesets = Changesets(api.srv)
    meta = changesets.create("p", "Edit symbol")
    preview = meta["preview"]
    _create(api, "new_symbol", preview)
    pins = _pins(api, preview)
    edit = f"/api/p/{preview}/l/l/c/amp/symbol/edit"
    _post(api.client, edit, {"op": "symbol_update", "id": pins[0]["id"], "name": "OUTPUT"})
    _post(api.client, edit, {"op": "symbol_add", "kind": "rectangle", "x": -10, "y": -10, "x2": 10, "y2": 10})
    assert (api.lib.path / "amp.sym").read_text() == original and not (api.lib.path / "new_symbol.sym").exists()
    history = api.srv.oplog(preview, "l").read(limit=100)
    assert [r["op"] for r in history] == ["create_cell", "symbol_update", "symbol_add"]
    assert all(r["changeset"] == meta["id"] and r["view"] == "symbol" for r in history)
    job = _check(api, preview)
    assert job["summary"]["match"] is False
    assert not (api.prj.path / "runs" / job["id"]).exists()
    changesets.apply("p", meta["id"])
    assert _pins(api)[0]["name"] == "OUTPUT" and (api.lib.path / "new_symbol.sym").is_file()
    changesets.revert("p", meta["id"])
    assert (api.lib.path / "amp.sym").read_text() == original


def test_revert_keeps_a_symbol_the_changeset_made_even_when_edited_since(api):
    # a revert keeps the files a changeset added (docs/design/changeset-branches.md): a symbol it made, a member of
    # its symbol transaction, is not taken back, so a later edit of it in the project is no conflict
    changesets = Changesets(api.srv)
    meta = changesets.create("p", "Make a symbol")
    preview = meta["preview"]
    _create(api, project=preview)
    pin = _pins(api, preview)[0]
    _post(api.client, f"/api/p/{preview}/l/l/c/amp/symbol/edit", {"op": "symbol_update", "id": pin["id"], "name": "Q"})
    assert any(r.get("symbol_group") for r in api.srv.oplog(preview, "l").read(limit=100))
    changesets.apply("p", meta["id"])
    pin = _pins(api)[0]
    assert pin["name"] == "Q"
    _post(api.client, "/api/p/p/l/l/c/amp/symbol/edit", {"op": "symbol_update", "id": pin["id"], "name": "R"})
    out = changesets.revert("p", meta["id"])
    assert out["status"] == "reverted" and "libs/l/amp.sym" in out["reverted"]["kept"]
    assert _pins(api)[0]["name"] == "R"


def test_active_agent_locks_symbol_mutations(api):
    _create(api)
    meta = Changesets(api.srv).create("p", "AI editing")
    preview = meta["preview"]
    api.srv.leases.acquire(meta["id"], "turn:test", "turn")
    body = {"op": "symbol_add", "kind": "pin", "name": "LOCKED", "x": 0, "y": 40}
    assert api.client.post(f"/api/p/{preview}/l/l/c/amp/symbol/edit", json=body).status_code == 409
    assert api.client.post(f"/api/p/{preview}/l/l/cells", json={"name": "new", "views": ["symbol"]}).status_code == 409
    api.srv.leases.release(meta["id"], "turn:test")
    assert [p["name"] for p in _pins(api, preview)] == ["OUT", "IN", "VSS"]


def test_check_http_history_result_and_freshness_survive_restart(api):
    _create(api)
    job = _check(api)
    assert job["kind"] == "symbol-check" and job["summary"]["match"]
    result = api.client.get(f"/api/p/p/jobs/{job['id']}/symbol-check")
    assert result.status_code == 200 and result.json()["match"]
    assert api.client.get("/api/p/p/jobs").json()[0]["id"] == job["id"]
    fresh = f"/api/p/p/jobs/{job['id']}/freshness"
    assert api.client.get(fresh).json() == {"fresh": True, "changed": []}
    edit = "/api/p/p/l/l/c/amp/symbol/edit"
    _post(api.client, edit, {"op": "symbol_update", "id": _pins(api)[0]["id"], "direction": "in"})
    assert api.client.get(fresh).json() == {"fresh": False, "changed": ["symbol interface of l/amp changed"]}
    # The saved result remains a pass on the old inputs; freshness distinguishes it from the current design.
    assert api.client.get(f"/api/p/p/jobs/{job['id']}/symbol-check").json()["match"]
    reloaded = TestClient(create_app(Server(api.root, {}, [api.root], llm_policy="none"), None))
    assert reloaded.get(f"/api/p/p/jobs/{job['id']}").json()["state"] == "done"
    assert reloaded.get(fresh).json()["fresh"] is False
    _post(api.client, edit, {"op": "undo"})
    assert reloaded.get(fresh).json()["fresh"]
    mismatch = _check(api, cell="missing")
    assert mismatch["summary"]["total"] == 2 and mismatch["summary"]["match"] is False
