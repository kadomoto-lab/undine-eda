// Thin client for the Undine design API. All heavy work happens on the server.

export interface PdkInfo {
  id: string;
  name: string;
  family: string;
  version: string;
}
export interface ProjectInfo {
  name: string;
  pdk: PdkInfo | null;
  pdk_requested: string | null;
  pdk_unsupported?: string | null;   // the PDK's PCell code is a generation Undine no longer runs
  pdk_upgrade?: string | null;       // the supported version of the same PDK on this server
  created?: string;
  error?: string;                    // project.json cannot be read (a git merge left conflict markers): name only
}
export interface LibraryInfo {
  name: string;
  readonly: boolean;
  origin: "project" | "dep" | "pdk";
  has_layout: boolean;
  version?: string;              // a dependency: "rev @ commit"
  // a dependency: its [package] name (null without a manifest), whether project.json names it (false: installed for a
  // package that needs it, or the package's own name beside an alias) and the packages that place it (transitive)
  package?: string | null;
  direct?: boolean;
  via?: string[];
}
/** A tool as undine.lock records it or as found here: its version number (null: its banner, `text`, has none) and
 * where it came from (pixi, PATH, ...; null in a format 1 lock, which does not record it). */
export interface ToolRecord { version: string | null; from: string | null; text?: string }
/** The project's PDK against its pin (server/undine/pdks/pdkops.py pdk_block). */
export type PdkState = "none" | "missing" | "not locked" | "other version" | "not pinned" | "mismatch" | "error"
  | "unchecked" | "modified" | "unverified" | "verified";
export interface PdkLockState {
  requested: string | null;      // project.json
  locked: string | null;         // undine.lock's id
  commit: string | null;         // the pin's full commit (format 2)
  installed: string | null;      // the id found on this server
  state: PdkState;
  changed: { part: string; added: string[]; removed: string[]; modified: string[] }[];
  verified_by: string | null;
  ok: boolean;
}
/** The project as locked (undine.lock) vs. as installed on this server (server/undine/store/deps.py). Only `issues`
 * make it not ok; `warnings` (another tool version or origin) and `notes` are said, not counted. */
export interface DepsStatus {
  format: number | null;         // undine.lock's format, null without a lock
  libraries: ({ name: string; kind: "own" | "linked" | "dependency"; state: string; git?: string; rev?: string | null;
    subdir?: string | null; locked?: string | null; installed?: string | null; path?: string;
    hash?: "sha" | "tree" | null;  // what the lock entry pins the files by
    in_store?: boolean } & DepPackageFields)[];  // the locked files are all in the store (sync needs no network)
  pdk: PdkLockState;
  tools: { name: string; locked: ToolRecord | null; now: ToolRecord | null; now_path?: string | null; ok: boolean }[];
  issues: string[];
  warnings?: string[];
  notes?: string[];
  ok: boolean;
}
/** What the trust rule would refuse in a dependency (trust.counts): findings in SPICE code, RTL and Tcl. */
export interface TrustCounts { spice: number; rtl: number; tcl: number }
/** A dependency row's package fields (deps.status, phase 2); an older server leaves them out. */
export interface DepPackageFields {
  package?: string | null;       // the manifest's [package] name; null for a plain git dependency
  version?: string | null;       // the manifest's version
  names?: string[];              // every local name the entry is installed under, the project.json key first
  direct?: boolean;              // project.json names it
  via?: string[];                // the packages that require it
  chain?: string | null;         // "p -> analog_ip 1.2.0 (v1.2.0 @ c12) -> sar_common 1.3.1 (...)"
  trusted?: boolean | null;      // null: not installed
  flagged?: TrustCounts | null;  // null: not installed
  internal_cells?: string[];     // cells its [exports] leaves out
  pdk?: { compatible: boolean | null; reason: string | null; note: string | null; built_with: string | null } | null;
}
/** One entry a resolving command (add, lib lock) resolved anew; a package adds package, version, names and via. */
export interface ResolvedDep {
  name: string; commit: string; package?: string; version?: string; names?: string[]; via?: string[];
  flagged?: TrustCounts; trusted?: boolean;
}
export interface DepAdd {
  name: string; commit: string; cells: string[];
  package?: string | null; version?: string | null;
  public_cells?: string[]; internal_cells?: string[];
  resolved?: ResolvedDep[];      // the dependency and every package it brought in
  flagged?: TrustCounts; trusted?: boolean;
}
/** A package an update added, moved or dropped (`to` null). */
export interface PackageChange {
  package: string | null; names: string[]; from: string | null; to: string | null;
  changed?: { added: string[]; removed: string[]; modified: string[] };
}
export interface DepRemove {
  ok?: boolean; name?: string;
  removed_packages?: { package: string | null; version: string | null; commit: string | null; names: string[] }[];
  kept?: { name: string; placed_by: string[] }[];
}

// ------------------------------------------------------------------ trust (server/undine/store/trust.py)

/** One line the trust rule would refuse: where (the dependency's file, from .undine/deps), what kind and the line. */
export interface TrustFinding { file: string; kind: string; line: number; text: string }
export interface TrustDependency {
  name: string; package: string | null; version: string | null; key: string; trusted: boolean; findings: TrustFinding[];
}
/** The findings as a list, from the list the server sends or from a trust.scan_dir listing
 * ({cells: {cell: [Finding]}, rtl: {file: [...]}, tcl: {file: [...]}}). */
export function trustFindings(x: unknown): TrustFinding[] {
  if (Array.isArray(x)) return x.filter((f) => f && typeof f === "object").map((f) => ({ file: String(f.file ?? f.where ?? ""),
    kind: String(f.kind ?? ""), line: Number(f.line ?? 0), text: String(f.text ?? "") }));
  if (!x || typeof x !== "object") return [];
  const out: TrustFinding[] = [];
  for (const part of ["cells", "rtl", "tcl"]) {
    const files = (x as Record<string, unknown>)[part];
    if (!files || typeof files !== "object") continue;
    for (const [file, list] of Object.entries(files as Record<string, unknown>)) {
      for (const f of trustFindings(list)) out.push({ ...f, file: f.file || file });
    }
  }
  return out;
}

// ------------------------------------------------------------------ refusals of the dependency commands

/** The codes of the dependency refusals that carry more than words (server/undine/server/api.py _err). */
export type DepRefusalCode = "resolution_refused" | "not_direct" | "pdk_incompatible" | "untrusted" | "needs_lock_format_2"
  | "not_synced";
/** A problem of a refused resolution: its words, and the chains that lead to it. */
export interface ResolveProblem { code: string; message: string; chains: string[] }
/** A locked package the PDK does not fit (compat.locked_incompatible). */
export interface IncompatiblePackage {
  name: string; package: string | null; version: string | null; chain: string; reason: string;
  family?: string; variants?: string[]; commits?: string[]; compat?: string[]; built_with?: string | null;
}
/** A launch refused because a dependency's content is not trusted (code untrusted, 403). */
/** One check the sign-off suite started, or why it did not: a trust refusal (code untrusted) carries the refusal's
 * fields as the 403 does (untrustedOfSuite). */
export interface SuiteJob {
  name: string; job?: string; kind?: string; tool?: string; reused?: boolean; error?: string;
  code?: string; params?: Record<string, unknown>;
}
/** A suite job's error as the ApiError its launch would have raised alone, so the panels' notices read it. */
export function suiteError(j: SuiteJob): ApiError | null {
  return j.error ? new ApiError(j.code === "untrusted" ? 403 : 400, j.error, j.code, j.params) : null;
}

export interface UntrustedRefusal {
  name: string; package: string | null; key: string; cell: string; findings: TrustFinding[]; detail: string;
}
const coded = (e: unknown, code: DepRefusalCode): ApiError | null => e instanceof ApiError && e.code === code ? e : null;
const listOf = <T,>(v: unknown): T[] => Array.isArray(v) ? v as T[] : [];
const strOrNull = (v: unknown) => typeof v === "string" ? v : null;
/** A resolution_refused error's problems (a problem may come as its words only); null for any other error. */
export function problemsOf(e: unknown): ResolveProblem[] | null {
  const err = coded(e, "resolution_refused");
  if (!err) return null;
  return listOf<unknown>(err.params?.problems).map((p) => typeof p === "string" ? { code: "", message: p, chains: [] }
    : { code: String((p as ResolveProblem).code ?? ""), message: String((p as ResolveProblem).message ?? ""),
      chains: listOf<string>((p as ResolveProblem).chains).map(String) });
}
/** A pdk_incompatible error's packages; null for any other error. */
export function incompatibleOf(e: unknown): IncompatiblePackage[] | null {
  const err = coded(e, "pdk_incompatible");
  return err ? listOf<IncompatiblePackage>(err.params?.packages) : null;
}
/** A not_direct error's placers (who installs that name); null for any other error. */
export function placedByOf(e: unknown): string[] | null {
  const err = coded(e, "not_direct");
  return err ? listOf<unknown>(err.params?.placed_by).map(String) : null;
}
/** An untrusted error's dependency and findings; null for any other error. */
export function untrustedOf(e: unknown): UntrustedRefusal | null {
  const err = coded(e, "untrusted");
  if (!err) return null;
  const p = err.params ?? {};
  return { name: String(p.name ?? ""), package: strOrNull(p.package), key: String(p.key ?? ""), cell: String(p.cell ?? ""),
    findings: trustFindings(p.findings), detail: typeof err.detail === "string" ? err.detail : err.message };
}

