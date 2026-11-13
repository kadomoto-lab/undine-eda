"""Tool results agents can read whole: a long result shortened inside and still valid JSON (results.py) at the in-app,
MCP and shell budgets; paging and filters (get_netlist, get_intent, get_design_context); argument shapes as usage
errors; the shell's key:=@file and request ids; plot windows, schematic zoom and full-precision samples."""
import argparse
import io
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from undine.agents import agent_tools as T, mcp, results  # noqa: E402
from undine.agents.client import Undine, add_parsers, main_call, main_tools  # noqa: E402
from undine.server.api import create_app  # noqa: E402
from undine.simulation import sim, simresults  # noqa: E402
import test_changeset as tc  # noqa: E402
from test_mcp_local import transport  # noqa: E402

M_SYM = ('v {xschem version=3.4.5 file_version=1.2}\nK {type=nmos\nformat="@spiceprefix@name @pinlist @model W=@W ad=@ad"\n'
         'template="name=M1 W=1 model=n ad=\\\\"expr(\'@W * 0.29\')\\\\" pd=\\\\"expr(\'2 * (@W + 0.29)\')\\\\""}\n'
         'B 5 -2.5 -32.5 2.5 -27.5 {name=D dir=inout}\nB 5 -22.5 -2.5 -17.5 2.5 {name=G dir=in}\n'
         'B 5 -2.5 27.5 2.5 32.5 {name=S dir=inout}\nB 5 17.5 -2.5 22.5 2.5 {name=B dir=in}\n')
CODE_SYM = 'v {xschem version=3.4.5 file_version=1.2}\nK {type=netlist_commands\ntemplate="name=s1 value=blabla"\nformat="\n@value\n"}\n'
MEAS = ".meas tran tlast FIND v(n3) AT=1u\n.options savecurrents\n.control\nrun\n.endc"


