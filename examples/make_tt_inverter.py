"""Builds a Tiny Tapeout SKY130 analog tile (tt_um_undine_inv, 1x2) around the hand-routed `inv` cell, through the Design API.

    python examples/make_tt_inverter.py [PROJECT] [LIB] [URL]

Expects `inv` (schematic, see make_inverter.py; layout, see route_inverter.py). Creates the wrapper schematic
`tt_um_undine_inv` (ua[0] -> A, Y -> ua[1], VDPWR -> VDD, VGND -> VSS) and its layout: prBoundary of the 1x2 tile, every
template pin on met4, VGND / VDPWR power stripes, and met4 routes from the inverter to the pads ua[0] and ua[1].
Units: micrometres in this file, nanometres (dbu) on the wire.
"""
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from undine.checks import tapeout  # noqa: E402
from undine.schematics import xschem
from undine.__main__ import DEFAULT_ROOT  # noqa: E402
from undine.schematics.xschem import XComp, XFile  # noqa: E402

PROJECT = sys.argv[1] if len(sys.argv) > 1 else "demo"
LIB = sys.argv[2] if len(sys.argv) > 2 else "mylib"
BASE = (sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:8700").rstrip("/")
URL = BASE + f"/api/p/{PROJECT}"
TOP = "tt_um_undine_inv"
NM = lambda v: int(round(v * 1000))
IX, IY = 138.0, 30.0              # where the inverter's origin goes


def call(path, body=None):
    req = urllib.request.Request(URL + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def C(sym, x, y, **p):
    return XComp(sym, x, y, 0, 0, p)


def wrapper_schematic():
    f = XFile(version="xschem version=3.4.5 file_version=1.2\n")
    f.comps = [C("inv.sym", 0, 0, name="x1"),
               C("devices/ipin.sym", -120, 0, name="p1", lab="ua[0]"), C("devices/opin.sym", 120, 0, name="p2", lab="ua[1]"),
               C("devices/iopin.sym", 0, -100, name="p3", lab="VDPWR"), C("devices/iopin.sym", 0, 100, name="p4", lab="VGND")]
    f.wires = [(-120, 0, -40, 0, {}), (40, 0, 120, 0, {}), (0, -100, 0, -40, {}), (0, 40, 0, 100, {})]
    return f


def main():
    (DEFAULT_ROOT / PROJECT / "libs" / LIB / f"{TOP}.sch").write_text(xschem.dump(wrapper_schematic()))
    tpl = tapeout.tt_template("1x2")
    lay = f"/l/{LIB}/layout/{TOP}"
    if not any(c["name"] == TOP and "layout" in c["views"] for c in call(f"/l/{LIB}/cells")):
        call(f"/l/{LIB}/layout-cells", {"name": TOP})
    old = call(f"{lay}/select", {"box": [-10**9, -10**9, 10**9, 10**9], "point": False})
    if old:
        call(f"{lay}/delete", {"refs": [o["ref"] for o in old]})
    layers = {l["source"]: l["id"] for l in call(f"{lay}/layers")}

    def box(layer, x1, y1, x2, y2):
        call(f"{lay}/create", {"kind": "box", "layer": layers[layer], "pts": [NM(x1), NM(y1), NM(x2), NM(y2)]})

    def label(layer, text, x, y):
        call(f"{lay}/create", {"kind": "text", "layer": layers[layer], "pts": [NM(x), NM(y)], "text": text})

    def via(start, end, cx, cy, size=0.5):                       # stack centred on (cx, cy)
        call(f"{lay}/pcell", {"pcell": "vias_gen", "params": {"l": size, "w": size, "start_layer": start, "end_layer": end},
                              "x": NM(cx - size / 2), "y": NM(cy - size / 2)})

    die = tpl["die"]
    box("235/4", 0, 0, die[0] / 1000, die[1] / 1000)
    for p in tpl["pins"].values():
        r = p["rect"]
        box("71/20", *[v / 1000 for v in r])
        box("71/16", *[v / 1000 for v in r])               # the precheck reads pins from the pin purpose
    for n in ("ua[0]", "ua[1]"):                                  # the pins this design uses carry a label so LVS sees them
        r = tpl["pins"][n]["rect"]
        label("71/5", n, (r[0] + r[2]) / 2000, (r[1] + r[3]) / 2000)
    # power stripes (met4, 2 um wide, 5 um from the top and bottom edge)
    for n, x in (("VGND", 154.0), ("VDPWR", 157.0)):
        box("71/20", x, 5, x + 2, die[1] / 1000 - 5)
        box("71/16", x, 5, x + 2, die[1] / 1000 - 5)
        label("71/5", n, x + 1, 100)
    # the inverter
    call(f"{lay}/place", {"child": "inv", "x": NM(IX), "y": NM(IY)})
    ax, ay = IX + 0.5, IY + 0.175
    yx, yy = IX + 0.275, IY + 3.0
    sx, sy = IX + 1.2, IY + 1.04         # li landing >= 0.17 um from the device's own li, mcon >= 0.19 um from its contacts
    dx, dy = IX + 1.2, IY + 4.54
    box("67/20", IX + 0.79, IY + 0.94, IX + 1.4, IY + 1.14)     # the bridges' li1 reaches the new contact
    box("67/20", IX + 0.79, IY + 4.44, IX + 1.4, IY + 4.64)
    via("metal1", "metal4", ax, ay)
    via("metal1", "metal4", yx, yy)
    via("li", "metal4", sx, sy, 0.34)
    via("li", "metal3", dx, dy, 0.34)
    w = 0.6
    # Y: inverter -> ua[1] pad
    ya = tpl["pins"]["ua[1]"]["rect"]
    px = (ya[0] + ya[2]) / 2000
    box("71/20", px - w / 2, 1.0, px + w / 2, yy + w / 2)
    box("71/20", px - w / 2, yy - w / 2, yx + w / 2, yy + w / 2)
    # A: ua[0] pad -> inverter
    aa = tpl["pins"]["ua[0]"]["rect"]
    qx = (aa[0] + aa[2]) / 2000
    box("71/20", qx - w / 2, 1.0, qx + w / 2, IY - 20 + w)
    box("71/20", ax - w / 2, IY - 20, qx + w / 2, IY - 20 + w)
    box("71/20", ax - w / 2, IY - 20, ax + w / 2, ay)
    box("70/20", sx - 0.25, sy - 0.25, sx + 0.25, sy + 0.25)      # met3 area >= 0.24 um2 around the small landing
    # VSS: met4 east to the VGND stripe
    box("71/20", sx - 0.25, sy - w / 2, 154.5, sy + w / 2)
    # VDD: met3 east under VGND, up to VDPWR through a met3-met4 stack
    box("70/20", dx - 0.25, dy - 0.25, 158.0, dy + 0.25)
    via("metal3", "metal4", 158.0, dy)
    call(f"/l/{LIB}/save", {})


if __name__ == "__main__":
    main()
