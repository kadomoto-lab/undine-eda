"""Phase 2 resolution in store/deps.py: add, update, remove and lib lock read every manifest (from git for what they
resolve, from the store or the install for what stays locked), check the whole graph (one version per package, local
names, cell names, PDK compatibility) and refuse with every problem before anything is written; a format 2 lock keeps
direct dependencies in `libraries` and the packages reached through manifests in `packages`, each installed under
every local name it is placed by. Plain git dependencies keep phase 1's entries and markers.

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
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine import tools  # noqa: E402
from undine.pdks.pdk import Pdk  # noqa: E402
from undine.store import cas, deps, gitsrc, trust  # noqa: E402
from undine.store.project import ProjectStore  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")

NOW = {"undine": "0.1.0 (abc1234)", "pixi_lock": None, "klayout": {"version": "0.30.12", "from": "pixi"},
       "xschem": None, "ngspice": None, "magic": None, "netgen": None}
BANNERS = {"klayout": "KLayout 0.30.12", "xschem": None, "ngspice": None, "magic": None, "netgen": None}
SYM = "v {}\nK {type=subcircuit}\n"
MANIFEST = "undine-package.toml"


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
    """Writes `files` into the repository (made on first use), commits and tags them; returns the commit."""
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
             tag: str | None = None, repo: Path | None = None) -> Path:
    """A repository holding package `name` (a cell <name>_cell unless `cells` says), tagged v<version>."""
    repo = repo or root / name
    files = {f"{c}.sym": SYM for c in (cells or (f"{name}_cell",))}
    files[MANIFEST] = _manifest(name, version, deps_, body)
    _put(repo, {**files, **(extra or {})}, tag or f"v{version}")
    return repo


def _dep(repo, tag: str = "v1.0.0", **more: str) -> str:
    """A manifest's git requirement: { git = "...", rev = "...", ... }."""
    items = [f'git = "{repo}"', f'rev = "{tag}"', *(f'{k} = "{v}"' for k, v in more.items())]
    return "{ " + ", ".join(items) + " }"


def _project(tmp_path: Path, fmt: int = 2, pdks: dict | None = None, pdk: str | None = None):
    store = ProjectStore(tmp_path / "projects", pdks or {})
    prj = store.create("p", pdk)
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


def _refused(call, kind=deps.DepError) -> deps.DepError:
    with pytest.raises(kind) as e:
        call()
    return e.value


def _lock(prj) -> dict:
    return deps.read_lock(prj.path)


def _marker(prj, name: str) -> dict:
    return json.loads((deps.dep_dir(prj.path, name) / deps.MARKER).read_text())


def _edit_project(prj, f) -> None:
    m = json.loads((prj.path / "project.json").read_text())
    f(m)
    (prj.path / "project.json").write_text(json.dumps(m))


def _phase1_entry(prj, store, name: str, repo: Path, rev: str) -> dict:
    """A format 2 entry as phase 1 wrote it (every file of the repository, no files / package keys), installed by
    sync."""
    commit = _git(repo, "rev-parse", rev)
    gitsrc.fetch(str(repo), None, rev)
    entry = {"git": str(repo), "rev": rev, "commit": commit,
             "tree": cas.write_tree(gitsrc.blob_tree(str(repo), commit, None))}
    _edit_project(prj, lambda m: m["libraries"].update({name: {"git": str(repo), "rev": rev}}))
    lock = _lock(prj)
    lock["libraries"][name] = entry
    deps.write_lock(prj.path, lock)
    assert [r["action"] for r in deps.sync(store.get("p"))] == ["installed"]
    return entry


# ------------------------------------------------------------------ the graph


def test_diamond_installs_c_once_hard_linked_and_fetches_it_once(tmp_path, monkeypatch):
    log = _counting_git(tmp_path, monkeypatch)
    src = tmp_path / "src"
    c = _package(src, "c")
    a = _package(src, "a", deps_={"c": _dep(c)})
    b = _package(src, "b", deps_={"c": _dep(c)})
    store, prj = _project(tmp_path)

    r = deps.add(prj, "a", str(a), "v1.0.0")
    assert (r["package"], r["version"], r["names"], r["deps"], r["files"]) == \
        ("a", "1.0.0", ["a"], {"c": "c"}, "include")
    assert [(x["name"], x["package"], x["via"]) for x in r["resolved"]] == [("a", "a", []), ("c", "c", ["a"])]
    assert r["cells"] == ["a_cell"] and r["flagged"] == {"spice": 0, "rtl": 0, "tcl": 0} and r["trusted"] is False
    deps.add(store.get("p"), "b", str(b), "v1.0.0")

    lock = _lock(prj)
    assert set(lock["libraries"]) == {"a", "b"} and set(lock["packages"]) == {"c"}
    assert lock["libraries"]["b"]["deps"] == {"c": "c"}
    pc = lock["packages"]["c"]
    assert {k: pc[k] for k in ("package", "version", "names", "deps", "files")} == \
        {"package": "c", "version": "1.0.0", "names": ["c"], "deps": {}, "files": "include"}
    assert pc["commit"] == _git(c, "rev-parse", "v1.0.0") and "subdir" not in pc
    assert _fetched(log, c) == 1                                    # the second add holds c as locked
    # hard links into the store, and a marker that says who placed it
    hex = cas.read_tree(pc["tree"])["c_cell.sym"][1]
    assert (deps.dep_dir(prj.path, "c") / "c_cell.sym").stat().st_ino == cas.object_path(hex).stat().st_ino
    m = _marker(prj, "c")
    assert (m["name"], m["direct"], m["package"], m["version"], m["via"]) == ("c", False, "c", "1.0.0", ["a", "b"])
    assert _marker(prj, "a")["direct"] is True and "via" not in _marker(prj, "a")
    assert deps.locked_trees(lock)[0] >= {pc["tree"], lock["libraries"]["a"]["tree"]}


