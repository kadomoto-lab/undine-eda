"""Merge and diff of a cell's YAML views (vcs/yamlmerge.py): the netlist, where only pending connections conflict and
the parts the schematic decides only warn, and design intent, merged per entry and field, with the ids both sides
added renumbered."""
import sys
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from undine.design import intent  # noqa: E402
from undine.schematics import netlist  # noqa: E402
from undine.vcs import yamlmerge  # noqa: E402
from undine.vcs.yamlmerge import read_intent, read_netlist  # noqa: E402

PFET, NFET, RES = "sky130_fd_pr/pfet_01v8.sym", "sky130_fd_pr/nfet_01v8.sym", "devices/res.sym"


def inverter() -> dict:
    """A CMOS inverter, and a pull-down M3 whose drain and gate are not drawn yet (floating net1, net2)."""
    m = netlist.empty()
    m["ports"] = [{"name": n, "type": t} for n, t in (("A", "ipin"), ("Y", "opin"), ("VDD", "iopin"), ("VSS", "iopin"))]
    m["instances"] = {
        "M1": {"symbol": PFET, "props": {"L": "0.15", "W": "2", "model": "pfet_01v8", "nf": "1"},
               "pins": {"D": "Y", "G": "A", "S": "VDD", "B": "VDD"}},
        "M2": {"symbol": NFET, "props": {"L": "0.15", "W": "1", "model": "nfet_01v8", "nf": "1"},
               "pins": {"D": "Y", "G": "A", "S": "VSS", "B": "VSS"}},
        "M3": {"symbol": NFET, "props": {"L": "0.15", "W": "1", "model": "nfet_01v8", "nf": "1"},
               "pins": {"D": "net1", "G": "net2", "S": "VSS", "B": "VSS"}},
    }
    m["code"] = [{"name": "s1", "symbol": "devices/code_shown.sym", "props": {"value": ".tran 1n 1u"}}]
    return m


def connect(m: dict, key: str, net: str) -> dict:
    """A pending connection, as NetlistEditor.connect leaves the file: in pending and on the instance's pin."""
    inst, pin = key.split(".")
    m["pending"][key] = net
    m["instances"][inst]["pins"][pin] = net
    return m


def draw(m: dict, key: str, net: str) -> dict:
    """A pin drawn to a net in the schematic, as netlist.read_drawing reads it: on that net, no pending connection."""
    inst, pin = key.split(".")
    m["pending"].pop(key, None)
    m["instances"][inst]["pins"][pin] = net
    return m


def nl(m: dict) -> bytes:
    return netlist.dump(m).encode()


def amp() -> dict:
    """A differential pair's intent: cell items g1 p1 c1 s1, a layout constraint c2."""
    return intent.normalize({
        "notes": "Drives a 2 pF load inside the ADC.\nKeep the input offset below 1 mV.\n"
                 "Bias from the shared 10 uA mirror.\n",
        "ports": {"inp": {"kind": "signal_in", "side": "W"}, "inn": {"kind": "signal_in", "side": "W"},
                  "out": {"kind": "signal_out", "side": "E"}},
        "supplies": {"vdd": "supply", "vss": "ground"},
        "items": [
            {"id": "g1", "kind": "goal", "text": "Minimise the input offset", "source": {"by": "user"}},
            {"id": "p1", "kind": "preference", "text": "Performance over area", "source": {"by": "user"}},
            {"id": "c1", "kind": "constraint", "text": "M1 and M2 symmetric", "strength": "must", "serves": ["g1"],
             "rule": {"type": "symmetric", "instances": ["M1", "M2"], "style": "common_centroid"},
             "source": {"by": "agent:layout"}},
            {"id": "s1", "kind": "spec", "text": "DC gain at least 60 dB",
             "spec": {"measure": "dc_gain_db", "min": 60, "testbench": "tb_ac", "output": "out",
                      "analysis": {"type": "ac", "fstop": "1G"}}},
        ],
        "views": {"layout": {"items": [{"id": "c2", "kind": "constraint", "text": "M1 and M2 in common centroid",
                                        "strength": "must", "serves": ["g1"]}]}},
    })


def ib(m: dict) -> bytes:
    return intent.dump(m).encode()


def item(m: dict, item_id: str) -> dict:
    """The item itself (to edit it in place; intent.all_items gives copies)."""
    return next(it for lst in [m["items"], *[s["items"] for s in m["views"].values()]] for it in lst
                if it["id"] == item_id)


def merged(base: dict, ours: dict, theirs: dict, prefer=None):
    return yamlmerge.merge(ib(base), ib(ours), ib(theirs), prefer, kind="intent")


# ---------------------------------------------------------------- netlist


def test_pending_connections_merge_per_pin():
    base = inverter()
    ours, theirs = connect(deepcopy(base), "M3.G", "A"), connect(deepcopy(base), "M3.D", "Y")
    r = yamlmerge.merge(nl(base), nl(ours), nl(theirs), kind="netlist")
    m = read_netlist(r.data)
    assert r.clean and r.kind == "netlist" and not r.warnings
    assert m["pending"] == {"M3.D": "Y", "M3.G": "A"}
    assert m["instances"]["M3"]["pins"] == {"D": "Y", "G": "A", "S": "VSS", "B": "VSS"}
    assert "pending connection M3.D" in r.combined


