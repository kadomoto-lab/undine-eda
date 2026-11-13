"""store/trust.py's scanners: what in a dependency's netlist text, RTL and Xschem files would run as code or read a
file nobody scanned. Pure: the texts are given, scan_dir reads a temporary directory, and the one Xschem run (when
Xschem is installed) netlists a schematic of the test's own."""
import subprocess
from pathlib import Path

import pytest

from undine import tools
from undine.store import trust
from undine.store.trust import Finding


def kinds(found):
    return [(f.kind, f.line) for f in found]


PDK = {"PDK_ROOT": "/pdk"}


def sim(text, allowed=(Path("/pdk"),), env=PDK, rules="sim"):
    return trust.spice_findings(text, allowed=list(allowed), env=env, rules=rules)


# ------------------------------------------------------------------ SPICE


def test_control_blocks_and_control_lines_with_their_lines():
    text = "\n".join([
        "R1 a b 1k",                    # 1
        ".control",                     # 2: no shell, still code
        "run",
        ".endc",
        ".subckt inner a b",            # 5
        "R2 a b 2k",
        ".control",                     # 7: inside a subcircuit
        "shell rm -rf somewhere",
        ".endc",
        ".ends",
        "*# shell ls",                  # 11
        "* an ordinary comment",
    ])
    assert kinds(sim(text)) == [("control", 2), ("control", 7), ("control_line", 11)]
    assert sim(text)[1].text == ".control"
    assert sim(text, rules="paths") == []


def test_case_and_leading_blanks_are_ignored():
    text = "  .CONTROL\n  shell ls\n  .EndC\n   *# echo\n\t.INCLUDE /etc/hostname\n"
    assert kinds(sim(text)) == [("control", 1), ("control_line", 4), ("path", 5)]


def test_continuation_lines_join_an_include_and_unparsed_cards_do_not_stop_the_scan():
    text = "\n".join([
        ".include",                     # 1: the file is on the next line
        "* a comment between",
        "+ /etc/passwd",
        ".inc",                         # 4: no file at all
        ".lib /etc/hostname tt",        # 5: still scanned
    ])
    found = sim(text)
    assert kinds(found) == [("path", 1), ("unparsed", 4), ("path", 5)]
    assert found[0].path == "/etc/passwd" and found[0].text == ".include /etc/passwd"
    assert found[2].path == "/etc/hostname"


def test_paths_after_substitution():
    ok = ".lib $PDK_ROOT/sky130A/libs.tech/combined/sky130.lib.spice tt\n.include ${PDK_ROOT}/x.spice\n" \
         ".include models/here.spice\n.include \"$::PDK_ROOT/quoted name.spice\"\n"
    assert sim(ok) == []
    bad = "\n".join([
        ".include $PDK_ROOT/../../x",   # absolute, out of the PDK once resolved
        ".include /etc/hostname",
        ".include ~/x",
        ".include ../up.spice",         # relative, with ..
        ".include $VAR/x",              # a variable the netlister does not know
    ])
    found = sim(bad)
    assert kinds(found) == [("path", n) for n in range(1, 6)]
    assert [f.path for f in found] == ["$PDK_ROOT/../../x", "/etc/hostname", "~/x", "../up.spice", "$VAR/x"]
    # no PDK root given: even the PDK's files are reported (scan_dir's listing)
    assert kinds(sim(ok, allowed=(), env={})) == [("path", 1), ("path", 2), ("path", 4)]


def test_an_include_into_a_dependency_install_is_refused(tmp_path):
    install = tmp_path / "prj" / ".undine" / "deps" / "ip"
    install.mkdir(parents=True)
    (install / "models.spice").write_text("* nothing scanned here\n")
    found = sim(f".include {install / 'models.spice'}\n", allowed=(tmp_path / "pdk",))
    assert kinds(found) == [("path", 1)]


def test_xspice_file_parameters():
    text = "\n".join([
        "a1 %v(in) %v(out) src",
        ".model src filesource(file=\"/etc/passwd\" amploffset=[0])",
        ".model rom d_source(input_file=../../secret.txt)",
        ".model st d_state(state_file=$PDK_ROOT/states.txt)",
        ".model ok d_source(input_file=vectors.txt)",
    ])
    found = sim(text)
    assert kinds(found) == [("path", 2), ("path", 3)]
    assert [f.path for f in found] == ["/etc/passwd", "../../secret.txt"]
    assert kinds(sim(text, rules="paths")) == [("path", 2), ("path", 3)]


