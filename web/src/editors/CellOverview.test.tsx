// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type CellOverview as Overview } from "../api";
import { setLocale } from "../i18n";
import { CellOverview } from "./CellOverview";

vi.mock("../api", async (original) => ({ ...await original<typeof import("../api")>(), api: { overview: vi.fn(), netlist: vi.fn(), flows: vi.fn(), schematic: vi.fn() } }));
vi.mock("../panels/LibreLanePanel", () => ({ LibreLanePanel: ({ source, ports }: { source?: string; ports: string[] }) =>
  <div className="librelane-panel">{source}: {ports.join(", ")}</div> }));

const overview: Overview = {
  lib: "l", cell: "amp", exists: true, readonly: false,
  intent: {
    notes: "Drives a 2 pF load.", items: [
      { id: "g1", kind: "goal", text: "Low offset" },
      { id: "s1", kind: "spec", text: "Gain", spec: { measure: "gain_db", min: 40, testbench: "tb_amp" } },
    ], ports: 2, supplies: { vdd: "supply" }, views: { layout: 1 },
    problems: { error: 0, warning: 0, info: 1 }, missing: { simulation: 0, layout: 3 },
  },
  design: [
    { view: "schematic", role: "source", exists: true, intent: 0, notes: [] },
    { view: "symbol", role: "source", exists: true, intent: 0, fresh: false, notes: ["differs from the schematic's ports (1)"] },
    { view: "rtl", role: "source", exists: false, intent: 0, notes: [] },
    { view: "layout", role: "source", exists: false, intent: 1, notes: [] },
  ],
  tests: [{ lib: "l", cell: "tb_amp", why: ["measures s1", "places amp"], exists: true, intent: 0,
    simulation: { job: "j1", state: "done", fresh: false, changed: ["schematic netlist of l/tb_amp changed"] } }],
  used_in: [{ lib: "l", cell: "top", why: ["places amp"] }],
  derived: [{ view: "netlist", from: ["schematic"], available: true, on_demand: true }, { view: "checks", from: ["schematic"], checks: [] }],
};

const handlers = { onOpenView: vi.fn(), onOpenCell: vi.fn(), onCreate: vi.fn(async () => {}), toast: vi.fn() };
beforeEach(async () => {
  vi.resetAllMocks();
  vi.mocked(api.overview).mockResolvedValue(overview);
  vi.mocked(api.netlist).mockResolvedValue({ text: ".subckt amp in out\n.ends", warnings: [] } as never);
  vi.mocked(api.flows).mockResolvedValue({ flows: [], librelane: { available: false } });
  await setLocale("en");
});
afterEach(() => cleanup());

async function open() {
  render(<CellOverview project="p" lib="l" cell="amp" active readonly={false} tick={0} {...handlers} />);
  await act(async () => {});
}

describe("the cell page", () => {
  it("puts the cell intent on top, the views under it, and what is generated below the waterline", async () => {
    await open();
    const above = [...document.querySelectorAll(".cellsheet:not(.below *) .cellsechead h2")].map((h) => h.textContent);
    const below = [...document.querySelectorAll(".below .cellsechead h2")].map((h) => h.textContent);
    expect(above).toEqual(["What it has to do", "Views"]);
    expect(below).toEqual(["Netlist", "Checks"]);
    expect(document.querySelector(".waterline")!.textContent).toBe("Generated from the views above");
    expect(screen.getByText("Low offset")).toBeTruthy();
    expect(screen.getByText("Drives a 2 pF load.")).toBeTruthy();
    expect(document.querySelector(".cellready")!.textContent).toContain("Ready for simulation.");
    expect(document.querySelector(".cellready")!.textContent).toContain("Still needed for layout: 3.");
  });

  it("sums up the cell from what the server reports", async () => {
    await open();
    expect(document.querySelector(".cellsum")!.textContent).toBe("In l. Out of date: symbol.");
  });

  it("marks what is outdated and what is missing, as the server says", async () => {
    await open();
    const symbol = screen.getByText("Symbol").closest(".viewrow")!;
    expect(symbol.textContent).toContain("outdated");
    expect(symbol.textContent).toContain("differs from the schematic's ports");
    expect(screen.getByText("RTL").closest(".viewrow")!.textContent).toContain("not created");
    const tb = screen.getByText("tb_amp").closest(".viewrow")!;
    expect(tb.textContent).toContain("outdated");
    expect(tb.textContent).toContain("measures s1, places amp");
  });

  it("opens views, testbenches and the cells using it; creates a missing view", async () => {
    await open();
    fireEvent.click(screen.getByText("Schematic").closest(".viewrow")!.querySelector("button")!);
    expect(handlers.onOpenView).toHaveBeenCalledWith("schematic");
    fireEvent.click(screen.getByText("tb_amp").closest(".viewrow")!.querySelector("button")!);
    expect(handlers.onOpenCell).toHaveBeenCalledWith("l", "tb_amp", "schematic");
    fireEvent.click(screen.getByText("top"));
    expect(handlers.onOpenCell).toHaveBeenCalledWith("l", "top");
    await act(async () => { fireEvent.click(screen.getByText("Layout").closest(".viewrow")!.querySelector("button")!); });
    expect(handlers.onCreate).toHaveBeenCalledWith("layout");
  });

  it("shows the LibreLane panel of a flow that builds the cell from its schematic", async () => {
    vi.mocked(api.flows).mockResolvedValue({ flows: [{ name: "amp", target: "librelane", top: { lib: "l", cell: "amp" },
      source: "schematic", clock: { port: "none" }, floorplan: {}, variables: {} }], librelane: { available: false } });
    vi.mocked(api.schematic).mockResolvedValue({ ports: [["clk", "ipin"], ["q", "opin"]] } as never);
    await open();
    const below = [...document.querySelectorAll(".below .cellsechead h2")].map((h) => h.textContent);
    expect(below).toEqual(["Netlist", "Checks", "LibreLane"]);
    await act(async () => { fireEvent.click(screen.getByText("Show the flow")); });
    expect(api.schematic).toHaveBeenCalledWith("p", "l", "amp", "schematic");
    expect(document.querySelector(".librelane-panel")!.textContent).toBe("schematic: clk, q");
  });

  it("shows the netlist, a derived view, on demand", async () => {
    await open();
    await act(async () => { fireEvent.click(screen.getByText("Show")); });
    expect(api.netlist).toHaveBeenCalledWith("p", "l", "amp", "undine", "spice", "spice");
    expect(screen.getByText(/\.subckt amp/)).toBeTruthy();
  });
});
