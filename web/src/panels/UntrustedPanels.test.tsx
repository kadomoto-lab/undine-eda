// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../api";
import { setLocale, t } from "../i18n";
import { SimPanel } from "./SimPanel";
import { VerifyPanel } from "./VerifyPanel";
import { IntentPanel } from "./IntentPanel";
import { NetlistPanel } from "./NetlistPanel";
import { RtlView } from "../editors/RtlView";
import { TapeoutPanel } from "./TapeoutPanel";
import { UntrustedNotice } from "../UntrustedNotice";

// A launch the trust gate refuses (403 untrusted, docs/design/packages.md Security: trust) shows in the panel that
// launched it, with the refused lines and Trust... (UntrustedNotice, TrustDialog), not as a passing toast.
vi.mock("../App", () => ({ hashParam: () => null }));
vi.mock("../editors/CodeEditor", async () => ({ CodeEditor: (await import("react")).forwardRef(() => null) }));
vi.mock("../api", async (original) => ({ ...await original<typeof import("../api")>(), api: {
  jobs: vi.fn(), job: vi.fn(), startSim: vi.fn(), decks: vi.fn(), startLvs: vi.fn(), startNetgenLvs: vi.fn(),
  intent: vi.fn(), intentEdit: vi.fn(), netlist: vi.fn(), rtl: vi.fn(), rtlLint: vi.fn(), rtlSim: vi.fn(),
  trustList: vi.fn(), trustConfirm: vi.fn(), trust: vi.fn(), untrust: vi.fn(),
  tapeoutTargets: vi.fn(), tapeoutCheck: vi.fn(), signoff: vi.fn(), tapeoutSuite: vi.fn(),
} }));
const mocked = api as unknown as Record<string, ReturnType<typeof vi.fn>>;

const DEP = { name: "analog_ip", package: "analog_ip", key: "analog_ip@1.2.0" };
const refusal = (cell: string, findings: { file: string; kind: string; line: number; text: string }[]) =>
  new ApiError(403, `${cell} (dependency analog_ip 1.2.0, not trusted): ...`, "untrusted", { ...DEP, cell, findings });

const settle = async () => { for (let i = 0; i < 3; i++) await act(async () => {}); };

beforeEach(async () => {
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (k: string) => storage.get(k) ?? null, setItem: (k: string, v: string) => storage.set(k, v) });
  await setLocale("en");
  for (const f of Object.values(mocked)) f.mockReset();
  mocked.jobs.mockResolvedValue([]);
  mocked.decks.mockResolvedValue({ drc: ["d"], lvs: ["l"], extra: [], klayout: true, magic: true, netgen: true });
  mocked.trustList.mockResolvedValue({ trusted: [], dependencies: [{ ...DEP, version: "1.2.0", trusted: false, findings: [] }] });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

/** The notice shows the refused cell and lines; Trust... opens the dialog for that dependency. */
async function expectNotice(cell: string, line: string) {
  const notice = screen.getByTestId("untrusted-notice");
  expect(notice.textContent).toContain(t("app:untrusted.refused", { cell, label: "analog_ip 1.2.0" }));
  expect(notice.textContent).toContain(line);
  fireEvent.click(within(notice).getByRole("button", { name: t("app:untrusted.trust") }));
  await settle();
  expect(screen.getByRole("dialog", { name: t("app:trust.title", { name: "analog_ip" }) })).toBeTruthy();
}

