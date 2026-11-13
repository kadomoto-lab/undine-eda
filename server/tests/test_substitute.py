"""Cells simulated as stand-ins (schematic.spice substitute, Server.start_sim substitute): every instance of a cell, at
any depth below the testbench, netlisted as another cell of the same pins, without editing the design. Two divider
variants of the same pins (in, out, vss) and a stage that places one: the netlist and the simulated voltages change;
pins that differ, a stand-in that contains the cell it replaces, a substitution that replaces nothing, chains and
unknown cells are reported; a spec's own stand-ins (check_specs), a testbench's default (a substitute rule of its
intent) and the run's stand-ins over both; results with and without stand-ins never stand in for each other (job keys,
freshness); cells simulated as extracted next to stand-ins."""
import json
import shutil
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine.agents import agent_tools as T  # noqa: E402
from undine.design import intent  # noqa: E402
from undine.schematics import schematic, xschem  # noqa: E402
from undine.simulation import sim, simresults, specs  # noqa: E402
from undine.store import provenance  # noqa: E402
from test_netlist import V, _devices  # noqa: E402
from test_simresults import _need, _project  # noqa: E402

# a three-pin subcircuit symbol: in at the left, out at the right, vss at the bottom (in this order)
PINS = {"in": (-40, -20), "out": (40, -20), "vss": (0, 40)}
DEVICE_PINS = {"devices/res.sym": {"P": (0, -30), "M": (0, 30)}, "devices/vsource.sym": {"p": (0, -30), "m": (0, 30)}}


def _sym(pins: list[str]) -> str:
    return ('v {xschem version=3.4.5 file_version=1.2}\nK {type=subcircuit\nformat="@name @pinlist @symname"\n'
            'template="name=x1"}\n' + "".join(f"B 5 {PINS[p][0] - 2.5} {PINS[p][1] - 2.5} {PINS[p][0] + 2.5} "
                                              f"{PINS[p][1] + 2.5} {{name={p} dir=inout}}\n" for p in pins))


def _sch(devices: list[tuple[str, dict, dict]], ports: tuple[str, ...] = ()) -> str:
    """A schematic without wires: each device (symbol, properties, {pin: net}) at its own place, a label on each of
    its pins naming the net, and the ports (iopin) in order."""
    lines = [f"C {{devices/iopin.sym}} -500 {40 * i} 0 0 {{name=p{i} lab={p}}}" for i, p in enumerate(ports)]
    for k, (sym, props, nets) in enumerate(devices):
        x = 200 * k
        lines.append(f"C {{{sym}}} {x} 0 0 0 {{{' '.join(f'{a}={b}' for a, b in props.items())}}}")
        pins = DEVICE_PINS.get(sym, PINS)
        lines += [f"C {{devices/lab_pin.sym}} {x + pins[pin][0]} {pins[pin][1]} 0 0 {{name=l{k}{pin} lab={net}}}"
                  for pin, net in nets.items()]
    return V + "\n".join(lines) + "\n"


def _divider(r2: str) -> str:
    """out = in * r2 / (1k + r2)."""
    return _sch([("devices/res.sym", {"name": "R1", "value": "1k"}, {"P": "in", "M": "out"}),
                 ("devices/res.sym", {"name": "R2", "value": r2}, {"P": "out", "M": "vss"})], ("in", "out", "vss"))


