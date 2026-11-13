"""Simulation jobs (ngspice) and raw-file reading.

A job is a directory under <project>/runs/<id>/ holding the netlist, the log,
the raw output and a status file. Jobs run in the server process as
subprocesses and survive page reloads; the UI polls their status.

Several people and agents simulate on one machine at once, so a job runs ngspice with one thread (UNDINE_SIM_THREADS;
some builds set eight in their spinit, and eight per job on a shared machine made every run several times slower), at
most UNDINE_SIM_JOBS jobs run together (default: the cores but two; the others wait in order, `queued` with their
`queue_position`), and a job still running at its time limit (UNDINE_SIM_TIMEOUT, or the caller's, at most
UNDINE_SIM_TIMEOUT_MAX) is stopped like a cancelled one: terminated, then killed. A job's process leads a process group
of its own (Process; every kind of job starts its tools so), and stopping it stops what it started too.

A server's jobs (Jobs with a root) outlive the server process, so that a restart (to deploy a change) loses no run of
hours. ngspice runs in that session of its own (Ctrl-C in the server's terminal does not reach it) and is left running
when the server stops, however it stops (`undine serve --stop-jobs` stops them instead: Jobs.shutdown); its status
records its process (pid and start time from /proc, the command, the directory). The next server on that root
(Jobs.resume) adopts each running job whose ngspice still runs (a Foreign process: polled in /proc, holding a slot, its
time limit counted from its start, cancel stops it and its group, its end recorded as any job's, judged by its log since
its exit code is not ours to know), also one an earlier Undine started without recording its process (found by its
directory and command line, once that server has gone), collects one whose ngspice finished meanwhile when it is next
read, and starts the jobs that had not started yet in their order (<root>/sim-queue.jsonl). A second server on the root
(the next one while the last still stops, or one too many) runs its own jobs, and what it leaves when it stops goes on
in the same way in the server that holds the root (Jobs). A simulation queued or running that no server works on any
more (its ngspice gone, or a queue nobody kept), and any other job (DRC, LVS, RTL ...) a stopped server left, is
"interrupted" (Jobs.status, with why): nothing finishes it. A job of another kind records its process and server too,
so that what a killed server left of its tools (they run on in their session, kill -9 or a crash does not reach them)
is stopped once a later server sees the run. Adopting needs Linux's /proc; elsewhere a server's ngspice ends with it
when it stops (a hangup too: `undine serve` handles SIGHUP as SIGTERM; not after kill -9), as a script's always does.

A job reads the PDK's model library as a copy reduced to what its netlist uses (modellib.py: the same results, a
fraction of the time); its status says so ("models"), and so do the first lines of its log.
"""
from __future__ import annotations

import atexit
import functools
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
import weakref
from pathlib import Path

import numpy as np

from undine.simulation import modellib
from undine.store import provenance
from undine import tools


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ[name]) if os.environ.get(name) else default
    except ValueError:
        return default


THREADS = int(_env_num("UNDINE_SIM_THREADS", 1))          # ngspice's num_threads per job; 0: as its spinit sets it
TIMEOUT_S = _env_num("UNDINE_SIM_TIMEOUT", 3600.0)        # a run's time limit unless the caller gives one
TIMEOUT_MAX_S = max(_env_num("UNDINE_SIM_TIMEOUT_MAX", 12 * 3600.0), TIMEOUT_S)
STOP_GRACE_S = 5.0                                        # from SIGTERM to SIGKILL
PAR_MAX = 99                                              # ngspice's limit of par() calls in one netlist
PROC = Path("/proc")
ADOPT = (PROC / "self" / "stat").is_file()                # a later server can adopt a job's process (Linux)
PROC_POLL_S = 0.5                                         # how often an adopted process is looked at
PARK_POLL_S = 2.0                                         # ... and one another live server still runs (Jobs._park)
QUEUE = "sim-queue.jsonl"                                 # <root>/...: the jobs that have not started yet (Jobs._journal)
OTHERS = "sim-queue-*.jsonl"                              # <root>/...: each other server's unfinished jobs (Jobs._journal)
LOCK = "sim-jobs.lock"                                    # <root>/...: held by the server process the jobs are with (Jobs.owner)
# how a job runs ngspice in its directory (Jobs._run): the tail of the command line
NGSPICE_ARGS = (["-b", "-o", "log.txt", "netlist.cir"], ["-b", "-o", "log.txt", "-r", "out.raw", "netlist.cir"])
# a log ngspice finished: its `quit` says so (every control block Undine runs ends with one), and a batch run (-r)
# ends with its statistics
ENDED = re.compile(r"^ngspice-\S+ done\s*$|^Total elapsed time", re.M)


_minted: tuple[str, int, set[str]] = ("", 0, set())   # the second new_run last gave ids in, how many, their suffixes
_mint_lock = threading.Lock()


def new_run(runs: Path) -> tuple[str, Path]:
    """A new job's id and its directory under `runs`, created: the time to the second and 4 hex digits
    ("20261104-133330-00b7"), so that ids sort by when they were made and read as a time. The first 3 digits count the
    second's ids in this process, so that ids of one second sort in the order they were made too (the newest run of a
    cell is the last id: Jobs.list, the newest extraction, the latest check run; about 60 checks start in one second in
    a run_checks); the last one is random, so that another server on the root rarely meets the same id. Unique: this
    process never gives an id twice (a job is also known by its id alone, verify.LIVE), past 4096 in a second the
    suffix is random and in no order, and the directory is made exclusively, with another id when it exists (another
    server on the root made it in the same second)."""
    global _minted
    for _ in range(100000):
        with _mint_lock:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            if _minted[0] != stamp:
                _minted = (stamp, 0, set())
            n, given = _minted[1], _minted[2]
            if n <= 0xfff:
                suffix = f"{n:03x}{uuid.uuid4().hex[0]}"
                _minted = (stamp, n + 1, given)
            else:
                suffix = uuid.uuid4().hex[:4]
            if suffix in given:
                continue
            given.add(suffix)
        jid = f"{stamp}-{suffix}"
        d = runs / jid
        try:
            d.mkdir()
        except FileExistsError:
            continue
        return jid, d
    raise RuntimeError(f"cannot make a new run directory in {runs}")


def job_limit() -> int:
    """How many simulation jobs run at once (UNDINE_SIM_JOBS, default the cores but two)."""
    return max(1, int(_env_num("UNDINE_SIM_JOBS", max(1, (os.cpu_count() or 2) - 2))))


def time_limit(seconds) -> float:
    """A run's time limit: `seconds` (1 .. TIMEOUT_MAX_S), or TIMEOUT_S when not given; ValueError otherwise."""
    if seconds in (None, ""):
        return TIMEOUT_S
    try:
        t = float(seconds)
    except (TypeError, ValueError):
        raise ValueError(f"time_limit_s is a number of seconds, not {seconds!r}") from None
    if not 1 <= t <= TIMEOUT_MAX_S:
        raise ValueError(f"time_limit_s is 1 to {TIMEOUT_MAX_S:g} seconds (UNDINE_SIM_TIMEOUT_MAX), not {seconds!r}")
    return t


ANALYSES = {
    "op": ".op",
    "tran": ".tran {step} {stop}",
    "ac": ".ac {sweep} {points} {fstart} {fstop}",
    "dc": ".dc {source} {start} {stop} {step}",
}
# the arguments of each analysis and their defaults; anything else is refused rather than silently ignored
ANALYSIS_ARGS = {
    "op": {},
    "tran": {"step": "1n", "stop": "100n"},
    "ac": {"sweep": "dec", "points": "10", "fstart": "1", "fstop": "1Meg"},
    "dc": {"source": "V1", "start": "0", "stop": "1.8", "step": "0.01"},
}
# arguments without a default: tran's tstart (the results start there; the run still starts at 0), tmax (the largest
# time step; maxstep is the same, as the simulator option of that name sets it) and uic (start from the .ic values
# instead of an operating point), as ngspice's .tran takes them
OPTIONAL_ARGS = {"tran": {"tstart": "results from this time on", "tmax": "the largest time step",
                          "uic": "true: start from the .ic values, without an operating point",
                          "maxstep": "the same as tmax"}}
# an operating point also prints each transistor's bias (id, vgs, vds, vth, vdsat, gm, gds) into the log. Both blocks
# quit: in batch mode ngspice would otherwise run the netlist's analyses once more after the block (twice the time,
# and an AC .meas with vm() / vp() then fails the job: "no data saved for A.C. Small signal analysis")
OP_CONTROL = ".control\nop\nwrite out.raw\nshow m : id vgs vds vbs vth vdsat gm gds\nquit\n.endc"
MEAS_CONTROL = ".control\nrun\nwrite out.raw\nquit\n.endc"      # the netlist's analysis, with its .meas lines evaluated
CONTROL = re.compile(r"^[ \t]*\.control\b.*?^[ \t]*\.endc\b[^\n]*\n?", re.M | re.S | re.I)
ANALYSIS_LINE = re.compile(r"^[ \t]*\.(op|tran|ac|dc|noise|pz|sens|tf|disto|sp|pss)\b[^\n]*\n?", re.M | re.I)
# simulator options a run can set (.options), and maxstep: the transient's largest time step (its tmax)
SIM_OPTIONS = ("reltol", "abstol", "vntol", "chgtol", "trtol", "gmin", "method", "maxord", "itl1", "itl4", "maxstep")
METHODS = ("gear", "trap", "trapezoidal")


def analysis_card(spec: dict) -> str:
    kind = spec.get("type", "tran")
    if kind not in ANALYSES:
        raise ValueError(f"unknown analysis {kind}: one of {', '.join(ANALYSES)}")
    allowed, optional = ANALYSIS_ARGS[kind], OPTIONAL_ARGS.get(kind, {})
    if (bad := sorted(set(spec) - {"type"} - set(allowed) - set(optional))):
        raise ValueError(f"{kind} analysis: unknown {', '.join(bad)}; it takes "
                         + (", ".join([*(f"{k} (default {v})" for k, v in allowed.items()),
                                       *(f"{k} ({v})" for k, v in optional.items())]) or "no arguments"))
    given = {k: str(v) for k, v in spec.items() if k != "type" and v not in (None, "") and not isinstance(v, bool)}
    if (bad := [k for k, v in given.items() if not re.fullmatch(r"[^\s;]+", v)]):
        raise ValueError(f"{kind} analysis: {', '.join(bad)} must be one value without spaces")
    d = dict(allowed)
    d.update({k: v for k, v in given.items() if k in allowed})
    card = ANALYSES[kind].format(**d)
    if kind == "tran":
        if given.get("tmax") and given.get("maxstep") and given["tmax"] != given["maxstep"]:
            raise ValueError("tran analysis: tmax and maxstep are the same (the largest time step): give one")
        tmax = given.get("tmax") or given.get("maxstep")
        if given.get("tstart") or tmax:                          # .tran tstep tstop <tstart <tmax>> <uic>
            card += f" {given.get('tstart') or 0}" + (f" {tmax}" if tmax else "")
        uic = spec.get("uic")
        if uic not in (None, "", False, True) and str(uic).lower() not in ("true", "false"):
            raise ValueError(f"tran analysis: uic is true or false, not {uic!r}")
        if uic is True or str(uic).lower() == "true":
            card += " uic"
    return card


