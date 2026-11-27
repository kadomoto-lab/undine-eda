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
      "options": {...},                                                         (deck switches, analysis, ...)
      "models":  [{"path": "/.../sky130.lib.spice", "sha": "..."}, ...],         (simulations only)
      "profile": {"drc_options": {...}, "deck_vars": {...}}                     (KLayout DRC; LVS: lvs_options,
    },                                                                            deck_vars; simulations: model_lib ...)
    "key":   "<hash of inputs>",          equal keys = the same run; a finished job with the key can be reused
    "about": {"packages": [{"name", "package", "version", "tree" | "sha", "index"}],      information only, not in
              "pdk": {"id", "commit", "family", "variant", "tree"},                        the key (`about`)
              "profile": {"family", "source": "built-in", "undine"}},
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

Models and profile (docs/design/packages.md, Changesets and evidence): a simulation records every file its netlist
reads through .lib / .include, transitively, and the OSDI files ngspice loads, by hash (`model_entries`; the original
files, not the reduced copies ngspice gets), so a model file a package's code block includes outdates the run even
when the netlist text is the same. A run records the fields of the PDK profile it uses (`profile_fields`): deck
options and deck variables for KLayout DRC and LVS; the model library, its includes, the OSDI models and the corners
for a simulation. A layer colour or anything else of the profile does not count.

`about` says what the run used, for the evidence: each package (lock entry) whose files it read, the PDK pin, the
profile. It is outside the key: the same files from another package version are the same run, and a README or an
unrelated cell of a package outdates nothing.

`recompute` builds the same inputs from the design as it is now; `diff` lists what changed. A job is outdated when
its design, PDK, deck, tool versions, model files or profile fields differ from now; jobs from before this record have
no inputs (unknown), and jobs from before models and profile were recorded are compared without them.
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


_FILE_SHAS: dict[tuple[str, int, int], str] = {}       # (path, mtime_ns, size) -> sha: decks, model files
_FILE_SHAS_LOCK = threading.Lock()


def cached_sha(p: Path) -> str | None:
    """file_sha of a file, read again only when its time or size changed (the sky130 model library is 7 MB in 170
    files, read by every freshness check of a simulation); None when it cannot be read."""
    try:
        st = p.stat()
        k = (str(p), st.st_mtime_ns, st.st_size)
        with _FILE_SHAS_LOCK:
            hit = _FILE_SHAS.get(k)
        if hit is None:
            hit = file_sha(p)
            with _FILE_SHAS_LOCK:
                _FILE_SHAS[k] = hit
        return hit
    except OSError:
        return None


def deck_entry(pdk, kind: str, name: str) -> dict:
    return {"kind": kind, "name": name, "sha": cached_sha(pdk.decks_dir / kind / name)}


def model_entries(paths) -> list[dict]:
    """[{"path", "sha"}] of the files a simulation reads besides its netlist (modellib.files_read, the OSDI files),
    by path; sha None for a file that cannot be read."""
    return [{"path": p, "sha": cached_sha(Path(p))} for p in sorted({str(p) for p in paths})]


def _plain(v):
    return json.loads(json.dumps(v))                     # tuples as lists: as status.json gives them back


DRC_PROFILE = ("drc_options", "deck_vars")
LVS_PROFILE = ("lvs_options", "deck_vars")
MAGIC_DRC_PROFILE = ("magic_maskhints",)
SIM_PROFILE = ("model_lib", "model_includes", "osdi", "corners")
LATER = ("models", "profile")               # inputs recorded since phase 4: a job from before has neither


def profile_fields(pdk, fields, deck: str | None = None) -> dict:
    """The fields of the PDK's profile a run uses, as they are now: drc_options (the defaults of DRC deck `deck`),
    lvs_options, deck_vars (this PDK's), model_lib, model_includes, osdi, corners, magic_maskhints, lvs_checks (the
    parameters Undine compares after the LVS deck: {lvs_elements model: {parameter: tolerance}}). None for each
    without a PDK."""
    p = pdk.profile if pdk else None
    get = {"drc_options": lambda: p.deck_defaults(deck or ""), "lvs_options": lambda: dict(p.lvs_options),
           "deck_vars": lambda: dict(p.deck_vars.get(pdk.name, {})), "model_lib": lambda: p.model_lib,
           "model_includes": lambda: list(p.model_includes), "osdi": lambda: list(p.osdi),
           "corners": lambda: list(p.corners), "magic_maskhints": lambda: list(p.magic_maskhints),
           "lvs_checks": lambda: {el.model: dict(el.check) for el in p.lvs_elements if el.check}}
    return _plain({f: (get[f]() if p is not None and f in get else None) for f in fields})


