// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, type SymbolAffectedView, type ViewKind } from "./api";
import { setLocale, t } from "./i18n";
import { App } from "./App";

const state = vi.hoisted(() => ({ mounts: {} as Record<string, number> }));
type EditorProps = { lib: string; cell: string; onChanged?: (affected: SymbolAffectedView[]) => void };
vi.mock("./editors/SymbolEditor", async () => {
  const { useEffect, useState } = await import("react");
  return { SymbolEditor: ({ lib, cell, onChanged }: EditorProps) => {
    const key = `${lib}/${cell}/symbol`, [count, setCount] = useState(0);
    useEffect(() => { state.mounts[key] = (state.mounts[key] ?? 0) + 1; }, []);
    return <button data-testid={key} onClick={() => {
      setCount((n) => n + 1);
      onChanged?.([{ lib: "l", cell: "source", view: "symbol" }, { lib: "l", cell: "consumer", view: "schematic" }, { lib: "l", cell: "consumer", view: "schematic" }]);
    }}>symbol state {count}</button>;
  } };
});
vi.mock("./editors/SchematicView", async () => {
  const { useEffect, useState } = await import("react");
  return { SchematicView: ({ lib, cell, onChanged }: EditorProps) => {
    const key = `${lib}/${cell}/schematic`, [count, setCount] = useState(0);
    useEffect(() => { state.mounts[key] = (state.mounts[key] ?? 0) + 1; }, []);
    return <button data-testid={key} onClick={() => {
      setCount((n) => n + 1);
      onChanged?.([{ lib: "l", cell: "source", view: "symbol" }, { lib: "l", cell: "consumer", view: "schematic" }]);
    }}>schematic state {count}</button>;
  } };
});
vi.mock("./layout", () => ({ Splitter: () => null, RowSplitter: () => null, TabStrip: ({ children }: { children: React.ReactNode }) => children }));
vi.mock("./editors/LayoutEditor", () => ({ LayoutEditor: () => null }));
vi.mock("./panels/ChatPanel", () => ({ ChatPanel: () => null }));
vi.mock("./panels/ChangesPanel", () => ({ ChangesPanel: () => null }));
vi.mock("./panels/SymbolCheckPanel", () => ({ SymbolCheckPanel: () => null }));
vi.mock("./api", async (original) => ({ ...await original<typeof import("./api")>(), api: {
  projects: vi.fn(async () => [{ name: "p", pdk: null, pdk_requested: null }]),
  pdks: vi.fn(async () => []),
  project: vi.fn(async () => ({ name: "p", pdk: null, pdk_requested: null, libraries: [
    { name: "l", readonly: false, origin: "project", has_layout: false }, { name: "otherlib", readonly: false, origin: "project", has_layout: false },
  ] })),
  cells: vi.fn(async () => [{ name: "source", views: ["symbol", "schematic"], top: false }, { name: "consumer", views: ["schematic"], top: true }, { name: "other", views: ["schematic"], top: false }]),
  changesets: vi.fn(async () => []), live: vi.fn(async () => []),
  deps: vi.fn(async () => ({ format: 1, libraries: [], tools: [], issues: [], warnings: [], notes: [], ok: true,
    pdk: { requested: null, locked: null, commit: null, installed: null, state: "none", changed: [], verified_by: null, ok: true } })),
  gitStatus: vi.fn(async () => ({ repo: false, setup: null, prefix: null, branch: null, head: null, operation: null, conflicted: [], cells: [], other: [], unsaved: [] })),
  importFile: vi.fn(async (_project: string, _lib: string, _path: string, view: ViewKind) => ({ new_cells: ["imported"], view })),
} }));

