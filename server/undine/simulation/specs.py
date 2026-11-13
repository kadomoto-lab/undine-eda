"""Specs measured: a cell's intent specs run on their testbenches, at corners, temperatures and parameter points.

A spec (intent.py) is `{measure, min, max, testbench}`, optionally with `analysis` (else the testbench's own .control
runs), `output` and `input` (for the AC figures), and `meas`: .meas statements added to its runs only (start_sim
measure), so a delay or a rise time is measured without changing the testbench. Where it is checked when the call does
not say: `corners` and `temps`, each a list or a mapping to the limits that differ there ({tt: {}, ss: {max: 97n}}),
and `conditions`. A measure is read from what a designer reads off a run (simresults.summary), in this order:

* a `.meas` result of that name (ngspice lowercases them): anything the testbench measures itself;
* an AC figure, with `output`: dc_gain_db, f_3db_hz, unity_gain_freq_hz, phase_margin_deg, gain_margin_db;
* a spectral figure of a transient, with `spectrum` (spectrum.py: how to sample the run, e.g. an ADC's bits): sndr_db,
  snr_db, thd_db, sfdr_db, enob, signal_dbfs;
* an operating point: `v(node)`, or `<device>.<quantity>` such as `x1.xm1.gm` or `x1.xm1.vds_minus_vdsat`;
* a transient or DC signal: `<signal>.<statistic>`, e.g. `v(out).max` (initial, final, min, max, max_slope_per_s);
* a derived measure (derived.py): arithmetic and max / min / mean over the measures above (`max(t1, t2)`,
  `-iavg * 1.8`), and `settle(v(out), tol=0.5m, from=10n)`, a settling time read from the raw file.

A point is a corner, a temperature, simulator options (`options.reltol` ..., sim.sim_options), a Monte Carlo seed
(mismatch) and property values ({cell: {instance: {property: value}}}), netlisted as if edited (schematic.spice
overrides): the design is never written. Property values are either design choices (`vary`, e.g. a transistor's W) or
operating conditions (`conditions`, e.g. the testbench's supply or load), like corners and temperatures: a design point
is judged by its worst case over all conditions (fold). Each point and group of specs that share a testbench and
analysis is one ngspice job; jobs run a few at a time, and a job that already ran on exactly the same inputs is reused
(provenance key) unless the call is `fresh`, so asking again costs nothing and an unfinished sweep continues where it
stopped. Each row says which of its runs were reused, and when they ran.

The margin of a measurement is its distance to the limit, positive when met; it is also given relative to the limit
(`margin_rel`, or the absolute margin when the limit is 0; kept unrounded, so two points that round alike are still
told apart). A point's score is its worst relative margin over the specs: the point that meets every spec by the most
has the highest score. The worst case is taken over the points measured: a point that failed or gave no value is
reported as such and never stands in for, or is replaced by, another point's value.

With `mismatch` ({runs, seed}) every point is simulated `runs` times with the PDK's device mismatch on (seeds seed ..
seed + runs - 1); per spec and point: the mean, sigma, min / max and the yield against its limits (mismatch_stats).

A finished run whose .meas gave the measure no value (it never triggered: a TRIG / TARG / WHEN level never crossed)
leaves the spec not measured there, unless the spec says `missing: fail`: then it is not met there, for "no value"
(`no_value`), as when a control that must clear after a clock edge never does. A .meas that could not run is not that:
a node the run does not have, a measure no .meas gives, an analysis that does not run. The spec is then wrong, not the
design, and stays not measured whatever it says.

A spec's `substitute` ({"lib/cell": "lib/cell"}) runs its testbench with stand-ins: every instance of a cell, at any
depth, netlisted as another of the same pins (a behavioural model for a long transient: an ADC's controller in a
256-point FFT run), over the testbench's own default and under the call's (Server.substitutes; null takes one back).
Specs with other stand-ins never share a run, and each spec's report names the stand-ins its value was measured with.
"""
from __future__ import annotations

import itertools
import math
import os
import re
import time

