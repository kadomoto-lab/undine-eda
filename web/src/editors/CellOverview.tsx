// The cell page: the cell is the unit, its intent is on top, its views are under it (server/undine/store/cellviews.py).
// Above the waterline is what people and the agent write (the intent, the source views, the testbenches); below it, on
// deeper water, what is generated from them (the netlist, the checks), each marked when what it came from has changed.
// Everything shown comes from the server: the page groups, summarises and hides, it never makes up a relation.
import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, Box, CircleHelp, CircuitBoard, FileCode2, FlaskConical, Pencil, RefreshCw, Shapes, X } from "lucide-react";
import { api, CellOverview as Overview, IntentItem, SignoffCheck, ViewKind } from "../api";
import { IntentPanel } from "../panels/IntentPanel";
import { LibreLanePanel } from "../panels/LibreLanePanel";
import { StateIcon, jobStateText } from "../panels/SimPanel";
import { apiErrorText, i18n, t, useI18n, type UiText } from "../i18n";
import { T } from "../theme";

const ICON = { schematic: CircuitBoard, symbol: Shapes, rtl: FileCode2, layout: Box } as const;
const KIND_ORDER = ["goal", "preference", "spec", "constraint", "context"] as const;

const list = (xs: string[]) => new Intl.ListFormat(i18n.language, { type: "conjunction" }).format(xs);
const view = (v: string) => t(`cell:view.${v}` as "cell:view.schematic");

/** One or two sentences on where the cell stands, from what the server reports: which source views agree, which are
 * out of date, and how its checks stand. */
function summary(o: Overview): string[] {
  const out = [t("cell:sum.in", { lib: o.lib })];
  const existing = o.design.filter((d) => d.exists && d.view !== "rtl");
  const stale = existing.filter((d) => d.fresh === false);
  if (existing.length > 1 && !stale.length && existing.some((d) => d.fresh === true))
    out.push(t("cell:sum.agree", { views: list(existing.map((d) => view(d.view).toLocaleLowerCase())) }));
  if (stale.length) out.push(t("cell:sum.stale", { views: list(stale.map((d) => view(d.view).toLocaleLowerCase())) }));
  const checks = (o.derived.find((d) => d.view === "checks") as { checks: SignoffCheck[] } | undefined)?.checks.filter((c) => c.job) ?? [];
  const old = checks.filter((c) => c.fresh === false).length;
  const failed = checks.filter((c) => c.state === "done" && c.passed === false).length;
  if (old) out.push(t("cell:sum.checksOld", { count: old }));
  if (failed) out.push(t("cell:sum.checksFail", { count: failed }));
  if (checks.length && !old && !failed && checks.every((c) => c.state === "done")) out.push(t("cell:sum.checksPass"));
  return out;
}