def magic_drc_profile(pdk) -> dict | None:
    """The profile fields a Magic DRC job records (MAGIC_DRC_PROFILE), only where they change what it runs: a PDK
    without magic_maskhints (GF180MCU, IHP) records none, so its jobs keep the key they had."""
    got = profile_fields(pdk, MAGIC_DRC_PROFILE)
    return got if got.get("magic_maskhints") else None


def lvs_profile(pdk, deck: str | None = None) -> dict:
    """The profile fields a KLayout LVS job records: LVS_PROFILE, and lvs_checks only where the profile has some
    (sky130's poly resistor W), so that a PDK without (GF180MCU, IHP) keeps the key its jobs had."""
    got = profile_fields(pdk, LVS_PROFILE + ("lvs_checks",), deck)
    if not got.get("lvs_checks"):
        got.pop("lvs_checks", None)
    return got


def unrecorded(kind: str | None, tool: str | None, inp: dict, pdk) -> list[str]:
    """What a job from before an input of its kind was recorded ran without, in words, where that input now changes
    the run (empty: nothing). A Magic DRC job without `profile` read no mask hints: on a PDK that has some
    (magic_maskhints) it made RPM / URPM from the resistors again, so its rpm.1 results are not today's. A KLayout LVS
    job without lvs_checks compared no parameter after the deck, on a PDK that has some."""
    if kind == "drc" and (tool or "klayout") == "magic" and "profile" not in inp:
        if (now := magic_drc_profile(pdk)) is not None:
            return [f"profile field magic_maskhints: not read → {now['magic_maskhints']}"]
    # a KLayout LVS job from before lvs_checks compared no parameter after the deck (two poly resistors in parallel
    # matched one)
    if kind == "lvs" and (tool or "klayout") == "klayout" and "lvs_checks" not in (inp.get("profile") or {}):
        if (now := lvs_profile(pdk).get("lvs_checks") if pdk is not None else None):
            return [f"profile field lvs_checks: not read → {now}"]
    return []


def inputs(design: list[dict], pdk, tool_names: list[str], deck: dict | None = None, options: dict | None = None,
           models: list[dict] | None = None, profile: dict | None = None) -> dict:
    """A job's inputs (the module's docstring); `models` and `profile` only for the jobs that record them, so that
    the key of any other job stays what it was."""
    return {"design": design, "pdk": pdk.id if pdk else None, "deck": deck,
            "tools": {n: tools.version(n) for n in tool_names}, "options": options or {},
            **({"models": models} if models is not None else {}),
            **({"profile": profile} if profile is not None else {})}


def key(inp: dict) -> str:
    return text_sha(json.dumps(inp, sort_keys=True, separators=(",", ":")))[:24]


def meta(inp: dict, about: dict | None = None) -> dict:
    """What goes into a new job's status.json: the inputs, their key and, outside the key, `about`."""
    return {"inputs": inp, "key": key(inp), **({"about": about} if about is not None else {})}


# ------------------------------------------------------------------ what a run used, for the evidence (not in the key)


def lock_packages(lock: dict | None) -> list[dict]:
    """The lock's entries (format 1 or 2) as [{"name": its first local name, "names", "package", "version", "tree" |
    "sha", "index"}], by name."""
    from undine.store import deps, resolve
    g = resolve.lock_graph(lock)
    out = []
    for id, e in g.entries.items():
        kind, h = deps.entry_hash(e)
        names = g.names_of(id)
        out.append({"name": names[0], "names": names, "package": e.get("package"), "version": e.get("version"),
                    kind: h, "index": e.get("index")})
    return sorted(out, key=lambda r: r["name"])


