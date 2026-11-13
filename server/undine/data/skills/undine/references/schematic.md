# Schematic

A schematic is Xschem's `.sch`. Its connectivity also lives in the **netlist view** (`<cell>.netlist.yaml`), which is
the truth: netlisting, LVS, schematic-driven layout and your context read it. The drawing is how it is shown.

## Read

- `get_netlist(lib, cell)` gives the ports in order, and each instance's symbol, parameters and pin -> net. It also
  lists `pending` connections (in the netlist, not drawn yet) and the code blocks' text under `code` (a testbench's
  `.meas`, `.control`, `.options`).
  - Parameters left at their symbol's formula defaults (sky130's `ad`, `as`, `pd`, `ps`, `nrd`, `nrs`) are left out;
    `all_params: true` lists them.
  - A large cell: `offset` / `limit` page through the instances (`shown.next_offset` is the next page),
    `instances: [names]` picks some, and `nets: [names]` gives the instances on those nets with each net's pins
    (a standard cell's supply parameter as `xa:VPWR`) and the code blocks naming them (`Vdd vdd 0 1.8`).
  - Standard cells (sky130_stdcells) take their supplies as parameters (`VPWR=vdd VGND=vss VPB=vdd VNB=vss`): they
    show as `supply_pins`, and `warnings` (first in the answer) names a supply parameter that nothing of the schematic
    is on: no pin, port or label, no node of a code block (left at its default `VPWR`, or misspelt).
- `get_drawing(lib, cell, net?, instances?, region?)` gives how it is drawn: the instances whose extents overlap
  (first), each instance's position, rotation, mirroring and extent (its texts included), and each net's wire ids,
  labels and pins. A large drawing comes in less detail; narrow it with `net`, `instances` or `region` ([x1, y1, x2,
  y2]). A wire's id is its end points, `x1,y1,x2,y2` with the ends sorted.
- `get_design_context(lib, cell, objects?)` adds neighbours, checks, recent changes and the intent summary. The
  objects come first: names (`["M1", "out", "MEAS"]`: an instance, a net, or a code block with its text) or
  `{kind, id}`. `overview: false` leaves out the other devices.
- `list_symbols(query?, lib?)` finds placeable symbols. `list_symbols(ref=...)` gives a symbol's pin names, pin
  offsets and default parameters. Read it before placing.

## Look

`render_schematic(lib, cell, size?)` returns a picture of the schematic as Undine's editor draws it, with its counts.
It shows placement, crossings and readability. What is connected comes from `get_netlist`, not the picture. In a
large schematic, `around: ["xgn2", "xseq2"]` zooms to those instances and `box: [x1, y1, x2, y2]` (schematic units,
y down) to a region; the answer gives the `box` drawn.

## Build and edit (inside a changeset)

- **Placing devices.**
  - `add_instance(lib, cell, symbol, reason, x?, y?, rot?, flip?, params?)` places one symbol. Without x / y it goes
    to free space next to the drawing, so nothing drawn moves.
  - `flip` mirrors left to right, for example the right half of a differential pair.
  - `add_instances(instances=[{symbol, x, y, rot?, flip?, params?}, ...])` places several in one step.
- **Parameters.** `set_instance_params(instance, params={"W": "1.5"})` sets values as strings, as in the schematic.
- **Connecting.**
  - `connect_pins(net, pins=["M1:D", "M3:D"])` and `connect_nets(nets={name: [pins]})` connect by net name. No
    coordinates are needed.
  - With `style` auto (the default) they draw routed wires as a person would, and use labels for supplies, ground
    and large nets.
  - Place the devices first with room between them: signal left to right, supplies on top, ground at the bottom.
  - Connect short local nets first and supplies last. `connect_nets` is all or nothing.
- **Ports.** Place `devices/ipin.sym`, `opin.sym` or `iopin.sym` with `params={"lab": "<net>"}` near the net. The same
  name joins it.
- **Raw wires.** `add_wire(x1, y1, x2, y2, label?)` draws one segment, in schematic units (10 per grid step).
- **Deleting.** `delete_objects(objects=[...])` removes instances and wires: `{kind: instance|wire, id}`, an
  instance's name alone (`["M5", "M6"]`), or a wire id from `get_drawing` as it is (`"0,-20,0,40"`).
- **Tidying.** `tidy_schematic` re-places what tools placed that has nothing drawn to it. It never moves what a
  person placed.

## Re-wiring and rearranging

- **A pin to another net.** A pin drawn to a net is refused by `connect_pins` / `connect_nets` for another one. Pass
  `move: true` to take it off its net first, in the same step; or `disconnect_pins(pins=["x5:X"])` alone. The wires
  from the pin up to the first junction or other pin go, a label on it goes, and the rest of its net stays connected
  (re-routed where that cut it apart) and keeps its name. Inserting a stage: `connect_nets(nets={"a": ["x5:X",
  "xd:A"], "b": ["xd:X", "xr:B"]}, move: true)`.
- **A long wire.** `label_net(net, wires?)` replaces the net's wires (the ids you give, or all of them) by net labels:
  what is left dangling goes too, and each piece of the net gets a label on a pin. The circuit stays the same.
- **Moving symbols.** `move_instance(view: "schematic", instance or instances, dx, dy | x, y)` moves symbols
  (snapped to the grid). The wires on their pins are re-routed; when that is not possible nothing moves. Use
  `get_drawing` first: its `overlaps` names what collides (a long source value over a code block).
- The router keeps wires on the 10-unit grid, and a bend or a junction more than one grid step away from other nets'
  pins and wires, so nothing reads as connected that is not. A pin it cannot reach that way gets a label.

## Pending connections

When a connection exists in the netlist but is not drawn, it is **pending**: Undine shows a dashed ratsnest with the
net name, and the netlisters use it. Drawing a wire replaces it. Do not leave a schematic full of pending
connections when the user asked for a drawn schematic: connect them with `connect_pins` / `connect_nets`.

## Symbols and new cells

- **New cells.** `create_cell(lib, cell, reason, views=["schematic", ...])` creates one in your changeset.
- **Order.** Give the schematic its ports before creating the symbol, so the symbol's pins follow them. The port
  order is the subcircuit's pin order: `set_port_order(order=[every port once])` sets it, and the symbol's pins take
  the same order in the same step (nothing else of the symbol changes: `sync_symbol` for that).
- **Keeping the symbol in step.** `check_symbol` compares the symbol with the ports: missing, extra or duplicate
  pins, directions, pin order (`pin_order_mismatch`), and sides against the intent's port sides; `get_design_context`
  and `run_checks` (kind `schematic`) report a mismatch too. `sync_symbol(sides?, regenerate?)` makes the symbol
  follow: order, directions, pins added and removed, and sides (`{"vdd": "N", "vss": "S"}`, else the intent's
  `set_port_intent` sides). A hand-drawn symbol keeps its drawing; a plain box is redrawn at the size its pins need.
  Schematics that place the symbol keep their connections: a moved pin's wiring follows it, and a pin a consumer
  connects (drawn or pending) cannot be removed. Do not delete and re-add port instances or symbols to reorder them.