def _design(lib: Path) -> None:
    """div (out = in / 2), div_alt (3 / 4), div9 (9 / 10, a schematic without a symbol), div_bad (pins in another
    order), div_two (no vss), div_loop (places div); stage places div; tb places stage (div at depth 2) and div."""
    pins = ["in", "out", "vss"]
    for cell, r2 in (("div", "1k"), ("div_alt", "3k"), ("div9", "9k"), ("div_bad", "1k"), ("div_two", "1k")):
        (lib / f"{cell}.sch").write_text(_divider(r2))
    for cell, p in (("div", pins), ("div_alt", pins), ("div_bad", ["in", "vss", "out"]), ("div_two", ["in", "out"]),
                    ("stage", pins), ("div_loop", pins)):
        (lib / f"{cell}.sym").write_text(_sym(p))
    (lib / "stage.sch").write_text(_sch([("div.sym", {"name": "x1"}, {"in": "in", "out": "out", "vss": "vss"})], tuple(pins)))
    (lib / "div_loop.sch").write_text(_sch([("div.sym", {"name": "x1"}, {"in": "in", "out": "out", "vss": "vss"})],
                                           tuple(pins)))
    (lib / "tb.sch").write_text(_sch([("devices/vsource.sym", {"name": "V1", "value": "1"}, {"p": "in", "m": "0"}),
                                      ("stage.sym", {"name": "x1"}, {"in": "in", "out": "o1", "vss": "0"}),
                                      ("div.sym", {"name": "x2"}, {"in": "in", "out": "o2", "vss": "0"})]))


def _setup():
    _devices()
    root, srv = _project()
    lib = srv.project("p").library("l").path
    _design(lib)
    return root, srv, lib, T.ToolCtx(srv, "p", actor="agent:t")


def _op(ctx, **args) -> dict:
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "tb", "analysis": {"type": "op"}, **args})
    assert not err, res
    return json.loads(res)


# ------------------------------------------------------------------ without a simulator


def test_a_substitution_map_is_checked():
    assert sim.substitute_map(None) == {} and sim.substitute_map({"a/b": "a/c", "a/d": None, "a/e": ""}) == \
        {"a/b": "a/c", "a/d": None, "a/e": None}
    for bad, msg in (({"b": "a/c"}, "'b' is not lib/cell"), (["a/b"], "a mapping"), ({"a/b": "a/c d"}, "not lib/cell")):
        with pytest.raises(ValueError, match=msg):
            sim.substitute_map(bad)
    assert schematic.pins_differ(["in", "out", "vss"], ["in", "vss", "out"]) == "in another order: pin 2 is vss, not out"
    assert schematic.pins_differ(["in", "out", "vss"], ["in", "out"]) == "without vss"
    assert schematic.pins_differ(["a"], ["a"]) is None
    # the intent panel says what a run would refuse
    model = {"items": [{"id": "c1", "kind": "constraint", "text": "a", "rule": {"type": "substitute"}},
                       {"id": "c2", "kind": "constraint", "text": "b", "rule": {"type": "substitute", "cells": {"x": "l/y"}}},
                       {"id": "s1", "kind": "spec", "text": "c", "spec": {"measure": "v(o)", "min": 1, "substitute": ["l/a"]}}],
             "views": {"schematic": {"items": [{"id": "c3", "kind": "constraint", "text": "d", "serves": ["c1"],
                                                "rule": {"type": "substitute", "cells": {"l/a": "l/b"}}}]}}}
    msgs = {p["entry"]: p["message"] for p in intent.validate(model, None)}
    assert msgs["items.c1"] == "a substitute rule names its cells" and msgs["items.c2"] == "cells: 'x' is not lib/cell"
    assert "substitute is a mapping" in msgs["items.s1"] and "cell intent of a testbench" in msgs["views.schematic.items.c3"]
    # a rule in the intent of a cell with ports (the cell measured, not its testbench) would never be used
    rule = {"items": [{"id": "c1", "kind": "constraint", "text": "a", "rule": {"type": "substitute", "cells": {"l/a": "l/b"}}}]}
    assert not intent.validate(rule, {"instances": [], "nets": [], "ports": [], "cells": []})
    assert "for its specs, give the spec substitute, or put the rule in its testbench's intent" in \
        intent.validate(rule, {"instances": [], "nets": ["in"], "ports": ["in"], "cells": []})[0]["message"]
    with pytest.raises(specs.SpecError, match="spec s1: substitute is a mapping"):
        specs.probes(model, "l", ["s1"])