def read_lock(prj_path: Path) -> dict | None:
    """The project's undine.lock; None when there is none or this Undine refuses it (nothing to tell then)."""
    from undine.store import deps
    try:
        return deps.read_lock(Path(prj_path))
    except Exception:
        return None


def pdk_about(pdk, lock: dict | None) -> dict | None:
    """{"id", "commit", "family", "variant", "tree"} of a PDK; the tree is the lock's pin of it (format 2), else
    None."""
    if pdk is None:
        return None
    pin = (lock or {}).get("pdk")
    tree = pin.get("tree") if isinstance(pin, dict) and pin.get("id") == pdk.id else None
    return {"id": pdk.id, "commit": pdk.commit, "family": pdk.family, "variant": pdk.variant, "tree": tree}


def about(prj, read=()) -> dict:
    """What a run of project `prj` used, outside its key: {"packages": the lock entries one of whose installs holds a
    file in `read` (the files the run read: a netlist's schematics and symbols, its model files, a layout library's
    folder), "pdk": pdk_about, "profile": {"family", "source": "built-in", "undine": Undine's version}}. Profile
    packages come in phase 6: until then every profile is Undine's own."""
    from undine.store import deps
    lock = read_lock(prj.path)
    read = [Path(p) for p in read]
    packages = []
    for row in lock_packages(lock):
        dirs = [deps.dep_dir(Path(prj.path), n).resolve() for n in row["names"]]
        if any(p.is_relative_to(d) for p in read for d in dirs):
            packages.append({k: v for k, v in row.items() if k != "names"})
    pdk = prj.pdk
    return {"packages": packages, "pdk": pdk_about(pdk, lock),
            "profile": {"family": pdk.profile.family if pdk else None, "source": "built-in",
                        "undine": tools.undine_version()}}


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
    out = {"design": design, "pdk": prj.pdk.id if prj.pdk else None,
           "deck": deck_entry(prj.pdk, d["kind"], d["name"]) if d and prj.pdk else d,
           "tools": {n: tools.version(n) for n in inp.get("tools", {})}, "options": inp.get("options", {})}
    # only what the job recorded: a job from before models and profile were recorded stays comparable (not unknown).
    # The same model files are hashed again: a netlist that reads others has another text, so its own entry changes.
    # Not so for the OSDI models, which .spiceinit loads, not the netlist: those are the PDK's as found now
    # (Pdk.osdi_files picks the first that exists), so a model compiled again elsewhere is seen
    if "models" in inp:
        paths = [m["path"] for m in inp["models"] or []]
        osdi = [p for p in paths if p.endswith(".osdi")]
        if osdi and prj.pdk and prj.pdk.profile.osdi:
            try:
                osdi = [str(f) for f in prj.pdk.osdi_files()]
            except (OSError, ValueError):
                pass
        out["models"] = model_entries([p for p in paths if not p.endswith(".osdi")] + osdi)
    if "profile" in inp:
        out["profile"] = profile_fields(prj.pdk, list(inp["profile"] or {}), (d or {}).get("name"))
    return out


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
    if "models" in old and "models" in new:           # one without was recorded before models were: not a change
        om = {m["path"]: m.get("sha") for m in old["models"] or []}
        nm = {m["path"]: m.get("sha") for m in new["models"] or []}
        for p in sorted(set(om) | set(nm)):
            if p not in nm:
                out.append(f"model file {p} no longer read")
            elif p not in om:
                out.append(f"model file {p} now read")
            elif om[p] != nm[p]:
                out.append(f"model file {p} {'is gone' if nm[p] is None else 'changed'}")
    if "profile" in old and "profile" in new:
        op, np_ = old["profile"] or {}, new["profile"] or {}
        for k in sorted(set(op) | set(np_)):
            if op.get(k) != np_.get(k):
                out.append(f"profile field {k}: {op.get(k)} → {np_.get(k)}")
    return out


def freshness(srv, project: str, status: dict) -> dict:
    """{"fresh": True | False | None (no record), "changed": [...]} for a job's status."""
    inp = status.get("inputs")
    if not inp:
        return {"fresh": None, "changed": []}
    changed = diff(inp, recompute(srv, project, inp)) + unrecorded(status.get("kind"), status.get("tool"), inp,
                                                                  srv.project(project).pdk)
    return {"fresh": not changed, "changed": changed}
