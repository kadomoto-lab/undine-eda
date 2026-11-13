# Verification: DRC, LVS, parasitic extraction, simulation

Results are jobs with provenance. Each records what it was run on, so Undine marks it *outdated* when the design
changes after it.

## DRC and LVS

- `run_checks(fresh?)` runs the checks on the cells your changeset changed, on both the baseline and your working
  copy, and waits for the results. Compare the two: report what your change fixed or broke. Each row has its `kind`,
  its `checker`, its `job` and when it `finished`. `reused: true` means a job that had already run on exactly these
  inputs (the checks are kept by their inputs); `fresh: true` runs every check anew, for an independent run. PCell
  variants Undine generated that nothing places any more get no rows (alone they are no design: Magic's LU.2 would
  fail them); `skipped_unused_cells` counts them, and `delete_unused_cells` takes them out of the changeset.
  - **DRC**: the PDK's KLayout deck, and on sky130, GF180MCU and IHP also Magic's `drc(full)` (`checker: magic`),
    as shuttle prechecks run it. The KLayout deck has no MV diff / tap rules (diff/tap.14-17) and checks the MIM spacings
    differently (`capm.11`, `met3.3c`): a 5 V PCell's 0.45 um butted tap is clean there and `diff/tap.16` in Magic.
    A layout is DRC clean when both are. Without Magic installed, its rows fail and say so.
  - **LVS** for a cell with a schematic and a layout, and with it, on sky130, GF180MCU and IHP, an **ERC** row
    (`kind: erc`). The ERC rows are warnings, not violations: a body tap butted against a source / drain of another
    net, from the LVS run's connectivity. That is an n+ / p+ junction against the tap with nothing between: its
    capacitance and leakage on that net, and its reverse voltage, are in no model and in no extraction (on GF180MCU,
    silicided, a short its LVS deck does not see; IHP's LVS reports it as a short itself). Each finding names the
    instance, the terminal, its net and the tap's net (`XMS D (vout) against the vss tap`). Fix it by routing the
    body net to the butted side, or by giving the device a tap that does not abut it.
- **Verifying another agent's changeset** (as a reviewer or second opinion): pass `changeset: "cs-..."` to
  `run_checks`, `check_specs`, `sweep`, `run_simulation`, `extract_parasitics`, `get_parasitics`, `get_checks`,
  `get_drc_report`, `get_lvs_report`, `get_layout_info`, `get_device_geometry`, `render_layout`, `wait_for` and the
  other reads. They work on that changeset's working copy, take no write lock, and leave your own changeset (if any)
  yours (also if your session opened it earlier); the answer starts with `changeset` (a text answer with the line
  `changeset: cs-...`). Pass `changeset` to the follow-ups an answer names too (`wait_for`, `get_parasitics`,
  `run_simulation job=`); `batch` does not take another's changeset. What you run there is recorded in it as its
  writer's runs are (the `run_checks` rows, the extraction and simulation jobs in its working copy), so its writer
  sees them. Nothing of it is edited: a write tool refuses `changeset`, and `cancel_job` stops only your own jobs
  there. The writer may go on editing meanwhile: a check made before an edit becomes outdated, so read `outdated`
  before you report.
- `get_checks(lib, cell)` gives the latest result of each checker and the ERC, with their jobs; an outdated one says
  what changed since. It, `get_drc_report`, `get_lvs_report`, `render_layout`'s markers and `get_device_geometry`'s
  `lvs` read the latest run among all of the project's runs, however many simulations a sweep ran after it.
- `get_drc_report(lib, cell, rule?, checker?, limit?)` says where the violations are, from the latest DRC run of each
  checker. For each rule it gives the description, the count and the markers in um. A marker is a `box`, the two
  `edges` a spacing or width rule measured, or a `polygon`, in the cell's coordinates (a violation inside a PCell
  variant is where that cell is placed). Magic merges a violation's error tiles into one marker.
  `render_layout(lib, cell, markers: true)` (or a list of rules) draws them over the layout, one colour per rule.
