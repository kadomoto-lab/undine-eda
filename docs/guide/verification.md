# Verification

DRC and LVS run on the server with the PDK's KLayout decks; the **DRC / LVS** tab of the layout editor starts them and browses the results (rules, markers in the layout, LVS cross-references).

**Top-cell pins.** KLayout's comparison accepts a schematic pin that has no pin in the layout and pairs its net with any
net that has the same devices, so a supply cut off from its labeled stripe can still "match". Undine therefore also checks
that every pin of the schematic's top cell is a pin of the layout's top cell, on the net it matched; a pin that is not is
reported under the verdict (e.g. `pin VDPWR: no VDPWR label on the layout net it matched ($12)`) and the result is a
mismatch.

**A port the cell does not use inside.** A schematic port connected to nothing inside the cell (an interface pin such
as a CDAC's `vdd`, there for the symbol) has nothing to match in the layout: the deck's netlist simplification purges a
net with no device on it, labelled or not, so the layout has no pin for it. Undine accepts such a port when its label
is in the layout's top cell, on a pin label layer (`met1.label` ...) and on a shape of that metal, and lists it under
`unused_pins` in the LVS summary and in `get_lvs_report`; without the label it is an error (`pin VDD: connected to
nothing inside the schematic and no VDD label in the layout's top cell`). A label of that name on metal the layout uses
(a net with devices on it) is an error too (`pin VDD: connected to nothing in the schematic but its label is on the
layout net ...`): it would make a capacitor's plate, or another net, the cell's `vdd` port.

**MIM capacitors (sky130).** The MIM symbols write a call to the device (`XC1 c0 c1 sky130_fd_pr__cap_mim_m3_1 W=12
L=12 m=2`), which the sky130 deck's reader leaves a call. The deck extracts the plates as a capacitor of another model,
compared by area and perimeter. So the reference netlist has a MIM as that capacitor: `cap_mim_m3_1` becomes
`CC1 c0 c1 sky130_fd_pr__model__cap_mim A=144 P=48 m=2` and `cap_mim_m3_2` becomes `sky130_fd_pr__model__cap_mim_m4`,
with `A = W * L` and `P = 2 * (W + L)` per unit (um) and `m` from `MF`. LVS then compares the capacitors' total area
and perimeter and their nets: two 2 x 3 um units match `W=2 L=3 MF=2`; one unit, units of another perimeter (2.4 x
2.5 um) or the capacitor on other nets do not. The rule is the PDK profile's `lvs_elements`, which can rename a model
and compute parameters (`server/undine/pdks/pdkprofile.py`).

**Standard cells in LVS (sky130).** A layout built from the PDK's standard cells (a LibreLane macro imported, or cells
placed by hand) is checked against a schematic of them. The schematic's netlist calls the cells without defining
them, so Undine adds each cell it calls from the library's SPICE file (`sky130_fd_sc_hd.spice`): its transistors as
elements the deck compares with the layout's (`special_nfet_01v8` as `nfet_01v8`, as the deck reads it; W and L in
um), and the reference's parallel fingers combined as the deck combines the layout's (`schematic_simplify`). The deck
compares the cells one by one and then how they are wired. Three things in the layout would otherwise not match:

