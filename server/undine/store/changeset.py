"""Changesets: a reviewable, revertible unit of change, on a git branch (docs/design/changeset-branches.md).

An agent (or a person) that wants to change a design first opens a changeset. That makes a commit of the project's
files as they are (the base: uncommitted edits included, Undine's own output and the changeset records left out),
the branch undine/<project>/<id> at it, and a git worktree of that branch, <root>/.changesets/<project>/<id>/work:
the *working copy*, a normal project named `<project>~<id>` (the project's directory in the worktree, when the
project is a subdirectory of its repository). Every API (editing, netlisting, simulation, DRC, LVS) works on it, and
every edit is recorded in its operation log with the actor and the reason. The base is checked out as well, detached,
at .../<id>/base: the baseline `<project>~<id>~base`, whose libraries are all read-only (Baseline). The real design is
not touched until a human applies the changeset:

    open  --apply-->  applied  --revert-->  reverted
     |
     +--discard--> discarded

* repository: a project in none is made one (git-setup, then the project's first commit on the user's branch: its
  files as they are, as the base would have them, made by the opener, the base's parent, so that what is applied
  later is committed; branches.first_commit, a log's unsaved layout edits committed as the base has them, as a
  journal; "git_setup", "first_commit" and "first_commit_note" in the answer); a project in a
  repository not set up for Undine is refused (branches.ensure_repo; NotSetUp, HTTP 400 not_set_up with the
  repository's top and the project). So is a project with a library a worktree cannot hold (UnsupportedLibrary,
  HTTP 400 unsupported_library), before anything is set up (branches.check_libraries), and for what only the tree
  shows (the first commit's, the base's), when it is made: the repository made for the open is taken back then.
* log: the worktree's logs start as the project's were; the changeset's records are those after each library's
  `base_seq` (in the record): numbering, the timeline, restore, the review's operations, undo and redo see only those
  (oplog.py's floor, live.LogHooks), and the project's unsaved layout edits at the base are not replayed in the
  working copy, which starts from the files. The floor of numbering, the snapshots of batch and restore and the tip's
  state (tip.json) live beside the worktrees, in .../<id>, never in them. The records after the base are parsed once
  per change of a log file (_records_after): a poll of the activity, the review and the timeline does not parse the
  project's whole history again.
* commits (_commit_tip): the working copy is committed onto the branch, its open layouts saved first, when the
  writer's lease ends (Server.lease_ended), before the checks run, and before it is applied or discarded. Not while
  someone else holds the lease: its end commits. While open the branch's tip is in tip.json (the record has other
  writers), once closed in the record.
* review: the recorded operations (net of undo/redo) grouped by cell, the checks run on the working copy and on the
  baseline (so a regression -- LVS matched before, fails now -- is visible), and the semantic diff of the base against
  the working copy (diff), which shows what no operation recorded too: what an apply merges.
* apply (_merge): the working copy committed (the tip), the evidence archived, then the semantic three-way merge
  (branches.plan_merge) of the base, the project's files now and the tip, all or nothing, with the project's editors
  (its intent and RTL ones too, made if none is open, so an edit that comes meanwhile waits), layouts and logs held:
    - the checks across files (_symbol_conflicts): each member view of a symbol transaction of the changeset must be
      in the project as it was at the base, and no schematic placed since that uses its symbol (one the transaction
      does not cover, "late") may need its wiring to follow it; a file merge cannot see either;
    - any conflict: nothing is written, MergeConflict (HTTP 409 merge_conflict, per file {path, kind, conflicts});
    - the project's layouts with unsaved edits in the libraries the merge touches are saved first (recorded; put back
      if the merge then fails), so the merge takes part in them as files;
    - the logs are not merged by lines nor by logmerge (_logs): the project's log as it is, the changeset's records
      after its base (tagged with it, renumbered: "merged_seq"), then a `merge` record naming the changeset, after
      which undo starts again (an applied changeset is taken back with Revert, not with undo). The changeset's
      records are its tip's log, or, when the tip has none (the project's .gitignore leaves history/ out of git),
      its working copy's log up to the tip (the gseq it was committed at); a revert's, its tip's or those its apply
      put in the project's log;
    - written as a compare-and-swap: each file is still what the plan merged as the project's (an edit that did not
      wait for the locks, say from another process, came meanwhile: MergeConflict, nothing written, apply again);
    - the merged files and logs written at once (branches.write_plan), the merged schematics' netlists read again
      (netlist.sync: the watcher records nothing), the project's open layouts and editors read the files again;
    - a merge commit on the user's branch (parents HEAD and the tip) when the project was clean before
      (branches.is_clean) and the branch has a commit; otherwise the merge stays in the working tree ("uncommitted",
      and "why": "no_commit", the branch has none yet, History makes the project's first commit; "changes", the
      project had other uncommitted changes; "failed", the commit failed, or Undine stopped before it: _left). A file
      git ignores and does not track (an ignored log) stays out of it.
* an interrupted merge: before writing, <project>/changesets/<id>/merging.json says what the merge writes (each
  file's digest before and after, a log's size) and how to close it; it says when all of it is written, and goes
  once the record says applied (reverted). A server killed meanwhile leaves it: `undine serve` finishes it before
  the netlist watcher and the API start (finish_interrupted), as the next access of the changeset does (_load, the
  sweep): the closure when all is written (every file the merged one; a log starting with it, records appended
  since), else what was renamed in is put back (write_plan's .old files; a log keeps the records appended since)
  and the changeset is left as it was, its record saying so ("interrupted"). The log is never merged twice.
* the sweep (once per project and server, at its first access): an interrupted merge as above; a branch
  undine/<project>/<id> or a worktree of a changeset that is closed (a server killed after the record said so: its
  closure is done now) or that has no record (killed while it was opened: committed, then kept as the ref below,
  never deleted); then git worktree prune. A working copy a closure kept on purpose ("retained") is left alone. The
  evidence a server killed while saving it left staged (<project>/changesets/<id>/.evidence-*) is removed.
* revert: the same merge from the tip to the base commit, the files the changeset added kept (a cell it made may be
  used elsewhere since) and so the project.json entry of a library it made that still has files (_keep_libraries),
  the symbol checks the other way round (members as at the tip, no "late" consumer); the log is not merged: the
  libraries it touches get a `merge` record naming the revert. Committed or not, as for apply. It needs no
  worktree: the commits are kept.
* closing (apply, discard): the libraries the working copy made are added to the record's "libs", then the evidence
  is archived (the runs of both sides as hard links to their files, a copy of one that may still be written, the
  manifest with each file's hash and stamp and the base and tip commits: _archive), the worktrees are removed and
  the branch is kept as refs/undine/changesets/<project>/<id> (_close_git). A late edit keeps the worktrees
  ("retained": the review offers the preview while it resolves). While a changeset is applied, discarded or
  reverted here, the list, the review and the screens' /live poll say how far ("closing": live.Closings; waiting for
  another closure, committing, saving the evidence n of N bytes, merging, cleaning up); a second closure of it
  meanwhile is refused (Busy).
* evidence afterwards: its size (evidence_sizes, the record's "evidence" in get; what a kept working copy still holds
  too); a person can prune the waveforms of its runs (prune_evidence), the manifest keeping their hashes and saying
  what was pruned, when and by whom.
* older changesets: a copy under .../<id>/{work,base}, no "git" in its record. One still open can be reviewed and
  discarded; applying it says to open the proposal again as a new changeset. One applied before cannot be reverted.

The record of each changeset is <project>/changesets/<id>.json (left out of every commit Undine makes):
    {"id", "project", "title", "reason", "actor", "created", "status", "preview", "baseline", "libs": [lib ...],
     "git": {"branch", "base", "head" (the user's HEAD at open), "prefix", "base_seq": {lib: seq}, "tip", "ref",
             "commits"},
     "base": {lib: {}} (the libraries, for older readers), "verify": [...],
     "evidence"?: {"path", "captured", "jobs", "bytes", "pruned"?: {"t", "by", "files", "bytes"}}, "cleanup_warning"?,
     "retained"? (the closure kept the working copy), "interrupted"? (a merge a server killed was taken back: the
     note, in cleanup_warning too), ...,
     "applied" / "reverted": {"t", "by", "commit": the commit on the user's branch | null, "uncommitted": the merge
                              was left in the working tree, "why": why ("no_commit", "changes", "failed"; null when
                              committed; absent in an older record), "paths": what it wrote, "tip": the commit
                              applied, "kept" (revert): the files the changeset added, kept}}
"""
from __future__ import annotations

import contextlib
import dataclasses
import errno
import fcntl
import fnmatch
import glob
import hashlib
import json
import os
import re
import shutil
import stat as statmod
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from functools import wraps
from pathlib import Path

from undine.store import cas, deps, oplog, provenance
from undine.layouts import layout, layoutstore
from undine.server import live
from undine.schematics import netlist, symbolsync, xschem
from undine.schematics.schedit import SchEditor
from undine.agents.llm_context import _eff as llm_effect
from undine.store.oplog import OpLog
from undine.store.project import Project
from undine.simulation import sim
from undine.simulation.sim import Job
from undine.vcs import branches, gitmerge, logmerge


class ChangesetError(Exception):
    pass


class MergeConflict(ChangesetError):
    """An apply or a revert whose merge conflicts: nothing was written. `files`: [{"path" (from the project
    directory), "kind", "conflicts": [{"id", "what", "base", "ours", "theirs", ...}]}] (api.py: 409 merge_conflict)."""

    def __init__(self, message: str, files: list[dict]):
        super().__init__(message)
        self.files = files


class Busy(ChangesetError):
    """The changeset is being applied, discarded or reverted already (live.Closings), or is being closed or reverted
    while its evidence is to be pruned (api.py: 409 busy)."""


class NotSetUp(ChangesetError):
    """Opening a changeset in a git repository not set up for Undine (branches.NotSetUp); `top`: the repository's
    top, `project`: the project, `command`: the command line that sets it up (`undine git-setup <project>`, with
    `--root` in a store not the command's default) (api.py: 400 not_set_up, with the three)."""

    def __init__(self, message: str, top: Path | None, project: str | None = None, command: str | None = None):
        super().__init__(message)
        self.top, self.project, self.command = top, project, command


class UnsupportedLibrary(ChangesetError):
    """Opening a changeset of a project with a library a worktree cannot hold (branches.Unsupported): `library`,
    `path` (from the project directory), `kind` ("link_out", "link_nowhere", "own_repository"), `target` (where the
    link leads; None for a repository) (api.py: 400 unsupported_library, with them)."""

    def __init__(self, message: str, library: str, path: str, kind: str | None, target: str | None):
        super().__init__(message)
        self.library, self.path, self.kind, self.target = library, path, kind, target

    @classmethod
    def of(cls, e: branches.Unsupported) -> "UnsupportedLibrary":
        return cls(f"cannot open a changeset: {e}", e.library, e.path, e.kind, e.target)


_SYMBOL_GROUP_FIELDS = ("symbol_group", "symbol_before", "symbol_after")
CID = re.compile(r"cs-[0-9a-f]{6}")
EVIDENCE = 2                          # the evidence manifest of a changeset on git: the runs, and the commits
RAW_OUTPUTS = ("*.raw", "*.vcd", "*.fst")     # an archived run's waveforms: what a person can prune (prune_evidence)
HASHERS = 4                           # threads that hash the evidence (hashlib and the reads let go of the GIL)
# a hard link refused (another file system; one that has none; the system's rules on links): the file is copied
_NO_LINK = {errno.EXDEV, errno.EPERM, errno.EACCES, errno.EMLINK, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}
_VERB = {"apply": "applied", "discard": "discarded", "revert": "reverted"}
OLD_FORM = ("changeset {cid} is a copy made before changesets were git branches: it can be reviewed and discarded; "
            "to apply it, open the proposal again as a new changeset")


def branch_of(project: str, cid: str) -> str:
    return f"undine/{project}/{cid}"


def kept_ref(project: str, cid: str) -> str:
    return f"refs/undine/changesets/{project}/{cid}"


def _serialized(fn):
    """Serialize proposal metadata writes without holding layout/editor cache locks."""
    @wraps(fn)
    def call(self, *args, **kwargs):
        with self.srv.changeset_lock:
            return fn(self, *args, **kwargs)
    return call


def _lib_dirs(prj) -> dict[str, Path]:
    """Project libraries that live inside the project directory (the ones a changeset can change)."""
    return {name: prj.path / spec["path"] for name, spec in prj.library_specs().items()
            if "path" in spec and not Path(spec["path"]).expanduser().is_absolute()}


