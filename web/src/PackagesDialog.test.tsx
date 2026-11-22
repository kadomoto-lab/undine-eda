// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, ApiError, type PackageInfo, type PackageVersion } from "./api";
import { setLocale, t } from "./i18n";
import { PackagesDialog } from "./PackagesDialog";

// The Packages section of the library browser (docs/design/packages.md, UI): search an index, read a package's
// versions and PDK fit, add it by name and requirement; refresh fetches the index again, after the person saw its URL
// and clicked Fetch (phase 4: a read never fetches an index, and an add from another machine is confirmed first).
vi.mock("./api", async (original) => ({ ...await original<typeof import("./api")>(), api: {
  indexes: vi.fn(), packages: vi.fn(), packageInfo: vi.fn(), refreshIndex: vi.fn(), addPackageInChangeset: vi.fn(),
  confirmDependencyFetch: vi.fn(), depsFetch: vi.fn(), depsFetchStatus: vi.fn(),
} }));
const mocked = api as unknown as Record<"indexes" | "packages" | "packageInfo" | "refreshIndex" | "addPackageInChangeset"
  | "confirmDependencyFetch" | "depsFetch" | "depsFetchStatus", ReturnType<typeof vi.fn>>;
// the add runs in a small changeset of its own, adopted at once (POST /deps/changeset)
const adopted = (result: unknown) => ({ changeset: "cs-000001", status: "applied", result });

const WHERE = { index: "https://github.com/kadomoto-lab/undine-index.git", index_name: "undine", commit: "c".repeat(40) };
const version = (v: string, more: Partial<PackageVersion> = {}): PackageVersion => ({
  name: "amp", version: v, description: "a two-stage amplifier", license: "Apache-2.0", yanked: false,
  fits: { ok: true, reason: null }, git: "https://github.com/me/amp.git", subdir: null, commit: "a".repeat(40),
  tree: "sha256:" + "b".repeat(64), size: null, pdk: null, exports: ["amp", "bias"],
  deps: [{ name: "common", package: "sar_common", req: "^1.2", index: null, index_name: null }], ...more });
const INFO: PackageInfo = { ...WHERE, name: "amp", newest: "1.1.0", locked: null,
  versions: [version("2.0.0", { yanked: true }), version("1.1.0"), version("1.0.0", { fits: { ok: false, reason: "family gf180mcu, this project is sky130" } })] };

beforeEach(async () => {
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key, value) });
  await setLocale("en");
  for (const f of Object.values(mocked)) f.mockReset();
  mocked.indexes.mockResolvedValue({ indexes: [{ name: "undine", url: WHERE.index, commit: WHERE.commit },
    { name: "acme", url: "git@git.acme.example:idx.git", commit: null }] });
  mocked.packages.mockResolvedValue({ ...WHERE, packages: [
    { name: "amp", version: "1.1.0", description: "a two-stage amplifier", license: "Apache-2.0", yanked: false, fits: { ok: true, reason: null } },
    { name: "bias", version: "0.3.0", description: null, license: null, yanked: false, fits: { ok: false, reason: "this project has no PDK" } }] });
  mocked.packageInfo.mockResolvedValue(INFO);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const settle = async () => { for (let i = 0; i < 4; i++) await act(async () => {}); };

it("searches, shows a package and adds it with the requirement given", async () => {
  const onAdded = vi.fn();
  render(<PackagesDialog project="p" readOnly={false} taken={() => false} onClose={() => {}} onAdded={onAdded} debounceMs={0} />);
  await settle();
  expect(mocked.packages).toHaveBeenCalledWith("", "undine", "p");
  expect(screen.getByText(t("app:packages.copy", { index: "undine", commit: "c".repeat(12) }))).toBeTruthy();
  fireEvent.change(screen.getByPlaceholderText(t("app:packages.search")), { target: { value: "amp" } });
  await settle();
  expect(mocked.packages).toHaveBeenLastCalledWith("amp", "undine", "p");

  fireEvent.click(screen.getByText("amp", { selector: "b" }));
  await settle();
  expect(mocked.packageInfo).toHaveBeenCalledWith("amp", "undine", "p");
  for (const text of ["amp, bias", "sar_common ^1.2 as common", t("app:packages.fits"), "Apache-2.0"]) expect(screen.getByText(text)).toBeTruthy();
  expect(screen.getByText("2.0.0").className).toContain("struck");             // yanked
  expect(screen.getByText("1.0.0").title).toContain("family gf180mcu");

  fireEvent.click(screen.getByText("1.0.0"));                                   // requires exactly it
  expect((screen.getByPlaceholderText("^1.1.0") as HTMLInputElement).value).toBe("=1.0.0");
  mocked.addPackageInChangeset.mockResolvedValue(adopted({ name: "amp", commit: "a".repeat(40), cells: ["amp"], package: "amp", version: "1.0.0" }));
  fireEvent.click(screen.getByRole("button", { name: t("app:packages.add", { name: "amp" }) }));
  await settle();
  expect(mocked.addPackageInChangeset).toHaveBeenCalledWith("p", "amp", "amp", "=1.0.0", undefined);
  expect(onAdded).toHaveBeenCalledWith("amp", expect.objectContaining({ version: "1.0.0" }), expect.objectContaining({ status: "applied" }));
});