- *Tie cells.* `conb_1` drives HI and LO through poly resistors the deck does not extract, so in the layout HI is
  VPWR and LO is VGND. The reference does the same: the tie cell leaves it and the nets on HI / LO join the supplies
  (the schematic's `hi9` is `vdd` in LVS). A tie cell that joins two ports (HI on an output port) is refused: in the
  layout the two ports are one net, which LVS cannot compare with two pins. Drive such a port from a buffer instead.
- *Physical cells.* The cells a place-and-route run adds for the layout alone (fill, tap, decap, antenna diode) are
  not in the schematic. For LVS they lose their devices (diffusion and poly) and keep their wells, taps and rails, so
  a row's n-well still reaches its taps; they then add nothing to compare. They also leave the reference, those the
  schematic draws as well (a decap, an antenna diode): a run adds more of the same cells, so they are not compared.
- *Pwell labels.* A flop has two `VNB` labels on its pwell (64/59) that nothing the deck connects joins (an error
  that stops the extraction). They are left out of the standard cells.

The layout file is not changed: this is the copy LVS reads. The rules are the profile's `stdcell_lvs`. A cell that the
PDK has no SPICE of (a cell in a file of its own, such as `sky130_ef_sc_hd__decap_12.spice`, is found there), or one
with a device the deck does not compare, is refused with its name.

## Second opinion: Magic DRC and Magic + Netgen LVS

Besides the KLayout decks, the Verify panel can run **Magic DRC** (style `drc(full)`) and **LVS with Magic + Netgen** (Magic extracts
the layout, Netgen compares it with the schematic using the PDK's setup file), the same tools shuttle prechecks use (ChipFoundry's
`cf-precheck` extracts with Magic and compares with Netgen; wafer.space runs Magic DRC before KLayout DRC). Results use the same
browser (rule list, markers in the layout; Netgen's output on demand). On the demo inverter both agree (DRC clean, LVS match); on
a deliberately bad layout KLayout reports 172 and Magic 171 violations, in different rules -- which is the point of running both.
Magic lists a violation as the error tiles of its area; Undine merges touching tiles into one marker, so a count is a
count of violations (a 24 um butted tap is one `diff/tap.16`, not 138 boxes).

## The checks of a changeset

**Run needed checks** in a changeset's review (and `run_checks` for an agent) checks each cell the changeset changed,
in the working copy and in the baseline:

- **DRC** of a changed layout, with the PDK's KLayout deck, and on sky130, GF180MCU and IHP SG13G2 / SG13CMOS5L also
  with Magic's `drc(full)` (a row of its own, *DRC (Magic)*). The KLayout deck `sky130A_mr.drc` has none of the MV
  diff / tap rules (diff/tap.14-17) and checks the MIM spacings otherwise. On a bootstrapped switch whose KLayout DRC
  was clean, Magic found `diff/tap.16` (a 5 V nfet PCell's butted tap, 0.45 um where 0.7 um is the minimum),
  `capm.11` twice and `met3.3c` twice. On IHP, Magic has rules the KLayout deck's default run leaves out (minimum
  areas `M1.d`, `Gat.e`; the latch-up tap distances `LU.a` / `LU.b`). Magic checks the whole cell and a 10 um halo
  around it: a wire too narrow at the cell's edge has its error area outside the cell, which Magic otherwise drops.
  Without Magic installed, its rows fail and say so; the PDK profile's `magic_drc` says which PDKs run it.
- **LVS** of a cell with a schematic and a layout, and on sky130, GF180MCU and IHP SG13G2 an **ERC** from that LVS
  run's connectivity: a body tap that abuts a source / drain of another net. That is an n+ / p+ junction against the
  tap with nothing between, whose capacitance, leakage and reverse voltage are in no model, in no LVS and in no
  extraction. It happens when a PCell's `bulk tie` (a tap butted against one source / drain) ends up on a signal
  net: on the same switch, 7 of 13 devices, among them the sampling switch's output against the vss tap. Each
  finding names the instance, the terminal, its net and the tap's net. ERC rows are warnings (shown in yellow), not
  violations. On GF180MCU the junction is silicided, so it is a short that its LVS deck does not see. IHP's LVS deck
  joins a silicided junction to the tap itself, so LVS reports such a tap as a short, and the ERC finds the junction
  only under SalBlock. The PDK profile's `erc_tap` says how a PDK's taps are drawn.

Each row names its job, when it finished, and whether it is a job that had already run on the same inputs
(`reused`): checks are kept by their inputs (layout, reference netlist, deck, tool versions), so running them again
on an unchanged design takes the earlier results. `run_checks(fresh: true)` (or `POST
/api/p/<project>/changesets/<id>/verify?fresh=true`) runs every check anew.

A PCell variant Undine generated that nothing places any more (a `sync_layout` that changed `nf` left the old one, a
`place_pcell` piece was deleted) gets no rows: alone it is no design, and Magic's `LU.2` would fail it. `run_checks`
counts such cells in `skipped_unused_cells`, and the assistant's `delete_unused_cells` takes them out.

An agent's `run_checks` also checks each schematic the changeset changed (`kind: schematic`: its symbol against its
ports, supply parameters, pending connections). A failing one comes with the baseline's row, and says
`unchanged_from_baseline` when the design had the same findings before the changeset (a symbol's pin order out of step
since long ago), or lists in `new_in_work` the ones the changeset added. `get_lvs_report` says on a mismatch `why`,
in counts and names: devices by class in the layout and the schematic, devices and pins without a counterpart,
layout nets with one connection, or that KLayout kept no circuit of the layout at all (its deck's netlist
simplification drops every net with fewer than two connections and no pin: an unrouted placement without labels
leaves nothing, and the extracted netlist is only its header).

**Where the violations are.** `get_drc_report(lib, cell, rule?, checker?, limit?)` gives, for the latest DRC run of
each checker, each rule's description, count and markers in um: a box, the two edges a spacing or width rule
measured, or a polygon. Markers are in the cell's coordinates: KLayout reports a violation inside a sub-cell (a PCell
variant) in that cell's own coordinates with its placement, and Undine places it there (a cell placed several times
has its violation reported once, at one of its placements). `render_layout(lib, cell, markers: true)` draws them
over the layout, one colour per rule; with none to draw it still gives the picture, and `markers_note` says whether the
latest runs were clean or there is no run yet.

