// A changeset's review: what it changes (by object from its operations, by file from its branch's semantic diff), its
// checks before and after, and the decision. On git (docs/design/changeset-branches.md) a changeset is the branch
// undine/<project>/<id>: adopting it merges that branch into the project object by object, all or nothing, and a
// conflict writes nothing; one made before (no "git" in its record) can only be reviewed and discarded.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle, Bot, Check, ChevronLeft, Eye, GitBranch, GitPullRequestArrow, History, Loader2, MessageSquare, Plus, RefreshCw, RotateCcw,
  Scissors, ShieldCheck, Square, Trash2, X,
} from "lucide-react";
import { hashParam } from "../App";
import {
  api, ChangeOp, ChangesetConflictFile, ChangesetInfo, ChangesetMerge, CheckRow, EvidenceSizes, GitConflict, GitDiff, ViewKind,
} from "../api";
import { Splitter } from "../layout";
import { ChangeItem, checkResultLabel, describeEffect, followText, formatChangeValue, groupChanges, viewLabel } from "./changeItems";
import { closingPhase, closingText, formatBytes } from "./closing";
import { ConflictRow, DiffCells, type ConflictSide, type OpenView } from "./gitViews";
import { TimelineView } from "./TimelineView";
import { ask, confirmDialog } from "../dialog";
import { apiErrorText, joinText, msg, resolveText, t, useI18n, type UiText } from "../i18n";

interface Props {
  initialId?: string | null;
  project: string; // the real project
  onClose: () => void;
  onPreview: (previewProject: string) => void;
  onOpen?: (id: string) => void; // choose a changeset in the list: it opens as a docked review
  /** Docked review: changed objects, checks and decision next to the drawing and the conversation. */
  review?: {
    side: "confirmed" | "proposal" | "diff";
    onSide: (s: "confirmed" | "proposal" | "diff", preview: string) => void;
    onFocus: (item: ChangeItem, preview: string) => void;
    focused: string | null;
    onSettled: () => void; // applied or discarded: the working copy is gone
    follow?: { paused: boolean; onPause: () => void; onResume: () => void }; // following an agent's work
    onRequestChanges?: (preview: string) => void; // open the agent's chat to continue this proposal
  };
  onChanged: (merge?: ChangesetMerge | null, action?: "apply" | "revert") => void; // the real design changed: the files the merge wrote
  onWorkChanged?: () => void; // the working copy changed under the open views (a checkpoint was restored)
  onOpenView?: (project: string, lib: string, cell: string, view: ViewKind | "cell") => void;  // a changed file's view
  onGit?: () => void; // the project's git state changed (a changeset opened: git-setup may have run)
  onHistory?: () => void; // the History panel: a merge left uncommitted is committed there
  /** The changesets being adopted, discarded or reverted on the server as the screen's /live poll last saw them (their
   * ids, joined): one that starts or ends there, from this screen's other panel or another screen, is read again. */
  liveClosings?: string;
  toast: (m: UiText, k?: "error" | "info") => void;
}

const isAgent = (a: string) => a.startsWith("agent:");
// how a changeset's state reads to the person deciding: a proposal they adopt or discard
const short = formatChangeValue;
export const describe = (e: Parameters<typeof describeEffect>[0]): string => resolveText(describeEffect(e));

const stateText = (state?: string) => {
  const states: Record<string, UiText> = {
    queued: msg("review:state.queued"), running: msg("review:state.running"), done: msg("review:state.done"), failed: msg("review:state.failed"),
    cancelled: msg("review:state.cancelled"), interrupted: msg("review:state.interrupted"), stopping: msg("review:state.stopping"), stopped: msg("review:state.stopped"), not_run: msg("review:state.not_run"),
  };
  return resolveText(state ? Object.hasOwn(states, state) ? states[state] : state : msg("review:check.unknown"));
};

function summaryLine(ops: ChangeOp[]): string {
  const n = ops.flatMap((o) => o.effects);
  const c = (k: string, d: string) => n.filter((e) => e.k === k && e.do === d).length;
  const parts = [
    (c("i", "mod") + c("o", "mod")) && t("review:summary.changed", { count: c("i", "mod") + c("o", "mod") }), (c("i", "add") + c("w", "add") + c("s", "add")) && t("review:summary.added", { count: c("i", "add") + c("w", "add") + c("s", "add") }),
    (c("i", "del") + c("w", "del") + c("s", "del")) && t("review:summary.removed", { count: c("i", "del") + c("w", "del") + c("s", "del") }),
  ].filter(Boolean);
  return parts.join(" · ") || t("review:summary.none");
}

/** Why a merge was left uncommitted (ChangesetMerge.why), as the review words it; null: a record from before "why" (its
 * words of then: the project had other uncommitted changes) or a why not known here. */
const leftWhy = (m: ChangesetMerge) => m.why === "no_commit" || m.why === "changes" || m.why === "failed" ? m.why : null;

/** Apply / revert: whether the merge was committed on the user's branch, or left in their working tree to commit, and
 * why: the repository has no commit yet (History makes the first one), the commit failed, other uncommitted changes. */
function mergeToast(action: "apply" | "revert", m?: ChangesetMerge | null): UiText {
  const apply = action === "apply";
  if (m?.commit) return msg(apply ? "review:adopted.commit" : "review:reverted.commit", { commit: m.commit.slice(0, 7) });
  if (m?.uncommitted) {
    const why = leftWhy(m);
    if (why === "no_commit" || why === "failed") return msg(apply ? `review:adopted.uncommitted.${why}` : `review:reverted.uncommitted.${why}`);
    return msg(apply ? "review:adopted.uncommitted" : "review:reverted.uncommitted");
  }
  return msg(apply ? "review:adopted" : "review:reverted");
}

