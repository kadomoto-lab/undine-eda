import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, CircleDashed, RefreshCw, Wand2 } from "lucide-react";
import { api, SdlStatus } from "../api";
import { T } from "../theme";
import { useI18n, t, msg, resolveText, apiErrorText, type UiText } from "../i18n";

export function SdlPanel({ project, lib, cell, version, hasSchematic, showRats, setShowRats, onStatus, onProbe, onFocus, onEdited, integrity, onFocusDbu, toast }: {
  project: string; lib: string; cell: string; version: number; hasSchematic: boolean;
  showRats: boolean; setShowRats: (v: boolean) => void;
  onStatus: (s: SdlStatus | null) => void;
  onProbe: (q: { inst: string } | null) => void;
  onFocus: (bbox: number[]) => void;
  onEdited: () => void;
  integrity: { instance: string; problem: string; where: number[] }[];
  onFocusDbu: (bbox: number[]) => void;
  toast: (m: UiText, k?: "error" | "info") => void;
}) {
  useI18n();
  const [st, setSt] = useState<SdlStatus | null>(null);
  const [err, setErr] = useState<UiText | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    if (!hasSchematic) { setSt(null); onStatus(null); setErr(msg("editor:sdl.noSchematic", { cell, lib })); return; }  // nothing to ask the server
    try {
      const s = await api.sdl(project, lib, cell);
      setSt(s); onStatus(s); setErr(null);
    } catch (e) { setSt(null); onStatus(null); setErr(String((e as Error)?.message ?? e).startsWith("no schematic") ? msg("editor:sdl.noSchematic", { cell, lib }) : apiErrorText(e)); }
  }, [project, lib, cell, hasSchematic]); // eslint-disable-line
  useEffect(() => { load(); }, [load, version]);

  const sync = async () => {
    setBusy(true);
    try {
      const r = await api.sdlSync(project, lib, cell);
      toast(msg(r.updated.length === 1 ? "editor:sdl.updatedOne" : "editor:sdl.updatedMany", { count: r.updated.length, failures: r.errors.length ? msg("editor:sdl.failures", { count: r.errors.length }) : "" }), r.errors.length ? "error" : "info");
      if (r.errors.length) toast(r.errors.map((e) => `${e.name}: ${e.error}`).join("; "), "error");
      onEdited();
      await load();
    } catch (e) { toast(apiErrorText(e), "error"); } finally { setBusy(false); }
  };

  const generate = async (all: boolean) => {
    setBusy(true);
    try {
      const r = await api.sdlGenerate(project, lib, cell, all);
      setSt(r.status); onStatus(r.status);
      onEdited();
      toast(msg(r.created.length === 1 ? "editor:sdl.placedOne" : "editor:sdl.placedMany", { count: r.created.length, failures: r.errors.length ? msg("editor:sdl.failures", { count: r.errors.length }) : "" }), r.errors.length ? "error" : "info");
      if (r.errors.length) toast(r.errors.map((e) => `${e.name}: ${e.error}`).join("; "), "error");
    } catch (e) { toast(apiErrorText(e), "error"); } finally { setBusy(false); }
  };

  if (err) return <div className="inspector"><div className="note">{resolveText(err)}</div></div>;
  if (!st) return <div className="inspector"><div className="empty">{t("editor:status.loading")}</div></div>;

  const placed = st.devices.filter((d) => d.placed).length;
  return (
    <div className="inspector panel">
      <div className="kv"><span>{t("editor:sdl.schematicDevices")}</span><b>{st.devices.length}</b></div>
      <div className="kv"><span>{t("editor:sdl.placedInLayout")}</span><b>{placed} / {st.devices.filter((d) => d.kind).length}</b></div>
      {st.stale.length > 0 && (
        <div className="note warnnote">
          {t("editor:sdl.stale", { devices: st.stale.join(", ") })}
          <div className="row" style={{ marginTop: 6 }}><button className="ghost" disabled={busy} onClick={sync}>{t("editor:sdl.updateLayout")}</button></div>
        </div>
      )}
      {integrity.length > 0 && (
        <div className="note warnnote">
          <b>{t("editor:sdl.overlapTitle")}</b> {t("editor:sdl.overlapDetail")}
          {integrity.map((p, i) => (
            <div key={i} className="integrow" onClick={() => onFocusDbu(p.where)}>
              <span className="mono">{p.instance}</span> <span className="small">{p.problem}</span>
            </div>
          ))}
          <div className="small dim" style={{ marginTop: 4 }}>{t("editor:sdl.overlapHelp")}</div>
        </div>
      )}
      {st.extra.length > 0 && <div className="note warnnote">{t("editor:sdl.extra", { devices: st.extra.join(", ") })}</div>}
      {st.unsupported.length > 0 && <div className="note warnnote">{t("editor:sdl.unsupported", { devices: st.unsupported.join(", ") })}</div>}
      <div className="row">
        <button className="primary" disabled={busy || !st.missing.length || !st.pcells} onClick={() => generate(false)}>
          <Wand2 size={13} /> {busy ? t("editor:sdl.generating") : t("editor:sdl.generateMissing", { count: st.missing.length })}
        </button>
        <button className="icon" title={t("editor:sdl.regenerateAll")} disabled={busy || !st.pcells} onClick={() => generate(true)}><RefreshCw size={14} /></button>
      </div>
      {!st.pcells && <div className="note">{t("editor:sdl.pcellsUnavailable")}{st.pcells_reason && <div className="small mono" style={{ marginTop: 4 }}>{st.pcells_reason}</div>}</div>}
      <label className="check"><input type="checkbox" checked={showRats} onChange={(e) => setShowRats(e.target.checked)} /> {t("editor:sdl.showRatsnest", { count: st.ratsnest.length })}</label>
      <div className="section">{t("editor:sdl.devices")}</div>
      <div className="itemlist tall">
        {st.devices.map((d) => (
          <div key={d.name} className="catrow" onClick={() => { onProbe({ inst: d.name }); if (d.bbox) onFocus(d.bbox); }}>
            {d.placed ? (d.stale ? <CircleDashed size={13} color={T.warn} /> : <CheckCircle2 size={13} color={T.ok} />) : <CircleDashed size={13} color={d.kind ? T.warn : T.faint} />}
            <span className="mono">{d.name}</span>
            <span className="dim small grow ellip">{d.pcell ? `${d.pcell} ${paramText(d.params)}` : d.kind === "cell" ? t("editor:sdl.cell", { cell: d.layout_cell ?? msg("editor:sdl.noLayout") }) : d.symbol}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function paramText(p?: Record<string, unknown>): string {
  if (!p) return "";
  return ["w", "l", "nf"].filter((k) => k in p).map((k) => `${k}=${p[k]}`).join(" ");
}