beforeEach(async () => {
  vi.clearAllMocks();
  state.mounts = {};
  location.hash = "#/p/p?chat=0";
  const tabs = [
    { id: "l/source/symbol", lib: "l", cell: "source", view: "symbol" },
    { id: "l/consumer/schematic", lib: "l", cell: "consumer", view: "schematic" },
    { id: "l/other/schematic", lib: "l", cell: "other", view: "schematic" },
    { id: "otherlib/consumer/schematic", lib: "otherlib", cell: "consumer", view: "schematic" },
  ];
  const storage = new Map([["undine.tabs.p", JSON.stringify({ tabs, active: "l/source/symbol" })]]);
  vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key, value) });
  await setLocale("en");
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("refreshes affected consumers while preserving the current symbol and unrelated tabs", async () => {
  render(<App />); await act(async () => {});
  expect(state.mounts).toEqual({ "l/source/symbol": 1, "l/consumer/schematic": 1, "l/other/schematic": 1, "otherlib/consumer/schematic": 1 });
  await act(async () => fireEvent.click(screen.getByTestId("l/source/symbol")));
  expect(state.mounts).toEqual({ "l/source/symbol": 1, "l/consumer/schematic": 2, "l/other/schematic": 1, "otherlib/consumer/schematic": 1 });
  expect(screen.getByTestId("l/source/symbol").textContent).toBe("symbol state 1");
});

it("refreshes the symbol after Undo initiated in its consumer without remounting that consumer", async () => {
  render(<App />); await act(async () => {});
  await act(async () => fireEvent.click(screen.getByTestId("l/consumer/schematic")));
  expect(state.mounts).toEqual({ "l/source/symbol": 2, "l/consumer/schematic": 1, "l/other/schematic": 1, "otherlib/consumer/schematic": 1 });
  expect(screen.getByTestId("l/consumer/schematic").textContent).toBe("schematic state 1");
});

it("places New Cell before Import Cell in the library toolbar", async () => {
  render(<App />); await act(async () => {});
  const newCell = screen.getAllByTitle(t("app:cell.newHint"))[0], importCell = screen.getAllByTitle(t("app:import.hintButton"))[0];
  expect(newCell.nextElementSibling).toBe(importCell);
});

it("updates import hints by view without losing the typed path", async () => {
  render(<App />); await act(async () => {});
  fireEvent.click(screen.getAllByTitle(t("app:import.hintButton"))[0]);
  const view = screen.getByRole("combobox", { name: t("app:import.view") });
  const path = screen.getByRole("textbox", { name: /File on the server/ }) as HTMLInputElement;
  expect((view as HTMLSelectElement).value).toBe("layout");
  expect(screen.getByText(t("app:import.hint.layout"))).toBeTruthy();
  fireEvent.change(path, { target: { value: "~/work/amp.sch" } });
  fireEvent.change(view, { target: { value: "schematic" } });
  expect(path.value).toBe("~/work/amp.sch"); expect(screen.getByText(t("app:import.hint.schematic"))).toBeTruthy();
  fireEvent.change(view, { target: { value: "symbol" } });
  expect(path.value).toBe("~/work/amp.sch"); expect(screen.getByText(t("app:import.hint.symbol"))).toBeTruthy();
});

it.each([['layout', 'gds'], ['layout', 'oas'], ['layout', 'oasis'], ['schematic', 'sch'], ['symbol', 'sym']] as const)(
  "imports a %s view from .%s and refreshes the library", async (view, extension) => {
    render(<App />); await act(async () => {});
    fireEvent.click(screen.getAllByTitle(t("app:import.hintButton"))[0]);
    fireEvent.change(screen.getByRole("combobox", { name: t("app:import.view") }), { target: { value: view } });
    fireEvent.change(screen.getByRole("textbox", { name: /File on the server/ }), { target: { value: ` ~/work/amp.${extension} ` } });
    const before = vi.mocked(api.cells).mock.calls.length;
    await act(async () => fireEvent.click(screen.getByRole("button", { name: t("app:import.action") })));
    expect(api.importFile).toHaveBeenCalledWith("p", "l", `~/work/amp.${extension}`, view);
    expect(vi.mocked(api.cells).mock.calls.length).toBeGreaterThan(before);
    expect(screen.queryByRole("dialog")).toBeNull();
  },
);

it("blocks mismatched file types and translates the selected view's error", async () => {
  await setLocale("ja"); render(<App />); await act(async () => {});
  fireEvent.click(screen.getAllByTitle(t("app:import.hintButton"))[0]);
  fireEvent.change(screen.getByRole("combobox", { name: t("app:import.view") }), { target: { value: "symbol" } });
  fireEvent.change(screen.getByRole("textbox", { name: /サーバー上のファイル/ }), { target: { value: "~/amp.sch" } });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: t("app:import.action") })));
  expect(screen.getByText(t("app:import.format.symbol"))).toBeTruthy(); expect(api.importFile).not.toHaveBeenCalled();
});