def test_pending_conflict_keeps_ours_and_resolves_to_theirs():
    base = inverter()
    ours, theirs = connect(deepcopy(base), "M3.G", "A"), connect(deepcopy(base), "M3.G", "Y")
    r = yamlmerge.merge(nl(base), nl(ours), nl(theirs), kind="netlist")
    assert [c.to_json() for c in r.conflicts] == [{"id": "pending:M3.G", "what": "pending connection M3.G",
                                                   "base": None, "ours": "A", "theirs": "Y", "taken": "ours",
                                                   "resolved": False}]
    assert not r.clean and r.data == nl(ours)
    assert r.warnings == []                         # the pin's net is the pending conflict, not a second report
    again = yamlmerge.merge(nl(base), nl(ours), nl(theirs), {"pending:M3.G": "theirs"}, kind="netlist")
    assert again.clean and again.conflicts[0].resolved and again.conflicts[0].taken == "theirs"
    assert again.data == nl(theirs)
    # drawn on one side, moved to another net on the other: delete / modify, the same id
    base2 = connect(inverter(), "M3.G", "A")
    ours2 = deepcopy(base2)
    del ours2["pending"]["M3.G"]                    # the wire was drawn to A
    theirs2 = connect(deepcopy(base2), "M3.G", "Y")
    r2 = yamlmerge.merge(nl(base2), nl(ours2), nl(theirs2), kind="netlist")
    assert [c.id for c in r2.conflicts] == ["pending:M3.G"] and read_netlist(r2.data)["pending"] == {}


def test_a_pin_connected_on_one_side_and_drawn_to_another_net_on_the_other_conflicts():
    """Reading the merged drawing would put the pin on the drawn net and drop the pending connection silently."""
    base = inverter()                                              # M3.G floating (net2)
    ours, theirs = connect(deepcopy(base), "M3.G", "A"), draw(deepcopy(base), "M3.G", "Y")
    r = yamlmerge.merge(nl(base), nl(ours), nl(theirs), kind="netlist")
    assert [c.to_json() for c in r.conflicts] == [{"id": "pending:M3.G", "what": "pending connection M3.G",
                                                   "base": None, "ours": "A", "theirs": "drawn to Y", "taken": "ours",
                                                   "resolved": False}]
    assert r.data == nl(ours) and r.warnings == []                 # the pin's net is the conflict, not a warning
    again = yamlmerge.merge(nl(base), nl(ours), nl(theirs), {"pending:M3.G": "theirs"}, kind="netlist")
    assert again.clean and again.data == nl(theirs)                # no pending connection, the pin on the drawn Y
    # the other way round: ours drew it, theirs connects it
    r = yamlmerge.merge(nl(base), nl(theirs), nl(ours), kind="netlist")
    assert [(c.id, c.ours, c.theirs) for c in r.conflicts] == [("pending:M3.G", "drawn to Y", "A")]
    assert r.data == nl(theirs) and not r.warnings
    again = yamlmerge.merge(nl(base), nl(theirs), nl(ours), {"pending:M3.G": "theirs"}, kind="netlist")
    assert again.clean and again.data == nl(ours)
    # a pending connection moved on one side, the pin drawn on the other
    base2 = connect(inverter(), "M3.G", "A")
    r = yamlmerge.merge(nl(base2), nl(connect(deepcopy(base2), "M3.G", "VSS")), nl(draw(deepcopy(base2), "M3.G", "Y")),
                        kind="netlist")
    assert [(c.id, c.base, c.ours, c.theirs) for c in r.conflicts] == [("pending:M3.G", "A", "VSS", "drawn to Y")]
    # drawn to the net the other side connects it to, or still floating (its unnamed net renamed): no conflict
    for other in (draw(deepcopy(base), "M3.G", "A"), draw(deepcopy(base), "M3.G", "net7")):
        r = yamlmerge.merge(nl(base), nl(ours), nl(other), kind="netlist")
        assert r.clean and not r.warnings and r.data == nl(ours)
    # a pin that was pending, drawn to two different nets: the drawing's, so the warning (no pending connection lost)
    r = yamlmerge.merge(nl(base2), nl(draw(deepcopy(base2), "M3.G", "Y")), nl(draw(deepcopy(base2), "M3.G", "VSS")),
                        kind="netlist")
    assert r.clean and len(r.warnings) == 1 and "instance M3: pin G" in r.warnings[0]


def test_a_floating_pin_wired_up_on_its_own_unnamed_net_is_drawn():
    """read_drawing names a new unnamed net after the old one it shares the most pins with, so R9.P, floating on net1
    and then wired to R10.M, stays on net1. Base's net, but now with two pins: drawn, and ours' pending connection
    conflicts (reading the merged drawing would drop it)."""
    base = netlist.empty()
    base["ports"] = [{"name": "in", "type": "ipin"}, {"name": "VSS", "type": "iopin"}]
    base["instances"] = {"R9": {"symbol": RES, "props": {"value": "1k"}, "pins": {"P": "net1", "M": "VSS"}}}
    ours = connect(deepcopy(base), "R9.P", "in")
    theirs = deepcopy(base)
    theirs["instances"]["R10"] = {"symbol": RES, "props": {"value": "2k"}, "pins": {"P": "VSS", "M": "net1"}}
    r = yamlmerge.merge(nl(base), nl(ours), nl(theirs), kind="netlist")
    assert [c.to_json() for c in r.conflicts] == [{"id": "pending:R9.P", "what": "pending connection R9.P",
                                                   "base": None, "ours": "in", "theirs": "drawn to net1",
                                                   "taken": "ours", "resolved": False}]
    m = read_netlist(r.data)
    assert m["pending"] == {"R9.P": "in"} and m["instances"]["R10"]["pins"]["M"] == "net1" and not r.warnings
    again = yamlmerge.merge(nl(base), nl(ours), nl(theirs), {"pending:R9.P": "theirs"}, kind="netlist")
    assert again.clean and again.data == nl(theirs)
    r = yamlmerge.merge(nl(base), nl(theirs), nl(ours), kind="netlist")             # the other way round
    assert [(c.id, c.ours, c.theirs) for c in r.conflicts] == [("pending:R9.P", "drawn to net1", "in")]
    # R10 on a net of its own, R9.P still floating on net1: no conflict, the pending connection kept
    apart = deepcopy(base)
    apart["instances"]["R10"] = {"symbol": RES, "props": {"value": "2k"}, "pins": {"P": "VSS", "M": "net2"}}
    r = yamlmerge.merge(nl(base), nl(ours), nl(apart), kind="netlist")
    assert r.clean and not r.warnings and read_netlist(r.data)["pending"] == {"R9.P": "in"}
    # a pin base drew already (net1 with R10.M), left there on one side: not that side's drawing, no conflict
    ours2 = deepcopy(theirs)
    del ours2["instances"]["R10"]                                  # the wire and R10 removed, then R9.P connected
    connect(ours2, "R9.P", "in")
    theirs2 = deepcopy(theirs)
    theirs2["instances"]["R9"]["props"]["value"] = "1.5k"
    r = yamlmerge.merge(nl(theirs), nl(ours2), nl(theirs2), kind="netlist")
    assert r.clean and not r.warnings and read_netlist(r.data)["pending"] == {"R9.P": "in"}


