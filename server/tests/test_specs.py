"""Specs measured (specs.py, check_specs, sweep): measures read from a run, margins against limits, sweep grids,
property overrides netlisted without editing (also inside a subcircuit), corners and temperatures, reused runs, the
worst case over failed points, per-corner limits and a spec's own corners, settling time and derived measures, simulator
options, Monte Carlo mismatch."""
import json
import math
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine.agents import agent_tools as T  # noqa: E402
from undine.design import intent  # noqa: E402
from undine.schematics import schematic, xschem
from undine.simulation import derived, sim, specs
from test_netlist import R_SYM, V, _devices  # noqa: E402
from test_simresults import _need, _project, _tb  # noqa: E402


def test_measures_are_read_from_what_a_run_shows():
    summ = {"measurements": {"gbw": 2.4e7}, "metrics": {"dc_gain_db": 61.2, "phase_margin_deg": 58.0},
            "node_voltages": {"out": 0.9}, "signals": {"v(out)": {"max": 1.79, "final": 1.2}},
            "transistors": [{"device": "x1.xm1", "gm": 1e-4, "vds_minus_vdsat": 0.12}]}
    assert specs.measure(summ, "GBW") == 2.4e7                                # .meas names, any case
    assert specs.measure(summ, "dc_gain_db") == 61.2 and specs.measure(summ, "f_3db_hz") is None
    assert specs.measure(summ, "v(out)") == 0.9
    assert specs.measure(summ, "v(out).max") == 1.79 and specs.measure(summ, "out.final") == 1.2
    assert specs.measure(summ, "x1.xm1.vds_minus_vdsat") == 0.12 and specs.measure(summ, "x1.xm2.gm") is None
    assert "gbw" in specs.available(summ) and "v(out)" in specs.available(summ)


def test_margins_against_limits():
    assert specs.verdict(65, 60, None) == {"pass": True, "margin": 5, "margin_rel": pytest.approx(5 / 60, rel=1e-3)}
    assert specs.verdict(55, 60, None)["pass"] is False and specs.verdict(55, 60, None)["margin"] == -5
    v = specs.verdict(1.0, 0.5, 1.1)                                          # the nearer limit counts
    assert v["pass"] and v["margin"] == pytest.approx(0.1)
    assert specs.verdict(0.1, None, 0.0) == {"pass": False, "margin": -0.1, "margin_rel": -0.1}   # limit 0: absolute
    assert specs.verdict(None, 1, None) == {"pass": None}


def test_sweep_grids():
    pts, names = specs.grid({"M1.W": [1, 2], "tb/V1.value": {"from": 1, "to": 100, "steps": 3, "scale": "log"}},
                            ["tt", "ss"], [27], "ota")
    assert names == ["ota/M1.W", "tb/V1.value"] and len(pts) == 2 * 3 * 2
    assert pts[0] == {"overrides": {"ota": {"M1": {"W": "1"}}, "tb": {"V1": {"value": "1"}}}, "corner": "tt", "temp": 27.0}
    assert [p["overrides"]["tb"]["V1"]["value"] for p in pts[:6:2]] == ["1", "10", "100"]
    assert specs.label(pts[-1]) == {"ota/M1.W": "2", "tb/V1.value": "100", "corner": "ss", "temp": 27.0}
    assert specs.grid({"M1.W": {"from": 1, "to": 2, "steps": 3}}, None, None, "c")[0][1]["overrides"]["c"]["M1"]["W"] == "1.5"
    assert specs.grid(None, None, None, "c")[0] == [{"overrides": {}}]
    pts, names = specs.grid({"M1.W": [1, 2]}, None, None, "c", conditions={"tb/V1.value": ["1.6", "2"]})
    assert names == ["c/M1.W"] and len(pts) == 4                              # conditions are axes, not design choices


def test_a_design_point_is_judged_by_its_worst_condition():
    def row(w, vdd, m):
        return {"point": {"c/M1.W": w, "tb/V1.value": vdd}, "results": {"s1": {"value": m, **specs.verdict(m, 1.0, None)}}}
    folded = specs.fold([row("1", "1.6", 0.9), row("1", "2", 1.5), row("2", "1.6", 1.1), row("2", "2", 1.3)], ["c/M1.W"])
    assert [f["point"] for f in folded] == [{"c/M1.W": "1"}, {"c/M1.W": "2"}]
    assert folded[0]["results"]["s1"]["value"] == 0.9 and folded[0]["all_pass"] is False
    assert folded[0]["results"]["s1"]["worst_at"] == {"tb/V1.value": "1.6"} and folded[0]["conditions"] == 2
    assert folded[1]["all_pass"] and folded[1]["score"] == pytest.approx(0.1)
    for bad in ({"W": [1]}, {"M1.W": []}, {"M1.W": {"from": 0, "to": 1, "scale": "log"}}):
        with pytest.raises(specs.SpecError):
            specs.grid(bad, None, None, "c")


def test_specs_come_from_the_intent():
    model = {"items": [{"id": "g1", "kind": "goal", "text": "fast"},
                       {"id": "s1", "kind": "spec", "text": "f3dB", "spec": {"measure": "f_3db_hz", "min": "100k", "testbench": "tb",
                                                                              "output": "out", "analysis": {"type": "ac"}}},
                       {"id": "s2", "kind": "spec", "text": "other lib", "spec": {"measure": "gbw", "max": 5, "testbench": "o/tb2"}}]}
    p = specs.probes(model, "l")
    assert [x["id"] for x in p] == ["s1", "s2"] and p[0]["min"] == 1e5 and p[0]["lib"] == "l"
    assert (p[1]["lib"], p[1]["testbench"], p[1]["max"]) == ("o", "tb2", 5.0)
    with pytest.raises(specs.SpecError, match="no spec s9"):
        specs.probes(model, "l", ["s9"])
    # the intent panel says what check_specs would refuse
    bad = {"items": [{"id": "s1", "kind": "spec", "text": "a", "spec": {"measure": "phase_margin_deg", "min": 45, "testbench": "tb"}},
                     {"id": "s2", "kind": "spec", "text": "b", "spec": {"measure": "gbw", "min": 1, "analysis": {"type": "noise"}}}]}
    msgs = {p["entry"]: p["message"] for p in intent.validate(bad, None)}
    assert "name the output node" in msgs["items.s1"] and "analysis is" in msgs["items.s2"]


def test_overrides_are_netlisted_inside_a_subcircuit_without_editing(tmp_path):
    lib = tmp_path / "l"
    lib.mkdir()
    (lib / "r.sym").write_text(R_SYM)
    (lib / "div.sch").write_text(V + "\n".join([
        "C {devices/ipin.sym} -100 -30 0 0 {name=p1 lab=in}", "C {devices/opin.sym} 300 -30 0 0 {name=p2 lab=out}",
        "C {r.sym} 0 0 0 0 {name=R1 value=1k}", "C {r.sym} 100 0 0 0 {name=R2 value=2k}",
        "N -100 -30 0 -30 {lab=in}", "N 0 30 100 -30 {lab=out}", "N 100 30 300 -30 {lab=out}"]) + "\n")
    (lib / "div.sym").write_text('v {xschem version=3.4.5 file_version=1.2}\nK {type=subcircuit\nformat="@name @pinlist @symname"\n'
                                 'template="name=x1"}\nB 5 -2.5 -32.5 2.5 -27.5 {name=in dir=in}\nB 5 -2.5 27.5 2.5 32.5 {name=out dir=out}\n')
    (lib / "tb.sch").write_text(V + "C {div.sym} 0 0 0 0 {name=x1}\nC {r.sym} 200 0 0 0 {name=RL value=10k}\n"
                                    "N 0 -30 0 -60 {lab=a}\nN 0 30 200 -30 {lab=b}\nN 200 30 200 60 {lab=0}\n")
    res = xschem.SymbolResolver([lib, *_devices()])
    before = (lib / "div.sch").read_text()
    plain = schematic.spice(lib / "tb.sch", res, top_subckt=False).text
    text = schematic.spice(lib / "tb.sch", res, top_subckt=False, overrides={"div": {"R2": {"value": "5k"}}, "tb": {"RL": {"value": "1Meg"}}}).text
    assert "\nR2 out out 2k\n" in plain and "\nR2 out out 5k\n" in text and "\nR1 in out 1k\n" in text
    assert "RL b 0 1Meg" in text and "RL b 0 10k" in plain
    assert (lib / "div.sch").read_text() == before


# ------------------------------------------------------------------ with ngspice


