"""A project's PDK operations (pdks/pdkops.py) on fake PDK trees and the fake ciel: verify and --pin (lock 1 -> 2 only
on an explicit command), a 12-character id expanded to the full commit, the status block, the fetch plan and the fetch,
and pick_pdk reading a dict that a rescan changes. Nothing here reads ~/.ciel, runs a real tool or uses the network."""
import copy
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_ciel import make_fake_ciel, read_log  # noqa: E402
from undine import tools  # noqa: E402
from undine.pdks import ciel, pdkops, pin  # noqa: E402
from undine.pdks.pdk import Pdk, discover  # noqa: E402
from undine.pdks.pin import PinError  # noqa: E402
from undine.store import deps  # noqa: E402
from undine.store.project import ProjectStore, pick_pdk  # noqa: E402

SKY = "8afc8346a57fe1ab7934ba5a6056ea8b43078e71"
SKY2 = "bdc9412b3e468c102d01b7cf6337be06ec6e9c9a"
FAR = "deadbeef00001111222233334444555566667777"            # a commit only ciel's source has
NOW = {"undine": "0.1.0 (abc1234)", "pixi_lock": "sha256:" + "1" * 64,
       "klayout": {"version": "0.30.12", "from": "pixi"}, "xschem": {"version": "3.4.5", "from": "pixi"},
       "ngspice": {"version": "47", "from": "pixi"}, "magic": {"version": "8.3.678", "from": "pixi"},
       "netgen": None}
BANNERS = {"klayout": "KLayout 0.30.12", "xschem": "XSCHEM V3.4.5",
           "ngspice": "ngspice-47 : Circuit level simulation program", "magic": "8.3.678", "netgen": None}
GIT_ENTRY = {"git": "https://example.org/ip.git", "rev": "v1", "subdir": "lib", "commit": "1" * 40, "sha": "2" * 64}


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


@pytest.fixture(autouse=True)
def _tools_here(monkeypatch):
    """The tools fixed: no tool runs, and every lock records the same."""
    monkeypatch.setattr(tools, "tools_record", lambda: copy.deepcopy(NOW))
    monkeypatch.setattr(tools, "version", lambda n: BANNERS[n])
    monkeypatch.setattr(tools, "find", lambda n: f"/opt/t/bin/{n}" if NOW[n] else None)


@pytest.fixture
def fake_ciel(tmp_path, monkeypatch):
    log = tmp_path / "ciel.log"
    exe = make_fake_ciel(tmp_path / "bin", [SKY, SKY2, FAR], log=log)
    monkeypatch.setenv("UNDINE_CIEL", str(exe))
    return log


def _age(d: Path) -> None:
    """Dates every file an hour back, as an installed PDK's are (a file written just now is read again each hash)."""
    t = time.time_ns() - 3600 * 1_000_000_000
    for p in [d, *d.rglob("*")]:
        os.utime(p, ns=(t, t))


def _tree(base: Path, commit: str, variant: str = "sky130A", libs=("lib_a", "lib_b"), kind: str = "ciel") -> Path:
    v = base / kind / "sky130" / "versions" / commit / variant
    (v / "libs.tech/ngspice").mkdir(parents=True)
    (v / "libs.tech/ngspice/models.spice").write_text(f"* {variant} {commit}\n")
    for lib in libs:
        (v / "libs.ref" / lib / "spice").mkdir(parents=True)
        (v / "libs.ref" / lib / "spice" / f"{lib}.spice").write_text(f".subckt {lib}\n.ends\n")
    _age(v)
    return v


def _store_of(tmp_path: Path, *commits: str, kind: str = "ciel") -> ProjectStore:
    base = tmp_path / "pdks"
    for c in commits:
        _tree(base, c, kind=kind)
    return ProjectStore(tmp_path / "projects", discover([base]))


def _bytes(prj) -> dict:
    return {n: (prj.path / n).read_bytes() for n in ("project.json", deps.LOCK) if (prj.path / n).exists()}


