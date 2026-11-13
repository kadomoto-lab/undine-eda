"""Library import accepts explicit view types without replacing existing Xschem views or dirty layout work."""
import sys
from pathlib import Path
from types import SimpleNamespace

import klayout.db as kdb
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine.store import provenance  # noqa: E402
from undine.schematics import xschem
from undine.server.api import Server, create_app  # noqa: E402
from undine.store.changeset import Changesets  # noqa: E402


@pytest.fixture
def api(tmp_path):
    root, imports = tmp_path / "projects", tmp_path / "imports"
    root.mkdir(); imports.mkdir()
    srv = Server(root, {}, [imports], llm_policy="none")
    prj = srv.store.create("p")
    lib = prj.create_library("l")
    return SimpleNamespace(srv=srv, client=TestClient(create_app(srv, None)), prj=prj, lib=lib,
                           root=root, imports=imports, outside=tmp_path / "outside")


def source(api, view, name="imported"):
    model = (xschem.make_symbol([("IN", "ipin"), ("OUT", "opin")]) if view == "symbol"
             else xschem.XFile(version="xschem version=3.4.5 file_version=1.2\n",
                                texts=[xschem.XText("Imported circuit", 10, 20, 0, 0, .3, .3, {})]))
    path = api.imports / f"{name}.{('sym' if view == 'symbol' else 'sch')}"
    path.write_text(xschem.dump(model))
    return path


def layout_source(api, suffix=".oas", name="bundle", cell="top"):
    ly = kdb.Layout()
    ly.dbu = .001
    top = ly.create_cell(cell)
    top.shapes(ly.layer(1, 0)).insert(kdb.Box(0, 0, 1000, 2000))
    path = api.imports / f"{name}{suffix}"
    # KLayout supports OASIS with either common suffix; use explicit options.
    options = kdb.SaveLayoutOptions()
    options.format = "GDS2" if suffix == ".gds" else "OASIS"
    ly.write(str(path), options)
    return path


def import_file(api, path, view=None, project="p", lib="l"):
    body = {"path": str(path)}
    if view is not None:
        body["view"] = view
    return api.client.post(f"/api/p/{project}/l/{lib}/import", json=body)


def snapshot(api, project="p"):
    prj = api.srv.project(project)
    dirs = [prj.path / "history", *(lib.path for lib in prj.libraries().values())]
    return {str(p): p.read_bytes() for directory in dirs if directory.is_dir()
            for p in directory.rglob("*") if p.is_file()}


@pytest.mark.parametrize("view", ["schematic", "symbol"])
def test_import_xschem_view_uses_filename_stem_and_records_creation(api, view):
    path = source(api, view, "gain-stage")
    response = import_file(api, path, view)
    assert response.status_code == 200, response.text
    assert response.json() == {"new_cells": ["gain-stage"], "view": view}
    destination = api.lib.path / path.name
    assert destination.read_bytes() == path.read_bytes()
    row = next(c for c in api.client.get("/api/p/p/l/l/cells").json() if c["name"] == "gain-stage")
    assert row["views"] == [view]
    records = api.srv.oplog("p", "l").read()
    assert len(records) == 1
    assert records[0]["op"] == "import_cell" and records[0]["view"] == view
    assert records[0]["cell"] == "gain-stage" and records[0]["effects"] == [
        {"k": "v", "do": "add", "text": path.read_text()}]


@pytest.mark.parametrize("view", ["schematic", "symbol"])
def test_import_xschem_undo_redo_survives_restart(api, view):
    path = source(api, view)
    assert import_file(api, path, view).status_code == 200
    api.srv = Server(api.root, {}, [api.imports], llm_policy="none")
    api.client = TestClient(create_app(api.srv, None))
    url = f"/api/p/p/l/l/c/imported/{view}/edit"
    undone = api.client.post(url, json={"op": "undo"})
    assert undone.status_code == 200, undone.text
    assert not (api.lib.path / path.name).exists()
    redone = api.client.post(url, json={"op": "redo"})
    assert redone.status_code == 200, redone.text
    assert (api.lib.path / path.name).read_bytes() == path.read_bytes()


@pytest.mark.parametrize("view", ["schematic", "symbol"])
def test_import_existing_view_is_refused_without_replacing_or_logging(api, view):
    path = source(api, view)
    destination = api.lib.path / path.name
    destination.write_text(xschem.dump(xschem.XFile(texts=[xschem.XText("KEEP", 0, 0, 0, 0, .3, .3, {})])))
    before = snapshot(api)
    response = import_file(api, path, view)
    assert response.status_code == 400, response.text
    assert snapshot(api) == before


@pytest.mark.parametrize("first,second", [("symbol", "schematic"), ("schematic", "symbol")])
def test_import_can_add_another_view_to_existing_cell(api, first, second):
    first_path = source(api, first, "same_cell")
    second_path = source(api, second, "same_cell")
    assert import_file(api, first_path, first).status_code == 200
    assert import_file(api, second_path, second).status_code == 200
    rows = api.client.get("/api/p/p/l/l/cells").json()
    assert next(c for c in rows if c["name"] == "same_cell")["views"] == ["schematic", "symbol"]
    assert (api.lib.path / first_path.name).read_bytes() == first_path.read_bytes()