def _rc(srv):
    ctx = _tb(srv, "rc", [("devices/vsource.sym", 0, {"name": "V1", "value": "dc 0 ac 1"}),
                          ("devices/res.sym", 100, {"name": "R1", "value": "1k"}),
                          ("devices/capa.sym", 200, {"name": "C1", "value": "1n"})],
              {"in": ["V1:p", "R1:P"], "out": ["R1:M", "C1:p"], "0": ["V1:m", "C1:m"]})
    ac = {"type": "ac", "points": "50", "fstart": "1k", "fstop": "100Meg"}
    for text, spec in (("bandwidth at least 100 kHz", {"measure": "f_3db_hz", "min": 100e3}),
                       ("bandwidth at most 300 kHz", {"measure": "f_3db_hz", "max": 300e3}),
                       ("passband flat", {"measure": "dc_gain_db", "min": -0.1})):
        res, err = T.call(ctx, "add_intent", {"lib": "l", "cell": "rc", "kind": "spec", "text": text, "reason": "spec",
                                              "spec": {**spec, "testbench": "rc", "output": "out", "input": "in", "analysis": ac}})
        assert not err, res
    return ctx


def test_rc_specs_checked_and_swept_without_editing():
    _need()
    root, srv = _project()
    ctx = _rc(srv)
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "rc"})
    assert not err, res
    r = json.loads(res)
    f3 = 1 / (2 * math.pi * 1e3 * 1e-9)                                       # 159 kHz
    by = {s["id"]: s for s in r["specs"]}
    assert r["met"] == "3/3" and by["s1"]["worst"] == pytest.approx(f3, rel=0.02) and by["s1"]["margin"] > 0
    sch = (srv.project(ctx.work).library("l").path / "rc.sch").read_text()
    res, err = T.call(ctx, "sweep", {"lib": "l", "cell": "rc", "vary": {"C1.value": ["0.4n", "1n", "2n"]}})
    assert not err, res
    s = json.loads(res)
    pts = s["points"]
    assert [p["point"]["rc/C1.value"] for p in pts] == ["0.4n", "1n", "2n"]
    assert pts[0]["not_met"] == ["s2"] and pts[2]["not_met"] == ["s1"] and pts[1]["all_pass"]   # too wide, too narrow
    assert s["best"][0]["point"]["rc/C1.value"] == "1n" and pts[2]["values"]["s1"] == pytest.approx(f3 / 2, rel=0.03)
    assert {w["id"]: w["status"] for w in s["worst_case"]} == {"s1": "not met", "s2": "not met", "s3": "met"}
    assert (srv.project(ctx.work).library("l").path / "rc.sch").read_text() == sch        # nothing was written
    # the resistor's tolerance as a condition: each capacitor value is judged at its worse end
    res, err = T.call(ctx, "sweep", {"lib": "l", "cell": "rc", "vary": {"C1.value": ["1n"]},
                                     "conditions": {"rc/R1.value": ["0.9k", "1.1k"]}})
    assert not err, res
    c = json.loads(res)
    assert c["conditions_per_point"] == 2 and len(c["points"]) == 1
    w = c["points"][0]["worst_at"]
    assert w["s1"] == {"rc/R1.value": "1.1k"} and w["s2"] == {"rc/R1.value": "0.9k"}       # narrowest, widest
    assert c["points"][0]["values"]["s1"] == pytest.approx(f3 / 1.1, rel=0.03)
    # asked again: the runs are reused, not repeated
    jobs = len(srv.jobs.list(srv.project(ctx.work).path))
    res, err = T.call(ctx, "sweep", {"lib": "l", "cell": "rc", "vary": {"C1.value": ["0.4n", "1n", "2n"]}})
    assert not err and len(srv.jobs.list(srv.project(ctx.work).path)) == jobs and json.loads(res)["points"] == pts
    # what cannot be swept says why
    for vary, msg in (({"C9.value": ["1n"]}, "no instance C9"), ({"C1.valu": ["1n"]}, "no property valu"),
                      ({"value": ["1n"]}, "instance.property"), ({}, "design choices")):
        res, err = T.call(ctx, "sweep", {"lib": "l", "cell": "rc", "vary": vary})
        assert err and msg in res, res
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "rc", "corners": ["tt"]})
    assert err and "no corner tt" in res                                      # no PDK in this project
    shutil.rmtree(root)


def test_a_spec_that_cannot_be_measured_says_why():
    _need()
    root, srv = _project()
    ctx = _rc(srv)
    T.call(ctx, "add_intent", {"lib": "l", "cell": "rc", "kind": "spec", "text": "no output", "reason": "r",
                               "spec": {"measure": "unity_gain_freq_hz", "min": 1, "testbench": "rc", "analysis": {"type": "ac"}}})
    T.call(ctx, "add_intent", {"lib": "l", "cell": "rc", "kind": "spec", "text": "typo", "reason": "r",
                               "spec": {"measure": "v(nowhere)", "max": 1, "testbench": "rc", "analysis": {"type": "op"}}})
    T.call(ctx, "add_intent", {"lib": "l", "cell": "rc", "kind": "spec", "text": "no testbench", "reason": "r",
                               "spec": {"measure": "gbw", "min": 1}})
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "rc", "ids": ["s4", "s5", "s6"]})
    assert not err, res
    by = {s["id"]: s for s in json.loads(res)["specs"]}
    assert by["s4"]["status"] == "not measured" and "needs output" in by["s4"]["why"]
    assert "no measure 'v(nowhere)'" in by["s5"]["why"] and "v(out)" in by["s5"]["why"]
    assert by["s6"]["status"].startswith("no testbench")
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "nothing"})
    assert err
    shutil.rmtree(root)


def test_sky130_corners_and_temperatures():
    _need()
    root, srv = _project("sky130A")
    ctx = _tb(srv, "nmos", [("devices/vsource.sym", 0, {"name": "V1", "value": "1.8"}),
                            ("devices/isource.sym", 100, {"name": "I1", "value": "10u"}),
                            ("sky130_fd_pr/nfet_01v8.sym", 200, {"name": "M1", "W": "2", "L": "1"})],
              {"vdd": ["V1:p", "I1:p"], "g": ["I1:m", "M1:D", "M1:G"], "0": ["V1:m", "M1:S", "M1:B"]})
    res, err = T.call(ctx, "add_intent", {"lib": "l", "cell": "nmos", "kind": "spec", "text": "vgs under 0.75 V", "reason": "r",
                                          "spec": {"measure": "v(g)", "max": 0.75, "testbench": "nmos", "analysis": {"type": "op"}}})
    assert not err, res
    res, err = T.call(ctx, "sweep", {"lib": "l", "cell": "nmos", "vary": {"M1.W": [1, 4]}, "corners": ["ss", "ff"]})
    assert not err, res
    pts = {p["point"]["nmos/M1.W"]: p for p in json.loads(res)["points"]}
    assert set(pts) == {"1", "4"} and all(p["worst_at"]["s1"] == {"corner": "ss"} for p in pts.values())   # slow: higher vgs
    assert pts["4"]["values"]["s1"] < pts["1"]["values"]["s1"]                                             # wider: lower vgs
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "nmos", "corners": ["ss", "ff"]})
    by = {p["point"]["corner"]: p["score"] for p in json.loads(res)["points"]}
    assert not err and by["ff"] > by["ss"]
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "nmos", "corners": ["ss"], "temps": [125]})
    assert not err, res
    job = srv.jobs.get(srv.project(ctx.work).path, json.loads(res)["specs"][0]["job"])     # the run behind the worst value
    text = (job.path / "netlist.cir").read_text()
    assert ".temp 125" in text and " ss\n" in text and text.count(".lib ") == 1
    assert job.status()["models"][0]["section"] == "ss" and "reduced" in job.status()["models"][0]
    assert T._corners(ctx, ["all"]) == ["tt", "ss", "ff", "sf", "fs"]
    shutil.rmtree(root)


# ------------------------------------------------------------------ a spec's own .meas


