"""Several agents over one MCP connection (mcp.Sessions, the `agent` argument): a desktop client's subagents share one
mcp-stdio relay, and each named agent has a changeset of its own, a write lease of its own and its name in the history;
without `agent` every call of the connection shares one changeset, as before. And what get_changeset tells of any
changeset, a closed one too."""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fastapi.testclient import TestClient  # noqa: E402

from undine.agents import agent_tools as T  # noqa: E402
from undine.agents import mcp  # noqa: E402
from undine.server.api import create_app  # noqa: E402
import test_changeset as tc  # noqa: E402
import test_mcp_local as tl  # noqa: E402

R1 = {"lib": "l", "cell": "top", "instance": "R1"}


def connect(la, client="claude-code"):
    la.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"clientInfo": {"name": client}}})
    n = iter(range(2, 10 ** 6))

    def tool(name, agent=None, **args):
        res = la.handle(tl.call(next(n), name, **({"agent": agent} if agent is not None else {}), **args))["result"]
        text = res["content"][0]["text"]
        if res["isError"]:
            return {"error": text}
        try:
            return json.loads(text)
        except ValueError:
            return {"text": text}
    return tool


def test_agents_on_one_connection_never_write_each_others_changesets():
    root, srv, la, _ = tl.setup()
    c = TestClient(create_app(srv, None))
    tool = connect(la)
    tools = la.handle({"jsonrpc": "2.0", "id": 99, "method": "tools/list"})["result"]["tools"]
    assert all("agent" in t["inputSchema"]["properties"] and "agent" not in t["inputSchema"].get("required", [])
               for t in tools)                       # the relay's own open_in_undine too
    a = tool("open_changeset", agent="layout", title="Layout", reason="r")["id"]
    b = tool("open_changeset", agent="sim", title="Testbench", reason="r")["id"]
    assert a != b
    # each edit lands in its own agent's changeset, however their calls interleave
    assert "error" not in tool("set_instance_params", agent="layout", **R1, params={"value": "2k"}, reason="for the layout")
    assert "error" not in tool("set_instance_params", agent="sim", **R1, params={"value": "3k"}, reason="for the bench")
    assert "error" not in tool("set_instance_params", agent="layout", **R1, params={"value": "4k"}, reason="again")
    assert (tc.value(srv, f"p~{a}"), tc.value(srv, f"p~{b}"), tc.value(srv)) == ("4k", "3k", "1k")
    mine = tool("get_changeset", agent="layout")
    assert mine["id"] == a and mine["yours"] and [s["by"] for s in mine["changes"]] == ["agent:claude-code/layout"] * 2
    assert {r["actor"] for r in srv.oplog(f"p~{b}", "l").read() if r.get("changeset") == b} == {"agent:claude-code/sim"}
    # the same name is the same agent: it shares the changeset (another call, or a parallel one, of that subagent)
    again = tool("open_changeset", agent="layout", title="other", reason="r")
    assert again["id"] == a and again["note"].startswith("already open in this session")
    assert again["you"] == f"claude-code/layout (MCP session {la.session[:8]})"      # who the caller is: its own name
    # the leases tell them apart: one agent cannot write the changeset another holds, and the refusal names the holder
    held = srv.leases.get(a)
    assert held.owner.endswith(":layout") and held.label == "claude-code/layout"
    took = tool("open_changeset", agent="sim", id=a)
    assert "claude-code/layout" in took["writer"]["who"] and not took["writer"]["you"] and "refused" in took["note"]
    refused = tool("set_instance_params", agent="sim", **R1, params={"value": "9k"}, reason="mine?")["error"]
    assert "claude-code/layout" in refused and "not your changeset" in refused and tc.value(srv, f"p~{a}") == "4k"
    rows = {r["id"]: r for r in tool("list_changesets", agent="layout")["open"]}
    assert rows[a]["yours"] and rows[a]["writer"]["you"] and "yours" not in rows[b] and not rows[b]["writer"]["you"]
    assert "claude-code/sim" in rows[b]["writer"]["who"]
    # the review says who is editing; stopping one agent stops that agent only
    assert c.get(f"/api/p/p/changesets/{a}").json()["writer"]["label"] == "claude-code/layout"
    assert c.post(f"/api/p/p/changesets/{a}/writer/stop").json()["stopped"]["label"] == "claude-code/layout"
    assert "stopped" in tool("set_instance_params", agent="layout", **R1, params={"value": "5k"}, reason="x")["error"]
    assert "stopped" in tool("open_changeset", agent="layout", title="again", reason="r")["error"]   # opens nothing
    assert sorted(r["id"] for r in tc.Changesets(srv).list("p") if r["status"] == "open") == sorted([a, b])
    assert "error" not in tool("open_changeset", agent="sim", id=b)
    assert srv.leases.get(a) is None                        # going back to its own, it took no lock of the one it left
    assert "error" not in tool("set_instance_params", agent="sim", **R1, params={"value": "6k"}, reason="still mine")
    # the connection ends (the chat closed): every agent's lease is released at once
    assert srv.leases.get(b) is not None
    la.close()
    assert srv.leases.get(a) is None and srv.leases.get(b) is None
    shutil.rmtree(root)


