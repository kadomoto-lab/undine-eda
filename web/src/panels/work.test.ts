import { describe, expect, it } from "vitest";
import { workTarget } from "./work";

describe("workTarget", () => {
  it("maps tool calls to the view they change", () => {
    expect(workTarget("add_instance", { lib: "l", cell: "ota" })).toEqual({ lib: "l", cell: "ota", view: "schematic" });
    expect(workTarget("add_path", { lib: "l", cell: "ota" })).toEqual({ lib: "l", cell: "ota", view: "layout" });
    expect(workTarget("sync_layout", { lib: "l", cell: "ota" })?.view).toBe("layout");
    expect(workTarget("place_cell", { lib: "l", cell: "sar", child: "sky130_fd_sc_hd__inv_1" })?.view).toBe("layout");
    for (const tool of ["route", "add_via", "add_tap", "guard_ring", "swap_instances"]) expect(workTarget(tool, { lib: "l", cell: "sar_bootsw" })?.view).toBe("layout");
    expect(workTarget("move_instance", { lib: "l", cell: "ota", instance: "M1" })?.view).toBe("layout");
    expect(workTarget("move_instance", { lib: "l", cell: "ota", instance: "M1", view: "schematic" })?.view).toBe("schematic");
    expect(workTarget("create_cell", { lib: "l", cell: "ota" })?.view).toBe("schematic");
    expect(workTarget("create_cell", { lib: "l", cell: "ota", views: ["layout"] })?.view).toBe("layout");
    expect(workTarget("create_cell", { lib: "l", cell: "ota", views: ["layout", "schematic"] })?.view).toBe("schematic");
  });
  it("has no target without lib and cell", () => {
    expect(workTarget("open_changeset", { title: "x" })).toBeNull();
    expect(workTarget("run_checks", undefined)).toBeNull();
    expect(workTarget("import_layout", { lib: "l", path: "/x/cdac.gds" })).toBeNull();
    expect(workTarget("delete_cell", { lib: "l", cell: "pilot" })).toBeNull();      // nothing of it left to follow
  });
});
