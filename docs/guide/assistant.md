# Agent and MCP

New to this? Start with the step-by-step [Connect an AI agent](../getting-started/connect-ai.md), then follow the
tutorial [Design with an AI agent](../tutorials/agent.md); this page is the reference.

## Agent (in-app)

The in-app **Agent** is for people who have only the browser (no shell on the server) or a small local model; external
agents over MCP are the main way. It is hidden by default. The Agent icon (the robot) just left of **Changes** in the top bar,
always there, shows or hides it; `?chat=1` at the end of Undine's address and **Request changes** in a review show it
too. It sits in the left column above the cell list (drag the line between them to share
the column, double-click to reset). It answers questions about what you have selected. The browser sends ids (`M3`, `net Y`), not design
data; the server resolves them (component type, parameters, connectivity, recent changes, DRC/LVS results) and builds the
context, so the model never has to guess from a screenshot. The attached objects are shown as chips (`M3 ×`) above the
input, the message records what was sent, and a selection is pinned to a design *revision*: if the design changed since,
the send is refused until you refresh.

One connection at a time, enforced on the server (`server/undine/agents/llm.py`):

| Connection | Where | Configure |
|---|---|---|
| **Cloud LLM** | Anthropic API (default model `claude-haiku-5-5`; any other model id can be set in the connection settings) | `ANTHROPIC_API_KEY` on the server, or a connection added in the UI (below) |
| **EDA-server LLM** | an OpenAI-compatible endpoint next to the server (Ollama, vLLM ...; default `http://127.0.0.1:11434/v1`) | endpoint + model in the connection settings (or `UNDINE_LLM_SERVER_URL`); optional `UNDINE_LLM_SERVER_KEY` |

`python -m undine serve --llm-policy {both,cloud,server,none}` (or `$UNDINE_LLM_POLICY`) decides what may be used; with
`server` no cloud client is ever created. There is no automatic fallback, a conversation stays on the connection it
started on (moving it needs an explicit "continue here", which sends the history to the new endpoint), keys are never sent back to
the browser, and everything sent is recorded in `<project>/llm/audit.jsonl`.

