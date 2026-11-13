"""Several edits as one step (batch, Changesets.batch) and idempotent writes (request_id): a failed step and a dry run
keep nothing, numbering never repeats for a screen following the changeset, and a retried write never acts twice."""
import json
import shutil
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fastapi.testclient import TestClient  # noqa: E402

from undine.agents import agent_tools as T, client as cl, mcp  # noqa: E402
from undine.server import idempotency, live
from undine.server.api import create_app  # noqa: E402
import test_changeset as tc  # noqa: E402
from test_mcp_local import transport  # noqa: E402


def setup():
    root, srv, cs = tc.setup()
    srv.mcp_enabled = True
    ctx = T.ToolCtx(srv, "p", actor="agent:test", owner="mcp:s1", owner_kind="mcp")
    text, err = T.call(ctx, "open_changeset", {"title": "Raise R1", "reason": "less current"})
    assert not err, text
    return root, srv, cs, ctx


def head(srv, cs, ctx):
    return cs._head(cs._load("p", ctx.changeset))


def call(ctx, name, **args):
    text, err = T.call(ctx, name, args)
    return json.loads(text) if not err else text, err


def test_a_step_is_one_operation_and_reports_what_it_did():
    root, srv, cs, ctx = setup()
    out, err = call(ctx, "batch", reason="less current", ops=[
        {"tool": "set_instance_params", "args": {"lib": "l", "cell": "top", "instance": "R1", "params": {"value": "10k"}}},
        {"tool": "get_netlist", "args": {"lib": "l", "cell": "top"}}])
    assert not err, out
    assert out["applied"] == 2 and out["results"][1]["result"]["instances"]["R1"]["params"]["value"] == "10k"
    assert [(list(c["cells"]), c["view"], c["reason"]) for c in out["changes"]] == [(["l/top"], "schematic", "less current")]
    assert tc.value(srv, f"p~{ctx.changeset}") == "10k" and tc.value(srv) == "1k"
    ops = cs.timeline("p", ctx.changeset)["ops"]
    assert len([o for o in ops if o["kind"] == "edit"]) == 1                 # one operation, one restore point
    shutil.rmtree(root)


def test_a_failed_step_keeps_nothing_and_numbering_goes_on():
    root, srv, cs, ctx = setup()
    call(ctx, "set_instance_params", lib="l", cell="top", instance="R1", params={"value": "10k"}, reason="first")
    before = head(srv, cs, ctx)
    out, err = call(ctx, "batch", reason="x", ops=[
        {"tool": "set_instance_params", "args": {"lib": "l", "cell": "top", "instance": "R1", "params": {"value": "22k"}}},
        {"tool": "set_instance_params", "args": {"lib": "l", "cell": "top", "instance": "R9", "params": {"value": "1"}}}])
    assert err and "ops[1] set_instance_params failed" in out and "Nothing was kept" in out
    assert tc.value(srv, f"p~{ctx.changeset}") == "10k" and head(srv, cs, ctx) == before
    # the floor is beside the changeset's worktree, never in it (a commit of the working copy would carry it into the
    # project): no longer in its history/
    floor_dir = root / ".changesets" / "p" / ctx.changeset
    assert not (srv.project(f"p~{ctx.changeset}").path / "history" / live.FLOOR).exists()
    assert live.floor(floor_dir) > before                                    # the put-back numbers are not given again
    call(ctx, "set_instance_params", lib="l", cell="top", instance="R1", params={"value": "33k"}, reason="after")
    assert head(srv, cs, ctx) > live.floor(floor_dir)
    out, err = call(ctx, "batch", ops=[{"tool": "run_checks", "args": {}}])
    assert err and "cannot be part of batch" in out
    out, err = call(ctx, "batch", ops=[])
    assert err
    shutil.rmtree(root)


def test_a_dry_run_shows_the_result_and_keeps_nothing():
    root, srv, cs, ctx = setup()
    before = head(srv, cs, ctx)
    out, err = call(ctx, "batch", dry_run=True, reason="try", ops=[
        {"tool": "set_instance_params", "args": {"lib": "l", "cell": "top", "instance": "R1", "params": {"value": "47k"}}},
        {"tool": "get_netlist", "args": {"lib": "l", "cell": "top"}}])
    assert not err, out
    assert out["dry_run"] and out["results"][1]["result"]["instances"]["R1"]["params"]["value"] == "47k"
    assert out["changes"] and tc.value(srv, f"p~{ctx.changeset}") == "1k" and head(srv, cs, ctx) == before
    assert [o for o in cs.timeline("p", ctx.changeset)["ops"] if o["kind"] == "edit"] == []     # nothing in its history either
    shutil.rmtree(root)