def test_meas_in_a_spec_is_parsed_and_checked():
    model = {"items": [{"id": "s1", "kind": "spec", "text": "delay", "spec": {
        "measure": "tphl", "max": "50p", "testbench": "tb", "analysis": {"type": "tran"},
        "meas": ".meas tran tphl TRIG v(in) VAL=0.9 RISE=1 TARG v(out) VAL=0.9 FALL=1"}}]}
    p = specs.probes(model, "l")[0]
    assert p["meas"] == [".meas tran tphl TRIG v(in) VAL=0.9 RISE=1 TARG v(out) VAL=0.9 FALL=1"] and p["max"] == 50e-12
    assert intent.meas_names(p["meas"]) == {"tphl": "tran"}
    with pytest.raises(specs.SpecError, match="one .meas statement"):
        specs.probes({"items": [{"id": "s1", "kind": "spec", "text": "x", "spec": {"measure": "a", "min": 1, "meas": "print a"}}]}, "l")
    with pytest.raises(specs.SpecError, match="define .meas d differently"):
        specs._group_meas([{"testbench": "tb", "meas": [".meas tran d TRIG v(a) VAL=1 RISE=1 TARG v(b) VAL=1 RISE=1"]},
                           {"testbench": "tb", "meas": [".meas tran d TRIG v(a) VAL=1 FALL=1 TARG v(b) VAL=1 FALL=1"]}])
    same = ".meas tran d TRIG v(a) VAL=1 RISE=1 TARG v(b) VAL=1 RISE=1"
    assert specs._group_meas([{"testbench": "tb", "meas": [same]}, {"testbench": "tb", "meas": [same]}]) == [same]
    # the intent panel says what would not measure
    bad = {"items": [{"id": "s1", "kind": "spec", "text": "a", "spec": {"measure": "tplh", "max": 1, "testbench": "tb",
                                                                         "meas": ".meas tran tphl TRIG v(in) VAL=0.9 RISE=1 TARG v(out) VAL=0.9 FALL=1"}},
                     {"id": "s2", "kind": "spec", "text": "b", "spec": {"measure": "g", "min": 1, "analysis": {"type": "ac"},
                                                                         "meas": ".meas tran g AVG v(out)"}},
                     {"id": "s3", "kind": "spec", "text": "c", "spec": {"measure": "g", "min": 1, "meas": ["not a meas"]}}]}
    msgs = {p["entry"]: p["message"] for p in intent.validate(bad, None)}
    assert "not what its meas defines (tphl)" in msgs["items.s1"]
    assert "measures a tran analysis, but the spec runs ac" in msgs["items.s2"] and "one .meas statement" in msgs["items.s3"]


def test_delays_measured_by_the_specs_own_meas_without_changing_the_testbench():
    _need()
    root, srv = _project()
    ctx = _tb(srv, "rcd", [("devices/vsource.sym", 0, {"name": "V1", "value": "pulse(0 1 1u 1n 1n 10u 20u)"}),
                           ("devices/res.sym", 100, {"name": "R1", "value": "1k"}),
                           ("devices/capa.sym", 200, {"name": "C1", "value": "1n"})],
               {"in": ["V1:p", "R1:P"], "out": ["R1:M", "C1:p"], "0": ["V1:m", "C1:m"]})
    tran = {"type": "tran", "step": "10n", "stop": "8u"}
    for text, spec in (
            ("delay under 1 us", {"measure": "tdelay", "max": 1e-6,
                                  "meas": ".meas tran tdelay TRIG v(in) VAL=0.5 RISE=1 TARG v(out) VAL=0.5 RISE=1"}),
            ("rise time under 3 us", {"measure": "trise", "max": 3e-6,
                                      "meas": [".meas tran trise TRIG v(out) VAL=0.1 RISE=1 TARG v(out) VAL=0.9 RISE=1"]}),
            ("never reached", {"measure": "tnever", "max": 1e-6,
                               "meas": ".meas tran tnever TRIG v(in) VAL=0.5 RISE=1 TARG v(out) VAL=2 RISE=1"})):
        res, err = T.call(ctx, "add_intent", {"lib": "l", "cell": "rcd", "kind": "spec", "text": text, "reason": "r",
                                              "spec": {**spec, "testbench": "rcd", "analysis": tran}})
        assert not err, res
    sch = srv.project(ctx.work).library("l").path / "rcd.sch"
    before = sch.read_text()
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "rcd"})
    assert not err, res
    by = {s["id"]: s for s in json.loads(res)["specs"]}
    tau = 1e3 * 1e-9
    assert by["s1"]["status"] == "met" and by["s1"]["worst"] == pytest.approx(tau * math.log(2), rel=0.03)
    assert by["s2"]["status"] == "met" and by["s2"]["worst"] == pytest.approx(tau * math.log(9), rel=0.03)
    assert by["s3"]["status"] == "not measured" and "gave no value" in by["s3"]["why"] and "out of interval" in by["s3"]["why"]
    assert by["s1"]["job"] == by["s2"]["job"]                                 # one run for the specs of one testbench
    text = (srv.jobs.get(srv.project(ctx.work).path, by["s1"]["job"]).path / "netlist.cir").read_text()
    assert text.count(".meas tran") == 3 and ".control" in text and sch.read_text() == before
    res, err = T.call(ctx, "sweep", {"lib": "l", "cell": "rcd", "vary": {"C1.value": ["1n", "2n"]}, "ids": ["s1", "s2"]})
    pts = {p["point"]["rcd/C1.value"]: p for p in json.loads(res)["points"]}
    assert not err and pts["2n"]["values"]["s1"] == pytest.approx(2 * tau * math.log(2), rel=0.03) and pts["2n"]["not_met"] == ["s1", "s2"] and pts["1n"]["all_pass"]
    shutil.rmtree(root)


def test_a_spec_can_count_a_meas_that_never_triggers_as_not_met():
    """"The DAC controls cleared after clk rose": a control that never clears gives its .meas no value, which such a
    spec (missing: fail) counts as not met, where any other spec says it is not measured. A .meas that could not run (a
    misspelt node) or a measure the run has no .meas of at all is a wrong spec, not a failure of the design: it stays
    not measured."""
    _need()
    root, srv = _project()
    ctx = _tb(srv, "rcm", [("devices/vsource.sym", 0, {"name": "V1", "value": "pulse(0 1 1u 1n 1n 10u 20u)"}),
                           ("devices/res.sym", 100, {"name": "R1", "value": "1k"}),
                           ("devices/capa.sym", 200, {"name": "C1", "value": "1n"})],
               {"in": ["V1:p", "R1:P"], "out": ["R1:M", "C1:p"], "0": ["V1:m", "C1:m"]})
    tran = {"type": "tran", "step": "10n", "stop": "8u"}
    delay = ".meas tran tdelay TRIG v(in) VAL=0.5 RISE=1 TARG v(out) VAL=0.5 RISE=1"
    never = ".meas tran tnever TRIG v(in) VAL=0.5 RISE=1 TARG v(out) VAL=2 RISE=1"       # out stays below 2
    for text, spec in (("clears within 1 us", {"measure": "tnever", "max": 1e-6, "meas": never, "missing": "fail"}),
                       ("the same, not measured", {"measure": "tnever", "max": 1e-6, "meas": never}),
                       ("both edges", {"measure": "max(tdelay, tnever)", "max": 1e-6, "meas": [delay, never],
                                       "missing": "fail"}),
                       ("misnamed", {"measure": "tnone", "max": 1, "meas": never, "missing": "fail"}),
                       ("delay", {"measure": "tdelay", "max": 1e-6, "meas": delay, "missing": "fail"}),
                       ("misspelt node", {"measure": "tbad", "max": 1e-6, "missing": "fail",
                                          "meas": ".meas tran tbad TRIG v(in) VAL=0.5 RISE=1 TARG v(outt) VAL=0.5 RISE=1"}),
                       ("twice the clear", {"measure": "tpar", "max": 1e-6, "missing": "fail",
                                            "meas": [never, ".meas tran tpar param='tnever*2'"]})):
        res, err = T.call(ctx, "add_intent", {"lib": "l", "cell": "rcm", "kind": "spec", "text": text, "reason": "r",
                                              "spec": {**spec, "testbench": "rcm", "analysis": tran}})
        assert not err, res
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "rcm"})
    assert not err, res
    out = json.loads(res)
    by = {s["id"]: s for s in out["specs"]}
    nv = by["s1"]["no_value_why"]
    assert by["s1"]["status"] == "not met" and nv.startswith("no value") and "out of interval" in nv
    assert "why" not in by["s1"]                                # its own reason: no_value_why
    assert by["s1"]["points_met"] == "0/1" and by["s1"]["no_value_at"] == [{}] and by["s1"]["worst"] is None
    assert by["s2"]["status"] == "not measured" and "gave no value" in by["s2"]["why"]          # the default: as before
    assert by["s3"]["status"] == "not met" and by["s3"]["no_value_why"].startswith("no value")
    assert by["s4"]["status"] == "not measured" and "has no measure 'tnone'" in by["s4"]["why"]
    assert by["s5"]["status"] == "met" and out["met"] == "1/7"
    assert by["s6"]["status"] == "not measured" and "no such vector" in by["s6"]["why"]      # a wrong spec
    assert "no_value_at" not in by["s6"]
    assert by["s7"]["status"] == "not met" and by["s7"]["no_value_why"].startswith("no value")   # a param= of tnever
    res, err = T.call(ctx, "sweep", {"lib": "l", "cell": "rcm", "vary": {"C1.value": ["1n", "2n"]}, "ids": ["s1", "s5"]})
    assert not err, res
    for p in json.loads(res)["points"]:
        assert "s1" in p["not_met"] and not p["all_pass"] and "not_measured" not in p
        assert p["no_value"]["s1"]["why"].startswith("no value") and p["no_value"]["s1"]["at"] == [{}]
    model = {"items": [{"id": "s1", "kind": "spec", "text": "x", "spec": {"measure": "t", "max": 1, "missing": "maybe"}}]}
    assert "missing is fail" in intent.validate(model, None)[0]["message"]
    with pytest.raises(specs.SpecError, match="missing is fail or unknown"):
        specs.probes(model, "l")
    shutil.rmtree(root)


