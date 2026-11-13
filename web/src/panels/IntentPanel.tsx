// The design intent (docs/design/intent.md), on two levels. Without `view`: the cell intent (on the cell page) —
// readiness per flow step, ports, supplies, goals / preferences / constraints / specs / context with their reasons and
// sources, free notes, and the same content as YAML. With `view` (in an editor): that view's intent, each item linked
// to the cell items it serves, next to the cell intent it comes from (read here, edited on the cell page).
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { ExternalLink, Plus, Redo2, Trash2, Undo2 } from "lucide-react";
import { api, IntentEdit, IntentItem, IntentKind, IntentView } from "../api";
import { msg, t, useI18n, type UiText } from "../i18n";
import { Refused, refusedOr } from "./SimPanel";

const KINDS: IntentKind[] = ["goal", "preference", "constraint", "spec", "context"];
const PORT_KINDS = ["signal_in", "signal_out", "inout", "bias_in", "clock", "supply", "ground"] as const;
const SIDES = ["N", "S", "E", "W", "auto"] as const;
const RULES = ["symmetric", "match", "place", "net"] as const;
const VIEW_KINDS: IntentKind[] = ["constraint", "preference", "spec", "context"];   // how a view realises the cell intent
const STEP_LABEL = { simulation: "intent:step.simulation", layout: "intent:step.layout" } as const;

/** Asks the app to open a cell's page (App listens; editors do not need to know about tabs). */
export const openCellPage = (lib: string, cell: string) =>
  window.dispatchEvent(new CustomEvent("undine:open-cell", { detail: { lib, cell } }));

