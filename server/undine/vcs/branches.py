"""The git plumbing of changesets on branches (docs/design/changeset-branches.md): the base commit made from a project's
working tree, the worktrees of a changeset's branch and of its base, the commits of a worktree, the three-way plan of
an apply or a revert and its atomic write, the merge commit on the user's branch, and the semantic diff of a base.
Pure functions over a project directory (or a worktree's): no server, no changeset records.

Paths are from the project directory (gitmerge's and git's are from the repository's top: a project may be a
subdirectory of its repository, `prefix`).

repository   Changesets need the project in a repository Undine's merge driver is registered in (merge.undine.driver
             in the repository's own config: git-setup's). ensure_repo sets up a project that is in none (git init
             and git-setup, nothing committed: the opening then makes its first commit, first_commit) and refuses one
             in a repository nobody set up for Undine (NotSetUp: a home directory kept in git), whose branches and
             configuration are not Undine's to write; NotSetUp names the command that sets it up (`undine git-setup
             <project>`) and has the repository's top, the project and the code the API answers with (400 {"detail",
             "code": "not_set_up", "top", "project"}).
first commit A branch with no commit yet (unborn: git init, nothing committed since) gets no merge commit: an apply
             there is left uncommitted. Where Undine runs git init (opening a changeset; Set up, in the History
             panel) it makes the project's first commit at once (first_commit: the tree as snapshot builds it, from
             what the user's index tracks of the project, no parent, the branch made at it only while it is still
             unborn); the History panel offers the same for a repository made otherwise, or before. A log with
             unsaved layout edits in it refuses it (Unsaved: the pre-commit hook refuses such a commit too), but at
             the opening of a changeset, which commits it as its base has it (nobody is there to save it).
lock         Every function below but diff and check_libraries takes the repository's lock (one per common git
             directory: a project's worktrees and the projects of one repository share it) for its temporary index,
             commit-tree, update-ref and worktrees, and for what it reads to decide them; reentrant, so that a caller
             holds it across several (`with lock(d) as r:` gives the Repo read inside it). check_libraries runs no
             git (there may be no repository yet). diff takes none: a layout's semantic diff can take
             seconds, and it only reads (git may refresh the index it reads on the way: the index updates after a
             commit are best effort for that reason).
git          Every git command runs with core.hooksPath=/dev/null (worktree add runs post-checkout, update-ref
             reference-transaction: the user's hooks are not for Undine's plumbing) and without the variables that
             would point it at another repository (GIT_DIR, GIT_INDEX_FILE, ...). Commits are made with commit-tree
             --no-gpg-sign, the message on stdin, author the actor ("user", "agent:claude"), committer Undine
             (identity: git may have no identity configured; reflog entries are Undine's too).
trees        Every tree built here is the project's files as they are (uncommitted and untracked files included,
             what .gitignore ignores left out unless tracked), through a temporary index in the git directory (never
             in the project: an archive of it sees no git file), without Undine's own output (gitmerge.GITIGNORE:
             runs/, tapeout/, llm/, .undine/, *.pre-split, whether or not the repository ignores it) and the
             changeset records (changesets/: the records, their evidence with copies of runs, the request log); in a
             repository whose project is a subdirectory, the rest of the tree is the parent commit's. The user's
             branch, index and working tree are never touched, but by first_commit and commit_merge (HEAD, and the
             index entries of the project's directory, of the paths commit_merge commits: reset to the commit, their
             stat data refreshed, best effort, as git leaves it to whoever holds index.lock).
stat cache   The temporary index starts from a copy of the index of the same working tree (snapshot: the user's;
             commit_worktree: the worktree's), its time kept (git's racy-file check), read-tree -m onto the parent:
             the entries whose content is the parent's keep their stat data, so git reads only the files that
             changed since, not every file (a file over core.bigFileThreshold would be compressed again each time).
             A file changed in the second the index was written may not show in its stat data: git reads it to be
             sure (in a worktree once, after its checkout: its index is then written anew). Entries the user marked
             assume-unchanged or skip-worktree are read again. Without a usable index (none yet, a conflict in it)
             every file is read. commit_worktree makes its index the worktree's afterwards (the tip's entries, stat
             data fresh; best effort, under index.lock).
libraries    A worktree holds what git stores, so snapshot refuses (Unsupported, naming the library) a project whose
             own library (project.json: a path in the project) the worktree could not hold as the project has it,
             each symbolic link judged the way the worktree's copy of it will be resolved (git checks a link out as
             its text says, from the worktree's folder):
             - a symbolic link, at the library's folder, above it, in it or on the way one of those leads, that
               leads out of the project or nowhere, whose text is an absolute path (it leads to the project's files,
               not to the worktree's copy), or whose text climbs out of the project (even to come back in: from the
               worktree it leads elsewhere): the proposal would write through it into the real files, or find
               nothing; a relative link that stays in the project is kept, it leads into the worktree's copy;
             - such a link, or what it leads to, that the tree does not keep (ignored, Undine's own output): the
               worktree would have nothing there;
             - a folder, the library's, above it or in it, that is a git repository of its own (a clone, a
               submodule): git stores a gitlink, which a new worktree holds empty.
             Refused rather than copied: a copy in the worktree is no file of the branch (apply would not merge it,
             or would write the copy over the user's link). Links and repositories elsewhere in the project are
             stored as git stores them. What the folder tells (all but what the tree keeps) is check_libraries',
             which needs no repository: it can refuse before ensure_repo makes one. Unsupported has the library, the
             path (the link's or the repository's), the kind ("link_out": out of the project, absolute or climbing
             out; "link_nowhere": to nothing, or to what the tree does not keep; "own_repository"), the target (the
             link's text; None for a repository) and the code the API answers with (400 {"detail", "code":
             "unsupported_library", "library", "path", "kind", "target"}).
worktrees    A worktree inside the repository's working tree (the store's root kept in git) is excluded in
             info/exclude while it exists: the user's `git add -A` would add it as an embedded repository. A worktree
             in the store's .changesets/ excludes all of it (/<path>/.changesets/: what is kept beside the worktrees,
             tip.json, gseq.floor, the snapshots of a batch or a restore, too) while a worktree of the repository is
             there; and the folder ignores itself whatever repository it is in (.changesets/.gitignore: "*", written
             before the first worktree there when it has none, never removed): the store's root kept in git with
             the project a repository of its own (a clone in the store) never sees it, nor what a closure leaves in
             it after the last worktree is gone.
plan         For each file of the project that differs between commits base and theirs: base's bytes, theirs', and
             ours, the project's file now. Ours equal to theirs: nothing to do; ours equal to base: theirs (a
             deletion when theirs has none); else the view's merge (gitmerge.merge by kind_of), git merge-file for a
             file of no view, both with no base when only the two sides have the file. Deleted on one side and
             changed on the other is the conflict "file"; a merge by lines that conflicts, "lines"; a merge that
             fails, "error". keep_added (a revert: base the tip, theirs the base commit): a file base has and theirs
             has not, that the changeset added, is kept as it is now (another cell may use what it made since).
               {"writes": {path: bytes | None (delete)}, "conflicts": [{"path", "kind", "conflicts": [conflict]}],
                "warnings": [{"path", "warning"}], "paths": the files the merge covers (not "kept"), "kept": [...]}
             A conflict is merge3.Conflict.to_json(): {"id", "what", "base", "ours", "theirs", "taken", "resolved"};
             those of whole files have the sides' states ("changed", "deleted", "added") as values.
write_plan   All or nothing: every new file is written next to its target first, then each target is moved aside and
             the new one renamed in; on any failure what was renamed is put back (and the directories made removed).
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from undine.vcs import gitmerge, history, logmerge

EMAIL = "undine@localhost"
COMMITTER = "Undine"
RECORDS = "changesets"              # a project's changeset records and evidence: never in a tree built here
SHADOW = ".changesets"              # the store's folder of worktrees (<root>/.changesets/<project>/<id>/work)
EXCLUDES = [*history._excludes(), f":(exclude,glob){RECORDS}/**"]     # pathspecs from the project directory
_OWN = [s.replace(":(exclude,", ":(", 1) for s in EXCLUDES]           # the same paths, to take out of an index
# what points git at another repository than the directory's (a server started from a git hook has them)
_LOCATING = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY", "GIT_PREFIX")
_NO_HOOKS = ("-c", "core.hooksPath=/dev/null")


class NotSetUp(Exception):
    """The project's repository is not set up for Undine (or there is none: NoRepo). `top`: the repository's top
    (None: no repository); `project`: the project's name (its directory's); `command`: the command line that sets it
    up (setup_command); `code`: what the API answers it with (module docstring)."""
    code = "not_set_up"

    def __init__(self, message: str, top: Path | None = None, project: str | None = None, command: str | None = None):
        super().__init__(message)
        self.top, self.project, self.command = top, project, command


class NoRepo(NotSetUp):
    """The directory is in no git repository."""


class Unsupported(ValueError):
    """A library of the project a worktree cannot hold as the project has it (module docstring, libraries):
    `library`, `path` (from the project directory: the symbolic link's, or the repository's), `kind` ("link_out",
    "link_nowhere", "own_repository"), `target` (the link's text; None for a repository); `code`: what the API
    answers it with."""
    code = "unsupported_library"

    def __init__(self, message: str, library: str, path: str, kind: str, target: str | None = None):
        super().__init__(message)
        self.library, self.path, self.kind, self.target = library, path, kind, target


class Unsaved(ValueError):
    """Operation logs of the project holding unsaved layout edits (a journal: what the pre-commit hook refuses,
    gitmerge.precommit), which first_commit does not put on the user's branch: `logs` [{"path" from the project
    directory, "lib", "cells"}]; `code`: what the API answers it with (409 {"detail", "code": "unsaved", "logs"})."""
    code = "unsaved"

    def __init__(self, message: str, logs: list[dict]):
        super().__init__(message)
        self.logs = logs


@dataclass(frozen=True)
class Repo:
    """The repository a directory is in, as git says when asked."""
    top: Path                       # the working tree's top (a worktree's own)
    prefix: str                     # the directory from the top ("" at the top)
    git_dir: Path                   # the working tree's git directory (a worktree's: .git/worktrees/<name>)
    common_dir: Path                # what the worktrees share: objects, refs, config, hooks
    head: str | None                # HEAD's commit; None on an unborn branch
    branch: str | None              # HEAD's branch (refs/heads/ left out), unborn too; None when detached
    set_up: bool                    # merge.undine.driver is in the repository's config (git-setup ran)


# ---------------------------------------------------------------- git


def identity(actor: str) -> dict[str, str]:
    """The environment of a commit: author the actor, committer Undine (whatever git has configured, or not)."""
    name = " ".join("".join(" " if ch in "<>\n" else ch for ch in actor or "").split()) or "user"
    return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": EMAIL,
            "GIT_COMMITTER_NAME": COMMITTER, "GIT_COMMITTER_EMAIL": EMAIL}


def _run(cwd: Path, *args: str, env: dict[str, str] | None = None,
         input: bytes | None = None) -> subprocess.CompletedProcess:
    e = {k: v for k, v in os.environ.items() if k not in _LOCATING}
    e.update(identity(COMMITTER))                   # reflog entries (worktree add -b, update-ref) need one too
    e.update(env or {})
    return subprocess.run(["git", *_NO_HOOKS, *args], cwd=cwd, input=input, capture_output=True, env=e)


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None, input: bytes | None = None) -> bytes:
    """git's output; RuntimeError("git <command>: <its last line of errors>") when it fails (gitmerge._git's)."""
    r = _run(cwd, *args, env=env, input=input)
    if r.returncode != 0:
        msg = r.stderr.decode(errors="replace").strip().splitlines()
        raise RuntimeError(f"git {' '.join(a for a in args[:2] if not a.startswith('-'))}: "
                           f"{msg[-1] if msg else 'failed'}")
    return r.stdout


def _out(cwd: Path, *args: str) -> str | None:
    """git's output as text; None when git fails (no such ref, no such setting)."""
    r = _run(cwd, *args)
    return (r.stdout.decode("utf-8", "surrogateescape").strip() or None) if r.returncode == 0 else None


