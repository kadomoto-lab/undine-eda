// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type SymbolEditBody, type SymbolModel, type SymbolObject } from "../api";
import { setLocale, t } from "../i18n";
import { Camera, saveView, viewKey } from "./camera";
import { hitSymbolObject, SymbolEditor } from "./SymbolEditor";

vi.mock("../layout", () => ({ Splitter: () => null }));
vi.mock("../api", async (original) => ({ ...await original<typeof import("../api")>(), api: { symbolView: vi.fn(), symbolEdit: vi.fn() } }));
const pin = (id = "p1", name = "IN", x = 0, order = 1): SymbolObject => ({ id, kind: "pin", x, y: 0, bbox: [x - 2.5, -2.5, x + 2.5, 2.5], name, direction: "in", order, layer: 5 });
const line: SymbolObject = { id: "l1", kind: "line", x: 0, y: 20, x2: 60, y2: 20, bbox: [0, 20, 60, 20], layer: 4 };
const blank: SymbolModel = { kind: "symbol", bbox: [-100, -100, 100, 100], colors: [], lines: [], boxes: [], polys: [], arcs: [], texts: [], wires: [], insts: [], missing: [], props: {}, objects: [], undo: 1, redo: 1, exists: true };
let data: SymbolModel;
const toast = vi.fn();
const props = { project: "p", lib: "l", cell: "c", active: true, readonly: false, hasSchematic: false, onOpenSchematic: vi.fn(), toast };

beforeEach(async () => {
  vi.clearAllMocks();
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, toJSON: () => ({}) });
  saveView(viewKey("p", "l", "c", "symbol"), new Camera(false));
  data = { ...blank, objects: [] };
  vi.mocked(api.symbolView).mockImplementation(async () => data);
  vi.mocked(api.symbolEdit).mockImplementation(async (_p, _l, _c, body) => {
    const id = `o${vi.mocked(api.symbolEdit).mock.calls.length}`;
    if (body.op === "symbol_add") {
      const o = { ...body, id, bbox: [body.x!, body.y!, body.x2 ?? body.x!, body.y2 ?? body.y!] } as SymbolObject;
      data = { ...data, objects: [...data.objects, o] };
      return { result: id, view: data };
    }
    if (body.op === "symbol_move") {
      data = { ...data, objects: data.objects.map((o) => body.ids?.includes(o.id) ? { ...o, id: `${o.id}m`, x: o.x + body.dx!, y: o.y + body.dy! } : o) };
      return { result: body.ids!.map((old) => `${old}m`), view: data };
    }
    if (body.op === "symbol_update") {
      data = { ...data, objects: data.objects.map((o) => o.id === body.id ? { ...o, ...body, id } : o) };
      return { result: id, view: data };
    }
    if (body.op === "symbol_delete") data = { ...data, objects: data.objects.filter((o) => !body.ids!.includes(o.id)) };
    return { result: [], view: data };
  });
  await setLocale("en");
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

async function open(extra: Partial<typeof props> = {}) {
  const result = render(<SymbolEditor {...props} {...extra} />);
  await act(async () => {});
  return { ...result, canvas: result.container.querySelector(".canvaswrap") as HTMLElement };
}
async function click(canvas: HTMLElement, x: number, y: number, options = {}) {
  await act(async () => { fireEvent.mouseDown(canvas, { clientX: x + 400, clientY: y + 300, button: 0, ...options }); fireEvent.mouseUp(canvas, { clientX: x + 400, clientY: y + 300, button: 0, ...options }); });
}
async function drag(canvas: HTMLElement, x: number, y: number, xx: number, yy: number, options = {}) {
  await act(async () => {
    fireEvent.mouseDown(canvas, { clientX: x + 400, clientY: y + 300, button: 0, ...options });
    fireEvent.mouseMove(canvas, { clientX: xx + 400, clientY: yy + 300, buttons: 1, ...options });
    fireEvent.mouseUp(canvas, { clientX: xx + 400, clientY: yy + 300, button: 0, ...options });
  });
}
function edits(): SymbolEditBody[] { return vi.mocked(api.symbolEdit).mock.calls.map((c) => c[3]); }