export function IntentPanel({ project, lib, cell, selection, tick, toast, view }: {
  project: string; lib: string; cell: string; selection: string[]; tick: number;
  toast: (m: UiText, k?: "error" | "info") => void;
  view?: string;              // "schematic" | "symbol" | "rtl" | "layout" | "test:<cell>": this view's intent
}) {
  useI18n();
  const [v, setV] = useState<IntentView | null>(null);
  const [yamlMode, setYamlMode] = useState(false);
  const [yaml, setYaml] = useState("");
  // a spec check the trust gate refused (check_specs and sweeps run ngspice on the testbench, which may place
  // a dependency's cell): shown with Trust... rather than as a toast
  const [refused, setRefused] = useState<unknown>(null);
  useEffect(() => setRefused(null), [project, lib, cell]);

  const load = useCallback(() => {
    api.intent(project, lib, cell).then((r) => { setV(r); setYaml(r.yaml); }).catch((e) => refusedOr(e, setRefused, toast));
  }, [project, lib, cell, toast]);
  useEffect(load, [load, tick]);

  const edit = async (e: IntentEdit) => {
    setRefused(null);
    try {
      const r = await api.intentEdit(project, lib, cell, e);
      setV(r);
      setYaml(r.yaml);
      return true;
    } catch (err) { refusedOr(err, setRefused, toast); return false; }
  };

  const notice = <Refused project={project} error={refused} what={msg("verification:untrusted.specs")} onTrusted={load} />;
  if (!v) return refused ? <div className="inspector panel">{notice}</div> : <div className="inspector panel dim small">…</div>;
  const ro = v.readonly;
  const it = v.intent;
  if (view) return <ViewIntent v={v} view={view} lib={lib} cell={cell} selection={selection} edit={edit} notice={notice} />;
  const ports = [...new Set([...(v.refs?.ports ?? []), ...Object.keys(it.ports)])];
  const missingPorts = new Set(v.requirements.layout?.map((m) => m.entry) ?? []);
  const nets = [...new Set([...Object.keys(it.supplies), ...(v.refs?.nets ?? []), ...(v.refs?.ports ?? [])])];
  const suggested = Object.entries(v.suggested_supplies).filter(([n]) => !it.supplies[n]);

  return (
    <div className="inspector panel intentpanel">
      <div className="row">
        <b className="grow">{t("intent:title")}</b>
        <button className="icon" title={t("intent:undo")} disabled={ro || !v.undo.undo} onClick={() => edit({ op: "undo" })}><Undo2 size={14} /></button>
        <button className="icon" title={t("intent:redo")} disabled={ro || !v.undo.redo} onClick={() => edit({ op: "redo" })}><Redo2 size={14} /></button>
        <button className="ghost small" onClick={() => setYamlMode((y) => !y)}>{yamlMode ? t("intent:form") : t("intent:yaml")}</button>
      </div>
      <div className="dim small">{t("intent:description")}</div>
      <div className="intentsteps">
        {Object.entries(v.requirements).map(([step, missing]) => {
          const label = t(STEP_LABEL[step as keyof typeof STEP_LABEL] ?? "intent:step.layout");
          return <span key={step} className={`pill ${missing.length ? "warn" : "ok"}`} title={missing.map((m) => m.message).join("\n")}>
            {missing.length ? t("intent:step.missing", { step: label, count: missing.length }) : t("intent:step.ready", { step: label })}
          </span>;
        })}
      </div>
      {ro && <div className="note">{t("intent:readOnly")}</div>}
      {notice}
      {!v.refs && <div className="note">{t("intent:noSchematic")}</div>}
      {!!v.problems.length && (
        <>
          <div className="section">{t("intent:problems")}</div>
          {v.problems.map((p, i) => <Problem key={i} p={p} />)}
        </>
      )}

      {yamlMode ? (
        <>
          <textarea className="txt mono intentyaml" rows={18} value={yaml} disabled={ro} onChange={(e) => setYaml(e.target.value)} />
          <button className="primary" disabled={ro || yaml === v.yaml} onClick={async () => { if (await edit({ op: "replace", text: yaml, base: v.sha })) toast(msg("intent:saved"), "info"); }}>{t("intent:yamlSave")}</button>
        </>
      ) : (
        <>
          {!!ports.length && <div className="section">{t("intent:section.ports")}</div>}
          {ports.map((name) => {
            const p = it.ports[name] ?? {};
            return (
              <div key={name} className={`intentport ${missingPorts.has(`ports.${name}`) ? "missing" : ""}`}>
                <span className="mono">{name}</span>
                <select value={p.kind ?? ""} disabled={ro} title={t("intent:port.kind")}
                  onChange={(e) => edit({ op: "set_port", name, fields: { kind: e.target.value } })}>
                  <option value="">{t("intent:port.kind")}</option>
                  {PORT_KINDS.map((k) => <option key={k} value={k}>{t(`intent:kind.${k}`)}</option>)}
                </select>
                <select value={p.side ?? ""} disabled={ro} title={t("intent:port.side")}
                  onChange={(e) => edit({ op: "set_port", name, fields: { side: e.target.value } })}>
                  <option value="">{t("intent:port.side")}</option>
                  {SIDES.map((s) => <option key={s} value={s}>{s === "auto" ? t("intent:side.auto") : s}</option>)}
                </select>
              </div>
            );
          })}

          <div className="section">{t("intent:section.supplies")}</div>
          {nets.filter((n) => it.supplies[n] || v.suggested_supplies[n]).map((n) => (
            <div key={n} className="intentport">
              <span className="mono">{n}</span>
              <select value={it.supplies[n] ?? ""} disabled={ro} onChange={(e) => edit({ op: "set_supply", net: n, role: e.target.value || null })}>
                <option value="">{t("intent:role.none")}</option>
                <option value="supply">{t("intent:role.supply")}</option>
                <option value="ground">{t("intent:role.ground")}</option>
              </select>
            </div>
          ))}
          {!!suggested.length && !ro && (
            <button className="ghost small" onClick={async () => { for (const [n, r] of suggested) await edit({ op: "set_supply", net: n, role: r }); }}>
              {t("intent:useSuggested")}
            </button>
          )}

          {KINDS.map((kind) => {
            const items = it.items.filter((x) => x.kind === kind);
            return items.length ? (
              <div key={kind}>
                <div className="section">{t(`intent:section.${kind}`)}</div>
                {items.map((x) => <ItemRow key={x.id} item={x} readonly={ro} onRemove={() => edit({ op: "remove_item", id: x.id })}
                  onText={(text) => edit({ op: "update_item", id: x.id, fields: { text } })} />)}
              </div>
            ) : null;
          })}
          {!it.items.length && !it.notes && <div className="dim small">{t("intent:empty")}</div>}
          {Object.entries(it.views ?? {}).filter(([, s]) => s.items.length).length > 0 && (
            <div className="dim small">{t("intent:viewCounts", { views: Object.entries(it.views).filter(([, s]) => s.items.length)
              .map(([name, s]) => `${name} (${s.items.length})`).join(", ") })}</div>
          )}
          {!ro && <AddItem refs={v.refs} selection={selection} onAdd={(item) => edit({ op: "add_item", item })} />}

          <div className="section">{t("intent:section.notes")}</div>
          <textarea key={it.notes} className="txt" rows={4} disabled={ro} defaultValue={it.notes} placeholder={t("intent:notesPlaceholder")}
            onBlur={(e) => { if (e.target.value !== it.notes) edit({ op: "set_notes", text: e.target.value }); }} />
        </>
      )}
    </div>
  );
}

