"""Library dependencies: libraries a project uses at a pinned version, pulled in like packages instead of linked.

project.json names them next to the project's own libraries:

    "libraries": {
      "mylib":     "libs/mylib",                                         the project's own library, edited here
      "analog_ip": {"git": "https://github.com/me/analog_ip.git", "rev": "v1.2.0", "subdir": "lib"}
    }

undine.lock (next to project.json; keep it in version control) pins what they resolved to, and what else the design
depends on:

    {"format": 1,
     "pdk": "sky130A@8afc8346a57f",
     "tools": {"klayout": "KLayout 0.30.12", "xschem": "...", "ngspice": "...", "magic": "...", "netgen": "..."},
     "libraries": {"analog_ip": {"git": "...", "rev": "v1.2.0", "subdir": "lib", "commit": "<sha1>", "sha": "<content>"}}}

That is format 1, which the commands of today write. Format 2 (docs/design/packages.md, Lockfile) pins the PDK as an
object (id, commit, part hashes), records each tool as {version, from} plus Undine's version and its pixi.lock hash, and
may hash a dependency as a `tree` (store/cas.py) instead of a `sha`. It is read here, and written only by the explicit
commands that need it; the other commands keep the format a lock has. The accessors (lock_pdk_id, lock_tools,
entry_hash) read either format.

A dependency is installed read-only into <project>/.undine/deps/<name>/ from exactly the locked commit (`sync`), so
every checkout of the project sees the same cells. `add` and `update` resolve `rev` to a commit and rewrite the lock:
changing a dependency is an explicit step that shows up as a diff of undine.lock, and reverting that diff and running
`sync` restores the previous version. `update` also reports which cells changed and which cells of the project use
them. `status` compares the installed files, the PDK and the tool versions with the lock.

The files come from the content-addressed store (store/cas.py): store/gitsrc.py fetches a commit into a bare repository
kept in the store and reads its files into objects/, the tree is recorded and checked against the lock, and the install
is hard links to those read-only objects (copies across file systems or with UNDINE_STORE_LINK=copy). A lock entry's
hash says how its files are read: `sha` (format 1) over `git archive` output, as phase 0 computed it; `tree` (format 2)
over git's blobs. `add`, `update` and `lib lock` write the kind of the lock's format; `sync` never converts an entry.

`sync` is frozen: it installs exactly what the lock pins and refuses a lock that no longer pins what project.json names
(after a pull that changed a requirement); `lib lock` (relock) and `lib update` resolve. Each entry is installed from the
store when its record and objects are there (no repository read), else from the kept repository, else fetched; offline
(`offline`, UNDINE_OFFLINE=1) nothing is fetched and what is missing is named. An install made before the store existed
is imported into it by the next sync.

Fetching (git, up to minutes for a slow remote) happens before anything of the project changes, under the project's
dependency lock (.undine/deps.lock: one operation on a project's dependencies at a time, in the server and the CLI),
from project.json as read once that lock is held. Only then are the files swapped in and project.json and undine.lock
written, under `meta_lock` (.undine/project.lock, which Undine's writers of the two files take: the server's requests,
the CLI, adopting or reverting a changeset; not a git command run on the project) and under the caller's `guard` (the
server's own lock). The order is deps.lock, then cas.store_lock(shared) from before the first git command until the
lock is written and the project registered in the store (so `store gc`, which takes it exclusive, never sees a tree
between its fetch and the lock naming it), then gitsrc's lock of one kept repository.

Packages (phase 2, docs/design/packages.md, Resolution and Lockfile). A dependency whose files have an
undine-package.toml is a package: its manifest names its own dependencies (git requirements until the index exists),
and add, update and lib lock resolve the whole graph (store/resolve.py) before anything is written. Every manifest is
read first, from git for what is resolved and from the store or the install for what stays locked (store/locked.py);
then the conflicts, local names, cell names and PDK compatibility are checked and every problem is refused at once
(ResolveError, "nothing was changed"). A format 2 lock keeps direct dependencies in `libraries` (a package among them
gains files, package, version, names and deps) and packages reached only through manifests in `packages`. Each
package is installed under every local name it is placed by, its own [package] name included, each a directory of
hard links into the store with its own install marker. A format 1 lock cannot record that: a package with
dependencies, or whose name is not its key, is refused there with the `lib lock --format 2` hint (NeedsFormat2).
`sync` and `status` work from the lock's graph alone (resolve.lock_graph): sync installs every entry project.json's
dependencies reach, under each of its names, and removes what nothing reaches any more; status has one row per local
name, with the package, who places it, the PDK check and what the trust rule would refuse in it.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import urllib.parse
from collections.abc import Callable
from pathlib import Path

from undine import tools
from undine.store import cas, gitsrc, locked, resolve
from undine.store import manifest as mf

LOCK = "undine.lock"
DEPS = Path(".undine") / "deps"
MARKER = ".undine-dep.json"
TOOLS = tools.TOOLS
_URL = re.compile(r"^(https?://|ssh://|git://|file://|[\w.-]+@[\w.-]+:|/|~/)")      # remote, or an absolute path
# a branch, tag or commit as git names them (v1.2, main, release/2025, HEAD~1, v1^{commit}); never an option
_REV = re.compile(r"^(?!-)[\w./@{}^~+=,-]+$")
_COMMIT = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")                                # SHA-1 or SHA-256, in full
_HEX = re.compile(r"^[0-9a-f]{64}$")


class DepError(Exception):
    pass


class ResolveError(DepError, resolve.ResolveError):
    """A resolution refused, with every problem found (resolve.Problem in `problems`); the message ends with "nothing
    was changed"."""


class NotDirect(DepError):
    """A local name that is installed only because packages place it (`placed_by`): update or remove those."""

    def __init__(self, message: str, placed_by: list[str]):
        super().__init__(message)
        self.placed_by = list(placed_by)


class NotSynced(DepError):
    """A locked package whose files and manifest are neither in the store nor installed (a fresh clone): `lib sync`
    first. Over HTTP 409 not_synced."""


class NeedsFormat2(DepError):
    """What a format 1 lock cannot record: a package's own dependencies, or a package under a name other than its
    key. `lib lock PROJECT --format 2` converts the lock and resolves in one write."""


# ------------------------------------------------------------------ files


def dep_dir(project_path: Path, name: str) -> Path:
    return project_path / DEPS / name


def _deps_root(project_path: Path) -> Path:
    """project_path/.undine/deps, once .undine and .undine/deps are each a real directory of the project, or absent.
    A clone can carry either as a symbolic link (git checks a link out as a link): followed, sync would remove, and an
    install would replace, directories wherever it points. Checked before anything there is listed, locked, staged,
    replaced or removed."""
    root = Path(project_path) / DEPS
    for p in (root.parent, root):
        try:
            st = p.lstat()
        except FileNotFoundError:
            return root
        if not stat.S_ISDIR(st.st_mode):
            raise DepError(f"{p.relative_to(project_path).as_posix()} is a link or not a directory: remove it (Undine "
                           "makes it again) and run lib sync again; nothing was changed")
    if os.path.realpath(root) != os.path.join(os.path.realpath(project_path), ".undine", "deps"):
        raise DepError(f"{DEPS.as_posix()} is not inside the project ({os.path.realpath(root)}): remove it and run "
                       "lib sync again; nothing was changed")
    return root


def tree_sha(d: Path) -> str | None:
    """Content hash of a directory: every file's path and SHA-256 (the install marker excluded)."""
    if not d.is_dir():
        return None
    h = hashlib.sha256()
    for f in sorted(p for p in d.rglob("*") if p.is_file() and p.name != MARKER):
        h.update(f"{f.relative_to(d).as_posix()}\0{hashlib.sha256(f.read_bytes()).hexdigest()}\n".encode())
    return h.hexdigest()


def installed(project_path: Path, name: str) -> dict | None:
    """What sync / add / update installed ({git, rev, subdir, commit, sha}), or None."""
    try:
        return json.loads((dep_dir(project_path, name) / MARKER).read_text())
    except (OSError, ValueError):
        return None


FORMAT = 1                     # the lock format the commands of today write (format 2 only by the ones that need it)
READS = (1, 2)                 # the formats this Undine reads, and keeps when it writes such a lock
NEWEST = max(READS)


def _reads() -> str:
    return "formats " + " and ".join(map(str, READS))


def read_lock(project_path: Path) -> dict | None:
    """The lock, or None when there is none. Refuses (DepError) a lock this Undine cannot read: every caller that
    changes the lock reads it first, so a lock of a newer format is refused before anything of the project is written,
    never rewritten in this format (which would drop what the newer one records)."""
    try:
        text = (project_path / LOCK).read_text()
    except FileNotFoundError:
        return None
    try:
        lock = json.loads(text)
    except ValueError as e:
        raise DepError(f"{LOCK} is not valid JSON ({e}): fix it, or restore it from git "
                       f"(git checkout {LOCK}); nothing was changed") from None
    if not isinstance(lock, dict):
        raise DepError(f"{LOCK} is not a lock (a JSON object): fix it, or restore it from git "
                       f"(git checkout {LOCK}); nothing was changed")
    # Undine has written "format" into every lock since the first; one without it was written by hand, in the only
    # shape there was, so it is read as format 1 (and gets the field on its next write). A newer format always says so.
    fmt = lock.setdefault("format", FORMAT)
    if type(fmt) is int and fmt > NEWEST:
        raise DepError(f"{LOCK} has format {fmt}, made by a newer Undine (this one reads {_reads()}): update "
                       f"Undine (git pull, then ./setup.sh) to work on this project; nothing was changed")
    if type(fmt) is not int or fmt not in READS:       # not True or 1.0 either, which compare equal to 1
        raise DepError(f"{LOCK} has format {fmt!r}, which no Undine writes (this one reads {_reads()}): fix it, "
                       f"or restore it from git (git checkout {LOCK}); nothing was changed")
    return lock


# The lock's fields as either format holds them: callers never look at "pdk" or "tools" directly, so a format 2 object
# is never compared with, or overwritten by, a format 1 string.


def lock_format(lock: dict) -> int:
    return lock.get("format", FORMAT)


def lock_pdk_id(lock: dict) -> str | None:
    """The PDK id the lock pins ("sky130A@8afc8346a57f"): format 1's string, format 2's pdk.id; None for no PDK."""
    pdk = lock.get("pdk")
    return pdk.get("id") if isinstance(pdk, dict) else pdk


def lock_pdk(lock: dict) -> dict | None:
    """The lock's PDK pin as an object: format 2's as written; format 1 records only the id, so {"id": id}. None for no
    PDK."""
    pdk = lock.get("pdk")
    if isinstance(pdk, dict):
        return pdk
    return {"id": pdk} if pdk else None


def _tool_entry(name: str, rec) -> dict | None:
    """One tool as {version, from, text?}: format 1's banner string parsed (no origin recorded), format 2's object."""
    if isinstance(rec, dict):
        out = {"version": rec.get("version"), "from": rec.get("from")}
        return out | ({"text": rec["text"]} if rec.get("text") is not None else {})
    if isinstance(rec, str):
        number = tools.parse_version(name, rec)
        return {"version": number, "from": None} | ({} if number else {"text": rec})
    return None                                           # not installed where it was locked


def lock_tools(lock: dict) -> dict:
    """The lock's tools, whatever its format: {name: {version, from (None in format 1), text (a banner with no number
    read from it)} or None when not installed}, plus 'undine' and 'pixi_lock' only when the lock records them."""
    recorded = lock.get("tools") or {}
    out: dict = {k: recorded[k] for k in ("undine", "pixi_lock") if k in recorded}
    return out | {n: _tool_entry(n, recorded.get(n)) for n in TOOLS}


def entry_hash(entry: dict) -> tuple[str, str | None]:
    """How a dependency's files are pinned: ('tree', 'sha256:...') (format 2, store/cas.py) or ('sha', hex) (the
    format 1 hash over the installed files, tree_sha)."""
    return ("tree", entry["tree"]) if entry.get("tree") else ("sha", entry.get("sha"))


def _content_ok(d: Path, want: dict) -> bool:
    """Whether directory `d` holds exactly the files lock entry `want` pins. A tree is compared with its record in the
    store; without one (the store emptied, or the install made on another machine) the files are hashed as a tree
    (_installed_tree). Reads only: nothing is written to the store."""
    kind, value = entry_hash(want)
    if kind == "sha":
        return value is not None and tree_sha(d) == value
    if not d.is_dir():
        return False
    try:
        files = cas.read_tree(value)
        if files is not None:
            return not any(cas.diff_installed(d, files).values())
        return _installed_tree(d, value) is not None
    except cas.StoreError:                                # not a tree id: nothing installed can match it
        return False


def _marked_modes(d: Path) -> dict:
    """The modes other than 100644 the install marker records ({path: mode}); {} when it records none."""
    try:
        modes = json.loads((d / MARKER).read_text()).get("modes")
    except (OSError, ValueError, AttributeError):
        return {}
    return modes if isinstance(modes, dict) else {}


def _installed_tree(d: Path, tree: str) -> dict[str, tuple[str, str]] | None:
    """The files installed in `d` as the tree `tree` ({path: (mode, hex)}), or None when they are not that tree. The
    modes cannot be read from disk (every installed file is 0o444), so they are taken from the install marker, which
    records the executable ones; failing that (an install made before markers did), every file as 100644, then each
    one starting with #! as 100755. Guessing is safe: only the locked files and modes hash to `tree`."""
    hexes, scripts = {}, set()
    for rel, p, st in cas._walk_files(d):
        if not stat.S_ISREG(st.st_mode):
            return None                                   # a link or a device is never part of an install
        hexes[rel] = cas._hash_file(p)
        with open(p, "rb") as f:
            if f.read(2) == b"#!":
                scripts.add(rel)
    marked = _marked_modes(d)
    for modes in (marked, {}, dict.fromkeys(scripts, "100755")):
        files = {rel: (str(modes.get(rel, "100644")), hex) for rel, hex in hexes.items()}
        try:
            if cas.tree_hash(files) == tree:
                return files
        except cas.StoreError:                            # a marker with a mode no tree has: not this one
            continue
    return None


def write_lock(project_path: Path, lock: dict) -> None:
    (project_path / LOCK).write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")


@contextlib.contextmanager
def _flock(path: Path, create: bool):
    with open(path, "a" if create else "r") as f:
        fcntl.flock(f, fcntl.LOCK_EX)                 # threads too: each open() is a lock of its own
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _lock_file(project_path: Path, name: str):
    d = project_path / ".undine"                      # not versioned, and not copied into a changeset's worktree
    d.mkdir(exist_ok=True)
    return _flock(d / name, create=True)


def meta_lock(project_path: Path):
    """Held while project.json and undine.lock are read, changed and written, by Undine's writers of them (the
    server's requests, the CLI, a changeset's adopt or revert): an flock on .undine/project.lock, a file of its own,
    which a write of project.json by rename (git, write_plan) leaves in place. Short: never held while fetching. Not
    reentrant."""
    return _lock_file(project_path, "project.lock")