def _digest(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha1(data).hexdigest()


def _file_digest(p: Path) -> str | None:
    """The digest of the file's bytes now; None when there is no file there (a directory neither)."""
    try:
        return _digest(p.read_bytes()) if p.is_file() else None
    except OSError:
        return None


# ---------------------------------------------------------------- the files of a changeset's evidence


def _walk(top: Path) -> tuple[list[str], list[tuple[str, Path, bool, os.stat_result]]]:
    """The folders and files under `top` as shutil.copytree copies them (symbolic links followed; a link that leads
    nowhere, a pipe or a socket left out), sorted: ([folder from top], [(file from top, its path, whether a symbolic
    link leads to it, its stat)])."""
    folders, files = [], []
    via = {".": False}
    for d, subs, names in os.walk(top, followlinks=True):
        rd = os.path.relpath(d, top)
        subs.sort()
        for s in subs:
            r = os.path.normpath(os.path.join(rd, s))
            via[r] = via[rd] or os.path.islink(os.path.join(d, s))
            folders.append(r)
        for n in sorted(names):
            p = os.path.join(d, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if statmod.S_ISREG(st.st_mode):
                files.append((os.path.normpath(os.path.join(rd, n)), Path(p), via[rd] or os.path.islink(p), st))
    return folders, files


def _sha(path: Path, progress: live.Closing) -> str:
    """provenance.file_sha, the bytes read counted in `progress`."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
            progress.add(bytes=len(b))
    return h.hexdigest()


def _copy(src: Path, dst: Path, progress: live.Closing) -> str:
    """shutil.copy2 that hashes what it copies: the copy's provenance.file_sha without reading it again, the bytes
    counted in `progress` as they are copied."""
    h = hashlib.sha256()
    with open(src, "rb") as f, open(dst, "wb") as out:
        while b := f.read(1 << 20):
            h.update(b)
            out.write(b)
            progress.add(bytes=len(b))
    shutil.copystat(src, dst)
    return h.hexdigest()


def _dep_copy(src: str, dst: str) -> None:
    """copytree's copy_function for .undine/deps: a dependency's files are read-only and changed only by replacing
    them, so the worktree hard-links them (no disk) or, where that is refused, copies them (cas.link_or_copy, so that
    UNDINE_STORE_LINK means the same here as for the store). The marker is a file of its own: copied."""
    if os.path.lexists(dst):                 # a file git checked out there: replaced, as copy2 would
        os.unlink(dst)
    if os.path.basename(src) == deps.MARKER:
        shutil.copy2(src, dst)
    else:
        cas.link_or_copy(Path(src), Path(dst))


def _ended_by_itself(status: dict) -> bool:
    """Whether a run's job ended by itself (done, or failed short of its time limit), not stopped from outside
    (cancelled, at its time limit: its process killed, what that process started perhaps not; an adopted ngspice
    killed while no server watched it: "cut", whatever its log says) nor left unfinished."""
    return status.get("state") in ("done", "failed") and not status.get("timed_out") and not status.get("cut") \
        and "timed out" not in str(status.get("error") or "") and status.get("error") != sim.CUT


def _is_raw(rel: str) -> bool:
    """Whether the file at `rel` in an evidence folder is a run's waveform (RAW_OUTPUTS): `<side>/runs/<id>/...`."""
    parts = rel.split("/")
    return len(parts) > 3 and parts[1] == "runs" and any(fnmatch.fnmatch(parts[-1], pat) for pat in RAW_OUTPUTS)


def _evidence_size(d: Path) -> dict:
    """{"bytes", "raw": of them, the waveforms (RAW_OUTPUTS), "files", "shared", "raw_shared": of the bytes and of the
    waveforms, those of a file another name still holds (a working copy a closure kept, its runs linked: removing the
    evidence's name frees nothing until that one goes too)} of the evidence folder `d` as it is now."""
    total = raw = n = shared = raw_shared = 0
    for top, _, names in os.walk(d):
        rd = Path(top).relative_to(d).as_posix()
        for name in names:
            try:
                st = os.lstat(os.path.join(top, name))
            except OSError:
                continue
            is_raw, more = _is_raw(f"{rd}/{name}"), st.st_nlink > 1
            n, total, shared = n + 1, total + st.st_size, shared + (st.st_size if more else 0)
            raw += st.st_size if is_raw else 0
            raw_shared += st.st_size if is_raw and more else 0
    return {"bytes": total, "raw": raw, "files": n, "shared": shared, "raw_shared": raw_shared}


# ---------------------------------------------------------------- the records of a log after its floor, parsed once


@dataclasses.dataclass(frozen=True)
class _Parsed:
    """A log file's records after a floor as _records_after last read them."""
    ident: tuple                    # (st_dev, st_ino): another file renamed in at the path is read anew
    floor: int
    stat: tuple                     # (st_size, st_mtime_ns) before it was read
    end: int                        # the bytes parsed: up to the end of the last whole line
    tail: bytes                     # the bytes just before `end`: a file only appended to still has them there
    recs: tuple


_PARSED: dict[Path, _Parsed] = {}
_PARSED_LOCK = threading.Lock()
_NL = re.compile(b"\n")


def _parse_lines(data: bytes, floor: int) -> list[dict]:
    """The records of whole lines, after `floor` (0: all); a line that is no record is skipped, as OpLog does."""
    out = []
    for line in data.split(b"\n"):
        if line.strip():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict) and (not floor or (r.get("seq") or 0) > floor):
                out.append(r)
    return out


def _floor_start(data: bytes, end: int, floor: int) -> int:
    """Where the line of the first record after `floor` starts in data[:end], by bisection of the lines (a log only
    grows: its seqs ascend), a few of them parsed; 0 (everything is parsed) when one it looks at is no record."""
    starts = [0] + [m.end() for m in _NL.finditer(data, 0, end)]
    if starts[-1] >= end:
        starts.pop()
    lo, hi = 0, len(starts)
    while lo < hi:
        mid = (lo + hi) // 2
        line = data[starts[mid]:starts[mid + 1] if mid + 1 < len(starts) else end]
        try:
            seq = json.loads(line)["seq"]
            if not isinstance(seq, int):
                return 0
        except (ValueError, KeyError, TypeError):
            return 0
        if seq > floor:
            hi = mid
        else:
            lo = mid + 1
    return starts[lo] if lo < len(starts) else end


def _records_after(path: Path, floor: int) -> list[dict]:
    """The records of the log file `path` after `floor` (OpLog.own's), parsed once per change of the file: a later
    read parses only the lines appended since (the same file, grown, still ending as it did where it was read up
    to), and the first one finds the floor by bisection instead of parsing the records before it (a working copy's
    log starts with the project's whole history, which the activity, the review and the timeline polls would parse
    each time). The records are shared: callers do not change them."""
    try:
        st = path.stat()
    except OSError:
        return []
    ident, now = (st.st_dev, st.st_ino), (st.st_size, st.st_mtime_ns)
    with _PARSED_LOCK:
        c = _PARSED.get(path)
    if c is not None and (c.ident, c.floor) != (ident, floor):
        c = None
    if c is not None and c.stat == now:
        return list(c.recs)
    try:
        data = None
        if c is not None and st.st_size >= c.end:
            with path.open("rb") as fh:
                fh.seek(c.end - len(c.tail))
                more = fh.read()
            if more.startswith(c.tail):
                data, base, recs = more, c.end - len(c.tail), list(c.recs)
                start = len(c.tail)
        if data is None:
            data, base, recs, start = path.read_bytes(), 0, [], 0
    except OSError:
        return []
    cut = data.rfind(b"\n") + 1
    if not recs and start == 0 and floor:
        start = _floor_start(data, cut, floor)
    recs += _parse_lines(data[start:cut], floor)
    parsed = _Parsed(ident, floor, now, base + cut, data[max(0, cut - 64):cut], tuple(recs))
    with _PARSED_LOCK:
        _PARSED[path] = parsed
    return recs + _parse_lines(data[cut:], floor)       # a last line being written, when it is whole already


def _forget_parsed(under: Path) -> None:
    with _PARSED_LOCK:
        for k in [k for k in _PARSED if k.is_relative_to(under)]:
            del _PARSED[k]


# ---------------------------------------------------------------- what an operation of this process is opening or closing


_BUSY: dict[str, int] = {}          # shadow root -> operations of this process on it (create, apply, revert, discard)
_BUSY_FD: dict[str, int] = {}       # shadow root -> the open lock file this process holds for them
_BUSY_LOCK = threading.Lock()


def _lock_file(root: Path) -> Path:
    """`<store>/.changesets/<project>/<id>.lock`, next to the shadow root (which a closure removes); the folder made to
    ignore itself first, as a worktree's (an older copy's closure may be the first to make it)."""
    branches._ignore_shadow(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    return root.parent / f"{root.name}.lock"


@contextmanager
def _busy(root: Path):
    """Marks a changeset (its shadow root) as being opened or closed here: the sweep and the recovery of an
    interrupted merge leave it alone (what they find is this operation's work in progress, not a dead server's). Also
    across processes: the first operation holds an OS lock on the changeset's lock file, which the kernel releases
    when the process dies, so a server started while the one before still finishes an apply (a restart) waits for it
    instead of taking its merge for an interrupted one."""
    k = str(root)
    with _BUSY_LOCK:
        first = k not in _BUSY
        _BUSY[k] = _BUSY.get(k, 0) + 1
    try:
        if first:
            fd = os.open(_lock_file(root), os.O_RDWR | os.O_CREAT, 0o644)
            with _BUSY_LOCK:
                _BUSY_FD[k] = fd
            fcntl.flock(fd, fcntl.LOCK_EX)       # another process's operation on it: wait for it
        yield
    finally:
        with _BUSY_LOCK:
            _BUSY[k] -= 1
            last = not _BUSY[k]
            if last:
                del _BUSY[k]
                fd = _BUSY_FD.pop(k, None)
        if last and fd is not None:
            os.close(fd)                         # releases the lock


def _is_busy(root: Path) -> bool:
    """Whether an operation of this process, or of another one still alive, is opening or closing the changeset."""
    with _BUSY_LOCK:
        if str(root) in _BUSY:
            return True
    f = root.parent / f"{root.name}.lock"
    if not f.is_file():
        return False
    fd = os.open(f, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return False
    except BlockingIOError:
        return True
    finally:
        os.close(fd)


_LEFTOVER = re.compile(r"\.(?P<name>.+)\.[0-9a-f]{12}\.(?P<kind>new|old)")


def _leftovers(target: Path, kind: str | None = None) -> list[Path]:
    """The files branches.write_plan leaves next to `target` when it is interrupted: its new file (".<name>.<token>
    .new") and the file it moved aside (".old")."""
    if not target.parent.is_dir():
        return []
    out = []
    for p in target.parent.glob(f".{glob.escape(target.name)}.*"):
        m = _LEFTOVER.fullmatch(p.name)
        if m and m["name"] == target.name and kind in (None, m["kind"]) and p.is_file():
            out.append(p)
    return sorted(out)


def _last_seq(log: Path) -> int:
    recs = OpLog(log)._read_all()
    return recs[-1]["seq"] if recs else 0


def _state(prj) -> list[str]:
    """What a batch or a restore may change in a working copy, as names in its project directory: the top directories
    of its libraries (libs/ ...), its logs, project.json, undine.lock and the flow configurations (flows/)."""
    from undine.design.librelane import FLOWS_DIR
    names = {"libs", "history", "project.json", deps.LOCK, FLOWS_DIR}
    for src in _lib_dirs(prj).values():
        parts = Path(os.path.relpath(src, prj.path)).parts
        if parts and parts[0] not in (".", ".."):
            names.add(parts[0])
    return sorted(names)


def _pending(log: OpLog) -> bool:
    """Whether the log has unsaved layout edits: a journal its LayoutDB replays when it opens the library."""
    return bool(layout._journal(log.read(limit=10**9))[1])


def _blob(d: Path, prefix: str, rev: str, rel: str) -> bytes | None:
    """The file `rel` (from the project directory `d`, `prefix` in its repository) in commit `rev`; None: it has none."""
    r = branches._run(d, "cat-file", "blob", f"{rev}:{prefix + '/' if prefix else ''}{rel}")
    return r.stdout if r.returncode == 0 else None


def _text(data: bytes | None) -> str | None:
    return None if data is None else data.decode("utf-8", "replace")


def _put(p: Path, data: bytes) -> None:
    """Writes the file anew (another inode: a layout sees its fingerprint change, whatever the clock's resolution)."""
    tmp = p.with_name(f".{p.name}.{uuid.uuid4().hex[:8]}.back")
    tmp.write_bytes(data)
    os.replace(tmp, p)


def _whole(cid: str, what: str, base: bytes | None, ours: bytes | None, theirs: bytes | None) -> dict:
    """A conflict about a whole file, as branches' plan gives them: the sides' states ("changed", "deleted", "added")."""
    def state(data):
        return "deleted" if data is None else "added" if base is None else "changed"
    return {"id": cid, "what": what, "base": None if base is None else "file", "ours": state(ours),
            "theirs": state(theirs), "taken": "ours", "resolved": False}


def _add(plan: dict, path: str, conflict: dict) -> None:
    """Adds a conflict to the plan's entry of `path` (made if it has none)."""
    entry = next((c for c in plan["conflicts"] if c["path"] == path), None)
    if entry is None:
        entry = {"path": path, "kind": gitmerge.kind_of(path) or "text", "conflicts": []}
        plan["conflicts"].append(entry)
    entry["conflicts"].append(conflict)


@dataclasses.dataclass(frozen=True)
class Preview:
    """Where a changeset's working copy and baseline are (Server.project), and what their logs start from."""
    shadow: Path                    # <root>/.changesets/<project>/<id>: the worktrees, the floor, tip.json, snapshots
    work: Path                      # the working copy's project directory
    base: Path                      # the baseline's
    base_seq: dict                  # {lib: the seq of its last record at the base} ({}: an older copy)
    floor_dir: Path                 # where the floor of numbering is (live.floor)


def preview(root: Path, project: str, cid: str) -> Preview:
    """The preview of changeset `cid` of `project` (KeyError: there is none), from its record."""
    rec = root / project / "changesets" / f"{cid}.json"
    if not CID.fullmatch(cid) or not rec.is_file():
        raise KeyError(f"no changeset preview {project}~{cid}")
    meta = json.loads(rec.read_text())
    shadow = root / ".changesets" / project / cid
    g = meta.get("git")
    if not g:                       # an older copy: the floor in its history/, as it was
        return Preview(shadow, shadow / "work", shadow / "base", {}, shadow / "work" / "history")
    prefix = g.get("prefix") or ""
    return Preview(shadow, shadow / "work" / prefix, shadow / "base" / prefix, dict(g.get("base_seq") or {}), shadow)


class Working(Project):
    """A changeset's working copy (`<project>~<id>`): the branch's worktree. A library whose folder is not in it (an
    absolute path in project.json, `~/...`, or one that climbs out) is the real library itself: read-only here, or the
    proposal would write straight into it, as dependencies are."""

    def libraries(self):
        here = self.path.resolve()
        return {n: lib if lib.readonly or lib.path.resolve().is_relative_to(here)
                else dataclasses.replace(lib, readonly=True) for n, lib in super().libraries().items()}


class Baseline(Project):
    """A changeset's baseline (`<project>~<id>~base`): the base commit as checked out, never written. Every library is
    read-only, so no journal is replayed, nothing is saved, and no route edits it."""

    def libraries(self):
        return {n: lib if lib.readonly else dataclasses.replace(lib, readonly=True)
                for n, lib in super().libraries().items()}

    def save(self) -> None:
        raise PermissionError("the proposal baseline is read-only")

    def create_library(self, name: str):
        raise PermissionError("the proposal baseline is read-only")


class Changesets:
    def __init__(self, srv):
        self.srv = srv

    # ------------------------------------------------------------------ paths and records

    def _dir(self, project: str) -> Path:
        d = self.srv.project(project.split("~", 1)[0]).path / "changesets"    # the real project's, from a preview too
        d.mkdir(exist_ok=True)
        return d

    def _shadow_root(self, project: str, cid: str) -> Path:
        return self.srv.store.root / ".changesets" / project / cid

    def _load(self, project: str, cid: str) -> dict:
        """The record; first, what a server killed mid-way left of the project's changesets is tidied (_sweep), and
        of this one (an interrupted merge: _recover)."""
        self._sweep(project)
        d = self._dir(project)
        if CID.fullmatch(cid) and (d / cid / "merging.json").is_file():          # _merging_file
            self._recover(project, cid)
        p = d / f"{cid}.json"
        if not p.is_file():
            raise KeyError(f"no changeset {cid}")
        return json.loads(p.read_text())

    def _save(self, project: str, meta: dict) -> dict:
        path = self._dir(project) / f"{meta['id']}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(meta, indent=2) + "\n")
        tmp.replace(path)
        return meta

    def list(self, project: str) -> list[dict]:
        """The records, newest first; "closing": how far its closure is, while one is being applied, discarded or
        reverted here (live.Closings.public)."""
        self._sweep(project)
        out = []
        records = [json.loads(p.read_text()) for p in self._dir(project).glob("*.json")]
        for m in sorted(records, key=lambda m: (m.get("created") or "", m.get("id") or ""), reverse=True):   # newest first
            row = {k: m.get(k) for k in ("id", "title", "reason", "actor", "created", "status", "preview")}
            if m.get("git"):
                row["git"] = self._git_info(project.split("~", 1)[0], m)
            if (closing := self.srv.closings.public(m.get("id") or "")):
                row["closing"] = closing
            out.append(row)
        return out

    def _libs(self, meta: dict) -> list[str]:
        """The libraries whose logs hold the changeset's records: the record's, and those its open working copy made."""
        libs = list(meta.get("libs") or meta.get("base") or [])
        if meta.get("git") and meta["status"] == "open":
            try:
                libs += [n for n in _lib_dirs(self.srv.project(meta["preview"])) if n not in libs]
            except (KeyError, ValueError):
                pass
        return libs

    # ------------------------------------------------------------------ the branch

    def _tip_file(self, project: str, cid: str) -> Path:
        return self._shadow_root(project, cid) / "tip.json"

    def _tip_state(self, project: str, cid: str) -> dict:
        """{"tip": the last commit of the working copy (None: none yet), "commits": how many, "gseq": the head when
        the working copy was last committed (or found as committed: nothing to commit), which the tip has}."""
        try:
            return json.loads(self._tip_file(project, cid).read_text())
        except (OSError, ValueError):
            return {}

    def _git_info(self, project: str, meta: dict) -> dict | None:
        """The record's "git" as the API gives it: {"branch", "base", "head", "prefix", "base_seq", "tip", "ref",
        "commits"}; the tip of an open changeset (or of one whose closure kept its worktree) from tip.json."""
        g = meta.get("git")
        if not g:
            return None
        out = {k: g.get(k) for k in ("branch", "base", "head", "prefix", "base_seq", "tip", "ref")}
        out["commits"] = g.get("commits") or 0
        if meta["status"] == "open" or not g.get("ref"):
            st = self._tip_state(project, meta["id"])
            if st:
                out.update(tip=st.get("tip"), commits=st.get("commits") or 0)
        return out

    def _tip_lock(self, meta: dict) -> threading.RLock:
        with self.srv.lock:
            return self.srv._cs_locks.setdefault(f"{meta['preview']}#tip", threading.RLock())

    def _layouts(self, name: str, libs) -> dict:
        """{lib: LayoutDB} of project `name`'s libraries `libs` that can have unsaved edits: those open, and those whose
        log has a journal (edits made before a restart), opened (which replays it)."""
        libraries = self.srv.project(name).libraries()
        out = {}
        for lib in sorted(libs):
            library = libraries.get(lib)
            if library is None or library.readonly or library.layout_file:
                continue
            with self.srv.lock:
                db = self.srv.dbs.get((name, lib))
            if db is None and _pending(self.srv.oplog(name, lib)):
                db = self.srv.db(name, lib)
            if db is not None:
                out[lib] = db
        return out

    def _save_layouts(self, meta: dict) -> None:
        """Saves the working copy's layouts with unsaved edits (recorded), so the commit has them as files: an edit
        left in a journal would not be in the tip's cell files, and an apply would not bring it."""
        for db in self._layouts(meta["preview"], self._libs(meta)).values():
            if db.dirty:
                with db.recording("save", {}, "system", None, meta["id"]):
                    db.save()

    @staticmethod
    def _message(meta: dict, recs: list[tuple[str, dict]]) -> str:
        """A commit of the working copy: the title, then who, the turns and the operations since the last commit."""
        lines = [meta["title"], "", f"Changeset {meta['id']} of {meta['project']}" +
                 (f": {meta['reason']}" if meta.get("reason") else "")]
        ops = [(lib, r) for lib, r in recs if r.get("effects") or r["op"] != "save"]
        actors = list(dict.fromkeys(r["actor"] for _, r in ops if r.get("actor")))
        turns = list(dict.fromkeys(r["turn"] for _, r in ops if r.get("turn")))
        lines += [f"Actor: {', '.join(actors)}"] if actors else []
        lines += [f"Turn: {', '.join(turns)}"] if turns else []
        if ops:
            lines += ["", f"Operations {ops[0][1]['gseq']}-{ops[-1][1]['gseq']}:"]
            for lib, r in ops[:50]:
                where = f"{lib}/{r['cell']} ({r.get('view')})" if r.get("cell") else lib
                lines.append(f"- {r['op']} {where}" + (f": {r['reason']}" if r.get("reason") else ""))
            lines += [f"- ... and {len(ops) - 50} more"] if len(ops) > 50 else []
        return "\n".join(lines) + "\n"

    def _commit_tip(self, project: str, meta: dict) -> str | None:
        """Commits the working copy onto the changeset's branch (module docstring), its open layouts saved first;
        returns the branch's tip (the base when nothing was ever committed). None for an older copy, and while
        someone other than the current writer holds the lease (its end commits)."""
        g = meta.get("git")
        if not g:
            return None
        cid = meta["id"]
        le = self.srv.leases.get(cid)
        if le and le.owner != live.writer():
            return None
        wt = self._shadow_root(project, cid) / "work"
        with self._tip_lock(meta):
            self._save_layouts(meta)
            state = self._tip_state(project, cid)
            recs = [(lib, r) for lib, r in self._all_records(meta) if r["gseq"] > (state.get("gseq") or 0)]
            actor = next((r["actor"] for _, r in reversed(recs) if r.get("actor") not in (None, "system")),
                         meta.get("actor") or "user")
            sha = branches.commit_worktree(wt, g.get("prefix") or "", self._message(meta, recs), actor)
            tip = branches.resolve(wt, f"refs/heads/{g['branch']}") or g["base"]
            head = max([r["gseq"] for _, r in recs], default=state.get("gseq") or 0)
            # the head moves on without a commit too (records whose files are the tip's: an edit undone, a log git
            # ignores): an apply merges the working copy's records up to it when its tip has no log (_records_of)
            if sha is not None or not state or head != state.get("gseq"):
                state = {"tip": None if tip == g["base"] else tip, "commits": (state.get("commits") or 0) + (sha is not None),
                         "gseq": head}
                f = self._tip_file(project, cid)
                tmp = f.with_name(f".tip.{uuid.uuid4().hex[:8]}.json")
                tmp.write_text(json.dumps(state) + "\n")
                tmp.replace(f)
            return tip

    def _commit_before(self, project: str, meta: dict, what: str) -> str | None:
        try:
            return self._commit_tip(project, meta)
        except (RuntimeError, ValueError, OSError) as e:    # git, a layout that cannot be saved (DiskConflict)
            raise ChangesetError(f"cannot commit the working copy before {what}: {e}") from e

    def commit(self, project: str, cid: str) -> str | None:
        """_commit_tip of an open changeset (a lease ended: Server.lease_ended)."""
        meta = self._load(project, cid)
        return self._commit_tip(project, meta) if meta["status"] == "open" else None

    def _close_git(self, project: str, meta: dict) -> None:
        """The end of an applied or discarded changeset on git: its worktrees removed, its branch moved to the kept ref
        (its commits stay, for the evidence and a revert), the record saved with the tip."""
        g, cid = meta["git"], meta["id"]
        d = self.srv.project(project).path
        root = self._shadow_root(project, cid)
        st = self._tip_state(project, cid)
        g["commits"] = st.get("commits") or 0
        try:
            for side in ("work", "base"):
                branches.remove_worktree(d, root / side)
            sha = branches.keep_ref(d, g["branch"], kept_ref(project, cid))
            g.update(tip=None if sha == g["base"] else sha, ref=kept_ref(project, cid))
        except (RuntimeError, ValueError, OSError) as e:
            g["tip"] = st.get("tip")
            meta["cleanup_warning"] = f"The branch {g['branch']} could not be kept as {kept_ref(project, cid)}: {e}"
        self._save(project, meta)
        if g.get("ref"):
            shutil.rmtree(root, ignore_errors=True)

    def diff(self, project: str, cid: str, lib: str | None = None, cell: str | None = None) -> dict:
        """The semantic diff of the changeset's base against its working copy while it is open (its files: what no
        operation recorded too), against the tip it was closed with afterwards; /git/diff's shape (branches.diff)."""
        meta = self._load(project, cid)
        g = meta.get("git")
        if not g:
            raise ChangesetError(f"changeset {cid} was made before changesets were git branches: it has no diff")
        if meta["status"] == "open" or not g.get("ref"):
            try:
                work = self.srv.project(meta["preview"]).path
            except KeyError:
                work = None
            if work is not None:
                return branches.diff(work, g["base"], "worktree", lib, cell)
        d = self.srv.project(project).path
        tip = (branches.resolve(d, g["ref"]) if g.get("ref") else None) or g.get("tip") or g["base"]
        return branches.diff(d, g["base"], tip, lib, cell)

    # ------------------------------------------------------------------ lifecycle

    def create(self, project: str, title: str, reason: str = "", actor: str = "user") -> dict:
        """Opens a changeset (module docstring): the record, with "git_setup": whether git-setup made the project a
        repository first, "first_commit": the project's first commit made then (None: none made), and
        "first_commit_note": what is left to do after it (git's index not reset to it), or why none was made (a
        commit of the user's made meanwhile); None when there is nothing to say."""
        if "~" in project:
            raise ChangesetError("a changeset cannot be opened inside another changeset")
        prj = self.srv.project(project)
        # what git-setup may change, to put back when only the tree (built after git init: the first commit's, the
        # base's) shows a library a worktree cannot hold: a refused open leaves no repository behind
        before = {n: (prj.path / n).read_bytes() if (prj.path / n).is_file() else None
                  for n in (".gitignore", ".gitattributes")}
        try:
            branches.check_libraries(prj.path)       # before git init: what the folders alone show
            setup = branches.ensure_repo(prj.path)["git_setup"]
        except branches.Unsupported as e:
            raise UnsupportedLibrary.of(e) from e
        except branches.NotSetUp as e:
            top = getattr(e, "top", None)
            if top is None:
                try:
                    top = branches.repo(prj.path).top
                except Exception:
                    top = None
            raise NotSetUp(str(e), top, project, getattr(e, "command", None) or branches.setup_command(prj.path)) from e
        self._sweep(project)
        cid = "cs-" + uuid.uuid4().hex[:6]
        root = self._shadow_root(project, cid)
        with _busy(root):                   # the sweep leaves its branch and worktrees alone until the record is saved
            try:
                return self._create(prj, project, cid, root, title, reason, actor, setup)
            except (UnsupportedLibrary, branches.Unsupported) as e:
                if setup:                   # the repository was made for this open: take it back
                    shutil.rmtree(prj.path / ".git", ignore_errors=True)
                    for n, data in before.items():
                        if data is None:
                            (prj.path / n).unlink(missing_ok=True)
                        else:
                            (prj.path / n).write_bytes(data)
                if isinstance(e, UnsupportedLibrary):
                    raise
                raise UnsupportedLibrary.of(e) from e

    def _create(self, prj, project: str, cid: str, root: Path, title: str, reason: str, actor: str, setup: bool) -> dict:
        branch = branch_of(project, cid)
        libs = _lib_dirs(prj)
        made: list[Path] = []
        try:
            root.mkdir(parents=True)
            # a repository git-setup made for this open: the project's first commit, the base's parent (an apply onto
            # a branch with no commit is left uncommitted), the logs as they are, as the base has them (an unsaved
            # layout edit's journal too: nobody is there to save it); refused (Unsupported), the repository goes too
            # (create); a commit of the user's made meanwhile is the base's parent instead
            first, note = None, None
            if setup:
                fc = branches.first_commit(prj.path, branches.first_message(project, setup=True), actor, journals=True)
                first, note = (fc["commit"], fc["note"]) if fc else \
                    (None, "a commit was made on its branch meanwhile: the changeset starts from it")
            with branches.lock(prj.path) as repo:
                base = branches.snapshot(prj.path, f"Base of {cid}: {title}\n\nThe project {project} as it was when "
                                                   "the changeset was opened, uncommitted edits included.\n", actor)
                for side, name in (("work", branch), ("base", None)):
                    made.append(root / side)
                    branches.add_worktree(prj.path, root / side, base, name)
            for side in ("work", "base"):
                top = root / side
                d = top / repo.prefix if repo.prefix else top
                if (prj.path / deps.DEPS).is_dir():          # ignored: the dependencies as installed
                    shutil.copytree(prj.path / deps.DEPS, d / deps.DEPS, copy_function=_dep_copy,
                                    ignore=shutil.ignore_patterns(".*.new*", "*.old"), dirs_exist_ok=True)
                if (prj.path / deps.LOCK).is_file() and not (d / deps.LOCK).exists():
                    shutil.copy2(prj.path / deps.LOCK, d / deps.LOCK)
                for src in libs.values():                    # git keeps no empty directory
                    lib_dir = Path(os.path.normpath(d / os.path.relpath(src, prj.path)))
                    if lib_dir.is_relative_to(top):
                        lib_dir.mkdir(parents=True, exist_ok=True)
            work = root / "work" / repo.prefix if repo.prefix else root / "work"
            base_seq = {p.stem: _last_seq(p) for p in sorted((work / "history").glob("*.jsonl"))}
        except Exception as e:
            for w in reversed(made):
                try:
                    branches.remove_worktree(prj.path, w)
                except Exception:
                    pass
            if branches.ref_exists(prj.path, branch):
                branches._git(prj.path, "update-ref", "-d", f"refs/heads/{branch}")
            shutil.rmtree(root, ignore_errors=True)
            if isinstance(e, branches.Unsupported):     # what only the tree shows (a link in a library's folder)
                raise UnsupportedLibrary.of(e) from e
            raise ChangesetError(f"cannot open a changeset: {e}") from e
        meta = {"id": cid, "project": project, "title": title, "reason": reason, "actor": actor,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "status": "open",
                "preview": f"{project}~{cid}", "baseline": f"{project}~{cid}~base", "libs": list(libs),
                "git": {"branch": branch, "base": base, "head": repo.head, "prefix": repo.prefix, "base_seq": base_seq,
                        "tip": None, "ref": None, "commits": 0},
                "base": {n: {} for n in libs}, "verify": [], "applied": None}    # "base": the libraries, for older readers
        self._save(project, meta)
        return {**meta, "git": self._git_info(project, meta), "git_setup": setup, "first_commit": first,
                "first_commit_note": note}

    def _check_open(self, meta: dict, want: str = "open") -> None:
        if meta["status"] != want:
            raise ChangesetError(f"changeset {meta['id']} is {meta['status']}, not {want}")

    # ------------------------------------------------------------------ closing: what the screens see meanwhile

    def _progress(self, cid: str) -> live.Closing:
        """The closure of `cid` under way here (live.Closings), or one nobody sees (a sweep finishing a closure)."""
        return self.srv.closings.get(cid) or live.Closing(cid, "", "", "")

    @contextmanager
    def _closing(self, project: str, cid: str, action: str, actor: str):
        """Notes the closure (`action`: "apply", "discard", "revert") while it waits for the changesets' lock and while
        it runs, for the screens (live.Closings); one asked while the changeset is being closed already is refused
        (Busy) rather than waiting to find it closed."""
        title = ""                                  # no record: _load says so
        if CID.fullmatch(cid):
            try:
                title = json.loads((self._dir(project) / f"{cid}.json").read_text()).get("title") or ""
            except (OSError, ValueError, AttributeError):
                pass
        c = self.srv.closings.begin(cid, title, action, actor)
        if c is None:
            now = self.srv.closings.public(cid) or {}
            verb = _VERB.get(now.get("action"), "closed")
            raise Busy(f"changeset {cid} is being {verb} already; wait for it to finish")
        try:
            yield c
        finally:
            self.srv.closings.end(c)

    def discard(self, project: str, cid: str, actor: str = "user") -> dict:
        """Closes the changeset without merging it: its evidence archived, its branch kept (_close_git); the record's
        "discarded" = {"t", "by"}. Seen as it waits and runs (_closing)."""
        with self._closing(project, cid, "discard", actor):
            return self._discard(project, cid, actor)

    @_serialized
    def _discard(self, project: str, cid: str, actor: str) -> dict:
        meta = self._load(project, cid)
        self._check_open(meta)
        self._check_writer(cid)
        self._check_merging(project, cid)
        progress = self._progress(cid)
        with _busy(self._shadow_root(project, cid)):
            progress.phase("committing")
            self._commit_before(project, meta, "discarding it")
            meta["libs"] = self._libs(meta)          # those its working copy made too, while it is open
            manifest = self._archive(project, meta)
            self._note(meta, None)                   # an interrupted apply's note: it is discarded now
            meta["status"] = "discarded"
            meta["discarded"] = {"t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "by": actor}
            self._save(project, meta)
            progress.phase("cleanup")
            self._cleanup(project, meta, manifest, discard=True)
        return {**meta, **({"git": self._git_info(project, meta)} if meta.get("git") else {})}

    def _job_status(self, path: Path) -> dict:
        # a simulation an earlier server left running is adopted when its ngspice still runs, or still run by the server
        # it is with (sim.Jobs.status, working); one no server works on cannot complete. Keep the original status file
        # intact.
        job = Job(path)
        status = self.srv.jobs.status(job)
        if status.get("state") in ("queued", "running") and not self.srv.jobs.working(job, status):
            return {**status, "state": "interrupted", "error": "job interrupted before completion"}
        return status

    def _ensure_idle(self, sources: list) -> None:
        """Refuses while a job of either side runs: its status says so and someone works on it (_job_status: this
        server, its own or one it adopted from an earlier server; another live server on the root, which runs or queues
        it, or which handed it over to this one), or its process or anything that process started is still there
        (sim.alive: a cancelled build's compiler, until the cancel has stopped it; an adopted ngspice's group too),
        also once the job has ended (sim.left: the group its status recorded; cancel_job stops it)."""
        jobs = self.srv.jobs
        for _, _, src in sources:
            for path in (src / "runs").glob("*/status.json"):
                job = jobs.live.get(path.parent.name)
                active_proc = job is not None and sim.alive(job.proc)
                if active_proc or self._job_status(path.parent).get("state") in ("queued", "running"):
                    raise ChangesetError(
                        "wait for the changeset's running jobs to finish before applying or discarding"
                        + ("" if job is not None or not jobs.others else
                           f" (job {path.parent.name} is worked on by the other Undine server on this projects root, "
                           "the one that holds its simulations; if it never ends there, apply or discard from that "
                           "server)"))
                if (rest := sim.left(Job(path.parent).status())) is not None:
                    raise ChangesetError(
                        f"wait for the changeset's running jobs to finish before applying or discarding: job "
                        f"{path.parent.name} has ended, but processes it started still run (process group {rest.pid}); "
                        "cancel_job stops them")

    @staticmethod
    def _copied_runs(sources: list) -> set[str]:
        """The runs whose files the evidence copies instead of linking (`<side>/runs/<id>`): one whose job did not end
        by itself (_ended_by_itself: what a killed process started may outlive it and write on) and one a process may
        write now, whichever (sim.writers: it works in the run, or holds a file of it open for writing). A copy is the
        run as it is then: a write while it is saved is seen as a change (the stamps), and one after the closure goes to
        the working copy's file, never the evidence's."""
        busy = sim.writers([src / "runs" for _, _, src in sources])
        out = set()
        for side, _, src in sources:
            runs = src / "runs"
            real = Path(os.path.realpath(runs))
            for d in sorted(runs.iterdir()) if runs.is_dir() else []:
                if d.is_dir() and (real / d.name in busy or not _ended_by_itself(Job(d).status())):
                    out.add(f"{side}/runs/{d.name}")
        return out

    # ------------------------------------------------------------------ the evidence

    @staticmethod
    def _evidence_files(root: Path) -> dict[str, str]:
        return {str(p.relative_to(root)): provenance.file_sha(p) for p in sorted(root.rglob("*")) if p.is_file()}

    @staticmethod
    def _evidence_dirs(meta: dict) -> tuple[str, ...]:
        """What is archived of each side besides project.json and undine.lock: the runs (which git ignores); an older
        copy's logs too (on git they are in the commits, with the project's whole history)."""
        return ("runs",) if meta.get("git") else ("runs", "history")

    def _source_files(self, sources: list, dirs: tuple[str, ...]) -> dict[str, str]:
        """The hashes of the files the evidence holds of each side, read now: what a manifest from before "sources"
        is compared with (_cleanup)."""
        files = {}
        for side, _, src in sources:
            for filename in ("project.json", deps.LOCK):
                if (src / filename).is_file():
                    files[f"{side}/{filename}"] = provenance.file_sha(src / filename)
            for dirname in dirs:
                files.update({f"{side}/{dirname}/{name}": sha
                              for name, sha in self._evidence_files(src / dirname).items()})
        return files

    @staticmethod
    def _evidence_tree(sources: list, dirs: tuple[str, ...]) -> tuple[list[str], list[tuple[str, Path, bool, os.stat_result]]]:
        """What the evidence holds of each side: ([folder], [(path in the evidence, the file, whether it may be a hard
        link, its stat now)]). project.json and undine.lock are copied (written in place), and so are an older copy's
        logs (appended to); a run's file may be linked, unless a symbolic link leads to it (it may be anything): it is,
        unless its run is one to copy (_copied_runs)."""
        folders, files = [], []
        for side, _, src in sources:
            folders.append(side)
            for filename in ("project.json", deps.LOCK):
                try:
                    st = os.stat(src / filename)
                except OSError:
                    continue
                if statmod.S_ISREG(st.st_mode):
                    files.append((f"{side}/{filename}", src / filename, False, st))
            for dirname in dirs:
                if not (src / dirname).is_dir():
                    continue
                sub, found = _walk(src / dirname)
                folders += [f"{side}/{dirname}"] + [f"{side}/{dirname}/{r}" for r in sub]
                files += [(f"{side}/{dirname}/{r}", p, dirname == "runs" and not via, st) for r, p, via, st in found]
        return folders, files

    def _stamps(self, sources: list, dirs: tuple[str, ...]) -> dict[str, list[int]]:
        """{path in the evidence: provenance.stamp} of the files the evidence holds of each side, as they are now."""
        return {rel: provenance.stamp(st) for rel, _, _, st in self._evidence_tree(sources, dirs)[1]}

    @staticmethod
    def _known_hashes(stage: Path, stamps: dict[str, list[int]]) -> dict[str, str]:
        """The hashes of staged run files that need no reading: those the run's status.json recorded when it finished
        (provenance.outputs) with the stamp the file still has (the same inode, size and time)."""
        known: dict[str, str] = {}
        runs: dict[str, dict] = {}
        for rel, st in stamps.items():
            parts = rel.split("/", 3)
            if len(parts) < 4 or parts[1] != "runs":
                continue
            run = "/".join(parts[:3])
            if run not in runs:
                try:
                    status = json.loads((stage / run / "status.json").read_text())
                    runs[run] = status if isinstance(status, dict) else {}
                except (OSError, ValueError):
                    runs[run] = {}
            files, recorded = runs[run].get("files") or {}, runs[run].get("stamps") or {}
            if isinstance(files, dict) and isinstance(recorded, dict) and isinstance(files.get(parts[3]), str) \
                    and recorded.get(parts[3]) == st:
                known[rel] = files[parts[3]]
        return known

    def _branch_tip(self, project: str, meta: dict) -> str | None:
        g = meta["git"]
        return branches.resolve(self.srv.project(project).path, f"refs/heads/{g['branch']}")

    def _archive(self, project: str, meta: dict) -> dict:
        """Saves the evidence before any merge or deletion. A failure leaves the proposal untouched.

        Archive all runs, including simulations and superseded checks, rather than only the jobs referenced by
        the latest verification. Freshness describes the proposal at closure, never the later real design. On git
        (manifest version 2) the commits of the base and of the reviewed state ("commits": {"base", "tip"}) stand for
        the design and its log; the tip must not move while the evidence is saved.

        No run is copied: each of its files is a hard link to the run's ("linked": how many; a copy where a link is
        refused, another file system, and for a file a symbolic link leads to), so gigabytes of waveforms are saved
        without writing them. Nothing writes a finished run's files in place (Job.set renames its status in, a job
        still running blocks the closure, and stopping a job stops what its process started: sim.Process), and the
        working copy goes with the closure: the evidence is then the only name of its runs. A run that may still be
        written all the same is copied (_copied_runs: its job was stopped from outside, or a process works in it), and
        so are project.json, undine.lock and an older copy's logs. Each file is read once, for its hash ("files"): a
        copy while it is made, a link by a few threads (HASHERS); a run's file whose stamp is still the one its status
        recorded with its hash is not read at all (_known_hashes). Whether the proposal changed meanwhile is told by
        the stamps of its files ("sources": {path: provenance.stamp} as saved), not by their bytes: the files and their
        stamps, the operations and the branch's tip are compared once all is saved, and the files again at the
        cleanup.
        The closure's progress (live.Closing): phase "evidence", files and bytes (copied or hashed) done of how many.
        """
        sources = [(side, meta[key], self.srv.project(meta[key]).path)
                   for side, key in (("base", "baseline"), ("work", "preview"))]
        self._ensure_idle(sources)
        progress = self._progress(meta["id"])
        progress.phase("evidence")
        dirs = self._evidence_dirs(meta)
        captured = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        manifest = {"version": EVIDENCE if meta.get("git") else 1, "changeset": meta["id"], "captured": captured,
                    "changes": self.changes(project, meta), "verify": self.verify_status(project, meta), "jobs": []}
        if meta.get("git"):
            manifest["commits"] = {"base": meta["git"]["base"], "tip": self._branch_tip(project, meta)}
        parent = self._dir(project) / meta["id"]
        parent.mkdir(exist_ok=True)
        changed = "the proposal changed while saving evidence; review it and try again"
        with tempfile.TemporaryDirectory(prefix=".evidence-", dir=parent) as tmp:
            stage = Path(tmp) / "evidence"
            stage.mkdir()
            folders, files = self._evidence_tree(sources, dirs)
            total = sum(st.st_size for *_, st in files)
            progress.phase("evidence", files=0, files_total=len(files), bytes=0, bytes_total=total)
            for d in folders:
                (stage / d).mkdir(parents=True, exist_ok=True)
            stamps: dict[str, list[int]] = {}
            copied: dict[str, str] = {}                          # {path: hash}, taken while it was copied
            copies = self._copied_runs(sources)
            linked, link = 0, True
            try:
                for rel, src, may_link, st in files:
                    dst = stage / rel
                    if may_link and link and "/".join(rel.split("/", 3)[:3]) not in copies:
                        try:
                            os.link(src, dst)
                        except OSError as e:
                            if e.errno not in _NO_LINK:
                                raise
                            link = e.errno != errno.EXDEV        # another file system: none of it links
                        else:
                            linked += 1
                            stamps[rel] = provenance.stamp(os.stat(dst))     # the inode archived
                            progress.add(files=1)
                            continue
                    copied[rel] = _copy(src, dst, progress)
                    stamps[rel] = provenance.stamp(st)
                    progress.add(files=1)
            except FileNotFoundError as e:                       # a run removed meanwhile
                raise ChangesetError(changed) from e
            known = self._known_hashes(stage, {rel: s for rel, s in stamps.items() if rel not in copied})
            progress.add(bytes=sum(stamps[rel][0] for rel in known))
            todo = sorted(set(stamps) - set(known) - set(copied))
            with ThreadPoolExecutor(HASHERS, thread_name_prefix="evidence") as pool:
                hashed = dict(zip(todo, pool.map(lambda rel: _sha(stage / rel, progress), todo)))
            # Include status.json itself: it contains the recorded input hashes and the verdict.
            found = {**hashed, **known, **copied}
            manifest["files"] = {rel: found[rel] for rel in sorted(stamps)}
            for side, name, _ in sources:
                for path in sorted((stage / side / "runs").glob("*/status.json")):
                    manifest["jobs"].append({"side": side, "project": name, "id": path.parent.name,
                                             "path": str(path.parent.relative_to(stage))})
            self._ensure_idle(sources)
            if self.changes(project, meta) != manifest["changes"] or self._stamps(sources, dirs) != stamps \
                    or (meta.get("git") and self._branch_tip(project, meta) != manifest["commits"]["tip"]):
                raise ChangesetError(changed)
            manifest.update(sources=stamps, linked=linked, bytes=total)
            (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
            # An unsuccessful apply may have left an earlier complete archive; the live sources still exist.
            evidence = parent / "evidence"
            if evidence.exists():
                shutil.rmtree(evidence)
            stage.replace(evidence)
        meta["evidence"] = {"path": str(evidence.relative_to(self.srv.project(project).path)),
                            "captured": captured, "jobs": len(manifest["jobs"]), "bytes": total}
        return manifest

    def _cleanup(self, project: str, meta: dict, manifest: dict, discard: bool = False) -> None:
        """Never delete edits or newly started runs which arrived after the evidence snapshot. On git, an edit that
        arrived is committed onto the branch (nothing is lost) and the worktrees are kept all the same. The files are
        compared by their stamps as the evidence saved them (_archive), those of a manifest from before stamps by
        their bytes."""
        sources = [(side, meta[key], self.srv.project(meta[key]).path)
                   for side, key in (("base", "baseline"), ("work", "preview"))]
        try:
            self._ensure_idle(sources)
            dirs = self._evidence_dirs(meta)
            unchanged = self.changes(project, meta) == manifest["changes"] and \
                (self._stamps(sources, dirs) == manifest["sources"] if "sources" in manifest
                 else self._source_files(sources, dirs) == manifest["files"])
            if meta.get("git"):                 # committed whether or not something else changed: nothing is lost
                unchanged = self._commit_tip(project, meta) == manifest["commits"]["tip"] and unchanged
        except (ChangesetError, OSError, RuntimeError, ValueError):
            unchanged = False
        if not unchanged:
            meta["cleanup_warning"] = "The proposal changed during closure. Its working copy was retained for recovery."
            meta["retained"] = True                 # kept on purpose: the sweep leaves it alone
            self._save(project, meta)
            return
        self._forget(project, meta["id"])
        if meta.get("git"):
            self._close_git(project, meta)
            return
        root = self._shadow_root(project, meta["id"])
        shutil.rmtree(root if discard else root / "work", ignore_errors=True)

    def _forget(self, project: str, cid: str) -> None:
        """Drops the cached editors / layout databases of the preview projects."""
        pre = (f"{project}~{cid}",)
        with self.srv.lock:
            for k in [k for k in self.srv.dbs if k[0].startswith(pre)]:
                del self.srv.dbs[k]
            for k in [k for k in self.srv.logs if f"/{cid}/" in str(k)]:
                del self.srv.logs[k]
            for k in [k for k in self.srv.editors if f"/{cid}/" in str(k)]:
                del self.srv.editors[k]
            getattr(self.srv, "_previews", {}).pop(pre[0], None)
        _forget_parsed(self._shadow_root(project, cid))

    # ------------------------------------------------------------------ what a server killed mid-way left

    def _merging_file(self, project: str, cid: str) -> Path:
        """An interrupted merge's state (module docstring): beside the changeset's evidence, never in a commit."""
        return self._dir(project) / cid / "merging.json"

    @staticmethod
    def _write_merging(f: Path, state: dict) -> None:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f".merging.{uuid.uuid4().hex[:8]}.json")
        tmp.write_text(json.dumps(state, indent=1) + "\n")
        os.replace(tmp, f)

    def _recover(self, project: str, cid: str) -> None:
        """Finishes or takes back the merge of changeset `cid` that a server killed mid-way left (module docstring,
        an interrupted merge); nothing while an operation of this process works on the changeset (it is that
        operation's merge), or another holds the changesets' lock (a later access does it)."""
        project = project.split("~", 1)[0]
        root = self._shadow_root(project, cid)
        if _is_busy(root) or not self.srv.changeset_lock.acquire(blocking=False):
            return
        try:
            f = self._merging_file(project, cid)
            if f.is_file() and not _is_busy(root):
                with _busy(root):
                    self._recover_merge(project, cid, f)
        except Exception as e:
            print(f"undine: cannot finish the interrupted merge of {project}/{cid}: {e}", file=sys.stderr)
        finally:
            self.srv.changeset_lock.release()

    def _recover_merge(self, project: str, cid: str, f: Path) -> None:
        """The merge is all written (merging.json says write_plan finished, or every file is the merged one: a log
        when it starts with it, records appended since): the closure is finished as the merge would have finished
        it (the record, the commit it made if any, then the worktrees and the branch). Else it is taken back
        (_put_back_merge) and the changeset stays as it was, its record saying so (_note). The log is a file of the
        merge like the others: it is never merged twice."""
        state = json.loads(f.read_text())
        rec = self._dir(project) / f"{cid}.json"
        meta = json.loads(rec.read_text()) if rec.is_file() else None
        revert = state.get("action") == "revert"
        if meta is None or meta.get("status") != ("applied" if revert else "open"):
            f.unlink(missing_ok=True)                # the record was saved after the merge: only this was left
            return
        d = self.srv.project(project).path
        files = state.get("files") or {}
        with self._project_locked(project, list(files)) as dbs, branches.lock(d):
            done = bool(state.get("written")) or state.get("paths") is not None \
                or all(self._is_merged(d / rel, x) for rel, x in files.items())
            if done:
                paths = state.get("paths")
                if paths is None:                    # killed before the netlists were read again
                    paths = sorted(set(files) | self._sync_netlists(project, sorted(files)))
                commit = state.get("commit") or self._merge_commit_of(d, state, cid)
            else:
                left = self._put_back_merge(project, d, files)
            for rel in files:
                for p in _leftovers(d / rel):
                    p.unlink(missing_ok=True)
            for db in dbs.values():
                self._reread_layout(db)
        self._reread(project)
        if not done:
            self._note(meta, (f"Undine stopped while {'reverting' if revert else 'applying'} this changeset "
                              f"({state.get('t')}); the {state.get('action')} was taken back")
                       + (f", but for {', '.join(left)}, which could not be put back (changed in the project "
                          "since): left as they are, they may hold part of it" if left
                          else ": nothing of it is in the project")
                       + f". {'Revert' if revert else 'Apply'} it again (that merges what is missing).")
            self._save(project, meta)
            f.unlink(missing_ok=True)
            if left:
                print(f"undine: an interrupted merge into {project} was taken back but for {', '.join(left)}, which "
                      "changed since", file=sys.stderr)
            return
        out = {"t": state.get("t"), "by": state.get("by"), "commit": commit,
               **self._left(commit, paths, state.get("head"), state.get("clean")), "paths": sorted(paths)}
        meta.update({k: v for k, v in (state.get("record") or {}).items() if v is not None})
        self._note(meta, None)                       # an earlier try's, taken back: this one is done
        if revert:
            meta.update(status="reverted", reverted={**out, "kept": state.get("kept") or []})
        else:
            meta.update(status="applied", applied={**out, "tip": state.get("theirs")})
        self._save(project, meta)
        f.unlink(missing_ok=True)
        if not revert:
            self._finish_closure(project, meta)

    @staticmethod
    def _note(meta: dict, note: str | None) -> None:
        """The record's note of an interrupted merge taken back ("interrupted"), shown with the closure's warning
        (cleanup_warning, which the review shows); None: the note dropped (the merge done since)."""
        old = meta.pop("interrupted", None)
        warning = meta.get("cleanup_warning") or ""
        if old and old in warning:
            warning = warning.replace(old, "").strip()
        if note:
            meta["interrupted"] = note
            warning = f"{warning} {note}".strip()
        if warning:
            meta["cleanup_warning"] = warning
        else:
            meta.pop("cleanup_warning", None)

    @staticmethod
    def _is_merged(target: Path, x: dict) -> bool:
        """Whether the file is what the merge wrote (merging.json's entry `x`: "new", its digest; a log's "size":
        the merged log's, which it starts with when records were appended since); a deletion: no file."""
        try:
            data = target.read_bytes() if target.is_file() else None
        except OSError:
            return False
        if data is None or x.get("new") is None:
            return data is None and x.get("new") is None and not target.exists()
        size = x.get("size")
        return _digest(data if size is None else data[:size]) == x["new"]

    def _put_back_merge(self, project: str, d: Path, files: dict) -> list[str]:
        """An interrupted write_plan taken back: each file it renamed in is replaced by the one it moved aside
        (".old"), a log keeping the records appended since after it, a file it added removed; one it moved aside and
        was killed before renaming the new one in is put back. A file whose new one is still beside it (".new") was
        never renamed in: the project's, edited since or not. Returns the files renamed in and changed since (they
        may hold part of the merge: left as they are) and those it could not put back; but a netlist whose schematic
        is not one of them, which is read from its schematic again (the edit since did that already)."""
        left = []
        for rel, x in sorted(files.items()):
            target = d / rel
            ours, new, size = x.get("ours"), x.get("new"), x.get("size")
            if _file_digest(target) == ours:
                continue
            old = next((p for p in _leftovers(target, "old") if _file_digest(p) == ours), None) \
                if ours is not None else None
            pending = new is not None and any(_file_digest(p) == new for p in _leftovers(target, "new"))
            if self._is_merged(target, x):
                data = target.read_bytes() if target.is_file() else b""
                tail = data[size:] if size is not None else b""
                before = data[:x["ours_size"]] if x.get("ours_size") is not None else None    # a merged log's start
                if ours is None:
                    if tail:
                        _put(target, tail)
                    else:
                        target.unlink(missing_ok=True)
                elif old is not None and not tail:
                    os.replace(old, target)
                elif old is not None or _digest(before) == ours:
                    _put(target, (old.read_bytes() if old is not None else before) + tail)
                    if old is not None:
                        old.unlink()
                else:
                    left.append(rel)
            elif not target.exists() and old is not None and pending:      # killed between its two renames
                os.replace(old, target)
            elif not pending:
                left.append(rel)
        schematics = []
        for rel in [r for r in left if r.endswith(netlist.SUFFIX)]:
            sch = (d / rel).with_name((d / rel).name[:-len(netlist.SUFFIX)] + ".sch")
            if sch.is_file() and sch.relative_to(d).as_posix() not in left:
                schematics.append(sch.relative_to(d).as_posix())
                left.remove(rel)                     # read from its schematic as it is now
        self._sync_netlists(project, schematics)
        return left

    @staticmethod
    def _merge_commit_of(d: Path, state: dict, cid: str) -> str | None:
        """The commit an interrupted merge made on the user's branch (it was killed before saying so): on HEAD's
        first-parent line since the HEAD it started from, the one whose parents are that HEAD (and, an apply's, the
        tip), committed by Undine with the merge's subject ("subject"; "Apply <id>: " / "Revert <id>: " in an
        older merging.json): a commit of the person's made since is not taken for it. None: it made none (the
        merge stays uncommitted)."""
        head, theirs = state.get("head"), state.get("theirs")
        if not head:
            return None
        revert = state.get("action") == "revert"
        subject = state.get("subject")
        out = branches._out(d, "rev-list", "--first-parent", "--parents", "-n", "200", "HEAD", f"^{head}") or ""
        for line in out.splitlines():
            c, *parents = line.split()
            if parents[:1] != [head] or (parents[1:] != [] if revert else parents[1:2] != [theirs]):
                continue
            r = branches._run(d, "cat-file", "commit", c)
            header, _, message = r.stdout.decode("utf-8", "surrogateescape").partition("\n\n")
            first = message.split("\n", 1)[0]
            committer = next((h[len("committer "):] for h in header.splitlines() if h.startswith("committer ")), "")
            if r.returncode == 0 and committer.startswith(f"{branches.COMMITTER} <{branches.EMAIL}> ") and \
                    (first == subject if subject is not None else first.startswith(
                        f"{'Revert' if revert else 'Apply'} {cid}: ")):
                return c
        return None

    def finish_interrupted(self) -> list[str]:
        """At the server's start (`undine serve`, before the netlist watcher and the API): the projects with a merge
        a server killed mid-way left are swept now (_sweep: the merge finished or taken back), before anything else
        writes their files (the watcher would read a schematic the merge has not renamed in yet back into the netlist
        it has, and record that in the log it has). Returns ["<project>/<id>"] of the merges finished or taken back."""
        found = [f for f in sorted(self.srv.store.root.glob("*/changesets/cs-*/merging.json"))
                 if CID.fullmatch(f.parent.name)]
        for name in dict.fromkeys(f.parent.parent.parent.name for f in found):
            self._sweep(name)
        return [f"{f.parent.parent.parent.name}/{f.parent.name}" for f in found if not f.exists()]

    def _sweep(self, project: str) -> None:
        """Once per project and server (its first access): what a server killed mid-way left is tidied (module
        docstring, the sweep). Not while another operation holds the changesets' lock: a later access does it."""
        project = project.split("~", 1)[0]
        with self.srv.lock:
            swept = self.srv.__dict__.setdefault("_cs_swept", set())
            if project in swept:
                return
            swept.add(project)
        if not self.srv.changeset_lock.acquire(blocking=False):
            with self.srv.lock:
                swept.discard(project)
            return
        try:
            self._sweep_now(project)
        except KeyError:
            pass                                     # no such project
        except Exception as e:
            print(f"undine: cannot tidy the changesets of {project}: {e}", file=sys.stderr)
        finally:
            self.srv.changeset_lock.release()

    def _sweep_now(self, project: str) -> None:
        d = self.srv.project(project).path
        records = d / "changesets"
        for f in sorted(records.glob("cs-*/merging.json")) if records.is_dir() else []:
            if CID.fullmatch(f.parent.name):
                self._recover(project, f.parent.name)
        # Evidence a server killed while saving it left staged: the closure saves it anew, and its links would hold the
        # runs' bytes once the working copy is gone, in no size and beyond a prune. Not one being saved now (_busy).
        for staged in sorted(records.glob("cs-*/.evidence-*")) if records.is_dir() else []:
            if CID.fullmatch(staged.parent.name) and not _is_busy(self._shadow_root(project, staged.parent.name)):
                shutil.rmtree(staged, ignore_errors=True)
        try:
            r = branches.repo(d)
        except (branches.NotSetUp, RuntimeError, OSError):
            return                                   # no repository: no branch (an older copy is no worktree)
        if not r.set_up:
            return                                   # not Undine's to write
        shadow = self.srv.store.root / ".changesets" / project
        cids = {p.name for p in shadow.iterdir() if p.is_dir() and CID.fullmatch(p.name)} if shadow.is_dir() else set()
        heads = branches._out(d, "for-each-ref", "--format=%(refname)", f"refs/heads/undine/{project}/") or ""
        cids |= {ref.rsplit("/", 1)[-1] for ref in heads.splitlines() if CID.fullmatch(ref.rsplit("/", 1)[-1])}
        for cid in sorted(cids):
            root = self._shadow_root(project, cid)
            if _is_busy(root):
                continue                             # being opened or closed here
            rec = records / f"{cid}.json"
            try:
                if not rec.is_file():
                    with _busy(root):
                        self._drop_orphan(project, cid, r.prefix)
                    continue
                meta = json.loads(rec.read_text())
                if meta.get("status") == "open" or not meta.get("git") or meta.get("retained") \
                        or (meta.get("cleanup_warning") and (root / "work").is_dir()):
                    continue                         # open; an older copy; a working copy its closure kept
                with _busy(root):
                    self._finish_closure(project, meta)
            except Exception as e:                   # the others are tidied all the same
                print(f"undine: cannot tidy the changeset {project}/{cid}: {e}", file=sys.stderr)
        branches.prune(d)

    def _finish_closure(self, project: str, meta: dict) -> None:
        """The closure of a changeset whose record says it is closed while its branch or worktrees are still there
        (a server killed after saving it): as _cleanup does it (a late edit committed onto the branch, the working
        copy then retained), else its worktrees removed and its branch kept (_close_git)."""
        g, cid = meta["git"], meta["id"]
        d = self.srv.project(project).path
        root = self._shadow_root(project, cid)
        if not branches.ref_exists(d, g["branch"]):
            kept = kept_ref(project, cid)
            if not g.get("ref") and branches.ref_exists(d, kept):     # moved, the record not saved since
                sha = branches.resolve(d, kept)
                g.update(tip=None if sha == g["base"] else sha, ref=kept,
                         commits=self._tip_state(project, cid).get("commits") or g.get("commits") or 0)
                meta.pop("cleanup_warning", None)
                self._save(project, meta)
            if g.get("ref") and root.exists():
                for side in ("work", "base"):
                    branches.remove_worktree(d, root / side)
                self._forget(project, cid)
                shutil.rmtree(root, ignore_errors=True)
            return
        manifest = self._dir(project) / cid / "evidence" / "manifest.json"
        if self._resolves(meta["preview"]):
            if manifest.is_file() and not meta.get("cleanup_warning"):
                self._cleanup(project, meta, json.loads(manifest.read_text()), discard=meta["status"] == "discarded")
                return
            try:
                self._commit_tip(project, meta)      # nothing is lost
            except Exception as e:
                print(f"undine: cannot commit the working copy of {meta['preview']}: {e}", file=sys.stderr)
                return
        meta.pop("cleanup_warning", None)            # what an earlier try said: this one says it again, or closes
        self._forget(project, cid)
        self._close_git(project, meta)

    def _drop_orphan(self, project: str, cid: str, prefix: str) -> None:
        """The branch and worktrees of a changeset with no record (a server killed while opening it): the working
        copy committed onto its branch (nothing is lost), the worktrees removed, the branch kept as the kept ref (never
        deleted). A directory that is no worktree of it (an older copy) is left alone."""
        d = self.srv.project(project).path
        root = self._shadow_root(project, cid)
        branch = branch_of(project, cid)
        worktrees = [root / side for side in ("work", "base") if (root / side / ".git").is_file()]
        if not worktrees and not branches.ref_exists(d, branch):
            return
        if (root / "work" / ".git").is_file():
            try:
                branches.commit_worktree(root / "work", prefix, f"Changeset {cid} of {project}, recovered\n\nIts "
                                         "record was never written (Undine stopped while opening it).\n", "system")
            except (RuntimeError, ValueError, OSError) as e:
                print(f"undine: cannot commit the working copy of {project}~{cid}: {e}", file=sys.stderr)
                return                               # left as it is rather than lost
        for w in worktrees:
            branches.remove_worktree(d, w)
        if branches.ref_exists(d, branch):
            try:
                branches.keep_ref(d, branch, kept_ref(project, cid))
            except (RuntimeError, ValueError) as e:
                print(f"undine: cannot keep {branch} as {kept_ref(project, cid)}: {e}", file=sys.stderr)
        self._forget(project, cid)
        if not any((root / side).exists() for side in ("work", "base")):
            shutil.rmtree(root, ignore_errors=True)

    # ------------------------------------------------------------------ timeline and checkpoint restore

    def _floor_dir(self, meta: dict) -> Path:
        """Where the floor of the changeset's numbering is (live.floor): beside its worktree; an older copy's history/."""
        if meta.get("git"):
            return self._shadow_root(meta["project"], meta["id"])
        return self.srv.project(meta["preview"]).path / "history"

    def _all_records(self, meta: dict) -> list[tuple[str, dict]]:
        """(lib, record) of the working copy in changeset order (gseq), across its libraries: the changeset's own,
        after the base (oplog.py's floor)."""
        out = [(lib, r) for lib in self._libs(meta) for r in self._own(meta["preview"], lib) if r.get("gseq")]
        return sorted(out, key=lambda x: x[1]["gseq"])

    def _own(self, name: str, lib: str) -> list[dict]:
        """The records of project `name`'s log of `lib` after its floor (OpLog.own), parsed once (_records_after)."""
        log = self.srv.oplog(name, lib)
        return _records_after(log.path, log.floor)

    def _activity(self, meta: dict) -> dict:
        """The latest committed change to each view, including operations undone or restored away.

        Unlike the review's effective changes, this keeps advancing on undo and restore so a client can refresh
        drawings and cell lists. A layout record with no cell affects the library's layout as a whole. A restore's
        library marker also invalidates previously touched views, including cells that the restore removed.
        """
        head = 0
        views: dict[tuple[str, str | None, str], dict] = {}
        for lib, r in self._all_records(meta):
            head = r["gseq"]
            if r.get("view") in ("schematic", "symbol", "layout", "intent", "rtl", "flow"):
                key = (lib, r.get("cell"), r["view"])
                views[key] = {"lib": lib, "cell": r.get("cell"), "view": r["view"], "gseq": head}
            elif r["op"] == "restore":
                for key, v in views.items():
                    if key[0] == lib:
                        v["gseq"] = head
        return {"head": head, "views": sorted(views.values(), key=lambda v: v["gseq"])}

    def timeline(self, project: str, cid: str) -> dict:
        """The working copy's operations in order (one entry per operation: a tool call, a request), whether each is still
        in effect, the assistant turns they belong to, and the points it can be restored to."""
        meta = self._load(project, cid)
        if meta["status"] != "open":
            return {"ops": [], "turns": {}, "checkpoints": []}
        libs = self._libs(meta)
        net = {(lib, r["seq"]) for lib in libs for r in self._records(meta["preview"], lib)}
        logs = {lib: {x["seq"]: x for x in self._own(meta["preview"], lib)} for lib in libs}
        ops: list[dict] = []
        for lib, r in self._all_records(meta):
            if r["op"] == "save" and not r.get("effects"):
                continue
            kind = r["op"] if r["op"] in ("undo", "redo", "restore") else "edit"
            o = ops[-1] if ops and ops[-1]["opid"] == r.get("opid") else None
            if o is None:
                o = {"opid": r.get("opid"), "first": r["gseq"], "last": r["gseq"], "turn": r.get("turn"), "actor": r.get("actor"),
                     "t": r.get("t"), "kind": kind, "ops": [], "cells": [], "reasons": [], "effects": [], "effect_data": [], "in_effect": False}
                ops.append(o)
            o["last"] = r["gseq"]
            if r["op"] not in o["ops"]:
                o["ops"].append(r["op"])
            where = f"{lib}/{r['cell']} ({r['view']})" if r.get("cell") else None
            if where and where not in o["cells"]:
                o["cells"].append(where)
            if r.get("reason") and r["reason"] not in o["reasons"]:
                o["reasons"].append(r["reason"])
            if kind == "edit":
                o["in_effect"] = o["in_effect"] or (lib, r["seq"]) in net
                o["effect_data"] += [{"lib": lib, "cell": r.get("cell"), "view": r.get("view"), "effect": e}
                                     for e in (r.get("effects") or [])][:max(0, 8 - len(o["effect_data"]))]
                o["effects"] += [llm_effect(e) for e in (r.get("effects") or [])][:max(0, 8 - len(o["effects"]))]
            elif kind == "restore":
                o["to"] = r["to"]
            elif (orig := logs[lib].get(r.get("of"))):
                o["of"] = {"gseq": orig.get("gseq"), "op": orig["op"], "cell": orig.get("cell")}
        turns = {t: self._turn_info(project, t) for t in {o["turn"] for o in ops if o["turn"]}}
        return {"ops": ops, "turns": turns, "checkpoints": [0] + [o["last"] for o in ops[:-1]]}

    def _turn_info(self, project: str, tid: str) -> dict:
        from undine.agents import llm
        prj_path = self.srv.project(project.split("~", 1)[0]).path      # turns are kept with the real project
        try:
            rec = self.srv.turns.get(prj_path, tid)
            conv = llm.Conversations(prj_path).get(rec["conversation"])
            msg = next((m["text"] for m in conv["messages"] if m["role"] == "user" and m.get("turn") == tid), "")
            return {"state": rec["state"], "message": msg}
        except Exception:
            return {"state": None, "message": ""}

    def _snapshot(self, meta: dict, prefix: str) -> tuple[Path, Path, list[str]]:
        """(the working copy's directory, a snapshot of what may change in it, the names snapshotted). The snapshot
        is beside the worktree, never in it (a project in a subdirectory of its repository has its parent in it)."""
        prj = self.srv.project(meta["preview"])
        wroot, names = prj.path, _state(prj)
        snap = Path(tempfile.mkdtemp(prefix=prefix, dir=self._shadow_root(meta["project"], meta["id"])))
        for name in names:
            src = wroot / name
            if src.is_dir():
                shutil.copytree(src, snap / name, symlinks=True)
            elif src.is_file():
                shutil.copy2(src, snap / name)
        return wroot, snap, names

    @staticmethod
    def _put_files_back(wroot: Path, snap: Path, names: list[str]) -> None:
        for name in names:
            dst = wroot / name
            if dst.is_dir() and not dst.is_symlink():
                shutil.rmtree(dst, ignore_errors=True)
            elif dst.exists() or dst.is_symlink():
                dst.unlink()
            src = snap / name
            if src.is_dir():
                shutil.copytree(src, dst, symlinks=True)
            elif src.is_file():
                shutil.copy2(src, dst)

    @_serialized
    def restore(self, project: str, cid: str, to: int, actor: str = "user") -> dict:
        """Brings the working copy back to its state right after gseq `to` (0: as opened), recorded as `restore`.

        The state is rebuilt from the log, not by undoing what is in effect: each record after `to` is inverted, newest
        first (an undo is re-applied, a redo or an earlier restore taken back), so an addition that was undone after the
        checkpoint comes back. Cells created after `to` are removed when they are back to how they were created and
        nothing that remains refers to them, and so are libraries created after it, with their logs, once nothing of
        theirs remains. All or nothing: the working copy's files and logs are snapshotted first and put back if any
        step fails."""
        meta = self._load(project, cid)
        self._check_open(meta)
        self._check_writer(cid)
        work = meta["preview"]
        recs = self._all_records(meta)
        ends = [0] + [r["gseq"] for i, (_, r) in enumerate(recs) if i + 1 == len(recs) or recs[i + 1][1].get("opid") != r.get("opid")]
        if not recs or to not in ends or to >= recs[-1][1]["gseq"]:
            raise ChangesetError(f"{to} is not a point this changeset can be restored to (the end of an earlier operation)")
        logs = {lib: {x["seq"]: x for x in self._own(work, lib)} for lib in self._libs(meta)}
        groups: dict[tuple, list[dict]] = {}      # (lib, view, cell) -> effects to apply, in order
        made_views: list[tuple[str, str, str, str]] = []
        made_cells: list[tuple[str, str, str | None]] = []
        made_libs: list[str] = []
        # a view or layout cell deleted with its content (delete_cell) after `to` and made again after that: the
        # remake is taken out where its creation is inverted (its later edits inverted before it), so the deletion's
        # inverse finds its place free, and is not one to drop at the end (_drop_made)
        deleted: dict[tuple, int] = {}                 # (lib, view, cell) / (lib, "layout", cell) -> its first deletion
        for lib, r in recs:
            if r["gseq"] > to and r["op"] not in ("undo", "redo", "restore"):
                for e in r.get("effects") or []:
                    if e["k"] == "v" and e["do"] == "del" and e.get("kept"):
                        deleted.setdefault((lib, r["view"], r["cell"]), r["gseq"])
                    elif e["k"] == "c" and e["do"] == "del" and e.get("kept"):
                        deleted.setdefault((lib, "layout", e["cell"]), r["gseq"])
        for lib, r in reversed([x for x in recs if x[1]["gseq"] > to]):
            remade: list[dict] = []
            if r["op"] == "undo":
                orig = logs[lib].get(r.get("of")) or {}
                eff = orig.get("effects") or []
            elif r["op"] == "redo":
                orig = logs[lib].get(r.get("of")) or {}
                eff = oplog.invert(orig.get("effects") or [])
            else:
                orig, eff = r, oplog.invert(r.get("effects") or [])
                for e in (r.get("effects") or []) if r["op"] != "restore" else []:
                    if e["k"] == "v" and e["do"] == "add" and not e.get("kept"):
                        if deleted.get((lib, r["view"], r["cell"]), r["gseq"]) < r["gseq"]:
                            remade.append({"k": "v", "do": "del", "kept": True, "text": e["text"]})
                        else:
                            made_views.append((lib, r["view"], r["cell"], e["text"]))
                    elif e["k"] == "c" and e["do"] == "add" and not e.get("kept"):
                        if deleted.get((lib, "layout", e["cell"]), r["gseq"]) < r["gseq"]:
                            c = self.srv.db(work, lib).layout.cell(e["cell"])
                            info = [[x.name, layout._jsonable(x.value)] for x in c.each_meta_info()
                                    if x.is_persisted()] if c is not None else []
                            remade.append({"k": "c", "do": "del", "cell": e["cell"], "drop": True, "kept": True,
                                           "src": e.get("src") or {}, "meta": info,
                                           **({"sha": e["sha"]} if e.get("sha") else {})})
                        else:
                            made_cells.append((lib, e["cell"], e.get("sha")))
                    elif e["k"] == "l" and e["do"] == "add":
                        made_libs.append(e["lib"])
            eff = eff + remade[::-1]
            if not eff:
                continue
            view = orig.get("view") or r.get("view")
            key = (lib, "layout", None) if view == "layout" else (lib, view, orig.get("cell") or r.get("cell"))
            groups.setdefault(key, []).extend(eff)
        m = {"op": "restore", "args": {"to": to}, "to": to, "actor": actor, "reason": f"restore to checkpoint {to}", "changeset": cid}
        wroot, snap, names = self._snapshot(meta, ".restore-")
        try:
            with live.operation():
                for (lib, view, cell), eff in groups.items():
                    if view == "layout":
                        self.srv.db(work, lib).replay(eff, dict(m))
                    else:
                        self.srv.editor(work, lib, cell, view).replay(eff, dict(m))
                wprj = self.srv.project(work)                  # a symbol's pins back as they were: its users' netlists too
                syms = [wprj.library(lib).path / f"{cell}.sym" for lib, view, cell in groups if view == "symbol"]
                self._sync_netlists(work, [s.relative_to(wprj.path).as_posix() for s in syms if s.is_relative_to(wprj.path)])
                dropped = self._drop_made(work, made_views, made_cells, m, made_libs)
                if dropped:                                    # their records went with them: numbering goes on past
                    live.raise_floor(self._floor_dir(meta), recs[-1][1]["gseq"])
                for lib in sorted({lib for lib, r in recs if r["gseq"] > to} - dropped):    # every library the restore covers says so
                    self.srv.oplog(work, lib).append({**m, "cell": None, "view": None, "effects": []})
        except Exception as e:
            self._forget(project, cid)                       # cached editors / layouts hold the failed state
            self._put_files_back(wroot, snap, names)
            if isinstance(e, live.WriteLocked):
                raise
            raise ChangesetError(f"cannot restore to checkpoint {to}: {e}; nothing was changed") from e
        finally:
            shutil.rmtree(snap, ignore_errors=True)
        return self.get(project, cid)

    def _drop_made(self, work: str, views: list, cells: list, m: dict, libs: list = ()) -> set[str]:
        """Removes what the operations after the checkpoint created, if unchanged and unused; returns the libraries it
        removed (_drop_libraries)."""
        from undine.schematics import xschem
        prj = self.srv.project(work)
        def file_of(lib, view, cell):
            if view == "rtl":
                from undine.design import rtl
                return rtl.find_file(prj.library(lib).path, cell) or prj.library(lib).path / f"{cell}.sv"
            return prj.library(lib).path / f"{cell}.{'sch' if view == 'schematic' else 'sym'}"
        gone = {(lib, file_of(lib, view, cell).name) for lib, view, cell, _ in views}
        for lib, view, cell, text in views:
            path = file_of(lib, view, cell)
            if not path.is_file():
                continue
            if path.read_text(errors="replace") != text:
                raise ChangesetError(f"{lib}/{path.name} was changed outside the recorded operations")
            users = []
            for l2 in _lib_dirs(prj):
                for sch in sorted(prj.library(l2).path.glob("*.sch")):
                    if (l2, sch.name) in gone:
                        continue
                    if view == "symbol" and any(c.sym.split("/")[-1] == path.name for c in xschem.parse(sch.read_text(errors="replace")).comps):
                        users.append(f"{l2}/{sch.stem}")
            if view == "schematic" and path.with_suffix(".sym").is_file() and (lib, f"{cell}.sym") not in gone:
                users.append(f"its symbol {lib}/{cell}.sym")
            if users:
                raise ChangesetError(f"{lib}/{path.name} is used by {', '.join(users)}")
            with self.srv.lock:
                self.srv.editors.pop(path, None)
            path.unlink()
        for lib, cell, sha in cells:                      # parents first (layout._brought)
            db = self.srv.db(work, lib)
            c = db.layout.cell(cell)
            if c is None:
                continue
            if c.parent_cells():
                raise ChangesetError(f"layout cell {lib}/{cell} is placed in another cell")
            if (not c.is_empty() or c.child_cells()) and (sha is None or sha != layout._content(db.layout, c)):
                raise ChangesetError(f"layout cell {lib}/{cell} is not empty" if sha is None else
                                     f"layout cell {lib}/{cell} was changed after it was brought in")
            db.replay([{"k": "c", "do": "del", "cell": cell, "drop": True}], dict(m))
        return self._drop_libraries(work, libs)

    LIB_LEFTOVERS = (layoutstore.SUFFIX, netlist.SUFFIX, ".intent.yaml")   # what a cell taken out leaves behind

    def _drop_libraries(self, work: str, libs: list) -> set[str]:
        """Takes the libraries the operations after the checkpoint created (create_library) out of the working copy once
        their cells are gone: the project.json entry, the folder and the log, so that applying does not bring an empty
        library into the project, and the name is free again. What is left in the folder can only be what a cell taken
        out leaves (its cell file, until a save; its netlist and intent); a cell or another file still there was not
        made by the recorded operations: refused."""
        dropped: set[str] = set()
        for lib in libs:                                   # newest first
            prj = self.srv.project(work)
            folder = _lib_dirs(prj).get(lib)
            if folder is None or not folder.resolve().is_relative_to(prj.path.resolve()):
                continue
            db = self.srv.db(work, lib)
            have = sorted(c.name for c in db.layout.each_cell() if not c.is_ghost_cell())
            have += sorted(p.name for p in folder.rglob("*") if not p.is_dir() and not p.name.endswith(self.LIB_LEFTOVERS))
            if have:
                raise ChangesetError(f"library {lib} holds {', '.join(have[:5])}{' ...' if len(have) > 5 else ''}, which the "
                                     "operations after the checkpoint did not make: it cannot be taken out")
            log = self.srv.oplog(work, lib).path
            with self.srv.lock:
                self.srv.dbs.pop((work, lib), None)
                self.srv.logs.pop(log, None)
                for k in [k for k in self.srv.editors if Path(k).is_relative_to(folder)]:
                    del self.srv.editors[k]
            if folder.is_dir():
                shutil.rmtree(folder)
            log.unlink(missing_ok=True)
            prj.meta.get("libraries", {}).pop(lib, None)
            prj.save()
            dropped.add(lib)
        return dropped

    # ------------------------------------------------------------------ several edits as one step (batch)

    def _head(self, meta: dict) -> int:
        recs = self._all_records(meta)
        return recs[-1][1]["gseq"] if recs else 0

    def _since(self, meta: dict, gseq: int) -> list[dict]:
        """What the records after `gseq` did, cell by cell, with their effects as recorded (for a batch's report, which
        counts them)."""
        out = []
        for lib, r in self._all_records(meta):
            if r["gseq"] > gseq and (r.get("effects") or r["op"] not in ("save",)):
                out.append({"cell": f"{lib}/{r['cell']}" if r.get("cell") else lib, "view": r.get("view"), "op": r["op"],
                            "reason": r.get("reason"), "effects": r.get("effects") or []})
        return out

    def batch(self, project: str, cid: str, run, dry_run: bool = False) -> dict:
        """Runs `run()` (several edits) in the working copy as one all-or-nothing step. The working copy's files, logs and
        the changeset's record are snapshotted first and put back if any edit fails, and always for a dry run, which
        reports what would change and keeps nothing. Returns {results: run()'s value, changes: what the step recorded}.

        Two things keep a screen that follows the changeset (the activity endpoint) right: while the step runs it does
        not see its records, and numbering continues past the records that were put back (live.raise_floor), so the
        next real record is never mistaken for one it has already seen."""
        meta = self._load(project, cid)
        self._check_open(meta)
        before = self._head(meta)
        meta_file = self._dir(project) / f"{cid}.json"
        wroot, snap, names = self._snapshot(meta, ".batch-")
        shutil.copy2(meta_file, snap / "changeset.json")
        self.srv.batching[cid] = before
        try:
            try:
                results = run()
            except BaseException:
                self._put_back(project, cid, wroot, snap, names, meta_file)
                raise
            changes = self._since(self._load(project, cid), before)
            if dry_run:
                self._put_back(project, cid, wroot, snap, names, meta_file)
            return {"results": results, "changes": changes}
        finally:
            self.srv.batching.pop(cid, None)
            shutil.rmtree(snap, ignore_errors=True)

    def _put_back(self, project: str, cid: str, wroot: Path, snap: Path, names: list[str], meta_file: Path) -> None:
        meta = self._load(project, cid)
        floor_dir = self._floor_dir(meta)
        used = max(self._head(meta), live.floor(floor_dir))
        self._forget(project, cid)                           # cached editors / layouts hold the state being dropped
        self._put_files_back(wroot, snap, names)
        shutil.copy2(snap / "changeset.json", meta_file)
        live.raise_floor(floor_dir, used)
        self._forget(project, cid)

    # ------------------------------------------------------------------ review

    def _records(self, project_name: str, lib: str) -> list[dict]:
        return oplog._net(self._own(project_name, lib))         # oplog.net_records, of the records parsed once

    @staticmethod
    def link_causes(ops: list[dict]) -> list[dict]:
        """Ties layout operations that follow the schematic (sdl_sync / sdl_generate) to the schematic operations that
        made them necessary: `causes` maps a layout instance name to the seq numbers of the schematic operations on
        that device since the layout last followed it. Derived from the log, so it holds for older changesets too."""
        touched: dict[tuple, list[int]] = {}
        for op in ops:
            if op.get("view") == "schematic":
                for e in op.get("effects") or []:
                    if e.get("k") == "i" and e.get("id"):
                        for n in {e["id"], e.get("id_after") or e["id"]}:
                            touched.setdefault((op["cell"], n), []).append(op["seq"])
            elif str(op.get("op", "")).startswith("sdl_"):
                names = {e["inst"] for e in op.get("effects") or [] if e.get("k") == "i" and e.get("inst")}
                causes = {n: sorted(set(touched.pop((op["cell"], n)))) for n in sorted(names) if (op["cell"], n) in touched}
                if causes:
                    op["causes"] = causes
        return ops

    def changes(self, project: str, meta: dict) -> list[dict]:
        """The recorded operations still in effect in the working copy, per library."""
        work = meta["preview"]
        out = []
        for lib in self._libs(meta):
            recs = self._records(work, lib)
            if recs:
                out.append({"lib": lib, "ops": self.link_causes([{k: r.get(k) for k in
                                                 ("seq", "gseq", "opid", "op", "args", "cell", "view", "actor", "reason", "t", "effects",
                                                  *_SYMBOL_GROUP_FIELDS)
                                                 if k not in _SYMBOL_GROUP_FIELDS or k in r}
                                                for r in recs])})
        return out

    def _resolves(self, name: str) -> bool:
        """Whether the preview `name` is there (its worktree, or an older copy, exists)."""
        try:
            self.srv.project(name)
            return True
        except (KeyError, ValueError):
            return False

    def get(self, project: str, cid: str) -> dict:
        """The record with the changes, the checks, the writer and the activity; "retained": the working copy of a
        closed changeset is still there (a closure kept it: its preview can be opened); "closing": how far its
        closure is while one runs here (live.Closings.public); a closed one's "evidence" with its "size" now
        (_evidence_size)."""
        meta = self._load(project, cid)
        git = {"git": self._git_info(project, meta)} if meta.get("git") else {}
        meta["retained"] = meta["status"] != "open" and self._resolves(meta["preview"])
        if (closing := self.srv.closings.public(cid)):
            meta["closing"] = closing
        if meta["status"] != "open" and meta.get("evidence"):
            archive = self._dir(project) / cid / "evidence" / "manifest.json"
            manifest = json.loads(archive.read_text())
            evidence = {**meta["evidence"], "size": _evidence_size(archive.parent)}
            return {**meta, **git, "evidence": evidence, "changes": manifest["changes"], "verify": manifest["verify"]}
        meta["changes"] = self.changes(project, meta) if meta["status"] == "open" else self._applied_changes(project, meta)
        meta["verify"] = self.verify_status(project, meta)
        le = self.srv.leases.get(cid) if meta["status"] == "open" else None
        meta["writer"] = le.public() if le else None
        if meta["status"] == "open":
            meta["activity"] = self._activity(meta)
        return {**meta, **git}

    def _applied_changes(self, project: str, meta: dict) -> list[dict]:
        """The operations of a changeset closed without evidence (older ones), from the project's log: on git, the net
        of the records an apply brought (its merge record left out); before, the replayed ones."""
        out = []
        for lib in meta.get("libs") or meta["base"]:
            recs = [r for r in self.srv.oplog(project, lib).read(limit=10**9) if r.get("changeset") == meta["id"]]
            if meta.get("git"):
                recs = oplog._net([r for r in recs if r["op"] != "merge"])
            else:
                recs = [r for r in recs if r["op"] not in ("undo", "redo", "revert")]
            if recs:
                out.append({"lib": lib, "ops": self.link_causes([{k: r.get(k) for k in
                                                 ("seq", "op", "cell", "view", "actor", "reason", "t", "effects", *_SYMBOL_GROUP_FIELDS)
                                                 if k not in _SYMBOL_GROUP_FIELDS or k in r}
                                                for r in recs])})
        return out

    # ------------------------------------------------------------------ a closed changeset's evidence: read, sized, pruned

    def _manifest(self, project: str, meta: dict) -> tuple[Path, dict]:
        """The evidence folder of a closed changeset and its manifest (ChangesetError: it has none)."""
        d = self._dir(project) / meta["id"] / "evidence"
        if meta["status"] == "open" or not meta.get("evidence") or not (d / "manifest.json").is_file():
            raise ChangesetError(f"changeset {meta['id']} has no archived evidence"
                                 + (" yet: it is open" if meta["status"] == "open" else ""))
        return d, json.loads((d / "manifest.json").read_text())

    @staticmethod
    def _pruned(manifest: dict) -> dict[str, dict]:
        """{path in the evidence: the prune that removed it ({"t", "by"})} of a manifest's "pruned" notes."""
        return {rel: {"t": p.get("t"), "by": p.get("by")} for p in manifest.get("pruned") or [] for rel in p.get("files") or []}

    def evidence(self, project: str, cid: str, check: bool = False) -> dict:
        """A closed changeset's evidence: {"path", "captured", "commits"?, "size" (now: _evidence_size),
        "bytes" (as captured), "linked", "pruned" (the manifest's notes), "jobs": the manifest's, each with "pruned":
        its files a prune removed}; with `check`, "check": {"ok": how many files still have their hash, "pruned",
        "missing", "changed": [path in the evidence]}: a file a prune removed is said to be pruned, not missing."""
        meta = self._load(project, cid)
        d, manifest = self._manifest(project, meta)
        pruned = self._pruned(manifest)
        jobs = [{**j, "pruned": sorted(rel[len(j["path"]) + 1:] for rel in pruned if rel.startswith(j["path"] + "/"))}
                for j in manifest.get("jobs") or []]
        out = {"path": meta["evidence"].get("path"), "captured": manifest.get("captured"), "size": _evidence_size(d),
               "bytes": manifest.get("bytes"), "linked": manifest.get("linked"), "pruned": manifest.get("pruned") or [],
               "jobs": jobs, **({"commits": manifest["commits"]} if "commits" in manifest else {})}
        if check:
            res = {"ok": 0, "pruned": [], "missing": [], "changed": []}
            present = [(rel, sha) for rel, sha in sorted(manifest.get("files", {}).items())
                       if rel not in pruned and (d / rel).is_file()]
            nobody = live.Closing(cid, "", "", "")
            with ThreadPoolExecutor(HASHERS, thread_name_prefix="evidence") as pool:
                now = dict(zip([rel for rel, _ in present], pool.map(lambda x: _sha(d / x[0], nobody), present)))
            for rel, sha in sorted(manifest.get("files", {}).items()):
                kind = "pruned" if rel in pruned else "missing" if rel not in now else "changed" if now[rel] != sha else "ok"
                if kind == "ok":
                    res["ok"] += 1
                else:
                    res[kind].append(rel)
            out["check"] = res
        return out

    def evidence_sizes(self, project: str) -> dict:
        """The evidence of the project's closed changesets as it is now: {"bytes", "raw": the total, and of it the
        waveforms a prune removes; "changesets": {id: {_evidence_size, "pruned": the record's note | null}}}."""
        out, total, raw = {}, 0, 0
        for m in self.list(project):
            d = self._dir(project) / m["id"] / "evidence"
            if m["status"] == "open" or not (d / "manifest.json").is_file():
                continue
            size = _evidence_size(d)
            try:
                note = (json.loads((self._dir(project) / f"{m['id']}.json").read_text()).get("evidence") or {}).get("pruned")
            except (OSError, ValueError):
                note = None
            out[m["id"]] = {**size, "pruned": note}
            total, raw = total + size["bytes"], raw + size["raw"]
        return {"bytes": total, "raw": raw, "changesets": out}

    @_serialized
    def prune_evidence(self, project: str, cid: str, actor: str = "user") -> dict:
        """Removes the waveforms (RAW_OUTPUTS: out.raw, a VCD ...) of a closed changeset's archived runs: a person's call,
        to free their space (no agent tool does it; the API refuses an agent's actor). Everything else stays: the runs'
        status.json (the inputs, the outputs' hashes, the verdict), logs, netlists, results, measurements, and the
        manifest with every file's hash as captured. The manifest says what was pruned, when and by whom ("pruned":
        [{"t", "by", "files": [path in the evidence], "bytes"}], written before the files go: a prune cut short is
        finished by the next), the record's "evidence" the sum ("pruned": {"t", "by", "files", "bytes"}). Returns
        {"pruned": this prune's {"t", "by", "files" (how many), "bytes", "freed": of them, those no other name holds
        (a working copy a closure kept frees the rest when it goes)}, "size": the evidence's now}. Refused (Busy) while
        the changeset is being closed or reverted."""
        meta = self._load(project, cid)
        d, manifest = self._manifest(project, meta)
        if self.srv.closings.get(cid) is not None or _is_busy(self._shadow_root(project, cid)):
            raise Busy(f"changeset {cid} is being closed or reverted: prune its evidence once that is done")
        noted = self._pruned(manifest)
        found = []
        for top, _, names in os.walk(d):
            rd = Path(top).relative_to(d).as_posix()
            for name in sorted(names):
                rel = f"{rd}/{name}"
                if _is_raw(rel):
                    st = os.lstat(os.path.join(top, name))
                    found.append((rel, st.st_size, st.st_nlink))
        t = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        new = [(rel, size) for rel, size, _ in sorted(found) if rel not in noted]
        if new:
            manifest.setdefault("pruned", []).append({"t": t, "by": actor, "files": [rel for rel, _ in new],
                                                      "bytes": sum(size for _, size in new)})
            tmp = d / f".manifest.{uuid.uuid4().hex[:8]}.json"
            tmp.write_text(json.dumps(manifest, indent=2) + "\n")
            tmp.replace(d / "manifest.json")
            ev, before = meta["evidence"], meta["evidence"].get("pruned") or {}
            ev["pruned"] = {"t": t, "by": actor, "files": (before.get("files") or 0) + len(new),
                            "bytes": (before.get("bytes") or 0) + sum(size for _, size in new)}
            self._save(project, meta)
        for rel, *_ in found:
            (d / rel).unlink(missing_ok=True)
        return {"pruned": {"t": t, "by": actor, "files": len(found), "bytes": sum(size for _, size, _ in found),
                           "freed": sum(size for _, size, links in found if links == 1)}, "size": _evidence_size(d)}

    # ------------------------------------------------------------------ checks

    def _changed_cells(self, meta: dict, changes: list[dict]) -> dict[tuple[str, str], set[str]]:
        cells: dict[tuple[str, str], set[str]] = {}
        for lc in changes:
            for op in lc["ops"]:
                if op["cell"]:
                    cells.setdefault((lc["lib"], op["cell"]), set()).add(op["view"])
        return cells

    def _checks(self, project: str, meta: dict) -> list[dict]:
        """Required check slots, including checks which have never been run: LVS of a changed cell that has a schematic
        and a layout, with its ERC (from the LVS run's connectivity, where the PDK's profile says how its taps are
        drawn: erc_tap), and DRC of a changed layout with the PDK's KLayout deck and, where the profile says so (magic_drc),
        Magic's drc(full). `checker` says which tool: klayout | magic."""
        checks = []
        work, base = meta["preview"], meta["baseline"]
        changed = self._changed_cells(meta, self.changes(project, meta))
        skip = self.unused_generated(project, meta, changed)
        for (lib, cell), views in sorted(changed.items()):
            if (lib, cell) in skip:
                continue
            wprj = self.srv.project(work)
            lib_path = wprj.library(lib).path
            has_sch = (lib_path / f"{cell}.sch").is_file()
            # a layout as it is in memory too: a library whose layout was never saved yet (a reader commits nothing)
            has_lay = ((wprj.library(lib).has_layout() or (work, lib) in getattr(self.srv, "dbs", {}))
                       and self.srv.db(work, lib).layout.cell(cell) is not None)
            profile = wprj.pdk.profile if wprj.pdk else None
            slots = []
            if has_lay and has_sch:
                slots += [("lvs", "klayout")] + ([("erc", "klayout")] if profile and profile.erc_tap else [])
            if has_lay and "layout" in views:
                slots += [("drc", "klayout")] + ([("drc", "magic")] if profile and profile.magic_drc else [])
            for side, name in (("work", work), ("base", base)):
                if side == "base" and not self._has_layout_cell(base, lib, cell):
                    continue  # a cell this changeset adds: nothing to compare with ("before -")
                checks += [{"side": side, "kind": kind, "checker": checker, "lib": lib, "cell": cell, "project": name}
                           for kind, checker in slots]
        return checks

    def unused_generated(self, project: str, meta: dict, changed: dict | None = None) -> list[tuple[str, str]]:
        """The cells the changeset changed that are PCell variants Undine generated (or sync_layout's cells round them)
        and that nothing in the working copy places: no check runs on them (a variant is no design of its own: its
        DRC alone fails Magic's LU.2, the latch-up tap it has only where it is placed); delete_unused_cells takes them
        out. `changed`: the changed cells (_changed_cells) when the caller has them."""
        from undine.layouts import sdl

        work, out, unused = meta["preview"], [], {}
        if changed is None:
            changed = self._changed_cells(meta, self.changes(project, meta))
        for lib, cell in sorted(changed):
            if lib not in unused:
                try:
                    wprj = self.srv.project(work)
                    db = (self.srv.db(work, lib) if wprj.library(lib).has_layout()
                          or (work, lib) in getattr(self.srv, "dbs", {}) else None)     # in memory only too (a reader's)
                    with db.lock if db is not None else contextlib.nullcontext():
                        unused[lib] = set(sdl.unused_generated(db.layout)) if db is not None else set()
                except Exception:
                    unused[lib] = set()
            if cell in unused[lib]:
                out.append((lib, cell))
        return out

    @staticmethod
    def _check_key(row: dict) -> tuple:
        """A slot's key (a row recorded before checkers were named is KLayout's)."""
        return (*(row.get(k) for k in ("side", "kind", "lib", "cell")), row.get("checker") or "klayout")

    def verify(self, project: str, cid: str, fresh: bool = False, commit: bool = True) -> dict:
        """Run missing/outdated checks, reusing jobs with identical inputs just like tapeout sign-off (`fresh`: every
        check runs anew). The working copy is committed first (outside the changesets' lock: git work is slow), so the
        branch has what was checked; not for a reader (commit False: an agent that checks another's changeset, which
        changes nothing of it but the checks it records; each check records the inputs it ran on, layouts as they are
        in memory)."""
        meta = self._load(project, cid)
        self._check_open(meta)
        if commit:
            self._commit_before(project, meta, "the checks")
        return self._verify(project, cid, fresh)

    @_serialized
    def _verify(self, project: str, cid: str, fresh: bool = False) -> dict:
        """Each slot's job, and whether it was `reused` (a job that had run on the same inputs) or started now; an ERC
        slot takes the LVS run of its side and cell."""
        meta = self._load(project, cid)
        self._check_open(meta)
        jobs, lvs = [], {}
        prepare = {("lvs", "klayout"): self.srv.prepare_lvs, ("drc", "klayout"): self.srv.prepare_drc,
                   ("drc", "magic"): self.srv.prepare_magic_drc}
        for check in self._checks(project, meta):
            where = (check["side"], check["lib"], check["cell"])
            if check["kind"] == "erc":
                jobs.append({**check, **lvs.get(where, {"state": "failed", "error": "no LVS run to take it from"})})
                continue
            try:
                run = prepare[check["kind"], check["checker"]](check["project"], check["lib"], check["cell"])
                job = None if fresh else self.srv.reusable(check["project"], run.inputs)
                # Old or incomplete status records may have a key but no auditable inputs.
                if job and (job.status().get("inputs") != run.inputs or
                            self._job_status(job.path).get("state") not in ("done", "queued", "running")):
                    job = None
                row = {**check, "reused": job is not None}
                row["job"] = (job or run.launch()).id
            except Exception as e:
                row = {**check, "state": "failed", "error": str(e)}
            jobs.append(row)
            if check["kind"] == "lvs":
                lvs[where] = {k: row[k] for k in ("job", "reused", "state", "error") if k in row}
        meta["verify"] = jobs
        self._save(project, meta)
        return self.get(project, cid)

    def _has_layout_cell(self, project: str, lib: str, cell: str) -> bool:
        try:
            return self.srv.project(project).library(lib).has_layout() and \
                self.srv.db(project, lib).layout.cell(cell) is not None
        except KeyError:
            return False

    def verify_status(self, project: str, meta: dict) -> list[dict]:
        """The slots with their results: state, ok, freshness (fresh, changed), detail, the job, when it finished and
        whether the last run of the checks reused it; an ERC row its findings (warnings: ok false, not a violation)."""
        out = []
        previous = {self._check_key(j): j for j in meta.get("verify", [])}
        rows = [previous.get(self._check_key(c), c) for c in self._checks(project, meta)] \
            if meta["status"] == "open" else meta.get("verify", [])
        for j in rows:
            if j.get("kind") == "error":
                out.append(j)
                continue
            if not j.get("job"):
                out.append({**j, "state": "failed" if j.get("error") else "not_run", "ok": None,
                            "fresh": None, "changed": []})
                continue
            try:
                job = self.srv.jobs.get(self.srv.project(j["project"]).path, j["job"])
                st = self._job_status(job.path)
            except Exception:
                out.append({**j, "state": "unknown", "ok": None, "fresh": None, "changed": []})
                continue
            try:
                fresh = provenance.freshness(self.srv, j["project"], st)
            except Exception as e:
                fresh = {"fresh": None, "changed": [f"cannot determine freshness: {e}"]}
            s = st.get("summary") or {}
            row = {**j, "state": st["state"], "ok": None, **fresh, **({"error": st["error"]} if st.get("error") else {}),
                   "summary": {k: s[k] for k in ("total", "match") if k in s}, "detail": st.get("error"),
                   **({"finished": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st["finished"]))}
                      if st.get("finished") else {})}
            if st["state"] == "done" and j["kind"] == "erc":
                row.update(self._erc(j, job, st))
            elif st["state"] == "done":
                row["ok"] = s.get("match") if j["kind"] == "lvs" else (s["total"] == 0 if "total" in s else None)
                row["detail"] = f"{s.get('total')} violations" if j["kind"] == "drc" else \
                    ("match" if s.get("match") else "mismatch")
            out.append(row)
        return out

    def _erc(self, row: dict, job: Job, st: dict) -> dict:
        """An ERC row's result from its LVS run (made once and kept with that run: verify.erc). Its summary is always
        its own: the LVS run's {"match"} would read as the ERC's result in the review."""
        from undine.checks import verify

        if "erc" not in st:                           # an LVS run made before its ERC was
            prj = self.srv.project(row["project"])
            taps = verify.tap_layers(prj.pdk.profile) if prj.pdk else []
            if not taps or not (job.path / "result.lvsdb").is_file():
                return {"ok": None, "summary": {}, "detail": "no ERC for this run"}
            st = job.set(erc=verify.erc(job.path, taps))
        erc = st["erc"]
        if erc.get("error"):
            return {"ok": None, "summary": {}, "detail": f"ERC failed: {erc['error']}"}
        found, more = erc.get("findings") or [], erc.get("more", 0)
        what = "; ".join(f"{f['instance']} {f['terminal']} ({f['net']}) against the {f['tap_net']} tap" for f in found[:4])
        n = len(found) + more
        return {"ok": not n, "severity": "warning", "summary": {"warnings": n}, "findings": found,
                **({"findings_more": more} if more else {}),
                "detail": f"{n} body tap{'s' * (n != 1)} abut a source / drain of another net: {what}"
                          + (f" and {n - 4} more" if n > 4 else "") if n else
                          "no body tap abuts a source / drain of another net"}

    # ------------------------------------------------------------------ apply / revert

    @_serialized
    def _check_writer(self, cid: str) -> None:
        """No one else is writing the changeset: the writer holding it may (an agent restoring its own changeset)."""
        if (le := self.srv.leases.get(cid)) and le.owner != live.writer():
            raise ChangesetError(f"{live.holder(le)} is editing the changeset; stop it or wait until it finishes")

    def apply(self, project: str, cid: str, actor: str = "user") -> dict:
        """Merges the changeset into the project (module docstring; docs/design/changeset-branches.md, Apply): the
        working copy committed (the tip), the evidence archived, then the merge of the archived tip (what was
        reviewed). A conflict: MergeConflict, nothing written, the changeset still open. An older copy is refused
        (OLD_FORM). Seen as it waits and runs (_closing): committing, evidence, merging, cleanup."""
        with self._closing(project, cid, "apply", actor):
            return self._apply(project, cid, actor)

    @_serialized
    def _apply(self, project: str, cid: str, actor: str) -> dict:
        meta = self._load(project, cid)
        self._check_open(meta)
        if not meta.get("git"):
            raise ChangesetError(OLD_FORM.format(cid=cid))
        self._check_writer(cid)
        self._check_merging(project, cid)
        progress = self._progress(cid)
        with _busy(self._shadow_root(project, cid)):
            progress.phase("committing")
            self._commit_before(project, meta, "applying it")
            meta["libs"] = self._libs(meta)          # those its working copy made too, while it is open
            manifest = self._archive(project, meta)
            tip = manifest["commits"]["tip"] or meta["git"]["base"]
            bound = self._tip_state(project, cid).get("gseq") or 0      # the records the tip was committed with
            progress.phase("merging")
            try:
                merged = self._merge(project, meta, "apply", meta["git"]["base"], tip, actor, manifest, bound)
            except ChangesetError:
                raise
            except Exception as e:
                raise ChangesetError(f"cannot apply {cid}: {e}") from e
            self._note(meta, None)                   # an interrupted apply's note: done now
            meta["status"] = "applied"
            meta["applied"] = {**merged, "tip": tip}
            self._save(project, meta)
            self._merging_file(project, cid).unlink(missing_ok=True)
            progress.phase("cleanup")
            self._cleanup(project, meta, manifest)
        return {**meta, "git": self._git_info(project, meta)}

    def revert(self, project: str, cid: str, actor: str = "user") -> dict:
        """Takes an applied changeset back (module docstring; docs/design/changeset-branches.md, Revert): the merge
        from the tip it applied to its base, the files it added kept. One applied before changesets were git branches
        cannot be reverted any more. Seen as it waits and merges (_closing)."""
        with self._closing(project, cid, "revert", actor):
            return self._revert(project, cid, actor)

    @_serialized
    def _revert(self, project: str, cid: str, actor: str) -> dict:
        meta = self._load(project, cid)
        self._check_open(meta, "applied")
        g = meta.get("git")
        if not g:
            raise ChangesetError(f"changeset {cid} was applied before changesets were git branches: it cannot be "
                                 "reverted any more")
        self._check_merging(project, cid)
        d = self.srv.project(project).path
        tip = (meta.get("applied") or {}).get("tip") or (branches.resolve(d, g["ref"]) if g.get("ref") else None) \
            or g.get("tip") or g["base"]
        with _busy(self._shadow_root(project, cid)):
            self._progress(cid).phase("merging")
            try:
                merged = self._merge(project, meta, "revert", tip, g["base"], actor)
            except ChangesetError:
                raise
            except Exception as e:
                raise ChangesetError(f"cannot revert {cid}: {e}") from e
            self._note(meta, None)
            meta["status"] = "reverted"
            meta["reverted"] = merged
            self._save(project, meta)
            self._merging_file(project, cid).unlink(missing_ok=True)
        return {**meta, "git": self._git_info(project, meta)}

    def _check_merging(self, project: str, cid: str) -> None:
        """Refuses to close a changeset whose interrupted merge _load could not finish nor take back (it would be
        merged twice, or discarded with its merge in the project)."""
        if self._merging_file(project, cid).is_file():
            raise ChangesetError(f"an interrupted merge of {cid} is still to be finished: open the project again, or "
                                 "restart Undine, then try again")

    def _merge(self, project: str, meta: dict, action: str, base: str, theirs: str, actor: str,
               manifest: dict | None = None, bound: int | None = None) -> dict:
        """Merges commit `theirs` into the project from commit `base` (apply: from the changeset's base to its tip,
        its records up to gseq `bound` when the tip has no log; revert: from the tip to the base, the files it added
        kept), all or nothing, the write a compare-and-swap, merging.json kept while it is written (module
        docstring). Returns {"t", "by", "commit", "uncommitted", "why", "paths"} (a revert: "kept" too; _left)."""
        revert = action == "revert"
        cid = meta["id"]
        prj = self.srv.project(project)
        d = prj.path
        commit = None
        state_file = self._merging_file(project, cid)
        changed = self._diff_paths(d, base, theirs)
        watch = sorted(set(changed) | {f"history/{lib}.jsonl" for lib in set(_lib_dirs(prj)) | set(meta.get("libs") or [])})
        with self._project_locked(project, changed) as dbs, branches.lock(d) as repo:
            clean = branches.is_clean(d)                     # before anything is written (a save neither)
            seen = self._digests(d, watch)                   # the project's files as the plan merges them ("ours")
            plan = self._plan(prj, meta, base, theirs, revert, bound)
            self._refuse(plan, cid, action)
            saved = self._save_project(dbs, self._touched(prj, plan), cid, action)
            t = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            try:
                if saved:                                    # the merge of the saved files, checked again
                    seen = self._digests(d, watch)
                    plan = self._plan(prj, meta, base, theirs, revert, bound)
                    self._refuse(plan, cid, action)
                writes = {**plan["writes"], **self._logs(prj, meta, plan, revert, actor)}
                # project.json / undine.lock also kept from the CLI's lib commands and a dependency operation
                # (deps.meta_lock) from the compare-and-swap to the write
                meta_files = deps.LOCK in writes or "project.json" in writes
                with deps.meta_lock(d) if meta_files else contextlib.nullcontext():
                    self._unchanged(d, writes, seen, cid, action)
                    message = self._merge_message(project, meta, action, actor, manifest)
                    # each file's digest before and after; a log's sizes too ("size": the merged log's, which records
                    # appended since leave in front; "ours_size": the project's log, which the merged one starts with)
                    state = {"action": action, "t": t, "by": actor, "theirs": theirs, "head": repo.head, "clean": clean,
                             "subject": message.split("\n", 1)[0],
                             "files": {rel: {"ours": seen[rel] if rel in seen else _file_digest(d / rel),
                                             "new": _digest(data), **self._sizes(d, rel, data)}
                                       for rel, data in sorted(writes.items())},
                             "record": {"libs": meta.get("libs"), "evidence": meta.get("evidence")}}
                    if revert:
                        state["kept"] = [p for p in plan["kept"] if gitmerge.kind_of(p) != "log"]
                    self._write_merging(state_file, state)       # before anything is written: what a recovery needs
                    try:
                        written = branches.write_plan(d, {"writes": writes})
                    except Exception:
                        state_file.unlink(missing_ok=True)       # nothing was written (write_plan put it all back)
                        raise
            except BaseException:
                self._unsave(saved)
                raise
            state["written"] = True                          # all of it renamed in: a recovery finishes it
            self._write_merging(state_file, state)
            paths = set(written) | {p for s in saved for p in s["paths"]} | self._sync_netlists(project, written)
            self._write_merging(state_file, {**state, "paths": sorted(paths)})
            for db in dbs.values():                         # held: nothing is drawn on the files as they were
                self._reread_layout(db)
            if clean and repo.head and paths:
                try:
                    commit = branches.commit_merge(d, [repo.head] if revert else [repo.head, theirs], message, actor,
                                                   self._committable(d, paths))
                except (RuntimeError, ValueError, OSError) as e:     # the merge is written: it stays uncommitted
                    print(f"undine: cannot commit the {action} of {cid}: {e}", file=sys.stderr)
                else:
                    self._write_merging(state_file, {**state, "paths": sorted(paths), "commit": commit})
        self._reread(project)
        out = {"t": t, "by": actor, "commit": commit, **self._left(commit, paths, repo.head, clean),
               "paths": sorted(paths)}
        return {**out, "kept": state["kept"]} if revert else out

    @staticmethod
    def _left(commit: str | None, paths, head: str | None, clean: bool | None) -> dict:
        """A merge's "uncommitted" (it wrote something and made no commit: it is in the working tree) and why, from
        the user's HEAD and is_clean before it: "no_commit" (the branch has no commit yet: the project's first commit
        is History's to make), "changes" (the project had other uncommitted changes), "failed" (the commit failed,
        or Undine stopped before it); None when committed or nothing was written."""
        left = commit is None and bool(paths)
        return {"uncommitted": left,
                "why": None if not left else "no_commit" if not head else "changes" if not clean else "failed"}

    @staticmethod
    def _sizes(d: Path, rel: str, data: bytes | None) -> dict:
        """merging.json's sizes of a log the merge writes as `data` (the project's, held): {"size", "ours_size"}."""
        if data is None or gitmerge.kind_of(rel) != "log":
            return {}
        return {"size": len(data), "ours_size": (d / rel).stat().st_size if (d / rel).is_file() else None}

    @staticmethod
    def _diff_paths(d: Path, base: str, theirs: str) -> list[str]:
        """The files of the project (paths from its directory) that differ between commits `base` and `theirs`."""
        out = branches._git(d, "diff", "--name-only", "-z", "--no-renames", "--relative", base, theirs, "--", ".")
        return sorted(set(filter(None, out.decode("utf-8", "surrogateescape").split("\0"))))

    @staticmethod
    def _digests(d: Path, rels) -> dict[str, str | None]:
        return {rel: _file_digest(d / rel) for rel in rels}

    @staticmethod
    def _unchanged(d: Path, writes: dict, seen: dict, cid: str, action: str) -> None:
        """The compare-and-swap of a merge's write (module docstring): each file it writes is still what the plan
        merged as the project's (`seen`, read before it was made); else MergeConflict, nothing written (an edit that
        did not wait for the project's locks came meanwhile: another process, a script writing the file)."""
        changed = [rel for rel in sorted(writes) if rel in seen and _file_digest(d / rel) != seen[rel]]
        if not changed:
            return
        more = len(changed) - 1
        raise MergeConflict(
            f"cannot {action} {cid}: {changed[0]} changed in the project while the merge was being made"
            + (f" (and {more} more file{'s' * (more != 1)})" if more else "") + f"; nothing was written: {action} it again",
            [{"path": rel, "kind": gitmerge.kind_of(rel) or "text", "conflicts": [
                {"id": "changed", "what": f"{rel} changed in the project while the merge was being made: {action} the "
                 "changeset again to merge it with that edit", "base": None, "ours": "changed", "theirs": None,
                 "taken": "ours", "resolved": False}]} for rel in changed])

    @staticmethod
    def _committable(d: Path, paths) -> list[str]:
        """The paths a merge commit takes: what git ignores and does not track (the logs, when the project's
        .gitignore leaves history/ out) stays out of the user's branch."""
        listed = sorted(paths)
        r = branches._run(d, "check-ignore", "-z", "--stdin",
                          input=b"".join(p.encode("utf-8", "surrogateescape") + b"\0" for p in listed))
        ignored = set(filter(None, r.stdout.decode("utf-8", "surrogateescape").split("\0"))) if r.returncode == 0 else set()
        return [p for p in listed if p not in ignored]

    @contextmanager
    def _project_locked(self, project: str, extra=()):
        """Holds what writes the project's files while a merge goes into it (as symbolsync does for a symbol edit), in
        one order: the editors (of every schematic, symbol, intent and RTL file of its libraries, of the intent and
        RTL files `extra` (paths from the project directory: those the merge may add) names, and those open: an
        intent or RTL edit asks the server for its editor, which is then this one, held), its layouts (open, or with a
        journal: the merge may save them; given as {lib: LayoutDB}), the netlists' lock (the watcher reads a
        schematic changed on disk under it and then writes a log), its logs, by path."""
        from undine.design import intent as intentmod, rtl
        prj = self.srv.project(project)
        d = prj.path
        libs = _lib_dirs(prj)                               # the project's own: never read-only
        editors = {}
        views: set[tuple[str, str, str]] = set()
        for lib, lib_dir in libs.items():
            for f in sorted(lib_dir.glob("*.sch")) + sorted(lib_dir.glob("*.sym")):
                ed = self.srv.editor(project, lib, f.stem, "schematic" if f.suffix == ".sch" else "symbol")
                editors[ed.path] = ed
            views |= {(lib, f.name[:-len(intentmod.SUFFIX)], "intent") for f in lib_dir.glob(f"*{intentmod.SUFFIX}")}
            views |= {(lib, f.stem, "rtl") for f in rtl.sources(lib_dir)}
        for rel in extra:
            f = d / rel
            lib = next((n for n, p in libs.items() if f.parent == p), None)
            if lib is not None and f.name.endswith(intentmod.SUFFIX):
                views.add((lib, f.name[:-len(intentmod.SUFFIX)], "intent"))
            elif lib is not None and f.suffix in rtl.SUFFIXES:
                views.add((lib, f.stem, "rtl"))
        for lib, cell, view in sorted(views):
            try:
                self.srv.editor(project, lib, cell, view)    # made (and kept) by the server, as an edit gets it
            except (ValueError, KeyError):
                continue                                    # no cell name
        with self.srv.lock:
            editors.update({Path(k): ed for k, ed in self.srv.editors.items()
                            if Path(k).is_relative_to(d) and Path(k) not in editors})     # intent, RTL, netlist
        dbs = self._layouts(project, libs)
        logs = [self.srv.oplog(project, lib) for lib in libs]
        with ExitStack() as stack:
            for _, ed in sorted(editors.items(), key=lambda kv: str(kv[0])):
                stack.enter_context(ed.lock)
            for _, db in sorted(dbs.items()):
                stack.enter_context(db.lock)
            stack.enter_context(netlist._LOCK)
            for log in sorted(logs, key=lambda log: str(log.path)):
                stack.enter_context(log.lock)
            yield dbs

    def _plan(self, prj, meta: dict, base: str, theirs: str, revert: bool, bound: int | None = None) -> dict:
        """branches.plan_merge from `base` to `theirs` (a revert keeps the files the changeset added), its logs taken
        out ("logs": _logs writes them; "records": the changeset's, by library, _records_of) and the checks across
        files added to its conflicts."""
        d = prj.path
        plan = branches.plan_merge(d, base, theirs, keep_added=revert)
        if revert:
            self._keep_libraries(prj, meta, plan, base, theirs)
        logs = [p for p in plan["paths"] if gitmerge.kind_of(p) == "log"]
        plan["writes"] = {p: v for p, v in plan["writes"].items() if p not in logs}
        plan["conflicts"] = [c for c in plan["conflicts"] if c["path"] not in logs]
        plan["warnings"] = [w for w in plan["warnings"] if w["path"] not in logs]
        plan["paths"] = [p for p in plan["paths"] if p not in logs]
        tip = base if revert else theirs
        plan["records"] = self._records_of(prj, meta, tip, revert, bound)
        plan["logs"] = sorted(set(logs) | {f"history/{lib}.jsonl" for lib in plan["records"]})
        self._symbol_conflicts(prj, meta, plan, self._transactions(plan["records"]), tip, revert)
        if not revert:
            self._journal_conflicts(plan)
        return plan

    @staticmethod
    def _keep_libraries(prj, meta: dict, plan: dict, tip: str, base: str) -> None:
        """A revert keeps the files the changeset added (a cell it made may be used elsewhere since), so it keeps the
        project.json entry of each library the changeset added (in the tip's project.json, not in the base's) whose
        folder still has files: the merge of project.json from the tip to the base takes it out, and the cells kept
        there, and the work done in them since, would no longer be in the project. The entry as the project has it."""
        d, rel = prj.path, "project.json"
        if plan["writes"].get(rel) is None:
            return

        def libraries(data: bytes | None) -> dict:
            try:
                specs = json.loads(data).get("libraries") if data is not None else None
            except (ValueError, AttributeError):
                return {}
            return specs if isinstance(specs, dict) else {}

        prefix = meta["git"].get("prefix") or ""
        added = set(libraries(_blob(d, prefix, tip, rel))) - set(libraries(_blob(d, prefix, base, rel)))
        ours = libraries((d / rel).read_bytes() if (d / rel).is_file() else None)
        try:
            merged = json.loads(plan["writes"][rel])
            have = merged.get("libraries")
            have = {} if have is None else have
        except (ValueError, AttributeError):
            return
        if not isinstance(have, dict):
            return
        gone = {Path(r) for r, data in plan["writes"].items() if data is None}

        def has_files(spec) -> bool:
            path = spec if isinstance(spec, str) else spec.get("path") if isinstance(spec, dict) else None
            if not isinstance(path, str) or Path(path).expanduser().is_absolute():
                return False                             # a dependency, or a library outside the project
            folder = d / path
            return folder.is_dir() and any(f.is_file() and f.relative_to(d) not in gone for f in folder.rglob("*"))

        keep = {n: spec for n, spec in ours.items() if n in added and n not in have and has_files(spec)}
        if not keep:
            return
        merged["libraries"] = {n: have.get(n, keep.get(n)) for n in [*(n for n in ours if n in have or n in keep),
                                                                      *(n for n in have if n not in ours)]}
        if merged == json.loads((d / rel).read_bytes()):
            del plan["writes"][rel]                      # the project's project.json as it is
        else:
            plan["writes"][rel] = (json.dumps(merged, indent=2) + "\n").encode()

    def _records_of(self, prj, meta: dict, tip: str, revert: bool, bound: int | None) -> dict[str, list[dict]]:
        """{lib: the changeset's records after its base, in order} (module docstring, apply): the tip's log of each
        library; when the tip has none (the project's .gitignore leaves history/ out of git: no commit has the logs),
        an apply's from the working copy's log file, up to gseq `bound` (what the tip was committed with), a
        revert's those its apply put in the project's log (tagged with it, "merged_seq")."""
        d, g, cid = prj.path, meta["git"], meta["id"]
        floor = g.get("base_seq") or {}
        libs = sorted(set(meta.get("libs") or []) | set(_lib_dirs(prj))) if revert else self._libs(meta)
        out = {}
        for lib in libs:
            data = _blob(d, g.get("prefix") or "", tip, f"history/{lib}.jsonl")
            if data is not None:
                recs = [r for _, r in logmerge.records(data) if r["seq"] > floor.get(lib, 0)]
            elif revert:
                try:
                    recs = [r for r in self.srv.oplog(meta["project"], lib).read(limit=10**9)
                            if r.get("changeset") == cid and "merged_seq" in r and r["op"] != "merge"]
                except KeyError:
                    recs = []
            else:
                try:
                    recs = [r for r in self._own(meta["preview"], lib)
                            if bound is None or (r.get("gseq") or 0) <= bound]
                except KeyError:                             # no working copy: nothing to read the records from
                    recs = []
            if recs:
                out[lib] = recs
        return out

    @staticmethod
    def _refuse(plan: dict, cid: str, action: str) -> None:
        if not plan["conflicts"]:
            return
        first = plan["conflicts"][0]
        more = len(plan["conflicts"]) - 1
        raise MergeConflict(f"cannot {action} {cid}: conflict in {first['path']}: {first['conflicts'][0]['what']}"
                            + (f" (and {more} more file{'s' * (more != 1)})" if more else "") + "; nothing was written",
                            [{k: c[k] for k in ("path", "kind", "conflicts")} for c in plan["conflicts"]])

    @staticmethod
    def _transactions(records: dict[str, list[dict]]) -> list[dict]:
        """The changeset's symbol transactions in effect at its tip (its net records after the base, `records` by
        library, with a symbol_group): [{"id", "source": (lib, cell, view), "members": {...}, "views": the views its
        records change, "consistent": every record names the same members}]."""
        found: dict[str, dict] = {}

        def key(m: dict) -> tuple[str, str, str]:
            return m["lib"], m["cell"], m["view"]

        for lib, recs in sorted(records.items()):
            for r in oplog._net(recs):
                grp = r.get("symbol_group")
                if not isinstance(grp, dict) or r.get("view") not in ("schematic", "symbol"):
                    continue
                members = {key(m) for m in grp.get("members") or []}
                t = found.setdefault(grp["id"], {"id": grp["id"], "source": key(grp["source"]), "members": members,
                                                 "views": set(), "consistent": True})
                t["consistent"] = t["consistent"] and members == t["members"]
                t["views"].add((lib, r["cell"], r["view"]))
        return list(found.values())

    def _symbol_conflicts(self, prj, meta: dict, plan: dict, transactions: list[dict], tip: str, revert: bool) -> None:
        """The checks of the changeset's symbol transactions on the merged result, added to the plan's conflicts (module
        docstring): each member view as it was where the merge starts (the base; a revert: the tip), and no other
        schematic using the symbol whose wiring would have to follow it (a three-way merge of each file would merge a
        concurrent edit of a member, and leave a consumer placed since unconnected, without a word)."""
        if not transactions:
            return
        d, g = prj.path, meta["git"]
        prefix = g.get("prefix") or ""
        libraries = prj.libraries()
        doing, since = ("reverting", "applied") if revert else ("applying", "opened")
        start, other = (tip, g["base"]) if revert else (g["base"], tip)

        def rel_of(lib: str, cell: str, view: str) -> str | None:
            if lib not in libraries:
                return None
            f = libraries[lib].path / f"{cell}.{'sch' if view == 'schematic' else 'sym'}"
            return f.relative_to(d).as_posix() if f.is_relative_to(d) else None

        def ours(rel: str) -> bytes | None:
            return (d / rel).read_bytes() if (d / rel).is_file() else None

        def merged(rel: str) -> bytes | None:
            return plan["writes"][rel] if rel in plan["writes"] else ours(rel)

        def name(k: tuple) -> str:
            return f"{k[0]}/{k[1]} ({k[2]})"

        sources: dict[tuple, set] = {}
        for t in transactions:
            src = name(t["source"])
            if not t["consistent"] or t["views"] != t["members"]:
                _add(plan, rel_of(*t["source"]) or f"history/{t['source'][0]}.jsonl", {
                    "id": f"symbol:{t['id']}", "what": f"the changeset's symbol transaction of {src} is incomplete (its "
                    "records do not cover the views it changed)", "base": None, "ours": None, "theirs": None,
                    "taken": "ours", "resolved": False})
                continue
            sources.setdefault(t["source"], set()).update(t["members"])
            for k in sorted(t["members"]):
                rel = rel_of(*k)
                if rel is None or rel in plan["kept"]:      # a file the changeset added: a revert leaves it as it is
                    continue
                now, then = ours(rel), _blob(d, prefix, start, rel)
                if symbolsync.fingerprint(_text(now)) != symbolsync.fingerprint(_text(then)):
                    _add(plan, rel, _whole(f"symbol:{t['id']}", f"{name(k)} changed in the project since the changeset "
                                           f"was {since}, and it is part of the changeset's symbol transaction of {src}: "
                                           f"undo those edits before {doing} it", then, now,
                                           _blob(d, prefix, other, rel)))
        resolver = xschem.SymbolResolver(prj.symbol_paths())
        for source, members in sorted(sources.items()):
            rel = rel_of(*source)
            a, b = (ours(rel), merged(rel)) if rel and source[2] == "symbol" else (None, None)
            if a is None or b is None or a == b:
                continue
            old, new = xschem.parse(_text(a)), xschem.parse(_text(b))
            if (symbolsync._interface(old), symbolsync._positions(old)) == \
                    (symbolsync._interface(new), symbolsync._positions(new)):
                continue                                     # its pins stay: no wiring follows
            path = (d / rel).resolve()
            consumers: dict[tuple, tuple] = {}

            def uses(data: bytes | None, folder: Path) -> bool:
                """The schematic `data` (a file in `folder`) places the symbol at `path`."""
                return data is not None and any(c.embedded is None and (p := resolver.find(c.sym, folder)) is not None
                                                and p.resolve() == path for c in xschem.parse(_text(data)).comps)
            for lib, library in libraries.items():
                if not library.path.is_dir():
                    continue
                schs = set(library.path.glob("*.sch")) | {d / w for w, data in plan["writes"].items()
                                                         if data is not None and w.endswith(".sch")
                                                         and (d / w).parent == library.path}
                for sch in sorted(schs):
                    k = (lib, sch.stem, "schematic")
                    r = sch.relative_to(d).as_posix() if sch.is_relative_to(d) else None
                    data = merged(r) if r else sch.read_bytes()
                    if k in members or data is None:
                        continue
                    # taken whole from the side whose symbol the merge leaves (apply: the changeset's tip; revert: the
                    # base) and with no instance of the symbol where the merge starts (apply: the base; revert: the
                    # tip): every instance in it was placed after the symbol changed, against its new pins (a bench
                    # the changeset drew after the pin move), so nothing has to follow. An instance that was there
                    # before is still checked: an undo can restore one with its old wiring next to the new symbol.
                    if r is not None and data == _blob(d, prefix, other, r) and not uses(_blob(d, prefix, start, r), sch.parent):
                        continue
                    model = xschem.parse(_text(data))
                    if any(c.embedded is None and (p := resolver.find(c.sym, sch.parent)) is not None
                           and p.resolve() == path for c in model.comps):
                        consumers[k] = (sch, r, model)
            if not consumers:
                continue
            editors = {k: SchEditor(sch, resolver) for k, (sch, _, _) in consumers.items()}
            try:
                following, why = symbolsync._plan_consumers(prj, editors, SchEditor(d / rel, resolver), old, new,
                                                            {k: m for k, (_, _, m) in consumers.items()}), None
            except (ValueError, PermissionError) as e:
                following, why = dict.fromkeys(consumers), str(e)
            for k in sorted(following):
                sch, r, _ = consumers[k]
                _add(plan, r or str(sch), {
                    "id": f"symbol-consumer:{k[0]}/{k[1]}",
                    "what": why or f"new dependent wiring in {k[0]}/{k[1]} is not part of the changeset's symbol "
                                   f"transaction of {name(source)}: update or undo those later instances before "
                                   f"{doing} it",
                    "base": "file" if r and _blob(d, prefix, start, r) is not None else None,
                    "ours": "changed" if r and _blob(d, prefix, start, r) is not None else "added",
                    "theirs": None, "taken": "ours", "resolved": False})

    @staticmethod
    def _journal_conflicts(plan: dict) -> None:
        """A layout edit of the changeset its tip has in a journal only (not in its cell files) would be lost: the
        merge follows the cell files, and the log's merge record ends the journal. _commit_tip saves first, so this
        is a working copy that could not be saved."""
        for lib, recs in sorted(plan["records"].items()):
            pending = layout._journal(recs)[1]
            if pending:
                n = len(pending)
                _add(plan, f"history/{lib}.jsonl", {"id": logmerge.JOURNAL, "what": f"{n} unsaved layout edit{'s' * (n != 1)} of the "
                                 "changeset are not in its commit: save its layout in the working copy, then apply it",
                                 "base": None, "ours": None, "theirs": [f"seq:{s}" for s, _ in pending[:10]],
                                 "taken": "ours", "resolved": False})

    @staticmethod
    def _touched(prj, plan: dict) -> set[str]:
        """The project's libraries a merge writes into: those whose log the changeset wrote, and those it writes a file
        of (they get the merge record, and their layouts are saved first)."""
        libs = {Path(p).stem for p in plan["logs"]}
        dirs = _lib_dirs(prj)
        for rel in plan["writes"]:
            f = prj.path / rel
            libs |= {n for n, p in dirs.items() if f.is_relative_to(p)}
        return libs

    def _save_project(self, dbs: dict, libs: set[str], cid: str, action: str) -> list[dict]:
        """Saves the project's layouts of `libs` with unsaved edits, recorded (the system's, with the changeset): the
        merge takes part in them as files, and the log's merge record ends their journal. Returns what _unsave puts
        back, with the paths each save wrote ("paths", from the project directory) and what it left there ("wrote":
        {cell file: its digest, None when the save removed it}; "saved": the log's size and digest)."""
        done: list[dict] = []
        try:
            for lib in sorted(libs):
                db = dbs.get(lib)
                if db is None or not db.dirty:
                    continue
                lib_dir, log = db.lib.path, db.log.path
                files = {p: p.read_bytes() for p in layoutstore.cell_files(lib_dir)}
                entry = {"db": db, "files": files, "log": log, "data": log.read_bytes() if log.is_file() else None}
                with db.recording("save", {}, "system", f"saved before the {action} of {cid}", cid):
                    db.save()
                after = {p: p.read_bytes() if p.is_file() else None
                         for p in set(layoutstore.cell_files(lib_dir)) | set(files)}
                changed = [p for p, data in after.items() if files.get(p) != data]
                saved = log.read_bytes() if log.is_file() else b""
                d = log.parent.parent
                entry.update(wrote={p: _digest(after[p]) for p in changed}, saved=(len(saved), _digest(saved)),
                             paths=sorted(p.relative_to(d).as_posix() for p in [*changed, log] if p.is_relative_to(d)))
                done.append(entry)
        except BaseException:
            self._unsave(done)
            raise
        return done

    @staticmethod
    def _unsave(done: list[dict]) -> None:
        """Puts back what _save_project saved where it is still what the save left (what another writer wrote since
        is kept: a merge refused by its compare-and-swap): each cell file the save wrote as it was (one it made
        removed), the log as it was with the records appended since after it. The layout reads them again (its
        unsaved edits replayed from the journal, as when the library is opened)."""
        for s in reversed(done):
            kept = []
            for p, digest in sorted(s["wrote"].items()):
                if _file_digest(p) != digest:
                    kept.append(p.name)                  # changed since: not the save's any more
                elif p in s["files"]:
                    _put(p, s["files"][p])
                else:
                    p.unlink(missing_ok=True)
            log, (size, digest) = s["log"], s["saved"]
            try:
                data = log.read_bytes() if log.is_file() else b""
            except OSError:
                data = None
            if data is None or len(data) < size or _digest(data[:size]) != digest:
                kept.append(log.name)                    # rewritten since: its journal is what it says now
            elif s["data"] is None and len(data) == size:
                log.unlink(missing_ok=True)
            else:
                _put(log, (s["data"] or b"") + data[size:])
            if kept:
                print(f"undine: the layout of {s['db'].lib.name} saved before a merge was put back but for "
                      f"{', '.join(kept)}, which changed since", file=sys.stderr)
            try:
                s["db"].check_disk(force=True)
            except Exception as e:
                print(f"undine: cannot read the layout of {s['db'].lib.name} again: {e}", file=sys.stderr)

    def _logs(self, prj, meta: dict, plan: dict, revert: bool, actor: str) -> dict[str, bytes]:
        """The project's logs after the merge, for each library it touches (module docstring): the log as it is, an
        apply's records of the changeset (plan["records"]: after its base, renumbered to continue the log:
        "merged_seq" the number the working copy gave; an "of" and a "replay" follow), then the merge record naming
        the changeset."""
        d, cid = prj.path, meta["id"]
        mark = {"op": "merge", "actor": actor, "cell": None, "view": None, "args": {"changeset": cid,
                "action": "revert" if revert else "apply"}, "changeset": cid, "effects": [],
                "reason": f"{'reverted' if revert else 'applied'} changeset {cid}: {meta['title']}"}
        out = {}
        for lib in sorted(self._touched(prj, plan)):
            rel = f"history/{lib}.jsonl"
            f = d / rel
            mine = logmerge.records(f.read_bytes() if f.is_file() else b"")
            lines = [line for line, _ in mine]
            nxt = max((r["seq"] for _, r in mine), default=0) + 1
            new = [] if revert or rel not in plan["logs"] else plan["records"].get(lib, [])
            renum = {r["seq"]: nxt + i for i, r in enumerate(new)}
            for r in new:
                r2 = {**r, "seq": renum[r["seq"]], "merged_seq": r["seq"], "changeset": cid}
                if isinstance(r.get("of"), int) and r["of"] in renum:
                    r2["of"] = renum[r["of"]]
                if isinstance(r.get("replay"), list):
                    r2["replay"] = [renum.get(s, s) for s in r["replay"] if isinstance(s, int)]
                lines.append(json.dumps(r2, separators=(",", ":")))
            rec = {"v": oplog.VERSION, "seq": nxt + len(new), "t": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **mark}
            lines.append(json.dumps(rec, separators=(",", ":")))
            out[rel] = ("\n".join(lines) + "\n").encode()
        return out

    def _sync_netlists(self, project: str, written: list[str]) -> set[str]:
        """The netlists of the merged schematics read again from them (netlist.sync, so the watcher finds them read and
        records nothing), and of the schematics placing a merged symbol (its pins may have changed under them);
        returns those it changed."""
        prj = self.srv.project(project)                   # project.json may be merged too
        d = prj.path
        resolver = xschem.SymbolResolver(prj.symbol_paths())
        out = set()
        if syms := [d / rel for rel in written if rel.endswith(".sym") and (d / rel).is_file()]:
            written = list(dict.fromkeys([*written, *(s.relative_to(d).as_posix() for s in symbolsync.placing(prj, syms)
                                                      if s.is_relative_to(d))]))
        for rel in written:
            sch = d / rel
            if sch.suffix != ".sch" or not sch.is_file():
                continue
            npath = netlist.path_of(sch)
            before = npath.read_bytes() if npath.is_file() else None
            try:
                netlist.sync(sch, resolver)
            except Exception as e:                       # the schematic is merged; its netlist is read when it is next
                print(f"undine: cannot read the netlist of {rel} again: {e}", file=sys.stderr)
                continue
            if (npath.read_bytes() if npath.is_file() else None) != before:
                out.add(npath.relative_to(d).as_posix())
        return out

    @staticmethod
    def _reread_layout(db) -> None:
        try:
            db.check_disk(force=True)                    # it looks at most once a second otherwise
        except Exception as e:
            print(f"undine: cannot read the layout of {db.lib.name} again: {e}", file=sys.stderr)

    def _reread(self, project: str) -> None:
        """After a merge wrote the project's files: its open layouts read them again, its editors forget the symbols
        they read (and the connectivity derived from them)."""
        d = self.srv.project(project).path
        with self.srv.lock:
            dbs = [db for (name, _), db in self.srv.dbs.items() if name == project]
            eds = [ed for k, ed in self.srv.editors.items() if Path(k).is_relative_to(d)]
        for db in dbs:
            self._reread_layout(db)
        for ed in eds:
            if getattr(ed, "resolver", None) is not None:
                ed.resolver.cache.clear()
            if hasattr(ed, "_net_cache"):
                ed._net_cache = None

    @staticmethod
    def _merge_message(project: str, meta: dict, action: str, actor: str, manifest: dict | None) -> str:
        """The commit of an apply (a merge: "Apply <id>: <title>") or a revert: the reason, the changeset and its kept
        branch, who, and the checks the changeset closed with."""
        cid, g = meta["id"], meta["git"]
        revert = action == "revert"
        lines = [f"{'Revert' if revert else 'Apply'} {cid}: {meta['title']}", ""]
        lines += [meta["reason"], ""] if meta.get("reason") else []
        lines += [f"Changeset {cid} of {project}, the branch {g['branch']} (kept as {kept_ref(project, cid)})",
                  f"{'Reverted' if revert else 'Applied'} by: {actor}"]
        if not revert:
            rows = [r for r in (manifest or {}).get("verify") or [] if r.get("kind") in ("lvs", "drc", "erc")]
            lines += ["Checks:" if rows else "Checks: none run"]
            for r in rows:
                state = r.get("detail") or (r.get("state") or "not_run").replace("_", " ")
                lines.append(f"- {r['kind'].upper()}{' (Magic)' if r.get('checker') == 'magic' else ''} {r['lib']}/{r['cell']} "
                             f"({'proposal' if r['side'] == 'work' else 'baseline'}): {state}"
                             + ("; outdated" if r.get("fresh") is False else ""))
        return "\n".join(lines) + "\n"
