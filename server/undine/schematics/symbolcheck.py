"""Compare a cell's schematic interface with its symbol, as a persistent verification job.

The order is the order Undine actually netlists: ipin/opin/iopin components in the
schematic, and layer-5 boxes in the symbol. A pin's ``pinnumber`` property does not
override that order. Jobs compare captured inputs, never files read later by a worker.
"""
from __future__ import annotations

import json
import shutil
import threading
import time
from collections import Counter
from pathlib import Path

from undine.store import provenance
from undine.schematics import schematic, xschem
from undine.store.project import check_name
from undine.simulation.sim import Job, Jobs, new_run

VERSION = 1
_DIRECTIONS = {"ipin": "in", "opin": "out", "iopin": "inout"}


def _schematic(path: Path, resolver: xschem.SymbolResolver) -> tuple[dict, dict[str, str]]:
    if not path.is_file():
        return {"exists": False, "ports": [], "unresolved_symbols": []}, {}
    text = path.read_text(errors="replace")
    model = xschem.parse(text)
    ports, unresolved, definitions = [], [], {}
    for instance, comp in zip(xschem.comp_ids(model), model.comps):
        sym = schematic.resolve_symbol(resolver, comp, path.parent)
        if sym is None:
            # An unresolved symbol might itself be a port; do not report a false pass.
            unresolved.append({"instance": instance, "symbol": comp.sym})
            continue
        typ = sym.global_props.get("type", "")
        if typ in schematic.PORT_TYPES:
            props = schematic.inst_props(comp, sym)
            ports.append({"name": props.get("lab", ""), "direction": _DIRECTIONS[typ],
                          "index": len(ports) + 1, "instance": instance})
            definitions[instance] = xschem.dump(sym)
    return {"exists": True, "ports": ports, "unresolved_symbols": unresolved, "source": text}, definitions


def _symbol(path: Path) -> dict:
    if not path.is_file():
        return {"exists": False, "pins": []}
    text = path.read_text(errors="replace")
    pins = [{"name": pin.name, "direction": pin.dir, "index": i + 1}
            for i, pin in enumerate(schematic.sym_pins(xschem.parse(text)))]
    return {"exists": True, "pins": pins, "source": text}


def _semantic(view: dict) -> dict:
    """Geometry/comments/instance names do not change an interface check's validity."""
    return {k: ([{p: v for p, v in pin.items() if p != "instance"} for pin in value]
                if k in ("ports", "pins") else value)
            for k, value in view.items() if k != "source"}


def _entry(view: str, lib: str, cell: str, data: dict) -> dict:
    return {"view": view, "lib": lib, "cell": cell,
            "sha": provenance.text_sha(json.dumps(_semantic(data), sort_keys=True, separators=(",", ":")))}


def design_entry(prj, lib: str, cell: str, view: str) -> dict:
    """Current semantic interface, including absent views and unresolved port definitions."""
    check_name(cell)
    directory = prj.library(lib).path
    if view == "schematic-interface":
        data, _ = _schematic(directory / f"{cell}.sch", xschem.SymbolResolver(prj.symbol_paths()))
    elif view == "symbol-interface":
        data = _symbol(directory / f"{cell}.sym")
    else:
        raise ValueError(f"unknown interface view {view}")
    return _entry(view, lib, cell, data)


def capture(prj, lib: str, cell: str) -> dict:
    """Capture both views. Callers may hold their editor locks to serialize local edits."""
    check_name(cell)
    directory = prj.library(lib).path
    sch, definitions = _schematic(directory / f"{cell}.sch", xschem.SymbolResolver(prj.symbol_paths()))
    sym = _symbol(directory / f"{cell}.sym")
    inputs = provenance.inputs([_entry("schematic-interface", lib, cell, sch),
                                _entry("symbol-interface", lib, cell, sym)], prj.pdk, [],
                               options={"check": "symbol-interface", "version": VERSION})
    return {"version": VERSION, "lib": lib, "cell": cell, "schematic": sch, "symbol": sym,
            "port_definitions": definitions, "inputs": inputs}