function Problem({ p }: { p: IntentView["problems"][number] }) {
  return <div className={`note ${p.level === "error" ? "err" : p.level === "warning" ? "warnnote" : ""} small`}><span className="mono">{p.entry}</span> {p.message}</div>;
}

/** One view's intent, linked to the cell intent: the items of the view (editable), then the cell items they serve
 *  and the rest of the cell intent (read-only; edited on the cell page). */
function ViewIntent({ v, view, lib, cell, selection, edit, notice }: {
  v: IntentView; view: string; lib: string; cell: string; selection: string[]; edit: (e: IntentEdit) => Promise<boolean>;
  notice: ReactNode;
}) {
  const ro = v.readonly;
  const items = v.intent.views?.[view]?.items ?? [];
  const cellItems = v.intent.items;
  const served = new Set(items.flatMap((x) => x.serves ?? []));
  const problems = v.problems.filter((p) => p.entry.startsWith(`views.${view}.`));
  const label = view.startsWith("test:") ? t("intent:view.test", { cell: view.slice(5) }) : t(`intent:view.${view}` as "intent:view.schematic");
  return (
    <div className="inspector panel intentpanel">
      <div className="row">
        <b className="grow">{t("intent:viewTitle", { view: label })}</b>
        <button className="icon" title={t("intent:undo")} disabled={ro || !v.undo.undo} onClick={() => edit({ op: "undo" })}><Undo2 size={14} /></button>
        <button className="icon" title={t("intent:redo")} disabled={ro || !v.undo.redo} onClick={() => edit({ op: "redo" })}><Redo2 size={14} /></button>
      </div>
      <div className="dim small">{t("intent:viewDescription")}</div>
      {ro && <div className="note">{t("intent:readOnly")}</div>}
      {notice}
      {problems.map((p, i) => <Problem key={i} p={p} />)}
      {items.map((x) => <ItemRow key={x.id} item={x} readonly={ro} cellItems={cellItems} onRemove={() => edit({ op: "remove_item", id: x.id })}
        onText={(text) => edit({ op: "update_item", id: x.id, fields: { text } })} />)}
      {!items.length && <div className="dim small">{t("intent:viewEmpty")}</div>}
      {!ro && <AddItem refs={v.refs} selection={selection} kinds={VIEW_KINDS} serveOptions={cellItems}
        onAdd={(item) => edit({ op: "add_item", item, view })} />}

      <div className="section row">
        <span className="grow">{t("intent:cellIntent")}</span>
        <button className="link small" onClick={() => openCellPage(lib, cell)}><ExternalLink size={12} /> {t("intent:openCellPage")}</button>
      </div>
      {v.intent.notes && <div className="small">{v.intent.notes}</div>}
      {cellItems.map((x) => (
        <div key={x.id} className={`intentitem compact ${served.has(x.id) ? "served" : ""}`} title={served.has(x.id) ? t("intent:servedHere") : undefined}>
          <span className="mono dim small">{x.id}</span> <span className="tag">{t(`intent:addKind.${x.kind}`)}</span> {x.text}
        </div>
      ))}
      {!cellItems.length && !v.intent.notes && <div className="dim small">{t("intent:cellEmpty")}</div>}
    </div>
  );
}