def _common_dir(d: Path) -> Path:
    r = _run(d, "rev-parse", "--git-common-dir")
    if r.returncode != 0:
        err = r.stderr.decode(errors="replace")
        if "not a git repository" in err:
            raise NoRepo(f"{_name(d)} is not in a git repository", project=_name(d))
        raise RuntimeError(f"git rev-parse: {(err.strip().splitlines() or ['failed'])[-1]}")
    return (d / r.stdout.decode("utf-8", "surrogateescape").strip()).resolve()


def repo(project_dir: str | Path) -> Repo:
    """The repository the directory is in (NoRepo: none)."""
    d = Path(project_dir)
    common = _common_dir(d)
    out = _git(d, "rev-parse", "--show-toplevel", "--absolute-git-dir", "--show-prefix") \
        .decode("utf-8", "surrogateescape").split("\n")
    ref = _out(d, "symbolic-ref", "-q", "HEAD") or ""
    return Repo(top=Path(out[0]), prefix=(out[2] if len(out) > 2 else "").rstrip("/"), git_dir=Path(out[1]),
                common_dir=common, head=_out(d, "rev-parse", "-q", "--verify", "HEAD^{commit}"),
                branch=ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else None,
                set_up=bool(_out(d, "config", "--local", "--get", "merge.undine.driver")))


