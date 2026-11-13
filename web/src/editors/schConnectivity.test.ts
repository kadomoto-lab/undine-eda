import { describe, expect, it } from "vitest";
import type { Schematic } from "../api";
import { drawingConnectivity, pointKey } from "./schConnectivity";

function drawing(segments: number[][], pins: Schematic["pins"] = [], terminals?: Schematic["terminals"]) {
  return { wires: segments.map(([a, b, c, d]) => [a, b, c, d, false, "", `${a},${b},${c},${d}`] as Schematic["wires"][number]), pins, terminals };
}
describe("schematic connection markers", () => {
  it("marks a T branch meeting the middle of a wire", () => {
    expect(drawingConnectivity(drawing([[0, 0, 100, 0], [50, 0, 50, 50]])).junctions).toEqual([[50, 0]]);
  });
  it("does not mark a crossing without a connecting endpoint", () => {
    expect(drawingConnectivity(drawing([[0, 0, 100, 0], [50, -50, 50, 50]])).junctions).toEqual([]);
  });
  it("marks an explicitly joined crossing", () => {
    expect(drawingConnectivity(drawing([[0, 0, 100, 0], [50, -50, 50, 0], [50, 0, 50, 50]])).junctions).toEqual([[50, 0]]);
  });
  it("does not report a pin touching a wire interior as dangling", () => {
    const result = drawingConnectivity(drawing([[0, 0, 100, 0]], [["R1", "P", 50, 0, "net1"], ["R1", "M", 50, 30, "net2"]]));
    expect([...result.connectedPins]).toEqual([pointKey(50, 0)]);
  });
  it("recognizes direct pin-to-pin and device-to-port connections", () => {
    const result = drawingConnectivity(drawing([], [["R1", "P", 0, 0, "net1"]], [["R1", "P", 0, 0], ["IN", "p", 0, 0]]));
    expect(result.connectedPins.has(pointKey(0, 0))).toBe(true);
  });
  it("does not invent junctions from duplicate or zero-length segments", () => {
    const d = drawing([[0, 0, 50, 0], [0, 0, 50, 0], [50, 0, 0, 0], [0, 0, 0, 0]]);
    expect(drawingConnectivity(d).junctions).toEqual([]);
    expect(drawingConnectivity(d)).toBe(drawingConnectivity(d));
  });
});