- `get_lvs_report(lib, cell)` gives what does not match (devices, pins, nets), the extracted netlist and the reference
  netlist. Read it to find why LVS fails. On a mismatch, `why` says it in counts and names: devices by class in the
  layout and the schematic, devices and pins without a counterpart, layout nets with one connection, or that KLayout
  kept no circuit of the layout (its devices not wired to each other and no pins: an unrouted placement).
  A schematic port connected to nothing inside the cell (an interface pin the cell does not use, such as a CDAC's
  `vdd`) passes when its label is in the layout's top cell, on a pin label layer and on a shape of its metal:
  `unused_pins` lists it (the deck purges the net, so the layout has no pin for it); without the label `pin_errors`
  says the label is missing. Draw the label where the port leaves the cell, on metal of its own: a label of that name
  on a net with devices is an error (it would make that net the port).
- A failing `kind: schematic` row of `run_checks` comes with the baseline's row: `unchanged_from_baseline` means the
  design had it before the changeset (say so; it is not yours), `new_in_work` lists what the changeset added.
- **MIM capacitors (sky130)**: the reference netlist has `cap_mim_m3_1` / `cap_mim_m3_2` as the capacitor the deck
  extracts (`sky130_fd_pr__model__cap_mim` / `_m4`), with `A = W * L` and `P = 2 * (W + L)` per unit and `m = MF`.
  LVS compares their total area and perimeter, and the nets. Draw as many W x L units as `MF` and join their plates.
- `check_device_integrity(lib, cell, instance?)` finds shapes that reach into a device's transistor area and change
  the extracted W / L.
- `get_layout_info(lib, cell)`'s `schematic_sync.wiring` (sky130) checks the drawn routing between the placed devices
  without a job: the nets still open, the shorts, and `butted_taps`. A tap butted against a source / drain on another
  net is a short through the silicide that neither LVS nor PEX sees. Fix it with a guard ring
  (`sync_layout(layout={dev: {"bulk": "ring"}})`) or by routing the bulk net's terminal to the tap side.
  - The substrate and the n-wells join nothing in this check. `substrate_only` names the open nets that only the
    substrate joins, such as a `vss` without its rail; `well_only` those that only an n-well joins. LVS matches them
    anyway, because the deck joins every p-tap to its substrate net and every n-tap to its well. A body without a tap
    (bulk `none`) next to a tap of its bulk net (within the latch-up distance, in the same substrate region or well) is
    tied: `body_ties` names the tap.
- The KLayout deck is not the whole sky130 rule set: Magic's capm.11 (1.34 um from a capm to another plate's metal3)
  and diff/tap.16 (a 0.7 um thick-oxide butted tap) are not in it. `sync_layout` and `place_array` keep to both.
- `render_layout(lib, cell, markers: true)` draws the latest DRC runs' markers on the layout, a colour per rule;
  `highlight_nets` shows what a net reaches. `get_device_geometry(lib, cell, instance)` gives each terminal's net in
  the layout now and in the latest LVS (`lvs`: layout and schematic net), and flags a source / drain butted against
  the body tap on another net than the bulk.

## Parasitic extraction: after LVS

The order is DRC and LVS clean first, then extraction, then the specs on the extracted view. An extraction of a layout
that does not match its schematic simulates the wrong circuit.

