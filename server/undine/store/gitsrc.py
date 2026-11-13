"""Git dependencies in the store: kept bare repositories, and a commit's files read into objects/.

store/git/<sha256 of URL>.git is a bare repository per dependency URL, fetched into and kept: the next version of a
dependency fetches only what changed, and a commit fetched before needs no network. Each commit fetched is kept by a
ref, refs/undine/<commit>, so a `git gc` there never drops it. One process fetches into a repository at a time (an
flock on git/<h>.git.lock, taken shared by the readers below).

Two ways to read a commit's files into the store, both returning {path below subdir: (mode, hex)}:

* blob_tree: lock format 2. Straight from git's objects (ls-tree, cat-file), as the tree hash (cas.tree_hash) is
  defined: no .gitattributes, no core.autocrlf, so one commit gives the same files on every machine. Only regular
  files; a symlink, a submodule, an LFS pointer or a path that could leave the install directory is refused. With
  files='include', only what the top-level undine-package.toml's [files] include selects (and the manifest) is
  checked and read; the rest of the subdir is listed, never read. read_file reads one file (a manifest) at a commit.
* archive_tree: lock format 1, whose `sha` (cas.archive_sha) is over `git archive` output as phase 0 computed it:
  export-subst, eol conversion and the machine's core.autocrlf applied. It runs in a throwaway repository that holds
  what phase 0's depth-1 fetch into an empty repository held: the commit's own objects, fetched at depth 1 from the
  kept repository, and no refs. So export-subst's %d, %D and %(describe) expand as they did then, and %h has the same
  length (git sizes an abbreviation by the number of objects it can see).

Callers hold cas.store_lock(shared=True) and run deps' checks first (_check_spec, _check_commit, _own_dir, and
_source over HTTP); what reaches git as an argument is checked here again. Nothing here writes into a project.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Literal

from undine.home import store_root
from undine.store import cas, manifest

MAX_BYTES = int(os.environ.get("UNDINE_FETCH_MAX", 1 << 30))     # one dependency tree, summed over its files
MAX_FILES = 100_000

_COMMIT = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")             # SHA-1 or SHA-256, in full
_REV = re.compile(r"^(?!-)[\w./@{}^~+=,-]+$")                        # deps._REV: never an option
_LFS = b"version https://git-lfs.github.com/spec/v1"
_LFS_MAX = 1024                                                      # LFS pointers are well under this
_TIMEOUT = 600
_KINDS = {"120000": "a symbolic link", "160000": "a submodule", "040000": "a directory"}
# a repository named in the environment would take the place of the one given with --git-dir, or add objects to it
_REPO_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
             "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_QUARANTINE_PATH")


class GitSourceError(RuntimeError):
    pass


# ------------------------------------------------------------------ git


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _REPO_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"                 # no password prompt: access is ssh keys or a credential helper
    return env


def _cmd(repo: Path | None, *args: str) -> list[str]:
    return ["git", "-c", "protocol.ext.allow=never", "-c", "gc.auto=0",
            *([f"--git-dir={repo}"] if repo is not None else []), *args]


def _last_line(stderr: bytes) -> str:
    """git's last line of complaint; a lock file git could not create is named instead, with its path (the last line
    then only says "remove the file manually to continue")."""
    lines = stderr.decode(errors="replace").strip().splitlines()
    locked = [x for x in lines if "Unable to create '" in x and ".lock'" in x]
    return locked[0] if locked else lines[-1] if lines else ""


def _run(repo: Path | None, *args: str, timeout: int = _TIMEOUT) -> bytes:
    proc = subprocess.Popen(_cmd(repo, *args), env=_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop(proc)
        raise GitSourceError(f"git {args[0]} took longer than {timeout} s: try again, or check the network") from None
    if proc.returncode != 0:
        raise GitSourceError(_last_line(err) or f"git {args[0]} failed")
    return out


def _stop(proc: subprocess.Popen) -> None:
    """SIGTERM first, so that git removes its lock files (shallow.lock, ...) itself; SIGKILL if it does not stop."""
    proc.terminate()
    try:
        proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()


def _git(repo: Path | None, *args: str) -> str:
    return _run(repo, *args).decode(errors="replace").strip()


# ------------------------------------------------------------------ checks


def _check_commit(commit) -> None:
    if not (isinstance(commit, str) and _COMMIT.match(commit)):
        raise GitSourceError(f"not a full commit id (40 or 64 lowercase hex characters): {commit!r}")


def _check_url(url) -> None:
    if not isinstance(url, str) or not url or url.startswith("-") or "\0" in url:
        raise GitSourceError(f"not a git URL or repository path: {url!r}")


def _subdir(subdir) -> str:
    """The subdir as git names it below the top ('' for the top): './lib/' is 'lib'."""
    sub = subdir or ""
    p = PurePosixPath(sub) if isinstance(sub, str) else None
    if p is None or "\0" in sub or p.is_absolute() or any(x == ".." or x.startswith("-") for x in p.parts):
        raise GitSourceError(f"subdir must be a relative path inside the repository (no .., no part starting with "
                             f"-): {subdir!r}")
    return "/".join(p.parts)


def _path(name: bytes, where: str) -> str:
    """A tree path from ls-tree, refused when it could not be installed as it is named."""
    try:
        path = name.decode("utf-8")
    except UnicodeDecodeError:
        raise GitSourceError(f"{where}: the path {name.decode(errors='backslashreplace')!r} is not UTF-8: a "
                             "dependency's file names must be") from None
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in path):
        raise GitSourceError(f"{where}: the path {path!r} has a control character in it")
    parts = path.split("/")
    if path.startswith("/") or any(x in ("", ".", "..") for x in parts):
        raise GitSourceError(f"{where}: the path {path!r} would leave the dependency's directory")
    if cas.MARKER in parts:
        raise GitSourceError(f"{where}: {path} is named {cas.MARKER}, the name Undine gives its install marker: "
                             "rename it in the repository")
    if any(cas.git_part(x) for x in parts):
        raise GitSourceError(f"{where}: {path} has a .git part, which git itself refuses in a tree: rename it in the "
                             "repository")
    return path


# ------------------------------------------------------------------ kept repositories


def repo_path(url: str) -> Path:
    _check_url(url)
    return store_root() / "git" / f"{hashlib.sha256(url.encode('utf-8', 'surrogateescape')).hexdigest()}.git"


@contextlib.contextmanager
def _repo_lock(repo: Path, shared: bool):
    """flock on git/<h>.git.lock: exclusive while fetching into the repository, shared while reading it."""
    repo.parent.mkdir(parents=True, exist_ok=True)
    with open(repo.with_name(repo.name + ".lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _ensure_repo(repo: Path, commit: str | None) -> None:
    """Creates the kept repository once: in tmp/, then renamed into place, so a crash leaves no half-made one."""
    if (repo / "HEAD").is_file() and (repo / "objects").is_dir():
        return
    shutil.rmtree(repo, ignore_errors=True)
    tmp = Path(tempfile.mkdtemp(dir=cas.tmp_dir(), prefix="git-"))
    try:
        # no template: no hooks or sample files from the user's init.templateDir in a repository that fetches
        fmt = ["--object-format=sha256"] if commit is not None and len(commit) == 64 else []
        _git(None, "init", "-q", "--bare", "--template=", *fmt, str(tmp))
        os.replace(tmp, repo)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def has_commit(url: str, commit: str) -> bool:
    """True when the kept repository for `url` has `commit`."""
    _check_commit(commit)
    repo = repo_path(url)
    if not (repo / "HEAD").is_file():
        return False
    r = subprocess.run(_cmd(repo, "cat-file", "-e", f"{commit}^{{commit}}"), env=_env(), capture_output=True,
                       timeout=60)
    return r.returncode == 0


# the lock files git makes in a repository while it writes it; one left behind (git killed, a crash, a power loss)
# fails every later fetch there
_GIT_LOCKS = ("shallow.lock", "packed-refs.lock", "config.lock", "HEAD.lock", "index.lock", "FETCH_HEAD.lock")


def _clear_stale_locks(repo: Path) -> None:
    """Removes the lock files a git killed while fetching left in the kept repository. Called under the exclusive
    _repo_lock: every git that writes this repository runs under it, so a lock file found then is no one's."""
    for name in _GIT_LOCKS:
        (repo / name).unlink(missing_ok=True)
    refs = repo / "refs"
    for p in refs.rglob("*.lock") if refs.is_dir() else []:
        if p.is_file() and not p.is_symlink():
            p.unlink(missing_ok=True)


