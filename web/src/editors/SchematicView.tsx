import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Box, ClipboardPaste, Copy, FlipHorizontal2, Info, Maximize, MousePointer2, Move, Pencil, Play, Redo2, RotateCw, ScrollText, Search, Shapes, ShieldCheck, SquareStack, Tag, Target, Undo2, X } from "lucide-react";
import { hashPanel } from "../App";
import { ask } from "../dialog";
import { useI18n, t, msg, type UiText, apiErrorText } from "../i18n";
import { api, ChatItem, InstanceDetail, ProbeResult, SchEditBody, Schematic, SymbolEntry, SymbolPreview, type SymbolAffectedView } from "../api";
import { Camera, restoreView, saveView, viewKey } from "./camera";
import { drawGhost, drawSchematic } from "./schdraw";
import { NetlistPanel } from "../panels/NetlistPanel";
import { SimPanel } from "../panels/SimPanel";
import { IntentPanel } from "../panels/IntentPanel";
import { SymbolCheckPanel } from "../panels/SymbolCheckPanel";
import { T, rgba } from "../theme";
import { Splitter } from "../layout";
import { SchDiff, schDiff } from "./diff";
import { Point, WirePreview, wireTarget } from "./wireInteraction";
import { MovePreview, MoveState, MoveStatus } from "./moveInteraction";
import { copyEditorClipboard, readEditorClipboard, type EditorClipboard } from "./editorClipboard";

interface Props {
  project: string;
  lib: string;
  cell: string;
  view: "schematic" | "symbol";
  active: boolean;
  readonly: boolean;
  hasLayout: boolean;
  hasSymbol?: boolean;
  onOpenOwnSymbol?: () => void;
  probe: ProbeResult | null;
  diffWith?: string | null; // a changeset preview: draw what differs between it and its real project
  fitOnLoad?: boolean;      // the assistant is building this cell and the view follows it: show all of it
  onProbe: (q: { inst?: string; net?: string } | null) => void;
  onSelection?: (items: ChatItem[]) => void;
  onChanged?: (affected: SymbolAffectedView[]) => void;
  onOpenSymbol: (symbol: string) => void;
  onOpenLayout: () => void;
  toast: (msg: UiText, kind?: "error" | "info") => void;
}

type Tool = "select" | "move" | "wire" | "place" | "paste";
type Item = { k: "i" | "w"; id: string };
type Drag = { x: number; y: number; wx: number; wy: number; moved: boolean }
  & ({ kind: "pan" | "box" } | { kind: "move"; items: Item[]; automatic?: boolean });
const GRID = 10;
const snap = (v: number) => Math.round(v / GRID) * GRID;

function distToSeg(px: number, py: number, x1: number, y1: number, x2: number, y2: number): number {
  const dx = x2 - x1, dy = y2 - y1;
  const l2 = dx * dx + dy * dy;
  const t = l2 ? Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / l2)) : 0;
  return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
}

