import { describe, expect, it } from "vitest";
import type { ChangeOp, Effect } from "../api";
import { checkResultLabel, describeEffect, followText, groupChanges, tabViewOf } from "./changeItems";
import { resolveText, setLocale } from "../i18n";

let seq = 0;
const op = (effects: Effect[], reason: string | null = null, extra: Partial<ChangeOp> = {}): ChangeOp =>
  ({ seq: ++seq, op: "x", cell: "top", view: "schematic", actor: "agent:a", reason, t: "", effects, ...extra });
const mod = (id: string, field: string, a: unknown, b: unknown): Effect => ({ k: "i", id, do: "mod", changes: { [`props.${field}`]: [a, b] } });
const group = (...ops: ChangeOp[]) => groupChanges({ changes: [{ lib: "l", ops }] });

describe("groupChanges", () => {
  it("merges repeated edits of one field: first before, last after, every reason", () => {
    const [it] = group(op([mod("R1", "value", "1k", "10k")], "cut"), op([mod("R1", "value", "10k", "12k")], "margin"));
    expect(it.label).toBe("R1");
    expect(it.fields).toEqual([{ name: "value", before: "1k", after: "12k" }]);
    expect(it.reasons.map((r) => r.text)).toEqual(["cut", "margin"]);
    expect(it.objectId).toBe("R1");
  });

  it("drops a value that was put back", () => {
    expect(group(op([mod("R1", "value", "1k", "2k")]), op([mod("R1", "value", "2k", "1k")]))).toEqual([]);
  });

  it("keeps symbol pin property changes even when the drawing coordinates do not change", () => {
    const before = [[5, 0, 0, 5, 5, { name: "IN", dir: "in" }]];
    const after = [[5, 0, 0, 5, 5, { name: "OUT", dir: "out" }]];
    const effect = (a: unknown, b: unknown): Effect => ({ k: "g", id: "boxes", do: "mod", changes: { value: [a, b] } });
    const change = op([effect(before, after)], null, { view: "symbol" });
    const items = group(change);
    expect(items).toHaveLength(1);
    expect(items[0].view).toBe("symbol");
    expect(items[0].fields[0]).toEqual({ name: "value", before, after });
    expect(items[0].objectId).toBeUndefined();
    expect(group(change, op([effect(after, before)], null, { view: "symbol" }))).toEqual([]);
  });

  it("drops an object that was added and removed again", () => {
    expect(group(op([{ k: "i", id: "M1", do: "add" }]), op([{ k: "i", id: "M1", do: "del" }]))).toEqual([]);
  });

  it("keeps a removal, and an object removed then put back is a modification", () => {
    expect(group(op([{ k: "i", id: "M1", do: "del" }]))[0].kind).toBe("del");
    expect(group(op([{ k: "i", id: "M1", do: "del" }]), op([{ k: "i", id: "M1", do: "add" }]))[0].kind).toBe("mod");
  });

  it("keeps an added object added when it is edited afterwards", () => {
    const [it] = group(op([{ k: "i", id: "M1", do: "add" }]), op([mod("M1", "W", "1", "2")]));
    expect(it.kind).toBe("add");
    expect(it.fields).toEqual([{ name: "W", after: "2" }]);       // new in the changeset: no "1 → 2" to show
  });

  it("separates objects, cells and views; only instances can be probed", () => {
    const items = group(
      op([mod("R1", "value", "1k", "2k")]),
      op([mod("R1", "value", "1k", "2k")], null, { cell: "other" }),
      op([{ k: "w", id: "w1", do: "add" }]),
      op([{ k: "s", do: "add", t: "box", layer: "met1", s: "0,0,1,1" }], null, { view: "layout" }),
    );
    expect(items.map((i) => [i.cell, i.view, resolveText(i.label)])).toEqual([
      ["top", "schematic", "R1"], ["other", "schematic", "R1"], ["top", "schematic", "wire w1"], ["top", "layout", "box on met1"],
    ]);
    expect(items.map((i) => i.objectId)).toEqual(["R1", "R1", undefined, undefined]);
  });

  it("follows a renamed instance", () => {
    const [it] = group(op([{ k: "i", id: "R1", id_after: "R9", do: "mod", changes: { "props.value": ["1", "2"] } }]));
    expect(it.label).toBe("R9");
    expect(it.objectId).toBe("R9");
  });
});