@pytest.mark.parametrize("view", ["schematic", "symbol"])
def test_import_in_proposal_is_isolated_and_replayed_on_apply(api, view):
    path = source(api, view)
    changesets = Changesets(api.srv)
    meta = changesets.create("p", "Import existing view")
    response = import_file(api, path, view, meta["preview"])
    assert response.status_code == 200, response.text
    work = api.srv.project(meta["preview"]).library("l").path / path.name
    assert work.read_bytes() == path.read_bytes()
    assert not (api.lib.path / path.name).exists()
    record = api.srv.oplog(meta["preview"], "l").read()[-1]
    assert record["changeset"] == meta["id"] and record["op"] == "import_cell"
    changesets.apply("p", meta["id"], "user")
    assert xschem.parse((api.lib.path / path.name).read_text()) == xschem.parse(path.read_text())


@pytest.mark.parametrize("suffix", [".gds", ".oas", ".oasis"])
def test_layout_formats_use_internal_cell_names_and_layout_is_default(api, suffix):
    path = layout_source(api, suffix)
    response = import_file(api, path)
    assert response.status_code == 200, response.text
    assert response.json() == {"new_cells": ["top"], "view": "layout"}
    db = api.srv.db("p", "l")
    assert db.cell("top").bbox() == kdb.Box(0, 0, 1000, 2000)
    assert (api.lib.path / "top.oas").exists()


@pytest.mark.parametrize("view", ["schematic", "symbol", "layout"])
def test_readonly_library_and_proposal_baseline_reject_import(api, view):
    path = layout_source(api) if view == "layout" else source(api, view)
    api.prj.meta["libraries"]["locked"] = {"git": "https://example.invalid/library.git", "rev": "main"}
    api.prj.save()
    api.prj.library("locked").path.mkdir(parents=True)
    before = snapshot(api)
    assert import_file(api, path, view, lib="locked").status_code == 403
    assert snapshot(api) == before
    meta = Changesets(api.srv).create("p", "Baseline import forbidden")
    baseline_before = snapshot(api, meta["baseline"])
    assert import_file(api, path, view, meta["baseline"]).status_code == 403
    assert snapshot(api, meta["baseline"]) == baseline_before


@pytest.mark.parametrize("view", ["schematic", "symbol", "layout"])
def test_outside_import_root_and_symlink_escape_are_rejected(api, view):
    inside = layout_source(api) if view == "layout" else source(api, view)
    api.outside.mkdir()
    outside = api.outside / inside.name
    outside.write_bytes(inside.read_bytes())
    alias = api.imports / f"link{inside.suffix}"
    alias.symlink_to(outside)
    before = snapshot(api)
    for path in (outside, alias):
        assert import_file(api, path, view).status_code == 403
        assert snapshot(api) == before


@pytest.mark.parametrize("view,suffix,content", [
    ("symbol", ".sym", "not an Xschem document"),
    ("schematic", ".sch", "v {}\nN 0 0 10 10 {}\n"),
    ("schematic", ".sch", "v {xschem version=3.4.5}\n"),
    ("symbol", ".sch", "v {xschem version=3.4.5 file_version=1.2}\n"),
    ("schematic", ".sym", "v {xschem version=3.4.5 file_version=1.2}\n"),
    ("layout", ".gds", "not a GDS file"),
])
def test_invalid_or_mismatched_file_leaves_library_and_logs_unchanged(api, view, suffix, content):
    path = api.imports / f"bad{suffix}"
    path.write_text(content)
    db = api.srv.db("p", "l")
    before = snapshot(api)
    response = import_file(api, path, view)
    assert response.status_code in (400, 422), response.text
    assert snapshot(api) == before
    assert api.srv.db("p", "l") is db


def test_missing_file_unknown_view_and_invalid_cell_name_are_refused(api):
    before = snapshot(api)
    assert import_file(api, api.imports / "missing.sym", "symbol").status_code == 404
    path = source(api, "symbol", "invalid name")
    assert import_file(api, path, "symbol").status_code == 400
    assert import_file(api, path, "unknown").status_code == 422
    assert snapshot(api) == before


def test_dirty_layout_import_is_refused_without_losing_cached_or_saved_work(api):
    db = api.srv.db("p", "l")
    db.new_cell("existing")
    db.save()
    db._commit("existing", [("ins", db.layout.layer(1, 0), kdb.Box(50, 50, 300, 400))])
    assert db.dirty
    before, shape_sha = snapshot(api), provenance.layout_sha(db.layout, "existing")
    response = import_file(api, layout_source(api))
    assert response.status_code == 400, response.text
    assert "save" in response.json()["detail"].lower()
    assert api.srv.db("p", "l") is db and db.dirty
    assert provenance.layout_sha(db.layout, "existing") == shape_sha
    assert snapshot(api) == before
    assert db.layout.cell("top") is None
