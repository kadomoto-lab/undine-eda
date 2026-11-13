"""Job provenance: content hashes of design views, what changed since a run, reuse of a run on the same inputs."""
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import klayout.db as kdb  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from undine.store import provenance  # noqa: E402
from undine.checks import verify
from undine.schematics import xschem
from undine.server.api import Server, create_app  # noqa: E402
from undine.pdks.pdk import default_pdk_roots, discover  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


def _layout(order=1):
    ly = kdb.Layout()
    ly.dbu = 0.001
    top, sub = ly.create_cell("top"), ly.create_cell("sub")
    m1, m2 = ly.layer(68, 20), ly.layer(69, 20)
    boxes = [kdb.Box(0, 0, 100, 100), kdb.Box(200, 0, 300, 50)][::order]
    for b in boxes:
        sub.shapes(m1).insert(b)
    top.shapes(m2).insert(kdb.Box(-50, -50, 400, 20))
    top.shapes(m1).insert(kdb.Text("A", kdb.Trans(10, 10)))
    top.insert(kdb.CellInstArray(sub.cell_index(), kdb.Trans(kdb.Trans.R90, 1000, 0)))
    ly.create_cell("unrelated").shapes(m1).insert(kdb.Box(0, 0, 5, 5))
    return ly


def test_layout_sha_is_about_content():
    a = provenance.layout_sha(_layout(), "top")
    assert a == provenance.layout_sha(_layout(order=-1), "top")          # shape order does not count
    d = Path(tempfile.mkdtemp())
    ly = _layout()
    ly.write(str(d / "x.oas"))
    back = kdb.Layout()
    back.read(str(d / "x.oas"))
    assert provenance.layout_sha(back, "top") == a                         # a save and reload keep it
    ly.cell("unrelated").shapes(ly.layer(68, 20)).insert(kdb.Box(9, 9, 20, 20))
    assert provenance.layout_sha(ly, "top") == a                           # cells outside the hierarchy do not count
    ly.cell("sub").shapes(ly.layer(68, 20)).insert(kdb.Box(0, 500, 10, 510))
    assert provenance.layout_sha(ly, "top") != a                           # a sub-cell does
    ly2 = _layout()
    next(ly2.cell("top").each_inst()).trans = kdb.Trans(kdb.Trans.R0, 1000, 0)
    assert provenance.layout_sha(ly2, "top") != a                          # and the placement of an instance
    shutil.rmtree(d)


def test_diff_in_words():
    old = {"design": [{"view": "layout", "lib": "l", "cell": "c", "sha": "1"},
                      {"view": "netlist", "lib": "l", "cell": "c", "top": "c", "mode": "lvs", "flat": False, "sha": "2"}],
           "pdk": "p@1", "deck": {"kind": "drc", "name": "d.drc", "sha": "3"}, "tools": {"klayout": "KLayout 0.30.12"},
           "options": {}}
    assert provenance.diff(old, json.loads(json.dumps(old))) == []
    new = json.loads(json.dumps(old))
    new["design"][0]["sha"] = "9"
    new["design"][1].update(flat=True, sha="8")
    new["tools"]["klayout"] = "KLayout 0.30.13"
    new["options"] = {"run_mode": "flat"}
    assert provenance.diff(old, new) == ["layout of l/c changed", "schematic netlist of l/c: flat False → True",
                                         "klayout: KLayout 0.30.12 → KLayout 0.30.13", "option run_mode: default → flat"]
    assert provenance.key(old) != provenance.key(new)


def _inverter_views():
    spec = importlib.util.spec_from_file_location("make_inverter", REPO / "examples" / "make_inverter.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return {"inv.sch": m.inv(), "inv.sym": m.inv_sym()}


def _fake_job(prj_path: Path, jid: str, run) -> None:
    """A finished job with the inputs of `run`, without running the tool."""
    d = prj_path / "runs" / jid
    d.mkdir(parents=True)
    (d / "status.json").write_text(json.dumps({"id": jid, "kind": run.kind, "tool": run.tool, "lib": run.lib, "cell": run.cell,
                                               "deck": run.deck, "state": "done", "summary": {"match": True},
                                               **provenance.meta(run.inputs)}))


def test_freshness_follows_edits():
    pdks = discover(default_pdk_roots())
    pdk = next((p for p in pdks.values() if p.family == "sky130" and verify.decks(p)["lvs"]), None)
    if pdk is None:                                                         # needs a PDK with KLayout decks
        return
    root = Path(tempfile.mkdtemp())
    srv = Server(root, pdks, [root])
    c = TestClient(create_app(srv, None))
    assert c.post("/api/projects", json={"name": "p", "pdk": pdk.id}).status_code == 200
    assert c.post("/api/p/p/libs", json={"name": "l"}).status_code == 200
    lib = root / "p/libs/l"
    for name, f in _inverter_views().items():
        (lib / name).write_text(xschem.dump(f))
    assert c.post("/api/p/p/l/l/cells", json={"name": "inv", "views": ["layout"]}).status_code == 200
    met1 = next(x["id"] for x in c.get("/api/p/p/l/l/layout/inv/layers").json() if x["source"] == "68/20")
    c.post("/api/p/p/l/l/layout/inv/create", json={"kind": "box", "layer": met1, "pts": [0, 0, 1, 1]})

    run = srv.prepare_lvs("p", "l", "inv")
    _fake_job(root / "p", "20260101-000000-aaaa", run)
    fresh = lambda: c.get("/api/p/p/jobs/20260101-000000-aaaa/freshness").json()   # noqa: E731
    assert fresh() == {"fresh": True, "changed": []}
    assert srv.reusable("p", srv.prepare_lvs("p", "l", "inv").inputs).id == "20260101-000000-aaaa"

    edit = "/api/p/p/l/l/c/inv/schematic/edit"
    c.post(edit, json={"op": "add_text", "text": "a note", "x": 300, "y": 300})
    assert fresh()["fresh"] is True                                         # a comment does not change the netlist
    c.post(edit, json={"op": "set_props", "kind": "i", "id": "M2", "props": {"W": "3"}})
    assert fresh() == {"fresh": False, "changed": ["schematic netlist of l/inv changed"]}
    assert srv.reusable("p", srv.prepare_lvs("p", "l", "inv").inputs) is None
    c.post(edit, json={"op": "undo"})
    assert fresh()["fresh"] is True                                         # back to what the job ran on

    c.post("/api/p/p/l/l/layout/inv/create", json={"kind": "box", "layer": met1, "pts": [5, 5, 6, 6]})
    assert fresh() == {"fresh": False, "changed": ["layout of l/inv changed"]}
    checks = {x["name"]: x for x in c.get("/api/p/p/l/l/signoff/inv").json()["checks"]}
    lvs = checks["KLayout LVS"]
    assert lvs["job"] == "20260101-000000-aaaa" and lvs["fresh"] is False and lvs["changed"] == ["layout of l/inv changed"]
    assert all("job" not in x for n, x in checks.items() if n != "KLayout LVS" and "error" not in x)   # not run yet
    c.post("/api/p/p/l/l/undo", json={})
    assert c.get("/api/p/p/l/l/signoff/inv").json()["checks"][[*checks].index("KLayout LVS")]["fresh"] is True
    shutil.rmtree(root)


if __name__ == "__main__":
    test_layout_sha_is_about_content()
    test_diff_in_words()
    test_freshness_follows_edits()
    print("ok")
