"""A locked dependency's files and manifest, without fetching (docs/design/packages.md, Resolution; Fetch and store).

Resolving one requirement (add, update) holds every other locked package fixed, and set-pdk checks the locked packages
against the new PDK: both need what a lock entry pins, and neither may read git. So the files come from the store's
record of the entry's tree (or, for a format 1 entry, of its archive), else from the project's install when it is
exactly that tree, else the caller is told to run lib sync, which fills the store on a fresh clone anyway.

Reads only the store and the project's .undine/deps; writes nothing.
"""
from __future__ import annotations

import hashlib
import stat
from pathlib import Path

from undine.store import cas
from undine.store import manifest as mf


class NotAvailable(Exception):
    pass


def _not_available(local: str) -> NotAvailable:
    return NotAvailable(f"{local}: not in the store or installed: run lib sync first")


def _stored(entry: dict) -> dict | None:
    """The store's record of the entry's tree (format 2) or archive (format 1); None when there is none."""
    try:
        if entry.get("tree"):
            return cas.read_tree(entry["tree"])
        if entry.get("sha"):
            return cas.read_archive_tree(entry["sha"])
    except (cas.StoreError, TypeError):                 # a hash that is no hash: nothing recorded can be it
        return None
    return None


def _installed(prj_path: Path, local: str, entry: dict) -> tuple[Path, dict] | None:
    """(install directory, its files) when .undine/deps/<local> was installed as this entry (its marker's tree or
    sha) and its content still hashes to it; None otherwise. A modified install is not used."""
    from undine.store import deps                       # deps imports the modules this one does
    d = deps._own_dir(Path(prj_path), local)            # refuses a linked .undine/deps and a name that is a path
    marker = deps.installed(Path(prj_path), local) or {}
    if entry.get("tree"):
        if marker.get("tree") != entry["tree"]:
            return None
        files = deps._installed_tree(d, entry["tree"])
        return None if files is None else (d, files)
    sha = entry.get("sha")
    if not sha or marker.get("sha") != sha or not d.is_dir():
        return None
    modes = deps._marked_modes(d)
    files = {}
    for rel, p, st in cas._walk_files(d):
        if not stat.S_ISREG(st.st_mode):
            return None
        files[rel] = (str(modes.get(rel, "100644")), cas._hash_file(p))
    try:
        return (d, files) if cas.archive_sha(files) == sha else None
    except cas.StoreError:
        return None


def files_of(prj_path: Path, local: str, entry: dict) -> dict:
    """The files lock entry `entry` (installed as `local`) pins, {path: (mode, hex)}: the store's record, else the
    verified install. Raises NotAvailable when neither has them."""
    files = _stored(entry)
    if files is None:
        got = _installed(prj_path, local, entry)
        files = None if got is None else got[1]
    if files is None:
        raise _not_available(local)
    return files


def manifest_of(prj_path: Path, local: str, entry: dict) -> mf.Manifest | None:
    """The manifest among those files (None when there is none), its bytes re-hashed before they are parsed: the
    store's object, else the install's file. Raises NotAvailable when neither has them, ManifestError for a bad
    manifest."""
    files = _stored(entry)
    if files is not None:
        if mf.MANIFEST not in files:
            return None
        if cas.check_object(files[mf.MANIFEST][1], repair_mode=True):
            return mf.from_files(files, local)
    got = _installed(prj_path, local, entry)
    if got is None:
        raise _not_available(local)
    d, files = got
    if mf.MANIFEST not in files:
        return None
    return mf.parse(_read_checked(d / mf.MANIFEST, files[mf.MANIFEST][1], local), local)


def _read_checked(p: Path, hex: str, where: str) -> bytes:
    """The install's manifest, when it is still the file that was hashed: a regular file with that SHA-256."""
    if p.is_symlink() or not p.is_file():
        raise mf.ManifestError(f"{where}: {mf.MANIFEST}: is not a regular file: reinstall with `undine lib sync`")
    with open(p, "rb") as f:
        data = f.read(mf.MAX_BYTES + 1)
    if len(data) <= mf.MAX_BYTES and hashlib.sha256(data).hexdigest() != hex:
        raise mf.ManifestError(f"{where}: {mf.MANIFEST}: changed while it was read: run `undine lib sync`")
    return data
