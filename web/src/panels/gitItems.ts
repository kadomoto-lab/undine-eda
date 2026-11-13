// What the git status says about the cells, as the library browser, the top bar and the history panel show it. The
// server reads git (server/undine/vcs/gitmerge.py); this only groups and words its answer.
import type { GitCellChange, GitConflict, GitFileStatus, GitSide, GitStatus, GitVerify, GitView, SemanticChange } from "../api";
import { i18n, joinText, msg, type UiText } from "../i18n";

/** How a change reads to a person: a merge left unfinished first, then an edit, a new file, a removed one. */
export type GitChangeKind = "conflicted" | "changed" | "new" | "deleted";
const RANK: GitChangeKind[] = ["conflicted", "changed", "new", "deleted"];
export const changeKind = (s: GitFileStatus): GitChangeKind =>
  s === "conflicted" ? "conflicted" : s === "added" || s === "untracked" ? "new" : s === "deleted" ? "deleted" : "changed";
export const cellKind = (c: GitCellChange): GitChangeKind =>
  RANK.find((k) => c.views.some((v) => changeKind(v.status) === k)) ?? "changed";

const VIEWS: GitView[] = ["schematic", "symbol", "layout", "netlist", "intent", "rtl", "other"];
export const viewName = (v: string): UiText => msg(`history:view.${VIEWS.includes(v as GitView) ? v as GitView : "other"}`);
export const statusName = (s: GitFileStatus): UiText => msg(`history:status.${s}`);

export interface CellGit { change: GitCellChange | null; unsaved: boolean }
const key = (lib: string, cell: string) => `${lib}/${cell}`;

/** lib/cell -> its uncommitted change and whether its layout has edits not saved yet. */
export function cellIndex(s: GitStatus | null): Map<string, CellGit> {
  const m = new Map<string, CellGit>();
  if (!s?.repo) return m;
  for (const c of s.cells ?? []) m.set(key(c.lib, c.cell), { change: c, unsaved: false });
  for (const u of s.unsaved ?? []) for (const cell of u.cells) {
    const k = key(u.lib, cell);
    m.set(k, { change: m.get(k)?.change ?? null, unsaved: true });
  }
  return m;
}
export const cellGit = (index: Map<string, CellGit>, lib: string, cell: string) => index.get(key(lib, cell));
/** The cells of a library with uncommitted changes (unsaved layout edits included). */
export const libraryCount = (index: Map<string, CellGit>, lib: string) =>
  [...index.keys()].filter((k) => k.slice(0, k.lastIndexOf("/")) === lib).length;

/** What a library's open layout holds that no cell shows: edits not saved that cancel out (the commit hook refuses
 * them all the same), and cell files changed on disk under unsaved edits (stale: saving is refused). */
export interface LibGit { unsaved: boolean; stale: string[] | null }
export function libraryGit(s: GitStatus | null): Map<string, LibGit> {
  const m = new Map<string, LibGit>();
  const at = (lib: string) => m.get(lib) ?? m.set(lib, { unsaved: false, stale: null }).get(lib)!;
  for (const x of s?.stale ?? []) at(x.lib).stale = x.cells;     // a layout changed on disk: in or out of git
  if (!s?.repo) return m;
  for (const u of s.unsaved ?? []) if (!u.cells.length) at(u.lib).unsaved = true;
  return m;
}

/** The marker of a group of cells (a library's device cells, folded): the first kind of change among them, unsaved
 * layout edits, and how many have either; null when none has. */
export interface GroupGit { kind: GitChangeKind | null; unsaved: boolean; count: number }
export function groupGit(index: Map<string, CellGit>, lib: string, cells: string[]): GroupGit | null {
  const gs = cells.map((c) => index.get(key(lib, c))).filter((g): g is CellGit => !!g);
  if (!gs.length) return null;
  const kinds = new Set(gs.flatMap((g) => (g.change ? [cellKind(g.change)] : [])));
  return { kind: RANK.find((k) => kinds.has(k)) ?? null, unsaved: gs.some((g) => g.unsaved), count: gs.length };
}