def test_a_pin_on_a_drawn_unnamed_net_wired_to_a_new_device_is_drawn():
    # base: R9.P and R10.P drawn on net3. Ours unwires R9.P and connects it to A as a pending connection; theirs wires a
    # new R11.P to R9.P, which keeps the name net3 (read_drawing names a net after the old one): R9.P is drawn there
    base = inverter()
    for name in ("R9", "R10"):
        base["instances"][name] = {"symbol": RES, "props": {"value": "1k"}, "pins": {"P": "net3", "M": "VSS"}}
    ours = connect(deepcopy(base), "R9.P", "A")
    theirs = deepcopy(base)
    theirs["instances"]["R11"] = {"symbol": RES, "props": {"value": "3k"}, "pins": {"P": "net3", "M": "VSS"}}
    r = yamlmerge.merge(nl(base), nl(ours), nl(theirs), kind="netlist")
    assert [(c.id, c.ours, c.theirs) for c in r.conflicts] == [("pending:R9.P", "A", "drawn to net3")]
    # theirs adding nothing to net3: R9.P stays where base drew it, ours' pending connection merges
    r = yamlmerge.merge(nl(base), nl(ours), nl(deepcopy(base)), kind="netlist")
    assert r.clean and read_netlist(r.data)["pending"] == {"R9.P": "A"}


def test_a_pending_connection_on_an_instance_the_other_side_removed_conflicts():
    base = inverter()
    ours = connect(deepcopy(base), "M3.G", "A")
    renamed, removed = deepcopy(base), deepcopy(base)
    renamed["instances"]["M33"] = renamed["instances"].pop("M3")
    del removed["instances"]["M3"]
    for theirs in (renamed, removed):
        r = yamlmerge.merge(nl(base), nl(ours), nl(theirs), kind="netlist")
        assert [(c.id, c.ours, c.theirs) for c in r.conflicts] == [("pending:M3.G", "A", "M3 removed")]
        r = yamlmerge.merge(nl(base), nl(ours), nl(theirs), {"pending:M3.G": "theirs"}, kind="netlist")
        assert "M3.G" not in read_netlist(r.data)["pending"]


def test_netlist_is_read_like_netlist_load(tmp_path):
    for i, text in enumerate(["version: 1\nports: []\ninstances: {}\npending: []\n", "pending:\ncode:\n", "",
                              "instances: {M1: {pins: {G: A}}}\npending: {M1.G: 0}\n"]):
        path = tmp_path / f"c{i}{netlist.SUFFIX}"
        path.write_text(text)
        assert read_netlist(text.encode()) == netlist.load(path)
    for bad in ("[1, 2]\n", "pending: [M3.G]\n", "pending: x\n", "instances: [M1]\n", "ports: {A: ipin}\n"):
        with pytest.raises(ValueError):
            read_netlist(bad.encode())


def test_derived_parts_conflict_is_one_warning_and_ours_is_kept():
    base = inverter()
    ours, theirs = deepcopy(base), deepcopy(base)
    ours["instances"]["M1"]["props"]["W"] = "4"
    ours["code"][0]["props"]["value"] = ".tran 1n 2u"
    theirs["instances"]["M1"]["props"]["W"] = "3"
    theirs["instances"]["M1"]["props"]["nf"] = "2"           # another property of the same device combines
    theirs["instances"]["M2"]["props"]["W"] = "0.5"
    theirs["code"][0]["props"]["value"] = ".tran 0.5n 1u"
    theirs["ports"].append({"name": "EN", "type": "ipin"})
    r = yamlmerge.merge(nl(base), nl(ours), nl(theirs), kind="netlist")
    assert r.clean and r.conflicts == []
    assert len(r.warnings) == 1
    assert "instance M1: property W" in r.warnings[0] and "code s1: property value" in r.warnings[0]
    assert "follow the merged schematic" in r.warnings[0]
    m = read_netlist(r.data)
    assert m["instances"]["M1"]["props"] == {"L": "0.15", "W": "4", "model": "pfet_01v8", "nf": "2"}
    assert m["instances"]["M2"]["props"]["W"] == "0.5" and m["code"][0]["props"]["value"] == ".tran 1n 2u"
    assert [p["name"] for p in m["ports"]] == ["A", "Y", "VDD", "VSS", "EN"]
    assert {"instance M2: property W", "instance M1: property nf", "port EN"} <= set(r.combined)


def test_netlist_merge_keeps_a_sides_bytes():
    base = inverter()
    flowed = yaml.safe_dump(base, sort_keys=False, default_flow_style=False).encode()   # same model, other layout
    ours, theirs = deepcopy(base), deepcopy(base)
    ours["instances"]["M2"]["props"]["W"] = "2"
    r = yamlmerge.merge(nl(base), nl(ours), flowed, kind="netlist")
    assert r.clean and r.data == nl(ours) and r.combined == []
    r = yamlmerge.merge(nl(base), flowed, nl(ours), kind="netlist")
    assert r.data == nl(ours)
    r = yamlmerge.merge(nl(base), flowed, nl(base), kind="netlist")
    assert r.data == flowed                          # ours unchanged in substance: its bytes, untouched


def test_netlist_both_added():
    a = connect(inverter(), "M3.G", "A")
    r = yamlmerge.merge(None, nl(a), yaml.safe_dump(a, sort_keys=False).encode(), kind="netlist")
    assert r.clean and r.data == nl(a)
    r = yamlmerge.merge(None, nl(a), nl(connect(inverter(), "M3.G", "VSS")), kind="netlist")
    assert [c.id for c in r.conflicts] == ["pending:M3.G"]


