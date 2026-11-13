import { useEffect, useRef, useState } from "react";
import { rgba, SERIES, T } from "../theme";
import { useI18n, t } from "../i18n";

export interface Series { name: string; y: number[]; color: string }


export const seriesColor = (i: number) => SERIES()[i % 8];

export function si(v: number, digits = 3): string {
  if (v === 0 || !isFinite(v)) return String(v);
  const a = Math.abs(v);
  const units: [number, string][] = [[1e9, "G"], [1e6, "M"], [1e3, "k"], [1, ""], [1e-3, "m"], [1e-6, "µ"], [1e-9, "n"], [1e-12, "p"], [1e-15, "f"]];
  for (const [m, s] of units) if (a >= m * 0.9999) return `${Number((v / m).toPrecision(digits))}${s}`;
  return v.toExponential(2);
}

function niceTicks(lo: number, hi: number, n: number): number[] {
  const span = hi - lo || Math.abs(hi) || 1;
  const raw = span / n;
  const e = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 5, 10].map((m) => m * e).find((s) => s >= raw) ?? e * 10;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Number(v.toPrecision(12)));
  return out;
}

export function WaveformChart({ x, series, xlog, xlabel, ylabel, height = 190 }: {
  x: number[]; series: Series[]; xlog?: boolean; xlabel: string; ylabel: string; height?: number;
}) {
  useI18n();
  const ref = useRef<HTMLCanvasElement>(null);
  const wrap = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const [view, setView] = useState<[number, number] | null>(null); // zoomed x range
  const [size, setSize] = useState(300);

  useEffect(() => {
    const ro = new ResizeObserver(() => setSize(wrap.current?.clientWidth ?? 300));
    if (wrap.current) ro.observe(wrap.current);
    return () => ro.disconnect();
  }, []);
  useEffect(() => setView(null), [x]);

  const L = 52, R = 8, TOP = 8, B = 22;
  const xs = (v: number) => (xlog ? Math.log10(Math.max(v, 1e-300)) : v);

  const geom = () => {
    const x0 = view ? view[0] : x[0], x1 = view ? view[1] : x[x.length - 1];
    let lo = Infinity, hi = -Infinity;
    for (const s of series) for (let i = 0; i < x.length; i++) {
      if (x[i] < x0 || x[i] > x1) continue;
      const v = s.y[i];
      if (isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
    }
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    if (lo === hi) { lo -= 0.5; hi += 0.5; }
    const pad = (hi - lo) * 0.06;
    return { x0, x1, y0: lo - pad, y1: hi + pad };
  };

  useEffect(() => {
    const c = ref.current;
    if (!c || !x.length) return;
    const dpr = window.devicePixelRatio || 1;
    const W = size, H = height;
    c.width = W * dpr; c.height = H * dpr;
    c.style.width = `${W}px`; c.style.height = `${H}px`;
    const ctx = c.getContext("2d")!;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const g = geom();
    const px = (v: number) => L + ((xs(v) - xs(g.x0)) / (xs(g.x1) - xs(g.x0) || 1)) * (W - L - R);
    const py = (v: number) => TOP + (1 - (v - g.y0) / (g.y1 - g.y0)) * (H - TOP - B);
    ctx.font = "10px 'JetBrains Mono', monospace";
    ctx.strokeStyle = T.line; ctx.fillStyle = T.dim; ctx.lineWidth = 1;
    ctx.textBaseline = "middle"; ctx.textAlign = "right";
    for (const v of niceTicks(g.y0, g.y1, 4)) {
      const y = Math.round(py(v)) + 0.5;
      ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(W - R, y); ctx.stroke();
      ctx.fillText(si(v), L - 5, y);
    }
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    const xt = xlog
      ? Array.from({ length: Math.ceil(xs(g.x1)) - Math.floor(xs(g.x0)) + 1 }, (_, i) => Math.pow(10, Math.floor(xs(g.x0)) + i)).filter((v) => v >= g.x0 * 0.999 && v <= g.x1 * 1.001)
      : niceTicks(g.x0, g.x1, Math.max(2, Math.floor((W - L - R) / 80)));
    for (const v of xt) {
      const xx = Math.round(px(v)) + 0.5;
      ctx.beginPath(); ctx.moveTo(xx, TOP); ctx.lineTo(xx, H - B); ctx.stroke();
      ctx.fillText(si(v), xx, H - B + 4);
    }
    ctx.strokeStyle = T.line2;
    ctx.strokeRect(L + 0.5, TOP + 0.5, W - L - R, H - TOP - B);
    ctx.save();
    ctx.beginPath(); ctx.rect(L, TOP, W - L - R, H - TOP - B); ctx.clip();
    for (const s of series) {
      ctx.strokeStyle = s.color; ctx.lineWidth = 1.5; ctx.beginPath();
      let first = true;
      for (let i = 0; i < x.length; i++) {
        if (!isFinite(s.y[i])) continue;
        const X = px(x[i]), Y = py(s.y[i]);
        if (first) { ctx.moveTo(X, Y); first = false; } else ctx.lineTo(X, Y);
      }
      ctx.stroke();
    }
    ctx.restore();
    if (hover != null) {
      const X = px(x[hover]);
      ctx.strokeStyle = rgba("text", 0.5);
      ctx.beginPath(); ctx.moveTo(X + 0.5, TOP); ctx.lineTo(X + 0.5, H - B); ctx.stroke();
      for (const s of series) {
        ctx.fillStyle = s.color;
        ctx.beginPath(); ctx.arc(X, py(s.y[hover]), 3, 0, 6.3); ctx.fill();
      }
    }
    ctx.fillStyle = T.faint; ctx.textAlign = "right"; ctx.textBaseline = "bottom";
    ctx.fillText(xlabel, W - R, H - 1);
    ctx.save(); ctx.translate(10, TOP + 4); ctx.rotate(-Math.PI / 2); ctx.textAlign = "right"; ctx.textBaseline = "top";
    ctx.fillText(ylabel, 0, 0); ctx.restore();
  });

  const idxAt = (clientX: number): number => {
    const r = ref.current!.getBoundingClientRect();
    const g = geom();
    const f = (clientX - r.left - L) / (size - L - R);
    const target = xlog ? Math.pow(10, xs(g.x0) + f * (xs(g.x1) - xs(g.x0))) : g.x0 + f * (g.x1 - g.x0);
    let lo = 0, hi = x.length - 1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (x[m] < target) lo = m; else hi = m; }
    return Math.abs(x[lo] - target) < Math.abs(x[hi] - target) ? lo : hi;
  };

  return (
    <div ref={wrap} className="chart" style={{ position: "relative" }}>
      <canvas ref={ref}
        role="img" aria-label={t("editor:waveform.description", { x: xlabel, y: ylabel })}
        title={t("editor:waveform.description", { x: xlabel, y: ylabel })}
        onMouseMove={(e) => setHover(idxAt(e.clientX))}
        onMouseLeave={() => setHover(null)}
        onWheel={(e) => {
          const g = geom();
          const i = idxAt(e.clientX);
          const f = e.deltaY > 0 ? 1.3 : 1 / 1.3;
          const c = x[i];
          setView([c - (c - g.x0) * f, c + (g.x1 - c) * f]);
        }}
        onDoubleClick={() => setView(null)} />
      {hover != null && (
        <div className="chart-read mono">
          {xlabel} = {si(x[hover], 4)}
          {series.map((s) => <div key={s.name} style={{ color: s.color }}>{s.name} = {si(s.y[hover], 4)}</div>)}
        </div>
      )}
    </div>
  );
}