_SETUP = threading.Lock()           # git init and git-setup of a project in no repository (there is no lock yet)


def ensure_repo(project_dir: str | Path) -> dict:
    """{"git_setup": True} when the project was in no repository and is now one set up for Undine (git init,
    git-setup; nothing committed: the caller makes the first commit, first_commit), False when it was set up;
    NotSetUp when its repository is not set up for Undine."""
    d = Path(project_dir)
    with _SETUP:
        try:
            r = repo(d)
        except NoRepo:
            from undine.__main__ import git_setup   # the command line's (it imports the store)
            git_setup(d, say=None)
            return {"git_setup": True}
    if not r.set_up:
        name = _name(d)
        raise NotSetUp(f"{name} is in the git repository {r.top}, which is not set up for Undine: set it up with "
                       f"`undine git-setup {name}` ({_setup_elsewhere(d)}Set up, in the History panel), or move the "
                       f"project out of it", r.top, name, setup_command(d))
    return {"git_setup": False}


def _name(d: Path) -> str:
    """The project's name: its directory's (the store's <root>/<project>)."""
    return os.path.basename(os.path.abspath(d))


def setup_command(d: Path) -> str:
    """The command line of git-setup for the project in directory `d`: `undine git-setup <project>`, with the store's
    root first (`--root`) in a store that is not the command's default one."""
    from undine.__main__ import DEFAULT_ROOT        # the command line's (it imports the store)
    root = os.path.dirname(os.path.abspath(d))
    if _same(root, DEFAULT_ROOT.expanduser()):
        return f"undine git-setup {shlex.quote(_name(d))}"
    return f"undine --root {shlex.quote(root)} git-setup {shlex.quote(_name(d))}"


def _setup_elsewhere(d: Path) -> str:
    """The command line of git-setup for a project in a store that is not the command's default one (its root
    given first), as the start of the parenthesis that follows `undine git-setup <project>`; "" in the default."""
    from undine.__main__ import DEFAULT_ROOT        # the command line's (it imports the store)
    root = os.path.dirname(os.path.abspath(d))
    if _same(root, DEFAULT_ROOT.expanduser()):
        return ""
    return f"in this store: `undine --root {shlex.quote(root)} git-setup {shlex.quote(_name(d))}`; "


_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_LOCK = threading.Lock()


def _lock_of(d: Path) -> threading.RLock:
    key = _common_dir(d)
    with _LOCKS_LOCK:
        return _LOCKS.setdefault(key, threading.RLock())


@contextmanager
def lock(project_dir: str | Path) -> Iterator[Repo]:
    """The repository's lock (module docstring), held; gives the Repo as it is once held."""
    d = Path(project_dir)
    with _lock_of(d):
        yield repo(d)


def resolve(project_dir: str | Path, rev: str) -> str | None:
    """The commit `rev` names; None when it names none."""
    return _out(Path(project_dir), "rev-parse", "-q", "--verify", "--end-of-options", f"{rev}^{{commit}}")


def _full(ref: str) -> str:
    """A ref's full name: a branch's under refs/heads/ unless it starts with refs/."""
    return ref if ref.startswith("refs/") else f"refs/heads/{ref}"


def ref_exists(project_dir: str | Path, ref: str) -> bool:
    """Whether the ref exists (a name without refs/ is a branch)."""
    return _run(Path(project_dir), "show-ref", "--verify", "--quiet", _full(ref)).returncode == 0


def _commit_of(d: Path, rev: str) -> str:
    sha = resolve(d, rev)
    if sha is None:
        raise ValueError(f"no commit {rev}")
    return sha


@contextmanager
def _index(r: Repo, tree: str | None, start: Path | None = None) -> Iterator[dict[str, str]]:
    """A temporary index in the git directory holding `tree` (None: empty): the environment that uses it. `start`:
    an index of the same working tree to start from, for its stat data (stat cache, module docstring); one git
    cannot merge `tree` into (a conflict in it, an extension it needs elsewhere) is left out."""
    tmp = tempfile.mkdtemp(prefix="undine-index-", dir=r.git_dir)
    index = os.path.join(tmp, "index")
    env = {"GIT_INDEX_FILE": index}
    try:
        seeded = False
        if tree and start is not None and start.is_file():
            try:
                shutil.copy2(start, index)          # its time too: what racy-git compares the entries' with
                _git(r.top, "read-tree", "-m", "-i", tree, env=env)
                seeded = True
            except (OSError, RuntimeError):
                pass
        if not seeded:
            if os.path.lexists(index):
                os.unlink(index)
            if tree:
                _git(r.top, "read-tree", tree, env=env)
        yield env
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _install_index(r: Repo, env: dict[str, str]) -> None:
    """The temporary index made the working tree's own (commit_worktree's: the new tip's entries, their stat data
    fresh), under git's index.lock; best effort: an index another git is writing is left to it."""
    lock_file = r.git_dir / "index.lock"
    try:
        os.close(os.open(lock_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666))
    except OSError:
        return
    try:
        os.replace(env["GIT_INDEX_FILE"], r.git_dir / "index")
    except OSError:
        pass
    finally:
        try:
            os.unlink(lock_file)
        except OSError:
            pass


