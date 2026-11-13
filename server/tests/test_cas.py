"""The content-addressed store (undine/store/cas.py) and where it lives (undine/home.py)."""
import errno
import hashlib
import io
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from undine import home
from undine.agents import llm
from undine.store import cas, deps

SERVER = str(Path(__file__).resolve().parents[1])
A, B, C, D = "a" * 64, "b" * 64, "c" * 64, "d" * 64


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


def _run(code: str, *args: str) -> subprocess.Popen:
    env = os.environ | {"PYTHONPATH": SERVER}
    return subprocess.Popen([sys.executable, "-c", code, *args], env=env, stdout=subprocess.PIPE, text=True)


def _tree(d: Path, files: dict[str, bytes]) -> Path:
    for rel, data in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return d


def _objects() -> list[Path]:
    base = home.store_root() / "objects"
    return [p for p in base.rglob("*") if p.is_file()] if base.is_dir() else []


def _tmp_entries() -> list[Path]:
    t = home.store_root() / "tmp"
    return list(t.iterdir()) if t.is_dir() else []


# ------------------------------------------------------------------ home


def test_home_and_store_follow_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("UNDINE_HOME", str(tmp_path / "h"))
    monkeypatch.delenv("UNDINE_STORE")
    assert home.home() == tmp_path / "h"
    assert home.store_root() == tmp_path / "h" / "store"
    assert llm._home() == tmp_path / "h"
    monkeypatch.setenv("UNDINE_STORE", str(tmp_path / "s"))
    assert home.store_root() == tmp_path / "s"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("UNDINE_STORE", "rel")
    assert home.store_root() == tmp_path / "rel"                     # absolute, whatever the cwd is later
    monkeypatch.delenv("UNDINE_HOME")
    assert home.home() == Path.home() / ".undine"


# ------------------------------------------------------------------ hashes


def test_tree_hash_golden_and_order_by_utf8_bytes():
    files = {"é": ("100644", D), "b": ("100644", C), "a/b": ("100755", B), "a.b": ("100644", A)}
    # "a.b" < "a/b" < "b" < "é" in UTF-8 bytes ('.' 0x2e, '/' 0x2f, 'é' 0xc3 0xa9)
    want = "".join(f"{m} {h} {p}\x00" for m, h, p in
                   [("100644", A, "a.b"), ("100755", B, "a/b"), ("100644", C, "b"), ("100644", D, "é")]).encode()
    assert cas.tree_hash(files) == "sha256:" + hashlib.sha256(want).hexdigest()
    assert cas.tree_hash(files) == "sha256:2aa70db8121e3542751e1db0dde00d4e1a66e1a4c9012759502fbbba6dd2c285"
    assert cas.tree_hash(dict(reversed(files.items()))) == cas.tree_hash(files)
    assert cas.tree_hash(files | {"b": ("100755", C)}) != cas.tree_hash(files)        # the mode is hashed


@pytest.mark.parametrize("bad", ["/etc/passwd", "../x", "a/../b", "a//b", "./a", "", "a/", "a\0b"])
def test_tree_paths_that_leave_the_directory_are_refused(bad):
    with pytest.raises(cas.StoreError):
        cas.tree_hash({bad: ("100644", A)})


def test_bad_entries_are_refused():
    for entry in [("100664", A), ("100644", "A" * 64), ("100644", "a" * 40), ("100644",)]:
        with pytest.raises(cas.StoreError):
            cas.tree_hash({"x": entry})
    with pytest.raises(cas.StoreError):
        cas.object_path("../../etc")
    with pytest.raises(cas.StoreError):
        cas.tree_path(A)                                                  # a tree id has its algorithm


def test_archive_sha_is_deps_tree_sha(tmp_path):
    content = {"a/b": b"1", "a-b/x": b"2", "a.b": b"3", "z/y/x.sch": b"4", "é.sym": b"5",
               "sub/.undine-dep.json": b"nested marker", ".undine-dep.json": b"{}", "B": b"6"}
    d = _tree(tmp_path / "dep", content)
    files = {rel: ("100644", hashlib.sha256(data).hexdigest()) for rel, data in content.items()}
    assert cas.archive_sha(files) == deps.tree_sha(d)
    files.pop("sub/.undine-dep.json")
    assert cas.archive_sha(files) == deps.tree_sha(d)                  # a marker at any depth is not hashed
    files["a/b"] = ("100755", files["a/b"][1])
    assert cas.archive_sha(files) == deps.tree_sha(d)                  # nor is the mode


