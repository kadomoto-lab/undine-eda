// The project's history in git: what changed since the last commit and in each commit, cell by cell and object by
// object (the semantic diff of server/undine/vcs/gitmerge.py), and for a merge commit the cells nobody checked as
// merged. It reads git only: commits, branches and merges are made with git (docs/design/merge.md), but the project's
// first commit, which it makes when the person asks (an adopted proposal is committed only onto a branch with one).
import { useEffect, useRef, useState, type Dispatch, type SetStateAction } from "react";
import { AlertTriangle, ChevronDown, ChevronRight, GitBranch, GitCommitHorizontal, GitMerge, GitPullRequestArrow, RefreshCw, ShieldCheck, X } from "lucide-react";
import { api, GitCommitRefusal, GitDiff, GitLogCommit, GitStatus, GitVerify } from "../api";
import { confirmDialog } from "../dialog";
import { Splitter } from "../layout";
import { apiErrorText, joinText, msg, resolveText, useI18n, type UiText } from "../i18n";
import { CellGit, cellKind, cellMarkText, changeKind, GroupGit, relativeDate, shellWord, statusName, verifyNotes, verifyWhy, viewName } from "./gitItems";
import { CellPageButton, Cells, ConflictRow, DiffCells, OpenButtons, OtherFiles, type OpenView, type ViewsNow } from "./gitViews";

interface Props {
  project: string;                  // as open: a changeset's working copy (`p~id`) shows its branch, not a history
  status: GitStatus | null;         // App's status, shared with the top bar and the library browser (null: not read)
  error: UiText | null;             // why the status could not be read
  cell: { lib: string; cell: string } | null;   // the active tab's cell, for "only <lib>/<cell>"
  tick: number;                     // the design was saved or reloaded: the working tree's diffs are read again
  cellViews?: ViewsNow;             // what can be opened: a changed cell may be gone since (no view, no cell page)
  wantCells?: (lib: string) => void;   // read a library's cells (cellViews said undefined)
  onStatus: (s: GitStatus) => void; // a status the panel got (after setting git up)
  onRefresh: () => void;            // read the status again
  onReloaded: (lib: string) => void;   // a library's layout was read again from its files (its unsaved edits dropped)
  onOpenView: OpenView;
  onReview?: (changeset: string) => void;   // a changeset's working copy: back to its review
  onClose: () => void;
  toast: (m: UiText, k?: "error" | "info") => void;
}

/** The marker of a cell in the library browser: changed since the last commit, and layout edits not saved yet. */
export function GitCellMark({ g }: { g: CellGit }) {
  useI18n();
  const text = resolveText(cellMarkText(g));
  return (
    <span className="gitmarks" title={text} aria-label={text} role="img">
      {g.change && <span className={`gitmark gm-${cellKind(g.change)}`} />}
      {g.unsaved && <span className="gitmark gm-unsaved" />}
    </span>
  );
}

/** The marker of a group of cells folded into one row (a library's device cells): any of them changed. */
export function GitGroupMark({ g }: { g: GroupGit }) {
  const { t } = useI18n();
  const text = t("history:mark.group", { count: g.count });
  return (
    <span className="gitmarks" title={text} aria-label={text} role="img">
      {g.kind && <span className={`gitmark gm-${g.kind}`} />}
      {g.unsaved && <span className="gitmark gm-unsaved" />}
    </span>
  );
}

