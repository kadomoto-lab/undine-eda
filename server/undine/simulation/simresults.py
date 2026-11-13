"""What a simulation tells an agent: the numbers a designer reads off the plots and the log.

`summary(job, ...)` turns a finished ngspice job into
* op:   every node voltage, and each transistor's bias (id, vgs, vds, vth, vdsat, gm, gds) with its region
        (saturation / linear / subthreshold) and the margin vds - vdsat;
* ac:   for `output` (over `input`, or over the 1 V AC source): low-frequency gain, -3 dB frequency, unity-gain
        frequency, phase margin, gain margin, and gain / phase at each decade;
* tran / dc: per signal min, max, initial, final, steepest slope and a short sampled waveform;
* with `spectrum`, a transient sampled as clocked (an ADC's codes, a sample-and-hold's output): SNDR, SNR, THD,
  SFDR, ENOB (spectrum.py);
* any `.meas` results and the measures that failed (with why and their statement), the netlister's notes and
  warnings, the cells simulated as extracted or as stand-ins (`substitute`: a behavioural model in place of a cell), and
  the log's error lines of a run that still finished.
`failure(job)` gives the error lines of the log and what they usually mean; `aborted(log)` whether the last analysis
stopped short (ngspice's exit code does not say so after a control block's `quit`); `pending(job)` what a run that has
not finished is doing (queued, or running for how long) and how to collect it later.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import numpy as np

from undine.simulation import sim, spectrum as spectral

NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
MAX_SIGNALS = 24
SAMPLES = 40


# ------------------------------------------------------------------ the log


def device_bias(log: str) -> list[dict]:
    """The tables `show m : id vgs ...` prints: one column per transistor, one row per quantity."""
    out: dict[str, dict] = {}
    names: list[str] = []
    for line in log.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "device":
            names = parts[1:]
            for n in names:
                out.setdefault(n, {})
        elif names and len(parts) == len(names) + 1 and re.fullmatch(r"[a-z]+", parts[0]):
            for n, v in zip(names, parts[1:]):
                try:
                    out[n][parts[0]] = float(v)
                except ValueError:
                    pass
        else:
            names = []
    rows = []
    for raw_name, q in out.items():
        if "vds" not in q:
            continue
        # m.x1.xm7.msky130_fd_pr__nfet... (the name column is cut): the instance path is between "m." and the model
        bits = raw_name.split(".")
        path = ".".join(bits[1:-1]) if len(bits) > 2 else raw_name
        vgs, vds, vth, vdsat = (abs(q.get(k, 0.0)) for k in ("vgs", "vds", "vth", "vdsat"))
        region = ("subthreshold" if vth and vgs < vth else "linear" if vds < vdsat else "saturation")
        rows.append({"device": path, **{k: _r(v) for k, v in q.items()}, "region": region, "vds_minus_vdsat": _r(vds - vdsat)})
    return sorted(rows, key=lambda r: r["device"])


MEAS_ERROR = re.compile(r"^\s*Error:\s*measure\s+(\S+)\s+(.*)$", re.I)
# what ngspice prints under "Measurements for ...": results, and for a failed measure its error lines and statement
_MEAS_OUTPUT = re.compile(r"^\s*(?:$|[A-Za-z_][\w.]*\s*=|Error\b|\.meas|insertnumber:|s\s*=\s*\")", re.I)
# and before "name = failed" of a param= it cannot compute (one that uses a failed measure): the expression's diagnostics
_MEAS_DIAG = re.compile(r"^\s*(?:(?:Error in )?netlist line no\.|Undefined parameter|Expression err|Cannot compute substitute|"
                        r"Closing .* not found|Syntax error|(?:[=;] sign|identifier) expected)", re.I)
_LINE_NO = re.compile(r"^\s*(?:Error in )?netlist line no\.", re.I)


def _meas_blocks(log: str) -> list[list[tuple[int, str]]]:
    """The (index, line) of each "Measurements for ..." section, up to the first line of something else. There are two
    per analysis when a testbench's own .control runs it and ngspice's batch mode runs it again after the block."""
    blocks: list[list[tuple[int, str]]] = []
    cur = None
    for i, line in enumerate(log.splitlines()):
        if "Measurements for" in line:
            cur = []
            blocks.append(cur)
        elif cur is not None and (_MEAS_OUTPUT.match(line) or _MEAS_DIAG.match(line)):
            cur.append((i, line))
        else:
            cur = None
    return blocks


def measurements(log: str) -> dict[str, float]:
    """Results of .meas statements (ngspice prints them under "Measurements for ..."). A failed one prints an error
    and its statement among them; the results after it still count."""
    out = {}
    for block in _meas_blocks(log):
        for _, line in block:
            if (m := re.match(rf"^\s*([A-Za-z_][\w.]*)\s*=\s*({NUM})", line)):
                out[m.group(1)] = float(m.group(2))
    return out


def measurement_failures(log: str) -> dict[str, str]:
    """{name: why (statement)} of the .meas statements ngspice could not evaluate: a TRIG / TARG level never crossed
    ("trig(TARG) : out of interval"), a vector the run does not have ("no such vector as v(a,b)"), a param= that could
    not be computed ("Undefined parameter [tpd]": it uses a measure that failed). A measure that has a value from another
    evaluation is left out: when a testbench's own .control runs the analysis, ngspice's batch mode can evaluate every
    .meas once more after it, and a param= fails there."""
    ok = {k.lower() for k in measurements(log)}
    out: dict[str, str] = {}
    for block in _meas_blocks(log):
        why = stmt = None
        for _, line in block:
            text = " ".join(line.split())
            if (m := MEAS_ERROR.match(line)):
                why = " ".join(m.group(2).split())
            elif (m := re.match(r"^Error:\s*(.*?)\.?$", text, re.I)):
                why = m.group(1)
            elif text.lower().startswith("insertnumber"):
                why = "its value could not be computed (" + text.rstrip(".") + ")"
            elif _MEAS_DIAG.match(text):
                if not _LINE_NO.match(text):            # the first says why: "Undefined parameter [tpd]"
                    why = why or text.rstrip(".")
            elif (m := re.match(r'^s\s*=\s*"\s*(\.meas[^"]*?)\s*"', text, re.I)):
                stmt = m.group(1)
            elif (m := re.match(r"^(\.meas(?:ure)?\s+\w+\s+(\S+)\s.*?)\s*failed!$", text, re.I)):
                out.setdefault(m.group(2).lower(), f"{why or 'failed'} ({m.group(1)})")
                why = stmt = None
            elif (m := re.match(r"^([A-Za-z_][\w.]*)\s*=\s*failed\b", text, re.I)):
                out.setdefault(m.group(1).lower(), (why or "failed") + (f" ({stmt})" if stmt else ""))
                why = stmt = None
            elif re.match(r"^[A-Za-z_][\w.]*\s*=", text):  # a result: what came before it was its own
                why = stmt = None
    return {k: v for k, v in out.items() if k not in ok}


ERROR_LINE = re.compile(r"\b(error|fatal)\b|^\s*doAnalyses:|simulation\(s\) aborted", re.I)
# an analysis that stopped short: its time step collapsed, a model refused a device ("Fatal:" of its parameter check)
ABORT = re.compile(r"simulation\(s\) aborted|^doAnalyses:|timestep too small|^Fatal error: detected during|^Fatal:", re.I)


def log_errors(log: str) -> list[str]:
    """The error lines of a log, but those of .meas evaluation (measurement_failures reports them per measure)."""
    skip = {i for block in _meas_blocks(log) for i, _ in block}
    return [l for i, l in enumerate(log.splitlines()) if i not in skip and ERROR_LINE.search(l)]


def aborted(log: str) -> str | None:
    """The line saying that the log's last analysis did not finish (from its "Doing analysis" on; an earlier one that
    aborted, in a testbench's loop, leaves only warnings, as ngspice's exit code has it), or None. When it is ngspice's
    reason ("doAnalyses: TRAN: Timestep too small ..."), the last error line of the analysis before it is added: what
    made it stop ("Error: -0.001 out of range for sqrt")."""
    lines = log.splitlines()
    skip = {i for block in _meas_blocks(log) for i, _ in block}
    start = max((i for i, l in enumerate(lines) if l.startswith("Doing analysis at")), default=0)
    hits = [(i, " ".join(l.split())) for i, l in enumerate(lines[start:], start) if i not in skip and ABORT.search(l.strip())]
    # the most telling: the model's complaint, else ngspice's reason ("doAnalyses: TRAN: Timestep too small; time = ...")
    i, hit = next(((i, h) for p in ("Fatal:", "doAnalyses:", "") for i, h in hits if h.startswith(p)), (None, None))
    if hit is None or hit.startswith("Fatal"):
        return hit
    before = [" ".join(l.split()) for k, l in enumerate(lines[start:i], start)
              if k not in skip and ERROR_LINE.search(l) and not ABORT.search(l.strip())]
    return f"{hit} (after: {before[-1]})" if before else hit


MODEL_CHECK = re.compile(r"Checking parameters for BSIM \S+ model (\S+?):(\S+)")

HINTS = [
    (r"unknown subckt|unknown model|could not find a valid modelname",
     "a device has no model: the testbench needs the PDK models (place the PDK corner symbol, e.g. sky130_fd_pr/corner.sym, "
     "or a code block with .lib), and every subcircuit must have a schematic"),
    (r"operating point could not be simulated|op failed|timestep too small(?!;\s*time\s*=)|no convergence|"
     r"iterations without convergence|gmin stepping failed",
     "the DC operating point does not converge: look for floating nodes or a loop without a DC path, check that every "
     "device can carry its bias current, or help with .nodeset / .ic in a code block"),
    (r"TRAN:\s*Timestep too small|timestep too small;\s*time\s*=",
     "the transient stopped before its end: it stopped part way, at the time the line gives (its values end there and "
     "are not used), where the solver could not take a small enough step. Look at the error lines before it (an "
     "expression that cannot be evaluated, a B-source with a step or out of its range), check that every node has a DC "
     "path and some capacitance ('trouble with node' names one that changes faster than any step can follow): smooth "
     "the edge, add a little capacitance, or try a smaller step or tmax (options {maxstep}), or options {method: gear}"),
    (r"is not positive|parameter.*out of range|Fatal: .*BSIM|Fatal error: detected during BSIM",
     "a device size is outside what its model supports (e.g. in sky130 W per finger and L combinations with nf > 1): "
     "use nf=1 and parallel devices (mult) or another W / L"),
    (r"singular matrix", "the circuit has a node with no DC path to ground (a floating node or a capacitor-only node)"),
    (r"no such vector as v\([^)]*,", "ngspice's .meas takes one node per v(): measure a difference as par('v(a)-v(b)') "
                                     "or as two .meas"),
    (r"no such vector|vector .* not found", "a .meas / .control statement names a node that does not exist"),
    (r"more than \d+ function calls to par", "ngspice takes at most 99 par() calls in one run (Undine turns those of .meas "
                                             "lines into B sources, so these are in a .control block): measure v(a) and v(b) "
                                             "on their own, or use spectrum for a sampled waveform"),
]
SIZE_HINT = next(h for pat, h in HINTS if pat.startswith("is not positive"))


def _instance(netlist: str, path: str) -> str | None:
    """The element line of instance `path` as ngspice names it (x1.xm7: xm7 in the subcircuit x1 calls)."""
    lines: list[str] = []
    for raw in netlist.splitlines():
        if raw.startswith("+") and lines:
            lines[-1] += " " + raw[1:]
        else:
            lines.append(raw)
    scopes: dict[str, list[str]] = {"": []}
    cur = ""
    for ln in (x.strip() for x in lines):
        low = ln.lower()
        if low.startswith(".subckt") and len(ln.split()) > 1:
            cur = ln.split()[1].lower()
            scopes[cur] = []
        elif low.startswith(".ends"):
            cur = ""
        elif ln and ln[0] not in "*.+":
            scopes[cur].append(ln)
    scope, parts = scopes[""], path.lower().split(".")
    for k, name in enumerate(parts):
        line = next((ln for ln in scope if ln.split()[0].lower() == name), None)
        if line is None or k == len(parts) - 1:
            return line
        calls = [x for x in line.split()[1:] if "=" not in x]
        scope = scopes.get(calls[-1].lower() if calls else "", [])
    return None


def _size(line: str) -> str:
    """'W=10 L=0.15 nf=2 (5 per finger)' of a transistor's element line."""
    p = {k.lower(): v for k, _, v in (t.partition("=") for t in line.split()[1:] if "=" in t)}
    out = " ".join(f"{k}={p[k.lower()]}" for k in ("W", "L", "nf") if k.lower() in p)
    try:
        nf = float(p.get("nf", 1))
        if nf > 1:
            out += f" ({float(p['w']) / nf:g} per finger)"
    except (KeyError, ValueError):
        pass
    return out


