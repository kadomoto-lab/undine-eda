import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, CircleHelp, Download, Play, Rocket, XCircle } from "lucide-react";
import { api, SignoffCheck, suiteError, untrustedOf } from "../api";
import { UntrustedNotice } from "../UntrustedNotice";
import { T } from "../theme";
import { StateIcon } from "./SimPanel";
import { apiErrorText, resolveText, t, useI18n, type UiText } from "../i18n";

interface Check { id: string; title: string; status: "pass" | "fail" | "warn" | "manual"; detail: string }

const ICON = {
  pass: <CheckCircle2 size={14} color={T.ok} />, fail: <XCircle size={14} color={T.err} />,
  warn: <AlertTriangle size={14} color={T.warn} />, manual: <CircleHelp size={14} color={T.dim} />,
} as const;

const PARAM_LABELS = ["top_module", "analog_pins", "ua", "title", "author", "description", "tiles", "slot", "cob", "wrapper", "golden_gds"] as const;
const CHECK_TITLES = ["dbu", "cellnames", "origin", "zero_area", "tt_top", "tt_no_met5", "tt_cellnames", "tt_layers", "tt_power", "tt_overlap", "tt_analog", "tt_urpm", "tt_verilog",
  "ws_origin", "ws_5lm", "ws_seal_ring", "ws_density", "ws_id_cells", "ws_pad_mask",
  "cf_pdk", "cf_cellnames", "cf_prboundary", "cf_extent", "cf_gpio", "cf_oeb"] as const;
const TARGET_KEYS: Record<string, "tt" | "generic" | "ws" | "cf" | undefined> = {
  "tt-sky130-analog": "tt", "generic-layout": "generic", "wafer-space-gf180": "ws", "chipfoundry-caravel": "cf" };
// wafer.space slots (gf180mcu-precheck check_size.py): die size in um
const WS_SLOTS: [string, string][] = [["1x1", "3932 × 5122"], ["0p5x1", "1936 × 5122"], ["1x0p5", "3932 × 2531"], ["0p5x0p5", "1936 × 2531"]];

function targetLabel(target: { id: string; label: string }): string {
  const key = TARGET_KEYS[target.id];
  return key ? t(`verification:target.${key}Label`) : target.label;
}

