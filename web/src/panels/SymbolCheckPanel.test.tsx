// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type JobStatus, type SymbolCheckResult } from "../api";
import { setLocale, t } from "../i18n";
import { SymbolCheckPanel } from "./SymbolCheckPanel";

vi.mock("../api", async (original) => ({ ...await original<typeof import("../api")>(), api: {
  jobs: vi.fn(), jobFreshness: vi.fn(), symbolCheckResult: vi.fn(), startSymbolCheck: vi.fn(),
} }));

const toast = vi.fn();
const props = { project: "p", lib: "l", cell: "amp", toast };
const job = (id: string, cell = "amp", state: JobStatus["state"] = "done"): JobStatus => ({
  id, kind: "symbol-check", title: "Interface check", cell, lib: "l", state,
});
const pass: SymbolCheckResult = { match: true, total: 0, issues: [], schematic_ports: [], symbol_pins: [] };
const mismatch: SymbolCheckResult = {
  match: false, total: 1,
  issues: [{ code: "pin_order_mismatch", severity: "error", message: "server English diagnostic", expected: "IN, OUT", actual: "OUT, IN" }],
  schematic_ports: [{ name: "IN", direction: "in", index: 1 }, { name: "OUT", direction: "out", index: 2 }],
  symbol_pins: [{ name: "OUT", direction: "out", index: 1 }, { name: "IN", direction: "in", index: 2 }],
};
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}
beforeEach(async () => {
  vi.resetAllMocks();
  vi.mocked(api.jobs).mockResolvedValue([job("newer"), job("older")]);
  vi.mocked(api.jobFreshness).mockResolvedValue({ fresh: true, changed: [] });
  vi.mocked(api.symbolCheckResult).mockResolvedValue(pass);
  vi.mocked(api.startSymbolCheck).mockResolvedValue(job("created"));
  await setLocale("en");
});
afterEach(() => { cleanup(); vi.useRealTimers(); });
async function open(extra: Partial<typeof props> = {}) {
  const rendered = render(<SymbolCheckPanel {...props} {...extra} />);
  await act(async () => {});
  return rendered;
}