def compare(snapshot: dict) -> dict:
    """Name, direction, uniqueness and positional interface agreement of captured views."""
    sch, sym = snapshot["schematic"], snapshot["symbol"]
    ports, pins = sch["ports"], sym["pins"]
    issues: list[dict] = []

    def issue(code: str, message: str, **details):
        issues.append({"code": code, "severity": "error", "message": message, **details})

    for view, data in (("schematic", sch), ("symbol", sym)):
        if not data["exists"]:
            issue(f"missing_{view}", f"The cell has no {view} view.")
    for unresolved in sch.get("unresolved_symbols", []):
        issue("unresolved_symbol", f"Cannot determine whether {unresolved['instance']} is a port: "
              f"symbol {unresolved['symbol']} is unavailable.", name=unresolved["instance"],
              actual=unresolved["symbol"])
    for side, entries in (("schematic", ports), ("symbol", pins)):
        counts = Counter(entry["name"] for entry in entries)
        for entry in entries:
            name = entry["name"]
            index = {f"{side}_index": entry["index"]}
            if not name.strip():
                issue(f"unnamed_{side}_pin", f"{side.title()} terminal {entry['index']} has no name.", **index)
            if entry["direction"] not in ("in", "out", "inout"):
                issue("invalid_direction", f"{side.title()} terminal {name or entry['index']} has an invalid direction.",
                      name=name, actual=entry["direction"], **index)
        for name, count in counts.items():
            if name and count > 1:
                issue(f"duplicate_{side}_pin", f"{side.title()} contains {count} terminals named {name}.",
                      name=name, actual=count)

    # Missing views already explain the failure; avoid reporting every terminal as missing too.
    if sch["exists"] and sym["exists"]:
        port_names = [p["name"] for p in ports]
        pin_names = [p["name"] for p in pins]
        port_count, pin_count = Counter(port_names), Counter(pin_names)
        for port in ports:
            name = port["name"]
            if name and name not in pin_count:
                issue("missing_symbol_pin", f"Schematic port {name} is missing from the symbol.",
                      name=name, schematic_index=port["index"])
            elif name and port_count[name] == pin_count[name] == 1:
                pin = next(p for p in pins if p["name"] == name)
                if port["direction"] != pin["direction"]:
                    issue("direction_mismatch", f"Terminal {name} has different directions in the two views.",
                          name=name, schematic_index=port["index"], symbol_index=pin["index"],
                          expected=port["direction"], actual=pin["direction"])
        for pin in pins:
            if pin["name"] and pin["name"] not in port_count:
                issue("extra_symbol_pin", f"Symbol terminal {pin['name']} has no schematic port.",
                      name=pin["name"], symbol_index=pin["index"])
        # A missing/extra or duplicate name is the underlying issue, not a separate ordering error.
        if (port_count == pin_count and all(count == 1 for count in port_count.values())
                and all(port_names) and port_names != pin_names):
            issue("pin_order_mismatch", "Symbol pin order differs from the schematic's subcircuit port order.",
                  expected=", ".join(port_names), actual=", ".join(pin_names))

    return {"match": not issues, "total": len(issues), "issues": issues,
            "schematic_ports": ports, "symbol_pins": pins,
            "schematic_exists": sch["exists"], "symbol_exists": sym["exists"],
            "lib": snapshot["lib"], "cell": snapshot["cell"]}


def result(job: Job) -> dict:
    path = job.path / "result.json"
    if job.status().get("kind") != "symbol-check" or not path.is_file():
        raise KeyError("no schematic-symbol check result (yet)")
    return json.loads(path.read_text())


def _run(jobs: Jobs, job: Job, snapshot: dict) -> None:
    started = time.time()
    try:
        if not jobs.record(job, state="running", started=started):
            return
        checked = compare(snapshot)
        (job.path / "result.json").write_text(json.dumps(checked, ensure_ascii=False, indent=2), encoding="utf-8")
        (job.path / "log.txt").write_text("\n".join(issue["message"] for issue in checked["issues"])
                                          or "Schematic ports and symbol pins match.\n", encoding="utf-8")
        jobs.record(job, state="done", finished=time.time(), seconds=time.time() - started,
                summary={"match": checked["match"], "total": checked["total"],
                         "schematic_ports": len(checked["schematic_ports"]),
                         "symbol_pins": len(checked["symbol_pins"])}, **provenance.outputs(job.path))
    except Exception as exc:
        jobs.record(job, state="failed", error=str(exc), finished=time.time(), seconds=time.time() - started)
    finally:
        jobs.untrack(job)


def start(jobs: Jobs, prj, lib: str, cell: str) -> Job:
    jobs.ensure_open()                          # refused once the server is stopping (sim.STOPPING)
    snapshot = capture(prj, lib, cell)
    jid, directory = new_run(jobs.runs_dir(prj.path))
    job = Job(directory)
    job.set(id=jid, kind="symbol-check", tool="undine", title=f"Schematic–symbol {lib}/{cell}",
            lib=lib, cell=cell, state="queued", created=time.time(), **jobs.here(), **provenance.meta(snapshot["inputs"]))
    try:
        jobs.track(job)
    except RuntimeError:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    try:
        (directory / "inputs.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        for view, filename in (("schematic", "input.sch"), ("symbol", "input.sym")):
            if snapshot[view]["exists"]:
                (directory / filename).write_text(snapshot[view]["source"], encoding="utf-8")
        threading.Thread(target=_run, args=(jobs, job, snapshot), daemon=True).start()
    except Exception as exc:
        jobs.record(job, state="failed", error=str(exc), finished=time.time())
        jobs.untrack(job)
        raise
    return job
