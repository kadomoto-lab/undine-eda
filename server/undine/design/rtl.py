"""The RTL view of a cell (Verilog / SystemVerilog), Verilator lint and simulation.

A cell's RTL is one file in its library, `<cell>.sv` or `<cell>.v`, holding at least the module of the same name. The
other RTL files of the library are its sources too (submodules, packages), so a testbench cell simulates the design
cells it instantiates.

RtlEditor records each change in the library's operation log (view "rtl"), so RTL has undo / redo, changesets and
review like the other views:

    {"k": "v", "do": "add", "text": "<file text>", "file": "counter.sv"}     # the RTL file was created
    {"k": "r", "do": "mod", "changes": {"text": [before, after]}}            # its text changed

Simulation runs a testbench cell with Verilator (`--binary --timing`): the sources are copied into the job, built,
run, and the job keeps the log, the VCD (`wave.vcd`) and a summary of what the testbench printed ($display), its
errors ($error / $fatal / failed assertions) and where it finished ($finish).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from undine.store import oplog, provenance, trust
from undine import tools
from undine.simulation.sim import Job, Jobs, new_run, stop

SUFFIXES = (".sv", ".v")
TEMPLATE = "module {cell} (\n    input  logic clk,\n    input  logic rst\n);\n\nendmodule\n"
LINT_TIMEOUT = 120
BUILD_TIMEOUT = 900
RUN_TIMEOUT = 600
MAX_DISPLAY = 200


def find_file(lib_dir: Path, cell: str) -> Path | None:
    for suffix in SUFFIXES:
        p = lib_dir / f"{cell}{suffix}"
        if p.is_file():
            return p
    return None


def sources(lib_dir: Path) -> list[Path]:
    """The library's RTL files (every cell's), in a stable order."""
    return sorted(p for p in lib_dir.iterdir() if p.suffix in SUFFIXES and p.is_file()) if lib_dir.is_dir() else []


_COMMENTS = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_DIR = r"(input|output|inout)"


def module_ports(text: str, module: str) -> list[tuple[str, str]]:
    """[(name, direction)] of `module` in Verilog / SystemVerilog source, ANSI or non-ANSI headers."""
    src = _COMMENTS.sub(" ", text)
    m = re.search(rf"\bmodule\s+{re.escape(module)}\b\s*(#\s*\((?:[^()]|\([^()]*\))*\)\s*)?\((.*?)\)\s*;(.*?)\bendmodule\b", src, re.S)
    if not m:
        return []
    header, body = m.group(2), m.group(3)
    out: list[tuple[str, str]] = []
    if re.search(rf"\b{_DIR}\b", header):                      # ANSI: directions in the header
        direction = "inout"
        for part in header.split(","):
            d = re.search(rf"\b{_DIR}\b", part)
            if d:
                direction = d.group(1)
            name = re.findall(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*(?:=[^,]*)?$", part.strip())
            if name:
                out.append((name[-1], direction))
        return out
    names = [n.strip() for n in header.split(",") if n.strip()]
    dirs = {}
    for d, decl in re.findall(rf"\b{_DIR}\b([^;]*);", body):
        for n in re.findall(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)*(?:,|$)", decl.strip()):
            dirs[n] = d
    return [(n, dirs.get(n, "inout")) for n in names if re.fullmatch(r"[A-Za-z_]\w*", n)]


def modules(text: str) -> list[str]:
    return re.findall(r"\bmodule\s+([A-Za-z_]\w*)", _COMMENTS.sub(" ", text))


# ------------------------------------------------------------------ the editor


class RtlEditor:
    """Edits one cell's RTL file and records each change in the library's operation log (view "rtl")."""

    view_kind = "rtl"

    def __init__(self, lib_dir: Path, cell: str, log: oplog.OpLog | None = None):
        self.lib_dir = lib_dir
        self.cell = cell
        self.log = log
        self.lock = threading.RLock()
        self._meta: dict = {"op": "edit", "args": {}, "actor": "user"}

    @property
    def path(self) -> Path:
        return find_file(self.lib_dir, self.cell) or self.lib_dir / f"{self.cell}.sv"

    @contextmanager
    def recording(self, op: str, args: dict, actor: str = "user", reason: str | None = None,
                  changeset: str | None = None):
        with self.lock:
            self._meta = {"op": op, "args": args, "actor": actor, "reason": reason, "changeset": changeset}
            try:
                yield
            finally:
                self._meta = {"op": "edit", "args": {}, "actor": "user"}

    def text(self) -> str:
        p = find_file(self.lib_dir, self.cell)
        return p.read_text(errors="replace") if p else ""

    def _append(self, text: str, effects: list[dict]) -> None:
        if self.log is not None:
            self.log.append({**self._meta, "cell": self.cell, "view": self.view_kind, "sha": oplog.sha(text), "effects": effects})

    def create_file(self, text: str | None = None, suffix: str = ".sv") -> Path:
        with self.lock:
            if self.log is not None:
                self.log.guard()
            if find_file(self.lib_dir, self.cell):
                raise ValueError(f"{self.cell} already has RTL")
            if suffix not in SUFFIXES:
                raise ValueError("RTL files end in .sv or .v")
            text = TEMPLATE.format(cell=self.cell) if text is None else text
            p = self.lib_dir / f"{self.cell}{suffix}"
            self.lib_dir.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            self._append(text, [{"k": "v", "do": "add", "text": text, "file": p.name}])
            return p

    def save(self, text: str) -> bool:
        """Replaces the text (creating the file when there is none); False when nothing changed."""
        with self.lock:
            if self.log is not None:
                self.log.guard()
            p = find_file(self.lib_dir, self.cell)
            if p is None:
                self.create_file(text)
                return True
            before = p.read_text(errors="replace")
            if before == text:
                return False
            p.write_text(text)
            self._append(text, [{"k": "r", "do": "mod", "changes": {"text": [before, text]}}])
            return True

    def _apply(self, effects: list[dict], backwards: bool) -> str:
        p = self.path
        text = self.text()
        for e in (reversed(effects) if backwards else effects):
            if e["k"] == "v":
                if backwards:
                    if text != e["text"]:
                        raise ValueError("conflict: the RTL changed outside this operation")
                    p.unlink(missing_ok=True)
                    text = ""
                else:
                    if find_file(self.lib_dir, self.cell) and text != e["text"]:
                        raise ValueError(f"conflict: {self.cell} got RTL in the meantime")
                    p = self.lib_dir / e.get("file", f"{self.cell}.sv")
                    text = e["text"]
            elif e["k"] == "r":
                old, new = e["changes"]["text"]
                if backwards:
                    old, new = new, old
                if text != old:
                    raise ValueError("conflict: the RTL changed outside this operation")
                text = new
        if text or any(e["k"] == "r" for e in effects):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
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


# ------------------------------------------------------------------ Verilator


_MSG = re.compile(r"^%(Warning|Error)(?:-([A-Z0-9_]+))?:\s*(?:(\S+?):(\d+):(?:(\d+):)?\s*)?(.*)$")


def parse_messages(log: str, lib_dir: Path | None = None) -> list[dict]:
    """Verilator's %Warning / %Error lines: [{level, code, file, line, col, message}]."""
    out = []
    for line in log.splitlines():
        m = _MSG.match(line.strip())
        if not m or m.group(6).startswith("Exiting due to"):
            continue
        level, code, f, ln, col, text = m.groups()
        out.append({"level": level.lower(), "code": code or "", "file": Path(f).name if f else "",
                    "line": int(ln) if ln else None, "col": int(col) if col else None, "message": text.strip()})
    return out


