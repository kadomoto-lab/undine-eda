"""A schematic of standard cells as a gate-level netlist (gatelevel.py), the LibreLane flow built from it (source
schematic), flow configurations proposed in a changeset (write_flow, FlowEditor), the KLayout LVS reading of standard
cells (stdcells.py), and Verilator with the PDK's cell models (lint_rtl / run_rtl_sim of a gate-level netlist)."""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import klayout.db as kdb  # noqa: E402
import pytest  # noqa: E402

from undine import tools  # noqa: E402
from undine.agents import agent_tools as T  # noqa: E402
from undine.checks import stdcells  # noqa: E402
from undine.design import gatelevel, librelane  # noqa: E402
from undine.pdks.pdk import default_pdk_roots, discover  # noqa: E402
from undine.store.changeset import Changesets  # noqa: E402

SKY = next((p for p in discover(default_pdk_roots()).values() if p.name == "sky130A" and
            (p.root / "libs.ref/sky130_fd_sc_hd/spice/sky130_fd_sc_hd.spice").is_file()), None)
needs_sky = pytest.mark.skipif(SKY is None, reason="sky130A with its standard cells is not installed")


def _call(ctx, tool, **a):
    res, err = T.call(ctx, tool, {"reason": "x", **a})
    assert not err, res
    return json.loads(res)


def _logic():
    """A project on sky130A with lib l / cell blk drawn from standard cells: a tie cell's HI into a NAND, an inverter,
    a flop; ports a, clk (in), q (out), vdd / vss (the cells' supply parameters)."""
    from undine.server.api import Server
    root = Path(tempfile.mkdtemp())
    srv = Server(root, {SKY.id: SKY}, [root])
    srv.store.create("p", SKY.id).create_library("l")
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    assert not T.call(ctx, "open_changeset", {"title": "logic", "reason": "r"})[1]
    _call(ctx, "create_cell", lib="l", cell="blk", views=["schematic"])
    ed = srv.editor(ctx.work, "l", "blk", "schematic")
    if ed.resolver.load("sky130_stdcells/conb_1.sym", ed.path.parent) is None:
        shutil.rmtree(root)
        pytest.skip("Xschem's sky130 symbols are not installed")
    sup = {"VPWR": "vdd", "VPB": "vdd", "VGND": "vss", "VNB": "vss"}
    for i, (sym, name) in enumerate([("conb_1", "xt"), ("inv_1", "x1"), ("nand2_1", "x2"), ("dfxtp_1", "x3")]):
        _call(ctx, "add_instance", lib="l", cell="blk", symbol=f"sky130_stdcells/{sym}.sym", x=300 * i, y=0,
              params={"name": name, **sup})
    for i, (sym, lab) in enumerate([("ipin", "a"), ("ipin", "clk"), ("opin", "q"), ("iopin", "vdd"), ("iopin", "vss")]):
        _call(ctx, "add_instance", lib="l", cell="blk", symbol=f"devices/{sym}.sym", x=-300, y=100 * i,
              params={"name": f"p{i}", "lab": lab})
    for net, pins in {"a": ["x1:A"], "n1": ["x1:Y", "x2:A"], "hi": ["xt:HI", "x2:B"], "y": ["x2:Y", "x3:D"],
                      "clk": ["x3:CLK"], "q": ["x3:Q"]}.items():
        _call(ctx, "connect_pins", lib="l", cell="blk", net=net, pins=pins, style="label")
    return root, srv, ctx


