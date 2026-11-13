# Schematic editor

In each library row, **New Cell** is followed by **Import existing cell**. The import form lets you select
Schematic (`.sch`), Symbol (`.sym`), or Layout (`.gds`, `.oas`, `.oasis`) and enter a file path on the server.
Schematic and symbol files use Xschem format; their filename becomes the cell name. Existing views are not
overwritten. These two import types support Undo/Redo and can be applied from a proposal. Referenced symbols
must be available in the project's symbol libraries. Save any pending layout edits before importing a layout.

| Key | | Key | |
|---|---|---|---|
| F | fit | W | wire (pin/wire click, Enter / Esc finishes) |
| I | place a symbol (picker) | R / X | rotate / mirror |
| L | place a net label | | |
| Ctrl+D | duplicate | Del, Backspace | delete |
| Ctrl+C / Ctrl+V | copy selection / paste | | |
| left drag | select, or move the selection | M | move tool |
| U / Shift+U, Ctrl+Z / Ctrl+Shift+Z | undo / redo | Esc | cancel, select tool |

With nothing selected, left-drag draws a selection rectangle around devices, wires and net labels. Objects fully
enclosed by the rectangle are selected. With a selection, the next left-drag anywhere on the canvas automatically
activates **Move** and previews the selected group with its attached wires and labels. Release to commit and return
to Select; Esc cancels. Shift+drag always toggles objects in the range selection, and a plain click on empty space clears
the selection. Read-only views always use left-drag for selection. Right/middle drag or Space+drag pans the view.
M or **Move** also activates the explicit move tool; Esc returns to selection.
Then use Ctrl+C to copy and Ctrl+V to preview the copy at the pointer.
Click to paste, or Esc to cancel. The snapshot keeps the copied geometry and properties even if the originals move
or are deleted. You can paste into another schematic cell in the same project and library, including its proposal.
Each paste is one undo step; instance names become unique while net label names remain unchanged. Same-name labels
therefore connect to the same net. Ctrl+V can paste the snapshot repeatedly; Ctrl+D still duplicates the current
selection. The toolbar offers Copy and Paste without a separate Duplicate button.
Cmd+C / Cmd+V also work on macOS, and text fields keep normal text copy/paste.

Use **Place net label (L)** in the toolbar, enter the net name, then click a pin or wire to place it.
The label snaps to the connection and follows the pointer before placement. Click again to place the same label
elsewhere, use R / X to rotate / mirror, or Esc to finish. Labels with the same name connect to the same net.
Select a placed label to edit its `lab` attribute in **Props**; each placement can be undone.

In wire mode, click a pin to start. Clicking empty space fixes a waypoint and keeps the connection in progress;
clicking a pin or an existing wire completes it. Pins snap at their exact coordinates, even off grid. The wire tool
stays active so the next click can start another connection. Enter or Esc ends the current connection.
The dashed preview and committed wire use the same orthogonal router, which avoids device bodies. Click waypoints
to guide the route. Each committed section, including its automatic bends, is one undo step.

Moving, rotating or mirroring a device keeps its existing pin connections by rerouting attached wires. Shared
junctions, branches, ports and wire labels remain connected. Old bends and junction positions can relocate to
accommodate the moved device. Net labels attached to its pins follow the device; labels on wires follow the moved
or rerouted conductor. Their names stay unchanged, and external ports keep their positions. While using Move, the editor
previews the moved device, wires and labels; release to commit, or press Esc to cancel. Previewing does not change
the design file or Undo history. The move and its wire changes form one undo step;
duplicating a device leaves the original connections in place. If a safe route cannot be found or a move would
change the circuit's connectivity, the edit fails without changing the design.

A T junction is connected and marked with a dot. Wires crossing in their interiors without a pin or wire endpoint
at the crossing are not connected. Routing avoids introducing unintended junctions.