function checkTitle(c: Check): string {
  const key = CHECK_TITLES.find((key) => key === c.id);
  if (key) return t(`verification:check.${key}`);
  // These titles carry values computed by the server. Keep the values instead of inferring them from current form input.
  const metal = c.id === "max_metal" && c.title.match(/^No metal above (.+)$/)?.[1];
  const tiles = c.id === "tt_boundary" && c.title.match(/^Layout is exactly the (.+) project area with a prBoundary$/)?.[1];
  const count = c.id === "tt_pins" && c.title.match(/^All (\d+) template pins /)?.[1];
  const pdk = c.id === "ws_pdk" && c.title.match(/^PDK is (\S+)$/)?.[1];
  const slot = c.id === "ws_slot" && c.title.match(/^Die is exactly (.+) um \(slot (.+)\)$/);
  const wrapper = c.id === "cf_xor" && c.title.match(/^Edge of the wrapper matches Caravel's empty (\S+) \(XOR\)$/)?.[1];
  if (wrapper) return t("verification:check.cf_xor", { wrapper });
  if (pdk) return t("verification:check.ws_pdk", { pdk });
  if (slot) return t("verification:check.ws_slot", { size: slot[1], slot: slot[2] });
  if (metal) return t("verification:check.max_metal", { metal });
  if (tiles) return t("verification:check.tt_boundary", { tiles });
  if (count) return t("verification:check.tt_pins", { count });
  return c.title;
}

export function TapeoutPanel({ project, lib, cell, version, toast }: {
  project: string; lib: string; cell: string; version: number; toast: (m: UiText, k?: "error" | "info") => void;
}) {
  useI18n();
  const [targets, setTargets] = useState<Awaited<ReturnType<typeof api.tapeoutTargets>>>([]);
  const [target, setTarget] = useState("tt-sky130-analog");
  const [params, setParams] = useState<Record<string, string>>({ top_module: "tt_um_", tiles: "1x2" });
  const [checks, setChecks] = useState<Check[] | null>(null);
  const [err, setErr] = useState<UiText>("");
  const [signoff, setSignoff] = useState<SignoffCheck[]>([]);
  const [bundle, setBundle] = useState<{ zip: string; files: string[]; dir: string } | null>(null);
  const [busy, setBusy] = useState(false);
  // why a check of the last "Run all" did not start (a trust refusal at launch, say), by check name: the signoff
  // rows do not know, as nothing ran
  const [refused, setRefused] = useState<Record<string, Error>>({});
  useEffect(() => { setRefused({}); }, [project, lib, cell, target]);

  useEffect(() => { api.tapeoutTargets().then(setTargets).catch(() => {}); }, []);
  const selectedTarget = targets.find((x) => x.id === target);
  useEffect(() => {
    if (selectedTarget) setParams({ ...selectedTarget.params, ...(selectedTarget.params.top_module !== undefined ? { top_module: /^tt_um_\w+$/.test(cell) ? cell : `tt_um_${cell.replace(/\W+/g, "_")}` } : {}) });
  }, [target, targets.length, cell]); // eslint-disable-line

  const run = async () => {
    setErr("");
    try { setChecks((await api.tapeoutCheck(project, lib, cell, target, params)).checks); }
    catch (e) { setChecks(null); setErr(apiErrorText(e)); }
  };
  useEffect(() => { run(); }, [project, lib, cell, target, version]); // eslint-disable-line
  useEffect(() => { if (checks) run(); }, [params.slot, params.cob, params.tiles, params.wrapper]); // eslint-disable-line -- the pickers have no blur

  // the sign-off checks as the server sees them: the job that ran on the design as it is now, or the latest one and
  // what has changed since; asked again while a check runs and after every edit
  const loadSignoff = () => api.signoff(project, lib, cell, target).then((r) => setSignoff(r.checks)).catch(() => {});
  useEffect(() => { loadSignoff(); }, [project, lib, cell, target, version]); // eslint-disable-line
  const running = signoff.some((s) => s.state === "running" || s.state === "queued");
  useEffect(() => {
    const t = setInterval(() => { if (!document.hidden) loadSignoff(); }, running ? 1500 : 4000);
    return () => clearInterval(t);
  }, [running, project, lib, cell, target]); // eslint-disable-line
  const startSuite = async (reuse: boolean) => {
    setBusy(true);
    setRefused({});
    try {
      const r = await api.tapeoutSuite(project, lib, cell, target, reuse);
      const why: Record<string, Error> = {};
      for (const j of r.jobs) { const e = suiteError(j); if (e) why[j.name] = e; }
      setRefused(why);
      await loadSignoff();
    }
    catch (e) { toast(apiErrorText(e), "error"); } finally { setBusy(false); }
  };
  const exportIt = async () => {
    setBusy(true);
    try {
      const r = await api.tapeoutExport(project, lib, cell, target, params);
      setBundle(r); setChecks(r.checks as Check[]);
      await saveAs(r.zip, r.target);
    } catch (e) { toast(apiErrorText(e), "error"); } finally { setBusy(false); }
  };

  // Chromium-based browsers let you choose the folder and name; others fall back to a normal download
  const saveAs = async (zip: string, tgt: string = target) => {
    const url = api.tapeoutDownloadUrl(project, tgt, zip);
    try {
      const w = window as unknown as { showSaveFilePicker?: (o: unknown) => Promise<{ createWritable: () => Promise<{ write: (b: Blob) => Promise<void>; close: () => Promise<void> }> }> };
      if (!w.showSaveFilePicker) throw new Error("no picker");
      const h = await w.showSaveFilePicker({ suggestedName: zip, types: [{ description: t("verification:tapeout.zip"), accept: { "application/zip": [".zip"] } }] });
      const r = await fetch(url);
      if (!r.ok) throw new Error(`download failed (${r.status})`);
      const f = await h.createWritable();
      await f.write(await r.blob()); await f.close();
    } catch (e) {
      if ((e as Error).name === "AbortError") return;                       // the person cancelled the dialog
      const a = document.createElement("a"); a.href = url; a.download = zip; a.click();
    }
  };

  const todo = signoff.filter((s) => !s.error && (refused[s.name] || !s.job || s.fresh === false || s.state === "failed" || s.state === "cancelled")).length;
  const signedOff = signoff.length > 0 && signoff.every((s) => !s.error && s.fresh && s.passed);
  const counts = checks ? { fail: checks.filter((c) => c.status === "fail").length, warn: checks.filter((c) => c.status === "warn").length, manual: checks.filter((c) => c.status === "manual").length } : null;

  return (
    <div className="inspector panel">
      <label className="stack"><span className="dim small">{t("verification:tapeout.target")}</span>
        <select className="full" value={target} onChange={(e) => setTarget(e.target.value)}>
          {targets.map((x) => <option key={x.id} value={x.id}>{targetLabel(x)}</option>)}
        </select></label>
      {selectedTarget && <div className="note small">{TARGET_KEYS[target] ? t(`verification:target.${TARGET_KEYS[target]}Note`) : selectedTarget.note}</div>}
      {selectedTarget && Object.keys(selectedTarget.params).filter((k) => k !== "max_metal").map((k) => (
        <label key={k} className="stack"><span className="dim small">{PARAM_LABELS.some((key) => key === k) ? t(`verification:tapeout.${k as typeof PARAM_LABELS[number]}`) : k}</span>
          {k === "tiles" ? (
            <select className="full" value={params[k] ?? ""} onChange={(e) => setParams({ ...params, [k]: e.target.value })}><option value="1x2">1x2 (161 × 225.76 µm)</option><option value="2x2">2x2 (334.88 × 225.76 µm)</option></select>
          ) : k === "slot" ? (
            <select className="full" value={params[k] ?? ""} onChange={(e) => setParams({ ...params, [k]: e.target.value })}>{WS_SLOTS.map(([s, size]) => <option key={s} value={s}>{`${s} (${size} µm)`}</option>)}</select>
          ) : k === "wrapper" ? (
            <select className="full" value={params[k] ?? "digital"} onChange={(e) => setParams({ ...params, [k]: e.target.value })}><option value="digital">{t("verification:tapeout.wrapperDigital")}</option><option value="analog">{t("verification:tapeout.wrapperAnalog")}</option></select>
          ) : k === "cob" ? (
            <select className="full" value={params[k] ?? "no"} onChange={(e) => setParams({ ...params, [k]: e.target.value })}><option value="no">{t("verification:tapeout.cobNo")}</option><option value="yes">{t("verification:tapeout.cobYes")}</option></select>
          ) : <input className="txt full" value={params[k] ?? ""} onChange={(e) => setParams({ ...params, [k]: e.target.value })} onBlur={run} />}
        </label>
      ))}

      <div className="section">{t("verification:tapeout.layoutChecks")} {counts && <span className="dim">{t("verification:tapeout.counts", counts)}</span>}</div>
      {err && <div className="note err">{resolveText(err)}</div>}
      <div className="itemlist tall">
        {checks?.map((c) => (
          <div key={c.id} className="chk" title={c.detail}>
            {ICON[c.status]}
            <div><div>{checkTitle(c)}</div>{c.detail && <div className="dim small">{c.detail}</div>}</div>
          </div>
        ))}
      </div>
      <div className="small dim">{t("verification:tapeout.manual")}</div>

      <div className="section">{t("verification:tapeout.suite")}</div>
      <button className="ghost full" disabled={busy || running || (!!signoff.length && !todo)} onClick={() => startSuite(true)}>
        <Play size={13} /> {todo === signoff.filter((s) => !s.error).length ? t("verification:tapeout.runAll") : t("verification:tapeout.runMissing", { count: todo })}
      </button>
      {signoff.map((s) => {
        const sum = s.summary as { total?: number; match?: boolean } | undefined;
        const why = refused[s.name];
        return (
          <div key={s.name} className="chk">
            {s.error || why ? <XCircle size={14} color={T.err} /> : s.state ? <StateIcon s={s.state} /> : <CircleHelp size={14} color={T.dim} />}
            <div className="grow chkbody">
              <div className="chkhead">
                <span className="grow ellip" title={s.name}>{s.name}</span>
                {!s.error && !s.job && <span className="tag">{t("verification:tapeout.notRun")}</span>}
                {s.fresh === false && <span className="tag warn">{t("verification:tapeout.outdated")}</span>}
                {s.state === "done" && sum && (
                  <span className={s.passed ? "okc" : "badc"}>
                    {sum.total !== undefined ? t("verification:common.violations", { count: sum.total }) : t(sum.match ? "verification:common.match" : "verification:common.mismatch")}
                  </span>
                )}
              </div>
              {s.error && <div className="small badc">{s.error}</div>}
              {why && (untrustedOf(why)
                ? <UntrustedNotice project={project} error={why} onTrusted={() => startSuite(true)} />
                : <div className="small badc" data-testid="suite-refused">{why.message}</div>)}
              {s.fresh === false && s.changed?.map((c) => <div key={c} className="small warn">{c}</div>)}
            </div>
          </div>
        );
      })}
      {signoff.some((s) => s.job) && (
        <div className="small dim">
          {signedOff ? `${t("verification:tapeout.signedOff")} ` : ""}{t("verification:tapeout.browse")}{" "}
          <button className="link small" disabled={busy || running} onClick={() => startSuite(false)}>{t("verification:tapeout.rerunAll")}</button>
        </div>
      )}

      <div className="section">{t("verification:tapeout.bundle")}</div>
      <button className="primary" disabled={busy || !!counts?.fail} onClick={exportIt}><Rocket size={13} /> {t("verification:tapeout.export")}</button>
      {counts?.fail ? <div className="small warn">{t("verification:tapeout.fixFirst")}</div> : null}
      {bundle && (
        <div className="note small">
          <div className="mono ellip">{bundle.dir}</div>
          {bundle.files.map((f) => <div key={f} className="mono dim">{f}</div>)}
          <button onClick={() => saveAs(bundle.zip)}><Download size={12} /> {t("verification:tapeout.saveAs", { name: bundle.zip })}</button>
          <div className="dim" style={{ marginTop: 4 }}>{t("verification:tapeout.report")}</div>
        </div>
      )}
    </div>
  );
}
