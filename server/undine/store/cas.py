"""The content-addressed store: dependency files kept once per user, installed into projects by hard link.

Layout under home.store_root() ($UNDINE_STORE, else ~/.undine/store):

    objects/sha256/<hex[:2]>/<hex[2:]>   a file's bytes, named by their SHA-256 (no git header); read-only (0o444)
    trees/sha256/<hex>.json              a tree: {path: [mode, hex]}, named by tree_hash() of that content
    trees/archive/<sha>.json             the same, named by archive_sha(): the lock format 1 `sha` of an entry
    projects/<sha256(realpath)>.json     {path, synced}: the projects whose locks keep trees alive (gc)
    tmp/                                 every write lands here first, then is renamed into place
    .lock                                store_lock(): shared while an operation fetches, records and installs;
                                         exclusive for gc

A file's mode ('100644' or '100755') enters the tree hash only: one inode is linked into every project that uses it,
and one inode cannot carry two modes, so every object and every installed file is 0o444.

Writers here do not take store_lock: the caller holds store_lock(shared=True) for its whole fetch, record, install
and register sequence, and gc takes it exclusive. Every write goes to tmp/ and is renamed into place, so a reader
sees a whole object or record, or none. Readers re-hash what they read: a record that does not hash to its name is
treated as absent, and an object is re-hashed before it is installed.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath

from undine.home import store_root

MARKER = ".undine-dep.json"                  # deps.MARKER: the install marker, never part of a tree's hash
MODES = ("100644", "100755")
_HEX = re.compile(r"^[0-9a-f]{64}$")
_CHUNK = 1 << 20
_TMP_AGE = 24 * 3600                         # gc clears tmp/ entries older than this: a crashed writer's leftovers

Files = Mapping[str, "tuple[str, str]"]      # {POSIX path below the subdir: (mode, hex)}


class StoreError(Exception):
    """`paths`: the tree paths whose objects were bad or missing (install), so the caller re-reads them from git."""

    def __init__(self, message: str, paths: list[str] | None = None):
        super().__init__(message)
        self.paths = paths or []


# ------------------------------------------------------------------ paths


def _check_hex(h) -> str:
    if not isinstance(h, str) or not _HEX.match(h):
        raise StoreError(f"not a SHA-256 hex digest: {h!r}")
    return h


def object_path(hex: str) -> Path:
    _check_hex(hex)
    return store_root() / "objects" / "sha256" / hex[:2] / hex[2:]


def tree_path(tree_id: str) -> Path:
    if not isinstance(tree_id, str) or not tree_id.startswith("sha256:"):
        raise StoreError(f"not a tree id (sha256:<hex>): {tree_id!r}")
    return store_root() / "trees" / "sha256" / f"{_check_hex(tree_id[7:])}.json"


def archive_path(sha: str) -> Path:
    return store_root() / "trees" / "archive" / f"{_check_hex(sha)}.json"


def tmp_dir() -> Path:
    d = store_root() / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def link_mode() -> str:
    mode = os.environ.get("UNDINE_STORE_LINK") or "link"
    if mode not in ("link", "copy"):
        raise StoreError(f"UNDINE_STORE_LINK is {mode!r}: set it to link (the default) or copy")
    return mode


def link_or_copy(src: Path, dst: Path) -> bool:
    """Hard-links src to dst; True when it linked. Copies (read-only) instead when UNDINE_STORE_LINK=copy, or when the
    file system cannot link it: another device (EXDEV), no hard links (EPERM, ENOTSUP), or an object linked into so
    many projects that it reached the file system's link limit (EMLINK)."""
    if link_mode() == "link":
        try:
            os.link(src, dst)
            return True
        except OSError as e:
            if e.errno not in (errno.EXDEV, errno.EPERM, errno.EMLINK, errno.ENOTSUP, errno.EOPNOTSUPP):
                raise
    shutil.copy2(src, dst)
    os.chmod(dst, 0o444)                     # a copy is its own inode: read-only like a link would be
    return False