export function SchematicView(props: Props) {
  useI18n();
  const { project, lib, cell, view, active } = props;
  const editable = view === "schematic" && !props.readonly;
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const cam = useRef(new Camera(false)).current;
  const [data, setData] = useState<Schematic | null>(null);
  const [diff, setDiff] = useState<SchDiff | undefined>(undefined);
  const [tool, setToolState] = useState<Tool>("select");
  const [sel, setSel] = useState<Item[]>([]);
  const [cursor, setCursor] = useState<[number, number]>([0, 0]);
  const [panel, setPanel] = useState<"props" | "netlist" | "sim" | "check" | "intent">((hashPanel() as "props") || "props");
  const [picker, setPicker] = useState(false);
  const [placing, setPlacing] = useState<{ ref: string; sym: SymbolPreview; rot: number; flip: number; lab?: string } | null>(null);
  const [detail, setDetail] = useState<InstanceDetail | null>(null);
  const [tick, setTick] = useState(0);
  const [moveStatus, setMoveStatus] = useState<MoveStatus>("idle");
  const st = useRef({
    drag: null as Drag | null,
    move: { result: null, status: "idle" } as MoveState,
    moveBusy: false,
    loadVersion: 0,
    refreshVersion: 0,
    editPending: 0,
    loaded: false,
    mouse: [0, 0] as [number, number],
    wire: null as Point | null,
    route: null as Point[] | null,
    wireBusy: false,
    wireVersion: 0,
    placeVersion: 0,
    placeBusy: false,
    clipboardVersion: 0,
    paste: null as EditorClipboard | null,
    pasteBusy: false,
    labelDialog: false,
    pressed: false,
    raf: 0,
    space: false,
  }).current;
  const routePreview = useRef<WirePreview | null>(null);
  const movePreview = useRef<MovePreview | null>(null);
  const live = useRef({ data, sel, tool, placing, editable, probe: props.probe, diff: undefined as SchDiff | undefined, onChanged: props.onChanged });
  live.current = { data, sel, tool, placing, editable, probe: props.probe, diff, onChanged: props.onChanged };

  const setTool = (t: Tool) => {
    if (st.moveBusy) return;
    st.placeVersion++;
    st.clipboardVersion++;
    st.paste = null;
    cancelDrag();
    finishWire();
    if (t !== "place") setPlacing(null);
    setToolState(t);
    req();
  };

  // ------------------------------------------------------------------ loading

  // fit once the canvas has its real size (see LayoutEditor)
  const pendingFit = useRef<[number, number, number, number] | null>(null);
  const placed = useRef(false); // the camera has been fitted or restored: worth remembering
  const tryFit = () => {
    size();
    if (pendingFit.current && cam.w > 120 && cam.h > 120) {
      const b = pendingFit.current;
      cam.fit(b, 0.08);
      pendingFit.current = null;
      placed.current = b[2] - b[0] > 1 && b[3] - b[1] > 1;   // an empty cell's fit is not worth remembering: it fills up
    }
  };

  const size = () => {
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

  useEffect(() => {
    st.loadVersion++;
    st.refreshVersion++;
    st.editPending = 0;
    st.loaded = false;
    st.placeVersion++;
    st.clipboardVersion++;
    st.paste = null;
    st.pasteBusy = false;
    st.moveBusy = false;
    cancelDrag();
    finishWire();
    setSel([]);
    setPlacing(null);
    setPicker(false);
    setToolState("select");
    const key = viewKey(project, lib, cell, view);
    let gone = false;
    api.schematic(project, lib, cell, view).then((d) => {
      if (gone) return;
      st.loaded = true;
      live.current.data = d;
      setData(d);
      if (!props.fitOnLoad && restoreView(key, cam)) placed.current = true; // same cell, other version: keep position and scale
      else { pendingFit.current = d.bbox; tryFit(); }
      req();
      if (d.missing.length) props.toast(msg("editor:schematic.missingSymbolsToast", { symbols: d.missing.slice(0, 4).join(", "), more: d.missing.length > 4 ? " …" : "" }), "info");
    }).catch((e) => { if (!gone) props.toast(apiErrorText(e), "error"); });
    return () => { gone = true; st.loadVersion++; st.refreshVersion++; st.clipboardVersion++; st.paste = null; if (placed.current) saveView(key, cam); };
  }, [project, lib, cell, view]); // eslint-disable-line

  const edit = useCallback(async (body: SchEditBody, keepSel = false) => {
    const version = st.loadVersion;
    st.refreshVersion++;
    st.editPending++;
    try {
      const r = await api.schEdit(project, lib, cell, body);
      if (version !== st.loadVersion) return null;
      live.current.data = r.view;
      setData(r.view);
      setTick((t) => t + 1);
      if (!keepSel) setSel([]);
      if (r.affected?.length) live.current.onChanged?.(r.affected);
      return r;
    } catch (e) {
      if (version === st.loadVersion) props.toast(apiErrorText(e), "error");
      return null;
    } finally {
      if (version === st.loadVersion) st.editPending--;
    }
  }, [project, lib, cell]); // eslint-disable-line

  useEffect(() => {
    const preview = new MovePreview(
      (input) => api.schMovePreview(project, lib, cell, input),
      (state) => { st.move = state; setMoveStatus(state.status); req(); },
      0, // ask as soon as the previous answer is in; one request at a time already merges pointer moves
    );
    movePreview.current = preview;
    return () => {
      preview.dispose();
      if (movePreview.current === preview) movePreview.current = null;
      st.move = { result: null, status: "idle" };
    };
  }, [project, lib, cell, view]); // eslint-disable-line

  const cancelDrag = () => {
    if (st.drag?.kind === "move" && st.drag.automatic) setToolState("select");
    st.drag = null;
    st.pressed = false;
    st.space = false;
    movePreview.current?.clear();
    st.move = { result: null, status: "idle" };
    setMoveStatus("idle");
    req();
  };

  const requestMovePreview = () => {
    const dr = st.drag;
    if (dr?.kind !== "move" || !dr.moved || st.moveBusy || !live.current.editable) return;
    movePreview.current?.request({ items: dr.items, dx: snap(st.mouse[0]) - snap(dr.wx), dy: snap(st.mouse[1]) - snap(dr.wy) });
  };

  useEffect(() => {
    const preview = new WirePreview(
      async ([x, y], [x2, y2]) => (await api.schRoute(project, lib, cell, { x, y, x2, y2 })).points,
      (points) => { st.route = points; req(); },
      0,
    );
    routePreview.current = preview;
    return () => { preview.dispose(); if (routePreview.current === preview) routePreview.current = null; };
  }, [project, lib, cell]); // eslint-disable-line

  const requestWirePreview = () => {
    if (!st.wire || st.wireBusy || !live.current.editable) return;
    const target = wireTarget(live.current.data, st.mouse[0], st.mouse[1], 8 * cam.s);
    routePreview.current?.request(st.wire, target.point);
  };

  useEffect(() => {
    routePreview.current?.clear();
    if (!editable || !st.moveBusy) cancelDrag(); // A preview is valid only for the drawing revision it started from.
    if (!editable) { finishWire(); st.placeVersion++; st.clipboardVersion++; st.paste = null; setPlacing(null); setToolState("select"); }
    else requestWirePreview();
  }, [data, editable]); // eslint-disable-line

  // ------------------------------------------------------------------ drawing

  const req = () => {
    if (st.raf) return;
    st.raf = requestAnimationFrame(() => {
      st.raf = 0;
      draw();
    });
  };

  const draw = () => {
    const c = canvasRef.current;
    const d = st.move.result?.view ?? live.current.data;
    if (!c || !d) return;
    const ctx = c.getContext("2d")!;
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = T.bg;
    ctx.fillRect(0, 0, c.width, c.height);
    const k = 1 / cam.s;
    ctx.setTransform(dpr * k, 0, 0, dpr * k, dpr * (cam.w / 2 - cam.cx * k), dpr * (cam.h / 2 - cam.cy * k));
    const px = cam.s;

    const [vx1, vy1, vx2, vy2] = cam.view();
    let g = GRID;
    while (g / cam.s < 12) g *= 2;
    if (((vx2 - vx1) / g) * ((vy2 - vy1) / g) < 30000) {
      ctx.fillStyle = rgba("dim", 0.18);
      for (let x = Math.ceil(vx1 / g) * g; x <= vx2; x += g)
        for (let y = Math.ceil(vy1 / g) * g; y <= vy2; y += g) ctx.fillRect(x - px / 2, y - px / 2, px, px);
    }

    const L = live.current;
    const hi = new Set<string>();
    if (d.wire_nets) {
      const nets = new Set<string>();
      d.wires.forEach((w, i) => { if (L.sel.some((s) => s.k === "w" && s.id === w[6])) nets.add(d.wire_nets![i]); });
      d.wire_nets.forEach((n, i) => { if (nets.has(n)) hi.add(d.wires[i][6]); });
    }
    const pr = L.probe;
    drawSchematic(ctx, d, cam, {
      selInsts: new Set(L.sel.filter((s) => s.k === "i").map((s) => s.id)),
      selWires: new Set(L.sel.filter((s) => s.k === "w").map((s) => s.id)),
      hiWires: hi,
      probeInsts: pr ? new Set(pr.sch_insts) : undefined,
      probeWires: pr ? new Set(pr.wires) : undefined,
      showPins: view === "schematic",
      diff: L.diff,
      pending: view === "schematic" ? d.pending : undefined,
    });

    const m = st.mouse;
    if (L.tool === "paste" && st.paste) {
      const [dx, dy] = pasteOffset(st.paste, m[0], m[1]);
      ctx.save();
      ctx.translate(dx, dy);
      ctx.globalAlpha = 0.65;
      drawSchematic(ctx, st.paste.preview as Schematic, cam, { showPins: true });
      ctx.restore();
    }
    if (L.tool === "place" && L.placing) {
      const [x, y] = L.placing.lab !== undefined ? wireTarget(d, m[0], m[1], 8 * cam.s).point : [snap(m[0]), snap(m[1])];
      drawGhost(ctx, L.placing.sym, cam, x, y, L.placing.rot, L.placing.flip, L.placing.lab !== undefined);
    }
    if (L.tool === "wire") {
      if (st.route?.length) {
        ctx.strokeStyle = T.ok;
        ctx.lineWidth = 1.6 * px;
        ctx.setLineDash([4 * px, 3 * px]);
        ctx.beginPath();
        st.route.forEach(([x, y], i) => (i ? ctx.lineTo : ctx.moveTo).call(ctx, x, y));
        ctx.stroke();
        ctx.setLineDash([]);
      }
      ctx.strokeStyle = rgba("text", 0.8);
      ctx.lineWidth = px;
      const target = wireTarget(d, m[0], m[1], 8 * cam.s);
      const [mx, my] = target.point;
      ctx.beginPath();
      ctx.moveTo(mx - 7 * px, my); ctx.lineTo(mx + 7 * px, my);
      ctx.moveTo(mx, my - 7 * px); ctx.lineTo(mx, my + 7 * px);
      ctx.stroke();
      if (target.connected) {
        ctx.strokeStyle = T.ok;
        ctx.beginPath(); ctx.arc(mx, my, 5 * px, 0, Math.PI * 2); ctx.stroke();
      }
      if (st.wire) {
        ctx.fillStyle = T.ok;
        ctx.fillRect(st.wire[0] - 2 * px, st.wire[1] - 2 * px, 4 * px, 4 * px);
      }
    }
    if (st.drag?.kind === "move" && st.drag.moved && st.move.status !== "ready") {
      const dx = snap(m[0]) - snap(st.drag.wx), dy = snap(m[1]) - snap(st.drag.wy);
      ctx.strokeStyle = rgba("sel", 0.9);
      ctx.lineWidth = 1.5 * px;
      ctx.setLineDash([5 * px, 4 * px]);
      for (const it of L.sel) {
        if (it.k === "i") {
          const inst = L.data?.insts.find((x) => x.id === it.id);
          if (inst) ctx.strokeRect(inst.bbox[0] + dx, inst.bbox[1] + dy, inst.bbox[2] - inst.bbox[0], inst.bbox[3] - inst.bbox[1]);
        } else {
          const w = L.data?.wires.find((x) => x[6] === it.id);
          if (!w) continue;
          ctx.beginPath(); ctx.moveTo(w[0] + dx, w[1] + dy); ctx.lineTo(w[2] + dx, w[3] + dy); ctx.stroke();
        }
      }
      ctx.setLineDash([]);
    }
    if (st.drag?.kind === "box" && st.drag.moved) {
      const x = Math.min(st.drag.wx, m[0]), y = Math.min(st.drag.wy, m[1]);
      const width = Math.abs(m[0] - st.drag.wx), height = Math.abs(m[1] - st.drag.wy);
      ctx.strokeStyle = T.accent;
      ctx.fillStyle = rgba("accent", 0.10);
      ctx.lineWidth = px;
      ctx.fillRect(x, y, width, height);
      ctx.strokeRect(x, y, width, height);
    }
  };

  useEffect(req, [data, sel, tool, placing, props.probe, diff]); // eslint-disable-line

  // differences between the proposal and the confirmed design, whichever of the two is shown
  const diffWith = props.diffWith;
  useEffect(() => {
    if (!diffWith || view !== "schematic") { setDiff(undefined); return; }
    let gone = false;
    Promise.all([api.schematic(diffWith, lib, cell, view), api.schematic(diffWith.split("~")[0], lib, cell, view)])
      .then(([now, before]) => { if (!gone) setDiff(schDiff(now, before)); })
      .catch(() => { if (!gone) setDiff(undefined); });
    return () => { gone = true; };
  }, [diffWith, lib, cell, view, data]);
  useEffect(() => {
    const ro = new ResizeObserver(() => { tryFit(); req(); });
    if (wrapRef.current) ro.observe(wrapRef.current);
    const el = wrapRef.current!;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      cam.zoomAt(e.clientX - r.left, e.clientY - r.top, Math.pow(1.0018, e.deltaMode === 1 ? e.deltaY * 30 : e.deltaY), 1e-3, 1e3);
      st.mouse = cam.toWorld(e.clientX - r.left, e.clientY - r.top);
      requestMovePreview();
      requestWirePreview();
      req();
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => { ro.disconnect(); el.removeEventListener("wheel", onWheel); };
  }, []); // eslint-disable-line
  useEffect(() => {
    const releaseOutside = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) cancelDrag();
    };
    const releaseSpace = (e: KeyboardEvent) => { if (e.key === " ") st.space = false; };
    window.addEventListener("blur", cancelDrag);
    window.addEventListener("mouseup", releaseOutside);
    window.addEventListener("keyup", releaseSpace);
    return () => {
      window.removeEventListener("blur", cancelDrag);
      window.removeEventListener("mouseup", releaseOutside);
      window.removeEventListener("keyup", releaseSpace);
      if (st.raf) cancelAnimationFrame(st.raf);
      st.raf = 0; // StrictMode replays effects; allow the next setup to schedule a fresh frame.
    };
  }, []); // eslint-disable-line
  useEffect(() => {
    st.refreshVersion++;
    if (!active) { cancelDrag(); st.clipboardVersion++; st.paste = null; setToolState("select"); return; }
    tryFit(); req(); wrapRef.current?.focus();
    // The symbol may have changed in another editor while this tab was hidden.
    // This read must never replace a move preview, pending edit or unfinished placement.
    const idle = () => !st.editPending && !st.moveBusy && !st.wireBusy && !st.placeBusy && !st.pasteBusy
      && !st.drag && !st.wire && !st.paste && !st.labelDialog && live.current.tool === "select" && !live.current.placing;
    if (!st.loaded || !idle()) return;
    const version = st.refreshVersion, loadVersion = st.loadVersion;
    api.schematic(project, lib, cell, view).then((d) => {
      if (version !== st.refreshVersion || loadVersion !== st.loadVersion || !idle()) return;
      live.current.data = d;
      setData(d);
      setSel((items) => items.filter((item) => item.k === "i" ? d.insts.some((i) => i.id === item.id) : d.wires.some((w) => w[6] === item.id)));
      setTick((n) => n + 1);
      req();
    }).catch((e) => { if (version === st.refreshVersion && loadVersion === st.loadVersion) props.toast(apiErrorText(e), "error"); });
    return () => { st.refreshVersion++; };
  }, [active]); // eslint-disable-line

  // zoom to cross-probed objects coming from the layout
  useEffect(() => {
    const pr = props.probe;
    if (!pr || !data || !pr.sch_insts.length || sel.length) return;
    const bs = data.insts.filter((i) => pr.sch_insts.includes(i.id)).map((i) => i.bbox);
    if (!bs.length) return;
    const b: [number, number, number, number] = [Math.min(...bs.map((x) => x[0])), Math.min(...bs.map((x) => x[1])),
      Math.max(...bs.map((x) => x[2])), Math.max(...bs.map((x) => x[3]))];
    const [vx1, vy1, vx2, vy2] = cam.view();
    if (b[0] < vx1 || b[1] < vy1 || b[2] > vx2 || b[3] > vy2) {
      cam.fit([b[0] - 80, b[1] - 80, b[2] + 80, b[3] + 80], 0.1);
      req();
    }
  }, [props.probe]); // eslint-disable-line

  // ------------------------------------------------------------------ selection

  const pos = (e: React.MouseEvent): [number, number] => {
    const r = canvasRef.current!.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  };

  const hit = (sx: number, sy: number): Item | null => {
    const d = live.current.data;
    if (!d) return null;
    const [x, y] = cam.toWorld(sx, sy);
    const tol = 6 * cam.s;
    let best: Item | null = null, bd = tol;
    d.wires.forEach((w) => {
      const dd = distToSeg(x, y, w[0], w[1], w[2], w[3]);
      if (dd < bd) { bd = dd; best = { k: "w", id: w[6] }; }
    });
    let area = Infinity;
    let ibest: Item | null = null;
    for (const inst of d.insts) {
      const [x1, y1, x2, y2] = inst.bbox;
      if (x >= x1 - 2 && x <= x2 + 2 && y >= y1 - 2 && y <= y2 + 2) {
        const a = (x2 - x1) * (y2 - y1);
        if (a < area) { area = a; ibest = { k: "i", id: inst.id }; }
      }
    }
    // a wire wins when the cursor is really on it; otherwise the instance
    return ibest && (!best || bd > 2 * cam.s) ? ibest : best ?? ibest;
  };

  const instOf = (id: string) => data?.insts.find((x) => x.id === id);
  const wireNet = (id: string) => { const i = data?.wires.findIndex((w) => w[6] === id) ?? -1; return i >= 0 ? data?.wire_nets?.[i] : undefined; };

  const boxHits = (x1: number, y1: number, x2: number, y2: number): Item[] => {
    const d = live.current.data;
    if (!d) return [];
    const left = Math.min(x1, x2), right = Math.max(x1, x2), top = Math.min(y1, y2), bottom = Math.max(y1, y2);
    const inside = (x: number, y: number) => x >= left && x <= right && y >= top && y <= bottom;
    return [
      ...d.insts.filter((i) => inside(i.bbox[0], i.bbox[1]) && inside(i.bbox[2], i.bbox[3]))
        .map((i): Item => ({ k: "i", id: i.id })),
      ...d.wires.filter((w) => inside(w[0], w[1]) && inside(w[2], w[3]))
        .map((w): Item => ({ k: "w", id: w[6] })),
    ];
  };

  const selectHits = (hits: Item[], toggle: boolean) => {
    setSel((current) => {
      if (!toggle) return hits;
      const keys = new Set(hits.map((item) => `${item.k}:${item.id}`));
      return [...current.filter((item) => !keys.has(`${item.k}:${item.id}`)),
        ...hits.filter((item) => !current.some((selected) => selected.k === item.k && selected.id === item.id))];
    });
  };

  // selection -> assistant attachments (ids only; the server resolves them against the design)
  useEffect(() => {
    if (!props.onSelection || !data) return;
    const items: ChatItem[] = [];
    const nets = new Set<string>();
    for (const s of sel) {
      if (s.k === "i") items.push({ k: "i", id: s.id, label: s.id });
      else { const n = wireNet(s.id); if (n && !nets.has(n)) { nets.add(n); items.push({ k: "n", id: n, label: `net ${n}` }); } }
    }
    props.onSelection(items);
  }, [sel, data]); // eslint-disable-line

  // selection -> cross-probe + inspector detail
  useEffect(() => {
    if (!data) return;
    let gone = false;
    if (sel.length === 1 && sel[0].k === "i") {
      const inst = instOf(sel[0].id);
      if (inst?.name) props.onProbe({ inst: inst.name });
      setDetail(null);
      api.schInstance(project, lib, cell, sel[0].id)
        .then((value) => { if (!gone) setDetail(value); })
        .catch(() => { if (!gone) setDetail(null); });
    } else {
      setDetail(null);
      const w = sel.find((s) => s.k === "w");
      if (w && wireNet(w.id)) props.onProbe({ net: wireNet(w.id)! });
      else if (!sel.length) props.onProbe(null);
    }
    return () => { gone = true; };
  }, [sel, data]); // eslint-disable-line

  // ------------------------------------------------------------------ actions

  const doSave = () => {
    wrapRef.current?.focus();
    props.toast(msg("editor:schematic.savedImmediately"));
  };
  const moving = () => st.moveBusy || st.pasteBusy || st.drag?.kind === "move" && st.drag.moved;
  const doDelete = () => { if (sel.length && editable && !st.wireBusy && !moving()) edit({ op: "delete", items: sel }); };
  const doRotate = () => {
    if (placing) { setPlacing({ ...placing, rot: (placing.rot + 1) & 3 }); return; }
    if (sel.length && editable && !st.wireBusy && !moving()) edit({ op: "orient", items: sel, rot: 1 }, true);
  };
  const doFlip = () => {
    if (placing) { setPlacing({ ...placing, flip: placing.flip ^ 1 }); return; }
    if (sel.length && editable && !st.wireBusy && !moving()) edit({ op: "orient", items: sel, flip: 1 }, true);
  };
  const doDup = async () => {
    if (!sel.length || !editable || st.wireBusy || moving()) return;
    const r = await edit({ op: "move", items: sel, dx: 20, dy: 20, duplicate: true }, true);
    if (r) selectEditResult(r);
  };

  const pasteOffset = (clipboard: EditorClipboard, x: number, y: number): Point => [
    snap(x - (clipboard.bbox[0] + clipboard.bbox[2]) / 2),
    snap(y - (clipboard.bbox[1] + clipboard.bbox[3]) / 2),
  ];

  const doCopy = async () => {
    if (view !== "schematic" || !sel.length || st.wireBusy || moving()) return;
    wrapRef.current?.focus();
    const version = st.loadVersion;
    const items = sel.map((item) => ({ ...item }));
    try {
      await copyEditorClipboard(api.schCopy(project, lib, cell, items).then((result) => ({
        version: 1, kind: "schematic", source: { project, lib }, ...result,
      })));
      if (version === st.loadVersion) props.toast(msg("editor:clipboard.copied", { count: items.length }), "info");
    } catch (error) {
      if (version === st.loadVersion) props.toast(apiErrorText(error), "error");
    }
  };

  const doPaste = async () => {
    if (!live.current.editable || st.wireBusy || st.placeBusy || moving()) return;
    setTool("select");
    setPicker(false);
    const version = st.clipboardVersion, loadVersion = st.loadVersion;
    try {
      const clipboard = await readEditorClipboard();
      if (version !== st.clipboardVersion || loadVersion !== st.loadVersion || !live.current.editable) return;
      if (!clipboard) { props.toast(msg("editor:clipboard.empty"), "info"); return; }
      if (clipboard.kind !== "schematic") { props.toast(msg("editor:clipboard.wrongView", { view: t("editor:view.schematic") }), "info"); return; }
      if (clipboard.source.project.split("~")[0] !== project.split("~")[0] || clipboard.source.lib !== lib) { props.toast(msg("editor:clipboard.wrongLibrary"), "info"); return; }
      setTool("paste");
      st.paste = clipboard;
      setSel([]);
      wrapRef.current?.focus();
      req();
    } catch (error) {
      if (version === st.clipboardVersion && loadVersion === st.loadVersion) props.toast(apiErrorText(error), "error");
    }
  };

  const commitPaste = async (x: number, y: number) => {
    const clipboard = st.paste;
    if (!clipboard || !live.current.editable || st.pasteBusy) return;
    const [dx, dy] = pasteOffset(clipboard, x, y);
    const version = st.clipboardVersion, loadVersion = st.loadVersion;
    st.refreshVersion++;
    st.pasteBusy = true;
    try {
      const result = await api.schPaste(project, lib, cell, { source: clipboard.source, payload: clipboard.payload, dx, dy });
      if (loadVersion !== st.loadVersion) return;
      live.current.data = result.view;
      setData(result.view);
      setTick((tick) => tick + 1);
      if (version === st.clipboardVersion && live.current.editable) {
        selectEditResult(result);
        st.paste = null;
        setToolState("select");
      }
    } catch (error) {
      if (loadVersion === st.loadVersion) props.toast(apiErrorText(error), "error");
    } finally {
      if (loadVersion === st.loadVersion) { st.pasteBusy = false; req(); }
    }
  };

  const selectEditResult = (result: { result: unknown; view: Schematic }) => {
    if (!Array.isArray(result.result)) return;
    const instances = new Set(result.view.insts.map((i) => i.id));
    const wires = new Set(result.view.wires.map((w) => w[6]));
    const selected: Item[] = [];
    for (const id of result.result) {
      if (typeof id !== "string") continue;
      if (instances.has(id)) selected.push({ k: "i", id });
      else if (wires.has(id)) selected.push({ k: "w", id });
    }
    setSel(selected);
  };
  const doUndo = (redo: boolean) => {
    if (editable && !st.wireBusy && !moving()) { finishWire(); edit({ op: redo ? "redo" : "undo" }); }
  };
  const fit = () => { if (data) { cam.fit(data.bbox, 0.08); req(); } };

  const choose = async (ref: string, lab?: string) => {
    if (!live.current.editable || moving()) return;
    const version = st.placeVersion, loadVersion = st.loadVersion;
    try {
      const sym = await api.symbol(project, ref);
      if (version !== st.placeVersion || loadVersion !== st.loadVersion || !live.current.editable) return;
      setPicker(false);
      setTool("place");
      // lab_pin's preview uses its default net name; show the name being placed.
      const preview = lab === undefined ? sym : { ...sym, texts: sym.texts.map((text) => text.s === "xxx" ? { ...text, s: lab } : text) };
      setPlacing({ ref, sym: preview, rot: 0, flip: 0, lab });
      wrapRef.current?.focus();
    } catch (e) {
      if (version === st.placeVersion && loadVersion === st.loadVersion) props.toast(apiErrorText(e), "error");
    }
  };

  const placeLabel = async () => {
    if (!live.current.editable || moving() || st.labelDialog || st.placeBusy) return;
    setTool("select");
    setPicker(false);
    const version = st.placeVersion, loadVersion = st.loadVersion;
    st.labelDialog = true;
    try {
      const values = await ask({
        title: msg("editor:schematic.newLabel"), submitLabel: msg("editor:action.place"),
        message: msg("editor:schematic.labelHelp"),
        fields: [{ name: "lab", label: msg("editor:schematic.netName"), required: true, mono: true,
          placeholder: msg("editor:schematic.labelExample"),
          suggestions: [...new Set(live.current.data?.pins?.map((pin) => pin[4]) ?? [])],
        }],
      });
      if (version !== st.placeVersion || loadVersion !== st.loadVersion || !live.current.editable) return;
      wrapRef.current?.focus();
      if (values?.lab.trim()) await choose("devices/lab_pin.sym", values.lab.trim());
    } finally { st.labelDialog = false; }
  };

  const finishWire = () => {
    st.wire = null;
    st.wireVersion++;
    routePreview.current?.clear();
    req();
  };

  const onClick = async (e: React.MouseEvent) => {
    if (e.button !== 0 || st.space || e.detail > 1 || st.wireBusy || st.moveBusy || st.placeBusy || st.pasteBusy) return;
    const [sx, sy] = pos(e);
    const [wx, wy] = cam.toWorld(sx, sy);
    const t = live.current.tool;
    if (t === "paste" && editable) {
      await commitPaste(wx, wy);
    } else if (t === "place" && placing && editable) {
      const [x, y] = placing.lab !== undefined ? wireTarget(live.current.data, wx, wy, 8 * cam.s).point : [snap(wx), snap(wy)];
      st.placeBusy = true;
      try {
        await edit({ op: "add_instance", sym: placing.ref, x, y, rot: placing.rot, flip: placing.flip,
          ...(placing.lab === undefined ? {} : { props: { lab: placing.lab } }) }, true);
      } finally { st.placeBusy = false; }
    } else if (t === "wire" && editable) {
      const target = wireTarget(live.current.data, wx, wy, 8 * cam.s);
      const [x, y] = target.point;
      st.mouse = [wx, wy];
      if (!st.wire) st.wire = [x, y];
      else {
        const [lx, ly] = st.wire;
        if (x === lx && y === ly) return;
        const version = st.wireVersion;
        st.wireBusy = true;
        routePreview.current?.clear();
        try {
          const result = await edit({ op: "route_wire", x: lx, y: ly, x2: x, y2: y }, true);
          // Escape/tool changes may end drawing while this atomic edit completes.
          // A rejected route leaves the last successful corner as the anchor.
          if (result && version === st.wireVersion) {
            if (target.connected) finishWire();
            else st.wire = [x, y];
          }
        } finally {
          st.wireBusy = false;
        }
      }
      requestWirePreview();
      req();
    }
  };

  const onMouseDown = (e: React.MouseEvent) => {
    if (st.moveBusy || st.pasteBusy) return;
    if ((e.target as HTMLElement).closest("button, input, select, textarea, [role=dialog]")) return;
    wrapRef.current?.focus();
    st.pressed = e.button === 0;
    const [sx, sy] = pos(e);
    const [wx, wy] = cam.toWorld(sx, sy);
    const t = live.current.tool;
    if (e.button === 1 || e.button === 2 || st.space) {
      st.drag = { kind: "pan", x: sx, y: sy, wx, wy, moved: false };
      e.preventDefault();
      return;
    }
    if (e.button !== 0) return;
    if (st.wireBusy) return;
    if (t === "select" || t === "move" && e.shiftKey) {
      if (editable && sel.length && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) {
        movePreview.current?.clear();
        st.drag = { kind: "move", x: sx, y: sy, wx, wy, moved: false,
          items: sel.map((item) => ({ ...item })), automatic: true };
      } else st.drag = { kind: "box", x: sx, y: sy, wx, wy, moved: false };
    } else if (t === "move" && editable) {
      const h = hit(sx, sy);
      if (h) {
        const already = sel.some((s) => s.k === h.k && s.id === h.id);
        const next = already ? sel : [h];
        setSel(next);
        movePreview.current?.clear();
        st.drag = { kind: "move", x: sx, y: sy, wx, wy, moved: false, items: next };
      }
    }
  };

  const onMouseMove = (e: React.MouseEvent) => {
    const [sx, sy] = pos(e);
    const w = cam.toWorld(sx, sy);
    st.mouse = w;
    setCursor(live.current.tool === "wire" || live.current.placing?.lab !== undefined ? wireTarget(live.current.data, w[0], w[1], 8 * cam.s).point : [snap(w[0]), snap(w[1])]);
    const dr = st.drag;
    if (dr) {
      if (dr.kind === "pan") {
        if (Math.hypot(sx - dr.x, sy - dr.y) > 2 || dr.moved) {
          cam.pan(sx - dr.x, sy - dr.y);
          st.drag = { ...dr, x: sx, y: sy, moved: true };
        }
      } else if (Math.hypot(sx - dr.x, sy - dr.y) > 4 || dr.moved) {
        if (!dr.moved && dr.kind === "move" && dr.automatic) setToolState("move");
        st.drag = { ...dr, moved: true };
      }
    }
    requestMovePreview();
    requestWirePreview();
    req();
  };

  const onMouseUp = async (e: React.MouseEvent) => {
    if (st.moveBusy || st.pasteBusy) return;
    const dr = st.drag;
    const pressed = st.pressed;
    st.pressed = false;
    st.drag = null;
    if (dr?.kind === "move" && dr.automatic) setToolState("select");
    if (!pressed || e.button !== 0 || st.space || dr?.kind === "pan"
      || (e.target as HTMLElement).closest("button, input, select, textarea, [role=dialog]")) { movePreview.current?.clear(); req(); return; }
    const [sx, sy] = pos(e);
    const t = live.current.tool;
    if (dr?.kind === "box") {
      const [wx, wy] = cam.toWorld(sx, sy);
      const clicked = dr.moved ? null : hit(sx, sy);
      selectHits(dr.moved ? boxHits(dr.wx, dr.wy, wx, wy) : clicked ? [clicked] : [], e.shiftKey);
      req();
      return;
    }
    if (dr?.kind === "move" && dr.automatic && !dr.moved) {
      const clicked = hit(sx, sy);
      selectHits(clicked ? [clicked] : [], e.shiftKey);
      req();
      return;
    }
    if (dr?.kind === "move" && dr.moved && editable && !st.wireBusy && !st.moveBusy) {
      const [wx, wy] = cam.toWorld(sx, sy);
      const dx = snap(wx) - snap(dr.wx), dy = snap(wy) - snap(dr.wy);
      if (dx || dy) {
        const version = st.loadVersion;
        st.moveBusy = true;
        movePreview.current?.hold();
        try {
          const r = await edit({ op: "move", items: dr.items, dx, dy }, true);
          if (r) selectEditResult(r); // routing can split one selected wire into several segments
        } finally {
          if (version === st.loadVersion) {
            st.moveBusy = false;
            movePreview.current?.clear();
          }
        }
      } else movePreview.current?.clear();
      req();
      return;
    }
    if (!dr || !dr.moved) {
      if (t !== "select") await onClick(e);
    }
    req();
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if ((e.target as HTMLElement).closest("input, select, textarea, [contenteditable]:not([contenteditable=false])")) return;
    const k = e.key, ctrl = e.ctrlKey || e.metaKey;
    if (ctrl && k.toLowerCase() === "c") { e.preventDefault(); void doCopy(); return; }
    if (ctrl && k.toLowerCase() === "v") { e.preventDefault(); void doPaste(); return; }
    if (ctrl && k.toLowerCase() === "z") { e.preventDefault(); doUndo(e.shiftKey); return; }
    if (ctrl && k.toLowerCase() === "s") { e.preventDefault(); doSave(); return; }
    if (ctrl && k.toLowerCase() === "d") { e.preventDefault(); doDup(); return; }
    if (ctrl) return;
    switch (k) {
      case "f": fit(); break;
      case "Escape": {
        const interacting = st.drag?.kind === "box" || st.drag?.kind === "move";
        cancelDrag();
        if (st.wire) finishWire(); else { setTool("select"); if (!interacting) setSel([]); }
        break;
      }
      case "Enter": finishWire(); break;
      case "Delete": case "Backspace": doDelete(); break;
      case "r": doRotate(); break;
      case "x": doFlip(); break;
      case "u": doUndo(false); break;
      case "U": doUndo(true); break;
      case "w": if (editable) setTool("wire"); break;
      case "m": if (editable) setTool("move"); break;
      case "i": if (editable && !moving()) setPicker(true); break;
      case "l": if (editable && !moving()) void placeLabel(); break;
      case " ": st.space = true; break;
      default: return;
    }
    e.preventDefault();
    req();
  };

  // ------------------------------------------------------------------ side panels

  const selInst = sel.length === 1 && sel[0].k === "i" ? instOf(sel[0].id) : null;
  const selWire = sel.length === 1 && sel[0].k === "w" ? sel[0].id : null;
  const selWireData = selWire != null ? data?.wires.find((w) => w[6] === selWire) : undefined;
  const netName = selWire != null ? wireNet(selWire) ?? null : null;
  const netPins = useMemo(() => {
    if (!netName || !data?.pins) return [];
    return data.pins.filter((p) => p[4] === netName).map((p) => `${p[0]}.${p[1]}`);
  }, [netName, data]); // eslint-disable-line

  return (
    <div className="editor">
      <div className="toolrail">
        <button className={`railbtn ${tool === "select" ? "on" : ""}`} title={t("editor:action.selectShortcut")} onClick={() => setTool("select")}><MousePointer2 size={17} strokeWidth={1.75} /></button>
        {view === "schematic" && (
          <>
            <button className={`railbtn ${tool === "move" ? "on" : ""}`} disabled={!editable || moveStatus !== "idle"} title={t("editor:schematic.moveShortcut")} onClick={() => setTool("move")}><Move size={17} strokeWidth={1.75} /></button>
            <button className={`railbtn ${tool === "wire" ? "on" : ""}`} disabled={!editable} title={t("editor:schematic.wireShortcut")} onClick={() => setTool("wire")}><Pencil size={17} strokeWidth={1.75} /></button>
            <button className={`railbtn ${tool === "place" && placing?.lab === undefined ? "on" : ""}`} disabled={!editable || moveStatus !== "idle"} title={t("editor:schematic.placeSymbolShortcut")} onClick={() => setPicker(true)}><SquareStack size={17} strokeWidth={1.75} /></button>
            <button className={`railbtn ${tool === "place" && placing?.lab !== undefined ? "on" : ""}`} disabled={!editable || moveStatus !== "idle"} title={t("editor:schematic.placeLabelShortcut")} onClick={() => void placeLabel()}><Tag size={17} strokeWidth={1.75} /></button>
            <div className="railsep" />
            <button className="railbtn" disabled={!editable || (!sel.length && !placing)} title={t("editor:action.rotateShortcut")} onClick={doRotate}><RotateCw size={17} strokeWidth={1.75} /></button>
            <button className="railbtn" disabled={!editable || (!sel.length && !placing)} title={t("editor:action.mirrorShortcut")} onClick={doFlip}><FlipHorizontal2 size={17} strokeWidth={1.75} /></button>
            <button className="railbtn" disabled={!sel.length} title={t("editor:clipboard.copyShortcut")} onClick={() => void doCopy()}><Copy size={17} strokeWidth={1.75} /></button>
            <button className={`railbtn ${tool === "paste" ? "on" : ""}`} disabled={!editable} title={t("editor:clipboard.pasteShortcut")} onClick={() => void doPaste()}><ClipboardPaste size={17} strokeWidth={1.75} /></button>
          </>
        )}
        <div className="railsep" />
        <button className="railbtn" title={t("editor:action.fitShortcut")} onClick={fit}><Maximize size={17} strokeWidth={1.75} /></button>
        {view === "schematic" && (
          <>
            <button className="railbtn" disabled={!editable || !data?.redo} title={t("editor:action.redoShortcut")} onClick={() => doUndo(true)}><Redo2 size={17} strokeWidth={1.75} /></button>
            <button className="railbtn" disabled={!editable || !data?.undo} title={t("editor:action.undoShortcut")} onClick={() => doUndo(false)}><Undo2 size={17} strokeWidth={1.75} /></button>
          </>
        )}
        {view === "schematic" && (props.hasLayout || props.onOpenOwnSymbol) && (
          <>
            <div className="railsep" />
            {props.hasLayout && <button className="railbtn" title={t("editor:action.openLayout")} onClick={props.onOpenLayout}><Box size={17} strokeWidth={1.75} /></button>}
            {props.onOpenOwnSymbol && <button className="railbtn" title={t(props.hasSymbol ? "symbolcheck:open" : "symbolcheck:create")}
              disabled={!props.hasSymbol && !editable} onClick={props.onOpenOwnSymbol}><Shapes size={17} strokeWidth={1.75} /></button>}
          </>
        )}
      </div>

      <div className="canvaswrap" ref={wrapRef} tabIndex={0}
        onMouseDown={onMouseDown} onMouseMove={onMouseMove} onMouseUp={onMouseUp}
        onMouseLeave={cancelDrag}
        onContextMenu={(e) => e.preventDefault()}
        onDoubleClick={(e) => {
          if (e.button !== 0 || st.space || (e.target as HTMLElement).closest("button, input, select, textarea, [role=dialog]")) return;
          if (tool === "wire") { finishWire(); return; }
          if (tool !== "select") return;
          const h = hit(...pos(e));
          if (h?.k === "i") { const i = instOf(h.id); if (i) props.onOpenSymbol(i.symbol); }
        }}
        onKeyDown={onKeyDown} onKeyUp={(e) => { if (e.key === " ") st.space = false; }}
        style={{ cursor: tool === "select" ? "default" : tool === "move" ? "move" : "crosshair" }}>
        <canvas ref={canvasRef} />
        <div className="hud-top">
          <span className="hud-chip">{view === "schematic" ? (tool === "select" ? t("editor:view.schematic") : tool === "move" ? t("editor:tool.move") : tool === "wire" ? t("editor:tool.wire") : tool === "paste" ? t("editor:clipboard.tool") : t("editor:tool.placeNamed", { name: placing?.lab ?? placing?.ref ?? "" })) : t("editor:view.symbol")}</span>
          <span className="hud-hint">
            {tool === "wire" ? t("editor:schematic.help.wire")
              : tool === "move" ? t("editor:schematic.help.move")
              : tool === "place" ? t("editor:schematic.help.place")
                : tool === "paste" ? t("editor:clipboard.help")
                : editable ? t("editor:schematic.help.edit")
                  : t("editor:schematic.help.readOnly")}
          </span>
        </div>
        <div className="statusbar">
          <span className="mono coord">X {cursor[0].toFixed(1)}</span>
          <span className="mono coord">Y {cursor[1].toFixed(1)}</span>
          <span className="sep" />
          <span className="dim">{data ? t("editor:schematic.counts", { instances: data.insts.length, wires: data.wires.length }) : ""}</span>
          {sel.length ? <span className="dim">{t("editor:selection.count", { count: sel.length })}</span> : null}
          {moveStatus !== "idle" && <span role="status" className={moveStatus === "blocked" ? "warn" : "dim"}>{t(`editor:schematic.move.${moveStatus}`)}</span>}
          <span className="grow" />
          {data?.pending?.length ? <span className="warn" title={`${t("editor:schematic.pendingHint")}\n${data.pending.map(([i, p, n]) => `${i}.${p} → ${n}`).join("\n")}`}>{t("editor:schematic.pendingCount", { count: data.pending.length })}</span> : null}
          {data?.placed_auto?.length && editable ? <button className="link small" title={t("editor:schematic.tidyHint")} onClick={async () => {
            try {
              await api.netlistEdit(project, lib, cell, { op: "tidy" });
              const d = await api.schematic(project, lib, cell, view);
              live.current.data = d; setData(d); setTick((n) => n + 1); req();
            } catch (e) { props.toast(apiErrorText(e), "error"); }
          }}>{t("editor:schematic.tidy", { count: data.placed_auto.length })}</button> : null}
          {data?.warnings?.length ? <span className="warn" title={data.warnings.join("\n")}>{t("editor:schematic.warningCount", { count: data.warnings.length })}</span> : null}
          {data?.missing.length ? <span className="warn">{t("editor:schematic.missingCount", { count: data.missing.length })}</span> : null}
          {props.readonly && <span className="dim mono">{t("editor:status.readOnly")}</span>}
        </div>
        {picker && <SymbolPicker project={project} onPick={choose} onClose={() => { setPicker(false); wrapRef.current?.focus(); }} />}
      </div>

      <Splitter pane="right" />
      <aside className="sidepanel">
        <div className="seg">
          <button className={panel === "props" ? "on" : ""} onClick={() => setPanel("props")}><Info size={14} /> {t("editor:property.propertiesShort")}</button>
          {view === "schematic" && <button className={panel === "netlist" ? "on" : ""} onClick={() => setPanel("netlist")}><ScrollText size={14} /> {t("editor:panel.netlist")}</button>}
          {view === "schematic" && <button className={panel === "sim" ? "on" : ""} onClick={() => setPanel("sim")}><Play size={14} /> {t("editor:panel.simulate")}</button>}
          {view === "schematic" && <button className={panel === "check" ? "on" : ""} onClick={() => setPanel("check")}><ShieldCheck size={14} /> {t("symbolcheck:tab")}</button>}
          <button className={panel === "intent" ? "on" : ""} onClick={() => setPanel("intent")}><Target size={14} /> {t("intent:tab")}</button>
        </div>
        {panel === "props" && (
          <div className="inspector">
            {selInst ? (
              <InstanceEditor key={selInst.id} inst={selInst} detail={detail} editable={editable && moveStatus === "idle"}
                onOpenSymbol={props.onOpenSymbol}
                onSet={async (props_) => {
                  if (moving()) return;
                  const r = await edit({ op: "set_props", kind: "i", id: selInst.id, props: props_ }, true);
                  if (typeof r?.result === "string" && r.result !== selInst.id) setSel([{ k: "i", id: r.result }]); // renamed
                }} />
            ) : selWire != null ? (
              <>
                <div className="kv"><span>{t("editor:property.net")}</span><b className="mono">{netName}</b></div>
                <div className="kv"><span>{t("editor:property.wireLabel")}</span>
                  <input className="txt" disabled={!editable || moveStatus !== "idle"} defaultValue={selWireData?.[5] ?? ""} key={selWire}
                    onBlur={(e) => { if (!moving() && e.target.value !== (selWireData?.[5] ?? "")) edit({ op: "set_props", kind: "w", id: selWire, props: { lab: e.target.value } }, true); }}
                    onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} /></div>
                <div className="section">{t("editor:schematic.connectedPins")}</div>
                {netPins.length ? <div className="childlist">{netPins.map((p) => <span key={p} className="chip">{p}</span>)}</div>
                  : <div className="dim small">{t("editor:status.none")}</div>}
              </>
            ) : (
              <>
                <div className="kv"><span>{t("editor:property.cell")}</span><b>{cell}</b></div>
                <div className="kv"><span>{t("editor:property.view")}</span><b>{t(view === "schematic" ? "editor:view.schematic" : "editor:view.symbol")}</b></div>
                {data?.props.type && <div className="kv"><span>{t("editor:property.type")}</span><b>{data.props.type}</b></div>}
                <div className="kv"><span>{t("editor:property.instances")}</span><b>{data?.insts.length ?? 0}</b></div>
                {data?.ports?.length ? (
                  <>
                    <div className="section">{t("editor:property.ports")}</div>
                    <div className="childlist">{data.ports.map(([n, t]) => <span key={n} className="chip">{n} <span className="dim">{t}</span></span>)}</div>
                  </>
                ) : null}
                {data?.warnings?.length ? (
                  <>
                    <div className="section">{t("editor:schematic.warnings")}</div>
                    {data.warnings.map((w, i) => <div key={i} className="note warnnote">{w}</div>)}
                  </>
                ) : null}
                {data?.missing.length ? (
                  <>
                    <div className="section">{t("editor:schematic.missingSymbols")}</div>
                    {data.missing.map((m) => <div key={m} className="mono dim small">{m}</div>)}
                  </>
                ) : null}
              </>
            )}
          </div>
        )}
        {panel === "netlist" && <NetlistPanel project={project} lib={lib} cell={cell} tick={tick} toast={props.toast} />}
        {panel === "sim" && <SimPanel project={project} lib={lib} cell={cell} toast={props.toast} />}
        {panel === "intent" && <IntentPanel project={project} lib={lib} cell={cell} tick={tick} toast={props.toast} view={view}
          selection={sel.filter((x) => x.k === "i").map((x) => x.id)} />}
        {panel === "check" && <SymbolCheckPanel key={`${project}/${lib}/${cell}`} project={project} lib={lib} cell={cell} toast={props.toast} />}
      </aside>
    </div>
  );
}

