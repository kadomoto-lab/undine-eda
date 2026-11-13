"""What Undine needs to know about a process, in one place.

Everything that used to be hard-wired to sky130 (layer numbers, rule-deck names and options, model paths, Magic /
Netgen setup) is described by a profile per PDK family. `for_pdk` picks it from the PDK's family. A profile with
`verified=False` is written from the PDK's public layer map / file layout and has not been exercised against an
installed PDK in this repository; `limits` names what is still missing, and Undine says so instead of pretending.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable

SWITCHES = ("feol", "beol", "offgrid")                 # the DRC switches the Verify panel shows


def _tf(v) -> str:
    return ("true" if v else "false") if isinstance(v, bool) else str(v)


@dataclass(frozen=True)
class PcellLib:
    """The PDK's KLayout PCell library and how to load it outside the KLayout application."""
    paths: tuple[str, ...]                             # added to sys.path, relative to libs.tech/klayout (the first holds module)
    module: str                                        # the package that defines the library
    library: str                                       # the name it registers under
    init: str | None = None                            # a callable of the module that registers it (None: importing does)
    technology: str | None = None                      # the KLayout technology the library is bound to
    env: str = "pcell"                                 # the pixi environment whose Python runs it
    # (module under `module`, attribute, value) set before generating: a generator's own clamp tighter than the rule
    limits: tuple[tuple[str, str, float], ...] = ()


@dataclass(frozen=True)
class LvsElement:
    """A device the symbols write as a call to its model (`Xname nodes model w=..`) that the LVS deck reads as an
    element: a call whose model matches `model` (a regex, case ignored) becomes a `letter` line with the parameters
    renamed (`rename`: {parameter: new name}, case ignored); `to` replaces the model, and `params` the parameters:
    computed from the call's numeric ones (lower-case names) and its multiplier (m, else mf, else mult; 1), returning
    {name: value} (the multiplier goes on as m)."""
    model: str
    letter: str
    rename: dict = field(default_factory=dict)
    to: str | None = None
    params: Callable[[dict], dict] | None = None


# the standard cells a place-and-route run adds for the layout alone, by name (sky130_fd_sc_hd__decap_3, _fill_1,
# _tapvpwrvgnd_1, _diode_2, sky130_ef_sc_hd__decap_12, gf180's filltie / endcap, sg13g2_fill_1 / antennanp)
PHYSICAL_CELLS = r"(^|_)(tapvpwrvgnd|tapvgnd|tapvpwr|filltie|fill|decap|decapkapwr|diode|fakediode|endcap|antenna\w*)(_\d+)?$"


@dataclass(frozen=True)
class StdcellLvs:
    """How the KLayout LVS of a layout with the PDK's standard cells reads them (checks/stdcells.py). The reference
    gets each standard cell it calls from the library's SPICE file (libs.ref/<lib>/spice/<lib>.spice): its transistor
    calls (`mos`, a regex of their models, case ignored) as M elements, the model renamed by `rename` ((regex,
    replacement), as the deck does), W and L times `scale` (the file's 650000u is 0.65 um); a resistor of a `short`
    model, which the deck does not extract (the layout's poly joins its ends), joins its two nets instead, so a tie
    cell (conb_1: HI-VPWR, LO-VGND) leaves the reference and its nets join the supplies, as in the layout. In the
    layout, the labels on `drop_labels` inside standard cells (sky130's pwell VNB: two per flop, joined by nothing the
    deck connects) are left out, and the cells a place-and-route run adds for the layout alone (`physical`, a regex of
    names: fill, tap, decap, antenna diode) lose their `strip` layers (their devices: a decap's transistors, a diode)
    and keep their wells and rails."""
    mos: str
    short: str = r"$^"
    rename: tuple[tuple[str, str], ...] = ()
    scale: float = 1.0
    drop_labels: tuple[str, ...] = ()
    strip: tuple[str, ...] = ()
    physical: str = PHYSICAL_CELLS
    simplify: bool = True                              # combine the reference's parallel fingers, as the layout's are


_SI = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "meg": 1e6, "g": 1e9}


def _computed(el: LvsElement, name: str, given: dict) -> dict:
    """The parameters of element `el` for the call `name` with parameters `given` ({name: text}), and m."""
    nums = {}
    for k, v in given.items():
        if (m := re.fullmatch(r"([-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?)(meg|[fpnumkg])?", v.strip("'{}"), re.I)):
            nums[k.lower()] = float(m.group(1)) * _SI.get((m.group(2) or "").lower(), 1.0)
    has = [k for k in ("m", "mf", "mult") if k in {x.lower() for x in given}]
    try:
        if has and has[0] not in nums:
            raise KeyError(has[0])
        return {**el.params(nums), "m": nums[has[0]] if has else 1.0}
    except KeyError:
        raise ValueError(f"{name}: LVS compares it as {el.to or 'an element'} by parameters computed from its own, "
                         f"which are not all numbers: {', '.join(f'{k}={v}' for k, v in given.items()) or 'none'}") from None


def _mim(p: dict) -> dict:
    """A MIM capacitor as KLayout's sky130 deck extracts it (MIMCap: A and P compared, C not): area and perimeter of
    one unit from its W and L in um."""
    return {"A": p["w"] * p["l"], "P": 2 * (p["w"] + p["l"])}


@dataclass(frozen=True)
class Mim:
    """A MIM capacitor as Magic extracts it: its device model, the capacitor layer (the top plate, layer / datatype),
    the bottom plate's metal, the via from the top plate up and the metal above (profile layer names), Magic's planes
    of the metal above and of the bottom plate's, and the capacitor's Magic types and their plane (`shield`). Magic's
    planes do not let the top plate shield the bottom plate from the metal above (the tech file puts the capacitor's
    plane above every metal in its planeorder), so it counts the overlap and the fringe of that metal inside the
    capacitor as coupling between the two plates' nets, which the device's own capacitance has already: parasitics.py
    extracts again with the capacitor's plane between its plates' (shielded_tech), or without `shield` takes out the
    overlap (mim_overlap)."""
    model: str
    cap: tuple[int, int]
    bottom: str
    via: str
    top: str
    planes: tuple[str, str]
    shield: tuple[str, str] = ("", "")


@dataclass(frozen=True)
class Strap:
    """What schematic-driven layout needs to join the fingers of a multi-finger MOSFET PCell into one device: via1
    between metal1 and metal2, its enclosures (um, (long side, short side)), metal2 spacing, and how the gates are
    joined (a metal1 bar over the PCell's gate pads, or a poly bar where the PCell draws no gate contacts)."""
    via: float
    enc1: tuple[float, float]
    enc2: tuple[float, float]
    m2_space: float
    gate: str = "met1"                                 # met1 | poly
    poly_w: float = 0.0                                # width of the poly bar
    layers: tuple[str, str, str, str, str] = ("diff", "poly", "met1", "via", "met2")   # profile layer names
    # with gate="poly": a contact on each gate under the poly bar and a metal1 pad over them, so the gate can be routed
    # (and labelled) in metal1. Contact size, its poly enclosure, metal1 enclosure (long, short), smallest metal1 pad
    cont: float = 0.0
    cont_poly: float = 0.0
    cont_m1: tuple[float, float] = (0.0, 0.0)
    m1_min: float = 0.0
    contact_layer: str = "contact"


