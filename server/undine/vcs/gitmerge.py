"""Git integration of the semantic merge (docs/design/merge.md): the merge driver git runs for a cell's files, resolving
the conflicts it leaves, the check after a merge, the pre-commit check of the operation logs, and the semantic diff of
two revisions.

`undine git-setup` registers the drivers and installs the hook:

    .gitattributes   *.sch *.sym *.oas *.netlist.yaml *.intent.yaml history/*.jsonl  merge=undine
    .gitignore       GITIGNORE: Undine's own output (job results, tapeout bundles, the assistant's log, the installed
                     dependencies, a legacy layout.oas a split left, changeset evidence and the request log), which
                     history.py never lists either
    .git/config      merge.undine.driver = <python> -P -m undine merge-driver %O %A %B %P
                     merge.undine.recursive = undine-base
                     merge.undine-base.driver = <python> -P -m undine merge-driver --virtual-base %O %A %B %P
    .git/hooks/pre-commit                  <python> -P -m undine precommit

(git runs them in the repository's top directory, which `-m` would put first on the module path: a committed yaml.py
would run instead of PyYAML. -P leaves it out; Python 3.10 has no -P, so there <python> -P -m undine is
`<python> -c 'import sys; del sys.path[0]; ...runpy.run_module("undine", ...)'`: -I would also drop the user's
site-packages and PYTHONPATH, where Undine may be installed.)

driver       git runs it for a file both sides changed (only then; a file one side changed is taken from that side).
             kind_of(%P) picks the view's merge (schmerge, layoutmerge, yamlmerge, logmerge); base %O is an empty file
             when there is no common ancestor (no base). The merged file replaces ours %A by a rename, so on any error
             %A stays ours whole. Exit 0: merged; 1: conflicts open (the file has ours' value for each) or an error, and
             git marks the file conflicted. It prints one line (stderr): what it merged, or the conflicts left open with
             the commands to go on (merge-check, resolve); with one open conflict, that conflict's own words ("what"),
             and "ours kept" except for the log, whose layout-journal conflict says what its file has (the merged log
             drops the unsaved layout edits). %A is written first, then the report, twice: to
             <git dir>/undine-merge/<path>.<key>.json, this merge's own (key: _Repo.report_key of its blobs, so a
             later merge of the file, which the operation log has almost every time, keeps it), and to
             <git dir>/undine-merge/<path>.json, the file's latest:
                 {"path", "kind", "clean", "conflicts", "warnings", "combined",      merge3.Result.to_json
                  "prefer": {conflict id: "ours" | "theirs"},                        the decisions taken (resolve)
                  "blobs": {"base", "ours", "theirs"}}                               git object ids of the inputs
             (base None: %O was empty). When the merge fails, the report (best effort) says so, %A being ours whole:
                 {"path", "kind", "error": "<Type>: <message>", "clean": false, "conflicts": [], "warnings": [],
                  "combined": [], "prefer": {}, "blobs"}
             Reports are local to the clone that merged (git never transfers them).
             A file of no view (an attribute set by hand) gets git's line merge (git merge-file).
virtual base A criss-cross merge has several merge bases, and git first merges them into one, the virtual merge base,
             with the driver merge.undine.recursive names (without it, merge.undine.driver itself, whose exit code git
             ignores there). Two branches that resolved one conflict differently would then merge silently: with ours
             kept the virtual base is one branch's resolution, which the merge sees that branch keep and the other
             change, so it takes the other's. `merge-driver --virtual-base` merges as usual with each conflict taking
             the merge bases' own base %O's value (the whole %O when a view cannot, or the merge fails): the merge
             then sees both branches' changes against it and reports the conflict. Exit 0 either way, no report
             (_virtual_base).
resolve      merges the index's stages 1 / 2 / 3 of a conflicted file again, taking the named side for the named
             conflicts (added to the decisions in the report of these stages), writes the file, then the report (under
             both names). The path is relative to the current directory, else to the repository's top (as the driver
             prints it); ":/path" is from the top. `git add` marks it resolved, as usual.
merge_check  during a merge, cherry-pick or rebase (theirs: the commits the first of MERGE_HEAD, CHERRY_PICK_HEAD,
             REBASE_HEAD names, the last only while rebase-merge/ or rebase-apply/ exists: git leaves it behind a
             finished rebase; the merged tree is the working tree), or for a merge commit C (ours C^1, theirs C^2,
             merged C): the conflicted files with the conflicts still open (or the error the driver failed with), the
             warnings of every report of the merge, and the cells to verify (below). An octopus merge has more sides:
             every parent of the commit, or HEAD and every commit MERGE_HEAD names (one per line; "parents" in the
             result, "theirs" the first after ours) while git's octopus strategy stopped at a conflict of its last
             branch; the result says that the files changed on more than one branch were merged by lines ("notes":
             always in progress, for a commit when such files have no report).
             For a merge commit also the view files of the project it combined (the blob is no parent's) that have no
             report here: merged in another clone (or by an Undine that kept only the latest report of a file), or by
             git's octopus strategy, which runs no merge driver; their warnings are unknown, as are those of a file
             the driver failed on and that was resolved without it.
             A rebase or a cherry-pick of several commits is checked at the commit git stopped at: ours is HEAD, which
             has the earlier commits already, so neither their warnings nor the cells only they merged are listed. The
             result says so ("notes") when earlier commits were applied: rebase-merge/done names more than one commit,
             rebase-apply/next is past 1, or the sequencer's HEAD (sequencer/head) is not HEAD any more while the
             sequencer's todo starts with the commit being picked (a single `git cherry-pick C` leaves the sequencer of
             an earlier sequence as it was, which then belongs to no commit being picked).
             No head but conflicted files (git merge --squash, cherry-pick -n, stash pop, checkout -m): there is no
             commit for theirs, so only the conflicted files are shown, with their reports' conflicts and warnings;
             theirs, the operation and the cells to verify are unknown (None, None, []) and a note says so.
             git writes MERGE_HEAD and the others only after the merge, so the driver tells a merge (GITHEAD_<id> in its
             environment), a rebase (its state directory) or a cherry-pick (GIT_REFLOG_ACTION, older git) by other
             signs, to point to merge_check; for anything else (stash pop, checkout -m) it suggests resolve only.
precommit    what the pre-commit hook refuses: the staged operation logs (history/*.jsonl in the index, read as staged)
             whose logmerge.journal() is not empty, layout edits not saved to the cell files. A merge of such a log
             with a branch that changed the layout too is a conflict (layout-journal), so they are saved first.
             Journal records a merge copied here and not committed yet (merged_seq: theirs' records renumbered by the
             log's merge; not in HEAD's log) are never refused: the driver decided they stay pending (git merge
             --squash, cherry-pick -n, a merge being concluded). A commit that concludes a merge or a cherry-pick
             (MERGE_HEAD, CHERRY_PICK_HEAD; without them, a squash: the commit the first "commit <id>" line of
             SQUASH_MSG names) refuses only the journal records made while concluding it: those neither HEAD's log nor
             the other head's (every head MERGE_HEAD names) has, and that are no copy a merge made. The others are the
             driver's decision of which edits stay pending (or git's, which took the other head's log whole), as for
             the merges git commits itself (which run no pre-commit hook).
diff_revs    the view's diff() of each file changed between two revisions (or a revision and the working tree, its
             untracked files that are not ignored included; the empty tree for the first revision of a root commit or
             of a repository with no commit yet).

A report belongs to a merge when its ours and theirs blobs are that merge's two versions of the file (the index's stages
2 and 3 while it is conflicted), and its base blob the merge's base version when that is known (stage 1; the unique
merge base, or the picked commit's parent); of several that do (the latest and the merge's own; a merge redone), the
merge's own, the newest. Every report of the clone is read, since a merge's base is not always known. A file renamed
on one side is merged under its new name, which the other side's tree lacks: that side's blob is then any blob of its
tree, and the base's (under the old name) any blob of the base's tree or none. Reports of other merges are ignored.
git's octopus strategy runs no merge driver, so for an octopus merge (more than two sides) a report counts only where
it has a conflicted file's stages exactly (`undine resolve` wrote it during that merge): none for an octopus merge
commit, the files it combined are listed as having no report. A report a two-way merge of two of its branches left
(aborted, say) has their blobs, and is not the octopus merge's.

The cells to verify (DRC / LVS). A cell is the files <cell>.sch .sym .oas .netlist.yaml of a project library
(project.json, a relative "path"). It instantiates the ghost cells of its .oas (the cell file of that name in its own
library, else in the first library that has one) and the instances of its .sch whose symbol resolves, the way
xschem.SymbolResolver looks (the schematic's directory, the libraries' parents, the libraries), to a cell of a project
library. The digest of a cell's hierarchy hashes its own files and the digests of what it instantiates (a cycle is cut
where it closes). A cell of the merged tree whose digest is no side's (ours, theirs; every parent of an octopus merge)
is a combination nobody checked: the cells whose own files were merged and every cell above one. A cell taken whole
from one side keeps that side's results. Each cell to verify says why, in words ("why", the command line's) and as
"reason": {"kind": "merged"}, its own files were merged; {"kind": "side", "side", "below": [{"lib", "cell", "side"}]},
its files are that side's ("ours", "theirs", "parent N" of an octopus merge) and the cells below it differ from that
side's (each marked with the side it is as, None when merged itself); {"kind": "resolves", "side"}, its files and
those of the cells below are that side's, but what it instantiates resolves to other cells. What the check does not
cover is said in "notes" (English) and "note_codes" ({"code": "no_theirs" | "octopus" | "earlier", "operation": the
rebase or cherry-pick whose earlier commits are not covered}).
"""
from __future__ import annotations