/** What a PDK fetch would run (pdkops.fetch_plan): libraries null is ciel's default set. */
export interface PdkFetchPlan {
  family: string; variant: string; commit: string; libraries: string[] | null; source: string; root: string;
}
/** A PDK fetch running on the server (fetchjobs.py); kept in memory, a server restart forgets it. */
export interface PdkFetchJob {
  id: string; project: string; state: "queued" | "running" | "done" | "failed"; log: string[];
  result: { path: string; verify: { state: string; warnings?: string[] } } | null;
  error: string | null; created: number; ended: number | null;
}
export interface DepUpdate {
  name: string; from: string | null; to: string; rev: string | null;
  changed: { added: string[]; removed: string[]; modified: string[] };
  used_by: string[];
  packages_changed?: PackageChange[];   // packages the update added, moved or dropped (phase 2)
}
export interface CellInfo {
  name: string;
  views: ViewKind[];
  top: boolean;
  device?: boolean;     // a layout cell generated for a schematic device (PCell), listed folded
}
export type ViewKind = "schematic" | "symbol" | "rtl" | "layout";

export interface LayoutInfo {
  cell: string;
  dbu: number;
  bbox: [number, number, number, number];
  version: number;
  dirty: boolean;
  tile_px: number;
  children: string[];
  readonly: boolean;
}
export interface LayerInfo {
  id: number;
  name: string;
  source: string;
  fill: string;
  frame: string;
  visible: boolean;
  valid: boolean;
  used: boolean;
  icon: string;
}
export interface Ref {
  k: "s" | "i";
  [key: string]: unknown;
}
export interface SelItem {
  kind: "shape" | "inst";
  type: string;
  ref: Ref;
  bbox: [number, number, number, number];
  outline: number[][];
  layer?: string;
  layer_name?: string;
  width?: number;
  text?: string;
  cell?: string;
  inst_name?: string;
  trans?: string;
  array?: { na: number; nb: number; a: string; b: string };
  pcell?: { library: string; params: Record<string, unknown> };
}
export interface EditResult<T = SelItem[]> {
  version: number;
  dirty: boolean;
  result: T;
}

export interface CopySnapshot<T> {
  payload: Record<string, unknown>;
  bbox: [number, number, number, number];
  preview: T;
}
export interface PasteBody {
  source: { project: string; lib: string };
  payload: Record<string, unknown>;
  dx: number;
  dy: number;
}

export interface SchText {
  s: string;
  x: number;
  y: number;
  rot: number;
  flip: number;
  size: number;
  layer: number;
  hc: boolean;
  vc: boolean;
  bold: boolean;
  italic: boolean;
}
export interface SchInst {
  i: number; // position in the file (display order only); use `id` to refer to the instance
  id: string;
  x: number;
  y: number;
  rot: number;
  flip: number;
  name: string;
  symbol: string;
  type: string;
  bbox: [number, number, number, number];
  props: Record<string, string>;
}
export interface Schematic {
  kind: "schematic" | "symbol";
  bbox: [number, number, number, number];
  colors: string[];
  lines: [number, number, number, number, number][];
  boxes: [number, number, number, number, number, number][];
  polys: [number, number, number[]][];
  arcs: [number, number, number, number, number, number, number][];
  texts: SchText[];
  wires: [number, number, number, number, boolean, string, string][]; // ..., label, id
  insts: SchInst[];
  missing: string[];
  props: Record<string, string>;
  undo?: number;
  redo?: number;
  wire_nets?: string[];
  ports?: [string, string][];
  pins?: [string, string, number, number, string][]; // instance id, pin, x, y, net
  terminals?: [string, string, number, number][]; // all symbol pins, including ports and net labels
  warnings?: string[];
  pending?: [string, string, string, number, number, number, number][]; // in the netlist, not drawn: inst, pin, net, from x/y, to x/y
  placed_auto?: string[];   // instances placed by tools that no person has moved (tidy may re-place them)
  pinsOfSymbol?: [string, number, number][];
}
export interface SchEditBody {
  op: string;
  sym?: string; x?: number; y?: number; x2?: number; y2?: number; rot?: number; flip?: number;
  dx?: number; dy?: number; duplicate?: boolean; items?: { k: "i" | "w" | "t"; id: string }[];
  props?: Record<string, string | null>; kind?: string; id?: string; text?: string; lab?: string;
}
export interface SymbolEntry { lib: string; name: string; ref: string }
export interface SymbolPreview extends Omit<Schematic, "pins"> { pins: [string, number, number][] }
export interface SymbolObject {
  id: string; kind: "pin" | "line" | "rectangle" | "text" | "arc" | "polygon";
  bbox: [number, number, number, number]; x: number; y: number; x2?: number; y2?: number;
  name?: string; direction?: "in" | "out" | "inout"; order?: number; text?: string; size?: number; layer?: number;
  points?: number[]; radius?: number; start?: number; sweep?: number;
}
export interface SymbolModel extends Schematic { objects: SymbolObject[]; exists?: boolean }
export interface SymbolAffectedView { lib: string; cell: string; view: "schematic" | "symbol" }
export type SymbolEditBody = Partial<Omit<SymbolObject, "bbox">> & { op: string; ids?: string[]; dx?: number; dy?: number };
export interface InstanceDetail { symbol: string; props: Record<string, string>; template: Record<string, string>; type: string }
export interface NetlistResult { backend: string; text: string; warnings: string[]; ports: string[] }

export interface JobStatus {
  id: string;
  kind?: "sim" | "drc" | "lvs" | "symbol-check" | "rtl-sim" | "librelane";
  tool?: "klayout" | "magic" | "netgen" | "undine";
  title: string;
  state: "queued" | "running" | "done" | "failed" | "cancelled" | "interrupted" | "unknown";   // interrupted: no server finishes it
  created?: number;
  seconds?: number;
  error?: string;
  analysis?: unknown;
  replaced_control?: boolean;    // the panel's analysis replaced the schematic's .control block and analysis lines
  substitute?: Record<string, string>;  // sim: cells netlisted as stand-ins ("lib/cell" -> "lib/cell"; the testbench's default too)
  substitute_warnings?: string[];       // sim: stand-ins asked for that replaced nothing
  queue_position?: number;       // sim, queued: its place in line (the server runs a few simulations at once)
  time_limit_s?: number;         // sim: stopped when still running after this
  cell?: string;
  lib?: string;
  top?: string;                  // rtl-sim: the testbench cell
  phase?: "build" | "run";       // rtl-sim
  raw?: string[];
  log?: string;
  summary?: DrcSummary | LvsSummary | SymbolCheckSummary | RtlSimSummary | LibreLaneSummary;
  warnings?: string[];
  inputs?: JobInputs;            // what the job ran on (server/undine/store/provenance.py)
  key?: string;                  // hash of the inputs: equal keys, the same run
  files?: Record<string, string>; // SHA-256 of every file the job wrote
}
export interface JobInputs {
  design: { view: "layout" | "netlist" | "file" | "text" | "schematic-interface" | "symbol-interface"; lib?: string; cell?: string; path?: string; sha: string | null }[];
  pdk: string | null;
  deck: { kind: string; name: string; sha: string | null } | null;
  tools: Record<string, string | null>;
  options: Record<string, unknown>;
}
/** Whether the design, PDK, deck and tools are still what a job ran on (null: the job predates the record). */
export interface Freshness { fresh: boolean | null; changed: string[] }
export interface SignoffCheck {
  name: string; kind?: "drc" | "lvs"; tool?: string; deck?: string | null; key?: string; error?: string;
  job?: string; state?: JobStatus["state"]; fresh?: boolean; changed?: string[];
  summary?: DrcSummary | LvsSummary; passed?: boolean | null;
}
export interface DrcSummary { total: number; rules: number; categories: { id: number; name: string; description: string; count: number }[] }
export interface LvsSummary { verdict?: string; devices?: number[] | null; nets?: number[] | null; match: boolean; pin_errors?: string[]; circuits: { layout: string | null; schematic: string | null; status: string; nets_ok: number; nets_bad: number }[] }
export interface DrcGeom { t: string; bbox: number[]; pts: number[] }
export interface DrcItem { cat: string; bbox: number[]; geoms: DrcGeom[]; cell?: string }
export interface Pair { layout: string | null; schematic: string | null; status: string; class?: string }
export interface LvsResult extends LvsSummary {
  detail: { circuit?: string; nets: Pair[]; devices: Pair[]; pins: Pair[]; subcircuits: Pair[] };
}
export interface SymbolCheckSummary { match: boolean; total: number; schematic_ports: number; symbol_pins: number }
export interface SymbolCheckPort { name: string; direction: string; index: number; instance?: string }
export interface SymbolCheckIssue {
  code: string; severity: "error" | "warning"; message: string; name?: string;
  schematic_index?: number; symbol_index?: number; expected?: unknown; actual?: unknown;
}
export interface SymbolCheckResult {
  match: boolean; total: number; issues: SymbolCheckIssue[]; schematic_ports: SymbolCheckPort[]; symbol_pins: SymbolCheckPort[];
}
export interface Waveform {
  plot: string;
  complex: boolean;
  x: { name: string; unit: string; values: number[] };
  vectors: { name: string; type: string }[];
  traces: Record<string, { y?: number[]; mag_db?: number[]; phase_deg?: number[] }>;
  points: number;
}

