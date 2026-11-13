// Which cell view an assistant tool call changed: what the drawing follows while a turn runs.
import type { ViewKind } from "../api";

export interface WorkTarget { lib: string; cell: string; view: ViewKind }

const LAYOUT_TOOLS = new Set(["add_box", "add_path", "add_label", "move_instance", "delete_shapes", "sync_layout", "place_cell",
  "route", "add_via", "add_tap", "guard_ring", "swap_instances"]);
/** The cell view a tool call changed, from its name and arguments. */
export function workTarget(name: string, input: Record<string, unknown> | undefined): WorkTarget | null {
  const lib = input?.lib, cell = input?.cell;
  if (typeof lib !== "string" || typeof cell !== "string") return null;
  if (name === "delete_cell") return null;          // a cell taken out: no view of it to follow
  if (name === "move_instance" && input?.view === "schematic") return { lib, cell, view: "schematic" };
  if (LAYOUT_TOOLS.has(name)) return { lib, cell, view: "layout" };
  if (name === "create_cell") {
    const views = (input?.views as string[] | undefined) ?? ["schematic"];
    return { lib, cell, view: (views.includes("schematic") ? "schematic" : views[0]) as ViewKind };
  }
  return { lib, cell, view: "schematic" };
}

