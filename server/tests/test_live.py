"""Live changesets, PR 1 (docs/design/live-changesets.md): one order across libraries, operation ids, one writer per
changeset on every write path, assistant turns as jobs (progress, stop, interruption, a retried request)."""
import json
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import klayout.db as kdb  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from undine.agents import agent_tools as T  # noqa: E402
from undine.layouts import layoutstore  # noqa: E402
from undine.server import live
from undine.agents import llm
from undine.server.api import create_app  # noqa: E402
from undine.store.changeset import ChangesetError  # noqa: E402
import test_changeset as tc  # noqa: E402

SCRIPT: list[dict] = []
GATE = {"hold": None, "from": 0, "n": 0}   # from the `from`-th call on, the fake model waits for `hold` (to stop a turn mid-way)


class Fake(BaseHTTPRequestHandler):
    def do_POST(self):
        json.loads(self.rfile.read(int(self.headers["content-length"])))
        GATE["n"] += 1
        if GATE["hold"] is not None and GATE["n"] >= GATE["from"]:
            GATE["hold"].wait(10)
        msg = SCRIPT.pop(0)
        out = json.dumps({"choices": [{"message": msg, "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}],
                          "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def call(name, **args):
    return {"id": f"c-{name}-{len(SCRIPT)}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def fake_model(srv, root):
    httpd = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    srv.gateway = llm.Gateway(root, "server")
    srv.gateway.switch("server", "m", f"http://127.0.0.1:{httpd.server_port}/v1")


BODY = {"context": {"lib": "l", "cell": "top", "view": "schematic", "selection": [], "overview": True}, "mode": "propose"}


def until(c, path, states=("done", "failed", "stopped", "interrupted"), timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = c.get(path).json()
        if r["state"] in states:
            return r
        time.sleep(0.05)
    raise AssertionError(f"turn still {r['state']}")


def test_records_get_one_order_across_libraries_and_operation_ids():
    root, srv, cs = tc.setup()
    lib2 = srv.project("p").create_library("m")
    ly = kdb.Layout()
    ly.create_cell("blk")
    layoutstore.write(lib2.path, ly, {})
    meta = cs.create("p", "Two libraries", "r", "user")
    P = meta["preview"]
    with live.operation() as first:
        ed = srv.editor(P, "l", "top", "schematic")
        ed.set_props("i", "R1", {"value": "2k"})
        db = srv.db(P, "m")
        with db.recording("create", {}, "user", None, meta["id"]):
            db._commit("blk", [("ins", db.layout.layer(68, 20), kdb.Box(0, 0, 10, 10))])
    srv.editor(P, "l", "top", "schematic").set_props("i", "R1", {"value": "3k"})
    recs = sorted([r for lib in ("l", "m") for r in srv.oplog(P, lib).read()], key=lambda r: r["gseq"])
    assert [r["gseq"] for r in recs] == [1, 2, 3] and first == [1, 2]
    assert recs[0]["opid"] == recs[1]["opid"] != recs[2]["opid"] and all(r["turn"] is None for r in recs)
    assert all("gseq" not in r for r in srv.oplog("p", "l").read())            # the real project's log is unchanged
    shutil.rmtree(root)


def test_one_writer_per_changeset_on_every_path():
    root, srv, cs = tc.setup()
    c = TestClient(create_app(srv, None))
    meta = cs.create("p", "Locked", "r", "user")
    P, cid = meta["preview"], meta["id"]
    srv.leases.acquire(cid, "turn:t-x", "turn")                                  # the assistant is working
    edit = {"op": "set_props", "kind": "i", "id": "R1", "props": {"value": "9k"}}
    r = c.post(f"/api/p/{P}/l/l/c/top/schematic/edit", json=edit)
    assert r.status_code == 409 and "assistant is editing" in r.json()["detail"]
    assert c.post(f"/api/p/{P}/l/l/c/top/schematic/edit", json={"op": "undo"}).status_code == 409
    assert c.post(f"/api/p/{P}/l/l/undo").status_code == 409                       # layout undo
    try:
        cs.apply("p", cid, "user")
        raise AssertionError("apply must be refused")
    except ChangesetError as e:
        assert "editing" in str(e)
    assert c.get(f"/api/p/p/changesets/{cid}").json()["writer"]["kind"] == "turn"
    # an MCP session cannot write either, and it does not take the lease over
    mcp = T.ToolCtx(srv, "p", actor="agent:x", changeset=cid, owner="mcp:s1", owner_kind="mcp")
    res, err = T.call(mcp, "set_instance_params", {"lib": "l", "cell": "top", "instance": "R1", "params": {"value": "1"}, "reason": "x"})
    assert err and "assistant is editing" in res
    # the owner can; afterwards everybody can again
    with live.acting("turn:t-x"):
        srv.editor(P, "l", "top", "schematic").set_props("i", "R1", {"value": "7k"})
    srv.leases.release(cid, "turn:t-x")
    assert c.post(f"/api/p/{P}/l/l/c/top/schematic/edit", json=edit).status_code == 200
    assert c.get(f"/api/p/p/changesets/{cid}").json()["writer"] is None
    shutil.rmtree(root)


def test_an_mcp_lease_lapses_and_a_stopped_session_stays_out():
    root, srv, cs = tc.setup()
    c = TestClient(create_app(srv, None))
    meta = cs.create("p", "External", "r", "user")
    cid = meta["id"]
    mcp = T.ToolCtx(srv, "p", actor="agent:x", changeset=cid, owner="mcp:s1", owner_kind="mcp")
    args = {"lib": "l", "cell": "top", "instance": "R1", "params": {"value": "4k"}, "reason": "x"}
    assert not T.call(mcp, "set_instance_params", args)[1]
    assert srv.leases.get(cid).kind == "mcp"
    srv.leases.get(cid).expires = time.time() - 1                                # 120 s without a write
    assert srv.leases.get(cid) is None
    assert not T.call(mcp, "set_instance_params", args)[1]                       # it may take it again
    assert c.post(f"/api/p/p/changesets/{cid}/writer/stop").json()["stopped"]["kind"] == "mcp"
    res, err = T.call(mcp, "set_instance_params", args)
    assert err and "stopped" in res
    edit = {"op": "set_props", "kind": "i", "id": "R1", "props": {"value": "9k"}}
    assert c.post(f"/api/p/{meta['preview']}/l/l/c/top/schematic/edit", json=edit).status_code == 200   # people can again
    shutil.rmtree(root)


def test_a_turn_runs_in_the_background_with_progress():
    root, srv, cs = tc.setup()
    fake_model(srv, root)
    c = TestClient(create_app(srv, None))
    SCRIPT[:] = [
        {"content": "Opening a proposal.", "tool_calls": [call("open_changeset", title="Raise R1", reason="r"),
                                                         call("set_instance_params", lib="l", cell="top", instance="R1", params={"value": "10k"}, reason="less current")]},
        {"content": "Done: R1 is 10k."},
    ]
    r = c.post("/api/p/p/chat", json={**BODY, "message": "raise R1", "client_id": "req-1"}).json()
    assert r["state"] == "running" and r["turn"].startswith("t-")
    t = until(c, f"/api/p/p/turns/{r['turn']}")
    assert t["state"] == "done" and "10k" in t["reply"] and t["changeset"]
    kinds = [(e["type"], e.get("name"), e.get("state")) for e in t["events"]]
    assert kinds == [("text", None, None), ("tool", "open_changeset", "running"), ("changeset", None, None),
                     ("tool", "open_changeset", "ok"), ("tool", "set_instance_params", "running"),
                     ("tool", "set_instance_params", "ok"), ("text", None, None), ("state", None, "done")]
    edit_done = [e for e in t["events"] if e.get("name") == "set_instance_params" and e["state"] == "ok"][0]
    assert edit_done["gseq"] == [1]
    rec = srv.oplog(f"p~{t['changeset']}", "l").read()[-1]
    assert rec["turn"] == r["turn"] and rec["gseq"] == 1
    assert c.get(f"/api/p/p/turns/{r['turn']}?after=6").json()["events"][0]["i"] == 6                 # only what is new
    conv = c.get(f"/api/p/p/chats/{r['conversation']}").json()
    assert [m["role"] for m in conv["messages"]] == ["user", "assistant"] and conv["messages"][1]["state"] == "done"
    assert srv.leases.get(t["changeset"]) is None                                                      # given back
    git = c.get(f"/api/p/p/changesets/{t['changeset']}").json()["git"]                                # and committed
    assert git["commits"] == 1 and git["tip"] and git["tip"] != git["base"]
    # the same request again (a retried POST) is the same turn, not a second one
    again = c.post("/api/p/p/chat", json={**BODY, "message": "raise R1", "client_id": "req-1", "conversation": r["conversation"]}).json()
    assert again["turn"] == r["turn"]
    shutil.rmtree(root)


def test_stopping_a_turn_keeps_what_it_committed():
    root, srv, cs = tc.setup()
    fake_model(srv, root)
    c = TestClient(create_app(srv, None))
    SCRIPT[:] = [
        {"content": None, "tool_calls": [call("open_changeset", title="T", reason="r"),
                                         call("set_instance_params", lib="l", cell="top", instance="R1", params={"value": "10k"}, reason="a")]},
        {"content": None, "tool_calls": [call("set_instance_params", lib="l", cell="top", instance="R1", params={"value": "20k"}, reason="b")]},
    ]
    GATE.update(hold=threading.Event(), n=0, **{"from": 2})  # the first answer goes through, the second waits
    r = c.post("/api/p/p/chat", json={**BODY, "message": "go"}).json()
    tid = r["turn"]
    t0 = time.time()
    while not any(e.get("state") == "ok" and e.get("name") == "set_instance_params" for e in c.get(f"/api/p/p/turns/{tid}").json()["events"]):
        assert time.time() - t0 < 10
        time.sleep(0.02)
    cid = c.get(f"/api/p/p/turns/{tid}").json()["changeset"]
    assert c.get(f"/api/p/p/changesets/{cid}").json()["writer"]["kind"] == "turn"
    s = c.post(f"/api/p/p/turns/{tid}/stop").json()
    assert s["state"] == "stopping"
    GATE["hold"].set()                                       # the second answer arrives; its tool call is not run
    t = until(c, f"/api/p/p/turns/{tid}")
    GATE["hold"] = None
    assert t["state"] == "stopped" and "stopped by the user" in t["reply"]
    assert srv.editor(f"p~{cid}", "l", "top", "schematic").instance("R1")["props"]["value"] == "10k"   # kept, not 20k
    assert c.get(f"/api/p/p/changesets/{cid}").json()["writer"] is None
    shutil.rmtree(root)


def test_a_turn_of_a_former_server_is_interrupted_and_the_conversation_says_so():
    root, srv, cs = tc.setup()
    t = srv.turns.create(srv.project("p").path, "chat-x", None, None)
    with srv.turns.lock:                                     # as if the process that ran it had ended
        srv.turns.live.clear()
    c = TestClient(create_app(srv, None))
    r = c.get(f"/api/p/p/turns/{t['id']}").json()
    assert r["state"] == "interrupted" and "kept" in r["error"]
    shutil.rmtree(root)


def test_screens_can_see_who_is_editing_and_follow_what_they_commit():
    root, srv, cs = tc.setup()
    c = TestClient(create_app(srv, None))
    assert c.get("/api/p/p/live").json() == []
    mcp = T.ToolCtx(srv, "p", actor="agent:claude-desktop", owner="mcp:s1", owner_kind="mcp")
    T.call(mcp, "open_changeset", {"title": "New OTA", "reason": "r"})
    cid = mcp.changeset
    start = c.get(f"/api/p/p/changesets/{cid}/activity").json()          # after=-1: only where to follow from
    assert start["records"] == [] and start["writer"]["label"] == "claude-desktop"
    live = c.get("/api/p/p/live").json()
    assert [(x["id"], x["title"], x["writer"]["kind"], x["writer"]["label"]) for x in live] == [(cid, "New OTA", "mcp", "claude-desktop")]
    T.call(mcp, "create_cell", {"lib": "l", "cell": "ota", "reason": "new"})
    T.call(mcp, "set_instance_params", {"lib": "l", "cell": "top", "instance": "R1", "params": {"value": "2k"}, "reason": "x"})
    act = c.get(f"/api/p/p/changesets/{cid}/activity?after={start['last']}").json()
    assert [(r["lib"], r["cell"], r["view"]) for r in act["records"]] == [("l", "ota", "schematic"), ("l", "top", "schematic")]
    assert act["last"] == act["records"][-1]["gseq"] and act["records"][0]["actor"] == "agent:claude-desktop"
    assert c.get(f"/api/p/p/changesets/{cid}/activity?after={act['last']}").json()["records"] == []
    srv.leases.release_owner("mcp:s1")
    assert c.get("/api/p/p/live").json() == [] and c.get(f"/api/p/p/changesets/{cid}/activity").json()["writer"] is None
    shutil.rmtree(root)
