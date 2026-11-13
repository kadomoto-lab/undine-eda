"""LibreLane backend (librelane.py): flow configurations, what a run needs, the inputs derived from the flow and the
design intent (with their sources), how LibreLane is invoked, and a run end to end with a stand-in for LibreLane."""
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from undine.agents import agent_tools as T  # noqa: E402
from undine.design import librelane  # noqa: E402
from undine.server.api import create_app  # noqa: E402
import test_changeset as tc  # noqa: E402
import test_rtl as tr  # noqa: E402

FAKE = '''import json, sys
from pathlib import Path
import klayout.db as kdb
args = sys.argv[1:]
if args == ["--version"]:
    print("LibreLane 0 (stand-in)"); sys.exit(0)
cfg = json.loads(Path(args[-1]).read_text())
final = Path("runs") / args[args.index("--run-tag") + 1] / "final"
(final / "gds").mkdir(parents=True)
ly = kdb.Layout(); top = ly.create_cell(cfg["DESIGN_NAME"])
top.shapes(ly.layer(68, 20)).insert(kdb.Box(0, 0, 1000, 1000))
ly.write(str(final / "gds" / (cfg["DESIGN_NAME"] + ".gds")))
(final / "metrics.json").write_text(json.dumps({"design__instance__count": 42, "timing__setup__ws": 3.5, "route__drc_errors": 0}))
print("[INFO] args", " ".join(args)); print("[INFO] pins", Path("pin_order.cfg").read_text().replace("\\n", " "))
'''


def _project():
    root, srv, cs = tc.setup()
    lib = srv.project("p").library("l").path
    (lib / "counter.sv").write_text(tr.COUNTER)
    (lib / "tb_counter.sv").write_text(tr.TB.replace("%EXPECT%", "10"))
    return root, srv, cs, lib


def test_flow_requirements_and_derived_inputs():
    root, srv, cs, lib = _project()
    path = srv.project("p").path
    flow = librelane.save_flow(path, "counter", {"top": {"lib": "l", "cell": "counter"}, "floorplan": {"core_util": 40},
                                                 "variables": {"PL_TARGET_DENSITY_PCT": 55}})
    assert librelane.list_flows(path)[0]["name"] == "counter"
    missing = [m["message"] for m in librelane.requirements(flow, lib, None)]
    assert "the clock port (or none for a combinational design)" in missing
    assert "the side of port q (or auto to let LibreLane place it)" in missing
    flow = librelane.save_flow(path, "counter", {**flow, "clock": {"port": "clk", "period_ns": 20}})
    ed = srv.intent_editor("p", "l", "counter")
    for port, side in (("clk", "W"), ("rst", "W"), ("q", "E")):
        ed.set_port(port, {"side": side})
    assert librelane.requirements(flow, lib, ed.model()) == []
    d = librelane.derive(flow, lib, ed.model(), "sky130A")
    cfg = d["config"]
    assert cfg["DESIGN_NAME"] == "counter" and cfg["VERILOG_FILES"] == ["dir::src/counter.sv"]      # no testbench
    assert cfg["CLOCK_PORT"] == "clk" and cfg["CLOCK_PERIOD"] == 20 and cfg["PL_TARGET_DENSITY_PCT"] == 55
    assert d["pin_order"] == "#E\nq(\\[\\d+\\])?\n#W\nclk(\\[\\d+\\])?\nrst(\\[\\d+\\])?\n"
    by = {x["variable"]: x["derived_from"] for x in d["derived"]}
    assert by["IO_PIN_ORDER_CFG"] == ["ports.q", "ports.clk", "ports.rst"] and by["CLOCK_PERIOD"] == ["flow.clock.period_ns"]
    with pytest.raises(ValueError, match="die_area_um"):
        librelane.save_flow(path, "bad", {"floorplan": {"die_area_um": [0, 0, 10]}})
    shutil.rmtree(root)