# ------------------------------------------------------------------ RTL


def test_rtl_findings():
    text = "\n".join([
        "module m;",                                            # 1
        "  initial $c(\"system(\\\"ls\\\");\");",              # 2
        "`systemc_header",                                      # 3
        "  initial $system(\"ls\");",                           # 4
        "  import \"DPI-C\" function int f();",                 # 5
        "`include \"/etc/abs.vh\"",                             # 6
        "`define RUN $c(\"x\")",                                # 7: a define holding $c (and $c itself)
        "`define C(x) $``x",                                    # 8: pasting builds $c
        "  localparam W = $clog2(8);",                          # 9: a system function, not code
        "  initial $readmemh(\"../../data.hex\", mem);",       # 10
        "  initial fd = $fopen(name);",                         # 11: a name nobody can tell
        "  initial $readmemh(\"data.hex\", mem);",             # 12: fine
        "`include \"ok.vh\"",                                   # 13: fine
        "`include `FILE",                                       # 14
        "endmodule",
    ])
    found = trust.rtl_findings(text)
    assert kinds(found) == [("rtl_c", 2), ("systemc", 3), ("rtl_system", 4), ("dpi", 5), ("rtl_path", 6),
                            ("rtl_define", 7), ("rtl_c", 7), ("rtl_define", 8), ("rtl_path", 10), ("rtl_path", 11),
                            ("rtl_path", 14)]
    assert found[4].path == "/etc/abs.vh" and found[4].text == '`include "/etc/abs.vh"'
    assert [(f.kind, f.line) for f in trust.rtl_findings(text, full=False)] == [("rtl_path", 6), ("rtl_path", 14)]


def test_rtl_in_comments_is_not_found():
    text = "\n".join([
        "// initial $c(\"x\"); `systemc_header $system(\"ls\")",
        "/* import \"DPI-C\" function int f();",
        "   `include \"/etc/abs.vh\" */",
        "initial $display(\"// $system( in a string is not a comment\");",
        "`include \"~/x.vh\" // a comment after",
    ])
    assert kinds(trust.rtl_findings(text)) == [("rtl_system", 4), ("rtl_path", 5)]


def test_rtl_multiline_define_and_include_forms():
    text = "`define BIG(x) \\\n  begin \\\n    $system(x); \\\n  end\n`include <../sys.vh>\n"
    assert kinds(trust.rtl_findings(text)) == [("rtl_define", 1), ("rtl_system", 3), ("rtl_path", 5)]


# ------------------------------------------------------------------ Tcl


SYM = """v {xschem version=3.4.5 file_version=1.2}
K {type=resistor
format="@name @pinlist tcleval([exec ls]@value)"
template="name=R1 value=1k"
alt="expr([exec ls]+1)"
safe="expr(1+2)"}
B 5 -2.5 -32.5 2.5 -27.5 {name=P dir=inout}
"""


def test_tcl_findings_in_a_symbol():
    found = trust.tcl_findings(SYM)
    assert kinds(found) == [("tcl", 3), ("tcl", 5)]
    assert found[0].text.startswith("format=")
    assert kinds(trust.tcl_findings('value="expr($::x)" b="expr(2*(3+1))"')) == [("tcl", 1)]


def test_xschem_evaluates_tcleval_when_it_netlists(tmp_path):
    """The reason for the Tcl rule: `xschem -n` runs tcleval( in an attribute. Xschem 3.4.5 leaves expr( as text;
    tcl_findings reports it all the same, as later versions evaluate expr( attributes."""
    exe = tools.find("xschem")
    if not exe:
        pytest.skip("xschem not installed")
    (tmp_path / "r.sym").write_text('v {xschem version=3.4.5 file_version=1.2}\nK {type=resistor\n'
                                    'format="@name @pinlist @value"\ntemplate="name=R1 value=1k"}\n'
                                    "B 5 -2.5 -32.5 2.5 -27.5 {name=P dir=inout}\n"
                                    "B 5 -2.5 27.5 2.5 32.5 {name=M dir=inout}\n")
    sch = (f'v {{xschem version=3.4.5 file_version=1.2}}\n'
           f'C {{r.sym}} 0 0 0 0 {{name=R1 value="tcleval([file mkdir {tmp_path}/by_tcleval]1k)"}}\n'
           f'C {{r.sym}} 100 0 0 0 {{name=R2 value="expr([file mkdir {tmp_path}/by_expr]+2)"}}\n')
    (tmp_path / "top.sch").write_text(sch)
    (tmp_path / "rc").write_text(f"set XSCHEM_LIBRARY_PATH {tmp_path}\n")
    (tmp_path / "out").mkdir()
    subprocess.run([exe, "-n", "-q", "--no_x", "--rcfile", str(tmp_path / "rc"), "-o", str(tmp_path / "out"),
                    str(tmp_path / "top.sch")], capture_output=True, timeout=120, env=tools.env(), cwd=tmp_path)
    assert (tmp_path / "by_tcleval").is_dir()
    assert kinds(trust.tcl_findings(sch)) == [("tcl", 2), ("tcl", 3)]