def test_no_value_counted_as_a_failure_is_the_worst_case_over_corners_and_runs():
    nv = {"value": None, "pass": False, "no_value": True, "error": "no value, which this spec counts as not met "
          "(missing: fail): its .meas tclr gave no value", "job": "j-ss"}
    rows = [_row({"corner": "tt"}, 1e-9, hi=2e-9), {"point": {"corner": "ss"}, "results": {"s1": nv}},
            _row({"corner": "ff"}, None, error="the simulation failed (job j-ff)")]
    probe = {"id": "s1", "text": "clears", "measure": "tclr", "min": None, "max": 2e-9, "lib": "l", "testbench": "tb"}
    s = specs.summarize([probe], {"rows": rows})[0]
    assert (s["status"], s["worst"], s["points_met"], s["no_value_at"]) == ("not met", 1e-9, "1/3", [{"corner": "ss"}])
    # each set of points with its own reason: ss gave no value, ff's run failed
    assert s["no_value_why"].startswith("no value") and s["not_measured_at"] == [{"corner": "ff"}]
    assert s["why"] == "the simulation failed (job j-ff)"
    r = specs.fold(rows, [])[0]["results"]["s1"]
    assert r["pass"] is False and r["no_value_at"] == [{"corner": "ss"}] and r["worst_at"] == {"corner": "ss"}
    assert r["not_measured_at"] == [{"corner": "ff"}] and r["worst_measured"]["value"] == 1e-9
    assert T._unmeasured({"s1": r}) == {          # a sweep row: each location shown with its own reason
        "not_measured": {"s1": {"why": "the simulation failed (job j-ff)", "at": [{"corner": "ff"}],
                                "worst_measured": 1e-9}},
        "no_value": {"s1": {"why": nv["error"], "at": [{"corner": "ss"}]}}}
    t = specs.table(rows, [probe])
    assert t[1]["not_met"] == ["s1"] and t[1]["no_value"] == ["s1"] and "not_measured" not in t[1]
    mc = [{"point": {"seed": i}, "results": {"s1": r}} for i, r in
          enumerate([_row({}, 1e-9, hi=2e-9)["results"]["s1"], nv, {"error": "the simulation failed"}])]
    st = specs.mismatch_stats([probe], mc)[0]["specs"]["s1"]
    assert (st["met"], st["yield"], st["no_value"], st["not_measured"]) == (1, pytest.approx(1 / 3, abs=1e-3), 1, 1)
    assert st["no_value_runs"] == [{"seed": 1, "job": "j-ss"}] and st["worst_seed"] == 0     # the run to look at


# ------------------------------------------------------------------ the worst case, per-corner limits, a spec's own points


def _row(point, value, lo=None, hi=None, **extra):
    r = {"value": value, **specs.verdict(value, lo, hi), "job": f"j-{point.get('corner')}", **extra} if value is not None \
        else extra
    return {"point": point, "results": {"s1": r}}


def test_a_failed_corner_is_never_replaced_by_another_corners_value():
    """bootsw: ss failed (Pclm not positive), tt measured 95.9 ohm, ff 82.8 ohm. The folded point used to show the ff
    value as its worst, and as all met."""
    rows = [_row({"c/M1.W": "10", "corner": "tt"}, 95.9, hi=100.0),
            _row({"c/M1.W": "10", "corner": "ss"}, None, error="the simulation failed (job j-ss)"),
            _row({"c/M1.W": "10", "corner": "ff"}, 82.8, hi=100.0)]
    f = specs.fold(rows, ["c/M1.W"])[0]
    r = f["results"]["s1"]
    assert r["value"] is None and r["pass"] is None and r["not_measured_at"] == [{"corner": "ss"}]
    assert r["worst_measured"] == {"value": 95.9, "at": {"corner": "tt"}} and "failed" in r["error"]
    assert f["all_pass"] is False if "all_pass" in f else "score" not in f
    # the order does not matter, and a measured failure is still a failure
    f = specs.fold([rows[1], rows[2], _row({"c/M1.W": "10", "corner": "tt"}, 105.0, hi=100.0)], ["c/M1.W"])[0]
    assert f["results"]["s1"]["pass"] is False and f["results"]["s1"]["worst_at"] == {"corner": "tt"} and not f["all_pass"]
    probe = {"id": "s1", "text": "ron", "measure": "ron", "min": None, "max": 100.0, "lib": "l", "testbench": "tb"}
    s = specs.summarize([probe], {"rows": rows})[0]
    assert (s["status"], s["worst"], s["worst_at"]["corner"], s["points_met"]) == ("not fully measured", 95.9, "tt", "2/3")
    assert s["not_measured_at"] == [{"c/M1.W": "10", "corner": "ss"}] and "failed" in s["why"]
    s = specs.summarize([probe], {"rows": [*rows, _row({"c/M1.W": "10", "corner": "sf"}, 101.0, hi=100.0)]})[0]
    assert s["status"] == "not met" and s["not_measured_at"][0]["corner"] == "ss"    # not met, and still says where
    t = specs.table(rows, [probe])
    assert [x["values"]["s1"] for x in t] == [95.9, None, 82.8] and "failed" in t[1]["not_measured"]["s1"]


def test_the_worst_case_is_compared_unrounded():
    """verify:bootsw: limit 0.44 mV, values far below it: every margin rounds to 0.44 and the first corner won."""
    rows = [_row({"corner": c}, v, hi=0.44) for c, v in (("tt", 5.26e-7), ("ss", 1e-6), ("ff", 5.86e-6))]
    probe = {"id": "s1", "text": "", "measure": "err", "min": None, "max": 0.44, "lib": "l", "testbench": "tb"}
    s = specs.summarize([probe], {"rows": rows})[0]
    assert s["worst"] == 5.86e-6 and s["worst_at"] == {"corner": "ff"}
    assert specs.fold(rows, [])[0]["results"]["s1"]["worst_at"] == {"corner": "ff"}


def test_limits_per_corner_and_a_specs_own_corners_temperatures_and_conditions():
    model = {"items": [
        {"id": "s1", "kind": "spec", "text": "conversion time", "spec": {
            "measure": "tconv", "max": "90n", "testbench": "tb", "corners": {"tt": {}, "ss": {"max": "97n"}, "ff": None},
            "temps": {27: {}, 125: {"max": "99n"}}, "conditions": {"tb/Vin.value": ["0.3", "1.5"]}}},
        {"id": "s2", "kind": "spec", "text": "power", "spec": {"measure": "-iavg*1.8", "max": "1m", "testbench": "tb"}}]}
    p1, p2 = specs.probes(model, "l")
    assert p1["corners"] == ["tt", "ss", "ff"] and p1["temps"] == [27.0, 125.0]
    assert p1["at"] == {("corner", "ss"): {"max": 97e-9}, ("temp", 125.0): {"max": 99e-9}}
    assert specs.limits(p1, {"corner": "ss"}) == (None, 97e-9) and specs.limits(p1, {"corner": "ff"}) == (None, 90e-9)
    assert specs.limits(p1, {"corner": "ss", "temp": 125.0}) == (None, 99e-9)       # the temperature's over the corner's
    plan = specs.plan([p1, p2], None, None, None, "c")
    assert len(plan["points"]) == 3 * 2 * 2 + 1 and len(plan["at"]["s1"]) == 12 and len(plan["at"]["s2"]) == 1
    assert plan["points"][plan["at"]["s2"][0]] == {"overrides": {}}                 # s2 has no points of its own
    assert len(specs._tasks([p1, p2], plan["points"], plan["at"])) == 13            # s2 as the testbench is: a run of its own
    plan = specs.plan([p1, p2], None, ["tt"], None, "c")                            # the call's corners win
    assert {pt["corner"] for pt in plan["points"]} == {"tt"} and len(plan["at"]["s2"]) == 1
    s = specs.summarize([p1], {"rows": [_row({"corner": "ss"}, 95e-9, hi=97e-9, limits={"max": 97e-9})]})[0]
    assert s["status"] == "met" and s["limits_at"] == {"corner": {"ss": {"max": 97e-9}}, "temp": {"125": {"max": 99e-9}}}
    assert s["limits_at_worst"] == {"max": 97e-9}
    for bad, msg in (({"corners": {"ss": {"typ": 1}}}, "limits there are"), ({"corners": 3.5j}, "a list, or a mapping"),
                     ({"temps": ["hot"]}, "not a number"), ({"conditions": ["x"]}, "conditions is a mapping")):
        m = {"items": [{"id": "s1", "kind": "spec", "text": "x", "spec": {"measure": "a", "max": 1, "testbench": "tb", **bad}}]}
        with pytest.raises(specs.SpecError, match=msg):
            specs.probes(m, "l")
        assert any(msg in x["message"] for x in intent.validate(m, None)), bad


