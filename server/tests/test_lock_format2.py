"""undine.lock format 2: read by every command next to format 1, kept by the ones that change a lock, tools compared as
number and origin (warnings and notes, never issues), and project.json no longer recording tools.

The tools here are fixed (tools.tools_record, tools.version and tools.find replaced), so nothing runs a real tool.
set-pdk on a format 2 lock writes the new PDK's pin object (pdks/pin.py) and keeps format 2."""
import contextlib
import copy
import hashlib
import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine import tools  # noqa: E402
from undine.pdks.pdk import Pdk  # noqa: E402
from undine.store import cas, deps  # noqa: E402
from undine.store.project import ProjectStore  # noqa: E402

NOW = {"undine": "0.1.0 (abc1234)", "pixi_lock": "sha256:" + "1" * 64,
       "klayout": {"version": "0.30.12", "from": "pixi"}, "xschem": {"version": "3.4.5", "from": "pixi"},
       "ngspice": {"version": "47", "from": "pixi"}, "magic": {"version": "8.3.678", "from": "pixi"},
       "netgen": None}
BANNERS = {"klayout": "KLayout 0.30.12", "xschem": "XSCHEM V3.4.5",
           "ngspice": "ngspice-47 : Circuit level simulation program", "magic": "8.3.678", "netgen": None}
# the pin of the fake PDK fakeA as _store_of writes it (libs.tech/models.spice), so status finds it unmodified
PIN_A = {"id": "fakeA@aaaaaaaaaaaa", "family": "fake", "variant": "fakeA", "commit": "a" * 40, "source": None,
         "libraries": [],
         "tree": {"libs.tech": cas.tree_hash({"models.spice": ("100644", hashlib.sha256(b"* a\n").hexdigest())})}}
UNCHECKED_A = "PDK fakeA@aaaaaaaaaaaa: unchecked: run `python -m undine pdk verify p`"   # status hashes no PDK


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


@pytest.fixture(autouse=True)
def _tools_here(monkeypatch):
    """The tools as this machine has them: fixed, so no tool runs and every test sees the same numbers."""
    monkeypatch.setattr(tools, "tools_record", lambda: copy.deepcopy(NOW))
    monkeypatch.setattr(tools, "version", lambda n: BANNERS[n])
    monkeypatch.setattr(tools, "find", lambda n: f"/opt/t/bin/{n}" if NOW[n] else None)


def _store_of(root: Path) -> ProjectStore:
    for d in ("a", "b"):                                                   # the fake PDKs on disk
        if not (root / d / "libs.tech").exists():
            (root / d / "libs.tech").mkdir(parents=True)
            (root / d / "libs.tech" / "models.spice").write_text(f"* {d}\n")
    pdks = {p.id: p for p in (Pdk("fakeA", "fake", "a" * 40, root / "a"), Pdk("fakeB", "fake", "b" * 40, root / "b"))}
    return ProjectStore(root / "projects", pdks)


def _format2(prj, **changes) -> dict:
    """Rewrites the project's lock as format 2 (what pdk verify --pin writes), with `changes` on top."""
    lock = deps.read_lock(prj.path)
    lock.update({"format": 2, "pdk": copy.deepcopy(PIN_A), "tools": copy.deepcopy(NOW)}, **changes)
    deps.write_lock(prj.path, lock)
    return lock


def _git(d: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          check=True, capture_output=True, text=True).stdout.strip()


