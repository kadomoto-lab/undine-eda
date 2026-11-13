import type { Schematic, SelItem } from "../api";

export interface EditorClipboard {
  version: 1;
  kind: "layout" | "schematic";
  source: { project: string; lib: string };
  payload: Record<string, unknown>;
  bbox: [number, number, number, number];
  preview: SelItem[] | Schematic;
}

const prefix = "undine-clipboard:";
const clone = <T,>(value: T): T => JSON.parse(JSON.stringify(value));
let cached: EditorClipboard | null = null;
let pending: Promise<EditorClipboard> | null = null;
let generation = 0;
let published = false;
let publishing: Promise<void> = Promise.resolve();

/** Capture values rather than selection references. Keep a local fallback when
 * the browser does not grant clipboard access (including non-secure servers). */
export function copyEditorClipboard(snapshot: Promise<EditorClipboard>): Promise<EditorClipboard> {
  const current = ++generation;
  const result = snapshot.then(async value => {
    const captured = clone(value);
    if (current === generation) {
      cached = captured;
      published = false;
      publishing = publishing.then(async () => {
        if (current !== generation) return;
        try {
          if (!navigator.clipboard?.writeText) return;
          await navigator.clipboard.writeText(prefix + JSON.stringify(captured));
          if (current === generation) published = true;
        } catch { /* The in-app clipboard still works without OS permission. */ }
      });
      await publishing;
    }
    return clone(captured);
  });
  pending = result;
  const clear = () => { if (pending === result) pending = null; };
  void result.then(clear, clear);
  return result;
}

const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const numericTuple = (value: unknown, length: number): value is number[] => Array.isArray(value) && value.length === length && value.every(Number.isFinite);
const coordinates = (value: unknown): value is number[] => Array.isArray(value) && value.length >= 2 && value.length % 2 === 0 && value.every(Number.isFinite);
const box = (value: unknown): boolean => numericTuple(value, 4) && value[0] <= value[2] && value[1] <= value[3];
const strings = (value: unknown): boolean => Array.isArray(value) && value.every(item => typeof item === "string");
const properties = (value: unknown): boolean => record(value) && Object.values(value).every(item => typeof item === "string");

function validSchematic(value: unknown): boolean {
  if (!record(value) || value.kind !== "schematic" || !box(value.bbox) || !strings(value.colors)
    || !strings(value.missing) || !properties(value.props)) return false;
  const rows = (key: string, check: (item: unknown) => boolean) => Array.isArray(value[key]) && value[key].every(check);
  if (!rows("lines", item => numericTuple(item, 5)) || !rows("boxes", item => numericTuple(item, 6))
    || !rows("arcs", item => numericTuple(item, 7) && item[3] >= 0)
    || !rows("polys", item => Array.isArray(item) && item.length === 3 && Number.isFinite(item[0]) && Number.isFinite(item[1]) && coordinates(item[2]))
    || !rows("texts", item => record(item) && typeof item.s === "string"
      && ["x", "y", "rot", "flip", "size", "layer"].every(key => Number.isFinite(item[key])) && Number(item.size) >= 0
      && ["hc", "vc", "bold", "italic"].every(key => typeof item[key] === "boolean"))
    || !rows("wires", item => Array.isArray(item) && item.length === 7 && item.slice(0, 4).every(Number.isFinite)
      && typeof item[4] === "boolean" && typeof item[5] === "string" && typeof item[6] === "string")
    || !rows("insts", item => record(item) && box(item.bbox) && properties(item.props)
      && ["id", "name", "symbol", "type"].every(key => typeof item[key] === "string")
      && ["i", "x", "y", "rot", "flip"].every(key => Number.isFinite(item[key])))) return false;
  const pins = (item: unknown, named: boolean) => Array.isArray(item) && item.length === (named ? 5 : 4)
    && typeof item[0] === "string" && typeof item[1] === "string" && Number.isFinite(item[2]) && Number.isFinite(item[3])
    && (!named || typeof item[4] === "string");
  return (value.pins === undefined || rows("pins", item => pins(item, true)))
    && (value.terminals === undefined || rows("terminals", item => pins(item, false)))
    && (value.pinsOfSymbol === undefined || rows("pinsOfSymbol", item => Array.isArray(item) && item.length === 3
      && typeof item[0] === "string" && Number.isFinite(item[1]) && Number.isFinite(item[2])))
    && (value.ports === undefined || rows("ports", item => Array.isArray(item) && item.length === 2 && strings(item)))
    && (value.wire_nets === undefined || strings(value.wire_nets)) && (value.warnings === undefined || strings(value.warnings));
}

function parse(text: string): EditorClipboard | null {
  if (!text.startsWith(prefix) || text.length > 16_000_000) return null;
  try {
    const value = JSON.parse(text.slice(prefix.length));
    if (value?.version !== 1 || !["layout", "schematic"].includes(value.kind)
      || typeof value.source?.project !== "string" || typeof value.source?.lib !== "string"
      || !value.payload || typeof value.payload !== "object" || Array.isArray(value.payload)
      || value.payload.kind !== `undine-${value.kind}` || value.payload.version !== 1
      || !box(value.bbox)) return null;
    if (value.kind === "layout") {
      if (!Array.isArray(value.preview) || !value.preview.length || !value.preview.every((item: SelItem) =>
        item && ["shape", "inst"].includes(item.kind) && Array.isArray(item.outline)
        && item.outline.every(coordinates) && box(item.bbox))) return null;
    } else if (!validSchematic(value.preview)) return null;
    return value;
  } catch { return null; }
}

export async function readEditorClipboard(): Promise<EditorClipboard | null> {
  // Ctrl+V immediately after Ctrl+C must await the new snapshot, not paste an
  // older selection. If another copy supersedes it, wait for that one too.
  if (pending) {
    while (pending) await pending;
    return clone(cached);
  }
  if (cached && !published) return clone(cached);
  try {
    if (navigator.clipboard?.readText) return parse(await navigator.clipboard.readText());
  } catch { /* Permission denied: use this app's last copied selection. */ }
  return clone(cached);
}