def _big(n=70):
    """A changeset with a cell of n transistors in a chain (net k joins Mk:D and Mk+1:S) and a code block."""
    root, srv, _ = tc.setup()
    lib = srv.project("p").library("l").path
    (lib / "m.sym").write_text(M_SYM)
    (lib / "code.sym").write_text(CODE_SYM)
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    T.call(ctx, "open_changeset", {"title": "big", "reason": "r"})
    assert not T.call(ctx, "create_cell", {"lib": "l", "cell": "big", "reason": "x"})[1]
    if srv.editor(ctx.work, "l", "big", "schematic").resolver.load(T.LABEL_SYMBOL, lib) is None:
        shutil.rmtree(root)
        pytest.skip("Xschem's devices library is not installed")
    insts = [{"symbol": "m.sym", "x": 200 * (i % 10), "y": 300 * (i // 10), "params": {"name": f"M{i}", "W": "2"}} for i in range(n)]
    insts.append({"symbol": "code.sym", "x": -600, "y": 0, "params": {"name": "MEAS", "value": MEAS}})
    assert not T.call(ctx, "add_instances", {"lib": "l", "cell": "big", "instances": insts, "reason": "x"})[1]
    nets = {f"n{i}": [f"M{i}:D", f"M{i + 1}:S"] for i in range(n - 1)}
    assert not T.call(ctx, "connect_nets", {"lib": "l", "cell": "big", "nets": nets, "style": "label", "reason": "x"}, None)[1]
    return root, srv, ctx


def test_a_long_result_is_shortened_inside_and_stays_valid_json():
    net = {"ports": [{"name": "a", "type": "ipin"}],
           "instances": {f"M{i}": {"symbol": "m.sym", "pins": {"D": f"n{i}", "S": f"n{i + 1}"}, "params": {"W": "2"}} for i in range(116)},
           "code": [{"name": "MEAS", "value": ".meas tran x FIND v(a) AT=1u\n" * 300}], "note": "a record keeps its keys"}
    text = results.text(net, 9000, "page with offset")
    out = json.loads(text)
    assert len(text) <= 9000 and out["note"] == net["note"] and out["ports"] == net["ports"]
    kept = [k for k in out["instances"] if k != results.MARK]
    assert kept == [f"M{i}" for i in range(len(kept))] and 5 < len(kept) < 116          # the first ones, in order
    assert out["instances"][results.MARK] == f"{116 - len(kept)} more (116 in all)"
    assert out["code"][0]["value"].endswith("more characters)")                        # both long parts give way
    t = out["_truncated"]
    assert t["max_chars"] == 9000 and t["chars"] == len(results.compact(net)) and t["how"] == "page with offset"
    assert t["cut"]["instances"] == f"{len(kept)} of 116" and t["cut"]["code[0].value"].endswith(" characters")
    # a list of rows is cut at its end; odd keys are written as a reader would; a list result gets a key of its own
    rows = {"signals": {"v(x1.b9)": [[k * 1e-9, 0.123456789012345] for k in range(512)]}}
    out = json.loads(results.text(rows, 3000))
    assert out["signals"]["v(x1.b9)"][-1].startswith(results.MARK) and 'signals["v(x1.b9)"]' in out["_truncated"]["cut"]
    out = json.loads(results.text(list(range(3000)), 500))
    assert out["result"][:3] == [0, 1, 2] and out["result"][-1].endswith("(3000 in all)")
    assert results.text(net, None) == results.compact(net) and results.text("x" * 50, 10).endswith("40 more characters)")
    pretty = lambda v: json.dumps(v, indent=2)                                            # noqa: E731
    assert len(results.text(net, 20000, "", pretty)) <= 20000                            # the budget of what is printed
    assert results.fit({"a": 1}, 10) == {"a": 1}


def _netlist(n, at=0):
    return {"ports": [{"name": "a", "type": "ipin"}], "pending": {}, "note": "pending connections ...",
            "instances": {f"M{i}": {"symbol": "sky130_fd_pr/nfet_01v8.sym", "pins": {"D": f"n{i}", "G": f"g{i}", "S": f"n{i + 1}"},
                                    "params": {"W": "2", "L": "0.15", "nf": "1", "model": "nfet_01v8"}} for i in range(at, at + n)}}


def test_a_few_large_parts_are_each_shortened_not_dropped():
    """A batch of two netlists over the budget keeps both, each shortened alike, and fills the budget; a collection
    drops parts only where each would be left less than MIN_PART."""
    v = {"applied": 3, "results": [{"ok": True}, _netlist(80), _netlist(80, 80)]}
    for limit in (20000, 9000, 3000):
        text = results.text(v, limit)
        out = json.loads(text)
        assert limit * 0.9 <= len(text) <= limit and len(out["results"]) == 3 and out["results"][0] == {"ok": True}
        kept = [len(r["instances"]) - 1 for r in out["results"][1:]]                    # alike: M80.. are longer names
        assert abs(kept[0] - kept[1]) <= 2 and 0 < min(kept) and max(kept) < 80
        assert out["_truncated"]["cut"] == {f"results[{i}].instances": f"{k} of 80" for i, k in zip((1, 2), kept)}
    out = json.loads(results.text([_netlist(100), _netlist(100), _netlist(100)], 9000))
    assert [r["instances"][results.MARK].endswith("(100 in all)") for r in out["result"]] == [True] * 3
    out = json.loads(results.text({"texts": ["x" * 5000] * 30}, 9000))["texts"]
    assert out[-1].endswith("(30 in all)") and 3 < len(out) - 1 < 10 and all(len(t) > results.MIN_PART * 0.8 for t in out[:-1])
    pretty = lambda x: json.dumps(x, indent=2)                                            # noqa: E731
    assert 22000 < len(results.text({"results": [_netlist(200), _netlist(200)]}, 25000, "", pretty)) <= 25000
    # a megabyte of mixed nesting: fitted at once, not by halves, and a reader still gets most of the budget
    rnd = np.random.default_rng(1)

    def mixed(d=0):
        r = rnd.random()
        if d > 3 or r < 0.3:
            return "x" * int(rnd.integers(1, 3000)) if rnd.random() < 0.5 else float(rnd.random())
        if r < 0.65:
            return [mixed(d + 1) for _ in range(int(rnd.integers(1, 12)))]
        return {f"k{i}": mixed(d + 1) for i in range(int(rnd.integers(1, 10)))}
    big = [mixed() for _ in range(18)]
    assert len(results.compact(big)) > 10 ** 6
    t0 = time.monotonic()
    text = results.text(big, 9000)
    assert time.monotonic() - t0 < 10 and 8100 <= len(text) <= 9000 and len(json.loads(text)["result"]) > 3
    rows = {"rows": [[k * 1e-9, 0.123456789012345] for k in range(20000)]}           # measured as far as it is listed
    assert json.loads(results.text(rows, 20000))["_truncated"]["cut"]["rows"].endswith("of 20000")


def test_batch_reads_through_the_tool_budgets_keep_every_result():
    root, srv, ctx = _big(70)
    L = {"lib": "l", "cell": "big"}
    ops = [{"tool": "set_instance_params", "args": {**L, "instance": "M1", "params": {"W": "3"}, "reason": "x"}},
           *({"tool": "get_netlist", "args": {**L, "instances": [f"M{i}" for i in range(a, a + 35)], "all_params": True}}
             for a in (0, 35))]
    whole = T.call(ctx, "batch", {"ops": ops, "dry_run": True, "reason": "x"}, None)[0]
    for limit in (12000, T.RESULT_CHARS):
        assert len(whole) > limit
        text, err = T.call(ctx, "batch", {"ops": ops, "dry_run": True, "reason": "x"}, limit)
        out = json.loads(text)
        assert not err and limit * 0.9 <= len(text) <= limit and len(out["results"]) == 3
        assert all(20 < len(r["result"]["instances"]) - 1 < 35 for r in out["results"][1:])     # both netlists, shortened
    shutil.rmtree(root)


def test_schemas_show_what_lists_hold_and_wrong_shapes_are_usage_errors():
    def arrays(s, path):
        for x in s.get("anyOf") or []:
            yield from arrays(x, path)
        if s.get("type") == "array":
            yield path, s
            yield from arrays(s.get("items") or {}, path + "[]")
        for k, x in (s.get("properties") or {}).items():
            yield from arrays(x, f"{path}.{k}")
    assert [p for t in T.TOOLS for p, s in arrays(t["input_schema"], t["name"]) if not s.get("items")] == []
    assert results.shape(T.BY_NAME["get_design_context"]["input_schema"]["properties"]["objects"]) == \
        "[string or {kind: instance|net|wire, id: string}, ...]"
    root, srv, _ = tc.setup()
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    T.call(ctx, "open_changeset", {"title": "t", "reason": "r"})
    text, err = T.call(ctx, "delete_objects", {"lib": "l", "cell": "top", "objects": [["R1"]], "reason": "x"})
    assert err and text.startswith("objects[0] is a list: delete_objects takes objects: [string or {kind: instance|wire")
    text, err = T.call(ctx, "add_path", {"lib": "l", "cell": "top", "layer": "met1", "points": [0, 1], "width": 0.2, "reason": "x"})
    assert err and "points[0] is a number (0), not a list" in text and "[[number, ...], ...]" in text
    text, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "top", "analysis": "tran"})
    assert err and "analysis is a string ('tran'), not an object" in text
    text, err = T.call(ctx, "batch", {"ops": [{"tool": "get_netlist", "args": {"lib": "l", "cell": "top", "nets": {"a": 1}}}]})
    assert err and text.startswith("ops[0] get_netlist: nets is an object, not a list")
    assert T._args(T.BY_NAME["run_simulation"], {"signals": "out", "lib": "l"}) == {"signals": ["out"], "lib": "l"}
    assert T._args(T.BY_NAME["import_layout"], {"replace": True})["replace"] is True        # a list or true
    assert T._args(T.BY_NAME["import_layout"], {"replace": "c1"})["replace"] == ["c1"]
    for tool in ("get_design_context", "delete_objects"):                                  # names or {kind, id}
        assert T._args(T.BY_NAME[tool], {"objects": "xgn2"}) == {"objects": ["xgn2"]}
    # a plain name is an instance to delete, alone too (objects=R1 from the shell); before, a string here was an
    # internal error
    assert json.loads(T.call(ctx, "delete_objects", {"lib": "l", "cell": "top", "objects": "R1", "reason": "x"})[0])["deleted"] == 1
    shutil.rmtree(root)


def test_yes_no_arguments_sent_as_text_are_booleans_for_every_tool():
    """undine call move=false sends the text 'false', which bool() and `if a.get(...)` read as true: every boolean of a
    schema is a boolean before a tool reads it, nested ones too, and other text is a usage error."""
    assert T._args(T.BY_NAME["get_netlist"], {"all_params": " FALSE "})["all_params"] is False
    assert T._args(T.BY_NAME["connect_nets"], {"move": "false"})["move"] is False
    assert T._args(T.BY_NAME["set_port_order"], {"symbol": "False"})["symbol"] is False
    assert T._args(T.BY_NAME["sync_symbol"], {"regenerate": "true"})["regenerate"] is True
    assert T._args(T.BY_NAME["get_design_context"], {"overview": "false"})["overview"] is False
    assert T._args(T.BY_NAME["batch"], {"dry_run": "true", "ops": []})["dry_run"] is True
    assert T._args(T.BY_NAME["add_instances"], {"instances": [{"symbol": "a.sym", "flip": "false"}]})["instances"][0]["flip"] is False
    assert T._args(T.BY_NAME["import_layout"], {"replace": "true"})["replace"] is True      # true, not the cell 'true'
    assert T._args(T.BY_NAME["import_layout"], {"replace": "c1"})["replace"] == ["c1"]
    assert T._args(T.BY_NAME["check_specs"], {"fresh": True})["fresh"] is True
    with pytest.raises(T.ToolError, match=r"move is a string \('no'\), not true or false: connect_nets takes move: boolean"):
        T._args(T.BY_NAME["connect_nets"], {"move": "no"})
    root, srv, _ = tc.setup()
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    T.call(ctx, "open_changeset", {"title": "t", "reason": "r"})
    text, err = T.call(ctx, "batch", {"ops": [{"tool": "get_netlist", "args": {"lib": "l", "cell": "top", "all_params": "yes"}}]})
    assert err and "ops[0] get_netlist: all_params is a string ('yes'), not true or false" in text
    shutil.rmtree(root)


def test_get_netlist_pages_filters_and_shows_code_blocks():
    root, srv, ctx = _big()
    L = {"lib": "l", "cell": "big"}
    whole = json.loads(T.call(ctx, "get_netlist", L, None)[0])
    assert len(whole["instances"]) == 70 and "_truncated" not in whole
    assert whole["instances"]["M3"]["params"] == {"W": "2", "model": "n"}               # formula defaults left out
    assert set(json.loads(T.call(ctx, "get_netlist", {**L, "all_params": True}, None)[0])["instances"]["M3"]["params"]) >= {"ad", "pd"}
    assert whole["code"] == [{"name": "MEAS", "symbol": "code.sym", "value": MEAS}]       # the testbench's .meas / .control
    text, err = T.call(ctx, "get_netlist", {**L, "all_params": True})                     # the in-app budget
    out = json.loads(text)
    assert not err and len(text) <= T.RESULT_CHARS and out["_truncated"]["cut"]["instances"].endswith("of 70")
    assert "offset" in out["_truncated"]["how"]
    page = json.loads(T.call(ctx, "get_netlist", {**L, "offset": 60, "limit": 8})[0])
    assert list(page["instances"]) == [f"M{i}" for i in range(60, 68)] and "code" not in page
    assert page["shown"] == {"instances": 8, "of": 70, "offset": 60, "next_offset": 68}
    last = json.loads(T.call(ctx, "get_netlist", {**L, "offset": 68, "limit": 8})[0])
    assert list(last["instances"]) == ["M68", "M69"] and "next_offset" not in last["shown"]
    on = json.loads(T.call(ctx, "get_netlist", {**L, "nets": "n3"})[0])                 # a lone net from the shell
    assert list(on["instances"]) == ["M3", "M4"] and on["nets"] == {"n3": ["M3:D", "M4:S"]}
    named = json.loads(T.call(ctx, "get_netlist", {**L, "instances": ["M5", "MEAS", "NOPE"]})[0])
    assert list(named["instances"]) == ["M5"] and named["code"][0]["name"] == "MEAS" and named["not_found"] == ["NOPE"]
    assert T.call(ctx, "get_netlist", {**L, "offset": "x"})[1]
    shutil.rmtree(root)


def test_get_design_context_puts_the_objects_first_and_takes_plain_names():
    root, srv, ctx = _big()
    text, err = T.call(ctx, "get_design_context", {"lib": "l", "cell": "big", "objects": ["M3", "n3", "MEAS", "zzz",
                                                                                         {"kind": "instance", "id": "M4"}]})
    assert not err
    asked = text.index("## Objects asked for")
    assert asked < text.index("## Design intent of big")                                  # before the intent summary
    assert "- instance M3:" in text and "- net n3: pins M3.D, M4.S" in text and "- instance M4:" in text
    assert "- code block MEAS (code.sym), its text now:\n    .meas tran tlast FIND v(n3) AT=1u" in text
    assert "- zzz: no instance, code block or net of this name in big" in text
    text, err = T.call(ctx, "get_design_context", {"lib": "l", "cell": "big", "objects": [{"kind": "instance"}]})
    assert err and "give names" in text
    text, err = T.call(ctx, "get_design_context", {"lib": "l", "cell": "big", "objects": [["M3"]]})
    assert err and "objects[0] is a list" in text and "internal error" not in text
    shutil.rmtree(root)


def test_get_intent_gives_the_items_asked_for():
    root, srv, _ = tc.setup()
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    L = {"lib": "l", "cell": "top"}
    T.call(ctx, "open_changeset", {"title": "intent", "reason": "r"})
    for i in range(12):
        assert not T.call(ctx, "add_intent", {**L, "kind": "spec" if i % 2 else "goal", "text": f"item {i} " + "x" * 900,
                                              "reason": "r"})[1]
    assert not T.call(ctx, "add_intent", {**L, "kind": "constraint", "text": "R9 stays", "view": "schematic",
                                          "rule": {"type": "place", "instances": ["R9"]}, "reason": "r"})[1]
    whole = json.loads(T.call(ctx, "get_intent", L, None)[0])
    ids = {it["kind"]: [] for it in whole["intent"]["items"]}
    for it in whole["intent"]["items"]:
        ids[it["kind"]].append(it["id"])
    c = whole["intent"]["views"]["schematic"]["items"][0]["id"]
    out = json.loads(T.call(ctx, "get_intent", {**L, "kinds": ["spec"]})[0])
    assert [it["id"] for it in out["intent"]["items"]] == ids["spec"] and "views" not in out["intent"]
    out = json.loads(T.call(ctx, "get_intent", {**L, "ids": [ids["goal"][0], c, "nope"]})[0])
    assert [it["id"] for it in out["intent"]["items"]] == [ids["goal"][0]] and out["not_found"] == ["nope"]
    assert [it["id"] for it in out["intent"]["views"]["schematic"]["items"]] == [c]
    assert {p["entry"].split(".")[-1] for p in out["problems"]} == {c}                    # R9 is not in the schematic
    assert "_truncated" in json.loads(T.call(ctx, "get_intent", L)[0])                   # all 13: over the in-app budget
    text, err = T.call(ctx, "get_intent", {**L, "kinds": ["specs"]})
    assert err and "goal, preference, constraint, spec, context" in text
    shutil.rmtree(root)


def _shell(argv, http, tmp: Path, stdin=None):
    os.environ["UNDINE_CLI_SESSIONS"] = str(tmp / "sessions.json")
    ap = argparse.ArgumentParser()
    add_parsers(ap.add_subparsers(dest="cmd"))
    args = ap.parse_args(argv)
    out, err = io.StringIO(), io.StringIO()
    old, sys.stdin = sys.stdin, io.StringIO(stdin or "")
    try:
        code = {"call": main_call, "tools": main_tools}[args.cmd](args, http=http, out=out, err=err)
    finally:
        sys.stdin = old
    return code, out.getvalue(), err.getvalue()


def test_budgets_of_mcp_the_shell_and_python(tmp_path, monkeypatch):
    root, srv, ctx = _big()
    srv.mcp_enabled = True
    mcp.ensure_token(srv.store.root)
    http = transport(TestClient(create_app(srv, None)))
    url = ["--url", "http://undine.test", "--project", "p"]
    code, out, err = _shell(["call", "open_changeset", f"id={ctx.changeset}", *url], http, tmp_path)
    assert code == 0, err
    (tmp_path / "args.json").write_text(json.dumps({"lib": "l", "cell": "big"}))
    (tmp_path / "nets.json").write_text('["n3", "n4"]')
    # the shell: what it prints fits --max-chars and parses; 0 prints all of it; key:=@file and --json @file
    code, out, err = _shell(["call", "get_netlist", "--json", f"@{tmp_path / 'args.json'}", "--max-chars", "6000", *url], http, tmp_path)
    assert code == 0, err
    printed = json.loads(out)
    assert len(out) <= 6001 and printed["_truncated"]["max_chars"] == 6000 and "--max-chars 0" in printed["_truncated"]["how"]
    code, out, err = _shell(["call", "get_netlist", "lib=l", "cell=big", "--max-chars", "0", *url], http, tmp_path)
    assert len(json.loads(out)["instances"]) == 70
    code, out, err = _shell(["call", "get_netlist", "lib=l", "cell=big", f"nets:=@{tmp_path / 'nets.json'}", *url], http, tmp_path)
    assert code == 0 and list(json.loads(out)["nets"]) == ["n3", "n4"]
    code, out, err = _shell(["call", "get_netlist", "lib=l", "cell=big", f"nets:=@{tmp_path / 'none.json'}", *url], http, tmp_path)
    assert code == 2 and "cannot read" in err
    # the request id comes first on stderr, and sends a write again without acting twice
    code, out, err = _shell(["call", "set_instance_params", "lib=l", "cell=big", "instance=M1", 'params:={"W": "3"}',
                             "reason=x", *url], http, tmp_path)
    rid = err.splitlines()[0].removeprefix("undine: request id ")
    assert code == 0 and len(rid) == 32
    code, out2, err = _shell(["call", "set_instance_params", "lib=l", "cell=big", "instance=M1", 'params:={"W": "3"}',
                              "reason=x", "--request-id", rid, *url], http, tmp_path)
    assert code == 0 and out2 == out and err == f"undine: request id {rid}\n"
    kept = srv.project("p").path / "changesets"                                           # its result in a file of its own
    rec = json.loads((kept / "requests.jsonl").read_text().splitlines()[-1])
    assert rec["id"] == rid and "text" not in rec and json.loads((kept / "requests" / rec["file"]).read_text()) == json.loads(out)

    def free():                                                                            # the last client is done writing
        if (le := srv.leases.get(ctx.changeset)):
            srv.leases.release(ctx.changeset, le.owner)
    free()
    # an MCP client: its budget, or the one it asks for
    sessions = mcp.Sessions()
    auth = {"authorization": f"Bearer {mcp.ensure_token(srv.store.root)}"}
    _, _, hdr = mcp.handle(srv, sessions, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, auth)
    h = {**auth, "mcp-session-id": hdr["mcp-session-id"]}

    def call(meta=None, tool="get_netlist", **a):
        a = {"project": "p", **({"lib": "l", "cell": "big"} if tool == "get_netlist" else {}), **a}
        p = {"name": tool, "arguments": a, **({"_meta": meta} if meta else {})}
        return mcp.handle(srv, sessions, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": p}, h)[1]["result"]["content"][0]["text"]
    call(tool="open_changeset", id=ctx.changeset)
    text = call({"undine/max_chars": 3000})
    assert len(text) <= 3000 and json.loads(text)["_truncated"]["max_chars"] == 3000
    monkeypatch.setattr(mcp, "AUDIT_CHARS", 3000)
    whole = call({"undine/max_chars": 0}, all_params=True)
    assert len(json.loads(whole)["instances"]) == 70
    audit = json.loads((srv.project("p").path / "llm" / "audit.jsonl").read_text().splitlines()[-1])["payload"]
    assert audit["result"] == whole[:3000] and audit["result_chars"] == len(whole) and len(audit["result_sha256"]) == 64
    assert [mcp.max_chars(p) for p in ({}, {"_meta": {"undine/max_chars": 0}}, {"_meta": {"undine/max_chars": 10}},
                                       {"_meta": {"undine/max_chars": True}})] == [mcp.RESULT_CHARS, None, 1000, mcp.RESULT_CHARS]
    assert T.RESULT_CHARS < mcp.RESULT_CHARS                                               # the in-app assistant gets less
    # Python: whole results, unless it asks for a budget
    free()
    with Undine("p", "http://undine.test", http=http) as u:
        u.open_changeset(id=ctx.changeset)
        assert len(u.get_netlist(lib="l", cell="big", all_params=True)["instances"]) == 70 and len(u.last_request_id) == 32
    free()
    with Undine("p", "http://undine.test", http=http, max_chars=4000) as u:
        u.open_changeset(id=ctx.changeset)
        assert "_truncated" in u.get_netlist(lib="l", cell="big")
    shutil.rmtree(root)


def test_a_result_kept_for_a_retry_is_a_file_of_its_own(tmp_path, monkeypatch):
    """The list searched on every write stays small however long the results (a client taking them whole); a result
    over KEPT_CHARS is kept shortened and says so; the oldest go with their files."""
    from undine.server import idempotency
    monkeypatch.setattr(idempotency, "KEEP", 5)
    big = results.compact({"results": [_netlist(800), _netlist(800, 800)]})
    assert len(big) > idempotency.KEPT_CHARS
    assert idempotency.once(tmp_path, "r0", "batch", {}, lambda: (big, False)) == (big, False, False)
    listed = tmp_path / "changesets" / "requests.jsonl"
    assert len(listed.read_text()) < 300
    text, err, replayed = idempotency.once(tmp_path, "r0", "batch", {}, lambda: ("acted twice", False))
    out = json.loads(text)
    assert replayed and not err and len(text) <= idempotency.KEPT_CHARS and len(out["results"]) == 2
    assert "kept shortened for a retry" in out["_truncated"]["how"]
    for i in range(1, 8):
        assert idempotency.once(tmp_path, f"r{i}", "t", {}, lambda: (f"done {i}", False))[0] == f"done {i}"
    names = [json.loads(line)["file"] for line in listed.read_text().splitlines()]
    assert len(names) <= 6 and sorted(names) == sorted(p.name for p in (tmp_path / "changesets" / "requests").glob("*"))
    assert idempotency.once(tmp_path, "r7", "t", {}, lambda: ("again", False)) == ("done 7", False, True)
    assert idempotency.once(tmp_path, "r0", "batch", {}, lambda: ("ran", False)) == ("ran", False, False)   # gone: runs
    with listed.open("a") as f:                                                          # kept in the list, as before
        f.write(json.dumps({"id": "old", "tool": "t", "fp": idempotency._fingerprint("t", {}), "text": "first"}) + "\n")
    assert idempotency.once(tmp_path, "old", "t", {}, lambda: ("x", False)) == ("first", False, True)


def _raw(path: Path, t: np.ndarray, vectors: dict[str, np.ndarray]) -> Path:
    """A binary ngspice raw file of a transient."""
    names = [("time", "time"), *((n, "voltage") for n in vectors)]
    head = (f"Title: t\nDate: x\nPlotname: Transient Analysis\nFlags: real\nNo. Variables: {len(names)}\nNo. Points: {len(t)}\n"
            "Variables:\n" + "".join(f"\t{i}\t{n}\t{k}\n" for i, (n, k) in enumerate(names)) + "Binary:\n")
    data = np.column_stack([t, *vectors.values()]).astype("<f8")
    path.write_bytes(head.encode() + data.tobytes())
    return path


def test_waveforms_in_a_window_keep_a_narrow_spike_and_samples_are_exact(tmp_path):
    t = np.linspace(0, 3.3e-6, 330001)                       # 10 ps steps
    y = np.full_like(t, 0.9)
    y[39070:39074] = 2.5                                      # a 40 ps overstress at 390.7 ns
    raw = _raw(tmp_path / "out.raw", t, {"v(top)": y, "v(x1.b9)": 0.5 + 0.1 * np.sin(2e7 * t),
                                          "v(m.x1.xm1.msky130#body)": y})
    whole = sim.waveform_json(raw, ["v(top)"], envelope=True)
    assert max(whole["traces"]["v(top)"]["y"]) == 2.5 and len(whole["x"]["values"]) <= 3000   # the spike survives
    assert max(sim.waveform_json(raw, ["v(top)"])["traces"]["v(top)"]["y"]) == 0.9               # every k-th point misses it
    w = sim.waveform_json(raw, ["v(top)"], window=(390e-9, 392e-9), envelope=True)
    xs = w["x"]["values"]
    assert xs[0] == pytest.approx(390e-9) and xs[-1] == pytest.approx(392e-9) and 201 <= len(xs) <= 203
    assert max(w["traces"]["v(top)"]["y"]) == 2.5
    with pytest.raises(ValueError, match="nothing of it"):
        sim.waveform_json(raw, ["v(top)"], window=(4e-6, 5e-6))
    assert sim.raw_vectors(raw) == sim.read_raw(raw)["vars"]                               # from the header alone
    # mapped into memory, not read whole, and parsed once for the readers of one run (each has its own dict)
    a, b = sim.read_raw(raw), sim.read_raw(raw)
    assert isinstance(a["data"]["v(top)"], np.memmap) and a["data"]["v(top)"] is b["data"]["v(top)"]
    a["data"]["point"] = np.zeros(3)
    assert "point" not in sim.read_raw(raw)["data"] and not a["data"]["v(top)"].flags.writeable
    (tmp_path / "x").mkdir()                                                               # not the job's raw files
    other = _raw(tmp_path / "x" / "other.raw", t[:5], {"v(top)": y[:5]})
    _raw(other, t[:4], {"v(top)": np.arange(4.0)})                                        # written again: read again
    assert sim.read_raw(other)["data"]["v(top)"].tolist() == [0, 1, 2, 3]
    asc = tmp_path / "x" / "ascii.raw"
    asc.write_text("Title: t\nPlotname: Transient Analysis\nFlags: real\nNo. Variables: 2\nNo. Points: 2\nVariables:\n"
                   "\t0\ttime\ttime\n\t1\tv(a)\tvoltage\nValues:\n 0\t0.0\n\t1.5\n 1\t1e-9\n\t2.5\n")
    assert sim.read_raw(asc)["data"]["v(a)"].tolist() == [1.5, 2.5] and sim.raw_vectors(asc)[1] == ("v(a)", "voltage")
    (tmp_path / "x" / "junk.raw").write_text("not a raw file")
    assert sim.raw_vectors(tmp_path / "x" / "junk.raw") == []
    names = [n for n, _ in sim.read_raw(raw)["vars"][1:]]
    assert simresults.own_first(names) == ["v(top)", "v(x1.b9)", "v(m.x1.xm1.msky130#body)"]
    assert simresults.select(names, ["top", "X1.B9", "v(nope)"]) == (["v(top)", "v(x1.b9)"], ["v(nope)"])
    req = simresults.sample_config({"signals": "x1.b9", "start": "1u", "fs": "100Meg", "points": 4})
    assert req["times"] == [1e-6 + k / 1e8 for k in range(4)]
    job = sim.Job(tmp_path)
    s = simresults.samples_of(job, req)
    exact = np.interp(np.array(req["times"]), t, 0.5 + 0.1 * np.sin(2e7 * t))
    assert s["columns"] == ["time", "v(x1.b9)"] and [r[1] for r in s["rows"]] == exact.tolist()   # not rounded
    assert s["on_time_points"] == 4
    assert "error" in simresults.samples_of(job, simresults.sample_config({"signals": ["top"], "times": [5e-6]}))
    # a transient with tstart keeps its results from there on: refused before the run, and named after it
    tran = {"type": "tran", "step": "1n", "stop": "8u", "tstart": "4u"}
    why = simresults.sample_check(simresults.sample_config({"signals": ["out"], "times": [1e-6, 5e-6]}), tran)
    assert "keeps its results from tstart 4e-06 s, after the first time, 1e-06 s" in why and "make tstart at most" in why
    assert simresults.sample_check(simresults.sample_config({"signals": ["out"], "times": ["4u", "5u"]}), tran) is None
    assert "after the transient's stop" in simresults.sample_check({"signals": ["out"], "times": [1e-6, 9e-6]}, tran)
    (tmp_path / "late").mkdir()
    _raw(tmp_path / "late" / "out.raw", np.linspace(4e-6, 8e-6, 401), {"v(top)": np.linspace(0, 1, 401)})
    late = simresults.samples_of(sim.Job(tmp_path / "late"), {"signals": ["top"], "times": [1e-6, 5e-6]})
    assert "keeps its results from 4e-06 s (the transient's tstart)" in late["error"]
    assert "make tstart earlier than the first sample time" in late["error"]
    for bad in ({"signals": ["a"]}, {"signals": ["a"], "times": [1], "points": 3}, {"signals": [], "times": [1]},
                {"signals": ["a"], "fs": "1G", "points": 0}, {"signals": ["a"], "every": 1}):
        with pytest.raises(ValueError, match="sample"):
            simresults.sample_config(bad)


def test_plot_and_sample_a_finished_run_by_plain_names(tmp_path):
    root, srv, _ = tc.setup()
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    prj = srv.project("p")
    job = srv.jobs.runs_dir(prj.path) / "20260101-000000-abcd"
    job.mkdir(parents=True)
    t = np.linspace(0, 1e-6, 1001)
    _raw(job / "out.raw", t, {"v(top)": t * 1e6, "v(x1.b9)": 1 - t * 1e6, "v(m.x1.xm1.msky130#body)": t})
    sim.Job(job).set(id=job.name, kind="sim", lib="l", cell="top", state="done", created=0)
    text, err = T.call(ctx, "plot_waveform", {"lib": "l", "cell": "top", "signals": ["top", "x1.b9", "gone"],
                                              "tstart": "100n", "tstop": "200n"})
    out = json.loads(text)
    assert not err and [tr["signal"] for tr in out["traces"]] == ["v(top)", "v(x1.b9)"] and out["missing_signals"] == ["gone"]
    assert out["x"]["from"] == pytest.approx(1e-7) and out["x"]["to"] == pytest.approx(2e-7) and ctx.images
    text, err = T.call(ctx, "plot_waveform", {"lib": "l", "cell": "top", "signals": ["nope"]})
    assert err and text.index("v(top)") < text.index("v(x1.b9)") < text.index("#body")      # the testbench's nets first
    assert "tstart is a number" in T.call(ctx, "plot_waveform", {"lib": "l", "cell": "top", "tstart": "soon"})[0]
    assert "nothing of it" in T.call(ctx, "plot_waveform", {"lib": "l", "cell": "top", "tstart": "2u", "tstop": "3u"})[0]
    text, err = T.call(ctx, "sample_waveform", {"lib": "l", "cell": "top", "signals": "top", "start": "0.1u", "fs": "100Meg",
                                                "points": 3})
    rows = json.loads(text)["rows"]
    assert not err and [r[1] for r in rows] == pytest.approx([0.1, 0.11, 0.12], rel=1e-12)
    text, err = T.call(ctx, "sample_waveform", {"lib": "l", "cell": "top", "signals": ["nope"], "times": [1e-7]})
    assert err and "no signal nope" in text and "v(top)" in text
    # a job named by id is found among all runs, as run_simulation(job=) finds it: check_specs and sweep name runs that
    # a mismatch check (up to 120 runs in one call) has pushed out of the newest 100
    for i in range(100):
        d = srv.jobs.runs_dir(prj.path) / f"20260102-000000-{i:04d}"
        d.mkdir()
        sim.Job(d).set(id=d.name, kind="sim", lib="l", cell="top", state="done", created=1)
    assert job.name not in [j["id"] for j in srv.jobs.list(prj.path)]
    text, err = T.call(ctx, "sample_waveform", {"lib": "l", "cell": "top", "job": job.name, "signals": "top", "times": [5e-7]})
    assert not err and json.loads(text)["job"] == job.name and json.loads(text)["rows"][0][1] == pytest.approx(0.5)
    text, err = T.call(ctx, "plot_waveform", {"lib": "l", "cell": "top", "job": job.name, "signals": "top"})
    assert not err and json.loads(text)["job"] == job.name
    text, err = T.call(ctx, "plot_waveform", {"lib": "l", "cell": "other", "job": job.name})
    assert err and "is not a simulation of l/other" in text
    text, err = T.call(ctx, "plot_waveform", {"lib": "l", "cell": "top", "job": "20260103-000000-none"})
    assert err and "no job 20260103-000000-none" in text
    left = srv.jobs.runs_dir(prj.path) / "20260101-000000-left"                    # an earlier server's run: never finishes
    left.mkdir()
    sim.Job(left).set(id=left.name, kind="sim", lib="l", cell="top", state="running", created=0)
    text, err = T.call(ctx, "sample_waveform", {"lib": "l", "cell": "top", "job": left.name, "signals": "top", "times": [5e-7]})
    assert err and "did not finish (interrupted" in text and f"run_simulation(job='{left.name}')" in text
    shutil.rmtree(root)


def test_run_simulation_returns_samples_at_full_precision():
    """512 samples of one run, not hundreds of .meas lines rounded to 6 digits: what an independent FFT needs."""
    import test_simresults as ts
    ts._need()
    root, srv = ts._project()
    ctx = ts._tb(srv, "rcs", [("devices/vsource.sym", 0, {"name": "V1", "value": "sin(0.9 0.5 1.953125Meg)"}),
                              ("devices/res.sym", 100, {"name": "R1", "value": "1k"}),
                              ("devices/capa.sym", 200, {"name": "C1", "value": "10p"})],
                 {"in": ["V1:p", "R1:P"], "out": ["R1:M", "C1:p"], "0": ["V1:m", "C1:m"]})
    sample = {"signals": ["out", "in"], "start": "1u", "fs": "50Meg", "points": 512}
    sim._parse_raw.cache_clear()
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "rcs", "analysis": {"type": "tran", "step": "1n", "stop": "11.3u"},
                                             "sample": sample, "spectrum": {"signal": "out", "fs": "50Meg", "points": 512,
                                                                           "start": "1u"}}, None)
    assert not err, res
    assert sim._parse_raw.cache_info().misses == 1                                      # summary, spectrum, samples: one read
    out = json.loads(res)
    s = out["samples"]
    assert s["columns"] == ["time", "v(out)", "v(in)"] and len(s["rows"]) == 512 and s["rows"][1][0] == 1e-6 + 1 / 50e6
    job = srv.jobs.get(srv.project(ctx.work).path, out["job"])
    raw = sim.read_raw(job.raw_files()[0])
    t = np.real(raw["data"]["time"])
    want = np.interp(np.array([r[0] for r in s["rows"]]), t, np.real(raw["data"]["v(out)"]))
    assert [r[1] for r in s["rows"]] == want.tolist()                                    # as stored, not rounded
    assert any(len(repr(r[1]).replace("-", "").replace(".", "").lstrip("0")) > 8 for r in s["rows"])
    again = json.loads(T.call(ctx, "sample_waveform", {"lib": "l", "cell": "rcs", "job": out["job"], **sample}, None)[0])
    assert again["rows"] == s["rows"] and again["job"] == out["job"]                    # a finished run, not run again
    pic = json.loads(T.call(ctx, "plot_waveform", {"lib": "l", "cell": "rcs", "signals": "out", "tstart": "2u", "tstop": "2.1u"})[0])
    assert [tr["signal"] for tr in pic["traces"]] == ["v(out)"] and pic["x"]["from"] == pytest.approx(2e-6)
    text, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "rcs", "sample": {"signals": ["out"], "fs": "1G"}})
    assert err and "sample: points" in text
    for analysis, why in (({"type": "tran", "stop": "5u"}, "after the transient's stop"), ({"type": "op"}, "transient"),
                          ({"type": "tran", "step": "1n", "stop": "11.3u", "tstart": "2u"}, "make tstart at most")):
        text, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "rcs", "analysis": analysis, "sample": sample})
        assert err and why in text                                                        # refused before running
    shutil.rmtree(root)