def verilator() -> str:
    exe = tools.find("verilator")
    if not exe:
        raise RuntimeError("Verilator is not installed (pixi install adds it; or put verilator on PATH)")
    return exe


def _extra_args(extra: dict | None, gen_dir: Path) -> tuple[list[str], list[str]]:
    """(Verilator arguments, file names) for gatelevel.rtl_inputs' `extra`: its gate-level netlists written into
    `gen_dir`, the PDK's cell models with their defines, and a configuration that keeps the models' own warnings and
    the netlists' outputs left open (.LO()) out of the lint."""
    if not extra:
        return [], []
    args, names, vlt = [f"-D{d}" for d in extra.get("defines") or []], [], ["`verilator_config"]
    gen_dir.mkdir(parents=True, exist_ok=True)
    for name, text in (extra.get("generated") or {}).items():
        (gen_dir / name).write_text(text)
        vlt.append(f'lint_off -rule PINCONNECTEMPTY -file "*/{name}"')
        names.append(name)
    for m in extra.get("models") or []:
        vlt.append(f'lint_off -file "{m}"')
    if extra.get("models"):                     # the models' `timescale: the RTL's modules need none for them
        vlt.append("lint_off -rule TIMESCALEMOD")
    (gen_dir / "undine.vlt").write_text("\n".join(vlt) + "\n")
    args += [str(gen_dir / "undine.vlt"), *[str(gen_dir / n) for n in names], *[str(m) for m in extra.get("models") or []]]
    return args, names + [Path(m).name for m in extra.get("models") or []]


