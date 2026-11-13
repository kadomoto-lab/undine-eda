// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type Schematic, type SchEditBody, type SymbolPreview } from "../api";
import { DialogHost } from "../dialog";
import { setLocale, t } from "../i18n";
import { Camera, saveView, viewKey } from "./camera";
import { SchematicView } from "./SchematicView";

vi.mock("../App", () => ({ hashPanel: () => "props" }));
vi.mock("../layout", () => ({ Splitter: () => null }));
vi.mock("../api", async (original) => ({
  ...await original<typeof import("../api")>(),
  api: { schematic: vi.fn(), schRoute: vi.fn(), schMovePreview: vi.fn(), schEdit: vi.fn(), schInstance: vi.fn(), symbols: vi.fn(), symbol: vi.fn() },
}));

const blank: Schematic = {
  kind: "schematic", bbox: [-100, -100, 100, 100], colors: [], lines: [], boxes: [], polys: [], arcs: [], texts: [],
  wires: [], insts: [], missing: [], props: {}, undo: 1, redo: 0,
};
const labelSymbol: SymbolPreview = {
  ...blank, kind: "symbol", bbox: [-36, -9, 2, 12], props: { type: "label", template: "name=p1 lab=xxx" },
  pins: [["p", 0, 0]],
  texts: [{ s: "xxx", x: -7.5, y: -8.125, rot: 0, flip: 1, size: 17.16, layer: 3, hc: false, vc: false, bold: false, italic: false }],
};
let data: Schematic;
const toast = vi.fn();

beforeEach(async () => {
  vi.clearAllMocks();
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.stubGlobal("requestAnimationFrame", vi.fn(() => 1));
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.stubGlobal("localStorage", { getItem: () => null, setItem: () => {} });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, toJSON: () => ({}),
  });
  for (const cell of ["c", "other"]) saveView(viewKey("p", "l", cell, "schematic"), new Camera(false));
  data = { ...blank };
  vi.mocked(api.schematic).mockImplementation(async () => data);
  vi.mocked(api.symbol).mockResolvedValue(labelSymbol);
  vi.mocked(api.schEdit).mockImplementation(async () => ({ result: "p1", view: data }));
  vi.mocked(api.schInstance).mockResolvedValue({ symbol: "devices/lab_pin.sym", props: {}, template: {}, type: "label" });
  await setLocale("en");
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });

function editor(readonly = false, cell = "c") {
  return <><SchematicView project="p" lib="l" cell={cell} view="schematic" active readonly={readonly}
    hasLayout={false} probe={null} onProbe={() => {}} onOpenSymbol={() => {}} onOpenLayout={() => {}} toast={toast} /><DialogHost /></>;
}

async function open(readonly = false) {
  const rendered = render(editor(readonly));
  await act(async () => {});
  return { ...rendered, canvas: rendered.container.querySelector(".canvaswrap") as HTMLElement };
}

async function click(canvas: HTMLElement, x: number, y: number) {
  await act(async () => {
    fireEvent.mouseDown(canvas, { clientX: x + 400, clientY: y + 300, button: 0, detail: 1 });
    fireEvent.mouseUp(canvas, { clientX: x + 400, clientY: y + 300, button: 0, detail: 1 });
  });
}

async function start(name: string, canvas?: HTMLElement) {
  if (canvas) fireEvent.keyDown(canvas, { key: "l" });
  else fireEvent.click(screen.getByTitle(t("editor:schematic.placeLabelShortcut")));
  const dialog = screen.getByRole("dialog", { name: t("editor:schematic.newLabel") });
  fireEvent.change(within(dialog).getByRole("textbox", { name: t("editor:schematic.netName") }), { target: { value: name } });
  await act(async () => { fireEvent.click(within(dialog).getByRole("button", { name: t("editor:action.place") })); });
}

function edits(): SchEditBody[] { return vi.mocked(api.schEdit).mock.calls.map((call) => call[3]); }

