import { useEffect, useMemo, useState } from "react";
import { ChevronRight, Play, ShieldCheck, Workflow } from "lucide-react";
import { hashParam } from "../App";
import { api, DrcItem, DrcSummary, JobStatus, LvsResult, LvsSummary } from "../api";
import { jobStateText, Outdated, Refused, refusedOr, StateIcon, useFreshness, useJobs } from "./SimPanel";
import { T } from "../theme";
import { msg, t, useI18n, type UiText } from "../i18n";

interface Props {
  project: string;
  lib: string;
  cell: string;
  hasSchematic: boolean;
  dbu: number;
  onMarkers: (m: DrcItem[] | null, focus?: number) => void;
  onFocus: (bbox: number[]) => void;
  toast: (m: UiText, k?: "error" | "info") => void;
}

const statusColor = (s: string) => (s === "Match" ? T.ok : s === "MatchWithWarning" || s === "Skipped" ? T.warn : T.err);
const statusText = (s: string) => {
  const keys = { Match: "match", MatchWithWarning: "warning", Skipped: "skipped", Mismatch: "mismatch", NoMatch: "noMatch" } as const;
  const key = keys[s as keyof typeof keys];
  return key ? t(`verification:status.${key}`) : s;
};

export function VerifyPanel({ project, lib, cell, hasSchematic, dbu, onMarkers, onFocus, toast }: Props) {
  useI18n();
  const [mode, setMode] = useState<"drc" | "lvs">(hashParam("mode") === "lvs" ? "lvs" : "drc");
  const drc = useJobs(project, "drc", cell, lib);
  const lvs = useJobs(project, "lvs", cell, lib);
  const [decks, setDecks] = useState<Awaited<ReturnType<typeof api.decks>> | null>(null);
  const [sel, setSel] = useState<{ drc?: string; lvs?: string }>({});
  const [items, setItems] = useState<DrcItem[]>([]);
  const [cat, setCat] = useState<number | null>(null);
  const [cur, setCur] = useState<number>(-1);
  const [lvsRes, setLvsRes] = useState<LvsResult | null>(null);
  const [schFile, setSchFile] = useState("");
  const [opts, setOpts] = useState({ feol: true, beol: true, offgrid: true });
  const [drcTool, setDrcTool] = useState<"klayout" | "magic">("klayout");
  const [lvsTool, setLvsTool] = useState<"klayout" | "netgen">("klayout");
  const [netgenText, setNetgenText] = useState<string | null>(null);
  // an LVS launch the trust gate refused: a dependency's line in the reference netlist reads a file outside the PDK
  // (checked when the run launches, KLayout and netgen alike; DRC reads no netlist)
  const [refused, setRefused] = useState<unknown>(null);
  useEffect(() => setRefused(null), [project, lib, cell]);

  useEffect(() => { api.decks(project).then(setDecks).catch(() => {}); }, [project]);
  useEffect(() => { if (!sel.drc && drc.jobs.length) setSel((s) => ({ ...s, drc: drc.jobs[0].id })); }, [drc.jobs, sel.drc]);
  useEffect(() => { if (!sel.lvs && lvs.jobs.length) setSel((s) => ({ ...s, lvs: lvs.jobs[0].id })); }, [lvs.jobs, sel.lvs]);

  const drcJob: JobStatus | undefined = drc.jobs.find((j) => j.id === sel.drc);
  const lvsJob: JobStatus | undefined = lvs.jobs.find((j) => j.id === sel.lvs);
  const drcFresh = useFreshness(project, drcJob);
  const lvsFresh = useFreshness(project, lvsJob);
  const drcSum = drcJob?.state === "done" ? (drcJob.summary as DrcSummary) : null;
  const lvsSum = lvsJob?.state === "done" ? (lvsJob.summary as LvsSummary) : null;

  // load markers of the selected category
  useEffect(() => {
    if (!drcJob || drcJob.state !== "done") { setItems([]); onMarkers(null); return; }
    api.drcItems(project, drcJob.id, cat).then((r) => { setItems(r.items); setCur(-1); onMarkers(r.items); }).catch(() => {});
    return () => onMarkers(null);
  }, [drcJob?.id, drcJob?.state, cat, project]); // eslint-disable-line

  useEffect(() => {
    setNetgenText(null);
    if (!lvsJob || lvsJob.state !== "done") { setLvsRes(null); return; }
    if (lvsJob.tool === "netgen") { setLvsRes(null); return; }
    api.lvsResult(project, lvsJob.id).then(setLvsRes).catch(() => setLvsRes(null));
  }, [lvsJob?.id, lvsJob?.state, project]);

  const run = async (kind: "drc" | "lvs") => {
    try {
      const sc = hasSchematic && !schFile ? cell : undefined;
      const j = kind === "drc"
        ? (drcTool === "magic" ? await api.startMagicDrc(project, lib, cell) : await api.startDrc(project, lib, cell, opts))
        : (lvsTool === "netgen" ? await api.startNetgenLvs(project, lib, cell, sc, schFile || undefined)
          : await api.startLvs(project, lib, cell, sc, schFile || undefined));
      if (kind === "lvs") setRefused(null);
      setSel((s) => ({ ...s, [kind]: j.id }));
      await (kind === "drc" ? drc.refresh() : lvs.refresh());
    } catch (e) {
      if (kind === "lvs") setRefused(null);
      refusedOr(e, setRefused, toast);
    }
  };

  const um = (v: number) => Number(v.toFixed(3));
  const cats = useMemo(() => drcSum?.categories ?? [], [drcSum]);

  return (
    <div className="inspector panel">
      <div className="seg inner">
        <button className={mode === "drc" ? "on" : ""} onClick={() => setMode("drc")}><ShieldCheck size={14} /> DRC</button>
        <button className={mode === "lvs" ? "on" : ""} onClick={() => setMode("lvs")}><Workflow size={14} /> LVS</button>
      </div>
      {decks && !decks.klayout && <div className="note err">{t("verification:verify.noKlayout")}</div>}

      {mode === "drc" ? (
        <>
          <ToolPick value={drcTool} onChange={setDrcTool} options={[["klayout", "KLayout"], ["magic", "Magic"]]} enabled={{ klayout: !!decks?.drc.length, magic: !!decks?.magic }} />
          {drcTool === "klayout" ? (
            <>
              <div className="dim small">{t("verification:verify.deck", { deck: decks?.drc[0] ?? t("verification:common.none") })}</div>
              <div className="checks">
                {(["feol", "beol", "offgrid"] as const).map((k) => (
                  <label key={k} className="check"><input type="checkbox" checked={opts[k]} onChange={(e) => setOpts({ ...opts, [k]: e.target.checked })} /> {k.toUpperCase()}</label>
                ))}
              </div>
            </>
          ) : <div className="dim small">{t("verification:verify.magicDescription")}</div>}
          <button className="primary" disabled={drcTool === "klayout" ? !decks?.drc.length : !decks?.magic} onClick={() => run("drc")}><Play size={13} /> {t("verification:verify.runDrc", { tool: drcTool === "magic" ? "Magic " : "", cell })}</button>
          {drc.jobs.length > 0 && (
            <select className="full" aria-label={t("verification:verify.drcRuns")} value={sel.drc ?? ""} onChange={(e) => { setSel({ ...sel, drc: e.target.value }); setCat(null); }}>
              {drc.jobs.slice(0, 10).map((j) => <option key={j.id} value={j.id}>{j.id.slice(9, 15)} · {j.tool ?? "klayout"} — {jobStateText(j.state)}{j.summary ? ` — ${t("verification:common.violations", { count: (j.summary as DrcSummary).total })}` : ""}</option>)}
            </select>
          )}
          {drcJob && (drcJob.state === "running" || drcJob.state === "queued") && <div className="note"><StateIcon s={drcJob.state} /> {t("verification:common.running")}</div>}
          {drcJob?.state === "failed" && <div className="note err">{drcJob.error}</div>}
          {drcSum && (
            <>
              <div className={`verdict ${drcSum.total ? "bad" : "good"}`}>
                {drcSum.total ? t("verification:verify.violationRules", { count: drcSum.total, rules: cats.length }) : t("verification:verify.noViolations")}
                <span className="dim small"> · {t("verification:verify.rules", { count: drcSum.rules })} · {drcJob?.seconds?.toFixed(1)}s</span>
              </div>
              <Outdated f={drcFresh} onRerun={() => run("drc")} />
              {cats.length > 0 && (
                <>
                  <div className="catlist">
                    <div className={`catrow ${cat == null ? "cur" : ""}`} onClick={() => setCat(null)}><span className="grow">{t("verification:verify.allRules")}</span><b>{drcSum.total}</b></div>
                    {cats.map((c) => (
                      <div key={c.id} className={`catrow ${cat === c.id ? "cur" : ""}`} onClick={() => setCat(c.id)} title={c.description}>
                        <span className="mono">{c.name}</span><span className="dim small grow ellip">{c.description.replace(/^.*? : /, "")}</span><b>{c.count}</b>
                      </div>
                    ))}
                  </div>
                  <div className="section">{t("verification:verify.violations")}{items.length ? ` (${items.length})` : ""}</div>
                  <div className="itemlist">
                    {items.slice(0, 300).map((it, i) => (
                      <div key={i} className={`catrow ${cur === i ? "cur" : ""}`}
                        onClick={() => { setCur(i); onMarkers(items, i); onFocus(it.bbox); }}>
                        <ChevronRight size={12} /><span className="mono">{it.cat}</span>
                        <span className="dim small grow mono">{um(it.bbox[0])}, {um(it.bbox[1])}</span>
                      </div>
                    ))}
                  </div>
                </>
              )}
            </>
          )}
        </>
      ) : (
        <>
          <ToolPick value={lvsTool} onChange={setLvsTool} options={[["klayout", "KLayout"], ["netgen", "Magic + Netgen"]]} enabled={{ klayout: !!decks?.lvs.length, netgen: !!(decks?.magic && decks?.netgen) }} />
          <div className="dim small">{lvsTool === "klayout" ? t("verification:verify.deck", { deck: decks?.lvs[0] ?? t("verification:common.none") }) : t("verification:verify.netgenDescription")}</div>
          <label className="stack"><span className="dim small">{t("verification:verify.compare")}</span>
            <select className="full" value={schFile ? "file" : "sch"} onChange={(e) => { if (e.target.value === "sch") setSchFile(""); else setSchFile("~/"); }}>
              <option value="sch" disabled={!hasSchematic}>{t("verification:verify.schematic", { lib, cell })}</option>
              <option value="file">{t("verification:verify.spiceFile")}</option>
            </select></label>
          {schFile !== "" && <input className="txt full" aria-label={t("verification:verify.spicePath")} placeholder="~/netlist.spice" value={schFile} onChange={(e) => setSchFile(e.target.value)} />}
          <button className="primary" disabled={(lvsTool === "klayout" ? !decks?.lvs.length : !(decks?.magic && decks?.netgen)) || (!hasSchematic && !schFile)} onClick={() => run("lvs")}><Play size={13} /> {t("verification:verify.runLvs", { cell })}</button>
          <Refused project={project} error={refused} what={msg("verification:untrusted.lvs")} onTrusted={() => run("lvs")} />
          {lvs.jobs.length > 0 && (
            <select className="full" aria-label={t("verification:verify.lvsRuns")} value={sel.lvs ?? ""} onChange={(e) => setSel({ ...sel, lvs: e.target.value })}>
              {lvs.jobs.slice(0, 10).map((j) => <option key={j.id} value={j.id}>{j.id.slice(9, 15)} · {j.tool ?? "klayout"} — {jobStateText(j.state)}{j.summary ? ` — ${t((j.summary as LvsSummary).match ? "verification:common.match" : "verification:common.mismatch")}` : ""}</option>)}
            </select>
          )}
          {lvsJob && (lvsJob.state === "running" || lvsJob.state === "queued") && <div className="note"><StateIcon s={lvsJob.state} /> {t("verification:common.running")}</div>}
          {lvsJob?.state === "failed" && <div className="note err">{lvsJob.error}</div>}
          {lvsSum && (
            <>
              <div className={`verdict ${lvsSum.match ? "good" : "bad"}`}>{lvsJob?.tool === "netgen" ? t(lvsSum.match ? "verification:verify.circuitsMatch" : "verification:verify.circuitsDiffer") : t(lvsSum.match ? "verification:verify.netlistsMatch" : "verification:verify.netlistsDiffer")}
                <span className="dim small"> · {lvsJob?.seconds?.toFixed(1)}s</span></div>
              {lvsJob?.tool === "netgen" && lvsSum.verdict && <div className="dim small">{lvsSum.verdict}</div>}
              {lvsSum.pin_errors?.map((e) => <div key={e} className="small badc mono">{e}</div>)}
              <Outdated f={lvsFresh} onRerun={() => run("lvs")} />
              {lvsJob?.tool === "netgen" && (
                <>
                  <button className="link small" onClick={async () => setNetgenText(netgenText === null ? await api.lvsText(project, lvsJob.id) : null)}>{t(netgenText === null ? "verification:verify.showNetgen" : "verification:verify.hideNetgen")}</button>
                  {netgenText !== null && <pre className="logbox">{netgenText}</pre>}
                </>
              )}
              {lvsJob?.tool !== "netgen" && lvsSum.circuits.map((c, i) => (
                <div key={i} className="kv"><span className="mono">{c.layout ?? "—"} ↔ {c.schematic ?? "—"}</span>
                  <b style={{ color: statusColor(c.status) }}>{statusText(c.status)}</b></div>
              ))}
              {lvsRes && (
                <>
                  <PairTable title={t("verification:verify.devices")} rows={lvsRes.detail.devices} />
                  <PairTable title={t("verification:verify.pins")} rows={lvsRes.detail.pins} />
                  <PairTable title={t("verification:verify.nets")} rows={lvsRes.detail.nets} />
                </>
              )}
              {lvsJob?.tool !== "netgen" && <a className="link small" href={`/api/p/${encodeURIComponent(project)}/jobs/${lvsJob?.id}/extracted`} target="_blank" rel="noreferrer">{t("verification:verify.extracted")}</a>}
            </>
          )}
        </>
      )}
      <span style={{ display: "none" }}>{dbu}</span>
    </div>
  );
}

