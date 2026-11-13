"""Phase 2 sync, leftovers and status over the lock's graph (store/deps.py): sync installs every package the lock
reaches from project.json, under each of its local names, from the store with no fetch when it can; what nothing
reaches any more is a leftover that sync removes; status has one row per local name with the package fields, and
reports what the graph, the PDK, the project's cells and the trust rule say about it.

The tools are fixed (as in test_store_sync.py), so nothing runs a real tool to record versions; git is real, every
repository local."""
import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import klayout.db as kdb
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine import tools  # noqa: E402
from undine.pdks.pdk import Pdk  # noqa: E402
from undine.store import cas, deps, gitsrc  # noqa: E402
from undine.store.project import ProjectStore  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

NOW = {"undine": "0.1.0 (abc1234)", "pixi_lock": None, "klayout": {"version": "0.30.12", "from": "pixi"},
       "xschem": None, "ngspice": None, "magic": None, "netgen": None}
BANNERS = {"klayout": "KLayout 0.30.12", "xschem": None, "ngspice": None, "magic": None, "netgen": None}
SYM = "v {}\nK {type=subcircuit}\n"
MANIFEST = "undine-package.toml"
CONTROL = ('v {xschem version=3.4.5 file_version=1.2}\n'
           'C {devices/res.sym} 0 0 0 0 {name=R1 value="1k\n.control\nshell ls\n.endc"}\n')


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


@pytest.fixture(autouse=True)
def _tools_here(monkeypatch):
    monkeypatch.setattr(tools, "tools_record", lambda: copy.deepcopy(NOW))
    monkeypatch.setattr(tools, "version", lambda n: BANNERS[n])
    monkeypatch.delenv("UNDINE_OFFLINE", raising=False)
    monkeypatch.delenv("UNDINE_STORE_LINK", raising=False)


