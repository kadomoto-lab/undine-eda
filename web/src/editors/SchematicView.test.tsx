// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, Schematic, SchEditBody } from "../api";
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
  wires: [], insts: [], missing: [], props: {}, undo: 1, redo: 1,
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
  const cam = new Camera(false);
  saveView(viewKey("p", "l", "c", "schematic"), cam);
  data = { ...blank };
  vi.mocked(api.schematic).mockImplementation(async () => data);
  vi.mocked(api.schRoute).mockImplementation(async (_p, _l, _c, b) => ({ points: [[b.x, b.y], [b.x2, b.y2]] }));
  vi.mocked(api.schMovePreview).mockImplementation(async () => ({ result: [], view: data }));
  vi.mocked(api.schEdit).mockImplementation(async (_p, _l, _c, body) => {
    if (body.op === "route_wire") data = { ...data, wires: [...data.wires, [body.x!, body.y!, body.x2!, body.y2!, false, "", `w${data.wires.length}`]] };
    return { result: [], view: data };
  });
  vi.mocked(api.schInstance).mockResolvedValue({ symbol: "r.sym", props: {}, template: {}, type: "resistor" });
  await setLocale("en");
});

afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });

async function open(readonly = false) {
  const rendered = render(<SchematicView project="p" lib="l" cell="c" view="schematic" active readonly={readonly}
    hasLayout={false} probe={null} onProbe={() => {}} onOpenSymbol={() => {}} onOpenLayout={() => {}} toast={toast} />);
  await act(async () => {});
  return rendered.container.querySelector(".canvaswrap") as HTMLElement;
}

async function click(canvas: HTMLElement, x: number, y: number, button = 0, detail = 1) {
  await act(async () => {
    fireEvent.mouseDown(canvas, { clientX: x + 400, clientY: y + 300, button, detail });
    fireEvent.mouseUp(canvas, { clientX: x + 400, clientY: y + 300, button, detail });
  });
}

function edits(): SchEditBody[] { return vi.mocked(api.schEdit).mock.calls.map((c) => c[3]); }
function wireTool() { fireEvent.click(screen.getByTitle(t("editor:schematic.wireShortcut"))); }

function resistor(id = "R1", x = 0): Schematic["insts"][number] {
  return { i: 0, id, name: id, symbol: "r.sym", type: "resistor", x, y: 0, rot: 0, flip: 0,
    bbox: [x - 10, -10, x + 10, 10], props: { name: id, value: "1k" } };
}

