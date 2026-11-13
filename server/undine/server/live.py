"""Live changesets: one order across a changeset's libraries, operation ids, and one writer at a time.

See docs/design/live-changesets.md. Records written in a changeset's working copy get
    gseq       position in the changeset across all its libraries
    opid       the operation (a tool call, an HTTP request) the record belongs to
    turn       the assistant turn, or null
    changeset  the changeset's id (a record merged into the project says whose it was, however it was written)
and every design write in a working copy first asks `check_write`, which refuses it while someone else holds the
changeset's write lease (an assistant turn, or an MCP session).

A working copy is a git worktree whose logs start as the project's were (docs/design/changeset-branches.md): the
changeset's records are those after each library's `base_seq`; numbering looks at those only. The floor of numbering
(gseq.floor, below) lives beside the worktree, never in it: it is not the design's, and a commit of the working copy
would carry it into the project.

A lease that ends (released, stopped, lapsed) is noted (`ended`): the server commits the working copy then
(Server.lease_ended), outside whatever call noticed it.

A changeset being closed (applied, discarded, reverted) says how far its closure is (Closings): the phase, and while
its evidence is saved the files and bytes done of how many. The screens' `/live` poll, the list and the review show it,
so an adoption that saves gigabytes of runs, or that waits for another one (the changesets' lock), is seen as such.
"""
from __future__ import annotations

import contextvars
import json
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from undine.store import oplog

MCP_LEASE_S = 120.0

# who is writing (lease owner, e.g. "turn:t-1a2b" / "mcp:<session>" / "mcp:<session>:<agent>": one of several agents
# that share an MCP connection, mcp.py), and the operation being performed
_writer: contextvars.ContextVar[str | None] = contextvars.ContextVar("undine_writer", default=None)
_op: contextvars.ContextVar[dict | None] = contextvars.ContextVar("undine_op", default=None)
# a call that reads and checks another's changeset (agent_tools: changeset=): what it starts there is a reader's
_reading: contextvars.ContextVar[bool] = contextvars.ContextVar("undine_reading", default=False)


class WriteLocked(Exception):
    """Someone else is writing this changeset (HTTP 409); `lease`: theirs (None: the writer itself was stopped)."""

    def __init__(self, message: str, lease: "Lease | None" = None):
        super().__init__(message)
        self.lease = lease


@contextmanager
def acting(owner: str | None):
    tok = _writer.set(owner)
    try:
        yield
    finally:
        _writer.reset(tok)


@contextmanager
def reading(on: bool = True):
    """The calls inside read and check a changeset that is not the caller's (agent_tools.call: changeset=)."""
    tok = _reading.set(on)
    try:
        yield
    finally:
        _reading.reset(tok)


def reader() -> bool:
    """Whether this call reads another's changeset (reading)."""
    return _reading.get()


def started_by() -> dict:
    """What a job records of who started it: the writer (lease owner) the call runs as, and whether it reads another's
    changeset (reader). cancel_job lets a caller stop the jobs it started by it, also in a changeset it only reads."""
    owner = _writer.get()
    return {"owner": owner, **({"reader": True} if _reading.get() else {})} if owner else {}


@contextmanager
def operation(turn: str | None = None, opid: str | None = None):
    """Groups the records written inside into one operation; yields the list of `gseq`s they got."""
    committed: list[int] = []
    tok = _op.set({"opid": opid or "op-" + uuid.uuid4().hex[:10], "turn": turn, "committed": committed})
    try:
        yield committed
    finally:
        _op.reset(tok)


@dataclass
class Lease:
    owner: str
    kind: str                      # "turn" | "mcp"
    since: float = field(default_factory=time.time)
    expires: float | None = None
    stop_requested: bool = False
    label: str | None = None       # who, for people: "claude-desktop", "assistant"
    last_write: float = field(default_factory=time.time)

    def public(self) -> dict:
        return {"owner": self.owner, "kind": self.kind, "since": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(self.since)),
                "stop_requested": self.stop_requested, "label": self.label}