def test_a_stopped_agent_opens_nothing_in_any_project_and_is_named():
    """Stop in the review stops that agent of the connection, whatever project it goes to next; the refusal says it
    was the agent, not the session, since the connection's other agents go on."""
    root, srv, la, _ = tl.setup()
    srv.store.create("q")
    tool = connect(la)
    a = tool("open_changeset", agent="layout", title="Layout", reason="r")["id"]
    assert "error" not in tool("set_instance_params", agent="layout", **R1, params={"value": "2k"}, reason="x")
    tool("open_changeset", agent="sim", title="Bench", reason="r")
    srv.leases.stop(a)
    refused = tool("set_instance_params", agent="layout", **R1, params={"value": "3k"}, reason="x")["error"]
    assert refused.startswith("this agent (layout) was stopped")
    elsewhere = tool("open_changeset", agent="layout", project="q", title="C", reason="r")
    assert "this agent (layout) was stopped" in elsewhere["error"]
    assert tc.Changesets(srv).list("q") == []                  # nothing created there either
    assert "error" not in tool("set_instance_params", agent="sim", **R1, params={"value": "4k"}, reason="still on")
    shutil.rmtree(root)


def test_request_ids_are_per_agent():
    """Two agents of one connection that choose the same request_id make two calls: neither gets the other's result
    as a replay (the open_changeset of the other's changeset, an edit done in the other's). A retry of one agent's
    call is still a replay of its own."""
    root, srv, la, _ = tl.setup()
    connect(la)
    n = iter(range(100, 10 ** 6))

    def raw(name, agent, rid, **args):
        res = la.handle(tl.call(next(n), name, agent=agent, request_id=rid, **args))["result"]
        assert not res["isError"], res
        return json.loads(res["content"][0]["text"]), (res.get("_meta") or {}).get("undine/replayed", False)

    (one, r1), (two, r2) = (raw("open_changeset", agent, "open-1", title="T") for agent in ("n1", "n2"))
    assert one["id"] != two["id"] and not r1 and not r2
    for agent, cid in (("n1", one["id"]), ("n2", two["id"])):
        _, replayed = raw("set_instance_params", agent, "req-1", **R1, params={"value": "same"}, reason="r")
        assert not replayed and tc.value(srv, f"p~{cid}") == "same"
    assert raw("open_changeset", "n2", "open-1", title="T") == (two, True)      # its own retry: replayed
    assert raw("set_instance_params", "n1", "req-1", **R1, params={"value": "same"}, reason="r")[1]
    assert raw("get_changeset", "n2", None)[0]["id"] == two["id"]
    shutil.rmtree(root)


def test_a_writer_note_says_when_the_holders_lock_ends():
    """open_changeset(id) of a changeset another writer holds says when that lock ends: an agent's lapses without
    writes, the assistant's lasts its turn."""
    root, srv, la, _ = tl.setup()
    tool = connect(la)
    a = tool("open_changeset", agent="layout", title="Layout", reason="r")["id"]
    assert "error" not in tool("set_instance_params", agent="layout", **R1, params={"value": "2k"}, reason="x")
    assert "lapses after 120 s without a write" in tool("open_changeset", agent="sim", id=a)["note"]
    srv.leases.release(a, srv.leases.get(a).owner)
    srv.leases.acquire(a, "turn:t-1", "turn", "assistant")
    note = tool("open_changeset", agent="sim", id=a)["note"]
    assert "the assistant is writing it now" in note and "until its turn ends" in note and "120 s" not in note
    shutil.rmtree(root)