/** rev's diff (the working tree, or a commit against its first parent), one cell or every cell it changed. */
function RevDiff({ project, rev, lib, cell, stamp, cache, onOpenView }: {
  project: string; rev: string; lib?: string; cell?: string; stamp: string;
  cache?: Map<string, GitDiff>; onOpenView: OpenView;
}) {
  const { t } = useI18n();
  const key = `${project}|${rev}|${lib ?? ""}|${cell ?? ""}`;       // one repository can hold several projects
  const [d, setD] = useState<GitDiff | null>(() => cache?.get(key) ?? null);
  const [err, setErr] = useState<UiText | null>(null);
  useEffect(() => {
    if (cache?.has(key)) { setD(cache.get(key)!); return; }        // a commit's diff never changes
    let gone = false;
    api.gitDiff(project, rev, lib, cell)
      .then((r) => { if (gone) return; cache?.set(key, r); setD(r); setErr(null); })
      .catch((e) => { if (!gone) setErr(apiErrorText(e)); });
    return () => { gone = true; };
  }, [project, key, stamp]); // eslint-disable-line react-hooks/exhaustive-deps
  if (err) return <div className="note err">{resolveText(err)}</div>;
  if (!d) return <div className="dim small">{t("common:state.loading")}</div>;
  return <div className="gitrevdiff"><DiffCells diff={d} lib={lib} cell={cell} onOpenView={onOpenView} /></div>;
}

/** What a merge check says besides the open conflicts: the cells whose merged hierarchy no branch had (no DRC / LVS
 * covers them), the warnings of the merge, and what the check does not cover. */
function CheckReport({ r, onOpenView, quiet }: { r: GitVerify; onOpenView: OpenView; quiet?: boolean }) {
  const { t } = useI18n();
  return (
    <div className="gitverify">
      {r.verify.length ? <div className="small"><b>{t("history:verify.cells", { count: r.verify.length })}</b></div>
        : !quiet && <div className="small okc">{t("history:verify.none")}</div>}
      {r.verify.map((v) => (
        <div key={`${v.lib}/${v.cell}`} className="checkrow">
          <span className="mono">{v.lib}/{v.cell}</span>
          <span className="grow small dim">{resolveText(verifyWhy(v))}</span>
          <CellPageButton lib={v.lib} cell={v.cell} onOpenView={onOpenView} />
        </div>
      ))}
      {!!r.warnings.length && <>
        <div className="small warn">{t("history:verify.warnings")}</div>
        {r.warnings.map((w, i) => <div key={i} className="small"><span className="mono dim">{w.path}</span> {w.warning}</div>)}
      </>}
      {!!r.no_report.length && <div className="small dim">{t("history:verify.noReport", { files: r.no_report.join(", ") })}</div>}
      {verifyNotes(r).map((n, i) => <div key={i} className="small dim">{resolveText(n)}</div>)}
    </div>
  );
}

/** A merge commit, checked on demand: what CheckReport shows. */
function MergeCheck({ project, sha, onOpenView }: { project: string; sha: string; onOpenView: OpenView }) {
  const { t } = useI18n();
  const [r, setR] = useState<GitVerify | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<UiText | null>(null);
  const load = async () => {
    setBusy(true);
    try { setR(await api.gitVerify(project, sha)); setErr(null); } catch (e) { setErr(apiErrorText(e)); } finally { setBusy(false); }
  };
  if (r) return <CheckReport r={r} onOpenView={onOpenView} />;
  return <>
    <button className="ghost" disabled={busy} title={t("history:verify.hint")} onClick={load}>
      <ShieldCheck size={13} /> {busy ? t("common:state.working") : t("history:verify.button")}
    </button>
    {err && <div className="note err">{resolveText(err)}</div>}
  </>;
}

/** The merge, cherry-pick or rebase in progress: each conflicted file with its open conflicts, how to resolve them,
 * then what the check says of the merge so far. Read again with every status (resolve runs in a shell). */