// what each side of a conflict is: apply merges the proposal into the design now (base: where it started); revert merges
// the state before it into the design now, with the adopted proposal as the base
const SIDES: Record<"apply" | "revert", Record<ConflictSide, UiText>> = {
  apply: { ours: msg("review:conflict.side.now"), theirs: msg("review:conflict.side.proposal"), base: msg("review:conflict.side.opened") },
  revert: { ours: msg("review:conflict.side.now"), theirs: msg("review:conflict.side.before"), base: msg("review:conflict.side.adopted") },
};
interface MergeConflict { action: "apply" | "revert"; detail: string; files: ChangesetConflictFile[] }

// The conflicts only an apply / revert finds (server/undine/vcs/branches.py, server/undine/store/changeset.py): a whole
// file that cannot be merged ("file": deleted on one side and changed on the other, a side that cannot be read as a
// file of its view (yamlmerge), a path that is no file in the project; "lines": merged by lines; "error"), a file
// that changed in the project while the merge was being made ("changed": the compare-and-swap of the write, nothing
// written: doing it again merges that edit), and the checks across files (a symbol transaction's member changed since,
// a consumer it does not cover, unsaved layout edits). The server words them in English for the command line and the
// agent; here they are worded from their ids, and a whole file's sides ("changed", "deleted", "added", "file": the
// base had it) are words too. The server's words stay for a form not known here; a semantic conflict (an object's id)
// is worded by ConflictRow.
const STATES = ["changed", "deleted", "added", "file"] as const;
const isState = (v: unknown): v is typeof STATES[number] => (STATES as readonly unknown[]).includes(v);
const symbolOf = (s: string) => s.replace(/ \((?:schematic|symbol)\)$/, "");
const NOT_FILE = "not a file in the project";            // branches.plan_merge: a folder (say) where the file is
const isNotFile = (c: GitConflict) => c.id === "file" && c.what === NOT_FILE;
// a file's kind as the server names it in "the <kind> file cannot be read" (yamlmerge: intent, netlist)
const KINDS = ["schematic", "symbol", "layout", "intent", "netlist"] as const;
const kindName = (k: string): UiText => ((KINDS as readonly string[]).includes(k) ? msg(`review:view.${k as typeof KINDS[number]}`) : k);
/** Every conflict of the 409 is a file that changed while the merge was being made: doing it again is all it takes. */
const changedOnly = (files: ChangesetConflictFile[]) =>
  files.length > 0 && files.every((f) => !!f.conflicts?.length && !f.error && f.conflicts.every((c) => c.id === "changed"));

