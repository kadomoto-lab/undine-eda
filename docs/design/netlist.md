# The netlist view (design)

## Goal

Make the circuit, not its drawing, the core of a cell, the way the article on a free alternative to OpenAccess asks:
OpenAccess has an API for schematics and none for netlists. Undine keeps the schematic (people draw circuits, and
Xschem reads the files), but the connectivity becomes a view of its own with its own API, and the schematic becomes the
layer that draws it.

## Decisions

| Question | Decision |
|---|---|
| A new concept "connectivity"? | No. It is the cell's **netlist**: `<cell>.netlist.yaml` next to `<cell>.sch`. SPICE and Verilog netlists are exports of it |
| Who writes it? | Editors write both views: every edit of the drawing is read back into the netlist. The netlist API changes connections without drawing them |
| Who reads it? | Everything that needs the circuit and not the picture: the SPICE / VLSIR netlisters, LVS, schematic-driven layout, the agent's view of the circuit, the cell page. The editor and cross-probing read the drawing |
| Git | The drawing is written in a deterministic order (wires and graphics sorted; instances keep their order: pin order is the subcircuit's pin order). The netlist holds connectivity only, so moving a symbol changes `<cell>.sch` and not `<cell>.netlist.yaml`; changing the circuit shows as its own diff |
| Names of unlabeled nets | Kept across edits: a net of the drawing takes the name of the previous net it shares the most pins with |
| A schematic changed outside Undine (Xschem, git) | The server watches the projects' schematics (polling, about once a second) and reads the drawn connections back into the netlist right away; pending connections stay. The import is recorded in the operation log (`import_drawing`, actor `external`; not undoable). Whatever reads the netlist also checks it first |
| A pending connection and Xschem | Xschem only sees what is drawn; Undine's netlisters read the netlist, which is the reference |
| Read-only libraries (a PDK, a pinned dependency) | Never get a netlist file, even when the disk allows it (a PDK installed in the home directory is writable). Their netlist is read from the drawing and kept in memory. The PDK trees and every read-only library are registered (`netlist.protect`), and the single writer refuses them |

## When the connectivity changes from outside the GUI

The drawing follows rules that keep "the picture I drew stays as I drew it":

- **Nothing drawn moves.** A tool that adds an instance (the netlist API, the agent without coordinates) places it in
  free space next to the drawing.
- **Undrawn connections stay pending.** A connection made through the netlist is not drawn; the schematic shows it as a
  ratsnest (a dashed line with the net name) until someone draws a wire. Drawing it replaces the pending connection; a
  pin drawn to another net takes that net (the drawing wins). The agent's connect tools still draw wires; a net they
  cannot draw becomes pending instead of failing.
- **Every instance knows who placed it.** Tools mark what they place (`undine_placed=auto`, a drawing attribute that is
  not part of the netlist). Once a person moves, turns or mirrors it, the mark goes and no tool moves it again. *Tidy*
  (an explicit action) re-places only marked instances that have nothing drawn to them.
- **A netlist without a drawing is drawn once.** When a cell has a netlist and no schematic (written by a tool, a
  synthesised one), opening it draws it: ports as pins, instances placed in free space, every connection pending. From
  then on it is an ordinary schematic.

## The file

```yaml
version: 1
ports:                          # in the schematic's order: the subcircuit's pin order
  - {name: A, type: ipin}
  - {name: Y, type: opin}
instances:                      # in the schematic's order
  M1:
    symbol: sky130_fd_pr/pfet_01v8.sym
    props: {L: '0.15', W: '2', model: pfet_01v8, nf: '1'}
    pins: {D: Y, G: A, S: VDD, B: VDD}     # in the symbol's pin order
code:                           # simulation / netlist commands, in order
  - {name: s1, symbol: devices/code_shown.sym, props: {value: .tran 1n 1u}}
pending: {R1.M: VSS, R1.P: Y}   # in the netlist, not drawn yet
```

Edits of pending connections are recorded in the operation log (view `netlist`), with undo, changesets and review
like any other view. Projects of format 2 get a netlist for every schematic when they are opened (format 3).

Some symbols take connections as parameters rather than pins: the standard cells of `sky130_stdcells` list
`VGND VNB VPB VPWR` in their Xschem `extra` attribute and write them as nodes of their netlist format. Those are
connections by net name (`supplies` in the API, `supply_pins` for the agent); they stay parameters in the file. One
naming a net that nothing of the schematic is on (no pin, port or label of the drawing, no node of a code block's
element or `.global` line; compared without case, as SPICE does) is a warning of the netlist (and of the schematic
editor): netlisted, it would be a node of its own and the supply pin would float.

A simulation can netlist a cell as another one with the same pins in the same order: a **stand-in**, such as a
behavioural model of a slow block (`substitute`; see [Stand-ins](../guide/verification.md#stand-ins)). The SPICE
netlister then gives every instance of the cell, at any depth, the stand-in's symbol and descends into the stand-in's
netlist view instead of the cell's. Nothing is written: the cell's own netlist, its LVS reference and its layout stay as
they are. Subcircuits are named by cell, so with stand-ins the netlister keeps which cell is behind each name: a
stand-in with the name of another cell netlisted there is refused rather than merged with it, and so is a call back
into a cell it is still inside of when the loop passes through a stand-in (one that contains the cell it replaces, at
any depth).

The same holds without stand-ins. Two cells of one name from different libraries (a project's `inv` and a dependency's
`inv`), or a subcell from another library with the top cell's name, would share one subcircuit. The netlister keeps
which schematic is behind each name and netlists a second one of that name: when its lines are the same it is written
once, otherwise the netlist is refused, naming both cells and the instance that places the second. The vlsirtools and
Xschem backends refuse the same; for Xschem, the cells are found in Xschem's order (the search path first, the placing
schematic's directory only when the path has none), before Xschem is run.

## API

| | |
|---|---|
| `GET /api/p/{p}/l/{lib}/c/{cell}/netlist/model` | the netlist, its pending connections, its supply parameters (`supplies`) and their `warnings` |
| `POST /api/p/{p}/l/{lib}/c/{cell}/netlist/edit` | `connect` / `disconnect` (pending), `add_instance` (placed in free space, pins pending on the given nets), `tidy`, `undo`, `redo` |
| `GET /api/p/{p}/l/{lib}/c/{cell}/netlist?backend=...` | the SPICE (or VLSIR) export, from the netlist |
| agent | `get_netlist`; `add_instance` without coordinates; `connect_pins` / `connect_nets` (pending when a net cannot be drawn; `move` takes a drawn pin to another net); `disconnect_pins`; `label_net`; `tidy_schematic` |

## Not yet

- Synthesis results and other netlists without a drawing are drawn by the rule above, but no importer writes them yet.
- Moving a drawn connection through the netlist API is refused ("change the wire in the schematic"). In the drawing,
  a pin is taken off its net by removing the wires from it up to the first junction or other pin (and a label on it),
  re-routing what that cuts apart of the rest of its net (the agent's `disconnect_pins`, and `move` of
  `connect_pins` / `connect_nets`); the GUI has no command for it yet.