export interface Effect {
  // n: a design intent entry (intent.py); l: a library created; o: the port order; r: a file's text (RTL, a flow configuration)
  k: "i" | "w" | "t" | "s" | "c" | "v" | "g" | "n" | "l" | "o" | "r";
  do: "add" | "del" | "mod";
  id?: string; id_after?: string; cell?: string; layer?: string; t?: string; s?: string; child?: string; inst?: string; trans?: string;
  lib?: string;
  file?: string;    // r of a flow configuration: flows/<name>.yaml
  kept?: boolean;   // a layout cell taken out with its content recorded (delete_unused_cells, delete_cell), or brought back
                    // with it; a schematic / symbol file deleted with its text (k "v", do "del": delete_cell)
  text?: string; obj?: Record<string, unknown>; changes?: Record<string, [unknown, unknown]>;
}
export interface ChangeOp {
  seq: number; op: string; cell: string | null; view: string; actor: string; reason: string | null; t: string; effects: Effect[];
  causes?: Record<string, number[]>; // layout follow-up (sdl_sync / sdl_generate): instance name -> schematic ops that made it necessary
}
/** A check slot of a changeset: kind drc (checker klayout: the PDK's deck, magic: Magic's drc(full)), lvs, or erc
 * (warnings from the LVS run's connectivity: severity "warning", ok false with findings, not a violation). */
export interface CheckRow {
  side: "work" | "base"; kind: "lvs" | "drc" | "erc" | "error"; lib: string; cell: string; job?: string;
  checker?: "klayout" | "magic";    // absent: a row recorded before checkers were named (KLayout's)
  state?: string; ok?: boolean | null; detail?: string; error?: string;
  summary?: { total?: number; match?: boolean; warnings?: number };
  fresh?: boolean | null; changed?: string[];
  finished?: string;                // when its job finished (local time)
  reused?: boolean;                 // the last run of the checks took a job that had run on the same inputs
  severity?: "warning";
  findings?: { instance: string; model: string; terminal: string; net: string; tap_net: string; edge_um: number; at: number[] }[];
}
/** A changeset on git (docs/design/changeset-branches.md): its branch, the commit it started from and its own commits. */
export interface ChangesetGit {
  branch: string;                   // undine/<project>/<id>
  base: string;                     // the base commit: the project's files when the changeset was opened
  head: string | null;              // the project's HEAD then (null: no commit yet)
  prefix: string;                   // the project directory within the repository
  tip: string | null;               // the branch's last commit (null: none yet)
  ref: string | null;               // after it was closed: where the branch is kept (refs/undine/changesets/...)
  commits: number;                  // base..tip
  base_seq?: Record<string, number>;
}
/** Apply / revert: the merge into the project, committed on the user's branch, or left in the working tree. */
export interface ChangesetMerge {
  t: string; by: string;
  commit?: string | null;           // the merge commit (null: not committed)
  uncommitted?: boolean;            // the merge is in the project's working tree, to commit
  // why it was left uncommitted: the repository has no commit yet (History makes the project's first commit), the
  // project had other uncommitted changes, the commit failed; null when committed; absent in an older record
  why?: "no_commit" | "changes" | "failed" | null;
  paths?: string[];                 // the files the merge wrote, from the project directory (the tabs showing them reload)
}
/** A file apply / revert could not merge (409 merge_conflict): its open conflicts; paths from the project directory. */
export interface ChangesetConflictFile { path: string; kind: string | null; conflicts: GitConflict[] | null; error?: string }
/** A changeset being adopted, discarded or reverted on the server now, and how far (server/undine/server/live.py,
 * Closings): waiting for another closure, committing its working copy, saving its evidence (the files and bytes done of
 * how many), merging, cleaning up. */
export interface ChangesetClosing {
  action: "apply" | "discard" | "revert"; by: string; since: string;
  phase: "waiting" | "committing" | "evidence" | "merging" | "cleanup";
  behind?: { id: string; title: string; action: "apply" | "discard" | "revert" } | null;   // waiting: the closure under way
  files?: number; files_total?: number; bytes?: number; bytes_total?: number;              // evidence
}
/** A closed changeset's evidence as it is now: its bytes, of them the waveforms a prune removes, its files; "shared",
 * "raw_shared": of the bytes and of the waveforms, those of files another name still holds (a working copy its closure
 * kept), which removing them from the evidence does not free yet. */
export interface EvidenceSize { bytes: number; raw: number; files: number; shared?: number; raw_shared?: number }
/** What prunes of an evidence's waveforms removed in all: the last one's time and person, how many files and bytes. */
export interface EvidencePruned { t: string; by: string; files: number; bytes: number }
export interface EvidenceSizes { bytes: number; raw: number; changesets: Record<string, EvidenceSize & { pruned: EvidencePruned | null }> }
export interface ChangesetInfo {
  id: string; title: string; reason: string; actor: string; created: string;
  status: "open" | "applied" | "discarded" | "reverted"; preview: string;
  changes?: { lib: string; ops: ChangeOp[] }[]; verify?: CheckRow[];
  applied?: ChangesetMerge | null; reverted?: ChangesetMerge;
  // "bytes": as captured; "size": now (a closed one's review); "pruned": its waveforms removed, their hashes kept
  evidence?: { path: string; captured: string; jobs: number; bytes?: number; size?: EvidenceSize; pruned?: EvidencePruned | null };
  closing?: ChangesetClosing;       // being adopted, discarded or reverted now
  writer?: Writer | null; // who is editing it now
  cleanup_warning?: string;
  retained?: boolean;              // a closed changeset's working copy is still there (its preview can be opened)
  git?: ChangesetGit;               // absent: a changeset made before changesets were git branches (review, discard)
  activity?: { head: number };      // open: the last committed change of its working copy (gseq)
}