# ------------------------------------------------------------------ objects


def put_file(fileobj) -> tuple[str, int]:
    """Stores what `fileobj` reads; returns (hex, size). The bytes go to a temporary file in tmp/ while they are
    hashed (the object's directory is only known at the end), which is then renamed into place, or dropped when the
    object is already there."""
    h = hashlib.sha256()
    size = 0
    with tempfile.NamedTemporaryFile(dir=tmp_dir(), prefix="obj-", delete=False) as t:
        try:
            while chunk := fileobj.read(_CHUNK):
                h.update(chunk)
                t.write(chunk)
                size += len(chunk)
            os.fchmod(t.fileno(), 0o444)
        except BaseException:
            os.unlink(t.name)
            raise
    hex = h.hexdigest()
    dst = object_path(hex)
    if dst.exists():                         # never replace a stored object: installs share its inode
        os.unlink(t.name)
        return hex, size
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(t.name, dst)
    return hex, size


def put_bytes(b: bytes) -> str:
    return put_file(io.BytesIO(b))[0]


def _hash_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def check_object(hex: str, size: int | None = None, repair_mode: bool = False) -> bool:
    """True when the object is there, a regular read-only file, of `size` bytes (when given), and hashes to its name.

    repair_mode: a good object that is merely not read-only is made so again and counts as good, as verify does.
    Installs are hard links, so a `chmod u+w` on an installed file in any project changes the object's mode without
    touching its bytes. When the file system does not keep the chmod, the object is still accepted on its hash."""
    p = object_path(hex)
    try:
        st = p.lstat()
        if not stat.S_ISREG(st.st_mode):
            return False
        wrong_mode = stat.S_IMODE(st.st_mode) != 0o444
        if (wrong_mode and not repair_mode) or (size is not None and st.st_size != size):
            return False
        if _hash_file(p) != hex:
            return False
        if wrong_mode:
            with contextlib.suppress(OSError):
                os.chmod(p, 0o444)
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ trees


def _check_files(files: Files) -> None:
    """Refuses a tree whose paths could leave the directory it is installed into, or whose entries are malformed."""
    for path, entry in files.items():
        if (not isinstance(path, str) or not path or path.startswith("/") or "\0" in path
                or any(part in ("", ".", "..") for part in path.split("/"))):
            raise StoreError(f"a tree has a bad path: {path!r}")
        if not isinstance(entry, (tuple, list)) or len(entry) != 2 or entry[0] not in MODES:
            raise StoreError(f"a tree has a bad entry for {path!r}: {entry!r} (want [mode, hex], mode 100644 or 100755)")
        _check_hex(entry[1])


def git_part(name: str) -> bool:
    """A path part git refuses in a tree (verify_path): .git in any case, and the names a case-insensitive or Windows
    file system takes for it (trailing dots or spaces, the 8.3 short name git~1). Installed, it would make a
    dependency's directory a repository whose config (core.fsmonitor, core.pager: commands) the dependency wrote."""
    folded = name.casefold()
    return folded.rstrip(" .") == ".git" or folded == "git~1"


def _check_installable(files: Files) -> None:
    for path in files:
        if any(git_part(x) for x in path.split("/")):
            raise StoreError(f"a tree has a .git part in {path!r}, which git itself refuses in a tree: rename it in "
                             "the repository")


def _utf8(path: str) -> bytes:
    return path.encode("utf-8", "surrogateescape")


def tree_hash(files: Files) -> str:
    """'sha256:' + SHA-256 of f"{mode} {hex} {path}\\0" for each file, in order of the UTF-8 bytes of its path."""
    _check_files(files)
    h = hashlib.sha256()
    for path in sorted(files, key=_utf8):
        mode, hex = files[path]
        h.update(f"{mode} {hex} ".encode() + _utf8(path) + b"\0")
    return "sha256:" + h.hexdigest()


