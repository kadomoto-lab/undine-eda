// The palette lives in styles.css (:root). Canvas code reads it from there so there is a single source.
const cache = new Map<string, string>();

function css(name: string, fallback: string): string {
  let v = cache.get(name);
  if (v === undefined) {
    v = (typeof document !== "undefined" && getComputedStyle(document.documentElement).getPropertyValue(name).trim()) || fallback;
    if (typeof document !== "undefined" && document.readyState !== "loading") cache.set(name, v);
  }
  return v;
}

const def = (name: string, fallback: string) => ({ get: () => css(`--${name}`, fallback), enumerable: true });

export const T = Object.defineProperties({} as Record<
  "bg0" | "bg" | "bg1" | "bg2" | "bg3" | "line" | "line2" | "text" | "textSoft" | "dim" | "faint" | "teal" | "accent" | "accentHi" |
  "ok" | "warn" | "err" | "sel" | "probe" | "agent" | "apricot" | "mark", string>, {
  bg0: def("bg0", "#050e12"), bg: def("bg", "#071217"), bg1: def("bg1", "#0c1b22"), bg2: def("bg2", "#11252d"), bg3: def("bg3", "#183039"),
  line: def("line", "#1a3540"), line2: def("line2", "#2a4856"),
  text: def("text", "#e6f2f4"), textSoft: def("text-soft", "#b6cbd1"), dim: def("dim", "#93adb5"), faint: def("faint", "#5d7780"),
  teal: def("teal", "#1b4c5e"), accent: def("accent", "#72bfc6"), accentHi: def("accent-hi", "#9ad8dd"),
  ok: def("ok", "#8fd6b5"), warn: def("warn", "#e6c88f"), err: def("err", "#e9929a"), sel: def("sel", "#f2d58a"),
  probe: def("probe", "#9bdcf2"), agent: def("agent", "#b7a9ee"), apricot: def("apricot", "#f0b28a"), mark: def("mark", "#f08a97"),
});

const RGB: Record<string, string> = {
  accent: "114, 191, 198", ok: "143, 214, 181", warn: "230, 200, 143", err: "233, 146, 154", sel: "242, 213, 138",
  probe: "155, 220, 242", agent: "183, 169, 238", text: "230, 242, 244", dim: "147, 173, 181", bg1: "12, 27, 34", mark: "240, 138, 151",
};
/** `rgba("sel", 0.2)` — a palette colour with alpha (for canvas fills). */
export const rgba = (name: keyof typeof RGB, a: number) => `rgba(${RGB[name]}, ${a})`;

/** Series colours for plots, in order. */
export const SERIES = () => [T.accent, T.ok, T.err, T.warn, T.agent, T.probe, T.apricot, "#9fe3d6"];

/** The UI sans (styles.css --sans) for canvas text: `${SANS()}` after the size. */
export const SANS = () => css("--sans", "'Atkinson Hyperlegible Next', 'BIZ UDPGothic', system-ui, sans-serif");
