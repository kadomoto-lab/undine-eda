"""Licences and vendoring (docs/design/packages.md, Fetch and store: Vendoring; Licences).

Files stay in the publishers' repositories, and a repository that is deleted, made private or force-pushed loses that
version for every machine whose store lacks it. So a tapeout keeps its own copy:

    pkg licenses PROJECT       the licence of every locked dependency and of the PDK
    pkg vendor PROJECT DIR     DIR/vendor/: every locked tree, vendor.json, LICENSES.json and LICENSES.md
    pkg sync PROJECT --from-vendor DIR
                               reads DIR/vendor/ back into the store, each tree checked against the lock, then
                               installs the lock offline: no repository and no network

DIR/vendor/ holds one directory per lock entry: `<package>@<version>` for a package, `<name>@<commit>` for a plain git
dependency. Its files are plain copies (0o644, or 0o755 for a file of mode 100755), so the bundle can be zipped and
read anywhere. vendor.json records per directory what the lock records of its entry (index, git, subdir, commit, tree
or sha), and the PDK as the lock pins it: the PDK is recorded, not copied (ciel's releases keep it, and a variant is
over a gigabyte).

A tree is taken from the store (its record and objects), else from the project's install when that is exactly the
locked tree, else it is fetched (the CLI asks first) or, offline, named and refused. Every written directory is hashed
again and compared with the lock before vendor/ is moved into place: a vendor/ that exists is always whole and as
locked. Reading it back hashes every directory before anything enters the store, so a file changed by hand is refused
with nothing imported.

The PDK licences are built in here until the PDK's profile package (phase 6) gives them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import time
from collections.abc import Callable
from pathlib import Path

from undine.store import cas, deps, locked, resolve
from undine.store import index as ix
from undine.store import manifest as mf

VENDOR = "vendor"
VENDOR_JSON = "vendor.json"
FORMAT = 1
# The licence of each open PDK family, until its profile package gives it: SkyWater's sky130, GlobalFoundries'
# gf180mcu and IHP's SG13G2 are all released under Apache-2.0.
PDK_LICENSES = {"sky130": "Apache-2.0", "gf180mcu": "Apache-2.0", "ihp-sg13": "Apache-2.0"}
_LICENSE_FILE = re.compile(r"^(licen[cs]e|copying|notice|copyright)([._-].*)?$", re.IGNORECASE)
_DIR = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.+@-]*$")


class VendorError(deps.DepError):
    pass


# ------------------------------------------------------------------ the lock's entries


class _Entry:
    """One lock entry to vendor: its graph id, table and key, the local names it is installed under, and the
    directory name it gets in vendor/."""

    def __init__(self, g: resolve.Graph, id: str):
        self.id, self.entry = id, g.entries[id]
        self.table, self.key = g.keys[id]
        self.names = g.names_of(id) or [self.key]
        e = self.entry
        package, version = e.get("package"), e.get("version")
        if isinstance(package, str) and package and isinstance(version, str) and version:
            self.dir = f"{package}@{version}"
        else:
            self.dir = f"{self.key}@{e.get('commit')}"
        if not _DIR.match(self.dir) or ".." in self.dir:
            raise VendorError(f"{self.key}: {deps.LOCK} gives it a name or version that is no directory name "
                              f"({self.dir!r}): fix that entry; nothing was written")

    @property
    def label(self) -> str:
        e = self.entry
        if e.get("package"):
            return f"{e['package']} {e.get('version') or '-'}"
        return f"{self.key} @ {str(e.get('commit'))[:12]}"


def _entries(lock: dict) -> list[_Entry]:
    g = resolve.lock_graph(lock)
    out = [_Entry(g, id) for id in sorted(g.entries, key=lambda i: (g.keys[i][0] != "libraries", g.keys[i][1]))]
    seen: dict[str, str] = {}
    for x in out:
        deps._check_hash(x.key, x.entry)
        deps._check_commit(x.key, x.entry.get("commit"))
        if x.dir in seen:
            raise VendorError(f"{deps.LOCK} locks {x.dir} twice ({seen[x.dir]} and {x.key}): run `python -m undine "
                              "lib lock` to resolve it; nothing was written")
        seen[x.dir] = x.key
    return out


def _source(prj, x: _Entry) -> tuple[dict, Callable[[str, str], Path]] | None:
    """The entry's files and where each one's bytes are: the store (its record and every object), else the install
    under one of its names when that is exactly the locked tree; None when neither has them."""
    kind, value = deps.entry_hash(x.entry)
    files = deps._stored(kind, value)
    if files is not None:
        return files, lambda rel, hex: cas.object_path(hex)
    for n in x.names:
        try:
            got = locked._installed(prj.path, n, x.entry)
        except deps.DepError:
            got = None
        if got is not None:
            d, files = got
            return files, lambda rel, hex, d=d: d / rel
    return None


# ------------------------------------------------------------------ licences


def _license_files(files: dict) -> list[str]:
    return sorted(p for p in files if "/" not in p and _LICENSE_FILE.match(p))


def _index_license(e: dict) -> str | None:
    """The licence the local copy of the entry's index gives its version (read only: no fetch)."""
    url, package, version = e.get("index"), e.get("package"), e.get("version")
    if not (isinstance(url, str) and isinstance(package, str) and isinstance(version, str)):
        return None
    try:
        idx = ix.Index(url)
        if not idx.fetched():
            return None
        return next((v.license for v in idx.versions(package) if str(v.vers) == version), None)
    except (ix.IndexReadError, deps.DepError, ValueError):
        return None


