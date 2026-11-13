"""Hand-routes the `inv` layout produced by schematic-driven layout and runs LVS, through the Design API.

    python examples/route_inverter.py [PROJECT] [LIB] [URL]

Expects the schematic `inv` (see make_inverter.py) with `Generate missing` already done in the layout
(or it does it). Stacks the pfet above the nfet, joins the gates with a poly bar, routes Y on met1,
ties source and bulk with li1 bridges and puts pin labels (A, Y, VDD, VSS).
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

import klayout.db as kdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from undine.__main__ import DEFAULT_ROOT  # noqa: E402  ($UNDINE_ROOT, default ~/undine-projects)
from undine.layouts import layoutstore  # noqa: E402

PROJECT = sys.argv[1] if len(sys.argv) > 1 else "demo"
LIB = sys.argv[2] if len(sys.argv) > 2 else "mylib"
URL = (sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:8700").rstrip("/") + f"/api/p/{PROJECT}"
CELL = "inv"
PITCH = 3000  # pfet origin above nfet origin (dbu = nm)


def call(path, body=None):
    req = urllib.request.Request(URL + path, data=None if body is None else json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def main():
    lay = f"/l/{LIB}/layout/{CELL}"
    if not any(c["name"] == CELL and "layout" in c["views"] for c in call(f"/l/{LIB}/cells")):
        call(f"/l/{LIB}/layout-cells", {"name": CELL})                     # a fresh project: no layout cell yet
    old = call(f"{lay}/select", {"box": [-10**8, -10**8, 10**8, 10**8], "point": False})  # start from a clean cell
    if old:
        call(f"{lay}/delete", {"refs": [o["ref"] for o in old]})
    call(f"/l/{LIB}/sdl/{CELL}/generate?all=true", {})
    call(f"/l/{LIB}/save", {})
    layers = {l["source"]: l["id"] for l in call(f"{lay}/layers")}
    n, p = (call(f"/l/{LIB}/sdl/{CELL}/probe?inst={i}")["layout"][0] for i in ("M2", "M1"))
    ly = kdb.Layout()
    layoutstore.read(DEFAULT_ROOT / PROJECT / "libs" / LIB, ly)      # the saved cell files
    def origin(inst):  # r0 instances: origin = instance bbox - cell bbox
        cb = ly.cell(inst["cell"]).bbox()
        return inst["bbox"][0] - cb.left, inst["bbox"][1] - cb.bottom

    (nx, ny), (px, py) = origin(n), origin(p)
    call(f"{lay}/move", {"refs": [p["ref"]], "dx": nx - px, "dy": ny + PITCH - py})
    py = ny + PITCH

    def polys(cellname, key):
        c = ly.cell(cellname)
        r = kdb.Region(c.begin_shapes_rec(ly.find_layer(*key)))
        r.merge()
        return sorted((q.bbox() for q in r.each()), key=lambda b: (b.left, b.bottom))

    def tall(boxes):
        return [b for b in boxes if b.height() >= 400]

    ns, ps = tall(polys(n["cell"], (68, 20))), tall(polys(p["cell"], (68, 20)))
    npad = [b for b in polys(n["cell"], (68, 20)) if b.height() < 400][0]
    nli, pli = tall(polys(n["cell"], (67, 20))), tall(polys(p["cell"], (67, 20)))
    npoly, ppoly = polys(n["cell"], (66, 20))[0], polys(p["cell"], (66, 20))[0]

    def box(layer, x1, y1, x2, y2):
        call(f"{lay}/create", {"kind": "box", "layer": layers[layer], "pts": [x1, y1, x2, y2]})

    def label(layer, text, x, y):
        call(f"{lay}/create", {"kind": "text", "layer": layers[layer], "pts": [x, y], "text": text})

    # gates: one poly bar between the two devices
    box("66/20", nx + npoly.left, ny + npoly.top - 10, nx + npoly.right, py + ppoly.bottom + 10)
    # Y: left source/drain strips joined on met1
    box("68/20", nx + ns[0].left, ny + ns[0].bottom, nx + ns[0].right, py + ps[0].top)
    # source + bulk of each device tied on li1 (strip is li1[1], bulk tap is the last li1 strip)
    for (ox, oy, li, name) in ((nx, ny, nli, "VSS"), (px + (nx - px), py, pli, "VDD")):
        s_, t_ = li[1], li[-1]
        yc = (max(s_.bottom, t_.bottom) + min(s_.top, t_.top)) // 2
        box("67/20", ox + s_.right - 20, oy + yc - 100, ox + t_.left + 20, oy + yc + 100)
        label("67/5", name, ox + (s_.right + t_.left) // 2, oy + yc)
    label("68/5", "A", nx + (npad.left + npad.right) // 2, ny + (npad.bottom + npad.top) // 2)
    label("68/5", "Y", nx + (ns[0].left + ns[0].right) // 2, ny + (ns[0].bottom + ns[0].top) // 2)
    call(f"/l/{LIB}/save", {})

    job = call("/lvs", {"lib": LIB, "cell": CELL})
    while job["state"] in ("queued", "running"):
        time.sleep(1)
        job = call(f"/jobs/{job['id']}")
    print(job["state"], json.dumps(job.get("summary") or job.get("error")))
    res = call(f"/jobs/{job['id']}/lvs")
    for k in ("devices", "pins", "nets"):
        print(k, [(r["layout"], r["schematic"], r["status"]) for r in res["detail"][k]])


main()