def lint(lib_dir: Path, top: str, extra: dict | None = None) -> dict:
    """verilator --lint-only of the library's RTL with `top` as the top module; `extra` (gatelevel.rtl_inputs): the
    gate-level netlists of schematic cells and the PDK's cell models it reads too."""
    files = sources(lib_dir)
    if not find_file(lib_dir, top) and f"{top}.v" not in ((extra or {}).get("generated") or {}):
        raise KeyError(f"no RTL for {top}")
    trust.check_rtl(lib_dir, full=False)        # a dependency's `include of a file outside it, unless trusted
    with tempfile.TemporaryDirectory(prefix="undine-lint-") as tmp:
        more, names = _extra_args(extra, Path(tmp))
        cmd = [verilator(), "--lint-only", "-Wall", "-Wno-DECLFILENAME", "-Wno-UNUSEDSIGNAL", "-Wno-fatal",
               "--timing", "--top-module", top, *[str(f) for f in files], *more]
        p = subprocess.run(cmd, cwd=lib_dir, env=tools.env(), capture_output=True, text=True, timeout=LINT_TIMEOUT)
    msgs = parse_messages(p.stdout + p.stderr)
    models = {Path(m).name for m in (extra or {}).get("models") or []}
    # the PDK's models warn of themselves (a module the RTL defines too: MODDUP), which is no finding of the design
    msgs = [m for m in msgs if not (m["file"] in models and m["level"] == "warning")]
    return {"ok": p.returncode == 0 and not any(m["level"] == "error" for m in msgs), "top": top,
            "messages": msgs, "files": [f.name for f in files] + names,
            **({"gate_level": {"from_schematic": sorted(extra.get("cells") or {}),
                               "models": [str(m) for m in extra.get("models") or []],
                               "defines": extra.get("defines") or []}} if extra and (extra.get("generated") or extra.get("models")) else {})}


_ERRORLINE = re.compile(r"^(%Error|%Fatal|Error|Fatal|\[\d+\] %Error)|Assertion failed|\$fatal|\bERROR\b")


def submit_sim(jobs: Jobs, project_path: Path, lib_dir: Path, top: str, title: str, meta: dict | None = None,
               extra: dict | None = None) -> Job:
    """A Verilator simulation job of testbench cell `top` (in a thread). Refused once the server is stopping
    (sim.STOPPING: it would end with the server, Jobs.close). `extra` (gatelevel.rtl_inputs): gate-level netlists of
    schematic cells (copied into src/ with the RTL) and the PDK's cell models (read where they are) with their defines."""
    jobs.ensure_open()
    exe = verilator()
    if not find_file(lib_dir, top):
        raise KeyError(f"no RTL for {top}")
    trust.check_rtl(lib_dir, full=True)         # a dependency's C++, shell or file access, unless trusted: before
                                                # anything is copied
    jid, d = new_run(jobs.runs_dir(project_path))
    (d / "src").mkdir()
    files = []
    for f in sources(lib_dir):                                  # a snapshot: the job keeps what it simulated
        shutil.copy2(f, d / "src" / f.name)
        files.append(f.name)
    more = []
    if extra:
        more, names = _extra_args({**extra, "generated": {}}, d / "src")
        for name, text in (extra.get("generated") or {}).items():
            (d / "src" / name).write_text(text)
            files.append(name)
    job = Job(d)
    job.set(id=jid, title=title, kind="rtl-sim", state="queued", created=time.time(), top=top, files=files, **jobs.here(),
            **({"models": [str(m) for m in extra.get("models") or []], "defines": extra.get("defines") or [],
                "from_schematic": sorted(extra.get("cells") or {})} if extra and (extra.get("models") or extra.get("generated")) else {}),
            **(meta or {}))
    try:
        jobs.track(job)                                         # refused once the server is stopping (sim.STOPPING)
    except RuntimeError:
        shutil.rmtree(d, ignore_errors=True)
        raise
    threading.Thread(target=_run, args=(jobs, job, exe, top, files, more), daemon=True).start()
    return job