import hashlib
import json
import os
import posixpath
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable

import klayout.db as kdb

from undine.layouts import layoutstore
from undine.schematics import xschem
from undine.vcs import layoutmerge, logmerge, merge3, schmerge, yamlmerge

REPORTS = "undine-merge"                    # in the git dir: the reports of the merges of files
# what git-setup's .gitignore keeps out (history.py never lists it either): job results, tapeout bundles, the
# assistant's log, the dependencies installed from undine.lock (all at the top of the project directory) and the legacy
# layout.oas a split leaves in a library
# a changeset's record is design history and is kept; the copies of its check runs (evidence) and the API's
# request log (with the results it keeps for retries) are not
GITIGNORE = ["/runs/", "/tapeout/", "/llm/", "/.undine/", "*.pre-split",
             "/changesets/*/evidence/", "/changesets/requests.jsonl", "/changesets/requests/"]
VIEWS = (".sch", ".sym", layoutstore.SUFFIX, yamlmerge.SUFFIXES["netlist"])     # what DRC / LVS see of a cell
_MODULES = {"schematic": schmerge, "symbol": schmerge, "layout": layoutmerge, "netlist": yamlmerge,
            "intent": yamlmerge, "log": logmerge}
_SIGN = {"add": "+", "del": "-", "mod": "~"}
_ROUNDS = 8                                 # resolve --all-*: deciding conflicts can surface others
_HEADS = (("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick"), ("REBASE_HEAD", "rebase"))   # stage 3's commit
_PICKS = {"pick", "p", "reword", "r", "edit", "e", "squash", "s", "fixup", "f", "merge", "m"}   # a todo line's commit
_NO_THEIRS = ("no commit is known for theirs (git merge --squash, cherry-pick -n, stash pop, checkout -m): the "
              "conflicted files only; the files merged without a conflict and the cells to verify are not known")
_OCTOPUS = ("an octopus merge runs no merge driver: git merged the files changed on more than one of its branches by "
            "lines")

Cell = tuple[str, str]                      # (library, cell)


def kind_of(path: str | Path) -> str | None:
    """The view a project file is (schematic, symbol, layout, netlist, intent, log), None for anything else."""
    p = Path(str(path))
    if p.suffix == ".jsonl" and p.parent.name == "history":
        return "log"
    if p.suffix == layoutstore.SUFFIX:
        return "layout"
    return schmerge.kind_of(p) or yamlmerge.kind_of(p)


def _module(kind: str):
    if kind not in _MODULES:
        raise ValueError(f"no view {kind!r} ({', '.join(_MODULES)})")
    return _MODULES[kind]


def merge(kind: str, base: bytes | None, ours: bytes, theirs: bytes,
          prefer: dict[str, str] | None = None) -> merge3.Result:
    """The view's three-way merge."""
    return _module(kind).merge(base, ours, theirs, prefer, kind=kind)


def diff(kind: str, old: bytes | None, new: bytes | None) -> dict:
    """The view's semantic diff."""
    return _module(kind).diff(old, new, kind=kind)


def diff_files(old: str | Path, new: str | Path, kind: str | None = None) -> dict:
    """diff() of two files; os.devnull for a side that does not exist. The view comes from `kind` or the names."""
    k = kind or kind_of(new) or kind_of(old)
    if k is None:
        raise ValueError(f"which view is {new}? (--kind {' | '.join(_MODULES)})")

    def read(p: str | Path) -> bytes | None:
        return None if str(p) == os.devnull else Path(p).read_bytes()
    return diff(k, read(old), read(new))


# ---------------------------------------------------------------- git


def _git(cwd: Path, *args: str, input: bytes | None = None) -> bytes:
    r = subprocess.run(["git", *args], cwd=cwd, input=input, capture_output=True)
    if r.returncode != 0:
        msg = r.stderr.decode(errors="replace").strip().splitlines()
        raise RuntimeError(f"git {' '.join(a for a in args[:2] if not a.startswith('-'))}: "
                           f"{msg[-1] if msg else 'failed'}")
    return r.stdout


