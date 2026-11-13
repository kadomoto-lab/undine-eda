// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, type GitDiff, type GitLogCommit, type GitStatus } from "../api";
import { DialogHost } from "../dialog";
import { msg, resolveText, setLocale, t } from "../i18n";
import { HistoryPanel } from "./HistoryPanel";
import { cellIndex, cellMarkText, groupGit, libraryCount, libraryGit, relativeDate, shellWord, shortValue } from "./gitItems";

vi.mock("../api", async (original) => ({ ...await original<typeof import("../api")>(), api: {
  gitLog: vi.fn(), gitDiff: vi.fn(), gitVerify: vi.fn(), gitSetup: vi.fn(), gitFirstCommit: vi.fn(), layoutReload: vi.fn(),
} }));
vi.mock("../layout", () => ({ Splitter: () => null }));

const head = { sha: "a".repeat(40), short: "aaaaaaa", parents: ["b".repeat(40)], subject: "Widen the input pair", body: "",
  author: "Ada", email: "ada@example.com", date: new Date(Date.now() - 3 * 60_000).toISOString(), refs: ["main"] };
const empty: GitStatus = { repo: false, setup: null, prefix: null, branch: null, head: null, operation: null, conflicted: [],
  cells: [], other: [], unsaved: [] };
const repo: GitStatus = { ...empty, repo: true, setup: { merge: true, diff: true, hook: true }, prefix: "", branch: "main", head,
  cells: [{ lib: "l", cell: "amp", views: [
    { view: "schematic", path: "libs/l/amp.sch", status: "modified" },
    { view: "layout", path: "libs/l/amp.oas", status: "untracked" },
  ] }],
  other: [{ path: "project.json", status: "modified" }],
  unsaved: [{ lib: "l", cells: ["inv"] }] };
const ampDiff: GitDiff = { base: head.sha, rev: null, other: [], cells: [{ lib: "l", cell: "amp", views: [
  { view: "schematic", path: "libs/l/amp.sch", status: "modified", diff: { kind: "schematic", changes: [
    { do: "mod", id: "inst:M1:prop:W", what: "instance M1: W", before: "1", after: "2" },
    { do: "add", id: "wire:3", what: "wire 3", before: null, after: { x1: 0, y1: 0, x2: 10, y2: 0 } },
  ] } },
  { view: "layout", path: "libs/l/amp.oas", status: "untracked", diff: { kind: "layout", changes: [
    { do: "mod", id: "shape:amp:met1", what: "cell amp layer met1: shapes 2 added, 1 removed",
      before: { shapes: 4, removed: 1, examples: [] }, after: { shapes: 5, added: 2, examples: [] } },
  ], geometry: [{ cell: "amp", layer: "met1", xor_um2: 0, geometry_unchanged: true }] } },
] }] };
const merge: GitLogCommit = { ...head, sha: "c".repeat(40), short: "ccccccc", parents: ["a".repeat(40), "d".repeat(40)],
  subject: "Merge branch 'bias'", refs: [], cells: [{ lib: "l", cell: "top", views: [{ view: "layout", path: "libs/l/top.oas", status: "modified" }] }], other: 1 };

const props: Omit<ComponentProps<typeof HistoryPanel>, "status"> = { project: "p", error: null, cell: null, tick: 0,
  onStatus: vi.fn(), onRefresh: vi.fn(), onReloaded: vi.fn(), onOpenView: vi.fn(), onClose: vi.fn(), toast: vi.fn() };
async function open(status: GitStatus | null, extra: Partial<typeof props> = {}) {
  const r = render(<><DialogHost /><HistoryPanel {...props} {...extra} status={status} /></>);
  await act(async () => {});
  return r;
}

beforeEach(async () => {
  vi.resetAllMocks();
  vi.mocked(api.gitLog).mockResolvedValue({ commits: [], more: false });
  vi.mocked(api.gitDiff).mockResolvedValue(ampDiff);
  await setLocale("en");
});
afterEach(() => cleanup());