@dataclass(frozen=True)
class Space:
    """A space two devices placed side by side keep (schematic-driven layout, unit arrays), between the merged shapes
    of layer `a` of one and layer `b` of the other, in um. `unless`: not checked when the first has this layer too (an
    n-diffusion's space to an n-well: a p-diffusion is inside a well of its own); `merge`: the two may overlap instead
    (the wells of units on one bulk net)."""
    a: str
    b: str
    um: float
    rule: str
    unless: str | None = None
    merge: bool = False


@dataclass(frozen=True)
class Ring:
    """The guard ring place_array draws around an array of MOSFET units (their bulk tap), drawn as guard_ring draws one
    (drawing.guard_ring, from the stack's tap rules: p-tap ring for nfets, n-tap ring for pfets): `width` wide, `space`
    from the units' cells, strapped in `conductor`, and for an n-tap ring the well `well` beyond it (thin, thick oxide).
    um."""
    width: float
    space: float
    conductor: str
    well: tuple[float, float]


@dataclass(frozen=True)
class Conductor:
    """A routing layer of the stack and its rules (um, um²): the narrowest wire, the spacing to another shape, the
    smallest area of a shape on it, the spacings next to a wide one ((width above which it applies, spacing) per
    level, the decks' run-length conditions left out: always as far), and `away`: layers a new shape of it never
    overlaps and keeps that far from (poly from diffusion and tap: poly drawn over a diffusion would be a new
    transistor)."""
    name: str                                          # profile layer name
    width: float
    space: float
    area: float = 0.0
    wide: tuple[tuple[float, float], ...] = ()
    away: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class Cut:
    """A contact or via between two conductors and what a rule-correct array of it needs (um): its square size and
    spacing, and how far each conductor encloses it, as (on every side, on two opposite sides). The second is the
    larger "on one of two adjacent sides" rule, which two opposite sides meet at every corner. `extra`: layers drawn
    around the array (sky130's npc over a poly contact) as (layer, enclosure, width, spacing): one closer than its
    spacing to another of its kind is bridged to it (agent_tools._bridged), and it keeps from what the array keeps
    away from by that less its enclosure; `away`: layers the array keeps that far from (a diffusion contact from the
    gate, a poly contact from diffusion). A conductor below that is a diffusion or tap (an implant goes with it) is not
    drawn by add_via: it has to be there, enclosing the contact."""
    name: str                                          # the cut layer (profile name)
    below: str
    above: str
    size: float
    space: float
    enc_below: tuple[float, float]
    enc_above: tuple[float, float]
    rules: str = ""                                    # the rule names, as answers cite them
    extra: tuple[tuple[str, float], ...] = ()
    away: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class Plate:
    """A capacitor plate layer (sky130's capm): it sits on `metal`, and `cut` lands on it instead of on the metal under
    it. `top` / `bottom` are the device's pins on the plate and on the metal (the symbol's c0 / c1). `also`: cut layers
    outside the stack that only ever land on the plate and join it to the metal above `cut` (IHP's Vmim, which its MIM
    PCell draws instead of TopVia1)."""
    name: str
    metal: str
    cut: str
    top: str = "c0"
    bottom: str = "c1"
    also: tuple[str, ...] = ()


@dataclass(frozen=True)
class TapRule:
    """What a rule-correct substrate / well tap needs (um): its implant per kind (ptap / ntap), how far it encloses
    the tap, its width and its spacing to another implant of its kind; the well an ntap sits in and its enclosure; the
    spacing from a tap to another diffusion; a ptap's to an n-well; an n-well's to another one and to an n+ diffusion
    outside it; the latch-up distance (a diffusion to the nearest tap of its substrate or well)."""
    implant: dict                                      # {"ptap": "psdm", "ntap": "nsdm"}
    implant_enc: float
    implant_width: float
    implant_space: float
    well_enc: float
    space: float
    well_space: float                                  # a ptap to an n-well
    well_well: float                                   # an n-well to another (closer: merged)
    well_ndiff: float                                  # an n-well to an n+ diffusion
    latchup: float = 0.0                               # the farthest a diffusion may be from a tap of its body (0: no rule)


@dataclass(frozen=True)
class Stack:
    """The layer stack as layout tools need it: connectivity (what touches what through which cut) for the nets of a
    layout and the terminals of a device (connectivity.py), and the rules for drawing on it (drawing.py: add_via, route,
    add_tap, guard_ring). Layer names are the profile's."""
    diff: str
    tap: str | None
    poly: str
    well: str                                          # the n-well (a pMOS's bulk); the substrate is the rest
    conductors: tuple[Conductor, ...]                  # bottom to top: poly, li1, met1 ...
    cuts: tuple[Cut, ...]
    plates: tuple[Plate, ...] = ()
    taps: TapRule | None = None

    def conductor(self, name: str) -> Conductor | None:
        return next((c for c in self.conductors if c.name == name), None)

    def order(self) -> list[str]:
        """The conductors bottom to top, the diffusion and tap below them."""
        return [self.diff, *([self.tap] if self.tap else []), *(c.name for c in self.conductors)]