def _plain(url: str) -> str:
    return str(Path(url).expanduser()) if url.startswith("~/") else url


def fetch(url: str, source: str | None, want: str, commit: str | None = None) -> str:
    """Fetches `want` (a branch, tag or commit; `commit` when the lock gives one) of `url` into its kept repository;
    returns the full commit. `source`: what git fetches from (deps._source's result over HTTP), else `url` itself.
    A depth-1 fetch first; all heads and tags only when the server does not serve `want` by itself, as phase 0 did.
    `commit` given and already kept: no network."""
    _check_url(url)
    if commit is not None:
        _check_commit(commit)
    if not (isinstance(want, str) and _REV.match(want)):
        raise GitSourceError(f"not a branch, tag or commit: {want!r} (it does not start with -)")
    src = source or _plain(url)
    _check_url(src)
    repo = repo_path(url)
    with _repo_lock(repo, shared=False):
        if commit is not None and has_commit(url, commit):
            _keep(repo, commit)
            return commit
        _ensure_repo(repo, commit)
        _clear_stale_locks(repo)
        try:
            _git(repo, "fetch", "-q", "--depth", "1", "--end-of-options", src, want)
            got = _git(repo, "rev-parse", "--verify", "FETCH_HEAD^{commit}")
        except GitSourceError:      # a commit the server does not serve by itself: fetch everything, then look it up
            got = _fetch_all(repo, url, src, want)
        if commit is not None and got != commit:
            raise GitSourceError(f"{url} gave {got[:12]}, not the locked {commit[:12]}")
        _keep(repo, got)
        return got


