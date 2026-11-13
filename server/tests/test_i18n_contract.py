"""UI localization adds machine-readable data without changing script/MCP-facing diagnostics or design values."""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import klayout.db as kdb  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.exceptions import HTTPException  # noqa: E402

from undine.server import live  # noqa: E402
from undine.server.api import create_app  # noqa: E402
import test_changeset as tc  # noqa: E402


@pytest.fixture
def design():
    root, srv, cs = tc.setup()
    app = create_app(srv, None)
    try:
        with TestClient(app) as client:
            yield srv, cs, app, client
    finally:
        shutil.rmtree(root)


@pytest.mark.parametrize("error_type,status,code", [
    (ValueError, 400, "invalid_request"),
    (KeyError, 404, "not_found"),
    (PermissionError, 403, "forbidden"),
    (live.WriteLocked, 409, "busy"),
])
def test_error_codes_keep_original_http_status_and_diagnostic(design, monkeypatch, error_type, status, code):
    srv, _, _, client = design
    diagnostic = "OTA/R_load: value=7.5k; server detail — 回路名は変更しない"

    def fail_project(name):
        raise error_type(diagnostic)

    monkeypatch.setattr(srv, "project", fail_project)
    for language in ("en", "ja"):
        response = client.get("/api/p/p", headers={"Accept-Language": language})
        assert response.status_code == status
        assert response.json() == {"detail": diagnostic, "code": code, "params": {}}


def test_http_handler_preserves_structured_detail_and_headers(design):
    _, _, app, client = design
    detail = {"tool": "netgen", "output": "Mismatch on net Vout\nW=1.5u L=0.15u", "cells": ["OTA", "R_load"]}

    @app.get("/contract-upstream-error")
    def upstream_error():
        raise HTTPException(502, detail=detail, headers={"Retry-After": "12"})

    response = client.get("/contract-upstream-error")
    assert response.status_code == 502
    assert response.headers["Retry-After"] == "12"
    assert response.json() == {"detail": detail, "code": "upstream_error", "params": {}}


def test_validation_detail_remains_the_fastapi_error_array(design):
    _, _, _, client = design
    body = {"reason": "Keep OTA/R_load at 7.5k"}  # the required title is missing
    response = client.post("/api/p/p/changesets", json=body)
    assert response.status_code == 422
    error = response.json()
    assert error["code"] == "validation_error" and error["params"] == {}
    assert error["detail"] == [{"type": "missing", "loc": ["body", "title"], "msg": "Field required", "input": body}]


def test_timeline_keeps_design_data_and_legacy_effect_text(design):
    srv, cs, _, client = design
    meta = cs.create("p", "OTA values", "レビュー用", "agent:contract")
    preview, cid = meta["preview"], meta["id"]
    ed = srv.editor(preview, "l", "top", "schematic")
    db = srv.db(preview, "l")
    with live.operation(opid="contract-op"):
        ed.set_props("i", "R1", {"value": "7.5k"})
        with db.recording("create", {}, "agent:contract", "test geometry", cid):
            db._commit("top", [("ins", db.layout.layer(68, 20), kdb.Box(-10, 20, 123, 456))])

    response = client.get(f"/api/p/p/changesets/{cid}/timeline")
    assert response.status_code == 200
    timeline = response.json()
    assert len(timeline["ops"]) == 1
    op = timeline["ops"][0]
    assert op["opid"] == "contract-op" and op["kind"] == "edit" and op["in_effect"]
    assert op["cells"] == ["l/top (schematic)", "l/top (layout)"]
    assert len(op["effect_data"]) == len(op["effects"]) == 2
    sch, layout = op["effect_data"]
    assert (sch["lib"], sch["cell"], sch["view"]) == ("l", "top", "schematic")
    assert sch["effect"]["id"] == sch["effect"]["id_after"] == "R1"
    assert sch["effect"]["changes"]["props.value"] == ["1k", "7.5k"]
    assert (layout["lib"], layout["cell"], layout["view"]) == ("l", "top", "layout")
    assert layout["effect"]["cell"] == "top" and layout["effect"]["layer"] == "68/20"
    assert layout["effect"]["s"] == "(-10,20;123,456)"
    # Existing consumers still receive their readable effects and unlocalized electrical values.
    assert all(isinstance(effect, str) and effect for effect in op["effects"])
    assert "R1" in op["effects"][0] and "7.5k" in op["effects"][0]
    assert "68/20" in op["effects"][1]
    assert tc.value(srv) == "1k"  # adding display metadata has not adopted the proposal

    assert ed.undo()
    after_undo = client.get(f"/api/p/p/changesets/{cid}/timeline").json()["ops"]
    assert after_undo[0]["effect_data"] == op["effect_data"]
    assert after_undo[0]["effects"] == op["effects"]
    assert after_undo[-1]["kind"] == "undo"
    assert after_undo[-1]["effect_data"] == after_undo[-1]["effects"] == []
    assert after_undo[-1]["of"]["cell"] == "top"
