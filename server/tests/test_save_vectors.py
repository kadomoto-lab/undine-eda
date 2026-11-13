"""What a simulation stores: a transient keeps the testbench's nets, what its .meas / .control lines name and the
signals asked for, not the nodes inside its cells (Server.start_sim, sim.save_cards); save_all keeps every node."""
import json
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from undine.agents import agent_tools as T  # noqa: E402
from undine.simulation import sim  # noqa: E402
from test_simresults import _need, _project  # noqa: E402
from test_substitute import _devices, _sch, _sym  # noqa: E402

TRAN = {"type": "tran", "step": "10n", "stop": "1u"}


def _setup():
    """div3: in -> m -> out -> vss (m inside the cell); tb places it as x1 driven by V1."""
    _devices()
    root, srv = _project()
    lib = srv.project("p").library("l").path
    (lib / "div3.sym").write_text(_sym(["in", "out", "vss"]))
    (lib / "div3.sch").write_text(_sch([("devices/res.sym", {"name": "R1", "value": "1k"}, {"P": "in", "M": "m"}),
                                        ("devices/res.sym", {"name": "R2", "value": "1k"}, {"P": "m", "M": "out"}),
                                        ("devices/res.sym", {"name": "R3", "value": "1k"}, {"P": "out", "M": "vss"})],
                                       ("in", "out", "vss")))
    (lib / "tb.sch").write_text(_sch([("devices/vsource.sym", {"name": "V1", "value": "1"}, {"p": "in", "m": "0"}),
                                      ("div3.sym", {"name": "x1"}, {"in": "in", "out": "o", "vss": "0"})]))
    return root, srv, T.ToolCtx(srv, "p", actor="agent:t")


def _run(ctx, **args) -> dict:
    res, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "tb", **args})
    assert not err, res
    return json.loads(res)


def _vectors(srv, job: str) -> list[str]:
    return [n.lower() for n, _ in sim.read_raw(srv.jobs.get(srv.project("p").path, job).path / "out.raw")["vars"]]


def test_save_cards_leave_a_netlist_that_says_what_to_save():
    tb = "* tb\n.subckt d in out\nR1 in m 1k\nR2 m out 1k\n.ends\nV1 in 0 1\nx1 in o d\n.tran 1n 1u\n.end\n"
    assert sim.is_transient(tb) and not sim.is_transient(tb.replace(".tran 1n 1u", ".op"))
    assert sim.is_transient(tb.replace(".tran 1n 1u", ".control\ntran 1n 1u\n.endc"))
    assert sim.save_cards(tb).split() == [".save", "in", "i(V1)", "o"]
    assert sim.save_cards(tb.replace(".end\n", ".options savecurrents\n.end\n")) == ""
    # a vector a .control block names without v(): kept, as one inside v() is
    ctl = tb.replace(".end\n", ".control\nrun\nlet d = x1.m - o\nmeas tran a avg v(x1.m)\nwrite out.raw\n.endc\n.end\n")
    assert sim.save_cards(ctl).split() == [".save", "in", "i(V1)", "o", "v(x1.m)"]


def test_a_transient_stores_the_testbench_nets_and_what_is_named():
    _need()
    root, srv, ctx = _setup()
    r = _run(ctx, analysis=TRAN)
    have = _vectors(srv, r["job"])
    assert {"v(in)", "v(o)"} <= set(have) and not [n for n in have if "x1." in n], have
    assert srv.jobs.get(srv.project("p").path, r["job"]).status()["saved"] == "testbench"
    assert any("not the nodes inside its cells" in n for n in r["notes"])
    # a node inside the cell named afterwards: the answer says how to keep it
    text, err = T.call(ctx, "plot_waveform", {"lib": "l", "cell": "tb", "job": r["job"], "signals": ["x1.m"]})
    assert err and "run it again naming them in signals (or with save_all)" in text
    s = _run(ctx, job=r["job"], signals=["x1.m", "o"])
    assert s["missing_signals"] == ["x1.m"] and "save_all" in s["missing_hint"]
    # named when it runs: stored, and read
    r = _run(ctx, analysis=TRAN, signals=["x1.m"])
    assert "v(x1.m)" in _vectors(srv, r["job"])
    assert r["signals"]["v(x1.m)"]["final"] == pytest.approx(2 / 3, rel=1e-3)
    # a measure on it: its .meas line names it
    r = _run(ctx, analysis=TRAN, measure=[".meas tran vm avg v(x1.m)"])
    assert r["measurements"]["vm"] == pytest.approx(2 / 3, rel=1e-3) and "v(x1.m)" in _vectors(srv, r["job"])
    shutil.rmtree(root)


def test_save_all_and_an_operating_point_store_every_node():
    _need()
    root, srv, ctx = _setup()
    r = _run(ctx, analysis=TRAN, save_all=True)
    st = srv.jobs.get(srv.project("p").path, r["job"]).status()
    assert "v(x1.m)" in _vectors(srv, r["job"]) and "saved" not in st
    assert st["inputs"]["options"]["save"] == "all"           # never taken for a run that stored less
    assert _run(ctx, analysis=TRAN)["job"] != r["job"]
    text, err = T.call(ctx, "run_simulation", {"lib": "l", "cell": "tb", "job": r["job"], "save_all": True})
    assert err and "save_all" in text and "describe a new run" in text
    op = _run(ctx, analysis={"type": "op"})
    assert op["node_voltages"]["x1.m"] == pytest.approx(2 / 3, rel=1e-3)
    shutil.rmtree(root)