# ------------------------------------------------------------------ objects


def test_put_twice_keeps_one_read_only_object():
    h1 = cas.put_bytes(b"hello\n")
    h2, size = cas.put_file(io.BytesIO(b"hello\n"))
    assert h1 == h2 == "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03" and size == 6
    p = cas.object_path(h1)
    assert p == home.store_root() / "objects" / "sha256" / h1[:2] / h1[2:]
    assert _objects() == [p] and stat.S_IMODE(p.stat().st_mode) == 0o444
    assert _tmp_entries() == []
    assert cas.check_object(h1) and cas.check_object(h1, 6) and not cas.check_object(h1, 7)


def test_concurrent_puts_of_one_object():
    code = "import sys\nfrom undine.store import cas\nfor _ in range(200): cas.put_bytes(b'same' * 50000)\n"
    procs = [_run(code) for _ in range(3)]
    assert all(p.wait(60) == 0 for p in procs)
    hex = hashlib.sha256(b"same" * 50000).hexdigest()
    assert _objects() == [cas.object_path(hex)]
    assert cas.check_object(hex) and _tmp_entries() == []


# ------------------------------------------------------------------ trees


def test_tree_records_round_trip_and_are_checked_against_their_name():
    files = {"lib/a.sch": ("100644", cas.put_bytes(b"a")), "run.sh": ("100755", cas.put_bytes(b"#!"))}
    tid = cas.write_tree(files)
    assert tid == cas.tree_hash(files) and cas.read_tree(tid) == files
    assert stat.S_IMODE(cas.tree_path(tid).stat().st_mode) == 0o444
    sha = cas.write_archive_tree(files | {"x/.undine-dep.json": ("100644", A)})
    assert sha == cas.archive_sha(files) and cas.read_archive_tree(sha) == files      # the marker is not recorded
    assert cas.read_tree("sha256:" + B) is None
    assert cas.missing_objects(files) == [] and cas.missing_objects(files | {"x": ("100644", A), "y": ("100644", A)}) == [A]
    # a record whose content does not hash to its name is as good as absent
    other = {"lib/a.sch": ("100644", cas.put_bytes(b"evil"))}
    p = cas.tree_path(tid)
    p.chmod(0o644)
    p.write_text(cas._record(other).decode())
    assert cas.read_tree(tid) is None
    p.write_text("not json")
    assert cas.read_tree(tid) is None
    assert cas.write_tree(files) == tid and cas.read_tree(tid) == files                 # rewritten whole
    ap = cas.archive_path(sha)
    ap.chmod(0o644)
    ap.write_text(cas._record(other).decode())
    assert cas.read_archive_tree(sha) is None
    assert _tmp_entries() == []


def test_put_dir_imports_an_install(tmp_path):
    d = _tree(tmp_path / "dep", {"a.sch": b"a", "sub/b.sym": b"b", "sub/.undine-dep.json": b"x", ".undine-dep.json": b"{}"})
    (d / "link").symlink_to(d / "a.sch")
    files = cas.put_dir(d)
    assert files == {"a.sch": ("100644", hashlib.sha256(b"a").hexdigest()),
                     "sub/b.sym": ("100644", hashlib.sha256(b"b").hexdigest())}
    (d / "link").unlink()
    assert cas.archive_sha(files) == deps.tree_sha(d)
    assert cas.missing_objects(files) == []


# ------------------------------------------------------------------ install


def _stored(content: dict[str, bytes]) -> dict:
    return {rel: ("100644", cas.put_bytes(data)) for rel, data in content.items()}


