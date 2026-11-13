import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Bot, Box, ChevronDown, ChevronRight, CircuitBoard, Cpu, Download, FileCode2, FileUp, FolderPlus, GitBranch, GitPullRequestArrow, LayoutDashboard, Library, Loader2, Lock, PackagePlus, Plus, RefreshCw, Search, Shapes, ShieldAlert, ShieldCheck, Trash2, X } from "lucide-react";
import { tabViewOf, type ChangeItem } from "./panels/changeItems";
import type { GitStatus, LiveWriter, SymbolAffectedView, Writer } from "./api";
import { api, ChatImage, CellInfo, ChangesetMerge, DepsStatus, LibraryInfo, PdkInfo, ProbeResult, ProjectInfo, ToolRecord, ViewKind } from "./api";
import { incompatibleOf, problemsOf, type DepAdd, type ResolvedDep, type TrustCounts } from "./api";
import { LayoutEditor } from "./editors/LayoutEditor";
import { LanguageMenu } from "./LanguageMenu";
import { PdkFetchDialog, pdkStateText } from "./PdkFetchDialog";
import { TrustDialog } from "./TrustDialog";
import { Logo } from "./Logo";
import { ChatPanel, Selection, WorkTarget } from "./panels/ChatPanel";
import { ChangesPanel } from "./panels/ChangesPanel";
import { closingText } from "./panels/closing";
import { GitCellMark, GitGroupMark, HistoryPanel } from "./panels/HistoryPanel";
import { cellGit, cellIndex, groupGit, libraryCount, libraryGit } from "./panels/gitItems";
import { SchematicView } from "./editors/SchematicView";
import { RtlView } from "./editors/RtlView";
import { SymbolEditor } from "./editors/SymbolEditor";
import { CellOverview } from "./editors/CellOverview";
import { SymbolCheckPanel } from "./panels/SymbolCheckPanel";
import { ask, confirmDialog, DialogHost, validName } from "./dialog";
import { RowSplitter, Splitter, TabStrip } from "./layout";
import { apiErrorText, joinText, msg, resolveText, useI18n, type UiText } from "./i18n";

type TabView = ViewKind | "cell";      // "cell": the cell page (its intent on top, its views under it)

interface Tab {
  id: string;
  lib: string;
  cell: string;
  view: TabView;
  dirty?: boolean;
}

interface Toast {
  id: number;
  msg: UiText;
  kind: "error" | "info";
}

const VIEW_ICON = { schematic: CircuitBoard, symbol: Shapes, rtl: FileCode2, layout: Box, cell: LayoutDashboard } as const;
type ImportView = "schematic" | "symbol" | "layout";
const VIEW_SHORT = { schematic: "sch", symbol: "sym", rtl: "rtl", layout: "lay" } as const;

const dependencyState = (state: string): UiText => {
  const keys = { ok: "ok", unlocked: "unlocked", missing: "missing", "other version": "otherVersion", modified: "modified", linked: "linked", installed: "installed", leftover: "leftover", removed: "removed" } as const;
  const key = keys[state as keyof typeof keys];
  return key ? msg(`app:dependency.state.${key}`) : state;
};

/** A tool's number and origin as the lock or this server has it ("0.28.17 from pixi"); a banner with no number in it
 * quoted, a format 1 lock's tool without its origin. */
const toolText = (r: ToolRecord | null | undefined, none: UiText): UiText => {
  if (!r) return none;
  const version = r.version ?? (r.text ? `'${r.text}'` : "?");
  return r.from ? msg("app:lock.tool", { version, origin: r.from }) : version;
};
/** What the trust rule found in a dependency, in words ("2 SPICE lines, 1 Tcl attribute"); null when nothing. */
const flagText = (f: TrustCounts | null | undefined): UiText | null => {
  if (!f) return null;
  const parts = (["spice", "rtl", "tcl"] as const).filter((k) => f[k] > 0).map((k) => msg(`app:trust.${k}`, { count: f[k] }));
  return parts.length ? joinText(parts, ", ") : null;
};
const short = (v: string | null | undefined) => v && /^[0-9a-f]{40,64}$/.test(v) ? v.slice(0, 12) : v ?? "-";

/** A refusal that lists what it is about (docs/design/packages.md: every problem at once, nothing changed), as the
 * dialog shows it; null for any other error. */
interface Refusal { title: UiText; intro: UiText; items: { text: string; sub: string[] }[]; fix?: UiText }
function listedRefusal(e: unknown, pdk?: string): Refusal | null {
  const problems = problemsOf(e);
  if (problems?.length) {
    return { title: msg("app:refusal.resolution"), intro: msg("app:refusal.resolutionIntro", { count: problems.length }),
      items: problems.map((p) => ({ text: p.message, sub: p.chains })) };
  }
  const packages = incompatibleOf(e);
  if (packages?.length) {
    return { title: msg("app:refusal.pdk"), intro: msg("app:refusal.pdkIntro", { pdk: pdk ?? "", count: packages.length }),
      items: packages.map((x) => ({ text: `${x.chain || x.name}: ${x.reason}`, sub: [] })), fix: msg("app:refusal.pdkFix") };
  }
  return null;
}
/** The first line of an error's words: what a form shows while the list is in the refusal dialog. */
const firstLine = (e: unknown) => resolveText(apiErrorText(e)).split("\n")[0];

function RefusalDialog({ refusal, onClose }: { refusal: Refusal; onClose: () => void }) {
  const { t } = useI18n();
  return (
    <div className="modalback" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={resolveText(refusal.title)}>
        <div className="modalhead">
          <span className="grow">{resolveText(refusal.title)}</span>
          <button className="icon" title={t("common:actions.cancelShortcut")} onClick={onClose}><X size={15} /></button>
        </div>
        <div className="modalbody">
          <div className="dim small">{resolveText(refusal.intro)}</div>
          <ul className="small" data-testid="refusal-list" style={{ margin: 0, paddingLeft: 18 }}>
            {refusal.items.map((x, i) => (
              <li key={i} style={{ whiteSpace: "pre-line", overflowWrap: "anywhere" }}>{x.text}
                {x.sub.map((c) => <div key={c} className="faint mono">{c}</div>)}
              </li>
            ))}
          </ul>
          {refusal.fix && <div className="dim small">{resolveText(refusal.fix)}</div>}
          <div className="faint small">{t("app:refusal.nothingChanged")}</div>
          <div className="modalfoot">
            <button type="button" className="primary" onClick={onClose}>{t("common:actions.ok")}</button>
          </div>
        </div>
      </div>
    </div>
  );
}

// the PDK states that need the person (the badge in the warning colour)
const PDK_TROUBLE = new Set(["missing", "modified", "mismatch", "other version", "error"]);

