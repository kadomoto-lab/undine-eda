// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type LayoutInfo, type SelItem } from "../api";
import { setLocale, t } from "../i18n";
import { Camera, saveView, viewKey } from "./camera";
import { copyEditorClipboard, readEditorClipboard, type EditorClipboard } from "./editorClipboard";
import { LayoutEditor } from "./LayoutEditor";

vi.mock("../App", () => ({ hashPanel: () => "props" }));
vi.mock("../layout", () => ({ Splitter: () => null }));
vi.mock("./tiles", () => ({ TileCache: class { clear() {} get() {} getStale() {} want() {} } }));
vi.mock("./editorClipboard", () => ({ copyEditorClipboard: vi.fn(), readEditorClipboard: vi.fn() }));
vi.mock("../api", async (original) => ({
  ...await original<typeof import("../api")>(),
  api: {
    layout: vi.fn(), layers: vi.fn(), select: vi.fn(), layoutCopy: vi.fn(), layoutPaste: vi.fn(),
    remove: vi.fn(), move: vi.fn(), integrity: vi.fn(), undo: vi.fn(), redo: vi.fn(), tileUrl: vi.fn(),
  },
}));

const original: SelItem = { kind: "shape", type: "box", ref: { k: "s", id: 1 }, bbox: [0, 0, 40, 20], outline: [[0, 0, 40, 0, 40, 20, 0, 20]] };
const inserted: SelItem = { ...original, ref: { k: "s", id: 2 }, bbox: [80, 40, 120, 60] };
const layout: LayoutInfo = { cell: "c", dbu: 0.001, bbox: [-100, -100, 100, 100], version: 1, dirty: false, tile_px: 256, children: [], readonly: false };
const snapshot = { payload: { items: [{ shape: "(0,0;40,20)" }] }, bbox: original.bbox, preview: [original] };
const toast = vi.fn();
const onDirty = vi.fn();
let clipboard: Promise<EditorClipboard> | null;

beforeEach(async () => {
  vi.clearAllMocks();
  clipboard = null;
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.stubGlobal("localStorage", { getItem: () => null, setItem: () => {} });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, toJSON: () => ({}),
  });
  for (const cell of ["c", "other"]) saveView(viewKey("p", "l", cell, "layout"), new Camera(true));
  vi.mocked(api.layout).mockResolvedValue(layout);
  vi.mocked(api.layers).mockResolvedValue([]);
  vi.mocked(api.select).mockResolvedValue([original]);
  vi.mocked(api.layoutCopy).mockResolvedValue(snapshot);
  vi.mocked(api.layoutPaste).mockResolvedValue({ version: 2, dirty: true, result: [inserted] });
  vi.mocked(api.remove).mockResolvedValue({ version: 2, dirty: true, result: [] });
  vi.mocked(api.move).mockResolvedValue({ version: 2, dirty: true, result: [inserted] });
  vi.mocked(api.undo).mockResolvedValue({ version: 3, dirty: false, result: "move" });
  vi.mocked(api.redo).mockResolvedValue({ version: 4, dirty: true, result: "move" });
  vi.mocked(api.integrity).mockResolvedValue({ problems: [] } as Awaited<ReturnType<typeof api.integrity>>);
  vi.mocked(copyEditorClipboard).mockImplementation((promise) => { clipboard = promise; return promise; });
  vi.mocked(readEditorClipboard).mockImplementation(async () => clipboard ? await clipboard : null);
  await setLocale("en");
});

function pointer(x: number, y: number, extras: Record<string, unknown> = {}) {
  return { clientX: 400 + x, clientY: 300 - y, button: 0, ...extras };
}

async function startDrag(canvas: HTMLElement, extras: Record<string, unknown> = {}, to: [number, number] = [177, 117]) {
  fireEvent.mouseDown(canvas, pointer(150, 100, extras));
  await act(async () => { fireEvent.mouseMove(canvas, pointer(to[0], to[1], { buttons: 1, ...extras })); });
}