describe("history panel", () => {
  it("offers to set git up when the project is not in a repository, after saying what it does", async () => {
    vi.mocked(api.gitSetup).mockResolvedValue({ ...repo, hook_note: null });
    await open(empty);
    expect(screen.getByText(t("history:setup.intro"))).toBeTruthy();
    expect(api.gitLog).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: t("history:setup.button") }));
    await act(async () => {});
    const dialog = screen.getByText(t("history:setup.message")).closest(".modal") as HTMLElement;
    expect(api.gitSetup).not.toHaveBeenCalled();                  // nothing happens before the person agrees
    fireEvent.click(within(dialog).getByRole("button", { name: t("history:setup.button") }));
    await act(async () => {});
    expect(api.gitSetup).toHaveBeenCalledWith("p");
    expect(props.onStatus).toHaveBeenCalledWith({ ...repo, hook_note: null });
    expect(props.toast).toHaveBeenCalledWith(msg("history:setup.done"));
  });

  it("sends a proposal's working copy to the real project, with nothing to set up", async () => {
    await open({ ...empty, preview: true }, { project: "p~cs1" });
    expect(screen.getByText(t("history:preview", { project: "p" }))).toBeTruthy();
    expect(screen.queryByRole("button", { name: t("history:setup.button") })).toBeNull();
  });

  it("names a proposal's branch and base in its working copy and leads back to its review, with nothing to set up", async () => {
    const onReview = vi.fn();
    await open({ ...empty, preview: true, changeset: "cs1", branch: "undine/p/cs1", base: "b".repeat(40),
      stale: [{ lib: "l", cells: ["inv"] }] }, { project: "p~cs1", onReview });
    expect(screen.getByText("undine/p/cs1")).toBeTruthy();
    expect(screen.getByText(t("history:preview.changeset", { changeset: "cs1", base: "bbbbbbb", project: "p" }))).toBeTruthy();
    expect(screen.queryByText(t("history:preview", { project: "p" }))).toBeNull();
    expect(screen.queryByRole("button", { name: t("history:setup.button") })).toBeNull();
    expect(api.gitLog).not.toHaveBeenCalled();                       // its commits are its review's, not a history
    // the working copy's own layouts changed on disk under unsaved edits: said here too
    expect(screen.getByText(t("history:stale", { lib: "l", cells: "inv" }))).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: t("history:preview.review") }));
    expect(onReview).toHaveBeenCalledWith("cs1");
  });

  it("lists the uncommitted changes by cell and shows a cell's semantic diff", async () => {
    await open(repo);
    expect(screen.getByText("main")).toBeTruthy();
    expect(screen.getByText("Widen the input pair")).toBeTruthy();
    expect(screen.getByText(`${t("history:view.schematic")} · ${t("history:status.modified")}`)).toBeTruthy();
    expect(screen.getByText(`${t("history:view.layout")} · ${t("history:status.untracked")}`)).toBeTruthy();
    expect(screen.getByText(t("history:unsaved", { lib: "l", cells: "inv" }))).toBeTruthy();
    expect(api.gitDiff).not.toHaveBeenCalled();                   // a diff is read when a cell is opened
    fireEvent.click(screen.getByText("l/amp"));
    await act(async () => {});
    expect(api.gitDiff).toHaveBeenCalledWith("p", "worktree", "l", "amp");
    expect(screen.getByText("instance M1: W")).toBeTruthy();
    expect(screen.getByText("1", { selector: ".before" })).toBeTruthy();
    expect(screen.getByText("2", { selector: ".after" })).toBeTruthy();
    expect(screen.getByText(t("history:diff.xor", { cell: "amp", layer: "met1", area: "0" }))).toBeTruthy();
    expect(screen.getByText(t("history:diff.sameGeometry"))).toBeTruthy();
    fireEvent.click(screen.getByTitle(t("history:open", { view: t("history:view.schematic") })));
    expect(props.onOpenView).toHaveBeenCalledWith("l", "amp", "schematic");
  });

  it("expands a merge commit into its diff and the cells to check again", async () => {
    vi.mocked(api.gitLog).mockResolvedValue({ commits: [merge], more: true });
    vi.mocked(api.gitDiff).mockResolvedValue({ base: head.sha, rev: merge.sha, other: [{ path: "undine.lock", status: "modified" }], cells: [
      { lib: "l", cell: "top", views: [{ view: "layout", path: "libs/l/top.oas", status: "modified", diff: { kind: "layout",
        changes: [{ do: "mod", id: "inst:top:X1", what: "cell top: instance X1 placement", before: "r0 0,0", after: "r0 5,0" }],
        geometry: [] } }] }] });
    vi.mocked(api.gitVerify).mockResolvedValue({ ours: head.sha, theirs: "d".repeat(40), commit: merge.sha, parents: merge.parents,
      operation: null, unmerged: [], warnings: [{ path: "libs/l/top.sch", warning: "M3 placed twice" }], merged: [], no_report: [],
      verify: [{ lib: "l", cell: "top", why: "merged: libs/l/top.oas" }], notes: [] });
    await open({ ...repo, cells: [], unsaved: [], other: [] });
    expect(screen.getByText(t("history:uncommitted.none"))).toBeTruthy();
    expect(screen.getByText(t("history:commit.otherFiles", { count: 1 }))).toBeTruthy();
    fireEvent.click(screen.getByText("Merge branch 'bias'"));
    await act(async () => {});
    expect(api.gitDiff).toHaveBeenCalledWith("p", merge.sha, undefined, undefined);
    expect(screen.getByText("cell top: instance X1 placement")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: t("history:verify.button") }));
    await act(async () => {});
    expect(api.gitVerify).toHaveBeenCalledWith("p", merge.sha);
    expect(screen.getByText(t("history:verify.cells", { count: 1 }))).toBeTruthy();
    expect(screen.getByText("merged: libs/l/top.oas")).toBeTruthy();
    expect(screen.getByText("M3 placed twice")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: t("history:commits.more") }));
    await act(async () => {});
    expect(api.gitLog).toHaveBeenLastCalledWith("p", { skip: 1, lib: undefined, cell: undefined });
  });

  it("shows a merge in progress with each open conflict's values and how to take theirs", async () => {
    vi.mocked(api.gitVerify).mockResolvedValue({ ours: head.sha, theirs: "d".repeat(40), commit: null, parents: null,
      operation: "merge", unmerged: [{ path: "libs/l/amp.sch", kind: "schematic", conflicts: [
        { id: "inst:M1:prop:W", what: "instance M1: W", base: "3", ours: "5", theirs: "4", taken: "ours", resolved: false }] }],
      warnings: [], merged: ["libs/l/amp.sch"], no_report: [], verify: [], notes: [] });
    vi.mocked(api.gitDiff).mockResolvedValue({ base: head.sha, rev: null, other: [], cells: [{ lib: "l", cell: "amp", views: [
      { view: "schematic", path: "libs/l/amp.sch", status: "conflicted", diff: { kind: "schematic", changes: [] } }] }] });
    await open({ ...repo, operation: "merge", conflicted: ["libs/l/amp.sch"], unsaved: [],
      cells: [{ lib: "l", cell: "amp", views: [{ view: "schematic", path: "libs/l/amp.sch", status: "conflicted" }] }] });
    expect(api.gitVerify).toHaveBeenCalledWith("p");                // the merge in progress, not a commit
    expect(screen.getByText(t("history:operation.merge"))).toBeTruthy();
    expect(screen.getByText("instance M1: W")).toBeTruthy();
    expect(screen.getByTitle(t("history:conflict.taken")).textContent).toContain("5");
    expect(screen.getByText(t("history:conflict.resolve", { count: 1 }))).toBeTruthy();
    expect(screen.getByText("undine resolve libs/l/amp.sch --theirs inst:M1:prop:W")).toBeTruthy();
    fireEvent.click(screen.getByText("l/amp"));
    await act(async () => {});
    expect(screen.getByText(t("history:diff.conflicted"))).toBeTruthy();   // not "formatting only"
    expect(screen.queryByText(t("history:diff.none"))).toBeNull();
  });

  it("narrows the changes and the commits to the active tab's cell", async () => {
    await open(repo, { cell: { lib: "l", cell: "inv" } });
    fireEvent.click(screen.getByLabelText(t("history:only", { cell: "l/inv" })));
    await act(async () => {});
    expect(api.gitLog).toHaveBeenLastCalledWith("p", { lib: "l", cell: "inv" });
    expect(screen.queryByText("l/amp")).toBeNull();
    expect(screen.getByText(t("history:unsaved", { lib: "l", cells: "inv" }))).toBeTruthy();
  });

  it("offers the first commit only while the repository has none, and makes it on a click", async () => {
    const unborn = { ...repo, head: null, cells: [], other: [], unsaved: [] };
    const button = () => screen.queryByRole("button", { name: t("history:firstCommit.button") });
    let r = await open(repo);                                          // a commit there: nothing offered
    expect(button()).toBeNull();
    r.unmount();
    r = await open(unborn, { cell: { lib: "l", cell: "amp" } });
    fireEvent.click(screen.getByLabelText(t("history:only", { cell: "l/amp" })));
    await act(async () => {});
    expect(button()).toBeNull();                                       // one cell's commits: not the project's first
    r.unmount();
    await open(unborn);
    expect(screen.getByText(t("history:commits.none"))).toBeTruthy();
    expect(screen.getByText(t("history:firstCommit.hint"))).toBeTruthy();   // what it commits, and why, beforehand
    const made = { ...repo, cells: [], other: [], unsaved: [], commit: head.sha };
    vi.mocked(api.gitFirstCommit).mockResolvedValue(made);
    vi.mocked(api.gitLog).mockClear();
    fireEvent.click(button()!);
    await act(async () => {});
    expect(api.gitFirstCommit).toHaveBeenCalledWith("p");
    expect(props.onStatus).toHaveBeenCalledWith(made);
    expect(api.gitLog).toHaveBeenCalled();                             // the commits read again
    expect(props.toast).toHaveBeenCalledWith(msg("history:firstCommit.done", { commit: "aaaaaaa" }));
  });

  it("says when the first commit was made meanwhile, and reads the status again", async () => {
    vi.mocked(api.gitFirstCommit).mockRejectedValue(new ApiError(409, "p's repository has a commit already", "has_commits"));
    await open({ ...repo, head: null, unsaved: [] });
    fireEvent.click(screen.getByRole("button", { name: t("history:firstCommit.button") }));
    await act(async () => {});
    expect(props.toast).toHaveBeenCalledWith(msg("history:firstCommit.hasCommits"), "error");
    expect(props.onRefresh).toHaveBeenCalled();
  });

  it("offers the first commit only in a repository set up for Undine, and says how to make it otherwise", async () => {
    const unborn = { ...repo, head: null, cells: [], other: [], unsaved: [] };
    let r = await open({ ...unborn, setup: { merge: false, diff: false, hook: false } });     // the server would refuse
    expect(screen.queryByRole("button", { name: t("history:firstCommit.button") })).toBeNull();
    expect(screen.getByText(t("history:noCommit.notSetUp"))).toBeTruthy();
    expect(screen.queryByText(t("history:noCommit"))).toBeNull();
    r.unmount();
    r = await open(unborn, { cell: { lib: "l", cell: "amp" } });
    fireEvent.click(screen.getByLabelText(t("history:only", { cell: "l/amp" })));
    await act(async () => {});
    expect(screen.getByText(t("history:noCommit.filter", { cell: "l/amp" }))).toBeTruthy();   // the button is under Commits, unfiltered
    r.unmount();
    await open(unborn);
    expect(screen.getByText(t("history:noCommit"))).toBeTruthy();
  });

  it("waits for unsaved layout edits before the first commit, and words the server's refusals as the first commit's", async () => {
    const unborn = { ...repo, head: null, cells: [], other: [], unsaved: [{ lib: "l", cells: ["inv", "top"] }] };
    let r = await open(unborn);
    const button = () => screen.getByRole("button", { name: t("history:firstCommit.button") }) as HTMLButtonElement;
    expect(button().disabled).toBe(true);
    expect(screen.getByText(t("history:firstCommit.saveFirst", { layouts: "l (inv, top)" }))).toBeTruthy();
    r.unmount();
    // a log the panel does not know of (edits from before a restart): the server refuses, naming it
    r = await open({ ...unborn, unsaved: [] });
    vi.mocked(api.gitFirstCommit).mockRejectedValueOnce(new ApiError(409, "library m has layout edits not saved", "unsaved",
      { logs: [{ path: "history/m.jsonl", lib: "m", cells: ["pad"] }] }));
    fireEvent.click(button());
    await act(async () => {});
    expect(props.toast).toHaveBeenCalledWith(msg("history:firstCommit.unsaved", { layouts: "m (pad)" }), "error");
    expect(props.onRefresh).toHaveBeenCalled();
    // a library no changeset's working copy can hold: not "A changeset cannot be opened"
    vi.mocked(api.gitFirstCommit).mockRejectedValueOnce(new ApiError(400, "library l: libs/l is a symbolic link", "unsupported_library",
      { library: "l", path: "libs/l", kind: "link_out", target: "/elsewhere" }, undefined, undefined,
      { library: "l", path: "libs/l", kind: "link_out", target: "/elsewhere" }));
    fireEvent.click(button());
    await act(async () => {});
    const said = msg("history:firstCommit.unsupported.linkOut", { library: "l", path: "libs/l", target: "/elsewhere" });
    expect(props.toast).toHaveBeenCalledWith(said, "error");
    expect(resolveText(said)).not.toContain("changeset cannot be opened");
    expect(resolveText(said)).toContain("make the first commit again");
  });

  it("says when git's index was not reset to the first commit", async () => {
    const note = "git's index was not reset to commit aaaaaaaaaaaa (Unable to create '/p/.git/index.lock': File exists.): run `git reset` in /p";
    vi.mocked(api.gitFirstCommit).mockResolvedValue({ ...repo, commit: head.sha, index_note: note });
    await open({ ...repo, head: null, unsaved: [] });
    fireEvent.click(screen.getByRole("button", { name: t("history:firstCommit.button") }));
    await act(async () => {});
    expect(props.toast).toHaveBeenCalledWith(msg("history:firstCommit.done", { commit: "aaaaaaa" }));
    expect(props.toast).toHaveBeenCalledWith(msg("history:firstCommit.indexNote", { commit: "aaaaaaa", why: note }), "error");
  });

  it("says when setting git up could not make the first commit, and why", async () => {
    vi.mocked(api.gitSetup).mockResolvedValue({ ...repo, head: null, hook_note: null, init: true, commit: null, index_note: null,
      commit_refused: { code: "unsaved", detail: "library l has layout edits not saved", logs: [{ path: "history/l.jsonl", lib: "l", cells: ["inv"] }] } });
    await open(empty);
    fireEvent.click(screen.getByRole("button", { name: t("history:setup.button") }));
    await act(async () => {});
    fireEvent.click(within(screen.getByText(t("history:setup.message")).closest(".modal") as HTMLElement)
      .getByRole("button", { name: t("history:setup.button") }));
    await act(async () => {});
    expect(props.toast).toHaveBeenCalledWith(msg("history:setup.doneNoCommit",
      { why: msg("history:firstCommit.unsaved", { layouts: "l (inv)" }) }), "error");
    expect(props.toast).not.toHaveBeenCalledWith(msg("history:setup.done"));
  });

  it("says when setting git up made the first commit", async () => {
    vi.mocked(api.gitSetup).mockResolvedValue({ ...repo, hook_note: null, commit: head.sha });
    await open(empty);
    fireEvent.click(screen.getByRole("button", { name: t("history:setup.button") }));
    await act(async () => {});
    fireEvent.click(within(screen.getByText(t("history:setup.message")).closest(".modal") as HTMLElement)
      .getByRole("button", { name: t("history:setup.button") }));
    await act(async () => {});
    expect(props.toast).toHaveBeenCalledWith(msg("history:setup.doneCommit", { commit: "aaaaaaa" }));
    expect(t("history:setup.message")).toContain("first commit");      // the dialog says so beforehand
  });

  it("says why the status could not be read", async () => {
    await open(null, { error: "not found" });
    expect(screen.getByText(t("history:unavailable"))).toBeTruthy();
    expect(screen.getByText("not found")).toBeTruthy();
  });
});

