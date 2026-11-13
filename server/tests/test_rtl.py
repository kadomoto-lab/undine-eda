"""The RTL view (rtl.py): ports from module headers, the editor with undo and changesets, VCD waveforms, and Verilator
lint and simulation through the API and the agent's tools."""
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
from undine.design import rtl  # noqa: E402
from undine import tools
from undine.server.api import create_app  # noqa: E402
from undine.store.oplog import OpLog  # noqa: E402
import test_changeset as tc  # noqa: E402

COUNTER = """// a 4-bit counter
module counter (
    input  logic       clk,
    input  logic       rst,   /* synchronous */
    output logic [3:0] q
);
  always_ff @(posedge clk) if (rst) q <= 0; else q <= q + 1;
endmodule
"""

TB = """`timescale 1ns/1ps
module tb_counter;
  logic clk = 0, rst = 1; logic [3:0] q;
  counter dut(.clk(clk), .rst(rst), .q(q));
  always #5 clk = ~clk;
  initial begin
    $dumpfile("wave.vcd"); $dumpvars(0, tb_counter);
    #12 rst = 0;
    #100 $display("q=%0d", q);
    if (q !== 4'd%EXPECT%) $error("expected %EXPECT%, got %0d", q);
    $finish;
  end
endmodule
"""


def test_ports_ansi_and_non_ansi():
    assert rtl.module_ports(COUNTER, "counter") == [("clk", "input"), ("rst", "input"), ("q", "output")]
    old = "module adder(a, b, s);\n input [7:0] a, b;\n output [8:0] s;\n assign s = a + b;\nendmodule\n"
    assert rtl.module_ports(old, "adder") == [("a", "input"), ("b", "input"), ("s", "output")]
    assert rtl.module_ports("module p #(parameter W = 4) (input [W-1:0] d, output y); endmodule", "p") == [("d", "input"), ("y", "output")]
    assert rtl.modules(COUNTER + TB) == ["counter", "tb_counter"]


def test_editor_undo_and_changeset_adopt_and_revert(tmp_path):
    ed = rtl.RtlEditor(tmp_path, "counter", OpLog(tmp_path / "history.jsonl"))
    ed.create_file()
    assert ed.path.name == "counter.sv" and "module counter" in ed.text()
    assert ed.save(COUNTER) and not ed.save(COUNTER)              # an unchanged save records nothing
    assert ed.undo() and "always_ff" not in ed.text()
    assert ed.undo() and not ed.path.exists()                     # the created file goes with the creation
    assert ed.redo() and ed.redo() and ed.text() == COUNTER

    root, srv, cs = tc.setup()
    meta = cs.create("p", "RTL", "a counter", "agent:t")
    e2 = srv.rtl_editor(meta["preview"], "l", "counter")
    with e2.recording("write_rtl", {}, "agent:t", "a counter", meta["id"]):
        e2.save(COUNTER)
    assert {c["name"]: c["views"] for c in srv.project(meta["preview"]).library("l").cells()}["counter"] == ["rtl"]
    assert rtl.find_file(srv.project("p").library("l").path, "counter") is None
    cs.apply("p", meta["id"], "user")
    assert srv.rtl_editor("p", "l", "counter").text() == COUNTER
    cs.revert("p", meta["id"], "user")
    assert srv.rtl_editor("p", "l", "counter").text() == COUNTER   # like a created schematic, a revert keeps the file
    shutil.rmtree(root)


def test_vcd_reading_and_steps(tmp_path):
    (tmp_path / "wave.vcd").write_text("""$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$var wire 4 " q [3:0] $end
$upscope $end
$enddefinitions $end
#0
$dumpvars
0!
bxxxx "
$end
#5
1!
b0000 "
#10
0!
b0001 "
""")
    v = rtl.read_vcd(tmp_path / "wave.vcd")
    assert [s["name"] for s in v["signals"]] == ["tb.clk", "tb.q"] and v["timescale_s"] == 1e-9
    assert v["changes"]["tb.q"] == [(0, None), (5, 0), (10, 1)]
    w = rtl.waveform_json(tmp_path / "wave.vcd", None)
    assert w["x"]["values"] == [0.0, 5e-9, 5e-9, 1e-8, 1e-8]       # a vertical edge at each change
    assert w["traces"]["tb.q"]["y"] == [None, None, 0, 0, 1]


