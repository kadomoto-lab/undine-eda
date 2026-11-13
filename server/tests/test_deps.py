"""Library dependencies pinned in undine.lock: add, update with a report, revert by lock + sync, drift detection."""
import importlib.util
import json
import shutil
import subprocess
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine.store import cas, deps, gitsrc  # noqa: E402
from undine.schematics import xschem
from fastapi.testclient import TestClient  # noqa: E402
from undine.server.api import Server, create_app  # noqa: E402
from undine.store.changeset import Changesets  # noqa: E402
from undine.pdks.pdk import default_pdk_roots, discover  # noqa: E402
from undine.schematics.xschem import XComp, XFile  # noqa: E402

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


def _git(d: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


def _ip_repo(root: Path) -> Path:
    """A git repository with an inverter library in lib/, tagged v1; v2 widens the NMOS."""
    spec = importlib.util.spec_from_file_location("make_inverter", REPO / "examples" / "make_inverter.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    repo = root / "ip"
    (repo / "lib").mkdir(parents=True)
    (repo / "lib" / "inv.sch").write_text(xschem.dump(m.inv()))
    (repo / "lib" / "inv.sym").write_text(xschem.dump(m.inv_sym()))
    (repo / "README").write_text("not part of the library\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "v1")
    _git(repo, "tag", "v1")
    sch = repo / "lib" / "inv.sch"
    sch.write_text(sch.read_text().replace("W=1 ", "W=2 "))
    _git(repo, "commit", "-qam", "v2")
    _git(repo, "tag", "v2")
    return repo


def test_dependency_life_cycle():
    if not shutil.which("git"):
        return
    root = Path(tempfile.mkdtemp())
    repo = _ip_repo(root / "src")
    # the inverter places sky130_fd_pr symbols: only with a sky130 PDK does the netlist show the transistors' W
    # (under another process they don't resolve and inv netlists empty, the same for v1 and v2)
    pdks = {k: p for k, p in discover(default_pdk_roots()).items() if p.family == "sky130"}
    srv = Server(root / "projects", pdks, [root])
    prj = srv.store.create("p", next((k for k, p in pdks.items() if p.family == "sky130"), None))
    assert deps.read_lock(prj.path)["tools"].keys() == set(deps.TOOLS)        # a new project starts locked

    r = deps.add(prj, "ip", str(repo), "v1", "lib")
    assert r["cells"] == ["inv"] and r["commit"] == _git(repo, "rev-parse", "v1")
    d = deps.dep_dir(prj.path, "ip")
    assert sorted(f.name for f in d.iterdir()) == [".undine-dep.json", "inv.sch", "inv.sym"]   # subdir only, no README
    assert not (d / "inv.sch").stat().st_mode & 0o222                        # read-only
    prj = srv.project("p")
    lib = prj.libraries()["ip"]
    assert (lib.origin, lib.readonly, lib.version) == ("dep", True, f"v1 @ {r['commit'][:7]}")
    assert deps.status(prj)["ok"], deps.status(prj)["issues"]

    # a cell of the project places the dependency's symbol; its netlist comes from the dependency
    prj.create_library("mylib")
    top = XFile(version="xschem version=3.4.5 file_version=1.2\n")
    top.comps = [XComp("ip/inv.sym", 0, 0, 0, 0, {"name": "x1"})]
    (prj.path / "libs/mylib/top.sch").write_text(xschem.dump(top))
    prj = srv.project("p")
    net1 = srv.netlist_sha("p", "mylib", "top", "top", "sim")
    lock_v1, project_v1 = (prj.path / deps.LOCK).read_text(), (prj.path / "project.json").read_text()

    rep = deps.update(prj, "ip", "v2")
    assert rep["changed"] == {"added": [], "removed": [], "modified": ["inv.sch"]}
    assert rep["used_by"] == ["mylib/top (schematic) places inv"]
    assert deps.read_lock(prj.path)["libraries"]["ip"]["commit"] == _git(repo, "rev-parse", "v2")
    assert (srv.netlist_sha("p", "mylib", "top", "top", "sim") != net1) == bool(prj.pdk)   # results on it are outdated

    # reverting the lock (as `git checkout undine.lock project.json` would) and syncing brings v1 back
    (prj.path / deps.LOCK).write_text(lock_v1)
    (prj.path / "project.json").write_text(project_v1)
    prj = srv.project("p")
    assert [x["state"] for x in deps.status(prj)["libraries"] if x["name"] == "ip"] == ["other version"]
    assert deps.sync(prj) == [{"name": "ip", "action": "installed", "commit": _git(repo, "rev-parse", "v1")}]
    assert deps.status(prj)["ok"] and srv.netlist_sha("p", "mylib", "top", "top", "sim") == net1

    # a dependency edited in place is reported, and sync restores it. The file is a hard link into the store: the
    # edit damaged the store's object too, which sync removes and reads again from the kept repository
    obj = cas.object_path(cas.read_archive_tree(deps.read_lock(prj.path)["libraries"]["ip"]["sha"])["inv.sym"][1])
    assert (d / "inv.sym").stat().st_ino == obj.stat().st_ino
    (d / "inv.sym").chmod(0o644)
    (d / "inv.sym").write_text("v {}\n")
    assert not cas.check_object(obj.parent.name + obj.name)
    assert "library ip: modified" in deps.status(prj)["issues"]
    deps.sync(prj)
    assert deps.status(prj)["ok"]
    assert cas.check_object(obj.parent.name + obj.name) and (d / "inv.sym").stat().st_ino == obj.stat().st_ino

    # a changeset preview sees the dependency too
    meta = Changesets(srv).create("p", "t")
    assert "ip" in srv.project(meta["preview"]).libraries()
    assert (srv.project(meta["preview"]).libraries()["ip"].path / "inv.sym").is_file()

    deps.remove(prj, "ip")
    prj = srv.project("p")
    assert "ip" not in prj.libraries() and "ip" not in deps.read_lock(prj.path)["libraries"] and not d.exists()
    shutil.rmtree(root, ignore_errors=True)


def test_refusals_and_drift():
    if not shutil.which("git"):
        return
    root = Path(tempfile.mkdtemp())
    repo = _ip_repo(root / "src")
    srv = Server(root / "projects", {}, [root])
    prj = srv.store.create("p")
    for url, sub in [("ext::sh -c touch% /tmp/pwned", None), ("--upload-pack=x", None), ("relative/path", None),
                     (str(repo), "../escape"), (str(repo), "/abs")]:
        try:
            deps.add(prj, "x", url, None, sub)
            raise AssertionError(f"accepted {url} {sub}")
        except deps.DepError:
            pass
    try:
        deps.add(prj, "x", str(repo), "no-such-tag", "lib")
        raise AssertionError("accepted an unknown rev")
    except deps.DepError as e:
        assert "no-such-tag" in str(e)
    assert "x" not in srv.project("p").library_specs()                      # nothing half-added
    # a library linked from outside the project, an extra symbol path, no lock, another tool version
    m = json.loads((prj.path / "project.json").read_text())
    m["libraries"]["shared"] = str(root / "elsewhere")
    m["symbol_paths"] = ["/opt/symbols"]
    (prj.path / "project.json").write_text(json.dumps(m))
    lock = deps.read_lock(prj.path)
    assert set(lock) == {"format", "pdk", "tools", "libraries"}             # versions, no paths of this machine
    lock["tools"]["klayout"] = "KLayout 0.1"
    deps.write_lock(prj.path, lock)
    st = deps.status(srv.project("p"))
    issues = st["issues"]
    assert any("shared: linked" in i for i in issues) and any("/opt/symbols" in i for i in issues)
    here = deps._short(deps.tools.find("klayout"))
    kl = next(t for t in st["tools"] if t["name"] == "klayout")
    assert "locked_path" not in kl and kl["now_path"] == here
    if kl["now"]:                                    # another version is a warning, not a lock issue (format 1: no origin)
        said = f"klayout: locked 0.1, here {deps._tool_text(kl['now'])}{f' ({here})' if here else ''}"
        assert said in st["warnings"] and not any(i.startswith("klayout") for i in issues) and not kl["ok"]
    (prj.path / deps.LOCK).unlink()
    st = deps.status(srv.project("p"))
    assert any("no undine.lock" in i for i in st["issues"])
    assert all(set(t) == {"name", "locked", "now", "now_path", "ok"} for t in st["tools"])
    shutil.rmtree(root, ignore_errors=True)


def test_slow_fetch_does_not_stop_the_server():
    """A dependency's git fetch runs without the server's lock: other requests answer meanwhile, and what they write
    to project.json (a new library) is kept when the dependency is written in."""
    real = shutil.which("git")
    if not real:
        return
    root = Path(tempfile.mkdtemp())
    repo = _ip_repo(root / "src")
    srv = Server(root / "projects", {}, [root], dep_roots=[root / "src"])     # the local repository may be fetched over HTTP
    c = TestClient(create_app(srv, None))
    assert c.post("/api/projects", json={"name": "p"}).status_code == 200
    # a remote that answers a fetch only when the test lets it
    bin_ = root / "bin"
    bin_.mkdir()
    (bin_ / "git").write_text(f"""#!/bin/sh
case " $* " in *" fetch "*)
  touch {root}/fetching; i=0
  while [ ! -e {root}/go ] && [ $i -lt 600 ]; do sleep 0.05; i=$((i+1)); done;;
esac
exec {real} "$@"
""")
    (bin_ / "git").chmod(0o755)
    path = os.environ["PATH"]
    os.environ["PATH"] = f"{bin_}:{path}"
    out = {}
    try:
        t = threading.Thread(target=lambda: out.update(r=c.post("/api/p/p/deps", json={
            "name": "ip", "git": str(repo), "rev": "v1", "subdir": "lib"})))
        t.start()
        for _ in range(400):
            if (root / "fetching").exists():
                break
            time.sleep(0.05)
        assert (root / "fetching").exists(), "the fetch did not start"
        assert srv.lock.acquire(timeout=5)                                   # not held while fetching
        srv.lock.release()
        assert c.get("/api/p/p").status_code == 200
        assert c.post("/api/p/p/libs", json={"name": "mylib"}).status_code == 200      # writes project.json
        assert c.post("/api/p/p/deps/lock").status_code == 200                         # writes undine.lock
        assert t.is_alive()
        (root / "go").touch()
        t.join(30)
        assert not t.is_alive() and out["r"].status_code == 200, out["r"].text
    finally:
        (root / "go").touch()
        os.environ["PATH"] = path
    prj = srv.project("p")
    assert set(prj.library_specs()) == {"mylib", "ip"}                       # neither write lost
    assert deps.status(prj)["ok"], deps.status(prj)["issues"]
    shutil.rmtree(root, ignore_errors=True)


def test_project_files_are_written_one_writer_at_a_time():
    """meta_lock (the CLI and every request take it to write project.json / undine.lock) holds other writers off."""
    root = Path(tempfile.mkdtemp())
    srv = Server(root / "projects", {}, [root])
    prj = srv.store.create("p")
    done = threading.Event()
    with deps.meta_lock(prj.path):
        t = threading.Thread(target=lambda: (srv.project("p").create_library("a"), done.set()))
        t.start()
        assert not done.wait(0.5)                                            # waits for the lock
        m = json.loads((prj.path / "project.json").read_text())               # another writer (the CLI) meanwhile
        m["libraries"]["b"] = "libs/b"
        (prj.path / "project.json").write_text(json.dumps(m))
    t.join(10)
    assert done.is_set() and set(srv.project("p").library_specs()) == {"a", "b"}   # read again under the lock
    shutil.rmtree(root, ignore_errors=True)


def _two_tags(root: Path):
    """A server, a project p with the dependency ip at v1, the repository."""
    repo = _ip_repo(root / "src")
    srv = Server(root / "projects", {}, [root])
    srv.store.create("p")
    deps.add(srv.project("p"), "ip", str(repo), "v1", "lib")
    return srv, repo


def test_an_operation_that_waited_works_from_what_the_one_before_wrote():
    """A dependency operation reads project.json again once it holds the dependency lock: a project loaded before
    another operation ran (a request that waited behind it) neither takes back nor brings back what that one did."""
    if not shutil.which("git"):
        return
    root = Path(tempfile.mkdtemp())
    srv, repo = _two_tags(root)
    v1, v2 = _git(repo, "rev-parse", "v1"), _git(repo, "rev-parse", "v2")
    # (A) a sync loaded before an update to v2
    stale = srv.project("p")
    deps.update(srv.project("p"), "ip", "v2")
    deps.sync(stale)
    prj = srv.project("p")
    assert prj.library_specs()["ip"]["rev"] == "v2" and deps.read_lock(prj.path)["libraries"]["ip"]["commit"] == v2
    assert deps.installed(prj.path, "ip")["commit"] == v2 and deps.status(prj)["ok"]
    # (B) an update without a rev loaded before an update to v1: its rev again is v1
    stale = srv.project("p")
    deps.update(srv.project("p"), "ip", "v1")
    assert deps.update(stale, "ip")["rev"] == "v1"
    prj = srv.project("p")
    assert prj.library_specs()["ip"]["rev"] == "v1" and deps.read_lock(prj.path)["libraries"]["ip"]["commit"] == v1
    # (C) a sync loaded before a remove
    stale = srv.project("p")
    deps.remove(srv.project("p"), "ip")
    assert deps.sync(stale) == []
    prj = srv.project("p")
    assert "ip" not in prj.library_specs() and "ip" not in deps.read_lock(prj.path)["libraries"]
    assert not deps.dep_dir(prj.path, "ip").exists()
    # status tells a lock that is not what project.json names
    deps.add(prj, "ip", str(repo), "v1", "lib")
    m = json.loads((prj.path / "project.json").read_text())
    m["libraries"]["ip"]["rev"] = "v2"
    (prj.path / "project.json").write_text(json.dumps(m))
    assert "library ip: unlocked, project.json has rev v2, the lock v1: run `python -m undine lib lock p` to lock it, " \
           "or `python -m undine lib update p ip`" in deps.status(srv.project("p"))["issues"]
    shutil.rmtree(root, ignore_errors=True)


def test_what_changed_while_fetching_is_not_written_back():
    """project.json changed by another writer while a lib lock or an update fetched (a changeset adopted, a git
    command): lib lock leaves out a dependency no longer named, update refuses; neither writes the old spec back."""
    if not shutil.which("git"):
        return
    root = Path(tempfile.mkdtemp())
    srv, repo = _two_tags(root)
    prj = srv.project("p")
    real = gitsrc.fetch

    def drop_ip(*a):
        got = real(*a)
        with deps.meta_lock(prj.path):
            m = json.loads((prj.path / "project.json").read_text())
            del m["libraries"]["ip"]
            (prj.path / "project.json").write_text(json.dumps(m))
        return got
    try:
        lock = deps.read_lock(prj.path)
        del lock["libraries"]["ip"]                                          # lib lock has to fetch ip
        deps.write_lock(prj.path, lock)
        gitsrc.fetch = drop_ip
        assert deps.relock(srv.project("p"))["resolved"] == []
        assert "ip" not in deps.read_lock(prj.path)["libraries"] and "ip" not in srv.project("p").library_specs()
        gitsrc.fetch = real
        deps.add(srv.project("p"), "ip", str(repo), "v1", "lib")
        gitsrc.fetch = drop_ip
        try:
            deps.update(srv.project("p"), "ip", "v2")
            raise AssertionError("updated a dependency removed meanwhile")
        except deps.DepError as e:
            assert "changed in project.json meanwhile" in str(e)
        assert "ip" not in srv.project("p").library_specs()
        assert deps.read_lock(prj.path)["libraries"]["ip"]["commit"] == _git(repo, "rev-parse", "v1")
        assert not list((prj.path / deps.DEPS).glob(".*.new*"))                # no staging left behind
    finally:
        gitsrc.fetch = real
    shutil.rmtree(root, ignore_errors=True)


def test_sync_is_all_or_nothing(tmp_path, monkeypatch):
    """One remote unreachable: sync fails and installs none of the dependencies, the reachable one neither."""
    if not shutil.which("git"):
        return
    root = Path(tempfile.mkdtemp())
    srv, repo = _two_tags(root)
    other = root / "other"
    shutil.copytree(repo, other)
    prj = srv.project("p")
    deps.add(prj, "ip2", str(other), "v2", "lib")
    lock = (prj.path / deps.LOCK).read_text()
    for n in ("ip", "ip2"):                                                  # a fresh checkout: nothing installed
        shutil.rmtree(deps.dep_dir(prj.path, n))
    shutil.rmtree(other)
    monkeypatch.setenv("UNDINE_STORE", str(tmp_path / "empty"))              # on another machine: an empty store
    try:
        deps.sync(srv.project("p"))
        raise AssertionError("synced with a remote gone")
    except deps.DepError as e:
        assert str(e).startswith("could not fetch ip2 @ ") and "nothing was changed" in str(e), e
    assert not deps.dep_dir(prj.path, "ip").exists() and not deps.dep_dir(prj.path, "ip2").exists()
    assert (prj.path / deps.LOCK).read_text() == lock
    assert not [p.name for p in (prj.path / deps.DEPS).iterdir()]            # no staging, no lock file in deps/
    shutil.rmtree(root, ignore_errors=True)


def _drop(prj, *names: str) -> None:
    """Takes dependencies out of project.json the way a git pull or an adopted changeset does: files and lock stay."""
    m = json.loads((prj.path / "project.json").read_text())
    for n in names:
        del m["libraries"][n]
    (prj.path / "project.json").write_text(json.dumps(m))


def test_status_lists_a_dependency_gone_from_project_json_and_sync_removes_its_files_lock_entry_and_symbols():
    if not shutil.which("git"):
        return
    root = Path(tempfile.mkdtemp())
    srv, repo = _two_tags(root)
    v1, v2 = _git(repo, "rev-parse", "v1"), _git(repo, "rev-parse", "v2")
    prj = srv.project("p")
    deps.add(prj, "keep", str(repo), "v2", "lib")                           # keeps .undine/deps/ on the symbol path
    shutil.copytree(deps.dep_dir(prj.path, "keep"), deps.dep_dir(prj.path, "stray"))   # installed, never locked
    lock = deps.read_lock(prj.path)
    lock["libraries"]["ghost"] = dict(lock["libraries"]["ip"])              # locked, never installed
    deps.write_lock(prj.path, lock)
    (prj.path / deps.DEPS / ".ip.new").mkdir()                              # staging is not a dependency
    _drop(prj, "ip")
    prj = srv.project("p")
    assert xschem.SymbolResolver(prj.symbol_paths()).find("ip/inv.sym", None) is not None   # still resolves

    st = deps.status(prj)
    assert not st["ok"]
    assert [(x["name"], x["state"], x["locked"], x["installed"]) for x in st["libraries"] if x["kind"] == "dependency"] \
        == [("keep", "ok", v2, v2), ("ghost", "leftover", v1, None), ("ip", "leftover", v1, v1),
            ("stray", "leftover", None, v2)]
    sync_it = "run `python -m undine lib sync p` to remove it, or add it back to project.json"
    assert f"library ip: not in project.json, still installed in .undine/deps/ip/ and locked in undine.lock (its " \
           f"symbols still resolve here, not in a fresh checkout): {sync_it}" in st["issues"]
    assert f"library ghost: not in project.json, still locked in undine.lock: {sync_it}" in st["issues"]
    assert any(i.startswith("library stray: not in project.json, still installed in .undine/deps/stray/ (its")
               for i in st["issues"])
    assert not any(".ip.new" in i for i in st["issues"])

    assert deps.sync(prj) == [{"name": "keep", "action": "ok", "commit": v2},
                              {"name": "ghost", "action": "removed", "commit": v1},
                              {"name": "ip", "action": "removed", "commit": v1},
                              {"name": "stray", "action": "removed", "commit": v2}]
    prj = srv.project("p")
    assert set(deps.read_lock(prj.path)["libraries"]) == {"keep"}
    assert not deps.dep_dir(prj.path, "ip").exists() and not deps.dep_dir(prj.path, "stray").exists()
    assert deps.status(prj)["ok"], deps.status(prj)["issues"]
    res = xschem.SymbolResolver(prj.symbol_paths())
    assert res.find("ip/inv.sym", None) is None and res.find("keep/inv.sym", None) is not None
    shutil.rmtree(root, ignore_errors=True)


def test_a_sync_that_fails_removes_no_leftover_either():
    """All or nothing holds for removals too: one fetch fails, the leftover keeps its files and its lock entry."""
    if not shutil.which("git"):
        return
    root = Path(tempfile.mkdtemp())
    srv, repo = _two_tags(root)
    prj = srv.project("p")
    _drop(prj, "ip")
    m = json.loads((prj.path / "project.json").read_text())
    m["libraries"]["far"] = {"git": str(root / "gone"), "rev": "v1"}       # unlocked, and its remote is gone
    (prj.path / "project.json").write_text(json.dumps(m))
    lock = (prj.path / deps.LOCK).read_text()
    try:
        deps.sync(srv.project("p"))
        raise AssertionError("synced a dependency that is not locked")
    except deps.DepError as e:                                               # frozen: nothing resolved, nothing removed
        assert str(e) == "far: not in undine.lock: run `python -m undine lib lock p` to resolve it, or " \
                         "`python -m undine lib update p far`; nothing was changed", e
    assert (prj.path / deps.LOCK).read_text() == lock and deps.dep_dir(prj.path, "ip").is_dir()
    _drop(prj, "far")
    assert [x["action"] for x in deps.sync(srv.project("p"))] == ["removed"]
    assert not deps.dep_dir(prj.path, "ip").exists()
    shutil.rmtree(root, ignore_errors=True)


def test_the_cli_and_http_report_a_leftover_and_sync_removes_it():
    if not shutil.which("git"):
        return
    import contextlib
    import io
    from undine import __main__ as cli
    root = Path(tempfile.mkdtemp())
    srv, repo = _two_tags(root)
    v1 = _git(repo, "rev-parse", "v1")
    prj = srv.project("p")
    _drop(prj, "ip")
    c = TestClient(create_app(srv, None))
    r = c.get("/api/p/p/deps").json()
    assert not r["ok"] and [x["state"] for x in r["libraries"] if x["name"] == "ip"] == ["leftover"]

    def run(*argv):
        out, argv0 = io.StringIO(), sys.argv
        sys.argv = ["undine", "--root", str(root / "projects"), *argv]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                try:
                    cli.cli()
                    code = 0
                except SystemExit as e:
                    code = e.code or 0
        finally:
            sys.argv = argv0
        return code, out.getvalue()
    code, text = run("lib", "status", "p")
    assert code == 1 and "! library ip: not in project.json" in text, text
    code, text = run("lib", "sync", "p")
    assert code == 0 and f"ip: removed {v1[:12]}" in text and "!" not in text, text       # no issue left
    assert not deps.dep_dir(prj.path, "ip").exists() and "ip" not in deps.read_lock(prj.path)["libraries"]
    deps.add(srv.project("p"), "ip", str(repo), "v1", "lib")
    _drop(prj, "ip")
    assert c.post("/api/p/p/deps/sync", json={}).json() == {"libraries": [{"name": "ip", "action": "removed",
                                                                            "commit": v1}]}
    assert c.get("/api/p/p/deps").json()["ok"]
    shutil.rmtree(root, ignore_errors=True)


def test_meta_lock_holds_across_processes_and_a_rename():
    """meta_lock is a file of its own: a project.json renamed in (git, a changeset's adopt) does not let a second
    writer in, nor does another process (the CLI) get it while the server holds it."""
    root = Path(tempfile.mkdtemp())
    srv = Server(root / "projects", {}, [root])
    prj = srv.store.create("p")
    probe = ("import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; from undine.store import deps\n"
             "with deps.meta_lock(Path(sys.argv[2])): print('got')")
    args = [sys.executable, "-c", probe, str(Path(__file__).resolve().parents[1]), str(prj.path)]
    with deps.meta_lock(prj.path):
        new = prj.path / ".project.json.new"
        new.write_text((prj.path / "project.json").read_text())
        os.replace(new, prj.path / "project.json")
        try:
            subprocess.run(args, capture_output=True, text=True, timeout=1.5)
            raise AssertionError("another process got the lock")
        except subprocess.TimeoutExpired:
            pass
    assert subprocess.run(args, capture_output=True, text=True, timeout=30).stdout.strip() == "got"
    assert (prj.path / ".undine" / "project.lock").is_file()
    shutil.rmtree(root, ignore_errors=True)


def test_an_adopt_waits_for_a_writer_of_project_json():
    """Adopting a changeset that changes project.json takes meta_lock around its write: a lib command (the CLI)
    holding it is not overwritten, the adopt merges with what it wrote."""
    if not shutil.which("git"):
        return
    root = Path(tempfile.mkdtemp())
    srv = Server(root / "projects", {}, [root])
    srv.store.create("p")
    cs = Changesets(srv)
    meta = cs.create("p", "a library")
    srv.project(meta["preview"]).create_library("x")
    out = {}
    with deps.meta_lock(srv.project("p").path):
        t = threading.Thread(target=lambda: out.update(r=cs.apply("p", meta["id"], "user")))
        t.start()
        t.join(1.0)
        assert t.is_alive()                                                  # waits for the lock
    t.join(60)
    assert not t.is_alive() and "applied" in out.get("r", {}), out
    assert "x" in srv.project("p").library_specs()
    shutil.rmtree(root, ignore_errors=True)


def _newer_lock_project(root: Path):
    """A project on PDK A with the dependency ip at v1, whose lock a newer Undine has rewritten as format 3."""
    from undine.pdks.pdk import Pdk
    repo = _ip_repo(root / "src")
    pdks = {p.id: p for p in (Pdk("fakeA", "fake", "a" * 40, root / "a"), Pdk("fakeB", "fake", "b" * 40, root / "b"))}
    srv = Server(root / "projects", pdks, [root])
    prj = srv.store.create("p", "fakeA")
    deps.add(prj, "ip", str(repo), "v1", "lib")
    lock = deps.read_lock(prj.path)
    lock["format"] = 3
    lock["pdk"] = {"name": "fakeA", "commit": "a" * 40, "tree": "c" * 64}             # what format 3 may hold
    deps.write_lock(prj.path, lock)
    return srv, repo


def _files(prj) -> dict[str, bytes]:
    """The bytes of everything a dependency operation may write: project.json, the lock, the installed dependency."""
    out = {n: (prj.path / n).read_bytes() for n in ("project.json", deps.LOCK)}
    d = prj.path / deps.DEPS
    out.update({f.relative_to(d).as_posix(): f.read_bytes() for f in sorted(d.rglob("*")) if f.is_file()})
    return out


def test_a_lock_of_a_newer_format_is_refused_by_every_operation_and_left_byte_for_byte():
    if not shutil.which("git"):
        return
    root = Path(tempfile.mkdtemp())
    srv, repo = _newer_lock_project(root)
    prj = srv.project("p")
    before = _files(prj)
    for what, op in [("status", lambda: deps.status(prj)), ("sync", lambda: deps.sync(prj)),
                     ("add", lambda: deps.add(prj, "ip2", str(repo), "v1", "lib")),
                     ("update", lambda: deps.update(prj, "ip", "v2")), ("remove", lambda: deps.remove(prj, "ip")),
                     ("lock", lambda: deps.relock(prj)), ("set-pdk", lambda: prj.set_pdk("fakeB"))]:
        try:
            op()
            raise AssertionError(f"{what} accepted a format 3 lock")
        except deps.DepError as e:
            assert "format 3, made by a newer Undine" in str(e) and "update Undine" in str(e), (what, e)
            assert "reads formats 1 and 2" in str(e), (what, e)
        assert _files(srv.project("p")) == before, what                     # nothing written, nothing left staged
    shutil.rmtree(root, ignore_errors=True)


def test_a_lock_of_a_newer_format_is_refused_over_http_and_by_the_cli():
    if not shutil.which("git"):
        return
    import contextlib
    import io
    from undine import __main__ as cli
    root = Path(tempfile.mkdtemp())
    srv, repo = _newer_lock_project(root)
    prj = srv.project("p")
    before = _files(prj)
    c = TestClient(create_app(srv, None))
    assert c.get("/api/p/p").status_code == 200                              # the project still opens
    for path, body in [("deps", None), ("deps", {"name": "ip2", "git": str(repo), "rev": "v1", "subdir": "lib"}),
                       ("deps/sync", {}), ("deps/lock", {}), ("deps/ip/update", {"rev": "v2"}),
                       ("deps/ip/remove", {}), ("pdk", {"pdk": "fakeB"})]:
        r = c.get(f"/api/p/p/{path}") if body is None else c.post(f"/api/p/p/{path}", json=body)
        assert r.status_code == 400 and "made by a newer Undine" in r.json()["detail"], (path, r.text)
        assert _files(prj) == before, path
    discover, argv0 = cli.discover, sys.argv
    cli.discover = lambda roots: srv.store.pdks                              # the fake PDKs, for set-pdk
    try:
        for argv in [["lib", "status", "p"], ["lib", "sync", "p"], ["lib", "lock", "p"], ["lib", "remove", "p", "ip"],
                     ["lib", "update", "p", "ip", "--rev", "v2"], ["lib", "add", "p", "ip2", str(repo), "--rev", "v1"],
                     ["set-pdk", "p", "fakeB"]]:
            out = io.StringIO()
            sys.argv = ["undine", "--root", str(root / "projects"), *argv]
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                try:
                    cli.cli()
                    code = 0
                except SystemExit as e:
                    code = e.code
            assert code == 1 and "made by a newer Undine" in out.getvalue(), (argv, code, out.getvalue())
            assert _files(prj) == before, argv
    finally:
        cli.discover, sys.argv = discover, argv0
    shutil.rmtree(root, ignore_errors=True)


def test_a_lock_without_format_is_read_as_format_1_and_a_broken_one_is_refused():
    root = Path(tempfile.mkdtemp())
    srv = Server(root / "projects", {}, [root])
    prj = srv.store.create("p")
    lock = deps.read_lock(prj.path)
    assert lock["format"] == deps.FORMAT == 1                                # what the commands of today write
    assert deps.READS == (1, 2) and deps.NEWEST == 2                         # and what this Undine reads
    del lock["format"]
    deps.write_lock(prj.path, lock)                                          # as written by hand
    assert deps.status(prj)["pdk"]["ok"]
    deps.relock(prj)
    assert json.loads((prj.path / deps.LOCK).read_text())["format"] == 1      # the field written back
    for text, says in [('{"format": "1"}', "which no Undine writes"), ('{"format": true}', "which no Undine writes"),
                       ('{"format": 0}', "which no Undine writes"), ('{"format": 1.0}', "which no Undine writes"),
                       ("[1]", "is not a lock"),
                       ('{"format": 1,', "is not valid JSON")]:
        (prj.path / deps.LOCK).write_text(text)
        try:
            deps.status(prj)
            raise AssertionError(f"read {text}")
        except deps.DepError as e:
            assert says in str(e) and "nothing was changed" in str(e), (text, e)
        try:
            deps.relock(prj)
            raise AssertionError(f"rewrote {text}")
        except deps.DepError:
            pass
        assert (prj.path / deps.LOCK).read_text() == text
    shutil.rmtree(root, ignore_errors=True)


def _two_pdk_project(root: Path):
    """A project on the fake PDK A, with B to move to."""
    from undine.pdks.pdk import Pdk
    pdks = {p.id: p for p in (Pdk("fakeA", "fake", "a" * 40, root / "a"), Pdk("fakeB", "fake", "b" * 40, root / "b"))}
    srv = Server(root / "projects", pdks, [root])
    return srv, srv.store.create("p", "fakeA")


def test_set_pdk_writes_the_lock_whether_or_not_the_project_has_one():
    root = Path(tempfile.mkdtemp())
    srv, prj = _two_pdk_project(root)
    lock = deps.read_lock(prj.path)
    lock["libraries"]["ip"] = {"git": "https://example.org/ip.git", "commit": "1" * 40, "sha": "2" * 64}
    lock["tools"]["klayout"] = "KLayout 0.0.1"                               # what another machine recorded
    deps.write_lock(prj.path, lock)
    assert prj.set_pdk("fakeB") == ("fakeA@aaaaaaaaaaaa", "fakeB@bbbbbbbbbbbb")
    assert deps.read_lock(prj.path) == {**lock, "pdk": "fakeB@bbbbbbbbbbbb"}  # only the pin moved
    (prj.path / deps.LOCK).unlink()                                          # a project made before locks
    assert prj.set_pdk("fakeA") == ("fakeB@bbbbbbbbbbbb", "fakeA@aaaaaaaaaaaa")
    made = deps.read_lock(prj.path)
    assert made == {"format": 1, **deps.environment(prj.pdk), "libraries": {}}   # as `lib lock` makes one
    assert made["pdk"] == srv.store.get("p").meta["pdk"] == "fakeA@aaaaaaaaaaaa" and deps.status(prj)["pdk"]["ok"]
    (prj.path / deps.LOCK).unlink()
    history = srv.store.get("p").meta["pdk_history"]
    assert prj.set_pdk("fakeA") == ("fakeA@aaaaaaaaaaaa", "fakeA@aaaaaaaaaaaa")    # the same PDK: the lock still
    assert deps.read_lock(prj.path)["pdk"] == "fakeA@aaaaaaaaaaaa"
    assert srv.store.get("p").meta["pdk_history"] == history                 # not a move
    deps.write_lock(prj.path, {**deps.read_lock(prj.path), "pdk": "fakeB@bbbbbbbbbbbb"})
    prj.set_pdk("fakeA")                                                     # a stale pin is written, too
    assert deps.read_lock(prj.path)["pdk"] == "fakeA@aaaaaaaaaaaa"
    for text in ['{"format": 1,', '{"format": "1"}']:                        # refused, not replaced by a new lock
        (prj.path / deps.LOCK).write_text(text)
        before = (prj.path / "project.json").read_bytes()
        try:
            prj.set_pdk("fakeB")
            raise AssertionError(f"set-pdk over {text}")
        except deps.DepError as e:
            assert "nothing was changed" in str(e), e
        assert (prj.path / deps.LOCK).read_text() == text and (prj.path / "project.json").read_bytes() == before
    shutil.rmtree(root, ignore_errors=True)


def test_set_pdk_over_http_and_from_the_cli_creates_a_missing_lock():
    import contextlib
    import io
    from undine import __main__ as cli
    root = Path(tempfile.mkdtemp())
    srv, prj = _two_pdk_project(root)
    (prj.path / deps.LOCK).unlink()
    c = TestClient(create_app(srv, None))
    r = c.post("/api/p/p/pdk", json={"pdk": "fakeB"})
    assert r.status_code == 200 and r.json() == {"from": "fakeA@aaaaaaaaaaaa", "to": "fakeB@bbbbbbbbbbbb"}, r.text
    assert deps.read_lock(prj.path)["pdk"] == "fakeB@bbbbbbbbbbbb"
    (prj.path / deps.LOCK).unlink()
    discover, argv0 = cli.discover, sys.argv
    cli.discover = lambda roots: srv.store.pdks
    try:
        sys.argv = ["undine", "--root", str(root / "projects"), "set-pdk", "p", "fakeA"]
        with contextlib.redirect_stdout(io.StringIO()):
            cli.cli()
    finally:
        cli.discover, sys.argv = discover, argv0
    assert deps.read_lock(prj.path)["pdk"] == "fakeA@aaaaaaaaaaaa" == srv.store.get("p").meta["pdk"]
    shutil.rmtree(root, ignore_errors=True)


def _run_cli(root: Path, *argv: str) -> tuple[int, str]:
    import contextlib
    import io
    from undine import __main__ as cli
    out, argv0 = io.StringIO(), sys.argv
    # an empty PDK root, so the CLI never scans this machine's ~/.ciel and ~/.volare
    sys.argv = ["undine", "--root", str(root / "projects"), "--pdk-root", str(root / "no-pdks"), *argv]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                cli.cli()
                code = 0
            except SystemExit as e:
                code = e.code or 0
    finally:
        sys.argv = argv0
    return code, out.getvalue()


def test_a_local_repository_is_refused_over_http_without_dep_root_and_the_cli_still_takes_it():
    """A browser must not read any repository on the server's disk: from HTTP a file://, /... or ~/... URL is refused
    when the server has no --dep-root, naming the option, and nothing is written. The CLI takes the same paths."""
    if not shutil.which("git"):
        return
    root = Path(tempfile.mkdtemp())
    repo = _ip_repo(root / "src")
    srv = Server(root / "projects", {}, [root])                  # --import-root does not open local dependencies
    prj = srv.store.create("p")
    before = _files(prj)
    c = TestClient(create_app(srv, None))
    home = Path.home()
    urls = [str(repo), f"file://{repo}", f"file://localhost{repo}"]
    if repo.is_relative_to(home):
        urls.append("~/" + repo.relative_to(home).as_posix())
    for url in urls:
        r = c.post("/api/p/p/deps", json={"name": "ip", "git": url, "rev": "v1", "subdir": "lib"})
        assert r.status_code == 400 and "--dep-root" in r.json()["detail"] and "started without" in r.json()["detail"], (url, r.text)
        assert "python -m undine lib add p" in r.json()["detail"]
        assert _files(prj) == before, url
    assert not any((prj.path / deps.DEPS).glob(".ip.new*"))     # refused before fetching: no staging left
    code, text = _run_cli(root, "lib", "add", "p", "ip", f"file://{repo}", "--rev", "v1", "--subdir", "lib")
    assert code == 0 and "ip:" in text, text
    assert deps.read_lock(prj.path)["libraries"]["ip"]["commit"] == _git(repo, "rev-parse", "v1")
    code, text = _run_cli(root, "lib", "update", "p", "ip", "--rev", "v2")
    assert code == 0, text
    # over HTTP, update fetches from the URL project.json names: refused there too
    r = c.post("/api/p/p/deps/ip/update", json={"rev": "v1"})
    assert r.status_code == 400 and "--dep-root" in r.json()["detail"], r.text
    assert deps.read_lock(prj.path)["libraries"]["ip"]["commit"] == _git(repo, "rev-parse", "v2")
    shutil.rmtree(root, ignore_errors=True)


def test_a_local_repository_under_a_dep_root_is_added_updated_and_synced_over_http():
    if not shutil.which("git"):
        return
    root = Path(tempfile.mkdtemp())
    repo = _ip_repo(root / "src" / "a b")                        # a space: %20 in the file:// URL
    srv = Server(root / "projects", {}, [root], dep_roots=[root / "src"])
    prj = srv.store.create("p")
    c = TestClient(create_app(srv, None))
    url = "file://" + str(repo).replace(" ", "%20")
    r = c.post("/api/p/p/deps", json={"name": "ip", "git": url, "rev": "v1", "subdir": "lib"})
    assert r.status_code == 200 and r.json()["cells"] == ["inv"], r.text
    assert srv.project("p").library_specs()["ip"]["git"] == url              # project.json keeps what was asked for
    r = c.post("/api/p/p/deps", json={"name": "ip2", "git": str(repo), "rev": "v1", "subdir": "lib"})
    assert r.status_code == 200, r.text
    r = c.post("/api/p/p/deps/ip/update", json={"rev": "v2"})
    assert r.status_code == 200 and r.json()["to"] == _git(repo, "rev-parse", "v2"), r.text
    shutil.rmtree(deps.dep_dir(prj.path, "ip"))
    r = c.post("/api/p/p/deps/sync", json={})
    assert r.status_code == 200 and {x["name"]: x["action"] for x in r.json()["libraries"]} == \
        {"ip": "installed", "ip2": "ok"}, r.text
    assert c.get("/api/p/p/deps").json()["ok"]
    shutil.rmtree(root, ignore_errors=True)


def test_a_path_that_escapes_the_dep_root_by_dotdot_or_a_symlink_is_refused_over_http():
    """The real path is checked (symlinks and .. resolved), and git is given that real path, so what git reads is
    the directory checked; a file:// URL naming another host, or with a query, is refused rather than guessed at."""
    if not shutil.which("git"):
        return
    root = Path(tempfile.mkdtemp())
    repo = _ip_repo(root / "src")
    allowed = root / "allowed"
    allowed.mkdir()
    (allowed / "link").symlink_to(repo)
    srv = Server(root / "projects", {}, [root], dep_roots=[allowed])
    prj = srv.store.create("p")
    before = _files(prj)
    c = TestClient(create_app(srv, None))
    for url, says in [(f"{allowed}/../src/ip", "is outside the directories this server fetches local repositories "
                                               f"from ({allowed}; --dep-root)"),
                      (f"file://{allowed}/../src/ip", "--dep-root"),
                      (f"file://{allowed}/%2E%2E/src/ip", "--dep-root"),
                      (str(allowed / "link"), f"{repo} is outside"),
                      (f"file://{allowed}/link", f"{repo} is outside"),
                      (f"file://otherhost{allowed}/link", "not a plain file:// URL"),
                      (f"file://{allowed}/link?x=1", "not a plain file:// URL")]:
        r = c.post("/api/p/p/deps", json={"name": "ip", "git": url, "rev": "v1", "subdir": "lib"})
        assert r.status_code == 400 and says in r.json()["detail"], (url, r.text)
        assert _files(prj) == before, url
    shutil.rmtree(root, ignore_errors=True)


def test_sync_over_http_fetches_a_local_path_project_json_names_only_under_a_dep_root(tmp_path, monkeypatch):
    """A project.json from a clone or an adopted changeset may name any local path: sync over HTTP checks it as add
    does before it fetches anything, all or nothing. A dependency already installed as locked is not fetched, so a
    project set up from the CLI still syncs from the browser. (One whose tree is in the store reads no repository
    either: test_store_sync.py.)"""
    if not shutil.which("git"):
        return
    root = Path(tempfile.mkdtemp())
    srv, repo = _two_tags(root)                                   # ip from a local path, added from the CLI side
    prj = srv.project("p")
    other = _ip_repo(root / "elsewhere")
    deps.add(prj, "remote", f"file://{other}", "v1", "lib")
    c = TestClient(create_app(srv, None))
    r = c.post("/api/p/p/deps/sync", json={})
    assert r.status_code == 200 and [x["action"] for x in r.json()["libraries"]] == ["ok", "ok"], r.text
    shutil.rmtree(deps.dep_dir(prj.path, "ip"))                   # a fresh clone: nothing installed
    shutil.rmtree(deps.dep_dir(prj.path, "remote"))
    monkeypatch.setenv("UNDINE_STORE", str(tmp_path / "empty"))  # on another machine: git has to be read
    before = _files(prj)
    srv.dep_roots = [(root / "src").resolve()]                    # ip's repository may be read, remote's may not
    r = c.post("/api/p/p/deps/sync", json={})
    assert r.status_code == 400 and f"{other.resolve()} is outside" in r.json()["detail"] \
        and "python -m undine lib sync p" in r.json()["detail"], r.text
    assert _files(prj) == before and not deps.dep_dir(prj.path, "ip").exists()      # all or nothing: ip not either
    srv.dep_roots.append((root / "elsewhere").resolve())
    r = c.post("/api/p/p/deps/sync", json={})
    assert r.status_code == 200 and [x["action"] for x in r.json()["libraries"]] == ["installed", "installed"], r.text
    shutil.rmtree(root, ignore_errors=True)


def test_serve_dep_root_is_repeatable_and_off_by_default():
    import undine.server.api as api
    seen = {}

    class Stop(Exception):
        pass

    def fake_server(root, pdks, import_roots, llm_policy=None, mcp=False, dep_roots=None, pdk_roots=None):
        seen["dep_roots"] = dep_roots
        raise Stop
    root = Path(tempfile.mkdtemp())
    real = api.Server
    api.Server = fake_server
    try:
        for extra, want in [([], []), (["--dep-root", "/a", "--dep-root", "/b"], [Path("/a"), Path("/b")])]:
            try:
                _run_cli(root, "serve", "--port", "0", *extra)
            except Stop:
                pass
            assert seen.pop("dep_roots") == want, extra
    finally:
        api.Server = real
    shutil.rmtree(root, ignore_errors=True)


def test_status_reports_a_leftovers_installed_commit_from_its_files_not_from_the_lock():
    """A git pull drops ip from project.json and moves its lock entry back to v1, while v2 is installed: status says
    v2 is installed and v1 locked, and sync says it removed v2's files."""
    if not shutil.which("git"):
        return
    root = Path(tempfile.mkdtemp())
    srv, repo = _two_tags(root)
    v1, v2 = _git(repo, "rev-parse", "v1"), _git(repo, "rev-parse", "v2")
    deps.update(srv.project("p"), "ip", "v2")
    prj = srv.project("p")
    lock = deps.read_lock(prj.path)
    lock["libraries"]["ip"]["commit"] = v1
    deps.write_lock(prj.path, lock)
    _drop(prj, "ip")
    st = deps.status(srv.project("p"))
    assert [(x["state"], x["installed"], x["locked"]) for x in st["libraries"] if x["name"] == "ip"] == \
        [("leftover", v2, v1)]
    assert deps.sync(srv.project("p")) == [{"name": "ip", "action": "removed", "commit": v2}]
    shutil.rmtree(root, ignore_errors=True)


def test_the_netlist_hash_changes_when_sync_removes_a_leftover_a_testbench_places():
    """While another dependency is declared, a leftover still resolves through .undine/deps/: removing it changes what
    a testbench placing its symbol netlists to, so the cached hash must not stay."""
    if not shutil.which("git"):
        return
    root = Path(tempfile.mkdtemp())
    srv, repo = _two_tags(root)
    prj = srv.project("p")
    deps.add(prj, "keep", str(repo), "v2", "lib")
    prj.create_library("mylib")
    top = XFile(version="xschem version=3.4.5 file_version=1.2\n")
    top.comps = [XComp("ip/inv.sym", 0, 0, 0, 0, {"name": "x1"})]
    (prj.path / "libs/mylib/top.sch").write_text(xschem.dump(top))
    _drop(prj, "ip")
    before = srv.netlist_sha("p", "mylib", "top", "top", "sim")             # ip/inv still resolves here
    assert [x["action"] for x in deps.sync(srv.project("p"))] == ["ok", "removed"]
    try:
        after = srv.netlist_sha("p", "mylib", "top", "top", "sim")
    except ValueError:
        after = None
    assert after != before
    shutil.rmtree(root, ignore_errors=True)


def test_a_rev_or_a_lock_commit_that_git_would_read_as_an_option_is_refused_before_git_runs():
    """git fetch takes options among its arguments: a rev or a locked commit `--upload-pack=<command>` would run that
    command. Refused by add, update and sync, from the CLI side and over HTTP, and nothing is written."""
    if not shutil.which("git"):
        return
    root = Path(tempfile.mkdtemp())
    srv, repo = _two_tags(root)
    srv.dep_roots = [(root / "src").resolve()]
    prj = srv.project("p")
    pwned = root / "PWNED"
    evil = f"--upload-pack=touch {pwned}; git-upload-pack"
    before = _files(prj)
    c = TestClient(create_app(srv, None))
    for call in (lambda: deps.add(prj, "x", str(repo), evil, "lib"), lambda: deps.update(prj, "ip", evil),
                 lambda: deps.add(prj, "x", str(repo), "-v1", "lib")):
        try:
            call()
            raise AssertionError("accepted a rev that starts with -")
        except deps.DepError as e:
            assert "not a branch, tag or commit" in str(e), e
    r = c.post("/api/p/p/deps", json={"name": "x", "git": str(repo), "rev": evil, "subdir": "lib"})
    assert r.status_code == 400 and "not a branch, tag or commit" in r.json()["detail"], r.text
    r = c.post("/api/p/p/deps/ip/update", json={"rev": evil})
    assert r.status_code == 400 and "not a branch, tag or commit" in r.json()["detail"], r.text
    assert _files(prj) == before
    # project.json's rev (a clone, an adopted changeset), then the lock's commit
    shutil.rmtree(deps.dep_dir(prj.path, "ip"))                              # so that sync fetches
    m = json.loads((prj.path / "project.json").read_text())
    m["libraries"]["ip"]["rev"] = evil
    (prj.path / "project.json").write_text(json.dumps(m))
    before = _files(prj)
    for sync in (lambda: deps.sync(srv.project("p")), lambda: deps.sync(srv.project("p"), local_roots=srv.dep_roots)):
        try:
            sync()
            raise AssertionError("synced a rev that starts with -")
        except deps.DepError as e:
            assert "not a branch, tag or commit" in str(e), e
    m["libraries"]["ip"]["rev"] = "v1"
    (prj.path / "project.json").write_text(json.dumps(m))
    lock = deps.read_lock(prj.path)
    lock["libraries"]["ip"]["commit"] = evil
    deps.write_lock(prj.path, lock)
    before = _files(prj)
    try:
        deps.sync(srv.project("p"))
        raise AssertionError("synced a lock commit that is not a commit id")
    except deps.DepError as e:
        assert f"ip: undine.lock locks it at {evil!r}, which is not a full commit id" in str(e), e
    r = c.post("/api/p/p/deps/sync", json={})
    assert r.status_code == 400 and "not a full commit id" in r.json()["detail"], r.text
    assert _files(prj) == before and not pwned.exists()
    assert not any((prj.path / deps.DEPS).glob(".*.new*"))                   # refused before fetching
    shutil.rmtree(root, ignore_errors=True)


def test_a_subdir_that_looks_like_a_git_option_is_refused_before_anything_is_fetched():
    """git archive takes options among its arguments too: subdir `--output=<file>` would write that file anywhere."""
    if not shutil.which("git"):
        return
    root = Path(tempfile.mkdtemp())
    srv, repo = _two_tags(root)
    prj = srv.project("p")
    target = root / "victim"
    target.write_text("keep\n")
    c = TestClient(create_app(srv, None))
    for sub in (f"--output={target}", f"lib/--output={target}", "-o"):
        try:
            deps.add(prj, "x", str(repo), "v1", sub)
            raise AssertionError(f"accepted subdir {sub}")
        except deps.DepError as e:
            assert "subdir must be a relative path inside the repository" in str(e), e
        r = c.post("/api/p/p/deps", json={"name": "x", "git": "https://example.invalid/r.git", "subdir": sub})
        assert r.status_code == 400 and "subdir must be" in r.json()["detail"], r.text
    shutil.rmtree(deps.dep_dir(prj.path, "ip"))
    m = json.loads((prj.path / "project.json").read_text())
    m["libraries"]["ip"]["subdir"] = f"--output={target}"
    (prj.path / "project.json").write_text(json.dumps(m))
    try:
        deps.sync(srv.project("p"))
        raise AssertionError("synced a subdir that starts with -")
    except deps.DepError as e:
        assert "subdir must be" in str(e), e
    assert target.read_text() == "keep\n"
    shutil.rmtree(root, ignore_errors=True)


def test_sync_never_removes_a_directory_a_lock_key_names_outside_the_deps_folder():
    """undine.lock comes from a git pull or a clone: a key that is an absolute path or ../x is not a library name.
    status reports it, sync leaves it (and the directory it names) alone; a project.json dependency of such a name is
    refused before anything is fetched or removed."""
    root = Path(tempfile.mkdtemp())
    srv = Server(root / "projects", {}, [root])
    prj = srv.store.create("p")
    outside = root / "work"
    (outside / "keep").mkdir(parents=True)
    (prj.path / ".undine" / "x").mkdir(parents=True)
    lock = deps.read_lock(prj.path)
    lock["libraries"] = {str(outside): {"commit": "0"}, "../x": {"commit": "0"}}
    deps.write_lock(prj.path, lock)
    st = deps.status(srv.project("p"))
    assert not st["ok"] and not [x for x in st["libraries"] if x["kind"] == "dependency"]
    assert f"undine.lock: libraries has an entry {str(outside)!r}, which is not a library name: delete that entry " \
           "(sync leaves it alone), or restore the lock from git" in st["issues"]
    assert any("'../x'" in i for i in st["issues"])
    assert deps.sync(srv.project("p")) == []
    c = TestClient(create_app(srv, None))
    assert c.post("/api/p/p/deps/sync", json={}).json() == {"libraries": []}
    assert (outside / "keep").is_dir() and (prj.path / ".undine" / "x").is_dir()
    assert set(deps.read_lock(prj.path)["libraries"]) == {str(outside), "../x"}
    # the same names in project.json: refused, nothing removed
    m = json.loads((prj.path / "project.json").read_text())
    m["libraries"]["../x"] = {"git": "https://example.invalid/r.git"}
    (prj.path / "project.json").write_text(json.dumps(m))
    for call in (lambda: deps.sync(srv.project("p")), lambda: deps.remove(srv.project("p"), "../x")):
        try:
            call()
            raise AssertionError("took ../x for a library name")
        except deps.DepError as e:
            assert "'../x' is not a library name" in str(e), e
    assert (prj.path / ".undine" / "x").is_dir() and "../x" in json.loads((prj.path / "project.json").read_text())["libraries"]
    shutil.rmtree(root, ignore_errors=True)


def test_a_path_whose_git_directory_is_outside_the_dep_root_is_refused_over_http():
    """git reads <path>.git when <path> does not exist, and follows a .git file or symlink: a symlink x.git (which
    any repository checked out under the root may hold) or a work tree whose .git points out of the root would read
    a repository outside it. Only an existing directory whose git directory is under a root is fetched from."""
    if not shutil.which("git"):
        return
    root = Path(tempfile.mkdtemp())
    outside = _ip_repo(root / "private")
    allowed = root / "allowed"
    allowed.mkdir()
    (allowed / "x.git").symlink_to(outside)
    (allowed / "w").mkdir()
    (allowed / "w" / ".git").symlink_to(outside / ".git")
    (allowed / "f").mkdir()
    (allowed / "f" / ".git").write_text(f"gitdir: {outside / '.git'}\n")
    (allowed / "plain").mkdir()
    srv = Server(root / "projects", {}, [root], dep_roots=[allowed])
    prj = srv.store.create("p")
    before = _files(prj)
    c = TestClient(create_app(srv, None))
    for url, says in [(str(allowed / "x"), f"{allowed / 'x'} is not a directory"),
                      (f"file://{allowed}/x", "is not a directory"),
                      (str(allowed / "w"), f"{(outside / '.git').resolve()} is outside"),
                      (str(allowed / "f"), f"{(outside / '.git').resolve()} is outside"),
                      (str(allowed / "plain"), "is not a git repository")]:
        r = c.post("/api/p/p/deps", json={"name": "ip", "git": url, "rev": "v1", "subdir": "lib"})
        assert r.status_code == 400 and says in r.json()["detail"], (url, r.text)
        assert _files(prj) == before, url
    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
