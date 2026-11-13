"""Dependencies on the content-addressed store (store/deps.py over store/cas.py and store/gitsrc.py): sync is frozen,
works offline from the store, imports installs made before the store, installs by hard links, and holds the store's
shared lock from the first read of git until the lock is written, so `store gc` never drops a tree in between.

The tools are fixed (as in test_lock_format2.py), so nothing runs a real tool; git is real."""
import copy
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine import tools  # noqa: E402
from undine.store import cas, deps, gitsrc  # noqa: E402
from undine.store.project import ProjectStore  # noqa: E402
from test_gitsrc import _phase0_sha  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

NOW = {"undine": "0.1.0 (abc1234)", "pixi_lock": None, "klayout": {"version": "0.30.12", "from": "pixi"},
       "xschem": None, "ngspice": None, "magic": None, "netgen": None}
BANNERS = {"klayout": "KLayout 0.30.12", "xschem": None, "ngspice": None, "magic": None, "netgen": None}


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


@pytest.fixture(autouse=True)
def _tools_here(monkeypatch):
    monkeypatch.setattr(tools, "tools_record", lambda: copy.deepcopy(NOW))
    monkeypatch.setattr(tools, "version", lambda n: BANNERS[n])
    monkeypatch.setattr(tools, "find", lambda n: f"/opt/t/bin/{n}" if NOW[n] else None)
    monkeypatch.delenv("UNDINE_OFFLINE", raising=False)
    monkeypatch.delenv("UNDINE_STORE_LINK", raising=False)


def _git(d: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


def _repo(d: Path) -> Path:
    """lib/ with two cells, tagged v1; v2 changes one of them and keeps the other."""
    (d / "lib" / "sub").mkdir(parents=True)
    (d / "lib" / "keep.sym").write_text("v {}\nK {type=subcircuit}\n")
    (d / "lib" / "change.sch").write_text("v {}\nC {res.sym} 0 0 0 0 {name=R1 value=1k}\n")
    (d / "lib" / "sub" / "notes.txt").write_text("one level down\n")
    (d / "README").write_text("not part of the library\n")
    _git(d, "init", "-q")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "v1")
    _git(d, "tag", "v1")
    (d / "lib" / "change.sch").write_text("v {}\nC {res.sym} 0 0 0 0 {name=R1 value=2k}\n")
    _git(d, "commit", "-qam", "v2")
    _git(d, "tag", "v2")
    return d


def _project(tmp_path: Path, fmt: int = 1):
    store = ProjectStore(tmp_path / "projects", {})
    prj = store.create("p")
    if fmt == 2:
        deps.write_lock(prj.path, {**deps.read_lock(prj.path), "format": 2, "pdk": None, "tools": copy.deepcopy(NOW)})
    return store, prj


def _files(prj) -> dict[str, bytes]:
    """project.json, the lock and everything under .undine/deps, byte for byte."""
    out = {n: (prj.path / n).read_bytes() for n in ("project.json", deps.LOCK) if (prj.path / n).exists()}
    d = prj.path / deps.DEPS
    out.update({f.relative_to(d).as_posix(): f.read_bytes() for f in sorted(d.rglob("*")) if f.is_file()} if d.is_dir()
               else {})
    return out


def _edit_project(prj, f) -> None:
    m = json.loads((prj.path / "project.json").read_text())
    f(m)
    (prj.path / "project.json").write_text(json.dumps(m))


