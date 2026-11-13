"""Design intent (intent.py): the file, its editor with undo / redo, checks against the schematic, what each flow
step is missing, the HTTP API, and changesets (adopt and revert)."""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pytest  # noqa: E402
import yaml  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from undine.design import intent  # noqa: E402
from undine.server.api import create_app  # noqa: E402
from undine.store.oplog import OpLog  # noqa: E402
import test_changeset as tc  # noqa: E402


@pytest.fixture
def ed(tmp_path):
    return intent.IntentEditor(tmp_path / f"amp{intent.SUFFIX}", OpLog(tmp_path / "history.jsonl"))


def test_items_ports_supplies_notes_with_undo_and_no_empty_file(ed):
    g = ed.add_item({"kind": "goal", "text": "Minimise the input offset", "reason": "ADC accuracy"}, "user")
    c = ed.add_item({"kind": "constraint", "text": "M1 and M2 symmetric", "serves": [g],
                     "rule": {"type": "symmetric", "instances": ["M1", "M2"]}}, "agent:t")
    ed.set_port("inp", {"kind": "signal_in", "side": "W"})
    ed.set_supply("vdd", "supply")
    ed.set_notes("Drives 2 pF.")
    m = ed.model()
    assert (g, c) == ("g1", "c1") and m["items"][1]["strength"] == "must"          # constraints default to must
    assert m["items"][1]["source"] == {"by": "agent:t"} and m["ports"]["inp"]["side"] == "W"
    ed.remove_item(g)
    assert "serves" not in ed.model()["items"][0]                                   # the dangling link goes too
    for _ in range(6):
        assert ed.undo()
    assert not ed.undo() and not ed.path.exists()                                   # all undone: no file left
    assert ed.redo() and ed.model()["items"][0]["text"] == "Minimise the input offset"
    with pytest.raises(ValueError, match="kind is one of"):
        ed.add_item({"kind": "wish", "text": "x"})
    with pytest.raises(ValueError, match="side"):
        ed.set_port("inp", {"side": "up"})


def test_ids_continue_after_the_highest_and_never_fill_a_gap(ed):
    """A deleted id is not handed out again: a git merge would take the other branch's edits of the deleted item for
    edits of the new one (vcs/yamlmerge.py)."""
    assert [ed.add_item({"kind": "constraint", "text": t}) for t in ("a", "b", "c")] == ["c1", "c2", "c3"]
    ed.remove_item("c1")
    assert ed.add_item({"kind": "constraint", "text": "d"}, view="layout") == "c4"     # numbered across views
    assert ed.add_item({"kind": "goal", "text": "g"}) == "g1"                          # per prefix
    ed.remove_item("c4")
    assert ed.add_item({"kind": "constraint", "text": "e"}) == "c4"     # the highest: nothing in the file remembers it
    m = intent.normalize({"items": [{"id": "c10", "kind": "constraint", "text": "x"},
                                    {"id": "cx", "kind": "constraint", "text": "y"}]})
    assert intent.next_id(m, "constraint") == "c11"


def test_yaml_replace_is_recorded_entry_by_entry(ed):
    ed.add_item({"kind": "goal", "text": "low power"})
    ed.replace("items:\n  - {id: g1, kind: goal, text: lower power}\n  - {id: s1, kind: spec, text: gain,"
               " spec: {measure: dc_gain_db, min: 60, testbench: tb_ac}}\nsupplies: {vss: ground}\n")
    rec = ed.log.read(view="intent")[-1]
    assert sorted((e["id"], e["do"]) for e in rec["effects"]) == [("items.g1", "mod"), ("items.s1", "add"), ("supplies.vss", "add")]
    with pytest.raises(ValueError, match="unique id"):
        ed.replace("items:\n  - {id: g1, kind: goal, text: a}\n  - {id: g1, kind: goal, text: b}\n")
    with pytest.raises(ValueError, match="YAML"):
        ed.replace("items: [")