function InstanceEditor({ inst, detail, editable, onSet, onOpenSymbol }: {
  inst: Schematic["insts"][number]; detail: InstanceDetail | null; editable: boolean;
  onSet: (p: Record<string, string | null>) => void; onOpenSymbol: (s: string) => void;
}) {
  useI18n();
  const keys = useMemo(() => {
    const ks = new Set<string>(["name"]);
    for (const k of Object.keys(detail?.props ?? inst.props)) ks.add(k);
    for (const k of Object.keys(detail?.template ?? {})) ks.add(k);
    return [...ks];
  }, [detail, inst]);
  const props = detail?.props ?? inst.props;
  return (
    <>
      <div className="kv"><span>{t("editor:view.symbol")}</span><b><button className="link" onClick={() => onOpenSymbol(inst.symbol)}>{inst.symbol}</button></b></div>
      {inst.type && <div className="kv"><span>{t("editor:property.type")}</span><b>{inst.type}</b></div>}
      <div className="section">{t("editor:property.attributes")}</div>
      {keys.map((k) => (
        <div className="kv edit" key={k}>
          <span>{k}</span>
          <input className="txt mono" key={`${k}:${props[k] ?? detail?.template[k] ?? ""}`} disabled={!editable} defaultValue={props[k] ?? detail?.template[k] ?? ""}
            onBlur={(e) => {
              const cur = props[k] ?? detail?.template[k] ?? "";
              if (e.target.value !== cur) onSet({ [k]: e.target.value });
            }}
            onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} />
        </div>
      ))}
    </>
  );
}