def _run(jobs: Jobs, job: Job, exe: str, top: str, files: list[str], more: list[str] = ()) -> None:
    """Builds and runs the testbench, each a step of `jobs` (none starts after a cancel), and records how it ended
    unless it was cancelled (record)."""
    t0 = time.time()
    log = job.path / "log.txt"
    try:
        if not jobs.record(job, state="running", started=t0, phase="build"):
            return
        with log.open("w") as out:
            build = [exe, "--binary", "--timing", "--trace", "-Wno-fatal", "-Wno-lint", "-Wno-style", "-j", "4",
                     "--top-module", top, "-Mdir", "obj", *[f"src/{f}" for f in files], *more]
            out.write("$ " + " ".join(build) + "\n")
            out.flush()
            proc = jobs.step(job, build, cwd=job.path, env=tools.env(), stdout=out, stderr=subprocess.STDOUT)
            if proc is None:
                return
            rc = proc.wait(timeout=BUILD_TIMEOUT)
            if job.cancelled:
                return
            if rc != 0:
                jobs.record(job, state="failed", phase="build", rc=rc,
                            error=_first_error(job.log(400)) or f"build failed (exit {rc})", finished=time.time(),
                            seconds=time.time() - t0, summary=summarize(job.log(4000)))
                return
            if not jobs.record(job, phase="run"):
                return
            out.write(f"\n$ ./obj/V{top}\n")
            out.flush()
            proc = jobs.step(job, [f"./obj/V{top}"], cwd=job.path, env=tools.env(), stdout=out, stderr=subprocess.STDOUT)
            if proc is None:
                return
            rc = proc.wait(timeout=RUN_TIMEOUT)
        if job.cancelled:
            return
        summary = summarize(job.log(20000))
        failed = rc != 0 or summary["errors"]
        jobs.record(job, state="failed" if failed else "done", rc=rc, finished=time.time(), seconds=time.time() - t0,
                    summary=summary, **({"error": summary["errors"][0] if summary["errors"] else f"exit code {rc}"} if failed else {}),
                    **({"vcd": "wave.vcd"} if (job.path / "wave.vcd").is_file() else {}), **provenance.outputs(job.path))
    except subprocess.TimeoutExpired:
        if job.proc:
            stop(job.proc)                      # with the compiler it started: nothing writes on once it has failed
        jobs.record(job, state="failed", error="timed out", timed_out=True, finished=time.time())
    except Exception as e:
        jobs.record(job, state="failed", error=str(e), finished=time.time())
    finally:
        jobs.untrack(job)


def _first_error(log: str) -> str:
    return next((l.strip() for l in log.splitlines() if l.startswith("%Error")), "")


def summarize(log: str) -> dict:
    """What a testbench reported: its printed lines, its errors, where it finished, and Verilator's messages."""
    run = log.split("\n$ ./obj/", 1)[1] if "\n$ ./obj/" in log else ""
    lines = run.splitlines()[1:]
    finish = next((l for l in lines if "$finish" in l or "Verilog $finish" in l), "")
    printed = [l for l in lines if l.strip() and not l.startswith("- ") and "S i m u l a t i o n" not in l
               and not l.startswith("%Warning")][:MAX_DISPLAY]
    errors = [l.strip() for l in lines if _ERRORLINE.search(l)]
    build_msgs = parse_messages(log.split("\n$ ./obj/", 1)[0])
    errors = [m["message"] for m in build_msgs if m["level"] == "error"] + errors
    t = re.search(r"\$finish at ([\d.]+\s*\w+)", run)
    return {"printed": printed, "errors": errors[:20], "finished_at": t.group(1) if t else None, "finish_line": finish.strip(),
            "warnings": [f"{m['code']}: {m['message']}" for m in build_msgs if m["level"] == "warning"][:20]}


