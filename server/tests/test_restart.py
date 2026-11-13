"""A server restart (to deploy a change) loses no work (sim.py's module docstring, mcp.Sessions, client.py).

Simulations: a real ngspice still running when the server goes is adopted by the next server on the same root and
finished as any job (done, its measurements, wait_for job:<id>), also one an earlier Undine started without recording
its process (found by its directory and command; left to that server while it still runs it), one a server handed
over while it is still on its way out, and one whose server was killed; one whose ngspice is gone is interrupted (and
a DRC a stopped server left), one whose ngspice finished meanwhile is collected; an adopted job keeps its time limit
and can be cancelled; the queue goes on in its order; a second server on the root takes nothing until the first has
gone; --stop-jobs stops them all; a wait answers at once when the server is asked to stop; `undine serve` itself, with
SIGTERM, SIGKILL and --stop-jobs.

Agents: an MCP session goes on after a restart with its changeset (and a stopped agent stays stopped); the shell client
continues its changeset in a new session when the server forgot the old one, and a call made while the server is down
waits for it; a write sent while the server restarts is sent again until it is back, and acts once; mcp-stdio waits
too."""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from undine import tools  # noqa: E402
from undine.agents import agent_tools as T, client as cl, mcp, mcp_local  # noqa: E402
from undine.agents.client import Undine  # noqa: E402
from undine.server.api import Server, create_app  # noqa: E402
from undine.simulation import sim, simresults  # noqa: E402
import test_changeset as tc  # noqa: E402
from test_client import cli, setup as client_setup  # noqa: E402
from test_mcp_local import call, setup as mcp_setup, transport  # noqa: E402

SERVER = Path(__file__).resolve().parents[1]


def _need():
    if not tools.find("ngspice"):
        pytest.skip("ngspice not installed")


def ladder(stop_us: float) -> str:
    """An RC ladder driven by a 50 % pulse: about 0.4 s of ngspice per microsecond simulated; its average is 0.51."""
    lines = ["* rc ladder", "V1 n0 0 PULSE(0 1 0 1n 1n 50n 100n)"]
    for i in range(40):
        lines += [f"R{i} n{i} n{i + 1} 1k", f"C{i} n{i + 1} 0 10f"]
    lines += [f".tran 0.05n {stop_us:g}u", ".save v(n40)",
              f".meas tran vavg avg v(n40) from={stop_us / 2:g}u to={stop_us:g}u", ".end"]
    return "\n".join(lines) + "\n"


def until(cond, timeout=30.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    return False


def started(job) -> dict:
    """The job's process once it runs (its status records it)."""
    assert until(lambda: "start" in (job.status().get("process") or {})), job.status()
    return job.status()["process"]


def alive(rec: dict) -> bool:
    return sim.Foreign(rec["pid"], rec["start"]).running()


@pytest.fixture
def root():
    d = Path(tempfile.mkdtemp())
    (d / "p").mkdir()
    yield d
    for jobs in list(sim._ALL):                    # a test that failed half way leaves no ngspice behind
        if jobs.root == d and not jobs.closed:
            jobs.shutdown(stop_jobs=True)
    shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------ simulations


def test_a_running_simulation_goes_on_in_the_next_server():
    """The server object goes (as `undine serve` on SIGTERM or Ctrl-C), its ngspice runs on, and the next server on
    the root finishes the job as its own: done, with its measurements, and a waiter (wait_for job:<id>) sees it."""
    _need()
    root, srv, _ = tc.setup()
    job = srv.jobs.submit(srv.project("p").path, "ladder", ladder(12), None, meta={"kind": "sim"})
    rec = started(job)
    srv.jobs.shutdown()
    assert alive(rec) and not (root / sim.QUEUE).exists()
    srv2 = Server(root, {}, [root])
    assert srv2.jobs.owner and srv2.jobs.resumed == {"adopted": [job.id], "queued": []}
    assert job.id in srv2.jobs.live and srv2.jobs.status(job)["state"] == "running"
    me = T.ToolCtx(srv2, "p", actor="agent:a")
    out = json.loads(T.call(me, "wait_for", {"events": [f"job:{job.id}"], "timeout_s": 120})[0])
    assert out["state"] == "done", job.status()
    st = job.status()
    assert st["state"] == "done" and st["adopted"] and st["raw"] == ["out.raw"] and st["process"] == rec
    assert simresults.measurements(job.log(5000))["vavg"] == pytest.approx(0.51, abs=0.01)
    assert not alive(rec) and job.id not in srv2.jobs.live
    srv2.jobs.shutdown()
    shutil.rmtree(root)


def old_job(root: Path, netlist: str) -> Path:
    """A job as an Undine before this change left it running: its status says so, and records no process."""
    d = root / "p" / "runs" / "20261103-120000-abcd"
    d.mkdir(parents=True)
    (d / "netlist.cir").write_text(sim._before_end(netlist, sim.MEAS_CONTROL))
    (d / ".spiceinit").write_text(sim.spiceinit(None))
    t = time.time() - 1
    (d / "status.json").write_text(json.dumps({"id": d.name, "title": "ladder", "kind": "sim", "state": "running",
                                               "created": t, "started": t, "analysis": "netlist", "time_limit_s": 600.0}))
    return d


NGSPICE = ["-b", "-o", "log.txt", "netlist.cir"]          # as an Undine before this change ran it


def test_a_simulation_an_earlier_undine_started_is_found_by_its_directory(root):
    """A server without this change recorded no process: its ngspice is found by the job's directory and command line
    (it is even this process's child here, never waited for: a zombie once it ends), and finished as any job."""
    _need()
    d = old_job(root, ladder(8))
    old = subprocess.Popen([tools.find("ngspice"), *NGSPICE], cwd=d, stdout=subprocess.DEVNULL,
                           stderr=subprocess.STDOUT, env=tools.env())
    try:
        jobs = sim.Jobs(root=root)
        assert jobs.resumed["adopted"] == [d.name]
        job = jobs.get(root / "p", d.name)
        assert job.status()["process"]["pid"] == old.pid and job.status()["process"]["cmd"][-1] == "netlist.cir"
        assert until(lambda: job.status()["state"] != "running", 120)
        assert job.status()["state"] == "done", job.status()
        assert simresults.measurements(job.log(5000))["vavg"] == pytest.approx(0.51, abs=0.01)
        jobs.shutdown()
    finally:
        old.kill()
        old.wait()


def test_a_simulation_an_earlier_undine_still_runs_is_adopted_once_it_has_gone(root, monkeypatch):
    """An Undine before this change, still on its way out, runs its ngspice as its child (and records its end itself
    if it ends first): the next server leaves it to that one (parked: running, worked on, not interrupted), and adopts
    it once that one has gone."""
    _need()
    monkeypatch.setattr(sim, "PARK_POLL_S", 0.2)
    d = old_job(root, ladder(8))
    # the old server: its command line `undine serve`, ngspice its child
    old = subprocess.Popen([sys.executable, "-c", "import subprocess, sys, time; subprocess.Popen(sys.argv[4:], "
                            "cwd=sys.argv[1], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT); time.sleep(600)",
                            str(d), "undine", "serve", tools.find("ngspice"), *NGSPICE], env=tools.env())
    try:
        assert until(lambda: any(sim._ngspice(p["argv"]) for p in sim._procs(d)))
        jobs = sim.Jobs(root=root)
        job = jobs.get(root / "p", d.name)
        assert jobs.resumed["adopted"] == [] and d.name in jobs.parked
        assert jobs.status(job)["state"] == "running" and jobs.working(job) and job.id not in jobs.live
        old.kill()                                     # it has gone: its ngspice is init's
        old.wait()
        assert until(lambda: job.id in jobs.live or job.status()["state"] != "running", 10), job.status()
        assert not jobs.parked and job.status()["process"]["cmd"][-1] == "netlist.cir"
        assert until(lambda: job.status()["state"] != "running", 120)
        assert job.status()["state"] == "done", job.status()
        assert simresults.measurements(job.log(5000))["vavg"] == pytest.approx(0.51, abs=0.01)
        jobs.shutdown()
    finally:
        old.kill()
        old.wait()


def test_a_simulation_handed_over_is_adopted_while_its_server_is_still_on_its_way_out(root):
    """A server that handed its jobs over (shutdown) may take a while to end, its ngspice still its child: the next
    one adopts them at once all the same."""
    _need()
    script = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(SERVER)!r})
        from pathlib import Path
        from undine.simulation import sim
        jobs = sim.Jobs(root=Path({str(root)!r}))
        job = jobs.submit(Path({str(root / 'p')!r}), "ladder", sys.stdin.read(), None)
        while "start" not in (job.status().get("process") or {{}}):
            time.sleep(0.05)
        jobs.shutdown()
        print(job.id, flush=True)
        time.sleep(600)
    """)
    p = subprocess.Popen([sys.executable, "-c", script, "undine", "serve"], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, text=True,
                         env={**os.environ, "PATH": os.path.dirname(tools.find("ngspice")) + os.pathsep + os.environ["PATH"]})
    try:
        p.stdin.write(ladder(8))
        p.stdin.close()
        jid = p.stdout.readline().strip()
        job = sim.Job(root / "p" / "runs" / jid)
        assert job.status()["handed_over"] and sim._serving(sim._proc_stat(job.status()["process"]["pid"])[1])
        jobs = sim.Jobs(root=root)
        assert jobs.resumed["adopted"] == [jid]
        assert until(lambda: job.status()["state"] != "running", 120)
        assert job.status()["state"] == "done", job.status()
        assert simresults.measurements(job.log(5000))["vavg"] == pytest.approx(0.51, abs=0.01)
        jobs.shutdown()
    finally:
        p.kill()
        p.wait()


def test_a_killed_servers_simulation_goes_on_in_the_next_one(root):
    """A server process killed outright (SIGKILL: nothing of it runs on its way out): its ngspice, no longer anyone's
    child, is adopted by the next server and finished."""
    _need()
    script = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(SERVER)!r})
        from pathlib import Path
        from undine.simulation import sim
        jobs = sim.Jobs(root=Path({str(root)!r}))
        job = jobs.submit(Path({str(root / 'p')!r}), "ladder", sys.stdin.read(), None)
        while "start" not in (job.status().get("process") or {{}}):
            time.sleep(0.05)
        print(job.id, flush=True)
        time.sleep(600)
    """)
    p = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                         env={**os.environ, "PATH": os.path.dirname(tools.find("ngspice")) + os.pathsep + os.environ["PATH"]})
    p.stdin.write(ladder(10))
    p.stdin.close()
    jid = p.stdout.readline().strip()
    p.send_signal(signal.SIGKILL)
    p.wait()
    jobs = sim.Jobs(root=root)                    # the lock went with the killed process
    job = jobs.get(root / "p", jid)
    rec = job.status()["process"]
    assert jobs.owner and jobs.resumed["adopted"] == [jid] and alive(rec)
    assert until(lambda: job.status()["state"] != "running", 120)
    assert job.status()["state"] == "done", job.status()
    assert simresults.measurements(job.log(5000))["vavg"] == pytest.approx(0.51, abs=0.01)
    jobs.shutdown()