def _pdk(prj, lock: dict) -> dict | None:
    pin = deps.lock_pdk(lock)
    if pin is None and prj.pdk is not None:
        pin = {"id": prj.pdk.id}
    if pin is None:
        return None
    from undine.pdks.pdk import family_of
    family = pin.get("family") or family_of(str(pin.get("id") or "").split("@")[0])
    lic = PDK_LICENSES.get(family)
    return {**pin, "family": family, "license": lic,
            "license_from": "built in (the PDK's profile package will give it)" if lic else None}


def licenses(prj, lock: dict | None = None) -> dict:
    """The licence of every locked dependency and of the PDK: {project, entries: [{name, table, package, version,
    index, git, subdir, commit, license, license_from, license_files, note?}], pdk}. A package's licence comes from its
    manifest, else from the local copy of its index; a plain git dependency has none recorded, and its top-level
    LICENSE / COPYING / NOTICE files are listed. Reads the store and the installs only: nothing is fetched."""
    lock = deps.read_lock(prj.path) if lock is None else lock
    lock = lock or {}
    rows = []
    for x in _entries(lock):
        e = x.entry
        row = {"name": x.key, "table": x.table, "dir": x.dir} | {
            k: e[k] for k in ("package", "version", "index", "git", "subdir", "commit") if e.get(k)}
        lic, where, note = None, None, None
        src = _source(prj, x)
        if src is None:
            note = f"not in the store or installed: run `python -m undine lib sync {prj.name}`"
            row["license_files"] = []
        else:
            files = src[0]
            row["license_files"] = _license_files(files)
            if mf.MANIFEST in files:
                try:
                    m = locked.manifest_of(prj.path, x.names[0], e)
                    if m is not None and m.license:
                        lic, where = m.license, mf.MANIFEST
                except (locked.NotAvailable, mf.ManifestError, deps.DepError):
                    pass
        if lic is None and (got := _index_license(e)):
            lic, where = got, "index"
        row |= {"license": lic, "license_from": where} | ({"note": note} if note else {})
        rows.append(row)
    return {"project": prj.name, "entries": rows, "pdk": _pdk(prj, lock)}


def licenses_md(data: dict) -> str:
    """LICENSES.md: the licences as a table a person reads in the bundle."""
    out = [f"# Licences of the dependencies of {data['project']}", "",
           "Written by Undine (`pkg licenses`). Each vendored directory keeps the files of its repository as locked, "
           "its own licence files among them.", "",
           "| Dependency | Version | Licence | From | Licence files |", "|---|---|---|---|---|"]
    for r in data["entries"]:
        name = r.get("package") or r["name"]
        version = r.get("version") or f"git {str(r.get('commit'))[:12]}"
        out.append(f"| {name} | {version} | {r.get('license') or 'not recorded'} | {r.get('license_from') or '-'} | "
                   f"{', '.join(r.get('license_files') or []) or '-'} |")
    if not data["entries"]:
        out.append("| (no dependency) | | | | |")
    p = data.get("pdk")
    out += ["", "## PDK", ""]
    if p:
        out.append(f"{p.get('id')} ({p.get('family')}): {p.get('license') or 'not recorded'}"
                   + (f", {p['license_from']}" if p.get("license_from") else "") + ". Recorded, not copied: "
                   "`pdk fetch` installs it again from ciel's releases.")
    else:
        out.append("No PDK.")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ vendor


def _write(files: dict, at: Callable[[str, str], Path], d: Path) -> int:
    """Copies the files into `d` as plain files (0o755 for mode 100755, else 0o644); returns the bytes written."""
    cas._check_files(files)
    cas._check_installable(files)
    total = 0
    for rel, (mode, hex) in sorted(files.items()):
        dst = d / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        with open(at(rel, hex), "rb") as fi, open(dst, "xb") as fo:
            shutil.copyfileobj(fi, fo, cas._CHUNK)
            total += fo.tell()
        os.chmod(dst, 0o755 if mode == "100755" else 0o644)
    return total