export function CellOverview({ project, lib, cell, active, readonly, tick, onOpenView, onOpenCell, onCreate, toast }: {
  project: string; lib: string; cell: string; active: boolean; readonly: boolean; tick: number;
  onOpenView: (view: ViewKind) => void;
  onOpenCell: (lib: string, cell: string, view?: ViewKind) => void;
  onCreate: (view: "schematic" | "symbol" | "layout") => Promise<void>;
  toast: (m: UiText, k?: "error" | "info") => void;
}) {
  useI18n();
  const [o, setO] = useState<Overview | null>(null);
  const [editing, setEditing] = useState(false);
  const [netlist, setNetlist] = useState<string | null>(null);
  const [n, setN] = useState(0);
  const [lanePorts, setLanePorts] = useState<string[] | null>(null);   // the LibreLane panel, open: the schematic's ports
  const [laneFlow, setLaneFlow] = useState(false);   // a flow builds this cell from its schematic (source schematic)

  const load = useCallback(() => {
    api.overview(project, lib, cell).then(setO).catch((e) => toast(apiErrorText(e), "error"));
    api.flows(project).then((r) => setLaneFlow(r.flows.some((f) => f.source === "schematic" && f.top.lib === lib && f.top.cell === cell)))
      .catch(() => setLaneFlow(false));
  }, [project, lib, cell, toast]);
  useEffect(() => { if (active) load(); }, [load, active, tick, n]);

  if (!o) return <div className="cellpage"><div className="cellsheet dim small">…</div></div>;
  const it = o.intent;
  const grouped = KIND_ORDER.map((k) => [k, it.items.filter((x) => x.kind === k)] as const).filter(([, xs]) => xs.length);
  const viewIntent = Object.values(it.views).reduce((a, b) => a + b, 0);
  const netlistRow = o.derived.find((d) => d.view === "netlist");
  const checksRow = o.derived.find((d) => d.view === "checks");
  const hasLayout = o.design.some((d) => d.view === "layout" && d.exists);
  // a flow builds this cell from its schematic: its LibreLane panel is here (a cell with RTL has it in its RTL view)
  const fromSchematic = laneFlow && o.design.some((d) => d.view === "schematic" && d.exists);
  const steps = Object.entries(it.missing);
  const ready = steps.filter(([, c]) => !c).map(([s]) => t(`intent:step.${s}` as "intent:step.layout"));
  const meta = [
    (it.problems.error + it.problems.warning + it.problems.info) > 0 ? t("cell:problems", it.problems) : "",
    it.ports > 0 ? t("cell:ports", { count: it.ports }) : "",
    Object.keys(it.supplies).length ? t("cell:supplies", { nets: Object.entries(it.supplies).map(([n, r]) => `${n}=${r}`).join(", ") }) : "",
    viewIntent > 0 ? t("cell:viewIntent", { count: viewIntent }) : "",
  ].filter(Boolean);

  return (
    <div className="cellpage">
      <div className="cellsheet">
        <header className="cellhead">
          <h1>{cell}</h1>
          <button className="icon" title={t("cell:reload")} onClick={() => setN((x) => x + 1)}><RefreshCw size={14} /></button>
          <p className="cellsum">{o.exists ? summary(o).join(" ") : t("cell:missingCell")}</p>
        </header>

        <section className="cellsec">
          <div className="cellsechead">
            <h2>{t("cell:intent")}</h2>
            <span className="dim small grow">{t("cell:intentHint")}</span>
            {editing
              ? <button className="ghost small" onClick={() => { setEditing(false); setN((x) => x + 1); }}><X size={13} /> {t("cell:closeIntent")}</button>
              : <button className="ghost small" onClick={() => setEditing(true)}><Pencil size={13} /> {t("cell:editIntent")}</button>}
          </div>
          {editing ? (
            <div className="cellintenteditor">
              <IntentPanel project={project} lib={lib} cell={cell} tick={tick + n} toast={toast} selection={[]} />
            </div>
          ) : (
            <>
              {(grouped.length > 0 || it.notes) && (
                <dl className="intentgrid">
                  {it.notes && <div className="intentgroup"><dt>{t("intent:section.notes")}</dt><dd className="cellnotes">{it.notes}</dd></div>}
                  {grouped.map(([kind, xs]) => (
                    <div key={kind} className="intentgroup">
                      <dt>{t(`intent:section.${kind}`)}</dt>
                      <dd>{xs.map((x) => <IntentLine key={x.id} item={x} />)}</dd>
                    </div>
                  ))}
                </dl>
              )}
              {!it.items.length && !it.notes && <p className="dim small">{t("cell:noIntent")}</p>}
              {steps.length > 0 && (
                <p className="cellready">
                  {ready.length > 0 && <span className="ok">{t("cell:readyFor", { steps: list(ready) })} </span>}
                  {steps.filter(([, c]) => c).map(([s, c]) => (
                    <span key={s} className="warn">{t("cell:stillNeeded", { step: t(`intent:step.${s}` as "intent:step.layout"), count: c })} </span>
                  ))}
                  {steps.some(([, c]) => c) && !readonly && <button className="link" onClick={() => setEditing(true)}>{t("cell:answer")}</button>}
                </p>
              )}
              {meta.length > 0 && <p className="cellmeta">{meta.join(" · ")}</p>}
            </>
          )}
        </section>

        <section className="cellsec">
          <div className="cellsechead"><h2>{t("cell:views")}</h2><span className="dim small grow">{t("cell:viewsHint")}</span></div>
          {o.design.map((d) => {
            const Icon = ICON[d.view];
            return (
              <div key={d.view} className={`viewrow ${d.exists ? "" : "absent"}`}>
                <Icon size={15} className={`tv-${d.view}`} />
                <span className="viewname">{view(d.view)}</span>
                <span className="viewstate grow">
                  {!d.exists && <span>{t("cell:absent")}</span>}
                  {d.exists && d.fresh === true && (d.view === "symbol" || d.view === "layout") && <span>{t(`cell:fresh.${d.view}`)}</span>}
                  {d.exists && d.fresh === false && <Fresh fresh={false} />}
                  {d.intent > 0 && <span title={t("intent:viewTitle", { view: t(`intent:view.${d.view}`) })}>{t("cell:intentCount", { count: d.intent })}</span>}
                  {d.notes.length > 0 && <span className="warn">{d.notes.join(" · ")}</span>}
                </span>
                {d.exists
                  ? <button className="ghost small" onClick={() => onOpenView(d.view)}>{t("cell:openView")}</button>
                  : !readonly && d.view !== "rtl" && <button className="ghost small" onClick={async () => { await onCreate(d.view as "schematic"); setN((x) => x + 1); }}>{t("cell:create")}</button>}
              </div>
            );
          })}
          {o.tests.map((tb) => (
            <div key={`${tb.lib}/${tb.cell}`} className={`viewrow ${tb.exists ? "" : "absent"}`}>
              <FlaskConical size={15} className="dim" />
              <span className="viewname mono">{tb.lib !== lib ? `${tb.lib}/` : ""}{tb.cell}</span>
              <span className="viewstate grow">
                <span>{t("cell:testbench")}</span>
                <span>{tb.why.join(", ")}</span>
                {!tb.exists && <span className="warn">{t("cell:missingCellFile")}</span>}
                {tb.simulation
                  ? <span><StateIcon s={tb.simulation.state} /> {t("cell:simLast", { state: tb.simulation.state })} <Fresh fresh={tb.simulation.fresh} changed={tb.simulation.changed} /></span>
                  : tb.exists && <span>{t("cell:noRun")}</span>}
                {tb.intent > 0 && <span>{t("cell:intentCount", { count: tb.intent })}</span>}
              </span>
              {tb.exists && <button className="ghost small" onClick={() => onOpenCell(tb.lib, tb.cell, "schematic")}>{t("cell:openView")}</button>}
            </div>
          ))}
          {!o.tests.length && <div className="viewrow absent"><FlaskConical size={15} className="dim" /><span className="viewstate">{t("cell:noTests")}</span></div>}
          {o.used_in.length > 0 && (
            <p className="usedin">
              <span className="dim">{t("cell:usedIn")}</span>
              {o.used_in.map((u) => (
                <button key={`${u.lib}/${u.cell}`} className="link mono" onClick={() => onOpenCell(u.lib, u.cell)}>{u.lib !== lib ? `${u.lib}/` : ""}{u.cell}</button>
              ))}
            </p>
          )}
        </section>
      </div>

      <div className="waterline"><span>{t("cell:waterline")}</span></div>

      <div className="below">
        <div className="cellsheet">
          {netlistRow && netlistRow.view === "netlist" && (
            <section className="cellsec">
              <div className="cellsechead">
                <h2>{view("netlist")}</h2>
                <span className="dim small grow">{netlistRow.available ? t("cell:onDemand", { views: netlistRow.from.join(", ") }) : t("cell:absent")}</span>
                {netlistRow.available && netlistRow.from.includes("schematic") && (
                  <button className="ghost small" onClick={async () => {
                    if (netlist !== null) { setNetlist(null); return; }
                    try { setNetlist((await api.netlist(project, lib, cell, "undine", "spice", "spice")).text); } catch (e) { toast(apiErrorText(e), "error"); }
                  }}>{netlist === null ? t("cell:showNetlist") : t("cell:hideNetlist")}</button>
                )}
              </div>
              {netlist !== null && <pre className="logbox">{netlist}</pre>}
            </section>
          )}
          {checksRow && checksRow.view === "checks" && (
            <section className="cellsec">
              <div className="cellsechead">
                <h2>{t("cell:checks")}</h2>
                <span className="dim small grow">{checksRow.from.length ? t("cell:from", { views: checksRow.from.join(", ") }) : ""}</span>
                {hasLayout && <button className="ghost small" onClick={() => onOpenView("layout")}>{t("cell:openView")}</button>}
              </div>
              {checksRow.checks.length > 0 && (
                <div className="checkgrid">
                  {checksRow.checks.map((c) => <CheckRow key={c.name} c={c} />)}
                </div>
              )}
              {!hasLayout && <p className="dim small">{t("cell:noChecks")}</p>}
            </section>
          )}
          {fromSchematic && (
            <section className="cellsec">
              <div className="cellsechead">
                <h2>{t("librelane:tab")}</h2>
                <span className="dim small grow">{t("cell:librelaneHint")}</span>
                <button className="ghost small" onClick={async () => {
                  if (lanePorts !== null) { setLanePorts(null); return; }
                  try { setLanePorts(((await api.schematic(project, lib, cell, "schematic")).ports ?? []).map(([p]) => p)); }
                  catch (e) { toast(apiErrorText(e), "error"); }
                }}>{lanePorts === null ? t("cell:showLibrelane") : t("cell:hideLibrelane")}</button>
              </div>
              {lanePorts !== null && (
                <LibreLanePanel project={project} lib={lib} cell={cell} ports={lanePorts} source="schematic"
                  onOpenIntent={() => setEditing(true)} onImported={() => setN((x) => x + 1)} toast={toast} />
              )}
            </section>
          )}
        </div>
      </div>
    </div>
  );
}

