"""Pinning a PDK: its full commit, the libraries installed and one tree hash per part (libs.tech, each libs.ref/<lib>).

A part's hash is cas.tree_hash over its files on disk, paths below the part's directory, so it is computed as a
dependency's tree is. What gets written into a PDK tree after install is left out (excluded()), so a PDK used here
still verifies. Hashing one sky130 variant reads 1.1 GB: the result is kept with file stamps in store_root()/pdk/, and
a file is re-read only when its stamp changed.

This module knows nothing of projects or locks on disk: callers pass the lock's `pdk` object. It never writes into a
PDK tree, and callers must not hold a server or project lock while it hashes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path

from undine.home import store_root
from undine.pdks import ciel
from undine.pdks.pdk import LEGACY_OSDI, Pdk, family_of
from undine.store import cas

_CACHE_FORMAT = 1
_SEEN = 4                                   # hashes remembered per part, to name the files a change touched
_CHUNK = 1 << 20
# A file whose mtime is this close to the hash is "racy": a write in the same clock tick (file times are coarse) would
# leave its stamp as it was, so its stamp is not trusted and the next hash reads it again (as git does for its index).
_RACY_NS = 2_000_000_000
_PREFIX = re.compile(r"[0-9a-f]{7,40}")
_FULL = re.compile(r"[0-9a-f]{40}")


class PinError(ValueError):
    pass


# ------------------------------------------------------------------ what is hashed


def excluded(rel: str) -> bool:
    """True for what is written into a PDK tree after install (rel is below the PDK root): `__pycache__/` and `*.pyc`
    from importing the PCell code, and libs.tech/ngspice/osdi/ from osdi.sh on installs set up by an earlier Undine."""
    segments = rel.split("/")
    return "__pycache__" in segments or segments[-1].endswith(".pyc") or rel.startswith(LEGACY_OSDI + "/")


def parts(root: Path) -> dict[str, Path]:
    """The parts hashed one by one: libs.tech, each libs.ref/<lib>, and each other top-level libs.* directory
    (IHP's libs.doc, libs.qa). One hash per library lets a PDK installed with more libraries still verify."""
    root = Path(root)
    out: dict[str, Path] = {}
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if not d.name.startswith("libs.") or not d.is_dir():
            continue
        if d.name == "libs.ref" and not d.is_symlink():
            for lib in sorted(d.iterdir()):
                if lib.is_dir():
                    out[f"libs.ref/{lib.name}"] = lib
        else:                                   # a linked libs.ref stays one part, so that hashing refuses it
            out[d.name] = d
    return out


def _sha256_file(p: Path) -> str:
    """Reads one file of the PDK (tests count the calls). O_NOFOLLOW: a file swapped for a link is not followed."""
    h = hashlib.sha256()
    fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _stamp(st: os.stat_result) -> list:
    return [st.st_size, st.st_mtime_ns, st.st_ino, st.st_ctime_ns, st.st_mode]


def _walk(root: Path):
    """(rel below the PDK root, path, lstat) of every file of every part, excluded ones left out. A link, fifo or
    device is an error: a PDK as ciel installs it has none, and a link would hash what it points to elsewhere."""
    for part, d in parts(root).items():
        if d.is_symlink():
            raise PinError(f"the PDK has a symbolic link at {d}: a PDK is hashed as installed, without links; "
                           f"reinstall it with ciel")
        for top, dirs, names in os.walk(d):
            top = Path(top)
            rel_top = top.relative_to(root).as_posix()
            keep = []
            for n in sorted(dirs):
                rel = f"{rel_top}/{n}"
                if excluded(rel + "/"):
                    continue
                if (top / n).is_symlink():
                    raise PinError(f"the PDK has a symbolic link at {top / n}: a PDK is hashed as installed, without "
                                   f"links; reinstall it with ciel")
                keep.append(n)
            dirs[:] = keep
            for n in sorted(names):
                rel = f"{rel_top}/{n}"
                if excluded(rel):
                    continue
                st = (top / n).lstat()
                if not stat.S_ISREG(st.st_mode):
                    kind = "a symbolic link" if stat.S_ISLNK(st.st_mode) else "a special file"
                    raise PinError(f"the PDK has {kind} at {top / n}: a PDK is hashed as installed, with regular "
                                   f"files only; reinstall it with ciel")
                yield part, rel, top / n, st


# ------------------------------------------------------------------ the stamp cache


def cache_path(pdk: Pdk) -> Path:
    """store_root()/pdk/<commit>-<variant>-<root>.json, <root> the first 12 hex of the SHA-256 of the real root: one
    commit installed under two roots keeps two records, so alternating between them re-reads nothing."""
    real = str(Path(pdk.root).resolve())
    key = hashlib.sha256(real.encode("utf-8", "surrogateescape")).hexdigest()[:12]
    name = f"{pdk.commit}-{pdk.variant}-{key}" if pdk.commit else f"nocommit-{key}-{pdk.name}"
    return store_root() / "pdk" / f"{name}.json"


def _load_cache(path: Path, real: str) -> dict:
    empty = {"format": _CACHE_FORMAT, "root": real, "files": {}, "parts": {}, "seen": {}}
    try:
        c = json.loads(path.read_text())
    except (OSError, ValueError):
        return empty
    if (not isinstance(c, dict) or c.get("format") != _CACHE_FORMAT or c.get("root") != real
            or not all(isinstance(c.get(k), dict) for k in ("files", "parts", "seen"))):
        return empty
    return c


def _save_cache(path: Path, cache: dict) -> None:
    """Written in the store's tmp/ and renamed over the old record: a reader sees the old one or the new one."""
    with tempfile.NamedTemporaryFile("w", dir=cas.tmp_dir(), prefix="pdk-", suffix=".json", delete=False) as t:
        try:
            json.dump(cache, t, separators=(",", ":"))   # not sorted: `seen` keeps the newest last
            t.write("\n")
        except BaseException:
            os.unlink(t.name)
            raise
    path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(t.name, path)


def _hash(pdk: Pdk, only_cached: bool) -> dict | None:
    """The cache brought up to date: {files, parts, seen}. With only_cached, None as soon as a file is new, gone or
    has another stamp (nothing is read then)."""
    root = Path(pdk.root).resolve()
    real = str(root)
    path = cache_path(pdk)
    cache = _load_cache(path, real)
    old = cache["files"]
    if only_cached and not old and not cache["parts"]:
        return None                                       # never hashed here: unchecked, even for an empty root
    files: dict[str, list] = {}
    per_part: dict[str, dict[str, tuple[str, str]]] = {p: {} for p in parts(root)}
    changed = False
    racy = time.time_ns() - _RACY_NS
    for part, rel, p, st in _walk(root):
        prev = old.get(rel)
        if isinstance(prev, list) and len(prev) == 6 and prev[:5] == _stamp(st):
            sha = prev[5]
        elif only_cached:
            return None
        else:
            sha, st = _read_steady(p, st)
            changed = True
        stamp = _stamp(st)
        if st.st_mtime_ns >= racy:
            stamp[1] = -1                               # never matches: read again next time
        files[rel] = stamp + [sha]
        mode = "100755" if st.st_mode & stat.S_IXUSR else "100644"
        per_part[part][rel[len(part) + 1:]] = (mode, sha)
    if set(files) != set(old):
        if only_cached:
            return None
        changed = True
    hashes = {part: cas.tree_hash(entries) for part, entries in per_part.items()}
    if hashes != cache["parts"]:
        changed = True
    seen = cache["seen"]
    for part, h in hashes.items():
        mine = {k: v for k, v in seen.get(part, {}).items() if k != h} if isinstance(seen.get(part), dict) else {}
        mine[h] = {k: v[1] for k, v in per_part[part].items()}
        seen[part] = dict(list(mine.items())[-_SEEN:])          # the newest last
    cache = {"format": _CACHE_FORMAT, "root": real, "files": files, "parts": hashes, "seen": seen}
    if changed:
        _save_cache(path, cache)
    return cache


def _read_steady(p: Path, st: os.stat_result) -> tuple[str, os.stat_result]:
    """The file's SHA-256 and the stat it goes with, read again when the file changed while it was read."""
    for _ in range(3):
        sha = _sha256_file(p)
        now = p.lstat()
        if _stamp(now) == _stamp(st):
            return sha, st
        st = now
    raise PinError(f"{p} keeps changing while it is hashed: stop what writes into the PDK and try again")


def part_hashes(pdk: Pdk, only_cached: bool = False) -> dict[str, str] | None:
    """{part: 'sha256:...'} of the PDK as it is on disk. With only_cached, None unless every file's stamp is in the
    cache (so a status check never reads the PDK)."""
    cache = _hash(pdk, only_cached)
    return None if cache is None else dict(cache["parts"])


# ------------------------------------------------------------------ pin and verify


def pin_object(pdk: Pdk) -> dict:
    """The lock's `pdk` object for this install. A ciel install is assumed to come from ciel's default source."""
    source = {"ciel": "ciel " + ciel.DEFAULT_SOURCE, "volare": "volare"}.get(pdk.source or "")
    return {"id": pdk.id, "family": family_of(pdk.variant), "variant": pdk.variant, "commit": pdk.commit,
            "source": source, "libraries": pdk.libraries(), "tree": part_hashes(pdk)}


def expected_trees(family: str, variant: str, commit: str | None) -> dict[str, str] | None:
    """The tree hashes a PDK profile package publishes for this commit. Profile packages come in phase 6: until then
    there are none, and a PDK that matches its lock is 'unverified' (trust on first use)."""
    return None


def verify(pdk: Pdk, lock_pdk: dict, only_cached: bool = False) -> dict:
    """Compares an install with the lock's `pdk` object (format 2). state:
    - 'other version': the install is another variant or commit than the lock's (nothing hashed);
    - 'unchecked': only_cached and the stamp cache is not up to date (nothing read);
    - 'modified': a locked part differs (`changed` names the part and, when known here, its files);
    - 'verified': it matches the hashes a profile publishes; 'unverified': it matches the lock's own hashes.
    A library installed beyond the lock is fine (extra_libraries); one locked but absent is in missing_libraries."""
    if not isinstance(lock_pdk, dict) or not isinstance(lock_pdk.get("tree"), dict):
        raise PinError("the lock pins the PDK by id only (format 1): run `undine pdk verify PROJECT --pin` to pin "
                       "its tree")
    lock_tree: dict = lock_pdk["tree"]
    warnings: list[str] = []
    if not pdk.commit:
        warnings.append(f"{pdk.name} at {pdk.root} has no version (a plain directory): it is pinned by its tree "
                        f"hashes only")
    installed = pdk.libraries()
    locked = list(lock_pdk.get("libraries") or [])
    locked += [p.split("/", 1)[1] for p in lock_tree if p.startswith("libs.ref/") and p.split("/", 1)[1] not in locked]
    out = {"state": "", "changed": [], "missing_libraries": sorted(set(locked) - set(installed)),
           "extra_libraries": sorted(set(installed) - set(locked)), "warnings": warnings}
    if lock_pdk.get("variant") != pdk.variant or (lock_pdk.get("commit") or None) != pdk.commit:
        out["state"] = "other version"
        return out
    cache = _hash(pdk, only_cached)
    if cache is None:
        out["state"] = "unchecked"
        return out
    here = cache["parts"]
    for part, want in sorted(lock_tree.items()):
        if part.startswith("libs.ref/") and part not in here:
            continue                                      # a missing library: reported as such
        if here.get(part) == want:
            continue
        diff = _diff(cache["seen"].get(part, {}), want, here.get(part))
        if diff is None:
            diff = {"added": [], "removed": [], "modified": []}
            warnings.append(f"{part} differs from the lock, but which files changed is not known here (the lock's "
                            f"hash was made elsewhere)")
        out["changed"].append({"part": part, **diff})
    expected = expected_trees(family_of(pdk.variant), pdk.variant, pdk.commit)
    if out["changed"]:
        out["state"] = "modified"
    elif expected is not None and all(expected.get(p) == h for p, h in lock_tree.items()):
        out["state"] = "verified"
    else:
        out["state"] = "unverified"
    return out


def _diff(seen: dict, want: str, have: str | None) -> dict | None:
    """The files that differ between two remembered hashes of a part (have None: the part is gone)."""
    old = seen.get(want)
    new = seen.get(have, {}) if have else {}
    if not isinstance(old, dict) or (have and not isinstance(seen.get(have), dict)):
        return None
    return {"added": sorted(set(new) - set(old)), "removed": sorted(set(old) - set(new)),
            "modified": sorted(k for k in set(old) & set(new) if old[k] != new[k])}


# ------------------------------------------------------------------ commits and roots


def resolve_commit(prefix: str, variant: str, installs: list[Pdk], allow_network: bool) -> str:
    """The full commit a prefix (the 12 characters of a PDK id) stands for: the one installed commit of the variant
    starting with it, else the one `ciel ls-remote` lists, which uses the network and so only with allow_network."""
    prefix = (prefix or "").lower()
    if not _PREFIX.fullmatch(prefix):
        raise PinError(f"{prefix!r} is not a PDK commit or a prefix of one (7 to 40 hex characters)")
    if _FULL.fullmatch(prefix):
        return prefix
    here = sorted({p.commit for p in installs if p.variant == variant and p.commit and p.commit.startswith(prefix)})
    if len(here) == 1:
        return here[0]
    if len(here) > 1:
        raise PinError(f"{variant}@{prefix} matches several installed commits ({', '.join(here)}): give more of "
                       f"the commit")
    if not allow_network:
        raise PinError(f"{variant}@{prefix} is not installed here, and finding its full commit needs the network: "
                       f"pass --network (or tick 'use the network' in the dialog)")
    family = family_of(variant)
    there = sorted({c for c in ciel.ls_remote(family) if c.startswith(prefix)})
    if len(there) > 1:
        raise PinError(f"{variant}@{prefix} matches several commits of {family} ({', '.join(there)}): give more "
                       f"of the commit")
    if not there or not _FULL.fullmatch(there[0]):
        raise PinError(f"ciel's source has no {family} commit starting with {prefix}: check the PDK id")
    return there[0]


def fetch_root(roots: list[Path]) -> Path:
    """Where `pdk fetch` lets ciel install: the first root scanned that is a ciel root (has ciel/), or that does not
    exist yet, or is empty. A root with only volare/ (or anything else) is passed over, since ciel's store there
    would not be the one Undine finds."""
    for r in roots:
        r = Path(r).expanduser()
        if (r / "ciel").is_dir() or not r.exists() or (r.is_dir() and not any(r.iterdir())):
            return r.absolute()
    names = ", ".join(str(Path(r).expanduser()) for r in roots) or "none"
    raise PinError(f"no PDK root scanned is a ciel root (scanned: {names}): pass --pdk-root DIR or set PDK_ROOT to "
                   f"a directory for ciel (a new or empty one, or one with ciel/ in it)")