def _read_dir(d: Path, where: str) -> dict[str, tuple[str, str]]:
    """The files of a vendored directory, {path: (mode, hex)}, the mode from the owner's execute bit. A link or any
    file that is not a regular one is refused: vendor never writes one."""
    if d.is_symlink() or not d.is_dir():
        raise VendorError(f"{where}: is not a directory; nothing was imported")
    files = {}
    for rel, p, st in cas._walk_files(d):
        if not stat.S_ISREG(st.st_mode):
            raise VendorError(f"{where}/{rel}: is not a regular file, which pkg vendor never writes; nothing was "
                              "imported")
        files[rel] = ("100755" if st.st_mode & stat.S_IXUSR else "100644", cas._hash_file(p))
    return files


def _hash(kind: str, files: dict) -> str:
    return cas.tree_hash(files) if kind == "tree" else cas.archive_sha(files)


def _fetch(prj, todo: list[_Entry]) -> None:
    """Reads each entry's commit into the store from its kept repository, fetching it when needed (the command line:
    any host, as `lib sync` there). The caller holds cas.store_lock(shared)."""
    for x in todo:
        kind, value = deps.entry_hash(x.entry)
        spec = deps._spec_of(x.entry)
        deps._check_spec(spec)
        try:
            commit, _, got = deps._read_commit(x.key, spec, x.entry["commit"], None, kind, False,
                                               deps._mode(x.entry))
        except mf.ManifestError as e:
            raise VendorError(str(e)) from None
        deps._same(x.key, commit, got, value)


def vendor(prj, out_dir: Path, *, offline: bool = True, confirm: Callable[[list[str]], bool] | None = None) -> dict:
    """Writes `out_dir`/vendor/: every lock entry's tree, vendor.json, LICENSES.json and LICENSES.md (module
    docstring). A tree neither in the store nor installed as locked is fetched when not `offline` and `confirm`
    (given the entries' labels) says yes; else it is named and nothing is written. vendor/ must not exist yet. Returns
    {dir, entries: [{dir, name, package?, version?, commit, tree | sha, files, bytes}], pdk, licenses}."""
    out_dir = Path(out_dir)
    dest = out_dir / VENDOR
    if dest.exists() or dest.is_symlink():
        raise VendorError(f"{dest} exists: remove it, or name another directory; nothing was written")
    with deps._deps_lock(prj.path):
        lock_bytes = (prj.path / deps.LOCK).read_bytes() if (prj.path / deps.LOCK).is_file() else b""
        lock = deps.read_lock(prj.path) or {}
        todo = _entries(lock)
        with cas.store_lock(shared=True):
            missing = [x for x in todo if _source(prj, x) is None]
            if missing:
                names = [x.label for x in missing]
                if offline or confirm is None or not confirm(names):
                    raise VendorError(f"not in the store or installed: {', '.join(names)}: run `python -m undine lib "
                                      f"sync {prj.name}`, or `pkg vendor` from the command line, which fetches "
                                      f"{'it' if len(names) == 1 else 'them'}; nothing was written")
                _fetch(prj, missing)
            out_dir.mkdir(parents=True, exist_ok=True)
            tmp = out_dir / f".{VENDOR}.new-{os.getpid()}"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir()
            try:
                rows, record = [], {}
                for x in todo:
                    src = _source(prj, x)
                    if src is None:
                        raise VendorError(f"{x.label}: left the store while it was vendored; nothing was written")
                    files, at = src
                    d = tmp / x.dir
                    d.mkdir()
                    size = _write(files, at, d)
                    kind, value = deps.entry_hash(x.entry)
                    if _hash(kind, _read_dir(d, f"{VENDOR}/{x.dir}")) != value:
                        raise VendorError(f"{x.label}: the files written do not hash to the lock's {kind} "
                                          f"{str(value)[:19]}: run `python -m undine store verify`, then vendor again; "
                                          "nothing was written")
                    keep = {k: x.entry[k] for k in ("package", "version", "index", "git", "rev", "subdir", "commit",
                                                    "tree", "sha", "files") if x.entry.get(k)}
                    record[x.dir] = {"table": x.table, "key": x.key, "names": x.names} | keep
                    rows.append({"dir": x.dir, "name": x.key} | {
                        k: keep[k] for k in ("package", "version", "commit", "tree", "sha") if k in keep} | {
                        "files": len(files), "bytes": size})
                lic = licenses(prj, lock)
                pdk = lic["pdk"]
                meta = {"format": FORMAT, "project": prj.name, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "lock": {"format": deps.lock_format(lock) if lock else None,
                                 "sha256": hashlib.sha256(lock_bytes).hexdigest() if lock_bytes else None},
                        "entries": record, "pdk": (pdk | {"copied": False}) if pdk else None}
                (tmp / VENDOR_JSON).write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
                (tmp / "LICENSES.json").write_text(json.dumps(lic, indent=2, sort_keys=True) + "\n")
                (tmp / "LICENSES.md").write_text(licenses_md(lic))
                if dest.exists() or dest.is_symlink():
                    raise VendorError(f"{dest} exists: remove it, or name another directory; nothing was written")
                os.replace(tmp, dest)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    return {"dir": str(dest), "entries": rows, "pdk": pdk, "licenses": lic}