def test_install_links_read_only_files(tmp_path):
    files = _stored({"a.sch": b"a", "sub/b.sym": b"b", "sub/c.sym": b"b"})
    cas.install(files, tmp_path / "st")
    for rel, (_, hex) in files.items():
        f = tmp_path / "st" / rel
        assert f.stat().st_ino == cas.object_path(hex).stat().st_ino
        assert stat.S_IMODE(f.stat().st_mode) == 0o444
    assert cas.diff_installed(tmp_path / "st", files) == {"added": [], "removed": [], "modified": []}
    assert cas.link_or_copy(cas.object_path(files["a.sch"][1]), tmp_path / "x") is True


def test_install_copies_when_asked_or_when_linking_fails(tmp_path, monkeypatch):
    files = _stored({"a.sch": b"a"})
    obj = cas.object_path(files["a.sch"][1])
    monkeypatch.setenv("UNDINE_STORE_LINK", "copy")
    cas.install(files, tmp_path / "copied")
    f = tmp_path / "copied" / "a.sch"
    assert f.stat().st_ino != obj.stat().st_ino and f.read_bytes() == b"a"
    assert stat.S_IMODE(f.stat().st_mode) == 0o444
    assert cas.link_or_copy(obj, tmp_path / "y") is False
    monkeypatch.setenv("UNDINE_STORE_LINK", "link")

    def no_link(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")
    monkeypatch.setattr(os, "link", no_link)
    cas.install(files, tmp_path / "xdev")
    f = tmp_path / "xdev" / "a.sch"
    assert f.stat().st_ino != obj.stat().st_ino and stat.S_IMODE(f.stat().st_mode) == 0o444
    assert cas.link_or_copy(obj, tmp_path / "z") is False
    monkeypatch.setenv("UNDINE_STORE_LINK", "hardlink")
    with pytest.raises(cas.StoreError, match="UNDINE_STORE_LINK"):
        cas.link_or_copy(obj, tmp_path / "w")


def test_an_object_edited_in_place_is_caught(tmp_path):
    files = _stored({"a.sch": b"good", "b.sch": b"fine"})
    bad = cas.object_path(files["a.sch"][1])
    bad.chmod(0o644)
    bad.write_bytes(b"edited")
    with pytest.raises(cas.StoreError) as e:
        cas.install(files, tmp_path / "st")
    assert e.value.paths == ["a.sch"] and not bad.exists()
    assert not (tmp_path / "st" / "b.sch").exists()                     # nothing linked from a bad set
    assert cas.missing_objects(files) == [files["a.sch"][1]]
    # verify finds it too
    files = _stored({"a.sch": b"good"})
    cas.write_tree(files)
    bad.chmod(0o644)
    bad.write_bytes(b"edited")
    r = cas.verify(dry_run=True)
    assert r["bad"] == [files["a.sch"][1]] and bad.exists()
    r = cas.verify()
    assert r["bad"] == [files["a.sch"][1]] and not bad.exists()
    assert r["broken_trees"] == [cas.tree_hash(files)]                  # it names an object no longer there


def test_verify_restores_the_mode_of_a_good_object():
    hex = cas.put_bytes(b"x")
    cas.object_path(hex).chmod(0o644)
    assert not cas.check_object(hex)
    r = cas.verify()
    assert r == {"checked": 1, "bad": [], "wrong_mode": [hex], "broken_trees": []}
    assert cas.check_object(hex)


def test_install_restores_the_mode_of_a_good_object_changed_through_a_project(tmp_path):
    files = _stored({"a.sch": b"a", "b.sch": b"b"})
    cas.install(files, tmp_path / "A")
    obj = cas.object_path(files["a.sch"][1])
    (tmp_path / "A" / "a.sch").chmod(0o644)                              # the same inode: the object's mode too
    assert stat.S_IMODE(obj.stat().st_mode) == 0o644
    before = sorted(_objects())
    cas.install(files, tmp_path / "B")
    assert sorted(_objects()) == before                                 # nothing removed
    assert (tmp_path / "B" / "a.sch").stat().st_ino == obj.stat().st_ino
    assert stat.S_IMODE(obj.stat().st_mode) == 0o444
    assert stat.S_IMODE((tmp_path / "A" / "a.sch").stat().st_mode) == 0o444


def test_install_accepts_a_good_object_whose_mode_does_not_stick(tmp_path, monkeypatch):
    # a file system that ignores chmod (a drvfs mount without metadata): the hash alone vouches for the object
    files = _stored({"a.sch": b"a"})
    obj = cas.object_path(files["a.sch"][1])
    obj.chmod(0o644)
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    cas.install(files, tmp_path / "st")
    assert obj.exists() and (tmp_path / "st" / "a.sch").read_bytes() == b"a"


def test_diff_installed(tmp_path):
    files = _stored({"a.sch": b"a", "b.sch": b"b", "c.sch": b"c"})
    d = tmp_path / "st"
    cas.install(files, d)
    (d / "b.sch").unlink()
    (d / "c.sch").unlink()
    (d / "c.sch").write_bytes(b"changed")
    (d / "new.sch").write_bytes(b"n")
    (d / ".undine-dep.json").write_text("{}")
    (d / "s").mkdir()
    (d / "s" / ".undine-dep.json").write_text("{}")
    assert cas.diff_installed(d, files) == {"added": ["new.sch"], "removed": ["b.sch"], "modified": ["c.sch"]}


# ------------------------------------------------------------------ projects and gc


def test_registered_projects_drops_a_deleted_project(tmp_path):
    (tmp_path / "p1").mkdir()
    (tmp_path / "p2").mkdir()
    cas.register_project(tmp_path / "p1")
    cas.register_project(tmp_path / "p2")
    cas.register_project(tmp_path / "p2")                             # once per project
    assert cas.registered_projects() == sorted([tmp_path / "p1", tmp_path / "p2"], key=lambda p: cas._project_record(str(p)))
    (tmp_path / "p2").rmdir()
    assert cas.registered_projects() == [tmp_path / "p1"]
    assert len(list((home.store_root() / "projects").iterdir())) == 1


def test_gc_keeps_live_trees_and_frees_the_rest():
    live = _stored({"a.sch": b"live", "shared.sym": b"shared"})
    dead = _stored({"b.sch": b"dead!", "shared.sym": b"shared"})
    old = _stored({"c.sch": b"old format"})
    live_id, dead_id = cas.write_tree(live), cas.write_tree(dead)
    old_sha = cas.write_archive_tree(old)
    stale = cas.tmp_dir() / "obj-crashed"
    stale.write_bytes(b"12345")
    os.utime(stale, (time.time() - 2 * 86400,) * 2)
    fresh = cas.tmp_dir() / "obj-writing"
    fresh.write_bytes(b"1")
    dry = cas.gc({live_id}, {old_sha}, dry_run=True)
    assert cas.read_tree(dead_id) == dead and stale.exists()
    r = cas.gc({live_id}, {old_sha})
    assert r == dry
    assert r["trees"] == 1 and r["objects"] == 1
    assert r["bytes"] == len(b"dead!") + len(cas._record(dead)) + 5
    assert cas.read_tree(live_id) == live and cas.read_archive_tree(old_sha) == old and cas.read_tree(dead_id) is None
    assert cas.missing_objects(live) == cas.missing_objects(old) == [] and cas.missing_objects(dead) == [dead["b.sch"][1]]
    assert not stale.exists() and fresh.exists()
    r = cas.gc(set(), set())
    assert r["trees"] == 2 and r["objects"] == 3 and _objects() == []


def test_gc_does_not_count_what_an_install_still_links(tmp_path):
    files = _stored({"a.sch": b"linked"})
    cas.write_tree(files)
    cas.install(files, tmp_path / "st")
    r = cas.gc(set(), set())
    assert r["objects"] == 1 and r["bytes"] == len(cas._record(files))
    assert (tmp_path / "st" / "a.sch").read_bytes() == b"linked"


def test_gc_waits_for_a_shared_lock():
    code = ("import sys, time\nfrom undine.store import cas\n"
            "with cas.store_lock(shared=True):\n    print('held', flush=True)\n    time.sleep(1.5)\n")
    p = _run(code)
    assert p.stdout.readline().strip() == "held"
    t0 = time.monotonic()
    with cas.store_lock(shared=True):                                  # shared locks do not wait for each other
        assert time.monotonic() - t0 < 1.0
    cas.gc(set(), set())
    assert time.monotonic() - t0 >= 1.0
    assert p.wait(30) == 0
