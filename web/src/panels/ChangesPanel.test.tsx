// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, type ChangesetInfo, type GitDiff } from "../api";
import { DialogHost } from "../dialog";
import { msg, setLocale, t } from "../i18n";
import { ChangesPanel } from "./ChangesPanel";

// A changeset on git (docs/design/changeset-branches.md): the review names its branch, shows its files from the base
// to the working copy, says what adopting did to the user's branch, shows a merge conflict file by file (nothing
// written), and offers only review and discard for one made before changesets were branches.
vi.mock("../App", () => ({ hashParam: () => null }));
vi.mock("../layout", () => ({ Splitter: () => null }));
vi.mock("../api", async (original) => ({ ...await original<typeof import("../api")>(), api: {
  changesets: vi.fn(), changeset: vi.fn(), openChangeset: vi.fn(), changesetAction: vi.fn(), changesetDiff: vi.fn(),
  timeline: vi.fn(), writerStop: vi.fn(), evidenceSizes: vi.fn(), pruneEvidence: vi.fn(),
} }));

const git = { branch: "undine/p/cs1", base: "b".repeat(40), head: "a".repeat(40), prefix: "", tip: "c".repeat(40), ref: null, commits: 3 };
const op = { seq: 1, op: "set_instance_params", cell: "amp", view: "schematic", actor: "agent:claude", reason: "more gain", t: "2026-10-28T10:00:00Z",
  effects: [{ k: "i" as const, do: "mod" as const, id: "M1", changes: { W: ["1", "2"] as [unknown, unknown] } }] };
const cs: ChangesetInfo = { id: "cs1", title: "Widen the input pair", reason: "more gain", actor: "agent:claude", created: "2026-10-28T10:00:00Z",
  status: "open", preview: "p~cs1", changes: [{ lib: "l", ops: [op] }], verify: [], writer: null, git, activity: { head: 1 } };
const diff: GitDiff = { base: git.base, rev: null, other: [{ path: "history/l.jsonl", status: "modified" }], cells: [{ lib: "l", cell: "amp", views: [
  { view: "schematic", path: "libs/l/amp.sch", status: "modified", diff: { kind: "schematic", changes: [
    { do: "mod", id: "inst:M1:prop:W", what: "instance M1: W", before: "1", after: "2" }] } },
  { view: "layout", path: "libs/l/amp.oas", status: "added", diff: { kind: "layout", changes: [], geometry: [] } },
] }] };

type Props = ComponentProps<typeof ChangesPanel>;
const props = (): Props => ({ project: "p", initialId: "cs1", onClose: vi.fn(), onPreview: vi.fn(), onChanged: vi.fn(), onOpenView: vi.fn(),
  onGit: vi.fn(), onHistory: vi.fn(), toast: vi.fn() });
async function open(record: ChangesetInfo, extra: Partial<Props> = {}) {
  vi.mocked(api.changeset).mockResolvedValue(record);
  const p = { ...props(), ...extra };
  render(<><DialogHost /><ChangesPanel {...p} /></>);
  await act(async () => {});
  return p;
}
const confirm = async (label: string) => {
  await act(async () => {});
  fireEvent.click(within(document.querySelector(".modal") as HTMLElement).getByRole("button", { name: label }));
  await act(async () => {});
};