def test_settling_time_and_derived_measures():
    import numpy as np
    x = np.linspace(0, 10e-9, 2001)
    y = 1 - np.exp(-x / 1e-9)
    assert derived.settling_time(x, y, 0.01, target=1.0) == pytest.approx(1e-9 * math.log(100), rel=1e-3)
    assert derived.settling_time(x, y, 0.01, start=2e-9, target=1.0) == pytest.approx(1e-9 * math.log(100) - 2e-9, rel=1e-3)
    assert derived.settling_time(x, y, 0.5, start=8e-9) == 0.0                       # inside from the start
    with pytest.raises(ValueError, match="has not settled"):
        derived.settling_time(x, y, 0.001, target=2.0)
    m = {"t1": 1e-9, "t2": 3e-9, "iavg": -2e-3, "v(in).max": 1.8, "x1.xm1.gm": 1e-4}.get
    assert derived.evaluate("max(t1, t2) + mean(t1, t2)", m, None) == pytest.approx(5e-9)
    assert derived.evaluate("-iavg*1.8", m, None) == pytest.approx(3.6e-3)
    assert derived.evaluate("v(in).max / 2 + x1.xm1.gm * 1k", m, None) == pytest.approx(1.0)
    assert derived.evaluate("settle(v(o), tol=10m, from=0, target=1)", m, lambda s: (x, y)) == pytest.approx(4.6052e-9, rel=1e-3)
    assert [derived.is_derived(t) for t in ("v(out)", "x1.xm1.gm", "v(in).max", "in", "max(a, b)", "-iavg*1.8")] == \
        [False, False, False, False, True, True]
    assert derived.references("max(t1, settle(v(in), tol=1m, from=2n), v(x).final)") == (["t1", "v(x).final"], ["v(in)"])
    for bad, msg in (("foo(t1)", "no function foo"), ("settle(v(o))", "needs tol"), ("max(", "cannot read"),
                     ("settle(v(o), tol=1m, by=1)", "unknown by")):
        with pytest.raises(ValueError, match=msg):
            derived.check(bad)
    with pytest.raises(ValueError, match="no measure t9"):
        derived.evaluate("max(t1, t9)", m, None)
    summ = {"measurements": {"t1": 1e-9}, "signals": {"v(out)": {"max": 1.2}}}
    assert specs.value(summ, "max(t1, v(out).max)") == 1.2
    with pytest.raises(ValueError, match="no measure t9 in this run; it has: t1"):
        specs.value(summ, "t1 + t9")
    # a .meas that ngspice could not evaluate says why, also inside a derived measure (measurement_failures' reason)
    summ["measurement_errors"] = {"tnever": "trig(TARG) : out of interval (.meas tran tnever TRIG v(in) VAL=0.5 RISE=1 "
                                            "TARG v(out) VAL=2 RISE=1)", "tw": "Undefined parameter [tnever]"}
    with pytest.raises(ValueError, match=r"^max\(t1, tNever\): its \.meas tNever gave no value: ngspice says 'trig\(TARG\) "
                                         r": out of interval .*\(a TRIG / TARG level never crossed"):
        specs.value(summ, "max(t1, tNever)")
    assert specs.no_value(summ, "tw").startswith("its .meas tw gave no value: ngspice says 'Undefined parameter [tnever]' "
                                                 "(a param= that uses a measure that failed")
    assert specs.no_value(summ, "t9") == "no measure t9 in this run; it has: t1, v(out).max"
    # a .meas that takes a measurement as its level would stop the whole ngspice run: refused, saying what to use
    lines = [".meas tran vf FIND v(top) AT=40n", ".meas tran ts WHEN v(top)='vf-0.44m' CROSS=LAST",
             ".meas tran tmax param='max(vf, 1)'"]
    why = intent.measured_levels(lines)
    assert len(why) == 1 and why[0].startswith(".meas ts uses the measurement vf") and "settle(" in why[0]
    bad = {"items": [{"id": "s1", "kind": "spec", "text": "x", "spec": {"measure": "ts", "max": 1, "testbench": "tb", "meas": lines}},
                     {"id": "s2", "kind": "spec", "text": "y", "spec": {"measure": "max(t1, ", "max": 1, "testbench": "tb"}},
                     {"id": "s3", "kind": "spec", "text": "z", "spec": {"measure": "max(t1, t2)", "max": 1, "testbench": "tb",
                                                                         "meas": ".meas tran t1 AVG v(a)"}}]}
    msgs = [(x["entry"], x["message"]) for x in intent.validate(bad, None)]
    assert any(e == "items.s1" and "uses the measurement vf" in m for e, m in msgs)
    assert any(e == "items.s2" and "cannot read the measure" in m for e, m in msgs)
    assert not any(e == "items.s3" for e, _ in msgs)                                # a derived measure over its meas


def test_a_bus_bit_is_a_name_not_an_expression():
    """dout[3].final and x1.xsw[2].gm parsed as Python subscripts, so every spec of the cell was refused."""
    for name in ("d[0].final", "dout[3].final", "x1.xm1[0].gm", "x1.xsw[2].gm", "v(dout[3]).final"):
        assert not derived.is_derived(name), name
    assert derived.references("max(d[0].final, x1.xsw[2].gm) * 2") == (["d[0].final", "x1.xsw[2].gm"], [])
    summ = {"signals": {"dout[3]": {"final": 1.79}}, "transistors": [{"device": "x1.xsw[2]", "gm": 2e-4}]}
    assert specs.value(summ, "dout[3].final") == 1.79 and specs.value(summ, "x1.xsw[2].gm") == 2e-4
    assert specs.value(summ, "max(dout[3].final, x1.xsw[2].gm * 1k)") == 1.79
    model = {"items": [{"id": f"s{i}", "kind": "spec", "text": "x", "spec": {"measure": m, "max": 2, "testbench": "tb"}}
                       for i, m in enumerate(("dout[3].final", "x1.xsw[2].gm"))]}
    assert [p["measure"] for p in specs.probes(model, "l")] == ["dout[3].final", "x1.xsw[2].gm"]
    assert not [x for x in intent.validate(model, None) if x["severity"] == "error"]
    assert specs.signals(specs.probes(model, "l")) == ["dout[3]"]


def test_simulator_options():
    assert sim.sim_options({"RELTOL": 1e-6, "method": "Gear", "maxstep": "0.2n"}) == \
        {"reltol": "1e-06", "method": "gear", "maxstep": "0.2n"}
    for bad, msg in (({"step": "1n"}, "no simulator option step"), ({"method": "euler"}, "method is gear or trap"),
                     ({"reltol": "tight"}, "not a number")):
        with pytest.raises(ValueError, match=msg):
            sim.sim_options(bad)
    assert sim.analysis_card({"type": "tran", "step": "1n", "stop": "1u", "maxstep": "0.1n"}) == ".tran 1n 1u 0 0.1n"
    assert sim.analysis_card({"type": "tran"}) == ".tran 1n 100n"
    text = "* t\n.tran 1n 1u uic\n.control\ntran 2n 2u 10n\n.endc\n.end\n"
    out = sim.with_options(text, {"reltol": "1e-6", "maxstep": "0.1n"})
    assert ".tran 1n 1u 0 0.1n uic\n" in out and "\ntran 2n 2u 10n 0.1n\n" in out and out.endswith(".options reltol=1e-6\n.end\n")
    with pytest.raises(ValueError, match="maxstep applies to a transient"):
        sim.with_options("* t\n.op\n.end\n", {"maxstep": "1n"})
    pts, names = specs.grid({"options.reltol": ["1e-3", "1e-6"]}, None, None, "c", {"options.maxstep": ["1n"]})
    assert names == ["options.reltol"] and pts[1]["options"] == {"reltol": "1e-6", "maxstep": "1n"}
    assert specs.label(pts[1]) == {"options.reltol": "1e-6", "options.maxstep": "1n"}
    with pytest.raises(specs.SpecError, match="no simulator option"):
        specs.grid(None, None, None, "c", {"options.stepsize": ["1n"]})


