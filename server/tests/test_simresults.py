"""Simulation results for agents (simresults.py, run_simulation): analysis arguments, AC figures, transistor bias,
.meas results, failure explanations, PDK models included without a model statement, the PDK corner symbol."""
import json
import math
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np  # noqa: E402
import pytest  # noqa: E402

from undine.agents import agent_tools as T  # noqa: E402
from undine.simulation import sim, simresults  # noqa: E402
from undine import tools
from undine.server.api import Server  # noqa: E402
from undine.pdks.pdk import default_pdk_roots, discover  # noqa: E402


def test_analysis_arguments_are_checked():
    assert sim.analysis_card({"type": "ac", "fstart": "1", "fstop": "1G", "points": "20"}) == ".ac dec 20 1 1G"
    assert sim.analysis_card({"type": "dc", "source": "V2", "start": "0", "stop": "1.8", "step": "0.1"}) == ".dc V2 0 1.8 0.1"
    assert sim.analysis_card({"type": "dc"}) == ".dc V1 0 1.8 0.01"         # no longer "stop 100n" from tran
    with pytest.raises(ValueError, match="unknown start, stop.*fstart"):
        sim.analysis_card({"type": "ac", "start": "1", "stop": "1G"})
    with pytest.raises(ValueError, match="unknown analysis noise"):
        sim.analysis_card({"type": "noise"})


def test_ac_figures_of_a_two_pole_amplifier():
    a0, p1, p2 = 1000.0, 1e4, 3e7                   # 60 dB, poles at 10 kHz and 30 MHz -> GBW about 10 MHz
    f = np.logspace(0, 9, 901)
    s = 2j * np.pi * f
    h = a0 / ((1 + s / (2 * np.pi * p1)) * (1 + s / (2 * np.pi * p2)))
    m = simresults.ac_metrics(f, h)
    assert abs(m["dc_gain_db"] - 60) < 0.01 and abs(m["f_3db_hz"] / p1 - 1) < 0.02
    wu = 9.5e6                                       # |h| = 1 near a0 * p1 with the second pole's correction
    assert abs(m["unity_gain_freq_hz"] / wu - 1) < 0.06
    pm = 180 - math.degrees(math.atan(m["unity_gain_freq_hz"] / p1) + math.atan(m["unity_gain_freq_hz"] / p2))
    assert abs(m["phase_margin_deg"] - pm) < 0.5
    assert "gain_margin_db" not in m                 # two poles never reach -180 degrees
    assert [d["f_hz"] for d in m["per_decade"]][:3] == [1.0, 10.0, 100.0]


LOG = """
     device m.x1.xm7.msky130_fd_p m.x1.xm6.msky130_fd_p
         id           8.26216e-05           8.26216e-05
        vgs              0.782963               1.23057
        vds              0.899305              0.900689
        vth                0.4805                0.9811
      vdsat               0.17646              0.256107
         gm           0.000776334           0.000569215
     device m.x1.xm5.msky130_fd_p
         id           1.70976e-05
        vgs              0.782964
        vds              0.153581
        vth                0.4805
      vdsat              0.176461

  Measurements for AC Analysis

gbw                 =  2.46400e+07
a0db                =  6.89000e+01
"""


def test_bias_regions_and_measurements_from_the_log():
    b = {d["device"]: d for d in simresults.device_bias(LOG)}
    assert set(b) == {"x1.xm5", "x1.xm6", "x1.xm7"}
    assert b["x1.xm7"]["region"] == "saturation" and b["x1.xm5"]["region"] == "linear"
    assert b["x1.xm6"]["region"] == "saturation"                          # pmos: magnitudes compared
    assert b["x1.xm5"]["vds_minus_vdsat"] < 0 and b["x1.xm7"]["gm"] == pytest.approx(7.763e-4, rel=1e-3)
    assert simresults.measurements(LOG) == {"gbw": 2.464e7, "a0db": 68.9}
    failed = ("\n  Measurements for Transient Analysis\n\n\nError: measure  tnever  trig(TARG) : out of interval\n"
              " .meas tran tnever trig v(in) val=0.5 rise=1 targ v(out) val=2 rise=1 failed!\n\n"
              "tdelay              =  6.931367e-07 targ=  1.693637e-06 trig=  1.000500e-06\n\n\nTotal analysis time\n")
    assert simresults.measurements(failed) == {"tdelay": 6.931367e-07}        # a failed .meas does not hide the rest
    assert simresults.measurement_failures(failed) == {
        "tnever": "trig(TARG) : out of interval (.meas tran tnever trig v(in) val=0.5 rise=1 targ v(out) val=2 rise=1)"}


