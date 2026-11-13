"""Design API (HTTP). The web UI is one client of it; scripts and agents are others."""
from __future__ import annotations

import hmac
import json
import re
import sys
import threading
import time
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Callable, Literal

import anyio
import anyio.to_thread
import orjson
import klayout.db as kdb
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, FiniteFloat

from undine.agents import agent, turns, llm, llm_context, mcp as mcpmod
from undine.server import live
from undine.store import deps, locked, oplog, provenance, cellviews, trust
from undine.checks import magicflow, parasitics, stdcells, tapeout, verify
from undine.pdks import ciel, compat, fetchjobs, pcell, pdkops
from undine.schematics import schematic, xschem, symbolcheck, symbolsync, netlist as netlistmod
from undine.layouts import layoutstore, sdl
from undine.simulation import sim
from undine import tools
from undine.design import gatelevel, intent as intentmod, rtl, librelane
from undine.layouts.layout import LayoutDB
from undine.pdks.pdk import Pdk, default_pdk_roots, discover
from undine.store.project import NAME_RE, Project, ProjectStore, check_name, import_layout, pick_pdk
from undine.store import changeset as changesetmod
from undine.store.changeset import Changesets
from undine.store.oplog import OpLog
from undine.schematics.schedit import SchEditor
from undine.vcs import branches, history as githistory

MCP_THREADS = 64        # MCP calls at once (each in a worker thread; most of them wait for a job or a person)


@dataclass
class Run:
    """A job about to start: what it will run on (`inputs`) and how to start it."""
    project: str
    kind: str          # drc | lvs
    tool: str          # klayout | magic | netgen
    lib: str
    cell: str
    deck: str | None
    inputs: dict
    launch: Callable[[], sim.Job]


def _passed(st: dict) -> bool | None:
    s = st.get("summary") or {}
    if st.get("state") != "done":
        return None
    return s.get("total") == 0 if "total" in s else bool(s.get("match"))


def _set_corner(text: str, model_lib: Path, corner: str) -> tuple[str, int]:
    """Points the `.lib <PDK model library> <section>` lines at another section; how many there were."""
    pat = re.compile(r"^([ \t]*\.lib[ \t]+)(['\"]?)(\S+?)\2([ \t]+)(\S+)[ \t]*$", re.M | re.I)
    n = 0

    def repl(m):
        nonlocal n
        if Path(m.group(3)) != model_lib:
            return m.group(0)
        n += 1
        return f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(2)}{m.group(4)}{corner}"
    return pat.sub(repl, text), n


def _is_cell_spice(statement: str, pdk: Pdk | None) -> bool:
    """Whether an .include names a file of one of the PDK's standard-cell libraries (libs.ref/<library>/spice/...):
    subcircuits made of the PDK's devices, no models."""
    parts = statement.split()
    if pdk is None or len(parts) < 2:
        return False
    try:
        rel = Path(parts[1].strip("'\"")).resolve().relative_to(pdk.root.resolve()).parts
    except (ValueError, OSError):
        return False
    return len(rel) == 4 and rel[0] == "libs.ref" and rel[2] == "spice" and \
        (pdk.profile.stdcell(f"{rel[1]}__cell") or ("",))[0] == rel[1]


def _cell_spice(text: str, pdk: Pdk | None) -> list[str]:
    """The SPICE files of the PDK's standard cells `text` calls (sky130_fd_sc_hd__nand2_1, from the PDK's own symbols
    sky130_stdcells/nand2_1.sym or a symbol of that name) that it neither defines nor includes: the cell's own file when
    the library has one (sky130_ef_sc_hd__decap_12.spice), else the library's (sky130_fd_sc_hd.spice)."""
    if pdk is None or not pdk.profile.stdcells:
        return []
    lines: list[str] = []
    for raw in text.splitlines():
        if raw.startswith("+") and lines:
            lines[-1] += " " + raw[1:]
        else:
            lines.append(raw)
    defined = {ln.split()[1].lower() for ln in lines if ln.strip().lower().startswith(".subckt") and len(ln.split()) > 1}
    included = set()
    for ln in lines:
        if re.match(r"\s*\.(include|inc|lib)\s", ln, re.I) and len(ln.split()) > 1:
            try:
                included.add(Path(ln.split()[1].strip("'\"")).resolve())
            except OSError:
                pass
    out: list[str] = []
    for ln in lines:
        t = ln.split()
        calls = [x for x in t[1:] if "=" not in x] if t and t[0][:1] in "xX" else []
        name = calls[-1] if calls else ""
        if not name or name.lower() in defined or not (hit := pdk.profile.stdcell(name)):
            continue
        defined.add(name.lower())                       # each cell once
        spice = pdk.root / "libs.ref" / hit[0] / "spice"
        f = next((p for p in (spice / f"{name}.spice", spice / f"{hit[0]}.spice") if p.is_file()), None)
        if f is not None and f.resolve() not in included and str(f) not in out:
            out.append(str(f))
    return out


def _dep_hash(project_path: Path, name: str) -> str | None:
    """What an installed dependency's content is known by: its tree (format 2 entries) or its sha (format 1)."""
    have = deps.installed(project_path, name) or {}
    return have.get("tree") or have.get("sha")


def _libs(libraries: list[str] | None) -> str:
    return ", ".join(libraries) if libraries else "ciel's default libraries"


def _scanned(p: Pdk) -> tuple:
    """What a scan finds of a PDK: where it is and what it derived from its files then. A scan that ran while ciel was
    still extracting saw no layer properties or xschem directory yet; the next scan differs, so the object is replaced."""
    return (_real(p.root), p.family, p.version, p.source, p.lyp, p.xschem_dir, p.klayout_tech)


def _real(p: Path) -> Path:
    try:
        return p.resolve()
    except OSError:
        return p