def _deps_lock(project_path: Path):
    """One operation on a project's dependencies at a time (they share the staging directories): an flock on
    .undine/deps.lock. Held while fetching; nothing else waits for it. .undine is checked first (_deps_root), so the
    lock file is never made through a link."""
    _deps_root(project_path)
    return _lock_file(project_path, "deps.lock")


def _reread(prj) -> None:
    """prj.meta as project.json is now: an operation that waited for _deps_lock works from what the one before it
    wrote, not from what its caller loaded."""
    with meta_lock(prj.path):
        prj.meta = json.loads((prj.path / "project.json").read_text())


def _short(path: str | None) -> str | None:
    home = str(Path.home())
    return "~" + path[len(home):] if path and (path == home or path.startswith(home + "/")) else path


def environment(pdk, fmt: int = FORMAT) -> dict:
    """What a lock of format `fmt` records of the environment, each tool from the executable tools.find finds (its
    search order: tools.py's docstring). Format 1: the PDK id and each tool's banner. Format 2: the tools only, as
    numbers with their origin (tools.tools_record); its PDK pin is an object only the PDK commands write."""
    if fmt == 1:
        return {"pdk": pdk.id if pdk else None, "tools": {n: tools.version(n) for n in TOOLS}}
    return {"tools": tools.tools_record()}


# ------------------------------------------------------------------ git


def _check_spec(spec: dict, git: bool = True) -> None:
    """Refuses what git would read as something else: each of these reaches git as an argument (rev and subdir too:
    git fetch and git archive take options among their arguments, `--upload-pack=<command>` or `--output=<file>`).
    `git`: also that git is installed (not needed to install from the store)."""
    url, rev, sub = spec.get("git") or "", spec.get("rev"), spec.get("subdir") or ""
    if not _URL.match(url) or url.startswith("-"):
        raise DepError(f"not a git URL or repository path: {url!r}")
    if rev is not None and not (isinstance(rev, str) and _REV.match(rev)):
        raise DepError(f"not a branch, tag or commit: {rev!r} (rev names one, e.g. v1.2.0, main or a commit; it "
                       "does not start with -)")
    parts = Path(sub).parts
    if sub and (Path(sub).is_absolute() or ".." in parts or any(x.startswith("-") for x in parts)):
        raise DepError(f"subdir must be a relative path inside the repository (no part starting with -): {sub!r}")
    if git and not shutil.which("git"):
        raise DepError("git is not installed (needed for library dependencies)")


def _check_commit(name: str, commit) -> None:
    """A lock's commit reaches git fetch as an argument: a full hex commit id, nothing else (undine.lock comes from a
    git pull or a clone, like project.json)."""
    if not (isinstance(commit, str) and _COMMIT.match(commit)):
        raise DepError(f"{name}: {LOCK} locks it at {commit!r}, which is not a full commit id: fix that entry, or "
                       f"restore {LOCK} from git (git checkout {LOCK}); nothing was changed")


def _local_path(url: str) -> Path | None:
    """The directory a local URL (file://, /..., ~/...) names, symlinks and .. resolved; None for a remote one."""
    if url.startswith("file://"):
        u = urllib.parse.urlsplit(url)
        if u.netloc not in ("", "localhost") or u.query or u.fragment or not u.path.startswith("/"):
            raise DepError(f"not a plain file:// URL (file:///path/to/repo): {url!r}")
        return Path(urllib.parse.unquote(u.path)).resolve()      # git decodes %xx in a file:// URL too
    if url.startswith(("/", "~/")):
        return Path(url).expanduser().resolve()
    return None


def _source(spec: dict, local_roots: list[Path] | None, command: str) -> str | None:
    """What git fetches `spec` from, checked before any fetch. `local_roots` None (the CLI): the URL as written. A list
    (HTTP: serve --dep-root): a local repository only when it is an existing directory whose real path and git
    directory (and that one's common directory) are under one of them, and then that git directory, so that git reads
    the very repository checked (given a path, git would also try <path>.git); a remote URL as written. Applies to a URL from the request and to
    one project.json names alike: a project.json from a clone or an adopted changeset can name any path on the disk,
    and what is fetched becomes readable through the browser. `command`: the CLI command that does it anyway."""
    if local_roots is None:
        return None
    _check_spec(spec)
    src = _local_path(spec["git"])
    if src is None:
        return None
    _under_roots(spec, src, local_roots, command)
    if not src.is_dir():
        # git would try <path>.git and <path>.git/.git next, which may be a symlink to anywhere
        raise DepError(f"{spec['git']}: {src} is not a directory: name the repository's own directory (the one that "
                       "holds .git, or a bare repository)")
    gitdir, common = _git_dirs(spec, src)
    for d in (gitdir, common):             # a .git file (gitdir: ...) or a worktree's commondir may point elsewhere
        _under_roots(spec, d, local_roots, command)
    return str(gitdir)


def _under_roots(spec: dict, path: Path, local_roots: list[Path], command: str) -> None:
    if not any(path.is_relative_to(r) for r in local_roots):
        why = (f"{path} is outside the directories this server fetches local repositories from "
               f"({', '.join(map(str, local_roots))}; --dep-root)") if local_roots else \
            "this server takes no local repository from the browser (it was started without --dep-root)"
        raise DepError(f"{spec['git']}: {why}: start the server with `--dep-root DIR` naming a directory above it, "
                       f"or run `python -m undine {command}` from the command line")


def _git_dirs(spec: dict, src: Path) -> tuple[Path, Path]:
    """The repository git reads for directory `src` (its .git, followed through a gitdir file, or `src` itself when
    bare) and that repository's common directory (objects, refs), real paths. Not looked for above `src`."""
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(src.parent)}
    for k in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE"):
        env.pop(k, None)
    r = subprocess.run(["git", "-C", str(src), "rev-parse", "--absolute-git-dir", "--git-common-dir"], env=env,
                       capture_output=True, text=True, timeout=60)
    lines = r.stdout.splitlines()
    if r.returncode != 0 or len(lines) != 2:
        raise DepError(f"{spec['git']}: {src} is not a git repository: name the repository's own directory (the one "
                       "that holds .git, or a bare repository)")
    return Path(lines[0]).resolve(), (src / lines[1]).resolve()        # the common dir may be relative to src


# ------------------------------------------------------------------ the store


def _check_hash(name: str, want: dict) -> tuple[str, str]:
    """The lock entry's (kind, hash), refused when it is not one: it names a record in the store."""
    kind, value = entry_hash(want)
    prefix = "sha256:" if kind == "tree" else ""
    if not (isinstance(value, str) and value.startswith(prefix) and _HEX.match(value[len(prefix):])):
        raise DepError(f"{name}: {LOCK} has no valid {kind} for it ({value!r}): fix that entry, or restore {LOCK} from "
                       f"git (git checkout {LOCK}); nothing was changed")
    return kind, value


def _stored(kind: str, value) -> dict | None:
    """The files of a tree recorded in the store whose objects are all there (presence: cas.install re-hashes each),
    or None. Reads no repository."""
    try:
        files = cas.read_tree(value) if kind == "tree" else cas.read_archive_tree(value)
    except cas.StoreError:
        return None
    return files if files is not None and not cas.missing_objects(files) else None


def _named(name: str, want: dict) -> str:
    kind, value = entry_hash(want)
    return f"{name} @ {str(want.get('commit'))[:12]} ({kind} {str(value)[:19 if kind == 'tree' else 12]})"


def _not_here(names: list[str]) -> str:
    return (f"offline, and not in the store: {', '.join(names)}: run the sync with the network (without --offline or "
            f"UNDINE_OFFLINE=1) to fetch {'it' if len(names) == 1 else 'them'}; nothing was changed")


def _read_commit(name: str, spec: dict, commit: str | None, source: str | None, kind: str,
                 offline: bool, files: str = "all") -> tuple[str, dict, str]:
    """Reads the files of `commit` (else of spec's rev) into the store from the kept repository, fetching it first when
    the repository does not have it, and records the tree; returns (commit, files, hash). `files`: 'include' reads
    only what the manifest's [files] include selects (a lock entry with files: "include"), 'all' every file of the
    subdir; a format 1 `sha` always covers every file. The caller ran the checks (_check_spec, _check_commit,
    _own_dir, and _source over HTTP) and holds cas.store_lock(shared). A bad manifest raises mf.ManifestError."""
    url, sub = spec["git"], spec.get("subdir")
    try:
        if commit is None or not gitsrc.has_commit(url, commit):
            if offline:
                raise DepError(_not_here([f"{name} @ {str(commit)[:12]}"]))
            commit = gitsrc.fetch(url, source, commit or spec.get("rev") or "HEAD", commit)
        if kind == "tree":
            got = gitsrc.blob_tree(url, commit, sub, files)
            return commit, got, cas.write_tree(got)
        got = gitsrc.archive_tree(url, commit, sub)
        return commit, got, cas.write_archive_tree(got)
    except (gitsrc.GitSourceError, cas.StoreError) as e:
        raise DepError(str(e)) from None


def _same(name: str, commit: str, got: str, want: str | None) -> None:
    if want is not None and got != want:
        raise DepError(f"{name}: the files at {commit[:12]} do not match the lock (content hash differs)")


def _stage(files: dict, staging: Path) -> None:
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    cas.install(files, staging)


def _mode(entry: dict) -> str:
    """Which files a lock entry's tree covers: 'include' when it says so (phase 2), else every file of the subdir."""
    return "include" if entry.get("files") == "include" else "all"


def _spec_of(entry: dict) -> dict:
    return {k: entry[k] for k in ("git", "rev", "subdir") if entry.get(k)}


def _resolve(project_path: Path, name: str, spec: dict, commit: str | None = None,
             source: Callable[[], str | None] = lambda: None, kind: str = "sha", want: str | None = None,
             files: dict | None = None, offline: bool = False, mode: str = "all",
             parse: bool = True) -> tuple[dict, dict, mf.Manifest | None]:
    """The lock entry, files and manifest of `spec` at `commit` (else at spec's rev), hashed as `kind` over `mode`'s
    files: from `files` (a tree found in the store: no repository is read), else read through the kept repository.
    `want`: the hash the lock pins, checked before anything is staged (_staged). `source()`: what git fetches from
    (_source), asked only when git is about to be read, so that over HTTP the --dep-root check comes before any read
    of a kept repository. `parse` False: the manifest is not read (None). The caller holds cas.store_lock(shared)."""
    _own_dir(project_path, name)
    if files is None:
        try:
            commit, files, value = _read_commit(name, spec, commit, source(), kind, offline, mode)
        except mf.ManifestError as e:
            raise DepError(str(e)) from None
        _same(name, commit, value, want)
    else:
        value = want
    entry = {k: spec[k] for k in ("git", "rev", "subdir") if spec.get(k)} | {"commit": commit, kind: value}
    if mode == "include":
        entry["files"] = "include"
    m = None
    if parse:
        try:
            m = mf.from_files(files, name)
        except mf.ManifestError as e:
            raise DepError(str(e)) from None
    return entry, files, m