1. `extract_parasitics(lib, cell, mode)`. Mode `c` gives each net's capacitance to the substrate and to each other
   net (coupling), one node per net: enough for capacitive loading and coupling (a CDAC's top plate, a comparator's
   inputs, a clock next to a sampling node). Mode `rc` adds the wiring's resistance, a resistor network per net; use it
   where the wires are long or narrow (supplies, clock and reference lines, the switches' paths into a CDAC). Mode `rc`
   keeps every coupling capacitor between the two nets. Magic's own resistance flow counts it a second time to ground,
   and Undine does not use that part of it. Every net with a port or a source / drain gets its network unless its
   largest resistance is below `res_threshold_ohm` (default 0.1 ohm); `res_nets: [...]` limits the networks to those
   nets (a large block; a name in any case). `res_options_not_applied` means this Magic (before 8.3.653) has no
   minres / mindelay, so `res_threshold_ohm` was not used.
2. Read the summary, a diagnosis:
   - `c_per_net`: total capacitance per net, largest first (to ground, coupling);
   - `coupling`: the largest net-to-net capacitors (a signal next to a clock is a kickback or crosstalk path);
   - `r_per_net` (rc): the sum of each net's resistors and `r_max_path_ohm`, the largest resistance from its pin;
   - `single_node` (rc): the nets left one node, each with why (below `res_threshold_ohm`, not in `res_nets`, no port
     or source / drain to start from such as a well, the substrate as ground 0);
   - `devices`: by model, to compare with the schematic;
   - `ports.match` with `missing` (schematic pins without a label in the layout), `shorted` (two schematic pins whose
     labels are on one net) and `extra_labels` (labels that only name inner nets);
   - `floating_nets`: capacitors only (a victim wire, dummy metal); each gets 1 TΩ to the substrate;
   - `names`: where the inner nets' names come from (below);
   - `mim_shielding`: what a MIM's top plate shields that Magic counted (below);
   - `warning`: the layout's LVS does not match its schematic (or has not been run): the extraction is for diagnosis;
   - `runtime_s`: how long the extraction took (to extrapolate to a larger array).
   Long lists are cut with a count (`*_more`). `get_parasitics(lib, cell, net?)` reads it again, only one net's rows
   with `net` (a net it does not have is an error that names the nearest ones). It always says `outdated: true / false` (when true, what changed in `changed`) and `made_from`: the
   layout's hash and when it was extracted.
3. Simulate post-layout: `run_simulation`, `check_specs` or `sweep` with `extracted: ["lib/cell", ...]` (each cell's
   newest up-to-date extraction; `{"lib/cell": "rc"}` picks a mode, `{"lib/cell": "<job>"}` one job). Everything else
   works as before: corners, temperatures, `.meas`, `spectrum`. Compare with the same specs on the schematic: what the
   layout costs (delay, settling, SNDR) is the difference. Report both. Such a run stores the testbench's nets and
   what you measure, not the nodes inside the extracted cells: name one in `signals` (e.g. `x1.out.n0`) to see it.

**Net names.** An extraction keeps the schematic's net names. When an LVS run of the same layout matched them
(`run_checks` before the extraction), every net without a label is labelled with its schematic name at one of its
contacts before Magic extracts it (`names.labelled`). Otherwise Magic names it after a point on it (`a_3109_5296#`),
and `names` says why (`from_lvs: null`: run `run_checks`, then extract again). `names.unnamed` lists nets LVS matched
that have no contact to label. So a testbench's probes of inner nets (`v(x1.g)`) work on the extracted view
without labels drawn in the layout. With the same LVS run, Magic's devices (`X0`, `X1` ...) take the names of the
schematic's elements as the simulation netlist writes them (`XXM5`), so device quantities
(`@m.x1.xxm5.msky130_fd_pr__nfet_g5v0d10v5[l]`) work too. A device Magic splits (fingers, m, a MIM's MF) is the
element and then `_2`, `_3` ... (a quantity on it is that piece's); `names.devices_kept` lists devices that kept
Magic's name. A run that names a node, or a device quantity, inside an extracted cell that the extraction does not
have is refused, and the error lists them. ngspice would otherwise add each node as a new, floating one (the
operating point fails with a convergence message), and evaluate nothing for a device.

**MIM capacitors.** Magic's sky130 tech orders the MIM capacitor's plane above every metal, so the top plate shields
nothing: Magic counts the metal above a capacitor (a strap, the contact pad) as coupling to the bottom plate inside
it, by area and by the fringe of its edges, which the device's own capacitance already includes. The extraction runs
Magic a second time with the capacitor between its plates (the top plate takes that coupling: none for its own metal,
a coupling to its net for another net's metal) and uses its coupling between nets (the capacitance to the substrate
stays the first run's). `mim_shielding.removed_ff` is the coupling taken out in all (`overlap_ff` by area, `fringe_ff`
the rest); `pairs` gives per pair of plate nets what Magic gave (`magic_ff`), what is kept (`kept_ff`: the coupling
outside the capacitors) and its change (`removed_ff`, net of coupling moved onto it from another bottom plate's metal:
`moved_to_plates_ff`); `moved` other pairs that changed. A 1.17 um unit: 191 aF
from Magic, 102 aF kept. What is kept is real coupling outside the capacitor: compare edge and inner units, and keep
it in the specs' budget rather than rescaling a spec.

The extraction is outdated when the layout or the schematic's ports change. A simulation then refuses it and says to
run `extract_parasitics` again. It also refuses an extraction whose `ports.match` is false: label the missing pins on
their metal (`add_label`), separate shorted pins, then run `run_checks` and the extraction again. A sweep cannot vary a property inside a cell
simulated as extracted: its devices come from the layout.

## ngspice simulation

`run_simulation(lib, cell, analysis?, signals?, output?, input?, measure?, spectrum?, sample?, corner?, temp?,
options?, substitute?, save_all?)` runs a **testbench** schematic: one that places the cell and drives it, with sources and no
ports of its own. It runs in your working copy when a changeset is open.

- **Models.** The PDK models are included when the testbench has none, at the typical corner. To choose a corner,
  pass `corner` (e.g. `"ss"`) and `temp` (°C): the run is netlisted at that corner and temperature without editing the
  testbench, also with the PDK's corner symbol placed (sky130: `sky130_fd_pr/corner.sym`; GF180MCU has none).
  `check_specs` / `sweep` take `corners` and `temps` lists. IHP SG13G2's models are Verilog-A compiled to OSDI: when
  the simulation says they are not compiled, ask the person to run `./setup.sh osdi`. Each run reads the model
  library as a copy reduced to the devices its netlist uses, with the same results in a fraction of the time; you do
  nothing for it (a job that read the whole library says why in the first line of its log).
- **Standard cells.** Place the PDK's own symbols (`sky130_stdcells/nand2_1.sym`; their supplies VPWR / VGND / VPB /
  VNB are properties naming nets): their SPICE (`sky130_fd_sc_hd.spice`) is included like the models, and corners
  still work. Do not add an `.include` for it.
- **`options`**: simulator options for this run, e.g. `{reltol: 1e-6, maxstep: "0.2n"}` (`reltol`, `abstol`,
  `vntol`, `chgtol`, `trtol`, `gmin`, `method`, `maxord`, `itl1`, `itl4`, `maxstep`), to check that a figure is not
  the simulator's numerical floor. `maxstep` is the transient's `tmax`.
- **What a transient stores.** The testbench's nets, what its `.meas` / `.control` lines name, and what this call
  names (`signals`, `output`, `input`, `spectrum`, `sample`, measures; `check_specs` / `sweep`: what the specs read).
  Not the nodes inside its cells: a mixed-signal transient of every node is hundreds of megabytes, and the changeset
  keeps every run. To look inside a cell later (`plot_waveform`, `sample_waveform`, `job=`), name the node in
  `signals` when you run it (`x1.xcmp.outp`). A missing one comes back with `missing_hint`: run again naming it.
  `save_all: true` stores every node; use it only for one debugging run.
- **op** gives every node voltage and each transistor's bias (id, vgs, vds, vth, vdsat, gm, gds, region,
  vds_minus_vdsat), with the devices not in saturation.
- **ac** with `output` (and `input`) gives dc_gain_db, f_3db_hz, unity_gain_freq_hz, phase_margin_deg, gain_margin_db,
  and gain / phase per decade.
- **tran / dc** give, per signal, the initial, final, min and max values, the steepest slope, and a sampled waveform.
  `tran` also takes `tstart` (results from then on), `tmax` (the largest time step) and `uic: true`.
- **`.meas` results** are returned as `measurements`. To measure without changing the testbench, pass
  `measure` with `.meas` statements for this run only, for example a propagation delay:
  `".meas tran tphl TRIG v(in) VAL=0.9 RISE=1 TARG v(out) VAL=0.9 FALL=1"`, or an AC figure at a frequency:
  `".meas ac cin FIND vm(in) AT=10Meg"`. The sampled waveform is coarse; time differences (delays, rise times) come
  from `.meas`, not from the samples. A `.meas` that gave no value is in `measurement_errors` with ngspice's reason
  and the statement: check it, a run can be `done` with failed measures. `v(a,b)` is not a vector in `.meas`: use
  `par('v(a)-v(b)')` (any number of them) or `spectrum`. A `.meas` whose level is another measurement
  (`WHEN v(out)='vf-1m'`) is refused: ngspice evaluates it before measuring and stops the whole run.
- **Measures read off the run** (in `measure` too, returned in `measures`): a settling time
  `"settle(v(top), tol=0.44m, from=12n)"` (how long after `from` the signal enters ±`tol` around its final value and
  stays there; `to`, `target` optional), a derived measure over `.meas` results and other measures
  (`"max(t1, t2)"`, `"-iavg*1.8"`), or a name such as `"v(out).max"`. One that cannot be read says why
  (`measure_errors`, with ngspice's reason when a `.meas` it uses failed); the others still count.
- **`sample`** (tran) returns signals at full precision, as rows `[time, value, ...]`: at `times: [...]`, or at
  `start + k / fs` for k < `points` (the times `spectrum` samples). Use it to check a figure yourself (your own FFT
  of 512 samples near 100 dB, a code), instead of many `.meas` lines printed to 6 digits. Values between the
  simulator's time points are interpolated linearly, as `spectrum` does (`on_time_points` counts those that fall on
  one). The times lie between the transient's `tstart` (if given) and `stop`. `sample_waveform(lib, cell, signals,
  ...)` reads a finished run the same way without running it again (the latest, or `job`: any run's id, such as one
  `check_specs` or `sweep` names).
- **`spectrum`** (tran only) measures an ADC or a sample-and-hold: SNDR, SNR, THD, SFDR, ENOB and the level in dBFS.
  See below.
- **`analysis`** replaces the testbench's own `.control` blocks and analysis lines for that run. **Without `analysis`**
  the testbench's own `.control` block runs. It needs no `set num_threads`: every run uses one thread.
- **On failure** you get the error lines and what they usually mean. A model hint names the transistor, its W / L / nf
  and model bin only when its model stopped the run (a warning alone is harmless). A run whose analysis stopped short
  (`Timestep too small`, a model's `Fatal:`) has failed, even with a partial raw file. A run that finished can still
  have `warnings`: error lines of its log, such as an earlier analysis of a loop that aborted.
- **Two cells of one name.** A netlist has one subcircuit per name. When the testbench reaches two cells of one name
  from different libraries that netlist to other lines (a project's `inv` and a dependency's `inv`, or a subcell named
  like the top cell), netlisting is refused, naming both (`two cells named inv are netlisted, a/inv and b/inv ...`):
  simulation, LVS, specs and sweeps alike. Rename the project's own cell in a changeset, or place the same cell in
  both places; a dependency's or the PDK's cell is read-only.
- **Dependencies nobody has trusted.** A dependency's cell or file that would run code or read files is refused until
  a person trusts that dependency in the project: a `.control` block or a `*#` line in its netlist, an `.include` /
  `.lib` / model file that is absolute outside the PDK or holds `..`; in its RTL, Verilator's `$c`, `` `systemc_* ``,
  `$system`, DPI, or such a file path (`run_rtl_sim`, `lint_rtl`); Tcl in a symbol when netlisting with the Xschem
  backend. LVS refuses only the file paths. The refusal names the cell, the line and what it found
  (`ip/inv (dependency analog_ip 1.2.0, not trusted): code block s1 line 1: .control; ...`). You cannot trust a
  dependency, and nothing you do can: tell the person, who trusts it with the Trust... button in Undine or
  `undine lib trust PROJECT NAME`. The project's own cells and the PDK's run as before.