def test_a_simulation_whose_ngspice_is_gone_is_interrupted_and_one_that_finished_is_collected(root):
    _need()
    jobs = sim.Jobs(root=root)
    killed = jobs.submit(root / "p", "killed", ladder(30), None)
    ended = jobs.submit(root / "p", "ended", ladder(3), None)
    a, b = started(killed), started(ended)
    jobs.shutdown()
    os.kill(a["pid"], signal.SIGKILL)                  # while no server runs
    assert until(lambda: not alive(a) and not alive(b), 60)
    jobs = sim.Jobs(root=root)
    assert jobs.resumed == {"adopted": [], "queued": []}
    st = jobs.status(killed)
    assert st["state"] == "interrupted" and st["error"] == sim.INTERRUPTED["running"]
    assert killed.status()["state"] == "running"       # its file says what the last server knew
    assert not jobs.cancel(killed) and jobs.find(root / "p", "-", ("running",)) is None
    # its log says ngspice finished: collected as the server that started it would have
    assert until(lambda: jobs.status(ended)["state"] == "done", 30), ended.status()
    assert simresults.measurements(ended.log(5000))["vavg"] == pytest.approx(0.51, abs=0.01)
    jobs.shutdown()


def test_an_adopted_simulation_keeps_its_time_limit(root):
    _need()
    jobs = sim.Jobs(root=root)
    job = jobs.submit(root / "p", "long", ladder(200), None, timeout_s=4)
    rec = started(job)
    jobs.shutdown()
    jobs = sim.Jobs(root=root)
    assert jobs.resumed["adopted"] == [job.id]
    assert until(lambda: job.status()["state"] != "running", 30)
    st = job.status()
    assert st["state"] == "failed" and st["timed_out"] and "time limit of 4 s" in st["error"]
    assert st["finished"] - st["started"] < 15 and until(lambda: not alive(rec), 10)
    jobs.shutdown()


def test_an_adopted_simulation_can_be_cancelled(root):
    _need()
    jobs = sim.Jobs(root=root)
    job = jobs.submit(root / "p", "long", ladder(200), None)
    rec = started(job)
    jobs.shutdown()
    jobs = sim.Jobs(root=root)
    assert jobs.cancel(job) and job.status()["state"] == "cancelled"
    assert until(lambda: not alive(rec), 10)
    time.sleep(1)                                      # its end, seen by the server, does not change that
    assert job.status()["state"] == "cancelled" and job.id not in jobs.live
    jobs.shutdown()


def test_the_queue_goes_on_in_its_order_in_the_next_server(root):
    _need()
    jobs = sim.Jobs(limit=1, root=root)
    first = jobs.submit(root / "p", "first", ladder(6), None)
    second = jobs.submit(root / "p", "second", ladder(1), None)
    third = jobs.submit(root / "p", "third", ladder(1), None)
    started(first)
    assert [j.status()["state"] for j in (second, third)] == ["queued", "queued"]
    jobs.shutdown()
    kept = [json.loads(line) for line in (root / sim.QUEUE).read_text().splitlines()]
    assert [k.get("add") for k in kept if "add" in k][-2:] == [f"p/runs/{second.id}", f"p/runs/{third.id}"]
    jobs = sim.Jobs(limit=1, root=root)
    assert jobs.resumed == {"adopted": [first.id], "queued": [second.id, third.id]}
    assert [j.status()["queue_position"] for j in (second, third)] == [1, 2]      # behind the adopted one
    assert until(lambda: all(j.status()["state"] == "done" for j in (first, second, third)), 120), \
        [j.status() for j in (first, second, third)]
    assert first.status()["finished"] <= second.status()["started"] < third.status()["started"]
    assert not (root / sim.QUEUE).exists()
    jobs.shutdown()


def test_a_second_server_on_the_root_takes_nothing_until_the_first_has_gone(root):
    _need()
    a = sim.Jobs(root=root)
    job = a.submit(root / "p", "long", ladder(200), None)
    rec = started(job)
    b = sim.Jobs(root=root)                            # one too many, or the next one while the last shuts down
    assert a.owner and not b.owner and b.resumed == {"adopted": [], "queued": []}
    assert b.status(job)["state"] == "running" and not b.cancel(b.get(root / "p", job.id)) and job.id not in b.live
    a.shutdown()
    assert until(lambda: b.resumed["adopted"] == [job.id], 10) and b.owner and job.id in b.live
    b.shutdown(stop_jobs=True)
    assert job.status()["state"] == "cancelled" and "--stop-jobs" in job.status()["error"] and not alive(rec)


