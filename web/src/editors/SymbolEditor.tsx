import { useEffect, useRef, useState, type ReactNode } from "react";
import { ArrowDown, ArrowUp, CircuitBoard, Info, Maximize, MousePointer2, Move, Pencil, Redo2, ShieldCheck, Square, Type, Undo2, Unplug } from "lucide-react";
import { api, type SymbolAffectedView, type SymbolEditBody, type SymbolModel, type SymbolObject } from "../api";
import { apiErrorText, msg, t, useI18n, type UiText } from "../i18n";
import { Splitter } from "../layout";
import { SANS, T, rgba } from "../theme";
import { Camera, restoreView, saveView, viewKey } from "./camera";
import { drawSchematic } from "./schdraw";
import "./SymbolEditor.css";

interface Props {
  project: string;
  lib: string;
  cell: string;
  active: boolean;
  readonly: boolean;
  hasSchematic: boolean;
  onOpenSchematic: () => void;
  fitOnLoad?: boolean;
  verificationPanel?: ReactNode;
  onChanged?: (affected: SymbolAffectedView[]) => void;
  toast: (message: UiText, kind?: "error" | "info") => void;
}

type Tool = "select" | "move" | "pin" | "line" | "rectangle" | "text";
type Point = [number, number];
type Drag = { kind: "pan" | "box" | "move"; screen: Point; world: Point; moved: boolean; ids: string[]; shift: boolean };
const GRID = 10;
const snap = (n: number) => Math.round(n / GRID) * GRID;

function segmentDistance(x: number, y: number, x1: number, y1: number, x2: number, y2: number) {
  const dx = x2 - x1, dy = y2 - y1, d = dx * dx + dy * dy;
  const f = d ? Math.min(1, Math.max(0, ((x - x1) * dx + (y - y1) * dy) / d)) : 0;
  return Math.hypot(x - x1 - f * dx, y - y1 - f * dy);
}

/** Select thin graphics by their stroke; pins take priority over their body outline. */
export function hitSymbolObject(objects: SymbolObject[], x: number, y: number, tolerance: number): SymbolObject | undefined {
  const hits = objects.filter((o) => {
    const [a, b, c, d] = o.bbox;
    if (x < a - tolerance || x > c + tolerance || y < b - tolerance || y > d + tolerance) return false;
    if (o.kind === "line") return segmentDistance(x, y, o.x, o.y, o.x2!, o.y2!) <= tolerance;
    if (o.kind === "rectangle") return Math.min(Math.abs(x - a), Math.abs(x - c), Math.abs(y - b), Math.abs(y - d)) <= tolerance;
    return true;
  });
  return hits.sort((a, b) => (a.kind === "pin" ? -1 : b.kind === "pin" ? 1 : 0))[0];
}

function drawObject(ctx: CanvasRenderingContext2D, object: SymbolObject, dx = 0, dy = 0) {
  const o = object, x = o.x + dx, y = o.y + dy;
  ctx.beginPath();
  if (o.kind === "line") { ctx.moveTo(x, y); ctx.lineTo(o.x2! + dx, o.y2! + dy); }
  else if (o.kind === "rectangle") ctx.rect(x, y, o.x2! - o.x, o.y2! - o.y);
  else if (o.kind === "pin") { ctx.rect(x - 2.5, y - 2.5, 5, 5); ctx.font = `12px ${SANS()}`; ctx.fillText(o.name ?? "", x + 8, y - 5); }
  else if (o.kind === "text") { ctx.font = `${(o.size ?? .3) * 52}px ${SANS()}`; ctx.textBaseline = "top"; ctx.fillText(o.text ?? "", x, y); }
  else if (o.kind === "polygon" && o.points) {
    for (let i = 0; i < o.points.length; i += 2) (i ? ctx.lineTo : ctx.moveTo).call(ctx, o.points[i] + dx, o.points[i + 1] + dy);
  } else if (o.kind === "arc") ctx.arc(x, y, o.radius ?? 1, -(o.start ?? 0) * Math.PI / 180, -((o.start ?? 0) + (o.sweep ?? 360)) * Math.PI / 180, true);
  ctx.stroke();
}

