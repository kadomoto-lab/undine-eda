# Tapeout

The rocket tab of the layout editor holds what a shuttle asks for. Pick the target, check the layout, run the sign-off
suite, and export the bundle. For a walk-through from a finished cell to the bundle, see the tutorial
[Tape out on Tiny Tapeout](../tutorials/tiny-tapeout.md). The targets are:

- **Tiny Tapeout - SKY130 analog**
- **wafer.space - GF180MCU die**
- **ChipFoundry - Caravel user project (SKY130)**
- **Layout hygiene only (any shuttle)**

## Tiny Tapeout - SKY130 analog

Its layout checks mirror the precheck of `tt-support-tools` (Apache-2.0) using the official pin table (`server/undine/data/tt_sky130A_analog.json`):
exact 1x2 / 2x2 project area with a `prBoundary` (235/4), every template pin and power stripe on met4 (71/20) and its pin purpose (71/16)
at its fixed position, `VGND` / `VDPWR` met4 stripes (>= 1.2 um, within 10 um of the bottom and top edge), no pin overlaps, no Metal 5,
`ua[n]` wired to metal exactly when `analog_pins` says so and described in `info.yaml`, nwell-to-urpm spacing.

- **Sign-off suite:** every KLayout deck of the PDK (including the ones cf-precheck runs), Magic DRC, and LVS with KLayout
  (flat, substrate `VGND`) and with Magic + Netgen. The panel lists each check with its latest result and whether that result
  is for the design as it is now (`outdated` with what changed, or `not run`); running the suite starts only the checks
  that are outdated or missing, and keeps the ones that already ran on exactly these inputs (design, PDK, deck, tool
  versions, options). **Rerun all** runs everything again.
- **Export bundle** (`<project>/tapeout/<target>/<top>-<time>/`, also as a zip): `gds/<top>.gds` with the top cell renamed,
  `lef/<top>.lef` with all template pins, `src/project.v` and `gds/<top>.v` port stubs, `info.yaml`, `docs/info.md`, and
  `report.json` with check results, PDK and tool versions and SHA-256 of every file. The export is refused while a check fails.
  `report.json` also holds the content hash of the exported cell (`layout`) and the sign-off status (`signoff`): for each
  check the job, its result, and whether it ran on the exported design (`fresh`) or what has changed since.
- Yosys syntax checking and the shuttle's own precheck are not part of Undine: they are reported as `manual`, never `pass`.

`examples/make_tt_inverter.py` builds a tile around the demo inverter (`tt_um_undine_inv`: inverter from `ua[0]` to `ua[1]`,
power stripes, via stacks) that passes all of the above. `tt_um_undine_inv` was also run through the official precheck
(`tt-support-tools`, Magic DRC, KLayout FEOL/BEOL/offgrid, pin, boundary, power, analog-pin and Yosys checks): all pass.

## wafer.space - GF180MCU die

The layout is a whole die on gf180mcuD, the only PDK wafer.space's precheck accepts. The checks mirror the layout steps
of [gf180mcu-precheck](https://github.com/wafer-space/gf180mcu-precheck) (Apache-2.0):

- The PDK is gf180mcuD. A different open_pdks version than the one the precheck pins (`792ef670`) is a warning.
- The die starts at the origin and is exactly the size of the **slot**:

    | Slot | Die (um) |
    |---|---|
    | 1x1 | 3932 x 5122 |
    | 0p5x1 | 1936 x 5122 |
    | 1x0p5 | 3932 x 2531 |
    | 0p5x0p5 | 1936 x 2531 |

- The metal stack has 5 layers: nothing on Via5 (82/0) or MetalTop (53/0).
- A seal ring surrounds the die (GUARD_RING_MK, 167/5).
- Global density over the die, with dummy fill (datatype 4) counted as drawing: COMP 25-70%, Poly2 >= 14%,
  Metal1-5 >= 30%.
- With **Chip-on-Board**, the four ID cells (`gf180mcu_ws_ip__qrcode_id`, `__shuttle_id`, `__project_id`, `__marker`)
  are each placed once, where the project template puts them. The pad openings must also cover the slot's golden pad
  mask; the platform's precheck compares them, so Undine reports this as `manual`.

The sign-off suite runs the PDK deck three times, as the precheck does:

- the main checks, which leave out density, antenna and CUP;
- `decks=density`;
- `decks=antenna`.

It also runs Magic DRC and LVS when the cell has a schematic. The pad ring, the seal ring and the fill come from the
wafer.space project template (LibreLane); Undine checks the result but does not generate them.

The bundle holds `gds/<top>.gds`, `report.json` and `SUBMIT.md`. `SUBMIT.md` gives the precheck command for the slot,
and the steps on [platform.wafer.space](https://platform.wafer.space): create the project with the slot (and CoB),
link the GDS, and submit once the platform's precheck passes.

## ChipFoundry - Caravel user project (SKY130)

The layout is the user project wrapper of Caravel on sky130A or sky130B. Choose the **wrapper**: digital
(`user_project_wrapper`) or analog (`user_analog_project_wrapper`). The checks mirror the layout steps of
[cf-precheck](https://github.com/chipfoundry/cf-precheck) (Apache-2.0), on the cell renamed to the wrapper's name:

- No `#` or `/` in cell names.
- A prBoundary (235/4) covers exactly the user area, 0,0 to 2920,3520 um. The clear-area density deck measures inside it.
- Nothing lies beyond the outer edge of the power rings, (-43.63, -38.27) to (2963.25, 3557.95) um.
- **XOR of the wrapper's edge:** what lies outside the user area (the pin stubs and the power rings) must be identical to
  Caravel's empty wrapper. As in cf-precheck, Undine leaves out labels and treats pin shapes as their drawn layer. It
  also erases met5 on the left and right and met4 at the top and bottom, so straps may run into the rings.

    Undine does not ship Caravel's files, so the XOR needs one. Download
    `gds/user_project_wrapper_empty.gds.gz` (or the analog one) from
    [chipfoundry/caravel](https://github.com/chipfoundry/caravel). Put it in a directory the server may read
    (`--import-root`, default your home directory) and enter its path as **golden_gds**. Without it, the XOR is `manual`.

- The GPIO defaults in `verilog/rtl/user_defines.v` and the CVC checks on io_oeb are `manual`. cf-precheck reads them
  from the repository and the extracted netlist.

The sign-off suite runs the sky130 decks that cf-precheck runs (FEOL, BEOL, offgrid, clear-area density, pin labels,
zero area), Magic DRC, and LVS.

The bundle holds `gds/<wrapper>.gds`, `report.json` and `SUBMIT.md`. `SUBMIT.md` lists the files to add to the
`caravel_user_project` repository next to the GDS, then the two ChipFoundry CLI steps: `cf precheck`, then `cf push`.

## Layout hygiene only (any shuttle)

This target checks what every shuttle needs, for designs that target none of the shuttles above:

- one top cell;
- the database unit;
- no zero-area shapes;
- the origin;
- the highest metal.
