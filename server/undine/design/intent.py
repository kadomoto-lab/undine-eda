"""Design intent: what a cell is meant to achieve and must respect, next to its views (docs/design/intent.md).

One file per cell in its library, `<cell>.intent.yaml`:

    version: 1
    notes: |                                  # free text: the designer's context and priorities
      Drives a 2 pF load inside the ADC.
    ports:                                    # per schematic port: kind and side on the cell outline
      inp: {kind: signal_in, side: W}
      ibias: {kind: bias_in, side: auto, decided_by: user, reason: no preference}
    supplies: {vdd: supply, vss: ground}      # which nets are power and ground
    items:                                    # everything else, each with a kind
      - {id: g1, kind: goal, text: Minimise the input offset}
      - {id: p1, kind: preference, text: Performance over area}
      - {id: c1, kind: constraint, text: M1 and M2 symmetric, strength: must, serves: [g1],
         rule: {type: symmetric, instances: [M1, M2], style: common_centroid}}
      - {id: s1, kind: spec, text: DC gain at least 60 dB,
         spec: {measure: dc_gain_db, min: 60, testbench: tb_ac, output: out, analysis: {type: ac, fstop: 1G}}}
      - {id: s2, kind: spec, text: Settles within 1 us, spec: {measure: tsettle, max: 1e-6, testbench: tb_step,
         analysis: {type: tran, step: 1n, stop: 5u}, meas: ".meas tran tsettle WHEN v(out)=0.99 RISE=1"}}
      - {id: s3, kind: spec, text: SNDR at least 45 dB, spec: {measure: sndr_db, min: 45, testbench: tb_adc,
         analysis: {type: tran, step: 1n, stop: 260u}, spectrum: {bits: [d0, d1, d2, d3, d4, d5, d6, d7], fs: 1Meg,
         points: 256, start: 1.5u, full_scale: 256}, substitute: {adc/sar_logic: adc/sar_logic_ideal}}}
      - {id: s4, kind: spec, text: Settles to 0.44 mV within 3 ns (4 ns at ss), spec: {measure: "settle(v(top),
         tol=0.44m, from=12n)", max: 3n, testbench: tb_cdac, corners: {tt: {}, ss: {max: 4n}, ff: {}}}}
      - {id: s5, kind: spec, text: The DAC controls clear within 2 ns of clk, spec: {measure: tclr, max: 2n,
         testbench: tb_logic, meas: ".meas tran tclr TRIG v(clk) VAL=0.9 RISE=1 TARG v(dac0) VAL=0.9 FALL=1",
         missing: fail}}
      - {id: x1, kind: context, text: Drives a 2 pF load, source: {by: agent:..., request: "...", turn: t-...,
         requests: [{request: "...", date: "2026-11-03", by: agent:...}]}}
    views:                                    # view intent: how a view realises the cell intent
      schematic:
        items:
          - {id: c2, kind: constraint, text: M1 and M2 equal and large, serves: [g1],
             rule: {type: match, instances: [M1, M2]}}
      layout:
        items:
          - {id: c3, kind: constraint, text: M1 and M2 in common centroid, serves: [g1]}
      test:tb_ac:                             # a testbench of this cell (a test view)
        items:
          - {id: x2, kind: context, text: Measure with the 2 pF load, serves: [s1, x1]}

Two levels. The cell intent (notes, ports, supplies, items) says what the cell has to achieve: goals, preferences,
specs, context; it does not depend on a view. A view intent says how one view realises it: constraints and choices
of that view (schematic, symbol, rtl, layout, or test:<testbench cell>), each linked with `serves` to the cell items
it comes from, so a change of a requirement shows the view intent it affects, and a view item that serves nothing is
a constraint without a reason to look at again. Item ids are unique in the whole file.

Kinds: goal (a direction to optimise), preference (how to trade off), constraint (to respect: strength must or
should), spec (measured against a limit), context (background for decisions). Every item can carry `reason` (why)
and `source` (who added it, and the request it came from). Constraints that tools need are derived from this per
flow step; the ones a person decides directly are items of kind constraint here.

A spec's `missing` says what a run whose .meas gives no value (it never triggered: a control that never clears in s5)
means: `unknown` (the default) reports the spec not measured, `fail` reports it not met for "no value" (specs.py).

A source's `request` holds the user's words the item came from; words that confirm or extend it later are added to
its `requests`, each with its date and who added it (IntentEditor.update_item: never replaced).

A spec's `substitute` ({lib/cell: lib/cell}) simulates every instance of a cell as a stand-in of the same pins (the
SNDR of s3 with a behavioural SAR controller); a testbench's own intent gives the stand-ins all its runs use by
default with a constraint `rule: {type: substitute, cells: {adc/sar_logic: adc/sar_logic_ideal}}` (substitutes).

Each item also has a `uid`, made when it is added (or first seen in the YAML panel) and never changed: what tells two
branches' items apart in a git merge (vcs/yamlmerge.py), where the readable `id` may be renumbered (both branches added
a c3). Leave it as it is.

Edits go through IntentEditor, which records one effect per entry in the library's operation log, so intent has
undo / redo, changesets and review like a schematic:

    {"k": "n", "id": "items.c1", "do": "add" | "del", "index": 2, "obj": {...}}
    {"k": "n", "id": "ports.inp", "do": "mod", "changes": {"value": [before, after]}}
"""
from __future__ import annotations