def _counting_git(tmp_path: Path, monkeypatch) -> Path:
    """git on PATH that notes each fetch in the returned log, then runs the real git."""
    real, log = shutil.which("git"), tmp_path / "fetches.log"
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "git").write_text(f'#!/bin/sh\ncase " $* " in *" fetch "*) echo "$*" >> {log};; esac\nexec {real} "$@"\n')
    (bin_ / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    return log


def _fetches(log: Path) -> int:
    return len(log.read_text().splitlines()) if log.exists() else 0


def _refused(call, *says: str) -> str:
    try:
        call()
    except deps.DepError as e:
        for s in says:
            assert s in str(e), (s, str(e))
        return str(e)
    raise AssertionError(f"not refused: {says}")


# ------------------------------------------------------------------ frozen sync


def test_sync_refuses_a_lock_that_no_longer_satisfies_project_json_and_lib_lock_resolves_it(tmp_path):
    repo = _repo(tmp_path / "src")
    store, prj = _project(tmp_path)
    deps.add(prj, "ip", str(repo), "v1", "lib")
    v1, v2 = _git(repo, "rev-parse", "v1"), _git(repo, "rev-parse", "v2")

    # a pull changed the requirement (rev v2) but not the lock
    _edit_project(prj, lambda m: m["libraries"]["ip"].update(rev="v2"))
    before = _files(prj)
    e = _refused(lambda: deps.sync(store.get("p")), "lib lock p")
    assert e == ("ip: project.json asks for rev v2, undine.lock locks v1: run `python -m undine lib lock p` to resolve "
                 "it, or `python -m undine lib update p ip`; nothing was changed")
    assert _files(prj) == before

    # an unlocked dependency, next to the changed one
    _edit_project(prj, lambda m: m["libraries"].update(ip2={"git": str(repo), "rev": "v1"}))
    before = _files(prj)
    e = _refused(lambda: deps.sync(store.get("p")), "ip: project.json asks for rev v2", "ip2: not in undine.lock",
                 "to resolve them", "lib update p NAME")
    assert _files(prj) == before

    # lib lock resolves both, installs them and keeps format 1 entries
    r = deps.relock(store.get("p"))
    assert [(x["name"], x["commit"]) for x in r["resolved"]] == [("ip", v2), ("ip2", v1)] and not r["created"]
    lock = deps.read_lock(prj.path)
    assert lock["format"] == 1 and {n: e["commit"] for n, e in lock["libraries"].items()} == {"ip": v2, "ip2": v1}
    assert all("sha" in e and "tree" not in e for e in lock["libraries"].values())
    assert deps.installed(prj.path, "ip")["commit"] == v2 and (deps.dep_dir(prj.path, "ip2") / "README").is_file()
    assert deps.status(store.get("p"))["ok"], deps.status(store.get("p"))["issues"]
    assert [x["action"] for x in deps.sync(store.get("p"))] == ["ok", "ok"]


def test_a_project_with_git_dependencies_and_no_lock_is_refused_and_no_lock_is_made(tmp_path):
    repo = _repo(tmp_path / "src")
    store, prj = _project(tmp_path)
    (prj.path / deps.LOCK).unlink()
    assert deps.sync(store.get("p")) == [] and deps.read_lock(prj.path) is not None    # no git dependency: as today
    (prj.path / deps.LOCK).unlink()
    _edit_project(prj, lambda m: m["libraries"].update(ip={"git": str(repo), "rev": "v1", "subdir": "lib"}))
    before = _files(prj)
    _refused(lambda: deps.sync(store.get("p")), "ip: not in undine.lock", "lib lock p", "nothing was changed")
    assert _files(prj) == before and not (prj.path / deps.LOCK).exists()
    r = deps.relock(store.get("p"))
    assert r["created"] and [x["name"] for x in r["resolved"]] == ["ip"]
    assert deps.read_lock(prj.path)["libraries"]["ip"]["commit"] == _git(repo, "rev-parse", "v1")
    assert deps.status(store.get("p"))["ok"]


# ------------------------------------------------------------------ offline, from the store


@pytest.mark.parametrize("fmt", [1, 2])
def test_offline_sync_installs_from_the_store_with_no_fetch(tmp_path, monkeypatch, fmt):
    log = _counting_git(tmp_path, monkeypatch)
    repo = _repo(tmp_path / "src")
    store, prj = _project(tmp_path, fmt)
    deps.add(prj, "ip", str(repo), "v1", "lib")
    entry = deps.read_lock(prj.path)["libraries"]["ip"]
    assert deps.entry_hash(entry)[0] == ("tree" if fmt == 2 else "sha")
    assert deps.installed(prj.path, "ip") == entry                           # the marker has the same tree or sha
    lock = (prj.path / deps.LOCK).read_bytes()
    fetched = _fetches(log)
    assert fetched >= 1
    shutil.rmtree(repo)                                                      # the remote is gone
    for how in ("env", "arg"):
        shutil.rmtree(prj.path / deps.DEPS)                                  # a fresh clone
        if how == "env":
            monkeypatch.setenv("UNDINE_OFFLINE", "1")
            out = deps.sync(store.get("p"))
            monkeypatch.delenv("UNDINE_OFFLINE")
        else:
            out = deps.sync(store.get("p"), offline=True)
        assert out == [{"name": "ip", "action": "installed", "commit": entry["commit"]}], how
        assert _fetches(log) == fetched, how                                 # no git fetch ran
        assert deps.status(store.get("p"))["ok"] and (prj.path / deps.LOCK).read_bytes() == lock
    # online, with the remote gone, it installs from the store just the same
    shutil.rmtree(prj.path / deps.DEPS)
    assert [x["action"] for x in deps.sync(store.get("p"))] == ["installed"] and _fetches(log) == fetched


@pytest.mark.parametrize("fmt", [1, 2])
def test_offline_sync_with_an_empty_store_names_what_is_missing_and_changes_nothing(tmp_path, monkeypatch, fmt):
    log = _counting_git(tmp_path, monkeypatch)
    repo = _repo(tmp_path / "src")
    store, prj = _project(tmp_path, fmt)
    deps.add(prj, "ip", str(repo), "v1", "lib")
    deps.add(prj, "ip2", str(repo), "v2", "lib")
    shutil.rmtree(prj.path / deps.DEPS)
    monkeypatch.setenv("UNDINE_STORE", str(tmp_path / "empty"))              # another machine's store
    fetched, before = _fetches(log), _files(prj)
    entry = deps.read_lock(prj.path)["libraries"]["ip"]
    kind, value = deps.entry_hash(entry)
    e = _refused(lambda: deps.sync(store.get("p"), offline=True), "offline, and not in the store",
                 f"ip @ {entry['commit'][:12]} ({kind} {value[:19 if kind == 'tree' else 12]})", "ip2 @ ",
                 "nothing was changed")
    assert "--offline" in e and _fetches(log) == fetched and _files(prj) == before
    # with the network it fetches them, into the store
    assert [x["action"] for x in deps.sync(store.get("p"))] == ["installed", "installed"]
    assert _fetches(log) > fetched and deps.status(store.get("p"))["ok"]


def test_a_failed_online_fetch_is_named_with_its_commit_and_hash(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "src")
    store, prj = _project(tmp_path)
    deps.add(prj, "ip", str(repo), "v1", "lib")
    shutil.rmtree(prj.path / deps.DEPS)
    shutil.rmtree(repo)
    monkeypatch.setenv("UNDINE_STORE", str(tmp_path / "empty"))
    before = _files(prj)
    entry = deps.read_lock(prj.path)["libraries"]["ip"]
    _refused(lambda: deps.sync(store.get("p")), f"could not fetch ip @ {entry['commit'][:12]} (sha {entry['sha'][:12]})",
             "nothing was changed")
    assert _files(prj) == before


def test_an_install_made_before_the_store_is_imported_by_sync_and_then_syncs_offline(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "src")
    store, prj = _project(tmp_path)
    deps.add(prj, "ip", str(repo), "v1", "lib")
    d = deps.dep_dir(prj.path, "ip")
    # as phase 0 installed it: plain read-only copies and a marker, and no store at all
    marker = (d / deps.MARKER).read_text()
    shutil.rmtree(d)
    subprocess.run(["git", "-C", str(repo), "archive", "-o", str(tmp_path / "v1.tar"), "v1", "lib"], check=True)
    subprocess.run(["tar", "-xf", str(tmp_path / "v1.tar"), "-C", str(tmp_path)], check=True)
    shutil.copytree(tmp_path / "lib", d)
    for f in d.rglob("*"):
        if f.is_file():
            f.chmod(0o444)
    (d / deps.MARKER).write_text(marker)
    empty = tmp_path / "pre-store"
    monkeypatch.setenv("UNDINE_STORE", str(empty))
    st = deps.status(store.get("p"))
    ip = next(x for x in st["libraries"] if x["name"] == "ip")
    assert st["ok"] and (ip["hash"], ip["in_store"]) == ("sha", False)
    assert not empty.exists()                                                # status reads only

    assert deps.sync(store.get("p")) == [{"name": "ip", "action": "ok", "commit": _git(repo, "rev-parse", "v1")}]
    ip = next(x for x in deps.status(store.get("p"))["libraries"] if x["name"] == "ip")
    assert ip["in_store"] and cas.read_archive_tree(deps.read_lock(prj.path)["libraries"]["ip"]["sha"]) is not None
    assert not (empty / "git").exists()                                      # imported from the files, not fetched
    shutil.rmtree(d)
    shutil.rmtree(repo)
    assert [x["action"] for x in deps.sync(store.get("p"), offline=True)] == ["installed"]
    assert deps.status(store.get("p"))["ok"]


# ------------------------------------------------------------------ hashes and formats


def test_a_format_1_lock_keeps_format_1_and_phase_0_shas(tmp_path):
    repo = _repo(tmp_path / "src")
    store, prj = _project(tmp_path)
    deps.add(prj, "ip", str(repo), "v1", "lib")
    sha_v1 = deps.read_lock(prj.path)["libraries"]["ip"]["sha"]
    assert sha_v1 == _phase0_sha(str(repo), "v1", "lib", tmp_path / "p0")    # the value phase 0 locked
    assert sha_v1 == deps.tree_sha(deps.dep_dir(prj.path, "ip"))
    deps.update(store.get("p"), "ip", "v2")
    lock = deps.read_lock(prj.path)
    assert lock["format"] == 1 and set(lock["libraries"]["ip"]) == {"git", "rev", "subdir", "commit", "sha"}
    assert lock["libraries"]["ip"]["sha"] == _phase0_sha(str(repo), "v2", "lib", tmp_path / "p0")
    shutil.rmtree(prj.path / deps.DEPS)
    deps.sync(store.get("p"))
    deps.relock(store.get("p"))
    assert deps.read_lock(prj.path) == {**lock, "tools": lock["tools"]}      # sync and lib lock change no entry
    assert deps.read_lock(prj.path)["format"] == 1


def test_a_format_2_lock_gets_trees_from_update_and_sync_never_converts_an_entry(tmp_path):
    repo = _repo(tmp_path / "src")
    store, prj = _project(tmp_path)
    deps.add(prj, "ip", str(repo), "v1", "lib")                              # format 1: a sha entry
    lock = deps.read_lock(prj.path)
    deps.write_lock(prj.path, {**lock, "format": 2, "pdk": None, "tools": copy.deepcopy(NOW)})
    sha_entry = deps.read_lock(prj.path)["libraries"]["ip"]
    shutil.rmtree(prj.path / deps.DEPS)
    assert [x["action"] for x in deps.sync(store.get("p"))] == ["installed"]
    assert deps.read_lock(prj.path)["libraries"]["ip"] == sha_entry          # still the sha it was locked with
    assert deps.status(store.get("p"))["libraries"][0]["hash"] == "sha"

    deps.update(store.get("p"), "ip", "v2")
    entry = deps.read_lock(prj.path)["libraries"]["ip"]
    assert set(entry) == {"git", "rev", "subdir", "commit", "tree"} and entry["tree"].startswith("sha256:")
    files = cas.read_tree(entry["tree"])
    assert set(files) == {"keep.sym", "change.sch", "sub/notes.txt"} and cas.tree_hash(files) == entry["tree"]
    assert deps.installed(prj.path, "ip")["tree"] == entry["tree"]
    deps.add(store.get("p"), "ip2", str(repo), "v1", "lib")
    assert "tree" in deps.read_lock(prj.path)["libraries"]["ip2"]
    assert deps.read_lock(prj.path)["format"] == 2
    assert deps.status(store.get("p"))["ok"]


def test_update_keeps_the_inode_of_an_unchanged_file(tmp_path):
    repo = _repo(tmp_path / "src")
    store, prj = _project(tmp_path)
    deps.add(prj, "ip", str(repo), "v1", "lib")
    d = deps.dep_dir(prj.path, "ip")
    ino = (d / "keep.sym").stat().st_ino
    rep = deps.update(store.get("p"), "ip", "v2")
    assert rep["changed"] == {"added": [], "removed": [], "modified": ["change.sch"]}
    files = cas.read_archive_tree(deps.read_lock(prj.path)["libraries"]["ip"]["sha"])
    assert (d / "keep.sym").stat().st_ino == ino == cas.object_path(files["keep.sym"][1]).stat().st_ino
    assert (d / "change.sch").stat().st_ino == cas.object_path(files["change.sch"][1]).stat().st_ino != ino


def test_installed_files_are_read_only_links_or_with_store_link_copy_copies(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "src")
    store, prj = _project(tmp_path)
    deps.add(prj, "ip", str(repo), "v1", "lib")
    d = deps.dep_dir(prj.path, "ip")
    files = cas.read_archive_tree(deps.read_lock(prj.path)["libraries"]["ip"]["sha"])
    for rel, (_, hex) in files.items():
        st = (d / rel).stat()
        assert stat.S_IMODE(st.st_mode) == 0o444 and st.st_ino == cas.object_path(hex).stat().st_ino, rel
    marker = (d / deps.MARKER).lstat()
    assert stat.S_ISREG(marker.st_mode) and marker.st_nlink == 1             # a file of its own
    monkeypatch.setenv("UNDINE_STORE_LINK", "copy")
    shutil.rmtree(prj.path / deps.DEPS)
    deps.sync(store.get("p"))
    for rel, (_, hex) in files.items():
        st = (d / rel).stat()
        assert stat.S_IMODE(st.st_mode) == 0o444 and st.st_ino != cas.object_path(hex).stat().st_ino, rel
    assert deps.status(store.get("p"))["ok"]


@pytest.mark.parametrize("fmt", [1, 2])
def test_a_file_edited_in_place_is_reported_and_sync_mends_it_and_the_store(tmp_path, fmt):
    repo = _repo(tmp_path / "src")
    store, prj = _project(tmp_path, fmt)
    deps.add(prj, "ip", str(repo), "v1", "lib")
    d = deps.dep_dir(prj.path, "ip")
    entry = deps.read_lock(prj.path)["libraries"]["ip"]
    files = cas.read_tree(entry["tree"]) if fmt == 2 else cas.read_archive_tree(entry["sha"])
    hex = files["keep.sym"][1]
    (d / "keep.sym").chmod(0o644)                                            # the store's inode too
    (d / "keep.sym").write_text("edited\n")
    assert not cas.check_object(hex)
    assert "library ip: modified" in deps.status(store.get("p"))["issues"]
    assert [x["action"] for x in deps.sync(store.get("p"))] == ["installed"]
    assert cas.check_object(hex) and (d / "keep.sym").stat().st_ino == cas.object_path(hex).stat().st_ino
    assert deps.status(store.get("p"))["ok"]


# ------------------------------------------------------------------ checks before git


class _Spy:
    """Records each gitsrc call; the real one runs."""

    def __init__(self, monkeypatch):
        self.calls = []
        for n in ("has_commit", "fetch", "blob_tree", "archive_tree"):
            real = getattr(gitsrc, n)
            monkeypatch.setattr(gitsrc, n, lambda *a, _n=n, _r=real, **k: (self.calls.append(_n), _r(*a, **k))[1])


def test_over_http_a_kept_repository_outside_the_dep_roots_is_not_read_but_the_store_installs(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "src")
    store, prj = _project(tmp_path)
    deps.add(prj, "ip", str(repo), "v1", "lib")                              # from the CLI: no dep root
    entry = deps.read_lock(prj.path)["libraries"]["ip"]
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    spy = _Spy(monkeypatch)
    # the record and its objects in the store: installed with no repository read, so not checked
    shutil.rmtree(prj.path / deps.DEPS)
    assert [x["action"] for x in deps.sync(store.get("p"), local_roots=[allowed])] == ["installed"]
    assert spy.calls == []
    # the commit is in the kept repository but the tree is not recorded: refused before git reads it
    assert gitsrc.repo_path(str(repo)).is_dir()
    cas.archive_path(entry["sha"]).unlink()
    shutil.rmtree(prj.path / deps.DEPS)
    before = _files(prj)
    _refused(lambda: deps.sync(store.get("p"), local_roots=[allowed]), f"{repo.resolve()} is outside", "--dep-root",
             "python -m undine lib sync p")
    assert spy.calls == [] and _files(prj) == before
    assert [x["action"] for x in deps.sync(store.get("p"), local_roots=[tmp_path / "src"])] == ["installed"]
    assert "archive_tree" in spy.calls


def test_an_option_looking_locked_commit_is_refused_before_any_git_call(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "src")
    store, prj = _project(tmp_path)
    deps.add(prj, "ip", str(repo), "v1", "lib")
    lock = deps.read_lock(prj.path)
    evil = f"--upload-pack=touch {tmp_path / 'PWNED'}"
    lock["libraries"]["ip"]["commit"] = evil
    deps.write_lock(prj.path, lock)
    shutil.rmtree(prj.path / deps.DEPS)
    spy = _Spy(monkeypatch)
    before = _files(prj)
    for roots in (None, [tmp_path / "src"]):
        _refused(lambda: deps.sync(store.get("p"), local_roots=roots), "which is not a full commit id")
        assert spy.calls == [] and _files(prj) == before
    # a hash that is not one: refused the same way, before the store or git is looked at
    lock["libraries"]["ip"]["commit"] = _git(repo, "rev-parse", "v1")
    lock["libraries"]["ip"]["sha"] = "../../x"
    deps.write_lock(prj.path, lock)
    _refused(lambda: deps.sync(store.get("p")), "has no valid sha for it")
    assert spy.calls == [] and not (tmp_path / "PWNED").exists()


# ------------------------------------------------------------------ gc, the project list, the live trees


def test_gc_waits_for_an_operation_between_its_tree_and_its_lock_write(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "src")
    store, prj = _project(tmp_path, 2)
    writing, go, got = threading.Event(), threading.Event(), threading.Event()
    real = deps.write_lock

    def slow_write(path, lock):
        if lock.get("libraries"):
            writing.set()
            go.wait(30)
        real(path, lock)
    monkeypatch.setattr(deps, "write_lock", slow_write)
    out = {}
    op = threading.Thread(target=lambda: out.update(r=deps.add(store.get("p"), "ip", str(repo), "v1", "lib")))
    op.start()
    assert writing.wait(30)
    tree = next(iter(cas.store_root().glob("trees/sha256/*.json")))       # recorded, not locked yet

    def gc():
        with cas.store_lock(shared=False):                                   # how `store gc` builds its live set
            got.set()
            live = deps.locked_trees(deps.read_lock(prj.path))
            inst = deps.installed_trees(prj.path)
        out["gc"] = cas.gc(live[0] | inst[0], live[1] | inst[1])
    g = threading.Thread(target=gc)
    g.start()
    assert not got.wait(0.5)                                                 # waits for the operation
    go.set()
    op.join(30)
    g.join(30)
    assert not op.is_alive() and not g.is_alive() and got.is_set()
    assert out["r"]["tree"] == "sha256:" + tree.stem and tree.is_file()       # the tree survived
    assert cas.read_tree(out["r"]["tree"]) is not None and deps.status(store.get("p"))["ok"]


def test_the_store_lists_the_project_and_its_locked_and_installed_trees(tmp_path):
    repo = _repo(tmp_path / "src")
    store, prj = _project(tmp_path)
    deps.add(prj, "ip", str(repo), "v1", "lib")
    lock = deps.read_lock(prj.path)
    deps.write_lock(prj.path, {**lock, "format": 2, "pdk": None, "tools": copy.deepcopy(NOW)})
    deps.add(store.get("p"), "ip2", str(repo), "v2", "lib")
    lock = deps.read_lock(prj.path)
    sha, tree = lock["libraries"]["ip"]["sha"], lock["libraries"]["ip2"]["tree"]
    assert deps.locked_trees(lock) == ({tree}, {sha})
    assert deps.installed_trees(prj.path) == ({tree}, {sha})
    assert [p.resolve() for p in cas.registered_projects()] == [prj.path.resolve()]
    shutil.rmtree(cas.store_root() / "projects")                             # forgotten: the next sync lists it again
    deps.sync(store.get("p"))
    assert [p.resolve() for p in cas.registered_projects()] == [prj.path.resolve()]
    assert deps.locked_trees(None) == (set(), set()) and deps.installed_trees(tmp_path / "nothing") == (set(), set())


# ------------------------------------------------------------------ an executable file without the store's record


def _exec_repo(d: Path) -> Path:
    """lib/ with a cell and an executable script (git mode 100755), tagged v1."""
    (d / "lib").mkdir(parents=True)
    (d / "lib" / "cell.sym").write_text("v {}\n")
    (d / "lib" / "run.sh").write_text("#!/bin/sh\necho run\n")
    (d / "lib" / "run.sh").chmod(0o755)
    (d / "lib" / "tool").write_text("not a script, still executable\n")
    (d / "lib" / "tool").chmod(0o755)
    _git(d, "init", "-q")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "v1")
    _git(d, "tag", "v1")
    return d