@dataclass(frozen=True)
class Profile:
    family: str
    label: str
    verified: bool
    layers: dict[str, tuple[int, int]]                 # logical name -> (layer, datatype)
    metals: tuple[str, ...]                            # logical names, bottom to top
    max_metal: str                                     # highest metal a shuttle allows (informational default)
    grid_nm: int = 5
    klayout_dir: str = ""                              # where the decks are, under libs.tech/klayout ("tech" for GF180 / IHP)
    drc_klayout: tuple[str, ...] = ()                  # file names / globs under <decks>/drc (first = main deck)
    drc_options: dict = field(default_factory=dict)    # main deck (or glob) -> default -rd switches
    drc_switches: str = "flags"                        # how SWITCHES reach the deck: flags (feol=true), tags (decks=all,-feol),
                                                       # negated (no_feol=true)
    drc_extra: tuple[str, ...] = ()                    # more KLayout decks the shuttle prechecks run
    lvs_klayout: tuple[str, ...] = ()                  # globs under <decks>/lvs
    lvs_options: dict = field(default_factory=dict)    # default -rd switches of the LVS deck
    deck_vars: dict = field(default_factory=dict)      # PDK name -> -rd values every DRC / LVS run gets (metal stack)
    magic_rc: str | None = None                        # under libs.tech/magic; {name} is the PDK name (sky130A)
    magic_drc: bool = False                            # run_checks runs Magic's drc(full) next to the KLayout deck
                                                       # (the deck lacks rules of Magic's that shuttle prechecks run);
                                                       # set once drc(full) has been run on the PDK's cells
    # body taps as drawn, for run_checks' ERC of taps butted against a source / drain of another net: a union of
    # terms, each the layers (profile names) a tap is drawn on and, after "-", those it is not on
    erc_tap: tuple[tuple[str, ...], ...] = ()
    mims: tuple[Mim, ...] = ()                         # MIM capacitors whose plates Magic couples a second time
    # (contact or via, the label layer of the metal above it): where an extraction labels a net that has no label, at
    # one of its contacts, with the schematic's name LVS matched it to (parasitics.py)
    net_labels: tuple[tuple[str, str], ...] = ()
    netgen_setup: str | None = None                    # under libs.tech/netgen
    pcells: bool = False                               # PDK ships PCell generators Undine can run
    pcell_lib: PcellLib | None = None
    strap: Strap | None = None                         # schematic-driven layout of multi-finger MOSFETs
    # what devices placed apart keep between them (schematic-driven layout, place_array); none: the 1 um default only
    space: tuple[Space, ...] = ()
    # wells, implants and marks that the units of one kind in an array share as one region drawn over them all (the
    # gaps between the units' own would be too narrow), and the guard ring place_array draws around MOSFET units
    fill: tuple[str, ...] = ()
    ring: Ring | None = None
    model_vars: tuple[tuple[str, str], ...] = ()       # ($::NAME, path relative to the PDK root) for Xschem schematics
    model_lib: str | None = None                       # ngspice library with corner sections, relative to the PDK root
    model_includes: tuple[str, ...] = ()               # files .include'd before it (GF180's design.ngspice)
    osdi: tuple[str, ...] = ()                         # Verilog-A models ngspice must load, by name (Pdk.osdi_files)
    corners: tuple[str, ...] = ()                      # its sections (the first is the default: typical)
    corner_symbol: str | None = None                   # Xschem symbol that writes the .lib line with a chosen corner
    # Monte Carlo device mismatch (check_specs / sweep mismatch): the model library section of a corner with it on
    # ("{corner}_mm"), or a .param that turns it on after the models; ngspice draws it from .option seed
    mismatch_section: str | None = None
    mismatch_param: str | None = None
    gate_layers: tuple[str, str] = ("poly", "diff")    # (gate, active) for device-integrity checks
    pin_label_layers: tuple[str, ...] = ()             # layers whose text labels name nets for LVS
    limits: tuple[str, ...] = ()                       # what does not work yet with this profile
    # LVS reference netlists: devices the symbols write as a call to their model that the LVS deck reads as elements
    lvs_elements: tuple[LvsElement, ...] = ()
    # standard cells by name: (cell name regex, its library under libs.ref; \1 for a group), and where a library's GDS is
    stdcells: tuple[tuple[str, str], ...] = ()
    stdcell_gds: str = "libs.ref/{lib}/gds/{lib}.gds"
    # the cells' SPICE: the library's file, else a file of the cell's own ({cell}: sky130_ef_sc_hd__decap_12.spice)
    stdcell_spice: tuple[str, ...] = ("libs.ref/{lib}/spice/{lib}.spice", "libs.ref/{lib}/spice/{cell}.spice")
    # the cells' Verilog models (relative to the PDK root, {lib}: the library) and the defines they are read with, for
    # Verilator's lint and simulation of a gate-level netlist (gatelevel.rtl_inputs); none: not offered
    stdcell_verilog: tuple[str, ...] = ()
    stdcell_verilog_defines: tuple[str, ...] = ()
    stdcell_lvs: StdcellLvs | None = None              # KLayout LVS of layouts with standard cells (None: as drawn)
    stdcell_tie: str = ""                              # the tie cell a constant input takes its 1 / 0 from (its pins)
    stack: Stack | None = None                         # connectivity and drawing rules (None: those tools refuse)
    stacks: dict = field(default_factory=dict)         # PDK name -> its own Stack where the variants' metal stacks differ

    def stdcell(self, name: str) -> tuple[str, str] | None:
        """(library, its GDS relative to the PDK root) of a standard cell by its name; None when the name is not one."""
        import re
        for pat, lib in self.stdcells:
            if (m := re.fullmatch(pat, name or "")):
                lib = m.expand(lib)
                return lib, self.stdcell_gds.format(lib=lib)
        return None

    def lvs_netlist(self, text: str) -> str:
        """`text` (a reference netlist for LVS) with the calls of lvs_elements rewritten as elements. ValueError when an
        element's parameters are computed from one that is not a number (an expression the netlist did not
        evaluate)."""
        if not self.lvs_elements:
            return text
        out = []
        for line in text.split("\n"):
            t = line.split()
            if t and t[0][:1] in "xX":
                args = [x for x in t[1:] if "=" not in x]
                for el in self.lvs_elements:
                    if args and re.fullmatch(el.model, args[-1], re.I):
                        kv = [x.partition("=")[::2] for x in t[1 + len(args):]]
                        if el.params is None:
                            params = [f"{el.rename.get(k.lower(), k)}={v}" for k, v in kv]
                        else:
                            params = [f"{k}={v:.12g}" for k, v in _computed(el, t[0], dict(kv)).items()]
                        line = " ".join([el.letter + t[0][1:], *args[:-1], el.to or args[-1], *params])
                        break
            out.append(line)
        return "\n".join(out)

    def magic_file(self, name: str) -> str | None:
        return self.magic_rc.format(name=name) if self.magic_rc else None

    def netgen_file(self, name: str) -> str | None:
        return self.netgen_setup.format(name=name) if self.netgen_setup else None

    def deck_defaults(self, deck: str) -> dict:
        return next((dict(v) for k, v in self.drc_options.items() if fnmatch(deck, k)), {})

    def drc_rd(self, deck: str, options: dict, name: str) -> dict[str, str]:
        """-rd values for a DRC run of `deck` on PDK `name`: the deck's defaults, the stack, then `options`, with the
        Verify panel's switches spelled the way the deck reads them."""
        opts = {**self.deck_defaults(deck), **(options or {})}
        rd = {k: _tf(v) for k, v in self.deck_vars.get(name, {}).items()}
        if self.drc_switches == "tags":
            tags = [t for t in str(opts.pop("decks", "all")).split(",") if t]
            tags += [f"-{k}" for k in SWITCHES if opts.pop(k, True) is False]
            rd["decks"] = ",".join(tags)
        elif self.drc_switches == "negated":
            for k in SWITCHES:
                if k in opts:
                    rd[f"no_{k}"] = _tf(not opts.pop(k))
        rd.update({k: _tf(v) for k, v in opts.items()})
        return rd

    def lvs_rd(self, options: dict, name: str) -> dict[str, str]:
        opts = {"run_mode": "deep", **self.lvs_options, **(options or {})}
        return {**{k: _tf(v) for k, v in self.deck_vars.get(name, {}).items()}, **{k: _tf(v) for k, v in opts.items()}}


