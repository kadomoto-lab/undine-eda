import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { TileCache, type TileReq } from "./tiles";

const req: TileReq = { key: "p|1|null|99|0|0|0", pos: "0|0|0", url: "/tile/0/0/0.png?v=1", prio: 0 };
const bitmap = { close: vi.fn() } as unknown as ImageBitmap;
const settle = () => vi.advanceTimersByTimeAsync(0);

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("createImageBitmap", vi.fn(async () => bitmap));
});
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

it("asks again for a tile that failed, after a pause that grows, and shows it once it comes", async () => {
  const fetch = vi.fn()
    .mockRejectedValueOnce(new TypeError("Failed to fetch"))                          // the server is restarting
    .mockResolvedValueOnce(new Response("busy", { status: 503 }))
    .mockResolvedValue(new Response(new Blob(["png"])));
  vi.stubGlobal("fetch", fetch);
  const redraw = vi.fn();
  const tiles = new TileCache(redraw);
  const draw = () => tiles.want(tiles.get(req.key) ? [] : [req]);                     // what the editor's draw does

  draw();
  await settle();
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(tiles.get(req.key)).toBeNull();
  draw();                                                                              // a redraw right away: still waits
  await settle();
  expect(fetch).toHaveBeenCalledTimes(1);

  await vi.advanceTimersByTimeAsync(1000);                                             // the pause asks for a redraw
  expect(redraw).toHaveBeenCalledTimes(1);
  draw();
  await settle();
  expect(fetch).toHaveBeenCalledTimes(2);                                              // ... and fails again (503)

  await vi.advanceTimersByTimeAsync(1000);
  draw();
  await settle();
  expect(fetch).toHaveBeenCalledTimes(2);                                              // the second pause is longer
  await vi.advanceTimersByTimeAsync(1000);
  draw();
  await settle();
  expect(fetch).toHaveBeenCalledTimes(3);
  expect(tiles.get(req.key)).toBe(bitmap);
});

it("forgets pending retries when cleared", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
  const redraw = vi.fn();
  const tiles = new TileCache(redraw);
  tiles.want([req]);
  await settle();
  tiles.clear();                                                                       // the editor went away
  await vi.advanceTimersByTimeAsync(60000);
  expect(redraw).not.toHaveBeenCalled();
});