def archive_sha(files: Files) -> str:
    """The lock format 1 `sha` (deps.tree_sha of the installed directory) of these files: `path\\0hex\\n` for each, in
    PurePosixPath order, with any file named MARKER left out at any depth. Byte for byte what tree_sha computes."""
    _check_files(files)
    h = hashlib.sha256()
    for p in sorted(PurePosixPath(k) for k in files if PurePosixPath(k).name != MARKER):
        h.update(f"{p.as_posix()}\0{files[p.as_posix()][1]}\n".encode("utf-8", "surrogateescape"))
    return h.hexdigest()


def _replace_from_tmp(dst: Path, data: bytes, mode: int) -> None:
    with tempfile.NamedTemporaryFile(dir=tmp_dir(), prefix="rec-", delete=False) as t:
        try:
            t.write(data)
            os.fchmod(t.fileno(), mode)
        except BaseException:
            os.unlink(t.name)
            raise
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(t.name, dst)


def _record(files: Files) -> bytes:
    return (json.dumps({p: list(e) for p, e in files.items()}, indent=1, sort_keys=True) + "\n").encode()


def _load_record(p: Path) -> dict[str, tuple[str, str]] | None:
    try:
        raw = json.loads(p.read_bytes())
        if not isinstance(raw, dict):
            return None
        files = {k: tuple(v) for k, v in raw.items() if isinstance(v, list)}
        if len(files) != len(raw):
            return None
        _check_files(files)
        return files
    except (OSError, ValueError, StoreError):
        return None


def write_tree(files: Files) -> str:
    tree_id = tree_hash(files)
    if read_tree(tree_id) is None:
        _replace_from_tmp(tree_path(tree_id), _record(files), 0o444)
    return tree_id


def write_archive_tree(files: Files) -> str:
    """Records a format 1 entry's files under its archive sha. Files named MARKER are left out: the sha does not cover
    them, and a record holds only what its name vouches for."""
    files = {p: e for p, e in files.items() if PurePosixPath(p).name != MARKER}
    sha = archive_sha(files)
    if read_archive_tree(sha) is None:
        _replace_from_tmp(archive_path(sha), _record(files), 0o444)
    return sha


def read_tree(tree_id: str) -> dict[str, tuple[str, str]] | None:
    """The tree's files, or None when it is not recorded or its record does not hash to its name."""
    files = _load_record(tree_path(tree_id))
    return files if files is not None and tree_hash(files) == tree_id else None


def read_archive_tree(sha: str) -> dict[str, tuple[str, str]] | None:
    files = _load_record(archive_path(sha))
    return files if files is not None and archive_sha(files) == sha else None


def missing_objects(files: Files) -> list[str]:
    """The objects (hex) these files need that the store does not have, each once. Presence only: install re-hashes."""
    out: dict[str, None] = {}
    for _, hex in files.values():
        if hex not in out and not object_path(hex).is_file():
            out[hex] = None
    return list(out)


def _walk_files(d: Path) -> Iterator[tuple[str, Path, os.stat_result]]:
    """(POSIX path below d, path, lstat) of every entry of d that is not a directory, MARKER at any depth excluded.
    Symbolic links are not followed."""
    for root, dirs, names in os.walk(d):
        dirs.sort()
        for n in sorted(names):
            if n == MARKER:
                continue
            p = Path(root) / n
            yield p.relative_to(d).as_posix(), p, p.lstat()


def put_dir(d: Path) -> dict[str, tuple[str, str]]:
    """Imports the regular files of a directory (an install made before the store existed) with mode 100644; returns
    its files. Links and other special files are skipped: a dependency install never has them."""
    files: dict[str, tuple[str, str]] = {}
    for rel, p, st in _walk_files(Path(d)):
        if stat.S_ISREG(st.st_mode):
            with open(p, "rb") as f:
                files[rel] = ("100644", put_file(f)[0])
    return files


