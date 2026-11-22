"""Phase 4 of docs/design/packages.md: project.json and undine.lock merge entry by entry (vcs/depmerge.py), in the git
merge driver and in a changeset's adopt, and the adopt refuses a merged lock that no longer pins what the merged
project.json asks (changeset.py, _dependency_conflicts), then installs it from the store (test_pkg_changesets.py has
the dependency operations that make such changesets)."""
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine.store import deps  # noqa: E402
from undine.store.changeset import MergeConflict  # noqa: E402
from undine.vcs import depmerge, gitmerge  # noqa: E402

C1, C2, C3 = "1" * 40, "2" * 40, "3" * 40
TREE = "sha256:" + "a" * 64


def pj(libs: dict, **more) -> bytes:
    return (json.dumps({"name": "p", "pdk": None, "libraries": {"l": "l", **libs}, **more}, indent=2) + "\n").encode()


def lock(libs: dict, packages: dict | None = None, **more) -> bytes:
    d = {"format": 2, "pdk": None, "tools": {"klayout": {"version": "0.30.12", "from": "pixi"}}, "libraries": libs,
         **({"packages": packages} if packages is not None else {}), **more}
    return (json.dumps(d, indent=2, sort_keys=True) + "\n").encode()


def entry(commit: str, **more) -> dict:
    return {"git": "https://example.org/ip.git", "rev": "v1", "commit": commit, "tree": TREE, **more}


# ------------------------------------------------------------------ the merge


def test_kind_of_names_the_two_files_only():
    assert gitmerge.kind_of("project.json") == "project" and gitmerge.kind_of("p/undine.lock") == "lock"
    assert gitmerge.kind_of("l/project.json.bak") is None and gitmerge.kind_of("l/top.sch") == "schematic"


def test_two_sides_adding_different_dependencies_merge_cleanly_in_either_order():
    base_p, base_l = pj({}), lock({})
    a_p, a_l = pj({"amp": {"version": "^1"}}), lock({"amp": entry(C1, package="amp", version="1.0.0")})
    b_p = pj({"bias": {"git": "https://example.org/bias.git", "rev": "v2"}})
    b_l = lock({"bias": entry(C2, git="https://example.org/bias.git", rev="v2")}, {"common": entry(C3)})
    for ours_p, theirs_p, ours_l, theirs_l in ((a_p, b_p, a_l, b_l), (b_p, a_p, b_l, a_l)):
        rp = gitmerge.merge("project", base_p, ours_p, theirs_p)
        rl = gitmerge.merge("lock", base_l, ours_l, theirs_l)
        assert rp.clean and rl.clean
        assert set(json.loads(rp.data)["libraries"]) == {"l", "amp", "bias"}
        got = json.loads(rl.data)
        assert set(got["libraries"]) == {"amp", "bias"} and set(got["packages"]) == {"common"}
        assert rl.data == (json.dumps(got, indent=2, sort_keys=True) + "\n").encode()     # as write_lock writes it
    assert json.loads(gitmerge.merge("project", base_p, a_p, b_p).data)["libraries"] == \
        {"l": "l", "amp": {"version": "^1"}, "bias": {"git": "https://example.org/bias.git", "rev": "v2"}}


def test_one_entry_changed_differently_conflicts_and_keeps_ours():
    base = lock({"ip": entry(C1)})
    r = gitmerge.merge("lock", base, lock({"ip": entry(C2, rev="v2")}), lock({"ip": entry(C3, rev="v3")}))
    assert not r.clean and [c.id for c in r.open_conflicts] == ["lock:library:ip"]
    assert json.loads(r.data)["libraries"]["ip"]["commit"] == C2
    r = gitmerge.merge("project", pj({"ip": {"version": "^1"}}), pj({"ip": {"version": "^2"}}),
                       pj({"ip": {"version": "~1.4"}}))
    assert [c.what for c in r.open_conflicts] == ["library ip"]
    # removed on one side, changed on the other
    r = gitmerge.merge("lock", base, lock({}), lock({"ip": entry(C2)}))
    assert [c.id for c in r.open_conflicts] == ["lock:library:ip"]