def test_problems_and_step_requirements_against_the_design():
    refs = {"instances": ["M1", "M2"], "nets": ["out", "vdd", "vss"], "ports": ["inp", "out", "vdd", "vss"], "cells": ["tb_ac"]}
    m = intent.normalize({"ports": {"inp": {"kind": "signal_in", "side": "W"}, "gone": {"kind": "signal_in"}},
                          "supplies": {"vdd": "supply"},
                          "items": [{"id": "c1", "kind": "constraint", "text": "match", "rule": {"type": "match", "instances": ["M1", "M9"]}},
                                    {"id": "s1", "kind": "spec", "text": "gain", "spec": {"measure": "dc_gain_db", "min": 60}}]})
    probs = {(p["entry"], p["message"]) for p in intent.validate(m, refs)}
    assert ("ports.gone", "the schematic has no port gone") in probs
    assert ("items.c1", "the schematic has no instance M9") in probs
    need = intent.requirements(m, refs)
    assert [n["message"] for n in need["simulation"]] == ["which testbench measures dc_gain_db"]
    layout = [n["message"] for n in need["layout"]]
    assert "the side of port out (or auto)" in layout and "which net is ground" in layout
    assert "the side of port inp (or auto)" not in layout
    assert intent.suggested_supplies(refs) == {"vdd": "supply", "vss": "ground"}
    text = intent.summary(m)
    assert "c1 constraint [must]: match" in text and "port inp: signal_in, side W" in text


