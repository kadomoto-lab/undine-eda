// What git says changed, drawn the same wherever it is shown: the History panel (the working tree, a commit) and a
// changeset's review (its base against its working copy). A cell's changed files each with their semantic diff (object
// by object, server/undine/vcs/gitmerge.py), buttons to open the views a cell has now, the files of no cell, and an
// open conflict with each side's value.
import { createContext, useContext, useEffect, useState } from "react";
import { LayoutDashboard } from "lucide-react";
import type { GitCellChange, GitConflict, GitDiff, GitFileChange, GitView, SemanticDiff, ViewKind } from "../api";
import { resolveText, useI18n, type UiText } from "../i18n";
import { cellKind, changeKind, changeText, conflictText, isFlat, shortValue, statusName, viewName } from "./gitItems";

export type OpenView = (lib: string, cell: string, view: ViewKind | "cell") => void;
/** The views a cell has now (null: there is no such cell, undefined: its library's cells are not read yet). */
export type ViewsNow = (lib: string, cell: string) => ViewKind[] | null | undefined;
/** Where the open buttons ask what is there now; without it every changed view not deleted is offered. */
export const Cells = createContext<{ views?: ViewsNow; want?: (lib: string) => void }>({});

const MARK = { add: "+", del: "−", mod: "~" } as const;
const SHORT: Partial<Record<GitView, string>> = { schematic: "sch", symbol: "sym", layout: "lay", rtl: "rtl" };
const OPENS: GitView[] = ["schematic", "symbol", "layout", "rtl"];        // a view with an editor; the others: the cell page
const LIMIT = 40;                                                         // changes shown per file before "more"
const area = (x: number) => (x === 0 ? "0" : x >= 100 ? x.toFixed(1) : x >= 1 ? x.toFixed(3) : x.toPrecision(3));

/** One file's semantic diff: each changed object, a value's before → after, and a layout's XOR per layer. */
export function SemanticDiffView({ diff, conflicted }: { diff: SemanticDiff; conflicted?: boolean }) {
  const { t } = useI18n();
  const [all, setAll] = useState(false);
  // a conflicted file holds one side of each conflict for now: against HEAD it may well show no change at all
  const open = conflicted && <div className="small warn">{t("history:diff.conflicted")}</div>;
  if (diff.error) return <>{open}<div className="note err">{t("history:diff.error", { error: diff.error })}</div></>;
  if (diff.kind === "text") return <>{open}<div className={`fx fx-${diff.do ?? "mod"}`}>{t(`history:diff.text.${diff.do ?? "mod"}`)}</div></>;
  const changes = diff.changes ?? [];
  const shown = all ? changes : changes.slice(0, LIMIT);
  return (
    <div className="gitdiff">
      {open}
      {!changes.length && !conflicted && <div className="dim small">{t("history:diff.none")}</div>}
      {shown.map((c, i) => (
        <div key={`${i}:${c.id}`} className={`fx fx-${c.do}`}>
          <span className="kindmark">{MARK[c.do] ?? "~"}</span>{resolveText(changeText(diff.kind, c))}
          {c.do === "mod" && isFlat(c.before) && isFlat(c.after) && <span className="fieldchange">
            {": "}<span className="before">{shortValue(c.before)}</span> → <span className="after">{shortValue(c.after)}</span>
          </span>}
        </div>
      ))}
      {changes.length > shown.length && (
        <button className="link small" onClick={() => setAll(true)}>{t("history:diff.more", { count: changes.length - shown.length })}</button>
      )}
      {!!diff.geometry?.length && <>
        <div className="small dim gitgeo">{t("history:diff.geometry")}</div>
        {diff.geometry.map((g) => (
          <div key={`${g.cell}|${g.layer}`} className="small mono">
            <span>{t("history:diff.xor", { cell: g.cell, layer: g.layer, area: area(g.xor_um2) })}</span>
            {g.geometry_unchanged && <> · <span className="okc">{t("history:diff.sameGeometry")}</span></>}
          </div>
        ))}
      </>}
    </div>
  );
}

/** The views a cell has now, its library's cells read when they are not yet (undefined until then). The editors and
 * the cell page show a cell as it is now: a cell a commit changed may be gone since, or deleted in the working tree. */
function useCellViews(lib: string, cell: string) {
  const { views, want } = useContext(Cells);
  const now = views?.(lib, cell);
  useEffect(() => { if (now === undefined) want?.(lib); }, [now === undefined, lib]); // eslint-disable-line react-hooks/exhaustive-deps
  return now;
}

export function CellPageButton({ lib, cell, onOpenView }: { lib: string; cell: string; onOpenView: OpenView }) {
  const { t } = useI18n();
  if (useCellViews(lib, cell) === null) return null;
  return <button className="icon" title={t("history:openCell")} onClick={() => onOpenView(lib, cell, "cell")}><LayoutDashboard size={13} /></button>;
}

