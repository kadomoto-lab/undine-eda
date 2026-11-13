# Specs, sweeps and corners: sizing against the intent

A cell's specs are intent items of kind spec: `{measure, min and / or max, testbench}`, plus `output` (and `input`) and
`analysis` when the measure is an AC figure, or `spectrum` when it is a spectral figure. Two tools measure them; neither
edits the design.

## check_specs: where the design stands

`check_specs(lib, cell, ids?, corners?, temps?, conditions?, fresh?, mismatch?, substitute?)` runs each spec's testbench (in your
working copy when a changeset is open) and returns, per spec, `met` / `not met` / `not measured`, the worst value, its
margin to the limit, where the worst case is (`worst_at`) and the `job` behind it (for `plot_waveform`). `not fully
measured` is met where measured, but some point gave no value (`why`, `not_measured_at`, `running`): that point may be
the worst case. The worst case is only ever taken from points that were measured.

- **Every point's value.** With several points (corners, temperatures, conditions), `points` gives per point each
  spec's value (`values`), the specs it misses (`not_met`), the ones it could not measure and why (`not_measured`: a
  failed run is reported there, never replaced by another point's value) and its `job` (for `plot_waveform`). Report
  from these.
- **Reused or fresh.** A run on exactly the same inputs (netlist, models, options) is reused: `runs` says how many
  were new and how many reused (and when those ran), and a point whose run was reused says when it ran (`reused`).
  To verify someone else's design independently, pass `fresh: true`: every point runs again.
- **Measures.** A `.meas` name of the testbench; an AC figure (`dc_gain_db`, `f_3db_hz`, `unity_gain_freq_hz`,
  `phase_margin_deg`, `gain_margin_db`; the spec needs `output`); `v(node)` of an operating point;
  `<device>.<quantity>` such as `x1.xm1.gm` or `x1.xm1.vds_minus_vdsat`; `<signal>.<min|max|initial|final>` of
  a transient; or a spectral figure of a transient (`sndr_db`, `snr_db`, `thd_db`, `sfdr_db`, `enob`,
  `signal_dbfs`). When a measure is missing, the answer lists what the run has.
- **Settling time.** `settle(v(top), tol=0.44m, from=12n)`: how long after `from` the signal enters ±`tol` around its
  final value and stays there to the end of the run; `to=40n` ends the window earlier (the final value is then the
  value at `to`), `target=0.9` settles to a given value instead. Read from the raw file, so it needs no `.meas` (an
  ngspice `.meas` cannot use a measured final value as its level, and such a `.meas` stops the whole run: Undine
  refuses it before running).
- **Derived measures.** Arithmetic and `max` / `min` / `mean` / `abs` / `sqrt` over other measures of the same run,
  computed after it: `max(tconv1, tconv2, tconv3)` (the slowest of several conversions, each a `.meas` in the spec's
  `meas`), `min(tw_msb, tw_lsb)`, `-iavg * 1.8` (power from an average current). One that cannot be computed says
  which measure is missing.
- **ADCs and sample-and-holds.** A spectral figure needs `spectrum`, the same as `run_simulation`'s (how to sample the
  run: `bits` or `signal`, `fs`, `points`, `start`, and `full_scale` for `signal_dbfs`; see
  [verification.md](verification.md)), and a tran `analysis` whose `stop` reaches the last sample (or a testbench
  whose `.control` writes a transient). A testbench that computes the figure itself (its `.control` prints `sndr_db`,
  or measures it with `meas`, after `echo Measurements for ...` in the same block; or a `.meas` of that name outside
  `.control`) needs no `spectrum`: `check_specs` reads its value, and the intent does not warn. A spec with its own
  `analysis` drops the testbench's `.control`: then only a `.meas` of that analysis counts. Specs with the same testbench and analysis share one run. Warnings about
  non-coherent sampling come back with the spec's result.
- **A spec's own `.meas`.** For a delay, a rise time or anything else the testbench does not measure, give the spec
  `meas`: one `.meas` statement (or a list), added to its runs only, and name its result in `measure`. The testbench
  stays as it is. Specs that share a testbench and analysis share one run and its `.meas` lines. When ngspice cannot
  evaluate one (a level never crossed), the answer quotes why.
