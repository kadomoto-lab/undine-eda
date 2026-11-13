// 2D camera shared by the editors. World units: dbu (layout, y up) or
// schematic units (y down). s = world units per CSS pixel.

export class Camera {
  cx = 0;
  cy = 0;
  s = 1;
  w = 1;
  h = 1;
  constructor(public yUp: boolean) {}

  toScreen(x: number, y: number): [number, number] {
    const sx = (x - this.cx) / this.s + this.w / 2;
    const sy = this.yUp ? this.h / 2 - (y - this.cy) / this.s : (y - this.cy) / this.s + this.h / 2;
    return [sx, sy];
  }

  toWorld(sx: number, sy: number): [number, number] {
    const x = (sx - this.w / 2) * this.s + this.cx;
    const y = this.yUp ? (this.h / 2 - sy) * this.s + this.cy : (sy - this.h / 2) * this.s + this.cy;
    return [x, y];
  }

  zoomAt(sx: number, sy: number, f: number, minS = 1e-4, maxS = 1e9): void {
    const [wx, wy] = this.toWorld(sx, sy);
    this.s = Math.min(maxS, Math.max(minS, this.s * f));
    const [nx, ny] = this.toWorld(sx, sy);
    this.cx += wx - nx;
    this.cy += wy - ny;
  }

  pan(dxPx: number, dyPx: number): void {
    this.cx -= dxPx * this.s;
    this.cy += (this.yUp ? 1 : -1) * dyPx * this.s;
  }

  fit(b: [number, number, number, number], margin = 0.06): void {
    const bw = Math.max(1e-9, b[2] - b[0]);
    const bh = Math.max(1e-9, b[3] - b[1]);
    this.cx = (b[0] + b[2]) / 2;
    this.cy = (b[1] + b[3]) / 2;
    if (b[2] <= b[0] && b[3] <= b[1]) {
      this.s = 1;
      return;
    }
    this.s = Math.max(bw / (this.w * (1 - 2 * margin)), bh / (this.h * (1 - 2 * margin)));
  }

  // visible world box [x1, y1, x2, y2] (x1 < x2, y1 < y2)
  view(): [number, number, number, number] {
    const [ax, ay] = this.toWorld(0, 0);
    const [bx, by] = this.toWorld(this.w, this.h);
    return [Math.min(ax, bx), Math.min(ay, by), Math.max(ax, bx), Math.max(ay, by)];
  }
}

// "nice" step (1, 2, 5 x 10^n) at least `min`
export function niceStep(min: number): number {
  const e = Math.pow(10, Math.floor(Math.log10(min)));
  for (const m of [1, 2, 5, 10]) if (m * e >= min) return m * e;
  return 10 * e;
}

export function fmt(v: number, digits = 4): string {
  const r = Number(v.toFixed(digits));
  return Object.is(r, -0) ? "0" : String(r);
}

// Where each view was last looking, kept while the page lives so that a reload of the same cell (another version of
// the design, the assistant's edits) comes back to the same place and scale. A working copy `<project>~<id>` shares its
// real project's entry, so confirmed and proposed versions of a cell are compared at the same position.
const saved = new Map<string, { cx: number; cy: number; s: number }>();

export const viewKey = (project: string, lib: string, cell: string, view: string) => `${project.split("~")[0]}|${lib}|${cell}|${view}`;

export function saveView(key: string, cam: Camera): void {
  saved.set(key, { cx: cam.cx, cy: cam.cy, s: cam.s });
}

/** Puts the camera back where `key` was last seen; false if it has not been seen. */
export function restoreView(key: string, cam: Camera): boolean {
  const v = saved.get(key);
  if (!v) return false;
  cam.cx = v.cx; cam.cy = v.cy; cam.s = v.s;
  return true;
}