def test_mismatch_runs_are_planned_within_the_job_limit_and_summarised():
    p = {"id": "s1", "text": "", "measure": "v(g)", "min": None, "max": 0.75, "lib": "l", "testbench": "tb", "analysis": None,
         "output": None, "input": None, "corners": None, "temps": None, "conditions": None, "at": {}}
    plan = specs.plan([p], None, ["tt", "ss"], None, "c", mismatch={"runs": 10, "seed": 5})
    assert len(plan["points"]) == 20 and plan["note"] is None and plan["points"][1] == {"overrides": {}, "corner": "tt", "seed": 6}
    plan = specs.plan([p], None, ["tt", "ss", "ff"], None, "c", mismatch={"runs": 100})
    assert plan["mismatch"] == {"runs": 40, "seed": 1, "asked": 100} and len(plan["points"]) == 120
    assert "40 of the 100 runs" in plan["note"] and "seed 41" in plan["note"]
    for bad in ({"runs": 0}, {"runs": "many"}, {"n": 3}, [3], {"runs": specs.MAX_MISMATCH_RUNS + 1}):
        with pytest.raises(specs.SpecError):
            specs.plan([p], None, None, None, "c", mismatch=bad)
    # ngspice skips seed 0, a negative one or one past 2^31 - 1 (a warning) and seeds from the time: not repeatable
    for bad in ({"runs": 5, "seed": 0}, {"runs": 5, "seed": -2}, {"runs": 5, "seed": 2 ** 31 - 4}):
        with pytest.raises(specs.SpecError, match="mismatch seed: 1 .. 2147483643"):
            specs.plan([p], None, None, None, "c", mismatch=bad)
    assert specs.plan([p], None, None, None, "c", mismatch={"runs": 5, "seed": 2 ** 31 - 5})["mismatch"]["seed"] == 2 ** 31 - 5
    # the points are built for the runs that fit in a call, not for all those asked
    built = []
    label = specs.label
    specs.label = lambda pt: built.append(1) or label(pt)
    try:
        plan = specs.plan([p], None, ["tt", "ss", "ff"], [-40, 27, 125], "c", mismatch={"runs": specs.MAX_MISMATCH_RUNS})
    finally:
        specs.label = label
    assert plan["mismatch"]["runs"] == 13 and len(plan["points"]) == 117 and len(built) < 200
    rows = [_row({"corner": "tt", "seed": s}, v, hi=0.75) for s, v in zip(range(1, 6), (0.70, 0.72, 0.74, 0.76, 0.73))]
    rows.append(_row({"corner": "tt", "seed": 6}, None, error="the simulation failed (job j)"))
    st = specs.mismatch_stats([p], rows)
    assert len(st) == 1 and st[0]["at"] == {"corner": "tt"}
    s = st[0]["specs"]["s1"]
    assert (s["runs"], s["met"], s["yield"], s["not_measured"], s["worst_seed"]) == (6, 4, 0.6667, 1, 4)
    assert s["mean"] == pytest.approx(0.73) and s["sigma"] == pytest.approx(0.02236, rel=1e-3)
    assert s["sigmas_to_limit"] == pytest.approx(0.02 / 0.02236, rel=1e-3) and (s["min"], s["max"]) == (0.70, 0.76)
    # runs still running are not failures: the yield is over the finished ones until they are in
    rows = [_row({"corner": "tt", "seed": i}, 0.70, hi=0.75) for i in range(1, 6)]
    rows += [_row({"corner": "tt", "seed": i}, None, pending=True) for i in range(6, 21)]
    s = specs.mismatch_stats([p], rows)[0]["specs"]["s1"]
    assert (s["runs"], s["met"], s["yield"], s["pending"], "not_measured" in s) == (20, 5, 1.0, 15, False)
    rows.append(_row({"corner": "tt", "seed": 21}, None, error="the simulation failed (job j)"))
    s = specs.mismatch_stats([p], rows)[0]["specs"]["s1"]
    assert (s["yield"], s["pending"], s["not_measured"]) == (0.8333, 15, 1)
    s = specs.mismatch_stats([p], rows[5:20])[0]["specs"]["s1"]
    assert (s["runs"], s["yield"], s["pending"]) == (15, None, 15) and "mean" not in s


def test_a_reused_run_is_found_among_all_runs_not_only_the_newest_hundred(tmp_path):
    jobs = sim.Jobs()
    runs = jobs.runs_dir(tmp_path)
    for i in range(105):
        d = runs / f"20261030-0000{i:03d}-abcd"
        d.mkdir()
        (d / "status.json").write_text(json.dumps({"id": d.name, "state": "done", "key": f"k{i}"}))
    assert jobs.find(tmp_path, "k0").id == "20261030-0000000-abcd" and jobs.find(tmp_path, "k104") is not None
    assert jobs.find(tmp_path, "k999") is None and len(jobs.list(tmp_path)) == 100
    # a job left running by an earlier server process never finishes: not taken (it would be waited on for ever)
    for name, state in (("20261030-085900-done", "done"), ("20261030-090000-dead", "running")):
        (runs / name).mkdir()
        (runs / name / "status.json").write_text(json.dumps({"id": name, "state": state, "key": "K"}))
    assert jobs.find(tmp_path, "K").id == "20261030-085900-done"
    jobs.live["20261030-090000-dead"] = sim.Job(runs / "20261030-090000-dead")      # running in this process: taken
    assert jobs.find(tmp_path, "K").id == "20261030-090000-dead"


def test_a_long_check_says_first_what_is_missing_and_fits_its_answer(monkeypatch):
    """10 specs at 3 corners with 40 Monte Carlo runs, 15 still running, cut from more: the statistics came before the
    runs, the pending count and the note on how to go on, and the answer's cut at RESULT_CHARS took exactly those."""
    probes = [{"id": f"s{i}", "text": f"spec number {i} of the cell", "measure": f"m{i}", "min": None, "max": 1.0, "lib": "l",
               "testbench": "tb", "at": {}} for i in range(10)]
    rows, pending = [], []
    for corner in ("tt", "ss", "ff"):
        for seed in range(1, 41):
            pt = {"corner": corner, "seed": seed}
            if seed > 25:
                pending.append({"point": pt, "testbench": "tb", "job": "j"})
                rows.append({"point": pt, "results": {p["id"]: {"pending": True, "job": "j"} for p in probes}, "runs": []})
            else:
                rows.append({"point": pt, "results": {p["id"]: {"value": 0.5 + seed / 1000, **specs.verdict(0.5 + seed / 1000, None, 1.0),
                                                                "job": f"20261030-0900{seed:02d}-{corner}"} for p in probes},
                             "runs": [{"job": f"20261030-0900{seed:02d}-{corner}", "testbench": "tb"}]})
    note = "mismatch: 40 of the 100 runs asked (seeds 1 .. 40) ... For more runs, ask again with seed 41"
    res = {"rows": rows, "failures": [], "pending": pending, "mismatch": {"runs": 40, "seed": 1, "asked": 100}, "cut": note}
    monkeypatch.setattr(T, "_measure", lambda c, a, vary: (probes, [r["point"] for r in rows], res, []))
    text = T._txt(T.check_specs(None, {"lib": "l", "cell": "c"}))
    assert len(text) <= T.RESULT_CHARS and "truncated" not in text
    out = json.loads(text)
    assert out["pending"] == 45 and "call the same tool again" in out["note"] and "seed 41" in out["mismatch_note"]
    assert out["runs"]["new"] == 75 and len(out["specs"]) == 10
    shown = out["mismatch"]["stats"]
    assert shown and shown[0]["at"] == {"corner": "tt"} and shown[0]["specs"]["s0"]["pending"] == 15
    assert len(shown) == 3 or out["mismatch"]["stats_not_shown"].startswith(f"the last {3 - len(shown)} of 3")
    # a check without mismatch: the per-point table is cut the same way, and says how many points it leaves out
    res = {"rows": [{**r, "point": {**r["point"], "temp": float(r["point"].pop("seed"))}} for r in rows[:75]],
           "failures": [], "pending": [], "mismatch": None, "cut": None}
    text = T._txt(T.check_specs(None, {"lib": "l", "cell": "c"}))
    out = json.loads(text)
    assert len(text) <= T.RESULT_CHARS and 0 < len(out["points"]) < 75 and "runs" in out
    assert out["points_not_shown"].startswith(f"the last {75 - len(out['points'])} of 75")


def test_yes_no_and_number_arguments_from_the_shell_client():
    """undine call key=value sends text: fresh=false read as bool('false'), which is True."""
    assert [T._flag({"fresh": v}, "fresh") for v in (None, False, True, "true", "False", " TRUE ")] == \
        [False, False, True, True, False, True]
    with pytest.raises(T.ToolError, match="fresh is true or false"):
        T._flag({"fresh": "yes"}, "fresh")


# ------------------------------------------------------------------ with ngspice: values per point, reuse, fresh, limits