def _before_end(text: str, lines: str) -> str:
    return text.replace("\n.end\n", f"\n{lines}\n.end\n") if "\n.end\n" in text else f"{text}\n{lines}\n"


def quit_after_control(text: str) -> str:
    """`text` with `quit` at the end of its last .control block when that block's commands already run every analysis
    of the netlist (`run`, or a command of each kind): in batch mode ngspice otherwise runs the netlist's analyses once
    more after the block, which doubles the time and evaluates every .meas again (the second time a param= measure fails:
    "insertnumber: fails")."""
    blocks = list(CONTROL.finditer(text))
    if not blocks:
        return text
    cmds = {ln.split()[0].lower() for b in blocks for ln in b.group(0).splitlines()[1:-1]
            if ln.strip() and not ln.lstrip().startswith(("*", "#"))}
    if cmds & {"quit", "exit"}:
        return text
    dot = {m.group(1).lower() for m in ANALYSIS_LINE.finditer(CONTROL.sub("", text))}
    if "run" not in cmds and not dot <= cmds:
        return text                                   # the batch run does an analysis the block does not: keep it
    last = blocks[-1]
    block = re.sub(r"(^[ \t]*\.endc\b)", "quit\n\\1", last.group(0), count=1, flags=re.M | re.I)
    return text[:last.start()] + block + text[last.end():]


def par_sources(text: str) -> tuple[str, int]:
    """More than PAR_MAX par('expression') calls (ngspice stops: "More than 99 function calls to par()"): each distinct
    expression of the top level's .meas lines becomes a B source, and par(...) the voltage of its node, as ngspice does
    itself within its limit. Returns the text and how many expressions became sources (0: unchanged)."""
    pat = re.compile(r"\bpar\(\s*'([^']*)'\s*\)", re.I)
    if len(pat.findall(CONTROL.sub("", text))) <= PAR_MAX:
        return text, 0
    nodes: dict[str, str] = {}

    def node(m):
        expr = " ".join(m.group(1).split())
        return f"v({nodes.setdefault(expr, f'undine_par{len(nodes) + 1}')})"
    out, depth, control = [], 0, False
    for ln in text.split("\n"):
        low = ln.strip().lower()
        if low.startswith((".subckt", ".ends")):
            depth += 1 if low.startswith(".subckt") else -1
        elif low.startswith((".control", ".endc")):
            control = low.startswith(".control")
        elif depth == 0 and not control and re.match(r"\.meas(ure)?\b", low):
            ln = pat.sub(node, ln)
        out.append(ln)
    out = "\n".join(out)
    if not nodes:
        return text, 0
    lines = [f"B{n} {n} 0 V={expr}" for expr, n in nodes.items()]
    if re.search(r"^[ \t]*\.save\b", out, re.M | re.I):        # a netlist that says what to save keeps these too
        lines += [".save " + " ".join(f"v({n})" for n in list(nodes.values())[i:i + 16]) for i in range(0, len(nodes), 16)]
    return _before_end(out, "\n".join(lines)), len(nodes)


def spiceinit(osdi: list[Path] | None = None) -> str:
    """A job's .spiceinit. ngspice reads it from its working directory after its spinit (which may set num_threads=8)
    and instead of ~/.spiceinit, whose lines therefore come first."""
    lines = []
    try:
        home = Path.home() / ".spiceinit"
        if home.is_file():
            lines.append(home.read_text().rstrip("\n"))
    except (OSError, RuntimeError):
        pass
    lines += [f"osdi '{p}'" for p in osdi or []]
    if THREADS > 0:
        lines.append(f"set num_threads={THREADS}")
    return "".join(f"{ln}\n" for ln in lines)


def sim_options(options: dict) -> dict[str, str]:
    """Checked simulator options ({reltol: 1e-6, method: gear, maxstep: 0.2n}) as text; ValueError names what is wrong."""
    if not isinstance(options, dict):
        raise ValueError("options is a mapping, e.g. {reltol: 1e-6, maxstep: 0.2n}")
    out = {}
    for k, v in options.items():
        k = str(k).lower()
        if k not in SIM_OPTIONS:
            raise ValueError(f"no simulator option {k}: one of {', '.join(SIM_OPTIONS)}")
        v = str(v).strip().lower() if not isinstance(v, float) else f"{v:g}"
        if k == "method" and v not in METHODS:
            raise ValueError(f"method is {' or '.join(METHODS[:2])}")
        if k != "method" and not re.fullmatch(r"[-+]?(\d+\.?\d*|\.\d+)(e[-+]?\d+)?(meg|[fpnumkgt])?s?", v):
            raise ValueError(f"option {k}: not a number: {v!r}")
        out[k] = v
    return out