beforeEach(async () => {
  vi.resetAllMocks();
  vi.mocked(api.changesets).mockResolvedValue([]);
  vi.mocked(api.evidenceSizes).mockResolvedValue({ bytes: 0, raw: 0, changesets: {} });
  vi.mocked(api.changesetDiff).mockResolvedValue(diff);
  vi.mocked(api.timeline).mockResolvedValue({ ops: [], turns: {}, checkpoints: [] });
  await setLocale("en");
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("a changeset on git", () => {
  it("names its branch, the base it started from and its commits", async () => {
    await open(cs);
    const row = document.querySelector(".csgit") as HTMLElement;
    expect(within(row).getByText("undine/p/cs1")).toBeTruthy();
    expect(within(row).getByText(t("review:git.base", { base: "bbbbbbb" })).getAttribute("title")).toContain(git.base);
    expect(within(row).getByText(t("review:git.commits", { count: 3 }))).toBeTruthy();
    expect(screen.queryByText(t("review:legacy.open"))).toBeNull();
  });

  it("shows its files from the base to the working copy with the History panel's diff, and opens them in the working copy", async () => {
    const p = await open(cs);
    expect(api.changesetDiff).not.toHaveBeenCalled();                // read when the tab is chosen
    fireEvent.click(screen.getByRole("button", { name: t("review:listing.files") }));
    await act(async () => {});
    expect(api.changesetDiff).toHaveBeenCalledWith("p", "cs1");
    expect(screen.getByText(t("review:files.open", { base: "bbbbbbb" }))).toBeTruthy();
    expect(screen.getByText("instance M1: W")).toBeTruthy();
    expect(screen.getByText("1", { selector: ".before" })).toBeTruthy();
    expect(screen.getByText("2", { selector: ".after" })).toBeTruthy();
    expect(screen.getByText(t("history:otherFiles"), { exact: false })).toBeTruthy();   // what no cell holds
    fireEvent.click(screen.getByTitle(t("history:open", { view: t("history:view.layout") })));
    expect(p.onOpenView).toHaveBeenCalledWith("p~cs1", "l", "amp", "layout");
    // read again by hand: a file can change in the working copy with no operation recorded
    fireEvent.click(screen.getByTitle(t("review:files.refresh")));
    await act(async () => {});
    expect(api.changesetDiff).toHaveBeenCalledTimes(2);
  });

  it("reads its files again when its working copy moves on, not on every poll", async () => {
    vi.useFakeTimers();
    await open(cs);
    fireEvent.click(screen.getByRole("button", { name: t("review:listing.files") }));
    await act(async () => {});
    expect(api.changesetDiff).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });     // the record polled, nothing new
    expect(api.changeset).toHaveBeenCalledTimes(2);
    expect(api.changesetDiff).toHaveBeenCalledTimes(1);
    vi.mocked(api.changeset).mockResolvedValue({ ...cs, activity: { head: 2 } });
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(api.changesetDiff).toHaveBeenCalledTimes(2);
  });

  it("shows an adopted one's files up to the tip it was closed with, opened in the design; a discarded one's opened nowhere", async () => {
    const closed = { ...git, branch: "undine/p/cs1", ref: "refs/undine/changesets/p/cs1" };
    const applied: ChangesetInfo = { ...cs, status: "applied", git: closed, applied: { t: "2026-10-28T11:00:00Z", by: "user", commit: "d".repeat(40), uncommitted: false } };
    const p = await open(applied);
    expect(within(document.querySelector(".csgit") as HTMLElement).getByText("refs/undine/changesets/p/cs1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: t("review:listing.files") }));
    await act(async () => {});
    expect(screen.getByText(t("review:files.closed", { base: "bbbbbbb", tip: "ccccccc" }))).toBeTruthy();
    expect(screen.queryByRole("button", { name: t("review:timeline") })).toBeNull();
    fireEvent.click(screen.getByTitle(t("history:open", { view: t("history:view.schematic") })));
    expect(p.onOpenView).toHaveBeenCalledWith("p", "l", "amp", "schematic");
    cleanup();
    await open({ ...applied, status: "discarded", applied: null });
    fireEvent.click(screen.getByRole("button", { name: t("review:listing.files") }));
    await act(async () => {});
    expect(screen.getByText("instance M1: W")).toBeTruthy();
    expect(screen.queryByTitle(t("history:open", { view: t("history:view.schematic") }))).toBeNull();
  });
});

describe("what adopting merges", () => {
  // nothing recorded, nothing committed yet (a library made in the preview, a layout imported, a file edited outside
  // Undine): by file shows it, and adopting merges it
  const quiet: ChangesetInfo = { ...cs, changes: [], git: { ...git, tip: null, commits: 0 },
    verify: [{ side: "work", kind: "drc", lib: "l", cell: "amp", state: "not_run" }] };
  const projectOnly: GitDiff = { base: git.base, rev: null, cells: [], other: [{ path: "project.json", status: "modified" }] };
  const button = (name: string) => screen.getByRole("button", { name }) as HTMLButtonElement;

  it("can be adopted, and its checks run, when only its files changed", async () => {
    vi.mocked(api.changesetDiff).mockResolvedValue(projectOnly);
    await open(quiet);
    await act(async () => {});
    expect(api.changesetDiff).toHaveBeenCalledWith("p", "cs1");         // read without the tab chosen: only the files can say
    expect(screen.getByText(t("review:noChanges"))).toBeTruthy();       // by object: no operation recorded
    expect(button(t("review:adopt")).disabled).toBe(false);
    expect(button(t("review:checks.run")).disabled).toBe(false);
  });

  it("cannot when its files show nothing but its log, and reads them again with each poll until they show a change", async () => {
    vi.useFakeTimers();
    vi.mocked(api.changesetDiff).mockResolvedValue({ ...projectOnly, other: [{ path: "history/l.jsonl", status: "modified" }] });
    await open(quiet);
    await act(async () => {});
    expect(button(t("review:adopt")).disabled).toBe(true);
    expect(button(t("review:checks.run")).disabled).toBe(true);
    vi.mocked(api.changesetDiff).mockResolvedValue(diff);
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(api.changesetDiff).toHaveBeenCalledTimes(2);
    expect(button(t("review:adopt")).disabled).toBe(false);
  });

  // a large layout imported into the working copy: its diff takes longer than a poll, which must not cancel it
  const slowDiff = (ms: number) => vi.mocked(api.changesetDiff).mockImplementation(() => new Promise((ok) => setTimeout(() => ok(diff), ms)));
  const inFlight = () => {
    let now = 0, most = 0;
    const start = vi.mocked(api.changesetDiff).getMockImplementation()!;
    vi.mocked(api.changesetDiff).mockImplementation((...a) => {
      most = Math.max(most, ++now);
      return start(...a).finally(() => { now--; });
    });
    return () => most;
  };
  // time passing a second at a time, each its own act: the screen renders between the polls, as in a browser
  const pass = async (ms: number) => {
    for (let t = 0; t < ms; t += 1000) await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
  };

  it("keeps a slow read of its files when the record is polled meanwhile, and stops reading once they show a change", async () => {
    vi.useFakeTimers();
    vi.mocked(api.changeset).mockImplementation(async () => ({ ...quiet }));   // a new record each poll
    slowDiff(8000);
    const most = inFlight();
    render(<><DialogHost /><ChangesPanel {...props()} /></>);
    await act(async () => {});
    expect(button(t("review:adopt")).disabled).toBe(true);
    await pass(60000);
    expect(vi.mocked(api.changeset).mock.calls.length).toBeGreaterThan(10);
    expect(button(t("review:adopt")).disabled).toBe(false);
    expect(most()).toBe(1);                                              // one read at a time
    expect(api.changesetDiff).toHaveBeenCalledTimes(1);                 // a change shown: no read with each poll
    // the working copy moving on is read again, the same way
    vi.mocked(api.changeset).mockImplementation(async () => ({ ...quiet, activity: { head: 2 } }));
    await pass(30000);
    expect(api.changesetDiff).toHaveBeenCalledTimes(2);
    expect(most()).toBe(1);
  });

  it("shows a slow read by file once it lands, not loading for ever", async () => {
    vi.useFakeTimers();
    vi.mocked(api.changeset).mockImplementation(async () => ({ ...quiet }));
    slowDiff(8000);
    render(<><DialogHost /><ChangesPanel {...props()} /></>);
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: t("review:listing.files") }));
    await pass(4000);
    expect(screen.getByText(t("common:state.loading"))).toBeTruthy();
    await pass(20000);
    expect(screen.getByText("instance M1: W")).toBeTruthy();
    expect(screen.queryByText(t("common:state.loading"))).toBeNull();
    expect(api.changesetDiff).toHaveBeenCalledTimes(1);
  });

  it("reads its files again by hand after the read under way, one read at a time", async () => {
    vi.useFakeTimers();
    vi.mocked(api.changeset).mockImplementation(async () => ({ ...quiet }));
    slowDiff(8000);
    const most = inFlight();
    render(<><DialogHost /><ChangesPanel {...props()} /></>);
    await act(async () => {});
    fireEvent.click(screen.getByRole("button", { name: t("review:listing.files") }));
    await act(async () => {});
    fireEvent.click(screen.getByTitle(t("review:files.refresh")));    // while the first read is under way
    await pass(9000);
    expect(api.changesetDiff).toHaveBeenCalledTimes(2);               // it follows the first
    await pass(9000);
    expect(api.changesetDiff).toHaveBeenCalledTimes(2);
    expect(most()).toBe(1);
    expect(screen.getByText("instance M1: W")).toBeTruthy();
  });

  it("can be adopted when its branch has commits, without reading its files", async () => {
    await open({ ...quiet, git: { ...git, tip: null, commits: 1 } });
    expect(button(t("review:adopt")).disabled).toBe(false);
    expect(api.changesetDiff).not.toHaveBeenCalled();
  });
});

