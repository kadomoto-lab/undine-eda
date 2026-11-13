import { useCallback, useEffect, useState } from "react";
import { Copy, Download, RefreshCw } from "lucide-react";
import { api, NetlistResult, untrustedOf } from "../api";
import { apiErrorText, msg, resolveText, t, useI18n, type UiText } from "../i18n";
import { Refused } from "./SimPanel";

export function NetlistPanel({ project, lib, cell, toast, tick }: {
  project: string; lib: string; cell: string; tick: number; toast: (m: UiText, k?: "error" | "info") => void;
}) {
  useI18n();
  const [backend, setBackend] = useState("undine");
  const [fmt, setFmt] = useState("spice");
  const [mode, setMode] = useState("spice");
  const [res, setRes] = useState<NetlistResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<UiText>("");
  // the Xschem netlister refused (403 untrusted): a dependency's schematics hold Tcl that Xschem would run
  const [refused, setRefused] = useState<unknown>(null);

  const gen = useCallback(async () => {
    setBusy(true);
    setErr("");
    setRefused(null);
    try {
      setRes(await api.netlist(project, lib, cell, backend, fmt, mode));
    } catch (e) {
      setRes(null);
      if (untrustedOf(e)) setRefused(e);
      else setErr(apiErrorText(e));
    } finally {
      setBusy(false);
    }
  }, [project, lib, cell, backend, fmt, mode]);
  useEffect(() => { gen(); }, [gen, tick]);

  const download = () => {
    if (!res) return;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([res.text], { type: "text/plain" }));
    a.download = `${cell}.${fmt === "spectre" && backend === "vlsirtools" ? "scs" : "spice"}`;
    a.click();
  };

  return (
    <div className="inspector panel">
      <div className="fieldgrid">
        <label><span>{t("verification:netlist.netlister")}</span>
          <select value={backend} onChange={(e) => setBackend(e.target.value)}>
            <option value="undine">{t("verification:netlist.undine")}</option>
            <option value="xschem">{t("verification:netlist.xschem")}</option>
            <option value="vlsirtools">vlsirtools</option>
          </select></label>
        {backend === "vlsirtools" && (
          <label><span>{t("verification:netlist.dialect")}</span>
            <select value={fmt} onChange={(e) => setFmt(e.target.value)}>
              <option value="spice">SPICE</option><option value="xyce">Xyce</option><option value="spectre">Spectre</option>
            </select></label>
        )}
        {backend === "undine" && (
          <label><span>{t("verification:netlist.mode")}</span>
            <select value={mode} onChange={(e) => setMode(e.target.value)}>
              <option value="spice">{t("verification:netlist.simulation")}</option><option value="lvs">LVS</option>
            </select></label>
        )}
      </div>
      <div className="row">
        <button className="ghost" onClick={gen}><RefreshCw size={13} className={busy ? "spin" : ""} /> {t("verification:netlist.regenerate")}</button>
        <button className="icon" title={t("verification:netlist.copy")} aria-label={t("verification:netlist.copy")} disabled={!res} onClick={() => { navigator.clipboard.writeText(res!.text); toast(msg("verification:netlist.copied")); }}><Copy size={14} /></button>
        <button className="icon" title={t("verification:netlist.download")} aria-label={t("verification:netlist.download")} disabled={!res} onClick={download}><Download size={14} /></button>
      </div>
      {err && <div className="note err">{resolveText(err)}</div>}
      <Refused project={project} error={refused} what={msg("verification:untrusted.netlist")} onTrusted={gen} />
      {res?.warnings.map((w, i) => <div key={i} className="note warnnote">{w}</div>)}
      {res && <pre className="logbox netlist">{res.text}</pre>}
    </div>
  );
}