def _with_git_entry(prj) -> dict:
    lock = deps.read_lock(prj.path)
    lock["libraries"]["ip"] = dict(GIT_ENTRY)
    deps.write_lock(prj.path, lock)
    return lock


def _meta(prj, **changes) -> None:
    meta = json.loads((prj.path / "project.json").read_text())
    meta.update(changes)
    (prj.path / "project.json").write_text(json.dumps(meta))
    prj.meta = meta


# ------------------------------------------------------------------ verify and --pin


def test_verify_without_pin_writes_nothing_and_pin_writes_format_2(tmp_path):
    store = _store_of(tmp_path, SKY)
    prj = store.create("p", f"sky130A@{SKY[:12]}")
    _with_git_entry(prj)
    before = _bytes(prj)
    r = pdkops.verify(prj)
    assert r["state"] == "not pinned" and not r["wrote"] and r["format"] == 1, r
    assert "--pin" in r["warnings"][0]
    assert _bytes(prj) == before                                           # without --pin: nothing written

    r = pdkops.verify(prj, pin=True)
    assert r["state"] == "unverified" and r["wrote"] and not r["created"] and r["format"] == 2, r
    lock = deps.read_lock(prj.path)
    assert lock["format"] == 2 and lock["tools"] == NOW
    assert lock["libraries"] == {"ip": GIT_ENTRY}                          # the git entry's 'sha' as it was
    p = lock["pdk"]
    assert p["id"] == f"sky130A@{SKY[:12]}" and p["commit"] == SKY and p["family"] == "sky130"
    assert p["variant"] == "sky130A" and p["source"] == "ciel " + ciel.DEFAULT_SOURCE
    assert p["libraries"] == ["lib_a", "lib_b"]
    assert set(p["tree"]) == {"libs.tech", "libs.ref/lib_a", "libs.ref/lib_b"}
    assert all(h.startswith("sha256:") for h in p["tree"].values())
    assert json.loads(before["project.json"]) == json.loads((prj.path / "project.json").read_text())

    pinned = _bytes(prj)
    r = pdkops.verify(prj)
    assert r["state"] == "unverified" and r["changed"] == [] and not r["wrote"] and _bytes(prj) == pinned
    assert pdkops.verify(prj, pin=True)["wrote"] is False                  # pinned again as it is: the same lock


def test_a_modified_pdk_is_reported_and_pin_re_pins_it_reporting_what_changed(tmp_path):
    store = _store_of(tmp_path, SKY)
    prj = store.create("p", f"sky130A@{SKY[:12]}")
    pdkops.upgrade(prj)
    old = deps.read_lock(prj.path)["pdk"]["tree"]
    (prj.pdk.root / "libs.ref/lib_a/spice/lib_a.spice").write_text(".subckt lib_a x\n.ends\n")
    pinned = _bytes(prj)
    r = pdkops.verify(prj)
    assert r["state"] == "modified" and not r["wrote"] and _bytes(prj) == pinned, r
    assert r["changed"] == [{"part": "libs.ref/lib_a", "added": [], "removed": [], "modified": ["spice/lib_a.spice"]}]
    assert any(f"ciel rm --pdk-family sky130 {SKY}" in w for w in r["warnings"]), r

    r = pdkops.verify(prj, pin=True)
    assert r["previous"] == "modified" and r["state"] == "unverified" and r["wrote"], r
    assert r["changed"][0]["modified"] == ["spice/lib_a.spice"]
    new = deps.read_lock(prj.path)["pdk"]["tree"]
    assert new["libs.ref/lib_a"] != old["libs.ref/lib_a"] and new["libs.tech"] == old["libs.tech"]


