// @vitest-environment jsdom
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, type LayoutInfo } from "../api";
import { setLocale } from "../i18n";
import type { TileReq } from "./tiles";
import { LayoutEditor } from "./LayoutEditor";

// The review switches one layout view between the confirmed design and the proposal without remounting it. Their
// libraries count versions separately: the same version must not bring back the other side's tiles.
const wanted = vi.hoisted(() => [] as TileReq[][]);
vi.mock("../App", () => ({ hashPanel: () => "layers" }));
vi.mock("../layout", () => ({ Splitter: () => null }));
vi.mock("./tiles", () => ({ TileCache: class {
  clear() {}
  get() { return null; }
  getStale() { return null; }
  want(reqs: TileReq[]) { wanted.push(reqs); }
} }));
vi.mock("../api", async (original) => ({
  ...await original<typeof import("../api")>(),
  api: {
    layout: vi.fn(), layers: vi.fn(), integrity: vi.fn(),
    tileUrl: vi.fn((p: string, _l: string, _c: string, level: number, ix: number, iy: number, v: number) => `${p}/${level}/${ix}/${iy}?v=${v}`),
  },
}));

const layout: LayoutInfo = { cell: "c", dbu: 0.001, bbox: [-100, -100, 100, 100], version: 3, dirty: false, tile_px: 256, children: [], readonly: false };
let frames: FrameRequestCallback[] = [];
const flush = () => act(async () => { await Promise.resolve(); const f = frames; frames = []; f.forEach((cb) => cb(0)); });

beforeEach(async () => {
  wanted.length = 0;
  frames = [];
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  vi.stubGlobal("requestAnimationFrame", vi.fn((cb: FrameRequestCallback) => { frames.push(cb); return frames.length; }));
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.stubGlobal("localStorage", { getItem: () => null, setItem: () => {} });
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    x: 0, y: 0, left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600, toJSON: () => ({}),
  });
  const ctx = new Proxy({} as Record<string | symbol, unknown>, {      // a 2D context that draws nothing
    get: (t, k) => (k in t ? t[k] : () => ({ width: 0 })),
    set: (t, k, v) => { t[k] = v; return true; },
  });
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(ctx as unknown as CanvasRenderingContext2D);
  vi.mocked(api.layout).mockResolvedValue(layout);
  vi.mocked(api.layers).mockResolvedValue([]);
  vi.mocked(api.integrity).mockResolvedValue({ problems: [] } as Awaited<ReturnType<typeof api.integrity>>);
  await setLocale("en");
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function editor(project: string) {
  return <LayoutEditor project={project} lib="l" cell="c" readonly={false} active libCells={["c"]} libraries={["l"]}
    hasSchematic={false} probe={null} onProbe={() => {}} onOpenSchematic={() => {}} onOpenCell={() => {}} onDirty={() => {}} toast={() => {}} />;
}

it("asks for the other project's tiles when the same view switches project at the same version", async () => {
  const view = render(editor("p~cs"));
  await flush(); await flush();
  const proposal = wanted.at(-1)!;
  expect(proposal.length).toBeGreaterThan(0);
  expect(proposal.every((r) => r.url.startsWith("p~cs/") && r.url.endsWith("?v=3"))).toBe(true);

  view.rerender(editor("p"));                                          // "Confirmed" in the review
  await flush(); await flush();
  const confirmed = wanted.at(-1)!;
  expect(confirmed.every((r) => r.url.startsWith("p/") && r.url.endsWith("?v=3"))).toBe(true);
  const before = new Set(proposal.map((r) => r.key));
  expect(confirmed.some((r) => before.has(r.key))).toBe(false);       // no bitmap of the proposal is reused
});
