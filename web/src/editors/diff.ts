// Differences between two versions of a cell, for drawing over the view: added green, removed red, changed amber.
import type { Schematic, SchInst } from "../api";

export type SchWire = Schematic["wires"][number];

export interface SchDiff {
  added: SchInst[];       // only in the new version
  removed: SchInst[];     // only in the old version (drawn where they were)
  modified: SchInst[];    // same id, moved / re-parameterised / another symbol (the new version's instance)
  wiresAdded: SchWire[];
  wiresRemoved: SchWire[];
}

const wireKey = (w: SchWire) => `${w[0]},${w[1]},${w[2]},${w[3]}`;
const instSame = (a: SchInst, b: SchInst) =>
  a.x === b.x && a.y === b.y && a.rot === b.rot && a.flip === b.flip && a.symbol === b.symbol &&
  JSON.stringify(Object.entries(a.props).sort()) === JSON.stringify(Object.entries(b.props).sort());

export function schDiff(now: Schematic, before: Schematic): SchDiff {
  const old = new Map(before.insts.map((i) => [i.id, i]));
  const cur = new Map(now.insts.map((i) => [i.id, i]));
  const wOld = new Set(before.wires.map(wireKey)), wNow = new Set(now.wires.map(wireKey));
  return {
    added: now.insts.filter((i) => !old.has(i.id)),
    removed: before.insts.filter((i) => !cur.has(i.id)),
    modified: now.insts.filter((i) => old.has(i.id) && !instSame(i, old.get(i.id)!)),
    wiresAdded: now.wires.filter((w) => !wOld.has(wireKey(w))),
    wiresRemoved: before.wires.filter((w) => !wNow.has(wireKey(w))),
  };
}

export const schDiffEmpty = (d: SchDiff) =>
  !d.added.length && !d.removed.length && !d.modified.length && !d.wiresAdded.length && !d.wiresRemoved.length;

/** Layout difference from the server: polygons as [hull, hole ...] flat x,y point lists in dbu, per layer. */
export interface LayoutDiff {
  dbu: number;
  added: { layer: string; polys: number[][][] }[];
  removed: { layer: string; polys: number[][][] }[];
  truncated: boolean;
}