describe("schematic net label placement", () => {
  it("asks for a nonempty name and cancels without creating an object", async () => {
    const { canvas } = await open();
    fireEvent.click(screen.getByTitle(t("editor:schematic.placeLabelShortcut")));
    const dialog = screen.getByRole("dialog", { name: t("editor:schematic.newLabel") });
    const input = within(dialog).getByRole("textbox", { name: t("editor:schematic.netName") });
    fireEvent.change(input, { target: { value: "   " } });
    await act(async () => { fireEvent.click(within(dialog).getByRole("button", { name: t("editor:action.place") })); });
    expect(screen.getByRole("dialog")).toBeTruthy();
    expect(within(dialog).getByText(t("common:validation.required"))).toBeTruthy();
    expect(api.symbol).not.toHaveBeenCalled();
    await act(async () => { fireEvent.keyDown(input, { key: "Escape" }); });
    expect(screen.queryByRole("dialog")).toBeNull();
    await click(canvas, 20, 20);
    expect(edits()).toEqual([]);
  });

  it("places trimmed electrical labels on off-grid pins and wire interiors, and supports repeated placement and Undo", async () => {
    data = { ...data, terminals: [["M1", "g", 3.5, 4.5]], wires: [[100.5, 0, 100.5, 100, false, "", "w0"]] };
    const { canvas } = await open();
    await start("  gate  ");
    expect(api.symbol).toHaveBeenCalledWith("p", "devices/lab_pin.sym");
    expect(edits()).toEqual([]);
    await click(canvas, 5, 5);
    await click(canvas, 98, 43);
    expect(edits()).toEqual([
      { op: "add_instance", sym: "devices/lab_pin.sym", x: 3.5, y: 4.5, rot: 0, flip: 0, props: { lab: "gate" } },
      { op: "add_instance", sym: "devices/lab_pin.sym", x: 100.5, y: 40, rot: 0, flip: 0, props: { lab: "gate" } },
    ]);
    await act(async () => { fireEvent.keyDown(canvas, { key: "u" }); });
    expect(edits().at(-1)).toEqual({ op: "undo" });
    fireEvent.keyDown(canvas, { key: "Escape" });
    await click(canvas, 170, 100);
    expect(edits()).toHaveLength(3);
  });

  it("opens with L and preserves the label name through rotation and mirror", async () => {
    const { canvas } = await open();
    await start("bias", canvas);
    fireEvent.keyDown(canvas, { key: "r" });
    fireEvent.keyDown(canvas, { key: "x" });
    await click(canvas, 21, 29);
    expect(edits()).toEqual([{ op: "add_instance", sym: "devices/lab_pin.sym", x: 20, y: 30, rot: 1, flip: 1, props: { lab: "bias" } }]);
  });

  it("paints the entered label at the pointer before it is placed", async () => {
    const frames = new Map<number, FrameRequestCallback>();
    let nextFrame = 1;
    vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => {
      const id = nextFrame++;
      frames.set(id, callback);
      return id;
    }));
    vi.stubGlobal("cancelAnimationFrame", vi.fn((id: number) => { frames.delete(id); }));
    const context = {
      setTransform: vi.fn(), fillRect: vi.fn(), save: vi.fn(), restore: vi.fn(), transform: vi.fn(),
      translate: vi.fn(), rotate: vi.fn(), measureText: vi.fn(() => ({ width: 48 })), fillText: vi.fn(),
    };
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(context as unknown as CanvasRenderingContext2D);
    const { canvas } = await open();
    await start("bias_gate", canvas);
    fireEvent.mouseMove(canvas, { clientX: 421, clientY: 329, buttons: 0 });
    await act(async () => {
      for (const [id, callback] of [...frames]) {
        frames.delete(id);
        callback(0);
      }
    });
    expect(context.transform).toHaveBeenCalledWith(1, 0, 0, 1, 20, 30);
    expect(context.fillText).toHaveBeenCalledWith("bias_gate", expect.any(Number), expect.any(Number));
    expect(context.fillText.mock.calls.some(([text]) => text === "xxx")).toBe(false);
    expect(edits()).toEqual([]);
  });

  it("uses an existing net suggestion without submitting the dialog", async () => {
    data = { ...data, pins: [["M1", "g", 0, 0, "bias"], ["M2", "g", 100, 0, "bias"]] };
    const { canvas } = await open();
    fireEvent.keyDown(canvas, { key: "l" });
    const dialog = screen.getByRole("dialog", { name: t("editor:schematic.newLabel") });
    await act(async () => { fireEvent.click(within(dialog).getByRole("button", { name: "bias" })); });
    expect((within(dialog).getByRole("textbox", { name: t("editor:schematic.netName") }) as HTMLInputElement).value).toBe("bias");
    expect(screen.getByRole("dialog")).toBe(dialog);
    expect(api.symbol).not.toHaveBeenCalled();
    expect(edits()).toEqual([]);
    await act(async () => { fireEvent.click(within(dialog).getByRole("button", { name: t("editor:action.place") })); });
    await click(canvas, 20, 30);
    expect(edits().at(-1)?.props).toEqual({ lab: "bias" });
  });

  it.each(["device", "wire"])("shows the attached label at the server preview position while dragging a %s, then restores it on cancellation", async (target) => {
    vi.useFakeTimers();
    const frames = new Map<number, FrameRequestCallback>();
    let nextFrame = 1;
    vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => {
      const id = nextFrame++;
      frames.set(id, callback);
      return id;
    }));
    vi.stubGlobal("cancelAnimationFrame", vi.fn((id: number) => { frames.delete(id); }));
    const context = {
      setTransform: vi.fn(), fillRect: vi.fn(), save: vi.fn(), restore: vi.fn(), transform: vi.fn(),
      translate: vi.fn(), rotate: vi.fn(), measureText: vi.fn(() => ({ width: 48 })), fillText: vi.fn(),
      beginPath: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(), stroke: vi.fn(), strokeRect: vi.fn(),
      setLineDash: vi.fn(), arc: vi.fn(), fill: vi.fn(),
    };
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(context as unknown as CanvasRenderingContext2D);
    const flush = () => { for (const [id, callback] of [...frames]) { frames.delete(id); callback(0); } };
    const label = { ...labelSymbol.texts[0], s: "bias", x: 50, y: 30, flip: 0 };
    const device: Schematic["insts"][number] = {
      i: 0, id: "R1", name: "R1", symbol: "r.sym", type: "resistor", x: 0, y: 0, rot: 0, flip: 0,
      bbox: [-10, -10, 10, 10], props: { name: "R1" },
    };
    data = { ...data, insts: [device], texts: [label], wires: [[0, 30, 100, 30, false, "", "w0"]] };
    vi.mocked(api.schMovePreview).mockResolvedValue({ result: [target === "device" ? "R1" : "w0"], view: {
      ...data, texts: [{ ...label, x: 80, y: 70 }],
      insts: [{ ...device, x: 40, y: 30, bbox: [30, 20, 50, 40] }],
      wires: [[40, 60, 80, 60, false, "", "w1"], [80, 60, 80, 100, false, "", "w2"]],
    } });
    const { canvas } = await open();
    flush();
    context.translate.mockClear(); context.fillText.mockClear();
    const [x, y] = target === "device" ? [0, 0] : [25, 30];
    fireEvent.keyDown(canvas, { key: "m" });
    fireEvent.mouseDown(canvas, { clientX: 400 + x, clientY: 300 + y, button: 0 });
    fireEvent.mouseMove(canvas, { clientX: 440 + x, clientY: 330 + y, buttons: 1 });
    await act(async () => { await vi.advanceTimersByTimeAsync(30); });
    flush();
    expect(api.schMovePreview).toHaveBeenCalledWith("p", "l", "c", {
      items: [{ k: target === "device" ? "i" : "w", id: target === "device" ? "R1" : "w0" }], dx: 40, dy: 30,
    });
    expect(context.translate).toHaveBeenCalledWith(80, 70);
    expect(context.translate).not.toHaveBeenCalledWith(50, 30);
    expect(context.fillText).toHaveBeenCalledWith("bias", expect.any(Number), expect.any(Number));
    expect(edits()).toEqual([]);
    context.translate.mockClear();
    fireEvent.keyDown(canvas, { key: "Escape" });
    fireEvent.mouseUp(canvas, { clientX: 440 + x, clientY: 330 + y, button: 0 });
    flush();
    expect(context.translate).toHaveBeenCalledWith(50, 30);
    expect(context.translate).not.toHaveBeenCalledWith(80, 70);
    expect(edits()).toEqual([]);
  });

  it("disables both the button and shortcut in a read-only schematic", async () => {
    const { canvas } = await open(true);
    expect((screen.getByTitle(t("editor:schematic.placeLabelShortcut")) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.keyDown(canvas, { key: "l" });
    expect(screen.queryByRole("dialog")).toBeNull();
    await click(canvas, 20, 20);
    expect(api.symbol).not.toHaveBeenCalled();
    expect(edits()).toEqual([]);
  });

  it.each(["Escape", "cell change", "read-only"])("ignores a label symbol that finishes loading after %s", async (cancel) => {
    let resolve!: (value: SymbolPreview) => void;
    vi.mocked(api.symbol).mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    const { canvas, rerender } = await open();
    await start("vdd", canvas);
    expect(screen.queryByRole("dialog")).toBeNull();
    if (cancel === "Escape") fireEvent.keyDown(canvas, { key: "Escape" });
    else rerender(editor(cancel === "read-only", cancel === "cell change" ? "other" : "c"));
    await act(async () => { resolve(labelSymbol); });
    if (cancel === "read-only") {
      rerender(editor());
      await act(async () => {});
    }
    await click(canvas, 30, 30);
    expect(edits()).toEqual([]);
  });
});