@needs_sky
def test_a_schematic_of_standard_cells_as_gate_level_verilog():
    root, srv, ctx = _logic()
    out = _call(ctx, "export_verilog", lib="l", cell="blk")
    text = out["text"]
    assert out["instances"] == 4 and out["power_ports"] == ["vdd", "vss"] and out["warnings"] == []
    assert out["ports"] == [["a", "input"], ["clk", "input"], ["q", "output"]]
    assert "`ifdef USE_POWER_PINS\n    inout vdd,\n    inout vss,\n`endif\n    input a," in text
    assert "    wire hi;" in text and "    wire n1;" in text and "    wire y;" in text
    assert "sky130_fd_sc_hd__conb_1 xt (\n`ifdef USE_POWER_PINS\n        .VGND(vss), .VNB(vss), .VPB(vdd), .VPWR(vdd),\n" \
           "`endif\n        .HI(hi), .LO());" in text                       # an output nothing reads: left open
    assert ".A(n1), .B(hi), .Y(y));" in text.replace("\n", " ").replace("  ", " ") or ".A(n1)" in text
    # an input straight on a supply: written as a constant, with a warning (a tie cell gives it its own driver)
    _call(ctx, "connect_pins", lib="l", cell="blk", net="vdd", pins=["x2:B"], style="label", move=True)
    out = _call(ctx, "export_verilog", lib="l", cell="blk")
    assert ".B(1'b1)" in out["text"] and any("x2.B is on the supply vdd" in w for w in out["warnings"])
    # anything but a standard cell is refused
    _call(ctx, "add_instance", lib="l", cell="blk", symbol="sky130_fd_pr/res_generic_po.sym", x=0, y=400,
          params={"name": "R1"})
    res, err = T.call(ctx, "export_verilog", {"lib": "l", "cell": "blk"})
    assert err and "R1" in res and "is not a standard cell" in res
    shutil.rmtree(root)


def test_what_a_run_did_to_the_drawn_netlist():
    drawn = gatelevel.instances("module m (input a);\n  sky130_fd_sc_hd__inv_1 x1 (.A(a), .Y(b));\n"
                                "  sky130_fd_sc_hd__conb_1 \\xt[0]  (.HI(h), .LO());\nendmodule\n")
    assert drawn == {"x1": "sky130_fd_sc_hd__inv_1", "xt[0]": "sky130_fd_sc_hd__conb_1"}
    built = {**drawn, "TAP_1": "sky130_fd_sc_hd__tapvpwrvgnd_1", "FILLER_0_1": "sky130_fd_sc_hd__decap_3",
             "ANTENNA_1": "sky130_fd_sc_hd__diode_2"}
    same = gatelevel.compare(drawn, built)
    assert same["kept"] and same["physical"] == {"sky130_fd_sc_hd__decap_3": 1, "sky130_fd_sc_hd__diode_2": 1,
                                                 "sky130_fd_sc_hd__tapvpwrvgnd_1": 1}
    changed = gatelevel.compare(drawn, {"x1": "sky130_fd_sc_hd__inv_2", "rebuffer1": "sky130_fd_sc_hd__buf_1"})
    assert not changed["kept"] and changed["added"] == ["rebuffer1 (sky130_fd_sc_hd__buf_1)"]
    assert changed["removed"] == ["xt[0] (sky130_fd_sc_hd__conb_1)"]
    assert changed["changed"] == ["x1: sky130_fd_sc_hd__inv_1 -> sky130_fd_sc_hd__inv_2"]


@needs_sky
def test_a_flow_from_the_schematic_keeps_the_netlist_as_drawn():
    root, srv, ctx = _logic()
    prj = srv.project(ctx.work)
    lib = prj.library("l").path
    flow = librelane.normalize_flow({"top": {"lib": "l", "cell": "blk"}, "source": "schematic",
                                     "clock": {"port": "clk", "period_ns": 10}, "floorplan": {"die_area_um": [0, 0, 40, 40]},
                                     "variables": {"RUN_CTS": True}})
    assert flow["source"] == "schematic"
    with pytest.raises(ValueError, match="source is rtl"):
        librelane.normalize_flow({"source": "netlist"})
    ed = srv.intent_editor(ctx.work, "l", "blk")
    gate = librelane.gate_netlist(prj, flow, ed.model())
    missing = [m["message"] for m in librelane.requirements(flow, lib, ed.model(), prj.path, gate)]
    assert missing == [f"the side of port {p} (or auto to let LibreLane place it)" for p in ("a", "clk", "q")]
    # (vdd / vss are the PDN's: no side needed)
    for port, side in (("a", "W"), ("clk", "W"), ("q", "E"), ("vdd", "N"), ("vss", "S")):
        ed.set_port(port, {"side": side})
    assert librelane.requirements(flow, lib, ed.model(), prj.path, gate) == []
    d = librelane.derive(flow, lib, ed.model(), "sky130A", prj.path, gate)
    cfg = d["config"]
    assert cfg["VERILOG_FILES"] == ["dir::src/blk.v"] and d["generated"]["blk.v"] == gate["text"]
    assert cfg["SYNTH_ELABORATE_ONLY"] is True and cfg["RSZ_DONT_TOUCH_RX"] == ".*" and cfg["DIODE_ON_PORTS"] == "none"
    assert cfg["RUN_CTS"] is True                                    # a flow variable overrides the derived setting
    assert "vdd" not in d["pin_order"] and "#W\na(" in d["pin_order"]   # the supplies are the PDN's pins
    by = {x["variable"]: x["derived_from"] for x in d["derived"] if x["variable"] == "RUN_POST_GRT_RESIZER_TIMING"}
    assert by == {"RUN_POST_GRT_RESIZER_TIMING": ["flow.source"]}
    # a schematic that is not all standard cells: what the run misses says so
    with pytest.raises(ValueError, match="source rtl"):
        librelane.normalize_flow({"source": "schematic", "caravel": {"role": "wrapper"}})
    bad = librelane.requirements(flow, lib, ed.model(), prj.path, {"error": "R1 (x.sym) is not a standard cell of the PDK"})
    assert bad == [{"entry": "flow.source", "message": "R1 (x.sym) is not a standard cell of the PDK"}]
    shutil.rmtree(root)


