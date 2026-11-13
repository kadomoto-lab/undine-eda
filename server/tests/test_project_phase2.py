"""Phase 2 in project.py, compat and pdkops: dependency libraries from install markers (transitive names, package
versions, [exports]), the deps directory on the symbol path, create_library against installed names, set-pdk refused
for locked packages that do not accept the new PDK, and pdk verify --pin / lib lock --format 2 reporting them.

The tools are fixed (as in test_lock_format2.py), so nothing runs a real tool; the fake PDKs fakeA and fakeB are
directories under tmp_path, and the store is the test's own."""
import copy
import json
import os
from pathlib import Path

import pytest

from undine import tools
from undine.pdks import compat, pdkops
from undine.pdks.pdk import Pdk
from undine.schematics import netlist
from undine.store import cas, deps
from undine.store import manifest as mf
from undine.store import project as project_mod
from undine.store.project import ProjectStore

NOW = {"undine": "0.1.0 (abc1234)", "pixi_lock": "sha256:" + "1" * 64,
       "klayout": {"version": "0.30.12", "from": "pixi"}, "xschem": {"version": "3.4.5", "from": "pixi"},
       "ngspice": {"version": "47", "from": "pixi"}, "magic": {"version": "8.3.678", "from": "pixi"},
       "netgen": None}
BANNERS = {"klayout": "KLayout 0.30.12", "xschem": "XSCHEM V3.4.5",
           "ngspice": "ngspice-47 : Circuit level simulation program", "magic": "8.3.678", "netgen": None}
A, B = "a" * 40, "b" * 40
IP_COMMIT = "229914698b1f" + "0" * 28
SAR_COMMIT = "5b1c0e2a9d11" + "0" * 28


@pytest.fixture(autouse=True)
def _store(fresh_store, monkeypatch):
    monkeypatch.setenv("UNDINE_STORE_LINK", "copy")      # installs of their own: changing one leaves the store alone
    return fresh_store


@pytest.fixture(autouse=True)
def _tools_here(monkeypatch):
    monkeypatch.setattr(tools, "tools_record", lambda: copy.deepcopy(NOW))
    monkeypatch.setattr(tools, "version", lambda n: BANNERS[n])
    monkeypatch.setattr(tools, "find", lambda n: f"/opt/t/bin/{n}" if NOW[n] else None)
    monkeypatch.setattr(tools, "xschem_library", lambda: None)


def _store_of(root: Path) -> ProjectStore:
    for d in ("a", "b"):
        (root / d / "libs.tech").mkdir(parents=True, exist_ok=True)
        (root / d / "libs.tech" / "models.spice").write_text(f"* {d}\n")
    pdks = {p.id: p for p in (Pdk("fakeA", "fake", A, root / "a"), Pdk("fakeB", "fake", B, root / "b"))}
    return ProjectStore(root / "projects", pdks)


def _manifest(name: str, version: str, commits=(A,), exports=None, pdk=True) -> bytes:
    text = f'[package]\nname = "{name}"\nversion = "{version}"\n'
    if pdk:
        text += ('[pdk]\nfamily = "fake"\nvariants = ["fakeA", "fakeB"]\n'
                 f"commits = [{', '.join(repr(c).replace(chr(39), chr(34)) for c in commits)}]\n")
    if exports is not None:
        text += f"[exports]\ncells = [{', '.join(chr(34) + c + chr(34) for c in exports)}]\n"
    return text.encode()


def _files(manifest: bytes | None, cells=("comparator",)) -> dict:
    files = {f"{c}.sym": ("100644", cas.put_bytes(f"v {{xschem version=3.4.5}}\n* {c}\n".encode())) for c in cells}
    if manifest is not None:
        files[mf.MANIFEST] = ("100644", cas.put_bytes(manifest))
    return files


def _install(prj: Path, local: str, files: dict, marker: dict) -> Path:
    d = prj / ".undine" / "deps" / local
    cas.install(files, d)
    (d / deps.MARKER).write_text(json.dumps(marker))
    return d


def _set_libraries(prj, **libs) -> None:
    meta = json.loads((prj.path / "project.json").read_text())
    meta["libraries"] = libs
    (prj.path / "project.json").write_text(json.dumps(meta, indent=2) + "\n")


def _lock(prj, libraries: dict, packages: dict | None = None) -> dict:
    lock = deps.read_lock(prj.path)
    lock["libraries"] = libraries
    if packages is not None:
        lock["packages"] = packages
    deps.write_lock(prj.path, lock)
    return lock