def test_check_specs_gives_every_points_value_says_what_it_reused_and_runs_fresh_on_request():
    _need()
    root, srv = _project()
    ctx = _rc(srv)
    cond = {"rc/R1.value": ["0.8k", "1k", "1.2k"]}
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "rc", "ids": ["s1", "s3"], "conditions": cond})
    assert not err, res
    r = json.loads(res)
    f3 = 1 / (2 * math.pi * 1e3 * 1e-9)
    pts = {p["point"]["rc/R1.value"]: p for p in r["points"]}
    assert set(pts) == {"0.8k", "1k", "1.2k"} and set(pts["1k"]["values"]) == {"s1", "s3"}
    assert pts["1.2k"]["values"]["s1"] == pytest.approx(f3 / 1.2, rel=0.02) and pts["0.8k"]["values"]["s1"] > f3
    assert r["runs"] == {"new": 3, "reused": 0} and all("reused" not in p and p["job"] for p in r["points"])
    by = {s["id"]: s for s in r["specs"]}
    assert by["s1"]["worst_at"] == {"rc/R1.value": "1.2k"} and by["s1"]["points_met"] == "3/3"
    # asked again: the same runs, reused, and when they ran
    n = len(srv.jobs.list(srv.project(ctx.work).path))
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "rc", "ids": ["s1", "s3"], "conditions": cond,
                                           "fresh": "false"})                            # as undine call fresh=false sends it
    again = json.loads(res)
    assert not err and again["runs"]["reused"] == 3 and again["runs"]["new"] == 0 and "fresh" in again["runs"]["note"]
    assert all(p["reused"].startswith("20") for p in again["points"])                # when they ran
    assert len(srv.jobs.list(srv.project(ctx.work).path)) == n
    # fresh: new runs, the same numbers
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "rc", "ids": ["s1", "s3"], "conditions": cond, "fresh": True})
    fresh = json.loads(res)
    assert not err and fresh["runs"] == {"new": 3, "reused": 0}
    assert len(srv.jobs.list(srv.project(ctx.work).path)) == n + 3
    assert {p["point"]["rc/R1.value"]: p["values"] for p in fresh["points"]} == {k: p["values"] for k, p in pts.items()}
    assert {p["job"] for p in fresh["points"]}.isdisjoint({p["job"] for p in r["points"]})
    shutil.rmtree(root)


def test_a_specs_own_temperatures_and_conditions_with_their_own_limits():
    """A spec carries where it is checked and the limits that differ there: used when the call does not say."""
    _need()
    root, srv = _project()
    ctx = _rc(srv)
    res, err = T.call(ctx, "add_intent", {"lib": "l", "cell": "rc", "kind": "spec", "text": "bandwidth, more when hot", "reason": "r",
                                          "spec": {"measure": "f_3db_hz", "min": "100k", "testbench": "rc", "output": "out",
                                                   "input": "in", "analysis": {"type": "ac", "points": "50", "fstart": "1k", "fstop": "100Meg"},
                                                   "temps": {27: {}, 125: {"min": "170k"}},
                                                   "conditions": {"rc/C1.value": ["1n", "0.9n"]}}})
    assert not err, res
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "rc", "ids": ["s4"]})
    assert not err, res
    r = json.loads(res)
    s = r["specs"][0]
    assert s["status"] == "not met" and s["points_met"] == "3/4" and s["worst_at"] == {"rc/C1.value": "1n", "temp": 125.0}
    assert s["limits_at"] == {"temp": {"125": {"min": 170000.0}}} and s["limits_at_worst"] == {"min": 170000.0}
    assert {(p["point"]["rc/C1.value"], p["point"]["temp"]): p["not_met"] for p in r["points"]} == \
        {("1n", 27.0): [], ("0.9n", 27.0): [], ("1n", 125.0): ["s4"], ("0.9n", 125.0): []}     # 177 kHz with 0.9 nF
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "rc", "ids": ["s4"], "temps": [27]})   # the call's own
    assert not err and json.loads(res)["specs"][0]["status"] == "met" and len(json.loads(res)["points"]) == 2
    shutil.rmtree(root)


def test_settling_time_and_derived_measures_on_an_rc_step():
    _need()
    root, srv = _project()
    ctx = _tb(srv, "rcs", [("devices/vsource.sym", 0, {"name": "V1", "value": "pulse(0 1 1u 1n 1n 10u 20u)"}),
                           ("devices/res.sym", 100, {"name": "R1", "value": "1k"}),
                           ("devices/capa.sym", 200, {"name": "C1", "value": "1n"})],
               {"in": ["V1:p", "R1:P"], "out": ["R1:M", "C1:p"], "0": ["V1:m", "C1:m"]})
    tran = {"type": "tran", "step": "10n", "stop": "8u"}
    meas = [".meas tran tdelay TRIG v(in) VAL=0.5 RISE=1 TARG v(out) VAL=0.5 RISE=1",
            ".meas tran trise TRIG v(out) VAL=0.1 RISE=1 TARG v(out) VAL=0.9 RISE=1"]
    never = ".meas tran tnever TRIG v(in) VAL=0.5 RISE=1 TARG v(out) VAL=2 RISE=1"       # out stays below 2
    for text, spec in (("settles to 1 % in 5 us", {"measure": "settle(v(out), tol=10m, from=1u, target=1)", "max": "5u"}),
                       ("the slower edge", {"measure": "max(tdelay, trise)", "max": "3u", "meas": meas}),
                       ("rise over delay", {"measure": "trise / tdelay", "max": 4, "meas": meas}),
                       ("not there", {"measure": "max(tdelay, tfall)", "max": 1, "meas": meas}),
                       ("never settles", {"measure": "settle(v(out), tol=1m, from=1u, target=2)", "max": 1}),
                       ("never crosses", {"measure": "max(tdelay, tnever)", "max": 1, "meas": [meas[0], never]})):
        res, err = T.call(ctx, "add_intent", {"lib": "l", "cell": "rcs", "kind": "spec", "text": text, "reason": "r",
                                              "spec": {**spec, "testbench": "rcs", "analysis": tran}})
        assert not err, res
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "rcs"})
    assert not err, res
    by = {s["id"]: s for s in json.loads(res)["specs"]}
    tau = 1e-6
    assert by["s1"]["status"] == "met" and by["s1"]["worst"] == pytest.approx(tau * math.log(100), rel=0.02)
    assert by["s2"]["worst"] == pytest.approx(tau * math.log(9), rel=0.03)
    assert by["s3"]["worst"] == pytest.approx(math.log(9) / math.log(2), rel=0.03)
    assert by["s4"]["status"] == "not measured" and "no measure tfall" in by["s4"]["why"] and "tdelay" in by["s4"]["why"]
    assert by["s5"]["status"] == "not measured" and "has not settled" in by["s5"]["why"]
    assert by["s6"]["status"] == "not measured" and "its .meas tnever gave no value: ngspice says" in by["s6"]["why"]
    assert by["s1"]["job"] == by["s2"]["job"]                                       # one run, every measure read off it
    # the same measures in one run_simulation, next to a .meas
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "rcs", "analysis": tran, "signals": ["in"],
                                             "measure": [meas[0], never, "settle(v(out), tol=10m, from=1u, target=1)",
                                                         "v(out).max", "2 * tdelay", "tnone", "tnever", "tnever + 1"]})
    assert not err, res
    out = json.loads(res)
    assert out["measures"]["settle(v(out), tol=10m, from=1u, target=1)"] == pytest.approx(tau * math.log(100), rel=0.02)
    assert out["measures"]["v(out).max"] == pytest.approx(0.999, abs=2e-3)       # read although signals names only in
    assert out["measures"]["2 * tdelay"] == pytest.approx(2 * tau * math.log(2), rel=0.03)
    assert out["measures"]["tnone"] is None and "no measure tnone" in out["measure_errors"]["tnone"]
    assert "tnever" in out["measurement_errors"]                                 # what ngspice said, for each read of it
    assert out["measure_errors"]["tnever"].startswith("its .meas tnever gave no value: ngspice says")
    assert out["measure_errors"]["tnever + 1"].startswith("tnever + 1: its .meas tnever gave no value: ngspice says")
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "rcs", "analysis": tran, "measure": ["settle(v(out))"]})
    assert err and "needs tol" in res
    # a .meas whose level is another measurement would stop the whole run in ngspice: refused before, saying what to use
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "rcs", "analysis": tran, "measure": [
        ".meas tran vf FIND v(out) AT=7u", ".meas tran ts WHEN v(out)='vf-0.01' CROSS=LAST"]})
    assert err and "uses the measurement vf" in res and "settle(" in res
    shutil.rmtree(root)