def _template(root: Path) -> Path:
    """A stand-in for a caravel_user_project checkout: the files a wrapper run takes from it."""
    t = root / "caravel_user_project"
    w = t / "openlane" / "user_project_wrapper"
    (w / "fixed_dont_change").mkdir(parents=True)
    (t / "verilog" / "rtl").mkdir(parents=True)
    (w / "config.json").write_text(json.dumps({"//": "comment", "DESIGN_NAME": "user_project_wrapper", "DIE_AREA": [0, 0, 2920, 3520],
                                               "FP_DEF_TEMPLATE": "dir::fixed_dont_change/user_project_wrapper.def",
                                               "VERILOG_FILES": ["dir::../../verilog/rtl/user_project_wrapper.v"],
                                               "MACROS": {"user_proj_example": {}}, "FP_PDN_CORE_RING": True, "CLOCK_PORT": "wb_clk_i"}))
    for f in (w / "fixed_dont_change" / "user_project_wrapper.def", w / "signoff.sdc", t / "verilog" / "rtl" / "defines.v"):
        f.write_text("// template file\n")
    return t


def _macro_run(path: Path, flow: str, design: str) -> Path:
    run = path / "runs" / f"20260101-000000-{flow}"
    final = run / "runs" / librelane.TAG / "final"
    for sub, name in (("gds", f"{design}.gds"), ("lef", f"{design}.lef"), ("nl", f"{design}.nl.v"), ("pnl", f"{design}.pnl.v"), ("spef/nom", f"{design}.nom.spef"),
                      ("lib/nom_tt_025C_1v80", f"{design}__nom_tt_025C_1v80.lib"), ("lib/max_ss_100C_1v60", f"{design}__max_ss_100C_1v60.lib")):
        (final / sub).mkdir(parents=True, exist_ok=True)
        (final / sub / name).write_text("x")
    (run / "status.json").write_text(json.dumps({"kind": "librelane", "flow": flow, "state": "done"}))
    return run