def _git(d: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


def _put(repo: Path, files: dict[str, str], tag: str) -> str:
    if not (repo / ".git").exists():
        repo.mkdir(parents=True, exist_ok=True)
        _git(repo, "init", "-q")
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", tag)
    _git(repo, "tag", tag)
    return _git(repo, "rev-parse", "HEAD")


def _manifest(name: str, version: str = "1.0.0", deps_: dict[str, str] | None = None, body: str = "") -> str:
    lines = ["[package]", f'name = "{name}"', f'version = "{version}"']
    if deps_:
        lines += ["", "[dependencies]", *(f"{k} = {v}" for k, v in deps_.items())]
    return "\n".join(lines) + "\n" + body


def _package(root: Path, name: str, version: str = "1.0.0", deps_: dict[str, str] | None = None,
             cells: tuple[str, ...] | None = None, body: str = "", extra: dict[str, str] | None = None,
             repo: Path | None = None, sub: str = "") -> Path:
    """Package `name` (a cell <name>_cell unless `cells` says) in a repository of its own (or `repo`, under `sub`),
    tagged v<version>."""
    repo = repo or root / name
    pre = f"{sub}/" if sub else ""
    files = {f"{pre}{c}.sym": SYM for c in (cells or (f"{name}_cell",))}
    files[f"{pre}{MANIFEST}"] = _manifest(name, version, deps_, body)
    _put(repo, {**files, **{pre + k: v for k, v in (extra or {}).items()}}, f"v{version}" if not sub else f"{sub}-v")
    return repo


def _dep(repo, tag: str = "v1.0.0", **more: str) -> str:
    items = [f'git = "{repo}"', f'rev = "{tag}"', *(f'{k} = "{v}"' for k, v in more.items())]
    return "{ " + ", ".join(items) + " }"


def _pin(p: Pdk) -> dict:
    """A format 2 PDK pin of `p` (what pdk verify --pin writes; its tree is not checked by status)."""
    return {"id": p.id, "family": p.family, "variant": p.variant, "commit": p.commit, "source": None,
            "libraries": [], "tree": {}}


def _project(tmp_path: Path, pdks: dict | None = None, pdk: str | None = None):
    store = ProjectStore(tmp_path / "projects", pdks or {})
    prj = store.create("p", pdk)
    pin = _pin(prj.pdk) if prj.pdk else None
    deps.write_lock(prj.path, {**deps.read_lock(prj.path), "format": 2, "pdk": pin, "tools": copy.deepcopy(NOW)})
    return store, prj


def _files(prj) -> dict[str, bytes]:
    out = {n: (prj.path / n).read_bytes() for n in ("project.json", deps.LOCK) if (prj.path / n).exists()}
    d = prj.path / deps.DEPS
    out.update({f.relative_to(d).as_posix(): f.read_bytes() for f in sorted(d.rglob("*")) if f.is_file()} if d.is_dir()
               else {})
    return out


def _counting_git(tmp_path: Path, monkeypatch) -> Path:
    """git on PATH that notes each fetch in the returned log, then runs the real git."""
    real, log = shutil.which("git"), tmp_path / "fetches.log"
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "git").write_text(f'#!/bin/sh\ncase " $* " in *" fetch "*) echo "$*" >> {log};; esac\nexec {real} "$@"\n')
    (bin_ / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    return log


def _fetched(log: Path, repo: Path) -> int:
    return sum(str(repo) in line for line in log.read_text().splitlines()) if log.exists() else 0


def _lock(prj) -> dict:
    return deps.read_lock(prj.path)


def _marker(prj, name: str) -> dict:
    return json.loads((deps.dep_dir(prj.path, name) / deps.MARKER).read_text())


def _edit_project(prj, f) -> None:
    m = json.loads((prj.path / "project.json").read_text())
    f(m)
    (prj.path / "project.json").write_text(json.dumps(m))


def _edit_lock(prj, f) -> None:
    lock = _lock(prj)
    f(lock)
    deps.write_lock(prj.path, lock)


def _row(st: dict, name: str) -> dict:
    return next(x for x in st["libraries"] if x["name"] == name)


def _graph(tmp_path: Path):
    """a needs c and bias_gen (placed as bias); a project with a added."""
    src = tmp_path / "src"
    c = _package(src, "c")
    bias = _package(src, "bias_gen", cells=("ref",))
    a = _package(src, "a", deps_={"c": _dep(c), "bias": _dep(bias, package="bias_gen")})
    store, prj = _project(tmp_path)
    deps.add(prj, "a", str(a), "v1.0.0")
    return store, prj, a, c, bias


# ------------------------------------------------------------------ sync


def test_a_frozen_sync_installs_every_package_and_name_from_the_store_with_no_fetch(tmp_path, monkeypatch):
    store, prj, a, c, bias = _graph(tmp_path)
    lock = _lock(prj)
    shutil.rmtree(prj.path / deps.DEPS)                                  # a fresh clone, on a machine that has them
    log = _counting_git(tmp_path, monkeypatch)
    rows = deps.sync(store.get("p"))
    ca = _git(a, "rev-parse", "v1.0.0")
    assert rows[0] == {"name": "a", "action": "installed", "commit": ca, "package": "a"}
    assert sorted((r["name"], r["action"], r.get("package")) for r in rows) == [
        ("a", "installed", "a"), ("bias", "installed", "bias_gen"), ("bias_gen", "installed", "bias_gen"),
        ("c", "installed", "c")]
    assert not log.exists()                                              # nothing fetched, nothing read from git
    assert _lock(prj) == lock                                            # sync never rewrites an entry
    for n in ("bias", "bias_gen"):
        m = _marker(prj, n)
        assert (m["name"], m["direct"], m["package"], m["via"]) == (n, False, "bias_gen", ["a"])
        assert m["tree"] == lock["packages"]["bias_gen"]["tree"]
    assert (deps.dep_dir(prj.path, "bias") / "ref.sym").stat().st_ino == \
        (deps.dep_dir(prj.path, "bias_gen") / "ref.sym").stat().st_ino
    assert _marker(prj, "a")["direct"] is True and "via" not in _marker(prj, "a")
    st = deps.status(store.get("p"))
    assert st["ok"], st["issues"]
    assert not any("leftover" == x["state"] for x in st["libraries"])    # transitive installs are no leftovers
    assert [r["action"] for r in deps.sync(store.get("p"))] == ["ok"] * 4


def test_a_frozen_sync_of_a_diamond_installs_the_shared_package_once_from_the_store_alone(tmp_path, monkeypatch):
    """a and b both need c: sync installs c once, under one name, with both as its requirers, its files linked from
    the store's objects. The kept repositories are gone and git may not be asked anything: the store is the only
    copy, online or offline."""
    src = tmp_path / "src"
    c = _package(src, "c")
    a = _package(src, "a", deps_={"c": _dep(c)})
    b = _package(src, "b", deps_={"c": _dep(c)})
    store, prj = _project(tmp_path)
    deps.add(prj, "a", str(a), "v1.0.0")
    deps.add(store.get("p"), "b", str(b), "v1.0.0")
    lock = _lock(prj)
    assert set(lock["packages"]) == {"c"}

    def never(*a, **k):
        raise AssertionError(f"git was asked: {a}")
    for f in ("has_commit", "fetch", "blob_tree", "read_file", "archive_tree"):
        monkeypatch.setattr(gitsrc, f, never)
    shutil.rmtree(cas.store_root() / "git")                               # the store is the only copy left
    for offline in (False, True):
        for root, ds, _ in os.walk(prj.path / deps.DEPS):
            os.chmod(root, 0o755)
        shutil.rmtree(prj.path / deps.DEPS)
        rows = deps.sync(store.get("p"), offline=offline)
        assert sorted((r["name"], r["action"]) for r in rows) == [
            ("a", "installed"), ("b", "installed"), ("c", "installed")]       # c once
        assert _lock(prj) == lock
        m = _marker(prj, "c")
        assert (m["direct"], sorted(m["via"])) == (False, ["a", "b"])
        files = cas.read_tree(lock["packages"]["c"]["tree"])
        sym = deps.dep_dir(prj.path, "c") / "c_cell.sym"
        assert sym.stat().st_ino == cas.object_path(files["c_cell.sym"][1]).stat().st_ino


def test_offline_with_an_empty_store_names_each_missing_package_once_and_changes_nothing(tmp_path, monkeypatch):
    store, prj, a, c, bias = _graph(tmp_path)
    shutil.rmtree(prj.path / deps.DEPS)
    monkeypatch.setenv("UNDINE_STORE", str(tmp_path / "empty"))
    before = _files(prj)
    with pytest.raises(deps.DepError) as e:
        deps.sync(store.get("p"), offline=True)
    msg = str(e.value)
    assert msg.startswith("offline, and not in the store: a @ ") and msg.endswith("nothing was changed"), msg
    named = msg.split("not in the store: ")[1].split(": run the sync")[0].split(", ")
    assert sorted(n.split(" @ ")[0] for n in named) == ["a", "bias", "c"], msg     # bias_gen is bias: once
    assert _files(prj) == before


def test_packages_of_one_repository_and_commit_are_fetched_once(tmp_path, monkeypatch):
    """p1 and its sibling p2 live in one repository: an online sync with an empty store fetches it once."""
    src = tmp_path / "src"
    mono = src / "mono"
    _package(src, "p2", repo=mono, sub="p2")
    _package(src, "p1", deps_={"p2": '{ subdir = "p2" }'}, repo=mono, sub="p1")
    top = _package(src, "top", deps_={"p1": _dep(mono, "p1-v", subdir="p1")})
    store, prj = _project(tmp_path)
    deps.add(prj, "top", str(top), "v1.0.0")
    assert _lock(prj)["packages"]["p2"]["subdir"] == "p2"
    shutil.rmtree(prj.path / deps.DEPS)
    monkeypatch.setenv("UNDINE_STORE", str(tmp_path / "empty"))
    log = _counting_git(tmp_path, monkeypatch)
    rows = deps.sync(store.get("p"))
    assert sorted(r["name"] for r in rows) == ["p1", "p2", "top"]
    assert _fetched(log, mono) == 1 and _fetched(log, top) == 1, log.read_text()
    assert (deps.dep_dir(prj.path, "p2") / "p2_cell.sym").is_file()


def test_plain_dependency_rows_stay_name_action_commit(tmp_path):
    src = tmp_path / "src"
    plain = src / "plain"
    v1 = _put(plain, {"inv.sym": SYM}, "v1")
    store, prj, *_ = _graph(tmp_path)
    deps.add(store.get("p"), "plain", str(plain), "v1")
    shutil.rmtree(deps.dep_dir(prj.path, "plain"))
    rows = deps.sync(store.get("p"))
    assert {"name": "plain", "action": "installed", "commit": v1} in rows
    assert all(set(r) == {"name", "action", "commit", "package"} for r in rows if r["name"] != "plain")
    assert _marker(prj, "plain") == _lock(prj)["libraries"]["plain"]    # a plain marker is the entry, as before


def test_a_phase_1_entry_whose_manifest_has_files_still_syncs_every_file_from_git(tmp_path, monkeypatch):
    """A format 2 entry as phase 1 wrote it (no files key: every file) of a commit whose manifest has [files]: a sync
    after the store is emptied reads it from git again over every file, and the tree matches."""
    src = tmp_path / "src"
    ip = _package(src, "analog_ip", "1.2.0", body='\n[files]\ninclude = ["*.sym"]\n', extra={"README": "docs\n"})
    commit = _git(ip, "rev-parse", "v1.2.0")
    store, prj = _project(tmp_path)
    gitsrc.fetch(str(ip), None, "v1.2.0")
    entry = {"git": str(ip), "rev": "v1.2.0", "commit": commit,
             "tree": cas.write_tree(gitsrc.blob_tree(str(ip), commit, None))}
    _edit_project(prj, lambda m: m["libraries"].update({"analog_ip": {"git": str(ip), "rev": "v1.2.0"}}))
    _edit_lock(prj, lambda lock: lock["libraries"].update({"analog_ip": entry}))
    shutil.rmtree(prj.path / deps.DEPS, ignore_errors=True)
    monkeypatch.setenv("UNDINE_STORE", str(tmp_path / "empty"))
    assert deps.sync(store.get("p")) == [{"name": "analog_ip", "action": "installed", "commit": commit}]
    assert (deps.dep_dir(prj.path, "analog_ip") / "README").read_text() == "docs\n"
    assert _marker(prj, "analog_ip") == entry and _lock(prj)["libraries"]["analog_ip"] == entry
    assert deps.status(store.get("p"))["ok"]


def test_removing_a_dependency_from_project_json_removes_what_only_it_reached(tmp_path):
    src = tmp_path / "src"
    c = _package(src, "c")
    d = _package(src, "d")
    a = _package(src, "a", deps_={"c": _dep(c), "d": _dep(d)})
    b = _package(src, "b", deps_={"c": _dep(c)})
    store, prj = _project(tmp_path)
    deps.add(prj, "a", str(a), "v1.0.0")
    deps.add(store.get("p"), "b", str(b), "v1.0.0")
    assert _marker(prj, "c")["via"] == ["a", "b"]
    cd = _git(d, "rev-parse", "v1.0.0")
    _edit_project(prj, lambda m: m["libraries"].pop("a"))               # a git pull: the lock and files stay

    st = deps.status(store.get("p"))
    assert [(x["name"], x["state"]) for x in st["libraries"] if x["kind"] == "dependency"] == \
        [("b", "ok"), ("c", "ok"), ("a", "leftover"), ("d", "leftover")]
    assert _row(st, "c")["via"] == ["b"] and _row(st, "c")["chain"].startswith("p -> b 1.0.0")
    assert _row(st, "d")["package"] == "d"
    assert any(i.startswith("library d: not in project.json, still installed in .undine/deps/d/ and locked")
               for i in st["issues"]), st["issues"]

    rows = deps.sync(store.get("p"))
    assert [r for r in rows if r["action"] == "removed"] == [
        {"name": "a", "action": "removed", "commit": _git(a, "rev-parse", "v1.0.0"), "package": "a"},
        {"name": "d", "action": "removed", "commit": cd, "package": "d"}]
    lock = _lock(prj)
    assert set(lock["libraries"]) == {"b"} and set(lock["packages"]) == {"c"}
    assert not deps.dep_dir(prj.path, "a").exists() and not deps.dep_dir(prj.path, "d").exists()
    assert _marker(prj, "c")["via"] == ["b"]                            # who places it, as the lock now says
    assert deps.status(store.get("p"))["ok"]


def test_sync_refuses_an_incomplete_graph_or_a_name_claimed_twice_before_anything_is_read(tmp_path, monkeypatch):
    store, prj, a, c, bias = _graph(tmp_path)
    ca = _git(a, "rev-parse", "v1.0.0")[:12]

    def nothing_read(*_a, **_k):
        raise AssertionError("read before the refusal")
    for f in ("has_commit", "fetch", "blob_tree", "read_file"):
        monkeypatch.setattr(gitsrc, f, nothing_read)
    monkeypatch.setattr(cas, "read_tree", nothing_read)

    _edit_lock(prj, lambda lock: lock["packages"].pop("c"))
    before = _files(prj)
    with pytest.raises(deps.DepError) as e:
        deps.sync(store.get("p"))
    assert str(e.value) == (f"a: a 1.0.0 (v1.0.0 @ {ca}): needs c (as c), which undine.lock does not lock: run "
                            "`python -m undine lib lock p` to resolve it, or `python -m undine lib update p a`; "
                            "nothing was changed")
    assert _files(prj) == before

    _edit_lock(prj, lambda lock: lock["packages"].update(
        {"c": {**lock["packages"]["bias_gen"], "package": "c", "names": ["c", "bias"]}}))
    with pytest.raises(deps.DepError) as e:
        deps.sync(store.get("p"))
    assert "bias: one local name for " in str(e.value) and str(e.value).endswith("nothing was changed")


# ------------------------------------------------------------------ status


def test_status_rows_carry_the_package_fields_and_one_row_per_local_name(tmp_path):
    store, prj, a, c, bias = _graph(tmp_path)
    ca, cb = _git(a, "rev-parse", "v1.0.0")[:12], _git(bias, "rev-parse", "v1.0.0")[:12]
    st = deps.status(store.get("p"))
    assert st["ok"], st["issues"]
    assert [x["name"] for x in st["libraries"]] == ["a", "bias", "bias_gen", "c"]
    ra, rb = _row(st, "a"), _row(st, "bias")
    assert {k: ra[k] for k in ("kind", "state", "package", "version", "names", "direct", "via", "chain", "trusted",
                               "flagged", "internal_cells")} == \
        {"kind": "dependency", "state": "ok", "package": "a", "version": "1.0.0", "names": ["a"], "direct": True,
         "via": [], "chain": f"p -> a 1.0.0 (v1.0.0 @ {ca})", "trusted": False,
         "flagged": {"spice": 0, "rtl": 0, "tcl": 0}, "internal_cells": []}
    assert (rb["package"], rb["names"], rb["direct"], rb["via"], rb["state"]) == \
        ("bias_gen", ["bias", "bias_gen"], False, ["a"], "ok")
    assert rb["chain"] == f"p -> a 1.0.0 (v1.0.0 @ {ca}) -> bias_gen 1.0.0 (v1.0.0 @ {cb})"
    assert ra["pdk"] == {"compatible": None, "reason": "no [pdk]", "note": None, "built_with": None}
    _edit_project(prj, lambda m: m.update(trusted=["a@1.0.0", "../x"]))
    st = deps.status(store.get("p"))
    assert _row(st, "a")["trusted"] is True and _row(st, "c")["trusted"] is False
    assert st["issues"] == ["project.json: trusted has '../x', which is not a name@version key: remove it, and trust "
                            "again with lib trust"]


def test_status_reports_an_incompatible_package_and_notes_a_built_with_difference(tmp_path):
    root = tmp_path / "pdk"
    fa, fb = Pdk("fakeA", "fake", "a" * 40, root / "a"), Pdk("fakeB", "fake", "b" * 40, root / "b")
    req = f'\n[pdk]\nfamily = "fake"\nvariants = ["fakeA", "fakeB"]\ncommits = ["{"a" * 40}", "{"c" * 40}"]\n'
    src = tmp_path / "src"
    c = _package(src, "c", body=req + f'built_with = "{"c" * 40}"\n')
    store, prj = _project(tmp_path, pdks={fa.id: fa, fb.id: fb}, pdk="fakeA")
    deps.add(prj, "c", str(c), "v1.0.0")
    st = deps.status(store.get("p"))
    assert st["ok"], st["issues"]
    assert _row(st, "c")["pdk"] == {"compatible": True, "reason": None,
                                    "note": "drawn on cccccccccccc, used at aaaaaaaaaaaa", "built_with": "c" * 40}
    assert "library c: c 1.0.0: drawn on cccccccccccc, used at aaaaaaaaaaaa" in st["notes"]

    _edit_project(prj, lambda m: m.update(pdk=fb.id))                   # by hand, past set-pdk's refusal
    st = deps.status(store.get("p"))
    cc = _git(c, "rev-parse", "v1.0.0")[:12]
    why = "fakeB@bbbbbbbbbbbb is not among commits aaaaaaaaaaaa, cccccccccccc: update or remove it (lib update, lib " \
          "remove), or choose a PDK it accepts"
    assert f"p -> c 1.0.0 (v1.0.0 @ {cc}): does not fit the PDK project.json asks for (fakeB@bbbbbbbbbbbb): " \
           f"{why}" in st["issues"], st["issues"]
    assert _row(st, "c")["pdk"]["compatible"] is True and not st["ok"]   # the lock's PDK still takes it
    _edit_lock(prj, lambda lock: lock.update(pdk=_pin(fb)))              # the pin moved too (pdk verify --pin)
    st = deps.status(store.get("p"))
    assert st["issues"] == [f"p -> c 1.0.0 (v1.0.0 @ {cc}): does not fit the PDK fakeB@bbbbbbbbbbbb: {why}"]
    assert _row(st, "c")["pdk"]["compatible"] is False
    rows = deps.sync(store.get("p"))                                     # installed all the same, with a warning
    assert rows[0]["action"] == "ok" and "does not fit the PDK fakeB@bbbbbbbbbbbb" in rows[0]["warning"]


def test_status_notes_a_plain_dependency_without_manifest_only_when_there_is_a_pdk(tmp_path):
    fa = Pdk("fakeA", "fake", "a" * 40, tmp_path / "pdk")
    plain = tmp_path / "src" / "plain"
    _put(plain, {"inv.sym": SYM}, "v1")
    store, prj = _project(tmp_path, pdks={fa.id: fa}, pdk="fakeA")
    deps.add(prj, "plain", str(plain), "v1")
    st = deps.status(store.get("p"))
    assert "library plain: no manifest: PDK compatibility not checked" in st["notes"] and st["ok"]
    store, prj = _project(tmp_path / "nopdk")
    deps.add(prj, "plain", str(plain), "v1")
    assert not any("PDK compatibility" in n for n in deps.status(store.get("p"))["notes"])


def test_a_broken_installed_manifest_is_an_issue_row_not_a_crash(tmp_path):
    src = tmp_path / "src"
    broken = src / "broken"
    commit = _put(broken, {"inv.sym": SYM, MANIFEST: '[package]\nname = "broken"\nversion = "1.2"\n'}, "v1")
    store, prj = _project(tmp_path)
    gitsrc.fetch(str(broken), None, "v1")
    entry = {"git": str(broken), "rev": "v1", "commit": commit,
             "tree": cas.write_tree(gitsrc.blob_tree(str(broken), commit, None))}
    _edit_project(prj, lambda m: m["libraries"].update({"broken": {"git": str(broken), "rev": "v1"}}))
    _edit_lock(prj, lambda lock: lock["libraries"].update({"broken": entry}))
    assert [r["action"] for r in deps.sync(store.get("p"))] == ["installed"]     # phase 1 never read it
    st = deps.status(store.get("p"))
    assert _row(st, "broken")["state"] == "ok" and _row(st, "broken")["package"] is None
    assert any(i.startswith("library broken: undine-package.toml: ") and "1.2" in i for i in st["issues"]), \
        st["issues"]


def test_a_project_cell_named_like_a_packages_cell_is_an_issue_a_layout_copy_is_not(tmp_path):
    src = tmp_path / "src"
    x = _package(src, "x", cells=("comparator",))
    plain = src / "plain"
    _put(plain, {"inv.sym": SYM}, "v1")
    store, prj = _project(tmp_path)
    deps.add(prj, "x", str(x), "v1.0.0")
    deps.add(store.get("p"), "plain", str(plain), "v1")
    lib = store.get("p").create_library("mylib")
    ly = kdb.Layout()
    ly.create_cell("comparator")
    ly.write(str(lib.path / "comparator.oas"))
    st = deps.status(store.get("p"))
    assert st["ok"], st["issues"]                                        # a layout copy of a placed cell
    (lib.path / "comparator.sch").write_text("v {}\n")
    (lib.path / "inv.sym").write_text(SYM)
    st = deps.status(store.get("p"))
    assert st["issues"] == ["comparator: x 1.0.0 and project library mylib: a netlist has one subcircuit per name: "
                            "rename one of them"]
    commit = _git(plain, "rev-parse", "v1")[:12]
    assert f"inv: plain (git {commit}) and project library mylib: a netlist has one subcircuit per name, so a " \
           "testbench placing both is refused: rename one" in st["notes"]


def test_a_plain_dependency_with_control_gets_the_trust_note(tmp_path):
    plain = tmp_path / "src" / "plain"
    commit = _put(plain, {"top.sch": CONTROL}, "v1")
    store, prj = _project(tmp_path)
    deps.add(prj, "plain", str(plain), "v1")
    st = deps.status(store.get("p"))
    assert st["ok"] and _row(st, "plain")["flagged"] == {"spice": 1, "rtl": 0, "tcl": 0}
    assert _row(st, "plain")["trusted"] is False
    assert "library plain: 1 cell with .control (not trusted): a simulation using it needs `python -m undine lib " \
           "trust p plain`" in st["notes"], st["notes"]
    _edit_project(prj, lambda m: m.update(trusted=[f"plain@{commit[:12]}"]))
    st = deps.status(store.get("p"))
    assert _row(st, "plain")["trusted"] is True and not any("not trusted" in n for n in st["notes"])


def test_a_package_entry_with_a_bad_key_or_name_is_reported_and_left_alone(tmp_path):
    store, prj, *_ = _graph(tmp_path)
    _edit_lock(prj, lambda lock: lock["packages"].update({"../evil": {"commit": "0", "names": ["x"]},
                                                          "c2": {"commit": "0", "names": ["/abs"]}}))
    st = deps.status(store.get("p"))
    assert any("packages has an entry '../evil'" in i for i in st["issues"])
    assert any("packages has an entry 'c2'" in i for i in st["issues"])
    rows = deps.sync(store.get("p"))
    assert [r["name"] for r in rows if r["action"] == "removed"] == []           # neither taken for a path
    assert {"../evil", "c2"} <= set(_lock(prj)["packages"])