function SymbolPicker({ project, onPick, onClose }: { project: string; onPick: (ref: string) => void; onClose: () => void }) {
  useI18n();
  const [all, setAll] = useState<SymbolEntry[]>([]);
  const [q, setQ] = useState("");
  const [lib, setLib] = useState("");
  useEffect(() => { api.symbols(project).then(setAll).catch(() => {}); }, [project]);
  const libs = useMemo(() => [...new Set(all.map((s) => s.lib))], [all]);
  const shown = useMemo(() => all.filter((s) => (!lib || s.lib === lib) && (!q || s.name.toLowerCase().includes(q.toLowerCase()))).slice(0, 300), [all, q, lib]);
  return (
    <div className="picker" onMouseDown={(e) => e.stopPropagation()} onMouseUp={(e) => e.stopPropagation()}
      onDoubleClick={(e) => e.stopPropagation()} onKeyDown={(e) => { if (e.key === "Escape") onClose(); e.stopPropagation(); }}>
      <div className="pickhead">
        <Search size={14} className="dim" />
        <input autoFocus placeholder={t("editor:symbolPicker.search")} value={q} onChange={(e) => setQ(e.target.value)} />
        <select value={lib} onChange={(e) => setLib(e.target.value)}>
          <option value="">{t("editor:symbolPicker.allLibraries")}</option>
          {libs.map((l) => <option key={l} value={l}>{l}</option>)}
        </select>
        <button className="icon" title={t("editor:action.close")} aria-label={t("editor:action.close")} onClick={onClose}><X size={14} /></button>
      </div>
      <div className="picklist">
        {shown.map((s) => (
          <div key={s.ref} className="pickrow" onClick={() => onPick(s.ref)}>
            <span>{s.name}</span><span className="dim small">{s.lib}</span>
          </div>
        ))}
        {!shown.length && <div className="empty">{t("editor:symbolPicker.empty")}</div>}
      </div>
    </div>
  );
}