export interface LlmStatus {
  policy: "both" | "cloud" | "server" | "none"; allowed: ("cloud" | "server")[];
  active: { kind: "cloud" | "server"; model: string; base_url: string | null; connection?: string; connection_label?: string };
  label: string; ready: boolean; cloud_key_set: boolean; server_url_default: string;
  connections: LlmConnection[];
}
/** A connection registered from the UI; its key never comes back, only the last 4 characters. */
export interface LlmConnection {
  id: string; label: string; provider: "anthropic" | "openai"; base_url: string; where: "cloud" | "server";
  models: string[]; added: string; key_hint: string | null;
}
export interface ChatItem { k: "i" | "w" | "n" | "s"; id?: string; desc?: string; label: string }
export interface ChatCtx { lib: string; cell: string; view: string; revision: string | null; selection: ChatItem[]; overview: boolean }
export interface ChatImage { media_type: string; data: string; label: string; width: number; height: number }
export interface ChatAction { tool: string; input: Record<string, unknown>; reason?: string | null; error: boolean; result: string }
export interface ChatTurn {
  role: "user" | "assistant"; text: string; t: string; actions?: ChatAction[]; images?: { sha1: string; label: string; media_type: string }[]; changeset?: string | null; mode?: string; summary?: { objects: string[]; sections: string[]; chars: number; cell: string };
  usage?: { input?: number; output?: number }; stop?: string;
  turn?: string; state?: TurnState;  // assistant: the turn that produced it; state other than done = it did not finish
}
export interface Conversation {
  id: string; created: string; title: string; connection: { kind: string; model: string }; messages: ChatTurn[];
}
export interface ChatReply {
  stale?: boolean; current?: string; message?: string;
  conversation?: string; reply?: string; usage?: { input?: number; output?: number }; stop?: string;
  connection?: { kind: string; model: string }; sent?: ChatTurn["summary"]; revision?: string;
  actions?: ChatAction[]; changeset?: string | null;
  turn?: string; state?: TurnState;
}
export interface Writer { owner: string; kind: "turn" | "mcp"; since: string; stop_requested: boolean; label: string | null }
/** /live: a changeset someone is writing now, or one being closed ("closing"; its writer null then). */
export interface LiveWriter { id: string; title: string; writer: Writer | null; closing?: ChangesetClosing }
/** What was committed in a changeset's working copy after a point (server: changeset activity). */
export interface Activity {
  records: { gseq: number; lib: string; cell: string; view: ViewKind; op: string; actor: string; turn: string | null }[];
  last: number; writer: Writer | null;
}
/** A changeset's operations in order (server/undine/store/changeset.py: timeline). */
export interface TimelineOp {
  opid: string; first: number; last: number; turn: string | null; actor: string; t: string;
  kind: "edit" | "undo" | "redo" | "restore"; ops: string[]; cells: string[]; reasons: string[]; effects: string[];
  effect_data?: { lib: string; cell: string | null; view: string | null; effect: Effect }[];
  in_effect: boolean; to?: number; of?: { gseq: number | null; op: string; cell: string | null };
}
export interface Timeline {
  ops: TimelineOp[]; turns: Record<string, { state: string | null; message: string }>; checkpoints: number[];
}
export type TurnState = "running" | "stopping" | "done" | "failed" | "stopped" | "interrupted";
/** Progress of an assistant turn (server/undine/agents/turns.py). */
export interface TurnEvent {
  i: number; t: string; type: "text" | "tool" | "changeset" | "state";
  text?: string; id?: string; name?: string; input?: Record<string, unknown>; reason?: string | null;
  state?: string; result?: string; gseq?: number[];
}
export interface Turn {
  id: string; conversation: string; state: TurnState; events: TurnEvent[];
  reply: string | null; changeset: string | null; error: string | null;
}

export interface PcellParam {
  name: string; label: string; type: "int" | "float" | "bool" | "string" | "list";
  default: unknown; unit?: string; readonly: boolean; choices: string[] | null; choice_values: string[] | null;
}
export interface PcellDecl { name: string; params: PcellParam[] }
export interface SdlDevice {
  name: string; symbol: string; index: number; type: string; kind: "pcell" | "cell" | null;
  pcell?: string; params?: Record<string, unknown>; layout_cell?: string | null; placed: boolean; bbox?: number[]; stale?: boolean; expected_cell?: string | null;
  nets: Record<string, string>;
}
export interface SdlStatus {
  cell: string; layout_exists: boolean; devices: SdlDevice[]; missing: string[]; stale: string[]; unsupported: string[]; extra: string[];
  pcells: boolean; pcells_reason?: string | null; ratsnest: { net: string; a: string; b: string; p: number[] }[]; warnings: string[];
}
export interface ProbeResult {
  insts: string[]; sch_insts: string[]; nets: string[]; wires: string[];
  layout: { name: string; cell: string; bbox: number[] }[];
}

// ------------------------------------------------------------------ git (server/undine/vcs/gitmerge.py); paths are
// relative to the project directory, except /git/verify's (merge-check's), which are from the repository's top

export type GitView = "schematic" | "symbol" | "layout" | "netlist" | "intent" | "rtl" | "other";
export type GitFileStatus = "modified" | "added" | "deleted" | "untracked" | "conflicted" | "renamed";
export interface GitCommit {
  sha: string; short: string; parents: string[]; subject: string; body: string;
  author: string; email: string; date: string; refs: string[];
}
export interface GitCellChange { lib: string; cell: string; views: { view: GitView; path: string; status: GitFileStatus; stamp?: string | null }[] }
export interface GitFileChange { path: string; status: GitFileStatus }
/** The repository as the project sees it: no repo (or a changeset's working copy, `preview`) leaves the rest empty.
 * A working copy names its changeset, branch and base commit instead (its own unsaved, stale and reads are filled). */
export interface GitStatus {
  repo: boolean; preview?: boolean;
  changeset?: string; base?: string | null;      // preview: the changeset, the commit it started from
  // what git-setup set up in this clone; gitignore: the lines of Undine's own output the project's .gitignore lacks
  // (a project set up before they were added: git lists that output as untracked), which only git-setup adds
  setup: { merge: boolean; diff: boolean; hook: boolean; gitignore?: string[] } | null;
  prefix: string | null;            // the project directory within the repository ("" at its top)
  branch: string | null;            // null: detached HEAD
  head: GitCommit | null;           // null: no commit yet
  operation: "merge" | "cherry-pick" | "rebase" | null;
  conflicted: string[] | null;
  cells: GitCellChange[] | null;    // working tree + index vs HEAD, untracked included
  other: GitFileChange[] | null;    // project.json, undine.lock, history/*.jsonl, ...
  unsaved: { lib: string; cells: string[] }[] | null;   // layout edits not saved to the cell files yet (no cell: they cancel out)
  // open layouts with unsaved edits whose cell files changed on disk since (a checkout, a merge, a pull): saving them is
  // refused until the edits are discarded (api.layoutReload)
  stale?: { lib: string; cells: string[] }[] | null;
  // how often each open layout read its cell files again because they changed on disk (its editors redraw then)
  reads?: Record<string, number>;
}
/** Why the project's first commit was refused, as POST .../git/first-commit answers it (git-setup's "commit_refused"):
 * unsaved, the logs whose layout edits are not saved to the cell files (the pre-commit hook refuses those too);
 * unsupported_library, a library a changeset's working copy cannot hold (ApiRefusal's fields). */
export interface GitCommitRefusal extends ApiRefusal {
  code: "unsaved" | "unsupported_library" | (string & {}); detail: string;
  logs?: { path: string; lib: string; cells: string[] }[];
}
/** git-setup's answer: the status, why the pre-commit hook was not installed (null: it was, or it is), whether it ran
 * git init, and the project's first commit made after it (null: no git init, or refused: commit_refused says why);
 * index_note: git's index was not reset to that commit (another git held it), what to run. Absent from an older server. */
export interface GitSetup extends GitStatus {
  hook_note: string | null; init?: boolean; commit?: string | null; commit_refused?: GitCommitRefusal | null;
  index_note?: string | null;
  gitignore_added?: string[];       // the lines it added to the project's .gitignore
}
export interface GitLogCommit extends GitCommit { cells: GitCellChange[]; other: number }
/** A view module's semantic diff (gitmerge.diff): object by object, with the merge's identities. */
export interface SemanticChange { do: "add" | "del" | "mod"; id: string; what: string; before: unknown; after: unknown }
export interface SemanticDiff {
  kind: string; changes?: SemanticChange[]; error?: string;
  do?: "add" | "del" | "mod";       // kind "text": a file of no view
  geometry?: { cell: string; layer: string; xor_um2: number; geometry_unchanged: boolean }[];   // layouts
  note?: string;
}
export interface GitDiff {
  base: string | null; rev: string | null;
  cells: { lib: string; cell: string; views: { view: GitView; path: string; status: GitFileStatus; diff: SemanticDiff }[] }[];
  other: GitFileChange[];
}
/** An open conflict of a file merged by objects: both sides' values (and the base's), the one the file holds now. */
export interface GitConflict {
  id: string; what: string; base?: unknown; ours?: unknown; theirs?: unknown; taken?: "ours" | "theirs"; resolved?: boolean;
}
/** A merge side as merge_check names it: "ours", "theirs", or "parent N" of an octopus merge. */
export type GitSide = "ours" | "theirs" | `parent ${number}`;
/** Why a cell is to be checked again: its own files were merged, or they are one side's while what is below differs
 * from that side (each such cell marked with the side it is from, null when merged itself), or resolves differently. */
export type GitVerifyReason =
  | { kind: "merged" }
  | { kind: "side"; side: GitSide; below: { lib: string; cell: string; side: GitSide | null }[] }
  | { kind: "resolves"; side: GitSide };