def test_a_flow_proposed_in_a_changeset():
    import test_changeset as tc
    root, srv, cs = tc.setup()
    lib = srv.project("p").library("l").path
    (lib / "counter.sv").write_text("module counter (input clk, output q);\nendmodule\n")
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    assert not T.call(ctx, "open_changeset", {"title": "flow", "reason": "r"})[1]
    config = {"top": {"lib": "l", "cell": "counter"}, "clock": {"port": "clk", "period_ns": 20},
              "floorplan": {"core_util": 40}}
    checked = _call(ctx, "check_flow", config=config)                 # inline: no flow saved yet
    assert checked["missing"] == [f"the side of port {p} (or auto to let LibreLane place it)" for p in ("clk", "q")]
    res, err = T.call(ctx, "check_flow", {})
    assert err and "give flow" in res
    out = _call(ctx, "write_flow", flow="counter", config=config, reason="harden the counter")
    assert out["changed"] and out["file"] == "flows/counter.yaml" and out["missing"] == checked["missing"]
    work = srv.project(ctx.work).path
    assert (work / "flows" / "counter.yaml").is_file() and not (srv.project("p").path / "flows").exists()
    assert [f["name"] for f in _call(ctx, "list_flows")["flows"]] == ["counter"]
    ops = [o for lc in cs.get("p", ctx.changeset)["changes"] for o in lc["ops"]]
    assert [(o["op"], o["view"], o["cell"]) for o in ops] == [("write_flow", "flow", "counter")]
    assert ops[0]["effects"][0]["file"] == "flows/counter.yaml" and ops[0]["effects"][0]["changes"]["text"][0] == ""
    # undo / redo through the server's editor, as the review does
    ed = srv.editor(ctx.work, "l", "counter", "flow")
    assert ed.undo() and not (work / "flows" / "counter.yaml").exists()
    assert ed.redo() and "core_util: 40.0" in (work / "flows" / "counter.yaml").read_text()
    out = _call(ctx, "write_flow", flow="counter", config={**config, "floorplan": {"core_util": 30}})
    assert out["changed"] and not _call(ctx, "write_flow", flow="counter", config={**config, "floorplan": {"core_util": 30}})["changed"]
    res, err = T.call(ctx, "write_flow", {"flow": "x", "config": {"top": {"lib": "l", "cell": "counter"}, "target": "make"},
                                          "reason": "x"})
    assert err and "target is librelane" in res
    # a restore to the start takes the file away; the flow comes back with a redo-free write, then the apply
    cs.restore("p", ctx.changeset, 0, "user")
    assert not (work / "flows" / "counter.yaml").exists()
    _call(ctx, "write_flow", flow="counter", config=config)
    cs.apply("p", ctx.changeset, "user")
    assert librelane.load_flow(srv.project("p").path, "counter")["floorplan"] == {"core_util": 40.0}
    shutil.rmtree(root)


