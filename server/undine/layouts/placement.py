"""Where generated devices go: the space they keep from one another by the PDK's rules, and the arrangements of unit
devices in one grid (place_array).

Spaces come from the profile (pdkprofile.Space): between two cells side by side, the merged shapes of each layer are
taken as their bounding box, which is exact for the rectangles of a PCell and on the safe side for anything else.
Units of one bulk net may share a well (Space.merge): their wells then overlap, or keep the full space.

The arrangements fill a columns x rows grid with the units of several devices (common centroid, interdigitated, or
rows given cell by cell) and say where each unit went, so the binding of every unit to its device is known.
"""
from __future__ import annotations

import math

import klayout.db as kdb

GAP_UM = 1.0     # the least space between generated devices: room to route between them


def extents(layout: kdb.Layout, cell: kdb.Cell, layers: dict[str, tuple[int, int]]) -> dict[str, kdb.Box]:
    """{profile layer name: bounding box of the cell's shapes on it (all levels)}, the cell's own frame."""
    out = {}
    for name, (l, d) in layers.items():
        li = layout.find_layer(l, d)
        if li is None:
            continue
        b = cell.bbox_per_layer(li)                   # of the cells under it too
        if not b.empty():
            out[name] = b
    return out


def gap(a: tuple[kdb.Box, dict], b: tuple[kdb.Box, dict], rules, axis: int, dbu: float, merge: bool = False,
        floor_um: float | None = None, skip: tuple[str, ...] = ()) -> int:
    """The space (dbu) between the bounding boxes of cell a and cell b, b after a along `axis` (0: x, 1: y), that keeps
    every rule between a layer of one and a layer of the other; `merge`: rules that let two units share a layer (a well
    of one bulk net) are met when the layer overlaps too; `skip`: layers drawn over both as one region (a fill), whose
    rules do not apply between them; `floor_um`: at least this. a and b are (cell bbox, extents). Negative: the boxes
    may overlap (the implants of units, which the rules do not keep apart)."""
    (fa, ea), (fb, eb) = a, b
    lo = (lambda box: box.left) if axis == 0 else (lambda box: box.bottom)
    hi = (lambda box: box.right) if axis == 0 else (lambda box: box.top)
    low = -(1 << 40)
    need = int(round(floor_um / dbu)) if floor_um is not None else low
    merged: list[tuple[int, int]] = []          # (overlap up to, space from) of the rules units may also meet by overlapping
    for r in rules:
        if r.a in skip or r.b in skip:
            continue
        for (e1, f1, la, e2, f2, lb) in ((ea, fa, r.a, eb, fb, r.b), (ea, fa, r.b, eb, fb, r.a)):
            if la not in e1 or lb not in e2:
                continue
            if r.unless and r.unless in (e1 if la == r.a else e2):     # the cell with layer a has `unless` too
                continue
            # b's layer starts at frame gap + (its offset into b); a's ends (its distance to a's far edge) before the gap
            inner = (hi(f1) - hi(e1[la])) + (lo(e2[lb]) - lo(f2))
            want = int(round(r.um / dbu)) - inner
            if merge and r.merge:
                merged.append((-inner, want))
            else:
                need = max(need, want)
    for _ in merged:                            # at a space up to `upto` the shared layer overlaps (or touches): fine too
        need = max([need] + [want for upto, want in merged if need > upto])
    return need if need > low else 0