### Long runs

The server runs a few simulations at once (one ngspice thread each); the others wait their turn in order.

- `run_simulation` waits `wait_s` (default 150 s). A run still going then is not an error: the answer has its `job`,
  `state` (`queued` with `queue_position`, or `running` with `elapsed_s`) and what to do next.
- Follow it with `wait_for(events=["job:<id>"], timeout_s=1800)`, then `run_simulation(lib, cell, job="<id>")` for
  its results (with any `signals`, `output`, `spectrum`, `sample`, and measures read off the run). For a run you know
  is long, pass `wait_s: 0` and do other work.
- A run stops at its time limit (default 1 h): pass `time_limit_s` (also to `check_specs` / `sweep`) for a long
  transient, e.g. several ADC conversions on ~2000 transistors.
- `cancel_job(job)` stops a run of yours that is outdated (you changed the testbench): the machine is shared.
- A server restart (to deploy a change) loses no simulation: ngspice runs on and the next server takes the run up
  (and the queued ones, in order), under the same job id. A wait that the restart cut short answers at once
  (`wait_for`: `"stopping": true`); wait again (its note says when the runs were stopped with the server instead).
  Only a run no server can finish (its ngspice killed or crashed) is `interrupted`, with why: run it again. Checks
  are not taken up: a DRC, LVS, ERC or extraction ends with the server as `interrupted` (`wait_for` lists it in
  `ended`), and none starts while the server stops; `run_checks` / `extract_parasitics` run it again once the server
  is back (both answer `"stopping": true` at once when the server stops, as `wait_for` does). A
  `wait_for job:<id>` that says `no job` names the changeset whose working copy has it: `open_changeset id=...`
  continues it.