class _Repo:
    """The repository a directory is in: its top, git dir, object format, and the directory's path from the top."""

    def __init__(self, d: Path):
        out = _git(d, "rev-parse", "--show-toplevel", "--absolute-git-dir", "--show-object-format",
                   "--show-prefix").decode("utf-8", "surrogateescape").splitlines()
        self.top, self.git_dir, self.fmt = Path(out[0]), Path(out[1]), out[2]
        self.prefix = (out[3] if len(out) > 3 else "").rstrip("/")

    def blob_id(self, data: bytes) -> str:
        """The id git gives a blob of these bytes."""
        return hashlib.new(self.fmt, b"blob %d\0" % len(data) + data).hexdigest()

    def empty_tree(self) -> str:
        """The id of the empty tree, which git knows without storing it: what comes before the first commit."""
        return hashlib.new(self.fmt, b"tree 0\0").hexdigest()

    def commit(self, rev: str) -> str:
        return _git(self.top, "rev-parse", "--verify", "--end-of-options", f"{rev}^{{commit}}").decode().strip()

    def tree(self, rev: str) -> dict[str, str]:
        """A commit's files: path from the top -> blob id (a name that is not UTF-8 with surrogateescape)."""
        files = {}
        for entry in _git(self.top, "ls-tree", "-r", "-z", "--full-tree", rev).decode("utf-8", "surrogateescape") \
                .split("\0"):
            if entry:
                meta, path = entry.split("\t", 1)
                _, typ, oid = meta.split()
                if typ == "blob":
                    files[path] = oid
        return files

    def worktree(self, dirs: Iterable[str]) -> dict[str, str]:
        """The view files in these directories of the working tree: path -> blob id (as `git add` would store it)."""
        paths = [posixpath.join(d, f.name) for d in dirs if (self.top / d).is_dir()
                 for f in sorted((self.top / d).iterdir())
                 if f.is_file() and not f.name.startswith(".") and f.name.endswith(VIEWS)]
        if not paths:
            return {}
        ids = _git(self.top, "hash-object", "--stdin-paths",
                   input="".join(p + "\n" for p in paths).encode("utf-8", "surrogateescape"))
        return dict(zip(paths, ids.decode().split()))

    def unmerged(self, *pathspec: str, cwd: Path | None = None) -> dict[str, dict[int, str]]:
        """The conflicted files of the index: path -> {stage: blob id} (1 base, 2 ours, 3 theirs)."""
        out = _git(cwd or self.top, "--literal-pathspecs", "ls-files", "-u", "-z", "--full-name", "--", *pathspec)
        stages: dict[str, dict[int, str]] = {}
        for entry in out.decode("utf-8", "surrogateescape").split("\0"):
            if entry:
                meta, path = entry.split("\t", 1)
                _, oid, stage = meta.split()
                stages.setdefault(path, {})[int(stage)] = oid
        return stages

    def report_key(self, blobs: dict[str, str | None]) -> str:
        """What names the report of one merge of a file: its base, ours and theirs blobs (the empty file's as None,
        which the driver gives an empty %O)."""
        empty = self.blob_id(b"")
        ids = [None if (b := blobs.get(k)) == empty else b for k in ("base", "ours", "theirs")]
        return hashlib.sha1("\0".join(i or "" for i in ids).encode()).hexdigest()[:12]

    def report_path(self, path: str, blobs: dict[str, str | None] | None = None) -> Path:
        """The file's latest report, or with the merge's blobs that merge's own (module docstring)."""
        p = Path(path)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"not a path inside the repository: {path}")
        return self.git_dir / REPORTS / (f"{path}.json" if blobs is None else f"{path}.{self.report_key(blobs)}.json")

    def write_report(self, path: str, data: bytes, blobs: dict[str, str | None]) -> None:
        """The report under both names: the merge's own first (the latest is what resolve reads during the merge)."""
        _write(self.report_path(path, blobs), data)
        _write(self.report_path(path), data)

    def conflicted(self, path: str | Path, here: Path) -> tuple[str, dict[int, str]]:
        """The conflicted file `path` names: (path from the top, its stages). `path` is relative to `here`, else to the
        top (the driver prints paths from the top, the project may be a subdirectory); ":/path" is from the top."""
        s = str(path)
        if s.startswith(":/"):
            tries = [(s[2:] or ".", self.top)]
        else:
            outside = Path(s).is_absolute() or ".." in Path(s).parts       # names a place from `here` only
            tries = [(s, here)] + ([] if outside else [(s, self.top)])
        found: dict[str, dict[int, str]] = {}
        for p, d in tries:
            found = self.unmerged(p, cwd=d)
            if found or d.resolve() == self.top.resolve():
                break
        if len(found) != 1:
            raise ValueError(f"{path} is not a conflicted file (git checkout -m {path} brings a merge's conflict back)"
                             if not found else f"{path}: {len(found)} conflicted files; name one")
        return found.popitem()


class _Objects:
    """One `git cat-file --batch` for many objects, read as they are asked for (a blob id or "<commit>:<path>")."""

    def __init__(self, top: Path):
        self.proc = subprocess.Popen(["git", "cat-file", "--batch"], cwd=top, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE)

    def get(self, name: str) -> bytes | None:
        """The object's bytes; None when there is no such object (a file the commit does not have)."""
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(name.encode("utf-8", "surrogateescape") + b"\n")     # git's names, as they were read
        self.proc.stdin.flush()
        head = self.proc.stdout.readline().split()
        if len(head) != 3 or not head[2].isdigit():
            return None                                         # "<name> missing" / "ambiguous"
        data = self.proc.stdout.read(int(head[2]))
        self.proc.stdout.read(1)
        return data

    def __enter__(self) -> _Objects:
        return self

    def __exit__(self, *exc) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        self.proc.wait()