def test_trusted_merges_as_a_set_and_the_tools_tool_by_tool():
    base = pj({}, trusted=["a@1", "b@1"])
    r = gitmerge.merge("project", base, pj({}, trusted=["a@1", "b@1", "c@1"]), pj({}, trusted=["b@1", "d@2"]))
    assert r.clean and json.loads(r.data)["trusted"] == ["b@1", "d@2", "c@1"]      # merge3.order
    tb = lock({}, tools={"klayout": {"version": "0.30.12", "from": "pixi"}, "magic": None})
    to = lock({}, tools={"klayout": {"version": "0.30.13", "from": "pixi"}, "magic": None})
    tt = lock({}, tools={"klayout": {"version": "0.30.12", "from": "pixi"}, "magic": {"version": "8.3", "from": "pixi"}})
    r = gitmerge.merge("lock", tb, to, tt)
    assert r.clean and json.loads(r.data)["tools"] == {"klayout": {"version": "0.30.13", "from": "pixi"},
                                                       "magic": {"version": "8.3", "from": "pixi"}}


def test_an_unreadable_side_is_one_conflict_for_the_file():
    r = gitmerge.merge("lock", lock({}), b"{not json", lock({"ip": entry(C1)}))
    assert [c.id for c in r.open_conflicts] == ["file"] and r.data == b"{not json"


def test_diff_lists_entries():
    d = depmerge.diff(lock({"ip": entry(C1)}), lock({"ip": entry(C2), "amp": entry(C3)}, {"common": entry(C3)}),
                      kind="lock")
    assert [(c["do"], c["id"]) for c in d["changes"]] == [
        ("add", "lock:library:amp"), ("mod", "lock:library:ip"), ("add", "lock:package:common")]
    d = depmerge.diff(pj({}), pj({"amp": {"version": "^1"}}), path="project.json")
    assert d["kind"] == "project" and [(c["do"], c["what"]) for c in d["changes"]] == [("add", "library amp")]


def test_git_setup_registers_the_driver_for_both_files():
    from undine.__main__ import GITATTRIBUTES
    assert "project.json merge=undine" in GITATTRIBUTES and "undine.lock merge=undine" in GITATTRIBUTES


# ------------------------------------------------------------------ the adopt


pytest_git = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


@pytest.fixture
def _store(fresh_store):
    return fresh_store


def _project_files(work: Path, libs: dict, entries: dict) -> None:
    meta = json.loads((work / "project.json").read_text())
    meta["libraries"] = {**meta["libraries"], **libs}
    (work / "project.json").write_text(json.dumps(meta, indent=2) + "\n")
    lk = json.loads((work / deps.LOCK).read_text()) if (work / deps.LOCK).is_file() else \
        {"format": 2, "pdk": None, "tools": {}, "libraries": {}}
    lk["libraries"] = {**lk["libraries"], **entries}
    (work / deps.LOCK).write_text(json.dumps(lk, indent=2, sort_keys=True) + "\n")


def _stored_tree(cell: str) -> str:
    """A tree recorded in the test's store with its objects: the adopt, which installs from the store only (and refuses
    an entry the store lacks), installs it."""
    from undine.store import cas
    return cas.write_tree({f"{cell}.sym": ("100644", cas.put_bytes(f"v {{}}\n* {cell}\n".encode()))})


def _base_lock(srv) -> None:
    p = srv.project("p").path
    (p / deps.LOCK).write_text(json.dumps({"format": 2, "pdk": None, "tools": {}, "libraries": {}}, indent=2,
                                          sort_keys=True) + "\n")


@pytest_git
@pytest.mark.parametrize("first", ["a", "b"])
def test_two_changesets_adding_different_dependencies_adopt_in_either_order(_store, first):
    import test_changeset as tc
    root, srv, cs = tc.setup()
    _base_lock(srv)
    a = cs.create("p", "add amp", "r", "agent:t")
    b = cs.create("p", "add bias", "r", "agent:t")
    _project_files(srv.project(a["preview"]).path, {"amp": {"git": "https://example.org/amp.git", "rev": "v1"}},
                   {"amp": entry(C1, git="https://example.org/amp.git", tree=_stored_tree("amp"))})
    _project_files(srv.project(b["preview"]).path, {"bias": {"git": "https://example.org/bias.git", "rev": "v1"}},
                   {"bias": entry(C2, git="https://example.org/bias.git", tree=_stored_tree("bias"))})
    order = (a, b) if first == "a" else (b, a)
    for m in order:
        assert not cs.apply("p", m["id"], "user")["applied"].get("deps_warning")
    p = srv.project("p").path
    assert set(json.loads((p / "project.json").read_text())["libraries"]) >= {"amp", "bias"}
    got = json.loads((p / deps.LOCK).read_text())["libraries"]
    assert got["amp"]["commit"] == C1 and got["bias"]["commit"] == C2
    for n in ("amp", "bias"):                                # installed after each adopt, from the store
        assert (deps.dep_dir(p, n) / f"{n}.sym").read_text() == f"v {{}}\n* {n}\n"


