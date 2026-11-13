"""A cell, its intent and its views, as one overview (what the cell page of the UI shows).

A cell is the unit. Its intent (intent.py) is meta information on top of it; under it are its views:

    source views    edited by people and agents
      schematic     <cell>.sch      the circuit (connectivity and drawing)
      symbol        <cell>.sym      the cell as others place it
      rtl           <cell>.sv / .v  the behaviour
      layout        <cell>.oas      the geometry (its devices can be generated from the schematic)
      test          testbenches: schematics that place this cell and drive it (no ports of their own; sources or
                    simulation commands), testbenches a spec of its intent names, test views of its intent
    derived views   generated from source views; each records what it was generated from (provenance.py)
      netlist       from the schematic (or the RTL), on demand
      checks        DRC from the layout, LVS from layout and schematic: the sign-off jobs and their freshness
      simulation    from a testbench: the latest run and whether the design changed since

The overview only reports relations the design holds (files, instances, intent links, job records); it never makes
one up. Every view can carry view intent (intent.py, views.<view>), linked to the cell intent with `serves`.
"""
from __future__ import annotations

import re

from undine.design import intent
from undine.store import provenance
from undine.layouts import sdl
from undine.schematics import symbolcheck, xschem

VIEWS = {   # source views of a cell, in the order the overview lists them
    "schematic": {"role": "source", "files": (".sch",)},
    "symbol": {"role": "source", "files": (".sym",)},
    "rtl": {"role": "source", "files": (".sv", ".v")},
    "layout": {"role": "source", "files": (".oas",)},
}
DERIVED = {  # derived views and the views they are generated from
    "netlist": {"from": ("schematic", "rtl")},       # the SPICE / Verilog export of the cell's netlist view
    "drc": {"from": ("layout",)},
    "lvs": {"from": ("layout", "schematic")},
    "simulation": {"from": ("test",)},
}


_PORT = re.compile(r"^C \{(?:[^}]*/)?(ipin|opin|iopin)\.sym\}", re.M)
_STIMULUS = re.compile(r"^C \{(?:[^}]*/)?(vsource|isource|vsource_arith|code|code_shown)\.sym\}|^\s*\.(tran|ac|dc|op|control)\b",
                       re.M | re.I)


def is_testbench(text: str) -> bool:
    """A schematic that has no ports of its own and drives what it places: sources or simulation commands."""
    return not _PORT.search(text) and bool(_STIMULUS.search(text))


def users(prj, lib: str, cell: str, model: dict) -> tuple[list[dict], list[dict]]:
    """(testbenches, used in) of a cell: schematics of the project that place its symbol, split into testbenches
    (is_testbench) and the cells that use it; testbenches a spec names and test views of its intent are tests too.
    Each as {lib, cell, why: [...]}."""
    tests: dict[tuple[str, str], list[str]] = {}
    used: dict[tuple[str, str], list[str]] = {}
    for lname, l in prj.libraries().items():
        if l.origin != "project" or not l.path.is_dir():
            continue
        ref = rf"(?:{re.escape(lib)}/)?" if lname == lib else rf"{re.escape(lib)}/"   # another library names the library
        pat = re.compile(rf"^C \{{{ref}{re.escape(cell)}\.sym\}}", re.M)
        for sch in sorted(l.path.glob("*.sch")):
            if (lname, sch.stem) == (lib, cell):
                continue
            text = sch.read_text(errors="replace")
            if pat.search(text):
                (tests if is_testbench(text) else used).setdefault((lname, sch.stem), []).append(f"places {cell}")
    for view, it in intent.all_items(model):
        tb = (it.get("spec") or {}).get("testbench")
        if tb:
            tests.setdefault((lib, tb), []).append(f"measures {it['id']}" + (f" ({view})" if view else ""))
    for view in intent.normalize(model)["views"]:
        if view.startswith("test:"):
            tests.setdefault((lib, view[5:]), []).append("has test intent")
    out = lambda d: [{"lib": l, "cell": c, "why": sorted(set(w))} for (l, c), w in d.items()]   # noqa: E731
    return out(tests), out({k: v for k, v in used.items() if k not in tests})