def _write(path: Path, data: bytes) -> None:
    """Replaces a file by a rename: whoever reads it (git, or a failure halfway) sees the old file or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if path.exists():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _report(path: str, res: merge3.Result, prefer: dict[str, str], blobs: dict[str, str | None]) -> bytes:
    return (json.dumps({"path": path, **res.to_json(), "prefer": prefer, "blobs": blobs}, indent=1) + "\n").encode()


def _error_report(path: str, kind: str, error: str, blobs: dict[str, str | None]) -> bytes:
    """The report of a merge that failed (the file is ours whole): merge_check shows the error."""
    return (json.dumps({"path": path, "kind": kind, "error": error, "clean": False, "conflicts": [], "warnings": [],
                        "combined": [], "prefer": {}, "blobs": blobs}, indent=1) + "\n").encode()


def _read_report(p: Path) -> dict | None:
    try:
        r = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return r if isinstance(r, dict) and isinstance(r.get("path"), str) else None


def _ids(cs: Iterable[merge3.Conflict] | Iterable[dict], n: int = 5) -> str:
    ids = [c.id if isinstance(c, merge3.Conflict) else c["id"] for c in cs]
    return ", ".join(ids[:n]) + (f" and {len(ids) - n} more" if len(ids) > n else "")


# ---------------------------------------------------------------- driver


def driver(base_path: str | Path, ours_path: str | Path, theirs_path: str | Path, path: str,
           cwd: str | Path | None = None, virtual_base: bool = False) -> int:
    """git's merge driver (%O %A %B %P): the merged file into %A and a report; the exit code git expects (see the
    module docstring). Never raises: an error leaves %A as it is, writes an error report when it can, and exits 1.
    With `virtual_base`, the driver of the merge of several merge bases (_virtual_base), which always exits 0."""
    here = Path(cwd or os.getcwd())
    kind = kind_of(path)
    if virtual_base:
        return _virtual_base(here, base_path, ours_path, theirs_path, path, kind)
    repo: _Repo | None = None
    blobs: dict[str, str | None] = {"base": None, "ours": None, "theirs": None}
    wrote = False
    try:
        if kind is None:
            return _line_merge(here, base_path, ours_path, theirs_path, path)
        repo = _Repo(here)
        base, ours, theirs = ((here / p).read_bytes() for p in (base_path, ours_path, theirs_path))
        # the inputs' ids before merging: an error report has them too
        blobs = {"base": repo.blob_id(base) if base else None, "ours": repo.blob_id(ours),
                 "theirs": repo.blob_id(theirs)}
        res = merge(kind, base or None, ours, theirs)
        _write(here / ours_path, res.data)              # first: a report never describes a merge %A does not have
        wrote = True
        repo.write_report(path, _report(path, res, {}, blobs), blobs)
    except Exception as e:                                       # git must get an exit code, never a traceback
        error = f"{e.__class__.__name__}: {e}"
        if not wrote:
            if repo is not None:
                try:
                    repo.write_report(path, _error_report(path, kind, error, blobs), blobs)
                except Exception:                                # best effort: git's conflict is what counts
                    pass
            print(f"undine: {path}: {error}; ours kept, the file is left conflicted", file=sys.stderr)
            return 1
        print(f"undine: {path}: merged, but no report ({error}): merge-check cannot show its warnings",
              file=sys.stderr)
    if res.clean:
        extra = [f"{len(res.combined)} from theirs"] if res.combined else []
        extra += [f"{len(res.warnings)} warning{'s' * (len(res.warnings) != 1)}"] if res.warnings else []
        print(f"undine: {path}: {kind} merged{' (' + ', '.join(extra) + ')' if extra else ''}", file=sys.stderr)
        return 0
    left = res.open_conflicts
    see = "see `undine merge-check`, then " if repo is not None and _checkable(repo) else ""
    # one conflict: its own words, which say what the file has for it (not always just ours': the log's journal)
    if len(left) == 1:
        what = " ".join(left[0].what.split("\n"))
        said = f"1 conflict ({left[0].id}): {what}" + (", ours kept" if kind != "log" else "")
    else:
        said = f"{len(left)} conflicts ({_ids(left)}), ours kept"
    print(f"undine: {path}: {kind}, {said}; {see}`undine resolve {shlex.quote(path)} --theirs ID`", file=sys.stderr)
    return 1


def _rebasing(repo: _Repo) -> bool:
    """A rebase is in progress (REBASE_HEAD alone is not one: git leaves it behind a finished rebase)."""
    return (repo.git_dir / "rebase-merge").is_dir() or (repo.git_dir / "rebase-apply").is_dir()


def _checkable(repo: _Repo) -> bool:
    """Whether merge_check will see the operation the driver runs in: git writes MERGE_HEAD, CHERRY_PICK_HEAD and
    REBASE_HEAD only after the merge, so a merge is told by GITHEAD_<id> (git merge sets it for the conflict labels), a
    rebase by its state directory, a cherry-pick by GIT_REFLOG_ACTION (only older git exports it: newer git's
    cherry-pick gets the resolve hint alone). Stash pop and checkout -m have none of these: merge_check has no commit
    for their theirs."""
    env = os.environ
    return (any(k.startswith("GITHEAD_") for k in env) or (repo.git_dir / "rebase-merge").is_dir()
            or (repo.git_dir / "rebase-apply" / "rebasing").exists()
            or env.get("GIT_REFLOG_ACTION", "").startswith(("cherry-pick", "rebase")))


def _merge_lines(here: Path, base_path: str | Path, ours_path: str | Path,
                 theirs_path: str | Path) -> tuple[bytes, int]:
    """git's own line merge of three files: (the merged bytes, conflicts marked in them; the number of conflicts)."""
    r = subprocess.run(["git", "merge-file", "-p", "-L", "ours", "-L", "base", "-L", "theirs",
                        str(ours_path), str(base_path), str(theirs_path)], cwd=here, capture_output=True)
    if r.returncode > 127:                                      # an error (a binary file); 1..127: conflicts
        raise RuntimeError(r.stderr.decode(errors="replace").strip() or "git merge-file failed")
    return r.stdout, r.returncode


def _line_merge(here: Path, base_path: str | Path, ours_path: str | Path, theirs_path: str | Path, path: str) -> int:
    """git's own line merge, for a file of no view Undine knows (its conflicts marked in the file)."""
    data, conflicts = _merge_lines(here, base_path, ours_path, theirs_path)
    _write(here / ours_path, data)
    print(f"undine: {path}: not a view Undine merges, merged by lines"
          f"{f' ({conflicts} conflicts)' if conflicts else ''}", file=sys.stderr)
    return 0 if conflicts == 0 else 1


_BASE_ROUNDS = 3        # a decision can change what else conflicts: merged again with the new ones, a few times


def _virtual_base(here: Path, base_path: str | Path, ours_path: str | Path, theirs_path: str | Path, path: str,
                  kind: str | None) -> int:
    """The driver of the merge git makes of several merge bases (a criss-cross merge: merge.undine.recursive, module
    docstring), whose result is the base of the merge itself, never a file anybody sees. Merged as usual, each conflict
    taking the bases' own base %O's value (prefer "base": ours kept would decide it silently, since the merge then
    sees one side's resolution as the base and takes the other's; against the older value both sides' changes are
    seen, and differ, a conflict of the merge), what merged cleanly kept. A conflict a view cannot take the base for
    (two instances added under one name, a database unit, an unreadable side), or an error: %A gets %O whole (empty
    when there is none), as git does for a binary file. Exit 0 either way (git reads %A all the same); no report (what
    counts is the merge's). Prints a line only when it took the whole base."""
    base: bytes | None = None
    try:
        base, ours, theirs = ((here / p).read_bytes() for p in (base_path, ours_path, theirs_path))
        if kind is None:
            data, n = _merge_lines(here, base_path, ours_path, theirs_path)
            said = f"{n} conflict{'s' * (n != 1)} by lines" if n else ""
        else:
            res, prefer = merge(kind, base or None, ours, theirs), {}
            for _ in range(_BASE_ROUNDS):    # the bases' base for what conflicts, what merged cleanly kept
                if res.clean:
                    break
                prefer.update({c.id: "base" for c in res.open_conflicts})
                res = merge(kind, base or None, ours, theirs, prefer)
            data, left = res.data, res.open_conflicts
            said = f"{len(left)} conflict{'s' * (len(left) != 1)}: {_ids(left)}" if left else ""
        if not said:
            _write(here / ours_path, data)
            return 0
    except Exception as e:                                       # git must get an exit code, never a traceback
        said = f"{e.__class__.__name__}: {e}"
    try:
        if base is None:
            base = (here / base_path).read_bytes()
        _write(here / ours_path, base)
    except Exception as e:                                       # %A stays the first merge base's: say so
        print(f"undine: {path}: the merge of the merge bases failed ({said}), and their base cannot be taken "
              f"({e.__class__.__name__}: {e}): check this file's merge", file=sys.stderr)
        return 0
    print(f"undine: {path}: the merge bases differ here ({said}): merged against the base they share",
          file=sys.stderr)
    return 0


# ---------------------------------------------------------------- resolve