def test_upgrade_without_a_pdk_writes_pdk_null_and_verify_pin_refuses(tmp_path):
    store = _store_of(tmp_path, SKY)
    prj = store.create("q")
    _with_git_entry(prj)
    r = pdkops.upgrade(prj)                                                # the lib lock --format 2 path
    assert r == {"wrote": True, "created": False, "format": 2, "pdk": None}
    lock = deps.read_lock(prj.path)
    assert lock == {"format": 2, "pdk": None, "tools": NOW, "libraries": {"ip": GIT_ENTRY}}
    (prj.path / deps.LOCK).unlink()
    assert pdkops.upgrade(prj)["created"] is True
    assert deps.read_lock(prj.path) == {"format": 2, "pdk": None, "tools": NOW, "libraries": {}}
    before = _bytes(prj)
    for pin_it in (True, False):
        with pytest.raises(PinError, match=r"lib lock q --format 2"):
            pdkops.verify(prj, pin=pin_it)
    assert _bytes(prj) == before


def test_a_requested_pdk_not_installed_is_refused_naming_pdk_fetch(tmp_path, fake_ciel):
    store = _store_of(tmp_path, SKY)
    prj = store.create("p", f"sky130A@{SKY[:12]}")
    _meta(prj, pdk=f"sky130A@{FAR[:12]}")
    (prj.path / deps.LOCK).unlink()
    with pytest.raises(PinError, match=rf"sky130A@{FAR[:12]} is not installed here.*--network"):
        pdkops.upgrade(prj)                                                # offline: the prefix is named
    assert read_log(fake_ciel) == []                                       # ciel never asked
    with pytest.raises(PinError, match=rf"sky130A@{FAR} is not installed here: run .*pdk fetch p"):
        pdkops.upgrade(prj, allow_network=True)                            # ls-remote gives the full commit
    assert read_log(fake_ciel) == [["ls-remote", "--pdk-family", "sky130"]]
    assert not (prj.path / deps.LOCK).exists()


def test_a_format_1_id_is_pinned_to_the_full_installed_commit(tmp_path, fake_ciel):
    store = _store_of(tmp_path, SKY, SKY2)
    prj = store.create("p", f"sky130A@{SKY2[:12]}")
    assert deps.read_lock(prj.path)["pdk"] == f"sky130A@{SKY2[:12]}"
    assert pdkops.upgrade(prj)["pdk"]["commit"] == SKY2
    assert read_log(fake_ciel) == []                                       # installed: no network needed


def test_upgrade_refuses_when_the_lock_changes_while_it_hashes(tmp_path, monkeypatch):
    store = _store_of(tmp_path, SKY, SKY2)
    prj = store.create("p", f"sky130A@{SKY[:12]}")
    real = pin.pin_object

    def slow(pdk):
        lock = deps.read_lock(prj.path)
        deps.write_lock(prj.path, {**lock, "pdk": f"sky130A@{SKY2[:12]}"})   # set-pdk elsewhere, meanwhile
        return real(pdk)
    monkeypatch.setattr(pin, "pin_object", slow)
    with pytest.raises(PinError, match="changed while the PDK was hashed"):
        pdkops.upgrade(prj)
    assert deps.read_lock(prj.path)["format"] == 1


# ------------------------------------------------------------------ the status block