def _need_verilator():
    if not tools.find("verilator"):
        pytest.skip("Verilator is not installed")


def _wait(job, limit=300):
    t0 = time.time()
    while job.status()["state"] in ("queued", "running") and time.time() - t0 < limit:
        time.sleep(0.5)
    return job.status()


def test_lint_and_simulation_over_the_api():
    _need_verilator()
    root, srv, cs = tc.setup()
    client = TestClient(create_app(srv, None))
    base = "/api/p/p/l/l/c"
    assert client.post(f"{base}/counter/rtl", json={"op": "save", "text": COUNTER}).status_code == 200
    assert client.post(f"{base}/tb_counter/rtl", json={"op": "save", "text": TB.replace("%EXPECT%", "10")}).status_code == 200
    r = client.get(f"{base}/counter/rtl").json()
    assert r["file"] == "counter.sv" and r["ports"] == [["clk", "input"], ["rst", "input"], ["q", "output"]]
    lint = client.post(f"{base}/counter/rtl/lint").json()
    assert lint["ok"], lint
    bad = COUNTER.replace("q <= q + 1;", "q <= q + undefined_sig;")
    client.post(f"{base}/counter/rtl", json={"op": "save", "text": bad})
    lint = client.post(f"{base}/counter/rtl/lint").json()
    assert not lint["ok"] and any("undefined_sig" in m["message"] and m["line"] == 7 for m in lint["messages"]), lint
    client.post(f"{base}/counter/rtl", json={"op": "undo"})
    st = client.post("/api/p/p/rtl-sim", json={"lib": "l", "cell": "tb_counter"}).json()
    job = srv.jobs.get(srv.project("p").path, st["id"])
    st = _wait(job)
    assert st["state"] == "done", st
    assert "q=10" in st["summary"]["printed"] and st["summary"]["errors"] == []
    data = client.get(f"/api/p/p/jobs/{st['id']}/data").json()
    assert "tb_counter.q" in data["traces"] and data["x"]["unit"] == "s"
    shutil.rmtree(root)


def test_agent_writes_lints_and_simulates_rtl_and_a_failing_check_fails():
    _need_verilator()
    root, srv, cs = tc.setup()
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    L = {"lib": "l"}
    T.call(ctx, "open_changeset", {"title": "counter", "reason": "r"})
    res, err = T.call(ctx, "write_rtl", {**L, "cell": "counter", "text": COUNTER.replace("module counter", "module other"), "reason": "x"})
    assert err and "must define module counter" in res
    res, err = T.call(ctx, "write_rtl", {**L, "cell": "counter", "text": COUNTER, "reason": "the counter"})
    assert not err and json.loads(res)["lint"]["ok"], res
    T.call(ctx, "write_rtl", {**L, "cell": "tb_counter", "text": TB.replace("%EXPECT%", "11"), "reason": "a testbench"})
    r = json.loads(T.call(ctx, "run_rtl_sim", {**L, "cell": "tb_counter"})[0])
    assert r["state"] == "failed" and any("expected 11" in e for e in r["errors"]), r
    T.call(ctx, "write_rtl", {**L, "cell": "tb_counter", "text": TB.replace("%EXPECT%", "10"), "reason": "fix the check"})
    r = json.loads(T.call(ctx, "run_rtl_sim", {**L, "cell": "tb_counter"})[0])
    assert r["state"] == "done" and "q=10" in r["printed"] and "tb_counter.q" in r["signals"], r
    assert json.loads(T.call(ctx, "get_rtl", {**L, "cell": "counter"})[0])["ports"][2] == ["q", "output"]
    v = srv.intent_view(ctx.work, "l", "counter")                  # intent refers to the RTL module's ports
    assert v["refs"]["ports"] == ["clk", "rst", "q"]
    shutil.rmtree(root)
