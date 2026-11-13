// In-app dialogs instead of the browser's prompt() / confirm(): the same look as the rest of the UI, several
// fields in one form, validation while typing, and server errors shown in the dialog (it stays open).
import { useEffect, useRef, useState } from "react";
import { AlertTriangle, X } from "lucide-react";
import { apiErrorText, msg, resolveText, useI18n, type UiText } from "./i18n";

export interface Field {
  name: string;
  label: UiText;
  type?: "text" | "select" | "textarea" | "checks";   // checks: value is the ticked option values, comma-separated
  value?: string;
  placeholder?: UiText;
  hint?: UiText | ((values: Record<string, string>) => UiText);
  options?: { value: string; label?: UiText; hint?: UiText }[];   // select, checks
  suggestions?: string[];                           // text: clickable completions under the field
  required?: boolean;
  validate?: (v: string, all: Record<string, string>) => UiText | null; // an error message, or null
  mono?: boolean;
}

export interface FormSpec {
  title: UiText;
  message?: UiText;
  aside?: UiText;          // under the message, lighter: what is said but asks nothing (warnings, notes)
  fields: Field[];
  submitLabel?: UiText;
  /** Runs the action; throw (or return a message) to keep the dialog open with the error shown. */
  submit?: (values: Record<string, string>) => Promise<UiText | void> | UiText | void;
}

interface ConfirmSpec {
  title: UiText;
  message: UiText;
  okLabel?: UiText;
  danger?: boolean;
}

type Req = { id: number } & (
  | { kind: "form"; spec: FormSpec; done: (v: Record<string, string> | null) => void }
  | { kind: "confirm"; spec: ConfirmSpec; done: (ok: boolean) => void });

let push: ((r: Req) => void) | null = null;
let seq = 0;

/** Shows a form; resolves with the values after `submit` succeeded, or null when cancelled. */
export function ask(spec: FormSpec): Promise<Record<string, string> | null> {
  return new Promise((done) => (push ? push({ id: ++seq, kind: "form", spec, done }) : done(null)));
}

/** A yes / no question. */
export function confirmDialog(spec: ConfirmSpec): Promise<boolean> {
  return new Promise((done) => (push ? push({ id: ++seq, kind: "confirm", spec, done }) : done(false)));
}

/** Names of projects, libraries and cells: what the server accepts (project.py NAME_RE). */
export const validName = (v: string) =>
  !v ? msg("common:validation.required") : /^[A-Za-z0-9_][A-Za-z0-9_.+-]*$/.test(v) ? null : msg("common:validation.name");

/** Mount once: renders the dialog that ask() / confirmDialog() request. */
export function DialogHost() {
  const [queue, setQueue] = useState<Req[]>([]);
  useEffect(() => {
    push = (r) => setQueue((q) => [...q, r]);
    return () => { push = null; };
  }, []);
  const cur = queue[0];
  if (!cur) return null;
  const close = () => setQueue((q) => q.slice(1));
  return cur.kind === "form"
    ? <FormDialog key={cur.id} spec={cur.spec} onDone={(v) => { cur.done(v); close(); }} />
    : <ConfirmBox key={cur.id} spec={cur.spec} onDone={(ok) => { cur.done(ok); close(); }} />;
}

function Shell({ title, onClose, children }: { title: UiText; onClose: () => void; children: React.ReactNode }) {
  const { t } = useI18n();
  // Esc cancels wherever the focus is, and never reaches the editor behind (where Esc changes the tool)
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
  return (
    <div className="modalback" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal" role="dialog" aria-modal="true" aria-label={resolveText(title)}>
        <div className="modalhead">
          <span className="grow">{resolveText(title)}</span>
          <button className="icon" title={t("common:actions.cancelShortcut")} onClick={onClose}><X size={15} /></button>
        </div>
        {children}
      </div>
    </div>
  );
}