def test_conflicting_requirements_name_both_chains_and_nothing_is_written(tmp_path):
    src = tmp_path / "src"
    sar = _package(src, "sar_common")
    _package(src, "sar_common", "1.3.0", repo=sar)
    ip = _package(src, "analog_ip", "1.2.0", deps_={"sar_common": _dep(sar, "v1.3.0")})
    store, prj = _project(tmp_path)
    deps.add(prj, "sar_common", str(sar), "v1.0.0")
    before = _files(prj)

    e = _refused(lambda: deps.add(store.get("p"), "analog_ip", str(ip), "v1.2.0"), deps.ResolveError)
    c10, c13, cip = (_git(sar, "rev-parse", "v1.0.0")[:12], _git(sar, "rev-parse", "v1.3.0")[:12],
                     _git(ip, "rev-parse", "v1.2.0")[:12])
    assert [p.code for p in e.problems] == ["conflict"]
    assert str(e) == (f"sar_common: two versions are required, p -> sar_common 1.0.0 (v1.0.0 @ {c10}) and p -> "
                      f"analog_ip 1.2.0 (v1.2.0 @ {cip}) -> sar_common 1.3.0 (v1.3.0 @ {c13}): align the revs (one "
                      "version per package); nothing was changed")
    assert isinstance(e, deps.DepError) and _files(prj) == before


def test_cell_names_clash_between_packages_but_not_with_one_package_under_two_names_or_a_layout_copy(tmp_path):
    src = tmp_path / "src"
    x = _package(src, "x", cells=("comparator",))
    y = _package(src, "y", cells=("comparator", "y_cell"))
    z = _package(src, "z", deps_={"x2": _dep(x, package="x")})
    store, prj = _project(tmp_path)
    deps.add(prj, "x", str(x), "v1.0.0")
    before = _files(prj)
    e = _refused(lambda: deps.add(store.get("p"), "y", str(y), "v1.0.0"), deps.ResolveError)
    assert [p.code for p in e.problems] == ["cell_clash"]
    assert str(e) == "comparator: x 1.0.0 and y 1.0.0; nothing was changed"
    assert _files(prj) == before

    # x placed again under the local name x2 by z: one package, one set of cells
    deps.add(store.get("p"), "z", str(z), "v1.0.0")
    lock = _lock(prj)
    assert lock["libraries"]["x"]["names"] == ["x", "x2"] and lock["libraries"]["z"]["deps"] == {"x2": "x"}
    assert (deps.dep_dir(prj.path, "x2") / "comparator.sym").stat().st_ino == \
        (deps.dep_dir(prj.path, "x") / "comparator.sym").stat().st_ino

    # a layout copy of the placed comparator in a project library is no clash; a schematic of that name is
    lib = prj.create_library("mylib")
    ly = kdb.Layout()
    ly.create_cell("comparator")
    ly.write(str(lib.path / "comparator.oas"))
    deps.update(store.get("p"), "x")
    (lib.path / "comparator.sch").write_text("v {}\n")
    e = _refused(lambda: deps.update(store.get("p"), "x"), deps.ResolveError)
    assert str(e) == "comparator: x 1.0.0 and project library mylib; nothing was changed"


def test_an_alias_installs_the_package_under_both_names_sharing_inodes(tmp_path):
    src = tmp_path / "src"
    bias = _package(src, "bias_gen", "0.3.0", cells=("ref",))
    ip = _package(src, "analog_ip", "1.2.0", deps_={"bias": _dep(bias, "v0.3.0", package="bias_gen")})
    store, prj = _project(tmp_path)
    deps.add(prj, "analog_ip", str(ip), "v1.2.0")
    lock = _lock(prj)
    assert lock["libraries"]["analog_ip"]["deps"] == {"bias": "bias_gen"}
    assert lock["packages"]["bias_gen"]["names"] == ["bias", "bias_gen"]
    one, two = (deps.dep_dir(prj.path, n) / "ref.sym" for n in ("bias", "bias_gen"))
    assert one.stat().st_ino == two.stat().st_ino
    for n in ("bias", "bias_gen"):
        m = _marker(prj, n)
        assert (m["name"], m["direct"], m["via"], m["package"]) == (n, False, ["analog_ip"], "bias_gen")
    assert "bias" not in prj.library_specs() and "bias_gen" not in store.get("p").library_specs()