import datetime
import re
import threading
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import yaml

from undine.simulation import spectrum
from undine.store import oplog

SUFFIX = ".intent.yaml"
KINDS = ("goal", "preference", "constraint", "spec", "context")
ID_PREFIX = {"goal": "g", "preference": "p", "constraint": "c", "spec": "s", "context": "x"}
STRENGTHS = ("must", "should")
PORT_KINDS = ("signal_in", "signal_out", "inout", "bias_in", "clock", "supply", "ground")
SIDES = ("N", "S", "E", "W", "auto")
SUPPLY_ROLES = ("supply", "ground")
RULES = {   # constraint rule types the flows understand, and the references each must have
    "symmetric": ("instances",), "match": ("instances",), "net": ("net",), "keepout": (), "place": ("instances",),
    "substitute": ("cells",),       # in a testbench's intent: the stand-ins its runs netlist by default (substitutes)
}
SUPPLY_NAME = re.compile(r"^(v(dd|cc|pwr)\w*|[ad]vdd\w*)$", re.I)
GROUND_NAME = re.compile(r"^(v(ss|ee|gnd)\w*|a?gnd\w*|[ad]vss\w*|0)$", re.I)
ITEM_FIELDS = ("id", "kind", "text", "strength", "rule", "spec", "serves", "reason", "source", "uid")
# what check_specs reads from an AC run when a spec names its output (specs.py, simresults.ac_metrics)
AC_FIGURES = ("dc_gain_db", "f_3db_hz", "unity_gain_freq_hz", "phase_margin_deg", "gain_margin_db")
MEAS = re.compile(r"^\s*\.meas(?:ure)?\s+(\w+)\s+(\w+)\b", re.I)
MISSING = ("unknown", "fail")   # a spec's `missing`: what a run whose .meas gave no value means (the module docstring)


def meas_lines(value) -> list[str]:
    """A spec's `meas`: one ngspice .meas statement or a list of them, each on one line."""
    return [value] if isinstance(value, str) else list(value or [])


def meas_names(lines: list[str]) -> dict[str, str]:
    """{result name (lowercase, as ngspice reports it): analysis} of .meas statements; ValueError for anything else."""
    out = {}
    for m in lines:
        hit = MEAS.match(m) if isinstance(m, str) and "\n" not in m else None
        if not hit:
            raise ValueError(f"a meas is one .meas statement on one line (e.g. .meas tran tphl TRIG ... TARG ...), not {m!r}")
        out[hit.group(2).lower()] = hit.group(1).lower()
    return out


# .control commands whose output ends a "Measurements for" section (simresults reads the "name = value" lines under it)
_PRINTING = {"run", "op", "tran", "ac", "dc", "noise", "pz", "sens", "tf", "disto", "sp", "pss", "show", "display",
             "listing", "rusage", "status"}


def testbench_measures(text: str) -> dict:
    """The measures a testbench's netlist text (its code blocks) gives itself, lowercase: {"netlist": {name: analysis}
    of its .meas statements outside .control (they stay when a spec gives its own analysis, and count for that
    analysis), "control": [names] its .control blocks print under an `echo ... Measurements for` line (ngspice's
    heading for .meas results, which a run's summary reads measurements under): meas commands and printed vectors
    after it in the same block, before anything else prints (an analysis, an echo of other text)}. A .control's meas
    without that heading prints where no summary reads it; a spec with an analysis drops the .control blocks."""
    from undine.simulation.sim import CONTROL
    netlist = {m.group(2).lower(): m.group(1).lower()
               for m in re.finditer(r"^[ \t]*\.meas(?:ure)?\s+(\w+)\s+(\w+)\b", CONTROL.sub("", text), re.I | re.M)}
    control: set[str] = set()
    for block in CONTROL.finditer(text):
        heading = False
        for line in block.group(0).splitlines()[1:]:
            words = line.split()
            cmd = words[0].lower() if words else ""
            if cmd == "echo":
                rest = line.strip()[4:].strip()
                heading = bool(re.search(r"\bMeasurements for\b", rest, re.I)) or (heading and not rest)
            elif cmd in ("meas", "measure") and len(words) >= 3:
                if heading:
                    control.add(words[2].lower())
            elif cmd == "print":
                if heading:
                    control |= {w.lower() for w in words[1:] if re.fullmatch(r"[A-Za-z_]\w*", w) and w.lower() != "all"}
            elif cmd in _PRINTING:
                heading = False
    return {"netlist": netlist, "control": sorted(control)}