def _project_tree(d: Path, env: dict[str, str]) -> str:
    """The tree of the index `env` names with the project directory `d` as its working tree has it (trees, module
    docstring): what `git add -A` would stage, which git (2.34) refuses with an exclude pathspec naming an ignored
    directory ("paths are ignored"), so ls-files lists the files (the index's and the untracked ones) and
    update-index stages them. The index's entries with stat data are read again only when that changed (stat
    cache); those marked assume-unchanged or skip-worktree leave the index first, so that they are read."""
    _git(d, "rm", "--cached", "-r", "-q", "--ignore-unmatch", "--", *_OWN, env=env)
    listed = _git(d, "ls-files", "-z", "-v", "--cached", "--others", "--exclude-standard", "--", ".", *EXCLUDES,
                  env=env)
    tagged = [(n[:1], n[2:].rstrip(b"/")) for n in listed.split(b"\0") if n]       # "sub/": a repository in it
    names = list(dict.fromkeys(n for _, n in tagged))
    marked = {n for tag, n in tagged if tag not in b"H?"}       # h: assume-unchanged, S/s: skip-worktree
    root = os.fsencode(d)
    folders: dict[bytes, bool] = {}

    def in_folders(n: bytes) -> bool:
        """Whether every folder above `n` is a directory now (not a symbolic link, a file, or nothing)."""
        parent = os.path.dirname(n)
        if not parent:
            return True
        if parent not in folders:
            p = os.path.join(root, parent)
            folders[parent] = in_folders(parent) and os.path.isdir(p) and not os.path.islink(p)
        return folders[parent]

    # a file that is a directory now (not a symbolic link, which git stores as it is, nor a repository, which git
    # adds as a gitlink), and one whose folder is not one now (a link, a file: git would refuse to add that in
    # its place), leave the index first
    gone = {n for n in names if not in_folders(n) or os.path.isdir(p := os.path.join(root, n))
            and not os.path.islink(p) and not os.path.lexists(os.path.join(p, b".git"))}
    for flag, group in (("--force-remove", sorted(gone | marked)), ("--add", [n for n in names if n not in gone])):
        if group:
            _git(d, "update-index", flag, "--remove", "-z", "--stdin", env=env, input=b"\0".join(group) + b"\0")
    return _git(d, "write-tree", env=env).decode().strip()


def _libraries(d: Path) -> dict[str, str]:
    """The project's own libraries (project.json: a relative path in the project): their paths from `d`."""
    try:
        specs = json.loads((d / "project.json").read_text()).get("libraries") or {}
        items = list(specs.items())
    except (OSError, ValueError, AttributeError):
        return {}
    found = {}
    for name, spec in items:
        path = spec if isinstance(spec, str) else spec.get("path") if isinstance(spec, dict) and "git" not in spec \
            else None
        if not isinstance(path, str) or os.path.isabs(os.path.expanduser(path)):
            continue
        rel = os.path.normpath(path)
        if rel != "." and rel != ".." and not rel.startswith(".." + os.sep):
            found[str(name)] = rel
    return found


_THEN = "then open the changeset again"
_FILES = f"Put the files themselves in the project instead of the link, or make it a dependency (a \"git\" library " \
         f"in project.json), {_THEN}"
_RELATIVE = f"Make it a relative link that stays inside the project (ln -sr), {_THEN}"
_UNKEPT = ".gitignore or Undine's own output leaves it out"
# why a symbolic link is refused: (kind, what its text is, what the worktree's copy of it would do, what to do)
_WHY = {
    "nowhere": ("link_nowhere", "which does not exist", "find nothing there either", _FILES),
    "outside": ("link_out", "outside the project", "write through it into the files there", _FILES),
    "absolute": ("link_out", "an absolute path", "write through it into the project's files, not into its copy of "
                 "them", _RELATIVE),
    "climbs": ("link_out", "a path that climbs out of the project", "follow it from where the working copy is, to "
               "other files or to nothing", _RELATIVE),
    "unkept": ("link_nowhere", f"a link the project's repository does not keep ({_UNKEPT})", "not have it",
               f"Let the repository keep the link, or put the files themselves in the project, {_THEN}"),
    "ignored": ("link_nowhere", "which leads to {to}, what the project's repository does not keep (an empty folder, "
                f"or {_UNKEPT})", "find nothing there", "Let the repository keep {to}, or put the files themselves "
                f"in the library, {_THEN}"),
}


def _link(name: str, path: str, text: str, why: str, to: str = "") -> Unsupported:
    """Unsupported for library `name`: the symbolic link `path` (from the project directory), whose text is `text`,
    refused for `why` (_WHY; "ignored": what it leads to, `to`, is not kept)."""
    kind, what, would, do = _WHY[why]
    return Unsupported(
        f"library {name}: {path} is a symbolic link to {text}, {what.format(to=to)}: a changeset's working copy (a git "
        f"worktree) would hold the link and {would}. {do.format(to=to)}", name, path, kind, text)


def _repository(name: str, path: str) -> Unsupported:
    """Unsupported for library `name`: `path` (from the project directory) is a git repository of its own."""
    return Unsupported(
        f"library {name}: {path} is a git repository of its own (a clone or a submodule), which a changeset's working "
        f"copy (a git worktree) would hold empty. Make it a dependency (a \"git\" library in project.json), or let the "
        f"project's repository keep its files (move its .git out of the project), {_THEN}", name, path,
        "own_repository")


_HOPS = 40                          # the symbolic links one path may go through (Linux's, then ELOOP)


def _follow(d: Path, name: str, rel: str) -> tuple[str | None, list[tuple[str, str]]]:
    """Path `rel` (from the project directory, inside it) resolved name by name the way the worktree's copy of it
    will be (a link's text from the link's folder, `..` the folder above): (where it leads, from the project
    directory, "" the project's own; None when a name of `rel` itself is not there), and the symbolic links
    followed on the way [(path, text)]. Unsupported for library `name` when a link on the way leads nowhere or out
    of the project, has an absolute path, or climbs out of the project (module docstring, libraries), or a folder
    on the way is a git repository of its own."""
    real_d = os.path.realpath(d).rstrip(os.sep) + os.sep
    todo: list[tuple[str, tuple[str, str] | None]] = [(part, None) for part in Path(rel).parts]     # (name, its link)
    done: list[str] = []
    links: list[tuple[str, str]] = []
    while todo:
        part, via = todo.pop(0)
        if part == "..":
            if not done:
                raise _link(name, *(via or (rel, rel)), "climbs")
            done.pop()
            continue
        here = "/".join([*done, part])
        p = os.path.join(d, here)
        if os.path.islink(p):
            text = os.readlink(p)
            if not os.path.exists(p) or len(links) >= _HOPS:
                raise _link(name, here, text, "nowhere")
            if not (os.path.realpath(p) + os.sep).startswith(real_d):
                raise _link(name, here, text, "outside")
            if os.path.isabs(text):
                raise _link(name, here, text, "absolute")
            links.append((here, text))
            todo[:0] = [(x, (here, text)) for x in Path(text).parts]
            continue
        if not os.path.lexists(p):
            return None, links
        if os.path.lexists(os.path.join(p, ".git")):
            raise _repository(name, here)
        done.append(part)
    return "/".join(done), links


@dataclass(frozen=True)
class LibraryFolder:
    """A library check_libraries let through: its name, its folder as it leads (from the project directory; "" the
    project's own, None not there), the symbolic links followed to it [(path, text)]."""
    name: str
    folder: str | None
    links: tuple[tuple[str, str], ...]