describe("schematic canvas lifecycle", () => {
  const props = { project: "p", lib: "l", cell: "c", view: "schematic" as const, active: true, readonly: false,
    hasLayout: false, probe: null, onProbe: () => {}, onOpenSymbol: () => {}, onOpenLayout: () => {}, toast };

  it("reloads after tab activation without a duplicate initial request or refitting the camera", async () => {
    data = { ...data, insts: [resistor()] };
    const result = render(<SchematicView {...props} />); await act(async () => {});
    expect(api.schematic).toHaveBeenCalledTimes(1);
    result.rerender(<SchematicView {...props} active={false} />);
    data = { ...data, bbox: [1000, 1000, 4000, 4000], insts: [resistor("R2", 100)] };
    result.rerender(<SchematicView {...props} />); await act(async () => {});
    expect(api.schematic).toHaveBeenCalledTimes(2);
    await click(result.container.querySelector(".canvaswrap")!, 100, 0);
    expect(api.schInstance).toHaveBeenLastCalledWith("p", "l", "c", "R2");
  });

  it("does not launch an activation refresh before initial loading finishes", async () => {
    let finish!: (view: Schematic) => void;
    vi.mocked(api.schematic).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    const result = render(<SchematicView {...props} active={false} />);
    result.rerender(<SchematicView {...props} />); await act(async () => finish(data));
    expect(api.schematic).toHaveBeenCalledTimes(1);
  });

  it("rejects an older activation read after a local edit completes", async () => {
    data = { ...data, insts: [resistor()] }; const old = data;
    const result = render(<SchematicView {...props} />); await act(async () => {});
    result.rerender(<SchematicView {...props} active={false} />);
    let finish!: (view: Schematic) => void;
    vi.mocked(api.schematic).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    result.rerender(<SchematicView {...props} />);
    const canvas = result.container.querySelector(".canvaswrap") as HTMLElement;
    await click(canvas, 0, 0);
    vi.mocked(api.schEdit).mockResolvedValueOnce({ result: [], view: { ...data, insts: [] } });
    await act(async () => fireEvent.keyDown(canvas, { key: "Delete" }));
    await act(async () => finish(old));
    expect(screen.getByText(t("editor:schematic.counts", { instances: 0, wires: 0 }))).toBeTruthy();
  });

  it("does not replace an unfinished wire with an activation read", async () => {
    const result = render(<SchematicView {...props} />); await act(async () => {});
    result.rerender(<SchematicView {...props} active={false} />);
    let finish!: (view: Schematic) => void;
    vi.mocked(api.schematic).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    result.rerender(<SchematicView {...props} />);
    const canvas = result.container.querySelector(".canvaswrap") as HTMLElement;
    wireTool(); await click(canvas, 20, 30);
    await act(async () => finish({ ...data, insts: [resistor("STALE")] }));
    await click(canvas, 80, 70);
    expect(edits()).toEqual([{ op: "route_wire", x: 20, y: 30, x2: 80, y2: 70 }]);
  });

  it("leaves a committing move intact when its tab is reactivated", async () => {
    data = { ...data, insts: [resistor()] };
    const result = render(<SchematicView {...props} />); await act(async () => {});
    const canvas = result.container.querySelector(".canvaswrap") as HTMLElement; await click(canvas, 0, 0);
    let finish!: (result: Awaited<ReturnType<typeof api.schEdit>>) => void;
    vi.mocked(api.schEdit).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    fireEvent.mouseDown(canvas, { clientX: 400, clientY: 300, button: 0 });
    fireEvent.mouseMove(canvas, { clientX: 440, clientY: 300, buttons: 1 });
    await act(async () => fireEvent.mouseUp(canvas, { clientX: 440, clientY: 300, button: 0 }));
    result.rerender(<SchematicView {...props} active={false} />); result.rerender(<SchematicView {...props} />);
    expect(api.schematic).toHaveBeenCalledTimes(1);
    await act(async () => finish({ result: ["R1"], view: { ...data, insts: [resistor("R1", 40)] } }));
    expect(edits()).toEqual([{ op: "move", items: [{ k: "i", id: "R1" }], dx: 40, dy: 0 }]);
    expect((screen.getByTitle(t("editor:schematic.moveShortcut")) as HTMLButtonElement).disabled).toBe(false);
  });

  it("ignores activation results after switching cells", async () => {
    const result = render(<SchematicView {...props} />); await act(async () => {});
    result.rerender(<SchematicView {...props} active={false} />);
    let finish!: (view: Schematic) => void;
    vi.mocked(api.schematic).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    result.rerender(<SchematicView {...props} />);
    vi.mocked(api.schematic).mockResolvedValueOnce({ ...data, insts: [resistor("NEW")] });
    result.rerender(<SchematicView {...props} cell="new" />); await act(async () => {});
    await act(async () => finish(data));
    expect(screen.getByText(t("editor:schematic.counts", { instances: 1, wires: 0 }))).toBeTruthy();
  });

  it("reports affected symbols after schematic Undo using the latest callback", async () => {
    const before = vi.fn(), after = vi.fn();
    const result = render(<SchematicView {...props} onChanged={before} />); await act(async () => {});
    result.rerender(<SchematicView {...props} onChanged={after} />);
    const affected = [{ lib: "l", cell: "device", view: "symbol" as const }];
    vi.mocked(api.schEdit).mockResolvedValueOnce({ result: [], view: data, affected });
    await act(async () => fireEvent.keyDown(result.container.querySelector(".canvaswrap")!, { key: "z", ctrlKey: true }));
    expect(before).not.toHaveBeenCalled(); expect(after).toHaveBeenCalledWith(affected);
  });

  it.each(["schematic", "symbol"] as const)("draws the %s after StrictMode replays its effects", async (view) => {
    const frames = new Map<number, FrameRequestCallback>();
    let nextFrame = 1;
    vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => {
      const id = nextFrame++;
      frames.set(id, callback);
      return id;
    }));
    vi.stubGlobal("cancelAnimationFrame", vi.fn((id: number) => { frames.delete(id); }));
    const context = {
      setTransform: vi.fn(), fillRect: vi.fn(), beginPath: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(), stroke: vi.fn(),
    };
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(context as unknown as CanvasRenderingContext2D);
    data = { ...data, kind: view, lines: [[4, 0, 0, 60, 30]] };

    render(<StrictMode><SchematicView project="p" lib="l" cell="c" view={view} active readonly={false}
      hasLayout={false} probe={null} onProbe={() => {}} onOpenSymbol={() => {}} onOpenLayout={() => {}} toast={toast} /></StrictMode>);
    await act(async () => {});
    expect(cancelAnimationFrame).toHaveBeenCalled();
    expect(frames.size).toBeGreaterThan(0);
    await act(async () => {
      for (const [id, callback] of [...frames]) {
        frames.delete(id);
        callback(0);
      }
    });
    expect(context.moveTo).toHaveBeenCalledWith(0, 0);
    expect(context.lineTo).toHaveBeenCalledWith(60, 30);
    expect(context.stroke).toHaveBeenCalled();
  });
});