def _two_packages(prj, ip_commits=(A,), sar_commits=(A,), record=True) -> tuple[dict, dict]:
    """ip (analog_ip 1.2.0, direct) needs sar (sar_common 1.3.1, only through ip), both in the lock and the store."""
    ip_files = _files(_manifest("analog_ip", "1.2.0", ip_commits))
    sar_files = _files(_manifest("sar_common", "1.3.1", sar_commits), ("sar",))
    tree = cas.write_tree if record else cas.tree_hash
    ip = {"git": "https://example.org/ip.git", "rev": "v1.2.0", "commit": IP_COMMIT, "tree": tree(ip_files),
          "files": "include", "package": "analog_ip", "version": "1.2.0", "names": ["ip", "analog_ip"],
          "deps": {"sar": "sar_common"}}
    sar = {"package": "sar_common", "version": "1.3.1", "git": "https://example.org/sar.git", "rev": "v1.3.1",
           "commit": SAR_COMMIT, "tree": tree(sar_files), "files": "include", "names": ["sar", "sar_common"],
           "deps": {}}
    _set_libraries(prj, ip={"git": "https://example.org/ip.git", "rev": "v1.2.0"})
    _lock(prj, {"ip": ip}, {"sar_common": sar})
    return ip_files, sar_files


# ------------------------------------------------------------------ libraries from markers


def test_a_transitive_install_is_a_read_only_dependency_library_and_the_lock_is_never_read(tmp_path):
    store = _store_of(tmp_path)
    prj = store.create("p", "fakeA")
    d = _install(prj.path, "sar_common", _files(None, ("sar",)),
                 {"git": "https://example.org/sar.git", "rev": "v1.3.1", "commit": SAR_COMMIT, "name": "sar_common",
                  "direct": False, "package": "sar_common", "version": "1.3.1", "via": ["analog_ip"]})
    _install(prj.path, "gone", _files(None), {"commit": "1" * 40, "name": "gone", "direct": True})   # a leftover
    _install(prj.path, "old", _files(None), {"commit": "1" * 40})                                    # phase 1
    (prj.path / ".undine" / "deps" / ".sar.new").mkdir()                                             # staging
    (prj.path / deps.LOCK).write_text("{not json")                     # opening the project never reads it

    prj = store.get("p")
    assert prj.public()["name"] == "p"
    libs = prj.libraries()
    assert "gone" not in libs and "old" not in libs and ".sar.new" not in libs
    sar = libs["sar_common"]
    assert (sar.origin, sar.readonly, sar.direct, sar.via, sar.package) == ("dep", True, False, ("analog_ip",),
                                                                          "sar_common")
    assert sar.version == "sar_common 1.3.1 @ 5b1c0e2"
    pub = sar.public()
    assert (pub["direct"], pub["via"], pub["package"], pub["origin"]) == (False, ["analog_ip"], "sar_common", "dep")
    assert [c["name"] for c in sar.cells()] == ["sar"]
    assert netlist.protected(d / "sar.sch")                            # never written, whatever the file mode

    _set_libraries(prj, sar_common={"git": "https://example.org/sar.git", "rev": "v1.3.1"})
    lib = store.get("p").libraries()["sar_common"]                     # a name project.json uses is left to it
    assert lib.direct is True and lib.version == "sar_common 1.3.1 (v1.3.1 @ 5b1c0e2)"


def test_a_dependency_row_shows_its_package_and_cells_outside_exports_are_internal(tmp_path):
    store = _store_of(tmp_path)
    prj = store.create("p", "fakeA")
    _set_libraries(prj, ip={"git": "https://example.org/ip.git", "rev": "v1.2.0"},
                   plain={"git": "https://example.org/plain.git", "rev": "v1"}, mine="libs/mine")
    _install(prj.path, "ip", _files(_manifest("analog_ip", "1.2.0", exports=["comparator"]), ("comparator", "bias")),
             {"commit": IP_COMMIT, "name": "ip", "direct": True, "package": "analog_ip", "version": "1.2.0"})
    _install(prj.path, "plain", _files(_manifest("plain_ip", "0.1.0", exports=["comparator"]), ("comparator", "x")),
             {"commit": "1" * 40})                                       # a phase 1 install: no package in its marker
    (prj.path / "libs" / "mine").mkdir(parents=True)
    (prj.path / "libs" / "mine" / "x.sym").write_text("v {}\n")
    libs = store.get("p").libraries()
    ip = libs["ip"]
    assert ip.version == "analog_ip 1.2.0 (v1.2.0 @ 2299146)" and ip.package == "analog_ip" and ip.direct
    assert ip.exports == ("comparator",)
    cells = {c["name"]: c for c in ip.cells()}
    assert cells["bias"]["internal"] is True and "internal" not in cells["comparator"]
    assert libs["plain"].version == "v1 @ 1111111" and libs["plain"].package is None
    assert all("internal" not in c for c in libs["plain"].cells())     # every cell of a plain dependency is public
    assert all("internal" not in c for c in libs["mine"].cells())
    assert libs["mine"].public()["package"] is None and libs["mine"].public()["direct"] is True

    names = store.get("p").dependency_cell_names()
    assert names == {"comparator": "ip", "bias": "ip", "x": "plain"}