def check_libraries(project_dir: str | Path) -> list[LibraryFolder]:
    """Unsupported when a library the project has in it (project.json) is one a worktree cannot hold as it is, as
    far as the folder tells (module docstring, libraries): a symbolic link at its folder, above it or on the way one
    of those leads, out of the project or to nothing, with an absolute path or climbing out of the project; a git
    repository of its own there. Needs no repository (before ensure_repo, nothing made). The libraries as they lead,
    for what the tree must keep (_check_library_trees)."""
    d = Path(project_dir)
    found = []
    for name, rel in sorted(_libraries(d).items()):
        folder, links = _follow(d, name, rel)
        found.append(LibraryFolder(name, folder, tuple(links)))
    return found


def _ls_tree(d: Path, tree: str, paths: Iterable[str]) -> list[tuple[str, str]]:
    """The tree's entries at `paths` (from the project directory) and under them: [(mode, path)]."""
    listed = _git(d, "--literal-pathspecs", "ls-tree", "-r", "-z", tree, "--", *paths)
    return [(meta.split(" ", 1)[0], path) for meta, _, path in
            (e.partition("\t") for e in listed.decode("utf-8", "surrogateescape").split("\0") if e)]


def _check_library_trees(d: Path, tree: str, libraries: list[LibraryFolder]) -> None:
    """Unsupported when the tree has, in a library's folder, a gitlink (a git repository of its own) or a symbolic
    link check_libraries would refuse; or does not keep what a library's link leads to: the link itself, or where
    it leads (ignored, Undine's own output), which the worktree would not have."""
    folders = {lib.folder: lib.name for lib in libraries if lib.folder}
    # (library, the link to blame, its text, a path the tree must have: the link's own entry, or what it leads to)
    need: list[tuple[str, str, str, str, bool]] = []
    for lib in libraries:
        need += [(lib.name, path, text, path, True) for path, text in lib.links]
        if lib.links and lib.folder:
            need.append((lib.name, *lib.links[0], lib.folder, False))
    for mode, path in _ls_tree(d, tree, folders) if folders else []:
        if mode not in ("120000", "160000"):
            continue
        name = next((n for f, n in folders.items() if path == f or path.startswith(f + "/")), path)
        if mode == "160000":
            raise _repository(name, path)
        to, links = _follow(d, name, path)
        need += [(name, link, text, link, True) for link, text in links[1:]]
        if links and to:
            need.append((name, *links[0], to, False))
    if not need:
        return
    entries = _ls_tree(d, tree, sorted({path for *_, path, _ in need}))
    kept_links = {path for mode, path in entries if mode == "120000"}
    kept = {"/".join(parts[:i]) for _, path in entries for parts in [path.split("/")] for i in range(1, len(parts) + 1)}
    for name, link, text, path, own in need:
        if own and path not in kept_links:
            raise _link(name, link, text, "unkept")
        if not own and path not in kept:
            raise _link(name, link, text, "ignored", path)


def _commit(d: Path, tree: str, parents: Iterable[str], message: str, actor: str) -> str:
    args = [a for p in parents for a in ("-p", p)]
    return _git(d, "commit-tree", "--no-gpg-sign", tree, *args, env=identity(actor),
                input=message.encode()).decode().strip()


def _subject(message: str) -> str:
    return f"undine: {(message.strip().splitlines() or [''])[0]}"


# ---------------------------------------------------------------- commits of the project and of worktrees


def snapshot(project_dir: str | Path, message: str, actor: str) -> str:
    """A commit of the project's working tree as it is (trees, module docstring), parent HEAD (none on an unborn
    branch); no ref points at it. Unsupported: a library the worktrees cannot hold (module docstring, libraries)."""
    d = Path(project_dir)
    libraries = check_libraries(d)
    with lock(d) as r, _index(r, r.head, r.git_dir / "index") as env:
        tree = _project_tree(d, env)
        _check_library_trees(d, tree, libraries)
        return _commit(d, tree, [r.head] if r.head else [], message, actor)


def first_commit(project_dir: str | Path, message: str, actor: str, journals: bool = False) -> dict | None:
    """The first commit of the user's branch, on an unborn HEAD only (None when HEAD has a commit, one of the user's
    made meanwhile too): the project's files as they are, the tree built as snapshot builds it (trees, module
    docstring: Undine's own output and the changeset records left out; Unsupported for a library the worktrees cannot
    hold) from the entries the user's index has of the project (what they track is kept, a file .gitignore ignores
    added with -f), no parent. Unsaved when a log in it holds unsaved layout edits, as the pre-commit hook refuses
    such a commit (commit-tree runs no hook), unless `journals` (the opening of a changeset's: nobody is there to
    save them, and its base has them so too). HEAD's branch is made at it only while it is still unborn
    (compare-and-swap: a commit of the user's made meanwhile wins), and the index entries of the project's directory
    are reset to it, Undine's output the user staged untracked again (_reset_index): `git status` in the project then
    shows nothing but what the tree leaves out. An unborn HEAD is never detached (git has no such state). In a
    repository whose project is a subdirectory, only the project's directory is committed: the rest of the working
    tree, and of the index, stays as it is. Returns {"commit", "note": None, or why the index was not reset}."""
    d = Path(project_dir)
    libraries = check_libraries(d)
    with lock(d) as r:
        if r.head is not None:
            return None
        with _index(r, None) as env:
            # the user's entries of the project, Undine's own output and the records left out (an index git cannot
            # read: none, as before a first `git add`); their stat data is not kept, every file is read
            tracked = _run(d, "ls-files", "-s", "-z", "--full-name", "--", ".", *EXCLUDES)
            if tracked.returncode == 0 and tracked.stdout:
                _git(r.top, "update-index", "-z", "--index-info", env=env, input=tracked.stdout)
            tree = _project_tree(d, env)
            _check_library_trees(d, tree, libraries)
        logs = [] if journals else _unsaved(d, tree)
        if logs:
            raise Unsaved("; ".join(f"library {x['lib']} has layout edits not saved to its cell files"
                                    + (f" (cells {', '.join(x['cells'])})" if x["cells"] else "") for x in logs)
                          + ": save the layouts in Undine, then make the project's first commit (Make the first "
                          "commit, in the History panel)", logs)
        sha = _commit(d, tree, [], message, actor)
        made = _run(d, "update-ref", "-m", _subject(message), "HEAD", sha, "")
        if made.returncode != 0:
            if _out(d, "rev-parse", "-q", "--verify", "HEAD^{commit}") is not None:
                return None                         # a commit of the user's, made meanwhile: it is the first
            err = made.stderr.decode(errors="replace").strip().splitlines()
            raise RuntimeError(f"git update-ref: {err[-1] if err else 'failed'}")
        return {"commit": sha, "note": _reset_index(d, sha)}