def test_a_point_names_each_run_of_a_testbench():
    """Two runs of one testbench at a point (a spec with stand-ins, one without): each value traces to its own run."""
    rows = [{"point": {"temp": 27}, "runs": [{"job": "j1", "testbench": "tb"}, {"job": "j2", "testbench": "tb"},
                                             {"job": "j3", "testbench": "tb_ac"}],
             "results": {"s1": {"value": 0.75, "job": "j1", "pass": True, "margin_rel": 0.1},
                         "s2": {"value": 0.5, "job": "j2", "pass": True, "margin_rel": 0.2},
                         "s3": {"value": 0.4, "job": "j2", "pass": True, "margin_rel": 0.2},
                         "s4": {"value": 60, "job": "j3", "pass": True, "margin_rel": 0.2}}}]
    probes = [{"id": k} for k in ("s1", "s2", "s3", "s4")]
    assert specs.table(rows, probes)[0]["jobs"] == {"tb (s1)": "j1", "tb (s2, s3)": "j2", "tb_ac": "j3"}


def test_every_instance_at_any_depth_is_netlisted_as_its_standin(tmp_path):
    lib = tmp_path / "l"
    lib.mkdir()
    _design(lib)
    res = xschem.SymbolResolver([tmp_path, lib, *_devices()])
    plain = schematic.spice(lib / "tb.sch", res, top_subckt=False)
    alt = schematic.Standin("l/div", "l/div_alt", "div_alt", res.load(str(lib / "div_alt.sym"), None), lib / "div_alt.sch",
                            lib / "div_alt.sch")
    r = schematic.spice(lib / "tb.sch", res, top_subckt=False, substitute={lib / "div.sym": alt})
    assert "\nx2 in o2 0 div\n" in plain.text and ".subckt div in out vss\n" in plain.text and not plain.substituted
    assert "\nx2 in o2 0 div_alt\n" in r.text and "\nx1 in out vss div_alt\n" in r.text      # depth 1 and depth 2
    assert ".subckt div " not in r.text and ".subckt div_alt in out vss\nR1 in out 1k" in r.text and "R2 out vss 3k" in r.text
    assert r.substituted == ["l/div"] and r.subckts == ["stage", "div_alt"]
    r = schematic.spice(lib / "tb.sch", res, top_subckt=False, substitute={lib / "div.sym": alt},
                        overrides={"div_alt": {"R2": {"value": "7k"}}})      # a sweep varies the stand-in's own schematic
    assert "R2 out vss 7k" in r.text and "R2 out vss 3k" not in r.text
    assert (lib / "stage.sch").read_text().count("div.sym") == 1                          # nothing was written
    # a stand-in without a symbol: the original's symbol, the stand-in's name and schematic
    r = schematic.spice(lib / "tb.sch", res, top_subckt=False,
                        substitute={lib / "div.sym": schematic.Standin("l/div", "l/div9", "div9", None, lib / "div9.sch",
                                                                       lib / "div9.sch")})
    assert "\nx2 in o2 0 div9\n" in r.text and "R2 out vss 9k" in r.text
    # a stand-in that places the cell it replaces never ends
    loop = schematic.Standin("l/div", "l/div_loop", "div_loop", res.load(str(lib / "div_loop.sym"), None),
                             lib / "div_loop.sch", lib / "div_loop.sch")
    with pytest.raises(ValueError, match=r"l/div_loop contains l/div \(div_loop > div, instance x1 in div_loop\), the "
                                         "cell it replaces"):
        schematic.spice(lib / "tb.sch", res, top_subckt=False, substitute={lib / "div.sym": loop})
    # next to a cell simulated as extracted: stage from its "layout", div (x2) as its stand-in
    ext = ".subckt stage in out vss\nR1 in out 1k\nR2 out vss 4k\n.ends\n"
    r = schematic.spice(lib / "tb.sch", res, top_subckt=False, extracted={lib / "stage.sch": ext},
                        substitute={lib / "div.sym": alt})
    assert r.extracted == ["stage"] and r.substituted == ["l/div"] and "R2 out vss 4k" in r.text and "\nx2 in o2 0 div_alt\n" in r.text
    r = schematic.spice(lib / "tb.sch", res, top_subckt=False, extracted={lib / "stage.sch": ext},
                        substitute={lib / "stage.sym": schematic.Standin("l/stage", "l/div_alt", "div_alt", None,
                                                                         lib / "div_alt.sch", lib / "div_alt.sch")})
    assert r.extracted == [] and "\nx1 in o1 0 div_alt\n" in r.text                       # the stand-in, not the layout