def failure(job: sim.Job, st: dict | None = None) -> dict:
    """`st`: the job's status as the server sees it (sim.Jobs.status: "interrupted"), else its file."""
    log = job.log(3000)
    seen: dict[str, str] = {}                # each line once (as the log has it), the error lines of .meas left out
    for line in log_errors(log):
        seen.setdefault(" ".join(line.split()), line.strip())
    errs = list(seen.values())[-8:]
    st = st or job.status()
    if st.get("timed_out") or st.get("state") in ("cancelled", "interrupted"):
        errs = [st.get("error") or "the simulation was cancelled"]
    hints = [h for pat, h in HINTS if any(re.search(pat, e, re.I) for e in errs)]
    # a device whose size its model refuses at this corner: "Checking parameters for BSIM 4.5 model x4.xms:...model.8",
    # then "Fatal: Pclm = -0.00098 is not positive" (a Warning there is harmless and is not reported): name the device,
    # its size and its model bin
    devices, lines = [], log.splitlines()
    for i, line in enumerate(lines):
        if (m := MODEL_CHECK.search(line)):
            fatal = next((l.strip() for l in lines[i + 1:i + 8] if l.strip().startswith("Fatal:")), None)
            if fatal:
                inst = _instance((job.path / "netlist.cir").read_text(errors="replace"), m.group(1)) \
                    if (job.path / "netlist.cir").is_file() else None
                size = _size(inst) if inst else ""
                devices.append(f"{m.group(1)} ({m.group(2)}{', ' + size if size else ''}): {fatal}")
    out = {}
    if devices:
        out["model_check"] = list(dict.fromkeys(devices))[:8]
        hints = [h for h in hints if h != SIZE_HINT]
        hints.insert(0, "a transistor's size is outside what its model supports at this corner (" + "; ".join(out["model_check"][:3])
                     + "). Its model bin comes from W and L, and the model is evaluated per finger: with nf > 1 use nf=1 "
                       "(the same W; parallel devices, mult, in the layout) or another W / L")
    return {"state": st.get("state"), "job": job.id, "errors": errs or [st.get("error") or "the simulation did not finish"],
            "hints": list(dict.fromkeys(hints)), **out, **_notes(st)}