it("adds from another index by its name, and refuses a local name the project has", async () => {
  const taken = (n: string) => n === "amp";
  render(<PackagesDialog project="p" readOnly={false} taken={taken} onClose={() => {}} onAdded={() => {}} debounceMs={0} />);
  await settle();
  fireEvent.change(screen.getByLabelText(t("app:packages.index")), { target: { value: "acme" } });
  await settle();
  expect(mocked.packages).toHaveBeenLastCalledWith("", "acme", "p");
  fireEvent.click(screen.getByText("amp", { selector: "b" }));
  await settle();
  expect(screen.getByText(t("app:library.exists"))).toBeTruthy();
  const addAmp = screen.getByRole("button", { name: t("app:packages.add", { name: "amp" }) }) as HTMLButtonElement;
  expect(addAmp.disabled).toBe(true);
  fireEvent.change(screen.getByDisplayValue("amp"), { target: { value: "amp2" } });
  mocked.addPackageInChangeset.mockResolvedValue(adopted({ name: "amp2", commit: "a".repeat(40), cells: [] }));
  fireEvent.click(screen.getByRole("button", { name: t("app:packages.add", { name: "amp2" }) }));
  await settle();
  expect(mocked.addPackageInChangeset).toHaveBeenCalledWith("p", "amp2", "amp", undefined, "acme");
});

it("hands an add whose adopt was refused to onKept: its changeset stays open", async () => {
  const onAdded = vi.fn(), onKept = vi.fn();
  render(<PackagesDialog project="p" readOnly={false} taken={() => false} onClose={() => {}} onAdded={onAdded} onKept={onKept} debounceMs={0} />);
  await settle();
  fireEvent.click(screen.getByText("amp", { selector: "b" }));
  await settle();
  const refused = new ApiError(409, "cannot apply cs-000002: conflict in undine.lock: ...", "merge_conflict", { changeset: "cs-000002" });
  mocked.addPackageInChangeset.mockRejectedValue(refused);
  fireEvent.click(screen.getByRole("button", { name: t("app:packages.add", { name: "amp" }) }));
  await settle();
  expect(onKept).toHaveBeenCalledWith(refused, "cs-000002");
  expect(onAdded).not.toHaveBeenCalled();
});

it("refreshes the index after showing its URL, and reads the search again; a refusal is shown", async () => {
  render(<PackagesDialog project="p" readOnly={false} taken={() => false} onClose={() => {}} onAdded={() => {}} debounceMs={0} />);
  await settle();
  mocked.refreshIndex.mockResolvedValue({ ...WHERE, commit: "d".repeat(40) });
  const calls = mocked.packages.mock.calls.length;
  fireEvent.click(screen.getByTitle(t("app:packages.refresh")));
  await settle();
  // the URL first; the token (refreshIndex asks for it) only on Fetch
  expect(screen.getByText(t("app:packages.fetchIndexFrom", { index: "undine", url: WHERE.index }))).toBeTruthy();
  expect(mocked.refreshIndex).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: t("app:packages.fetchIndexStart") }));
  await settle();
  expect(mocked.refreshIndex).toHaveBeenCalledWith("undine", "p");
  await waitFor(() => expect(mocked.packages.mock.calls.length).toBeGreaterThan(calls));
  mocked.refreshIndex.mockRejectedValue(new ApiError(400, "index https://idx.evil.example/x.git: not from idx.evil.example", "invalid_request"));
  fireEvent.click(screen.getByTitle(t("app:packages.refresh")));
  await settle();
  fireEvent.click(screen.getByRole("button", { name: t("app:packages.fetchIndexStart") }));
  await settle();
  expect(screen.getByText(/not from idx\.evil\.example/)).toBeTruthy();
});