**Adding your own connections in the UI.** The Agent's connection settings (the sliders icon) list the built-in
connections and the ones you add: **Add a connection** with a provider (Anthropic, OpenAI, or any OpenAI-compatible URL), a
name and an API key. Undine checks the key by listing the provider's models, then keeps it on the server in
`~/.undine/connections.json` (mode 0600, outside every project; `$UNDINE_HOME` moves it). The key is never shown again (only
its last four characters), never written to a project or the audit log, and can be removed with the connection. Pick the
connection and a model (from the provider's list, or typed) and **Use this connection**; it stays the one active connection
until you switch. A connection to a public endpoint counts as `cloud` and one on this machine or a private network as
`server` for `--llm-policy`.

API: `POST /api/llm/connections {provider, label, api_key, base_url?}`, `POST /api/llm/connection {connection, model}`,
`POST /api/llm/connections/<id>/models` (list again), `DELETE /api/llm/connections/<id>`.

**Giving the server its API key (alternative).** Instead of adding a connection in the UI, a key can be given to the
server process in its environment, never stored in the project. Put it in a file only you can read and let `scripts/restart.sh`
load it:

```sh
mkdir -p ~/.undine && install -m 600 /dev/null ~/.undine/env
echo 'ANTHROPIC_API_KEY=sk-ant-...' >> ~/.undine/env      # optional: UNDINE_LLM_SERVER_KEY=...
./scripts/restart.sh
```

## Design intent

Each cell can have a design intent next to its views: what it is meant to achieve and must respect. Open it with
**Intent** in the schematic's side panel. It holds:

- **ports** (kind and side on the cell outline, or *let the tool decide*) and **supplies** (which nets are power and
  ground; *Use suggested supplies* fills them from names like `vdd`, `vss`);
- **goals** (directions to optimise), **preferences** (how to trade off), **constraints** (to respect: *must* or
  *should*, optionally with a rule such as symmetric / matched instances, taken from the schematic selection), **specs**
  (a measure with a min / max and the testbench that checks it) and **context** (background), each with a reason and who
  added it;
- free **notes**, and the same content as YAML (**YAML** in the panel's header).

At the top, each flow step shows whether it has what it needs ("layout: 7 missing"); hover for the list. Problems, such
as a constraint naming an instance the schematic no longer has, are listed below. Intent edits are recorded with undo /
redo, and in a proposal they appear in the review like any other change. The file is `<cell>.intent.yaml` in the
library.

The agent always receives the intent of the cell it works on (in its design context; `get_intent` for all of it). It
works from it, never relaxes a *must* constraint or a spec on its own (it proposes the change instead), and records what
you state as a lasting requirement or priority with `add_intent`, quoting your words; one-off directions stay out.
When you later confirm or extend an item, it adds your words to that item with `update_intent` (`request`): the panel
shows them under the words the item came from, each with its date, and none is ever replaced. Its intent tools are
`add_intent`, `update_intent`, `remove_intent`, `set_port_intent`, `set_supply_intent` and `set_intent_notes`. The
design behind it: [Design intent](../design/intent.md).

## The tool surface

When asked to change something, the agent opens a changeset and edits its working copy (`set_instance_params`, `add_instance`,
`add_wire`, `delete_objects`), run `run_checks` (DRC/LVS before vs now) and `run_simulation`, and read with
`get_selection`, `get_design_context`, `get_checks`, `get_lvs_report`, `get_drc_report`, `check_device_integrity`, `get_history`, `list_cells`, `get_changeset`
(`server/undine/agents/agent_tools.py`; with `id`, any changeset, also a closed one: how it was applied, discarded or
reverted, its commit, its operations and the evidence archived then). It cannot adopt: the chat shows the steps taken and a button to the changeset, where a person
reviews and applies it. Layout tools (coordinates in µm, snapped to 5 nm): `get_layout_info`, `get_device_geometry`,
`query_shapes`, `sync_layout` (create the layout view; swap in the PCell that matches changed schematic parameters, in
place; place missing devices, a PDK standard cell such as `sky130_fd_sc_hd__inv_1` from the PDK's GDS), `place_pcell`,
`place_array`, `add_box`, `add_path`, `add_label`, `move_instance`, `swap_instances`, `delete_instances`, `delete_shapes`,
`delete_unused_cells`, `delete_cell`, `restore_changeset`, and for assembling a block, `create_library`, `place_cell` and `import_layout` (below). Drawing on the PDK's stack (sky130, GF180MCU and IHP SG13G2; taps and rings on sky130): `route` (a wire on one layer between two ends,
points, device terminals such as `"XMS:S"` (or one piece's, `"child@x,y:S"`) or nets by name, with via stacks where
an end is on another layer; the first way, straight, L, Z or round what is in the way, that touches no other net and
keeps the spacings, never poly over a diffusion; every point and bend on the 5 nm grid; a via's pads laid along the
way it is left, or turned a quarter when only that fits, as along a strip it comes onto across), `add_via` (a via stack with the PDK's cut sizes, spacings, enclosures and pad areas, a poly
contact's npc bridged to a neighbour's), `add_tap`
and `guard_ring` (substrate and n-well taps and closed rings with their implants, contacts and metal, an implant
apart from the devices' own, an n-well apart from other n-wells and n+ diffusion). They refuse, saying what and
where, a shape that would touch another net (also through an n-well) or break a spacing (also a notch it leaves in
its own net's metal), and a via that Magic refuses next to a placed cell's own vias (one abutting or partly over a via of
its kind there: Magic reads the cuts of an array as one contact, so a via goes exactly on the same
array or clear of it; or any via on a MIM capacitor's own contacts, such as a via4 on the top plate over its via3 array:
lead the plate out past the capm first); the nets come from a
connectivity extraction of the layout (labels, and the schematic nets of the bound devices' terminals), and `dry_run`
says what they would draw. `get_device_geometry` gives a device's layers (any layer by name or number, contacts as
arrays) and its terminals (S / G / D / B, a MIM's plates) with the net each is on now and in the latest LVS. A device
placed as several `place_pcell` pieces is all of them by its name (`pieces` lists their ids) and one of them by its
id; a multi-finger PCell placed as it is (nf 2 ...) has its terminals too, each the strips of all its fingers, with
`nets` while those are not joined yet; a route to such a terminal from one already joined to some of them goes to
one of the others (route again until it is refused as one net). `guard_ring` with `around` names encloses every piece bound to them;
`render_layout` can highlight nets, draw the markers of the latest DRC runs (each checker's, a colour per rule) and
fill the layers solid. Answers stay short: a
`batch` groups what it changed per reason and `get_changeset` per step (steps alike as one), with the reason said
once and the effects counted per cell (`add box met1 ×20`, instances by name and place; `detail` lists each
operation), `get_history` gives whole steps, coordinates are rounded to the grid, and `get_netlist` leaves a
testbench's long code out on request (`code: false`, `code_limit`). Schematic devices are connected by net name with `connect_pins` (`net: "out", pins: ["M2:d", "M4:d"]`),
so the model needs no pin coordinates. It draws the net as a person would: routed wires from each pin to the nearest pin or
wire already on the net (the router of the wire tool, avoiding device bodies), the first wire carrying the net's name. Supplies
and ground (`vdd`, `vss`, `gnd` ...), nets with more than 8 pins and pins with no safe route get net labels instead, pointing
away from their device; `style: "wire"` or `"label"` forces one or the other. A port (ipin / opin / iopin) with the same name
joins the net. Nothing is written unless every pin ends up on its net and no other nets were joined.
`list_symbols` with `ref` gives a symbol's pins with their offsets; `add_instance` takes `rot` and `flip` (mirror, e.g. the
right half of a differential pair). `add_instances` and `connect_nets` place several symbols or connect several nets in one
step (all or nothing). Writes need a `reason` and can never touch the real design; there is no cap on their
number unless the server sets `UNDINE_AGENT_MAX_WRITES` (counted per changeset).

**Re-working a drawn schematic, and its symbol.** Every wire is drawn on the 10-unit grid, with its bends and junctions
more than a grid step away from other nets' pins and wires, so nothing reads as connected that is not; a pin that
cannot be reached that way gets a label.

- `get_drawing` gives how a schematic is drawn: the instances whose extents overlap, each instance's position,
  rotation, mirroring and extent (its texts included), and each net's wire ids, labels and pins. `net`, `instances`
  or `region` (`[x1, y1, x2, y2]`) narrows it down; a drawing too large for one answer comes in less detail (positions,
  then names, then counts), the overlaps first. A wire id is its end points, `x1,y1,x2,y2` with the ends sorted;
  `delete_objects` and `label_net` take them as they are.
- A pin drawn to another net is moved in one step with `move: true` on `connect_pins` / `connect_nets` (or taken off
  alone with `disconnect_pins`): the wires from the pin to the first junction or other pin and a label on it go, the
  rest of its net stays connected (re-routed where that cut it apart) and keeps its name. Inserting a stage:
  `connect_nets(nets={"a": ["x5:X", "xd:A"], "b": ["xd:X", "xr:B"]}, move: true)`.
- `label_net(net, wires?)` replaces the net's wires (those ids, or all of them) by net labels: what is left dangling
  goes too, and each piece of the net gets a label on a pin. The circuit stays the same; for a wire across the sheet.
- `move_instance` with `view: "schematic"` moves schematic symbols (`instance` or `instances`, by `dx`, `dy` or to
  `x`, `y`, snapped to the grid); the wires on their pins are re-routed, and nothing moves when that is not possible.
- `set_port_order(order)` sets the order of the ports (the subcircuit's pin order), and the symbol's pins take the same
  order in the same step; nothing else of the symbol changes (if that step fails, the answer says so and the schematic's
  order stays set). `check_symbol` compares the symbol with the ports: missing, extra or duplicate pins, directions,
  pin order (`pin_order_mismatch`), and sides against the intent's port sides; a mismatch also shows in
  `get_design_context` and `run_checks` (kind `schematic`). `sync_symbol(sides?, regenerate?)` makes the symbol follow the ports: order,
  directions, pins added and removed, and sides (`sides`, else the sides `set_port_intent` gave). A hand-drawn symbol
  keeps its drawing; a plain box symbol is redrawn at the size its pins need. Schematics that place the symbol keep
  their connections; a pin one of them connects, drawn or pending, cannot be removed.
- Standard cells of `sky130_stdcells` take their supplies as parameters (`VPWR`, `VGND`, `VPB`, `VNB`). `get_netlist`
  shows them as `supply_pins`, connections to nets, and warns (ahead of the instances) of one naming a net nothing of
  the schematic is on: no pin, port or label, no node of a code block (left at its default `VPWR`, for example); the
  netlist, the schematic editor and `run_checks` warn of it too.

**Assembling a layout.** A block such as an ADC is put together from cells, in the changeset like any edit:

- `create_library(name)` adds a library to the project (named as in **New library**: letters, digits, `_ . + -`; not
  the name of a library the project has, a PDK's or a dependency's included). It is recorded in the new library's log
  and listed in the review, and applying the changeset adds it to `project.json`.
- `place_cell(lib, cell, child, x, y)` places a layout cell, its origin at x, y: a cell of `lib`, a cell of another
  library (`child_lib`; copied into `lib` with the cells under it, as placing another library's cell in the layout
  editor does), or a standard cell of the PDK by its name (`sky130_fd_sc_hd__nand2_1`; the library comes from the
  name, the layout from the PDK's GDS under `libs.ref/<library>/gds`, read when it is placed). A cell `lib` has under
  that name is placed only when it is the same as the one asked for, the cells under it included; otherwise the call
  is refused. A cell under the copied one that `lib` has with other content is copied under a new name (`via$1`), and
  the answer says so (`renamed`). `rot` (0, 90, 180, 270) and `mirror` (about the x axis, first) orient it.
  `columns`, `rows`, `pitch_x` and `pitch_y` make one regular array instance of at most 1048576 elements, such as the
  unit capacitors of a CDAC: LVS extracts every element of it, and the review and the log hold one instance. For
  elements that belong to different schematic instances (a common-centroid arrangement), place several arrays or
  single instances. `name` binds an instance to a schematic instance as `sync_layout` does, so `get_layout_info` sees
  it as that device. A bound array, or a bound cell that is not the device's PCell (unit capacitors bound to the
  schematic's capacitor), is the layout's own: `sync_layout` never puts one PCell in its place, also when the
  schematic's parameters change, and `get_layout_info` lists it under `own_layout` with the cell the schematic would
  give. LVS does not go by the binding: it compares the devices it extracts with the schematic, so the schematic must
  have as many (a multiplier such as `MF` equal to the number of unit capacitors, for example). The answer warns when
  the schematic has no such instance, or what `sync_layout` will do with the binding.
- `import_layout(lib, path | job)` brings the cells of a GDS / OASIS file into the library: a file a generator wrote
  (under the server's import directories, `--import-root`, as for **Import**), or the final GDS of a `run_librelane`
  run (`job`). New cells are added and saved at once; cells the library has with the same content stay. A cell it has
  with other content is never overwritten silently: the import is refused, naming the cells, unless they are listed in
  `replace`; then each replaced cell's old and new shapes and instances are recorded, so the review shows them and undo
  takes them back. A cell the file places without defining it must be in the library, or be a PDK standard cell.

Restoring the changeset to a checkpoint before a copy or an import takes the cells it brought in out again, as long as
nothing else changed them (the record keeps the content they came with, which reading them back from the library's
cell files does not change). Restoring to a checkpoint before `create_library` takes the library out too, with its
log, once its cells are gone; a file in it that no recorded operation made stops the restore.

**Schematic-driven devices the agent steers.** `sync_layout` creates the layout view of a cell that has only a
schematic. It places the units a MIM capacitor's `MF` asks for as one array at the rule pitch, and gives each MOSFET
a butted tie only where a source / drain is on its bulk net, else a guard ring (see the
[layout editor](layout.md#schematic-driven-layout)). Its `layout` argument sets a device's layout parameters, which
stay with the layout instance:

- `{"XMS": {"nf": 4}}` draws the schematic's one 20 µm finger as four strapped 5 µm fingers, and LVS still matches
  W 20;
- `bulk`, `tap_side`, `gate` and `sd_contacts` choose the tap and the contacts;
- `columns` shapes a unit array.

The other tools:

- `place_pcell(pcell, params, x, y, rot, mirror, name?)` generates any PCell of the PDK with chosen parameters and
  places it: a dummy, or a piece of a schematic instance, which `sync_layout` then leaves to the designer. Unknown
  PCells or parameters are refused with the valid ones. A variant the library already has is not generated again.
- `place_array(devices, pattern, origin, ...)` places the units of several devices in one grid, each unit bound to its
  device: `common_centroid` (each device's units point-symmetric about the centre), `interdigitated`, or rows given
  cell by cell. It can add dummy rings, unbound or bound to a dummy device that has exactly as many units (otherwise
  it refuses, since LVS would count them wrong). It answers with the unit map, each device's centroid offset, the rule
  pitch it used and where each unit's origin lies (so a unit's id follows from the map). MOSFET units share one well
  and implant inside a guard ring; thick-oxide units have the ring and its well inside their hvi.
- `delete_instances` removes instances by id (`child@x,y`, the origin in µm as the grid has it, as `get_layout_info`
  lists them), by the schematic instance they are bound to, inside a box, or by cell.
- `move_instance` moves a bound or unbound instance by `dx` / `dy` or to `x` / `y`, and sets `rot` / `mirror`. The one
  not given stays as it is. Without `x` / `y` it turns in place, then moves by `dx` / `dy`.
- `swap_instances(instances: [a, b])` exchanges two instances in one recorded step: their places (each takes the
  other's origin and orientation), or with `what: "binding"` the schematic instances they are bound to (both stay;
  instances of one cell only). An id names a place, so two bound units of a CDAC swapped this way need no free spot in
  between; after a place swap each id names the other instance.
- `restore_changeset(to)` takes the changeset back to one of the restore points `get_changeset` lists: the last 12
  by default, `restore_points_more` says how many older ones there are, and `restore_points` (a number or `all`) and
  `restore_before` (a point: the ones before it) reach every one. The points are numbered by the changeset's records
  (`restore_point_now` is where it is), `operations` counts those in effect: the two differ by undone operations,
  undo / redo / restore records and batches taken back. While a batch is still being applied (its client may be gone),
  `in_progress` says how far (`done` of `of`); its operations are neither counted nor listed, and it has no restore
  point, until it ends.
- `delete_unused_cells(lib, cells?, include_baseline?, dry_run?)` takes out the PCell variants Undine generated that
  nothing places any more (a `sync_layout` that changed nf or the gate side, a deleted `place_pcell` piece leaves them),
  with `sync_layout`'s `_par` / `_tie` cells round them. Without `cells` it takes those the changeset created; the
  ones that were there before it (left by changesets applied earlier) it counts in `baseline_unused` and takes only
  with `include_baseline: true` or by name. Only such cells: one of the designer's, or one still placed, is refused.
  It is recorded with each cell's content, so a restore brings them back. `run_checks` skips such cells anyway (a
  variant alone is no design: Magic's LU.2 would fail it) and says how many in `skipped_unused_cells`.
- `delete_cell(lib, cell, views?)` deletes a cell of the changeset: its layout, schematic and / or symbol (by default
  every view it has), such as a pilot cell that is no longer needed. It is refused while anything places it (the
  layout cell in another layout, the symbol in a schematic or testbench, the schematic while its placed symbol
  stays). It is recorded with the content, so a restore brings it back, and applying the changeset removes it from
  the project. The cells its layout placed stay.
- `get_changeset` lists every cell the changeset created that is still there in `new_cells` (`lib/cell` with its
  views), the generated PCell variants and the `_par` / `_tie` cells round them too: every other cell is the
  baseline's.

`get_layout_info` lists arrays with their columns, rows and pitch, lists unbound instances with their ids (and counts
them by cell), and lists the bound instances `sync_layout` leaves alone (`own_layout`).

Its `wiring` (sky130) comes from the drawn conductors, extracted flat with KLayout as the LVS deck joins them, at
each placed device's terminals:

- `unrouted_nets`: the nets whose terminals are not joined yet, with the pins apart; a port's label counts as one of
  its net's pins (`vout (label)`), so a pin left on a piece of its own shows. The substrate and the n-wells join
  nothing here, because neither is a wire: `substrate_only` names the unrouted nets that only the substrate would join
  (a `vss` whose rail is missing), and `well_only` those that only an n-well joins (two pMOS ties of `bt` in one well,
  with no metal between them). LVS matches both, because the deck joins every p-tap to its substrate net and every
  n-tap to its well;
- `body_ties`: the body of a device without a tap of its own (synced with bulk `none`) counts as tied when a tap of its
  bulk net sits in the same substrate region (the same n-well for a pMOS) within the latch-up distance (sky130: 15 um,
  LU.2 / LU.3) of its diffusion, for example a `vss` guard ring around it or the rail's p-tap beside it. Each entry
  names the tap (`tap_um`) and how far the farthest corner of the diffusion is from it. A tap of the net is one wired
  to another pin or the label of the net; for a net that is only such bodies (a well bias with no label in the cell),
  a tap wired to no other net. `bodies_untied` says why a body is not tied (no such tap, or the nearest one is too
  far);
- `shorts`: the nets that touch, also two nets whose taps (or bodies) share an n-well (`on: nwell`);
- `butted_taps`: taps butted against a source / drain on another net. LVS does not see those.

`route` follows the same view: two ends that only an n-well joins are not "one net already", and it draws the metal
between them (its answer says so in `note`).

`batch` takes up to 2000 operations in one step: 2000 `add_box` in a project on sky130A take about 4 s in one `batch`
and 6 s as separate calls, a 32 × 64 `place_cell` array 0.01 s (`server/tests/bench_layout_assembly.py`).

`run_simulation` returns what a designer reads off the plots: for an operating point every node voltage and each
transistor's bias (`id`, `vgs`, `vds`, `vth`, `vdsat`, `gm`, `gds`, its region and `vds_minus_vdsat`, plus the devices
not in saturation); for AC with `output` (and `input`) the low-frequency gain, -3 dB frequency, unity-gain frequency,
phase margin, gain margin and gain / phase per decade; for transient and DC sweeps each signal's initial, final, min,
max, steepest slope and a sampled waveform; and any `.meas` results. Analysis arguments are checked (`ac`: `sweep`,
`points`, `fstart`, `fstop`; `tran`: `step`, `stop`, `tstart`, `tmax` or `maxstep`, `uic`; `dc`: `source`, `start`, `stop`, `step`). A failed run returns the
error lines of the log with what they usually mean, naming a device whose size its model rejects. A testbench without a
model statement gets the PDK models (typical corner) and says so; the PDK's corner symbol (`sky130_fd_pr/corner.sym`)
selects another corner. `corner`, `temp` and `options` (simulator options: `reltol`, `abstol`, `method`, `maxstep` ...)
run it at a PDK corner, at a temperature or with other tolerances without editing the testbench. `measure` takes
`.meas` statements for that run (AC ones with `vm()` / `vp()` too), and measures Undine reads off the run:
`settle(v(out), tol=0.5m, from=10n)`, how long after `from` the signal enters `tol` around its final value (or
`target`) and stays there until `to` (the end of the run by default), and derived measures such as `max(t1, t2)` or
`-iavg*1.8`. A `.meas` that uses another
measurement as its level (`WHEN v(out)='vf-1m'`) is refused before the run: ngspice would stop the whole run on it.

A transient also takes `tstart` (results from then on), `tmax` (the largest time step) and `uic: true` (start from the
`.ic` values). An `analysis` replaces the testbench's own `.control` blocks and analysis lines for that run. Every
`.meas` that gave no value is listed under `measurement_errors` with ngspice's reason and the statement (a missing
vector such as `v(a,b)`, a level never crossed); a measure that ngspice evaluated a second time after the testbench's
own `.control` block is not reported as failed, and Undine ends such a block with `quit` when it already runs every
analysis, so the analysis runs once. More than 99 `par()` measures in one run work (ngspice's own limit is 99: Undine
computes them with B sources). The model hint appears only when a model stopped the run, and names the transistor, its
W / L / nf (W per finger) and its model bin. A run whose analysis stopped short (a time step too small, a model that
refuses a device) has failed, even with a partial raw file; the error lines of a run that still finished are its
`warnings`. A testbench that places the PDK's standard-cell symbols
(`sky130_stdcells/inv_1.sym`) gets the cells' SPICE (`sky130_fd_sc_hd.spice`) included like the models, and corners
still work.

**A dependency's code.** `run_simulation`, `sweep`, `check_specs`, `lint_rtl` and `run_rtl_sim` are refused when a
dependency's cell or RTL would run code or read a file outside the PDK and no person has trusted that dependency
([Trusting a dependency's code](libraries.md#trusting-a-dependencys-code)). The tool returns the refusal as its
result: the cell, what was found, and that a person trusts it with `lib trust` or **Trust...** in Undine. No agent
tool, `undine call` command or client method can trust a dependency, nor ask for the confirmation the browser's
dialog uses; the agent reports the refusal as it is and asks you.

**Long runs.** Each run uses one ngspice thread, and the server runs a limited number of simulations at once (by
default the cores but two; [settings](../reference/cli.md#configuration-environment)); the others wait in order.
`run_simulation` waits `wait_s` (default 150 s, 0 to answer at once); a run still going is not an error: the answer
gives its `job` and `state` (`queued` with its `queue_position`, or `running` with `elapsed_s`) and how to go on.
`wait_for(events=["job:<id>"], timeout_s=1800)` waits for it, `run_simulation(lib, cell, job="<id>")` returns its
results (with any `signals`, `output`, `spectrum`), and `cancel_job(job)` stops a run of the agent's own (in its
changeset, or started by its session). A run stops at its time limit (1 h by default): `time_limit_s` on
`run_simulation`, `check_specs` and `sweep` allows a longer one (up to 12 h by default). On timeout or cancel ngspice
is terminated, then killed.

**Across a server restart** (to deploy a change) runs go on: ngspice keeps running, the next server takes each run up
(its slot, its time limit, `cancel_job`, its results as usual) and starts the queued ones in their order, so
`wait_for` and `run_simulation(job=...)` simply go on working with the same job id. A call that waits (`wait_for`,
`run_simulation`, `check_specs`, `run_checks`, `extract_parasitics`) answers at once when the server is asked to stop
(`"stopping": true` in `wait_for`'s answer): wait again a few seconds later. The agent's session and its changeset
carry over too ([Restarting the server](../reference/cli.md#restarting-the-server)). Only a simulation that no server
can finish (its ngspice killed or crashed, the machine restarted) is `interrupted`, with why: run it again. Other runs
are not taken up: a DRC (KLayout or Magic), LVS, ERC, extraction, RTL or LibreLane run ends with the server, as
`interrupted` (`wait_for` lists it in `ended`), and is run again once the server is back (`run_checks` or
`extract_parasitics` for the checks).

With `spectrum`, a transient is measured the way an ADC or a sample-and-hold is: the run is sampled as the circuit is
clocked, at `start + k / fs` for `points` samples (linear interpolation between the simulator's time points), either
a node (`signal`, or two for a differential one) or an ADC's output code built from its bits (`bits`, LSB first, each
thresholded midway between the logic levels or at `threshold`; `weights` for a redundant code). One rectangular-window
FFT, which assumes coherent sampling (fin = fin_bin × fs / points, with fin_bin odd and prime to points), gives
`sndr_db`, `snr_db`, `thd_db` (the 2nd to the 6th harmonic, aliased into the first Nyquist zone; `harmonics` changes how
many), `sfdr_db` with its spur, `enob` = (SNDR − 1.76) / 6.02 and, with `full_scale` (peak-to-peak volts, or 2^N
codes), `signal_dbfs`. Leakage next to the signal bin (non-coherent sampling, or a signal still settling) comes back
as a warning with a coherent frequency to use; samples with no signal (a stuck ADC) are the worst result, with every
figure at -300 dB (THD +300 dB) so that the specs on them fail; samples outside the run, or a run that is not a
transient, are refused.

**Specs.** `check_specs(lib, cell, corners?, temps?)` measures every spec of the cell's intent on its testbench and
says whether it is met, by what margin, and where the worst case is (a spec that a point could not measure is
reported as not fully measured, never as met). A spec's measure is a `.meas` name, an AC figure
(the spec then names its `output`, and an `analysis` unless the testbench runs one), `v(node)` of an operating point,
`<device>.<quantity>`, `<signal>.<min|max|final>`, a spectral figure of a transient (`sndr_db`, `snr_db`, `thd_db`,
`sfdr_db`, `enob`, `signal_dbfs`: the spec gives `spectrum`, as above), or the result of the spec's own `meas` (`.meas` statements added
to its runs only, for delays and rise times without changing the testbench; a failed one is reported with ngspice's
reason), a settling time or a derived measure (as above). `corners: ['all']`, `temps: [-40, 27, 125]` and `conditions`
(testbench properties such as the supply: `{'tb_ac/V1.value': ['1.62', '1.98']}`, or simulator options such as
`options.reltol` to check the numerical floor) run every combination (the PDK model library's section is chosen per
run; `.temp` is set). A spec can carry its own `corners`, `temps` and `conditions`, used when the call gives none;
`corners` and `temps` may map to the limits that differ there (`{tt: {}, ss: {max: 97n}}`). With several points the
answer gives each spec's value at every point; a point whose run failed or gave no value is listed as such, and
another point's value never stands in for it. A spec with `missing: fail` counts a `.meas` that ran and gave no
value (it never triggered: a control that never clears after the clock) as not met there, with the reason "no value",
instead of not measured; a `.meas` that could not run (a node the run does not have) stays not measured. Runs on
exactly the same inputs are reused, and the answer says which and when they ran; `fresh: true` runs them all again,
for an independent check. `mismatch: {runs, seed}` runs each point that many times with the PDK's device mismatch
models (sky130: its `_mm` sections, `MC_MM_SWITCH=1`), one seed per run (seeds start at 1), and gives per spec the
mean, sigma, min / max, the yield against its limits (over the finished runs; runs still running are counted apart)
and the worst seed (for `missing: fail`, also the runs that gave no value); runs beyond the 120 simulations of a call
are cut, and the answer says so. A transient that stopped part way (`Timestep too small`) counts as a failed run.
`sweep(lib, cell, vary, ...)` explores sizes without editing: it
netlists each combination of the design choices in `vary` as if edited (`{'M1.W': [1, 2, 4]}`, a
`cell/instance.property` in another cell, or `{from, to, steps, scale}`), measures the specs at every condition, judges
each design point by its worst case, and ranks the points by their worst margin relative to the limit. At most 120 simulations per call,
a few at a time; a run on exactly the same inputs is reused, so asking again returns at once and continues a sweep
that was still running. The agent then sets the chosen values in a changeset and confirms with `check_specs`.

**Post-layout.** Once DRC and LVS are clean, `extract_parasitics(lib, cell, mode)` extracts a layout cell with Magic.
Mode `c` gives each net's capacitance to the substrate and to the other nets. Mode `rc` adds the wiring's resistance and
keeps the coupling capacitors (Magic's own resistance flow counts them twice; see
[Verification](verification.md#parasitic-extraction-and-post-layout-simulation)). The result gives the capacitance per
net, the largest couplings, the resistance per net, the devices, and whether the ports match the schematic;
`get_parasitics` reads it again (`net`: only that net's rows; a name it does not have is an error naming the nearest
ones). On a layout whose LVS does not match, it extracts anyway, for diagnosis, with a `warning`; `runtime_s` says how
long it took. `run_simulation`, `check_specs` and `sweep` take
`extracted: ['lib/cell']` to simulate those cells from their extraction. They refuse an extraction that is missing,
outdated or without the schematic's ports, and say what to run.

**Stand-ins.** The same tools take `substitute: {'lib/cell': 'lib/cell'}` to simulate every instance of a cell as a
behavioural model with the same pins, for example an ADC's SAR controller in a long FFT run. A spec or a testbench's
intent can keep it, and the answer names the stand-ins it used (see [Stand-ins](verification.md#stand-ins)).

## Watching and stopping a turn

A request is accepted at once and runs in the background as a *turn*. The chat shows it step by step: what the agent
says, each tool call as it starts and ends (with its arguments, reason and any error), and the changeset it works in. **Stop**
ends the turn before its next step; what it already changed stays in the changeset. Reloading the page, or opening the
conversation on another screen, picks the running turn up again; a turn that a server restart cut off is shown as
*interrupted* and is not re-run.

The screen you asked from shows the work as it happens: when the agent commits its first change, the review of its
changeset opens (once) on the proposal, and the drawing follows the cell it is editing, showing all of it as it grows;
only the views of cells that changed reload. Picking another tab, or **Stop following**, pauses that (the agent keeps
working); **Back to latest** resumes it. Closing the review is respected. Other screens see that the agent is working.

While a turn works on a changeset, nobody else can change that changeset: manual edits, undo, adopt and discard are refused
(HTTP 409) and the review says who is editing, with its own **Stop**. External agents over MCP hold the same lock while they
write (it lapses after 120 s without a write); stopping one refuses that session's (or that agent's) later writes. See
[Live changesets](../design/live-changesets.md) for the design.

API: `POST /api/p/<project>/chat` returns `{conversation, turn, state}` (`?wait=true` runs the turn before answering, and a
repeated `client_id` returns the turn already accepted); `GET .../turns/<id>?after=<n>` returns its state and the events from
index `n` on; `POST .../turns/<id>/stop`; `POST .../changesets/<id>/writer/stop` stops whoever is editing a changeset.

## MCP (external LLM clients)

The same tools are served over MCP (Streamable HTTP) for clients such as Claude Code or Claude Desktop, so people can use
the LLM tool they already have. It is off by default: start the server with `--mcp` (or `UNDINE_MCP=1`); it is never available with
`--llm-policy none`. A bearer token is created in `<projects root>/mcp-token`. Every tool takes `project`; edits go to a changeset,
and everything the tools return is recorded in `<project>/llm/audit.jsonl` (connection kind `mcp`; a result over 20000
characters, which a client gets only when it asks for more, by its start, length and hash). The GUI publishes its
current selection to the server, so `get_selection` answers "this transistor".

```sh
UNDINE_MCP=1 ./scripts/restart.sh
claude mcp add --scope user --transport http undine http://127.0.0.1:8700/mcp \
  --header "Authorization: Bearer $(cat ~/undine-projects/mcp-token)"
```

### Several agents on one connection

A client that runs several agents at once (Claude Code's subagents, for example) sends all their calls over one MCP
connection: one `mcp-stdio` relay is one MCP session. Undine keeps the changeset being written per session, so the
agents would share one: a changeset one of them opens becomes the others' too, and their edits land in it. Each agent
therefore passes its own name as `agent`, which every tool takes (`agent: "layout"`):

- Calls with the same name share a changeset and its write lock, as the calls of one session do.
- An agent cannot write a changeset that another agent holds (the one-writer lock, below). The refusal names the
  holder; `list_changesets` and `get_changeset` say which changeset is yours and who is writing each.
- The history records the agent with each edit (`agent:claude-code/layout`), and the review names it as the writer.
  **Stop** stops that agent only.
- When the session ends (the chat closes), every agent's lock is released.

Without `agent`, every call of the connection shares one changeset. In the shell, `--session NAME` is the agent
`NAME` (below).

### Checking another agent's changeset

An agent that verifies another's work (a reviewer, a second opinion) reads and checks that changeset without becoming
its writer: the read and check tools take `changeset: cs-...` (`get_layout_info`, `get_device_geometry`,
`render_layout`, `get_drc_report`, `get_lvs_report`, `run_checks`, `check_specs`, `extract_parasitics`,
`get_parasitics`, `run_simulation`, `wait_for` and the other reads).

- The call works on that changeset's working copy. It takes no write lock, and your own changeset (if you have one)
  stays yours: no `open_changeset id=...` is needed. It reads even if your session opened that changeset earlier;
  only its writer (the one holding its write lock) works in it as its own.
- What it starts there is recorded in that changeset as its writer's runs are: `run_checks` rows, extractions and
  simulations in its working copy. The writer sees them, and can go on editing meanwhile; an edit after a check makes
  that check outdated.
- Nothing of the changeset is edited: a write tool given `changeset` is refused, and `cancel_job` stops only jobs you
  started there (simulations, checks, extractions, any job). `batch` does not take reads of another changeset: call
  them on their own.
- Each answer says the changeset it read: a JSON answer starts with `"changeset": "cs-..."`, a text answer with the
  line `changeset: cs-...`. What it tells you to call next (`wait_for`, `get_parasitics`, `run_simulation job=`)
  carries `changeset` too: pass it.

```sh
undine call run_checks changeset=cs-cd6857 --session verify
undine call check_specs lib=sar cell=sar_bootsw extracted:='["sar/sar_bootsw"]' changeset=cs-cd6857 --session verify
```

### Watching an external agent in Undine

Work done by an external agent (MCP, `undine call`, Python) shows in Undine as it happens. You keep the terminal and the
browser side by side, and the browser follows on its own.

- **The screen follows by itself.** When an agent starts writing, a screen open on the project opens that changeset's
  review on the proposal, and the drawing follows the cell the agent changes, reloading only that cell.
  - It does so only when the screen is free: no review open and nothing unsaved. Otherwise it shows a bar
    "*claude-desktop* (over MCP) is editing “*title*”" with **Watch**.
  - Following works as for the in-app agent: pick another tab or **Stop following** to pause, **Back to latest** to
    resume. Close the review and that changeset is not followed again.
- **Who is editing.** The review says who is editing and has **Stop**, which refuses that session's further writes.
- **When the agent stops writing**, the review becomes an ordinary one. If the review is closed, a bar says "*agent*
  finished “*title*”: it waits for your review" with **Review**, and the **Changes** count goes up.
- **No second browser tab.** When the work starts, the local launcher (`mcp-stdio`) opens the review in the browser only
  if no Undine screen has the project open. Otherwise it leaves following to that screen
  (`GET /api/p/<project>/presence`).
- **The review URL.** `undine call open_changeset` prints it as `review`.

`GET /api/p/<project>/live` lists who is editing which changeset, and the changesets being adopted, discarded or
reverted with how far (`closing`; their `writer` is null); `GET .../changesets/<id>/activity?after=<gseq>` lists the
cell views committed after a point.

### Claude Desktop extension

The easiest way to connect Claude Desktop: build the extension and open it with Claude Desktop (or drag it onto its
**Settings → Extensions** page).

```sh
pixi run mcpb      # -> dist/undine-<version>.mcpb
```

Claude Desktop asks for the **Undine checkout** (where undine-eda is, with `pixi install` done; on Windows the path inside
WSL, e.g. `~/undine-eda`), and optionally the server and UI URLs. The extension runs the local launcher described below:
on macOS / Linux from the checkout's pixi environment, on Windows through `wsl.exe` (the review opens in the Windows
browser). It contains no server code, so updating the checkout updates what it runs. The bundle is not signed.

### Desktop clients: the local launcher

For a chat app on the same computer (Claude Desktop, Claude Code ...), register Undine as a *local* (stdio) MCP server. The
client then starts `python -m undine mcp-stdio`, which

- reuses the Undine server if it runs (default `http://127.0.0.1:8700`, or `--url` / `$UNDINE_URL`), or starts one in the
  background with MCP on (loading `~/.undine/env` like `scripts/restart.sh`; log in `~/.undine/serve.log`). The server keeps running
  when the chat ends, so the proposals stay in Undine to review, adopt or continue later;
- starts it with the tools of its own environment, as `pixi run` does (`UNDINE_TOOLS`, `SPICE_LIB_DIR`; what
  `~/.undine/env` sets is kept), so that an older ngspice in `~/undine-tools` never stands in for the environment's. An
  environment without ngspice (a plain venv) or a system Python is not made the tool prefix: that server looks in
  `~/undine-tools` and on `PATH`, as any other does;
- relays the conversation's tool calls to it with the bearer token (no token to copy);
- opens the changeset's review in the browser once, when work on it starts (`--ui-url` if the UI is served elsewhere, e.g. the
  Vite dev server; `--no-open` to turn it off; `$UNDINE_OPEN` to choose the command, `{url}` is replaced), and adds an
  `open_in_undine` tool to open it again on request;
- ends the MCP session when the chat closes, so the changeset it was writing is free at once (otherwise the lock lapses
  after 120 s).

A running server without MCP is not restarted; the client is told to restart it with `UNDINE_MCP=1 ./scripts/restart.sh`.
A later chat continues a proposal with `list_changesets` and `open_changeset` with `id`.

```sh
claude mcp add --scope user undine -- /path/to/undine-eda/.pixi/envs/default/bin/python -m undine mcp-stdio   # Claude Code
```

`--scope user` registers it for every folder; without it, only for the git repository you run it in (or that folder,
outside a repository). Step by step, with how to check it: [Connect an AI agent](../getting-started/connect-ai.md#claude-code).

Claude Desktop (`claude_desktop_config.json`); on Windows with the server in WSL, run it through `wsl.exe`:

```json
{"mcpServers": {"undine": {"command": "wsl.exe", "args": ["-e", "bash", "-lc",
  "cd ~/undine-eda/server && ../.pixi/envs/default/bin/python -m undine mcp-stdio"]}}}
```

In WSL the review opens in the Windows browser.

<a id="undine-on-another-machine"></a>
#### Undine on another machine

When Undine runs on a server (with the PDK) and the chat app on your computer, add `--ssh HOST` (or `$UNDINE_SSH`; in
the extension, **SSH host**). `HOST` is `user@host` or an alias in `~/.ssh/config`, so jump hosts, ports and keys come from
there. The launcher then

- forwards a port on this computer (`127.0.0.1`, the server's port if it is free, else any) to the server over SSH;
  `--url` is the server as seen on `HOST` (default `http://127.0.0.1:8700`);
- reads the token on `HOST` (`ssh HOST cat <projects root>/mcp-token`) and keeps it in memory only;
- opens the review in your browser through the forwarded port, and opens the forward again if it drops;
- never starts the server there: start it on `HOST` with `UNDINE_MCP=1 ./scripts/restart.sh`.

ssh runs without a terminal (its input is the chat), so it needs a key without a passphrase or an ssh-agent, and a known
host key: run `ssh HOST true` once in a terminal first. The launcher still runs from an Undine checkout on your computer.

```sh
claude mcp add --scope user undine -- ~/undine-eda/.pixi/envs/default/bin/python -m undine mcp-stdio --ssh me@eda-server
```

Without SSH (a VPN or the same network), point `--url` at the server and give the token in `$UNDINE_MCP_TOKEN`.
`undine call` and the Python client take the same `--ssh` / `ssh=` (see below).

Browser-based chats (a remote connector) reach the server from the
provider's cloud, not from your computer; that needs a reachable, authenticated endpoint and is not covered here.

Note the difference to the in-app agent: with MCP the external client decides where the data goes (the exclusive-connection
guarantee applies to the in-app agent only); what leaves the server is what the tools return, and that is logged.

## Scripts and the shell

The same tools are reachable without an MCP client, from the shell and from Python. Coding agents such as Claude Code or
Codex can use them through their shell, and you can script a change. This needs a running server with MCP on.

```sh
undine tools                                   # every tool; `undine tools write_rtl` shows one tool's arguments
export UNDINE_PROJECT=demo
undine call get_netlist lib=mylib cell=inv
undine call open_changeset title="Wider pull-down" reason="fall time misses s1"
undine call set_instance_params lib=mylib cell=inv instance=M2 params:='{"W": "1.5"}' reason="faster fall"
undine call write_rtl lib=mylib cell=counter text=@counter.sv reason="add a synchronous clear"
undine session end
```

- **Arguments.** `key=value` is a string, `key:=json` any JSON value, and `key=@path` the text of a file.
  `key:=@path` is the JSON value in a file, and `--json @path` (or `--json -` for stdin) all the arguments as one
  object, for long lists such as `batch` ops. A yes / no argument also takes `key=true` / `key=false` (`move=false`
  is false).
- **Output.** The result prints as JSON. The exit code is 1 when a tool refuses (its message on stderr) and 2 when
  Undine cannot be reached.
- **Request ids.** Each call first prints its request id on stderr (`undine: request id ...`). When a call is cut off
  (a timeout of the shell running it), send it again with `--request-id`: a write that already happened returns its
  first result instead of acting twice.
- **Server.** `--url` picks the server (default `$UNDINE_URL` or `http://127.0.0.1:8700`), and `--start` starts one
  with MCP on. `--ssh HOST` (or `$UNDINE_SSH`) reaches an Undine on another machine as the
  [launcher does](#undine-on-another-machine): each call forwards a port for as long as it runs and reads the token on
  `HOST`; the session is kept per host. A review URL in the output is as seen on `HOST`.
- **Sessions.** The shell keeps one MCP session per `--session` name between calls (in `~/.undine/cli-sessions.json`,
  or `$UNDINE_CLI_SESSIONS`), so a changeset opened by one call is still yours in the next. `undine session end` lets
  it go; the changeset stays open for review.
- **A named session is an agent.** `--session NAME` calls the tools as the agent `NAME` (`agent=NAME` on every call
  that gives no `agent` of its own). The history (`agent:undine-cli/NAME`), the review and the write lock name it, and
  two named sessions are two writers even in one sessions file. Without `--session` the calls are unnamed, as before.
  A name is up to 64 letters, digits, `.`, `_` or `-`; another name stays a session name only. Each call says on
  stderr who it runs as (`undine: request id ... (agent NAME)`), and `open_changeset` and `list_changesets` answer
  with `you`. Agents working side by side each use a `--session` (and helper scripts) of their own.

From Python the tools are methods, and results come back parsed:

```python
from undine.client import Undine, ToolError        # also undine.agents.client

with Undine(project="demo", agent="layout-1") as u:   # agent: who you are, as the shell's --session NAME
    cs = u.open_changeset(title="Wider pull-down", reason="fall time misses s1")
    for m in ("M2", "M4"):
        u.set_instance_params(lib="mylib", cell="inv", instance=m, params={"W": "1.5"}, reason="faster fall")
    print(u.run_checks(), u.review_url(cs["id"]))
```

`agent=` is the name the shell's `--session NAME` gives: the history, the review and the write lock name it.
`session=` is something else, the id of an MCP session (`u.session` of another client) to continue it; a name there
is refused with that hint.

The client is the standard library only, and it holds no design state. The rules are the same as for any MCP client:

- edits go into a changeset that a person reviews and applies;
- what the tools return is logged with the client's name (`undine-cli`, `undine-python`).

Flexibility comes from composing typed tools in your script, not from running code inside the server.

### Long results

A tool's result can be long: the netlist of a cell with a hundred devices, a batch's results, a simulation with many
signals. Each reader gets as much as it can use, and a result that does not fit is shortened inside, never cut through,
so it stays valid JSON:

- What holds most of it gives way, and several large parts (the netlists of a `batch`) are shortened alike. A list
  or an object of many entries (instances by name) keeps its first entries, a text its start. Each cut says so in
  place, for example `"…": "76 more (116 in all)"`.
- The result gets `_truncated`: its full size, the budget, what was cut (`"instances": "40 of 116"`), and how to ask
  for the rest.
- The rest comes from the tool's filters and pages: `get_netlist` with `offset` / `limit`, `instances` or `nets`;
  `get_intent` with `ids` or `kinds`; `get_design_context` with the `objects` it lists first.

| Reader | Characters per result |
|---|---|
| The in-app assistant | 9000: the results stay in its conversation |
| MCP clients | 20000, or what the client asks for with `_meta: {"undine/max_chars": N}` (0: all of it) |
| `undine call` | the whole result comes from the server; it prints at most 25000 (`--max-chars N` or `$UNDINE_MAX_CHARS`; 0: all of it, for a file or `jq`) |
| Python | the whole result (`Undine(max_chars=N)` asks for less) |

The shell prints less than it receives because an agent reads the output from its own shell, which cuts long output
(Claude Code's at about 30000 characters). A script needs all of it.

### Waiting for the person or a job

`wait_for(events, timeout_s)` blocks until something happens and says what happened. It answers in text, so it works
in every client (MCP, `undine call`, Python), unlike MCP notifications, which most clients do not act on.

- `changeset`: the person applies or discards the agent's changeset.
- `selection`: they select something in Undine, for example to point at what to look at next.
- `job:<id>`: a job ends, such as a LibreLane run.
- `design:<lib>/<cell>`: the cell changes in the real design.

It returns the first event that happens, or a timeout (default 60 s, at most 300; for `job:<id>` at most 1800, and the
timeout says whether the job is still queued or running). A simulation that ended comes with how to read its results.

### Several edits as one step, and safe retries

- **`batch`** runs several tool calls in the changeset as one step: `ops=[{tool, args}, ...]`, all or nothing.
  - If one fails, the working copy is put back as it was before the step, and the error names the op.
  - The step is one operation in the review's history (one restore point).
  - Reads such as `get_netlist` can sit between the edits.
  - Up to 2000 ops. With more than 40, the answer lists the first 20 results, the warnings of the others with the
    index of their op (`warnings`), and counts the changes per cell and operation.
- **`dry_run: true`** runs the step, returns each result and what would change (cell, view, effects), and keeps
  nothing. Use it to try a change before making it.
- **Request ids.** Every write tool takes a `request_id`. Calling again with the same id returns the first result
  (marked as replayed) instead of acting twice. The ids are kept per project and survive a server restart. They are
  per `agent` too: two agents on one connection that choose the same id make two calls.
  - So when a client cannot tell whether a call went through (a timeout, a dropped connection), it simply sends it
    again.
  - The Python client and `undine call` add an id to every call and resend once the answer is lost. `--request-id`
    sets one by hand, for example to re-run a script safely.

A screen following the changeset does not see a step while it runs. After a step is put back, numbering continues
past it, so nothing is missed or shown twice.

## The agent guide (skill)

Undine ships a guide for agents as an [Agent Skill](https://agentskills.io): a short `SKILL.md` covers the loop (read,
open a changeset, edit, verify, hand over) and the rules. One reference per task covers connecting, schematic,
layout, intent, verification, and RTL / LibreLane. An agent reads only the one it needs.

- **MCP clients** get it as resources (`undine://skills/undine/SKILL.md` and its references); the server's
  instructions point there. `mcp-stdio` serves the guide even when the server is not running.
- **Claude Code and other agents with a skills folder:** `undine skills install` copies it to
  `~/.claude/skills/undine` (`--dest` for another folder).
- **From the shell:** `undine skills` lists the files and `undine skills show layout` prints one.

The files are in `server/undine/data/skills/undine/`. A test checks that every tool they name exists and every link
resolves.

## Pictures

In the layout view the chat has an **Image** button: it captures the selection area (or the visible layout when nothing is
selected), downscaled to at most 1024 px, and shows it as a thumbnail you can remove before sending (off by default).
The picture is sent with that message only; the audit log stores a hash and the file is kept under
`<project>/llm/images/` so what left the machine can be reviewed. With the EDA-server connection the model must accept images.

External agents (MCP, `undine call`, Python) can ask for pictures themselves. Each picture comes with the numbers it
shows, because the picture is for looking and the numbers are the evidence:

- **`render_schematic`**: the schematic as the editor draws it, with its counts. `around` (instance names) or `box`
  draws a part of a large schematic closer; the answer gives the box drawn.
- **`render_layout`**: the whole cell or a box in um, optionally only some layers, with the PDK's colours and a scale
  ruler. It also gives the box drawn and um per pixel. `markers: true` (or a list of rules) draws the violations of
  the cell's latest DRC runs over it, a colour per rule and checker, and returns the legend; `get_drc_report` gives
  the same markers in um, per rule.
- **`plot_waveform`**: a testbench's latest finished run. AC runs show magnitude and phase over log frequency. Each
  trace comes with its colour and range. Signals are named as in `run_simulation` (`out`, `x1.b9`), and `tstart` /
  `tstop` show a stretch of the run, such as a few nanoseconds of a microsecond-long one. A long run keeps each
  stretch's lowest and highest point, so a narrow spike stays visible.

They are drawn on the server by KLayout, which also draws the layout tiles. Over MCP they are image blocks; the
audit log counts them without copying them. `undine call` writes them to files (`--out FILE.png`, or a new file in
`--out` when it is a directory, else in `--out-dir`, `$UNDINE_OUT_DIR`, the folder of `$UNDINE_CLI_SESSIONS` when it is
set, or the current directory) and prints the paths. The Python client returns the PNG bytes as `images`. The in-app
assistant receives text only, so these tools are not offered to it.
