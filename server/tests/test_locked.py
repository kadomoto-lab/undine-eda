"""store/locked.py: a locked entry's files and manifest from the store, else from a verified install, never fetched."""
import json
import os
import shutil
from pathlib import Path

import pytest

from undine.store import cas, deps, locked
from undine.store import manifest as mf

MANIFEST = b'[package]\nname = "analog_ip"\nversion = "1.2.0"\n'


@pytest.fixture(autouse=True)
def _store(fresh_store, monkeypatch):
    monkeypatch.setenv("UNDINE_STORE_LINK", "copy")      # an install of its own: changing it leaves the store alone
    return fresh_store


def _files(extra: bytes = b"") -> dict:
    return {"comparator.sch": ("100644", cas.put_bytes(b"v {xschem}\n" + extra)),
            "run.sh": ("100755", cas.put_bytes(b"#!/bin/sh\n")),
            mf.MANIFEST: ("100644", cas.put_bytes(MANIFEST))}


def _install(prj: Path, local: str, files: dict, entry: dict) -> Path:
    d = prj / ".undine" / "deps" / local
    cas.install(files, d)
    modes = {p: m for p, (m, _) in files.items() if m != "100644"}
    (d / cas.MARKER).write_text(json.dumps(entry | {"modes": modes}))
    return d


def _empty_store(store: Path) -> None:
    for root, dirs, _ in os.walk(store):
        os.chmod(root, 0o755)
    shutil.rmtree(store)


def test_files_and_manifest_from_the_store(tmp_path):
    files = _files()
    entry = {"commit": "1" * 40, "tree": cas.write_tree(files)}
    assert locked.files_of(tmp_path, "ip", entry) == files
    m = locked.manifest_of(tmp_path, "ip", entry)
    assert (m.name, str(m.version)) == ("analog_ip", "1.2.0")


def test_a_format_1_entry_from_the_store_and_from_its_install(tmp_path, _store):
    files = _files()
    entry = {"commit": "1" * 40, "sha": cas.write_archive_tree(files)}
    assert locked.files_of(tmp_path, "ip", entry) == files
    _install(tmp_path, "ip", files, entry)
    _empty_store(_store)
    assert {p: h for p, (_, h) in locked.files_of(tmp_path, "ip", entry).items()} == \
        {p: h for p, (_, h) in files.items()}
    assert locked.manifest_of(tmp_path, "ip", entry).name == "analog_ip"


def test_files_and_manifest_from_a_verified_install_when_the_store_is_empty(tmp_path, _store):
    files = _files()
    entry = {"commit": "1" * 40, "tree": cas.write_tree(files)}
    _install(tmp_path, "ip", files, entry)
    _empty_store(_store)
    assert cas.read_tree(entry["tree"]) is None
    assert locked.files_of(tmp_path, "ip", entry) == files
    assert locked.manifest_of(tmp_path, "ip", entry).name == "analog_ip"


def test_neither_store_nor_install_is_not_available(tmp_path, _store):
    entry = {"commit": "1" * 40, "tree": cas.tree_hash(_files())}
    _empty_store(_store)
    with pytest.raises(locked.NotAvailable, match=r"^ip: not in the store or installed: run lib sync first$"):
        locked.files_of(tmp_path, "ip", entry)
    with pytest.raises(locked.NotAvailable):
        locked.manifest_of(tmp_path, "ip", entry)


def test_a_modified_install_is_not_used(tmp_path, _store):
    files = _files()
    entry = {"commit": "1" * 40, "tree": cas.write_tree(files)}
    d = _install(tmp_path, "ip", files, entry)
    _empty_store(_store)
    p = d / "comparator.sch"
    p.chmod(0o644)
    p.write_bytes(b"v {xschem}\nchanged\n")
    with pytest.raises(locked.NotAvailable):
        locked.files_of(tmp_path, "ip", entry)


def test_an_install_of_another_tree_is_not_used(tmp_path, _store):
    files, other = _files(), _files(b"other\n")
    entry = {"commit": "1" * 40, "tree": cas.write_tree(files)}
    _install(tmp_path, "ip", other, {"commit": "2" * 40, "tree": cas.write_tree(other)})
    _empty_store(_store)
    with pytest.raises(locked.NotAvailable):
        locked.files_of(tmp_path, "ip", entry)


def test_a_damaged_store_object_falls_back_to_the_install(tmp_path, _store):
    files = _files()
    entry = {"commit": "1" * 40, "tree": cas.write_tree(files)}
    _install(tmp_path, "ip", files, entry)
    obj = cas.object_path(files[mf.MANIFEST][1])
    obj.chmod(0o644)
    obj.write_bytes(b"[package]\nname = 'evil'\n")
    assert locked.manifest_of(tmp_path, "ip", entry).name == "analog_ip"


def test_a_tree_without_a_manifest_has_none(tmp_path):
    files = {"comparator.sch": ("100644", cas.put_bytes(b"v {xschem}\n"))}
    assert locked.manifest_of(tmp_path, "ip", {"tree": cas.write_tree(files)}) is None


def test_a_linked_deps_directory_is_refused(tmp_path, _store):
    files = _files()
    entry = {"commit": "1" * 40, "tree": cas.tree_hash(files)}
    elsewhere = tmp_path / "elsewhere"
    _install(elsewhere, "ip", files, entry)
    prj = tmp_path / "prj"
    prj.mkdir()
    (prj / ".undine").symlink_to(elsewhere / ".undine")
    _empty_store(_store)
    with pytest.raises(deps.DepError, match="is a link"):
        locked.files_of(prj, "ip", entry)