describe("history panel, what review found", () => {
  it("reads an open diff of the working tree again with each status, though the file keeps its status", async () => {
    const r = await open(repo);
    fireEvent.click(screen.getByText("l/amp"));
    await act(async () => {});
    expect(api.gitDiff).toHaveBeenCalledTimes(1);
    // the schematic changed again (W 2 -> 3): still "modified", a new status all the same
    vi.mocked(api.gitDiff).mockResolvedValue({ ...ampDiff, cells: [{ ...ampDiff.cells[0], views: [{ ...ampDiff.cells[0].views[0],
      diff: { kind: "schematic", changes: [{ do: "mod", id: "inst:M1:prop:W", what: "instance M1: W", before: "1", after: "3" }] } }] }] });
    r.rerender(<><DialogHost /><HistoryPanel {...props} status={{ ...repo }} /></>);
    await act(async () => {});
    expect(api.gitDiff).toHaveBeenCalledTimes(2);
    expect(screen.getByText("3", { selector: ".after" })).toBeTruthy();
  });

  it("keeps each project's commit diffs apart in one repository", async () => {
    const x = { ...head, sha: "e".repeat(40), short: "eeeeeee", subject: "Add alpha and beta", refs: [] };
    vi.mocked(api.gitLog).mockImplementation(async (p) => ({ commits: [{ ...x, cells: [{ lib: "lib", cell: p === "p1" ? "alpha" : "beta",
      views: [{ view: "schematic" as const, path: `libs/lib/${p === "p1" ? "alpha" : "beta"}.sch`, status: "added" as const }] }], other: 0 }], more: false }));
    vi.mocked(api.gitDiff).mockImplementation(async (p) => ({ base: head.sha, rev: x.sha, other: [], cells: [{ lib: "lib",
      cell: p === "p1" ? "alpha" : "beta", views: [{ view: "schematic" as const, path: `libs/lib/${p === "p1" ? "alpha" : "beta"}.sch`,
        status: "added" as const, diff: { kind: "schematic", changes: [] } }] }] }));
    const status = { ...repo, cells: [], unsaved: [], other: [] };
    const r = await open(status, { project: "p1" });
    fireEvent.click(screen.getByText("Add alpha and beta"));
    await act(async () => {});
    expect(screen.getByText("lib/alpha")).toBeTruthy();
    r.rerender(<><DialogHost /><HistoryPanel {...props} project="p2" status={{ ...status }} /></>);
    await act(async () => {});
    expect(screen.queryByText("lib/alpha")).toBeNull();
    fireEvent.click(screen.getByText("Add alpha and beta"));
    await act(async () => {});
    expect(api.gitDiff).toHaveBeenLastCalledWith("p2", x.sha, undefined, undefined);
    expect(screen.getByText("lib/beta")).toBeTruthy();
    expect(screen.queryByText("lib/alpha")).toBeNull();
  });

  it("says that layout edits which cancel out are not saved, which the commit hook refuses", async () => {
    await open({ ...repo, cells: [], other: [{ path: "history/l.jsonl", status: "modified" }], unsaved: [{ lib: "l", cells: [] }] });
    expect(screen.getByText(t("history:unsaved.cancelled", { lib: "l" }))).toBeTruthy();
    expect(screen.queryByText(t("history:uncommitted.none"))).toBeNull();
  });

  it("says why git-setup installed no pre-commit hook, instead of that it is set up", async () => {
    const why = "the pre-commit hook is not installed (.git/hooks/pre-commit is not Undine's): add `python -m undine precommit` to it";
    vi.mocked(api.gitSetup).mockResolvedValue({ ...repo, setup: { merge: true, diff: true, hook: false }, hook_note: why });
    const missing = { ...repo, setup: { merge: false, diff: true, hook: false } };
    const r = await open(missing);
    fireEvent.click(screen.getByRole("button", { name: t("history:setup.again") }));
    await act(async () => {});
    fireEvent.click(within(screen.getByText(t("history:setup.message")).closest(".modal") as HTMLElement)
      .getByRole("button", { name: t("history:setup.button") }));
    await act(async () => {});
    expect(props.toast).toHaveBeenCalledWith(msg("history:setup.hookNote", { why }), "error");
    expect(props.toast).not.toHaveBeenCalledWith(msg("history:setup.done"));
    r.rerender(<><DialogHost /><HistoryPanel {...props} status={{ ...repo, setup: { merge: true, diff: true, hook: false } }} /></>);
    expect(screen.getByText(t("history:setup.hookNote", { why }))).toBeTruthy();
    // only the hook is missing, and setting up again cannot install it
    expect(screen.queryByRole("button", { name: t("history:setup.again") })).toBeNull();
  });

  it("says which lines of Undine's output the .gitignore lacks, and sets them up on request only", async () => {
    vi.mocked(api.gitSetup).mockResolvedValue({ ...repo, hook_note: null, gitignore_added: ["/changesets/requests/"] });
    const r = await open({ ...repo, setup: { merge: true, diff: true, hook: true, gitignore: ["/changesets/requests/"] } });
    expect(screen.getByText(t("history:setup.gitignore", { lines: "/changesets/requests/" }))).toBeTruthy();
    expect(screen.queryByText(t("history:setup.incomplete", { missing: "" }), { exact: false })).toBeNull();
    expect(api.gitSetup).not.toHaveBeenCalled();                    // never written while reading the status
    fireEvent.click(screen.getByRole("button", { name: t("history:setup.again") }));
    await act(async () => {});
    fireEvent.click(within(screen.getByText(t("history:setup.message")).closest(".modal") as HTMLElement)
      .getByRole("button", { name: t("history:setup.button") }));
    await act(async () => {});
    expect(api.gitSetup).toHaveBeenCalledWith("p");
    expect(props.toast).toHaveBeenCalledWith(msg("history:setup.gitignoreAdded", { lines: "/changesets/requests/" }));
    r.rerender(<><DialogHost /><HistoryPanel {...props} status={{ ...repo, setup: { merge: true, diff: true, hook: true, gitignore: [] } }} /></>);
    expect(screen.queryByRole("button", { name: t("history:setup.again") })).toBeNull();
  });

  it("ends a rebase with git rebase --continue, not with a commit", async () => {
    vi.mocked(api.gitVerify).mockResolvedValue({ ours: head.sha, theirs: "d".repeat(40), commit: null, parents: null,
      operation: "rebase", unmerged: [], warnings: [], merged: [], no_report: [], verify: [], notes: [] });
    const r = await open({ ...repo, operation: "rebase", conflicted: ["libs/l/amp.sch"] });
    expect(screen.getByText(t("history:operation.resolve.rebase"))).toBeTruthy();
    expect(t("history:operation.resolve.rebase")).toContain("git rebase --continue");
    r.rerender(<><DialogHost /><HistoryPanel {...props} status={{ ...repo, operation: "rebase", conflicted: [] }} /></>);
    await act(async () => {});
    expect(screen.getByText(t("history:operation.clean.rebase"))).toBeTruthy();
    expect(screen.queryByText(t("history:operation.clean.merge"))).toBeNull();
  });

  it("opens a commit or a cell's diff from the keyboard: the rows are buttons, with no button inside", async () => {
    vi.mocked(api.gitLog).mockResolvedValue({ commits: [{ ...head, cells: [], other: 0 }], more: false });
    await open(repo);
    const rows = screen.getAllByRole("button", { expanded: false });
    expect(rows.map((b) => b.textContent)).toEqual(["l/amp", "Widen the input pairmain"]);
    for (const row of rows) {
      expect(row.tagName).toBe("BUTTON");
      expect(row.querySelector("button")).toBeNull();
      row.focus();
      expect(document.activeElement).toBe(row);
    }
    fireEvent.click(rows[1]);
    await act(async () => {});
    expect(rows[1].getAttribute("aria-expanded")).toBe("true");
  });

  it("offers neither the views nor the page of a cell that is gone", async () => {
    const big = { lib: "l", cell: "big", views: [{ view: "schematic" as const, path: "libs/l/big.sch", status: "deleted" as const }] };
    const wantCells = vi.fn();
    const r = await open({ ...repo, cells: [big, ...repo.cells!] }, { wantCells });
    // the library's cells are not read yet: a cell whose every changed file was deleted is taken as gone
    expect(wantCells).toHaveBeenCalledWith("l");
    const row = (name: string) => screen.getByText(name).closest(".gitcellrow") as HTMLElement;
    expect(within(row("l/big")).queryByTitle(t("history:openCell"))).toBeNull();
    expect(within(row("l/amp")).getByTitle(t("history:openCell"))).toBeTruthy();
    // read: big is gone, amp has no layout any more
    const cellViews = (_lib: string, cell: string) => (cell === "amp" ? ["schematic" as const] : null);
    r.rerender(<><DialogHost /><HistoryPanel {...props} wantCells={wantCells} cellViews={cellViews} status={{ ...repo, cells: [big, ...repo.cells!] }} /></>);
    expect(within(row("l/big")).queryByTitle(t("history:openCell"))).toBeNull();
    expect(within(row("l/amp")).getByTitle(t("history:openCell"))).toBeTruthy();
    expect(within(row("l/amp")).queryByTitle(t("history:open", { view: t("history:view.layout") }))).toBeNull();
    expect(within(row("l/amp")).getByTitle(t("history:open", { view: t("history:view.schematic") }))).toBeTruthy();
  });

  it("says when files changed on disk under unsaved layout edits, and drops the edits after asking", async () => {
    vi.mocked(api.layoutReload).mockResolvedValue({});
    await open({ ...repo, stale: [{ lib: "l", cells: ["inv"] }] });
    expect(screen.getByText(t("history:stale", { lib: "l", cells: "inv" }))).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: t("history:stale.reload") }));
    await act(async () => {});
    expect(api.layoutReload).not.toHaveBeenCalled();                 // nothing is lost before the person agrees
    fireEvent.click(within(screen.getByText(t("history:stale.message", { lib: "l" })).closest(".modal") as HTMLElement)
      .getByRole("button", { name: t("history:stale.reload") }));
    await act(async () => {});
    expect(api.layoutReload).toHaveBeenCalledWith("p", "l");
    expect(props.onReloaded).toHaveBeenCalledWith("l");
  });

  it("words the diff, the merge check and its notes in Japanese, keeping the server's words for what it does not know", async () => {
    await setLocale("ja");
    vi.mocked(api.gitLog).mockResolvedValue({ commits: [merge], more: false });
    vi.mocked(api.gitDiff).mockResolvedValue({ base: head.sha, rev: merge.sha, other: [], cells: [{ lib: "l", cell: "inv", views: [
      { view: "schematic", path: "libs/l/inv.sch", status: "modified", diff: { kind: "schematic", changes: [
        { do: "mod", id: "inst:M1:prop:W", what: "instance M1: W", before: "2", after: "3" },
        { do: "add", id: "inst:@devices/noconn.sym@10,20,0,0#1189a773", what: "unnamed instance devices/noconn.sym at 10,20 (2 copies)",
          before: null, after: { sym: "devices/noconn.sym", x: 10, y: 20, rot: 0, flip: 0, props: {} } },
        { do: "mod", id: "ports", what: "port order (the subcircuit's pin order)", before: ["p1", "p2"], after: ["p2", "p1"] },
        { do: "mod", id: "something:new", what: "a change of a kind not known here", before: 1, after: 2 },
      ] } },
      { view: "layout", path: "libs/l/inv.oas", status: "modified", diff: { kind: "layout", changes: [
        { do: "mod", id: "shape:inv:68/20", what: "cell inv layer 68/20: shapes 2 added",
          before: { shapes: 1, removed: 0, examples: [] }, after: { shapes: 3, added: 2, examples: [] } },
      ], geometry: [] } },
    ] }] });
    vi.mocked(api.gitVerify).mockResolvedValue({ ours: head.sha, theirs: "d".repeat(40), commit: merge.sha, parents: merge.parents,
      operation: null, unmerged: [], warnings: [], merged: [], no_report: [], verify: [
        { lib: "l", cell: "buf", why: "its views are ours'; changed below it: l/inv", reason: { kind: "side", side: "ours", below: [{ lib: "l", cell: "inv", side: null }] } },
        { lib: "l", cell: "inv", why: "its views were merged", reason: { kind: "merged" } },
        { lib: "l", cell: "top", why: "its views are theirs'; what it instantiates resolves differently", reason: { kind: "resolves", side: "theirs" } },
      ], notes: ["an octopus merge runs no merge driver: ..."], note_codes: [{ code: "octopus" }] });
    await open({ ...repo, cells: [], unsaved: [], other: [] });
    fireEvent.click(screen.getByText("Merge branch 'bias'"));
    await act(async () => {});
    expect(screen.getByText("インスタンス M1: W")).toBeTruthy();
    expect(screen.getByText("名前のないインスタンス devices/noconn.sym（10,20）2 個")).toBeTruthy();
    expect(screen.getByText("ポートの順序（サブサーキットのピン順）")).toBeTruthy();
    expect(screen.getByText("セル inv レイヤー 68/20: 図形 2 個を追加")).toBeTruthy();
    expect(screen.getByText("a change of a kind not known here")).toBeTruthy();     // not known: the server's words
    fireEvent.click(screen.getByRole("button", { name: t("history:verify.button") }));
    await act(async () => {});
    expect(screen.getByText("ビューは ours のものですが、下位で変わったセルがあります: l/inv")).toBeTruthy();
    expect(screen.getByText("ビューがマージで統合されました")).toBeTruthy();
    expect(screen.getByText("ビューは theirs のものですが、インスタンス化しているセルの参照先が変わっています")).toBeTruthy();
    expect(screen.getByText(t("history:verify.note.octopus"))).toBeTruthy();
    const text = document.querySelector(".history")!.textContent!;
    for (const english of ["its views", "unnamed instance", "port order", "shapes 2 added", "octopus merge runs"]) expect(text).not.toContain(english);
  });

  it("words an open conflict in Japanese only where its words are a change's", async () => {
    await setLocale("ja");
    vi.mocked(api.gitVerify).mockResolvedValue({ ours: head.sha, theirs: "d".repeat(40), commit: null, parents: null,
      operation: "merge", unmerged: [{ path: "libs/l/amp.sch", kind: "schematic", conflicts: [
        { id: "inst:M1:prop:W", what: "instance M1: W", base: "3", ours: "5", theirs: "4", taken: "ours", resolved: false },
        { id: "inst:M9", what: "instance M9: added on both sides with different content", ours: {}, theirs: {}, taken: "ours", resolved: false },
      ] }, { path: "libs/l/amp.sym", kind: "symbol", conflicts: null, error: "ValueError: cannot read" }],
      warnings: [], merged: [], no_report: [], verify: [], notes: [] });
    await open({ ...repo, operation: "merge", conflicted: ["libs/l/amp.sch", "libs/l/amp.sym"], unsaved: [], cells: [] });
    expect(screen.getByText("インスタンス M1: W")).toBeTruthy();
    expect(screen.getByText("instance M9: added on both sides with different content")).toBeTruthy();
    expect(screen.getByText(t("history:conflict.error", { error: "ValueError: cannot read" }))).toBeTruthy();
  });
});