def _latest(jobs: list[dict], **match) -> dict | None:
    return next((j for j in jobs if all(j.get(k) == v for k, v in match.items())), None)


def overview(srv, project: str, lib: str, cell: str) -> dict:
    prj = srv.project(project)
    library = prj.library(lib)
    entry = next((c for c in library.cells() if c["name"] == cell), None)
    present = set(entry["views"]) if entry else set()
    iv = srv.intent_view(project, lib, cell)
    model = iv["intent"]
    m = intent.normalize(model)
    jobs = srv.jobs.list(prj.path)

    def view_intent(v: str) -> list[dict]:
        return (m["views"].get(v) or {}).get("items", [])

    design = []
    for v, spec in VIEWS.items():
        row = {"view": v, "role": spec["role"], "exists": v in present, "intent": len(view_intent(v)), "notes": []}
        try:
            if v == "schematic" and v in present:
                from undine.schematics import netlist
                nl = netlist.current(library.path / f"{cell}.sch", xschem.SymbolResolver(prj.symbol_paths()),
                                     write=not library.readonly)
                if nl["pending"]:
                    row["notes"].append(f"{len(nl['pending'])} connections in the netlist not drawn yet")
            if v == "symbol" and v in present and "schematic" in present:
                cmp = symbolcheck.compare(symbolcheck.capture(prj, lib, cell))
                row["fresh"] = cmp["match"]
                if not cmp["match"]:
                    row["notes"].append(f"differs from the schematic's ports ({cmp['total']})")
            if v == "layout" and v in present and "schematic" in present and prj.pdk:
                st = sdl.Sdl(prj.pdk, srv.db(project, lib), library.path / f"{cell}.sch",
                             xschem.SymbolResolver(prj.symbol_paths())).status()
                row["fresh"] = not (st["stale"] or st["missing"])
                if st["stale"]:
                    row["notes"].append(f"devices out of date with the schematic: {', '.join(st['stale'])}")
                if st["missing"]:
                    row["notes"].append(f"devices not placed yet: {', '.join(st['missing'])}")
        except Exception as e:                              # an overview must not fail on one view
            row["notes"].append(f"status unavailable: {e}")
        design.append(row)

    tbs, used_in = users(prj, lib, cell, model)
    tests = []
    for tb in tbs:
        exists = (prj.library(tb["lib"]).path / f"{tb['cell']}.sch").is_file() if tb["lib"] in prj.libraries() else False
        run = _latest(jobs, kind="sim", lib=tb["lib"], cell=tb["cell"])
        sim = None
        if run:
            sim = {"job": run["id"], "state": run["state"], "analysis": run.get("analysis"),
                   **provenance.freshness(srv, project, run)}
        tests.append({**tb, "exists": exists, "intent": len(view_intent(f"test:{tb['cell']}")), "simulation": sim})

    derived = [{"view": "netlist", "from": [v for v in DERIVED["netlist"]["from"] if v in present],
                "available": bool({"schematic", "rtl"} & present), "on_demand": True}]
    checks = []
    if "layout" in present and prj.pdk:
        try:
            checks = srv.signoff(project, lib, cell)
        except Exception as e:
            checks = [{"name": "sign-off", "error": str(e)}]
    derived.append({"view": "checks", "from": [v for v in DERIVED["lvs"]["from"] if v in present], "checks": checks})

    levels = {lvl: sum(p["level"] == lvl for p in iv["problems"]) for lvl in ("error", "warning", "info")}
    return {
        "lib": lib, "cell": cell, "exists": bool(entry), "readonly": library.readonly,
        "intent": {
            "notes": m["notes"], "items": m["items"],
            "ports": len(m["ports"]), "supplies": m["supplies"],
            "views": {v: len(s["items"]) for v, s in m["views"].items()},
            "problems": levels, "missing": {k: len(v) for k, v in iv["requirements"].items()},
        },
        "design": design, "tests": tests, "used_in": used_in, "derived": derived,
        "registry": {"views": VIEWS, "derived": DERIVED},
    }