def test_what_cannot_stand_in_says_why():
    root, srv, lib, ctx = _setup()
    for subst, msg in (({"l/div": "l/div_bad"}, "the stand-in's pins are not those of l/div in their order (in another "
                                                "order: pin 2 is vss, not out). l/div: in out vss; l/div_bad: in vss out"),
                       ({"l/div": "l/div_two"}, "(without vss)"),
                       ({"l/div": "l/nothing"}, "no cell l/nothing"), ({"l/div": "m/div"}, "no library m"),
                       ({"l/div": "l/div"}, "does not stand in for itself"),
                       ({"l/div": "l/div_alt", "l/div_alt": "l/div9"}, "l/div_alt is replaced as well (by l/div9)"),
                       ({"l/div": "l/div_loop"}, "l/div_loop contains l/div"), ({"l": "l/div"}, "'l' is not lib/cell")):
        res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "tb", "analysis": {"type": "op"}, "substitute": subst})
        assert err and msg in res, (subst, res)
    with pytest.raises(ValueError, match="not to a given netlist"):
        srv.start_sim("p", "l", "tb", None, "* x\n.end\n", substitute={"l/div": "l/div_alt"})
    with pytest.raises(ValueError, match=r"vary / conditions on div: l/div is netlisted as its stand-in l/div_alt "
                                         r"\(substitute\), so its schematic's properties do not apply: vary the "
                                         r"instances of the stand-in \(div_alt\) instead"):  # not netlisted
        srv.start_sim("p", "l", "tb", {"type": "op"}, overrides={"div": {"R2": {"value": "2k"}}},
                      substitute={"l/div": "l/div_alt"})
    with pytest.raises(ValueError, match=r"l/stage is simulated both as extracted and as its stand-in l/div_alt "
                                         r"\(substitute\): one or the other; substitute \{\"l/stage\": null\}"):
        srv.start_sim("p", "l", "tb", {"type": "op"}, extracted=["l/stage"], substitute={"l/stage": "l/div_alt"})
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "tb", "job": "x", "substitute": {"l/div": "l/div_alt"}})
    assert err and "substitute describe a new run" in res
    shutil.rmtree(root)


def test_a_standin_that_contains_the_cell_it_replaces_at_any_depth_or_shares_a_name():
    """The cycle is found whichever way the netlister comes to it: through a cell the stand-in places that is already
    being netlisted (tbw > wrap > div, div_s > wrap), and through two stand-ins. A stand-in with the name of another
    cell of the testbench (from another library) would share its subcircuit: refused, not merged, and not taken for
    a cycle."""
    root, srv, lib, ctx = _setup()
    pins = ("in", "out", "vss")
    for cell, places in (("wrap", "div.sym"), ("div_s", "wrap.sym"), ("div_w", "wrap.sym"), ("wrap_d", "div.sym")):
        (lib / f"{cell}.sym").write_text(_sym(list(pins)))
        (lib / f"{cell}.sch").write_text(_sch([(places, {"name": "x1"}, {p: p for p in pins})], pins))
    (lib / "tbw.sch").write_text(_sch([("devices/vsource.sym", {"name": "V1", "value": "1"}, {"p": "in", "m": "0"}),
                                       ("wrap.sym", {"name": "x1"}, {"in": "in", "out": "o1", "vss": "0"})]))
    for subst, msg in (({"l/div": "l/div_s"}, "substitute l/div -> l/div_s: l/div_s contains l/div (div_s > wrap > div), "
                                              "the cell it replaces"),
                       ({"l/div": "l/div_w", "l/wrap": "l/wrap_d"},
                        "substitute l/wrap -> l/wrap_d: l/wrap_d contains l/wrap (wrap_d > div_w > wrap), the cell it "
                        "replaces")):
        res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "tbw", "analysis": {"type": "op"}, "substitute": subst})
        assert err and msg in res, (subst, res)
    # another library's cells of the names of l's: div_alt (9/10) and stage
    srv.project("p").create_library("m")
    mlib = srv.project("p").library("m").path
    (mlib / "div_alt.sym").write_text(_sym(list(pins)))
    (mlib / "div_alt.sch").write_text(_divider("9k"))
    (mlib / "stage.sym").write_text(_sym(list(pins)))
    (mlib / "stage.sch").write_text(_divider("9k"))
    (lib / "tb2.sch").write_text(_sch([("devices/vsource.sym", {"name": "V1", "value": "1"}, {"p": "in", "m": "0"}),
                                       ("div.sym", {"name": "x2"}, {"in": "in", "out": "o2", "vss": "0"}),
                                       ("div_alt.sym", {"name": "x3"}, {"in": "in", "out": "o3", "vss": "0"})]))
    for tb, subst, other in (("tb2", {"l/div": "m/div_alt"}, "l/div_alt"), ("tb", {"l/div": "m/stage"}, "l/stage")):
        res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": tb, "analysis": {"type": "op"}, "substitute": subst})
        new = subst["l/div"]
        assert err and f"substitute l/div -> {new}: another cell named {new[2:]} is netlisted in this testbench as well " \
            f"({other}), and a netlist has one subcircuit per name" in res, res
    # the same cell placed as itself and as a stand-in is one cell
    assert "\nx3 in o3 0 div_alt\n" in srv.sim_netlist(srv.project("p"), "l", "tb2", substitute={"l/div": "l/div_alt"}).text
    shutil.rmtree(root)