/** The tooltip of a cell's marker: one line per kind of change, naming the views. */
export function cellMarkText(g: CellGit): UiText {
  const lines: UiText[] = [];
  for (const k of RANK) {
    const views = (g.change?.views ?? []).filter((v) => changeKind(v.status) === k);
    if (views.length) lines.push(msg(`history:mark.${k}`, { views: joinText(views.map((v) => viewName(v.view)), ", ") }));
  }
  if (g.unsaved) lines.push(msg("history:mark.unsaved"));
  return joinText(lines);
}

/** A value of a semantic diff on one line, as `undine diff` prints it: a record as k=v, long text cut. */
export function shortValue(v: unknown, n = 60): string {
  const s = v === null || v === undefined ? "∅"
    : typeof v === "object" && !Array.isArray(v) ? Object.entries(v as Record<string, unknown>).map(([k, x]) => `${k}=${shortValue(x, n)}`).join(" ")
      : typeof v === "string" ? v : JSON.stringify(v);
  const one = s.replace(/\n/g, "\\n");
  return one.length <= n ? one : `${one.slice(0, n - 1)}…`;
}
/** Worth a "before → after": a scalar or a record of scalars (a placement), not a summary of shapes. */
export function isFlat(v: unknown): boolean {
  const scalar = (x: unknown) => x === null || ["string", "number", "boolean"].includes(typeof x);
  return scalar(v) || (typeof v === "object" && !Array.isArray(v) && Object.values(v as object).every(scalar));
}

/** A word of a shell command as `sh` reads it: a layout shape's id has spaces and parentheses. */
export const shellWord = (s: string) => (/^[\w:./@%+=,-]+$/.test(s) ? s : `'${s.replace(/'/g, "'\\''")}'`);

// ---------------------------------------------------------------- the words of a semantic diff
// The view modules word each change in English for the command line (`undine diff`, server/undine/vcs/*merge.py); the
// panel words the changes whose id it knows (the merge's identities) in the reader's language, the server's words
// stay for any other.

type Change = Pick<SemanticChange, "do" | "id" | "what" | "before" | "after">;
const FIELDS = ["rot", "flip", "xs", "ys", "symbol", "kind", "text", "strength", "rule", "spec", "serves", "reason", "source",
  "view", "role", "side"] as const;
const ITEM_KINDS = ["goal", "preference", "constraint", "spec", "context"] as const;
const field = (f: string): UiText => (FIELDS as readonly string[]).includes(f) ? msg(`history:what.field.${f as typeof FIELDS[number]}`) : f;
const record = (v: unknown): Record<string, unknown> | null =>
  v !== null && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : null;
const word = (v: unknown): string => (typeof v === "string" ? v : shortValue(v));
const viewWord = (v: string): UiText => (VIEWS.includes(v as GitView) ? viewName(v) : v);

/** A change of a semantic diff in the reader's language, from its id and values (`kind`: the diff's view); the
 * server's words when the id is of no shape known here. */
export function changeText(kind: string, c: Change): UiText {
  const words = kind === "schematic" || kind === "symbol" ? xschemText(kind, c) : kind === "layout" ? layoutText(c)
    : kind === "netlist" ? netlistText(c) : kind === "intent" ? intentText(c) : kind === "log" ? logText(c) : null;
  return words ?? c.what;
}

/** An open conflict's words: changeText's when in English they are the server's (a conflict's id can stand for more
 * than a change's, "instance M1: added on both sides with different content"), else the server's. */
export function conflictText(kind: string | null, c: GitConflict): UiText {
  const words = changeText(kind ?? "", { do: "mod", id: c.id, what: c.what, before: c.base, after: c.theirs });
  return english(words) === c.what ? words : c.what;
}

function english(v: UiText): string {
  if (typeof v === "string") return v;
  if ("parts" in v) return v.parts.map(english).join(v.separator);
  const params = Object.fromEntries(Object.entries(v.params ?? {}).map(([k, x]) => [k, typeof x === "number" ? x : english(x)]));
  return String(i18n.t(v.key, { ...params, lng: "en" }));
}

/** A schematic's or a symbol's change (schmerge.diff). Instance names may hold a colon (XM1[3:0]): the known endings
 * are matched from the end. */
