import { describe, expect, it } from "vitest";
import type { SchInst, Schematic } from "../api";
import { schDiff, schDiffEmpty } from "./diff";

const inst = (id: string, over: Partial<SchInst> = {}): SchInst =>
  ({ i: 0, id, x: 0, y: 0, rot: 0, flip: 0, name: id, symbol: "r.sym", type: "", bbox: [0, 0, 10, 10], props: { value: "1k" }, ...over });
const sch = (insts: SchInst[], wires: Schematic["wires"] = []): Schematic => ({ insts, wires } as Schematic);
const wire = (x1: number, id = "w"): Schematic["wires"][number] => [x1, 0, x1 + 10, 0, false, "", id];

describe("schDiff", () => {
  it("is empty for identical versions, whatever the order of the properties", () => {
    const a = inst("R1", { props: { value: "1k", W: "1" } }), b = inst("R1", { props: { W: "1", value: "1k" } });
    expect(schDiffEmpty(schDiff(sch([a], [wire(0)]), sch([b], [wire(0)])))).toBe(true);
  });

  it("finds added, removed and modified instances (by id)", () => {
    const d = schDiff(sch([inst("R1", { props: { value: "2k" } }), inst("R2"), inst("R9")]), sch([inst("R1"), inst("R2"), inst("R3")]));
    expect(d.modified.map((i) => i.id)).toEqual(["R1"]);
    expect(d.added.map((i) => i.id)).toEqual(["R9"]);
    expect(d.removed.map((i) => i.id)).toEqual(["R3"]);
  });

  it("counts a move, a flip and another symbol as a change", () => {
    const base = sch([inst("A"), inst("B"), inst("C")]);
    const d = schDiff(sch([inst("A", { x: 10 }), inst("B", { flip: 1 }), inst("C", { symbol: "q.sym" })]), base);
    expect(d.modified.map((i) => i.id)).toEqual(["A", "B", "C"]);
  });

  it("compares wires by their end points, not their ids", () => {
    const d = schDiff(sch([], [wire(0, "x1"), wire(50, "x2")]), sch([], [wire(0, "y9"), wire(80, "y8")]));
    expect(d.wiresAdded.map((w) => w[0])).toEqual([50]);
    expect(d.wiresRemoved.map((w) => w[0])).toEqual([80]);
  });
});