def _unsaved(d: Path, tree: str) -> list[dict]:
    """The logs in `tree` (history/<lib>.jsonl) holding unsaved layout edits, as gitmerge.precommit finds them in a
    commit with no parent (the copies a merge made here, merged_seq, not counted): [{"path" from the project
    directory, "lib", "cells"}]."""
    listed = _git(d, "--literal-pathspecs", "ls-tree", "-z", tree, "--", "history/").decode("utf-8", "surrogateescape")
    entries = [(meta.split(" "), path) for meta, _, path in (e.partition("\t") for e in listed.split("\0") if e)]
    out = []
    with gitmerge._Objects(d) as objs:
        for (_, kind, sha), path in entries:
            if kind != "blob" or gitmerge.kind_of(path) != "log":
                continue
            pending = [x for x in logmerge.journal(objs.get(sha) or b"") if "merged_seq" not in x]
            if pending:
                out.append({"path": path, "lib": os.path.basename(path)[:-len(".jsonl")],
                            "cells": sorted({str(x["cell"]) for x in pending if x.get("cell")})})
    return out


_TRIES = 20                         # _reset_index's, 0.1 s apart: another git's index.lock, held for a moment


def _reset_index(d: Path, sha: str, listed: bytes | None = None) -> str | None:
    """The user's index entries reset to commit `sha` (a mixed reset; the working tree is not touched): those of the
    paths `listed` (NUL-terminated, from the project directory), else all of the project directory's. Not quiet: a
    reset -q skips the refresh (git 2.34), leaving the entries it wrote with no stat data, so that every `git
    status` would read those files again until git next writes the index. Tried again while another git holds
    index.lock; None once done, else why not, and what to run (best effort: the commit is made)."""
    paths = ("--pathspec-from-file=-", "--pathspec-file-nul") if listed is not None else ("--", ".")
    for i in range(_TRIES):
        if i:
            time.sleep(0.1)
        r = _run(d, *(("--literal-pathspecs",) if listed is not None else ()), "reset", sha, *paths, input=listed)
        if r.returncode == 0:
            return None
        err = r.stderr.decode(errors="replace")
        if "index.lock" not in err:
            break
    last = (err.strip().splitlines() or ["failed"])[0]
    return (f"git's index was not reset to commit {sha[:12]} ({last.removeprefix('fatal: ')}): run `git reset` in "
            f"{d}, or `git status` shows its files as changed")


def first_message(name: str, setup: bool = False) -> str:
    """The message of the first commit of project `name` (first_commit): `setup`, made as Undine set up its
    repository (git init), else when the person asked for it (History)."""
    subject = f"Project {name}: its files when Undine set up its repository" if setup else \
        f"Project {name}: its first commit, its files as they were"
    when = "when Undine made it a git repository" if setup else "when its first commit was made"
    return (f"{subject}\n\nThe project's files as they were {when}, so that the changesets applied since are "
            "committed on top of them. Left out: Undine's own output (runs/, tapeout/, llm/, .undine/, *.pre-split), "
            "the changeset records (changesets/) and what .gitignore ignores, unless tracked.\n")


def commit_worktree(worktree_dir: str | Path, prefix: str, message: str, actor: str) -> str | None:
    """A commit of the worktree's project directory (`prefix` in it) onto the worktree's branch, parent the branch's
    tip; None when its tree is the tip's. The branch moves only from the tip read (compare-and-swap); the worktree's
    index follows (stat cache, module docstring; best effort: a stale one only makes its `git status` odd)."""
    w = Path(worktree_dir)
    d = w / prefix if prefix else w
    with lock(w) as r:
        if r.branch is None:
            raise ValueError(f"{w} is not on a branch")
        tip = r.head
        with _index(r, tip, r.git_dir / "index") as env:
            tree = _project_tree(d, env)
            if tip and tree == _git(w, "rev-parse", f"{tip}^{{tree}}").decode().strip():
                _install_index(r, env)          # its stat data newer: the next commit reads no file anew
                return None
            sha = _commit(w, tree, [tip] if tip else [], message, actor)
            _git(w, "update-ref", "-m", _subject(message), f"refs/heads/{r.branch}", sha, tip or "")
            _install_index(r, env)              # the new tip's entries
            return sha


def commit_merge(project_dir: str | Path, parents: list[str], message: str, actor: str, paths: Iterable[str]) -> str:
    """A commit on the user's branch (HEAD's, or a detached HEAD) whose tree is HEAD's with `paths` (from the
    project directory) as the working tree has them now (a missing file: deleted), parents as given, of which HEAD
    must be the first (it was read before: the branch never loses a commit made since); HEAD moves only from the
    value read. The index entries of those paths are reset to it (_reset_index, best effort); the working tree is
    not touched."""
    d = Path(project_dir)
    paths = list(paths)
    listed = b"".join(p.encode("utf-8", "surrogateescape") + b"\0" for p in paths)
    with lock(d) as r:
        parents = [_commit_of(d, p) for p in parents]
        if r.head and (not parents or parents[0] != r.head):
            raise ValueError(f"HEAD is {r.head[:12]} now, not {parents[0][:12] if parents else 'a parent'}: "
                             "it moved since")
        with _index(r, r.head) as env:
            if paths:
                _git(d, "update-index", "--add", "--remove", "-z", "--stdin", env=env, input=listed)
            tree = _git(d, "write-tree", env=env).decode().strip()
        sha = _commit(d, tree, parents, message, actor)
        _git(d, "update-ref", "-m", _subject(message), "HEAD", sha, r.head or "")
        if paths:       # the commit is made: an index the user's git holds on leaves those entries as they were
            _reset_index(d, sha, listed)
        return sha


def is_clean(project_dir: str | Path) -> bool:
    """Nothing staged in the repository (no merge, cherry-pick or rebase in progress either) and the project's
    working tree as HEAD has it, untracked files included; what git ignores, Undine's own output and the changeset
    records (staged or not) are not counted. An unborn branch is clean only when the project has nothing to commit."""
    d = Path(project_dir)
    with lock(d) as r:
        if history._operation(r):                   # type: ignore[arg-type]  (it reads git_dir)
            return False
        # anywhere in the repository (":/"), but for Undine's own output and the records of the project
        staged = _run(d, "--no-optional-locks", "diff", "--cached", "--quiet", "--", ":/", *EXCLUDES)
        if staged.returncode != 0:
            return False
        out = _git(d, "--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all",
                   "--no-renames", "--", ".", *EXCLUDES)
        return not out.strip(b"\0")


