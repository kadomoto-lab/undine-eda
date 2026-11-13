// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, ApiError, type PdkFetchJob } from "./api";
import { setLocale, t } from "./i18n";
import { PdkFetchDialog } from "./PdkFetchDialog";

// The Fetch dialog shows the plan, and asks the server for a confirmation token only after the person clicks Fetch
// (docs/design/packages.md, Confirmation token); cancelling asks for nothing.
vi.mock("./api", async (original) => ({ ...await original<typeof import("./api")>(), api: {
  pdkFetchPlan: vi.fn(), confirmPdkFetch: vi.fn(), pdkFetch: vi.fn(), pdkFetchStatus: vi.fn(),
} }));
const mocked = api as unknown as Record<"pdkFetchPlan" | "confirmPdkFetch" | "pdkFetch" | "pdkFetchStatus", ReturnType<typeof vi.fn>>;

const COMMIT = "8afc8346a57fe1ab7934ba5a6056ea8b43078e71";
const PLAN = { family: "sky130", variant: "sky130A", commit: COMMIT, libraries: null,
  source: "static-web:https://fossi-foundation.github.io/ciel-releases", root: "/home/me/.ciel" };
const job = (state: PdkFetchJob["state"], log: string[], result: PdkFetchJob["result"] = null): PdkFetchJob =>
  ({ id: "j1", project: "p", state, log, result, error: null, created: 0, ended: null });

beforeEach(async () => {
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key, value) });
  await setLocale("en");
  for (const f of Object.values(mocked)) f.mockReset();
  mocked.pdkFetchPlan.mockResolvedValue(PLAN);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const settle = async () => { await act(async () => {}); await act(async () => {}); };

it("asks for a token only after the click, then starts the job and shows its progress", async () => {
  mocked.confirmPdkFetch.mockResolvedValue({ token: "tok", expires: 0, plan: PLAN });
  mocked.pdkFetch.mockResolvedValue({ job: job("queued", []) });
  mocked.pdkFetchStatus
    .mockResolvedValueOnce(job("running", ["Downloading sky130A..."]))
    .mockResolvedValue(job("done", ["Downloading sky130A...", "Done."], { path: "/home/me/.ciel/sky130A", verify: { state: "not pinned" } }));
  const onFetched = vi.fn();
  render(<PdkFetchDialog project="p" onClose={() => {}} onFetched={onFetched} pollMs={1} />);
  await settle();
  // the plan, as it will run: ciel's default libraries, the root it installs under
  expect(mocked.pdkFetchPlan).toHaveBeenCalledWith("p", false);
  for (const text of ["sky130", "sky130A", COMMIT, PLAN.source, PLAN.root, t("app:pdkFetch.cielDefault")]) {
    expect(screen.getByText(text)).toBeTruthy();
  }
  expect(mocked.confirmPdkFetch).not.toHaveBeenCalled();
  expect(mocked.pdkFetch).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: t("app:pdkFetch.start") }));
  await settle();
  expect(mocked.confirmPdkFetch).toHaveBeenCalledWith("p", { commit: COMMIT, variant: "sky130A", libraries: null, allow_network: false });
  expect(mocked.pdkFetch).toHaveBeenCalledWith("p", "tok");
  await waitFor(() => expect(onFetched).toHaveBeenCalledTimes(1));
  expect(mocked.pdkFetchStatus).toHaveBeenCalledWith("p", "j1");
  expect(screen.getByTestId("pdk-fetch-log").textContent).toContain("Done.");
  expect(screen.getByText(new RegExp(t("app:pdkFetch.state.done")))).toBeTruthy();
  expect(mocked.confirmPdkFetch).toHaveBeenCalledTimes(1);
});

it("asks for nothing when cancelled", async () => {
  const onClose = vi.fn();
  render(<PdkFetchDialog project="p" onClose={onClose} onFetched={() => {}} />);
  await settle();
  fireEvent.click(screen.getByRole("button", { name: t("common:actions.cancel") }));
  expect(onClose).toHaveBeenCalled();
  expect(mocked.confirmPdkFetch).not.toHaveBeenCalled();
  expect(mocked.pdkFetch).not.toHaveBeenCalled();
});

it("plans again with the network allowed when a 12-character id is not installed here", async () => {
  const offline = "sky130A@8afc8346a57f: no installed PDK has a commit starting 8afc8346a57f, and the network is not allowed";
  mocked.pdkFetchPlan.mockReset();
  mocked.pdkFetchPlan.mockImplementation(async (_p: string, network: boolean) => {
    if (!network) throw new ApiError(400, offline);
    return PLAN;
  });
  mocked.confirmPdkFetch.mockResolvedValue({ token: "tok", expires: 0, plan: PLAN });
  mocked.pdkFetch.mockResolvedValue({ job: job("failed", ["ciel: no such commit"]) });
  render(<PdkFetchDialog project="p" onClose={() => {}} onFetched={() => {}} />);
  await settle();
  expect(screen.getByText(new RegExp("the network is not allowed"))).toBeTruthy();
  expect((screen.getByRole("button", { name: t("app:pdkFetch.start") }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("checkbox", { name: t("app:pdkFetch.network") }));
  await settle();
  expect(mocked.pdkFetchPlan).toHaveBeenLastCalledWith("p", true);
  fireEvent.click(screen.getByRole("button", { name: t("app:pdkFetch.start") }));
  await settle();
  expect(mocked.confirmPdkFetch).toHaveBeenCalledWith("p", expect.objectContaining({ allow_network: true }));
  expect(screen.getByText(new RegExp(t("app:pdkFetch.state.failed")))).toBeTruthy();
  expect(mocked.pdkFetchStatus).not.toHaveBeenCalled();     // a failed job is not followed
});
