// A changeset's operation log grouped by what was changed: one item per design object (an instance, a wire, a
// shape ...), with before / after values per field and the reasons given. The raw operations stay available.
import type { ChangeOp, ChangesetInfo, CheckRow, Effect, ViewKind } from "../api";
import { msg, type UiText } from "../i18n";

export interface FieldChange { name: string; before?: unknown; after: unknown }   // no before: the object is new in the changeset

export interface ChangeItem {
  key: string;
  lib: string;
  cell: string;
  view: ViewKind;
  kind: "add" | "del" | "mod";
  label: UiText;            // raw object name or deferred UI label; grouping never depends on the UI language
  objectId?: string;        // instance id: what the schematic / layout cross-probe can highlight
  fields: FieldChange[];    // net before / after per field (a field changed twice shows first before, last after)
  reasons: { text: string; actor: string }[];
  ops: number[];            // seq of the operations behind this item
  effects: Effect[];
  causedBy: string[];       // keys of the items whose change made this one necessary (layout follows schematic)
  followedBy: string[];     // keys of the items that follow from this one
}

export function shapeLabel(shape?: string): UiText {
  const labels = { box: msg("review:shape.box"), path: msg("review:shape.path"), polygon: msg("review:shape.polygon"), text: msg("review:shape.text") };
  return shape && Object.hasOwn(labels, shape) ? labels[shape as keyof typeof labels] : shape ?? msg("review:shape.shape");
}

/** The tab that shows a changed item: its view, or the cell page for what has no view tab (an intent entry, a flow
 * configuration). */
export const tabViewOf = (view: string): ViewKind | "cell" =>
  view === "schematic" || view === "symbol" || view === "layout" || view === "rtl" ? view : "cell";

export const viewLabel = (view: string): UiText =>
  view === "schematic" || view === "symbol" || view === "layout" || view === "intent" || view === "flow" ? msg(`review:view.${view}`) : view;

/** A flow configuration's text change (librelane.py FlowEditor): "" before is a new file, "" after a deleted one. */
function flowMark(e: Effect): string {
  const [a, b] = (e.changes?.text ?? ["", ""]) as [string, string];
  return !a ? "+" : !b ? "−" : "~";
}

/** New results carry structured summaries; old server/archive diagnostics remain verbatim. */
export function checkResultLabel(row: Pick<CheckRow, "summary" | "detail">): UiText {
  if (typeof row.summary?.total === "number") return msg("review:check.violations", { count: row.summary.total });
  if (typeof row.summary?.match === "boolean") return msg(row.summary.match ? "review:check.match" : "review:check.mismatch");
  if (typeof row.summary?.warnings === "number") return msg("review:check.warnings", { count: row.summary.warnings });
  return row.detail ?? msg("review:check.completed");
}

export const formatChangeValue = (v: unknown): string => v === null || v === undefined || v === "" ? "∅"
  : typeof v === "object" ? JSON.stringify(v) : String(v);
const short = formatChangeValue;

/** A readable effect, kept separate from the recorded operation and resolved only for display. */
export function describeEffect(e: Effect): UiText {
  const mark = e.do === "add" ? "+" : "−";
  const changes = () => Object.entries(e.changes ?? {}).map(([k, [a, b]]) => `${k.replace("props.", "")}: ${short(a)} → ${short(b)}`).join(", ");
  if (e.k === "i" && e.do === "mod") return `${e.id}${e.id_after && e.id_after !== e.id ? ` → ${e.id_after}` : ""}  ${changes()}`;
  if (e.k === "i" && e.cell === undefined) return msg("review:effect.instance", { mark, id: e.id ?? "", symbol: (e.obj as { sym?: string })?.sym ?? "" });
  if (e.k === "w") return e.do === "mod" ? msg("review:effect.wireChanged", { id: e.id ?? "", changes: changes() }) : msg("review:effect.wire", { mark, id: e.id ?? "" });
  if (e.k === "t") return msg("review:effect.text", { mark, id: e.id ?? "" });
  if (e.k === "s") return msg("review:effect.shape", { mark, shape: shapeLabel(e.t), layer: e.layer ?? "", coordinates: e.s ?? "" });
  if (e.k === "i") return msg("review:effect.placed", { mark, cell: e.inst ? msg("review:effect.namedInstance", { cell: e.child ?? "", instance: e.inst }) : e.child ?? "", position: e.trans ?? "" });
  if (e.k === "v") return msg(e.do === "del" ? "review:effect.viewRemoved" : "review:effect.view");
  if (e.k === "g") return msg("symbolcheck:drawingChanged", { kind: e.id ?? "" });
  if (e.k === "n") return msg("review:effect.intent", { mark: e.do === "mod" ? "~" : mark, entry: e.id ?? "" });
  if (e.k === "l") return msg("review:effect.library", { mark, lib: e.lib ?? "" });
  if (e.k === "r" && e.id?.startsWith("flow:")) return msg("review:effect.flow", { mark: flowMark(e), file: e.file ?? e.id.slice(5) });
  if (e.k === "o") {
    const [before, after] = (e.changes?.ports ?? [[], []]) as [string[], string[]];
    return msg("review:effect.portOrder", { before: before.join(", "), after: after.join(", ") });
  }
  return msg("review:effect.cell", { mark, cell: e.cell ?? "" });
}