@pytest.mark.parametrize("how", ["stopped", "killed"])
def test_what_a_second_server_leaves_goes_on_in_the_one_that_holds_the_root(root, how):
    """A second server on the root stops (or is killed) before the one that holds it: its running simulation is
    adopted and the one it had queued runs, by the holder, without a restart of its own; a status read takes them up at
    once (else within PARK_POLL_S)."""
    _need()
    a = sim.Jobs(root=root)
    b = sim.Jobs(limit=1, root=root)
    # long enough to be still running when the holder looks again (PARK_POLL_S) on an idle machine as well
    run = b.submit(root / "p", "run", ladder(40), None)
    queued = b.submit(root / "p", "next", ladder(1), None)
    rec = started(run)
    assert a.owner and b.others and queued.status()["state"] == "queued"
    assert a.status(run)["state"] == "running" and a.working(run) and run.id not in a.live           # b's
    assert a.working(queued) and not a.cancel(a.get(root / "p", queued.id))
    if how == "stopped":
        b.shutdown()
        # the queued one may have run and ended by the time this looks (it is short)
        assert until(lambda: run.id in a.live and (queued.id in a.live or queued.status().get("resumed"))
                     and not list(root.glob(sim.OTHERS)), 10)
    else:                                              # as if its process were gone: nothing handed over
        b.closed = b._released = True
        assert a.status(queued)["state"] == "queued" and queued.id in a.live and run.id in a.live
    assert alive(rec) and not list(root.glob(sim.OTHERS))
    assert until(lambda: all(j.status()["state"] == "done" for j in (run, queued)), 120), \
        [j.status() for j in (run, queued)]
    assert run.status()["adopted"] and run.status()["process"] == rec
    assert simresults.measurements(run.log(5000))["vavg"] == pytest.approx(0.51, abs=0.01)
    a.shutdown()


def test_a_job_that_starts_while_its_server_stops_is_started_once(root, monkeypatch):
    """The server stops while a job's ngspice is being started (slow under load): it lets the start end and records
    the process before it lets go of the root, so the next server adopts that ngspice instead of starting another."""
    _need()
    real, calls, entered = sim.Process, [], threading.Event()

    def slow(cmd, **kw):
        if kw.get("cwd") is not None and Path(kw["cwd"]).parent == root / "p" / "runs":
            calls.append(Path(kw["cwd"]).name)
            entered.set()
            time.sleep(1)
        return real(cmd, **kw)
    monkeypatch.setattr(sim, "Process", slow)
    a = sim.Jobs(root=root)
    b = sim.Jobs(root=root)                            # the next server, waiting for the root
    job = a.submit(root / "p", "ladder", ladder(4), None)
    assert entered.wait(10)
    a.shutdown()
    assert job.status()["handed_over"] and "start" in job.status()["process"]
    assert until(lambda: b.resumed["adopted"], 10) and b.resumed == {"adopted": [job.id], "queued": []}
    assert until(lambda: job.status()["state"] == "done", 120), job.status()
    assert calls == [job.id]
    b.shutdown()


def test_stop_jobs_stops_the_simulations_with_the_server(root):
    _need()
    jobs = sim.Jobs(limit=1, root=root)
    running = jobs.submit(root / "p", "long", ladder(200), None)
    queued = jobs.submit(root / "p", "next", ladder(1), None)
    rec = started(running)
    jobs.shutdown(stop_jobs=True)
    assert not alive(rec) and [j.status()["state"] for j in (running, queued)] == ["cancelled", "cancelled"]
    assert not (root / sim.QUEUE).exists()
    jobs = sim.Jobs(root=root)
    assert jobs.resumed == {"adopted": [], "queued": []}
    jobs.shutdown()


def test_a_scripts_simulations_end_with_it(root):
    """Jobs without a root (a script's) or a server's that was never handed over (shutdown) end with the process, as
    before: nothing would collect their results."""
    _need()
    script = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(SERVER)!r})
        from pathlib import Path
        from undine.simulation import sim
        job = sim.Jobs().submit(Path({str(root / 'p')!r}), "ladder", sys.stdin.read(), None)
        while "start" not in (job.status().get("process") or {{}}):
            time.sleep(0.05)
        print(job.id, flush=True)
    """)
    out = subprocess.run([sys.executable, "-c", script], input=ladder(200), capture_output=True, text=True, timeout=60,
                         env={**os.environ, "PATH": os.path.dirname(tools.find("ngspice")) + os.pathsep + os.environ["PATH"]})
    rec = json.loads((root / "p" / "runs" / out.stdout.strip() / "status.json").read_text())["process"]
    assert until(lambda: not alive(rec), 10)


def test_a_job_of_another_kind_a_stopped_server_left_is_interrupted_with_why(root):
    """Only simulations are taken up: a DRC a stopped server left running is interrupted, and says so."""
    d = root / "p" / "runs" / "20261103-120000-drc0"
    d.mkdir(parents=True)
    t = time.time() - 1
    (d / "status.json").write_text(json.dumps({"id": d.name, "kind": "drc", "state": "running", "created": t}))
    jobs = sim.Jobs(root=root)
    st = jobs.status(sim.Job(d))
    assert st["state"] == "interrupted" and st["error"] == sim.INTERRUPTED["other"] and not jobs.working(sim.Job(d))
    jobs.shutdown()


def _gone_server() -> dict:
    """A job status's "server" naming a server process that has gone."""
    p = subprocess.Popen(["sleep", "30"])
    st = sim._proc_stat(p.pid)
    p.kill()
    p.wait()
    return {"pid": p.pid, "start": st[2], "boot": sim._boot(), "jobs": "gone00000000"}


def _orphan(cwd: Path, *argv: str) -> dict:
    """A tool a server killed outright left running: the leader of a session of its own (sim.Process), no longer
    anyone's child; its identity as its job's status records it."""
    argv = argv or ("sleep", "60")
    out = subprocess.run([sys.executable, "-c", "import subprocess, sys; print(subprocess.Popen(sys.argv[2:], "
                          "start_new_session=True, cwd=sys.argv[1], stdout=subprocess.DEVNULL, "
                          "stderr=subprocess.DEVNULL).pid)", str(cwd), *argv],
                         capture_output=True, text=True, check=True)
    pid = int(out.stdout)
    assert until(lambda: sim._proc_stat(pid) is not None)
    return sim.identity(pid, list(argv), cwd)


def _end(rec: dict) -> None:
    if alive(rec):
        os.killpg(rec["pid"], signal.SIGKILL)


def test_what_a_killed_servers_check_left_running_is_stopped_by_the_next_server(root, tmp_path):
    """A DRC's tools run in a session of their own, so a server killed outright (kill -9: no exit of its ends them)
    leaves them running. The next server stops them: at its start when they work in the run, else once it sees the
    run; the run is interrupted. Those of a server that still runs are left alone."""
    gone = _gone_server()
    runs = []
    for name, cwd in (("drc0", None), ("drc1", tmp_path), ("drc2", tmp_path)):
        d = root / "p" / "runs" / f"20261103-120000-{name}"
        d.mkdir(parents=True)
        runs.append((sim.Job(d), _orphan(cwd or d)))
    living = subprocess.Popen(["sleep", "60"])                 # another server on the root, still there
    st = sim._proc_stat(living.pid)
    live_server = {"pid": living.pid, "start": st[2], "boot": sim._boot(), "jobs": "live00000000"}
    t = time.time() - 1
    for (job, rec), server in zip(runs, (gone, gone, live_server)):
        (job.path / "status.json").write_text(json.dumps({"id": job.id, "kind": "drc", "state": "running",
                                                          "created": t, "server": server, "process": rec}))
    try:
        jobs = sim.Jobs(root=root)
        assert jobs.owner
        assert until(lambda: not alive(runs[0][1]), 15)          # at the start: it works in the run
        assert alive(runs[1][1]) and alive(runs[2][1])
        st = jobs.status(runs[1][0])                             # read: interrupted, and its tools stopped
        assert st["state"] == "interrupted" and st["error"] == sim.INTERRUPTED["other"]
        assert until(lambda: not alive(runs[1][1]), 15)
        assert jobs.status(runs[2][0])["state"] == "running" and alive(runs[2][1])
        assert not jobs.cancel(runs[0][0])                       # nothing left to stop
        jobs.shutdown()
    finally:
        for _, rec in runs:
            _end(rec)
        living.kill()
        living.wait()


