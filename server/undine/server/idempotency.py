"""Idempotent tool calls: a client gives a write a `request_id`; calling again with the same id returns the first result
instead of acting twice. That makes a retry safe when the client cannot tell whether a call went through (a timeout, a
dropped connection, a client restart): the outcome is never "unknown".

Kept per project in changesets/requests.jsonl (the last KEEP calls: id, tool, a fingerprint of the arguments), so a
retry after a server restart is still recognised. Each result is in a file of its own in changesets/requests/: the
list, searched on every write, stays small however long the results are (a client may take them whole, a batch of
netlists), and a result longer than KEPT_CHARS is kept shortened (results.py), saying so. Only successes are kept: a
call that failed changed nothing (every write tool is all or nothing; batch takes a failed step back), so retrying it
runs it again. A retry that arrives while the first call still runs waits for it. The same id with other arguments
is refused rather than guessed at. Ids are per agent (an MCP call's `agent`, kept by name, so a retry after a
reconnect is still one): two agents on one connection that choose the same id make two calls, never one the other's
replay.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

KEEP = 1000
KEPT_CHARS = 100_000      # of a result kept for a retry; a longer one is kept shortened, valid JSON
WAIT_S = 900.0

_lock = threading.Lock()
_running: dict[tuple[str, str, str], threading.Event] = {}


def _fingerprint(tool: str, args: dict) -> str:
    return hashlib.sha256(json.dumps({"tool": tool, "args": args}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


def _file(project_dir: Path) -> Path:
    d = project_dir / "changesets"
    d.mkdir(exist_ok=True)
    return d / "requests.jsonl"


def _find(f: Path, rid: str, agent: str = "") -> dict | None:
    if not f.is_file():
        return None
    for line in reversed(f.read_text().splitlines()):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("id") == rid and rec.get("agent", "") == agent:
            return rec
    return None


def _store(f: Path, rec: dict, text: str) -> None:
    """Keeps a result: its text (_shortened) in its own file, named by a hash of the id, and the record in the list.
    The oldest beyond KEEP go, with their files."""
    d = f.with_name("requests")
    d.mkdir(exist_ok=True)
    name = f"{rec['agent']}:{rec['id']}" if rec.get("agent") else rec["id"]       # ':' is in no agent's name
    rec["file"] = hashlib.sha256(name.encode()).hexdigest()[:32] + ".txt"
    tmp = d / (rec["file"] + ".tmp")
    tmp.write_text(text)
    tmp.replace(d / rec["file"])
    lines = f.read_text().splitlines() if f.is_file() else []
    lines.append(json.dumps(rec, ensure_ascii=False))
    if len(lines) > KEEP * 1.2:
        lines = lines[-KEEP:]
        tmp = f.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(lines) + "\n")
        tmp.replace(f)
        kept = {x.get("file") for x in map(_record, lines) if x}
        for p in d.glob("*.txt"):
            if p.name not in kept:
                p.unlink(missing_ok=True)
    else:
        with f.open("a") as fh:
            fh.write(lines[-1] + "\n")


def _record(line: str) -> dict | None:
    try:
        return json.loads(line)
    except ValueError:
        return None


def _shortened(text: str) -> str:
    if len(text) <= KEPT_CHARS:
        return text
    from undine.agents import results           # standard library only
    try:
        v = json.loads(text)
    except ValueError:
        v = text
    return results.text(v, KEPT_CHARS, "the first call's result, kept shortened for a retry: read the design again for "
                                       "the rest")


def _kept(f: Path, rec: dict) -> str:
    """The first call's result: in its file (or, as kept before, in the record)."""
    if "text" in rec:
        return rec["text"]
    try:
        return (f.with_name("requests") / rec["file"]).read_text()
    except (OSError, KeyError):
        return json.dumps({"replayed": True, "note": "this call was done before, and its result is no longer kept: "
                                                     "read the design to see what it did"})


def once(project_dir: Path, rid: str, tool: str, args: dict, run, agent: str | None = None) -> tuple[str, bool, bool]:
    """run() -> (text, is_error), at most once per (project, agent, request id); `agent` the caller's name (MCP's
    `agent`), None for a caller without one. Returns (text, is_error, replayed)."""
    if not isinstance(rid, str) or not 1 <= len(rid) <= 200:
        return "request_id must be a string of 1 to 200 characters", True, False
    agent = agent or ""
    f, fp, key = _file(project_dir), _fingerprint(tool, args), (str(project_dir), agent, rid)
    while True:
        with _lock:
            done = _find(f, rid, agent)
            if done is None and key not in _running:
                ev = _running[key] = threading.Event()
                break
            wait = _running.get(key)
        if done is not None:
            if done.get("fp") != fp:
                return (f"request_id {rid} was already used for another call ({done.get('tool')}); use a new id for a new call",
                        True, False)
            return _kept(f, done), False, True
        if wait is not None and not wait.wait(WAIT_S):
            return f"request {rid} is still running; try again later with the same id", True, False
    try:
        text, err = run()
        if not err:
            kept = _shortened(text)                  # before the lock: every write of the server waits for it
            with _lock:
                _store(f, {"id": rid, **({"agent": agent} if agent else {}), "tool": tool, "fp": fp, "chars": len(text),
                           "t": time.strftime("%Y-%m-%dT%H:%M:%S%z")}, kept)
        return text, err, False
    finally:
        with _lock:
            _running.pop(key, None)
        ev.set()
