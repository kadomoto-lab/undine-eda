// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, ApiError, needsFetchOf, type DepFetchJob, type FetchPlan, type FetchRow } from "./api";
import { setLocale, t } from "./i18n";
import { FetchConfirmDialog, sizeText } from "./FetchConfirmDialog";

// The dependency Fetch dialog (docs/design/packages.md, Confirmation token) shows the plan a 409 needs_fetch carried
// (URL, commit, tree, size, licence, a warning for a host not allowed), asks the server for a token only after the
// person clicks Fetch, starts the job and follows it with approximate progress; a second round shows the next plan.
vi.mock("./api", async (original) => ({ ...await original<typeof import("./api")>(), api: {
  confirmDependencyFetch: vi.fn(), depsFetch: vi.fn(), depsFetchStatus: vi.fn(),
} }));
const mocked = api as unknown as Record<"confirmDependencyFetch" | "depsFetch" | "depsFetchStatus", ReturnType<typeof vi.fn>>;

const COMMIT = "e74fc5d69ead606233aa7319d58db7dd05555c30";
const TREE = "sha256:" + "9".repeat(64);
const row = (more: Partial<FetchRow> = {}): FetchRow => ({ url: "https://git.example.org/amp.git", rev: null, commit: COMMIT,
  subdir: null, tree: TREE, size: 4096, license: "MIT", package: "amp", version: "1.0.0", host: "git.example.org",
  allowed: true, ...more });
const PLAN: FetchPlan = { op: "add", fetches: [row()], complete: true, project: "p",
  request: { op: "add", name: "amp", package: "amp", adopt: true } };
const job = (state: DepFetchJob["state"], more: Partial<DepFetchJob> = {}): DepFetchJob =>
  ({ id: "j1", project: "p", kind: "dependency", state, log: [], progress: null, result: null, error: null, code: null,
    created: 0, ended: null, ...more });

beforeEach(async () => {
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key, value) });
  await setLocale("en");
  for (const f of Object.values(mocked)) f.mockReset();
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const settle = async () => { for (let i = 0; i < 4; i++) await act(async () => {}); };

it("shows the plan, asks for a token only after the click, then follows the job to its result", async () => {
  mocked.confirmDependencyFetch.mockResolvedValue({ token: "tok", expires: 0, plan: PLAN });
  mocked.depsFetch.mockResolvedValue({ job: job("queued") });
  let release = () => {};
  mocked.depsFetchStatus
    .mockImplementationOnce(() => new Promise((r) => { release = () => r(job("running", { progress: { url: PLAN.fetches[0].url, bytes: 2500, limit: 1e9 } })); }))
    .mockResolvedValue(job("done", { result: { changeset: "cs-000001", status: "applied", result: { name: "amp" } } }));
  const onDone = vi.fn();
  render(<FetchConfirmDialog plan={PLAN} onClose={() => {}} onDone={onDone} pollMs={1} />);
  await settle();
  for (const text of [PLAN.fetches[0].url, COMMIT, TREE, "4.1 kB", "MIT", "amp 1.0.0"]) expect(screen.getByText(text)).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();                    // an allowed host: no warning
  expect(mocked.confirmDependencyFetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: t("app:depFetch.start") }));
  await settle();
  expect(mocked.confirmDependencyFetch).toHaveBeenCalledWith(PLAN);
  expect(mocked.depsFetch).toHaveBeenCalledWith("p", "tok");
  await waitFor(() => expect(mocked.depsFetchStatus).toHaveBeenCalledWith("p", "j1"));
  await act(async () => { release(); });
  await settle();
  await waitFor(() => expect(onDone).toHaveBeenCalledWith({ changeset: "cs-000001", status: "applied", result: { name: "amp" } }));
  expect(mocked.confirmDependencyFetch).toHaveBeenCalledTimes(1);
});

it("shows approximate progress while the job runs", async () => {
  mocked.confirmDependencyFetch.mockResolvedValue({ token: "tok", expires: 0, plan: PLAN });
  mocked.depsFetch.mockResolvedValue({ job: job("running", { progress: { url: PLAN.fetches[0].url, bytes: 2_500_000, limit: 1e9 } }) });
  mocked.depsFetchStatus.mockImplementation(() => new Promise(() => {}));        // still running
  render(<FetchConfirmDialog plan={PLAN} onClose={() => {}} onDone={() => {}} pollMs={1} />);
  await settle();
  fireEvent.click(screen.getByRole("button", { name: t("app:depFetch.start") }));
  await settle();
  expect(screen.getByTestId("fetch-progress").textContent).toBe(t("app:depFetch.bytes", { size: "2.5 MB" }));
  expect(screen.getByText(t("app:depFetch.background"))).toBeTruthy();
});