def _fetch_all(repo: Path, url: str, src: str, want: str) -> str:
    # an earlier depth-1 fetch left the repository shallow: without --unshallow the heads it already has would bring
    # no history, and an older commit would stay out of reach
    shallow = _git(repo, "rev-parse", "--is-shallow-repository") == "true"
    _git(repo, "fetch", "-q", *(["--unshallow"] if shallow else []), "--end-of-options", src,
         "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*")
    try:
        return _git(repo, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{want}^{{commit}}")
    except GitSourceError:
        raise GitSourceError(f"{url} has no branch, tag or commit {want!r}") from None


def _keep(repo: Path, commit: str) -> None:
    _git(repo, "update-ref", f"refs/undine/{commit}", commit)


def _need(repo: Path, url: str, commit: str) -> None:
    if not has_commit(url, commit):
        raise GitSourceError(f"{url} @ {commit[:12]} is not in the store's copy of the repository: fetch it first")


# ------------------------------------------------------------------ blob tree (lock format 2)


def _entry(rec: bytes) -> tuple[bytes, str, str, int]:
    meta, _, name = rec.partition(b"\t")
    mode, _kind, oid, size = meta.decode().split()
    return name, mode, oid, -1 if size == "-" else int(size)          # '-': a submodule has no size


def _list(url: str, commit: str, sub: str) -> list[tuple[bytes, str, str, int]]:
    """(path, mode, object id, size; -1 for a submodule) of every entry under `sub`, unchecked: the include set is
    chosen before paths and modes are checked. Read as git writes it and stopped past 10 * MAX_FILES entries, so a
    huge tree is never held whole, also when the include set would keep only part of it."""
    where, limit = f"{url} @ {commit[:12]}", 10 * MAX_FILES
    proc = subprocess.Popen(_cmd(repo_path(url), "ls-tree", "-r", "-z", "-l", "--end-of-options",
                                 f"{commit}:{sub}" if sub else f"{commit}^{{tree}}"),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_env())
    out, rest = [], b""
    try:
        for chunk in iter(lambda: proc.stdout.read(1 << 16), b""):
            *recs, rest = (rest + chunk).split(b"\0")
            out.extend(_entry(r) for r in recs if r)
            if len(out) > limit:
                raise GitSourceError(f"{where}: more than {limit} entries under {sub or 'the top'}, far more than "
                                     f"the {MAX_FILES} files a dependency may have")
        err = proc.stderr.read()
        rc = proc.wait(timeout=_TIMEOUT)
    finally:
        if proc.poll() is None:
            _stop(proc)
        for f in (proc.stdout, proc.stderr):
            with contextlib.suppress(OSError):
                f.close()
    if rc != 0:
        raise GitSourceError(f"{where} has no directory {sub!r} ({_last_line(err) or 'git ls-tree failed'})")
    if rest:
        out.append(_entry(rest))
    return out


def _check(entries, url: str, commit: str, sub: str) -> list[tuple[str, str, str, int]]:
    """(path, mode, blob id, size) of `entries`, checked as a whole before any blob is read: each path installable
    as it is named, regular files only, and the count and size limits."""
    where = f"{url} @ {commit[:12]}"
    out, total = [], 0
    for name, mode, oid, size in entries:
        path = _path(name, where)
        if mode not in cas.MODES:
            raise GitSourceError(f"{where}: {path} is {_KINDS.get(mode, f'of mode {mode}')}: a dependency holds "
                                 "regular files only")
        out.append((path, mode, oid, size))
        total += size
    if not out:
        raise GitSourceError(f"nothing at {sub or 'the top'} of {where}")
    if len(out) > MAX_FILES:
        raise GitSourceError(f"{where}: {len(out)} files under {sub or 'the top'}, more than the {MAX_FILES} a "
                             "dependency may have")
    if total > MAX_BYTES:
        raise GitSourceError(f"{where}: {total} bytes under {sub or 'the top'}, more than the {MAX_BYTES} a "
                             "dependency may have (UNDINE_FETCH_MAX)")
    return out


@contextlib.contextmanager
def _cat_file(repo: Path):
    """One `git cat-file --batch` for every blob of a tree. No --filters: the bytes as git stores them."""
    proc = subprocess.Popen(_cmd(repo, "cat-file", "--batch"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=_env())
    try:
        yield proc
        proc.stdin.close()
        proc.wait(timeout=_TIMEOUT)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for f in (proc.stdin, proc.stdout):
            with contextlib.suppress(OSError):
                f.close()


class _Exact:
    """Reads exactly `size` bytes of a stream, for cas.put_file."""

    def __init__(self, stream, size: int):
        self.stream, self.left = stream, size

    def read(self, n: int = -1) -> bytes:
        n = self.left if n < 0 else min(n, self.left)
        b = self.stream.read(n) if n else b""
        self.left -= len(b)
        return b


def _read_blob(proc, oid: str, size: int, where: str) -> str:
    proc.stdin.write(oid.encode() + b"\n")
    proc.stdin.flush()
    header = proc.stdout.readline().split()
    if len(header) != 3 or header[1] != b"blob" or header[2] != str(size).encode():
        raise GitSourceError(f"{where}: git cat-file gave {b' '.join(header).decode(errors='replace')!r} for blob "
                             f"{oid}: the store's copy of the repository may be damaged")
    if size < _LFS_MAX:
        data = proc.stdout.read(size)
        if data.startswith(_LFS):
            raise GitSourceError(f"{where} is a Git LFS pointer, not the file: Undine does not fetch LFS files; "
                                 "commit the file itself")
        hex, got = cas.put_bytes(data), len(data)
    else:
        hex, got = cas.put_file(_Exact(proc.stdout, size))
    if got != size or proc.stdout.read(1) != b"\n":
        raise GitSourceError(f"{where}: git cat-file stopped early on blob {oid}")
    return hex


def _manifest_where(url: str, commit: str, sub: str) -> str:
    return f"{url} @ {commit[:12]}" + (f":{sub}" if sub else "")


def _include_set(repo: Path, entries, url: str, commit: str, sub: str):
    """The entries a top-level manifest's [files] include selects, and the manifest (None when there is none: then
    every entry, so the tree is the one files='all' gives). Only the manifest's blob is read here, after its entry is
    found regular and small enough, so a manifest that is a link or huge is refused before anything else is read."""
    where = _manifest_where(url, commit, sub)
    name = manifest.MANIFEST.encode()

    def refuse(what: str, fix: str):
        return manifest.ManifestError(f"{where}: {manifest.MANIFEST}: {what}: {fix}")
    if any(e[0].startswith(name + b"/") for e in entries):
        raise refuse("is a directory", "a manifest is a file at the top of the package")
    top = next((e for e in entries if e[0] == name), None)
    if top is None:
        return entries, None
    _, mode, oid, size = top
    if mode not in cas.MODES:
        raise refuse(f"is {_KINDS.get(mode, f'of mode {mode}')}",
                     "a manifest is a regular file: commit the file itself")
    if size > manifest.MAX_BYTES:
        raise refuse(f"{size} bytes, more than {manifest.MAX_BYTES}", "a manifest is a short text file")
    m = manifest.parse(_run(repo, "cat-file", "blob", oid), where)
    # a path that is not UTF-8 is kept as it is for selecting; _check refuses it if the include set selects it
    keep = manifest.select(m.include, (e[0].decode("utf-8", "surrogateescape") for e in entries))
    return [e for e in entries if e[0].decode("utf-8", "surrogateescape") in keep], m


def blob_tree(url: str, commit: str, subdir: str | None,
              files: Literal["all", "include"] = "all") -> dict[str, tuple[str, str]]:
    """The files under `subdir` of `commit` (kept by fetch), read from git's objects into the store:
    {path below subdir: (mode, hex)}, for cas.tree_hash. Limits and paths are checked before any blob is read.
    files='all': every file under subdir (lock entries without a `files` key). files='include': only what the
    top-level manifest's [files] include selects, plus the manifest; the rest is neither checked nor read, and a
    manifest without [files] (or no manifest) gives the 'all' tree."""
    if files not in ("all", "include"):
        raise ValueError(f"files must be 'all' or 'include', not {files!r}")
    _check_commit(commit)
    sub = _subdir(subdir)
    repo = repo_path(url)
    with _repo_lock(repo, shared=True):
        _need(repo, url, commit)
        raw, m = _list(url, commit, sub), None
        if files == "include":
            raw, m = _include_set(repo, raw, url, commit, sub)
        entries = _check(raw, url, commit, sub)
        if m is not None:
            manifest.check_files(m, [e[0] for e in entries], _manifest_where(url, commit, sub))
        out: dict[str, tuple[str, str]] = {}
        hexes: dict[str, str] = {}                        # blob id -> object hex: a blob under two paths read once
        with _cat_file(repo) as proc:
            for path, mode, oid, size in entries:
                if oid not in hexes:
                    hexes[oid] = _read_blob(proc, oid, size, f"{url} @ {commit[:12]}: {path}")
                out[path] = (mode, hexes[oid])
    return out


def read_file(url: str, commit: str, subdir: str | None, name: str, max_bytes: int) -> bytes | None:
    """The bytes of `name` below `subdir` at `commit` (kept by fetch), as git stores them; None when there is no such
    entry. A link, submodule or directory there, or a file over `max_bytes`, is refused before it is read. For one
    file (a manifest) without reading the tree into the store."""
    _check_commit(commit)
    sub = _subdir(subdir)
    where = f"{url} @ {commit[:12]}"
    rel = _path(name.encode("utf-8", "surrogateescape") if isinstance(name, str) else b"", where)
    path = f"{sub}/{rel}" if sub else rel
    repo = repo_path(url)
    with _repo_lock(repo, shared=True):
        _need(repo, url, commit)
        # literal: ls-tree's paths are prefixes, not globs, but a pathspec option in the environment could change that
        raw = _run(repo, "--literal-pathspecs", "ls-tree", "-z", "-l", "--end-of-options", commit, "--", path)
        found = [e for e in map(_entry, filter(None, raw.split(b"\0"))) if e[0] == path.encode()]
        if not found:
            return None
        _, mode, oid, size = found[0]
        if mode not in cas.MODES:
            raise GitSourceError(f"{where}: {path} is {_KINDS.get(mode, f'of mode {mode}')}: Undine reads it only "
                                 "as a regular file")
        if size > max_bytes:
            raise GitSourceError(f"{where}: {path} is {size} bytes, more than the {max_bytes} Undine reads of it")
        return _run(repo, "cat-file", "blob", oid)


# ------------------------------------------------------------------ archive tree (lock format 1)


def _view(kept: Path, view: Path, commit: str) -> None:
    """A bare repository holding what phase 0's depth-1 fetch into an empty repository held: `commit` and its tree,
    fetched at depth 1 from the kept repository (a local read, no network), `commit` shallow, and no refs. So
    export-subst sees no ref names, tags or history, and %h is abbreviated against that commit's objects only: git
    sizes an abbreviation by how many objects it can see, which in the kept repository (or through alternates to it)
    can be many more. The object count, and loose objects or a pack by fetch's unpack limit, are phase 0's too."""
    fmt = _git(kept, "rev-parse", "--show-object-format")
    _git(None, "init", "-q", "--bare", "--template=", f"--object-format={fmt}", str(view))
    # by id: the kept repository may have the commit without its refs/undine ref (reached through a fallback fetch's
    # history). Protocol v2 serves any object the repository has; v0, which a global config may ask for, would not
    _git(view, "-c", "protocol.version=2", "fetch", "-q", "--depth", "1", "--no-tags", "--end-of-options",
         str(kept.resolve()), commit)


def _archive(view: Path, url: str, commit: str, sub: str, scratch: Path) -> dict[str, tuple[str, str]]:
    """Streams `git archive` of `commit` into the store: regular files only (links and submodules are skipped, as
    phase 0's extraction did), the subdir stripped, files named MARKER left out (deps.tree_sha leaves them out)."""
    where = f"{url} @ {commit[:12]}"
    files: dict[str, tuple[str, str]] = {}
    total, broken = 0, None
    with tempfile.TemporaryFile(dir=scratch) as err:
        proc = subprocess.Popen(_cmd(view, "archive", "--format=tar", commit, "--", *([sub] if sub else [])),
                                stdout=subprocess.PIPE, stderr=err, env=_env())
        try:
            try:
                with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
                    for m in tar:
                        if not m.isfile():
                            continue
                        rel = PurePosixPath(m.name)
                        if sub:
                            if not rel.is_relative_to(sub):
                                continue
                            rel = rel.relative_to(sub)
                        if rel.is_absolute() or ".." in rel.parts or not rel.parts or rel.name == cas.MARKER:
                            continue
                        try:
                            tarfile.data_filter(m, str(scratch))
                        except tarfile.FilterError as e:
                            raise GitSourceError(f"{where}: {e}") from None
                        total += m.size
                        if len(files) >= MAX_FILES or total > MAX_BYTES:
                            raise GitSourceError(f"{where}: more than {MAX_FILES} files or {MAX_BYTES} bytes under "
                                                 f"{sub or 'the top'} (UNDINE_FETCH_MAX)")
                        files[rel.as_posix()] = ("100644", cas.put_file(tar.extractfile(m))[0])
                proc.stdout.read()                    # the end-of-archive padding, so git can exit
            except tarfile.TarError as e:
                broken = e
            rc = proc.wait(timeout=_TIMEOUT)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
        if rc != 0:
            err.seek(0)
            raise GitSourceError(_last_line(err.read()) or f"git archive of {where} failed")
    if broken is not None:
        raise GitSourceError(f"{where}: git archive gave an unreadable tar ({broken})")
    if not files:
        raise GitSourceError(f"nothing at {sub or 'the top'} of {where}")
    return files


def archive_tree(url: str, commit: str, subdir: str | None) -> dict[str, tuple[str, str]]:
    """The files under `subdir` of `commit` (kept by fetch) as `git archive` gives them, read into the store:
    {path below subdir: ('100644', hex)}, whose cas.archive_sha is the lock format 1 `sha` phase 0 computed for that
    commit. The machine's core.autocrlf and the commit's .gitattributes apply, as they did."""
    _check_commit(commit)
    sub = _subdir(subdir)
    repo = repo_path(url)
    with _repo_lock(repo, shared=True):
        _need(repo, url, commit)
        scratch = Path(tempfile.mkdtemp(dir=cas.tmp_dir(), prefix="archive-"))
        try:
            view = scratch / "view.git"
            _view(repo, view, commit)
            return _archive(view, url, commit, sub, scratch)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
