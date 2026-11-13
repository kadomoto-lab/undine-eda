"""Phase 2's third behaviour change for plain git dependencies: a new local name may not be a folder of Xschem's
library or of a project.json symbol_paths entry (.undine/deps comes first on the one symbol path and would shadow
it), compared casefolded; a name project.json already gives keeps working, and status only notes it.

The tools are fixed (as in test_store_sync.py); git is real, every repository local, and Xschem's library is a
temporary folder."""
import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine import tools  # noqa: E402
from undine.store import deps  # noqa: E402
from undine.store.project import ProjectStore  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

NOW = {"undine": "0.1.0 (abc1234)", "pixi_lock": None, "klayout": {"version": "0.30.12", "from": "pixi"},
       "xschem": None, "ngspice": None, "magic": None, "netgen": None}
BANNERS = {"klayout": "KLayout 0.30.12", "xschem": None, "ngspice": None, "magic": None, "netgen": None}
SYM = "v {}\nK {type=subcircuit}\n"


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


@pytest.fixture
def xlib(tmp_path, monkeypatch) -> Path:
    """Xschem's library: a folder with one subfolder, `analog`."""
    d = tmp_path / "xschem_library"
    (d / "analog").mkdir(parents=True)
    monkeypatch.setattr(tools, "xschem_library", lambda: d)
    monkeypatch.setattr(tools, "tools_record", lambda: copy.deepcopy(NOW))
    monkeypatch.setattr(tools, "version", lambda n: BANNERS[n])
    monkeypatch.delenv("UNDINE_OFFLINE", raising=False)
    return d


def _git(d: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


def _plain(repo: Path) -> Path:
    """A plain git dependency (no manifest) with one cell, tagged v1."""
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "inv.sym").write_text(SYM)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "v1")
    _git(repo, "tag", "v1")
    return repo


def _files(prj) -> dict[str, bytes]:
    out = {n: (prj.path / n).read_bytes() for n in ("project.json", deps.LOCK) if (prj.path / n).exists()}
    d = prj.path / deps.DEPS
    out.update({f.relative_to(d).as_posix(): f.read_bytes() for f in sorted(d.rglob("*")) if f.is_file()} if d.is_dir()
               else {})
    return out


def test_a_new_local_name_may_not_be_a_folder_of_xschems_library_or_of_symbol_paths(tmp_path, xlib):
    repo = _plain(tmp_path / "src" / "plain")
    syms = tmp_path / "syms"
    (syms / "mysyms").mkdir(parents=True)
    store = ProjectStore(tmp_path / "projects", {})
    prj = store.create("p", None)
    meta = json.loads((prj.path / "project.json").read_text())
    meta["symbol_paths"] = [str(syms)]
    (prj.path / "project.json").write_text(json.dumps(meta, indent=2) + "\n")
    before = _files(store.get("p"))
    for name, what in (("analog", f"a folder of Xschem's library ({xlib / 'analog'})"),
                       ("Analog", f"a folder of Xschem's library ({xlib / 'analog'})"),     # casefolded
                       ("mysyms", f"a folder of symbol path {syms} ({syms / 'mysyms'})")):
        with pytest.raises(deps.DepError) as e:
            deps.add(store.get("p"), name, str(repo), "v1")
        assert str(e.value) == (f"{name}: {what} has that name, and .undine/deps would shadow it: choose another "
                                "local name; nothing was changed")
        assert _files(store.get("p")) == before
    deps.add(store.get("p"), "other", str(repo), "v1")                  # any other name is taken as before
    assert (prj.path / deps.DEPS / "other" / "inv.sym").is_file()


def test_a_name_project_json_already_gives_keeps_working_and_status_notes_what_it_shadows(tmp_path, xlib):
    repo = _plain(tmp_path / "src" / "plain")
    store = ProjectStore(tmp_path / "projects", {})
    prj = store.create("p", None)
    (xlib / "analog").rmdir()                                            # added before Xschem's folder existed
    deps.add(prj, "analog", str(repo), "v1")
    (xlib / "analog").mkdir()
    st = deps.status(store.get("p"))
    assert st["ok"], st["issues"]
    assert (f"library analog: a folder of Xschem's library ({xlib / 'analog'}) has that name too: "
            ".undine/deps/analog comes first on the symbol path and shadows it") in st["notes"]
    assert [r["action"] for r in deps.sync(store.get("p"))] == ["ok"]    # synced and updated as before
    deps.update(store.get("p"), "analog", "v1")
    assert (prj.path / deps.DEPS / "analog" / "inv.sym").is_file()
