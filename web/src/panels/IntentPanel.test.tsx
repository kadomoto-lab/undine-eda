// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type IntentView } from "../api";
import { setLocale, t } from "../i18n";
import { IntentPanel } from "./IntentPanel";

vi.mock("../api", async (original) => ({ ...await original<typeof import("../api")>(), api: { intent: vi.fn(), intentEdit: vi.fn() } }));

const view: IntentView = {
  intent: {
    version: 1, notes: "", ports: {}, supplies: {}, views: {},
    items: [{ id: "s1", kind: "spec", text: "DAC controls clear after clk", spec: { measure: "tclr", max: 2e-9, testbench: "tb" },
      source: { by: "agent:claude-code/logic", request: "clear them on clk", requests: [
        { request: "yes, within 2 ns at every corner", date: "2026-11-02", by: "agent:claude-code/logic" },
        { request: "also at -40 C", date: "2026-11-03", by: "alice" }] } }],
  },
  yaml: "", readonly: false, problems: [], requirements: {}, steps: {}, refs: null, suggested_supplies: {}, undo: { undo: false, redo: false },
};

beforeEach(async () => {
  vi.resetAllMocks();
  vi.mocked(api.intent).mockResolvedValue(view);
  await setLocale("en");
});
afterEach(cleanup);

describe("an intent item's requests", () => {
  it("shows the words it came from and every later confirmation, dated, in order, in each language", async () => {
    render(<IntentPanel project="p" lib="l" cell="dac" selection={[]} tick={0} toast={vi.fn()} />);
    await act(async () => {});
    expect(screen.getByText(/from “clear them on clk”/)).toBeTruthy();
    const later = [t("intent:then", { date: "2026-11-02", request: "yes, within 2 ns at every corner", actor: "agent:claude-code/logic" }),
      t("intent:then", { date: "2026-11-03", request: "also at -40 C", actor: "alice" })];
    expect(later[0]).toBe("then, 2026-11-02: “yes, within 2 ns at every corner” (agent:claude-code/logic)");
    const shown = later.map((s) => screen.getByText(s));
    expect(shown[0].compareDocumentPosition(shown[1]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    await act(async () => { await setLocale("ja"); });
    expect(screen.getByText("その後 2026-11-03:「also at -40 C」(alice)")).toBeTruthy();
  });
});