def test_one_local_name_for_two_packages_is_refused_naming_both(tmp_path):
    src = tmp_path / "src"
    c1, c2 = _package(src, "c1"), _package(src, "c2")
    a = _package(src, "a", deps_={"common": _dep(c1, package="c1")})
    b = _package(src, "b", deps_={"common": _dep(c2, package="c2")})
    store, prj = _project(tmp_path)
    deps.add(prj, "a", str(a), "v1.0.0")
    before = _files(prj)
    e = _refused(lambda: deps.add(store.get("p"), "b", str(b), "v1.0.0"), deps.ResolveError)
    assert [p.code for p in e.problems] == ["local_name"]
    assert str(e).startswith("common: one local name for two packages, p -> a 1.0.0 (v1.0.0 @ ")
    assert "-> c1 1.0.0 (v1.0.0 @ " in str(e) and "p -> b 1.0.0 (v1.0.0 @ " in str(e) and "-> c2 1.0.0" in str(e)
    assert _files(prj) == before


def test_an_incompatible_pdk_is_refused_directly_and_through_a_chain(tmp_path):
    pdk = Pdk("fakeA", "fake", "a" * 40, tmp_path / "pdk")
    req = f'\n[pdk]\nfamily = "fake"\nvariants = ["fakeA"]\ncommits = ["{"b" * 40}"]\n'
    src = tmp_path / "src"
    c = _package(src, "c", body=req)
    a = _package(src, "a", deps_={"c": _dep(c)})
    store, prj = _project(tmp_path, pdks={pdk.id: pdk}, pdk="fakeA")
    before = _files(prj)
    cc, ca = _git(c, "rev-parse", "v1.0.0")[:12], _git(a, "rev-parse", "v1.0.0")[:12]

    e = _refused(lambda: deps.add(prj, "c", str(c), "v1.0.0"), deps.ResolveError)
    assert [p.code for p in e.problems] == ["pdk"]
    assert str(e) == (f"c 1.0.0 does not fit this project's PDK: fakeA@aaaaaaaaaaaa is not among commits bbbbbbbbbbbb "
                      f"(required by p -> c 1.0.0 (v1.0.0 @ {cc})); nothing was changed")
    e = _refused(lambda: deps.add(store.get("p"), "a", str(a), "v1.0.0"), deps.ResolveError)
    assert e.problems[0].chains == [f"p -> a 1.0.0 (v1.0.0 @ {ca}) -> c 1.0.0 (v1.0.0 @ {cc})"]
    assert _files(prj) == before


def test_a_phase_1_entry_satisfies_a_package_requiring_its_commit_and_is_not_moved(tmp_path):
    """Mixed modes: the locked phase 1 entry (every file) and b's requirement (its include set) are one version when
    git, commit and subdir agree; the entry keeps its tree and its install."""
    src = tmp_path / "src"
    ip = _package(src, "analog_ip", "1.2.0", body='\n[files]\ninclude = ["*.sym"]\n', extra={"README": "docs\n"})
    commit = _git(ip, "rev-parse", "v1.2.0")
    b = _package(src, "b", deps_={"analog_ip": _dep(ip, commit)})           # the same commit by another rev
    store, prj = _project(tmp_path)
    entry = _phase1_entry(prj, store, "analog_ip", ip, "v1.2.0")
    readme = deps.dep_dir(prj.path, "analog_ip") / "README"
    ino = readme.stat().st_ino

    deps.add(store.get("p"), "b", str(b), "v1.0.0")
    lock = _lock(prj)
    kept = lock["libraries"]["analog_ip"]
    assert {k: kept[k] for k in entry} == entry and "files" not in kept      # its tree: every file, as locked
    assert kept["package"] == "analog_ip" and lock["libraries"]["b"]["deps"] == {"analog_ip": "analog_ip"}
    assert "packages" not in lock
    assert readme.stat().st_ino == ino                                        # not installed again


def test_a_fresh_clone_must_sync_before_add(tmp_path, monkeypatch):
    src = tmp_path / "src"
    c = _package(src, "c")
    a = _package(src, "a", deps_={"c": _dep(c)})
    plain = tmp_path / "src" / "plain"
    _put(plain, {"inv.sym": SYM}, "v1")
    store, prj = _project(tmp_path)
    deps.add(prj, "a", str(a), "v1.0.0")
    shutil.rmtree(prj.path / deps.DEPS)
    monkeypatch.setenv("UNDINE_STORE", str(tmp_path / "empty-store"))
    before = _files(prj)
    e = _refused(lambda: deps.add(store.get("p"), "plain", str(plain), "v1"))
    assert "a: not in the store or installed: run lib sync first" in str(e) and "nothing was changed" in str(e)
    assert _files(prj) == before


# ------------------------------------------------------------------ formats


