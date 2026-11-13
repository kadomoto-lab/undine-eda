"""What a job ran on, and whether the design has changed since.

Every job (simulation, DRC, LVS) is a transformation of design views into results. When it starts, the job records its
inputs in status.json; when it finishes, the SHA-256 of every file it wrote:

    "inputs": {
      "design":  [{"view": "layout",  "lib": "mylib", "cell": "inv", "sha": "<content hash>"},
                  {"view": "netlist", "lib": "mylib", "cell": "inv", "top": "inv", "mode": "lvs", "flat": false,
                   "sha": "<hash of the reference netlist generated from the schematic>"}],
      "pdk":     "sky130A@8afc8346a57f",
      "deck":    {"kind": "drc", "name": "sky130A_mr.drc", "sha": "..."},          (KLayout decks only)
      "tools":   {"klayout": "KLayout 0.30.12"},
      "options": {...}                                                          (deck switches, analysis, ...)
    },
    "key":   "<hash of inputs>",          equal keys = the same run; a finished job with the key can be reused
    "files": {"result.lyrdb": "<sha256>", "input.gds": "...", ...}
    "stamps": {"result.lyrdb": [size, mtime_ns, inode], ...}     each file as it was hashed (`stamp`)

Design entries:
  layout   hash of the cell's hierarchy as the tools see it (cell names, layers, shapes, instances; order, instance
           properties and meta information do not count), so it does not change on save / reload
  netlist  hash of the netlist generated from a schematic: moving a wire without changing connectivity keeps it; a
           simulation's with `substitute` ({"lib/cell": its stand-in}) is generated with those cells as their stand-ins
  file     a netlist file on the server (LVS against an external SPICE file)
  text     a netlist sent with the request (never outdated: there is nothing to compare with)
  extraction  a cell simulated as extracted from its layout (parasitics.py): hash of the extracted subcircuit of the
           extraction `job` named, None once that job is outdated; without a job, of the newest up-to-date extraction
           of that mode, None once none is (the layout changed)

`recompute` builds the same inputs from the design as it is now; `diff` lists what changed. A job is outdated when
its design, PDK, deck or tool versions differ from now; jobs from before this record have no inputs (unknown).
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import weakref
from pathlib import Path

import klayout.db as kdb

from undine import tools


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def layout_sha(layout: kdb.Layout, cell: str) -> str:
    """Content hash of a cell and everything it instantiates. Independent of shape order, cell indexes, instance
    properties and meta information."""
    c = layout.cell(cell)
    if c is None:
        raise KeyError(f"no layout cell {cell}")
    cells = sorted([c] + [layout.cell(i) for i in c.called_cells()], key=lambda x: x.name)
    layers = sorted((str(layout.get_info(li)), li) for li in layout.layer_indexes())
    h = hashlib.sha256(f"dbu {layout.dbu:.12g}\n".encode())
    for cc in cells:
        h.update(f"cell {cc.name}\n".encode())
        for info, li in layers:
            shapes = cc.shapes(li)
            if shapes.is_empty():
                continue
            h.update(f"layer {info}\n".encode())
            h.update("\n".join(sorted(s.to_s() for s in shapes.each())).encode())
        insts = []
        for i in cc.each_inst():
            a = f" {i.a} {i.b} {i.na} {i.nb}" if i.is_regular_array() else ""
            insts.append(f"{i.cell.name} {i.cplx_trans}{a}")
        h.update(("\ninst " + "\ninst ".join(sorted(insts)) if insts else "").encode())
    return h.hexdigest()


class _LayoutShas:
    """layout_sha per (LayoutDB, version, cell): computed once per edit."""

    def __init__(self):
        self.by_db: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
        self.lock = threading.Lock()

    def get(self, db, cell: str) -> str:
        with self.lock:
            d = self.by_db.setdefault(db, {})
            hit = d.get(cell)
            if hit and hit[0] == db.version:
                return hit[1]
        with db.lock:
            v, sha = db.version, layout_sha(db.layout, cell)
        with self.lock:
            self.by_db.setdefault(db, {})[cell] = (v, sha)
        return sha


LAYOUT_SHAS = _LayoutShas()


def layout_entry(db, lib: str, cell: str) -> dict:
    return {"view": "layout", "lib": lib, "cell": cell, "sha": LAYOUT_SHAS.get(db, cell)}


_DECK_SHAS: dict[tuple[str, int], str] = {}


def deck_entry(pdk, kind: str, name: str) -> dict:
    p = pdk.decks_dir / kind / name
    try:
        k = (str(p), p.stat().st_mtime_ns)
        sha = _DECK_SHAS.get(k) or _DECK_SHAS.setdefault(k, file_sha(p))
    except OSError:
        sha = None
    return {"kind": kind, "name": name, "sha": sha}


def inputs(design: list[dict], pdk, tool_names: list[str], deck: dict | None = None, options: dict | None = None) -> dict:
    return {"design": design, "pdk": pdk.id if pdk else None, "deck": deck,
            "tools": {n: tools.version(n) for n in tool_names}, "options": options or {}}


def key(inp: dict) -> str:
    return text_sha(json.dumps(inp, sort_keys=True, separators=(",", ":")))[:24]


def meta(inp: dict) -> dict:
    """What goes into a new job's status.json."""
    return {"inputs": inp, "key": key(inp)}


