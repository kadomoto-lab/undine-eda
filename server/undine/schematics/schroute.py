"""Pure orthogonal routing for schematic wires.

Symbol graphics are keepouts; annotation text is deliberately not. Existing
wires may be crossed in their interiors, but never used as an accidental
junction. No schematic state is changed while looking for a route.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import heapq
import math
import time
from pathlib import Path

from undine.schematics import schematic, xschem
from undine.schematics.xschem import XFile, SymbolResolver

Point = tuple[float, float]
CLEARANCE = 10.0
GRID = 10.0              # body keepouts end on this grid, so routes between pins on the grid stay on it
_EPS = 1e-7
_MAX_STATES = 40000
# a preview, or an edit (with a larger budget), gives up after its time budget instead of exhausting the search. The
# budget is the routing thread's own CPU time: a machine busy with simulations must not turn the same edit's wires
# into labels (a drawing that depends on the load)
_deadline: ContextVar[float | None] = ContextVar("route_deadline", default=None)


class RouteTimeout(ValueError):
    """No route was found within the time budget (there may still be one)."""


@contextmanager
def time_budget(seconds: float):
    """Routes searched inside this block, together, stop after `seconds` of this thread's CPU time with RouteTimeout."""
    token = _deadline.set(time.thread_time() + seconds)
    try:
        yield
    finally:
        _deadline.reset(token)


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.thread_time() >= deadline:
        raise RouteTimeout("No safe wire route found in time; choose another position")


def _point(p) -> Point:
    if len(p) != 2 or not all(math.isfinite(v) for v in p):
        raise ValueError("Wire coordinates must be finite")
    return float(p[0]), float(p[1])


def _on(p: Point, a: Point, b: Point) -> bool:
    return schematic._on_segment(*p, *a, *b)


# wider than schematic._on_segment's tolerance: the indexes below may only skip what `_on` would reject
_NEAR = 1e-5


def _near(keys: list[float], items: list, v: float) -> list:
    return items[bisect_left(keys, v - _NEAR):bisect_right(keys, v + _NEAR)]


class _PointIndex:
    """Points sorted by x and by y, so an orthogonal segment is tested only against the points on its line."""

    def __init__(self, points):
        self.all = list(points)
        self.by_x = sorted(self.all)
        self.xs = [p[0] for p in self.by_x]
        self.by_y = sorted(self.all, key=lambda p: (p[1], p[0]))
        self.ys = [p[1] for p in self.by_y]

    def any_on(self, a: Point, b: Point) -> bool:
        if a[1] == b[1]:
            candidates = _near(self.ys, self.by_y, a[1])
        elif a[0] == b[0]:
            candidates = _near(self.xs, self.by_x, a[0])
        else:
            candidates = self.all
        return any(_on(p, a, b) for p in candidates)


class _WireIndex:
    """Wires grouped by their line, for collinear overlaps and for points lying on a wire."""

    def __init__(self, wires: list[tuple[Point, Point]]):
        self.horizontal: dict[float, list[tuple[Point, Point]]] = {}
        self.vertical: dict[float, list[tuple[Point, Point]]] = {}
        self.other: list[tuple[Point, Point]] = []
        for w in wires:
            (c, d) = w
            if c[1] == d[1]:
                self.horizontal.setdefault(c[1], []).append(w)
            if c[0] == d[0]:
                self.vertical.setdefault(c[0], []).append(w)
            if c[1] != d[1] and c[0] != d[0]:
                self.other.append(w)
        self.hy = sorted(self.horizontal)
        self.vx = sorted(self.vertical)

    def through(self, p: Point) -> bool:
        """Whether p lies on a wire (its interior or an end)."""
        lines = [w for y in _near(self.hy, self.hy, p[1]) for w in self.horizontal[y]]
        lines += [w for x in _near(self.vx, self.vx, p[0]) for w in self.vertical[x]]
        return any(_on(p, a, b) for a, b in lines + self.other)

    def touches(self, a: Point, b: Point) -> bool:
        """Whether a wire meets the orthogonal segment a-b (along it, across it, or with an end on it)."""
        k = 1 if a[1] == b[1] else 0                  # the axis the segment runs along: x (1) or y (0)
        lo, hi = sorted((a[1 - k], b[1 - k]))
        at = a[k]
        along, across = (self.horizontal, self.vertical) if k else (self.vertical, self.horizontal)
        along_keys, across_keys = (self.hy, self.vx) if k else (self.vx, self.hy)
        for key in _near(along_keys, along_keys, at):
            for c, d in along[key]:
                if max(min(c[1 - k], d[1 - k]), lo) <= min(max(c[1 - k], d[1 - k]), hi) + _NEAR:
                    return True
        for key in across_keys[bisect_left(across_keys, lo - _NEAR):bisect_right(across_keys, hi + _NEAR)]:
            for c, d in across[key]:
                if min(c[k], d[k]) - _NEAR <= at <= max(c[k], d[k]) + _NEAR:
                    return True
        return any(_on(p, a, b) for w in self.other for p in w)


