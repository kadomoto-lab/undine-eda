import type { Schematic } from "../api";

type Point = [number, number];
type Segment = readonly [number, number, number, number];
type Drawing = Pick<Schematic, "wires" | "pins" | "terminals">;
const EPS = 1e-7;
export const pointKey = (x: number, y: number) => `${Math.round(x / EPS)},${Math.round(y / EPS)}`;

/** Connectivity markers must follow endpoint-on-segment semantics, as the netlist does.
 * Compute once per server snapshot, not on every pointer/camera repaint. */
const cache = new WeakMap<Drawing, { junctions: Point[]; connectedPins: Set<string> }>();
export function drawingConnectivity(data: Drawing) {
  const cached = cache.get(data);
  if (cached) return cached;
  const horizontal = new Map<number, Segment[]>(), vertical = new Map<number, Segment[]>(), diagonal: Segment[] = [];
  const points = new Map<string, Point>();
  const terminals = data.terminals ?? data.pins ?? [];
  const pinCounts = new Map<string, number>();
  for (const [, , x, y] of terminals) {
    const key = pointKey(x, y);
    pinCounts.set(key, (pinCounts.get(key) ?? 0) + 1);
    points.set(key, [x, y]);
  }
  for (const [x1, y1, x2, y2] of data.wires) {
    if (Math.hypot(x2 - x1, y2 - y1) < EPS) continue;
    const segment: Segment = [x1, y1, x2, y2];
    points.set(pointKey(x1, y1), [x1, y1]); points.set(pointKey(x2, y2), [x2, y2]);
    const map = Math.abs(y1 - y2) < EPS ? horizontal : Math.abs(x1 - x2) < EPS ? vertical : null;
    if (map) {
      const key = Math.round((map === horizontal ? y1 : x1) / EPS);
      const group = map.get(key) ?? [];
      group.push(segment); map.set(key, group);
    } else diagonal.push(segment);
  }
  const junctions: Point[] = [], connectedPins = new Set<string>();
  for (const [key, [x, y]] of points) {
    const rays = new Set<string>();
    const candidates = [...horizontal.get(Math.round(y / EPS)) ?? [], ...vertical.get(Math.round(x / EPS)) ?? [], ...diagonal];
    for (const [x1, y1, x2, y2] of candidates) {
      const dx = x2 - x1, dy = y2 - y1, length = Math.hypot(dx, dy);
      if (x < Math.min(x1, x2) - EPS || x > Math.max(x1, x2) + EPS || y < Math.min(y1, y2) - EPS || y > Math.max(y1, y2) + EPS
          || Math.abs((x - x1) * dy - (y - y1) * dx) > EPS * length) continue;
      for (const [ex, ey] of [[x1, y1], [x2, y2]]) {
        const distance = Math.hypot(ex - x, ey - y);
        if (distance > EPS) rays.add(pointKey((ex - x) / distance, (ey - y) / distance));
      }
    }
    if (rays.size >= 3) junctions.push([x, y]);
    if (pinCounts.has(key) && (rays.size > 0 || pinCounts.get(key)! > 1)) connectedPins.add(key);
  }
  const result = { junctions, connectedPins };
  cache.set(data, result);
  return result;
}