class Server:
    def __init__(self, root: Path, pdks: dict[str, Pdk], import_roots: list[Path], llm_policy: str | None = None,
                 mcp: bool = False, dep_roots: list[Path] | None = None, pdk_roots: list[Path] | None = None):
        self.store = ProjectStore(root, pdks)
        self.gateway = llm.Gateway(root, llm_policy)
        self.mcp_enabled = mcp and self.gateway.policy != "none"
        self.selection: dict[str, dict] = {}   # project -> what the GUI has selected right now
        self.pdks = pdks
        self.import_roots = [p.resolve() for p in import_roots]
        # where a library dependency's local repository (file://, /..., ~/...) may be for a request (--dep-root; none
        # by default): a browser must not read every repository on this disk (deps._source)
        self.dep_roots = [p.expanduser().resolve() for p in dep_roots or []]
        # where PDKs are found again (rescan_pdks) and where `pdk fetch` installs (pdkops.fetch_plan): those `pdks` came
        # from (`--pdk-root`, else $PDK_ROOT, ~/.ciel, ~/.volare)
        self.pdk_roots = list(pdk_roots) if pdk_roots is not None else default_pdk_roots()
        self._rescanning = threading.Lock()       # one rescan at a time: each works from the dict as the last left it
        self.pdk_jobs = fetchjobs.Jobs()          # PDK fetches over HTTP, in memory (fetchjobs.py)
        self.confirmations = fetchjobs.Confirmations()   # the person's confirmation of a fetch (fetchjobs.py)
        self.dbs: dict[tuple[str, str], LayoutDB] = {}
        self.editors: dict[Path, SchEditor] = {}
        self.logs: dict[Path, OpLog] = {}
        # the root's simulations: what a server stopped before this one left running or queued goes on here, and
        # what this one runs goes on in the next one (sim.py, Jobs.shutdown)
        self.jobs = sim.Jobs(root=self.store.root)
        # set when this server is asked to stop (`undine serve`: Ctrl-C, SIGTERM): a tool that waits for jobs or the
        # person (wait_for, run_simulation, check_specs ...) answers at once with what it has, so that the calls in
        # flight end and the server stops without cutting one (its jobs go on in the next server)
        self.stopping = threading.Event()
        self.stop_jobs = False      # `undine serve --stop-jobs`: its simulations end with it (they do not go on)
        self.netlist_shas: dict[tuple, tuple[list, str]] = {}
        self.lock = threading.RLock()
        self.changeset_lock = threading.RLock()
        self.screens: dict[str, float] = {}     # project -> when a visible Undine screen last asked who is writing
        self.batching: dict[str, int] = {}      # changeset -> its head before a running batch (activity hides the rest)
        self.batch_progress: dict[str, dict] = {}   # changeset -> a running batch's {ops, done, by, started}
        self.leases = live.Leases()               # one writer per changeset (live.py)
        self.closings = live.Closings()           # the changesets being applied, discarded, reverted, how far (live.py)
        self.turns = turns.Turns()                # assistant turns running in the background (turns.py)
        self._cs_locks: dict[str, threading.RLock] = {}
        self._previews: dict[str, changesetmod.Preview] = {}    # "<project>~<id>" -> where its worktrees are
        self._cs_projects: dict[str, str] = {}                  # changeset id -> its project (lease_ended)
        self._cs_swept: set[str] = set()                        # projects whose changesets were tidied (changeset.py)

    def project(self, name: str):
        """A project by name. `<project>~<changeset>` is the working copy of a changeset, in its git worktree
        (`...~base`: its baseline, the base commit checked out, every library read-only); changeset.py."""
        if "~" in name:
            pv = self.preview(name)
            base = name.count("~") == 2
            d = pv.base if base else pv.work
            if not (d / "project.json").is_file():
                raise KeyError(f"no changeset preview {name}")
            return changesetmod.Baseline(d, self.pdks) if base else changesetmod.Working(d, self.pdks)
        return self.store.get(name)

    def preview(self, name: str) -> changesetmod.Preview:
        """Where the preview `<project>~<id>` (or `...~base`) is, from the changeset's record, read once (KeyError:
        no such changeset)."""
        parts = name.split("~")
        if len(parts) not in (2, 3) or parts[2:] not in ([], ["base"]) or not NAME_RE.match(parts[0]) \
                or not changesetmod.CID.fullmatch(parts[1]):
            raise KeyError(f"no changeset preview {name}")
        key = f"{parts[0]}~{parts[1]}"
        with self.lock:
            pv = self._previews.get(key)
        if pv is None:
            pv = changesetmod.preview(self.store.root, parts[0], parts[1])
            with self.lock:
                self._previews[key] = pv
                self._cs_projects[parts[1]] = parts[0]
        return pv

    def lease_ended(self) -> None:
        """Commits the working copy of each changeset whose writer's lease ended (live.Leases.take_ended): at the end of
        an assistant turn or an MCP session, after a stop, and when a screen asks who is writing (a lease that lapsed)."""
        for cid in self.leases.take_ended():
            project = self._cs_projects.get(cid)
            if project is None:                 # never written through this server: nothing to commit
                continue
            try:
                Changesets(self).commit(project, cid)
            except Exception as e:              # the checks or the closure commit it later
                print(f"undine: cannot commit the working copy of {project}~{cid}: {e}", file=sys.stderr)

    # ------------------------------------------------------------------ jobs and what they ran on (provenance.py)

    def reference_netlist(self, prj, lib: str, sc: str, top: str, mode: str, flat: bool = False,
                          overrides: dict | None = None, extracted: dict | None = None,
                          substitute: dict[str, str] | None = None, instances: dict | None = None,
                          stdcell_info: dict | None = None, result_out: list | None = None) -> tuple[str, list[str]]:
        """The netlist a job gets from schematic `lib/sc`: mode sim (sim_netlist), lvs (KLayout), netgen. `instances`
        (lvs, netgen) is filled with the top cell's elements as the netlist writes them before lvs_elements:
        {element: the schematic's instance name}. Mode lvs adds the PDK's standard cells it calls (stdcells.reference;
        `stdcell_info` is filled with what it did). `result_out` gets the netlister's NetlistResult appended (where
        each line came from: the trust gate a run checks at launch)."""
        if mode == "sim":
            r = self.sim_netlist(prj, lib, sc, overrides, extracted, substitute)
            if result_out is not None:
                result_out.append(r)
            return r.text, r.warnings
        path = prj.library(lib).path / f"{sc}.sch"
        if not path.is_file():
            raise KeyError(f"no schematic {lib}/{sc}")
        res = xschem.SymbolResolver(prj.symbol_paths())
        r = schematic.spice(path, res, self.model_env(prj), mode=mode, top_subckt=True, top_name=top)
        if result_out is not None:
            result_out.append(r)
        if instances is not None:
            instances.update(r.instances)
        text = prj.pdk.profile.lvs_netlist(r.text) if mode == "lvs" and prj.pdk else r.text
        if mode == "lvs" and prj.pdk:
            text, info = stdcells.reference(text, prj.pdk)
            if stdcell_info is not None:
                stdcell_info.update(info)
        # a flat extraction is compared with a flat reference
        return (schematic.flatten_spice(text, top) if flat else text), r.warnings

    def sim_netlist(self, prj, lib: str, sc: str, overrides: dict | None = None, extracted: dict | None = None,
                    substitute: dict[str, str] | None = None) -> schematic.NetlistResult:
        """The netlist a simulation gets from testbench `lib/sc`: as drawn, or with property `overrides` for a sweep,
        with `extracted` {"lib/cell": (its schematic, extracted subcircuit)} in place of those cells, and with
        `substitute` {"lib/cell": its stand-in} (checked: standins) netlisting every instance of a cell as another."""
        path = prj.library(lib).path / f"{sc}.sch"
        if not path.is_file():
            raise KeyError(f"no schematic {lib}/{sc}")
        res = xschem.SymbolResolver(prj.symbol_paths())
        env = self.model_env(prj)
        r = schematic.spice(path, res, env, top_subckt=False, overrides=overrides,
                            extracted=dict((extracted or {}).values()),
                            substitute=self.standins(prj, res, substitute) if substitute else None)

        def hidden(name: str) -> str | None:
            """Why cell `name` is not netlisted from its schematic when a stand-in is the cause, in words."""
            for k, v in (substitute or {}).items():
                klib, _, kcell = k.partition("/")
                if k not in r.substituted or not (sch := prj.library(klib).path / f"{kcell}.sch").is_file():
                    continue
                if name == kcell:
                    return f"{k} is netlisted as its stand-in {v} (substitute)"
                if name in schematic.spice(sch, res, env, top_subckt=True).subckts:
                    return f"it lies inside {k}, which is netlisted as its stand-in {v} (substitute)"
            return None
        for key, (p, _) in (extracted or {}).items():
            if Path(p).stem not in r.extracted:
                raise ValueError(f"{key} is not netlisted from a schematic in {lib}/{sc}: " + (
                    hidden(Path(p).stem) or "it is not placed in the testbench or below it, or lies inside another "
                                            "cell simulated as extracted"))
        if extracted or substitute:
            from_schematics = ({sc} | set(r.subckts)) - set(r.extracted)
            for c in overrides or {}:
                if c in from_schematics:
                    continue
                why = ("it is simulated as extracted from its layout" if c in r.extracted else hidden(c)
                       or "it is not in the testbench, or lies inside a cell simulated as extracted")
                tip = next((f": vary the instances of the stand-in ({v.partition('/')[2]}) instead"
                            for k, v in (substitute or {}).items() if k in r.substituted and k.partition("/")[2] == c),
                           "")
                raise ValueError(f"vary / conditions on {c}: {why}, so its schematic's properties do not apply{tip}")
        return r

    def standins(self, prj, res: xschem.SymbolResolver, substitute: dict[str, str]) -> dict[Path, schematic.Standin]:
        """`substitute` ({"lib/cell": its stand-in}) as the netlister takes it (schematic.spice), checked: both cells
        exist, no cell stands in for itself or is a stand-in and replaced as well, and the stand-in has the pins of the
        cell it replaces in their order (each cell's symbol pins, or its schematic's ports when it has no symbol).
        ValueError names what differs. A cell without a symbol is never placed: nothing to replace."""
        def files(key: str, what: str) -> tuple[Path | None, Path | None, list[str]]:
            lib, _, cell = key.partition("/")
            try:
                d = prj.library(lib).path
            except KeyError:
                raise ValueError(f"{what}: no library {lib}") from None
            sym, sch = d / f"{cell}.sym", d / f"{cell}.sch"
            if sym.is_file():
                return sym, sch if sch.is_file() else None, [p.name for p in schematic.sym_pins(res.load(str(sym), None))]
            if sch.is_file():
                return None, sch, [n for n, _ in schematic.connectivity(sch, res, cell).ports]
            raise ValueError(f"{what}: no cell {key} (neither a symbol nor a schematic)")
        out: dict[Path, schematic.Standin] = {}
        for orig, new in substitute.items():
            what = f"substitute {orig} -> {new}"
            if orig == new:
                raise ValueError(f"{what}: a cell does not stand in for itself")
            if new in substitute:
                raise ValueError(f"{what}: {new} is replaced as well (by {substitute[new]}); name each cell's final "
                                 "stand-in")
            (osym, _, opins), (nsym, nsch, npins) = files(orig, what), files(new, what)
            if (why := schematic.pins_differ(opins, npins)):
                raise ValueError(f"{what}: the stand-in's pins are not those of {orig} in their order ({why}). "
                                 f"{orig}: {' '.join(opins)}; {new}: {' '.join(npins)}")
            if osym is not None:
                out[osym] = schematic.Standin(orig, new, new.partition("/")[2],
                                              res.load(str(nsym), None) if nsym else None, nsch, nsch or nsym)
        return out

    def substitutes(self, project: str, lib: str, cell: str,
                    substitute=None) -> tuple[dict[str, str], dict[str, str], list[str]]:
        """The cells a run of testbench `lib/cell` netlists as others ({"lib/cell": its stand-in}, sorted), where each
        comes from (the item of the testbench's intent, or "run") and warnings: the testbench's default, constraints of
        its intent with rule {type: substitute, cells: {...}} (intent.substitutes), with the run's `substitute`
        (sim.substitute_map) over it, where a cell mapped to null or "" is netlisted as itself. An intent file that is
        not YAML does not stop the run (a warning says its stand-ins were not read); a broken substitute rule does."""
        prj = self.project(project)
        path = intentmod.path_of(prj.library(lib).path, cell)
        layers = {}
        try:
            rules, unread = intentmod.substitutes(path)
        except ValueError as e:
            raise ValueError(f"the intent of {lib}/{cell} ({path.name}), {e}") from None
        warns = [f"the intent of {lib}/{cell}: {unread}, so its default stand-ins (substitute rules) were not read"
                 ] if unread else []
        for key, (new, item) in rules.items():
            try:
                layers.update({k: (v, item) for k, v in sim.substitute_map({key: new}).items()})
            except ValueError as e:
                raise ValueError(f"the intent of {lib}/{cell} ({path.name}), {item}: {e}") from None
        layers.update({k: (v, "run") for k, v in sim.substitute_map(substitute).items()})
        return ({k: v for k, (v, _) in sorted(layers.items()) if v},
                {k: item for k, (v, item) in sorted(layers.items()) if v}, warns)

    def netlist_sha(self, project: str, lib: str, sc: str, top: str, mode: str, flat: bool = False,
                    substitute: dict[str, str] | None = None) -> str:
        """Hash of reference_netlist (with `substitute`, sim_netlist), regenerated only when a schematic or symbol of
        the project changed. A substitution that no longer holds (the stand-in's pins changed) hashes its error: the
        netlist changed."""
        prj = self.project(project)
        fp = []
        for lb in prj.libraries().values():
            if lb.origin == "dep":                           # changes only through sync / update (deps.py)
                fp.append((lb.name, _dep_hash(prj.path, lb.name)))
            elif not lb.readonly and lb.path.is_dir():
                fp += [(f.name, s.st_mtime_ns, s.st_size) for f in sorted(lb.path.iterdir())
                       if f.suffix in (".sch", ".sym") and (s := f.stat())]
        # every directory in .undine/deps/, declared or not: a leftover one (project.json no longer names it) still
        # resolves there, so sync removing it changes the netlist (Project.symbol_paths)
        dep_root = prj.path / deps.DEPS
        fp += [("deps/" + d.name, _dep_hash(prj.path, d.name))
               for d in (sorted(dep_root.iterdir()) if dep_root.is_dir() else []) if d.is_dir() and d.name[0] != "."]
        k = (str(prj.path), lib, sc, top, mode, flat, tuple(sorted((substitute or {}).items())))
        hit = self.netlist_shas.get(k)
        if hit and hit[0] == fp:
            return hit[1]
        try:
            sha = provenance.text_sha(self.reference_netlist(prj, lib, sc, top, mode, flat, substitute=substitute)[0])
        except ValueError as e:
            if not substitute:
                raise
            sha = provenance.text_sha(f"refused: {e}")
        self.netlist_shas[k] = (fp, sha)
        return sha

    def _lvs_reference(self, prj, lib: str, cell: str, schematic_cell: str | None, schematic_file: str | None,
                       mode: str, flat: bool = False, instances: dict | None = None,
                       stdcell_info: dict | None = None, result_out: list | None = None) -> tuple[str, str, dict]:
        """(netlist text, label, design entry) of what LVS compares against; `instances`, `result_out`:
        reference_netlist's (a netlist file given by path has no result)."""
        if schematic_file:
            src = Path(schematic_file).expanduser().resolve()
            if not any(src.is_relative_to(r) for r in self.import_roots):
                raise PermissionError(f"{src} is outside the allowed directories")
            text = src.read_text()
            return text, str(src), {"view": "file", "path": str(src), "sha": provenance.text_sha(text)}
        sc = schematic_cell or cell
        text, _ = self.reference_netlist(prj, lib, sc, cell, mode, flat, instances=instances, stdcell_info=stdcell_info,
                                         result_out=result_out)
        return text, f"{lib}/{sc}", {"view": "netlist", "lib": lib, "cell": sc, "top": cell, "mode": mode, "flat": flat,
                                     "sha": provenance.text_sha(text)}

    def _run(self, project: str, kind: str, tool: str, lib: str, cell: str, deck: str | None, inputs: dict,
             start, gate: Callable[[], None] | None = None) -> "Run":
        """A run whose inputs are known: `start(meta)` creates the job. The layout entry is taken again, under the
        layout lock, right before the export, so the recorded hash is the one of what the tool gets. `gate` runs
        first and may refuse the launch (the trust gate): only launching is refused, so a run prepared for its status
        (suite, signoff, a changeset's reuse lookup) still is."""
        def launch():
            if gate is not None:
                gate()
            db = self.db(project, lib)
            with db.lock:
                inp = {**inputs, "design": [provenance.layout_entry(db, e["lib"], e["cell"]) if e["view"] == "layout" else e
                                            for e in inputs["design"]]}
                return start(db, provenance.meta(inp))
        return Run(project, kind, tool, lib, cell, deck, inputs, launch)

    def _pdk_of(self, project: str):
        prj = self.project(project)
        if not prj.pdk:
            raise ValueError("project has no PDK")
        return prj

    def prepare_drc(self, project: str, lib: str, cell: str, deck: str | None = None, options: dict | None = None) -> "Run":
        prj = self._pdk_of(project)
        deck = deck or (verify.decks(prj.pdk)["drc"] or [None])[0]
        if deck is None:
            raise ValueError("the PDK has no DRC deck")
        db = self.db(project, lib)
        inp = provenance.inputs([provenance.layout_entry(db, lib, cell)], prj.pdk, ["klayout"],
                                provenance.deck_entry(prj.pdk, "drc", deck), options or {})
        return self._run(project, "drc", "klayout", lib, cell, deck, inp, lambda db, meta: verify.start_drc(
            self.jobs, prj.path, prj.pdk, db.layout, cell, lib, deck, options or {}, meta))

    def prepare_magic_drc(self, project: str, lib: str, cell: str) -> "Run":
        prj = self._pdk_of(project)
        db = self.db(project, lib)
        inp = provenance.inputs([provenance.layout_entry(db, lib, cell)], prj.pdk, ["magic"])
        return self._run(project, "drc", "magic", lib, cell, None, inp, lambda db, meta: magicflow.start_drc(
            self.jobs, prj.path, prj.pdk, db.layout, cell, lib, meta))

    def prepare_lvs(self, project: str, lib: str, cell: str, deck: str | None = None, options: dict | None = None,
                    schematic_cell: str | None = None, schematic_file: str | None = None) -> "Run":
        prj = self._pdk_of(project)
        deck = deck or (verify.decks(prj.pdk)["lvs"] or [None])[0]
        if deck is None:
            raise ValueError("the PDK has no LVS deck")
        flat = prj.pdk.profile.lvs_rd(options or {}, prj.pdk.name).get("run_mode") == "flat"   # the PDK's default too
        instances: dict[str, str] = {}
        cells: dict = {}
        result: list = []
        sch, label, ref = self._lvs_reference(prj, lib, cell, schematic_cell, schematic_file, "lvs", flat, instances, cells,
                                              result)
        if cells.get("simplify") and cells.get("cells"):
            # the standard cells' parallel fingers combined in the reference, as the deck combines the layout's
            options = {"schematic_simplify": True, **(options or {})}
        db = self.db(project, lib)
        inp = provenance.inputs([provenance.layout_entry(db, lib, cell), ref], prj.pdk, ["klayout"],
                                provenance.deck_entry(prj.pdk, "lvs", deck), options or {})
        return self._run(project, "lvs", "klayout", lib, cell, deck, inp, lambda db, meta: verify.start_lvs(
            self.jobs, prj.path, prj.pdk, db.layout, cell, lib, sch, deck, options or {}, label, meta, instances),
            gate=self._paths_gate(prj, result))

    def prepare_netgen_lvs(self, project: str, lib: str, cell: str, schematic_cell: str | None = None,
                           schematic_file: str | None = None) -> "Run":
        prj = self._pdk_of(project)
        result: list = []
        sch, label, ref = self._lvs_reference(prj, lib, cell, schematic_cell, schematic_file, "netgen",
                                              result_out=result)
        db = self.db(project, lib)
        inp = provenance.inputs([provenance.layout_entry(db, lib, cell), ref], prj.pdk, ["magic", "netgen"])
        return self._run(project, "lvs", "netgen", lib, cell, None, inp, lambda db, meta: magicflow.start_lvs(
            self.jobs, prj.path, prj.pdk, db.layout, cell, lib, sch, label, meta), gate=self._paths_gate(prj, result))

    def _paths_gate(self, prj, result: list) -> Callable[[], None]:
        """The trust gate of an LVS run (KLayout, netgen), checked when it launches: the reference netlist's lines
        from untrusted dependencies may not read a file outside the PDK (they run no control code, so only paths).
        Whom the project trusts is read then, so trust given after the run was prepared counts."""
        def gate() -> None:
            if result:
                trust.check_netlist(result[0], untrusted=trust.untrusted_dirs(prj.path),
                                    pdk_root=prj.pdk.root if prj.pdk else None, env=self.model_env(prj), rules="paths")
        return gate

    def prepare_extraction(self, project: str, lib: str, cell: str, mode: str = "c", cthresh_ff: float | None = None,
                           meta: dict | None = None, res_threshold_ohm: float | None = None,
                           res_nets: list[str] | None = None) -> "Run":
        """Parasitic extraction of a layout cell (parasitics.py), with the ports of its schematic when it has one;
        mode rc with its resistance options (parasitics.res_options)."""
        prj = self._pdk_of(project)
        if mode not in parasitics.MODES:
            raise ValueError(f"mode is c (capacitance) or rc (capacitance and wire resistance), not {mode!r}")
        cthresh = parasitics.CTHRESH_FF if cthresh_ff is None else float(cthresh_ff)
        if not cthresh >= 0:
            raise ValueError("cthresh_ff is a capacitance in fF, 0 or more")
        if mode != "rc" and (res_threshold_ohm is not None or res_nets):
            raise ValueError("res_threshold_ohm and res_nets are for mode rc (the wire resistance)")
        ropts = parasitics.res_options(res_threshold_ohm, res_nets) if mode == "rc" else {}
        db = self.db(project, lib)
        design, ports, elements = [provenance.layout_entry(db, lib, cell)], None, None
        sch = prj.library(lib).path / f"{cell}.sch"
        if sch.is_file():
            res = xschem.SymbolResolver(prj.symbol_paths())
            ports = [p[0] for p in schematic.connectivity(sch, res, cell).ports]
            design.append(symbolcheck.design_entry(prj, lib, cell, "schematic-interface"))
            try:                                       # the names a testbench's @m.x1.xxm5... gives its devices
                elements = {inst: el for el, inst in schematic.spice(sch, res, self.model_env(prj), top_subckt=True,
                                                                     top_name=cell).instances.items()}
            except Exception:                          # a schematic the netlister refuses: Magic's device names stay
                elements = None
        inp = provenance.inputs(design, prj.pdk, ["magic"], None, {"mode": mode, "cthresh_ff": cthresh, **ropts})
        return self._run(project, "pex", "magic", lib, cell, None, inp, lambda db, m: parasitics.start(
            self.jobs, prj.path, prj.pdk, db.layout, cell, lib, mode, ports, cthresh, {**m, **(meta or {})}, elements,
            **ropts))

    def start_extraction(self, project: str, lib: str, cell: str, mode: str = "c", cthresh_ff: float | None = None,
                         meta: dict | None = None, res_threshold_ohm: float | None = None, res_nets: list[str] | None = None):
        return self.prepare_extraction(project, lib, cell, mode, cthresh_ff, meta, res_threshold_ohm, res_nets).launch()

    def extraction(self, project: str, lib: str, cell: str, want: str | None = None) -> dict:
        """The extraction a simulation of `lib/cell` as extracted uses: job `want`, or the newest finished one (of mode
        `want`: c | rc) that is up to date with the layout and the schematic's ports. ValueError, saying what to run,
        when there is none, or when its ports are not the schematic's."""
        prj = self.project(project)
        mode = want if want in parasitics.MODES else None
        run = f"run extract_parasitics(lib={lib!r}, cell={cell!r}, mode={mode or 'rc'!r})"
        runs = parasitics.jobs_of(self.jobs, prj.path, lib, cell)
        if want and mode is None:
            runs = [r for r in runs if r.get("id") == want]
            if not runs:
                raise ValueError(f"no parasitic extraction job {want} of {lib}/{cell}")
        done = [r for r in runs if r.get("state") == "done" and (mode is None or r.get("mode") == mode)]
        if not done:
            busy = [r["id"] for r in runs if r.get("state") in ("queued", "running") and (mode is None or r.get("mode") == mode)]
            raise ValueError(f"{lib}/{cell} has no finished parasitic extraction" + (f" of mode {mode}" if mode else "")
                             + (f": job {busy[0]} is still running (wait_for job:{busy[0]})" if busy else f": {run} first"))
        stale = None
        has_sch = (prj.library(lib).path / f"{cell}.sch").is_file()
        for st in done:
            fresh = provenance.freshness(self, project, st)
            if has_sch and not any(e.get("view") == "schematic-interface" for e in (st.get("inputs") or {}).get("design", [])):
                fresh = {"fresh": False, "changed": [f"{lib}/{cell} has a schematic since, whose ports it takes"]}
            if fresh["fresh"] is False:
                stale = stale or (st, fresh["changed"])
                continue
            ports = (st.get("summary") or {}).get("ports") or {}
            if not ports.get("match"):
                why = ([f"no layout label for {', '.join(ports['missing'])}; label them on their metal (add_label)"]
                       if ports.get("missing") else []) + \
                      [f"{s['port']} is shorted to {s['to']} (their labels are on one net); separate the two nets"
                       for s in ports.get("shorted") or []]
                raise ValueError(f"the extraction of {lib}/{cell} (job {st['id']}) does not have the schematic's ports: "
                                 f"{'; '.join(why) or 'they differ'}; then {run} again")
            return st
        st, changed = stale
        raise ValueError(f"the extraction of {lib}/{cell} (job {st['id']}, mode {st.get('mode')}) is outdated: "
                         f"{'; '.join(changed)}; {run} again")

    def extraction_sha(self, project: str, lib: str, cell: str, want: str | None = None) -> str | None:
        """Hash of the subcircuit a simulation would take for `lib/cell` now (extraction: `want` a job or a mode), None
        when there is no usable one (a job named: once it is outdated)."""
        try:
            return (self.extraction(project, lib, cell, want).get("files") or {}).get("extracted.spice")
        except (ValueError, KeyError):
            return None

    @staticmethod
    def _extracted_wants(extracted) -> dict[str, str | None]:
        """`extracted` of a simulation ("lib/cell" list, or {"lib/cell": job id | c | rc | None}) as {"lib/cell": want}."""
        if isinstance(extracted, str):
            extracted = [extracted]
        items = extracted.items() if isinstance(extracted, dict) else [(k, None) for k in extracted]
        out = {}
        for key, want in items:
            lib, _, cell = str(key).partition("/")
            if not lib or not cell:
                raise ValueError(f"extracted: {key!r} is not lib/cell")
            out[f"{lib}/{cell}"] = want or None
        return out

    def extracted_views(self, project: str, extracted) -> dict[str, dict]:
        """`extracted` of a simulation (_extracted_wants) as {"lib/cell": the extraction's status}."""
        return {k: self.extraction(project, *k.split("/", 1), want) for k, want in self._extracted_wants(extracted).items()}

    def start_drc(self, project: str, lib: str, cell: str, deck: str | None = None, options: dict | None = None):
        return self.prepare_drc(project, lib, cell, deck, options).launch()

    def start_magic_drc(self, project: str, lib: str, cell: str):
        return self.prepare_magic_drc(project, lib, cell).launch()

    def start_lvs(self, project: str, lib: str, cell: str, deck: str | None = None, options: dict | None = None,
                  schematic_cell: str | None = None, schematic_file: str | None = None):
        return self.prepare_lvs(project, lib, cell, deck, options, schematic_cell, schematic_file).launch()

    def start_netgen_lvs(self, project: str, lib: str, cell: str, schematic_cell: str | None = None,
                         schematic_file: str | None = None):
        return self.prepare_netgen_lvs(project, lib, cell, schematic_cell, schematic_file).launch()

    def start_symbol_check(self, project: str, lib: str, cell: str):
        check_name(cell)
        prj = self.project(project)
        prj.library(lib)
        # Capture both views before starting the worker, using the same locks as edits.
        with self.editor(project, lib, cell, "schematic").lock, self.editor(project, lib, cell, "symbol").lock:
            return symbolcheck.start(self.jobs, prj, lib, cell)

    def start_sim(self, project: str, lib: str, cell: str, analysis: dict | None = None, netlist: str | None = None,
                  meta: dict | None = None, measure: list[str] | None = None, *, overrides: dict | None = None,
                  corner: str | None = None, temp: float | None = None, reuse: bool = False,
                  extracted=None, save: list[str] | None = None, options: dict | None = None,
                  mismatch: int | None = None, timeout_s: float | None = None, substitute=None,
                  save_all: bool = False) -> sim.Job:
        """An ngspice job on testbench `lib/cell` (or on a given netlist). `measure`: .meas statements added to this run
        only (the testbench is not changed). For sweeps (specs.py): `overrides` ({cell: {instance: {property: value}}})
        netlists other values without touching the design, `corner` picks a section of the PDK model library, `temp`
        sets .temp, and `reuse` returns the job that already ran (or runs) on exactly these inputs instead of starting
        another. `extracted` (extracted_views) simulates those cells as extracted from their layouts: their latest
        up-to-date extraction (or the job named) replaces their schematics' subcircuits. A transient, and any run on
        an extracted view, stores the testbench's own nets, what its .meas and .control lines name and `save` (signals
        the caller reads), not the nodes inside its cells (sim.save_cards): a mixed-signal transient of every node is
        hundreds of megabytes, and the changeset's evidence keeps every run. `save_all` stores every node instead (the
        netlist's own .save or .options savecurrents decide by themselves). `options`: simulator options
        (sim.sim_options: reltol, method, maxstep ...). `mismatch`: a Monte Carlo run with device mismatch on (the PDK's mismatch models,
        pdkprofile.Profile.mismatch_section / mismatch_param), its random numbers drawn from this seed. `timeout_s`:
        the run's time limit (sim.time_limit). `substitute` ({"lib/cell": its stand-in}, sim.substitute_map) netlists
        every instance of those cells, at any depth, as their stand-ins (behavioural models with the same pins), over
        the testbench's own default (substitutes; null takes one back): the job says which did (`substitute`, and in
        the testbench's netlist entry, so a run with stand-ins is never reused for one without) and warns of one that
        replaced nothing (`substitute_warnings`). The PDK's standard cells a testbench calls get their library's SPICE
        (_cell_spice). The PDK's model library is read as a copy reduced to what the netlist uses (modellib.py); the
        inputs name the library and corner as the netlist does."""
        prj = self.project(project)
        for m in measure or []:
            if not isinstance(m, str) or "\n" in m or not re.match(r"\s*\.meas(ure)?\s", m, re.I):
                raise ValueError(f"a measure is one .meas statement on one line, not {m!r}")
        options = sim.sim_options(options) if options else {}
        if options.get("maxstep") and analysis:
            if analysis.get("type", "tran") != "tran":
                raise ValueError(f"maxstep applies to a transient, not to a {analysis.get('type')} analysis")
            # the option is the transient's tmax: it replaces one the analysis gives (a sweep over options.maxstep)
            analysis = {**{k: v for k, v in analysis.items() if k != "tmax"}, "maxstep": options["maxstep"]}
        wants = self._extracted_wants(extracted) if extracted else {}
        if netlist is None:
            subst, origin, unused = self.substitutes(project, lib, cell, substitute)
        elif any(sim.substitute_map(substitute).values()):
            raise ValueError("substitute applies to a testbench netlisted from its schematic, not to a given netlist")
        else:
            subst, origin, unused = {}, {}, []

        def whence(k: str) -> str:                  # where a stand-in comes from, in words
            return "substitute" + (f": the testbench's default, its intent's {origin[k]}" if origin[k] != "run" else "")
        if (both := sorted(set(wants) & set(subst))):
            raise ValueError(f"{both[0]} is simulated both as extracted and as its stand-in {subst[both[0]]} "
                             f"({whence(both[0])}): one or the other; substitute {{\"{both[0]}\": null}} simulates it "
                             "as itself")
        views = self.extracted_views(project, extracted) if extracted else {}
        if views and netlist is not None:
            raise ValueError("extracted applies to a testbench netlisted from its schematic, not to a given netlist")
        ext_entries, in_effect = [], {}
        if netlist is None:
            subs = {}
            for key, st in views.items():
                vlib, _, vcell = key.partition("/")
                subs[key] = (prj.library(vlib).path / f"{vcell}.sch",
                             (self.jobs.get(prj.path, st["id"]).path / "extracted.spice").read_text())
                # a job named stays this job: the entry is outdated with it, not when a newer extraction comes
                ext_entries.append({"view": "extraction", "lib": vlib, "cell": vcell, "mode": st.get("mode"),
                                    **({"job": st["id"]} if wants.get(key) not in (None, *parasitics.MODES) else {}),
                                    "sha": (st.get("files") or {}).get("extracted.spice")})
            r = self.sim_netlist(prj, lib, cell, overrides or None, subs, subst or None)
            # what ngspice would run of an untrusted dependency (.control, *#, a file outside the PDK): refused before
            # any job is looked up or made. A netlist given as text is the caller's own
            trust.check_netlist(r, untrusted=trust.untrusted_dirs(prj.path), pdk_root=prj.pdk.root if prj.pdk else None,
                                env=self.model_env(prj), rules="sim")
            text, warns = r.text, r.warnings
            in_effect = {k: v for k, v in subst.items() if k in r.substituted}
            unused += [f"substitute {k} -> {v}: {k} is not netlisted in {lib}/{cell} (not placed in it or below it, or "
                       "only inside a cell simulated as extracted or as another stand-in): nothing was replaced"
                       for k, v in subst.items() if k not in in_effect]
            # the design is the testbench as drawn, with its stand-ins (the substitutions asked are part of the entry,
            # so a run with them is never taken for one without); what a sweep changes is in the options (and the
            # extracted views in their own entries), so freshness still works
            sha = provenance.text_sha(text) if not (overrides or views) else \
                self.netlist_sha(project, lib, cell, cell, "sim", substitute=subst or None)
            entry = {"view": "netlist", "lib": lib, "cell": cell, "top": cell, "mode": "sim",
                     **({"substitute": subst} if subst else {}), "sha": sha}
        else:
            text, warns = netlist, []
            entry = {"view": "text", "sha": provenance.text_sha(text)}
        notes = [f"{k} is simulated as extracted from its layout (mode {st.get('mode')}, job {st['id']}): "
                 f"{parasitics.CONTENTS.get(st.get('mode'), '')}" for k, st in views.items()]
        notes += [f"every instance of {k}, at any depth, was netlisted as {v} ({whence(k)})"
                  for k, v in in_effect.items()]
        if netlist is None:
            # the same models from a corner symbol and a code block: ngspice must not read them twice
            seen, kept = set(), []
            for line in text.split("\n"):
                key = line.strip().lower()
                if re.match(r"\.(lib|include|inc)\b", key) and key in seen:
                    notes.append(f"a repeated model statement was dropped: {line.strip()}")
                    continue
                seen.add(key)
                kept.append(line)
            text = "\n".join(kept)
            if (cells := _cell_spice(text, prj.pdk)):
                first, _, rest = text.partition("\n")
                text = f"{first}\n" + "".join(f".include {f}\n" for f in cells) + rest
                notes.append("the PDK's standard cells the testbench uses were included from "
                             + ", ".join(Path(f).name for f in cells))
        profile = prj.pdk.profile if prj.pdk else None
        # the testbench's model statements: a standard-cell library's SPICE has subcircuits, not models
        models = [m for m in re.findall(r"^[ \t]*\.(?:lib|include|inc)\b[^\n]*", text, re.M | re.I)
                  if not _is_cell_spice(m, prj.pdk)]
        if corner is not None:
            if not profile or not profile.model_lib or corner not in profile.corners:
                raise ValueError(f"no corner {corner!r} in this PDK" + (f" (corners: {', '.join(profile.corners)})" if profile and profile.corners else ""))
            text, n = _set_corner(text, prj.pdk.root / profile.model_lib, corner)
            if n == 0 and models:
                raise ValueError("the testbench reads its models from files other than the PDK's model library, so a corner "
                                 "cannot be chosen; " + (f"use the PDK corner symbol ({profile.corner_symbol}) instead"
                                                         if profile.corner_symbol else
                                                         "remove its model statements and Undine includes the PDK models"))
        if netlist is None and profile and profile.model_lib and not models:
            # a testbench without any model statement would fail on the first PDK device ("unknown subckt")
            c = corner or (profile.corners[0] if profile.corners else "tt")
            line = "".join(f".include {prj.pdk.root / f}\n" for f in profile.model_includes) + \
                f".lib {prj.pdk.root / profile.model_lib} {c}"
            first, _, rest = text.partition("\n")
            text = f"{first}\n{line}\n{rest}"
            if corner is None:
                notes.append(f"the testbench has no model statement, so the PDK models were included ({c} corner); " +
                             (f"place the PDK's corner symbol ({profile.corner_symbol}) to choose another corner"
                              if profile.corner_symbol else
                              f"check_specs / sweep run it at the other corners ({', '.join(profile.corners[1:])})"))
        if temp is not None:
            if re.search(r"^\s*\.options?\b.*\btemp\s*=", text, re.M | re.I):
                raise ValueError("the testbench sets the temperature in an .option line; remove it to choose a temperature")
            text = re.sub(r"^[ \t]*\.temp\b[^\n]*\n?", "", text, flags=re.M | re.I)
            first, _, rest = text.partition("\n")
            text = f"{first}\n.temp {temp:g}\n{rest}"
        if measure:
            lines = "\n".join(m.strip() for m in measure)
            text = text.replace("\n.end\n", f"\n{lines}\n.end\n") if "\n.end\n" in text else f"{text}\n{lines}\n"
        if (bad := intentmod.measured_levels([ln for ln in text.split("\n") if re.match(r"\s*\.meas(ure)?\s", ln, re.I)])):
            raise ValueError(bad[0])
        if mismatch is not None:
            text, note = self._mismatch(prj, text, int(mismatch))
            notes.append(note)
        if options:
            text = sim.with_options(text, {k: v for k, v in options.items() if not (k == "maxstep" and analysis)})
        saved = None                    # "testbench": the nodes inside its cells are not stored
        # a transient: the analysis given (its type defaults to tran), or the testbench's own
        tran = analysis.get("type", "tran") == "tran" if analysis else sim.is_transient(text)
        if not save_all and (views or tran) and (lines := sim.save_cards(text, save)):
            text = text.replace("\n.end\n", f"\n{lines}\n.end\n") if "\n.end\n" in text else f"{text}\n{lines}\n"
            saved = "testbench"
            notes.append("only the testbench's nets, what its .meas lines name and the signals asked for are stored, not "
                         "the nodes inside its cells" + (" (those simulated as extracted too)" if views else "")
                         + ": name one in signals to keep it, or save_all to store every node")
        if views and (bad := parasitics.unknown_nodes(text, {k.partition("/")[2].lower() for k in views})):
            raise ValueError(self._unknown_nodes(views, bad))
        options = {**({"analysis": analysis} if analysis else {}), **({"measure": measure} if measure else {}),
                   **({"overrides": overrides} if overrides else {}), **({"corner": corner} if corner is not None else {}),
                   **({"temp": temp} if temp is not None else {}),
                   **({"sim_options": options} if options else {}),
                   **({"mismatch_seed": int(mismatch)} if mismatch is not None else {}),
                   **({"extracted": {k: st.get("mode") for k, st in views.items()}} if views else {}),
                   **({"save": sorted(set(save))} if saved and save else {}),
                   **({"save": "all"} if save_all else {})}
        inp = provenance.inputs([entry, *ext_entries], prj.pdk, ["ngspice"], None, options)
        if reuse and (job := self.reusable(project, inp)) is not None:
            return job
        used = [{"cell": k, "mode": st.get("mode"), "job": st["id"]} for k, st in views.items()]
        return self.jobs.submit(prj.path, f"{lib}/{cell}", text, analysis,
                                {"kind": "sim", "lib": lib, "cell": cell, "netlist_warnings": warns, **(meta or {}),
                                 **({"notes": notes} if notes else {}), **({"extracted": used} if used else {}),
                                 **({"substitute": in_effect} if in_effect else {}),
                                 **({"substitute_warnings": unused} if unused else {}),
                                 **({"saved": saved} if saved else {}),
                                 # who started it (cancel_job); one that reads this changeset is not its writer
                                 # (reader: mcp.Sessions._seed)
                                 **live.started_by(),
                                 **provenance.meta(inp)}, self._osdi(prj),
                                timeout_s=timeout_s,
                                models=(prj.pdk.root / profile.model_lib, prj.pdk.id) if profile and profile.model_lib else None)

    @staticmethod
    def _unknown_nodes(views: dict[str, dict], bad: dict[str, list[str]]) -> str:
        """Why a run on extracted views is refused: the nodes it names inside them that they do not have (ngspice
        would take each for a new, floating node), the devices whose quantities it asks for (@m.x1.xxm5..[l]: ngspice
        has no such device and evaluates nothing), and where their names come from."""
        def few(refs: list[str]) -> str:
            return ", ".join(refs[:12]) + (f" and {len(refs) - 12} more" if len(refs) > 12 else "")
        out = []
        for key, st in views.items():
            refs = bad.get(key.partition("/")[2].lower())
            if not refs:
                continue
            names = (st.get("summary") or {}).get("names") or {}
            at = f"the extracted view of {key} (job {st['id']}, mode {st.get('mode')})"
            if (nodes := [r for r in refs if not r.startswith("@")]):
                why = (f"its nets without a label are named after the schematic (LVS job {names['from_lvs']}), except "
                       + (", ".join(names["unnamed"]) + " (no contact to label)" if names.get("unnamed") else
                          "those LVS did not match") if names.get("from_lvs") else
                       "its nets without a label keep Magic's names (a_3109_5296#): "
                       + (names.get("why") or "an extraction made before Undine named them"))
                out.append(f"{few(nodes)}: no such node in {at}; {why}")
            if (devs := [r for r in refs if r.startswith("@")]):
                kept = names.get("devices_kept")
                why = (f"its devices take the schematic's element names (LVS job {names['from_lvs']})"
                       + (f", except {', '.join(kept[:12])} (not matched to one element)" if kept else "")
                       + (f"; the elements Magic split take _2, _3 ..: {', '.join(names['devices_split'])}"
                          if names.get("devices_split") else "")
                       if names.get("from_lvs") and isinstance(kept, list) else
                       "its devices keep Magic's names (X0, X1 ..): "
                       + (kept or names.get("why") or "an extraction made before Undine named them"))
                out.append(f"{few(devs)}: no such device in {at}; {why}")
        return ("the testbench refers to nodes or devices that the extracted views do not have (ngspice would add a "
                "node as a new, floating one, and evaluates nothing for a device): " + "; ".join(out) + ". Run LVS "
                "of the layout (run_checks) and extract_parasitics again so that its nets and devices take the "
                "schematic's names, label the nets in the layout, or name the extraction's own nodes and devices "
                "(get_parasitics lists its nets, and the devices that kept Magic's names in names.devices_kept)")

    @staticmethod
    def _osdi(prj) -> list[Path]:
        """The PDK's compiled Verilog-A models, which ngspice must load; refuses to start without them."""
        if not prj.pdk or not prj.pdk.profile.osdi:
            return []
        files = prj.pdk.osdi_files()
        missing = [f.name for f in files if not f.is_file()]
        if missing:
            raise ValueError(f"{prj.pdk.name}'s Verilog-A models are not compiled ({', '.join(missing)}): "
                             "run `./setup.sh osdi` (OpenVAF), then simulate again")
        return files

    @staticmethod
    def _mismatch(prj, text: str, seed: int) -> tuple[str, str]:
        """The netlist with the PDK's device mismatch on (its corner's mismatch section of the model library, or the
        .param that turns it on) and ngspice's random numbers drawn from `seed`; and a note saying so."""
        if not 1 <= seed <= 2 ** 31 - 1:         # ngspice skips any other seed (a warning) and draws from the time
            raise ValueError(f"mismatch seed {seed}: ngspice takes 1 .. {2 ** 31 - 1}")
        profile = prj.pdk.profile if prj.pdk else None
        if not profile or not (profile.mismatch_section or profile.mismatch_param):
            raise ValueError(f"{prj.pdk.name if prj.pdk else 'this project'} declares no mismatch models (pdkprofile.py: "
                             "mismatch_section or mismatch_param), so mismatch cannot be simulated")
        if profile.mismatch_section:
            model_lib = prj.pdk.root / profile.model_lib
            pat = re.compile(r"^([ \t]*\.lib[ \t]+)(['\"]?)(\S+?)\2([ \t]+)(\S+)[ \t]*$", re.M | re.I)
            sections = set(re.findall(r"^\s*\.lib\s+(\S+)\s*$", model_lib.read_text(errors="replace"), re.M | re.I))
            used = []

            def repl(m):
                if Path(m.group(3)) != model_lib:
                    return m.group(0)
                corner = m.group(5)
                mm = corner if corner not in profile.corners and corner in sections else \
                    profile.mismatch_section.format(corner=corner)
                if mm not in sections:
                    raise ValueError(f"the model library has no mismatch section {mm} for corner {corner}")
                used.append(mm)
                return f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(2)}{m.group(4)}{mm}"
            text = pat.sub(repl, text)
            if not used:
                raise ValueError("the testbench reads its models from files other than the PDK's model library, so its "
                                 "mismatch models cannot be chosen")
            how = f"model section {', '.join(dict.fromkeys(used))}"
        else:
            how = f".param {profile.mismatch_param}"
        lines = (f".param {profile.mismatch_param}\n" if profile.mismatch_param and not profile.mismatch_section else "") + \
            f".option seed={seed}"
        text = text.replace("\n.end\n", f"\n{lines}\n.end\n") if "\n.end\n" in text else f"{text}\n{lines}\n"
        return text, f"device mismatch on ({how}), random numbers from seed {seed}"

    def reusable(self, project: str, inputs: dict) -> sim.Job | None:
        """The newest job that ran (or is running) on exactly these inputs, among all of the project's runs; not one an
        earlier server process left queued or running (sim.Jobs.find skips those: nothing finishes them)."""
        return self.jobs.find(self.project(project).path, provenance.key(inputs))

    def suite(self, project: str, lib: str, cell: str, target: str | None = None) -> list[tuple[str, "Run | Exception"]]:
        """The sign-off checks of a cell: every KLayout DRC deck, Magic DRC, and LVS with each tool that is available.
        With a target, LVS uses that target's substrate net (Tiny Tapeout: VGND)."""
        prj = self._pdk_of(project)
        lvs_opts = dict(tapeout.TARGETS.get(target, {}).get("lvs_options", {})) if target else {}
        d = verify.decks(prj.pdk)
        out: list[tuple[str, Run | Exception]] = []

        def add(name: str, fn):
            try:
                out.append((name, fn()))
            except Exception as e:
                out.append((name, e))
        for deck in d["drc"] + d["extra"]:
            add(f"KLayout {deck}", lambda deck=deck: self.prepare_drc(project, lib, cell, deck))
        if d["drc"]:                    # a target's runs of the main deck with other switches (wafer.space: density, antenna)
            for name, opts in tapeout.TARGETS.get(target, {}).get("drc_runs", {}).items() if target else ():
                add(f"KLayout {name}", lambda opts=opts: self.prepare_drc(project, lib, cell, d["drc"][0], opts))
        if d["magic"]:
            add("Magic DRC", lambda: self.prepare_magic_drc(project, lib, cell))
        if (prj.library(lib).path / f"{cell}.sch").is_file():
            if d["lvs"]:
                add("KLayout LVS", lambda: self.prepare_lvs(project, lib, cell, options=lvs_opts))
            if d["magic"] and d["netgen"]:
                add("Magic + Netgen LVS", lambda: self.prepare_netgen_lvs(project, lib, cell))
        else:
            out.append(("LVS", KeyError(f"no schematic {lib}/{cell}: LVS needs a reference netlist")))
        return out

    def signoff(self, project: str, lib: str, cell: str, target: str | None = None) -> list[dict]:
        """For each sign-off check: the job that ran on the design as it is now, or else the latest one and what has
        changed since, or nothing."""
        jobs = self.jobs.list(self.project(project).path)
        out = []
        for name, run in self.suite(project, lib, cell, target):
            if isinstance(run, Exception):
                out.append({"name": name, "error": str(run).strip("'\"")})
                continue
            item = {"name": name, "kind": run.kind, "tool": run.tool, "deck": run.deck, "key": provenance.key(run.inputs)}
            same = next((j for j in jobs if j.get("key") == item["key"]), None)     # newest first; a failed run counts too
            prev = same or next((j for j in jobs if j.get("kind") == run.kind and j.get("tool", "klayout") == run.tool
                                 and j.get("lib") == lib and j.get("cell") == cell
                                 and (run.deck is None or j.get("deck") == run.deck)
                                 # a target's density / antenna runs of the same deck are other checks
                                 and ((j.get("inputs") or {}).get("options") or {}).get("decks") == run.inputs["options"].get("decks")),
                                None)
            if prev:
                if same:
                    changed = []
                elif prev.get("inputs"):
                    changed = provenance.diff(prev["inputs"], run.inputs) or ["ran with other settings"]
                else:
                    changed = ["ran before inputs were recorded"]
                item.update(job=prev["id"], state=prev["state"], fresh=bool(same), changed=changed,
                            summary=prev.get("summary"), passed=_passed(prev))
            out.append(item)
        return out

    def oplog(self, project: str, lib: str) -> OpLog:
        p = self.project(project).path / "history" / f"{lib}.jsonl"
        with self.lock:
            if p not in self.logs:
                parts = project.split("~")
                hooks, floor = None, 0
                if len(parts) > 1:                   # a preview: its log starts as the project's was (oplog.py)
                    pv = self.preview(project)
                    floor = pv.base_seq.get(lib, 0)
                if len(parts) == 2:                  # a changeset's working copy (not its baseline)
                    lock = self._cs_locks.setdefault(project, threading.RLock())
                    hooks = live.LogHooks(self.leases, parts[1], p.parent, lock, pv.base_seq, pv.floor_dir)
                elif len(parts) == 3:                # its baseline: never written, whatever edits it
                    hooks = _ReadOnlyLog()
                self.logs[p] = OpLog(p, hooks, floor)
            return self.logs[p]

    def editor(self, project: str, lib: str, cell: str, view: str) -> SchEditor:
        if view == "intent":
            return self.intent_editor(project, lib, cell)
        if view == "netlist":
            return self.netlist_editor(project, lib, cell)
        if view == "rtl":
            return self.rtl_editor(project, lib, cell)
        if view == "flow":
            return self.flow_editor(project, lib, cell)
        prj = self.project(project)
        p = prj.library(lib).path / f"{cell}.{'sch' if view == 'schematic' else 'sym'}"
        with self.lock:
            if p not in self.editors:
                self.editors[p] = SchEditor(p, xschem.SymbolResolver(prj.symbol_paths()), self.oplog(project, lib))
            ed = self.editors[p]
            ed.resolver = xschem.SymbolResolver(prj.symbol_paths())
            return ed

    def watch_netlists(self, interval: float = 1.0) -> None:
        """Starts reading schematics changed outside Undine back into their netlists as they change (netlist.Watcher)."""
        if getattr(self, "_netlist_watch", None) is None:
            self._netlist_watch = netlistmod.Watcher(self, interval)
            self._netlist_watch.start()

    def netlist_editor(self, project: str, lib: str, cell: str) -> netlistmod.NetlistEditor:
        check_name(cell)
        prj = self.project(project)
        sch = prj.library(lib).path / f"{cell}.sch"
        key = netlistmod.path_of(sch)
        with self.lock:
            if key not in self.editors:
                self.editors[key] = netlistmod.NetlistEditor(sch, xschem.SymbolResolver(prj.symbol_paths()), self.oplog(project, lib))
            ed = self.editors[key]
            ed.resolver = xschem.SymbolResolver(prj.symbol_paths())
            return ed

    def netlist_view(self, project: str, lib: str, cell: str) -> dict:
        """The cell's netlist (its connectivity) with what the drawing does not show yet."""
        ed = self.netlist_editor(project, lib, cell)
        library = self.project(project).library(lib)
        model = netlistmod.current(ed.sch, ed.resolver, write=not library.readonly)
        supplies = netlistmod.supplies(ed.sch, ed.resolver, model)       # standard cells' VPWR / VGND ... parameters
        return {"netlist": model, "pending": [{"pin": k, "net": v} for k, v in model["pending"].items()],
                "supplies": supplies, "warnings": netlistmod.supply_warnings(model, supplies, ed.sch),
                "undo": ed.can_undo_redo(), "readonly": library.readonly}

    def intent_editor(self, project: str, lib: str, cell: str) -> intentmod.IntentEditor:
        check_name(cell)
        p = intentmod.path_of(self.project(project).library(lib).path, cell)
        with self.lock:
            if p not in self.editors:
                self.editors[p] = intentmod.IntentEditor(p, self.oplog(project, lib))
            return self.editors[p]

    def rtl_editor(self, project: str, lib: str, cell: str) -> rtl.RtlEditor:
        check_name(cell)
        lib_dir = self.project(project).library(lib).path
        key = lib_dir / f"{cell}.rtl"                     # one editor per cell, whichever suffix its file has
        with self.lock:
            if key not in self.editors:
                self.editors[key] = rtl.RtlEditor(lib_dir, cell, self.oplog(project, lib))
            return self.editors[key]

    def flow_editor(self, project: str, lib: str, cell: str) -> librelane.FlowEditor:
        """The flow configurations whose top cell is lib/cell, recorded in lib's log (librelane.FlowEditor)."""
        check_name(cell)
        prj = self.project(project)
        prj.library(lib)                                  # KeyError: no such library
        key = prj.path / librelane.FLOWS_DIR / f".{lib}.{cell}.flow"   # one editor per top cell (not a file)
        with self.lock:
            if key not in self.editors:
                self.editors[key] = librelane.FlowEditor(prj.path, cell, self.oplog(project, lib))
            return self.editors[key]

    def design_refs(self, project: str, lib: str, cell: str, model: dict | None = None) -> dict | None:
        """What an intent may refer to: the schematic's instances, nets and ports, and the library's cells; with the
        intent `model`, the measures the testbenches of its spectral specs give themselves (intent.testbench_measures:
        such a spec needs no spectrum)."""
        out = self._design_refs(project, lib, cell)
        if out is not None and model is not None:
            out["measures"] = self._testbench_measures(project, lib, model)
        return out

    def _testbench_measures(self, project: str, lib: str, model: dict) -> dict[str, dict]:
        """{testbench as a spec names it: the measures its schematic's code blocks give (intentmod.testbench_measures)}
        of the spectral specs without a spectrum."""
        from undine.simulation import spectrum
        out: dict[str, dict] = {}
        prj = self.project(project)
        for _view, it in intentmod.all_items(intentmod.normalize(model)):
            spec = it.get("spec") if it.get("kind") == "spec" else None
            if not isinstance(spec, dict) or spec.get("spectrum") or not spec.get("testbench") or \
                    str(spec.get("measure") or "").lower() not in spectrum.FIGURES or spec["testbench"] in out:
                continue
            tlib, _, tcell = str(spec["testbench"]).rpartition("/")
            try:
                f = prj.library(tlib or lib).path / f"{tcell}.sch"
                out[spec["testbench"]] = intentmod.testbench_measures(_code_text(f.read_text(errors="replace")))
            except (OSError, KeyError, ValueError):
                out[spec["testbench"]] = {}
        return out

    def _design_refs(self, project: str, lib: str, cell: str) -> dict | None:
        prj = self.project(project)
        library = prj.library(lib)
        sch = library.path / f"{cell}.sch"
        cells = [c["name"] for c in library.cells()]
        if not sch.is_file():
            src = rtl.find_file(library.path, cell)
            if src is None:
                return None
            ports = [n for n, _ in rtl.module_ports(src.read_text(errors="replace"), cell)]   # an RTL cell: its module's ports
            return {"instances": [], "nets": ports, "ports": ports, "cells": cells}
        conn = schematic.connectivity(sch, xschem.SymbolResolver(prj.symbol_paths()))
        return {"instances": [d.name for d in conn.devices], "nets": [n for n in conn.nets if not n.startswith("net")],
                "ports": [p[0] if isinstance(p, (tuple, list)) else p for p in conn.ports], "cells": cells}

    def intent_view(self, project: str, lib: str, cell: str) -> dict:
        ed = self.intent_editor(project, lib, cell)
        model = ed.model()
        refs = self.design_refs(project, lib, cell, model)
        yaml_text = _intent_yaml(model)
        return {"intent": model, "yaml": yaml_text, "sha": oplog.sha(yaml_text),
                "problems": intentmod.validate(model, refs), "requirements": intentmod.requirements(model, refs),
                "steps": intentmod.STEPS, "refs": refs, "suggested_supplies": intentmod.suggested_supplies(refs) if refs else {},
                "undo": ed.can_undo_redo(), "readonly": self.project(project).library(lib).readonly or project.endswith("~base")}

    def symbols(self, project: str) -> list[dict]:
        """Symbols available for placement: [{lib, name, ref}] (the project's libraries, then Xschem's devices). A
        dependency's cells outside its package's [exports] are left out: internal, they still resolve and netlist,
        and placing one by name is allowed (Library.internal)."""
        prj = self.project(project)
        out, seen = [], set()
        dirs = [(l.name, l.path, l) for l in prj.libraries().values()]
        xl = tools.xschem_library()
        if xl and (xl / "devices").is_dir():
            dirs.append(("devices", xl / "devices", None))
        for name, d, lib in dirs:
            if d in seen or not d.is_dir():
                continue
            seen.add(d)
            for f in sorted(d.glob("*.sym")):
                if lib is None or not lib.internal(f.stem):
                    out.append({"lib": name, "name": f.stem, "ref": f"{name}/{f.name}"})
        return out

    def model_env(self, prj) -> dict[str, str]:
        """Variables Xschem schematics refer to ($::SKYWATER_MODELS ...)."""
        pdk = prj.pdk
        env: dict[str, str] = {}
        if pdk:
            env["PDK_ROOT"] = str(pdk.root.parent)
            env["PDK"] = pdk.name
            for name, rel in pdk.profile.model_vars:
                env[name] = str(pdk.root / rel)
            if pdk.profile.model_lib:          # read by the netlister for corner symbols (schematic.spice)
                env["UNDINE_MODEL_LIB"] = str(pdk.root / pdk.profile.model_lib)
                env["UNDINE_CORNERS"] = " ".join(pdk.profile.corners)
        return env

    def db(self, project: str, lib: str) -> LayoutDB:
        """The library's open layout. Its cell files may have changed on disk since it read them (git checkout,
        merge, pull): it looks first (at most once a second), and reads them again, or, under unsaved edits, notes
        the conflict that its save refuses (LayoutDB.check_disk)."""
        db = self._db(project, lib)
        db.poll_disk()
        return db

    def _db(self, project: str, lib: str) -> LayoutDB:
        key = (project, lib)
        with self.lock:
            if key not in self.dbs:
                prj = self.project(project)
                db = LayoutDB(prj.library(lib), prj.pdk, self.oplog(project, lib))
                # without looking at the disk: a layout replaying its journal holds its lock, and looking takes that one's
                db.lib_resolver = lambda name, _p=project: self._db(_p, name)
                self.dbs[key] = db
            return self.dbs[key]

    def rescan_pdks(self) -> list[dict]:
        """Finds the PDKs under pdk_roots again (one fetched or installed since the server started), with no restart.
        The scan runs without the server's lock. Then, under it, the shared dict changes with no moment in which a
        reader finds it empty or missing an id that stays: new and changed ids are set first, removed ones popped
        after (readers iterate a copy, pick_pdk). A PDK whose root and what the scan derives from it (layer properties,
        xschem and KLayout directories, source) are unchanged keeps its object; one first seen half extracted (a scan
        during another fetch) is replaced once its files are there. The open layouts and
        editors of a project whose PDK now resolves to another object (or none, or one at last) are dropped, as
        set-pdk drops them; LayoutDB is built under the same lock (_db), so none opened before the swap stays.
        Returns the PDKs as GET /api/pdks does."""
        with self._rescanning:
            found = discover(self.pdk_roots)
            old = dict(self.pdks)
            changed = {pid: p for pid, p in found.items() if pid not in old or _scanned(old[pid]) != _scanned(p)}
            removed = [pid for pid in old if pid not in found]
            netlistmod.protect(*(p.root for p in changed.values()))     # before any request can open them
            future = {**old, **changed}
            for pid in removed:
                future.pop(pid)
            with self.lock:
                moved = self._pdk_moved(old, future)
                for pid, p in changed.items():
                    self.pdks[pid] = p
                for pid in removed:
                    self.pdks.pop(pid, None)
                self._drop_open(moved, [old[pid].root for pid in [*removed, *changed] if pid in old])
                self.netlist_shas.clear()
        return [p.public() for p in list(self.pdks.values())]

    def _pdk_moved(self, old: dict[str, Pdk], new: dict[str, Pdk]) -> dict[str, Path]:
        """{name: path} of the open projects (a layout or an editor open) whose PDK resolves differently in `new`."""
        names = {k[0] for k in self.dbs}
        for k in self.editors:                     # an editor of a project's own library: <root>/<project>/...
            if isinstance(k, Path) and k.is_relative_to(self.store.root):
                names.add(k.relative_to(self.store.root).parts[0])
        out = {}
        for name in sorted(names):
            try:
                prj = self.project(name)
            except Exception:                      # not a project (.changesets/), gone, or unreadable: nothing to move
                continue
            spec = prj.meta.get("pdk")
            if spec and pick_pdk(old, spec) is not pick_pdk(new, spec):
                out[name] = prj.path
        return out

    def _drop_open(self, projects: dict[str, Path], pdk_roots: list[Path]) -> None:
        """Drops the layouts of these projects ({name: path}) and the editors of files under them or under these PDK
        roots; they open again on the next request, with the PDK as it resolves now."""
        for k in [k for k in self.dbs if k[0] in projects]:
            del self.dbs[k]
        under = [*projects.values(), *pdk_roots]
        for k in [k for k in self.editors if isinstance(k, Path) and any(k.is_relative_to(r) for r in under)]:
            del self.editors[k]

    @staticmethod
    def changeset_of(project: str) -> str | None:
        """Edits made in a changeset's preview project (<project>~<id>) are tagged with its id."""
        return project.split("~", 1)[1] if "~" in project else None