def test_pdk_block_issues_and_notes(tmp_path, monkeypatch):
    store = _store_of(tmp_path, SKY, SKY2)
    prj = store.create("p", f"sky130A@{SKY[:12]}")
    block, issues, notes = pdkops.pdk_block(prj, deps.read_lock(prj.path))
    assert block["state"] == "not pinned" and block["ok"] and issues == [], (block, issues)
    assert len(notes) == 1 and "not pinned (format 1)" in notes[0] and "pdk verify p --pin" in notes[0]

    pdkops.upgrade(prj)
    block, issues, notes = pdkops.pdk_block(prj, deps.read_lock(prj.path))
    assert block == {"requested": f"sky130A@{SKY[:12]}", "locked": f"sky130A@{SKY[:12]}", "commit": SKY,
                     "installed": f"sky130A@{SKY[:12]}", "state": "unverified", "changed": [], "verified_by": None,
                     "ok": True}
    assert issues == [] and len(notes) == 1 and "unverified (trust on first use)" in notes[0]

    _meta(prj, pdk=f"sky130A@{SKY2[:12]}")                                 # project.json changed by hand
    block, issues, _ = pdkops.pdk_block(prj, deps.read_lock(prj.path))
    assert not block["ok"] and len(issues) == 1, issues
    assert issues[0].startswith(f"project.json asks for sky130A@{SKY2[:12]}, undine.lock pins sky130A@{SKY[:12]}")
    assert "set-pdk" in issues[0]
    _meta(prj, pdk="sky130A")                                              # a bare name: that variant will do
    assert pdkops.pdk_block(prj, deps.read_lock(prj.path))[0]["ok"]
    _meta(prj, pdk=f"sky130A@{SKY[:12]}")

    f = store.get("p").pdk.root / "libs.ref/lib_b/spice/lib_b.spice"
    f.write_text("changed\n")
    reads = []
    monkeypatch.setattr(pin, "_sha256_file", lambda p, real=pin._sha256_file: reads.append(p) or real(p))
    block, issues, notes = pdkops.pdk_block(prj, deps.read_lock(prj.path))
    assert block["state"] == "unchecked" and block["ok"] and issues == [] and reads == [], (block, reads)
    assert any("unchecked: run `python -m undine pdk verify p`" in n for n in notes)
    block, issues, _ = pdkops.pdk_block(prj, deps.read_lock(prj.path), only_cached=False)
    assert block["state"] == "modified" and not block["ok"], block
    assert len(issues) == 1 and "libs.ref/lib_b/spice/lib_b.spice" in issues[0] and "ciel rm" in issues[0]


def test_pdk_block_without_a_stamp_cache_reads_nothing_and_names_a_missing_pdk(tmp_path, monkeypatch):
    store = _store_of(tmp_path, SKY)
    prj = store.create("p", f"sky130A@{SKY[:12]}")
    pdkops.upgrade(prj)
    pin.cache_path(prj.pdk).unlink()
    reads, real = [], pin._sha256_file
    monkeypatch.setattr(pin, "_sha256_file", lambda p: reads.append(p))
    block, issues, notes = pdkops.pdk_block(prj, deps.read_lock(prj.path))
    assert block["state"] == "unchecked" and block["ok"] and reads == [], block
    monkeypatch.setattr(pin, "_sha256_file", real)

    lock = deps.read_lock(prj.path)
    lock["pdk"]["libraries"] = ["lib_a", "lib_b", "lib_c"]                 # locked, not installed here
    block, issues, _ = pdkops.pdk_block(prj, lock, only_cached=False)
    assert not block["ok"] and any("library lib_c is locked but not installed" in i for i in issues), issues

    lock["pdk"].update(commit=SKY2, id=f"sky130A@{SKY2[:12]}")             # pinned to a commit not installed
    _meta(prj, pdk=f"sky130A@{SKY2[:12]}")
    block, issues, _ = pdkops.pdk_block(store.get("p"), lock)
    assert block["state"] == "missing" and not block["ok"] and block["installed"] is None
    assert issues == [f"PDK sky130A@{SKY2[:12]} is not installed here: run `python -m undine pdk fetch p`"]


# ------------------------------------------------------------------ fetch


def test_fetch_plan_from_a_format_1_id_and_a_volare_pin_refused(tmp_path, fake_ciel):
    store = _store_of(tmp_path, SKY)
    prj = store.create("p", f"sky130A@{SKY[:12]}")
    root = tmp_path / "fresh"
    plan = pdkops.fetch_plan(prj, [root])
    assert plan == {"family": "sky130", "variant": "sky130A", "commit": SKY, "libraries": None,
                    "source": ciel.DEFAULT_SOURCE, "root": str(root.absolute())}
    _meta(prj, pdk=f"sky130A@{FAR[:12]}")
    (prj.path / deps.LOCK).unlink()                                        # project.json's id alone
    with pytest.raises(PinError, match="--network"):
        pdkops.fetch_plan(prj, [root])
    assert pdkops.fetch_plan(prj, [root], allow_network=True)["commit"] == FAR

    vol = _store_of(tmp_path / "v", SKY, kind="volare")
    vp = vol.create("v", f"sky130A@{SKY[:12]}")
    pdkops.upgrade(vp)
    assert deps.read_lock(vp.path)["pdk"]["source"] == "volare"
    with pytest.raises(PinError, match="from 'volare', not from ciel"):
        pdkops.fetch_plan(vp, [root])
    lock = deps.read_lock(vp.path)
    lock["pdk"]["commit"] = None
    deps.write_lock(vp.path, lock)
    with pytest.raises(PinError, match="no commit to fetch"):
        pdkops.fetch_plan(vp, [root])