def test_format_1_refuses_dependencies_and_another_name_and_adds_a_plain_package_as_a_sha_entry(tmp_path):
    src = tmp_path / "src"
    c = _package(src, "c")
    a = _package(src, "a", deps_={"c": _dep(c)})
    solo = _package(src, "solo")
    store, prj = _project(tmp_path, fmt=1)
    before = _files(prj)
    e = _refused(lambda: deps.add(prj, "a", str(a), "v1.0.0"), deps.NeedsFormat2)
    assert str(e) == ("a: package a names dependencies (c): needs lock format 2: run python -m undine lib lock p "
                      "--format 2 first; nothing was changed")
    e = _refused(lambda: deps.add(store.get("p"), "other", str(solo), "v1.0.0"), deps.NeedsFormat2)
    assert "other: package solo is also installed under its own name solo" in str(e)
    assert _files(prj) == before

    r = deps.add(store.get("p"), "solo", str(solo), "v1.0.0")
    entry = _lock(prj)["libraries"]["solo"]
    assert set(entry) == {"git", "rev", "commit", "sha"} and _lock(prj)["format"] == 1
    assert (r["package"], r["version"]) == ("solo", "1.0.0")
    assert _marker(prj, "solo") | {} == entry | {"name": "solo", "direct": True, "package": "solo", "version": "1.0.0"}


def test_relock_upgrade_writes_format_2_once_and_a_refusal_leaves_format_1(tmp_path, monkeypatch):
    src = tmp_path / "src"
    c = _package(src, "c", cells=("comparator",))
    a = _package(src, "a", deps_={"c": _dep(c)})
    x = _package(src, "x", cells=("comparator",))

    def upgrade(lock: dict) -> dict:                 # what pdkops.upgrader converts in memory (no PDK here)
        return {**lock, "format": 2, "pdk": None, "tools": copy.deepcopy(NOW)}
    writes = []
    real = deps.write_lock
    monkeypatch.setattr(deps, "write_lock", lambda p, lock: (writes.append(lock["format"]), real(p, lock)))

    # refused (the cell clash of c and x): the lock stays byte for byte, format 1
    store, prj = _project(tmp_path / "one", fmt=1)
    _edit_project(prj, lambda m: m["libraries"].update(a={"git": str(a), "rev": "v1.0.0"},
                                                       x={"git": str(x), "rev": "v1.0.0"}))
    lock = (prj.path / deps.LOCK).read_bytes()
    _refused(lambda: deps.relock(store.get("p")), deps.NeedsFormat2)
    writes.clear()
    e = _refused(lambda: deps.relock(store.get("p"), upgrade=upgrade), deps.ResolveError)
    assert str(e) == "comparator: x 1.0.0 and c 1.0.0; nothing was changed"
    assert (prj.path / deps.LOCK).read_bytes() == lock and writes == [] and _lock(prj)["format"] == 1

    # accepted: one write, of format 2, with the transitive package
    store, prj = _project(tmp_path / "two", fmt=1)
    _edit_project(prj, lambda m: m["libraries"].update(a={"git": str(a), "rev": "v1.0.0"}))
    writes.clear()
    r = deps.relock(store.get("p"), upgrade=upgrade)
    lock = _lock(prj)
    assert writes == [2] and lock["format"] == 2 and r["created"] is False
    assert lock["libraries"]["a"]["deps"] == {"c": "c"} and set(lock["packages"]) == {"c"}
    assert [x["name"] for x in r["resolved"]] == ["a", "c"]
    assert (deps.dep_dir(prj.path, "c") / "comparator.sym").is_file()
    assert deps.relock(store.get("p"))["resolved"] == []                  # complete now: nothing to resolve


def test_plain_dependencies_keep_phase_1_entries_and_a_phase_1_entry_is_not_converted(tmp_path):
    src = tmp_path / "src"
    plain = src / "plain"
    _put(plain, {"lib/inv.sym": SYM, "README": "x\n"}, "v1")
    for fmt in (1, 2):
        store, prj = _project(tmp_path / f"f{fmt}", fmt=fmt)
        deps.add(prj, "ip", str(plain), "v1", "lib")
        entry = _lock(prj)["libraries"]["ip"]
        assert set(entry) == {"git", "rev", "subdir", "commit", "sha" if fmt == 1 else "tree"}
        assert deps.installed(prj.path, "ip") == entry and "packages" not in _lock(prj)
    # a phase 1 entry of a package (format 2, no files key) stays as it is when another dependency is added
    ip = _package(src, "analog_ip", "1.2.0", body='\n[files]\ninclude = ["*.sym"]\n', extra={"README": "docs\n"})
    store, prj = _project(tmp_path / "mixed")
    entry = _phase1_entry(prj, store, "analog_ip", ip, "v1.2.0")
    deps.add(store.get("p"), "ip", str(plain), "v1", "lib")
    assert _lock(prj)["libraries"]["analog_ip"] == entry


# ------------------------------------------------------------------ security


def test_a_remote_manifest_may_not_name_a_local_repository(tmp_path, monkeypatch):
    log = _counting_git(tmp_path, monkeypatch)
    src = tmp_path / "src"
    c, d = _package(src, "c"), _package(src, "d")
    a = _package(src, "a", deps_={"c": _dep(c.as_uri()), "d": _dep(d)})
    # https://example.invalid/a is the local repository a, as a remote one (git's url.<base>.insteadOf)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{a.as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://example.invalid/a")
    store, prj = _project(tmp_path)
    before = _files(prj)
    e = _refused(lambda: deps.add(prj, "a", "https://example.invalid/a", "v1.0.0"), deps.ResolveError)
    assert [p.code for p in e.problems] == ["local_url", "local_url"]
    assert f"{c.as_uri()} is a path on this machine, and a 1.0.0 comes from https://example.invalid/a" in str(e)
    assert _fetched(log, c) == 0 and _fetched(log, d) == 0 and _fetched(log, Path("example.invalid/a")) == 1
    assert _files(prj) == before