def test_markers_and_manifests_are_read_once_until_they_change(tmp_path, monkeypatch):
    store = _store_of(tmp_path)
    prj = store.create("p", "fakeA")
    _set_libraries(prj, ip={"git": "https://example.org/ip.git", "rev": "v1.2.0"})
    d = _install(prj.path, "ip", _files(_manifest("analog_ip", "1.2.0", exports=["comparator"]),
                                        ("comparator", "bias")),
                 {"commit": IP_COMMIT, "name": "ip", "direct": True, "package": "analog_ip", "version": "1.2.0"})
    _install(prj.path, "sar", _files(_manifest("sar_common", "1.3.1"), ("sar",)),
             {"commit": SAR_COMMIT, "name": "sar", "direct": False, "package": "sar_common", "version": "1.3.1",
              "via": ["analog_ip"]})
    calls = []
    real = mf.from_dir
    monkeypatch.setattr(mf, "from_dir", lambda *a: calls.append(a[0]) or real(*a))
    project_mod._MARKERS.clear()
    prj = store.get("p")
    for _ in range(3):
        assert prj.libraries()["ip"].exports == ("comparator",)
        assert prj.libraries()["sar"].exports is None
    assert len(calls) == 2

    os.chmod(d / mf.MANIFEST, 0o644)                                   # a reinstall: new manifest, new marker
    (d / mf.MANIFEST).write_bytes(_manifest("analog_ip", "1.3.0", exports=["comparator", "bias"]))
    (d / deps.MARKER).write_text(json.dumps({"commit": SAR_COMMIT, "name": "ip", "direct": True,
                                             "package": "analog_ip", "version": "1.3.0"}))
    st = (d / deps.MARKER).stat()                                      # within one clock tick of the first write
    os.utime(d / deps.MARKER, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    ip = prj.libraries()["ip"]
    assert ip.exports == ("comparator", "bias") and ip.version == "analog_ip 1.3.0 (v1.2.0 @ 5b1c0e2)"
    assert len(calls) == 3

    (d / mf.MANIFEST).write_bytes(b"[package]\nname = 'Bad'\n")       # unreadable: every cell public, no error
    st = (d / deps.MARKER).stat()
    os.utime(d / deps.MARKER, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert prj.libraries()["ip"].exports is None


def test_symbol_paths_hold_the_deps_directory_once(tmp_path):
    store = _store_of(tmp_path)
    prj = store.create("p", "fakeA")
    root = prj.path / ".undine" / "deps"
    assert root not in prj.symbol_paths()
    _install(prj.path, "sar", _files(None, ("sar",)),
             {"commit": SAR_COMMIT, "name": "sar", "direct": False, "package": "sar_common", "version": "1.3.1"})
    paths = store.get("p").symbol_paths()
    assert isinstance(paths, list) and paths.count(root) == 1 and paths.index(root) < paths.index(root / "sar")
    _set_libraries(prj, mine="libs/mine", ip={"git": "https://example.org/ip.git"})
    paths = store.get("p").symbol_paths()
    assert paths.count(root) == 1 and paths[:2] == [prj.path / "libs", root]


def test_create_library_refuses_an_installed_local_name(tmp_path):
    store = _store_of(tmp_path)
    prj = store.create("p", "fakeA")
    _install(prj.path, "sar_common", _files(None, ("sar",)),
             {"commit": SAR_COMMIT, "name": "sar_common", "direct": False, "package": "sar_common",
              "version": "1.3.1"})
    before = (prj.path / "project.json").read_bytes()
    for name in ("sar_common", "SAR_Common"):
        with pytest.raises(ValueError, match=r"a dependency is installed under the name sar_common"):
            prj.create_library(name)
    assert (prj.path / "project.json").read_bytes() == before and not (prj.path / "libs" / "sar_common").exists()
    assert prj.create_library("mine").name == "mine"


# ------------------------------------------------------------------ set-pdk


def test_set_pdk_is_refused_listing_every_locked_package_that_does_not_accept_it(tmp_path, monkeypatch):
    store = _store_of(tmp_path)
    prj = store.create("p", "fakeA")
    pdkops.upgrade(prj)                                                # format 2: set-pdk would hash the new PDK
    _two_packages(prj)
    lock = deps.read_lock(prj.path)
    lock["packages"]["free"] = {"package": "free", "version": "0.1.0", "git": "https://example.org/f.git",
                                "rev": "v0.1.0", "commit": "3" * 40, "names": ["free"], "deps": {}, "files": "include",
                                "tree": cas.write_tree(_files(_manifest("free", "0.1.0", pdk=False), ("free",)))}
    lock["libraries"]["ip"]["deps"]["free"] = "free"
    deps.write_lock(prj.path, lock)
    files = {f: (prj.path / f).read_bytes() for f in ("project.json", deps.LOCK)}

    with pytest.raises(compat.IncompatiblePdk) as e:
        prj.set_pdk("fakeB")
    rows = e.value.packages
    assert [r["name"] for r in rows] == ["ip", "sar"]                  # free has no [pdk]: not checked
    assert rows[0] == {"name": "ip", "package": "analog_ip", "version": "1.2.0",
                       "chain": "p -> analog_ip 1.2.0 (v1.2.0 @ 229914698b1f)",
                       "reason": "fakeB@bbbbbbbbbbbb is not among commits aaaaaaaaaaaa", "family": "fake",
                       "variants": ["fakeA", "fakeB"], "commits": [A], "compat": [], "built_with": None}
    assert rows[1]["chain"] == ("p -> analog_ip 1.2.0 (v1.2.0 @ 229914698b1f) -> "
                                "sar_common 1.3.1 (v1.3.1 @ 5b1c0e2a9d11)")
    msg = str(e.value)
    assert msg.startswith("the PDK fakeB@bbbbbbbbbbbb is not accepted by 2 locked packages:\n- p -> analog_ip")
    assert len(msg.splitlines()) == 4 and msg.endswith("nothing was changed")
    assert {f: (prj.path / f).read_bytes() for f in files} == files

    assert prj.set_pdk("fakeA") == ("fakeA@aaaaaaaaaaaa", "fakeA@aaaaaaaaaaaa")   # the PDK it has: never refused

    # the check runs again under the project lock, on the lock as it is then: a lib add between the two (made here
    # by the first call) is caught by the second
    import fcntl
    _two_packages(prj, ip_commits=(A, B), sar_commits=(B, A))          # both accept fakeB
    calls, held = [], []
    real = compat.locked_incompatible

    def concurrent_add(*a):
        calls.append(a[1])
        with open(prj.path / ".undine" / "project.lock", "a+") as f:  # does a request of its own wait now?
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f, fcntl.LOCK_UN)
                held.append(False)
            except BlockingIOError:
                held.append(True)
        out = real(*a)
        if len(calls) == 1:
            _two_packages(prj)                                         # ip and sar accept only fakeA again
        return out

    monkeypatch.setattr(compat, "locked_incompatible", concurrent_add)
    with pytest.raises(compat.IncompatiblePdk) as e:
        prj.set_pdk("fakeB")
    assert [r["name"] for r in e.value.packages] == ["ip", "sar"]
    assert len(calls) == 2 and calls[1] is not calls[0] and held == [False, True]
    assert calls[0]["libraries"]["ip"]["tree"] != calls[1]["libraries"]["ip"]["tree"]
    assert calls[1] == deps.read_lock(prj.path)                        # the second read the lock as it was then
    assert json.loads((prj.path / "project.json").read_text())["pdk"] == "fakeA@aaaaaaaaaaaa"

    monkeypatch.setattr(compat, "locked_incompatible", real)
    _two_packages(prj, ip_commits=(A, B), sar_commits=(B, A))          # both accept fakeB now
    assert prj.set_pdk("fakeB") == ("fakeA@aaaaaaaaaaaa", "fakeB@bbbbbbbbbbbb")


def test_set_pdk_reads_a_manifest_from_the_install_and_refuses_with_neither(tmp_path):
    store = _store_of(tmp_path)
    prj = store.create("p", "fakeA")
    ip_files, sar_files = _two_packages(prj, ip_commits=(A, B), sar_commits=(A, B), record=False)
    lock = deps.read_lock(prj.path)
    assert cas.read_tree(lock["libraries"]["ip"]["tree"]) is None      # the store has no record of either tree
    for local, files, entry in (("ip", ip_files, lock["libraries"]["ip"]),
                                ("sar", sar_files, lock["packages"]["sar_common"])):
        _install(prj.path, local, files, entry)
    assert prj.set_pdk("fakeB") == ("fakeA@aaaaaaaaaaaa", "fakeB@bbbbbbbbbbbb")

    import shutil
    for root, _, _ in os.walk(prj.path / ".undine" / "deps" / "sar"):
        os.chmod(root, 0o755)
    shutil.rmtree(prj.path / ".undine" / "deps" / "sar")
    with pytest.raises(compat.IncompatiblePdk, match=r"sar: not in the store or installed: run lib sync first") as e:
        prj.set_pdk("fakeA")
    assert [r["name"] for r in e.value.packages] == ["sar"]


def test_plain_git_dependencies_never_block_set_pdk(tmp_path):
    store = _store_of(tmp_path)
    prj = store.create("p", "fakeA")
    _set_libraries(prj, plain={"git": "https://example.org/plain.git"},
                   other={"git": "https://example.org/other.git"})
    _lock(prj, {"plain": {"git": "https://example.org/plain.git", "commit": "1" * 40, "sha": "2" * 64},
                "other": {"git": "https://example.org/other.git", "commit": "3" * 40,
                          "tree": cas.write_tree(_files(None))}})
    assert prj.set_pdk("fakeB") == ("fakeA@aaaaaaaaaaaa", "fakeB@bbbbbbbbbbbb")

    # a phase 1 entry (no package in the lock) whose manifest the store has, with a [pdk]: checked
    lock = deps.read_lock(prj.path)
    lock["libraries"]["other"]["tree"] = cas.write_tree(_files(_manifest("other_ip", "1.0.0", commits=(B,))))
    deps.write_lock(prj.path, lock)
    with pytest.raises(compat.IncompatiblePdk) as e:
        prj.set_pdk("fakeA")
    assert [(r["name"], r["package"], r["chain"]) for r in e.value.packages] == \
        [("other", "other_ip", "p -> other (git 333333333333)")]


# ------------------------------------------------------------------ pdk verify --pin, lib lock --format 2


def test_upgrade_reports_locked_packages_the_new_pin_does_not_accept(tmp_path):
    store = _store_of(tmp_path)
    prj = store.create("p", "fakeA")                                   # format 1: pins fakeA@aaaaaaaaaaaa
    _two_packages(prj, ip_commits=(B,), sar_commits=(A, B))
    r = pdkops.upgrade(prj)
    assert r["wrote"] and r["format"] == 2 and r["pdk"]["commit"] == A  # pinned all the same: a warning
    assert [(x["name"], x["reason"]) for x in r["incompatible"]] == \
        [("ip", "fakeA@aaaaaaaaaaaa is not among commits bbbbbbbbbbbb")]
    assert deps.read_lock(prj.path)["format"] == 2

    v = pdkops.verify(prj, pin=True)
    assert [x["name"] for x in v["incompatible"]] == ["ip"]

    _two_packages(prj, ip_commits=(A,), sar_commits=(A,))
    assert "incompatible" not in pdkops.upgrade(prj)


def test_upgrader_hashes_first_and_converts_in_memory(tmp_path):
    store = _store_of(tmp_path)
    prj = store.create("p", "fakeA")
    before = (prj.path / deps.LOCK).read_bytes()
    convert = pdkops.upgrader(prj)
    lock = deps.read_lock(prj.path)
    new = convert(lock)
    assert new["format"] == 2 and new["pdk"]["commit"] == A and new["tools"] == NOW and new is not lock
    assert lock["format"] == 1 and (prj.path / deps.LOCK).read_bytes() == before     # nothing written
    assert convert(None)["libraries"] == {}

    moved = dict(lock, pdk="fakeB@bbbbbbbbbbbb")                       # another PDK meanwhile (a git pull)
    with pytest.raises(pdkops.PinError, match="changed while the PDK was hashed"):
        convert(moved)