def _code_text(sch: str) -> str:
    """The SPICE a schematic's text gives besides its devices: its S block and its instances' value attributes (a code
    block's lines; a device's value, 1k, is no statement), each from the start of a line, in the file's order."""
    f = xschem.parse(sch)
    return "\n".join([f.code.get("S") or ""] + [str(c.props.get("value") or "") for c in f.comps])


def _check_images(images):
    import base64

    if len(images) > llm.MAX_IMAGES:
        raise llm.LlmError(f"at most {llm.MAX_IMAGES} images per message")
    for i in images:
        if i.media_type not in llm.IMAGE_TYPES:
            raise llm.LlmError(f"image type {i.media_type} is not supported (png, jpeg, webp)")
        try:
            raw = base64.b64decode(i.data, validate=True)
        except Exception as e:
            raise llm.LlmError("image data is not valid base64") from e
        if len(raw) > llm.MAX_IMAGE_BYTES:
            raise llm.LlmError("image is too large (max 3 MB)")
    return images


def _sha(i) -> str:
    import base64
    import hashlib

    return hashlib.sha1(base64.b64decode(i.data)).hexdigest()


def _has(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def _json(obj) -> Response:
    """Large JSON answers: orjson (Rust) straight to bytes. Returning a dict makes FastAPI walk it with jsonable_encoder
    first, which took longer than building it (0.6 s for a 9k-instance schematic view)."""
    return Response(orjson.dumps(obj, option=orjson.OPT_NON_STR_KEYS | orjson.OPT_SERIALIZE_NUMPY), media_type="application/json")


def _intent_yaml(model: dict) -> str:
    """The intent as the panel's YAML view shows it ("" for none)."""
    return intentmod.dump(model) if not intentmod.is_empty(model) else ""


class _ReadOnlyLog:
    """The hooks of a changeset baseline's logs (`<project>~<id>~base`, changeset.Baseline): every edit is refused
    before it changes anything (the editors' guard), and nothing is ever appended."""

    def __init__(self):
        self.lock = threading.RLock()

    def guard(self) -> None:
        raise PermissionError("the proposal baseline is read-only")

    def stamp(self) -> dict:
        raise PermissionError("the proposal baseline is read-only")


class StaleEdit(ValueError):
    """A whole text sent back (an RTL save, the intent's YAML) was loaded from a file that changed since: a git merge,
    checkout or pull, or another writer. Saving it would undo that change without a word."""


class HasCommits(ValueError):
    """The project's first commit asked of a repository whose branch has a commit already (POST .../git/first-commit:
    409 has_commits)."""


def _untrusted_note(srv: "Server", prj, r: schematic.NetlistResult) -> list[str]:
    """A netlist shown (not run) warns of what a simulation of it would refuse; it never fails for that."""
    try:
        trust.check_netlist(r, untrusted=trust.untrusted_dirs(prj.path), pdk_root=prj.pdk.root if prj.pdk else None,
                            env=srv.model_env(prj), rules="sim")
    except trust.UntrustedContent as e:
        return [f"not trusted: would be refused in a simulation: {e}"]
    except Exception:                                 # a display must not fail on the gate's own trouble
        return []
    return []


def _trust_findings(local: str, scan: dict) -> list[dict]:
    """trust.scan_dir's listing of dependency `local` as rows {content (spice, rtl or tcl), cell, file, where, kind,
    line, text}, with cell and file as '<local>/...' (as an 'untrusted' refusal names them)."""
    out = []
    for content, group in (("spice", scan["cells"]), ("rtl", scan["rtl"]), ("tcl", scan["tcl"])):
        for name, found in group.items():
            for f in found:
                file = name if content != "spice" else (f.where or name).split(" ", 1)[0]
                cell = None if content == "rtl" else (name if content == "spice" else trust._cell_of(name))
                out.append({"content": content, "cell": f"{local}/{cell}" if cell else None,
                            "file": f"{local}/{file}", "where": f.where, "kind": f.kind, "line": f.line,
                            "text": f.text})
    return out


def _trust_row(d: Path, trusted: set[str]) -> dict:
    """One installed dependency for the trust listing: {name, package, version, key, trusted, findings}."""
    key, _, package = trust._identity(d)
    return {"name": d.name, "package": package, "version": key.partition("@")[2] if package else None, "key": key,
            "trusted": key in trusted, "findings": _trust_findings(d.name, trust.scan_dir(d))}


def _err(e: Exception) -> HTTPException:
    if isinstance(e, changesetmod.MergeConflict):       # nothing written: each file and its conflicts (the body's "files")
        err = HTTPException(409, str(e))
        err.code = "merge_conflict"
        err.files = e.files
        return err
    if isinstance(e, changesetmod.NotSetUp):            # opening a changeset in a repository not set up for Undine
        err = HTTPException(400, str(e))
        err.code = "not_set_up"
        # the body's "top", "project" and "command" (the command line that sets it up, `--root` in a store not the
        # command's default), and its params'
        err.extra = {"top": str(e.top) if e.top is not None else None, "project": e.project, "command": e.command}
        return err
    if isinstance(e, (changesetmod.UnsupportedLibrary, branches.Unsupported)):     # a library no worktree can hold
        err = HTTPException(400, str(e))
        err.code = "unsupported_library"
        err.extra = {k: getattr(e, k, None) for k in ("library", "path", "kind", "target")}
        return err
    if isinstance(e, StaleEdit):
        err = HTTPException(409, str(e))
        err.code = "stale"
        return err
    if isinstance(e, HasCommits):
        err = HTTPException(409, str(e))
        err.code = "has_commits"
        return err
    if isinstance(e, branches.Unsaved):                 # the first commit, while a log holds unsaved layout edits
        err = HTTPException(409, str(e))
        err.code = "unsaved"
        err.extra = {"logs": e.logs}                    # [{"path", "lib", "cells"}]
        return err
    if isinstance(e, (live.WriteLocked, changesetmod.Busy)):     # a writer holds it; it is being closed already
        err = HTTPException(409, str(e))
        err.code = "busy"
        return err
    if isinstance(e, trust.UntrustedContent):         # a person can trust the dependency; the findings say what
        err = HTTPException(403, str(e))
        err.code = "untrusted"
        err.extra = {"name": e.name, "package": e.package, "key": e.key, "cell": e.cell,
                     "findings": [{"file": file, "kind": f.kind, "line": f.line, "text": f.text}
                                  for file, f in e.findings]}
        return err
    if isinstance(e, deps.ResolveError):              # every problem of the resolution, each with its chains
        err = HTTPException(409, str(e))
        err.code = "resolution_refused"
        err.extra = {"problems": [{"code": p.code, "message": p.message, "chains": list(p.chains)}
                                  for p in e.problems]}
        return err
    if isinstance(e, deps.NeedsFormat2):              # the command line converts the lock (no route does)
        err = HTTPException(409, str(e))
        err.code = "needs_lock_format_2"
        return err
    if isinstance(e, deps.NotDirect):                 # a name packages place: update or remove those
        err = HTTPException(400, str(e))
        err.code = "not_direct"
        err.extra = {"placed_by": list(e.placed_by)}
        return err
    if isinstance(e, compat.IncompatiblePdk):         # set-pdk: the locked packages that do not accept that PDK
        err = HTTPException(409, str(e))
        err.code = "pdk_incompatible"
        err.extra = {"packages": e.packages}
        return err
    if isinstance(e, (deps.NotSynced, locked.NotAvailable)):   # a locked package neither stored nor installed: sync
        err = HTTPException(409, str(e))
        err.code = "not_synced"
        return err
    if isinstance(e, KeyError):
        return HTTPException(404, str(e).strip("'\""))
    if isinstance(e, PermissionError):
        return HTTPException(403, str(e))
    return HTTPException(400, str(e))


class NewProject(BaseModel):
    name: str
    pdk: str | None = None


class NewLibrary(BaseModel):
    name: str


class NewDep(BaseModel):
    name: str
    git: str
    rev: str | None = None
    subdir: str | None = None
    package: str | None = None           # the package expected at that place (its manifest's name), when not `name`


class DepUpdate(BaseModel):
    rev: str | None = None


class SetPdk(BaseModel):
    pdk: str


class DepSync(BaseModel):
    offline: bool | None = None          # None: UNDINE_OFFLINE as the server has it (deps.sync)


class Confirm(BaseModel):
    """What the person confirmed in a dialog (fetchjobs.Confirmations): kind 'pdk', a PDK fetch (the fields after
    `name`), or kind 'trust', trusting the dependency installed under local name `name`."""
    kind: str
    project: str
    name: str = ""                       # kind 'trust': the dependency's local name (.undine/deps/<name>)
    commit: str = ""                     # the full commit, or at least its first 7 characters (an id's 12)
    variant: str = ""
    libraries: list[str] | None = None   # None: ciel's default set
    allow_network: bool = False          # resolve a 12-character id with ciel ls-remote when it is not installed


class PdkFetch(BaseModel):
    token: str | None = None


class Trust(BaseModel):
    """Trust the dependency installed as `name` (token: a confirmation of kind 'trust'), or take that back
    (untrust, no token)."""
    name: str
    token: str | None = None
    untrust: bool = False


# what a PDK fetch token is bound to besides the project and kind: the plan as pdkops.fetch_plan gives it
_PLAN_KEYS = ("family", "variant", "commit", "libraries", "source", "root")


class NetlistEdit(BaseModel):
    op: str                          # connect | disconnect | add_instance | tidy | undo | redo
    inst: str = ""                   # connect / disconnect
    pin: str = ""
    net: str = ""
    pins: dict[str, str] | None = None   # add_instance: pin -> net (pending until drawn)
    symbol: str = ""                 # add_instance
    props: dict | None = None
    reason: str | None = None


class ImportReq(BaseModel):
    path: str
    view: Literal["schematic", "symbol", "layout"] = "layout"


class SelectReq(BaseModel):
    box: list[float]
    point: bool = True
    mask: str | None = None
    tol: float = 0


class CreateReq(BaseModel):
    kind: str
    layer: int
    pts: list[float]
    width: float = 0
    text: str = ""
    reason: str | None = None


class RefsReq(BaseModel):
    refs: list[dict]
    dx: float = 0
    dy: float = 0
    duplicate: bool = False
    reason: str | None = None


class PlaceReq(BaseModel):
    child: str
    x: float
    y: float
    rot: int = 0
    mirror: bool = False
    reason: str | None = None


class NewCell(BaseModel):
    name: str


class NewCellViews(BaseModel):
    name: str
    views: list[str]                  # schematic | symbol | layout


class SchRoute(BaseModel):
    x: FiniteFloat
    y: FiniteFloat
    x2: FiniteFloat
    y2: FiniteFloat


class SchMoveItem(BaseModel):
    k: Literal["i", "w", "t"]
    id: str


class SchMovePreview(BaseModel):
    items: list[SchMoveItem]
    dx: FiniteFloat
    dy: FiniteFloat


class SchCopy(BaseModel):
    items: list[SchMoveItem]


class ClipboardSource(BaseModel):
    project: str
    lib: str


class ClipboardPaste(BaseModel):
    source: ClipboardSource
    payload: dict
    dx: FiniteFloat
    dy: FiniteFloat
    reason: str | None = None

    def check_scope(self, project: str, lib: str):
        if project.endswith("~base"):
            raise PermissionError("the proposal baseline is read-only")
        if self.source.project.split("~", 1)[0] != project.split("~", 1)[0] or self.source.lib != lib:
            raise ValueError("paste into a cell in the same project and library")


class RtlEdit(BaseModel):
    op: str                          # save | undo | redo
    text: str = ""
    base: str | None = None          # save: the "sha" of the text it was edited from (refused when the file moved on)
    reason: str | None = None


class IntentEdit(BaseModel):
    op: str                          # add_item | update_item | remove_item | set_port | set_supply | set_notes | replace | undo | redo
    item: dict | None = None         # add_item
    view: str | None = None          # add_item: the view whose intent gets the item (None: the cell intent)
    id: str = ""                     # update_item / remove_item
    fields: dict | None = None       # update_item / set_port (None with set_port removes the port's entry)
    name: str = ""                   # set_port: the port
    net: str = ""                    # set_supply
    role: str | None = None          # set_supply: supply | ground | None
    text: str = ""                   # set_notes / replace (YAML)
    base: str | None = None          # replace: the "sha" of the YAML it was edited from (refused when the file moved on)
    reason: str | None = None


class SchEdit(BaseModel):
    op: str
    sym: str = ""
    x: float = 0
    y: float = 0
    x2: float = 0
    y2: float = 0
    rot: int = 0
    flip: int = 0
    dx: float = 0
    dy: float = 0
    duplicate: bool = False
    items: list[dict] = []
    props: dict = {}
    kind: str = "i"
    id: str = ""
    text: str = ""
    lab: str = ""
    ids: list[str] = []
    name: str = ""
    direction: str = "inout"
    order: int = 1
    size: FiniteFloat = 0.2
    layer: int = 4
    reason: str | None = None        # the intent of the change, kept in the operation log
    changeset: str | None = None     # groups the operations of one proposal


class ChatCtx(BaseModel):
    lib: str
    cell: str
    view: str = "schematic"
    revision: str | None = None       # the design state the selection was made against
    selection: list[dict] = []        # [{k: "i"|"w"|"n"|"s", id | desc}]
    overview: bool = True


class ImageIn(BaseModel):
    media_type: str
    data: str                         # base64
    label: str = ""                   # what it shows, e.g. "selection area"
    width: int | None = None
    height: int | None = None


class ChatReq(BaseModel):
    message: str
    images: list[ImageIn] = []
    conversation: str | None = None
    context: ChatCtx
    mode: str = "ask"                 # "ask": answer only | "propose": may open a changeset and edit its working copy
    client_id: str | None = None      # the browser's id for this request: sending it again returns the same turn


class RestoreReq(BaseModel):
    to: int                           # a gseq at the end of an operation (0: as the changeset was opened)


class SelectionReq(BaseModel):
    lib: str
    cell: str
    view: str = "schematic"
    revision: str | None = None
    selection: list[dict] = []


class ConnReq(BaseModel):
    kind: str = "cloud"
    model: str | None = None
    base_url: str | None = None
    connection: str | None = None     # a registered connection (POST /api/llm/connections) instead of kind / base_url


class NewConn(BaseModel):
    label: str = ""
    provider: str                     # anthropic | openai (any OpenAI-compatible endpoint)
    api_key: str = ""
    base_url: str | None = None
    models: list[str] = []


class TapeoutReq(BaseModel):
    target: str
    params: dict = {}


class SuiteReq(BaseModel):
    target: str | None = None
    reuse: bool = True       # a check whose job already ran on the design as it is now is not run again


class NewChangeset(BaseModel):
    title: str
    reason: str = ""


class SimReq(BaseModel):
    lib: str
    cell: str
    analysis: dict | None = None
    netlist: str | None = None
    extracted: list[str] | dict[str, str | None] | None = None    # cells simulated as extracted (Server.start_sim)
    substitute: dict[str, str | None] | None = None                # cells netlisted as stand-ins (Server.start_sim)
    save_all: bool = False         # store every node, not only the testbench's nets (Server.start_sim)


class VerifyReq(BaseModel):
    lib: str
    cell: str
    deck: str | None = None
    options: dict = {}
    schematic_cell: str | None = None     # LVS against this schematic (default: same name)
    schematic_file: str | None = None     # ... or a SPICE file on the server


class PcellReq(BaseModel):
    pcell: str
    params: dict = {}
    x: float = 0
    y: float = 0
    rot: int = 0
    mirror: bool = False
    reason: str | None = None


class PlaceFromReq(BaseModel):
    lib: str
    cell: str
    x: float = 0
    y: float = 0
    rot: int = 0
    mirror: bool = False
    reason: str | None = None


def create_app(srv: Server, static_dir: Path | None) -> FastAPI:
    def schematic_write(fn):
        # Symbol edits enumerate and lock their consumers. Keep creation and all
        # schematic writes outside that enumeration/commit window, including MCP.
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with srv.changeset_lock:
                return fn(*args, **kwargs)
        return wrapped

    app = FastAPI(title="Undine design API")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        # Keep detail and HTTP status compatible with scripts/MCP. UI wording belongs to the client.
        codes = {400: "invalid_request", 401: "unauthorized", 403: "forbidden", 404: "not_found",
                 409: "conflict", 422: "validation_error", 502: "upstream_error"}
        body = {"detail": exc.detail, "code": getattr(exc, "code", codes.get(exc.status_code, "request_failed")), "params": {}}
        if getattr(exc, "files", None) is not None:       # 409 merge_conflict of a changeset's apply / revert
            body["files"] = jsonable_encoder(exc.files)
        if getattr(exc, "extra", None):                   # 400 not_set_up ("top", "project", "command"), unsupported_library
            body.update(exc.extra)
            body["params"] = dict(exc.extra)
        return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": jsonable_encoder(exc.errors()), "code": "validation_error", "params": {}}, status_code=422)

    @app.middleware("http")
    async def one_operation_per_request(request: Request, call_next):
        # the records one HTTP request writes form one operation of a changeset's timeline (live.py)
        with live.operation():
            return await call_next(request)

    # ------------------------------------------------------------- catalogue

    @app.get("/api/pdks")
    def pdks():
        return [p.public() for p in list(srv.pdks.values())]     # a copy: a rescan changes the dict

    @app.post("/api/pdks/rescan")
    def pdks_rescan():
        """Finds the PDKs again (one fetched or installed since the server started), without a restart."""
        try:
            return {"pdks": srv.rescan_pdks()}
        except Exception as e:
            raise _err(e)

    @app.get("/api/health")
    def health():
        """For the local launcher (mcp_local.py): is this Undine, which projects root, is MCP on."""
        return {"undine": True, "root": str(srv.store.root), "mcp": srv.mcp_enabled}

    @app.get("/api/projects")
    def projects():
        return [p.public() for p in srv.store.list()] + [{"name": n, "error": why} for n, why in srv.store.broken().items()]

    @app.post("/api/projects")
    def new_project(req: NewProject):
        try:
            return srv.store.create(req.name, req.pdk).public()
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}")
    def project(project: str):
        try:
            prj = srv.project(project)
            return {**prj.public(), "libraries": [l.public() for l in prj.libraries().values()]}
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/libs")
    def new_library(project: str, req: NewLibrary):
        try:
            return srv.project(project).create_library(req.name).public()
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/l/{lib}/cells")
    @schematic_write
    def new_cell_views(project: str, lib: str, req: NewCellViews, x_undine_actor: str = Header("user")):
        """Creates a cell's views in one go: an empty schematic, a symbol (pins from the cell's schematic ports when
        it has a schematic), an empty layout cell. Refused, with nothing created, when one of them exists."""
        try:
            prj = srv.project(project)
            library = prj.library(lib)
            if library.readonly or project.endswith("~base"):
                raise PermissionError("library is read-only")
            name = check_name(req.name)
            unknown = set(req.views) - {"schematic", "symbol", "rtl", "layout"}
            if unknown or not req.views:
                raise ValueError(f"views must be schematic, symbol, rtl and / or layout (got {sorted(req.views)})")
            views = [v for v in ("schematic", "symbol", "rtl", "layout") if v in req.views]
            sch, sym = library.path / f"{name}.sch", library.path / f"{name}.sym"
            exists = {"schematic": sch.exists(), "symbol": sym.exists(), "rtl": rtl.find_file(library.path, name) is not None,
                      "layout": name in library.layout_cells()[0]}
            if taken := [v for v in views if exists[v]]:
                raise ValueError(f"{name} already has a {' and a '.join(taken)}")
            srv.oplog(project, lib).guard()
            # Resolve the ports before creating anything: malformed schematics must not leave half-created views.
            ports = schematic.connectivity(sch, xschem.SymbolResolver(prj.symbol_paths())).ports if "symbol" in views and sch.is_file() else []
            library.path.mkdir(parents=True, exist_ok=True)
            if "schematic" in views:
                ed = srv.editor(project, lib, name, "schematic")
                with ed.recording("create_cell", {"view": "schematic"}, x_undine_actor, None, srv.changeset_of(project)):
                    ed.create_file(xschem.dump(xschem.XFile(version="xschem version=3.4.5 file_version=1.2\n")))
            if "symbol" in views:
                ed = srv.editor(project, lib, name, "symbol")
                with ed.recording("create_cell", {"view": "symbol"}, x_undine_actor, None, srv.changeset_of(project)):
                    ed.create_file(xschem.dump(xschem.make_symbol(ports)))
            if "rtl" in views:
                ed = srv.rtl_editor(project, lib, name)
                with ed.recording("create_cell", {"view": "rtl"}, x_undine_actor, None, srv.changeset_of(project)):
                    ed.create_file()
            if "layout" in views:
                db = srv.db(project, lib)
                with db.recording("new_cell", {"name": name}, x_undine_actor, None, srv.changeset_of(project)):
                    db.new_cell(name)
                with db.recording("save", {}, x_undine_actor, None, srv.changeset_of(project)):  # as POST .../save
                    db.save()
            return {"name": name, "created": views}
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/l/{lib}/schematics")
    @schematic_write
    def new_schematic(project: str, lib: str, req: NewCell):
        """Creates an empty schematic (Xschem format); its first edit is the first entry in the operation log."""
        try:
            library = srv.project(project).library(lib)
            if library.readonly:
                raise PermissionError("library is read-only")
            p = library.path / f"{check_name(req.name)}.sch"
            if p.exists():
                raise ValueError(f"schematic {req.name} exists")
            p.write_text(xschem.dump(xschem.XFile(version="xschem version=3.4.5 file_version=1.2\n")))
            netlistmod.sync(p, xschem.SymbolResolver(srv.project(project).symbol_paths()))     # and its netlist view
            return {"name": req.name}
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/l/{lib}/cells")
    def cells(project: str, lib: str):
        try:
            return srv.project(project).library(lib).cells()
        except Exception as e:
            raise _err(e)

    # ------------------------------------------------- library dependencies and undine.lock (deps.py)

    def _no_preview(project: str):
        if "~" in project:
            raise ValueError("library dependencies are changed in the project, not in a changeset preview")
        return srv.project(project)

    def _deps_changed(project: str):
        with srv.lock:                       # cached layouts of the dependencies (transitive ones too) are stale now
            for k in [k for k in srv.dbs if k[0] == project]:
                if srv.dbs[k].lib.origin == "dep":
                    del srv.dbs[k]

    @app.post("/api/p/{project}/pdk")
    def set_pdk(project: str, req: SetPdk):
        """Pins the project to another PDK version (e.g. from an unsupported one to pcell.SUPPORTED)."""
        try:
            if "~" in project:
                raise ValueError("the PDK is changed in the project, not in a changeset preview")
            old, new = srv.project(project).set_pdk(req.pdk)
            with srv.lock:                       # layouts and editors hold the old PDK
                for k in [k for k in srv.dbs if k[0] == project]:
                    del srv.dbs[k]
                srv.editors.clear()
            return {"from": old, "to": new}
        except Exception as e:
            raise _err(e)

    # ------------------------------------------------- PDK fetch: confirmed by the person, run as a job (fetchjobs.py)

    def _pdk_plan(project: str, allow_network: bool) -> dict:
        if "~" in project:
            raise ValueError("a PDK is fetched for the project, not for a changeset preview")
        return pdkops.fetch_plan(srv.project(project), srv.pdk_roots, allow_network)

    @app.get("/api/p/{project}/pdk/fetch-plan")
    def pdk_fetch_plan(project: str, allow_network: bool = False):
        """What a PDK fetch would run ({family, variant, commit, libraries, source, root}), for the dialog to show."""
        try:
            return _pdk_plan(project, allow_network)
        except Exception as e:
            raise _err(e)

    @app.post("/api/confirmations")
    def confirm(req: Confirm):
        """A person confirmed a PDK fetch (kind 'pdk') or trusting a dependency (kind 'trust') in a dialog: a token
        for exactly that, single use, five minutes. Only this response carries it; no MCP tool, `undine call` command
        or client method asks for one."""
        try:
            if req.kind == "trust":
                return _confirm_trust(req.project, req.name)
            if req.kind != "pdk":
                raise ValueError(f"no confirmation of kind {req.kind!r}: this Undine confirms PDK fetches (kind "
                                 "'pdk') and trusting a dependency (kind 'trust')")
            plan = _pdk_plan(req.project, req.allow_network)
            asked = req.commit.lower()
            if (req.variant != plan["variant"] or len(asked) < 7 or not plan["commit"].startswith(asked)
                    or req.libraries != plan["libraries"]):
                said = f"{req.variant or '?'}@{req.commit or '?'} with {_libs(req.libraries)}"
                raise ValueError(f"project {req.project} would fetch {plan['variant']}@{plan['commit']} with "
                                 f"{_libs(plan['libraries'])}, not {said}: show the fetch dialog again")
            if plan["source"] != ciel.DEFAULT_SOURCE:
                raise ValueError(f"the PDK's source is {plan['source']!r}, not ciel's default: fetch it from the "
                                 f"command line (`python -m undine pdk fetch {req.project}`)")
            token, expires = srv.confirmations.issue({"kind": "pdk", "project": req.project,
                                                      "allow_network": req.allow_network, **plan})
            return {"token": token, "expires": expires, "plan": plan}
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/pdk/fetch")
    def pdk_fetch(project: str, req: PdkFetch):
        """Starts `ciel fetch` for a confirmed plan, as a job: GET .../pdk/fetch/{job} follows it. The plan is made
        again now and must be the one confirmed (the lock may have changed since). The job holds no lock of the
        server's or the project's while ciel runs; then it rescans the PDKs."""
        try:
            if "~" in project:
                raise ValueError("a PDK is fetched for the project, not for a changeset preview")
            bound = srv.confirmations.take(req.token, "pdk")
            if bound["project"] != project:
                raise PermissionError(f"the confirmation token is for project {bound['project']}, not {project}")
            prj = srv.project(project)
            plan = pdkops.fetch_plan(prj, srv.pdk_roots, bound["allow_network"])
            if diff := [k for k in _PLAN_KEYS if plan[k] != bound[k]]:
                raise ValueError(f"what project {project} would fetch changed since it was confirmed "
                                 f"({', '.join(diff)}; {deps.LOCK} or project.json changed?): confirm the fetch again")

            def run(say) -> dict:
                out = pdkops.fetch(prj, plan, on_output=say, http=True)
                srv.rescan_pdks()
                return out
            job = srv.pdk_jobs.start((plan["root"], plan["family"], plan["commit"]), project, run)
            return {"job": job.public()}
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/pdk/fetch/{job}")
    def pdk_fetch_job(project: str, job: str):
        """A PDK fetch job: {id, project, state (queued | running | done | failed), log (its last lines), result
        ({path, verify}), error}. Jobs live in memory: a server restart forgets them."""
        found = srv.pdk_jobs.get(job)
        if found is None or found.project != project:
            raise HTTPException(404, f"no PDK fetch job {job} of project {project}")
        return found.public()

    # ------------------------------------------------- trust: a person's, confirmed in a dialog (store/trust.py)

    def _trust_project(project: str):
        if "~" in project:
            raise ValueError("trust is given in the project, not in a changeset preview: it reaches the changeset "
                             "from there")
        return srv.project(project)

    def _trusting(prj, name: str) -> tuple[Path, str, str]:
        """(install directory, key, installed tree) of dependency `name`, made from its files now: what a token of
        kind 'trust' is bound to and checked against."""
        d = deps._own_dir(prj.path, name)                   # a name, never a path; a linked .undine is refused
        if not d.is_dir():
            raise KeyError(f"no dependency {name} is installed in project {prj.name}: lib sync installs it")
        key = trust.key_of(d)
        if key.endswith("@" + trust._UNKNOWN) or not trust.KEY.match(key):
            raise ValueError(f"{name} was not installed by Undine, so there is no version to trust: lib sync "
                             f"{prj.name} removes or reinstalls it")
        return d, key, deps.tree_sha(d)

    def _confirm_trust(project: str, name: str) -> dict:
        prj = _trust_project(project)
        d, key, tree = _trusting(prj, name)
        token, expires = srv.confirmations.issue({"kind": "trust", "project": project, "name": name, "key": key,
                                                  "tree": tree})
        return {"token": token, "expires": expires, "key": key, "tree": tree,
                "findings": _trust_findings(name, trust.scan_dir(d))}

    @app.get("/api/p/{project}/trust")
    def trust_list(project: str):
        """What the project trusts (project.json `trusted`; in a changeset's preview the project's too) and each
        installed dependency with its key and what the trust rule finds in it, for the trust dialog."""
        try:
            prj = srv.project(project)
            keys = trust.trusted_keys(prj.path)
            root = deps._deps_root(prj.path)
            rows = []
            for d in sorted(root.iterdir()) if root.is_dir() else []:
                if d.is_dir() and not d.name.startswith("."):           # staging directories are no dependency
                    rows.append(_trust_row(d, keys))
            return {"trusted": sorted(keys), "dependencies": rows}
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/trust")
    def trust_set(project: str, req: Trust):
        """Trusts the dependency installed as `name` with a token of kind 'trust' for exactly what is installed now
        (a version or tree changed since is refused, 403), or takes that trust back (untrust: no token)."""
        try:
            prj = _trust_project(project)
            if req.untrust:
                _, key, _ = _trusting(prj, req.name)
                trust.set_trusted(prj, key, False)
                return {"key": key, "trusted": False}
            bound = srv.confirmations.take(req.token, "trust")
            if bound["project"] != project:
                raise PermissionError(f"the confirmation token is for project {bound['project']}, not {project}")
            if bound["name"] != req.name:
                raise PermissionError(f"the confirmation token is for dependency {bound['name']}, not {req.name}")
            with deps._deps_lock(prj.path):                   # no sync or update replaces it while this checks
                _, key, tree = _trusting(prj, req.name)
                now = {"key": key, "tree": tree}
                if diff := [k for k in ("key", "tree") if bound[k] != now[k]]:
                    # each part that differs, with its own values (a tree as its first 12 characters)
                    def shown(k: str, v) -> str:
                        return v[:12] if k == "tree" and isinstance(v, str) else str(v)
                    said = "; ".join(f"{k}: {shown(k, bound[k])} then, {shown(k, now[k])} now" for k in diff)
                    raise PermissionError(f"what {req.name} holds changed since it was confirmed ({said}): confirm "
                                          "the trust again")
                trust.set_trusted(prj, key, True)
            return {"key": key, "trusted": True}
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/deps")
    def deps_status(project: str):
        """The project as locked (undine.lock) vs. as installed here: dependencies, PDK, tool versions, issues."""
        try:
            return deps.status(srv.project(project))
        except Exception as e:
            raise _err(e)

    # the dependency operations fetch (git, minutes for a slow remote) without the server's lock: they take it only to
    # swap the installed files and write project.json and undine.lock (guard), so other requests go on meanwhile

    @app.post("/api/p/{project}/deps")
    def deps_add(project: str, req: NewDep):
        """Adds a library dependency (a git repository at a rev), installs it read-only and locks its commit."""
        try:
            r = deps.add(_no_preview(project), req.name, req.git, req.rev, req.subdir, guard=srv.lock,
                         local_roots=srv.dep_roots, package=req.package)
            _deps_changed(project)           # packages it reached may have moved under names already open
            return r
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/deps/sync")
    def deps_sync(project: str, req: DepSync | None = None):
        """Installs every dependency exactly as locked, from the store where it has them; refuses (400) a lock that
        does not pin what project.json names (`lib lock` resolves it) and, offline, one whose trees are not stored."""
        try:
            out = deps.sync(_no_preview(project), guard=srv.lock, local_roots=srv.dep_roots,
                            offline=req.offline if req else None)
            _deps_changed(project)
            return {"libraries": out}
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/deps/lock")
    def deps_relock(project: str):
        """Resolves the dependencies undine.lock does not pin as project.json names them (fetching them, under the
        --dep-root rule as add), and records the tool versions in use now. Returns the status and `resolved`."""
        try:
            r = deps.relock(_no_preview(project), guard=srv.lock, local_roots=srv.dep_roots)
            _deps_changed(project)
            return {**deps.status(srv.project(project)), "resolved": r["resolved"]}
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/deps/{name}/update")
    def deps_update(project: str, name: str, req: DepUpdate):
        """Moves a dependency to `rev` (default: its rev again); returns the changed files and the cells using them."""
        try:
            r = deps.update(_no_preview(project), name, req.rev, guard=srv.lock, local_roots=srv.dep_roots)
            _deps_changed(project)
            return r
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/deps/{name}/remove")
    def deps_remove(project: str, name: str):
        """Removes a dependency named in project.json, and the packages only it placed: {ok, name,
        removed_packages, kept} (kept: names other packages still place)."""
        try:
            r = deps.remove(_no_preview(project), name, guard=srv.lock)
            _deps_changed(project)
            return {"ok": True, **r}
        except Exception as e:
            raise _err(e)

    # ------------------------------------------------- git: the project's history (vcs/history.py)

    unsaved_seen: dict[tuple[str, str], tuple[int, list[str]]] = {}    # (project, lib) -> (layout version, cells)
    unsaved_gates: dict[tuple[str, str], threading.Lock] = {}            # (project, lib) -> held while hashing it

    def _open_layouts(project: str) -> list[tuple[str, LayoutDB]]:
        with srv.lock:
            return sorted(((k[1], db) for k, db in srv.dbs.items() if k[0] == project), key=lambda x: x[0])

    def _unsaved_cells(key: tuple[str, str], db: LayoutDB) -> list[str]:
        """The cells of an open layout whose content differs from the cell files, once per layout version. The
        snapshot hashes every shape of every cell (seconds for millions of shapes), so it hashes a copy, taken under
        the layout's lock (in C++, fast), outside it: edits and tiles go on meanwhile. One poll hashes a library at
        a time; another gets the last answer meanwhile (the first waits for it)."""
        with db.lock:
            seen = unsaved_seen.get(key)
            if seen is not None and seen[0] == db.version:
                return seen[1]
        with srv.lock:
            gate = unsaved_gates.setdefault(key, threading.Lock())
        if not gate.acquire(blocking=seen is None):
            return seen[1] if seen is not None else []
        try:
            with db.lock:
                version, saved = db.version, dict(db.saved)
                seen = unsaved_seen.get(key)
                if seen is not None and seen[0] == version:       # hashed while this one waited
                    return seen[1]
                copy = db.layout.dup()
            now = layoutstore.snapshot(copy)
            cells = sorted({n for n, sha in now.items() if saved.get(n) != sha} | (set(saved) - set(now)))
            unsaved_seen[key] = (version, cells)
            return cells
        finally:
            gate.release()

    def _unsaved(project: str) -> list[dict]:
        """The open layouts of the project with edits not saved to the cell files: the cells whose content differs
        from the files (new and deleted ones too). A library listed with no cell had edits that cancel out; its log
        still holds them, which the pre-commit hook refuses all the same."""
        return [{"lib": lib, "cells": _unsaved_cells((project, lib), db)}
                for lib, db in _open_layouts(project) if db.dirty]

    def _stale(project: str) -> list[dict]:
        """The open layouts with unsaved edits whose cell files changed on disk since they were read (git checkout,
        merge, pull: LayoutDB.disk_conflict), with the cells that changed there; saving them is refused until the
        edits are dropped or reloaded."""
        return [{"lib": lib, "cells": sorted(getattr(db, "disk_cells", None) or [])}
                for lib, db in _open_layouts(project) if getattr(db, "disk_conflict", False)]

    def _git_dir(project: str) -> Path:
        """The project's directory, without reading its project.json (Project does): a merge may have left it
        conflicted, and the history then reads HEAD's, to show that conflict."""
        check_name(project)
        d = srv.store.root / project
        if not (d / "project.json").is_file():
            raise KeyError(f"no project {project}")
        return d

    def _git_project(project: str) -> Path:
        if "~" in project:
            srv.project(project)                     # 404 for no such changeset
            raise ValueError("a changeset preview has no git history: it belongs to the project")
        return _git_dir(project)

    def _git_status(project: str) -> dict:
        if "~" in project:     # a changeset's working copy: a worktree of its branch, whose history is the project's
            srv.project(project)
            real, cid = project.split("~")[:2]
            g = Changesets(srv)._load(real, cid).get("git") or {}
            st = {**githistory.no_repo(preview=True), "changeset": cid, "branch": g.get("branch"), "base": g.get("base")}
            st["stale"] = _stale(project)
            st["unsaved"] = _unsaved(project)
        else:
            st = githistory.status(_git_dir(project))
            st["stale"] = _stale(project)            # first: it looks at the disk, and may read a layout again
            if st["repo"]:
                st["unsaved"] = _unsaved(project)
        st["reads"] = {lib: db.reads for lib, db in _open_layouts(project) if getattr(db, "reads", 0)}
        return st

    @app.get("/api/p/{project}/git")
    def git_status(project: str):
        """The project's working tree against HEAD by cell, the branch, HEAD's commit, a merge in progress, what
        git-setup set up, the open layouts with unsaved edits ({"repo": false} outside a repository), and those whose
        cell files changed on disk under them ("stale")."""
        try:
            return _git_status(project)
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/git/setup")
    def git_setup(project: str):
        """`undine git-setup`: git init when the project is in no repository, the attributes, the diff and merge
        drivers, the pre-commit hook; after a git init, the project's first commit too, by "user"
        (branches.first_commit: its files as they are, Undine's own output and the changeset records left out; an
        applied changeset is committed only onto a branch with a commit). Returns the status, with "hook_note": why
        the pre-commit hook was not installed (a hook of the repository's own, core.hooksPath: the line to add to it),
        None when it was; "init": it ran git init; "commit": the first commit (None: no git init, or refused);
        "commit_refused": why it was refused, as POST .../git/first-commit would answer ({"code": "unsaved", "detail",
        "logs"}: layout edits not saved; {"code": "unsupported_library", "detail", "library", "path", "kind",
        "target"}), None when it was not; "index_note": git's index not reset to it (branches._reset_index), None;
        "gitignore_added": the lines of Undine's own output it added to the project's .gitignore (status setup.gitignore
        lists those it lacks: only this adds them)."""
        try:
            from undine.__main__ import git_setup as setup

            d = _git_project(project)
            did = setup(d, say=None)
            made, refused = None, None
            if did["init"]:
                try:
                    made = branches.first_commit(d, branches.first_message(project, setup=True), "user")
                except (branches.Unsupported, branches.Unsaved) as e:      # set up all the same: History offers it
                    err = _err(e)
                    refused = {"code": err.code, "detail": err.detail, **err.extra}
            return {**_git_status(project), "hook_note": did["hook"], "init": did["init"], "gitignore_added": did["gitignore"],
                    "commit": made and made["commit"], "commit_refused": refused, "index_note": made and made["note"]}
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/git/first-commit")
    def git_first_commit(project: str):
        """The project's first commit, when its repository has none (one made before Undine made it at its setup, or
        by hand: an applied changeset is committed only onto a branch with a commit), by "user": its files as they
        are, Undine's own output and the changeset records left out (branches.first_commit). Returns the status, with
        "commit" and "index_note" (git's index not reset to it: what to run; None). 400 when the project is in no
        repository; 400 not_set_up (with "top", "project", "command") in one not set up for Undine; 400
        unsupported_library for a library the changesets' worktrees cannot hold; 409 unsaved (with "logs": [{"path",
        "lib", "cells"}]) while a log holds layout edits not saved to the cell files, which the pre-commit hook
        refuses in a commit too; 409 has_commits when its branch has a commit (one made meanwhile too). A person's:
        no agent tool makes it (agents never commit the user's branch)."""
        try:
            d = _git_project(project)
            try:
                r = branches.repo(d)
            except branches.NoRepo:
                raise ValueError(f"{project} is not in a git repository (set it up first)") from None
            if not r.set_up:
                command = branches.setup_command(d)
                raise changesetmod.NotSetUp(f"{project} is in the git repository {r.top}, which is not set up for "
                                            f"Undine: set it up first (`{command}`, or Set up in the History panel)",
                                            r.top, project, command)
            made = branches.first_commit(d, branches.first_message(project), "user")
            if made is None:
                raise HasCommits(f"{project}'s repository has a commit already: nothing was committed")
            return {**_git_status(project), "commit": made["commit"], "index_note": made["note"]}
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/git/log")
    def git_log(project: str, limit: int = 50, skip: int = 0, lib: str | None = None, cell: str | None = None):
        """The commits that changed the project (with lib and cell: that cell's files), newest first, each with the
        cells it changed against its first parent."""
        try:
            return _json(githistory.log(_git_project(project), limit, skip, lib, cell))
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/git/diff")
    def git_diff(project: str, rev: str, lib: str | None = None, cell: str | None = None):
        """The semantic diff of a commit against its first parent, or of the working tree against HEAD
        (rev=worktree), by cell and view."""
        try:
            return _json(githistory.diff(_git_project(project), rev, lib, cell))
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/git/verify")
    def git_verify(project: str, rev: str | None = None):
        """merge-check of a merge commit, or without `rev` of the merge in progress: the conflicts still open, the
        cells to verify (DRC / LVS) with why, warnings, notes. Its paths are merge-check's, from the repository's top
        (the other git routes': from the project directory)."""
        try:
            return _json(githistory.verify(_git_project(project), rev))
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/l/{lib}/import")
    @schematic_write
    def import_file(project: str, lib: str, req: ImportReq, x_undine_actor: str = Header("user")):
        try:
            src = Path(req.path).expanduser().resolve()
            if not any(src.is_relative_to(r) for r in srv.import_roots):
                raise PermissionError(f"{src} is outside the allowed import directories")
            library = srv.project(project).library(lib)
            if library.readonly or project.endswith("~base"):
                raise PermissionError("library is read-only")
            suffixes = {"schematic": {".sch"}, "symbol": {".sym"}, "layout": {".gds", ".oas", ".oasis"}}
            if src.suffix.lower() not in suffixes[req.view]:
                raise ValueError(f"Choose a {' / '.join(sorted(suffixes[req.view]))} file for the {req.view} view")
            if not src.is_file():
                raise KeyError(f"no file {src}")
            srv.oplog(project, lib).guard()
            if req.view != "layout":
                name = check_name(src.stem)
                text = src.read_text()
                model = xschem.parse(text)
                if "xschem" not in model.version.lower() or "file_version=" not in model.version:
                    raise ValueError("Choose an Xschem .sch or .sym file")
                ed = srv.editor(project, lib, name, req.view)
                with ed.recording("import_cell", {"path": str(src), "view": req.view}, x_undine_actor,
                                  None, srv.changeset_of(project)):
                    ed.create_file(text)
                return {"new_cells": [name], "view": req.view}
            # Import merges saved layout files. Do not discard a cached editor's
            # unsaved work or evict it when reading an invalid source fails.
            db = srv.db(project, lib)
            with db.lock:
                if db.dirty:
                    raise ValueError("Save the library's layout edits before importing a layout")
                added = import_layout(library, src)
                with srv.lock:
                    srv.dbs.pop((project, lib), None)
            return {"new_cells": added, "view": req.view}
        except Exception as e:
            raise _err(e)

    # ---------------------------------------------------------- schematics

    # ---------------------------------------------------------- RTL (rtl.py)

    @app.get("/api/p/{project}/l/{lib}/c/{cell}/rtl")
    def get_rtl(project: str, lib: str, cell: str):
        """The cell's RTL text, its file, the modules it defines and the ports of the cell's module."""
        try:
            return _json(_rtl_view(project, lib, cell))
        except Exception as e:
            raise _err(e)

    def _rtl_view(project: str, lib: str, cell: str) -> dict:
        ed = srv.rtl_editor(project, lib, cell)
        text = ed.text()
        src = rtl.find_file(srv.project(project).library(lib).path, cell)
        return {"text": text, "sha": oplog.sha(text), "file": src.name if src else None, "modules": rtl.modules(text),
                "ports": rtl.module_ports(text, cell), "undo": ed.can_undo_redo(),
                "readonly": srv.project(project).library(lib).readonly or project.endswith("~base")}

    @app.post("/api/p/{project}/l/{lib}/c/{cell}/rtl")
    def edit_rtl(project: str, lib: str, cell: str, req: RtlEdit, x_undine_actor: str = Header("user")):
        """save (the whole text; creates the file) | undo | redo; recorded in the library's operation log."""
        try:
            if srv.project(project).library(lib).readonly or project.endswith("~base"):
                raise PermissionError("library is read-only")
            ed = srv.rtl_editor(project, lib, cell)
            with ed.recording(req.op, {"chars": len(req.text)} if req.op == "save" else {}, x_undine_actor, req.reason,
                              srv.changeset_of(project)):
                if req.op == "save":
                    if req.base is not None and oplog.sha(ed.text()) != req.base:
                        raise StaleEdit(f"the RTL of {lib}/{cell} changed since it was loaded (a git merge, checkout or "
                                        "pull?): saving would undo that; load it again and redo your edit")
                    res = ed.save(req.text)
                elif req.op == "undo":
                    res = ed.undo()
                elif req.op == "redo":
                    res = ed.redo()
                else:
                    raise ValueError(f"unknown RTL operation {req.op}")
            return _json({"result": res, **_rtl_view(project, lib, cell)})
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/l/{lib}/c/{cell}/rtl/lint")
    def lint_rtl(project: str, lib: str, cell: str):
        """Verilator lint of the library's RTL with this cell as the top module (gate-level netlists of schematic cells
        and the PDK's cell models with it: gatelevel.rtl_inputs)."""
        try:
            prj = srv.project(project)
            return rtl.lint(prj.library(lib).path, cell, gatelevel.rtl_inputs(prj, lib, cell))
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/rtl-sim")
    def rtl_sim(project: str, req: VerifyReq):
        """A Verilator simulation of testbench cell `cell` (its library's RTL files are the sources)."""
        try:
            prj = srv.project(project)
            job = rtl.submit_sim(srv.jobs, prj.path, prj.library(req.lib).path, check_name(req.cell),
                                 f"RTL {req.lib}/{req.cell}", {"lib": req.lib, "cell": req.cell},
                                 extra=gatelevel.rtl_inputs(prj, req.lib, req.cell))
            return job.status()
        except Exception as e:
            raise _err(e)

    # ---------------------------------------------------------- LibreLane flows (librelane.py)

    def _flow_inputs(project: str, flow: dict):
        prj = srv.project(project)
        top = flow.get("top") or {}
        lib_dir = prj.library(top["lib"]).path if top.get("lib") else None
        model = srv.intent_editor(project, top["lib"], top["cell"]).model() if top.get("lib") and top.get("cell") else None
        return prj, lib_dir, model, librelane.gate_netlist(prj, flow, model)

    @app.get("/api/p/{project}/flows")
    def flows(project: str):
        try:
            return {"flows": librelane.list_flows(srv.project(project).path), "librelane": librelane.available()}
        except Exception as e:
            raise _err(e)

    @app.put("/api/p/{project}/flows/{name}")
    def save_flow(project: str, name: str, flow: dict):
        try:
            if project.endswith("~base"):
                raise PermissionError("the proposal baseline is read-only")
            return librelane.save_flow(srv.project(project).path, name, flow)
        except Exception as e:
            raise _err(e)

    @app.delete("/api/p/{project}/flows/{name}")
    def delete_flow(project: str, name: str):
        try:
            if project.endswith("~base"):
                raise PermissionError("the proposal baseline is read-only")
            librelane.delete_flow(srv.project(project).path, name)
            return {"ok": True}
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/flows/{name}/check")
    def check_flow(project: str, name: str):
        """What the flow is missing, and the LibreLane inputs it would derive now (config, pin order, their sources)."""
        try:
            prj, lib_dir, model, gate = _flow_inputs(project, flow := librelane.load_flow(srv.project(project).path, name))
            missing = librelane.requirements(flow, lib_dir, model, prj.path, gate)
            out = {"flow": flow, "missing": missing, "librelane": librelane.available()}
            if not missing:
                d = librelane.derive(flow, lib_dir, model, prj.pdk.name if prj.pdk else "sky130A", prj.path, gate)
                out.update(config=d["config"], pin_order=d["pin_order"], derived=d["derived"], ignored=d["ignored"])
            return _json(out)
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/flows/{name}/run")
    def run_flow(project: str, name: str):
        try:
            prj, lib_dir, model, gate = _flow_inputs(project, flow := librelane.load_flow(srv.project(project).path, name))
            if prj.pdk is None:
                raise ValueError("the project has no PDK")
            job = librelane.submit(srv.jobs, prj.path, lib_dir, name, flow, model, prj.pdk.root.parent, prj.pdk.name,
                                   gate=gate)
            return job.status()
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/jobs/{job}/librelane")
    def librelane_result(project: str, job: str):
        try:
            j = srv.jobs.get(srv.project(project).path, job)
            if j.status().get("kind") != "librelane":
                raise KeyError("not a LibreLane run")
            res = librelane.results(j.path)
            c = j.path / "constraints.json"
            return _json({**res, "derived": json.loads(c.read_text()) if c.is_file() else [], "log": j.log(200)})
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/jobs/{job}/import-gds")
    def librelane_import(project: str, job: str):
        """Merges the run's final GDS into the top cell's library (its layout cells, with the standard cells it uses)."""
        try:
            prj = srv.project(project)
            j = srv.jobs.get(prj.path, job)
            st = j.status()
            res = librelane.results(j.path)
            if st.get("kind") != "librelane" or not res.get("gds"):
                raise KeyError("this run has no final GDS")
            library = prj.library(st["lib"])
            if library.readonly or project.endswith("~base"):
                raise PermissionError("library is read-only")
            db = srv.db(project, st["lib"])
            with db.lock:
                if db.dirty:
                    raise ValueError("Save the library's layout edits before importing a layout")
                added = import_layout(library, j.path / res["gds"])
                with srv.lock:
                    srv.dbs.pop((project, st["lib"]), None)
            return {"new_cells": added, "top": st["cell"]}
        except Exception as e:
            raise _err(e)

    # ---------------------------------------------------------- design intent (intent.py)

    # ---------------------------------------------------------- the netlist view (netlist.py)

    @app.get("/api/p/{project}/l/{lib}/c/{cell}/netlist/model")
    def netlist_model(project: str, lib: str, cell: str):
        """The cell's connectivity: ports, instances with their pins' nets, code; and the connections the drawing
        does not show yet (pending). Tools that do not need the drawing read this."""
        try:
            return _json(srv.netlist_view(project, lib, cell))
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/l/{lib}/c/{cell}/netlist/edit")
    def netlist_edit(project: str, lib: str, cell: str, req: NetlistEdit, x_undine_actor: str = Header("user")):
        """Edits the connectivity. connect / disconnect change a pin's net without drawing it (a pending connection,
        shown as a ratsnest); a pin drawn to another net is changed in the schematic. add_instance places a symbol in
        free space (nothing drawn moves) with its pins pending on the given nets; tidy re-places what tools placed."""
        try:
            if srv.project(project).library(lib).readonly or project.endswith("~base"):
                raise PermissionError("library is read-only")
            nl = srv.netlist_editor(project, lib, cell)
            args = req.model_dump(exclude_unset=True, exclude={"op", "reason"})
            meta = (x_undine_actor, req.reason, srv.changeset_of(project))
            res = None
            if req.op in ("add_instance", "tidy"):
                sch = srv.editor(project, lib, cell, "schematic")
                if not sch.path.exists():
                    sch.create_file(xschem.dump(xschem.XFile(version="xschem version=3.4.5 file_version=1.2\n")))
                with sch.recording(req.op, args, *meta):
                    if req.op == "tidy":
                        res = sch.tidy()
                    else:
                        if not req.symbol:
                            raise ValueError("symbol is required, e.g. sky130_fd_pr/nfet_01v8.sym")
                        res = sch.add_instance(req.symbol, None, None, props=req.props or {}, placed="auto")
                if req.op == "add_instance" and req.pins:
                    with nl.recording("connect", {"inst": res, "pins": req.pins}, *meta):
                        nl.connect_many([(res, pin, net) for pin, net in req.pins.items()])
            else:
                with nl.recording(req.op, args, *meta):
                    if req.op == "connect":
                        res = nl.connect(req.inst, req.pin, req.net)
                    elif req.op == "disconnect":
                        nl.disconnect(req.inst, req.pin)
                    elif req.op == "undo":
                        res = nl.undo()
                    elif req.op == "redo":
                        res = nl.redo()
                    else:
                        raise ValueError(f"unknown netlist operation {req.op}")
            return _json({"result": res, **srv.netlist_view(project, lib, cell)})
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/l/{lib}/c/{cell}/overview")
    def cell_overview(project: str, lib: str, cell: str):
        """The cell as one page: its intent on top, then its design views, test views (testbenches) and derived views
        (netlist, checks, simulation results), each with what it is generated from and whether it is current."""
        try:
            return _json(cellviews.overview(srv, project, lib, cell))
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/l/{lib}/c/{cell}/intent")
    def get_intent(project: str, lib: str, cell: str):
        """The cell's design intent, its problems against the schematic and, per flow step, what is missing."""
        try:
            return _json(srv.intent_view(project, lib, cell))
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/l/{lib}/c/{cell}/intent/edit")
    def edit_intent(project: str, lib: str, cell: str, req: IntentEdit, x_undine_actor: str = Header("user")):
        """Applies one intent operation; each is recorded (who, what, why) in the library's operation log."""
        try:
            if srv.project(project).library(lib).readonly or project.endswith("~base"):
                raise PermissionError("library is read-only")
            ed = srv.intent_editor(project, lib, cell)
            args = req.model_dump(exclude_unset=True, exclude={"op", "reason"})
            res = None
            with ed.recording(req.op, args, x_undine_actor, req.reason, srv.changeset_of(project)):
                if req.op == "add_item":
                    res = ed.add_item(req.item or {}, x_undine_actor, req.view)
                elif req.op == "update_item":
                    ed.update_item(req.id, req.fields or {}, x_undine_actor)
                elif req.op == "remove_item":
                    ed.remove_item(req.id)
                elif req.op == "set_port":
                    ed.set_port(req.name, req.fields)
                elif req.op == "set_supply":
                    ed.set_supply(req.net, req.role)
                elif req.op == "set_notes":
                    ed.set_notes(req.text)
                elif req.op == "replace":
                    if req.base is not None and oplog.sha(_intent_yaml(ed.model())) != req.base:
                        raise StaleEdit(f"the intent of {lib}/{cell} changed since it was loaded (a git merge, checkout "
                                        "or pull?): saving the YAML would undo that; load it again and redo your edit")
                    ed.replace(req.text)
                elif req.op == "undo":
                    res = ed.undo()
                elif req.op == "redo":
                    res = ed.redo()
                else:
                    raise ValueError(f"unknown intent operation {req.op}")
            return _json({"result": res, **srv.intent_view(project, lib, cell)})
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/l/{lib}/c/{cell}/schematic/copy")
    def schematic_copy(project: str, lib: str, cell: str, req: SchCopy):
        try:
            ed = srv.editor(project, lib, cell, "schematic")
            if not ed.path.is_file():
                raise KeyError(f"no schematic {lib}/{cell}")
            snapshot = ed.copy_items([item.model_dump() for item in req.items])
            return _json({"payload": snapshot["payload"], "bbox": snapshot["bbox"], "preview": snapshot["view"]})
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/l/{lib}/c/{cell}/schematic/paste")
    @schematic_write
    def schematic_paste(project: str, lib: str, cell: str, req: ClipboardPaste,
                        x_undine_actor: str = Header("user")):
        try:
            req.check_scope(project, lib)
            if srv.project(project).library(lib).readonly:
                raise PermissionError("library is read-only")
            ed = srv.editor(project, lib, cell, "schematic")
            if not ed.path.is_file():
                raise KeyError(f"no schematic {lib}/{cell}")
            args = {"source": req.source.model_dump(), "dx": req.dx, "dy": req.dy}
            with ed.recording("paste", args, x_undine_actor, req.reason, srv.changeset_of(project)):
                result = ed.paste_items(req.payload, req.dx, req.dy)
            return _json({"result": result, "view": ed.view("schematic")})
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/l/{lib}/c/{cell}/schematic/move-preview")
    def schematic_move_preview(project: str, lib: str, cell: str, req: SchMovePreview):
        """Preview a drag with the same routing as a move, without saving or recording it."""
        try:
            ed = srv.editor(project, lib, cell, "schematic")
            if not ed.path.is_file():
                raise KeyError(f"no schematic {lib}/{cell}")
            return _json(ed.preview_move([item.model_dump() for item in req.items], req.dx, req.dy))
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/l/{lib}/c/{cell}/schematic/route")
    def schematic_route(project: str, lib: str, cell: str, req: SchRoute):
        """Preview a safe orthogonal route; does not write the design or its history."""
        try:
            ed = srv.editor(project, lib, cell, "schematic")
            if not ed.path.is_file():
                raise KeyError(f"no schematic {lib}/{cell}")
            return {"points": ed.wire_route(req.x, req.y, req.x2, req.y2)}
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/l/{lib}/c/{cell}/schematic/instance/{ref:path}")
    def schematic_instance(project: str, lib: str, cell: str, ref: str):
        try:
            return srv.editor(project, lib, cell, "schematic").instance(ref)
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/l/{lib}/c/{cell}/netlist")
    def netlist(project: str, lib: str, cell: str, backend: str = "undine", fmt: str = "spice",
                mode: str = "spice", subckt: bool | None = None):
        """SPICE netlist of a schematic. backend: undine | xschem | vlsirtools."""
        try:
            prj = srv.project(project)
            path = prj.library(lib).path / f"{cell}.sch"
            if not path.is_file():
                raise KeyError(f"no schematic {lib}/{cell}")
            res = xschem.SymbolResolver(prj.symbol_paths())
            env = srv.model_env(prj)
            if backend == "undine":
                r = schematic.spice(path, res, env, mode=mode, top_subckt=subckt)
                return {"backend": backend, "text": r.text, "warnings": r.warnings + _untrusted_note(srv, prj, r),
                        "ports": r.ports}
            if backend == "xschem":
                # Xschem evaluates tcleval( in any symbol it reads, whatever Undine's own pass makes of the cell
                trust.check_tcl(trust.untrusted_dirs(prj.path))
                return {"backend": backend, "text": schematic.xschem_netlist(path, prj.symbol_paths(), env),
                        "warnings": [], "ports": []}
            if backend == "vlsirtools":
                return {"backend": backend, "text": schematic.vlsirtools_netlist(path, res, fmt),
                        "warnings": [], "ports": []}
            raise ValueError(f"unknown backend {backend}")
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/l/{lib}/c/{cell}/{view}")
    def schematic_view(project: str, lib: str, cell: str, view: str):
        if view not in ("schematic", "symbol"):
            raise HTTPException(404, "unknown view")
        try:
            ed = srv.editor(project, lib, cell, view)
            if not ed.path.is_file():
                raise KeyError(f"no {view} for {lib}/{cell}")
            return _json(ed.view(view))
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/l/{lib}/c/{cell}/{view}/edit")
    @schematic_write
    def schematic_edit(project: str, lib: str, cell: str, view: str, req: SchEdit,
                       x_undine_actor: str = Header("user")):
        """Applies one operation; every operation is recorded (who, what, why) in the library's operation log."""
        try:
            if view not in ("schematic", "symbol"):
                raise KeyError(f"unknown view {view}")
            check_name(cell)
            if srv.project(project).library(lib).readonly or project.endswith("~base"):
                raise PermissionError("library is read-only")
            ed = srv.editor(project, lib, cell, view)
            if view == "symbol" and not ed.path.is_file() and req.op not in ("undo", "redo"):
                raise KeyError(f"no symbol for {lib}/{cell}")
            op = req.op
            args = req.model_dump(exclude_unset=True, exclude={"op", "reason", "changeset"})
            if view == "symbol" or op in ("undo", "redo"):
                return _json(symbolsync.edit(srv, project, lib, cell, view, op, args, x_undine_actor,
                                            req.reason, srv.changeset_of(project) or req.changeset))
            res = None
            with ed.recording(op, args, x_undine_actor, req.reason, srv.changeset_of(project) or req.changeset):
                if op.startswith("symbol_"):
                    res = ed.symbol_edit(op, args)
                elif op == "add_instance":
                    res = ed.add_instance(req.sym, req.x, req.y, req.rot, req.flip, req.props)
                elif op == "add_wire":
                    res = ed.add_wire(req.x, req.y, req.x2, req.y2, req.lab)
                elif op == "route_wire":
                    if view != "schematic":
                        raise ValueError("wire routing is only available in a schematic")
                    res = ed.route_wire(req.x, req.y, req.x2, req.y2, req.lab)
                elif op == "add_text":
                    res = ed.add_text(req.text, req.x, req.y)
                elif op == "move":
                    res = ed.move(req.items, req.dx, req.dy, req.duplicate)
                elif op == "orient":
                    ed.orient(req.items, req.rot, bool(req.flip))
                elif op == "delete":
                    ed.delete(req.items)
                elif op == "set_props":
                    res = ed.set_props(req.kind, req.id, req.props)
                elif op == "undo":
                    res = ed.undo()
                elif op == "redo":
                    res = ed.redo()
                else:
                    raise ValueError(f"unknown op {op}")
            return _json({"result": res, "view": ed.view(view)})
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/history")
    def history(project: str, lib: str, cell: str | None = None, view: str | None = None,
                limit: int = 200, after: int = 0):
        """Operation log of a library (newest last)."""
        try:
            srv.project(project).library(lib)
            return _json(srv.oplog(project, lib).read(cell, view, limit, after))
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/symbols")
    def symbols(project: str):
        """Symbols available for placement: [{lib, name, ref}]."""
        try:
            return srv.symbols(project)
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/symbol")
    def symbol_preview(project: str, ref: str):
        try:
            prj = srv.project(project)
            res = xschem.SymbolResolver(prj.symbol_paths())
            p = res.find(ref, None)
            if p is None:
                raise KeyError(f"symbol {ref} not found")
            d = xschem.render(p, res)
            d["pins"] = [[pn.name, pn.x, pn.y] for pn in schematic.sym_pins(xschem.parse(p.read_text(errors="replace")))]
            return d
        except Exception as e:
            raise _err(e)

    # ----------------------------------------------------------- simulation jobs

    @app.get("/api/tools")
    def tool_list():
        return {k: bool(v) for k, v in tools.versions().items()} | {"vlsirtools": _has("vlsirtools"),
                                                                    "pcells": pcell.python_exe() is not None}

    @app.post("/api/p/{project}/sim")
    def start_sim(project: str, req: SimReq):
        try:
            return srv.start_sim(project, req.lib, req.cell, req.analysis, req.netlist, extracted=req.extracted,
                                 substitute=req.substitute, save_all=req.save_all).status()
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/jobs")
    def jobs(project: str):
        try:
            return srv.jobs.list(srv.project(project).path)
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/jobs/{job}")
    def job_status(project: str, job: str):
        try:
            j = srv.jobs.get(srv.project(project).path, job)
            return {**srv.jobs.status(j), "log": j.log(200)}     # one an earlier server left: adopted, or interrupted
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/jobs/{job}/cancel")
    def job_cancel(project: str, job: str):
        try:
            j = srv.jobs.get(srv.project(project).path, job)
            srv.jobs.cancel(j)
            return j.status()
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/jobs/{job}/freshness")
    def job_freshness(project: str, job: str):
        """Whether the design, PDK, deck and tools are still what the job ran on: {fresh: true | false | null, changed}."""
        try:
            return provenance.freshness(srv, project, srv.jobs.get(srv.project(project).path, job).status())
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/jobs/{job}/netlist")
    def job_netlist(project: str, job: str):
        try:
            j = srv.jobs.get(srv.project(project).path, job)
            return Response((j.path / "netlist.cir").read_text(), media_type="text/plain")
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/jobs/{job}/data")
    def job_data(project: str, job: str, file: str | None = None, vars: str | None = None):
        try:
            j = srv.jobs.get(srv.project(project).path, job)
            if (j.path / "wave.vcd").is_file():                # an RTL simulation: its VCD, drawn as steps
                return _json(rtl.waveform_json(j.path / "wave.vcd", vars.split(",") if vars else None))
            raws = j.raw_files()
            if not raws:
                raise KeyError("this job has no raw output")
            f = next((r for r in raws if r.name == file), raws[0])
            return _json(sim.waveform_json(f, vars.split(",") if vars else None))
        except Exception as e:
            raise _err(e)

    # ----------------------------------------------------------------- DRC / LVS

    @app.post("/api/p/{project}/symbol-check")
    def start_symbol_check(project: str, req: VerifyReq):
        try:
            return srv.start_symbol_check(project, req.lib, req.cell).status()
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/jobs/{job}/symbol-check")
    def symbol_check_result(project: str, job: str):
        try:
            j = srv.jobs.get(srv.project(project).path, job)
            if j.status().get("kind") != "symbol-check":
                raise KeyError("not a schematic / symbol check")
            return symbolcheck.result(j)
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/verify/decks")
    def verify_decks(project: str):
        try:
            return {**verify.decks(srv.project(project).pdk), "klayout": bool(tools.find("klayout"))}
        except Exception as e:
            raise _err(e)

    def _layout_for(project: str, lib: str):
        prj = srv.project(project)
        if not prj.pdk:
            raise ValueError("project has no PDK")
        return prj, srv.db(project, lib)

    @app.post("/api/p/{project}/drc")
    def start_drc(project: str, req: VerifyReq):
        try:
            return srv.start_drc(project, req.lib, req.cell, req.deck, req.options).status()
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/lvs")
    def start_lvs(project: str, req: VerifyReq):
        try:
            return srv.start_lvs(project, req.lib, req.cell, req.deck, req.options, req.schematic_cell,
                                 req.schematic_file).status()
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/magic-drc")
    def start_magic_drc(project: str, req: VerifyReq):
        try:
            return srv.start_magic_drc(project, req.lib, req.cell).status()
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/netgen-lvs")
    def start_netgen_lvs(project: str, req: VerifyReq):
        try:
            return srv.start_netgen_lvs(project, req.lib, req.cell, req.schematic_cell, req.schematic_file).status()
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/pex")
    def start_pex(project: str, req: VerifyReq):
        """Parasitic extraction: options {mode: c | rc, cthresh_ff, res_threshold_ohm, res_nets (rc)}."""
        try:
            o = req.options or {}
            return srv.start_extraction(project, req.lib, req.cell, o.get("mode", "c"), o.get("cthresh_ff"), None,
                                        o.get("res_threshold_ohm"), o.get("res_nets")).status()
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/jobs/{job}/parasitics")
    def pex_result(project: str, job: str, net: str | None = None):
        try:
            j = srv.jobs.get(srv.project(project).path, job)
            f = j.path / "parasitics.json"
            if j.status().get("kind") != "pex" or not f.is_file():
                raise KeyError("no parasitic extraction result (yet)")
            return {**parasitics.compact(json.loads(f.read_text()), net), **provenance.freshness(srv, project, j.status())}
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/jobs/{job}/lvs-text")
    def lvs_text(project: str, job: str):
        try:
            j = srv.jobs.get(srv.project(project).path, job)
            f = j.path / "lvs.out"
            if not f.is_file():
                raise KeyError("no Netgen output (yet)")
            return Response(f.read_text(errors="replace"), media_type="text/plain")
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/jobs/{job}/drc")
    def drc_result(project: str, job: str, cat: int | None = None, limit: int = 500):
        try:
            j = srv.jobs.get(srv.project(project).path, job)
            f = j.path / "result.lyrdb"
            if (j.path / "result.tsv").is_file():
                return _json(magicflow.drc_items(j.path / "result.tsv", cat, limit))
            if not f.is_file():
                raise KeyError("no DRC result (yet)")
            return _json(verify.drc_items(f, cat, limit))
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/jobs/{job}/lvs")
    def lvs_result(project: str, job: str, circuit: str | None = None):
        try:
            j = srv.jobs.get(srv.project(project).path, job)
            f = j.path / "result.lvsdb"
            if not f.is_file():
                raise KeyError("no LVS result (yet)")
            return {**verify.lvs_summary(f), "detail": verify.lvs_detail(f, circuit)}
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/jobs/{job}/extracted")
    def lvs_extracted(project: str, job: str):
        try:
            j = srv.jobs.get(srv.project(project).path, job)
            f = next((j.path / n for n in ("extracted.cir", "extracted.spice") if (j.path / n).is_file()), None)  # KLayout / Magic
            if f is None:
                raise KeyError("no extracted netlist for this job")
            return Response(f.read_text(), media_type="text/plain")
        except Exception as e:
            raise _err(e)

    # -------------------------------------------------------------- layouts

    base = "/api/p/{project}/l/{lib}/layout"

    @app.get(base + "/{cell}")
    def layout_info(project: str, lib: str, cell: str):
        try:
            db = srv.db(project, lib)
            return {**db.info(cell), "readonly": db.lib.readonly}
        except Exception as e:
            raise _err(e)

    @app.get(base + "/{cell}/diff/{other}")
    def layout_diff(project: str, lib: str, cell: str, other: str, limit: int = 4000):
        """What differs in the cell's flattened geometry between `project` (the new side) and `other` (the old side):
        `added` is geometry only in `project`, `removed` only in `other`. Polygons are [hull, hole ...] point lists
        (dbu, flat x y), per layer."""
        try:
            new, old = srv.db(project, lib), srv.db(other, lib)
            if new.cell_exists(cell) is False and old.cell_exists(cell) is False:
                raise KeyError(f"no cell {lib}/{cell}")
            rn = new.regions(cell) if new.cell_exists(cell) else {}
            ro = old.regions(cell) if old.cell_exists(cell) else {}
            left = limit
            out = {"dbu": new.layout.dbu, "added": [], "removed": [], "truncated": False}
            for key in sorted(set(rn) | set(ro)):
                empty = kdb.Region()
                for name, a, b in (("added", rn.get(key, empty), ro.get(key, empty)), ("removed", ro.get(key, empty), rn.get(key, empty))):
                    d = a - b
                    if d.is_empty():
                        continue
                    polys = []
                    for p in d.each():
                        if left <= 0:
                            out["truncated"] = True
                            break
                        left -= 1
                        rings = [[v for pt in p.each_point_hull() for v in (pt.x, pt.y)]]
                        rings += [[v for pt in p.each_point_hole(h) for v in (pt.x, pt.y)] for h in range(p.holes())]
                        polys.append(rings)
                    if polys:
                        out[name].append({"layer": f"{key[0]}/{key[1]}", "polys": polys})
            return out
        except Exception as e:
            raise _err(e)

    @app.get(base + "/{cell}/layers")
    def layers(project: str, lib: str, cell: str):
        try:
            return srv.db(project, lib).layers(cell)
        except Exception as e:
            raise _err(e)

    @app.get(base + "/{cell}/tile/{level}/{ix}/{iy}.png")
    def tile(project: str, lib: str, cell: str, level: int, ix: int, iy: int,
             v: int = 0, m: str | None = None, d: int = 99):
        try:
            png = srv.db(project, lib).tile(cell, level, ix, iy, m, d)
        except Exception as e:
            raise _err(e)
        return Response(png, media_type="image/png",
                        headers={"Cache-Control": "private, max-age=3600"})

    @app.get(base + "/{cell}/integrity")
    def layout_integrity(project: str, lib: str, cell: str):
        """Shapes drawn in the cell that overlap a device's transistor area (they change the extracted L / W)."""
        try:
            return {"problems": srv.db(project, lib).integrity(cell)}
        except Exception as e:
            raise _err(e)

    @app.post(base + "/{cell}/select")
    def select(project: str, lib: str, cell: str, req: SelectReq):
        try:
            return _json(srv.db(project, lib).select(cell, req.box, req.point, req.mask, req.tol))
        except Exception as e:
            raise _err(e)

    def _edit(project: str, lib: str, op: str, args: dict, fn, actor: str = "user", reason: str | None = None):
        """Runs a layout edit inside a named operation (recorded in the operation log)."""
        try:
            db = srv.db(project, lib)
            with db.recording(op, args, actor, reason, srv.changeset_of(project)):
                res = fn(db)
            return _json({"version": db.version, "dirty": db.dirty, "result": res, "undo": db.can_undo_redo()})
        except Exception as e:
            raise _err(e)

    @app.post(base + "/{cell}/create")
    def create(project: str, lib: str, cell: str, req: CreateReq, x_undine_actor: str = Header("user")):
        return _edit(project, lib, "create", {"cell": cell, **req.model_dump(exclude={"reason"})},
                     lambda db: db.create(cell, req.kind, req.layer, req.pts, req.width, req.text), x_undine_actor, req.reason)

    @app.post(base + "/{cell}/copy")
    def layout_copy(project: str, lib: str, cell: str, req: RefsReq):
        try:
            snapshot = srv.db(project, lib).copy_items(cell, req.refs)
            return _json({"payload": snapshot["payload"], "bbox": snapshot["bbox"], "preview": snapshot["selection"]})
        except Exception as e:
            raise _err(e)

    @app.post(base + "/{cell}/paste")
    def layout_paste(project: str, lib: str, cell: str, req: ClipboardPaste,
                     x_undine_actor: str = Header("user")):
        def paste(db):
            req.check_scope(project, lib)
            return db.paste_items(cell, req.payload, req.dx, req.dy)
        return _edit(project, lib, "paste", {"cell": cell, "source": req.source.model_dump(), "dx": req.dx, "dy": req.dy},
                     paste, x_undine_actor, req.reason)

    @app.post(base + "/{cell}/delete")
    def delete(project: str, lib: str, cell: str, req: RefsReq, x_undine_actor: str = Header("user")):
        return _edit(project, lib, "delete", {"cell": cell, "count": len(req.refs)},
                     lambda db: db.delete(cell, req.refs), x_undine_actor, req.reason)

    @app.post(base + "/{cell}/move")
    def move(project: str, lib: str, cell: str, req: RefsReq, x_undine_actor: str = Header("user")):
        return _edit(project, lib, "copy" if req.duplicate else "move",
                     {"cell": cell, "count": len(req.refs), "dx": req.dx, "dy": req.dy},
                     lambda db: db.move(cell, req.refs, req.dx, req.dy, req.duplicate), x_undine_actor, req.reason)

    @app.post(base + "/{cell}/place")
    def place(project: str, lib: str, cell: str, req: PlaceReq, x_undine_actor: str = Header("user")):
        return _edit(project, lib, "place", {"cell": cell, **req.model_dump(exclude={"reason"})},
                     lambda db: db.place(cell, req.child, req.x, req.y, req.rot, req.mirror), x_undine_actor, req.reason)

    @app.post("/api/p/{project}/l/{lib}/layout-cells")
    def new_cell(project: str, lib: str, req: NewCell, x_undine_actor: str = Header("user")):
        return _edit(project, lib, "new_cell", {"name": req.name}, lambda db: db.new_cell(req.name), x_undine_actor)

    @app.post("/api/p/{project}/l/{lib}/undo")
    def undo(project: str, lib: str, cell: str | None = None, x_undine_actor: str = Header("user")):
        """Undoes the latest layout operation of `cell` (an editor passes the cell it shows), or of the library."""
        return _edit(project, lib, "undo", {"cell": cell} if cell else {}, lambda db: db.undo(cell), x_undine_actor)

    @app.post("/api/p/{project}/l/{lib}/redo")
    def redo(project: str, lib: str, cell: str | None = None, x_undine_actor: str = Header("user")):
        return _edit(project, lib, "redo", {"cell": cell} if cell else {}, lambda db: db.redo(cell), x_undine_actor)

    @app.post("/api/p/{project}/l/{lib}/save")
    def save(project: str, lib: str, x_undine_actor: str = Header("user")):
        """Writes the library's changed layout cells. Refused (400) while its cell files changed on disk under
        unsaved edits (git: the status' "stale"): it would write the cells as they were before over them."""
        return _edit(project, lib, "save", {}, lambda db: db.save(), x_undine_actor)

    @app.post("/api/p/{project}/l/{lib}/reload")
    def reload_layout(project: str, lib: str, x_undine_actor: str = Header("user")):
        """Drops the library's unsaved layout edits and reads its cell files again (after git changed them under the
        edits, or to throw the edits away). The log keeps the dropped edits as history, ended by a layout save record
        marked {"reload": true}, so they are never replayed. result: {"dropped": whether there were unsaved edits}."""
        return _edit(project, lib, "reload", {}, lambda db: db.reload(), x_undine_actor)

    # ------------------------------------------------------------------ assistant (LLM)

    def _llm_err(e: Exception) -> HTTPException:
        if isinstance(e, llm.LlmPolicyError):
            return HTTPException(403, str(e))
        if isinstance(e, llm.LlmError):
            return HTTPException(502, str(e))  # the connection failed: reported, never retried elsewhere
        return _err(e)

    @app.get("/api/llm")
    def llm_status():
        return srv.gateway.status()

    @app.post("/api/llm/connection")
    def llm_switch(req: ConnReq, x_undine_actor: str = Header("user")):
        """Explicitly switches the one active LLM connection (the server policy decides what is allowed)."""
        try:
            return srv.gateway.switch(req.kind, req.model, req.base_url, x_undine_actor, req.connection)
        except Exception as e:
            raise _llm_err(e)

    @app.post("/api/llm/connections")
    def llm_add(req: NewConn, x_undine_actor: str = Header("user")):
        """Registers an LLM connection with its API key. The key is checked by listing the endpoint's models, kept on
        the server (~/.undine/connections.json, mode 0600) and never returned. Policy applies when it is used."""
        try:
            base = (req.base_url or llm.PROVIDERS.get(req.provider, {}).get("base_url") or "").rstrip("/")
            models = req.models
            try:
                found = llm.list_models(req.provider, base, req.api_key or None)
                models = req.models or found
            except llm.LlmError as e:
                if not req.models:
                    raise llm.LlmError(f"the connection was not saved: {e}") from e
            rec = srv.gateway.conns.add(req.label, req.provider, req.api_key, base, models)
            return {**srv.gateway.status(), "added": rec["id"], "available_models": models}
        except Exception as e:
            raise _llm_err(e)

    @app.post("/api/llm/connections/{cid}/models")
    def llm_refresh_models(cid: str):
        """Asks the endpoint again which models it offers."""
        try:
            r = srv.gateway.conns.get(cid)
            srv.gateway.conns.set_models(cid, llm.list_models(r["provider"], r["base_url"], r.get("key") or None))
            return srv.gateway.status()
        except Exception as e:
            raise _llm_err(e)

    @app.delete("/api/llm/connections/{cid}")
    def llm_remove(cid: str):
        """Forgets a connection and its key; if it was active, the built-in connection is active again."""
        try:
            srv.gateway.conns.remove(cid)
            return srv.gateway.status()
        except Exception as e:
            raise _llm_err(e)

    @app.get("/api/p/{project}/revision")
    def design_revision(project: str, lib: str, cell: str):
        try:
            return {"revision": llm_context.revision(srv, project, lib, cell)}
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/chats")
    def chat_list(project: str):
        try:
            return llm.Conversations(srv.project(project.split("~")[0]).path).list()  # kept with the real project, not a working copy
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/chats/{cid}")
    def chat_get(project: str, cid: str):
        try:
            return llm.Conversations(srv.project(project.split("~")[0]).path).get(cid)
        except Exception as e:
            raise _err(e)

    def _history(conv: dict) -> list[dict]:
        """The conversation as sent to the model again: context blocks of older turns dropped (the latest two kept),
        and a turn that did not finish said as much, so it is not taken for an answer."""
        hist, users = [], [m for m in conv["messages"] if m["role"] == "user"]
        keep = {id(m) for m in users[-2:]}
        for m in conv["messages"]:
            body = (m["context"] + "\n\n" + m["text"]) if m["role"] == "user" and id(m) in keep and m.get("context") else m["text"]
            if m.get("images"):  # pictures are sent with the turn they belong to only
                body += "\n[image(s) attached to this message earlier: " + ", ".join(i.get("label") or "image" for i in m["images"]) + "]"
            if m["role"] == "assistant" and m.get("state") not in (None, "done"):
                body = (body + f"\n\n(this turn ended {m['state']} before it finished)").strip()
            hist.append({"role": m["role"], "content": body})
        return hist

    @app.post("/api/p/{project}/chat")
    def chat(project: str, req: ChatReq, x_undine_actor: str = Header("user"), wait: bool = False):
        """One request to the assistant. It is accepted at once as a turn that runs in the background (GET .../turns/<id>
        for its progress, POST .../turns/<id>/stop to stop it); wait=true runs it before answering. The browser sends
        ids; the server builds the context from the design."""
        try:
            real, _, preview = project.partition("~")
            prj = srv.project(real)      # conversations and the audit log belong to the real project; a working copy is deleted at apply
            gw = srv.gateway
            if not gw.allowed():
                raise llm.LlmPolicyError("LLM use is disabled on this server (--llm-policy none)")
            ctx = req.context
            lib = srv.project(project).library(ctx.lib)
            if not (lib.path / f"{ctx.cell}.sch").is_file() and ctx.cell not in lib.layout_cells()[0]:
                raise KeyError(f"no cell {ctx.lib}/{ctx.cell}")  # checked before anything is sent
            cur = llm_context.revision(srv, project, ctx.lib, ctx.cell)
            if ctx.revision and ctx.revision != cur:
                return {"stale": True, "current": cur, "message": "The design changed after you made the selection."}
            convs = llm.Conversations(prj.path)
            a = gw.active()
            conv = convs.get(req.conversation) if req.conversation else convs.new({"kind": a["kind"], "model": a["model"]}, persist=False)
            if (old := srv.turns.find(prj.path, conv["id"], req.client_id)):
                return {"conversation": conv["id"], "turn": old["id"], "state": old["state"]}   # the same request again
            if (busy := srv.turns.running(conv["id"])):
                raise live.WriteLocked(f"the assistant is still working on this conversation (turn {busy['id']}); stop it first")
            text, summary = llm_context.build(srv, project, ctx.model_dump())
            hist = _history(conv)
            actor = x_undine_actor if x_undine_actor != "user" else "agent:assistant"
            user_text = f"{text}\n\n{req.message}"
            images = _check_images(req.images)
            content = ([{"type": "image", "source": {"type": "base64", "media_type": i.media_type, "data": i.data}} for i in images]
                       + [{"type": "text", "text": user_text}]) if images else user_text
            if req.mode == "propose" and preview:
                # continuing a proposal: the agent works in this changeset's working copy, whoever started it
                cs_id = preview.split("~")[0]
                if Changesets(srv)._load(real, cs_id)["status"] != "open":
                    raise ValueError(f"changeset {cs_id} is closed; propose a new change from the real project")
                if conv.get("changeset") != cs_id:
                    conv["changeset"], conv["cs_seen"] = cs_id, {}
            t = srv.turns.create(prj.path, conv["id"], req.client_id, conv.get("changeset") if req.mode == "propose" else None)
            # the request is kept before any work starts; the reply replaces the placeholder when the turn ends
            conv["messages"] += [{"role": "user", "text": req.message, "context": text, "summary": summary, "t": llm._now(),
                                  "revision": cur, "mode": req.mode, "turn": t["id"],
                                  "images": [{"sha1": _sha(i), "label": i.label, "media_type": i.media_type, "width": i.width, "height": i.height}
                                             for i in images]},
                                 {"role": "assistant", "text": "", "t": llm._now(), "turn": t["id"], "state": "running"}]
            conv["title"] = conv["title"] or req.message[:60]
            convs.save(conv)
            tid, owner = t["id"], f"turn:{t['id']}"
            meta = {**summary, "actor": actor, "images": len(images)}

            def work():
                fields: dict = {}
                try:
                    if req.mode == "propose":
                        res = agent.run(srv, real, prj.path, conv, hist, content, actor, meta,
                                        on_event=lambda ev: srv.turns.event(prj.path, tid, ev),
                                        should_stop=lambda: srv.turns.stopping(tid), owner=owner, turn=tid)
                        fields = {"reply": res["reply"], "actions": res["actions"], "usage": res["usage"], "stop": res["stop"],
                                  "changeset": res["changeset"]}
                    else:
                        out = gw.complete(prj.path, "chat", llm_context.SYSTEM, hist + [{"role": "user", "content": content}],
                                          conv["id"], conv["connection"], {**summary, "actor": x_undine_actor, "images": len(images)})
                        srv.turns.event(prj.path, tid, {"type": "text", "text": out["text"]})
                        fields = {"reply": out["text"], "usage": out.get("usage"), "stop": out.get("stop")}
                except Exception as e:  # the turn records the failure; what it committed stays in the changeset
                    fields["error"] = str(e)
                finally:
                    srv.leases.release_owner(owner)
                    srv.lease_ended()                # the turn's work is committed onto the changeset's branch
                    rec = srv.turns.finish(prj.path, tid, **fields)
                    for m in conv["messages"]:
                        if m["role"] == "assistant" and m.get("turn") == tid:
                            m.update({"text": rec.get("reply") or rec.get("error") or "", "state": rec["state"], "t": llm._now(),
                                      "usage": rec.get("usage"), "stop": rec.get("stop"), "actions": rec.get("actions"),
                                      "changeset": rec.get("changeset")})
                    convs.save(conv)
                return rec

            if not wait:
                threading.Thread(target=work, name=f"turn-{tid}", daemon=True).start()
                return {"conversation": conv["id"], "turn": tid, "state": "running", "sent": summary, "revision": cur}
            rec = work()
            return {"conversation": conv["id"], "turn": tid, "state": rec["state"], "reply": rec.get("reply"),
                    "usage": rec.get("usage"), "stop": rec.get("stop"), "error": rec.get("error"), "sent": summary, "revision": cur,
                    "actions": rec.get("actions"), "changeset": rec.get("changeset"),
                    "connection": {"kind": conv["connection"]["kind"], "model": conv["connection"]["model"]}}
        except Exception as e:
            raise _llm_err(e)

    @app.get("/api/p/{project}/turns/{tid}")
    def turn_get(project: str, tid: str, after: int = 0):
        """An assistant turn: its state and the progress events from index `after` on."""
        try:
            return srv.turns.since(srv.turns.get(srv.project(project.split("~")[0]).path, tid), after)
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/turns/{tid}/stop")
    def turn_stop(project: str, tid: str):
        """Stops the turn before its next step; the changes it committed stay in the changeset."""
        try:
            return srv.turns.since(srv.turns.stop(srv.project(project.split("~")[0]).path, tid), 0)
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/live")
    def live_writers(project: str):
        """Open changesets someone (the assistant, an external agent over MCP) is editing right now, {"id", "title",
        "writer"}, and the changesets being applied, discarded or reverted, with how far ("closing", live.Closings;
        their "writer" null unless one holds the lease). Every visible screen of the project asks this every few
        seconds, so it also tells the server a screen is open (presence)."""
        try:
            srv.screens[project.split("~")[0]] = time.time()
            srv.lease_ended()                        # a lease that lapsed meanwhile: its work is committed
            cs = Changesets(srv)
            held = srv.leases.held()
            out = []
            for c in cs.list(project.split("~")[0]):
                writer = held[c["id"]].public() if c["status"] == "open" and c["id"] in held else None
                if writer or c.get("closing"):
                    out.append({"id": c["id"], "title": c["title"], "writer": writer,
                                **({"closing": c["closing"]} if c.get("closing") else {})})
            return out
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/presence")
    def presence(project: str):
        """Whether a visible Undine screen has the project open (it asked /live in the last 10 s): an external agent's
        launcher then leaves following its work to that screen instead of opening another browser tab."""
        last = srv.screens.get(project.split("~")[0])
        return {"screen": last is not None and time.time() - last < 10}

    def _real(project: str) -> str:
        """The project of a changeset route: a preview's name (`<project>~<id>`) stands for its project; the changeset
        is merged into, archived in and read from the real project, never the worktree."""
        return project.split("~", 1)[0]

    @app.get("/api/p/{project}/changesets/{cid}/activity")
    def changeset_activity(project: str, cid: str, after: int = -1):
        """What was committed in the working copy after gseq `after` (cell views, by whom), for a screen that follows the
        work; `after=-1` gives only `last`, the point to follow from. Also who is writing now."""
        try:
            cs = Changesets(srv)
            meta = cs._load(project.split("~")[0], cid)
            le = srv.leases.get(cid)
            if meta["status"] != "open":
                return {"records": [], "last": 0, "writer": None}
            recs = cs._all_records(meta)
            if (hide := srv.batching.get(cid)) is not None:   # an batch runs: shown when it is kept, never if put back
                recs = [x for x in recs if x[1]["gseq"] <= hide]
            out = [{"gseq": r["gseq"], "lib": lib, "cell": r.get("cell"), "view": r.get("view"), "op": r["op"],
                    "actor": r.get("actor"), "turn": r.get("turn")}
                   for lib, r in recs if after >= 0 and r["gseq"] > after and r.get("cell") and r.get("view")]
            return {"records": out[-200:], "last": recs[-1][1]["gseq"] if recs else 0, "writer": le.public() if le else None}
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/changesets/{cid}/timeline")
    def changeset_timeline(project: str, cid: str):
        """The working copy's operations in order, grouped by operation and assistant turn, with restore points."""
        try:
            return Changesets(srv).timeline(_real(project), cid)
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/changesets/{cid}/restore")
    def changeset_restore(project: str, cid: str, req: RestoreReq, x_undine_actor: str = Header("user")):
        """Back to the state right after gseq `to` (0: as opened); recorded, all or nothing."""
        try:
            return Changesets(srv).restore(_real(project), cid, req.to, x_undine_actor)
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/changesets/{cid}/writer/stop")
    def changeset_writer_stop(project: str, cid: str):
        """Stops whoever is editing the changeset: the assistant's turn, or an MCP session (whose later writes are refused)."""
        try:
            prj = srv.project(project.split("~")[0])
            le = srv.leases.get(cid)
            if le and le.kind == "turn":
                srv.turns.stop(prj.path, le.owner.split(":", 1)[1])
                return {"stopping": le.public()}
            le = srv.leases.stop(cid)
            srv.lease_ended()
            return {"stopped": le.public() if le else None}
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/chats/{cid}/rebind")
    def chat_rebind(project: str, cid: str, carry: bool, x_undine_actor: str = Header("user")):
        """Moves a conversation to the active connection. carry=true sends the whole history to the new endpoint
        (explicit consent); carry=false starts a fresh, empty conversation instead."""
        try:
            prj = srv.project(project.split("~")[0])
            convs = llm.Conversations(prj.path)
            a = srv.gateway.active()
            if a["kind"] not in srv.gateway.allowed():
                raise llm.LlmPolicyError(f"the server policy ({srv.gateway.policy}) does not allow {a['kind']} connections")
            old = convs.get(cid)
            if not carry:
                return convs.new({"kind": a["kind"], "model": a["model"]})
            srv.gateway.audit_event(prj.path, "rebind", conversation=cid, by=x_undine_actor,
                                    **{"from": old["connection"], "to": {"kind": a["kind"], "model": a["model"]}, "history_turns": len(old["messages"])})
            old["connection"] = {"kind": a["kind"], "model": a["model"]}
            convs.save(old)
            return old
        except Exception as e:
            raise _llm_err(e)

    @app.get("/api/p/{project}/llm/images/{sha}")
    def llm_image(project: str, sha: str):
        """A picture that was sent to an LLM (kept so that what left the machine can be reviewed)."""
        try:
            if not sha.isalnum():
                raise KeyError("bad id")
            d = llm.Gateway.image_dir(srv.project(project).path)
            for ext, mt in (("png", "image/png"), ("jpeg", "image/jpeg"), ("webp", "image/webp")):
                if (d / f"{sha}.{ext}").is_file():
                    return Response((d / f"{sha}.{ext}").read_bytes(), media_type=mt, headers={"Cache-Control": "private, max-age=86400"})
            raise KeyError("no such image")
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/llm-audit")
    def llm_audit(project: str, limit: int = 50, payload: bool = False):
        """What was sent to which LLM, newest last (payloads only on request)."""
        try:
            p = srv.project(project).path / "llm" / "audit.jsonl"
            rows = [json.loads(l) for l in p.read_text().splitlines()[-limit:]] if p.is_file() else []
            if not payload:
                for r in rows:
                    r.pop("payload", None)
            return rows
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/session/selection")
    def session_selection_set(project: str, req: SelectionReq):
        """The GUI publishes its current selection so that agents (and MCP clients) can resolve 'this transistor'."""
        try:
            srv.project(project)
            srv.selection[project] = {**req.model_dump(), "t": llm._now()}
            return {"ok": True}
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/session/selection")
    def session_selection_get(project: str):
        return srv.selection.get(project) or {"selection": []}

    mcp_sessions = mcpmod.Sessions(srv)               # kept in the root: a restart resumes them (their changesets)
    # MCP messages run in worker threads: a tool call can take minutes (run_simulation, check_specs and wait_for wait
    # for jobs), and on the event loop it would hold up every other request, of every client, meanwhile. Tool calls
    # have threads of their own, so that many waiting calls never hold up a ping, a tools/list or the UI's requests
    mcp_threads = anyio.CapacityLimiter(MCP_THREADS)

    @app.post("/mcp")
    async def mcp_endpoint(request: Request):
        if not srv.mcp_enabled:
            raise HTTPException(404, "MCP is not enabled on this server (start it with --mcp)")
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(mcpmod._rpc_error(None, -32700, "parse error"), status_code=400)
        code, out, hdr = await anyio.to_thread.run_sync(
            mcpmod.handle, srv, mcp_sessions, body, {k.lower(): v for k, v in request.headers.items()},
            limiter=mcp_threads if isinstance(body, dict) and body.get("method") == "tools/call" else None)
        if out is None:
            return Response(status_code=code, headers=hdr)
        return JSONResponse(out, status_code=code, headers=hdr)

    @app.delete("/mcp")
    async def mcp_end(request: Request):
        """The client ends its MCP session: the changesets it was writing are free for others at once."""
        h = {k.lower(): v for k, v in request.headers.items()}
        auth, expected = h.get("authorization", ""), mcpmod.ensure_token(srv.store.root)
        if not srv.mcp_enabled or not auth.lower().startswith("bearer ") or not hmac.compare_digest(auth[7:].strip(), expected):
            raise HTTPException(401 if srv.mcp_enabled else 404, "not allowed")
        sid = h.get("mcp-session-id") or ""

        def end():                                  # commits the working copies it wrote (git): not on the event loop
            mcp_sessions.drop(sid)
            srv.leases.release_owner(f"mcp:{sid}")
            srv.lease_ended()
        await anyio.to_thread.run_sync(end, limiter=mcp_threads)
        return Response(status_code=204)

    # ------------------------------------------------------------------ shuttle targets (tapeout)

    def _golden(params: dict) -> Path | None:
        """A reference layout a target compares with (ChipFoundry: Caravel's empty wrapper), from a directory the server
        may read (--import-root)."""
        raw = str(params.get("golden_gds") or "").strip()
        if not raw:
            return None
        src = Path(raw).expanduser().resolve()
        if not any(src.is_relative_to(r) for r in srv.import_roots):
            raise PermissionError(f"{src} is outside the allowed directories")
        if not src.is_file():
            raise KeyError(f"no file {src}")
        return src

    @app.get("/api/tapeout/targets")
    def tapeout_targets():
        return tapeout.targets()

    @app.post("/api/p/{project}/l/{lib}/tapeout/{cell}/check")
    def tapeout_check(project: str, lib: str, cell: str, req: TapeoutReq):
        try:
            prj = srv.project(project)
            db = srv.db(project, lib)
            golden = _golden(req.params)
            with db.lock:
                return {"checks": tapeout.run_checks(db.layout, cell, prj.pdk, req.target, req.params, golden)}
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/l/{lib}/tapeout/{cell}/suite")
    def tapeout_suite(project: str, lib: str, cell: str, req: SuiteReq | None = None):
        """Starts the sign-off checks together (Server.suite). With `reuse` (the default), a check that already ran on
        the design as it is now (same inputs: design, PDK, deck, tools, options) keeps that job instead of running again."""
        try:
            req = req or SuiteReq()
            started: list[dict] = []
            for name, run in srv.suite(project, lib, cell, req.target):
                try:
                    if isinstance(run, Exception):
                        raise run
                    j = srv.reusable(project, run.inputs) if req.reuse else None
                    reused = j is not None
                    j = j or run.launch()
                    started.append({"name": name, "job": j.id, "kind": run.kind, "tool": run.tool, "reused": reused})
                except trust.UntrustedContent as e:     # LVS of an untrusted dependency: as the 403 would say it
                    err = _err(e)
                    started.append({"name": name, "error": str(e), "code": err.code, "params": dict(err.extra)})
                except Exception as e:
                    started.append({"name": name, "error": str(e).strip("'\"")})
            return {"jobs": started}
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/l/{lib}/signoff/{cell}")
    def signoff(project: str, lib: str, cell: str, target: str | None = None):
        """Each sign-off check of the cell: its job on the design as it is now (fresh), or the latest one and what has
        changed since, or none yet."""
        try:
            return {"checks": srv.signoff(project, lib, cell, target)}
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/l/{lib}/tapeout/{cell}/export")
    def tapeout_export(project: str, lib: str, cell: str, req: TapeoutReq):
        try:
            prj = srv.project(project)
            db = srv.db(project, lib)
            golden = _golden(req.params)
            with db.lock:
                failing = [c["title"] + (f" ({c['detail']})" if c.get("detail") else "") for c in tapeout.run_checks(db.layout, cell, prj.pdk, req.target, req.params, golden) if c["status"] == "fail"]
                if failing and not req.params.get("allow_fail"):
                    raise ValueError("not exporting: failing checks: " + "; ".join(failing))
                res = tapeout.export_bundle(prj.path, db.layout, lib, cell, prj.pdk, req.target, req.params,
                                            llm_context.revision(srv, project, lib, cell),
                                            {"layout": provenance.layout_entry(db, lib, cell),
                                             "signoff": srv.signoff(project, lib, cell, req.target)}, golden)
            return {k: v for k, v in res.items() if k != "report"} | {"target": req.target}
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/tapeout/download/{target}/{name}")
    def tapeout_download(project: str, target: str, name: str):
        try:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+\.zip", name) or target not in tapeout.TARGETS:
                raise KeyError("bad name")
            f = srv.project(project).path / "tapeout" / target / name
            if not f.is_file():
                raise KeyError("no such bundle")
            return FileResponse(f, media_type="application/zip", filename=name)
        except Exception as e:
            raise _err(e)

    # ------------------------------------------------------------------ changesets

    changesets = Changesets(srv)

    @app.get("/api/p/{project}/changesets")
    def changeset_list(project: str):
        try:
            return changesets.list(_real(project))
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/changesets")
    def changeset_open(project: str, req: NewChangeset, x_undine_actor: str = Header("user")):
        """Opens a changeset: the branch undine/<project>/<id> and its working copy `<project>~<id>`, which can be
        edited with any API; nothing in the real design changes until it is applied. "git_setup": the project was in
        no git repository and is one now, "first_commit": its first commit, made then (its files as they were; null
        when none was made), "first_commit_note": what is left to do after it (git's index not reset to it) or why
        none was made (a commit of the user's made meanwhile), null
        (400 not_set_up, with "top", the repository's top, "project" and
        "command", the command line that sets it up: in a repository not set up for Undine; 400 unsupported_library, with "library", "path", "kind" and "target": a
        library a worktree cannot hold, refused before the project is made a repository)."""
        try:
            return changesets.create(project, req.title, req.reason, x_undine_actor)
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/changesets/evidence")
    def changeset_evidence_sizes(project: str):
        """The evidence of the project's closed changesets as it is now: {"bytes", "raw" (of them, the waveforms a
        prune removes), "changesets": {id: {"bytes", "raw", "files", "pruned"}}}."""
        try:
            return changesets.evidence_sizes(_real(project))
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/changesets/{cid}")
    def changeset_get(project: str, cid: str):
        try:
            return changesets.get(_real(project), cid)
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/changesets/{cid}/diff")
    def changeset_diff(project: str, cid: str, lib: str | None = None, cell: str | None = None):
        """The semantic diff of the changeset's base against its working copy (after closure: against the tip it was
        closed with), by cell and view, in /git/diff's shape; what no operation recorded shows too. 400 for a
        changeset made before changesets were git branches."""
        try:
            return _json(changesets.diff(_real(project), cid, lib, cell))
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/changesets/{cid}/verify")
    def changeset_verify(project: str, cid: str, fresh: bool = False):
        try:
            return changesets.verify(_real(project), cid, fresh)
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/changesets/{cid}/apply")
    def changeset_apply(project: str, cid: str, x_undine_actor: str = Header("user")):
        """Merges the changeset's branch into the project (changeset.py): the record, its "applied" = {"t", "by",
        "commit" (the merge commit on the user's branch, null: left in the working tree), "uncommitted", "why" (left
        uncommitted: "no_commit", the branch has no commit yet; "changes", the project had other uncommitted changes;
        "failed", the commit failed; null when committed), "paths"}. A conflict: 409 merge_conflict with "files"
        [{path, kind, conflicts}], nothing written."""
        try:
            return changesets.apply(_real(project), cid, x_undine_actor)
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/changesets/{cid}/discard")
    def changeset_discard(project: str, cid: str, x_undine_actor: str = Header("user")):
        try:
            return changesets.discard(_real(project), cid, x_undine_actor)
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/changesets/{cid}/evidence")
    def changeset_evidence(project: str, cid: str, check: bool = False):
        """A closed changeset's evidence (Changesets.evidence): its size, the manifest's jobs and what a prune removed;
        `check=true` reads every archived file against its hash (a pruned one is said to be pruned)."""
        try:
            return changesets.evidence(_real(project), cid, check)
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/changesets/{cid}/evidence/prune")
    def changeset_evidence_prune(project: str, cid: str, x_undine_actor: str = Header("user")):
        """Removes the waveforms of a closed changeset's archived runs, keeping their status, logs, results and the
        manifest's hashes (Changesets.prune_evidence). A person's call: refused (403) for an agent's actor."""
        try:
            if x_undine_actor.startswith("agent:"):
                raise PermissionError("pruning a changeset's evidence is a person's decision, not an agent's")
            return changesets.prune_evidence(_real(project), cid, x_undine_actor)
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/changesets/{cid}/revert")
    def changeset_revert(project: str, cid: str, x_undine_actor: str = Header("user")):
        """Takes an applied changeset back by the merge the other way round (the files it added kept): "reverted" as
        apply's "applied"; a conflict (a later edit of what it changed) is a 409 merge_conflict, nothing written."""
        try:
            return changesets.revert(_real(project), cid, x_undine_actor)
        except Exception as e:
            raise _err(e)

    # --------------------------------------------------------- PCells and schematic-driven layout

    @app.get("/api/p/{project}/pcells")
    def pcells(project: str):
        try:
            prj = srv.project(project)
            why = pcell.unavailable_reason(prj.pdk)
            if why:
                return {"available": False, "pcells": [], "reason": why}
            return {"available": True, "pcells": pcell.catalog(prj.pdk)}
        except Exception as e:
            raise _err(e)

    @app.post(base + "/{cell}/pcell")
    def place_pcell(project: str, lib: str, cell: str, req: PcellReq, x_undine_actor: str = Header("user")):
        try:
            prj = srv.project(project)
            db = srv.db(project, lib)
            with db.recording("place_pcell", {"cell": cell, **req.model_dump(exclude={"reason"})}, x_undine_actor,
                              req.reason, srv.changeset_of(project)):
                path, name, info = pcell.generate(prj.pdk, req.pcell, req.params)
                db.import_oas(path, {"pcell": req.pcell, "params": req.params})
                res = db.place(cell, name, req.x, req.y, req.rot, req.mirror)
            return {"version": db.version, "dirty": db.dirty, "result": res, "cell": name, "undo": db.can_undo_redo()}
        except Exception as e:
            raise _err(e)

    @app.post(base + "/{cell}/place-from")
    def place_from(project: str, lib: str, cell: str, req: PlaceFromReq, x_undine_actor: str = Header("user")):
        """Places a cell of another library (e.g. a PDK standard cell) into this layout."""
        try:
            db = srv.db(project, lib)
            src = srv.db(project, req.lib) if req.lib != lib else db
            with db.recording("place_from", {"cell": cell, **req.model_dump(exclude={"reason"})}, x_undine_actor,
                              req.reason, srv.changeset_of(project)):
                if src is not db:
                    db.copy_cell_from(src.layout, req.cell, req.lib)
                res = db.place(cell, req.cell, req.x, req.y, req.rot, req.mirror)
            return _json({"version": db.version, "dirty": db.dirty, "result": res, "undo": db.can_undo_redo()})
        except Exception as e:
            raise _err(e)

    def _sdl(project: str, lib: str, cell: str) -> sdl.Sdl:
        prj = srv.project(project)
        path = prj.library(lib).path / f"{cell}.sch"
        if not path.is_file():
            raise KeyError(f"no schematic {lib}/{cell}")
        return sdl.Sdl(prj.pdk, srv.db(project, lib), path, xschem.SymbolResolver(prj.symbol_paths()))

    @app.get("/api/p/{project}/l/{lib}/sdl/{cell}")
    def sdl_status(project: str, lib: str, cell: str):
        try:
            return _sdl(project, lib, cell).status()
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/l/{lib}/sdl/{cell}/generate")
    def sdl_generate(project: str, lib: str, cell: str, all: bool = False, reason: str | None = None,
                     x_undine_actor: str = Header("user")):
        try:
            with srv.db(project, lib).recording("sdl_generate", {"cell": cell, "all": all}, x_undine_actor, reason,
                                                srv.changeset_of(project)):
                return _sdl(project, lib, cell).generate(only_missing=not all)
        except Exception as e:
            raise _err(e)

    @app.post("/api/p/{project}/l/{lib}/sdl/{cell}/sync")
    def sdl_sync(project: str, lib: str, cell: str, names: str | None = None, reason: str | None = None,
                 x_undine_actor: str = Header("user")):
        """Updates layout devices whose schematic parameters changed (in place) and places missing ones."""
        try:
            with srv.db(project, lib).recording("sdl_sync", {"cell": cell}, x_undine_actor, reason, srv.changeset_of(project)):
                return _sdl(project, lib, cell).sync(names.split(",") if names else None)
        except Exception as e:
            raise _err(e)

    @app.get("/api/p/{project}/l/{lib}/sdl/{cell}/probe")
    def sdl_probe(project: str, lib: str, cell: str, inst: str | None = None, net: str | None = None):
        try:
            return _sdl(project, lib, cell).probe(inst, net)
        except Exception as e:
            raise _err(e)

    # ---------------------------------------------------------- web client

    if static_dir and static_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

        @app.get("/{path:path}")
        def spa(path: str):
            if path == "api" or path.startswith("api/"):  # an unknown API route is an error, not the app
                raise HTTPException(404, f"no API route /{path}")
            f = (static_dir / path).resolve()
            if path and f.is_file() and f.is_relative_to(static_dir.resolve()):
                return FileResponse(f)
            return FileResponse(static_dir / "index.html")

    return app