def test_what_a_standin_hides_is_named(monkeypatch):
    """A cell simulated as extracted, or varied, that lies inside a replaced cell: the message names the stand-in."""
    root, srv, lib, ctx = _setup()
    (lib / "tbs.sch").write_text(_sch([("devices/vsource.sym", {"name": "V1", "value": "1"}, {"p": "in", "m": "0"}),
                                       ("stage.sym", {"name": "x1"}, {"in": "in", "out": "o1", "vss": "0"})]))
    d = srv.jobs.runs_dir(srv.project("p").path) / "20260101-000000-pex0"
    d.mkdir(parents=True)
    (d / "extracted.spice").write_text(".subckt div in out vss\nR1 in out 1k\nR2 out vss 4k\n.ends\n")
    (d / "status.json").write_text(json.dumps({"id": d.name, "kind": "pex", "state": "done", "mode": "c"}))
    monkeypatch.setattr(srv, "extraction", lambda project, lib_, cell, want=None: {"id": d.name, "mode": "c", "files": {}})
    with pytest.raises(ValueError, match=r"l/div is not netlisted from a schematic in l/tbs: it lies inside l/stage, "
                                         r"which is netlisted as its stand-in l/div_alt \(substitute\)"):
        srv.start_sim("p", "l", "tbs", {"type": "op"}, extracted=["l/div"], substitute={"l/stage": "l/div_alt"})
    with pytest.raises(ValueError, match=r"vary / conditions on div: it lies inside l/stage, which is netlisted as its "
                                         r"stand-in l/div_alt \(substitute\), so its schematic's properties do not apply$"):
        srv.start_sim("p", "l", "tbs", {"type": "op"}, overrides={"div": {"R2": {"value": "2k"}}},
                      substitute={"l/stage": "l/div_alt"})
    with pytest.raises(ValueError, match=r"vary / conditions on stage: it is simulated as extracted from its layout"):
        srv.start_sim("p", "l", "tbs", {"type": "op"}, overrides={"stage": {"x1": {"name": "x1"}}}, extracted=["l/stage"])
    # the testbench's default: the refusal says where the stand-in comes from and how to take it back
    _spec(lib, "tbs", [{"id": "c1", "kind": "constraint", "text": "the stage as a divider",
                        "rule": {"type": "substitute", "cells": {"l/stage": "l/div_alt"}}}])
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "tbs", "analysis": {"type": "op"}, "extracted": ["l/stage"]})
    assert err and "l/stage is simulated both as extracted and as its stand-in l/div_alt (substitute: the testbench's " \
        "default, its intent's c1): one or the other; substitute {\"l/stage\": null} simulates it as itself" in res, res
    shutil.rmtree(root)


