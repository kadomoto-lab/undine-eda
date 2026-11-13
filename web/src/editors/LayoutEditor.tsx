import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CircuitBoard, ClipboardPaste, Copy, Eye, EyeOff, Layers as LayersIcon, Maximize, Move, MousePointer2, PenLine, Pentagon,
  Redo2, Rocket, Ruler, Save, ShieldCheck, Square, SquareStack, Type, Undo2, Info, Cpu, Workflow, Target,
} from "lucide-react";
import { hashPanel } from "../App";
import { api, ChatImage, ChatItem, DrcItem, LayerInfo, LayoutInfo, ProbeResult, SdlStatus, SelItem } from "../api";
import { Camera, fmt, niceStep, restoreView, saveView, viewKey } from "./camera";
import type { LayoutDiff } from "./diff";
import { TileCache, TileReq } from "./tiles";
import { copyEditorClipboard, readEditorClipboard, type EditorClipboard } from "./editorClipboard";
import { PcellPanel, PcellSpec } from "../panels/PcellPanel";
import { VerifyPanel } from "../panels/VerifyPanel";
import { SdlPanel } from "../panels/SdlPanel";
import { TapeoutPanel } from "../panels/TapeoutPanel";
import { IntentPanel } from "../panels/IntentPanel";
import { T, rgba } from "../theme";
import { Splitter } from "../layout";
import { ask } from "../dialog";
import { useI18n, t, msg, resolveText, apiErrorText, type UiText } from "../i18n";

type Tool = "select" | "rect" | "polygon" | "path" | "label" | "ruler" | "move" | "copy" | "paste" | "place" | "pcell";
type Drag = { sx: number; sy: number; moved: boolean; wx: number; wy: number } &
  ({ kind: "pan" | "box" } | { kind: "move"; items: SelItem[] });

interface Props {
  project: string;
  lib: string;
  cell: string;
  readonly: boolean;
  active: boolean;
  libCells: string[];
  libraries: string[];
  hasSchematic: boolean;
  probe: ProbeResult | null;
  diffWith?: string | null; // a changeset preview: draw what differs between it and its real project
  fitOnLoad?: boolean;      // the assistant is building this cell and the view follows it: show all of it
  onProbe: (q: { inst?: string; net?: string } | null) => void;
  onSelection?: (items: ChatItem[]) => void;
  registerCapture?: (fn: (() => ChatImage | null) | null) => void;
  onOpenSchematic: () => void;
  onOpenCell: (cell: string) => void;
  onDirty: (dirty: boolean) => void;
  toast: (msg: UiText, kind?: "error" | "info") => void;
}

const TOOLS: { id: Tool; icon: typeof Square; label: UiText; key: string; edit?: boolean }[] = [
  { id: "select", icon: MousePointer2, label: msg("editor:tool.select"), key: "Esc" },
  { id: "move", icon: Move, label: msg("editor:tool.move"), key: "M", edit: true },
  { id: "rect", icon: Square, label: msg("editor:tool.rect"), key: "R", edit: true },
  { id: "polygon", icon: Pentagon, label: msg("editor:tool.polygon"), key: "Shift+P", edit: true },
  { id: "path", icon: PenLine, label: msg("editor:tool.path"), key: "P", edit: true },
  { id: "label", icon: Type, label: msg("editor:tool.label"), key: "L", edit: true },
  { id: "place", icon: SquareStack, label: msg("editor:tool.place"), key: "I", edit: true },
  { id: "ruler", icon: Ruler, label: msg("editor:tool.ruler"), key: "K" },
];

const HELP: Partial<Record<Tool, UiText>> = {
  select: msg("editor:layout.help.select"),
  rect: msg("editor:layout.help.rect"),
  polygon: msg("editor:layout.help.polygon"),
  path: msg("editor:layout.help.path"),
  label: msg("editor:layout.help.label"),
  move: msg("editor:layout.help.move"),
  copy: msg("editor:layout.help.copy"),
  paste: msg("editor:clipboard.help"),
  place: msg("editor:layout.help.place"),
  pcell: msg("editor:layout.help.pcell"),
  ruler: msg("editor:layout.help.ruler"),
};

function maskOf(layers: LayerInfo[], vis: boolean[]): string | null {
  if (vis.every((v) => v)) return null;
  const n = Math.ceil(layers.length / 4);
  let s = "";
  for (let k = 0; k < n; k++) {
    let nib = 0;
    for (let b = 0; b < 4; b++) if (vis[4 * k + b] ?? true) nib |= 1 << b;
    s += nib.toString(16);
  }
  return s;
}

