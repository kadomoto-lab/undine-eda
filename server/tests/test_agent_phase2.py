"""Agents and dependency trust (packages phase 2): no tool, client method or `undine call` reaches trust, a refusal of
an untrusted dependency reaches the agent as the tool's error text unchanged (naming the cell and that a person can
trust it), and the assistant's prompt says trusting is a person's action."""
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from undine.agents import agent, agent_tools as T  # noqa: E402
from undine.agents.mcp_local import OPEN_TOOL  # noqa: E402
from undine.design import rtl  # noqa: E402
from undine.simulation import specs as specsmod  # noqa: E402
from undine.store import trust  # noqa: E402
from test_netlist_sources import _cell, _r, _sub  # noqa: E402
from test_trust_run import _install, _refused, _runs, _server, _tb  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "undine"


@pytest.fixture(autouse=True)
def _store(fresh_store):
    return fresh_store


# ------------------------------------------------------------------ nothing agent-facing


def test_no_tool_client_method_or_call_reaches_trust_or_a_confirmation():
    names = [t["name"] for t in T.TOOLS] + list(T.BY_NAME) + [OPEN_TOOL["name"]]   # BY_NAME: what `undine call` runs
    assert names and not [n for n in names if "trust" in n.lower() or "confirm" in n.lower()]
    for f in [*sorted((SRC / "agents").glob("*.py")), SRC / "client.py"]:
        text = f.read_text()
        for word in ("/trust", "set_trusted", '"trusted"', "'trusted'", "confirmations"):
            assert word not in text, (f.name, word)
    from undine.agents.client import Undine
    assert not [m for m in dir(Undine) if "trust" in m.lower() or "confirm" in m.lower()]


def test_the_prompt_says_trusting_a_dependency_is_a_persons_action():
    cannot = next(x for x in agent.SYSTEM_PROPOSE.splitlines() if x.startswith("- You cannot apply a changeset"))
    assert "mark a dependency's code trusted" in cannot and "a person does it" in cannot
    assert "Trust..." in cannot and "`undine lib trust`" in cannot
    assert "ask the person to trust the dependency" in agent.SYSTEM_PROPOSE


# ------------------------------------------------------------------ refusals as data


def _ctx(srv) -> T.ToolCtx:
    return T.ToolCtx(srv, "p", actor="agent:t")


def test_run_simulation_on_an_untrusted_dependency_returns_the_refusal_unchanged(tmp_path):
    srv = _server(tmp_path)
    d = _install(srv)
    _cell(d, "inv", _r("devices/res.sym", "R1", "1k\n.control\nshell rm -rf x\n.endc"))
    _tb(srv, _sub("ip/inv.sym", "x1"))
    want = str(_refused(lambda: srv.start_sim("p", "l", "tb", {"type": "op"})))

    text, err = T.call(_ctx(srv), "run_simulation", {"lib": "l", "cell": "tb", "analysis": {"type": "op"}})
    assert err and text == want
    assert text.startswith("ip/inv (dependency git ") and "a person can trust" in text and text.endswith("an agent cannot")
    assert _runs(srv) == []
    # without an analysis the testbench's own (none here) runs: refused in the same way, not an internal error
    text, err = T.call(_ctx(srv), "run_simulation", {"lib": "l", "cell": "tb"})
    assert err and text == want


def test_check_specs_and_sweep_pass_the_refusal_on(tmp_path, monkeypatch):
    """specs.run lets the refusal of the first point through; the tool says it as it is, once."""
    srv = _server(tmp_path)
    d = _install(srv)
    _cell(d, "inv", _r("devices/res.sym", "R1", "1k\n.control\nshell rm -rf x\n.endc"))
    _tb(srv, _sub("ip/inv.sym", "x1"))
    want = str(_refused(lambda: srv.start_sim("p", "l", "tb", {"type": "op"})))
    spec = {"kind": "spec", "id": "s1", "spec": {"measure": "v(out).max", "max": 1.0, "testbench": "l/tb",
                                                 "analysis": {"type": "op"}}}
    monkeypatch.setattr(T, "_spec_probes", lambda c, lib, cell, ids: specsmod.probes({"items": [spec]}, lib))
    monkeypatch.setattr(T, "_check_vary", lambda c, lib, points: None)
    calls = []
    real = srv.start_sim
    monkeypatch.setattr(srv, "start_sim", lambda *a, **k: calls.append(1) or real(*a, **k))
    text, err = T.call(_ctx(srv), "check_specs", {"lib": "l", "cell": "tb"})
    assert err and text == want and calls
    text, err = T.call(_ctx(srv), "sweep", {"lib": "l", "cell": "tb", "vary": {"x1.W": [1, 2]}})
    assert err and text == want
    assert _runs(srv) == []


def test_run_rtl_sim_and_lint_rtl_on_an_untrusted_dependency_return_the_refusal(tmp_path, monkeypatch):
    srv = _server(tmp_path)
    d = _install(srv)
    prj = srv.project("p")
    (d / "tb.sv").write_text('module tb;\n  initial begin\n    $c("system(\\"touch x\\");");\n  end\nendmodule\n')
    monkeypatch.setattr(rtl, "verilator", lambda: shutil.which("false") or "/bin/false")   # never builds anything
    want = str(_refused(lambda: rtl.submit_sim(srv.jobs, prj.path, d, "tb", "t")))

    text, err = T.call(_ctx(srv), "run_rtl_sim", {"lib": "ip", "cell": "tb"})
    assert err and text == want
    assert text.startswith("ip/tb.sv (dependency git ") and "$c" in text and "a person can trust" in text
    assert _runs(srv) == []

    (d / "tb.sv").write_text('`include "/etc/passwd"\nmodule tb;\nendmodule\n')
    want = str(_refused(lambda: rtl.lint(d, "tb")))
    text, err = T.call(_ctx(srv), "lint_rtl", {"lib": "ip", "cell": "tb"})
    assert err and text == want and "/etc/passwd" in text and "a person can trust" in text

    trust.set_trusted(prj, trust.key_of(d), True)            # a person trusted it: the agent's lint runs
    text, err = T.call(_ctx(srv), "lint_rtl", {"lib": "ip", "cell": "tb"})
    assert "not trusted" not in text