/** gitmerge.merge_check of a merge commit, or of the merge in progress: the cells nobody checked as merged, what the
 * merge reports said, and in progress the conflicts still open. Its paths are from the repository's top (the status'
 * `prefix` + a path from the project directory); `why` and `notes` are merge-check's English, for the command line. */
export interface GitVerify {
  ours: string; theirs: string | null; commit: string | null; parents: string[] | null; operation: string | null;
  unmerged: { path: string; kind: string | null; conflicts: GitConflict[] | null; error?: string }[];
  warnings: { path: string; warning: string }[]; merged: string[]; no_report: string[];
  verify: { lib: string; cell: string; why: string; reason?: GitVerifyReason }[]; notes: string[];
  note_codes?: { code: string; operation?: string }[];    // notes as codes: no_theirs, octopus, earlier (of operation)
}

/** What a refusal to open a changeset names besides its words (400): not_set_up's project and the command line that
 * sets it up (with the store's root in a store that is not the command's default); unsupported_library's library, its folder or the link in it (`path`, from the project directory), why a
 * changeset's working copy (a git worktree) cannot hold it, and where its link leads (`target`, as the link says). */
export interface ApiRefusal {
  project?: string; command?: string;
  library?: string; path?: string; kind?: "link_out" | "link_nowhere" | "own_repository" | (string & {}); target?: string | null;
}
const REFUSAL = ["project", "command", "library", "path", "kind", "target"] as const;

export class ApiError extends Error {
  constructor(readonly status: number, readonly detail: unknown, readonly code?: string, readonly params?: Record<string, unknown>,
    readonly files?: ChangesetConflictFile[],       // code merge_conflict (409): the files an apply / revert could not merge
    readonly top?: string,                          // code not_set_up (400): the repository not set up for Undine
    readonly refusal?: ApiRefusal) {                // codes not_set_up, unsupported_library (400)
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.name = "ApiError";
  }
}

/** The body's fields of a refusal (ApiRefusal), from its top level or, for a server that puts them there, its params. */
function refusalOf(payload: Record<string, unknown>): ApiRefusal | undefined {
  const params = (payload.params && typeof payload.params === "object" ? payload.params : {}) as Record<string, unknown>;
  const out: Record<string, string | null> = {};
  for (const k of REFUSAL) {
    const v = k in payload ? payload[k] : params[k];
    if (typeof v === "string") out[k] = v;
    else if (k === "target" && v === null) out[k] = null;          // own_repository: no link, nowhere it leads
  }
  return Object.keys(out).length ? out as ApiRefusal : undefined;
}

// ------------------------------------------------------------------ design intent (server/undine/design/intent.py)

export type IntentKind = "goal" | "preference" | "constraint" | "spec" | "context";
export interface IntentItem {
  id: string; kind: IntentKind; text: string; strength?: "must" | "should";
  rule?: { type: string; instances?: string[]; net?: string; [k: string]: unknown };
  spec?: { measure: string; min?: number; max?: number; testbench?: string; [k: string]: unknown };
  serves?: string[]; reason?: string; source?: IntentSource;
}
/** Who added an item and the user's words it came from; `requests`: their words that confirmed or extended it later,
 *  dated, oldest first (update_intent's request adds one; none is ever replaced). */
export interface IntentSource {
  by?: string; request?: string; turn?: string;
  requests?: { request: string; date?: string; by?: string; turn?: string }[];
}
export interface IntentPort { kind?: string; side?: string; reason?: string; decided_by?: string }
export interface Intent {
  version: number; notes: string; ports: Record<string, IntentPort>; supplies: Record<string, "supply" | "ground">; items: IntentItem[];
  views: Record<string, { items: IntentItem[] }>;   // view intent: schematic, symbol, rtl, layout, test:<testbench cell>
}
export interface IntentView {
  intent: Intent; yaml: string; sha?: string; readonly: boolean;
  problems: { level: "error" | "warning" | "info"; entry: string; message: string }[];
  requirements: Record<string, { entry: string; message: string }[]>;
  steps: Record<string, string>;
  refs: { instances: string[]; nets: string[]; ports: string[]; cells: string[] } | null;
  suggested_supplies: Record<string, "supply" | "ground">;
  undo: { undo: boolean; redo: boolean };
  result?: unknown;
}
export type IntentEdit =
  | { op: "add_item"; item: Partial<IntentItem>; view?: string } | { op: "update_item"; id: string; fields: Partial<IntentItem> }
  | { op: "remove_item"; id: string } | { op: "set_port"; name: string; fields: IntentPort | null }
  | { op: "set_supply"; net: string; role: string | null } | { op: "set_notes"; text: string }
  | { op: "replace"; text: string; base?: string } | { op: "undo" } | { op: "redo" };

// ------------------------------------------------------------------ the cell page (server/undine/store/cellviews.py)

export interface CellOverview {
  lib: string; cell: string; exists: boolean; readonly: boolean;
  intent: {
    notes: string; items: IntentItem[]; ports: number; supplies: Record<string, string>;
    views: Record<string, number>; problems: { error: number; warning: number; info: number }; missing: Record<string, number>;
  };
  design: { view: "schematic" | "symbol" | "rtl" | "layout"; role: "source"; exists: boolean; intent: number; fresh?: boolean; notes: string[] }[];
  tests: { lib: string; cell: string; why: string[]; exists: boolean; intent: number;
    simulation: ({ job: string; state: JobStatus["state"]; analysis?: unknown } & Freshness) | null }[];
  used_in: { lib: string; cell: string; why: string[] }[];
  derived: ({ view: "netlist"; from: string[]; available: boolean; on_demand: boolean } | { view: "checks"; from: string[]; checks: SignoffCheck[] })[];
}

// ------------------------------------------------------------------ RTL (server/undine/design/rtl.py)

export interface RtlInfo {
  text: string; sha?: string; file: string | null; modules: string[]; ports: [string, string][];
  undo: { undo: boolean; redo: boolean }; readonly: boolean; result?: unknown;
}
export interface RtlMessage { level: "warning" | "error"; code: string; file: string; line: number | null; col: number | null; message: string }
export interface RtlLint { ok: boolean; top: string; messages: RtlMessage[]; files: string[] }
export interface RtlSimSummary { printed: string[]; errors: string[]; finished_at: string | null; finish_line: string; warnings: string[] }

// ------------------------------------------------------------------ LibreLane (server/undine/design/librelane.py)

export interface Flow {
  name?: string; target: "librelane"; top: { lib?: string; cell?: string };
  clock: { port?: string; period_ns?: number }; floorplan: { core_util?: number; die_area_um?: number[] };
  variables: Record<string, unknown>; error?: string;
  source?: "rtl" | "schematic";   // schematic: the top's gate-level netlist as drawn (no synthesis), librelane.py
  // ChipFoundry's user project area: a macro for it, or user_project_wrapper with its macros (librelane.py)
  caravel?: { role: "macro" | "wrapper"; template?: string;
    macros?: { flow: string; instance: string; location: number[]; orientation: string; power: string }[] };
}
export interface LibreLaneAvailability { available: boolean; command?: string; reason?: string }
export interface Derived { variable: string; value: unknown; derived_from: string[] }
export interface FlowCheck {
  flow: Flow; missing: { entry: string; message: string }[]; librelane: LibreLaneAvailability;
  config?: Record<string, unknown>; pin_order?: string; derived?: Derived[];
  ignored?: { entry: string; value: unknown; message: string }[];   // intent entries the run does not use (a supply's side)
}
export interface NetlistKept {
  kept: boolean; added: string[]; removed: string[]; changed: string[]; physical: Record<string, number>;
  counts: { drawn: number; logical: number; added: number; removed: number; changed: number }; note?: string;
}
export interface LibreLaneSummary {
  metrics: Record<string, number>; gds: string | null; errors: string[]; hints?: string[];
  netlist?: NetlistKept;   // a run from a schematic: what it did to the drawn netlist
  signoff?: { violations: Record<string, number>; corners: Record<string, string[]> };   // max slew / cap / fanout
  ignored?: string[];
}
export interface LibreLaneResult { run: string | null; metrics?: Record<string, unknown>; key_metrics?: Record<string, number>; gds: string | null; derived: Derived[]; log: string }

