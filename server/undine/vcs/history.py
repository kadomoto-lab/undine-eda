"""A project's git history for the UI (docs/reference/api.md, Git): the state of the working tree, the commits, the
semantic diff of a commit or of the working tree, and the check of a merge commit. git stores the history and gitmerge
diffs and checks (its _Repo, _Objects, diff_revs, merge_check); this module says which cell each file is.

Paths are from the project directory (git's, and gitmerge's, are from the repository's top: a project may be a
subdirectory of its repository, `prefix`). A file is a view of a cell when it is directly in the directory of one of the
project's libraries (project.json, a relative "path" inside the project, as gitmerge._libraries reads it; a directory
libs/<lib>/ that project.json does not name is library <lib>, where create_library puts one, so that a library removed
since still shows in old commits), is not hidden, and is named

    <cell>.sch  schematic       <cell>.netlist.yaml  netlist        <cell>.sv, <cell>.v  rtl
    <cell>.sym  symbol          <cell>.intent.yaml   intent
    <cell>.oas  layout (the name %-decoded, layoutstore.cell_name)

Every other file of the project (project.json, undine.lock, history/<lib>.jsonl, .gitattributes, a file in a
library's subdirectory) is "other"; status and diff list at most MAX_OTHER of them (the first by path). Undine's own
output (gitmerge.GITIGNORE: runs/, tapeout/, llm/, .undine/, *.pre-split, as git-setup's .gitignore has them) is never
listed, whether or not the repository ignores it (a plain `git init`, a repository holding several projects): git
gets it as exclude pathspecs, so that a status poll does not walk thousands of job files, and what a commit has of it
anyway is left out. The libraries are the working tree's project.json (HEAD's while that one cannot be read: a conflict
in it), also for old commits.

A file's status, against HEAD (or a commit's, against its first parent): added, deleted, modified (a type change too),
untracked (not ignored), conflicted (unmerged in the index, whatever the sides did). No renames (--no-renames
everywhere): git pairs only staged files in the working tree, and only files inside the pathspec in a log, so a renamed
cell would be "renamed" in one place and deleted plus added in another; it is deleted plus added everywhere, the old
cell listed too, and "renamed" never appears.

status   git status --porcelain=v1 -z --untracked-files=all of the project directory: the index and the working tree
         against HEAD (--no-optional-locks: a poll never takes the index lock from the user's own git). A path both
         deleted from the index and untracked is deleted (what a commit of the index does); added to the index and
         deleted from the working tree, it is no change. Also the branch (None: detached HEAD), HEAD's commit (None: no
         commit yet), the operation in progress (rebase while rebase-merge/ or rebase-apply/rebasing exists, else merge
         with MERGE_HEAD, cherry-pick with CHERRY_PICK_HEAD; git am and revert are None), and what git-setup set up in
         this clone: merge, the merge driver is configured, its program exists and a .sch file has merge=undine; diff,
         the same for the layout's textconv and a .oas file; hook, the pre-commit hook git runs (core.hooksPath's too)
         is executable and has a line that runs `undine precommit`; gitignore, the lines of gitmerge.GITIGNORE the
         project directory's .gitignore lacks (a project set up before a line was added there: git then shows that
         output, the request log of the changesets, as untracked files), which git-setup adds and nothing else does.
log      git log --name-status --diff-merges=first-parent of HEAD, the project directory as the pathspec (with lib and
         cell: that cell's view files, and --full-diff, so that a commit still lists everything it changed in the
         project), newest first. git's history simplification applies: a merge that took the project's files from one
         parent whole is left out (it has nothing to verify). --no-follow: log.follow would follow one path only.
         `limit` + 1 commits are read after `skip`: whether there is a next page is known without reading further.
diff     the project's files that changed (status's for the working tree; git diff --name-status for a commit, against
         its first parent, the empty tree for a root commit), with gitmerge.diff_revs of the cells' view files only (the
         diff of an operation log, which nobody shows, is not computed). With a `base` commit (a changeset's), against
         it instead: a commit, or the working tree, whose untracked files are added, or modified where the base has
         them (a base made from a working tree has files the index has not), or left out when as the base has them.
verify   gitmerge.merge_check of a merge commit, or without one of the merge, cherry-pick or rebase in progress (its
         open conflicts with both values). Its paths are merge-check's, from the repository's top (as `undine
         resolve` and git take them), not from the project directory.

Names git gives as bytes that are not UTF-8 are read with surrogateescape (so the right file is opened) and shown with
backslash escapes (JSON has no lone surrogates): byte 0xe9 as the four characters \\xe9. A lib and cell given back so
(log and diff of one cell) name the file of those bytes, and the cell of that very text too where both exist.
"""
from __future__ import annotations

