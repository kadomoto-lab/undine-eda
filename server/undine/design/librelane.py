"""LibreLane as Undine's RTL-to-GDS backend (docs/design/intent.md, "Tool formats").

A **flow configuration** in the project, `flows/<name>.yaml`, says how to build a top cell:

    target: librelane
    top: {lib: digital, cell: counter}
    clock: {port: clk, period_ns: 20}
    floorplan: {core_util: 40}            # or {die_area_um: [0, 0, 120, 120]}
    variables: {PL_TARGET_DENSITY_PCT: 55}  # passed to LibreLane as they are (pdk:: / scl:: overrides included)
    source: schematic                     # optional: the top's gate-level netlist from its schematic, not its RTL

**From a schematic** (`source: schematic`): a top cell drawn from the PDK's standard cells is built from its schematic
as drawn. The run writes the schematic's gate-level Verilog (gatelevel.py) into src/ and LibreLane only elaborates it
(no synthesis), with no resizing, buffering, clock tree or antenna diodes and every instance and net don't-touch
(SCHEMATIC_KEEPS, each in constraints.json with derived_from flow.source; a flow variable overrides one). The supplies
are the schematic's supply ports (the nets on the cells' VPWR / VGND) with the intent's; the side the intent gives a
supply port is not used (the PDN makes its pins) and the check says so ("ignored"). What the layout would leave
unconnected is refused before the run (an input on a supply, an inner net with only inputs: no tie cell is inserted).
When the run ends, its final netlist is compared with the drawn one (summary "netlist": the logical cells it added,
removed or changed; fill, tap, decap and diode cells it added are counted apart), and the signoff violations no repair
removed are listed (summary "signoff": max slew / capacitance / fanout).

A run derives LibreLane's inputs from the flow configuration and the top cell's design intent, and keeps them with the
run: `config.json`, the pin order file from the intent's port sides, and `constraints.json`, which lists every derived
setting with the flow or intent entries it came from (`derived_from`). LibreLane runs in the job's directory; the job
keeps its log, metrics (`final/metrics.json`) and the final GDS, which can be imported into the top cell's library.

**Caravel** (ChipFoundry's user project area): a flow with `caravel: {role: macro}` hardens a cell as a macro for the
user area (power pins vccd1 / vssd1, its PDN without the top-level stripes, routing up to met4); a flow with
`caravel: {role: wrapper, template: <caravel_user_project checkout>, macros: [...]}` hardens user_project_wrapper with
the template's fixed outline, pins, PDN and constraints, and places the macros, each from the latest successful run of
its flow:

    caravel:
      role: wrapper
      template: ~/caravel_user_project       # openlane/user_project_wrapper and verilog/rtl of chipfoundry/caravel_user_project
      macros:
        - {flow: counter, instance: mprj, location: [60, 15], orientation: N}   # power: "vccd1 vssd1" (the default)

The template is not shipped with Undine; the wrapper's RTL (module user_project_wrapper) is the top cell's rtl view.

LibreLane itself comes from Nix (its supported installation): UNDINE_LIBRELANE gives the command (an executable, or a
wrapper ending in `--run`, such as `nix-shell /path/to/librelane --run`); otherwise `librelane` on PATH, otherwise a
checkout in ~/librelane entered with nix-shell.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import yaml

from undine.design import intent, rtl
from undine.store import oplog
from undine import tools
from undine.simulation.sim import Job, Jobs, new_run, stop

FLOWS_DIR = "flows"
NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
RUN_TIMEOUT = 6 * 3600
TAG = "undine"
METRICS = [   # what a designer reads first, when LibreLane reports it (all metrics are kept)
    ("design__instance__count", "cells"), ("design__instance__area", "cell area (µm²)"),
    ("design__die__area", "die area (µm²)"), ("timing__setup__ws", "setup slack (ns)"), ("timing__hold__ws", "hold slack (ns)"),
    ("timing__setup_vio__count", "setup violations"), ("timing__hold_vio__count", "hold violations"),
    ("route__drc_errors", "routing DRC errors"), ("magic__drc_error__count", "Magic DRC errors"),
    ("klayout__drc_error__count", "KLayout DRC errors"), ("design__lvs_error__count", "LVS errors"),
    ("antenna__violating__nets", "antenna violations"), ("design__max_slew_violation__count", "max slew violations"),
    ("design__max_cap_violation__count", "max capacitance violations"),
    ("design__max_fanout_violation__count", "max fanout violations"), ("power__total", "power (W)"),
]


# ------------------------------------------------------------------ flow configurations


def flows_dir(project_path: Path) -> Path:
    return project_path / FLOWS_DIR


def list_flows(project_path: Path) -> list[dict]:
    d = flows_dir(project_path)
    out = []
    for f in sorted(d.glob("*.yaml")) if d.is_dir() else []:
        try:
            out.append({"name": f.stem, **load_flow(project_path, f.stem)})
        except ValueError as e:
            out.append({"name": f.stem, "error": str(e)})
    return out


def load_flow(project_path: Path, name: str) -> dict:
    if not NAME.match(name):
        raise ValueError(f"bad flow name {name!r}")
    f = flows_dir(project_path) / f"{name}.yaml"
    if not f.is_file():
        raise KeyError(f"no flow {name}")
    return normalize_flow(yaml.safe_load(f.read_text()) or {})


SOURCES = ("rtl", "schematic")


def normalize_flow(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("a flow configuration is a mapping (target, top, clock, floorplan, variables)")
    top = raw.get("top") or {}
    clock = raw.get("clock") or {}
    plan = raw.get("floorplan") or {}
    variables = raw.get("variables") or {}
    if raw.get("target", "librelane") != "librelane":
        raise ValueError("target is librelane")
    if not isinstance(top, dict) or not isinstance(clock, dict) or not isinstance(plan, dict) or not isinstance(variables, dict):
        raise ValueError("top, clock, floorplan and variables are mappings")
    out = {"target": "librelane", "top": {k: str(top[k]) for k in ("lib", "cell") if top.get(k)},
           "clock": {}, "floorplan": {}, "variables": dict(variables)}
    if clock.get("port"):
        out["clock"]["port"] = str(clock["port"])
    if clock.get("period_ns") not in (None, ""):
        out["clock"]["period_ns"] = float(clock["period_ns"])
    if plan.get("core_util") not in (None, ""):
        out["floorplan"]["core_util"] = float(plan["core_util"])
    if plan.get("die_area_um"):
        area = [float(v) for v in plan["die_area_um"]]
        if len(area) != 4:
            raise ValueError("die_area_um is [x0, y0, x1, y1]")
        out["floorplan"]["die_area_um"] = area
    source = str(raw.get("source") or "rtl")
    if source not in SOURCES:
        raise ValueError("source is rtl (the top's RTL, synthesized) or schematic (its gate-level netlist as drawn)")
    if source != "rtl":
        out["source"] = source
    if raw.get("caravel"):
        out["caravel"] = _normalize_caravel(raw["caravel"])
        if source == "schematic" and out["caravel"]["role"] == "wrapper":
            raise ValueError("a Caravel wrapper is built from its RTL (source rtl)")
    return out


CARAVEL_ROLES = ("macro", "wrapper")
ORIENTATIONS = ("N", "S", "E", "W", "FN", "FS", "FE", "FW")
WRAPPER = "user_project_wrapper"


def _normalize_caravel(c: dict) -> dict:
    if not isinstance(c, dict) or c.get("role") not in CARAVEL_ROLES:
        raise ValueError("caravel is a mapping with role: macro or wrapper")
    out: dict = {"role": c["role"]}
    if c["role"] == "wrapper":
        out["template"] = str(c.get("template") or "")
        macros = c.get("macros") or []
        if not isinstance(macros, list):
            raise ValueError("caravel.macros is a list of {flow, instance, location, orientation}")
        out["macros"] = []
        for m in macros:
            if not isinstance(m, dict) or not m.get("flow") or not m.get("instance"):
                raise ValueError("each caravel macro names its flow and its instance in the wrapper")
            loc = [float(v) for v in (m.get("location") or [0, 0])]
            if len(loc) != 2:
                raise ValueError("a macro's location is [x, y] in um")
            orient = str(m.get("orientation") or "N").upper()
            if orient not in ORIENTATIONS:
                raise ValueError(f"orientation is one of {', '.join(ORIENTATIONS)}")
            out["macros"].append({"flow": str(m["flow"]), "instance": str(m["instance"]), "location": loc,
                                  "orientation": orient, "power": str(m.get("power") or "vccd1 vssd1")})
    return out


def template_files(template: str) -> dict[str, Path]:
    """The files of a caravel_user_project checkout a wrapper run uses."""
    t = Path(template).expanduser()
    w = t / "openlane" / WRAPPER
    return {"config": w / "config.json", "def": w / "fixed_dont_change" / f"{WRAPPER}.def", "sdc": w / "signoff.sdc",
            "defines": t / "verilog" / "rtl" / "defines.v"}


def latest_run(project_path: Path, flow_name: str) -> Path | None:
    """The newest successful LibreLane run of a flow (its run directory)."""
    runs = project_path / "runs"
    for d in sorted(runs.iterdir(), reverse=True) if runs.is_dir() else []:
        try:
            st = json.loads((d / "status.json").read_text())
        except (OSError, ValueError):
            continue
        if st.get("kind") == "librelane" and st.get("flow") == flow_name and st.get("state") == "done":
            return d
    return None


def macro_views(run: Path) -> dict | None:
    """A hardened macro's views from its run (final/): gds, lef, nl, the powered netlist pnl (what the wrapper's lint
    sees: the macro with its power pins), spef per corner, lib per corner."""
    final = run / "runs" / TAG / "final"
    one = lambda sub, pat: sorted((final / sub).glob(pat))
    gds, lef, nl, pnl = one("gds", "*.gds"), one("lef", "*.lef"), one("nl", "*.nl.v"), one("pnl", "*.pnl.v")
    if not (gds and lef and nl):
        return None
    return {"name": nl[0].name.removesuffix(".nl.v"), "gds": gds[0], "lef": lef[0], "nl": nl[0], "pnl": pnl[0] if pnl else None,
            "spef": {c: f for c in ("min", "nom", "max") for f in one(f"spef/{c}", "*.spef")[:1]},
            "lib": {p.parent.name: p for p in one("lib", "*/*.lib")}}


def save_flow(project_path: Path, name: str, flow: dict) -> dict:
    if not NAME.match(name):
        raise ValueError(f"bad flow name {name!r}")
    model = normalize_flow(flow)
    d = flows_dir(project_path)
    d.mkdir(exist_ok=True)
    (d / f"{name}.yaml").write_text(yaml.safe_dump(model, sort_keys=False, allow_unicode=True))
    return model


def delete_flow(project_path: Path, name: str) -> None:
    if not NAME.match(name):
        raise ValueError(f"bad flow name {name!r}")
    (flows_dir(project_path) / f"{name}.yaml").unlink(missing_ok=True)


def flow_text(flow: dict) -> str:
    """A flow configuration's file text (normalized: what save_flow writes)."""
    return yaml.safe_dump(normalize_flow(flow), sort_keys=False, allow_unicode=True)