def test_over_http_dep_root_applies_to_the_urls_a_manifest_names(tmp_path, monkeypatch):
    log = _counting_git(tmp_path, monkeypatch)
    c = _package(tmp_path / "elsewhere", "c")
    a = _package(tmp_path / "allowed", "a", deps_={"c": _dep(c)})
    store, prj = _project(tmp_path)
    before = _files(prj)
    e = _refused(lambda: deps.add(prj, "a", str(a), "v1.0.0", local_roots=[tmp_path / "allowed"]))
    assert f"{c}: {c.resolve()} is outside the directories this server fetches local repositories from" in str(e)
    assert "--dep-root" in str(e) and _fetched(log, c) == 0 and _files(prj) == before
    deps.add(store.get("p"), "a", str(a), "v1.0.0", local_roots=[tmp_path])
    assert set(_lock(prj)["packages"]) == {"c"}


def test_a_bad_manifest_and_an_index_requirement_are_refused_with_their_messages(tmp_path):
    src = tmp_path / "src"
    c = src / "c"
    _put(c, {"c_cell.sym": SYM, MANIFEST: '[package]\nname = "c"\nversion = "1.0.0"\nrevision = "main"\n'}, "v1.0.0")
    a = _package(src, "a", deps_={"c": _dep(c), "sar": '"^1.2"'})
    store, prj = _project(tmp_path)
    before = _files(prj)
    e = _refused(lambda: deps.add(prj, "a", str(a), "v1.0.0"), deps.ResolveError)
    assert sorted(p.code for p in e.problems) == ["index_dependency", "manifest"]
    text = str(e)
    assert text.startswith("2 problems with the dependencies:\n- ") and text.endswith("\nnothing was changed")
    assert "undine-package.toml: package.revision: unknown key" in text and "(required by p -> a 1.0.0" in text
    assert ("undine-package.toml: dependencies.sar: an index requirement (sar ^1.2), which comes with the package "
            'index (phase 3): name its git repository, sar = { git = "https://...", rev = "v1.2.0", '
            'version = "^1.2" }') in text
    assert _files(prj) == before


# ------------------------------------------------------------------ update, remove, trust, gc


def test_update_of_a_placed_name_is_refused_and_remove_keeps_a_name_another_package_places(tmp_path):
    src = tmp_path / "src"
    sar = _package(src, "sar_common")
    bias = _package(src, "bias_gen")
    ip = _package(src, "analog_ip", "1.2.0", deps_={"sar_common": _dep(sar), "bias": _dep(bias, package="bias_gen")})
    store, prj = _project(tmp_path)
    deps.add(prj, "sar_common", str(sar), "v1.0.0")
    deps.add(store.get("p"), "analog_ip", str(ip), "v1.2.0")
    lock = _lock(prj)
    assert set(lock["libraries"]) == {"sar_common", "analog_ip"} and set(lock["packages"]) == {"bias_gen"}

    for op in (lambda: deps.update(store.get("p"), "bias"), lambda: deps.remove(store.get("p"), "bias")):
        e = _refused(op, deps.NotDirect)
        assert str(e) == "bias: placed by analog_ip; update or remove that instead" and e.placed_by == ["analog_ip"]

    r = deps.remove(store.get("p"), "sar_common")
    assert r["kept"] == [{"name": "sar_common", "placed_by": ["analog_ip"]}] and r["removed_packages"] == []
    lock = _lock(prj)
    assert "sar_common" not in lock["libraries"] and lock["packages"]["sar_common"]["names"] == ["sar_common"]
    assert "sar_common" not in store.get("p").library_specs()
    m = _marker(prj, "sar_common")
    assert (m["direct"], m["via"]) == (False, ["analog_ip"])

    r = deps.remove(store.get("p"), "analog_ip")
    assert sorted(x["package"] for x in r["removed_packages"]) == ["analog_ip", "bias_gen", "sar_common"]
    assert r["kept"] == [] and "packages" not in _lock(prj) and _lock(prj)["libraries"] == {}
    assert not [d for d in (prj.path / deps.DEPS).iterdir() if not d.name.startswith(".")]


