// The language menu in the top bar: the current language as a short code, the languages by their own names in a menu
// (with the name in the current language when it differs). It scales to more languages without growing the top bar.
import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { Check, Languages } from "lucide-react";
import { setLocale, t, useI18n, type Locale } from "./i18n";

const LANGUAGES: { id: Locale; name: string; code: string }[] = [
  { id: "en", name: "English", code: "EN" },
  { id: "ja", name: "日本語", code: "JA" },
];

export function LanguageMenu() {
  const { locale } = useI18n();
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  const items = useRef<(HTMLButtonElement | null)[]>([]);
  const current = LANGUAGES.find((l) => l.id === locale) ?? LANGUAGES[0];

  useEffect(() => {
    if (!open) return;
    items.current[LANGUAGES.findIndex((l) => l.id === locale)]?.focus();
    const away = (e: PointerEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", away);
    return () => document.removeEventListener("pointerdown", away);
  }, [open, locale]);

  const close = () => { setOpen(false); toggle.current?.focus(); };
  const pick = (id: Locale) => { close(); if (id !== locale) void setLocale(id); };
  const onKey = (e: KeyboardEvent) => {
    if (!open) return;
    if (e.key === "Escape") { e.preventDefault(); close(); }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const n = LANGUAGES.length, i = items.current.indexOf(document.activeElement as HTMLButtonElement);
      items.current[(i + (e.key === "ArrowDown" ? 1 : n - 1) + n) % n]?.focus();
    }
  };

  return (
    <div className="langmenu" ref={root} onKeyDown={onKey}>
      <button ref={toggle} className={`ghost act${open ? " on" : ""}`} aria-haspopup="menu" aria-expanded={open}
        title={t("common:language.label")} aria-label={`${t("common:language.label")}: ${current.name}`} onClick={() => setOpen((v) => !v)}>
        <Languages size={14} /> {current.code}
      </button>
      {open && (
        <div className="langpop" role="menu" aria-label={t("common:language.label")}>
          {LANGUAGES.map((l, i) => {
            const here = t(`common:language.${l.id}`);
            return (
              <button key={l.id} ref={(e) => { items.current[i] = e; }} role="menuitemradio" aria-checked={l.id === locale}
                lang={l.id} className={l.id === locale ? "on" : ""} onClick={() => pick(l.id)}>
                <span className="ck">{l.id === locale && <Check size={14} />}</span>
                {l.name}
                {here !== l.name && <span className="hint">{here}</span>}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