def resolve(path: str | Path, theirs: Iterable[str] = (), ours: Iterable[str] = (), all_side: str | None = None,
            cwd: str | Path | None = None) -> list[merge3.Conflict]:
    """Merges a conflicted file again from the index's stages, taking theirs for the conflict ids in `theirs`, ours for
    those in `ours`, and `all_side` for every other open one; decisions accumulate in the report. Writes the file
    (`path` from `cwd`, else from the repository's top; ":/path" from the top), then the report; returns the conflicts
    still open. Does not `git add`."""
    here = Path(cwd or os.getcwd())
    theirs, ours = list(theirs), list(ours)
    if set(theirs) & set(ours):
        raise ValueError(f"both sides named for {', '.join(sorted(set(theirs) & set(ours)))}")
    if all_side not in (None, "ours", "theirs"):
        raise ValueError(f"all_side is ours or theirs, not {all_side!r}")
    repo = _Repo(here)
    rel, stages = repo.conflicted(path, here)
    if 2 not in stages or 3 not in stages:
        top = shlex.quote(f":/{rel}")                    # git's path from the top: right from any directory
        raise ValueError(f"{rel} was deleted on one side and changed on the other: keep it with `git add {top}` "
                         f"or delete it with `git rm {top}`")
    kind = kind_of(rel)
    if kind is None:
        raise ValueError(f"{rel} is not a file Undine merges; resolve it with git")
    with _Objects(repo.top) as objs:
        base = objs.get(stages[1]) if 1 in stages else None
        o, t = objs.get(stages[2]), objs.get(stages[3])
    if o is None or t is None:
        raise RuntimeError(f"{rel}: the index's stages cannot be read")
    blobs = {"base": stages.get(1), "ours": stages[2], "theirs": stages[3]}
    old = _read_report(repo.report_path(rel, blobs)) or _read_report(repo.report_path(rel)) or {}
    same = {k: (old.get("blobs") or {}).get(k) for k in ("ours", "theirs")} == {k: blobs[k] for k in ("ours", "theirs")}
    prefer = dict(old.get("prefer") or {}) if same else {}
    prefer.update({i: "theirs" for i in theirs} | {i: "ours" for i in ours})
    res = merge(kind, base or None, o, t, prefer)
    for _ in range(_ROUNDS if all_side else 0):
        if res.clean:
            break
        prefer.update({c.id: all_side for c in res.open_conflicts})
        res = merge(kind, base or None, o, t, prefer)
    known = {c.id for c in res.conflicts}
    unknown = sorted({*theirs, *ours} - known)
    if unknown:
        raise ValueError(f"{rel}: no conflict {', '.join(unknown)}; "
                         f"its conflicts: {', '.join(sorted(known)) or 'none'}")
    _write(repo.top / rel, res.data)                     # first, as in the driver
    repo.write_report(rel, _report(rel, res, prefer, blobs), blobs)
    return res.open_conflicts


def locate(path: str | Path, cwd: str | Path | None = None) -> str:
    """The path from the repository's top of the conflicted file `path` names (as resolve finds it)."""
    here = Path(cwd or os.getcwd())
    return _Repo(here).conflicted(path, here)[0]


# ---------------------------------------------------------------- pre-commit


def _record(r: dict) -> str:
    """A log record as one string whatever the spacing and key order of its line."""
    return json.dumps(r, sort_keys=True, separators=(",", ":"))