def test_a_tree_with_an_executable_file_is_ok_and_seeded_without_the_stores_record(tmp_path, monkeypatch):
    repo = _exec_repo(tmp_path / "src")
    store, prj = _project(tmp_path, 2)
    deps.add(prj, "ip", str(repo), "v1", "lib")
    entry = deps.read_lock(prj.path)["libraries"]["ip"]
    assert cas.read_tree(entry["tree"])["run.sh"][0] == "100755"
    assert deps.installed(prj.path, "ip")["modes"] == {"run.sh": "100755", "tool": "100755"}
    shutil.rmtree(repo)                                # no repository to read the modes from
    monkeypatch.setenv("UNDINE_STORE", str(tmp_path / "new-store"))          # the store moved or was emptied
    st = deps.status(store.get("p"))
    assert st["ok"], st["issues"]
    assert deps.sync(store.get("p")) == [{"name": "ip", "action": "ok", "commit": entry["commit"]}]
    assert cas.read_tree(entry["tree"]) is not None                          # seeded, with the modes
    shutil.rmtree(prj.path / deps.DEPS)
    assert [x["action"] for x in deps.sync(store.get("p"), offline=True)] == ["installed"]
    assert deps.status(store.get("p"))["ok"]


def test_a_script_is_found_by_its_shebang_when_the_marker_has_no_modes(tmp_path, monkeypatch):
    repo = tmp_path / "src"
    (repo / "lib").mkdir(parents=True)
    (repo / "lib" / "cell.sym").write_text("v {}\n")
    (repo / "lib" / "run.sh").write_text("#!/bin/sh\necho run\n")
    (repo / "lib" / "run.sh").chmod(0o755)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "v1")
    store, prj = _project(tmp_path, 2)
    deps.add(prj, "ip", str(repo), None, "lib")
    d = deps.dep_dir(prj.path, "ip")
    info = json.loads((d / deps.MARKER).read_text())
    del info["modes"]                                  # as an install of the first phase 1 builds left it
    (d / deps.MARKER).write_text(json.dumps(info))
    monkeypatch.setenv("UNDINE_STORE", str(tmp_path / "new-store"))
    assert deps.status(store.get("p"))["ok"]
    assert [x["action"] for x in deps.sync(store.get("p"))] == ["ok"]
    shutil.rmtree(prj.path / deps.DEPS)
    shutil.rmtree(repo)
    assert [x["action"] for x in deps.sync(store.get("p"), offline=True)] == ["installed"]


