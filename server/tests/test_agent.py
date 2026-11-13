"""Agent tools, the propose loop (fake OpenAI-compatible model) and the MCP front end."""
import json
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine.agents import agent, llm, mcp  # noqa: E402
from undine.agents import agent_tools as T  # noqa: E402
import test_changeset as tc  # noqa: E402

SCRIPT: list[dict] = []
SEEN: list[dict] = []


class Fake(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        SEEN.append(body)
        msg = SCRIPT.pop(0)
        out = json.dumps({"choices": [{"message": msg, "finish_reason": "tool_calls" if msg.get("tool_calls") else "stop"}],
                          "usage": {"prompt_tokens": 10, "completion_tokens": 5}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def call(name, **args):
    return {"id": f"c-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_writes_only_in_a_changeset_and_never_apply():
    root, srv, cs = tc.setup()
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    assert "apply" not in T.BY_NAME and not any("apply" in n for n in T.BY_NAME)
    res, err = T.call(ctx, "set_instance_params", {"lib": "l", "cell": "top", "instance": "R1", "params": {"value": "9k"}, "reason": "x"})
    assert err and "open_changeset" in res
    T.call(ctx, "open_changeset", {"title": "t", "reason": "r"})
    res, err = T.call(ctx, "set_instance_params", {"lib": "l", "cell": "top", "instance": "R1", "params": {"value": "9k"}})
    assert err and "reason is required" in res
    res, err = T.call(ctx, "set_instance_params", {"lib": "l", "cell": "top", "instance": "R1", "params": {"value": "9k"}, "reason": "less current"})
    assert not err
    res, err = T.call(ctx, "set_instance_params", {"lib": "l", "cell": "top", "instance": "NOPE", "params": {"value": "1"}, "reason": "x"})
    assert err and "NOPE" in res
    assert srv.editor("p", "l", "top", "schematic").instance("R1")["props"]["value"] == "1k"        # real design untouched
    assert "9k" in T.call(ctx, "get_design_context", {"lib": "l", "cell": "top"})[0]                # the agent sees its own change
    g = cs.get("p", ctx.changeset)
    assert g["changes"][0]["ops"][0]["actor"] == "agent:t" and g["changes"][0]["ops"][0]["reason"] == "less current"
    # no cap by default; an optional one counts the agents' operations in the changeset, so a new session does not reset it
    for v in ("7k", "6k", "5k"):
        assert not T.call(ctx, "set_instance_params", {"lib": "l", "cell": "top", "instance": "R1", "params": {"value": v}, "reason": "x"})[1]
    old, T.MAX_WRITES = T.MAX_WRITES, 4
    try:
        res, err = T.call(ctx, "set_instance_params", {"lib": "l", "cell": "top", "instance": "R1", "params": {"value": "8k"}, "reason": "x"})
        assert err and "limit" in res and "UNDINE_AGENT_MAX_WRITES" in res
        fresh = T.ToolCtx(srv, "p", actor="agent:other", changeset=ctx.changeset)          # another session, same changeset
        res, err = T.call(fresh, "set_instance_params", {"lib": "l", "cell": "top", "instance": "R1", "params": {"value": "8k"}, "reason": "x"})
        assert err and "limit" in res
    finally:
        T.MAX_WRITES = old
    shutil.rmtree(root)


def test_propose_loop_through_the_gateway():
    root, srv, cs = tc.setup()
    httpd = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    srv.gateway = llm.Gateway(root, "server")
    srv.gateway.switch("server", "m", f"http://127.0.0.1:{httpd.server_port}/v1")
    SCRIPT[:] = [
        {"content": None, "tool_calls": [call("open_changeset", title="Raise R1", reason="less current"),
                                         call("set_instance_params", lib="l", cell="top", instance="R1", params={"value": "10k"}, reason="less current")]},
        {"content": "R1 を 10k にしました。レビューしてください。"},
    ]
    conv = {"id": "chat-x", "connection": {"kind": "server", "model": "m"}, "messages": []}
    res = agent.run(srv, "p", srv.project("p").path, conv, [], "R1 の電流を減らして", "agent:assistant", {})
    assert res["changeset"] and [a["tool"] for a in res["actions"]] == ["open_changeset", "set_instance_params"]
    assert "10k" in res["reply"] and conv["changeset"] == res["changeset"]
    assert srv.editor("p", "l", "top", "schematic").instance("R1")["props"]["value"] == "1k"        # nothing applied
    assert cs.get("p", res["changeset"])["status"] == "open"
    # the second request carried the tool results back in OpenAI format, and only the declared tools were offered
    assert any(m["role"] == "tool" for m in SEEN[-1]["messages"]) and {t["function"]["name"] for t in SEEN[0]["tools"]} == {n for n, t in T.BY_NAME.items() if not t.get("picture")}
    audit = [json.loads(l) for l in (srv.project("p").path / "llm" / "audit.jsonl").read_text().splitlines()]
    assert [a["step"] for a in audit] == [0, 1] and all(a["connection"]["kind"] == "server" for a in audit)
    httpd.shutdown()
    shutil.rmtree(root)


def test_mcp_front_end():
    root, srv, cs = tc.setup()
    srv.mcp_enabled = True
    sessions = mcp.Sessions()
    tok = mcp.ensure_token(root)
    H = {"authorization": f"Bearer {tok}"}
    assert mcp.handle(srv, sessions, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, {})[0] == 401
    code, body, hdr = mcp.handle(srv, sessions, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                  "params": {"protocolVersion": "2025-03-26", "clientInfo": {"name": "tester"}}}, H)
    assert code == 200 and body["result"]["serverInfo"]["name"] == "undine" and hdr["mcp-session-id"]
    H = {**H, "mcp-session-id": hdr["mcp-session-id"]}
    assert mcp.handle(srv, sessions, {"jsonrpc": "2.0", "method": "notifications/initialized"}, H)[0] == 202
    tools = mcp.handle(srv, sessions, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, H)[1]["result"]["tools"]
    assert {t["name"] for t in tools} == set(T.BY_NAME) and all("project" in t["inputSchema"]["required"] for t in tools)

    def tool(i, name, **a):
        return mcp.handle(srv, sessions, {"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": a}}, H)[1]["result"]

    assert tool(3, "set_instance_params", project="p", lib="l", cell="top", instance="R1", params={"value": "2k"}, reason="x")["isError"]
    assert not tool(4, "open_changeset", project="p", title="from mcp", reason="r")["isError"]
    assert not tool(5, "set_instance_params", project="p", lib="l", cell="top", instance="R1", params={"value": "2k"}, reason="x")["isError"]
    assert srv.editor("p", "l", "top", "schematic").instance("R1")["props"]["value"] == "1k"
    ch = cs.list("p")[0]
    assert ch["actor"] == "agent:tester" and ch["status"] == "open"
    assert tool(6, "get_design_context", project="p~nope", lib="l", cell="top")["isError"]       # previews are not addressable
    audit = [json.loads(l) for l in (srv.project("p").path / "llm" / "audit.jsonl").read_text().splitlines()]
    assert {a["connection"]["kind"] for a in audit} == {"mcp"} and any("set_instance_params" in a["purpose"] for a in audit)
    shutil.rmtree(root)


def test_selection_tool():
    root, srv, cs = tc.setup()
    ctx = T.ToolCtx(srv, "p")
    assert T.call(ctx, "get_selection", {})[0].count("nothing is selected")
    srv.selection["p"] = {"lib": "l", "cell": "top", "view": "schematic", "selection": [{"k": "i", "id": "R1"}], "revision": "old"}
    out = json.loads(T.call(ctx, "get_selection", {})[0])
    assert out["selection"][0]["id"] == "R1" and out["stale"] is True
    shutil.rmtree(root)


def test_layout_tools_in_a_changeset():
    root, srv, cs = tc.setup()
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    box = {"lib": "l", "cell": "top", "layer": "68/20", "box": [0, 0, 1.0, 0.14], "reason": "stub"}
    res, err = T.call(ctx, "add_box", box)
    assert err and "open_changeset" in res
    T.call(ctx, "open_changeset", {"title": "route", "reason": "r"})
    db = srv.db(ctx.work, "l")
    db.layout.layer(68, 20)
    db.layout.layer(68, 5)
    db.lv.add_missing_layers()
    res, err = T.call(ctx, "add_box", {**box, "layer": "nonsense"})
    assert err and "unknown layer" in res
    res, err = T.call(ctx, "add_box", {**box, "box": [0, 0, 0, 1]})
    assert err and "zero" in res
    res, err = T.call(ctx, "add_box", {**box, "reason": ""})
    assert err and "reason" in res
    res, err = T.call(ctx, "add_box", {**box, "box": [0.0012, 0, 1.0, 0.1234]})
    assert not err, res
    assert json.loads(T.call(ctx, "query_shapes", {"lib": "l", "cell": "top", "box": [-1, -1, 2, 2], "layer": "68/20"})[0])["items"][0]["bbox_um"] == [0.0, 0.0, 1.0, 0.125]
    res, err = T.call(ctx, "add_label", {"lib": "l", "cell": "top", "layer": "68/5", "text": "out", "x": 0.5, "y": 0.05,
                                         "reason": "name the net"})
    assert not err, res
    label = json.loads(T.call(ctx, "query_shapes", {"lib": "l", "cell": "top", "box": [-1, -1, 2, 2], "layer": "68/5"})[0])
    assert [(i["type"], i["text"]) for i in label["items"]] == [("text", "out")]          # the net a label names
    T.call(ctx, "delete_shapes", {"lib": "l", "cell": "top", "box": [-1, -1, 2, 2], "layer": "68/5", "reason": "no label"})
    info = json.loads(T.call(ctx, "get_layout_info", {"lib": "l", "cell": "top"})[0])
    assert info["own_shapes_by_layer"] == {"68/20": 1}
    res, err = T.call(ctx, "delete_shapes", {"lib": "l", "cell": "top", "box": [-1, -1, 2, 2], "layer": "68/20", "reason": "undo stub"})
    assert not err and json.loads(res)["deleted"] == 1
    assert T.call(ctx, "move_instance", {"lib": "l", "cell": "top", "instance": "M9", "dx": 1, "dy": 0, "reason": "x"})[1]   # not bound
    g = cs.get("p", ctx.changeset)
    ops = [(o["view"], o["op"]) for lc in g["changes"] for o in lc["ops"]]
    assert ("layout", "add_box") in ops and ("layout", "delete_shapes") in ops
    real = srv.db("p", "l")
    assert not any(real.cell("top").shapes(li).size() for li in real.layout.layer_indexes())      # real layout untouched
    shutil.rmtree(root)


def test_device_integrity_finds_poly_over_diffusion():
    import klayout.db as kdb
    root, srv, cs = tc.setup()
    db = srv.db("p", "l")
    dev = db.layout.create_cell("dev")
    dev.shapes(db.layout.layer(65, 20)).insert(kdb.Box(0, 0, 750, 1500))
    dev.shapes(db.layout.layer(66, 20)).insert(kdb.Box(300, -100, 450, 1600))      # gate: 0.15 um long
    top = db.cell("top")
    pid = db.layout.properties_id({"undine_inst": "M2"})
    top.insert(kdb.CellInstArray(dev.cell_index(), kdb.Trans()), pid)
    poly = db.layout.layer(66, 20)
    ctx = T.ToolCtx(srv, "p")
    top.shapes(poly).insert(kdb.Box(240, 1700, 510, 3000))                 # starts above the device: fine
    r = json.loads(T.call(ctx, "check_device_integrity", {"lib": "l", "cell": "top"})[0])
    assert r["problems"] == []
    top.shapes(poly).insert(kdb.Box(240, 1190, 510, 1700))                 # reaches into the diffusion: LVS would see L/W change
    r = json.loads(T.call(ctx, "check_device_integrity", {"lib": "l", "cell": "top", "instance": "M2"})[0])
    assert len(r["problems"]) == 1 and r["problems"][0]["where_um"] == [0.24, 1.19, 0.51, 1.5]
    assert "delete_shapes" in r["advice"]
    shutil.rmtree(root)


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)


def test_propose_continues_a_changeset_and_reports_what_a_person_changed():
    root, srv, cs = tc.setup()
    httpd = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    srv.gateway = llm.Gateway(root, "server")
    srv.gateway.switch("server", "m", f"http://127.0.0.1:{httpd.server_port}/v1")
    conv = {"id": "chat-c", "connection": {"kind": "server", "model": "m"}, "messages": []}
    prj = srv.project("p").path
    SCRIPT[:] = [
        {"content": None, "tool_calls": [call("open_changeset", title="Raise R1", reason="r"),
                                         call("set_instance_params", lib="l", cell="top", instance="R1", params={"value": "10k"}, reason="less current")]},
        {"content": "done"},
    ]
    res = agent.run(srv, "p", prj, conv, [], "raise R1", "agent:assistant", {})
    cid = res["changeset"]
    # a person edits in the preview
    ed = srv.editor(f"p~{cid}", "l", "top", "schematic")
    with ed.recording("set_props", {"id": "R1"}, "user", "keep this value", cid):
        ed.set_props("i", "R1", {"value": "12k"})
    # next turn: the same conversation continues the same changeset and is told what changed since
    SEEN.clear()
    SCRIPT[:] = [{"content": "ok, kept 12k"}]
    res2 = agent.run(srv, "p", prj, conv, [], "now fix the rest", "agent:assistant", {})
    assert res2["changeset"] == cid
    first = json.dumps(SEEN[0]["messages"][-1])
    assert "<changeset_state" in first and "by a person" in first and "12k" in first and "keep this value" in first
    assert "10k→12k" in first.replace(" ", "") or "10k" in first
    # and a turn with nothing new says so
    SEEN.clear()
    SCRIPT[:] = [{"content": "nothing to do"}]
    agent.run(srv, "p", prj, conv, [], "anything else?", "agent:assistant", {})
    assert "No changes since your last turn" in json.dumps(SEEN[0]["messages"][-1])
    shutil.rmtree(root)


def test_chat_endpoint_from_a_preview_continues_the_changeset_and_keeps_the_conversation_in_the_real_project():
    from fastapi.testclient import TestClient
    from undine.server.api import create_app
    root, srv, cs = tc.setup()
    httpd = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    srv.gateway = llm.Gateway(root, "server")
    srv.gateway.switch("server", "m", f"http://127.0.0.1:{httpd.server_port}/v1")
    c = TestClient(create_app(srv, None))
    meta = cs.create("p", "Human started", "r", "user")
    P = meta["preview"]
    body = {"message": "continue", "context": {"lib": "l", "cell": "top", "view": "schematic", "selection": [], "overview": True}, "mode": "propose"}
    SCRIPT[:] = [{"content": "continued"}]
    r = c.post(f"/api/p/{P}/chat?wait=true", json=body)
    assert r.status_code == 200 and r.json()["changeset"] == meta["id"], r.text
    cid = r.json()["conversation"]
    assert c.get(f"/api/p/p/chats/{cid}").status_code == 200 and c.get(f"/api/p/{P}/chats/{cid}").status_code == 200
    # a closed changeset cannot be continued
    cs.discard("p", meta["id"])
    SCRIPT[:] = [{"content": "x"}]
    assert c.post(f"/api/p/{P}/chat?wait=true", json=body).status_code >= 400
    shutil.rmtree(root)


def test_create_cell_is_recorded_applied_and_the_symbol_waits_for_ports():
    root, srv, cs = tc.setup()
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    res, err = T.call(ctx, "create_cell", {"lib": "l", "cell": "ota", "reason": "x"})
    assert err and "open_changeset" in res
    T.call(ctx, "open_changeset", {"title": "New OTA", "reason": "r"})
    P = f"p~{ctx.changeset}"
    for bad, msg in (({"lib": "l", "cell": "ota"}, "reason"), ({"lib": "l", "cell": "bad name!", "reason": "x"}, ""),
                     ({"lib": "nope", "cell": "ota", "reason": "x"}, "nope"), ({"lib": "l", "cell": "top", "reason": "x"}, "already"),
                     ({"lib": "l", "cell": "ota", "views": ["symbol"], "reason": "x"}, "schematic first")):
        res, err = T.call(ctx, "create_cell", bad)
        assert err and msg in res, (bad, res)
    res, err = T.call(ctx, "create_cell", {"lib": "l", "cell": "ota", "views": ["schematic", "layout"], "reason": "a new amplifier"})
    assert not err, res
    sch = srv.project(P).library("l").path / "ota.sch"
    assert sch.is_file() and not (srv.project("p").library("l").path / "ota.sch").exists()     # the real project is untouched
    T.call(ctx, "add_instance", {"lib": "l", "cell": "ota", "symbol": "r.sym", "x": 0, "y": 0, "params": {"name": "R1", "value": "5k"}, "reason": "load"})
    res, err = T.call(ctx, "create_cell", {"lib": "l", "cell": "ota", "views": ["symbol"], "reason": "its symbol"})
    assert not err, res
    g = cs.get("p", ctx.changeset)
    ops = [(o["view"], o["op"], [e["k"] for e in o["effects"]]) for o in g["changes"][0]["ops"]]
    assert ops == [("schematic", "create_cell", ["v"]), ("layout", "create_cell", ["c"]), ("schematic", "add_instance", ["i"]),
                   ("symbol", "create_cell", ["v"])], ops
    cs.apply("p", ctx.changeset, "user")
    real = srv.project("p").library("l")
    assert (real.path / "ota.sch").is_file() and (real.path / "ota.sym").is_file() and "ota" in real.layout_cells()[0]
    assert srv.editor("p", "l", "ota", "schematic").instance("R1")["props"]["value"] == "5k"
    # taking it back keeps the cell: a revert merges the base back but keeps the files the changeset added as they are
    # now (docs/design/changeset-branches.md, Revert: a cell it made may be used elsewhere since), its instance too
    cs.revert("p", ctx.changeset, "user")
    assert (real.path / "ota.sch").is_file() and (real.path / "ota.sym").is_file() and "ota" in real.layout_cells()[0]
    assert srv.editor("p", "l", "ota", "schematic").instance("R1")["props"]["value"] == "5k"
    assert cs.get("p", ctx.changeset)["reverted"]["kept"] == ["libs/l/ota.netlist.yaml", "libs/l/ota.oas",
                                                               "libs/l/ota.sch", "libs/l/ota.sym"]
    shutil.rmtree(root)


def test_list_symbols_searches_and_describes():
    root, srv, cs = tc.setup()
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    res, err = T.call(ctx, "list_symbols", {"query": "r.sym"})
    assert not err and "l/r.sym" in res
    res, err = T.call(ctx, "list_symbols", {"ref": "l/r.sym"})
    assert not err and '"pins"' in res and "P" in res
    res, err = T.call(ctx, "list_symbols", {"ref": "l/missing.sym"})
    assert err and "not found" in res
    shutil.rmtree(root)


def test_connect_pins_by_net_name_and_ports_join_by_label():
    import pytest
    root, srv, cs = tc.setup()
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    T.call(ctx, "open_changeset", {"title": "wire", "reason": "r"})
    ed = srv.editor(f"p~{ctx.changeset}", "l", "top", "schematic")
    if ed.resolver.load("devices/lab_pin.sym", ed.path.parent) is None:
        pytest.skip("Xschem's devices library is not installed")
    T.call(ctx, "add_instance", {"lib": "l", "cell": "top", "symbol": "r.sym", "x": 200, "y": 0, "params": {"name": "R2"}, "reason": "x"})
    T.call(ctx, "add_instance", {"lib": "l", "cell": "top", "symbol": "devices/opin.sym", "x": -200, "y": 0, "params": {"lab": "out"}, "reason": "port"})
    res, err = T.call(ctx, "connect_pins", {"lib": "l", "cell": "top", "net": "out", "pins": ["R1:P", "R2:P"], "reason": "join"})
    assert not err, res
    assert '"wires_added":1' in res and '"labelled":[]' in res              # one routed wire, carrying the name
    ctxt = T.call(ctx, "get_design_context", {"lib": "l", "cell": "top"})[0]
    assert "R1 [resistor] value=1k | P=out" in ctxt and "R2 [resistor] value=1k | P=out" in ctxt
    assert "ports: out(opin)" in ctxt                                         # the port joins by its label
    for bad, msg in ((["R9:P"], "no instance R9"), (["R1:X"], "has no pin X"), (["R1"], "instance:pin")):
        res, err = T.call(ctx, "connect_pins", {"lib": "l", "cell": "top", "net": "y", "pins": bad, "reason": "x"})
        assert err and msg in res, res
    res, err = T.call(ctx, "connect_pins", {"lib": "l", "cell": "top", "net": "other", "pins": ["R1:P"], "reason": "x"})
    assert err and "already on net out" in res
    res, err = T.call(ctx, "connect_pins", {"lib": "l", "cell": "top", "net": "out", "pins": ["R1:P"], "reason": "again"})
    assert not err and '"already_on_net":["R1:P"]' in res and '"wires_added":0' in res      # already there
    res, err = T.call(ctx, "connect_pins", {"lib": "l", "cell": "top", "net": "vdd", "pins": ["R1:M", "R2:M"], "reason": "supply"})
    assert not err and '"labelled":["R1:M","R2:M"]' in res                   # supplies get labels
    shutil.rmtree(root)


def test_batch_tools_place_and_connect_in_one_operation():
    import pytest
    root, srv, cs = tc.setup()
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    T.call(ctx, "open_changeset", {"title": "batch", "reason": "r"})
    ed = srv.editor(f"p~{ctx.changeset}", "l", "top", "schematic")
    if ed.resolver.load("devices/lab_pin.sym", ed.path.parent) is None:
        pytest.skip("Xschem's devices library is not installed")
    res, err = T.call(ctx, "add_instances", {"lib": "l", "cell": "top", "reason": "two more", "instances": [
        {"symbol": "r.sym", "x": 100, "y": 0, "params": {"name": "R2"}}, {"symbol": "r.sym", "x": 200, "y": 0, "params": {"name": "R3"}}]})
    assert not err and '"R2","R3"' in res, res
    res, err = T.call(ctx, "connect_nets", {"lib": "l", "cell": "top", "reason": "chain",
                                            "nets": {"a": ["R1:M", "R2:P"], "b": ["R2:M", "R3:P"]}})
    assert not err and '"labelled":{}' in res and '"wires_added"' in res, res
    ctxt = T.call(ctx, "get_design_context", {"lib": "l", "cell": "top"})[0]
    assert "R2 [resistor] value=1k | P=a M=b" in ctxt
    res, err = T.call(ctx, "connect_nets", {"lib": "l", "cell": "top", "reason": "bad", "nets": {"c": ["R9:P"]}})
    assert err and "no instance R9" in res
    ops = [o["op"] for o in cs.get("p", ctx.changeset)["changes"][0]["ops"]]
    assert ops.count("add_instances") == 2 and ops.count("connect_nets") == 1      # one record, one operation each
    tl = cs.timeline("p", ctx.changeset)
    assert [o["ops"] for o in tl["ops"]] == [["add_instances"], ["connect_nets"]]
    shutil.rmtree(root)