# ------------------------------------------------------------------ with ngspice


def test_the_simulated_values_change_and_the_result_says_why():
    _need()
    root, srv, lib, ctx = _setup()
    plain = _op(ctx)
    assert plain["node_voltages"]["o1"] == pytest.approx(0.5) and plain["node_voltages"]["o2"] == pytest.approx(0.5)
    assert "substitute" not in plain
    r = _op(ctx, substitute={"l/div": "l/div_alt"})
    assert r["node_voltages"]["o1"] == pytest.approx(0.75) and r["node_voltages"]["o2"] == pytest.approx(0.75)
    assert r["substitute"] == {"l/div": "l/div_alt"} and "substitute_warnings" not in r
    assert "every instance of l/div, at any depth, was netlisted as l/div_alt (substitute)" in r["notes"]
    st = srv.jobs.get(srv.project("p").path, r["job"]).status()
    assert st["inputs"]["design"][0]["substitute"] == {"l/div": "l/div_alt"}             # the job's provenance
    # a schematic without a symbol stands in with the original's
    assert _op(ctx, substitute={"l/div": "l/div9"})["node_voltages"]["o2"] == pytest.approx(0.9)
    # a substitution that replaces nothing is a warning, not silence: already while the run is going
    job = srv.start_sim("p", "l", "tb", {"type": "tran", "step": "1n", "stop": "20u"},
                        substitute={"l/div_alt": "l/div9", "l/div": "l/div9"})
    p = simresults.pending(job, "l", "tb")
    assert p["substitute"] == {"l/div": "l/div9"} and "l/div_alt is not netlisted" in p["substitute_warnings"][0]
    assert "every instance of l/div, at any depth, was netlisted as l/div9 (substitute)" in p["notes"]
    while job.status()["state"] in ("queued", "running"):
        time.sleep(0.1)
    r = _op(ctx, substitute={"l/div_alt": "l/div9", "l/div": "l/div9"})
    assert r["substitute"] == {"l/div": "l/div9"} and r["substitute_warnings"] == [
        "substitute l/div_alt -> l/div9: l/div_alt is not netlisted in l/tb (not placed in it or below it, or only inside "
        "a cell simulated as extracted or as another stand-in): nothing was replaced"]
    # the stage replaced as a whole (with the div inside it), the div at depth 1 by another stand-in
    r = _op(ctx, substitute={"l/stage": "l/div_alt", "l/div": "l/div9"})
    assert r["node_voltages"]["o1"] == pytest.approx(0.75) and r["node_voltages"]["o2"] == pytest.approx(0.9)
    shutil.rmtree(root)


def test_a_run_with_standins_is_never_taken_for_one_without():
    _need()
    root, srv, lib, ctx = _setup()
    op = {"type": "op"}
    plain = srv.start_sim("p", "l", "tb", op, reuse=True)
    alt = srv.start_sim("p", "l", "tb", op, reuse=True, substitute={"l/div": "l/div_alt"})
    assert alt.id != plain.id and alt.status()["key"] != plain.status()["key"]
    assert srv.start_sim("p", "l", "tb", op, reuse=True).id == plain.id
    assert srv.start_sim("p", "l", "tb", op, reuse=True, substitute={"l/div": "l/div_alt"}).id == alt.id
    # a sweep's netlist entry (property overrides) is the testbench's with its stand-ins too
    swept = srv.start_sim("p", "l", "tb", op, reuse=True, overrides={"tb": {"V1": {"value": "2"}}},
                          substitute={"l/div": "l/div_alt"})
    for job in (plain, alt, swept):
        assert provenance.freshness(srv, "p", job.status()) == {"fresh": True, "changed": []}
    # the stand-in changes: the runs with it are outdated, the run without is not; the original changes: the reverse
    (lib / "div_alt.sch").write_text(_divider("4k"))
    assert provenance.freshness(srv, "p", alt.status())["changed"] == ["schematic netlist of l/tb changed"]
    assert provenance.freshness(srv, "p", swept.status())["fresh"] is False
    assert provenance.freshness(srv, "p", plain.status())["fresh"] is True
    (lib / "div_alt.sch").write_text(_divider("3k"))
    (lib / "div.sch").write_text(_divider("2k"))
    assert provenance.freshness(srv, "p", alt.status())["fresh"] is True
    assert provenance.freshness(srv, "p", plain.status())["fresh"] is False
    # the stand-in's pins change so that it no longer stands in: outdated, not an error
    (lib / "div_alt.sym").write_text(_sym(["in", "vss", "out"]))
    assert provenance.freshness(srv, "p", alt.status())["fresh"] is False
    for job in (plain, alt, swept):
        while job.status()["state"] in ("queued", "running"):
            time.sleep(0.1)
    shutil.rmtree(root)


