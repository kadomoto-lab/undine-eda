# RTL and simulation

A cell can have an **RTL** view: a Verilog / SystemVerilog file in its library, `<cell>.sv` (or `<cell>.v`), defining
the module named like the cell. Create it with **New cell** (check *RTL*), or open an existing `.sv` / `.v` file's cell
from the library browser (the `rtl` chip). The file starts from a module template.

## Editing

The RTL view is a code editor ([CodeMirror](https://codemirror.net/)) with Verilog / SystemVerilog highlighting
(keywords, types, numbers, strings, system tasks, macros, comments). It also has:

- line numbers, folding and bracket matching;
- search and replace: Ctrl+F (Ctrl+G for the next match, Ctrl+Alt+G to go to a line);
- Tab to indent by two spaces;
- Ctrl+Z / Ctrl+Y for edits that are not saved yet.

**Save** (or Ctrl+S) writes the file, and every save is recorded in the library's operation log. So Undo / Redo (the
arrows in the editor's header) and proposals work as for the other views: in a proposal the agent's RTL changes
appear in the review and are adopted or discarded with the rest. A file a proposal created stays when the proposal is
reverted, as created schematics do.

## Lint

**Lint** runs Verilator (`--lint-only -Wall`) with this cell as the top module. The library's other RTL files are
sources too, so a cell that instantiates another one resolves it. Errors and warnings are listed with file and line;
clicking one in this cell's file goes to its line and column. In the editor, problems are underlined where Verilator
points (coral for errors, sand for warnings), the gutter marks their lines, and hovering shows the message. Saving lints
again.

## Simulation

A testbench is an RTL cell too, for example `tb_counter`: it instantiates the design, drives it from `initial`
blocks, reports with `$display`, checks with `$error` / assertions, ends with `$finish`, and writes a waveform with
`$dumpfile` / `$dumpvars`. **Simulate** builds the testbench with Verilator (`--binary --timing --trace`) and runs it:

- the result is **Passed** when the run ends without an error; any `$error`, `$fatal`, failed assertion or build error
  makes it **Failed**, with the error lines listed;
- the testbench's output (`$display`), where it finished, and Verilator's build warnings are shown;
- the dumped signals are drawn as waveforms (vectors as their integer value; x / z as gaps).

Each run is a job in the project (`runs/<id>/`) with a copy of the sources it simulated, the log and `wave.vcd`.
The panel lists earlier runs of the chosen testbench.

### RTL from a dependency

Verilator turns `$c(...)` and `` `systemc_* `` blocks into C++ that runs in the simulation, `$system(...)` runs a
shell, a DPI import calls C, and `` `include ``, `$fopen`, `$dumpfile`, `$readmem*` and `$writemem*` read or write files. In a
[dependency](libraries.md#dependencies)'s library, Undine refuses these until a person
[trusts the dependency](libraries.md#trusting-a-dependencys-code): **Simulate** refuses a library with any of them
(and with `` `define `` bodies that build them, or a path that is absolute or has a `..` or `~`); **Lint**, which
compiles nothing, refuses only such `` `include `` paths. Every RTL file of the dependency is read, also in folders.
The refusal names each file and line:

```
analog_ip/rtl/ctrl.sv (dependency analog_ip 1.2.0, not trusted): line 12: initial $c("init_model();"); a person can trust analog_ip@1.2.0 in this project (undine lib trust demo analog_ip, or Trust... in Undine); an agent cannot
```

The panel shows it with **Trust...**, which opens the trust dialog; trusting runs the RTL as it is, so read what the
dialog lists first. Your own libraries' RTL is not checked.

### Gate-level netlists of standard-cell schematics

A cell drawn from the PDK's standard cells (sky130's `sky130_stdcells/*.sym`) and with no RTL is read by Lint and
Simulate as its **gate-level netlist**: one Verilog module named like the cell, its ports in the schematic's order and
one instance per standard cell, every pin connected by name. The same holds for such a cell that a testbench
instantiates. The PDK's Verilog models of the cells are read with it (sky130: `primitives.v` and
`sky130_fd_sc_hd.v`), as functional models without delay (`FUNCTIONAL`, `UNIT_DELAY` empty); their own warnings are
not reported. A standard cell that the RTL defines itself (a stub) is taken from the RTL, and when the RTL defines
every standard cell it uses, no models are read. The power pins are inside `` `ifdef USE_POWER_PINS ``, which these runs leave undefined, so a
testbench connects the logic ports only.

## LibreLane: RTL to GDS

The **LibreLane** tab builds the cell's layout from its RTL with [LibreLane](https://librelane.readthedocs.io/). A
**flow configuration** in the project (`flows/<name>.yaml`; *Create a flow* makes one for the cell) says how: the top
cell, the clock port and period (or *none* for a combinational design), the core utilisation, and any other LibreLane
variables, passed as they are. The cell's **design intent** supplies the rest: port sides become LibreLane's pin
order file (*let the tool decide* leaves a port to the placer), and supply / ground nets become `VDD_NETS` /
`GND_NETS`.

Before a run the tab lists what is missing (the clock, a side for each port) and the **derived settings**: every
setting the run passes to LibreLane with the flow or intent entries it comes from. They are derived again for each run
and kept with it (`config.json`, `pin_order.cfg`, `constraints.json`); change them in the flow or the intent, not in
the run. A run is a job (`runs/<id>/`): the panel shows LibreLane's log while it runs, then the key results (cells,
area, setup / hold slack and violations, DRC, LVS and antenna counts, max slew / capacitance / fanout violations,
power) and errors. **Import layout** merges the
final GDS into the cell's library, with the standard cells it uses.

### From a schematic of standard cells

A flow with `source: schematic` builds a cell drawn from standard cells from its schematic, with no RTL. The run
writes the schematic's gate-level netlist into `src/<cell>.v` and LibreLane only reads it: no synthesis, and every
cell stays as drawn (a delay chain, tie cells). The run derives these settings, each with `flow.source` in the
derived settings, and a flow variable overrides any of them:

| Setting | Why |
| --- | --- |
| `SYNTH_ELABORATE_ONLY: true` | Yosys reads the netlist and maps nothing |
| `RSZ_DONT_TOUCH_RX: ".*"` | every net and instance is don't-touch for the resizer |
| `RUN_POST_GPL_DESIGN_REPAIR`, `RUN_POST_GRT_DESIGN_REPAIR: false` | no buffers on long nets or ports, no resizing |
| `RUN_CTS: false` | no clock tree: the clocks are wired as drawn |
| `RUN_POST_CTS_RESIZER_TIMING`, `RUN_POST_GRT_RESIZER_TIMING: false` | no timing repair |
| `DIODE_ON_PORTS: none`, `RUN_HEURISTIC_DIODE_INSERTION: false`, `RUN_ANTENNA_REPAIR: false` | no antenna diodes |
| `DRT_ANTENNA_REPAIR_JUMPER_ONLY: true` | detailed routing fixes antennas by layer jumps only |

The supplies are the nets on the cells' VPWR / VGND parameters (`vdd`, `vss`), which must be ports of the
schematic. They become `VDD_NETS` / `GND_NETS` together with the supplies in the intent, if it lists any. Their pins
are the power grid's straps, not pins on one edge: a side that the intent gives a supply port is not used, and the
check lists it under *Not used by the run*.

Before a run starts, the check refuses what the layout would leave unconnected. LibreLane only reads the netlist and
inserts no tie cell, so an input pin drawn straight on a supply, or an inner net with only inputs on it, would have
no driver. Draw a tie cell (sky130: `sky130_fd_sc_hd__conb_1`, HI for 1, LO for 0) and take the constant from it.

When a run ends, its result compares the final netlist with the drawn one: the logical cells added, removed or
changed (none when the netlist was kept), and apart from them the fill, tap, decap and diode cells the layout added.
A decap or a diode that the schematic draws is compared like any other cell. The result also lists the max slew,
max capacitance and max fanout violations at signoff, with the corners that have them. With no buffering and no
resizing, they are the schematic's to fix: a stronger driver, or a buffer drawn for a net with many loads. Timing is
checked with LibreLane's generic SDC unless the flow gives its own (`PNR_SDC_FILE`, `SIGNOFF_SDC_FILE`); for a
self-timed or asynchronous design, that slack says little.

For a cell that a flow builds from its schematic, the LibreLane panel is on the cell page, below the waterline
(*Show the flow*). In the review, a flow configuration opens that page.

```yaml
# flows/sar_logic.yaml
top: {lib: sar, cell: sar_logic}
source: schematic
clock: {port: clk, period_ns: 195.3}
floorplan: {die_area_um: [0, 0, 60, 60]}
variables: {RT_MAX_LAYER: met4, FP_PDN_MULTILAYER: false, FP_PDN_VPITCH: 25, FP_PDN_VOFFSET: 5, FP_PDN_VWIDTH: 1.6,
            PL_TARGET_DENSITY_PCT: 60}
```

Once imported, the layout is checked against the schematic like any other (see
[Verification](verification.md), "Standard cells in LVS").

LibreLane comes from Nix, its supported installation: install Nix with LibreLane's binary cache, clone LibreLane to
`~/librelane`, and run `nix-shell --run "librelane --smoke-test"` there once (the [LibreLane installation
guide](https://librelane.readthedocs.io/en/latest/installation/nix_installation/index.html)). Undine then runs it with
`nix-shell ~/librelane --run …`; `UNDINE_LIBRELANE` sets another command. Runs use the project's PDK as it is
installed (`--manual-pdk`).

The agent reads flows (`list_flows`, `check_flow`), starts runs (`run_librelane`) and reads their results
(`get_librelane_result`). It proposes a flow configuration with `write_flow` in its changeset: the file is part of
the proposal, the review shows it (view *flow*, under the top cell), and it reaches the project when you adopt the
proposal. `check_flow` also checks a configuration that is not saved yet (`config`). `export_verilog` shows the
gate-level netlist of a standard-cell schematic.

### Caravel: macros into user_project_wrapper

For ChipFoundry's Caravel (SKY130), a design is hardened in two flows: the macro, then the wrapper that places it in
the 2920 x 3520 um user area.

```yaml
# flows/counter.yaml: the macro
top: {lib: dig, cell: counter}
clock: {port: clk, period_ns: 25}
floorplan: {die_area_um: [0, 0, 150, 150]}
caravel: {role: macro}

# flows/wrapper.yaml: the wrapper
top: {lib: dig, cell: user_project_wrapper}
caravel:
  role: wrapper
  template: ~/caravel_user_project
  macros:
    - {flow: counter, instance: mprj, location: [1400, 1700], orientation: N}
```

**The macro (`role: macro`).**

- It gets the user area's power pins: `vccd1` / `vssd1`, declared in the RTL inside `` `ifdef USE_POWER_PINS ``.
  These pins need no side.
- Its power grid has no top-level stripes, and routing stops at met4, so the wrapper's grid reaches it on met4 / met5.

**The wrapper (`role: wrapper`).**

- It takes the outline, pins, power rings and stripes, and timing constraints from a checkout of
  [chipfoundry/caravel_user_project](https://github.com/chipfoundry/caravel_user_project). Undine does not ship it;
  `openlane/user_project_wrapper` and `verilog/rtl` are all it reads.
- Its RTL is the `user_project_wrapper` cell's rtl view. Start from the template's `verilog/rtl/user_project_wrapper.v`
  and instantiate your macro under the instance name the flow gives it.
- Each macro comes from the latest successful run of its flow. The run copies the macro's views into
  `macros/<flow>/` and connects its `vccd1` / `vssd1` to the wrapper's power grid (`power` names other nets).
- The wrapper has no standard-cell rows, so detailed routing fixes antennas with jumpers only, not diode cells.

Undine's own build of a counter macro into the wrapper passes LibreLane's Magic / KLayout DRC and LVS, and the
**ChipFoundry** target's layout checks (see [Tapeout](tapeout.md)).

## The agent

The agent reads and writes RTL in its proposals: `get_rtl`, `write_rtl` (the whole file; the result includes the lint),
`lint_rtl`, and `run_rtl_sim` (the testbench's output, errors, finish and the last changes of each dumped signal), and
`create_cell` with views `[rtl]`. As for schematics, it works from the cell's design intent: an RTL cell's intent refers
to the ports of its module. A dependency's RTL that the trust rule refuses is refused for the agent too; it reports
the refusal and asks you, since only a person can trust a dependency.

## Installing Verilator

`pixi install` adds Verilator (conda-forge) to Undine's environment. Verilator builds the simulation with a C++
compiler and `make` (the system's, or the environment's).