@needs_sky
def test_standard_cells_in_the_lvs_reference_and_layout():
    ref = ("* top\n.subckt blk a y vdd vss\n"
           "xt vss vss vdd vdd hi net1 sky130_fd_sc_hd__conb_1\n"
           "x1 a vss vss vdd vdd n1 sky130_fd_sc_hd__inv_1\n"
           "x2 n1 hi vss vss vdd vdd y sky130_fd_sc_hd__nand2_1\n"
           "x3 hi vss vss vdd vdd n2 sky130_fd_sc_hd__dfxtp_1\n.ends\n")
    text, info = stdcells.reference(ref, SKY)
    assert info["ties"] == ["xt"] and info["joined"] == {"hi": "vdd", "net1": "vss"}
    assert info["cells"] == ["sky130_fd_sc_hd__dfxtp_1", "sky130_fd_sc_hd__inv_1", "sky130_fd_sc_hd__nand2_1"]
    assert "x2 n1 vdd vss vss vdd vdd y sky130_fd_sc_hd__nand2_1" in text and "conb_1" not in text
    inv = text[text.index(".subckt sky130_fd_sc_hd__inv_1"):]
    assert inv.splitlines()[1].startswith("M") and "W=" in inv and "w=" not in inv.split(".ends")[0]
    dff = text[text.index(".subckt sky130_fd_sc_hd__dfxtp_1"):].split(".ends")[0]
    assert "special_nfet" not in dff and "sky130_fd_pr__nfet_01v8 W=0.36 L=0.15" in dff
    assert stdcells.reference(".subckt t a\nR1 a 0 1k\n.ends\n", SKY) == (".subckt t a\nR1 a 0 1k\n.ends\n", {})
    # the layout: the pwell labels out of the standard cells, the devices out of the decap (its wells and rails stay)
    gds = kdb.Layout()
    gds.read(str(SKY.root / "libs.ref/sky130_fd_sc_hd/gds/sky130_fd_sc_hd.gds"))
    ly = kdb.Layout()
    ly.dbu = gds.dbu
    top = ly.create_cell("blk")
    for i, name in enumerate(["sky130_fd_sc_hd__dfxtp_1", "sky130_fd_sc_hd__decap_3"]):
        c = ly.create_cell(name)
        c.copy_tree(gds.cell(name))
        top.insert(kdb.CellInstArray(c.cell_index(), kdb.Trans(i * 10000, 0)))
    out = stdcells.layout_for_lvs(ly, "blk", SKY)
    pw, diff, nwell = (out.find_layer(64, 59), out.find_layer(65, 20), out.find_layer(64, 20))
    texts = lambda c, li: [s.text_string for s in c.shapes(li).each() if s.is_text()]      # noqa: E731
    assert texts(out.cell("sky130_fd_sc_hd__dfxtp_1"), pw) == [] and texts(ly.cell("sky130_fd_sc_hd__dfxtp_1"), pw)
    assert not out.cell("sky130_fd_sc_hd__dfxtp_1").shapes(diff).is_empty()           # a logic cell keeps its devices
    assert out.cell("sky130_fd_sc_hd__decap_3").shapes(diff).is_empty()
    assert not out.cell("sky130_fd_sc_hd__decap_3").shapes(nwell).is_empty()
    assert not ly.cell("sky130_fd_sc_hd__decap_3").shapes(diff).is_empty()            # the layout itself is untouched
    plain = kdb.Layout()
    plain.create_cell("x")
    assert stdcells.layout_for_lvs(plain, "x", SKY) is None