def test_without_an_agent_name_the_connection_shares_one_changeset():
    root, srv, la, _ = tl.setup()
    tool = connect(la, "desk")
    cid = tool("open_changeset", title="One", reason="r")["id"]
    again = tool("open_changeset", title="Two", reason="r")
    assert again["id"] == cid and again["title"] == "One" and again["you"] == f"desk (MCP session {la.session[:8]})"
    assert "opened by agent:desk" in again["note"]
    assert tool("open_changeset", agent=" ", title="Three", reason="r")["id"] == cid       # a blank name is no name
    assert "error" not in tool("set_instance_params", **R1, params={"value": "2k"}, reason="x")
    assert srv.leases.get(cid).owner == f"mcp:{la.session}" and srv.leases.get(cid).label == "desk"
    assert [r["actor"] for r in srv.oplog(f"p~{cid}", "l").read() if r.get("changeset") == cid] == ["agent:desk"]
    # a named agent of the same connection is another writer: the connection's changeset is not its own
    assert tool("get_changeset", agent="helper") == {"open": False, "note": tool("get_changeset", agent="helper")["note"]}
    tool("open_changeset", agent="helper", id=cid)
    assert "an external agent (desk, MCP session" in tool("set_instance_params", agent="helper", **R1,
                                                         params={"value": "3k"}, reason="x")["error"]
    for bad in ("a:b", "x" * 65, "two words"):
        assert "agent is a name" in tool("get_changeset", agent=bad)["error"]
    assert "agent is a name" in tool("get_changeset", agent=7)["error"]
    audit = [json.loads(line) for line in (srv.project("p").path / "llm" / "audit.jsonl").read_text().splitlines()]
    assert all("agent" not in a["payload"]["arguments"] for a in audit) and any(a["payload"].get("agent") == "helper" for a in audit)
    shutil.rmtree(root)


def test_sessions_key_their_contexts_by_agent_and_project():
    root, srv, _ = tc.setup()
    s = mcp.Sessions()
    sid = s.new("cli")
    one, two, none = s.ctx(srv, sid, "p", "one"), s.ctx(srv, sid, "p", "two"), s.ctx(srv, sid, "p")
    assert len({id(one), id(two), id(none)}) == 3 and s.ctx(srv, sid, "p", "one") is one
    assert (one.owner, one.actor, none.owner, none.actor) == (f"mcp:{sid}:one", "agent:cli/one", f"mcp:{sid}", "agent:cli")
    shutil.rmtree(root)


def test_get_changeset_of_a_closed_changeset_gives_its_record():
    root, srv, cs = tc.setup()
    ctx = T.ToolCtx(srv, "p", actor="agent:t", owner="mcp:s1", owner_kind="mcp")

    def call(name, c=ctx, **args):
        text, err = T.call(c, name, args)
        assert not err, text
        return json.loads(text)

    assert call("get_changeset")["open"] is False
    cid = call("open_changeset", title="Raise R1", reason="less current")["id"]
    call("set_instance_params", **R1, params={"value": "10k"}, reason="less current")
    open_ = call("get_changeset")
    assert open_["open"] and open_["status"] == "open" and open_["writer"] == {"who": open_["writer"]["who"], "you": True}
    assert [(s["by"], s["reason"], s["view"], list(s["cells"])) for s in open_["changes"]] == \
        [("agent:t", "less current", "schematic", ["l/top"])]
    srv.leases.release(cid, "mcp:s1")
    srv.lease_ended()
    cs.apply("p", cid, "user")
    done = call("get_changeset")                                   # its own, closed: the record, not {"open": false}
    assert (done["open"], done["status"], done["title"], done["reason"], done["by"]) == \
        (False, "applied", "Raise R1", "less current", "agent:t")
    assert done["applied"]["by"] == "user" and done["applied"]["commit"] and "uncommitted" in done["applied"]
    assert done["changes"] == open_["changes"] and done["evidence"]["captured"] and done["git"]["ref"]
    assert isinstance(done["checks"], list) and "writer" not in done
    other = T.ToolCtx(srv, "p", actor="agent:u", owner="mcp:s2", owner_kind="mcp")   # another session reads it by id
    assert {**call("get_changeset", other, id=cid), "yours": True} == done
    cs.revert("p", cid, "user")
    back = call("get_changeset", other, id=cid)
    assert back["status"] == "reverted" and back["reverted"]["by"] == "user" and back["applied"]["commit"]
    # its own changeset applied, an agent's open_changeset opens a new one instead of "already open"
    new = call("open_changeset", title="Next", reason="r")["id"]
    assert new != cid
    call("set_instance_params", **R1, params={"value": "7k"}, reason="r")
    srv.leases.release(new, "mcp:s1")
    cs.discard("p", new, "agent:reviewer")
    gone = call("get_changeset", other, id=new)
    assert gone["status"] == "discarded" and gone["discarded"]["by"] == "agent:reviewer" and gone["discarded"]["t"]
    assert gone["changes"][0]["reason"] == "r"
    for bad in ("cs-000000", "../p"):
        text, err = T.call(other, "get_changeset", {"id": bad})
        assert err and "list_changesets" in text
    shutil.rmtree(root)