def test_a_magic_check_runs_as_any_job_and_a_later_server_says_it_was_interrupted(root):
    """Magic's DRC, Magic + Netgen's LVS and the extraction run their steps through magicflow._run_seq: each step is
    a process group of its own recorded in the status (sim.Process) with the server that runs it (verify._new_job),
    a cancel ends the group, what a finished one wrote has its hashes and stamps (for the evidence), and a later
    server says a running one it finds was interrupted, with why."""
    from undine.checks import magicflow, verify

    jobs = sim.Jobs(root=root)
    prj = root / "p"
    env = dict(os.environ)
    done = verify._new_job(jobs, prj, "drc", "Magic DRC l/c", {"tool": "magic", "lib": "l", "cell": "c"})
    steps = [(["sh", "-c", "echo x > a.txt"], env), (["sh", "-c", "echo 1 > result.tsv"], env)]
    magicflow._run_seq(jobs, done, steps, lambda j: {"summary": {"total": 0}}, "result.tsv")
    st = done.status()
    assert st["state"] == "done" and st["server"]["jobs"] == jobs.token and st["process"]["pid"]
    assert set(st["files"]) >= {"a.txt", "result.tsv", "log.txt"} and st["stamps"]["result.tsv"][0] == 2
    busy = verify._new_job(jobs, prj, "lvs", "Magic+Netgen LVS l/c", {"tool": "netgen", "lib": "l", "cell": "c"})
    t = threading.Thread(target=magicflow._run_seq, args=(jobs, busy, [(["sh", "-c", "sleep 60 & sleep 60; wait"], env),
                                                                 (["sh", "-c", "echo no > lvs.out"], env)],
                                                          lambda j: {}, "lvs.out"))
    t.start()
    rec = started(busy)
    assert jobs.cancel(busy) and until(lambda: not alive(rec), 15)
    t.join(15)
    assert busy.status()["state"] == "cancelled" and not (busy.path / "lvs.out").exists()
    left = verify._new_job(jobs, prj, "pex", "Parasitic extraction (c) l/c", {"tool": "magic", "lib": "l", "cell": "c"})
    left.set(state="running", started=time.time(), created=time.time() - 1)
    jobs.shutdown()                                    # the server goes; its check was not finished (Jobs.close)
    verify.LIVE.pop(left.id, None)
    later = sim.Jobs(root=root)
    st = later.status(sim.Job(left.path))
    assert st["state"] == "interrupted" and st["error"] == sim.INTERRUPTED["other"]
    later.shutdown()


def test_cancel_stops_what_a_run_no_one_works_on_left_running(root, tmp_path, monkeypatch):
    """What resume or a read missed (a run read by no one yet) is stopped by a cancel as well."""
    monkeypatch.setattr(sim.Jobs, "_stop_left", lambda self, st: False)
    d = root / "p" / "runs" / "20261103-120000-drc0"
    d.mkdir(parents=True)
    rec = _orphan(tmp_path)
    (d / "status.json").write_text(json.dumps({"id": d.name, "kind": "drc", "state": "running",
                                               "created": time.time() - 1, "server": _gone_server(), "process": rec}))
    try:
        jobs = sim.Jobs(root=root)
        assert jobs.status(sim.Job(d))["state"] == "interrupted" and alive(rec)
        monkeypatch.undo()
        assert jobs.cancel(sim.Job(d)) and until(lambda: not alive(rec), 15)
        jobs.shutdown()
    finally:
        _end(rec)


def test_a_hangup_stops_the_server_as_sigterm_does(tmp_path):
    """The server's terminal or SSH session closes (SIGHUP): the server stops as on SIGTERM (exit 0, its simulations
    handed over), so that what runs in sessions of their own (a DRC's tools, ngspice) is ended or handed over, not
    left as no one's."""
    _need()
    from undine.store.project import ProjectStore

    root, log = tmp_path / "root", tmp_path / "serve.log"
    ProjectStore(root, {}).create("p")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    a = serve(root, port, log)
    rec = None
    try:
        jid = http(port, "POST", "/api/p/p/sim", {"lib": "l", "cell": "tb", "netlist": ladder(200)})["id"]
        job = sim.Job(root / "p" / "runs" / jid)
        rec = started(job)
        a.send_signal(signal.SIGHUP)
        assert a.wait(30) == 0 and "left to the next server on this root: 1 running" in log.read_text()
        assert alive(rec) and job.status()["handed_over"]
    finally:
        if a.poll() is None:
            a.kill()
            a.wait()
        if rec is not None:
            sim.stop(sim.Foreign(rec["pid"], rec["start"]))


def test_no_run_starts_once_the_server_is_stopping(root, monkeypatch):
    """A run submitted while the server stops (an assistant's turn goes on in its thread) is refused, also between the
    jobs --stop-jobs stops and the hand over: it would run on with no one's --stop-jobs, or be no server's."""
    _need()
    jobs = sim.Jobs(limit=1, root=root)
    running = jobs.submit(root / "p", "long", ladder(200), None)
    rec = started(running)
    cancel, refused = jobs.cancel, []

    def cancelling(job, why=None):
        try:
            jobs.submit(root / "p", "late", ladder(200), None)
        except RuntimeError as e:
            refused.append(str(e))
        return cancel(job, why)
    monkeypatch.setattr(jobs, "cancel", cancelling)
    jobs.shutdown(stop_jobs=True)
    assert refused and "stopping" in refused[0] and not alive(rec)
    with pytest.raises(RuntimeError, match="stopping"):
        jobs.submit(root / "p", "after", ladder(1), None)
    assert [d.name for d in (root / "p" / "runs").iterdir()] == [running.id]
    assert sim.Jobs(root=root).resumed == {"adopted": [], "queued": []}


def test_a_second_server_tells_the_runs_no_one_works_on(root):
    """A server that does not hold the root judges without taking anything up: a run whose server has gone and whose
    ngspice is gone, and a check whose server has gone (or an earlier one's), are interrupted, so that its changeset
    can be closed; one the holder may still take up (queued) is not."""
    gone = _gone_server()
    dead = {k: gone[k] for k in ("pid", "start", "boot")}       # its ngspice: gone too
    t = time.time() - 60
    runs = {}
    for name, st in {
        "sim": {"kind": "sim", "state": "running", "process": {**dead, "cmd": ["ngspice"], "cwd": "x"}, "server": gone},
        "drc": {"kind": "drc", "state": "running", "server": gone},
        "old": {"kind": "lvs", "state": "running"},
        "queued": {"kind": "sim", "state": "queued", "server": gone},
    }.items():
        d = root / "p" / "runs" / f"20261103-120000-{name}"
        d.mkdir(parents=True)
        (d / "status.json").write_text(json.dumps({"id": d.name, "created": t, **st}))
        runs[name] = sim.Job(d)
    a = sim.Jobs(root=root)
    b = sim.Jobs(root=root)
    assert a.owner and b.others
    for name in ("sim", "drc", "old"):
        assert b.status(runs[name])["state"] == "interrupted" and not b.working(runs[name]), name
        assert a.status(runs[name])["state"] == "interrupted", name
    assert b.status(runs["queued"])["state"] == "queued" and b.working(runs["queued"])
    assert b.find(root / "p", "-", ("running",)) is None
    b.shutdown()
    a.shutdown()


