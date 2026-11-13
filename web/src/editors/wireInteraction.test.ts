import { afterEach, describe, expect, it, vi } from "vitest";
import type { Schematic } from "../api";
import { Point, WirePreview, wireTarget } from "./wireInteraction";

function drawing(overrides: Partial<Schematic>): Schematic {
  return { wires: [], ...overrides } as Schematic;
}

describe("wire targets", () => {
  it("snaps to exact off-grid terminals using zoom-relative tolerance", () => {
    const d = drawing({ terminals: [["R1", "p", 13.5, 27.5]] });
    expect(wireTarget(d, 14, 28, 2)).toEqual({ point: [13.5, 27.5], connected: true });
    expect(wireTarget(d, 19, 28, 2)).toEqual({ point: [20, 30], connected: false });
    expect(wireTarget(d, 19, 28, 8)).toEqual({ point: [13.5, 27.5], connected: true });
  });

  it("prefers the closest terminal over an underlying wire, including legacy pins", () => {
    const d = drawing({ pins: [["R1", "p", 13.5, 27.5, "out"], ["R2", "p", 13.5, 34, "in"]],
      wires: [[0, 30, 40, 30, false, "", "w1"]] });
    expect(wireTarget(d, 14, 30, 8)).toEqual({ point: [13.5, 27.5], connected: true });
  });

  it("connects at existing wire interiors without moving the conductor's off-grid axis", () => {
    const d = drawing({ wires: [[13.5, 0, 13.5, 50, false, "", "w1"]] });
    expect(wireTarget(d, 15, 23, 4)).toEqual({ point: [13.5, 20], connected: true });
    expect(wireTarget(d, 15, 49, 4)).toEqual({ point: [13.5, 50], connected: true });
  });

  it("preserves diagonal legacy wire connections instead of snapping off the wire", () => {
    const d = drawing({ wires: [[0, 0, 30, 30, false, "", "w1"]] });
    expect(wireTarget(d, 14, 16, 4)).toEqual({ point: [15, 15], connected: true });
  });
});

afterEach(() => vi.useRealTimers());

describe("wire route preview", () => {
  it("coalesces pointer motion without restarting the deadline", async () => {
    vi.useFakeTimers();
    const route = vi.fn(async (_from: Point, to: Point): Promise<Point[]> => [[0, 0], to]);
    const preview = new WirePreview(route, vi.fn(), 10);
    preview.request([0, 0], [10, 0]);
    await vi.advanceTimersByTimeAsync(6);
    preview.request([0, 0], [20, 0]);          // still within the first deadline
    await vi.advanceTimersByTimeAsync(4);
    expect(route).toHaveBeenCalledTimes(1);
    expect(route).toHaveBeenCalledWith([0, 0], [20, 0]);
    preview.dispose();
  });

  it("keeps the shown route while the pointer moves, then shows routes in order", async () => {
    vi.useFakeTimers();
    const resolvers: ((points: Point[]) => void)[] = [];
    const route = vi.fn(() => new Promise<Point[]>((resolve) => resolvers.push(resolve)));
    const show = vi.fn();
    const preview = new WirePreview(route, show, 10);
    preview.request([0, 0], [20, 0]);
    await vi.advanceTimersByTimeAsync(10);
    resolvers[0]([[0, 0], [20, 0]]);
    await Promise.resolve();
    expect(show).toHaveBeenLastCalledWith([[0, 0], [20, 0]]);
    show.mockClear();
    preview.request([0, 0], [30, 0]);
    await vi.advanceTimersByTimeAsync(10);
    preview.request([0, 0], [40, 0]);          // queued behind the request in flight
    await vi.advanceTimersByTimeAsync(10);
    expect(show).not.toHaveBeenCalled();        // no blank frame between the routes
    resolvers[1]([[0, 0], [30, 0]]);
    await Promise.resolve();
    expect(show).toHaveBeenLastCalledWith([[0, 0], [30, 0]]);
    expect(route).toHaveBeenLastCalledWith([0, 0], [40, 0]);
    resolvers[2]([[0, 0], [40, 0]]);
    await Promise.resolve();
    expect(show).toHaveBeenLastCalledWith([[0, 0], [40, 0]]);
    expect(show.mock.calls.some(([p]) => p === null)).toBe(false);
    preview.dispose();
  });

  it("drops the route of an earlier start point", async () => {
    vi.useFakeTimers();
    let resolve!: (points: Point[]) => void;
    const show = vi.fn();
    const preview = new WirePreview(() => new Promise<Point[]>((r) => { resolve = r; }), show, 1);
    preview.request([0, 0], [50, 0]);
    await vi.advanceTimersByTimeAsync(1);
    preview.request([50, 0], [50, 50]);        // a segment was committed: the next one starts elsewhere
    expect(show).toHaveBeenLastCalledWith(null);
    resolve([[0, 0], [50, 0]]);
    await Promise.resolve();
    expect(show).toHaveBeenLastCalledWith(null);
    preview.dispose();
  });

  it("does not restore a cancelled route after Escape or a view change", async () => {
    vi.useFakeTimers();
    let resolve!: (points: Point[]) => void;
    const show = vi.fn();
    const preview = new WirePreview(() => new Promise<Point[]>((r) => { resolve = r; }), show, 1);
    preview.request([0, 0], [50, 50]);
    await vi.advanceTimersByTimeAsync(1);
    preview.clear();
    resolve([[0, 0], [50, 50]]);
    await Promise.resolve();
    expect(show.mock.calls.every(([p]) => p === null)).toBe(true);
    preview.dispose();
  });
});
