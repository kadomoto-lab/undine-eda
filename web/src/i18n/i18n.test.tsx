// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useEffect, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ask, DialogHost } from "../dialog";
import { ApiError } from "../api";
import { apiErrorText, catalogs, detectLocale, i18n, joinText, LANGUAGE_KEY, msg, resolveText, setLocale, t, useI18n } from ".";

beforeEach(async () => {
  // Isolate browser preferences (also avoids Node's experimental storage getter).
  const values = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
  });
  await setLocale("en");
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("translation contract", () => {
  for (const [name, c] of Object.entries(catalogs)) {
    it(`${name}: both languages cover every message and interpolation`, () => {
      const english: Record<string, string> = c.en, japanese: Record<string, string> = c.ja;
      // Plural categories can differ by language; compare each logical message separately.
      const base = (key: string) => key.replace(/_(zero|one|two|few|many|other)$/, "");
      expect([...new Set(Object.keys(japanese).map(base))].sort()).toEqual([...new Set(Object.keys(english).map(base))].sort());
      const variables = (s: string) => [...new Set([...s.matchAll(/{{(.*?)}}/g)].map((m) => m[1]))].sort();
      for (const [key, value] of Object.entries(english)) {
        const variants = Object.entries(japanese).filter(([k]) => base(k) === base(key));
        for (const [jk, jv] of variants) {
          expect(jv.trim(), `${name}:${jk} must not be empty`).not.toBe("");
          expect(variables(jv), `${name}:${jk}`).toEqual(variables(value));
        }
      }
      for (const [locale, entries] of [["en", english], ["ja", japanese]] as const) {
        const categories = new Intl.PluralRules(locale).resolvedOptions().pluralCategories;
        const pluralBases = new Set(Object.keys(entries).filter((k) => k.endsWith("_other")).map(base));
        for (const b of pluralBases) for (const category of categories) expect(entries[`${b}_${category}`], `${name}:${b}_${category}`).toBeTruthy();
      }
    });
  }

  it("prefers explicit language, then supported browser preferences", () => {
    expect(detectLocale("en", ["ja-JP"])).toBe("en");
    expect(detectLocale(null, ["fr-FR", "ja-JP", "en"])).toBe("ja");
    expect(detectLocale("invalid", ["EN-us", "ja"])).toBe("en");
    expect(detectLocale(null, ["fr"])).toBe("en");
  });

  it("switches without storage access and sets the document language", async () => {
    vi.spyOn(window.localStorage, "setItem").mockImplementation(() => { throw new Error("disabled"); });
    await setLocale("ja");
    expect(document.documentElement.lang).toBe("ja");
    expect(t("common:actions.cancel")).toBe("キャンセル");
  });

  it("retranslates deferred, nested messages without changing raw design values", async () => {
    const notice = msg("app:cell.existingViews", { name: "M1_日本語", views: joinText([msg("common:view.schematic"), "50p"], " / ") });
    expect(resolveText(notice)).toBe("M1_日本語 already has these views: Schematic / 50p");
    await setLocale("ja");
    expect(resolveText(notice)).toBe("M1_日本語 には既に次のビューがあります: 回路図 / 50p");
    expect(window.localStorage.getItem(LANGUAGE_KEY)).toBe("ja");
  });

  it("uses the locale's plural rules with typed interpolation", async () => {
    expect(t("app:lock.issues", { count: 1 })).toBe("1 lock issue");
    expect(t("app:lock.issues", { count: 2 })).toBe("2 lock issues");
    await setLocale("ja");
    expect(t("app:lock.issues", { count: 1 })).toBe("固定バージョンとの不一致 1 件");
    expect(t("app:lock.issues", { count: 2 })).toBe("固定バージョンとの不一致 2 件");
  });

  it("keeps raw diagnostics from older servers and adds explanations for structured errors", async () => {
    const raw = "device M1: unexpected 50p";
    const error = apiErrorText(new ApiError(409, raw, "busy", {}));
    await setLocale("ja");
    expect(resolveText(error)).toContain("別の操作がこの提案を使用しています");
    expect(resolveText(error)).toContain(raw);
    expect(resolveText(apiErrorText(new Error(raw)))).toBe(raw);
    expect(resolveText(apiErrorText(new ApiError(400, [{ loc: ["value"], msg: raw }])))).toContain(raw);
  });
});

it("preserves an open form's input and DOM identity when language changes", async () => {
  render(<DialogHost />);
  let result!: Promise<Record<string, string> | null>;
  act(() => { result = ask({ title: msg("app:project.new"), fields: [{ name: "name", label: msg("app:name"), required: true }], submitLabel: msg("common:actions.create") }); });
  const input = screen.getByRole("textbox") as HTMLInputElement;
  fireEvent.change(input, { target: { value: "ota_日本語" } });
  await act(async () => { await setLocale("ja"); });
  expect(screen.getByRole("dialog").getAttribute("aria-label")).toBe("新しいプロジェクト");
  expect(screen.getByRole("textbox")).toBe(input);
  expect(input.value).toBe("ota_日本語");
  fireEvent.click(screen.getByRole("button", { name: "作成" }));
  await expect(result).resolves.toEqual({ name: "ota_日本語" });
});

it("updates subscribed text while keeping edits and effect subscriptions alive", async () => {
  const start = vi.fn(), stop = vi.fn();
  function LiveEditor() {
    const { t: translate } = useI18n();
    const [draft, setDraft] = useState("50p");
    useEffect(() => { start(); return stop; }, [translate]);
    return <><input aria-label="design value" value={draft} onChange={(e) => setDraft(e.target.value)} /><span>{translate("common:view.schematic")}</span></>;
  }
  render(<LiveEditor />);
  const input = screen.getByRole("textbox") as HTMLInputElement;
  fireEvent.change(input, { target: { value: "100p" } });
  await act(async () => { await setLocale("ja"); });
  expect(screen.getByText("回路図")).toBeTruthy();
  expect(input.value).toBe("100p");
  expect(start).toHaveBeenCalledTimes(1);
  expect(stop).not.toHaveBeenCalled();
  expect(i18n.language).toBe("ja");
});

// These compile-time checks enforce call-site keys and interpolation names.
function typeContract() {
  // @ts-expect-error unknown keys must fail compilation
  msg("common:not-a-message");
  // @ts-expect-error an interpolated variable must be provided
  t("app:cell.open");
  // @ts-expect-error wrong interpolation names must fail compilation
  msg("app:cell.open", { name: "schematic" });
}
void typeContract;