Cells are created with the **+** (**New cell**) button of a library: a name and the views to create, schematic, symbol
and / or layout (also for an existing cell, to add a view). A new symbol gets its pins from the cell's schematic ports
(inputs on the left, outputs and in/outs on the right, in the subcircuit's pin order).

Schematic edits are written to the `.sch` file immediately. The toolbar groups **Fit → Redo → Undo**.
Ctrl+S shows the automatic-saving notice. The side panel has **Props**,
**Netlist** (Undine, Xschem or vlsirtools backend) and **Simulate**: either the schematic's own `.control` commands, or a
.tran / .ac / .dc / .op analysis set in the panel, which then replaces the schematic's `.control` block for that run (the
job says so). Transient and DC results are plotted, AC as magnitude / phase, an operating point as a table of values.
A testbench needs the PDK's models: place its corner symbol (`sky130_fd_pr/corner.sym`, choosing the corner) or a code
block with `.lib`; without either, the typical corner is included and the job says so. A testbench whose intent names
stand-ins (a behavioural model in place of a slow cell) runs with them, and the job lists them; see
[Stand-ins](verification.md#stand-ins).

## The schematic and its netlist

A schematic is the drawing of a circuit; the circuit itself is the cell's **netlist** (`<cell>.netlist.yaml`), which
every edit of the drawing keeps up to date and which the netlisters, LVS and the agent read
([design](../design/netlist.md)). Two things show where the two differ:

- **Connections not drawn** (dashed lines with the net name, and *N connections not drawn* in the status bar) are in
  the netlist but have no wire yet, for example a connection a tool made. They count in simulation and LVS. Draw a
  wire to a pin and it replaces the dashed line; nothing is ever wired for you.
- **Symbols placed by tools** go to free space next to your drawing; what you drew does not move. Once you move,
  turn or mirror such a symbol it is yours. *Tidy* in the status bar re-places only the symbols tools placed that
  have nothing drawn to them.
- **Supplies given as parameters.** The standard cells of `sky130_stdcells` take their supplies as instance
  parameters (`VPWR=vdd VGND=vss VPB=vdd VNB=vss`), not pins: the netlist connects them by net name. A parameter
  naming a net that nothing of the schematic is on (no pin, port or label, no node of a code block such as
  `Vdd vdd 0 1.8`; one left at its default `VPWR`, a misspelt `vdd`) is listed among the schematic's warnings, and
  the netlist's. Names compare without case, as in SPICE.

Moving symbols and wires changes the schematic file only; the netlist changes only when the circuit does.

## Symbol editing

Open the cell's **symbol** view to edit its drawing and pins. From a schematic, **Create symbol from schematic**
generates a starting symbol from the existing ports; **Open symbol** opens an existing one. The library's **New cell**
dialog can also create a standalone symbol, with an initial outline and no pins.

Use **P** to place pins, **L** for lines, **B** for rectangles, and **T** for text. Set a new pin's name and direction
before clicking to place it. Click a pin or graphic to edit its properties; drag to select a group, then drag the
selection to move it. Delete removes the selection. Pin names, directions and terminal order can be changed in
**Props**; the pin list's arrows change netlist order without moving the pins. Pin order is the file order of the
symbol's terminal boxes, not their position on the drawing or an imported `pinnumber` attribute.

Moving a pin updates the wires in schematics that use that exact symbol, including rotated or mirrored instances
and consumers in another project library. Their net connections stay the same. Embedded copies and a different
symbol with the same filename are independent. Open schematic views refresh after the edit. If the move cannot
preserve the connections, or would require rewriting a read-only schematic, the whole edit is refused.

The symbol and its dependent wire updates are one Undo / Redo operation, accessible from any participating view.
Undo later edits to those views first. Newly added connected instances can also prevent an older symbol operation
from being undone or redone; the error identifies the affected schematic. Applying or reverting a proposal uses the
same checks and refuses a conflict without leaving some libraries updated and others unchanged.

When a symbol is already instantiated, its pin order and directions can change and pins can be added: the circuits
that use it connect by pin name, so they stay as they are (a new pin starts unconnected, and must not land on their
wiring). A pin one of them connects (drawn, or a pending connection of its netlist) cannot be removed or renamed, and
the symbol's type cannot change; those need the consuming circuits changed first. The netlists of all the consuming
circuits are read again with the new pins, after an edit as after its Undo / Redo, a proposal's restore or its Apply.

Edits are written to `.sym` immediately and support Undo / Redo. In a proposal, they stay in the working copy until
Apply. Dependency libraries, the proposal baseline and a proposal currently being edited by an agent remain read-only.

## Schematic–symbol consistency

Open **Check** from either the schematic or symbol view and run **Check schematic / symbol**. The check compares
terminal names, input/output directions and netlist order. It also reports missing views, unnamed or duplicate
terminals, and unresolved symbols that prevent the schematic's ports from being identified reliably.

Like LVS, each check has a saved result and history. It captures both source views and the effective interfaces when
started; later edits do not rewrite old results. A changed interface marks an old result as outdated, with a rerun
action. Moving graphics or adding comments does not invalidate an otherwise unchanged interface. No PDK or external
verification tool is needed. This check verifies the cell's interface; use LVS separately to compare layout connectivity.
