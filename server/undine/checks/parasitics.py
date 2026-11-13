"""Parasitic extraction with Magic: a layout cell as a SPICE subcircuit that a simulation uses in place of its schematic.

The subcircuit (extracted.spice in the job) has the schematic's ports in its order, the devices Magic extracts, the
capacitance of every net to the substrate and between nets (coupling), and with mode rc the resistance of the wiring.
Server.start_sim(extracted=...) netlists a testbench with it in place of the cell's schematic.

The cell is flattened (in KLayout, keeping the top cell's labels only) and extracted by Magic in one piece; mode c is
ext2spice's netlist of that extraction. Flat, because Magic 8.3.465 gets hierarchical layouts wrong in three ways:
extresist writes every transistor of a sub-cell twice, the second time with w=0 l=0; Magic's own `flatten` cuts a
via4 drawn in the parent off the MIM capacitor's contact below it in the sub-cell (the top plate becomes a net of its
own); and the hierarchical extraction leaves out part of the parent's capacitance to the substrate (a met5 strip over
a MIM cell: 0.17 fF instead of 0.77 fF). A sub-cell's labels are dropped: a standard cell's A and Y would name nets,
and a top-level pin would only be an alias of a net named after them.

Mode rc is assembled here, because Magic 8.3.465's resistance flow (ext2sim, extresist, ext2spice extresist on) gets
the capacitance wrong: it keeps each coupling capacitor between the two nets' main nodes and adds it once more to each
net's capacitance to ground, spread over the net's resistor nodes. A 1 mm met1 wire next to another gets 113 fF of
coupling and 164 fF instead of 51 fF to ground (test_parasitics.py checks it on a 200 um pair). The rc netlist has:

* the devices and the resistor network of extresist's netlist. A net keeps one node when its largest resistance is below
  the threshold (`extresist threshold`, res_threshold_ohm: RES_THRESHOLD_OHM by default, with mindelay 0; Magic's own 10
  ohm and 1 ps leave every signal net of an analog cell one node; a Magic before 8.3.653, 8.3.465 here, has no minres
  or mindelay and its threshold means something else: it lumps by its own rules and `extresist tolerance` TOLERANCE,
  and res_options_not_applied says so), when res_nets names other nets (`extresist
  include`, a name in any case), or when extresist cannot start from a port or a source / drain (a well, a substrate
  that is not a port: extresist 8.3.465 crashes on those, so they are left out of it). The substrate is left out of it
  too unless it is a port: it is ground 0, and extresist would rename its nodes away from the name that maps to 0. The
  summary's single_node lists each net left one node and why (extresist's debug output says which it judged negligible).
  A net without a label (a_0_0#) loses its own node in extresist's netlist (`killnode`): its nodes are named after it
  without the '#' (a_0_0.t0, a_0_0.n1), and its capacitance goes to them;
* the capacitances of the c mode, to the substrate and between nets, with the same totals per net and per pair of
  nets. A net's capacitance goes to its resistor nodes in the proportions in which extresist spreads capacitance over
  them, by the area each node stands for (what it spreads is the coupling it reads from ext2sim's file: `ext2sim cthresh
  0` so that every net has some; a net it gives none keeps its capacitance on its main node, or shares it equally
  among its nodes when it has lost its main node). A coupling capacitor between two networks is split in halves, each
  spread over one net's nodes in that net's proportions (as its capacitance to the substrate is: where along the wires
  it sits follows their area, not where the two nets overlap) and placed between each node and the nearest node of the
  other net; next to a net of one node, the network's distribution takes all of it. So that each piece is about the
  threshold or more (and half a coupling capacitor at most SPREAD pieces), a larger network's nodes are first merged
  into fewer ones spread over the network (each the farthest from those already taken, starting from the heaviest),
  each with the share of the nodes nearest it. A piece still smaller than the threshold joins the largest piece of its
  capacitor.

Inner nets keep the schematic's names: with a KLayout LVS run of the same layout, each net without a label that LVS
matched to a schematic net gets that name as a label at one of its contacts before Magic reads the layout (lvs_names);
Magic names a net after its label, where it would otherwise name it after a point on it (a_3109_5296#), which a
testbench's v(x1.g) cannot find. Its devices (X0, X1 ...) take the names of the schematic's elements LVS matched
them to (device_names), so that @m.x1.xxm5.msky130_fd_pr__nfet_g5v0d10v5[l] finds one. When that LVS run does not
match, the summary has a warning (the extraction is of the layout as drawn, for diagnosis).

Magic's planes let no MIM top plate shield the bottom plate from the metal above, so it counts that metal inside the
capacitor (the top plate's strap or contact pad: its area and the fringe of its edges) as coupling between the plates'
nets, which the device has already. A cell with MIM capacitors is extracted a second time, in shielded/, with a tech
file in which each capacitor's plane is between its plates' (shielded_tech): the top plate takes that coupling (none
for its own metal, a coupling to its net for another net's), and its coupling between nets is the extraction's (the
capacitance to the substrate stays the first's); the summary's mim_shielding says per pair of nets what Magic gave and
what is kept (mim_shielding). Without it (the second run failed), only the overlap is taken out (mim_overlap).

Ports are the top cell's labels, in the schematic's order (a label the schematic does not have names an inner net).
Two labels on one net are one port in Magic's netlist: a schematic port whose label sits on the net of a label the
schematic does not have takes that net; two schematic ports on one net are shorted (`ports.shorted`), and the ports do
not match. Capacitors below the threshold are left out before they are spread. The substrate node (VSUBS, or the net
that substrate taps connect to) is the testbench's ground 0 unless it is a port. A net with capacitors only (a victim
wire, dummy metal) gets 1 Tohm to the substrate, or ngspice finds no operating point.
"""
from __future__ import annotations

import difflib
import heapq
import json
import re
import threading
import time
from collections import defaultdict
from pathlib import Path

import klayout.db as kdb
import numpy as np

from undine.checks import magicflow, verify
from undine.pdks.pdk import Pdk
from undine.simulation.sim import Job, Jobs

MODES = ("c", "rc")
# extresist: a net whose largest resistance (from where it starts to any point of it) is below this keeps one node, and
# resistors below it are merged into their neighbours where the network allows (Magic's threshold and minres: its own
# defaults, 10 and 1 ohm, and its 1 ps mindelay, leave the signal nets of an analog cell single nodes: a switch's few
# ohms of wiring next to its tens of ohms of Ron)
RES_THRESHOLD_OHM = 0.1
TOLERANCE = 10      # extresist tolerance, for a Magic without minres / mindelay (before 8.3.653)
SPREAD = 16         # at most this many nodes of a net take a share of one half of a coupling capacitor
LIST = 12           # rows per list in a summary; the rest is counted
CTHRESH_FF = 0.01   # default threshold: smaller capacitors are left out

_TCL = """gds read input.gds
load {cell}
select top cell
port makeall
extract do local
extract do capacitance
extract do coupling
extract {res} resistance
extract all
set f [open units.txt w]
puts $f [magic::i2u 1]
close $f
ext2spice cthresh 0
ext2spice subcircuit top on
ext2spice hierarchy off
ext2spice format ngspice
ext2spice -o c.spice
{rc}quit -noprompt
"""
# the MIM capacitors' second extraction (shielded_tech), in shielded/ with the tech file there: as the first (with
# resistance on, Magic names the substrate after a point of it, not VSUBS), up to ext2spice
_SHIELDED_TCL = """tech load {tech}
cd shielded
gds read ../input.gds
load {cell}
select top cell
port makeall
extract do local
extract do capacitance
extract do coupling
extract {res} resistance
extract all
ext2spice cthresh 0
ext2spice subcircuit top on
ext2spice hierarchy off
ext2spice format ngspice
ext2spice -o c.spice
quit -noprompt
"""
# extresist stops (segfaults) on a net without a port or a source / drain to start from: a well, a substrate that is not
# a port, a net reached only through gates. Those keep one node, and so does a substrate that is not a port (ground 0);
# ignored.txt lists them. With @INCLUDE@ (extresist include) only the nets named get a network: each name (lowercase)
# includes every node of the .ext whose name is it in any case, as Magic looks a name up in its exact case. extresist
# debug says in the log why it writes no network for a net ("Not adding ...": below the threshold or the delay). A Magic
# before minres and mindelay (8.3.653; 8.3.465 has an older threshold) says "Unknown option: minres" and goes on:
# tolerance (which later ones ignore, saying so) keeps it lumping as it did, and single_nodes says res_threshold_ohm was
# not applied.
_RC_TCL = """ext2sim labels on
ext2sim cthresh 0
ext2sim
set keep [dict create]
set ports [dict create]
set nets {}
set sub ""
set f [open @CELL@.ext]
while {[gets $f line] >= 0} {
    if {[regexp {^(node|substrate) "([^"]*)"} $line -> kind name]} {
        lappend nets $name
        if {$kind eq "substrate"} { set sub $name }
    } elseif {[regexp {^port "([^"]*)"} $line -> name]} {
        dict set keep $name 1
        dict set ports $name 1
    } elseif {[string match "device *" $line]} {
        set terms [regexp -all -inline {"[^"]*"} $line]
        foreach t [lrange $terms [expr {[llength $terms] >= 4 ? 2 : 1}] end] { dict set keep [string range $t 1 end-1] 1 }
    }
}
close $f
set f [open ignored.txt w]
foreach name $nets {
    if {![dict exists $keep $name] || ($name eq $sub && ![dict exists $ports $name])} {
        extresist ignore $name
        puts $f $name
    }
}
close $f
set want [list @INCLUDE@]
foreach w $want {
    set found 0
    foreach name $nets {
        if {[string tolower $name] eq $w} { extresist include $name; set found 1 }
    }
    if {!$found} { extresist include $w }
}
extresist tolerance @TOL@
extresist threshold @MOHM@
extresist minres @MOHM@
extresist mindelay 0
extresist debug on
extresist all
ext2spice extresist on
ext2spice -o rc.spice
"""

