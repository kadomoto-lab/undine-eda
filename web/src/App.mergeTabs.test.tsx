// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type ChangesetMerge, type GitStatus, type IntentView } from "./api";
import { setLocale, t } from "./i18n";
import { App } from "./App";
import type { ChangesPanel } from "./panels/ChangesPanel";
import type { CellOverview } from "./editors/CellOverview";

// Adopting or reverting a changeset merges files into the project under its open tabs (docs/design/changeset-branches.md,
// Apply 7): the tabs showing a file the merge wrote read it again, the others stay as they are, and one holding text
// not saved (an RTL, a cell page's intent YAML) keeps it, with a notice, rather than losing it to a reload.
const state = vi.hoisted(() => ({ review: null as null | Record<string, unknown>, mounts: {} as Record<string, number> }));
vi.mock("./editors/SchematicView", async () => {
  const { useEffect } = await import("react");
  return { SchematicView: ({ lib, cell }: { lib: string; cell: string }) => {
    useEffect(() => { state.mounts[`${lib}/${cell}`] = (state.mounts[`${lib}/${cell}`] ?? 0) + 1; }, [lib, cell]);
    return null;
  } };
});
vi.mock("./editors/LayoutEditor", () => ({ LayoutEditor: () => null }));
// the cell page with its intent editor open: the real IntentPanel, read again with the page's tick
vi.mock("./editors/CellOverview", async () => {
  const { IntentPanel } = await import("./panels/IntentPanel");
  return { CellOverview: ({ project, lib, cell, tick, toast }: ComponentProps<typeof CellOverview>) =>
    <IntentPanel project={project} lib={lib} cell={cell} tick={tick} toast={toast} selection={[]} /> };
});
// the real RtlView, with CodeMirror a textarea
vi.mock("./editors/CodeEditor", async () => {
  const { forwardRef } = await import("react");
  return { CodeEditor: forwardRef(({ value, onChange }: { value: string; onChange: (s: string) => void }, _ref) =>
    <textarea data-testid="code" value={value} onChange={(e) => onChange(e.target.value)} />) };
});
vi.mock("./panels/LibreLanePanel", () => ({ LibreLanePanel: () => null }));
vi.mock("./layout", () => ({ Splitter: () => null, RowSplitter: () => null, TabStrip: ({ children }: { children: React.ReactNode }) => children }));
vi.mock("./panels/ChatPanel", () => ({ ChatPanel: () => null }));
vi.mock("./panels/ChangesPanel", () => ({ ChangesPanel: (props: ComponentProps<typeof ChangesPanel>) => {
  if (props.review) state.review = props as unknown as Record<string, unknown>;
  return null;
} }));
vi.mock("./api", async (original) => ({ ...await original<typeof import("./api")>(), api: {
  projects: vi.fn(async () => [{ name: "p", pdk: null, pdk_requested: null }]),
  pdks: vi.fn(async () => []),
  project: vi.fn(async (name: string) => ({ name, pdk: null, pdk_requested: null, libraries: [
    { name: "l", readonly: false, origin: "project", has_layout: true },
  ] })),
  cells: vi.fn(async () => [{ name: "ctr", views: ["rtl"], top: true }, { name: "amp", views: ["schematic"], top: true },
    { name: "top", views: ["schematic"], top: true }]),
  changesets: vi.fn(async () => [{ id: "cs1", status: "open" }]), live: vi.fn(async () => []),
  activity: vi.fn(async () => ({ records: [], last: 0, writer: null })),
  deps: vi.fn(async () => ({ format: 1, libraries: [], tools: [], issues: [], warnings: [], notes: [], ok: true,
    pdk: { requested: null, locked: null, commit: null, installed: null, state: "none", changed: [], verified_by: null, ok: true } })),
  gitStatus: vi.fn(), gitLog: vi.fn(async () => ({ commits: [], more: false })),
  rtl: vi.fn(), rtlLint: vi.fn(async () => ({ ok: true, top: "ctr", messages: [], files: [] })), jobs: vi.fn(async () => []),
  intent: vi.fn(),
} }));

const none: GitStatus = { repo: false, setup: null, prefix: null, branch: null, head: null, operation: null, conflicted: [], cells: [], other: [], unsaved: [] };
const RTL = "module ctr;\nendmodule\n";
const intentView = (yaml: string): IntentView => ({
  intent: { version: 1, notes: "", ports: {}, supplies: {}, items: [], views: {} }, yaml, sha: "s1", readonly: false, problems: [],
  requirements: {}, steps: {}, refs: null, suggested_supplies: {}, undo: { undo: false, redo: false } });

function restore(tabs: { id: string; lib: string; cell: string; view: string }[], active: string) {
  const storage = new Map([["undine.tabs.p", JSON.stringify({ tabs, active })]]);
  vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key, value) });
}