def test_fetch_with_the_fake_ciel_installs_and_verifies_against_the_lock(tmp_path, fake_ciel):
    first = tmp_path / "first"
    ciel.fetch(first, "sky130", "sky130A", SKY, ["sky130_fd_pr"])         # the machine that pinned it
    store = ProjectStore(tmp_path / "projects", discover([first]))
    prj = store.create("p", f"sky130A@{SKY[:12]}")
    pdkops.verify(prj, pin=True)
    assert deps.read_lock(prj.path)["pdk"]["libraries"] == ["sky130_fd_pr"]
    pinned = _bytes(prj)

    here = tmp_path / "here"                                               # this machine: an empty ciel root
    plan = pdkops.fetch_plan(prj, [here])
    assert plan["libraries"] == ["sky130_fd_pr"] and plan["root"] == str(here.absolute())
    lines = []
    r = pdkops.fetch(prj, plan, on_output=lines.append)
    assert r["path"] == str(ciel.variant_dir(here.absolute(), "sky130", "sky130A", SKY))
    assert r["verify"]["state"] == "unverified" and r["verify"]["changed"] == [], r
    assert any("Fetching sky130" in x for x in lines)
    argv = read_log(fake_ciel)[-1]
    assert argv[:2] == ["fetch", "--pdk-root"] and argv[argv.index("--pdk-family") + 1] == "sky130"
    assert argv[argv.index("-l") + 1] == "sky130_fd_pr" and argv[-1] == SKY
    assert _bytes(prj) == pinned                                           # fetch never writes the lock

    lock = deps.read_lock(prj.path)
    lock["pdk"]["tree"]["libs.tech"] = "sha256:" + "0" * 64                # pinned elsewhere, differently
    deps.write_lock(prj.path, lock)
    r = pdkops.fetch(prj, plan)
    assert r["verify"]["state"] == "modified", r
    assert any(f"ciel rm --pdk-family sky130 {SKY}" in w for w in r["verify"]["warnings"])


def test_fetch_is_refused_with_only_a_volare_root_and_from_http_with_another_source(tmp_path, fake_ciel):
    store = _store_of(tmp_path, SKY)
    prj = store.create("p", f"sky130A@{SKY[:12]}")
    vol = tmp_path / "volare-root"
    (vol / "volare").mkdir(parents=True)
    with pytest.raises(PinError, match=r"no PDK root scanned is a ciel root.*--pdk-root"):
        pdkops.fetch_plan(prj, [vol])
    plan = dict(pdkops.fetch_plan(prj, [tmp_path / "ok"]), source="static-web:https://example.org/pdks")
    with pytest.raises(PinError, match="not ciel's default"):
        pdkops.fetch(prj, plan, http=True)
    assert read_log(fake_ciel) == []                                       # ciel never ran


# ------------------------------------------------------------------ set-pdk and pick_pdk


def test_set_pdk_on_format_2_hashes_before_meta_lock(tmp_path, monkeypatch):
    store = _store_of(tmp_path, SKY, SKY2)
    prj = store.create("p", f"sky130A@{SKY[:12]}")
    pdkops.upgrade(prj)
    held = []
    real = pin.pin_object

    def watching(pdk):
        lockfile = prj.path / ".undine" / "project.lock"
        import fcntl
        with open(lockfile, "a") as f:                                     # meta_lock is free while it hashes
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
        held.append(pdk.id)
        return real(pdk)
    monkeypatch.setattr(pin, "pin_object", watching)
    moved = prj.set_pdk(f"sky130A@{SKY2[:12]}")
    assert moved == (f"sky130A@{SKY[:12]}", f"sky130A@{SKY2[:12]}") and moved.created is False
    assert held == [f"sky130A@{SKY2[:12]}"]
    lock = deps.read_lock(prj.path)
    assert lock["format"] == 2 and lock["pdk"]["commit"] == SKY2 and lock["tools"] == NOW


