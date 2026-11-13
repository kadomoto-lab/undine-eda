"""DRC / LVS runs with the PDK's KLayout decks, plus result databases for the UI.

The decks (Ruby) are executed by the KLayout application in batch mode on a
GDS exported from the live layout. Results are read back with the KLayout
Python module (report database / LVS database) and served as JSON; the
browser never sees the decks.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

import klayout.db as kdb
import klayout.rdb as krdb

from undine.checks import stdcells
from undine.store import provenance
from undine import tools
from undine.pdks.pdk import Pdk
from undine.simulation.sim import Job, Jobs, new_run, stop

def decks(pdk: Pdk | None) -> dict:
    """KLayout decks of the PDK (from its profile) plus whether Magic / Netgen can be used."""
    from undine.pdks import pdkprofile as pp

    out: dict = {"drc": [], "lvs": [], "extra": [], "magic": False, "netgen": False}
    if pdk and pdk.decks_dir:
        p = pdk.profile
        out["drc"] = pp.resolve(pdk.decks_dir / "drc", p.drc_klayout)
        out["lvs"] = pp.resolve(pdk.decks_dir / "lvs", p.lvs_klayout)
        out["extra"] = pp.resolve(pdk.decks_dir / "drc", p.drc_extra)
        out["defaults"] = {d: p.deck_defaults(d) for d in out["drc"] if p.deck_defaults(d)}
    if pdk:
        p = pdk.profile
        rc, setup = p.magic_file(pdk.name), p.netgen_file(pdk.name)
        out["magic"] = bool(rc and (pdk.root / "libs.tech" / "magic" / rc).is_file() and tools.find("magic"))
        out["netgen"] = bool(setup and (pdk.root / "libs.tech" / "netgen" / setup).is_file() and tools.find("netgen"))
    return out


def _run(jobs: Jobs, job: Job, argv: list[str], summarize, result: str) -> None:
    """Runs a check's tool (a Process, `jobs`' step) and records how it ended (record: never over a cancel); then lets
    go of the job (untrack), so that a cancel of what its tool left running stops that (Jobs.cancel)."""
    t0 = time.time()
    try:
        if not jobs.record(job, state="running", started=t0):
            return
        with open(job.path / "log.txt", "wb") as log:
            proc = jobs.step(job, argv, cwd=job.path, env=tools.env(), stdout=log, stderr=subprocess.STDOUT)
            if proc is None:
                return
            try:
                rc = proc.wait(timeout=3 * 3600)
            except subprocess.TimeoutExpired:
                stop(proc)                      # not left running once the job is reported failed
                raise
        if job.cancelled:
            return
        text = job.log(1000)
        # KLayout's LVS exits non-zero on a netlist mismatch but still writes the database
        if rc != 0 and not (job.path / result).is_file():
            err = next((l for l in reversed(text.splitlines()) if re.search(r"error|exception", l, re.I)), f"exit code {rc}")
            jobs.record(job, state="failed", rc=rc, error=err.strip()[:300], finished=time.time(), seconds=time.time() - t0)
        else:
            jobs.record(job, state="done", rc=rc, finished=time.time(), seconds=time.time() - t0, **summarize(job),
                        **provenance.outputs(job.path))
    except Exception as e:
        jobs.record(job, state="failed", error=str(e), finished=time.time(),
                    **({"timed_out": True} if isinstance(e, subprocess.TimeoutExpired) else {}))
    finally:
        _ended(jobs, job)


def _ended(jobs: Jobs, job: Job) -> None:
    with Jobs_lock:
        LIVE.pop(job.id, None)
    jobs.untrack(job)


LIVE: dict[str, Job] = {}
Jobs_lock = threading.Lock()


def _new_job(jobs: Jobs, project_path: Path, kind: str, title: str, meta: dict) -> Job:
    """A check's job, held by `jobs` (track) until its runner ends (_run, magicflow._run_seq: _ended). Refused once the
    server is stopping (sim.STOPPING): it would end with the server (Jobs.close)."""
    jobs.ensure_open()
    jid, d = new_run(jobs.runs_dir(project_path))
    job = Job(d)
    job.set(id=jid, kind=kind, title=title, state="queued", created=time.time(), **jobs.here(), **meta)
    try:
        jobs.track(job)
    except RuntimeError:                         # the server began to stop meanwhile: no job is left behind
        shutil.rmtree(d, ignore_errors=True)
        raise
    with Jobs_lock:
        LIVE[jid] = job
    return job


@contextmanager
def _launch_setup(jobs: Jobs, job: Job):
    """A setup failure keeps its artifacts, but must not leave a queued job without a worker."""
    try:
        yield
    except Exception as e:
        try:
            jobs.record(job, state="failed", error=str(e), finished=time.time())
        finally:
            _ended(jobs, job)
        raise


def _klayout() -> str:
    k = tools.find("klayout")
    if not k:
        raise RuntimeError("klayout not found (set UNDINE_TOOLS or put it on PATH)")
    return k


def start_drc(jobs: Jobs, project_path: Path, pdk: Pdk, layout: kdb.Layout, cell: str, lib: str,
              deck: str | None = None, options: dict | None = None, meta: dict | None = None) -> Job:
    d = decks(pdk)
    deck = deck or (d["drc"] or [None])[0]
    if deck not in d["drc"] + d["extra"]:
        raise ValueError(f"unknown DRC deck {deck}")
    if layout.cell(cell) is None:  # before the job exists: a refused request must not leave a "queued" job behind
        raise KeyError(f"no layout cell {cell}")
    rd = pdk.profile.drc_rd(deck, options or {}, pdk.name)
    exe = _klayout()
    job = _new_job(jobs, project_path, "drc", f"DRC {lib}/{cell}", {"cell": cell, "lib": lib, "deck": deck, **(meta or {})})
    with _launch_setup(jobs, job):
        gds = job.path / "input.gds"
        _export(layout, cell, gds)
        # the top cell under each name the decks read it (sky130: top_cell, GF180 / IHP: topcell)
        argv = [exe, "-b", "-r", str(pdk.decks_dir / "drc" / deck),
                "-rd", f"input={gds}", "-rd", f"top_cell={cell}", "-rd", f"top_cell_name={cell}", "-rd", f"topcell={cell}",
                "-rd", f"report={job.path / 'result.lyrdb'}", "-rd", "thr=2", "-rd", "threads=2"]
        for k, v in rd.items():
            argv += ["-rd", f"{k}={v}"]
        threading.Thread(target=_run, args=(jobs, job, argv, lambda j: {"summary": drc_summary(j.path / "result.lyrdb")}, "result.lyrdb"),
                         daemon=True).start()
    return job


def start_lvs(jobs: Jobs, project_path: Path, pdk: Pdk, layout: kdb.Layout, cell: str, lib: str,
              schematic_netlist: Path | str, deck: str | None = None, options: dict | None = None,
              source_label: str = "", meta: dict | None = None, instances: dict[str, str] | None = None) -> Job:
    """`instances`: the reference's top-level elements as written, {element: the schematic's instance name}, kept as
    instances.json for the names the results give (names_of)."""
    d = decks(pdk)
    deck = deck or (d["lvs"] or [None])[0]
    if deck not in d["lvs"]:
        raise ValueError(f"unknown LVS deck {deck}")
    if layout.cell(cell) is None:
        raise KeyError(f"no layout cell {cell}")
    exe = _klayout()
    job = _new_job(jobs, project_path, "lvs", f"LVS {lib}/{cell}", {"cell": cell, "lib": lib, "deck": deck,
                                                                  "schematic": source_label, **(meta or {})})
    with _launch_setup(jobs, job):
        gds = job.path / "input.gds"
        opts = options or {}
        _export(stdcells.layout_for_lvs(layout, cell, pdk) or layout, cell, gds,
                top_labels_only=pdk.profile.lvs_rd(opts, pdk.name).get("run_mode") == "flat")
        sch = job.path / "schematic.spice"
        if isinstance(schematic_netlist, Path):
            sch.write_text(schematic_netlist.read_text())
        else:
            sch.write_text(schematic_netlist)
        if instances:
            (job.path / "instances.json").write_text(json.dumps(instances))
        (job.path / "pin_labels.json").write_text(json.dumps(pin_label_pairs(pdk.profile)))
        argv = [exe, "-b", "-r", str(pdk.decks_dir / "lvs" / deck),
                "-rd", f"input={gds}", "-rd", f"top_cell={cell}", "-rd", f"topcell={cell}",
                "-rd", f"report={job.path / 'result.lvsdb'}",
                "-rd", f"schematic={sch}", "-rd", f"target_netlist={job.path / 'extracted.cir'}", "-rd", "thr=2"]
        for k, v in pdk.profile.lvs_rd(opts, pdk.name).items():
            argv += ["-rd", f"{k}={v}"]
        taps = tap_layers(pdk.profile)
        threading.Thread(target=_run, args=(jobs, job, argv, lambda j: {"summary": lvs_summary(j.path / "result.lvsdb"),
                                                                  **({"erc": erc(j.path, taps)} if taps else {})},
                                            "result.lvsdb"), daemon=True).start()
    return job


def tap_layers(profile) -> list[list[tuple[tuple[int, int], bool]]]:
    """Profile.erc_tap by layer number: per term [((layer, datatype), drawn on it or not)]; [] without an ERC, or with
    a layer the profile does not number."""
    names = [n.lstrip("-") for term in profile.erc_tap for n in term]
    if not names or any(n not in profile.layers for n in names):
        return []
    return [[(profile.layers[n.lstrip("-")], not n.startswith("-")) for n in term] for term in profile.erc_tap]


def erc(d: Path, taps: list) -> dict:
    """The ERC of an LVS run in directory `d` (butted_taps; `taps`: tap_layers), or why it could not be made
    ({"error"})."""
    try:
        return butted_taps(d / "result.lvsdb", d / "input.gds", taps)
    except Exception as e:                        # a database the check cannot read must not fail the LVS run
        return {"error": f"{type(e).__name__}: {e}"}


def _export(layout: kdb.Layout, cell: str, dest: Path, top_labels_only: bool = False) -> None:
    c = layout.cell(cell)
    if c is None:
        raise KeyError(f"no layout cell {cell}")
    if top_labels_only:
        # a flat comparison names nets after the top cell's labels: pin labels inside sub-cells (inv's VSS) would give a net
        # two names (VGND|VSS) and keep it from joining the substrate net of the same name
        layout = layout.dup()
        c = layout.cell(cell)
        for sub in layout.each_cell():
            if sub.cell_index() != c.cell_index():
                for li in layout.layer_indexes():
                    for sh in [x for x in sub.shapes(li).each() if x.is_text()]:
                        sub.shapes(li).erase(sh)
    opt = kdb.SaveLayoutOptions()
    opt.format = "GDS2"
    opt.select_cell(c.cell_index())
    opt.deep = False
    layout.write(str(dest), opt)


# --------------------------------------------------------------------- DRC


def _load_rdb(path: Path) -> krdb.ReportDatabase:
    db = krdb.ReportDatabase("drc")
    db.load(str(path))
    return db


def drc_summary(path: Path) -> dict:
    db = _load_rdb(path)
    cats = []
    total = 0
    for c in db.each_category():
        n = c.num_items()
        total += n
        cats.append({"id": c.rdb_id(), "name": c.name(), "description": c.description, "count": n})
    cats.sort(key=lambda c: (-c["count"], c["name"]))
    return {"total": total, "categories": [c for c in cats if c["count"]], "rules": len(cats)}


def _to_top(db: krdb.ReportDatabase, cell_id: int, cache: dict) -> kdb.DCplxTrans:
    """The transformation from a cell of a DRC result to its top cell: a deck in deep mode reports a violation inside
    a sub-cell in that cell's coordinates, with the cell's placement in its parent as a reference (one placement, the
    first KLayout found: a violation of a cell placed twice is reported once)."""
    if cell_id not in cache:
        cache[cell_id] = kdb.DCplxTrans()                  # a cycle in a broken database ends here
        c = db.cell_by_id(cell_id) if cell_id else None
        ref = next(iter(c.each_reference()), None) if c is not None and c.name() != db.top_cell_name else None
        if ref is not None:
            cache[cell_id] = _to_top(db, ref.parent_cell_id, cache) * ref.trans
    return cache[cell_id]


def _values(db: krdb.ReportDatabase, item, cache: dict) -> list:
    """An item's geometric values in the top cell's coordinates (um): DBox, DPolygon, DEdgePair or DEdge (a box
    rotated by its cell's placement is a polygon)."""
    t = _to_top(db, item.cell_id(), cache)
    out = []
    for v in item.each_value():
        g = v.box() if v.is_box() else v.polygon() if v.is_polygon() else v.edge_pair() if v.is_edge_pair() \
            else v.edge() if v.is_edge() else None
        if g is not None:
            out.append(g if t.is_unity() else (kdb.DPolygon(g) if isinstance(g, kdb.DBox) else g).transformed(t))
    return out


def _value_geom(v) -> dict | None:
    """A marker for the Verify panel (um): {t: box | poly | edge | edges, bbox, pts}."""
    if isinstance(v, kdb.DPolygon) and v.is_box():
        v = v.bbox()
    if isinstance(v, kdb.DBox):
        return {"t": "box", "bbox": [v.left, v.bottom, v.right, v.top],
                "pts": [v.left, v.bottom, v.right, v.bottom, v.right, v.top, v.left, v.top]}
    if isinstance(v, kdb.DPolygon):
        b = v.bbox()
        return {"t": "poly", "bbox": [b.left, b.bottom, b.right, b.top],
                "pts": [c for pt in v.each_point_hull() for c in (pt.x, pt.y)]}
    if isinstance(v, kdb.DEdge):
        return {"t": "edge", "bbox": [min(v.x1, v.x2), min(v.y1, v.y2), max(v.x1, v.x2), max(v.y1, v.y2)],
                "pts": [v.x1, v.y1, v.x2, v.y2]}
    if isinstance(v, kdb.DEdgePair):
        pts = [v.first.x1, v.first.y1, v.first.x2, v.first.y2, v.second.x1, v.second.y1, v.second.x2, v.second.y2]
        xs, ys = pts[0::2], pts[1::2]
        return {"t": "edges", "bbox": [min(xs), min(ys), max(xs), max(ys)], "pts": pts}
    return None


def drc_items(path: Path, category: int | None, limit: int = 500) -> dict:
    """The markers of a KLayout DRC result for the Verify panel, in the top cell's coordinates (um)."""
    db = _load_rdb(path)
    items, cache = [], {}
    it = db.each_item() if category is None else db.each_item_per_category(category)
    for item in it:
        geoms = [g for g in (_value_geom(v) for v in _values(db, item, cache)) if g]
        if not geoms:
            continue
        bb = [min(g["bbox"][0] for g in geoms), min(g["bbox"][1] for g in geoms),
              max(g["bbox"][2] for g in geoms), max(g["bbox"][3] for g in geoms)]
        cat = db.category_by_id(item.category_id())
        items.append({"cat": cat.name() if cat else "", "bbox": bb, "geoms": geoms,
                      "cell": item.cell_id() and db.cell_by_id(item.cell_id()).name()})
        if len(items) >= limit:
            break
    return {"items": items, "truncated": len(items) >= limit}


def _um(v: float) -> float:
    return round(v, 4)


def marker(v) -> dict | None:
    """A DRC marker in um, as an agent reads it: {box}, {edges: [edge, edge]} (an edge pair: the two edges a spacing or
    width rule measured), {edge}, or {polygon} (its points; a box-shaped one is a box). An edge is [x1, y1, x2, y2]."""
    edge = lambda e: [_um(e.x1), _um(e.y1), _um(e.x2), _um(e.y2)]       # noqa: E731
    if isinstance(v, kdb.DBox):
        return {"box": [_um(v.left), _um(v.bottom), _um(v.right), _um(v.top)]}
    if isinstance(v, kdb.DPolygon):
        return marker(v.bbox()) if v.is_box() else {"polygon": [[_um(p.x), _um(p.y)] for p in v.each_point_hull()]}
    if isinstance(v, kdb.DEdgePair):
        return {"edges": [edge(v.first), edge(v.second)]}
    if isinstance(v, kdb.DEdge):
        return {"edge": edge(v)}
    return None


def drc_report(path: Path, rules: list[str] | None = None, limit: int = 10) -> list[dict]:
    """The rules with violations in a KLayout DRC result (result.lyrdb), most violations first: {rule, description,
    count, markers (at most `limit`, marker(); in the top cell's coordinates, _to_top), markers_more}; `rules`: only
    those (names)."""
    db = _load_rdb(path)
    out, cache = [], {}
    for c in db.each_category():
        n = c.num_items()
        if not n or (rules and c.name() not in rules):
            continue
        ms = []
        for item in db.each_item_per_category(c.rdb_id()):
            ms += [marker(g) for g in _values(db, item, cache)][:limit - len(ms)]
            if len(ms) >= limit:
                break
        out.append({"rule": c.name(), "description": c.description, "count": n, "markers": ms,
                    **({"markers_more": n - len(ms)} if n > len(ms) else {})})
    return sorted(out, key=lambda r: (-r["count"], r["rule"]))


def latest_drc(jobs: Jobs, project_path: Path, lib: str, cell: str) -> list[dict]:
    """The newest finished DRC run of lib/cell with each checker and deck (KLayout's, Magic's), newest first, among
    all of the project's runs (Jobs._scan, as Jobs.latest: Jobs.list stops at the newest 100, and a sweep makes more)."""
    out: dict[tuple, Path] = {}
    for d, st in jobs._scan(project_path):
        if st.get("kind") == "drc" and st.get("lib") == lib and st.get("cell") == cell and st.get("state") == "done" \
                and ((d / "result.lyrdb").is_file() or (d / "result.tsv").is_file()):
            out.setdefault((st.get("tool") or "klayout", st.get("deck")), d)
    return [Job(d).status() for d in out.values()]


# --------------------------------------------------------------------- LVS

def _g(o, attr: str):
    """Attribute or zero-arg method (the KLayout API differs between versions)."""
    v = getattr(o, attr)
    return v() if callable(v) else v


def _st(pair) -> str:
    """Status name of a cross-reference pair (CircuitPairData, NetPairData ...)."""
    v = _g(pair, "status")
    X = kdb.NetlistCrossReference
    for n in ("Match", "NoMatch", "Skipped", "MatchWithWarning", "Mismatch"):
        if v == getattr(X, n) or (hasattr(X, "Status") and v == getattr(X.Status, n, object())):
            return n
    return "None"


def lvs_summary(path: Path) -> dict:
    lvs = kdb.LayoutVsSchematic()
    lvs.read(str(path))
    xref = lvs.xref()
    circuits = []
    ok = True
    if xref is None:
        return {"match": False, "circuits": [], "error": "no cross reference in the LVS database"}
    for cp in xref.each_circuit_pair():
        a, b = _g(cp, 'first'), _g(cp, 'second')
        st = _st(cp)
        nets = {"Match": 0, "bad": 0}
        for npair in xref.each_net_pair(cp):
            nets["Match" if _st(npair) == "Match" else "bad"] += 1
        circuits.append({"layout": _g(a, "name") if a else None, "schematic": _g(b, "name") if b else None,
                         "status": st,
                         "nets_ok": nets["Match"], "nets_bad": nets["bad"]})
        if st != "Match":
            ok = False
    unused: list[dict] = []
    pin_errors = _top_pin_errors(lvs, xref, path.with_name("input.gds"), unused)
    out = {"match": ok and not pin_errors, "circuits": circuits}
    if pin_errors:
        out["pin_errors"] = pin_errors
    if unused:
        out["unused_pins"] = unused
    return out


def pin_label_pairs(profile) -> list[list[list[int]]]:
    """The profile's pin label layers with the metal each names a net on: [[label (layer, datatype), metal (layer,
    datatype)]] (met1.label on met1), kept by an LVS run as pin_labels.json for _top_labels."""
    out = []
    for lab in profile.pin_label_layers:
        ld, md = profile.layers.get(lab), profile.layers.get(lab.removesuffix(".label"))
        if ld and md:
            out.append([list(ld), list(md)])
    return out


def _top_labels(gds: Path, cell: str) -> set[str]:
    """The pin labels of a layout's cell itself (not those of the cells it places), upper-cased like SPICE: the texts
    on a pin label layer of the run (its pin_labels.json, pin_label_pairs) that fall on a shape of their metal (of the
    cell or below). A text on another layer, or off its metal, names no net; a run without the file has none."""
    pairs_f = gds.with_name("pin_labels.json")
    if not gds.is_file() or not pairs_f.is_file():
        return set()
    ly = kdb.Layout()
    ly.read(str(gds))
    c = ly.cell(cell)
    if c is None:
        return set()
    out: set[str] = set()
    for (ll, ld), (ml, md) in json.loads(pairs_f.read_text()):
        li, mi = ly.find_layer(ll, ld), ly.find_layer(ml, md)
        if li is None or mi is None:
            continue
        metal = None
        for s in c.shapes(li).each(kdb.Shapes.STexts):
            if metal is None:
                metal = kdb.Region(c.begin_shapes_rec(mi))
                metal.merge()
            p = s.text.trans.disp
            if not metal.interacting(kdb.Region(kdb.Box(p.x - 1, p.y - 1, p.x + 1, p.y + 1))).is_empty():
                out.add(s.text_string.upper())
    return out


def _live_named(circuit, name: str):
    """A net of a layout circuit that carries `name` among its names (a net with two labels is named "c1,vdd") and has
    a device or a subcircuit on it; None if there is none."""
    want = name.upper()
    for n in circuit.each_net():
        if n.terminal_count() + n.subcircuit_pin_count() and want in (x.strip().upper() for x in (n.name or "").split(",")):
            return n
    return None


def _on_net(net) -> str:
    """The devices and subcircuits on a layout net, for a message."""
    items = sorted({t.device().expanded_name() for t in net.each_terminal()}
                   | {sp.subcircuit().expanded_name() for sp in net.each_subcircuit_pin()})
    return ", ".join(items[:6]) + (f" and {len(items) - 6} more" if len(items) > 6 else "") if items else "none"


def _top_pin_errors(lvs, xref, gds: Path | None = None, unused: list | None = None) -> list[str]:
    """Pins of the top cell that the layout does not carry. KLayout reports a schematic pin without a layout pin as
    matching and pairs its net with whatever unnamed net has the same devices, so a supply that lost its connection
    to the labeled stripe (demo, 10/25: VDPWR, a via stack missing its top via) still "matched". Every pin of the
    schematic's top circuit has to be a pin of the layout's top circuit, on the net that matched it.

    A port the schematic connects to nothing inside the cell (an interface pin it does not use: sar_cdac's vdd) is the
    exception: the deck's netlist simplification purges the layout's net of it too (a labelled piece of metal with no
    device on it), so the layout cannot carry it as a pin. It is satisfied by its label in the layout's top cell (`gds`,
    the run's input; on a pin label layer, on its metal: _top_labels) and reported in `unused` ({pin, label}); without
    the label it is an error that says so. Only a net with that one pin and no device or subcircuit counts, and only
    while no net of the extracted layout carries the name: a label on live metal (a device's net) is a misconnection
    the exemption would hide, and is an error naming the net."""
    top = next(iter(lvs.netlist().each_circuit_top_down()), None) if lvs.netlist() else None
    if top is None:
        return []
    cp = next((cp for cp in xref.each_circuit_pair() if (a := _g(cp, "first")) is not None and _g(a, "name") == top.name), None)
    if cp is None:
        return []
    ref_circuit = _g(cp, "second")
    matched = {}                                   # schematic net name -> layout net name, upper-cased like SPICE
    for np in xref.each_net_pair(cp):
        a, b = _g(np, "first"), _g(np, "second")
        if a is not None and b is not None:
            matched[_g(b, "name").upper()] = _g(a, "name")
    errors = []
    labels: set[str] | None = None
    for pp in xref.each_pin_pair(cp):
        a, b = _g(pp, "first"), _g(pp, "second")
        if b is None:
            continue                                   # a layout pin the schematic does not have: not an open
        want = _g(b, "name")
        if a is None:
            net = ref_circuit.net_for_pin(b) if ref_circuit is not None else None
            if net is None or (net.terminal_count() + net.subcircuit_pin_count() == 0 and net.pin_count() == 1):
                live = _live_named(top, want)
                if live is not None:
                    # the label is on metal the layout uses: not an unused port, a misconnection
                    errors.append(f"pin {want}: connected to nothing in the schematic but its label is on the layout "
                                  f"net {live.name} (devices {_on_net(live)})")
                    continue
                if labels is None:
                    labels = _top_labels(gds, top.name) if gds is not None else set()
                if want.upper() in labels:
                    if unused is not None:
                        unused.append({"pin": want, "label": True,
                                       "note": "connected to nothing inside the schematic; its label is on the layout "
                                               "(the deck purges a net with no device, so the layout has no pin for it)"})
                    continue
                errors.append(f"pin {want}: connected to nothing inside the schematic and no {want} label in the "
                              f"layout's top cell: label the port where it leaves the cell")
                continue
            net = matched.get(want.upper())
            errors.append(f"pin {want}: no {want} label on the layout net it matched"
                          + (f" ({net}): not connected to the {want} pin in the layout?" if net else ""))
        elif _g(a, "name").upper() != want.upper():
            errors.append(f"pin {want}: matched the layout pin {_g(a, 'name')}")
    return errors


def names_of(sch: Path) -> tuple[dict[str, str], dict[str, str]]:
    """The names of a reference netlist (an LVS run's schematic.spice) as written: ({net, upper case: as written},
    {device as KLayout names it, upper case: the schematic's instance}). KLayout's SPICE reader upper-cases them and
    drops an element's letter (MXMS: XMS, M1: 1); the sky130 deck's reader makes a call of a device model the element
    its name begins with (XR1: R, named 1). The instance is the run's instances.json's for the element (or for the
    call lvs_elements made it: CC1 of XC1), else the element's name without its letter."""
    text = sch.read_text(errors="replace") if sch.is_file() else ""
    try:
        inst = json.loads(sch.with_name("instances.json").read_text())
    except (OSError, ValueError):
        inst = {}
    devs: dict[str, str] = {}
    for line in text.splitlines():
        t = line.split()
        if t and t[0][:1].isalpha() and len(t[0]) > 1:
            name = inst.get(t[0]) or inst.get("X" + t[0][1:]) or inst.get("x" + t[0][1:]) or t[0][1:]
            devs.setdefault(t[0][1:].upper(), name)
            if t[0][:1] in "xX" and len(t[0]) > 2:
                devs.setdefault(t[0][2:].upper(), name)
    return {t.upper(): t for t in re.findall(r"[^\s=()]+", text)}, devs


def schematic_names(lvs, xref, top, written: tuple[dict, dict]) -> tuple[dict, dict]:
    """({layout net's cluster id: schematic net name}, {layout device's id: schematic instance name}) of the top
    circuit's pairs that LVS matched (Match or MatchWithWarning), as the reference netlist writes them (names_of)."""
    cp = next((cp for cp in xref.each_circuit_pair() if (a := _g(cp, "first")) is not None and _g(a, "name") == top.name), None)
    nets, devs = {}, {}
    if cp is None:
        return nets, devs
    for pairs, out, key, names in ((xref.each_net_pair(cp), nets, lambda n: n.cluster_id, written[0]),
                                   (xref.each_device_pair(cp), devs, lambda d: d.id(), written[1])):
        for p in pairs:
            a, b = _g(p, "first"), _g(p, "second")
            if a is not None and b is not None and _st(p) in ("Match", "MatchWithWarning"):
                out[key(a)] = names.get(_g(b, "name").upper(), _g(b, "name"))
    return nets, devs


def butted_taps(path: Path, gds: Path, terms: list, limit: int = 50) -> dict:
    """ERC from an LVS database: body taps that abut a source / drain of another net, an n+ / p+ junction against the
    tap with nothing between (a PCell's butted tap on the drain of a switch: the junction's capacitance and leakage on
    that net, and its reverse voltage, are in no model; where the junction is silicided, as on GF180MCU, a short no
    LVS deck sees). The taps of each net are its shapes on the database's layers that lie inside the taps drawn in
    `gds` (the run's input; `terms`: tap_layers, the union of each term's layers less those it is not on); a finding
    is a source / drain terminal of a MOSFET in the top circuit that shares an edge with a tap of another net, named
    as the schematic names them where LVS paired them (the layout's names otherwise). {"findings": [{instance, model,
    terminal, net, tap_net, edge_um, at: the shared edge's box}], "more": n} (at most `limit`)."""
    lvs = kdb.LayoutVsSchematic()
    lvs.read(str(path))
    ly, cell = lvs.internal_layout(), lvs.internal_top_cell()
    top = next(iter(lvs.netlist().each_circuit_top_down()), None) if lvs.netlist() else None
    src = kdb.Layout()
    src.read(str(gds))
    if top is None or src.cell(cell.name) is None:
        return {"findings": []}

    def drawn(ld) -> kdb.Region:
        li = src.find_layer(*ld)
        return kdb.Region(src.cell(cell.name).begin_shapes_rec(li)) if li is not None else kdb.Region()
    taps = kdb.Region()
    for term in terms:
        r = drawn(term[0][0])
        for ld, on in term[1:]:
            r = r & drawn(ld) if on else r - drawn(ld)
        taps += r
    if taps.is_empty():
        return {"findings": []}
    if src.dbu != ly.dbu:
        taps.transform(kdb.ICplxTrans(src.dbu / ly.dbu))
    layers = [li for li in lvs.layer_indexes() if not (r := kdb.Region(cell.begin_shapes_rec(li))).is_empty()
              and (r - taps).is_empty()]
    by_net = {}
    for n in top.each_net():
        r = kdb.Region()
        for li in layers:
            r += lvs.shapes_of_net(n, lvs.layer_by_index(li), True)
        if not r.is_empty():
            by_net[n.cluster_id] = (n, r.merged())
    xref = lvs.xref()
    nets, devs = schematic_names(lvs, xref, top, names_of(path.with_name("schematic.spice"))) if xref else ({}, {})
    name = lambda n: nets.get(n.cluster_id) or n.expanded_name()                 # noqa: E731
    found = []
    for d in top.each_device():
        dc = d.device_class()
        if {t.name for t in dc.terminal_definitions()} < {"S", "G", "D"}:     # a MOSFET (3 or 4 terminals)
            continue
        for td in dc.terminal_definitions():
            net = d.net_for_terminal(td.id())
            if td.name not in ("S", "D") or net is None:
                continue
            ref = next((t for t in net.each_terminal() if t.device().id() == d.id() and t.terminal_id() == td.id()), None)
            sd = kdb.Region()
            for r in (lvs.shapes_of_terminal(ref) if ref else {}).values():
                sd += r
            for nid, (tn, tr) in by_net.items():
                edge = sd.edges() & tr.edges()
                if nid != net.cluster_id and edge.length() > 0:
                    b = edge.bbox()
                    found.append({"instance": devs.get(d.id()) or d.expanded_name(), "model": dc.name,
                                  "terminal": td.name, "net": name(net), "tap_net": name(tn),
                                  "edge_um": round(edge.length() * ly.dbu, 4),
                                  "at": [round(v * ly.dbu, 4) for v in (b.left, b.bottom, b.right, b.top)]})
    found.sort(key=lambda f: (f["instance"], f["terminal"]))
    return {"findings": found[:limit], **({"more": len(found) - limit} if len(found) > limit else {})}


LVS_NAMES = 20      # names per list in lvs_diagnosis


def _flat_devices(c, memo: dict) -> dict[str, int]:
    """A circuit's devices by class (upper case: KLayout keeps the deck's class name in the layout's netlist, and its
    SPICE reader upper-cases the schematic's), its subcircuits' counted in."""
    if c.name in memo:
        return memo[c.name]
    out: dict[str, int] = defaultdict(int)
    for d in c.each_device():
        out[_g(d.device_class(), "name").upper()] += 1
    for sc in c.each_subcircuit():
        if (ref := sc.circuit_ref()) is not None:
            for k, n in _flat_devices(ref, memo).items():
                out[k] += n
    memo[c.name] = dict(out)
    return memo[c.name]


def lvs_diagnosis(run: Path, cell: str, detail: dict | None = None) -> dict:
    """Why a KLayout LVS run of `cell` does not match, in counts and names a reader acts on: {"layout": {circuit,
    devices by class, nets, pins}, "schematic": the same, "why": [...]}. An empty layout netlist (no circuit, or one
    without devices and nets: the deck's netlist simplification purges every net with fewer than two connections and
    no pin, then the devices and circuits left without nets) is said as such, with the top cell's labels; then device
    counts by class, the schematic's devices and pins without a layout counterpart (`detail`: lvs_detail), and the
    layout's nets with one connection."""
    lvs = kdb.LayoutVsSchematic()
    lvs.read(str(run / "result.lvsdb"))
    lay, ref = lvs.netlist(), lvs.reference

    def top(nl, want: str):
        if nl is None:
            return None
        cs = list(nl.each_circuit_top_down())
        return next((c for c in cs if c.name.lower() == want.lower()), cs[0] if cs else None)

    def info(c) -> dict:
        if c is None:
            return {"circuit": None, "devices": {}, "nets": 0, "pins": []}
        return {"circuit": c.name, "devices": dict(sorted(_flat_devices(c, {}).items())),
                "nets": sum(1 for _ in c.each_net()), "pins": [p.name() for p in c.each_pin()]}
    lc, rc = top(lay, cell), top(ref, cell)
    li, ri = info(lc), info(rc)
    why = []
    if lc is None or (not li["devices"] and not li["nets"]):
        labels = []
        if (run / "input.gds").is_file():
            ly = kdb.Layout()
            ly.read(str(run / "input.gds"))
            if (tc := ly.cell(cell)) is not None:
                labels = sorted({sh.text_string for li_ in ly.layer_indexes() for sh in tc.shapes(li_).each()
                                 if sh.is_text()})
        why.append(f"KLayout's netlist of the layout is empty ({'no circuit ' + cell if lc is None else cell + ' has no devices or nets'}"
                   "): its netlist simplification drops every net with fewer than two connections and no pin, then the "
                   "devices and circuits left without one. The layout's devices are not wired to each other (an "
                   "unrouted placement), or its devices are not recognized"
                   + (f"; its top cell has no labels, so no pins (the schematic has {len(ri['pins'])})" if not labels
                      else f"; its top cell's labels: {', '.join(labels[:LVS_NAMES])}"))
    for k in sorted(set(li["devices"]) | set(ri["devices"])):
        if li["devices"].get(k, 0) != ri["devices"].get(k, 0):
            why.append(f"{k}: {li['devices'].get(k, 0)} in the layout, {ri['devices'].get(k, 0)} in the schematic")
    rows = (detail or {}).get("devices") or []
    if (miss := [r["schematic"] for r in rows if r["layout"] is None and r["schematic"]]):
        why.append(f"schematic devices without a layout device: {', '.join(miss[:LVS_NAMES])}"
                   + (f" and {len(miss) - LVS_NAMES} more" if len(miss) > LVS_NAMES else ""))
    if (extra := [r["layout"] for r in rows if r["schematic"] is None and r["layout"]]):
        why.append(f"layout devices without a schematic device: {', '.join(extra[:LVS_NAMES])}"
                   + (f" and {len(extra) - LVS_NAMES} more" if len(extra) > LVS_NAMES else ""))
    lp = {p.lower() for p in li["pins"]}
    if (pins := [p for p in ri["pins"] if p.lower() not in lp]):
        why.append(f"schematic pins the layout does not have (a label on the net in the layout's top cell): "
                   f"{', '.join(pins[:LVS_NAMES])}" + (f" and {len(pins) - LVS_NAMES} more" if len(pins) > LVS_NAMES else ""))
    if lc is not None:
        ones = [n.expanded_name() for n in lc.each_net()
                if sum(1 for _ in n.each_terminal()) + sum(1 for _ in n.each_subcircuit_pin()) + sum(1 for _ in n.each_pin()) < 2]
        if ones:
            why.append(f"{len(ones)} layout nets with one connection (unconnected): {', '.join(ones[:LVS_NAMES])}")
    return {"layout": li, "schematic": ri, "why": why}


def lvs_detail(path: Path, circuit: str | None = None) -> dict:
    """Net / device / pin pairs of a circuit for the result browser."""
    lvs = kdb.LayoutVsSchematic()
    lvs.read(str(path))
    xref = lvs.xref()
    out: dict = {"nets": [], "devices": [], "pins": [], "subcircuits": []}

    def nm(o, attr="expanded_name"):
        if o is None:
            return None
        try:
            return _g(o, attr)
        except AttributeError:
            return _g(o, "name")

    for cp in xref.each_circuit_pair():
        c = _g(cp, 'first') or _g(cp, 'second')
        if circuit and _g(c, "name") != circuit:
            continue
        out["circuit"] = _g(c, "name")
        for p in xref.each_net_pair(cp):
            out["nets"].append({"layout": nm(_g(p, 'first')), "schematic": nm(_g(p, 'second')), "status": _st(p)})
        for p in xref.each_device_pair(cp):
            dc = _g(_g(p, 'first') or _g(p, 'second'), "device_class")
            out["devices"].append({"layout": nm(_g(p, 'first')), "schematic": nm(_g(p, 'second')), "status": _st(p),
                                   "class": _g(dc, "name") if dc is not None else ""})
        for p in xref.each_pin_pair(cp):
            out["pins"].append({"layout": nm(_g(p, 'first'), "name"), "schematic": nm(_g(p, 'second'), "name"),
                                "status": _st(p)})
        for p in xref.each_subcircuit_pair(cp):
            out["subcircuits"].append({"layout": nm(_g(p, 'first')), "schematic": nm(_g(p, 'second')), "status": _st(p)})
        break
    return out
