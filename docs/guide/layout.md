# Layout editor

| Key | | Key | |
|---|---|---|---|
| F | fit | Shift+F / Ctrl+F | all levels / top level only |
| R | rectangle | P / Shift+P | path / polygon |
| L | label | I | place instance |
| M / C | move / copy | K / Shift+K | ruler / clear rulers |
| Ctrl+C / Ctrl+V | copy selection / paste | | |
| left drag | select, or move the selection | Shift+left drag | change range selection |
| U / Shift+U, Ctrl+Z / Ctrl+Shift+Z | undo / redo | Ctrl+S | save |
| Del | delete | Esc | cancel, select tool |
| Enter / Backspace | finish the shape / remove its last point | Z / Shift+Z | zoom in / out |
| wheel | zoom | middle/right drag, Space+drag, arrow keys | pan |

Double-click an instance to open it. Edits apply to the cell's top-level shapes and instances. Undo and redo take back
the operations of the cell you are editing only, never an edit made in another cell of the library; the history is kept
on the server (in the operation log), so it survives a restart. PCell instances show parameters only.

With nothing selected, left-drag draws a selection rectangle. Once objects are selected, the next left-drag anywhere
on the canvas automatically activates **Move** and previews the selected group at its new position. Release to commit
one undoable move and return to Select; Esc cancels. A plain click on empty space clears the selection. Shift+drag
changes the range selection even when objects are already selected. Read-only views always use left-drag for selection.
Pan has no toolbar button; middle/right drag, Space+drag and arrow keys still move the view. M still activates the
explicit move tool, with a reference-point click followed by a destination click.

Ctrl+C captures the selected shapes and instances. Ctrl+V shows a preview at the pointer; click to paste or press
Esc to cancel. The copied geometry and properties survive moving or deleting the originals. Paste can target another
cell in the same project and library (including its proposal); referenced child cells must exist there. Each paste is
one undo step, and instance names are made unique. Use Ctrl+V again to paste another copy. On macOS, Cmd+C / Cmd+V
also work. Text fields keep their normal text copy/paste behavior. The toolbar offers Copy and Paste; the C key still
duplicates the current selection without a separate toolbar button.

## Grid and panes

The grid field in the status bar sets the snap grid (µm); drawing tools show a crosshair at the snapped point, and a new
grid applies at once. The library browser, the editor and its side panel are separated by handles: drag to resize,
double-click to reset (the widths are remembered per browser). With many cells open, the tab strip scrolls and shows
how many tabs are hidden on each side.

## Schematic-driven layout

The layout's side panel generates the schematic's devices as PDK PCells (**Generate** places the missing ones, **Sync**
replaces those whose parameters changed, in place) and shows the ratsnest of the nets still to route. A MOSFET's `W` in the
schematic is the total width of its `nf` fingers and `mult` counts parallel devices, so a device becomes one PCell with
`W / nf` per finger and `nf × mult` fingers. The fingers of such a device are joined into one transistor: the gate pads by a
met1 bar, sources and drains by met2 bars through via1 along the top and bottom of the diffusion (in a cell named
`<pcell>_par`); fingers too short for two met2 bars are left for routing, with a warning. The result matches LVS as
`W` with `m = mult` and is DRC clean. MIM capacitors (`cap_mim_m3_1`, `cap_mim_m3_2`) are generated too.

**Units.** A MIM capacitor's `MF` (and `m`) and a resistor's `m` are that many devices in the layout: Undine places
that many units of the one device, as one array bound to the schematic instance. KLayout's LVS combines the parallel
units into one device once they are routed. The array's pitch follows the PDK's rules, where Magic is stricter than
the KLayout deck:

- sky130 1.17 µm MIM units sit 4.17 × 2.71 µm apart, a capm 1.34 µm from the next unit's metal3 (Magic's capm.11);
- the bottom plates (capm + 0.14 µm) stay 1.2 µm apart (capm.2b).