def substitute_map(value) -> dict[str, str | None]:
    """A run's stand-ins as asked ({"lib/cell": "lib/cell"}: every instance of the first cell, at any depth, netlisted
    as the second; Server.substitutes), checked, with None for a cell mapped to null or "" (netlisted as itself: it
    takes back a default). ValueError says what is wrong."""
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise ValueError("substitute is a mapping from a cell to the cell netlisted in its place, e.g. "
                         "{'sar/sar_logic': 'sar/sar_logic_ideal'}")
    out: dict[str, str | None] = {}
    for k, v in value.items():
        for x in (k, v) if v not in (None, "") else (k,):
            if not isinstance(x, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*/[A-Za-z0-9_][A-Za-z0-9_.+-]*", x):
                raise ValueError(f"substitute: {x!r} is not lib/cell")
        out[k] = v or None
    return out


def with_options(text: str, options: dict[str, str]) -> str:
    """The netlist with an .options line at its end (the last one wins over the testbench's own) and, for maxstep,
    its transients (.tran cards and tran commands in .control) with that tmax."""
    opts = {k: v for k, v in options.items() if k != "maxstep"}
    if (tmax := options.get("maxstep")):
        n = 0

        def repl(m):
            nonlocal n
            n += 1
            args = m.group(2).split()
            uic = [a for a in args if a.lower() == "uic"]
            args = [a for a in args if a.lower() != "uic"]
            return m.group(1) + " ".join([*args[:2], args[2] if len(args) > 2 else "0", tmax, *uic])
        text = re.sub(r"^([ \t]*\.?tran[ \t]+)([^\n]*)$", repl, text, flags=re.M | re.I)
        if n == 0:
            raise ValueError("maxstep applies to a transient: this run has none (give analysis {type: tran, ...})")
    if opts:
        line = ".options " + " ".join(f"{k}={v}" for k, v in opts.items())
        text = _before_end(text, line)
    return text


# the nodes of an element line by its first letter; X (a subcircuit) and N (an OSDI device): the tokens before the model
_NODES = {"r": 2, "c": 2, "l": 2, "v": 2, "i": 2, "d": 2, "b": 2, "f": 2, "h": 2, "w": 2, "e": 4, "g": 4, "s": 4,
          "t": 4, "o": 4, "m": 4, "q": 3, "j": 3, "z": 3}


def is_transient(netlist: str) -> bool:
    """Whether the netlist runs a transient: a .tran card, or a tran command in its .control."""
    return re.search(r"^[ \t]*\.?tran\b", netlist, re.M | re.I) is not None


def save_cards(netlist: str, signals: list[str] | None = None) -> str:
    """.save lines that keep the top level's nodes and its voltage sources' currents, the vectors its .meas and .control
    lines name (in .control also a bare name inside an instance, x1.out), and `signals` (a node, v(...), i(...)):
    ngspice then stores no node inside a subcircuit (an extracted view has thousands, a mixed-signal block's transient
    hundreds of megabytes). Empty when the netlist says what to save itself (.save, or .options savecurrents: every
    device's current is asked for). ngspice skips a name that is not a vector."""
    if re.search(r"^[ \t]*\.?save\b", netlist, re.M | re.I) or \
            re.search(r"^[ \t]*\.options?\b[^\n]*\bsavecurrents\b", netlist, re.M | re.I):
        return ""
    lines: list[str] = []
    for raw in netlist.splitlines():
        if raw.startswith("+") and lines:
            lines[-1] += " " + raw[1:]
        else:
            lines.append(raw)
    keep: dict[str, str] = {}                    # SPICE names are compared without case
    depth, control = 0, False
    for ln in (x.strip() for x in lines):
        low = ln.lower()
        if not ln or ln[0] == "*":
            continue
        if low.startswith((".subckt", ".ends")):
            depth += 1 if low.startswith(".subckt") else -1
        elif low.startswith((".control", ".endc")):
            control = low.startswith(".control")
        elif depth == 0 and (control or ln[0] == "."):
            for kind, args in re.findall(r"\b([vi])\(([^()]*)\)", ln, re.I):
                for x in (x.strip() for x in args.split(",") if x.strip()):
                    keep[f"{kind}({x})".lower()] = f"{kind.lower()}({x})"
            if control:                     # let d = x1.outp - x1.outn: a vector named without v()
                bare = re.sub(r"\b[vi]\([^()]*\)", " ", ln, flags=re.I)
                for x in re.findall(r"(?<![\w.@#(\[])x\w*(?:\.[\w#]+)+", bare, re.I):
                    keep.setdefault(f"v({x})".lower(), f"v({x})")
        elif depth == 0:
            t = ln.split()
            kind = t[0][0].lower()
            nodes = [x for x in t[1:] if "=" not in x][:-1] if kind in "xn" else t[1:1 + _NODES.get(kind, 0)]
            keep.update((n.lower(), n) for n in nodes
                        if re.fullmatch(r"[^\s(){}'\"=,]+", n) and n.lower() not in ("0", "gnd"))
            if kind == "v":
                keep[f"i({t[0]})".lower()] = f"i({t[0]})"
    for x in (x.strip() for x in signals or [] if x):
        keep.setdefault(x.lower(), x)
    names = list(keep.values())
    return "\n".join(".save " + " ".join(names[i:i + 16]) for i in range(0, len(names), 16))


class Job:
    def __init__(self, path: Path):
        self.path = path
        self.proc: subprocess.Popen | Foreign | None = None
        self.cancelled = False

    @property
    def id(self) -> str:
        return self.path.name

    def status(self) -> dict:
        try:
            return json.loads((self.path / "status.json").read_text())
        except Exception:
            return {"id": self.id, "state": "unknown"}

    def set(self, *, drop: tuple[str, ...] = (), **kw) -> dict:
        """Updates the status: written anew and renamed in, never in place (a changeset's evidence holds the job's files
        as hard links: a write in place would change the archive too), from a file of this call's own (several
        threads set one job's status)."""
        st = self.status()
        st.update(kw)
        for k in drop:
            st.pop(k, None)
        _replace(self.path / "status.json", json.dumps(st).encode())
        return st

    def log(self, tail: int = 400) -> str:
        p = self.path / "log.txt"
        if not p.is_file():
            return ""
        lines = p.read_text(errors="replace").splitlines()
        return "\n".join(lines[-tail:])

    def raw_files(self) -> list[Path]:
        return sorted(self.path.glob("*.raw"))


# ------------------------------------------------------------------ a job's process, also one an earlier server started


def _proc_stat(pid: int) -> tuple[str, int, int, int] | None:
    """(state, parent's pid, start time in clock ticks since boot, process group) of a process, from /proc/<pid>/stat;
    None when there is none."""
    try:
        s = (PROC / str(pid) / "stat").read_text()
        f = s[s.rindex(")") + 2:].split()           # after the command's name, which may hold spaces and parentheses
        return f[0], int(f[1]), int(f[19]), int(f[2])
    except (OSError, ValueError, IndexError):
        return None


@functools.cache
def _boot() -> str | None:
    """This boot's id: a start time in clock ticks since boot means nothing in another one."""
    try:
        return (PROC / "sys" / "kernel" / "random" / "boot_id").read_text().strip()
    except OSError:
        return None


def identity(pid: int, cmd: list[str], cwd: Path) -> dict:
    """What a job's status records of its process ("process"): its pid, its start time (clock ticks since this boot)
    so that a process given the same pid later is never taken for it, its command and its directory."""
    st = _proc_stat(pid)
    return {"pid": pid, **({"start": st[2], "boot": _boot()} if st else {}), "cmd": list(cmd), "cwd": str(cwd)}


@functools.cache
def _ident(pid: int) -> dict:
    st = _proc_stat(pid)
    return {"pid": pid, **({"start": st[2], "boot": _boot()} if st else {})}


def _me() -> dict:
    """This process as a job's status names the server that runs it ("server", with its Jobs' token): its pid, and its
    start time and boot, so that a process given its pid later is never taken for it."""
    return _ident(os.getpid())


def _server_runs(rec) -> bool:
    """Whether the server a job's status names ("server") is still there: in this process, a Jobs of it with that token
    that has not stopped (a test runs several); else that process (the same start time, in this boot)."""
    if not isinstance(rec, dict) or not isinstance(rec.get("pid"), int):
        return False
    if rec["pid"] == os.getpid() and rec.get("start") == _me().get("start"):
        return any(o.token == rec.get("jobs") and not o.closed for o in list(_ALL))
    if rec.get("boot") != _boot() or not isinstance(rec.get("start"), int):
        return False
    st = _proc_stat(rec["pid"])
    return st is not None and st[2] == rec["start"] and st[0] not in "ZX"


def _procs(under: Path):
    """The processes (of this user: another's cannot be read) whose working directory is `under` or below it: {pid,
    state, ppid, start, argv, cwd (resolved)}."""
    try:
        base = under.resolve()
    except OSError:
        return
    for d in PROC.iterdir():
        if not d.name.isdigit():
            continue
        try:
            cwd = Path(os.readlink(d / "cwd"))
            if cwd != base and base not in cwd.parents:
                continue
            argv = [a.decode(errors="replace") for a in (d / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")]
        except OSError:                             # another user's, or it ended meanwhile
            continue
        if (st := _proc_stat(int(d.name))) is not None:
            yield {"pid": int(d.name), "state": st[0], "ppid": st[1], "start": st[2], "argv": argv, "cwd": cwd}


def _ngspice(argv: list[str]) -> bool:
    """Whether a command line is ngspice as a job runs it (Jobs._run: NGSPICE_ARGS), maybe through a wrapper script
    named ngspice."""
    for tail in NGSPICE_ARGS:
        n = len(argv) - len(tail)
        if n >= 1 and argv[n:] == tail and any(Path(a).name == "ngspice" for a in argv[:n]):
            return True
    return False


def _serving(ppid: int) -> bool:
    """Whether a job's process has another live Undine server (`undine serve`) as its parent: that server still
    watches it (a second server on the same root, or an Undine before this version that is still on its way out), so it
    is not to be adopted yet. The process of a server that ended has init or a subreaper as its parent."""
    if ppid <= 1 or ppid == os.getpid():
        return False
    try:
        argv = (PROC / str(ppid) / "cmdline").read_bytes().decode(errors="replace").split("\0")
    except OSError:
        return False
    return "serve" in argv and any(a == "undine" or a.endswith("/undine") for a in argv)


class Process(subprocess.Popen):
    """A job's process, the leader of a process group of its own (a new session, on POSIX): what it starts (the
    compiler a build runs, the tools of a flow) is in that group, so that stop() ends all of it and alive() sees all of
    it. Otherwise a cancelled build's make and g++ outlive the verilator they were started by and go on writing into
    the run's log, after the job counts as ended (a changeset's evidence holds the run's files as hard links). A
    server's ngspice is one too: in a session of its own, Ctrl-C in the server's terminal does not reach it, and it runs
    on when the server stops (the next server adopts it: Foreign, the same group). Once the process has ended and its
    group is seen empty ("ended"), the group is never signalled again: its id may be another's by then (a terminal's
    new session takes its leader's pid as its group's id)."""

    def __init__(self, args, job: "Job | None" = None, **kwargs):
        """`job`: the job it runs for, whose status records it ("process": identity), so that a later server can stop
        what it left running (Jobs.interrupted: a server killed outright cannot); also its `proc`."""
        self.ended = os.name != "posix"
        super().__init__(args, start_new_session=os.name == "posix", **kwargs)
        st = _proc_stat(self.pid)                        # the leader's start time: one given its pid later is another
        self.start = st[2] if st else None
        if job is not None:
            job.proc = self
            try:
                job.set(process=identity(self.pid, [str(a) for a in args], Path(kwargs.get("cwd") or job.path)))
            except OSError:                              # it runs all the same; only a later server cannot find it
                pass

    @property
    def group(self) -> int | None:
        """The id of the process group while it may have members."""
        return None if self.ended else self.pid

    def wait(self, timeout=None):
        rc = super().wait(timeout)
        alive(self)                                      # the group seen empty now, if it is ("ended")
        return rc


class Foreign:
    """A job's process that an earlier server process started (Jobs._take adopted it): what Jobs uses of a Popen. It is
    not this process's child: its end is seen in /proc (polled every PROC_POLL_S), its exit code is never known (wait
    returns None), and it is that process only while its start time is the one recorded, so that a signal never
    reaches another process that was given its pid since. No pid: one that had ended before it was adopted. A server's
    ngspice (a Process) leads a process group of its own, and so the adopted one is that group ("group", as a Process's:
    stop ends all of it, alive sees all of it, and once seen empty it is never signalled again); one an Undine before
    process groups started is in that server's group, and only it is signalled."""

    def __init__(self, pid: int | None, start: int | None):
        self.pid, self.start = pid, start
        st = _proc_stat(pid) if pid is not None else None
        # the leader of a group of its own (its pid is the group's id) while it is the process recorded
        self.ended = not (st is not None and st[2] == start and st[3] == pid)

    @property
    def group(self) -> int | None:
        """The id of its process group while it may have members (None: not a group of its own, or seen empty)."""
        return None if self.ended else self.pid

    def running(self) -> bool:
        st = _proc_stat(self.pid) if self.pid is not None else None
        return st is not None and st[2] == self.start and st[0] not in "ZX"       # a zombie has ended

    def wait(self, timeout: float | None = None) -> None:
        end = None if timeout is None else time.monotonic() + timeout
        while self.running():
            left = PROC_POLL_S if end is None else end - time.monotonic()
            if left <= 0:
                raise subprocess.TimeoutExpired(f"pid {self.pid}", timeout)
            time.sleep(min(PROC_POLL_S, left))
        return None

    def send_signal(self, sig: int) -> None:
        if self.running():
            try:
                os.kill(self.pid, sig)
            except ProcessLookupError:
                pass

    def terminate(self) -> None:
        self.send_signal(signal.SIGTERM)

    def kill(self) -> None:
        self.send_signal(signal.SIGKILL)


def running(proc) -> bool:
    """Whether a job's process itself (a Popen, or a Foreign one) is still running; alive: or what it started."""
    return proc is not None and (proc.running() if isinstance(proc, Foreign) else proc.poll() is None)


def _members(group: int, leader: int | None = None) -> bool:
    """Whether a process of the process group `group` is still there, a zombie aside: the group can be signalled, and
    (Linux: /proc) not only a zombie no reaper takes is in it. `leader`: the start time of the group's leader, which
    has ended: a process with the group's id as its pid and another start time took that pid, which the system gives
    again only once the group has gone (it is another's group then)."""
    if leader is not None and (st := _proc_stat(group)) is not None and st[2] != leader:
        return False
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except OSError:                                      # there, not ours to signal
        pass
    try:
        pids = [n for n in os.listdir("/proc") if n.isdigit()]
    except OSError:
        return True
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                stat = f.read()
        except OSError:
            continue
        fields = stat[stat.rfind(b")") + 2:].split()        # after "pid (comm)": state, ppid, pgrp, ...
        if len(fields) > 2 and fields[0] != b"Z" and int(fields[2]) == group:
            return True
    return False


def alive(proc) -> bool:
    """Whether a job's process (a Popen, or a Foreign one) is still running, or (a Process, a Foreign one leading its
    group) any of its group: what a closure waits for. A group seen empty stays so (nothing joins a session whose
    processes are all gone): the process is "ended"."""
    if proc is None:
        return False
    if running(proc):
        return True
    group = getattr(proc, "group", None)
    if group and _members(group, getattr(proc, "start", None)):
        return True
    if isinstance(proc, (Process, Foreign)):
        proc.ended = True
    return False


def left(st: dict) -> "Foreign | None":
    """The process group a job's status recorded ("process": a Process, which leads a group of its own), while any of
    it is still there: the process, or what it started that outlived it (a leader gone: its group's members, which
    _members tells from a group of a process given that pid since). None: none is there, none was recorded, it was
    recorded in another boot, or nothing tells (not Linux)."""
    rec = st.get("process") if isinstance(st, dict) else None
    if not (ADOPT and isinstance(rec, dict) and isinstance(rec.get("pid"), int) and isinstance(rec.get("start"), int)
            and rec.get("boot") == _boot()):
        return None
    proc = Foreign(rec["pid"], rec["start"])
    if proc.ended and _proc_stat(rec["pid"]) is None:
        proc.ended = False                               # its leader gone: what it started may be there still
    return proc if alive(proc) else None


def _signal(proc, sig: int) -> None:
    group = getattr(proc, "group", None)
    try:
        if group:
            os.killpg(group, sig)
        elif running(proc):
            proc.send_signal(sig)
    except ProcessLookupError:
        pass


def stop(proc) -> None:
    """Ends a job's process (a Popen, or a Foreign one), and (a Process, an adopted one leading its group) all of its
    group: SIGTERM, then SIGKILL to what is still there after STOP_GRACE_S. Returns once they are gone (after a
    SIGKILL, STOP_GRACE_S at most)."""
    if not alive(proc):
        return
    _signal(proc, signal.SIGTERM)
    for sig in (signal.SIGKILL, None):
        end = time.monotonic() + STOP_GRACE_S
        while alive(proc) and time.monotonic() < end:
            try:
                proc.wait(0.05)                          # the leader, reaped (a Foreign one: seen gone)
            except subprocess.TimeoutExpired:
                continue
            time.sleep(0.05)                             # what it started
        if sig is None or not alive(proc):
            return
        _signal(proc, sig)
        if running(proc):
            proc.wait()


def writers(runs: list[Path]) -> set[Path]:
    """The runs (folders right under one of `runs`) a process may write now, whichever process: one whose current
    folder is in the run, or which holds a file of it open for writing (Linux: /proc; elsewhere none is known). A job of
    a server gone, or what left a job's process group, is seen here too."""
    tops = [os.path.realpath(r) + os.sep for r in runs]
    found: set[Path] = set()

    def note(path: str) -> bool:
        for top in tops:
            if path.startswith(top):
                found.add(Path(top) / path[len(top):].split(os.sep, 1)[0])
                return True
        return False

    try:
        pids = [n for n in os.listdir("/proc") if n.isdigit()]
    except OSError:
        return found
    for pid in pids:
        try:
            note(os.readlink(f"/proc/{pid}/cwd"))
            fds = os.listdir(f"/proc/{pid}/fd")
        except OSError:                                  # gone, or not ours to read
            continue
        for fd in fds:
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
                if not any(target.startswith(top) for top in tops):
                    continue
                with open(f"/proc/{pid}/fdinfo/{fd}") as f:
                    flags = next((int(ln.split()[1], 8) for ln in f if ln.startswith("flags:")), 0)
            except (OSError, ValueError):
                continue
            if flags & os.O_ACCMODE != os.O_RDONLY:
                note(target)
    return found


def _find(path: Path, st: dict, procs: list[dict] | None = None) -> tuple[Foreign | None, dict | None] | None:
    """The ngspice of a job that its status says is running, when it still runs: (it, the identity to record when the
    status has none), (None, None) while another live server is its parent (_serving) and has not handed it over
    (Jobs.shutdown: then it is only on its way out), None when it is gone. The process the status recorded (the same
    pid, start time and boot); for a job an earlier Undine started without recording it, an ngspice started as a job
    runs it (_ngspice), in the job's directory (`procs`: the processes there, _procs, when known)."""
    rec = st.get("process")
    if isinstance(rec, dict) and isinstance(rec.get("pid"), int) and isinstance(rec.get("start"), int):
        now = _proc_stat(rec["pid"]) if rec.get("boot") == _boot() else None
        if now is None or now[2] != rec["start"] or now[0] in "ZX":
            return None
        if not st.get("handed_over") and _serving(now[1]):
            return None, None
        return Foreign(rec["pid"], rec["start"]), None
    try:
        here = path.resolve()
    except OSError:
        return None
    for p in procs if procs is not None else sorted(_procs(path), key=lambda p: p["start"]):
        if p["cwd"] == here and p["state"] not in "ZX" and _ngspice(p["argv"]):
            if _serving(p["ppid"]):
                return None, None
            return Foreign(p["pid"], p["start"]), {"pid": p["pid"], "start": p["start"], "boot": _boot(),
                                                   "cmd": p["argv"], "cwd": str(path)}
    return None


INTERRUPTED = {
    "running": "its ngspice is gone and no server saw it end (the server was stopped, restarted or killed meanwhile, "
               "and ngspice was killed or crashed, or the machine restarted), so its results were never collected: run "
               "it again",
    "queued": "it waited in the queue of a server that stopped, and no later server took up its place in line, so it "
              "never ran: run it again",
    # a job of another kind (DRC, LVS, RTL, LibreLane, extraction) that was running
    "other": "the server that ran it stopped (or was restarted) before it ended, and a later server takes up only "
             "simulations, so its results were never collected: run it again",
}


def _simulation(st: dict) -> bool:
    """Whether a job is an ngspice run (Jobs.submit: kind "sim", or none in a script's), not a DRC, LVS, RTL ... one."""
    return st.get("kind", "sim") == "sim"
# what submit says once the server has begun to stop (Jobs.shutdown)
STOPPING = ("the server is stopping: no new run starts in it; run it again once the server is back (a restart takes a "
            "few seconds)")
# a Foreign process (its exit code unknown) whose log ngspice did not finish (ENDED)
CUT = ("ngspice ended before it finished its log: it was killed or it crashed (it ran on while the server was "
       "restarted, so its exit code is not known)")


class Jobs:
    """The simulation jobs of a server (`root`: its projects root; their life across restarts in the module
    docstring), or of a script (no root: they end with it).

    One server process at a time holds a root's jobs (`owner`: it holds <root>/LOCK, which the system lets go of when
    the process ends, however it ends): it adopts what an earlier one left and keeps the queue. Another server started
    on the same root meanwhile (the next one while the last one still shuts down, or one too many) runs its own jobs,
    takes nobody's, lists its unfinished ones in a file of its own (OTHERS), and becomes the owner, resuming what is
    left, once the holder has gone (or handed over: shutdown). Each job's status names the server that runs it
    ("server"); what another server leaves while the owner runs (it stopped, or was killed) is taken up by the owner
    within PARK_POLL_S, or when it is read (_others). A simulation whose ngspice another live server still runs as its
    child (that second server's own, or one an Undine before this version runs until it has gone) is parked: looked at
    again every PARK_POLL_S, and adopted once that server has gone."""

    def __init__(self, limit: int | None = None, root: Path | None = None):
        self.live: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.limit = limit or job_limit()          # simulation jobs running at once; the others wait in `waiting`
        self.waiting: list[Job] = []
        self.active: set[Job] = set()
        self.turn = threading.Condition(self.lock)
        self.started = time.time()                 # a job created before is an earlier server process's
        # status of finished jobs, which no longer changes (find), by the status file's time and size
        self.final: dict[Path, tuple[tuple[int, int], dict]] = {}
        self.root = root
        self.owner = False                         # holds the root's LOCK: what an earlier server left is this one's
        self.others = False                        # another server process holds it (this one waits: _take_over)
        self.closing = False                       # shutdown has begun: nothing new is taken (submit refuses)
        self.ending = False                        # asked to stop (close): no job of another kind starts any more
        self.closed = False                        # shutdown: this server process is going
        self.halting: dict[int, object] = {}       # processes being stopped (cancel, _stop_left), by pid: _stop_all
        self.pending: dict[str, dict] = {}         # the jobs not started yet, as QUEUE has them (_journal)
        self.gone: set[str] = set()                # running jobs of an earlier server whose ngspice is gone
        self.parked: dict[str, Job] = {}           # running jobs whose ngspice another live server runs (_park)
        self._unparking = False                    # a thread looks at them (_unpark)
        self.resumed: dict[str, list[str]] = {"adopted": [], "queued": []}      # what resume took up
        self._held = None                          # the open LOCK file while this one is the owner
        self._released = False                     # shutdown let go of the root: nothing is written for it any more
        self.token = uuid.uuid4().hex[:12]         # this Jobs, as a job's "server" and its OTHERS file name it
        self._mine = root / OTHERS.replace("*", self.token) if root is not None else None
        self.starting = 0                          # jobs between their turn and their process recorded (_run, shutdown)
        self._taking = threading.Lock()            # one _others at a time
        self.on_owner = lambda: None               # called once this one becomes the owner after waiting (_take_over)
        _ALL.add(self)
        if root is not None and (f := self._lockfile()) is not None:
            if self._own(f, wait=False):
                self.resumed = self.resume()
            else:
                self.others = True
                threading.Thread(target=self._take_over, args=(f,), daemon=True).start()

    @property
    def keeper(self) -> bool:
        """Whether this server keeps what goes on in the next one (its jobs' queue, mcp.Sessions): it holds the root
        (or there is no lock to hold), and has not let go of it (shutdown)."""
        return self.root is not None and not self.others and not self._released

    def _server(self) -> dict | None:
        """This server as a job's status names the one that runs it ("server"); None for a script's jobs."""
        return {**_me(), "jobs": self.token} if self.root is not None and ADOPT else None

    def here(self) -> dict:
        """What a job of another kind (DRC, LVS, extraction, RTL ...) records of the server that runs it, as a
        simulation's status does ({"server": ...}; none for a script's): a later server tells by it whether that server
        is still there. And who started it (live.started_by: owner, reader), as start_sim records it of a simulation."""
        from undine.server import live
        return {**({"server": srv} if (srv := self._server()) else {}), **live.started_by()}

    def _elsewhere(self, job: Job, st: dict) -> bool:
        """Whether another server works on a job this one does not: another Jobs of this process has it, or the server
        its status names (one more on the root) is still there and has not handed it over (shutdown)."""
        if any(o is not self and not o.closed and job.id in o.live for o in list(_ALL)):
            return True
        rec = st.get("server")
        return isinstance(rec, dict) and rec.get("jobs") != self.token and not st.get("handed_over") \
            and _server_runs(rec)

    def interrupted(self, job: Job, st: dict | None = None) -> bool:
        """Whether a job whose status says queued or running, that this server process does not run, is one no one
        works on any more: its ngspice is gone (one that still runs is adopted instead, and one that finished meanwhile
        is collected: _take), it waited in a queue that no server took up (resume, _others), or it is not a simulation.
        Such a job is an earlier server process's (created before this one held the root), or another server's on the
        root (its status names it) that has gone or handed it over since: what that one left is taken up first
        (_others). A server that does not hold the root judges without taking anything up (_abandoned); none once
        this one is going (the next one says). A job of another kind found so has what is left of its process stopped
        (_stop_left: its server was killed). Every job says when it was created; one that does not cannot be told
        apart, and is taken as it says, as is one an Undine before this version runs beside this one, and one this
        server made since it started (its status names it: a check's before it is live)."""
        st = job.status() if st is None else st
        created = st.get("created")
        if not (st.get("state") in ("queued", "running") and job.id not in self.live
                and isinstance(created, (int, float))):
            return False
        rec = st.get("server")
        if self.closing or self._elsewhere(job, st) \
                or (isinstance(rec, dict) and rec.get("jobs") == self.token and created >= self.started):
            return False
        if self.others:
            out = self._abandoned(job, st)
        else:
            if not isinstance(rec, dict):
                if created >= self.started:
                    return False
            else:
                self._others()
                st = job.status()
                if job.id in self.live or st.get("state") not in ("queued", "running"):
                    return False
            out = st["state"] == "queued" or self._take(job, st) is False
        if out and not _simulation(st):
            self._stop_left(st)
        return out

    def _abandoned(self, job: Job, st: dict) -> bool:
        """For a server that does not hold the root (another does, and takes up what a server that has gone left):
        whether no one works on a job, judged without taking anything up. Its status names a server that has gone or
        handed it over (_elsewhere said so), or names none and it was created before this server started (an earlier
        server's, as the holder judges it by its own start), and it is not a simulation (none is taken up), or it is a
        running one whose ngspice is gone without finishing its log (nothing to adopt or collect). A queued one, and
        one whose ngspice runs or finished meanwhile, is the holder's to take up."""
        if not isinstance(st.get("server"), dict) and st["created"] >= self.started:
            return False
        if not _simulation(st):
            return True
        if st["state"] != "running" or not isinstance(st.get("process"), dict) or not ADOPT:
            return False
        return _find(job.path, st) is None and not ENDED.search(job.log(50))

    def status(self, job: Job) -> dict:
        """job.status(), but "interrupted" (with why: INTERRUPTED) for a job an earlier server process left queued or
        running that no one works on any more."""
        st = job.status()
        if not self.interrupted(job, st):
            return st
        why = INTERRUPTED[st["state"] if st["state"] == "queued" or _simulation(st) else "other"]
        return {**st, "state": "interrupted", "error": why}

    def working(self, job: Job, st: dict | None = None) -> bool:
        """Whether someone works on a job whose status says queued or running: this server process (its own, or one it
        adopted: interrupted adopts one it can) or another one (that holds the root while this one does not, or runs
        its ngspice: parked). One that does not say when it was created cannot be told apart from an earlier server's:
        no one, as before jobs outlived their server."""
        st = job.status() if st is None else st
        return st.get("state") in ("queued", "running") and (
            job.id in self.live or (isinstance(st.get("created"), (int, float)) and not self.interrupted(job, st)))

    def runs_dir(self, project_path: Path) -> Path:
        d = project_path / "runs"
        d.mkdir(exist_ok=True)
        return d

    def list(self, project_path: Path) -> list[dict]:
        """The project's newest jobs' status, as status() tells it for one not finished (adopted, or interrupted)."""
        out = []
        for d in sorted(self.runs_dir(project_path).iterdir(), reverse=True):
            if (d / "status.json").is_file():
                st = (job := Job(d)).status()
                out.append(self.status(job) if st.get("state") in ("queued", "running") else st)
                if len(out) == 100:                      # the newest; a sweep leaves hundreds of runs behind
                    break
        return out

    FINAL = ("key", "state", "kind", "tool", "deck", "lib", "cell")   # what _scan keeps of a finished job's status

    def _scan(self, project_path: Path):
        """(run directory, status: the FINAL fields) of all of the project's runs, newest first. A job whose status says
        queued or running but that no one works on (an earlier server process left it, and its process is gone:
        working, which adopts it when its process still runs) never finishes: it is skipped. The status of a finished
        job is read once (kept while its file is unchanged)."""
        for d in sorted(self.runs_dir(project_path).iterdir(), reverse=True):
            try:
                f = (d / "status.json").stat()
            except OSError:
                continue
            stamp = (f.st_mtime_ns, f.st_size)
            hit = self.final.get(d)
            if hit is not None and hit[0] == stamp:
                st = hit[1]
            else:
                st = Job(d).status()
                if st.get("state") in ("done", "failed", "cancelled"):
                    self.final[d] = (stamp, {k: st.get(k) for k in self.FINAL})
            if st.get("state") in ("queued", "running") and d.name not in self.live:
                job = Job(d)
                st = job.status()                        # or it finished just now
                if st.get("state") in ("queued", "running") and not self.working(job, st):
                    continue
            yield d, st

    def find(self, project_path: Path, key: str, states: tuple = ("done", "running", "queued")) -> Job | None:
        """The newest job with provenance key `key` in one of `states`, among all of the project's runs (list() shows
        the newest 100 only: a sweep of more points would otherwise run its first points again when asked again)."""
        for d, st in self._scan(project_path):
            if st.get("key") == key and st.get("state") in states:
                return self.get(project_path, d.name)
        return None

    def latest(self, project_path: Path, states: tuple = ("done",), match=None, **want) -> dict | None:
        """The full status of the newest run in one of `states` whose status has the values of `want` (kind="lvs",
        lib=..., cell=...; match(status, run directory) -> bool for more), among all of the project's runs: list()
        shows the newest 100, and a check_specs over corners or a sweep leaves a cell's last DRC / LVS behind them."""
        for d, st in self._scan(project_path):
            if st.get("state") in states and all(st.get(k) == v for k, v in want.items()):
                full = Job(d).status()
                if match is None or match(full, d):
                    return full
        return None

    def get(self, project_path: Path, job_id: str) -> Job:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
            raise KeyError("bad job id")
        d = self.runs_dir(project_path) / job_id
        if not d.is_dir():
            raise KeyError(f"no job {job_id}")
        with self.lock:
            return self.live.get(job_id) or Job(d)

    def submit(self, project_path: Path, title: str, netlist: str, analysis: dict | None,
               meta: dict | None = None, osdi: list[Path] | None = None, timeout_s: float | None = None,
               models: tuple[Path, str] | None = None) -> Job:
        """`osdi`: compiled Verilog-A models (IHP's PSP103) ngspice loads before it reads the netlist. `timeout_s`: the
        run's time limit (time_limit). `models` (the PDK's model library, its PDK id): the netlist's `.lib` lines of that
        library read a copy reduced to what the netlist uses (modellib.reduce); the status' "models" and the first lines
        of the log say how each section was read, and why one was read whole."""
        if self.closing:
            raise RuntimeError(STOPPING)
        ngspice = tools.find("ngspice")
        if not ngspice:
            raise RuntimeError("ngspice not found (set UNDINE_TOOLS or put it on PATH)")
        limit = time_limit(timeout_s)
        text = netlist
        replaced_control = False
        if analysis:
            # an analysis chosen in the panel replaces the schematic's own: its .control blocks (which would otherwise
            # run instead and write their own raw file) and its analysis lines (which ngspice would run as well, into
            # the same raw file); .lib / .save / .param / .meas lines stay
            card = OP_CONTROL if analysis.get("type") == "op" else analysis_card(analysis)
            if analysis.get("type") == "op":
                analysis_card(analysis)                       # validates the arguments
            stripped = ANALYSIS_LINE.sub("", CONTROL.sub("", text))
            replaced_control = stripped != text
            text = _before_end(stripped, card)
        has_control = re.search(r"^\s*\.control", text, re.M | re.I) is not None
        if not has_control and re.search(r"^\s*\.meas(ure)?\b", text, re.M | re.I):
            # ngspice evaluates no .meas in batch mode with -r ("No .measure possible in batch mode (-b) with -r
            # rawfile set"): run the analysis from a .control block instead, which also writes the raw file
            text = _before_end(text, MEAS_CONTROL)
            has_control = True
        elif has_control:
            text = quit_after_control(text)
        text, pars = par_sources(text)
        notes = [f"{pars} par() expressions were computed by B sources (nodes undine_par1 ...): ngspice takes at most "
                 f"{PAR_MAX} par() calls in one run"] if pars else []
        jid, d = new_run(self.runs_dir(project_path))
        records = []
        if models is not None:
            text, records = modellib.reduce(text, models[0], models[1], cwd=d)
        (d / "netlist.cir").write_text(text)
        # ngspice reads .spiceinit from its working directory before the netlist: the thread count, and the models
        # to load, as the PDK's own .spiceinit does
        (d / ".spiceinit").write_text(spiceinit(osdi))
        meta = dict(meta or {})
        if notes:
            meta["notes"] = [*meta.get("notes", []), *notes]
        if records:
            meta["models"] = records
        job = Job(d)
        with self.turn:                                  # live before its status says queued (find skips one that is not)
            if self.closing:                             # shutdown began meanwhile: it would be no one's
                import shutil
                shutil.rmtree(d, ignore_errors=True)
                raise RuntimeError(STOPPING)
            self.live[jid] = job
            now = time.time()
            free = not self.waiting and len(self.active) < self.limit      # a free slot and no one in line: it runs
            if free:
                self.active.add(job)
            else:
                self.waiting.append(job)
            self._enqueue(job, ngspice, not has_control, limit)
            job.set(id=jid, title=title, state="running" if free else "queued", created=now, analysis=analysis or "netlist",
                    time_limit_s=limit, **({"started": now} if free else {"queue_position": len(self.waiting)}),
                    **({"server": srv} if (srv := self._server()) else {}),
                    **({"replaced_control": True} if replaced_control else {}), **meta)
        threading.Thread(target=self._run, args=(job, ngspice, not has_control, limit), daemon=True).start()
        return job

    def _renumber(self) -> None:
        for i, j in enumerate(self.waiting):
            if j.status().get("queue_position") != i + 1:
                j.set(queue_position=i + 1)

    def _run(self, job: Job, ngspice: str, use_dash_r: bool, limit: float) -> None:
        with self.turn:                                 # in order, while fewer than `limit` run
            while not job.cancelled and not self.closed and job not in self.active and \
                    (self.waiting[0] is not job or len(self.active) >= self.limit):
                self.turn.wait()
            if self.closed:                             # the server is going: the next one starts it (resume)
                return
            if job in self.waiting:                     # its turn
                self.waiting.remove(job)
                self._renumber()
                if not job.cancelled:
                    self.active.add(job)
                    job.set(state="running", started=time.time(), drop=("queue_position",))
                self.turn.notify_all()                  # the next in line is first now, and a slot may still be free
            if job.cancelled:
                self.active.discard(job)
                self.live.pop(job.id, None)
                self._dequeue(job)
                self.turn.notify_all()
                return
            t0 = job.status().get("started") or time.time()
            self.starting += 1                          # shutdown waits until its process is recorded
        cmd = [ngspice, "-b", "-o", "log.txt"]
        if use_dash_r:
            cmd += ["-r", "out.raw"]
        cmd.append("netlist.cir")
        env = {**tools.env(), **({"OMP_NUM_THREADS": str(THREADS)} if THREADS > 0 else {})}
        try:
            # a process group (a session) of its own (Process): Ctrl-C in the server's terminal does not reach it, a
            # server's runs on when the server stops (the next server adopts it), and a stop ends what it started too;
            # a script's ends with it, as before (_stop_all)
            job.proc = Process(cmd, cwd=job.path, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        except Exception as e:
            with self.turn:
                self._dequeue(job)
                job.set(state="failed", error=str(e), finished=time.time())
                self.starting -= 1
                self.turn.notify_all()
            self._release(job)
            return
        with self.turn:                                 # what a later server needs to adopt it, before it is off QUEUE
            job.set(process=identity(job.proc.pid, cmd, job.path))
            self._dequeue(job)
            self.starting -= 1
            self.turn.notify_all()
        if job.cancelled:                               # cancelled while it started
            stop(job.proc)
        self._watch(job, limit, t0)

    def _watch(self, job: Job, limit: float, t0: float) -> None:
        """Follows a running job to its end (_follow), then frees its slot."""
        try:
            self._follow(job, limit, t0)
        except Exception as e:
            if not self.closed:
                job.set(state="failed", error=str(e), finished=time.time())
        finally:
            self._release(job)

    def _release(self, job: Job) -> None:
        with self.turn:
            self.active.discard(job)
            self.live.pop(job.id, None)
            self._ended(job)
            self.turn.notify_all()

    def _follow(self, job: Job, limit: float, t0: float) -> None:
        """Waits for a job's process, at most until its time limit (counted from t0, its start), and records how it
        ended: failed or done, by its log, its raw files and its exit code; a Foreign process's is not known, and a log
        that ngspice did not finish (ENDED) then says that it was killed or crashed, and its end is when it last wrote
        its log (it may have ended while no server ran). Nothing once this server process is going (shutdown): the job
        is the next server's."""
        try:
            rc = job.proc.wait(timeout=max(0.0, t0 + limit - time.time()))
        except subprocess.TimeoutExpired:
            if self.closed:
                return
            stop(job.proc)
            _log_models(job)
            with self.lock:
                if not job.cancelled:
                    job.set(state="failed", timed_out=True, finished=time.time(), seconds=time.time() - t0,
                            error=f"stopped at its time limit of {limit:g} s; a longer run needs time_limit_s "
                                  f"(at most {TIMEOUT_MAX_S:g} s)")
            return
        if self.closed:
            return
        end = time.time()
        if isinstance(job.proc, Foreign):
            try:
                end = min(end, (job.path / "log.txt").stat().st_mtime)
            except OSError:
                pass
        _log_models(job)
        from undine.simulation import simresults
        log = job.log(5000)
        errors = simresults.log_errors(log)
        # the exit code alone does not tell: the `quit` that ends a control block (MEAS_CONTROL, quit_after_control)
        # makes it 0 also when the analysis aborted (and `write` still wrote what it had)
        aborted = simresults.aborted(log)
        raws = job.raw_files()
        if job.cancelled:
            return
        if isinstance(job.proc, Foreign) and not ENDED.search(log):
            # killed or crashed while no server watched it (whatever its log says before that): "cut", which a
            # changeset's evidence tells by (what it started may outlive it), with the last error of its log if any
            last = aborted or (errors[-1] if errors else None)
            st = dict(state="failed", rc=rc, cut=True, error=CUT, **({"log_error": last} if last else {}))
        elif (rc is not None and rc != 0) or aborted or (errors and not raws):
            st = dict(state="failed", rc=rc, error=aborted or (errors[-1] if errors else f"exit code {rc}"))
        else:
            warnings = list(dict.fromkeys(" ".join(e.split()) for e in errors))[:5]     # the same line once
            st = dict(state="done", rc=rc, raw=[p.name for p in raws], warnings=warnings, **provenance.outputs(job.path))
        with self.lock:
            if not job.cancelled:
                job.set(**st, finished=end, seconds=max(0.0, end - t0))

    def cancel(self, job: Job, why: str | None = None, state: str = "cancelled") -> bool:
        """Stops a job that is queued or running here (its process: terminated, then killed), an adopted one too (one
        an earlier server left running is adopted first, status); False when there is nothing to stop. `why`: the
        cancelled job's error (shutdown's); `state`: what its status says then (close's: "interrupted"). A job that has
        ended, or that no one works on any more (interrupted), whose process group is still there (what its process
        started outlived it; a killed server's tools) has that stopped (_stop_left), and True too; so has one this
        server still holds that has just ended (its runner lets go of it right after). A job of another kind (DRC, LVS,
        RTL ...) this server runs is cancelled also between two of its steps (Magic, then Netgen; a build, then its
        run): its runner starts no further one (step) and records nothing over the cancel (record)."""
        if job.id not in self.live:
            rest = left(job.status())                   # before status, which may stop it itself (interrupted)
            st = self.status(job)
            if job.id not in self.live and st.get("state") not in ("queued", "running"):
                if rest is None:
                    return False
                self._halt(rest)
                return True
        rest = None
        with self.turn:
            job = self.live.get(job.id, job)
            ours = job in self.active or job in self.waiting
            st = job.status()
            if job.cancelled:
                return False
            if st.get("state") not in ("queued", "running"):
                rest = job.proc if alive(job.proc) else left(st)
            elif not (ours or alive(job.proc) or (job.id in self.live and not _simulation(st))):
                return False
            else:
                job.cancelled = True
                job.set(state=state, finished=time.time(), drop=("queue_position",), **({"error": why} if why else {}))
                self._dequeue(job)
                self.turn.notify_all()
                rest = job.proc if alive(job.proc) else None
                if rest is None:
                    return True
        if rest is None:
            return False
        self._halt(rest)
        return True

    def _stop_left(self, st: dict) -> bool:
        """Stops (in the background) the process group a job's status recorded (left) when any of it is still there:
        the tools of a run whose server was killed (no exit of its ended them), or what a job's process started that
        outlived it. False: nothing is there."""
        proc = left(st)
        if proc is None:
            return False
        self._halt(proc)
        return True

    def _halt(self, proc) -> None:
        """stop(proc) in a thread of its own; until it has, the process is in `halting`, which _stop_all kills at the
        exit (a cancel the server's exit cuts short, what ignores SIGTERM included)."""
        with self.lock:
            if proc.pid in self.halting:
                return
            self.halting[proc.pid] = proc

        def run():
            try:
                stop(proc)
            finally:
                with self.lock:
                    if self.halting.get(proc.pid) is proc:
                        del self.halting[proc.pid]
        threading.Thread(target=run, daemon=True).start()

    # ------------------------------------------------------------------ jobs of other kinds (DRC, LVS, RTL ...)

    def ensure_open(self) -> None:
        """Raises STOPPING once the server is stopping (close, shutdown): a job of another kind started now would end
        with it before it ends (no later server takes one up)."""
        if self.ending or self.closing:
            raise RuntimeError(STOPPING)

    def track(self, job: Job) -> None:
        """Holds a job of another kind that this server runs (`live`: cancel stops it, a closure waits for it, close
        ends it); refused once the server is stopping (ensure_open). Its runner lets go of it when it ends (untrack)."""
        with self.lock:
            self.ensure_open()
            self.live[job.id] = job

    def untrack(self, job: Job) -> None:
        with self.lock:
            self.live.pop(job.id, None)

    def record(self, job: Job, **kw) -> bool:
        """Sets a tracked job's status unless it was cancelled (cancel, close: under the same lock), so that its runner
        never writes over how it was stopped. False: it was."""
        with self.turn:
            if job.cancelled:
                return False
            job.set(**kw)
            return True

    def step(self, job: Job, argv: list, **kwargs) -> "Process | None":
        """Starts a tracked job's next process (a Process, recorded in its status, its `proc`) unless the job was
        cancelled (None), under the lock cancel takes: a cancel never misses a process that is starting, nor does a
        step start after one (Magic's extraction, then Netgen)."""
        with self.turn:
            if job.cancelled:
                return None
            return Process(argv, job=job, **kwargs)

    def close(self) -> None:
        """The server is asked to stop (`undine serve`: Ctrl-C, SIGTERM, SIGHUP, while it still serves; shutdown when
        it has not been): no job of another kind starts in it any more (ensure_open), and those it runs end now, their
        status "interrupted" (INTERRUPTED["other"]), what their processes started too. No later server takes one up (it
        would end with this server's exit anyway: _stop_all), and the next server, which may serve while this one still
        stops, must not take one for a job still worked on and reuse it (a check: find) only to have it killed. Its
        simulations go on: shutdown hands them over."""
        with self.lock:
            self.ending = True
            others = [j for j in self.live.values() if j not in self.active and j not in self.waiting]
        for job in others:
            if not _simulation(job.status()):
                self.cancel(job, INTERRUPTED["other"], state="interrupted")

    # ------------------------------------------------------------------ across restarts

    def _take(self, job: Job, st: dict, procs: list[dict] | None = None) -> bool | None:
        """Adopts a running simulation of an earlier server process whose ngspice still runs (_find): it holds a slot,
        its time limit counts from its start, cancel stops it, and its end is recorded as any job's (_follow). One whose
        ngspice finished its log while no server watched it (a restart's gap) is adopted ended, and so collected now.
        True: this one follows it (now); None: another does (the server process that holds the root while this one does
        not; the live server whose child its ngspice is: parked until it has gone; another Jobs of this process); False:
        no one will finish it: its ngspice is gone (remembered: it does not come back), it is not a simulation (DRC,
        LVS, RTL ...: their tools end with their server), or nothing here adopts (a script's jobs, not Linux)."""
        key = str(job.path)
        if self.root is None or not ADOPT or key in self.gone or not _simulation(st):
            return False
        if job.id in self.live:
            return True
        if any(o is not self and not o.closed and job.id in o.live for o in list(_ALL)):
            return None
        found = _find(job.path, st, procs)
        if found is None and ENDED.search(job.log(50)):
            found = Foreign(None, None), None                         # ended: its wait returns at once
        if found is None:
            self.gone.add(key)
            return False
        proc, new = found
        with self.turn:
            if job.id in self.live:
                return True
            if not self.owner or self.closing:
                return None
            if proc is None:
                self._park(job)
                return None
            self.parked.pop(job.id, None)
            job.proc = proc
            self.live[job.id] = job
            self.active.add(job)
            job.set(adopted=time.time(), server=self._server(), **({"process": new} if new else {}))
        limit = float(st.get("time_limit_s") or TIMEOUT_S)
        t0 = st.get("started") or st.get("created") or time.time()
        threading.Thread(target=self._watch, args=(job, limit, t0), daemon=True).start()
        return True

    def _park(self, job: Job) -> None:
        """A simulation whose ngspice another live server runs as its child (_serving): an Undine before this version
        on its way out, which records its end itself unless it ends first, or a second server on the root, whose own
        it is. Looked at again every PARK_POLL_S (_unpark) until it has ended or is adopted (with self.turn held)."""
        self.parked[job.id] = job
        if not self._unparking:
            self._unparking = True
            threading.Thread(target=self._unpark, daemon=True).start()

    def _unpark(self) -> None:
        while True:
            time.sleep(PARK_POLL_S)
            with self.turn:
                if self.closed:
                    self.parked.clear()
                jobs = list(self.parked.values())
                if not jobs:
                    self._unparking = False
                    return
            for job in jobs:
                st = job.status()
                if st.get("state") != "running" or self._take(job, st) is not None:
                    with self.turn:
                        self.parked.pop(job.id, None)

    def resume(self) -> dict:
        """What an earlier server process left on this root goes on here: each running job whose ngspice still runs is
        adopted (_take; found by the processes working in the root's directories), then the jobs it had not started
        (QUEUE) wait their turn in their order, among them one whose status says running but whose process was never
        recorded (that server ended as it started it), and then what other servers that have gone left (_others).
        Returns {"adopted": [job ids], "queued": [job ids]}."""
        out: dict[str, list[str]] = {"adopted": [], "queued": []}
        if ADOPT:
            base = self.root.resolve()
            dirs: dict[Path, list[dict]] = {}
            for p in _procs(self.root):
                dirs.setdefault(p["cwd"], []).append(p)
            for cwd, procs in dirs.items():
                job = Job(self.root / cwd.relative_to(base))
                st = job.status()
                if st.get("state") != "running" or job.id in self.live:
                    continue
                if not _simulation(st):                 # a DRC's, LVS's ... tools a killed server left: stopped
                    if isinstance(st.get("server"), dict) and not self._elsewhere(job, st):
                        self._stop_left(st)
                elif self._take(job, st, sorted(procs, key=lambda p: p["start"])):
                    out["adopted"].append(job.id)
        for e in self._journaled():
            job = Job(self._dir(e["add"]))
            st = job.status()
            if job.id in self.live or not (st.get("state") == "queued"
                                           or (st.get("state") == "running" and not st.get("process"))):
                continue
            if self._requeue(job, e):
                out["queued"].append(job.id)
        for kind, jid in self._others(journal=False):
            out[kind].append(jid)
        with self.lock:
            self._journal(None)
        if self._mine is not None:                      # this one's own jobs are in QUEUE now, or running here
            self._mine.unlink(missing_ok=True)
        if ADOPT:
            threading.Thread(target=self._watch_others, daemon=True).start()
        return out

    def _requeue(self, job: Job, e: dict, journal: bool = True) -> bool:
        """A job another server process had not started (QUEUE's line, or another server's: `e`) waits its turn here,
        at the end of the line; one whose ngspice cannot be found fails. `journal`: onto QUEUE now (resume writes it
        whole afterwards)."""
        ngspice = e.get("ngspice") if e.get("ngspice") and Path(e["ngspice"]).is_file() else tools.find("ngspice")
        if not ngspice:
            job.set(state="failed", error="ngspice not found (set UNDINE_TOOLS or put it on PATH)",
                    finished=time.time(), drop=("queue_position", "started"))
            return False
        with self.turn:
            if job.id in self.live or self.closing:
                return False
            self.live[job.id] = job
            self.waiting.append(job)
            self.pending[e["add"]] = e
            if journal:
                self._journal(e)
            job.set(state="queued", queue_position=len(self.waiting), resumed=time.time(), server=self._server(),
                    drop=("started",))
        threading.Thread(target=self._run, args=(job, ngspice, bool(e.get("dash_r")), float(e.get("limit") or TIMEOUT_S)),
                         daemon=True).start()
        return True

    def _others(self, journal: bool = True) -> list[tuple[str, str]]:
        """What the other servers on the root (each with an OTHERS file: _journal) left goes on here, once each has gone
        or handed its jobs over (shutdown): its running jobs are adopted (_take: or parked, or collected; one that never
        started waits its turn), and those it had not started wait their turn here, in their order. Only by the owner.
        Returns [("adopted" or "queued", job id)]."""
        out: list[tuple[str, str]] = []
        if self.root is None or not self.owner or self.closing:
            return out
        with self._taking:
            for f in sorted(self.root.glob(OTHERS)):
                if f == self._mine:
                    continue
                server, entries, over = _read_others(f)
                if not over and (_server_runs(server) or (server is None and _fresh(f))):
                    continue                            # that server still runs them (or is writing its first line)
                for e in entries:
                    job = Job(self._dir(e["add"]))
                    st = job.status()
                    if job.id in self.live or st.get("state") not in ("queued", "running"):
                        continue
                    if st["state"] == "running":
                        took = self._take(job, st)
                        if took:
                            out.append(("adopted", job.id))
                        if took is not False or st.get("process"):
                            continue
                        self.gone.discard(str(job.path))   # it never started: its turn comes here
                    if self._requeue(job, e, journal):
                        out.append(("queued", job.id))
                f.unlink(missing_ok=True)
        return out

    def _watch_others(self) -> None:
        """The owner looks for what other servers on the root left (_others) every PARK_POLL_S while it runs."""
        while not self.closed:
            time.sleep(PARK_POLL_S)
            try:
                if (took := self._others()):
                    print(f"undine: took up the simulations another server left in {self.root}: {took}",
                          file=sys.stderr)
            except Exception as e:                       # a status unreadable for a moment: looked at again
                print(f"undine: cannot take up another server's simulations in {self.root}: {e}", file=sys.stderr)

    def shutdown(self, stop_jobs: bool = False) -> None:
        """This server process is going (`undine serve` on its way out). Its jobs are left for the next server on its
        root (resume), or for the owner when this one is not (_others): ngspice runs on, its status says it was handed
        over (so that it is adopted at once, also while this process is still on its way out, its parent), the jobs
        not started yet stay in QUEUE (or this one's OTHERS file), and nothing here starts, stops or finishes a job any
        more; a job whose turn came is first let start and its process recorded (so that it is never started twice);
        the root is let go of. With `stop_jobs` (`undine serve --stop-jobs`), every queued and running job is cancelled
        first, its process ended with what it started (its group, an adopted one's too). Where no later server could
        adopt a process (not Linux), it ends with this one, as a script's jobs do (_stop_all). From its start nothing
        new is taken (`closing`: submit refuses), so that no job starts between the list of those to stop and the hand
        over, and none is accepted that no server would run. Its jobs of other kinds (DRC, LVS, RTL ...) have ended
        before (close: as the server was asked to stop)."""
        self.close()
        with self.lock:
            self.closing = True
            jobs = [*self.waiting, *self.active] if stop_jobs else []
        if stop_jobs:
            for job in jobs:
                self.cancel(job, "the server was stopped with --stop-jobs, and its simulations with it")
            ends = [threading.Thread(target=stop, args=(j.proc,)) for j in jobs if alive(j.proc)]
            for t in ends:
                t.start()
            for t in ends:
                t.join()
        with self.turn:
            self.closed = True
            self.turn.notify_all()
            while self.starting:
                self.turn.wait(1.0)
            if ADOPT and self.root is not None:
                now = time.time()
                for job in self.active:
                    if not job.cancelled and running(job.proc):
                        job.set(handed_over=now)
                for job in self.waiting:
                    if not job.cancelled:
                        job.set(handed_over=now)
                if self.others and self._mine.exists():
                    self._journal({"handed_over": now})
            self._released = True
            if self._held is not None:
                self._held.close()                     # the next server on this root takes it up
                self._held = None

    def _lockfile(self):
        """The root's LOCK, open; None where there is no flock (not POSIX) or it cannot be opened."""
        try:
            import fcntl  # noqa: F401
            return open(self.root / LOCK, "a")
        except (ImportError, OSError):
            return None

    def _own(self, f, wait: bool) -> bool:
        """Takes the root: flocks its LOCK (`f`), which the system lets go of when this process ends, however it ends.
        False while another process (or another Jobs of this one) holds it; `wait`: until it is free."""
        import fcntl
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except OSError:
            if wait:
                f.close()
            return False
        with self.lock:
            if self.closing:                         # this one is going: the next one's
                f.close()
                return False
            self._held, self.owner, self.others = f, True, False
            if wait:      # what the holder started meanwhile is an earlier server process's too (its own are live)
                self.started = time.time()
        return True

    def _take_over(self, f) -> None:
        """Another server process holds the root: this one waits for it to go, then resumes what it left."""
        if self._own(f, wait=True):
            self.resumed = self.resume()
            if self.resumed["adopted"] or self.resumed["queued"]:
                print(f"undine: took up the simulations another server left in {self.root}: {self.resumed}",
                      file=sys.stderr)
            self.on_owner()

    def _key(self, job: Job) -> str:
        """A job's directory as QUEUE names it: from the root (absolute when it is elsewhere)."""
        try:
            return job.path.relative_to(self.root).as_posix()
        except ValueError:
            return str(job.path)

    def _dir(self, key: str) -> Path:
        return self.root / key

    def _enqueue(self, job: Job, ngspice: str, dash_r: bool, limit: float) -> None:
        """A job submitted: on QUEUE until its process is recorded, or on this one's OTHERS file until it ends (with
        self.lock held)."""
        if self.root is not None:
            key = self._key(job)
            self.pending[key] = {"add": key, "ngspice": ngspice, "dash_r": dash_r, "limit": limit}
            self._journal(self.pending[key])

    def _dequeue(self, job: Job) -> None:
        """A job started (its process recorded) or will not (cancelled): off QUEUE (with self.lock held)."""
        if self.root is not None and self.pending.pop(key := self._key(job), None) is not None and not self.others:
            self._journal({"drop": key})

    def _ended(self, job: Job) -> None:
        """A job of a server that is not the owner ended: off its OTHERS file (with self.lock held)."""
        if self.root is not None and self.others:
            self._journal({"drop": self._key(job)})

    def _journaled(self) -> list[dict]:
        """The jobs QUEUE says have not started, in their order: each added and not dropped (a last line cut short by a
        crash is skipped)."""
        return _read_others(self.root / QUEUE)[1]

    def _journal(self, line: dict | None) -> None:
        """What the next server needs to go on with this one's jobs, so that it does (resume, _others). The owner's:
        <root>/QUEUE, the jobs not started yet in their order: a line for each job submitted ({"add": its directory, the
        ngspice, -r or not, the time limit}) and for each that started or was cancelled ({"drop": its directory}); None
        writes it anew from `pending`; removed whenever no job is pending. Another server's (one more on the root):
        its OTHERS file, of the jobs it has not finished, from the line that names it ({"server": ...}), and that it
        handed them over ({"handed_over": time}); the owner reads it once that server has gone or handed over
        (_others), or this one does once it becomes the owner. With self.lock held; not once this one has let go of
        the root (shutdown: the next one's, then)."""
        if self.root is None or self._released or not (self.owner or self.others) or (line is None and not self.owner):
            return
        p = self.root / QUEUE if self.owner else self._mine
        try:
            if self.owner and not self.pending:
                p.unlink(missing_ok=True)
            elif line is None:
                (p.with_suffix(".tmp")).write_text("".join(json.dumps(e) + "\n" for e in self.pending.values()))
                p.with_suffix(".tmp").replace(p)
            else:
                head = "" if self.owner or p.exists() else json.dumps({"server": self._server()}) + "\n"
                with p.open("a") as f:
                    f.write(head + json.dumps(line) + "\n")
        except OSError as e:
            print(f"undine: cannot keep the simulation queue in {p}: {e}", file=sys.stderr)


def _read_others(p: Path) -> tuple[dict | None, list[dict], bool]:
    """A queue file (QUEUE, or another server's OTHERS: Jobs._journal): (the server it names, the jobs added and not
    dropped in their order, whether it handed them over). A line cut short by a crash is skipped."""
    server, out, over = None, {}, False
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return server, [], over
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if isinstance(rec.get("add"), str):
            out[rec["add"]] = rec
        elif isinstance(rec.get("drop"), str):
            out.pop(rec["drop"], None)
        elif "server" in rec:
            server = rec["server"] if isinstance(rec["server"], dict) else None
        elif "handed_over" in rec:
            over = True
    return server, list(out.values()), over


def _fresh(p: Path, s: float = 10.0) -> bool:
    """Whether a file was written in the last `s` seconds."""
    try:
        return time.time() - p.stat().st_mtime < s
    except OSError:
        return False


def _replace(path: Path, data: bytes) -> None:
    """`path` with `data`: a new file of the writer's own (`<name>.<token>.tmp`: status.json's keeps the name's start,
    which provenance.outputs leaves out), renamed in."""
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _log_models(job: Job) -> None:
    """How the job read the model library (modellib.describe), as the first lines of its log: ngspice starts the
    log afresh (-o), so they are put in when it has ended; once (the server that started it may have put them in
    before the one that adopted it collects it), in a new file renamed in (Job.set: a cancelled job ends after its
    status says so, when its files may already be a closed changeset's evidence)."""
    lines = "".join(f"Undine: {modellib.describe(r)}\n" for r in job.status().get("models") or []).encode()
    p = job.path / "log.txt"
    if lines:
        try:
            log = p.read_bytes() if p.is_file() else b""
            if not log.startswith(lines):
                _replace(p, lines + log)
        except OSError:
            pass


_ALL: "weakref.WeakSet[Jobs]" = weakref.WeakSet()


@atexit.register
def _stop_all() -> None:
    """The process ends: the jobs it started end with it, with all they started (nothing would collect their results;
    in a session of their own, a Ctrl-C at the server's terminal no longer reaches them), but for a server's
    simulations that it handed over (Jobs.shutdown, as `undine serve` does on its way out, however it was asked to
    stop) where the next server can adopt them (Linux): those run on (Jobs.resume). A script's jobs, a test's server,
    and a server's other jobs (DRC, LVS, RTL ...: no later server takes them up) end here. One it adopted (Foreign) is
    not ended here (it was not this process's, and the next server adopts it again) unless it was cancelled; nor is
    anything left that a stop was under way for (`halting`: a cancel's, what a killed server's run left): those are
    killed, since the stop's thread ends with the process."""
    for jobs in list(_ALL):
        handed = jobs.closed and ADOPT and jobs.root is not None
        found, halting = set(), []
        for _ in range(3):
            try:
                found = {*jobs.active, *jobs.live.values()}
                halting = list(jobs.halting.values())
                break
            except RuntimeError:                 # a job ending meanwhile: read again
                continue
        procs = [job.proc for job in found
                 if not ((isinstance(job.proc, Foreign) and not job.cancelled)
                         or (handed and job in jobs.active and not job.cancelled))]
        for proc in [*procs, *halting]:
            try:
                if alive(proc):                          # not a group id that may be another's now
                    _signal(proc, signal.SIGKILL)
            except Exception:                    # one that cannot be ended keeps none of the others from it
                pass


# ------------------------------------------------------------------ raw files


def read_raw(path: Path) -> dict:
    """Parses an ngspice raw file (binary or ascii). Returns all vectors as numpy arrays: a binary file's are views of
    the file mapped into memory, so a run of gigabytes is read only where a vector is used (a column of samples, not
    the whole file). The last few files read are kept parsed (by path, size and time): the summary, spectrum and
    samples of one run read it once."""
    st = path.stat()
    raw = _parse_raw(str(path), st.st_size, st.st_mtime_ns)
    return {**raw, "data": dict(raw["data"])}           # the caller's to add to (waveform_json's "point")


@functools.lru_cache(maxsize=4)
def _parse_raw(path: str, size: int, mtime: int) -> dict:
    with open(path, "rb") as f:
        head, pos, mode = _raw_head(f)
        hdr, variables = _raw_header(head)
        nvar, npts = int(hdr["No. Variables"]), int(hdr["No. Points"])
        cplx = "complex" in hdr.get("Flags", "")
        width = 2 if cplx else 1
        if mode == "Binary":
            if nvar * npts == 0:
                arr = np.zeros((npts, nvar * width))
            else:
                arr = np.memmap(path, dtype="<f8", mode="r", offset=pos, shape=(npts, nvar * width))
        else:
            f.seek(pos)
            toks = f.read().decode("latin-1").split()
            vals, i = [], 0
            for _ in range(npts):
                i += 1  # point index
                row = []
                for _v in range(nvar):
                    if cplx:
                        a, b = toks[i].split(",")
                        row += [float(a), float(b)]
                    else:
                        row.append(float(toks[i]))
                    i += 1
                vals.append(row)
            arr = np.array(vals)
    vecs: dict[str, np.ndarray] = {}
    for k, (name, _t) in enumerate(variables):
        if cplx:
            vecs[name] = arr[:, 2 * k] + 1j * arr[:, 2 * k + 1]
        else:
            vecs[name] = arr[:, k]
        vecs[name].flags.writeable = False              # shared by the readers of the kept parse
    return {"title": hdr.get("Title", ""), "plot": hdr.get("Plotname", ""), "complex": cplx,
            "vars": variables, "data": vecs}


def _raw_head(f) -> tuple[bytes, int, str]:
    """A raw file's header, up to and with its Binary: or Values: line, where the data starts, and which of the two."""
    head = b""
    while not (m := re.search(rb"^(Binary|Values):[^\n]*\n", head, re.M)):
        chunk = f.read(1 << 16)
        if not chunk:
            raise ValueError(f"{getattr(f, 'name', 'the file')} is not an ngspice raw file (no Binary: or Values: line)")
        head += chunk
    return head[:m.end()], m.end(), m.group(1).decode()


def _raw_header(head: bytes) -> tuple[dict, list[tuple[str, str]]]:
    """The header's fields and its vectors (name, type)."""
    hdr: dict[str, str] = {}
    variables: list[tuple[str, str]] = []
    lines = iter(head.decode("latin-1").split("\n"))
    for line in lines:
        if line.startswith("Variables:"):
            hdr["Variables"] = line.split(":", 1)[1].strip()
            for _ in range(int(hdr["No. Variables"])):
                parts = next(lines).split()
                variables.append((parts[1], parts[2]))
        elif line.startswith(("Binary:", "Values:")):
            break
        elif ":" in line:
            k, v = line.split(":", 1)
            hdr[k.strip()] = v.strip()
    return hdr, variables


def raw_vectors(path: Path) -> list[tuple[str, str]]:
    """The vectors (name, type) of a raw file from its header alone; none when it is not one."""
    try:
        with path.open("rb") as f:
            return _raw_header(_raw_head(f)[0])[1]
    except (KeyError, ValueError, IndexError, StopIteration):
        return []


def waveform_json(path: Path, names: list[str] | None, max_points: int = 3000, window: tuple | None = None,
                  envelope: bool = False) -> dict:
    """Traces of a raw file for a plot. window (lo, hi; None for an open end): only that stretch of the x axis (time,
    frequency, the swept value), with the traces' values at its ends. envelope: a run longer than max_points keeps
    each stretch's lowest and highest point of every trace, so a narrow spike stays visible (else every k-th point)."""
    raw = read_raw(path)
    vars_ = raw["vars"]
    n = len(raw["data"][vars_[0][0]])
    if raw["plot"].lower().startswith("operating point") or n == 1:
        # no sweep: every vector is a value, the first one is not an x axis
        vars_ = [("point", "")] + list(vars_)
        raw["data"]["point"] = np.zeros(n)
    xname = vars_[0][0]
    x = raw["data"][xname]
    xr = np.real(x)
    names = names or [v[0] for v in vars_[1:7]]
    pts, ends = np.arange(n), []
    if window:
        lo, hi = (xr[0] if window[0] is None else window[0]), (xr[-1] if window[1] is None else window[1])
        if not lo < hi:
            raise ValueError(f"the window's start ({lo:g}) is not before its stop ({hi:g})")
        pts = np.where((xr >= lo) & (xr <= hi))[0]
        if n > 1 and np.all(np.diff(xr) > 0):           # a monotonic axis: the traces are interpolated at the ends
            ends = [v for v in (lo, hi) if xr[0] <= v <= xr[-1]]
        if not pts.size and not ends:
            raise ValueError(f"the run spans {xr[0]:g} .. {xr[-1]:g} ({xname}), nothing of it is in {lo:g} .. {hi:g}")
    if len(pts) <= max_points:
        idx = pts
    elif envelope and not raw["complex"]:
        k = (max_points - 2) // 2
        cut = np.linspace(0, len(pts), k + 1).astype(int)
        keep = {int(pts[0]), int(pts[-1])}
        for nm in (nm for nm in names if nm in raw["data"]):
            y = np.real(raw["data"][nm][pts])
            for a, b in zip(cut[:-1], cut[1:]):
                if b > a:
                    keep |= {int(pts[a + np.argmin(y[a:b])]), int(pts[a + np.argmax(y[a:b])])}
        idx = np.array(sorted(keep))
    else:
        idx = pts[np.unique(np.linspace(0, len(pts) - 1, max_points).astype(int))]

    def at(v: np.ndarray) -> np.ndarray:
        """v at the points shown, and at the window's ends."""
        if not ends:
            return v[idx]
        f = (lambda e: np.interp(e, xr, v.real) + 1j * np.interp(e, xr, v.imag)) if np.iscomplexobj(v) else (lambda e: np.interp(e, xr, v))
        return np.concatenate([[f(ends[0])] if ends[0] == lo else [], v[idx], [f(ends[-1])] if ends[-1] == hi else []])
    xs = at(x)
    out: dict[str, dict] = {}
    for nm in names:
        if nm not in raw["data"]:
            continue
        y = at(raw["data"][nm])
        if raw["complex"]:
            mag = 20 * np.log10(np.maximum(np.abs(y), 1e-300))
            ph = np.degrees(np.unwrap(np.angle(y)))
            out[nm] = {"mag_db": mag.tolist(), "phase_deg": ph.tolist()}
        else:
            out[nm] = {"y": y.tolist()}
    return {
        "plot": raw["plot"], "title": raw["title"], "complex": raw["complex"],
        "x": {"name": xname, "unit": vars_[0][1], "values": (xs.real if raw["complex"] else xs).tolist()},
        "vectors": [{"name": n_, "type": t} for n_, t in vars_[1:]],
        "traces": out, "points": int(n),
    }