def _axis(a: Point, b: Point) -> int:
    if a[1] == b[1]:
        return 1
    if a[0] == b[0]:
        return 2
    raise ValueError("Wire route must be orthogonal")


@dataclass(frozen=True)
class Obstacle:
    xmin: float
    ymin: float
    xmax: float
    ymax: float
    terminals: tuple[Point, ...] = ()
    terminal_directions: tuple[tuple[Point, Point], ...] = ()

    def contains(self, p: Point) -> bool:
        return self.xmin + _EPS < p[0] < self.xmax - _EPS and self.ymin + _EPS < p[1] < self.ymax - _EPS

    def hits(self, a: Point, b: Point) -> bool:
        if a[1] == b[1]:
            return self.ymin + _EPS < a[1] < self.ymax - _EPS and max(a[0], b[0]) > self.xmin + _EPS and min(a[0], b[0]) < self.xmax - _EPS
        return self.xmin + _EPS < a[0] < self.xmax - _EPS and max(a[1], b[1]) > self.ymin + _EPS and min(a[1], b[1]) < self.ymax - _EPS

    def escapes(self, p: Point) -> list[Point]:
        """The nearest body sides determine the outward terminal direction."""
        if p not in self.terminals:
            return []
        directions = [d for pin, d in self.terminal_directions if pin == p]
        if directions:
            return [(self.xmin if dx < 0 else self.xmax, p[1]) if dx else
                    (p[0], self.ymin if dy < 0 else self.ymax) for dx, dy in directions]
        sides = [(p[0] - self.xmin, (self.xmin, p[1])),
                 (self.xmax - p[0], (self.xmax, p[1])),
                 (p[1] - self.ymin, (p[0], self.ymin)),
                 (self.ymax - p[1], (p[0], self.ymax))]
        distance = min(d for d, _ in sides)
        return [q for d, q in sides if d <= distance + _EPS]


def _shape(sym: XFile) -> tuple[list[Point], bool, tuple | None, list[tuple[Point, Point]]]:
    """A symbol in its own coordinates: its pins, whether it is a body (not a label, port or code block), the bounds of
    its graphics, and the outward direction of each pin a straight lead line ends at."""
    pins = [_point((p.x, p.y)) for p in schematic.sym_pins(sym)]
    if sym.global_props.get("type", "") in schematic.SKIP_TYPES | schematic.CODE_TYPES:
        return pins, False, None, []
    points = []
    directions = []
    for layer, x1, y1, x2, y2, _ in sym.lines:
        if layer in (xschem.PIN_LAYER, xschem.TEXT_LAYER):
            continue
        a, b = _point((x1, y1)), _point((x2, y2))
        if a == b or (a[0] != b[0] and a[1] != b[1]):
            continue
        for pin, inside in ((a, b), (b, a)):
            if pin in pins:
                directions.append((pin, (math.copysign(1, pin[0] - inside[0]) if pin[0] != inside[0] else 0,
                                         math.copysign(1, pin[1] - inside[1]) if pin[1] != inside[1] else 0)))
    for layer, x1, y1, x2, y2, props in sym.lines + sym.boxes:
        if layer not in (xschem.PIN_LAYER, xschem.TEXT_LAYER) and not props.get("flags", "").startswith("graph"):
            points.extend((_point((x1, y1)), _point((x2, y2))))
    for layer, pts, _ in sym.polys:
        if layer not in (xschem.PIN_LAYER, xschem.TEXT_LAYER):
            points.extend(_point((pts[i], pts[i + 1])) for i in range(0, len(pts) - 1, 2))
    for layer, cx, cy, r, a, b, _ in sym.arcs:
        if layer not in (xschem.PIN_LAYER, xschem.TEXT_LAYER):
            points.extend(_point(p) for p in _arc_extent(cx, cy, r, a, b))
    if not points:
        return pins, True, None, directions
    xs, ys = zip(*points)
    return pins, True, (min(xs), min(ys), max(xs), max(ys)), directions