describe("untrusted refusals in the panels", () => {
  it("SimPanel shows a refused run with Trust..., which runs it again once trusted", async () => {
    mocked.startSim.mockRejectedValueOnce(refusal("analog_ip/comp", [{ file: "analog_ip/comp.sch", kind: "control", line: 1, text: ".control" }]));
    const toast = vi.fn();
    render(<SimPanel project="p" lib="l" cell="tb" toast={toast} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: new RegExp(t("verification:sim.run")) }));
    await settle();
    expect(toast).not.toHaveBeenCalled();
    expect(screen.getByText(t("verification:untrusted.sim"))).toBeTruthy();
    await expectNotice("analog_ip/comp", ".control");

    // trusted from the dialog: the panel launches again, and the refusal goes
    mocked.trustConfirm.mockResolvedValue({ token: "tok", expires: 0, key: DEP.key, tree: "t", findings: [] });
    mocked.trust.mockResolvedValue({ key: DEP.key, trusted: true });
    mocked.startSim.mockResolvedValueOnce({ id: "20261112-abcdef", state: "queued", title: "" });
    mocked.job.mockResolvedValue({ id: "20261112-abcdef", state: "queued", title: "" });
    fireEvent.click(screen.getByRole("button", { name: t("app:trust.trust") }));
    await settle();
    expect(mocked.trust).toHaveBeenCalledWith("p", "analog_ip", "tok");
    expect(mocked.startSim).toHaveBeenCalledTimes(2);
    expect(screen.queryByTestId("untrusted-notice")).toBeNull();
  });

  it("SimPanel still toasts any other error", async () => {
    mocked.startSim.mockRejectedValueOnce(new ApiError(400, "no testbench", "bad_request"));
    const toast = vi.fn();
    render(<SimPanel project="p" lib="l" cell="tb" toast={toast} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: new RegExp(t("verification:sim.run")) }));
    await settle();
    expect(toast).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("untrusted-notice")).toBeNull();
  });

  it("VerifyPanel shows an LVS launch refused by the paths rule", async () => {
    mocked.startLvs.mockRejectedValueOnce(refusal("analog_ip/comp", [{ file: "analog_ip/comp.sch", kind: "path", line: 3, text: ".include /etc/passwd" }]));
    const toast = vi.fn();
    render(<VerifyPanel project="p" lib="l" cell="comp" hasSchematic dbu={0.001} onMarkers={() => {}} onFocus={() => {}} toast={toast} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: "LVS" }));
    fireEvent.click(screen.getByRole("button", { name: new RegExp(t("verification:verify.runLvs", { cell: "comp" })) }));
    await settle();
    expect(mocked.startLvs).toHaveBeenCalled();
    expect(toast).not.toHaveBeenCalled();
    expect(screen.getByText(t("verification:untrusted.lvs"))).toBeTruthy();
    await expectNotice("analog_ip/comp", ".include /etc/passwd");
  });

  it("IntentPanel shows a refused spec check", async () => {
    mocked.intent.mockResolvedValue({ intent: { version: 1, notes: "", ports: {}, supplies: {}, views: {}, items: [] }, yaml: "", readonly: false,
      problems: [], requirements: {}, steps: {}, refs: null, suggested_supplies: {}, undo: { undo: false, redo: false } });
    mocked.intentEdit.mockRejectedValueOnce(refusal("analog_ip/ota", [{ file: "analog_ip/ota.sch", kind: "control_line", line: 2, text: "*# shell ls" }]));
    const toast = vi.fn();
    render(<IntentPanel project="p" lib="l" cell="tb" selection={[]} tick={0} toast={toast} />);
    await settle();
    fireEvent.change(screen.getByPlaceholderText(t("intent:textPlaceholder")), { target: { value: "gain above 60 dB" } });
    fireEvent.click(screen.getByRole("button", { name: new RegExp(t("intent:add")) }));
    await settle();
    expect(toast).not.toHaveBeenCalled();
    expect(screen.getByText(t("verification:untrusted.specs"))).toBeTruthy();
    await expectNotice("analog_ip/ota", "*# shell ls");
  });

  it("NetlistPanel shows the Xschem backend's Tcl refusal, and the Undine backend's warning as a warning", async () => {
    const warning = "not trusted: would be refused in a simulation: analog_ip/comp (dependency analog_ip 1.2.0, not trusted): ...";
    mocked.netlist.mockImplementation(async (_p: string, _l: string, _c: string, backend: string) => {
      if (backend === "xschem") throw refusal("analog_ip/comp", [{ file: "analog_ip/comp.sch", kind: "tcl", line: 1, text: "tcleval([exec rm -rf ~])" }]);
      return { backend, text: "* netlist", warnings: [warning], ports: [] };
    });
    render(<NetlistPanel project="p" lib="l" cell="tb" tick={0} toast={vi.fn()} />);
    await settle();
    expect(screen.getByText(warning).className).toContain("warnnote");
    expect(screen.queryByTestId("untrusted-notice")).toBeNull();
    fireEvent.change(screen.getByRole("combobox", { name: t("verification:netlist.netlister") }), { target: { value: "xschem" } });
    await settle();
    expect(screen.getByText(t("verification:untrusted.netlist"))).toBeTruthy();
    expect(document.querySelector(".note.err")).toBeNull();
    await expectNotice("analog_ip/comp", "tcleval([exec rm -rf ~])");
  });

  it("RtlView shows Verilator's refusal for lint and for the simulation", async () => {
    mocked.rtl.mockResolvedValue({ text: "module ctrl; endmodule", file: "ctrl.sv", modules: ["ctrl"], ports: [], undo: { undo: false, redo: false }, readonly: true });
    const findings = [{ file: "analog_ip/rtl/ctrl.sv", kind: "rtl_c", line: 12, text: "$c(\"system(\\\"x\\\")\");" }];
    mocked.rtlLint.mockRejectedValueOnce(refusal("analog_ip/rtl/ctrl.sv", findings));
    mocked.rtlSim.mockRejectedValueOnce(refusal("analog_ip/rtl/ctrl.sv", findings));
    const toast = vi.fn();
    render(<RtlView project="p" lib="analog_ip" cell="ctrl" readonly rtlCells={["ctrl"]} active onDirty={() => {}} onLibraryChanged={() => {}} toast={toast} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: new RegExp(t("rtl:lint.run")) }));
    await settle();
    expect(screen.getByText(t("rtl:untrusted.lint"))).toBeTruthy();
    await expectNotice("analog_ip/rtl/ctrl.sv", "$c(");
    fireEvent.click(screen.getByRole("button", { name: t("common:actions.cancel") }));

    fireEvent.click(screen.getByRole("button", { name: new RegExp(t("rtl:simulate")) }));
    fireEvent.click(screen.getByRole("button", { name: new RegExp(`^${t("rtl:sim.run")}$`) }));
    await settle();
    expect(mocked.rtlSim).toHaveBeenCalledWith("p", "analog_ip", "ctrl");
    expect(screen.getByText(t("rtl:untrusted.sim"))).toBeTruthy();
    await expectNotice("analog_ip/rtl/ctrl.sv", "$c(");
    expect(toast).not.toHaveBeenCalled();
  });
});