SKY130 = Profile(
    family="sky130", label="SkyWater SKY130", verified=True,
    layers={"poly": (66, 20), "diff": (65, 20), "tap": (65, 44), "licon1": (66, 44), "li1": (67, 20), "mcon": (67, 44),
            "met1": (68, 20), "via": (68, 44), "met2": (69, 20), "via2": (69, 44), "met3": (70, 20), "via3": (70, 44),
            "met4": (71, 20), "via4": (71, 44), "met5": (72, 20), "nwell": (64, 20), "nsdm": (93, 44), "psdm": (94, 20),
            "li1.label": (67, 5), "met1.label": (68, 5), "met2.label": (69, 5), "met3.label": (70, 5), "met4.label": (71, 5),
            "met5.label": (72, 5), "met4.pin": (71, 16),
            # the rest of what sky130's devices draw (MIM plates, thick-oxide and Vt implants, the poly-contact implant,
            # marker layers), so that get_device_geometry names them
            "capm": (89, 44), "cap2m": (97, 44), "npc": (95, 20), "hvi": (75, 20), "hvntm": (125, 20), "lvtn": (125, 44),
            "poly.res": (66, 13), "diff.res": (65, 13),
            "hvtp": (78, 44), "hvtr": (18, 20), "dnwell": (64, 18), "pwell": (64, 44), "rpm": (86, 20), "urpm": (79, 20),
            "ldntm": (11, 44), "pad": (76, 20), "poly.label": (66, 5), "areaid.sc": (81, 4), "areaid.ce": (81, 2),
            "areaid.de": (81, 23), "areaid.ed": (81, 19), "areaid.lvt": (81, 60), "prBoundary": (235, 4),
            "li1.pin": (67, 16), "met1.pin": (68, 16), "met2.pin": (69, 16), "met3.pin": (70, 16), "met5.pin": (72, 16),
            "pwell.label": (64, 59)},
    metals=("li1", "met1", "met2", "met3", "met4", "met5"), max_metal="met5",
    drc_klayout=("sky130?_mr.drc",), drc_options={"sky130?_mr.drc": {"feol": True, "beol": True, "offgrid": True}},
    drc_extra=("met_min_ca_density.lydrc", "pin_label_purposes_overlapping_drawing.rb.drc", "zeroarea.rb.drc"),
    # the SPICE reader reads W=2 as 2 m (= 2e6 um); the deck's scale switch brings the layout side to the same units
    lvs_klayout=("sky130.lvs",), lvs_options={"lvs_sub": "VSS", "scale": True},
    # the MIM symbols write a call (XC1 c0 c1 sky130_fd_pr__cap_mim_m3_1 W=.. L=.. m=MF, um) that the deck's reader
    # leaves a call; the deck extracts the plates (met3 / capm, met4 / cap2m) as capacitors of the model's name,
    # compared by area and perimeter
    lvs_elements=(LvsElement(r"(sky130_fd_pr__)?cap_mim_m3_1", "C", to="sky130_fd_pr__model__cap_mim", params=_mim),
                  LvsElement(r"(sky130_fd_pr__)?cap_mim_m3_2", "C", to="sky130_fd_pr__model__cap_mim_m4", params=_mim)),
    magic_rc="{name}.magicrc", netgen_setup="{name}_setup.tcl", pcells=True,
    # sky130A_mr.drc has none of Magic's MV diff / tap rules (diff/tap.14-17) and checks the MIM spacings (capm.11,
    # met3.3c) otherwise: a PCell's 0.45 um butted tap on a 5 V device is clean in KLayout and diff/tap.16 in Magic
    magic_drc=True, erc_tap=(("tap",),),
    mims=(Mim("sky130_fd_pr__cap_mim_m3_1", (89, 44), "met3", "via3", "met4", ("metal4", "metal3"), ("*mimcap", "cap1")),
          Mim("sky130_fd_pr__cap_mim_m3_2", (97, 44), "met4", "via4", "met5", ("metal5", "metal4"), ("*mimcap2", "cap2"))),
    net_labels=(("licon1", "li1.label"), ("mcon", "met1.label"), ("via", "met2.label"), ("via2", "met3.label"),
                ("via3", "met4.label"), ("via4", "met5.label")),
    # open_pdks 2025+: gdsfactory 8 on kfactory 0.2x (the older pymacros/cells generation is not run, see pcell.py). Its
    # mim_cap (as Magic's generator) clamps each side of a cap_mim_m3_1 to 2 um, where capm.1 allows 1 um: a CDAC's
    # 1.17 um unit would come out 2 x 2 um, so the clamp is lowered to the rule (the drawing is clean in both decks
    # from 1 um). cap_mim_m3_2 keeps its 2.16 um: its 0.8 um via4 needs it
    pcell_lib=PcellLib(paths=("python",), module="cells", init="sky130", library="skywater130",
                       limits=(("cap", "l_mim", 1.0),)),
    strap=Strap(via=0.15, enc1=(0.085, 0.055), enc2=(0.085, 0.055), m2_space=0.14),
    # Magic's drc(full) where it is stricter than the KLayout deck (capm.11: 1.34 um from a capm to another plate's
    # metal3, which the mr deck checks as 0.5 um); the bottom plates of capm.2b are capm + 0.14 um, 1.2 um apart
    space=(Space("diff", "diff", 0.3, "difftap.3, difftap.15a (MV)"), Space("tap", "tap", 0.27, "difftap.3"),
           Space("diff", "tap", 0.37, "difftap.15b (MV)"), Space("poly", "poly", 0.21, "poly.2"),
           Space("npc", "npc", 0.27, "npc.2"), Space("licon1", "licon1", 0.17, "licon.2"), Space("li1", "li1", 0.17, "li.3"),
           Space("mcon", "mcon", 0.19, "ct.2"), Space("met1", "met1", 0.14, "m1.2"), Space("via", "via", 0.17, "via.2"),
           Space("met2", "met2", 0.14, "m2.2"), Space("met3", "met3", 0.3, "m3.2"), Space("met4", "met4", 0.3, "m4.2"),
           Space("met5", "met5", 1.6, "m5.2"), Space("nwell", "nwell", 1.27, "nwell.2a", merge=True),
           Space("diff", "nwell", 0.34, "difftap.9", unless="nwell"), Space("tap", "nwell", 0.13, "difftap.11", unless="nwell"),
           Space("hvi", "hvi", 0.7, "hvi.5", merge=True), Space("nwell", "hvi", 0.7, "hvi.5", unless="hvi"),
           Space("capm", "capm", 1.48, "capm.2b: bottom plates 1.2 apart"),
           Space("capm", "met3", 1.34, "capm.11 (Magic): to another plate's metal3"),
           Space("cap2m", "cap2m", 1.48, "cap2m.2b: bottom plates 1.2 apart"),
           Space("cap2m", "met4", 1.34, "cap2m.11 (Magic): to another plate's metal4")),
    fill=("nwell", "nsdm", "psdm", "lvtn", "hvi", "hvntm"),
    # a ring 0.41 um wide: licon 0.17 with 0.12 of tap around it (licon.5); units 0.4 um inside it (their implant clear
    # of the ring's); the n-tap 0.18 um inside the well (diff/tap.10), 0.33 um thick-oxide (diff/tap.19). The PDK's
    # guard_ring_gen is not used: Magic reads its n-tap ring as overlapping layers
    ring=Ring(width=0.41, space=0.4, conductor="li1", well=(0.18, 0.33)),
    model_vars=(("SKYWATER_MODELS", "libs.tech/ngspice"), ("SKYWATER_STDCELLS", "libs.ref/sky130_fd_sc_hd/spice"),
                ("SKYWATER_STDCELLS_HD", "libs.ref/sky130_fd_sc_hd/spice")),
    model_lib="libs.tech/ngspice/sky130.lib.spice", corners=("tt", "ss", "ff", "sf", "fs"),
    corner_symbol="sky130_fd_pr/corner.sym",
    mismatch_section="{corner}_mm",                    # MC_MM_SWITCH=1 (tt_mm ...): MOSFETs, MIM and MOM capacitors
    pin_label_layers=("li1.label", "met1.label", "met2.label", "met3.label", "met4.label", "met5.label"),
    # sky130_fd_sc_hd__nand2_1; the sky130_ef_sc_hd__ cells (fill, decap) are in the sky130_fd_sc_hd library too
    stdcells=((r"sky130_(?:fd|ef)_sc_([a-z]+)__\w+", r"sky130_fd_sc_\1"),),
    # their SPICE calls sky130_fd_pr__nfet_01v8 / special_nfet_01v8 / pfet_01v8_hvt with w=650000u (scale 1e-6); conb_1's
    # poly resistors (res_generic_po) have no resistor marker, the deck joins their ends; a flop has two VNB labels on
    # the pwell (64/59) that only the deck's inductors and vpp capacitors connect to (must-connect error otherwise)
    stdcell_lvs=StdcellLvs(mos=r"sky130_fd_pr__(special_)?[np]fet\w*", short=r"sky130_fd_pr__res_generic_\w+",
                           rename=((r"special_nfet", "nfet"),), scale=1e-6, drop_labels=("pwell.label",),
                           strip=("diff", "poly")),
    # the functional models (no specify blocks, no timing checks) without delay: UNIT_DELAY empty
    stdcell_verilog=("libs.ref/{lib}/verilog/primitives.v", "libs.ref/{lib}/verilog/{lib}.v"),
    stdcell_verilog_defines=("FUNCTIONAL", "UNIT_DELAY="),
    stdcell_tie="sky130_fd_sc_hd__conb_1 (HI: 1, LO: 0)",
    # the periphery rules (SkyWater's rule names, as KLayout's mr deck and Magic's drc(full) check them): poly.1a / 2 / 4 / 5,
    # li.1 / 3 / 6, m1.1 / 2 / 3b / 6, m2.*, m3.1 / 2 / 3cd / 6, m4.*, m5.*; licon.1 / 2 / 5a / 5c / 7 / 8 / 8a / 15
    # (npc around a poly contact), li.5, ct.1 / 2 / 4, m1.4 / 5, via.1a / 2 / 4a / 5a, m2.4 / 5, via2.1a / 2 / 4 / 5,
    # m3.4, via3.1 / 2 / 4 / 5, m4.3, via4.1 / 2 / 4, m5.3; diff/tap.3 / 9 / 10 / 11, nsd / psd.5a / 1 / 2, nwell.1 / 2a
    stack=Stack(
        diff="diff", tap="tap", poly="poly", well="nwell",
        conductors=(Conductor("poly", 0.15, 0.21, away=(("diff", 0.075), ("tap", 0.055))), Conductor("li1", 0.17, 0.17, 0.0561),
                    Conductor("met1", 0.14, 0.14, 0.083, ((3.0, 0.28),)), Conductor("met2", 0.14, 0.14, 0.0676, ((3.0, 0.28),)),
                    Conductor("met3", 0.3, 0.3, 0.24, ((3.0, 0.4),)), Conductor("met4", 0.3, 0.3, 0.24, ((3.0, 0.4),)),
                    Conductor("met5", 1.6, 1.6, 4.0)),
        cuts=(Cut("licon1", "diff", "li1", 0.17, 0.17, (0.04, 0.06), (0.0, 0.08), "licon.1, licon.2, licon.5a, licon.5c, "
                  "licon.11, li.5", away=(("poly", 0.055),)),
              Cut("licon1", "tap", "li1", 0.17, 0.17, (0.04, 0.12), (0.0, 0.08), "licon.1, licon.2, licon.7, li.5"),
              Cut("licon1", "poly", "li1", 0.17, 0.17, (0.05, 0.08), (0.0, 0.08), "licon.1, licon.2, licon.8, licon.8a, "
                  "licon.14, licon.15, li.5, npc.1, npc.2, licon.13", extra=(("npc", 0.1, 0.27, 0.27),),
                  away=(("diff", 0.19), ("tap", 0.19))),
              Cut("mcon", "li1", "met1", 0.17, 0.19, (0.0, 0.0), (0.03, 0.06), "ct.1, ct.2, ct.4, m1.4, m1.5"),
              Cut("via", "met1", "met2", 0.15, 0.17, (0.055, 0.085), (0.055, 0.085), "via.1a, via.2, via.4a, via.5a, m2.4, m2.5"),
              Cut("via2", "met2", "met3", 0.2, 0.2, (0.04, 0.085), (0.065, 0.065), "via2.1a, via2.2, via2.4, via2.5, m3.4"),
              Cut("via3", "met3", "met4", 0.2, 0.2, (0.06, 0.09), (0.065, 0.065), "via3.1, via3.2, via3.4, via3.5, m4.3"),
              Cut("via4", "met4", "met5", 0.8, 0.8, (0.19, 0.19), (0.31, 0.31), "via4.1, via4.2, via4.4, m5.3")),
        plates=(Plate("capm", "met3", "via3"), Plate("cap2m", "met4", "via4")),
        taps=TapRule(implant={"ptap": "psdm", "ntap": "nsdm"}, implant_enc=0.125, implant_width=0.38, implant_space=0.38,
                     well_enc=0.18, space=0.27, well_space=0.13, well_well=1.27, well_ndiff=0.34,
                     latchup=15.0)),        # LU.2 / LU.3
)

