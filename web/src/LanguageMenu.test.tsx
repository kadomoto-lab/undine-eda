// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { i18n, setLocale } from "./i18n";
import { LanguageMenu } from "./LanguageMenu";

beforeEach(async () => { await setLocale("en"); });
afterEach(async () => { cleanup(); await setLocale("en"); });

describe("the language menu", () => {
  it("shows the current language and lists the languages by their own names", () => {
    render(<LanguageMenu />);
    const toggle = screen.getByRole("button", { name: /English/ });
    expect(toggle.textContent).toContain("EN");
    expect(screen.queryByRole("menu")).toBeNull();
    fireEvent.click(toggle);
    const items = screen.getAllByRole("menuitemradio");
    expect(items.map((i) => i.textContent)).toEqual(["English", "日本語Japanese"]);
    expect(items[0].getAttribute("aria-checked")).toBe("true");
    expect(document.activeElement).toBe(items[0]);
  });

  it("switches the language from the menu", async () => {
    render(<LanguageMenu />);
    fireEvent.click(screen.getByRole("button", { name: /English/ }));
    await act(async () => { fireEvent.click(screen.getByRole("menuitemradio", { name: /日本語/ })); });
    expect(i18n.language).toBe("ja");
    expect(screen.queryByRole("menu")).toBeNull();
    expect(screen.getByRole("button", { name: /日本語/ }).textContent).toContain("JA");
  });

  it("moves with the arrow keys and closes on Escape", () => {
    render(<LanguageMenu />);
    const toggle = screen.getByRole("button", { name: /English/ });
    fireEvent.click(toggle);
    const items = screen.getAllByRole("menuitemradio");
    fireEvent.keyDown(items[0], { key: "ArrowDown" });
    expect(document.activeElement).toBe(items[1]);
    fireEvent.keyDown(items[1], { key: "ArrowDown" });
    expect(document.activeElement).toBe(items[0]);
    fireEvent.keyDown(items[0], { key: "Escape" });
    expect(screen.queryByRole("menu")).toBeNull();
    expect(document.activeElement).toBe(toggle);
  });
});