CONTENTS = {
    "c": "devices; capacitance of each net to the substrate and between nets (coupling), lumped per net",
    "rc": "devices; resistor network of each net extresist resolves (others: one node, single_node says why); "
          "capacitance to the substrate and "
          "between nets with the c mode's totals, spread over the resistor nodes, each piece of a coupling capacitor "
          "to the nearest node of the other net",
}


def start(jobs: Jobs, project_path: Path, pdk: Pdk, layout: kdb.Layout, cell: str, lib: str, mode: str,
          ports: list[str] | None, cthresh_ff: float = CTHRESH_FF, meta: dict | None = None,
          elements: dict[str, str] | None = None, res_threshold_ohm: float = RES_THRESHOLD_OHM,
          res_nets: list[str] | None = None) -> Job:
    """`ports`: the schematic subcircuit's ports in order (None: the cell has no schematic; the layout's labels are);
    `elements`: its instances' elements as a simulation netlist writes them ({XM5: XXM5}), the extracted devices'
    names (lvs_names, device_names). Mode rc: `res_threshold_ohm` (RES_THRESHOLD_OHM), `res_nets` the only nets that
    get a resistor network (None: every net extresist can start from)."""
    if mode not in MODES:
        raise ValueError(f"mode is c (capacitance) or rc (capacitance and resistance), not {mode!r}")
    res = res_options(res_threshold_ohm, res_nets) if mode == "rc" else {}
    exe, rc = magicflow._magic(pdk)
    if layout.cell(cell) is None:
        raise KeyError(f"no layout cell {cell}")
    job = verify._new_job(jobs, project_path, "pex", f"Parasitic extraction ({mode}) {lib}/{cell}",
                          {"cell": cell, "lib": lib, "tool": "magic", "mode": mode, "cthresh_ff": cthresh_ff,
                           **res, "schematic_ports": ports, **(meta or {})})
    with verify._launch_setup(jobs, job):
        names = lvs_names(jobs.runs_dir(project_path), pdk, lib, cell, (meta or {}).get("inputs"), ports, elements)
        export_flat(layout, cell, job.path / "input.gds", names.get("labels"))
        mims = [{"model": m.model, "cap": m.cap, **{k: pdk.profile.layers[getattr(m, k)] for k in ("bottom", "via", "top")},
                 "af": af} for m in pdk.profile.mims if (af := tech_overlap(rc.with_suffix(".tech"), m.planes)) is not None]
        argv = [exe, "-dnull", "-noconsole", "-rcfile", str(rc), "pex.tcl"]
        steps = [(argv, magicflow._env(pdk))]
        tech = rc.with_suffix(".tech")
        shields = [(m.shield[0], m.shield[1], m.planes[1]) for m in pdk.profile.mims if m.shield[1] and
                   (li := layout.find_layer(*m.cap)) is not None and not layout.cell(cell).begin_shapes_rec(li).at_end()]
        if shields and tech.is_file() and (text := shielded_tech(tech.read_text(errors="replace"), shields)):
            (job.path / "shielded").mkdir()
            (job.path / "shielded" / tech.name).write_text(text)
            (job.path / "shielded" / "pex.tcl").write_text(
                _SHIELDED_TCL.format(cell=cell, tech=f"shielded/{tech.name}", res="do" if mode == "rc" else "no"))
            steps.append(([*argv[:-1], "shielded/pex.tcl"], magicflow._env(pdk)))
        rc_tcl = _RC_TCL.replace("@CELL@", cell).replace("@MOHM@", f"{res.get('res_threshold_ohm', 0) * 1000:g}").replace(
            "@INCLUDE@", " ".join(_tcl_word(n.lower()) for n in dict.fromkeys(n.lower() for n in res.get("res_nets") or ()))
        ).replace("@TOL@", str(TOLERANCE)) if mode == "rc" else ""
        (job.path / "pex.tcl").write_text(_TCL.format(cell=cell, res="do" if mode == "rc" else "no", rc=rc_tcl))

        def finish(j: Job) -> dict:
            text, full = assemble(j.path, cell, mode, ports, cthresh_ff, mims, names, res)
            full["runtime_s"] = round(time.time() - (j.status().get("started") or time.time()), 2)
            (j.path / "extracted.spice").write_text(text)
            (j.path / "parasitics.json").write_text(json.dumps(full))
            return {"summary": compact(full)}
        threading.Thread(target=magicflow._run_seq, args=(jobs, job, steps, finish,
                                                          "rc.spice" if mode == "rc" else "c.spice"), daemon=True).start()
    return job


def res_options(threshold_ohm: float | None, nets: list[str] | None) -> dict:
    """Mode rc's options, checked: {res_threshold_ohm, res_nets (when given)}."""
    t = RES_THRESHOLD_OHM if threshold_ohm is None else threshold_ohm
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not t >= 0:
        raise ValueError("res_threshold_ohm is a resistance in ohms, 0 or more")
    if nets is not None and (not isinstance(nets, list) or not nets or
                             any(not isinstance(n, str) or not re.fullmatch(r"[^\s{}\"\\]+", n) for n in nets)):
        raise ValueError("res_nets is a list of net names (the layout's labels, or the schematic's names of inner nets)")
    return {"res_threshold_ohm": float(t), **({"res_nets": list(dict.fromkeys(nets))} if nets else {})}


def _tcl_word(name: str) -> str:
    return "{" + name + "}"


def export_flat(layout: kdb.Layout, cell: str, dest: Path, labels: list | None = None) -> None:
    """The cell flattened, with the top cell's labels only (a sub-cell's labels, such as a standard cell's A and Y,
    would name nets and be taken for pins), and `labels` [[text, [layer, datatype], x, y] in um] added."""
    ly = kdb.Layout()
    ly.dbu = layout.dbu
    c = ly.create_cell(cell)
    c.copy_tree(layout.cell(cell))
    for sub in ly.each_cell():
        if sub.cell_index() != c.cell_index():
            for li in ly.layer_indexes():
                for sh in [x for x in sub.shapes(li).each() if x.is_text()]:
                    sub.shapes(li).erase(sh)
    c.flatten(-1, True)
    for text, (layer, datatype), x, y in labels or ():
        c.shapes(ly.layer(layer, datatype)).insert(kdb.DText(text, x, y))
    opt = kdb.SaveLayoutOptions()
    opt.format = "GDS2"
    opt.select_cell(c.cell_index())
    ly.write(str(dest), opt)


def jobs_of(jobs: Jobs, project_path: Path, lib: str, cell: str) -> list[dict]:
    """Every extraction of lib/cell, newest first (Jobs.list stops at the newest 100 jobs; a sweep makes more): by when
    each was made, as two made in one second have job ids in no order (their random suffixes)."""
    out = []
    for d in sorted(jobs.runs_dir(project_path).iterdir(), reverse=True):
        if (d / "pex.tcl").is_file():
            st = Job(d).status()
            if st.get("kind") == "pex" and st.get("lib") == lib and st.get("cell") == cell:
                out.append(st)
    return sorted(out, key=lambda st: (st.get("created") or 0, st.get("id") or ""), reverse=True)


# ------------------------------------------------------------------ the schematic's names and the MIMs


def lvs_names(runs: Path, pdk: Pdk, lib: str, cell: str, inputs: dict | None, ports: list[str] | None,
              elements: dict[str, str] | None = None) -> dict:
    """The schematic's names for the nets and devices an extraction would name after Magic (a_3109_5296#, X0), from the
    newest finished KLayout LVS run of lib/cell on the same layout (its layout entry's hash): each net of the layout's
    top circuit that has no label and that LVS matched to a net of the schematic other than a port gets a label of
    that name at the centre of one of its contacts or vias, on the label layer of the metal above it
    (Profile.net_labels), where Magic, which names a net after its label, takes it. {"lvs": job, "labels": [[name,
    [layer, datatype], x, y]], "unnamed": [schematic nets matched to an unlabelled net without a contact], "devices":
    [each device LVS matched to an instance of `elements` ({instance: its element as a simulation netlist writes it,
    XXM5}): {name: that element, models: [regex of the models Magic may write for its class], terms: [[terminal, net
    or None]], l: its L in um in the reference or None]} (device_names)}, or {"lvs": None, "why"}."""
    sha = next((e.get("sha") for e in (inputs or {}).get("design", []) if e.get("view") == "layout"), None)
    if not pdk.profile.net_labels or sha is None:
        return {"lvs": None, "why": f"{pdk.name}'s profile names no contacts to label a net at (net_labels)" if sha
                else "the job's inputs have no layout hash"}
    run, match, other = None, False, None
    for d in sorted(runs.iterdir(), reverse=True) if runs.is_dir() else ():
        klayout = (d / "result.lvsdb").is_file()
        if klayout or (other is None and (d / "lvs.out").is_file()):     # lvs.out: Netgen's (Magic's extraction)
            st = Job(d).status()
            if st.get("kind") == "lvs" and st.get("lib") == lib and st.get("cell") == cell and st.get("state") == "done" \
                    and any(e.get("view") == "layout" and e.get("sha") == sha
                            for e in (st.get("inputs") or {}).get("design", [])):
                if klayout and st.get("tool", "klayout") == "klayout":
                    run, match = d, bool((st.get("summary") or {}).get("match"))
                    break
                if other is None and st.get("tool") not in (None, "klayout"):
                    other = (d.name, st.get("tool"), bool((st.get("summary") or {}).get("match")))
    if run is None and other is not None:
        return {"lvs": None, "other_lvs": other[0], "match": other[2],
                "why": f"the LVS run of this layout (job {other[0]}) is {other[1].capitalize()}'s: the schematic's names "
                       "come from a KLayout LVS run only (run_checks with KLayout's LVS, then extract again)"}
    if run is None:
        return {"lvs": None, "why": "no LVS run of this layout: run_checks (LVS), then extract again"}
    lvs = kdb.LayoutVsSchematic()
    lvs.read(str(run / "result.lvsdb"))
    ly = lvs.internal_layout()
    top = next(iter(lvs.netlist().each_circuit_top_down()), None) if lvs.netlist() else None
    if top is None or lvs.xref() is None:
        return {"lvs": run.name, "match": match, "labels": [], "unnamed": [], "empty": True}
    nets, devs = verify.schematic_names(lvs, lvs.xref(), top, verify.names_of(run / "schematic.spice"))
    vias = []                                          # (database layer, label layer) of each contact layer it has
    for via, label in pdk.profile.net_labels:
        want = pdk.profile.layers.get(via)
        li = next((i for i in lvs.layer_indexes() if (ly.get_info(i).layer, ly.get_info(i).datatype) == want), None)
        if li is not None and label in pdk.profile.layers:
            vias.append((li, list(pdk.profile.layers[label])))
    labelled = lambda n: bool(n.name) and not n.name.startswith("$")             # noqa: E731  ($1: KLayout's own)
    taken = {n.name.lower() for n in top.each_net() if labelled(n)} | {p.lower() for p in ports or ()}
    labels, unnamed = [], []
    for n in top.each_net():
        name = nets.get(n.cluster_id)
        if labelled(n) or not name or name.lower() in taken or not re.fullmatch(r"[\w.\[\]<>:/-]+", name):
            continue
        at = None
        for li, label in vias:                         # the lowest contact first
            p = next(lvs.shapes_of_net(n, lvs.layer_by_index(li), True).each(), None)
            if p is not None:
                at = (label, p.bbox().center().to_dtype(ly.dbu))
                break
        if at is None:
            unnamed.append(name)
            continue
        labels.append([name, at[0], round(at[1].x, 4), round(at[1].y, 4)])
        taken.add(name.lower())
    net_name = lambda n: None if n is None else n.name if labelled(n) else nets.get(n.cluster_id)    # noqa: E731
    lengths = _reference_lengths(run)
    devices = []
    for d in top.each_device() if elements else ():
        if (el := elements.get(devs.get(d.id()) or "")) is None:
            continue
        dc = d.device_class()
        devices.append({"name": el, "models": [re.escape(dc.name)] + [e.model for e in pdk.profile.lvs_elements
                                                                      if (e.to or "").lower() == dc.name.lower()],
                        "terms": [[t.name, net_name(d.net_for_terminal(t.id()))] for t in dc.terminal_definitions()],
                        "l": lengths.get(devs[d.id()])})
    return {"lvs": run.name, "match": match, "labels": labels, "unnamed": sorted(unnamed),
            "devices": devices if elements else None}