export function SymbolEditor(props: Props) {
  useI18n();
  const { project, lib, cell, active } = props;
  const canvasRef = useRef<HTMLCanvasElement>(null), wrapRef = useRef<HTMLDivElement>(null);
  const cam = useRef(new Camera(false)).current;
  const [data, setData] = useState<SymbolModel | null>(null);
  const [tool, setToolState] = useState<Tool>("select");
  const [selection, setSelection] = useState<string[]>([]);
  const [cursor, setCursor] = useState<Point>([0, 0]);
  const [busy, setBusy] = useState(false);
  const [panel, setPanel] = useState<"props" | "check">("props");
  const [pinName, setPinName] = useState("P1"), [direction, setDirection] = useState<"in" | "out" | "inout">("in");
  const [newText, setNewText] = useState("@name");
  const st = useRef({ drag: null as Drag | null, anchor: null as Point | null, mouse: [0, 0] as Point,
    space: false, raf: 0, generation: 0, busy: false, fitted: false, pendingFit: null as SymbolModel["bbox"] | null }).current;
  const editable = !props.readonly;
  const live = useRef({ data, tool, selection, editable, pinName, direction, newText, onChanged: props.onChanged });
  live.current = { data, tool, selection, editable, pinName, direction, newText, onChanged: props.onChanged };

  function select(ids: string[]) { live.current.selection = ids; setSelection(ids); }
  function cancel() { st.drag = null; st.anchor = null; req(); }
  function choose(next: Tool) {
    if (st.busy || ((!live.current.editable || live.current.data?.exists === false) && next !== "select")) return;
    cancel(); setToolState(next); live.current.tool = next;
    if (next !== "select" && next !== "move") { select([]); setPanel("props"); }
    wrapRef.current?.focus(); req();
  }
  function size() {
    const c = canvasRef.current, r = wrapRef.current?.getBoundingClientRect();
    if (!c || !r) return;
    cam.w = Math.max(1, r.width); cam.h = Math.max(1, r.height);
    const dpr = window.devicePixelRatio || 1;
    c.width = Math.round(cam.w * dpr); c.height = Math.round(cam.h * dpr);
    c.style.width = `${cam.w}px`; c.style.height = `${cam.h}px`;
    if (st.pendingFit && cam.w > 120 && cam.h > 120) {
      const b = st.pendingFit;
      // A new or very small symbol needs room around it for placing graphics.
      const x = (b[0] + b[2]) / 2, y = (b[1] + b[3]) / 2;
      cam.fit([Math.min(b[0], x - 100), Math.min(b[1], y - 80), Math.max(b[2], x + 100), Math.max(b[3], y + 80)], .12);
      st.pendingFit = null; st.fitted = true;
    }
  }
  function fit() { if (live.current.data) { st.pendingFit = live.current.data.bbox; size(); req(); } }
  function req() {
    if (st.raf) return;
    st.raf = requestAnimationFrame(() => { st.raf = 0; draw(); });
  }
  function draw() {
    const c = canvasRef.current, d = live.current.data;
    if (!c || !d) return;
    const ctx = c.getContext("2d"); if (!ctx) return;
    const dpr = window.devicePixelRatio || 1, k = 1 / cam.s;
    ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.fillStyle = T.bg; ctx.fillRect(0, 0, c.width, c.height);
    ctx.setTransform(dpr * k, 0, 0, dpr * k, dpr * (cam.w / 2 - cam.cx * k), dpr * (cam.h / 2 - cam.cy * k));
    const [a, b, z, q] = cam.view(); let grid = GRID;
    while (grid / cam.s < 12) grid *= 2;
    ctx.fillStyle = rgba("dim", .18);
    for (let x = Math.ceil(a / grid) * grid; x <= z; x += grid)
      for (let y = Math.ceil(b / grid) * grid; y <= q; y += grid) ctx.fillRect(x, y, cam.s, cam.s);
    drawSchematic(ctx, d, cam, {});
    const drag = st.drag, moving = drag?.kind === "move" && drag.moved;
    const dx = moving ? snap(st.mouse[0]) - snap(drag.world[0]) : 0, dy = moving ? snap(st.mouse[1]) - snap(drag.world[1]) : 0;
    ctx.strokeStyle = T.sel; ctx.fillStyle = T.sel; ctx.lineWidth = 1.5 * cam.s;
    for (const o of d.objects.filter((v) => live.current.selection.includes(v.id))) {
      if (moving) { ctx.globalAlpha = .8; drawObject(ctx, o, dx, dy); ctx.globalAlpha = 1; }
      const [x1, y1, x2, y2] = o.bbox, p = 4 * cam.s;
      ctx.setLineDash([4 * cam.s, 3 * cam.s]);
      ctx.strokeRect(x1 + dx - p, y1 + dy - p, x2 - x1 + 2 * p, y2 - y1 + 2 * p); ctx.setLineDash([]);
    }
    if (drag?.kind === "box" && drag.moved) {
      ctx.strokeStyle = T.accent; ctx.fillStyle = rgba("accent", .1);
      const x = Math.min(drag.world[0], st.mouse[0]), y = Math.min(drag.world[1], st.mouse[1]);
      const w = Math.abs(drag.world[0] - st.mouse[0]), h = Math.abs(drag.world[1] - st.mouse[1]);
      ctx.fillRect(x, y, w, h); ctx.strokeRect(x, y, w, h);
    }
    const current = live.current.tool, [mx, my] = st.mouse.map(snap);
    ctx.strokeStyle = T.ok; ctx.fillStyle = T.ok; ctx.lineWidth = 1.5 * cam.s;
    if (st.anchor) {
      ctx.setLineDash([4 * cam.s, 3 * cam.s]); ctx.beginPath();
      if (current === "rectangle") ctx.rect(st.anchor[0], st.anchor[1], mx - st.anchor[0], my - st.anchor[1]);
      else { ctx.moveTo(...st.anchor); ctx.lineTo(mx, my); }
      ctx.stroke(); ctx.setLineDash([]);
    } else if (current === "pin") {
      ctx.strokeRect(mx - 2.5, my - 2.5, 5, 5); ctx.font = `12px ${SANS()}`; ctx.fillText(live.current.pinName, mx + 8, my - 5);
    } else if (current === "text") {
      ctx.font = `15.6px ${SANS()}`; ctx.textBaseline = "top"; ctx.fillText(live.current.newText, mx, my);
    }
  }

  useEffect(() => {
    const generation = ++st.generation, key = viewKey(project, lib, cell, "symbol");
    st.busy = false; setBusy(false); st.fitted = false; cancel(); select([]); setData(null); live.current.data = null;
    setToolState("select"); live.current.tool = "select";
    api.symbolView(project, lib, cell).then((d) => {
      if (generation !== st.generation) return;
      setData(d); live.current.data = d;
      if (!props.fitOnLoad && restoreView(key, cam)) st.fitted = true;
      else st.pendingFit = d.bbox;
      size(); req();
    }).catch((e) => { if (generation === st.generation) props.toast(apiErrorText(e), "error"); });
    return () => { st.generation++; if (st.fitted) saveView(key, cam); };
  }, [project, lib, cell]); // eslint-disable-line
  useEffect(() => {
    const el = wrapRef.current!, ro = new ResizeObserver(() => { size(); req(); }); ro.observe(el);
    const wheel = (e: WheelEvent) => {
      e.preventDefault(); const r = el.getBoundingClientRect();
      cam.zoomAt(e.clientX - r.left, e.clientY - r.top, Math.pow(1.0018, e.deltaY * (e.deltaMode === 1 ? 30 : 1)), .01, 100);
      st.mouse = cam.toWorld(e.clientX - r.left, e.clientY - r.top); req();
    };
    const release = (e: MouseEvent) => { if (!el.contains(e.target as Node)) cancel(); };
    const keyup = (e: KeyboardEvent) => { if (e.key === " ") st.space = false; };
    const blur = () => { st.space = false; cancel(); };
    el.addEventListener("wheel", wheel, { passive: false }); window.addEventListener("mouseup", release);
    window.addEventListener("keyup", keyup); window.addEventListener("blur", blur);
    return () => { ro.disconnect(); el.removeEventListener("wheel", wheel); window.removeEventListener("mouseup", release);
      window.removeEventListener("keyup", keyup); window.removeEventListener("blur", blur);
      if (st.raf) cancelAnimationFrame(st.raf); st.raf = 0; };
  }, []); // eslint-disable-line
  useEffect(() => {
    if (!active) { cancel(); return; }
    size(); req();
    // A symbol can be changed in another view or by an external agent while this tab is hidden.
    if (!live.current.data || st.busy || live.current.data.exists === false) return;
    const generation = ++st.generation;
    api.symbolView(project, lib, cell).then((d) => {
      if (generation !== st.generation || st.busy) return;
      live.current.data = d; setData(d); select([]); req();
    }).catch((e) => { if (generation === st.generation) props.toast(apiErrorText(e), "error"); });
  }, [active]); // eslint-disable-line
  useEffect(() => { if (!editable) { cancel(); setToolState("select"); } }, [editable]); // eslint-disable-line
  useEffect(req, [data, selection, tool, pinName, newText]); // eslint-disable-line

  async function edit(body: SymbolEditBody, keepSelection = false) {
    if (!live.current.editable || st.busy || (live.current.data?.exists === false && body.op !== "redo" && body.op !== "undo")) return null;
    const generation = ++st.generation; st.busy = true; setBusy(true);
    try {
      const result = await api.symbolEdit(project, lib, cell, body);
      if (generation !== st.generation) return null;
      live.current.data = result.view; setData(result.view);
      if (result.view.exists === false) { cancel(); setToolState("select"); live.current.tool = "select"; }
      const ids = typeof result.result === "string" ? [result.result] : Array.isArray(result.result) ? result.result.filter((id): id is string => typeof id === "string") : [];
      select(keepSelection ? ids.filter((id) => result.view.objects.some((o) => o.id === id)) : []);
      if (result.affected?.length) live.current.onChanged?.(result.affected);
      return result;
    } catch (e) { if (generation === st.generation) props.toast(apiErrorText(e), "error"); return null; }
    finally { if (generation === st.generation) { st.busy = false; setBusy(false); req(); } }
  }
  const pos = (e: React.MouseEvent): Point => {
    const r = wrapRef.current!.getBoundingClientRect(); return cam.toWorld(e.clientX - r.left, e.clientY - r.top);
  };
  function down(e: React.MouseEvent) {
    if (e.target !== canvasRef.current && e.target !== wrapRef.current) return;
    wrapRef.current?.focus(); e.preventDefault(); st.mouse = pos(e);
    if (e.button !== 0 || st.space) { st.drag = { kind: "pan", screen: [e.clientX, e.clientY], world: st.mouse, moved: false, ids: [], shift: false }; return; }
    if (st.busy || !live.current.data || !["select", "move"].includes(live.current.tool)) return;
    let ids = live.current.selection;
    if (live.current.tool === "move" && !ids.length && editable) {
      const hit = hitSymbolObject(live.current.data.objects, ...st.mouse, 7 * cam.s); ids = hit ? [hit.id] : []; select(ids);
    }
    st.drag = { kind: editable && ids.length && !e.shiftKey ? "move" : "box", screen: [e.clientX, e.clientY],
      world: st.mouse, moved: false, ids: [...ids], shift: e.shiftKey };
  }
  function move(e: React.MouseEvent) {
    st.mouse = pos(e); setCursor(st.mouse);
    const drag = st.drag;
    if (drag) {
      if (Math.hypot(e.clientX - drag.screen[0], e.clientY - drag.screen[1]) > 4) drag.moved = true;
      if (drag.kind === "pan") { cam.pan(e.clientX - drag.screen[0], e.clientY - drag.screen[1]); drag.screen = [e.clientX, e.clientY]; }
      if (drag.kind === "move" && drag.moved) { setToolState("move"); live.current.tool = "move"; }
    }
    req();
  }
  async function up(e: React.MouseEvent) {
    const drag = st.drag; st.drag = null; st.mouse = pos(e);
    if (drag?.kind === "pan" || e.button !== 0 || st.space || st.busy) { req(); return; }
    if (drag?.moved) {
      if (drag.kind === "move") {
        const dx = snap(st.mouse[0]) - snap(drag.world[0]), dy = snap(st.mouse[1]) - snap(drag.world[1]);
        if (dx || dy) await edit({ op: "symbol_move", ids: drag.ids, dx, dy }, true);
        setToolState("select"); live.current.tool = "select";
      } else if (drag.kind === "box") {
        const [x, y] = drag.world, [xx, yy] = st.mouse;
        const inside = live.current.data?.objects.filter((o) => o.bbox[0] >= Math.min(x, xx) && o.bbox[1] >= Math.min(y, yy) && o.bbox[2] <= Math.max(x, xx) && o.bbox[3] <= Math.max(y, yy)).map((o) => o.id) ?? [];
        select(drag.shift ? [...drag.ids.filter((id) => !inside.includes(id)), ...inside.filter((id) => !drag.ids.includes(id))] : inside);
      }
      req(); return;
    }
    const current = live.current.tool;
    if (current === "select" || current === "move" || !editable) {
      const hit = hitSymbolObject(live.current.data?.objects ?? [], ...st.mouse, 7 * cam.s);
      select(e.shiftKey && hit ? (live.current.selection.includes(hit.id) ? live.current.selection.filter((id) => id !== hit.id) : [...live.current.selection, hit.id]) : hit ? [hit.id] : []);
      req(); return;
    }
    const [x, y] = st.mouse.map(snap);
    if (current === "pin") {
      const name = live.current.pinName.trim();
      if (!name) { props.toast(msg("symbol:pinNameRequired"), "error"); return; }
      if (await edit({ op: "symbol_add", kind: "pin", x, y, name, direction: live.current.direction })) {
        const names = new Set(live.current.data?.objects.filter((o) => o.kind === "pin").map((o) => o.name));
        let i = 1; while (names.has(`P${i}`)) i++; setPinName(`P${i}`);
      }
    } else if (current === "text") {
      if (!live.current.newText.trim()) { props.toast(msg("symbol:textRequired"), "error"); return; }
      await edit({ op: "symbol_add", kind: "text", x, y, text: live.current.newText, size: .3 });
    } else {
      if (!st.anchor) st.anchor = [x, y];
      else if (st.anchor[0] !== x || st.anchor[1] !== y) {
        const anchor = st.anchor;
        if (current !== "rectangle" || (anchor[0] !== x && anchor[1] !== y)) {
          if (await edit({ op: "symbol_add", kind: current, x: anchor[0], y: anchor[1], x2: x, y2: y, layer: 4 })) st.anchor = null;
        }
      }
    }
    req();
  }
  function keyDown(e: React.KeyboardEvent) {
    if ((e.target as HTMLElement).closest("input,textarea,select,[contenteditable=true]")) return;
    const key = e.key.toLowerCase(), mod = e.ctrlKey || e.metaKey;
    if (e.key === " ") { st.space = true; e.preventDefault(); return; }
    if (e.key === "Escape") { cancel(); setToolState("select"); live.current.tool = "select"; e.preventDefault(); return; }
    if (key === "f" && !mod) { fit(); e.preventDefault(); return; }
    if (mod && key === "s") { props.toast(msg("symbol:savedImmediately")); e.preventDefault(); return; }
    if (!editable || st.busy) return;
    if ((mod && key === "z") || (mod && key === "y") || (!mod && key === "u")) {
      const redo = key === "y" || e.shiftKey;
      if (redo ? data?.redo : data?.undo) void edit({ op: redo ? "redo" : "undo" });
      e.preventDefault(); return;
    }
    if (e.key === "Delete" || e.key === "Backspace") {
      if (selection.length) void edit({ op: "symbol_delete", ids: [...selection] });
      e.preventDefault(); return;
    }
    if (!mod) {
      const chosen = ({ m: "move", p: "pin", l: "line", b: "rectangle", t: "text" } as const)[key as "m"];
      if (chosen) { choose(chosen); e.preventDefault(); }
    }
  }

  const pins = data?.objects.filter((o) => o.kind === "pin").sort((a, b) => (a.order ?? 0) - (b.order ?? 0)) ?? [];
  const selected = selection.length === 1 ? data?.objects.find((o) => o.id === selection[0]) : null;
  const disabled = !editable || busy || data?.exists === false;
  const update = (o: SymbolObject, fields: Partial<SymbolEditBody>) => { void edit({ op: "symbol_update", id: o.id, ...fields }, true); };
  const controls: [Tool, string, typeof Move][] = [
    ["select", t("editor:action.selectShortcut"), MousePointer2], ["move", t("editor:schematic.moveShortcut"), Move],
    ["pin", t("symbol:tool.pin"), Unplug], ["line", t("symbol:tool.line"), Pencil], ["rectangle", t("symbol:tool.rectangle"), Square], ["text", t("symbol:tool.text"), Type],
  ];
  return <div className="editor symbol-editor">
    <div className="toolrail">
      {controls.map(([name, title, Icon]) => <button key={name} className={`railbtn ${tool === name ? "on" : ""}`} title={title}
        disabled={name !== "select" && disabled} onClick={() => choose(name)}><Icon size={17} strokeWidth={1.75} /></button>)}
      <div className="railsep" />
      <button className="railbtn" title={t("editor:action.fitShortcut")} onClick={fit}><Maximize size={17} strokeWidth={1.75} /></button>
      <button className="railbtn" title={t("editor:action.redoShortcut")} disabled={!editable || busy || !data?.redo} onClick={() => void edit({ op: "redo" })}><Redo2 size={17} strokeWidth={1.75} /></button>
      <button className="railbtn" title={t("editor:action.undoShortcut")} disabled={!editable || busy || !data?.undo} onClick={() => void edit({ op: "undo" })}><Undo2 size={17} strokeWidth={1.75} /></button>
      {props.hasSchematic && <><div className="railsep" /><button className="railbtn" title={t("symbol:openSchematic")} onClick={props.onOpenSchematic}><CircuitBoard size={17} strokeWidth={1.75} /></button></>}
    </div>
    <div className="canvaswrap" ref={wrapRef} tabIndex={0} onMouseDown={down} onMouseMove={move} onMouseUp={up}
      onMouseLeave={() => { st.drag = null; req(); }} onContextMenu={(e) => e.preventDefault()} onKeyDown={keyDown}
      onKeyUp={(e) => { if (e.key === " ") st.space = false; }} style={{ cursor: tool === "select" ? "default" : tool === "move" ? "move" : "crosshair" }}>
      <canvas ref={canvasRef} />
      <div className="hud-top"><span className="hud-chip">{t("editor:view.symbol")}</span><span className="hud-hint">
        {data?.exists === false ? t("symbol:removed") : !editable ? t("symbol:help.readonly") : tool === "select" ? t("symbol:help.select") : tool === "move" ? t("symbol:help.move") : tool === "line" || tool === "rectangle" ? t("symbol:help.draw") : t("symbol:help.place")}
      </span></div>
      <div className="statusbar"><span className="mono coord">X {cursor[0].toFixed(1)}</span><span className="mono coord">Y {cursor[1].toFixed(1)}</span><span className="sep" />
        <span className="dim">{t("symbol:counts", { pins: pins.length, graphics: (data?.objects.length ?? 0) - pins.length })}</span>
        {!!selection.length && <span className="dim">{t("editor:selection.count", { count: selection.length })}</span>}
        <span className="grow" />{busy && <span role="status">{t("symbol:saving")}</span>}{props.readonly && <span className="dim mono">{t("editor:status.readOnly")}</span>}
      </div>
    </div>
    <Splitter pane="right" />
    <aside className="sidepanel">
      <div className="seg"><button className={panel === "props" ? "on" : ""} onClick={() => setPanel("props")}><Info size={14} />{t("editor:property.propertiesShort")}</button>
        {props.verificationPanel && <button className={panel === "check" ? "on" : ""} onClick={() => setPanel("check")}><ShieldCheck size={14} />{t("symbolcheck:tab")}</button>}
      </div>
      {panel === "check" ? props.verificationPanel : <div className="inspector">
        {tool === "pin" ? <><div className="section">{t("symbol:newPin")}</div><label className="kv edit"><span>{t("symbol:name")}</span><input className="txt" value={pinName} disabled={disabled} onChange={(e) => setPinName(e.target.value)} /></label>
          <label className="kv edit"><span>{t("symbol:direction")}</span><select value={direction} disabled={disabled} onChange={(e) => setDirection(e.target.value as typeof direction)}>{["in", "out", "inout"].map((v) => <option key={v} value={v}>{t(`symbol:${v as "in"}`)}</option>)}</select></label></>
          : tool === "text" ? <><div className="section">{t("symbol:newText")}</div><label className="symbol-text-field"><span>{t("symbol:text")}</span><textarea className="txt" value={newText} disabled={disabled} onChange={(e) => setNewText(e.target.value)} /></label></>
            : selected ? <SymbolProperties key={selected.id} object={selected} disabled={disabled} pinCount={pins.length} update={(fields) => update(selected, fields)} />
              : <><div className="kv"><span>{t("editor:property.cell")}</span><b>{cell}</b></div><div className="note">{t("symbol:editSelection")}</div></>}
        <div className="section">{t("symbol:pins")}</div><div className="dim small">{t("symbol:pinOrderHint")}</div>
        <div className="symbol-pin-list">{pins.map((pin, i) => <div key={pin.id} className={`symbol-pin-row ${selection.includes(pin.id) ? "selected" : ""}`}>
          <button className="symbol-pin-select" disabled={busy} onClick={() => { choose("select"); select([pin.id]); }}><span className="dim">{pin.order}</span><span className="mono">{pin.name}</span><span className="dim">{t(`symbol:${pin.direction ?? "inout"}`)}</span></button>
          <button className="icon" title={t("symbol:up")} disabled={disabled || i === 0} onClick={() => update(pin, { order: i })}><ArrowUp size={13} /></button>
          <button className="icon" title={t("symbol:down")} disabled={disabled || i === pins.length - 1} onClick={() => update(pin, { order: i + 2 })}><ArrowDown size={13} /></button>
        </div>)}</div>{!pins.length && <div className="note">{t("symbol:noPins")}</div>}
        {data?.props.type && <><div className="section">{t("symbol:attributes")}</div><div className="kv"><span>{t("editor:property.type")}</span><b>{data.props.type}</b></div></>}
        <div className="note">{t("symbol:autoSave")}</div>
      </div>}
    </aside>
  </div>;
}