def test_a_large_schematic_is_drawn_closer_around_instances():
    root, srv, ctx = _big(20)
    whole = json.loads(T.call(ctx, "render_schematic", {"lib": "l", "cell": "big", "size": 400})[0])
    near = json.loads(T.call(ctx, "render_schematic", {"lib": "l", "cell": "big", "around": ["M11", "M12"], "size": 400})[0])
    assert near["units_per_px"] < whole["units_per_px"] / 3
    bx = near["box"]
    assert bx[0] < 200 < 400 < bx[2] and bx[1] < 300 < bx[3]                               # M11 at (200, 300), M12 at (400, 300)
    box = json.loads(T.call(ctx, "render_schematic", {"lib": "l", "cell": "big", "box": [0, 0, 100, 50]})[0])
    assert box["box"] == [0, 0, 100, 50] and box["image"].startswith("1024x512")
    text, err = T.call(ctx, "render_schematic", {"lib": "l", "cell": "big", "around": ["M99"]})
    assert err and "no instance M99" in text
    shutil.rmtree(root)


def test_tools_that_tell_less_of_a_large_result_follow_the_callers_budget(monkeypatch):
    """check_specs leaves out its last per-point blocks and get_drawing tells a large drawing in less detail to fit the
    budget of the call (the in-app 9000, an MCP client's 20000), not at all for a client that takes whole results (the
    shell and Python clients). delete_objects takes the wire ids get_drawing gives as plain strings, as it takes names."""
    from undine.simulation import specs
    root, srv, ctx = _big()
    L = {"lib": "l", "cell": "big"}
    whole = json.loads(T.call(ctx, "get_drawing", L, None)[0])
    assert not whole["note"].startswith("large drawing") and {f"M{i}" for i in range(70)} <= set(whole["instances"])
    assert all(isinstance(n["wires"], list) for n in whole["nets"].values())
    text, err = T.call(ctx, "get_drawing", L, 3000)
    assert not err and len(text) <= 3000 and json.loads(text)["note"].startswith("large drawing")
    assert not T.call(ctx, "add_wire", {**L, "x1": -1000, "y1": -1000, "x2": -900, "y2": -1000, "reason": "x"})[1]
    out, err = T.call(ctx, "delete_objects", {**L, "objects": ["-1000,-1000,-900,-1000"], "reason": "x"})
    assert not err and json.loads(out)["deleted"] == 1
    probes = [{"id": f"s{i}", "text": f"spec number {i} of the cell", "measure": f"m{i}", "min": None, "max": 1.0, "lib": "l",
               "testbench": "tb", "at": {}} for i in range(10)]
    rows = [{"point": {"corner": c, "temp": float(t)},
             "results": {p["id"]: {"value": 0.5, **specs.verdict(0.5, None, 1.0), "job": f"j-{c}-{t}"} for p in probes},
             "runs": [{"job": f"j-{c}-{t}", "testbench": "tb"}]} for c in ("tt", "ss", "ff") for t in range(60)]
    res = {"rows": rows, "failures": [], "pending": [], "mismatch": None, "cut": None}
    monkeypatch.setattr(T, "_measure", lambda c, a, vary: (probes, [r["point"] for r in rows], res, []))
    shown = {}
    for limit in (T.RESULT_CHARS, mcp.RESULT_CHARS, None):
        text, err = T.call(ctx, "check_specs", L, limit)
        out = json.loads(text)
        assert not err and "_truncated" not in out and (limit is None or len(text) <= limit)
        shown[limit] = len(out["points"])
        assert ("points_not_shown" in out) == (shown[limit] < 180)
    assert shown[T.RESULT_CHARS] < shown[mcp.RESULT_CHARS] < shown[None] == 180
    shutil.rmtree(root)
