// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { setLocale, t } from "./i18n";
import { App } from "./App";

// The in-app Agent is hidden by default (external agents over MCP are the main way), and the Agent icon just left of
// Changes in the top bar is always there to show or hide it (docs/guide/assistant.md).
vi.mock("./editors/SchematicView", () => ({ SchematicView: () => null }));
vi.mock("./editors/LayoutEditor", () => ({ LayoutEditor: () => null }));
vi.mock("./editors/CellOverview", () => ({ CellOverview: () => null }));
vi.mock("./layout", () => ({ Splitter: () => null, RowSplitter: () => null, TabStrip: ({ children }: { children: React.ReactNode }) => children }));
vi.mock("./panels/ChatPanel", () => ({ ChatPanel: () => <div className="chat-under-test" /> }));
vi.mock("./panels/ChangesPanel", () => ({ ChangesPanel: () => null }));
vi.mock("./api", async (original) => ({ ...await original<typeof import("./api")>(), api: {
  projects: vi.fn(async () => [{ name: "p", pdk: null, pdk_requested: null }]),
  pdks: vi.fn(async () => []),
  project: vi.fn(async (name: string) => ({ name, pdk: null, pdk_requested: null, libraries: [] })),
  cells: vi.fn(async () => []),
  changesets: vi.fn(async () => []), live: vi.fn(async () => []),
  activity: vi.fn(async () => ({ records: [], last: 0, writer: null })),
  deps: vi.fn(async () => ({ format: 1, libraries: [], tools: [], issues: [], warnings: [], notes: [], ok: true,
    pdk: { requested: null, locked: null, commit: null, installed: null, state: "none", changed: [], verified_by: null, ok: true } })),
  gitStatus: vi.fn(async () => ({ repo: false, setup: null, prefix: null, branch: null, head: null, operation: null, conflicted: [], cells: [], other: [], unsaved: [] })),
  gitLog: vi.fn(async () => ({ commits: [], more: false })),
} }));

beforeEach(async () => {
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key, value) });
  await setLocale("en");
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); location.hash = ""; });

const settle = async () => { await act(async () => {}); await act(async () => {}); };
const chat = () => document.querySelector(".chat-under-test");
const icon = (name: string) => within(document.querySelector(".topbar") as HTMLElement).getByRole("button", { name });

it("hides the Agent by default, and the icon just left of Changes shows and hides it", async () => {
  location.hash = "#/p/p";
  render(<App />);
  await settle();
  expect(chat()).toBeNull();
  const show = icon(t("app:agent.show"));
  const bar = document.querySelector(".topbar") as HTMLElement;
  const changes = within(bar).getByRole("button", { name: new RegExp(t("app:changes.label")) });
  expect(show.nextElementSibling).toBe(changes);                     // just left of Changes
  expect(show.getAttribute("aria-pressed")).toBe("false");
  fireEvent.click(show);
  await settle();
  expect(chat()).not.toBeNull();
  const hide = icon(t("app:agent.hide"));
  expect(hide.getAttribute("aria-pressed")).toBe("true");
  fireEvent.click(hide);
  await settle();
  expect(chat()).toBeNull();
  expect(icon(t("app:agent.show"))).toBeTruthy();                    // still there to open it again
});

it("shows it at once with ?chat=1, the icon then hiding it", async () => {
  location.hash = "#/p/p?chat=1";
  render(<App />);
  await settle();
  expect(chat()).not.toBeNull();
  fireEvent.click(icon(t("app:agent.hide")));
  await settle();
  expect(chat()).toBeNull();
});