const tag = (e: Effect): string =>
  e.id ? `${e.k}|${e.id_after ?? e.id}` : e.inst ? `${e.k}|${e.inst}` : `${e.k}|${e.layer ?? ""}|${e.s ?? e.child ?? e.cell ?? e.lib ?? ""}`;

function label(e: Effect, view: ViewKind, cell: string): UiText {
  if (e.k === "g") return msg("symbolcheck:drawingChanged", { kind: e.id ?? "" });
  if (e.k === "n") return msg("review:item.intent", { entry: e.id ?? "" });
  if (e.k === "v") return msg("review:item.view", { view: viewLabel(view), cell });
  if (e.k === "s") return msg("review:item.shape", { shape: shapeLabel(e.t), layer: e.layer ?? "?" });
  if (e.k === "i" && !e.id && e.child) return e.inst ?? e.child;
  if (e.k === "c") return msg("review:item.cell", { cell: e.cell ?? "" });
  if (e.k === "l") return msg("review:item.library", { lib: e.lib ?? "" });
  if (e.k === "o") return msg("review:item.portOrder");
  if (e.k === "r" && e.id?.startsWith("flow:")) return msg("review:item.flow", { name: e.id.slice(5) });
  const base = e.id_after ?? e.id ?? "";
  return e.k === "i" ? base : msg(e.k === "w" ? "review:item.wire" : "review:item.text", { id: base });
}

/** Only instances can be cross-probed; the id is the schematic instance name / layout instance name. */
function probeId(e: Effect): string | undefined {
  if (e.k === "i" && e.id) return e.id_after ?? e.id;
  if (e.k === "i" && e.inst) return e.inst;
  return undefined;
}

/** A port reordering: the ports by name; the instance ids that make the change apply are no reader's concern. */
function portFields(e: Effect): Record<string, [unknown, unknown]> | null {
  if (e.k !== "o" || !e.changes?.ports) return null;
  const [a, b] = e.changes.ports as [string[], string[]];
  return { ports: [a.join(", "), b.join(", ")] };
}

/** An intent entry that changed: its fields side by side (text: a → b), rather than the whole entry twice. */
function intentFields(e: Effect): Record<string, [unknown, unknown]> | null {
  if (e.k !== "n" || e.do !== "mod" || !e.changes?.value) return null;
  const [a, b] = e.changes.value as [unknown, unknown];
  if (!a || !b || typeof a !== "object" || typeof b !== "object") return { value: [a, b] };
  const before = a as Record<string, unknown>, after = b as Record<string, unknown>;
  const out: Record<string, [unknown, unknown]> = {};
  for (const k of new Set([...Object.keys(before), ...Object.keys(after)])) {
    if (JSON.stringify(before[k]) !== JSON.stringify(after[k])) out[k] = [before[k], after[k]];
  }
  return out;
}

/** The layout cells an operation takes out or brings back with their content (kept: delete_unused_cells, a restore
 * of it): their shapes and instances go with the cell's own item, not one item each. */
function keptCells(op: ChangeOp): Set<string> {
  const out = new Set<string>();
  for (const e of op.effects) if (e.k === "c" && e.kept && e.cell) out.add(e.cell);
  return out;
}

