"""The code a PDK profile may name: formulas and stack generators (docs/design/packages.md, PDK profiles).

A profile is data (profile.toml, read by profiledata.py). Where a value is computed, the profile names a function of
this registry and Undine runs it; a name not listed here is refused, so no code comes from a profile. Two kinds:

- `FORMULAS`: `LvsElement.params` (`params = "mim_area_perimeter"`), {name: function of the call's numeric parameters
  (and, where it takes a second argument `model`, the call's model)}.
- `STACKS`: a metal stack made from a few arguments instead of written out (`[profile.stack] generator =
  "gf180_stack@1"`, its arguments beside it). A generator's name carries its version: a changed rule is a new
  generator (`@2`), so a profile that names `@1` keeps drawing as it did. The arguments are typed by the function's
  annotations, the way profiledata converts every other field.
"""
from __future__ import annotations

import re


def mim_area_perimeter(p: dict) -> dict:
    """A MIM capacitor as KLayout's sky130 deck extracts it (MIMCap: A and P compared, C not): area and perimeter of
    one unit from its W and L in um."""
    return {"A": p["w"] * p["l"], "P": 2 * (p["w"] + p["l"])}


def res_length(p: dict, model: str = "") -> dict:
    """A fixed-width poly resistor as KLayout's sky130 deck extracts it (sky130_fd_pr__res_high_po_0p35 ...: the
    class of its width, L compared within 0.5 %, R and W not): its L in um, and its W, the width its model names
    (0p35: 0.35 um) times the multiplier, with m 1. Series resistors add their L in both netlists (the deck's
    simplification); parallel ones keep L and add W, which the deck does not compare: Undine does after it (the
    element's `check`), so two copies in parallel do not match one."""
    out = {"L": p["l"]}
    if (w := re.search(r"_(\d+)p(\d+)$", model)):
        out.update(W=float(f"{w.group(1)}.{w.group(2)}") * p.get("m", p.get("mult", 1.0)), m=1.0)
    return out


def bjt_emitters(p: dict) -> dict:
    """A bipolar transistor as KLayout's sky130 deck compares it: by its number of emitters NE (AE ignored), which its
    simplification adds up over parallel devices (the 24 units of a pnp_05v5 m=24 are one device of NE=24). The SPICE
    reader leaves NE at 1 whatever the multiplier, so the multiplier goes into NE and m is 1."""
    m = p.get("m", p.get("mf", p.get("mult", 1.0)))
    return {"NE": p.get("ne", 1.0) * m, "m": 1.0}


def gf180_stack(thin: int, top: str | None = None, plate: tuple[str, str] | None = None):
    """GF180MCU's stack, from gf180mcu_fd_pv's KLayout deck: metal1 .. metal{thin} thin, `top` above them with the
    9K / 11K MetalTop rules (MT.*), and the MIM's top plate FuseTop (option B: on the metal under the top one, `plate`
    (metal, its via) landing on it; the symbol's G is the metal under it, the deck's P1, as Undine netlists it). Where
    the 3.3 V and the 5 / 6 V rules differ, the larger: a shape is correct inside DualGate and outside it. Contacts and
    vias use the spacing a 4 x 4 array needs (CO.2b, Vn.2b) at every size. No taps: a GF180 tap is COMP with the other
    implant, which these tools do not draw."""
    from undine.pdks.pdkprofile import Conductor, Cut, Plate, Stack
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
                        f"V{i}.1, V{i}.2b, V{i}.3{'a' if i == 1 else 'b'}, V{i}.3c, V{i}.3d, "
                        f"V{i}.4a, V{i}.4b, V{i}.4c"))
    return Stack(diff="comp", tap=None, poly="poly", well="nwell", conductors=tuple(conductors), cuts=tuple(cuts),
                 plates=(Plate("fusetop", plate[0], plate[1], top="B", bottom="G"),) if plate else ())


def ihp_stack(thin: int, tops: tuple[str, ...]):
    """IHP SG13's stack, from its KLayout deck (the maximal rule set): metal1 .. metal{thin} and the thick `tops`, and
    for SG13G2 the MIM's plate on metal5 (TopVia1 on it joins it, as the deck's mim_via, and Vmim, which the cmim
    PCell draws; c0 the top plate, the deck's mim_top). Contacts and vias use the spacing a large array needs (Cnt.b1,
    Vn.b1) at every size; the wide-metal spacings (Mn.e, Mn.f) hold beside any wide shape, whatever the run length. Not
    checked: pSD's 0.09 um from a contact on Activ (Cnt.g1 / g2), the device's own. No taps: an IHP tap is Activ with or
    without pSD, which these tools do not draw."""
    from undine.pdks.pdkprofile import Conductor, Cut, Plate, Stack
    conductors = [Conductor("poly", 0.13, 0.18, 0.09, away=(("diff", 0.07),)),             # Gat.a, b, e, d
                  Conductor("metal1", 0.16, 0.18, 0.09, ((0.3, 0.22), (10.0, 0.6)))]       # M1.a, b, d, e, f
    conductors += [Conductor(f"metal{i}", 0.2, 0.21, 0.144, ((0.39, 0.24), (10.0, 0.6)))
                   for i in range(2, thin + 1)]
    conductors += [Conductor("topmetal1", 1.64, 1.64), Conductor("topmetal2", 2.0, 2.0, wide=((5.0, 5.0),))][:len(tops)]
    cuts = [Cut("contact", "diff", "metal1", 0.16, 0.2, (0.07, 0.07), (0.0, 0.05),
                "Cnt.a, Cnt.b1, Cnt.c, M1.c, M1.c1, Cnt.f", away=(("poly", 0.11),)),
            Cut("contact", "poly", "metal1", 0.16, 0.2, (0.07, 0.07), (0.0, 0.05),
                "Cnt.a, Cnt.b1, Cnt.d, M1.c, M1.c1, Cnt.e", away=(("diff", 0.14),)),
            Cut("via1", "metal1", "metal2", 0.19, 0.29, (0.01, 0.05), (0.005, 0.05),
                "V1.a, V1.b1, V1.c, V1.c1, M2.c, M2.c1")]
    cuts += [Cut(f"via{i}", f"metal{i}", f"metal{i + 1}", 0.19, 0.29, (0.005, 0.05), (0.005, 0.05),
                 f"V{i}.a, V{i}.b1, V{i}.c, V{i}.c1, M{i + 1}.c, M{i + 1}.c1") for i in range(2, thin)]
    cuts.append(Cut("topvia1", f"metal{thin}", "topmetal1", 0.42, 0.42, (0.1, 0.1), (0.42, 0.42),
                    "TV1.a, TV1.b, TV1.c, TV1.d"))
    if len(tops) > 1:
        cuts.append(Cut("topvia2", "topmetal1", "topmetal2", 0.9, 1.06, (0.5, 0.5), (0.5, 0.5),
                        "TV2.a, TV2.b, TV2.c, TV2.d"))
    return Stack(diff="diff", tap=None, poly="poly", well="nwell", conductors=tuple(conductors), cuts=tuple(cuts),
                 plates=(Plate("mim", "metal5", "topvia1", also=("vmim",)),) if thin == 5 else ())


FORMULAS = {"mim_area_perimeter": mim_area_perimeter, "res_length": res_length, "bjt_emitters": bjt_emitters}
STACKS = {"gf180_stack@1": gf180_stack, "ihp_stack@1": ihp_stack}