class Leases:
    """The write lease of each open changeset (in memory: a restarted server has no running turns)."""

    def __init__(self):
        self._l: dict[str, Lease] = {}
        self._revoked: set[str] = set()
        self._ended: set[str] = set()     # changesets whose lease ended since take_ended (module docstring)
        self.lock = threading.RLock()
        # called (outside the lock) when a writer is stopped: the MCP sessions a restarted server resumes keep it
        # (mcp.Sessions), so that a restart never lets a stopped agent write again
        self.on_stop = lambda: None

    def stopped(self) -> set[str]:
        """The writers that were stopped (stop): refused for good."""
        with self.lock:
            return set(self._revoked)

    def restore_stopped(self, owners) -> None:
        """Writers stopped before a restart (mcp.Sessions): refused here too."""
        with self.lock:
            self._revoked |= set(owners)

    def get(self, cs: str) -> Lease | None:
        with self.lock:
            le = self._l.get(cs)
            if le and le.expires is not None and le.expires < time.time():
                del self._l[cs]
                self._ended.add(cs)
                return None
            return le

    def take_ended(self) -> list[str]:
        """The changesets whose lease ended since the last call (and nobody holds now)."""
        with self.lock:
            self.held()                       # notices the leases that lapsed
            out = sorted(cs for cs in self._ended if cs not in self._l)
            self._ended.clear()
            return out

    def acquire(self, cs: str, owner: str, kind: str, label: str | None = None) -> Lease:
        """Takes or renews the lease; refused while someone else holds it."""
        with self.lock:
            self.check_stopped(owner)
            le = self.get(cs)
            if le and le.owner != owner:
                raise WriteLocked(_held(le), le)
            if not le:
                le = self._l[cs] = Lease(owner, kind, label=label)
            le.last_write = time.time()
            if kind == "mcp":
                le.expires = time.time() + MCP_LEASE_S
            return le

    def check_stopped(self, owner: str) -> None:
        """Refuses a writer that was stopped (stop), whatever it would write."""
        if owner in self._revoked:
            raise WriteLocked(_stopped(owner))

    def hand_over(self, cs: str | None, owner: str, to: str, label: str | None = None) -> None:
        """The lease `owner` holds on `cs`, if it holds it, held by `to` from now on (one session's agent going on
        under another name: mcp.Sessions.ctx)."""
        with self.lock:
            if cs and (le := self.get(cs)) is not None and le.owner == owner:
                le.owner, le.label = to, label

    def release(self, cs: str, owner: str) -> None:
        with self.lock:
            if (le := self._l.get(cs)) and le.owner == owner:
                del self._l[cs]
                self._ended.add(cs)

    def held(self) -> dict[str, Lease]:
        """The changesets someone is writing now (expired leases dropped)."""
        with self.lock:
            return {cs: le for cs in list(self._l) if (le := self.get(cs))}

    def release_owner(self, owner: str) -> None:
        """Releases what `owner` holds, and what the owners under it hold (`<owner>:<agent>`: the agents that share
        an MCP session)."""
        with self.lock:
            for cs in [cs for cs, le in self._l.items() if le.owner == owner or le.owner.startswith(owner + ":")]:
                del self._l[cs]
                self._ended.add(cs)

    def stop(self, cs: str) -> Lease | None:
        """A stop request: an assistant turn stops before its next step; an MCP session loses the lease for good."""
        with self.lock:
            le = self.get(cs)
            if not le:
                return None
            le.stop_requested = True
            if le.kind == "mcp":
                self._revoked.add(le.owner)
                del self._l[cs]
                self._ended.add(cs)
        if le.kind == "mcp":
            self.on_stop()
        return le

    def check(self, cs: str) -> None:
        """May the current writer change this changeset's working copy?"""
        me = _writer.get()
        if me and me in self._revoked:
            raise WriteLocked(_stopped(me))
        le = self.get(cs)
        if le and le.owner != me:
            raise WriteLocked(_held(le), le)


def holder(le: Lease) -> str:
    """Who holds a lease, for the person or the agent it refuses: the assistant, or an external agent by its name (its
    client, and its own name when several agents share one MCP connection: "claude-code/layout") and MCP session."""
    if le.kind == "turn":
        return "the assistant"
    sid = le.owner.removeprefix("mcp:").partition(":")[0]
    return f"an external agent ({f'{le.label}, ' if le.label else ''}MCP session {sid[:8]})"


def _stopped(owner: str) -> str:
    """The refusal a stopped writer gets: one agent of an MCP session ("mcp:<session>:<agent>") is stopped alone, and
    the session's other agents go on writing."""
    agent = owner.removeprefix("mcp:").partition(":")[2] if owner.startswith("mcp:") else ""
    who = f"this agent ({agent})" if agent else "this session"
    return f"{who} was stopped; its writes to the changeset are refused"


def _held(le: Lease) -> str:
    return f"{holder(le)} is editing this changeset; stop it or wait until it finishes"


PHASES = ("waiting", "committing", "evidence", "merging", "cleanup")


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t))