function conflictWords(c: GitConflict, action: "apply" | "revert"): UiText | null {
  const revert = action === "revert";
  let m: RegExpExecArray | null;
  if (c.id === "file") {
    if (isNotFile(c)) return msg("review:conflict.what.notFile");
    if ((m = /^the (\w+) file cannot be read \((.*)\)$/s.exec(c.what))) return msg("review:conflict.what.unreadable", { kind: kindName(m[1]), error: m[2] });
    if (/^deleted on (ours|theirs)\b/.test(c.what) || c.ours === "deleted" || c.theirs === "deleted") return msg("review:conflict.what.file");
    return null;
  }
  if (c.id === "changed") return msg(revert ? "review:conflict.what.changedMeanwhileRevert" : "review:conflict.what.changedMeanwhile");
  if (c.id === "lines") {
    const given = (c as { n?: unknown }).n;             // the count, when the server gives it as a field
    const n = typeof given === "number" ? given : Number(/^(\d+) place/.exec(c.what)?.[1] ?? NaN);
    return Number.isFinite(n) ? msg("review:conflict.what.lines", { count: n }) : msg("review:conflict.what.linesSome");
  }
  if (c.id === "error") return msg("review:conflict.what.error", { error: c.what.replace(/^cannot merge: /, "") });
  if (c.id === "layout-journal") {
    const n = Number(/^(\d+) unsaved/.exec(c.what)?.[1] ?? (Array.isArray(c.theirs) ? c.theirs.length : NaN));
    return Number.isFinite(n) ? msg("review:conflict.what.journal", { count: n }) : null;
  }
  if (c.id.startsWith("symbol:")) {
    if ((m = /symbol transaction of (.+) is incomplete/s.exec(c.what))) return msg("review:conflict.what.symbolIncomplete", { source: symbolOf(m[1]) });
    if ((m = /^(.+) \((schematic|symbol)\) changed in the project since .+ symbol transaction of (.+): undo those edits/s.exec(c.what))) {
      return msg(revert ? "review:conflict.what.symbolMemberRevert" : "review:conflict.what.symbolMember",
        { cell: m[1], view: msg(`review:view.${m[2] as "schematic" | "symbol"}`), source: symbolOf(m[3]) });
    }
    return null;
  }
  if (c.id.startsWith("symbol-consumer:")) {
    const cell = c.id.slice("symbol-consumer:".length);
    if ((m = /^new dependent wiring in .+? is not part of the changeset's symbol transaction of (.+): update or undo/s.exec(c.what))) {
      return msg(revert ? "review:conflict.what.consumerRevert" : "review:conflict.what.consumer", { cell, source: symbolOf(m[1]) });
    }
    return msg("review:conflict.what.consumerWhy", { cell, why: c.what });
  }
  return null;
}

/** A conflict as ConflictRow shows it: the ones of an apply / revert in the reader's language (conflictWords). */
function wordConflict(c: GitConflict, action: "apply" | "revert"): GitConflict {
  const words = conflictWords(c, action);
  if (!words) return c;
  const side = (v: unknown) => (isState(v) ? resolveText(msg(`review:conflict.state.${v}`)) : v);
  // no file in the design: the server's state there ("deleted") is no deletion
  const ours = isNotFile(c) ? resolveText(msg("review:conflict.state.notFile")) : side(c.ours);
  return { ...c, what: resolveText(words), base: side(c.base), ours, theirs: side(c.theirs) };
}

/** A 409 merge_conflict of apply / revert: each file and its conflicts, that nothing was written, and what to do. */
function ConflictNote({ c, branch, onHide }: { c: MergeConflict; branch: string; onHide: () => void }) {
  useI18n();
  const apply = c.action === "apply";
  const again = changedOnly(c.files);         // nothing conflicts: a file changed while the merge was being made
  return (
    <div className="note warnnote csconflict">
      <div className="row">
        <AlertTriangle size={13} />
        <b className="grow">{t(again ? apply ? "review:conflict.applyChanged" : "review:conflict.revertChanged"
          : apply ? "review:conflict.apply" : "review:conflict.revert", { count: c.files.length })}</b>
        <button className="icon" title={t("review:conflict.hide")} onClick={onHide}><X size={13} /></button>
      </div>
      <div className="small">{t("review:conflict.nothing")}</div>
      {!c.files.length && <div className="small">{c.detail}</div>}
      {c.files.map((f) => (
        <div key={f.path} className="gitunmerged">
          <div className="mono">{f.path}</div>
          {f.error && <div className="small">{t("history:conflict.error", { error: f.error })}</div>}
          {!f.conflicts?.length && !f.error && <div className="small">{t("review:conflict.file")}</div>}
          {f.conflicts?.map((x, i) => <ConflictRow key={`${i}:${x.id}`} c={wordConflict(x, c.action)} kind={f.kind} labels={SIDES[c.action]} />)}
        </div>
      ))}
      <div className="small">{again ? t(apply ? "review:conflict.againApply" : "review:conflict.againRevert")
        : t(apply ? "review:conflict.nextApply" : "review:conflict.nextRevert", { branch })}</div>
    </div>
  );
}

/** The changeset's branch by file as the review read it last (the error of the last read, the diff before it kept). */
interface Files { id: string; diff: GitDiff | null; err: UiText | null }
// a library's operation log (history/<lib>.jsonl) holds records an undo took back too: not a change to adopt on its own
const isLog = (path: string) => /(^|\/)history\/[^/]+\.jsonl$/.test(path);
const changesFiles = (d: GitDiff) => d.cells.length > 0 || d.other.some((f) => !isLog(f.path));

/** The changeset's branch by file: its base against the working copy while open (what adopting merges, also what no
 * operation recorded), against the tip it was closed with after. Read again when its working copy moves on. */
function ChangesetFiles({ cs, files, onRefresh, onOpenView }: {
  cs: ChangesetInfo; files: Files | null; onRefresh: () => void; onOpenView?: OpenView;
}) {
  useI18n();
  const f = files?.id === cs.id ? files : null;
  const open = cs.status === "open";
  const base = cs.git?.base.slice(0, 7) ?? "";
  return (
    <div className="gitrevdiff csfiles">
      <div className="row small dim">
        <span className="grow">{open ? t("review:files.open", { base }) : t("review:files.closed", { base, tip: cs.git?.tip?.slice(0, 7) ?? "—" })}</span>
        <button className="icon" title={t("review:files.refresh")} onClick={onRefresh}><RefreshCw size={13} /></button>
      </div>
      {f?.err && <div className="note err">{resolveText(f.err)}</div>}
      {f?.diff ? <DiffCells diff={f.diff} worktree={open} onOpenView={onOpenView} />
        : !f?.err && <div className="dim small">{t("common:state.loading")}</div>}
    </div>
  );
}

/** After "Adopted / Reverted <when> by <who>": its commit, or that the merge (the revert) was left in the working tree,
 * and why (History: the first commit of a repository with none, or what is uncommitted). */
function MergeNote({ m, revert, onHistory }: { m?: ChangesetMerge | null; revert?: boolean; onHistory?: () => void }) {
  useI18n();
  if (m?.commit) return <> · <span className="mono" title={m.commit}>
    {t(revert ? "review:merge.revertCommit" : "review:merge.commit", { commit: m.commit.slice(0, 7) })}</span></>;
  if (!m?.uncommitted) return null;
  const why = leftWhy(m);
  const label = why ? t(revert ? `review:merge.revertUncommitted.${why}` : `review:merge.uncommitted.${why}`)
    : t(revert ? "review:merge.revertUncommitted" : "review:merge.uncommitted");
  return <> · <span className="warn">{label}</span>
    {onHistory && <> <button className="link small" onClick={onHistory}><History size={12} /> {t("review:merge.history")}</button></>}</>;
}

function CheckResult({ row, archived }: { row?: CheckRow; archived: boolean }) {
  useI18n();
  if (!row) return <span className="dim">—</span>;
  const detail = resolveText(checkResultLabel(row));
  if (row.state === "not_run") return <span className="dim">{t("review:check.notRun")}</span>;
  if (row.state !== "done") return <span className={row.error || row.state === "failed" ? "badc" : "dim"}
    title={row.error ?? (row.summary || row.detail != null ? detail : undefined)}>{stateText(row.state)}</span>;
  if (row.fresh !== true) return <span className="warn" title={row.changed?.join("\n")}>
    {t("review:check.previous", { detail })} · {t(row.fresh === false ? "review:check.outdated" : "review:check.freshnessUnknown")}
  </span>;
  // an ERC row's findings are warnings, not violations
  return <span className={row.ok === true ? "okc" : row.ok === false ? (row.severity === "warning" ? "warn" : "badc") : "dim"}>
    {detail} <span className="dim">· {t(archived ? "review:check.captured" : "review:check.current")}</span>
  </span>;
}

export function ChangesPanel({ initialId, project, onClose, onPreview, onOpen, onChanged, onWorkChanged, onOpenView, onGit, onHistory, review,
  liveClosings, toast }: Props) {
  useI18n();
  const [list, setList] = useState<ChangesetInfo[]>([]);
  const [sel, setSel] = useState<string | null>(initialId ?? (hashParam("changes") !== "1" ? hashParam("changes") : null));
  const [cs, setCs] = useState<ChangesetInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [tab, setTab] = useState<"changes" | "files" | "timeline">("changes");
  const [conflict, setConflict] = useState<(MergeConflict & { id: string }) | null>(null);   // the last apply / revert's

  const loadList = useCallback(() => api.changesets(project).then(setList).catch((e) => toast(apiErrorText(e), "error")), [project, toast]);
  const [reads, setReads] = useState(0);              // the record read (each poll)
  const loadOne = useCallback((id: string) => api.changeset(project, id).then((c) => { setCs(c); setReads((n) => n + 1); })
    .catch((e) => toast(apiErrorText(e), "error")), [project, toast]);
  useEffect(() => { loadList(); }, [loadList]);
  useEffect(() => { if (sel) loadOne(sel); else setCs(null); }, [sel, loadOne]);
  // the list: the evidence closed changesets keep, each and in all (a review reads its own with its record)
  const [sizes, setSizes] = useState<EvidenceSizes | null>(null);
  const loadSizes = useCallback(() => { if (!review) api.evidenceSizes(project).then(setSizes).catch(() => setSizes(null)); }, [project, !!review]); // eslint-disable-line
  useEffect(() => { loadSizes(); }, [loadSizes]);
  // a changeset being adopted, discarded or reverted on the server: the list follows it, and reads the sizes when it ends
  const closingIds = list.filter((c) => c.closing).map((c) => c.id).join(",");
  const wasClosing = useRef("");
  useEffect(() => {
    if (wasClosing.current && wasClosing.current !== closingIds) loadSizes();
    wasClosing.current = closingIds;
    if (!closingIds) return;
    const t = setInterval(() => { if (!document.hidden) loadList(); }, 2000);
    return () => clearInterval(t);
  }, [closingIds, loadList, loadSizes]);
  // one begun or ended elsewhere (the review beside this list, another screen), as /live saw it: the list follows it
  // from then on, and so does the review of that changeset (an adopted one's, which is not polled otherwise)
  const liveSeen = useRef(liveClosings);
  useEffect(() => {
    const before = liveSeen.current;
    liveSeen.current = liveClosings;
    if (liveClosings === undefined || before === liveClosings) return;
    loadList();
    if (sel && `${before ?? ""},${liveClosings}`.split(",").includes(sel)) loadOne(sel);
  }, [liveClosings]); // eslint-disable-line react-hooks/exhaustive-deps

  // Keep freshness up to date when the preview is edited by another client, too.
  const running = cs?.verify?.some((v) => v.state === "running" || v.state === "queued");
  const writer = cs?.status === "open" ? cs.writer : null; // the assistant (or an MCP agent) is editing: read only
  const closing = cs?.closing ?? null;                     // being adopted, discarded or reverted: how far
  const isOpen = cs?.status === "open";
  const needsChecks = cs?.verify?.some((v) => v.kind === "error" ||
    (v.state !== "running" && v.state !== "queued" && (v.state !== "done" || v.fresh !== true)));
  useEffect(() => {
    if (!(isOpen || closing || busy) || !sel) return;     // a revert of an adopted one is followed too
    const t = setInterval(() => { if (!document.hidden) loadOne(sel); }, running || writer || closing || busy ? 1000 : 5000);
    return () => clearInterval(t);
  }, [isOpen, running, !!writer, !!closing, busy, sel, loadOne]); // eslint-disable-line

  const items = useMemo(() => (cs ? groupChanges(cs) : []), [cs]);
  // a tab this changeset has not: by file needs its branch, the timeline its open working copy
  const shownTab = !cs || (tab === "files" && !cs.git) || (tab === "timeline" && cs.status !== "open") ? "changes" : tab;
  // on git the branch's commits count too, and the files: the working copy can change in ways no operation recorded and
  // nothing committed yet (a library made in the preview, a layout imported, a file edited outside Undine), which by
  // file shows and adopting merges
  const recorded = !!cs?.changes?.length || !!cs?.git?.tip || (cs?.git?.commits ?? 0) > 0;
  const unsure = !!cs?.git && cs.status === "open" && !recorded;    // only the files can say: read with each poll
  const [files, setFiles] = useState<Files | null>(null);
  const [filesAgain, setFilesAgain] = useState(0);                  // read again by hand (by file's button)
  const wantFiles = !!cs?.git && (shownTab === "files" || unsure);
  const filesStamp = cs ? `${cs.id}|${cs.status}|${cs.activity?.head ?? ""}|${cs.git?.tip ?? ""}|${filesAgain}` : "";
  const filesShow = !!cs && files?.id === cs.id && !!files.diff && changesFiles(files.diff);
  // One read of a changeset's files at a time. A diff can take longer than a poll (a large layout imported into the
  // working copy): a poll never cancels the read under way, nor starts another beside it, and once the files have
  // shown a change it reads them no more (the working copy moving on, or the button, still do). What a read gives is
  // shown when it lands if the review still shows that changeset and no later read's was shown; one wanted while a
  // read is under way (the working copy moved on, the button) follows it.
  const filesRead = useRef({ seq: 0, shown: 0, busy: null as string | null, stamp: "", wanted: false, again: false, polled: -1 });
  const showing = useRef({ project, id: cs?.id ?? null });
  showing.current = { project, id: cs?.id ?? null };
  useEffect(() => () => { showing.current = { project: "", id: null }; }, []);       // gone: nothing lands
  const readFiles = useCallback((p: string, id: string) => {
    const r = filesRead.current;
    const seq = ++r.seq;
    const key = `${p}\0${id}`;
    r.busy = key;
    r.again = false;
    const land = (next: (f: Files | null) => Files) => {
      if (r.seq === seq) r.busy = null;
      const now = showing.current;
      if (now.project !== p || now.id !== id || seq < r.shown) return;   // another changeset, or a later read's shown
      r.shown = seq;
      setFiles(next);
      if (r.again && r.seq === seq) readFiles(p, id);
    };
    api.changesetDiff(p, id).then((diff) => land(() => ({ id, diff, err: null })),
      (e) => land((f) => ({ id, diff: f?.id === id ? f.diff : null, err: apiErrorText(e) })));
  }, []);
  useEffect(() => {
    const r = filesRead.current;
    if (!wantFiles || !cs) { r.wanted = false; return; }
    // moved: the working copy moved on, the button, another changeset, or the files wanted anew (by file chosen)
    const stamp = `${project}\0${filesStamp}`, moved = r.stamp !== stamp || !r.wanted;
    r.stamp = stamp;
    r.wanted = true;
    if (r.busy === `${project}\0${cs.id}`) {                         // under way: a poll waits for it, a move follows it
      if (moved) r.again = true;
      return;
    }
    if (!moved && !(unsure && !filesShow && r.polled !== reads)) return;   // a poll with nothing to learn
    r.polled = reads;
    readFiles(project, cs.id);
  }, [project, wantFiles, filesStamp, unsure && !filesShow ? reads : 0]); // eslint-disable-line react-hooks/exhaustive-deps
  const hasChanges = recorded || filesShow;
  // where a changed file's views are: the working copy while open, the design once adopted; nowhere otherwise
  const filesOpen = cs?.status === "open" ? cs.preview : cs?.status === "applied" ? project : null;
  const openFileView: OpenView | undefined = filesOpen && onOpenView ? (lib, cell, view) => onOpenView(filesOpen, lib, cell, view) : undefined;

  const CONFIRM = {
    apply: { title: msg("review:confirm.apply.title"), message: msg("review:confirm.apply.message"), okLabel: msg("review:adopt") },
    // one made before changesets were branches is a copy: nothing of it goes to git
    discard: { title: msg("review:confirm.discard.title"), message: msg(cs?.git ? "review:confirm.discard.message" : "review:confirm.discard.messageOld"),
      okLabel: msg("review:discard"), danger: true },
    revert: { title: msg("review:confirm.revert.title"), message: msg("review:confirm.revert.message"), okLabel: msg("review:revert"), danger: true },
  } as const;

  const act = async (action: "verify" | "apply" | "discard" | "revert") => {
    if (!cs || (action !== "verify" && !(await confirmDialog(CONFIRM[action])))) return;
    setBusy(true);
    setConflict(null);
    try {
      if (action !== "verify") {             // another is being closed: the server takes this one after it, say so
        const ahead = (await api.changesets(project).catch(() => [] as ChangesetInfo[])).find((c) => c.closing && c.id !== cs.id);
        if (ahead?.closing) toast(msg(`review:closing.queued.${ahead.closing.action}`, { title: ahead.title }), "info");
      }
      const r = await api.changesetAction(project, cs.id, action);
      // Read the record again for the changes and checks, including archived evidence.
      await loadOne(cs.id);
      loadList();
      if (action === "apply" || action === "discard") review?.onSettled();
      if (action === "apply" || action === "revert") {
        const merge = action === "apply" ? r.applied : r.reverted;
        onChanged(merge, action);                       // its paths: the files under the open views it wrote
        toast(mergeToast(action, merge));
      }
      if (action === "discard") toast(msg("review:discarded"));
    } catch (e) {
      // the merge conflicts: nothing was written, and the files say where (the error's own text names none of them)
      const x = e as { code?: string; detail?: unknown; files?: ChangesetConflictFile[] };
      if ((action === "apply" || action === "revert") && x?.code === "merge_conflict") {
        const files = x.files ?? [];
        setConflict({ id: cs.id, action, detail: typeof x.detail === "string" ? x.detail : "", files });
        toast(msg(changedOnly(files) ? action === "apply" ? "review:conflict.toastApplyChanged" : "review:conflict.toastRevertChanged"
          : action === "apply" ? "review:conflict.toastApply" : "review:conflict.toastRevert"), "error");
      } else toast(apiErrorText(e), "error");
    } finally { setBusy(false); }
  };

  // the waveforms of a closed changeset's evidence: the person's call, the size named before it is done (and what of it
  // a working copy its closure kept still holds: freed only when that copy goes)
  const prune = async () => {
    const size = cs?.evidence?.size;
    if (!cs || !size?.raw) return;
    const raw = formatBytes(size.raw);
    const message = msg("review:confirm.prune.message", { size: raw });
    if (!(await confirmDialog({ title: msg("review:confirm.prune.title"),
      message: size.raw_shared ? joinText([message, msg("review:confirm.prune.kept", { size: formatBytes(size.raw_shared) })], " ") : message,
      okLabel: msg("review:confirm.prune.ok", { size: raw }), danger: true }))) return;
    setBusy(true);
    try {
      const r = await api.pruneEvidence(project, cs.id);
      const kept = r.pruned.bytes - (r.pruned.freed ?? r.pruned.bytes);
      toast(kept > 0 ? msg("review:evidence.prunedToastKept", { size: formatBytes(r.pruned.bytes), kept: formatBytes(kept) })
        : msg("review:evidence.prunedToast", { size: formatBytes(r.pruned.bytes) }));
      await loadOne(cs.id);
    } catch (e) {
      toast(apiErrorText(e), "error");
    } finally { setBusy(false); }
  };

  const open = async () => {
    await ask({
      title: msg("review:create.title"), submitLabel: msg("review:create.open"),
      message: msg("review:create.message"),
      fields: [
        { name: "title", label: msg("review:create.name"), required: true, placeholder: msg("review:create.namePlaceholder") },
        { name: "reason", label: msg("review:create.reason"), type: "textarea", placeholder: msg("review:create.optional") },
      ],
      // in a repository not set up for Undine the server refuses (400, code not_set_up), and so it does a project with
      // a library a working copy cannot hold (400, code unsupported_library; before any git is made): the dialog shows
      // it, worded by apiErrorText (the History panel's button and the command; the library, where and why)
      submit: async (v) => {
        const c = await api.openChangeset(project, v.title.trim(), v.reason.trim());
        // made a repository: its files committed as the first commit (none when the person committed meanwhile), and
        // what to do when git's index was not reset to it
        if (c.git_setup) toast(msg(c.first_commit ? "review:create.gitSetup" : "review:create.gitSetupHead", { branch: c.git?.branch ?? c.id }));
        if (c.git_setup && c.first_commit && c.first_commit_note) {
          toast(msg("history:firstCommit.indexNote", { commit: c.first_commit.slice(0, 7), why: c.first_commit_note }), "error");
        }
        onGit?.();
        await loadList();
        setSel(c.id);
      },
    });
  };

  const renderItem = (it: ChangeItem, preview: string, nested = false): JSX.Element => (
    <div key={it.key} className={`citem ci-${it.kind} ${nested ? "nested" : ""} ${review?.focused === it.key ? "focus" : ""}`}
      onClick={(ev) => { ev.stopPropagation(); review?.onFocus(it, preview); }} role={review ? "button" : undefined}>
      <div className="row">
        <span className={`kindmark fx-${it.kind}`}>{it.kind === "add" ? "+" : it.kind === "del" ? "−" : "~"}</span>
        <b className="mono">{resolveText(it.label)}</b>
        {nested && <span className="dim small mono">{resolveText(followText(it))}</span>}
        <span className="grow" />
        <span className="dim small mono">{it.cell ? `${it.lib}/${it.cell} · ${resolveText(viewLabel(it.view))}` : it.lib}</span>
      </div>
      {it.fields.map((f) => <div key={f.name} className="fieldchange mono">
        {f.name}: {"before" in f && <><span className="before">{short(f.before)}</span> → </>}<span className="after">{short(f.after)}</span>
      </div>)}
      {it.reasons.map((r, i) => <div key={i} className="opreason">“{r.text}” <span className="dim">— {r.actor}</span></div>)}
      {!!it.followedBy.length && <div className="followed">
        <div className="small dim">{t("review:follow.title")}</div>
        {it.followedBy.map((k) => items.find((x) => x.key === k)).filter((x): x is ChangeItem => !!x).map((x) => renderItem(x, preview, true))}
      </div>}
    </div>
  );

  // checks: pair work / base per (kind, checker, library, cell)
  const pairs = (rows: CheckRow[]) => {
    const key = (r: CheckRow) => `${r.kind}|${r.checker ?? "klayout"}|${r.lib}|${r.cell}`;
    return [...new Set(rows.map(key))].map((k) => {
      const [kind, checker, lib, cell] = k.split("|");
      const w = rows.find((r) => key(r) === k && r.side === "work");
      const b = rows.find((r) => key(r) === k && r.side === "base");
      return { kind, checker, lib, cell, w, b };
    }).filter((p) => p.kind !== "error");
  };
  type Pair = ReturnType<typeof pairs>[number];
  const isCurrent = (p: Pair) => p.b?.fresh === true && p.w?.fresh === true && p.b.state === "done" && p.w.state === "done";
  const isRegress = (p: Pair) => isCurrent(p) && p.b?.ok === true && p.w?.ok === false;
  const regressions = cs ? pairs(cs.verify ?? []).filter(isRegress).length : 0;

  return (
    <div className={review ? "drawer docked review" : "drawer"}>
      <Splitter pane="drawer" className="drawer-edge" />
      <div className="drawerhead">
        {cs && !review ? <button className="icon" title={t("review:back")} onClick={() => setSel(null)}><ChevronLeft size={16} /></button> : <GitPullRequestArrow size={16} className="dim" />}
        <b className="grow">{cs ? cs.title : t("review:title")}</b>
        {!cs && <button className="ghost" onClick={open}><Plus size={13} /> {t("review:new")}</button>}
        <button className="icon" title={t(review ? "review:closeReview" : "review:close")} onClick={onClose}><X size={15} /></button>
      </div>

      {!cs && sel ? (
        <div className="drawerbody"><div className="empty">{t("common:state.loading")}</div></div>   // a review opening: not the empty list
      ) : !cs ? (
        <div className="drawerbody">
          {!list.length && <div className="empty">{t("review:empty")}</div>}
          {list.map((c) => (
            <div key={c.id} className="csrow" onClick={() => (onOpen ? onOpen(c.id) : setSel(c.id))}>
              {c.closing ? <span className="pill st-closing"><Loader2 size={12} className="spin" /> {t(`review:status.closing.${c.closing.action}`)}</span>
                : <span className={`pill st-${c.status}`}>{t(`review:status.${c.status}`)}</span>}
              <span className="grow ellip">{c.title}
                {c.closing && <span className="small dim csclosing"> · {resolveText(closingPhase(c.closing))}</span>}</span>
              {!c.closing && sizes?.changesets[c.id] && <span className="dim small mono">{formatBytes(sizes.changesets[c.id].bytes)}</span>}
              {isAgent(c.actor) ? <span className="agent"><Bot size={12} /> {c.actor.slice(6)}</span> : <span className="dim small">{c.actor}</span>}
            </div>
          ))}
          {!!sizes?.bytes && <div className="small dim evtotal">
            {sizes.raw ? t("review:evidence.totalRaw", { size: formatBytes(sizes.bytes), raw: formatBytes(sizes.raw) })
              : t("review:evidence.total", { size: formatBytes(sizes.bytes) })}
          </div>}
        </div>
      ) : (
        <div className="drawerbody">
          <div className="row small dim">
            <span className={`pill st-${cs.status}`}>{t(`review:status.${cs.status}`)}</span>
            {isAgent(cs.actor) ? <span className="agent"><Bot size={12} /> {cs.actor.slice(6)}</span> : <span>{cs.actor}</span>}
            <span className="grow" /><span className="mono">{cs.id}</span>
          </div>
          {cs.git && (
            <div className="row small dim csgit">
              <GitBranch size={13} />
              <span className="mono ellip" title={t(cs.git.ref ? "review:git.kept" : "review:git.branchHint")}>{cs.git.ref ?? cs.git.branch}</span>
              <span className="mono" title={`${t("review:git.baseHint")}\n${cs.git.base}`}>{t("review:git.base", { base: cs.git.base.slice(0, 7) })}</span>
              <span>{t("review:git.commits", { count: cs.git.commits })}</span>
            </div>
          )}
          {cs.reason && <div className="note">{cs.reason}</div>}
          {closing && (
            <div className="note closingnote" role="status">
              <Loader2 size={13} className="spin" /> <b>{resolveText(closingText(closing))}</b>
              <div className="small dim full">{t("review:closing.note")}</div>
            </div>
          )}
          {!cs.git && cs.status === "open" && <div className="note warnnote">{t("review:legacy.open")}</div>}
          {writer && (
            <div className="note writernote">
              <Bot size={13} /> {t(writer.stop_requested ? "review:writer.stopping" : "review:writer.editing", {
                writer: writer.label ?? t(writer.kind === "turn" ? "review:writer.agent" : "review:writer.external"),
                transport: writer.kind === "mcp" ? t("review:writer.mcp") : "",
              })}
              {review?.follow && (review.follow.paused
                ? <button className="ghost" onClick={review.follow.onResume}>{t("review:follow.resume")}</button>
                : <button className="ghost" onClick={review.follow.onPause}>{t("review:follow.pause")}</button>)}
              <button className="ghost" disabled={writer.stop_requested}
                onClick={() => api.writerStop(project, cs.id).then(() => loadOne(cs.id)).catch((e) => toast(apiErrorText(e), "error"))}>
                <Square size={12} /> {t("review:stop")}
              </button>
            </div>
          )}
          {/* the decision first: adopting is the usual outcome, and the changes and checks below can be long */}
          {cs.status === "open" && (
            <div className="row csactions">
              {!review && <button className="ghost" onClick={() => { onPreview(cs.preview); onClose(); }}><Eye size={13} /> {t("review:openPreview")}</button>}
              {/* one made before changesets were branches cannot be adopted: the agent's work in it would be lost */}
              {review?.onRequestChanges && cs.git && (
                <button className="ghost" disabled={busy} title={t("review:requestHint")}
                  onClick={() => review.onRequestChanges!(cs.preview)}><MessageSquare size={13} /> {t("review:request")}</button>
              )}
              <span className="grow" />
              <button className="ghost" title={t("review:discardHint")} disabled={busy || running || !!writer || !!closing} onClick={() => act("discard")}><Trash2 size={13} /> {t("review:discard")}</button>
              {/* one opened before changesets were branches has no branch to merge: it is opened again instead */}
              <button className="primary" disabled={busy || running || !!writer || !!closing || !hasChanges || !cs.git}
                title={cs.git ? undefined : t("review:legacy.adopt")} onClick={() => act("apply")}><Check size={13} /> {t("review:adopt")}</button>
            </div>
          )}
          {cs.status === "open" && (regressions > 0 || running) && (
            <div className={`small ${regressions ? "badc" : "dim"}`}>
              {regressions > 0 && t("review:checks.regressedBelow", { count: regressions })}
              {regressions > 0 && running && " "}
              {running && t("review:checks.wait")}
            </div>
          )}
          {conflict?.id === cs.id && cs.git && <ConflictNote c={conflict} branch={cs.git.ref ?? cs.git.branch} onHide={() => setConflict(null)} />}
          {cs.status === "applied" && (
            <div className="row csactions">
              <span className="dim small grow">{t("review:adoptedAt", { time: cs.applied?.t?.slice(0, 16).replace("T", " ") ?? "", actor: cs.applied?.by ?? "" })}
                <MergeNote m={cs.applied} onHistory={onHistory} /></span>
              {/* reverting one made before changesets were branches (by inverse effects) is not offered any more */}
              {cs.git && <button className="ghost" disabled={busy || !!closing} onClick={() => act("revert")}><RotateCcw size={13} /> {t("review:revert")}</button>}
            </div>
          )}
          {cs.status === "applied" && !cs.git && <div className="small dim">{t("review:legacy.revert")}</div>}
          {cs.status === "reverted" && <div className="note">{t("review:revertedAt", { time: cs.reverted?.t?.slice(0, 16).replace("T", " ") ?? "", actor: cs.reverted?.by ?? "" })}
            <MergeNote m={cs.reverted} revert onHistory={onHistory} /></div>}
          {review && cs.status === "open" && (
            <div className="sidetoggle" role="group" aria-label={t("review:side.label")}>
              <button className={review.side === "confirmed" ? "on" : ""} onClick={() => review.onSide("confirmed", cs.preview)}>{t("review:side.confirmed")}</button>
              <button className={review.side === "proposal" ? "on" : ""} onClick={() => review.onSide("proposal", cs.preview)}>{t("review:side.proposal")}</button>
              <button className={review.side === "diff" ? "on" : ""} onClick={() => review.onSide("diff", cs.preview)}>{t("review:side.diff")}</button>
            </div>
          )}
          {review && cs.status === "open" && review.side === "diff" && (
            <div className="difflegend small">
              <span className="okc">{t("review:diff.added")}</span> <span className="badc">{t("review:diff.removed")}</span> <span className="warn">{t("review:diff.changed")}</span>
              <span className="dim"> · {t("review:diff.note")}</span>
            </div>
          )}
          {cs.cleanup_warning && <div className="note warnnote">
            <div>{cs.cleanup_warning}</div>
            {cs.retained !== false && <button className="ghost" onClick={() => { onPreview(cs.preview); if (!review) onClose(); }}>
              <Eye size={13} /> {t("review:openRetained")}
            </button>}
          </div>}

          <div className="row">
            <div className="section grow">{t(shownTab === "timeline" ? "review:timeline" : shownTab === "files" ? "review:files" : "review:title")}</div>
            {(cs.status === "open" || cs.git) && (
              <div className="sidetoggle small" role="group" aria-label={t("review:listing.label")}>
                <button className={shownTab === "changes" ? "on" : ""} onClick={() => setTab("changes")}>{t("review:listing.objects")}</button>
                {cs.git && <button className={shownTab === "files" ? "on" : ""} onClick={() => setTab("files")}>{t("review:listing.files")}</button>}
                {cs.status === "open" && <button className={shownTab === "timeline" ? "on" : ""} onClick={() => setTab("timeline")}>{t("review:timeline")}</button>}
              </div>
            )}
          </div>
          {shownTab === "timeline" ? (
            <TimelineView project={project} changeset={cs.id} version={cs} locked={!!writer} toast={toast}
              onRestored={() => { loadOne(cs.id); onWorkChanged?.(); toast(msg("review:restored")); }} />
          ) : shownTab === "files" ? (
            <ChangesetFiles cs={cs} files={files} onRefresh={() => setFilesAgain((x) => x + 1)} onOpenView={openFileView} />
          ) : <>
          {!cs.changes?.length && <div className="empty" style={{ padding: 0 }}>{cs.status === "open" ? t("review:noChanges") : "—"}</div>}
          {items.filter((it) => !it.causedBy.length).map((it) => renderItem(it, cs.preview))}
          {!!cs.changes?.length && (
            <details className="oplog">
              <summary className="small dim">{t("review:operationLog")}</summary>
              {cs.changes.map((lc) => {
                const byCell = new Map<string, ChangeOp[]>();
                lc.ops.forEach((o) => { const k = `${o.cell ?? "(library)"} · ${o.view}`; byCell.set(k, [...(byCell.get(k) ?? []), o]); });
                return [...byCell].map(([k, ops]) => (
                  <div key={lc.lib + k} className="cellchanges">
                    <div className="row"><b className="mono">{lc.lib}/{ops[0].cell ?? t("review:library")} · {resolveText(viewLabel(ops[0].view))}</b><span className="grow" /><span className="dim small">{summaryLine(ops)}</span></div>
                    {ops.map((o) => (
                      <div key={o.seq} className="opblock">
                        {o.reason && <div className="opreason">“{o.reason}” <span className="dim">— {o.actor}</span></div>}
                        {o.effects.slice(0, 8).map((e, i) => <div key={i} className={`fx fx-${e.do}`}>{describe(e)}</div>)}
                        {o.effects.length > 8 && <div className="dim small">{t("review:more", { count: o.effects.length - 8 })}</div>}
                      </div>
                    ))}
                  </div>
                ));
              })}
            </details>
          )}

          </>}

          <div className="section">{t("review:checks")}</div>
          {cs.status !== "open" && <div className="note">
            {cs.evidence ? t(cs.status === "discarded" ? "review:evidence.discarded" : "review:evidence.applied", { time: cs.evidence.captured.slice(0, 16).replace("T", " ") })
              : t("review:evidence.historical")}
          </div>}
          {!cs.verify?.length && <div className="dim small">{cs.status === "open"
            ? t("review:checks.unavailable") : t("review:checks.none")}</div>}
          {pairs(cs.verify ?? []).map((p) => {
            const current = isCurrent(p);
            const regress = isRegress(p);
            const fixed = current && p.b?.ok === false && p.w?.ok === true;
            return (
              <div key={`${p.kind}|${p.checker}|${p.lib}|${p.cell}`} className={`checkrow ${regress ? "regress" : ""}`}>
                <span className="mono">{p.kind.toUpperCase()}{p.checker === "magic" ? ` (${t("review:check.magic")})` : ""} {p.lib}/{p.cell}</span>
                <span className="grow" />
                <span className="small dim">{t("review:checks.base")}</span> <CheckResult row={p.b} archived={cs.status !== "open"} /> <span className="dim">→</span>
                <span className="small dim">{t("review:checks.proposal")}</span> <CheckResult row={p.w} archived={cs.status !== "open"} />
                {regress && <b className="badc"> {t("review:checks.regression")}</b>}{fixed && <b className="okc"> {t("review:checks.fixed")}</b>}
                {[p.b, p.w].filter((r) => r?.error).map((r) => <div className="small badc full" key={`${r?.side}-error`}>
                  {t(r?.side === "base" ? "review:checks.base" : "review:checks.proposal")}: {r?.error}
                </div>)}
                {[p.b, p.w].flatMap((r) => (r?.changed ?? []).map((reason, i) =>
                  <div className="small warn full" key={`${r?.side}-${i}`}>{t(r?.side === "base" ? "review:checks.base" : "review:checks.proposal")}: {reason}</div>))}
                {(p.w?.findings ?? []).slice(0, 8).map((f, i) => <div className="small warn full" key={`finding-${i}`}>
                  {t("review:checks.proposal")}: {t("review:check.finding", { instance: f.instance, terminal: f.terminal, net: f.net, tap: f.tap_net })}
                </div>)}
              </div>
            );
          })}
          {cs.verify?.filter((v) => v.kind === "error").map((v, i) => <div key={i} className="note warnnote">{v.cell}: {v.error}</div>)}
          {cs.evidence && <div className="small dim">
            {t("review:evidence.jobs", { count: cs.evidence.jobs })}
            <div className="mono" style={{ overflowWrap: "anywhere" }}>{cs.evidence.path}</div>
            {cs.evidence.size && <div className="row evsize">
              <span className="grow">{cs.evidence.size.raw
                ? t("review:evidence.sizeRaw", { size: formatBytes(cs.evidence.size.bytes), raw: formatBytes(cs.evidence.size.raw) })
                : t("review:evidence.size", { size: formatBytes(cs.evidence.size.bytes) })}</span>
              {cs.evidence.size.raw > 0 && <button className="ghost" disabled={busy || !!closing} title={t("review:evidence.pruneHint")}
                onClick={prune}><Scissors size={12} /> {t("review:evidence.prune", { size: formatBytes(cs.evidence.size.raw) })}</button>}
            </div>}
            {cs.evidence.pruned && <div>{t("review:evidence.pruned", { time: cs.evidence.pruned.t.slice(0, 16).replace("T", " "),
              actor: cs.evidence.pruned.by, count: cs.evidence.pruned.files, size: formatBytes(cs.evidence.pruned.bytes) })}</div>}
          </div>}
          {cs.status === "open" && (
            <>
              <button className="ghost full" disabled={busy || running || !!closing || !hasChanges || !needsChecks}
                title={t("review:checks.runHint")}
                onClick={() => act("verify")}><ShieldCheck size={13} /> {t("review:checks.run")}</button>
              <div className="small dim">{t("review:checks.runNote")}</div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