# ---------------------------------------------------------------- intent


def test_item_fields_of_both_sides_combine():
    base = amp()
    ours, theirs = deepcopy(base), deepcopy(base)
    item(ours, "c1")["text"] = "M1 and M2 symmetric about the common-centroid axis"
    item(theirs, "c1")["strength"] = "should"
    item(theirs, "c1")["reason"] = "the area budget needs a 1D layout"
    theirs["supplies"]["avdd"] = "supply"
    r = merged(base, ours, theirs)
    assert r.clean and r.kind == "intent" and r.warnings == []
    c1 = item(read_intent(r.data), "c1")
    assert c1["text"] == "M1 and M2 symmetric about the common-centroid axis"
    assert (c1["strength"], c1["reason"], c1["serves"]) == ("should", "the area budget needs a 1D layout", ["g1"])
    assert read_intent(r.data)["supplies"] == {"vdd": "supply", "vss": "ground", "avdd": "supply"}
    assert r.combined == ["supply avdd", "item c1: strength", "item c1: reason"]
    assert r.data == intent.dump(read_intent(r.data)).encode()       # written as intent.dump writes it


def test_field_conflicts_ports_and_view_items():
    base = amp()
    ours, theirs = deepcopy(base), deepcopy(base)
    ours["ports"]["inp"]["side"] = "N"
    theirs["ports"]["inp"]["kind"] = "bias_in"             # another field of the same port: combines
    ours["ports"]["out"]["side"] = "S"
    theirs["ports"]["out"]["side"] = "N"
    item(ours, "c2")["text"] = "M1 and M2 interdigitated"
    item(theirs, "c2")["text"] = "M1 and M2 in a 2x2 common centroid"
    item(theirs, "s1")["spec"] = {**item(base, "s1")["spec"], "min": 66}
    r = merged(base, ours, theirs)
    assert [c.id for c in r.conflicts] == ["port:out:side", "view:layout:item:c2:text"]
    assert all(c.taken == "ours" for c in r.conflicts)
    m = read_intent(r.data)
    assert m["ports"]["inp"] == {"kind": "bias_in", "side": "N"} and m["ports"]["out"]["side"] == "S"
    assert item(m, "c2")["text"] == "M1 and M2 interdigitated" and item(m, "s1")["spec"]["min"] == 66
    r = merged(base, ours, theirs, {"port:out:side": "theirs", "view:layout:item:c2:text": "theirs"})
    m = read_intent(r.data)
    assert r.clean and m["ports"]["out"]["side"] == "N"
    assert item(m, "c2")["text"] == "M1 and M2 in a 2x2 common centroid"


def test_both_add_c3_renumbers_theirs_and_its_serves_follow():
    base = amp()
    ours, theirs = deepcopy(base), deepcopy(base)
    ours["items"].append({"id": "c3", "kind": "constraint", "text": "Input pair away from the clock lines",
                          "strength": "must", "serves": ["g1"]})
    ours["views"]["layout"]["items"].append({"id": "x1", "kind": "context", "text": "The clock tree runs north",
                                             "serves": ["c3"]})
    theirs["items"].append({"id": "c3", "kind": "constraint", "text": "Tail current at least 20 uA",
                            "strength": "should", "serves": ["s1"]})
    theirs["views"]["schematic"] = {"items": [{"id": "c4", "kind": "constraint", "text": "M5 sized for 20 uA",
                                               "strength": "must", "serves": ["c3"]}]}
    item(theirs, "c2")["serves"] = ["g1", "c3"]
    r = merged(base, ours, theirs)
    assert r.clean
    m = read_intent(r.data)
    assert item(m, "c3")["text"] == "Input pair away from the clock lines"
    assert item(m, "c5")["text"] == "Tail current at least 20 uA"     # c4 is theirs' too: the next free is c5
    assert item(m, "c4")["serves"] == ["c5"] and item(m, "c2")["serves"] == ["g1", "c5"]
    assert item(m, "x1")["serves"] == ["c3"]                          # ours' references stay on ours' c3
    assert [it["id"] for it in m["items"]] == ["g1", "p1", "c1", "s1", "c5", "c3"]   # after its predecessor in theirs
    assert [it["id"] for it in m["views"]["layout"]["items"]] == ["c2", "x1"]
    assert any("c5" in c and "c3" in c and "renumbered" in c for c in r.combined)
    assert intent.validate(m, None) == []                             # unique ids, no dangling serves
    # the same item added on both sides is one item
    same = deepcopy(base)
    same["items"].append(deepcopy(ours["items"][-1]))
    r = merged(base, same, deepcopy(same))
    assert r.data == ib(same)


def test_renumbering_never_fills_a_gap():
    """Theirs added c3 c4 c5 and deleted c4: a branch made meanwhile may still have c4, so the renumbered c3 is c6."""
    base = amp()
    ours, theirs = deepcopy(base), deepcopy(base)
    ours["items"].append({"id": "c3", "kind": "constraint", "text": "Input pair away from the clock lines",
                          "strength": "must"})
    theirs["items"] += [{"id": "c3", "kind": "constraint", "text": "Tail current at least 20 uA", "strength": "must"},
                        {"id": "c5", "kind": "constraint", "text": "Guard ring around M1 M2", "strength": "must"}]
    r = merged(base, ours, theirs)
    m = read_intent(r.data)
    assert r.clean and item(m, "c6")["text"] == "Tail current at least 20 uA"
    assert "c4" not in {it["id"] for _, it in intent.all_items(m)}