def _project(pdk=None):
    root = Path(tempfile.mkdtemp())
    pdks = discover(default_pdk_roots()) if pdk else {}
    if pdk and not any(p.startswith(pdk) for p in pdks):
        shutil.rmtree(root)
        pytest.skip(f"{pdk} not installed")
    srv = Server(root, pdks, [root])
    prj = srv.store.create("p", next(p for p in pdks if p.startswith(pdk)) if pdk else None)
    prj.create_library("l")
    return root, srv


def _need(*what):
    if not tools.find("ngspice"):
        pytest.skip("ngspice not installed")


def _tb(srv, cell, parts, nets):
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    T.call(ctx, "open_changeset", {"title": "tb", "reason": "r"})
    assert not T.call(ctx, "create_cell", {"lib": "l", "cell": cell, "reason": "tb"})[1]
    ed = srv.editor(ctx.work, "l", cell, "schematic")
    if ed.resolver.load("devices/res.sym", ed.path.parent) is None:
        pytest.skip("Xschem's devices library is not installed")
    for sym, x, params in parts:
        res, err = T.call(ctx, "add_instance", {"lib": "l", "cell": cell, "symbol": sym, "x": x, "y": 0, "params": params, "reason": "p"})
        assert not err, res
    for net, pins in nets.items():
        res, err = T.call(ctx, "connect_pins", {"lib": "l", "cell": cell, "net": net, "pins": pins, "reason": "n"})
        assert not err, res
    return ctx


def test_rc_lowpass_ac_and_tran_and_a_failure():
    _need()
    root, srv = _project()
    ctx = _tb(srv, "rc", [("devices/vsource.sym", 0, {"name": "V1", "value": "dc 0 ac 1 pulse(0 1 0 1n 1n 1 2)"}),
                          ("devices/res.sym", 100, {"name": "R1", "value": "1k"}),
                          ("devices/capa.sym", 200, {"name": "C1", "value": "1n"})],
              {"in": ["V1:p", "R1:P"], "out": ["R1:M", "C1:p"], "0": ["V1:m", "C1:m"]})
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "rc", "output": "out", "input": "in",
                                             "analysis": {"type": "ac", "points": "50", "fstart": "1k", "fstop": "100Meg"}})
    assert not err, res
    m = json.loads(res)["metrics"]
    assert abs(m["dc_gain_db"]) < 0.01 and abs(m["f_3db_hz"] / (1 / (2 * math.pi * 1e3 * 1e-9)) - 1) < 0.02
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "rc", "signals": ["out"],
                                             "analysis": {"type": "tran", "step": "10n", "stop": "10u"}})
    out = json.loads(res)["signals"]["v(out)"]
    assert not err and out["final"] == pytest.approx(1.0, abs=1e-3) and len(out["samples"]) <= simresults.SAMPLES
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "rc", "analysis": {"type": "ac", "stop": "1G"}})
    assert err and "unknown stop" in res and "fstop" in res
    # a broken testbench: the error lines and a hint come back
    T.call(ctx, "add_instance", {"lib": "l", "cell": "rc", "symbol": "devices/res.sym", "x": 300, "y": 0,
                                 "params": {"name": "R9", "value": "1k"}, "reason": "floating"})
    T.call(ctx, "connect_pins", {"lib": "l", "cell": "rc", "net": "float", "pins": ["R9:P"], "reason": "x"})
    T.call(ctx, "connect_pins", {"lib": "l", "cell": "rc", "net": "float2", "pins": ["R9:M"], "reason": "x"})
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "rc", "analysis": {"type": "op"}})
    if err:                                       # ngspice may also solve it with gmin; when it fails it is explained
        assert "errors" in res
    shutil.rmtree(root)