describe("groupChanges: layout follows schematic", () => {
  const sync = (seq: number, causes: Record<string, number[]> | undefined, from: string, to: string): ChangeOp => ({
    seq, op: "sdl_sync", cell: "top", view: "layout", actor: "agent:a", reason: "follow the schematic", t: "", causes,
    effects: [{ k: "i", do: "del", cell: "top", child: from, inst: "M2" }, { k: "i", do: "add", cell: "top", child: to, inst: "M2" }],
  });

  it("hangs the layout device under the schematic change that caused it", () => {
    const s = op([mod("M2", "W", "1", "2")], "wider");
    const l = sync(s.seq + 1, { M2: [s.seq] }, "nfet_1", "nfet_2");
    const items = group(s, l);
    const [sch, lay] = items;
    expect(items).toHaveLength(2);
    expect(sch.followedBy).toEqual([lay.key]);
    expect(lay.causedBy).toEqual([sch.key]);
    expect(lay.kind).toBe("mod");                       // removed and placed again
    expect(resolveText(followText(lay))).toBe("nfet_1 → nfet_2");
  });

  it("leaves the layout item on its own when no schematic change explains it", () => {
    const [lay] = group(sync(++seq, undefined, "a", "a"));
    expect(lay.causedBy).toEqual([]);
    expect(resolveText(followText(lay))).toBe("re-placed");
  });

  it("does not link when the schematic change cancelled out", () => {
    const s1 = op([mod("M2", "W", "1", "2")]), s2 = op([mod("M2", "W", "2", "1")]);
    const items = group(s1, s2, sync(s2.seq + 1, { M2: [s1.seq, s2.seq] }, "a", "b"));
    expect(items).toHaveLength(1);
    expect(items[0].causedBy).toEqual([]);
  });
});

describe("groupChanges: cells taken out with their content (delete_unused_cells)", () => {
  const shapes = (cell: string, n: number, d: "add" | "del"): Effect[] =>
    Array.from({ length: n }, (_, i) => ({ k: "s", do: d, cell, layer: "68/20", t: "box", s: `(${i},0;${i + 1},1)` }));
  const drop = (cell: string, n: number, extra: Partial<ChangeOp> = {}) =>
    op([...shapes(cell, n, "del"), { k: "c", do: "del", cell, kept: true }], "unused", { cell, view: "layout", ...extra });

  it("drops a cell the changeset made and took out again, its shapes with it", () => {
    const made = op([{ k: "c", do: "add", cell: "nfet_ab12cd" }], "placed", { cell: "nfet_ab12cd", view: "layout" });
    expect(group(made, drop("nfet_ab12cd", 20))).toEqual([]);
  });

  it("shows a cell of the baseline taken out as one removed cell, its shapes in that item", () => {
    const items = group(drop("old_cell", 20));
    expect(items).toHaveLength(1);
    expect([items[0].kind, resolveText(items[0].label), items[0].cell]).toEqual(["del", "cell old_cell", "old_cell"]);
    expect(items[0].effects).toHaveLength(21);
  });

  it("files a dropped cell under its own name when one operation takes out several (a wrapper and its variant)", () => {
    const both = op([...shapes("v_tie", 2, "del"), { k: "c", do: "del", cell: "v_tie", kept: true },
                     ...shapes("v", 3, "del"), { k: "c", do: "del", cell: "v", kept: true }], "unused", { cell: "v_tie", view: "layout" });
    const items = group(both);
    expect(items.map((i) => [i.kind, i.cell, i.effects.length])).toEqual([["del", "v_tie", 3], ["del", "v", 4]]);
    const made = (c: string) => op([{ k: "c", do: "add", cell: c }], "placed", { cell: c, view: "layout" });
    expect(group(made("v"), made("v_tie"), both)).toEqual([]);
  });

  it("a restore that brings a dropped cell back leaves nothing for a baseline cell, and the new cell for a made one", () => {
    const back = (cell: string) => op([{ k: "c", do: "add", cell, kept: true }, ...shapes(cell, 20, "add")], null,
                                      { op: "restore", cell: null, view: "layout" });
    expect(group(drop("old_cell", 20), back("old_cell"))).toEqual([]);
    const made = op([{ k: "c", do: "add", cell: "n" }], "placed", { cell: "n", view: "layout" });
    const items = group(made, drop("n", 20), back("n"));
    expect(items.map((i) => [i.kind, i.cell])).toEqual([["add", "n"]]);
  });

  it("an object the changeset drew in the cell goes with it as before", () => {
    const drew = op(shapes("old_cell", 1, "add"), "a probe", { cell: "old_cell", view: "layout" });
    const items = group(drew, drop("old_cell", 3));
    expect(items.map((i) => [i.kind, i.effects.length])).toEqual([["del", 3]]);
  });
});

