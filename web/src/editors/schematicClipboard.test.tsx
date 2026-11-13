// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type Schematic } from "../api";
import { setLocale, t } from "../i18n";
import { Camera, saveView, viewKey } from "./camera";
import { copyEditorClipboard, readEditorClipboard, type EditorClipboard } from "./editorClipboard";
import { drawSchematic } from "./schdraw";
import { SchematicView } from "./SchematicView";

vi.mock("../App", () => ({ hashPanel: () => "props" }));
vi.mock("../layout", () => ({ Splitter: () => null }));
vi.mock("./schdraw", () => ({ drawSchematic: vi.fn(), drawGhost: vi.fn() }));
vi.mock("./editorClipboard", () => ({ copyEditorClipboard: vi.fn(), readEditorClipboard: vi.fn() }));
vi.mock("../api", async (original) => ({
  ...await original<typeof import("../api")>(),
  api: { schematic: vi.fn(), schRoute: vi.fn(), schMovePreview: vi.fn(), schEdit: vi.fn(), schInstance: vi.fn(),
    schCopy: vi.fn(), schPaste: vi.fn(), symbols: vi.fn(), symbol: vi.fn() },
}));

const blank: Schematic = {
  kind: "schematic", bbox: [-100, -100, 100, 100], colors: [], lines: [], boxes: [], polys: [], arcs: [], texts: [],
  wires: [], insts: [], missing: [], props: {}, undo: 1, redo: 1,
};
const resistor = (id: string, x: number): Schematic["insts"][number] => ({
  i: 0, id, name: id, symbol: "r.sym", type: "resistor", x, y: 0, rot: 0, flip: 0,
  bbox: [x - 10, -10, x + 10, 10], props: { name: id, value: "1k" },
});
const sourceView: Schematic = { ...blank, insts: [resistor("R1", 0)] };
const payload = { kind: "undine-schematic", version: 1, instances: [{ symbol: "r.sym", x: 0, y: 0, props: { name: "R1", value: "1k" } }] };
const clipboard = (): EditorClipboard => ({
  version: 1, kind: "schematic", source: { project: "p", lib: "l" }, payload,
  bbox: [-10, -10, 10, 10], preview: sourceView,
});
const toast = vi.fn();
const props = { project: "p", lib: "l", cell: "c", view: "schematic" as const, active: true, readonly: false,
  hasLayout: false, probe: null, onProbe: () => {}, onOpenSymbol: () => {}, onOpenLayout: () => {}, toast };
let stored: EditorClipboard | null;
let pending: Promise<EditorClipboard> | null;
let frames: Map<number, FrameRequestCallback>;
let context: { setTransform: ReturnType<typeof vi.fn>; fillRect: ReturnType<typeof vi.fn>; strokeRect: ReturnType<typeof vi.fn>;
  save: ReturnType<typeof vi.fn>; restore: ReturnType<typeof vi.fn>; translate: ReturnType<typeof vi.fn> };

beforeEach(async () => {
  vi.clearAllMocks();
  stored = null; pending = null;
  frames = new Map(); let id = 0;
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => { frames.set(++id, callback); return id; }));
  vi.stubGlobal("cancelAnimationFrame", vi.fn((key: number) => { frames.delete(key); }));
  vi.stubGlobal("localStorage", { getItem: () => null, setItem: () => {} });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, toJSON: () => ({}),
  });
  context = { setTransform: vi.fn(), fillRect: vi.fn(), strokeRect: vi.fn(), save: vi.fn(), restore: vi.fn(), translate: vi.fn() };
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(context as unknown as CanvasRenderingContext2D);
  for (const cell of ["c", "other"]) saveView(viewKey("p", "l", cell, "schematic"), new Camera(false));
  vi.mocked(api.schematic).mockResolvedValue(sourceView);
  vi.mocked(api.schCopy).mockResolvedValue({ payload, bbox: [-10, -10, 10, 10], preview: sourceView });
  vi.mocked(api.schPaste).mockResolvedValue({ result: ["R2"], view: { ...sourceView, insts: [...sourceView.insts, resistor("R2", 100)] } });
  vi.mocked(api.schEdit).mockResolvedValue({ result: [], view: blank });
  vi.mocked(api.schInstance).mockResolvedValue({ symbol: "r.sym", props: {}, template: {}, type: "resistor" });
  vi.mocked(copyEditorClipboard).mockImplementation((promise) => {
    pending = promise;
    return promise.then((value) => { stored = value; pending = null; return value; });
  });
  vi.mocked(readEditorClipboard).mockImplementation(async () => pending ? await pending : stored);
  await setLocale("en");
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });

