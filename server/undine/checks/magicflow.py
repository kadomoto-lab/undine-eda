"""Magic DRC and Magic-extraction + Netgen LVS: the second opinion next to KLayout.

Shuttle prechecks use them (ChipFoundry's cf-precheck extracts with Magic and compares with Netgen; wafer.space runs
Magic DRC before KLayout DRC), so Undine runs the same tools on the same exported GDS with the PDK's own setup files.
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import klayout.db as kdb

from undine.store import provenance
from undine import tools
from undine.checks import verify
from undine.pdks.pdk import Pdk
from undine.simulation.sim import Job, Jobs, stop


def _env(pdk: Pdk) -> dict:
    e = tools.env()
    e.update({"PDK_ROOT": str(pdk.root.parent), "PDK": pdk.name, "PDKPATH": str(pdk.root)})
    return e


def _magic(pdk: Pdk) -> tuple[str, Path]:
    exe = tools.find("magic")
    name = pdk.profile.magic_file(pdk.name)
    rc = pdk.root / "libs.tech" / "magic" / (name or "")
    if not exe:
        raise RuntimeError("magic not found (set UNDINE_TOOLS or put it on PATH)")
    if not name or not rc.is_file():
        raise RuntimeError(f"no Magic setup for {pdk.name}")
    return exe, rc


def _run_seq(jobs: Jobs, job: Job, steps: list[tuple[list[str], dict]], summarize, result: str) -> None:
    """Runs commands one after the other (logs appended); `summarize(job)` makes the summary on success. Each step is
    `jobs`' (step: none starts after a cancel), what the job ends as is recorded unless it was cancelled (record), and
    the job is let go of at the end (verify._ended)."""
    import subprocess

    t0 = time.time()
    try:
        if not jobs.record(job, state="running", started=t0):
            return
        with open(job.path / "log.txt", "ab") as log:
            for argv, env in steps:
                log.write(("$ " + " ".join(argv) + "\n").encode())
                log.flush()
                proc = jobs.step(job, argv, cwd=job.path, env=env, stdout=log, stderr=subprocess.STDOUT)
                if proc is None:
                    return
                try:
                    rc = proc.wait(timeout=3 * 3600)
                except subprocess.TimeoutExpired:
                    stop(proc)                  # not left running once the job is reported failed
                    raise
                if job.cancelled:
                    return
                if rc != 0 and not (job.path / result).is_file():
                    jobs.record(job, state="failed", rc=rc, error=f"{Path(argv[0]).name} exited with {rc}",
                                finished=time.time(), seconds=time.time() - t0)
                    return
        jobs.record(job, state="done", finished=time.time(), seconds=time.time() - t0, **summarize(job),
                    **provenance.outputs(job.path))
    except Exception as e:
        jobs.record(job, state="failed", error=str(e), finished=time.time(),
                    **({"timed_out": True} if isinstance(e, subprocess.TimeoutExpired) else {}))
    finally:
        verify._ended(jobs, job)


# --------------------------------------------------------------------------------------- DRC

# drc check over the cell grown by a halo: Magic checks the top cell's own paint only where it is told to, and keeps no
# error outside the area checked (a wire too narrow at the cell's right edge has its error area beyond the edge)
_DRC_TCL = """gds read input.gds
load {cell}
drc style drc(full)
select top cell
box grow c 10um
drc check
drc catchup
set fh [open result.tsv w]
set r [drc listall why]
foreach {{why boxes}} $r {{
  foreach b $boxes {{
    puts $fh "$why\\t[magic::i2u [lindex $b 0]]\\t[magic::i2u [lindex $b 1]]\\t[magic::i2u [lindex $b 2]]\\t[magic::i2u [lindex $b 3]]"
  }}
}}
close $fh
puts "DRC_TOTAL: [drc list count total]"
quit -noprompt
"""


def start_drc(jobs: Jobs, project_path: Path, pdk: Pdk, layout: kdb.Layout, cell: str, lib: str,
              meta: dict | None = None) -> Job:
    exe, rc = _magic(pdk)
    if layout.cell(cell) is None:  # before the job exists (no "queued" job left behind)
        raise KeyError(f"no layout cell {cell}")
    job = verify._new_job(jobs, project_path, "drc", f"Magic DRC {lib}/{cell}", {"cell": cell, "lib": lib, "tool": "magic",
                                                                              "deck": "magic drc(full)", **(meta or {})})
    with verify._launch_setup(jobs, job):
        verify._export(layout, cell, job.path / "input.gds")
        (job.path / "run.tcl").write_text(_DRC_TCL.format(cell=cell))
        argv = [exe, "-dnull", "-noconsole", "-rcfile", str(rc), "run.tcl"]
        threading.Thread(target=_run_seq, args=(jobs, job, [(argv, _env(pdk))], lambda j: {"summary": drc_summary(j.path / "result.tsv")},
                                                "result.tsv"), daemon=True).start()
    return job


DBU = 0.0005                    # the grid the error tiles are merged on (um): finer than any PDK's


def _rule_id(why: str) -> str:
    m = re.search(r"\(([A-Za-z0-9_./-]+)\)\s*$", why)
    return m.group(1) if m else why[:40]


def _read_tsv(path: Path) -> list[tuple[str, list[float]]]:
    out = []
    for line in path.read_text().splitlines():
        p = line.split("\t")
        if len(p) == 5:
            out.append((p[0], [float(v) for v in p[1:]]))
    return out


def _markers(path: Path) -> dict[str, list[kdb.DPolygon]]:
    """Each rule's violations ({why: [marker]}, in the order Magic lists the rules), a marker for each area of touching
    error tiles: Magic lists a violation as the tiles of its area (a butted tap 24 um long is 138 boxes)."""
    tiles: dict[str, kdb.Region] = {}
    for why, b in _read_tsv(path):
        tiles.setdefault(why, kdb.Region()).insert(kdb.Box(*(round(v / DBU) for v in b)))
    out = {}
    for why, r in tiles.items():
        r.merged_semantics = True
        out[why] = sorted((p.to_dtype(DBU) for p in r.merged(False, 0).each()),
                          key=lambda p: (p.bbox().left, p.bbox().bottom))
    return out


def drc_summary(path: Path) -> dict:
    cats = [{"id": i, "name": _rule_id(why), "description": why, "count": len(ms)}
            for i, (why, ms) in enumerate(_markers(path).items())]
    items = sorted(cats, key=lambda c: (-c["count"], c["name"]))
    return {"total": sum(c["count"] for c in items), "categories": items, "rules": len(items)}


def drc_items(path: Path, category: int | None, limit: int = 500) -> dict:
    out = []
    for cid, (why, ms) in enumerate(_markers(path).items()):
        if category is not None and cid != category:
            continue
        for p in ms:
            b = p.bbox()
            bbox = [round(v, 4) for v in (b.left, b.bottom, b.right, b.top)]
            geom = {"t": "box", "bbox": bbox, "pts": [bbox[0], bbox[1], bbox[2], bbox[1], bbox[2], bbox[3], bbox[0], bbox[3]]} \
                if p.is_box() else {"t": "poly", "bbox": bbox, "pts": [round(c, 4) for pt in p.each_point_hull() for c in (pt.x, pt.y)]}
            out.append({"cat": _rule_id(why), "bbox": bbox, "cell": None, "geoms": [geom]})
            if len(out) >= limit:
                return {"items": out, "truncated": True}
    return {"items": out, "truncated": False}


def drc_report(path: Path, rules: list[str] | None = None, limit: int = 10) -> list[dict]:
    """The rules with violations in a Magic DRC result (result.tsv), as verify.drc_report gives KLayout's."""
    out = []
    for why, ms in _markers(path).items():
        if rules and _rule_id(why) not in rules:
            continue
        out.append({"rule": _rule_id(why), "description": why, "count": len(ms),
                    "markers": [verify.marker(p) for p in ms[:limit]],
                    **({"markers_more": len(ms) - limit} if len(ms) > limit else {})})
    return sorted(out, key=lambda r: (-r["count"], r["rule"]))