describe("schematic wire interactions", () => {
  it("continues after an empty corner and finishes after one click on an off-grid terminal", async () => {
    data = { ...data, terminals: [["R1", "p", 3.5, 4.5], ["R2", "p", 103.5, 84.5]] };
    const canvas = await open();
    wireTool();
    await click(canvas, 5, 5);
    await click(canvas, 50, 40);
    await click(canvas, 104, 85);
    await click(canvas, 160, 100); // A new anchor, not an extension from R2.
    expect(edits()).toEqual([
      { op: "route_wire", x: 3.5, y: 4.5, x2: 50, y2: 40 },
      { op: "route_wire", x: 50, y: 40, x2: 103.5, y2: 84.5 },
    ]);
    await click(canvas, 190, 130);
    expect(edits().at(-1)).toEqual({ op: "route_wire", x: 160, y: 100, x2: 190, y2: 130 });
  });

  it("finishes on an existing wire interior", async () => {
    data = { ...data, wires: [[100, 0, 100, 100, false, "out", "w0"]] };
    const canvas = await open();
    wireTool();
    await click(canvas, 0, 40);
    await click(canvas, 98, 43);
    await click(canvas, 150, 50);
    expect(edits()).toEqual([{ op: "route_wire", x: 0, y: 40, x2: 100, y2: 40 }]);
  });

  it("keeps the previous anchor when a route fails", async () => {
    const canvas = await open();
    wireTool();
    await click(canvas, 0, 0);
    vi.mocked(api.schEdit).mockRejectedValueOnce(new Error("No route"));
    await click(canvas, 50, 50);
    await click(canvas, 90, 90);
    expect(edits().at(-1)).toEqual({ op: "route_wire", x: 0, y: 0, x2: 90, y2: 90 });
    expect(toast).toHaveBeenCalledOnce();
  });

  it("guards rapid clicks and does not resurrect an anchor cancelled during commit", async () => {
    const canvas = await open();
    wireTool();
    await click(canvas, 0, 0);
    let finish!: (value: { result: unknown; view: Schematic }) => void;
    vi.mocked(api.schEdit).mockImplementationOnce(() => new Promise((r) => { finish = r; }));
    await click(canvas, 50, 50);
    await click(canvas, 90, 90);
    expect(edits()).toHaveLength(1);
    fireEvent.keyDown(canvas, { key: "Escape" });
    await act(async () => { finish({ result: [], view: data }); });
    await click(canvas, 120, 120);
    expect(edits()).toHaveLength(1);
    await click(canvas, 170, 150);
    expect(edits().at(-1)).toEqual({ op: "route_wire", x: 120, y: 120, x2: 170, y2: 150 });
  });

  it("does not start or extend wires with middle/right mouse or stationary space-pan", async () => {
    const canvas = await open();
    wireTool();
    await click(canvas, 0, 0, 2);
    await click(canvas, 30, 30, 1);
    fireEvent.keyDown(canvas, { key: " " });
    await click(canvas, 50, 50);
    fireEvent.keyUp(window, { key: " " });
    await click(canvas, 90, 90);
    expect(edits()).toHaveLength(0);
    await click(canvas, 120, 120);
    expect(edits()).toEqual([{ op: "route_wire", x: 90, y: 90, x2: 120, y2: 120 }]);
  });

  it("double-click ends a wire without restarting at its completed terminal", async () => {
    data = { ...data, terminals: [["R1", "p", 50, 50]] };
    const canvas = await open();
    wireTool();
    await click(canvas, 0, 0);
    await click(canvas, 50, 50);
    await click(canvas, 50, 50, 0, 2);
    fireEvent.doubleClick(canvas, { clientX: 450, clientY: 350, button: 0 });
    await click(canvas, 100, 100);
    expect(edits()).toHaveLength(1);
    await click(canvas, 140, 120);
    expect(edits().at(-1)).toEqual({ op: "route_wire", x: 100, y: 100, x2: 140, y2: 120 });
  });

  it("ignores release after Escape, blur, or leaving the canvas", async () => {
    const canvas = await open();
    wireTool();
    for (const cancel of [() => fireEvent.keyDown(canvas, { key: "Escape" }), () => fireEvent.blur(window), () => fireEvent.mouseLeave(canvas)]) {
      wireTool();
      await click(canvas, 0, 0);
      fireEvent.mouseDown(canvas, { clientX: 450, clientY: 350, button: 0 });
      cancel();
      fireEvent.mouseUp(canvas, { clientX: 450, clientY: 350, button: 0 });
      expect(edits()).toHaveLength(0);
    }
  });
});

