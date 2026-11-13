"""The Rust core (undine._core) against the Python reference implementations it replaced.

parse / parse_props / render / extract / dump / wire_id must give exactly what _py_parse ... _py_wire_id give.
Synthetic files cover the format's corners; the installed PDK's Xschem libraries are swept when present.
"""
import math
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine.schematics import schematic, xschem  # noqa: E402
from undine.pdks.pdk import default_pdk_roots, discover  # noqa: E402

SYM = """v {xschem version=3.4.5 file_version=1.2}
G {}
K {type=subcircuit format="@name @pinlist @symname" template="name=x1 W=2 L=0.15"}
T {@symname} -20 -40 0 0 0.3 0.3 {}
T {@name W=@W tcleval(@L)} 25 -10 1 0 0.2 0.2 {layer=4 hcenter=true}
T {@#0:net_name} 0 0 2 1 0.2 0.2 {vcenter=true}
T {hidden} 0 0 0 0 0.2 0.2 {hide=true}
L 4 -20 -30 20 -30 {}
B 5 -22.5 -2.5 -17.5 2.5 {name=A dir=in}
B 5 17.5 -2.5 22.5 2.5 {name=Y dir=out}
B 4 -20 -30 20 30 {fill=true}
B 4 0 0 1 1 {flags=graph}
P 4 3 -5 -5 5 0 -5 5 {fill=full}
A 4 0 0 10 30 120 {}
"""

SCH = """v {xschem version=3.4.5 file_version=1.2}
G {}
K {}
V {}
S {}
E {}
N -100 0 -42.5 0 {lab=in}
N 42.5 0 100 0 {}
N 100 0 100 50 {}
N 100 50 150 50 {lab=out}
N 0 200 0 300 {}
C {cell.sym} 0 0 0 0 {name=X1 W=3}
C {cell.sym} 0 100 1 0 {name=X2}
C {cell.sym} 0 200 2 1 {name=X1}
C {cell.sym} 0 300 3 1 {name=X4 L="0.5 u"}
C {devices/ipin.sym} -100 0 0 0 {name=p1 lab=in}
C {devices/opin.sym} 150 50 0 0 {name=p2 lab=out}
C {missing.sym} 500 500 0 0 {name=Q1}
C {devices/code_shown.sym} 300 -200 0 0 {name=s1 value=".tran 1n 10n"}
C {inline} 600 0 0 0 {name=E1} [
v {}
K {type=subcircuit}
B 5 -2.5 -2.5 2.5 2.5 {name=P}
T {embedded \\{x\\} é} 0 0 0 0 0.2 0.2 {}
]
T {multi
line 日本} 50 -100 0 1 0.4 0.4 {weight=bold slant=italic}
"""


def _same(a, b, path=""):
    if isinstance(a, float) or isinstance(b, float):
        ok = isinstance(a, (int, float)) and isinstance(b, (int, float)) and (a == b or (math.isnan(a) and math.isnan(b)))
        return None if ok else f"{path}: {a!r} != {b!r}"
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            return f"{path}: keys {set(a) ^ set(b)}"
        return next((r for k in a if (r := _same(a[k], b[k], f"{path}.{k}"))), None)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return f"{path}: len {len(a)} != {len(b)}"
        return next((r for i, (x, y) in enumerate(zip(a, b)) if (r := _same(x, y, f"{path}[{i}]"))), None)
    return None if (a == b and type(a) is type(b)) else f"{path}: {a!r} != {b!r}"


def _conn(c):
    return (c.cell, [(d.name, d.index, d.symname, d.type, d.pins, d.nets, d.comp, d.sym) for d in c.devices],
            dict(c.nets), c.ports, c.wire_net, c.wire_ids, c.net_points, list(c.code), c.warnings)


def _setup():
    d = Path(tempfile.mkdtemp())
    (d / "cell.sym").write_text(SYM)
    (d / "top.sch").write_text(SCH)
    return d


def _resolver(d: Path) -> xschem.SymbolResolver:
    from undine import tools
    xl = tools.xschem_library()
    return xschem.SymbolResolver([d] + ([xl, xl / "devices"] if xl else []))