# ------------------------------------------------------------------ a library


def test_scan_dir_lists_cells_rtl_and_tcl(tmp_path):
    lib = tmp_path / "ip"
    (lib / "rtl" / "sub").mkdir(parents=True)
    (lib / "res.sym").write_text('v {xschem version=3.4.5 file_version=1.2}\nK {type=resistor\n'
                                 'format="@name @pinlist @value"\ntemplate="name=R1 value=1k"}\n'
                                 "B 5 -2.5 -32.5 2.5 -27.5 {name=P dir=inout}\n")
    (lib / "top.sch").write_text('v {xschem version=3.4.5 file_version=1.2}\n'
                                 'C {ip/res.sym} 0 0 0 0 {name=R1 value="1k\n.control\nshell ls\n.endc"}\n'
                                 'C {ip/res.sym} 100 0 0 0 {name=R2 value=2k}\n')
    (lib / "top.netlist.yaml").write_text(
        "version: 1\nports: []\ninstances:\n  R1:\n    symbol: ip/res.sym\n    props: {value: '1k'}\n"
        "code:\n  - {name: s1, symbol: devices/code_shown.sym, props: {value: \".include /etc/hostname\"}}\n")
    (lib / "clean.sch").write_text('v {xschem version=3.4.5 file_version=1.2}\n'
                                   'C {ip/res.sym} 0 0 0 0 {name=R1 value=1k}\n')
    (lib / "pdk.sym").write_text('v {xschem version=3.4.5 file_version=1.2}\nK {type=primitive\n'
                                 'format="tcleval(@name [exec ls])"\ntemplate="name=x1"}\n')
    (lib / "rtl" / "sub" / "core.sv").write_text("module core;\n  initial $system(\"ls\");\nendmodule\n")
    (lib / "rtl" / "ok.v").write_text("module ok; endmodule\n")
    (lib / "notes.txt").write_text(".control\nshell ls\n.endc\n")      # not a view: not read
    scan = trust.scan_dir(lib)

    assert set(scan["cells"]) == {"top"}
    top = scan["cells"]["top"]
    assert [(f.kind, f.where) for f in top] == [("path", "top.netlist.yaml code s1 value"),
                                               ("control", "top.sch R1 value")]
    assert top[0].path == "/etc/hostname"
    assert list(scan["rtl"]) == ["rtl/sub/core.sv"]
    assert kinds(scan["rtl"]["rtl/sub/core.sv"]) == [("rtl_system", 2)]
    assert list(scan["tcl"]) == ["pdk.sym"] and kinds(scan["tcl"]["pdk.sym"]) == [("tcl", 3)]
    assert trust.counts(scan) == {"spice": 2, "rtl": 1, "tcl": 1}


def test_scan_dir_reports_pdk_paths_and_what_it_cannot_read(tmp_path):
    # a template's default value is a card of its own (\\" in a file is \" in the value)
    (tmp_path / "corner.sym").write_text('v {xschem version=3.4.5 file_version=1.2}\nK {type=netlist_commands\n'
                                         'format="@value"\ntemplate="value=\\\\".lib $PDK_ROOT/x.lib tt\\\\""}\n')
    (tmp_path / "broken.netlist.yaml").write_text("instances: [unclosed\n")
    scan = trust.scan_dir(tmp_path)
    assert [(f.kind, f.where) for f in scan["cells"]["corner"]] == [("path", "corner.sym template value")]
    assert scan["cells"]["corner"][0].path == "$PDK_ROOT/x.lib"
    assert [(f.kind, f.where) for f in scan["cells"]["broken"]] == [("unparsed", "broken.netlist.yaml")]
    assert trust.counts({"cells": {}, "rtl": {}, "tcl": {}}) == {"spice": 0, "rtl": 0, "tcl": 0}


