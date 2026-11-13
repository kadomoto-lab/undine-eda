"""Simulation under load (sim.Jobs and the tools around it): one ngspice thread per job, a server-wide limit of running
jobs (the others queued in order, with their place), a time limit that ends the process, cancel; the MCP endpoint
answering while a long tool call runs; following and collecting a long run (run_simulation wait_s / job, wait_for
job:<id>, cancel_job; a job an earlier server left is interrupted); a run whose analysis stopped short fails, whatever
ngspice exits with; every failed .meas reported once, with its statement; PDK standard cells in a testbench."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pytest  # noqa: E402

from undine import tools  # noqa: E402
from undine.agents import agent_tools as T, mcp  # noqa: E402
from undine.simulation import sim, simresults, specs  # noqa: E402
import test_changeset as tc  # noqa: E402

REAL_FIND = tools.find
FAKE = """#!/bin/sh
# ngspice for these tests: notes how it was started, then sleeps ("* sleep N" in the netlist, else the file's seconds)
# or waits for a file ("* gate F")
[ -f netlist.cir ] || {{ echo "ngspice-fake"; exit 0; }}
echo "$OMP_NUM_THREADS" > omp.txt
g=$(sed -n 's/^[*] gate \\(.*\\)$/\\1/p' netlist.cir)
if [ -n "$g" ]; then while [ ! -f "$g" ]; do sleep 0.02; done; echo "fake ngspice" > log.txt; exit 0; fi
s=$(sed -n 's/^[*] sleep \\([0-9.]*\\)$/\\1/p' netlist.cir)
[ -n "$s" ] || s=$(cat "{sleep}" 2>/dev/null)
grep -q '^[*] stubborn' netlist.cir && trap '' TERM
echo "fake ngspice" > log.txt
exec sleep "${{s:-0}}"
"""


def until(cond, timeout=10.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def fake(monkeypatch):
    """A stand-in ngspice; returns the file holding how long it sleeps when the netlist does not say."""
    d = Path(tempfile.mkdtemp())
    exe, sleep = d / "ngspice", d / "sleep.txt"
    exe.write_text(FAKE.format(sleep=sleep))
    exe.chmod(0o755)
    sleep.write_text("0")
    monkeypatch.setattr(tools, "find", lambda name: str(exe) if name == "ngspice" else REAL_FIND(name))
    yield sleep
    shutil.rmtree(d)


def state(job):
    return job.status().get("state")


def test_jobs_beyond_the_limit_wait_their_turn_in_order(fake):
    d = Path(tempfile.mkdtemp())
    jobs = sim.Jobs(limit=1)
    a, b, c = (jobs.submit(d, n, "* sleep 1\nR1 a 0 1\n.end\n", None) for n in "abc")
    assert state(a) == "running" and "queue_position" not in a.status()        # a free slot: running at once
    assert (state(b), b.status()["queue_position"]) == ("queued", 1)
    assert (state(c), c.status()["queue_position"]) == ("queued", 2)
    assert jobs.cancel(c) and state(c) == "cancelled" and "queue_position" not in c.status()
    assert until(lambda: state(b) == "running") and state(a) == "done"
    assert until(lambda: state(b) == "done") and state(c) == "cancelled"
    assert not (c.path / "omp.txt").exists()                                   # it never started
    # one ngspice thread per job: .spiceinit (read after the spinit that may set eight) and OpenMP
    assert (a.path / "omp.txt").read_text().strip() == "1"
    assert "set num_threads=1" in (a.path / ".spiceinit").read_text()
    assert not jobs.waiting and not jobs.active and not jobs.live
    shutil.rmtree(d)


def test_every_free_slot_is_taken_by_the_next_in_line(fake):
    """Slots freed while several jobs wait go to the next in line, each at once. A waiter that looked while another job
    was still first in line used to sleep on with a slot free until some job ended (for an ADC's transient, hours later):
    first in the worst order (the last in line looks first), then with jobs that end together."""
    d = Path(tempfile.mkdtemp())
    jobs = sim.Jobs(limit=3)
    line = []
    for i in range(4):                                  # in line, as when the jobs ahead of them have just ended
        j = sim.Job(jobs.runs_dir(d) / f"w{i}")
        j.path.mkdir()
        (j.path / "netlist.cir").write_text("* sleep 30\n.end\n")
        j.set(id=j.id, state="queued", created=time.time(), queue_position=i + 1)
        line.append(j)
    with jobs.turn:
        jobs.waiting += line
        jobs.live.update((j.id, j) for j in line)
    for k, j in enumerate(reversed(line)):
        threading.Thread(target=jobs._run, args=(j, tools.find("ngspice"), True, 60), daemon=True).start()
        if k < 3:                                       # it looked, and waits: not first in line
            assert until(lambda: len(jobs.turn._waiters) == k + 1)
    assert until(lambda: [state(j) for j in line] == ["running"] * 3 + ["queued"], 3), [state(j) for j in line]
    assert line[3].status()["queue_position"] == 1
    for j in line:
        jobs.cancel(j)
    assert until(lambda: not jobs.active and not jobs.waiting)
    for k in range(3):
        gate = d / f"gate{k}"
        first = [jobs.submit(d, "a", f"* gate {gate}\n.end\n", None) for _ in range(3)]
        waiters = [jobs.submit(d, "b", "* sleep 30\n.end\n", None) for _ in range(5)]
        assert [state(j) for j in first] == ["running"] * 3 and [state(j) for j in waiters] == ["queued"] * 5
        assert until(lambda: all(j.proc is not None for j in first))
        gate.write_text("")                                                    # the three end together
        assert until(lambda: [state(j) for j in waiters[:3]] == ["running"] * 3, 3), [state(j) for j in waiters]
        assert [(state(j), j.status()["queue_position"]) for j in waiters[3:]] == [("queued", 1), ("queued", 2)]
        for j in waiters:
            jobs.cancel(j)
        assert until(lambda: not jobs.active and not jobs.waiting)
    shutil.rmtree(d)


def test_the_limit_comes_from_the_machine_or_the_setting(monkeypatch):
    monkeypatch.delenv("UNDINE_SIM_JOBS", raising=False)
    assert sim.job_limit() == max(1, (os.cpu_count() or 2) - 2)
    monkeypatch.setenv("UNDINE_SIM_JOBS", "3")
    assert sim.Jobs().limit == 3


def test_a_run_at_its_time_limit_is_stopped_and_its_process_ends(fake, monkeypatch):
    monkeypatch.setattr(sim, "STOP_GRACE_S", 0.5)
    d = Path(tempfile.mkdtemp())
    jobs = sim.Jobs(limit=2)
    j = jobs.submit(d, "t", "* sleep 60\n* stubborn\n.end\n", None, timeout_s=1)      # it ignores SIGTERM
    assert j.status()["time_limit_s"] == 1
    assert until(lambda: state(j) == "failed", 15)
    st = j.status()
    assert st["timed_out"] and "time limit of 1 s" in st["error"] and "time_limit_s" in st["error"]
    assert j.proc.poll() is not None                                           # killed, not left running
    with pytest.raises(ValueError, match="time_limit_s is 1 to"):
        jobs.submit(d, "t", "* x\n.end\n", None, timeout_s=sim.TIMEOUT_MAX_S + 1)
    shutil.rmtree(d)


def test_cancel_ends_a_running_process(fake):
    d = Path(tempfile.mkdtemp())
    jobs = sim.Jobs(limit=2)
    j = jobs.submit(d, "t", "* sleep 60\n.end\n", None)
    assert until(lambda: state(j) == "running" and j.proc is not None)
    assert jobs.cancel(j)
    assert until(lambda: j.proc.poll() is not None, 3) and state(j) == "cancelled"
    assert not jobs.cancel(j)                                                  # nothing left to stop
    shutil.rmtree(d)


def test_an_analysis_given_replaces_the_testbenchs_own(fake):
    d = Path(tempfile.mkdtemp())
    jobs = sim.Jobs()
    tb = "* tb\nR1 a 0 1\n.tran 10n 2u\n.op\n.options savecurrents\n.control\nrun\n.endc\n.end\n"
    j = jobs.submit(d, "t", tb, {"type": "tran", "step": "1n", "stop": "1u", "tmax": "0.1n", "uic": True})
    text = (j.path / "netlist.cir").read_text()
    assert ".tran 1n 1u 0 0.1n uic" in text and ".tran 10n 2u" not in text and "\n.op\n" not in text
    assert ".options savecurrents" in text and j.status()["replaced_control"]
    assert until(lambda: state(j) == "done")
    assert sim.analysis_card({"type": "tran", "step": "1n", "stop": "2u", "tstart": "1u"}) == ".tran 1n 2u 1u"
    with pytest.raises(ValueError, match="tstart .*tmax .*uic"):
        sim.analysis_card({"type": "tran", "start": "1u"})
    with pytest.raises(ValueError, match="without spaces"):
        sim.analysis_card({"type": "tran", "stop": "1u\n.include /etc/passwd"})
    shutil.rmtree(d)


def test_quit_ends_a_control_block_that_runs_every_analysis():
    tb = "* t\nR1 a 0 1\n.tran 1n 1u\n.control\nset num_threads=1\nrun\nwrite out.raw\n.endc\n.end\n"
    assert "write out.raw\nquit\n.endc" in sim.quit_after_control(tb)
    assert sim.quit_after_control(tb.replace("run\n", "tran 1n 1u\n")).count("quit") == 1
    # the batch run would do the .ac the block does not: kept; a block that quits itself: kept
    two = "* t\nR1 a 0 1\n.tran 1n 1u\n.ac dec 10 1 1G\n.control\ntran 1n 1u\nwrite out.raw\n.endc\n.end\n"
    assert sim.quit_after_control(two) == two
    assert sim.quit_after_control(tb.replace("run\n", "run\nexit\n")).count("quit") == 0


def test_par_measures_beyond_ngspices_limit_become_b_sources():
    meas = "".join(f".meas tran a{i} FIND par('v(p) - v(n)') AT={i}n\n" for i in range(100))
    text = "* t\n.subckt s a\nB1 b 0 V=par('v(a)')\n.ends\n" + meas + ".control\nlet x = par('v(p)')\n.endc\n.end\n"
    out, n = sim.par_sources(text)
    assert n == 1 and "Bundine_par1 undine_par1 0 V=v(p) - v(n)\n.end" in out
    assert out.count("FIND v(undine_par1) AT=") == 100
    assert "B1 b 0 V=par('v(a)')" in out and "let x = par('v(p)')" in out             # not a top-level .meas: as is
    fewer = "".join(meas.splitlines(True)[:99])
    assert sim.par_sources(fewer) == (fewer, 0)                                        # 99 or fewer: ngspice does it


# ------------------------------------------------------------------ with ngspice


def _need():
    if not tools.find("ngspice"):
        pytest.skip("ngspice not installed")


RC = "* rc\nV1 in 0 pulse(0 1 1u 1n 1n 10u 20u)\nR1 in out 1k\nC1 out 0 1n\n.tran 10n 8u\n"


def _done(job, timeout=120):
    assert until(lambda: state(job) not in ("queued", "running"), timeout), job.log()
    return job


def test_ngspice_runs_one_thread_and_the_testbenchs_analysis_once():
    """A testbench with its own .control (here to print the thread count) used to run twice in batch mode: once in the
    block, then again after it, where a param= measure failed ("insertnumber: fails") and showed as an error."""
    _need()
    d = Path(tempfile.mkdtemp())
    tb = RC + (".meas tran tdelay TRIG v(in) VAL=0.5 RISE=1 TARG v(out) VAL=0.5 RISE=1\n.meas tran twice param='tdelay*2'\n"
               ".control\necho threads=$num_threads\nrun\nwrite out.raw\n.endc\n.end\n")
    j = _done(sim.Jobs().submit(d, "t", tb, None))
    log = j.log(2000)
    assert state(j) == "done" and "threads=1" in log and log.count("Doing analysis") == 1
    s = simresults.summary(j)
    assert s["measurements"]["twice"] == pytest.approx(2 * s["measurements"]["tdelay"], rel=1e-5)
    assert "measurement_errors" not in s and j.status()["warnings"] == []
    shutil.rmtree(d)


def test_every_failed_measure_is_reported_with_its_statement():
    _need()
    d = Path(tempfile.mkdtemp())
    tb = RC + (".meas tran a000 FIND v(out,in) AT=5u\n.meas tran a001 FIND v(out) AT=5u\n"
               ".meas tran tnever TRIG v(in) VAL=0.5 RISE=1 TARG v(out) VAL=2 RISE=1\n.end\n")
    j = _done(sim.Jobs().submit(d, "t", tb, None))
    s = simresults.summary(j)
    assert s["measurements"] == {"a001": pytest.approx(0.9817, rel=1e-3)}
    errs = s["measurement_errors"]
    assert errs["a000"] == "no such vector as v(out,in) (.meas tran a000 find v(out,in) at=5u)"
    assert errs["tnever"].startswith("trig(TARG) : out of interval (.meas tran tnever")
    assert j.status()["warnings"] == []                                        # reported per measure, not as warnings
    why = specs._missing({"measure": "a000", "meas": [], "output": None, "spectrum": None}, s, j.id)
    assert "one node per v()" in why
    shutil.rmtree(d)


def test_more_than_99_par_measures_in_one_run():
    _need()
    d = Path(tempfile.mkdtemp())
    tb = RC.replace("R1 in out 1k", "R1 in out 1k\nR2 in outb 2k\nC2 outb 0 1n") + "".join(
        f".meas tran a{i:03d} FIND par('v(out) - v(outb)') AT={1 + i * 0.05:.2f}u\n" for i in range(120)) + \
        ".meas tran b FIND par('v(out)*2') AT=5u\n.end\n"
    j = _done(sim.Jobs().submit(d, "t", tb, None))
    s = simresults.summary(j)
    assert state(j) == "done" and len(s["measurements"]) == 121, j.log()
    assert s["measurements"]["b"] == pytest.approx(2 * 0.9817, rel=1e-3)
    assert any("2 par() expressions" in n for n in s["notes"])
    shutil.rmtree(d)


def test_a_param_measure_that_uses_a_failed_one_is_reported_and_the_rest_still_count():
    """ngspice prints its expression diagnostics ("Undefined parameter [tpd]", "Cannot compute substitute") among the
    measurements: they belong to the param= measure after them, and the measures after that still count."""
    _need()
    d = Path(tempfile.mkdtemp())
    tb = RC + (".meas tran a3 FIND v(nope) AT=5u\n.meas tran a5 param='a3*2'\n"
               ".meas tran tpd TRIG v(in) VAL=0.5 RISE=1 TARG v(out) VAL=2 RISE=1\n.meas tran fmax param='1/tpd'\n"
               ".meas tran pwr AVG i(V1) FROM=1u TO=8u\n.meas tran tdel TRIG v(in) VAL=0.5 RISE=1 TARG v(out) VAL=0.5 RISE=1\n"
               ".meas tran pp PP v(out)\n.meas tran f5 FIND v(out) AT=5u\n.end\n")
    j = _done(sim.Jobs().submit(d, "t", tb, None))
    s = simresults.summary(j)
    assert state(j) == "done" and set(s["measurements"]) == {"pwr", "tdel", "pp", "f5"}, j.log()
    assert s["measurements"]["f5"] == pytest.approx(0.9817, rel=1e-3) and s["measurements"]["pwr"] < 0
    errs = s["measurement_errors"]
    assert set(errs) == {"a3", "a5", "tpd", "fmax"}
    assert errs["a5"] == "Undefined parameter [a3]" and errs["fmax"] == "Undefined parameter [tpd]"
    assert j.status()["warnings"] == [] and "warnings" not in s
    why = specs._missing({"measure": "fmax", "meas": [], "output": None, "spectrum": None}, s, j.id)
    assert "uses a measure that failed" in why
    shutil.rmtree(d)


# a transient whose time step collapses once v(a) rises (ln(-1)); a BSIM4 model that refuses its parameters, as sky130's
# ss corner refuses a 10 um nfet with nf=2 ("Fatal: Pclm = -0.000983 is not positive")
ABORT = ("* stops\nV1 a 0 pulse(0 1 1n 1n 1n 5n 10n)\nR1 a 0 1k\nB1 out 0 V = v(a) > 0.5 ? ln(-1) : 0\nR2 out 0 1k\n"
         ".tran 10p 10n\n")
FATAL = ("* refused\n.subckt sw d g s b\nXMS d g s b nbad L=0.15 W=10 nf=2\n.ends\n.subckt nbad d g s b w=1 l=1 nf=1\n"
         "M1 d g s b nbad__model W={w/nf} L={l}\n.model nbad__model nmos level=54 version=4.5 pclm=-0.001\n.ends\n"
         "x4 d g 0 0 sw\nV1 d 0 1.8\nV2 g 0 0.9\n.tran 10p 10n\n")
OWN = ".control\nrun\nwrite out.raw\n.endc\n.end\n"
MEAS = ".meas tran vmax MAX v(d)\n.end\n"


def test_a_run_whose_analysis_stops_short_fails_whatever_ngspice_exits_with():
    """Every control block Undine runs ends with `quit` (and so does a testbench's own that runs every analysis), and
    then ngspice exits with 0 also when the analysis aborted, its raw file written with what it had: the log tells. Each
    way a run is made: the testbench's own .control, its .meas (MEAS_CONTROL), analysis op (OP_CONTROL), analysis tran
    with .meas lines (check_specs)."""
    _need()
    d = Path(tempfile.mkdtemp())
    jobs = sim.Jobs()
    tran = {"type": "tran", "step": "10p", "stop": "10n"}
    for tb, analysis in ((ABORT + OWN, None), (ABORT + MEAS, None), (ABORT + MEAS, tran),
                         (FATAL + OWN, None), (FATAL + MEAS, None), (FATAL + MEAS, tran), (FATAL + OWN, {"type": "op"})):
        j = _done(jobs.submit(d, "t", tb, analysis))
        st, f = j.status(), simresults.failure(j)
        assert st["state"] == "failed" and (j.path / "out.raw").is_file(), (tb, analysis, j.log())
        if tb.startswith(ABORT):
            assert st["error"].startswith("doAnalyses: TRAN: Timestep too small"), st["error"]
            assert any(h.startswith("the transient stopped before its end") for h in f["hints"])
            assert not any("operating point" in h for h in f["hints"])
        else:
            assert st["error"] == "Fatal: Pclm = -0.001 is not positive."
            assert f["model_check"] == ["x4.xms (nbad__model, W=10 L=0.15 nf=2 (5 per finger)): "
                                        "Fatal: Pclm = -0.001 is not positive."]
            assert f["hints"][0].startswith("a transistor's size is outside what its model supports")
    # a testbench's loop: an analysis that aborted before one that finished leaves warnings, as ngspice's exit code has it
    loop = ABORT.replace("v(a) > 0.5", "(v(a) > 0.5 && k > 0)").replace(".tran 10p 10n\n", ".param k=1\n") + \
        ".control\ntran 10p 10n\nalterparam k=-1\nreset\ntran 10p 10n\nwrite out.raw\n.endc\n.end\n"
    j = _done(jobs.submit(d, "t", loop, None))
    s = simresults.summary(j)
    assert state(j) == "done" and any(w.startswith("doAnalyses: TRAN: Timestep too small") for w in s["warnings"]), j.log()
    shutil.rmtree(d)


def test_check_specs_says_that_a_run_stopped_short():
    """check_specs on a testbench whose transient aborts (a B source's ln(-1) once the input rises): the spec's run
    failed, with ngspice's reason and what it usually means, not "its .meas gave no value"."""
    _need()
    from test_simresults import _project, _tb
    root, srv = _project()
    ctx = _tb(srv, "rcb", [("devices/vsource.sym", 0, {"name": "V1", "value": "pulse(0 1 1u 1n 1n 10u 20u)"}),
                           ("devices/res.sym", 100, {"name": "R1", "value": "1k"}),
                           ("devices/capa.sym", 200, {"name": "C1", "value": "1n"}),
                           ("devices/code_shown.sym", 300, {"name": "BAD", "value": "B9 bad 0 V = v(in) > 0.5 ? ln(-1) : 0\nR9 bad 0 1k"})],
              {"in": ["V1:p", "R1:P"], "out": ["R1:M", "C1:p"], "0": ["V1:m", "C1:m"]})
    res, err = T.call(ctx, "add_intent", {"lib": "l", "cell": "rcb", "kind": "spec", "text": "output below 2 V", "reason": "r",
                                          "spec": {"measure": "vmax", "max": 2, "meas": ".meas tran vmax MAX v(out)",
                                                   "testbench": "rcb", "analysis": {"type": "tran", "step": "10n", "stop": "8u"}}})
    assert not err, res
    res, err = T.call(ctx, "check_specs", {"lib": "l", "cell": "rcb"})
    assert not err, res
    r = json.loads(res)
    assert r["specs"][0]["status"] != "met" and "failed" in json.dumps(r["specs"][0]), r
    fail = r["failures"][0]
    assert any("Timestep too small" in e for e in fail["errors"]) and fail["hints"][0].startswith("the transient stopped")
    assert "gave no value" not in res
    shutil.rmtree(root)


# ------------------------------------------------------------------ the tools


@pytest.fixture
def design(fake):
    root, srv, cs = tc.setup()
    yield srv, fake
    shutil.rmtree(root)


def test_a_long_run_is_followed_collected_and_cancelled(design):
    srv, sleep = design
    sleep.write_text("60")
    me = T.ToolCtx(srv, "p", actor="agent:a", owner="mcp:a", owner_kind="mcp")
    assert not T.call(me, "open_changeset", {"title": "t", "reason": "r"})[1]
    text, err = T.call(me, "run_simulation", {"lib": "l", "cell": "top", "wait_s": 0, "time_limit_s": 7200})
    out = json.loads(text)
    assert not err and out["state"] == "running" and "errors" not in out and "hints" not in out     # a slot was free
    jid = out["job"]
    assert f"job='{jid}'" in out["next"] and f"job:{jid}" in out["next"] and "cancel_job" in out["next"]
    job = srv.jobs.get(srv.project(me.work).path, jid)
    assert job.status()["time_limit_s"] == 7200 and job.status()["owner"] == "mcp:a"
    assert until(lambda: state(job) == "running")
    text, err = T.call(me, "wait_for", {"events": [f"job:{jid}"], "timeout_s": 1})
    assert json.loads(text)["jobs"] == {jid: "running"}
    # another session sees only the project: not this job
    other = T.ToolCtx(srv, "p", actor="agent:b", owner="mcp:b", owner_kind="mcp")
    text, err = T.call(other, "cancel_job", {"job": jid})
    assert err and "no job" in text
    text, err = T.call(me, "cancel_job", {"job": jid})
    assert not err and json.loads(text)["state"] == "cancelled"
    assert until(lambda: job.proc.poll() is not None, 3)
    assert json.loads(T.call(me, "wait_for", {"events": [f"job:{jid}"]})[0])["state"] == "cancelled"
    text, err = T.call(me, "run_simulation", {"lib": "l", "cell": "top", "job": jid})
    assert err and "cancelled" in text and "outside what its model" not in text
    # a finished run's results, later and as often as wanted
    sleep.write_text("0")
    jid2 = json.loads(T.call(me, "run_simulation", {"lib": "l", "cell": "top"})[0])["job"]
    text, err = T.call(me, "run_simulation", {"lib": "l", "cell": "top", "job": jid2})
    assert not err and json.loads(text)["job"] == jid2 and json.loads(text)["state"] == "done"
    assert "next" in json.loads(T.call(me, "wait_for", {"events": [f"job:{jid2}"]})[0])
    text, err = T.call(me, "run_simulation", {"lib": "l", "cell": "top", "job": jid2, "analysis": {"type": "op"}})
    assert err and "describe a new run" in text
    text, err = T.call(me, "run_simulation", {"lib": "l", "cell": "nope", "job": jid2})
    assert err


def test_a_job_an_earlier_server_left_running_is_interrupted(design):
    """A restart leaves status files queued or running with no process behind them (a sweep's queue, the runs the old
    server's end killed): they are not reused, and following or collecting one answers at once that it is interrupted."""
    srv, sleep = design
    me = T.ToolCtx(srv, "p", actor="agent:a", owner="mcp:a", owner_kind="mcp")
    a = srv.start_sim(me.project, "l", "top", {"type": "op"}, reuse=True)
    assert until(lambda: state(a) == "done") and srv.start_sim(me.project, "l", "top", {"type": "op"}, reuse=True).id == a.id
    jid = json.loads(T.call(me, "run_simulation", {"lib": "l", "cell": "top"})[0])["job"]
    for job in (a, srv.jobs.get(srv.project("p").path, jid)):                  # as the old server left them
        job.set(state="running", created=srv.jobs.started - 60, started=srv.jobs.started - 50, owner="mcp:a",
                drop=("finished", "seconds"))
    assert srv.jobs.status(a)["state"] == "interrupted"
    b = srv.start_sim(me.project, "l", "top", {"type": "op"}, reuse=True)
    assert b.id != a.id                                                         # not reused: it would never finish
    t0 = time.time()
    text, err = T.call(me, "run_simulation", {"lib": "l", "cell": "top", "job": jid})
    assert err and text.startswith("simulation interrupted") and "run it again" in text and "still running" not in text
    out = json.loads(T.call(me, "wait_for", {"events": [f"job:{jid}"], "timeout_s": 30})[0])
    assert out["state"] == "interrupted" and out["waited_s"] == 0 and "runs it again" in out["next"]
    assert json.loads(T.call(me, "cancel_job", {"job": jid})[0])["state"] == "interrupted"
    assert time.time() - t0 < 5
    assert until(lambda: state(b) == "done")
    assert srv.jobs.status(b)["state"] == "done"                                # this server's own: as its file says


def test_cancel_job_takes_only_the_callers_jobs_in_the_project(design):
    srv, sleep = design
    sleep.write_text("60")
    a = T.ToolCtx(srv, "p", actor="agent:a", owner="mcp:a", owner_kind="mcp")       # works in the project itself
    jid = json.loads(T.call(a, "run_simulation", {"lib": "l", "cell": "top", "wait_s": 0})[0])["job"]
    b = T.ToolCtx(srv, "p", actor="agent:b", owner="mcp:b", owner_kind="mcp")
    T.call(b, "open_changeset", {"title": "t", "reason": "r"})
    for who in (b, T.ToolCtx(srv, "p", actor="agent:c", owner="mcp:c", owner_kind="mcp")):
        text, err = T.call(who, "cancel_job", {"job": jid})
        assert err and "not one of yours" in text
    text, err = T.call(a, "cancel_job", {"job": jid})
    assert not err and json.loads(text)["state"] == "cancelled"
    assert json.loads(T.call(a, "cancel_job", {"job": jid})[0])["note"].startswith("it is not running")


def test_the_mcp_endpoint_answers_while_a_long_tool_call_runs(monkeypatch):
    """Tool calls run in worker threads: a wait of several seconds (run_simulation, check_specs, wait_for) no longer holds
    up the event loop, so other clients (and this one) get their answers meanwhile. Per-session state (the changeset a
    call opened) and request ids (a write retried while the first call runs acts once) hold with calls in parallel."""
    from fastapi.testclient import TestClient
    from undine.server.api import create_app
    root, srv, cs = tc.setup()
    srv.mcp_enabled = True
    H = {"authorization": f"Bearer {mcp.ensure_token(root)}"}
    with TestClient(create_app(srv, None)) as c:
        r = c.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                            "params": {"protocolVersion": "2025-03-26", "clientInfo": {"name": "t"}}})
        H = {**H, "mcp-session-id": r.headers["mcp-session-id"]}

        def call(i, name, **a):
            return c.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                                   "params": {"name": name, "arguments": {"project": "p", **a}}}).json()["result"]
        box = {}
        long = threading.Thread(target=lambda: box.update(r=call(2, "wait_for", events=["selection"], timeout_s=4)))
        long.start()
        time.sleep(0.5)
        t0 = time.time()
        assert c.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": 3, "method": "ping"}).status_code == 200
        assert c.get("/api/health").json()["undine"]
        assert not call(4, "open_changeset", title="t", reason="r")["isError"]
        quick = time.time() - t0
        assert long.is_alive() and quick < 2.0, quick
        assert json.loads(call(5, "get_changeset")["content"][0]["text"])                 # the session's changeset
        # the same write twice at once (a retry while the first still runs): it acts once
        slow = T.BY_NAME["set_instance_params"]["fn"]
        monkeypatch.setitem(T.BY_NAME["set_instance_params"], "fn", lambda ctx, a: (time.sleep(0.5), slow(ctx, a))[1])
        res = []
        args = dict(lib="l", cell="top", instance="R1", params={"value": "2k"}, reason="x", request_id="r-1")
        both = [threading.Thread(target=lambda i=i: res.append(call(10 + i, "set_instance_params", **args))) for i in range(2)]
        for th in both:
            th.start()
        for th in both:
            th.join()
        assert [r["isError"] for r in res] == [False, False]
        assert sorted(bool((r.get("_meta") or {}).get("undine/replayed")) for r in res) == [False, True]
        long.join()
        assert json.loads(box["r"]["content"][0]["text"])["timeout"] is True
    shutil.rmtree(root)