function SymbolProperties({ object: o, disabled, pinCount, update }: {
  object: SymbolObject; disabled: boolean; pinCount: number; update: (fields: Partial<SymbolEditBody>) => void;
}) {
  const numeric = (field: "x" | "y" | "x2" | "y2" | "layer" | "size" | "order", label: string, min?: number, max?: number) =>
    <label key={field} className="kv edit"><span>{label}</span><input className="txt mono" type="number" aria-label={label} disabled={disabled} defaultValue={o[field]} min={min} max={max} step={field === "order" || field === "layer" ? 1 : "any"}
      onBlur={(e) => { const n = Number(e.target.value); if (e.target.value.trim() && Number.isFinite(n) && n !== o[field] && (min == null || n >= min) && (max == null || n <= max)) update({ [field]: n }); else e.target.value = String(o[field] ?? ""); }}
      onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur(); }} /></label>;
  return <><div className="section">{t(`symbol:kind.${o.kind}`)}</div>
    {o.kind === "pin" && <><label className="kv edit"><span>{t("symbol:name")}</span><input className="txt" disabled={disabled} defaultValue={o.name}
      onBlur={(e) => { const name = e.target.value.trim(); if (name && name !== o.name) update({ name }); else e.target.value = o.name ?? ""; }}
      onKeyDown={(e) => { if (e.key === "Enter") e.currentTarget.blur(); }} /></label>
      <label className="kv edit"><span>{t("symbol:direction")}</span><select value={o.direction ?? "inout"} disabled={disabled} onChange={(e) => update({ direction: e.target.value as "in" | "out" | "inout" })}>
        {["in", "out", "inout"].map((v) => <option key={v} value={v}>{t(`symbol:${v as "in"}`)}</option>)}</select></label>{numeric("order", t("symbol:order"), 1, pinCount)}</>}
    {o.kind === "text" && <label className="symbol-text-field"><span>{t("symbol:text")}</span><textarea className="txt" disabled={disabled} defaultValue={o.text}
      onBlur={(e) => { if (e.target.value !== o.text) update({ text: e.target.value }); }} /></label>}
    {numeric("x", "X")}{numeric("y", "Y")}
    {o.x2 != null && numeric("x2", "X2")}{o.y2 != null && numeric("y2", "Y2")}
    {o.kind === "text" && numeric("size", t("symbol:size"), .01)}
    {o.layer != null && ["line", "rectangle", "text"].includes(o.kind) && numeric("layer", t("symbol:layer"), 0, 21)}
  </>;
}