# ------------------------------------------------------------------ reading it back


def _vendor_dir(src: Path) -> Path:
    src = Path(src)
    for d in (src / VENDOR, src):
        if (d / VENDOR_JSON).is_file():
            return d
    raise VendorError(f"{src}: no {VENDOR_JSON} there or in {src / VENDOR}: name the directory `pkg vendor` wrote "
                      "(the tapeout bundle) or its vendor/; nothing was imported")


def _read_meta(vdir: Path) -> dict:
    try:
        meta = json.loads((vdir / VENDOR_JSON).read_text())
    except (OSError, ValueError) as e:
        raise VendorError(f"{vdir / VENDOR_JSON}: cannot be read ({e}); nothing was imported") from None
    if not isinstance(meta, dict) or not isinstance(meta.get("entries"), dict):
        raise VendorError(f"{vdir / VENDOR_JSON}: is not what pkg vendor writes; nothing was imported")
    if meta.get("format") != FORMAT:
        raise VendorError(f"{vdir / VENDOR_JSON}: has format {meta.get('format')!r}, this Undine reads {FORMAT}: "
                          "update Undine; nothing was imported")
    return meta


def import_vendor(prj, src: Path) -> list[dict]:
    """Reads a vendored directory back into the store: for each lock entry, the vendored directory that records the
    same commit and hash, its files hashed and compared with the lock before any enters the store. All or nothing: one
    that does not match (a file changed, added or removed by hand) is refused with nothing imported. An entry the
    vendored directory lacks is left to the store (sync then names it if it is not there). Returns one row per lock
    entry: {name, dir, action: imported | in store | not vendored}."""
    vdir = _vendor_dir(src)
    meta = _read_meta(vdir)
    by_hash: dict[tuple[str, str, str], str] = {}
    for name, v in meta["entries"].items():
        if isinstance(v, dict) and isinstance(name, str) and _DIR.match(name) and ".." not in name:
            kind, value = deps.entry_hash(v)
            if isinstance(value, str) and isinstance(v.get("commit"), str):
                by_hash[kind, value, v["commit"]] = name
    with deps._deps_lock(prj.path):
        lock = deps.read_lock(prj.path) or {}
        todo = _entries(lock)
        rows, found = [], []
        with cas.store_lock(shared=True):
            for x in todo:
                kind, value = deps.entry_hash(x.entry)
                name = by_hash.get((kind, value, x.entry["commit"]))
                if name is None:
                    rows.append({"name": x.key, "dir": None, "action": "not vendored"})
                    continue
                d = vdir / name
                files = _read_dir(d, f"{VENDOR}/{name}")
                if _hash(kind, files) != value:
                    raise VendorError(f"{VENDOR}/{name}: its files do not hash to the lock's {kind} {value[:19]}: a "
                                      "file was changed, added or removed after `pkg vendor`; restore it from the "
                                      "bundle's zip, or vendor again; nothing was imported")
                found.append((x, d, kind, files))
            for x, d, kind, files in found:
                if deps._stored(kind, deps.entry_hash(x.entry)[1]) is not None:
                    rows.append({"name": x.key, "dir": d.name, "action": "in store"})
                    continue
                for rel, (_, hex) in files.items():
                    with open(d / rel, "rb") as f:
                        got, _ = cas.put_file(f)
                    if got != hex:
                        raise VendorError(f"{VENDOR}/{d.name}/{rel}: changed while it was imported; run the import "
                                          "again")
                (cas.write_tree if kind == "tree" else cas.write_archive_tree)(files)
                rows.append({"name": x.key, "dir": d.name, "action": "imported"})
            cas.register_project(prj.path)
    return rows


def sync_from_vendor(prj, src: Path, guard=None) -> dict:
    """`pkg sync --from-vendor`: import_vendor, then `lib sync` offline. Returns {imported: rows, sync: rows}."""
    imported = import_vendor(prj, src)
    return {"imported": imported, "sync": deps.sync(prj, guard, offline=True)}