function InProgress({ project, status, onOpenView }: { project: string; status: GitStatus; onOpenView: OpenView }) {
  const { t } = useI18n();
  const [r, setR] = useState<GitVerify | null>(null);
  const [err, setErr] = useState<UiText | null>(null);
  useEffect(() => {
    let gone = false;
    api.gitVerify(project).then((x) => { if (!gone) { setR(x); setErr(null); } })
      .catch((e) => { if (!gone) setErr(apiErrorText(e)); });
    return () => { gone = true; };
  }, [project, status]);
  const conflicted = status.conflicted ?? [];
  const top = (p: string) => `${status.prefix ?? ""}${p}`;           // merge_check's paths are from the repository's top
  const unmerged = new Map((r?.unmerged ?? []).map((u) => [u.path, u]));
  return <>
    {conflicted.length ? <>
      <div className="row">{!status.operation && <AlertTriangle size={13} />}{t("history:operation.conflicted", { count: conflicted.length })}</div>
      {conflicted.map((p) => {
        const u = unmerged.get(top(p));
        const open = (u?.conflicts ?? []).filter((c) => !c.resolved);
        return (
          <div key={p} className="gitunmerged">
            <div className="mono">{p}</div>
            {u?.error && <div className="small">{t("history:conflict.error", { error: u.error })}</div>}
            {u && !u.conflicts && !u.error && <div className="small">{t("history:conflict.git")}</div>}
            {u?.conflicts && !open.length && <div className="small okc">{t("history:conflict.none")}</div>}
            {u?.conflicts?.map((c) => <ConflictRow key={c.id} c={c} kind={u.kind} />)}
            {!!open.length && <>
              <div className="small">{t("history:conflict.resolve", { count: open.length })}</div>
              {/* a command to copy, not text to translate */}
              <div className="mono small gitcmd">{["undine resolve", shellWord(top(p)), "--theirs", ...open.map((c) => shellWord(c.id))].join(" ")}</div>
            </>}
          </div>
        );
      })}
      {/* how it ends depends on what is in progress: a rebase goes on with git rebase --continue, never a commit */}
      <div className="small">{t(`history:operation.resolve.${status.operation ?? "none"}`)}</div>
    </> : <div>{t(`history:operation.clean.${status.operation ?? "merge"}`)}</div>}
    {err && <div className="small">{resolveText(err)}</div>}
    {r && <CheckReport r={r} onOpenView={onOpenView} quiet />}
    <div className="small dim">{t("history:operation.layouts")}</div>
  </>;
}

/** Layouts with unsaved edits as a refusal of the first commit names them: "l (inv, top); m". */
const layoutsText = (logs: { lib: string; cells: string[] }[]) =>
  logs.map((x) => x.cells.length ? `${x.lib} (${x.cells.join(", ")})` : x.lib).join("; ");

/** Why the project's first commit was refused (POST .../git/first-commit's 409 unsaved, 400 unsupported_library; git
 * setup's commit_refused), in the first commit's words: apiErrorText words a library as an open's refusal. */
function refusalText(r: GitCommitRefusal): UiText {
  if (r.code === "unsaved") return msg("history:firstCommit.unsaved", { layouts: layoutsText(r.logs ?? []) });
  if (r.code === "unsupported_library") {
    const named = { library: r.library ?? "", path: r.path ?? "", target: r.target ?? "" };
    const kinds = { link_out: "linkOut", link_nowhere: "linkNowhere", own_repository: "ownRepository" } as const;
    const kind = r.kind && Object.hasOwn(kinds, r.kind) ? kinds[r.kind as keyof typeof kinds] : null;
    return msg(kind ? `history:firstCommit.unsupported.${kind}` : "history:firstCommit.unsupported", named);
  }
  return r.detail;
}