describe("schematic selection", () => {
  it("retains a selected device in a read-only view", async () => {
    data = { ...data, insts: [resistor()] };
    const canvas = await open(true);
    await click(canvas, 0, 0);
    expect(api.schInstance).toHaveBeenCalledWith("p", "l", "c", "R1");
    expect(screen.getByText("r.sym")).toBeTruthy();
    expect(edits()).toHaveLength(0);
  });

  it("keeps all resulting segments selected when moving a wire reroutes it", async () => {
    data = { ...data, wires: [[0, 0, 100, 0, false, "", "w0"]] };
    const canvas = await open();
    vi.mocked(api.schEdit).mockImplementationOnce(async () => ({ result: ["w1", "w2"], view: {
      ...data, wires: [[0, 20, 50, 20, false, "", "w1"], [50, 20, 100, 40, false, "", "w2"]],
    } }));
    fireEvent.keyDown(canvas, { key: "m" });
    fireEvent.mouseDown(canvas, { clientX: 440, clientY: 300, button: 0 });
    fireEvent.mouseMove(canvas, { clientX: 460, clientY: 320, buttons: 1 });
    await act(async () => { fireEvent.mouseUp(canvas, { clientX: 460, clientY: 320, button: 0 }); });
    expect(screen.getByText(t("editor:selection.count", { count: 2 }))).toBeTruthy();
    await act(async () => { fireEvent.keyDown(canvas, { key: "Delete" }); });
    expect(edits().at(-1)).toEqual({ op: "delete", items: [{ k: "w", id: "w1" }, { k: "w", id: "w2" }] });
  });

  it("refreshes property fields after an edit and undo", async () => {
    const instance = resistor();
    data = { ...data, insts: [instance] };
    vi.mocked(api.schInstance).mockImplementation(async () => ({ symbol: "r.sym", props: data.insts[0].props, template: {}, type: "resistor" }));
    vi.mocked(api.schEdit).mockImplementation(async (_p, _l, _c, body) => {
      data = { ...data, insts: [{ ...instance, props: { name: "R1", value: body.op === "undo" ? "1k" : "2000" } }] };
      return { result: "R1", view: data };
    });
    const canvas = await open();
    await click(canvas, 0, 0);
    const input = screen.getByDisplayValue("1k");
    fireEvent.change(input, { target: { value: "2k" } });
    await act(async () => { fireEvent.blur(input); });
    expect(screen.getByDisplayValue("2000")).toBeTruthy();
    await act(async () => { fireEvent.click(screen.getByTitle(t("editor:action.undoShortcut"))); });
    await click(canvas, 0, 0);
    await waitFor(() => expect(screen.getByDisplayValue("1k")).toBeTruthy());
  });

  it("discards a late detail response for the previously selected device", async () => {
    data = { ...data, insts: [resistor(), resistor("R2", 100)] };
    const resolvers: ((value: Awaited<ReturnType<typeof api.schInstance>>) => void)[] = [];
    vi.mocked(api.schInstance).mockImplementation(() => new Promise((r) => resolvers.push(r)));
    const canvas = await open();
    await click(canvas, 0, 0);
    await click(canvas, 100, 0);
    await act(async () => { resolvers[1]({ symbol: "r.sym", props: { value: "2k" }, template: {}, type: "resistor" }); });
    await act(async () => { resolvers[0]({ symbol: "r.sym", props: { value: "old-1k" }, template: {}, type: "resistor" }); });
    expect(screen.getByDisplayValue("2k")).toBeTruthy();
    expect(screen.queryByDisplayValue("old-1k")).toBeNull();
  });
});

