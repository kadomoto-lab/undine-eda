// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, ApiError, type DepsStatus, type PdkLockState } from "./api";
import { setLocale, t } from "./i18n";
import { App } from "./App";

// The PDK badge is a menu (its state against undine.lock, Fetch... for a missing PDK, Rescan PDKs), and the lock
// badge opens the lock dialog, which offers first what fixes the project's state (docs/design/packages.md, Lock badge).
vi.mock("./editors/SchematicView", () => ({ SchematicView: () => null }));
vi.mock("./editors/LayoutEditor", () => ({ LayoutEditor: () => null }));
vi.mock("./editors/CellOverview", () => ({ CellOverview: () => null }));
vi.mock("./layout", () => ({ Splitter: () => null, RowSplitter: () => null, TabStrip: ({ children }: { children: React.ReactNode }) => children }));
vi.mock("./panels/ChatPanel", () => ({ ChatPanel: () => null }));
vi.mock("./panels/ChangesPanel", () => ({ ChangesPanel: () => null }));
vi.mock("./api", async (original) => ({ ...await original<typeof import("./api")>(), api: {
  projects: vi.fn(async () => [{ name: "p", pdk: null, pdk_requested: "sky130A@8afc8346a57f" }]),
  pdks: vi.fn(async () => []),
  project: vi.fn(),
  cells: vi.fn(async () => []),
  changesets: vi.fn(async () => []), live: vi.fn(async () => []),
  activity: vi.fn(async () => ({ records: [], last: 0, writer: null })),
  deps: vi.fn(),
  depsSync: vi.fn(), depsLock: vi.fn(),
  rescanPdks: vi.fn(),
  pdkFetchPlan: vi.fn(async () => new Promise(() => {})),
  confirmPdkFetch: vi.fn(),
  gitStatus: vi.fn(async () => ({ repo: false, setup: null, prefix: null, branch: null, head: null, operation: null, conflicted: [], cells: [], other: [], unsaved: [] })),
  gitLog: vi.fn(async () => ({ commits: [], more: false })),
} }));
const mocked = api as unknown as Record<keyof typeof api, ReturnType<typeof vi.fn>>;

const SKY = { id: "sky130A@8afc8346a57f", name: "sky130A", family: "sky130", version: "8afc8346a57fe1ab7934ba5a6056ea8b43078e71" };
const PDK_NONE: PdkLockState = { requested: null, locked: null, commit: null, installed: null, state: "none", changed: [], verified_by: null, ok: true };
const status = (over: Partial<DepsStatus> = {}): DepsStatus => ({ format: 1, libraries: [], tools: [], issues: [],
  warnings: [], notes: [], ok: true, pdk: { ...PDK_NONE }, ...over });
const dep = (name: string, state: string) => ({ name, kind: "dependency" as const, state, git: "https://example.org/ip.git",
  rev: "v1", subdir: null, locked: "0123456789abcdef0123456789abcdef01234567", installed: null, hash: "sha" as const, in_store: false });

beforeEach(async () => {
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key, value) });
  await setLocale("en");
  for (const k of ["project", "deps", "depsSync", "depsLock", "rescanPdks", "confirmPdkFetch"] as const) mocked[k].mockReset();
  mocked.pdkFetchPlan.mockClear();
  location.hash = "#/p/p";
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); location.hash = ""; });

const settle = async () => { await act(async () => {}); await act(async () => {}); };
const bar = () => within(document.querySelector(".topbar") as HTMLElement);

it("offers Fetch for a missing PDK, and Rescan reads the PDKs, the project and its lock status again", async () => {
  const missing = { name: "p", pdk: null, pdk_requested: SKY.id, libraries: [] };
  mocked.project.mockResolvedValueOnce(missing).mockResolvedValue({ ...missing, pdk: SKY });
  mocked.deps.mockResolvedValueOnce(status({ ok: false, issues: [`PDK ${SKY.id} is not installed here: run \`python -m undine pdk fetch p\``],
    pdk: { ...PDK_NONE, requested: SKY.id, state: "missing", ok: false } }))
    .mockResolvedValue(status({ pdk: { ...PDK_NONE, requested: SKY.id, locked: SKY.id, installed: SKY.id, state: "not pinned" } }));
  mocked.rescanPdks.mockResolvedValue({ pdks: [SKY] });
  render(<App />);
  await settle();

  const badge = bar().getByRole("button", { name: t("app:pdk.missing", { pdk: SKY.id }) });
  expect(badge.className).toContain("warn");
  fireEvent.click(badge);
  const menu = screen.getByRole("menu", { name: t("app:pdk.menu") });
  expect(within(menu).getByText(t("app:pdk.state.missing"))).toBeTruthy();
  fireEvent.click(within(menu).getByRole("menuitem", { name: t("app:pdk.fetch") }));
  await settle();
  expect(screen.getByRole("dialog", { name: t("app:pdkFetch.title") })).toBeTruthy();
  expect(mocked.pdkFetchPlan).toHaveBeenCalledWith("p", false);
  expect(mocked.confirmPdkFetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: t("common:actions.cancel") }));
  await settle();

  const projectReads = mocked.project.mock.calls.length, depsReads = mocked.deps.mock.calls.length;
  fireEvent.click(bar().getByRole("button", { name: t("app:pdk.missing", { pdk: SKY.id }) }));
  fireEvent.click(within(screen.getByRole("menu")).getByRole("menuitem", { name: t("app:pdk.rescan") }));
  await settle();
  expect(mocked.rescanPdks).toHaveBeenCalledTimes(1);
  expect(mocked.project.mock.calls.length).toBeGreaterThan(projectReads);
  expect(mocked.deps.mock.calls.length).toBeGreaterThan(depsReads);
  // the project now resolves to the PDK found: its badge names it, with no Fetch in its menu
  const found = bar().getByRole("button", { name: /sky130A/ });
  expect(found.className).not.toContain("warn");
  fireEvent.click(found);
  const after = screen.getByRole("menu");
  expect(within(after).queryByRole("menuitem", { name: t("app:pdk.fetch") })).toBeNull();
  expect(within(after).getByText(t("app:pdk.state.notPinned"))).toBeTruthy();
});