it("warns for a host the server does not fetch from without asking, and a rev whose commit comes with the fetch", async () => {
  const plan: FetchPlan = { ...PLAN, complete: false, fetches: [row({ url: "https://other.example/ip.git", rev: "v1", commit: null,
    tree: null, size: null, license: null, package: null, version: null, host: "other.example", allowed: false })] };
  render(<FetchConfirmDialog plan={plan} onClose={() => {}} onDone={() => {}} />);
  await settle();
  expect(screen.getByRole("alert").textContent).toContain(t("app:depFetch.hostWarning", { host: "other.example" }));
  expect(screen.getByText(t("app:depFetch.commitAfter"))).toBeTruthy();
  expect(screen.getByText("v1")).toBeTruthy();
  expect(screen.getByText(t("app:depFetch.incomplete"))).toBeTruthy();
});

it("a fetch that shows more to fetch: the next plan is shown, and nothing more is asked for until the next click", async () => {
  const a = row({ url: "https://git.example.org/a.git", rev: "v1", commit: null, tree: null, size: null, license: null });
  const plan: FetchPlan = { ...PLAN, complete: false, fetches: [a] };
  const next: FetchPlan = { ...plan, fetches: [{ ...a, commit: COMMIT }, row({ url: "https://git.example.org/b.git", rev: "v1", commit: null,
    tree: null, size: null, license: null })] };
  mocked.confirmDependencyFetch.mockResolvedValue({ token: "tok", expires: 0, plan });
  mocked.depsFetch.mockResolvedValue({ job: job("failed", { code: "needs_fetch", plan: next, error: "this needs 2 fetches" }) });
  const onDone = vi.fn();
  render(<FetchConfirmDialog plan={plan} onClose={() => {}} onDone={onDone} pollMs={1} />);
  await settle();
  fireEvent.click(screen.getByRole("button", { name: t("app:depFetch.start") }));
  await settle();
  expect(screen.getByText(t("app:depFetch.again"))).toBeTruthy();
  expect(screen.getAllByTestId("fetch-row")).toHaveLength(2);
  expect(screen.getByText("https://git.example.org/b.git")).toBeTruthy();
  expect(screen.getByText(t("app:depFetch.lastSeen", { commit: COMMIT }))).toBeTruthy();
  expect(mocked.confirmDependencyFetch).toHaveBeenCalledTimes(1);
  mocked.depsFetch.mockResolvedValue({ job: job("done", { result: { ok: true } }) });
  fireEvent.click(screen.getByRole("button", { name: t("app:depFetch.start") }));
  await settle();
  expect(mocked.confirmDependencyFetch).toHaveBeenLastCalledWith(next);
  await waitFor(() => expect(onDone).toHaveBeenCalledWith({ ok: true }));
});

it("asks for nothing when cancelled, and shows a refused confirmation", async () => {
  const onClose = vi.fn();
  render(<FetchConfirmDialog plan={PLAN} onClose={onClose} onDone={() => {}} />);
  await settle();
  mocked.confirmDependencyFetch.mockRejectedValue(new ApiError(400, "project p would fetch nothing, not what was confirmed: show the fetch dialog again"));
  fireEvent.click(screen.getByRole("button", { name: t("app:depFetch.start") }));
  await settle();
  expect(screen.getByText(/show the fetch dialog again/)).toBeTruthy();
  expect(mocked.depsFetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: t("common:actions.cancel") }));
  expect(onClose).toHaveBeenCalled();
});

it("reads a needs_fetch refusal's plan, and sizes in words", () => {
  expect(needsFetchOf(new ApiError(409, "x", "needs_fetch", { plan: PLAN }))).toEqual(PLAN);
  expect(needsFetchOf(new ApiError(409, "x", "resolution_refused", { plan: PLAN }))).toBeNull();
  expect(needsFetchOf(new Error("x"))).toBeNull();
  expect([sizeText(512), sizeText(4096), sizeText(12_345_678), sizeText(3e9)]).toEqual(["512 B", "4.1 kB", "12 MB", "3.0 GB"]);
});