### Stand-ins: a behavioural model in place of a cell

A top level places its real cells (layout, LVS and extraction need them), but a long transient of it may only be
practical with a model of a slow block. For example, an ADC's SAR controller is ~1700 transistors of standard cells,
and its 256-sample FFT run is ~50 µs of transient. `substitute: {"adc/sar_logic": "adc/sar_logic_ideal"}` (on
`run_simulation`, `check_specs`, `sweep`) netlists every instance of the first cell, at any depth below the
testbench, as the second. The design is not edited.

- **Same pins, same order.** Both cells must exist, and the stand-in must have the original's pins in the same order:
  their symbols, or a schematic's ports when one has no symbol. Otherwise the run is refused, and the error names the
  pins (`in another order: pin 2 is vss, not out`). Also refused: a stand-in that contains the cell it replaces (at
  any depth; the error names the chain), a stand-in that is itself replaced (`{a: b, b: c}`), a stand-in with the
  name of another cell in the testbench (from another library: one subcircuit per name), and a cell both `extracted`
  and substituted.
- **Read the answer** (also of a run still going). `substitute` lists the stand-ins in effect; `notes` says where each
  comes from. `substitute_warnings` lists one that replaced nothing (the cell is not in the testbench, or only inside
  a cell simulated as extracted or as another stand-in), or a testbench intent that is not YAML, whose stand-ins were
  not read. Say in your report that a figure comes from a model.
