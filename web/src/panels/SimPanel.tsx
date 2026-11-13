import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Play, RotateCw, Square, Trash2, Loader2, CheckCircle2, XCircle } from "lucide-react";
import { api, Freshness, JobStatus, untrustedOf, Waveform } from "../api";
import { seriesColor, si, WaveformChart } from "./WaveformChart";
import { T } from "../theme";
import { apiErrorText, msg, resolveText, t, useI18n, type UiText } from "../i18n";
import { UntrustedNotice } from "../UntrustedNotice";

interface Props {
  project: string;
  lib: string;
  cell: string;
  toast: (m: UiText, k?: "error" | "info") => void;
}

type Kind = "netlist" | "tran" | "ac" | "dc" | "op";

const FIELDS: Record<Kind, { key: string; label: `field.${"step" | "stop" | "sweep" | "points" | "start" | "source"}`; def: string }[]> = {
  netlist: [],
  op: [],
  tran: [{ key: "step", label: "field.step", def: "50p" }, { key: "stop", label: "field.stop", def: "20n" }],
  ac: [{ key: "sweep", label: "field.sweep", def: "dec" }, { key: "points", label: "field.points", def: "20" },
    { key: "fstart", label: "field.start", def: "1" }, { key: "fstop", label: "field.stop", def: "1G" }],
  dc: [{ key: "source", label: "field.source", def: "Vin" }, { key: "start", label: "field.start", def: "0" },
    { key: "stop", label: "field.stop", def: "1.8" }, { key: "step", label: "field.step", def: "0.01" }],
};

export function jobStateText(state: string): string {
  const keys = { queued: "state.queued", running: "state.running", done: "state.done", failed: "state.failed", cancelled: "state.cancelled", interrupted: "state.interrupted", unknown: "state.unknown" } as const;
  const key = keys[state as keyof typeof keys];
  return key ? t(`verification:${key}`) : state;
}

export function StateIcon({ s }: { s: JobStatus["state"] }) {
  useI18n();
  const label = jobStateText(s);
  if (s === "running" || s === "queued") return <Loader2 size={13} className="spin" aria-label={label} role="img" />;
  if (s === "done") return <CheckCircle2 size={13} color={T.ok} aria-label={label} role="img" />;
  return <XCircle size={13} color={T.err} aria-label={label} role="img" />;
}

export function useJobs(project: string, kind: string | null, cell?: string, lib?: string) {
  const scope = JSON.stringify([project, kind, cell, lib]);
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const mounted = useRef(false);
  const request = useRef(0);
  const generation = useRef(0);
  const applied = useRef(0);
  const [loaded, setLoaded] = useState<{ scope: string; jobs: JobStatus[] }>({ scope, jobs: [] });
  // A new cell/project must never briefly inherit the previous view's job IDs.
  const jobs = useMemo(() => loaded.scope === scope ? loaded.jobs : [], [loaded, scope]);
  const refresh = useCallback(async () => {
    const sequence = ++request.current;
    const lifetime = generation.current;
    try {
      const all = await api.jobs(project);
      if (mounted.current && currentScope.current === scope && generation.current === lifetime && sequence > applied.current) {
        applied.current = sequence;
        setLoaded({ scope, jobs: all.filter((j) => (!kind || j.kind === kind) && (!cell || j.cell === cell) && (!lib || j.lib === lib)) });
      }
      return all;
    } catch { return []; }
  }, [project, kind, cell, lib, scope]);
  useEffect(() => {
    mounted.current = true;
    void refresh();
    return () => { mounted.current = false; generation.current++; };
  }, [refresh]);
  const busy = jobs.some((j) => j.state === "running" || j.state === "queued");
  useEffect(() => {
    if (!busy) return;
    const t = setInterval(refresh, 1200);
    return () => clearInterval(t);
  }, [busy, refresh]);
  return { jobs, refresh };
}

/** Whether a finished job still matches the design (asked again every few seconds while the page is visible, so an
 *  edit in any editor shows up). */
export function useFreshness(project: string, job: JobStatus | undefined | null): Freshness | null {
  const [f, setF] = useState<Freshness | null>(null);
  const id = job?.state === "done" ? job.id : null;
  useEffect(() => {
    setF(null);
    if (!id) return;
    let live = true;
    const ask = () => { if (!document.hidden) api.jobFreshness(project, id).then((r) => live && setF(r)).catch(() => {}); };
    ask();
    const t = setInterval(ask, 3000);
    return () => { live = false; clearInterval(t); };
  }, [project, id]);
  return f;
}

/** "Outdated: what changed since the run" with a button to run it again. */
export function Outdated({ f, onRerun }: { f: Freshness | null; onRerun?: () => void }) {
  useI18n();
  if (!f || f.fresh !== false) return null;
  return (
    <div className="note warnnote stale">
      <AlertTriangle size={13} />
      <div className="grow">
        <div>{t("verification:freshness.outdated")}</div>
        {f.changed.map((c) => <div key={c} className="small">{c}</div>)}
      </div>
      {onRerun && <button className="ghost small" onClick={onRerun}><RotateCw size={12} /> {t("verification:freshness.rerun")}</button>}
    </div>
  );
}