function FormDialog({ spec, onDone }: { spec: FormSpec; onDone: (v: Record<string, string> | null) => void }) {
  const { t } = useI18n();
  const [vals, setVals] = useState<Record<string, string>>(
    () => Object.fromEntries(spec.fields.map((f) => [f.name, f.value ?? (f.type === "select" ? f.options?.[0]?.value ?? "" : "")])));
  const [touched, setTouched] = useState<Record<string, boolean>>({});
  const [err, setErr] = useState<UiText>("");
  const [busy, setBusy] = useState(false);
  const first = useRef<HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement | null>(null);
  useEffect(() => {
    const el = first.current;
    el?.focus();
    if (el instanceof HTMLInputElement) el.select();
  }, []);

  const fieldError = (f: Field): UiText | null => {
    const v = vals[f.name] ?? "";
    if (f.required && !v.trim()) return msg(f.type === "checks" ? "common:validation.chooseOne" : "common:validation.required");
    return f.validate ? f.validate(v, vals) : null;
  };
  const errors = Object.fromEntries(spec.fields.map((f) => [f.name, fieldError(f)]));
  const invalid = Object.values(errors).some(Boolean);

  const submit = async () => {
    setTouched(Object.fromEntries(spec.fields.map((f) => [f.name, true])));
    if (invalid || busy) return;
    setBusy(true);
    setErr("");
    try {
      const r = spec.submit ? await spec.submit(vals) : undefined;
      if (r) { setErr(r); return; }
      onDone(vals);
    } catch (e) {
      setErr(apiErrorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Shell title={spec.title} onClose={() => onDone(null)}>
      <form className="modalbody" onSubmit={(e) => { e.preventDefault(); submit(); }}>
        {spec.message && <div className="dim small" style={{ whiteSpace: "pre-line" }}>{resolveText(spec.message)}</div>}
        {spec.aside && <div className="faint small" style={{ whiteSpace: "pre-line" }}>{resolveText(spec.aside)}</div>}
        {spec.fields.map((f, i) => {
          const showErr = touched[f.name] && errors[f.name];
          const hint = typeof f.hint === "function" ? f.hint(vals) : f.hint;
          const common = {
            className: `txt full ${f.mono ? "mono" : ""} ${showErr ? "bad" : ""}`,
            value: vals[f.name] ?? "",
            placeholder: f.placeholder === undefined ? undefined : resolveText(f.placeholder),
            ref: i === 0 ? (el: HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement | null) => { first.current = el; } : undefined,
          };
          const set = (v: string) => { setVals((o) => ({ ...o, [f.name]: v })); setErr(""); };
          const Outer = f.type === "checks" ? "div" : "label"; // checkboxes carry their own labels
          const sugg = (f.suggestions ?? []).filter((s) => s !== vals[f.name] && s.toLowerCase().includes((vals[f.name] ?? "").toLowerCase())).slice(0, 8);
          return (
            <Outer key={f.name} className="stack modalfield">
              <span className="dim small">{resolveText(f.label)}</span>
              {f.type === "checks" ? (
                <div className="checkrow">
                  {(f.options ?? []).map((o) => {
                    const on = (vals[f.name] ?? "").split(",").filter(Boolean);
                    const ticked = on.includes(o.value);
                    return (
                      <label key={o.value} className={`checkopt ${ticked ? "on" : ""}`} title={resolveText(o.hint)}>
                        <input type="checkbox" checked={ticked} ref={i === 0 && o === f.options![0] ? (el) => { first.current = el; } : undefined}
                          onChange={() => { set((ticked ? on.filter((x) => x !== o.value) : [...on, o.value]).join(",")); setTouched((t) => ({ ...t, [f.name]: true })); }} />
                        {resolveText(o.label ?? o.value)}
                      </label>
                    );
                  })}
                </div>
              ) : f.type === "select" ? (
                <select {...common} onChange={(e) => set(e.target.value)}>
                  {(f.options ?? []).map((o) => <option key={o.value} value={o.value}>{resolveText(o.label ?? o.value)}</option>)}
                </select>
              ) : f.type === "textarea" ? (
                <textarea {...common} rows={3} onChange={(e) => set(e.target.value)}
                  onBlur={() => setTouched((t) => ({ ...t, [f.name]: true }))}
                  onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submit(); } }} />
              ) : (
                <input {...common} spellCheck={false} autoComplete="off" onChange={(e) => set(e.target.value)}
                  onBlur={() => setTouched((t) => ({ ...t, [f.name]: true }))} />
              )}
              {showErr ? <span className="fielderr">{resolveText(errors[f.name])}</span> : hint ? <span className="faint small">{resolveText(hint)}</span> : null}
              {sugg.length > 0 && (
                <div className="suggest">
                  {sugg.map((s) => <button type="button" key={s} className="chip"
                    onMouseDown={(e) => e.preventDefault()} // Keep focus: blur validation can move this button before mouseup.
                    onClick={() => set(s)}>{s}</button>)}
                </div>
              )}
            </Outer>
          );
        })}
        {err && <div className="note err"><AlertTriangle size={13} /> {resolveText(err)}</div>}
        <div className="modalfoot">
          <button type="button" className="ghost" onClick={() => onDone(null)}>{t("common:actions.cancel")}</button>
          <button type="submit" className="primary" disabled={busy}>{busy ? t("common:state.working") : resolveText(spec.submitLabel ?? msg("common:actions.ok"))}</button>
        </div>
      </form>
    </Shell>
  );
}

function ConfirmBox({ spec, onDone }: { spec: ConfirmSpec; onDone: (ok: boolean) => void }) {
  const { t } = useI18n();
  const ok = useRef<HTMLButtonElement>(null);
  useEffect(() => ok.current?.focus(), []);
  return (
    <Shell title={spec.title} onClose={() => onDone(false)}>
      <div className="modalbody">
        <div>{resolveText(spec.message)}</div>
        <div className="modalfoot">
          <button className="ghost" onClick={() => onDone(false)}>{t("common:actions.cancel")}</button>
          <button ref={ok} className={`primary ${spec.danger ? "danger" : ""}`} onClick={() => onDone(true)}>{resolveText(spec.okLabel ?? msg("common:actions.ok"))}</button>
        </div>
      </div>
    </Shell>
  );
}
