import { useEffect, useMemo, useState } from "react";
import { MousePointerClick } from "lucide-react";
import { api, PcellDecl, PcellParam } from "../api";
import { useI18n, t } from "../i18n";

export interface PcellSpec { pcell: string; params: Record<string, unknown> }

export function PcellPanel({ project, active, onArm, onCancel }: {
  project: string; active: PcellSpec | null; onArm: (s: PcellSpec) => void; onCancel: () => void;
}) {
  useI18n();
  const [decls, setDecls] = useState<PcellDecl[] | null>(null);
  const [name, setName] = useState("nfet");
  const [vals, setVals] = useState<Record<string, unknown>>({});
  const [avail, setAvail] = useState(true);
  const [reason, setReason] = useState<string | null>(null);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    api.pcells(project).then((r) => {
      setAvail(r.available); setDecls(r.pcells); setReason(r.reason ?? null);
      // start on the n-channel transistor: nfet (sky130, GF180), nmos (IHP), else the first PCell
      const names = (r.pcells ?? []).map((d) => d.name);
      setName((cur) => names.includes(cur) ? cur : names.find((x) => x === "nfet" || x === "nmos") ?? names[0] ?? cur);
    }).catch(() => setAvail(false));
  }, [project]);
  const decl = decls?.find((d) => d.name === name);
  useEffect(() => { setVals({}); }, [name]);

  const params = useMemo(() => (decl?.params ?? []).filter((p) => !p.readonly), [decl]);
  const value = (p: PcellParam) => (p.name in vals ? vals[p.name] : p.type === "list" ? (p.choice_values?.[0] ?? "") : p.default ?? "");

  const arm = () => {
    const out: Record<string, unknown> = {};
    for (const p of params) {
      const v = value(p);
      if (v === "" || v === null || v === undefined) continue;
      out[p.name] = p.type === "int" ? Math.round(Number(v)) : p.type === "float" ? Number(v) : v;
    }
    onArm({ pcell: name, params: out });
  };

  if (!avail) return <div className="inspector"><div className="note">{t("editor:pcell.unavailable")}{reason && <div className="small mono" style={{ marginTop: 4 }}>{reason}</div>}</div></div>;
  if (!decls) return <div className="inspector"><div className="empty">{t("editor:pcell.loading")}</div></div>;

  return (
    <div className="inspector panel">
      <input className="search full" placeholder={t("editor:pcell.search")} value={filter} onChange={(e) => setFilter(e.target.value)} />
      <div className="pcelllist">
        {decls.filter((d) => !filter || d.name.includes(filter)).map((d) => (
          <button key={d.name} className={`chip ${d.name === name ? "on" : ""}`} onClick={() => setName(d.name)}>{d.name}</button>
        ))}
      </div>
      <div className="section">{t("editor:pcell.parameters", { name })}</div>
      {params.map((p) => (
        <div className="kv edit" key={p.name}>
          <span title={p.label}>{p.label || p.name}{p.unit ? <span className="dim"> ({p.unit})</span> : null}</span>
          {p.type === "list" ? (
            <select value={String(value(p))} onChange={(e) => setVals({ ...vals, [p.name]: e.target.value })}>
              {p.choice_values?.map((c, i) => <option key={c} value={c}>{p.choices?.[i] ?? c}</option>)}
            </select>
          ) : p.type === "bool" ? (
            <input type="checkbox" checked={!!value(p)} onChange={(e) => setVals({ ...vals, [p.name]: e.target.checked })} />
          ) : (
            <input className="txt mono" value={String(value(p))} onChange={(e) => setVals({ ...vals, [p.name]: e.target.value })} />
          )}
        </div>
      ))}
      {active ? (
        <button className="ghost full" onClick={onCancel}><MousePointerClick size={13} /> {t("editor:pcell.clickToPlace")}</button>
      ) : (
        <button className="primary" onClick={arm}><MousePointerClick size={13} /> {t("editor:tool.placeNamed", { name })}</button>
      )}
      <div className="note">{t("editor:pcell.note")}</div>
    </div>
  );
}