def pending(job: sim.Job, lib: str, cell: str, limit: int | None = None, changeset: str | None = None) -> dict:
    """What a run that has not finished yet is doing, and how to follow it, with what start_sim already said of it (its
    notes, netlist warnings, the cells simulated as extracted or as stand-ins, and those that replaced nothing).
    `changeset`: the run is in another's changeset that the caller reads: each call it names passes it."""
    cs = f", changeset='{changeset}'" if changeset else ""
    st = job.status()
    out = {"job": job.id, "state": st.get("state")}
    if st.get("state") == "queued":
        out["queue_position"] = st.get("queue_position")
        out["note"] = (f"waiting for its turn: {limit or 'a few'} simulations run at once on this server, the others wait "
                       "in order")
    else:
        out["elapsed_s"] = round(time.time() - (st.get("started") or time.time()))
        out["note"] = "still running"
    if st.get("time_limit_s"):
        out["time_limit_s"] = st["time_limit_s"]
    out.update(_notes(st))
    out["next"] = (f"wait_for(events=['job:{job.id}'], timeout_s=...{cs}) until it ends, then run_simulation(lib='{lib}', "
                   f"cell='{cell}', job='{job.id}'{cs}) for its results (signals / output / spectrum / sample / measures "
                   f"read off the run, as you like); cancel_job(job='{job.id}'{cs}) stops it")
    return out


