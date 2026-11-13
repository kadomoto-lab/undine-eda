// Canvas drawing of Xschem primitives (shared by the schematic editor and symbol previews).
import type { Schematic, SchText, SymbolPreview } from "../api";
import type { Camera } from "./camera";
import { SANS, T, rgba } from "../theme";
import type { SchDiff } from "./diff";
import { drawingConnectivity, pointKey } from "./schConnectivity";

const LINE_H = 1.16; // Xschem: font extents height relative to the size

// Text box as in Xschem's text_bbox(): returns [x1, y1, x2, y2].
export function textBox(t: SchText, w: number, h: number): [number, number, number, number] {
  let x1 = t.x, y1 = t.y;
  const r = t.rot, f = t.flip;
  if (t.hc) {
    const d = w / 2;
    if (r === 0) x1 += f ? d : -d;
    else if (r === 1) y1 += f ? d : -d;
    else if (r === 2) x1 += f ? -d : d;
    else y1 += f ? -d : d;
  }
  if (t.vc) {
    const d = h / 2;
    if (r === 0) y1 -= d;
    else if (r === 1) x1 += d;
    else if (r === 2) y1 += d;
    else x1 -= d;
  }
  const px = x1 + w, py = y1 + h;
  const xx = f ? 2 * t.x - px : px;
  let rx: number, ry: number;
  if (r === 0) { rx = xx; ry = py; }
  else if (r === 1) { rx = t.x - py + t.y; ry = t.y + xx - t.x; }
  else if (r === 2) { rx = 2 * t.x - xx; ry = 2 * t.y - py; }
  else { rx = t.x + py - t.y; ry = t.y - xx + t.x; }
  return [Math.min(x1, rx), Math.min(y1, ry), Math.max(x1, rx), Math.max(y1, ry)];
}

// Xschem ROTATION(): mirror x first, then rotate by 90 degrees * rot.
export function xf(rot: number, flip: number, x: number, y: number): [number, number] {
  if (flip) x = -x;
  switch (rot & 3) {
    case 0: return [x, y];
    case 1: return [-y, x];
    case 2: return [-x, -y];
    default: return [y, -x];
  }
}

export interface DrawOpts {
  selInsts?: Set<string>; // instance ids
  selWires?: Set<string>; // wire ids
  hiWires?: Set<string>; // wires of the highlighted net(s)
  probeInsts?: Set<string>; // cross-probed instances
  probeWires?: Set<string>;
  showPins?: boolean;
  diff?: SchDiff;   // differences from the other version, drawn over the cell
  pending?: Schematic["pending"];   // connections in the netlist that the drawing does not show: a ratsnest
}

type Prims = Pick<Schematic, "lines" | "boxes" | "polys" | "arcs" | "texts" | "colors">;

export function drawPrims(ctx: CanvasRenderingContext2D, d: Prims, cam: Camera, withText = true): void {
  const px = cam.s;
  const col = (l: number) => d.colors[l] ?? "#cccccc";
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.lineWidth = 1.25 * px;
  for (const [l, x1, y1, x2, y2] of d.lines) {
    ctx.strokeStyle = col(l);
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
  }
  for (const [l, x1, y1, x2, y2, fill] of d.boxes) {
    ctx.strokeStyle = col(l);
    if (fill) {
      ctx.fillStyle = l === 5 ? col(l) : col(l) + (fill === 2 ? "" : "55");
      ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
    }
    ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
  }
  for (const [l, fill, pts] of d.polys) {
    ctx.strokeStyle = col(l);
    ctx.beginPath();
    for (let i = 0; i < pts.length; i += 2) (i ? ctx.lineTo : ctx.moveTo).call(ctx, pts[i], pts[i + 1]);
    if (fill) {
      ctx.fillStyle = col(l);
      ctx.fill();
    }
    ctx.stroke();
  }
  for (const [l, cx, cy, r, a, b, fill] of d.arcs) {
    ctx.strokeStyle = col(l);
    ctx.beginPath();
    if (b >= 360) ctx.arc(cx, cy, r, 0, 2 * Math.PI);
    else ctx.arc(cx, cy, r, (-a * Math.PI) / 180, (-(a + b) * Math.PI) / 180, true);
    if (fill) {
      ctx.fillStyle = col(l);
      ctx.fill();
    }
    ctx.stroke();
  }
  if (!withText) return;
  for (const t of d.texts) {
    const fs = t.size;
    if (fs / cam.s < 3.5) continue;
    ctx.font = `${t.italic ? "italic " : ""}${t.bold ? "600 " : ""}${fs}px ${SANS()}`;
    const lines = t.s.split("\n");
    const w = Math.max(...lines.map((s) => ctx.measureText(s).width));
    const h = lines.length * fs * LINE_H;
    const [bx1, by1, , by2] = textBox(t, w, h);
    ctx.fillStyle = col(t.layer);
    ctx.textBaseline = "top";
    ctx.save();
    if (t.rot & 1) {
      ctx.translate(bx1, by2);
      ctx.rotate(-Math.PI / 2);
    } else ctx.translate(bx1, by1);
    lines.forEach((s, i) => ctx.fillText(s, 0, i * fs * LINE_H + fs * 0.08));
    ctx.restore();
  }
}