def test_ids_that_are_not_strings_are_renumbered_by_their_text():
    """intent.normalize takes id: 7. Both sides adding a different 7 renumbers theirs' like any id (by its text, as
    intent.next_id reads ids), and theirs' references to it follow."""
    base = amp()
    ours, theirs = deepcopy(base), deepcopy(base)
    ours["items"].append({"id": 7, "kind": "constraint", "text": "Input pair away from the clock lines",
                          "strength": "must", "serves": ["g1"]})
    theirs["items"] += [{"id": 7, "kind": "constraint", "text": "Tail current at least 20 uA", "strength": "must",
                         "serves": ["s1"]},
                        {"id": "x1", "kind": "context", "text": "From the bias review", "serves": [7]}]
    r = merged(base, ours, theirs)
    assert r.clean and not r.warnings
    m = read_intent(r.data)
    assert item(m, 7)["text"] == "Input pair away from the clock lines"
    assert item(m, "8")["text"] == "Tail current at least 20 uA" and item(m, "x1")["serves"] == ["8"]
    assert any("8" in c and "7" in c and "renumbered" in c for c in r.combined)
    assert intent.validate(m, None) == []                             # unique ids, no dangling serves


def test_a_new_item_does_not_take_the_id_of_one_deleted_and_edited_on_the_other_side():
    base = amp()                                                 # c1 in the cell intent, c2 in the layout view
    ours, theirs = deepcopy(base), deepcopy(base)
    ours["items"] = [it for it in ours["items"] if it["id"] != "c1"]
    new = {"id": intent.next_id(ours, "constraint"), "kind": "constraint", "text": "Input pair away from the clock",
           "strength": "must"}                                   # as IntentEditor.add_item allocates it
    ours["items"].append(new)
    item(theirs, "c1")["strength"] = "should"
    r = merged(base, ours, theirs)
    assert new["id"] == "c3"
    assert [c.id for c in r.conflicts] == ["item:c1"]            # delete / modify, not an edit of ours' new item
    assert item(read_intent(r.data), "c3") == new
    # the case left: deleting the highest-numbered item and adding one gives its id again, which reads as an edit
    ours = deepcopy(base)
    ours["views"]["layout"]["items"] = []
    assert intent.next_id(ours, "constraint") == "c2"


def test_one_item_both_sides_added_is_merged_not_renumbered():
    # a cherry-picked addition edited on one side: the same item (same kind and measure), merged field by field
    base = amp()
    spec = {"id": "s2", "kind": "spec", "text": "Phase margin at least 60 deg",
            "spec": {"measure": "phase_margin_deg", "min": 60, "testbench": "tb_ac", "output": "out"}}
    ours, theirs = deepcopy(base), deepcopy(base)
    ours["items"].append(deepcopy(spec))
    edited = deepcopy(spec)
    edited["text"], edited["spec"]["min"] = "Phase margin at least 65 deg", 65
    theirs["items"].append(edited)
    r = merged(base, ours, theirs)
    assert [c.id for c in r.conflicts] == ["item:s2"]
    assert [it["id"] for it in read_intent(r.data)["items"]].count("s2") == 1
    assert "s3" not in {it["id"] for it in read_intent(r.data)["items"]}


def added(tmp_path: Path, side: str, model: dict, it: dict) -> dict:
    """The model after a person adds an item in Undine (IntentEditor.add_item: the next id, source {by: user})."""
    path = tmp_path / side / f"amp{intent.SUFFIX}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ib(model))
    intent.IntentEditor(path).add_item(it)
    return intent.load(path)


def test_items_alike_but_added_apart_are_two_items(tmp_path):
    """Two items both sides added as one id are one item only when more ties them than who added them (every item a
    person adds has source {by: user}), a rule of one type (on other instances) or a spec of one measure (on another
    testbench): each such pair merges cleanly, theirs' item renumbered."""
    base = amp()
    pairs = [
        ({"kind": "constraint", "text": "Input pair inside a guard ring"},
         {"kind": "constraint", "text": "Keep the output away from the clock"}),
        ({"kind": "constraint", "text": "M1 and M2 matched", "rule": {"type": "match", "instances": ["M1", "M2"]}},
         {"kind": "constraint", "text": "M3 and M4 matched", "rule": {"type": "match", "instances": ["M3", "M4"]}}),
        ({"kind": "spec", "text": "Phase margin at least 60 deg in unity gain",
          "spec": {"measure": "phase_margin_deg", "min": 60, "testbench": "tb_ac", "output": "out"}},
         {"kind": "spec", "text": "Phase margin at least 45 deg into 2 pF",
          "spec": {"measure": "phase_margin_deg", "min": 45, "testbench": "tb_load", "output": "out"}}),
    ]
    for n, (mine, other) in enumerate(pairs):
        ours, theirs = added(tmp_path / str(n), "ours", base, mine), added(tmp_path / str(n), "theirs", base, other)
        i, j = intent.next_id(base, mine["kind"]), intent.next_id(ours, mine["kind"])          # c3 c4, s2 s3
        assert item(ours, i)["source"] == item(theirs, i)["source"] == {"by": "user"}
        r = merged(base, ours, theirs)
        assert r.clean and not r.warnings, (mine, r.conflicts)
        m = read_intent(r.data)
        assert item(m, i)["text"] == mine["text"] and item(m, j)["text"] == other["text"]
        assert any("renumbered" in c for c in r.combined) and intent.validate(m, None) == []
    # each added item has its own uid: two additions are two items, even for one request
    asked = {"source": {"request": "add a guard ring", "turn": "t-17"}}
    ours = added(tmp_path / "asked", "ours", base, {**pairs[0][0], **asked})
    theirs = added(tmp_path / "asked", "theirs", base, {**pairs[0][0], "text": "Input pair in a guard ring", **asked})
    r = merged(base, ours, theirs)
    assert r.clean and {"c3", "c4"} <= {it["id"] for it in read_intent(r.data)["items"]}


