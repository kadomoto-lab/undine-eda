"""Phase 4 of docs/design/packages.md, slice 1: dependency operations in a changeset's working copy (dep_add,
dep_update, dep_remove, pdk_set: Changesets.dep_op / pdk_set), recorded in the project log (history/@project.jsonl),
taken back by restore, merged and installed from the store at adopt (nothing fetched then), and the browser's
one-click route (POST /deps/changeset) that runs one in a small changeset and adopts it.

Every repository is local, under the test's directory (a --dep-root); the tools are fixed; nothing here uses the
network or ~/.undine."""
import copy
import json
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fastapi.testclient import TestClient  # noqa: E402
import test_changeset as tc  # noqa: E402
from undine import tools  # noqa: E402
from undine.pdks.pdk import Pdk  # noqa: E402
from undine.server import live  # noqa: E402
from undine.server.api import Server, create_app  # noqa: E402
from undine.store import deps  # noqa: E402
from undine.store.changeset import PROJECT_LOG, Busy, ChangesetError, Changesets, MergeConflict  # noqa: E402
from undine.store.project import check_name  # noqa: E402
from undine.vcs import branches  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

NOW = {"undine": "0.1.0 (abc1234)", "pixi_lock": None, "klayout": {"version": "0.30.12", "from": "pixi"},
       "xschem": None, "ngspice": None, "magic": None, "netgen": None}
BANNERS = {"klayout": "KLayout 0.30.12", "xschem": None, "ngspice": None, "magic": None, "netgen": None}
SYM = "v {}\nK {type=subcircuit\nformat=\"@name @pinlist @symname\"\ntemplate=\"name=x1\"}\n"


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


@pytest.fixture(autouse=True)
def _tools_here(monkeypatch):
    monkeypatch.setattr(tools, "tools_record", lambda: copy.deepcopy(NOW))
    monkeypatch.setattr(tools, "version", lambda n: BANNERS[n])
    monkeypatch.setattr(tools, "xschem_library", lambda: None)
    monkeypatch.delenv("UNDINE_OFFLINE", raising=False)
    monkeypatch.delenv("UNDINE_STORE_LINK", raising=False)