describe("groupChanges: new cells", () => {
  it("shows a new schematic / symbol file and a new layout cell as added items, one per view", () => {
    const items = group(
      op([{ k: "v", do: "add", text: "v {}" }], "a new amplifier", { cell: "ota" }),
      op([{ k: "c", do: "add", cell: "ota" }], "a new amplifier", { cell: "ota", view: "layout" }),
      op([{ k: "v", do: "add", text: "v {}" }], "its symbol", { cell: "ota", view: "symbol" }),
    );
    expect(items.map((i) => [i.kind, resolveText(i.label), i.view])).toEqual([
      ["add", "schematic ota", "schematic"], ["add", "cell ota", "layout"], ["add", "symbol ota", "symbol"],
    ]);
  });

  it("shows a library an agent created, with no cell (create_library)", () => {
    const [it] = groupChanges({ changes: [{ lib: "cdac", ops: [
      op([{ k: "l", do: "add", lib: "cdac" }], "a block of its own", { op: "create_library", cell: null, view: null as unknown as string }),
    ] }] });
    expect([it.kind, resolveText(it.label), it.lib, it.cell]).toEqual(["add", "library cdac", "cdac", ""]);
    expect(it.reasons.map((r) => r.text)).toEqual(["a block of its own"]);
    expect(it.objectId).toBeUndefined();
  });

  it("translates cached labels without changing grouping, identifiers or recorded reasons", async () => {
    const items = group(
      op([{ k: "v", do: "add", text: "v {}" }], "keep this reason", { cell: "ota" }),
      op([{ k: "s", do: "add", t: "box", layer: "met1", s: "0,0,1,1" }], null, { view: "layout" }),
    );
    const snapshot = JSON.stringify(items);
    try {
      await setLocale("ja");
      expect(items.map((i) => resolveText(i.label))).toEqual(["回路図 ota", "met1 上の矩形"]);
      expect(JSON.stringify(items)).toBe(snapshot);
      expect(items[0].reasons[0].text).toBe("keep this reason");
      await setLocale("en");
      expect(items.map((i) => resolveText(i.label))).toEqual(["schematic ota", "box on met1"]);
    } finally { await setLocale("en"); }
  });
});

describe("check result labels", () => {
  it("localizes structured DRC/LVS summaries with English plural forms and keeps old diagnostics", async () => {
    const one = checkResultLabel({ summary: { total: 1 }, detail: "1 violations" });
    const many = checkResultLabel({ summary: { total: 2 } });
    const zero = checkResultLabel({ summary: { total: 0 } });
    const match = checkResultLabel({ summary: { match: true } });
    const mismatch = checkResultLabel({ summary: { match: false } });
    const legacy = checkResultLabel({ detail: "raw archived diagnostic: 1G, met1, OTA" });
    const warnings = checkResultLabel({ summary: { warnings: 7 }, detail: "7 body taps abut a source / drain of another net" });
    // an ERC row whose ERC could not be made has a summary of its own (not its LVS run's match): its detail shows
    const ercFailed = checkResultLabel({ summary: {}, detail: "ERC failed: OSError: simulated" });
    try {
      await setLocale("en");
      expect([one, many, zero, match, mismatch, warnings].map(resolveText)).toEqual(["1 violation", "2 violations", "0 violations", "match", "mismatch", "7 warnings"]);
      await setLocale("ja");
      expect([one, many, zero, match, mismatch, warnings].map(resolveText)).toEqual(["違反 1 件", "違反 2 件", "違反 0 件", "一致", "不一致", "警告 7 件"]);
      expect(resolveText(legacy)).toBe("raw archived diagnostic: 1G, met1, OTA");
      expect(resolveText(ercFailed)).toBe("ERC failed: OSError: simulated");
    } finally { await setLocale("en"); }
  });
});

describe("design intent in the review", () => {
  it("shows an intent entry by name and only the fields that changed", () => {
    const items = groupChanges({ changes: [{ lib: "l", ops: [
      { seq: 1, op: "add_intent", cell: "amp", view: "intent", actor: "agent:t", reason: "from the request", t: "",
        effects: [{ k: "n", id: "items.c1", do: "add", obj: { id: "c1", kind: "constraint", text: "M1, M2 symmetric" } }] },
      { seq: 2, op: "update_intent", cell: "amp", view: "intent", actor: "user", reason: "relax", t: "",
        effects: [{ k: "n", id: "items.c1", do: "mod", changes: { value: [
          { id: "c1", kind: "constraint", text: "M1, M2 symmetric", strength: "must" },
          { id: "c1", kind: "constraint", text: "M1, M2 symmetric", strength: "should" }] } }] },
    ] }] } as never);
    expect(items).toHaveLength(1);
    expect(resolveText(items[0].label)).toBe("intent items.c1");
    expect(items[0].kind).toBe("add");
    expect(items[0].fields).toEqual([{ name: "strength", after: "should" }]);
  });
});