class FlowEditor:
    """Writes flow configurations in a changeset (an agent's proposal a person reviews and applies, like any other
    edit) and records each change in the log of the top cell's library (view "flow", cell: the top cell):

        {"k": "r", "do": "mod", "id": "flow:<name>", "file": "flows/<name>.yaml", "changes": {"text": [before, after]}}

    "" stands for no file (a flow added, or deleted). A restore applies the text back (replay), as undo / redo do
    (the editor interface; no route offers them for flows); the file is part of the changeset's commits, so an apply
    brings it into the project, and a batch or a restore that is not kept takes it back (changeset._state)."""

    view_kind = "flow"

    def __init__(self, project_path: Path, cell: str, log=None):
        self.project_path = project_path
        self.cell = cell
        self.log = log
        self.lock = threading.RLock()
        self._meta: dict = {"op": "edit", "args": {}, "actor": "user"}

    @contextmanager
    def recording(self, op: str, args: dict, actor: str = "user", reason: str | None = None,
                  changeset: str | None = None):
        with self.lock:
            self._meta = {"op": op, "args": args, "actor": actor, "reason": reason, "changeset": changeset}
            try:
                yield
            finally:
                self._meta = {"op": "edit", "args": {}, "actor": "user"}

    def _file(self, rel: str) -> Path:
        p = Path(rel)
        if p.parent.as_posix() != FLOWS_DIR or p.suffix != ".yaml" or not NAME.match(p.stem):
            raise ValueError(f"not a flow configuration: {rel}")
        return self.project_path / p

    def save(self, name: str, flow: dict | None) -> bool:
        """Writes flow `name` (None deletes it); False when nothing changed."""
        if not NAME.match(name):
            raise ValueError(f"bad flow name {name!r}")
        rel = f"{FLOWS_DIR}/{name}.yaml"
        with self.lock:
            if self.log is not None:
                self.log.guard()
            f = self._file(rel)
            before = f.read_text() if f.is_file() else ""
            after = flow_text(flow) if flow is not None else ""
            if before == after:
                return False
            self._write(f, after)
            self._append(after, [{"k": "r", "do": "mod", "id": f"flow:{name}", "file": rel,
                                  "changes": {"text": [before, after]}}])
            return True

    @staticmethod
    def _write(f: Path, text: str) -> None:
        if text:
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
        else:
            f.unlink(missing_ok=True)

    def _append(self, text: str, effects: list[dict]) -> None:
        if self.log is not None:
            self.log.append({**self._meta, "cell": self.cell, "view": self.view_kind, "sha": oplog.sha(text),
                             "effects": effects})

    def _apply(self, effects: list[dict], backwards: bool) -> str:
        text = ""
        for e in (reversed(effects) if backwards else effects):
            if e.get("k") != "r":
                continue
            f = self._file(e["file"])
            old, new = e["changes"]["text"]
            if backwards:
                old, new = new, old
            now = f.read_text() if f.is_file() else ""
            if now != old:
                raise ValueError(f"conflict: {e['file']} changed outside this operation")
            self._write(f, new)
            text = new
        return text

    def replay(self, effects: list[dict], meta: dict) -> None:
        with self.lock:
            if self.log is not None:
                self.log.guard()
            text = self._apply(effects, False)
            self._meta = meta
            try:
                self._append(text, effects)
            finally:
                self._meta = {"op": "edit", "args": {}, "actor": "user"}

    def _step(self, backwards: bool) -> bool:
        if self.log is None:
            return False
        self.log.guard()
        done, redo = self.log.stacks(self.cell, self.view_kind)
        rec = (done[-1] if backwards else redo[-1]) if (done if backwards else redo) else None
        if rec is None:
            return False
        text = self._apply(rec["effects"], backwards)
        self.log.append({"op": "undo" if backwards else "redo", "of": rec["seq"], "actor": self._meta.get("actor", "user"),
                         "cell": self.cell, "view": self.view_kind, "sha": oplog.sha(text), "effects": None})
        return True

    def undo(self) -> bool:
        with self.lock:
            return self._step(True)

    def redo(self) -> bool:
        with self.lock:
            return self._step(False)

    def can_undo_redo(self) -> dict:
        if self.log is None:
            return {"undo": False, "redo": False}
        done, redo = self.log.stacks(self.cell, self.view_kind)
        return {"undo": bool(done), "redo": bool(redo)}


