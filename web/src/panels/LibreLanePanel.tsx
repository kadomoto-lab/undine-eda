// LibreLane for a cell (server/undine/design/librelane.py), from its RTL or (source schematic) from its schematic of
// standard cells: its flow configuration, what a run still needs, the settings derived from the flow and the design
// intent (with their sources) and the intent entries a run does not use, runs with results, and the final GDS.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Download, Play, Save } from "lucide-react";
import { api, type Flow, type FlowCheck, type LibreLaneAvailability, type LibreLaneResult, type LibreLaneSummary } from "../api";
import { apiErrorText, msg, t, useI18n, type UiText } from "../i18n";
import { useJobs } from "./SimPanel";

export function LibreLanePanel({ project, lib, cell, ports, source, onOpenIntent, onImported, toast }: {
  project: string; lib: string; cell: string; ports: string[]; onOpenIntent: () => void; onImported: () => void;
  source?: "schematic";     // a new flow builds the cell from its schematic (a cell of standard cells without RTL)
  toast: (m: UiText, k?: "error" | "info") => void;
}) {
  useI18n();
  const [flows, setFlows] = useState<Flow[] | null>(null);
  const [avail, setAvail] = useState<LibreLaneAvailability | null>(null);
  const [draft, setDraft] = useState<Flow | null>(null);
  const [check, setCheck] = useState<FlowCheck | null>(null);
  const { jobs, refresh } = useJobs(project, "librelane", cell, lib);
  const [result, setResult] = useState<LibreLaneResult | null>(null);

  const mine = flows?.find((f) => f.top.lib === lib && f.top.cell === cell);
  const name = mine?.name ?? cell;
  const load = useCallback(async () => {
    try { const r = await api.flows(project); setFlows(r.flows); setAvail(r.librelane); } catch (e) { toast(apiErrorText(e), "error"); }
  }, [project, toast]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { setDraft(mine ? { ...mine } : null); }, [mine?.name, JSON.stringify(mine)]); // eslint-disable-line
  const recheck = useCallback(() => {
    if (!mine) { setCheck(null); return; }
    api.checkFlow(project, name).then(setCheck).catch((e) => toast(apiErrorText(e), "error"));
  }, [project, name, mine, toast]);
  useEffect(recheck, [recheck]);

  const job = jobs[0];
  useEffect(() => {
    setResult(null);
    if (!job) return;
    api.librelaneResult(project, job.id).then(setResult).catch(() => setResult(null));
  }, [project, job?.id, job?.state]); // eslint-disable-line
  useEffect(() => {                                  // the log grows while LibreLane runs
    if (!job || (job.state !== "running" && job.state !== "queued")) return;
    const timer = setInterval(() => { api.librelaneResult(project, job.id).then(setResult).catch(() => {}); }, 3000);
    return () => clearInterval(timer);
  }, [project, job?.id, job?.state]); // eslint-disable-line

  const create = async () => {
    const clk = ports.find((p) => /^(clk|clock|clk_i|i_clk)$/i.test(p));
    const flow: Flow = { target: "librelane", top: { lib, cell }, clock: clk ? { port: clk, period_ns: 20 } : { port: "none" },
      floorplan: { core_util: 40 }, variables: {}, ...(source ? { source } : {}) };
    try { await api.saveFlow(project, cell, flow); await load(); } catch (e) { toast(apiErrorText(e), "error"); }
  };
  const save = async () => {
    if (!draft) return;
    try { await api.saveFlow(project, name, draft); toast(msg("librelane:saved"), "info"); await load(); recheck(); }
    catch (e) { toast(apiErrorText(e), "error"); }
  };
  const run = async () => {
    try { await api.runFlow(project, name); await refresh(); } catch (e) { toast(apiErrorText(e), "error"); }
  };
  const importGds = async () => {
    if (!job) return;
    try { const r = await api.importGds(project, job.id); toast(msg("librelane:imported", { count: r.new_cells.length, lib }), "info"); onImported(); }
    catch (e) { toast(apiErrorText(e), "error"); }
  };
  const summary = job?.summary as LibreLaneSummary | undefined;
  const derived = useMemo(() => check?.derived ?? [], [check]);

  return (
    <div className="inspector panel">
      <b>{t(source ? "librelane:titleSchematic" : "librelane:title")}</b>
      <div className="dim small">{t(source ? "librelane:descriptionSchematic" : "librelane:description")}</div>
      {avail && (avail.available ? <div className="dim small mono">{t("librelane:available", { command: avail.command ?? "" })}</div>
        : <div className="note warnnote small">{t("librelane:unavailable", { reason: avail.reason ?? "" })}</div>)}
      {flows && !mine && (
        <>
          <div className="dim small">{t("librelane:noFlow")}</div>
          <button className="primary" onClick={create}>{t("librelane:create")}</button>
        </>
      )}
      {draft && (
        <>
          <div className="section">{t("librelane:flow")} <span className="mono">{name}</span></div>
          {draft.source === "schematic" && <div className="note small">{t("librelane:fromSchematic")}</div>}
          {draft.caravel?.role === "macro" && <div className="note small">{t("librelane:caravelMacro")}</div>}
          {draft.caravel?.role === "wrapper" && (
            <div className="note small">{t("librelane:caravelWrapper", { template: draft.caravel.template ?? "" })}
              {(draft.caravel.macros ?? []).map((m) => (
                <div key={m.instance} className="mono">{t("librelane:caravelPlaced", { instance: m.instance, flow: m.flow, x: m.location[0], y: m.location[1], orientation: m.orientation })}</div>
              ))}
            </div>
          )}
          <div className="fieldgrid">
            <label><span>{t("librelane:clockPort")}</span>
              <select value={draft.clock.port ?? ""} onChange={(e) => setDraft({ ...draft, clock: { ...draft.clock, port: e.target.value } })}>
                <option value="none">{t("librelane:clock.none")}</option>
                {ports.map((p) => <option key={p} value={p}>{p}</option>)}
              </select></label>
            {draft.clock.port !== "none" && (
              <label><span>{t("librelane:period")}</span>
                <input className="txt" value={draft.clock.period_ns ?? ""} onChange={(e) => setDraft({ ...draft, clock: { ...draft.clock, period_ns: Number(e.target.value) || undefined } })} /></label>
            )}
            <label><span>{t("librelane:coreUtil")}</span>
              <input className="txt" value={draft.floorplan.core_util ?? ""} disabled={!!draft.floorplan.die_area_um}
                onChange={(e) => setDraft({ ...draft, floorplan: { core_util: Number(e.target.value) || undefined } })} /></label>
            <label><span>{t("librelane:dieSize")}</span>
              <input className="txt" defaultValue={draft.floorplan.die_area_um ? `${draft.floorplan.die_area_um[2]} × ${draft.floorplan.die_area_um[3]}` : ""}
                placeholder="100 × 100" onBlur={(e) => {
                  const m = e.target.value.match(/^\s*([\d.]+)\s*[x×*]\s*([\d.]+)\s*$/);
                  setDraft({ ...draft, floorplan: m ? { die_area_um: [0, 0, Number(m[1]), Number(m[2])] } : { core_util: draft.floorplan.core_util ?? 40 } });
                }} /></label>
          </div>
          <button className="ghost small" onClick={save}><Save size={13} /> {t("librelane:save")}</button>
        </>
      )}
      {!!check?.missing.length && (
        <>
          <div className="section">{t("librelane:missing")}</div>
          {check.missing.map((m, i) => <div key={i} className="note warnnote small">{m.message}</div>)}
          {check.missing.some((m) => m.entry.startsWith("ports.")) && <button className="ghost small" onClick={onOpenIntent}>{t("librelane:openIntent")}</button>}
        </>
      )}
      {!!check?.ignored?.length && (
        <>
          <div className="section">{t("librelane:ignored")}</div>
          {check.ignored.map((m) => <div key={m.entry} className="note small">{m.message}</div>)}
        </>
      )}
      {!!derived.length && (
        <>
          <div className="section">{t("librelane:derived")}</div>
          <div className="dim small">{t("librelane:derivedHint")}</div>
          <table className="derived small">
            <thead><tr><th>{t("librelane:variable")}</th><th>{t("librelane:value")}</th><th>{t("librelane:from")}</th></tr></thead>
            <tbody>{derived.map((d) => (
              <tr key={d.variable}><td className="mono">{d.variable}</td><td className="mono">{JSON.stringify(d.value)}</td><td className="mono dim">{d.derived_from.join(", ")}</td></tr>
            ))}</tbody>
          </table>
        </>
      )}
      {mine && (
        <button className="primary" disabled={!avail?.available || !!check?.missing.length || job?.state === "running" || job?.state === "queued"} onClick={run}>
          <Play size={13} /> {t("librelane:run")}
        </button>
      )}
      {job && (job.state === "running" || job.state === "queued") && (
        <>
          <div className="dim small"><span className="spinner" /> {t("librelane:running")}</div>
          {result?.log && <pre className="rtlout mono small">{result.log.split("\n").slice(-12).join("\n")}</pre>}
        </>
      )}
      {job && (job.state === "done" || job.state === "failed") && (
        <>
          <div className={job.state === "done" ? "ok" : "err"}>{job.state === "done" ? t("librelane:done") : t("librelane:failed")} <span className="dim small mono">{job.id}</span></div>
          {!!summary && Object.keys(summary.metrics).length > 0 && (
            <>
              <div className="section">{t("librelane:metrics")}</div>
              {Object.entries(summary.metrics).map(([k, v]) => <div key={k} className="kv"><span>{k}</span><b className="mono">{String(v)}</b></div>)}
            </>
          )}
          {summary?.netlist && (
            <div className={summary.netlist.kept ? "note small" : "note warnnote small"}>
              {summary.netlist.kept ? t("librelane:netlistKept", { count: summary.netlist.counts.drawn })
                : t("librelane:netlistChanged", { added: summary.netlist.counts.added, removed: summary.netlist.counts.removed, changed: summary.netlist.counts.changed })}
              {[...summary.netlist.added, ...summary.netlist.removed, ...summary.netlist.changed].slice(0, 8).map((x) => <div key={x} className="mono">{x}</div>)}
            </div>
          )}
          {!!summary?.errors.length && (<><div className="section">{t("librelane:errors")}</div>
            {summary.errors.map((e, i) => <div key={i} className="note err mono small">{e}</div>)}</>)}
          {/* hints: what the errors usually mean, and the signoff violations a finished run still has */}
          {(summary?.hints ?? []).map((h, i) => <div key={`h${i}`} className="note warnnote small">{h}</div>)}
          {job.state === "failed" && result?.log && (<><div className="section">{t("librelane:log")}</div>
            <pre className="rtlout mono small">{result.log.split("\n").slice(-30).join("\n")}</pre></>)}
          {summary?.gds && (
            <>
              <div className="dim small mono">{t("librelane:gds", { file: summary.gds })}</div>
              <button className="ghost small" onClick={importGds}><Download size={13} /> {t("librelane:importGds")}</button>
            </>
          )}
        </>
      )}
    </div>
  );
}
