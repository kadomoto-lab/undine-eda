"""How long an agent's layout assembly takes inside a changeset (not part of the suite: run it by hand).

    python server/tests/bench_layout_assembly.py [--no-pdk]

A CDAC is thousands of shapes and instances: a batch of 2000 add_box, the same as 2000 separate calls, a place_cell
array of 32 x 64 unit cells (one instance), and for comparison 2000 single place_cell in one batch. With sky130A
installed it runs on a project of that PDK (its few hundred layer views are what made add_box slow), else without.
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("GIT_CONFIG_GLOBAL", os.devnull)       # as conftest.py: no user configuration in the commits
os.environ.setdefault("GIT_CONFIG_NOSYSTEM", "1")

from undine.agents import agent_tools as T  # noqa: E402
from undine.pdks.pdk import default_pdk_roots, discover  # noqa: E402
from undine.server.api import Server  # noqa: E402

N = 2000


def timed(label: str, fn) -> float:
    t0 = time.perf_counter()
    fn()
    dt = time.perf_counter() - t0
    print(f"{label:<52} {dt:7.2f} s")
    return dt


def ok(ctx, tool, args):
    res, err = T.call(ctx, tool, args)
    assert not err, res[:400]
    return json.loads(res)


def main(use_pdk: bool = True) -> dict:
    pdks = discover(default_pdk_roots()) if use_pdk else {}
    sky = next((p for p in pdks.values() if p.name == "sky130A"), None)
    root = Path(tempfile.mkdtemp())
    try:
        srv = Server(root, {sky.id: sky} if sky else {}, [root])
        srv.store.create("p", sky.id if sky else None).create_library("l")
        print(f"project with {sky.id if sky else 'no PDK'}")
        ctx = T.ToolCtx(srv, "p", actor="agent:bench", owner="mcp:bench", owner_kind="mcp")
        ok(ctx, "open_changeset", {"title": "bench", "reason": "r"})
        for cell in ("cdac", "calls", "array", "singles", "unit"):
            ok(ctx, "create_cell", {"lib": "l", "cell": cell, "views": ["layout"], "reason": "r"})
        db = srv.db(ctx.work, "l")
        if sky is None:                                     # no PDK: the library's layer views are its layers
            db.layout.layer(68, 20)
            db.lv.add_missing_layers()
        layer = "met1" if sky else "68/20"
        ok(ctx, "add_box", {"lib": "l", "cell": "unit", "layer": layer, "box": [0, 0, 1.5, 1.5], "reason": "r"})
        box = lambda cell, i: {"lib": "l", "cell": cell, "layer": layer, "box": [i % 64 * 2, i // 64 * 2, i % 64 * 2 + 1, i // 64 * 2 + 1]}
        out = {}
        out["batch_add_box"] = timed(f"batch of {N} add_box", lambda: ok(ctx, "batch", {
            "reason": "unit plates", "ops": [{"tool": "add_box", "args": box("cdac", i)} for i in range(N)]}))
        out["calls_add_box"] = timed(f"{N} add_box calls", lambda: [ok(ctx, "add_box", {**box("calls", i), "reason": "r"})
                                                                   for i in range(N)])
        out["array_32x64"] = timed("place_cell array 32 x 64", lambda: ok(ctx, "place_cell", {
            "lib": "l", "cell": "array", "child": "unit", "x": 0, "y": 0, "columns": 32, "rows": 64, "pitch_x": 2,
            "pitch_y": 2, "name": "C1", "reason": "unit capacitors"}))
        out["batch_place_cell"] = timed(f"batch of {N} single place_cell", lambda: ok(ctx, "batch", {
            "reason": "unit capacitors one by one", "ops": [{"tool": "place_cell", "args": {
                "lib": "l", "cell": "singles", "child": "unit", "x": i % 64 * 2, "y": i // 64 * 2}} for i in range(N)]}))
        out["review"] = timed("the changeset's review (get)", lambda: T.Changesets(srv).get("p", ctx.changeset))
        info = ok(ctx, "get_layout_info", {"lib": "l", "cell": "array"})
        assert info["instances"][0]["array"]["columns"] == 32
        return out
    finally:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main("--no-pdk" not in sys.argv)
