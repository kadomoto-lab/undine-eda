// The RTL view of a cell (server/undine/design/rtl.py): a Verilog / SystemVerilog editor, Verilator lint, testbench
// simulation with its output and waveforms, and the cell's design intent.
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CheckCircle2, Cpu, FileCode2, Play, Redo2, Save, Target, Undo2, XCircle } from "lucide-react";
import { api, type JobStatus, type RtlInfo, type RtlLint, type RtlSimSummary, type Waveform } from "../api";
import { apiErrorText, msg, t, useI18n, type UiText } from "../i18n";
import { Splitter } from "../layout";
import type { CodeEditorHandle } from "./CodeEditor";
import { IntentPanel, openCellPage } from "../panels/IntentPanel";
import { LibreLanePanel } from "../panels/LibreLanePanel";
import { seriesColor, WaveformChart } from "../panels/WaveformChart";
import { Refused, refusedOr, useJobs } from "../panels/SimPanel";

// CodeMirror loads with the first RTL view, not with the app
const CodeEditor = lazy(() => import("./CodeEditor").then((m) => ({ default: m.CodeEditor })));

export function RtlView({ project, lib, cell, readonly, rtlCells, active, onDirty, onLibraryChanged, toast }: {
  project: string; lib: string; cell: string; readonly: boolean; rtlCells: string[]; active: boolean;
  onDirty: (dirty: boolean) => void; onLibraryChanged: () => void; toast: (m: UiText, k?: "error" | "info") => void;
}) {
  useI18n();
  const [info, setInfo] = useState<RtlInfo | null>(null);
  const [text, setText] = useState("");
  const [panel, setPanel] = useState<"lint" | "sim" | "intent" | "librelane">("lint");
  const [lint, setLint] = useState<RtlLint | null>(null);
  const [linting, setLinting] = useState(false);
  const [lintRefused, setLintRefused] = useState<unknown>(null);   // Verilator refused: a dependency's RTL is not trusted
  const [tick, setTick] = useState(0);
  const editor = useRef<CodeEditorHandle>(null);

  const load = useCallback(() => {
    api.rtl(project, lib, cell).then((r) => { setInfo(r); setText(r.text); }).catch((e) => toast(apiErrorText(e), "error"));
  }, [project, lib, cell, toast]);
  useEffect(load, [load]);
  useEffect(() => setLintRefused(null), [project, lib, cell]);
  const dirty = !!info && text !== info.text;
  useEffect(() => onDirty(dirty), [dirty]); // eslint-disable-line
  const ro = readonly || !!info?.readonly;

  const runLint = useCallback(async () => {
    setLinting(true);
    setLintRefused(null);
    try { setLint(await api.rtlLint(project, lib, cell)); } catch (e) { refusedOr(e, (x) => { setLint(null); setLintRefused(x); }, toast); } finally { setLinting(false); }
  }, [project, lib, cell, toast]);

  const save = async () => {
    if (ro) return;
    if (!dirty) { toast(msg("rtl:unchanged"), "info"); return; }
    try {
      const r = await api.rtlEdit(project, lib, cell, "save", text, info?.sha);   // refused if git moved the file on
      setInfo(r);
      setText(r.text);
      setTick((n) => n + 1);
      toast(msg("rtl:saved"), "info");
      void runLint();
    } catch (e) { toast(apiErrorText(e), "error"); }
  };
  const step = async (op: "undo" | "redo") => {
    try { const r = await api.rtlEdit(project, lib, cell, op); setInfo(r); setText(r.text); setTick((n) => n + 1); }
    catch (e) { toast(apiErrorText(e), "error"); }
  };
  // Verilator's messages on this cell's file, drawn in the text
  const diagnostics = useMemo(() => (lint?.messages ?? []).filter((m) => m.line && m.file.startsWith(cell + "."))
    .map((m) => ({ line: m.line!, col: m.col, level: m.level, message: m.message, source: m.code })), [lint, cell]);

  return (
    <div className="editor rtleditor">
      <div className="rtlmain">
        <div className="rtltoolbar">
          <FileCode2 size={14} className="dim" />
          <span className="mono">{info?.file ?? `${cell}.sv`}</span>
          {dirty && <span className="tag warn">{t("rtl:unsaved")}</span>}
          <span className="grow" />
          <button className="icon" title={t("rtl:undo")} disabled={ro || dirty || !info?.undo.undo} onClick={() => step("undo")}><Undo2 size={14} /></button>
          <button className="icon" title={t("rtl:redo")} disabled={ro || dirty || !info?.undo.redo} onClick={() => step("redo")}><Redo2 size={14} /></button>
          <button className="ghost small" disabled={ro} onClick={save}><Save size={13} /> {t("rtl:save")}</button>
        </div>
        {info && !info.file && <div className="note">{t("rtl:noFile", { file: `${cell}.sv` })}</div>}
        {ro && <div className="note">{t("rtl:readOnly")}</div>}
        <div className="rtlcode">
          <Suspense fallback={<div className="codeeditor" />}>
            <CodeEditor key={`${lib}/${cell}`} ref={editor} value={text} onChange={setText} readOnly={ro} diagnostics={diagnostics}
              onSave={() => void save()} label={info?.file ?? `${cell}.sv`} />
          </Suspense>
        </div>
      </div>
      <Splitter pane="right" />
      <aside className="sidepanel">
        <div className="seg">
          <button className={panel === "lint" ? "on" : ""} onClick={() => setPanel("lint")}><CheckCircle2 size={14} /> {t("rtl:lint")}</button>
          <button className={panel === "sim" ? "on" : ""} onClick={() => setPanel("sim")}><Play size={14} /> {t("rtl:simulate")}</button>
          <button className={panel === "intent" ? "on" : ""} onClick={() => setPanel("intent")}><Target size={14} /> {t("intent:tab")}</button>
          <button className={panel === "librelane" ? "on" : ""} onClick={() => setPanel("librelane")}><Cpu size={14} /> {t("librelane:tab")}</button>
        </div>
        {panel === "lint" && (
          <div className="inspector panel">
            <div className="dim small">{t("rtl:lint.hint")}</div>
            <button className="primary" disabled={linting || !info?.file} onClick={runLint}>{linting ? <span className="spinner" /> : <CheckCircle2 size={13} />} {t("rtl:lint.run")}</button>
            <Refused project={project} error={lintRefused} what={msg("rtl:untrusted.lint")} onTrusted={runLint} />
            {lint && (
              <>
                <div className={lint.messages.length ? "warn small" : "ok small"}>
                  {lint.messages.length ? t("rtl:lint.count", { errors: lint.messages.filter((m) => m.level === "error").length, warnings: lint.messages.filter((m) => m.level === "warning").length }) : t("rtl:lint.ok")}
                </div>
                {lint.messages.map((m, i) => (
                  <button key={i} className={`rtlmsg ${m.level}`} onClick={() => m.file.startsWith(cell + ".") && m.line && editor.current?.goto(m.line, m.col)}>
                    <span className="mono small">{m.file}{m.line ? `:${m.line}` : ""} {m.code}</span>
                    <span>{m.message}</span>
                  </button>
                ))}
              </>
            )}
            {!!info?.ports.length && (
              <>
                <div className="section">{t("rtl:ports")}</div>
                <div className="childlist">{info.ports.map(([n, d]) => <span key={n} className="chip mono">{n} <span className="dim">{d}</span></span>)}</div>
              </>
            )}
          </div>
        )}
        {panel === "sim" && <RtlSimPanel project={project} lib={lib} cell={cell} rtlCells={rtlCells} active={active} toast={toast} />}
        {panel === "librelane" && <LibreLanePanel project={project} lib={lib} cell={cell} ports={(info?.ports ?? []).map(([n]) => n)}
          onOpenIntent={() => openCellPage(lib, cell)} onImported={onLibraryChanged} toast={toast} />}
        {panel === "intent" && <IntentPanel project={project} lib={lib} cell={cell} tick={tick} toast={toast} selection={[]} view="rtl" />}
      </aside>
    </div>
  );
}