def _notes(st: dict) -> dict:
    out = {}
    if st.get("notes"):
        out["notes"] = st["notes"]
    if st.get("netlist_warnings"):
        out["netlist_warnings"] = st["netlist_warnings"][:10]
    if st.get("extracted"):                 # cells simulated as extracted from their layouts (Server.start_sim)
        out["extracted"] = st["extracted"]
    for k in ("substitute", "substitute_warnings"):     # cells netlisted as stand-ins, and those that replaced nothing
        if st.get(k):
            out[k] = st[k]
    return out


# ------------------------------------------------------------------ the raw file


def _r(v: float, digits: int = 4) -> float:
    return float(f"{v:.{digits}g}")


def own_first(names: list[str]) -> list[str]:
    """Vectors in the order a person looks for them: the testbench's own nets, nets inside instances (x1.b9),
    currents, then device internals (#body ...)."""
    def key(n: str):
        current = n.lower().startswith("i(") or n.lower().endswith("#branch")
        return "#" in n and not current, current, "." in n
    return sorted(names, key=key)


def select(names: list[str], wanted: list[str] | None) -> tuple[list[str], list[str]]:
    """The vectors `wanted` names (a node as out or v(out), x1.b9, i(v1); any case) and the names not found; without
    `wanted`, up to MAX_SIGNALS node voltages, the testbench's own first."""
    volts = [n for n in names if n.startswith("v(") and "#" not in n]
    if wanted:
        norm = {n.lower(): n for n in names}
        pick = [norm.get(w.lower()) or norm.get(f"v({w.lower()})") for w in wanted]
        return [p for p in pick if p], [w for w, p in zip(wanted, pick) if not p]
    # the testbench's own nets first: an extracted view brings many nodes inside its instance (x1.out.n0 ...)
    return sorted(volts, key=lambda n: "." in n)[:MAX_SIGNALS], []