def test_pick_pdk_reads_a_dict_that_another_thread_changes():
    pdks = {f"x{i}": Pdk(f"x{i}", "x", f"{i:040x}", Path(f"/nonexistent/x{i}")) for i in range(50)}
    keep = Pdk("keep", "x", "f" * 40, Path("/nonexistent/keep"))
    pdks[keep.id] = keep
    stop = threading.Event()

    def churn():
        i = 0
        while not stop.is_set():
            p = Pdk(f"y{i % 40}", "x", f"{i:040x}", Path("/nonexistent/y"))
            pdks[p.id] = p                                                 # as rescan does: add first, pop after
            pdks.pop(f"y{(i - 20) % 40}@{(i - 20) % (1 << 40):012x}", None)
            for k in [k for k in list(pdks) if k.startswith("y") and len(pdks) > 120]:
                pdks.pop(k, None)
            i += 1
    t = threading.Thread(target=churn)
    t.start()
    try:
        for _ in range(3000):
            assert pick_pdk(pdks, keep.id) is keep
            assert pick_pdk(pdks, "keep") is keep
    finally:
        stop.set()
        t.join()


def test_two_processes_fetching_one_pdk_commit_run_one_after_the_other(tmp_path, monkeypatch):
    # a fetch from the browser's job and `pdk fetch` in a terminal: the flock is the files', not one process's
    import shlex
    import subprocess
    log, times = tmp_path / "ciel.log", tmp_path / "times"
    fake = make_fake_ciel(tmp_path / "fake", [SKY], log=log)
    slow = tmp_path / "bin" / "ciel"
    slow.parent.mkdir()
    slow.write_text(f"""#!/bin/sh
case " $* " in *" fetch "*) echo "start $$" >> {shlex.quote(str(times))}; sleep 1;; esac
{shlex.quote(str(fake))} "$@"; rc=$?
case " $* " in *" fetch "*) echo "end $$" >> {shlex.quote(str(times))};; esac
exit $rc
""")
    slow.chmod(0o755)
    monkeypatch.setenv("UNDINE_CIEL", str(slow))
    store = ProjectStore(tmp_path / "projects", {})
    prj = store.create("p")
    root = tmp_path / "root"
    plan = {"family": "sky130", "variant": "sky130A", "commit": SKY, "libraries": None,
            "source": ciel.DEFAULT_SOURCE, "root": str(root)}
    code = ("import json, sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; "
            "from undine.pdks import pdkops; from undine.store.project import ProjectStore; "
            "prj = ProjectStore(Path(sys.argv[2]), {}).get('p'); "
            "print(json.dumps(pdkops.fetch(prj, json.loads(sys.argv[3]))['verify']['state']))")
    other = subprocess.Popen([sys.executable, "-c", code, str(Path(__file__).resolve().parents[1]),
                              str(tmp_path / "projects"), json.dumps(plan)], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 30
    while not times.exists() and time.monotonic() < deadline:            # the other one is inside ciel fetch
        time.sleep(0.02)
    lines = []
    r = pdkops.fetch(prj, plan, on_output=lines.append)
    out, err = other.communicate(timeout=60)
    assert other.returncode == 0, err
    assert r["verify"]["state"] == "not pinned" and json.loads(out) == "not pinned"
    marks = [x.split()[0] for x in times.read_text().splitlines()]
    assert marks == ["start", "end", "start", "end"], marks                # never two at once
    assert any("waiting for another fetch of sky130" in x for x in lines), lines
    assert pdkops.fetch_lock_path(root, "sky130", "sky130A", SKY).is_file()
    assert not any(p.name.startswith(".undine") for p in (root / "ciel/sky130/versions").iterdir())
    with pytest.raises(ciel.CielError, match="not a valid name"):
        pdkops.fetch_lock_path(root, "../x", "../xA", SKY)