# ------------------------------------------------------------------ what a run needs, and what it derives


def design_sources(lib_dir: Path, top: str) -> list[Path]:
    """The top's RTL and the library files of the modules it instantiates (transitively), the top's file first.
    Testbenches and other tops (a Caravel wrapper beside its macro) stay out."""
    files = [f for f in rtl.sources(lib_dir) if not re.match(r"^(tb_.*|.*_tb)$", f.stem)]
    defines = {mod: f for f in files for mod in rtl.modules(f.read_text(errors="replace"))}
    first = defines.get(top) or next((f for f in files if f.stem == top), None)
    if first is None:
        return sorted(files, key=lambda f: (f.stem != top, f.name))
    keep, todo = [first], [first]
    while todo:
        text = rtl._COMMENTS.sub(" ", todo.pop().read_text(errors="replace"))
        for mod, f in defines.items():
            if f not in keep and re.search(rf"\b{re.escape(mod)}\s*(#\s*\(.*?\)\s*)?[A-Za-z_]\w*\s*\(", text, re.S):
                keep.append(f)
                todo.append(f)
    return [first] + sorted(keep[1:], key=lambda f: f.name)


def gate_netlist(prj, flow: dict, model: dict | None) -> dict | None:
    """The gate-level netlist a flow with source schematic builds from (gatelevel.of_cell: the export, or {"error"});
    None for a flow built from RTL."""
    top = flow.get("top") or {}
    if flow.get("source") != "schematic" or not top.get("lib") or not top.get("cell"):
        return None
    from undine.design import gatelevel
    return gatelevel.of_cell(prj, top["lib"], top["cell"], intent.normalize(model or {})["supplies"])