# ---------------------------------------------------------------- refs


def keep_ref(project_dir: str | Path, branch: str, ref: str) -> str:
    """Moves `branch` to `ref` (under refs/undine/; it must not exist) in one transaction: the branch is deleted,
    its commits kept from gc. Refuses while a worktree has the branch checked out (remove it first). Returns the
    commit; again for a branch already moved, the kept one."""
    if not ref.startswith("refs/undine/"):
        raise ValueError(f"a kept ref is under refs/undine/, not {ref}")
    d, full = Path(project_dir), _full(branch)
    with lock(d):
        _git(d, "worktree", "prune")                # a worktree deleted by hand has it checked out no more
        sha = _out(d, "rev-parse", "-q", "--verify", full)
        if sha is None:
            kept = _out(d, "rev-parse", "-q", "--verify", ref)
            if kept is None:
                raise ValueError(f"no branch {branch}")
            return kept
        if any(w["branch"] == full for w in _worktrees(d, main=True)):
            raise ValueError(f"{branch} is checked out (git checkout another branch, or remove its worktree)")
        _git(d, "update-ref", "-m", f"undine: keep {branch}", "--stdin",
             input=f"create {ref} {sha}\ndelete {full} {sha}\n".encode())
        return sha


# ---------------------------------------------------------------- worktrees


def _worktrees(d: Path, main: bool = False) -> list[dict]:
    """The linked worktrees (with `main`, the main one first): [{"path", "branch": full ref or None}]."""
    out = _git(d, "worktree", "list", "--porcelain").decode("utf-8", "surrogateescape")
    found = []
    for block in out.split("\n\n")[0 if main else 1:]:
        lines = dict(line.partition(" ")[::2] for line in block.splitlines() if line)
        if "worktree" in lines:
            found.append({"path": lines["worktree"], "branch": lines.get("branch") or None})
    return found


def _same(a: str | Path, b: str | Path) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _shadow(r: Repo, w: Path) -> Path | None:
    """The store's .changesets/ the worktree path `w` is in, when that is inside the repository's working tree."""
    top = Path(os.path.realpath(r.top))
    for p in Path(os.path.realpath(w)).parents:
        if p == top or top not in p.parents:
            return None
        if p.name == SHADOW:
            return p
    return None


def _exclude(r: Repo, path: Path, add: bool) -> None:
    """The directory's line in info/exclude added or removed, when it is inside the repository's working tree."""
    try:
        rel = Path(os.path.realpath(path)).relative_to(os.path.realpath(r.top))
    except ValueError:
        return                                      # outside: git never sees it
    if rel == Path("."):
        return
    line = "/" + "".join("\\" + c if c in "*?[\\" else c for c in rel.as_posix()) + "/"
    f = r.common_dir / "info" / "exclude"
    try:
        lines = f.read_text().splitlines() if f.is_file() else []
    except (OSError, UnicodeDecodeError):
        return                                      # not ours to rewrite
    new = [x for x in lines if x != line] + ([line] if add else [])
    if new != lines:
        f.parent.mkdir(parents=True, exist_ok=True)
        gitmerge._write(f, ("\n".join(new) + "\n" if new else "").encode())


def _ignore_shadow(w: Path) -> None:
    """The store's .changesets/ the worktree path `w` is in (the nearest folder of that name above it), made to
    ignore itself in whatever repository it is: its .gitignore, "*", written when it has none (module docstring)."""
    shadow = next((p for p in w.parents if p.name == SHADOW), None)
    if shadow is None:
        return
    shadow.mkdir(parents=True, exist_ok=True)
    try:
        with open(shadow / ".gitignore", "x") as f:
            f.write("*\n")
    except FileExistsError:
        pass                                        # the user's, or ours: as it is


def add_worktree(project_dir: str | Path, path: str | Path, commit: str, branch: str | None = None) -> Path:
    """A worktree at `path` of `commit`: on a new branch `branch` made at it, else detached. Returns the project
    directory in it (`path`/prefix)."""
    d, w = Path(project_dir), Path(path).absolute()
    with lock(d) as r:
        sha = _commit_of(d, commit)
        _ignore_shadow(w)                           # first, as the exclusion below
        shadow = _shadow(r, w)
        _exclude(r, shadow or w, add=True)          # first: the user's git never sees it unexcluded
        try:
            _git(d, "worktree", "add", "-q", *(["-b", branch] if branch else ["--detach"]), "--", str(w), sha)
        except BaseException:
            if shadow is None or not _in_shadow(d, shadow):
                _exclude(r, shadow or w, add=False)
            raise
        return w / r.prefix if r.prefix else w


def _in_shadow(d: Path, shadow: Path) -> bool:
    """Whether a worktree of the repository is in the store's .changesets/ (after a prune)."""
    return any(Path(os.path.realpath(x["path"])).is_relative_to(shadow) for x in _worktrees(d))


def remove_worktree(project_dir: str | Path, path: str | Path) -> None:
    """Removes the worktree at `path` (its files whatever their state, ignored ones too), then prunes: a path
    deleted by hand, or no worktree there any more, is no error. Never anything but a linked worktree."""
    d, w = Path(project_dir), Path(path).absolute()
    with lock(d) as r:
        if any(_same(x["path"], w) for x in _worktrees(d)):
            res = _run(d, "worktree", "remove", "--force", "--force", "--", str(w))
            if res.returncode != 0 and w.is_dir():      # one git will not remove (submodules): its files, then prune
                shutil.rmtree(w)
        _git(d, "worktree", "prune")
        _exclude(r, w, add=False)
        shadow = _shadow(r, w)
        if shadow is not None and not _in_shadow(d, shadow):
            _exclude(r, shadow, add=False)


def prune(project_dir: str | Path) -> None:
    """git worktree prune: the worktrees whose directory is gone are forgotten (their branches can move again)."""
    d = Path(project_dir)
    with lock(d):
        _git(d, "worktree", "prune")


# ---------------------------------------------------------------- the plan of an apply or a revert


def _state(data: bytes | None, base: bytes | None) -> str:
    return "deleted" if data is None else "added" if base is None else "changed"