- **No value as a failure (`missing: fail`).** A `.meas` that never triggers gives no value, and the spec is then
  `not measured` there. When that itself is the failure, give the spec `missing: fail`: for example "the DAC
  controls cleared after clk rose", whose `.meas` (TRIG clk rising, TARG the control falling) gives no value exactly
  when a control never clears. The spec is then `not met` there: the points in `no_value_at`, with `no_value_why`
  ("no value ..."), beside the other points' `not_measured_at` and `why` (in `points`: `not_met` and `no_value`).
  It counts only a `.meas` that ran and never triggered (a TRIG / TARG / WHEN level never crossed: ngspice says "out
  of interval"), or a `param=` of one. A `.meas` that could not run is a wrong spec and stays `not measured`: a node
  the run does not have ("no such vector"), a measure no `.meas` gives, an analysis that does not run. Without
  `missing` (or `missing: unknown`) nothing changes.
- **Without `analysis`** in the spec the testbench's own `.control` runs (with its `.meas` lines).
- **Stand-ins (`substitute`).** A spec measured on a top level with a slow block can run with a behavioural model of
  it. For example, a 256-point FFT run of an ADC (50 µs of transient) is only practical with an event-driven model of
  its SAR controller (~1700 transistors). Give the spec `substitute: {"adc/sar_logic": "adc/sar_logic_ideal"}`: every
  instance of the cell, at any depth below the testbench, is netlisted as the stand-in. The stand-in needs the same
  pins in the same order; otherwise the spec is not measured, and `why` names the pins that differ. A plain
  `check_specs` then measures it with the stand-in, and the spec's report names it (`substitute`): report the value as
  measured with the model. The call's `substitute` goes over the spec's, and the spec's over the testbench's default
  (a substitute rule in its intent). `{"adc/sar_logic": null}` measures with the real cell, to confirm the model now
  and then. `substitute_warnings` lists a substitution that replaced nothing (the cell is not in the testbench). A
  testbench run twice at a point (a spec with stand-ins next to one without) is named per run in the point's `jobs`,
  with the specs it measured: `tb_adc_fft (s1)`.

```
add_intent(lib="mylib", cell="inv", kind="spec", text="falling delay under 50 ps", reason="...", spec={
  "measure": "tphl", "max": 50e-12, "testbench": "tb_inv", "analysis": {"type": "tran", "step": "1p", "stop": "8n"},
  "meas": ".meas tran tphl TRIG v(in) VAL=0.9 RISE=1 TARG v(out) VAL=0.9 FALL=1"})
add_intent(lib="adc", cell="sar_logic", kind="spec", text="the DAC controls clear within 2 ns of clk", reason="...", spec={
  "measure": "tclr", "max": 2e-9, "testbench": "tb_logic", "missing": "fail",
  "meas": ".meas tran tclr TRIG v(clk) VAL=0.9 RISE=1 TARG v(dacp0) VAL=0.9 FALL=1"})
add_intent(lib="adc", cell="sar8", kind="spec", text="ENOB at least 7.5 bits at -1 dBFS", reason="...", spec={
  "measure": "enob", "min": 7.5, "testbench": "tb_sar", "analysis": {"type": "tran", "step": "1n", "stop": "260u"},
  "spectrum": {"bits": ["d0", "d1", "d2", "d3", "d4", "d5", "d6", "d7"], "fs": "1Meg", "points": 256, "start": "1.5u",
               "full_scale": 256}, "substitute": {"adc/sar_logic": "adc/sar_logic_ideal"}})
```
- **Operating conditions.** `corners: ['all']` (or a list of the PDK's corners: `['tt', 'ss', 'ff']` in sky130,
  `['typical', 'ss', 'ff']` in GF180MCU), `temps: [-40, 27, 125]` and
  `conditions` (testbench properties such as the supply or the load: `{'tb_ac/V1.value': ['1.62', '1.98']}`) run
  every combination; a spec is met only when it is met at all of them.
- **A spec's own conditions and limits per corner.** A spec can carry `corners`, `temps` and `conditions`: check_specs
  and sweep use them when the call gives none (the call's own replace them). `corners` and `temps` are lists, or
  mappings to the limits that differ there: `{measure: tconv, max: 90e-9, corners: {tt: {}, ss: {max: 97e-9}, ff: {}}}`
  is checked at tt, ss and ff, with 97 ns at ss; `temps: {27: {}, 125: {max: 99e-9}}` likewise (a temperature's limit
  over its corner's). The answer shows them (`limits_at`, and `limits_at_worst` when the worst point has its own).
- **Simulator options.** `conditions: {'options.reltol': ['1e-3', '1e-6'], 'options.maxstep': ['1n', '0.2n']}` runs
  each spec at each setting, to see whether a figure (an SNDR, an error of µV) is the circuit's or the simulator's.
  Options: `reltol`, `abstol`, `vntol`, `chgtol`, `trtol`, `gmin`, `method` (gear | trap), `maxord`, `itl1`, `itl4`,
  `maxstep` (the transient's largest step). `run_simulation` takes them as `options`.
- **Monte Carlo mismatch.** `mismatch: {runs: 40, seed: 1}` runs every point 40 times with the PDK's device mismatch
  models (sky130: the `_mm` sections of the model library, `MC_MM_SWITCH=1`: MOSFETs, MIM and MOM capacitors), seeds 1
  to 40 (reproducible: the same seed, the same numbers, and a run is reused; seeds start at 1, ngspice ignores 0 or a
  negative one). `mismatch.stats` gives per point (corner, temperature, condition) and spec: `runs`, `mean`, `sigma`,
  `min`, `max`, `met` and `yield` over the finished runs (a run that failed or gave no value, `not_measured`, counts as
  not met; one still running, `pending`, is not counted yet: call again for the rest, and with none finished the yield
  is null), `sigmas_to_limit` and the `worst_seed` / `worst_job` of the measured runs to look at. For a spec with
  `missing: fail`, the runs whose `.meas` never triggered are `no_value`, and `no_value_runs` gives the first five
  (seed, job): those are the runs to look at, whatever the measured ones give. A call runs at most 120 simulations:
  more runs are cut and `mismatch_note` says how many ran and which seed to continue from. Use few corners at a time
  with many runs; 20 runs give a rough sigma, `sigmas_to_limit` says how far the yield is from the limit.
- **A long answer.** `runs`, `pending`, `note`, `mismatch_note` and `failures` come before `points` /
  `mismatch.stats`; when those would not fit in the answer (its reader's budget: 9000 characters in the app, 20000
  for MCP clients), the last ones are left out and `points_not_shown` / `stats_not_shown` says how many: ask with fewer
  `ids`, corners or temperatures to see them. The Python client gets all of them (and `undine call` shortens what it
  prints as any long result).
- **A failed run.** A transient that stopped part way (`Timestep too small`, a B-source out of range) is a failed run,
  listed in `failures` with the log's line and a hint; none of its values is used.

```
check_specs(lib="adc", cell="cdac", ids=["s4"], corners=["tt"], mismatch={"runs": 40, "seed": 1})
add_intent(lib="adc", cell="cdac", kind="spec", text="settles to 0.44 mV within 3 ns of the edge (4 ns at ss)",
  reason="...", spec={"measure": "settle(v(top), tol=0.44m, from=12n, to=40n)", "max": 3e-9, "testbench": "tb_cdac",
  "analysis": {"type": "tran", "step": "10p", "stop": "40n"}, "corners": {"tt": {}, "ss": {"max": 4e-9}, "ff": {}}})
```

## sweep: where the design could go

`sweep(lib, cell, vary, conditions?, corners?, temps?, ids?)` measures the specs at every combination of property
values, netlisted as if edited (a transistor's derived values such as `ad` follow). Each design point is judged by its
worst case over the conditions, corners and temperatures (`worst_at` says where), and the points are ranked.

- **`vary`** holds the design choices: `instance.property` in the cell, or `cell/instance.property` elsewhere, mapped to
  a list of values or `{from, to, steps, scale: lin|log}`. Put what the design must survive (supply, load,
  temperature, corner) in `conditions`, `corners` and `temps`, never in `vary`: otherwise the ranking would pick the
  kindest supply instead of the best size.
- **Score**: the worst margin relative to its limit. A positive score meets every spec by at least that fraction.
  `best` lists the top points; `worst_case` summarises each spec over all points.
- **At most 120 simulations per call.** Finished runs are reused, so asking again costs nothing. If the answer says
  some are still running, `wait_for` one of its `pending_jobs`, then call it again with the same arguments (without
  `fresh`: the runs a fresh call started are then collected, not started again). Long transients need `time_limit_s`
  (each run's limit, default 1 h).
- **Failed points.** A design point with a condition that failed or gave no value has no value for that spec
  (`values` null, `not_measured` with why, where, and the worst of the other conditions as `worst_measured`): it is
  never shown with another condition's value. A spec with `missing: fail` whose `.meas` never triggered there is in
  `not_met` instead, and in `no_value` with its own why and where, so that point never ranks with those that meet
  every spec. With `mismatch`, each point's `yield` is its lowest over its conditions, of the finished runs
  (`yield_pending`: runs still running).

## The sizing loop

1. `check_specs` (add `corners: ['all']` once the typical corner passes) to see which specs fail and by how much.
2. Choose the few parameters that move the failing measures, from the circuit and the intent's goals and preferences
   (e.g. the input pair's W / L for gain and GBW, the compensation capacitor for phase margin).
3. `sweep` coarse (3 to 5 values each, log scale over a wide range), then refine around the best point.
4. Open a changeset and set the chosen values with `set_instance_params` (`reason`: the sweep result that supports it).
   Then `sync_layout` if there is a layout.
5. `check_specs` with corners on the working copy, and `run_checks`. Report each spec's value, margin and worst corner.

Never relax a spec or a `must` constraint to make the numbers pass. If no point meets every spec, report the best
trade-off you found and propose the intent change to the person.