/** A launch the trust gate refused (403 untrusted, docs/design/packages.md Security: trust): what was not started, then
 *  the cell, the lines refused and Trust... (UntrustedNotice). Nothing for any other error, so a panel keeps a refusal
 *  here and toasts the rest. Trusting from the dialog calls `onTrusted`: the panel launches again. */
export function Refused({ project, error, what, onTrusted }: {
  project: string; error: unknown; what: UiText; onTrusted?: () => void | Promise<void>;
}) {
  useI18n();
  if (!untrustedOf(error)) return null;
  return (
    <div className="stack" data-testid="untrusted-refused">
      <div className="small">{resolveText(what)}</div>
      <UntrustedNotice project={project} error={error} onTrusted={onTrusted} />
    </div>
  );
}

/** Keeps an 'untrusted' refusal for <Refused>, toasting any other error. */
export function refusedOr(e: unknown, keep: (e: unknown) => void, toast: (m: UiText, k?: "error" | "info") => void) {
  if (untrustedOf(e)) keep(e);
  else toast(apiErrorText(e), "error");
}

export function SimPanel({ project, lib, cell, toast }: Props) {
  useI18n();
  const [kind, setKind] = useState<Kind>("netlist");
  const [vals, setVals] = useState<Record<string, string>>({});
  const [saveAll, setSaveAll] = useState(false);       // every node, not only the testbench's nets
  const { jobs, refresh } = useJobs(project, "sim", cell, lib);
  const [sel, setSel] = useState<string | null>(null);
  const [job, setJob] = useState<JobStatus | null>(null);
  const [wf, setWf] = useState<Waveform | null>(null);
  const [traces, setTraces] = useState<string[]>([]);
  const [filter, setFilter] = useState("");
  const [showLog, setShowLog] = useState(false);
  const [refused, setRefused] = useState<unknown>(null);   // the last run refused by the trust gate
  const loaded = useRef("");
  const fresh = useFreshness(project, job);
  useEffect(() => setRefused(null), [project, lib, cell]);

  useEffect(() => { if (!sel && jobs.length) setSel(jobs[0].id); }, [jobs, sel]);
  useEffect(() => {
    if (!sel) { setJob(null); return; }
    api.job(project, sel).then(setJob).catch(() => setJob(null));
  }, [sel, jobs, project]);

  // load waveform when a finished job is selected
  useEffect(() => {
    if (!job || job.state !== "done" || !job.raw?.length || loaded.current === job.id) return;
    loaded.current = job.id;
    api.jobData(project, job.id, null).then((w) => {
      const names = w.vectors.map((v) => v.name);
      const pick = names.filter((n) => /^v\((?!.*(#|\.))/.test(n)).slice(0, 3);
      const t = pick.length ? pick : names.slice(0, 2);
      setTraces(t);
      // fetch the picked traces here: when a re-run picks the same names, the traces effect below does not fire
      return api.jobData(project, job.id, t);
    }).then(setWf).catch((e) => toast(apiErrorText(e), "error"));
  }, [job, project, toast]);
  useEffect(() => {
    if (!job || job.state !== "done" || !wf || !traces.length) return;
    api.jobData(project, job.id, traces).then(setWf).catch(() => {});
  }, [traces.join(",")]); // eslint-disable-line

  const run = async () => {
    const analysis = kind === "netlist" ? null : { type: kind, ...Object.fromEntries(FIELDS[kind].map((f) => [f.key, vals[f.key] ?? f.def])) };
    try {
      const j = await api.startSim(project, lib, cell, analysis, saveAll && (kind === "tran" || kind === "netlist"));
      setRefused(null);
      loaded.current = "";
      setWf(null);
      setSel(j.id);
      await refresh();
    } catch (e) { setRefused(null); refusedOr(e, setRefused, toast); }
  };

  const vecs = useMemo(() => (wf?.vectors ?? []).filter((v) => !filter || v.name.toLowerCase().includes(filter.toLowerCase())), [wf, filter]);
  const series = (key: "y" | "mag_db" | "phase_deg") => traces.filter((t) => wf?.traces[t]?.[key]).map((t, i) => ({
    name: t, y: wf!.traces[t][key] as number[], color: seriesColor(i),
  }));
  const isAc = wf?.complex;

  return (
    <div className="inspector panel">
      <div className="section" style={{ marginTop: 0 }}>{t("verification:sim.analysis")}</div>
      <select value={kind} onChange={(e) => setKind(e.target.value as Kind)} className="full" aria-label={t("verification:sim.analysis")}>
        {(["netlist", "tran", "ac", "dc", "op"] as const).map((k) => <option key={k} value={k}>{t(`verification:sim.${k}`)}</option>)}
      </select>
      <div className="fieldgrid">
        {FIELDS[kind].map((f) => (
          <label key={f.key}><span>{t(`verification:${f.label}`)}</span>
            <input className="txt" value={vals[f.key] ?? f.def} onChange={(e) => setVals({ ...vals, [f.key]: e.target.value })} /></label>
        ))}
      </div>
      {(kind === "tran" || kind === "netlist") && (
        <label className="check small" title={t("verification:sim.saveAllHint")}>
          <input type="checkbox" checked={saveAll} onChange={(e) => setSaveAll(e.target.checked)} /> {t("verification:sim.saveAll")}
        </label>
      )}
      <button className="primary" onClick={run}><Play size={13} /> {t("verification:sim.run")}</button>
      <Refused project={project} error={refused} what={msg("verification:untrusted.sim")} onTrusted={run} />

      <div className="section">{t("verification:sim.jobs")}</div>
      {!jobs.length && <div className="empty" style={{ padding: 0 }}>{t("verification:sim.noRuns")}</div>}
      <div className="joblist">
        {jobs.slice(0, 12).map((j) => (
          <div key={j.id} className={`jobrow ${sel === j.id ? "cur" : ""}`} onClick={() => { setSel(j.id); loaded.current = ""; setWf(null); }}>
            <StateIcon s={j.state} />
            <span className="mono">{j.id.slice(9, 15)}</span>
            <span className="dim small grow">{typeof j.analysis === "object" ? String((j.analysis as { type: string }).type) : "netlist"}</span>
            {j.seconds != null && <span className="dim small">{j.seconds.toFixed(1)}s</span>}
            {j.state === "queued" && j.queue_position != null && (
              <span className="dim small" title={t("verification:sim.queuePosition", { count: j.queue_position })}>#{j.queue_position}</span>
            )}
            {(j.state === "running" || j.state === "queued") && (
              <button className="icon" title={t("verification:sim.cancel")} aria-label={t("verification:sim.cancel")} onClick={(e) => { e.stopPropagation(); api.cancelJob(project, j.id).then(refresh); }}><Square size={12} /></button>
            )}
          </div>
        ))}
      </div>

      {job && (
        <>
          <Outdated f={fresh} onRerun={run} />
          {(job.state === "failed" || job.state === "interrupted") && <div className="note err">{job.error}</div>}
          {job.replaced_control && <div className="note">{t("verification:sim.replacedControl")}</div>}
          {job.substitute && (
            <div className="note">{t("verification:sim.substitute", { cells: Object.entries(job.substitute).map(([a, b]) => `${a} → ${b}`).join(", ") })}</div>
          )}
          {job.substitute_warnings?.map((w) => <div key={w} className="note">{w}</div>)}
          {job.state === "done" && !job.raw?.length && <div className="note">{t("verification:sim.noRaw")}</div>}
          <button className="link small" onClick={() => setShowLog(!showLog)}>{t(showLog ? "verification:sim.hideLog" : "verification:sim.showLog")}</button>
          {showLog && <pre className="logbox">{job.log}</pre>}
        </>
      )}

      {wf && (
        <>
          <div className="section">{wf.plot}</div>
          {wf.points === 1 ? (
            <table className="optable"><tbody>
              {traces.filter((t) => wf.traces[t]?.y).map((t) => (
                <tr key={t}><td className="mono">{t}</td><td className="mono num">{si(wf.traces[t].y![0], 4)}</td></tr>
              ))}
            </tbody></table>
          ) : isAc ? (
            <>
              <WaveformChart x={wf.x.values} xlog series={series("mag_db")} xlabel={wf.x.name} ylabel="dB" height={150} />
              <WaveformChart x={wf.x.values} xlog series={series("phase_deg")} xlabel={wf.x.name} ylabel="deg" height={130} />
            </>
          ) : (
            <WaveformChart x={wf.x.values} series={series("y")} xlabel={wf.x.name} ylabel="V / A" />
          )}
          <input className="search full" placeholder={t("verification:sim.filter")} aria-label={t("verification:sim.filter")} value={filter} onChange={(e) => setFilter(e.target.value)} />
          <div className="veclist">
            {vecs.slice(0, 200).map((v) => (
              <label key={v.name} className="check vecrow">
                <input type="checkbox" checked={traces.includes(v.name)}
                  onChange={(e) => setTraces(e.target.checked ? [...traces, v.name] : traces.filter((t) => t !== v.name))} />
                <span className="mono grow">{v.name}</span>
                {traces.includes(v.name) && <i style={{ background: seriesColor(traces.indexOf(v.name)) }} className="swatch" />}
              </label>
            ))}
          </div>
          <div className="dim small">{wf.points === 1 ? t("verification:sim.opHint") : t("verification:sim.plotHint", { count: wf.points })}</div>
        </>
      )}
      <Trash2 size={0} />
    </div>
  );
}