def _shape_of(resolver: SymbolResolver, comp, base: Path, sym: XFile):
    """_shape, remembered on the resolver for the symbols it loaded (one schematic routes many wires)."""
    if comp.embedded is not None:
        return _shape(sym)
    shapes = resolver.__dict__.setdefault("_route_shapes", {})
    hit = shapes.get(id(sym))
    if hit is None or hit[0] is not sym:          # the symbol itself is kept, so its id is not reused meanwhile
        hit = shapes[id(sym)] = (sym, _shape(sym))
    return hit[1]


def _geometry(f: XFile, resolver: SymbolResolver, base: Path) -> tuple[list[Obstacle], list[Point]]:
    obstacles: list[Obstacle] = []
    terminals: list[Point] = []
    for comp in f.comps:
        _point((comp.x, comp.y))
        sym = schematic.resolve_symbol(resolver, comp, base)
        if sym is None:
            obstacles.append(Obstacle(comp.x - 20, comp.y - 20, comp.x + 20, comp.y + 20))
            continue

        def transform(x, y):
            rx, ry = xschem._rot(comp.rot, comp.flip, x, y)
            return _point((rx + comp.x, ry + comp.y))

        local_pins, body, bounds, local_dirs = _shape_of(resolver, comp, base, sym)
        pins = tuple(transform(*p) for p in local_pins)
        terminals.extend(pins)
        if not body or bounds is None:
            continue
        directions = tuple((transform(*pin), xschem._rot(comp.rot, comp.flip, *d)) for pin, d in local_dirs)
        corners = [transform(x, y) for x in (bounds[0], bounds[2]) for y in (bounds[1], bounds[3])]
        xs, ys = zip(*corners)
        raw = Obstacle(min(xs) - CLEARANCE, min(ys) - CLEARANCE, max(xs) + CLEARANCE, max(ys) + CLEARANCE)
        box = [_snap(raw.xmin, -1), _snap(raw.ymin, -1), _snap(raw.xmax, 1), _snap(raw.ymax, 1)]
        for p in pins:                      # a pin left outside the keepout (no lead) is not taken into it by the snap
            if raw.contains(p) or not Obstacle(*box).contains(p):
                continue
            box = [raw.xmin if p[0] <= raw.xmin + _EPS else box[0], raw.ymin if p[1] <= raw.ymin + _EPS else box[1],
                   raw.xmax if p[0] >= raw.xmax - _EPS else box[2], raw.ymax if p[1] >= raw.ymax - _EPS else box[3]]
        obstacles.append(Obstacle(*box, pins, directions))
    return obstacles, list(dict.fromkeys(terminals))


def _lead_ends(ob: Obstacle, t: Point) -> list[Point]:
    """Where a wire would look attached to terminal t: one grid step out along its lead, and where it leaves the body."""
    out = []
    for q in ob.escapes(t):
        dx, dy = (math.copysign(1, q[0] - t[0]) if q[0] != t[0] else 0), (math.copysign(1, q[1] - t[1]) if q[1] != t[1] else 0)
        out += [(t[0] + dx * GRID, t[1] + dy * GRID), q]
    return out


def _arc_extent(cx: float, cy: float, r: float, a: float, b: float) -> list[Point]:
    """The points that bound an arc as drawn (Xschem: from angle a, b degrees counter-clockwise, y down): its ends and
    the quadrant points it passes. The whole circle would make the large flat arcs of a standard cell's OR / XOR shape
    a keepout far beyond the gate."""
    r = abs(r)
    if not all(math.isfinite(v) for v in (a, b)) or abs(b) >= 360:
        angles = [0.0, 90.0, 180.0, 270.0]
    else:
        lo, hi = (a, a + b) if b >= 0 else (a + b, a)
        angles = [lo, hi] + [q for q in range(int(math.ceil(lo / 90)) * 90, int(math.floor(hi / 90)) * 90 + 1, 90)]
    return [(cx + r * math.cos(math.radians(t)), cy - r * math.sin(math.radians(t))) for t in angles]