describe("layout automatic selected-object dragging", () => {
  it("has no standalone Pan tool and highlights Move during a selected-group drag anywhere", async () => {
    const second: SelItem = { ...original, ref: { k: "s", id: 3 } };
    vi.mocked(api.select).mockResolvedValueOnce([original, second]);
    vi.mocked(api.move).mockResolvedValueOnce({ version: 2, dirty: true, result: [inserted, second] });
    const { canvas, container } = await open();
    expect(screen.queryByTitle(`${t("editor:tool.pan")} (Space)`)).toBeNull();
    expect(screen.queryByTitle(`${t("editor:tool.copy")} (C)`)).toBeNull();
    expect(container.querySelectorAll(".toolrail .railbtn")[1].getAttribute("title")).toBe(`${t("editor:tool.move")} (M)`);
    await click(canvas);
    await startDrag(canvas);
    expect(screen.getByTitle(`${t("editor:tool.move")} (M)`).classList.contains("on")).toBe(true);
    expect(screen.getByText(t("editor:layout.help.dragMove"))).toBeTruthy();
    expect(api.move).not.toHaveBeenCalled();
    await act(async () => { fireEvent.mouseUp(canvas, pointer(177, 117)); });
    expect(api.move).toHaveBeenCalledTimes(1);
    expect(api.move).toHaveBeenCalledWith("p", "l", "c", [original.ref, second.ref], 25, 15, false);
    expect(screen.getByTitle(`${t("editor:tool.select")} (Esc)`).classList.contains("on")).toBe(true);
    expect(screen.getByText(t("editor:selection.count", { count: 2 }))).toBeTruthy();
    await key(canvas, "z", true);
    expect(api.undo).toHaveBeenCalledTimes(1);
  });

  it("draws the selected group's moved outlines while the mouse is held", async () => {
    const frames = new Map<number, FrameRequestCallback>();
    let frameId = 0;
    vi.stubGlobal("requestAnimationFrame", vi.fn((frame: FrameRequestCallback) => { frames.set(++frameId, frame); return frameId; }));
    const context = {
      setTransform: vi.fn(), fillRect: vi.fn(), beginPath: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(),
      stroke: vi.fn(), fill: vi.fn(), setLineDash: vi.fn(), closePath: vi.fn(), drawImage: vi.fn(),
    };
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(context as unknown as CanvasRenderingContext2D);
    const { canvas } = await open();
    await click(canvas);
    await startDrag(canvas);
    await act(async () => { for (const [id, frame] of [...frames]) { frames.delete(id); frame(0); } });
    expect(context.moveTo).toHaveBeenCalledWith(425, 285);
    expect(context.lineTo).toHaveBeenCalledWith(465, 265);
    expect(api.move).not.toHaveBeenCalled();
  });

  it("keeps a small pointer jitter as a click and allows an empty click to deselect", async () => {
    const { canvas } = await open();
    await click(canvas);
    vi.mocked(api.select).mockResolvedValueOnce([]);
    await startDrag(canvas, {}, [153, 102]);
    expect(screen.getByTitle(`${t("editor:tool.move")} (M)`).classList.contains("on")).toBe(false);
    await act(async () => { fireEvent.mouseUp(canvas, pointer(153, 102)); });
    expect(api.move).not.toHaveBeenCalled();
    expect(api.select).toHaveBeenLastCalledWith("p", "l", "c", [153, 102, 153, 102], true, null, 5);
    expect((screen.getByTitle(t("editor:clipboard.copyShortcut")) as HTMLButtonElement).disabled).toBe(true);
  });

  it.each([false, true])("uses range selection when no objects are selected or Shift is held (Shift=%s)", async (shiftKey) => {
    const { canvas } = await open();
    if (shiftKey) await click(canvas);
    await startDrag(canvas, { shiftKey });
    await act(async () => { fireEvent.mouseUp(canvas, pointer(177, 117, { shiftKey })); });
    expect(api.move).not.toHaveBeenCalled();
    expect(api.select).toHaveBeenLastCalledWith("p", "l", "c", [150, 100, 177, 117], false, null, 5);
  });

  it.each(["Escape", "leave", "blur", "outside", "right-release", "control-release"])("cancels a move on %s without an edit", async (reason) => {
    const { canvas } = await open();
    await click(canvas);
    await startDrag(canvas);
    if (reason === "Escape") await key(canvas, "Escape");
    else if (reason === "leave") fireEvent.mouseLeave(canvas);
    else if (reason === "blur") fireEvent.blur(window);
    else if (reason === "outside") fireEvent.mouseUp(document.body, pointer(177, 117));
    else if (reason === "right-release") fireEvent.mouseUp(canvas, pointer(177, 117, { button: 2 }));
    else fireEvent.mouseUp(canvas.querySelector(".statusbar")!, pointer(177, 117));
    await act(async () => { fireEvent.mouseUp(canvas, pointer(177, 117)); });
    expect(api.move).not.toHaveBeenCalled();
    expect(screen.getByTitle(`${t("editor:tool.select")} (Esc)`).classList.contains("on")).toBe(true);
    expect((screen.getByTitle(t("editor:clipboard.copyShortcut")) as HTMLButtonElement).disabled).toBe(false);
  });

  it("preserves selection when Escape cancels a move before its threshold", async () => {
    const { canvas } = await open();
    await click(canvas);
    fireEvent.mouseDown(canvas, pointer(150, 100));
    await key(canvas, "Escape");
    await act(async () => { fireEvent.mouseUp(canvas, pointer(153, 102)); });
    expect(api.move).not.toHaveBeenCalled();
    expect(api.select).toHaveBeenCalledTimes(1);
    expect((screen.getByTitle(t("editor:clipboard.copyShortcut")) as HTMLButtonElement).disabled).toBe(false);
  });

  it.each(["middle", "right", "space"])("keeps %s-button panning without moving the selection", async (pan) => {
    const { canvas } = await open();
    await click(canvas);
    if (pan === "space") await key(canvas, " ");
    const extras = { button: pan === "middle" ? 1 : pan === "right" ? 2 : 0 };
    await startDrag(canvas, extras);
    await act(async () => { fireEvent.mouseUp(canvas, pointer(177, 117, extras)); });
    fireEvent.keyUp(window, { key: " " });
    expect(api.move).not.toHaveBeenCalled();
    expect(api.select).toHaveBeenCalledTimes(1);
    expect(screen.getByTitle(`${t("editor:tool.select")} (Esc)`).classList.contains("on")).toBe(true);
  });

  it("never moves a read-only selection", async () => {
    const { canvas } = await open(true);
    await click(canvas);
    await startDrag(canvas);
    await act(async () => { fireEvent.mouseUp(canvas, pointer(177, 117)); });
    expect(api.move).not.toHaveBeenCalled();
    expect(api.select).toHaveBeenLastCalledWith("p", "l", "c", [150, 100, 177, 117], false, null, 5);
  });

  it("makes a snapped zero displacement a no-op and retains manual M click-click behavior", async () => {
    const { canvas } = await open();
    await click(canvas);
    await startDrag(canvas);
    await act(async () => { fireEvent.mouseUp(canvas, pointer(151, 101)); });
    expect(api.move).not.toHaveBeenCalled();
    await key(canvas, "m");
    await click(canvas, 20, 10);
    await click(canvas, 100, 50);
    expect(api.move).toHaveBeenCalledWith("p", "l", "c", [original.ref], 80, 40, false);
  });

  it("blocks edits while one drag move is committing and selects the returned references", async () => {
    let resolve!: (value: Awaited<ReturnType<typeof api.move>>) => void;
    vi.mocked(api.move).mockReturnValueOnce(new Promise(done => { resolve = done; }));
    const { canvas } = await open();
    await click(canvas);
    await startDrag(canvas);
    await act(async () => { fireEvent.mouseUp(canvas, pointer(177, 117)); });
    await startDrag(canvas);
    await act(async () => { fireEvent.mouseUp(canvas, pointer(177, 117)); });
    await key(canvas, "Delete");
    await key(canvas, "z", true);
    expect(api.move).toHaveBeenCalledTimes(1);
    expect(api.remove).not.toHaveBeenCalled();
    expect(api.undo).not.toHaveBeenCalled();
    await act(async () => { resolve({ version: 2, dirty: true, result: [inserted] }); });
    await key(canvas, "Delete");
    expect(api.remove).toHaveBeenCalledWith("p", "l", "c", [inserted.ref]);
  });

  it.each(["cell", "readonly"])("cancels a held move and ignores its pending response after changing %s", async (change) => {
    const { canvas, rerender } = await open();
    await click(canvas);
    await startDrag(canvas);
    rerender(change === "cell" ? editor(false, "other") : editor(true));
    await act(async () => { fireEvent.mouseUp(canvas, pointer(177, 117)); });
    expect(api.move).not.toHaveBeenCalled();
    rerender(editor());
    await act(async () => {});
    await click(canvas);
    let resolve!: (value: Awaited<ReturnType<typeof api.move>>) => void;
    vi.mocked(api.move).mockReturnValueOnce(new Promise(done => { resolve = done; }));
    await startDrag(canvas);
    await act(async () => { fireEvent.mouseUp(canvas, pointer(177, 117)); });
    rerender(change === "cell" ? editor(false, "other") : editor(true));
    await act(async () => { resolve({ version: 2, dirty: true, result: [inserted] }); });
    expect(onDirty).not.toHaveBeenCalled();
    expect(screen.getByTitle(`${t("editor:tool.select")} (Esc)`).classList.contains("on")).toBe(true);
  });
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function editor(readonly = false, cell = "c") {
  return <LayoutEditor project="p" lib="l" cell={cell} readonly={readonly} active libCells={["c", "other"]} libraries={["l"]}
    hasSchematic={false} probe={null} onProbe={() => {}} onOpenSchematic={() => {}} onOpenCell={() => {}} onDirty={onDirty} toast={toast} />;
}

async function open(readonly = false) {
  const rendered = render(editor(readonly));
  await act(async () => {});
  return { ...rendered, canvas: rendered.container.querySelector(".canvaswrap") as HTMLElement };
}

async function click(canvas: HTMLElement, x = 20, y = 10) {
  await act(async () => {
    fireEvent.mouseDown(canvas, { clientX: 400 + x, clientY: 300 - y, button: 0 });
    fireEvent.mouseUp(canvas, { clientX: 400 + x, clientY: 300 - y, button: 0 });
  });
}

async function key(canvas: HTMLElement, value: string, ctrlKey = false) {
  await act(async () => { fireEvent.keyDown(canvas, { key: value, ctrlKey }); });
}

describe("layout clipboard", () => {
  it("draws the copied geometry following the pointer before any edit", async () => {
    const frames = new Map<number, FrameRequestCallback>();
    let frameId = 0;
    vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => {
      frames.set(++frameId, callback);
      return frameId;
    }));
    const context = {
      setTransform: vi.fn(), fillRect: vi.fn(), beginPath: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(),
      stroke: vi.fn(), fill: vi.fn(), setLineDash: vi.fn(), closePath: vi.fn(), drawImage: vi.fn(),
    };
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(context as unknown as CanvasRenderingContext2D);
    const { canvas } = await open();
    await click(canvas);
    await key(canvas, "c", true);
    await key(canvas, "v", true);
    fireEvent.mouseMove(canvas, { clientX: 502, clientY: 248 });
    await act(async () => {
      for (const [id, frame] of [...frames]) { frames.delete(id); frame(0); }
    });
    expect(context.moveTo).toHaveBeenCalledWith(480, 260);
    expect(context.lineTo).toHaveBeenCalledWith(520, 240);
    expect(api.layoutPaste).not.toHaveBeenCalled();
  });

  it("copies without editing and previews a snapped placement until one click commits it", async () => {
    const { canvas } = await open();
    await click(canvas);
    await key(canvas, "c", true);
    expect(api.layoutCopy).toHaveBeenCalledWith("p", "l", "c", [original.ref]);
    expect(api.layoutPaste).not.toHaveBeenCalled();
    await key(canvas, "v", true);
    fireEvent.mouseMove(canvas, { clientX: 502, clientY: 248 });
    expect(screen.getByText(t("editor:clipboard.help"))).toBeTruthy();
    expect(api.layoutPaste).not.toHaveBeenCalled();
    await click(canvas, 102, 52);
    expect(api.layoutPaste).toHaveBeenCalledTimes(1);
    expect(api.layoutPaste).toHaveBeenCalledWith("p", "l", "c", {
      source: { project: "p", lib: "l" }, payload: snapshot.payload, dx: 80, dy: 40,
    });
    expect(screen.queryByText(t("editor:clipboard.help"))).toBeNull();
    await key(canvas, "Delete");
    expect(api.remove).toHaveBeenCalledWith("p", "l", "c", [inserted.ref]);
  });

  it("keeps the snapshot after deleting the original and supports repeated pastes", async () => {
    const { canvas } = await open();
    await click(canvas);
    await key(canvas, "c", true);
    await key(canvas, "Delete");
    for (const x of [100, 200]) { await key(canvas, "v", true); await click(canvas, x, 50); }
    expect(api.layoutCopy).toHaveBeenCalledTimes(1);
    expect(api.layoutPaste).toHaveBeenCalledTimes(2);
    expect(vi.mocked(api.layoutPaste).mock.calls.map((c) => c[3].payload)).toEqual([snapshot.payload, snapshot.payload]);
  });

  it("waits for a pending copy when Ctrl+V follows immediately", async () => {
    let resolve!: (value: typeof snapshot) => void;
    vi.mocked(api.layoutCopy).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    const { canvas } = await open();
    await click(canvas);
    await key(canvas, "c", true);
    await key(canvas, "v", true);
    expect(screen.queryByText(t("editor:clipboard.help"))).toBeNull();
    await act(async () => { resolve(snapshot); });
    expect(screen.getByText(t("editor:clipboard.help"))).toBeTruthy();
    await click(canvas, 100, 50);
    expect(api.layoutPaste).toHaveBeenCalledTimes(1);
  });

  it("cancels armed and pending pastes with Escape without editing", async () => {
    const { canvas } = await open();
    await click(canvas);
    await key(canvas, "c", true);
    await key(canvas, "v", true);
    await key(canvas, "Escape");
    await click(canvas);
    expect(api.layoutPaste).not.toHaveBeenCalled();
    let resolve!: (value: EditorClipboard | null) => void;
    vi.mocked(readEditorClipboard).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    await key(canvas, "v", true);
    await key(canvas, "Escape");
    await act(async () => { resolve({ version: 1, kind: "layout", source: { project: "p", lib: "l" }, ...snapshot }); });
    expect(screen.queryByText(t("editor:clipboard.help"))).toBeNull();
    await click(canvas);
    expect(api.layoutPaste).not.toHaveBeenCalled();
  });

  it("copies from a read-only source but does not paste there", async () => {
    const { canvas } = await open(true);
    await click(canvas);
    await key(canvas, "c", true);
    await key(canvas, "v", true);
    await click(canvas, 100, 50);
    expect(api.layoutCopy).toHaveBeenCalledTimes(1);
    expect(api.layoutPaste).not.toHaveBeenCalled();
    expect((screen.getByTitle(t("editor:clipboard.pasteShortcut")) as HTMLButtonElement).disabled).toBe(true);
  });

  it("leaves input, textarea, select and contenteditable clipboard shortcuts alone", async () => {
    const { canvas } = await open();
    await click(canvas);
    for (const tag of ["input", "textarea", "select", "div"]) {
      const field = document.createElement(tag);
      if (tag === "div") field.setAttribute("contenteditable", "true");
      canvas.append(field);
      const copy = new KeyboardEvent("keydown", { key: "c", ctrlKey: true, bubbles: true, cancelable: true });
      const paste = new KeyboardEvent("keydown", { key: "v", ctrlKey: true, bubbles: true, cancelable: true });
      field.dispatchEvent(copy); field.dispatchEvent(paste);
      expect(copy.defaultPrevented).toBe(false);
      expect(paste.defaultPrevented).toBe(false);
      field.remove();
    }
    expect(api.layoutCopy).not.toHaveBeenCalled();
    expect(readEditorClipboard).not.toHaveBeenCalled();
  });

  it("provides toolbar actions and the Meta shortcuts without changing the C duplicate tool", async () => {
    const { canvas } = await open();
    await click(canvas);
    await act(async () => { fireEvent.click(screen.getByTitle(t("editor:clipboard.copyShortcut"))); });
    await act(async () => { fireEvent.click(screen.getByTitle(t("editor:clipboard.pasteShortcut"))); });
    expect(screen.getByText(t("editor:clipboard.help"))).toBeTruthy();
    await key(canvas, "Escape");
    await act(async () => { fireEvent.keyDown(canvas, { key: "c", metaKey: true }); fireEvent.keyDown(canvas, { key: "v", metaKey: true }); });
    expect(api.layoutCopy).toHaveBeenCalledTimes(2);
    expect(screen.getByText(t("editor:clipboard.help"))).toBeTruthy();
    await key(canvas, "Escape");
    await key(canvas, "c");
    expect(canvas.querySelector(".hud-chip")?.textContent).toBe(t("editor:tool.copy"));
    await click(canvas, 20, 10);
    await click(canvas, 100, 50);
    expect(api.move).toHaveBeenCalledWith("p", "l", "c", [original.ref], 80, 40, true);
    expect(api.layoutPaste).not.toHaveBeenCalled();
  });

  it("copies between cells in the same library and rejects a different source library", async () => {
    const { canvas, rerender } = await open();
    await click(canvas);
    await key(canvas, "c", true);
    rerender(editor(false, "other"));
    await act(async () => {});
    await key(canvas, "v", true);
    await click(canvas, 100, 50);
    expect(api.layoutPaste).toHaveBeenCalledWith("p", "l", "other", expect.objectContaining({ payload: snapshot.payload }));
    clipboard = Promise.resolve({ version: 1, kind: "layout", source: { project: "p", lib: "another" }, ...snapshot });
    await key(canvas, "v", true);
    expect(screen.queryByText(t("editor:clipboard.help"))).toBeNull();
    expect(api.layoutPaste).toHaveBeenCalledTimes(1);
  });

  it("allows the same library across the confirmed design and a proposal", async () => {
    const { canvas } = await open();
    clipboard = Promise.resolve({ version: 1, kind: "layout", source: { project: "p~cs-test", lib: "l" }, ...snapshot });
    await key(canvas, "v", true);
    await click(canvas, 100, 50);
    expect(api.layoutPaste).toHaveBeenCalledWith("p", "l", "c", expect.objectContaining({ source: { project: "p~cs-test", lib: "l" } }));
  });

  it("does not enter paste mode for an empty clipboard or schematic objects", async () => {
    const { canvas } = await open();
    await key(canvas, "v", true);
    expect(screen.queryByText(t("editor:clipboard.help"))).toBeNull();
    clipboard = Promise.resolve({ version: 1, kind: "schematic", source: { project: "p", lib: "l" }, ...snapshot });
    await key(canvas, "v", true);
    expect(screen.queryByText(t("editor:clipboard.help"))).toBeNull();
    expect(api.layoutPaste).not.toHaveBeenCalled();
  });

  it("does not arm a pending paste after changing cells or making the view read-only", async () => {
    const { canvas, rerender } = await open();
    let resolve!: (value: EditorClipboard | null) => void;
    for (const next of [editor(false, "other"), editor(true, "other")]) {
      vi.mocked(readEditorClipboard).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
      await key(canvas, "v", true);
      rerender(next);
      await act(async () => { resolve({ version: 1, kind: "layout", source: { project: "p", lib: "l" }, ...snapshot }); });
      expect(screen.queryByText(t("editor:clipboard.help"))).toBeNull();
    }
    expect(api.layoutPaste).not.toHaveBeenCalled();
  });

  it("guards double clicks during commit and does not restore a cancelled paste", async () => {
    let resolve!: (value: Awaited<ReturnType<typeof api.layoutPaste>>) => void;
    vi.mocked(api.layoutPaste).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    const { canvas } = await open();
    await click(canvas);
    await key(canvas, "c", true);
    await key(canvas, "v", true);
    await click(canvas, 100, 50);
    await click(canvas, 120, 50);
    await key(canvas, "Escape");
    await act(async () => { resolve({ version: 2, dirty: true, result: [inserted] }); });
    expect(api.layoutPaste).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(t("editor:clipboard.help"))).toBeNull();
    expect(onDirty).toHaveBeenCalledWith(true);
  });

  it("ignores a paste response after the editor moves to another cell", async () => {
    let resolve!: (value: Awaited<ReturnType<typeof api.layoutPaste>>) => void;
    vi.mocked(api.layoutPaste).mockReturnValueOnce(new Promise((done) => { resolve = done; }));
    const { canvas, rerender } = await open();
    await click(canvas);
    await key(canvas, "c", true);
    await key(canvas, "v", true);
    await click(canvas, 100, 50);
    rerender(editor(false, "other"));
    await act(async () => { resolve({ version: 2, dirty: true, result: [inserted] }); });
    expect(onDirty).not.toHaveBeenCalled();
    expect(screen.queryByText(t("editor:clipboard.help"))).toBeNull();
    await key(canvas, "Delete");
    expect(api.remove).not.toHaveBeenCalled();
  });
});