describe("a flow configuration in the review", () => {
  const flow = (a: string, b: string): Effect =>
    ({ k: "r", id: "flow:sar_logic", do: "mod", file: "flows/sar_logic.yaml", changes: { text: [a, b] } });

  it("shows a proposed flow as one item with its text before and after", async () => {
    const items = group(op([flow("", "top: {lib: sar}\n")], "lay it out", { view: "flow", cell: "sar_logic" }),
                        op([flow("top: {lib: sar}\n", "top: {lib: sar}\nsource: schematic\n")], null, { view: "flow", cell: "sar_logic" }));
    expect(items).toHaveLength(1);
    expect(items[0].view).toBe("flow");
    expect(resolveText(items[0].label)).toBe("flow configuration sar_logic");
    expect(items[0].fields).toEqual([{ name: "text", before: "", after: "top: {lib: sar}\nsource: schematic\n" }]);
    expect(resolveText(describeEffect(items[0].effects[0]))).toBe("+ flow configuration flows/sar_logic.yaml");
    expect(resolveText(describeEffect(flow("a", "b")))).toBe("~ flow configuration flows/sar_logic.yaml");
    expect(resolveText(describeEffect(flow("a", "")))).toBe("− flow configuration flows/sar_logic.yaml");
    try {
      await setLocale("ja");
      expect(resolveText(items[0].label)).toBe("フロー設定 sar_logic");
    } finally { await setLocale("en"); }
  });
});

describe("schematic re-drawing in the review", () => {
  const order = (a: string[], b: string[], ia: string[], ib: string[]): Effect =>
    ({ k: "o", id: "ports", id_after: "ports", do: "mod", changes: { order: [ia, ib], ports: [a, b] } });

  it("shows a port reordering by the ports' names, as one item however often they were reordered", async () => {
    const items = group(op([order(["in", "out", "vdd"], ["vdd", "in", "out"], ["p1", "p2", "p3"], ["p3", "p1", "p2"])], "supply first"),
                        op([order(["vdd", "in", "out"], ["vdd", "out", "in"], ["p3", "p1", "p2"], ["p3", "p2", "p1"])]));
    expect(items).toHaveLength(1);
    expect(resolveText(items[0].label)).toBe("port order");
    expect(items[0].fields).toEqual([{ name: "ports", before: "in, out, vdd", after: "vdd, out, in" }]);
    expect(items[0].objectId).toBeUndefined();
    expect(resolveText(describeEffect(items[0].effects[0]))).toBe("port order in, out, vdd → vdd, in, out");
    expect(group(op([order(["a", "b"], ["b", "a"], ["p1", "p2"], ["p2", "p1"])]),
                 op([order(["b", "a"], ["a", "b"], ["p2", "p1"], ["p1", "p2"])]))).toEqual([]);   // put back: nothing to review
    try {
      await setLocale("ja");
      expect(resolveText(items[0].label)).toBe("ポートの順序");
    } finally { await setLocale("en"); }
  });

  it("tells a view file deleted (delete_cell) from one created", async () => {
    expect(resolveText(describeEffect({ k: "v", do: "add", text: "v {}" }))).toBe("+ new view file");
    expect(resolveText(describeEffect({ k: "v", do: "del", kept: true, text: "v {}" }))).toBe("− view file removed");
    try {
      await setLocale("ja");
      expect(resolveText(describeEffect({ k: "v", do: "del", kept: true, text: "v {}" }))).toBe("− ビューファイルを削除");
    } finally { await setLocale("en"); }
  });

  it("shows a moved symbol as its position change and the re-routed wires as wires", () => {
    const items = group(op([{ k: "i", id: "x6", id_after: "x6", do: "mod", changes: { y: [200, 300], "props.undine_placed": ["auto", null] } },
                           { k: "w", id: "50,200,160,200", do: "del" }, { k: "w", id: "50,300,160,300", do: "add" }], "room", { op: "move" }));
    expect(items.map((i) => [resolveText(i.label), i.kind])).toEqual([["x6", "mod"], ["wire 50,200,160,200", "del"], ["wire 50,300,160,300", "add"]]);
    expect(items[0].fields).toContainEqual({ name: "y", before: 200, after: 300 });
  });
});

describe("tabViewOf", () => {
  it("opens a changed item in its view, or on the cell page when it has no view tab (intent, flow)", () => {
    expect(["schematic", "symbol", "layout", "rtl"].map(tabViewOf)).toEqual(["schematic", "symbol", "layout", "rtl"]);
    expect(tabViewOf("flow")).toBe("cell");
    expect(tabViewOf("intent")).toBe("cell");
  });
});