- **Keep it** where a result must be reproducible. On a spec, `substitute` makes `check_specs` use it
  ([specs.md](specs.md)). On a testbench that is only practical with the model, its runs can use it by default: in
  the testbench's own intent, `add_intent(lib, cell=<testbench>, kind="constraint", text=..., rule={"type":
  "substitute", "cells": {"adc/sar_logic": "adc/sar_logic_ideal"}})` (in a changeset, with why). Not in the intent of
  the cell measured (a cell with ports): no run reads it there. A call's `substitute` goes over both;
  `{"adc/sar_logic": null}` runs the real cell.
- **With `extracted`**: both apply, each to its own cells. A sweep can vary properties inside the stand-in, not
  inside the cell it replaces.
- A run with stand-ins is never reused for one without (job provenance), and it is outdated when the stand-in changes,
  not when only the replaced cell does.

### ADCs and sample-and-holds: spectrum

An ADC is judged by a coherent FFT of its output codes. `spectrum` samples the transient as the circuit is clocked, at
`start + k / fs` for k = 0 .. `points` - 1:

- **What to sample.** `bits: ["d0", ..., "d7"]` (LSB first) builds the code; every bit is thresholded at one level,
  midway between the lowest and highest level the bits reach in the run (or at `threshold`); `weights` replaces 1, 2,
  4 ... for a redundant SAR. `signal: "out"` (or `["outp", "outn"]`) samples a waveform, such as a sample-and-hold's
  output.
- **When.** `start` is the time of the first sample. Sample a code where it is valid: after the conversion, not on the
  clock edge that updates it. `fs` (or `period`) is the conversion rate.
- **Coherent input.** Choose fin = fin_bin × fs / points, with fin_bin odd and prime to points (e.g. 256 points,
  fin_bin 13). Set the input amplitude to about -1 dBFS. If the result warns that the signal leaks into its
  neighbouring bins, the figures are pessimistic: make fin the coherent frequency it names or, if fin already is,
  start later (the signal is still settling).
- **Run length.** The transient's `stop` must reach the last sample, `start + (points - 1) / fs`.
- **Results.** `sndr_db`, `snr_db`, `thd_db` (2nd to 6th harmonic; `harmonics` changes how many), `sfdr_db` with its
  `spur_bin` (and `spur_harmonic`), `enob`, `harmonics_dbc`, and `signal_dbfs` with `full_scale` (2^N for codes, or
  volts peak-to-peak). `range` is the lowest and highest code; at -1 dBFS, reaching 0 or 2^N - 1 means something
  clips. A constant code (a stuck ADC) is the worst result, not an error: -300 dB (THD +300 dB) and a warning.

```
run_simulation(lib="adc", cell="tb_sar", analysis={"type": "tran", "step": "1n", "stop": "260u"}, spectrum={
  "bits": ["d0", "d1", "d2", "d3", "d4", "d5", "d6", "d7"], "fs": "1Meg", "points": 256, "start": "1.5u",
  "full_scale": 256})
```

`plot_waveform(lib, cell, signals?, job?, tstart?, tstop?)` plots the testbench's latest finished run:
- transient / DC as one panel;
- AC as magnitude (dB) and phase over log frequency;
- each trace's colour and range;
- signals named as in `run_simulation` (`out`, `x1.b9`, `v(out)`, `i(v1)`); a name that is not in the run is listed
  under `missing_signals`, and when none is, the error lists the run's vectors, the testbench's own nets first;
- `tstart` / `tstop` (e.g. `"390n"`, `"392n"`) show a stretch of the run. A long run keeps each stretch's lowest and
  highest point, so a narrow spike (a 40 ps overstress in a 3 us run) stays visible; zoom in to see its shape.

Look at the shape with it (ringing, slew, a wrong operating point). Report the figures from `run_simulation`.

To check the specs in the intent, use `check_specs(lib, cell)`: it runs each spec's testbench and compares the measure
with its limits, also at corners and temperatures. Report the numbers next to the targets. Sweeps and sizing:
[specs.md](specs.md).