**Checking another agent's changeset.** A verifier passes `changeset: cs-...` to `run_checks`, `get_drc_report`,
`check_specs`, `extract_parasitics` and the other reads and checks: they run on that changeset's working copy without
taking its write lock or making it the verifier's, and are recorded in it as its writer's are
([Checking another agent's changeset](assistant.md#checking-another-agents-changeset)).

## Parasitic extraction and post-layout simulation

Once DRC and LVS are clean, a layout cell can be extracted with its parasitics and a testbench simulated with the
extracted cell in place of its schematic.

**Extraction.** An agent calls `extract_parasitics(lib, cell, mode)`; over HTTP it is `POST /api/p/<project>/pex` with
`{"lib", "cell", "options": {"mode": "c" | "rc", "cthresh_ff": 0.01, "res_threshold_ohm": 0.1, "res_nets": [...]}}`
(the last two for mode `rc`). The job flattens the cell (only the top cell's
labels are kept: they are the pins), extracts it with Magic and the PDK's setup file, and writes `extracted.spice`: a
subcircuit with the schematic's ports in the schematic's order and the devices of the layout, and

- mode `c`: each net's capacitance to the substrate and to each other net (the coupling capacitors), one node per net;
- mode `rc`: also the resistance of the wiring, a resistor network per net as Magic's `extresist` resolves it, with the
  same capacitances as mode `c` spread over the network's nodes. A net's capacitance is spread as Magic spreads it, by
  the area each node stands for. A coupling capacitor is split in two halves, one for each net. Each half is spread
  over that net's nodes in the same way, and each piece goes to the nearest node of the other net. So where the
  coupling sits along a wire follows the wire's area, not where the two nets overlap. To keep the netlist small, one
  half goes to at most 16 nodes, spread along the network.

Which nets get a resistor network in mode `rc`: every net with a port or a source / drain, unless its largest
resistance (from where the network starts to any point of it) is below `res_threshold_ohm` (0.1 ohm by default).
Resistors below that are also merged into their neighbours where the network allows. Magic's own defaults (10 ohm, and
a delay of 1 ps) leave every signal net of a small analog cell one node, so a switch's on-resistance would include no
wiring at all; Undine sets the delay limit to 0. `res_nets: [...]` gives networks to those nets only (a large digital
block, where the default would make a large netlist); a name matches the net in any case (`VIN` is `vin`). A net without a port or a source / drain (a well, a net
reached only through gates) keeps one node, because `extresist` stops on such a net; so does the substrate when it is
not a port (it is ground 0). The summary's `single_node` lists each net left as one node and why; for a net
`extresist` judged negligible, it gives the largest resistance it found. A Magic before 8.3.653 has no
`extresist minres` or `mindelay`, and its threshold means something else: it lumps nets by its own rules, and
`res_options_not_applied` says that `res_threshold_ohm` was not used.

Capacitors below `cthresh_ff` are left out (0.01 fF by default). The substrate node is ground (`0`) in a simulation
unless it is a port. A label the schematic does not have names an inner net. A net without a label (Magic names it
like `a_0_0#`) keeps its capacitance in mode `rc` too: `extresist` renames its nodes `a_0_0.t0`, `a_0_0.n1`, and its
capacitance goes to them.

**Net names.** The extracted subcircuit keeps the schematic's net names, also for nets that have no label. When an
LVS run of the same layout (the same layout hash) matched the layout's nets to the schematic's, the extraction labels
each net without a label with its schematic name, at the centre of one of its contacts or vias, on the label layer of
the metal above (the PDK profile's `net_labels`), before Magic extracts it. Magic names a net after its label, so a
testbench that probes an inner node (`v(x1.g)`) finds it in the extracted view. Without such an LVS run, Magic names
those nets after a point on them (`a_3109_5296#`), and the summary's `names` says so (run the checks, then extract
again). A schematic net that LVS matched to a net with no contact to label is listed in `names.unnamed`. The PDKs
with `net_labels` are sky130, GF180MCU and IHP SG13G2.

**Device names.** Magic names the devices `X0`, `X1` ... in no particular order, so a testbench's device quantity
(`@m.x1.xxm5.msky130_fd_pr__nfet_g5v0d10v5[l]`, `@m.x1.xm1.msky130_fd_pr__nfet_01v8_lvt[gm]`) would find no device.
With the same LVS run, each of Magic's devices takes the name of the schematic's element (as the simulation netlist
writes it, `XXM5`) that LVS matched to the layout device whose model and terminal nets are its own (source and drain,
or a capacitor's plates, either way round; a net Magic left without a name stands for one net throughout). A device
that Magic splits in several (fingers, a multiplier) is the element, then `_2`, `_3` ... (`XXCB`, `XXCB_2` for a
MIM of MF=2): a quantity read on it (`[id]`, `[gm]`) is that piece's. Elements alike (same model, nets and L, such as
dummies) share Magic's devices out; a device that fits elements that differ keeps Magic's name. The summary's
`names` says how many were named (`devices_named`), which kept Magic's name (`devices_kept`) and which elements are
split (`devices_split`).

**MIM capacitors.** Magic's sky130 tech orders the MIM capacitor's plane (`cap1` for `capm`, `cap2` for `cap2m`)
above every metal in its `planeorder` ("MiM cap layers have been purposely put out of order"). Its default
capacitance rules (`defaultoverlap`, `defaultsideoverlap`) take the planes that shield one metal from another from that
order, so the top plate shields nothing: the metal above a capacitor reaches the bottom plate through it. Magic then
counts, between the two plates' nets, the overlap of that metal over the bottom plate inside the capacitor (84.03
aF/um² for met4 over met3 on sky130A) and the fringe of its edges there (`defaultsideoverlap allm4 metal4 allm3 metal3`:
70.52 aF/um). In silicon that is metal over the top plate, of its own net; the device's model has the plates' own
capacitance, the fringe of the top plate's edge in its perimeter term.