def _repo(root: Path) -> Path:
    repo = root / "ip"
    (repo / "lib").mkdir(parents=True)
    (repo / "lib" / "inv.sym").write_text("v {xschem version=3.4.5 file_version=1.2}\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "v1")
    _git(repo, "tag", "v1")
    return repo


def _cli(store: ProjectStore, *argv: str) -> tuple[int, str]:
    from undine import __main__ as cli
    out = io.StringIO()
    discover, argv0 = cli.discover, sys.argv
    cli.discover = lambda roots: store.pdks
    sys.argv = ["undine", "--root", str(store.root), *argv]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                cli.cli()
                code = 0
            except SystemExit as e:
                code = e.code or 0
    finally:
        cli.discover, sys.argv = discover, argv0
    return code, out.getvalue()


def test_a_format_2_lock_is_read_and_lib_add_keeps_its_format_and_pdk_object(tmp_path):
    if not shutil.which("git"):
        pytest.skip("no git")
    store = _store_of(tmp_path)
    prj = store.create("p", "fakeA")
    _format2(prj)
    st = deps.status(prj)
    assert st["format"] == 2 and st["ok"], st
    assert st["pdk"] == {"requested": "fakeA@aaaaaaaaaaaa", "locked": "fakeA@aaaaaaaaaaaa", "commit": "a" * 40,
                         "installed": "fakeA@aaaaaaaaaaaa", "state": "unchecked", "changed": [], "verified_by": None,
                         "ok": True}
    assert st["warnings"] == [] and st["notes"] == [UNCHECKED_A]
    assert [t["locked"] for t in st["tools"]] == [NOW[n] for n in deps.TOOLS]

    deps.add(prj, "ip", str(_repo(tmp_path / "src")), "v1", "lib")
    lock = deps.read_lock(prj.path)
    assert lock["format"] == 2 and lock["pdk"] == PIN_A and lock["tools"] == NOW          # only the library added
    assert set(lock["libraries"]) == {"ip"} and deps.status(store.get("p"))["ok"]


def test_a_format_2_lock_with_no_pdk_is_read(tmp_path):
    prj = _store_of(tmp_path).create("p")
    _format2(prj, pdk=None)
    lock = deps.read_lock(prj.path)
    assert deps.lock_pdk_id(lock) is None and deps.lock_pdk(lock) is None
    st = deps.status(prj)
    assert st["pdk"] == {"requested": None, "locked": None, "commit": None, "installed": None, "state": "none",
                         "changed": [], "verified_by": None, "ok": True} and st["ok"], st


def test_the_accessors_read_either_format(tmp_path):
    prj = _store_of(tmp_path).create("p", "fakeA")
    one = deps.read_lock(prj.path)
    assert deps.lock_format(one) == 1 and deps.lock_pdk_id(one) == "fakeA@aaaaaaaaaaaa"
    assert deps.lock_pdk(one) == {"id": "fakeA@aaaaaaaaaaaa"}                 # format 1 records only the id
    t1 = deps.lock_tools(one)
    assert "undine" not in t1 and "pixi_lock" not in t1                       # format 1 records neither
    assert t1["klayout"] == {"version": "0.30.12", "from": None} and t1["netgen"] is None
    assert deps.lock_tools({"tools": {"magic": "?"}})["magic"] == {"version": None, "from": None, "text": "?"}
    two = _format2(prj)
    assert deps.lock_format(two) == 2 and deps.lock_pdk_id(two) == "fakeA@aaaaaaaaaaaa" and deps.lock_pdk(two) == PIN_A
    assert deps.lock_tools(two) == NOW
    assert deps.entry_hash({"commit": "1" * 40, "sha": "2" * 64}) == ("sha", "2" * 64)
    assert deps.entry_hash({"commit": "1" * 40, "tree": "sha256:" + "3" * 64}) == ("tree", "sha256:" + "3" * 64)


def test_today_s_commands_write_format_1_with_banners_and_a_lock_without_format_is_format_1(tmp_path):
    store = _store_of(tmp_path)
    prj = store.create("p", "fakeA")
    lock = deps.read_lock(prj.path)
    assert lock == {"format": 1, "pdk": "fakeA@aaaaaaaaaaaa", "tools": BANNERS, "libraries": {}}
    del lock["format"]
    deps.write_lock(prj.path, lock)
    assert deps.lock_format(deps.read_lock(prj.path)) == 1 and deps.status(prj)["format"] == 1
    prj.set_pdk("fakeB")
    deps.relock(store.get("p"))
    lock = json.loads((prj.path / deps.LOCK).read_text())
    assert lock == {"format": 1, "pdk": "fakeB@bbbbbbbbbbbb", "tools": BANNERS, "libraries": {}}
    assert "undine" not in lock["tools"] and "pixi_lock" not in lock["tools"]


def test_relock_on_format_2_keeps_the_pdk_object_and_records_tools_as_numbers(tmp_path):
    prj = _store_of(tmp_path).create("p", "fakeA")
    old = copy.deepcopy(NOW)
    old["klayout"] = {"version": "0.28.17", "from": "PATH"}
    old["undine"] = "0.0.9 (0000000)"
    _format2(prj, tools=old)
    deps.relock(prj)
    lock = deps.read_lock(prj.path)
    assert lock["format"] == 2 and lock["pdk"] == PIN_A and lock["tools"] == NOW
    assert set(lock["tools"]) == {"undine", "pixi_lock", *deps.TOOLS}


def test_status_on_format_1_has_one_note_and_no_undine_or_pixi_warning(tmp_path):
    prj = _store_of(tmp_path).create("p", "fakeA")
    st = deps.status(prj)
    assert st["ok"] and st["warnings"] == [], st
    assert st["notes"] == ["PDK fakeA@aaaaaaaaaaaa: not pinned (format 1): `python -m undine pdk verify p --pin` "
                           "pins it",
                           "origins, Undine version and pixi.lock are not recorded (format 1)"]
    kl = next(t for t in st["tools"] if t["name"] == "klayout")
    assert kl == {"name": "klayout", "locked": {"version": "0.30.12", "from": None}, "now": NOW["klayout"],
                  "now_path": "/opt/t/bin/klayout", "ok": True}
    lock = deps.read_lock(prj.path)
    lock["tools"]["xschem"] = "xschem, some build"                         # a banner no rule reads a number from
    deps.write_lock(prj.path, lock)
    st = deps.status(prj)
    assert st["ok"] and len(st["warnings"]) == 1 and "'xschem, some build'" in st["warnings"][0], st


def test_status_on_format_2_warns_of_another_tool_and_notes_another_commit(tmp_path):
    store = _store_of(tmp_path)
    prj = store.create("p", "fakeA")
    old = copy.deepcopy(NOW)
    old["klayout"] = {"version": "0.28.17", "from": "pixi"}                # another number
    old["magic"] = {"version": "8.3.678", "from": "PATH"}                  # another origin
    old["undine"] = "0.1.0 (fff0000+dirty)"                                # the same Undine, another commit
    _format2(prj, tools=old)
    st = deps.status(prj)
    assert st["ok"] and st["issues"] == [], st
    assert st["warnings"] == ["klayout: locked 0.28.17 from pixi, here 0.30.12 from pixi (/opt/t/bin/klayout)",
                              "magic: locked 8.3.678 from PATH, here 8.3.678 from pixi (/opt/t/bin/magic)"]
    assert st["notes"] == [UNCHECKED_A, "Undine: locked 0.1.0 (fff0000+dirty), here 0.1.0 (abc1234)"]
    assert [t["ok"] for t in st["tools"]] == [False, True, True, False, True]
    code, out = _cli(store, "lib", "status", "p")
    assert code == 0, out                                                  # warnings never fail it
    lines = out.splitlines()
    assert "~ klayout: locked 0.28.17 from pixi, here 0.30.12 from pixi (/opt/t/bin/klayout)" in lines
    assert "  note: Undine: locked 0.1.0 (fff0000+dirty), here 0.1.0 (abc1234)" in lines
    assert "  note: " + UNCHECKED_A in lines
    assert lines.index("~ " + st["warnings"][-1]) < lines.index("  note: " + st["notes"][0]) < lines.index("as locked")

    old["undine"] = "0.0.9 (abc1234)"                                      # another Undine version
    old["pixi_lock"] = None                                                # locked by an Undine without pixi
    _format2(prj, tools=old)
    st = deps.status(prj)
    assert st["ok"] and any(w.startswith("Undine: locked 0.0.9") for w in st["warnings"]), st
    assert any(w.startswith("tools not pinned") for w in st["warnings"]), st

    lock = deps.read_lock(prj.path)                                        # an issue still fails it
    lock["pdk"] = dict(PIN_A, id="fakeB@bbbbbbbbbbbb")
    deps.write_lock(prj.path, lock)
    code, out = _cli(store, "lib", "status", "p")
    assert code == 1 and "! project.json asks for fakeA@aaaaaaaaaaaa, undine.lock pins fakeB@bbbbbbbbbbbb" in out, out


def test_status_checks_a_tree_entry_through_the_store(tmp_path):
    prj = _store_of(tmp_path).create("p")
    d = deps.dep_dir(prj.path, "ip")
    d.mkdir(parents=True)
    (d / "inv.sym").write_text("v {}\n")
    (d / "sub").mkdir()
    (d / "sub" / "x.sch").write_text("x\n")
    commit = "1" * 40
    (d / deps.MARKER).write_text(json.dumps({"git": "https://example.org/ip.git", "commit": commit}))
    files = {"inv.sym": ("100644", cas.put_bytes(b"v {}\n")), "sub/x.sch": ("100644", cas.put_bytes(b"x\n"))}
    tree = cas.tree_hash(files)
    meta = json.loads((prj.path / "project.json").read_text())
    meta["libraries"]["ip"] = {"git": "https://example.org/ip.git"}
    (prj.path / "project.json").write_text(json.dumps(meta))
    _format2(prj, pdk=None, libraries={"ip": {"git": "https://example.org/ip.git", "commit": commit, "tree": tree}})

    def state():
        p = prj.__class__(prj.path, prj.pdks)
        return next(x["state"] for x in deps.status(p)["libraries"] if x["name"] == "ip")

    assert state() == "ok"                                                 # no record: the files hashed as 100644
    assert cas.write_tree(files) == tree
    assert state() == "ok"                                                 # the record
    (d / "sub" / "x.sch").write_text("y\n")
    assert state() == "modified"
    cas.tree_path(tree).chmod(0o644)
    cas.tree_path(tree).unlink()
    assert state() == "modified"


def test_set_pdk_on_a_format_2_lock_writes_the_new_pin_and_on_format_1_the_id(tmp_path):
    for d in ("a", "b"):                                                   # the fake PDKs on disk, to be hashed
        (tmp_path / d / "libs.tech").mkdir(parents=True)
        (tmp_path / d / "libs.tech" / "models.spice").write_text(f"* {d}\n")
    store = _store_of(tmp_path)
    prj = store.create("p", "fakeA")
    _format2(prj, libraries={"ip": {"git": "https://example.org/ip.git", "commit": "1" * 40, "sha": "2" * 64}})
    before = deps.read_lock(prj.path)
    moved = prj.set_pdk("fakeB")
    assert moved == ("fakeA@aaaaaaaaaaaa", "fakeB@bbbbbbbbbbbb") and moved.created is False
    lock = deps.read_lock(prj.path)
    assert lock["format"] == 2 and lock["tools"] == NOW and lock["libraries"] == before["libraries"]
    assert lock["pdk"] == {"id": "fakeB@bbbbbbbbbbbb", "family": "fake", "variant": "fakeB", "commit": "b" * 40,
                           "source": None, "libraries": [],
                           "tree": {"libs.tech": cas.tree_hash({"models.spice": ("100644",
                                                                                 cas.put_bytes(b"* b\n"))})}}
    meta = json.loads((prj.path / "project.json").read_text())
    assert meta["pdk"] == "fakeB@bbbbbbbbbbbb" and meta["pdk_history"][-1]["to"] == "fakeB@bbbbbbbbbbbb"
    assert deps.status(store.get("p"))["pdk"]["ok"]

    pinned = (prj.path / deps.LOCK).read_bytes()
    assert prj.set_pdk("fakeB") == ("fakeB@bbbbbbbbbbbb", "fakeB@bbbbbbbbbbbb")   # the same PDK: its pin kept
    assert (prj.path / deps.LOCK).read_bytes() == pinned
    code, out = _cli(store, "set-pdk", "p", "fakeA")
    assert code == 0 and "fakeB@bbbbbbbbbbbb -> fakeA@aaaaaaaaaaaa" in out, out
    lock = deps.read_lock(prj.path)
    assert lock["format"] == 2 and lock["pdk"]["id"] == "fakeA@aaaaaaaaaaaa" and lock["pdk"]["commit"] == "a" * 40

    other = store.create("q", "fakeA")
    assert other.set_pdk("fakeB") == ("fakeA@aaaaaaaaaaaa", "fakeB@bbbbbbbbbbbb")
    assert deps.read_lock(other.path)["pdk"] == "fakeB@bbbbbbbbbbbb" and deps.read_lock(other.path)["format"] == 1
    (other.path / deps.LOCK).unlink()
    assert other.set_pdk("fakeA").created is True                          # a new lock: format 1, the id
    assert deps.read_lock(other.path)["pdk"] == "fakeA@aaaaaaaaaaaa" and deps.read_lock(other.path)["format"] == 1


def test_project_json_records_no_tools_and_an_old_key_is_left_alone(tmp_path):
    store = _store_of(tmp_path)
    prj = store.create("p", "fakeA")
    assert "tools" not in json.loads((prj.path / "project.json").read_text())
    assert "tools" not in prj.public()
    meta = json.loads((prj.path / "project.json").read_text())
    meta["tools"] = {"klayout": "0.28"}                                    # written by an older Undine
    (prj.path / "project.json").write_text(json.dumps(meta))
    prj = store.get("p")
    assert "tools" not in prj.public()
    prj.create_library("mine")                                             # a save of project.json
    prj.set_pdk("fakeB")
    assert json.loads((prj.path / "project.json").read_text())["tools"] == {"klayout": "0.28"}