# ------------------------------------------------------------------ standard cells


def test_standard_cells_in_a_testbench_get_their_spice_and_any_corner():
    from undine.pdks.pdk import default_pdk_roots, discover
    from undine.server.api import Server
    pdks = {k: v for k, v in discover(default_pdk_roots()).items() if k.startswith("sky130")}
    if not pdks:
        pytest.skip("sky130 not installed")
    root = Path(tempfile.mkdtemp())
    srv = Server(root, pdks, [root])
    prj = srv.store.create("p", sorted(pdks)[0])
    prj.create_library("l")
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    T.call(ctx, "open_changeset", {"title": "tb", "reason": "r"})
    T.call(ctx, "create_cell", {"lib": "l", "cell": "tb", "reason": "tb"})
    ed = srv.editor(ctx.work, "l", "tb", "schematic")
    if ed.resolver.load("sky130_stdcells/inv_1.sym", ed.path.parent) is None or \
            ed.resolver.load("devices/vsource.sym", ed.path.parent) is None:
        shutil.rmtree(root)
        pytest.skip("Xschem's sky130 symbols are not installed")
    # the cell's supplies are properties of the PDK's symbol (nets by name), its pins A and Y
    for sym, x, params in [("sky130_stdcells/inv_1.sym", 0, {"name": "x1", "VPWR": "vdd", "VPB": "vdd", "VGND": "0", "VNB": "0"}),
                           ("devices/vsource.sym", 100, {"name": "V1", "value": "1.8"}),
                           ("devices/vsource.sym", 200, {"name": "V2", "value": "0"})]:
        assert not T.call(ctx, "add_instance", {"lib": "l", "cell": "tb", "symbol": sym, "x": x, "y": 0, "params": params,
                                                "reason": "p"})[1]
    for net, pins in {"vdd": ["V1:p"], "a": ["V2:p", "x1:A"], "y": ["x1:Y"], "0": ["V1:m", "V2:m"]}.items():
        res, err = T.call(ctx, "connect_pins", {"lib": "l", "cell": "tb", "net": net, "pins": pins, "reason": "n"})
        assert not err, res
    lib_spice = prj.pdk.root / "libs.ref" / "sky130_fd_sc_hd" / "spice" / "sky130_fd_sc_hd.spice"
    job = srv.start_sim(ctx.work, "l", "tb", {"type": "op"}, corner="ss")
    text = (job.path / "netlist.cir").read_text()
    assert f".include {lib_spice}" in text and text.count("sky130_fd_sc_hd.spice") == 1
    assert " ss\n" in text and any("standard cells" in n for n in job.status()["notes"])
    srv.jobs.cancel(job)
    # included by the testbench itself (a code block): not twice, and a corner can still be chosen
    assert not T.call(ctx, "add_instance", {"lib": "l", "cell": "tb", "symbol": "devices/code_shown.sym", "x": 300, "y": 0,
                                            "params": {"name": "STD", "value": f".include {lib_spice}"}, "reason": "p"})[1]
    job = srv.start_sim(ctx.work, "l", "tb", {"type": "op"}, corner="ff")
    text = (job.path / "netlist.cir").read_text()
    assert text.count("sky130_fd_sc_hd.spice") == 1 and " ff\n" in text
    # the models reduced to what the testbench and the cells' SPICE use (their p-channel devices are hvt)
    copy = Path(job.status()["models"][0]["reduced"]).read_text()
    assert ".subckt  sky130_fd_pr__pfet_01v8_hvt " in copy and "sky130_fd_pr__nfet_g5v0d10v5 " not in copy
    _done(job, 300)
    s = simresults.summary(job)
    assert state(job) == "done" and s["node_voltages"]["y"] > 1.7, job.log()                 # an inverter: in 0, out high
    shutil.rmtree(root)