def test_caravel_macro_and_wrapper_flows(tmp_path):
    root, srv, cs, lib = _project()
    path = srv.project("p").path
    (lib / "counter.sv").write_text(tr.COUNTER.replace("module counter (", "module counter (\n`ifdef USE_POWER_PINS\n"
                                                       "    inout vccd1, inout vssd1,\n`endif"))
    macro = librelane.save_flow(path, "counter", {"top": {"lib": "l", "cell": "counter"}, "clock": {"port": "clk", "period_ns": 25},
                                                  "caravel": {"role": "macro"}})
    ed = srv.intent_editor("p", "l", "counter")
    for port in ("clk", "rst", "q"):
        ed.set_port(port, {"side": "auto"})
    assert librelane.requirements(macro, lib, ed.model(), path) == []        # vccd1 / vssd1 need no side: the PDN connects them
    cfg = librelane.derive(macro, lib, ed.model(), "sky130A", path)["config"]
    assert cfg["VDD_NETS"] == ["vccd1"] and cfg["GND_NETS"] == ["vssd1"] and cfg["FP_PDN_MULTILAYER"] is False and cfg["RT_MAX_LAYER"] == "met4"
    (lib / "plain.sv").write_text("module plain (input logic clk); endmodule\n")
    plain = {"top": {"lib": "l", "cell": "plain"}, "clock": {"port": "clk", "period_ns": 25}, "caravel": {"role": "macro"}}
    assert any("vccd1 and vssd1" in m["message"] for m in librelane.requirements(plain, lib, None, path))

    tpl = _template(tmp_path)
    (lib / "user_project_wrapper.v").write_text("module user_project_wrapper (input wb_clk_i);\n  counter mprj (.clk(wb_clk_i));\nendmodule\n")
    wrapper = librelane.save_flow(path, "wrapper", {"top": {"lib": "l", "cell": "user_project_wrapper"}, "caravel": {
        "role": "wrapper", "template": str(tpl), "macros": [{"flow": "counter", "instance": "mprj", "location": [60, 15]}]}})
    assert wrapper["caravel"]["macros"][0]["orientation"] == "N" and wrapper["caravel"]["macros"][0]["power"] == "vccd1 vssd1"
    missing = [m["message"] for m in librelane.requirements(wrapper, lib, None, path)]
    assert missing == ["a successful run of flow counter (the macro mprj): run it first"]
    _macro_run(path, "counter", "counter")
    assert librelane.requirements(wrapper, lib, None, path) == []
    d = librelane.derive(wrapper, lib, None, "sky130A", path)
    cfg = d["config"]
    assert cfg["DIE_AREA"] == [0, 0, 2920, 3520] and cfg["FP_PDN_CORE_RING"] is True and "//" not in cfg     # from the template
    assert cfg["FP_DEF_TEMPLATE"] == str(tpl / "openlane/user_project_wrapper/fixed_dont_change/user_project_wrapper.def")
    assert cfg["VERILOG_FILES"] == [str(tpl / "verilog/rtl/defines.v"), "dir::src/user_project_wrapper.v"]    # not the macro's RTL
    m = cfg["MACROS"]["counter"]
    assert m["instances"] == {"mprj": {"location": [60.0, 15.0], "orientation": "N"}} and m["gds"] == ["dir::macros/counter/counter.gds"]
    assert m["lib"] == {"*": "dir::macros/counter/lib_nom_tt_025C_1v80_counter__nom_tt_025C_1v80.lib"} and "nom_*" in m["spef"]
    assert m["pnl"] == ["dir::macros/counter/counter.pnl.v"]                     # the wrapper's lint sees the power pins
    assert cfg["PDN_MACRO_CONNECTIONS"] == ["mprj vccd1 vssd1 vccd1 vssd1"]
    assert {rel for _, rel in d["copies"]} >= {"macros/counter/counter.gds", "macros/counter/counter.nl.v"}
    assert [f.name for f in librelane.design_sources(lib, "counter")] == ["counter.sv"]        # not the wrapper beside it
    (lib / "sub.v").write_text("module sub (input a); endmodule\n")
    (lib / "counter.sv").write_text((lib / "counter.sv").read_text().replace("endmodule", "  sub u_sub (.a(clk));\nendmodule"))
    assert [f.name for f in librelane.design_sources(lib, "counter")] == ["counter.sv", "sub.v"]  # what it instantiates
    (lib / "user_project_wrapper.v").write_text("module user_project_wrapper (input wb_clk_i);\nendmodule\n")
    assert [m["message"] for m in librelane.requirements(wrapper, lib, None, path)] == ["an instance mprj of counter in the wrapper's RTL"]
    with pytest.raises(ValueError, match="orientation"):
        librelane.save_flow(path, "bad", {"caravel": {"role": "wrapper", "macros": [{"flow": "a", "instance": "b", "orientation": "Q"}]}})
    shutil.rmtree(root)

def test_how_librelane_is_invoked(monkeypatch):
    monkeypatch.setenv("UNDINE_LIBRELANE", "nix-shell /opt/librelane --run")
    assert librelane.command(["--pdk", "sky130A", "config.json"]) == ["nix-shell", "/opt/librelane", "--run", "librelane --pdk sky130A config.json"]
    monkeypatch.setenv("UNDINE_LIBRELANE", "/usr/bin/python3 /x/fake.py")
    assert librelane.command(["config.json"]) == ["/usr/bin/python3", "/x/fake.py", "config.json"]