beforeEach(async () => {
  vi.clearAllMocks();
  state.review = null;
  state.mounts = {};
  vi.mocked(api.gitStatus).mockImplementation(async () => none);
  vi.mocked(api.rtl).mockImplementation(async () => ({ text: RTL, sha: "abc", file: "ctr.sv", modules: ["ctr"], ports: [],
    undo: { undo: false, redo: false }, readonly: false }));
  vi.mocked(api.intent).mockImplementation(async () => intentView("notes: ''\n"));
  await setLocale("en");
  location.hash = "#/p/p?changes=cs1";
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const settle = async () => { for (let i = 0; i < 6; i++) await act(async () => { await new Promise((r) => setTimeout(r, 0)); }); };
const merge = (paths: string[]): ChangesetMerge => ({ t: "2026-10-28T11:00:00Z", by: "user", commit: "d".repeat(40), uncommitted: false, paths });
const adopted = async (m: ChangesetMerge | undefined, action: "apply" | "revert" = "apply") => {
  await act(async () => { (state.review!.onChanged as (m?: ChangesetMerge, a?: string) => void)(m, action); });
  await settle();
};
const code = () => document.querySelector("[data-testid=code]") as HTMLTextAreaElement;
const notice = () => document.querySelector(".tabnotice");

describe("an RTL tab", () => {
  beforeEach(() => restore([{ id: "l/ctr/rtl", lib: "l", cell: "ctr", view: "rtl" },
    { id: "l/amp/schematic", lib: "l", cell: "amp", view: "schematic" }, { id: "l/top/schematic", lib: "l", cell: "top", view: "schematic" }],
  "l/ctr/rtl"));

  it("keeps its unsaved text when the merge did not write its file, and the views of the files it wrote read them again", async () => {
    render(<App />);
    await settle();
    expect(code().value).toBe(RTL);
    fireEvent.change(code(), { target: { value: `// my note\n${RTL}` } });
    await settle();
    expect(document.querySelector(".tab .dirty")).toBeTruthy();
    const reads = vi.mocked(api.gitStatus).mock.calls.length;
    await adopted(merge(["libs/l/top.sch", "history/l.jsonl"]));
    expect(code().value).toBe(`// my note\n${RTL}`);                    // not read again: its file is as it was
    expect(document.querySelector(".tab .dirty")).toBeTruthy();
    expect(notice()).toBeNull();
    expect(api.rtl).toHaveBeenCalledTimes(1);
    expect(state.mounts["l/top"]).toBe(2);                              // the schematic the merge wrote
    expect(state.mounts["l/amp"]).toBe(1);                              // one it did not
    expect(vi.mocked(api.gitStatus).mock.calls.length).toBeGreaterThan(reads);
  });

  it("keeps its unsaved text when the merge wrote its file, with a notice; reloading takes the merged file", async () => {
    render(<App />);
    await settle();
    fireEvent.change(code(), { target: { value: `// my note\n${RTL}` } });
    await settle();
    vi.mocked(api.rtl).mockImplementation(async () => ({ text: "module ctr(input a);\nendmodule\n", sha: "def", file: "ctr.sv", modules: ["ctr"],
      ports: [], undo: { undo: false, redo: false }, readonly: false }));
    await adopted(merge(["libs/l/ctr.sv"]), "revert");
    expect(code().value).toBe(`// my note\n${RTL}`);
    expect(document.querySelector(".tab .dirty")).toBeTruthy();
    expect(notice()?.textContent).toContain(t("app:merged.revert", { lib: "l", cell: "ctr" }));
    expect(notice()?.getAttribute("title")).toBe("libs/l/ctr.sv");
    fireEvent.click(screen.getByRole("button", { name: t("app:merged.reload") }));
    await settle();
    expect(code().value).toBe("module ctr(input a);\nendmodule\n");
    expect(document.querySelector(".tab .dirty")).toBeNull();
    expect(notice()).toBeNull();
  });

  it("is read again, its notice gone, when its unsaved text is undone after the merge wrote its file", async () => {
    render(<App />);
    await settle();
    fireEvent.change(code(), { target: { value: `// my note\n${RTL}` } });
    await settle();
    const MERGED = "module ctr(input a);\nendmodule\n";
    vi.mocked(api.rtl).mockImplementation(async () => ({ text: MERGED, sha: "def", file: "ctr.sv", modules: ["ctr"],
      ports: [], undo: { undo: false, redo: false }, readonly: false }));
    await adopted(merge(["libs/l/ctr.sv"]));
    expect(notice()).toBeTruthy();
    fireEvent.change(code(), { target: { value: RTL } });               // typed back to the text it loaded: clean again
    await settle();
    expect(code().value).toBe(MERGED);                                  // not the text from before the merge
    expect(api.rtl).toHaveBeenCalledTimes(2);
    expect(document.querySelector(".tab .dirty")).toBeNull();
    expect(notice()).toBeNull();
  });

  it("is read again when it has nothing unsaved and the merge wrote its file", async () => {
    render(<App />);
    await settle();
    await adopted(merge(["libs/l/ctr.sv"]));
    expect(api.rtl).toHaveBeenCalledTimes(2);
    expect(notice()).toBeNull();
    expect(state.mounts["l/amp"]).toBe(1);
  });
});

describe("a cell page's intent YAML", () => {
  beforeEach(() => restore([{ id: "l/ctr/cell", lib: "l", cell: "ctr", view: "cell" }], "l/ctr/cell"));
  const yaml = () => document.querySelector("textarea.intentyaml") as HTMLTextAreaElement;

  it("keeps its unsaved text when the merge wrote the intent, with a notice in the reader's language", async () => {
    render(<App />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: t("intent:yaml") }));
    fireEvent.change(yaml(), { target: { value: "notes: mine\n" } });
    vi.mocked(api.intent).mockImplementation(async () => intentView("notes: merged\n"));
    await setLocale("ja");
    await adopted(merge(["libs/l/ctr.intent.yaml"]));
    expect(yaml().value).toBe("notes: mine\n");
    expect(notice()?.textContent).toContain(t("app:merged.apply", { lib: "l", cell: "ctr" }));
    expect(t("app:merged.apply", { lib: "l", cell: "ctr" })).toContain("未保存");
  });

  it("is read again, its editor kept open, when it holds no unsaved text", async () => {
    render(<App />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: t("intent:yaml") }));
    vi.mocked(api.intent).mockImplementation(async () => intentView("notes: merged\n"));
    await adopted(merge(["libs/l/ctr.netlist.yaml"]));
    expect(yaml().value).toBe("notes: merged\n");
    expect(notice()).toBeNull();
  });
});