it("an index with no local copy is said, never fetched until the person clicks Fetch", async () => {
  const url = "https://github.com/kadomoto-lab/undine-index.git";
  mocked.packages.mockRejectedValue(new ApiError(409, "index undine has not been fetched on this machine", "index_not_fetched",
    { index: "undine", url, host: "github.com" }));
  render(<PackagesDialog project="p" readOnly={false} taken={() => false} onClose={() => {}} onAdded={() => {}} debounceMs={0} />);
  await settle();
  expect(screen.getByTestId("index-not-fetched").textContent).toContain(t("app:packages.notFetched", { index: "undine" }));
  expect(screen.queryByText(/has not been fetched on this machine$/)).toBeNull();      // said in words, not as an error
  fireEvent.click(screen.getByRole("button", { name: t("app:packages.fetchIndex", { index: "undine" }) }));
  await settle();
  expect(screen.getByText(t("app:packages.fetchIndexHost", { host: "github.com" }))).toBeTruthy();
  expect(mocked.refreshIndex).not.toHaveBeenCalled();
  mocked.packages.mockResolvedValue({ ...WHERE, packages: [] });
  mocked.refreshIndex.mockResolvedValue(WHERE);
  fireEvent.click(screen.getByRole("button", { name: t("app:packages.fetchIndexStart") }));
  await settle();
  expect(mocked.refreshIndex).toHaveBeenCalledWith("undine", "p");
  await waitFor(() => expect(screen.queryByTestId("index-not-fetched")).toBeNull());
});

it("an add that needs a fetch from another machine opens the Fetch dialog, and adds once the fetch ran", async () => {
  const onAdded = vi.fn();
  render(<PackagesDialog project="p" readOnly={false} taken={() => false} onClose={() => {}} onAdded={onAdded} debounceMs={0} />);
  await settle();
  fireEvent.click(screen.getByText("amp", { selector: "b" }));
  await settle();
  const plan = { op: "add", complete: true, project: "p", request: { op: "add", name: "amp", package: "amp", adopt: true },
    fetches: [{ url: "https://github.com/me/amp.git", rev: null, commit: "a".repeat(40), subdir: null, tree: "sha256:" + "b".repeat(64),
      size: 2048, license: "Apache-2.0", package: "amp", version: "1.1.0", host: "github.com", allowed: true }] };
  mocked.addPackageInChangeset.mockRejectedValue(new ApiError(409, "this needs 1 fetch a person confirms first", "needs_fetch", { plan }));
  fireEvent.click(screen.getByRole("button", { name: t("app:packages.add", { name: "amp" }) }));
  await settle();
  expect(screen.getByRole("dialog", { name: t("app:depFetch.title") })).toBeTruthy();
  expect(onAdded).not.toHaveBeenCalled();
  expect(mocked.confirmDependencyFetch).not.toHaveBeenCalled();
  const result = { name: "amp", commit: "a".repeat(40), cells: ["amp"], package: "amp", version: "1.1.0" };
  mocked.confirmDependencyFetch.mockResolvedValue({ token: "tok", expires: 0, plan });
  mocked.depsFetch.mockResolvedValue({ job: { id: "j1", project: "p", kind: "dependency", state: "done", log: [], progress: null,
    result: adopted(result), error: null, code: null, created: 0, ended: 0 } });
  fireEvent.click(screen.getByRole("button", { name: t("app:depFetch.start") }));
  await settle();
  expect(mocked.depsFetch).toHaveBeenCalledWith("p", "tok");
  await waitFor(() => expect(onAdded).toHaveBeenCalledWith("amp", result, expect.objectContaining({ status: "applied" })));
});

it("in a preview it shows packages and adds none", async () => {
  render(<PackagesDialog project="p" readOnly taken={() => false} onClose={() => {}} onAdded={() => {}} debounceMs={0} />);
  await settle();
  fireEvent.click(screen.getByText("amp", { selector: "b" }));
  await settle();
  expect(screen.getByText(t("app:packages.readOnly"))).toBeTruthy();
  expect(screen.queryByRole("button", { name: t("app:packages.add", { name: "amp" }) })).toBeNull();
});