def test_simulator_options_in_a_run_and_as_a_condition():
    _need()
    root, srv = _project()
    ctx = _tb(srv, "rco", [("devices/vsource.sym", 0, {"name": "V1", "value": "pulse(0 1 1u 1n 1n 10u 20u)"}),
                           ("devices/res.sym", 100, {"name": "R1", "value": "1k"}),
                           ("devices/capa.sym", 200, {"name": "C1", "value": "1n"})],
               {"in": ["V1:p", "R1:P"], "out": ["R1:M", "C1:p"], "0": ["V1:m", "C1:m"]})
    tran = {"type": "tran", "step": "10n", "stop": "8u"}
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "rco", "analysis": tran, "signals": ["out"],
                                             "options": {"reltol": 1e-6, "method": "gear", "maxstep": "20n"}})
    assert not err, res
    text = (srv.jobs.get(srv.project(ctx.work).path, json.loads(res)["job"]).path / "netlist.cir").read_text()
    assert ".options reltol=1e-06 method=gear" in text and ".tran 10n 8u 0 20n" in text
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "rco", "analysis": {"type": "op"}, "options": {"maxstep": "1n"}})
    assert err and "maxstep applies to a transient" in res
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "rco", "analysis": {"type": "op"}, "temp": "125"})   # undine call
    assert not err, res
    assert ".temp 125\n" in (srv.jobs.get(srv.project(ctx.work).path, json.loads(res)["job"]).path / "netlist.cir").read_text()
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "rco", "analysis": {"type": "op"}, "temp": "hot"})
    assert err and "temp is a temperature" in res
    res, err = T.call(ctx, "add_intent", {"lib": "l", "cell": "rco", "kind": "spec", "text": "delay", "reason": "r", "spec": {
        "measure": "tdelay", "max": "1u", "testbench": "rco", "analysis": tran,
        "meas": ".meas tran tdelay TRIG v(in) VAL=0.5 RISE=1 TARG v(out) VAL=0.5 RISE=1"}})
    assert not err, res
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "rco", "conditions": {"options.reltol": ["1e-3", "1e-6"],
                                                                                     "options.maxstep": ["100n", "5n"]}})
    assert not err, res
    pts = json.loads(res)["points"]
    assert len(pts) == 4 and {p["point"]["options.maxstep"] for p in pts} == {"100n", "5n"}
    vals = [p["values"]["s1"] for p in pts]
    assert all(v == pytest.approx(math.log(2) * 1e-6, rel=0.03) for v in vals)
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "rco", "conditions": {"options.stepsize": ["1n"]}})
    assert err and "no simulator option stepsize" in res
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "rco", "mismatch": {"runs": 2}})
    assert not err and "declares no mismatch models" in json.loads(res)["specs"][0]["why"]       # no PDK here
    shutil.rmtree(root)


# ------------------------------------------------------------------ sky130: Monte Carlo mismatch, limits per corner


def _sky130_mc(srv):
    """A diode-connected nfet at 10 uA (its vgs) and a MIM capacitor charged by 1 uA for 20 ns (its voltage)."""
    ctx = _tb(srv, "mc", [("devices/vsource.sym", 0, {"name": "V1", "value": "1.8"}),
                          ("devices/isource.sym", 100, {"name": "I1", "value": "10u"}),
                          ("sky130_fd_pr/nfet_01v8.sym", 200, {"name": "M1", "W": "1", "L": "0.15"}),
                          ("devices/isource.sym", 300, {"name": "I2", "value": "pulse(0 1u 1n 0.1n 0.1n 1 2)"}),
                          ("sky130_fd_pr/cap_mim_m3_1.sym", 400, {"name": "C1", "W": "4", "L": "4", "MF": "1"}),
                          ("devices/res.sym", 500, {"name": "R1", "value": "1G"})],
              {"vdd": ["V1:p", "I1:p", "I2:p"], "g": ["I1:m", "M1:D", "M1:G"], "top": ["I2:m", "C1:c0", "R1:P"],
               "0": ["V1:m", "M1:S", "M1:B", "C1:c1", "R1:M"]})
    tran = {"type": "tran", "step": "0.1n", "stop": "20n"}
    for text, spec in (("vgs under 0.75 V (0.8 V at ss)", {"measure": "v(g).final", "max": 0.75, "corners": {"tt": {}, "ss": {"max": 0.8}}}),
                       ("ramp in range", {"measure": "v(top).final", "min": 0.3, "max": 0.8})):
        res, err = T.call(ctx, "add_intent", {"lib": "l", "cell": "mc", "kind": "spec", "text": text, "reason": "r",
                                              "spec": {**spec, "testbench": "mc", "analysis": tran}})
        assert not err, res
    return ctx


def test_sky130_mismatch_monte_carlo_and_limits_per_corner(monkeypatch):
    _need()
    root, srv = _project("sky130A")
    ctx = _sky130_mc(srv)
    # the spec's own corners, with its limit at ss
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "mc", "ids": ["s1"]})
    assert not err, res
    r = json.loads(res)
    assert {p["point"]["corner"] for p in r["points"]} == {"tt", "ss"} and r["specs"][0]["limits_at"] == {"corner": {"ss": {"max": 0.8}}}
    vgs = {p["point"]["corner"]: p["values"]["s1"] for p in r["points"]}
    assert vgs["ss"] > vgs["tt"]
    # Monte Carlo: the PDK's mismatch models (tt_mm), one seed per run, the same seed the same numbers
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "mc", "corners": ["tt"], "mismatch": {"runs": 4, "seed": 11}})
    assert not err, res
    r = json.loads(res)
    st = r["mismatch"]["stats"]
    assert r["mismatch"]["runs"] == 4 and len(st) == 1 and st[0]["at"] == {"corner": "tt"}
    s1, s2 = st[0]["specs"]["s1"], st[0]["specs"]["s2"]
    assert s1["runs"] == 4 and 1e-3 < s1["sigma"] < 0.05 and s1["min"] < s1["mean"] < s1["max"]
    assert 0.001 < s2["sigma"] / s2["mean"] < 0.03                                 # MIM: 2.8 % / sqrt(16 um^2) = 0.7 %
    assert s1["yield"] == pytest.approx(s1["met"] / 4, abs=1e-3)
    job = srv.jobs.get(srv.project(ctx.work).path, s1["worst_job"])
    text = (job.path / "netlist.cir").read_text()
    assert " tt_mm\n" in text and f".option seed={s1['worst_seed']}" in text and 11 <= s1["worst_seed"] <= 14
    assert job.status()["models"][0]["section"] == "tt_mm" and "reduced" in job.status()["models"][0]
    assert any("device mismatch on" in n for n in job.status()["notes"])
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "mc", "corners": ["tt"], "mismatch": {"runs": 4, "seed": 11}})
    assert not err and json.loads(res)["runs"]["reused"] == 4 and json.loads(res)["mismatch"]["stats"] == st
    # too many runs for one call: cut, and said (a smaller limit than the real one, to keep the test short)
    monkeypatch.setattr(specs, "MAX_JOBS", 3)
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "mc", "corners": ["tt", "ss", "ff"], "ids": ["s2"],
                                           "mismatch": {"runs": 200, "seed": 11}})
    assert not err, res
    r = json.loads(res)
    assert r["mismatch"]["runs"] == 1 and r["mismatch"]["asked"] == 200 and "seed 12" in r["mismatch_note"]
    assert [g["at"]["corner"] for g in r["mismatch"]["stats"]] == ["tt", "ss", "ff"]
    monkeypatch.undo()
    # a sweep with mismatch: each design point by its worst run, and its yield
    res, err = T.call(ctx, "sweep", {"lib": "l", "cell": "mc", "ids": ["s1"], "vary": {"M1.W": ["1", "4"]}, "corners": ["tt"],
                                     "mismatch": {"runs": 2, "seed": 11}})
    assert not err, res
    pts = {p["point"]["mc/M1.W"]: p for p in json.loads(res)["points"]}
    assert set(pts) == {"1", "4"} and pts["4"]["values"]["s1"] < pts["1"]["values"]["s1"] and "s1" in pts["4"]["yield"]
    assert pts["1"]["worst_at"]["s1"]["seed"] in (11, 12) and json.loads(res)["conditions_per_point"] == 2
    # an extra .meas at another corner and temperature, without touching the testbench
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "mc", "analysis": {"type": "tran", "step": "0.1n", "stop": "20n"},
                                             "corner": "ss", "temp": 125, "measure": [".meas tran vg FIND v(g) AT=19n"]})
    assert not err, res
    out = json.loads(res)
    text = (srv.jobs.get(srv.project(ctx.work).path, out["job"]).path / "netlist.cir").read_text()
    assert " ss\n" in text and ".temp 125" in text and out["measurements"]["vg"] < vgs["ss"]     # hot: lower vth
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "mc", "corner": "xx"})
    assert err and "no corner 'xx'" in res
    shutil.rmtree(root)