def _gf180_stack(thin: int, top: str | None, plate: tuple[str, str] | None) -> Stack:
    """GF180MCU's stack, from gf180mcu_fd_pv's KLayout deck: metal1 .. metal{thin} thin, `top` above them with the
    9K / 11K MetalTop rules (MT.*), and the MIM's top plate FuseTop (option B: on the metal under the top one, `plate`
    (metal, its via) landing on it; the symbol's G is the metal under it, the deck's P1, as Undine netlists it). Where
    the 3.3 V and the 5 / 6 V rules differ, the larger: a shape is correct inside DualGate and outside it. Contacts and
    vias use the spacing a 4 x 4 array needs (CO.2b, Vn.2b) at every size. No taps: a GF180 tap is COMP with the other
    implant, which these tools do not draw."""
    conductors = [Conductor("poly", 0.2, 0.24, away=(("comp", 0.3),))]          # PL.1_MV, PL.3a, PL.5a / 5b_MV
    conductors += [Conductor(f"metal{i}", 0.23 if i == 1 else 0.28, 0.23 if i == 1 else 0.28, 0.1444, ((10.0, 0.3),))
                   for i in range(1, thin + 1)]                                  # Mn.1, Mn.2a, Mn.3, Mn.2b
    if top:
        conductors.append(Conductor(top, 0.44, 0.46, 0.5625, ((10.0, 0.6),)))   # MT.1, MT.2a, MT.4, MT.2b
    metals = [c.name for c in conductors[1:]]
    cuts = [Cut("contact", "comp", "metal1", 0.22, 0.28, (0.07, 0.07), (0.005, 0.06),
                "CO.1, CO.2b, CO.4, CO.6, CO.6a, CO.6b, CO.7", away=(("poly", 0.15),)),
            Cut("contact", "poly", "metal1", 0.22, 0.28, (0.07, 0.07), (0.005, 0.06),
                "CO.1, CO.2b, CO.3, CO.6, CO.6a, CO.6b, CO.8", away=(("comp", 0.17),))]
    for i, (lo, hi) in enumerate(zip(metals, metals[1:]), 1):    # metal1 encloses via1 by 0 (V1.3a), the others 0.01
        cuts.append(Cut(f"via{i}", lo, hi, 0.26, 0.36, (0.0 if i == 1 else 0.01, 0.06), (0.01, 0.06),
                        f"V{i}.1, V{i}.2b, V{i}.3{'a' if i == 1 else 'b'}, V{i}.3c, V{i}.3d, V{i}.4a, V{i}.4b, V{i}.4c"))
    return Stack(diff="comp", tap=None, poly="poly", well="nwell", conductors=tuple(conductors), cuts=tuple(cuts),
                 plates=(Plate("fusetop", plate[0], plate[1], top="B", bottom="G"),) if plate else ())


