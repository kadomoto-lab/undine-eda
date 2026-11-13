"""Three-way merge and semantic diff of an operation log (history/<lib>.jsonl, see undine.store.oplog), as in
docs/design/merge.md.

The log is append-only JSON lines, one record per operation, numbered by `seq`. Two branches that both recorded
operations share a common part (the longest prefix both files have) and differ after it. A side's own operations are
those after the common part and after the part it shares with the base, less those the other side has as a merge's copy
(below): in a merge, what a side shares with the base the other has too (in the common part, or copied by an earlier
merge), so this changes nothing; in a cherry-pick or a rebase step (base = the picked commit's parent) the parent's
operations are not theirs' own, and are not taken. The merged log:

    ours' records, as they are (torn or invalid lines dropped)
    theirs' own records, renumbered to continue ours' seq: the number theirs gave a record is kept as "merged_seq", and
        an "of" (undo / redo) and a "replay" (a merge record's, below) follow the records they point to; an "of" is
        null, a "replay" entry left out, when the merged log does not have that record (a cherry-pick's undo of an
        operation of the parent)
    {"op": "merge", "actor": "git", "cell": null, "view": null, "effects": [], "reason": "merged N operations ...",
     "replay": [seq, ...]}       ("replay" only when a journal stays pending, below)
        dated like the latest record of the inputs, so the same three files always merge to the same bytes

The merge record is a barrier for the undo / redo stacks (OpLog._stacks): an edit made before the merge is never undone
on top of the other branch's edits.

Unsaved layout edits. Layout edits reach the cell files (.oas) only on a save; until then they are a journal in the log.
A log's journal (journal()) is its layout records after its last boundary, a layout save or a merge record, in order,
whatever their op (undo / redo too); when the boundary is a merge record, the records its "replay" lists come first.
LayoutDB._replay_pending replays exactly these when it opens the library. A merge record is a boundary because the
merged cell files are the merge of both sides' saved files. The log driver cannot see those files, so a journal is
carried over only where it is known which files it belongs to. A side changed the layout when it has a layout record of
its own (a save is one); also when the base has a layout save or a merge the side has no record of (a cherry-pick whose
parent saved: the side's cell files are not the base's then, and git merges them against the base's). For theirs, any
layout record of the base it has no record of counts too: in a revert (base = the reverted commit, theirs = its parent)
those are the reverted commit's edits, which ours' journal may hold, and replaying them would bring back what the
revert takes out. (A base record ours lacks needs no such rule: theirs' journal is carried without it, below.)

    theirs did not change the layout   the merged cell files are ours': ours' journal stays pending
    ours did not                       they are theirs': theirs' journal stays pending, those of its records the merged
                                       log has (a cherry-pick does not bring the parent's, nor an undo / redo of one:
                                       its "of" became null, it has nothing to replay)
    both did, both journals empty      nothing is pending
    both did, a journal is not         conflict "layout-journal": the merged log carries no journal, whichever side
                                       resolving names (both give the same log)

The pending journal is not copied: the merge record lists it in "replay", by seq in the merged log. Replaying it on the
files of the side it was recorded on is exact; on files merged from both sides it would not be (an undo of an edit the
other side's files lack, an edit both saved).

Unsaved edits the merged log does not carry are never dropped silently. The conflict says so ("unsaved layout edits of
ours | theirs | both sides are dropped by this merge (the merged layout is the merge of the saved files): git merge
--abort, save the layouts on each branch, commit, and merge again; or resolve it to accept dropping them"), listing
both journals. Without a conflict, when the merged layout follows one side's files (and journal), the other side's
journal records that are not in that side's layout are a warning ("N unsaved layout edits of ours are dropped: the
merged layout follows theirs' saved files"). An edit is in a side's layout when the side has the record (in the common
part, or one a merge's copy of the other) and it is pending at the end of that log, or saved: a layout save follows it
before any merge record that does not list it in "replay" (one that dropped it: a layout-journal conflict resolved
earlier, so merging back a branch on which one was resolved warns on the other side too). A cherry-pick's parent's
edits are not theirs' (not picked), and are no warning.

A record theirs has after the common part that ours has too, renumbered by a merge, is not taken twice. That happens
whenever a branch is merged back into the one it was updated from: the operations the update copied are the same
operations. Two records are the same operation when one of them is a merge's copy (it has merged_seq) and they are
equal apart from their numbering (seq, merged_seq, and an "of" and a "replay" compared as the records they point to).
Two equal records neither of which a merge made (two branches that made the same edit in the same second) are both
kept: the log keeps both records as history; the layout keeps the shape once (both sides made the same change).

No operation of theirs' own that ours lacks, and no layout record or boundary of the base that theirs lacks: ours'
bytes, its journal with them (theirs changed nothing). Without an operation of its own, but lacking such a record (a
revert): ours' records and a merge record, which ends ours' journal as above. Every operation of ours in theirs, and
none of the base's that ours lacks: theirs' bytes, its journal with them. Lines that are not JSON objects with a seq (a
torn last line, a hand edit) are skipped like OpLog._read_all skips them.

The diff lists the operations each file has after the common part: those only the new one has as added, those only the
old one has (the two files are on different branches) as removed, identified by their seq ("seq:42"). An operation
after the common part that the other file has there as a merge's copy is not a change.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from undine.store.oplog import VERSION
from undine.vcs import merge3

KIND = "log"
JOURNAL = "layout-journal"                           # the conflict id
_NUMBERING = ("seq", "merged_seq", "of", "replay")
_SHOWN = ("seq", "t", "op", "cell", "view", "actor", "reason", "changeset")


def records(data: bytes | None) -> list[tuple[str, dict]]:
    """(line, record) for each valid line of a log file, in file order."""
    out = []
    for line in (data or b"").decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue                                 # a torn last line
        if isinstance(r, dict) and isinstance(r.get("seq"), int):
            out.append((line, r))
    return out


def _common(a: list[dict], b: list[dict]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _seqs(v) -> list[int]:
    """The seq numbers of a "replay" list (anything else in it is ignored)."""
    return [s for s in v if isinstance(s, int) and not isinstance(s, bool)] if isinstance(v, list) else []


def _ident(r: dict, byseq: dict[int, dict], depth: int = 2) -> str:
    """The record without its numbering: equal for the same operation in two logs that numbered it differently. An
    "of" and a "replay" are the records they point to (a replay entry can be an undo: two levels)."""
    def ref(s):
        target = byseq.get(s) if depth and isinstance(s, int) else None
        return _ident(target, byseq, depth - 1) if target is not None and target is not r else s

    x = {k: v for k, v in r.items() if k not in _NUMBERING}
    if "of" in r:
        x["of"] = ref(r["of"])
    if "replay" in r:
        x["replay"] = [ref(s) for s in r["replay"]] if isinstance(r["replay"], list) else r["replay"]
    return json.dumps(x, sort_keys=True, separators=(",", ":"))


def _twins(a: list[dict], a_all: list[dict], b: list[dict], b_all: list[dict]) -> dict[int, dict]:
    """Pairs records of `b` with the same operation in `a`, one a merge's copy of the other (each used once, in order):
    {index in b: record of a}. `a_all` / `b_all` are the whole files, which an "of" is resolved in."""
    aseq, bseq = {r["seq"]: r for r in a_all}, {r["seq"]: r for r in b_all}
    pool: dict[str, list[dict]] = {}
    for r in a:
        pool.setdefault(_ident(r, aseq), []).append(r)
    out = {}
    for i, r in enumerate(b):
        q = pool.get(_ident(r, bseq), [])
        j = next((j for j, x in enumerate(q) if "merged_seq" in r or "merged_seq" in x), None)
        if j is not None:
            out[i] = q.pop(j)
    return out


def _instant(s: str) -> datetime | None:
    """A "t" as OpLog.append writes it ("2026-10-23T01:40:12+0900", which Python 3.10's fromisoformat rejects), else
    any ISO 8601 form fromisoformat reads; None when neither does."""
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _latest(recs: list[dict]) -> str:
    """The latest "t" (ISO 8601 with an offset, as OpLog.append writes it), compared as instants, not as text."""
    best: tuple[datetime, str] | None = None
    for r in recs:
        s = r.get("t")
        if not isinstance(s, str):
            continue
        d = _instant(s)
        if d is None:
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        if best is None or (d, s) > best:
            best = (d, s)
    return best[1] if best else ""


# ---------------------------------------------------------------- unsaved layout edits


def _boundary(r: dict) -> bool:
    """A record LayoutDB._replay_pending starts after: a layout save, or a merge (it wrote the merged cell files)."""
    return r.get("op") == "merge" or (r.get("op") == "save" and r.get("view") == "layout")


def _journal(recs: list[dict]) -> list[dict]:
    """The pending layout records of a log (the module docstring): those its last boundary's "replay" lists, then the
    layout records after it."""
    last = max((i for i, r in enumerate(recs) if _boundary(r)), default=-1)
    out = []
    if last >= 0 and recs[last].get("op") == "merge":
        before = {r["seq"]: r for r in recs[:last] if r.get("view") == "layout"}
        out = [before[s] for s in _seqs(recs[last].get("replay")) if s in before]
    return out + [r for r in recs[last + 1:] if r.get("view") == "layout"]


def journal(data: bytes) -> list[dict]:
    """The pending layout records of a log file: the edits its layout cell files lack, which LayoutDB replays when it
    opens the library (after the last layout save or merge record; a merge record's "replay" list first)."""
    return _journal([r for _, r in records(data)])


def _gone(side: list[dict], base: list[dict]) -> list[dict]:
    """The base's records after the part `side` shares with it, of which `side` has no copy: a save or a merge among
    them means the side's cell files are not the base's (a cherry-pick of a commit whose parent saved); on theirs, they
    are a reverted commit's records (base = that commit)."""
    k = _common(side, base)
    tail = base[k:]
    twins = _twins(side[k:], side, tail, base)
    return [r for i, r in enumerate(tail) if i not in twins]


def _lost(recs: list[dict], seq: int | None) -> bool:
    """A layout edit, `seq` in the log `recs` (None: the log lacks it), is not in that log's layout (the module
    docstring): the log lacks it, or a merge record after it, before any layout save, does not list it in "replay" (it
    was dropped there). Pending at the end, or saved, it is in the layout."""
    i = next((i for i, r in enumerate(recs) if r["seq"] == seq), None) if seq is not None else None
    if i is None:
        return True
    for r in recs[i + 1:]:
        if r.get("op") == "merge":
            if seq not in _seqs(r.get("replay")):
                return True
        elif _boundary(r):
            return False
    return False


def _drop(m: merge3.Merge, side: str, other: str, recs: list[dict]) -> None:
    """Warns that the merged log drops these unsaved layout edits of `side` (the module docstring)."""
    n = len(recs)
    if n:
        m.warn(f"{n} unsaved layout edit{'s' * (n != 1)} of {side} {'are' if n != 1 else 'is'} dropped: the merged "
               f"layout follows {other}' saved files")


def _listing(recs: list[dict]) -> list[str] | None:
    """A journal as a conflict report shows it (None: empty)."""
    out = [f"seq:{r['seq']} {_what(r)}" for r in recs[:10]]
    return (out + [f"... {len(recs) - 10} more"] if len(recs) > 10 else out) or None


def _places(recs: list[dict]) -> str:
    places = sorted({f"{r['cell']} ({r.get('view')})" for r in recs if r.get("cell")})
    return (": " + ", ".join(places[:10]) + (", ..." if len(places) > 10 else "")) if places else ""


def _ops(n: int) -> str:
    return f"{n} operation{'s' * (n != 1)}"


def _dump(r: dict) -> str:
    return json.dumps(r, separators=(",", ":"))      # as OpLog.append writes a line


def merge(base: bytes | None, ours: bytes, theirs: bytes, prefer: dict[str, str] | None = None, **kw) -> merge3.Result:
    """Merges three versions of history/<lib>.jsonl (see the module docstring). The common part is what ours and theirs
    share; `base` (None when there is none) tells which records are a side's own. The one conflict is "layout-journal";
    `prefer` decides it either way with the same result (the unsaved layout edits are dropped). Unsaved layout edits
    dropped without a conflict are a warning."""
    m = merge3.Merge(prefer)
    o, t = records(ours), records(theirs)
    ro, rt, rb = [r for _, r in o], [r for _, r in t], [r for _, r in records(base)]
    c = _common(ro, rt)
    st = max(c, _common(rt, rb))                      # theirs' own records: not the base's (a cherry-pick's parent)
    only_o, only_t = ro[c:], rt[c:]
    twins = _twins(only_o, ro, only_t, rt)            # theirs' copies of operations ours has (a branch merged back)
    new = [r for i, r in enumerate(only_t) if i not in twins and c + i >= st]
    # theirs changed the layout (module docstring): a layout record of its own, or one of the base's it lacks (a revert)
    changed_t = (any(r.get("view") == "layout" for r in new)
                 or any(r.get("view") == "layout" or _boundary(r) for r in _gone(rt, rb)))
    # the records both have, by their seq on the other side (in the common part, or one a merge's copy of the other)
    in_t = {id(r): r["seq"] for r in ro[:c]} | {id(x): only_t[i]["seq"] for i, x in twins.items()}
    in_o = {id(r): r["seq"] for r in rt[:c]} | {id(only_t[i]): x["seq"] for i, x in twins.items()}
    if new and len(twins) == len(only_o) and all(i in twins for i in range(st - c)):   # theirs has every op ours has
        m.combined.append(f"{_ops(len(new))} of the other branch{_places(new)}")
        _drop(m, "ours", "theirs", [r for r in _journal(ro) if _lost(rt, in_t.get(id(r)))])
        return m.result(theirs, KIND)
    if not changed_t:     # the merged cell files are ours': theirs' unsaved edits ours' layout lacks are dropped
        _drop(m, "theirs", "ours", [r for r in _journal(rt) if id(r) in in_o and _lost(ro, in_o[id(r)])])
        if not new:
            return m.result(ours, KIND)
    nxt = max((r["seq"] for r in ro), default=0) + 1
    renum = {only_t[i]["seq"]: r["seq"] for i, r in twins.items()}
    renum.update({r["seq"]: nxt + k for k, r in enumerate(new)})
    pos = {r["seq"]: i for i, r in enumerate(rt)}

    def their_seq(s):
        """Theirs' seq `s` as the merged log numbers it; None for a record of theirs the merged log does not have."""
        return renum[s] if s in renum else (None if pos.get(s, -1) >= c else s)

    lines = [line for line, _ in o]
    merged = {r["seq"]: r for r in ro}
    for r in new:
        r2 = {**r, "seq": renum[r["seq"]], "merged_seq": r["seq"]}
        if isinstance(r.get("of"), int):
            r2["of"] = their_seq(r["of"])
        if "replay" in r:
            r2["replay"] = [s for s in map(their_seq, _seqs(r["replay"])) if s is not None]
        lines.append(_dump(r2))
        merged[r2["seq"]] = r2
    if new:
        m.combined.append(f"{_ops(len(new))} of the other branch{_places(new)}, numbered from {nxt}")

    # the journal (module docstring): ours' as it is, theirs' renumbered
    copied = {id(r) for r in twins.values()}          # ours' records theirs has too: no change of ours
    own_o = [r for r in ro[max(c, _common(ro, rb)):] if id(r) not in copied]
    changed_o = any(r.get("view") == "layout" for r in own_o) or any(_boundary(r) for r in _gone(ro, rb))
    jo, jt = _journal(ro), []
    for r in _journal(rt):        # the records the merged log has: not a cherry-pick parent's, nor an undo / redo of one
        x = merged.get(their_seq(r["seq"]), {})
        if x and not (x.get("op") in ("undo", "redo") and x.get("of") is None):
            jt.append(r)
    replay: list[int] = []
    if not changed_t:
        replay = [r["seq"] for r in jo]
    elif not changed_o:
        replay = [their_seq(r["seq"]) for r in jt]
        if replay:
            m.combined.append(f"layout: ours did not change it, so the merged cell files are theirs', and theirs' "
                              f"{len(replay)} unsaved edit{'s' * (len(replay) != 1)} stay pending{_places(jt)}")
        _drop(m, "ours", "theirs", [r for r in jo if _lost(rt, in_t.get(id(r)))])
    elif jo or jt:
        sides = "both sides" if jo and jt else "ours" if jo else "theirs"
        m.conflict(JOURNAL, f"unsaved layout edits of {sides} are dropped by this merge (the merged layout is the "
                            f"merge of the saved files): git merge --abort, save the layouts on each branch, commit, "
                            f"and merge again; or resolve it to accept dropping them", None, _listing(jo), _listing(jt))
    rec = {"v": VERSION, "seq": nxt + len(new), "t": _latest(ro + rt), "op": "merge", "actor": "git", "cell": None,
           "view": None, "effects": [], "reason": f"merged {_ops(len(new))} of the other branch"}
    lines.append(_dump({**rec, "replay": replay} if replay else rec))
    return m.result(("\n".join(lines) + "\n").encode(), KIND)


def _what(r: dict) -> str:
    where = f" {r['cell']} ({r.get('view')})" if r.get("cell") else (f" ({r['view']})" if r.get("view") else "")
    of = f" of {r['of']}" if r.get("of") is not None else ""
    why = f': "{r["reason"]}"' if r.get("reason") else ""
    return f"{r.get('op')}{of}{where} by {r.get('actor', 'user')}{why}"


def _shown(r: dict) -> dict:
    return {k: r.get(k) for k in _SHOWN} | {k: r[k] for k in ("of", "merged_seq", "replay") if k in r}


def diff(old: bytes | None, new: bytes | None, **kw) -> dict:
    """The operations each version has after the part both share (see the module docstring)."""
    a, b = [r for _, r in records(old)], [r for _, r in records(new)]
    c = _common(a, b)
    only_a, only_b = a[c:], b[c:]
    twins = _twins(only_a, a, only_b, b)
    moved = set(map(id, twins.values()))             # old's operations new has under another number
    changes = [{"do": "del", "id": f"seq:{r['seq']}", "what": _what(r), "before": _shown(r), "after": None}
               for r in only_a if id(r) not in moved]
    changes += [{"do": "add", "id": f"seq:{r['seq']}", "what": _what(r), "before": None, "after": _shown(r)}
                for i, r in enumerate(only_b) if i not in twins]
    return {"kind": KIND, "common": c, "changes": changes}
