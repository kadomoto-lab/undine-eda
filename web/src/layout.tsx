// Window layout helpers: resizable panes (splitters) and the scrolling tab strip.
import { ReactNode, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useI18n } from "./i18n";

// ----------------------------------------------------------------- resizable panes

// Pane widths are CSS variables on :root, so every editor's side panel follows one setting; remembered per browser.
const PANES = {
  left: { cssVar: "--left-w", def: 340, min: 200, max: 900, grows: "right" },     // assistant chat + library browser
  right: { cssVar: "--right-w", def: 280, min: 220, max: 760, grows: "left" },    // editor side panels
  drawer: { cssVar: "--drawer-w", def: 460, min: 320, max: 1200, grows: "left" }, // Assistant / Changes
} as const;
export type Pane = keyof typeof PANES;

const key = (p: Pane) => `undine.pane.${p}`;
const MIN_EDITOR = 320; // px the canvas keeps however wide the panes get

function width(p: Pane): number {
  const v = parseFloat(getComputedStyle(document.documentElement).getPropertyValue(PANES[p].cssVar));
  return Number.isFinite(v) ? v : PANES[p].def;
}

function setWidth(p: Pane, w: number | null) {
  const root = document.documentElement.style;
  if (w == null) root.removeProperty(PANES[p].cssVar);
  else root.setProperty(PANES[p].cssVar, `${Math.round(w)}px`);
}

/** Restores the remembered pane widths (call once at start-up). */
export function initPaneWidths() {
  try {
    const h = localStorage.getItem(CHAT_H.key);
    if (h) document.documentElement.style.setProperty(CHAT_H.cssVar, h);
  } catch {
    /* storage unavailable: default */
  }
  for (const p of Object.keys(PANES) as Pane[]) {
    try {
      const v = parseFloat(localStorage.getItem(key(p)) ?? "");
      if (Number.isFinite(v)) setWidth(p, v);
    } catch {
      /* storage unavailable: defaults */
    }
  }
}

/** A vertical handle between two panes: drag to resize, double-click to reset. */
export function Splitter({ pane, className = "" }: { pane: Pane; className?: string }) {
  const { t } = useI18n();
  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const conf = PANES[pane];
    const x0 = e.clientX, w0 = width(pane);
    const el = e.currentTarget;
    el.setPointerCapture(e.pointerId);
    document.body.classList.add("resizing");
    const others = pane === "left" ? width("right") : pane === "right" ? width("left") : 0;
    const move = (ev: PointerEvent) => {
      const dx = ev.clientX - x0;
      const w = conf.grows === "right" ? w0 + dx : w0 - dx;
      const max = Math.min(conf.max, window.innerWidth - others - MIN_EDITOR);
      setWidth(pane, Math.max(conf.min, Math.min(max, w)));
    };
    const up = () => {
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", up);
      el.removeEventListener("pointercancel", up);
      document.body.classList.remove("resizing");
      try { localStorage.setItem(key(pane), String(width(pane))); } catch { /* not remembered */ }
    };
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", up);
  };
  const reset = () => {
    setWidth(pane, null);
    try { localStorage.removeItem(key(pane)); } catch { /* nothing stored */ }
  };
  return (
    <div className={`splitter ${className}`} role="separator" aria-orientation="vertical"
      title={t("common:layout.resize")} onPointerDown={onPointerDown} onDoubleClick={reset} />
  );
}

const CHAT_H = { cssVar: "--chat-h", key: "undine.pane.chat", min: 140, keepBelow: 140 };

/** The handle between the assistant chat (above) and the library browser (below): drag to share the column. */
export function RowSplitter() {
  const { t } = useI18n();
  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return;
    e.preventDefault();
    const el = e.currentTarget;
    const col = el.parentElement!;
    const top = col.getBoundingClientRect().top;
    const colH = col.getBoundingClientRect().height;
    el.setPointerCapture(e.pointerId);
    document.body.classList.add("resizing-rows");
    const move = (ev: PointerEvent) => {
      const h = Math.max(CHAT_H.min, Math.min(colH - CHAT_H.keepBelow, ev.clientY - top));
      document.documentElement.style.setProperty(CHAT_H.cssVar, `${Math.round(h)}px`);
    };
    const up = () => {
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", up);
      el.removeEventListener("pointercancel", up);
      document.body.classList.remove("resizing-rows");
      try { localStorage.setItem(CHAT_H.key, document.documentElement.style.getPropertyValue(CHAT_H.cssVar)); } catch { /* not remembered */ }
    };
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", up);
  };
  const reset = () => {
    document.documentElement.style.removeProperty(CHAT_H.cssVar);
    try { localStorage.removeItem(CHAT_H.key); } catch { /* nothing stored */ }
  };
  return <div className="rowsplitter" role="separator" aria-orientation="horizontal" title={t("common:layout.resize")}
    onPointerDown={onPointerDown} onDoubleClick={reset} />;
}

// ----------------------------------------------------------------- tab strip

/** Horizontally scrolling tabs. When they overflow, a thin scrollbar and "‹ n" / "n ›" buttons show how many
 *  tabs are hidden on each side; the wheel scrolls sideways and the active tab is kept in view. */
export function TabStrip({ children, activeId, count }: { children: ReactNode; activeId: string | null; count: number }) {
  const { t } = useI18n();
  const ref = useRef<HTMLDivElement>(null);
  const [hidden, setHidden] = useState({ left: 0, right: 0 });

  const update = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    let left = 0, right = 0;
    for (const c of Array.from(el.children) as HTMLElement[]) {
      const b = c.getBoundingClientRect();
      const visible = Math.min(b.right, r.right) - Math.max(b.left, r.left);
      if (visible >= b.width / 2) continue; // mostly visible counts as shown
      if (b.left + b.width / 2 < r.left + r.width / 2) left++;
      else right++;
    }
    setHidden((h) => (h.left === left && h.right === right ? h : { left, right }));
  }, []);

  useLayoutEffect(update, [count, update]);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(update);
    ro.observe(el);
    const wheel = (e: WheelEvent) => {
      if (el.scrollWidth <= el.clientWidth || Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;
      e.preventDefault();
      el.scrollLeft += e.deltaY;
    };
    el.addEventListener("wheel", wheel, { passive: false });
    return () => { ro.disconnect(); el.removeEventListener("wheel", wheel); };
  }, [update]);

  useEffect(() => {  // the active tab is always in view
    const on = ref.current?.querySelector<HTMLElement>(".tab.on");
    on?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [activeId, count]);

  const page = (dir: -1 | 1) => {
    const el = ref.current;
    if (el) el.scrollBy({ left: dir * el.clientWidth * 0.8, behavior: "smooth" });
  };

  return (
    <div className={`tabstrip ${hidden.left ? "more-left" : ""} ${hidden.right ? "more-right" : ""}`}>
      {hidden.left > 0 && (
        <button className="tabmore left" title={t("common:layout.scrollLeft")} onClick={() => page(-1)}>
          <ChevronLeft size={13} />{hidden.left}
        </button>
      )}
      <div className="tabbar" ref={ref} onScroll={update}>{children}</div>
      {hidden.right > 0 && (
        <button className="tabmore right" title={t("common:layout.scrollRight")} onClick={() => page(1)}>
          {hidden.right}<ChevronRight size={13} />
        </button>
      )}
    </div>
  );
}