@needs_sky
@pytest.mark.skipif(not tools.find("verilator"), reason="Verilator is not installed")
def test_verilator_reads_a_gate_level_netlist_with_the_cell_models():
    root, srv, ctx = _logic()
    # a decap drawn on purpose: supply pins alone, written with no comma after them
    _call(ctx, "add_instance", lib="l", cell="blk", symbol="sky130_stdcells/decap_4.sym", x=1500, y=0,
          params={"name": "xc", "VPWR": "vdd", "VPB": "vdd", "VGND": "vss", "VNB": "vss"})
    text = _call(ctx, "export_verilog", lib="l", cell="blk")["text"]
    assert "sky130_fd_sc_hd__decap_4 xc (\n`ifdef USE_POWER_PINS\n        .VGND(vss), .VNB(vss), .VPB(vdd), .VPWR(vdd)\n" \
           "`endif\n        );" in text, text
    out = _call(ctx, "lint_rtl", lib="l", cell="blk")            # no RTL: its schematic's netlist
    assert out["ok"], out
    assert out["gate_level"]["from_schematic"] == ["blk"] and "FUNCTIONAL" in out["gate_level"]["defines"]
    assert out["files"][:1] == ["blk.v"] and "sky130_fd_sc_hd.v" in out["files"]
    assert not [m for m in out["messages"] if m["file"] in ("primitives.v", "sky130_fd_sc_hd.v")]
    # a testbench in RTL around it: q follows NAND(NOT a, 1) = a, clocked
    tb = """`timescale 1ns/1ps
module tb_blk;
  reg a = 0, clk = 0;
  wire q;
  blk dut (.a(a), .clk(clk), .q(q));
  always #5 clk = ~clk;
  initial begin
    a = 1; #12; if (q !== 1'b1) $error("q is %b, not 1", q);
    a = 0; #10; if (q !== 1'b0) $error("q is %b, not 0", q);
    $display("checked"); $finish;
  end
endmodule
"""
    _call(ctx, "write_rtl", lib="l", cell="tb_blk", text=tb)
    out = _call(ctx, "run_rtl_sim", lib="l", cell="tb_blk")
    job = srv.jobs.get(srv.project(ctx.work).path, out["job"])
    t0 = time.time()
    while job.status()["state"] in ("queued", "running") and time.time() - t0 < 300:
        time.sleep(1)
    st = job.status()
    assert st["state"] == "done", (st.get("error"), job.log(400)[:3000])
    assert "checked" in st["summary"]["printed"] and st["from_schematic"] == ["blk"]
    assert (job.path / "src" / "blk.v").is_file()
    shutil.rmtree(root)


@needs_sky
def test_a_flow_from_the_schematic_takes_its_supplies_and_refuses_what_it_cannot_build():
    root, srv, ctx = _logic()
    prj = srv.project(ctx.work)
    lib = prj.library("l").path
    ed = srv.intent_editor(ctx.work, "l", "blk")
    for port, side in (("a", "W"), ("clk", "W"), ("q", "E"), ("vdd", "N")):
        ed.set_port(port, {"side": side})
    assert ed.model()["supplies"] == {}                  # the intent names no supply: the cells' VPWR / VGND do
    flow = librelane.normalize_flow({"top": {"lib": "l", "cell": "blk"}, "source": "schematic",
                                     "clock": {"port": "clk", "period_ns": 10}, "floorplan": {"core_util": 30}})
    gate = librelane.gate_netlist(prj, flow, ed.model())
    assert gate["supplies"] == {"vdd": "supply", "vss": "ground"} and gate["tie_cell"].startswith("sky130_fd_sc_hd__conb_1")
    assert librelane.requirements(flow, lib, ed.model(), prj.path, gate) == []
    d = librelane.derive(flow, lib, ed.model(), "sky130A", prj.path, gate)
    assert d["config"]["VDD_NETS"] == ["vdd"] and d["config"]["GND_NETS"] == ["vss"]
    by = {x["variable"]: x["derived_from"] for x in d["derived"]}
    assert by["VDD_NETS"] == ["flow.source", "schematic.vdd"] and by["GND_NETS"] == ["flow.source", "schematic.vss"]
    # the side the intent gives a supply port is not used (the PDN makes its pins): said, not dropped silently
    assert "vdd" not in d["pin_order"]
    assert [(i["entry"], i["value"]) for i in d["ignored"]] == [("ports.vdd.side", "N")]
    checked = _call(ctx, "check_flow", config=flow)
    assert checked["ignored"] == [d["ignored"][0]["message"]] and "PDN" in checked["ignored"][0]
    # an input on a supply, an input nothing drives, a supply that is no port: refused before a run
    _call(ctx, "connect_pins", lib="l", cell="blk", net="vdd", pins=["x2:B"], style="label", move=True)
    _call(ctx, "connect_pins", lib="l", cell="blk", net="nd", pins=["x3:D"], style="label", move=True)
    _call(ctx, "set_instance_params", lib="l", cell="blk", instance="x1", params={"VPWR": "vpi"})
    gate = librelane.gate_netlist(prj, flow, ed.model())
    missing = [m["message"] for m in librelane.requirements(flow, lib, ed.model(), prj.path, gate)]
    assert any(m.startswith("x2.B is on the supply vdd: a run from the schematic inserts no tie cell") and
               "sky130_fd_sc_hd__conb_1" in m for m in missing), missing
    assert any(m.startswith("net nd has no driver (only inputs: x3.D)") for m in missing), missing
    assert any(m.startswith("a port for each supply of blk's standard cells (vpi") for m in missing), missing
    assert len(missing) == 3, missing                   # x2.Y, which now drives nothing, is left open: no refusal
    shutil.rmtree(root)