function RtlSimPanel({ project, lib, cell, rtlCells, active, toast }: {
  project: string; lib: string; cell: string; rtlCells: string[]; active: boolean; toast: (m: UiText, k?: "error" | "info") => void;
}) {
  const benches = useMemo(() => [...new Set([...rtlCells.filter((c) => /^tb_|_tb$/.test(c)), cell, ...rtlCells])], [rtlCells, cell]);
  const [tb, setTb] = useState(() => benches.find((c) => c === `tb_${cell}`) ?? (/^tb_|_tb$/.test(cell) ? cell : benches[0] ?? cell));
  const { jobs, refresh } = useJobs(project, "rtl-sim", tb, lib);
  const [shown, setShown] = useState<string | null>(null);
  const job: JobStatus | undefined = jobs.find((j) => j.id === shown) ?? jobs[0];
  const [wave, setWave] = useState<Waveform | null>(null);
  const [refused, setRefused] = useState<unknown>(null);   // Verilator refused: a dependency's RTL is not trusted
  useEffect(() => setRefused(null), [project, lib, tb]);

  useEffect(() => {
    setWave(null);
    if (!active || job?.state !== "done") return;
    api.jobData(project, job.id, null).then(setWave).catch(() => setWave(null));
  }, [project, job?.id, job?.state, active]);

  const run = async () => {
    setRefused(null);
    try { const j = await api.rtlSim(project, lib, tb); setShown(j.id); await refresh(); } catch (e) { refusedOr(e, setRefused, toast); }
  };
  const s = job?.summary as RtlSimSummary | undefined;
  return (
    <div className="inspector panel">
      <div className="dim small">{t("rtl:sim.hint")}</div>
      <div className="row">
        <label className="grow"><span className="dim small">{t("rtl:sim.testbench")} </span>
          <select value={tb} onChange={(e) => { setTb(e.target.value); setShown(null); }}>
            {benches.map((c) => <option key={c} value={c}>{c}</option>)}
          </select></label>
        <button className="primary" onClick={run}><Play size={13} /> {t("rtl:sim.run")}</button>
      </div>
      <Refused project={project} error={refused} what={msg("rtl:untrusted.sim")} onTrusted={run} />
      {!job && <div className="dim small">{t("rtl:sim.none")}</div>}
      {job && (job.state === "running" || job.state === "queued") && (
        <div className="dim small"><span className="spinner" /> {job.phase === "run" ? t("rtl:sim.running") : t("rtl:sim.building")}</div>
      )}
      {job && (job.state === "done" || job.state === "failed") && (
        <>
          <div className={job.state === "done" ? "ok" : "err"}>
            {job.state === "done" ? <CheckCircle2 size={13} /> : <XCircle size={13} />} {job.state === "done" ? t("rtl:sim.passed") : t("rtl:sim.failed")}
            {s?.finished_at && <span className="dim small"> · {t("rtl:sim.finished", { time: s.finished_at })}</span>}
          </div>
          {!!s?.errors.length && (<><div className="section">{t("rtl:sim.errors")}</div>
            {s.errors.map((e, i) => <div key={i} className="note err mono small">{e}</div>)}</>)}
          {!s?.errors.length && job.error && <div className="note err mono small">{job.error}</div>}
          {!!s?.printed.length && (<><div className="section">{t("rtl:sim.printed")}</div>
            <pre className="rtlout mono small">{s.printed.join("\n")}</pre></>)}
          {!!s?.warnings.length && (<><div className="section">{t("rtl:sim.warnings")}</div>
            {s.warnings.map((w, i) => <div key={i} className="dim mono small">{w}</div>)}</>)}
        </>
      )}
      {wave && wave.x.values.length > 1 && (
        <>
          <div className="section">{t("rtl:sim.signals")}</div>
          {Object.entries(wave.traces).map(([name, tr], i) => (
            <div key={name}>
              <div className="mono small">{name}</div>
              <WaveformChart x={wave.x.values} height={70} xlabel="s" ylabel=""
                series={[{ name, y: (tr.y ?? []).map((v) => (v == null ? NaN : v)), color: seriesColor(i) }]} />
            </div>
          ))}
        </>
      )}
      {jobs.length > 1 && (
        <>
          <div className="section">{t("rtl:sim.history")}</div>
          {jobs.slice(0, 8).map((j) => (
            <button key={j.id} className={`rtlmsg ${j.state === "failed" ? "error" : ""} ${j.id === job?.id ? "cur" : ""}`} onClick={() => setShown(j.id)}>
              <span className="mono small">{j.id}</span><span>{j.state}</span>
            </button>
          ))}
        </>
      )}
    </div>
  );
}