def test_sky130_transistor_bias_with_models_included_automatically_and_the_corner_symbol():
    _need()
    root, srv = _project("sky130A")
    ctx = _tb(srv, "nmos", [("devices/vsource.sym", 0, {"name": "V1", "value": "1.8"}),
                            ("devices/isource.sym", 100, {"name": "I1", "value": "10u"}),
                            ("sky130_fd_pr/nfet_01v8.sym", 200, {"name": "M1", "W": "2", "L": "1"}),
                            ("sky130_fd_pr/nfet_01v8.sym", 300, {"name": "M2", "W": "2", "L": "1"})],
              {"vdd": ["V1:p", "I1:p"], "g": ["I1:m", "M1:D", "M1:G", "M2:G"], "d2": ["M2:D"], "0": ["V1:m", "M1:S", "M1:B", "M2:S", "M2:B"]})
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "nmos", "analysis": {"type": "op"}})
    assert not err, res
    r = json.loads(res)
    assert any("PDK models were included" in n for n in r["notes"])
    b = {t["device"]: t for t in r["transistors"]}
    assert b["xm1"]["region"] == "saturation" and b["xm1"]["id"] == pytest.approx(1e-5, rel=1e-3)
    assert b["xm2"]["region"] == "linear" and r["not_in_saturation"] == ["xm2"]          # its drain is at 0 V
    # the PDK's corner symbol now gives the models (and no automatic include)
    assert not T.call(ctx, "add_instance", {"lib": "l", "cell": "nmos", "symbol": "sky130_fd_pr/corner.sym", "x": -200, "y": 0,
                                            "params": {"corner": "ss"}, "reason": "corner"})[1]
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "nmos", "analysis": {"type": "op"}})
    assert not err, res
    r2 = json.loads(res)
    assert "notes" not in r2
    job = srv.jobs.get(srv.project(ctx.work).path, r2["job"])
    assert ".lib " in (job.path / "netlist.cir").read_text() and " ss\n" in (job.path / "netlist.cir").read_text()
    # the corner symbol's section, read as a copy reduced to the netlist's devices (simulation/modellib.py)
    m = job.status()["models"]
    assert [(Path(x["library"]).name, x["section"], "reduced" in x) for x in m] == [("sky130.lib.spice", "ss", True)]
    assert job.log().startswith("Undine: models: sky130.lib.spice section ss as a copy reduced to the ")
    assert r2["node_voltages"]["g"] > r["node_voltages"]["g"]                             # slow corner: higher vgs
    shutil.rmtree(root)


NETLIST = """* tb
.subckt sw d g s b
XMS d g s b sky130_fd_pr__nfet_01v8 L=0.15 W=10 nf=2 ad=0 as=0 mult=1 m=1
.ends
x4 d g 0 0 sw
.end
"""


def test_failure_names_the_device_outside_its_model():
    """Only a model check that stops the run counts (a Fatal line, as sky130's ss corner gives a 10 um, two-finger
    nfet: its bin is model.8 by W, its fingers are 5 um); a warning of the same check is harmless and is not a cause."""
    d = Path(tempfile.mkdtemp())
    (d / "status.json").write_text(json.dumps({"id": "j", "state": "failed", "error": "run simulation(s) aborted"}))
    (d / "netlist.cir").write_text(NETLIST)
    (d / "log.txt").write_text("Checking parameters for BSIM 4.5 model x4.xms:sky130_fd_pr__nfet_01v8__model.8\n"
                               "Fatal: Pclm = -0.000983115 is not positive.\nWarning: Cdscd = -0.00236246 is negative.\n"
                               "Fatal error: detected during BSIM4v5.5.0 parameter checking for \n"
                               "    model x4.xms:sky130_fd_pr__nfet_01v8__model.8 of device instance m.x4.xms.msky130_fd_pr__nfet_01v8\n"
                               "doAnalyses: no such parameter on this device or parameter is missing\nrun simulation(s) aborted\n")
    f = simresults.failure(sim.Job(d))
    assert f["model_check"] == ["x4.xms (sky130_fd_pr__nfet_01v8__model.8, W=10 L=0.15 nf=2 (5 per finger)): "
                                "Fatal: Pclm = -0.000983115 is not positive."]
    assert f["hints"][0].startswith("a transistor's size is outside") and "nf=1" in f["hints"][0]
    assert not any(h.startswith("a device size is outside") for h in f["hints"])           # said once, precisely
    # the same device at tt: a warning only, and the run failed for another reason
    (d / "log.txt").write_text("Checking parameters for BSIM 4.5 model x4.xms:sky130_fd_pr__nfet_01v8__model.8\n"
                               "Warning: Cdscd = -0.00234204 is negative.\nWarning: source stepping failed\n"
                               "Error: Transient op failed, timestep too small\nError: The operating point could not be simulated successfully.\n")
    f = simresults.failure(sim.Job(d))
    assert "model_check" not in f and not any("size" in h for h in f["hints"])
    assert any("does not converge" in h for h in f["hints"]) and len(f["errors"]) == 2
    shutil.rmtree(d)


def test_measures_for_one_run_are_evaluated_without_touching_the_testbench():
    """.meas used to be ignored with an analysis (ngspice: "No .measure possible in batch mode with -r"); measure adds
    them to one run only, through a .control block that runs the analysis and writes the raw file."""
    _need()
    root, srv = _project()
    ctx = _tb(srv, "rcm", [("devices/vsource.sym", 0, {"name": "V1", "value": "pulse(0 1 1u 1n 1n 10u 20u)"}),
                           ("devices/res.sym", 100, {"name": "R1", "value": "1k"}),
                           ("devices/capa.sym", 200, {"name": "C1", "value": "1n"})],
               {"in": ["V1:p", "R1:P"], "out": ["R1:M", "C1:p"], "0": ["V1:m", "C1:m"]})
    sch = srv.project(ctx.work).library("l").path / "rcm.sch"
    before = sch.read_text()
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "rcm", "signals": ["out"],
                                             "analysis": {"type": "tran", "step": "10n", "stop": "8u"},
                                             "measure": [".meas tran tdelay TRIG v(in) VAL=0.5 RISE=1 TARG v(out) VAL=0.5 RISE=1"]})
    assert not err, res
    out = json.loads(res)
    assert out["measurements"]["tdelay"] == pytest.approx(1e3 * 1e-9 * math.log(2), rel=0.03)   # RC ln 2
    assert out["signals"]["v(out)"]["final"] > 0.99                                            # the raw file is there too
    job = srv.jobs.get(srv.project(ctx.work).path, out["job"])
    assert ".control" in (job.path / "netlist.cir").read_text() and sch.read_text() == before
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "rcm", "measure": ["print hello"]})
    assert err and "one .meas statement" in res
    shutil.rmtree(root)