describe("adopting and reverting", () => {
  it("says which merge commit adopting made, and reads the design again", async () => {
    const applied = { ...cs, status: "applied" as const, applied: { t: "2026-10-28T11:00:00Z", by: "user", commit: "d".repeat(40), uncommitted: false, paths: ["libs/l/amp.sch"] } };
    const onSettled = vi.fn();
    const p = await open(cs, { review: { side: "proposal", onSide: vi.fn(), onFocus: vi.fn(), focused: null, onSettled } });
    vi.mocked(api.changesetAction).mockResolvedValue(applied);
    vi.mocked(api.changeset).mockResolvedValue(applied);
    fireEvent.click(screen.getByRole("button", { name: t("review:adopt") }));
    await confirm(t("review:adopt"));
    expect(api.changesetAction).toHaveBeenCalledWith("p", "cs1", "apply");
    expect(p.toast).toHaveBeenCalledWith(msg("review:adopted.commit", { commit: "ddddddd" }));
    expect(p.onChanged).toHaveBeenCalledWith(applied.applied, "apply");   // the files it wrote: the views showing them read them
    expect(onSettled).toHaveBeenCalled();
    expect(screen.getByText(t("review:merge.commit", { commit: "ddddddd" }))).toBeTruthy();
  });

  it("says when the merge was left uncommitted, and leads to History to commit it", async () => {
    const applied = { ...cs, status: "applied" as const, applied: { t: "2026-10-28T11:00:00Z", by: "user", commit: null, uncommitted: true, paths: [] } };
    const p = await open(cs);
    vi.mocked(api.changesetAction).mockResolvedValue(applied);
    vi.mocked(api.changeset).mockResolvedValue(applied);
    fireEvent.click(screen.getByRole("button", { name: t("review:adopt") }));
    await confirm(t("review:adopt"));
    expect(p.toast).toHaveBeenCalledWith(msg("review:adopted.uncommitted"));
    expect(screen.getByText(t("review:merge.uncommitted"))).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: t("review:merge.history") }));
    expect(p.onHistory).toHaveBeenCalled();
  });

  it("shows a conflicting apply file by file with each side's value, and that nothing was written", async () => {
    const p = await open(cs);
    vi.mocked(api.changesetAction).mockRejectedValue(new ApiError(409, "the merge conflicts in 2 files; nothing was written", "merge_conflict", {}, [
      { path: "libs/l/amp.sch", kind: "schematic", conflicts: [{ id: "inst:M1:prop:W", what: "instance M1: W", base: "1", ours: "3", theirs: "2" }] },
      { path: "libs/l/notes.txt", kind: null, conflicts: null },
    ]));
    fireEvent.click(screen.getByRole("button", { name: t("review:adopt") }));
    await confirm(t("review:adopt"));
    const note = document.querySelector(".csconflict") as HTMLElement;
    expect(within(note).getByText(t("review:conflict.apply", { count: 2 }))).toBeTruthy();
    expect(within(note).getByText(t("review:conflict.nothing"))).toBeTruthy();
    expect(within(note).getByText("libs/l/amp.sch")).toBeTruthy();
    expect(within(note).getByText("instance M1: W")).toBeTruthy();
    // the sides as the person deciding knows them: the design now, the proposal, where it started
    for (const [side, value] of [["now", "3"], ["proposal", "2"], ["opened", "1"]] as const) {
      expect(within(note).getByText(t(`review:conflict.side.${side}`)).parentElement?.textContent).toContain(value);
    }
    expect(within(note).getByText(t("review:conflict.file"))).toBeTruthy();
    expect(within(note).getByText(t("review:conflict.nextApply", { branch: "undine/p/cs1" }))).toBeTruthy();
    expect(p.toast).toHaveBeenCalledWith(msg("review:conflict.toastApply"), "error");
    expect(p.onChanged).not.toHaveBeenCalled();
    fireEvent.click(within(note).getByTitle(t("review:conflict.hide")));
    expect(document.querySelector(".csconflict")).toBeNull();
  });

  it("words the conflicts of whole files and across files in the reader's language, from their ids", async () => {
    await setLocale("ja");
    await open(cs);
    const what = {
      lines: "1 place changed differently on both sides (merged by lines)",
      file: "deleted on theirs, changed on the other side",
      error: "cannot merge: UnicodeDecodeError: bad byte",
      member: "l/amp (schematic) changed in the project since the changeset was opened, and it is part of the changeset's symbol " +
        "transaction of l/amp (symbol): undo those edits before applying it",
      incomplete: "the changeset's symbol transaction of l/amp (symbol) is incomplete (its records do not cover the views it changed)",
      consumer: "new dependent wiring in l/top is not part of the changeset's symbol transaction of l/amp (symbol): update or undo " +
        "those later instances before applying it",
      journal: "2 unsaved layout edits of the changeset are not in its commit: save its layout in the working copy, then apply it",
    };
    vi.mocked(api.changesetAction).mockRejectedValue(new ApiError(409, "conflicts", "merge_conflict", {}, [
      { path: "notes.txt", kind: "text", conflicts: [{ id: "lines", what: what.lines, base: "file", ours: "changed", theirs: "changed" }] },
      { path: "libs/l/inv.sch", kind: "schematic", conflicts: [{ id: "file", what: what.file, base: "file", ours: "changed", theirs: "deleted" }] },
      { path: "libs/l/blob.bin", kind: "text", conflicts: [{ id: "error", what: what.error, base: "file", ours: "changed", theirs: "changed" }] },
      { path: "libs/l/amp.sch", kind: "schematic", conflicts: [
        { id: "symbol:g1", what: what.member, base: "file", ours: "changed", theirs: "changed" },
        { id: "symbol:g2", what: what.incomplete, base: null, ours: null, theirs: null }] },
      { path: "libs/l/top.sch", kind: "schematic", conflicts: [{ id: "symbol-consumer:l/top", what: what.consumer, base: "file", ours: "changed", theirs: null }] },
      { path: "history/l.jsonl", kind: "log", conflicts: [{ id: "layout-journal", what: what.journal, base: null, ours: null, theirs: ["seq:4", "seq:5"] }] },
    ]));
    fireEvent.click(screen.getByRole("button", { name: t("review:adopt") }));
    await confirm(t("review:adopt"));
    const note = document.querySelector(".csconflict") as HTMLElement;
    for (const text of [t("review:conflict.what.lines", { count: 1 }), t("review:conflict.what.file"),
      t("review:conflict.what.error", { error: "UnicodeDecodeError: bad byte" }),
      t("review:conflict.what.symbolMember", { cell: "l/amp", view: t("review:view.schematic"), source: "l/amp" }),
      t("review:conflict.what.symbolIncomplete", { source: "l/amp" }),
      t("review:conflict.what.consumer", { cell: "l/top", source: "l/amp" }), t("review:conflict.what.journal", { count: 2 })]) {
      expect(within(note).getByText(text)).toBeTruthy();
    }
    for (const english of Object.values(what)) expect(note.textContent).not.toContain(english);
    expect(t("review:conflict.what.file")).toContain("削除");
    // each side's state in words: the design now changed, the proposal deleted, the base had the file
    const row = within(note).getByText(t("review:conflict.what.file")).closest(".gitconflict") as HTMLElement;
    for (const [side, state] of [["now", "changed"], ["proposal", "deleted"], ["opened", "file"]] as const) {
      expect(within(row).getByText(t(`review:conflict.side.${side}`)).parentElement?.textContent).toContain(t(`review:conflict.state.${state}`));
    }
    expect(row.textContent).not.toMatch(/changed|deleted/);
    // the ids of whole files and of the merge are no object of the design: not shown (an object's id is)
    const ids = [...note.querySelectorAll(".gitconflict > .mono")].map((e) => e.textContent);
    expect(ids).toEqual(["symbol:g1", "symbol:g2", "symbol-consumer:l/top"]);
  });

  // the 409 of a real server (yamlmerge._unreadable): the project's intent file is no YAML; nothing was deleted
  const unreadable = "the intent file cannot be read (not valid YAML: while parsing a flow sequence\n  in \"<unicode string>\", line 1, " +
    "column 8:\n    items: [unclosed\n           ^\nexpected ',' or ']', but got '<stream end>'\n  in \"<unicode string>\", line 2, column 1:\n    \n    ^)";
  const apply409 = { detail: `cannot apply cs-5903c2: conflict in libs/l/top.intent.yaml: ${unreadable}; nothing was written`, code: "merge_conflict",
    params: {}, files: [{ path: "libs/l/top.intent.yaml", kind: "intent", conflicts: [
      { id: "file", what: unreadable, base: null, ours: null, theirs: null, taken: "ours" as const, resolved: false }] }] };

  for (const locale of ["en", "ja"] as const) {
    it(`words an unreadable file as unreadable, with its reason, not as deleted on one side (${locale})`, async () => {
      await setLocale(locale);
      await open(cs);
      vi.mocked(api.changesetAction).mockRejectedValue(new ApiError(409, apply409.detail, apply409.code, apply409.params, apply409.files));
      fireEvent.click(screen.getByRole("button", { name: t("review:adopt") }));
      await confirm(t("review:adopt"));
      const note = document.querySelector(".csconflict") as HTMLElement;
      const reason = unreadable.slice("the intent file cannot be read (".length, -1);
      const words = t("review:conflict.what.unreadable", { kind: t("review:view.intent"), error: reason });
      expect(note.querySelector(".gitconflict .grow")?.textContent).toBe(words);
      expect(note.textContent).toContain("not valid YAML: while parsing a flow sequence");
      expect(note.textContent).not.toContain(t("review:conflict.what.file"));
      // no sides to show (the file has none that could be read), and no id ("file")
      expect(note.querySelector(".gitconflict .gitsides")).toBeNull();
      expect(note.querySelector(".gitconflict > .mono")).toBeNull();
      if (locale === "ja") expect(words).toContain("設計意図");
    });
  }

  it("words a path that is no file in the design, and a changed-meanwhile file, in the reader's language", async () => {
    await setLocale("ja");
    await open(cs);
    vi.mocked(api.changesetAction).mockRejectedValue(new ApiError(409, "conflicts", "merge_conflict", {}, [
      { path: "libs/l/amp.sch", kind: "schematic", conflicts: [{ id: "file", what: "not a file in the project", base: "file", ours: "deleted", theirs: "changed" }] },
      { path: "libs/l/top.sch", kind: "schematic", conflicts: [{ id: "changed", base: null, ours: "changed", theirs: null,
        what: "libs/l/top.sch changed in the project while the merge was being made: apply the changeset again to merge it with that edit" }] },
    ]));
    fireEvent.click(screen.getByRole("button", { name: t("review:adopt") }));
    await confirm(t("review:adopt"));
    const note = document.querySelector(".csconflict") as HTMLElement;
    const row = within(note).getByText(t("review:conflict.what.notFile")).closest(".gitconflict") as HTMLElement;
    expect(within(row).getByText(t("review:conflict.side.now")).parentElement?.textContent).toContain(t("review:conflict.state.notFile"));
    expect(within(row).getByText(t("review:conflict.side.proposal")).parentElement?.textContent).toContain(t("review:conflict.state.changed"));
    expect(row.textContent).not.toContain(t("review:conflict.state.deleted"));     // nothing was deleted
    expect(note.textContent).not.toContain(t("review:conflict.what.file"));
    expect(within(note).getByText(t("review:conflict.what.changedMeanwhile"))).toBeTruthy();
    // one conflict that is not "changed": the next step is the one of a conflict
    expect(within(note).getByText(t("review:conflict.nextApply", { branch: "undine/p/cs1" }))).toBeTruthy();
  });

  it("says a file changed while it was being adopted, in the reader's language, and that adopting again is the next step", async () => {
    await setLocale("ja");
    const p = await open(cs);
    const what = "libs/l/top.sch changed in the project while the merge was being made: apply the changeset again to merge it with that edit";
    vi.mocked(api.changesetAction).mockRejectedValue(new ApiError(409,
      "cannot apply cs1: libs/l/top.sch changed in the project while the merge was being made; nothing was written: apply it again",
      "merge_conflict", {}, [{ path: "libs/l/top.sch", kind: "schematic", conflicts: [
        { id: "changed", what, base: null, ours: "changed", theirs: null, taken: "ours", resolved: false }] }]));
    fireEvent.click(screen.getByRole("button", { name: t("review:adopt") }));
    await confirm(t("review:adopt"));
    const note = document.querySelector(".csconflict") as HTMLElement;
    expect(within(note).getByText(t("review:conflict.applyChanged", { count: 1 }))).toBeTruthy();
    expect(within(note).getByText(t("review:conflict.what.changedMeanwhile"))).toBeTruthy();
    expect(note.textContent).not.toContain(what);
    const sides = note.querySelector(".gitsides") as HTMLElement;
    expect(within(sides).getByText(t("review:conflict.side.now")).parentElement?.textContent).toContain(t("review:conflict.state.changed"));
    expect(sides.textContent).not.toMatch(/changed/);                  // the state in words, not the server's
    expect(within(note).getByText(t("review:conflict.againApply"))).toBeTruthy();
    expect(within(note).queryByText(t("review:conflict.nextApply", { branch: "undine/p/cs1" }))).toBeNull();
    expect(p.toast).toHaveBeenCalledWith(msg("review:conflict.toastApplyChanged"), "error");
    expect(t("review:conflict.againApply")).toContain("採用");
  });

  it("says a file changed while it was being reverted, with reverting again as the next step", async () => {
    const p = await open({ ...cs, status: "applied", git: { ...git, ref: "refs/undine/changesets/p/cs1" },
      applied: { t: "2026-10-28T11:00:00Z", by: "user", commit: "d".repeat(40), uncommitted: false } });
    vi.mocked(api.changesetAction).mockRejectedValue(new ApiError(409, "cannot revert cs1", "merge_conflict", {}, [
      { path: "libs/l/top.sch", kind: "schematic", conflicts: [{ id: "changed", base: null, ours: "changed", theirs: null,
        what: "libs/l/top.sch changed in the project while the merge was being made: revert the changeset again to merge it with that edit" }] },
      { path: "history/l.jsonl", kind: "log", conflicts: [{ id: "changed", base: null, ours: "changed", theirs: null,
        what: "history/l.jsonl changed in the project while the merge was being made: revert the changeset again to merge it with that edit" }] },
    ]));
    fireEvent.click(screen.getByRole("button", { name: t("review:revert") }));
    await confirm(t("review:revert"));
    const note = document.querySelector(".csconflict") as HTMLElement;
    expect(within(note).getByText(t("review:conflict.revertChanged", { count: 2 }))).toBeTruthy();
    expect(within(note).getAllByText(t("review:conflict.what.changedMeanwhileRevert"))).toHaveLength(2);
    expect(within(note).getByText(t("review:conflict.againRevert"))).toBeTruthy();
    expect(within(note).queryByText(t("review:conflict.nextRevert"))).toBeNull();
    expect(p.toast).toHaveBeenCalledWith(msg("review:conflict.toastRevertChanged"), "error");
  });

  it("words a symbol transaction's conflicts of a revert as a revert's", async () => {
    await open({ ...cs, status: "applied", git: { ...git, ref: "refs/undine/changesets/p/cs1" },
      applied: { t: "2026-10-28T11:00:00Z", by: "user", commit: "d".repeat(40), uncommitted: false } });
    vi.mocked(api.changesetAction).mockRejectedValue(new ApiError(409, "conflicts", "merge_conflict", {}, [
      { path: "libs/l/top.sch", kind: "schematic", conflicts: [{ id: "symbol-consumer:l/top", base: "file", ours: "changed", theirs: null,
        what: "new dependent wiring in l/top is not part of the changeset's symbol transaction of l/amp (symbol): update or undo those later instances before reverting it" }] },
      { path: "libs/l/mix.sch", kind: "schematic", conflicts: [{ id: "symbol-consumer:l/mix", base: "file", ours: "changed", theirs: null,
        what: "pin B of l/amp is gone" }] },
    ]));
    fireEvent.click(screen.getByRole("button", { name: t("review:revert") }));
    await confirm(t("review:revert"));
    const note = document.querySelector(".csconflict") as HTMLElement;
    expect(within(note).getByText(t("review:conflict.what.consumerRevert", { cell: "l/top", source: "l/amp" }))).toBeTruthy();
    expect(within(note).getByText(t("review:conflict.what.consumerWhy", { cell: "l/mix", why: "pin B of l/amp is gone" }))).toBeTruthy();
  });

  it("reverts one adopted on git, with its commit, and shows a conflicting revert with the revert's sides", async () => {
    const applied: ChangesetInfo = { ...cs, status: "applied", git: { ...git, ref: "refs/undine/changesets/p/cs1" },
      applied: { t: "2026-10-28T11:00:00Z", by: "user", commit: "d".repeat(40), uncommitted: false } };
    const p = await open(applied);
    vi.mocked(api.changesetAction).mockRejectedValueOnce(new ApiError(409, "conflicts", "merge_conflict", {}, [
      { path: "libs/l/amp.sch", kind: "schematic", conflicts: [{ id: "inst:M1:prop:W", what: "instance M1: W", base: "2", ours: "5", theirs: "1" }] }]));
    fireEvent.click(screen.getByRole("button", { name: t("review:revert") }));
    await confirm(t("review:revert"));
    const note = document.querySelector(".csconflict") as HTMLElement;
    expect(within(note).getByText(t("review:conflict.revert", { count: 1 }))).toBeTruthy();
    expect(within(note).getByText(t("review:conflict.side.before")).parentElement?.textContent).toContain("1");
    expect(within(note).getByText(t("review:conflict.side.adopted")).parentElement?.textContent).toContain("2");
    expect(within(note).getByText(t("review:conflict.nextRevert"))).toBeTruthy();
    const reverted = { ...applied, status: "reverted" as const, reverted: { t: "2026-10-28T12:00:00Z", by: "user", commit: "e".repeat(40), uncommitted: false } };
    vi.mocked(api.changesetAction).mockResolvedValue(reverted);
    vi.mocked(api.changeset).mockResolvedValue(reverted);
    fireEvent.click(screen.getByRole("button", { name: t("review:revert") }));
    await confirm(t("review:revert"));
    expect(p.toast).toHaveBeenCalledWith(msg("review:reverted.commit", { commit: "eeeeeee" }));
    expect(p.onChanged).toHaveBeenCalledTimes(1);
    expect(p.onChanged).toHaveBeenCalledWith(reverted.reverted, "revert");
    expect(document.querySelector(".csconflict")).toBeNull();          // the last attempt's conflicts are gone
    expect(screen.getByText(t("review:merge.revertCommit", { commit: "eeeeeee" }))).toBeTruthy();
  });

  it("says why the merge was left uncommitted: no commit yet leads to History's first commit", async () => {
    const left = (why: "no_commit" | "changes" | "failed") => ({ ...cs, status: "applied" as const,
      applied: { t: "2026-10-28T11:00:00Z", by: "user", commit: null, uncommitted: true, why, paths: ["libs/l/amp.sch"] } });
    for (const [why, toast] of [["no_commit", "review:adopted.uncommitted.no_commit"], ["failed", "review:adopted.uncommitted.failed"],
      ["changes", "review:adopted.uncommitted"]] as const) {
      const p = await open(cs);
      vi.mocked(api.changesetAction).mockResolvedValue(left(why));
      vi.mocked(api.changeset).mockResolvedValue(left(why));
      fireEvent.click(screen.getByRole("button", { name: t("review:adopt") }));
      await confirm(t("review:adopt"));
      expect(p.toast).toHaveBeenCalledWith(msg(toast));
      expect(screen.getByText(t(`review:merge.uncommitted.${why}`))).toBeTruthy();
      expect(screen.queryByText(t("review:merge.uncommitted"))).toBeNull();
      fireEvent.click(screen.getByRole("button", { name: t("review:merge.history") }));
      expect(p.onHistory).toHaveBeenCalled();
      cleanup();
    }
    // the person's case: no other change, the repository had no commit; the words say so, not "other changes"
    expect(t("review:adopted.uncommitted.no_commit")).toContain("first commit");
    expect(t("review:adopted.uncommitted.no_commit")).not.toContain("other uncommitted changes");
  });

  it("says why a revert was left uncommitted, in the revert's own words", async () => {
    const applied: ChangesetInfo = { ...cs, status: "applied", git: { ...git, ref: "refs/undine/changesets/p/cs1" },
      applied: { t: "2026-10-28T11:00:00Z", by: "user", commit: null, uncommitted: true, why: "no_commit" } };
    const left = (why: "no_commit" | "changes" | "failed"): ChangesetInfo => ({ ...applied, status: "reverted",
      reverted: { t: "2026-10-28T12:00:00Z", by: "user", commit: null, uncommitted: true, why } });
    for (const [why, toast] of [["no_commit", "review:reverted.uncommitted.no_commit"], ["failed", "review:reverted.uncommitted.failed"],
      ["changes", "review:reverted.uncommitted"]] as const) {
      const p = await open(applied);
      vi.mocked(api.changesetAction).mockResolvedValue(left(why));
      vi.mocked(api.changeset).mockResolvedValue(left(why));
      fireEvent.click(screen.getByRole("button", { name: t("review:revert") }));
      await confirm(t("review:revert"));
      expect(p.toast).toHaveBeenCalledWith(msg(toast));
      expect(screen.getByText(t(`review:merge.revertUncommitted.${why}`))).toBeTruthy();
      expect(screen.queryByText(t("review:merge.revertUncommitted"))).toBeNull();
      cleanup();
    }
  });

  it("words a record's no_commit as what was so then: the first commit made since, it tells nobody to make it", async () => {
    // the record keeps "why" from the apply; History's first commit comes later and does not rewrite it
    for (const key of ["review:merge.uncommitted.no_commit", "review:merge.revertUncommitted.no_commit"] as const) {
      for (const locale of ["en", "ja"] as const) {
        await setLocale(locale);
        expect(t(key)).not.toMatch(/History|履歴|make the first commit|作成してください/);
      }
    }
    await setLocale("en");
    expect(t("review:merge.uncommitted.no_commit")).toContain("had no commit");
  });

  it("says when a revert was left uncommitted, in the revert's own words", async () => {
    const reverted: ChangesetInfo = { ...cs, status: "reverted", git: { ...git, ref: "refs/undine/changesets/p/cs1" },
      applied: { t: "2026-10-28T11:00:00Z", by: "user", commit: "d".repeat(40), uncommitted: false },
      reverted: { t: "2026-10-28T12:00:00Z", by: "user", commit: null, uncommitted: true } };
    const p = await open(reverted);
    expect(screen.getByText(t("review:merge.revertUncommitted"))).toBeTruthy();
    expect(screen.queryByText(t("review:merge.uncommitted"))).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: t("review:merge.history") }));
    expect(p.onHistory).toHaveBeenCalled();
  });
});