# GF180MCU: open_pdks' gf180mcu{A..D}, decks from globalfoundries-pdk-libs-gf180mcu_fd_pv (libs.tech/klayout/tech).
# Exercised on gf180mcuD (open_pdks 792ef670, the version wafer.space pins): KLayout DRC and LVS of
# gf180mcu_fd_sc_mcu7t5v0__inv_1 (LVS matches), ngspice at typical / ss / ff.
GF180 = Profile(
    family="gf180mcu", label="GlobalFoundries GF180MCU", verified=True,
    layers={"comp": (22, 0), "poly": (30, 0), "contact": (33, 0), "metal1": (34, 0), "via1": (35, 0), "metal2": (36, 0),
            "via2": (38, 0), "metal3": (42, 0), "via3": (40, 0), "metal4": (46, 0), "via4": (41, 0), "metal5": (81, 0),
            "via5": (82, 0), "metaltop": (53, 0), "pad": (37, 0), "nwell": (21, 0), "dnwell": (12, 0), "lvpwell": (204, 0),
            "nplus": (32, 0), "pplus": (31, 0),
            "dualgate": (55, 0), "pr_bndry": (0, 0), "guard_ring_mk": (167, 5), "poly.label": (30, 10),
            "metal1.label": (34, 10), "metal2.label": (36, 10), "metal3.label": (42, 10), "metal4.label": (46, 10),
            "metal5.label": (81, 10), "metaltop.label": (53, 10), "fusetop": (75, 0)},
    metals=("metal1", "metal2", "metal3", "metal4", "metal5", "metaltop"), max_metal="metal5",
    klayout_dir="tech", drc_klayout=("gf180mcu.drc",), drc_switches="tags",
    # density and antenna are chip-level (a shuttle target runs them on the die); CUP needs pads. As the wafer.space precheck.
    drc_options={"gf180mcu.drc": {"decks": "all,-density,-antenna,-cup"}},
    # flat: in deep mode the deck extracts no pfet that sits in a sub-cell (a PCell placed in a cell, 10/26); nfets are found.
    # combine: the fingers of a device (nf, m in the schematic) are compared as one device, as SG13G2's deck does by default
    lvs_klayout=("gf180mcu.lvs",), lvs_options={"run_mode": "flat", "combine": True},
    # metal stack per variant (the decks default to 6LM / 9K otherwise); from the PDK's drc/options.rb
    deck_vars={"gf180mcuA": {"metal_level": "3LM", "metal_top": "30K", "mim_option": "A"},
               "gf180mcuB": {"metal_level": "4LM", "metal_top": "11K", "mim_option": "B"},
               "gf180mcuC": {"metal_level": "5LM", "metal_top": "9K", "mim_option": "B"},
               "gf180mcuD": {"metal_level": "5LM", "metal_top": "11K", "mim_option": "B"}},
    magic_rc="{name}.magicrc", netgen_setup="{name}_setup.tcl", pcells=True,
    # wafer.space's precheck runs Magic DRC before KLayout's; drc(full) exercised on gf180mcuA / D: a wire too narrow
    # (M1.1) in a cell and in the top cell, clean on gf180mcu_fd_sc_mcu7t5v0__inv_1 and the nfet / pfet PCells
    magic_drc=True,
    # COMP of the well's own implant, as the LVS deck's ptap / ntap (and _dw in a deep n-well); its deck does not join a
    # silicided source / drain butted to a tap, which is a short there
    erc_tap=(("comp", "pplus", "-nwell", "-dnwell"), ("comp", "nplus", "nwell"), ("comp", "pplus", "lvpwell", "dnwell"),
             ("comp", "nplus", "dnwell", "-lvpwell")),
    # exercised with two gf180mcu_fd_sc_mcu7t5v0__inv_1 joined in metal2 (test_parasitics.py)
    net_labels=(("contact", "metal1.label"), ("via1", "metal2.label"), ("via2", "metal3.label"),
                ("via3", "metal4.label")),
    # gdsfactory 7.9 on gdstk (gf180mcu_fd_pr requirements.txt), hence an environment of its own
    pcell_lib=PcellLib(paths=("tech/pymacros",), module="cells", init="gf180mcu", library="gf180mcu", env="pcell-gf180"),
    # V1.1 0.26, V1.3c / V1.4b end-of-line enclosure 0.06 (metal narrower than 0.34), M2.2a 0.28
    strap=Strap(via=0.26, enc1=(0.06, 0.06), enc2=(0.06, 0.06), m2_space=0.28,
                layers=("comp", "poly", "metal1", "via1", "metal2")),
    model_vars=(("180MCU_MODELS", "libs.tech/ngspice"),),
    # the resistor and MIM symbols have no lvs_format: their Xname .. model r_width= .. becomes the R / C element the deck's
    # reader takes (as its own gf180_xschem_klayout_spice_convert.py does for ppolyf_u)
    lvs_elements=(LvsElement(r"(ppolyf|npolyf|nplus|pplus)_[us](_[0-9a-z]+)*|nwell|rm[1-3]|tm(6|9|11|30)k", "R",
                             {"r_width": "W", "r_length": "L"}),
                  LvsElement(r"cap_mim_[0-9a-z]+", "C", {"c_width": "W", "c_length": "L"})),
    model_lib="libs.tech/ngspice/sm141064.ngspice", model_includes=("libs.tech/ngspice/design.ngspice",),
    corners=("typical", "ss", "ff", "sf", "fs"), gate_layers=("poly", "comp"),
    mismatch_param="sw_stat_mismatch=1",               # design.ngspice sets it 0; checked with ngspice 42, not in a test
    pin_label_layers=("metal1.label", "metal2.label", "metal3.label", "metal4.label", "metal5.label"),
    stdcells=((r"(gf180mcu_fd_sc_mcu[79]t5v0)__\w+", r"\1"),),
    # C, D: five metals, metal5 the top one, the MIM on metal4. B: four, metal4 on top, the MIM on metal3. A: metal3 is
    # the 30K top metal (its own rules, MT30.*: not drawn here) and the MIM is option A (on metal2, via2 lands on it)
    stack=_gf180_stack(4, "metal5", ("metal4", "via4")),
    stacks={"gf180mcuB": _gf180_stack(3, "metal4", ("metal3", "via3")), "gf180mcuA": _gf180_stack(2, None, None)},
)

