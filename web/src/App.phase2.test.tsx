// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, ApiError, type DepsStatus, type LibraryInfo, type PdkLockState } from "./api";
import { setLocale, t } from "./i18n";
import { App } from "./App";

// Phase 2 in the library browser (docs/design/packages.md, UI): dependencies installed for a package show who needs
// them and offer no Update / Remove; an alias names its package; a dependency with content the trust rule refuses has
// a shield that opens the Trust dialog; refusals that list every problem (resolution, a PDK locked packages do not
// accept) show their list in a dialog.
vi.mock("./editors/SchematicView", () => ({ SchematicView: () => null }));
vi.mock("./editors/LayoutEditor", () => ({ LayoutEditor: () => null }));
vi.mock("./editors/CellOverview", () => ({ CellOverview: () => null }));
vi.mock("./layout", () => ({ Splitter: () => null, RowSplitter: () => null, TabStrip: ({ children }: { children: React.ReactNode }) => children }));
vi.mock("./panels/ChatPanel", () => ({ ChatPanel: () => null }));
vi.mock("./panels/ChangesPanel", () => ({ ChangesPanel: () => null }));
vi.mock("./api", async (original) => ({ ...await original<typeof import("./api")>(), api: {
  projects: vi.fn(async () => [{ name: "p", pdk: null, pdk_requested: null }]),
  pdks: vi.fn(async () => []),
  project: vi.fn(),
  cells: vi.fn(async () => []),
  changesets: vi.fn(async () => []), live: vi.fn(async () => []),
  activity: vi.fn(async () => ({ records: [], last: 0, writer: null })),
  deps: vi.fn(),
  depsSync: vi.fn(), depsLock: vi.fn(), depAdd: vi.fn(), setPdk: vi.fn(),
  trustList: vi.fn(), trustConfirm: vi.fn(), trust: vi.fn(), untrust: vi.fn(),
  gitStatus: vi.fn(async () => ({ repo: false, setup: null, prefix: null, branch: null, head: null, operation: null, conflicted: [], cells: [], other: [], unsaved: [] })),
  gitLog: vi.fn(async () => ({ commits: [], more: false })),
} }));
const mocked = api as unknown as Record<keyof typeof api, ReturnType<typeof vi.fn>>;

const C = "229914698b1f0123456789abcdef0123456789ab";
const PDK_NONE: PdkLockState = { requested: null, locked: null, commit: null, installed: null, state: "none", changed: [], verified_by: null, ok: true };
const status = (over: Partial<DepsStatus> = {}): DepsStatus => ({ format: 2, libraries: [], tools: [], issues: [],
  warnings: [], notes: [], ok: true, pdk: { ...PDK_NONE }, ...over });
const lib = (name: string, over: Partial<LibraryInfo> = {}): LibraryInfo =>
  ({ name, readonly: true, origin: "dep", has_layout: false, version: `v1 @ ${C.slice(0, 7)}`, ...over });
type Row = DepsStatus["libraries"][number];
const row = (name: string, over: Partial<Row> = {}): Row => ({ name, kind: "dependency", state: "ok", git: "https://example.org/ip.git",
  rev: "v1.2.0", subdir: null, locked: C, installed: C, hash: "tree", in_store: true, package: null, version: null, names: [name],
  direct: true, via: [], chain: null, trusted: true, flagged: { spice: 0, rtl: 0, tcl: 0 }, internal_cells: [], pdk: null, ...over });

// analog_ip (direct) needs sar_common; bias is an alias of bias_gen, installed under its own name too
const LIBS: LibraryInfo[] = [
  { name: "mine", readonly: false, origin: "project", has_layout: false, package: null, direct: true, via: [] },
  lib("sar_common", { package: "sar_common", direct: false, via: ["analog_ip"] }),
  lib("bias_gen", { package: "bias_gen", direct: false, via: [] }),
  lib("analog_ip", { package: "analog_ip", direct: true, via: [] }),
  lib("bias", { package: "bias_gen", direct: true, via: [] }),
];
const ROWS: Row[] = [
  row("analog_ip", { package: "analog_ip", version: "1.2.0", names: ["analog_ip"], trusted: false, flagged: { spice: 2, rtl: 0, tcl: 1 } }),
  row("bias", { package: "bias_gen", version: "0.3.0", names: ["bias", "bias_gen"] }),
  row("sar_common", { package: "sar_common", version: "1.3.1", names: ["sar_common"], direct: false, via: ["analog_ip"],
    chain: `p -> analog_ip 1.2.0 (v1.2.0 @ ${C.slice(0, 12)}) -> sar_common 1.3.1 (v1.3.1 @ ${C.slice(0, 12)})` }),
  row("bias_gen", { package: "bias_gen", version: "0.3.0", names: ["bias", "bias_gen"], direct: false }),
];