def measured_levels(lines: list[str], names=()) -> list[str]:
    """Why each .meas statement that uses a measurement (another of `lines`, or of `names`) in an expression outside
    param= fails: ngspice evaluates such a level (WHEN v(out)='vf-1m', VAL='vf/2') when it reads the netlist, before
    anything is measured, and stops the whole run on it."""
    found = {n.lower() for n in names}
    for m in lines:
        if isinstance(m, str) and (hit := MEAS.match(m)):
            found.add(hit.group(2).lower())
    out = []
    for m in lines:
        if not isinstance(m, str) or not (hit := MEAS.match(m)):
            continue
        for e in re.finditer(r"(\bparam\s*=\s*)?('[^']*'|\{[^}]*\})", m, re.I):
            names = {w.lower() for w in re.findall(r"(?<![\w.])[A-Za-z_]\w*", e.group(2))}     # not the m of 0.44m
            used = sorted(names & found - {hit.group(2).lower()})
            if used and not e.group(1):
                out.append(f".meas {hit.group(2)} uses the measurement {', '.join(used)} in {e.group(2)}: ngspice fixes such a "
                           "level when it reads the netlist, before anything is measured, and stops the whole run. For a "
                           "settling time use the measure settle(v(node), tol=..., from=...); to combine measurements, a "
                           "derived measure such as max(t1, t2) or -iavg*1.8 (specs: measure)")
                break
    return out


VIEWS = ("schematic", "symbol", "rtl", "layout")
VIEW_NAME = re.compile(r"^(schematic|symbol|rtl|layout|test:[A-Za-z0-9_][A-Za-z0-9_.+-]*)$")


def empty() -> dict:
    return {"version": 1, "notes": "", "ports": {}, "supplies": {}, "items": [], "views": {}}


def all_items(model: dict) -> list[tuple[str | None, dict]]:
    """(view or None for the cell intent, item) for every item of the file."""
    m = normalize(model)
    return [(None, it) for it in m["items"]] + [(v, it) for v, sec in m["views"].items() for it in sec["items"]]


def path_of(lib_dir: Path, cell: str) -> Path:
    return lib_dir / f"{cell}{SUFFIX}"


def load(path: Path) -> dict:
    if not path.is_file():
        return empty()
    return normalize(yaml.safe_load(path.read_text(errors="replace")) or {})


def dump(model: dict) -> str:
    m = normalize(model)
    out: dict = {"version": 1}
    if m["notes"]:
        out["notes"] = m["notes"]
    for k in ("ports", "supplies", "items"):
        if m[k]:
            out[k] = m[k]
    views = {v: {"items": sec["items"]} for v, sec in m["views"].items() if sec["items"]}
    if views:
        out["views"] = views
    return yaml.safe_dump(out, sort_keys=False, allow_unicode=True, width=120)


def is_empty(model: dict) -> bool:
    m = normalize(model)
    return not (m["notes"] or m["ports"] or m["supplies"] or m["items"] or any(s["items"] for s in m["views"].values()))