def requirements(flow: dict, lib_dir: Path | None, model: dict | None, project_path: Path | None = None,
                 gate: dict | None = None) -> list[dict]:
    """What the flow configuration and the top's intent are missing for a LibreLane run: [{entry, message}]. `gate`:
    gate_netlist's answer for a flow with source schematic."""
    need = []
    top = flow.get("top") or {}
    if not top.get("lib") or not top.get("cell"):
        return [{"entry": "flow.top", "message": "the top cell (library and cell) to build"}]
    car = flow.get("caravel") or {}
    if flow.get("source") == "schematic":
        if gate is None or gate.get("error"):
            return [{"entry": "flow.source", "message": (gate or {}).get("error")
                     or f"the gate-level netlist of {top['lib']}/{top['cell']}'s schematic"}]
        power = set(gate["power_ports"])
        if car.get("role") == "macro" and not {"vccd1", "vssd1"} <= power:
            need.append({"entry": "flow.caravel.role", "message": f"the power ports vccd1 and vssd1 of {top['cell']}'s "
                         "schematic (its standard cells' supplies), which the wrapper's PDN connects"})
        return need + _gate_requirements(top["cell"], gate) + \
            _port_requirements(flow, [n for n, _ in gate["ports"]], model, power)
    src = rtl.find_file(lib_dir, top["cell"]) if lib_dir else None
    if src is None:
        return [{"entry": "flow.top", "message": f"RTL for {top['lib']}/{top['cell']} (the top cell has no rtl view; "
                 "a cell drawn from standard cells is built from its schematic with source: schematic)"}]
    text = src.read_text(errors="replace")
    if car.get("role") == "wrapper":
        return _wrapper_requirements(flow, text, project_path)
    if car.get("role") == "macro" and not all(re.search(rf"\b{n}\b", text) for n in ("vccd1", "vssd1")):
        need.append({"entry": "flow.caravel.role", "message": f"the power ports vccd1 and vssd1 of {top['cell']} (inside "
                     "`ifdef USE_POWER_PINS), which the wrapper's PDN connects"})
    return need + _port_requirements(flow, [n for n, _ in rtl.module_ports(text, top["cell"])], model)


def _gate_requirements(cell: str, gate: dict) -> list[dict]:
    """What a schematic's gate-level netlist needs before a run takes it as it is: LibreLane only elaborates it (no
    tie cells inserted, SYNTH_ELABORATE_ONLY), so an input on a supply or on a net nothing drives would be left
    unconnected in the layout; the supplies on the cells' supply pins are ports (the PDN's pins, VDD_NETS /
    GND_NETS)."""
    need = []
    tie = gate.get("tie_cell") or "the PDK's tie cell"
    for c in gate.get("constants") or []:
        need.append({"entry": "flow.source", "message": f"{c['pin']} is on the {gate['supplies'].get(c['net'], 'supply')} "
                     f"{c['net']}: a run from the schematic inserts no tie cell, so the layout would leave it "
                     f"unconnected; draw a tie cell ({tie}) and take the constant from it"})
    for u in gate.get("undriven") or []:
        need.append({"entry": "flow.source", "message": f"net {u['net']} has no driver (only inputs: "
                     f"{', '.join(u['pins'])}): draw what drives it, or a tie cell ({tie}) for a constant"})
    power = set(gate.get("power_ports") or [])
    inner = [n for n in gate.get("supply_nets") or [] if n not in power]
    if inner:
        need.append({"entry": "flow.source", "message": f"a port for each supply of {cell}'s standard cells "
                     f"({', '.join(inner)}: on their supply pins, not a port of the schematic); LibreLane's PDN makes "
                     "the macro's supply pins from them"})
    roles = {(gate.get("supplies") or {}).get(n) for n in power}
    for role, pins in (("supply", "VPWR"), ("ground", "VGND")):
        if gate.get("supply_nets") and not inner and role not in roles:
            need.append({"entry": "flow.source", "message": f"a {role} port of {cell}'s schematic (the net on the "
                         f"cells' {pins})"})
    return need


def _port_requirements(flow: dict, ports: list[str], model: dict | None, power: set[str] = frozenset()) -> list[dict]:
    """The clock and the port sides a run needs, for the top's ports `ports` (its supplies left out: the PDN)."""
    need = []
    top = flow["top"]
    car = flow.get("caravel") or {}
    clock = flow.get("clock") or {}
    if not clock.get("port"):
        need.append({"entry": "flow.clock.port", "message": "the clock port (or none for a combinational design)"})
    elif clock["port"] != "none" and clock["port"] not in ports:
        need.append({"entry": "flow.clock.port", "message": f"{top['cell']} has no port {clock['port']}"})
    if clock.get("port") not in (None, "none") and not clock.get("period_ns"):
        need.append({"entry": "flow.clock.period_ns", "message": "the clock period (ns)"})
    m = intent.normalize(model or {})
    power = set(power) | set(m["supplies"]) | ({"vccd1", "vssd1"} if car.get("role") == "macro" else set())   # the PDN
    for p in ports:
        if p in power:
            continue
        if p not in m["ports"] or not (m["ports"][p] or {}).get("side"):
            need.append({"entry": f"ports.{p}", "message": f"the side of port {p} (or auto to let LibreLane place it)"})
    return need


def _wrapper_requirements(flow: dict, text: str, project_path: Path | None) -> list[dict]:
    need = []
    car = flow["caravel"]
    if flow["top"]["cell"] != WRAPPER:
        need.append({"entry": "flow.top", "message": f"the wrapper cell is named {WRAPPER}"})
    if not car.get("template"):
        need.append({"entry": "flow.caravel.template", "message": "the path of a caravel_user_project checkout (template)"})
    else:
        for k, f in template_files(car["template"]).items():
            if not f.is_file():
                need.append({"entry": "flow.caravel.template", "message": f"{f} (the template's {k})"})
    if not car.get("macros"):
        need.append({"entry": "flow.caravel.macros", "message": "the macros to place (flow, instance, location)"})
    for m in car.get("macros") or []:
        run = latest_run(project_path, m["flow"]) if project_path else None
        views = macro_views(run) if run else None
        if views is None:
            need.append({"entry": f"flow.caravel.macros.{m['instance']}",
                         "message": f"a successful run of flow {m['flow']} (the macro {m['instance']}): run it first"})
        elif not re.search(rf"\b{re.escape(views['name'])}\s+(#\s*\(.*?\)\s*)?{re.escape(m['instance'])}\b", text, re.S):
            need.append({"entry": f"flow.caravel.macros.{m['instance']}",
                         "message": f"an instance {m['instance']} of {views['name']} in the wrapper's RTL"})
    return need