// `ctx` must already have the world transform set (1 world unit = 1 schematic unit).
export function drawSchematic(ctx: CanvasRenderingContext2D, d: Schematic, cam: Camera, o: DrawOpts): void {
  const px = cam.s; // one CSS pixel in world units
  const col = (l: number) => d.colors[l] ?? "#cccccc";
  drawPrims(ctx, d, cam);

  // wires
  const connections = drawingConnectivity(d);
  d.wires.forEach(([x1, y1, x2, y2, bus, lab, wid]) => {
    const hi = o.hiWires?.has(wid), sel = o.selWires?.has(wid), pr = o.probeWires?.has(wid);
    ctx.strokeStyle = sel ? T.sel : pr ? T.probe : hi ? T.ok : col(1);
    ctx.lineWidth = ((bus ? 4 : 1.6) + (sel || hi || pr ? 1 : 0)) * px;
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(x2, y2);
    ctx.stroke();
    if (lab && px < 0.6) {
      ctx.fillStyle = T.textSoft;
      ctx.font = `${10}px ${SANS()}`;
      ctx.textBaseline = "bottom";
      ctx.fillText(lab, (x1 + x2) / 2, (y1 + y2) / 2 - 2);
    }
  });
  ctx.fillStyle = col(1);
  for (const [x, y] of connections.junctions) {
    ctx.beginPath();
    ctx.arc(x, y, Math.max(3, 3 * px), 0, 2 * Math.PI);
    ctx.fill();
  }

  // pin markers: dangling pins are drawn red
  if (o.showPins && d.pins) {
    ctx.fillStyle = rgba("err", 0.9);
    for (const [, , x, y, net] of d.pins) {
      if (!connections.connectedPins.has(pointKey(x, y)) && /^net\d+$/.test(net)) {
        // pin touches nothing named: a dangling pin
        ctx.beginPath();
        ctx.arc(x, y, Math.max(2.5, 2.5 * px), 0, 2 * Math.PI);
        ctx.fill();
      }
    }
  }

  // instance highlights
  for (const inst of d.insts) {
    const [x1, y1, x2, y2] = inst.bbox;
    const sel = o.selInsts?.has(inst.id), pr = o.probeInsts?.has(inst.id);
    if (!sel && !pr) continue;
    ctx.strokeStyle = sel ? T.sel : T.probe;
    ctx.fillStyle = sel ? rgba("sel", 0.08) : rgba("probe", 0.10);
    ctx.lineWidth = 1.5 * px;
    ctx.fillRect(x1 - 3, y1 - 3, x2 - x1 + 6, y2 - y1 + 6);
    ctx.strokeRect(x1 - 3, y1 - 3, x2 - x1 + 6, y2 - y1 + 6);
  }
  if (o.pending?.length) drawPending(ctx, o.pending, px);
  if (o.diff) drawDiff(ctx, o.diff, px);
}

/** Pending connections (netlist.py): a dashed line from the pin to its net, never a wire until someone draws one. */
function drawPending(ctx: CanvasRenderingContext2D, pending: NonNullable<Schematic["pending"]>, px: number): void {
  ctx.save();
  ctx.strokeStyle = rgba("warn", 0.8);
  ctx.fillStyle = T.warn;
  ctx.lineWidth = 1.2 * px;
  ctx.setLineDash([5 * px, 4 * px]);
  ctx.font = `${10 * px}px 'JetBrains Mono', monospace`;
  ctx.textBaseline = "bottom";
  for (const [, , net, x1, y1, x2, y2] of pending) {
    ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
    ctx.beginPath(); ctx.arc(x1, y1, 3 * px, 0, 2 * Math.PI); ctx.stroke();
    ctx.fillText(net, (x1 + x2) / 2 + 3 * px, (y1 + y2) / 2 - 2 * px);
  }
  ctx.restore();
}

function drawDiff(ctx: CanvasRenderingContext2D, d: SchDiff, px: number): void {
  const wire = (w: SchDiff["wiresAdded"][number], color: string, dash: boolean) => {
    ctx.strokeStyle = color;
    ctx.lineWidth = 3.5 * px;
    ctx.setLineDash(dash ? [6 * px, 4 * px] : []);
    ctx.beginPath(); ctx.moveTo(w[0], w[1]); ctx.lineTo(w[2], w[3]); ctx.stroke();
  };
  d.wiresAdded.forEach((w) => wire(w, rgba("ok", 0.85), false));
  d.wiresRemoved.forEach((w) => wire(w, rgba("err", 0.85), true));
  const box = (i: SchDiff["added"][number], kind: "ok" | "err" | "warn", dash: boolean) => {
    const [x1, y1, x2, y2] = i.bbox;
    ctx.strokeStyle = T[kind];
    ctx.fillStyle = rgba(kind, dash ? 0.06 : 0.16);
    ctx.lineWidth = 2 * px;
    ctx.setLineDash(dash ? [6 * px, 4 * px] : []);
    ctx.fillRect(x1 - 4, y1 - 4, x2 - x1 + 8, y2 - y1 + 8);
    ctx.strokeRect(x1 - 4, y1 - 4, x2 - x1 + 8, y2 - y1 + 8);
    ctx.setLineDash([]);
    ctx.fillStyle = T[kind];
    ctx.font = `${11 * px}px ${SANS()}`;
    ctx.textBaseline = "bottom";
    ctx.fillText(`${kind === "ok" ? "+" : kind === "err" ? "−" : "~"} ${i.name || i.id}`, x1 - 4, y1 - 6 * px);
  };
  d.added.forEach((i) => box(i, "ok", false));
  d.modified.forEach((i) => box(i, "warn", false));
  d.removed.forEach((i) => box(i, "err", true));
  ctx.setLineDash([]);
}

// Draws a symbol preview (ghost) at a placement transform.
export function drawGhost(ctx: CanvasRenderingContext2D, sym: SymbolPreview, cam: Camera, x: number, y: number,
  rot: number, flip: number, withText = false): void {
  const [a, b] = xf(rot, flip, 1, 0);
  const [c, d] = xf(rot, flip, 0, 1);
  ctx.save();
  ctx.globalAlpha = 0.7;
  ctx.transform(a, b, c, d, x, y);
  drawPrims(ctx, sym, cam, withText);
  ctx.restore();
}