function PairTable({ title, rows }: { title: string; rows: { layout: string | null; schematic: string | null; status: string }[] }) {
  useI18n();
  const bad = rows.filter((r) => r.status !== "Match").length;
  return (
    <>
      <div className="section">{title} <span className="dim">({rows.length}{bad ? t("verification:verify.notMatching", { count: bad }) : ""})</span></div>
      <div className="pairtable">
        {rows.slice(0, 200).map((r, i) => (
          <div key={i} className="pairrow">
            <i className="dot" style={{ background: statusColor(r.status) }} title={statusText(r.status)} />
            <span className="mono ellip" title={t("verification:verify.layout")}>{r.layout ?? "—"}</span>
            <span className="mono ellip dim" title={t("verification:verify.schematicView")}>{r.schematic ?? "—"}</span>
          </div>
        ))}
      </div>
    </>
  );
}

function ToolPick<T extends string>({ value, onChange, options, enabled }: {
  value: T; onChange: (v: T) => void; options: [T, string][]; enabled: Record<string, boolean>;
}) {
  useI18n();
  return (
    <div className="modeseg" style={{ alignSelf: "flex-start" }}>
      {options.map(([k, label]) => (
        <button key={k} className={value === k ? "on" : ""} disabled={!enabled[k]} title={enabled[k] ? "" : t("verification:common.unavailable")} onClick={() => onChange(k)}>{label}</button>
      ))}
    </div>
  );
}
