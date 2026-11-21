// The library browser's Packages section (docs/design/packages.md, UI): search an index, read a package's versions,
// licence, dependencies, exported cells and whether each version fits this project's PDK, and add it. The first read
// of an index fetches it on the server (under --dep-root / --dep-host); after that the local copy is read until
// Refresh fetches it again.
import { useEffect, useRef, useState } from "react";
import { AlertTriangle, Loader2, PackagePlus, RefreshCw, Search, X } from "lucide-react";
import { api, type DepAdd, type IndexInfo, type IndexWhere, type PackageFit, type PackageInfo, type PackageSummary } from "./api";
import { apiErrorText, msg, resolveText, useI18n, type UiText } from "./i18n";

const fitText = (fit: PackageFit): UiText | null =>
  fit === null ? null : fit.ok ? msg("app:packages.fits") : msg("app:packages.noFit", { reason: fit.reason ?? "" });

export function PackagesDialog({ project, readOnly, taken, onClose, onAdded, debounceMs = 250 }: {
  project: string;
  readOnly: boolean;                       // a preview of a changeset: nothing is added
  taken: (name: string) => boolean;        // a library of the project has that name already
  onClose: () => void;
  onAdded: (name: string, r: DepAdd) => void | Promise<void>;
  debounceMs?: number;
}) {
  const { t } = useI18n();
  const [indexes, setIndexes] = useState<IndexInfo[]>([]);
  const [index, setIndex] = useState("undine");
  const [query, setQuery] = useState("");
  const [found, setFound] = useState<(IndexWhere & { packages: PackageSummary[] }) | null>(null);
  const [searching, setSearching] = useState(false);
  const [picked, setPicked] = useState<string | null>(null);
  const [info, setInfo] = useState<PackageInfo | null>(null);
  const [local, setLocal] = useState("");
  const [req, setReq] = useState("");
  const [busy, setBusy] = useState<"refresh" | "add" | null>(null);
  const [err, setErr] = useState<UiText>("");
  const [tick, setTick] = useState(0);     // a refresh reads the search and the package again
  const live = useRef(true);
  useEffect(() => () => { live.current = false; }, []);

  useEffect(() => {
    api.indexes(project).then((r) => { if (live.current) setIndexes(r.indexes); }).catch(() => {});
  }, [project, tick]);

  useEffect(() => {
    setSearching(true);
    const h = setTimeout(() => {
      api.packages(query.trim(), index, project)
        .then((r) => { if (live.current) { setFound(r); setErr(""); } })
        .catch((e) => { if (live.current) { setFound(null); setErr(apiErrorText(e)); } })
        .finally(() => { if (live.current) setSearching(false); });
    }, debounceMs);
    return () => clearTimeout(h);
  }, [query, index, project, tick, debounceMs]);

  useEffect(() => {
    setInfo(null);
    if (!picked) return;
    api.packageInfo(picked, index, project)
      .then((r) => { if (live.current) { setInfo(r); setLocal((v) => v || r.name); } })
      .catch((e) => { if (live.current) setErr(apiErrorText(e)); });
  }, [picked, index, project, tick]);

  // Esc closes wherever the focus is (the dialog.tsx rule), and never reaches the editor behind
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      close.current();
    };
    window.addEventListener("keydown", key, true);
    return () => window.removeEventListener("keydown", key, true);
  }, []);

  const pick = (name: string) => { setPicked(name); setLocal(name); setReq(""); setErr(""); };
  const refresh = async () => {
    setBusy("refresh");
    setErr("");
    try { await api.refreshIndex(index, project); setTick((n) => n + 1); }
    catch (e) { setErr(apiErrorText(e)); }
    finally { if (live.current) setBusy(null); }
  };
  const name = local.trim() || info?.name || "";
  const nameErr = name && taken(name) ? msg("app:library.exists") : null;
  const add = async () => {
    if (!info || !name || nameErr || readOnly) return;
    setBusy("add");
    setErr("");
    try {
      const r = await api.depAddPackage(project, name, info.name, req.trim() || undefined, index === "undine" ? undefined : index);
      await onAdded(name, r);
    } catch (e) {
      if (live.current) setErr(apiErrorText(e));
    } finally {
      if (live.current) setBusy(null);
    }
  };

  const newest = info?.versions.find((v) => v.version === info.newest) ?? null;
  const row = (label: UiText, value: UiText, mono = true) => (
    <div className="kv"><span>{resolveText(label)}</span><b className={mono ? "mono" : ""}>{resolveText(value)}</b></div>);
  return (
    <div className="modalback" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal pkgdialog" role="dialog" aria-modal="true" aria-label={t("app:packages.title")}>
        <div className="modalhead">
          <span className="grow">{t("app:packages.title")}</span>
          <button className="icon" title={t("common:actions.cancelShortcut")} onClick={onClose}><X size={15} /></button>
        </div>
        <div className="modalbody">
          <div className="pkgbar">
            <select className="txt" aria-label={t("app:packages.index")} value={index}
              onChange={(e) => { setIndex(e.target.value); setPicked(null); }}>
              {(indexes.length ? indexes : [{ name: "undine", url: "", commit: null }]).map((x) => (
                <option key={x.name} value={x.name} title={x.url}>{x.name}</option>))}
            </select>
            <div className="searchbox grow"><Search size={13} />
              <input autoFocus placeholder={t("app:packages.search")} value={query} onChange={(e) => setQuery(e.target.value)} /></div>
            <button className="icon" title={t("app:packages.refresh")} disabled={busy !== null} onClick={refresh}>
              {busy === "refresh" ? <Loader2 size={14} className="spin" /> : <RefreshCw size={14} />}</button>
          </div>
          {found && <div className="faint small mono" title={found.index}>
            {t("app:packages.copy", { index: found.index_name, commit: (found.commit ?? "").slice(0, 12) })}</div>}
          <div className="pkgpanes">
            <div className="pkglist" role="listbox" aria-label={t("app:packages.results")}>
              {searching && !found && <div className="dim small"><Loader2 size={12} className="spin" /> {t("common:state.loading")}</div>}
              {found && !found.packages.length && <div className="empty">{query.trim()
                ? t("app:packages.none", { query: query.trim() }) : t("app:packages.empty")}</div>}
              {found?.packages.map((p) => (
                <div key={p.name} role="option" aria-selected={picked === p.name}
                  className={`pkgrow${picked === p.name ? " cur" : ""}`} onClick={() => pick(p.name)}>
                  <div><b className="mono">{p.name}</b> <span className="mono dim">{p.version}</span>
                    {p.fits && !p.fits.ok && <span className="tag warn" title={resolveText(fitText(p.fits)!)}><AlertTriangle size={11} /></span>}</div>
                  {(p.description || p.license) && <div className="dim small">{p.description}{p.license ? ` (${p.license})` : ""}</div>}
                </div>
              ))}
            </div>
            <div className="pkgdetail">
              {!picked && <div className="dim small">{t("app:packages.pick")}</div>}
              {picked && !info && !err && <div className="dim small"><Loader2 size={12} className="spin" /> {t("common:state.loading")}</div>}
              {info && newest && (
                <>
                  <div><b className="mono">{info.name}</b> <span className="mono">{info.newest}</span>
                    {info.locked && <span className="tag"> {t("app:packages.locked", { version: info.locked })}</span>}</div>
                  {newest.description && <div className="small">{newest.description}</div>}
                  <div>
                    {row(msg("app:packages.license"), newest.license ?? msg("app:packages.noLicense"), !!newest.license)}
                    {newest.fits && row(msg("app:packages.pdk"), fitText(newest.fits)!, false)}
                    {row(msg("app:packages.repository"), newest.git + (newest.subdir ? ` (${newest.subdir})` : ""))}
                    {row(msg("app:packages.commit"), newest.commit.slice(0, 12))}
                    {row(msg("app:packages.exports"), newest.exports === null ? msg("app:packages.allCells") : newest.exports.join(", ") || "-")}
                    {row(msg("app:packages.needs"), newest.deps.length ? newest.deps.map((d) => `${d.package} ${d.req}`
                      + (d.name !== d.package ? ` as ${d.name}` : "") + (d.index_name ? ` (${d.index_name})` : "")).join(", ") : "-")}
                  </div>
                  <div className="small">
                    <div className="dim">{t("app:packages.versions")}</div>
                    <div className="pkgversions">{info.versions.map((v) => (
                      <span key={v.version} className={`chip mono${v.yanked ? " struck" : ""}`}
                        title={[v.yanked ? resolveText(msg("app:packages.yanked")) : "", v.fits && !v.fits.ok ? resolveText(fitText(v.fits)!) : ""]
                          .filter(Boolean).join("\n") || undefined}
                        onClick={() => !v.yanked && setReq(`=${v.version}`)}>{v.version}</span>))}</div>
                  </div>
                  {!readOnly && (
                    <div className="pkgadd">
                      <label className="modalfield"><span className="small dim">{t("app:packages.localName")}</span>
                        <input className={`txt mono${nameErr ? " bad" : ""}`} value={local} onChange={(e) => setLocal(e.target.value)} /></label>
                      <label className="modalfield"><span className="small dim">{t("app:packages.requirement")}</span>
                        <input className="txt mono" value={req} placeholder={`^${info.newest}`} onChange={(e) => setReq(e.target.value)} /></label>
                      {nameErr && <div className="fielderr">{resolveText(nameErr)}</div>}
                    </div>
                  )}
                </>
              )}
            </div>
          </div>
          {readOnly && <div className="faint small">{t("app:packages.readOnly")}</div>}
          {err && <div className="note err"><AlertTriangle size={13} /> {resolveText(err)}</div>}
          <div className="modalfoot">
            <button type="button" className="ghost" onClick={onClose}>{t("common:actions.close")}</button>
            {!readOnly && (
              <button type="button" className="primary" disabled={!info || !name || !!nameErr || busy !== null} onClick={add}>
                <PackagePlus size={13} /> {busy === "add" ? t("common:state.working") : t("app:packages.add", { name: name || "-" })}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