beforeEach(async () => {
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key, value) });
  await setLocale("en");
  for (const k of ["project", "deps", "depsLock", "depAdd", "setPdk", "trustList", "trustConfirm", "trust", "untrust"] as const) mocked[k].mockReset();
  mocked.project.mockResolvedValue({ name: "p", pdk: null, pdk_requested: null, libraries: LIBS });
  mocked.deps.mockResolvedValue(status({ libraries: ROWS }));
  location.hash = "#/p/p";
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); location.hash = ""; });

const settle = async () => { await act(async () => {}); await act(async () => {}); };
const libRow = (name: string) => {
  const el = [...document.querySelectorAll(".librow")].find((r) => r.querySelector(".libname")?.textContent === name);
  if (!el) throw new Error(`no row ${name}`);
  return el as HTMLElement;
};
const hasButton = (el: HTMLElement, title: string) => [...el.querySelectorAll("button")].some((b) => b.getAttribute("title") === title);

it("lists direct dependencies first, tags transitive and alias rows, and offers Update / Remove on direct ones only", async () => {
  render(<App />);
  await settle();
  const names = [...document.querySelectorAll(".lbgroup.deep")][0].querySelectorAll(".libname");
  expect([...names].map((n) => n.textContent)).toEqual(["analog_ip", "bias", "sar_common", "bias_gen"]);

  const sar = libRow("sar_common");
  expect(sar.textContent).toContain(t("app:dependency.via", { list: "analog_ip" }));
  expect(hasButton(sar, t("app:dependency.updateHint"))).toBe(false);
  expect(hasButton(sar, t("app:dependency.removeHint"))).toBe(false);
  // the tag's hint gives the chain from the project
  expect(within(sar).getByText(t("app:dependency.via", { list: "analog_ip" })).getAttribute("title")).toContain("p -> analog_ip 1.2.0");

  const bias = libRow("bias");
  expect(bias.textContent).toContain(t("app:dependency.alias", { package: "bias_gen" }));
  expect(hasButton(bias, t("app:dependency.updateHint"))).toBe(true);
  // the package's own name beside the alias: the same files, no Update / Remove
  const own = libRow("bias_gen");
  expect(own.textContent).toContain(t("app:dependency.sameAs", { names: "bias" }));
  expect(hasButton(own, t("app:dependency.removeHint"))).toBe(false);

  const ip = libRow("analog_ip");
  expect(hasButton(ip, t("app:dependency.updateHint"))).toBe(true);
  expect(hasButton(ip, t("app:dependency.removeHint"))).toBe(true);
});

it("opens the Trust dialog from the shield of a dependency with untrusted content", async () => {
  mocked.trustList.mockResolvedValue({ trusted: [], dependencies: [{ name: "analog_ip", package: "analog_ip", version: "1.2.0",
    key: "analog_ip@1.2.0", trusted: false, findings: [{ file: "analog_ip/tb.sch", kind: "control", line: 1, text: ".control" }] }] });
  render(<App />);
  await settle();
  expect(libRow("sar_common").querySelector("button.warn")).toBeNull();    // nothing flagged there: no shield
  const shield = libRow("analog_ip").querySelector("button.warn") as HTMLButtonElement;
  expect(shield.getAttribute("title")).toContain("2 SPICE lines, 1 Tcl attribute");
  fireEvent.click(shield);
  await settle();
  const dialog = screen.getByRole("dialog", { name: t("app:trust.title", { name: "analog_ip" }) });
  expect(dialog.textContent).toContain("analog_ip@1.2.0");
  expect(dialog.textContent).toContain("analog_ip/tb.sch");
  expect(mocked.trustConfirm).not.toHaveBeenCalled();
});

