// The Trust dialog (docs/design/packages.md, Security: trust): what a dependency would run or read that the trust
// rule refuses (.control, *# lines, files outside the PDK, $c, DPI, Tcl), grouped by the file it is in, and the key a
// person trusts (name@version, or name@commit12 without a manifest). Like the PDK Fetch dialog it asks the server for
// a confirmation token only after the person clicks Trust; the token is for that key and the installed tree, once.
// Taking trust back needs no token. No agent tool reaches this: the routes need the token a click asks for.
import { useEffect, useRef, useState } from "react";
import { AlertTriangle, Loader2, X } from "lucide-react";
import { api, type TrustDependency, type TrustFinding } from "./api";
import { apiErrorText, msg, resolveText, useI18n, type UiText } from "./i18n";

const KINDS = new Set(["control", "control_line", "path", "unparsed", "rtl_c", "systemc", "rtl_system", "dpi", "rtl_path",
  "rtl_define", "tcl"]);
/** A finding's kind in words, or as the server named it when this UI has no words for it. */
export const findingKind = (kind: string): UiText =>
  KINDS.has(kind) ? msg(`app:trust.kind.${kind as "control"}`) : kind;

const SHOWN = 200;          // findings listed; a generated netlist can hold thousands of lines

/** The findings by file, in the order the server gave them. */
function byFile(findings: TrustFinding[]): [string, TrustFinding[]][] {
  const out = new Map<string, TrustFinding[]>();
  for (const f of findings.slice(0, SHOWN)) out.set(f.file, [...out.get(f.file) ?? [], f]);
  return [...out];
}

/** The findings, grouped by file: kind, line and the line's text. */
export function FindingList({ findings }: { findings: TrustFinding[] }) {
  const { t } = useI18n();
  return (
    <div data-testid="trust-findings">
      {byFile(findings).map(([file, list]) => (
        <div key={file} className="stack">
          <b className="mono small">{file}</b>
          {list.map((f, i) => (
            <div key={i} className="small" style={{ paddingLeft: 12 }}>
              <span className="dim">{resolveText(findingKind(f.kind))}{f.line > 0 ? `, ${t("app:trust.line", { line: f.line })}` : ""}: </span>
              <span className="mono" style={{ overflowWrap: "anywhere" }}>{f.text}</span>
            </div>
          ))}
        </div>
      ))}
      {findings.length > SHOWN && <div className="faint small">{t("app:trust.more", { count: findings.length - SHOWN })}</div>}
    </div>
  );
}

export function TrustDialog({ project, name, onClose, onChanged }: {
  project: string;
  name: string;                                         // the dependency's local name (.undine/deps/<name>)
  onClose: () => void;
  onChanged?: (trusted: boolean) => void | Promise<void>;   // trusted or taken back: the lock status reads again
}) {
  const { t } = useI18n();
  const [dep, setDep] = useState<TrustDependency | null>(null);
  const [err, setErr] = useState<UiText>("");
  const [done, setDone] = useState<UiText>("");
  const [busy, setBusy] = useState(false);
  const live = useRef(true);
  useEffect(() => () => { live.current = false; }, []);

  useEffect(() => {
    setDep(null);
    setErr("");
    api.trustList(project).then((r) => {
      if (!live.current) return;
      const d = r.dependencies.find((x) => x.name === name);
      if (d) setDep(d);
      else setErr(msg("app:trust.missing", { name }));
    }).catch((e) => { if (live.current) setErr(apiErrorText(e)); });
  }, [project, name]);

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

  const change = async (trust: boolean) => {
    if (!dep || busy) return;
    setBusy(true);
    setErr("");
    try {
      // the token only now, after the click: the server binds it to the key and the tree it finds installed, and
      // refuses the trust (403) when either changed since
      const r = trust ? await api.trust(project, name, (await api.trustConfirm(project, name)).token)
        : await api.untrust(project, name);
      if (!live.current) return;
      setDep({ ...dep, key: r.key || dep.key, trusted: r.trusted });
      setDone(msg(r.trusted ? "app:trust.trusted" : "app:trust.untrusted", { key: r.key || dep.key }));
      await onChanged?.(r.trusted);
    } catch (e) {
      if (live.current) setErr(apiErrorText(e));
    } finally {
      if (live.current) setBusy(false);
    }
  };

  const row = (label: UiText, value: UiText, mono = true) => (
    <div className="kv"><span>{resolveText(label)}</span><b className={mono ? "mono" : ""}>{resolveText(value)}</b></div>);
  return (
    <div className="modalback" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={t("app:trust.title", { name })}>
        <div className="modalhead">
          <span className="grow">{t("app:trust.title", { name })}</span>
          <button className="icon" title={t("common:actions.cancelShortcut")} onClick={onClose}><X size={15} /></button>
        </div>
        <div className="modalbody">
          <div className="dim small">{t("app:trust.description")}</div>
          {dep ? (
            <>
              <div>
                {row(msg("app:trust.key"), dep.key)}
                {row(msg("app:trust.package"), dep.package ? `${dep.package} ${dep.version ?? ""}`.trim() : msg("app:trust.noPackage"), !!dep.package)}
                {row(msg("app:trust.state"), msg(dep.trusted ? "app:trust.isTrusted" : "app:trust.isNotTrusted"), false)}
              </div>
              <div className="dim small">{t("app:trust.findings")}</div>
              {dep.findings.length ? <FindingList findings={dep.findings} /> : <div className="faint small">{t("app:trust.none")}</div>}
            </>
          ) : !err && <div className="dim small"><Loader2 size={12} className="spin" /> {t("app:trust.loading")}</div>}
          {done && <div className="note">{resolveText(done)}</div>}
          {err && <div className="note err"><AlertTriangle size={13} /> {resolveText(err)}</div>}
          <div className="modalfoot">
            <button type="button" className="ghost" onClick={onClose}>
              {done ? t("common:actions.close") : t("common:actions.cancel")}
            </button>
            {dep?.trusted && (
              <button type="button" className="ghost" disabled={busy} onClick={() => change(false)}>
                {busy ? t("common:state.working") : t("app:trust.untrust")}
              </button>
            )}
            {dep && !dep.trusted && (
              <button type="button" className="primary" disabled={busy} onClick={() => change(true)}>
                {busy ? t("common:state.working") : t("app:trust.trust")}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
