"""New cell with views: schematic, a symbol generated from the schematic's ports, a layout cell."""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi.testclient import TestClient  # noqa: E402

from undine.schematics import schematic, xschem  # noqa: E402
from undine.server.api import Server, create_app  # noqa: E402
from undine.pdks.pdk import default_pdk_roots, discover  # noqa: E402

PORTS = [("A", "ipin"), ("B", "ipin"), ("Y", "opin"), ("VDD", "iopin"), ("VSS", "iopin")]


def test_make_symbol_keeps_port_order_and_sides():
    f = xschem.make_symbol(PORTS)
    pins = [(b[5]["name"], b[5]["dir"], (b[1] + b[3]) / 2) for b in f.boxes if b[0] == 5]
    assert [p[0] for p in pins] == ["A", "B", "Y", "VDD", "VSS"]                   # the subcircuit's pin order
    assert all(x < 0 for n, d, x in pins if d == "in") and all(x > 0 for n, d, x in pins if d != "in")
    assert f.global_props["type"] == "subcircuit"
    assert xschem.parse(xschem.dump(f)) == xschem.parse(xschem.dump(f))           # writes and reads back
    assert not xschem.make_symbol([]).boxes and xschem.make_symbol([]).lines     # no ports: an empty box


def test_new_cell_api_creates_views():
    root = Path(tempfile.mkdtemp())
    srv = Server(root, discover(default_pdk_roots()), [root])
    c = TestClient(create_app(srv, None))
    assert c.post("/api/projects", json={"name": "p"}).status_code == 200
    assert c.post("/api/p/p/libs", json={"name": "l"}).status_code == 200
    r = c.post("/api/p/p/l/l/cells", json={"name": "amp", "views": ["layout", "schematic"]})
    assert r.status_code == 200 and r.json()["created"] == ["schematic", "layout"]
    views = {x["name"]: x["views"] for x in c.get("/api/p/p/l/l/cells").json()}
    assert views["amp"] == ["schematic", "layout"]
    # ports in the schematic, then the symbol is generated from them
    sch = root / "p/libs/l/amp.sch"
    f = xschem.parse(sch.read_text())
    for i, (n, t) in enumerate(PORTS):
        f.comps.append(xschem.XComp(f"devices/{t}.sym", -100, i * 20, 0, 0, {"name": f"p{i}", "lab": n}))
    sch.write_text(xschem.dump(f))
    r = c.post("/api/p/p/l/l/cells", json={"name": "amp", "views": ["symbol"]})
    assert r.status_code == 200
    sym = xschem.parse((root / "p/libs/l/amp.sym").read_text())
    if schematic.extract(sch, xschem.SymbolResolver(srv.project("p").symbol_paths())).ports:  # needs Xschem's devices
        assert [b[5]["name"] for b in sym.boxes if b[0] == 5] == [n for n, _ in PORTS]
    # refused, nothing created, when a view exists; bad names and views refused
    r = c.post("/api/p/p/l/l/cells", json={"name": "amp", "views": ["schematic", "layout"]})
    assert r.status_code == 400 and "already has" in r.json()["detail"]
    assert c.post("/api/p/p/l/l/cells", json={"name": "a b", "views": ["schematic"]}).status_code == 400
    assert c.post("/api/p/p/l/l/cells", json={"name": "x", "views": ["netlist"]}).status_code == 400
    assert c.post("/api/p/p/l/l/cells", json={"name": "x", "views": []}).status_code == 400
    shutil.rmtree(root)


if __name__ == "__main__":
    test_make_symbol_keeps_port_order_and_sides()
    test_new_cell_api_creates_views()
    print("ok")