/** A substitute rule's cells or a spec's substitute ({lib/cell: its stand-in}), as " a → b, c → null": which cells its
 *  runs simulate as stand-ins (null: as itself). */
function standins(m: unknown): string {
  if (!m || typeof m !== "object" || Array.isArray(m)) return "";
  const pairs = Object.entries(m as Record<string, unknown>).map(([a, b]) => `${a} → ${b == null || b === "" ? "null" : String(b)}`);
  return pairs.length ? ` ${pairs.join(", ")}` : "";
}

function ItemRow({ item, readonly, onRemove, onText, cellItems }: {
  item: IntentItem; readonly: boolean; onRemove: () => void; onText: (s: string) => void; cellItems?: IntentItem[];
}) {
  const servesText = item.serves?.map((id) => {
    const c = cellItems?.find((x) => x.id === id);
    return c ? `${id} (${c.text})` : id;
  }).join(", ");
  const detail = item.rule ? `${item.rule.type}${item.rule.instances ? ` ${item.rule.instances.join(", ")}` : ""}${item.rule.net ? ` ${item.rule.net}` : ""}${standins(item.rule.cells)}`
    : item.spec ? `${item.spec.measure}${item.spec.min !== undefined ? ` ≥ ${item.spec.min}` : ""}${item.spec.max !== undefined ? ` ≤ ${item.spec.max}` : ""}${item.spec.testbench ? ` (${item.spec.testbench})` : ""}${standins(item.spec.substitute)}` : "";
  return (
    <div className="intentitem">
      <div className="row">
        <span className="mono dim small">{item.id}</span>
        {item.kind === "constraint" && <span className={`tag ${item.strength === "should" ? "" : "must"}`}>{t(`intent:strength.${item.strength ?? "must"}`)}</span>}
        <input key={item.text} className="txt grow" defaultValue={item.text} disabled={readonly}
          onBlur={(e) => { if (e.target.value.trim() && e.target.value !== item.text) onText(e.target.value); }}
          onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); }} />
        {!readonly && <button className="icon" title={t("intent:remove")} onClick={onRemove}><Trash2 size={13} /></button>}
      </div>
      {detail && <div className="mono small">{detail}</div>}
      {item.reason && <div className="small"><span className="dim">{t("intent:why")}: </span>{item.reason}</div>}
      <div className="dim small">
        {[item.source?.request ? t("intent:from", { request: item.source.request }) : "", item.source?.by ? t("intent:by", { actor: item.source.by }) : "",
          item.serves?.length ? t("intent:serves", { ids: servesText ?? "" }) : ""].filter(Boolean).join(" · ")}
      </div>
      {item.source?.requests?.map((r, i) => (   // the user's later words, confirming or extending it, oldest first
        <div key={i} className="dim small">{t("intent:then", { date: r.date ?? "?", request: r.request, actor: r.by ?? "?" })}</div>
      ))}
    </div>
  );
}

