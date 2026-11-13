// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, type CellInfo, type GitStatus } from "./api";
import { setLocale, t } from "./i18n";
import { App } from "./App";

// The library browser and the top bar show what git says changed since the last commit: a marker on the cell, the
// count on its library and in the top bar, and a cell whose layout has edits not saved to its file yet.
vi.mock("./editors/LayoutEditor", () => ({ LayoutEditor: () => null }));
vi.mock("./editors/SchematicView", () => ({ SchematicView: () => null }));
vi.mock("./editors/CellOverview", () => ({ CellOverview: () => null }));
vi.mock("./layout", () => ({ Splitter: () => null, RowSplitter: () => null, TabStrip: ({ children }: { children: React.ReactNode }) => children }));
vi.mock("./panels/ChatPanel", () => ({ ChatPanel: () => null }));
vi.mock("./panels/ChangesPanel", () => ({ ChangesPanel: () => null }));
vi.mock("./api", async (original) => ({ ...await original<typeof import("./api")>(), api: {
  projects: vi.fn(async () => [{ name: "p", pdk: null, pdk_requested: null }]),
  pdks: vi.fn(async () => []),
  project: vi.fn(async () => ({ name: "p", pdk: null, pdk_requested: null, libraries: [
    { name: "l", readonly: false, origin: "project", has_layout: true },
  ] })),
  cells: vi.fn(),
  changesets: vi.fn(async () => []), live: vi.fn(async () => []),
  deps: vi.fn(async () => ({ format: 1, libraries: [], tools: [], issues: [], warnings: [], notes: [], ok: true,
    pdk: { requested: null, locked: null, commit: null, installed: null, state: "none", changed: [], verified_by: null, ok: true } })),
  gitStatus: vi.fn(), gitLog: vi.fn(async () => ({ commits: [], more: false })), layoutReload: vi.fn(async () => ({})),
} }));

const cells: CellInfo[] = [
  { name: "amp", views: ["schematic", "layout"], top: false },
  { name: "inv", views: ["schematic", "layout"], top: false },
  { name: "ref", views: ["schematic"], top: false },
];

const status: GitStatus = {
  repo: true, setup: { merge: true, diff: true, hook: true }, prefix: "", branch: "feature/bias", operation: null, conflicted: [],
  head: { sha: "a".repeat(40), short: "aaaaaaa", parents: [], subject: "start", body: "", author: "Ada", email: "", date: "2026-10-28T00:00:00Z", refs: [] },
  cells: [{ lib: "l", cell: "amp", views: [
    { view: "schematic", path: "libs/l/amp.sch", status: "modified" },
    { view: "layout", path: "libs/l/amp.oas", status: "modified" },
  ] }],
  other: [], unsaved: [{ lib: "l", cells: ["inv"] }],
};

beforeEach(async () => {
  vi.clearAllMocks();
  vi.mocked(api.gitStatus).mockResolvedValue(status);
  vi.mocked(api.cells).mockResolvedValue(cells);
  location.hash = "#/p/p";
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key, value) });
  await setLocale("en");
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("marks the cells changed since the last commit and counts them", async () => {
  render(<App />);
  await act(async () => {});
  await act(async () => {});
  expect(api.gitStatus).toHaveBeenCalledWith("p");
  const amp = screen.getByText("amp").closest(".cellrow") as HTMLElement;
  expect(amp.querySelector(".gitmark.gm-changed")).toBeTruthy();
  expect(screen.getByLabelText(t("history:mark.changed", { views: "Schematic, Layout" }))).toBeTruthy();
  expect(screen.getByLabelText(t("history:mark.unsaved")).closest(".cellrow")?.textContent).toContain("inv");
  expect((screen.getByText("ref").closest(".cellrow") as HTMLElement).querySelector(".gitmarks")).toBeNull();
  expect(screen.getByTitle(t("history:changedCells", { count: 2 })).textContent).toBe("2");     // the library row
  expect(screen.getByText("feature/bias").closest("button")?.textContent).toContain("2");          // the top bar
});

it("names no branch for a project outside git", async () => {
  vi.mocked(api.gitStatus).mockResolvedValue({ repo: false, setup: null, prefix: null, branch: null, head: null, operation: null,
    conflicted: [], cells: [], other: [], unsaved: [] });
  render(<App />);
  await act(async () => {});
  await act(async () => {});
  expect(screen.getByText(t("history:button.noGit"))).toBeTruthy();
  expect(document.querySelector(".gitmarks")).toBeNull();
});

it("marks a library whose unsaved edits cancel out or whose files changed under them, and folded device cells that changed", async () => {
  vi.mocked(api.cells).mockResolvedValue([...cells, { name: "nfet_dev", views: ["layout"], top: false, device: true }]);
  vi.mocked(api.gitStatus).mockResolvedValue({ ...status, unsaved: [{ lib: "l", cells: [] }], stale: [{ lib: "l", cells: ["inv"] }],
    cells: [...status.cells!, { lib: "l", cell: "nfet_dev", views: [{ view: "layout", path: "libs/l/nfet_dev.oas", status: "modified" }] }] });
  render(<App />);
  await act(async () => {});
  await act(async () => {});
  const lib = document.querySelector(".librow") as HTMLElement;
  expect(within(lib).getByLabelText(t("history:mark.unsavedLib"))).toBeTruthy();
  expect(within(lib).getByLabelText(t("history:mark.stale"))).toBeTruthy();
  const devices = screen.getByText(t("app:cell.devices")).closest(".devicerow") as HTMLElement;
  expect(within(devices).getByLabelText(t("history:mark.group", { count: 1 })).querySelector(".gitmark.gm-changed")).toBeTruthy();
  fireEvent.click(devices);                                           // unfolded: each device cell shows its own
  expect(within(devices).queryByLabelText(t("history:mark.group", { count: 1 }))).toBeNull();
  expect((screen.getByText("nfet_dev").closest(".cellrow") as HTMLElement).querySelector(".gitmark.gm-changed")).toBeTruthy();
});

it("opens from the history panel only the cells there are now, and reads a stale layout again", async () => {
  location.hash = "#/p/p?history=1";
  vi.mocked(api.gitStatus).mockResolvedValue({ ...status, stale: [{ lib: "l", cells: ["inv"] }],
    cells: [...status.cells!, { lib: "l", cell: "gone", views: [{ view: "schematic", path: "libs/l/gone.sch", status: "modified" }] }] });
  render(<App />);
  await act(async () => {});
  await act(async () => {});
  const row = (name: string) => screen.getByText(name, { selector: ".gitcellrow b" }).closest(".gitcellrow") as HTMLElement;
  // gone is not in its library's cells as read: no view to open; but git sees its file in the working tree, so its
  // cell page (which reads the files) is offered
  expect(within(row("l/gone")).getByTitle(t("history:openCell"))).toBeTruthy();
  expect(within(row("l/gone")).queryByTitle(t("history:open", { view: t("history:view.schematic") }))).toBeNull();
  expect(within(row("l/amp")).getByTitle(t("history:openCell"))).toBeTruthy();
  const polls = vi.mocked(api.gitStatus).mock.calls.length;
  fireEvent.click(screen.getByRole("button", { name: t("history:stale.reload") }));
  await act(async () => {});
  fireEvent.click(within(document.querySelector(".modal") as HTMLElement).getByRole("button", { name: t("history:stale.reload") }));
  await act(async () => {});
  expect(api.layoutReload).toHaveBeenCalledWith("p", "l");
  expect(vi.mocked(api.gitStatus).mock.calls.length).toBeGreaterThan(polls);   // the status is read again
});
