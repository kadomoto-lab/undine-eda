import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, type ChangesetConflictFile } from "./api";
import { apiErrorText, resolveText, setLocale, t } from "./i18n";

const answer = (status: number, body: unknown) => vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));

beforeEach(async () => {
  vi.stubGlobal("localStorage", { getItem: () => null, setItem: () => {} });
  await setLocale("en");
});
afterEach(() => vi.unstubAllGlobals());

describe("errors of the server", () => {
  it("keeps an apply's merge conflict: its code and the files with their conflicts", async () => {
    const files: ChangesetConflictFile[] = [{ path: "libs/l/amp.sch", kind: "schematic", conflicts: [
      { id: "inst:M1:prop:W", what: "instance M1: W", base: "1", ours: "3", theirs: "2" }] }];
    vi.stubGlobal("fetch", answer(409, { detail: "the merge conflicts in 1 file; nothing was written", code: "merge_conflict", files }));
    const e = await api.changesetAction("p", "cs1", "apply").catch((x: unknown) => x) as ApiError;
    expect(e).toBeInstanceOf(ApiError);
    expect(e.status).toBe(409);
    expect(e.code).toBe("merge_conflict");
    expect(e.files).toEqual(files);
    expect(e.detail).toBe("the merge conflicts in 1 file; nothing was written");
  });

  it("words any other error as before, with no files", async () => {
    vi.stubGlobal("fetch", answer(409, { detail: "the changeset is being written", code: "busy", params: {} }));
    const e = await api.changesetAction("p", "cs1", "apply").catch((x: unknown) => x) as ApiError;
    expect(e.code).toBe("busy");
    expect(e.files).toBeUndefined();
    expect(resolveText(apiErrorText(e))).toBe(resolveText(apiErrorText(new ApiError(409, "the changeset is being written", "busy", {}))));
    vi.stubGlobal("fetch", answer(400, { detail: "not set up for Undine: run git-setup", code: "invalid_request", params: {}, files: "no" }));
    const bad = await api.openChangeset("p", "t", "").catch((x: unknown) => x) as ApiError;
    expect(bad.files).toBeUndefined();                                // only a list of files is taken as one
    expect(resolveText(apiErrorText(bad))).toContain("not set up for Undine: run git-setup");
  });

  it("keeps the repository of a refusal to open a changeset where git is not set up, and words it in the reader's language", async () => {
    const detail = "q is in the git repository /home/u, which is not set up for Undine: set it up with `undine git-setup q` (the History panel's Set up git), or move the project out of it";
    const command = "undine --root /home/u/designs git-setup q";
    const refusal = { top: "/home/u", project: "q", command };
    vi.stubGlobal("fetch", answer(400, { detail, code: "not_set_up", ...refusal, params: refusal }));
    const e = await api.openChangeset("q", "t", "").catch((x: unknown) => x) as ApiError;
    expect(e.code).toBe("not_set_up");
    expect(e.top).toBe("/home/u");
    expect(e.refusal?.project).toBe("q");
    // the button as the History panel shows it in that state (setup.again), in the panel the top bar opens; the
    // server's command line, the store's root in it (the project's store is not the command's default one)
    const words = () => resolveText(apiErrorText(e));
    expect(words()).toBe(t("common:error.notSetUp", { top: "/home/u", panel: t("history:button.label"), button: t("history:setup.again"), command }));
    expect(words()).toContain("“Set up” in the History panel");
    expect(words()).toContain(`\`${command}\``);
    await setLocale("ja");
    expect(words()).toContain("/home/u");
    expect(words()).toContain("履歴パネルの「セットアップ」");
    expect(words()).toContain(`\`${command}\``);
    expect(words()).not.toContain(detail);
    // an older server names no command: made of the project (`undine git-setup` alone stops at its missing argument)
    await setLocale("en");
    vi.stubGlobal("fetch", answer(400, { detail, code: "not_set_up", top: "/home/u", project: "q", params: { top: "/home/u", project: "q" } }));
    const old = await api.openChangeset("q", "t", "").catch((x: unknown) => x) as ApiError;
    expect(resolveText(apiErrorText(old))).toContain("`undine git-setup q`");
  });

  it("words a refused save of text loaded before its file changed (409 stale) in the reader's language", async () => {
    const detail = "the RTL of l/ctr changed since it was loaded (a git merge, checkout or pull): reload it";
    vi.stubGlobal("fetch", answer(409, { detail, code: "stale", params: {} }));
    const e = await api.rtlEdit("p", "l", "ctr", "save", "module ctr; endmodule", "abc").catch((x: unknown) => x) as ApiError;
    expect(e.code).toBe("stale");
    expect(resolveText(apiErrorText(e))).toContain(t("common:error.stale"));
    await setLocale("ja");
    const words = resolveText(apiErrorText(e));
    expect(words).toContain(t("common:error.stale"));
    expect(words).toContain("再読み込み");
    expect(words).toContain(detail);                                    // the server's words, as for the other codes
  });

  describe("a library a changeset's working copy cannot hold (400 unsupported_library)", () => {
    const cases = [
      { kind: "link_out", path: "libs/shared", target: "/home/u/outside/shared", key: "common:error.library.linkOut" },
      { kind: "link_nowhere", path: "libs/shared", target: "../data/shared", key: "common:error.library.linkNowhere" },
      { kind: "own_repository", path: "libs/clone", target: null, key: "common:error.library.ownRepository" },
    ] as const;
    for (const c of cases) {
      it(`names the library, its path and why (${c.kind}), in EN and JA`, async () => {
        const detail = `cannot open a changeset: library shared: ${c.path} is ...`;
        vi.stubGlobal("fetch", answer(400, { detail, code: "unsupported_library", library: "shared", path: c.path, kind: c.kind, target: c.target,
          params: { library: "shared", path: c.path, kind: c.kind, target: c.target } }));
        const e = await api.openChangeset("p", "t", "").catch((x: unknown) => x) as ApiError;
        expect(e.code).toBe("unsupported_library");
        expect(e.refusal).toEqual({ library: "shared", path: c.path, kind: c.kind, target: c.target });
        for (const locale of ["en", "ja"] as const) {
          await setLocale(locale);
          const words = resolveText(apiErrorText(e));
          expect(words).toBe(t(c.key, { library: "shared", path: c.path, target: c.target ?? "" }));
          expect(words).toContain("shared");
          expect(words).toContain(c.path);
          if (c.target) expect(words).toContain(c.target);
          expect(words).not.toContain(detail);
        }
        expect(resolveText(apiErrorText(e))).toContain("変更セットを開けません");
      });
    }

    it("words a kind it does not know with the library, its path and the server's words", async () => {
      const e = new ApiError(400, "cannot open a changeset: library shared: something new", "unsupported_library", {}, undefined, undefined,
        { library: "shared", path: "libs/shared", kind: "elsewhere" });
      for (const locale of ["en", "ja"] as const) {
        await setLocale(locale);
        const words = resolveText(apiErrorText(e));
        expect(words).toContain(t("common:error.library", { library: "shared", path: "libs/shared" }));
        expect(words).toContain("something new");
      }
    });
  });

  it("reads a changeset's diff, all of it or one cell's", async () => {
    const fetch = answer(200, { base: "b", rev: null, cells: [], other: [] });
    vi.stubGlobal("fetch", fetch);
    await api.changesetDiff("p", "cs 1");
    await api.changesetDiff("p", "cs1", "l", "amp");
    expect(fetch.mock.calls.map((c) => (c as unknown[])[0])).toEqual(["/api/p/p/changesets/cs%201/diff", "/api/p/p/changesets/cs1/diff?lib=l&cell=amp"]);
  });
});