import fnmatch
import json
import os
import posixpath
import re
import shlex
import shutil
from pathlib import Path
from typing import Iterable

from undine.design import rtl
from undine.layouts import layoutstore
from undine.vcs import gitmerge, yamlmerge

VIEWS = ("schematic", "symbol", "layout", "netlist", "intent", "rtl", "other")    # a cell's views in this order
_SUFFIXES = [("schematic", ".sch"), ("symbol", ".sym"), ("layout", layoutstore.SUFFIX),
             ("netlist", yamlmerge.SUFFIXES["netlist"]), ("intent", yamlmerge.SUFFIXES["intent"]),
             *(("rtl", s) for s in rtl.SUFFIXES)]
_UNMERGED = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}       # git status's XY of a conflicted path
_LETTERS = {b"A": "added", b"D": "deleted"}                  # --name-status; the others (M, T) are modifications
# one commit: \x1e, then its fields separated by NUL (a commit message has no NUL); --name-status follows
_FORMAT = "%x1e" + "%x00".join(("%H", "%h", "%P", "%an", "%ae", "%aI", "%D", "%s", "%b"))
_FIELDS = 9
MAX_LIMIT = 500
MAX_OTHER = 500                 # "other" files a status or a diff lists
_ESCAPED = re.compile(r"\\x([89a-f][0-9a-f])")      # _text's escape of a byte that is not UTF-8 (0x80 and up)


def _text(s: str) -> str:
    """A name as JSON can carry it: bytes that are not UTF-8 (surrogateescape) as backslash escapes."""
    return s.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")


def _untext(s: str) -> str:
    """The name _text was given: its backslash escapes back to the bytes (surrogateescape) they stand for."""
    return _ESCAPED.sub(lambda m: chr(0xdc00 + int(m[1], 16)), s)