export function LayoutEditor(props: Props) {
  const { locale } = useI18n();
  const { project, lib, cell, readonly, active } = props;
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const cam = useRef(new Camera(true)).current;
  const [info, setInfo] = useState<LayoutInfo | null>(null);
  const [layoutDiff, setLayoutDiff] = useState<LayoutDiff | null>(null);
  const [layers, setLayers] = useState<LayerInfo[]>([]);
  const [vis, setVis] = useState<boolean[]>([]);
  const [curLayer, setCurLayer] = useState<number>(-1);
  const [tool, setToolState] = useState<Tool>("select");
  const [sel, setSel] = useState<SelItem[]>([]);
  const [version, setVersion] = useState(0);
  const [depth, setDepth] = useState(99);
  const [grid, setGrid] = useState(5); // dbu
  const [gridText, setGridText] = useState<string | null>(null); // the grid field while it is being typed
  const [pathWidth, setPathWidth] = useState(140); // dbu
  const [placeCell, setPlaceCell] = useState("");
  const [placeBox, setPlaceBox] = useState<[number, number, number, number] | null>(null);
  const [cursor, setCursor] = useState<[number, number]>([0, 0]);
  const [panel, setPanel] = useState<"layers" | "props" | "pcell" | "verify" | "sdl" | "tapeout" | "intent">((hashPanel() as "layers") || "layers");
  const [markers, setMarkers] = useState<{ items: DrcItem[]; focus: number } | null>(null);
  const [sdlStatus, setSdlStatus] = useState<SdlStatus | null>(null);
  const [showRats, setShowRats] = useState(true);
  const [pcellSpec, setPcellSpec] = useState<PcellSpec | null>(null);
  const [integrity, setIntegrity] = useState<{ instance: string; problem: string; where: number[] }[]>([]);
  const [placeFromLib, setPlaceFromLib] = useState<string | null>(null);
  const [layerFilter, setLayerFilter] = useState("");
  const [usedOnly, setUsedOnly] = useState(true);
  const [busy, setBusy] = useState(false);
  const paste = useRef<EditorClipboard | null>(null);
  const pasteBusy = useRef(false);
  const moveBusy = useRef(false);
  const clipboardSession = useRef(0);
  const clipboardContext = useRef(0);

  // interaction state kept in refs (no re-render per mouse move)
  const st = useRef({
    pts: [] as number[], // world points of the shape being drawn
    mouse: [0, 0] as [number, number], // snapped world
    mouseRaw: [0, 0] as [number, number],
    screen: [0, 0] as [number, number],
    drag: null as Drag | null,
    moveGhost: null as { items: SelItem[]; dx: number; dy: number } | null,
    moveSession: 0,
    selectionVersion: 0,
    space: false,
    rulers: [] as number[][],
    raf: 0,
  }).current;

  const mask = useMemo(() => maskOf(layers, vis), [layers, vis]);
  const dbu = info?.dbu ?? 0.001;
  const tiles = useMemo(() => new TileCache(() => requestDraw()), []); // eslint-disable-line
  useEffect(() => () => tiles.clear(), [tiles]);

  // refs mirroring state for the draw loop
  const live = useRef({ project, version, mask, depth, sel, tool, grid, pathWidth, placeBox, layers, curLayer, info, markers, sdlStatus, showRats, probe: props.probe, panel, integrity, layoutDiff, busy });
  live.current = { project, version, mask, depth, sel, tool, grid, pathWidth, placeBox, layers, curLayer, info, markers, sdlStatus, showRats, probe: props.probe, panel, integrity, layoutDiff, busy };

  const setTool = useCallback((t: Tool) => {
    st.pts = [];
    st.drag = null;
    st.moveGhost = null;
    st.moveSession++;
    st.selectionVersion++;
    clipboardSession.current++;
    paste.current = null;
    if (readonly && (t === "paste" || t === "copy" || TOOLS.find((x) => x.id === t)?.edit)) {
      props.toast(msg("editor:layout.readOnlyLibrary"), "info");
      return;
    }
    setToolState(t);
  }, [readonly]); // eslint-disable-line

  useEffect(() => {
    clipboardContext.current++;
    clipboardSession.current++;
    paste.current = null;
    st.pts = [];
    st.drag = null;
    st.moveGhost = null;
    st.moveSession++;
    st.selectionVersion++;
    setToolState("select");
    setBusy(false);
    return () => {
      clipboardContext.current++;
      clipboardSession.current++;
      paste.current = null;
      st.drag = null;
      st.moveGhost = null;
      st.moveSession++;
      st.selectionVersion++;
    };
  }, [project, lib, cell, readonly, active]);

  useEffect(() => { setSel([]); }, [project, lib, cell]);

  // ----------------------------------------------------------------- loading

  // The first fit of a freshly opened cell waits until the canvas has its real size (a tab that is still hidden,
  // or a layout that has not settled yet, reports ~0 px and would fit into nothing).
  const pendingFit = useRef<[number, number, number, number] | null>(null);
  const placed = useRef(false); // the camera has been fitted or restored: worth remembering
  const emptyFit = useRef(false);
  const tryFit = () => {
    sizeCanvas();
    if (pendingFit.current && cam.w > 120 && cam.h > 120) {
      cam.fit(pendingFit.current);
      placed.current = !emptyFit.current;        // an empty cell's fit is not worth remembering: it fills up
      pendingFit.current = null;
      requestDraw();
    }
  };

  const reload = useCallback(async (fit: boolean) => {
    const [i, ls] = await Promise.all([api.layout(project, lib, cell), api.layers(project, lib, cell)]);
    setInfo(i);
    setVersion(i.version);
    setLayers(ls);
    setVis((v) => (v.length === ls.length ? v : ls.map((l) => l.visible)));
    // drawing layer: a used one, else a routing layer (an empty cell must not start on e.g. prBoundary)
    const routing = ls.find((l) => /^met1\b/i.test(l.name)) ?? ls.find((l) => /^li1\b/i.test(l.name));
    setCurLayer((c) => (c >= 0 ? c : (ls.find((l) => l.used)?.id ?? routing?.id ?? 0)));
    if (fit) {
      if (!props.fitOnLoad && restoreView(viewKey(project, lib, cell, "layout"), cam)) placed.current = true; // same cell, other version
      else {
        emptyFit.current = !(i.bbox[2] > i.bbox[0]);
        pendingFit.current = i.bbox[2] > i.bbox[0] ? i.bbox : [-5000, -5000, 5000, 5000];
        tryFit();
      }
    }
    requestDraw();
  }, [project, lib, cell]); // eslint-disable-line

  useEffect(() => {
    reload(true).catch((e) => props.toast(apiErrorText(e), "error"));
    const key = viewKey(project, lib, cell, "layout");
    return () => { if (placed.current) saveView(key, cam); };
  }, [reload]); // eslint-disable-line

  // differences between the proposal and the confirmed design, whichever of the two is shown
  const diffWith = props.diffWith;
  useEffect(() => {
    if (!diffWith) { setLayoutDiff(null); return; }
    let gone = false;
    api.layoutDiff(diffWith, lib, cell, diffWith.split("~")[0])
      .then((d) => { if (!gone) setLayoutDiff(d); })
      .catch(() => { if (!gone) setLayoutDiff(null); });
    return () => { gone = true; };
  }, [diffWith, lib, cell, version]);

  const afterEdit = useCallback((r: { version: number; dirty: boolean }) => {
    const context = clipboardContext.current;
    setVersion(r.version);
    props.onDirty(r.dirty);
    api.layout(project, lib, cell).then((next) => { if (context === clipboardContext.current) setInfo(next); }).catch(() => {});
  }, [project, lib, cell]); // eslint-disable-line

  const run = useCallback(async <T,>(p: Promise<T>): Promise<T | undefined> => {
    setBusy(true);
    try {
      return await p;
    } catch (e) {
      props.toast(apiErrorText(e), "error");
      return undefined;
    } finally {
      setBusy(false);
    }
  }, []); // eslint-disable-line

  // ----------------------------------------------------------------- drawing

  const sizeCanvas = () => {
    const c = canvasRef.current, w = wrapRef.current;
    if (!c || !w) return;
    const dpr = window.devicePixelRatio || 1;
    const r = w.getBoundingClientRect();
    cam.w = Math.max(1, r.width);
    cam.h = Math.max(1, r.height);
    c.width = Math.round(cam.w * dpr);
    c.height = Math.round(cam.h * dpr);
    c.style.width = `${cam.w}px`;
    c.style.height = `${cam.h}px`;
  };

  const requestDraw = () => {
    if (st.raf) return;
    st.raf = requestAnimationFrame(() => {
      st.raf = 0;
      draw();
    });
  };

  const draw = () => {
    const c = canvasRef.current;
    const L = live.current;
    if (!c || !L.info) return;
    const ctx = c.getContext("2d")!;
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = T.bg;
    ctx.fillRect(0, 0, c.width, c.height);

    // --- tiles (device pixel space)
    const TP = L.info.tile_px;
    const level = Math.max(-6, Math.floor(Math.log2(cam.s / dpr) + 1e-9));
    const W = TP * Math.pow(2, level);
    const [vx1, vy1, vx2, vy2] = cam.view();
    const ix1 = Math.floor(vx1 / W), ix2 = Math.floor(vx2 / W);
    const iy1 = Math.floor(vy1 / W), iy2 = Math.floor(vy2 / W);
    const reqs: TileReq[] = [];
    const [ccx, ccy] = [(vx1 + vx2) / 2 / W, (vy1 + vy2) / 2 / W];
    // the project too: the review switches the same view between the confirmed design and the proposal, whose
    // libraries count versions separately
    const keyOf = (lv: number, ix: number, iy: number) => `${L.project}|${L.version}|${L.mask}|${L.depth}|${lv}|${ix}|${iy}`;
    ctx.imageSmoothingEnabled = true;
    const devRect = (x0: number, y0: number, x1: number, y1: number) => {
      const [ax, ay] = cam.toScreen(x0, y1);
      const [bx, by] = cam.toScreen(x1, y0);
      const l = Math.round(ax * dpr), t = Math.round(ay * dpr);
      return [l, t, Math.round(bx * dpr) - l, Math.round(by * dpr) - t];
    };
    for (let iy = iy1; iy <= iy2; iy++) {
      for (let ix = ix1; ix <= ix2; ix++) {
        const key = keyOf(level, ix, iy);
        const [dl, dt, dw, dh] = devRect(ix * W, iy * W, (ix + 1) * W, (iy + 1) * W);
        let bmp = tiles.get(key);
        if (!bmp) {
          reqs.push({
            key, pos: `${level}|${ix}|${iy}`,
            url: api.tileUrl(L.project, lib, cell, level, ix, iy, L.version, L.mask, L.depth),
            prio: Math.hypot(ix + 0.5 - ccx, iy + 0.5 - ccy),
          });
          bmp = tiles.getStale(`${level}|${ix}|${iy}`);
        }
        if (bmp) {
          ctx.drawImage(bmp, dl, dt, dw, dh);
          continue;
        }
        // fallback: a coarser tile scaled up
        for (let k = 1; k <= 6; k++) {
          const px = Math.floor(ix / 2 ** k), py = Math.floor(iy / 2 ** k);
          const pb = tiles.get(keyOf(level + k, px, py)) ?? tiles.getStale(`${level + k}|${px}|${py}`);
          if (!pb) continue;
          const sub = TP / 2 ** k;
          const jx = ix - px * 2 ** k, jy = iy - py * 2 ** k;
          ctx.drawImage(pb, jx * sub, (2 ** k - 1 - jy) * sub, sub, sub, dl, dt, dw, dh);
          break;
        }
      }
    }
    tiles.want(reqs);

    // --- vector overlay (CSS pixel space)
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    drawGrid(ctx, L.grid);
    // origin axes
    const [ox, oy] = cam.toScreen(0, 0);
    ctx.strokeStyle = rgba("dim", 0.25);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(ox, 0); ctx.lineTo(ox, cam.h);
    ctx.moveTo(0, oy); ctx.lineTo(cam.w, oy);
    ctx.stroke();

    // selection
    const t = L.tool;
    const autoMove = st.drag?.kind === "move" && st.drag.moved
      ? { items: st.drag.items, ...dragOffset(st.drag, st.mouse) } : st.moveGhost;
    const moving = !!autoMove || (t === "move" || t === "copy") && st.pts.length === 2;
    const [mdx, mdy] = autoMove ? [autoMove.dx, autoMove.dy] : moving ? [st.mouse[0] - st.pts[0], st.mouse[1] - st.pts[1]] : [0, 0];
    for (const s of autoMove?.items ?? L.sel) {
      ctx.strokeStyle = T.sel;
      ctx.fillStyle = rgba("sel", 0.10);
      ctx.lineWidth = 1.5;
      for (const ring of s.outline) polyPath(ctx, ring, 0, 0, true);
      if (s.kind === "inst") ctx.setLineDash([5, 4]);
      ctx.fill("evenodd");
      ctx.stroke();
      ctx.setLineDash([]);
      if (moving) {
        ctx.strokeStyle = rgba("sel", 0.85);
        ctx.setLineDash([4, 3]);
        for (const ring of s.outline) {
          polyPath(ctx, ring, mdx, mdy, true);
          ctx.stroke();
        }
        ctx.setLineDash([]);
      }
    }
    if (t === "paste" && paste.current?.kind === "layout") {
      const [dx, dy] = pasteOffset(paste.current.bbox, st.mouse);
      ctx.strokeStyle = T.sel;
      ctx.fillStyle = rgba("sel", 0.18);
      ctx.lineWidth = 1.5;
      ctx.setLineDash([4, 3]);
      for (const item of paste.current.preview as SelItem[]) {
        for (const ring of item.outline) {
          polyPath(ctx, ring, dx, dy, true);
          ctx.fill("evenodd");
          ctx.stroke();
        }
      }
      ctx.setLineDash([]);
    }

    drawOverlays(ctx);

    // tool previews
    const layerColor = L.layers[L.curLayer]?.frame ?? T.accent;
    const m = st.mouse;
    ctx.strokeStyle = layerColor;
    ctx.fillStyle = hexA(layerColor, 0.22);
    ctx.lineWidth = 1.5;
    if (t === "rect" && st.pts.length === 2) {
      polyPath(ctx, [st.pts[0], st.pts[1], m[0], st.pts[1], m[0], m[1], st.pts[0], m[1]], 0, 0, true);
      ctx.fill(); ctx.stroke();
      dimLabel(ctx, st.pts[0], st.pts[1], m[0], m[1]);
    } else if (t === "polygon" && st.pts.length >= 2) {
      polyPath(ctx, [...st.pts, m[0], m[1]], 0, 0, true);
      ctx.fill(); ctx.stroke();
    } else if (t === "path" && st.pts.length >= 2) {
      const pts = [...st.pts, m[0], m[1]];
      ctx.lineWidth = Math.max(1, L.pathWidth / cam.s);
      ctx.strokeStyle = hexA(layerColor, 0.45);
      ctx.lineCap = "square";
      ctx.lineJoin = "miter";
      polyPath(ctx, pts, 0, 0, false);
      ctx.stroke();
      ctx.lineWidth = 1;
      ctx.strokeStyle = layerColor;
      polyPath(ctx, pts, 0, 0, false);
      ctx.stroke();
      ctx.lineCap = "butt";
    } else if (t === "place" && L.placeBox) {
      const b = L.placeBox;
      ctx.strokeStyle = T.accent;
      ctx.setLineDash([6, 4]);
      polyPath(ctx, [b[0] + m[0], b[1] + m[1], b[2] + m[0], b[1] + m[1], b[2] + m[0], b[3] + m[1], b[0] + m[0], b[3] + m[1]], 0, 0, true);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    if (st.drag?.kind === "box" && st.drag.moved) {
      const [ax, ay] = [st.drag.sx, st.drag.sy];
      const [bx, by] = st.screen;
      ctx.strokeStyle = T.accent;
      ctx.fillStyle = rgba("accent", 0.10);
      ctx.lineWidth = 1;
      ctx.fillRect(Math.min(ax, bx), Math.min(ay, by), Math.abs(bx - ax), Math.abs(by - ay));
      ctx.strokeRect(Math.min(ax, bx) + 0.5, Math.min(ay, by) + 0.5, Math.abs(bx - ax), Math.abs(by - ay));
    }

    // rulers
    const rulers = [...st.rulers];
    if (t === "ruler" && st.pts.length === 2) rulers.push([st.pts[0], st.pts[1], m[0], m[1]]);
    for (const r of rulers) drawRuler(ctx, r);

    // crosshair snap marker
    if (t !== "select") {
      const [sx, sy] = cam.toScreen(m[0], m[1]);
      ctx.strokeStyle = rgba("text", 0.8);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(sx - 7, sy); ctx.lineTo(sx + 7, sy);
      ctx.moveTo(sx, sy - 7); ctx.lineTo(sx, sy + 7);
      ctx.stroke();
    }
  };

  const drawOverlays = (ctx: CanvasRenderingContext2D) => {
    const L = live.current;
    // unrouted connections (schematic-driven layout)
    if (L.showRats && L.sdlStatus && L.panel === "sdl") {
      const hot = new Set(L.probe?.nets ?? []);
      ctx.lineWidth = 1.2;
      for (const r of L.sdlStatus.ratsnest) {
        const [ax, ay] = cam.toScreen(r.p[0], r.p[1]);
        const [bx, by] = cam.toScreen(r.p[2], r.p[3]);
        ctx.strokeStyle = hot.has(r.net) ? T.probe : rgba("warn", 0.75);
        ctx.lineWidth = hot.has(r.net) ? 2 : 1.2;
        ctx.setLineDash([5, 4]);
        ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.stroke();
        ctx.setLineDash([]);
        if (cam.s < 400) {
          ctx.fillStyle = hot.has(r.net) ? T.probe : T.warn;
          ctx.font = "10px 'JetBrains Mono', monospace";
          ctx.textBaseline = "bottom";
          ctx.fillText(r.net, (ax + bx) / 2 + 3, (ay + by) / 2 - 2);
        }
      }
    }
    // shapes that overlap a device's transistor area
    if (L.integrity.length) {
      ctx.strokeStyle = T.warn;
      ctx.fillStyle = rgba("warn", 0.28);
      ctx.lineWidth = 2;
      for (const p of L.integrity) {
        const b = p.where;
        polyPath(ctx, [b[0], b[1], b[2], b[1], b[2], b[3], b[0], b[3]], 0, 0, true);
        ctx.fill(); ctx.stroke();
        const [sx, sy] = cam.toScreen(b[2], b[3]);
        ctx.fillStyle = T.warn;
        ctx.font = "11px 'JetBrains Mono', monospace";
        ctx.textBaseline = "bottom";
        ctx.fillText(t("editor:layout.overlap", { instance: p.instance }), sx + 4, sy);
        ctx.fillStyle = rgba("warn", 0.28);
      }
    }
    // cross-probed instances
    if (L.probe) {
      ctx.strokeStyle = T.probe;
      ctx.fillStyle = rgba("probe", 0.14);
      ctx.lineWidth = 2;
      for (const it of L.probe.layout) {
        const b = it.bbox;
        polyPath(ctx, [b[0], b[1], b[2], b[1], b[2], b[3], b[0], b[3]], 0, 0, true);
        ctx.fill(); ctx.stroke();
      }
    }
    // differences from the other version: added green, removed red
    if (L.layoutDiff) {
      for (const [list, kind] of [[L.layoutDiff.added, "ok"], [L.layoutDiff.removed, "err"]] as const) {
        ctx.fillStyle = rgba(kind, 0.38);
        ctx.strokeStyle = T[kind];
        ctx.lineWidth = 1.2;
        for (const g of list) {
          ctx.beginPath();
          for (const rings of g.polys) {
            for (const ring of rings) {
              for (let i = 0; i < ring.length; i += 2) {
                const [sx, sy] = cam.toScreen(ring[i], ring[i + 1]);
                if (i === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy);
              }
              ctx.closePath();
            }
          }
          ctx.fill("evenodd");
          ctx.stroke();
        }
      }
    }
    // DRC markers (um -> dbu)
    if (L.markers) {
      const k = 1 / dbu;
      L.markers.items.forEach((it, i) => {
        const on = i === L.markers!.focus;
        ctx.strokeStyle = on ? "#ffffff" : T.mark;
        ctx.fillStyle = on ? "rgba(255,255,255,0.25)" : rgba("mark", 0.22);
        ctx.lineWidth = on ? 2.5 : 1.5;
        for (const g of it.geoms) {
          const pts = g.pts.map((v) => v * k);
          if (g.t === "edge" || g.t === "edges") {
            for (let j = 0; j + 3 < pts.length; j += 4) {
              polyPath(ctx, pts.slice(j, j + 4), 0, 0, false);
              ctx.stroke();
            }
            const bb = it.bbox.map((v) => v * k);
            polyPath(ctx, [bb[0], bb[1], bb[2], bb[1], bb[2], bb[3], bb[0], bb[3]], 0, 0, true);
            ctx.stroke();
          } else {
            polyPath(ctx, pts, 0, 0, true);
            ctx.fill(); ctx.stroke();
          }
        }
        // make tiny markers visible at any zoom
        const bb = it.bbox.map((v) => v * k);
        const [cx, cy] = cam.toScreen((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2);
        if ((bb[2] - bb[0]) / cam.s < 8 && (bb[3] - bb[1]) / cam.s < 8) {
          ctx.beginPath(); ctx.arc(cx, cy, 8, 0, 6.3); ctx.stroke();
        }
      });
    }
  };

  // device integrity: re-check whenever the layout changes (cheap: only devices bound to schematic instances)
  useEffect(() => {
    const t = setTimeout(() => {
      api.integrity(project, lib, cell).then((r) => setIntegrity(r.problems)).catch(() => setIntegrity([]));
    }, 300);
    return () => clearTimeout(t);
  }, [project, lib, cell, version]);

  const focusBox = (b: number[], inUm: boolean) => {
    const k = inUm ? 1 / dbu : 1;
    const bb: [number, number, number, number] = [b[0] * k, b[1] * k, b[2] * k, b[3] * k];
    const w = Math.max(bb[2] - bb[0], 400), h = Math.max(bb[3] - bb[1], 400);
    const cx = (bb[0] + bb[2]) / 2, cy = (bb[1] + bb[3]) / 2;
    cam.fit([cx - w, cy - h, cx + w, cy + h], 0.05);
    requestDraw();
  };

  const drawGrid = (ctx: CanvasRenderingContext2D, g: number) => {
    // dots at the snap grid when dense enough, otherwise at a coarser decade
    let step = g;
    while (step / cam.s < 14) step *= step.toString().startsWith("2") ? 2.5 : 2;
    const [x1, y1, x2, y2] = cam.view();
    const n = ((x2 - x1) / step) * ((y2 - y1) / step);
    if (n > 40000) return;
    ctx.fillStyle = rgba("dim", 0.22);
    for (let x = Math.ceil(x1 / step) * step; x <= x2; x += step) {
      for (let y = Math.ceil(y1 / step) * step; y <= y2; y += step) {
        const [sx, sy] = cam.toScreen(x, y);
        ctx.fillRect(Math.round(sx), Math.round(sy), 1, 1);
      }
    }
  };

  const polyPath = (ctx: CanvasRenderingContext2D, pts: number[], dx: number, dy: number, close: boolean) => {
    ctx.beginPath();
    for (let i = 0; i < pts.length; i += 2) {
      const [sx, sy] = cam.toScreen(pts[i] + dx, pts[i + 1] + dy);
      if (i === 0) ctx.moveTo(sx, sy);
      else ctx.lineTo(sx, sy);
    }
    if (close) ctx.closePath();
  };

  const um = (v: number) => fmt(v * dbu, 4);

  const dimLabel = (ctx: CanvasRenderingContext2D, x1: number, y1: number, x2: number, y2: number) => {
    const [sx, sy] = cam.toScreen(Math.max(x1, x2), Math.min(y1, y2));
    pill(ctx, `${um(Math.abs(x2 - x1))} × ${um(Math.abs(y2 - y1))} µm`, sx + 8, sy + 8);
  };

  const pill = (ctx: CanvasRenderingContext2D, text: string, x: number, y: number) => {
    ctx.font = "500 11px 'JetBrains Mono', monospace";
    const w = ctx.measureText(text).width + 10;
    ctx.fillStyle = rgba("bg1", 0.92);
    ctx.strokeStyle = rgba("accent", 0.6);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.roundRect(x, y, w, 18, 5);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = T.text;
    ctx.textBaseline = "middle";
    ctx.fillText(text, x + 5, y + 9.5);
  };

  const drawRuler = (ctx: CanvasRenderingContext2D, r: number[]) => {
    const [ax, ay] = cam.toScreen(r[0], r[1]);
    const [bx, by] = cam.toScreen(r[2], r[3]);
    ctx.strokeStyle = T.err;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(ax, ay); ctx.lineTo(bx, by);
    ctx.stroke();
    const len = Math.hypot(r[2] - r[0], r[3] - r[1]);
    // ticks
    const ang = Math.atan2(by - ay, bx - ax);
    const nx = -Math.sin(ang) * 5, ny = Math.cos(ang) * 5;
    ctx.beginPath();
    ctx.moveTo(ax - nx, ay - ny); ctx.lineTo(ax + nx, ay + ny);
    ctx.moveTo(bx - nx, by - ny); ctx.lineTo(bx + nx, by + ny);
    ctx.stroke();
    pill(ctx, `${um(len)} µm`, (ax + bx) / 2 + 6, (ay + by) / 2 + 6);
  };

  // redraw on state changes
  useEffect(requestDraw, [version, mask, depth, sel, tool, grid, pathWidth, placeBox, layers, curLayer, info, markers, sdlStatus, showRats, props.probe, panel, integrity, layoutDiff, locale]); // eslint-disable-line

  // cross-probe: bring the objects selected in the schematic into view
  useEffect(() => {
    const pr = props.probe;
    if (!pr || !pr.layout.length || sel.length) return;
    const bs = pr.layout.map((i) => i.bbox);
    const b = [Math.min(...bs.map((x) => x[0])), Math.min(...bs.map((x) => x[1])), Math.max(...bs.map((x) => x[2])), Math.max(...bs.map((x) => x[3]))];
    const [vx1, vy1, vx2, vy2] = cam.view();
    if (b[0] < vx1 || b[1] < vy1 || b[2] > vx2 || b[3] > vy2) focusBox(b, false);
  }, [props.probe]); // eslint-disable-line

  // selection in the layout -> schematic
  useEffect(() => {
    props.onSelection?.(sel.slice(0, 8).map((s): ChatItem => {
      if (s.inst_name) return { k: "i", id: s.inst_name, label: s.inst_name };
      const b = s.bbox.map((v) => Number((v * dbu).toFixed(3)));
      const what = `${s.type}${s.layer_name ? ` on ${s.layer_name}` : s.layer ? ` on ${s.layer}` : ""}`;
      return { k: "s", desc: `${what}, bbox [${b.join(", ")}] um${s.cell ? `, cell ${s.cell}` : ""}`, label: what };
    }));
  }, [sel]); // eslint-disable-line

  // a picture of the selection area (or of the view) for the assistant; the user sees it before it is sent
  const capture = (): ChatImage | null => {
    const c = canvasRef.current;
    if (!c) return null;
    const dpr = window.devicePixelRatio || 1;
    let [x1, y1, x2, y2] = [0, 0, cam.w, cam.h];
    let label = "visible area of the layout";
    const sels = live.current.sel;
    if (sels.length) {
      const pts = sels.flatMap((s) => s.outline.flatMap((ring) => ring.reduce<[number, number][]>((a, v, i) => (i % 2 ? a : [...a, cam.toScreen(v, ring[i + 1])]), [])));
      if (pts.length) {
        const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
        let [a, b, cc, d] = [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
        const padX = Math.max(60, (cc - a) * 0.4), padY = Math.max(60, (d - b) * 0.4);
        [a, b, cc, d] = [a - padX, b - padY, cc + padX, d + padY];
        x1 = Math.max(0, a); y1 = Math.max(0, b); x2 = Math.min(cam.w, cc); y2 = Math.min(cam.h, d);
        if (x2 - x1 > 20 && y2 - y1 > 20) label = `selection area (${sels.slice(0, 3).map((s) => s.inst_name ?? s.type).join(", ")})`;
        else [x1, y1, x2, y2] = [0, 0, cam.w, cam.h];
      }
    }
    const sw = (x2 - x1) * dpr, sh = (y2 - y1) * dpr;
    const k = Math.min(1, 1024 / Math.max(sw, sh));
    const out = document.createElement("canvas");
    out.width = Math.max(1, Math.round(sw * k));
    out.height = Math.max(1, Math.round(sh * k));
    out.getContext("2d")!.drawImage(c, x1 * dpr, y1 * dpr, sw, sh, 0, 0, out.width, out.height);
    const url = out.toDataURL("image/png");
    return { media_type: "image/png", data: url.slice(url.indexOf(",") + 1), label, width: out.width, height: out.height };
  };
  const captureRef = useRef(capture);
  captureRef.current = capture;
  useEffect(() => {
    if (!active) return;
    props.registerCapture?.(() => captureRef.current());
    return () => props.registerCapture?.(null);
  }, [active]); // eslint-disable-line

  const prevSel = useRef(0);
  useEffect(() => {
    const named = sel.length === 1 ? sel[0].inst_name : undefined;
    if (named) props.onProbe({ inst: named });
    else if (!sel.length && prevSel.current > 0) props.onProbe(null);
    prevSel.current = sel.length;
  }, [sel]); // eslint-disable-line

  useEffect(() => {
    const ro = new ResizeObserver(() => {
      tryFit();
      requestDraw();
    });
    if (wrapRef.current) ro.observe(wrapRef.current);
    return () => ro.disconnect();
  }, []); // eslint-disable-line

  // ----------------------------------------------------------------- input

  const snap = (x: number, y: number, ortho: boolean): [number, number] => {
    const g = live.current.grid;
    let sx = Math.round(x / g) * g, sy = Math.round(y / g) * g;
    if (ortho && st.pts.length >= 2) {
      const px = st.pts[st.pts.length - 2], py = st.pts[st.pts.length - 1];
      if (Math.abs(sx - px) >= Math.abs(sy - py)) sy = py;
      else sx = px;
    }
    return [sx, sy];
  };

  const pasteOffset = (bbox: [number, number, number, number], position: [number, number]): [number, number] =>
    snap(position[0] - (bbox[0] + bbox[2]) / 2, position[1] - (bbox[1] + bbox[3]) / 2, false);

  const dragOffset = (drag: Drag, point: [number, number]) => {
    const from = snap(drag.wx, drag.wy, false), to = snap(point[0], point[1], false);
    return { dx: to[0] - from[0], dy: to[1] - from[1] };
  };

  const cancelDrag = () => {
    const automatic = st.drag?.kind === "move" && st.drag.moved || !!st.moveGhost;
    st.drag = null;
    st.moveGhost = null;
    st.moveSession++;
    if (automatic) setToolState("select");
    requestDraw();
  };

  const commitMove = async (items: SelItem[], dx: number, dy: number, duplicate: boolean, automatic = false) => {
    if (automatic) setToolState("select");
    if (readonly || moveBusy.current || pasteBusy.current || (!duplicate && !dx && !dy)) { requestDraw(); return; }
    const context = clipboardContext.current, session = st.moveSession;
    if (automatic) st.moveGhost = { items, dx, dy };
    moveBusy.current = true;
    setBusy(true);
    try {
      const result = await api.move(project, lib, cell, items.map(item => item.ref), dx, dy, duplicate);
      if (context !== clipboardContext.current) return;
      afterEdit(result);
      if (session === st.moveSession) {
        setSel(result.result);
        if (!duplicate) setToolState("select");
      } else setSel([]); // Moving invalidates old shape references even when its UI gesture was cancelled.
    } catch (error) {
      if (context === clipboardContext.current && session === st.moveSession) props.toast(apiErrorText(error), "error");
    } finally {
      moveBusy.current = false;
      if (context === clipboardContext.current) {
        if (session === st.moveSession) st.moveGhost = null;
        setBusy(false);
        requestDraw();
      }
    }
  };

  const commitDragMove = (drag: Drag & { kind: "move" }, point: [number, number]) => {
    const { dx, dy } = dragOffset(drag, point);
    return commitMove(drag.items, dx, dy, false, true);
  };

  const doCopy = async () => {
    if (moveBusy.current) return;
    const selection = live.current.sel;
    if (!selection.length) return;
    const session = clipboardSession.current;
    try {
      await copyEditorClipboard(api.layoutCopy(project, lib, cell, selection.map((s) => s.ref)).then((snapshot) => ({
        version: 1, kind: "layout", source: { project, lib }, ...snapshot,
      })));
      if (session === clipboardSession.current) props.toast(msg("editor:clipboard.copied", { count: selection.length }), "info");
    } catch (e) {
      if (session === clipboardSession.current) props.toast(apiErrorText(e), "error");
    }
  };

  const startPaste = async () => {
    if (readonly || pasteBusy.current || moveBusy.current) return;
    cancelDrag();
    const session = ++clipboardSession.current;
    paste.current = null;
    try {
      const snapshot = await readEditorClipboard();
      if (session !== clipboardSession.current) return;
      if (!snapshot) { props.toast(msg("editor:clipboard.empty"), "info"); return; }
      if (snapshot.kind !== "layout") { props.toast(msg("editor:clipboard.wrongView", { view: msg("common:view.layout") }), "info"); return; }
      if (snapshot.source.project.split("~")[0] !== project.split("~")[0] || snapshot.source.lib !== lib) {
        props.toast(msg("editor:clipboard.wrongLibrary"), "info");
        return;
      }
      st.pts = [];
      st.drag = null;
      paste.current = snapshot;
      setToolState("paste");
      wrapRef.current?.focus();
      requestDraw();
    } catch (e) {
      if (session === clipboardSession.current) props.toast(apiErrorText(e), "error");
    }
  };

  const commitPaste = async (position: [number, number]) => {
    const snapshot = paste.current;
    if (!snapshot || readonly || pasteBusy.current || moveBusy.current) return;
    const session = clipboardSession.current;
    const context = clipboardContext.current;
    const [dx, dy] = pasteOffset(snapshot.bbox, position);
    pasteBusy.current = true;
    setBusy(true);
    try {
      const result = await api.layoutPaste(project, lib, cell, { source: snapshot.source, payload: snapshot.payload, dx, dy });
      if (context !== clipboardContext.current) return;
      afterEdit(result);
      if (session !== clipboardSession.current) return;
      setSel(result.result);
      setPanel("props");
      setTool("select");
    } catch (e) {
      if (context === clipboardContext.current && session === clipboardSession.current) props.toast(apiErrorText(e), "error");
    } finally {
      pasteBusy.current = false;
      if (context === clipboardContext.current) { setBusy(false); requestDraw(); }
    }
  };

  const evPos = (e: { clientX: number; clientY: number }): [number, number] => {
    const r = canvasRef.current!.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  };

  const doSelect = async (box: number[], point: boolean, add: boolean) => {
    const context = clipboardContext.current, selectionVersion = ++st.selectionVersion;
    const res = await run(api.select(project, lib, cell, box, point, live.current.mask, 5 * cam.s));
    if (!res || context !== clipboardContext.current || selectionVersion !== st.selectionVersion) return;
    setSel((cur) => {
      if (!add) return res;
      const keys = new Set(cur.map((s) => JSON.stringify(s.ref)));
      const out = [...cur];
      for (const r of res) {
        const k = JSON.stringify(r.ref);
        if (keys.has(k)) {
          const i = out.findIndex((s) => JSON.stringify(s.ref) === k);
          out.splice(i, 1);
        } else out.push(r);
      }
      return out;
    });
    if (res.length) setPanel("props");
  };

  const finishShape = async () => {
    if (moveBusy.current) return;
    const t = live.current.tool;
    if (t === "polygon" && st.pts.length >= 6) {
      const r = await run(api.create(project, lib, cell, "polygon", live.current.curLayer, st.pts));
      if (r) afterEdit(r);
    } else if (t === "path" && st.pts.length >= 4) {
      const r = await run(api.create(project, lib, cell, "path", live.current.curLayer, st.pts, live.current.pathWidth));
      if (r) afterEdit(r);
    }
    st.pts = [];
    requestDraw();
  };

  const onClickTool = async (e: React.MouseEvent, sx: number, sy: number) => {
    if (moveBusy.current) return;
    const t = live.current.tool;
    const [wx, wy] = snap(...cam.toWorld(sx, sy), t === "path" && !e.altKey);
    if (t === "paste") {
      await commitPaste([wx, wy]);
    } else if (t === "rect") {
      if (st.pts.length === 0) st.pts = [wx, wy];
      else {
        const pts = [st.pts[0], st.pts[1], wx, wy];
        st.pts = [];
        const r = await run(api.create(project, lib, cell, "box", live.current.curLayer, pts));
        if (r) afterEdit(r);
      }
    } else if (t === "polygon" || t === "path") {
      const n = st.pts.length;
      if (n >= 2 && st.pts[n - 2] === wx && st.pts[n - 1] === wy) return;
      st.pts.push(wx, wy);
    } else if (t === "label") {
      const layer = live.current.layers.find((l) => l.id === live.current.curLayer);
      const v = await ask({
        title: msg("editor:layout.newLabel"), submitLabel: msg("editor:action.place"),
        message: msg("editor:layout.labelPosition", { layer: layer?.name ?? msg("editor:layout.currentLayer"), x: fmt(wx * dbu, 4), y: fmt(wy * dbu, 4) }),
        fields: [{ name: "text", label: msg("editor:property.text"), required: true, placeholder: msg("editor:layout.labelExample"), mono: true }],
      });
      wrapRef.current?.focus();
      if (v) {
        const r = await run(api.create(project, lib, cell, "text", live.current.curLayer, [wx, wy], 0, v.text));
        if (r) afterEdit(r);
      }
    } else if (t === "ruler") {
      if (st.pts.length === 0) st.pts = [wx, wy];
      else {
        st.rulers.push([st.pts[0], st.pts[1], wx, wy]);
        st.pts = [];
      }
    } else if (t === "move" || t === "copy") {
      if (!live.current.sel.length) {
        await doSelect([...cam.toWorld(sx, sy)], true, false);
        return;
      }
      if (st.pts.length === 0) st.pts = [wx, wy];
      else {
        const [dx, dy] = [wx - st.pts[0], wy - st.pts[1]];
        st.pts = [];
        await commitMove(live.current.sel.slice(), dx, dy, t === "copy");
      }
    } else if (t === "place") {
      if (!placeCell) return;
      const r = await run(placeFromLib && placeFromLib !== lib
        ? api.placeFrom(project, lib, cell, placeFromLib, placeCell, wx, wy)
        : api.place(project, lib, cell, placeCell, wx, wy));
      if (r) afterEdit(r);
    } else if (t === "pcell") {
      if (!pcellSpec) return;
      const r = await run(api.placePcell(project, lib, cell, pcellSpec.pcell, pcellSpec.params, wx, wy));
      if (r) {
        afterEdit(r);
        reload(false);
        props.toast(msg("editor:layout.placed", { cell: r.cell }));
      }
    }
    requestDraw();
  };

  // the status bar and the HUD sit inside the canvas wrapper: clicks there belong to their controls
  // (the grid field), not to the drawing tools
  const onControl = (e: React.MouseEvent) => (e.target as HTMLElement).closest(".statusbar, .hud-top") !== null;

  const onMouseDown = (e: React.MouseEvent) => {
    if (onControl(e)) return;
    wrapRef.current?.focus();
    const [sx, sy] = evPos(e);
    st.screen = [sx, sy];
    const t = live.current.tool;
    if (e.button === 1 || e.button === 2 || (e.button === 0 && st.space)) {
      if (st.drag?.kind === "move") cancelDrag();
      st.drag = { kind: "pan", sx, sy, moved: false, wx: 0, wy: 0 };
      e.preventDefault();
      return;
    }
    if (e.button !== 0) return;
    if (moveBusy.current || pasteBusy.current || live.current.busy) return;
    if (t === "select") {
      const [wx, wy] = cam.toWorld(sx, sy);
      st.drag = !readonly && live.current.sel.length && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey
        ? { kind: "move", sx, sy, moved: false, wx, wy, items: live.current.sel.slice() }
        : { kind: "box", sx, sy, moved: false, wx, wy };
    } else {
      onClickTool(e, sx, sy);
    }
  };

  // a new grid applies at once: the cursor snaps to it without waiting for the mouse to move
  useEffect(() => {
    st.mouse = snap(st.mouseRaw[0], st.mouseRaw[1], false);
    setCursor(st.mouse);
    requestDraw();
  }, [grid]); // eslint-disable-line

  const onMouseMove = (e: React.MouseEvent) => {
    if (onControl(e) && !st.drag) return;
    const [sx, sy] = evPos(e);
    const t = live.current.tool;
    if (st.drag?.kind === "pan") {
      cam.pan(sx - st.screen[0], sy - st.screen[1]);
      st.drag.moved = true;
    } else if (st.drag && !st.drag.moved && Math.hypot(sx - st.drag.sx, sy - st.drag.sy) > 4) {
      st.drag.moved = true;
      if (st.drag.kind === "move") {
        st.pts = [];
        st.selectionVersion++;
        setToolState("move");
      }
    }
    st.screen = [sx, sy];
    const raw = cam.toWorld(sx, sy);
    st.mouseRaw = raw;
    st.mouse = snap(raw[0], raw[1], t === "path" && !e.altKey);
    setCursor(st.mouse);
    requestDraw();
  };

  const onMouseUp = (e: React.MouseEvent) => {
    const d = st.drag;
    if (d && d.kind !== "pan" && (e.button !== 0 || onControl(e))) { cancelDrag(); return; }
    st.drag = null;
    if (d?.kind === "box" || d?.kind === "move") {
      const [sx, sy] = evPos(e);
      const [wx, wy] = cam.toWorld(sx, sy);
      if (d.kind === "move" && d.moved) void commitDragMove(d, [wx, wy]);
      else if (d.moved) doSelect([d.wx, d.wy, wx, wy], false, e.shiftKey);
      else doSelect([wx, wy, wx, wy], true, e.shiftKey);
    }
    requestDraw();
  };

  const onDoubleClick = async (e: React.MouseEvent) => {
    if (onControl(e)) return;
    const t = live.current.tool;
    if (t === "polygon" || t === "path") {
      await finishShape();
      return;
    }
    if (t === "select") {
      const [sx, sy] = evPos(e);
      const [wx, wy] = cam.toWorld(sx, sy);
      const res = await run(api.select(project, lib, cell, [wx, wy, wx, wy], true, live.current.mask, 5 * cam.s));
      const inst = res?.find((r) => r.kind === "inst");
      if (inst) props.onOpenCell(String(inst.ref.c));
    }
  };

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      const f = Math.pow(1.0018, e.deltaMode === 1 ? e.deltaY * 30 : e.deltaY);
      cam.zoomAt(e.clientX - r.left, e.clientY - r.top, f, 1 / 64, 1e7);
      requestDraw();
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []); // eslint-disable-line

  useEffect(() => {
    const releaseOutside = (event: MouseEvent) => {
      if (!wrapRef.current?.contains(event.target as Node)) cancelDrag();
    };
    const blur = () => { st.space = false; cancelDrag(); };
    const releaseSpace = (event: KeyboardEvent) => { if (event.key === " ") st.space = false; };
    window.addEventListener("mouseup", releaseOutside);
    window.addEventListener("blur", blur);
    window.addEventListener("keyup", releaseSpace);
    return () => {
      window.removeEventListener("mouseup", releaseOutside);
      window.removeEventListener("blur", blur);
      window.removeEventListener("keyup", releaseSpace);
      if (st.raf) cancelAnimationFrame(st.raf);
      st.raf = 0;
    };
  }, []); // eslint-disable-line

  const fit = () => {
    const i = live.current.info;
    if (i) cam.fit(i.bbox[2] > i.bbox[0] ? i.bbox : [-5000, -5000, 5000, 5000]);
    requestDraw();
  };

  const doUndo = async (redo: boolean) => {
    if (moveBusy.current || readonly) return;
    cancelDrag();
    const r = await run(redo ? api.redo(project, lib, cell) : api.undo(project, lib, cell));
    if (r) {
      if (r.result === null) props.toast(redo ? msg("editor:action.nothingToRedo") : msg("editor:action.nothingToUndo"), "info");
      setSel([]);
      afterEdit(r);
    }
  };

  const doSave = async () => {
    if (moveBusy.current) return;
    const r = await run(api.save(project, lib));
    if (r) {
      props.onDirty(false);
      // a layout belongs to its cell: name the cells written (usually just this one), not the library
      const cells = [...(r.result?.written ?? []), ...(r.result?.removed ?? [])];
      props.toast(cells.length ? msg("editor:layout.saved", { cells: cells.join(", ") }) : msg("editor:layout.nothingToSave"), "info");
    }
  };

  const doDelete = async () => {
    if (!live.current.sel.length || readonly || moveBusy.current) return;
    cancelDrag();
    const r = await run(api.remove(project, lib, cell, live.current.sel.map((s) => s.ref)));
    if (r) {
      setSel([]);
      afterEdit(r);
    }
  };

  const startPlace = async () => {
    if (moveBusy.current) return;
    let found: { fromLib: string; name: string; bbox: number[] } | null = null;
    await ask({
      title: msg("editor:layout.placeInstance"), submitLabel: msg("editor:layout.choosePosition"),
      message: msg("editor:layout.placeInstanceHelp"),
      fields: [{ name: "cell", label: msg("editor:property.cell"), mono: true, required: true,
        value: placeCell || props.libCells.find((c) => c !== cell) || "",
        hint: msg("editor:layout.instanceHint", { lib, libraries: props.libraries.filter((l) => l !== lib).join(", ") || msg("editor:layout.noOtherLibraries") }),
        suggestions: [...props.libCells.filter((c) => c !== cell), ...props.libraries.filter((l) => l !== lib).map((l) => `${l}/`)] }],
      submit: async (v) => {
        const input = v.cell.trim();
        const slash = input.indexOf("/");
        const fromLib = slash > 0 ? input.slice(0, slash) : lib;
        const name = slash > 0 ? input.slice(slash + 1) : input;
        if (!name) return msg("editor:layout.missingCellName");
        if (fromLib === lib && name === cell) return msg("editor:layout.selfReference");
        const ci = await api.layout(project, fromLib, name); // throws "no cell ..." into the dialog
        found = { fromLib, name, bbox: ci.bbox };
      },
    });
    wrapRef.current?.focus();
    if (!found) return;
    const f = found as { fromLib: string; name: string; bbox: number[] };
    setPlaceFromLib(f.fromLib);
    setPlaceCell(f.name);
    setPlaceBox(f.bbox as [number, number, number, number]);
    setTool("place");
  };

  const armPcell = (spec: PcellSpec) => {
    if (moveBusy.current) return;
    setPcellSpec(spec);
    setPlaceBox(null);
    setTool("pcell");
    wrapRef.current?.focus();
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if ((e.target as HTMLElement).closest("input, textarea, select, [contenteditable]:not([contenteditable='false'])")) return;
    const k = e.key;
    const ctrl = e.ctrlKey || e.metaKey;
    if (moveBusy.current && k !== "Escape") { e.preventDefault(); return; }
    if (ctrl && k.toLowerCase() === "c") { e.preventDefault(); doCopy(); return; }
    if (ctrl && k.toLowerCase() === "v") { e.preventDefault(); startPaste(); return; }
    if (ctrl && k.toLowerCase() === "s") { e.preventDefault(); doSave(); return; }
    if (ctrl && k.toLowerCase() === "z") { e.preventDefault(); doUndo(e.shiftKey); return; }
    if (ctrl && k.toLowerCase() === "f") { e.preventDefault(); setDepth(0); return; }
    if (ctrl) return;
    const handled = true;
    switch (k) {
      case " ": st.space = true; break;
      case "Escape": {
        const movingSelection = st.drag?.kind === "move" || !!st.moveGhost;
        st.pts = [];
        setTool("select");
        if (live.current.tool === "select" && !movingSelection) setSel([]);
        break;
      }
      case "f": fit(); break;
      case "F": setDepth(99); break;
      case "r": setTool("rect"); break;
      case "p": setTool("path"); break;
      case "P": setTool("polygon"); break;
      case "l": setTool("label"); break;
      case "m": setTool("move"); break;
      case "c": setTool("copy"); break;
      case "k": setTool("ruler"); break;
      case "K": st.rulers = []; requestDraw(); break;
      case "i": if (!readonly) startPlace(); break;
      case "u": doUndo(false); break;
      case "U": doUndo(true); break;
      case "Delete": doDelete(); break;
      case "Backspace":
        if (st.pts.length) { st.pts.splice(-2, 2); requestDraw(); } else doDelete();
        break;
      case "Enter": finishShape(); break;
      case "ArrowLeft": cam.pan(cam.w / 4, 0); requestDraw(); break;
      case "ArrowRight": cam.pan(-cam.w / 4, 0); requestDraw(); break;
      case "ArrowUp": cam.pan(0, cam.h / 4); requestDraw(); break;
      case "ArrowDown": cam.pan(0, -cam.h / 4); requestDraw(); break;
      case "z": cam.zoomAt(cam.w / 2, cam.h / 2, 0.5); requestDraw(); break;
      case "Z": cam.zoomAt(cam.w / 2, cam.h / 2, 2); requestDraw(); break;
      default: return;
    }
    if (handled) e.preventDefault();
  };

  const onKeyUp = (e: React.KeyboardEvent) => {
    if (e.key === " ") st.space = false;
  };

  useEffect(() => {
    if (active) {
      wrapRef.current?.focus();
      tryFit();
      requestDraw();
    }
  }, [active]); // eslint-disable-line

  // ----------------------------------------------------------------- panels

  const anyUsed = layers.some((l) => l.used);   // an empty cell lists every layer, "used" or not
  const shownLayers = layers.filter((l) =>
    (!usedOnly || !anyUsed || l.used || l.id === curLayer) &&
    (!layerFilter || `${l.name} ${l.source}`.toLowerCase().includes(layerFilter.toLowerCase())));

  const toggleVis = (id: number, solo: boolean) => {
    setVis((v) => {
      if (solo) return v.map((_, i) => i === id);
      const n = [...v];
      n[id] = !n[id];
      return n;
    });
  };

  const setAllVis = (on: boolean) => setVis((v) => v.map((_, i) => (shownLayers.some((l) => l.id === i) ? on : v[i])));

  const scaleBar = useMemo(() => {
    const step = niceStep(cam.s * 90);
    return { px: step / cam.s, label: `${fmt(step * dbu, 4)} µm` };
  }, [cursor, info]); // eslint-disable-line

  return (
    <div className="editor">
      <div className="toolrail">
        {TOOLS.map((t) => (
          <button key={t.id} className={`railbtn ${tool === t.id ? "on" : ""}`}
            disabled={readonly && t.edit}
            title={`${resolveText(t.label)} (${t.key})`}
            onClick={() => (t.id === "place" ? startPlace() : setTool(t.id))}>
            <t.icon size={17} strokeWidth={1.75} />
          </button>
        ))}
        <div className="railsep" />
        <button className="railbtn" title={t("editor:clipboard.copyShortcut")} disabled={!sel.length}
          onClick={() => { doCopy(); wrapRef.current?.focus(); }}><Copy size={17} strokeWidth={1.75} /></button>
        <button className={`railbtn ${tool === "paste" ? "on" : ""}`} title={t("editor:clipboard.pasteShortcut")} disabled={readonly}
          onClick={startPaste}><ClipboardPaste size={17} strokeWidth={1.75} /></button>
        <div className="railsep" />
        <button className="railbtn" title={t("editor:action.fitShortcut")} onClick={fit}><Maximize size={17} strokeWidth={1.75} /></button>
        <button className="railbtn" title={t("editor:action.redoShortcut")} disabled={readonly} onClick={() => doUndo(true)}><Redo2 size={17} strokeWidth={1.75} /></button>
        <button className="railbtn" title={t("editor:action.undoShortcut")} disabled={readonly} onClick={() => doUndo(false)}><Undo2 size={17} strokeWidth={1.75} /></button>
        <button className="railbtn" title={t("editor:action.saveShortcut")} disabled={readonly} onClick={doSave}><Save size={17} strokeWidth={1.75} /></button>
        {props.hasSchematic && (
          <>
            <div className="railsep" />
            <button className="railbtn" title={t("editor:action.openSchematic")} onClick={props.onOpenSchematic}><CircuitBoard size={17} strokeWidth={1.75} /></button>
          </>
        )}
      </div>

      <div className="canvaswrap" ref={wrapRef} tabIndex={0}
        onMouseDown={onMouseDown} onMouseMove={onMouseMove} onMouseUp={onMouseUp}
        onMouseLeave={cancelDrag}
        onDoubleClick={onDoubleClick} onContextMenu={(e) => e.preventDefault()}
        onKeyDown={onKeyDown} onKeyUp={onKeyUp}
        style={{ cursor: tool === "select" ? "default" : "none" /* the snapped crosshair is drawn */ }}>
        <canvas ref={canvasRef} />
        <div className="hud-top">
          <span className="hud-chip">{tool === "paste" ? t("editor:clipboard.tool") : tool === "copy" ? t("editor:tool.copy") : resolveText(TOOLS.find((t) => t.id === tool)?.label)}</span>
          <span className="hud-hint">{st.drag?.kind === "move" && st.drag.moved ? t("editor:layout.help.dragMove") : resolveText(HELP[tool])}</span>
          {busy && <span className="spinner" />}
        </div>
        <div className="scalebar">
          <div className="scalebar-line" style={{ width: scaleBar.px }} />
          <span>{scaleBar.label}</span>
        </div>
        <div className="statusbar">
          <span className="mono coord">X {(cursor[0] * dbu).toFixed(3)}</span>
          <span className="mono coord">Y {(cursor[1] * dbu).toFixed(3)}</span>
          <span className="sep" />
          <label>{t("editor:layout.grid")} <input className="num" type="number" step="any" min={dbu}
            value={gridText ?? fmt(grid * dbu, 4)}
            onChange={(e) => {
              setGridText(e.target.value); // "0.0" on the way to "0.05" stays in the field
              const v = parseFloat(e.target.value);
              if (v > 0) setGrid(Math.max(1, Math.round(v / dbu)));
            }}
            onBlur={() => setGridText(null)}
            onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} /> µm</label>
          {tool === "path" && (
            <label>{t("editor:property.width")} <input className="num" type="number" step="any" value={fmt(pathWidth * dbu, 4)}
              onChange={(e) => { const v = parseFloat(e.target.value); if (v > 0) setPathWidth(Math.round(v / dbu)); }} /> µm</label>
          )}
          <label>{t("editor:layout.levels")} <select value={depth} onChange={(e) => setDepth(Number(e.target.value))}>
            {[0, 1, 2, 3, 5, 99].map((d) => <option key={d} value={d}>{d === 99 ? t("editor:layout.allLevels") : d}</option>)}
          </select></label>
          <span className="sep" />
          <span className="dim">{sel.length ? t("editor:selection.count", { count: sel.length }) : ""}</span>
          <span className="grow" />
          <span className="dim">{[info?.dirty && t("editor:status.unsaved"), readonly && t("editor:status.readOnly")].filter(Boolean).join(" · ")}</span>
        </div>
      </div>

      <Splitter pane="right" />
      <aside className="sidepanel">
        <div className="seg icons">
          <button className={panel === "layers" ? "on" : ""} title={t("editor:panel.layers")} onClick={() => setPanel("layers")}><LayersIcon size={15} /></button>
          <button className={panel === "props" ? "on" : ""} title={t("editor:panel.properties")} onClick={() => setPanel("props")}><Info size={15} /></button>
          <button className={panel === "pcell" ? "on" : ""} title={t("editor:panel.pcells")} onClick={() => setPanel("pcell")}><Cpu size={15} /></button>
          <button className={panel === "sdl" ? "on" : ""} title={t("editor:panel.sdl")} onClick={() => setPanel("sdl")}><Workflow size={15} />{integrity.length > 0 && <i className="tabdot" />}</button>
          <button className={panel === "tapeout" ? "on" : ""} title={t("editor:panel.tapeout")} onClick={() => setPanel("tapeout")}><Rocket size={15} /></button>
          <button className={panel === "verify" ? "on" : ""} title={t("editor:panel.verify")} onClick={() => setPanel("verify")}><ShieldCheck size={15} /></button>
          <button className={panel === "intent" ? "on" : ""} title={t("intent:tab")} onClick={() => setPanel("intent")}><Target size={15} /></button>
        </div>
        {panel === "pcell" && (
          <PcellPanel project={project} active={tool === "pcell" ? pcellSpec : null} onArm={armPcell} onCancel={() => setTool("select")} />
        )}
        {panel === "tapeout" && <TapeoutPanel project={project} lib={lib} cell={cell} version={version} toast={props.toast} />}
        {panel === "intent" && <IntentPanel project={project} lib={lib} cell={cell} tick={version} toast={props.toast} view="layout"
          selection={sel.filter((s) => s.kind === "inst" && s.inst_name).map((s) => s.inst_name!)} />}
        {panel === "verify" && (
          <VerifyPanel project={project} lib={lib} cell={cell} hasSchematic={props.hasSchematic} dbu={dbu}
            onMarkers={(items, focus) => setMarkers(items ? { items, focus: focus ?? -1 } : null)}
            onFocus={(b) => focusBox(b, true)} toast={props.toast} />
        )}
        {panel === "sdl" && (
          <SdlPanel project={project} lib={lib} cell={cell} version={version} hasSchematic={props.hasSchematic}
            showRats={showRats} setShowRats={setShowRats}
            onStatus={setSdlStatus} onProbe={props.onProbe} onFocus={(b) => focusBox(b, false)}
            onEdited={() => { reload(false); setVersion((v) => v + 1); }} toast={props.toast}
            integrity={integrity} onFocusDbu={(b) => focusBox(b, false)} />
        )}
        {(panel === "layers" || panel === "props") && (panel === "layers" ? (
          <>
            <div className="layertools">
              <input className="search" placeholder={t("editor:layout.filterLayers")} value={layerFilter} onChange={(e) => setLayerFilter(e.target.value)} />
              <label className="check"><input type="checkbox" checked={usedOnly} onChange={(e) => setUsedOnly(e.target.checked)} /> {t("editor:layout.usedLayers")}</label>
              <button className="icon" title={t("editor:layout.showAllLayers")} onClick={() => setAllVis(true)}><Eye size={14} /></button>
              <button className="icon" title={t("editor:layout.hideAllLayers")} onClick={() => setAllVis(false)}><EyeOff size={14} /></button>
            </div>
            <div className="layerlist">
              {shownLayers.map((l) => (
                <div key={l.id} className={`layerrow ${l.id === curLayer ? "cur" : ""} ${vis[l.id] ? "" : "hidden"}`}
                  onClick={() => setCurLayer(l.id)} title={t("editor:layout.layerTooltip", { name: l.name, source: l.source })}>
                  <img src={`data:image/png;base64,${l.icon}`} width={28} height={16} alt="" />
                  <span className="lname">{l.name}</span>
                  <span className="lsrc mono">{l.source}</span>
                  <button className="icon eye" aria-label={t(vis[l.id] ? "editor:layout.hideLayer" : "editor:layout.showLayer", { name: l.name })} onClick={(e) => { e.stopPropagation(); toggleVis(l.id, e.altKey); }}>
                    {vis[l.id] ? <Eye size={14} /> : <EyeOff size={14} />}
                  </button>
                </div>
              ))}
              {!shownLayers.length && <div className="empty">{t("editor:layout.noLayers")}</div>}
            </div>
          </>
        ) : (
          <Inspector sel={sel} dbu={dbu} info={info} onOpen={props.onOpenCell} />
        ))}
      </aside>
    </div>
  );
}

function hexA(hex: string, a: number): string {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}

function Inspector({ sel, dbu, info, onOpen }: { sel: SelItem[]; dbu: number; info: LayoutInfo | null; onOpen: (c: string) => void }) {
  useI18n();
  const u = (v: number) => fmt(v * dbu, 4);
  if (!sel.length) {
    return (
      <div className="inspector">
        <div className="kv"><span>{t("editor:property.cell")}</span><b>{info?.cell}</b></div>
        {info && <div className="kv"><span>{t("editor:property.size")}</span><b className="mono">{u(info.bbox[2] - info.bbox[0])} × {u(info.bbox[3] - info.bbox[1])} µm</b></div>}
        <div className="kv"><span>DBU</span><b className="mono">{info?.dbu} µm</b></div>
        <div className="kv"><span>{t("editor:property.subcells")}</span><b>{info?.children.length ?? 0}</b></div>
        <div className="childlist">
          {info?.children.slice(0, 200).map((c) => <button key={c} className="chip" onClick={() => onOpen(c)}>{c}</button>)}
        </div>
      </div>
    );
  }
  const s = sel[0];
  const b = s.bbox;
  return (
    <div className="inspector">
      {sel.length > 1 && <div className="note">{t("editor:selection.firstOfMany", { count: sel.length })}</div>}
      <div className="kv"><span>{t("editor:property.type")}</span><b>{s.type}</b></div>
      {s.layer && <div className="kv"><span>{t("editor:property.layer")}</span><b className="mono">{s.layer_name ? `${s.layer_name} ` : ""}{s.layer}</b></div>}
      {(s as SelItem & { inst_name?: string }).inst_name && <div className="kv"><span>{t("editor:view.schematic")}</span><b className="mono">{(s as SelItem & { inst_name?: string }).inst_name}</b></div>}
      {s.cell && <div className="kv"><span>{t("editor:property.cell")}</span><b><button className="link" onClick={() => onOpen(String(s.ref.c))}>{s.cell}</button></b></div>}
      {s.trans && <div className="kv"><span>{t("editor:property.transform")}</span><b className="mono">{s.trans}</b></div>}
      {s.text !== undefined && <div className="kv"><span>{t("editor:property.text")}</span><b>{s.text}</b></div>}
      {s.width !== undefined && <div className="kv"><span>{t("editor:property.width")}</span><b className="mono">{u(s.width)} µm</b></div>}
      <div className="kv"><span>{t("editor:property.origin")}</span><b className="mono">{u(b[0])}, {u(b[1])}</b></div>
      <div className="kv"><span>{t("editor:property.size")}</span><b className="mono">{u(b[2] - b[0])} × {u(b[3] - b[1])} µm</b></div>
      {s.array && <div className="kv"><span>{t("editor:property.array")}</span><b className="mono">{s.array.na} × {s.array.nb}</b></div>}
      {s.pcell && (
        <>
          <div className="section">PCell {s.pcell.library && <span className="dim">({s.pcell.library})</span>}</div>
          {Object.entries(s.pcell.params).map(([k, v]) => (
            <div className="kv" key={k}><span>{k}</span><b className="mono">{String(v)}</b></div>
          ))}
          <div className="note">{t("editor:layout.pcellImplementation")}</div>
        </>
      )}
    </div>
  );
}