def _squashed(repo: _Repo) -> list[str]:
    """The commit `git merge --squash` took theirs from: the first "commit <id>" line of SQUASH_MSG (it lists the
    squashed commits, newest first); [] without one."""
    try:
        text = (repo.git_dir / "SQUASH_MSG").read_text(errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        if line.startswith("commit ") and len(w := line.split()) == 2:
            try:
                return [repo.commit(w[1])]
            except RuntimeError:                    # not a commit (any more): no theirs
                return []
    return []


def precommit(cwd: str | Path | None = None) -> list[dict]:
    """The staged operation logs that hold unsaved layout edits (module docstring), what the pre-commit hook refuses:
    [{"path" from the repository's top, "lib", "cells": the cells the edits are in}]. Never the copies a merge made here
    (merged_seq, not in HEAD's log). While a merge, a cherry-pick or a squash merge is being concluded, only the edits
    made since: the journal records in neither HEAD's log nor the other head's, and no copy a merge made."""
    repo = _Repo(Path(cwd or os.getcwd()))
    theirs = [i for f in ("MERGE_HEAD", "CHERRY_PICK_HEAD") if (repo.git_dir / f).is_file()
              for i in (repo.git_dir / f).read_text().split()] or _squashed(repo)
    sides = ["HEAD", *theirs] if theirs else []
    # the index git commits (a commit -a or with paths has its own: GIT_INDEX_FILE, which git passes to the hook)
    staged = _git(repo.top, "diff", "--cached", "--name-only", "-z", "--no-renames", "--no-relative",
                  "--diff-filter=d").decode("utf-8", "surrogateescape").split("\0")
    out = []
    with _Objects(repo.top) as objs:
        for p in sorted(p for p in staged if p and kind_of(p) == "log"):
            pending = logmerge.journal(objs.get(f":{p}") or b"")
            if pending:                             # copies a merge made here, not committed yet: the driver's choice
                head = {_record(r) for _, r in logmerge.records(objs.get(f"HEAD:{p}"))}
                pending = [r for r in pending if "merged_seq" not in r or _record(r) in head]
            if pending and sides:                   # the driver decided which of the sides' edits stay pending
                known = {_record(r) for s in sides for _, r in logmerge.records(objs.get(f"{s}:{p}"))}
                pending = [r for r in pending if "merged_seq" not in r and _record(r) not in known]
            if pending:
                out.append({"path": p, "lib": posixpath.basename(p)[:-len(".jsonl")],
                            "cells": sorted({str(r["cell"]) for r in pending if r.get("cell")})})
    return out


# ---------------------------------------------------------------- merge check


def _libraries(meta: dict, prefix: str) -> dict[str, str]:
    """The project's own libraries (as Project.library_specs reads them): name -> directory from the repository top."""
    out = {}
    for name, spec in (meta.get("libraries") or {}).items():
        rel = spec if isinstance(spec, str) else spec.get("path") if isinstance(spec, dict) else None
        if not isinstance(rel, str) or Path(rel).expanduser().is_absolute():
            continue                                             # a dependency (git) or a directory outside
        d = posixpath.normpath(posixpath.join(prefix, rel))
        if d != ".." and not d.startswith("../"):
            out[name] = "" if d == "." else d                   # "": the repository's top (as dirname gives it)
    return out


class _Parser:
    """What a cell's files instantiate, by blob id (ours, theirs and the merge share most files)."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.ghosts: dict[str, list[str]] = {}
        self.symbols: dict[str, list[str]] = {}

    def layout(self, oid: str, data: Callable[[], bytes | None]) -> list[str]:
        if oid not in self.ghosts:
            p = self.tmp / f"{oid}{layoutstore.SUFFIX}"
            p.write_bytes(data() or b"")
            ly = kdb.Layout()
            try:
                ly.read(str(p))
                self.ghosts[oid] = sorted(c.name for c in ly.each_cell() if c.is_ghost_cell())
            except RuntimeError:
                self.ghosts[oid] = []                           # unreadable: its own bytes still count
            p.unlink()
        return self.ghosts[oid]

    def schematic(self, oid: str, data: Callable[[], bytes | None]) -> list[str]:
        if oid not in self.symbols:
            try:
                f = xschem.parse((data() or b"").decode("utf-8", "replace"))
                self.symbols[oid] = sorted({c.sym for c in f.comps})
            except Exception:                                    # unparsable: its own bytes still count
                self.symbols[oid] = []
        return self.symbols[oid]


class _Hierarchy:
    """The cells of the project libraries in one tree, what each instantiates, the digests of their hierarchies."""

    def __init__(self, files: dict[str, str], read: Callable[[str], bytes | None], libs: dict[str, str],
                 parser: _Parser):
        self.libs, self.read, self.parser = libs, read, parser
        self.lib_of = {d: n for n, d in libs.items()}
        self.files = {p: o for p, o in files.items() if posixpath.dirname(p) in self.lib_of}
        self.cells: dict[Cell, dict[str, str]] = {}            # cell -> {view suffix: path}
        for p in self.files:
            d, name = posixpath.split(p)
            suffix = next((s for s in VIEWS if name.endswith(s) and len(name) > len(s)), None)
            if suffix is None or name.startswith("."):
                continue
            cell = layoutstore.cell_name(Path(name)) if suffix == layoutstore.SUFFIX else name[:-len(suffix)]
            self.cells.setdefault((self.lib_of[d], cell), {})[suffix] = p
        self._kids: dict[Cell, list[Cell]] = {}
        self.digest: dict[Cell, str] = {}
        for c in sorted(self.cells):
            self._visit(c, ())

    def own(self, c: Cell) -> list | None:
        views = self.cells.get(c)
        return None if views is None else sorted((s, self.files[p]) for s, p in views.items())

    def children(self, c: Cell) -> list[Cell]:
        if c not in self._kids:
            views, out = self.cells[c], set()
            if (p := views.get(layoutstore.SUFFIX)) is not None:
                out |= {k for n in self.parser.layout(self.files[p], lambda: self.read(p))
                        if (k := self._layout_cell(c[0], n)) is not None}
            if (p := views.get(".sch")) is not None:
                out |= {k for s in self.parser.schematic(self.files[p], lambda: self.read(p))
                        if (k := self._symbol_cell(posixpath.dirname(p), s)) is not None}
            self._kids[c] = sorted(out - {c})
        return self._kids[c]

    def _layout_cell(self, lib: str, name: str) -> Cell | None:
        """A ghost cell's definition: its cell file in the same library, else in the first library that has one."""
        f = layoutstore.file_name(name)
        return next(((n, name) for n in dict.fromkeys([lib, *self.libs])
                     if posixpath.join(self.libs[n], f) in self.files), None)

    def _symbol_cell(self, sch_dir: str, sym: str) -> Cell | None:
        """The project cell an instance's symbol is, looked up like xschem.SymbolResolver.find (None: not ours)."""
        if not sym or os.path.isabs(sym):
            return None
        dirs = [sch_dir, *(posixpath.dirname(d) for d in self.libs.values()), *self.libs.values()]
        for name in ([sym] if sym.endswith((".sym", ".sch")) else [sym, f"{sym}.sym"]):
            for d in dirs:
                p = posixpath.normpath(posixpath.join(d, name))
                if p in self.files:
                    lib = self.lib_of.get(posixpath.dirname(p))
                    return (lib, posixpath.basename(p).rsplit(".", 1)[0]) if lib else None
        return None

    def _visit(self, c: Cell, stack: tuple[Cell, ...]) -> str:
        if c in self.digest:
            return self.digest[c]
        if c in stack:
            return "cycle"
        kids = [[*k, self._visit(k, (*stack, c))] for k in self.children(c)]
        self.digest[c] = hashlib.sha256(json.dumps([self.own(c), kids]).encode()).hexdigest()
        return self.digest[c]


def _whose(name: str) -> str:
    return f"{name}'" if name.endswith("s") else f"{name}'s"       # ours', theirs', parent 3's


def _why(c: Cell, merged: _Hierarchy, sides: list[tuple[str, _Hierarchy]]) -> tuple[str, dict]:
    """Why a cell is listed, in words and as a reason (module docstring): its own files were merged, or which cells
    below it differ from the side its files are from (marked with another side when they are that side's, unmarked
    when merged themselves)."""
    for name, h in sides:
        if merged.own(c) == h.own(c):
            below = []
            for k in merged.children(c):
                if merged.digest[k] != h.digest.get(k):
                    other = next((n for n, x in sides if x is not h and x.digest.get(k) == merged.digest[k]), None)
                    below.append({"lib": k[0], "cell": k[1], "side": other})
            if not below:
                return (f"its views are {_whose(name)}; what it instantiates resolves differently",
                        {"kind": "resolves", "side": name})
            words = [f"{b['lib']}/{b['cell']}" + (f" ({_whose(b['side'])})" if b["side"] else "") for b in below]
            return (f"its views are {_whose(name)}; changed below it: {', '.join(words)}",
                    {"kind": "side", "side": name, "below": below})
    return "its views were merged", {"kind": "merged"}


def _base_tree(repo: _Repo, op: str, ours: str, theirs: str) -> dict[str, str] | None:
    """The files of the merge's base: the merge base of ours and theirs ({} when there is none), or for a pick the
    parent of the picked commit; None when it is not one commit (criss-cross merge bases, a picked merge commit)."""
    if op == "merge":
        r = subprocess.run(["git", "merge-base", "--all", ours, theirs], cwd=repo.top, capture_output=True, text=True)
        bases = r.stdout.split() if r.returncode in (0, 1) else None   # 1: unrelated histories
    else:
        bases = _git(repo.top, "rev-list", "--parents", "-n", "1", theirs).decode().split()[1:]
    if bases is None or len(bases) > 1:
        return None
    return repo.tree(bases[0]) if bases else {}


def _in_progress(repo: _Repo) -> tuple[str, list[str]] | None:
    """The operation in progress and the commits its theirs are, from the first of _HEADS git wrote (REBASE_HEAD only
    during a rebase; MERGE_HEAD names every branch of an octopus merge, one per line); None when there is none."""
    for f, op in _HEADS:
        p = repo.git_dir / f
        if p.is_file() and (op != "rebase" or _rebasing(repo)):
            return op, p.read_text().split() or [""]
    return None


def _earlier(repo: _Repo, op: str, theirs: str) -> str | None:
    """The sequence ("rebase", "cherry-pick") whose earlier commits are applied already, so merge_check, which sees the
    commit git stopped at (`theirs`), does not cover them; None when this is its first commit, or there is no
    sequence."""
    g = repo.git_dir
    try:
        if (g / "rebase-merge").is_dir():           # done: the todo lines carried out, the stopped one included
            done = (g / "rebase-merge" / "done").read_text().splitlines()
            return "rebase" if sum(1 for x in done if x.split()[:1] and x.split()[0] in _PICKS) > 1 else None
        if (g / "rebase-apply").is_dir():           # next: the number of the patch being applied
            return "rebase" if int((g / "rebase-apply" / "next").read_text().strip() or 0) > 1 else None
        if op == "cherry-pick" and (g / "sequencer").is_dir():
            # todo: the picks left, the stopped one first; a single pick leaves an earlier sequence's as it was
            todo = [x.split() for x in (g / "sequencer" / "todo").read_text().splitlines()]
            first = next((x for x in todo if x and not x[0].startswith("#")), [])
            if len(first) < 2 or len(first[1]) < 4 or not theirs.startswith(first[1].lower()):
                return None                         # not this pick's sequence
            # head: HEAD when the picks began
            return "cherry-pick" if (g / "sequencer" / "head").read_text().strip() != repo.commit("HEAD") else None
    except (OSError, ValueError):
        return "rebase" if _rebasing(repo) else op     # unreadable: say it may not cover everything
    return None


def merge_check(project_dir: str | Path, commit: str | None = None) -> dict:
    """The state of a merge of the project's repository: conflicts still open, warnings, cells to verify (module
    docstring). Without `commit`, the merge, cherry-pick or rebase in progress (or, with no head, the conflicted files
    of the index); with it, that merge commit.

        {"ours", "theirs": None when no commit is known for it (conflicts without a head: merge --squash, ...),
         "commit": the merge commit or None (in progress: the working tree),
         "parents": the merge commit's parents (more than two: an octopus merge); in progress, HEAD and the commits
             MERGE_HEAD names when they are more than one (an octopus merge), else None,
         "operation": merge | cherry-pick | rebase in progress (None for a commit, and with no theirs),
         "unmerged": [{"path", "kind", "conflicts": open conflicts (None: git's own conflict), "error"?}],
         "warnings": [{"path", "warning"}], "merged": [paths the driver merged],
         "no_report": [view files the merge commit combined that have no report here (always [] in progress)],
         "verify": [{"lib", "cell", "why", "reason"}],
         "notes": [what the check does not cover: earlier commits of a rebase, an unknown theirs, an octopus merge],
         "note_codes": [the notes as {"code": no_theirs | octopus | earlier, "operation": the earlier's sequence}]}

    Paths are from the repository's top.
    """
    d = Path(project_dir)
    repo = _Repo(d)
    notes: list[str] = []
    codes: list[dict] = []

    def note(code: str, text: str, **extra: str) -> None:
        if not any(c["code"] == code for c in codes):
            notes.append(text)
            codes.append({"code": code, **extra})

    parents: list[str] | None = None
    if commit is None:
        head, stages = _in_progress(repo), repo.unmerged()
        if head is None and not stages:
            if _rebasing(repo):                     # stopped at a break or a failed exec: nothing being merged
                return {"ours": repo.commit("HEAD"), "theirs": None, "commit": None, "parents": None,
                        "operation": "rebase", "unmerged": [], "warnings": [], "merged": [], "no_report": [],
                        "verify": [], "notes": [], "note_codes": []}
            raise ValueError(f"no merge in progress in {repo.top}, nor a cherry-pick or rebase "
                             "(for a merge commit: --commit C)")
        ours, merged = repo.commit("HEAD"), None
        if head is None:                            # merge --squash, cherry-pick -n, stash pop: theirs is no commit
            op, theirs, heads = None, None, [ours]
            note("no_theirs", _NO_THEIRS)
        else:
            op = head[0]
            heads = [ours, *(repo.commit(i) for i in head[1])]
            theirs = heads[1]
            if len(heads) > 2:                      # an octopus merge, stopped at a conflict of its last branch
                parents = heads
                note("octopus", _OCTOPUS)
            elif seq := _earlier(repo, op, theirs):
                note("earlier", f"earlier commits of this {seq} are not covered: their warnings were printed when "
                     "they were merged, and the cells only they merged are not listed", operation=seq)
    else:
        merged, op = repo.commit(commit), "merge"
        parents = _git(repo.top, "rev-list", "--parents", "-n", "1", merged).decode().split()[1:]
        if len(parents) < 2:
            raise ValueError(f"{commit} is not a merge commit")
        (ours, theirs), heads = parents[:2], parents
        stages = {}
    trees = [repo.tree(h) for h in heads]
    o_files, others = trees[0], trees[1:]           # others: theirs, or every other parent of an octopus merge
    base_files = _base_tree(repo, op, ours, theirs) if theirs is not None and len(heads) == 2 else None
    empty = repo.blob_id(b"")
    blobs_of: dict[int, set[str]] = {}

    def side(files: dict[str, str], p: str) -> set[str]:
        """A side's blob of p; when it lacks p (renamed on the other side: under its old name), any of its blobs."""
        if p in files:
            return {files[p]}
        if id(files) not in blobs_of:
            blobs_of[id(files)] = set(files.values())
        return blobs_of[id(files)]

    def belongs(r: dict) -> bool:
        """The report is this merge's: the same ours and theirs, and the same base where that is known."""
        p, blobs = r["path"], r.get("blobs") or {}
        st = stages.get(p)
        if st:
            want = {"ours": {st.get(2)}, "theirs": {st.get(3)}, "base": {st.get(1)}}
        else:
            want = {"ours": side(o_files, p), "theirs": set().union(*(side(f, p) for f in others))}
            if base_files is not None:
                want["base"] = {base_files[p]} if p in base_files else {None, *side(base_files, p)}
        if "base" in want:                          # the driver sees no base and an empty one alike
            want["base"] = {None if b == empty else b for b in want["base"]}
        return all(blobs.get(k) in v for k, v in want.items())

    # by its blobs, a report of a file merged without a conflict; by the stages only, without a theirs, or in an octopus
    # merge, which runs no driver (a report that matches blobs there is another merge's: a two-way one, aborted)
    by_blobs = theirs is not None and len(heads) == 2
    found: dict[str, tuple[tuple[bool, int], dict]] = {}
    for p in sorted((repo.git_dir / REPORTS).rglob("*.json")):
        r = _read_report(p)
        if r is None or not ((by_blobs or r["path"] in stages) and belongs(r)):
            continue
        try:                                        # the merge's own report rather than the latest, the newest
            rank = (p != repo.report_path(r["path"]), p.stat().st_mtime_ns)
        except (OSError, ValueError):
            continue
        if r["path"] not in found or rank > found[r["path"]][0]:
            found[r["path"]] = (rank, r)
    reports = {path: r for path, (_, r) in sorted(found.items())}
    unmerged = []
    for p in sorted(stages):
        r = reports.get(p)
        entry: dict[str, Any] = {"path": p, "kind": kind_of(p),
                                 "conflicts": [c for c in r["conflicts"] if not c.get("resolved")] if r else None}
        if r and r.get("error"):
            entry["error"] = r["error"]
        unmerged.append(entry)
    warnings = [{"path": p, "warning": w} for p, r in reports.items() for w in r.get("warnings", [])]
    warnings += [{"path": p, "warning": f"not merged by Undine ({r['error']}): its warnings are unknown"}
                 for p, r in reports.items() if r.get("error") and p not in stages]     # resolved without Undine
    no_report: list[str] = []
    if merged is not None:
        m_files = repo.tree(merged)
        under = f"{repo.prefix}/" if repo.prefix else ""
        no_report = [p for p, oid in sorted(m_files.items()) if p.startswith(under) and kind_of(p) is not None
                     and not any(oid in side(f, p) for f in trees) and p not in reports]
        if no_report and len(heads) > 2:
            note("octopus", _OCTOPUS)

    verify: list[dict] = []
    if theirs is not None:
        with _Objects(repo.top) as objs, tempfile.TemporaryDirectory(prefix="undine-check-") as tmp:
            pj = posixpath.join(repo.prefix, "project.json")     # the merged project's libraries
            if merged is None:
                meta = (repo.top / pj).read_bytes() if (repo.top / pj).is_file() else None
            else:
                meta = objs.get(f"{merged}:{pj}")
            if meta is None:
                raise ValueError(f"no project.json in {d} (merge-check takes a project directory)")
            try:
                libs = _libraries(json.loads(meta), repo.prefix)
            except (ValueError, AttributeError) as e:
                raise ValueError(f"project.json cannot be read ({e}); resolve it first") from e
            parser = _Parser(Path(tmp))

            def tree(files: dict[str, str]) -> _Hierarchy:
                return _Hierarchy(files, lambda p: objs.get(files[p]), libs, parser)

            hs = [tree(f) for f in trees]
            if merged is None:
                hm = _Hierarchy(repo.worktree(libs.values()), lambda p: (repo.top / p).read_bytes(), libs, parser)
            else:
                hm = tree(m_files)
        names = ["ours", "theirs"] if len(hs) == 2 else ["ours", *(f"parent {i}" for i in range(2, len(hs) + 1))]
        sides = list(zip(names, hs))
        verify = [{"lib": c[0], "cell": c[1], **dict(zip(("why", "reason"), _why(c, hm, sides)))}
                  for c in sorted(hm.cells) if hm.digest[c] not in {h.digest.get(c) for h in hs}]
    return {"ours": ours, "theirs": theirs, "commit": merged, "parents": parents,
            "operation": None if merged else op, "unmerged": unmerged, "warnings": warnings,
            "merged": sorted(p for p, r in reports.items() if not r.get("error")), "no_report": no_report,
            "verify": verify, "notes": notes, "note_codes": codes}


# ---------------------------------------------------------------- diff of revisions


def diff_revs(project_dir: str | Path, a: str | None, b: str | None = None,
              paths: Iterable[str] = (".",)) -> dict[str, dict]:
    """{path from the repository top: diff} of each file of the project that changed from revision `a` (None: the
    empty tree, before the first commit) to `b` (None: the working tree, its untracked files included: those git does
    not ignore, which `git diff` leaves out): the view's diff(), or {"kind": "text", "do"} for a file of no view;
    {"kind", "error"} when a version cannot be read. `paths`: pathspecs from the project directory (none: no file)."""
    paths = list(paths)
    if not paths:                                   # git would take an empty pathspec for the whole repository
        return {}
    d = Path(project_dir)
    repo = _Repo(d)
    ca = repo.commit(a) if a is not None else repo.empty_tree()
    cb = repo.commit(b) if b is not None else None
    names = _git(d, "diff", "--name-only", "-z", "--no-renames", "--no-relative", ca, *([cb] if cb else []),
                 "--", *paths)
    if cb is None:                                  # a cell drawn since and not added yet is a change all the same
        names += _git(d, "ls-files", "-z", "--others", "--exclude-standard", "--full-name", "--", *paths)
    out: dict[str, dict] = {}
    with _Objects(repo.top) as objs:
        for p in sorted(set(filter(None, names.decode("utf-8", "surrogateescape").split("\0")))):
            kind = kind_of(p)
            try:                                    # one unreadable version: the other files still show
                old = objs.get(f"{ca}:{p}")
                new = objs.get(f"{cb}:{p}") if cb else (repo.top / p).read_bytes() if (repo.top / p).is_file() else None
                if old == new:                      # untracked, but as `a` has it (taken out of the index)
                    continue
                out[p] = diff(kind, old, new) if kind else \
                    {"kind": "text", "do": "add" if old is None else "del" if new is None else "mod"}
            except Exception as e:
                out[p] = {"kind": kind or "text", "error": f"{e.__class__.__name__}: {e}"}
    return out


# ---------------------------------------------------------------- text output (the command line)


def _short(v: Any, n: int = 60) -> str:
    if isinstance(v, dict):
        s = " ".join(f"{k}={_short(x, n)}" for k, x in v.items())
    elif isinstance(v, str):
        s = v
    else:
        s = json.dumps(v)
    s = s.replace("\n", "\\n")
    return s if len(s) <= n else s[:n - 3] + "..."


def _flat(v: Any) -> bool:
    """A value worth printing on one line: a scalar, or a record of scalars (a placement); not a summary of shapes."""
    scalar = (str, int, float, bool, type(None))
    return isinstance(v, scalar) or isinstance(v, dict) and all(isinstance(x, scalar) for x in v.values())


def format_diff(files: dict[str, dict]) -> str:
    """diff_revs / diff_files results as lines: + added, - removed, ~ changed."""
    lines = []
    for path, d in files.items():
        lines.append(f"{path}  ({d['kind']})")
        if "error" in d:
            lines.append(f"  ! {d['error']}")
            continue
        if d["kind"] == "text":
            lines.append(f"  {_SIGN[d['do']]} text (git diff shows it)")
            continue
        for c in d.get("changes", []):
            vals = f": {_short(c['before'])} -> {_short(c['after'])}" \
                if c["do"] == "mod" and _flat(c["before"]) and _flat(c["after"]) else ""
            lines.append(f"  {_SIGN[c['do']]} {c['what']}{vals}")
        for g in d.get("geometry", []):
            lines.append(f"  xor {g['cell']} {g['layer']}: {g['xor_um2']:g} um2"
                         + (" (same area)" if g["geometry_unchanged"] else ""))
        if not d.get("changes"):
            lines.append("  no change")
    return "\n".join(lines)


def format_check(r: dict) -> str:
    """merge_check's result as lines."""
    if r["commit"]:
        where = f"merge commit {r['commit'][:12]}"
    else:
        where = f"{r['operation']} in progress" if r.get("operation") else "conflicts in the index"
    theirs = ", ".join(p[:12] for p in r["parents"][1:]) if r.get("parents") else \
        r["theirs"][:12] if r["theirs"] else "unknown"
    lines = [f"{where}: ours {r['ours'][:12]}, theirs {theirs}"]
    lines += [f"note: {n}" for n in r.get("notes", [])]
    if r["unmerged"]:
        lines.append(f"conflicted files ({len(r['unmerged'])}):")
        for u in r["unmerged"]:
            if u.get("error"):
                lines.append(f"  {u['path']}  merge failed: {u['error']}")
            elif u["conflicts"] is None:
                lines.append(f"  {u['path']}  git's conflict (not merged by Undine)")
            elif not u["conflicts"]:
                lines.append(f"  {u['path']}  resolved: git add {shlex.quote(':/' + u['path'])}")
            for c in u["conflicts"] or []:
                lines.append(f"  {u['path']}  {c['id']}  {c['what']}: ours {_short(c['ours'], 40)}, "
                             f"theirs {_short(c['theirs'], 40)}")
    elif not r["commit"]:
        lines.append("no conflicted files")
    if r["warnings"]:
        lines.append(f"warnings ({len(r['warnings'])}):")
        lines += [f"  {w['path']}  {w['warning']}" for w in r["warnings"]]
    lines += [f"no merge report for {p} (merged in another clone or by git's octopus strategy, or the report is "
              "gone): its warnings are unknown" for p in r.get("no_report", [])]
    if r["verify"]:
        lines.append(f"verify (DRC / LVS), {len(r['verify'])} cell{'s' * (len(r['verify']) != 1)}:")
        lines += [f"  {v['lib']}/{v['cell']}  {v['why']}" for v in r["verify"]]
    elif r["theirs"] is None:                       # never "nothing to verify" when it could not be computed
        lines.append("cells to verify: not known without a commit for theirs")
    else:
        lines.append("no cell to verify: every cell is as one side has it")
    return "\n".join(lines)