def _whole(what: str, b: bytes | None, o: bytes | None, t: bytes | None, cid: str = "file") -> dict:
    """A conflict of a whole file (module docstring)."""
    return {"id": cid, "what": what, "base": None if b is None else "file", "ours": _state(o, b),
            "theirs": _state(t, b), "taken": "ours", "resolved": False}


def _lines(b: bytes | None, o: bytes, t: bytes) -> tuple[bytes, int]:
    """git merge-file of three versions: (the merged bytes, the number of conflicts)."""
    with tempfile.TemporaryDirectory(prefix="undine-merge-") as tmp:
        here = Path(tmp)
        for name, data in (("base", b or b""), ("ours", o), ("theirs", t)):
            (here / name).write_bytes(data)
        return gitmerge._merge_lines(here, "base", "ours", "theirs")


def _merge(kind: str | None, b: bytes | None, o: bytes | None,
           t: bytes | None) -> tuple[bool, bytes | None, list[dict], list[str]]:
    """One file of the plan: (write, the bytes to write (None: delete), conflicts, warnings)."""
    if o == t:
        return False, None, [], []
    if o == b:
        return True, t, [], []
    if o is None or t is None:
        side = "ours" if o is None else "theirs"
        return False, None, [_whole(f"deleted on {side}, changed on the other side", b, o, t)], []
    try:
        if kind is not None:
            res = gitmerge.merge(kind, b, o, t)
            if not res.clean:
                return False, None, [c.to_json() for c in res.open_conflicts], []
            data, warnings = res.data, list(res.warnings)
        else:
            data, n = _lines(b, o, t)
            if n:
                return False, None, [_whole(f"{n} place{'s' * (n != 1)} changed differently on both sides "
                                            "(merged by lines)", b, o, t, "lines")], []
            warnings = []
    except Exception as e:                          # an unreadable side, a binary file: nothing written
        return False, None, [_whole(f"cannot merge: {e.__class__.__name__}: {e}", b, o, t, "error")], []
    return data != o, data, [], warnings


def plan_merge(project_dir: str | Path, base: str, theirs: str, keep_added: bool = False) -> dict:
    """What merging commit `theirs` into the project's working tree, from commit `base`, writes (module docstring);
    nothing is written."""
    d = Path(project_dir)
    plan: dict = {"writes": {}, "conflicts": [], "warnings": [], "paths": [], "kept": []}
    with lock(d) as r:
        b, t = _commit_of(d, base), _commit_of(d, theirs)
        names = _git(d, "diff", "--name-only", "-z", "--no-renames", "--no-relative", b, t, "--", ".", *EXCLUDES)
        under = f"{r.prefix}/" if r.prefix else ""
        with gitmerge._Objects(r.top) as objs:
            for path in sorted(set(filter(None, names.decode("utf-8", "surrogateescape").split("\0")))):
                rel = path[len(under):]
                bb, tt = objs.get(f"{b}:{path}"), objs.get(f"{t}:{path}")
                if bb == tt:                        # its mode alone changed
                    continue
                if keep_added and bb is not None and tt is None:
                    plan["kept"].append(rel)
                    continue
                f = d / rel
                oo = f.read_bytes() if f.is_file() else None
                plan["paths"].append(rel)
                kind = gitmerge.kind_of(rel)
                if oo is None and f.exists():       # a directory where the file was
                    conflicts, warnings = [_whole("not a file in the project", bb, oo, tt)], []
                    write, data = False, None
                else:
                    write, data, conflicts, warnings = _merge(kind, bb, oo, tt)
                if conflicts:
                    plan["conflicts"].append({"path": rel, "kind": kind or "text", "conflicts": conflicts})
                elif write:
                    plan["writes"][rel] = data
                plan["warnings"] += [{"path": rel, "warning": w} for w in warnings]
    return plan


def _replace(src: Path, dst: Path) -> None:
    """A rename of write_plan's (tests make it fail)."""
    os.replace(src, dst)


def _inside(d: Path, rel: str) -> Path:
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise ValueError(f"not a path inside the project: {rel}")
    return d / p


def write_plan(project_dir: str | Path, plan: dict) -> list[str]:
    """Writes a plan without conflicts all or nothing (module docstring): the paths written or deleted."""
    if plan.get("conflicts"):
        raise ValueError("the merge has conflicts: nothing is written")
    d = Path(project_dir)
    writes = sorted(plan.get("writes", {}).items())
    token = uuid.uuid4().hex[:12]
    staged: list[tuple[Path, Path | None]] = []         # target, its new file (None: deleted)
    made: list[Path] = []                               # directories made for new files, outermost first
    moved: list[tuple[Path, Path | None, Path | None]] = []     # target, where it was moved, its new file
    with lock(d):
        try:
            for rel, data in writes:
                f = _inside(d, rel)
                if data is None:
                    staged.append((f, None))
                    continue
                missing = [p for p in [f.parent, *f.parent.parents] if not p.exists()]
                for p in reversed(missing):
                    p.mkdir()
                    made.append(p)
                tmp = f.with_name(f".{f.name}.{token}.new")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)    # the umask's mode, as git's
                staged.append((f, tmp))
                with os.fdopen(fd, "wb") as out:
                    out.write(data)
                if f.is_file():
                    shutil.copymode(f, tmp)
            for f, tmp in staged:
                old = None
                if f.exists() or f.is_symlink():
                    old = f.with_name(f".{f.name}.{token}.old")
                    _replace(f, old)
                moved.append((f, old, tmp))
                if tmp is not None:
                    _replace(tmp, f)
        except BaseException:
            for f, old, tmp in reversed(moved):         # os.replace itself: _replace may be what failed
                if tmp is not None and not tmp.exists():
                    f.unlink(missing_ok=True)           # the new file was renamed in
                if old is not None:
                    os.replace(old, f)
            for _, tmp in staged:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
            for p in reversed(made):
                try:
                    p.rmdir()
                except OSError:
                    pass
            raise
        for _, old, _ in moved:
            if old is not None:
                old.unlink(missing_ok=True)
    return [rel for rel, _ in writes]


# ---------------------------------------------------------------- the semantic diff of a base


def diff(project_dir: str | Path, base: str, against: str = "worktree", lib: str | None = None,
         cell: str | None = None) -> dict:
    """The semantic diff from commit `base` to the working tree of the project directory (a worktree's: the
    changeset's working copy), or to commit `against`, in /git/diff's shape (history.diff); the changeset records
    left out. Takes no lock (module docstring)."""
    out = history.diff(project_dir, against, lib, cell, base=base)
    out["other"] = [o for o in out["other"] if o["path"].split("/", 1)[0] != RECORDS]
    return out