def test_a_flow_written_in_a_batch_that_is_not_kept_leaves_no_file():
    import test_changeset as tc
    root, srv, cs = tc.setup()
    lib = srv.project("p").library("l").path
    (lib / "counter.sv").write_text("module counter (input clk, output q);\nendmodule\n")
    ctx = T.ToolCtx(srv, "p", actor="agent:t")
    assert not T.call(ctx, "open_changeset", {"title": "flow", "reason": "r"})[1]
    work = srv.project(ctx.work).path
    config = {"top": {"lib": "l", "cell": "counter"}, "clock": {"port": "clk", "period_ns": 20}}
    out = _call(ctx, "batch", ops=[{"tool": "write_flow", "args": {"flow": "counter", "config": config}}], dry_run=True)
    assert out["dry_run"] and not (work / "flows" / "counter.yaml").exists()
    res, err = T.call(ctx, "batch", {"reason": "x", "ops": [
        {"tool": "write_flow", "args": {"flow": "c2", "config": config}},
        {"tool": "write_flow", "args": {"flow": "c3", "config": {**config, "target": "make"}}}]})
    assert err and "taken back" in res and not (work / "flows" / "c2.yaml").exists()
    assert not [o for lc in cs.get("p", ctx.changeset)["changes"] for o in lc["ops"]]
    # a restore that fails puts the flow back as it was
    _call(ctx, "write_flow", flow="counter", config=config)
    text = (work / "flows" / "counter.yaml").read_text()

    def broken(*a, **k):
        raise RuntimeError("broken on purpose")
    cs._drop_made = broken
    with pytest.raises(Exception, match="nothing was changed"):
        cs.restore("p", ctx.changeset, 0, "user")
    assert (work / "flows" / "counter.yaml").read_text() == text
    shutil.rmtree(root)


def test_what_a_run_did_to_a_netlist_with_drawn_physical_cells_and_its_signoff(tmp_path):
    drawn = {"x1": "sky130_fd_sc_hd__inv_1", "xd": "sky130_fd_sc_hd__diode_2", "xc": "sky130_fd_sc_hd__decap_4"}
    same = gatelevel.compare(drawn, dict(drawn))
    assert same["kept"] and same["removed"] == [] and same["physical"] == {}
    gone = gatelevel.compare(drawn, {"x1": "sky130_fd_sc_hd__inv_1", "FILLER_1": "sky130_fd_sc_hd__decap_4"})
    assert not gone["kept"] and gone["removed"] == ["xc (sky130_fd_sc_hd__decap_4)", "xd (sky130_fd_sc_hd__diode_2)"]
    assert gone["physical"] == {"sky130_fd_sc_hd__decap_4": 1}
    # a finished run from a schematic: the drawn netlist kept, the signoff violations and the generic SDC said
    run = tmp_path / "run"
    (run / "src").mkdir(parents=True)
    (run / "src" / "blk.v").write_text("module blk (input a, output y);\n  sky130_fd_sc_hd__inv_1 x1 (.A(a), .Y(y));\n"
                                       "  sky130_fd_sc_hd__diode_2 xd (.DIODE(a));\nendmodule\n")
    final = run / "runs" / librelane.TAG / "final"
    (final / "nl").mkdir(parents=True)
    (final / "nl" / "blk.nl.v").write_text("module blk (a, y);\n sky130_fd_sc_hd__inv_1 x1 (.A(a), .Y(y));\n"
                                           " sky130_fd_sc_hd__diode_2 xd (.DIODE(a));\n"
                                           " sky130_fd_sc_hd__tapvpwrvgnd_1 TAP_1 ();\nendmodule\n")
    metrics = {"design__max_slew_violation__count": 34, "design__max_fanout_violation__count": 4,
               "design__max_cap_violation__count": 0, "timing__setup__ws": 57.0,
               "design__max_slew_violation__count__corner:nom_ss_100C_1v60": 34,
               "design__max_slew_violation__count__corner:nom_tt_025C_1v80": 0,
               "design__max_fanout_violation__count__corner:nom_tt_025C_1v80": 4}
    (final / "metrics.json").write_text(json.dumps(metrics))
    res = librelane.results(run)
    assert res["key_metrics"]["max slew violations"] == 34 and res["key_metrics"]["max fanout violations"] == 4
    s = librelane.summary(run, {"source": "schematic", "cell": "blk", "sdc": False, "ignored": ["side"]}, res, [])
    assert s["netlist"]["kept"] and s["netlist"]["physical"] == {"sky130_fd_sc_hd__tapvpwrvgnd_1": 1}
    assert s["signoff"] == {"violations": {"max slew": 34, "max fanout": 4},
                            "corners": {"max slew": ["nom_ss_100C_1v60"], "max fanout": ["nom_tt_025C_1v80"]}}
    assert any(h.startswith("the run ends with 34 max slew, 4 max fanout violations: the drawn netlist is kept")
               for h in s["hints"]), s["hints"]
    assert any("generic SDC" in h for h in s["hints"]) and s["ignored"] == ["side"]
    assert "signoff" not in librelane.summary(run, {"source": "rtl"}, {"metrics": {}}, [])