def _staged(project_path: Path, name: str, entry: dict, files: dict, source: Callable[[], str | None],
            offline: bool = False) -> Path:
    """Makes the staging directory .undine/deps/.<name>.new by hard links into the store from `files`, the files of
    lock entry `entry`. An object that fails its check in the store is removed and the entry read again from git,
    under the same checks (_check_spec, _check_commit, `source()`), and must hash to the entry's hash. The caller
    holds cas.store_lock(shared)."""
    _own_dir(project_path, name)
    staging = project_path / DEPS / f".{name}.new"
    kind, value = entry_hash(entry)
    try:
        try:
            _stage(files, staging)
        except cas.StoreError as e:
            if not e.paths:                                   # not a bad object: nothing git can mend
                raise DepError(str(e)) from None
            spec = _spec_of(entry)
            _check_spec(spec)
            _check_commit(name, entry.get("commit"))
            try:
                commit, files, again = _read_commit(name, spec, entry["commit"], source(), kind, offline,
                                                    _mode(entry))
            except mf.ManifestError as e2:
                raise DepError(str(e2)) from None
            _same(name, commit, again, value)
            try:
                _stage(files, staging)
            except cas.StoreError as e2:
                raise DepError(str(e2)) from None
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def _install(staging: Path, dest: Path, info: dict) -> None:
    """Swaps the staged files in, with the install marker: a file of its own, never a link into the store. For a
    `tree` the marker also records the files of mode 100755 (`modes`), which the installed 0o444 files cannot show:
    status and seeding check the install against its tree with them when the store has no record of it."""
    _deps_root(dest.parent.parent.parent)                # dest is <project>/.undine/deps/<name>
    marker = staging / MARKER
    marker.unlink(missing_ok=True)
    files = cas.read_tree(info["tree"]) if info.get("tree") else None
    modes = {p: m for p, (m, _) in (files or {}).items() if m != "100644"}
    marker.write_text(json.dumps(info | ({"modes": modes} if modes else {}), indent=2) + "\n")
    old = dest.with_name(dest.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if dest.exists():
        os.replace(dest, old)
    os.replace(staging, dest)
    shutil.rmtree(old, ignore_errors=True)


def _remark(d: Path, info: dict) -> None:
    """Rewrites the install marker of `d`, whose files stay as they are (a package that gained or lost a requirer,
    or became direct): the marker is a file of its own, replaced by rename. The modes it records are kept."""
    _deps_root(d.parent.parent.parent)
    modes = _marked_modes(d)
    tmp = d / f".{MARKER}.new"
    tmp.write_text(json.dumps(info | ({"modes": modes} if modes else {}), indent=2) + "\n")
    os.replace(tmp, d / MARKER)


def _seed(d: Path, want: dict) -> None:
    """Imports an install that matches its lock entry but is not in the store (made before the store existed, or the
    store emptied): a later sync, offline too, installs it from there. Best effort: the install stays as it is."""
    kind, value = entry_hash(want)
    if _stored(kind, value) is not None:
        return
    with contextlib.suppress(cas.StoreError, OSError):
        if kind == "tree":
            files = _installed_tree(d, value)             # with the modes the hash needs, or None: not that tree
            if files is not None:
                cas.put_dir(d)                            # the objects; the modes come from files
                cas.write_tree(files)
            return
        files = cas.put_dir(d)
        if cas.archive_sha(files) == value:
            cas.write_archive_tree(files)


@contextlib.contextmanager
def _in_store(prj):
    """cas.store_lock(shared) around an operation's fetch, record, install and lock write; the project is registered
    in the store's list (the projects whose locks keep trees alive) once that went through."""
    with cas.store_lock(shared=True):
        yield
        cas.register_project(prj.path)


def _hashes(entries) -> tuple[set[str], set[str]]:
    trees: set[str] = set()
    shas: set[str] = set()
    for e in entries:
        if isinstance(e, dict):
            kind, value = entry_hash(e)
            if isinstance(value, str):
                (trees if kind == "tree" else shas).add(value)
    return trees, shas


def locked_trees(lock: dict | None) -> tuple[set[str], set[str]]:
    """The tree ids and archive shas a lock pins, in `libraries` and `packages`: what `store gc` keeps for it, so a
    package that is locked but not installed keeps its tree."""
    lock = lock or {}
    tables = [t for t in (lock.get("libraries"), lock.get("packages")) if isinstance(t, dict)]
    return _hashes(e for t in tables for e in t.values())


def installed_trees(project_dir: Path) -> tuple[set[str], set[str]]:
    """The tree ids and archive shas installed in a project (the markers of .undine/deps/*, staging and old
    directories included): what `store gc` keeps for it, so a store copy of what is installed stays."""
    try:
        root = _deps_root(Path(project_dir))
    except DepError:                                     # a link: nothing installed there is this project's
        return set(), set()
    markers = []
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        try:
            markers.append(json.loads((d / MARKER).read_text()))
        except (OSError, ValueError):
            continue
    return _hashes(markers)


# ------------------------------------------------------------------ operations


def new_lock(env: dict) -> dict:
    """A lock with no dependencies yet, recording `env` (environment()): what `lib lock`, `set-pdk` and the first
    `lib add` write into a project without one."""
    return {"format": FORMAT, **env, "libraries": {}}


def _lock_of(prj, env: dict | None = None) -> dict:
    """The lock, or a new one recording `env` (default: the environment now)."""
    return read_lock(prj.path) or new_lock(env or environment(prj.pdk))


def _declared(prj) -> list[str]:
    return [n for n, s in prj.library_specs().items() if "git" in s]


def leftovers(prj, lock: dict | None) -> dict[str, dict]:
    """Dependencies installed in .undine/deps/ or locked in `lock` that nothing of project.json places any more
    (removed by a git pull, a changeset adopted, a hand edit): {name: {git, rev, subdir, commit, locked, installed,
    on_disk, in_lock, package}}, `commit` the lock's or else the installed one, `locked` and `installed` each its own.
    What is expected is project.json's git names and every name of the packages their lock entries reach
    (resolve.reachable); every name of a lock entry nothing reaches, and every other directory, is a leftover. Their
    files stay on the symbol path until `sync` removes them: .undine/deps/ is one entry of it (Project.symbol_paths),
    not one per name. A lock key or name that is not a library name (bad_lock_keys) is none of them: sync must not
    take it for a path."""
    declared = _declared(prj)
    root = _deps_root(prj.path)
    dirs = {d.name for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")} if root.is_dir() else set()
    g = resolve.lock_graph(lock)
    ids, expected = resolve.reachable(lock, declared)
    expected |= set(declared)
    locked: dict[str, dict] = {}
    for n, e in ((lock or {}).get("libraries") or {}).items():      # not an object: still a lock key sync drops
        if _plain_name(n) and not isinstance(e, dict):
            locked[n] = {}
    for id, e in g.entries.items():
        if id not in ids and _sane(g, id):
            for n in g.names_of(id):
                locked.setdefault(n, e)
    out = {}
    for name in sorted((dirs | set(locked)) - expected):     # dot names are staging, never looked up
        want = locked.get(name) or {}
        have = (installed(prj.path, name) if name in dirs else None) or {}
        known = want or have
        package = want.get("package") or have.get("package")
        out[name] = {**{k: known.get(k) for k in ("git", "rev", "subdir", "commit")},
                     "locked": want.get("commit"), "installed": have.get("commit"),
                     "on_disk": name in dirs, "in_lock": name in locked,
                     "package": package if isinstance(package, str) else None}
    return out


def _unreached(lock: dict | None, declared) -> set[tuple[str, str]]:
    """The (table, key) of the lock entries that project.json's git names `declared` no longer reach: sync drops
    them with their names. A key that is not a library name is left alone (bad_lock_keys)."""
    g = resolve.lock_graph(lock)
    ids, _ = resolve.reachable(lock, declared)
    return {g.keys[id] for id in g.entries if id not in ids and _sane(g, id)}


def _sane(g: resolve.Graph, id: str) -> bool:
    """Whether a lock entry's key and every local name it gives are library names: one that is not was written by
    hand or by something else, and sync leaves it, and the directories it names, alone (status reports it)."""
    return _plain_name(g.keys[id][1]) and all(_plain_name(n) for n in g.names_of(id))


def _plain_name(name) -> bool:
    """A library name (project.check_name's): one directory under .undine/deps/, never a path out of it."""
    from undine.store.project import NAME_RE
    return isinstance(name, str) and bool(NAME_RE.match(name))


def bad_lock_keys(lock: dict | None, table: str = "libraries") -> list[str]:
    """Keys of the lock's `table` that are not library names (an absolute path, ../x), and for `packages` also those
    whose entry gives a local name that is not one: written by hand or by something else, never by Undine. Reported
    by status, left alone by sync."""
    entries = (lock or {}).get(table)
    if not isinstance(entries, dict):
        return []
    out = []
    for key, e in entries.items():
        names = e.get("names") if isinstance(e, dict) and isinstance(e.get("names"), list) else []
        if not _plain_name(key) or (table == "packages" and not all(_plain_name(n) for n in names)):
            out.append(str(key))
    return sorted(out)


def _own_dir(project_path: Path, name: str) -> Path:
    """dep_dir(), for a name that keeps it a child of .undine/deps/ (no /, no ..; an absolute name would replace the
    whole path): checked before anything is fetched into, replaced or removed there."""
    if not _plain_name(name):
        raise DepError(f"{name!r} is not a library name (letters, digits and _ . + -, not starting with . or -): "
                       "rename it in project.json; nothing was changed")
    return _deps_root(project_path) / name


@contextlib.contextmanager
def _committing(prj, guard):
    """Where an operation changes the project: under `guard` (the server's lock, or nothing) and meta_lock, with
    prj.meta read again from disk, as another writer (a request, the CLI) may have changed it while fetching."""
    with guard if guard is not None else contextlib.nullcontext(), meta_lock(prj.path):
        prj.meta = json.loads((prj.path / "project.json").read_text())
        yield


def _lock_now(prj, env: dict | None = None) -> tuple[dict, bool]:
    """_lock_of, and whether it is new (the call writing it creates the lock)."""
    was = read_lock(prj.path)
    return (was, False) if was is not None else (new_lock(env or environment(prj.pdk)), True)


def _fits(lock: dict, entry: dict) -> None:
    """A `tree` entry goes into a format 2 lock only: a lock replaced meanwhile by one of format 1 (a git checkout)
    would otherwise get an entry an older Undine cannot check."""
    if "tree" in entry and lock_format(lock) < 2:
        raise DepError(f"{LOCK} was replaced meanwhile by a format 1 lock: nothing was changed, try again")


# ------------------------------------------------------------------ resolution (packages, phase 2)


_PDK_CELLS: dict[tuple[str, str], dict[str, set[str]]] = {}      # (PDK id, root) -> {library: cell names}
_MACRO = re.compile(r"(?m)^\s*MACRO\s+(\S+)")


def _reserved(prj, lock: dict | None) -> dict[str, str]:
    """{casefolded name: what it is} that no local name of a dependency may be (Resolution 5): the project's own
    libraries, the PDK's libraries (as installed here, else as the lock's pin lists them), Xschem's `devices`, and
    every folder of Xschem's library and of project.json's symbol_paths, which .undine/deps would shadow on the one
    symbol path. A name project.json already gives a git dependency is left out: it worked before, and status notes
    it."""
    direct = {n.casefold() for n in _declared(prj)}
    return {k: v for k, v in _shadowed(prj, lock).items() if k not in direct}


def _shadowed(prj, lock: dict | None) -> dict[str, str]:
    """_reserved's names, project.json's git dependencies included."""
    specs = prj.library_specs()
    out: dict[str, str] = {}
    for n, s in specs.items():
        if "git" not in s:
            out.setdefault(n.casefold(), f"the project library {n}")
    pdk_libs = [n for n, lib in prj.libraries().items() if lib.origin == "pdk"]
    if not pdk_libs:
        pdk_libs = [n for n in (lock_pdk(lock or {}) or {}).get("libraries") or [] if isinstance(n, str)]
    for n in pdk_libs:
        out.setdefault(n.casefold(), f"the PDK library {n}")
    out.setdefault("devices", "Xschem's devices library")
    folders = [(tools.xschem_library(), "Xschem's library")]
    folders += [(Path(p).expanduser(), f"symbol path {p}") for p in prj.meta.get("symbol_paths", [])
                if isinstance(p, str)]
    for base, what in folders:
        try:
            subs = sorted(d.name for d in base.iterdir() if d.is_dir()) if base is not None and base.is_dir() else []
        except OSError:
            subs = []
        for n in subs:
            out.setdefault(n.casefold(), f"a folder of {what} ({base / n})")
    return out


def _project_cells(prj) -> dict[str, set[str]]:
    """The logic cells of the project's own libraries (top-level .sch, .sym, .sv, .v stems). Layout copies of placed
    cells are not the project's cells: counting them would refuse every project that placed a package's cell."""
    out = {}
    for n, s in prj.library_specs().items():
        if "git" in s:
            continue
        d = prj.lib_path(s["path"])
        names = [f.name for f in d.iterdir() if f.is_file()] if d.is_dir() else []
        out[n] = set(resolve.cell_names(names)[0].values())
    return out


def _pdk_cells(prj) -> dict[str, set[str]]:
    """The cells of the PDK's libraries: each symbol library's .sym stems, each layout-only library's LEF MACROs, or
    its GDS cells when it has no LEF. Read once per PDK (a standard cell library holds hundreds); status never asks."""
    pdk = prj.pdk
    if pdk is None:
        return {}
    key = (pdk.id, str(pdk.root))
    if key not in _PDK_CELLS:
        out: dict[str, set[str]] = {}
        for n, lib in prj.libraries().items():
            if lib.origin != "pdk":
                continue
            if lib.layout_file is None:
                out[n] = {p.stem for p in lib.path.glob("*.sym")}
                continue
            lefs = sorted((pdk.root / "libs.ref" / n / "lef").glob("*.lef"))
            if lefs:
                out[n] = {m for f in lefs for m in _MACRO.findall(f.read_text(errors="replace"))}
                continue
            try:
                out[n] = set(lib.layout_cells()[0])
            except Exception:                    # a GDS KLayout cannot read: its cells are not known, not a clash
                out[n] = set()
        _PDK_CELLS[key] = out
    return _PDK_CELLS[key]


def _requirement(local: str, place: dict, package: str | None = None,
                 commit: str | None = None) -> resolve.Requirement:
    return resolve.Requirement(local, package, place.get("git"), place.get("rev"), place.get("subdir"),
                               commit=commit)


def _not_taken(prj, name: str) -> None:
    """add's check before the dependency lock: a project library, a PDK library or a dependency of project.json has
    the name. A name installed only because a package places it is not taken: adding it makes it direct, when it is
    the same package (resolution checks that)."""
    specs = prj.library_specs()
    libs = prj.libraries()
    lib = libs.get(name)
    if name in specs or (lib is not None and not (lib.origin == "dep" and name not in specs)):
        raise DepError(f"library {name} exists")
    # one directory of a case-insensitive file system serves both spellings: removing one would take the other
    other = next((n for n in [*specs, *libs] if n != name and n.casefold() == name.casefold()), None)
    if other is not None:
        raise DepError(f"library {name} exists (as {other}): spell it {other}, or choose another name; nothing was "
                       "changed")


def _unchanged(start: dict | None, now: dict) -> None:
    """The lock as the operation read it before fetching, and as it is now under meta_lock: another writer (a git
    command, a CLI outside the dependency lock) that changed its dependencies or its format meanwhile is not
    overwritten."""
    was = start or {}
    if start is not None and lock_format(now) != lock_format(was):
        raise DepError(f"{LOCK} was replaced meanwhile by a format {lock_format(now)} lock: nothing was changed, try "
                       "again")
    if any((now.get(t) or {}) != (was.get(t) or {}) for t in ("libraries", "packages")):
        raise DepError(f"{LOCK} changed meanwhile: nothing was changed, try again")


def _put_tables(lock: dict, libs: dict, pkgs: dict) -> None:
    """Writes resolved entries into `lock`: direct ones into `libraries` (others there stay as they are), `packages`
    as resolved (a lock that never had one gets none)."""
    lock.setdefault("libraries", {}).update(libs)
    if pkgs:
        lock["packages"] = pkgs
    else:
        lock.pop("packages", None)


def _apply(prj, installs: dict, staged: dict[str, Path], gone: list[str]) -> None:
    """Swaps in each staged install with its marker, rewrites the markers that changed of the installs that stay,
    and removes the names no longer installed. Under _committing."""
    for n, (node, entry, marker) in installs.items():
        d = dep_dir(prj.path, n)
        if n in staged:
            _install(staged[n], d, marker)
            continue
        have = installed(prj.path, n)
        if have is not None and d.is_dir() and {k: v for k, v in have.items() if k != "modes"} != marker:
            _remark(d, marker)
    for n in gone:
        shutil.rmtree(_own_dir(prj.path, n), ignore_errors=True)


def _flagged(prj, item: dict, d: Path) -> dict:
    """item with what the trust rule would refuse in the install `d` (trust.scan_dir counted: spice, rtl, tcl) and
    whether the project trusts it."""
    from undine.store import trust
    if not d.is_dir():
        return item
    trusted = trust.key_of(d) in trust.trusted_keys(prj.path)
    return item | {"flagged": trust.counts(trust.scan_dir(d)), "trusted": trusted}


class _Graph:
    """One resolving operation (add, update, lib lock) of a project: the reader that resolve.resolve walks with (each
    requirement's commit and manifest, read from git), the locked packages held fixed (files and manifests from the
    store or the install, store/locked.py, never fetched), the trees read once every manifest is in, and the checks
    resolve runs with them: PDK compatibility and cell names. Then what to write: the lock's tables and one install
    per (package, local name)."""

    def __init__(self, prj, lock: dict | None, local_roots: list[Path] | None, command: str):
        from undine.pdks import compat
        self.prj, self.lock = prj, lock
        self.fmt = lock_format(lock) if lock else FORMAT
        self.kind = "tree" if self.fmt >= 2 else "sha"
        self.local_roots, self.command = local_roots, command
        self.graph = resolve.lock_graph(lock)
        self.sources: dict[str, str | None] = {}
        self.commits: dict[tuple, str] = {}
        self.trees: dict[tuple, tuple[dict, str]] = {}
        self.hashes: dict = {}                            # node read from git -> its tree id or archive sha
        self.origin: dict = {}                            # fixed node -> its lock entry
        pin = lock_pdk(lock) if lock else ({"id": prj.pdk.id} if prj.pdk else None)
        self.target = compat.project_target(pin, prj.pdk)

    # -- reading

    def source(self, spec: dict) -> str | None:
        """What git fetches spec's URL from (_source: over HTTP the --dep-root check), asked once per URL, only when
        git is about to read its repository."""
        url = spec["git"]
        if url not in self.sources:
            self.sources[url] = _source(spec, self.local_roots, self.command)
        return self.sources[url]

    def commit_of(self, local: str, spec: dict, commit: str | None) -> str:
        """The commit spec's rev names, or `commit` itself, in the kept repository (fetched when it is not there):
        one fetch per URL and rev in an operation, however many packages that repository holds."""
        want = commit or spec.get("rev") or "HEAD"
        key = (spec["git"], want)
        if key not in self.commits:
            src = self.source(spec)
            try:
                self.commits[key] = gitsrc.fetch(spec["git"], src, want, commit)
            except gitsrc.GitSourceError as e:
                raise DepError(f"{local}: {e}") from None
        return self.commits[key]

    def why_2(self, local: str, m: mf.Manifest) -> str:
        if m.dependencies:
            return f"{local}: package {m.name} names dependencies ({', '.join(m.dependencies)})"
        return f"{local}: package {m.name} is also installed under its own name {m.name}"

    def needs_2(self, why: str) -> NeedsFormat2:
        return NeedsFormat2(f"{why}: needs lock format 2: run python -m undine lib lock {self.prj.name} --format 2 "
                            "first; nothing was changed")

    def read(self, req: resolve.Requirement, parent) -> resolve.Node:
        """resolve's reader: the requirement's commit and manifest, from git; its tree comes once every manifest is
        in (cells). The spec as git reads it, the commit and, over HTTP, --dep-root are checked before any
        repository is read. A manifest that is a link or too large is refused by gitsrc; a bad one is ManifestError
        (resolve collects it)."""
        spec = _spec_of({"git": req.git, "rev": req.rev, "subdir": req.subdir})
        _check_spec(spec)
        if req.commit is not None:
            _check_commit(req.local, req.commit)
        commit = self.commit_of(req.local, spec, req.commit)
        try:
            data = gitsrc.read_file(req.git, commit, req.subdir, mf.MANIFEST, mf.MAX_BYTES)
        except gitsrc.GitSourceError as e:
            raise DepError(f"{req.local}: {e}") from None
        where = f"{req.git} @ {commit[:12]}" + (f":{req.subdir}" if req.subdir else "")
        m = None if data is None else mf.parse(data, where)
        if m is not None and self.fmt < 2 and (m.dependencies or m.name != req.local):
            raise self.needs_2(self.why_2(req.local, m))
        return resolve.Node("?", git=req.git, rev=req.rev, subdir=req.subdir, commit=commit,
                            files_mode="include" if m is not None and self.fmt >= 2 else "all", manifest=m)

    def fixed(self, keys, skip: set[str] = frozenset()) -> dict[str, resolve.Node]:
        """The locked entries reached from the direct names `keys` (but the ids in `skip`, which are resolved
        again), as fixed nodes. A package that is in neither the store nor a verified install (a fresh clone) is
        refused with lib sync's hint; an entry with no package recorded is then taken as a plain one, as before
        phase 2 (nothing of it is checked)."""
        g = self.graph
        ids, _ = resolve.reachable(self.lock, keys)
        out: dict[str, resolve.Node] = {}
        problems: list[resolve.Problem] = []
        for id in sorted(ids - set(skip)):
            table, key = g.keys[id]
            e = g.entries[id]
            local = g.names_of(id)[0]
            try:
                files = locked.files_of(self.prj.path, local, e)
                m = locked.manifest_of(self.prj.path, local, e)
            except locked.NotAvailable as x:
                if table == "packages" or e.get("package"):
                    raise NotSynced(f"{x}; nothing was changed") from None
                files, m = {}, None
            except mf.ManifestError as x:
                problems.append(resolve.Problem("manifest", str(x)))
                continue
            # an entry locked before phase 2 (format 1, or no package recorded) is held as that plain entry: its
            # manifest's dependencies, own name and [pdk] wait for lib lock or lib update of it, so a command about
            # another dependency neither fetches nor refuses for them, and does not rewrite it
            held = m is not None and table == "libraries" and not e.get("package")
            chains = g.chains(id)
            node = resolve.Node(m.name if m else f"git:{key}", m.name if m else None, m.version if m else None,
                                e.get("git"), e.get("rev"), e.get("subdir"), e.get("commit"), e.get("tree"),
                                _mode(e), files, m, True, g.chain_text(self.prj.name, chains[0]) if chains else None,
                                held=held)
            k = node.id
            while k in out:                    # one package under two direct names: two entries, either matches
                k += "+"
            out[k] = node
            self.origin[node] = e
        if problems:
            raise ResolveError(problems)
        return out

    # -- checks

    def pdk_check(self, node: resolve.Node) -> str | None:
        from undine.pdks import compat
        return compat.check(node.manifest.pdk, self.target)

    def closure(self, res: resolve.Resolution, id: str) -> set[str]:
        out: set[str] = set()
        todo = list(res.deps.get(id, {}).values())
        while todo:
            i = todo.pop()
            if i not in out and i != id:
                out.add(i)
                todo += res.deps.get(i, {}).values()
        return out

    def cells(self, res: resolve.Resolution) -> list[resolve.Problem]:
        """resolve's last check, once every manifest is in: the tree of each node read from git goes into the store
        (its [files] include and [exports] checked), then the cells of every package, of the project's libraries and
        of the PDK must have different names (Resolution 4). Only plain dependencies: nothing to refuse."""
        problems = []
        for node in res.nodes.values():
            if node.fixed or node in self.hashes:
                continue
            spec = _spec_of({"git": node.git, "rev": node.rev, "subdir": node.subdir})
            key = (node.git, node.commit, node.subdir, node.files_mode)
            try:
                if key not in self.trees:
                    _, files, value = _read_commit(node.id, spec, node.commit, self.source(spec), self.kind, False,
                                                   node.files_mode)
                    if node.manifest is not None and self.kind == "sha":
                        mf.check_files(node.manifest, files, f"{node.git} @ {node.commit[:12]}"
                                       + (f":{node.subdir}" if node.subdir else ""))
                    self.trees[key] = (files, value)
            except mf.ManifestError as e:
                problems.append(resolve.Problem("manifest", str(e)))
                continue
            node.files, self.hashes[node] = self.trees[key]
            if self.kind == "tree":
                node.tree = self.hashes[node]
        if not any(n.manifest is not None for n in res.nodes.values()):
            return problems
        labels = {id: resolve.name_text(n) for id, n in res.nodes.items()}
        packages = {}
        for id, node in res.nodes.items():
            logic, layout = resolve.cell_names(node.files)
            packages[labels[id]] = (logic, layout, {labels[i] for i in self.closure(res, id)},
                                    node.manifest is not None and not node.held)
        return problems + resolve.check_disjoint(packages, _project_cells(self.prj), _pdk_cells(self.prj))

    def run(self, direct: dict[str, resolve.Requirement], fixed: dict[str, resolve.Node]) -> resolve.Resolution:
        """Resolves `direct` with `fixed` held; ResolveError with every problem. On a format 1 lock, what it cannot
        record is NeedsFormat2."""
        self.direct = direct
        try:
            res = resolve.resolve(self.prj.name, direct, self.read, fixed=fixed,
                                  reserved=_reserved(self.prj, self.lock), pdk_check=self.pdk_check, cells=self.cells)
        except resolve.ResolveError as e:
            raise ResolveError(e.problems) from None
        if self.fmt < 2:
            for id, node in res.nodes.items():
                keys = [k for k, i in res.direct.items() if i == id]
                if node.held and not (set(res.names_of(id)) - set(keys) or res.requirers(id)):
                    continue                             # as phase 1 locked it, and nothing new names it
                if node.manifest is not None and (res.deps.get(id) or not keys or set(res.names_of(id)) - set(keys)):
                    raise self.needs_2(self.why_2(keys[0] if keys else res.names_of(id)[0], node.manifest))
        return res

    # -- what to write

    def walked(self, exclude=()) -> list[str]:
        """project.json's git dependencies the lock has an entry for (but `exclude`): held at their locked place."""
        return [k for k in _git_specs(self.prj) if k not in exclude and k in self.graph.direct]

    def held(self, keys) -> dict[str, resolve.Requirement]:
        """Requirements for locked direct names, at the place their lock entry has (a stale one stays as locked:
        only lib lock and lib update resolve it), so each takes its fixed node and nothing is fetched."""
        return {k: _requirement(k, self.graph.entries[self.graph.direct[k]]) for k in keys}

    def entry_of(self, node: resolve.Node) -> dict:
        if node.fixed:
            return dict(self.origin[node])
        e = _spec_of({"git": node.git, "rev": node.rev, "subdir": node.subdir})
        e |= {"commit": node.commit, self.kind: self.hashes[node]}
        if node.files_mode == "include":
            e["files"] = "include"
        return e

    def tables(self, res: resolve.Resolution) -> tuple[dict, dict, dict]:
        """(libraries entries by key, packages entries by package, installs {local name: (node, entry, marker)}).
        A package (format 2) gets files, package, version, names and deps; a locked entry from before phase 2 keeps
        its tree and gets those only when it now has names, dependencies or requirers to record. A plain entry is
        exactly phase 1's, and its marker the entry."""
        libs: dict[str, dict] = {}
        pkgs: dict[str, dict] = {}
        installs: dict[str, tuple] = {}
        for id, node in res.nodes.items():
            keys = [k for k, i in res.direct.items() if i == id]
            names = res.names_of(id)
            m = node.manifest
            entry = self.entry_of(node)
            if node.held:                                # as locked before phase 2, unless a package now names it
                meta = m is not None and self.fmt >= 2 and bool(set(names) - set(keys) or res.requirers(id))
            else:
                meta = m is not None and self.fmt >= 2 and (
                    not node.fixed or "package" in entry or names != keys[:1] or bool(res.deps.get(id))
                    or bool(res.requirers(id)))
            if meta and len(keys) > 1:
                # one package entry per package name (lock rule e): a second project.json name would be an entry
                # without the package's fields, which sync, status and remove could not tell for the same package
                raise ResolveError([resolve.Problem(
                    "local_name", f"{', '.join(keys[1:])}: {resolve.name_text(node)} is a dependency of this project "
                                  f"already, as {keys[0]}: one package has one name in project.json; use {keys[0]}")])
            if meta:
                own = [n for n in names if n not in keys[1:]]
                if keys:
                    own = [keys[0], *(n for n in own if n != keys[0])]
                entry |= {"package": m.name, "version": str(m.version), "names": own}
                if not node.held:                        # a held entry's dependencies are not resolved: none said
                    entry["deps"] = {local: res.nodes[t].package for local, t in res.deps.get(id, {}).items()}
            if keys:
                libs[keys[0]] = self.place(keys[0], entry)
                for k in keys[1:]:                       # one package under two project.json names: its own entry
                    libs[k] = self.place(k, {x: entry[x] for x in ("commit", "tree", "sha", "files") if x in entry})
            else:
                pkgs[m.name] = entry
            for n in names:
                written = libs[n] if n in keys else libs[keys[0]] if keys else entry
                marker = dict(written)
                if m is not None:
                    marker |= {"name": n, "direct": n in keys, "package": m.name, "version": str(m.version)}
                    if not keys:
                        marker["via"] = [res.nodes[r].package for r in res.requirers(id)]
                installs[n] = (node, written, marker)
        return libs, pkgs, installs

    def place(self, key: str, entry: dict) -> dict:
        """A libraries entry at the git, rev and subdir its project.json requirement names (the same commit may have
        been reached first through a manifest, or locked as a package), so that _stale finds it as asked."""
        r = self.direct[key]
        rest = {k: v for k, v in entry.items() if k not in ("git", "rev", "subdir")}
        return _spec_of({"git": r.git, "rev": r.rev, "subdir": r.subdir}) | rest

    def stage(self, installs: dict) -> dict[str, Path]:
        """Stages every (package, local name) not installed as it will be locked: everything resolved anew, and a
        locked package's new names. A locked one already installed under a name is left as it is (its marker may
        change). Returns {name: staging directory}; on failure none is left."""
        staged: dict[str, Path] = {}
        try:
            for n, (node, entry, _) in installs.items():
                d = _own_dir(self.prj.path, n)
                have = installed(self.prj.path, n)
                if node.fixed and (not node.files or have is not None and d.is_dir()
                                   and have.get("commit") == entry.get("commit")
                                   and entry_hash(have) == entry_hash(entry)):
                    continue                             # as installed (or a plain entry sync installs later)
                spec = _spec_of(entry)
                staged[n] = _staged(self.prj.path, n, entry, node.files, lambda s=spec: self.source(s))
        except BaseException:
            for st in staged.values():
                shutil.rmtree(st, ignore_errors=True)
            raise
        return staged

    def gone(self, res: resolve.Resolution, walked, keep) -> list[str]:
        """The local names the lock gave the entries reached from `walked` that the resolution installs no more."""
        _, old = resolve.reachable(self.lock, walked)
        return sorted(n for n in old if n not in res.names and n not in keep and _plain_name(n))

    def changed(self, new: dict, walked) -> list[tuple[str, dict, dict | None]]:
        """The entries reached from `walked` in the lock as read whose commit or files the new lock changes or
        drops: (id, old entry, new entry or None)."""
        g, h = self.graph, resolve.lock_graph(new)
        ids, _ = resolve.reachable(self.lock, walked)
        out = []
        for id in sorted(ids):
            e = g.entries[id]
            table, key = g.keys[id]
            now = h.entries.get(id)
            if now is None and table == "libraries":
                now = (new.get("libraries") or {}).get(key)
            if now is None or now.get("commit") != e.get("commit") or entry_hash(now) != entry_hash(e):
                out.append((id, e, now))
        return out

    def untrust(self, changed) -> list[str]:
        """The names whose trust (project.json `trusted`) goes with other content: the package names and local names
        of every changed or dropped entry."""
        out: list[str] = []
        for id, e, now in changed:
            for n in (e.get("package"), (now or {}).get("package"), *self.graph.names_of(id)):
                if isinstance(n, str) and n not in out:
                    out.append(n)
        return out

    def fresh(self, res: resolve.Resolution) -> list[str]:
        """The package names resolved anew at content the lock did not have for them (a package new to the project,
        or at another commit or tree): a trust key is only name@version, which any repository can claim, so one left
        from earlier content (a hand-removed dependency) must not trust this one."""
        out: list[str] = []
        for id, node in res.nodes.items():
            if node.fixed or node.manifest is None:
                continue
            old = self.graph.entries.get(id)
            same = old is not None and old.get("commit") == node.commit and \
                entry_hash(old)[1] == self.hashes.get(node)
            if not same and node.package not in out:
                out.append(node.package)
        return out

    def resolved(self, res: resolve.Resolution, libs: dict, pkgs: dict) -> list[dict]:
        """What this operation resolved anew: {name, **entry} for a plain dependency, as phase 1 reported it; a
        package adds package, version, names and via (its requirers)."""
        out = []
        for id, node in res.nodes.items():
            if node.fixed:
                continue
            keys = [k for k, i in res.direct.items() if i == id]
            names = res.names_of(id)
            item = {"name": names[0], **(libs[keys[0]] if keys else pkgs[node.package])}
            if node.manifest is not None:
                item |= {"package": node.package, "version": str(node.version), "names": names,
                         "via": [res.nodes[r].package for r in res.requirers(id)]}
            out.append(item)
        return out


def _prune(res: resolve.Resolution, drop: list[str]) -> None:
    """Leaves the direct names `drop` out of a resolution (project.json changed them meanwhile: the next lib lock
    resolves them), with every node only they reach and their names."""
    for k in drop:
        res.direct.pop(k, None)
    keep: set[str] = set()
    todo = list(res.direct.values())
    while todo:
        i = todo.pop()
        if i not in keep:
            keep.add(i)
            todo += res.deps.get(i, {}).values()
    for i in [i for i in res.nodes if i not in keep]:
        del res.nodes[i]
        res.deps.pop(i, None)
    for n in [n for n, i in res.names.items() if i not in keep or (n in drop and n not in res.direct)]:
        node = res.nodes.get(res.names[n])
        if node is None or node.manifest is None or n != node.package:
            del res.names[n]


def _result_cells(item: dict, d: Path, m: mf.Manifest | None) -> dict:
    cells = _cells(d)
    internal = [c for c in cells if m is not None and m.exports is not None and c not in m.exports]
    return item | {"cells": cells, "public_cells": [c for c in cells if c not in internal], "internal_cells": internal}


def add(prj, name: str, git: str, rev: str | None = None, subdir: str | None = None, guard=None,
        local_roots: list[Path] | None = None, *, package: str | None = None) -> dict:
    """Adds a dependency, hashed as the lock's format says (`sha` in format 1, `tree` in format 2), with every
    package its manifest needs, resolved together with what is locked (held as locked, never fetched again). Nothing
    is written until every manifest is read and the whole graph checked (ResolveError lists every problem).
    `package`: the [package] name the repository must have (recorded in project.json). `local_roots`: given from
    HTTP (serve --dep-root), where a local repository may be (_source), for every URL a manifest names too. Returns
    the entry with `cells` (`public_cells`, `internal_cells` by [exports]), `created` (whether this call made the
    lock), `package`, `version`, `resolved` (every entry resolved: the dependency and the packages it brought) and
    `flagged` / `trusted` (what the trust rule would refuse in it)."""
    from undine.store.project import check_name
    check_name(name)
    _not_taken(prj, name)
    if package is not None and not (isinstance(package, str) and mf.NAME.match(package)):
        raise DepError(f"package {package!r} is not a package name ({mf._NAME_FIX}); nothing was changed")
    spec = {"git": git, **({"rev": rev} if rev else {}), **({"subdir": subdir} if subdir else {}),
            **({"package": package} if package else {})}
    with _deps_lock(prj.path):
        _reread(prj)
        _not_taken(prj, name)
        lock = read_lock(prj.path)                       # a lock this Undine cannot write: refused before fetching
        _own_dir(prj.path, name)
        what = _reserved(prj, lock).get(name.casefold())
        if what:
            raise DepError(f"{name}: {what} has that name, and .undine/deps would shadow it: choose another local "
                           "name; nothing was changed")
        g = _Graph(prj, lock, local_roots, f"lib add {prj.name} {name} {git}")
        _check_spec(spec)
        with _in_store(prj):
            keys = g.walked(exclude={name})
            direct = g.held(keys)
            direct[name] = _requirement(name, spec, package)
            res = g.run(direct, g.fixed(keys))
            libs, pkgs, installs = g.tables(res)
            staged = g.stage(installs)
            try:
                env = environment(prj.pdk)               # for a new lock; the tools answer before the guard
                with _committing(prj, guard):
                    if name in prj.library_specs():
                        raise DepError(f"library {name} exists")
                    now, created = _lock_now(prj, env)   # read again (a git pull meanwhile), before anything changes
                    _unchanged(lock, now)
                    _apply(prj, installs, staged, g.gone(res, keys, set(_git_specs(prj))))
                    prj.meta.setdefault("libraries", {})[name] = spec
                    _untrust(prj.meta, g.fresh(res))
                    prj.save()
                    _put_tables(now, libs, pkgs)
                    write_lock(prj.path, now)
            finally:
                for st in staged.values():
                    shutil.rmtree(st, ignore_errors=True)
    node = res.nodes[res.direct[name]]
    d = dep_dir(prj.path, name)
    out = _result_cells({"name": name, **libs[name]}, d, node.manifest)
    out |= {"created": created, "package": node.package, "version": str(node.version) if node.version else None,
            "resolved": [_flagged(prj, r, dep_dir(prj.path, r["name"])) for r in g.resolved(res, libs, pkgs)]}
    return _flagged(prj, out, d)


def _placed_by(prj, name: str) -> list[str]:
    """The packages that place local name `name` (not a project.json dependency): from the lock, else from its
    install marker. [] when none does."""
    try:
        g = resolve.lock_graph(read_lock(prj.path))
    except DepError:
        g = resolve.lock_graph(None)

    def label(id: str) -> str:                          # a direct one by its project.json name: what to update
        table, key = g.keys[id]
        return key if table == "libraries" else g.entries[id].get("package") or key
    out: list[str] = []
    for id in g.entries:
        if g.keys[id] != ("libraries", name) and name in g.names_of(id):
            for r in g.requirers(id) or [id]:
                if label(r) not in out:
                    out.append(label(r))
    if not out:
        via = (installed(prj.path, name) or {}).get("via")
        out = [v for v in via if isinstance(v, str)] if isinstance(via, list) else []
    return out


def _dep_spec(prj, name: str) -> dict:
    spec = prj.library_specs().get(name)
    if not spec or "git" not in spec:
        placed = _placed_by(prj, name) if spec is None and _plain_name(name) else []
        if placed:
            raise NotDirect(f"{name}: placed by {' and '.join(placed)}; update or remove that instead", placed)
        raise DepError(f"{name} is not a library dependency")
    return spec


def update(prj, name: str, rev: str | None = None, guard=None, local_roots: list[Path] | None = None) -> dict:
    """Moves a dependency to `rev` (default: its rev again, e.g. a branch that moved), hashed as the lock's format says
    (a format 2 lock's entry gets a `tree` and loses its `sha`; a package moves onto its [files] include set). Its
    packages are resolved again with it, every other locked package held as locked; a package no longer reached is
    dropped with its names. Returns what changed: `changed` and `used_by` for it, `packages_changed` ({package,
    names, from, to, changed} per entry moved, added or dropped) and `used_by` over every local name of each. A
    name only a package places is NotDirect. `local_roots`: as add's."""
    _dep_spec(prj, name)
    with _deps_lock(prj.path):
        _reread(prj)
        base = _dep_spec(prj, name)                      # as the operation before this one left it
        lock = read_lock(prj.path)                       # a lock this Undine cannot write: refused before fetching
        spec = dict(base, **({"rev": rev} if rev else {}))
        _own_dir(prj.path, name)
        g = _Graph(prj, lock, local_roots, f"lib update {prj.name} {name}")
        _check_spec(spec)
        with _in_store(prj):
            keys = g.walked(exclude={name})
            direct = g.held(keys)
            direct[name] = _requirement(name, spec, spec.get("package"))
            target = g.graph.direct.get(name)
            res = g.run(direct, g.fixed(keys, {target} if target else set()))
            libs, pkgs, installs = g.tables(res)
            staged = g.stage(installs)
            walked = [*keys, name]
            try:
                preview = copy.deepcopy(lock or {})
                _put_tables(preview, libs, pkgs)
                changed = g.changed(preview, walked)
                report = _update_report(prj, g, res, name, spec, staged, changed, preview)
                env = environment(prj.pdk)
                with _committing(prj, guard):
                    if prj.library_specs().get(name) != base:   # removed or changed meanwhile (an adopt, git)
                        raise DepError(f"{name} was changed in project.json meanwhile: nothing updated, try again")
                    now, _ = _lock_now(prj, env)         # read again (a git pull meanwhile), before anything changes
                    _unchanged(lock, now)
                    _apply(prj, installs, staged, g.gone(res, walked, set(_git_specs(prj))))
                    prj.meta["libraries"][name] = spec
                    _untrust(prj.meta, g.untrust(changed) + g.fresh(res))
                    prj.save()
                    _put_tables(now, libs, pkgs)
                    write_lock(prj.path, now)
            finally:
                for st in staged.values():
                    shutil.rmtree(st, ignore_errors=True)
    return report


def _untrust(meta: dict, names: list[str]) -> list[str]:
    from undine.store import trust
    return trust.drop(meta, names)


def _update_report(prj, g: _Graph, res: resolve.Resolution, name: str, spec: dict, staged: dict[str, Path],
                   changed: list, new: dict) -> dict:
    """update's result, before anything is swapped in: the old installs are compared with the staged ones."""
    dest = dep_dir(prj.path, name)
    old = installed(prj.path, name) or {}
    entry = (new.get("libraries") or {}).get(name) or {}
    changes = _diff_files(dest, staged.get(name, dest))
    h = resolve.lock_graph(new)
    moved = []
    for id, e, now in changed:
        names = h.names_of(id) if now is not None and id in h.entries else g.graph.names_of(id)
        first = g.graph.names_of(id)[0]
        after = staged.get(names[0], dep_dir(prj.path, names[0])) if now is not None else prj.path / DEPS / ".none"
        moved.append({"package": (now or e).get("package"), "names": names, "from": e.get("commit"),
                      "to": (now or {}).get("commit"), "changed": _diff_files(dep_dir(prj.path, first), after)})
    for id, node in res.nodes.items():                   # packages this update brought in
        if id not in g.graph.entries and not node.fixed and id != res.direct.get(name):
            names = res.names_of(id)
            moved.append({"package": node.package, "names": names, "from": None, "to": node.commit,
                          "changed": _diff_files(prj.path / DEPS / ".none", staged.get(names[0], dep_dir(prj.path,
                                                                                                         names[0])))})
    used_by = _used_by_all(prj, name, changes, moved)
    return {"name": name, "from": old.get("commit"), "to": entry.get("commit"), "rev": spec.get("rev"),
            "changed": changes, "used_by": used_by, "packages_changed": moved}


def _used_by_all(prj, name: str, changes: dict, moved: list[dict]) -> list[str]:
    """_used_by for the dependency itself and for every local name of each package that moved."""
    out = _used_by(prj, name, changes)
    for p in moved:
        for n in p["names"]:
            if n != name:
                out += [u for u in _used_by(prj, n, p["changed"]) if u not in out]
    return out


def remove(prj, name: str, guard=None) -> dict:
    """Removes a dependency from project.json, with the packages only it reached (their entries and every name they
    were installed under), from the lock alone: nothing is fetched or resolved. A package that another one still
    places stays (it moves from `libraries` to `packages`, under the names its requirers give it) and its names are
    reported in `kept` ({name, placed_by}); `removed_packages` lists what went ({package, version, commit, names}).
    The trust given to what went goes too. A name only a package places is NotDirect."""
    _dep_spec(prj, name)
    with _deps_lock(prj.path), _committing(prj, guard):
        _dep_spec(prj, name)                              # removed meanwhile?
        _own_dir(prj.path, name)
        lock = read_lock(prj.path)                        # refused before project.json changes
        others = [k for k in _git_specs(prj) if k != name]
        removed, kept, gone, remark = _removal(prj, lock, name, others)
        for n in [*gone, *(n for n, _ in remark)]:
            _own_dir(prj.path, n)                         # every name checked before anything is written or removed
        del prj.meta["libraries"][name]
        _untrust(prj.meta, [n for r in removed for n in (r["package"], *r["names"]) if n])
        prj.save()
        if lock is not None:
            write_lock(prj.path, lock)
        for n, marker in remark:
            _remark(_own_dir(prj.path, n), marker)
        for n in gone:
            shutil.rmtree(_own_dir(prj.path, n), ignore_errors=True)
    return {"name": name, "removed_packages": removed, "kept": kept}


def _reach(g: resolve.Graph, id: str) -> set[str]:
    out, todo = set(), [id]
    while todo:
        i = todo.pop()
        if i not in out:
            out.add(i)
            todo += g.deps.get(i, {}).values()
    return out


def _removal(prj, lock: dict | None, name: str, others: list[str]) -> tuple[list, list, list, list]:
    """What removing direct dependency `name` changes in `lock` (in place): (removed [{package, version, commit,
    names}], kept [{name, placed_by}], local names to remove (`name` among them unless a package still places it),
    [(name, marker)] of the installs that stay and whose marker changes (no longer direct, other requirers))."""
    g = resolve.lock_graph(lock)
    if name not in g.direct:                              # not locked (or a format 1 / plain entry): as phase 1
        (lock or {}).get("libraries", {}).pop(name, None)
        return [], [], [name], []
    before, _ = resolve.reachable(lock, [*others, name])
    after, _ = resolve.reachable(lock, others)
    target = g.direct[name]
    names = {id: g.names_of(id) for id in after}
    if target in after:                                  # still placed by a package: no longer direct
        pkg = g.entries[target].get("package")
        placed = {n for r in after for n, t in g.deps.get(r, {}).items() if t == target and r != target}
        names[target] = [n for n in g.names_of(target) if n == pkg or n in placed]
        lock.setdefault("packages", {})[pkg] = dict(g.entries[target]) | {"names": names[target]}
    lock.get("libraries", {}).pop(name, None)
    removed = []
    for id in sorted(before - after):
        table, key = g.keys[id]
        e = g.entries[id]
        if table == "packages":
            lock.get("packages", {}).pop(key, None)
        removed.append({"package": e.get("package"), "version": e.get("version"), "commit": e.get("commit"),
                        "names": g.names_of(id)})
    if "packages" in lock and not lock["packages"]:
        del lock["packages"]
    final = {n for ns in names.values() for n in ns}
    old = {n for id in before for n in g.names_of(id)}
    gone = sorted(n for n in old - final if n not in others and _plain_name(n))
    h = resolve.lock_graph(lock)
    closure = _reach(g, target)
    kept, remark = [], []
    for id in sorted(after):
        e = h.entries.get(id)
        if e is None:
            continue
        via = [h.entries[r].get("package") or h.keys[r][1] for r in h.requirers(id)]
        for n in h.names_of(id):
            if id in closure and h.keys[id] != ("libraries", n) and n not in others:
                kept.append({"name": n, "placed_by": via})
            if not _plain_name(n):                        # a lock's name that is a path: never read or written
                continue
            have = installed(prj.path, n)
            if not e.get("package") or have is None:
                continue
            marker = dict(e) | {"name": n, "direct": h.keys[id] == ("libraries", n), "package": e["package"],
                                "version": e.get("version")}
            if h.keys[id][0] == "packages":
                marker["via"] = via
            if {k: v for k, v in have.items() if k != "modes"} != marker:
                remark.append((n, marker))
    return removed, kept, gone, remark


def _git_specs(prj) -> dict[str, dict]:
    return {n: s for n, s in prj.library_specs().items() if "git" in s}


def _stale(specs: dict[str, dict], lock: dict | None) -> dict[str, str]:
    """The git dependencies of project.json that the lock does not pin as named there, {name: why}: not locked, or
    locked with another git, rev or subdir, or (format 2) as another package than project.json's `package` (a pull
    that changed a requirement, a hand edit). A format 1 lock records no package, so there it is not compared."""
    locked = (lock or {}).get("libraries", {})
    out = {}
    for name, spec in specs.items():
        want = locked.get(name)
        if not isinstance(want, dict):
            out[name] = f"{name}: not in {LOCK}"
            continue
        keys = [k for k in ("git", "rev", "subdir") if want.get(k) != spec.get(k)]
        if spec.get("package") and lock_format(lock) >= 2 and want.get("package") != spec["package"]:
            keys.append("package")
        if keys:
            asks = ", ".join(f"{k} {spec[k]}" if spec.get(k) else f"no {k}" for k in keys)
            locks = ", ".join(str(want[k]) if want.get(k) else f"no {k}" for k in keys)
            out[name] = f"{name}: project.json asks for {asks}, {LOCK} locks {locks}"
    return out


def _unresolved(prj, lock: dict | None) -> dict[str, tuple[str, str]]:
    """The git dependencies of project.json locked before phase 2 (no `deps` in their entry) whose manifest asks for
    more than that entry installs, {name: (kind, why)}: kind 'deps' when it names dependencies the lock does not lock,
    'own' when its [package] name is not among the names it is installed under. From the store or the install, never
    git; an entry whose manifest is in neither (a fresh clone before sync) is not looked into. Such an entry is held
    as it was locked (_Graph.fixed) until lib lock or lib update of it resolves it."""
    if not lock:
        return {}
    g = resolve.lock_graph(lock)
    out: dict[str, tuple[str, str]] = {}
    for k in _git_specs(prj):
        if k not in g.direct:
            continue
        e = g.entries[g.direct[k]]
        if "deps" in e:                                  # resolved by phase 2: its graph is in the lock
            continue
        try:
            m = locked.manifest_of(prj.path, k, e)
        except (locked.NotAvailable, mf.ManifestError, DepError):
            continue
        if m is None:
            continue
        if m.dependencies:
            out[k] = ("deps", f"{k}: its {mf.MANIFEST} names dependencies ({', '.join(m.dependencies)}) that {LOCK} "
                              "does not lock")
        elif m.name != k and m.name not in g.names_of(g.direct[k]):
            out[k] = ("own", f"{k}: package {m.name}, which {LOCK} does not install under its own name")
    return out


def _incomplete(prj, lock: dict | None, manifests: bool = True, own: bool = True) -> dict[str, str]:
    """The git dependencies of project.json whose locked graph cannot be installed as it is, {name: why}: a package
    the lock needs and does not lock, a local name claimed twice or reserved (resolve.incomplete, format 2), or, in
    format 2, an entry locked before phase 2 that _unresolved names (`own` False: not one only missing its own name,
    which status notes). A format 1 lock cannot record what such an entry needs, so there they are left to status's
    notes and to `lib lock --format 2`. `manifests` False: from the lock alone (what sync refuses: an entry locked
    before phase 2 installs as it did then)."""
    if not lock:
        return {}
    specs = _git_specs(prj)
    g = resolve.lock_graph(lock)
    keys = [k for k in specs if k in g.direct]
    if not keys:
        return {}
    out: dict[str, str] = {}
    if manifests and lock_format(lock) >= 2:
        out = {k: why for k, (kind, why) in _unresolved(prj, lock).items() if own or kind != "own"}
    if lock_format(lock) < 2:
        return out
    reserved = _reserved(prj, lock)

    def sub(ids) -> dict:
        tables: dict = {"libraries": {}, "packages": {}}
        for id in ids:
            table, key = g.keys[id]
            tables[table][key] = g.entries[id]
        return {"format": 2, **tables}
    if not resolve.incomplete(sub(resolve.reachable(lock, keys)[0]), reserved):
        return out
    found = False
    for k in keys:
        why = resolve.incomplete(sub(resolve.reachable(lock, [k])[0]), reserved)
        if why:
            out.setdefault(k, f"{k}: " + "; ".join(why))
            found = True
    if not found:                                        # between two closures (a name claimed by both): each of them
        why = "; ".join(resolve.incomplete(sub(resolve.reachable(lock, keys)[0]), reserved))
        for k in keys:
            if g.entries[g.direct[k]].get("package"):
                out.setdefault(k, f"{k}: {why}")
    return out


def _frozen(prj, specs: dict[str, dict], lock: dict | None) -> None:
    """Sync installs what the lock pins and resolves nothing: a lock that does not pin what project.json names, or
    whose graph cannot be installed as it is (a package it needs and does not lock, a local name claimed twice or
    reserved: _incomplete from the lock alone), is refused before anything is read or fetched, naming each entry and
    the commands that resolve it."""
    why = _stale(specs, lock)
    for k, w in _incomplete(prj, lock, manifests=False).items():
        why.setdefault(k, w)
    if why:
        one = next(iter(why)) if len(why) == 1 else "NAME"
        raise DepError(f"{'; '.join(why.values())}: run `python -m undine lib lock {prj.name}` to resolve "
                       f"{'it' if len(why) == 1 else 'them'}, or `python -m undine lib update {prj.name} {one}`; "
                       "nothing was changed")


def _marker_of(g: resolve.Graph, id: str, name: str, reached) -> dict:
    """The install marker of lock entry `id` under local name `name`: a plain entry's is the entry itself, as phase 1
    wrote it; a package's adds name, direct, package and version, and via (its requirers among the entries
    `reached`) when it is reached only through manifests. As _Graph.tables writes it."""
    e = g.entries[id]
    package = e.get("package")
    if not (isinstance(package, str) and package):
        return dict(e)
    table, key = g.keys[id]
    marker = dict(e) | {"name": name, "direct": table == "libraries" and name == key, "package": package,
                        "version": e.get("version")}
    if table == "packages":
        marker["via"] = _via(g, id, reached)
    return marker


def _via(g: resolve.Graph, id: str, reached) -> list[str]:
    """The requirers of entry `id` among `reached`, by package name (a libraries entry without one by its key)."""
    return [g.entries[r].get("package") or g.keys[r][1] for r in g.requirers(id) if r in reached]


def _plan(declared, lock: dict | None) -> dict[str, tuple[str, dict, dict, frozenset]]:
    """What the lock installs for project.json's git names `declared`, from its graph alone: {local name: (id, lock
    entry, marker, the declared names it is reached from)}, for every name of every entry they reach. Direct entries
    first, in project.json's order, then what they reach."""
    g = resolve.lock_graph(lock)
    roots: dict[str, set[str]] = {}
    for k in declared:
        todo = [g.direct[k]] if k in g.direct else []
        while todo:
            id = todo.pop(0)
            if k not in roots.setdefault(id, set()):
                roots[id].add(k)
                todo += g.deps.get(id, {}).values()
    direct = [g.direct[k] for k in declared if k in g.direct]
    order = list(dict.fromkeys([*direct, *roots]))
    out: dict[str, tuple[str, dict, dict, frozenset]] = {}
    for id in order:
        for n in g.names_of(id):
            out.setdefault(n, (id, g.entries[id], _marker_of(g, id, n, roots), frozenset(roots[id])))
    return out


def _current(project_path: Path, name: str, want: dict) -> bool:
    have = installed(project_path, name)
    return bool(have) and have.get("commit") == want.get("commit") and _content_ok(dep_dir(project_path, name), want)


def _fetch_missing(wanted: list[tuple[str, dict]], source: Callable[[int], str | None], offline: bool) -> None:
    """Brings the commit of each (local name, lock entry) of `wanted` into its kept repository, all before anything
    is staged, once per URL and commit (the packages of one repository share it); refuses naming every package that
    is not there offline (once, by its first name), or whose fetch failed. source(i): what git fetches wanted[i]
    from, checked before git reads its repository."""
    missing, failed = [], []
    there: dict[tuple[str, str], bool] = {}
    for i, (name, want) in enumerate(wanted):
        url, commit = want["git"], want["commit"]
        try:
            if (url, commit) not in there:
                there[url, commit] = gitsrc.has_commit(url, commit)
                if not there[url, commit] and not offline:
                    gitsrc.fetch(url, source(i), commit, commit)
                    there[url, commit] = True
            if not there[url, commit]:
                missing.append(_named(name, want))
        except gitsrc.GitSourceError as e:
            there[url, commit] = True                    # said once
            failed.append(f"{_named(name, want)}: {e}")
    if missing:
        raise DepError(_not_here(missing))
    if failed:
        raise DepError(f"could not fetch {'; '.join(failed)}; nothing was changed")


def _row(name: str, action: str, commit, entry: dict | None) -> dict:
    """A sync result row: {name, action, commit}, as phase 1 gave it, and `package` for a package's names."""
    package = (entry or {}).get("package")
    return {"name": name, "action": action, "commit": commit} | (
        {"package": package} if isinstance(package, str) and package else {})


def sync(prj, guard=None, local_roots: list[Path] | None = None, offline: bool | None = None) -> list[dict]:
    """Installs every dependency exactly as locked: each of project.json's git dependencies and every package their
    lock entries reach (`packages`, through `deps`), under each of its local names. Frozen: a dependency of
    project.json that the lock does not pin as named (not locked, or another git / rev / subdir / package), or whose
    locked graph cannot be installed as it is (a package missing from the lock, a local name claimed twice), is
    refused before anything is read (`lib lock` or `lib update` resolves it). Each entry not installed as locked
    comes from the store when its record and objects are there (no repository is read), else from the kept
    repository, else is fetched (once per repository and commit); `offline` (default: UNDINE_OFFLINE=1) never
    fetches and names what is missing. All or nothing: when one fails (a remote unreachable, files that do not match
    the lock) none is installed, and nothing removed. A dependency that nothing of project.json places any more
    (leftovers()) loses its files and its lock entry, a package entry no dependency reaches too. A name whose
    project.json requirement or lock entry changed once staged (a changeset adopted, a git command) is left out:
    neither installed nor removed, as this sync did not see that change; the next one acts on it. `local_roots`: as
    add's, before git reads a dependency's repository; one installed from the store or as locked reads none, so it is
    not checked. An install that matches its lock entry but is not in the store is imported into it. Returns one row
    per name ({name, action, commit}, `package` for a package), and `warning` on a package the locked PDK does not
    fit: it is installed all the same (status reports it)."""
    if offline is None:
        offline = os.environ.get("UNDINE_OFFLINE") == "1"
    command = f"lib sync {prj.name}"
    with _deps_lock(prj.path):
        _reread(prj)
        lock = read_lock(prj.path)
        specs = _git_specs(prj)
        for name, spec in specs.items():                 # all refused before anything is read, nothing half done
            _own_dir(prj.path, name)
            _check_spec(spec, git=False)
        _frozen(prj, specs, lock)
        lock = lock or new_lock(environment(prj.pdk))    # no git dependency: a lock as `lib lock` makes one
        plan = _plan(specs, lock)
        for name, (_, want, _, _) in plan.items():       # every name a package is installed under, and its place
            _own_dir(prj.path, name)
            _check_spec(_spec_of(want), git=False)
            _check_commit(name, want.get("commit"))
            _check_hash(name, want)
        gone, gone_entries = leftovers(prj, lock), _unreached(lock, specs)
        with _in_store(prj):
            current = {n for n, (_, want, _, _) in plan.items() if _current(prj.path, n, want)}
            first: dict[str, str] = {}                   # entry id -> the first of its names to install
            for name, (id, _, _, _) in plan.items():
                if name not in current:
                    first.setdefault(id, name)
            entries = {id: plan[n][1] for id, n in first.items()}
            stored = {id: _stored(*entry_hash(e)) for id, e in entries.items()}      # None: not there
            for name in current:
                _seed(dep_dir(prj.path, name), plan[name][1])
            sources: dict[str, str | None] = {}

            def source(id: str) -> str | None:          # checked before git reads the repository, once
                if id not in sources:
                    spec = _spec_of(entries[id])
                    _check_spec(spec)
                    sources[id] = _source(spec, local_roots, command)
                return sources[id]
            missing = [id for id, files in stored.items() if files is None]
            for id in missing:
                source(id)
            _fetch_missing([(first[id], entries[id]) for id in missing], lambda i: source(missing[i]), offline)
            out, todo, files_of = [], [], {}
            try:
                for name, (id, want, marker, _) in plan.items():
                    if name in current:
                        out.append(_row(name, "ok", want["commit"], want))
                        continue
                    if id not in files_of:
                        kind, value = entry_hash(want)
                        _, files_of[id], _ = _resolve(prj.path, name, _spec_of(want), want["commit"],
                                                      lambda i=id: source(i), kind, value, stored[id], offline,
                                                      _mode(want), parse=False)
                    staging = _staged(prj.path, name, want, files_of[id], lambda i=id: source(i), offline)
                    todo.append((name, staging, marker))
                    out.append(_row(name, "installed", want["commit"], want))
                env = environment(prj.pdk)
                with _committing(prj, guard):
                    lock = _lock_of(prj, env)             # as it is now (a PDK change meanwhile is kept)
                    now_specs = _git_specs(prj)
                    now = _plan(now_specs, lock)

                    def same(n: str) -> bool:            # placed as staged, by requirements as they were
                        was, is_ = plan[n], now.get(n)
                        return is_ is not None and was[:3] == is_[:3] and all(
                            now_specs.get(k) == specs[k] for k in was[3])
                    for name, staging, marker in todo:
                        if not same(name):
                            out = [o for o in out if o["name"] != name]
                            continue
                        _install(staging, dep_dir(prj.path, name), marker)   # the lock keeps its entry as it is
                    for name in current:                  # a package's marker: who places it may have changed
                        marker = plan[name][2]
                        have = installed(prj.path, name)
                        if marker.get("package") and same(name) and have is not None and \
                                {k: v for k, v in have.items() if k != "modes"} != marker:
                            _remark(dep_dir(prj.path, name), marker)
                    out += _remove_leftovers(prj, lock, gone, gone_entries, now_specs)
                    write_lock(prj.path, lock)
            finally:
                for _, staging, _ in todo:
                    shutil.rmtree(staging, ignore_errors=True)
    _warn_unfit(prj, lock, out)
    return out


def _remove_leftovers(prj, lock: dict, gone: dict, gone_entries: set, declared) -> list[dict]:
    """sync's removals, under _committing: the names and entries that were leftovers when it started and still are
    in `lock` as it is now (one placed again meanwhile stays, for the next sync). Changes `lock` in place."""
    out = []
    dropped: list = []                                    # the package and local names whose trust goes with them
    for name, info in leftovers(prj, lock).items():
        if name not in gone:                              # gone from project.json while fetching: the next sync
            continue
        shutil.rmtree(_own_dir(prj.path, name), ignore_errors=True)            # a plain name: leftovers()
        lock.get("libraries", {}).pop(name, None)
        dropped += [name, info["package"]]
        out.append(_row(name, "removed", info["installed"] or info["locked"], info))
    g = resolve.lock_graph(lock)
    for table, key in _unreached(lock, declared) & gone_entries:
        id = next((i for i, k in g.keys.items() if k == (table, key)), None)
        if id is not None:
            dropped += [g.entries[id].get("package"), *g.names_of(id)]
        lock[table].pop(key, None)
    if "packages" in lock and not lock["packages"]:
        del lock["packages"]
    # a trust key is name@version, which any repository can claim: one left for what went would trust the next
    # package to claim it. What the lock still installs keeps its own.
    h = resolve.lock_graph(lock)
    ids, names = resolve.reachable(lock, declared)
    still = names | set(declared) | {h.entries[i].get("package") for i in ids}
    if _untrust(prj.meta, [n for n in dropped if isinstance(n, str) and n not in still]):
        prj.save()
    return out


def _warn_unfit(prj, lock: dict, rows: list[dict]) -> None:
    """Adds `warning` to the rows of the packages the locked PDK does not fit (compat.locked_incompatible): sync
    installs what is locked all the same; set-pdk, add and update are where it is refused."""
    from undine.pdks import compat
    names = {r["name"] for r in rows if r["action"] != "removed"}
    if not names:
        return
    target = compat.project_target(lock_pdk(lock), prj.pdk)
    try:
        unfit = compat.locked_incompatible(prj.path, lock, target)
    except (DepError, OSError):
        return
    for u in unfit:
        for r in rows:
            if r["name"] == u["name"] and r["name"] in names and "warning" not in r:
                r["warning"] = f"{u['chain']}: does not fit the PDK {target.id if target else '(none)'}: {u['reason']}"


def relock(prj, guard=None, local_roots: list[Path] | None = None, *,
           upgrade: Callable[[dict], dict] | None = None) -> dict:
    """`lib lock`: resolves the git dependencies the lock does not pin as project.json names them (not locked, or
    another git / rev / subdir / package) or whose locked graph is incomplete (_incomplete: then at its locked
    commit), with the packages their manifests need, every other locked package held as locked; all or nothing,
    fetching them (with add's checks, `local_roots` as its) and installing them. Then it records the tool versions
    in use now, and the PDK id in format 1, in the lock's own format: format 2 gets its tools only, as numbers with
    their origin; its PDK pin (an object) is moved by the PDK commands. `upgrade` (pdkops.upgrader's converter): the
    lock is converted to format 2 in memory first, resolved in format 2 and written once, so a refused resolution
    leaves the lock as it was. A trust given to an entry whose commit or files change goes. Returns {lock, resolved:
    the entries resolved, created: whether this call made the lock}."""
    _reread(prj)
    if upgrade is None:
        lock = read_lock(prj.path)
        if not _stale(_git_specs(prj), lock) and not _incomplete(prj, lock):
            with _in_store(prj):                         # nothing to fetch: no wait behind a fetch in progress
                lock, created, _ = _record_env(prj, guard, [])
            return {"lock": lock, "resolved": [], "created": created}
    with _deps_lock(prj.path):
        _reread(prj)
        start = read_lock(prj.path)
        lock = upgrade(copy.deepcopy(start) if start is not None else {"libraries": {}}) if upgrade else start
        specs = _git_specs(prj)
        stale, incomplete = _stale(specs, lock), _incomplete(prj, lock)
        todo = [n for n in specs if n in stale or n in incomplete]
        for name in todo:                                # all refused before the first fetch
            _own_dir(prj.path, name)
            _check_spec(specs[name])
        g = _Graph(prj, lock, local_roots, f"lib lock {prj.name}")
        for name in todo:
            g.source(specs[name])
        with _in_store(prj):
            keys = g.walked(exclude=set(todo))
            direct = g.held(keys)
            for name in todo:                            # complete only: read again at the commit it is locked at
                at = None if name in stale else g.graph.entries[g.graph.direct[name]].get("commit")
                direct[name] = _requirement(name, specs[name], specs[name].get("package"), at)
            res = g.run(direct, g.fixed(keys, {g.graph.direct[n] for n in todo if n in g.graph.direct}))
            libs, pkgs, installs = g.tables(res)
            staged = g.stage(installs)
            fmt = g.fmt
            try:
                env = environment(prj.pdk, fmt)          # the tools answer outside the lock
                with _committing(prj, guard):
                    now, created = _lock_now(prj, env if start is None and fmt == 1 else None)
                    if start is not None:
                        _unchanged(start, now)
                    gone_meanwhile = [n for n in todo if prj.library_specs().get(n) != specs[n]]
                    if gone_meanwhile:                   # changed in project.json meanwhile: the next lib lock
                        _prune(res, gone_meanwhile)
                        libs, pkgs, installs = g.tables(res)
                    new = upgrade(now) if upgrade else now
                    if fmt == 1:
                        new.update(env)
                    else:
                        new["tools"] = env["tools"]
                    walked = [*keys, *(n for n in todo if n not in gone_meanwhile)]
                    _apply(prj, installs, {n: st for n, st in staged.items() if n in installs},
                           g.gone(res, walked, set(_git_specs(prj))))
                    _put_tables(new, libs, pkgs)
                    if _untrust(prj.meta, g.untrust(g.changed(new, walked)) + g.fresh(res)):
                        prj.save()
                    write_lock(prj.path, new)
            finally:
                for st in staged.values():
                    shutil.rmtree(st, ignore_errors=True)
    return {"lock": new, "resolved": g.resolved(res, libs, pkgs), "created": created}


def _record_env(prj, guard, todo: list) -> tuple[dict, bool, list[dict]]:
    """relock's write: the environment, in the lock's format, and the resolved entries `todo` (name, staging, entry,
    spec) whose spec project.json still names. Returns (lock, created, resolved)."""
    fmt = lock_format(read_lock(prj.path) or {})         # the tools answer outside the lock, for the format seen
    env = environment(prj.pdk, fmt)
    with _committing(prj, guard):
        lock, created = _lock_now(prj, env if fmt == 1 else None)
        if lock_format(lock) != fmt:                      # rewritten meanwhile (a git pull): asked again, rarely
            fmt = lock_format(lock)
            env = environment(prj.pdk, fmt)
        now = prj.library_specs()
        todo = [t for t in todo if now.get(t[0]) == t[3]]   # changed in project.json meanwhile: the next lib lock
        for _, _, entry, _ in todo:
            _fits(lock, entry)
        if fmt == 1:
            lock.update(env)
        else:
            lock["tools"] = env["tools"]
        resolved = []
        for name, staging, entry, _ in todo:
            _install(staging, dep_dir(prj.path, name), entry)
            lock.setdefault("libraries", {})[name] = entry
            resolved.append({"name": name, **entry})
        write_lock(prj.path, lock)
    return lock, created, resolved


def _state(project_path: Path, name: str, want: dict, have: dict | None) -> str:
    """How the install of `name` compares with its lock entry: missing, other version, modified or ok."""
    d = dep_dir(project_path, name)
    if have is None or not d.is_dir():
        return "missing"
    if have.get("commit") != want.get("commit"):
        return "other version"
    return "ok" if _content_ok(d, want) else "modified"


def status(prj) -> dict:
    """Everything that differs between the project as locked and as it is here, as {format, libraries, pdk, tools,
    issues, warnings, notes, ok}. Only issues make it not ok: another tool version or origin, or another Undine, is a
    warning (pixi pins the tools; a patch release must not block anyone), and a difference that is expected on a
    developer's machine (another Undine commit) or that the lock cannot tell (format 1) a note. `pdk` is
    pdkops.pdk_block's {requested, locked, commit, installed, state, changed, verified_by, ok}: the requirement and
    the pin disagreeing, or the pinned PDK missing or modified, is an issue; unverified, unchecked or not pinned a
    note.

    `libraries` has one row per local name: project.json's own libraries and git dependencies, every name the
    packages their lock entries reach are installed under (kind 'dependency' too, `direct` false), then the
    leftovers. A dependency row adds package, version, names, direct, via, chain, trusted (None when not installed),
    flagged (trust.counts), internal_cells and pdk {compatible, reason, note, built_with} (_Installed). Reads the
    lock, the installs and the store; no PDK file and no network."""
    lock = read_lock(prj.path)
    issues: list[str] = []
    libs = []
    view = _Installed(prj, lock)
    for name, spec in prj.library_specs().items():
        if "git" in spec:
            want = (lock or {}).get("libraries", {}).get(name)
            have = installed(prj.path, name)
            named = [k for k in ("git", "rev", "subdir") if want and want.get(k) != spec.get(k)]
            if want is None or named:
                state = "unlocked"                           # what project.json names is not what is locked: sync
            else:
                state = _state(prj.path, name, want, have)
            if state != "ok":
                issues.append(f"library {name}: {state}" + "".join(
                    f", project.json has {k} {spec.get(k)}, the lock {want.get(k)}" for k in named)
                    + (f": run `python -m undine lib lock {prj.name}` to lock it, or `python -m undine lib update "
                       f"{prj.name} {name}`" if state == "unlocked" else ""))
            libs.append({"name": name, "kind": "dependency", "git": spec["git"], "rev": spec.get("rev"),
                         "subdir": spec.get("subdir"), "locked": (want or {}).get("commit"),
                         "installed": (have or {}).get("commit"), "state": state, **_in_store_of(want),
                         **view.fields(name, state)})
        else:
            p = Path(spec["path"]).expanduser()
            inside = not p.is_absolute() or p.resolve().is_relative_to(prj.path.resolve())
            if not inside:
                issues.append(f"library {name}: linked from {p}, not versioned with the project")
            libs.append({"name": name, "kind": "own" if inside else "linked", "path": spec["path"],
                         "state": "ok" if inside else "linked"})
    for name in view.transitive():                       # placed by packages: installed as their lock entries say
        _, want, _, _ = view.plan[name]
        have = installed(prj.path, name) if view.readable else None
        state = _state(prj.path, name, want, have)
        if state != "ok":
            issues.append(f"library {name}: {state}: run `python -m undine lib sync {prj.name}`")
        libs.append({"name": name, "kind": "dependency", "git": want.get("git"), "rev": want.get("rev"),
                     "subdir": want.get("subdir"), "locked": want.get("commit"),
                     "installed": (have or {}).get("commit"), "state": state, **_in_store_of(want),
                     **view.fields(name, state)})
    # what project.json no longer names: until sync removes it, a schematic placing its symbols works here and not in
    # a fresh checkout (.undine/deps/ is on the symbol path while any dependency is declared)
    resolves = any("git" in s for s in prj.library_specs().values())
    try:
        gone = leftovers(prj, lock)
    except DepError as e:                                # .undine/deps is a link: reported, never followed
        issues.append(str(e))
        gone = {}
    for name, info in gone.items():
        where = " and ".join(w for w, on in ((f"installed in {(DEPS / name).as_posix()}/", info["on_disk"]),
                                             (f"locked in {LOCK}", info["in_lock"])) if on)
        issues.append(f"library {name}: not in project.json, still {where}"
                      + (" (its symbols still resolve here, not in a fresh checkout)" if info["on_disk"] and resolves
                         else "")
                      + f": run `python -m undine lib sync {prj.name}` to remove it, or add it back to project.json")
        want = (lock or {}).get("libraries", {}).get(name)
        libs.append({"name": name, "kind": "dependency", "git": info["git"], "rev": info["rev"], "subdir": info["subdir"],
                     "locked": info["locked"], "installed": info["installed"], "state": "leftover",
                     **_in_store_of(want), **_NO_FIELDS, "package": info["package"], "names": [name],
                     "direct": False})
    for key in bad_lock_keys(lock):
        issues.append(f"{LOCK}: libraries has an entry {key!r}, which is not a library name: delete that entry (sync "
                      "leaves it alone), or restore the lock from git")
    for key in bad_lock_keys(lock, "packages"):
        issues.append(f"{LOCK}: packages has an entry {key!r} that is not a package name or gives a local name that "
                      "is not a library name: delete that entry (sync leaves it alone), or restore the lock from git")
    for sp in prj.meta.get("symbol_paths", []):
        issues.append(f"symbol path {sp}: outside the project, not versioned with it")
    issues += view.issues()
    from undine.pdks import pdkops                       # here: pdkops imports this module
    # only_cached: status never reads the PDK (GET /deps would hash a gigabyte); a stale stamp cache is 'unchecked'
    pdk, pdk_issues, notes = pdkops.pdk_block(prj, lock, only_cached=True)
    issues += pdk_issues
    now = tools.tools_record()
    where = {n: _short(tools.find(n)) for n in TOOLS}     # here only: another machine's paths mean nothing here
    warnings: list[str] = []
    if lock is None:
        issues.append(f"no {LOCK}: run `python -m undine lib lock {prj.name}`")
        tl = [{"name": n, "locked": None, "now": now[n], "now_path": where[n], "ok": False} for n in TOOLS]
    else:
        locked_tools = lock_tools(lock)
        tl = []
        for n in TOOLS:
            said = _tool_warnings(n, locked_tools[n], now[n], where[n])
            tl.append({"name": n, "locked": locked_tools[n], "now": now[n], "now_path": where[n], "ok": not said})
            warnings += said
        _undine_and_pixi(locked_tools, now, warnings, notes)
        if lock_format(lock) == 1:
            notes.append("origins, Undine version and pixi.lock are not recorded (format 1)")
    notes += view.notes
    return {"format": lock_format(lock) if lock else None, "libraries": libs, "pdk": pdk, "tools": tl,
            "issues": issues, "warnings": warnings, "notes": notes, "ok": not issues}


# the package fields of a dependency row that is not installed from a lock entry (unlocked, a leftover)
_NO_FIELDS = {"package": None, "version": None, "names": [], "direct": True, "via": [], "chain": None,
              "trusted": None, "flagged": None, "internal_cells": [], "pdk": None}
_SCANS: dict[tuple[str, str], dict] = {}       # (hash kind, hash) -> trust.scan_dir of an install that is as locked
# what each kind of trust finding is, for status's notes
_FLAGS = {"control": ".control", "control_line": "*# lines", "path": "include paths", "unparsed": "cards not read",
          "rtl_c": "$c", "systemc": "`systemc", "rtl_system": "$system", "dpi": "DPI", "rtl_path": "file paths",
          "rtl_define": "token-pasting `define", "tcl": "Tcl"}


class _Installed:
    """status's view of what the lock installs (_plan): per lock entry its manifest (from the install, else the
    store's record: store/locked.py, never git), cells and trust scan, and from them each row's package fields, the
    issues (a closure that is not locked, a name claimed twice, a package the PDK does not fit, a project cell named
    like a package's, a manifest that cannot be read, a malformed `trusted`) and the notes (the same cell clash with a
    plain dependency, a direct name that shadows an Xschem folder, PDK compatibility not checked, another PDK commit
    than the one a package was drawn on, untrusted content a simulation would refuse)."""

    def __init__(self, prj, lock: dict | None):
        from undine.pdks import compat
        from undine.store import trust
        self.prj, self.lock = prj, lock
        self.g = resolve.lock_graph(lock)
        self.declared = _declared(prj)
        self.plan = _plan(self.declared, lock)
        self.reached = {i for i, _, _, _ in self.plan.values()}
        self.roots = {self.g.direct[k] for k in self.declared if k in self.g.direct}
        try:
            _deps_root(prj.path)
            self.readable = True
        except DepError:                                 # a link: leftovers() reports it; nothing read through it
            self.readable = False
        self.trusted = trust.trusted_keys(prj.path)
        pin = lock_pdk(lock) if lock else ({"id": prj.pdk.id} if prj.pdk else None)
        self.target = compat.project_target(pin, prj.pdk)
        asked = compat.project_target(None, prj.pdk) if prj.pdk else None
        self.asked = asked if asked is not None and asked != self.target else None   # project.json's, when other
        self.manifests: dict[str, tuple] = {}
        self.pdks: dict[str, dict | None] = {}
        self.found: list[str] = []
        self.notes: list[str] = []
        self.seen: set[str] = set()

    def transitive(self) -> list[str]:
        return [n for n in self.plan if n not in self.declared]

    def dir_of(self, id: str) -> Path | None:
        """The first install of entry `id` (a directory under .undine/deps), None when none is."""
        if not self.readable:
            return None
        for n in self.g.names_of(id):
            d = dep_dir(self.prj.path, n)
            if _plain_name(n) and d.is_dir():
                return d
        return None

    def manifest(self, id: str) -> tuple:
        """(manifest or None, the error that kept it from being read or None): from the install, else the store."""
        if id not in self.manifests:
            m = err = None
            d = self.dir_of(id)
            try:
                if d is not None:
                    m = mf.from_dir(d, d.name)
                elif self.readable:
                    m = locked.manifest_of(self.prj.path, self.g.names_of(id)[0], self.g.entries[id])
            except locked.NotAvailable:
                pass
            except mf.ManifestError as e:
                err = str(e)
            except (DepError, OSError):
                pass
            self.manifests[id] = (m, err)
        return self.manifests[id]

    def label(self, id: str) -> str:
        m, _ = self.manifest(id)
        if m is not None:
            return f"{m.name} {m.version}"
        commit = self.g.entries[id].get("commit")
        return f"{self.g.keys[id][1]} (git {str(commit)[:12]})"

    def files(self, id: str) -> list[str]:
        """The top-level file names of entry `id`: its install's, else the store's record of it."""
        d = self.dir_of(id)
        if d is not None:
            return [f.name for f in d.iterdir() if f.is_file()]
        try:
            return [p for p in locked.files_of(self.prj.path, self.g.names_of(id)[0], self.g.entries[id])
                    if "/" not in p] if self.readable else []
        except (locked.NotAvailable, DepError, OSError):
            return []

    def scan(self, d: Path, want: dict, as_locked: bool) -> dict:
        from undine.store import trust
        key = entry_hash(want)
        if as_locked and key in _SCANS:
            return _SCANS[key]
        found = trust.scan_dir(d)
        if as_locked:
            _SCANS[key] = found
        return found

    def pdk_of(self, id: str) -> dict:
        """The row's pdk {compatible, reason, note, built_with}, once per entry, with its issue or notes."""
        from undine.pdks import compat
        if id in self.pdks:
            return self.pdks[id]
        m, err = self.manifest(id)
        first = self.g.names_of(id)[0]
        req = m.pdk if m is not None else None
        if req is None:
            why = "no manifest" if m is None else "no [pdk]"
            out = {"compatible": None, "reason": why, "note": None, "built_with": None}
            if self.target is not None and err is None:
                what = "no manifest" if m is None else f"{m.name} {m.version} has no [pdk]"
                self.notes.append(f"library {first}: {what}: PDK compatibility not checked")
        else:
            reason, note = compat.check(req, self.target), compat.note(req, self.target)
            out = {"compatible": reason is None, "reason": reason, "note": note, "built_with": req.built_with}
            chain = self.chain(id) or first
            pdk_id = self.target.id if self.target else None
            if reason is not None:
                self.found.append(f"{chain}: does not fit the PDK {pdk_id or '(none)'}: {reason}: update or remove "
                                  f"it (lib update, lib remove), or choose a PDK it accepts")
            if note is not None:
                self.notes.append(f"library {first}: {m.name} {m.version}: {note}")
            other = compat.check(req, self.asked) if self.asked is not None else None
            if other is not None and other != reason:
                self.found.append(f"{chain}: does not fit the PDK project.json asks for ({self.asked.id}): {other}: "
                                  "update or remove it (lib update, lib remove), or choose a PDK it accepts")
        self.pdks[id] = out
        return out

    def chain(self, id: str) -> str | None:
        chains = [c for c in self.g.chains(id) if c[0] in self.roots]       # from project.json's dependencies
        return self.g.chain_text(self.prj.name, chains[0]) if chains else None

    def fields(self, name: str, state: str) -> dict:
        """A dependency row's package fields; _NO_FIELDS for a name the lock does not install."""
        from undine.store import trust
        if name not in self.plan or state == "unlocked":
            return dict(_NO_FIELDS, names=[name])
        id, want, _, _ = self.plan[name]
        m, err = self.manifest(id)
        if err is not None and id not in self.seen:
            self.found.append(f"library {err}")
        d = dep_dir(self.prj.path, name)
        here = self.readable and d.is_dir()
        found = self.scan(d, want, state == "ok") if here else None
        key = trust.key_of(d) if here else None
        trusted = (key in self.trusted) if key else None
        if found is not None and not trusted and id not in self.seen and any(found.values()):
            self.notes.append(f"library {name}: {_flag_text(found)} (not trusted): a simulation using it needs "
                              f"`python -m undine lib trust {self.prj.name} {name}`")
        pdk = self.pdk_of(id)
        self.seen.add(id)
        cells = _cells(d) if here else []
        internal = [c for c in cells if m is not None and m.exports is not None and c not in m.exports]
        version = str(m.version) if m is not None else want.get("version")
        return {"package": m.name if m is not None else want.get("package"), "version": version,
                "names": self.g.names_of(id), "direct": name in self.declared,
                "via": _via(self.g, id, self.reached),
                "chain": self.chain(id), "trusted": trusted,
                "flagged": trust.counts(found) if found is not None else None, "internal_cells": internal, "pdk": pdk}

    def issues(self) -> list[str]:
        """Every issue of the graph, once the rows are made (they find the per-entry ones)."""
        from undine.store import trust
        out = []
        fmt2 = bool(self.lock) and lock_format(self.lock) >= 2
        try:
            broken = _incomplete(self.prj, self.lock, own=False)
            unresolved = _unresolved(self.prj, self.lock)
        except DepError:
            broken, unresolved = {}, {}
        for why in broken.values():
            out.append(f"{why}: run `python -m undine lib lock {self.prj.name}{'' if fmt2 else ' --format 2'}`")
        for k, (kind, why) in unresolved.items():       # installed as phase 1 locked it, which still works
            if k in broken:
                continue
            fix = (f"`python -m undine lib lock {self.prj.name}` or `python -m undine lib update {self.prj.name} {k}`"
                   if fmt2 else f"`python -m undine lib lock {self.prj.name} --format 2`")
            self.notes.append(f"{why}: locked before packages had dependencies; {fix} resolves it")
        out += self.found
        out += self.clashes()
        trusted = self.prj.meta.get("trusted")
        if trusted is not None and not isinstance(trusted, list):
            out.append(f"project.json: trusted is {trusted!r}, not a list of name@version keys: fix it, or remove "
                       "it and trust again (lib trust)")
        for k in trusted if isinstance(trusted, list) else []:
            if not (isinstance(k, str) and trust.KEY.match(k)):
                out.append(f"project.json: trusted has {k!r}, which is not a name@version key: remove it, and trust "
                           "again with lib trust")
        shadowed = _shadowed(self.prj, self.lock)
        for n in self.declared:
            what = shadowed.get(n.casefold())
            if what and not what.startswith("the project library"):
                self.notes.append(f"library {n}: {what} has that name too: .undine/deps/{n} comes first on the "
                                  "symbol path and shadows it")
        return out

    def clashes(self) -> list[str]:
        """Project-library logic cells named like a dependency's (Resolution 4): an issue when the dependency is a
        package with a manifest (resolve.check_disjoint, no PDK cells: status reads no PDK files), a note for a plain
        one. Layout copies in the project are not its cells."""
        ids = list(dict.fromkeys(i for i, _, _, _ in self.plan.values()))
        if not ids:
            return []
        project = _project_cells(self.prj)
        labels = {id: self.label(id) for id in ids}
        packages = {}
        for id in ids:
            logic, layout = resolve.cell_names(self.files(id))
            closure = {labels[i] for i in _reach(self.g, id) - {id} if i in labels}
            manifested = self.manifest(id)[0] is not None
            packages[labels[id]] = (logic, layout, closure, manifested)
            if manifested:
                continue
            for cf, cell in logic.items():
                for lib, names in project.items():
                    if cf in {n.casefold() for n in names}:
                        self.notes.append(f"{cell}: {labels[id]} and project library {lib}: a netlist has one "
                                          "subcircuit per name, so a testbench placing both is refused: rename one")
        return [f"{p.message}: a netlist has one subcircuit per name: rename one of them"
                for p in resolve.check_disjoint(packages, project, {})]


def _flag_text(found: dict) -> str:
    """trust.scan_dir's findings in a few words: '2 cells with .control, include paths; 1 RTL file with $c'."""
    parts = []
    for group, one, many in (("cells", "cell", "cells"), ("rtl", "RTL file", "RTL files"), ("tcl", "file", "files")):
        items = found.get(group) or {}
        if items:
            kinds = sorted({_FLAGS.get(f.kind, f.kind) for fs in items.values() for f in fs})
            parts.append(f"{len(items)} {one if len(items) == 1 else many} with {', '.join(kinds)}")
    return "; ".join(parts)


def _in_store_of(want) -> dict:
    """A lock entry's `hash` kind ('sha' or 'tree', None when not locked) and whether its tree is in the store, whole
    (a sync installs it with no network). Read only: status imports nothing into the store."""
    if not isinstance(want, dict):
        return {"hash": None, "in_store": False}
    kind, value = entry_hash(want)
    return {"hash": kind, "in_store": isinstance(value, str) and _stored(kind, value) is not None}


def _tool_text(rec: dict | None) -> str:
    if rec is None:
        return "not installed"
    return (rec.get("version") or repr(rec.get("text"))) + (f" from {rec['from']}" if rec.get("from") else "")


def _tool_warnings(name: str, locked: dict | None, now: dict | None, path: str | None) -> list[str]:
    """What differs about one tool between the lock and here: another number, another origin (when the lock records
    one), installed on one side only, or a banner with no number in it (which cannot be compared)."""
    out = [f"{name}: no version number in its banner {rec.get('text')!r} ({side}), so its version is not compared"
           for side, rec in (("locked", locked), ("here", now)) if rec and not rec.get("version")]
    differs = (locked is None) != (now is None) or bool(locked and now and (
        locked.get("version") and now.get("version") and locked["version"] != now["version"]
        or locked.get("from") and locked["from"] != now.get("from")))
    if differs:                                          # where it came from here: tools.find may hit another one
        out.append(f"{name}: locked {_tool_text(locked)}, here {_tool_text(now)}{f' ({path})' if now and path else ''}")
    return out


_UNDINE = re.compile(r"^(?P<version>\S+)(?: \((?P<commit>[^)]*)\))?$")


def _undine_and_pixi(locked: dict, now: dict, warnings: list[str], notes: list[str]) -> None:
    """Undine's version and pixi.lock, when the lock records them (format 2). Another Undine version warns; another
    commit, or a build with changes (+dirty), is only a note, so a developer's Undine does not warn after each commit."""
    if "undine" in locked and locked["undine"] != now["undine"]:
        was, here = (_UNDINE.match(str(v)) for v in (locked["undine"], now["undine"]))
        same = was and here and was["version"] == here["version"]
        (notes if same else warnings).append(f"Undine: locked {locked['undine']}, here {now['undine']}")
    if "pixi_lock" in locked:
        if locked["pixi_lock"] is None:
            warnings.append(f"tools not pinned: {LOCK} records no pixi.lock (it was locked by an Undine installed "
                            "without pixi), so only the version numbers above say which tools it used")
        elif locked["pixi_lock"] != now["pixi_lock"]:
            notes.append(f"pixi.lock: locked {locked['pixi_lock']}, here {now['pixi_lock'] or 'none (not run by pixi)'}")


# ------------------------------------------------------------------ what an update changes


def _cells(d: Path) -> list[str]:
    return sorted({f.stem for f in d.iterdir() if f.suffix in (".sch", ".sym", ".oas")}) if d.is_dir() else []


def _diff_files(old: Path, new: Path) -> dict[str, list[str]]:
    def shas(d):
        return {f.relative_to(d).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
                for f in d.rglob("*") if f.is_file() and f.name != MARKER} if d.is_dir() else {}
    a, b = shas(old), shas(new)
    return {"added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b)),
            "modified": sorted(k for k in set(a) & set(b) if a[k] != b[k])}


def _used_by(prj, name: str, changes: dict[str, list[str]]) -> list[str]:
    """Cells of the project's own libraries that use a changed cell: schematics placing its symbol, and layout cells
    of the same name (copied into the library when placed, so they keep the old geometry until replaced)."""
    changed = {Path(f).stem: Path(f).suffix for f in changes["modified"] + changes["removed"]}
    out = []
    for lname, spec in prj.library_specs().items():
        if "git" in spec:
            continue
        lib = prj.library(lname)
        for sch in sorted(lib.path.glob("*.sch")) if lib.path.is_dir() else []:
            text = sch.read_text(errors="replace")
            hits = sorted(c for c in changed if re.search(rf"\bC \{{{re.escape(name)}/{re.escape(c)}\.sym\}}", text))
            if hits:
                out.append(f"{lname}/{sch.stem} (schematic) places {', '.join(hits)}")
        own = set(lib.layout_cells()[0]) if lib.path.is_dir() else set()
        for c in sorted(c for c, ext in changed.items() if ext == ".oas" and c in own):
            out.append(f"{lname}/{c} (layout) is a copy of {name}/{c}: not updated, place it again to take the new version")
    return out