export function groupChanges(cs: Pick<ChangesetInfo, "changes">): ChangeItem[] {
  const items = new Map<string, ChangeItem>();
  const gone = new Map<string, boolean>();
  const born = new Set<string>();                // layout cells made in the changeset (their first effect an add)
  const causes = new Map<string, Set<number>>(); // item key -> schematic op seqs it follows from
  for (const lc of cs.changes ?? []) {
    for (const op of lc.ops as ChangeOp[]) {
      const view = (op.view || "layout") as ViewKind;
      const kept = view === "layout" ? keptCells(op) : new Set<string>();
      const held = new Map<string, Effect[]>();  // a kept cell's shapes / instances, for its item (unless one of
      for (const e of op.effects) {              // them is an item already: an object the changeset drew in it)
        if (e.k !== "c" && e.cell && kept.has(e.cell) && !items.has(`${lc.lib}|${e.cell}|${view}|${tag(e)}`)) {
          held.set(e.cell, [...(held.get(e.cell) ?? []), e]);
        }
      }
      const folded = new Set([...held.values()].flat());
      for (const e of op.effects) {
        if (folded.has(e)) continue;
        const cell = (view === "layout" && e.k === "c" ? e.cell : undefined) ?? op.cell ?? "";
        const key = `${lc.lib}|${cell}|${view}|${tag(e)}`;
        let it = items.get(key);
        if (!it) {
          it = { key, lib: lc.lib, cell, view, kind: e.do, label: label(e, view, cell), objectId: probeId(e), fields: [], reasons: [], ops: [], effects: [], causedBy: [], followedBy: [] };
          items.set(key, it);
          if (e.k === "c" && e.do === "add") born.add(key);
        } else if (e.k === "c" && e.do === "add" && e.kept && it.kind === "del") {
          gone.set(key, !born.has(key));        // brought back as it was: as in the baseline, or new again
          it.kind = born.has(key) ? "add" : "del";
        } else if (e.do === "del") {
          gone.set(key, it.kind === "add"); // added and removed again within the changeset: nothing left to review
          it.kind = "del";
        } else if (e.do === "add" && it.kind === "del") {
          gone.delete(key);
          it.kind = "mod"; // removed and put back
        }
        it.effects.push(e, ...(e.k === "c" && e.cell ? held.get(e.cell) ?? [] : []));
        const cs = e.inst ? op.causes?.[e.inst] : undefined;
        if (cs) causes.set(key, new Set([...(causes.get(key) ?? []), ...cs]));
        if (!it.ops.includes(op.seq)) it.ops.push(op.seq);
        if (op.reason && !it.reasons.some((r) => r.text === op.reason && r.actor === op.actor)) it.reasons.push({ text: op.reason, actor: op.actor });
        for (const [raw, [before, after]] of Object.entries(portFields(e) ?? intentFields(e) ?? e.changes ?? {})) {
          const name = raw.replace(/^props\./, "");
          const f = it.fields.find((x) => x.name === name);
          if (f) f.after = after; else it.fields.push({ name, before, after });
        }
      }
    }
  }
  // a field that ended where it began is no change; an item that is only that (and not added / removed) disappears
  for (const it of items.values()) it.fields = it.fields.filter((f) => short(f.before) !== short(f.after));
  const out = [...items.values()].filter((it) => !gone.get(it.key)).filter((it) => it.kind !== "mod" || it.fields.length > 0 || it.effects.some((e) => !e.changes));
  // an object added in the changeset has no confirmed value to compare with: only its final values are shown
  for (const it of out) if (it.kind === "add") it.fields = it.fields.map((f) => ({ name: f.name, after: f.after }));
  // layout items that follow a schematic change hang under it (same library and cell, matched by operation seq)
  for (const it of out) {
    const seqs = causes.get(it.key);
    if (!seqs) continue;
    for (const parent of out) {
      if (parent.view !== "schematic" || parent.lib !== it.lib || parent.cell !== it.cell) continue;
      if (parent.ops.some((s) => seqs.has(s))) { it.causedBy.push(parent.key); parent.followedBy.push(it.key); }
    }
  }
  return out;
}

/** What a layout device change did, for display: "nfet_w1.0 → nfet_w2.0" for a replaced PCell. */
export function followText(it: ChangeItem): UiText {
  const gone = it.effects.find((e) => e.do === "del" && e.child);
  const made = [...it.effects].reverse().find((e) => e.do === "add" && e.child);
  if (gone && made) return gone.child === made.child ? msg("review:follow.replaced") : `${gone.child} → ${made.child}`;
  if (made) return msg("review:follow.placed", { cell: made.child ?? "" });
  return gone ? msg("review:follow.removed", { cell: gone.child ?? "" }) : msg("review:follow.changed");
}