@needs_sky
def test_standard_cells_the_lvs_reference_cannot_take_are_refused_clearly():
    # a tie cell between two ports: one net in the layout, two pins in the schematic
    with pytest.raises(ValueError, match="joins the ports one and vdd"):
        stdcells.reference(".subckt blk a y one vdd vss\nxt vss vss vdd vdd one net1 sky130_fd_sc_hd__conb_1\n"
                           "x1 a vss vss vdd vdd y sky130_fd_sc_hd__inv_1\n.ends\n", SKY)
    # physical cells the schematic draws leave the reference, as their devices leave the layout
    text, info = stdcells.reference(".subckt blk a d vdd vss\nx1 a vss vss vdd vdd y sky130_fd_sc_hd__inv_1\n"
                                    "xd d vss vss vdd vdd sky130_fd_sc_hd__diode_2\n"
                                    "xc vss vss vdd vdd sky130_ef_sc_hd__decap_12\n.ends\n", SKY)
    assert info["physical"] == ["xd", "xc"] and "diode" not in text and "decap" not in text
    assert info["cells"] == ["sky130_fd_sc_hd__inv_1"]
    # a cell in a file of its own (sky130_ef_sc_hd__*) is found there; one the PDK has no SPICE of is named
    d, where = stdcells._definition(SKY, "sky130_ef_sc_hd__decap_12", "sky130_fd_sc_hd")
    assert d is not None and where[0].name == "sky130_ef_sc_hd__decap_12.spice"
    with pytest.raises(ValueError, match="no SPICE of the standard cells sky130_fd_sc_hd__nosuch_1"):
        stdcells.reference(".subckt blk a\nx1 a sky130_fd_sc_hd__nosuch_1\n.ends\n", SKY)
    with pytest.raises(ValueError, match="sky130_fd_sc_hd__macro_sparecell: LVS cannot read"):
        stdcells.reference(".subckt blk a b c d e f\nx1 a b c d e f sky130_fd_sc_hd__macro_sparecell\n.ends\n", SKY)