# --------------------------------------------------------------------------------------- LVS

_EXT_TCL = """gds read input.gds
load {cell}
select top cell
port makeall
extract do local
extract no capacitance
extract no coupling
extract no resistance
extract no adjust
extract unique
extract all
ext2spice lvs
ext2spice subcircuit top on
ext2spice hierarchy off
ext2spice format ngspice
ext2spice -o extracted.spice
quit -noprompt
"""


def start_lvs(jobs: Jobs, project_path: Path, pdk: Pdk, layout: kdb.Layout, cell: str, lib: str,
              schematic_netlist: str, source_label: str = "", meta: dict | None = None) -> Job:
    exe, rc = _magic(pdk)
    netgen = tools.find("netgen")
    name = pdk.profile.netgen_file(pdk.name)
    setup = pdk.root / "libs.tech" / "netgen" / (name or "")
    if not netgen or not name or not setup.is_file():
        raise RuntimeError(f"no Netgen setup for {pdk.name}")
    if layout.cell(cell) is None:
        raise KeyError(f"no layout cell {cell}")
    job = verify._new_job(jobs, project_path, "lvs", f"Magic+Netgen LVS {lib}/{cell}",
                          {"cell": cell, "lib": lib, "tool": "netgen", "deck": "magic extract + netgen", "schematic": source_label,
                           **(meta or {})})
    with verify._launch_setup(jobs, job):
        verify._export(layout, cell, job.path / "input.gds")
        (job.path / "schematic.spice").write_text(schematic_netlist)
        (job.path / "extract.tcl").write_text(_EXT_TCL.format(cell=cell))
        env = _env(pdk)
        steps = [([exe, "-dnull", "-noconsole", "-rcfile", str(rc), "extract.tcl"], env),
                 ([netgen, "-batch", "lvs", f"extracted.spice {cell}", f"schematic.spice {cell}", str(setup), "lvs.out"], env)]
        threading.Thread(target=_run_seq, args=(jobs, job, steps, lambda j: {"summary": lvs_summary(j.path / "lvs.out", cell)}, "lvs.out"),
                         daemon=True).start()
    return job


def lvs_summary(path: Path, cell: str) -> dict:
    text = path.read_text(errors="replace") if path.is_file() else ""
    final = re.findall(r"Final result:\s*(.*)", text)
    verdict = (final[-1].strip() if final else "").rstrip(".")
    if not verdict:  # netgen prints the verdict on the next line in some versions
        m = re.search(r"Final result:\s*\n?\s*([^\n]+)", text)
        verdict = m.group(1).strip().rstrip(".") if m else "no result"
    ok = "match uniquely" in verdict.lower() and "do not match" not in verdict.lower()
    warn = "property errors" in verdict.lower() or "match with" in verdict.lower()
    counts = re.findall(r"Circuit 1 contains (\d+) devices, Circuit 2 contains (\d+) devices", text)
    nets = re.findall(r"Circuit 1 contains (\d+) nets,\s+Circuit 2 contains (\d+) nets", text)
    return {"match": ok, "verdict": verdict, "warnings": warn,
            "devices": list(map(int, counts[-1])) if counts else None, "nets": list(map(int, nets[-1])) if nets else None,
            "circuits": [{"layout": cell, "schematic": cell, "status": "Match" if ok else ("MatchWithWarning" if warn else "Mismatch"),
                          "nets_ok": 0, "nets_bad": 0}]}