async function open(overrides: Partial<typeof props> = {}) {
  const rendered = render(<SchematicView {...props} {...overrides} />);
  await act(async () => {});
  return { ...rendered, canvas: rendered.container.querySelector(".canvaswrap") as HTMLElement };
}

async function click(canvas: HTMLElement, x: number, y: number, shiftKey = false) {
  await act(async () => {
    fireEvent.mouseDown(canvas, { clientX: x + 400, clientY: y + 300, button: 0, shiftKey });
    fireEvent.mouseUp(canvas, { clientX: x + 400, clientY: y + 300, button: 0, shiftKey });
  });
}

async function key(canvas: HTMLElement, key: string, ctrlKey = true) {
  await act(async () => { fireEvent.keyDown(canvas, { key, ctrlKey }); });
}

function flush() {
  for (const [id, callback] of [...frames]) { frames.delete(id); callback(0); }
}

describe("schematic clipboard", () => {
  it("uses Copy and Paste buttons without Duplicate, while preserving Ctrl+D", async () => {
    vi.mocked(api.schEdit).mockResolvedValueOnce({ result: ["R2"], view: {
      ...sourceView, insts: [...sourceView.insts, resistor("R2", 20)],
    } });
    const { canvas } = await open();
    expect(screen.getByTitle(t("editor:clipboard.copyShortcut"))).toBeTruthy();
    expect(screen.getByTitle(t("editor:clipboard.pasteShortcut"))).toBeTruthy();
    expect(screen.queryByTitle(t("editor:action.duplicateShortcut"))).toBeNull();
    await click(canvas, 0, 0);
    await key(canvas, "d");
    expect(api.schEdit).toHaveBeenCalledOnce();
    expect(api.schEdit).toHaveBeenCalledWith("p", "l", "c", {
      op: "move", items: [{ k: "i", id: "R1" }], dx: 20, dy: 20, duplicate: true,
    });
    expect(api.schInstance).toHaveBeenLastCalledWith("p", "l", "c", "R2");
  });

  it("copies a device, wire and label without editing, then previews and atomically pastes at the pointer", async () => {
    const selection = { ...sourceView, insts: [resistor("R1", 0), { ...resistor("lab1", 50), type: "label" }],
      wires: [[-40, 30, 60, 30, false, "out", "w0"]] as Schematic["wires"] };
    vi.mocked(api.schematic).mockResolvedValue(selection);
    const rendered = await open();
    await click(rendered.canvas, 0, 0);
    await click(rendered.canvas, 50, 0, true);
    await click(rendered.canvas, -30, 30, true);
    await key(rendered.canvas, "c");
    expect(api.schCopy).toHaveBeenCalledWith("p", "l", "c", [{ k: "i", id: "R1" }, { k: "i", id: "lab1" }, { k: "w", id: "w0" }]);
    expect(api.schEdit).not.toHaveBeenCalled();
    expect(api.schPaste).not.toHaveBeenCalled();
    await key(rendered.canvas, "v");
    fireEvent.mouseMove(rendered.canvas, { clientX: 503, clientY: 356 });
    flush();
    expect(drawSchematic).toHaveBeenLastCalledWith(context, sourceView, expect.any(Camera), { showPins: true });
    expect(context.translate).toHaveBeenLastCalledWith(100, 60);
    expect(api.schPaste).not.toHaveBeenCalled();
    await click(rendered.canvas, 103, 56);
    expect(api.schPaste).toHaveBeenCalledOnce();
    expect(api.schPaste).toHaveBeenCalledWith("p", "l", "c", { source: { project: "p", lib: "l" }, payload, dx: 100, dy: 60 });
    expect(api.schInstance).toHaveBeenLastCalledWith("p", "l", "c", "R2");
    expect(screen.queryByText(t("editor:clipboard.help"))).toBeNull();
  });

  it("waits for a rapid copy then paste, and Escape prevents late clipboard results from arming paste", async () => {
    let resolve!: (value: Awaited<ReturnType<typeof api.schCopy>>) => void;
    vi.mocked(api.schCopy).mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    const { canvas } = await open();
    await click(canvas, 0, 0);
    await key(canvas, "c");
    await key(canvas, "v");
    await key(canvas, "Escape", false);
    await act(async () => { resolve({ payload, bbox: [-10, -10, 10, 10], preview: sourceView }); });
    await click(canvas, 100, 80);
    expect(api.schPaste).not.toHaveBeenCalled();
    await key(canvas, "v");
    await click(canvas, 100, 80);
    expect(api.schPaste).toHaveBeenCalledOnce();
  });

  it("copies in a read-only source and reuses the snapshot after source deletion and a cell switch", async () => {
    const rendered = await open({ readonly: true });
    await click(rendered.canvas, 0, 0);
    await key(rendered.canvas, "c");
    await key(rendered.canvas, "v");
    expect(api.schCopy).toHaveBeenCalledOnce();
    expect(readEditorClipboard).not.toHaveBeenCalled();
    expect((screen.getByTitle(t("editor:clipboard.copyShortcut")) as HTMLButtonElement).disabled).toBe(false);
    vi.mocked(api.schematic).mockResolvedValue(blank);
    rendered.rerender(<SchematicView {...props} cell="other" />);
    await act(async () => {});
    await key(rendered.canvas, "v");
    await click(rendered.canvas, 100, 60);
    await key(rendered.canvas, "v");
    await click(rendered.canvas, 200, 60);
    expect(api.schCopy).toHaveBeenCalledOnce();
    expect(api.schPaste).toHaveBeenCalledTimes(2);
    expect(api.schPaste).toHaveBeenLastCalledWith("p", "l", "other", { source: { project: "p", lib: "l" }, payload, dx: 200, dy: 60 });
  });

  it.each(["readonly", "cell", "unmount"])("ignores a clipboard read that finishes after %s changes", async (change) => {
    let resolve!: (value: EditorClipboard) => void;
    vi.mocked(readEditorClipboard).mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    const rendered = await open();
    await key(rendered.canvas, "v");
    if (change === "unmount") rendered.unmount();
    else rendered.rerender(<SchematicView {...props} readonly={change === "readonly"} cell={change === "cell" ? "other" : "c"} />);
    await act(async () => { resolve(clipboard()); });
    expect(screen.queryByText(t("editor:clipboard.help"))).toBeNull();
    expect(api.schPaste).not.toHaveBeenCalled();
  });

  it.each(["input", "textarea", "select", "editable"])("leaves native %s copy and paste untouched", async (kind) => {
    const { canvas } = await open();
    await click(canvas, 0, 0);
    const target = document.createElement(kind === "editable" ? "div" : kind);
    if (kind === "editable") target.setAttribute("contenteditable", "");
    canvas.append(target);
    expect(fireEvent.keyDown(target, { key: "c", ctrlKey: true })).toBe(true);
    expect(fireEvent.keyDown(target, { key: "v", ctrlKey: true })).toBe(true);
    expect(api.schCopy).not.toHaveBeenCalled();
    expect(readEditorClipboard).not.toHaveBeenCalled();
    target.remove();
  });

  it("rejects another view/library and cancels an armed paste without edits", async () => {
    const { canvas } = await open();
    stored = { ...clipboard(), kind: "layout" };
    await key(canvas, "v");
    stored = { ...clipboard(), source: { project: "p", lib: "elsewhere" } };
    await key(canvas, "v");
    expect(toast).toHaveBeenCalledTimes(2);
    stored = clipboard();
    await key(canvas, "v");
    await key(canvas, "Escape", false);
    await click(canvas, 100, 60);
    expect(api.schPaste).not.toHaveBeenCalled();
    expect(api.schEdit).not.toHaveBeenCalled();
  });

  it("guards repeated clicks while a paste commits and ignores its result after changing cells", async () => {
    let resolve!: (value: Awaited<ReturnType<typeof api.schPaste>>) => void;
    vi.mocked(api.schPaste).mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    stored = clipboard();
    const rendered = await open();
    await key(rendered.canvas, "v");
    await click(rendered.canvas, 100, 60);
    await click(rendered.canvas, 200, 60);
    expect(api.schPaste).toHaveBeenCalledOnce();
    vi.mocked(api.schematic).mockResolvedValue(blank);
    rendered.rerender(<SchematicView {...props} cell="other" />);
    await act(async () => { resolve({ result: ["R2"], view: { ...blank, insts: [resistor("R2", 100)] } }); });
    expect(api.schInstance).not.toHaveBeenCalledWith("p", "l", "other", "R2");
    flush();
    expect(drawSchematic).toHaveBeenLastCalledWith(context, blank, expect.any(Camera), expect.any(Object));
  });

  it("shows a committed paste after Escape without resurrecting paste mode", async () => {
    let resolve!: (value: Awaited<ReturnType<typeof api.schPaste>>) => void;
    vi.mocked(api.schPaste).mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    stored = clipboard();
    const { canvas } = await open();
    await key(canvas, "v");
    await click(canvas, 100, 60);
    await key(canvas, "Escape", false);
    const committed = { ...blank, insts: [resistor("R2", 100)] };
    await act(async () => { resolve({ result: ["R2"], view: committed }); });
    flush();
    expect(drawSchematic).toHaveBeenLastCalledWith(context, committed, expect.any(Camera), expect.any(Object));
    expect(screen.queryByText(t("editor:clipboard.help"))).toBeNull();
  });

  it("does not edit the copied originals with rotation or deletion while positioning the paste", async () => {
    const { canvas } = await open();
    await click(canvas, 0, 0);
    await key(canvas, "c");
    await key(canvas, "v");
    await key(canvas, "r", false);
    await key(canvas, "x", false);
    await key(canvas, "Delete", false);
    expect(api.schEdit).not.toHaveBeenCalled();
    expect(api.schPaste).not.toHaveBeenCalled();
    await click(canvas, 100, 60);
    expect(api.schPaste).toHaveBeenCalledOnce();
  });

  it("accepts a snapshot from the real project's library in its changeset", async () => {
    stored = clipboard();
    const { canvas } = await open({ project: "p~cs-example" });
    await key(canvas, "v");
    await click(canvas, 100, 60);
    expect(api.schPaste).toHaveBeenCalledWith("p~cs-example", "l", "c", expect.objectContaining({ source: { project: "p", lib: "l" }, payload }));
  });
});