def test_update_moves_a_package_and_its_dependencies_and_drops_what_it_no_longer_needs(tmp_path):
    src = tmp_path / "src"
    c = _package(src, "c")
    _package(src, "c", "1.1.0", repo=c, cells=("c_cell", "c_new"))
    d = _package(src, "d")
    a = _package(src, "a", deps_={"c": _dep(c), "d": _dep(d)})
    _package(src, "a", "2.0.0", repo=a, deps_={"c": _dep(c, "v1.1.0")})
    store, prj = _project(tmp_path)
    deps.add(prj, "a", str(a), "v1.0.0")
    r = deps.update(store.get("p"), "a", "v2.0.0")
    lock = _lock(prj)
    assert set(lock["packages"]) == {"c"} and lock["packages"]["c"]["version"] == "1.1.0"
    assert not deps.dep_dir(prj.path, "d").exists() and (deps.dep_dir(prj.path, "c") / "c_new.sym").is_file()
    moved = {p["package"]: p for p in r["packages_changed"]}
    assert set(moved) == {"a", "c", "d"} and moved["d"]["to"] is None
    assert moved["c"]["changed"]["added"] == ["c_new.sym"] and moved["c"]["names"] == ["c"]
    assert prj.library_specs()["a"]["rev"] == "v1.0.0" and store.get("p").library_specs()["a"]["rev"] == "v2.0.0"


def test_trust_goes_with_an_update_to_another_commit_and_start_sim_refuses_again(tmp_path):
    from undine.server.api import Server
    from test_netlist_sources import _cell, _code, _devices, _sub
    _devices()
    repo = tmp_path / "src" / "ip"
    _cell(repo, "inv", [_code("s1", ".control\necho one\n.endc")])
    (repo / MANIFEST).write_text(_manifest("analog_ip", "1.2.0"))
    _put(repo, {}, "v1")
    _cell(repo, "inv", [_code("s1", ".control\necho two\n.endc")])
    _put(repo, {}, "v2")                                                     # other content, the same version
    srv = Server(tmp_path / "projects", {}, [tmp_path])
    srv.store.create("p").create_library("l")
    prj = srv.project("p")
    deps.write_lock(prj.path, {**deps.read_lock(prj.path), "format": 2, "pdk": None, "tools": copy.deepcopy(NOW)})
    r = deps.add(srv.project("p"), "analog_ip", str(repo), "v1")
    assert r["flagged"]["spice"] == 1 and r["trusted"] is False
    _cell(srv.project("p").library("l").path, "tb", _sub("analog_ip/inv.sym", "x1"), ports=False)
    with pytest.raises(trust.UntrustedContent):
        srv.start_sim("p", "l", "tb", {"type": "op"})

    assert trust.set_trusted(srv.project("p"), "analog_ip@1.2.0", True)
    assert not trust.untrusted_dirs(prj.path)                                # what start_sim checks: trusted
    deps.update(srv.project("p"), "analog_ip", "v2")
    assert "trusted" not in json.loads((prj.path / "project.json").read_text())
    with pytest.raises(trust.UntrustedContent) as e:
        srv.start_sim("p", "l", "tb", {"type": "op"})
    assert (e.value.name, e.value.package, e.value.key) == ("analog_ip", "analog_ip", "analog_ip@1.2.0")


def test_store_gc_keeps_a_packages_tree_that_is_locked_but_not_installed(tmp_path):
    src = tmp_path / "src"
    c = _package(src, "c")
    a = _package(src, "a", deps_={"c": _dep(c)})
    store, prj = _project(tmp_path)
    deps.add(prj, "a", str(a), "v1.0.0")
    tree = _lock(prj)["packages"]["c"]["tree"]
    shutil.rmtree(deps.dep_dir(prj.path, "c"))
    with cas.store_lock(shared=False):                                       # how `store gc` builds its live set
        live = deps.locked_trees(deps.read_lock(prj.path))
        inst = deps.installed_trees(prj.path)
        assert tree in live[0] and tree not in inst[0]
        cas.gc(live[0] | inst[0], live[1] | inst[1], held=True)
    assert cas.read_tree(tree) is not None and not cas.missing_objects(cas.read_tree(tree))


def test_a_transitive_name_becomes_direct_for_the_same_package_and_conflicts_for_another_version(tmp_path):
    src = tmp_path / "src"
    c = _package(src, "c")
    _package(src, "c", "1.1.0", repo=c)
    a = _package(src, "a", deps_={"c": _dep(c)})
    store, prj = _project(tmp_path)
    deps.add(prj, "a", str(a), "v1.0.0")
    before = _files(prj)
    e = _refused(lambda: deps.add(store.get("p"), "c", str(c), "v1.1.0"), deps.ResolveError)
    assert [p.code for p in e.problems] == ["conflict"] and "p -> a 1.0.0 (v1.0.0 @ " in str(e)
    assert _files(prj) == before

    commit = _git(c, "rev-parse", "v1.0.0")
    deps.add(store.get("p"), "c", str(c), commit)                            # the locked commit, by another rev
    lock = _lock(prj)
    assert "packages" not in lock and lock["libraries"]["c"]["rev"] == commit
    assert lock["libraries"]["c"]["commit"] == commit and lock["libraries"]["a"]["deps"] == {"c": "c"}
    assert _marker(prj, "c")["direct"] is True and "via" not in _marker(prj, "c")
    assert deps.relock(store.get("p"))["resolved"] == []                    # as project.json names it


