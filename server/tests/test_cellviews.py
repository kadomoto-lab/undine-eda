"""The cell overview: the intent on top, then design views, test views (testbenches) and the cells using it."""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402

from undine.store import cellviews  # noqa: E402
from undine.server.api import Server, create_app  # noqa: E402

V = "v {xschem version=3.4.5 file_version=1.2}\nG {}\nK {}\nV {}\nS {}\nE {}\n"


def test_testbenches_and_users_are_told_apart():
    assert cellviews.is_testbench(V + "C {mylib/amp.sym} 0 0 0 0 {name=x1}\nC {devices/vsource.sym} 0 0 0 0 {name=V1}\n")
    assert cellviews.is_testbench(V + "C {amp.sym} 0 0 0 0 {name=x1}\nC {devices/code_shown.sym} 0 0 0 0 {name=s1 value=\".tran 1n 1u\"}\n")
    assert not cellviews.is_testbench(V + "C {amp.sym} 0 0 0 0 {name=x1}\nC {devices/ipin.sym} 0 0 0 0 {name=p1 lab=in}\n"
                                          "C {devices/vsource.sym} 0 0 0 0 {name=V1}\n")          # has ports: a design cell
    assert not cellviews.is_testbench(V + "C {amp.sym} 0 0 0 0 {name=x1}\n")                     # places it, drives nothing


def test_overview_of_a_cell():
    root = Path(tempfile.mkdtemp())
    srv = Server(root, {}, [root])
    c = TestClient(create_app(srv, None))
    assert c.post("/api/projects", json={"name": "p"}).status_code == 200
    assert c.post("/api/p/p/libs", json={"name": "l"}).status_code == 200
    lib = root / "p/libs/l"
    (lib / "amp.sch").write_text(V + "C {devices/ipin.sym} 0 0 0 0 {name=p1 lab=in}\nC {devices/opin.sym} 100 0 0 0 {name=p2 lab=out}\n")
    (lib / "tb_amp.sch").write_text(V + "C {l/amp.sym} 0 0 0 0 {name=x1}\nC {devices/vsource.sym} 0 0 0 0 {name=V1}\n")
    (lib / "top.sch").write_text(V + "C {amp.sym} 0 0 0 0 {name=x1}\nC {devices/ipin.sym} 0 0 0 0 {name=p1 lab=a}\n")
    (lib / "tb_spec.sch").write_text(V + "C {devices/vsource.sym} 0 0 0 0 {name=V1}\n")          # named by a spec only
    e = "/api/p/p/l/l/c/amp/intent/edit"
    assert c.post(e, json={"op": "add_item", "item": {"kind": "goal", "text": "low offset"}}).status_code == 200
    assert c.post(e, json={"op": "add_item", "item": {"kind": "spec", "text": "gain",
                                                       "spec": {"measure": "gain_db", "min": 40, "testbench": "tb_spec"}}}).status_code == 200
    assert c.post(e, json={"op": "add_item", "view": "layout",
                           "item": {"kind": "constraint", "text": "input pair common centroid", "serves": ["g1"]}}).json()["result"] == "c1"
    assert c.post(e, json={"op": "add_item", "view": "test:tb_amp",
                           "item": {"kind": "context", "text": "2 pF load", "serves": ["s1"]}}).status_code == 200
    o = c.get("/api/p/p/l/l/c/amp/overview").json()
    assert [i["id"] for i in o["intent"]["items"]] == ["g1", "s1"]                       # the cell intent on top
    assert o["intent"]["views"] == {"layout": 1, "test:tb_amp": 1}
    assert [(d["view"], d["role"], d["exists"], d["intent"]) for d in o["design"]] == [
        ("schematic", "source", True, 0), ("symbol", "source", False, 0), ("rtl", "source", False, 0), ("layout", "source", False, 1)]
    tests = {t["cell"]: t for t in o["tests"]}
    assert set(tests) == {"tb_amp", "tb_spec"}
    assert tests["tb_amp"]["why"] == ["has test intent", "places amp"] and tests["tb_amp"]["intent"] == 1
    assert tests["tb_spec"]["why"] == ["measures s1"] and tests["tb_spec"]["exists"]
    assert [u["cell"] for u in o["used_in"]] == ["top"]                                  # uses it, not a test
    assert o["derived"][0] == {"view": "netlist", "from": ["schematic"], "available": True, "on_demand": True}
    assert c.get("/api/p/p/l/l/c/nothing/overview").json()["exists"] is False
    shutil.rmtree(root)