describe("opening one", () => {
  const ask = async (title: string) => {
    fireEvent.click(screen.getByRole("button", { name: t("review:new") }));
    await act(async () => {});
    fireEvent.change(screen.getByPlaceholderText(t("review:create.namePlaceholder")), { target: { value: title } });
    fireEvent.click(screen.getByRole("button", { name: t("review:create.open") }));
    await act(async () => {});
  };

  it("says when the project was put in git to open it, and reads the git status again", async () => {
    const p = await open(cs, { initialId: null });
    vi.mocked(api.openChangeset).mockResolvedValue({ ...cs, git_setup: true, first_commit: "f".repeat(40), first_commit_note: null });
    await ask("Widen the input pair");
    expect(api.openChangeset).toHaveBeenCalledWith("p", "Widen the input pair", "");
    expect(p.toast).toHaveBeenCalledWith(msg("review:create.gitSetup", { branch: "undine/p/cs1" }));
    expect(p.toast).toHaveBeenCalledTimes(1);
    expect(p.onGit).toHaveBeenCalled();
    expect(t("review:create.message")).toContain("git");               // the dialog says it beforehand
  });

  it("says when git's index was not reset to the first commit, and when the person's own commit came first", async () => {
    let p = await open(cs, { initialId: null });
    const note = "git's index was not reset to commit ffffffffffff (Unable to create '/p/.git/index.lock': File exists.): run `git reset` in /p";
    vi.mocked(api.openChangeset).mockResolvedValue({ ...cs, git_setup: true, first_commit: "f".repeat(40), first_commit_note: note });
    await ask("one");
    expect(p.toast).toHaveBeenCalledWith(msg("review:create.gitSetup", { branch: "undine/p/cs1" }));
    expect(p.toast).toHaveBeenCalledWith(msg("history:firstCommit.indexNote", { commit: "fffffff", why: note }), "error");
    cleanup();
    p = await open(cs, { initialId: null });
    vi.mocked(api.openChangeset).mockResolvedValue({ ...cs, git_setup: true, first_commit: null,
      first_commit_note: "a commit was made on its branch meanwhile: the changeset starts from it" });
    await ask("two");
    expect(p.toast).toHaveBeenCalledWith(msg("review:create.gitSetupHead", { branch: "undine/p/cs1" }));
    expect(p.toast).not.toHaveBeenCalledWith(msg("review:create.gitSetup", { branch: "undine/p/cs1" }));
  });

  it("says nothing of git when the project was in it already, and keeps the dialog open when the repository is not set up", async () => {
    const p = await open(cs, { initialId: null });
    vi.mocked(api.openChangeset).mockResolvedValueOnce({ ...cs, git_setup: false });
    await ask("one");
    expect(p.toast).not.toHaveBeenCalledWith(msg("review:create.gitSetup", { branch: "undine/p/cs1" }));
    fireEvent.click(screen.getByRole("button", { name: t("review:back") }));
    await act(async () => {});
    // the server's refusal has its code: the dialog words it, naming the History panel's button as that panel shows it
    const why = "q is in the git repository /home/u, which is not set up for Undine: set it up with `undine git-setup q` (the History panel's Set up git), or move the project out of it";
    vi.mocked(api.openChangeset).mockRejectedValueOnce(new ApiError(400, why, "not_set_up", undefined, undefined, "/home/u",
      { project: "q", command: "undine git-setup q" }));
    await setLocale("ja");
    await ask("two");
    const text = t("common:error.notSetUp", { top: "/home/u", panel: t("history:button.label"), button: t("history:setup.again"), command: "undine git-setup q" });
    expect(screen.getByText(text)).toBeTruthy();                        // shown in the dialog, which stays open
    expect(text).toContain("「セットアップ」");
    expect(text).toContain("履歴パネル");
    expect(text).toContain("`undine git-setup q`");                     // the server's command line, as it runs
    expect(screen.queryByText(why, { exact: false })).toBeNull();
    expect(screen.getByRole("dialog")).toBeTruthy();
  });
});