# ------------------------------------------------------------------ a mode changed through an installed link


def test_a_chmod_through_one_project_does_not_cost_another_its_offline_sync(tmp_path):
    repo = _repo(tmp_path / "src")
    store, prj = _project(tmp_path)
    deps.add(prj, "ip", str(repo), "v1", "lib")
    lock = (prj.path / deps.LOCK).read_text()
    project_json = (prj.path / "project.json").read_text()
    shutil.rmtree(cas.store_root() / "git")            # no kept repository: the store's objects are the only copy
    shutil.rmtree(repo)
    for f in deps.dep_dir(prj.path, "ip").rglob("*"):
        if f.is_file() and f.name != deps.MARKER:
            f.chmod(0o644)                             # `chmod -R u+w .undine/deps`: the store's inodes too
    other = store.create("q")
    (other.path / deps.LOCK).write_text(lock)
    (other.path / "project.json").write_text(project_json.replace('"p"', '"q"'))
    assert [x["action"] for x in deps.sync(store.get("q"), offline=True)] == ["installed"]
    files = cas.read_archive_tree(json.loads(lock)["libraries"]["ip"]["sha"])
    for rel, (_, hex) in files.items():
        assert stat.S_IMODE(cas.object_path(hex).stat().st_mode) == 0o444, rel