def test_a_run_end_to_end_with_a_stand_in(monkeypatch, tmp_path):
    (tmp_path / "fake.py").write_text(FAKE)
    monkeypatch.setenv("UNDINE_LIBRELANE", f"{sys.executable} {tmp_path / 'fake.py'}")
    root, srv, cs, lib = _project()
    fake_pdk = type("P", (), {"name": "sky130A", "root": tmp_path / "pdk" / "sky130A", "id": "sky130A@test", "family": "sky130",
                              "version": "test", "lyp": None, "xschem_dir": None})()
    monkeypatch.setattr(type(srv.project("p")), "pdk", property(lambda self: fake_pdk))
    client = TestClient(create_app(srv, None))
    assert client.put("/api/p/p/flows/counter", json={"top": {"lib": "l", "cell": "counter"}, "clock": {"port": "clk", "period_ns": 25}}).status_code == 200
    chk = client.get("/api/p/p/flows/counter/check").json()
    assert chk["librelane"]["available"] and any("side of port clk" in m["message"] for m in chk["missing"])
    assert client.post("/api/p/p/flows/counter/run").status_code == 400                        # still missing
    ed = srv.intent_editor("p", "l", "counter")
    for port in ("clk", "rst", "q"):
        ed.set_port(port, {"side": "auto"})                                                     # let the tool decide
    chk = client.get("/api/p/p/flows/counter/check").json()
    assert chk["missing"] == [] and "IO_PIN_ORDER_CFG" not in chk["config"]
    ed.set_port("q", {"side": "N"})
    st = client.post("/api/p/p/flows/counter/run").json()
    job = srv.jobs.get(srv.project("p").path, st["id"])
    t0 = time.time()
    while job.status()["state"] in ("queued", "running") and time.time() - t0 < 60:
        time.sleep(0.2)
    st = job.status()
    assert st["state"] == "done", (st, job.log())
    assert st["summary"]["metrics"] == {"cells": 42, "setup slack (ns)": 3.5, "routing DRC errors": 0}
    assert "--manual-pdk --pdk-root" in job.log() and "#N q" in job.log()
    res = client.get(f"/api/p/p/jobs/{st['id']}/librelane").json()
    assert res["gds"] == "runs/undine/final/gds/counter.gds" and res["derived"][0]["variable"] == "DESIGN_NAME"
    r = client.post(f"/api/p/p/jobs/{st['id']}/import-gds").json()
    assert "counter" in r["new_cells"]
    assert "layout" in {c["name"]: c["views"] for c in srv.project("p").library("l").cells()}["counter"]

    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    out = json.loads(T.call(ctx, "check_flow", {"flow": "counter"})[0])
    assert out["missing"] == [] and any(x["variable"] == "IO_PIN_ORDER_CFG" for x in out["derived"])
    r = json.loads(T.call(ctx, "get_librelane_result", {"job": st["id"]})[0])
    assert r["state"] == "done" and r["metrics"]["cells"] == 42
    shutil.rmtree(root)


def test_librelane_runs_without_undines_environment(monkeypatch):
    own = str(Path(sys.prefix) / "bin")
    monkeypatch.setenv("PATH", f"{own}:/home/u/.pixi/envs/default/bin:/usr/bin:/bin")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/home/u/undine-tools/lib")
    monkeypatch.setenv("CONDA_PREFIX", "/x")
    monkeypatch.setenv("PIXI_ENVIRONMENT_NAME", "default")
    monkeypatch.setenv("HOME", "/home/u")
    env = librelane.clean_env()
    assert "LD_LIBRARY_PATH" not in env and "CONDA_PREFIX" not in env and "PIXI_ENVIRONMENT_NAME" not in env
    assert env["HOME"] == "/home/u"
    path = env["PATH"].split(":")
    assert own not in path and not any(".pixi/envs" in p for p in path) and "/usr/bin" in path


def test_errors_come_from_librelanes_error_log_with_a_hint(tmp_path):
    from undine.simulation.sim import Job
    (tmp_path / "runs" / librelane.TAG).mkdir(parents=True)
    (tmp_path / "runs" / librelane.TAG / "error.log").write_text(
        "[PDN-0185] Insufficient width (19.78 um) to add straps on layer met4 in grid \"stdcell_grid\"\n  with total strap width 4.9 um.\n")
    errors = librelane.run_errors(Job(tmp_path))
    assert errors == ['[PDN-0185] Insufficient width (19.78 um) to add straps on layer met4 in grid "stdcell_grid" with total strap width 4.9 um.']
    assert librelane.hints(errors)[0].startswith("the die is too small for the power straps")