def test_a_screen_following_the_changeset_does_not_see_a_running_step():
    root, srv, cs, ctx = setup()
    c = TestClient(create_app(srv, None))
    url = f"/api/p/p/changesets/{ctx.changeset}/activity?after=0"
    seen = {}

    def run():
        T.BY_NAME["set_instance_params"]["fn"](ctx, {"lib": "l", "cell": "top", "instance": "R1", "params": {"value": "5k"}, "reason": "x"})
        seen["during"] = c.get(url).json()
        return []
    with live.acting(ctx.owner):
        cs.batch("p", ctx.changeset, run, dry_run=True)
    assert seen["during"]["records"] == [] and seen["during"]["last"] == 0
    assert c.get(url).json()["records"] == []
    shutil.rmtree(root)


def test_a_retried_write_acts_once():
    root, srv, cs, ctx = setup()
    auth = {"authorization": f"Bearer {mcp.ensure_token(srv.store.root)}"}
    sessions = mcp.Sessions()
    _, init, hdr = mcp.handle(srv, sessions, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, auth)
    h = {**auth, "mcp-session-id": hdr["mcp-session-id"]}

    def tool(i, name, **args):
        return mcp.handle(srv, sessions, {"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                          "params": {"name": name, "arguments": {"project": "p", **args}}}, h)[1]["result"]
    schema = {t["name"]: t for t in mcp.tool_list()}
    assert "request_id" in schema["open_changeset"]["inputSchema"]["properties"]
    assert "request_id" not in schema["get_netlist"]["inputSchema"]["properties"]
    first = tool(2, "open_changeset", title="A", reason="r", request_id="req-1")
    again = tool(3, "open_changeset", title="A", reason="r", request_id="req-1")
    assert again["content"] == first["content"] and again["_meta"] == {"undine/replayed": True} and "_meta" not in first
    assert len(cs.list("p")) == 2                                             # setup's and this one: not three
    other = tool(4, "open_changeset", title="B", reason="r", request_id="req-1")
    assert other["isError"] and "already used for another call" in other["content"][0]["text"]
    sessions2 = mcp.Sessions()                                                # as after a server restart
    _, _, hdr2 = mcp.handle(srv, sessions2, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, auth)
    r = mcp.handle(srv, sessions2, {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "open_changeset",
                   "arguments": {"project": "p", "title": "A", "reason": "r", "request_id": "req-1"}}}, {**auth, "mcp-session-id": hdr2["mcp-session-id"]})
    assert r[1]["result"]["_meta"] == {"undine/replayed": True} and len(cs.list("p")) == 2
    shutil.rmtree(root)


def test_a_failed_write_is_not_remembered_and_a_retry_waits_for_the_first():
    root, srv, cs, ctx = setup()
    d = srv.project("p").path
    assert idempotency.once(d, "r1", "t", {}, lambda: ("refused", True)) == ("refused", True, False)
    assert idempotency.once(d, "r1", "t", {}, lambda: ("done", False)) == ("done", False, False)   # ran again
    assert idempotency.once(d, "r1", "t", {}, lambda: ("again", False)) == ("done", False, True)
    runs, out = [], []

    def slow():
        runs.append(1)
        time.sleep(0.3)
        return "slow done", False
    th = threading.Thread(target=lambda: out.append(idempotency.once(d, "r2", "t", {}, slow)))
    th.start()
    time.sleep(0.05)
    out.append(idempotency.once(d, "r2", "t", {}, slow))                     # arrives while the first still runs
    th.join()
    assert len(runs) == 1 and sorted(o[2] for o in out) == [False, True]
    shutil.rmtree(root)


def test_the_client_resends_a_lost_answer_without_acting_twice(monkeypatch):
    root, srv, cs, ctx = setup()
    mcp.ensure_token(srv.store.root)
    http = transport(TestClient(create_app(srv, None)))
    lost = {"n": 0}

    def flaky(method, path, body=None, headers=None):
        out = http(method, path, body, headers)
        if body and body.get("method") == "tools/call" and lost["n"] == 0:
            lost["n"] += 1
            raise OSError("connection reset")                                 # it was done; the answer never arrived
        return out
    monkeypatch.setattr(cl, "RETRIES", (0.0, 0.0))
    u = cl.Undine("p", "http://undine.test", http=flaky)
    res = u.open_changeset(title="Once", reason="r")
    assert u.last_replayed and lost["n"] == 1
    assert [c["id"] for c in cs.list("p") if c["title"] == "Once"] == [res["id"]]
    shutil.rmtree(root)