So an extraction of a cell with MIM capacitors runs Magic a second time (in `shielded/` of the job), with each
capacitor plane ordered just above its bottom plate and every default rule between the bottom plate's metal and a
metal above copied to the capacitor at the same value. The top plate takes what the bottom plate got through it: no
coupling for its own metal, a coupling to the top plate's net for another net's metal. The second extraction's
coupling between nets is the result. Its capacitance to the substrate is not: Magic shares the fringe of an edge out
among what lies beside it, and the capacitor's new fringe rules take a little of the metal above's fringe to the
substrate (2.8 aF of a 1.17 um unit's 1521 aF bottom plate), which the top plate over its bottom plate does not change
in silicon. So the capacitance to the substrate is the first extraction's. The summary's `mim_shielding` says what
changed:

- `removed_ff`: the coupling taken out in all (every pair's change summed: what moves keeps its total), of which
  `overlap_ff` by area (the metal tied to the top plate by a via inside the capacitor) and `fringe_ff` the rest (the
  fringe, and any metal of the top plate's net over it without a via there);
- `pairs`: each pair of nets that are a MIM's two plates, with what Magic gave (`magic_ff`), what is kept
  (`kept_ff`: the coupling outside the capacitors, such as a strap over the bottom plate's edge or the bottom plate's
  contact beside it), its change `removed_ff` and `overlap_ff` by area. A pair's change is net of what moved onto it:
  in a CDAC with one top net, a bottom plate's metal over another unit's capacitor couples to the top net now, the
  first unit's plate pair (`moved_to_plates_ff` in all);
- `moved`: every other pair that changed, such as another net's wire over a capacitor, now coupled to its top plate
  (its total stays).

A 1.17 um sky130 unit with its 0.78 um contact pad: Magic 191 aF between its plates, 51 aF of it the pad's area and
38 aF its fringe inside the capacitor, 102 aF kept (the pad's fringe onto the bottom plate around the capacitor, the
bottom plate's met4 contact beside it). With a met4 strap per column over a 4 x 4 array, 357 aF per inner unit became
303 aF. When the second extraction fails, only the overlap area is taken out, as before (`mim_overlap_removed`), and
`mim_shielding.not_applied` says why.

Two labels on one net are one port in Magic's netlist. When only one of them is a pin of the schematic, that pin names
the net. When both are pins of the schematic, the summary lists them under `ports.shorted` and the ports do not match.

Undine assembles mode `rc` itself because Magic's resistance flow gets the capacitance wrong. Magic 8.3.465 keeps each
coupling capacitor and adds it a second time to the nets' capacitance to ground. For a 1 mm met1 wire next to another
wire, the coupling is 113 fF and the capacitance to ground becomes 164 fF instead of 51 fF. In the paper "The Era of
Vibe Tapeout", lumping the coupling into ground made an ADC's SNDR look 9 dB better than it was. Undine takes the
devices and the resistor network from Magic's resistance run and the capacitances from the capacitance run of the same
extraction, so the totals per net and per pair of nets are those of mode `c`. Magic also has trouble with hierarchy, so
the cell is flattened before the extraction: on a hierarchical cell, `extresist` writes every transistor twice, and
Magic's own `flatten` disconnects a MIM capacitor from a via4 drawn above it. The module docstring of
`server/undine/checks/parasitics.py` has the details.

**Summary.** The job result (and `GET /api/p/<project>/jobs/<job>/parasitics?net=`) gives:

- the capacitance of each net, largest first (to ground, coupling, total);
- the largest coupling capacitors;
- in mode `rc`, the resistance of each net (the sum of its resistors, and the largest resistance from its pin along the
  network);
- the devices by model;
- whether the ports match the schematic (missing pins, pins shorted to another pin, extra labels);
- floating nets: nets with capacitors only, which get 1 TΩ to the substrate so that ngspice finds an operating point;
- where the inner nets' names come from (`names`), and what a MIM capacitor's top plate shields (`mim_shielding`);
- `warning` when the layout's LVS does not match its schematic (the extraction runs anyway, for diagnosis; names
  says why devices keep Magic's names), and `runtime_s`, how long it took.

An agent's `get_parasitics` with `net` gives only that net's rows (a net the extraction does not have is an error
that names the nearest ones). It also says whether the extraction is outdated (`outdated: true / false`, and what changed)
and what it was made from (the layout's hash and the time).

Long lists are cut at 12 rows, with a count of the rest. Run times on sky130A: an inverter, about 1 s; a 2 × 1024 MIM
capacitor array (2048 devices), 1.9 s in mode `c` and 3.7 s in mode `rc` (8124 resistors); an unrouted CDAC of 1156
1.17 um units, 3.6 s in mode `rc` with the second MIM extraction.

**Simulation on the extracted view.** `run_simulation`, `check_specs` and `sweep` take
`extracted: ["lib/cell", ...]` (or `{"lib/cell": "<job>" | "c" | "rc"}`); `POST /api/p/<project>/sim` takes the same
field. The testbench is netlisted as usual, with its corners, temperatures, property overrides and spectral measures.
The difference: each listed cell's subcircuit is its newest up-to-date extraction (or the job named), with its ports
in the symbol's pin order. That cell and everything below it come from the layout. The run is refused, with what to
run, when:

- the cell has no finished extraction;
- the extraction is outdated (the layout or the schematic's ports changed since);
- the extraction lacks a port of the schematic (a pin without a label), or has two pins on one net;
- the cell is not in the testbench;
- a sweep varies a property inside a cell simulated as extracted;
- the testbench names a node inside an extracted cell that the extraction does not have (`v(x1.g)` where the
  extraction named that net `a_3109_5296#`, or any name it lacks: in a `.meas`, a B source, `.save`, `.control`, or
  `signals`). ngspice would make each such node a new, floating one, and the operating point would fail with a
  convergence message. The error lists the nodes and where the extraction's names come from;
- the testbench asks for a quantity of a device inside an extracted cell that the extraction does not have
  (`@m.x1.xxm5.msky130_fd_pr__nfet_g5v0d10v5[l]` where the device kept Magic's name `X0`). ngspice says "no such
  device" and evaluates nothing, so a `.control` script would go on with a wrong value. The error lists them.

Such a run stores only the testbench's own nets and voltage source currents, the vectors its `.meas` lines name, and
the signals asked for (`signals`, `output`, `input`, `spectrum`, `sample`, and those a spec measures). It does not store the
nodes inside an extracted cell: an `rc` extraction of a CDAC has thousands, and a long transient would store all of
them at every time point. To see such a node, name it in `signals` (for example `x1.out.n0`). A testbench with its own
`.save` lines stores what they say. A transient on the schematic stores the same set: see
[What a simulation stores](#what-a-simulation-stores).

The result and the job (`extracted`, and an `extraction` entry in its inputs) name the cells and the extraction jobs.
When the layout changes, the simulation is outdated as well. A simulation on a named extraction job stays up to date
while that job does. A simulation on the newest extraction is outdated as soon as a newer extraction of that mode gives
another netlist. For example, an inverter driving a 1 mm wire has a
falling delay of 26 ps as drawn, 186 ps extracted in mode `c` and 214 ps in mode `rc`.

<a id="what-a-simulation-stores"></a>
## What a simulation stores

A transient, on the schematic or post-layout, stores in its raw file only:

- the testbench's own nets and the currents of its voltage sources;
- the vectors its `.meas` lines and its `.control` block name (`v(x1.outp)`, or `x1.outp` in a `let`);
- the signals asked for: `signals`, `output`, `input`, `spectrum`, `sample`, the measures read off the run, and those
  a spec measures in `check_specs` and `sweep`.

The nodes inside its cells are not stored. ngspice would otherwise store every node at every time point. For a
mixed-signal block that is hundreds of megabytes per run, and a changeset's evidence keeps every run. The SAR ADC's
comparator, for example, has 3,191 vectors and wrote 870 MB per Monte Carlo transient. To see a node inside a cell,
name it in `signals` (for example `x1.xcmp.outp`). To store every node, pass `save_all: true`, or tick **Store every
node** in the Simulate panel. A testbench with its own `.save` lines, or with `.options savecurrents`, stores what it
says. An operating point, an AC run and a DC sweep store every node: their files are small.

A result that names a signal the run did not store says so, and how to keep it the next time (`missing_hint`). The
job says what it stored (`saved: testbench`).

<a id="stand-ins"></a>
## Stand-ins: a cell simulated as a behavioural model

A top level has to place the real cells: its layout, LVS and extraction are built from them. Some of its simulations
are only practical with a behavioural model of a slow block. An example is an ADC whose SAR controller is about 1700
transistors of standard cells, with an event-driven model of the controller in a cell of its own. The model has the
same pins in the same order. The ADC's 256-sample FFT testbench needs about 50 µs of transient, which is only
practical with the model. A **stand-in** runs such a testbench with the model in place of the controller, without
editing the design:

```
run_simulation(lib="adc", cell="tb_adc_fft", substitute={"adc/sar_logic": "adc/sar_logic_ideal"}, ...)
```

Every instance of `adc/sar_logic`, at any depth below the testbench, is netlisted as `adc/sar_logic_ideal`. The
stand-in's own symbol netlists the instance (its format and template). A stand-in without a symbol (only a
schematic) is called with the original's symbol and its own name. `run_simulation`, `check_specs`, `sweep` and
`POST /api/p/<project>/sim` all take `substitute`. The run is refused, with what differs, when:

- either cell does not exist;
- the stand-in's pins are not the original's in the same order (each cell's symbol pins, or its schematic's ports when
  it has no symbol). The message names the pins, for example `in another order: pin 2 is vss, not out`;
- the stand-in contains the cell it replaces, directly or deeper (also through a cell the testbench places above it,
  or through another stand-in), which would never end. The message names the chain, for example
  `div_s > wrap > div`;
- a stand-in is itself replaced by another (`{a: b, b: c}`): name each cell's final stand-in;
- the stand-in has the name of another cell the testbench netlists (from another library): a netlist has one
  subcircuit per name, so the two would be simulated as one;
- a cell is simulated both as extracted and as a stand-in. The message says where the stand-in comes from (the
  testbench's default, for example), and `substitute {"lib/cell": null}` takes it back.

**Where it is kept.** A one-off run passes `substitute`. A result that has to be reproducible keeps it in the intent:

- **A spec** carries `substitute` next to its corners, temperatures and conditions. A plain `check_specs` of the ADC's
  SNDR spec then measures it with the model:
  `{measure: sndr_db, min: 60, testbench: tb_adc_fft, spectrum: {...}, substitute: {adc/sar_logic: adc/sar_logic_ideal}}`.
- **A testbench** gives the stand-ins that all its runs use by default, also runs from the schematic editor's
  **Simulate** panel. This is a constraint of the testbench's own intent:
  `{kind: constraint, text: "the controller as its model: 50 us at transistor level is too slow", rule: {type: substitute, cells: {adc/sar_logic: adc/sar_logic_ideal}}}`.
  A run reads only these rules from the file: a problem elsewhere in it does not stop the run. A file that is not
  YAML at all is reported in `substitute_warnings` (its stand-ins were not read); a broken substitute rule refuses
  the run, naming the file and the item. The same rule in the intent of a cell with ports (the cell measured, not
  its testbench) is never used, and the intent panel warns about it.

The layers add up. The testbench's default comes first, the spec's goes over it, and the call's goes over both. A cell
mapped to `null` (or `""`) is netlisted as itself. `check_specs(..., substitute={"adc/sar_logic": null})` therefore
measures the SNDR spec with the transistor-level controller once, to confirm the model.

**What a result says.** A run with stand-ins lists them in its result and job (`substitute`, and a note saying where
each comes from). A substitution that replaced nothing is reported in `substitute_warnings`, not ignored: the cell is
not in the testbench, or it lies only inside a cell simulated as extracted or as another stand-in. `check_specs` names
the stand-ins of each spec in that spec's report, so a reader of the SNDR knows the controller was behavioural. Specs
with other stand-ins never share a run. When a testbench runs more than once at a point, the point's `jobs` names each
run with the specs it measured (`tb_adc_fft (s1)`).

**Provenance.** The stand-ins are part of the job's inputs (`substitute` in the testbench's `netlist` entry). A run with
stand-ins is never reused for one without them, nor for one with others. Its netlist hash covers the stand-in's cell
instead of the original's. When the model changes, the run is outdated; when only the transistor-level controller
changes, it is not.

**With extracted cells.** `extracted` and `substitute` work together. Each extracted cell comes from its layout, each
replaced cell is netlisted as its stand-in, and the other cells come from their schematics as before. A cell that lies
inside an extracted cell is not netlisted from a schematic, so replacing it does nothing, and the result warns about it.
A sweep cannot vary a property inside a replaced cell (the message names the stand-in). Inside the stand-in it can,
since that schematic is netlisted.

## Cells from a dependency

A simulation runs what the netlist tells ngspice, and the cells of a [dependency](libraries.md#dependencies) are
someone else's work. So when a simulation, a sweep or a spec check is about to run, Undine refuses the lines a
dependency's cell contributes that run commands (`.control` blocks, `*#` lines) or read a file nobody looked at
(`.include`, `.lib` or XSPICE `file=` paths that are absolute outside the PDK, have a `..` part, or keep a `$` or
`~`), unless a person has [trusted the dependency](libraries.md#trusting-a-dependencys-code). A line counts as the
dependency's when it is in one of its cells, or was formed from one of its symbols placed in your schematic. A result
that would be reused is refused all the same, since the run is checked before reuse.

```
ip/opamp (dependency analog_ip 1.2.0, not trusted): code block s1 line 1: .control; a person can trust analog_ip@1.2.0 in this project (undine lib trust demo ip, or Trust... in Undine); an agent cannot
```

**LVS** (KLayout, and Magic + Netgen) runs no control code, so it refuses only those paths in the reference netlist,
when the run starts: showing a sign-off status or reusing a result in a changeset reads the netlist without
refusing. The netlist view with Xschem's netlister refuses `tcleval(...)` in the schematic or symbol attributes of
any untrusted dependency installed in the project, which Xschem would evaluate as Tcl whichever symbols it reads;
with Undine's own netlister it only warns (`not trusted: would be refused in a simulation: ...`). A netlist given to a
simulation as text is the caller's own, and is not checked.

The Simulation and Verify panels, and the sign-off suite's rows in the Tapeout panel, show the refusal with
**Trust...**, which opens the trust dialog for that dependency; once trusted, run again. In a changeset preview the
refusal says to trust the dependency in the project instead. An agent gets the refusal as text and asks you.

## Outdated results

Each run records what it ran on: a content hash of the layout cell (with everything it instantiates), of the netlist
generated from the schematic, the PDK, the deck and the tool versions (see [Job provenance](../reference/data.md#job-provenance)).
When the design changes after a run, the result is marked **Outdated** with what changed ("layout of mylib/inv changed",
"schematic netlist of mylib/inv changed") and a **Rerun** button; the Simulation panel does the same for simulations. The
comparison is by content: an undo back to the state of the run makes the result current again, and a comment added to
the schematic does not make an LVS result outdated.

## PDK models in a simulation

A simulation does not read the PDK's whole model library. The section its netlist names (sky130: `sky130.lib.spice tt`,
`tt_mm` with mismatch) is read as a copy that keeps only what the netlist can reach: the devices it and the files it
includes (standard cells, extracted views) use, with the parameters, options and random draws they depend on, in the
library's own order and wording. The results are the same to the last digit, and the models load in a fraction of the
time. On sky130, on a busy machine, a one-transistor operating point took 65 s with the whole library and 0.2 s with
the copy (about 20 s whole on an idle machine), and an operating point of a SAR ADC's mix of devices (1.8 V, low-Vt and
5 V transistors, MIM capacitors, standard cells) 80 s and 2 s. Sections read together (GF180MCU's: `typical`,
`res_typical`, `mimcap_typical` ...) are reduced together, each keeping what the others' kept definitions need. A copy
is made once per PDK version, section and set of devices, in `~/.cache/undine/models` (`UNDINE_CACHE`), and made again
when a library file changes. The job's netlist names the library and section, and its status (`models`) and the first
line of its log say which copy it read. A library or a netlist line the reducer does not understand (IHP SG13G2's
library) is read whole, and the job says why; `UNDINE_SIM_FULL_MODELS=1` always reads it whole.