describe("one made before changesets were git branches", () => {
  const old: ChangesetInfo = { ...cs, git: undefined, activity: undefined };

  it("can be reviewed and discarded, not adopted: Adopt says why, and there is no file view", async () => {
    await open(old);
    expect(screen.getByText(t("review:legacy.open"))).toBeTruthy();
    const adopt = screen.getByRole("button", { name: t("review:adopt") }) as HTMLButtonElement;
    expect(adopt.disabled).toBe(true);
    expect(adopt.title).toBe(t("review:legacy.adopt"));
    expect((screen.getByRole("button", { name: t("review:discard") }) as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByRole("button", { name: t("review:listing.files") })).toBeNull();
    expect(document.querySelector(".csgit")).toBeNull();
    expect(screen.getByText("M1", { selector: ".citem b" })).toBeTruthy();   // its changes, by object, as before
  });

  it("says on discard that its copy is removed, not that a branch is kept in git; one on git says so", async () => {
    await open(old);
    fireEvent.click(screen.getByRole("button", { name: t("review:discard") }));
    await act(async () => {});
    let modal = document.querySelector(".modal") as HTMLElement;
    expect(within(modal).getByText(t("review:confirm.discard.messageOld"))).toBeTruthy();
    expect(modal.textContent).not.toContain("git");
    cleanup();
    await open(cs);
    fireEvent.click(screen.getByRole("button", { name: t("review:discard") }));
    await act(async () => {});
    modal = document.querySelector(".modal") as HTMLElement;
    expect(within(modal).getByText(t("review:confirm.discard.message"))).toBeTruthy();
  });

  it("is not offered for changes requested of the agent: its work could not be adopted", async () => {
    const review = { side: "proposal" as const, onSide: vi.fn(), onFocus: vi.fn(), focused: null, onSettled: vi.fn(), onRequestChanges: vi.fn() };
    await open(old, { review });
    expect(screen.queryByRole("button", { name: t("review:request") })).toBeNull();
    cleanup();
    await open(cs, { review });
    fireEvent.click(screen.getByRole("button", { name: t("review:request") }));
    expect(review.onRequestChanges).toHaveBeenCalledWith("p~cs1");
  });

  it("is not offered to be reverted once adopted", async () => {
    await open({ ...old, status: "applied", applied: { t: "2026-10-28T11:00:00Z", by: "user" } });
    expect(screen.queryByRole("button", { name: t("review:revert") })).toBeNull();
    expect(screen.getByText(t("review:legacy.revert"))).toBeTruthy();
  });
});

describe("an agent's re-work of a schematic", () => {
  it("lists a port reordering by the ports' names, and counts it in the operation log", async () => {
    const order = { seq: 2, op: "set_port_order", cell: "amp", view: "schematic", actor: "agent:claude", reason: "supplies first", t: "2026-10-28T10:01:00Z",
      effects: [{ k: "o" as const, do: "mod" as const, id: "ports", id_after: "ports",
                  changes: { order: [["p1", "p2", "p3"], ["p3", "p1", "p2"]], ports: [["in", "out", "vdd"], ["vdd", "in", "out"]] } as Record<string, [unknown, unknown]> }] };
    await open({ ...cs, changes: [{ lib: "l", ops: [order] }] });
    expect(screen.getByText(t("review:item.portOrder"))).toBeTruthy();
    expect(screen.getByText("in, out, vdd", { selector: ".before" })).toBeTruthy();
    expect(screen.getByText("vdd, in, out", { selector: ".after" })).toBeTruthy();
    expect(screen.queryByText(/p3, p1, p2/)).toBeNull();                    // the instance ids are not the reader's concern
    expect(screen.getByText(t("review:summary.changed", { count: 1 }))).toBeTruthy();
  });
});

describe("checks by checker", () => {
  it("pairs each checker's DRC on its own row and shows an ERC's findings as warnings", async () => {
    const done = { state: "done", fresh: true, changed: [] as string[], job: "j" };
    await open({ ...cs, verify: [
      { side: "work", kind: "drc", checker: "klayout", lib: "l", cell: "amp", ...done, ok: true, summary: { total: 0 } },
      { side: "work", kind: "drc", checker: "magic", lib: "l", cell: "amp", ...done, ok: false, summary: { total: 1 } },
      { side: "work", kind: "erc", checker: "klayout", lib: "l", cell: "amp", ...done, ok: false, severity: "warning",
        summary: { warnings: 1 }, findings: [{ instance: "XMS", model: "nfet", terminal: "D", net: "vout", tap_net: "vss",
                                               edge_um: 20, at: [0, 0, 0, 20] }] }] });
    expect(screen.getByText("DRC l/amp")).toBeTruthy();
    expect(screen.getByText(`DRC (${t("review:check.magic")}) l/amp`)).toBeTruthy();
    const erc = screen.getByText("ERC l/amp").closest(".checkrow") as HTMLElement;
    expect(within(erc).getByText(t("review:check.warnings", { count: 1 }), { exact: false }).className).toBe("warn");
    expect(within(erc).getByText(`${t("review:checks.proposal")}: ${t("review:check.finding", { instance: "XMS", terminal: "D", net: "vout", tap: "vss" })}`)).toBeTruthy();
  });
});

// A changeset being adopted on the server (live.Closings): the list and the review say how far, an Adopt pressed while
// another is adopted says it waits for it; a closed one's evidence says its size, and a person can prune its waveforms.
describe("while a changeset is being adopted", () => {
  const closing = (extra: Partial<NonNullable<ChangesetInfo["closing"]>> = {}): NonNullable<ChangesetInfo["closing"]> => ({
    action: "apply", by: "user", since: "2026-11-03T10:00:00+0900", phase: "evidence",
    files: 120, files_total: 4000, bytes: 3.1e9, bytes_total: 25e9, ...extra });

  it("says in the list that it is adopting and how far, instead of a proposal, and follows it until it is done", async () => {
    vi.useFakeTimers();
    vi.mocked(api.changesets).mockResolvedValue([{ ...cs, closing: closing() }, { ...cs, id: "cs2", title: "Other" }]);
    await open(cs, { initialId: null });
    const rows = document.querySelectorAll(".csrow");
    expect(within(rows[0] as HTMLElement).getByText(t("review:status.closing.apply"))).toBeTruthy();
    expect(within(rows[0] as HTMLElement).queryByText(t("review:status.open"))).toBeNull();
    expect(rows[0].textContent).toContain(t("review:closing.evidenceBytes", { done: "3.1", total: "25 GB" }));
    expect(within(rows[1] as HTMLElement).getByText(t("review:status.open"))).toBeTruthy();
    vi.mocked(api.changesets).mockResolvedValue([{ ...cs, status: "applied" }, { ...cs, id: "cs2", title: "Other" }]);
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(api.changesets).toHaveBeenCalledTimes(2);
    expect(within(document.querySelectorAll(".csrow")[0] as HTMLElement).getByText(t("review:status.applied"))).toBeTruthy();
    expect(api.evidenceSizes).toHaveBeenCalledTimes(2);              // it ended: the evidence it saved is counted
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(api.changesets).toHaveBeenCalledTimes(2);                  // none closing: not polled any more
  });

  it("shows the phase in the review, with Adopt and Discard unavailable, and a second one waiting for the first", async () => {
    await open({ ...cs, closing: closing() });
    expect(screen.getByRole("status").textContent).toContain(
      t("review:closing.line", { action: t("review:status.closing.apply"), phase: t("review:closing.evidenceBytes", { done: "3.1", total: "25 GB" }) }));
    expect((screen.getByRole("button", { name: t("review:adopt") }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: t("review:discard") }) as HTMLButtonElement).disabled).toBe(true);
    cleanup();
    await open({ ...cs, id: "cs2", closing: closing({ phase: "waiting", behind: { id: "cs1", title: "Widen the input pair", action: "apply" } }) });
    expect(screen.getByRole("status").textContent).toContain(t("review:closing.waiting", { title: "Widen the input pair", action: t("review:status.closing.apply") }));
  });

  it("says, when Adopt is pressed while another is being adopted, that this one waits for it", async () => {
    const p = await open(cs);
    vi.mocked(api.changesets).mockResolvedValue([{ ...cs, id: "cs9", title: "Comparator", closing: closing() }]);
    vi.mocked(api.changesetAction).mockResolvedValue({ ...cs, status: "applied", applied: { t: "2026-11-03T10:30:00+0900", by: "user", commit: null, uncommitted: true } });
    fireEvent.click(screen.getByRole("button", { name: t("review:adopt") }));
    await confirm(t("review:adopt"));
    expect(p.toast).toHaveBeenCalledWith(msg("review:closing.queued.apply", { title: "Comparator" }), "info");
    expect(api.changesetAction).toHaveBeenCalledWith("p", "cs1", "apply");
  });

  it("in a list opened before it began elsewhere, shows it once the screen's /live sees it, and follows it", async () => {
    vi.useFakeTimers();
    vi.mocked(api.changesets).mockResolvedValue([cs]);
    const p = { ...props(), initialId: null, liveClosings: "" };
    const { rerender } = render(<><DialogHost /><ChangesPanel {...p} /></>);
    await act(async () => {});
    expect(within(document.querySelector(".csrow") as HTMLElement).getByText(t("review:status.open"))).toBeTruthy();
    vi.mocked(api.changesets).mockResolvedValue([{ ...cs, closing: closing() }]);
    rerender(<><DialogHost /><ChangesPanel {...p} liveClosings="cs1" /></>);
    await act(async () => {});
    expect(within(document.querySelector(".csrow") as HTMLElement).getByText(t("review:status.closing.apply"))).toBeTruthy();
    const reads = vi.mocked(api.changesets).mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(vi.mocked(api.changesets).mock.calls.length).toBe(reads + 1);     // followed from then on
  });

  it("in the review of an adopted one, shows a revert begun on another screen", async () => {
    const adopted: ChangesetInfo = { ...cs, status: "applied", applied: { t: "2026-11-03T10:30:00+0900", by: "user", commit: "d".repeat(40) } };
    vi.mocked(api.changeset).mockResolvedValue(adopted);
    const p = { ...props(), liveClosings: "" };
    const { rerender } = render(<><DialogHost /><ChangesPanel {...p} /></>);
    await act(async () => {});
    expect(screen.queryByRole("status")).toBeNull();
    vi.mocked(api.changeset).mockResolvedValue({ ...adopted, closing: closing({ action: "revert", phase: "merging" }) });
    rerender(<><DialogHost /><ChangesPanel {...p} liveClosings="cs9,cs1" /></>);
    await act(async () => {});
    expect(screen.getByRole("status").textContent).toContain(t("review:closing.merging"));
    expect((screen.getByRole("button", { name: t("review:revert") }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("follows its own adoption every second while the server works on it", async () => {
    vi.useFakeTimers();
    await open(cs);
    let finish: (c: ChangesetInfo) => void = () => {};
    vi.mocked(api.changesetAction).mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    vi.mocked(api.changeset).mockResolvedValue({ ...cs, closing: closing({ phase: "merging" }) });
    fireEvent.click(screen.getByRole("button", { name: t("review:adopt") }));
    await confirm(t("review:adopt"));
    const reads = vi.mocked(api.changeset).mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(vi.mocked(api.changeset).mock.calls.length).toBe(reads + 1);
    expect(screen.getByRole("status").textContent).toContain(t("review:closing.merging"));
    vi.mocked(api.changeset).mockResolvedValue({ ...cs, status: "applied" });
    await act(async () => { finish({ ...cs, status: "applied" }); });
    expect(screen.queryByRole("status")).toBeNull();
  });
});

describe("the evidence of a closed changeset", () => {
  const applied: ChangesetInfo = { ...cs, status: "applied", applied: { t: "2026-11-03T10:30:00+0900", by: "user", commit: "d".repeat(40) },
    evidence: { path: "changesets/cs1/evidence", captured: "2026-11-03T10:00:00+0900", jobs: 812, bytes: 25e9,
      size: { bytes: 25e9, raw: 24.2e9, files: 9000 } } };

  it("says its size and, after a confirm naming the size, prunes its waveforms", async () => {
    const p = await open(applied);
    expect(screen.getByText(t("review:evidence.sizeRaw", { size: "25 GB", raw: "24.2 GB" }))).toBeTruthy();
    vi.mocked(api.pruneEvidence).mockResolvedValue({ pruned: { t: "2026-11-03T11:00:00+0900", by: "user", files: 800, bytes: 24.2e9 },
      size: { bytes: 0.8e9, raw: 0, files: 8200 } });
    const pruned = { ...applied, evidence: { ...applied.evidence!, size: { bytes: 0.8e9, raw: 0, files: 8200 },
      pruned: { t: "2026-11-03T11:00:00+0900", by: "user", files: 800, bytes: 24.2e9 } } };
    fireEvent.click(screen.getByRole("button", { name: t("review:evidence.prune", { size: "24.2 GB" }) }));
    await act(async () => {});
    expect(document.querySelector(".modal")?.textContent).toContain(t("review:confirm.prune.message", { size: "24.2 GB" }));
    vi.mocked(api.changeset).mockResolvedValue(pruned);
    await confirm(t("review:confirm.prune.ok", { size: "24.2 GB" }));
    expect(api.pruneEvidence).toHaveBeenCalledWith("p", "cs1");
    expect(p.toast).toHaveBeenCalledWith(msg("review:evidence.prunedToast", { size: "24.2 GB" }));
    expect(screen.getByText(t("review:evidence.pruned", { time: "2026-11-03 11:00", actor: "user", count: 800, size: "24.2 GB" }))).toBeTruthy();
    expect(screen.queryByRole("button", { name: t("review:evidence.prune", { size: "24.2 GB" }) })).toBeNull();   // nothing left to prune
  });

  it("says in one file's words what a prune of one removed", async () => {
    await open({ ...applied, evidence: { ...applied.evidence!, size: { bytes: 1e6, raw: 0, files: 9 },
      pruned: { t: "2026-11-03T11:00:00+0900", by: "user", files: 1, bytes: 4e6 } } });
    expect(screen.getByText(/: 1 file, 4 MB\. Its hash stays/)).toBeTruthy();
  });

  it("says, before and after, what of it the working copy its closure kept still holds", async () => {
    const p = await open({ ...applied, retained: true, evidence: { ...applied.evidence!,
      size: { bytes: 25e9, raw: 24.2e9, files: 9000, shared: 25e9, raw_shared: 24.2e9 } } });
    vi.mocked(api.pruneEvidence).mockResolvedValue({ pruned: { t: "2026-11-03T11:00:00+0900", by: "user", files: 800, bytes: 24.2e9, freed: 0 },
      size: { bytes: 0.8e9, raw: 0, files: 8200 } });
    fireEvent.click(screen.getByRole("button", { name: t("review:evidence.prune", { size: "24.2 GB" }) }));
    await act(async () => {});
    expect(document.querySelector(".modal")?.textContent).toContain(t("review:confirm.prune.kept", { size: "24.2 GB" }));
    await confirm(t("review:confirm.prune.ok", { size: "24.2 GB" }));
    expect(p.toast).toHaveBeenCalledWith(msg("review:evidence.prunedToastKept", { size: "24.2 GB", kept: "24.2 GB" }));
  });

  it("is not pruned when the confirm is cancelled", async () => {
    await open(applied);
    fireEvent.click(screen.getByRole("button", { name: t("review:evidence.prune", { size: "24.2 GB" }) }));
    await confirm(t("common:actions.cancel"));
    expect(api.pruneEvidence).not.toHaveBeenCalled();
  });

  it("is listed with each closed changeset's size and the total of them all", async () => {
    vi.mocked(api.changesets).mockResolvedValue([applied, { ...cs, id: "cs2", title: "Other" }]);
    vi.mocked(api.evidenceSizes).mockResolvedValue({ bytes: 57e9, raw: 52e9,
      changesets: { cs1: { bytes: 25e9, raw: 24.2e9, files: 9000, pruned: null } } });
    await open(cs, { initialId: null });
    const rows = document.querySelectorAll(".csrow");
    expect(within(rows[0] as HTMLElement).getByText("25 GB")).toBeTruthy();
    expect(rows[1].textContent).not.toContain("GB");
    expect(screen.getByText(t("review:evidence.totalRaw", { size: "57 GB", raw: "52 GB" }))).toBeTruthy();
  });
});

describe("the decision", () => {
  // adopting is the usual outcome and the changes and checks can run long: the buttons come before them, with a line
  // naming checks that got worse so that they are not adopted unseen
  const follows = (a: Element, b: Element) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
  const drc = (side: "base" | "work", ok: boolean) => ({ side, kind: "drc", lib: "l", cell: "amp", state: "done", ok, fresh: true });

  it("puts Adopt and Discard above the changes and the checks", async () => {
    await open(cs, { review: { side: "confirmed", onSide: vi.fn() } as unknown as Props["review"] });
    const adopt = screen.getByRole("button", { name: t("review:adopt") });
    const discard = screen.getByRole("button", { name: t("review:discard") });
    const side = screen.getByRole("button", { name: t("review:side.confirmed") });
    const checks = screen.getByText(t("review:checks"), { selector: ".section" });
    expect(follows(adopt, side)).toBe(true);
    expect(follows(discard, side)).toBe(true);
    expect(follows(adopt, checks)).toBe(true);
    expect(follows(adopt, screen.getByText(t("review:operationLog")))).toBe(true);
  });

  it("names checks that got worse next to the buttons, and says nothing when none did", async () => {
    await open({ ...cs, verify: [drc("base", true), drc("work", false)] as ChangesetInfo["verify"] });
    const line = screen.getByText(t("review:checks.regressedBelow", { count: 1 }));
    expect(follows(line, screen.getByText(t("review:checks"), { selector: ".section" }))).toBe(true);
    cleanup();
    await open({ ...cs, verify: [drc("base", true), drc("work", true)] as ChangesetInfo["verify"] });
    expect(screen.queryByText(t("review:checks.regressedBelow", { count: 1 }))).toBeNull();
  });

  it("puts Revert above the changes of an adopted one", async () => {
    const closed = { ...git, ref: "refs/undine/changesets/p/cs1" };
    await open({ ...cs, status: "applied", git: closed, applied: { t: "2026-10-28T11:00:00Z", by: "user", commit: "d".repeat(40), uncommitted: false } });
    expect(follows(screen.getByRole("button", { name: t("review:revert") }), screen.getByText(t("review:checks"), { selector: ".section" }))).toBe(true);
  });
});
