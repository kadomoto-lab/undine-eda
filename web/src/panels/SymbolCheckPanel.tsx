import { useEffect, useRef, useState } from "react";
import { CheckCircle2, Loader2, Play, XCircle } from "lucide-react";
import { api, type JobStatus, type SymbolCheckPort, type SymbolCheckResult, type SymbolCheckIssue } from "../api";
import { apiErrorText, t, useI18n, type UiText } from "../i18n";
import { Outdated, StateIcon, jobStateText, useFreshness, useJobs } from "./SimPanel";

interface Props { project: string; lib: string; cell: string; toast: (m: UiText, kind?: "error" | "info") => void }

export function SymbolCheckPanel({ project, lib, cell, toast }: Props) {
  useI18n();
  const { jobs, refresh } = useJobs(project, "symbol-check", cell, lib);
  const [selected, setSelected] = useState<string | null>(null);
  const [result, setResult] = useState<SymbolCheckResult | null>(null);
  const [starting, setStarting] = useState(false);
  const [startedJob, setStartedJob] = useState<JobStatus | null>(null);
  const scope = `${project}/${lib}/${cell}`;
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const job = jobs.find((j) => j.id === selected) ?? (startedJob?.id === selected ? startedJob : null);
  const fresh = useFreshness(project, job);
  useEffect(() => { setSelected(null); setStartedJob(null); setResult(null); setStarting(false); }, [scope]);
  useEffect(() => { if (!selected && jobs.length) setSelected(jobs[0].id); }, [jobs, selected]);
  useEffect(() => {
    let live = true;
    setResult(null);
    if (job?.state === "done") api.symbolCheckResult(project, job.id).then((r) => { if (live) setResult(r); })
      .catch((e) => { if (live) toast(apiErrorText(e), "error"); });
    return () => { live = false; };
  }, [project, job?.id, job?.state, toast]);
  const run = async () => {
    if (starting) return;
    setStarting(true);
    try {
      const created = await api.startSymbolCheck(project, lib, cell);
      if (currentScope.current !== scope) return;
      setStartedJob(created); setSelected(created.id); setResult(null);
      await refresh();
    } catch (e) { if (currentScope.current === scope) toast(apiErrorText(e), "error"); }
    finally { if (currentScope.current === scope) setStarting(false); }
  };
  const busy = starting || job?.state === "running" || job?.state === "queued";
  return <div className="inspector panel">
    <div className="section" style={{ marginTop: 0 }}>{t("symbolcheck:title")}</div>
    <p className="dim small">{t("symbolcheck:description")}</p>
    <button className="primary full" disabled={busy} onClick={() => void run()}>
      {busy ? <Loader2 size={13} className="spin" /> : <Play size={13} />} {t("symbolcheck:run")}
    </button>
    <div className="section">{t("symbolcheck:runs")}</div>
    {jobs.length ? <select className="full mono small" aria-label={t("symbolcheck:runs")} value={selected ?? ""} onChange={(e) => setSelected(e.target.value)}>
      {jobs.map((j) => <option key={j.id} value={j.id}>{j.id} · {jobStateText(j.state)}</option>)}
    </select> : <div className="dim small">{t("symbolcheck:empty")}</div>}
    {job && <>
      <Outdated f={fresh} onRerun={busy ? undefined : () => void run()} />
      {job.state !== "done" && <div className="statusline"><StateIcon s={job.state} /> {jobStateText(job.state)}</div>}
      {job.error && <pre className="logbox err">{job.error}</pre>}
    </>}
    {result && <>
      <div className={`verdict ${result.match ? "good" : "bad"}`} style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        {result.match ? <CheckCircle2 size={19} /> : <XCircle size={19} />}
        <b>{t(result.match ? "symbolcheck:match" : "symbolcheck:mismatch")}</b>
        <span className="dim small">{t("symbolcheck:issues", { count: result.issues.length })}</span>
      </div>
      {result.issues.map((issue, i) => <div key={i} className={`note ${issue.severity === "error" ? "warnnote" : ""}`}>
        <div><b className="mono">{issue.name ?? ""}</b><div className="small">{issueText(issue)}</div>
          {issue.expected !== undefined && <div className="small">{t("symbolcheck:expected")}: {String(issue.expected)}</div>}
          {issue.actual !== undefined && <div className="small">{t("symbolcheck:actual")}: {String(issue.actual)}</div>}
        </div>
      </div>)}
      <Ports title={t("symbolcheck:schematic")} ports={result.schematic_ports} />
      <Ports title={t("symbolcheck:symbol")} ports={result.symbol_pins} />
    </>}
  </div>;
}

function issueText(issue: SymbolCheckIssue): string {
  const codes = ["missing_schematic", "missing_symbol", "unresolved_symbol", "unnamed_schematic_pin", "unnamed_symbol_pin",
    "duplicate_schematic_pin", "duplicate_symbol_pin", "invalid_direction", "missing_symbol_pin", "extra_symbol_pin",
    "direction_mismatch", "pin_order_mismatch"] as const;
  const code = codes.find((c) => c === issue.code);
  return code ? t(`symbolcheck:${code}`) : issue.message;
}

function Ports({ title, ports }: { title: string; ports: SymbolCheckPort[] }) {
  return <><div className="section">{title}</div>{ports.length ? <table className="small" style={{ width: "100%", textAlign: "left" }}>
    <thead><tr><th>{t("symbolcheck:order")}</th><th>{t("symbolcheck:name")}</th><th>{t("symbolcheck:direction")}</th></tr></thead>
    <tbody>{ports.map((p, i) => <tr key={i}><td>{p.index}</td><td className="mono">{p.name}</td><td>{p.direction === "in" || p.direction === "out" || p.direction === "inout" ? t(`symbolcheck:${p.direction}`) : p.direction}</td></tr>)}</tbody>
  </table> : <div className="dim small">{t("symbolcheck:missing")}</div>}</>;
}