function AddItem({ refs, selection, onAdd, kinds = KINDS, serveOptions }: {
  refs: IntentView["refs"]; selection: string[]; onAdd: (item: Partial<IntentItem>) => Promise<boolean>;
  kinds?: IntentKind[]; serveOptions?: IntentItem[];   // a view item names the cell items it serves
}) {
  const [kind, setKind] = useState<IntentKind>(kinds[0]);
  const [serves, setServes] = useState<string[]>([]);
  const [text, setText] = useState("");
  const [why, setWhy] = useState("");
  const [rule, setRule] = useState<"" | (typeof RULES)[number]>("");
  const [strength, setStrength] = useState<"must" | "should">("must");
  const [net, setNet] = useState("");
  const [width, setWidth] = useState("");
  const [measure, setMeasure] = useState("");
  const [min, setMin] = useState("");
  const [max, setMax] = useState("");
  const [tb, setTb] = useState("");
  const instances = selection.filter((s) => refs?.instances.includes(s));

  const submit = async () => {
    if (!text.trim()) return;
    const item: Partial<IntentItem> = { kind, text: text.trim(), reason: why.trim() || undefined, ...(serves.length ? { serves } : {}) };
    if (kind === "constraint") {
      item.strength = strength;
      if (rule === "net" && net) item.rule = { type: "net", net, ...(width ? { min_width_um: Number(width) } : {}) };
      else if (rule && rule !== "net" && instances.length) item.rule = { type: rule, instances };
    }
    if (kind === "spec" && measure) item.spec = { measure, ...(min ? { min: Number(min) } : {}), ...(max ? { max: Number(max) } : {}), ...(tb ? { testbench: tb } : {}) };
    if (await onAdd(item)) { setText(""); setWhy(""); setServes([]); }
  };

  return (
    <div className="intentadd">
      <div className="section">{serveOptions ? t("intent:addViewTitle") : t("intent:addTitle")}</div>
      <div className="row">
        <select value={kind} onChange={(e) => setKind(e.target.value as IntentKind)}>
          {kinds.map((k) => <option key={k} value={k}>{t(`intent:addKind.${k}`)}</option>)}
        </select>
        <input className="txt grow" value={text} placeholder={t("intent:textPlaceholder")} onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") submit(); }} />
      </div>
      {kind === "constraint" && (
        <div className="row">
          <select value={strength} onChange={(e) => setStrength(e.target.value as "must" | "should")}>
            <option value="must">{t("intent:strength.must")}</option><option value="should">{t("intent:strength.should")}</option>
          </select>
          <select value={rule} title={t("intent:ruleType")} onChange={(e) => setRule(e.target.value as typeof rule)}>
            <option value="">{t("intent:rule.none")}</option>
            {RULES.map((r) => <option key={r} value={r}>{t(`intent:rule.${r}`)}</option>)}
          </select>
        </div>
      )}
      {kind === "constraint" && rule && rule !== "net" && (
        <div className="small dim">{instances.length ? t("intent:fromSelection", { names: instances.join(", ") }) : t("intent:noSelection")}</div>
      )}
      {kind === "constraint" && rule === "net" && (
        <div className="row">
          <select value={net} onChange={(e) => setNet(e.target.value)} title={t("intent:netName")}>
            <option value="">{t("intent:netName")}</option>
            {(refs?.nets ?? []).map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          <input className="txt" style={{ width: 90 }} value={width} placeholder={t("intent:minWidth")} onChange={(e) => setWidth(e.target.value)} />
        </div>
      )}
      {kind === "spec" && (
        <div className="row">
          <input className="txt" style={{ width: 120 }} value={measure} placeholder={t("intent:measure")} onChange={(e) => setMeasure(e.target.value)} />
          <input className="txt" style={{ width: 60 }} value={min} placeholder={t("intent:min")} onChange={(e) => setMin(e.target.value)} />
          <input className="txt" style={{ width: 60 }} value={max} placeholder={t("intent:max")} onChange={(e) => setMax(e.target.value)} />
          <select value={tb} onChange={(e) => setTb(e.target.value)} title={t("intent:testbench")}>
            <option value="">{t("intent:testbench.none")}</option>
            {(refs?.cells ?? []).map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
      )}
      {serveOptions && (
        <div className="servepick">
          <span className="dim small">{t("intent:servesPick")}</span>
          {serveOptions.length ? serveOptions.map((x) => (
            <label key={x.id} className="check small" title={x.text}>
              <input type="checkbox" checked={serves.includes(x.id)}
                onChange={(e) => setServes(e.target.checked ? [...serves, x.id] : serves.filter((s) => s !== x.id))} />
              <span className="mono">{x.id}</span> <span className="ellip">{x.text}</span>
            </label>
          )) : <span className="dim small">{t("intent:noCellItems")}</span>}
        </div>
      )}
      <div className="row">
        <input className="txt grow" value={why} placeholder={t("intent:whyPlaceholder")} onChange={(e) => setWhy(e.target.value)} />
        <button className="primary" disabled={!text.trim()} onClick={submit}><Plus size={13} /> {t("intent:add")}</button>
      </div>
    </div>
  );
}
