// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, type ChangesetMerge, type GitStatus } from "./api";
import { setLocale, t } from "./i18n";
import { App } from "./App";
import type { ChangesPanel } from "./panels/ChangesPanel";

// A changeset is a git branch (docs/design/changeset-branches.md): its working copy names that branch in the top bar and
// the History panel, which leads back to its review; adopting or reverting it merges files under the open views of the
// project, those of the files it wrote read them again (App.mergeTabs.test.tsx: what holds unsaved text).
const state = vi.hoisted(() => ({ mounts: {} as Record<string, number>, projects: {} as Record<string, string>,
  review: null as null | Record<string, unknown>, list: null as null | Record<string, unknown> }));
vi.mock("./editors/SchematicView", async () => {
  const { useEffect } = await import("react");
  return { SchematicView: ({ project, lib, cell }: { project: string; lib: string; cell: string }) => {
    const key = `${lib}/${cell}/schematic`;
    useEffect(() => { state.mounts[key] = (state.mounts[key] ?? 0) + 1; }, []);
    state.projects[key] = project;
    return null;
  } };
});
vi.mock("./editors/LayoutEditor", () => ({ LayoutEditor: () => null }));
vi.mock("./editors/CellOverview", () => ({ CellOverview: () => null }));
vi.mock("./layout", () => ({ Splitter: () => null, RowSplitter: () => null, TabStrip: ({ children }: { children: React.ReactNode }) => children }));
vi.mock("./panels/ChatPanel", () => ({ ChatPanel: () => null }));
vi.mock("./panels/ChangesPanel", () => ({ ChangesPanel: (props: ComponentProps<typeof ChangesPanel>) => {
  if (props.review) state.review = props as unknown as Record<string, unknown>;
  else state.list = props as unknown as Record<string, unknown>;
  return null;
} }));
vi.mock("./api", async (original) => ({ ...await original<typeof import("./api")>(), api: {
  projects: vi.fn(async () => [{ name: "p", pdk: null, pdk_requested: null }]),
  pdks: vi.fn(async () => []),
  project: vi.fn(async (name: string) => ({ name, pdk: null, pdk_requested: null, libraries: [
    { name: "l", readonly: false, origin: "project", has_layout: true },
  ] })),
  cells: vi.fn(async () => [{ name: "amp", views: ["schematic"], top: true }]),
  changesets: vi.fn(async () => [{ id: "cs1", status: "open" }]), live: vi.fn(async () => []),
  activity: vi.fn(async () => ({ records: [], last: 0, writer: null })),
  deps: vi.fn(async () => ({ format: 1, libraries: [], tools: [], issues: [], warnings: [], notes: [], ok: true,
    pdk: { requested: null, locked: null, commit: null, installed: null, state: "none", changed: [], verified_by: null, ok: true } })),
  gitStatus: vi.fn(), gitLog: vi.fn(async () => ({ commits: [], more: false })),
} }));

const none: GitStatus = { repo: false, setup: null, prefix: null, branch: null, head: null, operation: null, conflicted: [], cells: [], other: [], unsaved: [] };
// the server's answer for a working copy: no history of its own, its changeset's branch and base
const preview: GitStatus = { ...none, preview: true, changeset: "cs1", branch: "undine/p/cs1", base: "b".repeat(40), stale: [], reads: {} };

beforeEach(async () => {
  vi.clearAllMocks();
  state.mounts = {};
  state.projects = {};
  state.review = null;
  state.list = null;
  vi.mocked(api.gitStatus).mockImplementation(async (p: string) => (p.includes("~") ? preview : none));
  const tabs = [{ id: "l/amp/schematic", lib: "l", cell: "amp", view: "schematic" }];
  const storage = new Map([["undine.tabs.p", JSON.stringify({ tabs, active: "l/amp/schematic" })],
    ["undine.tabs.p~cs1", JSON.stringify({ tabs, active: "l/amp/schematic" })]]);
  vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key, value) });
  await setLocale("en");
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const settle = async () => { await act(async () => {}); await act(async () => {}); };

it("names a working copy's branch in the top bar and leads from History back to its review, with nothing to set up", async () => {
  location.hash = "#/p/p~cs1?history=1";
  render(<App />);
  await settle();
  expect(api.gitStatus).toHaveBeenCalledWith("p~cs1");
  const bar = document.querySelector(".topbar") as HTMLElement;
  expect(within(bar).getByText("undine/p/cs1")).toBeTruthy();
  expect(within(bar).queryByText(t("history:button.noGit"))).toBeNull();
  const history = document.querySelector(".history") as HTMLElement;
  expect(within(history).getByText("undine/p/cs1")).toBeTruthy();
  expect(within(history).queryByRole("button", { name: t("history:setup.button") })).toBeNull();
  expect(state.review).toBeNull();
  fireEvent.click(within(history).getByRole("button", { name: t("history:preview.review") }));
  await settle();
  expect(state.review?.initialId).toBe("cs1");                      // the review docks, the History panel closes
  expect(document.querySelector(".history")).toBeNull();
});