def test_a_cancelled_adopted_job_is_killed_at_the_exit(root, monkeypatch):
    """A cancel's stop runs in a thread that ends with the process: an adopted job's group that ignores SIGTERM is
    killed at the exit (_stop_all), as a started job's is, rather than left running as a cancelled job."""
    import weakref
    monkeypatch.setattr(sim, "_ALL", weakref.WeakSet())
    p = subprocess.Popen(["sh", "-c", "trap '' TERM; sleep 60 & wait"], start_new_session=True)
    try:
        assert until(lambda: sim._proc_stat(p.pid) is not None)
        jobs = sim.Jobs(root=root)
        d = root / "p" / "runs" / "20261103-120000-sim0"
        d.mkdir(parents=True)
        job = sim.Job(d)
        job.set(id=d.name, kind="sim", state="running", created=time.time())
        job.proc = sim.Foreign(p.pid, sim._proc_stat(p.pid)[2])
        jobs.live[job.id] = job
        jobs.active.add(job)
        assert jobs.cancel(job) and sim.alive(job.proc)
        time.sleep(0.3)
        assert sim.alive(job.proc)                         # SIGTERM is ignored: the stop waits STOP_GRACE_S
        jobs.closed = True                                 # the server's exit
        sim._stop_all()
        assert until(lambda: not sim.alive(job.proc), sim.STOP_GRACE_S - 1)
    finally:
        if p.poll() is None:
            os.killpg(p.pid, signal.SIGKILL)
        p.wait()


def test_a_wait_answers_at_once_when_the_server_is_asked_to_stop():
    """A restart does not wait for the calls that wait (wait_for, run_simulation ...): they answer with what they
    have, so no call is cut, and the job goes on."""
    _need()
    root, srv, _ = tc.setup()
    job = srv.jobs.submit(srv.project("p").path, "long", ladder(200), None, meta={"kind": "sim"})
    started(job)
    me = T.ToolCtx(srv, "p", actor="agent:a")
    out = {}

    def wait():
        out.update(json.loads(T.call(me, "wait_for", {"events": [f"job:{job.id}"], "timeout_s": 600})[0]))
    t = threading.Thread(target=wait)
    t.start()
    time.sleep(1)
    t0 = time.time()
    srv.stopping.set()                                 # `undine serve` asked to stop
    t.join(10)
    assert not t.is_alive() and time.time() - t0 < 5
    assert out["stopping"] and out["jobs"] == {job.id: "running"} and "the next server" in out["note"]
    srv.stop_jobs = True                               # `undine serve --stop-jobs`: no restart takes them up
    out = json.loads(T.call(me, "wait_for", {"events": [f"job:{job.id}"], "timeout_s": 600})[0])
    assert out["stopping"] and "--stop-jobs" in out["note"] and "next server" not in out["note"]
    srv.jobs.shutdown(stop_jobs=True)
    shutil.rmtree(root)



def test_run_checks_and_extract_parasitics_answer_at_once_when_the_server_is_asked_to_stop(monkeypatch):
    """The checks' waits answer at once too, and say what becomes of their jobs: a DRC, LVS or extraction ends with the
    server (only simulations are taken up), so they are to run again once it is back. Called while it stops, they
    start nothing."""
    root, srv, cs = tc.setup()
    meta = cs.create("p", "Checks", "checks while stopping", "agent:a")
    me = T.ToolCtx(srv, "p", actor="agent:a")
    me.changeset = meta["id"]
    running = {"state": "running", "id": "20260101-000000-abcd"}
    launched = []

    class Busy:                                         # a check job that does not end while the call waits
        id = running["id"]

        def status(self):
            return dict(running)
    monkeypatch.setattr(srv, "start_extraction", lambda *a, **k: launched.append("pex") or Busy())
    monkeypatch.setattr(type(me.cs), "verify", lambda self, *a, **k: launched.append("checks"))
    monkeypatch.setattr(type(me.cs), "verify_status", lambda self, *a, **k: [
        {"side": "work", "kind": "drc", "checker": "magic", "lib": "l", "cell": "top", "state": "running", "job": Busy.id}])
    out = {}

    def call(name, args):
        out[name] = json.loads(T.call(me, name, args)[0])
    calls = [threading.Thread(target=call, args=a) for a in (("extract_parasitics", {"lib": "l", "cell": "top"}),
                                                             ("run_checks", {}))]
    for t in calls:
        t.start()
    assert until(lambda: len(launched) == 2, 10)
    time.sleep(1)
    t0 = time.time()
    srv.stopping.set()                                  # asked to stop while they wait
    for t in calls:
        t.join(10)
    assert time.time() - t0 < 5
    pex, checks = out["extract_parasitics"], out["run_checks"]
    assert pex["stopping"] and pex["state"] == "running" and "extract_parasitics again" in pex["note"]
    assert checks["stopping"] and checks["checks"][0]["state"] == "running" and "run_checks again" in checks["note"]
    launched.clear()                                    # called while it stops: nothing starts
    pex = json.loads(T.call(me, "extract_parasitics", {"lib": "l", "cell": "top"})[0])
    checks = json.loads(T.call(me, "run_checks", {})[0])
    assert not launched and pex["stopping"] and checks["stopping"] and checks["checks"] == []
    assert "no check starts" in pex["note"] and "run_checks again" in checks["note"]
    shutil.rmtree(root)


def test_run_checks_does_not_wait_on_a_cancelled_or_interrupted_check(monkeypatch):
    """A check a person cancelled, or one a restart interrupted, has ended: run_checks answers at once, and says that
    it runs the check again."""
    root, srv, cs = tc.setup()
    meta = cs.create("p", "Checks", "ended checks", "agent:a")
    me = T.ToolCtx(srv, "p", actor="agent:a")
    me.changeset = meta["id"]
    monkeypatch.setattr(type(me.cs), "verify", lambda self, *a, **k: None)
    monkeypatch.setattr(type(me.cs), "verify_status", lambda self, *a, **k: [
        {"side": "work", "kind": "drc", "checker": "magic", "lib": "l", "cell": "top", "state": "cancelled", "job": "j1"},
        {"side": "work", "kind": "lvs", "checker": "klayout", "lib": "l", "cell": "top", "state": "interrupted",
         "job": "j2", "error": sim.INTERRUPTED["other"]},
        {"side": "base", "kind": "lvs", "checker": "klayout", "lib": "l", "cell": "top", "state": "not_run"}])
    t0 = time.time()
    out = json.loads(T.call(me, "run_checks", {})[0])
    assert time.time() - t0 < 5 and not out.get("stopping")
    rows = {r["job"]: r for r in out["checks"] if r.get("job")}
    assert rows["j1"]["next"] == rows["j2"]["next"] == "run_checks runs it again"
    shutil.rmtree(root)


# ------------------------------------------------------------------ checks: a job of another kind, run by this server


def _check(jobs, root: Path, *steps: str, key: str = "k-check"):
    """A check job (verify._new_job, as a DRC) whose steps (shell lines) run in magicflow._run_seq; result.tsv is its
    result file."""
    from undine.checks import magicflow, verify

    job = verify._new_job(jobs, root / "p", "drc", "check", {"key": key})
    t = threading.Thread(target=magicflow._run_seq, args=(jobs, job, [(["sh", "-c", st], dict(os.environ)) for st in steps],
                                                          lambda j: {"summary": {}}, "result.tsv"))
    t.start()
    return job, t


def test_a_check_that_ended_is_let_go_of_and_a_cancel_stops_what_it_left(root):
    """A check's runner lets go of its job when it ends (no longer live): a cancel of what its tool started and left
    running stops that, and a closure is told so (cancel_job), rather than "nothing to stop"."""
    from undine.checks import verify

    jobs = sim.Jobs(root=root)
    job, t = _check(jobs, root, "(sleep 30 &); echo 1 > result.tsv")
    t.join(30)
    st = job.status()
    assert st["state"] == "done" and job.id not in jobs.live and job.id not in verify.LIVE
    rest = sim.left(st)
    assert rest is not None
    assert jobs.cancel(job) and until(lambda: not sim.alive(rest), 15)
    assert not jobs.cancel(job)                                  # nothing left
    jobs.shutdown()