def test_items_are_told_apart_by_uid_however_merges_renumber_them(tmp_path):
    """a and b each add a c3; a merges b (b's item becomes c4 on a), b then adds its own c4 and tightens its c3, and a
    merges b again: b's edit lands on b's item, by its uid, whatever the ids say. Merged back the other way too."""
    b0 = amp()
    a1 = added(tmp_path / "1", "a", b0, {"kind": "constraint", "text": "Guard ring around M1", "strength": "should"})
    b1 = added(tmp_path / "1", "b", b0, {"kind": "constraint", "text": "Keep M3 near M2", "strength": "should"})
    a2 = read_intent(merged(b0, a1, b1).data)
    assert {it["text"]: it["id"] for it in a2["items"] if it["kind"] == "constraint"} == {
        "M1 and M2 symmetric": "c1", "Guard ring around M1": "c3", "Keep M3 near M2": "c4"}
    b2 = deepcopy(b1)
    item(b2, "c3")["strength"] = "must"
    b2 = added(tmp_path / "2", "b", b2, {"kind": "constraint", "text": "Tail current at least 20 uA"})
    for got in (read_intent(merged(b1, a2, b2).data), read_intent(merged(b1, b2, a2).data)):
        by = {it["text"]: it for it in got["items"] if it["kind"] == "constraint"}
        assert by["Keep M3 near M2"]["strength"] == "must" and by["Guard ring around M1"]["strength"] == "should"
        assert len({it["id"] for it in got["items"]}) == len(got["items"])            # ids unique
        assert "Tail current at least 20 uA" in by


def test_a_renumbering_an_earlier_merge_made_is_followed():
    """a merged b, both having added c3: a has b's item as c4, b still calls it c3. b then tightens its c3, and a
    merges b again (base: b's old tip, where c3 is b's item): b's edit goes to a's c4, not to a's own c3, and b's
    references to its c3 follow. The same merge the other way round (b merges a) gives b a's numbering."""
    b0 = amp()
    a, b = deepcopy(b0), deepcopy(b0)
    a["items"] += [{"id": "c3", "kind": "constraint", "text": "Guard ring around M1", "strength": "should",
                    "source": {"by": "user"}},
                   {"id": "x1", "kind": "context", "text": "The ring is in the floorplan", "serves": ["c3"]}]
    item(a, "c1")["serves"] = ["g1", "c3"]
    b["items"].append({"id": "c3", "kind": "constraint", "text": "Keep M3 near M2", "strength": "should",
                       "source": {"by": "user"}})
    item(b, "c1")["serves"] = ["g1", "c3"]
    item(b, "c2")["serves"] = ["g1", "c3"]
    r = merged(b0, a, b)                                              # a merges b: b's c3 is a's c4
    a2 = read_intent(r.data)
    assert r.clean and item(a2, "c4")["text"] == "Keep M3 near M2" and set(item(a2, "c1")["serves"]) == \
        {"g1", "c3", "c4"}
    b2 = deepcopy(b)
    item(b2, "c3")["strength"] = "must"
    item(b2, "c2")["serves"] = ["g1", "c3", "s1"]

    def check(r):
        m = read_intent(r.data)
        assert r.clean and not r.warnings, r.conflicts
        assert (item(m, "c3")["text"], item(m, "c3")["strength"]) == ("Guard ring around M1", "should")
        assert (item(m, "c4")["text"], item(m, "c4")["strength"]) == ("Keep M3 near M2", "must")
        assert "c5" not in {str(it["id"]) for _, it in intent.all_items(m)}
        assert set(item(m, "c1")["serves"]) == {"g1", "c3", "c4"} and item(m, "x1")["serves"] == ["c3"]
        assert set(item(m, "c2")["serves"]) == {"g1", "c4", "s1"}
        assert any("an earlier merge renumbered" in c for c in r.combined) and intent.validate(m, None) == []
        return {str(it["id"]): {**it, "serves": sorted(it.get("serves", []))} for _, it in intent.all_items(m)}

    assert check(merged(b, a2, b2)) == check(merged(b, b2, a2))       # a merges b again; b merges a


def test_an_item_cherry_picked_before_is_not_added_twice():
    """a and b add a different c3; a cherry-picks b (b's item is c4 on a), then merges b: theirs' c3 is ours' c4,
    one item (both versions differing: a conflict on it), not renumbered a second time."""
    b0 = amp()
    a, b = deepcopy(b0), deepcopy(b0)
    a["items"].append({"id": "c3", "kind": "constraint", "text": "Guard ring around M1", "strength": "should"})
    b["items"] += [{"id": "c3", "kind": "constraint", "text": "Keep M3 near M2", "strength": "should"},
                   {"id": "x1", "kind": "context", "text": "M3 shares the well of M2", "serves": ["c3"]}]
    picked = read_intent(merged(b0, a, b).data)                      # the cherry-pick: base is b's parent
    assert item(picked, "c4")["text"] == "Keep M3 near M2" and item(picked, "x1")["serves"] == ["c4"]
    r = merged(b0, picked, b)
    assert r.clean and r.data == ib(picked)
    assert any("theirs' item c3 is ours' c4" in c for c in r.combined)
    # b edited its c3 after the pick: one item, a conflict, resolved to b's
    b2 = deepcopy(b)
    item(b2, "c3")["strength"] = "must"
    r = merged(b0, picked, b2)
    assert [c.id for c in r.conflicts] == ["item:c4"] and r.data == ib(picked)
    r = merged(b0, picked, b2, {"item:c4": "theirs"})
    m = read_intent(r.data)
    assert r.clean and item(m, "c4")["strength"] == "must" and item(m, "c3")["text"] == "Guard ring around M1"
    assert sorted(str(it["id"]) for _, it in intent.all_items(m)) == sorted(str(it["id"]) for _, it in
                                                                            intent.all_items(picked))
    # each side picked the other's: ours c3 A c4 B, theirs c3 B c4 A, swapped back onto ours' ids
    swapped = deepcopy(b)
    swapped["items"].insert(-1, {**a["items"][-1], "id": "c4"})
    r = merged(b0, picked, swapped)
    assert r.clean and r.data == ib(picked)


