import type { Schematic } from "../api";

export type Point = [number, number];
export type WireTarget = { point: Point; connected: boolean };

/** Prefer actual terminals over the drawing grid, including terminals off-grid. */
export function wireTarget(data: Schematic | null, x: number, y: number, tolerance: number, grid = 10): WireTarget {
  const snap = (v: number) => Math.round(v / grid) * grid;
  if (!data) return { point: [snap(x), snap(y)], connected: false };
  let best: Point | null = null, distance = tolerance;
  for (const pin of data.terminals ?? data.pins ?? []) {
    const d = Math.hypot(x - pin[2], y - pin[3]);
    if (d <= distance) { distance = d; best = [pin[2], pin[3]]; }
  }
  if (best) return { point: best, connected: true };
  for (const wire of data.wires) {
    const [x1, y1, x2, y2] = wire;
    const dx = x2 - x1, dy = y2 - y1, length2 = dx * dx + dy * dy;
    const u = length2 ? Math.max(0, Math.min(1, ((x - x1) * dx + (y - y1) * dy) / length2)) : 0;
    let px = x1 + u * dx, py = y1 + u * dy;
    const d = Math.hypot(x - px, y - py);
    if (d > distance) continue;
    // Preserve the existing conductor's exact coordinate. Along an orthogonal
    // wire, use the grid when possible and preserve either off-grid endpoint.
    if (dx === 0) py = Math.max(Math.min(y1, y2), Math.min(Math.max(y1, y2), snap(py)));
    if (dy === 0) px = Math.max(Math.min(x1, x2), Math.min(Math.max(x1, x2), snap(px)));
    if (Math.hypot(x - x1, y - y1) <= tolerance) { px = x1; py = y1; }
    else if (Math.hypot(x - x2, y - y2) <= tolerance) { px = x2; py = y2; }
    distance = d;
    best = [px, py];
  }
  return best ? { point: best, connected: true } : { point: [snap(x), snap(y)], connected: false };
}

/** One preview request at a time. New pointer positions replace queued work. */
export class WirePreview {
  private version = 0;
  private key = "";
  private busy = false;
  private disposed = false;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private pending: { from: Point; to: Point; version: number } | null = null;
  private from: Point | null = null;

  constructor(
    private readonly route: (from: Point, to: Point) => Promise<Point[]>,
    private readonly show: (points: Point[] | null) => void,
    private readonly delay = 35,
  ) {}

  request(from: Point, to: Point): void {
    const key = `${from.join(",")}:${to.join(",")}`;
    if (this.disposed || key === this.key) return;
    // A new start point invalidates the shown route. A new end point only replaces it when its route
    // arrives, so the dashed preview stays on screen instead of blinking while the pointer moves.
    if (!this.from || this.from[0] !== from[0] || this.from[1] !== from[1]) this.clear();
    this.from = [...from];
    this.key = key;
    this.pending = { from: [...from], to: [...to], version: this.version };
    // Keep the existing deadline so continuous pointer events cannot postpone all feedback.
    if (this.timer === null) this.timer = setTimeout(() => { this.timer = null; void this.run(); }, this.delay);
  }

  clear(): void {
    this.version++;
    this.key = "";
    this.from = null;
    this.pending = null;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.show(null);
  }

  dispose(): void { this.clear(); this.disposed = true; }

  private async run(): Promise<void> {
    if (this.busy || this.disposed || !this.pending) return;
    const pending = this.pending;
    this.pending = null;
    this.busy = true;
    try {
      const points = await this.route(pending.from, pending.to);
      // One request is in flight, so routes arrive in pointer order; a route the pointer has already left
      // is still shown as progress until the current one arrives.
      if (!this.disposed && pending.version === this.version) this.show(points);
    } catch {
      // A blocked route has no ghost wire; the commit reports the server error.
      if (!this.disposed && pending.version === this.version) this.show(null);
    } finally {
      this.busy = false;
      if (this.timer === null) void this.run();
    }
  }
}
