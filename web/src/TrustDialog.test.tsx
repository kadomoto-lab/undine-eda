// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { api, ApiError, trustFindings } from "./api";
import { setLocale, t } from "./i18n";
import { TrustDialog } from "./TrustDialog";
import { UntrustedNotice } from "./UntrustedNotice";

// The Trust dialog shows the key and what the trust rule refuses, and asks the server for a confirmation token only
// after the person clicks Trust (docs/design/packages.md, Security: trust); taking trust back needs no token.
vi.mock("./api", async (original) => ({ ...await original<typeof import("./api")>(), api: {
  trustList: vi.fn(), trustConfirm: vi.fn(), trust: vi.fn(), untrust: vi.fn(),
} }));
const mocked = api as unknown as Record<"trustList" | "trustConfirm" | "trust" | "untrust", ReturnType<typeof vi.fn>>;

const FINDINGS = [
  { file: "analog_ip/tb_comp.sch", kind: "control", line: 1, text: ".control" },
  { file: "analog_ip/tb_comp.sch", kind: "path", line: 4, text: ".include /etc/passwd" },
  { file: "analog_ip/rtl/ctrl.sv", kind: "rtl_c", line: 12, text: "$c(\"system(\\\"x\\\")\");" },
];
const DEP = { name: "analog_ip", package: "analog_ip", version: "1.2.0", key: "analog_ip@1.2.0", trusted: false, findings: FINDINGS };

beforeEach(async () => {
  const storage = new Map<string, string>();
  vi.stubGlobal("localStorage", { getItem: (key: string) => storage.get(key) ?? null, setItem: (key: string, value: string) => storage.set(key, value) });
  await setLocale("en");
  for (const f of Object.values(mocked)) f.mockReset();
  mocked.trustList.mockResolvedValue({ trusted: [], dependencies: [DEP, { ...DEP, name: "other", key: "other@0.1.0", findings: [] }] });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const settle = async () => { await act(async () => {}); await act(async () => {}); };

it("lists the findings by file, then asks for a token on Trust and trusts with it", async () => {
  mocked.trustConfirm.mockResolvedValue({ token: "tok", expires: 0, key: "analog_ip@1.2.0", tree: "t1", findings: FINDINGS });
  mocked.trust.mockResolvedValue({ key: "analog_ip@1.2.0", trusted: true });
  const onChanged = vi.fn();
  render(<TrustDialog project="p" name="analog_ip" onClose={() => {}} onChanged={onChanged} />);
  await settle();
  expect(mocked.trustList).toHaveBeenCalledWith("p");
  const dialog = screen.getByRole("dialog", { name: t("app:trust.title", { name: "analog_ip" }) });
  expect(dialog.textContent).toContain("analog_ip@1.2.0");
  const list = screen.getByTestId("trust-findings");
  expect(list.textContent).toContain("analog_ip/tb_comp.sch");
  expect(list.textContent).toContain("analog_ip/rtl/ctrl.sv");
  expect(list.textContent).toContain(`${t("app:trust.kind.path")}, ${t("app:trust.line", { line: 4 })}: .include /etc/passwd`);
  expect(list.textContent).toContain(t("app:trust.kind.rtl_c"));
  // the file named once, its findings under it
  expect(list.querySelectorAll("b")).toHaveLength(2);
  expect(mocked.trustConfirm).not.toHaveBeenCalled();

  fireEvent.click(screen.getByRole("button", { name: t("app:trust.trust") }));
  await settle();
  expect(mocked.trustConfirm).toHaveBeenCalledWith("p", "analog_ip");
  expect(mocked.trust).toHaveBeenCalledWith("p", "analog_ip", "tok");
  expect(mocked.trustConfirm.mock.invocationCallOrder[0]).toBeLessThan(mocked.trust.mock.invocationCallOrder[0]);
  expect(onChanged).toHaveBeenCalledWith(true);
  expect(screen.getByText(t("app:trust.trusted", { key: "analog_ip@1.2.0" }))).toBeTruthy();
  // now trusted: Untrust is offered instead
  expect(screen.queryByRole("button", { name: t("app:trust.trust") })).toBeNull();
  expect(screen.getByRole("button", { name: t("app:trust.untrust") })).toBeTruthy();
});

it("shows a 403 refusal of the trust as an error, and stays open", async () => {
  mocked.trustConfirm.mockResolvedValue({ token: "tok", expires: 0, key: "analog_ip@1.2.0", tree: "t1", findings: FINDINGS });
  const refusal = "analog_ip: the installed files changed since the confirmation: open the dialog again";
  mocked.trust.mockRejectedValue(new ApiError(403, refusal, "forbidden"));
  const onChanged = vi.fn();
  render(<TrustDialog project="p" name="analog_ip" onClose={() => {}} onChanged={onChanged} />);
  await settle();
  fireEvent.click(screen.getByRole("button", { name: t("app:trust.trust") }));
  await settle();
  expect(screen.getByRole("dialog").querySelector(".note.err")?.textContent).toContain(refusal);
  expect(onChanged).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: t("app:trust.trust") })).toBeTruthy();
});