# a run from a schematic keeps its netlist as drawn: LibreLane elaborates it (no synthesis) and changes no cell of it
SCHEMATIC_KEEPS = {
    "SYNTH_ELABORATE_ONLY": True,                    # Yosys reads the netlist, keeps every instance (keep), maps nothing
    "RSZ_DONT_TOUCH_RX": ".*",                       # every net and instance don't-touch for the resizer
    "RUN_POST_GPL_DESIGN_REPAIR": False,             # no buffering of long nets or ports, no resizing
    "RUN_POST_GRT_DESIGN_REPAIR": False,
    "RUN_CTS": False,                                # no clock tree: the clocks are wired as drawn
    "RUN_POST_CTS_RESIZER_TIMING": False,            # no timing repair (it would insert buffers)
    "RUN_POST_GRT_RESIZER_TIMING": False,
    "DIODE_ON_PORTS": "none",                        # no antenna diodes anywhere
    "RUN_HEURISTIC_DIODE_INSERTION": False,
    "RUN_ANTENNA_REPAIR": False,
    "DRT_ANTENNA_REPAIR_JUMPER_ONLY": True,          # detailed routing fixes antennas by layer jumps only
}


def derive(flow: dict, lib_dir: Path, model: dict | None, pdk: str, project_path: Path | None = None,
           gate: dict | None = None) -> dict:
    """LibreLane's inputs from the flow configuration and the intent, each setting with what it came from. `gate`:
    gate_netlist's export for a flow with source schematic (its text becomes src/<top>.v)."""
    if (flow.get("caravel") or {}).get("role") == "wrapper":
        return _derive_wrapper(flow, lib_dir, pdk, project_path)
    top = flow["top"]["cell"]
    m = intent.normalize(model or {})
    derived: list[dict] = []

    def put(key, value, *sources):
        derived.append({"variable": key, "value": value, "derived_from": list(sources)})
        return value

    generated: dict[str, str] = {}
    name = put("DESIGN_NAME", top, "flow.top")
    if flow.get("source") == "schematic":
        if not gate or gate.get("error"):
            raise ValueError((gate or {}).get("error") or "the schematic's gate-level netlist is missing")
        srcs = []
        generated[f"{top}.v"] = gate["text"]
        files = put("VERILOG_FILES", [f"dir::src/{top}.v"], "flow.top", "flow.source", "schematic")
    else:
        srcs = design_sources(lib_dir, top)
        files = put("VERILOG_FILES", [f"dir::src/{f.name}" for f in srcs], "flow.top", "rtl")
    cfg: dict = {"DESIGN_NAME": name, "VERILOG_FILES": files,
                 "PDK": put("PDK", pdk, "project.pdk")}
    clock = flow.get("clock") or {}
    if clock.get("port") and clock["port"] != "none":
        cfg["CLOCK_PORT"] = put("CLOCK_PORT", clock["port"], "flow.clock.port")
        cfg["CLOCK_PERIOD"] = put("CLOCK_PERIOD", clock["period_ns"], "flow.clock.period_ns")
    else:
        cfg["CLOCK_PORT"] = put("CLOCK_PORT", None, "flow.clock.port")
    plan = flow.get("floorplan") or {}
    if plan.get("die_area_um"):
        cfg["FP_SIZING"] = put("FP_SIZING", "absolute", "flow.floorplan.die_area_um")
        cfg["DIE_AREA"] = put("DIE_AREA", plan["die_area_um"], "flow.floorplan.die_area_um")
    elif plan.get("core_util"):
        cfg["FP_CORE_UTIL"] = put("FP_CORE_UTIL", plan["core_util"], "flow.floorplan.core_util")
    # the supplies: the intent's, and for a run from a schematic its supply ports (the nets on the cells' VPWR / VGND)
    roles = dict(m["supplies"])
    froms = {n: [f"supplies.{n}"] for n in roles}
    for n in (gate or {}).get("power_ports") or []:
        if n not in roles and (r := (gate.get("supplies") or {}).get(n)):
            roles[n], froms[n] = r, ["flow.source", f"schematic.{n}"]
    supplies = [n for n, r in roles.items() if r == "supply"]
    grounds = [n for n, r in roles.items() if r == "ground"]
    if supplies:
        cfg["VDD_NETS"] = put("VDD_NETS", supplies, *[f for n in supplies for f in froms[n]])
    if grounds:
        cfg["GND_NETS"] = put("GND_NETS", grounds, *[f for n in grounds for f in froms[n]])
    sides: dict[str, list[str]] = {"N": [], "S": [], "E": [], "W": []}
    power = set(roles)                              # the PDN makes their pins, not the IO placer
    ignored = []
    for name, p in m["ports"].items():
        if not (isinstance(p, dict) and p.get("side") in sides):
            continue
        if name in power:
            ignored.append({"entry": f"ports.{name}.side", "value": p["side"],
                            "message": f"the side {p['side']} of supply port {name} is not used: LibreLane's PDN makes "
                                       "the supply pins (on its straps across the core, not on one edge); the pin order "
                                       "places the signal pins only"})
            continue
        sides[p["side"]].append(name)
    pin_cfg = ""
    if any(sides.values()):
        lines = []
        for side in ("N", "S", "E", "W"):
            if sides[side]:
                lines.append(f"#{side}")
                lines += [f"{re.escape(n)}(\\[\\d+\\])?" for n in sides[side]]   # a bus: all its bits
        pin_cfg = "\n".join(lines) + "\n"
        cfg["IO_PIN_ORDER_CFG"] = put("IO_PIN_ORDER_CFG", "dir::pin_order.cfg",
                                      *[f"ports.{n}" for s in sides.values() for n in s])
    if (flow.get("caravel") or {}).get("role") == "macro":
        # caravel_user_project's macros (openlane/user_proj_example): the wrapper's PDN reaches them on met4 / met5
        cfg["VDD_NETS"] = put("VDD_NETS", ["vccd1"], "flow.caravel.role")
        cfg["GND_NETS"] = put("GND_NETS", ["vssd1"], "flow.caravel.role")
        cfg["FP_PDN_MULTILAYER"] = put("FP_PDN_MULTILAYER", False, "flow.caravel.role")
        cfg["RT_MAX_LAYER"] = put("RT_MAX_LAYER", "met4", "flow.caravel.role")
    if generated:
        for k, v in SCHEMATIC_KEEPS.items():
            cfg[k] = put(k, v, "flow.source")
    for k, v in (flow.get("variables") or {}).items():
        cfg[k] = put(k, v, f"flow.variables.{k}")
    return {"config": cfg, "pin_order": pin_cfg, "sources": srcs, "derived": derived, "copies": [],
            "generated": generated, "ignored": ignored}


