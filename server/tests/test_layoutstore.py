"""Layout storage: one OASIS file per cell, writes only what changed, migration from layout.oas, git diff as text."""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import klayout.db as kdb  # noqa: E402

from undine.layouts import layoutstore  # noqa: E402
from undine.store import provenance
from undine.__main__ import git_setup  # noqa: E402
from undine.store.project import FORMAT, Project  # noqa: E402


def _layout():
    ly = kdb.Layout()
    ly.dbu = 0.001
    top, inv, via = ly.create_cell("top"), ly.create_cell("inv"), ly.create_cell("via$1")
    m1, m2 = ly.layer(68, 20), ly.layer(69, 20)
    via.shapes(m1).insert(kdb.Box(0, 0, 170, 170))
    s = inv.shapes(m1).insert(kdb.Box(0, 0, 500, 2000))
    s.set_property("net", "Y")
    inv.shapes(m1).insert(kdb.Text("Y", kdb.Trans(100, 100)))
    inv.insert(kdb.CellInstArray(via.cell_index(), kdb.Trans(10, 10), kdb.Vector(400, 0), kdb.Vector(0, 400), 3, 2))
    i = top.insert(kdb.CellInstArray(inv.cell_index(), kdb.Trans(kdb.Trans.M90, 5000, 0)))
    i.set_property("undine_inst", "X1")
    top.shapes(m2).insert(kdb.Path([kdb.Point(0, 0), kdb.Point(9000, 0)], 300))
    inv.add_meta_info(kdb.LayoutMetaInfo("undine.pdks.pcell", json.dumps({"pcell": "inv", "params": {"w": 1}}), None, True))
    return ly


def _same(a: kdb.Layout, b: kdb.Layout) -> bool:
    return layoutstore.snapshot(a) == layoutstore.snapshot(b)


def test_file_names():
    for name in ["inv", "via$1", "a/b", ".hidden", "日本", "x y", "layout"]:
        f = layoutstore.file_name(name)
        assert "/" not in f and not f.startswith(".") and layoutstore.cell_name(Path(f)) == name


def test_write_read_round_trip_in_any_order():
    d = Path(tempfile.mkdtemp())
    ly = _layout()
    saved = layoutstore.write(d, ly, {})
    assert sorted(p.name for p in d.iterdir()) == ["inv.oas", "top.oas", "via$1.oas"]
    for order in (1, -1):
        back = kdb.Layout()
        for f in layoutstore.cell_files(d)[::order]:
            back.read(str(f))
        assert _same(ly, back) and not any(c.is_ghost_cell() for c in back.each_cell())
        assert provenance.layout_sha(back, "top") == provenance.layout_sha(ly, "top")
        assert json.loads(next(back.cell("inv").each_meta_info()).value)["params"] == {"w": 1}   # PCell parameters
        assert dict(back.properties(next(back.cell("top").each_inst()).prop_id)) == {"undine_inst": "X1"}
    assert saved == layoutstore.snapshot(ly)
    # a cell file holds the cell once; what it uses is there by name only
    one = kdb.Layout()
    one.read(str(d / "top.oas"))
    assert [(c.name, c.is_ghost_cell()) for c in one.each_cell()] == [("top", False), ("inv", True)]
    shutil.rmtree(d)


def test_write_touches_only_changed_cells():
    d = Path(tempfile.mkdtemp())
    ly = _layout()
    saved = layoutstore.write(d, ly, {})
    ino = {p.name: p.stat().st_ino for p in d.iterdir()}
    back = kdb.Layout()
    layoutstore.read(d, back)
    saved = layoutstore.snapshot(back)
    assert layoutstore.write(d, back, saved) == saved                        # nothing changed: nothing written
    assert {p.name: p.stat().st_ino for p in d.iterdir()} == ino
    back.cell("inv").shapes(back.layer(68, 20)).insert(kdb.Box(0, 3000, 100, 3100))
    back.delete_cell(back.cell("via$1").cell_index())
    saved = layoutstore.write(d, back, saved)
    now = {p.name: p.stat().st_ino for p in d.iterdir()}
    assert sorted(now) == ["inv.oas", "top.oas"] and now["top.oas"] == ino["top.oas"] and now["inv.oas"] != ino["inv.oas"]
    assert not list(d.glob(".*"))                                            # no temporary files left
    shutil.rmtree(d)


def test_project_format_1_is_converted():
    root = Path(tempfile.mkdtemp())
    p = root / "old"
    (p / "libs" / "l").mkdir(parents=True)
    (p / "project.json").write_text(json.dumps({"name": "old", "pdk": None, "libraries": {"l": "libs/l"}}))
    ly = _layout()
    ly.write(str(p / "libs/l/layout.oas"))
    prj = Project(p, {})
    assert prj.meta["format"] == FORMAT == json.loads((p / "project.json").read_text())["format"]
    lib = prj.library("l")
    assert sorted(f.name for f in lib.layout_files()) == ["inv.oas", "top.oas", "via$1.oas"]
    assert (p / "libs/l/layout.oas.pre-split").is_file() and not (p / "libs/l/layout.oas").exists()
    assert lib.layout_cells() == (["inv", "top", "via$1"], ["top"])
    back = kdb.Layout()
    lib.read_layout(back)
    assert _same(ly, back)
    Project(p, {})                                                           # converted once
    assert (p / "libs/l/layout.oas.pre-split").is_file()
    shutil.rmtree(root)


def test_git_diff_shows_cells_as_text():
    if not shutil.which("git"):
        return
    root = Path(tempfile.mkdtemp())
    lib = root / "libs" / "l"
    ly = _layout()
    saved = layoutstore.write(lib, ly, {})
    text = layoutstore.textconv(lib / "inv.oas")
    assert "cell inv" in text and "uses via$1" in text and "68/20  box (0,0;500,2000) props={net=>Y}" in text
    git_setup(root)
    git = lambda *a: subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *a],  # noqa: E731
                                    check=True, capture_output=True, text=True).stdout
    assert "runs/" in (root / ".gitignore").read_text() and "*.oas diff=undine-layout" in (root / ".gitattributes").read_text()
    git("add", "-A")
    git("commit", "-qm", "one")
    ly.cell("inv").shapes(ly.layer(68, 20)).insert(kdb.Box(0, 3000, 100, 3100))
    layoutstore.write(lib, ly, saved)
    diff = git("diff")
    assert "+  68/20  box (0,3000;100,3100)" in diff and "top.oas" not in diff
    shutil.rmtree(root)


if __name__ == "__main__":
    for n, fn in list(globals().items()):
        if n.startswith("test_"):
            fn()
            print("ok", n)