function hashProject(): string | null {
  const m = location.hash.match(/^#\/p\/([^/?]+)/);
  return m ? decodeURIComponent(m[1]) : null;
}

// Deep link: #/p/<project>/o/<lib>/<cell>/<view>[?panel=...] opens that view.
export function hashOpen(): { lib: string; cell: string; view: TabView } | null {
  const m = location.hash.match(/^#\/p\/[^/]+\/o\/([^/]+)\/([^/]+)\/(schematic|symbol|rtl|layout|cell)/);
  return m ? { lib: decodeURIComponent(m[1]), cell: decodeURIComponent(m[2]), view: m[3] as TabView } : null;
}
export function hashParam(k: string): string | null {
  return new URLSearchParams(location.hash.split("?")[1] ?? "").get(k);
}
export const hashPanel = () => hashParam("panel");

// ---- a changeset adopted or reverted: the files its merge wrote (its "paths", from the project directory) and the open
// tabs that show them, which read them again (server/undine/vcs/history.py places a file the same way)
type FileKind = ViewKind | "intent" | "netlist";
const FILE_KINDS: [FileKind, string][] = [["intent", ".intent.yaml"], ["netlist", ".netlist.yaml"], ["schematic", ".sch"],
  ["symbol", ".sym"], ["layout", ".oas"], ["rtl", ".sv"], ["rtl", ".v"]];
/** A written file: its library (its directory's name when that names one; null: it may be any), its cell (null: a
 * library's log, history/<lib>.jsonl) and what it is; null for a file of no cell. */
interface Written { lib: string | null; cell: string | null; kind: FileKind | "log" }
function writtenFile(path: string, libs: readonly string[]): Written | null {
  const parts = path.split("/");
  const name = parts[parts.length - 1], dir = parts[parts.length - 2] ?? "";
  if (dir === "history" && name.endsWith(".jsonl")) return { lib: name.slice(0, -".jsonl".length), cell: null, kind: "log" };
  for (const [kind, suffix] of FILE_KINDS) {
    if (!name.endsWith(suffix) || name.length === suffix.length) continue;
    let cell = name.slice(0, -suffix.length);
    if (kind === "layout") try { cell = decodeURIComponent(cell); } catch { /* not %-encoded */ }
    return { lib: libs.includes(dir) ? dir : null, cell, kind };
  }
  return null;
}
/** Whether a tab shows a written file: its cell's file of its view or its intent (every view has its intent panel); a
 * schematic also the symbols it places, a layout any layout file (its hierarchy) or its library's log (its journal);
 * the cell page any file of its cell. */
function showsFile(t: Tab, w: Written): boolean {
  const lib = w.lib === null || w.lib === t.lib;
  if (w.kind === "log") return t.view === "layout" && lib;
  if ((t.view === "layout" && w.kind === "layout") || (t.view === "schematic" && w.kind === "symbol")) return true;
  return lib && w.cell === t.cell && (t.view === "cell" || w.kind === t.view || w.kind === "intent");
}
/** A cell page's intent YAML with edits not saved (IntentPanel: its Save is enabled once the text differs). */
function unsavedYaml(pane: HTMLElement | undefined): boolean {
  return [...pane?.querySelectorAll<HTMLTextAreaElement>("textarea.intentyaml") ?? []].some((text) => {
    const save = text.nextElementSibling;
    return !text.disabled && (!(save instanceof HTMLButtonElement) || !save.disabled);
  });
}

export function App() {
  const { t } = useI18n();
  const [projects, setProjects] = useState<ProjectInfo[]>([]);
  const [pdks, setPdks] = useState<PdkInfo[]>([]);
  const [project, setProject] = useState<string | null>(hashProject());
  const [info, setInfo] = useState<(ProjectInfo & { libraries: LibraryInfo[] }) | null>(null);
  const [deps, setDeps] = useState<DepsStatus | null>(null);
  const [pdkMenu, setPdkMenu] = useState(false);         // the PDK badge's menu
  const [pdkFetch, setPdkFetch] = useState(false);       // the PDK Fetch dialog
  const [trustName, setTrustName] = useState<string | null>(null);   // the Trust dialog of that dependency
  const [refusal, setRefusal] = useState<Refusal | null>(null);      // a refusal with its list (resolution, PDK)
  const pdkMenuRef = useRef<HTMLDivElement>(null);
  const [cells, setCells] = useState<Record<string, CellInfo[]>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [deviceFolds, setDeviceFolds] = useState<Set<string>>(new Set());   // libraries whose device cells are shown
  const [filter, setFilter] = useState("");
  const [tabs, setTabs] = useState<Tab[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [toasts, setToasts] = useState<Toast[]>([]);
  // the in-app agent is hidden by default: external agents (MCP, `undine call`) are the main way. The Agent icon just left
  // of Changes in the top bar (always there), `?chat=1` in the URL, or "Request changes" in a review shows it
  // (docs/guide/assistant.md)
  const [showChat, setShowChat] = useState(() => hashParam("chat") === "1");
  const [selection, setSelection] = useState<Selection | null>(null);
  // Review: one changeset's changed objects, checks and decision docked beside the drawing and the conversation
  const [reviewId, setReviewId] = useState<string | null>(() => { const c = hashParam("changes"); return c && c !== "1" ? c : null; });
  const [focusedItem, setFocusedItem] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0); // the working copy changed under all views: they reload
  const [tabNonce, setTabNonce] = useState<Record<string, number>>({}); // ... under some views only
  const [pageTick, setPageTick] = useState<Record<string, number>>({}); // a cell page reads again (its editors kept open)
  // tabs whose files a merge wrote while they held unsaved text: kept as they are, with a notice
  const [merged, setMerged] = useState<Record<string, { project: string; action: "apply" | "revert"; files: string[] }>>({});
  const panes = useRef(new Map<string, HTMLDivElement>());
  const tabsNow = useRef<Tab[]>([]);
  tabsNow.current = tabs;
  // the assistant's running turn as this screen sees it: the review opens once, the drawing follows unless paused
  const work = useRef<{ turn: string; opened: boolean; auto: string | null; last: WorkTarget | null } | null>(null);
  const [followPaused, setFollowPaused] = useState(false);
  const [turnRunning, setTurnRunning] = useState(false);
  const [chatFocus, setChatFocus] = useState(0);
  const [diffPreview, setDiffPreview] = useState<string | null>(null); // review "Diff" mode: the proposal with what differs drawn over it
  const keepTabs = useRef(false); // switching confirmed <-> proposal keeps the open views
  const captureRef = useRef<(() => ChatImage | null) | null>(null);
  const [showChanges, setShowChanges] = useState(() => hashParam("changes") === "1");
  const [openIds, setOpenIds] = useState<string[]>([]);        // the open changesets (the Changes badge)
  const [probe, setProbe] = useState<{ lib: string; cell: string; res: ProbeResult } | null>(null);
  const [showHistory, setShowHistory] = useState(() => hashParam("history") === "1");

  const toast = useCallback((msg: UiText, kind: "error" | "info" = "info") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t.slice(-3), { id, msg, kind }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === "error" ? 6000 : 3000);
  }, []);

  useEffect(() => {
    api.projects().then((ps) => {
      setProjects(ps);
      if (!project && ps.length) setProject(ps[0].name);
    }).catch((e) => toast(msg("app:error.server", { detail: e.message }), "error"));
    api.pdks().then(setPdks).catch(() => {});
  }, []); // eslint-disable-line

  // a project pinned to a PDK whose PCell code Undine no longer runs: move it to the supported version
  const movePdk = async () => {
    if (!project || !info?.pdk_unsupported) return;
    const to = info.pdk_upgrade;
    if (!to) { await ask({ title: t("app:pdk.unsupportedTitle"), message: info.pdk_unsupported, fields: [], submitLabel: "OK" }); return; }
    if (!await confirmDialog({ title: t("app:pdk.unsupportedTitle"), message: t("app:pdk.moveMessage", { from: info.pdk?.id ?? "", to }), okLabel: t("app:pdk.move", { to }) })) return;
    try {
      await api.setPdk(project, to);
      await loadProject(project);
      await loadDeps(project);
      toast(t("app:pdk.moved", { to }));
    } catch (e) {
      // locked packages that do not accept that PDK: each with its chain and why, in a dialog
      const listed = listedRefusal(e, to);
      if (listed) setRefusal(listed);
      else toast((e as Error).message, "error");
    }
  };

  const loadProject = useCallback(async (name: string) => {
    const i = await api.project(name);
    setInfo(i);
    return i;
  }, []);

  useEffect(() => {
    if (!project) return;
    const deep = hashOpen();
    if (!deep) location.hash = `#/p/${encodeURIComponent(project)}`;
    setCells({});
    setExpanded(new Set());
    const keep = keepTabs.current;
    keepTabs.current = false;
    let saved: { tabs: Tab[]; active: string | null } | null = null;
    try {
      saved = JSON.parse(localStorage.getItem(`undine.tabs.${project}`) ?? "null");
    } catch {
      saved = null;
    }
    if (keep) {
      /* same views, other version of the design */
    } else if (deep) {
      const id = `${deep.lib}/${deep.cell}/${deep.view}`;
      setTabs([{ id, ...deep }]);
      setActiveId(id);
    } else {
      setTabs(saved?.tabs.map((t) => ({ ...t, dirty: false })) ?? []);
      setActiveId(saved?.active ?? null);
    }
    loadProject(project).then((i) => {
      const first = i.libraries.find((l) => l.origin === "project");
      if (first) toggleLib(first.name, true);
    }).catch((e) => toast(apiErrorText(e), "error"));
  }, [project]); // eslint-disable-line

  useEffect(() => {
    if (!project) return;
    try {
      localStorage.setItem(`undine.tabs.${project}`, JSON.stringify({ tabs, active: activeId }));
    } catch {
      /* storage unavailable */
    }
  }, [tabs, activeId, project]);

  const loadCells = useCallback(async (lib: string) => {
    if (!project) return [];
    const cs = await api.cells(project, lib);
    setCells((c) => ({ ...c, [lib]: cs }));
    return cs;
  }, [project]);

  // an open tab needs its library's cell list (which views exist: Open schematic, LVS, schematic-driven layout),
  // also when the library is collapsed in the browser, e.g. after a deep link or restored tabs
  useEffect(() => {
    for (const lib of new Set(tabs.map((t) => t.lib))) if (!cells[lib]) loadCells(lib).catch(() => {});
  }, [tabs, cells, loadCells]);

  const toggleDevices = (lib: string) => setDeviceFolds((s) => {
    const n = new Set(s);
    if (n.has(lib)) n.delete(lib); else n.add(lib);
    return n;
  });

  const toggleLib = (lib: string, open?: boolean) => {
    setExpanded((s) => {
      const n = new Set(s);
      const want = open ?? !n.has(lib);
      if (want) n.add(lib);
      else n.delete(lib);
      return n;
    });
    if ((open ?? !expanded.has(lib)) && !cells[lib]) loadCells(lib).catch((e) => toast(apiErrorText(e), "error"));
  };

  const openView = useCallback((lib: string, cell: string, view: TabView) => {
    const id = `${lib}/${cell}/${view}`;
    setTabs((t) => (t.some((x) => x.id === id) ? t : [...t, { id, lib, cell, view }]));
    setActiveId(id);
  }, []);
  // editors ask for a cell's page (IntentPanel's openCellPage) without knowing about tabs
  useEffect(() => {
    const open = (e: Event) => { const d = (e as CustomEvent<{ lib: string; cell: string }>).detail; openView(d.lib, d.cell, "cell"); };
    window.addEventListener("undine:open-cell", open);
    return () => window.removeEventListener("undine:open-cell", open);
  }, [openView]);

  const closeTab = async (id: string) => {
    const t = tabs.find((x) => x.id === id);
    if (t?.dirty && !(await confirmDialog({ title: msg("app:tabs.close"), okLabel: msg("common:actions.close"), danger: true,
      message: msg("app:tabs.unsaved", { lib: t.lib, cell: t.cell }) }))) return;
    const i = tabs.findIndex((x) => x.id === id);
    const n = tabs.filter((x) => x.id !== id);
    setTabs(n);
    if (activeId === id) setActiveId(n[Math.min(i, n.length - 1)]?.id ?? null);
  };

  // Symbol reference from a schematic ("lib/cell.sym" or "cell.sym") -> open its schematic or symbol.
  const openSymbol = useCallback(async (fromLib: string, symbol: string) => {
    const parts = symbol.replace(/\.(sym|sch)$/, "").split("/");
    const cellName = parts[parts.length - 1];
    const libs = info?.libraries.map((l) => l.name) ?? [];
    const candidates = parts.length > 1 && libs.includes(parts[parts.length - 2])
      ? [parts[parts.length - 2]] : [fromLib, ...libs.filter((l) => l !== fromLib)];
    for (const lib of candidates) {
      const cs = cells[lib] ?? await loadCells(lib).catch(() => [] as CellInfo[]);
      const c = cs.find((x) => x.name === cellName);
      if (c) {
        const v = c.views.includes("schematic") ? "schematic" : c.views.includes("symbol") ? "symbol" : "layout";
        openView(lib, cellName, v);
        return;
      }
    }
    toast(msg("app:symbol.missing", { symbol }), "info");
  }, [info, cells, loadCells, openView, toast]);

  // changesets belong to the real project; `<project>~<id>` is the working copy of one
  const realProject = project ? project.split("~")[0] : null;
  const inPreview = !!project && project.includes("~");
  const refreshChanges = useCallback(() => {
    if (!realProject) return;
    api.changesets(realProject).then((l) => setOpenIds(l.filter((c) => c.status === "open").map((c) => c.id))).catch(() => {});
  }, [realProject]);
  useEffect(() => { refreshChanges(); }, [refreshChanges, showChanges]);

  // ---- git: one status for the top bar, the library browser and the history panel (server/undine/vcs/gitmerge.py),
  // read every 10 s while the page is seen, and soon after a save or a reload of the design
  const [git, setGit] = useState<GitStatus | null>(null);
  const [gitError, setGitError] = useState<UiText | null>(null);
  const [gitTick, setGitTick] = useState(0);     // the files changed: the history panel reads its open diffs again
  const gitOf = useRef<string | null>(null);
  const refreshGit = useCallback(() => {
    const p = project;
    if (!p) return;
    api.gitStatus(p).then((s) => { if (gitOf.current === p) { setGit(s); setGitError(null); } })
      .catch((e) => { if (gitOf.current === p) { setGit(null); setGitError(apiErrorText(e)); } });
  }, [project]);
  useEffect(() => {
    gitOf.current = project;
    setGit(null);
    setGitError(null);
    if (!project) return;
    refreshGit();
    const seen = () => { if (!document.hidden) refreshGit(); };
    const t = setInterval(seen, 10000);
    document.addEventListener("visibilitychange", seen);
    return () => { clearInterval(t); document.removeEventListener("visibilitychange", seen); };
  }, [project, refreshGit]);
  useEffect(() => { if (showHistory) refreshGit(); }, [showHistory]); // eslint-disable-line react-hooks/exhaustive-deps
  // a layout the server read again (git changed its files under it, without unsaved edits): its open tabs draw anew,
  // or they would mix the tiles of the old files with those of the new ones
  const layoutReads = useRef<Record<string, number>>({});
  useEffect(() => {
    const now = git?.reads ?? {}, before = layoutReads.current;
    layoutReads.current = now;
    const moved = Object.keys(now).filter((lib) => before[lib] !== undefined && now[lib] !== before[lib]);
    if (!moved.length) return;
    setTabNonce((n) => ({ ...n, ...Object.fromEntries(tabs.filter((x) => moved.includes(x.lib) && x.view === "layout")
      .map((x) => [x.id, (n[x.id] ?? 0) + 1])) }));
  }, [git]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (reloadNonce) { refreshGit(); setGitTick((n) => n + 1); } }, [reloadNonce]); // eslint-disable-line react-hooks/exhaustive-deps
  // an editor's unsaved state changed (an edit, a save): the layout's unsaved edits and the files git sees did too
  const gitSoonTimer = useRef<ReturnType<typeof setTimeout>>();
  const gitSoon = useCallback((saved: boolean) => {
    clearTimeout(gitSoonTimer.current);
    gitSoonTimer.current = setTimeout(() => { refreshGit(); if (saved) setGitTick((n) => n + 1); }, 500);
  }, [refreshGit]);
  useEffect(() => () => clearTimeout(gitSoonTimer.current), []);
  const gitCells = useMemo(() => cellIndex(git), [git]);
  const gitLibs = useMemo(() => libraryGit(git), [git]);
  const gitStale = [...gitLibs].filter(([, g]) => g.stale).map(([lib]) => lib);
  const gitUnsavedLibs = [...gitLibs].filter(([, g]) => g.unsaved).map(([lib]) => lib);
  // git changed the files under the library browser (a checkout, a merge, a cell added or deleted outside Undine): the
  // cell lists read so far are read again
  const gitFiles = useMemo(() => !git?.repo ? null : JSON.stringify([git.head?.sha ?? null,
    (git.cells ?? []).flatMap((c) => c.views.filter((v) => v.status !== "modified").map((v) => `${c.lib}/${c.cell}/${v.view}/${v.status}`))]), [git]);
  const gitFilesSeen = useRef<string | null>(null);
  useEffect(() => {
    const before = gitFilesSeen.current;
    gitFilesSeen.current = gitFiles;
    if (before !== null && gitFiles !== null && before !== gitFiles) Object.keys(cells).forEach((l) => loadCells(l).catch(() => {}));
  }, [gitFiles]); // eslint-disable-line react-hooks/exhaustive-deps
  // HEAD moved (a checkout, a pull) or project.json changed: the libraries may be others, so the project is read again
  const headSeen = useRef<string | null>(null);
  useEffect(() => {
    const now = git?.repo ? `${git.head?.sha ?? ""}|${(git.other ?? []).some((o) => o.path === "project.json")}` : null;
    const before = headSeen.current;
    headSeen.current = now;
    if (before !== null && now !== null && before !== now && realProject) loadProject(realProject).catch(() => {});
  }, [git]); // eslint-disable-line react-hooks/exhaustive-deps
  // the history panel opens a changed cell's views only when they are there now: the cell lists say so
  const cellViews = useCallback((lib: string, cell: string) => {
    if (info && !info.libraries.some((l) => l.name === lib)) return null;
    const cs = cells[lib];
    return cs ? cs.find((c) => c.name === cell)?.views ?? null : undefined;
  }, [info, cells]);
  const cellsAsked = useRef(new Set<string>());
  const wantCells = useCallback((lib: string) => {
    if (cells[lib] || cellsAsked.current.has(lib)) return;
    cellsAsked.current.add(lib);
    loadCells(lib).catch(() => {}).finally(() => cellsAsked.current.delete(lib));
  }, [cells, loadCells]);
  // a library's layout was read again from its files (its unsaved edits dropped): its layout views show the files
  const layoutReloaded = (lib: string) => {
    const hit = (x: Tab) => x.lib === lib && x.view === "layout";
    setTabs((ts) => ts.map((x) => (hit(x) ? { ...x, dirty: false } : x)));
    setTabNonce((n) => ({ ...n, ...Object.fromEntries(tabs.filter(hit).map((x) => [x.id, (n[x.id] ?? 0) + 1])) }));
    if (cells[lib]) loadCells(lib).catch(() => {});      // the files may have other cells than the edits had
    refreshGit();
    setGitTick((n) => n + 1);
  };

  // publish the GUI selection to the server so that agents / MCP clients can resolve "this transistor"
  useEffect(() => {
    if (!project || !selection) return;
    const t = setTimeout(() => {
      api.revision(project, selection.lib, selection.cell)
        .then((r) => api.pushSelection(project, { lib: selection.lib, cell: selection.cell, view: selection.view, revision: r.revision, selection: selection.items }))
        .catch(() => {});
    }, 250);
    return () => clearTimeout(t);
  }, [selection, project]);

  // Cross-probing: a selection in the schematic or the layout of a cell is translated by the server
  // (instance <-> instance, net -> instances) and shown in every open view of that cell.
  const doProbe = useCallback(async (lib: string, cell: string, q: { inst?: string; net?: string } | null) => {
    if (!project) return;
    if (!q) { setProbe(null); return; }
    try {
      setProbe({ lib, cell, res: await api.probe(project, lib, cell, q) });
    } catch {
      setProbe(null); // no schematic/layout pair for this cell
    }
  }, [project]);

  // ---- review: the confirmed / proposed version in the same views, and a changed object -> the drawing
  const pendingProbe = useRef<{ lib: string; cell: string; inst: string } | null>(null);
  useEffect(() => {
    const p = pendingProbe.current;
    if (!p) return;
    pendingProbe.current = null;
    doProbe(p.lib, p.cell, { inst: p.inst });
  }, [project, doProbe]);

  const switchProject = (name: string | null) => {
    if (!name || name === project) return false;
    keepTabs.current = true;
    setProject(name);
    return true;
  };
  const focusItem = (it: ChangeItem, preview: string) => {
    setFocusedItem(it.key);
    // an added object only exists in the proposal, a removed one only in the confirmed design
    const want = it.kind === "add" ? preview : it.kind === "del" ? realProject : project;
    const switched = switchProject(want);
    if (!it.cell) { setProbe(null); return; }        // a library made: no view of a cell to show
    // an intent entry or a flow configuration has no view tab of its own: the cell page shows both
    const tabView = tabViewOf(it.view);
    openView(it.lib, it.cell, tabView);
    if (tabView === "cell") { setProbe(null); return; }
    if (!it.objectId) { setProbe(null); return; }
    if (switched) pendingProbe.current = { lib: it.lib, cell: it.cell, inst: it.objectId };
    else doProbe(it.lib, it.cell, { inst: it.objectId });
  };
  const onTurnWork = (turn: string, cs: string, targets: WorkTarget[]) => {
    if (!realProject) return;
    if (work.current?.turn !== turn) {
      work.current = { turn, opened: false, auto: null, last: null };
      setFollowPaused(false);
    }
    setTurnRunning(true);
    const w = work.current;
    const preview = `${realProject}~${cs}`;
    let switched = false;
    if (!w.opened) {               // the first committed change: show the proposal and its review, once
      w.opened = true;
      setReviewId(cs);
      setShowChanges(false);
      setDiffPreview(null);
      switched = switchProject(preview);
    }
    if (targets.length) w.last = targets[targets.length - 1];
    // only the views of the cells that changed reload (all of them when that is not known)
    const hit = (t: Tab) => !targets.length || targets.some((x) => x.lib === t.lib && x.cell === t.cell);
    setTabNonce((n) => Object.fromEntries([...Object.entries(n), ...tabs.filter(hit).map((t) => [t.id, (n[t.id] ?? 0) + 1])]));
    if (!switched && project === preview) new Set(targets.map((x) => x.lib)).forEach((l) => loadCells(l).catch(() => {}));
    refreshChanges();
    if (!followPausedRef.current && w.last && (project === preview || switched)) {
      const id = `${w.last.lib}/${w.last.cell}/${w.last.view}`;
      w.auto = id;
      openView(w.last.lib, w.last.cell, w.last.view);
    }
  };
  const followFit = (tabId: string) => turnRunning && !followPaused && work.current?.auto === tabId;
  const followPausedRef = useRef(false);
  followPausedRef.current = followPaused;
  const activateTab = (id: string) => {        // a person picks a view: following pauses (not the assistant)
    setActiveId(id);
    if (work.current && work.current.auto && work.current.auto !== id) setFollowPaused(true);
  };
  const refreshAffected = (source: string, affected: SymbolAffectedView[]) => setTabNonce((current) => {
    const next = { ...current };
    for (const tab of tabs) {
      if (tab.id !== source && affected.some((changed) => changed.lib === tab.lib && changed.cell === tab.cell && changed.view === tab.view)) {
        next[tab.id] = (current[tab.id] ?? 0) + 1;
      }
    }
    return next;
  });
  const resumeFollow = () => {
    setFollowPaused(false);
    const t = work.current?.last;
    if (t) { work.current!.auto = `${t.lib}/${t.cell}/${t.view}`; openView(t.lib, t.cell, t.view); }
  };

  // ---- agents working from outside this screen (MCP clients, the assistant on another screen): notice and follow
  const [liveWriters, setLiveWriters] = useState<LiveWriter[]>([]);
  const [dismissed, setDismissed] = useState<Set<string>>(new Set());
  useEffect(() => {
    if (!realProject) return;
    let gone = false;
    const tick = () => api.live(realProject).then((l) => { if (!gone) setLiveWriters(l); }).catch(() => {});
    tick();
    const t = setInterval(() => { if (!document.hidden) tick(); }, 3000);
    return () => { gone = true; clearInterval(t); };
  }, [realProject]);
  // who writes which changeset, and (writer null) the changesets being adopted, discarded or reverted on the server
  const writing = useMemo(() => liveWriters.filter((w): w is LiveWriter & { writer: Writer } => !!w.writer), [liveWriters]);
  const closings = liveWriters.filter((w) => w.closing);
  const ownTurn = (owner: string) => turnRunning && !!work.current && owner === `turn:${work.current.turn}`;
  const others = writing.filter((w) => !ownTurn(w.writer.owner) && w.id !== reviewId && !dismissed.has(w.id));
  const watch = (w: LiveWriter & { writer: Writer }) => {
    work.current = { turn: `ext:${w.id}:${w.writer.owner}`, opened: false, auto: null, last: null };
    setFollowPaused(false);
    onTurnWork(work.current.turn, w.id, []);
  };
  const writerName = (w: LiveWriter & { writer: Writer }) => w.writer.label ?? msg(w.writer.kind === "mcp" ? "app:agent.external" : "app:agent.writer");
  // An agent starts writing: this screen follows it by itself when it is free (no review open, nothing unsaved), once
  // per changeset; a person who closes that review is not followed again. An agent stops writing and its changeset is
  // still open: it waits for review, and the screen says so until someone opens it.
  const [awaiting, setAwaiting] = useState<{ id: string; title: string; agent: UiText }[]>([]);
  const seenWriters = useRef<Map<string, LiveWriter & { writer: Writer }>>(new Map());
  const autoFollowed = useRef<Set<string>>(new Set());
  useEffect(() => {
    const prev = seenWriters.current;
    const now = new Map(writing.map((w) => [w.id, w]));
    seenWriters.current = now;
    for (const w of writing) {
      if (prev.has(w.id) || autoFollowed.current.has(w.id) || ownTurn(w.writer.owner) || dismissed.has(w.id)) continue;
      autoFollowed.current.add(w.id);
      if (!reviewId && !tabs.some((t) => t.dirty)) watch(w);
    }
    const ended = [...prev.values()].filter((w) => !now.has(w.id) && !w.writer.owner.startsWith("turn:"));
    if (ended.length && realProject) {
      api.changesets(realProject).then((all) => {
        const open = new Set(all.filter((c) => c.status === "open").map((c) => c.id));
        setOpenIds([...open]);
        setAwaiting((a) => [...a.filter((x) => open.has(x.id) && !ended.some((w) => w.id === x.id)),
          ...ended.filter((w) => open.has(w.id)).map((w) => ({ id: w.id, title: w.title, agent: writerName(w) }))]);
      }).catch(() => {});
    } else if (now.size !== prev.size) refreshChanges();
  }, [writing]); // eslint-disable-line react-hooks/exhaustive-deps
  const waiting = awaiting.filter((a) => a.id !== reviewId && !dismissed.has(a.id) && !writing.some((w) => w.id === a.id));
  // a changeset starts or ends being closed: the count of open ones is read again (the badge says "adopting" meanwhile,
  // and counts the open ones being closed no more)
  const closingIds = closings.map((w) => w.id).join(",");
  const lastClosing = useRef("");
  useEffect(() => {
    if (lastClosing.current === closingIds) return;
    lastClosing.current = closingIds;
    refreshChanges();
  }, [closingIds, refreshChanges]);
  // the badge: the open changesets but those /live says are being adopted or discarded; one that closed meanwhile (the
  // list read again before /live) is not taken off a second time
  const openChanges = openIds.filter((id) => !closings.some((w) => w.id === id && w.closing?.action !== "revert")).length;

  // the review being looked at is written by someone else: follow what they commit
  const followFrom = useRef<{ cs: string; last: number } | null>(null);
  const latest = useRef({ onTurnWork, turnRunning });
  latest.current = { onTurnWork, turnRunning };
  useEffect(() => {
    if (!reviewId || !realProject) return;
    let gone = false, timer: ReturnType<typeof setTimeout>;
    followFrom.current = null;
    const tick = async () => {
      let busy = false;
      try {
        const from = followFrom.current?.cs === reviewId ? followFrom.current.last : -1;
        const a = await api.activity(realProject, reviewId, from);
        if (gone) return;
        busy = !!a.writer;
        followFrom.current = { cs: reviewId, last: a.last };
        const inApp = work.current && !work.current.turn.startsWith("ext:") && latest.current.turnRunning ? work.current.turn : null;
        // this screen's own turn is followed by the chat; a flow configuration (view flow) has no view to follow
        const recs = a.records.filter((r) => (!inApp || r.turn !== inApp) && (r.view as string) !== "flow");
        const targets = recs.map((r) => ({ lib: r.lib, cell: r.cell, view: r.view }));
        if (recs.length && a.writer) {
          const key = `ext:${reviewId}:${a.writer.owner}`;
          if (work.current?.turn !== key) { work.current = { turn: key, opened: true, auto: null, last: null }; setFollowPaused(false); }
          latest.current.onTurnWork(key, reviewId, targets);
        } else if (!a.writer && latest.current.turnRunning && work.current?.turn.startsWith(`ext:${reviewId}:`)) {
          // the agent is done (or its lock lapsed), seen once: the views already show what it committed, except what
          // came with this poll. Reloading every view here (and on each later poll) blanked them every few seconds.
          if (recs.length) latest.current.onTurnWork(work.current.turn, reviewId, targets);
          setTurnRunning(false);
        }
      } catch { /* try again */ }
      if (!gone) timer = setTimeout(tick, busy ? 1000 : 4000);
    };
    tick();
    return () => { gone = true; clearTimeout(timer); };
  }, [reviewId, realProject]); // eslint-disable-line

  const closeReview = () => { setReviewId(null); setFocusedItem(null); setDiffPreview(null); };
  // a changeset was adopted or reverted: the merge wrote the real project's files (`paths`; all of them for a server
  // that does not say). The tabs showing one of them read it again, not only layouts; one holding text not saved (an
  // RTL, a cell page's intent YAML) keeps it, with a notice: its save is refused if its file changed. The libraries,
  // the cell lists and the git status are read again. A preview on screen shows none of these files.
  const designChanged = (merge?: ChangesetMerge | null, action: "apply" | "revert" = "apply") => {
    if (!inPreview && realProject) {
      loadProject(realProject).catch(() => {});
      Object.keys(cells).forEach((l) => loadCells(l).catch(() => {}));
      const libs = info?.libraries.map((l) => l.name) ?? [];
      const paths = merge?.paths;
      const written = (t: Tab) => paths ? paths.filter((p) => { const w = writtenFile(p, libs); return !!w && showsFile(t, w); }) : null;
      const hit = tabsNow.current.map((t) => ({ t, files: written(t) })).filter((x) => !x.files || x.files.length);
      const holds = (t: Tab) => (t.view === "rtl" && !!t.dirty) || (t.view === "cell" && unsavedYaml(panes.current.get(t.id)));
      const bump = (n: Record<string, number>, ts: Tab[]) => ({ ...n, ...Object.fromEntries(ts.map((t) => [t.id, (n[t.id] ?? 0) + 1])) });
      const kept = hit.filter((x) => holds(x.t)), read = hit.map((x) => x.t).filter((t) => !holds(t));
      setTabNonce((n) => bump(n, read.filter((t) => t.view !== "cell")));
      setPageTick((n) => bump(n, read.filter((t) => t.view === "cell")));    // its intent editor stays open
      if (kept.length) {
        setMerged((m) => ({ ...m, ...Object.fromEntries(kept.map((x) => [x.t.id, { project: realProject, action, files: x.files ?? [] }])) }));
      }
    }
    refreshGit();
    setGitTick((n) => n + 1);
    refreshChanges();
  };
  const dropMerged = (id: string) => setMerged((m) => Object.fromEntries(Object.entries(m).filter(([k]) => k !== id)));
  // a merge's notice: read the tab's files again, its unsaved text dropped
  const reloadMerged = (t: Tab) => {
    if (t.view === "cell") setPageTick((n) => ({ ...n, [t.id]: (n[t.id] ?? 0) + 1 }));
    else setTabNonce((n) => ({ ...n, [t.id]: (n[t.id] ?? 0) + 1 }));
    dropMerged(t.id);
  };
  // the notice ends with the tab, and with an RTL's unsaved text: clean again, it is read again (with nothing unsaved
  // nothing is lost), since clean may mean its text is the one it loaded before the merge (the edits undone) rather
  // than saved (its file had not changed after all)
  useEffect(() => {
    const clean = tabs.filter((t) => merged[t.id] && t.view === "rtl" && !t.dirty).map((t) => t.id);
    const gone = [...clean, ...Object.keys(merged).filter((id) => !tabs.some((x) => x.id === id))];
    if (clean.length) setTabNonce((n) => ({ ...n, ...Object.fromEntries(clean.map((id) => [id, (n[id] ?? 0) + 1])) }));
    if (gone.length) setMerged((m) => Object.fromEntries(Object.entries(m).filter(([id]) => !gone.includes(id))));
  }, [tabs]); // eslint-disable-line react-hooks/exhaustive-deps
  // a changed file of a changeset (its working copy, or the design once adopted): that version, the views kept
  const openIn = (p: string, lib: string, cell: string, view: TabView) => { switchProject(p); openView(lib, cell, view); };

  const hasView = (lib: string, cell: string, v: ViewKind) => !!cells[lib]?.find((c) => c.name === cell)?.views.includes(v);
  const probeFor = (lib: string, cell: string) => (probe && probe.lib === lib && probe.cell === cell ? probe.res : null);

  const newProject = async () => {
    const def = pdks.find((p) => p.name === "sky130A")?.id ?? pdks[0]?.id ?? "";
    await ask({
      title: msg("app:project.new"), submitLabel: msg("common:actions.create"),
      fields: [
        { name: "name", label: msg("app:name"), placeholder: msg("app:project.example"),
          validate: (v) => projects.some((p) => p.name === v) ? msg("app:project.exists") : validName(v) },
        { name: "pdk", label: msg("app:project.pdk"), type: "select", value: def,
          options: [...pdks.map((p) => ({ value: p.id })), { value: "", label: msg("common:state.none") }] },
      ],
      submit: async (v) => {
        await api.newProject(v.name, v.pdk || null);
        setProjects(await api.projects());
        setProject(v.name);
      },
    });
  };

  const newLibrary = async () => {
    if (!project) return;
    await ask({
      title: msg("app:library.new"), submitLabel: msg("common:actions.create"),
      fields: [{ name: "name", label: msg("app:name"), placeholder: msg("app:library.example"),
        validate: (v) => info?.libraries.some((l) => l.name === v) ? msg("app:library.exists") : validName(v) }],
      submit: async (v) => {
        await api.newLibrary(project, v.name);
        await loadProject(project);
        toggleLib(v.name, true);
      },
    });
  };

  // ---- library dependencies and undine.lock (server/undine/store/deps.py)
  const loadDeps = useCallback(async (name: string) => {
    try { setDeps(await api.deps(name)); } catch { setDeps(null); }
  }, []);
  useEffect(() => { if (project) loadDeps(project); else setDeps(null); }, [project, loadDeps]);

  const addDep = async () => {
    if (!project) return;
    await ask({
      title: msg("app:dependency.add"), submitLabel: msg("common:actions.add"),
      message: msg("app:dependency.description"),
      fields: [
        { name: "name", label: msg("app:library.name"), placeholder: msg("app:dependency.example"),
          validate: (v) => info?.libraries.some((l) => l.name === v) ? msg("app:library.exists") : validName(v) },
        { name: "git", label: msg("app:dependency.repository"), placeholder: "https://github.com/me/analog_ip.git", mono: true, required: true },
        { name: "rev", label: msg("app:dependency.revision"), placeholder: msg("app:dependency.defaultBranch"), mono: true },
        { name: "subdir", label: msg("app:dependency.directory"), placeholder: msg("app:shortcut.top"), mono: true },
        { name: "package", label: msg("app:dependency.package"), hint: msg("app:dependency.packageHint"), mono: true },
      ],
      submit: async (v) => {
        let r: DepAdd;
        try {
          r = await api.depAdd(project, v.name, v.git, v.rev, v.subdir, v.package?.trim());
        } catch (e) {
          // every problem of the resolution at once, in a dialog over this form (which keeps what was typed)
          const listed = listedRefusal(e);
          if (!listed) throw e;
          setRefusal(listed);
          return firstLine(e);
        }
        await loadProject(project);
        await loadDeps(project);
        toggleLib(v.name, true);
        const result = addedText(v.name, r);
        if (result) setTimeout(() => ask({ title: msg("app:dependency.addedTitle", { name: v.name }), message: result, fields: [], submitLabel: msg("common:actions.ok") }), 0);
        else toast(msg("app:dependency.installed", { name: v.name, count: r.cells.length, commit: r.commit.slice(0, 7) }));
      },
    });
  };
  /** What an add brought besides the dependency: the packages it needs, and content not trusted; null when nothing. */
  const addedText = (name: string, r: DepAdd): UiText | null => {
    const brought = (r.resolved ?? []).filter((x) => x.name !== name && x.package);
    const all: ResolvedDep[] = [{ ...r, package: r.package ?? undefined, version: r.version ?? undefined }, ...(r.resolved ?? []).filter((x) => x.name !== name)];
    const untrusted = all.filter((x) => x.trusted === false && flagText(x.flagged));
    if (!brought.length && !untrusted.length) return null;
    const head = r.package ? msg("app:dependency.addedPackage", { name, package: r.package, version: r.version ?? "", commit: r.commit.slice(0, 7) })
      : msg("app:dependency.installed", { name, count: r.cells.length, commit: r.commit.slice(0, 7) });
    const lines: UiText[] = [head];
    if (brought.length) {
      lines.push("", msg("app:dependency.broughtIn"), ...brought.map((x) => msg("app:dependency.broughtInRow", { package: x.package ?? x.name,
        version: x.version ?? "", names: (x.names ?? [x.name]).join(", "), via: (x.via ?? []).join(", ") || name })));
    }
    if (untrusted.length) {
      lines.push("", msg("app:dependency.untrustedList"), ...untrusted.map((x) => msg("app:dependency.untrustedRow", {
        name: x.package ? `${x.package} ${x.version ?? ""}`.trim() : x.name, what: flagText(x.flagged)! })));
    }
    return joinText(lines);
  };

  const updateDep = async (lib: string) => {
    if (!project) return;
    const d = deps?.libraries.find((x) => x.name === lib);
    const done = await ask({
      title: msg("app:dependency.update", { library: lib }), submitLabel: msg("common:actions.update"),
      message: msg("app:dependency.updateDescription", { revision: d?.rev ?? msg("app:dependency.defaultBranch"), commit: (d?.locked ?? "").slice(0, 7) }),
      fields: [{ name: "rev", label: msg("app:dependency.revision"), value: d?.rev ?? "", placeholder: msg("app:dependency.defaultBranch"), mono: true }],
      submit: async (v) => {
        let r: Awaited<ReturnType<typeof api.depUpdate>>;
        try {
          r = await api.depUpdate(project, lib, v.rev);
        } catch (e) {
          const listed = listedRefusal(e);
          if (!listed) throw e;
          setRefusal(listed);
          return firstLine(e);
        }
        const lines: UiText[] = [`${(r.from ?? "-").slice(0, 7)} → ${r.to.slice(0, 7)}`, ""];
        for (const k of ["added", "modified", "removed"] as const) r.changed[k].forEach((f) => lines.push(msg(`app:dependency.${k}`, { file: f })));
        if (!r.changed.added.length && !r.changed.modified.length && !r.changed.removed.length) lines.push(msg("app:dependency.noChanges"));
        // the packages it needs that came, moved or went with it
        const moved = (r.packages_changed ?? []).filter((x) => x.package);
        if (moved.length) {
          lines.push("", msg("app:dependency.packagesChanged"), ...moved.map((x) => {
            const named = { package: x.package!, from: short(x.from), to: short(x.to) };
            return msg(x.from === null ? "app:dependency.packageAdded" : x.to === null ? "app:dependency.packageDropped" : "app:dependency.packageMoved", named);
          }));
        }
        if (r.used_by.length) lines.push("", msg("app:dependency.usedBy"), ...r.used_by);
        await loadProject(project);
        await loadDeps(project);
        await loadCells(lib);
        setTimeout(() => ask({ title: msg("app:dependency.updated", { library: lib }), message: joinText(lines), fields: [], submitLabel: msg("common:actions.ok") }), 0);
      },
    });
    return done;
  };

  const removeDep = async (lib: string) => {
    if (!project) return;
    if (!await confirmDialog({ title: msg("app:dependency.remove", { library: lib }), message: msg("app:dependency.removeDescription", { library: lib }), okLabel: msg("common:actions.remove"), danger: true })) return;
    try {
      const r = await api.depRemove(project, lib);
      await loadProject(project);
      await loadDeps(project);
      // the packages only it needed went with it; one another dependency still places stays
      const also = (r?.removed_packages ?? []).filter((x) => !x.names.includes(lib)).map((x) => x.package ? `${x.package} ${x.version ?? ""}`.trim() : x.names[0]);
      const said = [...(also.length ? [msg("app:dependency.removedPackages", { list: also.join(", ") })] : []),
        ...(r?.kept ?? []).map((k) => msg("app:dependency.keptRow", { name: k.name, list: k.placed_by.join(", ") }))];
      if (said.length) toast(joinText(said));
    } catch (e) { toast(apiErrorText(e), "error"); }
  };

  // after a rescan or a fetch: the PDKs, the project (which PDK it resolves to) and its lock status read again
  const pdksChanged = async (list?: PdkInfo[]) => {
    setPdks(list ?? await api.pdks());
    if (project) { await loadProject(project); await loadDeps(project); }
  };
  const rescanPdks = async () => {
    setPdkMenu(false);
    try {
      const r = await api.rescanPdks();
      await pdksChanged(r.pdks);
      toast(msg("app:pdk.rescanned", { count: r.pdks.length }));
    } catch (e) { toast(apiErrorText(e), "error"); }
  };
  useEffect(() => {
    if (!pdkMenu) return;
    const away = (e: PointerEvent) => { if (!pdkMenuRef.current?.contains(e.target as Node)) setPdkMenu(false); };
    document.addEventListener("pointerdown", away);
    return () => document.removeEventListener("pointerdown", away);
  }, [pdkMenu]);

  const showLock = async () => {
    if (!project || !deps) return;
    const none = msg("common:state.none");
    const libRow = (l: DepsStatus["libraries"][number]): UiText => {
      if (l.kind !== "dependency") return msg("app:dependency.linkRow", { name: l.name, state: dependencyState(l.state), path: l.path ?? "" });
      const row = msg("app:dependency.lockRow", { name: l.name, revision: l.rev ?? msg("app:dependency.defaultBranch"), commit: (l.locked ?? "-").slice(0, 7), state: dependencyState(l.state) });
      // how the lock pins its files (sha: format 1, git archive; tree: format 2, per file) and whether sync needs the network
      const how = l.hash ? [l.hash, ...(l.in_store === false ? [msg("app:dependency.notInStore")] : [])] : [];
      const parts: UiText[] = [how.length ? joinText([row, " (", joinText(how, ", "), ")"], "") : row];
      // a package: its name and version, who needs it (transitive) and whether what it would run is trusted
      if (l.package) parts.push(msg("app:dependency.lockPackage", { package: l.package, version: l.version ?? "" }));
      if (l.direct === false && l.via?.length) parts.push(msg("app:dependency.via", { list: l.via.join(", ") }));
      const flagged = flagText(l.flagged);
      if (flagged && l.trusted !== null && l.trusted !== undefined) parts.push(l.trusted ? msg("app:dependency.trusted") : msg("app:dependency.notTrusted", { what: flagged }));
      return joinText(parts, ", ");
    };
    const pdk = deps.pdk;
    const lines: UiText[] = [
      deps.format ? msg("app:lock.format", { format: deps.format }) : msg("app:lock.none"), "",
      ...deps.libraries.filter((l) => l.kind !== "own").map(libRow),
      msg("app:lock.pdkRow", { requested: pdk.requested ?? none, locked: pdk.locked ?? none, installed: pdk.installed ?? none, state: pdkStateText(pdk.state) }),
      ...deps.tools.map((x) => msg("app:lock.row", { name: x.name, version: toolText(x.locked, msg("app:lock.notRecorded")),
        current: x.ok ? "" : msg("app:lock.current", { version: toolText(x.now, msg("app:notInstalled")) }) })),
      ...(deps.issues.length ? ["", ...deps.issues.map((i) => `! ${i}`)] : ["", msg("app:lock.matches")]),
    ];
    // said, not counted: another tool version or origin, unverified PDK, format 1 (lighter, under the rows)
    const warnings = deps.warnings ?? [], notes = deps.notes ?? [];
    const aside = [...warnings.map((w) => `~ ${w}`), ...notes.map((n) => `note: ${n}`)];
    const depRows = deps.libraries.filter((l) => l.kind === "dependency");
    // lock first while project.json names what the lock does not pin (sync refuses that); sync first for what is not
    // installed as locked
    const lockFirst = depRows.some((l) => l.state === "unlocked");
    const syncFirst = !lockFirst && depRows.some((l) => ["missing", "other version", "modified", "leftover"].includes(l.state));
    const options = [{ value: "lock", label: msg("app:lock.record") }, { value: "sync", label: msg("app:lock.sync") }];
    await ask({
      title: msg("app:lock.title"), submitLabel: msg("common:actions.run"), message: joinText(lines),
      aside: aside.length ? aside.join("\n") : undefined,
      fields: inPreview ? [] : [{ name: "action", label: msg("app:lock.action"), type: "select",
        options: syncFirst ? [...options].reverse() : options }],
      submit: async (v) => {
        if (v.action === "sync") {
          // a refusal (the lock does not pin what project.json names; offline and not in the store) throws: the
          // dialog stays open with the server's message
          const r = await api.depsSync(project);
          toast(r.libraries.length ? joinText(r.libraries.map((x) => msg("app:dependency.syncRow", { name: x.name, state: dependencyState(x.action) })), ", ") : msg("app:lock.empty"));
          // installed all the same: a package the locked PDK does not fit (GET /deps reports it as an issue too)
          for (const x of r.libraries) if (x.warning) toast(msg("app:dependency.syncWarning", { name: x.name, warning: x.warning }), "error");
          await loadProject(project);
          for (const x of r.libraries) if (x.action !== "ok" && x.action !== "removed") await loadCells(x.name);
        } else if (v.action === "lock") {
          let r: Awaited<ReturnType<typeof api.depsLock>>;
          try {
            r = await api.depsLock(project);
          } catch (e) {
            const listed = listedRefusal(e);
            if (!listed) throw e;
            setRefusal(listed);
            return firstLine(e);
          }
          toast(r.resolved?.length ? msg("app:lock.resolved", { list: r.resolved.map((x) => `${x.name} @ ${x.commit.slice(0, 7)}`).join(", ") })
            : msg("app:lock.recorded"));
          if (r.resolved?.length) {
            await loadProject(project);
            for (const x of r.resolved) await loadCells(x.name);
          }
        }
        await loadDeps(project);
      },
    });
  };

  const importInto = async (lib: string) => {
    if (!project) return;
    await ask({
      title: msg("app:import.title", { library: lib }), submitLabel: msg("app:import.action"),
      fields: [
        { name: "view", label: msg("app:import.view"), type: "select", value: "layout", required: true,
          options: (["schematic", "symbol", "layout"] as const).map((view) => ({ value: view, label: msg(`common:view.${view}`) })) },
        { name: "path", label: msg("app:import.path"), value: "~/", mono: true, required: true,
          hint: (values) => msg(`app:import.hint.${values.view as ImportView}`),
          validate: (path, values) => {
            const extension = { layout: /\.(gds|oas|oasis)$/i, schematic: /\.sch$/i, symbol: /\.sym$/i }[values.view as ImportView];
            return extension?.test(path.trim()) ? null : msg(`app:import.format.${values.view as ImportView}`);
          } },
      ],
      submit: async (v) => {
        const r = await api.importFile(project, lib, v.path.trim(), v.view as ViewKind);
        toast(msg("app:import.done", { count: r.new_cells.length, library: lib }));
        await loadProject(project);
        await loadCells(lib);
      },
    });
  };

  const newCell = async (lib: string) => {
    if (!project) return;
    const have = (name: string) => cells[lib]?.find((c) => c.name === name)?.views ?? [];
    await ask({
      title: msg("app:cell.new", { library: lib }), submitLabel: msg("common:actions.create"),
      fields: [
        { name: "name", label: msg("app:cell.name"), placeholder: msg("app:cell.example"), mono: true,
          hint: msg("app:cell.hint"),
          suggestions: (cells[lib] ?? []).filter((c) => c.views.length < 3).map((c) => c.name),
          validate: (v, all) => {
            const bad = validName(v);
            if (bad) return bad;
            const taken = (all.views ?? "").split(",").filter((x) => x && have(v).includes(x as ViewKind));
            return taken.length ? msg("app:cell.existingViews", { name: v, views: joinText(taken.map((view) => msg(`common:view.${view as ViewKind}`)), ", ") }) : null;
          } },
        { name: "views", label: msg("app:cell.views"), type: "checks", value: "schematic", required: true,
          options: [
            { value: "schematic", label: msg("common:view.schematic"), hint: msg("app:cell.schematicHint") },
            { value: "symbol", label: msg("common:view.symbol"), hint: msg("app:cell.symbolHint") },
            { value: "rtl", label: msg("common:view.rtl"), hint: msg("app:cell.rtlHint") },
            { value: "layout", label: msg("common:view.layout"), hint: msg("app:cell.layoutHint") },
          ],
          hint: msg("app:cell.viewsHint") },
      ],
      submit: async (v) => {
        const r = await api.newCellViews(project, lib, v.name, v.views.split(",").filter(Boolean));
        await loadCells(lib);
        for (const view of ["schematic", "layout", "symbol"] as ViewKind[]) {
          if (r.created.includes(view)) { openView(lib, v.name, view); break; }
        }
      },
    });
  };

  const libGroups = useMemo(() => {
    const ls = info?.libraries ?? [];
    return [
      // depth is provenance: your libraries at the surface, locked dependencies and the PDK in deeper water
      { title: msg("app:libraries.project"), libs: ls.filter((l) => l.origin === "project"), deep: false },
      // the ones project.json names first, then those installed for a package that needs them
      { title: msg("app:libraries.dependencies"), libs: [...ls.filter((l) => l.origin === "dep" && l.direct !== false),
        ...ls.filter((l) => l.origin === "dep" && l.direct === false)], deep: true },
      { title: msg("app:libraries.pdk"), libs: ls.filter((l) => l.origin === "pdk"), deep: true },
    ];
  }, [info]);

  const active = tabs.find((t) => t.id === activeId) ?? null;
  const libInfo = (name: string) => info?.libraries.find((l) => l.name === name);
  const previewReadonly = !!project && (project.endsWith("~base") ||
    (inPreview && liveWriters.some((w) => w.id === project.split("~")[1])));

  // a dependency library's tags: who needs it (installed for a package, not named in project.json), the package an
  // alias name installs, or the alias a package's own name serves
  const depTags = (l: LibraryInfo) => {
    const row = deps?.libraries.find((x) => x.kind === "dependency" && x.name === l.name);
    const via = l.via?.length ? l.via : row?.via ?? [];
    const pkg = l.package ?? row?.package ?? null;
    if (l.direct === false && via.length) {
      return <span className="tag" title={t("app:dependency.viaHint", { list: via.join(", "), chain: row?.chain ?? via.join(", ") })}>
        {t("app:dependency.via", { list: via.join(", ") })}</span>;
    }
    if (pkg && pkg !== l.name) {
      return <span className="tag mono" title={t("app:dependency.aliasHint", { name: l.name, package: pkg })}>
        {t("app:dependency.alias", { package: pkg })}</span>;
    }
    const others = (row?.names ?? []).filter((n) => n !== l.name);
    if (l.direct === false && others.length) {
      return <span className="tag mono" title={t("app:dependency.sameAsHint", { names: others.join(", ") })}>
        {t("app:dependency.sameAs", { names: others.join(", ") })}</span>;
    }
    return null;
  };
  // the shield of a dependency with content the trust rule refuses: the Trust dialog (a person's, never an agent's)
  const trustButton = (name: string) => {
    const row = deps?.libraries.find((x) => x.kind === "dependency" && x.name === name);
    if (!row || inPreview || row.trusted === null || row.trusted === undefined || !flagText(row.flagged)) return null;
    const hint = row.trusted ? t("app:dependency.trustedHint") : `${t("app:dependency.trustHint")}: ${resolveText(flagText(row.flagged))}`;
    return (
      <button className={`icon${row.trusted ? " dim" : " warn"}`} title={hint} aria-label={hint}
        onClick={(e) => { e.stopPropagation(); setTrustName(name); }}>
        {row.trusted ? <ShieldCheck size={13} /> : <ShieldAlert size={13} />}
      </button>
    );
  };

  // the PDK badge: the project's PDK and how it stands against undine.lock, a menu with what can be done about it
  const pdkBadge = () => {
    if (!info) return null;
    const state = deps?.pdk.state;
    const missing = state === "missing" || (!info.pdk && !!info.pdk_requested);
    const warn = !!info.pdk_unsupported || missing || PDK_TROUBLE.has(state ?? "");
    const fetchable = !inPreview && (missing || state === "modified");
    const label = info.pdk ? (
      <>
        {info.pdk_unsupported ? <AlertTriangle size={12} /> : <Cpu size={13} />} {info.pdk.name}
        <span className={`mono${info.pdk_unsupported ? "" : " dim"}`}>@{info.pdk.version.slice(0, 7)}</span>
        {info.pdk_unsupported ? ` ${t("app:pdk.unsupported")}` : state && state !== "not pinned" && state !== "not locked" && state !== "none"
          ? <span className="dim">{resolveText(pdkStateText(state))}</span> : null}
      </>
    ) : info.pdk_requested ? t("app:pdk.missing", { pdk: info.pdk_requested }) : t("app:pdk.none");
    const family = pdks.find((x) => x.id === deps?.pdk.installed)?.family ?? "FAMILY";
    const pid = { project: project ?? "" };
    const hint = missing ? msg("app:pdk.hint.missing")
      : state === "modified" ? msg("app:pdk.hint.modified", { family, commit: deps?.pdk.commit ?? "COMMIT" })
      : state === "verified" ? msg("app:pdk.hint.verified")
      : state === "unverified" ? msg("app:pdk.hint.unverified")
      : state === "unchecked" ? msg("app:pdk.hint.unchecked", pid)
      : state === "not pinned" ? msg("app:pdk.hint.notPinned", pid) : null;
    return (
      <div className="langmenu pdkmenu" ref={pdkMenuRef}
        onKeyDown={(e) => { if (e.key === "Escape" && pdkMenu) { e.preventDefault(); setPdkMenu(false); } }}>
        <button className={`badge${warn ? " warn" : info.pdk ? "" : " dim"}`} aria-haspopup="menu" aria-expanded={pdkMenu}
          title={info.pdk_unsupported ?? (info.pdk ? t("app:project.pdkPinned", { pdk: info.pdk.id }) : undefined)}
          onClick={() => setPdkMenu((v) => !v)}>
          {label}
        </button>
        {pdkMenu && (
          <div className="langpop pdkpop" role="menu" aria-label={t("app:pdk.menu")}>
            {state && (
              <div className="pdkstate">
                <span className={PDK_TROUBLE.has(state) ? "warn" : ""}>{resolveText(pdkStateText(state))}</span>
                {hint && <span className="faint small">{resolveText(hint)}</span>}
              </div>
            )}
            {info.pdk_unsupported && (
              <button role="menuitem" onClick={() => { setPdkMenu(false); void movePdk(); }}>
                <AlertTriangle size={13} /> {info.pdk_upgrade ? t("app:pdk.move", { to: info.pdk_upgrade }) : t("app:pdk.unsupportedEntry")}
              </button>
            )}
            {fetchable && (
              <button role="menuitem" onClick={() => { setPdkMenu(false); setPdkFetch(true); }}>
                <Download size={13} /> {t("app:pdk.fetch")}
              </button>
            )}
            <button role="menuitem" onClick={rescanPdks}><RefreshCw size={13} /> {t("app:pdk.rescan")}</button>
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="app">
      <DialogHost />
      {pdkFetch && project && <PdkFetchDialog project={project} onClose={() => setPdkFetch(false)} onFetched={() => pdksChanged()} />}
      {trustName && project && <TrustDialog project={project} name={trustName} onClose={() => setTrustName(null)} onChanged={() => loadDeps(project)} />}
      {refusal && <RefusalDialog refusal={refusal} onClose={() => setRefusal(null)} />}
      <header className="topbar">
        <div className="brand" title="Undine"><Logo variant="full" height={20} /></div>
        <select className="projsel" value={project ?? ""} onChange={(e) => setProject(e.target.value)}>
          {!project && <option value="">{t("app:project.select")}</option>}
          {inPreview && project && <option value={project}>{project}</option>}
          {projects.map((p) => <option key={p.name} value={p.name} title={p.error}>{p.error ? `${p.name} ⚠` : p.name}</option>)}
        </select>
        <button className="ghost" onClick={newProject}><Plus size={14} /> {t("app:project.label")}</button>
        {info && pdkBadge()}
        {deps && (
          <button className={`badge ${deps.ok ? "" : "warn"}`} onClick={showLock}
            title={deps.ok ? t("app:lock.hint") : deps.issues.join("\n")}>
            {deps.ok ? <><Lock size={12} /> {t("app:lock.locked")}</> : <><AlertTriangle size={12} /> {t("app:lock.issues", { count: deps.issues.length })}</>}
          </button>
        )}
        <span className="grow" />
        <button className={`icon topagent${showChat ? " on" : ""}`} aria-pressed={showChat}
          title={t(showChat ? "app:agent.hide" : "app:agent.show")} aria-label={t(showChat ? "app:agent.hide" : "app:agent.show")}
          onClick={() => { if (!showChat) setChatFocus((n) => n + 1); setShowChat(!showChat); }}>
          <Bot size={16} />
        </button>
        <button className={`ghost act${showChanges ? " on" : ""}`} title={t("app:changes.hint")} onClick={() => { setShowChanges((v) => !v); setShowHistory(false); }}>
          <GitPullRequestArrow size={14} /> {t("app:changes.label")}
          {openChanges > 0 ? <span className="countdot">{openChanges}</span> : null}
          {closings[0]?.closing && <span className="countdot closing"
            title={closings.map((w) => `${w.title}: ${resolveText(closingText(w.closing!))}`).join("\n")}>
            <Loader2 size={11} className="spin" /> {t(`app:changes.closing.${closings[0].closing.action}`)}</span>}
        </button>
        <button className={`ghost act${showHistory ? " on" : ""}`} onClick={() => { setShowHistory((v) => !v); setShowChanges(false); }}
          title={[t("history:button.hint"), gitCells.size ? t("history:changedCells", { count: gitCells.size }) : "",
            gitUnsavedLibs.length ? `${t("history:mark.unsavedLib")}: ${gitUnsavedLibs.join(", ")}` : "",
            gitStale.length ? `${t("history:mark.stale")}: ${gitStale.join(", ")}` : ""].filter(Boolean).join("\n")}>
          {/* a changeset's working copy: its branch (the server names it), though the copy has no history of its own */}
          <GitBranch size={14} /> {git?.repo || (git?.preview && git.branch) ? <span className="mono gitbranch">{git.branch ?? t("history:detached")}</span>
            : git && !git.preview ? t("history:button.noGit") : t("history:button.label")}
          {gitCells.size ? <span className="countdot">{gitCells.size}</span> : null}
          {gitUnsavedLibs.length > 0 && !gitCells.size && <span className="gitmark gm-unsaved" />}
          {gitStale.length > 0 && <AlertTriangle size={13} className="gitstalemark" />}
        </button>
        <LanguageMenu />
      </header>
      {others.map((w) => (
        <div key={w.id} className="previewbar livebar">
          <Bot size={14} /> {t(w.writer.kind === "mcp" ? "app:agent.editingMcp" : "app:agent.editing", { agent: writerName(w), title: w.title })} <span className="mono dim">{w.id}</span>
          <span className="grow" />
          <button className="ghost" onClick={() => watch(w)}>{t("app:agent.watch")}</button>
          <button className="icon" title={t("app:hide")} onClick={() => setDismissed((d) => new Set([...d, w.id]))}><X size={13} /></button>
        </div>
      ))}

      {waiting.map((a) => (
        <div key={a.id} className="previewbar livebar">
          <GitPullRequestArrow size={14} /> {t("app:agent.awaiting", { agent: a.agent, title: a.title })} <span className="mono dim">{a.id}</span>
          <span className="grow" />
          <button className="ghost" onClick={() => { setReviewId(a.id); setShowChanges(false); }}>{t("app:agent.review")}</button>
          <button className="icon" title={t("app:hide")} onClick={() => setDismissed((d) => new Set([...d, a.id]))}><X size={13} /></button>
        </div>
      ))}

      {inPreview && realProject && (
        <div className="previewbar" hidden={!!reviewId}>
          <GitPullRequestArrow size={14} /> {t("app:preview.description", { id: project!.split("~")[1] })}
          <span className="grow" />
          <button className="ghost" onClick={() => setReviewId(project!.split("~")[1])}>{t("app:review")}</button>
          <button className="ghost" onClick={() => setProject(realProject)}>{t("app:preview.back", { project: realProject })}</button>
        </div>
      )}
      <div className={`main${reviewId ? " reviewing" : ""}`}>
        <aside className={`leftcol${showChat ? " withchat" : ""}`}>
          {showChat && project && (<>
            <ChatPanel project={project} onClose={() => setShowChat(false)} toast={toast} selection={selection}
              capture={active?.view === "layout" ? () => captureRef.current?.() ?? null : null}
              onProposed={() => { setReloadNonce((n) => n + 1); refreshChanges(); setTurnRunning(false); }} onTurnWork={onTurnWork}
              follow={work.current && turnRunning ? { paused: followPaused, onPause: () => setFollowPaused(true), onResume: resumeFollow } : undefined}
              focusNonce={chatFocus} docked onReview={(id) => { setReviewId(id); setShowChanges(false); }}
              target={active && active.view !== "symbol" ? { lib: active.lib, cell: active.cell, view: active.view } : null} />
            <RowSplitter />
          </>)}
        <nav className="libbrowser">
          <div className="lbhead">
            <div className="searchbox"><Search size={13} /><input placeholder={t("app:cell.find")} value={filter} onChange={(e) => setFilter(e.target.value)} /></div>
            <button className="icon" title={t("app:library.new")} onClick={newLibrary}><FolderPlus size={15} /></button>
            <button className="icon" title={t("app:dependency.addHint")} disabled={inPreview} onClick={addDep}><PackagePlus size={15} /></button>
          </div>
          <div className="lbscroll">
            {libGroups.map((g) => g.libs.length ? (
              <div key={g.title.key} className={`lbgroup${g.deep ? " deep" : ""}`}>
                <div className="lbgtitle">{resolveText(g.title)}<span className="n">{g.libs.length}</span>
                  {g.deep && <span className="why">{t("app:libraries.readonly")}</span>}</div>
                {g.libs.map((l) => {
                  const cs = (cells[l.name] ?? []).filter((c) => !filter || c.name.toLowerCase().includes(filter.toLowerCase()));
                  if (filter && cells[l.name] && !cs.length) return null;
                  const open = expanded.has(l.name) || (!!filter && cs.length > 0);
                  // cells generated for schematic devices fold into one row; a search shows the ones it finds
                  const plain = cs.filter((c) => !c.device), devices = cs.filter((c) => c.device);
                  const devicesOpen = deviceFolds.has(l.name) || !!filter || (active?.lib === l.name && devices.some((c) => c.name === active.cell));
                  const devicesGit = devices.length ? groupGit(gitCells, l.name, devices.map((c) => c.name)) : null;     // folded: one marker
                  const cellRow = (c: CellInfo) => (
                    <div key={c.name} className={`cellrow ${active?.lib === l.name && active.cell === c.name ? "cur" : ""}`}
                      onDoubleClick={() => openView(l.name, c.name, c.device ? c.views[0] : "cell")}>
                      <span className={`cellname ${c.top ? "top" : ""}`} title={c.device ? undefined : t("cell:open")}
                        onClick={() => openView(l.name, c.name, c.device ? c.views[0] : "cell")}>{c.name}</span>
                      {cellGit(gitCells, l.name, c.name) && <GitCellMark g={cellGit(gitCells, l.name, c.name)!} />}
                      <span className="views">
                        {c.views.map((v) => (
                          <button key={v} className={`view v-${v}`} title={t("app:cell.open", { view: msg(`common:view.${v}`) })} onClick={() => openView(l.name, c.name, v)}>{VIEW_SHORT[v]}</button>
                        ))}
                      </span>
                    </div>
                  );
                  return (
                    <div key={l.name}>
                      <div className="librow" onClick={() => toggleLib(l.name)}>
                        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                        <Library size={14} className="dim" />
                        <span className="libname">{l.name}</span>
                        {l.readonly && <Lock size={12} className="dim" />}
                        {l.version && <span className="tag mono" title={t("app:dependency.pinned", { version: l.version })}>{l.version}</span>}
                        {l.origin === "dep" && depTags(l)}
                        <span className="grow" />
                        {libraryCount(gitCells, l.name) > 0 && <span className="gitcount" title={t("history:changedCells", { count: libraryCount(gitCells, l.name) })}>
                          {libraryCount(gitCells, l.name)}</span>}
                        {gitLibs.get(l.name)?.unsaved && <span className="gitmarks" title={t("history:mark.unsavedLib")} aria-label={t("history:mark.unsavedLib")} role="img">
                          <span className="gitmark gm-unsaved" /></span>}
                        {gitLibs.get(l.name)?.stale && <span className="gitstalemark" title={t("history:mark.stale")} aria-label={t("history:mark.stale")} role="img">
                          <AlertTriangle size={13} /></span>}
                        {l.origin === "dep" && trustButton(l.name)}
                        {l.origin === "dep" && !inPreview && l.direct !== false && (
                          <span className="rowactions">
                            <button className="icon" title={t("app:dependency.updateHint")} onClick={(e) => { e.stopPropagation(); updateDep(l.name); }}><RefreshCw size={13} /></button>
                            <button className="icon" title={t("app:dependency.removeHint")} onClick={(e) => { e.stopPropagation(); removeDep(l.name); }}><Trash2 size={13} /></button>
                          </span>
                        )}
                        {!l.readonly && (
                          <span className="rowactions">
                            <button className="icon" title={t("app:cell.newHint")} onClick={(e) => { e.stopPropagation(); newCell(l.name); }}><Plus size={13} /></button>
                            <button className="icon" title={t("app:import.hintButton")} onClick={(e) => { e.stopPropagation(); importInto(l.name); }}><FileUp size={13} /></button>
                          </span>
                        )}
                      </div>
                      {open && (
                        <div className="cells">
                          {!cells[l.name] && <div className="empty">{t("common:state.loading")}</div>}
                          {cells[l.name] && !cs.length && <div className="empty">{t("app:cell.empty")}</div>}
                          {plain.slice(0, 600).map(cellRow)}
                          {plain.length > 600 && <div className="empty">{t("app:cell.more", { count: plain.length - 600 })}</div>}
                          {devices.length > 0 && (
                            <div className="devicefold">
                              <div className="cellrow devicerow" title={t("app:cell.devicesHint")} onClick={() => toggleDevices(l.name)}>
                                {devicesOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                                <span className="cellname dim">{t("app:cell.devices")}</span>
                                {!devicesOpen && devicesGit && <GitGroupMark g={devicesGit} />}
                                <span className="dim small mono">{devices.length}</span>
                              </div>
                              {devicesOpen && devices.slice(0, 600).map(cellRow)}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            ) : null)}
            {info && !info.libraries.length && <div className="empty">{t("app:library.empty")}</div>}
          </div>
        </nav>
        </aside>
        <Splitter pane="left" />

        <section className="workspace">
          <TabStrip activeId={activeId} count={tabs.length}>
            {tabs.map((t) => {
              const Icon = VIEW_ICON[t.view];
              return (
                <div key={t.id} className={`tab ${t.id === activeId ? "on" : ""}`} onClick={() => activateTab(t.id)}
                  onAuxClick={(e) => { if (e.button === 1) closeTab(t.id); }}>
                  <Icon size={13} className={`tv-${t.view}`} />
                  <span className="dim">{t.lib}/</span>{t.cell}
                  {t.dirty ? <span className="dirty" /> : null}
                  <button className="tabx" onClick={(e) => { e.stopPropagation(); closeTab(t.id); }}><X size={12} /></button>
                </div>
              );
            })}
          </TabStrip>
          {active && project && merged[active.id]?.project === project && (
            <div className="note warnnote stale tabnotice" style={{ margin: 0 }} title={merged[active.id].files.join("\n") || undefined}>
              <AlertTriangle size={13} />
              <span className="grow">{t(merged[active.id].action === "revert" ? "app:merged.revert" : "app:merged.apply", { lib: active.lib, cell: active.cell })}</span>
              <button className="ghost" onClick={() => reloadMerged(active)}>{t("app:merged.reload")}</button>
              <button className="icon" title={t("app:merged.hide")} onClick={() => dropMerged(active.id)}><X size={13} /></button>
            </div>
          )}
          <div className="tabbody">
            {!active && <Welcome project={info} />}
            {project && tabs.map((t) => (
              <div key={`${t.id}#${reloadNonce}#${tabNonce[t.id] ?? 0}`} className="tabpane" style={{ display: t.id === activeId ? "flex" : "none" }}
                ref={(el) => { if (el) panes.current.set(t.id, el); else panes.current.delete(t.id); }}>
                {t.view === "cell" ? (
                  <CellOverview project={project} lib={t.lib} cell={t.cell} active={t.id === activeId}
                    readonly={!!libInfo(t.lib)?.readonly || previewReadonly} tick={reloadNonce + (pageTick[t.id] ?? 0)}
                    onOpenView={(v) => openView(t.lib, t.cell, v)}
                    onOpenCell={(l, c, v) => openView(l, c, v ?? "cell")}
                    onCreate={async (v) => {
                      try {
                        await api.newCellViews(project, t.lib, t.cell, [v]);
                        await loadCells(t.lib);
                        openView(t.lib, t.cell, v);
                      } catch (e) { toast(apiErrorText(e), "error"); }
                    }}
                    toast={toast} />
                ) : t.view === "layout" ? (
                  <LayoutEditor project={project} lib={t.lib} cell={t.cell} active={t.id === activeId}
                    readonly={!!libInfo(t.lib)?.readonly || previewReadonly}
                    libCells={(cells[t.lib] ?? []).filter((c) => c.views.includes("layout")).map((c) => c.name)}
                    libraries={(info?.libraries ?? []).filter((l) => l.has_layout).map((l) => l.name)}
                    hasSchematic={hasView(t.lib, t.cell, "schematic")}
                    probe={probeFor(t.lib, t.cell)} diffWith={diffPreview} fitOnLoad={followFit(t.id)}
                    onProbe={(q) => doProbe(t.lib, t.cell, q)}
                    onSelection={(items) => setSelection({ lib: t.lib, cell: t.cell, view: "layout", items })}
                    registerCapture={(fn) => { if (fn || t.id === activeId) captureRef.current = fn; }}
                    onOpenSchematic={() => openView(t.lib, t.cell, "schematic")}
                    onOpenCell={(c) => openView(t.lib, c, "layout")}
                    onDirty={(d) => { setTabs((ts) => ts.map((x) => (x.lib === t.lib && x.view === "layout" ? { ...x, dirty: d } : x))); gitSoon(!d); }}
                    toast={toast} />
                ) : t.view === "rtl" ? (
                  <RtlView project={project} lib={t.lib} cell={t.cell} active={t.id === activeId}
                    readonly={!!libInfo(t.lib)?.readonly || previewReadonly}
                    rtlCells={(cells[t.lib] ?? []).filter((c) => c.views.includes("rtl")).map((c) => c.name)}
                    onDirty={(d) => { setTabs((ts) => ts.map((x) => (x.id === t.id ? { ...x, dirty: d } : x))); gitSoon(!d); }}
                    onLibraryChanged={() => { void loadCells(t.lib); }}
                    toast={toast} />
                ) : t.view === "symbol" ? (
                  <SymbolEditor project={project} lib={t.lib} cell={t.cell} active={t.id === activeId}
                    readonly={!!libInfo(t.lib)?.readonly || previewReadonly}
                    hasSchematic={hasView(t.lib, t.cell, "schematic")} fitOnLoad={followFit(t.id)}
                    onOpenSchematic={() => openView(t.lib, t.cell, "schematic")}
                    verificationPanel={<SymbolCheckPanel project={project} lib={t.lib} cell={t.cell} toast={toast} />}
                    onChanged={(affected) => refreshAffected(t.id, affected)}
                    toast={toast} />
                ) : (
                  <SchematicView project={project} lib={t.lib} cell={t.cell} view={t.view} active={t.id === activeId}
                    onChanged={(affected) => refreshAffected(t.id, affected)}
                    readonly={!!libInfo(t.lib)?.readonly || previewReadonly}
                    hasLayout={hasView(t.lib, t.cell, "layout")}
                    hasSymbol={hasView(t.lib, t.cell, "symbol")}
                    onOpenOwnSymbol={async () => {
                      try {
                        if (!hasView(t.lib, t.cell, "symbol")) {
                          await api.newCellViews(project, t.lib, t.cell, ["symbol"]);
                          await loadCells(t.lib);
                        }
                        openView(t.lib, t.cell, "symbol");
                      } catch (e) { toast(apiErrorText(e), "error"); }
                    }}
                    probe={probeFor(t.lib, t.cell)} diffWith={diffPreview} fitOnLoad={followFit(t.id)}
                    onProbe={(q) => doProbe(t.lib, t.cell, q)}
                    onSelection={(items) => setSelection({ lib: t.lib, cell: t.cell, view: "schematic", items })}
                    onOpenLayout={() => openView(t.lib, t.cell, "layout")}
                    onOpenSymbol={(s) => openSymbol(t.lib, s)} toast={toast} />
                )}
              </div>
            ))}
          </div>
        </section>
      </div>

      {reviewId && realProject && (
        <ChangesPanel key={`review-${reviewId}`} initialId={reviewId} project={realProject} onClose={closeReview} toast={toast}
          onPreview={(p) => { switchProject(p); }}
          onWorkChanged={() => setReloadNonce((n) => n + 1)}
          review={{
            side: diffPreview ? "diff" : inPreview ? "proposal" : "confirmed", focused: focusedItem, onFocus: focusItem,
            onSide: (side, preview) => { switchProject(side === "confirmed" ? realProject : preview); setDiffPreview(side === "diff" ? preview : null); setProbe(null); },
            onSettled: () => { setFocusedItem(null); setDiffPreview(null); if (inPreview) switchProject(realProject); },
            onRequestChanges: (preview) => { switchProject(preview); setDiffPreview(null); setShowChat(true); setChatFocus((n) => n + 1); },
            follow: work.current?.turn.startsWith(`ext:${reviewId}:`) && turnRunning
              ? { paused: followPaused, onPause: () => setFollowPaused(true), onResume: resumeFollow } : undefined,
          }}
          onOpenView={openIn} onGit={refreshGit} onHistory={() => setShowHistory(true)}
          onChanged={designChanged} liveClosings={closingIds} />
      )}
      {showChanges && realProject && (
        <ChangesPanel key="list" project={realProject} onClose={() => setShowChanges(false)} toast={toast}
          onPreview={(p) => setProject(p)}
          onOpen={(id) => { setReviewId(id); setShowChanges(false); }}
          onOpenView={openIn} onGit={refreshGit} onHistory={() => { setShowChanges(false); setShowHistory(true); }}
          onChanged={designChanged} liveClosings={closingIds} />
      )}
      {showHistory && project && (
        <HistoryPanel project={project} status={git} error={gitError} tick={gitTick}
          cell={active ? { lib: active.lib, cell: active.cell } : null} cellViews={cellViews} wantCells={wantCells}
          onStatus={setGit} onRefresh={refreshGit} onReloaded={layoutReloaded} onOpenView={openView}
          onReview={(id) => { setReviewId(id); setShowChanges(false); setShowHistory(false); }}
          onClose={() => setShowHistory(false)} toast={toast} />
      )}
      <div className="toasts">
        {toasts.map((t) => <div key={t.id} className={`toast ${t.kind}`}>{resolveText(t.msg)}</div>)}
      </div>
    </div>
  );
}

function Welcome({ project }: { project: ProjectInfo | null }) {
  const { t } = useI18n();
  return (
    <div className="welcome">
      <div className="wcard">
        <Logo variant="full" height={64} className="welcomelogo" />
        <p className="tagline">{t("app:welcome.tagline")}</p>
        <p className="dim">{t("app:welcome.server", { pdk: project?.pdk?.name ?? msg("common:state.none") })}</p>
        <div className="keys">
          <div><kbd>F</kbd> {t("app:shortcut.fit")}</div><div><kbd>R</kbd> {t("app:shortcut.rectangle")}</div><div><kbd>P</kbd> {t("app:shortcut.path")}</div>
          <div><kbd>Shift</kbd>+<kbd>P</kbd> {t("app:shortcut.polygon")}</div><div><kbd>M</kbd> {t("app:shortcut.move")}</div><div><kbd>C</kbd> {t("app:shortcut.copy")}</div>
          <div><kbd>I</kbd> {t("app:shortcut.instance")}</div><div><kbd>K</kbd> {t("app:shortcut.ruler")}</div><div><kbd>U</kbd> {t("app:shortcut.undo")}</div>
          <div><kbd>Ctrl</kbd>+<kbd>F</kbd> {t("app:shortcut.top")}</div><div><kbd>Shift</kbd>+<kbd>F</kbd> {t("app:shortcut.all")}</div><div><kbd>Ctrl</kbd>+<kbd>S</kbd> {t("app:shortcut.save")}</div>
        </div>
        <p className="dim small">{t("app:welcome.open")}</p>
      </div>
    </div>
  );
}