def factor(n: int) -> tuple[int, int]:
    """(columns, rows) of a grid of exactly n units, as square as n allows (columns >= rows): 512 -> 32 x 16, 7 -> 7 x 1."""
    best = (n, 1)
    for r in range(1, int(math.isqrt(n)) + 1):
        if n % r == 0:
            best = (n // r, r)
    return best


def grid_for(n: int) -> tuple[int, int]:
    """(columns, rows) of the squarest grid with room for n units, an even count of free cells (common centroid pairs
    cells about the centre)."""
    cols = max(1, math.ceil(math.sqrt(n)))
    rows = max(1, math.ceil(n / cols))
    while (cols * rows - n) % 2 and cols * rows > 1:
        cols += 1
        rows = max(1, math.ceil(n / cols))
    return cols, rows


# ---------------------------------------------------------------------------------- arrangements


def _deal(counts: dict[str, int], slots: int) -> list[str]:
    """`slots` turns dealt to the devices in proportion to their counts, each turn to the device furthest behind its
    share (so every device is spread over the whole sequence, not bunched)."""
    total = sum(counts.values())
    got = {k: 0 for k in counts}
    first = {k: i for i, k in enumerate(counts)}     # a tie goes to the device named first
    out = []
    for i in range(slots):
        k = max(counts, key=lambda k: (counts[k] * (i + 1) / total - got[k], counts[k], -first[k]))
        got[k] += 1
        out.append(k)
    return out


def common_centroid(counts: dict[str, int], cols: int, rows: int) -> list[list[str | None]]:
    """A grid (rows bottom-up, columns left to right) in which each device's units sit in pairs point-symmetric about
    the centre, so every device has the centroid of the array. The pairs nearest the centre come first and are dealt
    to the devices by their shares (_deal), so each device spreads from the middle to the edge. A device with an odd
    count has one unit left over: such units take the centre cell of an odd grid, then share the innermost pairs two
    by two; a last one takes half a pair, the only unit off the centroid. Free cells (None) are left at the edge."""
    grid: list[list[str | None]] = [[None] * cols for _ in range(rows)]
    cx, cy = (cols - 1) / 2, (rows - 1) / 2
    cells = sorted(((c, r) for r in range(rows) for c in range(cols)),
                   key=lambda p: ((p[0] - cx) ** 2 + (p[1] - cy) ** 2, math.atan2(p[1] - cy, p[0] - cx)))
    pairs, seen, centre = [], set(), None
    for c, r in cells:
        if (c, r) in seen:
            continue
        m = (cols - 1 - c, rows - 1 - r)
        seen |= {(c, r), m}
        if m == (c, r):
            centre = (c, r)
        else:
            pairs.append(((c, r), m))
    singles = sorted((k for k, n in counts.items() if n % 2), key=lambda k: (counts[k], k))
    if centre is not None and singles:
        grid[centre[1]][centre[0]] = singles.pop(0)
    halves = {k: n // 2 for k, n in counts.items() if n // 2}
    if len(pairs) < (len(singles) + 1) // 2 + sum(halves.values()):
        raise ValueError(f"{sum(counts.values())} units do not fit in {cols} x {rows} as symmetric pairs")
    queue = iter(pairs)
    while singles:                                # two odd units on one pair, or the last one on half a pair
        a, b = next(queue)
        grid[a[1]][a[0]] = singles.pop(0)
        if singles:
            grid[b[1]][b[0]] = singles.pop(0)
    for (a, b), k in zip(queue, _deal(halves, sum(halves.values()))):
        grid[a[1]][a[0]] = grid[b[1]][b[0]] = k
    return grid


def interdigitated(counts: dict[str, int], cols: int, rows: int) -> list[list[str | None]]:
    """The units dealt in turn by their shares (ABAB..., ABBABB... for 1:2), row by row from the bottom, each row the
    reverse of the one below (so the columns alternate too)."""
    order = _deal(counts, sum(counts.values()))
    grid: list[list[str | None]] = []
    for r in range(rows):
        row = order[r * cols:(r + 1) * cols]
        row += [None] * (cols - len(row))
        grid.append(row if r % 2 == 0 else row[::-1])
    return grid


def centroid_error(grid: list[list[str | None]], pitch: tuple[float, float]) -> dict[str, list[float]]:
    """Per device, its units' centroid minus the array's centre (um)."""
    rows, cols = len(grid), len(grid[0]) if grid else 0
    cx, cy = (cols - 1) / 2, (rows - 1) / 2
    acc: dict[str, list[float]] = {}
    for r, row in enumerate(grid):
        for c, k in enumerate(row):
            if k:
                a = acc.setdefault(k, [0.0, 0.0, 0])
                a[0] += c - cx
                a[1] += r - cy
                a[2] += 1
    return {k: [round(x / n * pitch[0], 3), round(y / n * pitch[1], 3)] for k, (x, y, n) in acc.items()}
