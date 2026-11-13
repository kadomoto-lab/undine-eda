"""PCells: the supported generation (open_pdks 2025+, gdsfactory 8 / kfactory) runs; the older one is reported."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine.pdks import pcell  # noqa: E402
from undine.pdks.pdk import default_pdk_roots, discover  # noqa: E402
from undine.store.project import ProjectStore, pick_pdk  # noqa: E402


class _Pdk:
    def __init__(self, tech: Path, name="sky130A", version="0" * 40):
        self.klayout_tech, self.name, self.version, self.family = tech, name, version, "sky130"

    @property
    def id(self):
        return f"{self.name}@{self.version[:12]}"

    def public(self):
        return {"id": self.id, "name": self.name, "version": self.version}


def _tech(*dirs: str) -> Path:
    t = Path(tempfile.mkdtemp())
    for d in dirs:
        (t / d / "cells").mkdir(parents=True)
        (t / d / "cells" / "__init__.py").write_text("")
    return t


def test_only_the_newer_pcell_code_runs():
    new, old = _tech("python"), _tech("pymacros")
    (new / "pymacros").mkdir()                       # a newer PDK keeps pymacros/ for its loader macro, without cells
    assert pcell.macros_dir(_Pdk(new)) == new / "python" and not pcell.legacy(_Pdk(new))
    assert pcell.macros_dir(_Pdk(old)) is None and pcell.legacy(_Pdk(old))
    why = pcell.unavailable_reason(_Pdk(old, version="bdc9412b3e468c102d01b7cf6337be06ec6e9c9a"))
    assert "no longer supports" in why and f"set-pdk PROJECT sky130A@{pcell.SUPPORTED['sky130'][:12]}" in why
    assert "no PCell generators" in pcell.unavailable_reason(_Pdk(_tech()))


def test_missing_environment_is_named(monkeypatch):
    monkeypatch.setattr(pcell, "python_exe", lambda env="pcell": None)
    assert "pixi install -e pcell" in pcell.unavailable_reason(_Pdk(_tech("python")))


def test_a_bare_pdk_name_prefers_the_supported_version():
    old = _Pdk(_tech("pymacros"), version="b" * 40)
    new = _Pdk(_tech("python"), version="8" * 40)
    pdks = {old.id: old, new.id: new}                # the old one is found first
    assert pick_pdk(pdks, "sky130A") is new and pick_pdk(pdks, old.id) is old and pick_pdk(pdks, "x") is None


def test_set_pdk_moves_a_project_and_records_it():
    old = _Pdk(_tech("pymacros"), version="b" * 40)
    new = _Pdk(_tech("python"), version="8" * 40)
    store = ProjectStore(Path(tempfile.mkdtemp()), {old.id: old, new.id: new})
    prj = store.create("p", old.id)
    pub = prj.public()
    assert pub["pdk_unsupported"] and pub["pdk_upgrade"] == new.id
    assert prj.set_pdk("sky130A") == (old.id, new.id)
    prj = store.get("p")
    assert prj.meta["pdk"] == new.id and prj.meta["pdk_history"][-1]["from"] == old.id
    assert json.loads((prj.path / "undine.lock").read_text())["pdk"] == new.id
    assert prj.public()["pdk_unsupported"] is None
    assert store.create("q", "sky130A").meta["pdk"] == new.id             # new projects get the supported version


def test_real_pcells_list_and_generate():
    pdks = discover(default_pdk_roots())
    pdk = next((p for p in pdks.values() if p.family == "sky130" and pcell.available(p)), None)
    if pdk is None:
        pytest.skip("no sky130 PDK with supported PCells and the PCell environment installed")
    cat = {c["name"]: c for c in pcell.catalog(pdk)}
    assert {"nfet", "pfet", "vias_gen"} <= set(cat)
    typ = next(q for q in cat["nfet"]["params"] if q["name"] == "type")
    assert typ["type"] == "list" and "sky130_fd_pr__nfet_01v8" in typ["choice_values"]   # a string with choices
    path, name, info = pcell.generate(pdk, "nfet", {"type": "sky130_fd_pr__nfet_01v8", "w": 1.0, "l": 0.15, "nf": 2})
    assert path.is_file() and info["bbox"][2] - info["bbox"][0] > 1000
    import klayout.db as kdb
    ly = kdb.Layout()
    ly.read(str(path))
    c = ly.cell(name)
    meta = json.loads(next(m for m in c.each_meta_info() if m.name == "undine.pdks.pcell").value)
    assert meta["pcell"] == "nfet" and meta["params"]["nf"] == 2
    poly = ly.find_layer(66, 20)
    assert poly is not None and not c.shapes(poly).is_empty()                     # the gates are there


def test_the_worker_is_run_without_writing_bytecode(monkeypatch):
    # PCell code imported from the PDK must leave no __pycache__ in the PDK tree: the lock pins the tree's hash
    seen = {}

    def run(argv, **kw):
        seen.update(kw)
        return type("R", (), {"returncode": 0, "stdout": "[]\n", "stderr": ""})()

    monkeypatch.setattr(pcell, "python_exe", lambda env="pcell": sys.executable)
    monkeypatch.setattr(pcell.subprocess, "run", run)
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "")
    pcell._run({"env": "pcell"}, ["list"])
    assert seen["env"]["PYTHONDONTWRITEBYTECODE"] == "1" and seen["env"]["PATH"] == os.environ["PATH"]


def test_a_worker_run_leaves_no_pycache_in_the_pcell_package(tmp_path):
    # a stand-in PDK: a PCell package and a pya with an empty library, both imported from the PDK's paths
    lib = tmp_path / "pdk" / "libs.tech" / "klayout" / "python"
    (lib / "cells").mkdir(parents=True)
    (lib / "cells" / "__init__.py").write_text("from . import nfet\n")
    (lib / "cells" / "nfet.py").write_text("X = 1\n")
    (lib / "pya.py").write_text(
        "class _Layout:\n    def pcell_names(self):\n        return []\n"
        "class _Lib:\n    def layout(self):\n        return _Layout()\n"
        "class Library:\n    @staticmethod\n    def library_by_name(*a):\n        return _Lib()\n")
    spec = {"paths": [str(lib)], "module": "cells", "init": None, "library": "fake", "technology": None}
    env = {k: v for k, v in os.environ.items() if k != "PYTHONDONTWRITEBYTECODE"}   # the worker itself must see to it
    r = subprocess.run([sys.executable, str(pcell.WORKER), json.dumps(spec), "list"], capture_output=True, text=True,
                       env=env, cwd=tmp_path)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip().splitlines()[-1]) == []
    assert not list(tmp_path.rglob("__pycache__"))