def _derive_wrapper(flow: dict, lib_dir: Path, pdk: str, project_path: Path | None) -> dict:
    """user_project_wrapper: the template's configuration (outline, pins, PDN, constraints) with the wrapper's RTL and
    the macros from their flows' runs. The macros' views are copied into the run (macros/<flow>/), so it keeps what it
    was built from."""
    car = flow["caravel"]
    files = template_files(car["template"])
    derived: list[dict] = []

    def put(key, value, *sources):
        derived.append({"variable": key, "value": value, "derived_from": list(sources)})
        return value

    base = {k: v for k, v in json.loads(files["config"].read_text()).items() if k != "//"}
    cfg: dict = {}
    for k, v in base.items():
        if k not in ("VERILOG_FILES", "MACROS", "PDN_MACRO_CONNECTIONS", "FP_DEF_TEMPLATE", "SIGNOFF_SDC_FILE", "PNR_SDC_FILE"):
            cfg[k] = put(k, v, "caravel.template")
    src = rtl.find_file(lib_dir, WRAPPER)
    cfg["DESIGN_NAME"] = put("DESIGN_NAME", WRAPPER, "flow.top")
    cfg["PDK"] = put("PDK", pdk, "project.pdk")
    cfg["VERILOG_FILES"] = put("VERILOG_FILES", [str(files["defines"]), f"dir::src/{src.name}"], "caravel.template", "rtl")
    cfg["FP_DEF_TEMPLATE"] = put("FP_DEF_TEMPLATE", str(files["def"]), "caravel.template")
    cfg["SIGNOFF_SDC_FILE"] = put("SIGNOFF_SDC_FILE", str(files["sdc"]), "caravel.template")
    cfg["PNR_SDC_FILE"] = put("PNR_SDC_FILE", str(files["sdc"]), "caravel.template")
    macros, conns, copies = {}, [], []
    for m in car["macros"]:
        views = macro_views(latest_run(project_path, m["flow"]))
        dest = f"macros/{m['flow']}"
        rel = lambda p, sub="": f"dir::{dest}/{sub}{p.name}"
        copies += [(views[k], f"{dest}/{views[k].name}") for k in ("gds", "lef", "nl", "pnl") if views[k]]
        copies += [(p, f"{dest}/spef_{c}_{p.name}") for c, p in views["spef"].items()]
        copies += [(p, f"{dest}/lib_{c}_{p.name}") for c, p in views["lib"].items()]
        entry = macros.setdefault(views["name"], {
            "gds": [rel(views["gds"])], "lef": [rel(views["lef"])], "nl": [rel(views["nl"])],
            **({"pnl": [rel(views["pnl"])]} if views["pnl"] else {}),
            "spef": {f"{c}_*": [f"dir::{dest}/spef_{c}_{p.name}"] for c, p in views["spef"].items()},
            "instances": {}})
        typ = next((c for c in views["lib"] if c.startswith("nom_tt")), next(iter(views["lib"]), None))
        if typ:                                        # one timing view for every corner, as the template's example does
            entry["lib"] = {"*": f"dir::{dest}/lib_{typ}_{views['lib'][typ].name}"}
        entry["instances"][m["instance"]] = {"location": m["location"], "orientation": m["orientation"]}
        # "<instance> <supply net> <ground net> <supply pin> <ground pin>": the macro's pins carry the nets' names
        nets = m["power"].split()
        conns.append(" ".join([m["instance"], *(nets * 2 if len(nets) == 2 else nets)]))
    # the wrapper has no rows and no taps: detailed routing's antenna repair must not put diode cells in it (LibreLane 3
    # does by default; they show up as untapped nwells in Magic DRC and as extra devices in LVS), jumpers it may
    cfg["DRT_ANTENNA_REPAIR_JUMPER_ONLY"] = put("DRT_ANTENNA_REPAIR_JUMPER_ONLY", True, "flow.caravel.role")
    cfg["MACROS"] = put("MACROS", macros, *[f"flow.caravel.macros.{m['instance']}" for m in car["macros"]])
    cfg["PDN_MACRO_CONNECTIONS"] = put("PDN_MACRO_CONNECTIONS", conns, *[f"flow.caravel.macros.{m['instance']}" for m in car["macros"]])
    for k, v in (flow.get("variables") or {}).items():
        cfg[k] = put(k, v, f"flow.variables.{k}")
    return {"config": cfg, "pin_order": "", "sources": [src], "derived": derived, "copies": copies, "ignored": []}


