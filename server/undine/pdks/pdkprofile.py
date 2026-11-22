"""What Undine needs to know about a process, in one place.

Everything that used to be hard-wired to sky130 (layer numbers, rule-deck names and options, model paths, Magic /
Netgen setup) is described by a profile per process. `for_pdk` picks it from the PDK's name, then its family. The
profiles are data: a profile package per ciel family (profile_sky130, profile_gf180mcu, profile_ihp_sg13), whose
profile.toml profiledata.py reads into the dataclasses below; Undine's own copies are built in
(undine/data/profiles/), and SKY130, GF180, IHP, IHP_CMOS5L and PROFILES are read from them on first use. A profile
with `verified=False` has not been exercised against an installed PDK in Undine's tests (pdks/tested.py records
which have); `limits` names what is still missing, and Undine says so instead of pretending.
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field, replace
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable

from undine.pdks.formulas import mim_area_perimeter as _mim  # noqa: F401  (LvsElement.params of sky130's MIMs)

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
    extra: tuple[tuple[str, float, float, float], ...] = ()
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
    verified: bool                                     # Undine's tests ran it (pdks/tested.py, not profile.toml)
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
    # PDK name -> -rd values every DRC / LVS run gets (metal stack)
    deck_vars: dict[str, dict] = field(default_factory=dict)
    magic_rc: str | None = None                        # under libs.tech/magic; {name} is the PDK name (sky130A)
    magic_drc: bool = False                            # run_checks runs Magic's drc(full) next to the KLayout deck
                                                       # (the deck lacks rules of Magic's that shuttle prechecks run);
                                                       # set once drc(full) has been run on the PDK's cells (tested.py)
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
    # PDK name -> its own Stack where the variants' metal stacks differ
    stacks: dict[str, Stack] = field(default_factory=dict)

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




GENERIC = Profile(family="generic", label="Unknown process", verified=False, layers={}, metals=(), max_metal="")

# the built-in profiles by module attribute, as Undine's code and tests name them: Profile.family of each
_NAMED = {"SKY130": "sky130", "GF180": "gf180mcu", "IHP": "ihp-sg13g2", "IHP_CMOS5L": "ihp-sg13cmos5l"}


@functools.cache
def profiles() -> dict[str, Profile]:
    """The built-in profiles by name (Profile.family), in package order: the processes of profiledata.builtin()."""
    from undine.pdks import profiledata
    out: dict[str, Profile] = {}
    for s in profiledata.builtin():
        for name, p in s.profiles.items():
            if name in out:
                raise profiledata.ProfileError(f"built-in {s.package}: profile.toml: {name!r} is a profile of another "
                                               f"built-in package too")
            out[name] = p
    return out


def __getattr__(name: str):
    """SKY130, GF180, IHP, IHP_CMOS5L and PROFILES ({name: Profile}): read from the built-in profile.toml files on
    first use, the same objects every time."""
    if name == "PROFILES":
        return profiles()
    if name in _NAMED:
        return profiles()[_NAMED[name]]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def for_family(family: str | None) -> Profile:
    f = (family or "").lower()
    for k, p in profiles().items():
        if f == k or f.startswith(k.split("-")[0]):
            return p
    return GENERIC


_WITH_STACK: dict[tuple[str, str], tuple[Profile, Profile]] = {}


def for_pdk(name: str | None, family: str | None) -> Profile:
    """The profile of a PDK: by its name first (ciel puts sg13g2 and sg13cmos5l under one family, ihp-sg13), then
    by its family; with the variant's own stack where it has one (gf180mcuA's three metals), made once per variant."""
    n = (name or "").lower()
    for k, p in profiles().items():
        if n == k or (n.startswith(k) and n[len(k):] in ("a", "b", "c", "d", "e", "f")):
            st = next((v for key, v in p.stacks.items() if key.lower() == n), None)
            if st is None:
                return p
            got = _WITH_STACK.get((k, n))
            if got is None or got[0] is not p:
                got = _WITH_STACK[(k, n)] = (p, replace(p, stack=st))
            return got[1]
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