describe("symbol graphic editing", () => {
  it("draws a snapped line after two clicks and cancels a pending endpoint with Escape", async () => {
    const { canvas } = await open(); fireEvent.click(screen.getByTitle(t("symbol:tool.line")));
    await click(canvas, 4, 6); expect(edits()).toEqual([]);
    await click(canvas, 53, 34); expect(edits()).toEqual([{ op: "symbol_add", kind: "line", x: 0, y: 10, x2: 50, y2: 30, layer: 4 }]);
    await click(canvas, 90, 90); fireEvent.keyDown(canvas, { key: "Escape" }); await click(canvas, 120, 120);
    expect(edits()).toHaveLength(1);
  });
  it("does not commit a rectangle with zero width or height", async () => {
    const { canvas } = await open(); fireEvent.keyDown(canvas, { key: "b" });
    await click(canvas, 0, 0); await click(canvas, 0, 40); expect(edits()).toEqual([]);
    await click(canvas, 50, 40); expect(edits()[0]).toMatchObject({ kind: "rectangle", x: 0, y: 0, x2: 50, y2: 40 });
  });
  it("creates named pins with direction and advances the default name", async () => {
    const { canvas } = await open(); fireEvent.click(screen.getByTitle(t("symbol:tool.pin")));
    fireEvent.change(screen.getByLabelText(t("symbol:name")), { target: { value: "OUT" } });
    fireEvent.change(screen.getByLabelText(t("symbol:direction")), { target: { value: "out" } });
    await click(canvas, 100, 20);
    expect(edits()[0]).toEqual({ op: "symbol_add", kind: "pin", x: 100, y: 20, name: "OUT", direction: "out" });
    expect((screen.getByLabelText(t("symbol:name")) as HTMLInputElement).value).toBe("P1");
  });
  it("creates text using Xschem scale instead of pixel font size", async () => {
    const { canvas } = await open(); fireEvent.keyDown(canvas, { key: "t" });
    fireEvent.change(screen.getByLabelText(t("symbol:text")), { target: { value: "@name" } });
    await click(canvas, 20, 30); expect(edits()[0]).toEqual({ op: "symbol_add", kind: "text", x: 20, y: 30, text: "@name", size: .3 });
  });
  it("moves a selection from an empty drag origin and uses returned IDs for subsequent Delete", async () => {
    data.objects = [pin()]; const { canvas } = await open(); await click(canvas, 0, 0);
    await drag(canvas, 100, 100, 125, 137);
    expect(edits()[0]).toEqual({ op: "symbol_move", ids: ["p1"], dx: 30, dy: 40 });
    await act(async () => fireEvent.keyDown(canvas, { key: "Delete" }));
    expect(edits()[1]).toEqual({ op: "symbol_delete", ids: ["p1m"] });
  });
  it("range selects graphics and pins for one atomic move", async () => {
    data.objects = [pin(), line]; const { canvas } = await open();
    await drag(canvas, -10, -10, 70, 30); await drag(canvas, 100, 100, 120, 130);
    expect(edits()).toEqual([{ op: "symbol_move", ids: ["p1", "l1"], dx: 20, dy: 30 }]);
  });
  it("Shift drag changes selection without moving selected objects", async () => {
    data.objects = [pin(), line]; const { canvas } = await open(); await click(canvas, 0, 0);
    await drag(canvas, -10, 10, 70, 30, { shiftKey: true });
    expect(edits()).toEqual([]);
    await act(async () => fireEvent.keyDown(canvas, { key: "Delete" }));
    expect(edits()).toEqual([{ op: "symbol_delete", ids: ["p1", "l1"] }]);
  });
  it("cancels a move with Escape without writing", async () => {
    data.objects = [pin()]; const { canvas } = await open(); await click(canvas, 0, 0);
    fireEvent.mouseDown(canvas, { clientX: 400, clientY: 300, button: 0 });
    fireEvent.mouseMove(canvas, { clientX: 450, clientY: 340, buttons: 1 });
    fireEvent.keyDown(canvas, { key: "Escape" });
    fireEvent.mouseUp(canvas, { clientX: 450, clientY: 340, button: 0 });
    expect(edits()).toEqual([]);
  });
  it("edits pin name and direction and reorders it in the netlist", async () => {
    data.objects = [pin(), pin("p2", "OUT", 80, 2)]; const { canvas } = await open(); await click(canvas, 0, 0);
    const name = screen.getByLabelText(t("symbol:name")); fireEvent.change(name, { target: { value: "INPUT" } });
    await act(async () => fireEvent.blur(name));
    expect(edits()[0]).toEqual({ op: "symbol_update", id: "p1", name: "INPUT" });
    await act(async () => fireEvent.change(screen.getByLabelText(t("symbol:direction")), { target: { value: "inout" } }));
    expect(edits()[1]).toMatchObject({ op: "symbol_update", direction: "inout" });
    await act(async () => fireEvent.click(screen.getAllByTitle(t("symbol:down"))[0]));
    expect(edits()[2]).toMatchObject({ op: "symbol_update", order: 2 });
  });
  it("uses Undo/Redo shortcuts while preserving text-field typing", async () => {
    data.objects = [pin()]; const { canvas } = await open();
    await act(async () => fireEvent.keyDown(canvas, { key: "z", ctrlKey: true }));
    await act(async () => fireEvent.keyDown(canvas, { key: "z", ctrlKey: true, shiftKey: true }));
    expect(edits()).toEqual([{ op: "undo" }, { op: "redo" }]);
    await click(canvas, 0, 0); fireEvent.keyDown(screen.getByLabelText(t("symbol:name")), { key: "Delete" });
    expect(edits()).toHaveLength(2);
  });
  it("keeps readonly symbols selectable but blocks all mutations", async () => {
    data.objects = [pin()]; const { canvas } = await open({ readonly: true }); await click(canvas, 0, 0);
    expect((screen.getByTitle(t("symbol:tool.pin")) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.keyDown(canvas, { key: "p" }); await click(canvas, 20, 20); fireEvent.keyDown(canvas, { key: "Delete" });
    fireEvent.keyDown(canvas, { key: "z", ctrlKey: true }); expect(edits()).toEqual([]);
  });
  it("offers Redo after undoing symbol creation and blocks edits until it is restored", async () => {
    data.objects = [pin()]; const { canvas } = await open();
    vi.mocked(api.symbolEdit).mockResolvedValueOnce({ result: [], view: { ...blank, exists: false, objects: [], undo: 0, redo: 1 } });
    await act(async () => fireEvent.keyDown(canvas, { key: "z", ctrlKey: true }));
    expect(screen.getByText(t("symbol:removed"))).toBeTruthy();
    expect((screen.getByTitle(t("symbol:tool.pin")) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTitle(t("editor:action.redoShortcut")) as HTMLButtonElement).disabled).toBe(false);
    fireEvent.keyDown(canvas, { key: "p" }); await click(canvas, 0, 0); expect(edits()).toHaveLength(1);
    await act(async () => fireEvent.click(screen.getByTitle(t("editor:action.redoShortcut"))));
    expect(edits()[1]).toEqual({ op: "redo" });
  });
  it("blocks selection and extra mutations while an edit is pending", async () => {
    data.objects = [pin(), pin("p2", "OUT", 80, 2)]; const { canvas } = await open(); await click(canvas, 0, 0);
    let resolve!: (result: { result: string[]; view: SymbolModel }) => void;
    vi.mocked(api.symbolEdit).mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    await drag(canvas, 0, 0, 20, 20); await click(canvas, 80, 0); fireEvent.keyDown(canvas, { key: "Delete" });
    expect(edits()).toHaveLength(1);
    expect((screen.getAllByRole("button").find((b) => b.classList.contains("symbol-pin-select")) as HTMLButtonElement).disabled).toBe(true);
    await act(async () => resolve({ result: ["p1"], view: data }));
    expect((screen.getByLabelText(t("symbol:name")) as HTMLInputElement).value).toBe("IN");
  });
  it("does not put a stale edit response into a newly opened cell", async () => {
    data.objects = [pin()]; const { canvas, rerender } = await open(); await click(canvas, 0, 0);
    let resolve!: (result: { result: string[]; view: SymbolModel }) => void;
    vi.mocked(api.symbolEdit).mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    await drag(canvas, 0, 0, 20, 20);
    vi.mocked(api.symbolView).mockResolvedValue({ ...blank, objects: [pin("other", "OTHER")] });
    rerender(<SymbolEditor {...props} cell="other" />); await act(async () => {});
    await act(async () => resolve({ result: ["p1"], view: data }));
    expect(screen.getByText("OTHER")).toBeTruthy(); expect(screen.queryByText("IN")).toBeNull();
  });
  it("refreshes an inactive symbol when its tab is shown again", async () => {
    data.objects = [pin()]; const { rerender } = await open();
    rerender(<SymbolEditor {...props} active={false} />);
    data = { ...data, objects: [pin("changed", "CHANGED")] };
    rerender(<SymbolEditor {...props} />); await act(async () => {});
    expect(api.symbolView).toHaveBeenCalledTimes(2); expect(screen.getByText("CHANGED")).toBeTruthy();
  });
  it.each(["undo", "redo"])("reports affected consumer views after %s without refetching this symbol", async (op) => {
    const onChanged = vi.fn(), affected = [{ lib: "l", cell: "consumer", view: "schematic" as const }];
    const result = render(<SymbolEditor {...props} onChanged={onChanged} />); await act(async () => {});
    vi.mocked(api.symbolEdit).mockResolvedValueOnce({ result: [], view: data, affected });
    await act(async () => fireEvent.keyDown(result.container.querySelector(".canvaswrap")!, { key: "z", ctrlKey: true, shiftKey: op === "redo" }));
    expect(onChanged).toHaveBeenCalledWith(affected); expect(api.symbolView).toHaveBeenCalledTimes(1);
  });
  it("reports a successful pin edit to the latest onChanged callback", async () => {
    data.objects = [pin()]; const oldChanged = vi.fn(), newChanged = vi.fn();
    const result = render(<SymbolEditor {...props} onChanged={oldChanged} />); await act(async () => {});
    const canvas = result.container.querySelector(".canvaswrap") as HTMLElement; await click(canvas, 0, 0);
    let finish!: (value: Awaited<ReturnType<typeof api.symbolEdit>>) => void;
    vi.mocked(api.symbolEdit).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    await drag(canvas, 0, 0, 20, 20); result.rerender(<SymbolEditor {...props} onChanged={newChanged} />);
    const affected = [{ lib: "l", cell: "consumer", view: "schematic" as const }];
    await act(async () => finish({ result: ["p1"], view: data, affected }));
    expect(oldChanged).not.toHaveBeenCalled(); expect(newChanged).toHaveBeenCalledWith(affected);
  });
  it("does not report changed views when an edit is rejected", async () => {
    const onChanged = vi.fn(); const result = render(<SymbolEditor {...props} onChanged={onChanged} />); await act(async () => {});
    vi.mocked(api.symbolEdit).mockRejectedValueOnce(new Error("conflict"));
    await act(async () => fireEvent.keyDown(result.container.querySelector(".canvaswrap")!, { key: "z", ctrlKey: true }));
    expect(onChanged).not.toHaveBeenCalled();
  });
  it("survives StrictMode effect replay and draws its graphics", async () => {
    const frames = new Map<number, FrameRequestCallback>(); let next = 1;
    vi.stubGlobal("requestAnimationFrame", vi.fn((cb: FrameRequestCallback) => { const id = next++; frames.set(id, cb); return id; }));
    vi.stubGlobal("cancelAnimationFrame", vi.fn((id: number) => frames.delete(id)));
    const ctx = { setTransform: vi.fn(), fillRect: vi.fn(), beginPath: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(), stroke: vi.fn() };
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(ctx as unknown as CanvasRenderingContext2D);
    data.lines = [[4, 0, 0, 60, 30]];
    render(<StrictMode><SymbolEditor {...props} /></StrictMode>); await act(async () => {});
    expect(frames.size).toBeGreaterThan(0);
    await act(async () => { for (const [id, cb] of [...frames]) { frames.delete(id); cb(0); } });
    expect(ctx.lineTo).toHaveBeenCalledWith(60, 30);
  });
});

describe("symbol picking", () => {
  it("picks a pin over a body edge and rejects clicks away from thin strokes", () => {
    expect(hitSymbolObject([line, pin()], 0, 0, 5)?.kind).toBe("pin");
    expect(hitSymbolObject([line], 30, 0, 5)).toBeUndefined();
    expect(hitSymbolObject([line], 30, 23, 5)?.id).toBe("l1");
  });
});