def test_a_live_check_that_has_just_ended_has_what_it_left_stopped(root):
    """The moment between a check's end and its runner letting go of it: a cancel still stops what it left."""
    jobs = sim.Jobs(root=root)
    d = root / "p" / "runs" / "20261103-120000-drc0"
    d.mkdir(parents=True)
    job = sim.Job(d)
    job.set(id=d.name, kind="drc", state="running", created=time.time())
    jobs.track(job)
    proc = jobs.step(job, ["sh", "-c", "(sleep 30 &); true"], cwd=d, stdout=subprocess.DEVNULL)
    proc.wait()
    job.set(state="done")
    assert sim.alive(proc)                                       # the sleep, in its group
    assert jobs.cancel(job) and until(lambda: not sim.alive(proc), 15)
    jobs.shutdown()


def test_a_check_cancelled_while_a_step_runs_stays_cancelled_and_starts_no_more(root):
    jobs = sim.Jobs(root=root)
    job, t = _check(jobs, root, "sleep 30", "touch second; echo 1 > result.tsv")
    started(job)
    assert jobs.cancel(job)
    t.join(15)
    assert not t.is_alive()
    st = job.status()
    assert st["state"] == "cancelled" and not (job.path / "second").exists() and job.id not in jobs.live
    jobs.shutdown()


def test_a_check_cancelled_between_two_steps_starts_no_more(root, monkeypatch):
    """Magic's extraction has ended and Netgen has not started: no process runs, yet the job is cancelled (not "running
    but not in this server's hands"), and Netgen does not start."""
    jobs = sim.Jobs(root=root)
    step, seen = jobs.step, []

    def between(job, argv, **kw):
        if seen:                                                 # before the second step
            seen.append(jobs.cancel(job))
        seen.append(None)
        return step(job, argv, **kw)
    monkeypatch.setattr(jobs, "step", between)
    job, t = _check(jobs, root, "true", "touch second; echo 1 > result.tsv")
    t.join(15)
    assert seen == [None, True, None]
    assert job.status()["state"] == "cancelled" and not (job.path / "second").exists()
    jobs.shutdown()


def test_a_cancel_as_a_step_starts_is_not_written_over(root, monkeypatch):
    """The step's process is recorded in the status while a cancel writes the status: under one lock, so the cancel
    stays (it once came back as running, and then as "failed: exited with -15")."""
    jobs = sim.Jobs(root=root)
    real = sim.identity

    def slow(*a, **kw):
        out = real(*a, **kw)
        threading.Thread(target=jobs.cancel, args=(job,)).start()
        time.sleep(0.3)                                          # the cancel waits for the lock meanwhile
        return out
    monkeypatch.setattr(sim, "identity", slow)
    job, t = _check(jobs, root, "sleep 30", "echo 1 > result.tsv")
    t.join(20)
    assert not t.is_alive() and job.status()["state"] == "cancelled"
    jobs.shutdown()


def test_a_stopping_server_ends_its_checks_and_starts_none(root):
    """Asked to stop (close: SIGTERM, before it stops serving), a server ends its checks at once, as interrupted, and
    starts none; the next server, which may already answer, does not take one for a running job (find: a check is not
    reused only to be killed with the last server)."""
    from undine.checks import verify

    jobs = sim.Jobs(root=root)
    job, t = _check(jobs, root, "sleep 30", "echo 1 > result.tsv", key="k1")
    rec = started(job)
    nxt = sim.Jobs(root=root)                                    # the next one, while this one still serves
    assert nxt.find(root / "p", "k1", ("running", "queued")) is not None
    jobs.close()
    t.join(15)
    st = job.status()
    assert st["state"] == "interrupted" and st["error"] == sim.INTERRUPTED["other"] and not t.is_alive()
    assert until(lambda: not alive(rec), 15)
    assert nxt.find(root / "p", "k1", ("running", "queued", "done")) is None
    before = sorted(d.name for d in (root / "p" / "runs").iterdir())
    with pytest.raises(RuntimeError, match="stopping"):
        verify._new_job(jobs, root / "p", "drc", "late", {})
    assert sorted(d.name for d in (root / "p" / "runs").iterdir()) == before
    jobs.shutdown()
    nxt.shutdown()


def test_a_wait_for_a_check_says_it_ends_with_the_server():
    """wait_for on a check when the server is asked to stop: it ends with the server (no later server takes it up),
    not "it goes on"; once ended, it says why and what runs it again."""
    root, srv, _ = tc.setup()
    job, t = _check(srv.jobs, srv.project("p").path.parent, "sleep 30", "echo 1 > result.tsv")
    started(job)
    me = T.ToolCtx(srv, "p", actor="agent:a")
    out = {}

    def wait():
        out.update(json.loads(T.call(me, "wait_for", {"events": [f"job:{job.id}"], "timeout_s": 600})[0]))
    w = threading.Thread(target=wait)
    w.start()
    time.sleep(1)
    srv.stopping.set()                                 # `undine serve` asked to stop: then Jobs.close, in a thread
    w.join(10)
    srv.jobs.close()
    assert out["stopping"] and out["ended"] == [job.id] and "interrupted" in out["note"]
    assert "goes on" not in out["note"] and "takes them up (a restart loses" not in out["note"]
    t.join(15)
    out = json.loads(T.call(me, "wait_for", {"events": [f"job:{job.id}"]})[0])
    assert out["state"] == "interrupted" and out["note"] == sim.INTERRUPTED["other"] and "run_checks" in out["next"]
    srv.jobs.shutdown()
    shutil.rmtree(root)


def serve(root: Path, port: int, log: Path, *extra: str) -> subprocess.Popen:
    """`undine serve` on `root`, once it answers."""
    p = subprocess.Popen([sys.executable, "-m", "undine", "--root", str(root), "serve", "--port", str(port), *extra],
                         cwd=SERVER, stdout=log.open("a"), stderr=subprocess.STDOUT,
                         env={**os.environ, "PYTHONPATH": str(SERVER), "PYTHONUNBUFFERED": "1",
                              "PATH": os.path.dirname(tools.find("ngspice")) + os.pathsep + os.environ["PATH"]})
    assert until(lambda: http(port, "GET", "/api/health") is not None or p.poll() is not None, 120), log.read_text()
    assert p.poll() is None, log.read_text()
    return p


