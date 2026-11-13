// The words of a semantic diff's changes, from their ids: changes as the server's view modules give them
// (gitmerge.diff of real files, server/undine/vcs/*merge.py), in English as `undine diff` says them where the
// meaning allows, and in Japanese; a change whose id is of no known shape keeps the server's words.
import { beforeEach, describe, expect, it } from "vitest";
import type { SemanticChange } from "../api";
import { resolveText, setLocale } from "../i18n";
import { changeText, conflictText } from "./gitItems";

type Row = [kind: string, change: SemanticChange, en: string | null, ja: string];   // en null: the server's words
const ROWS: Row[] = [
  ["schematic", { do: "add", id: "K:spice_ignore", what: "attribute spice_ignore", before: null, after: "false" }, null, "属性 spice_ignore"],
  ["schematic", { do: "mod", id: "inst:M1:prop:W", what: "instance M1: W", before: "2", after: "3" }, null, "インスタンス M1: W"],
  ["schematic", { do: "mod", id: "rename:M2", what: "instance M2: name", before: "M2", after: "M7" }, null, "インスタンス M2: 名前"],
  ["schematic", { do: "mod", id: "inst:p1:placement", what: "instance p1: placement", before: { x: -100, y: 0, rot: 0, flip: 0 },
    after: { x: -110, y: 0, rot: 0, flip: 0 } }, null, "インスタンス p1: 配置"],
  ["schematic", { do: "add", id: "inst:M3", what: "instance M3", before: null, after: { sym: "sky130_fd_pr/nfet_01v8.sym", x: 40, y: 60 } },
    null, "インスタンス M3"],
  ["schematic", { do: "mod", id: "inst:XM1[3:0]:prop:W", what: "instance XM1[3:0]: W", before: "1", after: "2" }, null, "インスタンス XM1[3:0]: W"],
  ["schematic", { do: "add", id: "inst:@devices/noconn.sym@10,20,0,0#1189a773", what: "unnamed instance devices/noconn.sym at 10,20",
    before: null, after: { sym: "devices/noconn.sym", x: 10, y: 20, rot: 0, flip: 0, props: {} } }, null, "名前のないインスタンス devices/noconn.sym（10,20）"],
  ["schematic", { do: "add", id: "inst:@devices/noconn.sym@10,20,0,0#1189a773", what: "unnamed instance devices/noconn.sym at 10,20 (2 copies)",
    before: null, after: { sym: "devices/noconn.sym", x: 10, y: 20, rot: 0, flip: 0, props: {} } }, null, "名前のないインスタンス devices/noconn.sym（10,20）2 個"],
  ["schematic", { do: "del", id: "wire:0,0,100,0", what: "wire 0,0,100,0", before: { x1: 0, y1: 0, x2: 100, y2: 0, props: { lab: "Y" } }, after: null },
    null, "配線 0,0,100,0"],
  ["schematic", { do: "mod", id: "text:-80,-200,CMOS inverter:rot", what: "text -80,-200,CMOS inverter: rot", before: 0, after: 1 },
    null, "テキスト -80,-200,CMOS inverter: 回転"],
  ["schematic", { do: "add", id: "text:5,5,note: a:b", what: "text 5,5,note: a:b", before: null, after: { s: "note: a:b", x: 5, y: 5 } },
    null, "テキスト 5,5,note: a:b"],
  ["schematic", { do: "mod", id: "ports", what: "port order (the subcircuit's pin order)", before: ["p1", "p2"], after: ["p2", "p1"] },
    null, "ポートの順序（サブサーキットのピン順）"],
  ["symbol", { do: "mod", id: "drawing:lines", what: "drawing: lines", before: [], after: [] }, null, "図形: 線"],
  ["symbol", { do: "mod", id: "drawing:boxes", what: "drawing: boxes (the pins and their order): pins A, Y -> A, Z", before: [], after: [] },
    "drawing: boxes (the pins and their order): pins A, Y → A, Z", "図形: 矩形（ピンとその順序）: ピン A, Y → A, Z"],
  ["layout", { do: "add", id: "cell:extra", what: "cell extra added", before: null, after: { shapes: 1, instances: 0, meta: [] } }, null, "セル extra を追加"],
  ["layout", { do: "add", id: "shape:extra:66/20", what: "cell extra layer 66/20: shapes 1 added", before: null,
    after: { shapes: 1, added: 1, examples: [] } }, null, "セル extra レイヤー 66/20: 図形 1 個を追加"],
  ["layout", { do: "mod", id: "shape:amp:met1", what: "cell amp layer met1: shapes 2 added, 1 removed", before: { shapes: 4, removed: 1, examples: [] },
    after: { shapes: 5, added: 2, examples: [] } }, null, "セル amp レイヤー met1: 図形 2 個を追加、1 個を削除"],
  ["layout", { do: "add", id: "meta:inv:undine.note", what: "cell inv: meta undine.note added", before: null, after: "x" },
    null, "セル inv: メタ情報 undine.note を追加"],
  ["layout", { do: "add", id: "inst:inv:via1 r0 *1 700,100", what: "cell inv: 1 instance(s) of via1 at r0 *1 0.7,0.1 added", before: null,
    after: { child: "via1", placement: "r0 *1 0.7,0.1", props: {}, count: 1 } }, null, "セル inv: via1 のインスタンス 1 個を r0 *1 0.7,0.1 に追加"],
  ["layout", { do: "del", id: "inst:inv:X2", what: "cell inv: instance X2 of via1 at r0 *1 0.9,0.1 removed",
    before: { child: "via1", placement: "r0 *1 0.9,0.1", props: {} }, after: null }, null, "セル inv: r0 *1 0.9,0.1 にある via1 のインスタンス X2 を削除"],
  ["layout", { do: "mod", id: "inst:inv:X1",
    what: "cell inv: instance X1: child cell via1 -> via2; placement r0 *1 0.1,0.1 -> r0 *1 0.3,0.1; property W None -> 2",
    before: { child: "via1", placement: "r0 *1 0.1,0.1", props: {} }, after: { child: "via2", placement: "r0 *1 0.3,0.1", props: { W: "2" } } },
  "cell inv: instance X1: child cell via1 → via2; placement r0 *1 0.1,0.1 → r0 *1 0.3,0.1; property W ∅ → 2",
  "セル inv: インスタンス X1: 子セル via1 → via2; 配置 r0 *1 0.1,0.1 → r0 *1 0.3,0.1; プロパティ W ∅ → 2"],
  ["netlist", { do: "add", id: "port:EN", what: "port EN", before: null, after: { name: "EN", type: "ipin" } }, null, "ポート EN"],
  ["netlist", { do: "mod", id: "inst:M1:props:W", what: "instance M1: property W", before: "2", after: "3" }, null, "インスタンス M1: プロパティ W"],
  ["netlist", { do: "mod", id: "inst:M1:pins:G", what: "instance M1: pin G", before: "A", after: "net9" }, null, "インスタンス M1: ピン G"],
  ["netlist", { do: "mod", id: "inst:M2:symbol", what: "instance M2: symbol", before: "a.sym", after: "b.sym" }, null, "インスタンス M2: シンボル"],
  ["netlist", { do: "add", id: "pending:M1.G", what: "pending connection M1.G", before: null, after: "net9" }, null, "保留中の接続 M1.G"],
  ["intent", { do: "mod", id: "notes", what: "notes", before: "a", after: "b" }, null, "メモ"],
  ["intent", { do: "add", id: "port:inp:role", what: "port inp: role", before: null, after: "changed" }, null, "ポート inp: 役割"],
  ["intent", { do: "add", id: "supply:VDD9", what: "supply VDD9", before: null, after: { v: 1.8 } }, null, "電源 VDD9"],
  ["intent", { do: "mod", id: "item:g1:text", what: "item g1: text", before: "a", after: "b" }, null, "項目 g1: 本文"],
  // an item with a uid: its change id is the uid, the words name the id a person reads
  ["intent", { do: "mod", id: "view:layout:item:uid:7f3a:strength", what: "layout view item c3: strength", before: "should", after: "must" },
    "Layout view item c3: strength", "レイアウトビューの項目 c3: 強さ"],
  ["intent", { do: "add", id: "item:uid:91be", what: "item c9 (constraint): Keep it small", before: null,
    after: { id: "c9", kind: "constraint", text: "Keep it small", strength: "should" } }, "item c9 (Constraint): Keep it small", "項目 c9（制約）: Keep it small"],
  ["log", { do: "add", id: "seq:2", what: "move inv (layout) by agent:x: \"make room\"", before: null,
    after: { seq: 2, op: "move", cell: "inv", view: "layout", actor: "agent:x", reason: "make room", changeset: null } },
  "move inv (Layout) by agent:x: \"make room\"", "inv（レイアウト）の move（agent:x）:「make room」"],
  ["log", { do: "add", id: "seq:3", what: "undo of 2 by user", before: null,
    after: { seq: 3, op: "undo", cell: null, view: null, actor: "user", reason: null, changeset: null, of: 2 } }, null, "操作 2 の undo（user）"],
  // not known here: the server's words in either language
  ["schematic", { do: "mod", id: "name:M1", what: "instance name M1: given to 2 instances", before: 1, after: 2 },
    null, "instance name M1: given to 2 instances"],
  ["layout", { do: "mod", id: "shape:inv:68/20", what: "cell inv layer 68/20: shapes rearranged", before: { shapes: 1 }, after: { shapes: 1 } },
    null, "cell inv layer 68/20: shapes rearranged"],
  ["text", { do: "mod", id: "x", what: "something", before: null, after: null }, null, "something"],
];

beforeEach(async () => { await setLocale("en"); });

describe("the words of a semantic diff", () => {
  it("says each known change as `undine diff` does in English, where the meaning allows", () => {
    for (const [kind, c, en] of ROWS) expect(resolveText(changeText(kind, c)), c.id).toBe(en ?? c.what);
  });

  it("says each known change in Japanese, and keeps the server's words for any other", async () => {
    await setLocale("ja");
    for (const [kind, c, , ja] of ROWS) expect(resolveText(changeText(kind, c)), c.id).toBe(ja);
  });

  it("words a conflict only when its English words are the change's", async () => {
    await setLocale("ja");
    const c = { id: "inst:M1:prop:W", what: "instance M1: W", base: "3", ours: "5", theirs: "4" };
    expect(resolveText(conflictText("schematic", c))).toBe("インスタンス M1: W");
    // the same id standing for more than its change
    const both = { id: "inst:M9", what: "instance M9: added on both sides with different content", ours: {}, theirs: {} };
    expect(resolveText(conflictText("schematic", both))).toBe(both.what);
    expect(resolveText(conflictText(null, c))).toBe(c.what);
  });
});