def test_an_ac_run_with_its_own_meas_runs_once_and_measures():
    """verify:bootsw: an AC run with `.meas ac ... vm(...)` printed the values, then ngspice ran the netlist again in
    batch mode ("can't parse 'vm'", "no data saved for A.C."), and the job failed. Every run with a .meas also
    simulated twice: Undine's .control block now quits after its run."""
    _need()
    root, srv = _project()
    ctx = _tb(srv, "rca", [("devices/vsource.sym", 0, {"name": "V1", "value": "dc 0 ac 1"}),
                           ("devices/res.sym", 100, {"name": "R1", "value": "1k"}),
                           ("devices/capa.sym", 200, {"name": "C1", "value": "1n"})],
              {"in": ["V1:p", "R1:P"], "out": ["R1:M", "C1:p"], "0": ["V1:m", "C1:m"]})
    f3 = 1 / (2 * math.pi * 1e3 * 1e-9)
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "rca", "output": "out",
                                             "analysis": {"type": "ac", "points": "50", "fstart": "1k", "fstop": "100Meg"},
                                             "measure": [f".meas ac g3 FIND vm(out) AT={f3:.6g}",
                                                         f".meas ac p3 FIND vp(out) AT={f3:.6g}"]})
    assert not err, res
    out = json.loads(res)
    assert out["measurements"]["g3"] == pytest.approx(1 / math.sqrt(2), rel=1e-3)
    assert out["measurements"]["p3"] == pytest.approx(-math.pi / 4, rel=1e-3) and "dc_gain_db" in out["metrics"]
    log = (srv.jobs.get(srv.project(ctx.work).path, out["job"]).path / "log.txt").read_text()
    assert log.count("Doing analysis") == 1 and "can't parse" not in log
    shutil.rmtree(root)


def test_a_transient_that_stops_part_way_fails_although_ngspice_exits_0(tmp_path):
    """Undine's .control block ends in quit, so ngspice exits 0 after "Timestep too small ... run simulation(s)
    aborted": the job was done, its raw file cut part way read as a whole run, and a spec measured on it."""
    _need()
    # a current that runs away past 0.3 V: ngspice 42 and 47 alike stop at 2.29 ns (a cubic one, which 42 stopped
    # on, converges in 47)
    net = ("* stops at 2.3 ns\nV1 in 0 PULSE(0 1 2n 1n 1n 5n 10n)\nR1 in out 1k\nC1 out 0 1f\n"
           "B1 out 0 I=-1e-3*exp(1e3*(v(out)-0.3))\n.meas tran vmax MAX v(out)\n.end\n")
    jobs = sim.Jobs()
    job = jobs.submit(tmp_path, "t", net, {"type": "tran", "step": "1p", "stop": "20n"})
    for _ in range(600):
        if job.status()["state"] not in ("queued", "running"):
            break
        time.sleep(0.1)
    st = job.status()
    assert st["state"] == "failed" and "Timestep too small" in st["error"], st
    f = simresults.failure(job)
    assert any(e.startswith("doAnalyses: TRAN:  Timestep too small; time =") for e in f["errors"])
    assert any("stopped part way" in h for h in f["hints"]) and not any("operating point" in h for h in f["hints"])
    # a B-source out of its range part way: failed too, with its own error (it used to be done, with it in warnings)
    net = "* sqrt\nV1 in 0 PULSE(1 -1 2n 1n 1n 5n 10n)\nR1 in out 1k\nC1 out 0 1f\nB1 x 0 V=sqrt(v(out))\nR2 x 0 1k\n" \
          ".meas tran vmax MAX v(out)\n.end\n"
    job = jobs.submit(tmp_path, "t", net, {"type": "tran", "step": "1p", "stop": "20n"})
    for _ in range(600):
        if job.status()["state"] not in ("queued", "running"):
            break
        time.sleep(0.1)
    assert job.status()["state"] == "failed" and "out of range for sqrt" in job.status()["error"]