it("offers lock first for an unlocked dependency, and warnings and notes do not count in the badge", async () => {
  mocked.project.mockResolvedValue({ name: "p", pdk: null, pdk_requested: null, libraries: [] });
  mocked.deps.mockResolvedValue(status({ ok: false, libraries: [dep("ip", "unlocked")], issues: ["library ip: unlocked"],
    warnings: ["klayout: locked 0.28.17 from pixi, here 0.30.12 from pixi"], notes: ["origins, Undine version and pixi.lock are not recorded (format 1)"],
    tools: [{ name: "klayout", locked: { version: "0.28.17", from: "pixi" }, now: { version: "0.30.12", from: "pixi" }, now_path: "klayout", ok: false }] }));
  mocked.depsLock.mockResolvedValue({ ...status(), resolved: [{ name: "ip", commit: "0123456789abcdef0123456789abcdef01234567" }] });
  render(<App />);
  await settle();
  fireEvent.click(bar().getByRole("button", { name: new RegExp(t("app:lock.issues", { count: 1 })) }));
  await settle();
  const dialog = screen.getByRole("dialog", { name: t("app:lock.title") });
  const action = within(dialog).getByRole("combobox") as HTMLSelectElement;
  expect(action.value).toBe("lock");
  expect(dialog.textContent).toContain("undine.lock format 1");
  expect(dialog.textContent).toContain("sha, not in the store");
  expect(dialog.textContent).toContain("klayout: 0.28.17 from pixi (here 0.30.12 from pixi)");
  expect(dialog.textContent).toContain("~ klayout: locked 0.28.17");
  expect(dialog.textContent).toContain("note: origins");
  fireEvent.click(within(dialog).getByRole("button", { name: t("common:actions.run") }));
  await settle();
  expect(mocked.depsLock).toHaveBeenCalledWith("p");
  expect(mocked.depsSync).not.toHaveBeenCalled();
});

it("counts issues only, and offers sync first for a missing dependency, showing a refusal", async () => {
  mocked.project.mockResolvedValue({ name: "p", pdk: null, pdk_requested: null, libraries: [] });
  mocked.deps.mockResolvedValue(status({ ok: false, libraries: [dep("ip", "missing")], issues: ["library ip: missing"],
    warnings: ["w1", "w2"], notes: ["n1", "n2", "n3"] }));
  const refusal = "offline, and not in the store: ip @ 0123456789ab (sha 89abcdef0123): run the sync with the network";
  mocked.depsSync.mockRejectedValue(new ApiError(400, refusal));
  render(<App />);
  await settle();
  const badge = bar().getByRole("button", { name: new RegExp(t("app:lock.issues", { count: 1 })) });
  expect(badge.getAttribute("title")).toBe("library ip: missing");
  fireEvent.click(badge);
  await settle();
  const dialog = screen.getByRole("dialog", { name: t("app:lock.title") });
  expect((within(dialog).getByRole("combobox") as HTMLSelectElement).value).toBe("sync");
  fireEvent.click(within(dialog).getByRole("button", { name: t("common:actions.run") }));
  await settle();
  expect(mocked.depsSync).toHaveBeenCalledWith("p");
  expect(screen.getByRole("dialog", { name: t("app:lock.title") }).textContent).toContain(refusal);   // it stays open
});

it("counts no issue for warnings and notes alone", async () => {
  mocked.project.mockResolvedValue({ name: "p", pdk: null, pdk_requested: null, libraries: [] });
  mocked.deps.mockResolvedValue(status({ warnings: ["xschem: no version number in its banner"], notes: ["n"] }));
  render(<App />);
  await settle();
  const badge = bar().getByRole("button", { name: new RegExp(t("app:lock.locked")) });
  expect(badge.className).not.toContain("warn");
});