it("still renders a dependency from a server without the phase 2 fields, with Update and Remove", async () => {
  mocked.project.mockResolvedValue({ name: "p", pdk: null, pdk_requested: null, libraries: [
    { name: "ip", readonly: true, origin: "dep", has_layout: false, version: "v1 @ 0123456" }] });
  mocked.deps.mockResolvedValue(status({ format: 1, libraries: [{ name: "ip", kind: "dependency", state: "ok", git: "https://example.org/ip.git",
    rev: "v1", subdir: null, locked: C, installed: C, hash: "sha", in_store: true }] }));
  render(<App />);
  await settle();
  const ip = libRow("ip");
  expect(hasButton(ip, t("app:dependency.updateHint"))).toBe(true);
  expect(hasButton(ip, t("app:dependency.removeHint"))).toBe(true);
  expect(ip.querySelector(".tag:not(.mono)")).toBeNull();
  expect(ip.querySelector("button.warn")).toBeNull();
});

it("shows the lock dialog rows with package, version, via and trust", async () => {
  render(<App />);
  await settle();
  fireEvent.click(within(document.querySelector(".topbar") as HTMLElement).getByRole("button", { name: new RegExp(t("app:lock.locked")) }));
  await settle();
  const text = screen.getByRole("dialog", { name: t("app:lock.title") }).textContent ?? "";
  expect(text).toContain("analog_ip 1.2.0");
  expect(text).toContain(t("app:dependency.notTrusted", { what: "2 SPICE lines, 1 Tcl attribute" }));
  expect(text).toContain(`sar_common 1.3.1, ${t("app:dependency.via", { list: "analog_ip" })}`);
});

it("lists the transitive packages and the untrusted content after an add", async () => {
  mocked.depAdd.mockResolvedValue({ name: "dac", commit: C, cells: ["dac8"], package: "dac", version: "0.1.0",
    flagged: { spice: 2, rtl: 0, tcl: 0 }, trusted: false, resolved: [
      { name: "dac", commit: C, package: "dac", version: "0.1.0", names: ["dac"], via: [], flagged: { spice: 2, rtl: 0, tcl: 0 }, trusted: false },
      { name: "dac_common", commit: C, package: "dac_common", version: "1.0.0", names: ["dac_common"], via: ["dac"], flagged: { spice: 0, rtl: 1, tcl: 0 }, trusted: false },
    ] });
  render(<App />);
  await settle();
  fireEvent.click(screen.getByTitle(t("app:dependency.addHint")));
  await settle();
  const form = screen.getByRole("dialog", { name: t("app:dependency.add") });
  const inputs = form.querySelectorAll("input");
  fireEvent.change(inputs[0], { target: { value: "dac" } });
  fireEvent.change(inputs[1], { target: { value: "https://example.org/dac.git" } });
  fireEvent.change(inputs[4], { target: { value: "dac" } });
  fireEvent.click(within(form).getByRole("button", { name: t("common:actions.add") }));
  await settle();
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  expect(mocked.depAdd).toHaveBeenCalledWith("p", "dac", "https://example.org/dac.git", "", "", "dac");
  const result = screen.getByRole("dialog", { name: t("app:dependency.addedTitle", { name: "dac" }) });
  expect(result.textContent).toContain(`dac: dac 0.1.0 at ${C.slice(0, 7)}`);
  expect(result.textContent).toContain("+ dac_common 1.0.0 as dac_common, via dac");
  expect(result.textContent).toContain("dac 0.1.0: 2 SPICE lines");
  expect(result.textContent).toContain("dac_common 1.0.0: 1 RTL line");
});