it("reads the open views of the files a merge wrote, and the git status, again when a changeset is adopted or reverted", async () => {
  location.hash = "#/p/p?changes=cs1";
  render(<App />);
  await settle();
  expect(state.mounts["l/amp/schematic"]).toBe(1);
  const reads = vi.mocked(api.gitStatus).mock.calls.length;
  const projectReads = vi.mocked(api.project).mock.calls.length;
  const changed = (paths?: string[]) => (state.review!.onChanged as (m?: ChangesetMerge | null, a?: string) => void)(
    paths && { t: "2026-10-28T11:00:00Z", by: "user", commit: null, uncommitted: true, paths }, "apply");
  await act(async () => { changed(["libs/l/other.sch", "history/l.jsonl"]); });
  await settle();
  expect(state.mounts["l/amp/schematic"]).toBe(1);                   // none of its files
  expect(vi.mocked(api.gitStatus).mock.calls.length).toBeGreaterThan(reads);
  expect(vi.mocked(api.project).mock.calls.length).toBeGreaterThan(projectReads);
  // a schematic, not only a layout (whose reads the git status reports): the merge wrote its file
  await act(async () => { changed(["libs/l/amp.sch"]); });
  await settle();
  expect(state.mounts["l/amp/schematic"]).toBe(2);
  await act(async () => { changed(["libs/l/inv.sym"]); });             // a symbol it may place
  await settle();
  expect(state.mounts["l/amp/schematic"]).toBe(3);
  await act(async () => { changed(undefined); });                      // a server that does not say which: all of them
  await settle();
  expect(state.mounts["l/amp/schematic"]).toBe(4);
});

it("opens a changed file's view in the version the review names, keeping the open views", async () => {
  location.hash = "#/p/p?changes=cs1";
  render(<App />);
  await settle();
  expect(state.projects["l/amp/schematic"]).toBe("p");
  await act(async () => { (state.review!.onOpenView as (p: string, l: string, c: string, v: string) => void)("p~cs1", "l", "amp", "schematic"); });
  await settle();
  expect(state.projects["l/amp/schematic"]).toBe("p~cs1");
  expect(within(document.querySelector(".topbar") as HTMLElement).getByText("undine/p/cs1")).toBeTruthy();
  // a changeset opened (git-setup may have made the project a repository): the status is read again
  const reads = vi.mocked(api.gitStatus).mock.calls.length;
  await act(async () => { (state.review!.onGit as () => void)(); });
  expect(vi.mocked(api.gitStatus).mock.calls.length).toBe(reads + 1);
});

it("says in the top bar that a changeset is being adopted, counts it among the open ones no more, and counts again when it is done", async () => {
  vi.useFakeTimers();
  try {
    vi.mocked(api.changesets).mockResolvedValue([{ id: "cs1", status: "open" }, { id: "cs2", status: "open" }] as never);
    vi.mocked(api.live).mockResolvedValue([{ id: "cs1", title: "Comparator", writer: null, closing: {
      action: "apply", by: "user", since: "2026-11-03T10:00:00+0900", phase: "evidence", bytes: 3.1e9, bytes_total: 25e9 } }]);
    location.hash = "#/p/p";
    render(<App />);
    await settle();
    await act(async () => { await vi.advanceTimersByTimeAsync(10); });
    const bar = document.querySelector(".topbar") as HTMLElement;
    const [count, closing] = [...bar.querySelectorAll(".countdot")].filter((x) => x.closest("button")?.textContent?.includes(t("app:changes.label")));
    expect(count.textContent).toBe("1");                                  // cs2: cs1 is being adopted
    expect(closing.textContent).toContain(t("app:changes.closing.apply"));
    expect(closing.getAttribute("title")).toBe(`Comparator: ${t("review:closing.line", { action: t("review:status.closing.apply"),
      phase: t("review:closing.evidenceBytes", { done: "3.1", total: "25 GB" }) })}`);
    expect(document.querySelector(".livebar")).toBeNull();               // no one writes it: nothing to watch
    // the Changes list opened: it hears of the closures /live sees
    fireEvent.click(within(bar).getByTitle(t("app:changes.hint")));
    await settle();
    expect(state.list?.liveClosings).toBe("cs1");
    // the adoption ended on the server and the count was read again before /live was: cs1 is not taken off twice
    vi.mocked(api.changesets).mockResolvedValue([{ id: "cs1", status: "applied" }, { id: "cs2", status: "open" }] as never);
    fireEvent.click(within(bar).getByTitle(t("app:changes.hint")));
    await settle();
    const ended = [...bar.querySelectorAll(".countdot")].filter((x) => x.closest("button")?.textContent?.includes(t("app:changes.label")));
    expect(ended.map((x) => x.textContent)).toEqual(["1", expect.stringContaining(t("app:changes.closing.apply"))]);
    vi.mocked(api.live).mockResolvedValue([]);
    vi.mocked(api.changesets).mockResolvedValue([{ id: "cs1", status: "applied" }, { id: "cs2", status: "open" }] as never);
    const reads = vi.mocked(api.changesets).mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(vi.mocked(api.changesets).mock.calls.length).toBeGreaterThan(reads);     // it ended: the count is read again
    const after = [...bar.querySelectorAll(".countdot")].filter((x) => x.closest("button")?.textContent?.includes(t("app:changes.label")));
    expect(after.map((x) => x.textContent)).toEqual(["1"]);
  } finally {
    vi.useRealTimers();
  }
});