def ac_metrics(f: np.ndarray, h: np.ndarray) -> dict:
    mag = 20 * np.log10(np.maximum(np.abs(h), 1e-30))
    ph = np.unwrap(np.angle(h)) * 180 / np.pi
    lag = ph - ph[0]                                     # phase relative to low frequency
    out = {"dc_gain_db": _r(mag[0]), "phase_low_freq_deg": _r(ph[0])}
    below = np.where(mag < mag[0] - 3)[0]
    if len(below):
        out["f_3db_hz"] = _r(_cross(f, mag, below[0], mag[0] - 3))
    if mag[0] > 0 and (i := np.where(mag < 0)[0]).size:
        fu = _cross(f, mag, i[0], 0.0)
        out["unity_gain_freq_hz"] = _r(fu)
        out["phase_margin_deg"] = _r(180 + float(np.interp(fu, f, lag)))
    if (j := np.where(lag <= -180)[0]).size:
        f180 = _cross(f, lag, j[0], -180.0)
        out["gain_margin_db"] = _r(-float(np.interp(f180, f, mag)))
    dec = []
    for k in range(int(np.floor(np.log10(f[0]))), int(np.ceil(np.log10(f[-1]))) + 1):
        if f[0] <= 10 ** k <= f[-1]:
            dec.append({"f_hz": _r(10 ** k), "gain_db": _r(float(np.interp(10 ** k, f, mag))),
                        "phase_deg": _r(float(np.interp(10 ** k, f, ph)))})
    out["per_decade"] = dec
    return out


def _cross(x: np.ndarray, y: np.ndarray, i: int, level: float) -> float:
    if i == 0:
        return float(x[0])
    x0, x1, y0, y1 = x[i - 1], x[i], y[i - 1], y[i]
    if y1 == y0:
        return float(x1)
    # interpolate on a log frequency axis
    lx = np.log10(x0) + (level - y0) / (y1 - y0) * (np.log10(x1) - np.log10(x0))
    return float(10 ** lx)