export function HistoryPanel({ project, status, error, cell, tick, cellViews, wantCells, onStatus, onRefresh, onReloaded, onOpenView, onReview,
  onClose, toast }: Props) {
  const { t, locale } = useI18n();
  const [only, setOnly] = useState(false);
  const [n, setN] = useState(0);                       // refreshed by hand
  const [seq, setSeq] = useState(0);                   // statuses read: an open diff of the working tree is read again
  const [busy, setBusy] = useState(false);
  // what is open, by project: the panel stays open when another project is opened, and one repository can hold both
  const [openCells, setOpenCells] = useState<Set<string>>(new Set());
  const [openCommits, setOpenCommits] = useState<Set<string>>(new Set());
  const [log, setLog] = useState<{ key: string; commits: GitLogCommit[]; more: boolean } | null>(null);
  const [logErr, setLogErr] = useState<UiText | null>(null);
  const [moreBusy, setMoreBusy] = useState(false);
  const [hook, setHook] = useState<{ project: string; note: string } | null>(null);   // why git-setup installed no hook
  const commitDiffs = useRef(new Map<string, GitDiff>());
  const preview = !!status?.preview || project.includes("~");
  const repo = !!status?.repo && !preview;
  const filter = only && cell ? cell : null;
  const logKey = `${project}|${filter ? `${filter.lib}/${filter.cell}` : ""}`;
  const head = status?.head?.sha ?? null;

  // a file can change again and keep its status (modified, conflicted): every status read, an open diff is read again
  useEffect(() => { setSeq((x) => x + 1); }, [status]);

  // commits: again when HEAD moves (a commit, a checkout), the filter changes or by hand; "load more" appends
  useEffect(() => {
    if (!repo) { setLog(null); return; }
    let gone = false;
    api.gitLog(project, { lib: filter?.lib, cell: filter?.cell })
      .then((r) => { if (!gone) { setLog({ key: logKey, ...r }); setLogErr(null); } })
      .catch((e) => { if (!gone) setLogErr(apiErrorText(e)); });
    return () => { gone = true; };
  }, [project, repo, logKey, head, n]); // eslint-disable-line react-hooks/exhaustive-deps
  const shownLog = log?.key === logKey ? log : null;   // another project's or filter's commits are not these
  const loadMore = async () => {
    if (!shownLog) return;
    setMoreBusy(true);
    try {
      const r = await api.gitLog(project, { skip: shownLog.commits.length, lib: filter?.lib, cell: filter?.cell });
      setLog((l) => l && l.key === logKey ? { key: l.key, commits: [...l.commits, ...r.commits.filter((c) => !l.commits.some((x) => x.sha === c.sha))], more: r.more } : l);
    } catch (e) { toast(apiErrorText(e), "error"); } finally { setMoreBusy(false); }
  };

  const setup = async () => {
    if (!await confirmDialog({ title: msg("history:setup.title"), message: msg("history:setup.message"), okLabel: msg("history:setup.button") })) return;
    setBusy(true);
    try {
      const r = await api.gitSetup(project);
      onStatus(r);
      setHook(r.hook_note ? { project, note: r.hook_note } : null);
      // a hook of the repository's own, or core.hooksPath: git-setup leaves it, and says what to add to it
      if (r.hook_note) toast(msg("history:setup.hookNote", { why: r.hook_note }), "error");
      // after its git init: the first commit, or why not (the button below then offers it once that is dealt with)
      else if (r.commit_refused) toast(msg("history:setup.doneNoCommit", { why: refusalText(r.commit_refused) }), "error");
      else toast(r.commit ? msg("history:setup.doneCommit", { commit: r.commit.slice(0, 7) })
        : r.gitignore_added?.length ? msg("history:setup.gitignoreAdded", { lines: r.gitignore_added.join(", ") }) : msg("history:setup.done"));
      if (r.commit && r.index_note) toast(msg("history:firstCommit.indexNote", { commit: r.commit.slice(0, 7), why: r.index_note }), "error");
    } catch (e) { toast(apiErrorText(e), "error"); } finally { setBusy(false); }
  };
  const reload = async (lib: string) => {
    if (!await confirmDialog({ title: msg("history:stale.title", { lib }), message: msg("history:stale.message", { lib }),
      okLabel: msg("history:stale.reload"), danger: true })) return;
    setBusy(true);
    try {
      await api.layoutReload(project, lib);
      toast(msg("history:stale.done", { lib }));
      onReloaded(lib);
    } catch (e) { toast(apiErrorText(e), "error"); } finally { setBusy(false); }
  };
  const refresh = () => { setN((x) => x + 1); onRefresh(); };
  // a repository with no commit yet (git init by hand, or by an Undine before): its files as they are, as the first commit
  const firstCommit = async () => {
    setBusy(true);
    try {
      const r = await api.gitFirstCommit(project);
      onStatus(r);
      setN((x) => x + 1);
      toast(msg("history:firstCommit.done", { commit: r.commit.slice(0, 7) }));
      // another git held the index: the commit is made, `git status` shows the files as changed until git reset
      if (r.index_note) toast(msg("history:firstCommit.indexNote", { commit: r.commit.slice(0, 7), why: r.index_note }), "error");
    } catch (e) {
      const err = e as { code?: string; detail?: unknown; params?: Record<string, unknown>; refusal?: Record<string, unknown> } | null;
      // made meanwhile (git, another window): nothing was committed, and the status says what there is
      if (err?.code === "has_commits") { toast(msg("history:firstCommit.hasCommits"), "error"); refresh(); }
      // unsaved layout edits, a library no working copy can hold: in the first commit's words, not an open's
      else if (err?.code === "unsaved" || err?.code === "unsupported_library") {
        toast(refusalText({ ...err.params, ...err.refusal, code: err.code, detail: typeof err.detail === "string" ? err.detail : "" }), "error");
        if (err.code === "unsaved") refresh();
      } else toast(apiErrorText(e), "error");
    } finally { setBusy(false); }
  };
  const toggle = (set: Dispatch<SetStateAction<Set<string>>>, k: string) =>
    set((s) => { const x = new Set(s); if (x.has(k)) x.delete(k); else x.add(k); return x; });

  const mine = (lib: string, c: string) => !filter || (lib === filter.lib && c === filter.cell);
  const cells = (status?.cells ?? []).filter((c) => mine(c.lib, c.cell));
  // a library with no cell: its edits cancel out, and its log still holds them, which the commit hook refuses all the same
  const unsaved = (status?.unsaved ?? []).flatMap((u) => {
    const cs = u.cells.filter((c) => mine(u.lib, c));
    return cs.length || (!u.cells.length && (!filter || filter.lib === u.lib)) ? [{ lib: u.lib, cells: cs }] : [];
  });
  const stale = (status?.stale ?? []).filter((x) => !filter || filter.lib === x.lib);
  const staleNotes = stale.map((x) => (
    <div key={x.lib} className="note warnnote gitstale">
      <div className="row"><AlertTriangle size={13} />
        <span className="grow">{x.cells.length ? t("history:stale", { lib: x.lib, cells: x.cells.join(", ") }) : t("history:stale.lib", { lib: x.lib })}</span>
      </div>
      <button className="ghost" disabled={busy} onClick={() => reload(x.lib)}><RefreshCw size={13} /> {t("history:stale.reload")}</button>
    </div>
  ));
  const conflicted = status?.conflicted ?? [];
  const setupMissing = status?.setup ? (["merge", "diff", "hook"] as const).filter((k) => !status.setup![k]) : [];
  const ignoreMissing = status?.setup?.gitignore ?? [];    // .gitignore lines of Undine's output it lacks: set up adds them
  // the open layouts whose unsaved edits keep the first commit from being made (all of them: the button has no filter)
  const pending = status?.unsaved?.length ? layoutsText(status.unsaved) : "";
  const hookNote = hook?.project === project && setupMissing.includes("hook") ? hook.note : null;
  const stamp = `${tick}.${n}`;

  const commitRow = (c: GitLogCommit) => {
    const k = `${project}|${c.sha}`;
    const open = openCommits.has(k);
    const merge = c.parents.length > 1;
    const names = c.cells.map((x) => x.cell);
    return (
      <div key={k} className="citem gitcommit">
        <button type="button" className="row gitrow" aria-expanded={open} title={t("history:diff.toggle")} onClick={() => toggle(setOpenCommits, k)}>
          {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
          {merge && <span className="gitmergeicon" title={t("history:commit.merge")}><GitMerge size={13} /></span>}
          <span className="ellip grow">{c.subject}</span>
          {c.refs.map((r) => <span key={r} className="tag mono">{r}</span>)}
        </button>
        <div className="row small dim gitmeta">
          <span className="mono" title={c.sha}>{c.short}</span>
          <span className="ellip gitauthor" title={c.email}>{c.author}</span>
          <span title={c.date}>{relativeDate(c.date, locale)}</span>
          <span className="mono ellip gitcells" title={c.cells.map((x) => `${x.lib}/${x.cell}`).join("\n")}>
            {names.slice(0, 3).join(", ")}{names.length > 3 ? ` +${names.length - 3}` : ""}
          </span>
          {c.other > 0 && <span>{t("history:commit.otherFiles", { count: c.other })}</span>}
        </div>
        {open && <div className="gitopen">
          {c.body && <div className="gitbody small">{c.body}</div>}
          <RevDiff project={project} rev={c.sha} lib={filter?.lib} cell={filter?.cell} stamp="" cache={commitDiffs.current} onOpenView={onOpenView} />
          {merge && <MergeCheck project={project} sha={c.sha} onOpenView={onOpenView} />}
        </div>}
      </div>
    );
  };

  return (
    <Cells.Provider value={{ views: cellViews, want: wantCells }}>
    <div className="drawer history">
      <Splitter pane="drawer" className="drawer-edge" />
      <div className="drawerhead">
        <GitBranch size={16} className="dim" />
        <b className="grow">{t("history:title")}</b>
        {repo && <button className="icon" title={t("history:refresh")} onClick={refresh}><RefreshCw size={14} /></button>}
        <button className="icon" title={t("history:close")} onClick={onClose}><X size={15} /></button>
      </div>
      <div className="drawerbody">
        {preview ? (<>
          {/* a changeset's working copy is its branch: what it changes is in its review, never set up here */}
          {status?.changeset && status.branch ? <>
            <div className="githead">
              <div className="row">
                <GitBranch size={14} className="dim" />
                <b className="mono ellip" title={status.branch}>{status.branch}</b>
              </div>
              <div className="small">{t("history:preview.changeset", { changeset: status.changeset, base: status.base?.slice(0, 7) ?? "",
                project: project.split("~")[0] })}</div>
            </div>
            {onReview && <button className="ghost" onClick={() => onReview(status.changeset!)}>
              <GitPullRequestArrow size={13} /> {t("history:preview.review")}
            </button>}
          </> : <div className="note">{t("history:preview", { project: project.split("~")[0] })}</div>}
          {staleNotes}
        </>) : !status ? (
          error ? <div className="note err">
            <div>{t("history:unavailable")}</div>
            <div className="small">{resolveText(error)}</div>
            <button className="ghost" onClick={onRefresh}><RefreshCw size={13} /> {t("history:refresh")}</button>
          </div> : <div className="empty">{t("common:state.loading")}</div>
        ) : !status.repo ? (<>
          {staleNotes}
          <div className="note">{t("history:setup.intro")}</div>
          <div className="small gittext">{t("history:setup.what")}</div>
          <button className="primary" disabled={busy} onClick={setup}><GitBranch size={13} /> {t("history:setup.button")}</button>
        </>) : (<>
          <div className="githead">
            <div className="row">
              <GitBranch size={14} className="dim" />
              <b className="mono ellip">{status.branch ?? t("history:detached")}</b>
            </div>
            {status.head ? (
              <div className="row small">
                <span className="mono dim" title={status.head.sha}>{status.head.short}</span>
                <span className="ellip grow" title={status.head.subject}>{status.head.subject}</span>
                <span className="dim" title={status.head.date}>{relativeDate(status.head.date, locale)}</span>
              </div>
            ) : <div className="small dim">{!status.setup?.merge ? t("history:noCommit.notSetUp")
              : filter ? t("history:noCommit.filter", { cell: `${filter.lib}/${filter.cell}` }) : t("history:noCommit")}</div>}
          </div>
          {!!(setupMissing.length || ignoreMissing.length) && (
            <div className="note warnnote gitsetup">
              <div className="row">
                <span className="grow">
                  {!!setupMissing.length && <div>{t("history:setup.incomplete", { missing: joinText(setupMissing.map((k) => msg(`history:setup.${k}`)), ", ") })}</div>}
                  {!!ignoreMissing.length && <div>{t("history:setup.gitignore", { lines: ignoreMissing.join(", ") })}</div>}
                </span>
                {/* a hook git-setup would not install: setting up again changes nothing until the hook is taken care of */}
                {!(hookNote && setupMissing.length === 1 && !ignoreMissing.length) && <button className="ghost" disabled={busy} onClick={setup}>{t("history:setup.again")}</button>}
              </div>
              {hookNote && <div className="small gitnote">{t("history:setup.hookNote", { why: hookNote })}</div>}
            </div>
          )}
          {staleNotes}
          {(status.operation || conflicted.length > 0) && (
            <div className="note warnnote">
              {status.operation && <div className="row"><AlertTriangle size={13} /> <b>{t(`history:operation.${status.operation}`)}</b></div>}
              <InProgress project={project} status={status} onOpenView={onOpenView} />
            </div>
          )}
          {cell && (
            <label className="row small gitonly" title={t("history:onlyHint")}>
              <input type="checkbox" checked={only} onChange={(e) => setOnly(e.target.checked)} />
              {t("history:only", { cell: `${cell.lib}/${cell.cell}` })}
            </label>
          )}

          <div className="section">{t("history:uncommitted")}</div>
          {!cells.length && !unsaved.length && <div className="dim small">{t("history:uncommitted.none")}</div>}
          {cells.map((c) => {
            const k = `${c.lib}/${c.cell}`;
            const open = openCells.has(`${project}|${k}`);
            return (
              <div key={`${project}|${k}`} className="citem gitcellrow">
                <div className="row">
                  <button type="button" className="row gitrow grow" aria-expanded={open} title={t("history:diff.toggle")}
                    onClick={() => toggle(setOpenCells, `${project}|${k}`)}>
                    {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                    <span className={`gitmark gm-${cellKind(c)}`} />
                    <b className="mono ellip">{k}</b>
                  </button>
                  <OpenButtons change={c} worktree onOpenView={onOpenView} />
                </div>
                <div className="row gitchips">
                  {c.views.map((v) => (
                    <span key={v.path} className={`gitchip gs-${changeKind(v.status)}`} title={v.path}>
                      {resolveText(viewName(v.view))} · {resolveText(statusName(v.status))}
                    </span>
                  ))}
                </div>
                {open && <RevDiff project={project} rev="worktree" lib={c.lib} cell={c.cell}
                  stamp={`${stamp}.${c.views.map((v) => `${v.path}:${v.status}:${v.stamp ?? seq}`).join("|")}`} onOpenView={onOpenView} />}
              </div>
            );
          })}
          {unsaved.filter((u) => !stale.some((x) => x.lib === u.lib)).map((u) => (
            <div key={u.lib} className="note warnnote small">
              <span className="gitmark gm-unsaved" />{" "}
              {u.cells.length ? t("history:unsaved", { lib: u.lib, cells: u.cells.join(", ") }) : t("history:unsaved.cancelled", { lib: u.lib })}
            </div>
          ))}
          {!filter && !!status.other?.length && <OtherFiles files={status.other} />}

          <div className="section">{t("history:commits")}</div>
          {logErr && <div className="note err">{resolveText(logErr)}</div>}
          {!shownLog ? (!logErr && <div className="dim small">{t("common:state.loading")}</div>)
            : !shownLog.commits.length ? <div className="dim small">{filter ? t("history:commits.noneFor", { cell: `${filter.lib}/${filter.cell}` }) : t("history:commits.none")}</div>
              : shownLog.commits.map(commitRow)}
          {!status.head && !filter && status.setup?.merge && (
            <div className="gitfirst">
              <button className="ghost" disabled={busy || !!pending} onClick={firstCommit}>
                <GitCommitHorizontal size={13} /> {t("history:firstCommit.button")}
              </button>
              {/* the server refuses a log with unsaved layout edits (the hook would): saved first, by the person */}
              {pending && <div className="small warn">{t("history:firstCommit.saveFirst", { layouts: pending })}</div>}
              <div className="small gittext">{t("history:firstCommit.hint")}</div>
            </div>
          )}
          {shownLog?.more && (
            <button className="ghost full" disabled={moreBusy} onClick={loadMore}>
              {moreBusy ? t("common:state.loading") : t("history:commits.more")}
            </button>
          )}
        </>)}
      </div>
    </div>
    </Cells.Provider>
  );
}