describe("git status helpers", () => {
  it("indexes changed and unsaved cells and words their markers", () => {
    const index = cellIndex(repo);
    expect([...index.keys()].sort()).toEqual(["l/amp", "l/inv"]);
    expect(libraryCount(index, "l")).toBe(2);
    expect(libraryCount(index, "m")).toBe(0);
    expect(resolveText(cellMarkText(index.get("l/amp")!))).toBe(
      `${t("history:mark.changed", { views: "Schematic" })}\n${t("history:mark.new", { views: "Layout" })}`);
    expect(resolveText(cellMarkText(index.get("l/inv")!))).toBe(t("history:mark.unsaved"));
    expect(cellIndex({ ...repo, repo: false }).size).toBe(0);
  });

  it("marks a library whose unsaved edits cancel out or whose files changed under them, and a folded group of cells", () => {
    const s: GitStatus = { ...repo, unsaved: [{ lib: "l", cells: ["inv"] }, { lib: "m", cells: [] }], stale: [{ lib: "l", cells: ["inv"] }] };
    const libs = libraryGit(s);
    expect(libs.get("m")).toEqual({ unsaved: true, stale: null });   // no cell shows it: the library does
    expect(libs.get("l")).toEqual({ unsaved: false, stale: ["inv"] });
    // outside git only a layout changed on disk under unsaved edits is marked (its save is refused there too)
    expect([...libraryGit({ ...s, repo: false }).entries()]).toEqual([["l", { unsaved: false, stale: ["inv"] }]]);
    const index = cellIndex(s);
    expect(groupGit(index, "l", ["amp", "inv", "ref"])).toEqual({ kind: "changed", unsaved: true, count: 2 });
    expect(groupGit(index, "l", ["ref"])).toBeNull();
  });

  it("shortens values and dates the way people read them", () => {
    expect(shortValue({ x: 1, y: "a\nb" })).toBe("x=1 y=a\\nb");
    expect(shortValue("x".repeat(80), 10)).toBe(`${"x".repeat(9)}…`);
    expect(shortValue(null)).toBe("∅");
    expect(shellWord("inst:M1:prop:W")).toBe("inst:M1:prop:W");
    expect(shellWord("shape:amp:68/20:box (0,0;1,1)")).toBe("'shape:amp:68/20:box (0,0;1,1)'");
    expect(shellWord("it's")).toBe("'it'\\''s'");
    const now = Date.parse("2026-10-28T12:00:00Z");
    expect(relativeDate("2026-10-28T11:57:00Z", "en", now)).toBe("3 minutes ago");
    expect(relativeDate("2026-10-26T12:00:00Z", "ja", now)).toBe("一昨日");
    expect(relativeDate("2026-10-28T12:00:30Z", "en", now)).toBe("now");
  });
});


describe("history panel, the last recheck", () => {
  it("offers the cell page of a cell that has only an intent file, and no save hint for a layout whose save is refused", async () => {
    const s: GitStatus = { ...repo, cells: [{ lib: "l", cell: "plan", views: [{ view: "intent", path: "libs/l/plan.intent.yaml", status: "untracked", stamp: "1:2" }] }],
      unsaved: [{ lib: "l", cells: ["inv"] }], stale: [{ lib: "l", cells: ["inv"] }] };
    await open(s, { cellViews: () => null });
    const row = (await screen.findByText("l/plan")).closest(".gitcellrow") as HTMLElement;
    expect(within(row).getByTitle(t("history:openCell"))).toBeTruthy();
    expect(screen.queryByText((x) => x.includes(resolveText(msg("history:unsaved", { lib: "l", cells: "inv" }))))).toBeNull();
    expect(screen.getByText(t("history:stale.reload"))).toBeTruthy();
  });
});