def _git(d: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


def _repo(d: Path, cells: dict[str, str], tag: str = "v1") -> Path:
    """A repository with a library in lib/ (`cells`: {cell: symbol text}), committed and tagged."""
    if not (d / ".git").exists():
        d.mkdir(parents=True)
        _git(d, "init", "-q")
    for cell, text in cells.items():
        (d / "lib").mkdir(exist_ok=True)
        (d / "lib" / f"{cell}.sym").write_text(text)
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", tag)
    _git(d, "tag", tag)
    return d


def _setup(tmp_path: Path, pdks: dict | None = None):
    """The changeset test project (test_changeset.setup: no git repository yet), its server taking local
    repositories from under tmp_path (--dep-root)."""
    root, srv, cs = tc.setup()
    srv.dep_roots = [tmp_path.resolve()]
    if pdks:
        srv.pdks.update(pdks)
    return root, srv, cs


def _add(cs, cid: str, name: str, repo: Path, tmp_path: Path, **more) -> dict:
    return cs.dep_op("p", cid, "add", {"name": name, "git": str(repo), "subdir": "lib", **more}, "user", f"use {name}",
                     local_roots=[tmp_path.resolve()])


def _files(d: Path) -> tuple:
    """project.json, undine.lock and what .undine/deps holds (name -> files) of a project directory."""
    root = d / deps.DEPS
    installed = {p.name: sorted(f.relative_to(p).as_posix() for f in p.rglob("*") if f.is_file())
                 for p in sorted(root.iterdir()) if not p.name.startswith(".")} if root.is_dir() else {}
    lock = d / deps.LOCK
    return (d / "project.json").read_text(), lock.read_text() if lock.is_file() else None, installed


def _project_log(srv, project: str) -> list[dict]:
    return srv.oplog(project, PROJECT_LOG).read(limit=None)


# ------------------------------------------------------------------ the acceptance: add, adopt, restore


def test_add_in_a_changeset_adopt_restore(tmp_path):
    root, srv, cs = _setup(tmp_path)
    ip = _repo(tmp_path / "ip", {"buf": SYM})
    meta = cs.create("p", "add ip", "a buffer", "user")
    cid, P = meta["id"], meta["preview"]
    r = _add(cs, cid, "ip", ip, tmp_path)
    assert r["changeset"] == cid and r["op"]["gseq"] and r["result"]["cells"] == ["buf"]
    assert srv.project(P).libraries()["ip"].origin == "dep"                   # the preview has it
    assert "ip" not in srv.project("p").libraries()                           # the project does not
    assert not (srv.project("p").path / deps.DEPS / "ip").exists()
    rec = _project_log(srv, P)[-1]
    assert rec["op"] == "dep_add" and rec["changeset"] == cid and rec["actor"] == "user"
    (e,) = rec["effects"]
    assert e["k"] == "d" and e["do"] == "add" and e["name"] == "ip" and e["state"] == "installed"
    assert "ip" not in e["before"]["project"]["libraries"] and "ip" in e["after"]["project"]["libraries"]
    assert "ip" not in e["before"]["lock"]["libraries"] and e["after"]["lock"]["libraries"]["ip"]["commit"]
    tl = cs.timeline("p", cid)
    assert [o["ops"] for o in tl["ops"]] == [["dep_add"]] and tl["ops"][0]["in_effect"]
    assert tl["ops"][0]["effects"] == ["added dependency ip"]
    assert [lc["lib"] for lc in cs.get("p", cid)["changes"]] == [PROJECT_LOG]
    # a cell of it placed in the changeset
    ed = srv.editor(P, "l", "top", "schematic")
    with ed.recording("add_instance", {"sym": "ip/buf.sym"}, "user", "place the buffer", cid):
        ed.add_instance("ip/buf.sym", 200, 0)

    applied = cs.apply("p", cid, "user")["applied"]
    prj = srv.project("p")
    assert [x["name"] for x in applied["deps"]] == ["ip"] and not applied.get("deps_warning")
    assert (deps.dep_dir(prj.path, "ip") / "buf.sym").read_text() == SYM
    row = next(x for x in deps.status(prj)["libraries"] if x["name"] == "ip")
    assert row["state"] == "ok", row
    assert applied["commit"] and branches.is_clean(prj.path)               # the merge committed, nothing left over
    assert "ip/buf.sym" in (prj.library("l").path / "top.sch").read_text()
    assert [r["op"] for r in _project_log(srv, "p")] == ["dep_add", "merge"]

    # a second changeset adds another dependency and is restored to before the add
    amp = _repo(tmp_path / "amp", {"amp": SYM})
    b = cs.create("p", "add amp", "", "user")
    W = srv.project(b["preview"]).path
    before = _files(W)
    _add(cs, b["id"], "amp", amp, tmp_path)
    assert "amp" in _files(W)[2] and _files(W) != before
    got = cs.restore("p", b["id"], 0, "user")
    assert _files(W) == before                                               # project.json, lock, installs
    tl = cs.timeline("p", b["id"])
    assert [o["kind"] for o in tl["ops"]] == ["edit", "restore"] and not tl["ops"][0]["in_effect"]
    assert [r["op"] for r in _project_log(srv, b["preview"])][-2:] == ["dep_add", "restore"]
    assert got["status"] == "open"
    cs.discard("p", b["id"])
    shutil.rmtree(root)


def test_dep_op_never_writes_through_links(tmp_path):
    """The working copy's .undine/deps are hard links of the project's installed files: an update, an add and a
    remove there replace and remove directories, never write a linked file."""
    root, srv, cs = _setup(tmp_path)
    ip = _repo(tmp_path / "ip", {"buf": SYM})
    prj = srv.project("p")
    deps.add(prj, "ip", str(ip), "v1", "lib")                              # installed in the project (the CLI)
    d = deps.dep_dir(prj.path, "ip")
    have = {f.relative_to(d).as_posix(): (f.stat().st_ino, f.read_bytes()) for f in d.rglob("*") if f.is_file()}
    meta = cs.create("p", "move ip", "", "user")
    W = srv.project(meta["preview"]).path
    assert (deps.dep_dir(W, "ip") / "buf.sym").stat().st_ino == have["buf.sym"][0]     # linked
    _repo(ip, {"buf": SYM.replace("x1", "x2"), "inv": SYM}, "v2")
    r = cs.dep_op("p", meta["id"], "update", {"name": "ip", "rev": "v2"}, local_roots=[tmp_path.resolve()])
    assert r["result"]["changed"]["added"] == ["inv.sym"] and r["result"]["changed"]["modified"] == ["buf.sym"]
    _add(cs, meta["id"], "amp", _repo(tmp_path / "amp", {"amp": SYM}), tmp_path)
    cs.dep_op("p", meta["id"], "remove", {"name": "ip"})
    assert not deps.dep_dir(W, "ip").exists()
    now = {f.relative_to(d).as_posix(): (f.stat().st_ino, f.read_bytes()) for f in d.rglob("*") if f.is_file()}
    assert now == have                                                     # the project's files as they were
    assert not stat.S_IMODE((d / "buf.sym").stat().st_mode) & 0o222
    cs.discard("p", meta["id"])
    shutil.rmtree(root)


def _pdks(root: Path) -> dict:
    out = {}
    for name, commit in (("fakeA", "a" * 40), ("fakeB", "b" * 40)):
        (root / name / "libs.tech").mkdir(parents=True, exist_ok=True)
        (root / name / "libs.tech" / "models.spice").write_text(f"* {name}\n")
        out[name] = Pdk(name, "fake", commit, root / name)
    return {p.id: p for p in out.values()}


def _id(pdks: dict, name: str) -> str:
    return next(p.id for p in pdks.values() if p.name == name)


def test_update_remove_and_pdk_set_in_changeset(tmp_path):
    pdks = _pdks(tmp_path / "pdks")
    root, srv, cs = _setup(tmp_path, pdks)
    ip = _repo(tmp_path / "ip", {"buf": SYM})
    meta = cs.create("p", "dependencies", "", "user")
    cid, P = meta["id"], meta["preview"]
    W = srv.project(P).path
    _add(cs, cid, "ip", ip, tmp_path, rev="v1")
    v1 = deps.read_lock(W)["libraries"]["ip"]["commit"]
    _repo(ip, {"inv": SYM}, "v2")
    up = cs.dep_op("p", cid, "update", {"name": "ip", "rev": "v2"}, "user", "newer", local_roots=[tmp_path.resolve()])
    assert up["result"]["from"] == v1 and up["result"]["to"] != v1
    assert (deps.dep_dir(W, "ip") / "inv.sym").is_file()
    gone = cs.dep_op("p", cid, "remove", {"name": "ip"}, "user", "not needed")
    assert gone["result"]["ok"] and gone["result"]["name"] == "ip"
    assert "ip" not in json.loads((W / "project.json").read_text())["libraries"] and not deps.dep_dir(W, "ip").exists()
    pdk = cs.pdk_set("p", cid, "fakeB", "user", "the other PDK")
    assert (pdk["old"], pdk["new"]) == (None, _id(pdks, "fakeB"))
    assert json.loads((W / "project.json").read_text())["pdk"] == _id(pdks, "fakeB")
    assert deps.lock_pdk_id(deps.read_lock(W)) == _id(pdks, "fakeB")
    recs = _project_log(srv, P)
    assert [r["op"] for r in recs] == ["dep_add", "dep_update", "dep_remove", "pdk_set"]
    assert [r["effects"][0]["do"] for r in recs] == ["add", "mod", "del", "mod"]
    assert recs[-1]["effects"][0]["pdk"] == [None, _id(pdks, "fakeB")] and recs[-1]["reason"] == "the other PDK"
    assert srv.project("p").meta.get("pdk") is None                        # the project untouched
    # restore to after the update: the remove and the PDK change taken back
    cs.restore("p", cid, up["op"]["gseq"], "user")
    assert (deps.dep_dir(W, "ip") / "inv.sym").is_file()
    assert json.loads((W / "project.json").read_text()).get("pdk") is None
    # adopted: the project gets the dependency at v2, and no PDK change
    applied = cs.apply("p", cid, "user")["applied"]
    prj = srv.project("p")
    assert deps.read_lock(prj.path)["libraries"]["ip"]["commit"] == up["result"]["to"]
    assert (deps.dep_dir(prj.path, "ip") / "inv.sym").is_file() and not applied.get("deps_warning")
    shutil.rmtree(root)


def test_pdk_set_adopted_moves_the_project(tmp_path):
    pdks = _pdks(tmp_path / "pdks")
    root, srv, cs = _setup(tmp_path, pdks)
    meta = cs.create("p", "pdk", "", "user")
    cs.pdk_set("p", meta["id"], "fakeA")
    applied = cs.apply("p", meta["id"], "user")["applied"]
    prj = srv.project("p")
    assert prj.meta["pdk"] == _id(pdks, "fakeA") and deps.lock_pdk_id(deps.read_lock(prj.path)) == _id(pdks, "fakeA")
    assert not applied.get("deps_warning")
    shutil.rmtree(root)


def test_baseline_preview_refused(tmp_path):
    root, srv, cs = _setup(tmp_path)
    ip = _repo(tmp_path / "ip", {"buf": SYM})
    meta = cs.create("p", "t", "", "user")
    B = srv.project(meta["baseline"]).path
    before = _files(B)
    with pytest.raises(PermissionError):
        cs.dep_op(meta["baseline"], meta["id"], "add", {"name": "ip", "git": str(ip), "subdir": "lib"},
                  local_roots=[tmp_path.resolve()])
    cl = TestClient(create_app(srv, None))
    r = cl.post(f"/api/p/{meta['baseline']}/changesets/{meta['id']}/deps",
                json={"action": "add", "name": "ip", "git": str(ip), "subdir": "lib"})
    assert r.status_code == 403, r.text
    r = cl.post(f"/api/p/{meta['baseline']}/deps", json={"name": "ip", "git": str(ip), "subdir": "lib"})
    assert r.status_code == 400                                            # the direct route, as before
    assert _files(B) == before and not _project_log(srv, meta["preview"])
    shutil.rmtree(root)


def test_dep_op_respects_write_lease(tmp_path):
    root, srv, cs = _setup(tmp_path)
    ip = _repo(tmp_path / "ip", {"buf": SYM})
    meta = cs.create("p", "t", "", "agent:t")
    W = srv.project(meta["preview"]).path
    before = _files(W)
    srv.leases.acquire(meta["id"], "mcp:s1", "mcp", "claude-code")
    with pytest.raises(live.WriteLocked):
        _add(cs, meta["id"], "ip", ip, tmp_path)
    cl = TestClient(create_app(srv, None))
    r = cl.post(f"/api/p/p/changesets/{meta['id']}/deps",
                json={"action": "add", "name": "ip", "git": str(ip), "subdir": "lib"})
    assert r.status_code == 409 and r.json()["code"] == "busy", r.text
    assert _files(W) == before
    with live.acting("mcp:s1"):                                            # the writer itself may
        _add(cs, meta["id"], "ip", ip, tmp_path)
    assert "ip" in _files(W)[2]
    srv.leases.release(meta["id"], "mcp:s1")
    shutil.rmtree(root)


# ------------------------------------------------------------------ the adopt


def test_adopt_refused_when_tree_not_in_store(tmp_path, _store):
    root, srv, cs = _setup(tmp_path)
    ip = _repo(tmp_path / "ip", {"buf": SYM})
    meta = cs.create("p", "add ip", "", "user")
    _add(cs, meta["id"], "ip", ip, tmp_path)
    prj = srv.project("p")
    before = _files(prj.path)
    shutil.rmtree(_store)                                                  # the store emptied (a gc, another disk)
    with pytest.raises(MergeConflict) as e:
        cs.apply("p", meta["id"], "user")
    f = next(f for f in e.value.files if f["path"] == deps.LOCK)
    assert f["kind"] == "lock" and "not in the store: fetch it first" in f["conflicts"][0]["what"]
    assert _files(prj.path) == before and cs.get("p", meta["id"])["status"] == "open"
    cs.discard("p", meta["id"])
    shutil.rmtree(root)


def test_adopt_install_recovered(tmp_path, monkeypatch):
    """A server killed after the merge was written and before the install: the next start installs."""
    root, srv, cs = _setup(tmp_path)
    ip = _repo(tmp_path / "ip", {"buf": SYM})
    meta = cs.create("p", "add ip", "", "user")
    _add(cs, meta["id"], "ip", ip, tmp_path)

    def killed(self, project):
        raise RuntimeError("killed")
    with monkeypatch.context() as m:
        m.setattr(Changesets, "_install_deps", killed)
        with pytest.raises(ChangesetError):
            cs.apply("p", meta["id"], "user")
    prj = srv.project("p")
    state = json.loads((prj.path / "changesets" / meta["id"] / "merging.json").read_text())
    assert state["written"] and state["install"] and not state.get("installed")
    assert "ip" in prj.meta["libraries"] and not deps.dep_dir(prj.path, "ip").exists()
    srv2 = Server(root, {}, [root], dep_roots=[tmp_path.resolve()])           # the restart
    done = Changesets(srv2).finish_interrupted()
    assert done == [f"p/{meta['id']}"]
    got = Changesets(srv2).get("p", meta["id"])
    assert (deps.dep_dir(prj.path, "ip") / "buf.sym").is_file()
    assert got["status"] == "applied" and [x["name"] for x in got["applied"]["deps"]] == ["ip"]
    assert not got["applied"].get("deps_warning")
    shutil.rmtree(root)


@pytest.mark.parametrize("first", ["a", "b"])
def test_two_changesets_add_then_both_installed(tmp_path, first):
    root, srv, cs = _setup(tmp_path)
    a = cs.create("p", "add amp", "", "user")
    b = cs.create("p", "add bias", "", "user")
    _add(cs, a["id"], "amp", _repo(tmp_path / "amp", {"amp": SYM}), tmp_path)
    _add(cs, b["id"], "bias", _repo(tmp_path / "bias", {"bias": SYM}), tmp_path)
    for m in (a, b) if first == "a" else (b, a):
        assert not cs.apply("p", m["id"], "user")["applied"].get("deps_warning")
    prj = srv.project("p")
    assert {"amp", "bias"} <= set(prj.meta["libraries"])
    assert set(deps.read_lock(prj.path)["libraries"]) == {"amp", "bias"}
    for n in ("amp", "bias"):
        assert (deps.dep_dir(prj.path, n) / f"{n}.sym").is_file()
    assert all(x["state"] == "ok" for x in deps.status(prj)["libraries"] if x["kind"] == "dependency")
    assert [r["op"] for r in _project_log(srv, "p")].count("dep_add") == 2
    shutil.rmtree(root)


def test_revert_uninstalls_added_dependency(tmp_path):
    """Decision 6: the revert goes through, and says which cells outside the changeset still place the dependency's
    cells."""
    root, srv, cs = _setup(tmp_path)
    ip = _repo(tmp_path / "ip", {"buf": SYM})
    meta = cs.create("p", "add ip", "", "user")
    _add(cs, meta["id"], "ip", ip, tmp_path)
    cs.apply("p", meta["id"], "user")
    prj = srv.project("p")
    assert deps.dep_dir(prj.path, "ip").is_dir()
    ed = srv.editor("p", "l", "top", "schematic")                          # placed in the project since
    with ed.recording("add_instance", {"sym": "ip/buf.sym"}, "user", "use it", None):
        ed.add_instance("ip/buf.sym", 200, 0)
    reverted = cs.revert("p", meta["id"], "user")["reverted"]
    prj = srv.project("p")
    assert "ip" not in prj.meta["libraries"] and not deps.dep_dir(prj.path, "ip").exists()
    assert reverted["deps_dropped"] == [{"name": "ip", "used_by": ["l/top"]}]
    assert "ip is no longer installed, but l/top still place its cells" in reverted["deps_warning"]
    assert [r["op"] for r in _project_log(srv, "p")][-1] == "merge"
    shutil.rmtree(root)


def test_restore_while_dep_op_running_is_busy(tmp_path, monkeypatch):
    """A dependency operation holds the working copy's dependency lock while it fetches and then waits for the
    changesets' lock at its commit; a restore holds that lock and would wait for the dependency lock: it is Busy
    instead, nothing changed, and the operation then goes through."""
    root, srv, cs = _setup(tmp_path)
    meta = cs.create("p", "t", "", "user")
    cid = meta["id"]
    W = srv.project(meta["preview"]).path
    _add(cs, cid, "ip", _repo(tmp_path / "ip", {"buf": SYM}), tmp_path)
    fetching, go = threading.Event(), threading.Event()
    stage = deps._Graph.stage

    def slow(self, installs):
        fetching.set()
        assert go.wait(30)
        return stage(self, installs)
    monkeypatch.setattr(deps._Graph, "stage", slow)
    out: dict = {}

    def run():
        try:
            out["r"] = _add(cs, cid, "amp", _repo(tmp_path / "amp", {"amp": SYM}), tmp_path)
        except Exception as e:                                              # noqa: BLE001
            out["e"] = e
    t = threading.Thread(target=run)
    t.start()
    assert fetching.wait(30)
    before = _files(W)
    with srv.changeset_lock:
        go.set()                                                           # it now waits for the changesets' lock
        with pytest.raises(Busy):
            cs.restore("p", cid, 0, "user")
        assert _files(W) == before
    t.join(30)
    assert not t.is_alive() and "e" not in out, out
    assert "amp" in _files(W)[2]
    shutil.rmtree(root)


def test_adopt_while_dep_op_fetches_does_not_wait_and_the_op_is_refused(tmp_path, monkeypatch):
    """A dependency operation holds no changeset lock while it fetches: an adopt of its changeset goes through
    meanwhile (nothing waits for the fetch), and the operation, checked again at its commit, is refused, writing
    nothing into the project."""
    root, srv, cs = _setup(tmp_path)
    meta = cs.create("p", "t", "", "user")
    cid = meta["id"]
    _add(cs, cid, "ip", _repo(tmp_path / "ip", {"buf": SYM}), tmp_path)
    fetching, go = threading.Event(), threading.Event()
    stage = deps._Graph.stage

    def slow(self, installs):
        fetching.set()
        assert go.wait(30)
        return stage(self, installs)
    monkeypatch.setattr(deps._Graph, "stage", slow)
    out: dict = {}

    def run():
        try:
            out["r"] = _add(cs, cid, "amp", _repo(tmp_path / "amp", {"amp": SYM}), tmp_path)
        except Exception as e:                                              # noqa: BLE001
            out["e"] = e
    t = threading.Thread(target=run)
    t.start()
    assert fetching.wait(30)
    applied = cs.apply("p", cid, "user")["applied"]                         # while the other one fetches
    go.set()
    t.join(30)
    assert not t.is_alive() and "e" in out and "r" not in out
    prj = srv.project("p")
    assert [x["name"] for x in applied["deps"]] == ["ip"] and "amp" not in prj.meta["libraries"]
    assert not deps.dep_dir(prj.path, "amp").exists()
    shutil.rmtree(root)


# ------------------------------------------------------------------ the browser's one click


def test_one_click_add_applied(tmp_path):
    root, srv, cs = _setup(tmp_path)
    ip = _repo(tmp_path / "ip", {"buf": SYM})
    cl = TestClient(create_app(srv, None))
    prj = srv.project("p")
    assert not (prj.path / ".git").exists()                                # decision 1: made a repository
    r = cl.post("/api/p/p/deps/changeset", json={"action": "add", "name": "ip", "git": str(ip), "subdir": "lib"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "applied" and body["git_setup"] is True and body["result"]["cells"] == ["buf"]
    assert [x["name"] for x in body["deps"]] == ["ip"] and "deps_warning" not in body
    assert (deps.dep_dir(prj.path, "ip") / "buf.sym").is_file()
    got = cs.get("p", body["changeset"])
    assert got["status"] == "applied" and got["title"] == "Add ip"
    # remove, the same way
    r = cl.post("/api/p/p/deps/changeset", json={"action": "remove", "name": "ip"})
    assert r.status_code == 200 and r.json()["result"]["ok"], r.text
    assert not deps.dep_dir(prj.path, "ip").exists() and "ip" not in srv.project("p").meta["libraries"]
    # an operation refused: its error, the changeset discarded
    r = cl.post("/api/p/p/deps/changeset", json={"action": "add", "name": "gone", "git": str(tmp_path / "nothing")})
    assert r.status_code == 400 and "is not a directory" in r.json()["detail"]
    assert [c["status"] for c in cs.list("p") if c["title"] == "Add gone"] == ["discarded"]
    # the direct route works as before
    r = cl.post("/api/p/p/deps", json={"name": "ip", "git": str(ip), "subdir": "lib"})
    assert r.status_code == 200, r.text
    shutil.rmtree(root)


def test_one_click_add_conflict_keeps_changeset(tmp_path, monkeypatch, _store):
    root, srv, cs = _setup(tmp_path)
    ip = _repo(tmp_path / "ip", {"buf": SYM})
    cl = TestClient(create_app(srv, None))
    dep_op = Changesets.dep_op

    def then_empty(self, *a, **kw):                                         # the store emptied before the adopt
        out = dep_op(self, *a, **kw)
        shutil.rmtree(_store)
        return out
    monkeypatch.setattr(Changesets, "dep_op", then_empty)
    r = cl.post("/api/p/p/deps/changeset", json={"action": "add", "name": "ip", "git": str(ip), "subdir": "lib"})
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "merge_conflict" and body["changeset"] and body["params"]["changeset"] == body["changeset"]
    got = cs.get("p", body["changeset"])
    assert got["status"] == "open" and [lc["lib"] for lc in got["changes"]] == [PROJECT_LOG]
    assert "ip" not in srv.project("p").meta["libraries"]
    shutil.rmtree(root)


def test_one_click_in_a_preview_goes_into_that_changeset(tmp_path):
    root, srv, cs = _setup(tmp_path)
    ip = _repo(tmp_path / "ip", {"buf": SYM})
    meta = cs.create("p", "t", "", "user")
    cl = TestClient(create_app(srv, None))
    r = cl.post(f"/api/p/{meta['preview']}/deps/changeset",
                json={"action": "add", "name": "ip", "git": str(ip), "subdir": "lib"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "open" and r.json()["changeset"] == meta["id"]
    assert "ip" in srv.project(meta["preview"]).libraries() and "ip" not in srv.project("p").libraries()
    cs.discard("p", meta["id"])
    shutil.rmtree(root)


def test_changeset_routes(tmp_path):
    pdks = _pdks(tmp_path / "pdks")
    root, srv, cs = _setup(tmp_path, pdks)
    ip = _repo(tmp_path / "ip", {"buf": SYM})
    meta = cs.create("p", "t", "", "user")
    cl = TestClient(create_app(srv, None))
    r = cl.post(f"/api/p/p/changesets/{meta['id']}/deps",
                json={"action": "add", "name": "ip", "git": str(ip), "subdir": "lib", "reason": "a buffer"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["op"]["gseq"] and body["result"]["name"] == "ip"
    assert [x["name"] for x in body["status"]["libraries"] if x["kind"] == "dependency"] == ["ip"]
    r = cl.post(f"/api/p/p/changesets/{meta['id']}/pdk", json={"pdk": "fakeA"})
    assert r.status_code == 200 and r.json()["new"] == _id(pdks, "fakeA"), r.text
    r = cl.post(f"/api/p/p/changesets/{meta['id']}/deps", json={"action": "remove", "name": "nothing"})
    assert r.status_code == 400
    r = cl.post(f"/api/p/p/changesets/{meta['id']}/deps", json={"action": "move", "name": "ip"})
    assert r.status_code == 400
    assert [x["op"] for x in _project_log(srv, meta["preview"])] == ["dep_add", "pdk_set"]
    cs.discard("p", meta["id"])
    shutil.rmtree(root)


def test_project_log_name_is_not_a_library_name(tmp_path):
    with pytest.raises(ValueError):
        check_name(PROJECT_LOG)
    root, srv, cs = _setup(tmp_path)
    with pytest.raises(ValueError):
        srv.project("p").create_library(PROJECT_LOG)
    assert PROJECT_LOG not in cs._libs(cs._record("p", cs.create("p", "t", "", "user")["id"]))
    shutil.rmtree(root)


# ------------------------------------------------------------------ found by review


def _no_lock(srv) -> None:
    """The project without undine.lock (one made before locks, or whose lock was deleted)."""
    (srv.project("p").path / deps.LOCK).unlink()


def test_install_never_waits_for_a_held_dependency_lock(tmp_path):
    """Another process (a CLI `pkg add` fetching for minutes) holds the project's dependency lock: the adopt does not
    wait for it with the changesets' lock and the project's locks held. It is done, the install left pending for
    the next sync."""
    root, srv, cs = _setup(tmp_path)
    meta = cs.create("p", "add ip", "", "user")
    _add(cs, meta["id"], "ip", _repo(tmp_path / "ip", {"buf": SYM}), tmp_path)
    prj = srv.project("p")
    (prj.path / ".undine").mkdir(exist_ok=True)
    holder = subprocess.Popen([sys.executable, "-c", "import fcntl, sys, time\n"
                               "f = open(sys.argv[1], 'a'); fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True)\n"
                               "time.sleep(120)\n", str(prj.path / ".undine" / "deps.lock")],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        out: dict = {}
        t = threading.Thread(target=lambda: out.update(cs.apply("p", meta["id"], "user")["applied"]))
        t.start()
        t.join(60)
        assert not t.is_alive(), "the adopt waited for the dependency lock"
        assert srv.changeset_lock.acquire(blocking=False)
        srv.changeset_lock.release()
        assert out["deps_pending"] is True and "not installed yet" in out["deps_warning"]
        assert "ip" in srv.project("p").meta["libraries"] and not deps.dep_dir(prj.path, "ip").exists()
    finally:
        holder.kill()
        holder.wait()
    deps.sync(srv.project("p"), offline=True)                              # the next sync installs it
    assert (deps.dep_dir(prj.path, "ip") / "buf.sym").is_file()
    shutil.rmtree(root)


def test_pdk_set_prepares_outside_the_locks(tmp_path, monkeypatch):
    """set_pdk's slow part (the tools' versions with no lock, a format 2 pin hashed) runs before the guard takes the
    server's and the changesets' locks."""
    pdks = _pdks(tmp_path / "pdks")
    root, srv, cs = _setup(tmp_path, pdks)
    _no_lock(srv)
    meta = cs.create("p", "pdk", "", "user")
    seen: list[dict] = []
    real = deps.environment

    def probe(*a, **kw):
        got: dict = {}

        def look():
            for name, lock in (("server", srv.lock), ("changesets", srv.changeset_lock)):
                got[name] = lock.acquire(blocking=False)
                if got[name]:
                    lock.release()
        t = threading.Thread(target=look)
        t.start()
        t.join()
        seen.append(got)
        return real(*a, **kw)
    monkeypatch.setattr(deps, "environment", probe)
    cs.pdk_set("p", meta["id"], "fakeA")
    assert seen and all(g == {"server": True, "changesets": True} for g in seen), seen
    cs.discard("p", meta["id"])
    shutil.rmtree(root)


def test_restore_past_a_restore_that_undid_a_pdk_set_drops_the_cached_layouts(tmp_path):
    pdks = _pdks(tmp_path / "pdks")
    root, srv, cs = _setup(tmp_path, pdks)
    meta = cs.create("p", "pdk", "", "user")
    cid, P = meta["id"], meta["preview"]
    g1 = cs.pdk_set("p", cid, "fakeB")["op"]["gseq"]
    assert srv.db(P, "l").pdk.id == _id(pdks, "fakeB")
    cs.restore("p", cid, 0)
    assert srv.db(P, "l").pdk is None
    cs.restore("p", cid, g1)                       # back past the restore: its record has no "pdk" key
    assert srv.project(P).meta["pdk"] == _id(pdks, "fakeB")
    assert srv.db(P, "l").pdk.id == _id(pdks, "fakeB")
    cs.discard("p", cid)
    shutil.rmtree(root)


def test_restore_in_a_working_copy_with_no_lock_makes_none(tmp_path):
    root, srv, cs = _setup(tmp_path)
    _no_lock(srv)
    meta = cs.create("p", "add ip", "", "user")
    W = srv.project(meta["preview"]).path
    before = _files(W)
    assert before[1] is None
    _add(cs, meta["id"], "ip", _repo(tmp_path / "ip", {"buf": SYM}), tmp_path)
    assert (W / deps.LOCK).is_file()
    cs.restore("p", meta["id"], 0)
    assert _files(W) == before                                             # no lock, nothing installed
    cs.discard("p", meta["id"])
    shutil.rmtree(root)


def test_revert_takes_back_a_lock_the_changeset_made(tmp_path):
    """Reverting a one-click Set PDK, or the first add, in a project with no lock leaves no lock (a revert keeps the
    files a changeset added, but not undine.lock)."""
    pdks = _pdks(tmp_path / "pdks")
    root, srv, cs = _setup(tmp_path, pdks)
    _no_lock(srv)
    cl = TestClient(create_app(srv, None))
    r = cl.post("/api/p/p/deps/changeset", json={"action": "pdk", "pdk": "fakeB"})
    assert r.status_code == 200, r.text
    prj = srv.project("p")
    assert prj.meta["pdk"] == _id(pdks, "fakeB") and (prj.path / deps.LOCK).is_file()
    reverted = cs.revert("p", r.json()["changeset"], "user")["reverted"]
    prj = srv.project("p")
    assert prj.meta.get("pdk") is None and not (prj.path / deps.LOCK).exists()
    assert deps.LOCK not in reverted["kept"] and not reverted.get("deps_warning")
    # the first dependency added the same way, reverted
    ip = _repo(tmp_path / "ip", {"buf": SYM})
    r = cl.post("/api/p/p/deps/changeset", json={"action": "add", "name": "ip", "git": str(ip), "subdir": "lib"})
    assert r.status_code == 200, r.text
    reverted = cs.revert("p", r.json()["changeset"], "user")["reverted"]
    prj = srv.project("p")
    assert not (prj.path / deps.LOCK).exists() and not deps.dep_dir(prj.path, "ip").exists()
    assert not reverted.get("deps_warning")
    shutil.rmtree(root)


def test_revert_refuses_a_lock_it_made_and_the_project_changed_since(tmp_path):
    root, srv, cs = _setup(tmp_path)
    _no_lock(srv)
    meta = cs.create("p", "add ip", "", "user")
    _add(cs, meta["id"], "ip", _repo(tmp_path / "ip", {"buf": SYM}), tmp_path)
    cs.apply("p", meta["id"], "user")
    prj = srv.project("p")
    deps.add(prj, "amp", str(_repo(tmp_path / "amp", {"amp": SYM})), None, "lib")      # since, in the project
    with pytest.raises(MergeConflict) as e:
        cs.revert("p", meta["id"], "user")
    assert deps.LOCK in {f["path"] for f in e.value.files}
    assert set(deps.read_lock(prj.path)["libraries"]) == {"ip", "amp"}
    shutil.rmtree(root)


def test_one_click_refusals_open_no_changeset_nor_repository(tmp_path):
    pdks = _pdks(tmp_path / "pdks")
    root, srv, cs = _setup(tmp_path, pdks)
    cl = TestClient(create_app(srv, None))
    prj = srv.project("p")
    for body, code in (({"action": "add", "name": "ip"}, 400),
                       ({"action": "add", "name": "l", "git": str(tmp_path)}, 400),     # a library of the project
                       ({"action": "add", "name": "bad/name", "git": str(tmp_path)}, 400),
                       ({"action": "pdk"}, 400),
                       ({"action": "pdk", "pdk": "nothing"}, 404),
                       ({"action": "remove", "name": "nothing"}, 400),
                       ({"action": "update", "name": "l"}, 400),
                       ({"action": "move", "name": "ip"}, 400)):
        r = cl.post("/api/p/p/deps/changeset", json=body)
        assert r.status_code == code, (body, r.text)
    assert not (prj.path / ".git").exists() and cs.list("p") == []
    shutil.rmtree(root)


def test_one_click_says_the_project_was_made_a_repository(tmp_path):
    root, srv, cs = _setup(tmp_path)
    cl = TestClient(create_app(srv, None))
    r = cl.post("/api/p/p/deps/changeset", json={"action": "add", "name": "ip",
                                                 "git": str(_repo(tmp_path / "ip", {"buf": SYM})), "subdir": "lib"})
    body = r.json()
    assert r.status_code == 200 and body["git_setup"] is True and body["first_commit"], r.text
    assert "first_commit_note" in body
    shutil.rmtree(root)