def _reference_lengths(run: Path) -> dict[str, float]:
    """{the schematic's instance: L in um} of the top-level elements of an LVS run's reference netlist that give a
    numeric L (verify.names_of names them)."""
    text = (run / "schematic.spice").read_text(errors="replace") if (run / "schematic.spice").is_file() else ""
    try:
        inst = json.loads((run / "instances.json").read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for line in text.splitlines():
        t = line.split()
        name = t and (inst.get(t[0]) or inst.get("X" + t[0][1:]) or inst.get("x" + t[0][1:]))
        for kv in t[1:] if name else ():
            k, _, v = kv.partition("=")
            if k.lower() == "l" and (x := _value(v)) is not None:
                out[name] = x
    return out


def tech_overlap(tech: Path, planes: tuple[str, str]) -> float | None:
    """The overlap capacitance (aF/um²) of Magic plane planes[0] over planes[1] in the tech file's first extraction
    style, its default variant, as Magic extracts with it: its `defaultoverlap <types> <plane> <types> <plane> <aF>`
    line; None when it has none."""
    text = tech.read_text(errors="replace") if tech.is_file() else ""
    m = re.search(r"^extract\s*$(.*?)^end\s*$", text, re.M | re.S)
    styles, active = 0, True
    for raw in m.group(1).splitlines() if m else ():
        t = raw.split("#")[0].split()
        if not t:
            continue
        if t[0] == "style":
            styles += 1
            if styles > 1:
                break
        elif t[0] == "variants":
            active = "*" in t[1:] or "" in re.findall(r"\(([^)]*)\)", raw)
        elif active and t[0] == "defaultoverlap" and len(t) == 6 and (t[2], t[4]) == tuple(planes):
            return float(t[5])
    return None


def shielded_tech(text: str, shields: list[tuple[str, str, str]]) -> str | None:
    """A tech file in which each MIM capacitor's top plate shields its bottom plate from the metal above: `shields`
    [(the capacitor's Magic types, their plane, the bottom plate's plane)], as sky130A.tech has them ("*mimcap",
    "cap1", "metal3"); None when its extract section has no planeorder for those planes.

    Why: Magic's default capacitance rules (defaultoverlap / defaultsideoverlap A planeA B planeB aF) take the planes
    that shield A from B from the extract section's planeorder, the planes ordered between the two. sky130A.tech puts
    cap1 (capm) and cap2 (capm2) at the end of it, above every metal ("MiM cap layers have been purposely put out of
    order"), so nothing on them is between metal3 and metal4: the top plate shields nothing, and a met4 strap or
    contact pad over the capacitor reaches the bottom plate below it, by area (defaultoverlap allm4 metal4 allm3
    metal3 84.03 aF/um2) and by the fringe of its edges inside the capacitor (defaultsideoverlap allm4 metal4 allm3
    metal3 70.52 aF/um): of the 191 aF Magic gives between the plates of a 1.17 um unit with its 0.78 um contact pad,
    51 aF by area and 38 aF by fringe inside the capacitor. In silicon that
    is metal over the top plate, of its own net, and the edge of the top plate over the bottom plate is in the model's
    perimeter term. Here each capacitor plane is ordered just above its bottom plate's plane, and every default rule
    between the bottom plate's plane and a plane above the capacitor (the nearest one when two give the same pair, in
    each variant block) gets a copy to the capacitor's types at the same value: what the metal on either side gave the
    metal on the other side through the capacitor, it gives the top plate now (none for the top plate's own metal, a
    coupling to the top plate's net for any other). A rule's shielding changes only where a capacitor is between its
    planes, and a capacitor always sits on its bottom plate; but Magic shares the fringe of an edge out among what
    lies beside it, and the capacitor's new fringe rules take a share of the metal above's fringe that went to the
    substrate (2.8 aF of a 1.17 um unit's 1521 aF bottom plate): the capacitance to the substrate is taken from the
    first extraction (mim_shielding)."""
    m = re.search(r"^extract\s*$(.*?)^end\s*$", text, re.M | re.S)
    if not m:
        return None
    lines = m.group(1).split("\n")
    order: list[tuple[int, str]] = []
    for raw in lines:                                  # the first style's, as Magic extracts with it
        t = raw.split("#")[0].split()
        if t[:1] == ["style"] and order:
            break
        if len(t) == 3 and t[0] == "planeorder" and re.fullmatch(r"\d+", t[2]):
            order.append((int(t[2]), t[1]))
    planes = [p for _, p in sorted(order)]
    caps = {cp: (ct, bp) for ct, cp, bp in shields}
    if not caps or any(cp not in planes or bp not in planes for cp, (_, bp) in caps.items()):
        return None
    new = [p for p in planes if p not in caps]
    for cp, (_, bp) in caps.items():
        new.insert(new.index(bp) + 1, cp)
    o = {p: i for i, p in enumerate(new)}
    best: dict[tuple, tuple[int, int, str]] = {}
    block, first = 0, len(lines)                       # the first style's lines only: Magic extracts with it
    for i, raw in enumerate(lines):
        t = raw.split("#")[0].split()
        if t[:1] == ["style"] and block:
            first = i
            break
        if t[:1] in (["variants"], ["style"]):
            block += 1
        if len(t) != 6 or t[0] not in ("defaultoverlap", "defaultsideoverlap") or t[2] not in o or t[4] not in o:
            continue
        for cp, (ct, bp) in caps.items():
            a, pa, b, pb = t[1:5]
            if t[0] == "defaultoverlap" and pa == bp:  # an overlap rule either way round: the bottom plate's second
                a, pa, b, pb = b, pb, a, pa
            # the metal above onto the bottom plate (overlap, or the fringe of its edges); the fringe of the bottom
            # plate's metal onto metal above: the capacitor in place of the side across it
            if pb == bp and o[pa] > o[cp] or t[0] == "defaultsideoverlap" and pa == bp and o[pb] > o[cp]:
                far = abs(o[pb if pa == bp else pa] - o[cp])
                key = (block, t[0], a, pa, cp)
                if key not in best or far < best[key][0]:
                    best[key] = (far, i, f" {t[0]} {a} {pa} {ct} {cp} {t[5]}")
    after: dict[int, list[str]] = defaultdict(list)
    for _, i, line in best.values():
        after[i].append(line)
    out = []
    for i, raw in enumerate(lines):
        t = raw.split("#")[0].split()
        out.append(f" planeorder {t[1]} {o[t[1]]}" if i < first and len(t) == 3 and t[0] == "planeorder" and t[1] in o
                   else raw)
        out += after.get(i, [])
    return text[:m.start(1)] + "\n".join(out) + text[m.end(1):]


MIM_GRID_UM = 10.0              # mim_overlap's buckets: a capacitor is looked for among those reaching into one


def mim_overlap(d: Path, cell: str, mims: list[dict]) -> dict[tuple[str, str], list]:
    """Magic's overlap capacitance of the metal above over the bottom plate inside each MIM capacitor of the job's
    input (pdkprofile.Mim), which the device has already: {(net, net): [F, um², capacitors, {model: aF/um²}]} between
    the device's two terminals (as the .ext names them). Only the metal above inside a capacitor that touches a via
    inside it counts (the top plate's strap, its net: metal of another net over a capacitor keeps the coupling Magic
    gives it). Each capacitor is the device whose position (the .ext's, in Magic's internal units: units.txt) lies in
    it. The overlaps of all capacitors are found together and each assigned to its capacitor through buckets of
    MIM_GRID_UM, so a CDAC of 2048 units takes well under a second."""
    unit = float((d / "units.txt").read_text().split()[0]) if (d / "units.txt").is_file() else None
    if not mims or unit is None or not (d / f"{cell}.ext").is_file():
        return {}
    devs = [(m, x * unit, y * unit, a, b) for m, x, y, a, b in mim_devices(d / f"{cell}.ext", {m["model"] for m in mims})]
    ly = kdb.Layout()
    ly.read(str(d / "input.gds"))
    top = ly.cell(cell)
    g = max(1, round(MIM_GRID_UM / ly.dbu))

    def reg(ld) -> kdb.Region:
        li = ly.find_layer(*ld)
        return kdb.Region(top.begin_shapes_rec(li)) if li is not None else kdb.Region()
    out: dict[tuple[str, str], list] = {}
    for m in mims:
        caps = reg(m["cap"]).merged()
        # the metal above inside the capacitors (a piece per capacitor) that touches a via inside one, over the bottom plate
        strap = (caps & reg(m["top"])).interacting(caps & reg(m["via"])) & reg(m["bottom"])
        if strap.is_empty():
            continue
        polys, bucket = list(caps.each()), defaultdict(list)
        for i, p in enumerate(polys):
            b = p.bbox()
            for bx in range(b.left // g, b.right // g + 1):
                for by in range(b.bottom // g, b.top // g + 1):
                    bucket[bx, by].append(i)

        def where(pt: kdb.Point) -> int | None:
            return next((i for i in bucket.get((pt.x // g, pt.y // g), ()) if polys[i].inside(pt)), None)
        dev: dict[int, tuple] = {}
        for x in devs:
            if x[0] == m["model"] and (i := where(kdb.Point(round(x[1] / ly.dbu), round(x[2] / ly.dbu)))) is not None:
                dev.setdefault(i, x)
        area: dict[int, int] = defaultdict(int)
        for q in strap.each():
            c = q.bbox().center()
            i = where(c if q.inside(c) else next(q.each_point_hull()))
            if i in dev:
                area[i] += q.area()
        for i, a in area.items():
            um2 = a * ly.dbu ** 2
            acc = out.setdefault(tuple(sorted(dev[i][3:])), [0.0, 0.0, 0, {}])
            acc[0] += um2 * m["af"] * 1e-18
            acc[1] += um2
            acc[2] += 1
            acc[3][m["model"]] = m["af"]
    return out


def mim_devices(ext: Path, models: set[str]) -> list[tuple[str, float, float, str, str]]:
    """The MIM capacitors of a .ext file: (model, x, y in Magic's internal units, the top plate's net, the bottom
    plate's net)."""
    out = []
    for ln in ext.read_text(errors="replace").splitlines() if ext.is_file() else ():
        t = ln.split()
        if len(t) > 6 and t[0] == "device" and t[2] in models:
            nets = re.findall(r'"([^"]*)"', ln)
            if len(nets) >= 3:
                out.append((t[2], float(t[3]) + 0.5, float(t[4]) + 0.5, nets[1], nets[2]))
    return out


def _pair_sums(caps: list[tuple[str, str, float]]) -> dict[tuple[str, str], float]:
    out: dict[tuple[str, str], float] = defaultdict(float)
    for a, b, v in caps:
        if a != b:
            out[(a, b) if a <= b else (b, a)] += v
    return out


def mim_shielding(d: Path, cell: str, c: dict, mims: list[dict]) -> dict:
    """The coupling between nets of the job's second extraction (shielded/, shielded_tech: each MIM's top plate shields
    its bottom plate from the metal above) in place of the first's, and what changed: {"caps": the second's capacitors
    between nets and the first's to the substrate (the capacitor's fringe rules take a share of the fringe of the metal
    above that went to the substrate, which the top plate over its bottom plate does not change), "removed_ff" (the
    coupling taken out: all pairs' changes summed, since what moves keeps its total; overlap_ff of it by area as
    mim_overlap counts it, fringe_ff the rest), "moved_to_plates_ff" (coupling of other nets' pairs that went to a
    pair of plate nets), "pairs": [{nets, magic_ff, kept_ff, removed_ff, overlap_ff, capacitors}] for each pair of
    nets that are a MIM's two plates (removed_ff: the pair's change, net of what moved onto it), "moved": [{nets,
    magic_ff, kept_ff}] for every other pair of nets that changed (metal of another net over a capacitor, coupled to
    the top plate in place of the bottom plate), "why"}. {} when there was no second extraction (no MIM capacitor in
    the cell); {"not_applied": why} when it failed or does not match the first (mim_overlap is taken out then)."""
    if not (d / "shielded" / "pex.tcl").is_file():
        return {}
    if not (d / "shielded" / "c.spice").is_file():
        return {"not_applied": "the second extraction (shielded/) failed: see log.txt; only the overlap of the metal "
                               "above inside each capacitor is taken out (mim_overlap_removed)"}
    s = read_spice(d / "shielded" / "c.spice")
    known = {*c["ports"], *(n for a, b, _ in c["caps"] for n in (a, b)), *(t for x in c["devices"] for t in _terminals(x))}
    if len(s["devices"]) != len(c["devices"]) or any(n not in known for a, b, _ in s["caps"] for n in (a, b)):
        return {"not_applied": "the second extraction (shielded/) does not have the first's devices and nets; only the "
                               "overlap of the metal above inside each capacitor is taken out (mim_overlap_removed)"}
    sub = read_ext(d / f"{cell}.ext")[0] or "VSUBS"
    sub2 = read_ext(d / "shielded" / f"{cell}.ext")[0] or sub
    caps = [x for x in s["caps"] if sub2 not in x[:2]] + [x for x in c["caps"] if sub in x[:2]]
    plates: dict[tuple[str, str], int] = defaultdict(int)
    for _, _, _, a, b in mim_devices(d / f"{cell}.ext", {m["model"] for m in mims}):
        if a != b:
            plates[(a, b) if a <= b else (b, a)] += 1
    over = {k: v[0] for k, v in mim_overlap(d, cell, mims).items()}
    before, after = _pair_sums(c["caps"]), _pair_sums(caps)
    pairs, moved = [], []
    removed = to_plates = overlap = 0.0
    for k in sorted(set(before) | set(after), key=lambda k: -abs(before.get(k, 0.0) - after.get(k, 0.0))):
        b, a = before.get(k, 0.0), after.get(k, 0.0)
        removed += b - a
        if abs(b - a) < 1e-20:                         # 0.01 aF: ext2spice's rounding
            continue
        if k in plates:
            o = min(over.get(k, 0.0), b)
            overlap += o
            pairs.append({"nets": list(k), "magic_ff": _r(b * 1e15), "kept_ff": _r(a * 1e15), "removed_ff": _r((b - a) * 1e15),
                          "overlap_ff": _r(o * 1e15), "capacitors": plates[k]})
        else:
            to_plates += b - a
            moved.append({"nets": list(k), "magic_ff": _r(b * 1e15), "kept_ff": _r(a * 1e15)})
    overlap = min(overlap, max(removed, 0.0))
    return {"caps": caps, "removed_ff": _r(removed * 1e15), "overlap_ff": _r(overlap * 1e15),
            "fringe_ff": _r((removed - overlap) * 1e15), "moved_to_plates_ff": _r(to_plates * 1e15),
            "pairs": pairs, "moved": moved,
            "why": "Magic's tech file orders the MIM capacitor's plane above every metal, so its top plate shields "
                   "nothing: the metal above a capacitor reaches the bottom plate through it, by area and by the fringe "
                   "of its edges inside the capacitor, and Magic counts that as coupling between the plates' nets (the "
                   "device's model has the plates' own capacitance, its edge fringe in the perimeter term). Extracted a "
                   "second time with the capacitor's plane between its plates' and the rules from the metal above to "
                   "the bottom plate also to the capacitor (shielded/): its coupling between nets is the extraction's, "
                   "the capacitance to the substrate the first extraction's. removed_ff: the coupling taken out in "
                   "all (overlap_ff by area of the metal tied to the top plate by a via inside the capacitor, fringe_ff "
                   "the rest: the fringe, and metal of the top plate's net over it without a via there). pairs: between "
                   "a MIM's own plates, coupling outside the capacitors kept; a pair's removed_ff is its change, net of "
                   "what moved onto it (moved_to_plates_ff in all: another bottom plate's metal over a capacitor of the "
                   "same top net). moved: other nets' metal over a capacitor, coupled to its top plate now (its total "
                   "stays)"}


_REF = r"(?<![\w.$@#\[/])([A-Za-z_][\w#!]*(?:\.[\w#!]+)+)"     # x1.g, x2.x1.out.n0 (not @m.x1.xm1[id], 1.8)
_DEV = r"(@[A-Za-z]+\.[A-Za-z_][\w#!]*(?:\.[\w#!]+)+)\["       # @m.x1.xxm5.msky130_fd_pr__nfet_01v8[id]
_FILE_COMMANDS = {"write", "wrdata", "wrs2p", "load", "source", "shell", "echo", "set", "cd"}   # .control: names files


def unknown_nodes(netlist: str, cells: set[str]) -> dict[str, list[str]]:
    """What a simulation netlist refers to inside an instance of one of `cells` (subcircuits simulated as extracted,
    lower case) that the subcircuit does not have, by cell: {cell: ["x1.g", "@m.x1.xxm5.msky130_fd_pr__nfet_01v8",
    ...]}. A reference is a dotted name in a top-level line or a .control block (v(x1.g), x1.out.n0 in .save, .meas, a
    B source, plot) whose path goes from the top level through instances to one of `cells`; what follows must be one
    of its nodes or elements. ngspice takes an unknown one for a new node of its own, floating: the run then fails to
    converge, or measures nothing. A device's quantity (@m.x1.xxm5.msky130_fd_pr__nfet_01v8[l]) must name one of its
    elements after the path (what follows is inside the device's own subcircuit, the PDK's): ngspice says "no such
    device" and the expression is not evaluated. A .control command that names files (write, wrdata, load, source,
    shell, echo, set ...) is not read."""
    subs: dict[str | None, dict] = {None: {"inst": {}, "names": set(), "elems": set()}}    # None: the top level
    cur, lines, refs, control = None, [], [], False
    for raw in netlist.split("\n"):
        if raw.startswith("+") and lines:
            lines[-1] += " " + raw[1:]
        else:
            lines.append(raw)
    for ln in lines:
        t = ln.split()
        if not t or t[0].startswith("*"):
            continue
        low = t[0].lower()
        if low in (".control", ".endc"):
            control = low == ".control"
            continue
        if control:
            if low not in _FILE_COMMANDS:
                refs += re.findall(_REF, ln) + re.findall(_DEV, ln)
            continue
        if low == ".subckt" and len(t) > 1:
            cur = t[1].lower()
            subs[cur] = {"inst": {}, "names": {x.lower() for x in t[2:] if "=" not in x}, "elems": set()}
            continue
        if low == ".ends":
            cur = None
            continue
        sub = subs.setdefault(cur, {"inst": {}, "names": set(), "elems": set()})
        args = [x for x in t[1:] if "=" not in x]
        if low[0] == "x" and args:
            sub["inst"][low] = args[-1].lower()
        if cur is not None:
            sub["names"] |= {low, *(x.lower() for x in args)}
            sub["elems"].add(low)
        elif not re.match(r"\.(lib|include|inc)\b", low):
            refs += re.findall(_REF, ln) + re.findall(_DEV, ln)
    out: dict[str, list[str]] = {}
    for ref in dict.fromkeys(refs):
        dev = ref.startswith("@")
        parts, at = ref.lower().split(".")[1 if dev else 0:], None
        for i, p in enumerate(parts[:-1]):
            at = subs.get(at, {"inst": {}})["inst"].get(p)
            if at is None:
                break
            if at in cells:
                have = subs.get(at, {"names": set(), "elems": set()})
                if (parts[i + 1] not in have["elems"]) if dev else (".".join(parts[i + 1:]) not in have["names"]):
                    out.setdefault(at, []).append(ref)
                break
    return out


# ------------------------------------------------------------------ reading Magic's files

_SI = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "meg": 1e6, "g": 1e9}


def _value(tok: str) -> float | None:
    m = re.fullmatch(r"([-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?)(meg|[fpnumkg])?", tok, re.I)
    return float(m.group(1)) * _SI.get((m.group(2) or "").lower(), 1.0) if m else None


def read_spice(path: Path) -> dict:
    return read_spice_text(path.read_text(errors="replace"))


def read_spice_text(text: str) -> dict:
    """An ext2spice netlist: {name, ports, devices: [line], caps: [(a, b, F)], res: [(a, b, ohm)]}; a C / R line with a
    plain value is a parasitic, one with a model a device. A trailing comment is dropped in both of ngspice's styles:
    Magic 8.3.465 writes a substrate capacitor of a net without a label (`C4 a_0_0# VSUBS 2f $ **FLOATING`) with `$`,
    8.3.678 with `;`, on every resistor node of mode rc too (`C51 vdd.n0 vss 2.19f ; **FLOATING`)."""
    lines: list[str] = []
    for raw in text.splitlines():
        if raw.startswith("+") and lines:
            lines[-1] += " " + raw[1:]
        else:
            lines.append(raw)
    out: dict = {"name": None, "ports": [], "devices": [], "caps": [], "res": []}
    for ln in lines:
        ln = re.sub(r"(?:^|\s+)[;$].*$", "", ln).strip()
        if not ln or ln.startswith("*"):
            continue
        t = ln.split()
        low = t[0].lower()
        if low == ".subckt":
            out["name"], out["ports"] = t[1], t[2:]
        elif low.startswith("."):
            continue
        elif low[0] in "cr" and len(t) == 4 and (v := _value(t[3])) is not None:
            out["caps" if low[0] == "c" else "res"].append((t[1], t[2], v))
        else:
            out["devices"].append(ln)
    return out


def read_ext(path: Path) -> tuple[str | None, dict[str, tuple[float, float]], dict[str, str]]:
    """The substrate node of a flat .ext file, where each node is (Magic's internal units), and the node each label is
    on ({label: node}: two labels on one net are one node, the other name an `equiv` of it)."""
    sub, where, labels, parent = None, {}, [], {}

    def root(n: str) -> str:
        while parent.get(n, n) != n:
            n = parent[n]
        return n
    for ln in path.read_text(errors="replace").splitlines() if path.is_file() else []:
        if (m := re.match(r'(node|substrate)\s+"([^"]+)"\s+\S+\s+\S+\s+(-?\d+)\s+(-?\d+)', ln)):
            where[m.group(2)] = (float(m.group(3)), float(m.group(4)))
            if m.group(1) == "substrate":
                sub = m.group(2)
        elif (m := re.match(r'port\s+"([^"]+)"', ln)):
            labels.append(m.group(1))
        elif (m := re.match(r'equiv\s+"([^"]+)"\s+"([^"]+)"', ln)) and root(m.group(1)) != root(m.group(2)):
            parent[root(m.group(1))] = root(m.group(2))
    groups: dict[str, list[str]] = defaultdict(list)
    for n in {*parent, *labels, *where}:
        groups[root(n)].append(n)
    node = {n: next((x for x in g if x in where), n) for g in groups.values() for n in g}
    return sub, where, {lb: node[lb] for lb in labels}


def read_res_ext(path: Path) -> dict[str, tuple[float, float, float]]:
    """extresist's nodes: name -> (capacitance it gives the node, x, y). A net without a label is renamed: `killnode
    "a_0_0#"`, and its nodes a_0_0.t0, a_0_0.n1 (the '#' or a global's '!' dropped)."""
    out = {}
    for ln in path.read_text(errors="replace").splitlines() if path.is_file() else []:
        m = re.match(r'rnode\s+"([^"]+)"\s+\S+\s+(\S+)\s+(-?\d+)\s+(-?\d+)', ln)
        if m:
            out[m.group(1)] = (float(m.group(2)), float(m.group(3)), float(m.group(4)))
    return out


def _model(line: str) -> str:
    """The model of a device line: the last token before the parameters."""
    t = [x for x in line.split()[1:] if "=" not in x]
    return t[-1] if t else "?"


# ------------------------------------------------------------------ the netlist and its summary


def assemble(d: Path, cell: str, mode: str, ports: list[str] | None, cthresh_ff: float, mims: list[dict] | None = None,
             named: dict | None = None, res_opts: dict | None = None) -> tuple[str, dict]:
    """The subcircuit of the cell from the job's Magic output, and the full summary (compact() shortens it). `mims`:
    the MIM capacitors whose overlap Magic counts twice (mim_overlap, with the tech's aF/um² as "af"); `named`: the
    labels lvs_names added (they name inner nets, and are not the layout's extra labels); `res_opts`: mode rc's
    options (res_options)."""
    c = read_spice(d / "c.spice")
    shielded = mim_shielding(d, cell, c, mims or [])
    if (caps_shielded := shielded.pop("caps", None)) is not None:
        c = {**c, "caps": caps_shielded}
    sub, where, on = read_ext(d / f"{cell}.ext")
    sub = sub or "VSUBS"
    labels = c["ports"]
    low = {p.lower(): p for p in labels}
    moved: dict[str, str] = {}      # a net named after a label the schematic lacks -> the schematic port's label on it
    shorted: list[dict] = []
    added = {x[0].lower() for x in (named or {}).get("labels") or ()}
    if ports is None:                                  # the layout's labels, not those lvs_names added (inner nets)
        header, missing, extra = [lb for lb in labels if lb.lower() not in added], [], []
    else:
        sch = {p.lower() for p in ports}
        label_of = {lb.lower(): (lb, n) for lb, n in on.items()}
        for p in ports:
            if p.lower() in low or p.lower() not in label_of:
                continue
            lb, n = label_of[p.lower()]
            n = moved.get(n, n)
            if n.lower() in sch:
                shorted.append({"port": p, "to": n})
            elif n.lower() in low:
                moved[n] = lb
                del low[n.lower()]
                low[p.lower()] = lb
        header = [low.get(p.lower(), p) for p in ports]
        missing = [p for p in ports if p.lower() not in low and p not in {s["port"] for s in shorted}]
        extra = [lb for lb in dict.fromkeys([*labels, *on]) if lb.lower() not in sch and lb.lower() not in added]
    if moved:
        c = _renamed(c, moved)
        sub, where = moved.get(sub, sub), {moved.get(k, k): v for k, v in where.items()}
    ground = sub if sub.lower() in {h.lower() for h in header} else "0"
    thr = cthresh_ff * 1e-15
    pairs: dict[tuple[str, str], float] = defaultdict(float)
    dropped = [0, 0.0]
    for a, b, v in c["caps"]:
        if a == b:
            continue
        if v < thr:
            dropped[0] += 1
            dropped[1] += v
            continue
        pairs[(a, b) if a <= b else (b, a)] += v
    removed, af = [], {}                            # Magic's second count of a MIM's area between its plates
    for (a, b), (v, area, n, models) in (mim_overlap(d, cell, mims or []) if caps_shielded is None else {}).items():
        k = tuple(sorted((moved.get(a, a), moved.get(b, b))))
        if (take := min(v, pairs.get(k, 0.0))) > 0:
            pairs[k] -= take
            removed.append({"nets": list(k), "c_ff": _r(take * 1e15), "area_um2": _r(area), "capacitors": n})
            af.update(models)
    gcap = {(b if a == sub else a): v for (a, b), v in pairs.items() if sub in (a, b)}
    cpl = {k: v for k, v in pairs.items() if sub not in k}
    if mode == "rc":
        rc = read_spice(d / "rc.spice")
        if len(rc["devices"]) != len(c["devices"]):
            raise RuntimeError(f"extresist's netlist has {len(rc['devices'])} devices, the extraction {len(c['devices'])}")
        # extresist keeps a node per label (a and a2 of one net); a net it leaves as one node has the c mode's name
        names = {n.lower() for n in rc["ports"]} | {t.lower() for x in rc["devices"] for t in _terminals(x)} | \
            {n.lower() for a, b, _ in rc["res"] for n in (a, b)}
        if (back := {old: new for old, new in moved.items() if new.lower() not in names and old.lower() in names}):
            rc = _renamed(rc, back)
        devices = rc["devices"]
        alias = {**{lb: moved.get(n, n) for lb, n in on.items()}, **moved}
        res, caps, nodes, start = _rc_caps(d / f"{cell}.res.ext", where, gcap, cpl, sub, rc["res"], thr, alias)
    else:
        devices, res, caps, nodes, start = c["devices"], c["res"], pairs, {}, {}
    renamed: tuple | None = None
    if (named or {}).get("devices"):
        node_net = {m: n for n, ms in nodes.items() for m in ms}
        ports_low = {h.lower() for h in header}
        renamed = device_names(devices, named["devices"], lambda x: node_net.get(x) or re.sub(r"\.[tn]\d+$", "", x),
                               lambda n: bool(_ANON.fullmatch(n)) or (n == sub and n.lower() not in ports_low))
        devices = [" ".join([renamed[0].get(x.split()[0], x.split()[0]), *x.split()[1:]]) for x in devices]

    per_net: dict[str, dict] = {}
    for n, v in gcap.items():
        per_net.setdefault(n, {"net": n, "ground_ff": 0.0, "coupling_ff": 0.0})["ground_ff"] += v * 1e15
    for (a, b), v in cpl.items():
        for n in (a, b):
            per_net.setdefault(n, {"net": n, "ground_ff": 0.0, "coupling_ff": 0.0})["coupling_ff"] += v * 1e15
    for r in per_net.values():
        r["total_ff"] = r["ground_ff"] + r["coupling_ff"]
    # a net with capacitors only (a victim wire, dummy metal) has no DC path: ngspice finds no operating point
    used = {t for x in devices for t in x.split()[1:]} | {n for a, b, _ in res for n in (a, b)}
    floating = sorted(n for n in per_net if n not in used and not any(m in used for m in nodes.get(n, ()))
                      and n.lower() not in {h.lower() for h in header} and n != sub)

    def nm(n: str) -> str:
        return ground if n == sub else n
    rename = re.compile(rf"(?<=\s){re.escape(sub)}(?=\s|$)")
    lines = [f"* {cell}: extracted from its layout by Magic (mode {mode}): {CONTENTS[mode]}",
             f"* capacitors below {cthresh_ff:g} fF left out; substrate node {sub}" + (" is ground 0" if ground == "0" else "")
             + ("; nets with no DC path have 1 Tohm to it" if floating else ""),
             f".subckt {cell} " + " ".join(header)]
    lines += [rename.sub(ground, x) if ground != sub else x for x in devices]
    lines += [f"R{i} {nm(a)} {nm(b)} {v:.6g}" for i, (a, b, v) in enumerate(res, 1)]
    lines += [f"Rfloat{i} {n} {ground} 1e12" for i, n in enumerate(floating, 1)]
    lines += [f"C{i} {nm(a)} {nm(b)} {v * 1e15:.6g}f" for i, ((a, b), v) in enumerate(sorted(caps.items()), 1) if v > 0]
    lines += [".ends", ""]
    models: dict[str, int] = defaultdict(int)
    for x in devices:
        models[_model(x)] += 1
    full = {"mode": mode, "contents": CONTENTS[mode], "cthresh_ff": cthresh_ff,
            "devices": {"total": len(devices), "by_model": dict(sorted(models.items(), key=lambda t: -t[1]))},
            "nets": len(per_net), "capacitors": sum(1 for v in caps.values() if v > 0), "resistors": len(res),
            "c_total_ff": {"ground": _r(sum(gcap.values()) * 1e15), "coupling": _r(sum(cpl.values()) * 1e15)},
            "c_per_net": [{k: (_r(v) if isinstance(v, float) else v) for k, v in r.items()}
                          for r in sorted(per_net.values(), key=lambda r: -r["total_ff"])],
            "coupling": [{"nets": [a, b], "c_ff": _r(v * 1e15)} for (a, b), v in sorted(cpl.items(), key=lambda t: -t[1])],
            "ports": {"match": not missing and not shorted, "order": header, "missing": missing,
                      **({"shorted": shorted} if shorted else {}), "extra_labels": extra,
                      "from": "layout labels (no schematic)" if ports is None else "schematic"},
            "substrate": sub if ground == sub else f"{sub} (not a port: ground 0 in a simulation)",
            "dropped_below_threshold": {"count": dropped[0], "total_ff": _r(dropped[1] * 1e15)},
            "floating_nets": floating}
    if shielded:
        def named_rows(rows: list[dict]) -> list[dict]:
            return [{**r, "nets": sorted(moved.get(n, n) for n in r["nets"])} for r in rows]
        full["mim_shielding"] = {**shielded, **{k: named_rows(shielded[k]) for k in ("pairs", "moved") if k in shielded}}
    if removed:
        full["mim_overlap_removed"] = {
            "total_ff": _r(sum(r["c_ff"] for r in removed)), "pairs": removed,
            "why": "Magic counts the metal above a MIM's bottom plate inside the capacitor (the top plate's strap) as "
                   "coupling between the plates' nets, which the device's own capacitance has already: taken out, at "
                   "the tech's overlap capacitance (" + ", ".join(f"{k}: {v:g} aF/um2" for k, v in af.items()) + ")"}
    if named is not None:
        if renamed:
            kept: dict = {"devices_named": len(renamed[0]), "devices_kept": renamed[1],
                          **({"devices_split": renamed[2]} if renamed[2] else {})}
        elif ports is None:
            kept = {"devices_kept": "all: the cell has no schematic to name its devices after"}
        elif named.get("empty"):
            kept = {"devices_kept": f"all: the LVS run {named['lvs']} kept no circuit of the layout (it does not match: "
                                    "get_lvs_report says why)"}
        elif named.get("devices") == []:
            kept = {"devices_kept": "all: the LVS run matched no device to an instance of the schematic" + (
                " (it does not match: get_lvs_report says why)" if named.get("match") is False else
                " (one compared with a file, or made before Undine kept its instances: run it again)")}
        else:
            kept = {"devices_kept": "all: the schematic's netlist, which names them, could not be written"}
        full["names"] = ({"from_lvs": named["lvs"], "labelled": [x[0] for x in named.get("labels") or ()],
                          **({"unnamed": named["unnamed"]} if named.get("unnamed") else {}), **kept}
                         if named.get("lvs") else {"from_lvs": None, "why": named.get("why")})
        if ports is not None and (job := named.get("lvs") or named.get("other_lvs")) and named.get("match") is False:
            full["warning"] = (f"LVS of this layout does not match its schematic (job {job}: get_lvs_report "
                               "says why): the extraction is of the layout as drawn, for diagnosis; a simulation on it "
                               "is not of the schematic's circuit")
        elif ports is not None and not named.get("lvs") and named.get("why", "").startswith("no LVS run"):
            full["warning"] = ("no LVS run of this layout: it is not known to match its schematic (run_checks, then "
                               "extract again for the schematic's names)")
    if mode == "rc":
        full["r_per_net"] = _r_per_net(res, {n: m for n, m in nodes.items() if len(m) > 1}, start)
        full["res_threshold_ohm"] = (res_opts or {}).get("res_threshold_ohm", RES_THRESHOLD_OHM)
        if (res_opts or {}).get("res_nets"):
            full["res_nets"] = res_opts["res_nets"]
        full["single_node"] = single_nodes(d, [moved.get(n, n) for n in where], {n for n, m in nodes.items() if len(m) > 1},
                                           sub, sub.lower() in {h.lower() for h in header}, moved, res_opts or {})
        log = (d / "log.txt").read_text(errors="replace") if (d / "log.txt").is_file() else ""
        if (unknown := unsupported_options(log)):
            full["res_options_not_applied"] = {"options": unknown, "why": "this Magic's extresist does not have them "
                                               "(Magic 8.3.653 or later does): it lumps nets by its own threshold and "
                                               f"tolerance ({TOLERANCE}), not by res_threshold_ohm"}
    return "\n".join(lines), full


def single_nodes(d: Path, nets: list[str], resolved: set[str], sub: str, sub_port: bool, moved: dict[str, str],
                 opts: dict) -> list[dict]:
    """The nets of an rc extraction that kept one node (no resistor network), each with why: the substrate (ground
    0), a net extresist cannot start from (ignored.txt), one res_nets leaves out, or what extresist's debug output in
    the job's log says (its largest resistance below the threshold, its delay below mindelay, an error)."""
    ignored = {moved.get(n, n) for n in (d / "ignored.txt").read_text(errors="replace").split()} \
        if (d / "ignored.txt").is_file() else set()
    log = (d / "log.txt").read_text(errors="replace") if (d / "log.txt").is_file() else ""
    said: dict[str, str] = {}
    t = opts.get("res_threshold_ohm", RES_THRESHOLD_OHM)
    unsupported = unsupported_options(log)
    below = (f"res_threshold_ohm ({t:g})" if not unsupported else f"this Magic's own threshold (it has no extresist "
             f"{' or '.join(unsupported)}, so res_threshold_ohm {t:g} was not applied)")
    for m in re.finditer(r"^Not adding\s+(\S+?);?\s+\(maxres = ([\d.eE+-]+)\s*ohm\)", log, re.M):
        said[moved.get(m.group(1), m.group(1))] = (f"its largest resistance, {float(m.group(2)):g} ohm, is below "
                                                   f"{below}: negligible, so lumped")
    for m in re.finditer(r"^Not adding\s+(\S+?);?\s+\(Tnew = ([\d.]+)\s*(f?s|ps)", log, re.M):
        said[moved.get(m.group(1), m.group(1))] = f"its delay, {m.group(2)} {m.group(3)}, is below extresist's mindelay"
    for m in re.finditer(r"^Error in extracting node (\S+)", log, re.M):
        said[moved.get(m.group(1), m.group(1))] = "extresist could not extract it (see the job's log)"
    for m in re.finditer(r"^Note:\s+Substrate node (\S+) not extracted as network", log, re.M):
        said[moved.get(m.group(1), m.group(1))] = "extresist does not extract a substrate as a network"
    only = {n.lower() for n in opts.get("res_nets") or ()}
    out = []
    for n in dict.fromkeys(nets):
        if n in resolved:
            continue
        if n == sub and not sub_port:
            why = "the substrate: ground 0 in a simulation"
        elif n in ignored:
            why = ("no port or source / drain for extresist to start a network from (a well, a net reached only "
                   "through gates): extresist stops on such a net, so it is left out")
        elif only and n.lower() not in only:
            why = "not in res_nets"
        elif unsupported:
            why = said.get(n) or (f"extresist lumped it by this Magic's own threshold and tolerance ({TOLERANCE}): it "
                                  f"has no extresist {' or '.join(unsupported)}, so res_threshold_ohm ({t:g}) was not "
                                  "applied (Magic 8.3.653 or later has them)")
        else:
            why = said.get(n) or ("extresist simplified its network to one node (every resistor below "
                                  f"res_threshold_ohm, {t:g} ohm)")
        out.append({"net": n, "why": why})
    if only:
        known = {n.lower() for n in nets}
        out += [{"net": n, "why": "no net of the layout has this name: res_nets names the layout's labels (or the "
                                  "schematic's names of inner nets LVS matched)"}
                for n in opts["res_nets"] if n.lower() not in known]
    return out


def unsupported_options(log: str) -> list[str]:
    """The extresist options the job's Magic did not know (it says "Unknown option: minres" and goes on)."""
    return sorted({m.group(1) for m in re.finditer(r"^Unknown option:\s*(threshold|minres|mindelay)\b", log, re.M)})


def _r(v: float) -> float:
    return float(f"{v:.4g}")


def _terminals(line: str) -> list[str]:
    """The nodes of a device line: the tokens between its name and its model."""
    t = [x for x in line.split()[1:] if "=" not in x]
    return t[:-1]


_ANON = re.compile(r"[a-z]+_n?\d+_n?\d+#?")       # Magic's name of a net without a label (a_3109_n52#, w_n86_352#
                                                   # of an n-well; a_0_0 in rc)


def device_names(lines: list[str], layout: list[dict], net_of, anon) -> tuple[dict[str, str], list[str], dict]:
    """The schematic's element names for Magic's devices (X0, X1 ...: a testbench's @m.x1.xxm5.msky130_fd_pr__..[l]
    finds none of them), from the devices LVS matched (lvs_names' "devices"). A device of Magic's takes the element of
    the layout device whose model fits (its "models") and whose terminal nets are its own (`net_of`: a terminal node's
    net), source and drain or the plates either way round; a net of Magic's without a name (`anon`) stands for the
    net it stands for in every other device it is on. Several of Magic's devices on one element (its fingers, m) are
    the element, then the element _2, _3 ..; elements alike (the same model, nets and L, dummies) share them out.
    A device that fits elements unalike, or none, keeps Magic's name. ({Magic's name: the element}, [Magic's names
    kept], {element: devices, where more than one})."""
    def order(names: list[str]) -> tuple[list[str], tuple[str, str] | None]:
        if {"S", "G", "D"} <= set(names):              # Magic writes a MOSFET d g s b
            return ["D", "G", "S"] + (["B"] if "B" in names else []), ("D", "S")
        return names, (names[0], names[1]) if names[:2] == ["A", "B"] else None
    ref = []                                           # (record, {terminal: net}, Magic's order, swap, alike)
    for rec in layout:
        terms = dict((t, n) for t, n in rec["terms"])
        names, swap = order([t for t, _ in rec["terms"]])
        same = {"D": "SD", "S": "SD", "A": "AB", "B": "AB"} if swap else {}
        alike = tuple(sorted((same.get(t, t), (n or "").lower()) for t, n in terms.items()))
        ref.append((rec, terms, names, swap, (tuple(rec["models"]), alike, rec.get("l"))))
    devs = []                                          # (index, name, model, terminal nets, l)
    for i, line in enumerate(lines):
        t = line.split()
        kv = {a.lower(): b for a, _, b in (x.partition("=") for x in t if "=" in x)}
        devs.append((i, t[0], _model(line), [net_of(x) for x in _terminals(line)], _value(kv.get("l", ""))))
    bind: dict[str, str] = {}                          # Magic's net without a name -> the layout's (schematic) name
    rev: dict[str, str] = {}
    taken: dict[int, list[int]] = defaultdict(list)    # layout device -> Magic's devices on it

    def fits(dev, k) -> dict | None:
        """The bindings Magic's device `dev` on layout device k makes (a dict, maybe empty); None: it does not fit."""
        _, _, model, nets, _ = dev
        rec, terms, names, swap, _ = ref[k]
        if len(nets) != len(names) or not any(re.fullmatch(rx, model, re.I) for rx in rec["models"]):
            return None
        for v in ([names, [{swap[0]: swap[1], swap[1]: swap[0]}.get(x, x) for x in names]] if swap else [names]):
            new: dict[str, str] = {}
            for mn, tn in zip(nets, v):
                kn = terms.get(tn)
                if not anon(mn):
                    ok = kn is not None and kn.lower() == mn.lower()
                elif kn is None:
                    ok = mn not in bind and mn not in new
                else:                                  # one net for one net, here and in the devices taken before
                    ok = bind.get(mn, new.get(mn, kn)) == kn and rev.get(kn, mn) == mn and \
                        all(m == mn for m, x in new.items() if x == kn)
                    new[mn] = kn
                if not ok:
                    break
            else:
                return new
        return None
    left, moved = list(devs), True
    while left and moved:
        moved, keep = False, []
        for dev in left:
            cands = [(k, b) for k in range(len(ref)) if (b := fits(dev, k)) is not None]
            if dev[4] is not None:                     # the reference's L, where it says one, tells elements apart
                cands = [(k, b) for k, b in cands if (x := ref[k][0].get("l")) is None or
                         abs(x - dev[4]) <= 1e-6 * max(1.0, dev[4])] or cands
            if not cands or len({ref[k][4] for k, _ in cands}) > 1:
                keep.append(dev)
                continue
            k, b = min(cands, key=lambda c: (len(taken[c[0]]), c[0]))
            taken[k].append(dev[0])
            bind.update(b)
            rev.update({v: m for m, v in b.items()})
            moved = True
        left = keep
    out: dict[str, str] = {}
    split = {}
    for k, ix in taken.items():
        el = ref[k][0]["name"]
        for j, i in enumerate(sorted(ix)):
            old = devs[i][1]
            base = el if el[:1].lower() == old[:1].lower() else old[:1] + el
            out[old] = base if j == 0 else f"{base}_{j + 1}"
        if len(ix) > 1:
            split[el] = len(ix)
    kept = [d[1] for d in left]
    clash = {n.lower() for n in kept}
    for old, new in list(out.items()):                # never a name another element has (a parasitic's R1 / C1)
        if new.lower() in clash or re.fullmatch(r"[rc](float)?\d+", new, re.I):
            del out[old]
            kept.append(old)
    return out, sorted(kept, key=lambda n: (len(n), n)), split


def _renamed(net: dict, to: dict[str, str]) -> dict:
    """A read_spice netlist with nodes renamed."""
    def dev(line: str) -> str:
        t = line.split()
        n = len(_terminals(line))
        return " ".join([t[0], *(to.get(x, x) for x in t[1:1 + n]), *t[1 + n:]])
    return {**net, "ports": [to.get(p, p) for p in net["ports"]], "devices": [dev(x) for x in net["devices"]],
            "caps": [(to.get(a, a), to.get(b, b), v) for a, b, v in net["caps"]],
            "res": [(to.get(a, a), to.get(b, b), v) for a, b, v in net["res"]]}


def _rc_caps(res_ext: Path, where: dict, gcap: dict, cpl: dict, sub: str, res: list, thr: float, alias: dict):
    """(resistors, capacitors {(a, b): F}, {net: its resistor nodes}, {net: the node its resistance is counted from})
    of the rc netlist (module docstring). `alias`: {label: net} for the labels that are not the net's name."""
    rnodes = read_res_ext(res_ext)
    known = set(gcap) | {n for k in cpl for n in k} | {sub} | set(where)

    def net_of(node: str) -> str:
        """A node's net: the node itself, the net of its label, or the net extresist named it after (a_0_0.t0 of
        a_0_0#, the '#' dropped)."""
        base = re.sub(r"\.[tn]\d+$", "", node)
        for n in (node, base, base + "#", base + "!"):
            if n in alias or n in known:
                return alias.get(n, n)
        return node
    members: dict[str, list[str]] = defaultdict(list)
    for n in dict.fromkeys([*rnodes, *(x for a, b, _ in res for x in (a, b))]):   # some nodes have no rnode line
        members[net_of(n)].append(n)

    def start(net: str, ms: list[str]) -> str:
        """Its own node, or for a net extresist renamed, the node nearest where Magic places the net."""
        at = [(rnodes[m][1:], m) for m in ms if m in rnodes]
        if net in ms or net not in where or not at:
            return net if net in ms else ms[0]
        x, y = where[net]
        return min(at, key=lambda t: (t[0][0] - x) ** 2 + (t[0][1] - y) ** 2)[1]

    shares: dict[str, tuple[list, list, np.ndarray | None]] = {}

    def share(net: str) -> tuple[list, list, np.ndarray | None]:
        """Where a net's capacitance goes: its nodes, their fractions, and where they are (None: not known)."""
        if net not in shares:
            ms = members.get(net, [])
            got = [n for n in ms if n in rnodes and rnodes[n][0] > 0]
            tot = sum(rnodes[n][0] for n in got)
            if got and tot > 0:
                fr = [rnodes[n][0] / tot for n in got]
            else:                                         # a net extresist renamed has no node of its own name
                got = [net] if net in ms or not ms else ms
                fr = [1.0 / len(got)] * len(got)
            pos = [rnodes[n][1:] if n in rnodes else where.get(n) for n in got]
            shares[net] = (got, fr, None if any(p is None for p in pos) else np.array(pos, dtype=float))
        return shares[net]

    caps: dict[tuple[str, str], float] = defaultdict(float)

    def place(pieces: list[tuple[str, str, float]], v: float) -> None:
        """A capacitor of v farads in pieces; a piece below the threshold joins the capacitor's largest one."""
        big = max(range(len(pieces)), key=lambda i: pieces[i][2])
        moved = sum(f for i, (_, _, f) in enumerate(pieces) if i != big and v * f < thr)
        for i, (a, b, f) in enumerate(pieces):
            f = f + moved if i == big else 0.0 if v * f < thr else f
            if f > 0 and a != b:
                caps[(a, b) if a <= b else (b, a)] += v * f
    memo: dict[tuple[str, int], tuple] = {}

    def coarse(s: tuple, k: int) -> tuple:
        """_coarse, once per net and k (the nets' nodes are distinct: the first names the net)."""
        if (s[0][0], k) not in memo:
            memo[(s[0][0], k)] = _coarse(s, k)
        return memo[(s[0][0], k)]
    for n, v in gcap.items():
        nodes, fracs, _ = coarse(share(n), _fit(v, thr, len(share(n)[0])))
        place([(node, sub, f) for node, f in zip(nodes, fracs)], v)
    for (a, b), v in cpl.items():
        place(_pieces(share(a), share(b), v, thr, coarse), v)
    return res, caps, dict(members), {n: start(n, m) for n, m in members.items()}


def _pieces(sa: tuple, sb: tuple, v: float, thr: float, coarse=None) -> list[tuple[str, str, float]]:
    """Where a coupling capacitor of v farads between nets A and B goes: [(node of A, node of B, fraction)]. A net with
    one node tells nothing about where along the other the coupling is: the other's distribution takes all of it."""
    if len(sa[0]) == 1 and len(sb[0]) == 1:
        return [(sa[0][0], sb[0][0], 1.0)]
    if len(sb[0]) == 1:
        return _spread(sa, sb, v, thr, coarse)
    if len(sa[0]) == 1:
        return [(x, y, f) for y, x, f in _spread(sb, sa, v, thr, coarse)]
    return [(x, y, f / 2) for x, y, f in _spread(sa, sb, v / 2, thr, coarse)] + \
        [(x, y, f / 2) for y, x, f in _spread(sb, sa, v / 2, thr, coarse)]


def _spread(sa: tuple, sb: tuple, v: float, thr: float, coarse=None) -> list[tuple[str, str, float]]:
    """v farads over A's nodes in A's proportions (merged into at most SPREAD, each about the threshold or more), each
    share to the nearest node of B."""
    na, fa, pa = (coarse or _coarse)(sa, _fit(v, thr, SPREAD))
    nb, fb, pb = sb
    if pa is None or pb is None:                       # a node Magic gave no position: B's heaviest node
        near = np.full(len(na), int(np.argmax(fb)))
    else:
        near = _nearest(pa, pb)
    return [(x, nb[j], f) for x, j, f in zip(na, near, fa)]


def _fit(v: float, thr: float, most: int) -> int:
    """How many pieces of the threshold or more v makes, at most `most`."""
    return most if thr <= 0 else min(most, int(v / thr + 1e-6))


def _coarse(s: tuple, k: int) -> tuple[list, list, np.ndarray | None]:
    """A net's distribution (share()) on at most k of its nodes: nodes spread over the network, each the farthest from
    those already taken (from the heaviest), with the fractions of the nodes nearest it. Without positions, the k
    heaviest in proportion."""
    nodes, fr, pos = s
    k = max(1, k)
    if len(nodes) <= k:
        return s
    fr = np.asarray(fr, dtype=float)
    if pos is None:
        keep = np.argsort(-fr, kind="stable")[:k]
        return [nodes[i] for i in keep], list(fr[keep] / fr[keep].sum()), None
    taken = [int(np.argmax(fr))]
    d = np.hypot(*(pos - pos[taken[0]]).T)
    while len(taken) < k and (i := int(np.argmax(d))) not in taken and d[i] > 0:
        taken.append(i)
        d = np.minimum(d, np.hypot(*(pos - pos[i]).T))
    w = np.bincount(_nearest(pos, pos[taken]), weights=fr, minlength=len(taken))
    return [nodes[i] for i in taken], list(w), pos[taken]


def _nearest(pa: np.ndarray, pb: np.ndarray) -> np.ndarray:
    """For each point of pa, the index of the nearest point of pb (in bounded memory for two large networks)."""
    near = np.empty(len(pa), dtype=int)
    step = max(1, 2_000_000 // len(pb))
    for s in range(0, len(pa), step):
        d = np.hypot(pa[s:s + step, None, 0] - pb[None, :, 0], pa[s:s + step, None, 1] - pb[None, :, 1])
        near[s:s + step] = d.argmin(axis=1)
    return near


def _r_per_net(res: list, nodes: dict[str, list[str]], start: dict[str, str]) -> list[dict]:
    """Per net with a resistor network: the sum of its resistors and the largest resistance from its main node (the
    port, or where extresist starts; for a net it renamed, the node nearest where Magic places it) to another of its
    nodes, along the network."""
    of = {n: net for net, ms in nodes.items() for n in ms}
    by: dict[str, list] = defaultdict(list)
    for a, b, v in res:
        if of.get(a) is not None:
            by[of[a]].append((a, b, v))
    out = []
    for net, rs in by.items():
        adj: dict[str, list] = defaultdict(list)
        for a, b, v in rs:
            adj[a].append((b, v))
            adj[b].append((a, v))
        s = start.get(net, net)
        dist, heap = {s: 0.0}, [(0.0, s)]
        while heap:
            dd, n = heapq.heappop(heap)
            if dd > dist.get(n, float("inf")):
                continue
            for m, v in adj[n]:
                if dd + v < dist.get(m, float("inf")):
                    dist[m] = dd + v
                    heapq.heappush(heap, (dd + v, m))
        out.append({"net": net, "r_total_ohm": _r(sum(v for _, _, v in rs)), "r_max_path_ohm": _r(max(dist.values())),
                    "resistors": len(rs), "nodes": len(nodes[net])})
    return sorted(out, key=lambda r: -r["r_total_ohm"])


def nets_of(full: dict) -> list[str]:
    """Every net an extraction's summary names (its ports, capacitance, couplings, resistance, single nodes)."""
    names = [*(full.get("ports") or {}).get("order", []), *(r["net"] for r in full.get("c_per_net") or ()),
             *(n for r in full.get("coupling") or () for n in r["nets"]), *(r["net"] for r in full.get("r_per_net") or ()),
             *(r.get("net") for r in full.get("single_node") or () if isinstance(r, dict)), *(full.get("floating_nets") or ())]
    return list(dict.fromkeys(n for n in names if n))


def compact(full: dict, net: str | None = None) -> dict:
    """The summary an agent reads: the longest lists cut to LIST rows with how many more there are. With `net`, only
    that net's rows (its capacitance, couplings, resistance, single node, a MIM's plates), found in any case: KeyError
    with the nearest names when the extraction has no such net."""
    lists = ("c_per_net", "coupling", "r_per_net", "single_node", "floating_nets")
    if net is not None:
        known = nets_of(full)
        if net not in known:
            same = [n for n in known if n.lower() == net.lower()]
            if len(same) != 1:
                near = same or [*difflib.get_close_matches(net, known, 6, 0.5),
                                *(n for n in known if net.lower() in n.lower())]
                raise KeyError(f"no net {net} in this extraction" + (
                    f" (near: {', '.join(list(dict.fromkeys(near))[:8])})" if near else
                    f" ({len(known)} nets, e.g. {', '.join(known[:8])})"))
            net = same[0]
        out: dict = {"net": net, "mode": full.get("mode"), **({"runtime_s": full["runtime_s"]} if "runtime_s" in full else {})}
        if net in (full.get("ports") or {}).get("order", []):
            out["port"] = True
        for k in lists:
            if (rows := full.get(k)) is None:
                continue
            rows = [r for r in rows if (r == net if isinstance(r, str) else net in (r.get("nets") or [r.get("net")]))]
            if k == "floating_nets":
                out["floating"] = bool(rows)
                continue
            out[k] = rows[:LIST]
            if len(rows) > LIST:
                out[f"{k}_more"] = len(rows) - LIST
        for k in ("mim_shielding", "mim_overlap_removed"):
            if isinstance(m := full.get(k), dict):
                rows = {sub: [r for r in m.get(sub) or () if net in r["nets"]] for sub in ("pairs", "moved")}
                if any(rows.values()):
                    out[k] = {sub: r for sub, r in rows.items() if r}
        return out
    out = {k: v for k, v in full.items() if k not in lists}
    for k in lists:
        rows = full.get(k)
        if rows is None:
            continue
        out[k] = rows[:LIST]
        if len(rows) > LIST:
            out[f"{k}_more"] = len(rows) - LIST
    for k, sub in (("names", "labelled"), ("names", "devices_kept"), ("mim_overlap_removed", "pairs"),
                   ("mim_shielding", "pairs"), ("mim_shielding", "moved")):              # a CDAC's thousands
        if isinstance(rows := (full.get(k) or {}).get(sub), list) and len(rows) > LIST:
            out[k] = {**out[k], sub: rows[:LIST], f"{sub}_more": len(rows) - LIST}
    return out