describe("schematic-symbol verification results", () => {
  it("shows translated mismatch, pin ordering evidence and stale history without changing the saved result", async () => {
    vi.mocked(api.symbolCheckResult).mockResolvedValue(mismatch);
    vi.mocked(api.jobFreshness).mockResolvedValue({ fresh: false, changed: ["symbol interface of l/amp changed"] });
    await open();
    expect(screen.getByText(t("symbolcheck:mismatch"))).toBeTruthy();
    expect(screen.getByText(t("symbolcheck:pin_order_mismatch"))).toBeTruthy();
    expect(screen.getByText("Expected: IN, OUT")).toBeTruthy();
    expect(screen.getByText("Actual: OUT, IN")).toBeTruthy();
    expect(screen.getAllByRole("table")).toHaveLength(2);
    expect(screen.getByText(t("verification:freshness.outdated"))).toBeTruthy();
    await act(async () => { await setLocale("ja"); });
    expect(screen.getByText(t("symbolcheck:pin_order_mismatch"))).toBeTruthy();
    expect(screen.getByText("期待値: IN, OUT")).toBeTruthy();
    expect(api.symbolCheckResult).toHaveBeenCalledTimes(1);
  });

  it("ignores a delayed result from the previously selected history entry", async () => {
    const oldResult = deferred<SymbolCheckResult>();
    vi.mocked(api.symbolCheckResult).mockImplementation(async (_p, id) => id === "newer" ? oldResult.promise : mismatch);
    await open();
    await act(async () => { fireEvent.change(screen.getByRole("combobox"), { target: { value: "older" } }); });
    expect(screen.getByText(t("symbolcheck:mismatch"))).toBeTruthy();
    await act(async () => { oldResult.resolve(pass); });
    expect(screen.getByText(t("symbolcheck:mismatch"))).toBeTruthy();
    expect(screen.queryByText(t("symbolcheck:match"))).toBeNull();
  });

  it("clears old history immediately on a cell change and auto-selects the new cell's history", async () => {
    const nextList = deferred<JobStatus[]>();
    vi.mocked(api.jobs).mockResolvedValueOnce([job("old-cell")]).mockImplementationOnce(() => nextList.promise);
    const ui = await open();
    expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("old-cell");
    ui.rerender(<SymbolCheckPanel {...props} cell="other" />);
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.queryByText(t("symbolcheck:match"))).toBeNull();
    await act(async () => { nextList.resolve([job("new-cell", "other")]); });
    expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("new-cell");
    expect(api.symbolCheckResult).toHaveBeenLastCalledWith("p", "new-cell");
  });

  it("ignores an old project's late job list instead of replacing current history", async () => {
    const previous = deferred<JobStatus[]>();
    vi.mocked(api.jobs).mockImplementation(async (project) => project === "p" ? previous.promise : [job("current")]);
    const ui = await open();
    ui.rerender(<SymbolCheckPanel {...props} project="proposal" />);
    await act(async () => {});
    expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("current");
    await act(async () => { previous.resolve([job("obsolete")]); });
    expect((screen.getByRole("combobox") as HTMLSelectElement).value).toBe("current");
    expect(screen.queryByRole("option", { name: /obsolete/ })).toBeNull();
    expect(api.symbolCheckResult).not.toHaveBeenCalledWith("proposal", "obsolete");
  });

  it("does not attach a late started job to a different project", async () => {
    const started = deferred<JobStatus>();
    vi.mocked(api.jobs).mockResolvedValue([]);
    vi.mocked(api.startSymbolCheck).mockReturnValue(started.promise);
    const ui = await open();
    fireEvent.click(screen.getByRole("button", { name: t("symbolcheck:run") }));
    ui.rerender(<SymbolCheckPanel {...props} project="proposal" />);
    await act(async () => { started.resolve(job("old-start")); });
    expect(api.symbolCheckResult).not.toHaveBeenCalled();
    expect((screen.getByRole("button", { name: t("symbolcheck:run") }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("polls an active run through completion and enables another check", async () => {
    vi.useFakeTimers();
    vi.mocked(api.jobs).mockResolvedValueOnce([job("active", "amp", "running")]).mockResolvedValue([job("active")]);
    await open();
    expect((screen.getByRole("button", { name: t("symbolcheck:run") }) as HTMLButtonElement).disabled).toBe(true);
    expect(api.symbolCheckResult).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(1200); });
    expect(screen.getByText(t("symbolcheck:match"))).toBeTruthy();
    expect((screen.getByRole("button", { name: t("symbolcheck:run") }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("does not let an older poll response overwrite a newer completed result", async () => {
    vi.useFakeTimers();
    const older = deferred<JobStatus[]>();
    vi.mocked(api.jobs).mockResolvedValueOnce([job("active", "amp", "running")])
      .mockImplementationOnce(() => older.promise).mockResolvedValue([job("active")]);
    await open();
    await act(async () => { await vi.advanceTimersByTimeAsync(2400); });
    expect(screen.getByText(t("symbolcheck:match"))).toBeTruthy();
    await act(async () => { older.resolve([job("active", "amp", "running")]); });
    expect(screen.getByText(t("symbolcheck:match"))).toBeTruthy();
  });

  it("can display a completed poll while a later slow poll is still pending", async () => {
    vi.useFakeTimers();
    const completed = deferred<JobStatus[]>();
    const later = deferred<JobStatus[]>();
    vi.mocked(api.jobs).mockResolvedValueOnce([job("active", "amp", "running")])
      .mockImplementationOnce(() => completed.promise).mockImplementation(() => later.promise);
    await open();
    await act(async () => { await vi.advanceTimersByTimeAsync(2400); completed.resolve([job("active")]); });
    expect(screen.getByText(t("symbolcheck:match"))).toBeTruthy();
    await act(async () => { later.resolve([job("active")]); });
  });
});