def _snap(v: float, outward: int) -> float:
    """v moved outward (-1 down, 1 up) to the grid: a body drawn off the grid (arcs of standard cells) would otherwise
    put detours, and so wires, between grid points."""
    k = v / GRID
    if abs(k - round(k)) < 1e-6:
        return round(k) * GRID
    return (math.floor(k) if outward < 0 else math.ceil(k)) * GRID


def body_obstacles(f: XFile, resolver: SymbolResolver, base: Path) -> list[Obstacle]:
    """Return body bounds including clearance, excluding labels and text."""
    return _geometry(f, resolver, base)[0]


def terminal_points(f: XFile, resolver: SymbolResolver, base: Path) -> list[Point]:
    """All transformed symbol pins, including label and port terminals."""
    return _geometry(f, resolver, base)[1]


class _Bodies:
    """Obstacles by the cells of a coarse grid they cover, so a segment is tested only against the bodies near it."""
    CELL = 200.0

    def __init__(self, obstacles: list[Obstacle]):
        self.all = obstacles
        self.cells: dict[tuple[int, int], list[Obstacle]] = {}
        self.big: list[Obstacle] = []               # too large to file by cell: always tested
        for ob in obstacles:
            keys = self._keys(ob.xmin, ob.ymin, ob.xmax, ob.ymax)
            if keys is None:
                self.big.append(ob)
            for key in keys or ():
                self.cells.setdefault(key, []).append(ob)

    def _keys(self, x1: float, y1: float, x2: float, y2: float):
        c = self.CELL
        xs, ys = range(math.floor(x1 / c), math.floor(x2 / c) + 1), range(math.floor(y1 / c), math.floor(y2 / c) + 1)
        if len(xs) * len(ys) > max(64, len(self.all)):
            return None
        return [(i, j) for i in xs for j in ys]

    def near(self, a: Point, b: Point) -> list[Obstacle]:
        keys = self._keys(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
        if keys is None:
            return self.all
        return list({id(ob): ob for ob in [*self.big, *(ob for k in keys for ob in self.cells.get(k, ()))]}.values())


def _body_blocked(obstacles: list[Obstacle], a: Point, b: Point) -> bool:
    for ob in obstacles:
        if not ob.hits(a, b):
            continue
        # A terminal can leave its own body only through its nearest side.
        portals = (ob.escapes(a) if ob.contains(a) else []) + (ob.escapes(b) if ob.contains(b) else [])
        if any(_on(portal, a, b) for portal in portals):
            continue
        return True
    return False


def segment_blocked(f: XFile, resolver: SymbolResolver, base: Path, start: Point, end: Point,
                    *, obstacles: list[Obstacle] | None = None) -> bool:
    """Whether an orthogonal segment intersects a device body/clearance.

    Terminal escape segments are allowed. This helper ignores existing wires
    and is useful for identifying wires that a moved device obstructs.
    """
    a, b = _point(start), _point(end)
    _axis(a, b)
    return _body_blocked(body_obstacles(f, resolver, base) if obstacles is None else obstacles, a, b)


def _simplify(points: list[Point]) -> list[Point]:
    out: list[Point] = []
    for p in points:
        if out and out[-1] == p:
            continue
        if len(out) > 1 and _axis(out[-2], out[-1]) == _axis(out[-1], p):
            out[-1] = p
        else:
            out.append(p)
    return out


def route(f: XFile, resolver: SymbolResolver, base: Path, start: Point, end: Point,
          *, allowed_wire_indices: set[int] | None = None, avoid_backtrack: bool = False,
          keep_clear: bool = False) -> list[Point]:
    """Return a deterministic safe route, or raise ``ValueError``.

    Existing wire interiors can be crossed without a bend. A wire endpoint or
    another terminal cannot be crossed. Only ``start`` and ``end`` may make a
    new junction. Callers rebuilding an already connected net may explicitly
    exempt its wire indices; unrelated nets must never be exempted.
    Interactive extensions can forbid reversing along a dangling wire at
    ``start``, so a clicked waypoint remains a bend instead of a dead stub.
    With ``keep_clear`` (a drawing made for someone to read) a bend keeps more
    than a grid step from other terminals and wires, and the route never passes
    the point just outside another terminal: either would read as a connection.
    """
    start, end = _point(start), _point(end)
    if start == end:
        return [start]
    deadline = _deadline.get()
    if not math.isfinite(abs(start[0] - end[0]) + abs(start[1] - end[1])):
        raise ValueError("Wire coordinates are too far apart")
    obstacles, pins = _geometry(f, resolver, base)
    terminals = set(pins) - {start, end}
    allowed = allowed_wire_indices or set()
    wires = [(_point(w[:2]), _point(w[2:4])) for i, w in enumerate(f.wires) if i not in allowed]
    protected = terminals | {p for w in wires for p in w if p not in (start, end)}
    near_points = set(protected)
    if keep_clear:            # the lead of another terminal, continued one step out of its body
        for ob in obstacles:
            for t in ob.terminals:
                if t in terminals:
                    protected.update(q for q in _lead_ends(ob, t) if q not in (start, end))
    protected_index, wire_index = _PointIndex(protected), _WireIndex(wires)
    near_index, bodies = _PointIndex(near_points), _Bodies(obstacles)
    incoming = None
    if avoid_backtrack and start not in pins:
        attached = [w for w in f.wires if _on(start, w[:2], w[2:4])]
        rays = {(math.copysign(1, p[0] - start[0]) if p[0] != start[0] else 0,
                 math.copysign(1, p[1] - start[1]) if p[1] != start[1] else 0)
                for w in attached for p in (w[:2], w[2:4]) if p != start}
        # Re-selecting an already covered segment stays a no-op. A terminal
        # or junction can also deliberately reuse its existing conductors.
        if len(rays) == 1 and not any(_on(end, w[:2], w[2:4]) for w in attached):
            incoming = next(iter(rays))

    def wire_clear(a: Point, b: Point) -> bool:
        if incoming and start in (a, b):
            q = b if a == start else a
            dx, dy = q[0] - start[0], q[1] - start[1]
            if dx * incoming[1] == dy * incoming[0] and dx * incoming[0] + dy * incoming[1] > 0:
                return False
        if protected_index.any_on(a, b):
            return False
        # a wire along the same line must not overlap the segment
        if a[1] == b[1]:
            for c, d in wire_index.horizontal.get(a[1], ()):
                if min(max(a[0], b[0]), max(c[0], d[0])) > max(min(a[0], b[0]), min(c[0], d[0])) + _EPS:
                    return False
        if a[0] == b[0]:
            for c, d in wire_index.vertical.get(a[0], ()):
                if min(max(a[1], b[1]), max(c[1], d[1])) > max(min(a[1], b[1]), min(c[1], d[1])) + _EPS:
                    return False
        return True

    def crowded(p: Point) -> bool:
        """Another terminal, wire end or wire within a grid step of p, along x or y."""
        probes = (((p[0] - GRID, p[1]), (p[0] + GRID, p[1])), ((p[0], p[1] - GRID), (p[0], p[1] + GRID)))
        return any(near_index.any_on(a, b) or wire_index.touches(a, b) for a, b in probes)

    def bend_clear(p: Point) -> bool:
        return p in (start, end) or not (wire_index.through(p) or keep_clear and crowded(p))

    def valid(points: list[Point]) -> bool:
        return (all(bend_clear(p) for p in points[1:-1]) and
                all(not _body_blocked(bodies.near(a, b), a, b) and wire_clear(a, b)
                    for a, b in zip(points, points[1:])))

    def cost(points: list[Point]) -> float:
        return sum(abs(a[0] - b[0]) + abs(a[1] - b[1]) for a, b in zip(points, points[1:])) + 20 * max(0, len(points) - 2)

    if keep_clear and any(p not in pins and crowded(p) for p in (start, end)):
        raise ValueError("No readable wire route: a junction there would sit next to another net")

    # Most mouse previews are unobstructed; avoid building a search graph.
    simple = [_simplify([start, (end[0], start[1]), end]),
              _simplify([start, (start[0], end[1]), end])]
    candidates = [p for p in simple if valid(p)]
    if candidates:
        return min(candidates, key=cost)

    def portals(p: Point) -> list[Point]:
        containing = [ob for ob in bodies.near(p, p) if ob.contains(p)]
        if not containing:
            return [p]
        options = {q for ob in containing for q in ob.escapes(p)}
        return sorted(q for q in options if not any(ob.contains(q) for ob in bodies.near(q, q))
                      and not _body_blocked(bodies.near(p, q), p, q) and wire_clear(p, q))

    sources, targets = portals(start), portals(end)
    if not sources or not targets:
        raise ValueError("No safe wire route: a terminal is blocked by a device")
    xs = {start[0], end[0], *(p[0] for p in sources + targets)}
    ys = {start[1], end[1], *(p[1] for p in sources + targets)}
    for ob in obstacles:
        xs.update((ob.xmin, ob.xmax))
        ys.update((ob.ymin, ob.ymax))
    steps = (CLEARANCE, CLEARANCE + GRID) if keep_clear else (CLEARANCE,)     # a bend just beyond the keepout too
    for p in protected | {start, end}:
        for s in steps:
            xs.update((p[0] - s, p[0] + s))
            ys.update((p[1] - s, p[1] + s))
    xs, ys = sorted(xs), sorted(ys)
    # Try the cheapest two-bend detours before searching. This covers the
    # common case of several devices between two pins without visiting every
    # intersection of their visibility lines on each mouse movement.
    detours = [_simplify([start, (x, start[1]), (x, end[1]), end]) for x in xs]
    detours += [_simplify([start, (start[0], y), (end[0], y), end]) for y in ys]
    for n, candidate in enumerate(sorted(detours, key=cost)):
        if n % 32 == 0:
            _check_deadline(deadline)
        if valid(candidate):
            return candidate
    xi, yi = {v: i for i, v in enumerate(xs)}, {v: i for i, v in enumerate(ys)}
    target_set = set(targets)
    # Lazy grid edges avoid the potentially quadratic allocation of vertices.
    edge_cache: dict[tuple[Point, Point], bool] = {}
    bend_cache: dict[Point, bool] = {}

    def edge_clear(a: Point, b: Point) -> bool:
        key = (a, b) if a < b else (b, a)
        if key not in edge_cache:
            edge_cache[key] = not any(ob.hits(a, b) for ob in bodies.near(a, b)) and wire_clear(a, b)
        return edge_cache[key]

    def heuristic(p: Point) -> float:
        return abs(p[0] - end[0]) + abs(p[1] - end[1])

    queue = []
    best = {}
    previous = {}
    for p in sources:
        axis = _axis(start, p) if p != start else 0
        state = (p, axis)
        distance = abs(p[0] - start[0]) + abs(p[1] - start[1])
        best[state] = distance
        previous[state] = None
        heapq.heappush(queue, (distance + heuristic(p), distance, state))
    expanded = 0
    while queue and expanded < _MAX_STATES:
        _, distance, state = heapq.heappop(queue)
        if best.get(state) != distance:
            continue
        expanded += 1
        if expanded % 128 == 0:
            _check_deadline(deadline)
        p, axis = state
        if p in target_set:
            tail_axis = _axis(p, end) if p != end else axis
            if (axis in (0, tail_axis) or bend_clear(p)):
                path = []
                current = state
                while current is not None:
                    path.append(current[0])
                    current = previous[current]
                result = _simplify([start, *reversed(path), end])
                if valid(result):
                    return result
        ix, iy = xi[p[0]], yi[p[1]]
        neighbors = []
        if ix:
            neighbors.append(((xs[ix - 1], p[1]), 1))
        if ix + 1 < len(xs):
            neighbors.append(((xs[ix + 1], p[1]), 1))
        if iy:
            neighbors.append(((p[0], ys[iy - 1]), 2))
        if iy + 1 < len(ys):
            neighbors.append(((p[0], ys[iy + 1]), 2))
        if p not in bend_cache:
            bend_cache[p] = bend_clear(p)
        for q, next_axis in neighbors:
            turn = axis not in (0, next_axis)
            if (turn and not bend_cache[p]) or not edge_clear(p, q):
                continue
            score = distance + abs(p[0] - q[0]) + abs(p[1] - q[1]) + (20 if turn else 0)
            next_state = (q, next_axis)
            if score < best.get(next_state, math.inf):
                best[next_state] = score
                previous[next_state] = state
                heapq.heappush(queue, (score + heuristic(q), score, next_state))
    raise ValueError("No safe wire route found; choose another waypoint or move the device")