def _samples(x: np.ndarray, y: np.ndarray) -> list[list[float]]:
    idx = np.unique(np.linspace(0, len(x) - 1, min(SAMPLES, len(x))).astype(int))
    return [[_r(float(x[i])), _r(float(y[i]))] for i in idx]


def transient_raw(job: sim.Job, what: str = "a spectrum") -> dict:
    """The job's transient raw file (sim.read_raw); ValueError says what the run is instead."""
    raws = job.raw_files()
    for path in raws:
        raw = sim.read_raw(path)
        if "transient" in raw["plot"].lower():
            return raw
    raise ValueError(f"{what} is taken from a transient run; this one " + (f"is {raw['plot']}" if raws else "wrote no "
                     "raw file") + ": run analysis {type: tran, ...}, or a testbench whose .control writes a transient raw file")


def trace(raw: dict, name: str) -> tuple[np.ndarray, np.ndarray]:
    """(time, values) of a signal of a raw file: a node, v(node) or i(source); ValueError when the run has no such
    signal."""
    names = [n for n, _ in raw["vars"]]
    pick, missing = select(names[1:], [name])
    if missing:
        raise ValueError(f"no signal {name} in this run; it has "
                         f"{', '.join(own_first([n for n in names[1:] if '#' not in n]))[:600]}")
    return np.real(raw["data"][names[0]]), np.real(raw["data"][pick[0]])


UNSAVED = ("this run stored only the testbench's nets and the signals named when it ran, not the nodes inside its "
           "cells: run it again naming them in signals (or with save_all)")


def unsaved(job: sim.Job, missing) -> str:
    """Why a signal is missing from a run that stored only the testbench's nets (Server.start_sim: "saved"), or ""."""
    return UNSAVED if missing and job.status().get("saved") == "testbench" else ""


def _hinted(job: sim.Job, msg: str) -> str:
    """A "no signal" error with UNSAVED where the run did not store the nodes inside its cells."""
    return f"{msg} ({hint})" if msg.startswith("no signal") and (hint := unsaved(job, [msg])) else msg


def spectrum_of(job: sim.Job, cfg: dict) -> dict:
    """The spectral figures of a request (spectrum.config) on the job's transient raw file, or {error}."""
    try:
        return spectral.measure(transient_raw(job, "a spectrum"), cfg)
    except ValueError as e:
        return {"error": _hinted(job, str(e))}


SAMPLE_FIELDS = ("signals", "times", "start", "fs", "period", "points")
MAX_SAMPLES = 1 << 16


def sample_config(spec) -> dict:
    """A sample request checked, numbers read (SI suffixes too): {signals, times} from {signals, times: [t, ...]} or
    {signals, start?, fs or period, points} (start + k / fs, k = 0 .. points-1, as spectrum samples). ValueError
    says what to change."""
    if not isinstance(spec, dict):
        raise ValueError("sample is {signals, times: [...]} or {signals, start, fs or period, points}")
    if (bad := sorted(set(spec) - set(SAMPLE_FIELDS))):
        raise ValueError(f"sample: unknown {', '.join(bad)}; it takes {', '.join(SAMPLE_FIELDS)}")
    sig = [spec["signals"]] if isinstance(spec.get("signals"), str) else spec.get("signals")
    if not isinstance(sig, list) or not sig or not all(isinstance(s, str) and s.strip() for s in sig):
        raise ValueError("sample: signals are the nodes to sample, e.g. ['outp', 'outn'] or ['x1.b9']")
    num = lambda v, what: spectral._num(v, what, "sample")                        # noqa: E731
    if spec.get("times") is not None:
        if (both := [k for k in ("start", "fs", "period", "points") if spec.get(k) not in (None, "")]):
            raise ValueError(f"sample: give times, or start / fs / points, not both ({', '.join(both)})")
        times = spec["times"] if isinstance(spec["times"], list) else [spec["times"]]
        times = [num(t, "times") for t in times]
    else:
        n = spectral._int(spec.get("points"), "points", "sample")
        if n is None or not 1 <= n <= MAX_SAMPLES:
            raise ValueError(f"sample: points is from 1 to {MAX_SAMPLES} (or give times)")
        fs, period = spec.get("fs"), spec.get("period")
        if n > 1 and (fs in (None, "")) == (period in (None, "")):
            raise ValueError("sample: give the sample rate fs (e.g. '10Meg') or the period (e.g. '100n'), one of them")
        rate = 1.0 if n == 1 else num(fs, "fs") if fs not in (None, "") else 1.0 / num(period, "period")
        times = list(num(spec.get("start") or 0, "start") + np.arange(n) / rate)
    if not 1 <= len(times) <= MAX_SAMPLES:
        raise ValueError(f"sample: from 1 to {MAX_SAMPLES} times")
    return {"signals": [s.strip() for s in sig], "times": [float(t) for t in times]}


