// A changeset's timeline: its operations in order, grouped by the request (assistant turn) they belong to, with what is
// no longer in effect struck through, and "Restore to here" between them (docs/design/live-changesets.md).
import { useEffect, useState } from "react";
import { Bot, History, RotateCcw, Undo2, User } from "lucide-react";
import { api, Timeline, TimelineOp } from "../api";
import { confirmDialog } from "../dialog";
import { describeEffect } from "./changeItems";
import { apiErrorText, msg, resolveText, t, useI18n, type UiText } from "../i18n";

interface Props {
  project: string;
  changeset: string;
  version: unknown;              // changes whenever the changeset was reloaded: read the timeline again
  locked: boolean;               // someone is writing: no restore
  onRestored: () => void;
  toast: (m: UiText, k?: "error" | "info") => void;
}

const who = (actor: string) => (actor.startsWith("agent:") ? actor.slice(6) : actor === "user" ? t("review:timeline.you") : actor);
const turnState = (state: string) => {
  const states: Record<string, UiText> = { running: msg("review:state.running"), stopping: msg("review:state.stopping"), done: msg("review:state.done"),
    failed: msg("review:state.failed"), stopped: msg("review:state.stopped"), interrupted: msg("review:state.interrupted") };
  return resolveText(Object.hasOwn(states, state) ? states[state] : state);
};

function Row({ o }: { o: TimelineOp }) {
  useI18n();
  if (o.kind === "restore") return <div className="tlop tl-restore"><History size={12} /> {t("review:timeline.restored", { checkpoint: o.to ?? "" })} <span className="dim">— {who(o.actor)}</span></div>;
  if (o.kind === "undo" || o.kind === "redo") return (
    <div className="tlop tl-undo"><Undo2 size={12} /> {t(o.kind === "undo" ? "review:timeline.undo" : "review:timeline.redo")}: {o.of?.cell ? t("review:timeline.onCell", { operation: o.of.op ?? t("review:timeline.operation"), cell: o.of.cell }) : o.of?.op ?? t("review:timeline.operation")} <span className="dim">— {who(o.actor)}</span></div>
  );
  return (
    <div className={`tlop ${o.in_effect ? "" : "gone"}`} title={o.in_effect ? "" : t("review:timeline.inactive")}>
      <div className="row"><span className="mono">{o.ops.join(", ")}</span><span className="dim small grow ellip">{o.cells.join(", ")}</span></div>
      {o.effect_data ? o.effect_data.slice(0, 3).map((e, i) => <div key={i} className="fx small">{resolveText(describeEffect(e.effect))}</div>)
        : o.effects.slice(0, 3).map((e, i) => <div key={i} className="fx small">{e}</div>)}
      {o.reasons.slice(0, 2).map((r, i) => <div key={i} className="opreason">“{r}”</div>)}
    </div>
  );
}

export function TimelineView({ project, changeset, version, locked, onRestored, toast }: Props) {
  useI18n();
  const [tl, setTl] = useState<Timeline | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { api.timeline(project, changeset).then(setTl).catch((e) => toast(apiErrorText(e), "error")); }, [project, changeset, version, toast]);

  const restore = async (to: number) => {
    if (!(await confirmDialog({ title: msg("review:timeline.restoreTitle"), okLabel: msg("review:timeline.restore"), danger: true,
      message: msg(to === 0 ? "review:timeline.restoreStart" : "review:timeline.restoreStep") }))) return;
    setBusy(true);
    try { await api.restore(project, changeset, to); onRestored(); }
    catch (e) { toast(apiErrorText(e), "error"); } finally { setBusy(false); }
  };

  if (!tl) return <div className="dim small">…</div>;
  if (!tl.ops.length) return <div className="empty" style={{ padding: 0 }}>{t("review:timeline.empty")}</div>;
  const can = new Set(tl.checkpoints);
  // consecutive operations of one request (or of one person) form a group
  const groups: { key: string; turn: string | null; actor: string; ops: TimelineOp[] }[] = [];
  for (const o of tl.ops) {
    const g = groups[groups.length - 1];
    if (g && g.turn === o.turn && (o.turn || g.actor === o.actor)) g.ops.push(o);
    else groups.push({ key: o.opid, turn: o.turn, actor: o.actor, ops: [o] });
  }
  const here = (to: number) => can.has(to) && (
    <button className="tlhere" disabled={busy || locked} title={locked ? t("review:timeline.wait") : ""} onClick={() => restore(to)}>
      <RotateCcw size={11} /> {t("review:timeline.restoreHere")}
    </button>
  );
  return (
    <div className="timeline">
      {here(0)}
      {groups.map((g) => (
        <div key={g.key} className="tlgroup">
          <div className="tlhead">
            {g.turn ? <><Bot size={12} /> <span className="grow ellip">“{tl.turns[g.turn]?.message || t("review:timeline.agent")}”</span>
              {tl.turns[g.turn]?.state && tl.turns[g.turn].state !== "done" && <span className="pill">{turnState(tl.turns[g.turn].state!)}</span>}</>
              : <><User size={12} /> <span className="grow">{who(g.actor)}</span></>}
          </div>
          {g.ops.map((o) => (
            <div key={o.opid}>
              <Row o={o} />
              {o !== tl.ops[tl.ops.length - 1] && here(o.last)}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
