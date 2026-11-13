import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MovePreview, MoveRequest, MoveResult } from "./moveInteraction";

const request = (dx: number): MoveRequest => ({ items: [{ k: "i", id: "R1" }], dx, dy: 0 });
const result = (x: number) => ({ result: ["R1"], view: { insts: [{ id: "R1", x }] } }) as MoveResult;
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (reason: Error) => void;
  const promise = new Promise<T>((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
}

beforeEach(() => vi.useFakeTimers());
afterEach(() => vi.useRealTimers());

describe("move preview scheduling", () => {
  it("keeps providing previews during continuous pointer movement", async () => {
    const fetch = vi.fn(async (input: MoveRequest) => result(input.dx)), show = vi.fn();
    const preview = new MovePreview(fetch, show);
    for (let dx = 10; dx <= 90; dx += 10) {
      preview.request(request(dx));
      await vi.advanceTimersByTimeAsync(10);
    }
    expect(fetch.mock.calls.map(([input]) => input.dx)).toEqual([30, 60, 90]);
    expect(show).toHaveBeenLastCalledWith({ result: result(90), status: "ready" });
    preview.dispose();
  });

  it("shows completed geometry during continuous movement even when requests take longer than pointer updates", async () => {
    const fetch = vi.fn(async (input: MoveRequest) => {
      await new Promise((resolve) => setTimeout(resolve, 25));
      return result(input.dx);
    });
    const show = vi.fn(), preview = new MovePreview(fetch, show);
    for (let dx = 10; dx <= 90; dx += 10) {
      preview.request(request(dx));
      await vi.advanceTimersByTimeAsync(10);
    }
    expect(show).toHaveBeenCalledWith({ result: result(30), status: "pending" });
    expect(show).toHaveBeenCalledWith({ result: result(60), status: "pending" });
    expect(fetch.mock.calls.map(([input]) => input.dx)).toEqual([30, 60, 90]);
    preview.dispose();
    await vi.runAllTimersAsync();
  });

  it("coalesces positions with one in-flight request and marks intermediate geometry as pending", async () => {
    const first = deferred<MoveResult>(), latest = deferred<MoveResult>();
    const fetch = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(latest.promise);
    const show = vi.fn();
    const preview = new MovePreview(fetch, show);
    preview.request(request(10));
    preview.request(request(20));
    await vi.advanceTimersByTimeAsync(30);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenLastCalledWith(request(20));
    preview.request(request(30));
    preview.request(request(40));
    await vi.advanceTimersByTimeAsync(30);
    expect(fetch).toHaveBeenCalledTimes(1);
    first.resolve(result(20));
    await vi.advanceTimersByTimeAsync(0);
    expect(fetch).toHaveBeenLastCalledWith(request(40));
    expect(show).toHaveBeenLastCalledWith({ result: result(20), status: "pending" });
    latest.resolve(result(40));
    await vi.advanceTimersByTimeAsync(0);
    expect(show).toHaveBeenLastCalledWith({ result: result(40), status: "ready" });
    preview.dispose();
  });

  it("retains the last valid geometry while pending, blocked, and committing", async () => {
    const blocked = deferred<MoveResult>(), late = deferred<MoveResult>();
    const fetch = vi.fn().mockResolvedValueOnce(result(10)).mockReturnValueOnce(blocked.promise).mockReturnValueOnce(late.promise);
    const show = vi.fn();
    const preview = new MovePreview(fetch, show);
    preview.request(request(10));
    await vi.advanceTimersByTimeAsync(30);
    preview.request(request(20));
    expect(show).toHaveBeenLastCalledWith({ result: result(10), status: "pending" });
    await vi.advanceTimersByTimeAsync(30);
    blocked.reject(new Error("blocked"));
    await vi.advanceTimersByTimeAsync(0);
    expect(show).toHaveBeenLastCalledWith({ result: result(10), status: "blocked" });
    preview.request(request(30));
    await vi.advanceTimersByTimeAsync(30);
    preview.hold();
    late.resolve(result(30));
    await vi.advanceTimersByTimeAsync(0);
    expect(show).toHaveBeenLastCalledWith({ result: result(10), status: "committing" });
    preview.clear();
    expect(show).toHaveBeenLastCalledWith({ result: null, status: "idle" });
    preview.dispose();
  });

  it.each(["clear", "dispose"] as const)("%s prevents queued and late work from restoring a cancelled drag", async (cancel) => {
    const pending = deferred<MoveResult>();
    const fetch = vi.fn().mockReturnValue(pending.promise), show = vi.fn();
    const preview = new MovePreview(fetch, show);
    preview.request(request(10));
    await vi.advanceTimersByTimeAsync(30);
    preview.request(request(20));
    preview[cancel]();
    const calls = show.mock.calls.length;
    pending.resolve(result(10));
    await vi.advanceTimersByTimeAsync(60);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(show).toHaveBeenCalledTimes(calls);
  });
});