def sample_check(req: dict, analysis) -> str | None:
    """Why a run with this analysis cannot give the samples (not a transient, it stops before the last time, or its
    tstart drops the results before the first time, as spectrum.check_run says), or None; without an analysis the
    testbench's own .control runs, and its result says."""
    if not isinstance(analysis, dict):
        return None
    if analysis.get("type", "tran") != "tran":
        return f"sample is taken from a transient run, not {analysis.get('type')}: analysis {{type: tran, step, stop}}"
    try:
        stop = spectral._num(analysis.get("stop") or sim.ANALYSIS_ARGS["tran"]["stop"], "stop", "analysis")
        tstart = spectral._num(analysis["tstart"], "tstart", "analysis") if analysis.get("tstart") not in (None, "") else 0.0
    except ValueError:
        return None                                       # the analysis itself is refused, with its own message
    first, last = min(req["times"]), max(req["times"])
    if last > stop * (1 + 1e-9):
        return (f"sample: the last time, {last:.9g} s, is after the transient's stop at {stop:.9g} s: make stop at "
                "least that, or sample earlier")
    if tstart > first * (1 + 1e-9) and tstart > 0:        # the raw file holds the run from tstart on
        return (f"sample: the transient keeps its results from tstart {tstart:.9g} s, after the first time, "
                f"{first:.9g} s: make tstart at most the first sample time, or sample later")
    return None


def samples_of(job: sim.Job, req: dict) -> dict:
    """The signals of a request (sample_config) at its times, at full precision: a row [time, value, ...] per time,
    linearly interpolated between the simulator's time points as spectrum does; or {error}."""
    try:
        raw = transient_raw(job, "a sample")
    except ValueError as e:
        return {"error": str(e)}
    names = [n for n, _ in raw["vars"]]
    time, t = np.real(raw["data"][names[0]]), np.asarray(req["times"])
    tol = 1e-9 * max(abs(time[-1]), 1e-30)
    if t.min() < time[0] - tol and time[0] > tol:  # a run that keeps its results from its tstart on
        return {"error": f"the times run from {t.min():.9g} s to {t.max():.9g} s, the run keeps its results from "
                         f"{time[0]:.9g} s (the transient's tstart) to {time[-1]:.9g} s: make tstart earlier than the "
                         "first sample time, or sample later"}
    if t.min() < time[0] - tol or t.max() > time[-1] + tol:
        return {"error": f"the times run from {t.min():.9g} s to {t.max():.9g} s, the run covers {time[0]:.9g} s to "
                         f"{time[-1]:.9g} s: run the transient longer (stop), or sample earlier"}
    pick, missing = select(names[1:], req["signals"])
    if missing:
        return {"error": _hinted(job, f"no signal {', '.join(missing)} in this run"),
                "available_signals": own_first(names[1:])[:60]}
    # only the stretch of the run the times are in: a long run's file is read there alone (sim.read_raw maps it)
    lo, hi = np.searchsorted(time, [t.min(), t.max()], side="right")
    lo, hi = max(lo - 2, 0), min(max(hi + 1, lo + 2), len(time))
    time = np.asarray(time[lo:hi])
    cols = [np.interp(t, time, np.real(raw["data"][p][lo:hi])) for p in pick]
    i = np.clip(np.searchsorted(time, t), 1, len(time) - 1)
    near = np.minimum(np.abs(time[i] - t), np.abs(time[i - 1] - t)) <= tol
    return {"columns": ["time", *pick], "rows": [[float(t[k]), *(float(c[k]) for c in cols)] for k in range(len(t))],
            "points": len(t), "on_time_points": int(np.count_nonzero(near)),
            "note": "values at full precision; between the simulator's time points they are interpolated linearly (as "
                    "spectrum samples): on_time_points of the times fall on one. A smaller tran step brings them closer"}