def stamp(st: os.stat_result) -> list[int]:
    """What says a file is unchanged without reading it again: [size, mtime_ns, inode] (a file renamed in at its path
    is another inode, a write changes the size or the time). The changesets' evidence compares these
    (changeset.py)."""
    return [st.st_size, st.st_mtime_ns, st.st_ino]


def outputs(job_dir: Path) -> dict:
    """{"files": {name: sha256}, "stamps": {name: stamp}} of what is in the job directory (written when the job
    finishes). A file's stamp is the one it had while it was hashed (none when it changed meanwhile): the evidence of
    a changeset takes the hash from here instead of reading the file again while its stamp is the same."""
    files, stamps = {}, {}
    for p in sorted(job_dir.rglob("*")):
        if p.is_file() and not p.name.startswith("status.json"):
            name, before = str(p.relative_to(job_dir)), stamp(p.stat())
            files[name] = file_sha(p)
            if stamp(p.stat()) == before:
                stamps[name] = before
    return {"files": files, "stamps": stamps}


# ------------------------------------------------------------------ now vs. then


def recompute(srv, project: str, inp: dict) -> dict:
    """The same inputs, from the design as it is now (a design view that no longer exists gets sha None)."""
    prj = srv.project(project)
    design = []
    for e in inp.get("design", []):
        e = dict(e)
        try:
            if e["view"] == "layout":
                e["sha"] = LAYOUT_SHAS.get(srv.db(project, e["lib"]), e["cell"])
            elif e["view"] == "netlist":
                e["sha"] = srv.netlist_sha(project, e["lib"], e["cell"], e.get("top") or e["cell"], e["mode"], e.get("flat", False),
                                           e.get("substitute"))
            elif e["view"] == "file":
                e["sha"] = file_sha(Path(e["path"]))
            elif e["view"] in ("schematic-interface", "symbol-interface"):
                from undine.schematics import symbolcheck
                e["sha"] = symbolcheck.design_entry(prj, e["lib"], e["cell"], e["view"])["sha"]
            elif e["view"] == "extraction":
                e["sha"] = srv.extraction_sha(project, e["lib"], e["cell"], e.get("job") or e.get("mode"))
        except (KeyError, OSError):
            e["sha"] = None
        design.append(e)
    d = inp.get("deck")
    return {"design": design, "pdk": prj.pdk.id if prj.pdk else None,
            "deck": deck_entry(prj.pdk, d["kind"], d["name"]) if d and prj.pdk else d,
            "tools": {n: tools.version(n) for n in inp.get("tools", {})}, "options": inp.get("options", {})}


def _name(e: dict) -> str:
    if e["view"] == "file":
        return f"netlist file {e['path']}"
    what = {"netlist": "schematic netlist", "schematic-interface": "schematic interface",
            "symbol-interface": "symbol interface", "extraction": "extracted view"}.get(e["view"], e["view"])
    return f"{what} of {e['lib']}/{e['cell']}"


def _ident(e: dict) -> dict:
    return {k: v for k, v in e.items() if k != "sha"}


def diff(old: dict, new: dict) -> list[str]:
    """What differs between the inputs a job ran on and `new`, in words (empty: up to date)."""
    out = []
    for a, b in zip(old.get("design", []), new.get("design", [])):
        if _ident(a) != _ident(b):           # another source or other settings (e.g. a flat reference), not an edit
            if _name(a) != _name(b):
                out.append(f"ran on {_name(a)}, now {_name(b)}")
            else:
                ka = {k: v for k, v in _ident(a).items() if _ident(b).get(k) != v}
                out.append(f"{_name(a)}: " + ", ".join(f"{k} {v} → {_ident(b).get(k)}" for k, v in ka.items()))
        elif a.get("sha") != b.get("sha"):
            out.append(f"{_name(a)} {'is gone' if b.get('sha') is None else 'changed'}")
    if len(old.get("design", [])) != len(new.get("design", [])):
        out.append("other design inputs")
    if old.get("pdk") != new.get("pdk"):
        out.append(f"PDK {old.get('pdk')} → {new.get('pdk')}")
    if (old.get("deck") or {}).get("sha") != (new.get("deck") or {}).get("sha"):
        out.append(f"deck {(old.get('deck') or {}).get('name')} changed")
    for n, v in old.get("tools", {}).items():
        if new.get("tools", {}).get(n) != v:
            out.append(f"{n}: {v} → {new.get('tools', {}).get(n)}")
    oo, no = old.get("options", {}), new.get("options", {})
    for k in sorted(set(oo) | set(no)):
        if oo.get(k) != no.get(k):
            out.append(f"option {k}: {oo.get(k, 'default')} → {no.get(k, 'default')}")
    return out


def freshness(srv, project: str, status: dict) -> dict:
    """{"fresh": True | False | None (no record), "changed": [...]} for a job's status."""
    inp = status.get("inputs")
    if not inp:
        return {"fresh": None, "changed": []}
    changed = diff(inp, recompute(srv, project, inp))
    return {"fresh": not changed, "changed": changed}