describe("schematic drag preview", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    data = { ...data, insts: [resistor()], lines: [[4, -10, 0, 10, 0]], wires: [[0, 10, 100, 10, false, "", "w0"]] };
  });

  function drawing() {
    const frames = new Map<number, FrameRequestCallback>();
    let id = 0;
    vi.stubGlobal("requestAnimationFrame", vi.fn((callback: FrameRequestCallback) => { frames.set(++id, callback); return id; }));
    vi.stubGlobal("cancelAnimationFrame", vi.fn((key: number) => { frames.delete(key); }));
    const context = {
      setTransform: vi.fn(), fillRect: vi.fn(), beginPath: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(), stroke: vi.fn(),
      strokeRect: vi.fn(), setLineDash: vi.fn(), arc: vi.fn(), fill: vi.fn(),
    };
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(context as unknown as CanvasRenderingContext2D);
    return { context, flush: () => {
      for (const [key, callback] of [...frames]) { frames.delete(key); callback(0); }
    } };
  }

  function startDrag(canvas: HTMLElement, dx = 40, dy = 30) {
    fireEvent.keyDown(canvas, { key: "m" });
    fireEvent.mouseDown(canvas, { clientX: 400, clientY: 300, button: 0 });
    fireEvent.mouseMove(canvas, { clientX: 400 + dx, clientY: 300 + dy, buttons: 1 });
  }

  function moved(dx = 40, dy = 30): Schematic {
    return { ...data, insts: [{ ...resistor("R1", dx), y: dy, bbox: [dx - 10, dy - 10, dx + 10, dy + 10] }],
      lines: [[4, dx - 10, dy, dx + 10, dy]],
      wires: [[dx, dy + 10, dx, 70, false, "", "w0"], [dx, 70, 100, 70, false, "", "w1"]] };
  }

  it("draws both the moved device and rerouted wires while the mouse is held without editing", async () => {
    const { context, flush } = drawing();
    const preview = moved();
    vi.mocked(api.schMovePreview).mockResolvedValue({ result: ["R1"], view: preview });
    const canvas = await open();
    flush();
    context.moveTo.mockClear(); context.lineTo.mockClear();
    startDrag(canvas);
    await act(async () => { await vi.advanceTimersByTimeAsync(30); });
    flush();
    fireEvent.keyDown(canvas, { key: "Delete" });
    fireEvent.keyDown(canvas, { key: "r" });
    fireEvent.keyDown(canvas, { key: "z", ctrlKey: true });
    expect(api.schMovePreview).toHaveBeenCalledWith("p", "l", "c", { items: [{ k: "i", id: "R1" }], dx: 40, dy: 30 });
    expect(edits()).toHaveLength(0);
    expect(context.moveTo).toHaveBeenCalledWith(30, 30); // Full symbol primitives, not just its bounding box.
    expect(context.moveTo).toHaveBeenCalledWith(40, 40);
    expect(context.lineTo).toHaveBeenCalledWith(40, 70);
    expect(context.lineTo).toHaveBeenCalledWith(100, 70);
    expect(context.moveTo).not.toHaveBeenCalledWith(-10, 0);
    expect(screen.getByRole("status").textContent).toBe(t("editor:schematic.move.ready"));
  });

  it("keeps the last preview until one atomic commit at the release position and ignores a late preview", async () => {
    const { context, flush } = drawing();
    let finishPreview!: (value: Awaited<ReturnType<typeof api.schMovePreview>>) => void;
    let finishEdit!: (value: Awaited<ReturnType<typeof api.schEdit>>) => void;
    const preview = moved(), committed = moved(80, 60);
    vi.mocked(api.schMovePreview).mockResolvedValueOnce({ result: ["R1"], view: preview })
      .mockImplementationOnce(() => new Promise((resolve) => { finishPreview = resolve; }));
    vi.mocked(api.schEdit).mockImplementationOnce(() => new Promise((resolve) => { finishEdit = resolve; }));
    const canvas = await open();
    flush();
    startDrag(canvas);
    await act(async () => { await vi.advanceTimersByTimeAsync(30); });
    fireEvent.mouseMove(canvas, { clientX: 460, clientY: 350, buttons: 1 });
    await act(async () => { await vi.advanceTimersByTimeAsync(30); });
    fireEvent.mouseUp(canvas, { clientX: 480, clientY: 360, button: 0 });
    fireEvent.mouseUp(canvas, { clientX: 480, clientY: 360, button: 0 });
    expect(edits()).toEqual([{ op: "move", items: [{ k: "i", id: "R1" }], dx: 80, dy: 60 }]);
    expect(screen.getByRole("status").textContent).toBe(t("editor:schematic.move.committing"));
    context.moveTo.mockClear();
    flush();
    expect(context.moveTo).toHaveBeenCalledWith(30, 30);
    await act(async () => { finishEdit({ result: ["R1"], view: committed }); });
    await act(async () => { finishPreview({ result: ["R1"], view: moved(60, 50) }); });
    context.moveTo.mockClear();
    flush();
    expect(context.moveTo).toHaveBeenCalledWith(70, 60);
    expect(context.moveTo).not.toHaveBeenCalledWith(50, 50);
    expect(screen.queryByRole("status")).toBeNull();
  });

  it.each(["Escape", "blur", "leave"])("cancels a held drag on %s without committing or restoring late geometry", async (cancel) => {
    let finish!: (value: Awaited<ReturnType<typeof api.schMovePreview>>) => void;
    vi.mocked(api.schMovePreview).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    const canvas = await open();
    startDrag(canvas);
    await act(async () => { await vi.advanceTimersByTimeAsync(30); });
    if (cancel === "Escape") fireEvent.keyDown(canvas, { key: "Escape" });
    else if (cancel === "blur") fireEvent.blur(window);
    else fireEvent.mouseLeave(canvas);
    await act(async () => { finish({ result: ["R1"], view: moved() }); });
    fireEvent.mouseUp(canvas, { clientX: 440, clientY: 330, button: 0 });
    expect(screen.queryByRole("status")).toBeNull();
    expect(edits()).toHaveLength(0);
  });

  it.each(["readonly", "cell", "unmount"])("invalidates a pending preview on %s changes", async (change) => {
    let finish!: (value: Awaited<ReturnType<typeof api.schMovePreview>>) => void;
    vi.mocked(api.schMovePreview).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    const props = { project: "p", lib: "l", cell: "c", view: "schematic" as const, active: true, readonly: false,
      hasLayout: false, probe: null, onProbe: () => {}, onOpenSymbol: () => {}, onOpenLayout: () => {}, toast };
    const rendered = render(<SchematicView {...props} />);
    await act(async () => {});
    const canvas = rendered.container.querySelector(".canvaswrap") as HTMLElement;
    startDrag(canvas);
    await act(async () => { await vi.advanceTimersByTimeAsync(30); });
    if (change === "unmount") rendered.unmount();
    else rendered.rerender(<SchematicView {...props} readonly={change === "readonly"} cell={change === "cell" ? "other" : "c"} />);
    await act(async () => { finish({ result: ["R1"], view: moved() }); });
    expect(screen.queryByRole("status")).toBeNull();
    expect(edits()).toHaveLength(0);
  });

  it("does not request a move preview or edit in a read-only view", async () => {
    const canvas = await open(true);
    startDrag(canvas);
    await act(async () => { await vi.advanceTimersByTimeAsync(60); });
    fireEvent.mouseUp(canvas, { clientX: 440, clientY: 330, button: 0 });
    expect(api.schMovePreview).not.toHaveBeenCalled();
    expect(edits()).toHaveLength(0);
  });
});