# ------------------------------------------------------------------ .undine/deps must be the project's own


@pytest.mark.parametrize("link", [".undine", ".undine/deps"])
def test_a_linked_deps_directory_is_refused_and_nothing_outside_is_touched(tmp_path, link):
    repo = _repo(tmp_path / "src")
    store, prj = _project(tmp_path)
    deps.add(prj, "ip", str(repo), "v1", "lib")
    victim = tmp_path / "victim"
    (victim / "deps" / "precious").mkdir(parents=True)
    (victim / "deps" / "precious" / "keep.txt").write_text("mine\n")
    (victim / "deps" / "ip").mkdir()
    (victim / "deps" / "ip" / "keep.txt").write_text("mine too\n")
    (victim / "precious").mkdir()
    (victim / "ip").mkdir()
    shutil.rmtree(prj.path / ".undine")
    if link == ".undine":
        (prj.path / ".undine").symlink_to(victim)
    else:
        (prj.path / ".undine").mkdir()
        (prj.path / ".undine" / "deps").symlink_to(victim / "deps")
    before = sorted((p.relative_to(victim).as_posix(), p.read_bytes() if p.is_file() else None)
                    for p in victim.rglob("*"))
    prj = store.get("p")
    says = f"{link} is a link or not a directory"
    _refused(lambda: deps.sync(prj), says, "nothing was changed")
    _refused(lambda: deps.sync(prj, offline=True), says)
    _refused(lambda: deps.update(prj, "ip", "v2"), says)
    _refused(lambda: deps.add(prj, "ip2", str(repo), "v1", "lib"), says)
    _refused(lambda: deps.remove(prj, "ip"), says)
    assert sorted((p.relative_to(victim).as_posix(), p.read_bytes() if p.is_file() else None)
                  for p in victim.rglob("*")) == before
    assert any(says in i for i in deps.status(prj)["issues"])
    assert deps.installed_trees(prj.path) == (set(), set())
    assert "ip" in prj.library_specs()                 # remove changed nothing