# ------------------------------------------------------------------ running LibreLane


def command(args: list[str]) -> list[str]:
    """The command line that runs `librelane <args>`, by how LibreLane is installed here."""
    configured = os.environ.get("UNDINE_LIBRELANE", "").strip()
    if configured:
        parts = shlex.split(configured)
        if parts[-1] == "--run":                                  # a wrapper such as nix-shell <dir> --run
            return parts + [shlex.join(["librelane", *args])]
        return parts + args
    exe = tools.find("librelane")
    if exe:
        return [exe, *args]
    checkout = Path.home() / "librelane"
    nix_shell = shutil.which("nix-shell") or next((p for p in (f"{NIX_BIN}/nix-shell",) if Path(p).is_file()), None)
    if (checkout / "shell.nix").is_file() and nix_shell:
        return [nix_shell, str(checkout), "--run", shlex.join(["librelane", *args])]
    raise RuntimeError("LibreLane is not available: install Nix and clone LibreLane to ~/librelane (see the RTL guide), "
                       "or set UNDINE_LIBRELANE to its command")


NIX_BIN = "/nix/var/nix/profiles/default/bin"


def clean_env() -> dict[str, str]:
    """The environment LibreLane runs in. Its tools come from Nix; none of Undine's own environment may leak in: a
    conda / pixi LD_LIBRARY_PATH makes OpenROAD load the wrong libraries (libGL.so.1 not found), and their bin
    directories would shadow Nix's tools."""
    pre = tools.tools_prefix()
    ours = {str(Path(sys.prefix) / "bin"), *([str(pre / "bin")] if pre else [])}
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("LD_", "CONDA", "PIXI_", "PYTHON", "UNDINE_TOOLS")) and k not in ("SPICE_LIB_DIR", "QT_QPA_PLATFORM")}
    path = [p for p in os.environ.get("PATH", "").split(":") if p and p not in ours and "/.pixi/envs/" not in p]
    if Path(NIX_BIN).is_dir() and NIX_BIN not in path:
        path.insert(0, NIX_BIN)
    env["PATH"] = ":".join(path)
    return env


def available() -> dict:
    try:
        cmd = command(["--version"])
        return {"available": True, "command": shlex.join(cmd[:3]) + (" …" if len(cmd) > 3 else "")}
    except RuntimeError as e:
        return {"available": False, "reason": str(e).removeprefix("LibreLane is not available: ")}


def submit(jobs: Jobs, project_path: Path, lib_dir: Path, flow_name: str, flow: dict, model: dict | None,
           pdk_root: Path, pdk: str, meta: dict | None = None, gate: dict | None = None) -> Job:
    missing = requirements(flow, lib_dir, model, project_path, gate)
    if missing:
        raise ValueError("the flow is missing: " + "; ".join(m["message"] for m in missing))
    d = derive(flow, lib_dir, model, pdk, project_path, gate)
    if lib_dir is not None:
        # LibreLane's Verilator lint and Yosys read the top library's RTL ($readmem, `include): a dependency's only
        # once a person trusts it. Refused before anything is copied or started.
        from undine.store import trust
        trust.check_rtl(lib_dir, full=True, texts=d.get("generated"))
    cmd = command(["--run-tag", TAG, "--manual-pdk", "--pdk-root", str(pdk_root), "--pdk", pdk, "config.json"])
    jobs.ensure_open()                          # refused once the server is stopping (sim.STOPPING)
    jid, run = new_run(jobs.runs_dir(project_path))
    (run / "src").mkdir()
    for f in d["sources"]:
        shutil.copy2(f, run / "src" / f.name)
    for name, text in d.get("generated", {}).items():               # the schematic's netlist, as the run took it
        (run / "src" / name).write_text(text)
    for f, rel in d.get("copies", []):
        (run / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, run / rel)
    (run / "config.json").write_text(json.dumps(d["config"], indent=2))
    if d["pin_order"]:
        (run / "pin_order.cfg").write_text(d["pin_order"])
    (run / "constraints.json").write_text(json.dumps(d["derived"], indent=2))
    job = Job(run)
    job.set(id=jid, kind="librelane", title=f"LibreLane {flow['top']['lib']}/{flow['top']['cell']}", state="queued",
            created=time.time(), flow=flow_name, lib=flow["top"]["lib"], cell=flow["top"]["cell"], command=shlex.join(cmd),
            source=flow.get("source", "rtl"), **({"ignored": [i["message"] for i in d["ignored"]]} if d.get("ignored") else {}),
            **({"sdc": False} if not any(k in d["config"] for k in SDC_VARIABLES) else {}),
            **jobs.here(), **(meta or {}))
    try:
        jobs.track(job)                                         # refused once the server is stopping (sim.STOPPING)
    except RuntimeError:
        shutil.rmtree(run, ignore_errors=True)
        raise
    threading.Thread(target=_run, args=(jobs, job, cmd), daemon=True).start()
    return job


def _run(jobs: Jobs, job: Job, cmd: list[str]) -> None:
    t0 = time.time()
    try:
        if not jobs.record(job, state="running", started=t0):
            return
        with (job.path / "log.txt").open("w") as out:
            proc = jobs.step(job, cmd, cwd=job.path, env=clean_env(), stdout=out, stderr=subprocess.STDOUT)
            if proc is None:
                return
            rc = proc.wait(timeout=RUN_TIMEOUT)
        if job.cancelled:
            return
        res = results(job.path)
        errors = run_errors(job)
        ok = rc == 0 and res.get("gds")
        jobs.record(job, state="done" if ok else "failed", rc=rc, finished=time.time(), seconds=time.time() - t0,
                    summary=summary(job.path, job.status(), res, errors),
                    **({} if ok else {"error": errors[-1] if errors else f"exit code {rc}"}))
    except subprocess.TimeoutExpired:
        if job.proc:
            stop(job.proc)                      # with the tools it started: nothing writes on once it has failed
        jobs.record(job, state="failed", error="timed out", timed_out=True, finished=time.time())
    except Exception as e:
        jobs.record(job, state="failed", error=str(e), finished=time.time())
    finally:
        jobs.untrack(job)