def http(port: int, method: str, path: str, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, data=json.dumps(body).encode()
                                 if body is not None else None, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except OSError:
        return None


def test_undine_serve_hands_its_simulations_over(tmp_path):
    """The real server: stopped with SIGTERM it ends at once (exit 0), its ngspice running and handed over, and the
    next one finishes the run (done, its measurements); one killed (SIGKILL) leaves its run to the next one as well,
    and one started with --stop-jobs stops what it runs when it stops."""
    _need()
    from undine.store.project import ProjectStore

    root, log = tmp_path / "root", tmp_path / "serve.log"
    ProjectStore(root, {}).create("p")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    a = serve(root, port, log)
    jid = http(port, "POST", "/api/p/p/sim", {"lib": "l", "cell": "tb", "netlist": ladder(40)})["id"]
    job = sim.Job(root / "p" / "runs" / jid)
    rec = started(job)
    a.send_signal(signal.SIGTERM)
    assert a.wait(30) == 0 and "left to the next server on this root: 1 running" in log.read_text()
    assert alive(rec) and job.status()["handed_over"] and job.status()["state"] == "running"
    b = serve(root, port, log)
    try:
        assert "taken up from the last server: 1 running" in log.read_text()
        assert until(lambda: http(port, "GET", f"/api/p/p/jobs/{jid}")["state"] != "running", 180), job.status()
        st = http(port, "GET", f"/api/p/p/jobs/{jid}")
        assert st["state"] == "done" and st["adopted"] and st["process"] == rec, st
        assert simresults.measurements(job.log(5000))["vavg"] == pytest.approx(0.51, abs=0.01)
        jid = http(port, "POST", "/api/p/p/sim", {"lib": "l", "cell": "tb", "netlist": ladder(200)})["id"]
        job = sim.Job(root / "p" / "runs" / jid)
        rec = started(job)
    finally:
        b.kill()
        b.wait()
    c = serve(root, port, log, "--stop-jobs")
    try:
        assert alive(rec) and http(port, "GET", f"/api/p/p/jobs/{jid}")["state"] == "running"
        c.send_signal(signal.SIGTERM)
        assert c.wait(30) == 0
    finally:
        c.kill()
        c.wait()
    assert not alive(rec) and job.status()["state"] == "cancelled" and "--stop-jobs" in job.status()["error"]


# ------------------------------------------------------------------ agents


def restarted(root: Path, srv: Server, forget: bool = False):
    """The server goes and a new one starts on its root (`forget`: without the sessions it kept)."""
    srv.jobs.shutdown()
    if forget:
        (root / mcp.SESSIONS).unlink()
    srv2 = Server(root, {}, [root])
    srv2.mcp_enabled = True
    return srv2, transport(TestClient(create_app(srv2, None)))


def test_an_mcp_session_goes_on_after_a_restart_with_its_changeset(tmp_path):
    root, srv, http = client_setup()
    url = ["--url", "http://undine.test", "--project", "p"]
    code, out, err = cli(["call", "open_changeset", "title=Raise R1", "reason=less current", *url], http, tmp_path)
    assert code == 0, err
    cid = json.loads(out)["id"]
    kept = json.loads((tmp_path / "sessions.json").read_text())
    srv, http = restarted(root, srv)
    code, out, err = cli(["call", "set_instance_params", "lib=l", "cell=top", "instance=R1", 'params:={"value": "22k"}',
                          "reason=less current", *url], http, tmp_path)
    assert code == 0, err
    assert tc.value(srv, f"p~{cid}") == "22k" and tc.value(srv) == "1k"
    assert json.loads((tmp_path / "sessions.json").read_text()) == kept             # the same session, resumed
    sid = next(iter(kept.values()))["session"]
    assert srv.leases.get(cid).owner == f"mcp:{sid}"                                  # the same lease owner
    srv.leases.stop(cid)                                                              # the person stops the agent
    srv, http = restarted(root, srv)
    code, out, err = cli(["call", "set_instance_params", "lib=l", "cell=top", "instance=R1", 'params:={"value": "1M"}',
                          "reason=x", *url], http, tmp_path)
    assert code == 1 and "was stopped" in err                                         # stopped stays stopped
    assert tc.value(srv, f"p~{cid}") == "22k"
    shutil.rmtree(root)


def test_a_shell_call_made_while_the_server_is_down_waits_for_it(tmp_path, monkeypatch):
    """`undine call` is a new process each time: one made while the server restarts (a session kept with it) waits for
    it to come back instead of saying that Undine is not running, and goes on in its session; one that never had a
    session with it says so at once."""
    monkeypatch.setattr(mcp_local, "RESTART_POLL_S", 0.01)
    root, srv, http = client_setup()
    url = ["--url", "http://undine.test", "--project", "p"]
    code, out, err = cli(["call", "open_changeset", "title=Raise R1", "reason=less current", *url], http, tmp_path)
    cid = json.loads(out)["id"]
    box = {"down": 20}

    def restarting(method, path, body=None, headers=None):
        if box["down"]:
            box["down"] -= 1
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        return http(method, path, body, headers)
    code, out, err = cli(["call", "set_instance_params", "lib=l", "cell=top", "instance=R1", 'params:={"value": "22k"}',
                          "reason=less current", *url], restarting, tmp_path)
    assert code == 0 and box["down"] == 0, err
    assert tc.value(srv, f"p~{cid}") == "22k"
    box["down"] = 10 ** 6
    t0 = time.time()
    code, out, err = cli(["call", "list_changesets", "--session", "new", *url], restarting, tmp_path)
    assert code == 2 and "not running" in err and time.time() - t0 < 5
    shutil.rmtree(root)


def test_the_shell_continues_its_changeset_when_the_server_forgot_its_session(tmp_path, capsys):
    root, srv, http = client_setup()
    url = ["--url", "http://undine.test", "--project", "p"]
    code, out, err = cli(["call", "open_changeset", "title=Raise R1", "reason=less current", *url], http, tmp_path)
    cid = json.loads(out)["id"]
    before = next(iter(json.loads((tmp_path / "sessions.json").read_text()).values()))
    assert before["changesets"] == {"p|": cid}
    srv, http = restarted(root, srv, forget=True)
    code, out, err = cli(["call", "set_instance_params", "lib=l", "cell=top", "instance=R1", 'params:={"value": "22k"}',
                          "reason=less current", *url], http, tmp_path)
    assert code == 0, err
    assert tc.value(srv, f"p~{cid}") == "22k" and f"continued the changeset {cid}" in capsys.readouterr().err
    after = next(iter(json.loads((tmp_path / "sessions.json").read_text()).values()))
    assert after["session"] != before["session"] and after["changesets"] == {"p|": cid}
    shutil.rmtree(root)


def test_a_write_sent_while_the_server_restarts_acts_once(monkeypatch):
    """The server acts and goes before its answer is out; the call is sent again (its request id) while it refuses
    connections and while a proxy in front says 503, until it is back: the answer is the first call's."""
    monkeypatch.setattr(cl, "RETRIES", (0.01, 0.01))
    monkeypatch.setattr(mcp_local, "RESTART_POLL_S", 0.01)
    root, srv, http = client_setup()
    box = {"http": http, "phase": "up", "refused": 0}

    def flaky(method, path, body=None, headers=None):
        if box["phase"] == "up":
            out = box["http"](method, path, body, headers)
            if isinstance(body, dict) and (body.get("params") or {}).get("name") == "open_changeset" and box["http"] is http:
                box["phase"] = "down"
                raise ConnectionResetError("the server went away")                  # it acted; the answer is lost
            return out
        if box["phase"] == "down":
            box["refused"] += 1
            if box["refused"] == 5:
                box["srv"], box["http"] = restarted(root, srv)
                box["phase"] = "proxy"
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
        box["refused"] += 1
        if box["refused"] < 8:
            return 503, {"detail": "Service Unavailable"}, {}
        box["phase"] = "up"
        return box["http"](method, path, body, headers)
    u = Undine("p", "http://undine.test", http=flaky)
    u.list_changesets()
    sid = u.session
    cid = u.open_changeset(title="A", reason="r")["id"]
    assert u.last_replayed and box["refused"] == 8 and u.session == sid
    assert [c["title"] for c in tc.Changesets(box["srv"]).list("p")].count("A") == 1
    assert u.get_changeset()["id"] == cid                                             # still this session's
    shutil.rmtree(root)


def test_a_server_that_does_not_come_back_is_reported(monkeypatch):
    monkeypatch.setattr(mcp_local, "RESTART_WAIT_S", 0.3)
    monkeypatch.setattr(mcp_local, "RESTART_POLL_S", 0.05)
    root, srv, http = client_setup()
    u = Undine("p", "http://undine.test", http=http)
    u.list_changesets()

    def refused(method, path, body=None, headers=None):
        raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    u._l.http = refused
    t0 = time.time()
    with pytest.raises(cl.UndineError, match="not back after 0.3 s"):
        u.list_changesets()
    assert 0.25 <= time.time() - t0 < 5
    shutil.rmtree(root)


def test_the_stdio_relay_waits_while_the_server_restarts(monkeypatch):
    """mcp-stdio (a desktop client's connection) sends a message again while the server refuses connections or a proxy
    in front says 503, and goes on in the session it had; a tool call a proxy answered 502 for (it may have reached the
    server) is sent again only when it carries a request id."""
    monkeypatch.setattr(mcp_local, "RESTART_POLL_S", 0.01)
    root, srv, la, _ = mcp_setup()
    up, box = la.http, {"away": 0}

    def restarting(method, path, body=None, headers=None):
        if box["away"] > 0:
            box["away"] -= 1
            if box["away"] % 2:
                raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
            return 503, {"detail": "Service Unavailable"}, {}
        return up(method, path, body, headers)
    la.http = restarting
    la.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    sid = la.session
    box["away"] = 6
    cid = json.loads(la.handle(call(2, "open_changeset", title="A", reason="r"))["result"]["content"][0]["text"])["id"]
    assert box["away"] == 0 and la.session == sid
    box["away"] = 4
    out = la.handle(call(4, "set_instance_params", lib="l", cell="top", instance="R1", params={"value": "3k"}, reason="x"))
    assert not out["result"]["isError"] and tc.value(srv, f"p~{cid}") == "3k"
    la.http = lambda *a: (502, {"detail": "Bad Gateway"}, {})
    out = la.handle(call(5, "set_instance_params", lib="l", cell="top", instance="R1", params={"value": "4k"}, reason="x"))
    assert "502" in out["error"]["message"]                            # not sent again: it may have acted
    shutil.rmtree(root)


def test_agents_sharing_a_shell_session_keep_each_others_changesets(tmp_path):
    """Two agents on one --session (agent=NAME each): one opens its changeset while the other's call is still open,
    and the other's end does not drop it from the session file; when the server forgot the session, both go on with
    their changesets."""
    root, srv, http = client_setup()
    url = ["--url", "http://undine.test", "--project", "p"]
    code, out, err = cli(["call", "open_changeset", "title=X", "reason=r", "agent=x", *url], http, tmp_path)
    cx, box = json.loads(out)["id"], {}

    def meanwhile(method, path, body=None, headers=None):
        if "y" not in box and isinstance(body, dict) and (body.get("params") or {}).get("name") == "get_changeset":
            box["y"] = cli(["call", "open_changeset", "title=Y", "reason=r", "agent=y", *url], http, tmp_path)
        return http(method, path, body, headers)
    code, out, err = cli(["call", "get_changeset", "agent=x", *url], meanwhile, tmp_path)
    assert code == 0 and box["y"][0] == 0, (err, box)
    cy = json.loads(box["y"][1])["id"]
    kept = next(iter(json.loads((tmp_path / "sessions.json").read_text()).values()))
    assert kept["changesets"] == {"p|x": cx, "p|y": cy}
    srv, http = restarted(root, srv, forget=True)
    for agent, cid, v in (("x", cx, "2k"), ("y", cy, "3k")):
        code, out, err = cli(["call", "set_instance_params", "lib=l", "cell=top", "instance=R1",
                              f'params:={{"value": "{v}"}}', "reason=r", f"agent={agent}", *url], http, tmp_path)
        assert code == 0, err
        assert tc.value(srv, f"p~{cid}") == v
    shutil.rmtree(root)


def test_the_next_server_has_what_the_last_one_changed_while_it_stopped(tmp_path):
    """The next server starts while the last one still finishes its calls (restart.sh): a changeset the last one opened
    for a session meanwhile is the session's in the next one, a session the next one started meanwhile is kept once it
    holds the root, and a third server resumes both."""
    root, a, http_a = client_setup()
    url = ["--url", "http://undine.test", "--project", "p"]
    code, out, err = cli(["call", "open_changeset", "title=one", "reason=r", *url], http_a, tmp_path)
    b = Server(root, {}, [root])
    b.mcp_enabled = True
    http_b = transport(TestClient(create_app(b, None)))
    assert a.jobs.keeper and not b.jobs.keeper
    code, out, err = cli(["call", "open_changeset", "title=two", "reason=r", *url], http_a, tmp_path)     # the last one's
    c2 = json.loads(out)["id"]
    code, out, err = cli(["call", "get_changeset", *url], http_b, tmp_path)
    assert code == 0 and json.loads(out)["id"] == c2, (out, err)
    code, out, err = cli(["call", "open_changeset", "title=three", "reason=r", "--session", "other", *url], http_b,
                         tmp_path)
    c3 = json.loads(out)["id"]
    kept = json.loads((root / mcp.SESSIONS).read_text())["sessions"]
    assert c3 not in json.dumps(kept)                                  # not b's to write yet
    a.jobs.shutdown()
    assert until(lambda: c3 in (root / mcp.SESSIONS).read_text(), 10)
    srv3, http3 = restarted(root, b)
    sessions = json.loads((tmp_path / "sessions.json").read_text())
    for name, cid in (("default", c2), ("other", c3)):
        code, out, err = cli(["call", "get_changeset", "--session", name, *url], http3, tmp_path)
        assert code == 0 and json.loads(out)["id"] == cid, (name, out, err)
    assert json.loads((tmp_path / "sessions.json").read_text()) == sessions          # resumed, not new sessions
    srv3.jobs.shutdown()
    shutil.rmtree(root)


def test_the_first_restart_from_an_older_undine_keeps_the_sessions_of_running_jobs(tmp_path):
    """An Undine before this version kept no sessions: the sessions whose simulations still run in a changeset are
    taken from those jobs, so their agents' next call (a kept session id of the older format) goes on in that
    changeset; a `no job` answer of another session names the changeset that has the job."""
    root, srv, http = client_setup()
    url = ["--url", "http://undine.test", "--project", "p"]
    code, out, err = cli(["call", "open_changeset", "title=long run", "reason=r", "agent=top", *url], http, tmp_path)
    cid = json.loads(out)["id"]
    key, kept = next(iter(json.loads((tmp_path / "sessions.json").read_text()).items()))
    (tmp_path / "sessions.json").write_text(json.dumps({key: kept["session"]}))     # as an older client kept it
    run = srv.project(f"p~{cid}").path / "runs" / "20261103-120000-ab12"
    run.mkdir(parents=True)
    (run / "status.json").write_text(json.dumps({"id": run.name, "kind": "sim", "state": "running",
                                                 "owner": f"mcp:{kept['session']}:top", "created": time.time() - 60}))
    srv, http = restarted(root, srv, forget=True)
    code, out, err = cli(["call", "get_changeset", "agent=top", *url], http, tmp_path)
    assert code == 0 and json.loads(out)["id"] == cid, (out, err)
    assert next(iter(json.loads((tmp_path / "sessions.json").read_text()).values()))["session"] == kept["session"]
    code, out, err = cli(["call", "wait_for", 'events:=["job:20261103-120000-ab12"]', "--session", "fresh", *url],
                         http, tmp_path)
    assert code == 1 and f"open_changeset id={cid}" in err, err
    srv.jobs.shutdown()
    shutil.rmtree(root)


def test_over_ssh_a_call_waits_while_the_server_restarts(monkeypatch):
    """Through an SSH forward the server's refusal is a connection accepted and closed: a call that can be sent twice
    waits while the server does not answer through the forward, and acts once."""
    from test_client import fake_tunnel
    monkeypatch.setattr(mcp_local, "RESTART_POLL_S", 0.01)
    monkeypatch.setattr(cl, "RETRIES", (0.01,))
    root, srv, http = client_setup()
    t = fake_tunnel(srv)
    box = {"cut": 0}

    def forward(method, path, body=None, headers=None):
        if box["cut"]:
            box["cut"] -= 1
            raise ConnectionResetError(104, "Connection reset by peer")
        return http(method, path, body, headers)
    u = Undine("p", "http://127.0.0.1:8700", http=forward, ssh=t)
    cid = u.open_changeset(title="A", reason="r")["id"]
    box["cut"] = 30
    u.set_instance_params(lib="l", cell="top", instance="R1", params={"value": "2k"}, reason="x")
    assert box["cut"] == 0 and len(t.started) == 1 and tc.value(srv, f"p~{cid}") == "2k"
    u.close()
    shutil.rmtree(root)


def test_a_restart_wait_that_is_not_a_number_is_said_and_the_default_taken(monkeypatch, capsys):
    monkeypatch.setenv("UNDINE_RESTART_WAIT_S", "2m")
    assert mcp_local._wait_s() == 120.0 and "not '2m'" in capsys.readouterr().err
    monkeypatch.setenv("UNDINE_RESTART_WAIT_S", "5")
    assert mcp_local._wait_s() == 5.0