it("shows every problem of a refused add in a dialog, the form kept open", async () => {
  const problems = [
    { code: "conflict", message: "sar_common: two versions: 1.3.1 and 2.0.0", chains: ["p -> analog_ip 1.2.0 -> sar_common 1.3.1", "p -> dac 0.1.0 -> sar_common 2.0.0"] },
    { code: "cell_clash", message: "comparator: analog_ip 1.2.0 and sar_common 1.3.1", chains: [] },
  ];
  mocked.depAdd.mockRejectedValue(new ApiError(409, "2 problems with the dependencies:\n- ...\nnothing was changed", "resolution_refused", { problems }));
  render(<App />);
  await settle();
  fireEvent.click(screen.getByTitle(t("app:dependency.addHint")));
  await settle();
  const form = screen.getByRole("dialog", { name: t("app:dependency.add") });
  const inputs = form.querySelectorAll("input");
  fireEvent.change(inputs[0], { target: { value: "dac" } });
  fireEvent.change(inputs[1], { target: { value: "https://example.org/dac.git" } });
  fireEvent.click(within(form).getByRole("button", { name: t("common:actions.add") }));
  await settle();
  const refused = screen.getByRole("dialog", { name: t("app:refusal.resolution") });
  expect(refused.textContent).toContain(t("app:refusal.resolutionIntro", { count: 2 }));
  const items = within(refused).getByTestId("refusal-list").querySelectorAll("li");
  expect(items).toHaveLength(2);
  expect(items[0].textContent).toContain("p -> dac 0.1.0 -> sar_common 2.0.0");
  expect(items[1].textContent).toContain("comparator: analog_ip 1.2.0 and sar_common 1.3.1");
  expect(screen.getByRole("dialog", { name: t("app:dependency.add") }).textContent).toContain("2 problems with the dependencies:");
  fireEvent.click(within(refused).getByRole("button", { name: t("common:actions.ok") }));
  expect(screen.queryByRole("dialog", { name: t("app:refusal.resolution") })).toBeNull();
  expect(screen.getByRole("dialog", { name: t("app:dependency.add") })).toBeTruthy();
});

it("shows the packages that do not accept the PDK when moving the PDK is refused", async () => {
  const OLD = { id: "sky130A@aaaaaaaaaaaa", name: "sky130A", family: "sky130", version: "a".repeat(40) };
  mocked.project.mockResolvedValue({ name: "p", pdk: OLD, pdk_requested: OLD.id, pdk_unsupported: "old PCell code",
    pdk_upgrade: "sky130A@bbbbbbbbbbbb", libraries: LIBS });
  const packages = [{ name: "analog_ip", package: "analog_ip", version: "1.2.0", chain: "p -> analog_ip 1.2.0 (v1.2.0 @ 229914698b1f)",
    reason: "bbbbbbbbbbbb is not among commits aaaaaaaaaaaa", family: "sky130", variants: ["sky130A"], commits: ["a".repeat(40)], compat: [], built_with: null }];
  mocked.setPdk.mockRejectedValue(new ApiError(409, "the PDK sky130A@bbbbbbbbbbbb is not accepted by 1 locked package(s):\n- ...",
    "pdk_incompatible", { packages }));
  render(<App />);
  await settle();
  fireEvent.click(within(document.querySelector(".topbar") as HTMLElement).getByRole("button", { name: /sky130A/ }));
  fireEvent.click(screen.getByRole("menuitem", { name: t("app:pdk.move", { to: "sky130A@bbbbbbbbbbbb" }) }));
  await settle();
  fireEvent.click(screen.getByRole("button", { name: t("app:pdk.move", { to: "sky130A@bbbbbbbbbbbb" }) }));
  await settle();
  expect(mocked.setPdk).toHaveBeenCalledWith("p", "sky130A@bbbbbbbbbbbb");
  const refused = screen.getByRole("dialog", { name: t("app:refusal.pdk") });
  expect(refused.textContent).toContain(t("app:refusal.pdkIntro", { pdk: "sky130A@bbbbbbbbbbbb", count: 1 }));
  expect(within(refused).getByTestId("refusal-list").textContent)
    .toBe("p -> analog_ip 1.2.0 (v1.2.0 @ 229914698b1f): bbbbbbbbbbbb is not among commits aaaaaaaaaaaa");
  expect(refused.textContent).toContain(t("app:refusal.pdkFix"));
});