function xschemText(kind: string, c: Change): UiText | null {
  const id = c.id;
  let m: RegExpExecArray | null;
  if (id === "version") return msg("history:what.version");
  if (id === "G") return msg("history:what.g");
  if (id === "ports") return msg("history:what.ports");
  if ((m = /^K:(.+)$/s.exec(id))) return msg("history:what.attribute", { name: m[1] });
  if ((m = /^code:(.+)$/s.exec(id))) return msg("history:what.codeBlock", { name: m[1] });
  if ((m = /^drawing:(lines|boxes|polys|arcs)$/.exec(id))) {
    const part = m[1] as "lines" | "boxes" | "polys" | "arcs";
    if (kind === "symbol" && part === "boxes") {        // a symbol's boxes are its pins: the server names them
      const pins = /: pins (.*) -> (.*)$/s.exec(c.what);
      return pins ? msg("history:what.pins", { before: pins[1], after: pins[2] }) : msg("history:what.pinBoxes");
    }
    return msg("history:what.drawing", { part: msg(`history:what.drawing.${part}`) });
  }
  // an unnamed instance: @symbol@x,y,rot,flip#hash; the copies of one added or removed are counted in the words only
  if ((m = /^inst:@(.*)@([^,@]+),([^,@]+),[^,@]+,[^,@]+#[0-9a-f]+$/s.exec(id))) {
    const at = { symbol: m[1], x: m[2], y: m[3] };
    if (typeof c.before === "number" && typeof c.after === "number") return msg("history:what.unnamed.count", at);
    const n = /\((\d+) copies\)$/.exec(c.what);
    return n ? msg("history:what.unnamed.copies", { ...at, n: n[1] }) : msg("history:what.unnamed", at);
  }
  if ((m = /^rename:(.+)$/s.exec(id))) return msg("history:what.instance.name", { name: m[1] });
  if ((m = /^inst:(.+?)(?::(sym|placement|embedded)|:prop:(.+))?$/s.exec(id))) {
    const name = m[1];
    if (m[3] !== undefined) return msg("history:what.instance.prop", { name, prop: m[3] });
    if (m[2] === "sym") return msg("history:what.instance.symbol", { name });
    if (m[2] === "placement") return msg("history:what.instance.placement", { name });
    if (m[2] === "embedded") return msg("history:what.instance.embedded", { name });
    return msg("history:what.instance", { name });
  }
  if ((m = /^wire:(.+?)(?::prop:(.+))?$/s.exec(id))) {
    return m[2] !== undefined ? msg("history:what.wire.prop", { id: m[1], prop: m[2] }) : msg("history:what.wire", { id: m[1] });
  }
  // a text's id is x,y,its text (which may hold anything): its fields and properties are matched from the end
  if ((m = /^text:(.+?)(?::(rot|flip|xs|ys)|:prop:([^:]+))?$/s.exec(id))) {
    if (m[2] !== undefined) return msg("history:what.text.field", { id: m[1], field: field(m[2]) });
    if (m[3] !== undefined) return msg("history:what.text.field", { id: m[1], field: m[3] });
    return msg("history:what.text", { id: m[1] });
  }
  return null;
}

/** A layout's change (layoutmerge.diff): the cell's name first, then what in it. */
function layoutText(c: Change): UiText | null {
  const id = c.id;
  let m: RegExpExecArray | null;
  if (id === "dbu") return msg("history:what.dbu");
  if ((m = /^cell:(.+)$/s.exec(id))) {
    return c.do === "mod" ? msg("history:what.cell", { cell: m[1] }) : msg(`history:what.cell.${c.do}`, { cell: m[1] });
  }
  if ((m = /^meta:([^:]+):(.+)$/s.exec(id))) return msg(`history:what.meta.${c.do}`, { cell: m[1], key: m[2] });
  if ((m = /^shape:([^:]+):(.+)$/s.exec(id))) {
    const at = { cell: m[1], layer: m[2] };
    const added = Number(record(c.after)?.added ?? 0), removed = Number(record(c.before)?.removed ?? 0);
    return added && removed ? msg("history:what.shapes.both", { ...at, added, removed })
      : added ? msg("history:what.shapes.added", { ...at, added }) : removed ? msg("history:what.shapes.removed", { ...at, removed }) : null;
  }
  if ((m = /^inst:([^:]+):(.+)$/s.exec(id))) {
    const cell = m[1], name = m[2];
    const a = record(c.before), b = record(c.after), one = b ?? a;
    const child = one?.child, placement = one?.placement;
    if (typeof child !== "string" || typeof placement !== "string") return null;
    if (a && "count" in a || b && "count" in b) {       // instances with no name of their own, counted by value
      if (c.do === "mod") return null;
      return msg(`history:what.loose.${c.do}`, { cell, n: Math.abs(Number(b?.count ?? 0) - Number(a?.count ?? 0)), child, placement });
    }
    if (c.do !== "mod") return msg(`history:what.layoutInst.${c.do}`, { cell, name, child, placement });
    if (!a || !b) return null;
    const fields: UiText[] = [];
    if (a.child !== b.child) fields.push(msg("history:what.layoutInst.child", { before: word(a.child), after: word(b.child) }));
    if (a.placement !== b.placement) fields.push(msg("history:what.layoutInst.placement", { before: word(a.placement), after: word(b.placement) }));
    const pa = record(a.props) ?? {}, pb = record(b.props) ?? {};
    for (const k of [...new Set([...Object.keys(pa), ...Object.keys(pb)])].sort()) {
      if (JSON.stringify(pa[k]) === JSON.stringify(pb[k])) continue;
      fields.push(msg("history:what.layoutInst.prop", { prop: k, before: shortValue(pa[k]), after: shortValue(pb[k]) }));
    }
    return fields.length ? msg("history:what.layoutInst.mod", { cell, name, fields: joinText(fields, "; ") }) : null;
  }
  return null;
}

/** A netlist's change (yamlmerge.diff_netlist): ports, instances and code records field by field, pending connections. */
function netlistText(c: Change): UiText | null {
  const id = c.id;
  let m: RegExpExecArray | null;
  if (id === "ports") return msg("history:what.ports");
  if ((m = /^port:(.+)$/s.exec(id))) return msg("history:what.port", { name: m[1] });
  if ((m = /^pending:(.+)$/s.exec(id))) return msg("history:what.pending", { pin: m[1] });
  if ((m = /^(inst|code):(.+?)(?::(props|pins):(.+)|:(\w+))?$/s.exec(id))) {
    const [, of, name, per, k, f] = m;
    if (of === "inst") {
      if (per === "props") return msg("history:what.instance.property", { name, prop: k });
      if (per === "pins") return msg("history:what.instance.pin", { name, pin: k });
      return f !== undefined ? msg("history:what.instance.field", { name, field: field(f) }) : msg("history:what.instance", { name });
    }
    if (per === "props") return msg("history:what.code.property", { name, prop: k });
    if (per !== undefined) return null;
    return f !== undefined ? msg("history:what.code.field", { name, field: field(f) }) : msg("history:what.code", { name });
  }
  return null;
}

/** A design intent's change (yamlmerge.diff_intent). An item's change id is its identity (its uid when it has one):
 * the id a person reads is the item's own, or for a field of it the one the server's words name. */
function intentText(c: Change): UiText | null {
  const id = c.id;
  let m: RegExpExecArray | null;
  if (id === "notes") return msg("history:what.notes");
  if ((m = /^supply:(.+)$/s.exec(id))) return msg("history:what.supply", { name: m[1] });
  if ((m = /^port:(.+?)(?::(\w+))?$/s.exec(id))) {
    return m[2] !== undefined ? msg("history:what.port.field", { name: m[1], field: field(m[2]) }) : msg("history:what.port", { name: m[1] });
  }
  if ((m = /^(?:view:([^:]+):)?item:(uid:[^:]+|[^:]+)(?::(\w+))?$/s.exec(id))) {
    const [, view, key, f] = m;
    const it = f === undefined ? record(c.do === "del" ? c.before : c.after) : null;
    const own = it && (typeof it.id === "string" || typeof it.id === "number") ? String(it.id) : null;
    const shown = own ?? (key.startsWith("uid:") ? /\bitem ([^\s:]+)/.exec(c.what)?.[1] : key);
    if (!shown) return null;
    if (it && typeof it.text === "string" && it.text) {
      const k = typeof it.kind === "string" ? it.kind : "";
      const kind = (ITEM_KINDS as readonly string[]).includes(k) ? msg(`intent:addKind.${k as typeof ITEM_KINDS[number]}`) : k;
      return view ? msg("history:what.viewItem.text", { view: viewWord(view), id: shown, kind, text: it.text })
        : msg("history:what.item.text", { id: shown, kind, text: it.text });
    }
    const item = view ? msg("history:what.viewItem", { view: viewWord(view), id: shown }) : msg("history:what.item", { id: shown });
    return f !== undefined ? msg("history:what.item.field", { item, field: field(f) }) : item;
  }
  return null;
}

/** An operation of a library's log (logmerge.diff): the record shown on the side that has it. */
function logText(c: Change): UiText | null {
  const r = record(c.do === "del" ? c.before : c.after);
  if (!/^seq:\d+$/.test(c.id) || !r || typeof r.op !== "string") return null;
  const op: UiText = r.of !== undefined && r.of !== null ? msg("history:what.op.of", { op: r.op, of: word(r.of) }) : r.op;
  const actor = typeof r.actor === "string" ? r.actor : "user";
  const text = r.cell ? (r.view ? msg("history:what.op.cell", { op, cell: word(r.cell), view: viewWord(word(r.view)), actor }) : null)
    : r.view ? msg("history:what.op.view", { op, view: viewWord(word(r.view)), actor }) : msg("history:what.op", { op, actor });
  if (!text) return null;
  return r.reason ? msg("history:what.op.reason", { text, reason: word(r.reason) }) : text;
}

// ---------------------------------------------------------------- merge-check's words

const sideWord = (s: GitSide | string): UiText => s === "ours" || s === "theirs" ? msg(`history:verify.side.${s}`)
  : /^parent \d+$/.test(s) ? msg("history:verify.side.parent", { n: s.slice("parent ".length) }) : s;

/** Why merge-check lists a cell, in the reader's language; merge-check's English when no reason known here came. */
export function verifyWhy(v: GitVerify["verify"][number]): UiText {
  const r = v.reason;
  if (r?.kind === "merged") return msg("history:verify.why.merged");
  if (r?.kind === "resolves") return msg("history:verify.why.resolves", { side: sideWord(r.side) });
  if (r?.kind === "side" && r.below?.length) {
    const below = r.below.map((b) => b.side ? msg("history:verify.why.below", { cell: `${b.lib}/${b.cell}`, side: sideWord(b.side) })
      : `${b.lib}/${b.cell}`);
    return msg("history:verify.why.side", { side: sideWord(r.side), below: joinText(below, ", ") });
  }
  return v.why;
}

/** What merge-check does not cover, in the reader's language (note_codes); its English notes for a code not known. */
export function verifyNotes(r: GitVerify): UiText[] {
  if (!r.note_codes) return r.notes;
  return r.note_codes.map((n, i) => {
    if (n.code === "no_theirs") return msg("history:verify.note.no_theirs");
    if (n.code === "octopus") return msg("history:verify.note.octopus");
    if (n.code === "earlier") {
      const op = n.operation === "rebase" || n.operation === "cherry-pick" ? msg(`history:verify.op.${n.operation}`) : n.operation ?? "";
      return msg("history:verify.note.earlier", { operation: op });
    }
    return r.notes[i] ?? n.code;
  });
}

/** "3 minutes ago" in the reader's language. */
export function relativeDate(iso: string, locale: string, now = Date.now()): string {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return iso;
  const f = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  const steps: [Intl.RelativeTimeFormatUnit, number][] = [["second", 60], ["minute", 60], ["hour", 24], ["day", 30], ["month", 12]];
  let v = Math.min(0, Math.round((t - now) / 1000));     // a clock ahead of this one: "now", not "in 2 minutes"
  for (const [unit, size] of steps) {
    if (Math.abs(v) < size) return f.format(v, unit);
    v = Math.round(v / size);
  }
  return f.format(v, "year");
}