async function req<T>(url: string, body?: unknown, method?: string): Promise<T> {
  const r = await fetch(url, body === undefined ? (method ? { method } : undefined) : {
    method: method ?? "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    let payload: { detail?: unknown; code?: string; params?: Record<string, unknown>; files?: unknown; top?: unknown } = {};
    try {
      payload = await r.json();
    } catch {
      /* not json */
    }
    if (!payload || typeof payload !== "object") payload = {};
    throw new ApiError(r.status, payload.detail ?? `${r.status}`, payload.code, payload.params,
      Array.isArray(payload.files) ? payload.files as ChangesetConflictFile[] : undefined,
      typeof payload.top === "string" ? payload.top : undefined, refusalOf(payload));
  }
  return r.json();
}

const enc = encodeURIComponent;
const P = (p: string) => `/api/p/${enc(p)}`;
const L = (p: string, l: string) => `${P(p)}/l/${enc(l)}`;
const LY = (p: string, l: string, c: string) => `${L(p, l)}/layout/${enc(c)}`;

export const api = {
  pdks: () => req<PdkInfo[]>("/api/pdks"),
  projects: () => req<ProjectInfo[]>("/api/projects"),
  newProject: (name: string, pdk: string | null) => req<ProjectInfo>("/api/projects", { name, pdk }),
  project: (p: string) => req<ProjectInfo & { libraries: LibraryInfo[] }>(P(p)),
  newLibrary: (p: string, name: string) => req<LibraryInfo>(`${P(p)}/libs`, { name }),
  // 409 pdk_incompatible (ApiError params.packages: incompatibleOf) when a locked package does not accept that PDK
  setPdk: (p: string, pdk: string) => req<{ from: string | null; to: string }>(`${P(p)}/pdk`, { pdk }),
  deps: (p: string) => req<DepsStatus>(`${P(p)}/deps`),
  // package: the [package] name the repository must have (left out when empty). Refusals: 409 resolution_refused
  // (problemsOf), 409 needs_lock_format_2, 409 not_synced (a locked package neither in the store nor installed)
  depAdd: (p: string, name: string, git: string, rev?: string, subdir?: string, pkg?: string) =>
    req<DepAdd>(`${P(p)}/deps`, { name, git, rev: rev || null, subdir: subdir || null, ...(pkg ? { package: pkg } : {}) }),
  // 400 not_direct (placedByOf) for a name a package placed
  depUpdate: (p: string, name: string, rev?: string) => req<DepUpdate>(`${P(p)}/deps/${enc(name)}/update`, { rev: rev || null }),
  depRemove: (p: string, name: string) => req<DepRemove>(`${P(p)}/deps/${enc(name)}/remove`, {}),
  // offline: install from the store only (null / left out: the server's UNDINE_OFFLINE)
  depsSync: (p: string, opts: { offline?: boolean | null } = {}) =>
    req<{ libraries: { name: string; action: "ok" | "installed" | "removed"; commit: string | null;
      package?: string;                // a package's names
      warning?: string;                // "CHAIN: does not fit the PDK ID: REASON": installed all the same
    }[] }>(`${P(p)}/deps/sync`, opts),
  depsLock: (p: string) => req<DepsStatus & { resolved: ResolvedDep[] }>(`${P(p)}/deps/lock`, {}),
  // trust (a person's, never an agent's): what each dependency would have refused, and whom the project trusts
  trustList: (p: string) => req<{ trusted: string[]; dependencies: TrustDependency[] }>(`${P(p)}/trust`)
    .then((r) => ({ ...r, dependencies: r.dependencies.map((d) => ({ ...d, findings: trustFindings(d.findings) })) })),
  // only after the person clicked Trust in the dialog that shows the findings: the token is for that key and tree, once
  trustConfirm: (p: string, name: string) =>
    req<{ token: string; expires: number; key: string; tree: string | null; findings: unknown }>("/api/confirmations", { kind: "trust", project: p, name })
      .then((r) => ({ ...r, findings: trustFindings(r.findings) })),
  trust: (p: string, name: string, token: string) => req<{ key: string; trusted: boolean }>(`${P(p)}/trust`, { name, token }),
  untrust: (p: string, name: string) => req<{ key: string; trusted: boolean }>(`${P(p)}/trust`, { name, untrust: true }),
  rescanPdks: () => req<{ pdks: PdkInfo[] }>("/api/pdks/rescan", {}),
  pdkFetchPlan: (p: string, allowNetwork = false) =>
    req<PdkFetchPlan>(`${P(p)}/pdk/fetch-plan?allow_network=${allowNetwork}`),
  // only after the person clicked Fetch in the dialog that shows the plan: the token is for that plan, once
  confirmPdkFetch: (p: string, c: { commit: string; variant: string; libraries: string[] | null; allow_network: boolean }) =>
    req<{ token: string; expires: number; plan: PdkFetchPlan }>("/api/confirmations", { kind: "pdk", project: p, ...c }),
  pdkFetch: (p: string, token: string) => req<{ job: PdkFetchJob }>(`${P(p)}/pdk/fetch`, { token }),
  pdkFetchStatus: (p: string, job: string) => req<PdkFetchJob>(`${P(p)}/pdk/fetch/${enc(job)}`),
  cells: (p: string, l: string) => req<CellInfo[]>(`${L(p, l)}/cells`),
  importFile: (p: string, l: string, path: string, view: ViewKind = "layout") =>
    req<{ new_cells: string[]; view: ViewKind }>(`${L(p, l)}/import`, { path, view }),
  newCell: (p: string, l: string, name: string) => req<EditResult<null>>(`${L(p, l)}/layout-cells`, { name }),
  newSchematic: (p: string, l: string, name: string) => req<{ name: string }>(`${L(p, l)}/schematics`, { name }),
  newCellViews: (p: string, l: string, name: string, views: string[]) =>
    req<{ name: string; created: ViewKind[] }>(`${L(p, l)}/cells`, { name, views }),

  schematic: (p: string, l: string, c: string, view: "schematic" | "symbol") =>
    req<Schematic>(`${L(p, l)}/c/${enc(c)}/${view}`),
  schEdit: (p: string, l: string, c: string, body: SchEditBody) =>
    req<{ result: unknown; view: Schematic; affected?: SymbolAffectedView[] }>(`${L(p, l)}/c/${enc(c)}/schematic/edit`, body),
  symbolView: (p: string, l: string, c: string) => req<SymbolModel>(`${L(p, l)}/c/${enc(c)}/symbol`),
  symbolEdit: (p: string, l: string, c: string, body: SymbolEditBody) =>
    req<{ result: unknown; view: SymbolModel; affected?: SymbolAffectedView[] }>(`${L(p, l)}/c/${enc(c)}/symbol/edit`, body),
  schCopy: (p: string, l: string, c: string, items: NonNullable<SchEditBody["items"]>) =>
    req<CopySnapshot<Schematic>>(`${L(p, l)}/c/${enc(c)}/schematic/copy`, { items }),
  schPaste: (p: string, l: string, c: string, body: PasteBody) =>
    req<{ result: string[]; view: Schematic }>(`${L(p, l)}/c/${enc(c)}/schematic/paste`, body),
  schRoute: (p: string, l: string, c: string, endpoints: { x: number; y: number; x2: number; y2: number }) =>
    req<{ points: [number, number][] }>(`${L(p, l)}/c/${enc(c)}/schematic/route`, endpoints),
  schMovePreview: (p: string, l: string, c: string, move: { items: NonNullable<SchEditBody["items"]>; dx: number; dy: number }) =>
    req<{ result: string[]; view: Schematic }>(`${L(p, l)}/c/${enc(c)}/schematic/move-preview`, move),
  schInstance: (p: string, l: string, c: string, id: string) =>
    req<InstanceDetail>(`${L(p, l)}/c/${enc(c)}/schematic/instance/${enc(id)}`),
  symbols: (p: string) => req<SymbolEntry[]>(`${P(p)}/symbols`),
  symbol: (p: string, ref: string) => req<SymbolPreview>(`${P(p)}/symbol?ref=${enc(ref)}`),
  netlist: (p: string, l: string, c: string, backend: string, fmt: string, mode: string) =>
    req<NetlistResult>(`${L(p, l)}/c/${enc(c)}/netlist?backend=${backend}&fmt=${fmt}&mode=${mode}`),
  tools: () => req<Record<string, boolean>>("/api/tools"),
  startSymbolCheck: (p: string, lib: string, cell: string) => req<JobStatus>(`${P(p)}/symbol-check`, { lib, cell }),
  symbolCheckResult: (p: string, job: string) => req<SymbolCheckResult>(`${P(p)}/jobs/${enc(job)}/symbol-check`),
  flows: (p: string) => req<{ flows: Flow[]; librelane: LibreLaneAvailability }>(`${P(p)}/flows`),
  saveFlow: (p: string, name: string, flow: Flow) => req<Flow>(`${P(p)}/flows/${enc(name)}`, flow, "PUT"),
  checkFlow: (p: string, name: string) => req<FlowCheck>(`${P(p)}/flows/${enc(name)}/check`),
  runFlow: (p: string, name: string) => req<JobStatus>(`${P(p)}/flows/${enc(name)}/run`, {}),
  librelaneResult: (p: string, job: string) => req<LibreLaneResult>(`${P(p)}/jobs/${enc(job)}/librelane`),
  importGds: (p: string, job: string) => req<{ new_cells: string[]; top: string }>(`${P(p)}/jobs/${enc(job)}/import-gds`, {}),
  rtl: (p: string, l: string, c: string) => req<RtlInfo>(`${L(p, l)}/c/${enc(c)}/rtl`),
  // save: `base` is the "sha" of the text it was edited from; a file that moved on since (git) refuses with 409
  rtlEdit: (p: string, l: string, c: string, op: "save" | "undo" | "redo", text = "", base?: string) =>
    req<RtlInfo>(`${L(p, l)}/c/${enc(c)}/rtl`, { op, text, ...(base ? { base } : {}) }),
  rtlLint: (p: string, l: string, c: string) => req<RtlLint>(`${L(p, l)}/c/${enc(c)}/rtl/lint`, {}),
  rtlSim: (p: string, lib: string, cell: string) => req<JobStatus>(`${P(p)}/rtl-sim`, { lib, cell }),
  intent: (p: string, l: string, c: string) => req<IntentView>(`${L(p, l)}/c/${enc(c)}/intent`),
  overview: (p: string, l: string, c: string) => req<CellOverview>(`${L(p, l)}/c/${enc(c)}/overview`),
  netlistEdit: (p: string, l: string, c: string, edit: { op: "connect" | "disconnect" | "add_instance" | "tidy" | "undo" | "redo"; [k: string]: unknown }) =>
    req<{ result: unknown; pending: { pin: string; net: string }[] }>(`${L(p, l)}/c/${enc(c)}/netlist/edit`, edit),
  intentEdit: (p: string, l: string, c: string, edit: IntentEdit & { reason?: string }) =>
    req<IntentView>(`${L(p, l)}/c/${enc(c)}/intent/edit`, edit),

  /** saveAll: store every node, not only the testbench's nets (a transient stores those and what its .meas names). */
  startSim: (p: string, lib: string, cell: string, analysis: Record<string, string> | null, saveAll = false) =>
    req<JobStatus>(`${P(p)}/sim`, { lib, cell, analysis, ...(saveAll ? { save_all: true } : {}) }),
  jobs: (p: string) => req<JobStatus[]>(`${P(p)}/jobs`),
  job: (p: string, id: string) => req<JobStatus>(`${P(p)}/jobs/${enc(id)}`),
  jobFreshness: (p: string, id: string) => req<Freshness>(`${P(p)}/jobs/${enc(id)}/freshness`),
  cancelJob: (p: string, id: string) => req<JobStatus>(`${P(p)}/jobs/${enc(id)}/cancel`, {}),
  jobData: (p: string, id: string, vars: string[] | null) =>
    req<Waveform>(`${P(p)}/jobs/${enc(id)}/data${vars ? `?vars=${enc(vars.join(","))}` : ""}`),
  decks: (p: string) => req<{ drc: string[]; lvs: string[]; extra: string[]; klayout: boolean; magic: boolean; netgen: boolean }>(`${P(p)}/verify/decks`),
  startDrc: (p: string, lib: string, cell: string, options: Record<string, boolean> = {}) =>
    req<JobStatus>(`${P(p)}/drc`, { lib, cell, options }),
  startLvs: (p: string, lib: string, cell: string, schematic_cell?: string, schematic_file?: string) =>
    req<JobStatus>(`${P(p)}/lvs`, { lib, cell, schematic_cell, schematic_file }),
  startMagicDrc: (p: string, lib: string, cell: string) => req<JobStatus>(`${P(p)}/magic-drc`, { lib, cell }),
  startNetgenLvs: (p: string, lib: string, cell: string, schematic_cell?: string, schematic_file?: string) =>
    req<JobStatus>(`${P(p)}/netgen-lvs`, { lib, cell, schematic_cell, schematic_file }),
  lvsText: async (p: string, id: string) => (await fetch(`${P(p)}/jobs/${enc(id)}/lvs-text`)).text(),
  drcItems: (p: string, id: string, cat: number | null, limit = 500) =>
    req<{ items: DrcItem[]; truncated: boolean }>(`${P(p)}/jobs/${enc(id)}/drc?limit=${limit}${cat != null ? `&cat=${cat}` : ""}`),
  lvsResult: (p: string, id: string, circuit?: string) =>
    req<LvsResult>(`${P(p)}/jobs/${enc(id)}/lvs${circuit ? `?circuit=${enc(circuit)}` : ""}`),

  changesets: (p: string) => req<ChangesetInfo[]>(`${P(p)}/changesets`),
  changeset: (p: string, id: string) => req<ChangesetInfo>(`${P(p)}/changesets/${enc(id)}`),
  evidenceSizes: (p: string) => req<EvidenceSizes>(`${P(p)}/changesets/evidence`),
  // "freed": of the bytes it removed, those no other name held (a working copy its closure kept frees the rest as it goes)
  pruneEvidence: (p: string, id: string) =>
    req<{ pruned: EvidencePruned & { freed?: number }; size: EvidenceSize }>(`${P(p)}/changesets/${enc(id)}/evidence/prune`, {}),
  // git_setup: the project was in no repository, and git-setup made it one, first_commit: its first commit then (its
  // files as they were; null: a commit of the person's made meanwhile is the proposal's start), first_commit_note: what
  // to do after it (git's index not reset to it) or why none was made; in a repository nobody set up for Undine, 400
  // with code not_set_up and ApiError.top; a library its working copy cannot hold, 400 with code unsupported_library,
  // before any git is made (ApiError.refusal: apiErrorText words both)
  openChangeset: (p: string, title: string, reason: string) =>
    req<ChangesetInfo & { git_setup?: boolean; first_commit?: string | null; first_commit_note?: string | null }>(
      `${P(p)}/changesets`, { title, reason }),
  // apply / revert: 409 with code merge_conflict and ApiError.files when the merge conflicts (nothing written)
  changesetAction: (p: string, id: string, action: "verify" | "apply" | "discard" | "revert") =>
    req<ChangesetInfo>(`${P(p)}/changesets/${enc(id)}/${action}`, {}),
  // the base against the working copy while open, against the kept tip after it was closed (400: no git)
  changesetDiff: (p: string, id: string, lib?: string, cell?: string) =>
    req<GitDiff>(`${P(p)}/changesets/${enc(id)}/diff${lib && cell ? `?lib=${enc(lib)}&cell=${enc(cell)}` : ""}`),

  gitStatus: (p: string) => req<GitStatus>(`${P(p)}/git`),
  gitSetup: (p: string) => req<GitSetup>(`${P(p)}/git/setup`, {}),
  // the project's first commit, in a repository with none (its files as they are, Undine's own output and the changeset
  // records left out): the status, the commit and index_note (git's index not reset to it: what to run); 409 with code
  // has_commits when the branch has one already, 409 unsaved (ApiError.params.logs, GitCommitRefusal) while layout edits
  // are not saved, 400 unsupported_library, 400 not_set_up in a repository not set up for Undine, 400 when in none
  gitFirstCommit: (p: string) =>
    req<GitStatus & { commit: string; index_note?: string | null }>(`${P(p)}/git/first-commit`, {}),
  gitLog: (p: string, o: { limit?: number; skip?: number; lib?: string; cell?: string } = {}) =>
    req<{ commits: GitLogCommit[]; more: boolean }>(`${P(p)}/git/log?limit=${o.limit ?? 50}&skip=${o.skip ?? 0}${
      o.lib && o.cell ? `&lib=${enc(o.lib)}&cell=${enc(o.cell)}` : ""}`),
  // rev "worktree": HEAD vs the working tree; a commit: its first parent vs it
  gitDiff: (p: string, rev: string, lib?: string, cell?: string) =>
    req<GitDiff>(`${P(p)}/git/diff?rev=${enc(rev)}${lib && cell ? `&lib=${enc(lib)}&cell=${enc(cell)}` : ""}`),
  // a merge commit; without rev, the merge (cherry-pick, rebase) in progress
  gitVerify: (p: string, rev?: string) => req<GitVerify>(`${P(p)}/git/verify${rev ? `?rev=${enc(rev)}` : ""}`),

  llm: () => req<LlmStatus>("/api/llm"),
  llmSwitch: (kind: string, model: string | null, base_url: string | null) => req<LlmStatus>("/api/llm/connection", { kind, model, base_url }),
  llmUse: (connection: string, model: string | null) => req<LlmStatus>("/api/llm/connection", { connection, model }),
  llmAdd: (c: { label: string; provider: string; api_key: string; base_url: string | null }) =>
    req<LlmStatus & { added: string; available_models: string[] }>("/api/llm/connections", c),
  llmRefreshModels: (id: string) => req<LlmStatus>(`/api/llm/connections/${enc(id)}/models`, {}),
  llmRemove: (id: string) => req<LlmStatus>(`/api/llm/connections/${enc(id)}`, undefined, "DELETE"),
  revision: (p: string, lib: string, cell: string) => req<{ revision: string }>(`${P(p)}/revision?lib=${enc(lib)}&cell=${enc(cell)}`),
  chats: (p: string) => req<{ id: string; title: string; created: string; connection: { kind: string; model: string }; turns: number }[]>(`${P(p)}/chats`),
  chatGet: (p: string, id: string) => req<Conversation>(`${P(p)}/chats/${enc(id)}`),
  chat: (p: string, message: string, conversation: string | null, context: ChatCtx, mode: "ask" | "propose" = "ask", images: ChatImage[] = [],
    client_id: string | null = null) =>
    req<ChatReply>(`${P(p)}/chat`, { message, conversation, context, mode, images, client_id }),
  turn: (p: string, id: string, after = 0) => req<Turn>(`${P(p)}/turns/${enc(id)}?after=${after}`),
  turnStop: (p: string, id: string) => req<Turn>(`${P(p)}/turns/${enc(id)}/stop`, {}),
  live: (p: string) => req<LiveWriter[]>(`${P(p)}/live`),
  activity: (p: string, id: string, after: number) => req<Activity>(`${P(p)}/changesets/${enc(id)}/activity?after=${after}`),
  timeline: (p: string, id: string) => req<Timeline>(`${P(p)}/changesets/${enc(id)}/timeline`),
  restore: (p: string, id: string, to: number) => req<ChangesetInfo>(`${P(p)}/changesets/${enc(id)}/restore`, { to }),
  writerStop: (p: string, changeset: string) => req<unknown>(`${P(p)}/changesets/${enc(changeset)}/writer/stop`, {}),
  llmImageUrl: (p: string, sha: string) => `${P(p)}/llm/images/${sha}`,
  pushSelection: (p: string, sel: { lib: string; cell: string; view: string; revision: string | null; selection: ChatItem[] }) =>
    req<{ ok: boolean }>(`${P(p)}/session/selection`, sel),
  chatRebind: (p: string, id: string, carry: boolean) => req<Conversation>(`${P(p)}/chats/${enc(id)}/rebind?carry=${carry}`, {}),

  tapeoutTargets: () => req<{ id: string; label: string; family: string | null; note: string; params: Record<string, string> }[]>("/api/tapeout/targets"),
  tapeoutCheck: (p: string, l: string, c: string, target: string, params: Record<string, string>) =>
    req<{ checks: { id: string; title: string; status: "pass" | "fail" | "warn" | "manual"; detail: string }[] }>(`${L(p, l)}/tapeout/${enc(c)}/check`, { target, params }),
  tapeoutSuite: (p: string, l: string, c: string, target?: string, reuse = true) =>
    req<{ jobs: SuiteJob[] }>(`${L(p, l)}/tapeout/${enc(c)}/suite`, { target, reuse }),
  signoff: (p: string, l: string, c: string, target?: string) =>
    req<{ checks: SignoffCheck[] }>(`${L(p, l)}/signoff/${enc(c)}${target ? `?target=${enc(target)}` : ""}`),
  tapeoutExport: (p: string, l: string, c: string, target: string, params: Record<string, string>) =>
    req<{ dir: string; zip: string; files: string[]; target: string; checks: { id: string; title: string; status: string; detail: string }[] }>(
      `${L(p, l)}/tapeout/${enc(c)}/export`, { target, params }),
  tapeoutDownloadUrl: (p: string, target: string, zip: string) => `${P(p)}/tapeout/download/${enc(target)}/${enc(zip)}`,

  pcells: (p: string) => req<{ available: boolean; pcells: PcellDecl[]; reason?: string }>(`${P(p)}/pcells`),
  placePcell: (p: string, l: string, c: string, pcell: string, params: Record<string, unknown>, x: number, y: number) =>
    req<EditResult & { cell: string }>(`${LY(p, l, c)}/pcell`, { pcell, params, x, y }),
  placeFrom: (p: string, l: string, c: string, fromLib: string, cell: string, x: number, y: number) =>
    req<EditResult>(`${LY(p, l, c)}/place-from`, { lib: fromLib, cell, x, y }),
  sdl: (p: string, l: string, c: string) => req<SdlStatus>(`${L(p, l)}/sdl/${enc(c)}`),
  sdlGenerate: (p: string, l: string, c: string, all: boolean) =>
    req<{ created: { name: string; cell: string }[]; errors: { name: string; error: string }[]; status: SdlStatus }>(
      `${L(p, l)}/sdl/${enc(c)}/generate?all=${all}`, {}),
  sdlSync: (p: string, l: string, c: string, names?: string[]) =>
    req<{ updated: { name: string; from: string; to: string }[]; unchanged: string[]; added: string[]; errors: { name: string; error: string }[] }>(
      `${L(p, l)}/sdl/${enc(c)}/sync${names?.length ? `?names=${enc(names.join(","))}` : ""}`, {}),
  probe: (p: string, l: string, c: string, q: { inst?: string; net?: string }) =>
    req<ProbeResult>(`${L(p, l)}/sdl/${enc(c)}/probe?${q.inst ? `inst=${enc(q.inst)}` : `net=${enc(q.net ?? "")}`}`),

  layout: (p: string, l: string, c: string) => req<LayoutInfo>(LY(p, l, c)),
  layoutDiff: (p: string, l: string, c: string, other: string) => req<import("./editors/diff").LayoutDiff>(`${LY(p, l, c)}/diff/${enc(other)}`),
  layers: (p: string, l: string, c: string) => req<LayerInfo[]>(`${LY(p, l, c)}/layers`),
  tileUrl: (p: string, l: string, c: string, level: number, ix: number, iy: number,
    version: number, mask: string | null, depth: number) =>
    `${LY(p, l, c)}/tile/${level}/${ix}/${iy}.png?v=${version}&d=${depth}${mask ? `&m=${mask}` : ""}`,
  integrity: (p: string, l: string, c: string) =>
    req<{ problems: { instance: string; problem: string; where: number[] }[] }>(`${LY(p, l, c)}/integrity`),
  select: (p: string, l: string, c: string, box: number[], point: boolean, mask: string | null, tol: number) =>
    req<SelItem[]>(`${LY(p, l, c)}/select`, { box, point, mask, tol }),
  create: (p: string, l: string, c: string, kind: string, layer: number, pts: number[], width = 0, text = "") =>
    req<EditResult>(`${LY(p, l, c)}/create`, { kind, layer, pts, width, text }),
  remove: (p: string, l: string, c: string, refs: Ref[]) => req<EditResult>(`${LY(p, l, c)}/delete`, { refs }),
  layoutCopy: (p: string, l: string, c: string, refs: Ref[]) =>
    req<CopySnapshot<SelItem[]>>(`${LY(p, l, c)}/copy`, { refs }),
  layoutPaste: (p: string, l: string, c: string, body: PasteBody) =>
    req<EditResult<SelItem[]>>(`${LY(p, l, c)}/paste`, body),
  move: (p: string, l: string, c: string, refs: Ref[], dx: number, dy: number, duplicate = false) =>
    req<EditResult>(`${LY(p, l, c)}/move`, { refs, dx, dy, duplicate }),
  place: (p: string, l: string, c: string, child: string, x: number, y: number, rot = 0, mirror = false) =>
    req<EditResult>(`${LY(p, l, c)}/place`, { child, x, y, rot, mirror }),
  // undo / redo the operations of one cell (the one the editor shows), never another cell's unseen
  undo: (p: string, l: string, c: string) => req<EditResult<string | null>>(`${L(p, l)}/undo?cell=${enc(c)}`, {}),
  redo: (p: string, l: string, c: string) => req<EditResult<string | null>>(`${L(p, l)}/redo?cell=${enc(c)}`, {}),
  save: (p: string, l: string) => req<EditResult<{ written: string[]; removed: string[] }>>(`${L(p, l)}/save`, {}),
  // drops the library's unsaved layout edits and reads its cell files again (GitStatus.stale)
  layoutReload: (p: string, l: string) => req<unknown>(`${L(p, l)}/reload`, {}),
};