def _ihp_stack(thin: int, tops: tuple[str, ...]) -> Stack:
    """IHP SG13's stack, from its KLayout deck (the maximal rule set): metal1 .. metal{thin} and the thick `tops`, and
    for SG13G2 the MIM's plate on metal5 (TopVia1 on it joins it, as the deck's mim_via, and Vmim, which the cmim
    PCell draws; c0 the top plate, the deck's mim_top). Contacts and vias use the spacing a large array needs (Cnt.b1,
    Vn.b1) at every size; the wide-metal spacings (Mn.e, Mn.f) hold beside any wide shape, whatever the run length. Not
    checked: pSD's 0.09 um from a contact on Activ (Cnt.g1 / g2), the device's own. No taps: an IHP tap is Activ with or
    without pSD, which these tools do not draw."""
    conductors = [Conductor("poly", 0.13, 0.18, 0.09, away=(("diff", 0.07),)),             # Gat.a, b, e, d
                  Conductor("metal1", 0.16, 0.18, 0.09, ((0.3, 0.22), (10.0, 0.6)))]       # M1.a, b, d, e, f
    conductors += [Conductor(f"metal{i}", 0.2, 0.21, 0.144, ((0.39, 0.24), (10.0, 0.6))) for i in range(2, thin + 1)]
    conductors += [Conductor("topmetal1", 1.64, 1.64), Conductor("topmetal2", 2.0, 2.0, wide=((5.0, 5.0),))][:len(tops)]
    cuts = [Cut("contact", "diff", "metal1", 0.16, 0.2, (0.07, 0.07), (0.0, 0.05), "Cnt.a, Cnt.b1, Cnt.c, M1.c, M1.c1, Cnt.f",
                away=(("poly", 0.11),)),
            Cut("contact", "poly", "metal1", 0.16, 0.2, (0.07, 0.07), (0.0, 0.05), "Cnt.a, Cnt.b1, Cnt.d, M1.c, M1.c1, Cnt.e",
                away=(("diff", 0.14),)),
            Cut("via1", "metal1", "metal2", 0.19, 0.29, (0.01, 0.05), (0.005, 0.05), "V1.a, V1.b1, V1.c, V1.c1, M2.c, M2.c1")]
    cuts += [Cut(f"via{i}", f"metal{i}", f"metal{i + 1}", 0.19, 0.29, (0.005, 0.05), (0.005, 0.05),
                 f"V{i}.a, V{i}.b1, V{i}.c, V{i}.c1, M{i + 1}.c, M{i + 1}.c1") for i in range(2, thin)]
    cuts.append(Cut("topvia1", f"metal{thin}", "topmetal1", 0.42, 0.42, (0.1, 0.1), (0.42, 0.42), "TV1.a, TV1.b, TV1.c, TV1.d"))
    if len(tops) > 1:
        cuts.append(Cut("topvia2", "topmetal1", "topmetal2", 0.9, 1.06, (0.5, 0.5), (0.5, 0.5), "TV2.a, TV2.b, TV2.c, TV2.d"))
    return Stack(diff="diff", tap=None, poly="poly", well="nwell", conductors=tuple(conductors), cuts=tuple(cuts),
                 plates=(Plate("mim", "metal5", "topvia1", also=("vmim",)),) if thin == 5 else ())


# IHP SG13G2: IHP-Open-PDK through ciel (family ihp-sg13), decks in libs.tech/klayout/tech.
# Exercised on ihp-sg13g2 (89d42ee6): KLayout DRC of sg13g2_inv_1 (clean) and LVS against its CDL (matches), ngspice 47
# with the OSDI models at mos_tt / mos_ss / mos_ff.
_IHP_LAYERS = {"diff": (1, 0), "psd": (14, 0), "nsd": (7, 0), "poly": (5, 0), "contact": (6, 0), "nwell": (31, 0),
               "metal1": (8, 0), "via1": (19, 0), "metal2": (10, 0), "via2": (29, 0), "metal3": (30, 0), "via3": (49, 0),
               "metal4": (50, 0), "via4": (66, 0), "metal5": (67, 0), "topvia1": (125, 0), "topmetal1": (126, 0),
               "topvia2": (133, 0), "topmetal2": (134, 0), "passiv": (9, 0), "edgeseal": (39, 0), "prboundary": (189, 0),
               "metal1.label": (8, 25), "metal2.label": (10, 25), "metal3.label": (30, 25), "metal4.label": (50, 25),
               "metal5.label": (67, 25), "topmetal1.label": (126, 25), "topmetal2.label": (134, 25),
               "metal1.pin": (8, 2), "metal2.pin": (10, 2), "metal3.pin": (30, 2), "metal4.pin": (50, 2),
               "metal5.pin": (67, 2), "topmetal1.pin": (126, 2), "topmetal2.pin": (134, 2), "mim": (36, 0), "vmim": (129, 0)}