def test_lib_lock_completes_a_phase_1_entry_whose_manifest_names_dependencies_at_its_commit(tmp_path):
    src = tmp_path / "src"
    c = _package(src, "c")
    a = _package(src, "a", deps_={"c": _dep(c)}, extra={"README": "docs\n"})
    store, prj = _project(tmp_path)
    entry = _phase1_entry(prj, store, "a", a, "v1.0.0")
    _put(a, {"later.sym": SYM}, "later")                                    # the repository moved on meanwhile
    assert deps._incomplete(store.get("p"), _lock(prj)) == {
        "a": "a: its undine-package.toml names dependencies (c) that undine.lock does not lock"}
    r = deps.relock(store.get("p"))
    assert [x["name"] for x in r["resolved"]] == ["a", "c"]
    lock = _lock(prj)
    got = lock["libraries"]["a"]
    assert got["commit"] == entry["commit"] and got["files"] == "include" and got["deps"] == {"c": "c"}
    assert set(lock["packages"]) == {"c"} and (deps.dep_dir(prj.path, "c") / "c_cell.sym").is_file()
    assert deps._incomplete(store.get("p"), lock) == {}


# ------------------------------------------------------------------ what phase 1 locked, held as it was


def _as_phase_1(monkeypatch, call):
    """Runs `call` as phase 1 did it: no manifest is read, so the entry is a plain one of every file."""
    real = gitsrc.read_file
    monkeypatch.setattr(gitsrc, "read_file", lambda url, commit, subdir, name, *a:
                        None if name == MANIFEST else real(url, commit, subdir, name, *a))
    try:
        return call()
    finally:
        monkeypatch.setattr(gitsrc, "read_file", real)


def _deps_dirs(prj) -> list[str]:
    return sorted(d.name for d in (prj.path / deps.DEPS).iterdir() if not d.name.startswith("."))


@pytest.mark.parametrize("fmt", [1, 2])
@pytest.mark.parametrize("kind", ["alias", "dependencies"])
def test_an_entry_locked_in_phase_1_is_held_as_it_was_by_commands_about_other_dependencies(tmp_path, monkeypatch,
                                                                                          fmt, kind):
    """A phase 1 entry whose manifest names another package (an alias) or names dependencies: an unrelated add and
    update (and, in format 1, lib lock, which cannot record more) work as they did, leave its entry and its install
    byte for byte, fetch nothing for its manifest and add no install under its package's name. Status notes the
    alias (and, in format 1, the dependencies); in format 2 the dependencies stay an issue until lib lock or lib
    update of it, and an explicit lib lock resolves it."""
    log = _counting_git(tmp_path, monkeypatch)
    src = tmp_path / "src"
    c = _package(src, "c2")
    ip = _package(src, "analog_ip", deps_={"c2": _dep(c)} if kind == "dependencies" else None)
    plain, more = src / "plain", src / "more"
    _put(plain, {"inv.sym": SYM}, "v1")
    _put(plain, {"inv2.sym": SYM}, "v2")
    _put(more, {"buf.sym": SYM}, "v1")
    store, prj = _project(tmp_path, fmt=fmt)
    _as_phase_1(monkeypatch, lambda: deps.add(prj, "ip", str(ip), "v1.0.0"))
    deps.add(store.get("p"), "plain", str(plain), "v1")
    entry = _lock(prj)["libraries"]["ip"]
    assert "package" not in entry and "files" not in entry
    ip_files = {k: v for k, v in _files(prj).items() if k.startswith("ip/")}
    fetched = _fetched(log, c)

    deps.add(store.get("p"), "more", str(more), "v1")
    deps.update(store.get("p"), "plain", "v2")
    if fmt == 1:
        assert deps.relock(store.get("p"))["resolved"] == []
    lock = _lock(prj)
    assert lock["libraries"]["ip"] == entry and "packages" not in lock and lock["format"] == fmt
    assert {k: v for k, v in _files(prj).items() if k.startswith("ip/")} == ip_files
    assert _deps_dirs(prj) == ["ip", "more", "plain"]                        # no analog_ip
    assert _fetched(log, c) == fetched == 0                                  # its dependency is never fetched

    st = deps.status(store.get("p"))
    said = [x for x in st["issues"] if x.startswith("ip:")]
    noted = [x for x in st["notes"] if x.startswith("ip:")]
    if kind == "dependencies" and fmt == 2:
        assert said and "names dependencies (c2)" in said[0] and not noted
    else:
        assert not said and len(noted) == 1, (st["issues"], st["notes"])
        assert ("lib lock p --format 2" if fmt == 1 else "lib update p ip") in noted[0]
    if fmt == 2:                                                             # lib lock, asked: resolved now
        assert [x["name"] for x in deps.relock(store.get("p"))["resolved"]][0] == "ip"
        assert _lock(prj)["libraries"]["ip"]["package"] == "analog_ip" and "analog_ip" in _deps_dirs(prj)


def test_a_phase_1_entry_whose_manifest_names_an_unlocked_package_is_not_resolved_by_an_unrelated_add(tmp_path,
                                                                                                    monkeypatch):
    log = _counting_git(tmp_path, monkeypatch)
    src = tmp_path / "src"
    c = _package(src, "c")
    a = _package(src, "a", deps_={"c": _dep(c)})
    o = _package(src, "o")
    store, prj = _project(tmp_path)
    entry = _phase1_entry(prj, store, "a", a, "v1.0.0")
    raw = json.dumps(_lock(prj)["libraries"]["a"], sort_keys=True)
    r = deps.add(store.get("p"), "o", str(o), "v1.0.0")
    assert [x["name"] for x in r["resolved"]] == ["o"]
    assert _fetched(log, c) == 0
    lock = _lock(prj)
    assert json.dumps(lock["libraries"]["a"], sort_keys=True) == raw and lock["libraries"]["a"] == entry
    assert "packages" not in lock
    assert "a" in deps._incomplete(store.get("p"), lock)                     # until lib lock or lib update a