def _spec(lib: Path, cell: str, items: list[dict]) -> None:
    intent.IntentEditor(intent.path_of(lib, cell))._write(intent.normalize({"items": items}))


def test_a_specs_own_standins_and_the_testbenchs_default():
    _need()
    root, srv, lib, ctx = _setup()
    op = {"type": "op"}
    _spec(lib, "stage", [{"id": "s1", "kind": "spec", "text": "o1 at least 0.7 V with the 3/4 divider",
                          "spec": {"measure": "v(o1)", "min": 0.7, "testbench": "tb", "analysis": op,
                                   "substitute": {"l/div": "l/div_alt"}}},
                         {"id": "s2", "kind": "spec", "text": "o2 at most 0.6 V as drawn",
                          "spec": {"measure": "v(o2)", "max": 0.6, "testbench": "tb", "analysis": op}}])
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "stage"})
    assert not err, res
    r = json.loads(res)
    by = {s["id"]: s for s in r["specs"]}
    assert r["met"] == "2/2" and by["s1"]["worst"] == pytest.approx(0.75) and by["s2"]["worst"] == pytest.approx(0.5)
    assert by["s1"]["substitute"] == {"l/div": "l/div_alt"} and "substitute" not in by["s2"]
    assert by["s1"]["job"] != by["s2"]["job"] and r["runs"] == {"new": 2, "reused": 0}  # never one run for both
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "stage", "temps": [27, 60]})
    pt = json.loads(res)["points"][0]
    assert pt["values"] == {"s1": pytest.approx(0.75), "s2": pytest.approx(0.5)} and \
        set(pt["jobs"]) == {"tb (s1)", "tb (s2)"} and pt["jobs"]["tb (s1)"] != pt["jobs"]["tb (s2)"]
    # the call's stand-ins over the spec's: null measures the cell itself
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "stage", "ids": ["s1"], "substitute": {"l/div": None}})
    s1 = json.loads(res)["specs"][0]
    assert s1["status"] == "not met" and s1["worst"] == pytest.approx(0.5) and "substitute" not in s1
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "stage", "substitute": {"l/div": "l/div9"}})
    by = {s["id"]: s for s in json.loads(res)["specs"]}
    assert by["s2"]["worst"] == pytest.approx(0.9) and by["s2"]["status"] == "not met" and by["s2"]["substitute"] == {"l/div": "l/div9"}
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "stage", "ids": ["s2"], "substitute": {"l/div_alt": "l/div9"}})
    s2 = json.loads(res)["specs"][0]
    assert s2["status"] == "met" and "nothing was replaced" in s2["substitute_warnings"][0]
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "stage", "ids": ["s2"], "substitute": {"l/div": "l/div_bad"}})
    assert not err and "pins are not those of l/div" in json.loads(res)["specs"][0]["why"]
    # the testbench's default: a substitute rule of its own intent, for every run of it (run_simulation, the panel)
    _spec(lib, "tb", [{"id": "c1", "kind": "constraint", "text": "the 3/4 divider: the plain one is too slow",
                       "rule": {"type": "substitute", "cells": {"l/div": "l/div_alt"}}}])
    r = _op(ctx)
    assert r["node_voltages"]["o2"] == pytest.approx(0.75) and r["substitute"] == {"l/div": "l/div_alt"}
    assert "every instance of l/div, at any depth, was netlisted as l/div_alt (substitute: the testbench's default, its " \
           "intent's c1)" in r["notes"]
    assert _op(ctx, substitute={"l/div": None})["node_voltages"]["o2"] == pytest.approx(0.5)
    assert _op(ctx, substitute={"l/div": "l/div9"})["node_voltages"]["o2"] == pytest.approx(0.9)
    job = srv.jobs.get(srv.project("p").path, r["job"])
    assert job.status()["inputs"]["design"][0]["substitute"] == {"l/div": "l/div_alt"}
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "stage", "ids": ["s2"]})
    s2 = json.loads(res)["specs"][0]
    assert s2["worst"] == pytest.approx(0.75) and s2["substitute"] == {"l/div": "l/div_alt"}   # under the spec's own
    _spec(lib, "tb", [{"id": "c1", "kind": "constraint", "text": "broken", "rule": {"type": "substitute", "cells": {"l/div": "x"}}}])
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "tb", "analysis": op})
    assert err and "the intent of l/tb (tb.intent.yaml), c1: substitute: 'x' is not lib/cell" in res
    # only the substitute rules are read: what else is wrong in the file does not stop a run, and a file that is not
    # YAML at all is a warning (its stand-ins were not read), not a refusal
    ipath = intent.path_of(lib, "tb")
    ipath.write_text("version: 1\nnotes: [a, b]\nitems:\n  - {id: c1, kind: context, text: driven by 1 V}\n"
                     "  - {id: c1, kind: constraint, text: 3/4, rule: {type: substitute, cells: {l/div: l/div_alt}}}\n")
    r = _op(ctx)
    assert r["node_voltages"]["o2"] == pytest.approx(0.75) and "substitute_warnings" not in r
    ipath.write_text("items: [\n")
    r = _op(ctx)
    assert r["node_voltages"]["o2"] == pytest.approx(0.5) and "substitute" not in r
    assert r["substitute_warnings"][0].startswith("the intent of l/tb: tb.intent.yaml is not YAML (") and \
        r["substitute_warnings"][0].endswith("so its default stand-ins (substitute rules) were not read")
    ipath.write_text("items:\n  - {id: c1, kind: constraint, text: x, rule: {type: substitute, cells: [l/div]}}\n")
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "tb", "analysis": op})
    assert err and "the intent of l/tb (tb.intent.yaml), c1: a substitute rule's cells map a cell to its stand-in" in res
    shutil.rmtree(root)


