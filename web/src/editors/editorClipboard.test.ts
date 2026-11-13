// @vitest-environment jsdom
import { beforeEach, expect, it, vi } from "vitest";
import type { EditorClipboard } from "./editorClipboard";
import type { Schematic } from "../api";

const snapshot = (): EditorClipboard => ({
  version: 1, kind: "layout", source: { project: "p", lib: "l" },
  payload: { kind: "undine-layout", version: 1, effects: [{ s: "box" }] },
  bbox: [0, 0, 10, 10],
  preview: [{ kind: "shape", type: "box", ref: { k: "s" }, bbox: [0, 0, 10, 10], outline: [[0, 0, 10, 0, 10, 10, 0, 10]] }],
});
const schematic = (): EditorClipboard => ({
  ...snapshot(), kind: "schematic", payload: { kind: "undine-schematic", version: 1, items: [] },
  preview: {
    kind: "schematic", bbox: [0, 0, 10, 10], colors: ["#ffffff"],
    lines: [[0, 0, 0, 10, 10]], boxes: [[0, 0, 0, 10, 10, 0]], polys: [[0, 1, [0, 0, 10, 0, 10, 10]]],
    arcs: [[0, 5, 5, 3, 0, 360, 0]],
    texts: [{ s: "R1", x: 0, y: 0, rot: 0, flip: 0, size: 10, layer: 0, hc: false, vc: false, bold: false, italic: false }],
    wires: [[0, 0, 10, 0, false, "out", "w0"]],
    insts: [{ i: 0, id: "R1", name: "R1", symbol: "r.sym", type: "resistor", x: 0, y: 0, rot: 0, flip: 0,
      bbox: [0, 0, 10, 10], props: { name: "R1" } }],
    props: {}, missing: [], pins: [["R1", "p", 0, 0, "out"]], terminals: [["R1", "p", 0, 0]],
    ports: [["out", "out"]], wire_nets: ["out"], warnings: [],
  },
});
beforeEach(() => {
  vi.resetModules();
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
});

it("keeps an independent snapshot and gives each paste an independent value", async () => {
  const { copyEditorClipboard, readEditorClipboard } = await import("./editorClipboard");
  const original = snapshot();
  await copyEditorClipboard(Promise.resolve(original));
  original.bbox[0] = 100;
  const pasted = (await readEditorClipboard())!;
  expect(pasted.bbox[0]).toBe(0);
  pasted.bbox[0] = 200;
  expect((await readEditorClipboard())!.bbox[0]).toBe(0);
});

it("waits for the newest copy when paste follows immediately", async () => {
  const { copyEditorClipboard, readEditorClipboard } = await import("./editorClipboard");
  await copyEditorClipboard(Promise.resolve(snapshot()));
  let resolve!: (value: EditorClipboard) => void;
  const copy = copyEditorClipboard(new Promise(r => { resolve = r; }));
  const paste = readEditorClipboard();
  resolve({ ...snapshot(), source: { project: "p", lib: "new" } });
  await copy;
  expect((await paste)!.source.lib).toBe("new");
});

it("does not let a late older copy replace the latest snapshot", async () => {
  const { copyEditorClipboard, readEditorClipboard } = await import("./editorClipboard");
  let resolve!: (value: EditorClipboard) => void;
  const old = copyEditorClipboard(new Promise(r => { resolve = r; }));
  await copyEditorClipboard(Promise.resolve({ ...snapshot(), source: { project: "p", lib: "new" } }));
  resolve(snapshot());
  await old;
  expect((await readEditorClipboard())!.source.lib).toBe("new");
});

it.each([snapshot, schematic])("uses the OS clipboard across app sessions and ignores unrelated text (%#)", async (drawing) => {
  let text = "";
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
    writeText: vi.fn(async (value: string) => { text = value; }), readText: vi.fn(async () => text),
  } });
  const first = await import("./editorClipboard");
  await first.copyEditorClipboard(Promise.resolve(drawing()));
  vi.resetModules();
  const second = await import("./editorClipboard");
  expect(await second.readEditorClipboard()).toEqual(drawing());
  text = "ordinary text copied from an input";
  expect(await first.readEditorClipboard()).toBeNull();
});

it("retains in-app paste when OS access is denied", async () => {
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
    writeText: vi.fn(async () => { throw new Error("denied"); }),
    readText: vi.fn(async () => { throw new Error("denied"); }),
  } });
  const { copyEditorClipboard, readEditorClipboard } = await import("./editorClipboard");
  await copyEditorClipboard(Promise.resolve(snapshot()));
  expect(await readEditorClipboard()).toEqual(snapshot());
});

it("rejects malformed native clipboard data before previewing it", async () => {
  let value = snapshot();
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
    readText: vi.fn(async () => "undine-clipboard:" + JSON.stringify(value)),
  } });
  const { readEditorClipboard } = await import("./editorClipboard");
  value = { ...snapshot(), bbox: [0, 0, Infinity, 10] };
  expect(await readEditorClipboard()).toBeNull();
  value = { ...snapshot(), preview: [{}] as EditorClipboard["preview"] };
  expect(await readEditorClipboard()).toBeNull();
});

it.each([
  ["lines", [null]], ["boxes", [[0, 0, 0]]], ["polys", [[0, 1, [0, 0, 10]]]],
  ["arcs", [[0, 0, 0, -1, 0, 360, 0]]], ["texts", [{ s: null }]], ["wires", [null]],
  ["insts", [{ bbox: null }]], ["pins", [null]], ["terminals", {}], ["colors", null],
] as const)("rejects malformed schematic %s before it reaches the canvas", async (key, malformed) => {
  const value = schematic();
  (value.preview as Schematic)[key] = malformed as never;
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
    readText: vi.fn(async () => "undine-clipboard:" + JSON.stringify(value)),
  } });
  const { readEditorClipboard } = await import("./editorClipboard");
  expect(await readEditorClipboard()).toBeNull();
});

it("rejects an odd or non-finite layout outline coordinate sequence", async () => {
  const value = snapshot();
  const items = value.preview as import("../api").SelItem[];
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
    readText: vi.fn(async () => "undine-clipboard:" + JSON.stringify(value)),
  } });
  const { readEditorClipboard } = await import("./editorClipboard");
  for (const outline of [[[0, 0, 5]], [[0, 0, Infinity, 5]]]) {
    items[0].outline = outline;
    expect(await readEditorClipboard()).toBeNull();
  }
});

it("surfaces a failed pending copy rather than pasting the previous selection", async () => {
  const { copyEditorClipboard, readEditorClipboard } = await import("./editorClipboard");
  await copyEditorClipboard(Promise.resolve(snapshot()));
  const copy = copyEditorClipboard(Promise.reject(new Error("selection changed")));
  const paste = readEditorClipboard();
  await expect(copy).rejects.toThrow("selection changed");
  await expect(paste).rejects.toThrow("selection changed");
});