# the PDK ships its transistor models as Verilog-A; `pixi run osdi` compiles them with OpenVAF into the store
# (Pdk.osdi_dir), each into <name>.osdi; Pdk.osdi_files says where each is found
_IHP_OSDI = ("psp103", "psp103_nqs", "r3_cmc", "mosvar", "cap_cmomi", "cap_cmomf")

IHP = Profile(
    family="ihp-sg13g2", label="IHP SG13G2", verified=True, layers=_IHP_LAYERS,
    metals=("metal1", "metal2", "metal3", "metal4", "metal5", "topmetal1", "topmetal2"), max_metal="topmetal2",
    klayout_dir="tech", drc_klayout=("ihp-sg13g2.drc",), drc_switches="negated", lvs_klayout=("sg13g2.lvs",),
    magic_rc="{name}.magicrc", netgen_setup="{name}_setup.tcl", pcells=True,
    # drc(full) exercised on ihp-sg13g2: M1.a in a cell and in the top cell, clean on sg13g2_inv_1; it has rules the
    # KLayout deck's default run leaves out (M1.d, Gat.e minimum areas, LU.a / LU.b tap distances on a lone PCell)
    magic_drc=True,
    # Activ of the well's own implant, as the LVS deck's ptap / ntap. The deck joins a salicided source / drain butted
    # to a tap to the tap (nsd_ptap_abutt, psd_ntap_abutt): a tap of another net there is an LVS short, and the ERC
    # finds the junction only under SalBlock, where it is a diode (test_layout_checks.py)
    erc_tap=(("diff", "psd", "-nwell"), ("diff", "nwell", "-psd")),
    # its Magic tech reads Metal1-TopMetal2 text (n/25) as labels; exercised with two sg13g2_inv_1 joined in metal2
    # (test_parasitics.py)
    net_labels=(("contact", "metal1.label"), ("via1", "metal2.label"), ("via2", "metal3.label"),
                ("via3", "metal4.label"), ("via4", "metal5.label"), ("topvia1", "topmetal1.label"),
                ("topvia2", "topmetal2.label")),
    # IHP's own PyCell API on KLayout; the library is bound to the sg13g2 technology
    pcell_lib=PcellLib(paths=("python", "python/pycell4klayout-api/source/python"), module="sg13g2_pycell_lib",
                       library="SG13_dev", technology="sg13g2"),
    # V1.a 0.19, V1.c / V1.c1 metal1 enclosure 0.01 / 0.05 (two opposite sides), Mn.c 0.005, Mn.b 0.21; the PCells draw no
    # gate contacts: the gates are joined by a poly bar below the diffusion with a contact on each gate (Cnt.a 0.16, Cnt.d
    # poly enclosure 0.07, Cnt.e 0.14 from Activ) under a metal1 pad (M1.c / c1 0 / 0.05, M1.d area 0.09: 0.3 x 0.3)
    strap=Strap(via=0.19, enc1=(0.05, 0.01), enc2=(0.05, 0.01), m2_space=0.21, gate="poly", poly_w=0.13,
                layers=("diff", "poly", "metal1", "via1", "metal2"), cont=0.16, cont_poly=0.07, cont_m1=(0.05, 0.0), m1_min=0.30),
    model_lib="libs.tech/ngspice/models/cornerMOSlv.lib", corners=("mos_tt", "mos_ss", "mos_ff", "mos_sf", "mos_fs"),
    mismatch_section="{corner}_mismatch",              # mos_tt_mismatch ... with the symbols' mm_ok=1; ngspice 47, not in a test
    osdi=_IHP_OSDI,
    pin_label_layers=("metal1.label", "metal2.label", "metal3.label", "metal4.label", "metal5.label",
                      "topmetal1.label", "topmetal2.label"),
    stdcells=((r"sg13g2_\w+", "sg13g2_stdcell"),),     # sg13g2_inv_1 (the IO cells, sg13g2_io, are not in its GDS)
    stack=_ihp_stack(5, ("topmetal1", "topmetal2")),
)

# SG13CMOS5L: the same front end with four thin metals and one thick one; ciel installs it next to SG13G2. Its DRC deck
# runs; the PDK has no LVS runset and no cell libraries yet.
IHP_CMOS5L = Profile(
    family="ihp-sg13cmos5l", label="IHP SG13CMOS5L", verified=False,
    layers={k: v for k, v in _IHP_LAYERS.items() if not k.startswith(("metal5", "via4", "topmetal2", "topvia2", "mim", "vmim"))},
    metals=("metal1", "metal2", "metal3", "metal4", "topmetal1"), max_metal="topmetal1",
    klayout_dir="tech", drc_klayout=("ihp-sg13cmos5l.drc",), drc_switches="negated",
    magic_rc="{name}.magicrc", netgen_setup="{name}_setup.tcl", magic_drc=True,      # as SG13G2 (M1.a, in a test)
    model_lib="libs.tech/ngspice/models/cornerMOSlv.lib", corners=("mos_tt", "mos_ss", "mos_ff", "mos_sf", "mos_fs"),
    mismatch_section="{corner}_mismatch",              # as SG13G2 (not exercised)
    osdi=_IHP_OSDI,
    limits=("no KLayout LVS runset in the PDK", "no standard cells or device symbols in the PDK",
            "simulation set up as for SG13G2 but not exercised"),
    stack=_ihp_stack(4, ("topmetal1",)),
)

GENERIC = Profile(family="generic", label="Unknown process", verified=False, layers={}, metals=(), max_metal="")

PROFILES = {p.family: p for p in (SKY130, GF180, IHP, IHP_CMOS5L)}


def for_family(family: str | None) -> Profile:
    f = (family or "").lower()
    for k, p in PROFILES.items():
        if f == k or f.startswith(k.split("-")[0]):
            return p
    return GENERIC


def for_pdk(name: str | None, family: str | None) -> Profile:
    """The profile of a PDK: by its name first (ciel puts sg13g2 and sg13cmos5l under one family, ihp-sg13), then
    by its family; with the variant's own stack where it has one (gf180mcuA's three metals)."""
    n = (name or "").lower()
    for k, p in PROFILES.items():
        if n == k or (n.startswith(k) and n[len(k):] in ("a", "b", "c", "d", "e", "f")):
            st = next((v for key, v in p.stacks.items() if key.lower() == n), None)
            return replace(p, stack=st) if st is not None else p
    return for_family(family)


def resolve(base: Path | None, patterns: tuple[str, ...]) -> list[str]:
    """File names under `base` matching the patterns (in pattern order)."""
    if base is None or not base.is_dir():
        return []
    out: list[str] = []
    for pat in patterns:
        for f in sorted(base.glob(pat)):
            if f.is_file() and f.name not in out:
                out.append(f.name)
    return out