def _plain(v):
    """A copy of a value read from YAML, as JSON holds it: a date or time written bare (date: 2026-11-03, which YAML
    reads as a date) becomes its ISO text, so that the operation log and the tools can write it."""
    if isinstance(v, dict):
        return {k if isinstance(k, (str, int, float, bool)) or k is None else str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    if isinstance(v, (datetime.date, datetime.time)):
        return v.isoformat()
    return deepcopy(v)


def _requests(src: dict) -> dict:
    """A source's request history (update_item) as a list of {request, date?, by?, turn?}: an entry without the user's
    words, or not a mapping, is left out; a source with none keeps none."""
    raw = src["requests"] if isinstance(src["requests"], list) else []
    said = [{k: str(r[k]) for k in ("request", "date", "by", "turn") if r.get(k) not in (None, "")}
            for r in raw if isinstance(r, dict)]
    said = [r for r in said if r.get("request", "").strip()]
    return {**{k: v for k, v in src.items() if k != "requests"}, **({"requests": said} if said else {})}


def normalize(raw: dict) -> dict:
    """A model with every section present and only known item fields; raises ValueError on a malformed file."""
    if not isinstance(raw, dict):
        raise ValueError("an intent file is a mapping (version, notes, ports, supplies, items)")
    m = empty()
    notes = raw.get("notes") or ""
    if not isinstance(notes, str):
        raise ValueError("notes is text")
    m["notes"] = notes
    for section in ("ports", "supplies"):
        value = raw.get(section) or {}
        if not isinstance(value, dict):
            raise ValueError(f"{section} is a mapping from name to its entry")
        m[section] = {str(k): _plain(v) for k, v in value.items()}
    seen: set[str] = set()

    def items_of(raw_items, where: str) -> list[dict]:
        if not isinstance(raw_items, list) or not all(isinstance(i, dict) for i in raw_items):
            raise ValueError(f"{where} is a list of entries with id, kind and text")
        out = []
        for i in raw_items:
            it = {k: _plain(i[k]) for k in ITEM_FIELDS if k in i and i[k] not in (None, "", [], {})}
            if not it.get("id") or it["id"] in seen:
                raise ValueError(f"every item needs a unique id, in the whole file (got {it.get('id')!r})")
            if isinstance(it.get("source"), dict) and "requests" in it["source"]:
                it["source"] = _requests(it["source"])
            seen.add(it["id"])
            out.append(it)
        return out

    m["items"] = items_of(raw.get("items") or [], "items")
    views = raw.get("views") or {}
    if not isinstance(views, dict):
        raise ValueError("views is a mapping from a view (schematic, layout, test:<cell> ...) to its intent")
    for v, sec in views.items():
        v = str(v)
        if not VIEW_NAME.match(v):
            raise ValueError(f"no view {v!r}: one of {', '.join(VIEWS)} or test:<testbench cell>")
        if not isinstance(sec, dict):
            raise ValueError(f"views.{v} is a mapping with items")
        m["views"][v] = {"items": items_of(sec.get("items") or [], f"views.{v}.items")}
    return m


# ---------------------------------------------------------------- entries and effects


def entries(model: dict) -> dict[str, object]:
    """The model as flat entries in file order: notes, ports.<name>, supplies.<net>, items.<id>."""
    m = normalize(model)
    out: dict[str, object] = {}
    if m["notes"]:
        out["notes"] = m["notes"]
    for name, v in m["ports"].items():
        out[f"ports.{name}"] = v
    for net, v in m["supplies"].items():
        out[f"supplies.{net}"] = v
    for it in m["items"]:
        out[f"items.{it['id']}"] = it
    for v, sec in m["views"].items():
        for it in sec["items"]:
            out[f"views.{v}.items.{it['id']}"] = it
    return out


def from_entries(flat: dict[str, object]) -> dict:
    m = empty()
    for key, v in flat.items():
        if key == "notes":
            m["notes"] = v
            continue
        section, _, name = key.partition(".")
        if section == "views":
            view, _, _ = name.rpartition(".items.")
            m["views"].setdefault(view, {"items": []})["items"].append(v)
        elif section == "items":
            m["items"].append(v)
        else:
            m[section][name] = v
    return m


def diff(before: dict, after: dict) -> list[dict]:
    a, b = entries(before), entries(after)
    out = []
    for key, v in a.items():
        if key not in b:
            out.append({"k": "n", "id": key, "do": "del", "index": list(a).index(key), "obj": v})
    for i, (key, v) in enumerate(b.items()):
        if key not in a:
            out.append({"k": "n", "id": key, "do": "add", "index": i, "obj": v})
        elif a[key] != v:
            out.append({"k": "n", "id": key, "do": "mod", "changes": {"value": [a[key], v]}})
    return out


def apply(model: dict, effects: list[dict], backwards: bool) -> dict:
    """Applies intent effects forwards (redo, replay) or backwards (undo); a conflicting entry raises ValueError."""
    flat = list(entries(model).items())
    keys = lambda: [k for k, _ in flat]                       # noqa: E731
    for e in (reversed(effects) if backwards else effects):
        if e.get("k") != "n":
            continue
        do = {"add": "del", "del": "add", "mod": "mod"}[e["do"]] if backwards else e["do"]
        key = e["id"]
        if do == "add":
            if key in keys():
                raise ValueError(f"conflict: intent entry {key} exists")
            flat.insert(min(e.get("index", len(flat)), len(flat)), (key, deepcopy(e["obj"])))
        elif do == "del":
            if key not in keys():
                raise ValueError(f"conflict: intent entry {key} is gone")
            i = keys().index(key)
            if flat[i][1] != e["obj"]:
                raise ValueError(f"conflict: intent entry {key} changed outside this operation")
            del flat[i]
        else:
            old, new = e["changes"]["value"]
            if backwards:
                old, new = new, old
            if key not in keys():
                raise ValueError(f"conflict: intent entry {key} is gone")
            i = keys().index(key)
            if flat[i][1] != old:
                raise ValueError(f"conflict: intent entry {key} changed outside this operation")
            flat[i] = (key, deepcopy(new))
    return from_entries(dict(flat))


# ---------------------------------------------------------------- checks against the design


def _gives_itself(spec: dict, design: dict | None) -> bool:
    """Whether the spec's measure comes from its own meas or its testbench's netlist (design["measures"]: {testbench:
    testbench_measures}), so that it needs no spectrum to be measured. With the spec's own analysis the testbench's
    .control blocks do not run: only its .meas statements of that analysis count."""
    name = str(spec.get("measure") or "").lower()
    try:
        if name in meas_names(meas_lines(spec.get("meas"))):
            return True
    except ValueError:
        pass
    got = ((design or {}).get("measures") or {}).get(str(spec.get("testbench") or "")) or {}
    analysis = spec.get("analysis")
    if isinstance(analysis, dict):
        return (got.get("netlist") or {}).get(name) == str(analysis.get("type") or "").lower()
    return name in (got.get("netlist") or {}) or name in (got.get("control") or ())


def validate(model: dict, design: dict | None) -> list[dict]:
    """Problems of an intent against the cell's schematic: [{level: error | warning, entry, message}].

    `design` is {"instances": [...], "nets": [...], "ports": [...], "cells": [...]} from the schematic (and the
    library's cells, for testbenches), and optionally "measures": {testbench: the measures its netlist gives itself
    (testbench_measures)}; None skips the reference checks."""
    m = normalize(model)
    out = []

    def problem(level, entry, message):
        out.append({"level": level, "entry": entry, "message": message})

    for name, p in m["ports"].items():
        entry = f"ports.{name}"
        if not isinstance(p, dict):
            problem("error", entry, "a port entry is a mapping (kind, side)"); continue
        if p.get("kind") not in (None, *PORT_KINDS):
            problem("error", entry, f"kind is one of {', '.join(PORT_KINDS)}")
        if p.get("side") not in (None, *SIDES):
            problem("error", entry, f"side is one of {', '.join(SIDES)}")
        if design is not None and name not in design["ports"]:
            problem("warning", entry, f"the schematic has no port {name}")
    for net, role in m["supplies"].items():
        if role not in SUPPLY_ROLES:
            problem("error", f"supplies.{net}", "a supply net is supply or ground")
        if design is not None and net not in design["nets"] and net not in design["ports"]:
            problem("warning", f"supplies.{net}", f"the schematic has no net {net}")
    ids = {it["id"] for _, it in all_items(m)}
    cell_ids = {it["id"] for it in m["items"]}
    for view, it in all_items(m):
        entry = f"items.{it['id']}" if view is None else f"views.{view}.items.{it['id']}"
        if view and view.startswith("test:") and design is not None and view[5:] not in design.get("cells", []):
            problem("warning", entry, f"no testbench cell {view[5:]}")
        if view and it.get("kind") in ("constraint", "preference") and not set(it.get("serves") or []) & cell_ids:
            problem("info", entry, "serves no cell intent item: say which requirement it comes from (serves)")
        if it.get("kind") not in KINDS:
            problem("error", entry, f"kind is one of {', '.join(KINDS)}")
        if not str(it.get("text", "")).strip():
            problem("error", entry, "say the intent in text (text)")
        if it.get("kind") == "constraint":
            if it.get("strength", "must") not in STRENGTHS:
                problem("error", entry, "strength is must or should")
            rule = it.get("rule")
            if rule is not None:
                if not isinstance(rule, dict) or rule.get("type") not in RULES:
                    problem("warning", entry, f"rule type is one of {', '.join(RULES)}; others are kept for people only")
                else:
                    for need in RULES[rule["type"]]:
                        if not rule.get(need):
                            problem("error", entry, f"a {rule['type']} rule names its {need}")
                    if rule["type"] == "substitute":
                        from undine.simulation import sim
                        try:
                            sim.substitute_map(rule.get("cells"))
                        except ValueError as e:
                            problem("error", entry, str(e).replace("substitute", "cells", 1))
                        if view is not None:
                            problem("warning", entry, "a substitute rule counts in the cell intent of a testbench, "
                                                      "not in a view's")
                        elif design is not None and design.get("ports"):
                            # a testbench has no ports: this is likely the cell measured, whose own runs are none
                            problem("warning", entry, "a substitute rule gives the stand-ins of the runs of this cell "
                                                      "as a testbench, and a cell with ports is rarely one: for its "
                                                      "specs, give the spec substitute, or put the rule in its "
                                                      "testbench's intent")
                    if design is not None:
                        for inst in rule.get("instances") or []:
                            if inst not in design["instances"]:
                                problem("warning", entry, f"the schematic has no instance {inst}")
                        if rule.get("net") and rule["net"] not in design["nets"] and rule["net"] not in design["ports"]:
                            problem("warning", entry, f"the schematic has no net {rule['net']}")
        if it.get("kind") == "spec":
            spec = it.get("spec") or {}
            if not isinstance(spec, dict) or not spec.get("measure") or ("min" not in spec and "max" not in spec):
                problem("error", entry, "a spec names its measure and a min and / or max")
            elif design is not None and spec.get("testbench") and spec["testbench"] not in design.get("cells", []):
                problem("warning", entry, f"no testbench cell {spec['testbench']}")
            if isinstance(spec, dict) and spec.get("analysis") is not None and \
                    (not isinstance(spec["analysis"], dict) or spec["analysis"].get("type") not in ("op", "tran", "ac", "dc")):
                problem("error", entry, "a spec's analysis is {type: op|tran|ac|dc, ...}")
            if isinstance(spec, dict) and spec.get("measure") in AC_FIGURES and not spec.get("output"):
                problem("warning", entry, f"{spec['measure']} is an AC figure: name the output node (output) to measure it")
            spectral = isinstance(spec, dict) and str(spec.get("measure") or "").lower() in spectrum.FIGURES
            if spectral and not spec.get("spectrum") and not _gives_itself(spec, design):
                problem("warning", entry, f"{spec['measure']} is a spectral figure: say how to sample the transient "
                                          "(spectrum {signal or bits, fs, points, start}) to measure it")
            if isinstance(spec, dict) and spec.get("spectrum") is not None:
                try:
                    cfg = spectrum.config(spec["spectrum"])
                except ValueError as e:
                    problem("error", entry, str(e))
                else:
                    if (why := spectrum.check_run(cfg, spec.get("analysis"))):
                        problem("error", entry, why)
                    if not spectral:
                        problem("warning", entry, f"its spectrum gives {', '.join(spectrum.FIGURES)}, not {spec.get('measure')}")
                    elif str(spec["measure"]).lower() == "signal_dbfs" and not cfg["full_scale"]:
                        problem("warning", entry, "signal_dbfs needs the full scale (spectrum full_scale: volts peak-to-peak, "
                                                  "or 2^N codes)")
            if isinstance(spec, dict):
                from undine.simulation import derived, sim, specs   # specs reads intent: imported when used
                derived_measure = derived.is_derived(spec.get("measure"))
                try:
                    if derived_measure:
                        derived.check(spec["measure"])
                    specs.where(spec.get("corners"), "corner")         # a list, or a mapping to the limits there
                    specs.where(spec.get("temps"), "temp")
                except ValueError as e:
                    problem("error", entry, str(e))
                if spec.get("conditions") is not None and not isinstance(spec["conditions"], dict):
                    problem("error", entry, "conditions is a mapping from cell/instance.property to values, as check_specs takes")
                if spec.get("missing") is not None and spec["missing"] not in MISSING:
                    problem("error", entry, "a spec's missing is fail (a run whose .meas gives no value does not meet "
                                            "it) or unknown (the default: it is not measured there)")
                try:
                    sim.substitute_map(spec.get("substitute"))
                except ValueError as e:
                    problem("error", entry, str(e))
            if isinstance(spec, dict) and spec.get("meas"):
                try:
                    names = meas_names(meas_lines(spec["meas"]))
                except ValueError as e:
                    problem("error", entry, str(e))
                else:
                    kind = (spec.get("analysis") or {}).get("type") if isinstance(spec.get("analysis"), dict) else None
                    for why in measured_levels(meas_lines(spec["meas"])):
                        problem("error", entry, why)
                    if str(spec.get("measure", "")).lower() not in names and not derived_measure:
                        problem("warning", entry, f"measure {spec.get('measure')} is not what its meas defines ({', '.join(names)})")
                    if kind and (other := sorted({a for a in names.values() if a != kind})):
                        problem("warning", entry, f"its meas measures a {', '.join(other)} analysis, but the spec runs {kind}")
        for s in it.get("serves") or []:
            if s not in ids:
                problem("warning", entry, f"serves {s}, which is not an item")
    return out


def substitutes(path: Path) -> tuple[dict[str, tuple[object, str]], str | None]:
    """The stand-ins a testbench's runs netlist by default, from its intent file at `path`: the constraints of its cell
    intent with rule {type: substitute, cells: {"lib/cell": "lib/cell"}} (Server.substitutes), a later item's over an
    earlier one's, as {cell: (its stand-in as written, the item's id)}; and why none could be read (the file is not
    YAML), or None. Only these items are read: what is wrong elsewhere in the file (validate) does not stop a run.
    ValueError for a substitute rule whose cells are not a mapping."""
    if not path.is_file():
        return {}, None
    try:
        raw = yaml.safe_load(path.read_text(errors="replace")) or {}
    except yaml.YAMLError as e:
        return {}, f"{path.name} is not YAML ({' '.join(str(e).split())})"
    items = raw.get("items") if isinstance(raw, dict) else None
    out: dict[str, tuple[object, str]] = {}
    for n, it in enumerate(items if isinstance(items, list) else []):
        rule = it.get("rule") if isinstance(it, dict) else None
        if isinstance(rule, dict) and it.get("kind") == "constraint" and rule.get("type") == "substitute":
            name = str(it.get("id") or f"item {n + 1}")
            if not isinstance(cells := rule.get("cells"), dict):
                raise ValueError(f"{name}: a substitute rule's cells map a cell to its stand-in")
            out.update({str(k): (v, name) for k, v in cells.items()})
    return out, None


# what each flow step needs from the intent (docs/design/intent.md, section 3)
STEPS = {
    "simulation": "Simulation",
    "layout": "Analog placement and routing",
}


def requirements(model: dict, design: dict | None) -> dict[str, list[dict]]:
    """Per flow step, the intent it needs that is missing: [{entry, message}] (shown, not stored)."""
    m = normalize(model)
    need: dict[str, list[dict]] = {step: [] for step in STEPS}
    for it in m["items"]:
        spec = it.get("spec") or {}
        if it.get("kind") == "spec" and not spec.get("testbench"):
            need["simulation"].append({"entry": f"items.{it['id']}", "message": f"which testbench measures {spec.get('measure', it['id'])}"})
    if design is not None:
        for port in design["ports"]:
            p = m["ports"].get(port) or {}
            if not p.get("side"):
                need["layout"].append({"entry": f"ports.{port}", "message": f"the side of port {port} (or auto)"})
            if not p.get("kind"):
                need["layout"].append({"entry": f"ports.{port}", "message": f"the kind of port {port}"})
        if not any(r == "supply" for r in m["supplies"].values()):
            need["layout"].append({"entry": "supplies", "message": "which net is the supply"})
        if not any(r == "ground" for r in m["supplies"].values()):
            need["layout"].append({"entry": "supplies", "message": "which net is ground"})
    return need


def suggested_supplies(design: dict) -> dict[str, str]:
    """Supply / ground roles guessed from net and port names (vdd, vss ...), for pre-filling."""
    out = {}
    for n in [*design["ports"], *design["nets"]]:
        if SUPPLY_NAME.match(n):
            out.setdefault(n, "supply")
        elif GROUND_NAME.match(n):
            out.setdefault(n, "ground")
    return out


def summary(model: dict) -> str:
    """The intent as text for an agent's context: every entry, its kind and strength, reasons and sources."""
    m = normalize(model)
    lines = []
    if m["notes"].strip():
        lines.append("notes: " + " ".join(m["notes"].split()))
    for name, p in m["ports"].items():
        p = p if isinstance(p, dict) else {}
        extra = f" ({p['reason']})" if p.get("reason") else ""
        lines.append(f"port {name}: {p.get('kind', '?')}, side {p.get('side', 'unset')}{extra}")
    if m["supplies"]:
        lines.append("supplies: " + ", ".join(f"{n}={r}" for n, r in m["supplies"].items()))
    for view, it in all_items(m):
        head = it["kind"] + (f" [{it.get('strength', 'must')}]" if it["kind"] == "constraint" else "")
        bits = [f"{'[' + view + ' view] ' if view else ''}{it['id']} {head}: {it.get('text', '')}"]
        if it.get("rule"):
            bits.append(f"rule {it['rule']}")
        if it.get("spec"):
            bits.append(f"spec {it['spec']}")
        if it.get("serves"):
            bits.append("serves " + ", ".join(it["serves"]))
        if it.get("reason"):
            bits.append(f"why: {it['reason']}")
        src = it.get("source") or {}
        if src.get("request"):
            bits.append(f"from the request \"{src['request']}\"")
        for r in src.get("requests") or []:                  # confirmed or extended since (update_item)
            if isinstance(r, dict) and r.get("request"):
                bits.append(f"then on {r.get('date', '?')} \"{r['request']}\"")
        lines.append(" | ".join(bits))
    return "\n".join(lines)


def new_uid() -> str:
    """An item's identity for merges (vcs/yamlmerge.py): made once when the item is added, never shown as its name and
    never changed, so two branches' items stay told apart however a merge renumbers their ids."""
    return uuid.uuid4().hex[:12]


def next_id(model: dict, kind: str) -> str:
    """The id of a new item of `kind`: its prefix and one more than the highest number in use with that prefix. A
    gap (a deleted id) is never filled: in a git merge (vcs/yamlmerge.py) a new item that took a deleted item's id
    would take the other side's edits of the deleted item too."""
    prefix = ID_PREFIX.get(kind, "i")
    numbered = re.compile(rf"^{re.escape(prefix)}(\d+)$")
    used = [int(hit.group(1)) for _, it in all_items(model) if (hit := numbered.match(str(it["id"])))]
    return f"{prefix}{max(used, default=0) + 1}"


# ---------------------------------------------------------------- the editor


class IntentEditor:
    """Edits one cell's intent file and records each change in the library's operation log (view "intent")."""

    view_kind = "intent"

    def __init__(self, path: Path, log: oplog.OpLog | None = None):
        self.path = path
        self.log = log
        self.cell = path.name[: -len(SUFFIX)]
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

    def model(self) -> dict:
        return load(self.path)

    def _write(self, model: dict) -> str:
        text = "" if is_empty(model) else dump(model)
        if text:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(text)
        else:
            self.path.unlink(missing_ok=True)                 # an empty intent leaves no file behind
        return text

    def _commit(self, model: dict) -> list[dict]:
        if self.log is not None:
            self.log.guard()                                  # one writer per changeset (live.py)
        before = self.model()
        model = normalize(model)
        effects = diff(before, model)
        if not effects:
            return []
        text = self._write(model)
        if self.log is not None:
            self.log.append({**self._meta, "cell": self.cell, "view": self.view_kind,
                             "sha": oplog.sha(text), "effects": effects})
        return effects

    # ------------------------------------------------------------ operations

    def _items(self, m: dict, view: str | None) -> list[dict]:
        if view is None:
            return m["items"]
        if not VIEW_NAME.match(view):
            raise ValueError(f"no view {view!r}: one of {', '.join(VIEWS)} or test:<testbench cell>")
        return m["views"].setdefault(view, {"items": []})["items"]

    def _find(self, m: dict, item_id: str) -> tuple[list[dict], dict] | None:
        for lst in [m["items"], *[s["items"] for s in m["views"].values()]]:
            for it in lst:
                if it["id"] == item_id:
                    return lst, it
        return None

    def add_item(self, item: dict, actor: str = "user", view: str | None = None) -> str:
        """Adds an item to the cell intent, or with `view` to that view's intent (link it with serves)."""
        with self.lock:
            m = self.model()
            kind = item.get("kind")
            if kind not in KINDS:
                raise ValueError(f"kind is one of {', '.join(KINDS)}")
            if not str(item.get("text", "")).strip():
                raise ValueError("say the intent in text (text)")
            it = {k: deepcopy(item[k]) for k in ITEM_FIELDS if k in item and item[k] not in (None, "", [], {})}
            it["id"] = next_id(m, kind)
            it["uid"] = new_uid()
            if kind == "constraint":
                it.setdefault("strength", "must")
            it["source"] = {"by": actor, **(it.get("source") or {})}
            self._items(m, view).append(it)
            self._commit(m)
            return it["id"]

    def update_item(self, item_id: str, fields: dict, actor: str = "user", turn: str | None = None) -> bool:
        """Sets an item's fields (an empty value clears one; id, uid and source stay). `request`: the user's words that
        later confirm or extend the item, added to its source's request history (source.requests: {request, date, by,
        turn?}) after the words it came from and those added before, which are kept; blank words, or the same words as
        the last ones, add nothing. Returns whether words were added."""
        with self.lock:
            m = self.model()
            found = self._find(m, item_id)
            if found is None:
                raise KeyError(f"no intent item {item_id}")
            it = found[1]
            for k, v in fields.items():
                if k in ("id", "source", "uid") or k not in ITEM_FIELDS:
                    continue
                if v in (None, "", [], {}):
                    it.pop(k, None)
                else:
                    it[k] = deepcopy(v)
            if it.get("kind") not in KINDS:
                raise ValueError(f"kind is one of {', '.join(KINDS)}")
            words = fields.get("request")
            if words is not None and not isinstance(words, str):
                raise ValueError("request is the user's words, quoted (text)")
            added = False
            if words and words.strip():
                src = it.setdefault("source", {})
                said = [r for r in src.get("requests") or [] if isinstance(r, dict)]
                last = said[-1].get("request") if said else src.get("request")
                if words.strip() != (last or "").strip():
                    src["requests"] = [*said, {"request": words.strip(), "date": time.strftime("%Y-%m-%d"), "by": actor,
                                               **({"turn": turn} if turn else {})}]
                    added = True
            self._commit(m)
            return added

    def remove_item(self, item_id: str) -> None:
        with self.lock:
            m = self.model()
            found = self._find(m, item_id)
            if found is None:
                raise KeyError(f"no intent item {item_id}")
            found[0].remove(found[1])
            for i in [*m["items"], *[x for s in m["views"].values() for x in s["items"]]]:   # the model itself
                if item_id in (i.get("serves") or []):
                    i["serves"] = [s for s in i["serves"] if s != item_id]
                    if not i["serves"]:
                        del i["serves"]
            self._commit(m)

    def set_port(self, name: str, fields: dict | None) -> None:
        """Sets (merges) a port's entry; None removes it. A field set to None or "" is cleared."""
        with self.lock:
            m = self.model()
            if fields is None:
                m["ports"].pop(name, None)
            else:
                p = dict(m["ports"].get(name) or {})
                for k, v in fields.items():
                    if v in (None, ""):
                        p.pop(k, None)
                    else:
                        p[k] = v
                if p.get("kind") not in (None, *PORT_KINDS) or p.get("side") not in (None, *SIDES):
                    raise ValueError(f"kind is one of {', '.join(PORT_KINDS)}; side one of {', '.join(SIDES)}")
                if p:
                    m["ports"][name] = p
                else:
                    m["ports"].pop(name, None)
            self._commit(m)

    def set_supply(self, net: str, role: str | None) -> None:
        with self.lock:
            m = self.model()
            if role in (None, ""):
                m["supplies"].pop(net, None)
            elif role not in SUPPLY_ROLES:
                raise ValueError("a supply net is supply or ground")
            else:
                m["supplies"][net] = role
            self._commit(m)

    def set_notes(self, text: str) -> None:
        with self.lock:
            m = self.model()
            m["notes"] = text or ""
            self._commit(m)

    def replace(self, text: str) -> None:
        """Replaces the whole intent with YAML text (the panel's YAML view); recorded entry by entry."""
        with self.lock:
            try:
                raw = yaml.safe_load(text) if text.strip() else {}
            except yaml.YAMLError as e:
                raise ValueError(f"not valid YAML: {e}") from e
            new = normalize(raw or {})
            have = {str(it["id"]): it.get("uid") for _, it in all_items(self.model())}
            for lst in [new["items"], *[sec["items"] for sec in new["views"].values()]]:
                for it in lst:                           # an item kept keeps its uid, a new one gets one
                    it.setdefault("uid", have.get(str(it["id"])) or new_uid())
            self._commit(new)

    # ------------------------------------------------------------ history

    def replay(self, effects: list[dict], meta: dict) -> None:
        """Applies effects recorded elsewhere (a changeset, or the inverse of one) and logs them as `meta`."""
        with self.lock:
            m = apply(self.model(), effects, False)
            self._meta = meta
            try:
                self._commit(m)
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
        m = apply(self.model(), rec["effects"], backwards)
        text = self._write(m)
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