def test_a_cell_simulated_as_extracted_next_to_a_standin(monkeypatch):
    """stage from an extraction (its job: a subcircuit with R2 4k), div as its stand-in: o1 from the layout, o2 from the
    stand-in; the div inside stage is not netlisted, so its substitution there replaces nothing."""
    _need()
    root, srv, lib, ctx = _setup()
    d = srv.jobs.runs_dir(srv.project("p").path) / "20260101-000000-pex0"
    d.mkdir()
    (d / "extracted.spice").write_text(".subckt stage in out vss\nR1 in out 1k\nR2 out vss 4k\n.ends\n")
    (d / "status.json").write_text(json.dumps({"id": d.name, "kind": "pex", "state": "done", "mode": "c"}))
    monkeypatch.setattr(srv, "extraction", lambda project, lib_, cell, want=None: {"id": d.name, "mode": "c", "files": {}})
    r = _op(ctx, extracted=["l/stage"], substitute={"l/div": "l/div_alt"})
    assert r["node_voltages"]["o1"] == pytest.approx(0.8) and r["node_voltages"]["o2"] == pytest.approx(0.75)
    assert r["extracted"][0]["cell"] == "l/stage" and r["substitute"] == {"l/div": "l/div_alt"}
    netlist = (srv.jobs.get(srv.project("p").path, r["job"]).path / "netlist.cir").read_text()
    assert "\nx2 in o2 0 div_alt\n" in netlist and "R2 out vss 4k" in netlist and ".subckt div " not in netlist
    r = _op(ctx, extracted=["l/stage"], substitute={"l/stage": None, "l/div_alt": "l/div9"})
    assert "l/div_alt is not netlisted" in r["substitute_warnings"][0]
    shutil.rmtree(root)