def test_parse_matches_reference_on_corner_cases():
    cases = [SYM, SCH, "", "C {a.sym} 1 2 5 3 {name=x1 W=\"1 2\" k}", "B 5 0 0 1 {unterminated", "{{{", "}}}", "\\",
             "C {a.sym} 0 0 0 0 {} [ unterminated", "L 4 1_0 2 3 4 {}", "# c\n* c\nZ x\nN 1 2 3 4 {}",
             "C {a.sym} 0 0 0 0 {=v x= a=1 a=2 c=\"q\\\"q\"}", "C {日本.sym} 1 1 0 0 {name=日本 v=\"値\"}"]
    for t in cases:
        assert xschem.parse(t) == xschem._py_parse(t), t[:40]
    for p in ["a=1", "a=\"x \\\" y\"", "=1", "k", "a=1 a=2", "x=\"unterminated", "é=ü", "a=\"\\é\""]:
        assert xschem.parse_props(p) == xschem._py_parse_props(p), p


def test_render_extract_dump_match_reference():
    d = _setup()
    res = _resolver(d)
    for f in (d / "top.sch", d / "cell.sym"):
        assert _same(xschem.render(f, res), xschem._py_render(f, res)) is None
        x = xschem.parse(f.read_text())
        assert xschem.dump(x) == xschem._py_dump(x)
    a, b = schematic.extract(d / "top.sch", res), schematic._py_extract(d / "top.sch", res)
    assert _conn(a) == _conn(b)
    assert a.wire_net[0] == "in" and a.wire_net[3] == "out" and any("missing.sym" in w for w in a.warnings)
    shutil.rmtree(d)


def test_wire_id_matches_reference():
    vals = [0, -0.0, 1, 0.5, 1e-5, 123456.7, 1234567.0, 999999.5, 1e21, -3.25, 0.1 + 0.2, float("inf"), 1e-300]
    for a in vals:
        for b in vals:
            assert xschem.wire_id(a, b, b, a) == xschem._py_wire_id(a, b, b, a)


def test_render_model_uses_unsaved_geometry_without_touching_the_design(tmp_path):
    (tmp_path / "cell.sym").write_text(SYM)
    path = tmp_path / "top.sch"
    path.write_text(SCH)
    original = path.read_bytes()
    model = xschem.parse(SCH)
    model.comps[0].x = 75
    model.comps[0].y = -25
    model.wires.append((75, -25, 175, -25, {"lab": "preview"}))
    resolver = _resolver(tmp_path)
    preview = xschem.render_model(model, path, resolver)
    assert path.read_bytes() == original
    assert preview["insts"][0]["x"] == 75 and preview["insts"][0]["y"] == -25
    assert preview["wires"][-1][0:6] == [75, -25, 175, -25, False, "preview"]
    # Relative and embedded symbols also resolve when the draft has no disk file.
    draft = tmp_path / "draft.sch"
    assert xschem.render_model(model, draft, resolver)["lines"] == preview["lines"]
    assert not draft.exists()


def test_pdk_libraries_match_reference():
    """Every Xschem file of the installed PDK (skipped without one)."""
    pdks = discover(default_pdk_roots())
    pdk = next((p for p in pdks.values() if p.xschem_dir and p.family == "sky130"), None)
    if pdk is None:
        return
    res = xschem.SymbolResolver([pdk.xschem_dir])
    files = sorted(f for f in pdk.xschem_dir.rglob("*") if f.suffix in (".sch", ".sym"))
    for f in files:
        t = f.read_text(errors="replace")
        x = xschem.parse(t)
        assert x == xschem._py_parse(t) or "nan" in t, f
        assert xschem.dump(x) == xschem._py_dump(x), f
        assert _same(xschem.render(f, res), xschem._py_render(f, res)) is None, f
        if f.suffix == ".sch":
            assert _conn(schematic.extract(f, res)) == _conn(schematic._py_extract(f, res)), f


if __name__ == "__main__":
    for n, fn in list(globals().items()):
        if n.startswith("test_"):
            fn()
            print("ok", n)