def test_http_api_and_changeset_adopt_and_revert():
    root, srv, cs = tc.setup()
    client = TestClient(create_app(srv, None))
    meta = cs.create("p", "Intent", "capture the goals", "agent:t")
    P = meta["preview"]
    url = f"/api/p/{P}/l/l/c/top/intent"
    r = client.post(url + "/edit", json={"op": "add_item", "reason": "from the request",
                                         "item": {"kind": "goal", "text": "Keep R1 cool", "source": {"request": "keep it cool"}}},
                    headers={"x-undine-actor": "agent:t"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"] == "g1" and body["intent"]["items"][0]["source"] == {"by": "agent:t", "request": "keep it cool"}
    assert body["refs"]["instances"] == ["R1"]
    assert client.get("/api/p/p/l/l/c/top/intent").json()["intent"]["items"] == []     # the real design is untouched
    assert client.post(f"/api/p/{P}~base/l/l/c/top/intent/edit", json={"op": "set_notes", "text": "x"}).status_code == 403
    cs.apply("p", meta["id"], "user")
    real = srv.intent_editor("p", "l", "top")
    assert real.model()["items"][0]["text"] == "Keep R1 cool"
    # the intent file is one the changeset added: a revert keeps it as it is (docs/design/changeset-branches.md,
    # Revert: the files a changeset added are kept)
    cs.revert("p", meta["id"], "user")
    assert real.path.exists() and real.model()["items"][0]["text"] == "Keep R1 cool"
    assert "libs/l/top.intent.yaml" in cs.get("p", meta["id"])["reverted"]["kept"]
    shutil.rmtree(root)


def test_agent_reads_the_intent_in_its_context_and_records_requests():
    import json
    from undine.agents import agent_tools as T
    root, srv, cs = tc.setup()
    ctx = T.ToolCtx(srv, "p", actor="agent:t", turn="t-1")
    L = {"lib": "l", "cell": "top"}
    text = T.call(ctx, "get_design_context", L)[0]
    assert "## Design intent of top" in text and "No design intent is recorded" in text
    T.call(ctx, "open_changeset", {"title": "intent", "reason": "r"})
    res, err = T.call(ctx, "add_intent", {**L, "kind": "constraint", "text": "R1 stays at 1k", "why": "sets the bias",
                                         "rule": {"type": "place", "instances": ["R1"]}, "request": "R1 must stay 1k",
                                         "reason": "the user said so"})
    assert not err, res
    model = srv.intent_editor(ctx.work, "l", "top").model()
    assert model["items"][0]["source"] == {"by": "agent:t", "request": "R1 must stay 1k", "turn": "t-1"}
    assert model["items"][0]["strength"] == "must" and model["items"][0]["reason"] == "sets the bias"
    res, err = T.call(ctx, "add_intent", {**L, "kind": "goal", "text": "x"})
    assert err and "reason is required" in res
    res, err = T.call(ctx, "set_port_intent", {**L, "port": "out", "side": "auto", "reason": "no preference"})
    assert not err and json.loads(res)["decided_by"] == "agent:t"
    res, err = T.call(ctx, "set_instance_params", {**L, "instance": "R1", "params": {"value": "2k"}, "reason": "try"})
    assert not err and "c1 constraint [must]: R1 stays at 1k (why: sets the bias)" in res     # the tool reminds of the intent
    text = T.call(ctx, "get_design_context", L)[0]
    assert "c1 constraint [must]: R1 stays at 1k" in text and 'from the request "R1 must stay 1k"' in text
    v = json.loads(T.call(ctx, "get_intent", L)[0])
    assert v["intent"]["ports"]["out"]["side"] == "auto"
    assert not T.call(ctx, "remove_intent", {**L, "id": "c1", "reason": "agreed with the user"})[1]
    ops = [o["op"] for o in cs.get("p", ctx.changeset)["changes"][0]["ops"]]
    assert {"add_intent", "set_port_intent", "remove_intent"} <= set(ops)
    shutil.rmtree(root)


def test_a_users_later_words_are_added_to_an_items_request_history():
    """A user confirms or extends a decision later: update_intent's request adds their words, dated, after the words
    the item came from; it never replaces them, and get_intent and the design context show them all."""
    import json
    import time
    from undine.agents import agent_tools as T
    root, srv, cs = tc.setup()
    ctx = T.ToolCtx(srv, "p", actor="agent:t", turn="t-2")
    L = {"lib": "l", "cell": "top"}
    T.call(ctx, "open_changeset", {"title": "intent", "reason": "r"})
    T.call(ctx, "add_intent", {**L, "kind": "spec", "text": "DAC controls clear after clk", "request": "clear them on clk",
                               "spec": {"measure": "tclr", "max": 2e-9, "testbench": "tb"}, "reason": "the user said so"})
    res, err = T.call(ctx, "update_intent", {**L, "id": "s1", "reason": "the user confirmed it",
                                             "fields": {"request": "yes, and within 2 ns at every corner",
                                                        "spec": {"measure": "tclr", "max": 2e-9, "testbench": "tb",
                                                                 "corners": ["all"]}}})
    assert not err and "request history" in json.loads(res)["note"]
    today = time.strftime("%Y-%m-%d")
    first = {"request": "yes, and within 2 ns at every corner", "date": today, "by": "agent:t", "turn": "t-2"}
    item = json.loads(T.call(ctx, "get_intent", {**L, "ids": ["s1"]})[0])["intent"]["items"][0]
    assert item["source"] == {"by": "agent:t", "request": "clear them on clk", "turn": "t-2", "requests": [first]}
    assert item["spec"]["corners"] == ["all"]
    ctx.turn = None
    for words in ("yes, and within 2 ns at every corner", "  ", None):          # the same words, or none: nothing added
        res, err = T.call(ctx, "update_intent", {**L, "id": "s1", "reason": "r",
                                                 "fields": {"request": words, "text": "DAC controls clear"}})
        assert not err and ("nothing was added" in json.loads(res)["note"] if words else "note" not in json.loads(res))
    T.call(ctx, "update_intent", {**L, "id": "s1", "fields": {"request": "also at -40 C"}, "reason": "r"})
    src = srv.intent_editor(ctx.work, "l", "top").model()["items"][0]["source"]
    assert src["request"] == "clear them on clk" and src["requests"] == [first, {"request": "also at -40 C", "date": today,
                                                                               "by": "agent:t"}]
    assert T.call(ctx, "update_intent", {**L, "id": "s1", "fields": {"source": {}}, "reason": "r"})[1] is False
    assert srv.intent_editor(ctx.work, "l", "top").model()["items"][0]["source"] == src          # never replaced
    res, err = T.call(ctx, "update_intent", {**L, "id": "s1", "fields": {"request": ["not", "text"]}, "reason": "r"})
    assert err and "request is the user's words" in res
    text = T.call(ctx, "get_design_context", L)[0]
    assert f'from the request "clear them on clk" | then on {today} "yes, and within 2 ns at every corner" | ' \
           f'then on {today} "also at -40 C"' in text
    # a person's words through the HTTP API are theirs
    c = TestClient(create_app(srv, None))
    r = c.post(f"/api/p/p~{ctx.changeset}/l/l/c/top/intent/edit", json={"op": "update_item", "id": "s1", "fields": {"request": "ok"}},
               headers={"x-undine-actor": "alice"})
    assert r.status_code == 200, r.text
    assert r.json()["intent"]["items"][0]["source"]["requests"][-1] == {"request": "ok", "date": today, "by": "alice"}
    # a history written by hand in the YAML view, its date bare (which YAML reads as a date): kept as text, and an
    # entry without words left out, so that the log, get_intent and the next update can still write it
    yml = ("items:\n"
           "  - {id: s1, kind: spec, text: DAC controls clear, spec: {measure: tclr, max: 2.0e-09, testbench: tb},\n"
           "     source: {by: agent:t, request: clear them on clk,\n"
           "              requests: [{request: ok, date: 2026-11-03, by: alice}, stray, {date: 2026-11-04}]}}\n")
    r = c.post(f"/api/p/p~{ctx.changeset}/l/l/c/top/intent/edit", json={"op": "replace", "text": yml})
    assert r.status_code == 200, r.text
    res, err = T.call(ctx, "get_intent", {**L, "ids": ["s1"]})
    assert not err and json.loads(res)["intent"]["items"][0]["source"]["requests"] == \
        [{"request": "ok", "date": "2026-11-03", "by": "alice"}]
    res, err = T.call(ctx, "update_intent", {**L, "id": "s1", "fields": {"request": "more"}, "reason": "r"})
    assert not err and "request history" in json.loads(res)["note"], res
    said = srv.intent_editor(ctx.work, "l", "top").model()["items"][0]["source"]["requests"]
    assert [x["request"] for x in said] == ["ok", "more"]
    shutil.rmtree(root)


def test_view_intent_is_linked_to_the_cell_intent(ed):
    """The cell intent says what to achieve; a view intent says how that view does it, linked with serves."""
    g = ed.add_item({"kind": "goal", "text": "Minimise the input offset"}, "user")
    s = ed.add_item({"kind": "spec", "text": "DC gain >= 60 dB", "spec": {"measure": "dc_gain_db", "min": 60, "testbench": "tb_ac"}})
    c_sch = ed.add_item({"kind": "constraint", "text": "M1 and M2 equal and large", "serves": [g],
                         "rule": {"type": "match", "instances": ["M1", "M2"]}}, "user", view="schematic")
    c_lay = ed.add_item({"kind": "constraint", "text": "M1 and M2 in common centroid", "serves": [g]}, "user", view="layout")
    x_tb = ed.add_item({"kind": "context", "text": "Measure with the 2 pF load", "serves": [s]}, "user", view="test:tb_ac")
    assert (c_sch, c_lay, x_tb) == ("c1", "c2", "x1")                                    # ids unique in the whole file
    m = ed.model()
    assert [i["id"] for i in m["items"]] == ["g1", "s1"]
    assert {v: [i["id"] for i in sec["items"]] for v, sec in m["views"].items()} == \
        {"schematic": ["c1"], "layout": ["c2"], "test:tb_ac": ["x1"]}
    text = ed.path.read_text()
    assert "views:" in text and "test:tb_ac:" in text and intent.load(ed.path) == m      # written and read back
    # problems: a view constraint that serves nothing; a testbench that does not exist; a broken link
    ed.add_item({"kind": "constraint", "text": "Output pin on the right"}, "user", view="layout")
    design = {"instances": ["M1", "M2"], "nets": [], "ports": [], "cells": ["amp"]}
    probs = {(p["entry"], p["level"]) for p in intent.validate(ed.model(), design)}
    assert ("views.layout.items.c3", "info") in probs                                    # a constraint without a reason
    assert ("views.test:tb_ac.items.x1", "warning") in probs                             # no testbench cell tb_ac
    assert ("views.layout.items.c2", "info") not in probs                                # linked to g1
    ed.remove_item(g)                                                                    # links to it go with it
    assert "serves" not in ed.model()["views"]["layout"]["items"][0]
    assert "[layout view] c2 constraint" in intent.summary(ed.model())
    for _ in range(4):                                                                   # removal, c3, x1, c2
        assert ed.undo()                                                                 # view items undo like any
    assert "layout" not in ed.model()["views"] and ed.model()["views"]["schematic"]["items"][0]["id"] == "c1"   # an emptied view leaves the file
    with pytest.raises(ValueError, match="no view"):
        ed.add_item({"kind": "context", "text": "x"}, "user", view="netlist")


def test_a_file_with_views_round_trips_and_rejects_duplicates(tmp_path):
    raw = {"version": 1, "items": [{"id": "g1", "kind": "goal", "text": "low offset"}],
           "views": {"layout": {"items": [{"id": "g1", "kind": "constraint", "text": "dup"}]}}}
    with pytest.raises(ValueError, match="unique id"):
        intent.normalize(raw)
    raw["views"]["layout"]["items"][0]["id"] = "c1"
    m = intent.normalize(raw)
    assert intent.normalize(yaml.safe_load(intent.dump(m))) == m
    assert intent.apply(intent.empty(), intent.diff(intent.empty(), m), False) == m        # entries round trip
