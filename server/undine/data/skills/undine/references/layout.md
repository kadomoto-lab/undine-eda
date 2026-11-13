# Layout

Layouts are per-cell OASIS files. Coordinates in tools are micrometres (snapped to 5 nm). Devices are PCells of the
project's PDK, bound to the schematic's instances.

## Read

- `get_layout_info(lib, cell)` gives the size, the bound devices (with their layout parameters), the unbound instances
  with their ids (`child@x,y`, the origin in um as the grid has it) and shape counts per layer. It also compares the
  layout with the schematic: missing or out-of-date devices, `own_layout`, `notes` (what is not generated), `taps`
  (how each MOSFET's tap is to be routed) and `wiring`:
  - `wiring` comes from the drawn conductors (KLayout, flat, the LVS deck's connections) at each placed device's
    terminals. `unrouted_nets` and `wiring.unrouted` name the nets whose terminals are not joined yet, with the pins
    `apart` (`C9:c1 (37 of 512)`: units of an array); a port's label is one of its net's pins (`vout (label)`), so a
    pin left on a piece of its own shows. `shorts` names the nets that touch.
  - The substrate and the n-wells join nothing here (neither is a wire): taps count as joined only by what is drawn
    between them. `substrate_only` names the unrouted nets that only the substrate joins (`vss` with its rail
    missing); `well_only` those that only an n-well joins (two pMOS ties of `bt` in one well, no metal between them:
    route the metal). LVS matches both anyway, because the deck joins every p-tap to its substrate net and every n-tap
    to its well. Two nets whose taps share an n-well are a short (`on: nwell`).
  - A body without a tap of its own (bulk `none`) is tied when a tap of its bulk net is in the same substrate region
    (the same n-well for a pMOS) within the latch-up distance of its diffusion (sky130: 15 um, LU.2 / LU.3): a `vss`
    guard ring round it, the rail's p-tap beside it. A tap of the net is wired to another pin or the label of it (for
    a net of such bodies only, with no label: a tap wired to no other net). `body_ties` names that tap (`tap_um`,
    `distance_um`); `bodies_untied` says why a body is not tied.
  - `butted_taps` names a tap butted against a source / drain on another net. LVS and PEX do not see it, but the
    silicide over the junction shorts them in silicon.
  - It is no LVS: devices outside bound instances are not seen. It runs on sky130 only. Other PDKs answer
    `nets_between_devices` (the nets that join placed devices, routed or not).
- A cell with a schematic and no layout view: the layout tools say so; `sync_layout` creates the view.
- `get_device_geometry(lib, cell, instance, layers?, polygons?)` gives a placed device in absolute um. `instance`
  is a schematic instance name (a device placed as several `place_pcell` pieces: all of them, listed in `pieces`
  with their ids) or one piece's id `child@x,y` (bound or not; `bound_to` says to what):
  - every layer it draws (or `layers`: any layer by name, `capm`, `hvi`, `npc`, `areaid.sc`, or `"75/20"`; an unknown
    one is an error), as the exact rectangles of its merged shapes (a gate with its contact head is two rectangles),
    or with `polygons: true` their outlines; contacts and vias as arrays: count, size, columns x rows, pitch, extent
    (`irregular`, with where its columns `x` / rows `y` are, when they are unevenly spaced: a source's, a drain's and a
    tap's contact columns side by side);
  - its **terminals**: a MOSFET's `S`, `G`, `D`, `B` (the bulk with its tap), a MIM capacitor's `c0` (top plate) and
    `c1` (bottom plate), each with its shapes per layer, `schematic_net`, `net` (the net it is on in the layout now:
    its labels, then the schematic nets of the device terminals on it; `vin / vout (shorted)` when two meet) and
    `lvs` (its layout and schematic net in the latest LVS, however many runs came after it);
  - `abuts_tap` on a source / drain butted against the body tap, and a warning when that strip is on another net
    than the bulk (a junction against the tap);
  - a MOSFET's S and D are interchangeable: they are named after the schematic nets they are routed to; unrouted,
    the strip against the PCell's body tap is the one on the bulk's net (vss for an nMOS whose S is vss), so tying it
    to the tap leaves no junction of another net against it;
  - a multi-finger PCell placed as it is (`nf: 2` ...; `device.fingers`) is one device: S is the strips on one side
    of each gate in turn (the outer two of two fingers), D the others, G every gate. While a terminal's strips are not
    joined yet it has `nets` (each with where it is) and `terminals_split` says so: join them when routing.
  Read it to find a terminal before drawing to it. `route` and `add_via` take terminals as `"XMS:S"`, or one
  piece's as `"child@x,y:S"`. On a PDK
  whose stack Undine lacks, `terminals` is null and `terminals_note` says so; the layers are still given.
- `query_shapes(lib, cell, box=[x1, y1, x2, y2], layer?)` gives the cell's own shapes inside a box.

## Look

`render_layout(lib, cell, box?, layers?, size?, highlight_nets?, markers?, solid?)` returns a picture with the PDK's
colours and a scale ruler: the whole cell, or a box in um, optionally only some layers (`["li1", "mcon", "met1"]`).
- `highlight_nets: ["vin", "vout"]` draws those nets' shapes solid, each in its colour, over the dimmed layout (a net
  by its label or the schematic net of a device terminal on it). The answer gives each net's colour and layers, and
  how many unconnected pieces it is in.
- `markers: true` (or a list of rules, `["capm.11"]`) outlines the DRC markers of the cell's latest run of each
  checker (KLayout's deck, Magic), one colour per rule, found among all of the project's runs (also behind a sweep's);
  the answer's `markers` maps each rule (and checker) to its colour, count, job and `outdated` when the layout
  changed since; with none, `markers_note` says whether the runs were clean or there is no run yet. `get_drc_report`
  gives the same markers as coordinates.
- `solid: true` fills every layer solid instead of its stipple.
It also gives the box drawn and um per pixel. Look with it, and measure with `query_shapes` / `get_device_geometry`.

## Devices from the schematic

`sync_layout(lib, cell, reason, instances?, layout?)` makes the layout devices match the schematic:
- a cell without a layout view gets one;
- a device whose parameters changed gets the matching PCell in place. It keeps its origin, so a grown device can
  overlap its neighbours: the answer warns, and you move one;
- missing devices are placed at the PDK's spacing:
  - a PDK standard cell (`sky130_stdcells/inv_1.sym`, netlisted as `sky130_fd_sc_hd__inv_1`) comes from the PDK's GDS;
  - a cell of your own comes from its layout in the library (draw or import that first);
- MF / m of a MIM capacitor or resistor places that many units, as one array bound to the device;
  - sky130 1.17 um MIM units sit 4.17 x 2.71 um apart: a capm 1.34 um from the next plate's metal3 (Magic capm.11).
    The KLayout deck checks only 0.5 um there;
  - a unit count that changed is a new array at the old origin;
  - a MOSFET's `mult` becomes more fingers of one strapped device;
- MIM sides down to 1 um are drawn as asked (capm.1). The PDK generators clamp them to 2 um;
- a device placed with `place_pcell` or `place_array` (`placed_with`), in several pieces, an array of another cell, or
  a cell of your own bound to a device is left as it is (`own_layout`): redo it yourself when the schematic changes;
- a device it puts in place keeps its orientation, an array its turned steps too;
- routing is not redone.

**Taps.** A MOSFET gets the PCell's butted bulk tie only where a source / drain is on its bulk net. That source /
drain is then joined to the tap in li1 (`<cell>_tie`), so routing another net to it fails LVS instead of shorting in
silicon. Otherwise the device gets a guard ring.
- `taps` in the answer says what to route where, for example `route S there` (the side of the tie) or `guard ring:
  route it to the bulk net`.
- A thick-oxide tie (`g5v0d10v5`, `05v0`) is two contact columns wide (0.99 um nfet, 0.86 um pfet), at least Magic's
  diff/tap.16 0.7 um.

**Layout parameters** (`layout: {device: {...}}`) stay with the device's layout instance, so a later sync keeps them.
`null` takes one back.
- MOSFETs:
  - `nf`: fingers drawn, each `W x mult / nf` wide. The schematic keeps its `nf`, and LVS combines the fingers into
    its W. `XMS` with W 20 as `{"nf": 4}` draws 4 x 5 um, strapped. W / nf must be on the 5 nm grid;
  - `bulk`: `auto`, `tie`, `ring` or `none`. A tie that the nets forbid is refused. `none` leaves the tap to you
    (LU.2);
  - `tap_side`: `right` or `left` (the PCell mirrored);
  - `gate`: `bottom`, `top` or `alternating`;
  - `sd_contacts`: contact columns per source / drain.
- MIM / resistor units: `columns` of their array.

After changing a W / L / nf in the schematic, sync, then repair the routing that no longer fits.

## Devices by hand (inside a changeset)

- `place_pcell(lib, cell, pcell, params, x, y, rot?, mirror?, name?, reason)` generates a PDK PCell with the
  parameters you choose and places it:
  - unbound (a dummy), or with `name` as a piece of that schematic instance (a device split into parallel pieces,
    which LVS combines once routed). A piece is yours: `sync_layout` leaves the device as it is, one piece too;
  - pcell and params are as the PDK names them (sky130 `nfet`: `w` per finger, `nf`, `bulk`, `gate_con_pos`,
    `sd_con_col`; `mim_cap`: `w`, `l`). An unknown one is refused with the list. A variant the library has already
    is not generated again;
  - the answer gives its id and box.
- `place_array(lib, cell, devices, pattern, origin, columns?, pitch?, rows?, dummies?, dummy?, replace?, reason)`
  places the units of several devices in one grid, each unit bound to its device:
  - MIMs / resistors place their MF x m units. MOSFETs (`devices: {"M1": 4, "M2": 4}`) become one-finger units of
    W x mult / units, in one well and implant, inside a guard ring (route its li1 to the bulk net). Thick-oxide units
    have the ring and its well inside their hvi too;
  - `common_centroid` puts each device's units in pairs point-symmetric about the centre (a binary CDAC, a matched
    pair). `interdigitated` places them ABAB, each row reversed. With `rows` you give the grid yourself, top row
    first (device names, `dummy`, `''` for a free cell);
  - `pitch` defaults to the rule pitch. A device placed already is refused unless `replace`;
  - `dummies` adds rings of dummy units: unbound, which LVS sees as extra devices, or bound to `dummy`. That device
    is one of its own with exactly as many units (MF; a MOSFET's fingers), else it is refused. A CDAC's `CDUM` of
    MF 1 is one of the 1024 units, not the ring;
  - the answer gives the unit map (one character per unit, a legend), each device's units and centroid offset, the
    pitch, the box and `unit_origin_um`. A unit's id is `cell@x,y`: its lower-left corner, origin + [c, r] x pitch,
    plus `unit_origin_um[cell]`. `sync_layout` leaves the units alone.
- `delete_instances(lib, cell, instances? | box? | child?, reason)` deletes instances by id or binding (all the
  pieces), inside a box, or of a child cell; each one deleted meets all the criteria given.
- `move_instance(lib, cell, instance, ..., reason)` moves a bound or unbound instance (by its id):
  - `dx` / `dy` move it by that much;
  - `x` / `y` put its origin there;
  - `rot` / `mirror` set its orientation; the one not given stays as it is. Without x / y it turns in place (the
    centre of its box stays), then moves by `dx` / `dy`. `rot: 180, mirror: true` mirrors it left to right. An
    array's steps turn with it.
- `swap_instances(lib, cell, instances: [a, b], what?, reason)` exchanges two instances in one step (one restore
  point): `what: "place"` (default) each takes the other's origin and orientation; `what: "binding"` each stays and
  takes the other's binding (one cell's instances only). Use it to swap two bound units between bits: ids name places,
  so two `move_instance` calls would need a free spot in between. Afterwards each id names the other instance.
- `restore_changeset(to)` takes the changeset back to a restore point: `after` of an operation in
  `get_changeset`'s `restore_points`, or 0 for the changeset as opened. `get_changeset` lists the last 12;
  `restore_points_more` says how many older ones there are, `restore_before: <after>` lists those before a point and
  `restore_points: "all"` (or a number) lists more. The points count every record of the changeset
  (`restore_point_now` is where it is), `operations` those in effect. While a batch is still being applied (also one
  whose client is gone), `in_progress` says how far (`done` of `of`): wait for it to end (`get_changeset` again)
  before you edit.
- `delete_unused_cells(lib, cells?, include_baseline?, dry_run?, reason)` takes out the PCell variants Undine generated
  that nothing places any more, with `sync_layout`'s `_par` / `_tie` cells round them: a `sync_layout` that changed
  `nf` or the gate side leaves the old variant, and so does a deleted `place_pcell` piece. Without `cells` only those
  your changeset created; `baseline_unused` counts the ones that were there before it (leave them unless the user asks:
  `include_baseline: true` or `cells` takes them). `dry_run` lists them. A cell of yours, or one still placed, is
  refused. It is recorded with each cell's content (a restore brings them back). `run_checks` skips such cells anyway
  and counts them in `skipped_unused_cells`.
- `delete_cell(lib, cell, views?, dry_run?, reason)` deletes a cell of yours that is no longer needed (an emptied
  pilot): its layout, schematic and / or symbol, by default every view it has. Refused while anything places it
  (`delete_instances` first). Recorded with the content: a restore brings it back. The cells its layout placed stay
  (`delete_unused_cells` then). `get_changeset`'s `new_cells` lists every cell your changeset created and still has
  (generated variants and their `_par` / `_tie` cells too); any other cell is the baseline's.

## Assemble a block (inside a changeset)

A block such as an ADC is put together from cells: the bootstrapped switch, the comparator and the CDAC as cells of
their own, the SAR logic from standard cells or a LibreLane run.

- `create_library(name, reason)` adds a library to the project, for a block or a generator's cells.
- `place_cell(lib, cell, child, x, y, reason)` places a layout cell, its origin at x, y (um):
  - a cell of `lib`; a cell of another library with `child_lib` (copied in with the cells under it; refused when
    `lib` has a cell of that name that differs, a cell under it included; a cell under it that `lib` has with other
    content is copied as `via$1`, listed in `renamed`); or a PDK standard cell by name (`sky130_fd_sc_hd__nand2_1`);
  - `rot` 0 / 90 / 180 / 270 and `mirror` (about the x axis, before the rotation);
  - `columns`, `rows`, `pitch_x`, `pitch_y` make one regular array instance (unit capacitors; at most 1048576
    elements): one instance in the log and review, every element in LVS. Elements of different schematic instances
    (common centroid) need separate arrays or instances;
  - `name` binds the instance to a schematic instance, as `sync_layout` does. A bound array, or a cell that is not the
    device's PCell (unit capacitors bound to the schematic's MIM capacitor), is your own layout: `sync_layout` never
    replaces it. LVS compares the extracted devices, not the binding, so give the schematic as many (e.g. `MF` = the
    number of unit capacitors). The answer warns when the schematic has no such instance, and what sync will do.
    Without `name` the instance is unbound (standard cells of a logic block).
- `import_layout(lib, path | job, reason, replace?)` brings in the cells of a GDS / OASIS file: one a generator wrote
  (under the server's import directories) or the final GDS of a `run_librelane` job. New cells are added, identical
  ones stay. A cell the library has with other content is refused, and the error names it: pass `replace` with those
  names to overwrite them (recorded shape by shape), or import into a library of its own and place from there.
- `create_library`, a copy and an import are taken back by restoring the changeset to a checkpoint before them.
- Thousands of shapes: `batch` takes up to 2000 ops per step (about 4 s for 2000 `add_box`). Its answer is grouped at
  any size: consecutive ops of one tool that answered the same are one result (`"op": "0-35"`), and what it changed
  is one entry per reason (one, unless ops give their own): its operations counted and per cell its effects counted
  (`"add box met1 ×20"`, an instance by name and place, a moved one from where to where). Past 40 distinct results it
  keeps the first 20 and every later warning (`warnings`, by op index). A regular arrangement is one `place_cell`
  array, not thousands of instances.

## Draw (inside a changeset)

Route with the rule-correct tools below; they know the PDK's stack and refuse, saying what and where, what would
touch another net or break a spacing (also a notch narrower than the spacing that a new pad leaves in its own
net's metal, and a cut partly over or against a cut already there, of any net: the two would be one shape, not a
cut; a via exactly on one is the same cut). sky130 has all of them; GF180MCU (its A / B / C, D metal stacks: metal1 up to
its top metal, the 30K top metal of A excepted) and IHP SG13G2 / SG13CMOS5L have `route`, `add_via` and the
terminals, not `add_tap` / `guard_ring` (their taps are diffusion with an implant: draw those with `add_box`); another
PDK says it has none. The nets come from a connectivity extraction
of the layout as it is: labels, and the schematic nets of the bound devices' terminals, so an unlabelled net that
only joins devices has its name too. Each takes `dry_run: true` (what it would draw, drawing nothing; it needs no
changeset) and goes in a `batch` like any edit.

- `route(lib, cell, from, to, layer, width?, net?, through?)` draws a wire on one layer between two ends: a point
  `[x, y]` (on the shape there, of the route's layer or the nearest layer below it, else free), a terminal
  `"XMS:S"` / `"XCB:c0"`, or a net by its label or schematic name (`"vin"`: its nearest shape). An end with no shape
  on the layer gets a via stack from its nearest layer. The way is the first of straight, the two L's, the Z's and
  then ways round what was in their way, that touches no other net and keeps the spacings (the larger one where
  either side is a wide metal: the part of a shape wider than 3 um, as the decks find it, so a comb of narrow bars is
  not wide); a way round never runs past an end and back. On poly it never crosses a diffusion or tap (that would be
  a new transistor) and keeps 0.075 / 0.055 um from them. An end on a terminal or net leaves it at the point nearest
  the other end (with `through`, nearest the first / last through point), kept inside its shape by half the wire or
  the cut, or on the centre line of a strip no wider than that (a PCell's 0.17 um source, drain or tap strip). Every
  point and bend is on the 5 nm grid. A via at an end has its pads along the way it leaves; when they would come too
  close to a neighbouring strip and turned a quarter they fit, they are turned (`vias[].pads` says whether that is
  along the strip it lands on or across the way it comes in).
  `through: [[x, y], ...]` leads it through points (each leg horizontal first). It is refused when the ends are on
  different schematic nets (or not on `net`), or one net already (a net in pieces is routed from a piece the other
  end is not on, a terminal whose strips are not joined yet from a strip the other end is not on, and two ends
  on the same few nets from one of them to the others: route to `"XMS:S"` again till it is refused as one net; two
  ends only an n-well joins are not one net: it draws the metal, and `note` says so), or no way is clear: the error
  says what is in the way, where. `width` defaults to the layer's
  minimum. The answer gives the net, the points, the vias and the length.
- `add_via(lib, cell, x, y, from_layer, to_layer, rows?, cols?, size?, direction?, net?)` draws a via stack, either
  way up (sky130: `diff`, `tap` or `poly`, `li1`, `met1` ... `met5`): each cut an array, `rows` x `cols` at every
  level or as many as fit in `size: [w, h]` (default one), each conductor a pad with the enclosures (the larger "one
  of two adjacent sides" value on two opposite sides, along `direction` `h` / `v`; default along the shape it lands
  on), minimum width and area, and npc around a poly contact. npc closer than 0.27 um to another npc (a neighbouring
  gate's contact, a PCell's own) is bridged to it, as the decks want (npc.2; the fills are in `bridged`), and refused
  where no fill closes the gap (corner to corner) or a fill would come within 0.09 um of a diffusion or tap; `route`
  does the same for the poly contacts at its ends. A contact on a diffusion or tap needs that there (it is not
  drawn). Refused when it would join two nets (with `net`, touch a net not called `net`) or break a spacing.
- `add_via` and `route` also refuse a via Magic refuses next to a placed cell's own vias, which the KLayout deck
  passes: Magic reads a via as a contact (the cut grown by its enclosure: via3 0.06 um, via4 0.19 um), the cuts
  of an array as one contact, which may not abut or partly overlap one of its kind in a subcell ("can't abut or
  partially overlap between subcells": put the same via, the same array, exactly on it, or clear of it; a single via
  on one cut of a 2x2 array is refused), and a MIM capacitor's own contacts (sky130's via3 on capm) stack with no via at
  all ("Can't overlap those layers"). So a via4 on a MIM's top plate over its via3 array is refused: lead the plate's
  met4 out past the capm and put the via4 there, or join the top plates in met4.
- `add_tap(lib, cell, kind, net, box | x, y, metal?)` draws a substrate (`ptap`, psdm) or n-well (`ntap`, nsdm and
  the n-well) tap: `box` is the tap, `x, y` the smallest one; contacts along it, li1 and (`metal: met1`, the default,
  or a metal above it) the vias and straps up to that metal. `guard_ring(lib, cell, kind, net, around, width?,
  spacing?, metal?)` draws a closed ring around instances (`["XM1", "XM2"]`: the extent of every piece bound to them,
  or ids, plus `spacing`, default
  0.505 um on sky130, so that the ring's implant is 0.38 um from the devices' own, which reach their extent) or a box
  (`[x1, y1, x2, y2]`: the ring starts at its edge); `width` defaults to the narrowest ring that holds a row of each
  via up to `metal` (sky130: 0.25 um to met1, 0.26 to met2, 0.33 to met3 / met4, 1.6 to met5), and a narrower one is
  refused; an ntap ring's n-well covers what it encloses, so it encloses pMOS only. Both are refused against a
  diffusion; touching another net, also through their n-well (an ntap ring round a pMOS whose bulk is another net); a
  tap spacing or poly; the other implant; an implant of their kind closer than 0.38 um (merge with it or keep away);
  an n-well closer than 1.27 um to another one, or than 0.34 um to an n+ diffusion. Route them to their net (or label
  it): `net` says which it is for.
- sky130 rules they apply (um): licon 0.17 / 0.17 space, diff 0.04 (0.06), tap 0.04 (0.12), poly 0.05 (0.08) and npc
  0.1, li1 0 (0.08), kept 0.055 from a gate (a poly contact 0.19 from diffusion); mcon 0.17 / 0.19, met1 0.03 (0.06);
  via 0.15 / 0.17, met1 and met2 0.055 (0.085); via2 0.2 / 0.2, met2 0.04 (0.085), met3 0.065; via3 0.2 / 0.2, met3
  0.06 (0.09), met4 0.065; via4 0.8 / 0.8, met4 0.19, met5 0.31; minimum areas li1 0.0561, met1 0.083, met2 0.0676,
  met3 / met4 0.24, met5 4 um²; met1 / met2 0.28 and met3 / met4 0.4 where either is wider than 3 um; poly 0.075 from
  diffusion, 0.055 from a tap; a tap 0.27 from diffusion and taps, its implant 0.125 around it and 0.38 from another
  of its kind, an ntap's n-well 0.18 around it, 1.27 from another n-well and 0.34 from n+ diffusion, a ptap 0.13 from
  an n-well. Not drawn by them, for placing MIM
  capacitors: two bottom plates (KLayout takes capm + 0.14) 1.2 um apart, so capm 1.48 apart (capm.2b); other met3
  1.2 um from a bottom plate (capm.2b_a), and Magic wants capm 1.34 um from met3 that is not its own (capm.11).
- GF180MCU and IHP SG13G2 rules they apply: their KLayout decks' contact and via sizes, spacings (an array's) and
  enclosures, metal widths, spacings (wide-metal ones too), areas, poly from diffusion and a contact from the gate;
  on GF180 the larger of the 3.3 V and 5 / 6 V values, so a shape is correct inside DualGate and outside it.
- `add_box(layer, box)` draws a rectangle, and `add_path(layer, points, width)` a wire, for what the tools above do
  not draw. Layers are named (`met1`, `li1`, `poly`) or given as `layer/datatype` (`68/20`). Respect the PDK rules.
  The answer is `{ok: true}`, with `snapped_to` when the 5 nm grid moved a coordinate.
- `add_label(layer, text, x, y)` puts a pin or net label on a label layer (`met1.label` = 68/5, `li1.label` = 67/5).
  LVS uses these names, so label every port.
- `move_instance(instance, dx, dy)` moves a device. Attached wires do not follow.
- `delete_shapes(box, layer?)` deletes the cell's own shapes inside a box (at most 40 per call); instances go with
  `delete_instances`.

## Then check

Run `run_checks` after editing: KLayout's DRC deck, Magic's DRC on sky130, GF180MCU and IHP (rules the deck lacks,
such as sky130's MV butted tap and MIM spacings), LVS and its ERC. `get_drc_report` says where each violation is. The ERC warns of a body tap
butted against a source / drain of another net. A PCell's `bulk tie` butts its tap against one source / drain, so
put the body net's terminal on that side (or use a tap that does not abut). When LVS reports odd device sizes, use
`check_device_integrity`: a shape drawn into a device's transistor area changes the extracted W / L. See
[verification.md](verification.md).