# ------------------------------------------------------------------ install


def install(files: Files, staging: Path) -> None:
    """Links (or copies) every file into `staging`. Every object is re-hashed first: a bad one is removed from the
    store and StoreError names the paths that need it, before anything is linked, so the caller re-reads them from
    git and calls again. An object whose only fault is its mode (a chmod through a project's hard link) is good: it
    is made read-only again, not removed."""
    _check_files(files)
    _check_installable(files)                # a record or lock from elsewhere: blob_tree refuses these already
    bad, good = [], set()
    for path, (_, hex) in files.items():
        if hex in good:
            continue
        if check_object(hex, repair_mode=True):
            good.add(hex)
            continue
        bad.append(path)
        with contextlib.suppress(FileNotFoundError):
            object_path(hex).unlink()
    if bad:
        raise StoreError(f"{len(bad)} file(s) are missing or damaged in the store: {', '.join(sorted(bad)[:5])}",
                         sorted(bad))
    staging = Path(staging)
    for path, (_, hex) in files.items():
        dst = staging.joinpath(*path.split("/"))
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            link_or_copy(object_path(hex), dst)          # a linked file is never chmodded: the inode is shared
        except (FileExistsError, NotADirectoryError) as e:
            raise StoreError(f"cannot install {path!r} into {staging}: {e.strerror}") from None


def diff_installed(d: Path, files: Files) -> dict[str, list[str]]:
    """How an installed directory differs from its tree: paths added, removed or modified (a link on disk counts as
    modified), MARKER ignored at any depth."""
    want = {p: e[1] for p, e in files.items() if PurePosixPath(p).name != MARKER}
    have: dict[str, str | None] = {}
    if Path(d).is_dir():
        for rel, p, st in _walk_files(Path(d)):
            have[rel] = _hash_file(p) if stat.S_ISREG(st.st_mode) else None
    return {"added": sorted(set(have) - set(want)), "removed": sorted(set(want) - set(have)),
            "modified": sorted(p for p in set(want) & set(have) if have[p] != want[p])}


# ------------------------------------------------------------------ locks and projects


