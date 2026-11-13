# RTL, Verilator, LibreLane

A cell's RTL is `<cell>.sv` (or `.v`) in its library. The file defines the module named like the cell; other modules
in it are fine. The library's other RTL files are sources of every lint and simulation.

## Read and write

- `get_rtl(lib, cell)` gives the file text, the module's ports, and the library's other RTL files.
- `write_rtl(lib, cell, text, reason)` writes the whole file in your changeset (creating it if needed) and returns
  Verilator's lint of the result. From the shell: `undine call write_rtl ... text=@counter.sv`.
- `create_cell(lib, cell, reason, views=["rtl"], rtl=...)` starts a new RTL cell.

## Lint and simulate

- `lint_rtl(lib, cell)` runs Verilator `--lint-only -Wall` with this cell as the top module.
  - A cell drawn from standard cells (a schematic, no RTL) is read as its gate-level netlist, with the PDK's
    functional cell models (zero delay). So is such a cell that a testbench instantiates (`run_rtl_sim`).
  - `export_verilog(lib, cell)` shows that netlist: power pins inside `` `ifdef USE_POWER_PINS ``, warnings such as
    an input tied straight to a supply.
- `run_rtl_sim(lib, cell)` simulates a **testbench cell** with Verilator (`--binary --timing`).
  - It returns what the testbench printed, its errors ($error, $fatal, failed assertions; any of them fails the run),
    where it finished, build warnings, and the last changes of the dumped signals.
  - A testbench is a cell too, for example `tb_counter`, with initial blocks, `$display`, `$error` / assertions and
    `$finish`. Add `$dumpfile` / `$dumpvars` for a waveform.
  - Check the cell's specs in the testbench.

## LibreLane (RTL to GDS)

- `list_flows()` gives the project's flow configurations, and whether LibreLane is available.
- `check_flow(flow)` says what is missing (the clock, port sides in the top cell's intent). When nothing is missing,
  it gives the derived LibreLane settings and where each comes from. `check_flow(config={...})` checks a
  configuration before you save it.
- `write_flow(flow, config, reason)` proposes a flow configuration (`flows/<flow>.yaml`) in your changeset. The person
  reviews and adopts it with the rest; `run_librelane` runs it in your working copy before that.
  `config`: `{top: {lib, cell}, source, clock: {port, period_ns}, floorplan: {die_area_um | core_util}, variables}`.
- `run_librelane(flow)` starts a run and returns a job id at once. Runs take minutes.
- `wait_for(events=["job:<id>"], timeout_s=300)` returns when the run ends, instead of polling.
- `get_librelane_result(job)` gives the state; when done, the key metrics (cells, area, timing slack and violations,
  DRC / LVS / antenna counts, power), the final GDS and errors.

Port sides come from the intent (`set_port_intent`). Set them rather than editing the flow's variables by hand.

**From a schematic of standard cells.** `source: schematic` builds a cell drawn from standard cells from its
schematic as drawn: LibreLane only reads its gate-level netlist (no synthesis), with no resizing, buffering, clock tree
or antenna diodes, every net and instance don't-touch. Supplies are the nets on the cells' VPWR / VGND, ports of the
schematic; they are the power grid's pins (a side in the intent is not used: `check_flow` lists it under `ignored`).
- `check_flow` refuses an input on a supply and an inner net with only inputs: LibreLane inserts no tie cell. Draw
  `sky130_fd_sc_hd__conb_1` and take the constant from HI / LO.
- `get_librelane_result` then says under `netlist` whether the run kept the drawn netlist (`kept`), and which logical
  cells it added, removed or changed; fill, tap, decap and diode cells are counted apart. Not kept: find the setting
  that changed it and set it in the flow's variables.
- `signoff` lists max slew / capacitance / fanout violations and their corners. Nothing repairs them in such a run:
  fix them in the schematic (a stronger driver, a buffer drawn). Without an SDC of the design (`PNR_SDC_FILE`), the
  timing is LibreLane's generic one: report it as such.
- Then `import_layout(lib, job)`, `run_checks` (LVS against the schematic reads the standard cells; tie cells'
  HI / LO nets are the supplies in LVS), `extract_parasitics` and `check_specs(extracted=[...])`.

**Caravel (ChipFoundry).** A flow with `caravel: {role: macro}` hardens a user-area macro. Its RTL needs `vccd1` and
`vssd1` inside `` `ifdef USE_POWER_PINS ``. A flow with `caravel: {role: wrapper, template, macros}` hardens
`user_project_wrapper`, with each macro from the latest successful run of its flow. So run the macro flows first.
`check_flow` names what is missing: the template's files, a macro run, or the macro's instance in the wrapper RTL.