def test_finding_is_a_plain_record():
    f = Finding("path", 3, ".include /x", "/x")
    assert (f.kind, f.line, f.text, f.path, f.where) == ("path", 3, ".include /x", "/x", None)


def test_control_is_matched_by_prefix_as_ngspice_does():
    """ngspice opens a control block on any card that starts with .control (.controlx, .control;) and closes it on
    one that starts with .endc."""
    for card in (".controlx", ".control;", ".CONTROLS", ".control*", ".control,"):
        assert kinds(sim(f"R1 a 0 1k\n{card}\nshell id\n.endc")) == [("control", 2)], card
    # the block ends at an .endc variant: the next .control is a block of its own
    assert kinds(sim(".control\nshell id\n.endcx\nR1 a 0 1k\n.control;\n.endc")) == [("control", 1), ("control", 5)]
    assert kinds(sim(".controlx\nshell id\n.endc", rules="paths")) == []


def test_rtl_system_functions_named_without_a_call_and_dpi_built_by_a_macro():
    """A macro argument becomes the call once Verilator expands it: $system and $c count wherever they appear."""
    text = '`define CALL(f, x) f(x)\nmodule tb;\n  initial `CALL($system, "touch x");\nendmodule\n'
    assert ("rtl_system", 3) in kinds(trust.rtl_findings(text))
    text = '`define CALL(f, x) f(x)\nmodule tb;\n  initial `CALL($c, "x");\nendmodule\n'
    assert ("rtl_c", 3) in kinds(trust.rtl_findings(text))
    text = '`define IMP import\nmodule tb;\n  `IMP "DP" + "I-C" function int system(string s);\nendmodule\n'
    assert ("rtl_define", 1) in kinds(trust.rtl_findings(text))
    assert kinds(trust.rtl_findings("module m;\n  initial $display($clog2(8), $countones(3));\nendmodule\n")) == []


def test_rtl_dumpfile_is_a_file_task():
    """--trace is on in a simulation build, so $dumpfile writes (and truncates) the file it names."""
    found = trust.rtl_findings('module tb;\n  initial $dumpfile("/abs/wave.vcd");\nendmodule\n')
    assert [(f.kind, f.line, f.path) for f in found] == [("rtl_path", 2, "/abs/wave.vcd")]
    assert kinds(trust.rtl_findings("module tb;\n  initial $dumpfile(name);\nendmodule\n")) == [("rtl_path", 2)]
    assert kinds(trust.rtl_findings('module tb;\n  initial begin $dumpfile("wave.vcd"); $dumpvars; end\nendmodule\n')) \
        == []
    assert kinds(trust.rtl_findings('module tb;\n  initial $dumpports(tb, "/abs/p.evcd");\nendmodule\n')) == \
        [("rtl_path", 2)]
    assert kinds(trust.rtl_findings('module tb;\n  initial $dumpports(tb, "p.evcd");\nendmodule\n')) == []


def test_tcleval_escaped_with_backslashes_is_found():
    """Xschem drops a value's backslash escapes before it looks for tcleval(."""
    for word in ("tcl\\eval(", "tc\\leval(", "t\\\\cleval("):
        text = f'v {{xschem version=3.4.5 file_version=1.2}}\nC {{r.sym}} 0 0 0 0 {{name=R1 value="{word}[exec id]1k)"}}\n'
        assert kinds(trust.tcl_findings(text)) == [("tcl", 2)], word


def test_check_tcl_refuses_an_escaped_tcleval_in_an_untrusted_dependency(tmp_path):
    d = tmp_path / "p" / ".undine" / "deps" / "ip"
    d.mkdir(parents=True)
    (d / "inv.sch").write_text('v {xschem version=3.4.5 file_version=1.2}\n'
                               'C {r.sym} 0 0 0 0 {name=R1 value="tcl\\eval([exec id]1k)"}\n')
    with pytest.raises(trust.UntrustedContent) as e:
        trust.check_tcl({d: ("ip", "ip@unknown")})
    assert [(f, x.kind) for f, x in e.value.findings] == [("ip/inv.sch", "tcl")]