def _texts(v):
    """_text of every string in a JSON value (merge-check's paths and cells, a diff's values)."""
    if isinstance(v, str):
        return _text(v)
    if isinstance(v, dict):
        return {_texts(k): _texts(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_texts(x) for x in v]
    return v


def _own(rel: str) -> bool:
    """Undine's own output (gitmerge.GITIGNORE, matched as .gitignore matches it), a path from the project directory."""
    parts = rel.split("/")
    for pat in gitmerge.GITIGNORE:
        core, is_dir = pat.strip("/"), pat.endswith("/")
        if pat.startswith("/"):                     # from the project directory
            n = core.count("/") + 1
            if fnmatch.fnmatchcase("/".join(parts[:n]), core) and (len(parts) > n or not is_dir):
                return True
        elif any(fnmatch.fnmatchcase(x, core) for x in (parts[:-1] if is_dir else parts)):     # at any depth
            return True
    return False


def _excludes() -> list[str]:
    """Undine's own output as exclude pathspecs (from the project directory: git runs there)."""
    out = []
    for pat in gitmerge.GITIGNORE:
        core = pat.strip("/")
        glob = core if pat.startswith("/") else f"**/{core}"
        out += [f":(exclude,glob){glob}/**"] if pat.endswith("/") else \
            [f":(exclude,glob){glob}", f":(exclude,glob){glob}/**"]
    return out


def _layout_file(cell: str) -> str:
    """A cell's layout file name (layoutstore.file_name); a name with bytes that are not UTF-8 as it is, which place()
    reads back to the same cell (%-encoding needs text)."""
    try:
        return layoutstore.file_name(cell)
    except UnicodeEncodeError:
        return cell + layoutstore.SUFFIX


def _literal(path: str) -> str:
    """A pathspec of exactly this path (a cell name may have * or ? in it)."""
    return f":(literal){path}"


class _Project:
    """A project directory in its repository: which library, cell and view each path is."""

    def __init__(self, d: Path, repo: gitmerge._Repo):
        self.dir, self.repo = d, repo
        self.under = f"{repo.prefix}/" if repo.prefix else ""     # the project directory from the top
        try:
            self.libs = gitmerge._libraries(self._meta(), "")       # name -> directory from the project directory
        except (AttributeError, TypeError):                         # "libraries" is no mapping
            self.libs = {}
        self.lib_of = {v: k for k, v in self.libs.items()}

    def _meta(self) -> dict:
        try:
            meta = json.loads((self.dir / "project.json").read_bytes())
        except (OSError, ValueError):                               # a conflict in it: HEAD's
            with gitmerge._Objects(self.repo.top) as objs:
                data = objs.get(f"HEAD:{self.under}project.json")
            try:
                meta = json.loads(data) if data else {}
            except ValueError:
                meta = {}
        return meta if isinstance(meta, dict) else {}

    def rel(self, path: str) -> str | None:
        """A path from the repository's top as one from the project directory; None outside it."""
        return path[len(self.under):] if path.startswith(self.under) else None

    def listed(self, path: str) -> str | None:
        """rel() of a path the history lists: None outside the project directory and for Undine's own output."""
        rel = self.rel(path)
        return None if rel is None or _own(rel) else rel

    def place(self, rel: str) -> tuple[str, str, str] | None:
        """(library, cell, view) of a file from the project directory; None for a file of no cell."""
        d, name = posixpath.split(rel)
        lib = self.lib_of.get(d)
        if lib is None:
            top, _, sub = d.partition("/")
            lib = sub if top == "libs" and sub and "/" not in sub and sub not in self.libs else None
        if lib is None or name.startswith("."):
            return None
        for view, suffix in _SUFFIXES:
            if name.endswith(suffix) and len(name) > len(suffix):
                return lib, layoutstore.cell_name(Path(name)) if view == "layout" else name[:-len(suffix)], view
        return None

    def cell_paths(self, lib: str, cell: str) -> list[str]:
        """The files a cell's views can be, from the project directory: of lib and cell as they are, and as the names
        _text escaped (module docstring)."""
        for x in (lib, cell):
            if not x or "/" in x or "\0" in x or x in (".", ".."):
                raise ValueError(f"not a library or cell name: {x!r}")
        out: list[str] = []
        for lib_, cell_ in dict.fromkeys([(lib, cell), (_untext(lib), _untext(cell))]):
            d = self.libs.get(lib_)
            if d is None:
                d = f"libs/{lib_}"
                if d in self.lib_of:                                # another library's directory
                    if lib_ == lib:
                        raise ValueError(f"no library {lib}")
                    continue
            out += [posixpath.join(d, _layout_file(cell_) if view == "layout" else cell_ + suffix)
                    for view, suffix in _SUFFIXES]
        return out

    def pathspecs(self, lib: str | None, cell: str | None) -> list[str]:
        """The project directory but Undine's own output, or with lib and cell that cell's files."""
        if (lib is None) != (cell is None):
            raise ValueError("lib and cell go together")
        return [".", *_excludes()] if lib is None or cell is None else [_literal(p) for p in self.cell_paths(lib, cell)]

    def maybe(self, *args: str) -> str | None:
        """git's output in the project directory; None when git fails (no such ref, no such setting)."""
        try:
            return gitmerge._git(self.dir, *args).decode("utf-8", "surrogateescape").strip() or None
        except RuntimeError:
            return None

    def head(self) -> str | None:
        return self.maybe("rev-parse", "-q", "--verify", "HEAD^{commit}")


def _open(project_dir: str | Path) -> _Project | None:
    d = Path(project_dir)
    try:
        repo = gitmerge._Repo(d)
    except (RuntimeError, OSError):                 # not in a repository (or no git): a state, not an error here
        return None
    return _Project(d, repo)


def _project(project_dir: str | Path) -> _Project:
    p = _open(project_dir)
    if p is None:
        raise ValueError(f"{Path(project_dir).name} is not in a git repository (set it up first)")
    return p


# ---------------------------------------------------------------- files and their statuses


def _status(xy: str) -> str | None:
    """A file's status of git status's XY (index, working tree); None: no change against HEAD."""
    if xy in _UNMERGED:
        return "conflicted"
    if xy == "??":
        return "untracked"
    if xy[0] == "A":
        return None if xy[1] == "D" else "added"
    if xy == "!!":
        return None
    return "deleted" if "D" in xy else "modified"


def _worktree(p: _Project, paths: list[str]) -> list[tuple[str, str]]:
    """(path from the top, status) of the files of `paths` whose index or working tree differs from HEAD."""
    out = gitmerge._git(p.dir, "--no-optional-locks", "status", "--porcelain=v1", "-z", "--untracked-files=all",
                        "--no-renames", "--", *paths)
    seen: dict[str, str] = {}
    for entry in out.decode("utf-8", "surrogateescape").split("\0"):
        if len(entry) > 3 and (st := _status(entry[:2])) is not None:
            seen.setdefault(entry[3:], st)            # deleted from the index first, then untracked: deleted
    return list(seen.items())


def _between(p: _Project, a: str, b: str | None, paths: list[str]) -> list[tuple[str, str]]:
    """(path from the top, status) of the files of `paths` that differ from tree `a` to commit `b`, or to the working
    tree (None; its untracked files too, module docstring)."""
    out = gitmerge._git(p.dir, "--no-optional-locks", "diff", "--name-status", "-z", "--no-renames", "--no-relative",
                        a, *([b] if b else []), "--", *paths)
    tokens = out.split(b"\0")
    found = {tokens[i + 1].decode("utf-8", "surrogateescape"): _LETTERS.get(tokens[i][:1], "modified")
             for i in range(0, len(tokens) - 1, 2) if tokens[i]}
    if b is None:
        new = gitmerge._git(p.dir, "--no-optional-locks", "ls-files", "-z", "--others", "--exclude-standard",
                            "--full-name", "--", *paths).decode("utf-8", "surrogateescape").split("\0")
        # a file `a` has and the index has not is "deleted" for git diff
        had = [path for path in new if path and found.get(path) == "deleted"]
        found.update({path: "added" for path in new if path and path not in found})
        if had:
            with gitmerge._Objects(p.repo.top) as objs:
                for path in had:
                    f = p.repo.top / path
                    if objs.get(f"{a}:{path}") == (f.read_bytes() if f.is_file() else None):
                        del found[path]
                    else:
                        found[path] = "modified"
    return list(found.items())


def _group(p: _Project, entries: Iterable[tuple[str, str]], diffs: dict[str, dict] | None = None,
           stamped: bool = False) -> tuple[list[dict], list[dict]]:
    """The cells and the other files of the project among (path from the top, status) pairs; with `diffs` (diff_revs'),
    each view with its diff; `stamped` (the working tree), each view with its file's "stamp" (mtime and size: a shown
    diff is read again when it moves, also while the status stays the same)."""
    cells: dict[tuple[str, str], list[dict]] = {}
    other = []
    for path, st in entries:
        rel = p.listed(path)
        if rel is None:
            continue
        at = p.place(rel)
        if at is None:
            other.append({"path": _text(rel), "status": st})
            continue
        view: dict = {"view": at[2], "path": _text(rel), "status": st}
        if stamped:
            try:
                f = p.dir / rel
                view["stamp"] = f"{f.stat().st_mtime_ns}:{f.stat().st_size}"
            except OSError:
                view["stamp"] = None
        if diffs is not None:                       # none: the same bytes on both sides (untracked, as HEAD has it)
            view["diff"] = _texts(diffs.get(path) or {"kind": gitmerge.kind_of(path) or "text", "changes": []})
        cells.setdefault((at[0], at[1]), []).append(view)
    return ([{"lib": _text(lib), "cell": _text(cell),
              "views": sorted(vs, key=lambda v: (VIEWS.index(v["view"]), v["path"]))}
             for (lib, cell), vs in sorted(cells.items())],
            sorted(other, key=lambda o: o["path"]))


# ---------------------------------------------------------------- commits


def _refs(decoration: str) -> list[str]:
    """The branch and tag names of %D ("HEAD -> main, tag: v1, origin/main"); HEAD itself is none."""
    names = (r.removeprefix("HEAD -> ").removeprefix("tag: ") for r in decoration.split(", "))
    return [r for r in names if r and r != "HEAD"]


def _log(p: _Project, *args: str, files: bool = True) -> list[dict]:
    """The commits `git log ARGS` lists, as Commit records; with `files`, each with "files": [(path from the top,
    status)] against its first parent."""
    out = gitmerge._git(p.dir, "log", f"--format={_FORMAT}", "-z", "--no-color", "--no-show-signature", "--no-follow",
                        *(["--name-status", "--no-renames", "--no-relative", "--diff-merges=first-parent"]
                          if files else []), *args)
    tokens = out.split(b"\0")
    commits, i = [], 0
    while i < len(tokens):
        if not tokens[i].startswith(b"\x1e"):
            i += 1
            continue
        fields = [tokens[i][1:], *tokens[i + 1:i + _FIELDS]]
        i += _FIELDS
        if len(fields) < _FIELDS:
            break
        sha, short, parents, author, email, date, refs, subject, body = (f.decode("utf-8", "replace") for f in fields)
        commit: dict = {"sha": sha, "short": short, "parents": parents.split(), "subject": subject,
                        "body": body.rstrip(), "author": author, "email": email, "date": date, "refs": _refs(refs)}
        changes = []
        # status, path pairs up to the next commit; git starts them with a newline
        while i + 1 < len(tokens) and tokens[i].strip() and not tokens[i].startswith(b"\x1e"):
            changes.append((tokens[i + 1].decode("utf-8", "surrogateescape"),
                            _LETTERS.get(tokens[i].strip()[:1], "modified")))
            i += 2
        if files:
            commit["files"] = changes
        commits.append(commit)
    return commits


def _operation(repo: gitmerge._Repo) -> str | None:
    """The operation in progress: rebase, merge, cherry-pick or None (REBASE_HEAD alone is no rebase: git leaves it
    behind a finished one; rebase-apply/ without `rebasing` is git am)."""
    g = repo.git_dir
    if (g / "rebase-merge").is_dir() or (g / "rebase-apply" / "rebasing").exists():
        return "rebase"
    return next((op for f, op in gitmerge._HEADS if op != "rebase" and (g / f).is_file()), None)


def _setup(p: _Project) -> dict:
    """What git-setup set up in this clone (module docstring)."""
    config = dict(line.partition(" ")[::2] for line in (p.maybe(
        "config", "--get-regexp", r"^(merge\.undine\.driver|diff\.undine-layout\.textconv)$") or "").splitlines())
    attrs = {}
    for line in (p.maybe("check-attr", "merge", "diff", "--", "a.sch", "a.oas") or "").splitlines():
        path, attr, value = (line.split(": ", 2) + ["", ""])[:3]
        attrs[path, attr] = value

    def runs(key: str) -> bool:
        try:
            prog = shlex.split(config.get(key, ""))[:1]
        except ValueError:
            return False
        return bool(prog) and shutil.which(prog[0]) is not None
    try:
        have = set((p.dir / ".gitignore").read_text(errors="replace").splitlines())
    except OSError:
        have = set()
    return {"merge": runs("merge.undine.driver") and attrs.get(("a.sch", "merge")) == "undine",
            "diff": runs("diff.undine-layout.textconv") and attrs.get(("a.oas", "diff")) == "undine-layout",
            "hook": _hooked(p), "gitignore": [x for x in gitmerge.GITIGNORE if x not in have]}


def _hooked(p: _Project) -> bool:
    from undine.__main__ import _RUNS_PRECOMMIT     # what git-setup's hook runs

    rel = p.maybe("rev-parse", "--git-path", "hooks/pre-commit")    # core.hooksPath's when it is set
    hook = p.dir / rel if rel else None
    try:
        if hook is None or not os.access(hook, os.X_OK):
            return False
        lines = hook.read_text(errors="replace").splitlines()
    except OSError:
        return False
    return any(not line.lstrip().startswith("#") and _RUNS_PRECOMMIT.search(line) for line in lines)


# ---------------------------------------------------------------- the API's answers


def no_repo(preview: bool = False) -> dict:
    """The status of a project that is in no repository (or a changeset's working copy, `preview`)."""
    return {"repo": False, "preview": preview, "setup": None, "prefix": None, "branch": None, "head": None,
            "operation": None, "conflicted": [], "cells": [], "other": [], "unsaved": [], "stale": []}


def status(project_dir: str | Path) -> dict:
    """The project's working tree and index against HEAD, the branch, HEAD's commit, the operation in progress and
    the setup (module docstring). "unsaved" and "stale" are the server's to fill in (open layouts)."""
    p = _open(project_dir)
    if p is None:
        return no_repo()
    head = p.head()
    entries = _worktree(p, p.pathspecs(None, None))
    cells, other = _group(p, entries, stamped=True)
    branch = p.maybe("symbolic-ref", "-q", "--short", "HEAD")      # fails on a detached HEAD
    return {"repo": True, "preview": False, "setup": _setup(p), "prefix": _text(p.under),
            "branch": _text(branch) if branch else None,
            "head": _log(p, "-1", head, "--", files=False)[0] if head else None, "operation": _operation(p.repo),
            "conflicted": sorted(_text(r) for path, st in entries if st == "conflicted" and (r := p.listed(path))),
            "cells": cells, "other": other[:MAX_OTHER], "unsaved": [], "stale": []}


def log(project_dir: str | Path, limit: int = 50, skip: int = 0, lib: str | None = None,
        cell: str | None = None) -> dict:
    """{"commits": the commits of HEAD that changed the project (with lib and cell: that cell's files), newest first,
    from `skip` on, at most `limit`, each with its "cells" and the number of its "other" files against its first
    parent; "more": there are more}."""
    p = _project(project_dir)
    if not 1 <= limit <= MAX_LIMIT or skip < 0:
        raise ValueError(f"limit is 1 to {MAX_LIMIT}, skip 0 or more")
    paths = p.pathspecs(lib, cell)
    head = p.head()
    if head is None:                                # no commit yet
        return {"commits": [], "more": False}
    commits = _log(p, f"--skip={skip}", f"--max-count={limit + 1}", *(["--full-diff"] if lib else []), head, "--",
                   *paths)
    for c in commits:
        c["cells"], other = _group(p, c.pop("files"))
        c["other"] = len(other)
    return {"commits": commits[:limit], "more": len(commits) > limit}


def diff(project_dir: str | Path, rev: str, lib: str | None = None, cell: str | None = None,
         base: str | None = None) -> dict:
    """The semantic diff of commit `rev` against its first parent, or of the working tree ("worktree") against HEAD
    (module docstring): {"base", "rev": commits (None: the empty tree, the working tree), "cells": [{"lib", "cell",
    "views": [{"view", "path", "status", "diff"}]}], "other": [{"path", "status"}]}; with lib and cell, that cell's
    files only; with `base`, against that commit."""
    p = _project(project_dir)
    paths = p.pathspecs(lib, cell)
    if base is not None:
        base = p.repo.commit(base)
        new = None if rev == "worktree" else p.repo.commit(rev)
        entries = _between(p, base, new, paths)
    elif rev == "worktree":
        base, new = p.head(), None
        entries = _worktree(p, paths)
    else:
        new = p.repo.commit(rev)
        base = next(iter(gitmerge._git(p.dir, "rev-list", "--parents", "-n", "1", new).decode().split()[1:]), None)
        entries = _between(p, base or p.repo.empty_tree(), new, paths)
    views = [r for path, _ in entries if (r := p.listed(path)) is not None and p.place(r) is not None]
    diffs = gitmerge.diff_revs(p.dir, base, new, [_literal(r) for r in views])
    cells, other = _group(p, entries, diffs)
    return {"base": base, "rev": new, "cells": cells, "other": other[:MAX_OTHER]}


def verify(project_dir: str | Path, rev: str | None = None) -> dict:
    """gitmerge.merge_check of merge commit `rev` (None: of the operation in progress): the cells to verify, warnings,
    notes, files without a report, and in progress the conflicts still open; paths from the repository's top."""
    return _texts(gitmerge.merge_check(_project(project_dir).dir, commit=rev))