class Closing:
    """How far the closure of one changeset is: `action` ("apply", "discard", "revert"), by whom, since when, and its
    phase (PHASES): waiting for the changesets' lock, committing the working copy, saving the evidence ("files" and
    "bytes" done of "files_total" and "bytes_total"), merging, cleaning up."""

    def __init__(self, cid: str, title: str, action: str, by: str):
        self.cid, self.title, self.action, self.by = cid, title, action, by
        self.since = time.time()
        self._lock = threading.Lock()
        self._state: dict = {"phase": "waiting"}

    @property
    def waiting(self) -> bool:
        with self._lock:
            return self._state["phase"] == "waiting"

    def phase(self, name: str, **fields) -> None:
        """Enters phase `name`, its counts (files, bytes, ...) as given."""
        with self._lock:
            self._state = {"phase": name, **fields}

    def add(self, **counts: int) -> None:
        """Adds to the phase's counts (from the threads that hash the evidence)."""
        with self._lock:
            for k, v in counts.items():
                self._state[k] = self._state.get(k, 0) + v

    def public(self) -> dict:
        with self._lock:
            return {"action": self.action, "by": self.by, "since": _iso(self.since), **self._state}


class Closings:
    """The changesets this server is closing now (in memory: a restarted server closes none). One closure of a
    changeset at a time: a second asks `begin` in vain while the first runs (the caller refuses it)."""

    def __init__(self):
        self._c: dict[str, Closing] = {}
        self.lock = threading.Lock()

    def begin(self, cid: str, title: str, action: str, by: str) -> Closing | None:
        """Notes the closure of `cid`, waiting; None when it is being closed already."""
        with self.lock:
            if cid in self._c:
                return None
            c = self._c[cid] = Closing(cid, title, action, by)
            return c

    def end(self, c: Closing) -> None:
        with self.lock:
            if self._c.get(c.cid) is c:
                del self._c[c.cid]

    def get(self, cid: str) -> Closing | None:
        with self.lock:
            return self._c.get(cid)

    def public(self, cid: str) -> dict | None:
        """The closure of `cid` as the API gives it (None: not being closed); one waiting names the closure under way
        ("behind": {"id", "title", "action"}; null when what holds the lock is no closure, a check being started)."""
        with self.lock:
            c = self._c.get(cid)
            others = [x for x in self._c.values() if x is not c]
        if c is None:
            return None
        out = c.public()
        if out["phase"] == "waiting":
            ahead = next((x for x in others if not x.waiting), None)
            out["behind"] = {"id": ahead.cid, "title": ahead.title, "action": ahead.action} if ahead else None
        return out

    def ids(self) -> list[str]:
        with self.lock:
            return list(self._c)


FLOOR = "gseq.floor"


def writer() -> str | None:
    """Who is writing in this context (acting's owner), or None."""
    return _writer.get()


def floor(d: Path) -> int:
    """The highest gseq ever given in this working copy, as far as records that were put back are concerned (a failed or
    dry-run batch): numbering continues past it, so a screen following the changeset never sees a number twice. `d` is
    where the changeset keeps it (Changesets._floor_dir: beside a worktree; an older working copy's history/)."""
    try:
        return int((d / FLOOR).read_text().strip() or 0)
    except (OSError, ValueError):
        return 0


def raise_floor(d: Path, gseq: int) -> None:
    if gseq > floor(d):
        d.mkdir(parents=True, exist_ok=True)
        (d / FLOOR).write_text(f"{gseq}\n")


class LogHooks:
    """Attached to the operation logs of a changeset's working copy: the write check and the changeset-wide stamp.
    `base_seq`: each library's last seq when the changeset was opened (its records come after); `floor_dir`: where
    the numbering's floor is (floor)."""

    def __init__(self, leases: Leases, cs: str, history: Path, lock: threading.RLock,
                 base_seq: dict[str, int] | None = None, floor_dir: Path | None = None):
        self.leases, self.cs, self.history, self.lock = leases, cs, history, lock
        self.base_seq = base_seq or {}
        self.floor_dir = floor_dir or history

    def guard(self) -> None:
        self.leases.check(self.cs)

    def stamp(self) -> dict:
        """gseq / opid / turn / changeset for a record about to be appended (called with the changeset's lock held).
        The last gseq of each log is the one nearest its end, after its base_seq."""
        last = floor(self.floor_dir)
        for p in self.history.glob("*.jsonl"):
            below = self.base_seq.get(p.stem, 0)
            for line in oplog.tail_lines(p):       # from the end: only as far back as this changeset's last record
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (r.get("seq") or 0) <= below:
                    break                       # the project's history: not this changeset's
                if (g := r.get("gseq")):
                    last = max(last, g)
                    break
        op = _op.get()
        out = {"gseq": last + 1, "opid": op["opid"] if op else "op-" + uuid.uuid4().hex[:10], "turn": op["turn"] if op else None,
               "changeset": self.cs}
        if op:
            op["committed"].append(out["gseq"])
        return out