@contextlib.contextmanager
def store_lock(shared: bool):
    """flock on <store>/.lock: shared while an operation fetches, records, installs and registers; exclusive for gc,
    which waits until those are done. Each call opens the file anew, so threads lock each other too."""
    root = store_root()
    root.mkdir(parents=True, exist_ok=True)
    with open(root / ".lock", "a") as f:
        fcntl.flock(f, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _project_record(realpath: str) -> Path:
    return store_root() / "projects" / f"{hashlib.sha256(realpath.encode('utf-8', 'surrogateescape')).hexdigest()}.json"


def register_project(path: Path) -> None:
    """Notes that this project's lock keeps trees alive, for gc."""
    real = os.path.realpath(path)
    rec = {"path": real, "synced": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    _replace_from_tmp(_project_record(real), (json.dumps(rec, indent=1) + "\n").encode(), 0o644)


def registered_projects() -> list[Path]:
    """The registered projects that still exist; the records of the others (and unreadable ones) are removed."""
    out = []
    d = store_root() / "projects"
    for rec in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            path = Path(json.loads(rec.read_text())["path"])
        except (OSError, ValueError, KeyError, TypeError):
            path = None
        if path is not None and path.is_dir():
            out.append(path)
        else:
            rec.unlink(missing_ok=True)
    return out


# ------------------------------------------------------------------ maintenance


def _objects() -> Iterator[tuple[str, Path]]:
    base = store_root() / "objects" / "sha256"
    for sub in sorted(base.iterdir()) if base.is_dir() else []:
        for p in sorted(sub.iterdir()) if sub.is_dir() else []:
            yield sub.name + p.name, p


def _records(kind: str) -> Iterator[tuple[str, Path]]:
    base = store_root() / "trees" / kind
    for p in sorted(base.glob("*.json")) if base.is_dir() else []:
        yield p.stem, p


def verify(dry_run: bool = False) -> dict:
    """Re-hashes every object and record. Objects that do not hash to their name are removed (`bad`); good ones that
    are not read-only are made so (`wrong_mode`); records that do not hash to their name, or that name an object the
    store does not have, are `broken_trees` (tree ids, and archive shas as 'archive:<sha>'), and the ones that do not
    hash are removed. With dry_run nothing is changed. It takes store_lock shared itself, so a caller must not hold
    the lock exclusive (flock is per open file: it would wait for itself)."""
    out: dict = {"checked": 0, "bad": [], "wrong_mode": [], "broken_trees": []}
    with store_lock(shared=True):
        for hex, p in _objects():
            out["checked"] += 1
            try:
                st = p.lstat()
                ok = _HEX.match(hex) is not None and stat.S_ISREG(st.st_mode) and _hash_file(p) == hex
            except OSError:
                continue
            if not ok:
                out["bad"].append(hex)
                if not dry_run:
                    p.unlink(missing_ok=True)
            elif stat.S_IMODE(st.st_mode) != 0o444:
                out["wrong_mode"].append(hex)
                if not dry_run:
                    os.chmod(p, 0o444)
        for kind, name, read in (("sha256", "sha256:{}", read_tree), ("archive", "archive:{}", read_archive_tree)):
            for stem, p in _records(kind):
                key = name.format(stem)
                try:
                    files = read(key if kind == "sha256" else stem)
                except StoreError:
                    files = None
                if files is None:
                    out["broken_trees"].append(key)
                    if not dry_run:
                        p.unlink(missing_ok=True)
                elif missing_objects(files):
                    out["broken_trees"].append(key)
    return out


def _size(p: Path) -> int:
    """What removing p frees: nothing when an install still links its inode."""
    try:
        st = p.lstat()
    except OSError:
        return 0
    return st.st_size if st.st_nlink <= 1 else 0


def gc(live_trees: set[str], live_archive: set[str], dry_run: bool = False, held: bool = False) -> dict:
    """Removes the tree and archive records not in the live sets, then the objects no remaining record names, and
    tmp/ entries older than a day. Under an exclusive store_lock, so it waits for every operation in progress.
    `held`: the caller holds that lock already (`store gc` builds the live sets under it, so no operation can record
    a tree between the two). Returns {trees, objects, bytes}: the records and objects removed (or that would be, with
    dry_run), and the bytes that frees. Kept git repositories, PDK records and builds are left alone."""
    out = {"trees": 0, "objects": 0, "bytes": 0}
    with contextlib.nullcontext() if held else store_lock(shared=False):
        keep: set[str] = set()
        for kind, live in (("sha256", {t[7:] for t in live_trees if t.startswith("sha256:")}), ("archive", live_archive)):
            for stem, p in _records(kind):
                files = _load_record(p) if stem in live else None
                if files is None:
                    out["trees"] += 1
                    out["bytes"] += _size(p)
                    if not dry_run:
                        p.unlink(missing_ok=True)
                else:
                    keep.update(e[1] for e in files.values())
        for hex, p in _objects():
            if hex not in keep:
                out["objects"] += 1
                out["bytes"] += _size(p)
                if not dry_run:
                    p.unlink(missing_ok=True)
                    with contextlib.suppress(OSError):
                        p.parent.rmdir()                 # the fan-out directory, once empty
        tmp = store_root() / "tmp"
        cutoff = time.time() - _TMP_AGE
        for p in list(tmp.iterdir()) if tmp.is_dir() else []:
            try:
                if p.lstat().st_mtime >= cutoff:
                    continue
            except OSError:
                continue
            out["bytes"] += _size(p) if not p.is_dir() else 0
            if dry_run:
                continue
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
    return out