@pytest_git
def test_conflicting_requirements_are_refused_at_adopt(_store):
    import test_changeset as tc
    root, srv, cs = tc.setup()
    _base_lock(srv)
    p = srv.project("p").path
    _project_files(p, {"ip": {"git": "https://example.org/ip.git", "rev": "v1"}}, {"ip": entry(C1)})
    a = cs.create("p", "ip v2", "r", "agent:t")
    b = cs.create("p", "ip v3", "r", "agent:t")
    _project_files(srv.project(a["preview"]).path, {"ip": {"git": "https://example.org/ip.git", "rev": "v2"}},
                   {"ip": entry(C2, rev="v2", tree=_stored_tree("ip"))})
    _project_files(srv.project(b["preview"]).path, {"ip": {"git": "https://example.org/ip.git", "rev": "v3"}},
                   {"ip": entry(C3, rev="v3")})
    cs.apply("p", a["id"], "user")
    before = ((p / "project.json").read_bytes(), (p / deps.LOCK).read_bytes())
    with pytest.raises(MergeConflict) as e:
        cs.apply("p", b["id"], "user")
    paths = {f["path"] for f in e.value.files}
    assert paths == {"project.json", deps.LOCK} and "nothing was written" in str(e.value)
    assert ((p / "project.json").read_bytes(), (p / deps.LOCK).read_bytes()) == before


@pytest_git
def test_a_merge_that_leaves_the_lock_stale_is_refused_with_the_reason(_store):
    """One changeset moves the requirement, the other the locked entry: each file merges cleanly, the pair does not
    fit."""
    import test_changeset as tc
    root, srv, cs = tc.setup()
    _base_lock(srv)
    p = srv.project("p").path
    _project_files(p, {"ip": {"git": "https://example.org/ip.git", "rev": "v1"}}, {"ip": entry(C1)})
    a = cs.create("p", "require v2", "r", "agent:t")
    w = srv.project(a["preview"]).path
    meta = json.loads((w / "project.json").read_text())
    meta["libraries"]["ip"]["rev"] = "v2"                        # project.json only: the lock still pins v1
    (w / "project.json").write_text(json.dumps(meta, indent=2) + "\n")
    with pytest.raises(MergeConflict) as e:
        cs.apply("p", a["id"], "user")
    f = next(f for f in e.value.files if f["path"] == deps.LOCK)
    assert f["kind"] == "lock"
    assert "project.json asks for rev v2, undine.lock locks v1" in f["conflicts"][0]["what"]
    # a change of project.json that leaves dependencies alone adopts as before, an old problem notwithstanding
    _project_files(p, {"gone": {"git": "https://example.org/gone.git", "rev": "v1"}}, {})     # not locked: old
    b = cs.create("p", "a library", "r", "agent:t")
    w = srv.project(b["preview"]).path
    meta = json.loads((w / "project.json").read_text())
    meta["symbol_paths"] = ["sym"]
    (w / "project.json").write_text(json.dumps(meta, indent=2) + "\n")
    cs.apply("p", b["id"], "user")
    assert json.loads((p / "project.json").read_text())["symbol_paths"] == ["sym"]


@pytest_git
def test_git_merge_of_two_branches_adding_dependencies_runs_the_driver(tmp_path):
    import subprocess

    from undine.__main__ import git_setup

    def git(*a: str) -> str:
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=d, check=True,
                              capture_output=True, text=True).stdout

    d = tmp_path / "p"
    d.mkdir()
    (d / "project.json").write_bytes(pj({}))
    (d / deps.LOCK).write_bytes(lock({}))
    git("init", "-q", "-b", "main")
    git_setup(d, say=None)
    git("add", "-A")
    git("commit", "-qm", "base")
    git("switch", "-qc", "a")
    (d / "project.json").write_bytes(pj({"amp": {"version": "^1"}}))
    (d / deps.LOCK).write_bytes(lock({"amp": entry(C1, package="amp", version="1.0.0")}))
    git("commit", "-qam", "amp")
    git("switch", "-q", "main")
    (d / "project.json").write_bytes(pj({"bias": {"version": "~0.3"}}))
    (d / deps.LOCK).write_bytes(lock({"bias": entry(C2, package="bias", version="0.3.0")}))
    git("commit", "-qam", "bias")
    git("merge", "-q", "--no-edit", "a")                     # a line merge would conflict on both files
    assert set(json.loads((d / "project.json").read_text())["libraries"]) == {"l", "amp", "bias"}
    assert set(json.loads((d / deps.LOCK).read_text())["libraries"]) == {"amp", "bias"}