def summary(job: sim.Job, signals: list[str] | None = None, output: str | None = None, input: str | None = None,
            spectrum: dict | None = None, sample: dict | None = None) -> dict:
    """What a finished run shows (see the module docstring); `spectrum` and `sample` are checked requests
    (spectrum.config, sample_config)."""
    st = job.status()
    log = job.log(5000)
    res: dict = {"state": "done", "job": job.id, **_notes(st)}
    if st.get("warnings"):                  # error lines of a run that still finished (an earlier analysis that aborted)
        res["warnings"] = st["warnings"]
    if (meas := measurements(log)):
        res["measurements"] = {k: _r(v, 6) for k, v in meas.items()}
    if (failed := measurement_failures(log)):
        res["measurement_errors"] = failed
    if spectrum:
        res["spectrum"] = spectrum_of(job, spectrum)
    if sample:
        res["samples"] = samples_of(job, sample)
    raws = job.raw_files()
    if not raws:
        res["note"] = "no raw output: pass analysis {type: op|tran|ac|dc, ...} or write a raw file in the schematic's .control"
        return res
    raw = sim.read_raw(Path(raws[0]))
    names = [n for n, _ in raw["vars"]]
    xname = names[0]
    plot = raw["plot"].lower()
    res["analysis"] = raw["plot"]
    if "operating point" in plot:
        volts = {n[2:-1]: _r(float(np.real(raw["data"][n][0]))) for n in names if n.startswith("v(") and "#" not in n}
        res["node_voltages"] = dict(sorted(volts.items())[:120])
        if (bias := device_bias(log)):
            res["transistors"] = bias
            res["not_in_saturation"] = [b["device"] for b in bias if b["region"] != "saturation"]
        return res
    x = np.real(raw["data"][xname])
    pick, missing = select(names[1:], signals)
    if missing:
        res["missing_signals"] = missing
        res["available_signals"] = own_first([n for n in names[1:] if "#" not in n])[:80]
        if (hint := unsaved(job, missing)):
            res["missing_hint"] = hint
    if "ac analysis" in plot:
        if output:
            o = select(names[1:], [output])[0]
            i = select(names[1:], [input])[0] if input else []
            if not o:
                res["error"] = f"no signal {output}"
            else:
                h = raw["data"][o[0]] / (raw["data"][i[0]] if i else 1.0)
                res["metrics"] = {"output": o[0], "input": i[0] if i else "the 1 V AC source", **ac_metrics(x, h)}
        res["signals"] = {n: {"gain_db_low_freq": _r(20 * np.log10(max(abs(raw["data"][n][0]), 1e-30))),
                              "max_magnitude": _r(float(np.max(np.abs(raw["data"][n]))))} for n in pick}
        if not output:
            res["hint"] = "pass output (and input) to get dc gain, -3 dB, unity-gain frequency and phase margin"
        return res
    sig = {}
    for n in pick:
        y = np.real(raw["data"][n])
        slope = float(np.max(np.abs(np.gradient(y, x)))) if len(x) > 2 and np.ptp(x) > 0 else 0.0
        sig[n] = {"initial": _r(float(y[0])), "final": _r(float(y[-1])), "min": _r(float(np.min(y))), "max": _r(float(np.max(y))),
                  "max_slope_per_s": _r(slope), "samples": _samples(x, y)}
    res["x"] = {"name": xname, "from": _r(float(np.min(x))), "to": _r(float(np.max(x))), "points": int(len(x))}
    res["signals"] = sig
    return res