def test_one_package_under_a_second_project_json_name_is_refused(tmp_path):
    """One package has one name in project.json: a second would be a libraries entry without the package's fields,
    which sync, status and remove could not tell for the same package. The first keeps working throughout."""
    src = tmp_path / "src"
    a = _package(src, "a")
    store, prj = _project(tmp_path)
    deps.add(prj, "x", str(a), "v1.0.0", package="a")
    before = _files(prj)
    for package in ("a", None):
        e = _refused(lambda: deps.add(store.get("p"), "y", str(a), "v1.0.0", package=package), deps.ResolveError)
        assert str(e) == ("y: a 1.0.0 is a dependency of this project already, as x: one package has one name in "
                          "project.json; use x; nothing was changed")
        assert _files(prj) == before
    assert [r["action"] for r in deps.sync(store.get("p"))] == ["ok", "ok"]   # x and its own name a
    assert deps.status(store.get("p"))["ok"] and deps.relock(store.get("p"))["resolved"] == []
    r = deps.remove(store.get("p"), "x")
    assert [(x["package"], x["names"]) for x in r["removed_packages"]] == [("a", ["x", "a"])]
    assert _deps_dirs(prj) == [] and _lock(prj)["libraries"] == {}


def test_a_local_name_spelled_another_way_is_refused(tmp_path):
    """IP and ip are one directory on a case-insensitive file system: removing one would take the other's install."""
    src = tmp_path / "src"
    a = _package(src, "a")
    store, prj = _project(tmp_path)
    deps.add(prj, "ip", str(a), "v1.0.0")
    before = _files(prj)
    e = _refused(lambda: deps.add(store.get("p"), "IP", str(a), "v1.0.0"))
    assert str(e) == "library IP exists (as ip): spell it ip, or choose another name; nothing was changed"
    e = _refused(lambda: deps.add(store.get("p"), "A", str(a), "v1.0.0"))   # its own name, as installed
    assert str(e).startswith("library A exists (as a)")
    assert _files(prj) == before


def test_lib_remove_never_writes_a_marker_outside_the_projects_deps(tmp_path):
    """A lock's `names` are data: one that is a path is skipped when remove rewrites the markers of what stays."""
    src = tmp_path / "src"
    c = _package(src, "c")
    a = _package(src, "a", deps_={"c": _dep(c)})
    b = _package(src, "b", deps_={"c": _dep(c)})
    store, prj = _project(tmp_path)
    deps.add(prj, "a", str(a), "v1.0.0")
    deps.add(store.get("p"), "b", str(b), "v1.0.0")
    _, q = _project(tmp_path / "q")
    other = q.path / deps.DEPS / "ip"
    other.mkdir(parents=True)
    (other / deps.MARKER).write_text('{"name": "ip"}')
    lock = _lock(prj)
    lock["packages"]["c"]["names"] = ["c", os.path.relpath(other, deps.dep_dir(prj.path, "x").parent)]
    assert lock["packages"]["c"]["names"][1].startswith("../")
    deps.write_lock(prj.path, lock)
    deps.remove(store.get("p"), "b")
    assert (other / deps.MARKER).read_text() == '{"name": "ip"}'
    assert _marker(prj, "c")["via"] == ["a"]


def test_trust_left_from_earlier_content_does_not_trust_another_repository(tmp_path):
    """A trust key is name@version, which any repository can claim: sync drops the key of what it removes, and an
    add of other content that claims a key still listed does not inherit it."""
    src = tmp_path / "src"
    good = _package(src, "analog_ip", "1.2.0")
    evil = _package(src / "evil", "analog_ip", "1.2.0", cells=("other",))
    store, prj = _project(tmp_path)
    deps.add(prj, "ip", str(good), "v1.2.0")
    assert trust.set_trusted(store.get("p"), "analog_ip@1.2.0", True)
    _edit_project(prj, lambda m: m["libraries"].pop("ip"))                   # removed by hand (a teammate's commit)
    deps.sync(store.get("p"))
    assert "trusted" not in json.loads((prj.path / "project.json").read_text())

    _edit_project(prj, lambda m: m.update(trusted=["analog_ip@1.2.0"]))      # a key left over all the same
    r = deps.add(store.get("p"), "ip2", str(evil), "v1.2.0")
    assert r["trusted"] is False
    assert "trusted" not in json.loads((prj.path / "project.json").read_text())

    # the same content resolved again keeps the trust a person gave it
    trust.set_trusted(store.get("p"), "analog_ip@1.2.0", True)
    deps.update(store.get("p"), "ip2")
    assert json.loads((prj.path / "project.json").read_text())["trusted"] == ["analog_ip@1.2.0"]