describe("schematic range selection", () => {
  const rangeView: Schematic = {
    ...blank,
    insts: [resistor("R1", 0), { ...resistor("R2", 50), y: 20, bbox: [40, 10, 60, 30] },
      { ...resistor("lab1", 25), type: "label", y: 40, bbox: [20, 35, 30, 45] }, resistor("R3", 100)],
    wires: [[10, 40, 60, 40, false, "out", "inside"], [-30, 50, 100, 50, false, "in", "crossing"]],
  };
  const group = [{ k: "i", id: "R1" }, { k: "i", id: "R2" }, { k: "i", id: "lab1" }, { k: "w", id: "inside" }];

  beforeEach(() => { vi.mocked(api.schematic).mockResolvedValue(rangeView); });

  async function range(canvas: HTMLElement, from: [number, number], to: [number, number], shiftKey = false) {
    await act(async () => {
      fireEvent.mouseDown(canvas, { clientX: 400 + from[0], clientY: 300 + from[1], button: 0, shiftKey });
      fireEvent.mouseMove(canvas, { clientX: 400 + to[0], clientY: 300 + to[1], buttons: 1, shiftKey });
      fireEvent.mouseUp(canvas, { clientX: 400 + to[0], clientY: 300 + to[1], button: 0, shiftKey });
    });
  }

  it.each([false, true])("selects fully contained devices, labels and wires in either drag direction (reverse=%s)", async (reverse) => {
    const { canvas } = await open();
    await range(canvas, reverse ? [80, 60] : [-20, -20], reverse ? [-20, -20] : [80, 60]);
    await key(canvas, "c");
    expect(api.schCopy).toHaveBeenCalledWith("p", "l", "c", group);
    expect(api.schEdit).not.toHaveBeenCalled();
    expect(api.schMovePreview).not.toHaveBeenCalled();
    expect(api.schPaste).not.toHaveBeenCalled();
  });

  it("starts a selection rectangle on a device and draws it live without moving anything", async () => {
    const { canvas } = await open();
    fireEvent.mouseDown(canvas, { clientX: 400, clientY: 300, button: 0 });
    fireEvent.mouseMove(canvas, { clientX: 480, clientY: 350, buttons: 1 });
    flush();
    expect(context.strokeRect).toHaveBeenLastCalledWith(0, 0, 80, 50);
    expect(api.schEdit).not.toHaveBeenCalled();
    expect(api.schMovePreview).not.toHaveBeenCalled();
    await act(async () => { fireEvent.mouseUp(canvas, { clientX: 480, clientY: 350, button: 0 }); });
    await key(canvas, "c");
    expect(api.schCopy).toHaveBeenCalledWith("p", "l", "c", group.slice(1));
  });

  it("toggles enclosed items with Shift and preserves click selection", async () => {
    const { canvas } = await open();
    await click(canvas, 0, 0);
    await range(canvas, [35, 5], [65, 32], true);
    await key(canvas, "c");
    expect(api.schCopy).toHaveBeenLastCalledWith("p", "l", "c", group.slice(0, 2));
    await range(canvas, [-15, -15], [15, 15], true);
    await key(canvas, "c");
    expect(api.schCopy).toHaveBeenLastCalledWith("p", "l", "c", [{ k: "i", id: "R2" }]);
    await click(canvas, 50, 20, true);
    expect((screen.getByTitle(t("editor:clipboard.copyShortcut")) as HTMLButtonElement).disabled).toBe(true);
  });

  it("cancels an unfinished rectangle with Escape and retains the previous selection", async () => {
    const { canvas } = await open();
    await click(canvas, 0, 0);
    fireEvent.mouseDown(canvas, { clientX: 380, clientY: 280, button: 0, shiftKey: true });
    fireEvent.mouseMove(canvas, { clientX: 480, clientY: 360, buttons: 1, shiftKey: true });
    await key(canvas, "Escape", false);
    await act(async () => { fireEvent.mouseUp(canvas, { clientX: 480, clientY: 360, button: 0 }); });
    await key(canvas, "c");
    expect(api.schCopy).toHaveBeenCalledWith("p", "l", "c", [{ k: "i", id: "R1" }]);
    expect(api.schEdit).not.toHaveBeenCalled();
  });

  it("allows read-only range selection and copy but disables the move tool", async () => {
    const { canvas } = await open({ readonly: true });
    await key(canvas, "m", false);
    await range(canvas, [-20, -20], [80, 60]);
    await key(canvas, "c");
    expect(api.schCopy).toHaveBeenCalledWith("p", "l", "c", group);
    expect((screen.getByTitle(t("editor:schematic.moveShortcut")) as HTMLButtonElement).disabled).toBe(true);
    await range(canvas, [85, -15], [115, 15]);
    await key(canvas, "c");
    expect(api.schCopy).toHaveBeenLastCalledWith("p", "l", "c", [{ k: "i", id: "R3" }]);
    expect(api.schMovePreview).not.toHaveBeenCalled();
    expect(api.schEdit).not.toHaveBeenCalled();
  });

  it.each(["middle", "right", "space"])("pans with %s drag without changing the selected objects", async (mode) => {
    const { canvas } = await open();
    await click(canvas, 0, 0);
    const button = mode === "middle" ? 1 : mode === "right" ? 2 : 0;
    if (mode === "space") await key(canvas, " ", false);
    fireEvent.mouseDown(canvas, { clientX: 400, clientY: 300, button });
    fireEvent.mouseMove(canvas, { clientX: 440, clientY: 320, buttons: 1 << button });
    if (mode === "space") fireEvent.keyUp(window, { key: " " });
    await act(async () => { fireEvent.mouseUp(canvas, { clientX: 440, clientY: 320, button }); });
    await key(canvas, "c");
    expect(api.schCopy).toHaveBeenCalledWith("p", "l", "c", [{ k: "i", id: "R1" }]);
    flush();
    expect(vi.mocked(drawSchematic).mock.lastCall![2].cx).toBe(-40);
    expect(vi.mocked(drawSchematic).mock.lastCall![2].cy).toBe(-20);
    expect(api.schEdit).not.toHaveBeenCalled();
  });

  it("also supports the explicit Move tool for a range-selected group", async () => {
    vi.useFakeTimers();
    vi.mocked(api.schMovePreview).mockResolvedValue({ result: ["R1", "R2", "lab1", "inside"], view: rangeView });
    const { canvas } = await open();
    await range(canvas, [-20, -20], [80, 60]);
    fireEvent.click(screen.getByTitle(t("editor:schematic.moveShortcut")));
    fireEvent.mouseDown(canvas, { clientX: 400, clientY: 300, button: 0 });
    fireEvent.mouseMove(canvas, { clientX: 440, clientY: 330, buttons: 1 });
    await act(async () => { await vi.advanceTimersByTimeAsync(30); });
    expect(api.schMovePreview).toHaveBeenCalledWith("p", "l", "c", { items: group, dx: 40, dy: 30 });
    expect(api.schEdit).not.toHaveBeenCalled();
    await act(async () => { fireEvent.mouseUp(canvas, { clientX: 440, clientY: 330, button: 0 }); });
    expect(api.schEdit).toHaveBeenCalledWith("p", "l", "c", { op: "move", items: group, dx: 40, dy: 30 });
  });

  it("automatically moves a selection from empty canvas space and highlights Move only after the drag threshold", async () => {
    vi.useFakeTimers();
    vi.mocked(api.schMovePreview).mockResolvedValue({ result: ["R1", "R2", "lab1", "inside"], view: rangeView });
    const { canvas } = await open();
    await range(canvas, [-20, -20], [80, 60]);
    const move = screen.getByTitle(t("editor:schematic.moveShortcut"));
    const select = screen.getByTitle(t("editor:action.selectShortcut"));
    fireEvent.mouseDown(canvas, { clientX: 550, clientY: 400, button: 0 });
    fireEvent.mouseMove(canvas, { clientX: 552, clientY: 401, buttons: 1 });
    expect(move.classList.contains("on")).toBe(false);
    expect(select.classList.contains("on")).toBe(true);
    expect(api.schMovePreview).not.toHaveBeenCalled();
    fireEvent.mouseMove(canvas, { clientX: 590, clientY: 430, buttons: 1 });
    expect(move.classList.contains("on")).toBe(true);
    expect(select.classList.contains("on")).toBe(false);
    await act(async () => { await vi.advanceTimersByTimeAsync(30); });
    expect(api.schMovePreview).toHaveBeenCalledWith("p", "l", "c", { items: group, dx: 40, dy: 30 });
    expect(api.schEdit).not.toHaveBeenCalled();
    await act(async () => { fireEvent.mouseUp(canvas, { clientX: 590, clientY: 430, button: 0 }); });
    expect(api.schEdit).toHaveBeenCalledOnce();
    expect(api.schEdit).toHaveBeenCalledWith("p", "l", "c", { op: "move", items: group, dx: 40, dy: 30 });
    expect(move.classList.contains("on")).toBe(false);
    expect(select.classList.contains("on")).toBe(true);
  });

  it("cancels an automatic move with Escape and keeps the entire original selection", async () => {
    const { canvas } = await open();
    await range(canvas, [-20, -20], [80, 60]);
    fireEvent.mouseDown(canvas, { clientX: 550, clientY: 400, button: 0 });
    fireEvent.mouseMove(canvas, { clientX: 590, clientY: 430, buttons: 1 });
    await key(canvas, "Escape", false);
    await act(async () => { fireEvent.mouseUp(canvas, { clientX: 590, clientY: 430, button: 0 }); });
    await key(canvas, "c");
    expect(api.schCopy).toHaveBeenCalledWith("p", "l", "c", group);
    expect(api.schEdit).not.toHaveBeenCalled();
    expect(screen.getByTitle(t("editor:action.selectShortcut")).classList.contains("on")).toBe(true);
  });

  it("still selects a clicked object and clears selection on a simple empty-space click", async () => {
    const { canvas } = await open();
    await range(canvas, [-20, -20], [80, 60]);
    await click(canvas, 50, 20);
    await key(canvas, "c");
    expect(api.schCopy).toHaveBeenCalledWith("p", "l", "c", [{ k: "i", id: "R2" }]);
    await click(canvas, 150, 100);
    expect((screen.getByTitle(t("editor:clipboard.copyShortcut")) as HTMLButtonElement).disabled).toBe(true);
    await range(canvas, [-20, -20], [80, 60]);
    await key(canvas, "c");
    expect(api.schCopy).toHaveBeenLastCalledWith("p", "l", "c", group);
    expect(api.schMovePreview).not.toHaveBeenCalled();
    expect(api.schEdit).not.toHaveBeenCalled();
  });

  it("keeps Shift-drag as range selection even in explicit Move mode", async () => {
    const { canvas } = await open();
    await click(canvas, 0, 0);
    await key(canvas, "m", false);
    await range(canvas, [35, 5], [65, 32], true);
    await key(canvas, "c");
    expect(api.schCopy).toHaveBeenCalledWith("p", "l", "c", group.slice(0, 2));
    expect(api.schMovePreview).not.toHaveBeenCalled();
    expect(api.schEdit).not.toHaveBeenCalled();
  });
});