/** Buttons to open a changed cell's views and its page, those there are now. */
export function OpenButtons({ change, worktree, onOpenView }: { change: GitCellChange; worktree?: boolean; onOpenView: OpenView }) {
  const { t } = useI18n();
  const now = useCellViews(change.lib, change.cell);
  const there = (v: GitView) => (now === undefined ? true : !!now?.includes(v as ViewKind));
  const views = [...new Set(change.views.filter((v) => v.status !== "deleted" && OPENS.includes(v.view) && there(v.view)).map((v) => v.view))];
  // not read yet: a cell all of whose changed files were deleted is taken as gone
  // a file of it in the working tree (an intent written before any view: its page starts the cell) is a cell page
  const page = (now === undefined ? cellKind(change) !== "deleted" : now !== null)
    || (!!worktree && change.views.some((v) => v.status !== "deleted"));
  return (
    <span className="views">
      {views.map((v) => (
        <button key={v} className={`view v-${v}`} title={t("history:open", { view: viewName(v) })}
          onClick={() => onOpenView(change.lib, change.cell, v as ViewKind)}>{SHORT[v]}</button>
      ))}
      {page && <button className="icon" title={t("history:openCell")} onClick={() => onOpenView(change.lib, change.cell, "cell")}>
        <LayoutDashboard size={13} />
      </button>}
    </span>
  );
}

/** A cell's changed files, each with its semantic diff. */
export function CellViews({ change }: { change: GitDiff["cells"][number] }) {
  useI18n();
  return <>
    {change.views.map((v) => (
      <div key={v.path} className="gitview">
        <div className="row small">
          <b>{resolveText(viewName(v.view))}</b>
          <span className={`gitchip gs-${changeKind(v.status)}`}>{resolveText(statusName(v.status))}</span>
          <span className="dim mono ellip" title={v.path}>{v.path}</span>
        </div>
        <SemanticDiffView diff={v.diff} conflicted={v.status === "conflicted"} />
      </div>
    ))}
  </>;
}

/** A diff's cells (one cell's files when lib and cell are given), then the files of no cell. Without onOpenView no
 * view is offered (the version the diff ends at is nowhere to be opened). */
export function DiffCells({ diff, lib, cell, worktree, onOpenView }: {
  diff: GitDiff; lib?: string; cell?: string; worktree?: boolean; onOpenView?: OpenView;
}) {
  const { t } = useI18n();
  if (lib && cell) return <>{diff.cells.filter((c) => c.lib === lib && c.cell === cell).map((c) => <CellViews key={`${c.lib}/${c.cell}`} change={c} />)}</>;
  return <>
    {!diff.cells.length && <div className="dim small">{t("history:diff.noCell")}</div>}
    {diff.cells.map((c) => (
      <div key={`${c.lib}/${c.cell}`} className="gitcelldiff">
        <div className="row">
          <span className={`gitmark gm-${cellKind(c)}`} />
          <b className="mono ellip">{c.lib}/{c.cell}</b>
          <span className="grow" />
          {onOpenView && <OpenButtons change={c} worktree={worktree} onOpenView={onOpenView} />}
        </div>
        <CellViews change={c} />
      </div>
    ))}
    {!!diff.other.length && <OtherFiles files={diff.other} />}
  </>;
}

export function OtherFiles({ files }: { files: GitFileChange[] }) {
  const { t } = useI18n();
  return (
    <details className="oplog">
      <summary className="small dim">{t("history:otherFiles")} <span className="mono">{files.length}</span></summary>
      {files.map((f) => (
        <div key={f.path} className="row small">
          <span className={`gitchip gs-${changeKind(f.status)}`}>{resolveText(statusName(f.status))}</span>
          <span className="mono ellip" title={f.path}>{f.path}</span>
        </div>
      ))}
    </details>
  );
}

export type ConflictSide = "ours" | "theirs" | "base";
/** One open conflict: what it is, each side's value (the one the file holds now marked), and its id for resolve. The
 * sides are git's words unless `labels` names them (a changeset's apply: the design now, the proposal, its base). */
// The ids of conflicts about a whole file or the merge itself (server/undine/vcs/branches.py, logmerge.py): no object
// of the design, so nothing to show the reader as one (the words say it all)
const WHOLE = new Set(["file", "changed", "lines", "error", "layout-journal"]);

export function ConflictRow({ c, kind, labels }: { c: GitConflict; kind: string | null; labels?: Record<ConflictSide, UiText> }) {
  const { t } = useI18n();
  // a file that cannot be read has no sides to show (all three null): only its words
  const sides = c.base != null || c.ours != null || c.theirs != null;
  return (
    <div className={`gitconflict${c.resolved ? " resolved" : ""}`}>
      <div className="row">
        <span className="grow">{resolveText(conflictText(kind, c))}</span>
        {c.resolved && <span className="small okc">{t("history:conflict.resolved")}</span>}
      </div>
      {sides && <div className="row small gitsides">
        {(["ours", "theirs", "base"] as const).map((side) => (
          <span key={side} className={c.taken === side ? "taken" : undefined} title={c.taken === side ? t("history:conflict.taken") : undefined}>
            <span className="dim">{labels ? resolveText(labels[side]) : t(`history:conflict.${side}`)}</span> <span className="mono">{shortValue(c[side], 32)}</span>
          </span>
        ))}
      </div>}
      {!WHOLE.has(c.id) && <div className="small dim mono ellip" title={c.id}>{c.id}</div>}
    </div>
  );
}