describe("untrusted refusals elsewhere", () => {
  it("TapeoutPanel's Run all shows an LVS the trust gate refused at launch under its row", async () => {
    mocked.tapeoutTargets.mockResolvedValue([]);
    mocked.tapeoutCheck.mockResolvedValue({ checks: [] });
    mocked.signoff.mockResolvedValue({ checks: [{ name: "KLayout DRC" }, { name: "KLayout LVS" }] });
    const findings = [{ file: "analog_ip/inv.sch", kind: "path", line: 2, text: ".include /etc/hostname" }];
    mocked.tapeoutSuite.mockResolvedValue({ jobs: [
      { name: "KLayout DRC", job: "j1", kind: "drc", reused: false },
      { name: "KLayout LVS", error: "analog_ip/inv (dependency analog_ip 1.2.0, not trusted): ...", code: "untrusted",
        params: { ...DEP, cell: "analog_ip/inv", findings } }] });
    const toast = vi.fn();
    render(<TapeoutPanel project="p" lib="l" cell="top" version={0} toast={toast} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: t("verification:tapeout.runAll") }));
    await settle();
    expect(mocked.tapeoutSuite).toHaveBeenCalledWith("p", "l", "top", "tt-sky130-analog", true);
    expect(toast).not.toHaveBeenCalled();
    await expectNotice("analog_ip/inv", ".include /etc/hostname");
  });

  it("TapeoutPanel shows another launch error under its row", async () => {
    mocked.tapeoutTargets.mockResolvedValue([]);
    mocked.tapeoutCheck.mockResolvedValue({ checks: [] });
    mocked.signoff.mockResolvedValue({ checks: [{ name: "Netgen LVS" }] });
    mocked.tapeoutSuite.mockResolvedValue({ jobs: [{ name: "Netgen LVS", error: "netgen is not installed" }] });
    render(<TapeoutPanel project="p" lib="l" cell="top" version={0} toast={vi.fn()} />);
    await settle();
    fireEvent.click(screen.getByRole("button", { name: t("verification:tapeout.runAll") }));
    await settle();
    expect(screen.getByTestId("suite-refused").textContent).toBe("netgen is not installed");
  });

  it("in a changeset preview the notice says where trust is given and offers no Trust...", async () => {
    render(<UntrustedNotice project="p~0123456789ab" error={refusal("analog_ip/comp", [])} />);
    await settle();
    const notice = screen.getByTestId("untrusted-notice");
    expect(within(notice).queryByRole("button", { name: t("app:untrusted.trust") })).toBeNull();
    expect(notice.textContent).toContain(t("app:untrusted.inPreview", { name: "analog_ip 1.2.0" }));
    expect(mocked.trustConfirm).not.toHaveBeenCalled();
    expect(mocked.trustList).not.toHaveBeenCalled();
  });
});