@needs_sky
@pytest.mark.skipif(not tools.find("klayout"), reason="KLayout is not installed")
def test_klayout_lvs_of_a_row_of_standard_cells_matches_its_schematic():
    """tap, inv_1, conb_1, decap_3, diode_2, tap abutted in a row: the rails join, inv_1 is a / y, the tie cell's HI is
    an inner net (vdd in the layout), the diode on its own net d; the schematic draws them all."""
    from undine.checks import verify
    from undine.server.api import Server
    root = Path(tempfile.mkdtemp())
    srv = Server(root / "projects", {SKY.id: SKY}, [root])
    srv.store.create("p", SKY.id).create_library("l")
    gds = kdb.Layout()
    gds.read(str(SKY.root / "libs.ref/sky130_fd_sc_hd/gds/sky130_fd_sc_hd.gds"))
    ly = kdb.Layout()
    ly.dbu = gds.dbu
    top = ly.create_cell("blk")
    x, at = 0.0, {}
    for name in ("tapvpwrvgnd_1", "inv_1", "conb_1", "decap_3", "diode_2", "tapvpwrvgnd_1"):
        full = f"sky130_fd_sc_hd__{name}"
        c = ly.cell(full)
        if c is None:
            c = ly.create_cell(full)
            c.copy_tree(gds.cell(full))
        top.insert(kdb.DCellInstArray(c.cell_index(), kdb.DTrans(x, 0)))
        at.setdefault(name, x)
        x += round((gds.cell(full).dbbox().width() - 0.38) / 0.46) * 0.46          # the cell's sites
    li, m1 = ly.layer(67, 5), ly.layer(68, 5)
    for t, layer, px, py in (("a", li, at["inv_1"] + 0.445, 1.19), ("y", li, at["inv_1"] + 0.905, 1.53),
                             ("d", li, at["diode_2"] + 0.69, 1.53), ("vdd", m1, 0.23, 2.72), ("vss", m1, 0.23, 0)):
        top.shapes(layer).insert(kdb.DText(t, px, py))
    ly.write(str(root / "blk.gds"))
    srv.db("p", "l").import_oas(root / "blk.gds")
    sch = (".subckt blk a y d vdd vss\nxt vss vss vdd vdd hi lo sky130_fd_sc_hd__conb_1\n"
           "x1 a vss vss vdd vdd y sky130_fd_sc_hd__inv_1\nxc vss vss vdd vdd sky130_fd_sc_hd__decap_3\n"
           "xd d vss vss vdd vdd sky130_fd_sc_hd__diode_2\nxtap vdd vss sky130_fd_sc_hd__tapvpwrvgnd_1\n.ends\n")
    text, info = stdcells.reference(sch, SKY)
    assert info["ties"] == ["xt"] and info["physical"] == ["xc", "xd", "xtap"]
    job = verify.start_lvs(srv.jobs, srv.project("p").path, SKY, srv.db("p", "l").layout, "blk", "l", text,
                           options={"schematic_simplify": True})
    t0 = time.time()
    while job.status()["state"] in ("queued", "running") and time.time() - t0 < 300:
        time.sleep(0.5)
    st = job.status()
    assert st["state"] == "done" and st["summary"]["match"], (st.get("summary"), job.log(30))
    shutil.rmtree(root)


@needs_sky
@pytest.mark.skipif(not tools.find("verilator"), reason="Verilator is not installed")
def test_rtl_that_defines_a_standard_cell_itself_reads_no_models():
    root, srv, ctx = _logic()
    stub = ("module sky130_fd_sc_hd__inv_1 (input A, output Y);\n  assign Y = ~A;\nendmodule\n"
            "module top (input a, output y);\n  sky130_fd_sc_hd__inv_1 u (.A(a), .Y(y));\nendmodule\n")
    _call(ctx, "write_rtl", lib="l", cell="top", text=stub)
    out = _call(ctx, "lint_rtl", lib="l", cell="top")
    assert out["ok"] and "gate_level" not in out, out
    assert not [m for m in out["messages"] if m["code"] in ("MODDUP", "TIMESCALEMOD")], out["messages"]
    # with a schematic cell too, the models come in: their warnings of themselves (MODDUP) stay out of the lint
    stub2 = stub.replace("endmodule\n", "endmodule\n", 1).replace(
        "module top (input a, output y);\n", "module top (input a, input clk, output y, output q);\n  blk b (.a(a), .clk(clk), .q(q));\n")
    _call(ctx, "write_rtl", lib="l", cell="top", text=stub2)
    out = _call(ctx, "lint_rtl", lib="l", cell="top")
    assert out["gate_level"]["from_schematic"] == ["blk"], out
    assert not [m for m in out["messages"] if m["code"] in ("MODDUP", "TIMESCALEMOD")], out["messages"]
    shutil.rmtree(root)