from undine.simulation import derived, sim, simresults, spectrum
from undine.design.intent import AC_FIGURES, MISSING, meas_lines, meas_names, measured_levels
SIGNAL_STATS = ("initial", "final", "min", "max", "max_slope_per_s")
MAX_JOBS = 120              # per call: a larger sweep is narrowed, or split, by the caller
MAX_MISMATCH_RUNS = 10000   # Monte Carlo runs asked per point (a call runs those that fit in MAX_JOBS)
NO_VALUE_RUNS = 5           # Monte Carlo runs named per spec and point whose .meas never triggered (mismatch_stats)
MAX_SEED = 2 ** 31 - 1      # ngspice's .option seed: 1 .. this; it ignores any other and seeds from the time
PARALLEL = max(1, min(4, (os.cpu_count() or 2) // 2))


class SpecError(ValueError):
    """The request cannot be run as asked; the message says what to change."""


# ------------------------------------------------------------------ what to measure


def probes(model: dict, lib: str, ids: list[str] | None = None) -> list[dict]:
    """The specs of an intent model as probes: {id, text, measure, min, max, lib, testbench, analysis, output, input,
    meas, spectrum (checked, spectrum.config), corners, temps, conditions (where it is checked when the call does not
    say), at ({("corner", "ss"): {"max": 9.7e-08}}: the limits that differ there), substitute (its stand-ins,
    sim.substitute_map), missing (unknown | fail: what a .meas that gave no value means)}."""
    out = []
    for it in model.get("items") or []:
        if it.get("kind") != "spec" or (ids and it.get("id") not in ids):
            continue
        s = it.get("spec") or {}
        tb = str(s.get("testbench") or "")
        tlib, _, tcell = tb.rpartition("/")
        out.append({"id": it.get("id"), "text": it.get("text", ""), "measure": s.get("measure"),
                    "min": _num(s.get("min")), "max": _num(s.get("max")), "lib": tlib or lib, "testbench": tcell,
                    "analysis": s.get("analysis") or None, "output": s.get("output"), "input": s.get("input"),
                    "meas": [m.strip() for m in meas_lines(s.get("meas"))], "spectrum": s.get("spectrum"),
                    "corners": s.get("corners"), "temps": s.get("temps"), "conditions": s.get("conditions") or None,
                    "substitute": s.get("substitute"), "missing": s.get("missing") or "unknown"})
    if ids and (missing := sorted(set(ids) - {p["id"] for p in out})):
        raise SpecError(f"no spec {', '.join(missing)} in the intent")
    for p in out:
        try:
            meas_names(p["meas"])
            if (bad := measured_levels(p["meas"])):
                raise ValueError(bad[0])
            if derived.is_derived(p["measure"]):
                derived.check(p["measure"])
            p["corners"], at = where(p["corners"], "corner")
            p["temps"], at_temp = where(p["temps"], "temp")
            p["at"] = {**at, **at_temp}
            if p["conditions"] is not None and not isinstance(p["conditions"], dict):
                raise ValueError("conditions is a mapping from cell/instance.property to values, as check_specs takes")
            if p["missing"] not in MISSING:
                raise ValueError(f"missing is fail or unknown, not {p['missing']!r}")
            p["substitute"] = sim.substitute_map(p["substitute"])
            if p["spectrum"] is not None:
                p["spectrum"] = spectrum.config(p["spectrum"])
                if (why := spectrum.check_run(p["spectrum"], p["analysis"])):
                    raise ValueError(why)
        except ValueError as e:
            raise SpecError(f"spec {p['id']}: {e}") from e
    return out


def _num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(_si(str(v)))


SI = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "meg": 1e6, "g": 1e9, "t": 1e12}


def _si(text: str) -> float:
    m = re.fullmatch(r"\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?)\s*(meg|[fpnumkgt])?[a-z]*\s*", text, re.I)
    if not m:
        raise SpecError(f"not a number: {text!r}")
    scale = SI.get((m.group(2) or "").lower(), 1.0)
    if scale != 1.0 and "e" not in m.group(1).lower():
        return float(f"{m.group(1)}e{round(math.log10(scale))}")     # 97n is 9.7e-08, not 9.700000000000001e-08
    return float(m.group(1)) * scale


def where(value, kind: str) -> tuple[list | None, dict]:
    """A spec's corners or temps: a list, or a mapping to the limits that differ there ({tt: {}, ss: {max: 97n}}; {} or
    nothing where its own hold). Returns (the list, {(kind, corner or temperature): {min and / or max}})."""
    if value in (None, "", [], {}):
        return None, {}
    keys = [value] if isinstance(value, (str, int, float)) else list(value) if isinstance(value, (list, dict)) else None
    if keys is None:
        raise ValueError(f"{kind}s is a list, or a mapping to the limits that differ there, e.g. {{ss: {{max: 97n}}}}")
    names = [str(k) if kind == "corner" else float(_num(k)) for k in keys]
    at = {}
    for k, name in zip(keys, names):
        lim = value.get(k) if isinstance(value, dict) else None
        if lim in (None, {}):
            continue
        if not isinstance(lim, dict) or set(lim) - {"min", "max"}:
            raise ValueError(f"{kind} {k}: the limits there are {{min, max}}, not {lim!r}")
        at[(kind, name)] = {x: _num(lim[x]) for x in ("min", "max") if x in lim}
    return names, at


def limits(p: dict, pt: dict) -> tuple[float | None, float | None]:
    """A spec's (min, max) at a point (a grid point or its label): its own, or what its corners / temps give there (the
    temperature's over the corner's)."""
    lo, hi = p["min"], p["max"]
    for key in (("corner", pt.get("corner")), ("temp", pt.get("temp"))):
        o = (p.get("at") or {}).get(key) or {}
        lo, hi = o.get("min", lo), o.get("max", hi)
    return lo, hi


def _no_raw(signal: str):
    raise ValueError(f"settle({signal}) reads the run's raw file, which this caller does not have")


def value(summary: dict, name: str, trace=_no_raw) -> float | None:
    """measure(), or a derived measure (derived.py) evaluated on the run; `trace(signal)` gives a transient signal of
    its raw file for settle(). ValueError says why a derived measure has no value."""
    v = measure(summary, name)
    if v is not None or not derived.is_derived(name):
        return v
    try:
        return derived.evaluate(name, lambda n: measure(summary, n), trace)
    except ValueError as e:
        if (m := re.fullmatch(r"no measure (.+) in this run", str(e))):
            raise ValueError(f"{name}: {no_value(summary, m.group(1))}") from e      # the .meas that failed, and why
        raise ValueError(f"{name}: {e}") from e


def no_value(summary: dict, name: str) -> str:
    """Why the run gives the measure `name` no value: its .meas failed (what ngspice says, measurement_failures), or
    the run has no measure of that name (and what it has)."""
    if (why := (summary.get("measurement_errors") or {}).get(name.lower())):
        return meas_failed(name, why)
    return f"no measure {name} in this run; it has: {', '.join(available(summary)[:24]) or 'nothing to read'}"


def meas_failed(name: str, why: str) -> str:
    """A .meas ngspice could not evaluate: what it says (`why`), and the likely cause."""
    cause = ("a node or vector the run does not have; one node per v(), a difference is par('v(a)-v(b)')"
             if "no such vector" in why else "a TRIG / TARG level never crossed, or a measurement window outside the run"
             if "out of interval" in why else "a param= that uses a measure that failed, or a name that is not defined"
             if "undefined parameter" in why.lower() else "see the log")
    return f"its .meas {name} gave no value: ngspice says {why!r} ({cause})"


def measure(summary: dict, name: str) -> float | None:
    """The value of `name` in a run's summary (see the module docstring), or None."""
    low = name.lower()
    meas = {k.lower(): v for k, v in (summary.get("measurements") or {}).items()}
    if low in meas:
        return meas[low]
    if low in AC_FIGURES:
        return (summary.get("metrics") or {}).get(low)
    if low in spectrum.FIGURES:
        return (summary.get("spectrum") or {}).get(low)
    if (m := re.fullmatch(r"v\((.+)\)", low)) and "node_voltages" in summary:
        volts = {k.lower(): v for k, v in summary["node_voltages"].items()}
        return volts.get(m.group(1))
    head, _, tail = low.rpartition(".")
    if head and tail in SIGNAL_STATS:
        sig = {k.lower(): v for k, v in (summary.get("signals") or {}).items()}
        s = sig.get(head) or sig.get(f"v({head})")
        if s is not None and tail in s:
            return s[tail]
    if head:
        for t in summary.get("transistors") or []:
            if t["device"].lower() == head and isinstance(t.get(tail), (int, float)):
                return t[tail]
    return None


def signals(ps: list[dict]) -> list[str]:
    """What the specs of one run read from its raw file (an operating point's v(node), a signal's statistic, output and
    input, a spectrum's signal or bits): a transient, and a run on an extracted view, stores these and the
    testbench's nets only (Server.start_sim)."""
    out: list[str] = []
    for p in ps:
        m = (p["measure"] or "").strip()
        names, settled = derived.references(m) if derived.is_derived(m) else ([m], [])
        out += settled
        for m in names:
            head, _, tail = m.rpartition(".")
            if re.fullmatch(r"v\(.+\)", m, re.I):
                out.append(m)
            elif head and tail.lower() in SIGNAL_STATS:
                out.append(head)
        out += [x for x in (p["output"], p["input"]) if x]
        out += (p["spectrum"] or {}).get("signal") or (p["spectrum"] or {}).get("bits") or []
    return list(dict.fromkeys(out))


def available(summary: dict) -> list[str]:
    """Measure names this run offers, to say what could be asked for when a name is not there."""
    out = list(summary.get("measurements") or {})
    out += [k for k in AC_FIGURES if k in (summary.get("metrics") or {})]
    out += [k for k in spectrum.FIGURES if k in (summary.get("spectrum") or {})]
    out += [f"v({n})" for n in list(summary.get("node_voltages") or {})[:20]]
    out += [f"{s}.max" for s in list(summary.get("signals") or {})[:10] if "max" in (summary["signals"][s] or {})]
    out += [f"{t['device']}.gm" for t in (summary.get("transistors") or [])[:6]]
    return out


def verdict(value: float | None, lo: float | None, hi: float | None) -> dict:
    """{pass, margin, margin_rel} of a value against min / max; the margin is to the nearer violated or closest limit."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return {"pass": None}
    margins = []
    for limit, m in ((lo, None if lo is None else value - lo), (hi, None if hi is None else hi - value)):
        if limit is not None:
            margins.append((m, m / abs(limit) if limit else m))
    if not margins:
        return {"pass": None}
    m, rel = min(margins, key=lambda t: t[1])
    return {"pass": m >= 0, "margin": _r(m), "margin_rel": rel}       # unrounded: worst cases are compared on it


def _r(v: float) -> float:
    return float(f"{v:.4g}")


# ------------------------------------------------------------------ where to measure


def grid(vary: dict | None, corners: list[str] | None, temps: list | None, default_cell: str,
         conditions: dict | None = None) -> tuple[list[dict], list[str]]:
    """The points of a sweep: every combination of the varied values, the conditions, corners and temperatures, in that
    order; and the names of the varied (design) properties. Keys are `inst.prop` (in `default_cell`) or
    `cell/inst.prop`, or `options.<name>` for a simulator option (sim.SIM_OPTIONS: reltol, maxstep ...); values a list,
    or {from, to, steps, scale}."""
    axes, names = [], []
    for key, values in [*(vary or {}).items(), *(conditions or {}).items()]:
        if (o := re.fullmatch(r"options?\.(\w+)", str(key), re.I)):
            opt = o.group(1).lower()
            try:
                vals = [sim.sim_options({opt: v})[opt] for v in _values(key, values)]
            except ValueError as e:
                raise SpecError(f"{key}: {e}") from e
            axes.append([("options", opt, v) for v in vals])
            if key in (vary or {}):
                names.append(f"options.{opt}")
            continue
        m = re.fullmatch(r"(?:([\w.$-]+)/)?([\w.$\[\]-]+)\.([\w-]+)", str(key))
        if not m:
            raise SpecError(f"vary key {key!r}: write instance.property (e.g. M1.W) or cell/instance.property")
        cell, inst, prop = m.group(1) or default_cell, m.group(2), m.group(3)
        axes.append([(cell, inst, prop, v) for v in _values(key, values)])
        if key in (vary or {}):
            names.append(f"{cell}/{inst}.{prop}")
    if corners:
        axes.append([("corner", c) for c in corners])
    if temps:
        axes.append([("temp", float(_num(t))) for t in temps])
    points = []
    for combo in itertools.product(*axes) if axes else [()]:
        pt: dict = {"overrides": {}}
        for a in combo:
            if len(a) == 2:
                pt[a[0]] = a[1]                                 # corner, temp
            elif len(a) == 3:
                pt.setdefault("options", {})[a[1]] = a[2]
            else:
                cell, inst, prop, v = a
                pt["overrides"].setdefault(cell, {}).setdefault(inst, {})[prop] = v
        points.append(pt)
    return points, names


def _values(key: str, values) -> list[str]:
    if isinstance(values, dict):
        try:
            lo, hi, n = _num(values["from"]), _num(values["to"]), int(values.get("steps", 5))
        except (KeyError, TypeError, ValueError) as e:
            raise SpecError(f"vary {key}: a range is {{from, to, steps, scale: lin|log}}") from e
        if n < 2:
            return [_fmt(lo)]
        if values.get("scale") == "log":
            if lo <= 0 or hi <= 0:
                raise SpecError(f"vary {key}: a log range needs positive ends")
            return [_fmt(lo * (hi / lo) ** (i / (n - 1))) for i in range(n)]
        return [_fmt(lo + (hi - lo) * i / (n - 1)) for i in range(n)]
    if not isinstance(values, list) or not values:
        raise SpecError(f"vary {key}: give a list of values or {{from, to, steps}}")
    return [v if isinstance(v, str) else _fmt(v) for v in values]


def _fmt(v: float) -> str:
    return f"{v:.6g}"


def label(pt: dict) -> dict:
    """A point as the caller reads it: {"inv/M1.W": "2", "options.reltol": "1e-6", "corner": "ss", "temp": 125,
    "seed": 3}."""
    out = {f"{cell}/{inst}.{prop}": v for cell, insts in pt["overrides"].items()
           for inst, props in insts.items() for prop, v in props.items()}
    out.update({f"options.{k}": v for k, v in (pt.get("options") or {}).items()})
    for k in ("corner", "temp", "seed"):
        if k in pt:
            out[k] = pt[k]
    return out


def plan(probes_: list[dict], vary: dict | None, corners: list[str] | None, temps: list | None, cell: str,
         conditions: dict | None = None, mismatch: dict | None = None) -> dict:
    """The points to measure: for each spec, every combination of `vary` with the call's corners, temperatures and
    conditions or, where the call gives none, the spec's own; with `mismatch` ({runs, seed}) that many Monte Carlo
    runs of each. Returns {points, design (the varied names), at ({spec id: its point indices}), mismatch ({runs, seed,
    asked}), note}: runs that would take more than MAX_JOBS simulations are cut, and the note says so."""
    _, design = grid(vary, None, None, cell)
    mm = _mismatch(mismatch)

    def build(runs: int) -> tuple[list[dict], dict]:
        points, index, at = [], {}, {}
        for p in probes_:
            pts, _ = grid(vary, corners or p["corners"], temps or p["temps"], cell, conditions or p["conditions"])
            for pt in pts:
                for k in range(runs) if mm else [None]:
                    q = pt if k is None else {**pt, "seed": mm["seed"] + k}
                    key = repr(sorted(label(q).items(), key=lambda t: t[0]))
                    if key not in index:
                        index[key] = len(points)
                        points.append(q)
                    at.setdefault(p["id"], []).append(index[key])
        return points, at
    note = None
    if mm:                  # what one run costs here first: the points are built for the runs that fit, not all asked
        points, at = build(1)
        each = len(_tasks(probes_, points, at))
        if each > MAX_JOBS:
            raise SpecError(f"one mismatch run is {each} simulations here (points x testbench runs), more than the {MAX_JOBS} "
                            "a call runs: check fewer corners, temperatures or conditions at a time")
        if (fit := MAX_JOBS // max(each, 1)) < mm["runs"]:
            note = (f"mismatch: {fit} of the {mm['runs']} runs asked (seeds {mm['seed']} .. {mm['seed'] + fit - 1}): each run "
                    f"is {each} simulations here and a call runs at most {MAX_JOBS}. For more runs, ask again with seed "
                    f"{mm['seed'] + fit} (the next ones), or check fewer corners, temperatures or conditions at a time")
            mm = {**mm, "runs": fit, "asked": mm["runs"]}
    points, at = build(mm["runs"] if mm else 1)
    return {"points": points, "design": design, "at": at, "mismatch": mm, "note": note}


def _mismatch(m) -> dict | None:
    if not m:
        return None
    if not isinstance(m, dict) or set(m) - {"runs", "seed"}:
        raise SpecError("mismatch is {runs: how many Monte Carlo runs per point, seed: the first run's seed (default 1)}")
    try:
        runs, seed = int(m.get("runs", 0)), int(m.get("seed", 1))
    except (TypeError, ValueError) as e:
        raise SpecError("mismatch runs and seed are whole numbers") from e
    if not 1 <= runs <= MAX_MISMATCH_RUNS:
        raise SpecError(f"mismatch runs: 1 .. {MAX_MISMATCH_RUNS} (a sigma needs 20 or more; a call runs those that fit in "
                        f"{MAX_JOBS} simulations and says the seed to go on from)")
    if seed < 1 or seed + runs - 1 > MAX_SEED:
        # ngspice takes no other .option seed: it warns, skips it and draws from the time, so the run is not repeatable
        raise SpecError(f"mismatch seed: 1 .. {MAX_SEED - runs + 1} (ngspice's seeds are 1 .. {MAX_SEED}; runs take seed .. "
                        "seed + runs - 1)")
    return {"runs": runs, "seed": seed}


def _gkey(p: dict) -> tuple:
    """The run a spec is measured in: specs with the same testbench, analysis, output, input and stand-ins share one."""
    return (p["lib"], p["testbench"], repr(p["analysis"]), p["output"] or "", p["input"] or "",
            repr(sorted((p.get("substitute") or {}).items())))


def _tasks(probes_: list[dict], points: list[dict], at: dict) -> list[tuple]:
    """(point index, run key) of every simulation: the runs a point needs for the specs checked there."""
    sets = {pid: set(ix) for pid, ix in at.items()}
    out = []
    for pi in range(len(points)):
        keys = [_gkey(p) for p in probes_ if p["testbench"] and pi in sets.get(p["id"], ())]
        out += [(pi, gk) for gk in dict.fromkeys(keys)]
    return out


# ------------------------------------------------------------------ running


def run(srv, work: str, probes_: list[dict], points: list[dict], wait_s: float, extracted: dict | None = None,
        at: dict | None = None, fresh: bool = False, timeout_s: float | None = None) -> dict:
    """Measures each probe at its points (`at`: {id: point indices}, default every point). Returns {rows: [{point,
    results: {id: {value, pass, margin...} | {error} | {pending}}, score, runs: [{job, testbench, reused?}]}],
    failures, pending}: a point whose job did not finish within `wait_s` is pending (ask again to collect it); a run
    started before this call is `reused` (when it ran). `extracted` ({"lib/cell": extraction job}) simulates those
    cells as extracted from their layouts at every point; `fresh` runs every point again instead of reusing a run.
    `timeout_s`: each run's time limit (sim.time_limit). The runs wait their turn in the server's job queue (sim.Jobs)
    like any other, Monte Carlo runs too: a queued one is pending."""
    at = at if at is not None else {p["id"]: range(len(points)) for p in probes_}
    sets = {pid: set(ix) for pid, ix in at.items()}
    groups: dict[tuple, list[dict]] = {}
    for p in probes_:
        if p["testbench"]:
            groups.setdefault(_gkey(p), []).append(p)
    measures = {gk: _group_meas(ps) for gk, ps in groups.items()}       # the specs of one run share its .meas lines
    tasks = _tasks(probes_, points, at)
    if len(tasks) > MAX_JOBS:
        raise SpecError(f"{len(points)} points x {len(groups)} testbench runs = {len(tasks)} simulations; at "
                        f"most {MAX_JOBS} per call: vary fewer values (coarse first, then refine around the best point)")
    jobs: dict[tuple, object] = {}
    errors: dict[tuple, str] = {}
    t0 = time.time()
    queue, active = list(tasks), []
    while queue or active:
        active = [j for j in active if _running(j)]
        while queue and len(active) < PARALLEL:
            pi, gk = queue.pop(0)
            lib, tb = gk[0], gk[1]
            g = groups[gk][0]
            pt = points[pi]
            try:
                job = jobs[(pi, gk)] = srv.start_sim(
                    work, lib, tb, g["analysis"], measure=measures[gk] or None, overrides=pt["overrides"] or None,
                    corner=pt.get("corner"), temp=pt.get("temp"), reuse=not fresh, meta={"sweep": label(pt)},
                    extracted=extracted or None, save=signals(groups[gk]) or None,
                    options=pt.get("options") or None, mismatch=pt.get("seed"), timeout_s=timeout_s,
                    substitute=g.get("substitute") or None)
                if _running(job):
                    active.append(job)
            except (ValueError, KeyError, RuntimeError) as e:
                errors[(pi, gk)] = str(e).strip("'\"")
        if time.time() - t0 > wait_s:
            break
        if getattr(srv, "stopping", None) is not None and srv.stopping.is_set():
            break                   # a restart (Server.stopping): what runs goes on in the next server, the rest pending
        if active:
            time.sleep(0.3)
    rows, failures, pending = [], [], []
    for pi, pt in enumerate(points):
        results: dict[str, dict] = {}
        runs = []
        for gk, ps in groups.items():
            ps = [p for p in ps if pi in sets.get(p["id"], ())]
            if not ps:
                continue
            job = jobs.get((pi, gk))
            if job is None:
                if (pi, gk) in errors:
                    for p in ps:
                        results[p["id"]] = {"error": errors[(pi, gk)]}
                else:
                    pending.append({"point": label(pt), "testbench": gk[1]})
                    for p in ps:
                        results[p["id"]] = {"pending": True}
                continue
            status = job.status()
            runs.append({"job": job.id, "testbench": gk[1],
                         **({"reused": _when(status)} if status.get("created", t0) < t0 else {})})
            # the stand-ins the run netlisted, and those that replaced nothing: with every result of it (summarize)
            subst = {k: status[k] for k in ("substitute", "substitute_warnings") if status.get(k)}
            st = status.get("state")
            if st in ("queued", "running"):
                pending.append({"point": label(pt), "testbench": gk[1], "job": job.id})
                for p in ps:
                    results[p["id"]] = {"pending": True, "job": job.id, **subst}
                continue
            if st != "done":
                f = simresults.failure(job)
                failures.append({"point": label(pt), "testbench": gk[1], "job": job.id, "errors": f["errors"][:3],
                                 "hints": f["hints"][:2]})
                for p in ps:
                    results[p["id"]] = {"error": f"the simulation failed (job {job.id})", "job": job.id, **subst}
                continue
            summ = simresults.summary(job, None, gk[3] or None, gk[4] or None)
            spectra: dict[str, dict] = {}                           # specs that sample the run alike share one FFT
            trace = tracer(job)
            for p in ps:
                s = summ
                if p["spectrum"]:
                    key = repr(p["spectrum"])
                    if key not in spectra:
                        spectra[key] = simresults.spectrum_of(job, p["spectrum"])
                    s = {**summ, "spectrum": spectra[key]}
                why = None
                try:
                    v = value(s, p["measure"] or "", trace)
                except ValueError as e:
                    v, why = None, str(e)
                if isinstance(v, float) and math.isnan(v):
                    v, why = None, f"{p['measure']} is not a number (NaN) in this run; see the log of job {job.id}"
                lo, hi = limits(p, pt)
                r = {"value": v, **verdict(v, lo, hi), "job": job.id, **subst}
                if (lo, hi) != (p["min"], p["max"]):
                    r["limits"] = {k: x for k, x in (("min", lo), ("max", hi)) if x is not None}
                if v is None:
                    r["error"] = why or _missing(p, s, job.id)
                    if p["missing"] == "fail" and _no_value(p, s):    # the spec counts it as not met there
                        r.update({"pass": False, "no_value": True,
                                  "error": f"no value, which this spec counts as not met (missing: fail): {r['error']}"})
                elif "margin_rel" not in r:
                    r["error"] = "no limit here: the spec gives no min or max at this point"
                elif p["spectrum"] and s["spectrum"].get("warnings"):
                    r["warnings"] = s["spectrum"]["warnings"]
                results[p["id"]] = r
        scored = [r["margin_rel"] for r in results.values() if "margin_rel" in r]
        rows.append({"point": label(pt), "results": results, "runs": runs,
                     **({"score": _r(min(scored)), "all_pass": all(r.get("pass") is True for r in results.values())}
                        if scored else {})})
    return {"rows": rows, "failures": failures, "pending": pending}


def _when(status: dict) -> str:
    t = status.get("finished") or status.get("started") or status.get("created")
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t)) if t else "unknown"


def tracer(job):
    """trace() for derived.evaluate: a transient signal of the job's raw file, which is read once and only when asked."""
    raw: dict = {}

    def trace(signal: str):
        if "raw" not in raw:
            raw["raw"] = simresults.transient_raw(job, "a settling time")
        try:
            return simresults.trace(raw["raw"], signal)
        except ValueError as e:
            raise ValueError(simresults._hinted(job, str(e))) from e
    return trace


def fold(rows: list[dict], design: list[str]) -> list[dict]:
    """One row per design point (the values of the `design` properties), judged by its worst case over the other
    point coordinates (conditions, corners, temperatures, Monte Carlo runs): per spec the measured value with the
    smallest relative margin, and where it was (`worst_at`). A point that failed or gave no value is never replaced by
    another point's value: the spec then has no value (`not_measured_at` and why, `error`; `worst_measured`: the worst
    of the others), and is not met when a measured point fails it, or a point gave no value, which it counts as not met
    (missing: fail: `no_value_at` and why, `no_value_why`), else not known."""
    out: dict[tuple, dict] = {}
    for row in rows:
        key = tuple(row["point"].get(n) for n in design)
        cond = {k: v for k, v in row["point"].items() if k not in design}
        d = out.setdefault(key, {"point": {n: row["point"].get(n) for n in design}, "results": {}, "conditions": 0})
        d["conditions"] += 1
        for sid, r in row["results"].items():
            d["results"].setdefault(sid, []).append((cond, r))
    for d in out.values():
        d["results"] = {sid: _worst(rs) for sid, rs in d["results"].items()}
        scored = [r["margin_rel"] for r in d["results"].values() if "margin_rel" in r]
        if scored:
            d["score"] = _r(min(scored))
            d["all_pass"] = all(r.get("pass") is True for r in d["results"].values())
    return list(out.values())


def _worst(rs: list[tuple[dict, dict]]) -> dict:
    got = [(c, r) for c, r in rs if "margin_rel" in r]
    none = [(c, r) for c, r in rs if r.get("no_value")]                  # no value, which fails the spec (missing: fail)
    miss = [(c, r) for c, r in rs if "margin_rel" not in r and not r.get("no_value")]
    worst = None
    if got:
        c, w = min(got, key=lambda t: t[1]["margin_rel"])
        worst = {**w, **({"worst_at": c} if c else {})}
    if not miss and not none:
        return worst
    out = {"value": None, "pass": None}
    if miss:                                        # each set of points with its own reason
        out.update({"not_measured_at": [c for c, _ in miss],
                    "error": next((r["error"] for _, r in miss if r.get("error")), "still running: ask again to collect it")})
    if none:                                        # not met there, whatever the others give: no value is the worst
        c, r = none[0]
        out.update({"pass": False, "no_value_at": [c for c, _ in none], "no_value_why": r["error"], "job": r["job"],
                    **({"worst_at": c} if c else {})})
    if worst:
        out["worst_measured"] = {"value": worst["value"], **({"at": worst["worst_at"]} if "worst_at" in worst else {})}
        if worst["pass"] is False and not none:     # a measured point fails it: not met, whatever the others give
            out.update({k: worst[k] for k in ("pass", "margin", "margin_rel", "job", "worst_at") if k in worst})
    return out


def _running(job) -> bool:
    return job.status().get("state") in ("queued", "running")


def _group_meas(ps: list[dict]) -> list[str]:
    """The .meas lines of specs that share a run, each once; two different statements for one name are refused."""
    lines, by_name = [], {}
    for p in ps:
        for line in p["meas"]:
            name = next(iter(meas_names([line])))
            if by_name.get(name, line) != line:
                raise SpecError(f"two specs on testbench {p['testbench']} define .meas {name} differently; give them different "
                                "names (and measure)")
            if name not in by_name:
                by_name[name] = line
                lines.append(line)
    return lines


def _no_value(p: dict, summ: dict) -> bool:
    """Whether a finished run has no value of the spec's measure because the design never did what its .meas waits
    for: ngspice says the .meas (or one a derived measure reads) is "out of interval", a TRIG / TARG / WHEN level that
    never crossed, or it is a param= of one that is. Not a .meas that could not run: a node the run does not have ("no
    such vector"), a name no .meas gives, an analysis that does not run (no measurement at all); the spec is wrong."""
    m = p["measure"] or ""
    names = derived.references(m)[0] if derived.is_derived(m) else [m]
    failed = {k.lower(): v for k, v in (summ.get("measurement_errors") or {}).items()}
    return any(_never_crossed(n.lower(), failed) for n in names)


def _never_crossed(name: str, failed: dict[str, str], seen: tuple[str, ...] = ()) -> bool:
    """Whether ngspice's reason for the .meas `name` (failed: measurement_failures) is a level never crossed, or an
    undefined parameter that is such a .meas (param='tclr * 2' of a tclr that never triggered)."""
    why = failed.get(name) or ""
    if "out of interval" in why:
        return True
    m = re.search(r"undefined parameter \[([^\]]+)\]", why, re.I)
    return bool(m) and (u := m.group(1).strip().lower()) not in seen and _never_crossed(u, failed, (*seen, name))


def _missing(p: dict, summ: dict, job: str) -> str:
    name = (p["measure"] or "").lower()
    if (why := (summ.get("measurement_errors") or {}).get(name)):
        return f"{meas_failed(p['measure'], why)}; see the log of job {job}"
    if p["meas"] and name in meas_names(p["meas"]):
        return f"its .meas {p['measure']} gave no value (a wrong node, or an analysis that does not run); see the log of job {job}"
    if p["measure"] in AC_FIGURES and not p["output"]:
        return f"{p['measure']} is an AC figure: the spec needs output (the output node), and analysis {{type: ac, ...}} " \
               "unless the testbench runs an AC analysis itself"
    if p["measure"] in AC_FIGURES and "metrics" not in summ:
        return f"{p['measure']} is an AC figure, but this run is {summ.get('analysis', 'not an AC analysis')}: " \
               "give the spec analysis {type: ac, ...}"
    if name in spectrum.FIGURES and not p["spectrum"]:
        return f"{p['measure']} is a spectral figure: the spec needs spectrum {{signal or bits, fs, points, start}} (how to " \
               "sample its transient run), and analysis {type: tran, ...} unless the testbench runs one"
    if name in spectrum.FIGURES and (why := (summ.get("spectrum") or {}).get("error")):
        return f"its spectrum could not be taken: {why} (job {job})"
    if name == "signal_dbfs":
        return "signal_dbfs needs full_scale in the spec's spectrum (the peak-to-peak range: volts, or 2^N codes)"
    return f"this run has no measure {p['measure']!r}; it has: {', '.join(available(summ)[:24]) or 'nothing to read'} " \
           "(or add a .meas statement of that name to the testbench)"




def summarize(probes_: list[dict], res: dict) -> list[dict]:
    """Per spec, over the points it was checked at: the worst measured value against its limits, and how many points
    meet it. A spec failed at a measured point is not met; met where it was measured, but not measured everywhere
    (a failed run, no value, still running), it is not fully measured rather than met: such a point may be the worst,
    so it is listed (`not_measured_at`, `why`) and never stands in for a value. A spec that counts no value as a
    failure (missing: fail) is not met where its .meas never triggered (`no_value_at`, `no_value_why`: "no value ..."),
    each set of points with its own reason. A spec measured with stand-ins says which (`substitute`, and
    `substitute_warnings` for one that replaced nothing)."""
    out = []
    for p in probes_:
        vals = [(row["point"], row["results"][p["id"]]) for row in res["rows"] if p["id"] in row["results"]]
        got = [(pt, r) for pt, r in vals if "margin_rel" in r]
        none = [(pt, r) for pt, r in vals if r.get("no_value")]
        miss = [(pt, r) for pt, r in vals if "margin_rel" not in r and not r.get("no_value")]
        s = {"id": p["id"], "text": p["text"], "measure": p["measure"], "min": p["min"], "max": p["max"],
             "testbench": f"{p['lib']}/{p['testbench']}" if p["testbench"] else None}
        # measured with stand-ins (a behavioural model in place of a cell): a reader of the value must know
        if (sub := next((r for _, r in vals if r.get("substitute") or r.get("substitute_warnings")), None)):
            s.update({k: sub[k] for k in ("substitute", "substitute_warnings") if k in sub})
        if p.get("at"):
            s["limits_at"] = {}
            for (kind, where_), lim in p["at"].items():
                s["limits_at"].setdefault(kind, {})[where_ if kind == "corner" else f"{where_:g}"] = lim
        if not p["testbench"]:
            s["status"] = "no testbench: the spec names none (add testbench to it)"
        elif got:
            pt, worst = min(got, key=lambda t: t[1]["margin_rel"])
            met = all(r["pass"] for _, r in got) and not none
            s.update({"status": "not met" if not met else "not fully measured" if miss else "met",
                      "worst": worst["value"], "margin": worst["margin"], "worst_at": pt,
                      "points_met": f"{sum(r['pass'] for _, r in got)}/{len(vals)}", "job": worst["job"],
                      **({"limits_at_worst": worst["limits"]} if worst.get("limits") else {}),
                      **({"warnings": worst["warnings"]} if worst.get("warnings") else {})})
        elif none:
            s.update({"status": "not met", "worst": None, "worst_at": none[0][0], "points_met": f"0/{len(vals)}",
                      "job": none[0][1]["job"]})
        else:
            s["status"] = "not measured"
        if p["testbench"] and miss:
            if got or none:
                s["not_measured_at"] = [pt for pt, _ in miss][:8]
            if (errs := [r["error"] for _, r in miss if r.get("error")]):
                s["why"] = errs[0]
            if (n := sum(1 for _, r in miss if r.get("pending"))):
                s["running"] = n
        if none:                                    # not met for no value, with its own reason (why: the others')
            s.update({"no_value_at": [pt for pt, _ in none][:8], "no_value_why": none[0][1]["error"]})
        out.append(s)
    return out


def table(rows: list[dict], probes_: list[dict]) -> list[dict]:
    """The rows of a check as the caller reads them, compactly (a tool result is cut at a few thousand characters): per
    point the value of each spec there (None where it has none, with why in not_measured), its score, the specs not
    met, its job (jobs: per testbench, or per run of it with the specs it measured when a testbench ran more than once
    there: another analysis, other stand-ins) and, when its runs were reused, when the first of them ran (reused)."""
    out = []
    for r in rows:
        res = r["results"]
        row = {"point": r["point"], **({"score": r["score"]} if "score" in r else {}),
               "values": {p["id"]: res[p["id"]].get("value") for p in probes_ if p["id"] in res},
               "not_met": [k for k, v in res.items() if v.get("pass") is False]}
        if (miss := {k: v.get("error") or "still running" for k, v in res.items()
                     if "margin_rel" not in v and not v.get("no_value")}):
            row["not_measured"] = miss
        if (none := [k for k, v in res.items() if v.get("no_value")]):
            row["no_value"] = none                  # in not_met: their .meas gave no value (missing: fail)
        runs = r.get("runs") or []
        if len(runs) == 1:
            row["job"] = runs[0]["job"]
        elif runs:
            tbs = [x["testbench"] for x in runs]
            row["jobs"] = {x["testbench"] if tbs.count(x["testbench"]) == 1 else
                           f"{x['testbench']} ({', '.join(k for k, v in res.items() if v.get('job') == x['job'])})":
                           x["job"] for x in runs}
        if (when := [x["reused"] for x in runs if "reused" in x]):
            row["reused"] = min(when)
        out.append(row)
    return out


def runs_summary(res: dict) -> dict:
    """How many of a call's runs were new and how many reused from earlier ones on the same inputs (and when those
    ran)."""
    runs = [x for row in res["rows"] for x in row.get("runs") or []]
    reused = sorted(x["reused"] for x in runs if "reused" in x)
    out = {"new": len(runs) - len(reused), "reused": len(reused)}
    if reused:
        out["reused_ran"] = reused[0] if reused[0] == reused[-1] else f"{reused[0]} .. {reused[-1]}"
    return out


def mismatch_stats(probes_: list[dict], rows: list[dict]) -> list[dict]:
    """Per group of Monte Carlo runs (one point but for its seed) and spec: how many runs, the mean, sigma, min and
    max of the values, the yield (the fraction of the finished runs that meet the spec there: a run that failed or gave
    no value does not; one still running is not counted until it finishes, `pending`, and with none finished the yield
    is None), how many sigmas the mean is from the nearer limit, and the worst run (its seed and job, to look at). A
    spec with missing: fail counts the runs whose .meas never triggered (`no_value`), and names the first of them
    (`no_value_runs`: seed and job): such a run fails it, however the measured ones do."""
    groups: dict[str, dict] = {}
    for row in rows:
        if "seed" in row["point"]:
            at = {k: v for k, v in row["point"].items() if k != "seed"}
            groups.setdefault(repr(sorted(at.items(), key=lambda t: t[0])), {"at": at, "rows": []})["rows"].append(row)
    out = []
    for g in groups.values():
        per: dict[str, dict] = {}
        for p in probes_:
            rs = [(row["point"]["seed"], row["results"][p["id"]]) for row in g["rows"] if p["id"] in row["results"]]
            if not rs:
                continue
            got = [(seed, r) for seed, r in rs if "margin_rel" in r]
            met = sum(1 for _, r in got if r["pass"])
            pending = sum(1 for _, r in rs if r.get("pending"))
            done = len(rs) - pending
            st: dict = {"runs": len(rs), "met": met, "yield": _r(met / done) if done else None}
            if pending:
                st["pending"] = pending
            if got:
                vals = [r["value"] for _, r in got]
                mean = sum(vals) / len(vals)
                sigma = math.sqrt(sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)) if len(vals) > 1 else None
                st.update({"mean": _r(mean), "sigma": _r(sigma) if sigma is not None else None,
                           "min": _r(min(vals)), "max": _r(max(vals))})
                lo, hi = limits(p, g["at"])
                if sigma and (dist := [x for x in ((hi - mean) if hi is not None else None,
                                                    (mean - lo) if lo is not None else None) if x is not None]):
                    st["sigmas_to_limit"] = _r(min(dist) / sigma)
                seed, w = min(got, key=lambda t: t[1]["margin_rel"])
                st.update({"worst_seed": seed, "worst_job": w["job"]})
            nv = [(seed, r) for seed, r in rs if r.get("no_value")]         # which fail the spec (missing: fail)
            if nv:
                st.update({"no_value": len(nv), "no_value_runs": [{"seed": seed, "job": r.get("job")}
                                                                  for seed, r in nv[:NO_VALUE_RUNS]]})
            if (n := done - len(got) - len(nv)):
                st["not_measured"] = n              # failed, or no value: not met
            per[p["id"]] = st
        out.append({"at": g["at"], "specs": per})
    return out