def test_ids_7_and_text_7_are_one_id():
    base = amp()
    ours, theirs = deepcopy(base), deepcopy(base)
    ours["items"].append({"id": 7, "kind": "constraint", "text": "Input pair away from the clock"})
    theirs["items"].append({"id": "7", "kind": "constraint", "text": "Tail current at least 20 uA"})
    r = merged(base, ours, theirs)
    got = [str(it["id"]) for it in read_intent(r.data)["items"]]
    assert got.count("7") == 1 and "8" in got


def test_an_id_that_is_no_text_is_an_unreadable_file():
    bad = ib(amp()).decode() + "- {id: [c, 3], kind: constraint, text: M1 M2 matched}\n"
    bad = bad.replace("items:\n", "items:\n- {id: [c, 3], kind: constraint, text: M1 M2 matched}\n", 1)
    r = yamlmerge.merge(ib(amp()), ib(amp()), bad.encode(), kind="intent")
    assert [c.id for c in r.conflicts] == ["file"]
    with pytest.raises(ValueError):
        yamlmerge.diff(ib(amp()), bad.encode(), kind="intent")


def test_serves_merges_as_a_set():
    base = amp()
    ours, theirs = deepcopy(base), deepcopy(base)
    item(ours, "c2")["serves"] = ["g1", "p1"]
    item(theirs, "c2")["serves"] = ["s1"]
    r = merged(base, ours, theirs)
    assert r.clean and sorted(item(read_intent(r.data), "c2")["serves"]) == ["p1", "s1"]
    assert {"layout view item c2: serves g1 removed", "layout view item c2: serves s1 added"} <= set(r.combined)
    # only reordered on one side: no change
    item(theirs, "c2")["serves"] = ["g1"]
    item(ours, "c1")["serves"] = ["g1"]
    r = merged(base, ours, theirs)
    assert r.data == ib(ours) and r.combined == []


def test_notes_merge_by_line():
    base = amp()
    ours, theirs = deepcopy(base), deepcopy(base)
    ours["notes"] = base["notes"].replace("2 pF", "3 pF")
    theirs["notes"] = base["notes"].replace("10 uA", "20 uA") + "Layout shares the well with the comparator.\n"
    r = merged(base, ours, theirs)
    assert r.clean and read_intent(r.data)["notes"] == (
        "Drives a 3 pF load inside the ADC.\nKeep the input offset below 1 mV.\nBias from the shared 20 uA mirror.\n"
        "Layout shares the well with the comparator.\n")
    assert "notes: the lines both sides changed" in r.combined


def test_notes_conflict_keeps_ours():
    base = amp()
    ours, theirs = deepcopy(base), deepcopy(base)
    ours["notes"] = base["notes"].replace("1 mV", "0.5 mV")
    theirs["notes"] = base["notes"].replace("1 mV", "2 mV")
    item(theirs, "g1")["text"] = "Minimise the input offset and its drift"
    r = merged(base, ours, theirs)
    assert [c.id for c in r.conflicts] == ["notes"] and r.conflicts[0].theirs == theirs["notes"]
    m = read_intent(r.data)
    assert m["notes"] == ours["notes"] and item(m, "g1")["text"] == "Minimise the input offset and its drift"
    r = merged(base, ours, theirs, {"notes": "theirs"})
    assert r.clean and read_intent(r.data)["notes"] == theirs["notes"]


def test_notes_without_git_are_a_conflict(monkeypatch):
    base = amp()
    ours, theirs = deepcopy(base), deepcopy(base)
    ours["notes"] = base["notes"].replace("2 pF", "3 pF")
    theirs["notes"] = base["notes"].replace("10 uA", "20 uA")

    def no_git(*a, **kw):
        raise FileNotFoundError("git")

    monkeypatch.setattr(yamlmerge.subprocess, "run", no_git)
    r = merged(base, ours, theirs)
    assert [c.id for c in r.conflicts] == ["notes"] and read_intent(r.data)["notes"] == ours["notes"]


def test_delete_modify_item_is_a_conflict():
    base = amp()
    ours, theirs = deepcopy(base), deepcopy(base)
    ours["items"] = [it for it in ours["items"] if it["id"] != "p1"]
    ours["views"] = {}                                              # ours removed the layout constraint c2
    item(theirs, "p1")["text"] = "Area over performance"
    item(theirs, "c2")["strength"] = "should"
    r = merged(base, ours, theirs)
    assert [c.id for c in r.conflicts] == ["item:p1", "view:layout:item:c2"]
    assert r.conflicts[0].ours is None and r.conflicts[0].theirs["text"] == "Area over performance"
    assert r.conflicts[1].theirs["view"] == "layout"
    m = read_intent(r.data)
    assert "p1" not in {it["id"] for _, it in intent.all_items(m)} and m["views"] == {}
    r = merged(base, ours, theirs, {"item:p1": "theirs", "view:layout:item:c2": "theirs"})
    m = read_intent(r.data)
    assert r.clean and item(m, "p1")["text"] == "Area over performance"
    assert [it["id"] for it in m["items"]] == ["g1", "p1", "c1", "s1"] and item(m, "c2")["strength"] == "should"


def test_a_deleted_item_still_served_by_the_other_side_warns():
    base = amp()
    ours, theirs = deepcopy(base), deepcopy(base)
    ours["items"] = [it for it in ours["items"] if it["id"] != "g1"]       # like IntentEditor.remove_item
    for it in [*ours["items"], *ours["views"]["layout"]["items"]]:
        if "g1" in it.get("serves", []):
            it["serves"].remove("g1")
    theirs["views"]["layout"]["items"].append({"id": "c3", "kind": "constraint", "text": "Dummies around M1 M2",
                                               "strength": "should", "serves": ["g1"]})
    r = merged(base, ours, theirs)
    assert r.clean and r.warnings == ["layout view item c3 serves g1, which the merged intent does not have"]
    assert "serves" not in item(read_intent(r.data), "c1")


