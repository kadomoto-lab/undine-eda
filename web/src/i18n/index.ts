import i18next from "i18next";
import { initReactI18next, useTranslation } from "react-i18next";
import * as common from "./common";
import * as app from "./app";
import * as editor from "./editor";
import * as agent from "./agent";
import * as review from "./review";
import * as verification from "./verification";
import * as symbol from "./symbol";
import * as symbolcheck from "./symbolcheck";
import * as intent from "./intent";
import * as rtl from "./rtl";
import * as librelane from "./librelane";
import * as cell from "./cell";
import * as history from "./history";

export const catalogs = { common, app, editor, agent, review, verification, symbol, symbolcheck, intent, rtl, librelane, cell, history };
export type Locale = "en" | "ja";
type Catalogs = typeof catalogs;
type Namespace = keyof Catalogs;
type PluralBase<K> = K extends `${infer Base}_other` ? Base : never;
type CatalogKey<N extends Namespace> = keyof Catalogs[N]["en"] & string;
export type MessageKey = { [N in Namespace]: `${N}:${CatalogKey<N> | PluralBase<CatalogKey<N>>}` }[Namespace];
type Source<K extends MessageKey> = K extends `${infer N extends Namespace}:${infer P}`
  ? P extends keyof Catalogs[N]["en"] ? Catalogs[N]["en"][P]
    : `${P}_other` extends keyof Catalogs[N]["en"] ? Catalogs[N]["en"][`${P}_other`] : never : never;
type Variables<S> = S extends `${string}{{${infer V}}}${infer Rest}` ? V | Variables<Rest> : never;
type Params = Record<string, string | number | UiText>;
type Args<K extends MessageKey> = [Variables<Source<K>>] extends [never]
  ? [params?: Params] : [params: { [V in Variables<Source<K>>]: string | number | UiText }];
export interface Message { readonly key: MessageKey; readonly params?: Params }
export type UiText = string | Message | { readonly parts: readonly UiText[]; readonly separator: string };
export const LANGUAGE_KEY = "undine.language";

export function detectLocale(saved: string | null, languages: readonly string[]): Locale {
  if (saved === "en" || saved === "ja") return saved;
  for (const language of languages) {
    const base = language.toLowerCase().split(/[-_]/)[0];
    if (base === "en" || base === "ja") return base;
  }
  return "en";
}
function initialLocale(): Locale {
  let saved: string | null = null;
  try { saved = window.localStorage.getItem(LANGUAGE_KEY); } catch { /* storage may be disabled */ }
  return detectLocale(saved, typeof navigator === "undefined" ? [] : navigator.languages ?? [navigator.language]);
}

export const i18n = i18next.createInstance();
void i18n.use(initReactI18next).init({
  resources: {
    en: Object.fromEntries(Object.entries(catalogs).map(([name, c]) => [name, c.en])),
    ja: Object.fromEntries(Object.entries(catalogs).map(([name, c]) => [name, c.ja])),
  },
  lng: initialLocale(), supportedLngs: ["en", "ja"], fallbackLng: "en",
  defaultNS: "common", keySeparator: false, initAsync: false,
  interpolation: { escapeValue: false }, // React escapes text; never insert translated HTML.
  react: { useSuspense: false },
});
function updateDocumentLanguage() {
  if (typeof document !== "undefined") document.documentElement.lang = i18n.language;
}
updateDocumentLanguage();
i18n.on("languageChanged", updateDocumentLanguage);

export async function setLocale(locale: Locale) {
  try { window.localStorage.setItem(LANGUAGE_KEY, locale); } catch { /* still switch for this session */ }
  await i18n.changeLanguage(locale);
}

/** Stable identity: a language change must never restart effects, requests or jobs. */
export function t<K extends MessageKey>(key: K, ...[params]: Args<K>): string {
  return translate(key, params);
}
function translate(key: MessageKey, params?: Params): string {
  const values = Object.fromEntries(Object.entries(params ?? {}).map(([k, v]) => [k, typeof v === "number" ? v : resolveText(v)]));
  return String(i18n.t(key, values));
}
/** Store this in a notification or dialog; translate when it is displayed. */
export function msg<K extends MessageKey>(key: K, ...[params]: Args<K>): Message { return { key, params }; }
export function joinText(parts: readonly UiText[], separator = "\n"): UiText { return { parts, separator }; }
export function resolveText(value: UiText | undefined | null): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  return "parts" in value ? value.parts.map(resolveText).join(value.separator) : translate(value.key, value.params);
}
export function useI18n() {
  useTranslation(undefined, { i18n });
  return { t, locale: i18n.language as Locale };
}

/** Preserve diagnostic details, including messages from older servers and external tools. */
export function apiErrorText(error: unknown): UiText {
  const e = error as { code?: string; status?: number; detail?: unknown; message?: string; top?: unknown;
    refusal?: Record<string, unknown>; params?: Record<string, unknown> } | null;
  const detail = typeof e?.detail === "string" ? e.detail : e?.detail ? JSON.stringify(e.detail) : e?.message ?? String(error);
  // what a refusal names (api.ts ApiRefusal), from the error's own fields or its params
  const field = (k: string) => { const v = e?.refusal?.[k] ?? e?.params?.[k]; return typeof v === "string" ? v : ""; };
  // a changeset opened in a repository nobody set up for Undine: what to do, naming the History panel's button as it
  // reads there (the server's English names none in the reader's language), and the command line (the server's: the
  // store's root in it when that is not the command's default; from an older server, made of the project)
  if (e?.code === "not_set_up") {
    return msg("common:error.notSetUp", { top: typeof e.top === "string" ? e.top : field("top"), panel: msg("history:button.label"),
      button: msg("history:setup.again"), command: field("command") || `undine git-setup ${field("project") || "<project>"}` });
  }
  // a library a changeset's working copy (a git worktree) cannot hold: which, where, why, and what to do
  if (e?.code === "unsupported_library") {
    const named = { library: field("library"), path: field("path"), target: field("target") };
    const kinds = { link_out: "error.library.linkOut", link_nowhere: "error.library.linkNowhere", own_repository: "error.library.ownRepository" } as const;
    const why = field("kind");
    if (Object.hasOwn(kinds, why)) return msg(`common:${kinds[why as keyof typeof kinds]}`, named);
    return joinText([msg("common:error.library", named), msg("common:error.details", { detail })]);
  }
  const keys = { invalid_request: "error.invalidRequest", not_found: "error.notFound", conflict: "error.conflict",
    readonly: "error.readonly", busy: "error.busy", internal_error: "error.internal", validation_error: "error.validation",
    unauthorized: "error.unauthorized", forbidden: "error.forbidden", upstream_error: "error.upstream", request_failed: "error.request",
    stale: "error.stale" } as const;
  const key = keys[e?.code as keyof typeof keys];
  if (!key) return detail;
  return joinText([msg(`common:${key}`), msg("common:error.details", { detail })]);
}