HINTS = [   # what a LibreLane error usually means for the flow configuration
    (r"PDN-0185|Insufficient width .* to add straps",
     "the die is too small for the power straps: set a die area in the flow (e.g. 100 × 100 µm) instead of a utilisation"),
    (r"GPL-0302|utilization .* exceeds|overflow",
     "the cells do not fit: lower the core utilisation or enlarge the die"),
    (r"setup violations|timing__setup_vio|Setup violations",
     "setup timing is not met: lengthen the clock period, or simplify the logic between registers"),
    (r"PPL-00(24|90)|Number of IO pins .* exceeds",
     "the pins do not fit on the die edge: enlarge the die or put fewer ports on one side"),
]


def hints(errors: list[str]) -> list[str]:
    return list(dict.fromkeys(h for pat, h in HINTS if any(re.search(pat, e) for e in errors)))


SDC_VARIABLES = ("PNR_SDC_FILE", "SIGNOFF_SDC_FILE")
SIGNOFF = [   # the signoff checks a run passes with violations (LibreLane warns, the run goes on)
    ("design__max_slew_violation__count", "max slew"), ("design__max_cap_violation__count", "max capacitance"),
    ("design__max_fanout_violation__count", "max fanout"),
]


def summary(run: Path, status: dict, res: dict, errors: list[str]) -> dict:
    """A finished run's summary: its key metrics, GDS, errors with hints, the signoff violations it ended with
    (signoff: what to do about them), and for a run from a schematic what it did to the drawn netlist."""
    metrics = res.get("metrics") or {}
    out: dict = {"metrics": res.get("key_metrics", {}), "gds": res.get("gds"), "errors": errors, "hints": hints(errors)}
    found = {label: metrics[k] for k, label in SIGNOFF if isinstance(metrics.get(k), (int, float)) and metrics[k] > 0}
    schematic = status.get("source") == "schematic"
    if found:
        worst = {label: sorted(c.split("corner:")[1] for c, v in metrics.items()
                               if c.startswith(k + "__corner:") and isinstance(v, (int, float)) and v > 0)
                 for k, label in SIGNOFF if label in found}
        out["signoff"] = {"violations": found, "corners": {k: v for k, v in worst.items() if v}}
        out["hints"].append(
            f"the run ends with {', '.join(f'{v} {k}' for k, v in found.items())} violations"
            + (": the drawn netlist is kept as it is (no buffering, no resizing), so they are the schematic's to fix "
               "(a stronger driver, a buffer drawn for a net of many loads)" if schematic else
               ": LibreLane's repair did not remove them (see the corners in signoff)"))
    if schematic and status.get("sdc") is False and metrics.get("timing__setup__ws") is not None:
        out["hints"].append("timing was checked with LibreLane's generic SDC (the clock on CLOCK_PORT, default input "
                            "and output delays): for a self-timed or asynchronous design its slack says little; give "
                            "the flow an SDC of the design (variables PNR_SDC_FILE, SIGNOFF_SDC_FILE)")
    if status.get("ignored"):
        out["ignored"] = status["ignored"]
    drawn = netlist_kept(run, status)
    if drawn:
        out["netlist"] = drawn
    return out


def run_errors(job: Job) -> list[str]:
    """LibreLane's errors: its error.log (one entry per error, e.g. "[PDN-0185] Insufficient width ..."), else the
    ERROR lines of the run's log."""
    f = job.path / "runs" / TAG / "error.log"
    if f.is_file() and f.read_text(errors="replace").strip():
        return [" ".join(e.split()) for e in re.split(r"\n(?=\S)", f.read_text(errors="replace").strip())][-10:]
    return [l.strip() for l in job.log(400).splitlines() if re.search(r"\[(ERROR|CRITICAL)\]|\]\s+ERROR\s|^\s*(ERROR|Error):", l)][-10:]


def netlist_kept(run: Path, status: dict) -> dict | None:
    """For a run from a schematic: what it did to the drawn netlist (gatelevel.compare of src/<top>.v with the final
    netlist); None for a run from RTL or one without a final netlist."""
    if status.get("source") != "schematic":
        return None
    from undine.design import gatelevel
    drawn = run / "src" / f"{status.get('cell')}.v"
    final = sorted((run / "runs" / TAG / "final" / "nl").glob("*.nl.v"))
    if not drawn.is_file() or not final:
        return None
    out = gatelevel.compare(gatelevel.instances(drawn.read_text()), gatelevel.instances(final[0].read_text()))
    if not out["kept"]:
        out["note"] = ("LibreLane changed the drawn netlist: set the flow variables that stop it (the derived "
                       "SCHEMATIC_KEEPS are in constraints.json); fill, tap, decap and diode cells are counted apart")
    return out


def results(run: Path) -> dict:
    """The final metrics and GDS of a LibreLane run (runs/<tag>/final)."""
    final = run / "runs" / TAG / "final"
    out: dict = {"run": str(final.relative_to(run)) if final.is_dir() else None}
    mf = final / "metrics.json"
    if mf.is_file():
        metrics = json.loads(mf.read_text())
        out["metrics"] = metrics
        out["key_metrics"] = {label: metrics[k] for k, label in METRICS if k in metrics}
    gds = sorted((final / "gds").glob("*.gds")) if (final / "gds").is_dir() else []
    out["gds"] = gds[0].relative_to(run).as_posix() if gds else None
    return out