The PDK's generators draw no MIM side smaller than 2 µm. Undine draws `cap_mim_m3_1` down to the rule's 1 µm (capm.1),
so a CDAC's 1.17 µm unit is not silently made larger. A device whose `MF` the PDK's PCells cannot repeat is listed
with the reason (`notes`), not dropped.

**Taps.** A MOSFET gets the PCell's butted bulk tie only where one of its source / drain is on its bulk net. That
source / drain is joined to the tap in li1 (the device's cell is `<pcell>_tie`), so routing another net to it is an LVS
error instead of a short in silicon: the silicide over a butted junction joins the two. Where neither is on the bulk
net (a sampling switch: `vin` / `vout`), the device gets a guard ring. A thick-oxide tie (`*_g5v0d10v5`, `*_05v0`) is
two contacts wide (0.99 µm for an nfet, 0.86 µm for a pfet), at least the 0.7 µm Magic's diff/tap.16 wants.

**Layout parameters.** A device's own layout choices are kept with its layout instance, not in the schematic, so the
next sync keeps them. For a MOSFET:

- `nf`: fingers drawn, each W × mult / nf wide. The schematic keeps its `nf`; sky130's models bin on the total W, and
  LVS combines the fingers into it;
- `bulk`: auto, tie, ring or none;
- `tap_side`: right or left;
- `gate`: where the gate contacts are;
- `sd_contacts`: contact columns per source / drain.

A unit array takes `columns`. The assistant sets them with `sync_layout(layout=...)`. A device that sync puts in place
(new parameters, another unit count) keeps its position and orientation; a unit array's steps turn with it. Devices
placed with `place_pcell` or `place_array`, even a single piece, are the designer's: sync leaves them as they are.

A device that sync swaps for another PCell variant (other `nf`, the gate on the other side) leaves the old variant in
the library, and so does a `place_pcell` piece deleted later. Nothing places those cells, so the checks skip them (a
variant alone is no design: Magic's LU.2 would fail it), and the assistant's `delete_unused_cells` takes them out of
the changeset, recorded so that a restore brings them back. It takes only such generated cells nothing places, and
by default only those the changeset made itself (it counts the baseline's and takes them when asked). A cell of the
designer's, such as a pilot that is no longer needed, goes with `delete_cell`, refused while anything places it.

**GF180MCU and IHP SG13G2.** MOSFETs are generated from their PCells the same way:

| | Devices | Width | Joining the fingers |
|---|---|---|---|
| GF180MCU | `nfet_*` / `pfet_*` (3.3, 5, 6 V), `nfet_06v0_nvt` | `W` is the total of the `nf` fingers | gates: a metal1 bar over the gate pads; sources and drains: metal2 |
| SG13G2 | `sg13_lv_*` / `sg13_hv_*` | `w` is the total of the `ng` gates | gates: a poly bar below the diffusion, contacted to a metal1 pad; sources and drains: metal2 |

Both read `m` as more fingers, and both add a tap (GF180MCU: bulk tie; SG13G2: guard ring), so the device is DRC clean
and its bulk can be routed. SG13G2's PCells draw no gate contacts, so Undine adds them, even to a one-gate device: the
gate routes, and takes its label, in metal1 like the other terminals.

Resistors and MIM capacitors are generated too, with their `W` / `L`:

- GF180MCU: `ppolyf_u`, `npolyf_u`, `nplus_u`, `pplus_u`, `nwell`, `rm1`–`rm3`, `cap_mim_2f0fF` (MIM option B under
  Metal5);
- SG13G2: `rppd`, `rsil`, `rhigh`, `cap_cmim`, with the value computed from the size.

Other devices, and GF180MCU / SG13G2 passives with `m` > 1, are listed as unsupported, with the reason; place them
from the PCell panel. For LVS,
GF180MCU's resistor and MIM symbols (written as calls to their model) are compared as R / C elements.

GF180MCU's LVS runs flat and combines the fingers of a device. The deck's hierarchical mode does not extract a pfet
inside a sub-cell, such as a PCell placed in a cell.