function CheckRow({ c }: { c: SignoffCheck }) {
  const result = c.error ? <span className="badc">{c.error}</span>
    : !c.job ? <span className="dim">{t("cell:noRun")}</span>
    : c.state !== "done" ? <span className="dim">{jobStateText(c.state ?? "queued")}</span>
    : c.passed === null || c.passed === undefined ? <span className="dim">{jobStateText(c.state)}</span>
    : c.passed
      ? <span className="ok">{t(c.kind === "drc" ? "cell:result.clean" : c.kind === "lvs" ? "cell:result.matched" : "cell:result.passed")}</span>
      : <span className="badc">{c.kind === "drc" && c.summary && "total" in c.summary ? t("cell:result.violations", { count: c.summary.total })
        : t(c.kind === "lvs" ? "cell:result.mismatch" : "cell:result.failed")}</span>;
  return (
    <div className="checkline" title={c.changed?.length ? c.changed.join("\n") : undefined}>
      {c.error ? <AlertTriangle size={13} color={T.err} /> : c.state ? <StateIcon s={c.state} /> : <CircleHelp size={13} color={T.dim} />}
      <span className="ellip">{c.name}</span>
      {result}
      <span>{c.job && <Fresh fresh={c.fresh} changed={c.changed} />}</span>
    </div>
  );
}

function IntentLine({ item }: { item: IntentItem }) {
  const spec = item.spec ? ` — ${item.spec.measure}${item.spec.min !== undefined ? ` ≥ ${item.spec.min}` : ""}${item.spec.max !== undefined ? ` ≤ ${item.spec.max}` : ""}${item.spec.testbench ? ` (${item.spec.testbench})` : ""}` : "";
  return (
    <div className="intentline" title={item.reason ?? undefined}>
      <span className="mono dim small">{item.id}</span>
      {item.kind === "constraint" && <span className={`tag ${item.strength === "should" ? "" : "must"}`}>{t(`intent:strength.${item.strength ?? "must"}`)}</span>}
      <span>{item.text}<span className="dim small">{spec}</span></span>
    </div>
  );
}

function Fresh({ fresh, changed }: { fresh?: boolean | null; changed?: string[] }) {
  if (fresh === undefined || fresh === null) return null;
  return fresh
    ? <span className="tag ok">{t("cell:current")}</span>
    : <span className="tag warn" title={changed?.join("\n")}>{t("cell:outdated")}</span>;
}
