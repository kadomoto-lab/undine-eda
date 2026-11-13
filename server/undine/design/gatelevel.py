"""A schematic of standard cells as a gate-level (structural) Verilog netlist (docs/guide/rtl.md, "From a schematic").

A cell drawn from the PDK's standard cells (sky130_stdcells/nand2_1.sym and the like, with their supply pins as the
VPWR / VGND / VPB / VNB parameters) is written as one Verilog module of the same name: its ports in the schematic's
order, one wire per inner net, one instance per standard cell with every pin connected by name. The power pins are
inside `ifdef USE_POWER_PINS`, as in the PDK's own powered netlists: LibreLane reads the module without them and
connects the supplies itself, Verilator with -DUSE_POWER_PINS and the PDK's models sees them.

    module sar_logic (
    `ifdef USE_POWER_PINS
        inout vdd,
        inout vss,
    `endif
        input clk,
        output clkc
    );
        wire op;
        sky130_fd_sc_hd__buf_2 xbop (
    `ifdef USE_POWER_PINS
            .VGND(vss), .VNB(vss), .VPB(vdd), .VPWR(vdd),
    `endif
            .A(outp), .X(op));
    endmodule

The netlist is the schematic's, not a synthesis of it: nothing is added, removed or resized here. A run that takes
it as its input (librelane.py, flow source "schematic") keeps it so. What cannot be written is refused with the reason
(an instance that is not a standard cell of the PDK, a symbol that is missing); what can but may not be meant is a
warning: an input pin on a supply (a constant the schematic should take from a tie cell; written as 1'b1 / 1'b0), a
pin alone on its net (an output left open is written unconnected), an inner net with no output on it. A LibreLane run
from the schematic refuses the first and the last (librelane.requirements): it only elaborates the netlist, inserts no
tie cell, and the layout would leave those inputs unconnected.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from undine.pdks.pdkprofile import PHYSICAL_CELLS
from undine.schematics import netlist as netlistmod, schematic, xschem

POWER_DEFINE = "USE_POWER_PINS"
# the supply pins of a standard cell that carry the supply (1) or the ground (0)
HIGH_PINS = {"VPWR", "VPB", "VDD", "VNW"}
LOW_PINS = {"VGND", "VNB", "VSS", "VPW"}
PORT_DIRS = {"ipin": "input", "opin": "output", "iopin": "inout"}
_SIMPLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
KEYWORDS = set("""
always and assign automatic begin buf bufif0 bufif1 case casex casez cell cmos config deassign default defparam design
disable edge else end endcase endconfig endfunction endgenerate endmodule endprimitive endspecify endtable endtask event
for force forever fork function generate genvar highz0 highz1 if ifnone incdir include initial inout input instance
integer join large liblist library localparam macromodule medium module nand negedge nmos nor noshowcancelled not
notif0 notif1 or output parameter pmos posedge primitive pull0 pull1 pulldown pullup pulsestyle_onevent
pulsestyle_ondetect rcmos real realtime reg release repeat rnmos rpmos rtran rtranif0 rtranif1 scalared
showcancelled signed small specify specparam strong0 strong1 supply0 supply1 table task time tran tranif0 tranif1 tri
tri0 tri1 triand trior trireg unsigned use uwire vectored wait wand weak0 weak1 while wire wor xnor xor
logic bit byte int shortint longint var type typedef struct union enum interface endinterface package endpackage
import export class endclass program endprogram property endproperty sequence endsequence assert assume cover
always_comb always_ff always_latch unique priority final alias bind chandle const context extern forkjoin global
iff inside intersect join_any join_none local matches modport new null packed protected pure rand randc randcase
randsequence ref return shortreal solve static string super tagged this throughout timeprecision timeunit
virtual void wait_order wildcard with within do foreach break continue clocking endclocking
""".split())


def ident(name: str) -> str:
    """A net, port or instance name as a Verilog identifier: as it is when it is a simple one, else escaped."""
    return name if _SIMPLE.match(name) and name not in KEYWORDS else f"\\{name} "


def cell_name(dev: schematic.Device, props: dict) -> str | None:
    """The cell an instance netlists as, if it may be a standard cell (layouts.sdl's reading of the PDK's symbols)."""
    from undine.layouts.sdl import _stdcell_name
    return _stdcell_name(dev, props)


def export(sch: Path, resolver: xschem.SymbolResolver, cell: str, is_stdcell, supplies: dict[str, str] | None = None) -> dict:
    """The gate-level Verilog of schematic `sch` (module `cell`).

    `is_stdcell(name)`: whether a cell name is one of the PDK's standard cells (Profile.stdcell). `supplies`: the cell
    intent's {net: supply | ground}; a net that only supply parameters name is a supply too, its role from the pins.
    Returns {"module", "text", "cells": {cell: count}, "instances", "ports": [(name, direction)], "power_ports",
    "supplies": {net: supply | ground}, "supply_nets": [the nets on the cells' supply pins], "constants": [{pin, net,
    value}: an input on a supply], "undriven": [{net, pins}: an inner net only inputs are on], "warnings"};
    ValueError lists what keeps the schematic from being written."""
    conn = netlistmod.connectivity(sch, resolver, cell, write=False)
    problems = [w for w in conn.warnings if w.startswith("missing symbol")]
    insts = []                          # (name, cell, {pin: net}, {power pin: net})
    power_role: dict[str, str] = dict(supplies or {})
    for dev in conn.devices:
        props = schematic.inst_props(dev.comp, dev.sym)
        name = cell_name(dev, props)
        if not name or not is_stdcell(name):
            problems.append(f"{dev.name} ({dev.comp.sym}) is not a standard cell of the PDK")
            continue
        power = netlistmod.supply_params(dev.sym, dev.comp.props)
        for pin, net in power.items():
            if net not in power_role and pin.upper() in HIGH_PINS | LOW_PINS:
                power_role[net] = "supply" if pin.upper() in HIGH_PINS else "ground"
        insts.append((dev.name, name, dict(dev.nets), power))
    if problems:
        raise ValueError(f"{cell} cannot be written as a gate-level netlist: " + "; ".join(problems))
    if not insts:
        raise ValueError(f"{cell} has no standard cells")

    warnings: list[str] = []
    ports = [(n, PORT_DIRS.get(t, "inout")) for n, t in conn.ports]
    port_names = {n for n, _ in ports}
    power_nets = set(power_role)
    signal_ports = [(n, d) for n, d in ports if n not in power_nets]
    power_ports = [n for n, _ in ports if n in power_nets]
    pins_on: Counter = Counter(net for _, _, pins, _ in insts for net in pins.values())
    sym_dirs = {}
    for dev in conn.devices:
        if dev.sym is not None:
            sym_dirs[dev.name] = {p.name: p.dir for p in schematic.sym_pins(dev.sym)}

    wires = sorted({net for _, _, pins, _ in insts for net in pins.values()
                    if net not in port_names and net not in power_nets and pins_on[net] > 1}, key=_natural)
    # an inner net is driven when a pin on it is an output (or a pin of no known direction)
    readers: dict[str, list[str]] = {}
    driven: set[str] = set()
    for name, _, pins, _ in insts:
        for pin, net in pins.items():
            if (sym_dirs.get(name) or {}).get(pin) == "in":
                readers.setdefault(net, []).append(f"{name}.{pin}")
            else:
                driven.add(net)
    undriven = [{"net": n, "pins": readers[n]} for n in sorted(readers, key=_natural)
                if n not in driven and n not in port_names and n not in power_nets]
    constants = []
    body = []
    for name, cname, pins, power in insts:
        conns = []
        for pin, net in pins.items():
            if net in power_nets:
                const = "1'b1" if power_role[net] == "supply" else "1'b0"
                warnings.append(f"{name}.{pin} is on the {power_role[net]} {net}: written as {const} (a tie cell "
                                "gives a constant input its own driver)")
                constants.append({"pin": f"{name}.{pin}", "net": net, "value": const})
                conns.append(f".{pin}({const})")
            elif net not in port_names and pins_on[net] <= 1:
                if (sym_dirs.get(name) or {}).get(pin) == "out":
                    conns.append(f".{pin}()")          # an output nothing reads: left open
                else:
                    warnings.append(f"{name}.{pin} is the only pin on net {net}")
                    conns.append(f".{pin}({ident(net)})")
                    wires.append(net)
            else:
                conns.append(f".{pin}({ident(net)})")
        pwr = [f".{p}({ident(n)})" for p, n in sorted(power.items())]
        lines = [f"    {cname} {ident(name)} ("]
        if pwr:                                        # a cell of supply pins alone (decap, tap): no comma after them
            lines += [f"`ifdef {POWER_DEFINE}", "        " + ", ".join(pwr) + ("," if conns else ""), "`endif"]
        lines.append("        " + ", ".join(conns) + ");")
        body.append("\n".join(lines))
    for u in undriven:
        if len(u["pins"]) > 1:
            warnings.append(f"net {u['net']} has no driver (only inputs: {', '.join(u['pins'])})")
    if ident(cell) != cell:
        warnings.append(f"the cell's name {cell} is no plain Verilog identifier (a keyword?): the module is \\{cell}, "
                        "which a flow's DESIGN_NAME does not match")
    unused_ports = [n for n, _ in signal_ports if pins_on[n] == 0]
    if unused_ports:
        warnings.append(f"ports on no standard cell: {', '.join(unused_ports)}")

    head = [f"// {cell}: the gate-level netlist of its schematic, written by Undine; every standard cell as drawn",
            "`timescale 1ns / 1ps", f"module {ident(cell)} ("]
    if power_ports:
        head.append(f"`ifdef {POWER_DEFINE}")
        decl = [f"    inout {ident(n)}" for n in power_ports]
        head += [d + "," for d in decl[:-1]] + [decl[-1] + ("," if signal_ports else "")]
        head.append("`endif")
    decl = [f"    {d} {ident(n)}" for n, d in signal_ports]
    head += [d + "," for d in decl[:-1]] + decl[-1:]
    head.append(");")
    text = "\n".join(head + [f"    wire {ident(w)};" for w in dict.fromkeys(wires)] + body + ["endmodule", ""])
    return {"module": cell, "text": text, "cells": dict(sorted(Counter(c for _, c, _, _ in insts).items())),
            "instances": len(insts), "ports": signal_ports, "power_ports": power_ports,
            "supplies": {n: power_role[n] for n in sorted(power_nets)},
            "supply_nets": sorted({n for _, _, _, power in insts for n in power.values()}, key=_natural),
            "constants": constants, "undriven": undriven, "warnings": list(dict.fromkeys(warnings))}


def _natural(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


# ------------------------------------------------------------------ the netlist a run came back with


_INST = re.compile(r"^\s*([A-Za-z_][\w$]*)\s+(?:#\s*\(.*?\)\s*)?(\\\S+|[A-Za-z_][\w$\[\].]*)\s*\(", re.M | re.S)
# the cells a place-and-route run adds that do nothing logical: wells / rails / fill / spare capacitance, antenna diodes
PHYSICAL = re.compile(PHYSICAL_CELLS, re.I)


def instances(text: str) -> dict[str, str]:
    """{instance: cell} of the standard cells in a Verilog netlist (a run's final .nl.v)."""
    src = re.sub(r"//[^\n]*|/\*.*?\*/", " ", text, flags=re.S)
    out = {}
    for m in _INST.finditer(src):
        cell, inst = m.group(1), m.group(2).strip()
        if cell in ("module", "assign", "wire", "input", "output", "inout", "reg") or "__" not in cell:
            continue
        out[inst.lstrip("\\")] = cell
    return out


def physical(cell: str) -> bool:
    """A cell a run adds for the layout alone (fill, tap, decap, antenna diode)."""
    return bool(PHYSICAL.search(cell))


def compare(drawn: dict[str, str], built: dict[str, str]) -> dict:
    """What a run did to a schematic's netlist: the logical cells it added, removed or changed (the physical ones it
    adds are counted apart; those the schematic draws, an antenna diode or a decap, are compared as the others). {"kept": bool, "added", "removed", "changed", "physical": {cell: count}}."""
    logical = {i: c for i, c in built.items() if not physical(c) or i in drawn}   # a diode the schematic draws is its
    added = sorted(i for i in logical if i not in drawn)
    removed = sorted(i for i in drawn if i not in logical)
    changed = sorted(i for i in drawn if i in logical and logical[i] != drawn[i])
    return {"kept": not (added or removed or changed),
            "added": [f"{i} ({logical[i]})" for i in added][:50], "removed": [f"{i} ({drawn[i]})" for i in removed][:50],
            "changed": [f"{i}: {drawn[i]} -> {logical[i]}" for i in changed][:50],
            "counts": {"drawn": len(drawn), "logical": len(logical), "added": len(added), "removed": len(removed),
                       "changed": len(changed)},
            "physical": dict(sorted(Counter(c for i, c in built.items() if i not in logical).items()))}


def of_cell(prj, lib: str, cell: str, supplies: dict[str, str] | None = None) -> dict:
    """export() of a project cell's schematic with the project's PDK, or {"error": why not}."""
    try:
        sch = prj.library(lib).path / f"{cell}.sch"
    except KeyError as e:
        return {"error": str(e).strip("'\"")}
    if not sch.is_file():
        return {"error": f"{lib}/{cell} has no schematic"}
    if prj.pdk is None or not prj.pdk.profile.stdcells:
        return {"error": "the project's PDK has no standard cells"}
    try:
        out = export(sch, xschem.SymbolResolver(prj.symbol_paths()), cell,
                     lambda n: prj.pdk.profile.stdcell(n) is not None, supplies)
    except (ValueError, KeyError) as e:
        return {"error": str(e).strip("'\"")}
    return {**out, "tie_cell": prj.pdk.profile.stdcell_tie}


# ------------------------------------------------------------------ Verilator: a gate-level netlist with the cells' models


_CALL = re.compile(r"\b([A-Za-z_][\w$]*)\s+(?:#\s*\(.*?\)\s*)?\\?[A-Za-z_][\w$\[\].]*\s*\(", re.S)


def rtl_inputs(prj, lib: str, top: str) -> dict:
    """What a Verilator lint or simulation of `top` in library `lib` reads besides the library's RTL files: the
    gate-level netlists of the library's cells that have a schematic and no RTL and that the RTL instantiates (`top`
    itself when it is one), and the PDK's Verilog models of the standard cells any of them instantiates, with the
    defines they are read with (Profile.stdcell_verilog). A standard cell the RTL defines itself (a stub) is read from
    the RTL: no models for it, and none at all when the RTL defines every one it uses. {"generated": {file: text},
    "models": [Path], "defines": [...], "cells": {cell: from}}; ValueError when a netlist that is needed cannot be
    written, or the PDK has no models of a standard cell that is instantiated."""
    from undine.design import intent, rtl
    lib_dir = prj.library(lib).path
    files = rtl.sources(lib_dir)
    texts = [re.sub(r"//[^\n]*|/\*.*?\*/", " ", f.read_text(errors="replace"), flags=re.S) for f in files]
    defined = {m for t in texts for m in rtl.modules(t)}
    out: dict = {"generated": {}, "models": [], "defines": [], "cells": {}}
    wanted = [] if top in defined or rtl.find_file(lib_dir, top) else [top]
    for sch in sorted(lib_dir.glob("*.sch")) if lib_dir.is_dir() else []:
        cell = sch.stem
        if cell in defined or cell in wanted:
            continue
        if any(re.search(rf"\b{re.escape(cell)}\s*(#\s*\(.*?\)\s*)?[A-Za-z_]\w*\s*\(", t, re.S) for t in texts):
            wanted.append(cell)
    for cell in wanted:
        if not (lib_dir / f"{cell}.sch").is_file():
            continue
        res = of_cell(prj, lib, cell, intent.load(intent.path_of(lib_dir, cell))["supplies"])
        if res.get("error") and cell == top:
            raise KeyError(f"no RTL for {top}, and its schematic is no gate-level netlist: {res['error']}")
        if res.get("error"):
            raise ValueError(res["error"])
        out["generated"][f"{cell}.v"] = res["text"]
        out["cells"][cell] = "schematic"
        texts.append(res["text"])
    pdk = prj.pdk
    used = sorted({m.group(1) for t in texts for m in _CALL.finditer(t)
                   if m.group(1) not in defined and pdk.profile.stdcell(m.group(1))}) if pdk is not None else []
    if used:
        if not pdk.profile.stdcell_verilog:
            raise ValueError(f"{pdk.name} has no Verilog models of its standard cells here ({', '.join(used[:5])} ...)")
        for lib_name in sorted({pdk.profile.stdcell(c)[0] for c in used}):
            for rel in pdk.profile.stdcell_verilog:
                f = pdk.root / rel.format(lib=lib_name)
                if not f.is_file():
                    raise ValueError(f"the PDK has no {f.name} ({f})")
                if f not in out["models"]:
                    out["models"].append(f)
        out["defines"] = list(pdk.profile.stdcell_verilog_defines)
        out["stdcells"] = len(used)
    return out