# ------------------------------------------------------------------ VCD


_SCALE = {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9, "ps": 1e-12, "fs": 1e-15}


def read_vcd(path: Path, wanted: list[str] | None = None, max_signals: int = 64) -> dict:
    """{timescale_s, signals: [{name, width}], changes: {name: [(time_units, value or None)]}, end}.

    Names are hierarchical (tb.dut.q). A multi-bit value is an integer; x / z bits make it None."""
    text = path.read_text(errors="replace")
    head, _, body = text.partition("$enddefinitions")
    ts = re.search(r"\$timescale\s*([\d.]+)\s*([a-z]+)\s*\$end", head)
    scale = float(ts.group(1)) * _SCALE.get(ts.group(2), 1e-9) if ts else 1e-9
    scope, ids, signals = [], {}, []
    for tok in re.finditer(r"\$scope\s+\w+\s+(\S+)\s*\$end|\$upscope\s*\$end|\$var\s+\w+\s+(\d+)\s+(\S+)\s+(\S+)(?:\s+\[[^\]]*\])?\s*\$end", head):
        if tok.group(1):
            scope.append(tok.group(1))
        elif tok.group(2) is None:
            scope.pop() if scope else None
        else:
            name = ".".join([*scope, tok.group(4)])
            if wanted and name not in wanted:
                continue
            if not wanted and len(signals) >= max_signals:
                continue
            ids.setdefault(tok.group(3), []).append(name)
            signals.append({"name": name, "width": int(tok.group(2))})
    changes: dict[str, list] = {s["name"]: [] for s in signals}
    t = 0
    toks = iter(body.split())
    for tok in toks:
        if tok[0] == "#":
            t = int(tok[1:])
        elif tok[0] in "bBrR":                                 # a vector (or real) value, then the signal's id
            ident = next(toks, "")
            if ident in ids:
                value = tok[1:]
                if tok[0] in "rR":
                    v = float(value)
                else:
                    v = None if re.search(r"[xzXZ]", value) else int(value, 2)
                for n in ids[ident]:
                    changes[n].append((t, v))
        elif tok[0] in "01xzXZ" and tok[1:] in ids:            # a scalar value with its id
            for n in ids[tok[1:]]:
                changes[n].append((t, None if tok[0] in "xzXZ" else int(tok[0])))
    return {"timescale_s": scale, "signals": signals, "changes": changes, "end": t}


def waveform_json(path: Path, names: list[str] | None, max_points: int = 6000) -> dict:
    """The VCD in the shape of sim.waveform_json, drawn as steps: every change is a vertical edge."""
    v = read_vcd(path, names or None)
    sigs = v["signals"] if names else v["signals"][:8]
    times = sorted({t for s in sigs for t, _ in v["changes"][s["name"]]} | {v["end"]})
    xs: list[float] = []
    traces: dict[str, list] = {s["name"]: [] for s in sigs}
    cur = {s["name"]: None for s in sigs}
    pos = {s["name"]: 0 for s in sigs}
    for t in times:
        before = dict(cur)
        for s in sigs:
            ch = v["changes"][s["name"]]
            while pos[s["name"]] < len(ch) and ch[pos[s["name"]]][0] <= t:
                cur[s["name"]] = ch[pos[s["name"]]][1]
                pos[s["name"]] += 1
        if xs:                                                 # the edge: the old value up to t, then the new one
            xs.append(t * v["timescale_s"])
            for n in traces:
                traces[n].append(before[n])
        xs.append(t * v["timescale_s"])
        for n in traces:
            traces[n].append(cur[n])
        if len(xs) > max_points:
            break
    return {"plot": "rtl simulation", "title": path.parent.name, "complex": False,
            "x": {"name": "time", "unit": "s", "values": xs},
            "vectors": [{"name": s["name"], "type": f"{s['width']} bit"} for s in v["signals"]],
            "traces": {n: {"y": ys} for n, ys in traces.items()}, "points": len(xs)}