def test_intent_merge_keeps_a_sides_bytes():
    base = amp()
    ours = b"# reviewed with the layout team\n" + ib(base)
    assert merged(base, base, base).data == ib(base)
    r = yamlmerge.merge(ib(base), ours, ib(base), kind="intent")
    assert r.data == ours and r.clean
    theirs = deepcopy(base)
    item(theirs, "s1")["text"] = "DC gain at least 66 dB"
    theirs_bytes = b"# tightened after review\n" + ib(theirs)
    r = yamlmerge.merge(ib(base), ib(base), theirs_bytes, kind="intent")
    assert r.data == theirs_bytes and r.combined == ["item s1: text"]
    # both added the same intent (base None), formatted differently
    r = yamlmerge.merge(None, ib(base), yaml.safe_dump(base, sort_keys=False).encode(), kind="intent")
    assert r.clean and r.data == ib(base)


def test_unreadable_side_is_one_conflict_and_ours_is_kept():
    base = amp()
    r = yamlmerge.merge(ib(base), ib(base), b"items: [\n", kind="intent")
    assert [c.id for c in r.conflicts] == ["file"] and r.data == ib(base)
    r = yamlmerge.merge(ib(base), b"items: {g1: 1}\n", ib(base), {"file": "theirs"}, kind="intent")
    assert r.clean and r.data == ib(base)


def test_dispatch_by_kind_or_path():
    base = amp()
    ours = deepcopy(base)
    ours["supplies"]["avss"] = "ground"
    r = yamlmerge.merge(ib(base), ib(ours), ib(base), path="libs/analog/amp.intent.yaml")
    assert r.kind == "intent" and r.data == ib(ours)
    assert yamlmerge.kind_of("libs/analog/inv.netlist.yaml") == "netlist" and yamlmerge.kind_of("inv.sch") is None
    assert yamlmerge.diff(None, nl(inverter()), path="inv.netlist.yaml")["kind"] == "netlist"
    with pytest.raises(ValueError, match="netlist or intent"):
        yamlmerge.merge(b"", b"", b"x", kind="schematic")


# ---------------------------------------------------------------- diff


def test_netlist_diff():
    old = connect(inverter(), "M3.G", "A")
    new = deepcopy(old)
    new["instances"] = dict(reversed(list(new["instances"].items())))     # reordered: no change by itself
    assert yamlmerge.diff(nl(old), yaml.safe_dump(new, sort_keys=False).encode(), kind="netlist") == \
        {"kind": "netlist", "changes": []}
    new["instances"]["M1"]["props"]["W"] = "4"
    del new["instances"]["M2"]
    new["instances"]["M4"] = deepcopy(old["instances"]["M2"])
    connect(new, "M3.D", "Y")
    new["ports"] = [new["ports"][1], new["ports"][0], *new["ports"][2:]]
    d = yamlmerge.diff(nl(old), nl(new), kind="netlist")
    assert [(c["do"], c["id"]) for c in d["changes"]] == [
        ("mod", "ports"),
        ("mod", "inst:M3:pins:D"), ("mod", "inst:M1:props:W"),
        ("del", "inst:M2"), ("add", "inst:M4"),                         # new order, a deleted key where it was
        ("add", "pending:M3.D"),
    ]
    by = {c["id"]: c for c in d["changes"]}
    assert by["inst:M1:props:W"] == {"do": "mod", "id": "inst:M1:props:W", "what": "instance M1: property W",
                                     "before": "2", "after": "4"}
    assert by["ports"]["after"] == ["Y", "A", "VDD", "VSS"] and by["inst:M2"]["after"] is None
    assert by["pending:M3.D"]["after"] == "Y"
    assert yamlmerge.diff(nl(new), nl(new), kind="netlist")["changes"] == []


def test_intent_diff():
    old = amp()
    new = deepcopy(old)
    new["items"] = list(reversed(new["items"]))                           # reordered: no change
    item(new, "c1")["serves"] = ["g1"]
    assert yamlmerge.diff(ib(old), ib(new), kind="intent")["changes"] == []
    new["notes"] = old["notes"].replace("2 pF", "3 pF")
    new["ports"]["out"]["side"] = "N"
    del new["ports"]["inn"]
    item(new, "c1")["strength"] = "should"
    item(new, "c1")["serves"] = ["g1", "s1"]
    new["items"] = [it for it in new["items"] if it["id"] != "p1"]
    new["views"]["schematic"] = {"items": [{"id": "c3", "kind": "constraint", "text": "M1 and M2 equal",
                                            "serves": ["g1"]}]}
    d = yamlmerge.diff(ib(old), ib(new), kind="intent")
    assert d["kind"] == "intent"
    assert [(c["do"], c["id"]) for c in d["changes"]] == [
        ("mod", "notes"), ("del", "port:inn"), ("mod", "port:out:side"),
        ("mod", "item:c1:strength"), ("mod", "item:c1:serves"), ("del", "item:p1"),
        ("add", "view:schematic:item:c3"),
    ]
    by = {c["id"]: c for c in d["changes"]}
    assert by["item:c1:serves"]["before"] == ["g1"] and by["item:c1:serves"]["after"] == ["g1", "s1"]
    assert by["view:schematic:item:c3"]["after"]["view"] == "schematic" and by["item:p1"]["after"] is None
    assert [c["do"] for c in yamlmerge.diff(None, ib(old), kind="intent")["changes"]][:2] == ["add", "add"]
    assert yamlmerge.diff(ib(old), None, kind="intent")["changes"][0] == \
        {"do": "del", "id": "notes", "what": "notes", "before": old["notes"], "after": None}
    # an item with a uid (every item the editor adds): identified by it, worded by its id
    a = {"version": 1, "items": [{"id": "c1", "kind": "constraint", "text": "M1 and M2 symmetric", "uid": "u1"}]}
    b = {"version": 1, "items": [{**a["items"][0], "strength": "should"},
                                 {"id": "g1", "kind": "goal", "text": "Balanced rise and fall", "uid": "u2"}]}
    assert [(c["id"], c["what"]) for c in yamlmerge.diff(ib(a), ib(b), kind="intent")["changes"]] == [
        ("item:uid:u1:strength", "item c1: strength"), ("item:uid:u2", "item g1 (goal): Balanced rise and fall")]
