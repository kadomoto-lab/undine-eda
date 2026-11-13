"""Rule-correct geometry on a PDK's stack (pdkprofile.Stack): via stacks, taps and guard rings, and L / Z routes, as
shapes [(profile layer name, kdb.Box in dbu)] for the layout tools to draw (agent_tools: add_via, route, add_tap,
guard_ring). Nothing here touches a layout; what the shapes would touch is checked with connectivity.Nets.

Every coordinate is on the PDK's grid. An enclosure "on one of two adjacent sides" (sky130's via.5a, li.5 ...) is met
by giving it on two opposite sides, along `direction` (h: left and right; v: bottom and top), so every corner has it.
Pads grow to their conductor's minimum width and area along that direction."""
from __future__ import annotations

import math

import klayout.db as kdb

from undine.pdks.pdkprofile import Cut, Profile, Stack


class DrawError(ValueError):
    """What cannot be drawn rule-correct, said so the caller can change the request."""


Shape = tuple[str, kdb.Box]


def _d(v: float, dbu: float) -> int:
    return int(round(v / dbu))


class Rules:
    """A stack's rules in dbu, snapped to the grid."""

    def __init__(self, profile: Profile, dbu: float):
        if profile.stack is None:
            raise DrawError(f"{profile.label} has no layer stack in Undine yet (its via, tap and spacing rules): draw "
                            "with add_box / add_path and check with run_checks")
        self.st: Stack = profile.stack
        self.dbu = dbu
        self.grid = max(1, _d(profile.grid_nm * 1e-3, dbu))
        self.profile = profile

    def d(self, v: float) -> int:
        return _d(v, self.dbu)

    def snap(self, v: int) -> int:
        return int(round(v / self.grid)) * self.grid

    def up(self, v: int) -> int:
        return -(-v // self.grid) * self.grid

    def chain(self, frm: str, to: str) -> list[Cut]:
        """The cuts from one layer of the stack to another (either way up), bottom first."""
        order = self.st.order()
        for n in (frm, to):
            if n not in order:
                plates = {p.name for p in self.st.plates}
                raise DrawError(f"{n} is not a layer of the stack ({', '.join(order)})" +
                                ("; a capacitor plate is contacted by its PCell's own cuts: route to its metal" if n in plates else ""))
        lo, hi = sorted((frm, to), key=order.index)
        if lo == hi:
            raise DrawError(f"from_layer and to_layer are both {lo}")
        front = {self.st.diff, *([self.st.tap] if self.st.tap else []), self.st.poly}
        if hi in front:
            raise DrawError(f"no cut joins {lo} and {hi} directly: contacts go from {lo} up to the first metal")
        out, cur = [], lo
        while cur != hi:
            c = next((c for c in self.st.cuts if c.below == cur), None)
            if c is None:
                raise DrawError(f"the stack has no cut above {cur}")
            out.append(c)
            cur = c.above
        return out


def _array(r: Rules, c: Cut, x: int, y: int, cols: int, rows: int) -> kdb.Box:
    s, p = r.d(c.size), r.d(c.space)
    w, h = cols * s + (cols - 1) * p, rows * s + (rows - 1) * p
    x1, y1 = r.snap(x - w // 2), r.snap(y - h // 2)
    return kdb.Box(x1, y1, x1 + w, y1 + h)


def _cuts(r: Rules, c: Cut, a: kdb.Box) -> list[kdb.Box]:
    s, p = r.d(c.size), r.d(c.space)
    cols, rows = (a.width() + p) // (s + p), (a.height() + p) // (s + p)
    return [kdb.Box(a.left + i * (s + p), a.bottom + j * (s + p), a.left + i * (s + p) + s, a.bottom + j * (s + p) + s)
            for j in range(rows) for i in range(cols)]


def _enclose(r: Rules, a: kdb.Box, enc: tuple[float, float], horizontal: bool) -> kdb.Box:
    lo, hi = r.d(enc[0]), r.d(enc[1])
    return a.enlarged(hi, lo) if horizontal else a.enlarged(lo, hi)


def _fit(r: Rules, b: kdb.Box, layer: str, horizontal: bool, at_least: kdb.Box | None = None) -> kdb.Box:
    """A pad grown (about its centre) to its conductor's minimum width and area, and to `at_least`."""
    if at_least is not None:
        b = b + at_least
    c = r.st.conductor(layer)
    if c is None:
        return b
    wmin = r.up(r.d(c.width))
    if b.width() < wmin:
        g = r.up((wmin - b.width() + 1) // 2)
        b = b.enlarged(g, 0)
    if b.height() < wmin:
        g = r.up((wmin - b.height() + 1) // 2)
        b = b.enlarged(0, g)
    area = c.area / r.dbu ** 2
    if b.width() * b.height() < area:
        if horizontal:
            g = r.up(math.ceil((area / b.height() - b.width()) / 2))
            b = b.enlarged(g, 0)
        else:
            g = r.up(math.ceil((area / b.width() - b.height()) / 2))
            b = b.enlarged(0, g)
    return b


def via_stack(r: Rules, x: int, y: int, frm: str, to: str, rows: int | None = None, cols: int | None = None,
              size: tuple[int, int] | None = None, horizontal: bool = True) -> dict:
    """A stack of cut arrays from `frm` to `to` centred at x, y (dbu), with the pads each conductor needs. rows / cols:
    the same array at every level; size (w, h in dbu): as many cuts as fit in that footprint at each level, the pads at
    least that big; neither: one cut per level. Returns {shapes: [(layer, box)], needs: [(layer, box)] (the diffusion
    or tap that must already enclose a contact: not drawn), levels: [{cut, rows, cols, size, pitch, box}], pads}."""
    chain = r.chain(frm, to)
    x, y = r.snap(x), r.snap(y)
    levels = []
    for c in chain:
        if rows or cols:
            nr, nc = max(1, int(rows or 1)), max(1, int(cols or 1))
        elif size:
            ex = max(c.enc_below[1 if horizontal else 0], c.enc_above[1 if horizontal else 0])
            ey = max(c.enc_below[0 if horizontal else 1], c.enc_above[0 if horizontal else 1])
            s, p = r.d(c.size), r.d(c.space)
            nc = max(1, (size[0] - 2 * r.d(ex) + p) // (s + p))
            nr = max(1, (size[1] - 2 * r.d(ey) + p) // (s + p))
        else:
            nr = nc = 1
        levels.append((c, _array(r, c, x, y, nc, nr), nr, nc))
    need: dict[str, kdb.Box] = {}
    for c, a, _, _ in levels:
        for layer, enc in ((c.below, c.enc_below), (c.above, c.enc_above)):
            b = _enclose(r, a, enc, horizontal)
            need[layer] = need[layer] + b if layer in need else b
    footprint = kdb.Box(x - size[0] // 2, y - size[1] // 2, x - size[0] // 2 + size[0], y - size[1] // 2 + size[1]) if size else None
    front = {r.st.diff, *([r.st.tap] if r.st.tap else [])}
    shapes: list[Shape] = []
    needs: list[Shape] = []
    pads: dict[str, kdb.Box] = {}
    for layer, b in need.items():
        if layer in front:
            needs.append((layer, b))
            continue
        pads[layer] = _fit(r, b, layer, horizontal, footprint)
        shapes.append((layer, pads[layer]))
    for c, a, _, _ in levels:
        shapes += [(c.name, b) for b in _cuts(r, c, a)]
        shapes += [(layer, a.enlarged(r.d(enc), r.d(enc))) for layer, enc, _w, _s in c.extra]
    return {"shapes": shapes, "needs": needs, "pads": pads,
            "levels": [{"cut": c.name, "rows": nr, "cols": nc, "size": c.size, "pitch": round(c.size + c.space, 4),
                        "box": a, "rules": c.rules} for c, a, nr, nc in levels]}


def bridge(r: Rules, mine: kdb.Region, around: kdb.Region, width: int, space: int) -> tuple[list[kdb.Box], list[kdb.Box]]:
    """New shapes of a layer the decks want merged with others of their kind closer than `space` (sky130's npc,
    npc.2: "should be manually merged"), next to those others (`around`, from the layout): the fills that bridge the
    gaps (the shapes closed by half the spacing, square-cornered, as Magic bridges npc when it writes GDS), and where
    the result near the new shapes still breaks the spacing or `width` (a corner-to-corner gap no fill closes)."""
    h = r.up((space + 1) // 2)
    both = (mine + around).merged()
    near = mine.sized(space)
    fill = ((both.sized(h).sized(-h) - both) & near).merged()
    res = (both + fill).merged()
    bad = [ep.bbox() for check in (res.space_check(space), res.width_check(width)) for ep in check.each()
           if not near.interacting(kdb.Region(ep.bbox().enlarged(1, 1))).is_empty()]
    boxes = [b for p in fill.each() for b in ([p.bbox()] if p.is_box() else
                                              [t.bbox() for t in p.decompose_trapezoids(kdb.Polygon.TD_htrapezoids)])]
    return boxes, bad


def segments(r: Rules, layer: str, points: list[tuple[int, int]], width: int) -> list[Shape]:
    """A Manhattan wire through `points` (dbu) as one box per segment, each `width` wide and reaching width / 2 past
    its ends, so that bends are square and an end covers its point."""
    lo = (width // 2) // r.grid * r.grid
    hi = width - lo
    out = []
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        if x1 != x2 and y1 != y2:
            raise DrawError("a route segment must be horizontal or vertical")
        out.append((layer, kdb.Box(min(x1, x2) - lo, min(y1, y2) - lo, max(x1, x2) + hi, max(y1, y2) + hi)))
    return out


def manhattan(a: tuple[int, int], b: tuple[int, int], steps: int = 3, grid: int = 1) -> list[list[tuple[int, int]]]:
    """The ways from a to b with at most two bends, shortest first: straight, the two L's, then Z's (a middle leg at
    `steps` positions between them, each way round, on the grid: a and b are on it already)."""
    (x1, y1), (x2, y2) = a, b
    if x1 == x2 or y1 == y2:
        return [[a, b]]
    out = [[a, (x2, y1), b], [a, (x1, y2), b]]
    on = lambda v: int(round(v / grid)) * grid
    for k in range(1, steps + 1):
        f = k / (steps + 1)
        xm, ym = on(x1 + (x2 - x1) * f), on(y1 + (y2 - y1) * f)
        out += [[a, (xm, y1), (xm, y2), b], [a, (x1, ym), (x2, ym), b]]
    return [w for i, w in enumerate(out) if w not in out[:i]]


def detours(a: tuple[int, int], b: tuple[int, int], blocked: kdb.Box, clear: int, grid: int = 1) -> list[list[tuple[int, int]]]:
    """The ways round a box of obstacles, shortest first: from each end square out to a side of the box, at least
    `clear` outside it on the grid, along that side, and back (a U, or an L-like dogleg when an end is beyond the box
    already). A way that runs past an end and doubles back on itself (ends level with each other, a side beyond them),
    or is the straight way again, is not one."""
    (x1, y1), (x2, y2) = a, b
    down = lambda v: (v // grid) * grid
    up = lambda v: -(-v // grid) * grid
    ways = [[a, (x, y1), (x, y2), b] for x in (down(blocked.left - clear), up(blocked.right + clear))] + \
           [[a, (x1, y), (x2, y), b] for y in (down(blocked.bottom - clear), up(blocked.top + clear))]
    out = []
    for w in map(simplify, ways):
        if w is not None and len(w) > 2 and w not in out:
            out.append(w)
    return sorted(out, key=lambda w: sum(abs(p[0] - q[0]) + abs(p[1] - q[1]) for p, q in zip(w, w[1:])))


def simplify(way: list[tuple[int, int]]) -> list[tuple[int, int]] | None:
    """A Manhattan way without repeated points and without the points it runs straight through; None when it turns
    back along the line it came on."""
    pts: list[tuple[int, int]] = []
    for p in way:
        if pts and p == pts[-1]:
            continue
        if len(pts) >= 2:
            d1 = (pts[-1][0] - pts[-2][0], pts[-1][1] - pts[-2][1])
            d2 = (p[0] - pts[-1][0], p[1] - pts[-1][1])
            if d1[0] * d2[1] == d1[1] * d2[0]:         # on one line: through it, or back along it
                if d1[0] * d2[0] + d1[1] * d2[1] < 0:
                    return None
                pts.pop()
        pts.append(p)
    return pts


def nearest(region: kdb.Region, p: tuple[int, int]) -> tuple[int, int] | None:
    """The point of a region closest to p (its rectangles' clamps)."""
    best = None
    for poly in region.merged().each():
        for t in (poly.decompose_trapezoids(kdb.Polygon.TD_htrapezoids) if not poly.is_box() else [poly]):
            b = t.bbox()
            q = (min(max(p[0], b.left), b.right), min(max(p[1], b.bottom), b.top))
            dist = (q[0] - p[0]) ** 2 + (q[1] - p[1]) ** 2
            if best is None or dist < best[0]:
                best = (dist, q)
    return best[1] if best else None


# ------------------------------------------------------------------------------------------ taps and guard rings


def _tap_cuts(r: Rules, box: kdb.Box, c: Cut, horizontal: bool) -> list[kdb.Box]:
    """Cuts of `c` in a strip along its long side, enclosed by c.enc_below (across) and the larger value (along)."""
    s, p = r.d(c.size), r.d(c.space)
    along, across = r.d(c.enc_below[1]), r.d(c.enc_below[0])
    inner = box.enlarged(-along, -across) if horizontal else box.enlarged(-across, -along)
    if inner.width() < s or inner.height() < s or inner.empty():
        return []
    cols, rows = (inner.width() + p) // (s + p), (inner.height() + p) // (s + p)
    return _cuts(r, c, _array(r, c, inner.center().x, inner.center().y, cols, rows))


def _contact(r: Rules, below: str) -> Cut:
    c = next((c for c in r.st.cuts if c.below == below), None)
    if c is None:
        raise DrawError(f"the stack has no contact on {below}")
    return c


def tap(r: Rules, box: kdb.Box, kind: str, metal: str = "met1") -> list[Shape]:
    """A substrate (ptap) or n-well (ntap) tap filling `box` (dbu): the tap with its implant (and n-well), contacts
    along it, the first metal over them, and with metal met1 mcon cuts and a met1 strap so it can be routed."""
    t = r.st.taps
    if t is None or not r.st.tap:
        raise DrawError(f"{r.profile.label} has no tap rules in Undine")
    if kind not in t.implant:
        raise DrawError(f"kind is {' or '.join(t.implant)}")
    lic = _contact(r, r.st.tap)
    horizontal = box.width() >= box.height()
    cuts = _tap_cuts(r, box, lic, horizontal)
    if not cuts:
        need = lic.size + 2 * lic.enc_below[0], lic.size + 2 * lic.enc_below[1]
        raise DrawError(f"the tap is too small for a contact: at least {need[1]:g} x {need[0]:g} um ({lic.rules})")
    out: list[Shape] = [(r.st.tap, box), *((lic.name, b) for b in cuts)]
    imp = _grow_to(r, box.enlarged(r.d(t.implant_enc), r.d(t.implant_enc)), r.d(t.implant_width))
    out.append((t.implant[kind], imp))
    if kind == "ntap":
        well = r.st.conductor(r.st.well)
        out.append((r.st.well, _grow_to(r, box.enlarged(r.d(t.well_enc), r.d(t.well_enc)), r.d(0.84) if well is None else r.d(well.width))))
    out += _straps(r, cuts, lic, metal, horizontal)
    return out


def _grow_to(r: Rules, b: kdb.Box, wmin: int, hmin: int | None = None) -> kdb.Box:
    """A box grown about its centre to at least wmin wide and hmin (default wmin) high, on the grid."""
    hmin = wmin if hmin is None else hmin
    gx = r.up(max(0, wmin - b.width()) // 2 + 1) if b.width() < wmin else 0
    gy = r.up(max(0, hmin - b.height()) // 2 + 1) if b.height() < hmin else 0
    return b.enlarged(gx, gy)


def _straps(r: Rules, cuts: list[kdb.Box], lic: Cut, metal: str, horizontal: bool) -> list[Shape]:
    """The first metal over a row of contacts and, up to `metal`, the cuts and straps above it: each strip grown to
    hold a row of the next cut with its enclosures (a one-contact-high tap's met1 is too narrow for a via)."""
    order = [c.name for c in r.st.conductors]
    if metal not in order or order.index(metal) < order.index(lic.above):
        raise DrawError(f"metal is {lic.above} or a metal above it")
    span = kdb.Box()
    for b in cuts:
        span = span + b
    out: list[Shape] = []
    below, strip = lic.above, _fit(r, _enclose(r, span, lic.enc_above, horizontal), lic.above, horizontal)
    while below != metal:
        c = _contact(r, below)
        s, p = r.d(c.size), r.d(c.space)
        along, across = r.d(c.enc_below[1]), r.d(c.enc_below[0])
        ex, ey = (along, across) if horizontal else (across, along)
        strip = _grow_to(r, strip, s + 2 * ex, s + 2 * ey)
        out.append((below, strip))
        inner = strip.enlarged(-ex, -ey)
        cols, rows = (inner.width() + p) // (s + p), (inner.height() + p) // (s + p)
        a = _array(r, c, inner.center().x, inner.center().y, cols, 1) if horizontal else \
            _array(r, c, inner.center().x, inner.center().y, 1, rows)
        out += [(c.name, b) for b in _cuts(r, c, a)]
        strip = _fit(r, _enclose(r, a, c.enc_above, horizontal), c.above, horizontal)
        below = c.above
    out.append((below, strip))
    return out


def guard_ring(r: Rules, inner: kdb.Box, kind: str, width: int | None = None, metal: str = "met1",
               fill_well: bool = True) -> list[Shape]:
    """A closed tap ring around `inner` (dbu; the ring starts at its edge): four strips that overlap at the corners,
    their contacts on one lattice per side so that cuts meeting at a corner keep their spacing, the implant over the
    ring, an ntap ring's n-well over the whole ring and what it encloses (fill_well), and the metal strap ring."""
    t = r.st.taps
    if t is None or not r.st.tap:
        raise DrawError(f"{r.profile.label} has no tap rules in Undine")
    if kind not in t.implant:
        raise DrawError(f"kind is {' or '.join(t.implant)}")
    lic = _contact(r, r.st.tap)
    order = [c.name for c in r.st.conductors]
    if metal not in order or order.index(metal) < order.index(lic.above):
        raise DrawError(f"metal is {lic.above} or a metal above it")
    chain = [lic, *r.chain(lic.above, metal)] if metal != lic.above else [lic]
    wmin = ring_width(r, chain)
    if width is not None and width < wmin:
        raise DrawError(f"a ring {width * r.dbu:g} um wide is too narrow up to {metal}: it needs {wmin * r.dbu:g} um "
                        f"(a row of {', '.join(c.name for c in chain)} with their enclosures, and each metal's width)")
    w = r.up(width or wmin)
    o = inner.enlarged(w, w)
    sides = [kdb.Box(o.left, o.bottom, o.right, inner.bottom), kdb.Box(o.left, inner.top, o.right, o.top),
             kdb.Box(o.left, inner.bottom, inner.left, inner.top), kdb.Box(inner.right, inner.bottom, o.right, inner.top)]
    out: list[Shape] = [(r.st.tap, b) for b in sides]
    for cut in chain:
        out += [(cut.name, b) for b in _ring_cuts(r, inner, o, w, cut)]
        out += [(cut.above, b) for b in sides]           # each conductor as wide as the ring: it encloses its cuts
    e = r.d(t.implant_enc)
    imp = t.implant[kind]
    out += [(imp, b) for b in (kdb.Box(o.left - e, o.bottom - e, o.right + e, inner.bottom + e),
                               kdb.Box(o.left - e, inner.top - e, o.right + e, o.top + e),
                               kdb.Box(o.left - e, inner.bottom + e, inner.left + e, inner.top - e),
                               kdb.Box(inner.right - e, inner.bottom + e, o.right + e, inner.top - e))]
    if kind == "ntap" and fill_well:
        g = r.d(t.well_enc)
        out.append((r.st.well, o.enlarged(g, g)))
    return out


def ring_width(r: Rules, chain: list[Cut]) -> int:
    """The narrowest tap ring (dbu, on the grid) that holds a row of each cut of `chain` (the tap contact up to the
    ring's metal) with its enclosures across the ring, and is as wide as each conductor's minimum."""
    w = 0
    for c in chain:
        cond = r.st.conductor(c.above)
        w = max(w, r.d(c.size + 2 * max(c.enc_below[0], c.enc_above[0])), r.d(cond.width) if cond else 0)
    return r.up(w)


def _ring_cuts(r: Rules, inner: kdb.Box, o: kdb.Box, w: int, c: Cut) -> list[kdb.Box]:
    """A cut's arrays along a ring of width w between `inner` and `o`: rows across the bottom and top strips (corners
    included, `along` from the outer ends), columns along the sides starting a spacing past those rows."""
    s, p = r.d(c.size), r.d(c.space)
    across, along = r.d(max(c.enc_below[0], c.enc_above[0])), r.d(max(c.enc_below[1], c.enc_above[1]))
    k = (w - 2 * across + p) // (s + p)                     # cuts across the ring
    if k <= 0:
        raise DrawError(f"the ring is too narrow for a {c.name} ({w * r.dbu:g} um; {c.size + 2 * across * r.dbu:g} um needed)")
    n = (o.width() - 2 * along + p) // (s + p)
    if n <= 0:
        return []
    rows = [_array(r, c, o.center().x, y, n, k) for y in ((o.bottom + inner.bottom) // 2, (inner.top + o.top) // 2)]
    out = [b for a in rows for b in _cuts(r, c, a)]
    lo, hi = rows[0].top + p, rows[1].bottom - p              # the sides' columns keep a spacing from both rows
    m = (hi - lo + p) // (s + p)
    if m > 0:
        ext = m * s + (m - 1) * p
        y1 = lo + ((hi - lo - ext) // 2) // r.grid * r.grid
        for x in ((o.left + inner.left) // 2, (inner.right + o.right) // 2):
            a = _array(r, c, x, 0, k, 1)
            out += _cuts(r, c, kdb.Box(a.left, y1, a.right, y1 + ext))
    return out
