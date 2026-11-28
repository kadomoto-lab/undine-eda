"""Replaying an operation log with many restores: the undo / redo stacks and the operations in effect come out as the
plain recursive replay gives them, in time linear in the restores (each applied changeset leaves one in the log)."""
from __future__ import annotations

import random
import time

from undine.store import oplog
from undine.store.oplog import OpLog, _before


def _ref_net(recs):
    net, byseq = [], {r["seq"]: r for r in recs}
    for i, r in enumerate(recs):
        if r["op"] == "restore":
            net = _ref_net(_before(recs, i))
        elif r["op"] == "undo":
            net = [x for x in net if x["seq"] != r["of"]]
        elif r["op"] == "redo":
            if (x := byseq.get(r["of"])):
                net.append(x)
        elif r.get("effects"):
            net.append(r)
    return net


def _ref_stacks(recs, cell, view):
    done, redo = [], []
    for i, r in enumerate(recs):
        if r["op"] == "restore" and (r.get("view") is None or r.get("view") == view):
            done, redo = _ref_stacks(_before(recs, i), cell, view)
            redo = []
            continue
        if r["op"] == "merge":
            done, redo = [], []
            continue
        if (cell is not None and r.get("cell") != cell) or r.get("view") != view:
            continue
        if r["op"] == "undo":
            x = next((d for d in done if d["seq"] == r["of"]), None)
            if x:
                done.remove(x)
                redo.append(x)
        elif r["op"] == "redo":
            x = next((d for d in redo if d["seq"] == r["of"]), None)
            if x:
                redo.remove(x)
                done.append(x)
        elif r.get("effects"):
            done.append(r)
            redo.clear()
    return done, redo


def _log(rng: random.Random, n: int, restores: float = 0.08) -> list[dict]:
    recs: list[dict] = []
    for seq in range(1, n + 1):
        cs = rng.choice([None, "cs-a", "cs-b"])
        base = {"seq": seq, "gseq": seq, "changeset": cs, "cell": rng.choice(["a", "b"]),
                "view": rng.choice(["layout", "schematic"])}
        k = rng.random()
        if k < restores and seq > 1:
            recs.append({**base, "op": "restore", "to": rng.randrange(seq),
                         "view": rng.choice([None, None, "layout", "schematic"])})
        elif k < restores + 0.02:
            recs.append({**base, "op": "merge"})
        elif k < 0.3 and seq > 1:
            recs.append({**base, "op": rng.choice(["undo", "redo"]), "of": rng.randrange(1, seq)})
        else:
            recs.append({**base, "op": "edit", "effects": [{"k": "s"}]})
    return recs


def _stacks(recs, cell, view):
    return OpLog._stacks(object.__new__(OpLog), recs, cell, view)


def test_the_replay_agrees_with_the_recursive_one_on_random_logs():
    rng = random.Random(1234)
    for _ in range(300):
        recs = _log(rng, rng.randrange(1, 60))
        assert oplog._net(recs) == _ref_net(recs)
        for cell in (None, "a", "b"):
            for view in ("layout", "schematic"):
                assert _stacks(recs, cell, view) == _ref_stacks(recs, cell, view)


def test_a_log_with_hundreds_of_project_restores_replays_quickly():
    # an edit, then a project-wide restore to just before the newest edit, again and again: the recursive replay
    # visits every earlier restore again from each one (2^n)
    recs: list[dict] = []
    seq = 0
    for _ in range(400):
        seq += 1
        recs.append({"seq": seq, "gseq": seq, "op": "edit", "cell": "a", "view": "layout", "effects": [{"k": "s"}]})
        seq += 1
        recs.append({"seq": seq, "gseq": seq, "op": "restore", "to": seq - 1, "view": None, "changeset": None})
    t = time.monotonic()
    done, redo = _stacks(recs, "a", "layout")
    net = oplog._net(recs)
    assert time.monotonic() - t < 5
    assert [r["seq"] for r in done] == [r["seq"] for r in net] == list(range(1, 2 * 400, 2))
    assert redo == []
