// @vitest-environment jsdom
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, type Activity } from "./api";
import { setLocale } from "./i18n";
import { App } from "./App";

// An external agent writes the changeset under review, then its session ends: the views follow what it committed and
// then stay put (no remount on every later poll of the finished changeset).
const state = vi.hoisted(() => ({ mounts: {} as Record<string, number>, activity: [] as Activity[] }));
vi.mock("./editors/LayoutEditor", async () => {
  const { useEffect } = await import("react");
  return { LayoutEditor: ({ lib, cell }: { lib: string; cell: string }) => {
    useEffect(() => { const key = `${lib}/${cell}/layout`; state.mounts[key] = (state.mounts[key] ?? 0) + 1; }, []);
    return null;
  } };
});
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
  cells: vi.fn(async () => [{ name: "inv", views: ["schematic", "layout"], top: true }]),
  changesets: vi.fn(async () => [{ id: "cs1", status: "open" }]), live: vi.fn(async () => []),
  deps: vi.fn(async () => ({ format: 1, libraries: [], tools: [], issues: [], warnings: [], notes: [], ok: true,
    pdk: { requested: null, locked: null, commit: null, installed: null, state: "none", changed: [], verified_by: null, ok: true } })),
  gitStatus: vi.fn(async () => ({ repo: false, setup: null, prefix: null, branch: null, head: null, operation: null, conflicted: [], cells: [], other: [], unsaved: [] })),
  activity: vi.fn(async () => state.activity.length > 1 ? state.activity.shift()! : state.activity[0]),
} }));

const writer = { owner: "mcp:s1", kind: "mcp", label: "agent" } as unknown as Activity["writer"];
const sync = (gseq: number, turn = "t1"): Activity["records"][number] =>
  ({ gseq, lib: "l", cell: "inv", view: "layout", op: "sync_layout", actor: "agent", turn });
const tick = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

beforeEach(async () => {
  vi.useFakeTimers();
  vi.clearAllMocks();
  state.mounts = {};
  location.hash = "#/p/p~cs1?changes=cs1";
  const tabs = [{ id: "l/inv/layout", lib: "l", cell: "inv", view: "layout" }];
  const storage = new Map([["undine.tabs.p~cs1", JSON.stringify({ tabs, active: "l/inv/layout" })]]);
  vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key, value) });
  await setLocale("en");
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });

it("reloads a followed view once per commit and not again after the agent is done", async () => {
  state.activity = [
    { records: [], last: 0, writer },                 // the agent holds the changeset
    { records: [sync(1)], last: 1, writer },          // it commits a layout change
    { records: [], last: 1, writer: null },           // its session ends
  ];
  render(<App />);
  await tick(0);
  expect(state.mounts).toEqual({ "l/inv/layout": 1 });
  await tick(1000);                                   // the commit: the layout reloads
  expect(state.mounts).toEqual({ "l/inv/layout": 2 });
  await tick(1000);                                   // done: nothing new to show
  await tick(4000 * 5);                               // later polls of the finished changeset
  expect(vi.mocked(api.activity).mock.calls.length).toBeGreaterThanOrEqual(7);
  expect(state.mounts).toEqual({ "l/inv/layout": 2 });
});

it("shows what the agent committed in the poll that finds it done", async () => {
  state.activity = [
    { records: [sync(1)], last: 1, writer },
    { records: [sync(2)], last: 2, writer: null },    // the last commit and the end in the same poll
    { records: [], last: 2, writer: null },
  ];
  render(<App />);
  await tick(0);
  expect(state.mounts).toEqual({ "l/inv/layout": 2 });
  await tick(1000);
  expect(state.mounts).toEqual({ "l/inv/layout": 3 });
  await tick(4000 * 3);
  expect(state.mounts).toEqual({ "l/inv/layout": 3 });
});