it("takes trust back without a token, and asks for nothing when cancelled", async () => {
  mocked.trustList.mockResolvedValue({ trusted: ["analog_ip@1.2.0"], dependencies: [{ ...DEP, trusted: true }] });
  mocked.untrust.mockResolvedValue({ key: "analog_ip@1.2.0", trusted: false });
  const onClose = vi.fn();
  render(<TrustDialog project="p" name="analog_ip" onClose={onClose} />);
  await settle();
  fireEvent.click(screen.getByRole("button", { name: t("app:trust.untrust") }));
  await settle();
  expect(mocked.untrust).toHaveBeenCalledWith("p", "analog_ip");
  expect(mocked.trustConfirm).not.toHaveBeenCalled();
  expect(screen.getByText(t("app:trust.untrusted", { key: "analog_ip@1.2.0" }))).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: t("common:actions.close") }));
  expect(onClose).toHaveBeenCalled();
});

it("says so for a name that is not an installed dependency", async () => {
  render(<TrustDialog project="p" name="gone" onClose={() => {}} />);
  await settle();
  expect(screen.getByText(new RegExp(t("app:trust.missing", { name: "gone" })))).toBeTruthy();
  expect(screen.queryByRole("button", { name: t("app:trust.trust") })).toBeNull();
});

it("renders an untrusted refusal with its findings and a Trust... button that opens the dialog", async () => {
  const error = new ApiError(403, "analog_ip/tb_comp (dependency analog_ip 1.2.0, not trusted): ...", "untrusted",
    { name: "analog_ip", package: "analog_ip", key: "analog_ip@1.2.0", cell: "analog_ip/tb_comp", findings: FINDINGS.slice(0, 2) });
  render(<UntrustedNotice project="p" error={error} />);
  const notice = screen.getByTestId("untrusted-notice");
  expect(notice.textContent).toContain(t("app:untrusted.refused", { cell: "analog_ip/tb_comp", label: "analog_ip 1.2.0" }));
  expect(notice.textContent).toContain(".include /etc/passwd");
  fireEvent.click(screen.getByRole("button", { name: t("app:untrusted.trust") }));
  await settle();
  expect(screen.getByRole("dialog", { name: t("app:trust.title", { name: "analog_ip" }) })).toBeTruthy();
});

it("renders nothing for any other error", () => {
  const { container } = render(<UntrustedNotice project="p" error={new ApiError(403, "forbidden", "forbidden")} />);
  expect(container.innerHTML).toBe("");
});

it("reads findings from a list or from a scan_dir listing", () => {
  expect(trustFindings(FINDINGS)).toEqual(FINDINGS);
  expect(trustFindings({ cells: { tb_comp: [{ kind: "control", line: 1, text: ".control", path: null, where: "tb_comp.sch code s1 value" }] },
    rtl: { "rtl/ctrl.sv": [{ kind: "rtl_c", line: 12, text: "$c(x)" }] }, tcl: {} })).toEqual([
    { file: "tb_comp.sch code s1 value", kind: "control", line: 1, text: ".control" },
    { file: "rtl/ctrl.sv", kind: "rtl_c", line: 12, text: "$c(x)" },
  ]);
  expect(trustFindings(null)).toEqual([]);
});
