"""Pinning a PDK (pdks/pin.py) on fake PDK trees: one tree hash per part with what gets written after install left out,
the stamp cache, verify, a prefix expanded to the full commit (the fake ciel for ls-remote), and where `pdk fetch`
lets ciel install. Nothing here reads ~/.ciel or the network."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_ciel import make_fake_ciel, read_log  # noqa: E402
from undine.home import store_root  # noqa: E402
from undine.pdks import ciel, pin  # noqa: E402
from undine.pdks.pdk import LEGACY_OSDI, installs  # noqa: E402
from undine.store import cas  # noqa: E402

SKY = "8afc8346a57fe1ab7934ba5a6056ea8b43078e71"
IHP = "89d42ee6d2fd9b50b86a1aada6fee6673c3e6008"


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


def _tree(base: Path, family: str, commit: str, variant: str, libs=("lib_a", "lib_b"), kind="ciel") -> Path:
    """A ciel-shaped install: <base>/ciel/<family>/versions/<commit>/<variant>/ with libs.tech and libs.ref/<lib>
    (kind "volare": the same under volare/; kind "plain": <base>/<variant>/, with no version)."""
    v = base / variant if kind == "plain" else base / kind / family / "versions" / commit / variant
    (v / "libs.tech/klayout/python/cells").mkdir(parents=True)
    (v / "libs.tech/klayout/python/cells/fet.py").write_text("def fet(): pass\n")
    (v / "libs.tech/ngspice").mkdir(parents=True)
    (v / "libs.tech/ngspice/models.spice").write_text(f"* {variant}\n")
    tool = v / "libs.tech/klayout/run.sh"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    for lib in libs:
        (v / "libs.ref" / lib / "spice").mkdir(parents=True)
        (v / "libs.ref" / lib / "spice" / f"{lib}.spice").write_text(f".subckt {lib}\n.ends\n")
    (v / "SOURCES").write_text("open_pdks\n")                 # a top-level file: not part of any hash
    _age(v)
    return v


def _age(d: Path, seconds: int = 3600) -> None:
    """Dates every file an hour back, as an installed PDK's are: a file written just now is "racy" and is read
    again on the next hash."""
    t = time.time_ns() - seconds * 1_000_000_000
    for p in [d, *d.rglob("*")] if d.is_dir() else [d]:
        if not p.is_symlink():
            os.utime(p, ns=(t, t))


def _pdk(base: Path, variant: str):
    return next(p for p in installs([base]) if p.variant == variant)


@pytest.fixture
def sky(tmp_path):
    _tree(tmp_path / "pdks", "sky130", SKY, "sky130A")
    return _pdk(tmp_path / "pdks", "sky130A")


@pytest.fixture
def reads(monkeypatch):
    """Counts the PDK files hashing reads."""
    seen = []
    real = pin._sha256_file

    def counting(p):
        seen.append(Path(p))
        return real(p)
    monkeypatch.setattr(pin, "_sha256_file", counting)
    return seen


# ------------------------------------------------------------------ parts and the exclusion list


def test_excluded_covers_what_is_written_into_a_pdk_after_install():
    assert pin.excluded("libs.tech/klayout/python/cells/__pycache__/x.cpython-312.pyc")
    assert pin.excluded("libs.tech/klayout/python/cells/__pycache__/README")
    assert pin.excluded("libs.tech/klayout/python/x.pyc")
    assert pin.excluded(f"{LEGACY_OSDI}/psp103.osdi")
    assert not pin.excluded(LEGACY_OSDI.rsplit("/", 1)[0] + "/models.spice")
    assert not pin.excluded("libs.tech/ngspice/osdi_notes.txt")
    assert not pin.excluded("libs.tech/klayout/python/x.py")


def test_parts_are_libs_tech_each_library_and_other_libs_dirs(tmp_path):
    v = _tree(tmp_path, "ihp-sg13", IHP, "ihp-sg13g2", libs=("sg13g2_pr", "sg13g2_stdcell"))
    (v / "libs.qa").mkdir()
    (v / "libs.ref/README").write_text("not a library\n")
    assert list(pin.parts(v)) == ["libs.qa", "libs.ref/sg13g2_pr", "libs.ref/sg13g2_stdcell", "libs.tech"]


def test_a_sky130_tree_with_pycache_still_matches(sky):
    lock = pin.pin_object(sky)
    cache = sky.root / "libs.tech/klayout/python/cells/__pycache__"
    cache.mkdir()
    (cache / "x.cpython.pyc").write_bytes(b"\0compiled")
    (sky.root / "libs.tech/klayout/python/stray.pyc").write_bytes(b"\0")
    assert pin.part_hashes(sky) == lock["tree"]
    assert pin.verify(sky, lock)["state"] == "unverified"


def test_an_ihp_tree_with_compiled_osdi_still_matches(tmp_path):
    _tree(tmp_path, "ihp-sg13", IHP, "ihp-sg13g2", libs=("sg13g2_pr",))
    ihp = _pdk(tmp_path, "ihp-sg13g2")
    lock = pin.pin_object(ihp)
    (ihp.root / LEGACY_OSDI).mkdir(parents=True)
    (ihp.root / LEGACY_OSDI / "psp103.osdi").write_bytes(b"\x7fELF")
    assert pin.part_hashes(ihp) == lock["tree"]
    assert pin.verify(ihp, lock)["state"] == "unverified"


def test_a_part_hash_is_the_store_tree_hash_of_its_files_with_the_exec_bit_as_mode(sky):
    tech = sky.root / "libs.tech"
    want = {}
    for p in sorted(tech.rglob("*")):
        if p.is_file():
            want[p.relative_to(tech).as_posix()] = ("100755" if os.stat(p).st_mode & 0o100 else "100644",
                                                    pin._sha256_file(p))
    assert want["klayout/run.sh"][0] == "100755"
    assert pin.part_hashes(sky)["libs.tech"] == cas.tree_hash(want)
    (tech / "klayout/run.sh").chmod(0o644)
    assert pin.part_hashes(sky)["libs.tech"] != cas.tree_hash(want)


def test_pin_object_has_the_full_commit_family_source_libraries_and_trees(sky):
    obj = pin.pin_object(sky)
    assert obj["id"] == f"sky130A@{SKY[:12]}"
    assert (obj["family"], obj["variant"], obj["commit"]) == ("sky130", "sky130A", SKY)
    assert obj["source"] == "ciel " + ciel.DEFAULT_SOURCE
    assert obj["libraries"] == ["lib_a", "lib_b"]
    assert sorted(obj["tree"]) == ["libs.ref/lib_a", "libs.ref/lib_b", "libs.tech"]
    assert all(h.startswith("sha256:") and len(h) == 71 for h in obj["tree"].values())


def test_volare_and_plain_installs_name_their_source(tmp_path):
    _tree(tmp_path / "v", "sky130", SKY, "sky130A", kind="volare")
    assert pin.pin_object(_pdk(tmp_path / "v", "sky130A"))["source"] == "volare"
    _tree(tmp_path / "p", "sky130", SKY, "sky130B", kind="plain")
    obj = pin.pin_object(_pdk(tmp_path / "p", "sky130B"))
    assert obj["source"] is None and obj["commit"] is None and obj["id"] == "sky130B"


def test_a_pdk_with_no_version_is_pinned_by_tree_with_a_warning(tmp_path):
    _tree(tmp_path, "sky130", SKY, "sky130A", kind="plain")
    pdk = _pdk(tmp_path, "sky130A")
    obj = pin.pin_object(pdk)
    r = pin.verify(pdk, obj)
    assert r["state"] == "unverified"
    assert any("no version" in w for w in r["warnings"])
    assert pin.cache_path(pdk).name.startswith("nocommit-") and pin.cache_path(pdk).name.endswith("-sky130A.json")


def test_a_symlink_inside_a_part_is_an_error_naming_it(sky):
    link = sky.root / "libs.ref/lib_a/spice/elsewhere.spice"
    link.symlink_to(sky.root / "libs.tech/ngspice/models.spice")
    with pytest.raises(pin.PinError, match="elsewhere.spice"):
        pin.part_hashes(sky)
    link.unlink()
    (sky.root / "libs.tech/klayout/linked").symlink_to(sky.root / "libs.ref")
    with pytest.raises(pin.PinError, match="linked"):
        pin.part_hashes(sky)


def test_a_fifo_inside_a_part_is_an_error_naming_it(sky):
    os.mkfifo(sky.root / "libs.tech/pipe")
    with pytest.raises(pin.PinError, match="pipe"):
        pin.part_hashes(sky)


def test_hashing_writes_nothing_into_the_pdk(sky):
    before = {p: p.stat().st_mtime_ns for p in sky.root.rglob("*")}
    pin.pin_object(sky)
    assert {p: p.stat().st_mtime_ns for p in sky.root.rglob("*")} == before


# ------------------------------------------------------------------ verify


def test_a_changed_pdk_file_is_reported_by_part_and_file(sky):
    lock = pin.pin_object(sky)
    spice = sky.root / "libs.ref/lib_a/spice/lib_a.spice"
    spice.write_text(".subckt lib_a x\n.ends\n")
    r = pin.verify(sky, lock)
    assert r["state"] == "modified"
    assert r["changed"] == [{"part": "libs.ref/lib_a", "added": [], "removed": [], "modified": ["spice/lib_a.spice"]}]
    (sky.root / "libs.ref/lib_a/spice/new.spice").write_text("* new\n")
    (sky.root / "libs.tech/ngspice/models.spice").unlink()
    r = pin.verify(sky, lock)
    assert r["changed"] == [
        {"part": "libs.ref/lib_a", "added": ["spice/new.spice"], "removed": [], "modified": ["spice/lib_a.spice"]},
        {"part": "libs.tech", "added": [], "removed": ["ngspice/models.spice"], "modified": []}]


def test_a_change_against_a_hash_made_elsewhere_names_the_part_and_says_the_files_are_not_known(sky):
    lock = pin.pin_object(sky)
    lock["tree"]["libs.tech"] = "sha256:" + "0" * 64
    r = pin.verify(sky, lock)
    assert r["state"] == "modified"
    assert r["changed"] == [{"part": "libs.tech", "added": [], "removed": [], "modified": []}]
    assert any("not known here" in w for w in r["warnings"])


def test_an_extra_library_verifies_and_a_missing_locked_one_is_reported(tmp_path):
    _tree(tmp_path, "sky130", SKY, "sky130A", libs=("lib_a",))
    small = _pdk(tmp_path, "sky130A")
    lock = pin.pin_object(small)
    (small.root / "libs.ref/lib_c").mkdir()
    (small.root / "libs.ref/lib_c/README").write_text("more\n")
    r = pin.verify(small, lock)
    assert (r["state"], r["changed"], r["extra_libraries"], r["missing_libraries"]) == ("unverified", [], ["lib_c"], [])
    lock["libraries"].append("lib_z")
    lock["tree"]["libs.ref/lib_z"] = "sha256:" + "1" * 64
    r = pin.verify(small, lock)
    assert r["missing_libraries"] == ["lib_z"] and r["changed"] == []


def test_another_commit_or_variant_is_other_version_and_nothing_is_hashed(sky, reads):
    lock = pin.pin_object(sky)
    reads.clear()
    for k, v in (("commit", "f" * 40), ("variant", "sky130B")):
        r = pin.verify(sky, {**lock, k: v})
        assert r["state"] == "other version"
    assert reads == []


def test_verify_refuses_a_format_1_id(sky):
    with pytest.raises(pin.PinError, match="--pin"):
        pin.verify(sky, sky.id)


def test_expected_trees_is_the_phase_6_seam():
    assert pin.expected_trees("sky130", "sky130A", SKY) is None


def test_a_profile_hash_makes_it_verified(sky, monkeypatch):
    lock = pin.pin_object(sky)
    monkeypatch.setattr(pin, "expected_trees", lambda f, v, c: dict(lock["tree"]))
    assert pin.verify(sky, lock)["state"] == "verified"


# ------------------------------------------------------------------ the stamp cache


def test_a_second_hash_reads_no_file_and_a_touched_file_is_read_again(sky, reads):
    first = pin.part_hashes(sky)
    assert len(reads) == 5
    reads.clear()
    assert pin.part_hashes(sky) == first
    assert reads == []
    f = sky.root / "libs.ref/lib_b/spice/lib_b.spice"
    os.utime(f, ns=(f.stat().st_atime_ns, f.stat().st_mtime_ns + 1_000_000_000))
    assert pin.part_hashes(sky) == first
    assert reads == [f]


def test_the_cache_is_keyed_by_commit_variant_and_real_root(sky):
    pin.part_hashes(sky)
    path = pin.cache_path(sky)
    assert path.parent == store_root() / "pdk"
    assert path.name.startswith(f"{SKY}-sky130A-") and path.is_file()
    rec = json.loads(path.read_text())
    assert rec["format"] == 1 and rec["root"] == str(sky.root.resolve())
    assert rec["parts"] == pin.part_hashes(sky)
    size, mtime, ino, ctime, mode, sha = rec["files"]["libs.tech/ngspice/models.spice"]
    assert sha == pin._sha256_file(sky.root / "libs.tech/ngspice/models.spice")
    assert all(h in rec["seen"][p] for p, h in rec["parts"].items())


def test_one_commit_under_two_roots_keeps_two_caches_and_neither_rehashes(tmp_path, reads):
    _tree(tmp_path / "r1", "sky130", SKY, "sky130A")
    _tree(tmp_path / "r2", "sky130", SKY, "sky130A")
    both = [p for p in installs([tmp_path / "r1", tmp_path / "r2"]) if p.variant == "sky130A"]
    assert len(both) == 2
    a, b = both
    assert pin.part_hashes(a) == pin.part_hashes(b)
    assert pin.cache_path(a) != pin.cache_path(b)
    assert pin.cache_path(a).is_file() and pin.cache_path(b).is_file()
    reads.clear()
    for _ in range(3):
        pin.part_hashes(a)
        pin.part_hashes(b)
    assert reads == []


def test_the_cache_is_replaced_atomically_from_the_store_tmp(sky, monkeypatch):
    pin.part_hashes(sky)
    path = pin.cache_path(sky)
    old = path.read_bytes()
    (sky.root / "libs.tech/new.txt").write_text("x\n")
    renames = []
    real = os.replace

    def watching(src, dst):
        renames.append((Path(src), Path(dst)))
        assert path.read_bytes() == old              # the old record stays whole until the rename
        return real(src, dst)
    monkeypatch.setattr(os, "replace", watching)
    pin.part_hashes(sky)
    assert renames and renames[-1][1] == path and renames[-1][0].parent == cas.tmp_dir()
    assert path.read_bytes() != old
    assert not list(cas.tmp_dir().iterdir())


def test_a_failed_cache_write_leaves_the_old_record(sky, monkeypatch):
    pin.part_hashes(sky)
    path = pin.cache_path(sky)
    old = path.read_bytes()
    (sky.root / "libs.tech/new.txt").write_text("x\n")
    monkeypatch.setattr(pin.json, "dump", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        pin.part_hashes(sky)
    assert path.read_bytes() == old and not list(cas.tmp_dir().iterdir())


def test_only_cached_without_a_cache_is_unchecked_and_reads_nothing(sky, reads):
    assert pin.part_hashes(sky, only_cached=True) is None
    lock = {"variant": "sky130A", "commit": SKY, "libraries": ["lib_a", "lib_b"],
            "tree": {"libs.tech": "sha256:" + "0" * 64}}
    assert pin.verify(sky, lock, only_cached=True)["state"] == "unchecked"
    assert reads == [] and not pin.cache_path(sky).exists()


def test_only_cached_on_a_root_that_was_never_hashed_is_unchecked_even_with_no_files(sky, reads):
    lock = {"variant": "sky130A", "commit": SKY, "libraries": [], "tree": {"libs.tech": "sha256:" + "0" * 64}}
    for p in sorted(sky.root.rglob("*"), reverse=True):
        p.unlink() if not p.is_dir() else p.rmdir()       # a root emptied (ciel rm meanwhile): nothing to walk
    assert pin.part_hashes(sky, only_cached=True) is None
    assert pin.verify(sky, lock, only_cached=True)["state"] == "unchecked"
    assert not pin.cache_path(sky).exists()


def test_only_cached_answers_from_a_warm_cache_and_turns_stale_on_any_change(sky, reads):
    lock = pin.pin_object(sky)
    reads.clear()
    assert pin.part_hashes(sky, only_cached=True) == lock["tree"]
    assert pin.verify(sky, lock, only_cached=True)["state"] == "unverified"
    (sky.root / "libs.tech/added.txt").write_text("x\n")
    assert pin.part_hashes(sky, only_cached=True) is None
    (sky.root / "libs.tech/added.txt").unlink()
    assert pin.part_hashes(sky, only_cached=True) == lock["tree"]
    (sky.root / "libs.ref/lib_a/spice/lib_a.spice").unlink()
    assert pin.part_hashes(sky, only_cached=True) is None
    assert reads == []


def test_a_cache_of_another_root_or_format_is_ignored(sky, reads):
    pin.part_hashes(sky)
    path = pin.cache_path(sky)
    rec = json.loads(path.read_text())
    path.write_text(json.dumps({**rec, "root": "/elsewhere"}))
    assert pin.part_hashes(sky, only_cached=True) is None
    path.write_text("not json")
    reads.clear()
    pin.part_hashes(sky)
    assert len(reads) == 5


def test_seen_keeps_the_last_four_hashes_of_a_part(sky):
    f = sky.root / "libs.tech/ngspice/models.spice"
    hashes = []
    for i in range(6):
        f.write_text(f"* {i}\n")
        _age(f, 3600 - i)
        hashes.append(pin.part_hashes(sky)["libs.tech"])
    seen = json.loads(pin.cache_path(sky).read_text())["seen"]["libs.tech"]
    assert list(seen) == hashes[-4:]


def test_a_file_written_just_before_the_hash_is_read_again_next_time(sky, reads):
    f = sky.root / "libs.tech/ngspice/models.spice"
    pin.part_hashes(sky)
    f.write_text("* x\n")                               # same size, same clock tick for all the cache could tell
    first = pin.part_hashes(sky)
    assert json.loads(pin.cache_path(sky).read_text())["files"]["libs.tech/ngspice/models.spice"][1] == -1
    assert pin.part_hashes(sky, only_cached=True) is None
    reads.clear()
    f.write_text("* y\n")
    assert pin.part_hashes(sky) != first
    assert reads == [f]
    _age(f)
    reads.clear()
    pin.part_hashes(sky)
    pin.part_hashes(sky)
    assert reads == [f]                                 # once, now that its stamp is old enough to trust


# ------------------------------------------------------------------ prefix -> full commit


def _two_commits(tmp_path):
    other = SKY[:12] + "0" * 28
    _tree(tmp_path, "sky130", SKY, "sky130A")
    _tree(tmp_path, "sky130", other, "sky130A")
    return installs([tmp_path]), other


def test_a_unique_prefix_is_found_among_installs(sky, tmp_path):
    found = installs([tmp_path / "pdks"])
    assert pin.resolve_commit(SKY[:12], "sky130A", found, allow_network=False) == SKY
    assert pin.resolve_commit(SKY, "sky130A", [], allow_network=False) == SKY


def test_an_ambiguous_prefix_is_refused_listing_both(tmp_path):
    found, other = _two_commits(tmp_path)
    with pytest.raises(pin.PinError) as e:
        pin.resolve_commit(SKY[:12], "sky130A", found, allow_network=False)
    assert SKY in str(e.value) and other in str(e.value)
    assert pin.resolve_commit(SKY[:13], "sky130A", found, allow_network=False) == SKY


def test_an_unknown_prefix_offline_is_refused_saying_how_to_allow_the_network(tmp_path, monkeypatch):
    log = tmp_path / "ciel.log"
    monkeypatch.setenv("UNDINE_CIEL", str(make_fake_ciel(tmp_path / "bin", [IHP], log=log)))
    with pytest.raises(pin.PinError, match="--network") as e:
        pin.resolve_commit(IHP[:12], "ihp-sg13g2", [], allow_network=False)
    assert IHP[:12] in str(e.value)
    assert read_log(log) == []                       # ciel was not run


def test_with_the_network_the_fake_ciel_ls_remote_gives_the_commit_by_family(tmp_path, monkeypatch):
    log = tmp_path / "ciel.log"
    monkeypatch.setenv("UNDINE_CIEL", str(make_fake_ciel(tmp_path / "bin", [IHP, "1" * 40], log=log)))
    assert pin.resolve_commit(IHP[:12], "ihp-sg13g2", [], allow_network=True) == IHP
    argv = read_log(log)[0]
    assert argv[0] == "ls-remote" and argv[argv.index("--pdk-family") + 1] == "ihp-sg13"
    with pytest.raises(pin.PinError, match="no ihp-sg13 commit"):
        pin.resolve_commit("abcdef0", "ihp-sg13g2", [], allow_network=True)


def test_an_ambiguous_remote_prefix_is_refused(tmp_path, monkeypatch):
    a, b = "abcdef1" + "0" * 33, "abcdef1" + "1" * 33
    monkeypatch.setenv("UNDINE_CIEL", str(make_fake_ciel(tmp_path / "bin", [a, b])))
    with pytest.raises(pin.PinError) as e:
        pin.resolve_commit("abcdef1", "sky130A", [], allow_network=True)
    assert a in str(e.value) and b in str(e.value)


def test_a_prefix_that_is_not_hex_is_refused():
    for bad in ("", "abc", "-rf", "8afc8346a57G", "a" * 41):
        with pytest.raises(pin.PinError):
            pin.resolve_commit(bad, "sky130A", [], allow_network=True)


# ------------------------------------------------------------------ fetch root


def test_a_root_with_only_volare_is_never_picked(tmp_path):
    volare = tmp_path / "volare-root"
    (volare / "volare/sky130/versions").mkdir(parents=True)
    with pytest.raises(pin.PinError) as e:
        pin.fetch_root([volare])
    assert str(volare) in str(e.value) and "--pdk-root" in str(e.value) and "PDK_ROOT" in str(e.value)
    assert pin.fetch_root([volare, tmp_path / "missing"]) == tmp_path / "missing"


def test_an_empty_dir_or_a_ciel_root_qualifies(tmp_path):
    volare = tmp_path / "volare-root"
    (volare / "volare").mkdir(parents=True)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert pin.fetch_root([volare, empty]) == empty
    both = tmp_path / "both"
    (both / "volare").mkdir(parents=True)
    (both / "ciel").mkdir()
    assert pin.fetch_root([volare, both, empty]) == both
    other = tmp_path / "other"
    (other / "stuff").mkdir(parents=True)
    with pytest.raises(pin.PinError):
        pin.fetch_root([volare, other])
    with pytest.raises(pin.PinError):
        pin.fetch_root([])
